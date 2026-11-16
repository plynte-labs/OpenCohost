"""Strict-TDD tests for T2 (memory-noise-amplification-20260928): dedup
before promotion.

A confident KEEP is checked against the profile's existing DURABLE rows
(promoted/imported/curated/summary, non-blank content) before it is allowed to
become a NEW 'promoted' row. A near-duplicate is routed through the same
reject/scrub path as a judge REJECT, with reason "duplicate" -- never counted
as `kept`. Two keepers in the SAME batch that duplicate each other collapse to
one durable row (intra-batch dedup), because the check reads the
transaction's own uncommitted writes.

Parent review (2026-09-28) found the ORIGINAL fallback -- a raw shared/union
Jaccard ratio -- swallowed fact UPDATES and NEGATIONS as duplicates (e.g. "24
anos" vs "25 anos", or "no le gusta" vs "le gusta"). Since contradiction-
supersede (T3) is deferred, dropping the NEW fact left the stale one winning
forever, which costs more than keeping one duplicate row. The fallback is now
containment-only (a candidate must add literally NO new significant token
beyond an existing durable row) plus a negation gate (differing negation
NEVER collapses, in either direction). Both are exercised below.

All strings below are synthetic — never real captured rows.
"""

from __future__ import annotations

import logging
import sqlite3
from collections import Counter

import pytest

from opencohost.core.memory.memoria_store import (
    MemoriaStore,
    _has_negation_marker,
    _is_lexical_duplicate,
    derive_stable_key,
)


def _row(db_path, row_id):
    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute("SELECT * FROM memorias WHERE id = ?", (row_id,)).fetchone()


def _seed_durable(db_path, profile_id, content, *, status="promoted", row_id=None):
    """Insert a durable row directly (bypassing the judge), the way an
    already-promoted/imported/curated/summary row would already exist."""
    row_id = row_id or f"durable-{status}-{abs(hash(content)) % 100000}"
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            "INSERT INTO memorias (id, profile_id, stable_key, revision, title, "
            "content, status, pinned, private, inactive, created_at, updated_at, "
            "signature, judged_at) VALUES (?, ?, ?, 1, 'titulo', ?, ?, 0, 0, 0, "
            "'2026-01-01', '2026-01-01', ?, '2026-01-01')",
            (row_id, profile_id, f"{profile_id}|{row_id}", content, status, content),
        )
    return row_id


def _seed_draft(store, profile_id, key_suffix, content):
    key = derive_stable_key(profile_id, content)
    assert key is not None
    row_id = store.upsert_draft(
        profile_id, key, f"titulo {key_suffix}", content, signature=f"firma {key_suffix}",
    )
    assert isinstance(row_id, str)
    return row_id


def _batch(store, profile_id="p"):
    return store.list_unjudged_drafts(profile_id, limit=8, now_s=100, raising=True)


# _SUBSET's significant-token set is fully CONTAINED in _SUPERSET's (adds
# nothing new); _DISTINCT shares only "juega" with either and is contained in
# neither.
_SUPERSET = "El streamer juega Hollow Knight Silksong todos los martes por la tarde."
_SUBSET = "El streamer juega Hollow Knight Silksong los martes."
_DISTINCT = "El streamer tambien juega Dota los domingos por la noche."


# ---------------------------------------------------------------------------
# 1. A candidate contained in an existing durable row is rejected as duplicate
# ---------------------------------------------------------------------------

def test_contained_candidate_of_existing_durable_row_is_rejected_as_duplicate(tmp_path):
    db = tmp_path / "memorias.db"
    store = MemoriaStore(db)
    durable_id = _seed_durable(db, "p", _SUPERSET)
    draft_id = _seed_draft(store, "p", "k1", "streamer: contenido crudo de silksong")

    result = store.apply_promotion_batch(
        "p", _batch(store), decisions=[(1, _SUBSET, False, "")],
        unresolved={}, now_s=100,
    )

    assert result.kept == 0
    assert result.rejected == 1
    assert result.reasons["duplicate"] == 1
    draft_row = _row(db, draft_id)
    assert draft_row["status"] == "draft"
    assert draft_row["inactive"] == 1
    assert draft_row["content"] == "" and draft_row["title"] == ""
    # The existing durable row is completely untouched.
    durable_row = _row(db, durable_id)
    assert durable_row["content"] == _SUPERSET
    assert durable_row["status"] == "promoted"
    with sqlite3.connect(str(db)) as conn:
        promoted_count = conn.execute(
            "SELECT COUNT(*) FROM memorias WHERE profile_id = 'p' AND status = 'promoted'"
        ).fetchone()[0]
    assert promoted_count == 1


def test_a_superset_candidate_is_kept_even_though_a_subset_row_already_exists(tmp_path):
    """The reverse direction of the containment check: the candidate adds
    "todos ... por la tarde" beyond the existing row, so it is NOT contained
    and must be promoted as its own new row."""
    db = tmp_path / "memorias.db"
    store = MemoriaStore(db)
    _seed_durable(db, "p", _SUBSET)
    draft_id = _seed_draft(store, "p", "k1", "streamer: contenido crudo de silksong extendido")

    result = store.apply_promotion_batch(
        "p", _batch(store), decisions=[(1, _SUPERSET, False, "")],
        unresolved={}, now_s=100,
    )

    assert result.kept == 1
    assert result.rejected == 0
    assert _row(db, draft_id)["status"] == "promoted"


def test_distinct_new_fact_is_kept_despite_an_existing_durable_row(tmp_path):
    db = tmp_path / "memorias.db"
    store = MemoriaStore(db)
    _seed_durable(db, "p", _SUPERSET)
    draft_id = _seed_draft(store, "p", "k1", "streamer: contenido crudo de dota")

    result = store.apply_promotion_batch(
        "p", _batch(store), decisions=[(1, _DISTINCT, False, "")],
        unresolved={}, now_s=100,
    )

    assert result.kept == 1
    assert result.rejected == 0
    draft_row = _row(db, draft_id)
    assert draft_row["status"] == "promoted"
    assert draft_row["content"] == _DISTINCT


@pytest.mark.parametrize("status", ["promoted", "imported", "curated", "summary"])
def test_every_durable_status_is_a_dedup_source(tmp_path, status):
    db = tmp_path / "memorias.db"
    store = MemoriaStore(db)
    durable_id = _seed_durable(db, "p", _SUPERSET, status=status)
    _seed_draft(store, "p", "k1", "streamer: contenido crudo de silksong")

    result = store.apply_promotion_batch(
        "p", _batch(store), decisions=[(1, _SUBSET, False, "")],
        unresolved={}, now_s=100,
    )

    assert result.reasons["duplicate"] == 1
    # The durable source row itself is never touched.
    assert _row(db, durable_id)["content"] == _SUPERSET
    assert _row(db, durable_id)["status"] == status


def test_draft_status_rows_are_never_a_dedup_source(tmp_path):
    """A sibling still-unjudged DRAFT sharing the same words must never
    block promotion -- only durable statuses count."""
    db = tmp_path / "memorias.db"
    store = MemoriaStore(db)
    _seed_draft(store, "p", "other", _SUPERSET)  # a plain draft, never judged
    draft_id = _seed_draft(store, "p", "k1", "streamer: contenido crudo de silksong 2")

    batch = [r for r in _batch(store) if r["id"] == draft_id]
    result = store.apply_promotion_batch(
        "p", batch, decisions=[(1, _SUBSET, False, "")], unresolved={}, now_s=100,
    )

    assert result.kept == 1
    assert _row(db, draft_id)["status"] == "promoted"


# ---------------------------------------------------------------------------
# 2. Intra-batch dedup: two keepers, one contained in the other, collapse to one
# ---------------------------------------------------------------------------

def test_two_keepers_in_one_batch_where_the_second_is_contained_yield_one_row(tmp_path):
    db = tmp_path / "memorias.db"
    store = MemoriaStore(db)
    first_id = _seed_draft(store, "p", "k1", "streamer: crudo silksong uno")
    second_id = _seed_draft(store, "p", "k2", "streamer: crudo silksong dos")
    batch = _batch(store)
    order = {row["id"]: i for i, row in enumerate(batch, start=1)}

    result = store.apply_promotion_batch(
        "p", batch,
        decisions=[
            (order[first_id], _SUPERSET, False, ""),
            (order[second_id], _SUBSET, False, ""),
        ],
        unresolved={}, now_s=100,
    )

    assert result.kept == 1
    assert result.rejected == 1
    assert result.reasons["duplicate"] == 1
    with sqlite3.connect(str(db)) as conn:
        promoted_count = conn.execute(
            "SELECT COUNT(*) FROM memorias WHERE profile_id = 'p' AND status = 'promoted'"
        ).fetchone()[0]
    assert promoted_count == 1


def test_two_keepers_in_one_batch_that_differ_both_get_promoted(tmp_path):
    db = tmp_path / "memorias.db"
    store = MemoriaStore(db)
    first_id = _seed_draft(store, "p", "k1", "streamer: crudo silksong info")
    second_id = _seed_draft(store, "p", "k2", "streamer: crudo dota info")
    batch = _batch(store)
    order = {row["id"]: i for i, row in enumerate(batch, start=1)}

    result = store.apply_promotion_batch(
        "p", batch,
        decisions=[
            (order[first_id], _SUPERSET, False, ""),
            (order[second_id], _DISTINCT, False, ""),
        ],
        unresolved={}, now_s=100,
    )

    assert result.kept == 2
    assert result.rejected == 0


# ---------------------------------------------------------------------------
# 3. Uncertain keeps never durable, so they skip the dedup check
# ---------------------------------------------------------------------------

def test_uncertain_keep_is_never_dedup_checked_or_blocked(tmp_path):
    db = tmp_path / "memorias.db"
    store = MemoriaStore(db)
    _seed_durable(db, "p", _SUPERSET)
    draft_id = _seed_draft(store, "p", "k1", "streamer: crudo silksong incierto")

    result = store.apply_promotion_batch(
        "p", _batch(store), decisions=[(1, _SUBSET, True, "")],
        unresolved={}, now_s=100,
    )

    assert result.kept == 1
    assert result.reasons["duplicate"] == 0
    row = _row(db, draft_id)
    assert row["status"] == "draft"
    assert row["content"] == _SUBSET


# ---------------------------------------------------------------------------
# 4. Cross-profile isolation: another profile's durable rows never count
# ---------------------------------------------------------------------------

def test_duplicate_check_never_crosses_profiles(tmp_path):
    db = tmp_path / "memorias.db"
    store = MemoriaStore(db)
    _seed_durable(db, "otro-perfil", _SUPERSET)
    draft_id = _seed_draft(store, "p", "k1", "streamer: crudo silksong cross profile")

    result = store.apply_promotion_batch(
        "p", _batch(store), decisions=[(1, _SUBSET, False, "")],
        unresolved={}, now_s=100,
    )

    assert result.kept == 1
    assert result.reasons["duplicate"] == 0
    assert _row(db, draft_id)["status"] == "promoted"


# ---------------------------------------------------------------------------
# 5. `_is_near_duplicate` — semantic hook vs. containment fallback (unit-level)
# ---------------------------------------------------------------------------

def test_is_near_duplicate_uses_containment_when_no_semantic_fn(tmp_path):
    store = MemoriaStore(tmp_path / "memorias.db")
    assert store._is_near_duplicate(_SUBSET, _SUPERSET) is True
    assert store._is_near_duplicate(_DISTINCT, _SUPERSET) is False
    # Reverse direction: the candidate adds tokens beyond the existing row.
    assert store._is_near_duplicate(_SUPERSET, _SUBSET) is False


def test_is_near_duplicate_prefers_a_real_semantic_score_over_containment(tmp_path):
    store = MemoriaStore(
        tmp_path / "memorias.db",
        semantic_similarity_fn=lambda a, b: 0.99,
    )
    # Token-DISSIMILAR, never contained: containment alone would say "not a
    # duplicate", but a real high semantic score overrides it.
    assert store._is_near_duplicate(_DISTINCT, _SUPERSET) is True


def test_is_near_duplicate_semantic_score_below_threshold_does_not_veto_containment(tmp_path):
    store = MemoriaStore(
        tmp_path / "memorias.db",
        semantic_similarity_fn=lambda a, b: 0.10,
    )
    # F2: duplicate = semantic >= threshold OR lexical containment. A real
    # (low) semantic score no longer overrides a contained pair...
    assert store._is_near_duplicate(_SUBSET, _SUPERSET) is True
    # ...and with no containment either, it is simply not a duplicate.
    assert store._is_near_duplicate(_DISTINCT, _SUPERSET) is False


def test_is_near_duplicate_falls_back_to_containment_when_semantic_returns_none(tmp_path):
    store = MemoriaStore(
        tmp_path / "memorias.db",
        semantic_similarity_fn=lambda a, b: None,
    )
    assert store._is_near_duplicate(_SUBSET, _SUPERSET) is True
    assert store._is_near_duplicate(_DISTINCT, _SUPERSET) is False


def test_is_near_duplicate_falls_back_to_containment_when_semantic_raises(tmp_path):
    def _boom(a, b):
        raise RuntimeError("worker offline")

    store = MemoriaStore(tmp_path / "memorias.db", semantic_similarity_fn=_boom)
    assert store._is_near_duplicate(_SUBSET, _SUPERSET) is True


def test_is_near_duplicate_never_raises_on_empty_text():
    store = MemoriaStore.__new__(MemoriaStore)
    store._semantic_similarity_fn = None
    assert store._is_near_duplicate("", "") is False
    assert store._is_near_duplicate("algo", "") is False


# ---------------------------------------------------------------------------
# 6. Parent-review regression: fact UPDATES and NEGATIONS must never dedup
#    (both directions), whichever text happens to be the existing durable row.
# ---------------------------------------------------------------------------

_REGRESSION_PAIRS = [
    pytest.param(
        "El operador tiene 24 años", "El operador tiene 25 años",
        id="age-update",
    ),
    pytest.param(
        "El operador juega Hades 2", "El operador juega Hades 1",
        id="version-update",
    ),
    pytest.param(
        "Al operador le gusta el cafe", "Al operador no le gusta el cafe",
        id="negation-reversal",
    ),
    pytest.param(
        "El operador trabaja con un socio contador",
        "El operador trabaja con un socio abogado",
        id="changed-detail",
    ),
]


@pytest.mark.parametrize("text_a,text_b", _REGRESSION_PAIRS)
def test_regression_pairs_are_never_duplicates_forward(text_a, text_b):
    assert _is_lexical_duplicate(text_a, text_b) is False


@pytest.mark.parametrize("text_a,text_b", _REGRESSION_PAIRS)
def test_regression_pairs_are_never_duplicates_reverse(text_a, text_b):
    assert _is_lexical_duplicate(text_b, text_a) is False


def test_negation_marker_detection():
    assert _has_negation_marker("Al operador no le gusta el cafe") is True
    assert _has_negation_marker("Al operador le gusta el cafe") is False
    assert _has_negation_marker("The operator does not like coffee") is True
    assert _has_negation_marker("The operator doesn't like coffee") is True
    assert _has_negation_marker("The operator likes coffee") is False
    assert _has_negation_marker("El operador nunca juega de noche") is True
    assert _has_negation_marker("El operador ya no juega Dota") is True
    assert _has_negation_marker("Al operador tampoco le gusta el te") is True
    assert _has_negation_marker("El operador ya no longer plays") is True


def test_negation_reversal_is_kept_end_to_end_through_apply_promotion_batch(tmp_path):
    """Integration proof for the trickiest regression pair: a durable NEGATIVE
    fact must never block a new POSITIVE fact (or vice versa) from being
    promoted as its own row -- T3 (contradiction supersede) is deferred, so
    both facts simply coexist rather than the new one being silently lost."""
    db = tmp_path / "memorias.db"
    store = MemoriaStore(db)
    _seed_durable(db, "p", "Al operador no le gusta el cafe.")
    draft_id = _seed_draft(store, "p", "k1", "streamer: crudo sobre el cafe del operador")

    result = store.apply_promotion_batch(
        "p", _batch(store),
        decisions=[(1, "Al operador le gusta el cafe.", False, "")],
        unresolved={}, now_s=100,
    )

    assert result.kept == 1
    assert result.reasons["duplicate"] == 0
    assert _row(db, draft_id)["status"] == "promoted"


def test_true_containment_duplicate_is_rejected_end_to_end(tmp_path):
    db = tmp_path / "memorias.db"
    store = MemoriaStore(db)
    _seed_durable(db, "p", "El operador es ingeniero en sistemas y desarrollador.")
    draft_id = _seed_draft(store, "p", "k1", "streamer: crudo sobre la profesion del operador")

    result = store.apply_promotion_batch(
        "p", _batch(store),
        decisions=[(1, "El operador es ingeniero en sistemas.", False, "")],
        unresolved={}, now_s=100,
    )

    assert result.kept == 0
    assert result.reasons["duplicate"] == 1
    assert _row(db, draft_id)["status"] == "draft"
    assert _row(db, draft_id)["inactive"] == 1


# ---------------------------------------------------------------------------
# 7. F1 (memory-paraphrase-contradiction-20260928): per-call
#    ``semantic_similarity_fn`` override on ``apply_promotion_batch``, and the
#    counts-only dedup stats it feeds through ``_is_near_duplicate``.
# ---------------------------------------------------------------------------

def test_is_near_duplicate_stats_records_semantic_scored_and_hit():
    store = MemoriaStore.__new__(MemoriaStore)
    store._semantic_similarity_fn = None
    stats = Counter()
    assert store._is_near_duplicate(
        _DISTINCT, _SUPERSET, similarity_fn=lambda a, b: 0.99, stats=stats,
    ) is True
    assert stats["semantic_scored"] == 1
    assert stats["semantic_hits"] == 1
    assert stats["lexical_scored"] == 0


def test_is_near_duplicate_stats_records_semantic_scored_without_hit():
    store = MemoriaStore.__new__(MemoriaStore)
    store._semantic_similarity_fn = None
    stats = Counter()
    assert store._is_near_duplicate(
        _DISTINCT, _SUPERSET, similarity_fn=lambda a, b: 0.10, stats=stats,
    ) is False
    assert stats["semantic_scored"] == 1
    assert stats["semantic_hits"] == 0


def test_is_near_duplicate_stats_records_lexical_path_when_fn_returns_none():
    store = MemoriaStore.__new__(MemoriaStore)
    store._semantic_similarity_fn = None
    stats = Counter()
    assert store._is_near_duplicate(
        _SUBSET, _SUPERSET, similarity_fn=lambda a, b: None, stats=stats,
    ) is True
    assert stats["semantic_scored"] == 0
    assert stats["lexical_scored"] == 1
    assert stats["lexical_hits"] == 1


def test_apply_promotion_batch_per_call_override_takes_priority_over_instance_hook(tmp_path):
    """A call-scoped ``semantic_similarity_fn`` wins over whatever the store
    instance was constructed with -- the F1 per-sweep closure never has to
    fight a stale/wrong instance-level hook."""
    db = tmp_path / "memorias.db"
    store = MemoriaStore(db, semantic_similarity_fn=lambda a, b: 0.0)  # would say "not a dup"
    _seed_durable(db, "p", _SUPERSET)
    draft_id = _seed_draft(store, "p", "k1", "streamer: crudo silksong override")

    result = store.apply_promotion_batch(
        "p", _batch(store), decisions=[(1, _DISTINCT, False, "")],
        unresolved={}, now_s=100,
        semantic_similarity_fn=lambda a, b: 0.99,  # overrides: says "duplicate"
    )

    assert result.kept == 0
    assert result.reasons["duplicate"] == 1
    assert _row(db, draft_id)["inactive"] == 1


def test_apply_promotion_batch_override_below_threshold_is_kept(tmp_path):
    db = tmp_path / "memorias.db"
    store = MemoriaStore(db)
    _seed_durable(db, "p", _SUPERSET)
    draft_id = _seed_draft(store, "p", "k1", "streamer: crudo silksong bajo umbral")

    result = store.apply_promotion_batch(
        "p", _batch(store), decisions=[(1, _DISTINCT, False, "")],
        unresolved={}, now_s=100,
        semantic_similarity_fn=lambda a, b: 0.10,  # below _DEDUP_SEMANTIC_THRESHOLD
    )

    assert result.kept == 1
    assert result.reasons["duplicate"] == 0
    assert _row(db, draft_id)["status"] == "promoted"


def test_apply_promotion_batch_override_missing_score_for_one_row_falls_back_to_lexical(tmp_path):
    """F1: a row with no cached/fresh vector (the override returns None for
    it -- e.g. a stale cache entry) falls back to lexical for THAT pair
    only, not the whole call."""
    db = tmp_path / "memorias.db"
    store = MemoriaStore(db)
    _seed_durable(db, "p", _SUPERSET)  # no vector available -> fn returns None
    draft_id = _seed_draft(store, "p", "k1", "streamer: crudo silksong sin vector")

    def _no_vector(a, b):
        return None  # simulates a missing/stale cache entry for every pair

    result = store.apply_promotion_batch(
        "p", _batch(store), decisions=[(1, _SUBSET, False, "")],
        unresolved={}, now_s=100,
        semantic_similarity_fn=_no_vector,
    )

    # _SUBSET is lexically contained in _SUPERSET, so the lexical fallback
    # (the only path with no score available) still rejects it.
    assert result.kept == 0
    assert result.reasons["duplicate"] == 1


def test_apply_promotion_batch_intra_batch_semantic_dedup_via_override(tmp_path):
    """Two keepers, lexically DISTINCT (no containment either way -- lexical
    alone would keep both), collapse to one durable row when the per-call
    override reports them as near-duplicates (their own precomputed
    vectors, F1 requirement 6)."""
    db = tmp_path / "memorias.db"
    store = MemoriaStore(db)
    first_id = _seed_draft(store, "p", "k1", "streamer: crudo silksong paraphrase uno")
    second_id = _seed_draft(store, "p", "k2", "streamer: crudo silksong paraphrase dos")
    batch = _batch(store)
    order = {row["id"]: i for i, row in enumerate(batch, start=1)}

    text_a = "El streamer disfruta mucho Silksong."
    text_b = "Le encanta jugar Silksong al streamer."  # lexically distinct from text_a

    def _paraphrase_fn(a, b):
        pair = {a, b}
        return 0.99 if pair == {text_a, text_b} else 0.0

    result = store.apply_promotion_batch(
        "p", batch,
        decisions=[
            (order[first_id], text_a, False, ""),
            (order[second_id], text_b, False, ""),
        ],
        unresolved={}, now_s=100,
        semantic_similarity_fn=_paraphrase_fn,
    )

    assert result.kept == 1
    assert result.rejected == 1
    assert result.reasons["duplicate"] == 1


def test_apply_promotion_batch_dedup_log_line_carries_counts_only(tmp_path, caplog):
    """RC-8: the dedup summary line names counts and a profile hash, never
    the candidate/existing memory text."""
    db = tmp_path / "memorias.db"
    store = MemoriaStore(db)
    _seed_durable(db, "p", _SUPERSET)
    _seed_draft(store, "p", "k1", "streamer: crudo silksong logging")

    with caplog.at_level(logging.INFO, logger="opencohost.core.memory.memoria_store"):
        store.apply_promotion_batch(
            "p", _batch(store), decisions=[(1, _SUBSET, False, "")],
            unresolved={}, now_s=100,
            semantic_similarity_fn=lambda a, b: None,
        )

    dedup_lines = [r.getMessage() for r in caplog.records if "memoria dedup:" in r.getMessage()]
    assert len(dedup_lines) == 1
    line = dedup_lines[0]
    assert "lexical_scored=1" in line
    assert "lexical_hits=1" in line
    assert _SUPERSET not in line
    assert _SUBSET not in line
    hash_value = line.split("profile_hash=")[1].split()[0]
    assert hash_value != "p" and len(hash_value) == 12  # hashed, not the raw profile_id


def test_list_durable_rows_for_dedup_returns_id_revision_content_status(tmp_path):
    db = tmp_path / "memorias.db"
    store = MemoriaStore(db)
    durable_id = _seed_durable(db, "p", _SUPERSET, status="promoted")
    _seed_durable(db, "otro-perfil", _SUPERSET, row_id="durable-otro-perfil")  # cross-profile, excluded
    _seed_draft(store, "p", "k1", "streamer: crudo silksong draft-only")  # draft, excluded

    rows = store.list_durable_rows_for_dedup("p")

    assert len(rows) == 1
    assert rows[0]["id"] == durable_id
    assert rows[0]["revision"] == 1
    assert rows[0]["content"] == _SUPERSET
    assert rows[0]["status"] == "promoted"


def test_list_durable_rows_for_dedup_fails_open_to_empty_list_on_sqlite_error(tmp_path, monkeypatch):
    store = MemoriaStore(tmp_path / "memorias.db")

    def _boom(*args, **kwargs):
        raise sqlite3.OperationalError("locked")

    monkeypatch.setattr(store, "_connect", _boom)
    assert store.list_durable_rows_for_dedup("p") == []


def test_true_containment_reverse_direction_is_kept_end_to_end(tmp_path):
    """The reverse of the above: the candidate ADDS "desarrollador" beyond
    the existing durable row, so it is a new fact, not a duplicate."""
    db = tmp_path / "memorias.db"
    store = MemoriaStore(db)
    _seed_durable(db, "p", "El operador es ingeniero en sistemas.")
    draft_id = _seed_draft(store, "p", "k1", "streamer: crudo sobre la profesion extendida")

    result = store.apply_promotion_batch(
        "p", _batch(store),
        decisions=[(1, "El operador es ingeniero en sistemas y desarrollador.", False, "")],
        unresolved={}, now_s=100,
    )

    assert result.kept == 1
    assert result.reasons["duplicate"] == 0
    assert _row(db, draft_id)["status"] == "promoted"
