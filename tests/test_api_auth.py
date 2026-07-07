"""Strict-TDD tests for the warn-only bearer-token auth core (Phase 1 of
track agent_context_gateway_20260705, design.md 'Auth design' ADR-3/ADR-4).

Owner decision D2 binds this suite: auth ships WARN-ONLY — the enforcement
flag (`settings.API_AUTH_ENFORCED`, env OPENCOHOST_API_AUTH) defaults OFF, and
a missing/invalid token on existing mutating routes logs a warning while the
request still succeeds, so the shipped Tauri app (which sends no token today)
keeps working unchanged.

Covers:
- ensure_tokens(): mints ``{"version": 1, "operator": ..., "agent": ...}``
  once and only once (delete the file to rotate).
- load_tokens(): corrupt/missing file raises TokenFileError (the middleware
  maps that to 503 on protected surfaces — never a crash).
- verify_token(): match/mismatch semantics (hmac.compare_digest path).
- Middleware rule 1: paths under /api/agent/ require agent OR operator token,
  ALWAYS enforced (401 missing/bad token even with the flag off).
- Middleware rule 2: other /api/* POST/PUT/DELETE — flag off: 200 + one
  warning without a valid operator token; flag on: 401 missing/bad, 403 agent
  token, 200 operator token.
- Middleware rule 3: GET stays open in v1.
- lifespan() mints the token file at startup.

tests/conftest.py redirects ``settings.API_TOKENS_FILE`` to a per-test temp
path (autouse), so nothing here ever writes the real user config directory.
"""

import json
import logging
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import opencohost.api.auth as auth
from opencohost.config import settings
from opencohost.smart_aggregator.kira_agenda_controller import KiraAgendaController
from tests.test_api_phase1 import FakeHost

_DEFAULT_TEST_ORIGINS = ["http://localhost:5173"]


@pytest.fixture(autouse=True)
def _reset_host_active():
    import opencohost.api.main as main_mod

    main_mod._host_active = False
    yield
    main_mod._host_active = False


@pytest.fixture(autouse=True)
def _auth_flag_off(monkeypatch):
    """D2 default: enforcement flag OFF unless a test flips it explicitly."""
    monkeypatch.setattr(settings, "API_AUTH_ENFORCED", False)


def _app(host_factory=FakeHost):
    import opencohost.api.main as main_mod

    return main_mod.create_app(host_factory=host_factory, cors_origins=_DEFAULT_TEST_ORIGINS)


def _host_with_agenda():
    def factory():
        host = FakeHost()
        host.agenda = KiraAgendaController()
        return host

    return factory


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _tokens_path() -> Path:
    return Path(settings.API_TOKENS_FILE)


def _auth_warnings(caplog) -> list:
    return [
        record
        for record in caplog.records
        if record.name == "opencohost.api.auth" and record.levelno == logging.WARNING
    ]


# ──────────────────────────────────────────────────────────────────────────
# Token lifecycle: mint once, load, verify
# ──────────────────────────────────────────────────────────────────────────


class TestTokenLifecycle:
    def test_ensure_tokens_mints_expected_format(self):
        auth.ensure_tokens()
        data = json.loads(_tokens_path().read_text(encoding="utf-8"))
        assert data["version"] == 1
        assert isinstance(data["operator"], str)
        assert isinstance(data["agent"], str)
        # secrets.token_urlsafe(32) yields ~43 chars; require real entropy.
        assert len(data["operator"]) >= 32
        assert len(data["agent"]) >= 32
        assert data["operator"] != data["agent"]

    def test_ensure_tokens_never_regenerates_existing_file(self):
        auth.ensure_tokens()
        first = _tokens_path().read_text(encoding="utf-8")
        auth.ensure_tokens()
        assert _tokens_path().read_text(encoding="utf-8") == first

    def test_load_tokens_roundtrip(self):
        auth.ensure_tokens()
        tokens = auth.load_tokens()
        minted = json.loads(_tokens_path().read_text(encoding="utf-8"))
        assert tokens == {"operator": minted["operator"], "agent": minted["agent"]}

    def test_load_tokens_missing_file_raises(self):
        with pytest.raises(auth.TokenFileError):
            auth.load_tokens()

    def test_load_tokens_corrupt_json_raises(self):
        _tokens_path().parent.mkdir(parents=True, exist_ok=True)
        _tokens_path().write_text("{not valid json", encoding="utf-8")
        with pytest.raises(auth.TokenFileError):
            auth.load_tokens()

    def test_load_tokens_missing_key_raises(self):
        _tokens_path().parent.mkdir(parents=True, exist_ok=True)
        _tokens_path().write_text(
            json.dumps({"version": 1, "operator": "only-one-token"}), encoding="utf-8"
        )
        with pytest.raises(auth.TokenFileError):
            auth.load_tokens()

    def test_verify_token_match_and_mismatch(self):
        assert auth.verify_token("same-token", "same-token") is True
        assert auth.verify_token("same-token", "other-token") is False
        assert auth.verify_token("", "expected") is False


@pytest.mark.parametrize(
    "path", ["config/api_tokens.json", "opencohost/config/api_tokens.json"]
)
def test_minted_token_file_is_gitignored(path):
    """Dev-mode USER_DATA_DIR is the repo root (opencohost/config/storage.py),
    so ensure_tokens() mints a live bearer secret at config/api_tokens.json
    inside the working tree. It must never be stageable via `git add`.
    """
    repo_root = Path(__file__).resolve().parents[1]
    if not (repo_root / ".git").exists():
        pytest.skip("not a git checkout")
    result = subprocess.run(
        ["git", "check-ignore", "-q", path], cwd=repo_root, capture_output=True
    )
    assert result.returncode == 0, f"{path} is not gitignored — secret leak risk"


def test_lifespan_mints_token_file_at_startup():
    app = _app()
    assert not _tokens_path().exists()
    with TestClient(app):
        assert _tokens_path().exists()
        data = json.loads(_tokens_path().read_text(encoding="utf-8"))
        assert data["version"] == 1


# ──────────────────────────────────────────────────────────────────────────
# Rule 1: /api/agent/* always requires a valid token (flag off included)
# ──────────────────────────────────────────────────────────────────────────


class TestAgentSurfaceAlwaysEnforced:
    def test_post_without_token_is_401_even_with_flag_off(self):
        app = _app()
        with TestClient(app) as client:
            resp = client.post("/api/agent/topics", json={})
            assert resp.status_code == 401

    def test_get_without_token_is_401(self):
        app = _app()
        with TestClient(app) as client:
            resp = client.get("/api/agent/status")
            assert resp.status_code == 401

    def test_invalid_token_is_401(self):
        app = _app()
        with TestClient(app) as client:
            resp = client.post(
                "/api/agent/topics", json={}, headers=_bearer("not-a-real-token")
            )
            assert resp.status_code == 401

    def test_valid_agent_token_passes_middleware(self):
        # No /api/agent/* routes exist yet (they land in Phase 2): a valid
        # token must reach the router and get its plain 404, proving the
        # middleware let the request through instead of rejecting it.
        auth.ensure_tokens()
        tokens = auth.load_tokens()
        app = _app()
        with TestClient(app) as client:
            resp = client.post(
                "/api/agent/topics", json={}, headers=_bearer(tokens["agent"])
            )
            assert resp.status_code == 404

    def test_operator_token_is_accepted_on_agent_surface(self):
        # ADR-3: operator token is a superset — accepted wherever agent is.
        auth.ensure_tokens()
        tokens = auth.load_tokens()
        app = _app()
        with TestClient(app) as client:
            resp = client.post(
                "/api/agent/topics", json={}, headers=_bearer(tokens["operator"])
            )
            assert resp.status_code == 404

    def test_corrupt_token_file_yields_503_not_crash(self):
        _tokens_path().parent.mkdir(parents=True, exist_ok=True)
        _tokens_path().write_text("garbage-not-json", encoding="utf-8")
        app = _app()
        with TestClient(app) as client:
            resp = client.post(
                "/api/agent/topics", json={}, headers=_bearer("anything")
            )
            assert resp.status_code == 503


# ──────────────────────────────────────────────────────────────────────────
# Rule 2, flag OFF (D2 warn-only): mutating routes succeed + warn
# ──────────────────────────────────────────────────────────────────────────


class TestWarnOnlyMode:
    def test_post_without_token_succeeds_and_logs_one_warning(self, caplog):
        app = _app(_host_with_agenda())
        with TestClient(app) as client:
            with caplog.at_level(logging.WARNING, logger="opencohost.api.auth"):
                resp = client.post(
                    "/api/agenda/topic", json={"title": "Warn only test topic"}
                )
            assert resp.status_code == 200
            assert len(_auth_warnings(caplog)) == 1

    def test_post_with_invalid_token_succeeds_and_logs_one_warning(self, caplog):
        auth.ensure_tokens()
        app = _app(_host_with_agenda())
        with TestClient(app) as client:
            with caplog.at_level(logging.WARNING, logger="opencohost.api.auth"):
                resp = client.post(
                    "/api/agenda/topic",
                    json={"title": "Warn only bad token topic"},
                    headers=_bearer("wrong-token"),
                )
            assert resp.status_code == 200
            assert len(_auth_warnings(caplog)) == 1

    def test_post_with_operator_token_succeeds_without_warning(self, caplog):
        auth.ensure_tokens()
        tokens = auth.load_tokens()
        app = _app(_host_with_agenda())
        with TestClient(app) as client:
            with caplog.at_level(logging.WARNING, logger="opencohost.api.auth"):
                resp = client.post(
                    "/api/agenda/topic",
                    json={"title": "Warn only operator topic"},
                    headers=_bearer(tokens["operator"]),
                )
            assert resp.status_code == 200
            assert _auth_warnings(caplog) == []


# ──────────────────────────────────────────────────────────────────────────
# Rule 2, flag ON: operator token required on mutating routes
# ──────────────────────────────────────────────────────────────────────────


class TestEnforcedMode:
    @pytest.fixture(autouse=True)
    def _flag_on(self, monkeypatch):
        monkeypatch.setattr(settings, "API_AUTH_ENFORCED", True)

    def test_missing_token_is_401(self):
        app = _app(_host_with_agenda())
        with TestClient(app) as client:
            resp = client.post("/api/agenda/topic", json={"title": "Enforced topic"})
            assert resp.status_code == 401

    def test_invalid_token_is_401(self):
        auth.ensure_tokens()
        app = _app(_host_with_agenda())
        with TestClient(app) as client:
            resp = client.post(
                "/api/agenda/topic",
                json={"title": "Enforced topic"},
                headers=_bearer("wrong-token"),
            )
            assert resp.status_code == 401

    def test_agent_token_on_operator_surface_is_403(self):
        # Owner decision D1: agents never reach the agenda queue directly.
        auth.ensure_tokens()
        tokens = auth.load_tokens()
        app = _app(_host_with_agenda())
        with TestClient(app) as client:
            resp = client.post(
                "/api/agenda/topic",
                json={"title": "Enforced topic"},
                headers=_bearer(tokens["agent"]),
            )
            assert resp.status_code == 403

    def test_operator_token_is_200(self):
        auth.ensure_tokens()
        tokens = auth.load_tokens()
        app = _app(_host_with_agenda())
        with TestClient(app) as client:
            resp = client.post(
                "/api/agenda/topic",
                json={"title": "Enforced operator topic"},
                headers=_bearer(tokens["operator"]),
            )
            assert resp.status_code == 200

    def test_corrupt_token_file_is_503(self):
        _tokens_path().parent.mkdir(parents=True, exist_ok=True)
        _tokens_path().write_text("garbage-not-json", encoding="utf-8")
        app = _app(_host_with_agenda())
        with TestClient(app) as client:
            resp = client.post(
                "/api/agenda/topic",
                json={"title": "Enforced topic"},
                headers=_bearer("anything"),
            )
            assert resp.status_code == 503

    def test_get_stays_open_without_token(self):
        # Rule 3: the read surface stays open in v1 even when enforced.
        app = _app(_host_with_agenda())
        with TestClient(app) as client:
            resp = client.get("/api/health")
            assert resp.status_code == 200
