"""Tests for cascading profile deletion, physical SQLite sanitization, and orphan GC."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from opencohost.core.memory.memoria_store import MemoriaStore
from opencohost.core.memory.promotion_backoff_store import PromotionBackoffStore
from opencohost.core.memory.purge_coordinator import gc_orphan_profiles, purge_profile_cascade
from opencohost.core.memory_v5_shadow.evidence import CommittedTurnSnapshot
from opencohost.core.memory_v5_shadow.runtime import MemoryRuntime
from opencohost.core.memory_v5_shadow.semantic_cache import (
    ExchangeEmbeddingRecord,
    FactEmbeddingRecord,
    SemanticCacheStore,
)
from opencohost.core.memory_v5_shadow.subsystem import MemorySubsystem
from tests.test_api_phase1 import FakeHost


_TEST_ORIGINS = ["http://localhost:5173"]


@pytest.fixture(autouse=True)
def _reset_host_active():
    import opencohost.api.main as main_mod

    main_mod._host_active = False
    yield
    main_mod._host_active = False


def _app():
    import opencohost.api.main as main_mod

    return main_mod.create_app(host_factory=FakeHost, cors_origins=_TEST_ORIGINS)


def _seed_memoria_row(
    store: MemoriaStore,
    *,
    profile_id: str,
    text: str,
) -> str:
    row_id = store.upsert_draft(
        profile_id=profile_id,
        stable_key=f"{profile_id}|key_{profile_id}_{len(text)}",
        title=f"Title {text}",
        content=text,
        signature=f"sig {text}",
    )
    assert row_id
    return str(row_id)


def _seed_promotion_attempt(db_path: Path, memoria_id: str) -> None:
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            """
            INSERT INTO memoria_promotion_attempts (
                memoria_id, draft_revision, attempt_count,
                last_attempt_at_s, next_attempt_at_s, last_failure_code, promotion_state
            ) VALUES (?, 1, 1, 100, 400, 'invalid_decision', 'pending')
            """,
            (memoria_id,),
        )
        conn.commit()


def _seed_backups(memorias_db: Path) -> tuple[Path, Path]:
    backups_dir = memorias_db.parent / "backups"
    backups_dir.mkdir(parents=True, exist_ok=True)
    bak_db = backups_dir / "memorias-20261017T081127Z.db"
    bak_db.write_bytes(b"SQLite format 3\x00backup-residue")
    manifest = backups_dir / "memoria_cleanup_manifest.json"
    manifest.write_text('{"status": "old"}', encoding="utf-8")
    return bak_db, manifest


def test_delete_perfil_cascades_memorias_backoff_backups_and_vacuums(tmp_path, monkeypatch):
    import opencohost.api.main as main_mod

    profiles_path = tmp_path / "perfiles.json"
    profiles_data = {
        "default": {"id": "uuid-default", "prompt": "Base prompt", "use_system": False},
        "ephemeral": {"id": "uuid-eph", "prompt": "Secret prompt", "use_system": False},
    }
    profiles_path.write_text(json.dumps(profiles_data), encoding="utf-8")

    def fake_cargar():
        return json.loads(profiles_path.read_text(encoding="utf-8"))

    def fake_guardar(data):
        profiles_path.write_text(json.dumps(data), encoding="utf-8")
        return True

    db_path = tmp_path / "memorias" / "memorias.db"
    store = MemoriaStore(db_path)
    keep_id = _seed_memoria_row(store, profile_id="uuid-default", text="ACTIVE_KEEP_TOKEN_111")
    eph_uuid_row = _seed_memoria_row(
        store, profile_id="uuid-eph", text="SECRET_EPH_UUID_TOKEN_98765"
    )
    _seed_memoria_row(store, profile_id="ephemeral", text="SECRET_EPH_LEGACY_TOKEN_54321")
    _seed_promotion_attempt(db_path, eph_uuid_row)

    backoff = PromotionBackoffStore(db_path.with_name("memoria_promotion_backoff.db"))
    backoff.record_failure("uuid-default", "model_unavailable", 100)
    backoff.record_failure("uuid-eph", "model_unavailable", 100)
    backoff.record_failure("ephemeral", "model_unavailable", 100)

    bak_db, manifest = _seed_backups(db_path)

    monkeypatch.setattr(main_mod, "MEMORIAS_ENABLED", True)
    monkeypatch.setattr(main_mod, "MEMORIAS_DB", str(db_path))
    monkeypatch.setattr(main_mod, "cargar_perfiles", fake_cargar)
    monkeypatch.setattr("opencohost.api.routers.perfiles.guardar_perfiles", fake_guardar)

    with TestClient(_app()) as client:
        resp = client.delete("/api/perfiles/ephemeral")
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}

    assert store.list_for_profile("uuid-eph") == []
    assert store.list_for_profile("ephemeral") == []
    assert [r["id"] for r in store.list_for_profile("uuid-default")] == [keep_id]

    with sqlite3.connect(str(db_path)) as conn:
        attempts_left = conn.execute("SELECT COUNT(*) FROM memoria_promotion_attempts").fetchone()[0]
    assert attempts_left == 0

    raw_mem_bytes = db_path.read_bytes()
    assert b"SECRET_EPH_UUID_TOKEN_98765" not in raw_mem_bytes
    assert b"SECRET_EPH_LEGACY_TOKEN_54321" not in raw_mem_bytes

    assert backoff.get_state("uuid-default") is not None
    assert backoff.get_state("uuid-eph") is None
    assert backoff.get_state("ephemeral") is None
    raw_backoff_bytes = db_path.with_name("memoria_promotion_backoff.db").read_bytes()
    assert b"uuid-eph" not in raw_backoff_bytes

    assert not bak_db.exists()
    assert not manifest.exists()


def test_delete_perfil_purges_memorias_even_when_feature_flag_disabled(tmp_path, monkeypatch):
    import opencohost.api.main as main_mod

    profiles_path = tmp_path / "perfiles.json"
    profiles_path.write_text(
        json.dumps(
            {
                "default": {"id": "uuid-default", "prompt": "p", "use_system": False},
                "ephemeral": {"id": "uuid-eph", "prompt": "p", "use_system": False},
            }
        ),
        encoding="utf-8",
    )

    db_path = tmp_path / "memorias" / "memorias.db"
    store = MemoriaStore(db_path)
    _seed_memoria_row(store, profile_id="uuid-eph", text="SECRET_WHEN_DISABLED_777")

    monkeypatch.setattr(main_mod, "MEMORIAS_ENABLED", False)
    monkeypatch.setattr(main_mod, "MEMORIAS_DB", str(db_path))
    monkeypatch.setattr(main_mod, "cargar_perfiles", lambda: json.loads(profiles_path.read_text()))
    monkeypatch.setattr(
        "opencohost.api.routers.perfiles.guardar_perfiles",
        lambda d: profiles_path.write_text(json.dumps(d)) is not None,
    )

    with TestClient(_app()) as client:
        resp = client.delete("/api/perfiles/ephemeral")
        assert resp.status_code == 200

    assert store.list_for_profile("uuid-eph") == []
    assert b"SECRET_WHEN_DISABLED_777" not in db_path.read_bytes()


def test_post_memoria_purge_cleans_uuid_legacy_backoff_backups_and_vacuums(tmp_path, monkeypatch):
    import opencohost.api.main as main_mod

    profiles_data = {
        "Akira": {"id": "uuid-akira", "prompt": "p", "use_system": False},
    }
    db_path = tmp_path / "memorias" / "memorias.db"
    store = MemoriaStore(db_path)
    _seed_memoria_row(store, profile_id="uuid-akira", text="SECRET_AKIRA_UUID_123")
    _seed_memoria_row(store, profile_id="Akira", text="SECRET_AKIRA_LEGACY_456")

    backoff = PromotionBackoffStore(db_path.with_name("memoria_promotion_backoff.db"))
    backoff.record_failure("uuid-akira", "model_unavailable", 100)
    backoff.record_failure("Akira", "model_unavailable", 100)
    bak_db, manifest = _seed_backups(db_path)

    monkeypatch.setattr(main_mod, "MEMORIAS_ENABLED", True)
    monkeypatch.setattr(main_mod, "MEMORIAS_DB", str(db_path))
    monkeypatch.setattr(main_mod, "cargar_perfiles", lambda: profiles_data)

    with TestClient(_app()) as client:
        resp = client.post("/api/memoria/purge", json={"profile_id": "uuid-akira"})
        assert resp.status_code == 200
        assert resp.json()["deleted"] == 2

    assert store.list_for_profile("uuid-akira") == []
    assert store.list_for_profile("Akira") == []
    assert backoff.get_state("uuid-akira") is None
    assert backoff.get_state("Akira") is None
    assert not bak_db.exists()
    assert not manifest.exists()
    raw_bytes = db_path.read_bytes()
    assert b"SECRET_AKIRA_UUID_123" not in raw_bytes
    assert b"SECRET_AKIRA_LEGACY_456" not in raw_bytes


def test_v5_shadow_and_semantic_cache_physical_purge_and_fence_drop(tmp_path):
    v5_db = tmp_path / "v5" / "memory_v5_shadow.db"
    cache_db = tmp_path / "v5" / "memory_v5_semantic_cache.db"

    cache_store = SemanticCacheStore(cache_db)
    cache_store.initialize()
    runtime = MemoryRuntime(v5_db, semantic_cache=cache_store)
    sub = MemorySubsystem(mode="ACTIVE", runtime=runtime, cache_store=cache_store)

    try:
        runtime.on_profile_switch(None, "uuid-eph")
        runtime.record_turn_exchange(
            CommittedTurnSnapshot(
                committed_turn_id="",
                profile_id="uuid-eph",
                run_id="",
                stream_sequence=0,
                role="user",
                source="ptt",
                occurred_at="2026-11-20T10:00:01Z",
                content="SECRET_V5_TURN_TEXT_443322",
                is_private=False,
            ),
            CommittedTurnSnapshot(
                committed_turn_id="",
                profile_id="uuid-eph",
                run_id="",
                stream_sequence=0,
                role="assistant",
                source="ptt",
                occurred_at="2026-11-20T10:00:02Z",
                content="SECRET_V5_REPLY_TEXT_998877",
                is_private=False,
            ),
        )
        # Barrier sync ensures turns above are committed before cache seeding + purge
        assert sub.purge_profile("__sync_barrier__") is True
        sub.drop_profile_fence("__sync_barrier__")

        cache_store.insert_exchange_embedding(
            ExchangeEmbeddingRecord(
                exchange_key="ex-1",
                profile_id="uuid-eph",
                session_id="sess-eph",
                episode_id="ep-1",
                content_hash="abc",
                model_id="minilm",
                model_version="v1",
                dimensions=4,
                vector=[0.5, 0.5, 0.5, 0.5],
                created_at="2026-11-20T10:00:03Z",
            )
        )
        cache_store.upsert_fact_embedding(
            FactEmbeddingRecord(
                fact_id="mem-1",
                profile_id="uuid-eph",
                revision=1,
                content_hash="def",
                model_id="minilm",
                model_version="v1",
                vector=[0.5, 0.5, 0.5, 0.5],
                created_at="2026-11-20T10:00:04Z",
            )
        )

        assert "uuid-eph" in sub.list_distinct_profile_ids()

        res = purge_profile_cascade(
            profile_id="uuid-eph",
            legacy_name="ephemeral",
            memorias_db=None,
            v5_subsystem=sub,
            drop_v5_privacy_fence=True,
        )
        assert res["v5_purged"] is True
        assert "uuid-eph" not in sub.list_distinct_profile_ids()

        with sqlite3.connect(str(cache_db)) as cconn:
            fences = cconn.execute(
                "SELECT COUNT(*) FROM profile_privacy_fences WHERE profile_id = 'uuid-eph'"
            ).fetchone()[0]
        assert fences == 0

        raw_v5 = v5_db.read_bytes()
        wal_v5 = Path(str(v5_db) + "-wal")
        wal_bytes = wal_v5.read_bytes() if wal_v5.is_file() else b""
        assert b"SECRET_V5_TURN_TEXT_443322" not in raw_v5 + wal_bytes
        assert b"SECRET_V5_REPLY_TEXT_998877" not in raw_v5 + wal_bytes
    finally:
        sub.shutdown(timeout_s=2.0)


def test_gc_orphan_profiles_sweeps_all_stores_and_preserves_active(tmp_path):
    db_path = tmp_path / "memorias" / "memorias.db"
    store = MemoriaStore(db_path)
    _seed_memoria_row(store, profile_id="uuid-active", text="ACTIVE_MEMORY_SAFE")
    _seed_memoria_row(store, profile_id="uuid-orphan", text="ORPHAN_SECRET_XYZ")
    _seed_memoria_row(store, profile_id="legacy-orphan", text="ORPHAN_LEGACY_ZYX")

    backoff = PromotionBackoffStore(db_path.with_name("memoria_promotion_backoff.db"))
    backoff.record_failure("uuid-active", "model_unavailable", 100)
    backoff.record_failure("uuid-orphan", "model_unavailable", 100)
    bak_db, manifest = _seed_backups(db_path)

    v5_db = tmp_path / "v5" / "memory_v5_shadow.db"
    cache_db = tmp_path / "v5" / "memory_v5_semantic_cache.db"
    cache_store = SemanticCacheStore(cache_db)
    cache_store.initialize()
    runtime = MemoryRuntime(v5_db, semantic_cache=cache_store)
    sub = MemorySubsystem(mode="ACTIVE", runtime=runtime, cache_store=cache_store)

    try:
        runtime.on_profile_switch(None, "uuid-active")
        runtime.record_turn(
            CommittedTurnSnapshot(
                committed_turn_id="",
                profile_id="uuid-active",
                run_id="",
                stream_sequence=0,
                role="user",
                source="ptt",
                occurred_at="2026-11-20T10:00:01Z",
                content="ACTIVE_V5_CONTENT_SAFE",
                is_private=False,
            )
        )
        runtime.on_profile_switch("uuid-active", "uuid-orphan")
        runtime.record_turn(
            CommittedTurnSnapshot(
                committed_turn_id="",
                profile_id="uuid-orphan",
                run_id="",
                stream_sequence=0,
                role="user",
                source="ptt",
                occurred_at="2026-11-20T10:01:01Z",
                content="ORPHAN_V5_SECRET_LEAK",
                is_private=False,
            )
        )
        assert sub.purge_profile("__sync_barrier__") is True
        sub.drop_profile_fence("__sync_barrier__")

        # Fail-safe: empty active_profiles must NOT wipe anything
        empty_res = gc_orphan_profiles(
            active_profiles={},
            memorias_db=str(db_path),
            v5_subsystem=sub,
        )
        assert empty_res == {"memorias_deleted": 0, "backoff_deleted": 0, "v5_profiles_purged": 0}
        assert len(store.list_for_profile("uuid-active")) == 1

        res = gc_orphan_profiles(
            active_profiles={
                "ActiveName": {"id": "uuid-active", "prompt": "p", "use_system": False},
            },
            memorias_db=str(db_path),
            v5_subsystem=sub,
        )
        assert res["memorias_deleted"] == 2
        assert res["backoff_deleted"] == 1
        assert res["v5_profiles_purged"] >= 1

        assert len(store.list_for_profile("uuid-active")) == 1
        assert store.list_for_profile("uuid-orphan") == []
        assert store.list_for_profile("legacy-orphan") == []
        assert backoff.get_state("uuid-active") is not None
        assert backoff.get_state("uuid-orphan") is None
        assert not bak_db.exists()
        assert not manifest.exists()
        assert "uuid-orphan" not in sub.list_distinct_profile_ids()
        assert "uuid-active" in sub.list_distinct_profile_ids()

        assert b"ORPHAN_SECRET_XYZ" not in db_path.read_bytes()
        assert b"ORPHAN_LEGACY_ZYX" not in db_path.read_bytes()
        wal_v5 = Path(str(v5_db) + "-wal")
        wal_bytes = wal_v5.read_bytes() if wal_v5.is_file() else b""
        assert b"ORPHAN_V5_SECRET_LEAK" not in v5_db.read_bytes() + wal_bytes
    finally:
        sub.shutdown(timeout_s=2.0)


def test_engine_host_stop_clears_session_topics_view(tmp_path, monkeypatch):
    from unittest.mock import MagicMock
    from opencohost.api.engine_host import EngineHost

    host = EngineHost(lock_path=str(tmp_path / "host.lock"))
    host.motor = MagicMock()
    host.motor.is_processing = False
    host.motor._speech_active = False
    host.motor._memory = None
    host.motor.current_model = None

    host.stop(unload_models=False)
    host.motor.reset_session_topics.assert_called_once()
