"""Tests for GET /api/memoria/row/{row_id} (WU-H, operator viewing decision).

USER DECISION (binding, 2026-07-05): memoria list shows title + dates only;
CONTENT loads on-demand, per row, only when the operator explicitly requests
it (never preloaded alongside the list). This is the single-row content
endpoint that backs that click. R8 (raw viewer chat never leaves the API) is
unaffected — memoria content is Kira's curated/derived memory, not raw chat.

Mirrors test_api_memoria_mutations.py's fixture shape (tmp sqlite + monkeypatched
MEMORIAS_DB/MEMORIAS_ENABLED, shared MemoriaStore singleton reset) and the
ownership-guard pattern from POST /api/memoria/delete and /update: a
store.get(raising=True) pre-check maps a wrong-profile row to the SAME 404 as
a missing row (R7: no cross-profile existence oracle).
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


@pytest.fixture(autouse=True)
def _reset_memoria_store():
    import opencohost.api.main as main_mod

    main_mod._memoria_store = None
    yield
    main_mod._memoria_store = None


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
        "status, pinned, private, inactive, created_at, updated_at) VALUES "
        "('mem_a', 'default', 'k1', 1, 'title a', 'content a', 'draft', 1, 0, 0, "
        "'2026-01-01T00:00:00+00:00', '2026-01-02T00:00:00+00:00')"
    )
    conn.commit()
    conn.close()


def _enable(monkeypatch, db_path, enabled=True):
    import opencohost.api.main as main_mod

    monkeypatch.setattr(main_mod, "MEMORIAS_ENABLED", enabled)
    monkeypatch.setattr(main_mod, "MEMORIAS_DB", str(db_path))


def test_get_memoria_row_happy_path(tmp_path, monkeypatch):
    db_path = tmp_path / "memorias.db"
    _seed_memorias_db(db_path)
    _enable(monkeypatch, db_path)

    with TestClient(_app()) as client:
        resp = client.get("/api/memoria/row/mem_a", params={"profile_id": "default"})
        assert resp.status_code == 200
        body = resp.json()
        assert body == {
            "id": "mem_a",
            "title": "title a",
            "content": "content a",
            "created_at": "2026-01-01T00:00:00+00:00",
            "updated_at": "2026-01-02T00:00:00+00:00",
            "pinned": True,
            "private": False,
            "inactive": False,
            # memoria_draft_visibility_20260725: seeded status='draft' above.
            "draft": True,
            # memory_promotion_20260725: machine-judged rows are labelled
            # honestly instead of borrowing the operator's `curated`.
            "promoted": False,
        }


def test_get_memoria_row_reports_a_promoted_row_as_promoted_not_draft(tmp_path, monkeypatch):
    db_path = tmp_path / "memorias.db"
    _seed_memorias_db(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE memorias SET status = 'promoted' WHERE id = 'mem_a'")
    _enable(monkeypatch, db_path)

    with TestClient(_app()) as client:
        body = client.get("/api/memoria/row/mem_a", params={"profile_id": "default"}).json()

    assert body["promoted"] is True
    assert body["draft"] is False


def test_get_memoria_row_wrong_profile_id_404(tmp_path, monkeypatch):
    db_path = tmp_path / "memorias.db"
    _seed_memorias_db(db_path)
    _enable(monkeypatch, db_path)

    with TestClient(_app()) as client:
        resp = client.get("/api/memoria/row/mem_a", params={"profile_id": "other"})
        assert resp.status_code == 404
        assert resp.json() == {"detail": "memoria not found"}
        # No content leak on the 404 path.
        assert "content a" not in resp.text


def test_get_memoria_row_missing_id_404(tmp_path, monkeypatch):
    db_path = tmp_path / "memorias.db"
    _seed_memorias_db(db_path)
    _enable(monkeypatch, db_path)

    with TestClient(_app()) as client:
        resp = client.get("/api/memoria/row/nope", params={"profile_id": "default"})
        assert resp.status_code == 404
        # Identical body to the wrong-profile case (R7: no existence oracle).
        assert resp.json() == {"detail": "memoria not found"}


def test_get_memoria_row_disabled_returns_404(tmp_path, monkeypatch):
    db_path = tmp_path / "memorias.db"
    _seed_memorias_db(db_path)
    _enable(monkeypatch, db_path, enabled=False)

    with TestClient(_app()) as client:
        resp = client.get("/api/memoria/row/mem_a", params={"profile_id": "default"})
        assert resp.status_code == 404
        assert resp.json() == {"detail": "memoria not found"}


def test_get_memoria_row_store_unavailable_503(tmp_path, monkeypatch):
    import opencohost.api.main as main_mod

    db_path = tmp_path / "memorias.db"
    _seed_memorias_db(db_path)
    _enable(monkeypatch, db_path)
    monkeypatch.setattr(main_mod, "_memoria_store_or_none", lambda: None)

    with TestClient(_app()) as client:
        resp = client.get("/api/memoria/row/mem_a", params={"profile_id": "default"})
        assert resp.status_code == 503
        assert resp.json() == {"detail": "memoria_unavailable"}


def test_get_memoria_row_transient_read_error_503(tmp_path, monkeypatch):
    import opencohost.core.memoria_store as store_mod

    db_path = tmp_path / "memorias.db"
    _seed_memorias_db(db_path)
    _enable(monkeypatch, db_path)

    def _raise(*a, **k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(store_mod.MemoriaStore, "get", _raise)

    with TestClient(_app()) as client:
        resp = client.get("/api/memoria/row/mem_a", params={"profile_id": "default"})
        assert resp.status_code == 503
        assert resp.json() == {"detail": "memoria_unavailable"}
