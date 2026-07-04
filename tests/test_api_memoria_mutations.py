"""Tests for POST /api/memoria/flags (WU1, F5-preserving, R7/R8).

Mirrors test_api_memoria_list.py's fixture shape (tmp sqlite + monkeypatched
MEMORIAS_DB/MEMORIAS_ENABLED). The flags handler routes writes through the
shared MemoriaStore singleton so the F5 unified-freeze invariant (pin/private
promote status='curated' in the same statement; un-pin never demotes) is
enforced in ONE place. These tests assert on the raw sqlite row directly to
prove that passthrough, not just the HTTP status.

The `_memoria_store` module global is a lazy singleton (design D1) that would
otherwise leak a stale tmp-db path between tests — the autouse fixture resets
it before AND after each test.
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
        "('mem_a', 'default', 'k1', 1, 'title a', 'content a', 'draft', 0, 0, 0, "
        "'2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')"
    )
    conn.commit()
    conn.close()


def _read_row(db_path, memoria_id):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            "SELECT * FROM memorias WHERE id = ?", (memoria_id,)
        ).fetchone()
    finally:
        conn.close()


def _enable(monkeypatch, db_path, enabled=True):
    import opencohost.api.main as main_mod

    monkeypatch.setattr(main_mod, "MEMORIAS_ENABLED", enabled)
    monkeypatch.setattr(main_mod, "MEMORIAS_DB", str(db_path))


def test_post_memoria_flags_pin_promotes_to_curated(tmp_path, monkeypatch):
    db_path = tmp_path / "memorias.db"
    _seed_memorias_db(db_path)
    _enable(monkeypatch, db_path)

    with TestClient(_app()) as client:
        resp = client.post(
            "/api/memoria/flags",
            json={"profile_id": "default", "id": "mem_a", "pinned": True},
        )
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}

    row = _read_row(db_path, "mem_a")
    assert row["pinned"] == 1
    assert row["status"] == "curated"


def test_post_memoria_flags_unpin_does_not_demote_status(tmp_path, monkeypatch):
    db_path = tmp_path / "memorias.db"
    _seed_memorias_db(db_path)
    # Freeze it first (direct SQL to the curated state a prior pin would set).
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE memorias SET pinned = 1, status = 'curated' WHERE id = 'mem_a'")
    conn.commit()
    conn.close()
    _enable(monkeypatch, db_path)

    with TestClient(_app()) as client:
        resp = client.post(
            "/api/memoria/flags",
            json={"profile_id": "default", "id": "mem_a", "pinned": False},
        )
        assert resp.status_code == 200

    row = _read_row(db_path, "mem_a")
    assert row["pinned"] == 0
    assert row["status"] == "curated"  # one-way freeze, never demoted


def test_post_memoria_flags_inactive_only_leaves_status_untouched(tmp_path, monkeypatch):
    db_path = tmp_path / "memorias.db"
    _seed_memorias_db(db_path)  # mem_a is status='draft'
    _enable(monkeypatch, db_path)

    with TestClient(_app()) as client:
        resp = client.post(
            "/api/memoria/flags",
            json={"profile_id": "default", "id": "mem_a", "inactive": True},
        )
        assert resp.status_code == 200

    row = _read_row(db_path, "mem_a")
    assert row["inactive"] == 1
    assert row["status"] == "draft"  # inactive is pure visibility, no promotion


def test_post_memoria_flags_no_flags_provided_422(tmp_path, monkeypatch):
    db_path = tmp_path / "memorias.db"
    _seed_memorias_db(db_path)
    _enable(monkeypatch, db_path)

    with TestClient(_app()) as client:
        resp = client.post(
            "/api/memoria/flags",
            json={"profile_id": "default", "id": "mem_a"},
        )
        assert resp.status_code == 422
        assert resp.json() == {"detail": "no flags provided"}


def test_post_memoria_flags_wrong_profile_id_404(tmp_path, monkeypatch):
    db_path = tmp_path / "memorias.db"
    _seed_memorias_db(db_path)
    _enable(monkeypatch, db_path)

    with TestClient(_app()) as client:
        resp = client.post(
            "/api/memoria/flags",
            json={"profile_id": "other", "id": "mem_a", "pinned": True},
        )
        assert resp.status_code == 404
        assert resp.json() == {"detail": "memoria not found"}


def test_post_memoria_flags_missing_id_404(tmp_path, monkeypatch):
    db_path = tmp_path / "memorias.db"
    _seed_memorias_db(db_path)
    _enable(monkeypatch, db_path)

    with TestClient(_app()) as client:
        resp = client.post(
            "/api/memoria/flags",
            json={"profile_id": "default", "id": "nope", "pinned": True},
        )
        assert resp.status_code == 404
        # Identical body to the wrong-profile case (R7: no existence oracle).
        assert resp.json() == {"detail": "memoria not found"}


def test_post_memoria_flags_store_write_failure_503(tmp_path, monkeypatch):
    import opencohost.core.memoria_store as store_mod

    db_path = tmp_path / "memorias.db"
    _seed_memorias_db(db_path)
    _enable(monkeypatch, db_path)
    monkeypatch.setattr(store_mod.MemoriaStore, "set_flags", lambda *a, **k: False)

    with TestClient(_app()) as client:
        resp = client.post(
            "/api/memoria/flags",
            json={"profile_id": "default", "id": "mem_a", "pinned": True},
        )
        assert resp.status_code == 503
        assert resp.json() == {"detail": "memoria_write_failed"}


def test_post_memoria_flags_gated_returns_ok_false_noop(tmp_path, monkeypatch):
    db_path = tmp_path / "memorias.db"
    _seed_memorias_db(db_path)
    _enable(monkeypatch, db_path, enabled=False)

    with TestClient(_app()) as client:
        resp = client.post(
            "/api/memoria/flags",
            json={"profile_id": "default", "id": "mem_a", "pinned": True},
        )
        assert resp.status_code == 200
        assert resp.json() == {"ok": False}

    row = _read_row(db_path, "mem_a")
    assert row["pinned"] == 0
    assert row["status"] == "draft"  # db untouched


# --- WU2: POST /api/memoria/delete ---


def test_post_memoria_delete_removes_row_then_404_on_repeat(tmp_path, monkeypatch):
    db_path = tmp_path / "memorias.db"
    _seed_memorias_db(db_path)
    _enable(monkeypatch, db_path)

    with TestClient(_app()) as client:
        resp = client.post(
            "/api/memoria/delete",
            json={"profile_id": "default", "id": "mem_a"},
        )
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}

        assert _read_row(db_path, "mem_a") is None  # row gone from db

        # Repeat: the 404 pre-check (store.get) catches the already-absent row
        # before delete_row runs — never a misleading 200/503.
        resp2 = client.post(
            "/api/memoria/delete",
            json={"profile_id": "default", "id": "mem_a"},
        )
        assert resp2.status_code == 404
        assert resp2.json() == {"detail": "memoria not found"}


def test_post_memoria_delete_wrong_profile_id_404_row_survives(tmp_path, monkeypatch):
    db_path = tmp_path / "memorias.db"
    _seed_memorias_db(db_path)
    _enable(monkeypatch, db_path)

    with TestClient(_app()) as client:
        resp = client.post(
            "/api/memoria/delete",
            json={"profile_id": "other", "id": "mem_a"},
        )
        assert resp.status_code == 404
        assert resp.json() == {"detail": "memoria not found"}

    # No side effect on the 404 path — the row is untouched.
    assert _read_row(db_path, "mem_a") is not None


def test_post_memoria_delete_store_write_failure_503(tmp_path, monkeypatch):
    import opencohost.core.memoria_store as store_mod

    db_path = tmp_path / "memorias.db"
    _seed_memorias_db(db_path)
    _enable(monkeypatch, db_path)
    monkeypatch.setattr(store_mod.MemoriaStore, "delete_row", lambda *a, **k: False)

    with TestClient(_app()) as client:
        resp = client.post(
            "/api/memoria/delete",
            json={"profile_id": "default", "id": "mem_a"},
        )
        assert resp.status_code == 503
        assert resp.json() == {"detail": "memoria_write_failed"}


def test_post_memoria_delete_gated_returns_ok_false_noop(tmp_path, monkeypatch):
    db_path = tmp_path / "memorias.db"
    _seed_memorias_db(db_path)
    _enable(monkeypatch, db_path, enabled=False)

    with TestClient(_app()) as client:
        resp = client.post(
            "/api/memoria/delete",
            json={"profile_id": "default", "id": "mem_a"},
        )
        assert resp.status_code == 200
        assert resp.json() == {"ok": False}

    assert _read_row(db_path, "mem_a") is not None  # db untouched


# --- WU3: POST /api/memoria/update ---


def test_post_memoria_update_edits_content_and_curates(tmp_path, monkeypatch):
    db_path = tmp_path / "memorias.db"
    _seed_memorias_db(db_path)  # mem_a is status='draft'
    _enable(monkeypatch, db_path)

    with TestClient(_app()) as client:
        resp = client.post(
            "/api/memoria/update",
            json={
                "profile_id": "default",
                "id": "mem_a",
                "title": "new title",
                "content": "new content",
            },
        )
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}

    row = _read_row(db_path, "mem_a")
    assert row["title"] == "new title"
    assert row["content"] == "new content"
    assert row["status"] == "curated"  # F5: edit promotes in the same statement


def test_post_memoria_update_normalizes_whitespace(tmp_path, monkeypatch):
    db_path = tmp_path / "memorias.db"
    _seed_memorias_db(db_path)
    _enable(monkeypatch, db_path)

    with TestClient(_app()) as client:
        resp = client.post(
            "/api/memoria/update",
            json={
                "profile_id": "default",
                "id": "mem_a",
                "title": "  spaced   title  ",
                "content": "  multi   line\n\ncontent  ",
            },
        )
        assert resp.status_code == 200

    row = _read_row(db_path, "mem_a")
    # Matches store.update_row's " ".join(text.split()) normalization.
    assert row["title"] == "spaced title"
    assert row["content"] == "multi line content"


def test_post_memoria_update_empty_title_or_content_422(tmp_path, monkeypatch):
    db_path = tmp_path / "memorias.db"
    _seed_memorias_db(db_path)
    _enable(monkeypatch, db_path)

    with TestClient(_app()) as client:
        resp_title = client.post(
            "/api/memoria/update",
            json={"profile_id": "default", "id": "mem_a", "title": "   ", "content": "ok"},
        )
        assert resp_title.status_code == 422

        resp_content = client.post(
            "/api/memoria/update",
            json={"profile_id": "default", "id": "mem_a", "title": "ok", "content": "   "},
        )
        assert resp_content.status_code == 422

    # No side effect: the row stays draft, unedited.
    row = _read_row(db_path, "mem_a")
    assert row["status"] == "draft"
    assert row["title"] == "title a"


def test_post_memoria_update_oversize_422(tmp_path, monkeypatch):
    import opencohost.api.main as main_mod

    db_path = tmp_path / "memorias.db"
    _seed_memorias_db(db_path)
    _enable(monkeypatch, db_path)

    with TestClient(_app()) as client:
        resp_title = client.post(
            "/api/memoria/update",
            json={
                "profile_id": "default",
                "id": "mem_a",
                "title": "x" * (main_mod._MEMORIA_TITLE_MAX_LENGTH + 1),
                "content": "ok",
            },
        )
        assert resp_title.status_code == 422

        resp_content = client.post(
            "/api/memoria/update",
            json={
                "profile_id": "default",
                "id": "mem_a",
                "title": "ok",
                "content": "x" * (main_mod._MEMORIA_CONTENT_MAX_LENGTH + 1),
            },
        )
        assert resp_content.status_code == 422


def test_post_memoria_update_response_never_echoes_title_content(tmp_path, monkeypatch):
    db_path = tmp_path / "memorias.db"
    _seed_memorias_db(db_path)
    _enable(monkeypatch, db_path)

    with TestClient(_app()) as client:
        resp = client.post(
            "/api/memoria/update",
            json={
                "profile_id": "default",
                "id": "mem_a",
                "title": "secret title",
                "content": "secret content",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        # R8: MemoriaMutationResponse carries ok only — never the inbound text.
        assert "title" not in body
        assert "content" not in body


def test_post_memoria_update_wrong_profile_id_404(tmp_path, monkeypatch):
    db_path = tmp_path / "memorias.db"
    _seed_memorias_db(db_path)
    _enable(monkeypatch, db_path)

    with TestClient(_app()) as client:
        resp = client.post(
            "/api/memoria/update",
            json={"profile_id": "other", "id": "mem_a", "title": "t", "content": "c"},
        )
        assert resp.status_code == 404
        assert resp.json() == {"detail": "memoria not found"}

    # No side effect on the 404 path.
    row = _read_row(db_path, "mem_a")
    assert row["title"] == "title a"
    assert row["status"] == "draft"


def test_post_memoria_update_missing_id_404(tmp_path, monkeypatch):
    db_path = tmp_path / "memorias.db"
    _seed_memorias_db(db_path)
    _enable(monkeypatch, db_path)

    with TestClient(_app()) as client:
        resp = client.post(
            "/api/memoria/update",
            json={"profile_id": "default", "id": "nope", "title": "t", "content": "c"},
        )
        assert resp.status_code == 404
        # Identical body to the wrong-profile case (R7: no existence oracle).
        assert resp.json() == {"detail": "memoria not found"}


def test_post_memoria_update_store_write_failure_503(tmp_path, monkeypatch):
    import opencohost.core.memoria_store as store_mod

    db_path = tmp_path / "memorias.db"
    _seed_memorias_db(db_path)
    _enable(monkeypatch, db_path)
    monkeypatch.setattr(store_mod.MemoriaStore, "update_row", lambda *a, **k: False)

    with TestClient(_app()) as client:
        resp = client.post(
            "/api/memoria/update",
            json={"profile_id": "default", "id": "mem_a", "title": "t", "content": "c"},
        )
        assert resp.status_code == 503
        assert resp.json() == {"detail": "memoria_write_failed"}


def test_post_memoria_update_gated_returns_ok_false_noop(tmp_path, monkeypatch):
    db_path = tmp_path / "memorias.db"
    _seed_memorias_db(db_path)
    _enable(monkeypatch, db_path, enabled=False)

    with TestClient(_app()) as client:
        resp = client.post(
            "/api/memoria/update",
            json={"profile_id": "default", "id": "mem_a", "title": "t", "content": "c"},
        )
        assert resp.status_code == 200
        assert resp.json() == {"ok": False}

    row = _read_row(db_path, "mem_a")
    assert row["title"] == "title a"
    assert row["status"] == "draft"  # db untouched


# --- WU4: POST /api/memoria/capture (capture-privacy toggle, direct motor call) ---


def test_post_memoria_capture_pauses_and_resumes(monkeypatch):
    import opencohost.api.main as main_mod

    monkeypatch.setattr(main_mod, "MEMORIAS_ENABLED", True)

    app = _app()
    with TestClient(app) as client:
        host = app.state.host

        resp = client.post("/api/memoria/capture", json={"paused": True})
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}
        # Direct motor call (resolution 2905/D3: NOT via /api/commands) — the
        # switch flips on the host's motor, mirroring inspector_memory.py.
        assert host.motor.memorias_private is True

        resp2 = client.post("/api/memoria/capture", json={"paused": False})
        assert resp2.status_code == 200
        assert resp2.json() == {"ok": True}
        assert host.motor.memorias_private is False


def test_post_memoria_capture_gated_is_noop(monkeypatch):
    import opencohost.api.main as main_mod

    monkeypatch.setattr(main_mod, "MEMORIAS_ENABLED", False)

    app = _app()
    with TestClient(app) as client:
        host = app.state.host

        resp = client.post("/api/memoria/capture", json={"paused": True})
        assert resp.status_code == 200
        assert resp.json() == {"ok": False}
        # Gated: motor untouched — mirrors the CTK toggle being absent/inert
        # while the feature is dormant.
        assert host.motor.memorias_private is False


def test_post_memoria_capture_requires_bool(monkeypatch):
    import opencohost.api.main as main_mod

    monkeypatch.setattr(main_mod, "MEMORIAS_ENABLED", True)

    with TestClient(_app()) as client:
        # A non-boolean, non-coercible value must fail Pydantic validation (422).
        # NB: Pydantic v2 lax-coerces yes/no/true/false/on/off/1/0 to bool, so a
        # value OUTSIDE that set (an array here) is used to prove the bool guard.
        resp = client.post("/api/memoria/capture", json={"paused": ["not", "a", "bool"]})
        assert resp.status_code == 422
