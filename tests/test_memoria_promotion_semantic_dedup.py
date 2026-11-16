"""Strict-TDD tests for F1 (memory-paraphrase-contradiction-20260928):
per-sweep MiniLM paraphrase dedup wired through the promotion engine.

The store's own dedup mechanics (containment fallback, negation gate,
intra-batch dedup via SQL visibility) are covered by
test_memoria_promotion_dedup.py. This file covers the ENGINE layer: building
one per-sweep ``semantic_similarity_fn`` closure from
- ONE ``embed_batch`` call over this batch's <=4 keeper texts, and
- the profile's cached ``fact_embeddings`` vectors (via a real
  ``FactSemanticIndex`` wired to fake worker/cache-store collaborators, so
  ``isinstance(fact_index, FactSemanticIndex)`` holds exactly like production),

and its fail-open behavior end to end through
``MotorVocalIA.promote_pending_drafts``. No real MiniLM/ONNX model, no IPC --
every embedding here is a synthetic, deterministic vector.

All memory content strings below are synthetic -- never real captured rows.
"""

from __future__ import annotations

import json
import logging
import queue
import sqlite3
from types import SimpleNamespace
from unittest.mock import MagicMock

import opencohost.core.llm_engine as llm_engine
from opencohost.core.llm_engine import MotorVocalIA
from opencohost.core.memory.memoria_store import MemoriaStore, derive_stable_key
from opencohost.core.memory_v5_shadow.fact_index import (
    FactSemanticIndex,
    content_fingerprint,
)
from opencohost.core.memory_v5_shadow.semantic_cache import FactEmbeddingRecord


# ---------------------------------------------------------------------------
# Helpers (deliberately independent copies of test_memoria_promotion.py's --
# test files in this repo do not import each other's private helpers).
# ---------------------------------------------------------------------------

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


def _seed_draft(store, profile_id, key_suffix, content):
    key = derive_stable_key(profile_id, content)
    assert key is not None
    row_id = store.upsert_draft(
        profile_id, key, f"titulo {key_suffix}", content, signature=f"firma {key_suffix}",
    )
    assert isinstance(row_id, str)
    return row_id


def _seed_durable(store, profile_id, content, *, status="promoted", row_id=None, revision=1):
    """Insert a durable row directly (bypassing the judge). *store* must
    already exist (its constructor creates the schema) before this runs."""
    row_id = row_id or f"durable-{status}-{abs(hash(content)) % 100000}"
    with sqlite3.connect(str(store.db_path)) as conn:
        conn.execute(
            "INSERT INTO memorias (id, profile_id, stable_key, revision, title, "
            "content, status, pinned, private, inactive, created_at, updated_at, "
            "signature, judged_at) VALUES (?, ?, ?, ?, 'titulo', ?, ?, 0, 0, 0, "
            "'2026-01-01', '2026-01-01', ?, '2026-01-01')",
            (row_id, profile_id, f"{profile_id}|{row_id}", revision, content, status, content),
        )
    return row_id


class _Recorder:
    """A chat stub: records every kwargs it saw and returns a canned reply."""

    def __init__(self, reply):
        self.calls = []
        self._reply = reply

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        reply = self._reply(**kwargs) if callable(self._reply) else self._reply
        return {"message": {"content": reply}}


def _decisions(*entries) -> str:
    return json.dumps({"decisions": list(entries)})


class _FakeWorker:
    """Stands in for SemanticWorkerService.embed_batch (synchronous, no IPC).

    *vectors_by_text* maps a keeper text to its deterministic embedding;
    *fail* simulates a dead/timed-out/lock-busy worker (embed_batch -> None,
    exactly like the real fail-open contract).
    """

    def __init__(self, vectors_by_text=None, *, fail=False):
        self.vectors_by_text = vectors_by_text or {}
        self.calls: list[list[str]] = []
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


def _fact_index(worker, records=None):
    return FactSemanticIndex(_FakeCacheStore(records), worker)


def _record(fact_id, profile_id, revision, content, vector):
    return FactEmbeddingRecord(
        fact_id=fact_id, profile_id=profile_id, revision=revision,
        content_hash=content_fingerprint(content), model_id="minilm-l12",
        model_version="v1", vector=vector, created_at="2026-01-01T00:00:00Z",
    )


# Deterministic 2-D "embeddings": parallel vectors score cosine == 1.0
# (above _DEDUP_SEMANTIC_THRESHOLD == 0.97), orthogonal vectors score 0.0.
_PARALLEL = [1.0, 0.0]
_PARALLEL_2 = [2.0, 0.0]  # same direction, different magnitude -> cosine 1.0
_ORTHOGONAL = [0.0, 1.0]


# ---------------------------------------------------------------------------
# 1. A semantic paraphrase (lexically DISTINCT) is rejected as a duplicate
# ---------------------------------------------------------------------------

def test_semantic_paraphrase_is_rejected_as_duplicate(monkeypatch, tmp_path):
    motor = _make_motor(monkeypatch, tmp_path)
    store = MemoriaStore(tmp_path / "memorias.db")
    existing = "El operador trabaja como ingeniero de sistemas."
    paraphrase = "Es programador de profesion el operador."  # no shared significant tokens
    durable_id = _seed_durable(store, "profile-1", existing)
    draft_id = _seed_draft(store, "profile-1", "k1", "streamer: crudo profesion detalle extra")

    worker = _FakeWorker({paraphrase: _PARALLEL})
    motor._memory = SimpleNamespace(
        fact_index=_fact_index(worker, [_record(durable_id, "profile-1", 1, existing, _PARALLEL_2)]),
    )
    stub = _Recorder(_decisions({"i": 1, "keep": True, "text": paraphrase}))

    counts = motor.promote_pending_drafts(chat_callable=stub)

    assert counts["kept"] == 0
    assert counts["rejected"] == 1
    assert counts["reasons"]["duplicate"] == 1
    assert _row(tmp_path, draft_id)["inactive"] == 1
    # F2: one pre-judge call (draft texts, related retrieval) + ONE post-judge
    # call (keeper texts, dedup).
    assert len(worker.calls) == 2 and worker.calls[-1] == [paraphrase]


# ---------------------------------------------------------------------------
# 2. Below-threshold semantic score does NOT veto lexical containment (F2:
#    duplicate = semantic >= threshold OR containment)
# ---------------------------------------------------------------------------

def test_below_threshold_semantic_score_does_not_veto_lexical_containment(monkeypatch, tmp_path):
    motor = _make_motor(monkeypatch, tmp_path)
    store = MemoriaStore(tmp_path / "memorias.db")
    superset = "El streamer juega Hollow Knight Silksong todos los martes por la tarde."
    subset = "El streamer juega Hollow Knight Silksong los martes."  # lexically CONTAINED
    durable_id = _seed_durable(store, "profile-1", superset)
    draft_id = _seed_draft(store, "profile-1", "k1", "streamer: crudo silksong detalle extra")

    worker = _FakeWorker({subset: _PARALLEL})
    motor._memory = SimpleNamespace(
        fact_index=_fact_index(worker, [_record(durable_id, "profile-1", 1, superset, _ORTHOGONAL)]),
    )
    stub = _Recorder(_decisions({"i": 1, "keep": True, "text": subset}))

    counts = motor.promote_pending_drafts(chat_callable=stub)

    assert counts["kept"] == 0
    assert counts["reasons"]["duplicate"] == 1
    assert _row(tmp_path, draft_id)["inactive"] == 1


# ---------------------------------------------------------------------------
# 3. Worker absent/timed out (embed_batch -> None) falls back to lexical and
#    the sweep completes
# ---------------------------------------------------------------------------

def test_worker_returning_none_falls_back_to_lexical_and_sweep_completes(monkeypatch, tmp_path):
    motor = _make_motor(monkeypatch, tmp_path)
    store = MemoriaStore(tmp_path / "memorias.db")
    superset = "El streamer juega Hollow Knight Silksong todos los martes por la tarde."
    subset = "El streamer juega Hollow Knight Silksong los martes."
    durable_id = _seed_durable(store, "profile-1", superset)
    draft_id = _seed_draft(store, "profile-1", "k1", "streamer: crudo silksong detalle sin obrero")

    worker = _FakeWorker(fail=True)  # simulates dead/timed-out/lock-busy
    motor._memory = SimpleNamespace(
        fact_index=_fact_index(worker, [_record(durable_id, "profile-1", 1, superset, _PARALLEL)]),
    )
    stub = _Recorder(_decisions({"i": 1, "keep": True, "text": subset}))

    counts = motor.promote_pending_drafts(chat_callable=stub)

    # Lexical containment still fires: subset is contained in superset.
    assert counts["kept"] == 0
    assert counts["reasons"]["duplicate"] == 1
    assert counts["skipped"] == ""  # the sweep itself was never gated/aborted


def test_missing_memory_attribute_falls_back_to_lexical_and_sweep_completes(monkeypatch, tmp_path):
    """No ``_memory`` at all (v5 OFF) must behave exactly like the
    pre-F1 lexical-only sweep."""
    motor = _make_motor(monkeypatch, tmp_path)
    store = MemoriaStore(tmp_path / "memorias.db")
    superset = "El streamer juega Hollow Knight Silksong todos los martes por la tarde."
    subset = "El streamer juega Hollow Knight Silksong los martes."
    _seed_durable(store, "profile-1", superset)
    draft_id = _seed_draft(store, "profile-1", "k1", "streamer: crudo silksong detalle sin memoria")
    assert not hasattr(motor, "_memory") or motor._memory is None

    stub = _Recorder(_decisions({"i": 1, "keep": True, "text": subset}))
    counts = motor.promote_pending_drafts(chat_callable=stub)

    assert counts["kept"] == 0
    assert counts["reasons"]["duplicate"] == 1
    assert counts["skipped"] == ""


# ---------------------------------------------------------------------------
# 4. A durable row with NO cached vector falls back to lexical for THAT pair
#    only (the worker itself is healthy and embeds the keeper fine)
# ---------------------------------------------------------------------------

def test_missing_cached_vector_for_existing_row_falls_back_to_lexical_for_that_pair(monkeypatch, tmp_path):
    motor = _make_motor(monkeypatch, tmp_path)
    store = MemoriaStore(tmp_path / "memorias.db")
    superset = "El streamer juega Hollow Knight Silksong todos los martes por la tarde."
    subset = "El streamer juega Hollow Knight Silksong los martes."
    _seed_durable(store, "profile-1", superset)  # no FactEmbeddingRecord for it
    draft_id = _seed_draft(store, "profile-1", "k1", "streamer: crudo silksong detalle sin cache")

    worker = _FakeWorker({subset: _PARALLEL})  # keeper embeds fine
    motor._memory = SimpleNamespace(fact_index=_fact_index(worker, []))  # empty cache
    stub = _Recorder(_decisions({"i": 1, "keep": True, "text": subset}))

    counts = motor.promote_pending_drafts(chat_callable=stub)

    assert worker.calls[-1] == [subset]  # the keeper embed itself succeeded
    # Lexical containment still catches it since no cached vector exists.
    assert counts["kept"] == 0
    assert counts["reasons"]["duplicate"] == 1


def test_stale_cached_vector_content_hash_mismatch_falls_back_to_lexical(monkeypatch, tmp_path):
    """The existing row's content changed since the vector was cached (a
    stale entry, wrong content_hash) -- same fallback as no entry at all."""
    motor = _make_motor(monkeypatch, tmp_path)
    store = MemoriaStore(tmp_path / "memorias.db")
    superset = "El streamer juega Hollow Knight Silksong todos los martes por la tarde."
    subset = "El streamer juega Hollow Knight Silksong los martes."
    durable_id = _seed_durable(store, "profile-1", superset)  # revision 1
    draft_id = _seed_draft(store, "profile-1", "k1", "streamer: crudo silksong detalle hash viejo")

    worker = _FakeWorker({subset: _PARALLEL})
    # Same fact_id and revision, but the CONTENT the vector was computed for
    # has since changed -- content_hash mismatch is the staleness signal.
    stale_record = _record(durable_id, "profile-1", 1, "texto viejo distinto", _ORTHOGONAL)
    motor._memory = SimpleNamespace(fact_index=_fact_index(worker, [stale_record]))
    stub = _Recorder(_decisions({"i": 1, "keep": True, "text": subset}))

    counts = motor.promote_pending_drafts(chat_callable=stub)

    assert counts["kept"] == 0
    assert counts["reasons"]["duplicate"] == 1  # lexical containment, not the stale vector


def test_stale_cached_vector_revision_or_model_mismatch_falls_back_to_lexical(monkeypatch, tmp_path):
    """Revision and model identity are part of the same freshness contract
    as the content hash (FactSemanticIndex._is_valid)."""
    superset = "El streamer juega Hollow Knight Silksong todos los martes por la tarde."
    subset = "El streamer juega Hollow Knight Silksong los martes."
    for variant in ("revision", "model"):
        base = tmp_path / variant
        base.mkdir()
        motor = _make_motor(monkeypatch, base)
        store = MemoriaStore(base / "memorias.db")
        durable_id = _seed_durable(store, "profile-1", superset, revision=2)
        _seed_draft(store, "profile-1", "k1", f"streamer: crudo silksong detalle {variant}")
        record = _record(durable_id, "profile-1", 1 if variant == "revision" else 2,
                         superset, _ORTHOGONAL)
        if variant == "model":
            record = FactEmbeddingRecord(**{**record.__dict__, "model_version": "v0"})
        worker = _FakeWorker({subset: _PARALLEL})
        motor._memory = SimpleNamespace(fact_index=_fact_index(worker, [record]))
        stub = _Recorder(_decisions({"i": 1, "keep": True, "text": subset}))

        counts = motor.promote_pending_drafts(chat_callable=stub)

        # An orthogonal (score 0.0) vector would KEEP it; the stale vector is
        # ignored, so lexical containment rejects it instead.
        assert counts["kept"] == 0, variant
        assert counts["reasons"]["duplicate"] == 1, variant


def test_worker_raising_falls_back_to_lexical_and_sweep_completes(monkeypatch, tmp_path):
    """A worker that RAISES (not just returns None) is still fail-open."""
    motor = _make_motor(monkeypatch, tmp_path)
    store = MemoriaStore(tmp_path / "memorias.db")
    superset = "El streamer juega Hollow Knight Silksong todos los martes por la tarde."
    subset = "El streamer juega Hollow Knight Silksong los martes."
    _seed_durable(store, "profile-1", superset)
    _seed_draft(store, "profile-1", "k1", "streamer: crudo silksong detalle excepcion")

    class _RaisingWorker:
        calls = 0

        def embed_batch(self, texts, timeout_s=10.0):
            _RaisingWorker.calls += 1
            raise TimeoutError("worker stalled")

    motor._memory = SimpleNamespace(fact_index=_fact_index(_RaisingWorker(), []))
    stub = _Recorder(_decisions({"i": 1, "keep": True, "text": subset}))

    counts = motor.promote_pending_drafts(chat_callable=stub)

    assert _RaisingWorker.calls == 2  # pre-judge related retrieval + post-judge dedup
    assert counts["kept"] == 0
    assert counts["reasons"]["duplicate"] == 1
    assert counts["skipped"] == ""


# ---------------------------------------------------------------------------
# 5. Intra-batch semantic dedup: two keepers whose OWN precomputed vectors
#    (from the same embed_batch call) collapse them, though lexically
#    distinct
# ---------------------------------------------------------------------------

def test_intra_batch_semantic_dedup_collapses_two_lexically_distinct_keepers(monkeypatch, tmp_path):
    motor = _make_motor(monkeypatch, tmp_path)
    store = MemoriaStore(tmp_path / "memorias.db")
    first_id = _seed_draft(store, "profile-1", "k1", "streamer: crudo paraphrase uno extra")
    second_id = _seed_draft(store, "profile-1", "k2", "streamer: crudo paraphrase dos extra")

    text_a = "El operador tiene un gato llamado Nube."
    text_b = "Su mascota felina se llama Nube."  # paraphrase, no shared significant tokens

    worker = _FakeWorker({text_a: _PARALLEL, text_b: _PARALLEL_2})
    motor._memory = SimpleNamespace(fact_index=_fact_index(worker, []))

    batch_rows = store.list_unjudged_drafts("profile-1", limit=8, now_s=100, raising=True)
    order = {row["id"]: i for i, row in enumerate(batch_rows, start=1)}
    stub = _Recorder(_decisions(
        {"i": order[first_id], "keep": True, "text": text_a},
        {"i": order[second_id], "keep": True, "text": text_b},
    ))

    counts = motor.promote_pending_drafts(chat_callable=stub)

    assert counts["kept"] == 1
    assert counts["rejected"] == 1
    assert counts["reasons"]["duplicate"] == 1
    with sqlite3.connect(str(tmp_path / "memorias.db")) as conn:
        promoted_count = conn.execute(
            "SELECT COUNT(*) FROM memorias WHERE profile_id = 'profile-1' AND status = 'promoted'"
        ).fetchone()[0]
    assert promoted_count == 1


# ---------------------------------------------------------------------------
# 6. Reconcile is scheduled (never blocked on) after a sweep that keeps rows
# ---------------------------------------------------------------------------

def test_reconcile_is_scheduled_after_a_kept_row_commits(monkeypatch, tmp_path):
    motor = _make_motor(monkeypatch, tmp_path)
    store = MemoriaStore(tmp_path / "memorias.db")
    _seed_draft(store, "profile-1", "k1", "streamer: crudo reconcile detalle extra")

    worker = _FakeWorker({})
    fact_index = _fact_index(worker, [])
    reconcile_mock = MagicMock(return_value=None)
    fact_index.schedule_reconcile = reconcile_mock
    motor._memory = SimpleNamespace(fact_index=fact_index)
    stub = _Recorder(_decisions(
        {"i": 1, "keep": True, "text": "El streamer juega Silksong los jueves."}
    ))

    counts = motor.promote_pending_drafts(chat_callable=stub)

    assert counts["kept"] == 1
    reconcile_mock.assert_called_once()
    args, kwargs = reconcile_mock.call_args
    assert args[0] == "profile-1"


def test_reconcile_is_not_scheduled_when_nothing_is_kept(monkeypatch, tmp_path):
    motor = _make_motor(monkeypatch, tmp_path)
    store = MemoriaStore(tmp_path / "memorias.db")
    _seed_draft(store, "profile-1", "k1", "streamer: crudo sin keep detalle extra")

    worker = _FakeWorker({})
    fact_index = _fact_index(worker, [])
    reconcile_mock = MagicMock(return_value=None)
    fact_index.schedule_reconcile = reconcile_mock
    motor._memory = SimpleNamespace(fact_index=fact_index)
    stub = _Recorder(_decisions({"i": 1, "keep": False, "reason": "vague"}))

    counts = motor.promote_pending_drafts(chat_callable=stub)

    assert counts["kept"] == 0
    reconcile_mock.assert_not_called()


# ---------------------------------------------------------------------------
# 7. RC-8: no memory text ever reaches the logs, through the full engine path
# ---------------------------------------------------------------------------

def test_no_memory_text_in_logs_through_the_engine_semantic_path(monkeypatch, tmp_path, caplog):
    motor = _make_motor(monkeypatch, tmp_path)
    store = MemoriaStore(tmp_path / "memorias.db")
    existing = "El operador trabaja como ingeniero de sistemas."
    paraphrase = "Es programador de profesion el operador."
    durable_id = _seed_durable(store, "profile-1", existing)
    _seed_draft(store, "profile-1", "k1", "streamer: crudo logging texto detalle extra")

    worker = _FakeWorker({paraphrase: _PARALLEL})
    motor._memory = SimpleNamespace(
        fact_index=_fact_index(worker, [_record(durable_id, "profile-1", 1, existing, _PARALLEL_2)]),
    )
    stub = _Recorder(_decisions({"i": 1, "keep": True, "text": paraphrase}))

    with caplog.at_level(logging.DEBUG):
        counts = motor.promote_pending_drafts(chat_callable=stub)

    assert counts["reasons"]["duplicate"] == 1
    for record in caplog.records:
        message = record.getMessage()
        assert existing not in message
        assert paraphrase not in message
