"""Strict-TDD tests for F2 (memory-paraphrase-contradiction-20260928):
related-fact retrieval, judge ``action``/``target`` parsing, the deterministic
validator, and the reversible supersede write (schema v6 ``superseded_by``).

All memory text below is synthetic. No real MiniLM model, no IPC: embeddings
are deterministic stub vectors.
"""

from __future__ import annotations

import json
import logging
import queue
import sqlite3
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import opencohost.core.llm_engine as llm_engine
from opencohost.core.llm_engine import MotorVocalIA
from opencohost.core.memory.memoria_store import MemoriaStore, derive_stable_key
from opencohost.core.memory.promotion_parser import _parse_promotion_diagnostics
from opencohost.core.memory.promotion_related import (
    EXISTING_CLIP_CHARS,
    RELATED_BAND_HIGH,
    RELATED_BAND_LOW,
    ExistingFact,
    build_related_context,
    rank_related,
    resolve_judge_actions,
)
from opencohost.core.memory_v5_shadow.fact_index import (
    FactSemanticIndex,
    content_fingerprint,
)
from opencohost.core.memory_v5_shadow.semantic_cache import FactEmbeddingRecord


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fact(fid, content="Vive en Madrid desde hace anos.", *, profile="profile-1",
          status="promoted", revision=1, inactive=0):
    return ExistingFact(
        id=fid, profile_id=profile, status=status, revision=revision,
        content=content, inactive=inactive,
    )


def _make_motor(monkeypatch, tmp_path, *, profile_id="profile-1"):
    monkeypatch.setattr(llm_engine, "MEMORIAS_ENABLED", True)
    monkeypatch.setattr(llm_engine, "MEMORIAS_DB", str(tmp_path / "memorias.db"))
    motor = MotorVocalIA(queue.Queue(), lambda e: None)
    motor.ollama = MagicMock()
    motor.pygame = MagicMock()
    motor._provider_config = {"active_provider": "local"}
    motor._cloud_fallback_active = False
    motor._current_profile_id = profile_id
    motor._loaded_model = "m"
    motor.current_model = "m"
    motor._last_known_good_model = "m"
    resident_client = MagicMock()
    resident_client.ps.side_effect = lambda: SimpleNamespace(
        models=[SimpleNamespace(model=motor._loaded_model)]
    )
    motor.ollama.Client.return_value = resident_client
    motor._pending_model_switch = None
    motor._awaiting_first_success_after_switch = False
    motor._check_capabilities_reasoning = lambda model: False
    motor._promotion_wall_clock = lambda: 100
    return motor


def _row(tmp_path, row_id):
    with sqlite3.connect(str(tmp_path / "memorias.db")) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute("SELECT * FROM memorias WHERE id = ?", (row_id,)).fetchone()


def _seed_draft(store, profile_id, suffix, content):
    key = derive_stable_key(profile_id, content)
    assert key is not None
    row_id = store.upsert_draft(
        profile_id, key, f"titulo {suffix}", content, signature=f"firma {suffix}",
    )
    assert isinstance(row_id, str)
    return row_id


def _seed_durable(store, profile_id, content, *, status="promoted", row_id,
                  revision=1, inactive=0):
    with sqlite3.connect(str(store.db_path)) as conn:
        conn.execute(
            "INSERT INTO memorias (id, profile_id, stable_key, revision, title, "
            "content, status, pinned, private, inactive, created_at, updated_at, "
            "signature, judged_at) VALUES (?, ?, ?, ?, 'titulo', ?, ?, 0, 0, ?, "
            "'2026-01-01', '2026-01-01', ?, '2026-01-01')",
            (row_id, profile_id, f"{profile_id}|{row_id}", revision, content,
             status, inactive, content),
        )
    return row_id


class _Recorder:
    def __init__(self, reply):
        self.calls = []
        self._reply = reply

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return {"message": {"content": self._reply}}

    @property
    def prompt(self):
        return self.calls[0]["messages"][0]["content"]


def _decisions(*entries) -> str:
    return json.dumps({"decisions": list(entries)})


class _FakeWorker:
    def __init__(self, vectors_by_text=None, *, fail=False):
        self.vectors_by_text = vectors_by_text or {}
        self.calls = []
        self.fail = fail

    def embed_batch(self, texts, timeout_s=10.0):
        self.calls.append(list(texts))
        if self.fail:
            return None
        try:
            return [self.vectors_by_text[t] for t in texts]
        except KeyError:
            return None


class _FakeCacheStore:
    def __init__(self, records=None):
        self._records = list(records or [])

    def get_fact_embeddings_by_profile(self, profile_id):
        return [r for r in self._records if r.profile_id == profile_id]


def _record(fact_id, profile_id, revision, content, vector):
    return FactEmbeddingRecord(
        fact_id=fact_id, profile_id=profile_id, revision=revision,
        content_hash=content_fingerprint(content), model_id="minilm-l12",
        model_version="v1", vector=vector, created_at="2026-01-01T00:00:00Z",
    )


def _wire(motor, worker, records=None):
    motor._memory = SimpleNamespace(
        fact_index=FactSemanticIndex(_FakeCacheStore(records), worker),
    )


# ---------------------------------------------------------------------------
# 1. Related retrieval: semantic band and lexical fallback
# ---------------------------------------------------------------------------

def test_band_constants_sit_below_the_dedup_threshold():
    assert RELATED_BAND_LOW == 0.40
    assert RELATED_BAND_HIGH == 0.97


def test_rank_related_uses_the_band_and_caps_at_two_best_first():
    facts = [_fact(f"f{i}", f"contenido {i}") for i in range(5)]
    scores = {"f0": 0.39, "f1": 0.40, "f2": 0.96, "f3": 0.97, "f4": 0.70}
    picked = rank_related("draft text", facts, scores)
    assert [f.id for f in picked] == ["f2", "f4"]  # 0.97 and 0.39 excluded, top-2
    assert [f.id for f in rank_related("d", facts[:2], {"f0": 0.5, "f1": 0.40})] == ["f0", "f1"]


def test_rank_related_lexical_fallback_needs_a_shared_significant_token():
    facts = [
        _fact("a", "Juega Hollow Knight Silksong los martes."),
        _fact("b", "Cocina pasta con tomate cada domingo."),
    ]
    picked = rank_related("operador: juega silksong ahora", facts, None)
    assert [f.id for f in picked] == ["a"]
    assert rank_related("operador: nada en comun zzz", facts, None) == []


def test_rank_related_mixes_scored_and_unscored_rows():
    facts = [_fact("a", "Juega Silksong los martes."), _fact("b", "Cocina pasta.")]
    # "a" has a vector (out of band), "b" has none and shares no token.
    assert rank_related("juega silksong", facts, {"a": 0.2}) == []


def test_lexical_fallback_is_capped_at_two():
    facts = [_fact(f"f{i}", f"Juega Silksong titulo{i}.") for i in range(4)]
    assert len(rank_related("juega silksong", facts, None)) == 2


# ---------------------------------------------------------------------------
# 2. Prompt context and budget
# ---------------------------------------------------------------------------

def test_context_labels_clips_and_dedups_existing_across_drafts():
    long_text = "Vive en Madrid " + ("x" * 400)
    f1, f2 = _fact("f1", long_text), _fact("f2", "Le gusta el cafe.")
    batch = [{"content": "uno"}, {"content": "dos"}]
    ctx = build_related_context(batch, {1: [f1, f2], 2: [f1]}, budget_chars=4000)
    assert list(ctx.existing_map) == ["E1", "E2"]
    assert ctx.existing_map["E1"] is f1
    assert "E1." in ctx.existing_block and "E2." in ctx.existing_block
    line = next(l for l in ctx.existing_block.splitlines() if l.startswith("E1."))
    assert len(line) <= len("E1. ") + EXISTING_CLIP_CHARS
    assert "x" * (EXISTING_CLIP_CHARS + 1) not in ctx.existing_block
    assert "1. uno [related: E1, E2]" in ctx.draft_block
    assert "2. dos [related: E1]" in ctx.draft_block


def test_context_without_related_facts_is_empty_and_plain():
    batch = [{"content": "uno"}, {"content": "dos"}]
    ctx = build_related_context(batch, {}, budget_chars=4000)
    assert ctx.existing_block == "" and ctx.existing_map == {}
    assert ctx.draft_block == "1. uno\n2. dos"


def test_context_drops_existing_items_first_when_over_budget():
    batch = [{"content": "d" * 100}]
    facts = [_fact(f"f{i}", f"Hecho numero {i} " + "y" * 80) for i in range(3)]
    # Room for the draft plus roughly one existing line, not three.
    ctx = build_related_context(batch, {1: facts}, budget_chars=100 + 130)
    assert 0 < len(ctx.existing_map) < 3
    assert list(ctx.existing_map) == ["E1"][: len(ctx.existing_map)]
    assert ctx.draft_block.startswith("1. " + "d" * 100)  # the draft is never cut
    for label in ctx.existing_map:
        assert f"[related:" in ctx.draft_block and label in ctx.draft_block
    zero = build_related_context(batch, {1: facts}, budget_chars=100)
    assert zero.existing_map == {} and zero.existing_block == ""
    assert "[related" not in zero.draft_block


# ---------------------------------------------------------------------------
# 3. Parser defaults
# ---------------------------------------------------------------------------

def _parse(*entries, n=None):
    return _parse_promotion_diagnostics(_decisions(*entries), n or len(entries))


def _keep(i=1, **extra):
    base = {"i": i, "keep": True, "text": "Vive en Madrid ahora.", "origin": "self_stated"}
    base.update(extra)
    return base


def test_parser_reads_valid_update_and_duplicate_actions():
    diag = _parse(_keep(1, action="update", target="E2"),
                  _keep(2, action="duplicate", target="e1"))
    assert diag.actions == {1: ("update", "E2"), 2: ("duplicate", "E1")}


@pytest.mark.parametrize("extra", [
    {},                                                   # missing action
    {"action": "new"},
    {"action": "merge", "target": "E1"},                  # invalid action
    {"action": "update"},                                 # missing target
    {"action": "update", "target": "somewhere"},          # unknown target shape
    {"action": "duplicate", "target": 7},                 # non-string target
    {"action": 5, "target": "E1"},
    {"action": "update", "target": "E0"},
])
def test_parser_degrades_missing_or_invalid_action_to_new(extra):
    diag = _parse(_keep(1, **extra))
    assert diag.top_level_valid
    assert diag.actions == {}
    assert diag.decisions[0][1] is not None  # the keep itself survives


def test_parser_ignores_actions_on_rejects():
    diag = _parse({"i": 1, "keep": False, "reason": "vague", "origin": "self_stated",
                   "action": "update", "target": "E1"})
    assert diag.actions == {}


# ---------------------------------------------------------------------------
# 4. Deterministic validator
# ---------------------------------------------------------------------------

def _resolve(fact, *, action="update", uncertain=False, origin="self_stated",
             label="E1", profile="profile-1", text="Vive en Sevilla ahora."):
    decisions = [(1, text, uncertain, "")]
    return resolve_judge_actions(
        decisions, {1: (action, label)}, {1: origin}, {"E1": fact}, profile,
    )


def test_validator_accepts_a_valid_update():
    out = _resolve(_fact("f1", revision=3))
    assert out.updates == {1: ("f1", 3)}
    assert out.decisions[0][1] == "Vive en Sevilla ahora."
    assert out.stats["update"] == 1 and out.stats["degraded"] == 0


def test_validator_turns_a_valid_duplicate_into_a_duplicate_reject():
    out = _resolve(_fact("f1"), action="duplicate")
    assert out.updates == {}
    assert out.decisions == [(1, None, False, "duplicate")]
    assert out.stats["duplicate"] == 1


@pytest.mark.parametrize("kwargs,fact_kwargs,reason", [
    ({"label": "E9"}, {}, "unknown_target"),
    ({}, {"profile": "profile-2"}, "cross_profile"),
    ({"uncertain": True}, {}, "uncertain"),
    ({"origin": "shared_content"}, {}, "shared_content"),
    ({"origin": None}, {}, "origin_unknown"),
    ({}, {"inactive": 1}, "inactive_target"),
    ({}, {"status": "draft"}, "invalid_target_status"),
])
@pytest.mark.parametrize("action", ["update", "duplicate"])
def test_validator_degrades_every_invalid_case_to_new(action, kwargs, fact_kwargs, reason):
    out = _resolve(_fact("f1", **fact_kwargs), action=action, **kwargs)
    assert out.updates == {}
    assert out.decisions[0][1] == "Vive en Sevilla ahora."  # untouched keep
    assert out.stats["degraded"] == 1
    assert out.stats[f"degraded_{reason}"] == 1
    assert out.stats["update"] == 0 and out.stats["duplicate"] == 0


def test_validator_degrades_an_update_of_a_curated_target_to_new():
    out = _resolve(_fact("f1", status="curated"), action="update")
    assert out.updates == {}
    assert out.decisions[0][1] == "Vive en Sevilla ahora."
    assert out.stats["degraded_curated_target"] == 1


def test_validator_accepts_a_duplicate_of_a_curated_target():
    # A duplicate only rejects the NEW draft; the curated row is never written,
    # so the operator-touched guard does not apply. Degrading it to `new` would
    # create exactly the repeated row dedup exists to prevent.
    out = _resolve(_fact("f1", status="curated"), action="duplicate")
    assert out.decisions == [(1, None, False, "duplicate")]
    assert out.stats["duplicate"] == 1 and out.stats["degraded"] == 0


def test_validator_leaves_actionless_decisions_alone():
    out = resolve_judge_actions(
        [(1, "Texto suficiente aqui.", False, "")], {}, {1: "self_stated"}, {}, "profile-1",
    )
    assert out.stats["new"] == 1
    assert out.decisions == [(1, "Texto suficiente aqui.", False, "")]


# ---------------------------------------------------------------------------
# 5. Schema v6 migration
# ---------------------------------------------------------------------------

def _columns(path):
    with sqlite3.connect(str(path)) as conn:
        return [r[1] for r in conn.execute("PRAGMA table_info(memorias)")]


CURRENT_SCHEMA_VERSION = 8  # v8 adds the lang column; bump with the store migration


def _version(path):
    with sqlite3.connect(str(path)) as conn:
        return conn.execute("PRAGMA user_version").fetchone()[0]


def test_fresh_db_has_superseded_by_at_the_current_schema(tmp_path):
    MemoriaStore(tmp_path / "memorias.db")
    assert _version(tmp_path / "memorias.db") == CURRENT_SCHEMA_VERSION
    assert "superseded_by" in _columns(tmp_path / "memorias.db")


def test_v5_shaped_db_migrates_to_v6_idempotently(tmp_path):
    path = tmp_path / "memorias.db"
    store = MemoriaStore(path)
    _seed_durable(store, "profile-1", "Vive en Madrid desde hace anos.", row_id="old")
    with sqlite3.connect(str(path)) as conn:
        conn.execute("ALTER TABLE memorias DROP COLUMN superseded_by")
        conn.execute("PRAGMA user_version = 5")
    assert "superseded_by" not in _columns(path)

    MemoriaStore(path)
    assert _version(path) == CURRENT_SCHEMA_VERSION
    assert "superseded_by" in _columns(path)
    with sqlite3.connect(str(path)) as conn:
        assert conn.execute(
            "SELECT superseded_by, content FROM memorias WHERE id = 'old'"
        ).fetchone() == (None, "Vive en Madrid desde hace anos.")

    MemoriaStore(path)  # second open is a no-op
    assert _version(path) == CURRENT_SCHEMA_VERSION
    assert _columns(path).count("superseded_by") == 1


def test_resume_when_column_exists_but_version_is_still_5(tmp_path):
    path = tmp_path / "memorias.db"
    MemoriaStore(path)
    with sqlite3.connect(str(path)) as conn:
        conn.execute("PRAGMA user_version = 5")  # killed between ALTER and stamp
    MemoriaStore(path)
    assert _version(path) == CURRENT_SCHEMA_VERSION


# ---------------------------------------------------------------------------
# 6. Store: supersede applies in ONE transaction
# ---------------------------------------------------------------------------

_JUDGED = "Vive en Sevilla desde este ano."


def _apply(store, batch, updates, *, text=_JUDGED, uncertain=False, origin="self_stated"):
    return store.apply_promotion_batch(
        "profile-1", batch,
        decisions=[(1, text, uncertain, "")], unresolved={}, now_s=100,
        origins={1: origin}, updates=updates,
    )


def test_update_supersedes_old_row_and_promotes_new_one(tmp_path):
    store = MemoriaStore(tmp_path / "memorias.db")
    old = _seed_durable(store, "profile-1", "Vive en Madrid desde hace anos.", row_id="old")
    draft = _seed_draft(store, "profile-1", "k1", "operador: ahora vivo en sevilla")
    batch = store.list_unjudged_drafts("profile-1", limit=4, now_s=100)

    result = _apply(store, batch, {1: ("old", 1)})

    assert result.kept == 1 and result.reasons["superseded"] == 1
    new_row, old_row = _row(tmp_path, draft), _row(tmp_path, old)
    assert new_row["status"] == "promoted" and new_row["inactive"] == 0
    assert new_row["content"] == _JUDGED
    assert old_row["inactive"] == 1 and old_row["superseded_by"] == draft
    assert old_row["content"] == "Vive en Madrid desde hace anos."  # never deleted


def test_supersede_is_atomic_when_the_old_row_write_fails(tmp_path):
    store = MemoriaStore(tmp_path / "memorias.db")
    _seed_durable(store, "profile-1", "Vive en Madrid desde hace anos.", row_id="old")
    draft = _seed_draft(store, "profile-1", "k1", "operador: ahora vivo en sevilla")
    batch = store.list_unjudged_drafts("profile-1", limit=4, now_s=100)
    with sqlite3.connect(str(store.db_path)) as conn:
        conn.execute(
            "CREATE TRIGGER boom BEFORE UPDATE OF inactive ON memorias "
            "WHEN NEW.id = 'old' BEGIN SELECT RAISE(ABORT, 'boom'); END"
        )
    with pytest.raises(sqlite3.Error):
        _apply(store, batch, {1: ("old", 1)})
    assert _row(tmp_path, draft)["status"] == "draft"  # the promote rolled back too
    assert _row(tmp_path, "old")["superseded_by"] is None


@pytest.mark.parametrize("seed", [
    {"status": "curated"},
    {"inactive": 1},
    {"revision": 2},   # snapshot revision is 1: the row was edited since retrieval
])
def test_store_never_supersedes_an_invalid_target(tmp_path, seed):
    store = MemoriaStore(tmp_path / "memorias.db")
    _seed_durable(store, "profile-1", "Vive en Madrid desde hace anos.", row_id="old", **seed)
    draft = _seed_draft(store, "profile-1", "k1", "operador: ahora vivo en sevilla")
    batch = store.list_unjudged_drafts("profile-1", limit=4, now_s=100)

    result = _apply(store, batch, {1: ("old", 1)})

    assert result.kept == 1 and result.reasons["update_target_stale"] == 1
    assert _row(tmp_path, draft)["status"] == "promoted"  # degraded to new
    old = _row(tmp_path, "old")
    assert old["superseded_by"] is None
    assert old["inactive"] == seed.get("inactive", 0)


def test_store_does_not_supersede_across_profiles(tmp_path):
    store = MemoriaStore(tmp_path / "memorias.db")
    _seed_durable(store, "profile-2", "Vive en Madrid desde hace anos.", row_id="old")
    _seed_draft(store, "profile-1", "k1", "operador: ahora vivo en sevilla")
    batch = store.list_unjudged_drafts("profile-1", limit=4, now_s=100)
    _apply(store, batch, {1: ("old", 1)})
    assert _row(tmp_path, "old")["superseded_by"] is None
    assert _row(tmp_path, "old")["inactive"] == 0


def test_superseded_rows_no_longer_count_for_dedup(tmp_path):
    store = MemoriaStore(tmp_path / "memorias.db")
    _seed_durable(store, "profile-1", _JUDGED, row_id="old")
    with sqlite3.connect(str(store.db_path)) as conn:
        conn.execute("UPDATE memorias SET inactive = 1, superseded_by = 'x' WHERE id = 'old'")
    assert store.list_durable_rows_for_dedup("profile-1") == []
    with closing_conn(store) as conn:
        assert store._has_duplicate_durable_row(conn, "profile-1", _JUDGED) is False


def closing_conn(store):
    from contextlib import closing
    return closing(store._connect(timeout=1.0))


def test_list_supersede_candidates_filters_to_active_durable_public_rows(tmp_path):
    store = MemoriaStore(tmp_path / "memorias.db")
    _seed_durable(store, "profile-1", "Uno dos tres cuatro.", row_id="ok1")
    _seed_durable(store, "profile-1", "Cinco seis siete ocho.", row_id="ok2", status="curated")
    _seed_durable(store, "profile-1", "Nueve diez once doce.", row_id="off", inactive=1)
    _seed_durable(store, "profile-1", "Trece catorce quince.", row_id="sum", status="summary")
    _seed_durable(store, "profile-2", "Otro perfil aqui.", row_id="other")
    ids = {r["id"] for r in store.list_supersede_candidates("profile-1")}
    assert ids == {"ok1", "ok2"}


# ---------------------------------------------------------------------------
# 7. F1 carry-over: semantic OR lexical
# ---------------------------------------------------------------------------

def test_near_duplicate_is_semantic_or_lexical(tmp_path):
    store = MemoriaStore(tmp_path / "memorias.db")
    superset = "El streamer juega Hollow Knight Silksong todos los martes por la tarde."
    subset = "El streamer juega Hollow Knight Silksong los martes."
    # Below-threshold semantic score no longer vetoes lexical containment.
    assert store._is_near_duplicate(subset, superset, similarity_fn=lambda a, b: 0.2) is True
    # Above threshold wins even without lexical overlap.
    assert store._is_near_duplicate("aaa bbb ccc", "xxx yyy zzz",
                                    similarity_fn=lambda a, b: 0.98) is True
    # Neither: not a duplicate; negation gate still applies to the lexical side.
    assert store._is_near_duplicate("aaa bbb ccc", "xxx yyy zzz",
                                    similarity_fn=lambda a, b: 0.5) is False
    assert store._is_near_duplicate(
        "El streamer no juega Hollow Knight Silksong.", superset,
        similarity_fn=lambda a, b: 0.5) is False


# ---------------------------------------------------------------------------
# 8. Engine end to end
# ---------------------------------------------------------------------------

_OLD = "El operador vive en Madrid desde hace anos."
_RAW = "operador: ahora vivo en sevilla"


def _engine_case(monkeypatch, tmp_path, *, old_status="promoted", worker=None,
                 with_vectors=True):
    motor = _make_motor(monkeypatch, tmp_path)
    store = MemoriaStore(tmp_path / "memorias.db")
    old = _seed_durable(store, "profile-1", _OLD, row_id="old", status=old_status)
    draft = _seed_draft(store, "profile-1", "k1", _RAW)
    if worker is None:
        worker = _FakeWorker({_RAW: [1.0, 1.0], _JUDGED: [0.0, 1.0]})
    records = [_record(old, "profile-1", 1, _OLD, [1.0, 0.5])] if with_vectors else []
    _wire(motor, worker, records)
    return motor, old, draft, worker


def _judge(**extra):
    return _decisions({"i": 1, "keep": True, "text": _JUDGED, "origin": "self_stated", **extra})


def test_prompt_lists_existing_facts_and_action_rules(monkeypatch, tmp_path):
    motor, _, _, _ = _engine_case(monkeypatch, tmp_path)
    stub = _Recorder(_judge())
    motor.promote_pending_drafts(chat_callable=stub)
    prompt = stub.prompt
    assert "EXISTING FACTS" in prompt
    assert f"E1. {_OLD}" in prompt
    assert "1. " + _RAW + " [related: E1]" in prompt
    assert '"update"' in prompt and '"duplicate"' in prompt and '"new"' in prompt
    assert prompt.index("EXISTING FACTS") < prompt.index("CANDIDATES:")


def test_prompt_omits_existing_section_when_nothing_is_related(monkeypatch, tmp_path):
    motor = _make_motor(monkeypatch, tmp_path)
    store = MemoriaStore(tmp_path / "memorias.db")
    _seed_draft(store, "profile-1", "k1", _RAW)
    stub = _Recorder(_judge())
    motor.promote_pending_drafts(chat_callable=stub)
    assert "EXISTING FACTS" not in stub.prompt
    assert "[related" not in stub.prompt


def test_existing_items_are_dropped_before_drafts_when_over_budget(monkeypatch, tmp_path):
    monkeypatch.setattr(llm_engine, "_PROMOTION_DRAFT_CHARS", len(_RAW) + 10)
    motor, _, _, _ = _engine_case(monkeypatch, tmp_path)
    stub = _Recorder(_judge())
    motor.promote_pending_drafts(chat_callable=stub)
    assert "EXISTING FACTS" not in stub.prompt
    assert _RAW in stub.prompt  # the draft itself is never dropped


def test_update_supersedes_the_old_fact_end_to_end(monkeypatch, tmp_path):
    motor, old, draft, _ = _engine_case(monkeypatch, tmp_path)
    counts = motor.promote_pending_drafts(chat_callable=_Recorder(_judge(action="update", target="E1")))
    assert counts["kept"] == 1
    assert _row(tmp_path, draft)["status"] == "promoted"
    assert _row(tmp_path, old)["inactive"] == 1
    assert _row(tmp_path, old)["superseded_by"] == draft


def test_duplicate_with_a_target_rejects_the_draft(monkeypatch, tmp_path):
    motor, old, draft, _ = _engine_case(monkeypatch, tmp_path)
    counts = motor.promote_pending_drafts(chat_callable=_Recorder(_judge(action="duplicate", target="E1")))
    assert counts["kept"] == 0 and counts["reasons"]["duplicate"] == 1
    assert _row(tmp_path, draft)["inactive"] == 1
    assert _row(tmp_path, old)["inactive"] == 0


def test_curated_target_degrades_to_new_end_to_end(monkeypatch, tmp_path):
    motor, old, draft, _ = _engine_case(monkeypatch, tmp_path, old_status="curated")
    counts = motor.promote_pending_drafts(chat_callable=_Recorder(_judge(action="update", target="E1")))
    assert counts["kept"] == 1
    assert _row(tmp_path, old)["inactive"] == 0 and _row(tmp_path, old)["superseded_by"] is None
    assert _row(tmp_path, draft)["status"] == "promoted"


def test_unknown_target_label_degrades_to_new_end_to_end(monkeypatch, tmp_path):
    motor, old, draft, _ = _engine_case(monkeypatch, tmp_path)
    counts = motor.promote_pending_drafts(chat_callable=_Recorder(_judge(action="update", target="E7")))
    assert counts["kept"] == 1
    assert _row(tmp_path, old)["inactive"] == 0


def test_shared_content_never_supersedes_end_to_end(monkeypatch, tmp_path):
    motor, old, _, _ = _engine_case(monkeypatch, tmp_path)
    reply = _decisions({"i": 1, "keep": True, "text": _JUDGED,
                        "origin": "shared_content", "action": "update", "target": "E1"})
    motor.promote_pending_drafts(chat_callable=_Recorder(reply))
    assert _row(tmp_path, old)["inactive"] == 0


def test_worker_failure_still_completes_with_lexical_related_fallback(monkeypatch, tmp_path):
    worker = _FakeWorker(fail=True)
    motor, old, draft, _ = _engine_case(monkeypatch, tmp_path, worker=worker, with_vectors=False)
    # Shares the significant token "vivo/vive"-independent word: use a shared noun.
    with sqlite3.connect(str(tmp_path / "memorias.db")) as conn:
        conn.execute("UPDATE memorias SET content = ? WHERE id = 'old'",
                     ("El operador vive en Sevilla o Madrid segun la epoca.",))
    stub = _Recorder(_judge(action="update", target="E1"))
    counts = motor.promote_pending_drafts(chat_callable=stub)
    assert counts["skipped"] == "" and counts["kept"] == 1
    assert "EXISTING FACTS" in stub.prompt  # lexical fallback found the related row
    assert _row(tmp_path, old)["superseded_by"] == draft


def test_no_memory_text_in_logs_on_the_supersede_path(monkeypatch, tmp_path, caplog):
    motor, _, _, _ = _engine_case(monkeypatch, tmp_path)
    with caplog.at_level(logging.DEBUG):
        motor.promote_pending_drafts(chat_callable=_Recorder(_judge(action="update", target="E1")))
    joined = "\n".join(r.getMessage() for r in caplog.records)
    for secret in (_OLD, _RAW, _JUDGED, "sevilla", "Madrid"):
        assert secret.lower() not in joined.lower()
    assert "update=1" in joined  # counts are logged
