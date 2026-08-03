"""Migrate-on-read re-key for legacy name-keyed memorias (GET /api/memoria/list).

Root cause: memorias were historically saved keyed by the profile's display
NAME. The FE now lists by the profile's stable UUID (`id`) — frontend commit
da108a0 in OpenCohost_UI — so legacy name-keyed rows no longer match the UUID
query and the operator sees "No hay memorias guardadas" for profiles that DO
have (legacy) memories. New memorias already save under the UUID correctly.

Fix: on the first UUID list for a profile, the endpoint resolves the profile's
current display NAME (via cargar_perfiles) and re-keys its legacy rows
(profile_id = display name) to the UUID — an additive column re-key, zero row
deletions, one bounded transaction. Idempotent afterwards, scoped to the
requested profile's name so another profile's rows are never swallowed.
"""

import logging
import sqlite3

import pytest
from fastapi.testclient import TestClient

from tests.test_api_phase1 import FakeHost

_DEFAULT_TEST_ORIGINS = ["http://localhost:5173"]

_AKIRA_ID = "11111111-1111-4111-8111-111111111111"
_BRUNO_ID = "22222222-2222-4222-8222-222222222222"

_PERFILES = {
    "Akira": {"id": _AKIRA_ID, "prompt": "p", "use_system": False},
    "Bruno": {"id": _BRUNO_ID, "prompt": "p", "use_system": False},
}


@pytest.fixture(autouse=True)
def _reset_host_active():
    import opencohost.api.main as main_mod

    main_mod._host_active = False
    yield
    main_mod._host_active = False


def _app():
    import opencohost.api.main as main_mod

    return main_mod.create_app(host_factory=FakeHost, cors_origins=_DEFAULT_TEST_ORIGINS)


def _seed(db_path):
    """Real schema (UNIQUE(profile_id, stable_key)) + legacy name-keyed rows
    and a UUID-keyed row, mirroring the production save history."""
    from opencohost.core.memory.memoria_store import MemoriaStore

    MemoriaStore(db_path)  # creates the real schema, index, and PRAGMA v1

    conn = sqlite3.connect(db_path)

    def _ins(row_id, profile_id, stable_key, title):
        conn.execute(
            "INSERT INTO memorias (id, profile_id, stable_key, revision, title, "
            "content, status, pinned, private, inactive, created_at, updated_at) "
            "VALUES (?, ?, ?, 1, ?, 'content', 'draft', 0, 0, 0, "
            "'2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')",
            (row_id, profile_id, stable_key, title),
        )

    # Legacy rows keyed by display NAME (the pre-da108a0 save key).
    _ins("mem_legacy_akira", "Akira", "Akira|alpha-beta-gamma", "legacy akira")
    _ins("mem_legacy_bruno", "Bruno", "Bruno|delta-epsilon-zeta", "legacy bruno")
    # A row already correctly keyed by Akira's UUID (post-da108a0 save), whose
    # token part collides with a legacy row's below — proves profile_id-only
    # re-key never triggers a UNIQUE(profile_id, stable_key) rollback.
    _ins("mem_uuid_akira", _AKIRA_ID, _AKIRA_ID + "|shared-token-part", "uuid akira")
    _ins("mem_legacy_dup", "Akira", "Akira|shared-token-part", "legacy dup")
    conn.commit()
    conn.close()


def _prime(monkeypatch, db_path):
    import opencohost.api.main as main_mod

    monkeypatch.setattr(main_mod, "MEMORIAS_ENABLED", True)
    monkeypatch.setattr(main_mod, "MEMORIAS_DB", str(db_path))
    monkeypatch.setattr(main_mod, "cargar_perfiles", lambda: dict(_PERFILES))


def _profile_id_of(db_path, row_id):
    conn = sqlite3.connect(db_path)
    try:
        r = conn.execute(
            "SELECT profile_id FROM memorias WHERE id = ?", (row_id,)
        ).fetchone()
        return r[0] if r else None
    finally:
        conn.close()


def test_legacy_name_rows_appear_in_uuid_list_and_are_rekeyed(tmp_path, monkeypatch):
    """(a) Legacy name-keyed rows show up in the UUID list, and the profile_id
    column is actually re-keyed on disk (migration, not just a read-time merge)."""
    db_path = tmp_path / "memorias.db"
    _seed(db_path)
    _prime(monkeypatch, db_path)

    app = _app()
    with TestClient(app) as client:
        resp = client.get("/api/memoria/list", params={"profile_id": _AKIRA_ID})

    assert resp.status_code == 200
    ids = {item["id"] for item in resp.json()["items"]}
    # Both legacy Akira rows are now visible alongside the UUID-keyed one; the
    # colliding token part did not roll back the migration.
    assert ids == {"mem_legacy_akira", "mem_legacy_dup", "mem_uuid_akira"}
    # The column was actually re-keyed on disk.
    assert _profile_id_of(db_path, "mem_legacy_akira") == _AKIRA_ID
    assert _profile_id_of(db_path, "mem_legacy_dup") == _AKIRA_ID


def test_second_read_is_idempotent(tmp_path, monkeypatch):
    """(b) Once re-keyed, a second list finds nothing left under the name."""
    db_path = tmp_path / "memorias.db"
    _seed(db_path)
    _prime(monkeypatch, db_path)

    app = _app()
    with TestClient(app) as client:
        first = client.get("/api/memoria/list", params={"profile_id": _AKIRA_ID}).json()
        second = client.get("/api/memoria/list", params={"profile_id": _AKIRA_ID}).json()

    assert {i["id"] for i in first["items"]} == {i["id"] for i in second["items"]}
    conn = sqlite3.connect(db_path)
    try:
        remaining = conn.execute(
            "SELECT COUNT(*) FROM memorias WHERE profile_id = ?", ("Akira",)
        ).fetchone()[0]
    finally:
        conn.close()
    assert remaining == 0


def test_uuid_keyed_rows_unaffected(tmp_path, monkeypatch):
    """(c) A row already keyed by the UUID keeps its profile_id and stable_key."""
    db_path = tmp_path / "memorias.db"
    _seed(db_path)
    _prime(monkeypatch, db_path)

    app = _app()
    with TestClient(app) as client:
        client.get("/api/memoria/list", params={"profile_id": _AKIRA_ID})

    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT profile_id, stable_key FROM memorias WHERE id = ?",
            ("mem_uuid_akira",),
        ).fetchone()
    finally:
        conn.close()
    assert row[0] == _AKIRA_ID
    assert row[1] == _AKIRA_ID + "|shared-token-part"


def test_other_profiles_name_rows_are_not_swallowed(tmp_path, monkeypatch):
    """(d) Listing Akira must not surface or re-key Bruno's name-keyed rows."""
    db_path = tmp_path / "memorias.db"
    _seed(db_path)
    _prime(monkeypatch, db_path)

    app = _app()
    with TestClient(app) as client:
        resp = client.get("/api/memoria/list", params={"profile_id": _AKIRA_ID})

    ids = {item["id"] for item in resp.json()["items"]}
    assert "mem_legacy_bruno" not in ids
    assert _profile_id_of(db_path, "mem_legacy_bruno") == "Bruno"


def test_rekey_with_rows_logs_info_line(tmp_path, monkeypatch, caplog):
    """Rekeying rows for a profile must log an INFO line with the row count
    and the profile_id -- the only observability the migrate-on-read path
    had none of before."""
    db_path = tmp_path / "memorias.db"
    _seed(db_path)
    _prime(monkeypatch, db_path)

    app = _app()
    with caplog.at_level(logging.INFO, logger="opencohost.api.main"):
        with TestClient(app) as client:
            resp = client.get("/api/memoria/list", params={"profile_id": _AKIRA_ID})

    assert resp.status_code == 200
    rekey_records = [r for r in caplog.records if "rekeyed" in r.message]
    assert len(rekey_records) == 1
    assert "2" in rekey_records[0].message
    assert _AKIRA_ID in rekey_records[0].message


def test_rekey_with_zero_rows_logs_nothing(tmp_path, monkeypatch, caplog):
    """An unknown profile_id never resolves a legacy key, so no rekey happens
    and nothing is logged."""
    db_path = tmp_path / "memorias.db"
    _seed(db_path)
    _prime(monkeypatch, db_path)

    app = _app()
    with caplog.at_level(logging.INFO, logger="opencohost.api.main"):
        with TestClient(app) as client:
            resp = client.get(
                "/api/memoria/list", params={"profile_id": "99999999-0000-4000-8000-000000000000"}
            )

    assert resp.status_code == 200
    rekey_records = [r for r in caplog.records if "rekeyed" in r.message]
    assert rekey_records == []


def test_unknown_profile_id_is_a_noop(tmp_path, monkeypatch):
    """A UUID with no matching profile name (deleted/renamed profile) re-keys
    nothing and simply lists the UUID rows — never crashes."""
    db_path = tmp_path / "memorias.db"
    _seed(db_path)
    _prime(monkeypatch, db_path)

    app = _app()
    with TestClient(app) as client:
        resp = client.get(
            "/api/memoria/list", params={"profile_id": "99999999-0000-4000-8000-000000000000"}
        )

    assert resp.status_code == 200
    assert resp.json()["items"] == []
    # Legacy rows are untouched (still under their names).
    assert _profile_id_of(db_path, "mem_legacy_akira") == "Akira"
