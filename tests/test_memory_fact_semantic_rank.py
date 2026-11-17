"""T3 memory-semantic-recall: semantic ranking over the fact notebook.

Contract (ADR-060, extends ADR-059):
- promoted / curated / imported / summary `memorias` rows are embedded into
  the v5 semantic cache (`fact_embeddings`), keyed by row id + revision +
  content hash; drafts, private and inactive rows are never embedded and
  never ranked;
- embedding happens in a background reconcile (never on the turn); at query
  time only the query is embedded, inside the remaining recall budget;
- automatic top-k becomes a hybrid rank: semantic score with lexical
  corroboration; pinned rows are unchanged;
- timeout / cold cache / error fall back to today's lexical top-k
  byte-identically and say so in the `memory_recall:` line
  (`semantic=N ... fact_fallback=<code>`), numbers and codes only.

Fixture text uses distinctive `zq*` tokens; the embedder is a deterministic
fake keyed on those markers. Temp DBs only.
"""

from __future__ import annotations

import logging
import math
import queue
import re
import sqlite3
import threading
import time
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

import opencohost.core.llm_engine as llm_engine
from opencohost.config import settings
from opencohost.core.memory.memoria_store import (
    MemoriaStore,
    build_injection_lines,
    select_top_k,
    select_top_k_hybrid,
)
from opencohost.core.memory_v5_shadow.episodic_recall import (
    EpisodicRecallPacket,
    RecallMode,
)
from opencohost.core.memory_v5_shadow.fact_index import FactSemanticIndex
from opencohost.core.memory_v5_shadow.query_analyzer import RecallScope
from opencohost.core.memory_v5_shadow.semantic_cache import SemanticCacheStore
from opencohost.core.memory_v5_shadow.subsystem import MemorySubsystem
from opencohost.i18n import active as i18n_active

PROFILE = "profile-1"

# A paraphrase: no significant token shared with FACT_PET.
QUERY_PET = "zqquery como se llama tu mascota peluda"
FACT_PET = "zqpetfact el streamer tiene un perro que se llama firulais"
FACT_CAR = "zqcarfact el streamer maneja un auto rojo viejo"
FACT_PIN = "zqpinfact el streamer usa un telescopio dobsoniano"

_TELEMETRY_RE = re.compile(
    r"^memory_recall: lexical=\d+ semantic=\d+ episodes=\d+ decision=[A-Z_]+ "
    r"scope=[A-Z_]+ top_score=(?:\d+\.\d{3}|none) ms=\d+ "
    r"fallback=(?:none|timeout|error|disabled|no_hit|no_room) "
    r"fact_fallback=(?:none|disabled|cold|timeout|error|skipped) over_budget=[01]$"
)


@pytest.fixture(autouse=True)
def _pin_es_locale():
    from opencohost.i18n.startup import resolve_active_bundle

    i18n_active.set_active_bundle(resolve_active_bundle(locale="es"))
    yield
    i18n_active.reset_active_bundle()


# ---------------------------------------------------------------------------
# Deterministic fake embedder
# ---------------------------------------------------------------------------

_AXES = {
    "zqquery": (1.0, 0.0, 0.0, 0.0),
    "zqpetfact": (0.95, 0.31, 0.0, 0.0),
    "zqcarfact": (0.0, 1.0, 0.0, 0.0),
    "zqpinfact": (0.9, 0.0, 0.43, 0.0),
    "zqdraft": (1.0, 0.0, 0.0, 0.0),
    "zqprivate": (1.0, 0.0, 0.0, 0.0),
    "zqinactive": (1.0, 0.0, 0.0, 0.0),
    "zqlong": (0.97, 0.2, 0.0, 0.0),
    "zqweak": (0.6, 0.8, 0.0, 0.0),
}


def _vec(text):
    acc = [0.0, 0.0, 0.0, 0.0]
    for marker, axis in _AXES.items():
        if marker in text:
            acc = [a + b for a, b in zip(acc, axis)]
    if not any(acc):
        acc = [0.0, 0.0, 0.0, 1.0]
    norm = math.sqrt(sum(x * x for x in acc))
    return [x / norm for x in acc]


class FakeWorker:
    def __init__(self, query_result="ok"):
        self.batches = []
        self.queries = []
        self.query_result = query_result

    def embed_query(self, text, timeout_s=0.5):
        self.queries.append(timeout_s)
        if self.query_result == "timeout":
            return None
        if self.query_result == "error":
            raise RuntimeError("zqboom")
        return _vec(text)

    def embed_batch(self, texts, timeout_s=10.0):
        self.batches.append(list(texts))
        return [_vec(t) for t in texts]

    @property
    def embedded_texts(self):
        return [t for batch in self.batches for t in batch]


class FakeCoordinator:
    """Episodic side always misses: these tests are about facts."""

    def process_query(self, query_text, profile_id, reference_time=None, embed_timeout_s=None):
        return EpisodicRecallPacket(
            query_hash="h", profile_id=profile_id, mode=RecallMode.ACTIVE,
            retrieved_episodes=[], formatted_block="", token_estimate=0,
            reason_code="NO_HISTORICAL_ANCHOR", scope=RecallScope.EPISODIC_TOPIC,
            candidate_count=0, top_score=None,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TS = datetime(2026, 1, 1, tzinfo=timezone.utc).isoformat()


def _seed(db_path, rows):
    MemoriaStore(db_path)
    with sqlite3.connect(str(db_path)) as conn:
        conn.executemany(
            "INSERT INTO memorias (id, profile_id, stable_key, revision, title, "
            "content, status, pinned, private, inactive, created_at, updated_at, signature) "
            "VALUES (?, ?, ?, 1, 'titulo', ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (r["id"], PROFILE, f"{PROFILE}|{r['id']}", r["content"],
                 r.get("status", "promoted"), 1 if r.get("pinned") else 0,
                 1 if r.get("private") else 0, 1 if r.get("inactive") else 0,
                 _TS, _TS, r.get("signature", ""))
                for r in rows
            ],
        )


def _rows_from(db_path):
    return MemoriaStore(db_path).list_injection_candidates(PROFILE)


def _all_rows(db_path):
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute("SELECT * FROM memorias WHERE profile_id = ?", (PROFILE,)).fetchall()
    finally:
        conn.close()


def _make_index(tmp_path, worker=None, scheduled=None):
    cache = SemanticCacheStore(tmp_path / "cache.db")
    cache.initialize()
    worker = worker or FakeWorker()
    sched = (lambda fn: scheduled.append(fn)) if scheduled is not None else (lambda fn: None)
    return FactSemanticIndex(cache, worker, scheduler=sched), cache, worker


def _make_motor(monkeypatch, tmp_path, rows):
    monkeypatch.setattr(llm_engine, "MEMORIAS_ENABLED", True)
    tmp_path.mkdir(parents=True, exist_ok=True)
    db = tmp_path / "memorias.db"
    monkeypatch.setattr(llm_engine, "MEMORIAS_DB", str(db))
    _seed(db, rows)
    motor = llm_engine.MotorVocalIA(queue.Queue(), lambda e: None)
    motor.ollama = MagicMock()
    motor.pygame = MagicMock()
    motor._current_profile_id = PROFILE
    motor._memory = None
    return motor, db


def _prompt(motor, query=QUERY_PET):
    setup = motor._build_generation_request(
        query, source="direct", is_local=True, provider_cfg={},
        request_model="m", watchdog_timeout=10.0,
    )
    return setup.messages[-1]["content"]


def _block_lines(prompt):
    open_tag = i18n_active.memorias_block_open()
    close_tag = i18n_active.memorias_block_close()
    if open_tag not in prompt:
        return []
    inner = prompt.split(open_tag + "\n", 1)[1].split("\n" + close_tag, 1)[0]
    return inner.split("\n")


def _recall_records(caplog):
    return [
        r for r in caplog.records
        if r.name == "OpenCohost" and r.getMessage().startswith("memory_recall:")
    ]


def _semantic_motor(monkeypatch, tmp_path, rows, worker=None, reconcile=True, scheduled=None):
    motor, db = _make_motor(monkeypatch, tmp_path, rows)
    index, cache, worker = _make_index(tmp_path, worker=worker, scheduled=scheduled)
    if reconcile:
        index.reconcile(PROFILE, _rows_from(db))
    motor._memory = MemorySubsystem(mode="ACTIVE", coordinator=FakeCoordinator(), fact_index=index)
    return motor, db, index, cache, worker


def _baseline(monkeypatch, tmp_path, rows, query=QUERY_PET):
    motor, _ = _make_motor(monkeypatch, tmp_path / "base", rows)
    return _prompt(motor, query)


# ---------------------------------------------------------------------------
# Ranking: a paraphrase lexical alone misses
# ---------------------------------------------------------------------------

def test_hybrid_finds_paraphrase_that_lexical_misses():
    rows = [
        {"id": "pet", "content": FACT_PET, "title": "perro", "signature": "streamer perro llama firulais", "pinned": 0},
        {"id": "car", "content": FACT_CAR, "title": "auto", "signature": "streamer maneja auto rojo", "pinned": 0},
    ]
    scores = {"pet": 0.95, "car": 0.0}

    assert select_top_k(QUERY_PET, rows) == []
    assert [r["id"] for r in select_top_k_hybrid(QUERY_PET, rows, scores)] == ["pet"]


def test_lexical_corroboration_breaks_near_ties():
    query = "zqquery hablemos de musica synthwave"
    rows = [
        {"id": "plain", "content": "a", "title": "t", "signature": "otra cosa distinta", "pinned": 0},
        {"id": "corro", "content": "b", "title": "t", "signature": "musica synthwave retro", "pinned": 0},
    ]
    scores = {"plain": 0.72, "corro": 0.70}

    ranked = select_top_k_hybrid(query, rows, scores)

    assert [r["id"] for r in ranked] == ["corro", "plain"]


def test_below_threshold_semantic_only_row_is_not_returned():
    rows = [{"id": "weak", "content": "x", "title": "t", "signature": "nada comun aqui", "pinned": 0}]
    below = settings.MEMORY_FACT_SEMANTIC_MIN_SCORE - 0.01

    assert select_top_k_hybrid(QUERY_PET, rows, {"weak": below}) == []


def test_engine_injects_paraphrased_fact_and_counts_it(monkeypatch, tmp_path, caplog):
    rows = [{"id": "pet", "content": FACT_PET}, {"id": "car", "content": FACT_CAR}]
    baseline = _baseline(monkeypatch, tmp_path, rows)
    motor, *_ = _semantic_motor(monkeypatch, tmp_path, rows)

    caplog.clear()
    with caplog.at_level(logging.INFO, logger="OpenCohost"):
        lines = _block_lines(_prompt(motor))

    assert FACT_PET not in baseline
    assert lines == [FACT_PET]
    (rec,) = _recall_records(caplog)
    msg = rec.getMessage()
    assert _TELEMETRY_RE.match(msg), msg
    assert "semantic=1" in msg and "fact_fallback=none" in msg


# ---------------------------------------------------------------------------
# Eligibility: drafts, private and inactive rows
# ---------------------------------------------------------------------------

def test_private_inactive_and_draft_rows_never_embedded_or_returned(monkeypatch, tmp_path):
    rows = [
        {"id": "pet", "content": FACT_PET, "status": "promoted"},
        {"id": "draft", "content": "zqdraft un borrador sin juzgar", "status": "draft"},
        {"id": "priv", "content": "zqprivate un dato privado", "status": "curated", "private": True},
        {"id": "inact", "content": "zqinactive un dato silenciado", "status": "promoted", "inactive": True},
    ]
    motor, db, index, cache, worker = _semantic_motor(monkeypatch, tmp_path, rows)
    # Reconcile over EVERY row (not just the injection candidates): the index
    # must filter by itself.
    index.reconcile(PROFILE, _all_rows(db))

    assert worker.embedded_texts == [FACT_PET]
    assert {r.fact_id for r in cache.get_fact_embeddings_by_profile(PROFILE)} == {"pet"}
    lines = _block_lines(_prompt(motor))
    assert lines == [FACT_PET]


def test_row_turned_private_is_never_scored_and_its_embedding_dropped(monkeypatch, tmp_path):
    rows = [{"id": "pet", "content": FACT_PET}, {"id": "car", "content": FACT_CAR}]
    motor, db, index, cache, _ = _semantic_motor(monkeypatch, tmp_path, rows)
    assert {r.fact_id for r in cache.get_fact_embeddings_by_profile(PROFILE)} == {"pet", "car"}

    MemoriaStore(db).set_flags("pet", private=True)
    # Before any reconcile, the stale vector must already be unreachable.
    assert FACT_PET not in _prompt(motor)

    index.reconcile(PROFILE, _rows_from(db))
    assert {r.fact_id for r in cache.get_fact_embeddings_by_profile(PROFILE)} == {"car"}


def test_statuses_embedded_are_promoted_curated_imported_summary(tmp_path):
    index, cache, worker = _make_index(tmp_path)
    rows = [
        {"id": s, "profile_id": PROFILE, "revision": 1, "status": s, "private": 0,
         "inactive": 0, "pinned": 0, "content": f"zqpetfact {s}"}
        for s in ("promoted", "curated", "imported", "summary", "draft")
    ]

    result = index.reconcile(PROFILE, rows)

    assert result.indexed == 4
    assert {r.fact_id for r in cache.get_fact_embeddings_by_profile(PROFILE)} == {
        "promoted", "curated", "imported", "summary"}


# ---------------------------------------------------------------------------
# Invalidation: revision, content, delete
# ---------------------------------------------------------------------------

def test_revision_change_invalidates_and_reembeds(tmp_path):
    db = tmp_path / "memorias.db"
    _seed(db, [{"id": "pet", "content": FACT_PET}])
    index, cache, worker = _make_index(tmp_path)
    index.reconcile(PROFILE, _rows_from(db))
    assert len(worker.batches) == 1

    with sqlite3.connect(str(db)) as conn:
        conn.execute("UPDATE memorias SET revision = 2 WHERE id = 'pet'")
    rows = _rows_from(db)
    view = index.view(PROFILE, rows, _vec(QUERY_PET))

    assert view.needs_reconcile
    assert "pet" not in view.scores  # a stale vector is never used
    index.reconcile(PROFILE, rows)
    assert len(worker.batches) == 2
    (rec,) = cache.get_fact_embeddings_by_profile(PROFILE)
    assert rec.revision == 2
    assert "pet" in index.view(PROFILE, rows, _vec(QUERY_PET)).scores


def test_content_edit_without_revision_bump_reembeds(tmp_path):
    db = tmp_path / "memorias.db"
    _seed(db, [{"id": "pet", "content": FACT_PET}])
    index, cache, worker = _make_index(tmp_path)
    index.reconcile(PROFILE, _rows_from(db))

    # update_row (operator edit / promotion) rewrites content without
    # bumping revision: the content hash must still invalidate.
    MemoriaStore(db).update_row("pet", content="zqcarfact texto editado por el operador")
    rows = _rows_from(db)

    assert index.view(PROFILE, rows, _vec(QUERY_PET)).needs_reconcile
    index.reconcile(PROFILE, rows)
    assert worker.batches[-1] == ["zqcarfact texto editado por el operador"]


def test_unchanged_rows_are_not_reembedded(tmp_path):
    db = tmp_path / "memorias.db"
    _seed(db, [{"id": "pet", "content": FACT_PET}])
    index, cache, worker = _make_index(tmp_path)
    index.reconcile(PROFILE, _rows_from(db))
    result = index.reconcile(PROFILE, _rows_from(db))

    assert len(worker.batches) == 1
    assert result.indexed == 0 and result.dropped == 0
    assert not index.view(PROFILE, _rows_from(db), None).needs_reconcile


def test_delete_drops_the_embedding(tmp_path):
    db = tmp_path / "memorias.db"
    _seed(db, [{"id": "pet", "content": FACT_PET}, {"id": "car", "content": FACT_CAR}])
    index, cache, _ = _make_index(tmp_path)
    index.reconcile(PROFILE, _rows_from(db))

    MemoriaStore(db).delete_row("pet")
    rows = _rows_from(db)
    assert index.view(PROFILE, rows, None).needs_reconcile
    result = index.reconcile(PROFILE, rows)

    assert result.dropped == 1
    assert {r.fact_id for r in cache.get_fact_embeddings_by_profile(PROFILE)} == {"car"}


def test_profile_purge_and_forget_all_drop_fact_embeddings(tmp_path):
    db = tmp_path / "memorias.db"
    _seed(db, [{"id": "pet", "content": FACT_PET}])
    index, cache, _ = _make_index(tmp_path)
    index.reconcile(PROFILE, _rows_from(db))
    cache.purge_profile_cache(PROFILE)
    assert cache.get_fact_embeddings_by_profile(PROFILE) == []

    index.reconcile(PROFILE, _rows_from(db))
    cache.forget_all_cache()
    assert cache.get_fact_embeddings_by_profile(PROFILE) == []


def test_failed_batch_embeds_nothing_and_reports_failure(tmp_path):
    class DeadWorker(FakeWorker):
        def embed_batch(self, texts, timeout_s=10.0):
            return None

    db = tmp_path / "memorias.db"
    _seed(db, [{"id": "pet", "content": FACT_PET}])
    index, cache, _ = _make_index(tmp_path, worker=DeadWorker())

    result = index.reconcile(PROFILE, _rows_from(db))

    assert result.failed == 1 and result.indexed == 0
    assert cache.get_fact_embeddings_by_profile(PROFILE) == []


# ---------------------------------------------------------------------------
# Off the hot path: the turn only embeds the query
# ---------------------------------------------------------------------------

def test_turn_never_embeds_rows_and_schedules_reconcile_when_cold(monkeypatch, tmp_path):
    rows = [{"id": "pet", "content": FACT_PET}]
    scheduled = []
    motor, db, index, cache, worker = _semantic_motor(
        monkeypatch, tmp_path, rows, reconcile=False, scheduled=scheduled)

    _prompt(motor)

    assert worker.batches == []  # nothing embedded on the turn
    assert len(scheduled) == 1   # background reconcile requested
    scheduled[0]()               # the background job itself
    assert {r.fact_id for r in cache.get_fact_embeddings_by_profile(PROFILE)} == {"pet"}


def test_query_embed_uses_the_remaining_recall_budget(monkeypatch, tmp_path):
    rows = [{"id": "pet", "content": FACT_PET}]
    motor, db, index, cache, worker = _semantic_motor(monkeypatch, tmp_path, rows)

    _prompt(motor)

    (timeout_s,) = worker.queries
    assert 0.0 < timeout_s <= settings.MEMORY_RECALL_BUDGET_MS / 1000.0


def test_default_scheduler_runs_reconcile_in_a_background_thread(tmp_path):
    db = tmp_path / "memorias.db"
    _seed(db, [{"id": "pet", "content": FACT_PET}])
    cache = SemanticCacheStore(tmp_path / "cache.db")
    cache.initialize()
    index = FactSemanticIndex(cache, FakeWorker())

    thread = index.schedule_reconcile(PROFILE, _rows_from(db))

    assert isinstance(thread, threading.Thread)
    thread.join(timeout=5.0)
    assert {r.fact_id for r in cache.get_fact_embeddings_by_profile(PROFILE)} == {"pet"}


# ---------------------------------------------------------------------------
# Fallbacks: byte-identical lexical prompt + logged reason
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "mode,expected",
    [("timeout", "timeout"), ("error", "error"), ("cold", "cold")],
)
def test_fallback_is_byte_identical_lexical_and_logged(monkeypatch, tmp_path, caplog, mode, expected):
    rows = [
        {"id": "pin", "content": FACT_PIN, "pinned": True},
        {"id": "pet", "content": FACT_PET},
        {"id": "lex", "content": "zqlexfact mascota peluda llama",
         "signature": "mascota peluda llama", "status": "draft"},
    ]
    baseline = _baseline(monkeypatch, tmp_path, rows)
    worker = FakeWorker(query_result="ok" if mode == "cold" else mode)
    motor, *_ = _semantic_motor(
        monkeypatch, tmp_path, rows, worker=worker, reconcile=(mode != "cold"), scheduled=[])

    caplog.clear()
    with caplog.at_level(logging.INFO, logger="OpenCohost"):
        prompt = _prompt(motor)

    assert prompt == baseline
    (rec,) = _recall_records(caplog)
    msg = rec.getMessage()
    assert _TELEMETRY_RE.match(msg), msg
    assert f"fact_fallback={expected}" in msg and "semantic=0" in msg


def test_no_fact_index_reports_disabled(monkeypatch, tmp_path, caplog):
    rows = [{"id": "pet", "content": FACT_PET}]
    motor, _ = _make_motor(monkeypatch, tmp_path, rows)
    motor._memory = MemorySubsystem(mode="ACTIVE", coordinator=FakeCoordinator())

    with caplog.at_level(logging.INFO, logger="OpenCohost"):
        _prompt(motor)

    (rec,) = _recall_records(caplog)
    assert "fact_fallback=disabled" in rec.getMessage()


# ---------------------------------------------------------------------------
# Pins, budget, telemetry
# ---------------------------------------------------------------------------

def test_pins_unchanged_and_never_duplicated(monkeypatch, tmp_path):
    rows = [
        {"id": "pin", "content": FACT_PIN, "pinned": True},
        {"id": "pet", "content": FACT_PET},
    ]
    baseline_lines = _block_lines(_baseline(monkeypatch, tmp_path, rows))
    motor, *_ = _semantic_motor(monkeypatch, tmp_path, rows)

    lines = _block_lines(_prompt(motor))

    assert baseline_lines == [FACT_PIN]
    assert lines == [FACT_PIN, FACT_PET]  # pin first, exactly as before


def test_budget_and_k_respected(monkeypatch, tmp_path):
    rows = [{"id": f"long{i}", "content": f"zqlong{i} " + "palabra " * 60} for i in range(6)]
    motor, *_ = _semantic_motor(monkeypatch, tmp_path, rows)

    lines = _block_lines(_prompt(motor))

    assert 1 <= len(lines) <= 3
    assert len("\n".join(lines)) <= settings.MEMORIAS_MAX_INJECT_CHARS


def test_build_injection_lines_without_scores_is_unchanged():
    rows = [
        {"id": "a", "content": "musica synthwave retro", "title": "t",
         "signature": "musica synthwave retro", "pinned": 0, "created_at": _TS},
    ]
    assert build_injection_lines(rows, "hablemos de musica synthwave") == \
        build_injection_lines(rows, "hablemos de musica synthwave", semantic_scores=None)


def test_telemetry_has_no_row_text(monkeypatch, tmp_path, caplog):
    rows = [{"id": "pin", "content": FACT_PIN, "pinned": True},
            {"id": "pet", "content": FACT_PET}, {"id": "car", "content": FACT_CAR}]
    motor, db, index, *_ = _semantic_motor(monkeypatch, tmp_path, rows)

    with caplog.at_level(logging.DEBUG):
        _prompt(motor)
        index.reconcile(PROFILE, _rows_from(db))

    (rec,) = _recall_records(caplog)
    assert _TELEMETRY_RE.match(rec.getMessage())
    for r in caplog.records:
        text = r.getMessage()
        for needle in ("zq", "firulais", "telescopio", "mascota"):
            assert needle not in text, (r.name, needle)


# ---------------------------------------------------------------------------
# Worker: a background batch and a turn's query never cross replies
# ---------------------------------------------------------------------------

def test_worker_serializes_calls_and_busy_caller_fails_open():
    from opencohost.core.memory_v5_shadow.semantic_worker import SemanticWorkerService

    worker = SemanticWorkerService(use_dummy=True, simulated_delay_s=0.4)
    worker.start(timeout=30.0)
    try:
        result = {}
        batch = threading.Thread(
            target=lambda: result.setdefault("batch", worker.embed_batch(["a", "b"], timeout_s=10.0)))
        batch.start()
        time.sleep(0.1)  # the batch holds the worker
        busy = worker.embed_query("q", timeout_s=0.05)
        batch.join(timeout=15.0)

        assert busy is None  # fails open inside its own budget
        assert result["batch"] is not None and len(result["batch"]) == 2
        assert worker.embed_query("q", timeout_s=5.0) is not None
    finally:
        worker.shutdown()
