"""WU3 — persist failures surface as 503, never a false 200/500 (backend_reliability_fixes).

Design: engram sdd/backend_reliability_fixes/design (id 2936), WU3.

Before this WU the write handlers lied on failure:
  - POST/PUT/DELETE /api/perfiles returned 200 even when guardar_perfiles could
    not persist (fail-open writer, ignored return) -> caller believes a write
    landed that never did.
  - POST /api/agenda/cohost-profiles returned 200 even when the save failed.
  - PUT /api/obs|avatar/config let a save OSError propagate as an opaque 500.
  - A locked/corrupt memoria DB construction raised -> 500; a transient read
    lock on the 404 pre-check masqueraded as a 404 (row-absent).

Seams (design WU3 "Failing tests first"): the writer is made to fail
(``os.replace`` raiser for the atomic path; ``_get_memoria_store`` /
store-method raiser for memoria) and the handler must map it to the correct
503, not a false success or a misleading code.
"""

import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from tests.test_api_phase1 import FakeHost

_DEFAULT_TEST_ORIGINS = ["http://localhost:5173"]


def _raise_oserror(*args, **kwargs):
    raise OSError("simulated write failure")


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


# ──────────────────────────────────────────────────────────────────────────
# Profiles — guardar_perfiles failure -> 503 profiles_write_failed (NOT 200)
# ──────────────────────────────────────────────────────────────────────────


@pytest.fixture
def profiles_file(tmp_path, monkeypatch):
    import opencohost.core.profiles as profiles_mod

    path = tmp_path / "perfiles.json"
    path.write_text(
        json.dumps(
            {
                "default": {"id": "id-default", "prompt": "Default", "use_system": False},
                "streamer": {"id": "id-streamer", "prompt": "Streamer", "use_system": True},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(profiles_mod, "PROFILES_FILE", str(path))
    return path


def test_post_perfiles_write_failure_returns_503(profiles_file, monkeypatch):
    import opencohost.core.profiles as profiles_mod

    monkeypatch.setattr(profiles_mod.os, "replace", _raise_oserror)
    with TestClient(_app()) as client:
        resp = client.post("/api/perfiles", json={"name": "nueva", "prompt": "hola"})
        assert resp.status_code == 503
        assert resp.json() == {"detail": "profiles_write_failed"}


def test_put_perfiles_write_failure_returns_503(profiles_file, monkeypatch):
    import opencohost.core.profiles as profiles_mod

    monkeypatch.setattr(profiles_mod.os, "replace", _raise_oserror)
    with TestClient(_app()) as client:
        resp = client.put("/api/perfiles/default", json={"prompt": "editado"})
        assert resp.status_code == 503
        assert resp.json() == {"detail": "profiles_write_failed"}


def test_delete_perfiles_write_failure_returns_503(profiles_file, monkeypatch):
    import opencohost.core.profiles as profiles_mod

    monkeypatch.setattr(profiles_mod.os, "replace", _raise_oserror)
    with TestClient(_app()) as client:
        resp = client.delete("/api/perfiles/streamer")
        assert resp.status_code == 503
        assert resp.json() == {"detail": "profiles_write_failed"}


# ──────────────────────────────────────────────────────────────────────────
# Cohost profiles — save failure -> 503 cohost_write_failed (NOT 200)
# ──────────────────────────────────────────────────────────────────────────


def test_post_cohost_profiles_write_failure_returns_503(tmp_path, monkeypatch):
    import opencohost.core.cohost_profiles as cp_mod
    import opencohost.config.storage as storage_mod

    monkeypatch.setattr(cp_mod, "COHOST_PROFILES_FILE", str(tmp_path / "cohost.json"))
    monkeypatch.setattr(storage_mod.os, "replace", _raise_oserror)
    with TestClient(_app()) as client:
        resp = client.post(
            "/api/agenda/cohost-profiles",
            json={"name": "Nueva", "style": "seca y directa"},
        )
        assert resp.status_code == 503
        assert resp.json() == {"detail": "cohost_write_failed"}


# ──────────────────────────────────────────────────────────────────────────
# Avatar/OBS config — save failure -> 503 config_write_failed (NOT 500)
# ──────────────────────────────────────────────────────────────────────────


def test_put_avatar_config_write_failure_returns_503(tmp_path, monkeypatch):
    import opencohost.avatar.avatar_config as avatar_config_mod
    import opencohost.config.storage as storage_mod

    # Absent file -> strict load returns defaults (no config_unreadable); the
    # SAVE is what must fail here.
    monkeypatch.setattr(avatar_config_mod, "AVATAR_CONFIG_FILE", tmp_path / "avatar.yaml")
    monkeypatch.setattr(storage_mod.os, "replace", _raise_oserror)
    with TestClient(_app()) as client:
        resp = client.put("/api/avatar/config", json={"enabled": False})
        assert resp.status_code == 503
        assert resp.json() == {"detail": "config_write_failed"}


def test_put_obs_config_write_failure_returns_503(tmp_path, monkeypatch):
    import opencohost.avatar.avatar_config as avatar_config_mod
    import opencohost.config.storage as storage_mod

    monkeypatch.setattr(avatar_config_mod, "AVATAR_CONFIG_FILE", tmp_path / "avatar.yaml")
    monkeypatch.setattr(storage_mod.os, "replace", _raise_oserror)
    with TestClient(_app()) as client:
        resp = client.put("/api/obs/config", json={"host": "localhost", "port": 4455})
        assert resp.status_code == 503
        assert resp.json() == {"detail": "config_write_failed"}


# ──────────────────────────────────────────────────────────────────────────
# Memoria — availability + transient-lock vs row-absent (D4, P3 fold)
# ──────────────────────────────────────────────────────────────────────────


def _enable_memorias(monkeypatch, db_path):
    import opencohost.api.main as main_mod

    monkeypatch.setattr(main_mod, "MEMORIAS_ENABLED", True)
    monkeypatch.setattr(main_mod, "MEMORIAS_DB", str(db_path))


class _FakeStore:
    """Minimal MemoriaStore stand-in exercising the raising seam (D4)."""

    def __init__(self, *, get_result=None, get_exc=None, set_flags_exc=None, set_flags_result=True):
        self._get_result = get_result
        self._get_exc = get_exc
        self._set_flags_exc = set_flags_exc
        self._set_flags_result = set_flags_result

    def get(self, memoria_id, *, raising=False):
        if self._get_exc is not None:
            raise self._get_exc
        return self._get_result

    def set_flags(self, memoria_id, *, pinned=None, private=None, inactive=None, raising=False):
        if self._set_flags_exc is not None:
            raise self._set_flags_exc
        return self._set_flags_result


def test_memoria_flags_store_construction_failure_returns_503(tmp_path, monkeypatch):
    import opencohost.api.main as main_mod

    _enable_memorias(monkeypatch, tmp_path / "memorias.db")
    monkeypatch.setattr(
        main_mod,
        "_get_memoria_store",
        lambda: (_ for _ in ()).throw(sqlite3.OperationalError("database is locked")),
    )
    with TestClient(_app()) as client:
        resp = client.post(
            "/api/memoria/flags",
            json={"profile_id": "default", "id": "mem_a", "pinned": True},
        )
        assert resp.status_code == 503
        assert resp.json() == {"detail": "memoria_unavailable"}


def test_memoria_flags_transient_lock_on_get_returns_503_not_404(tmp_path, monkeypatch):
    import opencohost.api.main as main_mod

    _enable_memorias(monkeypatch, tmp_path / "memorias.db")
    fake = _FakeStore(get_exc=sqlite3.OperationalError("database is locked"))
    monkeypatch.setattr(main_mod, "_get_memoria_store", lambda: fake)
    with TestClient(_app()) as client:
        resp = client.post(
            "/api/memoria/flags",
            json={"profile_id": "default", "id": "mem_a", "pinned": True},
        )
        # A transient read lock must NOT masquerade as a missing row (404).
        assert resp.status_code == 503
        assert resp.json() == {"detail": "memoria_unavailable"}


def test_memoria_flags_row_vanished_after_precheck_returns_404(tmp_path, monkeypatch):
    import opencohost.api.main as main_mod

    _enable_memorias(monkeypatch, tmp_path / "memorias.db")
    # Pre-check finds the row; the write then affects 0 rows (row deleted in the
    # race window). rowcount==0 on a pre-found row -> 404, not a write-failed 503.
    fake = _FakeStore(get_result={"profile_id": "default"}, set_flags_result=False)
    monkeypatch.setattr(main_mod, "_get_memoria_store", lambda: fake)
    with TestClient(_app()) as client:
        resp = client.post(
            "/api/memoria/flags",
            json={"profile_id": "default", "id": "mem_a", "pinned": True},
        )
        assert resp.status_code == 404
        assert resp.json() == {"detail": "memoria not found"}
