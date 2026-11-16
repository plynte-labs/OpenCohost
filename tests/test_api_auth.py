"""Strict-TDD tests for the bearer-token auth core (Phase 1 of
track agent_context_gateway_20260705, design.md 'Auth design' ADR-3/ADR-4).

Owner decision 2026-11-08 binds this suite (it supersedes the warn-only D2):
the enforcement flag (`settings.API_AUTH_ENFORCED`, env OPENCOHOST_API_AUTH)
defaults ON. The kill switch is OPENCOHOST_API_AUTH=0, which restores the old
warn-only behavior: a missing/invalid token logs a warning while the request
still succeeds. Most tests below pin the flag explicitly; `TestDefaultEnforcement`
exercises the real default resolution.

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
    """Kill-switch state (flag OFF) unless a test flips it explicitly."""
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
        # A valid token must reach the route's own request validation (an
        # empty body is a pydantic 422), proving the middleware let the
        # request through instead of rejecting it with 401/403.
        auth.ensure_tokens()
        tokens = auth.load_tokens()
        app = _app()
        with TestClient(app) as client:
            resp = client.post(
                "/api/agent/topics", json={}, headers=_bearer(tokens["agent"])
            )
            assert resp.status_code == 422

    def test_operator_token_is_accepted_on_agent_surface(self):
        # ADR-3: operator token is a superset — accepted wherever agent is.
        auth.ensure_tokens()
        tokens = auth.load_tokens()
        app = _app()
        with TestClient(app) as client:
            resp = client.post(
                "/api/agent/topics", json={}, headers=_bearer(tokens["operator"])
            )
            assert resp.status_code == 422

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


class TestKillSwitchMode:
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

    def test_get_sensitive_route_without_token_succeeds_and_warns(self, caplog):
        app = _app(_host_with_agenda())
        with TestClient(app) as client:
            with caplog.at_level(logging.WARNING, logger="opencohost.api.auth"):
                resp = client.get("/api/memoria/stats")
            assert resp.status_code == 200
            assert len(_auth_warnings(caplog)) == 1

    def test_get_sensitive_route_with_operator_token_succeeds_without_warning(self, caplog):
        auth.ensure_tokens()
        tokens = auth.load_tokens()
        app = _app(_host_with_agenda())
        with TestClient(app) as client:
            with caplog.at_level(logging.WARNING, logger="opencohost.api.auth"):
                resp = client.get("/api/memoria/stats", headers=_bearer(tokens["operator"]))
            assert resp.status_code == 200
            assert _auth_warnings(caplog) == []

    def test_get_non_sensitive_route_with_invalid_token_warns(self, caplog):
        auth.ensure_tokens()
        app = _app(_host_with_agenda())
        with TestClient(app) as client:
            with caplog.at_level(logging.WARNING, logger="opencohost.api.auth"):
                resp = client.get("/api/health", headers=_bearer("wrong-token"))
            assert resp.status_code == 200
            assert len(_auth_warnings(caplog)) == 1

    def test_get_non_sensitive_route_without_token_does_not_warn(self, caplog):
        app = _app(_host_with_agenda())
        with TestClient(app) as client:
            with caplog.at_level(logging.WARNING, logger="opencohost.api.auth"):
                resp = client.get("/api/health")
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

    def test_get_sensitive_route_without_token_is_401(self):
        app = _app(_host_with_agenda())
        with TestClient(app) as client:
            resp = client.get("/api/memoria/stats")
            assert resp.status_code == 401
            assert resp.json() == {"detail": "missing bearer token"}

    def test_get_sensitive_route_with_invalid_token_is_401(self):
        auth.ensure_tokens()
        app = _app(_host_with_agenda())
        with TestClient(app) as client:
            resp = client.get("/api/memoria/stats", headers=_bearer("wrong-token"))
            assert resp.status_code == 401
            assert resp.json() == {"detail": "invalid token"}

    def test_get_sensitive_route_with_agent_token_is_403(self):
        auth.ensure_tokens()
        tokens = auth.load_tokens()
        app = _app(_host_with_agenda())
        with TestClient(app) as client:
            resp = client.get("/api/memoria/stats", headers=_bearer(tokens["agent"]))
            assert resp.status_code == 403
            assert resp.json() == {"detail": "operator token required"}

    def test_get_sensitive_route_with_operator_token_is_200(self):
        auth.ensure_tokens()
        tokens = auth.load_tokens()
        app = _app(_host_with_agenda())
        with TestClient(app) as client:
            resp = client.get("/api/memoria/stats", headers=_bearer(tokens["operator"]))
            assert resp.status_code == 200

    def test_get_sensitive_route_corrupt_token_file_is_503(self):
        _tokens_path().parent.mkdir(parents=True, exist_ok=True)
        _tokens_path().write_text("garbage-not-json", encoding="utf-8")
        app = _app(_host_with_agenda())
        with TestClient(app) as client:
            resp = client.get("/api/memoria/stats", headers=_bearer("anything"))
            assert resp.status_code == 503

    def test_stream_admin_prefix_is_not_a_sensitive_route(self):
        # /api/stream-admin matches no route (dead prefix, removed 2026-11-08).
        assert not auth._is_sensitive_path("/api/stream-admin/status")
        assert not any("stream-admin" in p for p in auth._SENSITIVE_PREFIXES)

    def test_get_non_sensitive_route_with_invalid_token_is_401(self):
        auth.ensure_tokens()
        app = _app(_host_with_agenda())
        with TestClient(app) as client:
            resp = client.get("/api/health", headers=_bearer("wrong-token"))
            assert resp.status_code == 401
            assert resp.json() == {"detail": "invalid token"}

    def test_get_non_sensitive_route_with_valid_token_is_200(self):
        auth.ensure_tokens()
        tokens = auth.load_tokens()
        app = _app(_host_with_agenda())
        with TestClient(app) as client:
            resp = client.get("/api/health", headers=_bearer(tokens["agent"]))
            assert resp.status_code == 200
            resp = client.get("/api/health", headers=_bearer(tokens["operator"]))
            assert resp.status_code == 200


# ──────────────────────────────────────────────────────────────────────────
# Default-ON enforcement (owner decision 2026-11-08) and the kill switch
# ──────────────────────────────────────────────────────────────────────────


class TestDefaultEnforcement:
    def test_resolver_defaults_on_without_env(self, monkeypatch):
        monkeypatch.delenv("OPENCOHOST_API_AUTH", raising=False)
        assert settings._resolve_api_auth_enforced() is True

    @pytest.mark.parametrize("value", ["1", "true", "", "yes"])
    def test_resolver_stays_on_for_non_zero_values(self, monkeypatch, value):
        monkeypatch.setenv("OPENCOHOST_API_AUTH", value)
        assert settings._resolve_api_auth_enforced() is True

    def test_resolver_kill_switch_zero_turns_it_off(self, monkeypatch):
        monkeypatch.setenv("OPENCOHOST_API_AUTH", "0")
        assert settings._resolve_api_auth_enforced() is False

    def test_default_config_memoria_get_without_token_is_401_and_with_token_200(self, monkeypatch):
        monkeypatch.delenv("OPENCOHOST_API_AUTH", raising=False)
        monkeypatch.setattr(settings, "API_AUTH_ENFORCED", settings._resolve_api_auth_enforced())
        auth.ensure_tokens()
        tokens = auth.load_tokens()
        app = _app(_host_with_agenda())
        with TestClient(app) as client:
            assert client.get("/api/memoria/stats").status_code == 401
            ok = client.get("/api/memoria/stats", headers=_bearer(tokens["operator"]))
            assert ok.status_code == 200

    def test_kill_switch_memoria_get_without_token_passes(self, monkeypatch):
        monkeypatch.setenv("OPENCOHOST_API_AUTH", "0")
        monkeypatch.setattr(settings, "API_AUTH_ENFORCED", settings._resolve_api_auth_enforced())
        app = _app(_host_with_agenda())
        with TestClient(app) as client:
            assert client.get("/api/memoria/stats").status_code == 200


class TestTokenFileAcl:
    def test_ensure_tokens_restricts_the_token_file(self, monkeypatch):
        calls = []
        import opencohost.stream_admin.oauth_store as store

        monkeypatch.setattr(store, "restrict_file_permissions", lambda p: calls.append(str(p)) or True)
        auth.ensure_tokens()
        assert calls == [str(_tokens_path())]

    def test_ensure_tokens_logs_when_restriction_fails_without_leaking_tokens(self, monkeypatch, caplog):
        import opencohost.stream_admin.oauth_store as store

        monkeypatch.setattr(store, "restrict_file_permissions", lambda p: False)
        with caplog.at_level(logging.WARNING, logger="opencohost.api.auth"):
            auth.ensure_tokens()
        warnings = _auth_warnings(caplog)
        assert len(warnings) == 1
        tokens = json.loads(_tokens_path().read_text(encoding="utf-8"))
        text = warnings[0].getMessage()
        assert tokens["operator"] not in text and tokens["agent"] not in text
        # The token file is still usable despite the failed ACL restriction.
        assert auth.load_tokens()["operator"] == tokens["operator"]
