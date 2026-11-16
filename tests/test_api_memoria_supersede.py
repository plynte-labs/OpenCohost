"""F3 (memory-paraphrase-contradiction-20260928): superseded_by exposure and
restore through the existing POST /api/memoria/flags un-hide path."""

import sqlite3

import pytest
from fastapi.testclient import TestClient

from tests.test_api_phase1 import FakeHost

_ORIGINS = ["http://localhost:5173"]


@pytest.fixture(autouse=True)
def _reset_globals():
    import opencohost.api.main as main_mod

    main_mod._host_active = False
    main_mod._memoria_store = None
    yield
    main_mod._host_active = False
    main_mod._memoria_store = None


def _app():
    import opencohost.api.main as main_mod

    return main_mod.create_app(host_factory=FakeHost, cors_origins=_ORIGINS)


def _seed(db_path):
    """Build a current-schema db through the store, then add two rows: old
    (superseded) and new (its replacement)."""
    from opencohost.core.memory.memoria_store import MemoriaStore

    MemoriaStore(str(db_path))
    conn = sqlite3.connect(db_path)
    for rid, profile, inactive, sup, key in (
        ("mem_old", "default", 1, "mem_new", "k1"),
        ("mem_new", "default", 0, None, "k2"),
        ("mem_other", "other", 1, "mem_x", "k3"),
    ):
        conn.execute(
            "INSERT INTO memorias (id, profile_id, stable_key, revision, title, content, "
            "status, pinned, private, inactive, superseded_by, created_at, updated_at) "
            "VALUES (?, ?, ?, 1, 't', 'c', 'promoted', 0, 0, ?, ?, "
            "'2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')",
            (rid, profile, key, inactive, sup),
        )
    conn.commit()
    conn.close()


def _row(db_path, rid):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute("SELECT * FROM memorias WHERE id = ?", (rid,)).fetchone()
    finally:
        conn.close()


def _enable(monkeypatch, db_path):
    import opencohost.api.main as main_mod

    monkeypatch.setattr(main_mod, "MEMORIAS_ENABLED", True)
    monkeypatch.setattr(main_mod, "MEMORIAS_DB", str(db_path))


def test_list_exposes_superseded_by(tmp_path, monkeypatch):
    db = tmp_path / "m.db"
    _seed(db)
    _enable(monkeypatch, db)
    with TestClient(_app()) as client:
        items = client.get("/api/memoria/list", params={"profile_id": "default"}).json()["items"]
    by_id = {i["id"]: i for i in items}
    assert by_id["mem_old"]["superseded_by"] == "mem_new"
    assert by_id["mem_new"]["superseded_by"] is None


def test_row_exposes_superseded_by(tmp_path, monkeypatch):
    db = tmp_path / "m.db"
    _seed(db)
    _enable(monkeypatch, db)
    with TestClient(_app()) as client:
        old = client.get("/api/memoria/row/mem_old", params={"profile_id": "default"}).json()
        new = client.get("/api/memoria/row/mem_new", params={"profile_id": "default"}).json()
    assert old["superseded_by"] == "mem_new"
    assert new["superseded_by"] is None


def test_restore_clears_inactive_and_link_keeps_newer_row_and_status(tmp_path, monkeypatch):
    db = tmp_path / "m.db"
    _seed(db)
    _enable(monkeypatch, db)
    with TestClient(_app()) as client:
        resp = client.post(
            "/api/memoria/flags",
            json={"profile_id": "default", "id": "mem_old", "inactive": False},
        )
    assert resp.status_code == 200
    old = _row(db, "mem_old")
    assert old["inactive"] == 0
    assert old["superseded_by"] is None
    assert old["status"] == "promoted"  # never curated by a restore
    new = _row(db, "mem_new")
    assert new["inactive"] == 0  # the newer row is NOT deactivated


def test_muting_does_not_set_a_supersede_link(tmp_path, monkeypatch):
    db = tmp_path / "m.db"
    _seed(db)
    _enable(monkeypatch, db)
    with TestClient(_app()) as client:
        client.post(
            "/api/memoria/flags",
            json={"profile_id": "default", "id": "mem_new", "inactive": True},
        )
    assert _row(db, "mem_new")["superseded_by"] is None


def test_restore_is_profile_scoped(tmp_path, monkeypatch):
    db = tmp_path / "m.db"
    _seed(db)
    _enable(monkeypatch, db)
    with TestClient(_app()) as client:
        resp = client.post(
            "/api/memoria/flags",
            json={"profile_id": "default", "id": "mem_other", "inactive": False},
        )
    assert resp.status_code == 404
    other = _row(db, "mem_other")
    assert other["inactive"] == 1 and other["superseded_by"] == "mem_x"
