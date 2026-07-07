"""Tests for GET /api/memoria/list and POST /api/memoria/purge (F5, R8-CRITICAL).

Mirrors test_api_reads.py's memoria/stats coverage. WU-H (operator viewing
decision, 2026-07-05): the list projection now ALSO includes `title` — a
deliberate, scoped relaxation of the prior metadata-only rule so the operator
can recognize a row before deciding to load its content. `content` stays
excluded from the list SELECT entirely; it is only readable one row at a time
via GET /api/memoria/row/{id} (see test_api_memoria_row.py). R8 (raw viewer
chat never leaves the API) is untouched — memoria title/content is Kira's
curated/derived memory, not raw chat.
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


def test_memoria_list_shape_includes_title_but_not_content(tmp_path, monkeypatch):
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
                "title",
                "created_at",
                "updated_at",
                "revision",
                "pinned",
                "private",
                "inactive",
            }
        raw = resp.text.lower()
        # WU-H: title is now part of the list contract...
        assert "secret title a" in raw
        assert "secret title b" in raw
        # ...but content stays excluded (still one-row-at-a-time only).
        assert "secret content" not in raw

        by_id = {item["id"]: item for item in body["items"]}
        assert by_id["mem_a"]["pinned"] is True
        assert by_id["mem_a"]["private"] is False
        assert by_id["mem_a"]["title"] == "secret title a"
        assert by_id["mem_b"]["revision"] == 2
        assert by_id["mem_b"]["private"] is True
        assert by_id["mem_b"]["title"] == "secret title b"


def test_memoria_list_includes_inactive_field(tmp_path, monkeypatch):
    import opencohost.api.main as main_mod

    db_path = tmp_path / "memorias.db"
    _seed_memorias_db(db_path)
    # Seed one row with inactive=1 to prove the bool round-trips from disk.
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO memorias (id, profile_id, stable_key, revision, title, content, "
        "pinned, private, inactive, created_at, updated_at) VALUES "
        "('mem_c', 'default', 'k3', 1, 't c', 'c c', 0, 0, 1, "
        "'2026-01-03T00:00:00+00:00', '2026-01-03T00:00:00+00:00')"
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(main_mod, "MEMORIAS_ENABLED", True)
    monkeypatch.setattr(main_mod, "MEMORIAS_DB", str(db_path))

    app = _app()
    with TestClient(app) as client:
        resp = client.get("/api/memoria/list", params={"profile_id": "default"})
        assert resp.status_code == 200
        by_id = {item["id"]: item for item in resp.json()["items"]}
        assert "inactive" in by_id["mem_a"]
        assert by_id["mem_a"]["inactive"] is False
        assert by_id["mem_c"]["inactive"] is True


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
