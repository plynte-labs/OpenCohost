"""Tests for GET /api/memoria/list and POST /api/memoria/purge (F5, R8-CRITICAL).

Mirrors test_api_reads.py's memoria/stats coverage: the list endpoint must
expose ONLY metadata (id/created_at/updated_at/revision/pinned/private) and
never leak the memoria's title/content, since the SELECT never reads those
columns off disk in the first place.
"""

import sqlite3

import pytest
from fastapi.testclient import TestClient

from tests.test_api_phase1 import FakeHost

_DEFAULT_TEST_ORIGINS = ["http://localhost:5173"]


@pytest.fixture(autouse=True)
def _reset_host_active():
    import opencohost.api.main as main_mod

    main_mod._host_active = False
    yield
    main_mod._host_active = False


def _app():
    import opencohost.api.main as main_mod

    return main_mod.create_app(host_factory=FakeHost, cors_origins=_DEFAULT_TEST_ORIGINS)


def _seed_memorias_db(db_path):
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE memorias (
            id TEXT PRIMARY KEY,
            profile_id TEXT NOT NULL,
            stable_key TEXT NOT NULL,
            revision INTEGER NOT NULL DEFAULT 1,
            title TEXT NOT NULL,
            content TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'draft',
            pinned INTEGER NOT NULL DEFAULT 0,
            private INTEGER NOT NULL DEFAULT 0,
            inactive INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "INSERT INTO memorias (id, profile_id, stable_key, revision, title, content, "
        "pinned, private, created_at, updated_at) VALUES "
        "('mem_a', 'default', 'k1', 1, 'secret title a', 'secret content a', 1, 0, "
        "'2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')"
    )
    conn.execute(
        "INSERT INTO memorias (id, profile_id, stable_key, revision, title, content, "
        "pinned, private, created_at, updated_at) VALUES "
        "('mem_b', 'default', 'k2', 2, 'secret title b', 'secret content b', 0, 1, "
        "'2026-01-02T00:00:00+00:00', '2026-01-02T00:00:00+00:00')"
    )
    conn.commit()
    conn.close()


def test_memoria_list_shape_and_no_text_leak(tmp_path, monkeypatch):
    import opencohost.api.main as main_mod

    db_path = tmp_path / "memorias.db"
    _seed_memorias_db(db_path)

    monkeypatch.setattr(main_mod, "MEMORIAS_ENABLED", True)
    monkeypatch.setattr(main_mod, "MEMORIAS_DB", str(db_path))

    app = _app()
    with TestClient(app) as client:
        resp = client.get("/api/memoria/list", params={"profile_id": "default"})
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["items"]) == 2
        for item in body["items"]:
            assert set(item.keys()) == {
                "id",
                "created_at",
                "updated_at",
                "revision",
                "pinned",
                "private",
            }
        raw = resp.text.lower()
        assert "content" not in raw
        assert "text" not in raw
        assert "secret title" not in raw
        assert "secret content" not in raw

        by_id = {item["id"]: item for item in body["items"]}
        assert by_id["mem_a"]["pinned"] is True
        assert by_id["mem_a"]["private"] is False
        assert by_id["mem_b"]["revision"] == 2
        assert by_id["mem_b"]["private"] is True


def test_memoria_list_disabled_returns_empty(tmp_path, monkeypatch):
    import opencohost.api.main as main_mod

    db_path = tmp_path / "memorias.db"
    _seed_memorias_db(db_path)

    monkeypatch.setattr(main_mod, "MEMORIAS_ENABLED", False)
    monkeypatch.setattr(main_mod, "MEMORIAS_DB", str(db_path))

    app = _app()
    with TestClient(app) as client:
        resp = client.get("/api/memoria/list", params={"profile_id": "default"})
        assert resp.status_code == 200
        assert resp.json()["items"] == []


def test_memoria_purge_deletes_rows_then_list_is_empty(tmp_path, monkeypatch):
    import opencohost.api.main as main_mod

    db_path = tmp_path / "memorias.db"
    _seed_memorias_db(db_path)

    monkeypatch.setattr(main_mod, "MEMORIAS_ENABLED", True)
    monkeypatch.setattr(main_mod, "MEMORIAS_DB", str(db_path))

    app = _app()
    with TestClient(app) as client:
        resp = client.post("/api/memoria/purge", json={"profile_id": "default"})
        assert resp.status_code == 200
        assert resp.json()["deleted"] == 2

        resp = client.get("/api/memoria/list", params={"profile_id": "default"})
        assert resp.json()["items"] == []


def test_memoria_purge_disabled_returns_zero(tmp_path, monkeypatch):
    import opencohost.api.main as main_mod

    db_path = tmp_path / "memorias.db"
    _seed_memorias_db(db_path)

    monkeypatch.setattr(main_mod, "MEMORIAS_ENABLED", False)
    monkeypatch.setattr(main_mod, "MEMORIAS_DB", str(db_path))

    app = _app()
    with TestClient(app) as client:
        resp = client.post("/api/memoria/purge", json={"profile_id": "default"})
        assert resp.status_code == 200
        assert resp.json()["deleted"] == 0
