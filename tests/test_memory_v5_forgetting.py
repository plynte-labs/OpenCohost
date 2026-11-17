"""S1 (release-050-stabilization): forgetting reaches the v5 episodic layer.

Before this, ``POST /api/memoria/purge`` and ``/api/memoria/delete`` removed rows
only from ``memorias.db`` while every owner turn and Kira reply also sat verbatim
in the v5 ``evidence_journal`` (and its derived episodes and embeddings), so
"forget" did not forget. Also pins the bounded evidence retention TTL.
Synthetic data only.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from opencohost.config import settings
from opencohost.core.memory.memoria_store import derive_stable_key
from opencohost.core.memory_v5_shadow.subsystem import MemorySubsystem
from tests.test_api_memoria_mutations import (
    _app,
    _enable,
    _reset_host_active,  # noqa: F401 (autouse fixture)
    _reset_memoria_store,  # noqa: F401 (autouse fixture)
)
from tests.test_memory_v5_write_path import FakeWorker, _snap, temp_v5  # noqa: F401 (fixture)

PID = "prof-forget"
OTHER = "prof-keep"
CAT = "mi gato Pixel odia la aspiradora cuando suena de noche"
CAT_REPLY = "Zephyr cohete inventado que Kira jamas vivio"
GARDEN = "cultivo tomates cherry en el balcon durante el verano"


def _table(shadow, sql, *args):
    con = sqlite3.connect(str(shadow))
    try:
        return con.execute(sql, args).fetchall()
    finally:
        con.close()


def _sub_with_exchanges(temp_v5, exchanges):
    """A live ACTIVE subsystem holding *exchanges* [(pid, user, asst, ts)]."""
    sub = MemorySubsystem.from_settings(mode="ACTIVE")
    assert sub.status["effective"] == "ACTIVE"
    rt = sub.runtime
    for pid, user, asst, ts in exchanges:
        sub.record_turn_exchange(
            _snap(pid, rt.run_id, "user", user, ts),
            _snap(pid, rt.run_id, "assistant", asst, ts),
        )
    rt._queue.join()
    return sub


def _seed_memorias(db_path, rows):
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE memorias (
            id TEXT PRIMARY KEY, profile_id TEXT NOT NULL, stable_key TEXT NOT NULL,
            revision INTEGER NOT NULL DEFAULT 1, title TEXT NOT NULL, content TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'draft', pinned INTEGER NOT NULL DEFAULT 0,
            private INTEGER NOT NULL DEFAULT 0, inactive INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        )
        """
    )
    for mid, pid, key in rows:
        conn.execute(
            "INSERT INTO memorias (id, profile_id, stable_key, title, content, created_at, updated_at) "
            "VALUES (?,?,?,?,?,'2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00')",
            (mid, pid, key, "t", "c"),
        )
    conn.commit()
    conn.close()


@pytest.fixture
def client_with_v5(tmp_path, monkeypatch, temp_v5):
    shadow, _cache = temp_v5
    db = tmp_path / "memorias.db"
    cat_key = derive_stable_key(PID, CAT)
    garden_key = derive_stable_key(PID, GARDEN)
    assert cat_key and garden_key
    _seed_memorias(
        db,
        [
            ("mem_cat", PID, cat_key),
            ("mem_garden", PID, garden_key),
            ("mem_other", OTHER, derive_stable_key(OTHER, CAT)),
        ],
    )
    _enable(monkeypatch, db)
    sub = _sub_with_exchanges(
        temp_v5,
        [
            (PID, CAT, CAT_REPLY, "2026-11-01T10:00:00Z"),
            (PID, GARDEN, "claro, tomates", "2026-11-01T10:01:00Z"),
            (OTHER, CAT, "tambien", "2026-11-01T10:02:00Z"),
        ],
    )
    with TestClient(_app()) as client:
        client.app.state.host.motor._memory = sub
        yield client, shadow, sub
    sub.shutdown(timeout_s=2.0)


# --- full purge -------------------------------------------------------------


def test_memoria_purge_also_purges_the_v5_profile(client_with_v5):
    client, shadow, sub = client_with_v5
    assert _table(shadow, "SELECT COUNT(*) FROM evidence_journal WHERE profile_id=?", PID)[0][0] == 4
    resp = client.post("/api/memoria/purge", json={"profile_id": PID})
    assert resp.status_code == 200
    assert resp.json()["deleted"] == 2
    assert _table(shadow, "SELECT COUNT(*) FROM evidence_journal WHERE profile_id=?", PID)[0][0] == 0
    assert _table(shadow, "SELECT COUNT(*) FROM sessions WHERE profile_id=?", PID)[0][0] == 0


def test_memoria_purge_leaves_other_profiles_in_v5(client_with_v5):
    client, shadow, _sub = client_with_v5
    client.post("/api/memoria/purge", json={"profile_id": PID})
    assert _table(shadow, "SELECT COUNT(*) FROM evidence_journal WHERE profile_id=?", OTHER)[0][0] == 2


def test_memoria_purge_reports_v5_outcome(client_with_v5):
    client, _shadow, _sub = client_with_v5
    assert client.post("/api/memoria/purge", json={"profile_id": PID}).json()["v5_purged"] is True


def test_memoria_purge_without_a_v5_subsystem_still_purges_memorias(client_with_v5):
    client, shadow, _sub = client_with_v5
    client.app.state.host.motor._memory = None
    resp = client.post("/api/memoria/purge", json={"profile_id": PID})
    assert resp.json()["deleted"] == 2
    assert resp.json()["v5_purged"] is None


def test_v5_purge_failure_is_reported_not_raised(client_with_v5):
    client, _shadow, sub = client_with_v5
    sub.purge_profile = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    resp = client.post("/api/memoria/purge", json={"profile_id": PID})
    assert resp.status_code == 200
    assert resp.json()["v5_purged"] is False


def test_memoria_purge_with_memorias_disabled_still_purges_v5(client_with_v5, monkeypatch, tmp_path):
    client, shadow, _sub = client_with_v5
    _enable(monkeypatch, tmp_path / "memorias.db", enabled=False)
    resp = client.post("/api/memoria/purge", json={"profile_id": PID})
    assert resp.json()["deleted"] == 0
    assert _table(shadow, "SELECT COUNT(*) FROM evidence_journal WHERE profile_id=?", PID)[0][0] == 0


# --- single-row delete ------------------------------------------------------


def test_row_delete_removes_the_matching_v5_exchange_only(client_with_v5):
    client, shadow, _sub = client_with_v5
    resp = client.post("/api/memoria/delete", json={"profile_id": PID, "id": "mem_cat"})
    assert resp.status_code == 200 and resp.json() == {"ok": True}
    contents = [r[0] for r in _table(shadow, "SELECT content FROM evidence_journal WHERE profile_id=?", PID)]
    assert CAT not in contents and CAT_REPLY not in contents  # user turn AND its Kira reply
    assert GARDEN in contents  # the other row's evidence stays
    # Another profile that said the same words keeps its own evidence.
    assert CAT in [r[0] for r in _table(shadow, "SELECT content FROM evidence_journal WHERE profile_id=?", OTHER)]


def test_row_delete_drops_embeddings_of_the_affected_episodes(client_with_v5, temp_v5):
    client, shadow, sub = client_with_v5
    cache = sub.cache_store
    removed = []
    orig = cache.delete_episode_cache
    cache.delete_episode_cache = lambda eid: (removed.append(eid), orig(eid))[1]
    # Materialize episodes first so the delete has something derived to clean.
    MemorySubsystem.startup_episode_recovery(shadow)
    client.post("/api/memoria/delete", json={"profile_id": PID, "id": "mem_cat"})
    assert removed, "affected episode embeddings must be dropped"


def test_row_delete_with_no_v5_link_changes_nothing_in_v5(client_with_v5, tmp_path):
    client, shadow, _sub = client_with_v5
    conn = sqlite3.connect(tmp_path / "memorias.db")
    conn.execute(
        "INSERT INTO memorias (id, profile_id, stable_key, title, content, created_at, updated_at) "
        "VALUES ('mem_import', ?, 'imported|nothing', 't', 'c', 'x', 'x')",
        (PID,),
    )
    conn.commit()
    conn.close()
    before = _table(shadow, "SELECT COUNT(*) FROM evidence_journal")[0][0]
    assert client.post("/api/memoria/delete", json={"profile_id": PID, "id": "mem_import"}).status_code == 200
    assert _table(shadow, "SELECT COUNT(*) FROM evidence_journal")[0][0] == before


def test_row_delete_survives_a_v5_failure(client_with_v5):
    client, _shadow, sub = client_with_v5
    sub.forget_user_turns = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    resp = client.post("/api/memoria/delete", json={"profile_id": PID, "id": "mem_cat"})
    assert resp.status_code == 200 and resp.json() == {"ok": True}


# --- retention TTL ----------------------------------------------------------

NOW = datetime(2026, 11, 9, tzinfo=timezone.utc)


def test_retention_deletes_only_evidence_older_than_the_ttl(temp_v5):
    shadow, _ = temp_v5
    sub = _sub_with_exchanges(
        temp_v5,
        [
            (PID, "recuerdo viejo del verano pasado", "ok", "2026-05-01T10:00:00Z"),
            (PID, "recuerdo reciente de esta semana", "ok", "2026-11-05T10:00:00Z"),
        ],
    )
    try:
        out = sub.run_retention(ttl_days=90, now=NOW)
        assert out["events"] == 2
        left = [r[0] for r in _table(shadow, "SELECT content FROM evidence_journal ORDER BY stream_sequence")]
        assert left == ["recuerdo reciente de esta semana", "ok"]
    finally:
        sub.shutdown(timeout_s=2.0)


def test_retention_records_the_watermark_in_retention_state(temp_v5):
    shadow, _ = temp_v5
    sub = _sub_with_exchanges(temp_v5, [(PID, "dato antiguo de prueba", "ok", "2026-01-01T10:00:00Z")])
    try:
        sub.run_retention(ttl_days=90, now=NOW)
        rows = _table(shadow, "SELECT profile_id, last_purge_deleted_evidence, last_purge_reason FROM retention_state")
        assert rows == [(PID, 2, "TTL")]
    finally:
        sub.shutdown(timeout_s=2.0)


def test_retention_zero_ttl_keeps_everything(temp_v5):
    shadow, _ = temp_v5
    sub = _sub_with_exchanges(temp_v5, [(PID, "dato antiguo de prueba", "ok", "2020-01-01T10:00:00Z")])
    try:
        assert sub.run_retention(ttl_days=0, now=NOW)["events"] == 0
        assert _table(shadow, "SELECT COUNT(*) FROM evidence_journal")[0][0] == 2
    finally:
        sub.shutdown(timeout_s=2.0)


def test_retention_is_bounded_per_run(temp_v5):
    shadow, _ = temp_v5
    sub = _sub_with_exchanges(
        temp_v5, [(PID, f"dato viejo numero {i} de prueba", "ok", "2026-01-01T10:00:00Z") for i in range(5)]
    )
    try:
        out = sub.run_retention(ttl_days=90, now=NOW, batch_limit=4)
        assert out["events"] == 4
        assert _table(shadow, "SELECT COUNT(*) FROM evidence_journal")[0][0] == 6
        assert sub.run_retention(ttl_days=90, now=NOW, batch_limit=4)["events"] == 4
    finally:
        sub.shutdown(timeout_s=2.0)


def test_retention_removes_closed_old_episodes_and_their_embeddings(temp_v5):
    shadow, _ = temp_v5
    sub = _sub_with_exchanges(temp_v5, [(PID, "charla vieja del puerto y el faro", "si", "2026-01-01T10:00:00Z")])
    sub.shutdown(timeout_s=5.0)  # closes the session and segments episodes
    sub2 = MemorySubsystem.from_settings(mode="ACTIVE")
    try:
        assert _table(shadow, "SELECT COUNT(*) FROM episodes")[0][0] >= 1
        removed = []
        orig = sub2.cache_store.delete_episode_cache
        sub2.cache_store.delete_episode_cache = lambda eid: (removed.append(eid), orig(eid))[1]
        sub2.run_retention(ttl_days=90, now=NOW)
        assert _table(shadow, "SELECT COUNT(*) FROM episodes")[0][0] == 0
        assert removed
    finally:
        sub2.shutdown(timeout_s=2.0)


def test_retention_failure_is_swallowed(temp_v5):
    sub = _sub_with_exchanges(temp_v5, [(PID, "dato antiguo de prueba", "ok", "2026-01-01T10:00:00Z")])
    try:
        sub.runtime._store.purge_older_than = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x"))
        assert sub.run_retention(ttl_days=90, now=NOW) == {"events": 0, "episodes": 0, "error": "RuntimeError"}
    finally:
        sub.shutdown(timeout_s=2.0)


# --- setting ----------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [(None, 90), ("", 90), ("30", 30), (" 365 ", 365), ("0", 0), ("-5", 90), ("abc", 90), ("1.5", 90)],
)
def test_ttl_setting_resolver(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv("OPENCOHOST_MEMORY_V5_EVIDENCE_TTL_DAYS", raising=False)
    else:
        monkeypatch.setenv("OPENCOHOST_MEMORY_V5_EVIDENCE_TTL_DAYS", raw)
    assert settings._resolve_memory_v5_evidence_ttl_days() == expected


def test_ttl_setting_default_is_90_days():
    assert settings.MEMORY_V5_EVIDENCE_TTL_DAYS == 90


# --- scheduling -------------------------------------------------------------


class _FakeMem:
    def __init__(self):
        self.calls = 0

    def run_retention(self, **_kw):
        self.calls += 1
        return {"events": 0, "episodes": 0}


def _tick_motor(monkeypatch, mem):
    import queue
    import opencohost.core.llm_engine as llm_engine

    clock = {"now": 1000.0}
    monkeypatch.setattr(llm_engine, "_retention_monotonic", lambda: clock["now"], raising=False)
    motor = llm_engine.MotorVocalIA(queue.Queue(), lambda e: None)
    motor._memory = mem
    return motor, clock


def test_retention_tick_runs_once_then_waits_the_interval(monkeypatch):
    mem = _FakeMem()
    motor, clock = _tick_motor(monkeypatch, mem)
    motor._memory_retention_tick()
    motor._memory_retention_tick()
    assert mem.calls == 1
    clock["now"] += 6 * 3600 + 1
    motor._memory_retention_tick()
    assert mem.calls == 2


def test_retention_tick_without_a_v5_subsystem_is_a_noop(monkeypatch):
    motor, _clock = _tick_motor(monkeypatch, None)
    motor._memory_retention_tick()  # must not raise


def test_retention_tick_swallows_failures(monkeypatch):
    class Boom(_FakeMem):
        def run_retention(self, **_kw):
            raise RuntimeError("x")

    motor, _clock = _tick_motor(monkeypatch, Boom())
    motor._memory_retention_tick()
