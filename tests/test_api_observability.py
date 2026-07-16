"""Tests for opencohost/api/observability.py (WU-C, WU-B).

WU-C persists and redacts the ``opencohost.api`` logger tree. Today the
API's own loggers (``opencohost.api``, ``opencohost.api.engine_host``, ...)
are a separate tree from the configured ``OpenCohost`` engine logger
(opencohost/config/logger.py) -- with no handler attached, anything logged
under ``opencohost.api.*`` falls through to ``logging.lastResort``:
stderr-only, WARNING+, unpersisted, and UNREDACTED.

WU-B adds a per-request audit trail (``audit_middleware``): one metadata-only
JSONL line per request (ts, method, path, status, duration_ms, role,
idempotency_key) -- never the request/response body, never the Authorization
value, never query-string content.
"""

import json
import logging
import logging.handlers
import threading

import pytest
from fastapi.testclient import TestClient

import opencohost.api.auth as auth
import opencohost.api.engine_host as engine_host_mod
import opencohost.api.main as main_mod
from opencohost.api import observability
from opencohost.config import settings
from tests.test_api_phase1 import FakeHost


@pytest.fixture
def clean_api_logger():
    """Isolate the shared ``opencohost.api`` logger for each test.

    ``setup_api_logging()`` attaches handlers to the single, process-wide
    ``logging.getLogger("opencohost.api")`` instance stdlib logging keeps
    for the whole process. Reset around each test so tests in this file
    don't leak handlers into each other (and close handlers opened during
    the test before restoring, so Windows doesn't keep the tmp_path file
    locked after teardown).
    """
    logger = logging.getLogger("opencohost.api")
    saved_handlers = list(logger.handlers)
    logger.handlers = []
    yield logger
    for handler in logger.handlers:
        handler.close()
    logger.handlers = saved_handlers


def _file_handlers(logger):
    return [h for h in logger.handlers if isinstance(h, logging.handlers.RotatingFileHandler)]


def test_setup_api_logging_is_idempotent(tmp_path, monkeypatch, clean_api_logger):
    """Calling setup_api_logging() twice must leave exactly one file handler."""
    from opencohost.config import settings

    monkeypatch.setattr(settings, "LOG_DIR", str(tmp_path))

    observability.setup_api_logging()
    observability.setup_api_logging()

    assert len(_file_handlers(clean_api_logger)) == 1


def test_setup_api_logging_redacts_bearer_token_through_propagation(
    tmp_path, monkeypatch, clean_api_logger
):
    """A record logged on a CHILD logger (opencohost.api.engine_host) must
    land, redacted, in the file attached to the PARENT (opencohost.api) --
    proving handler-level filtering (not logger-level) is what redacts."""
    from opencohost.config import settings

    monkeypatch.setattr(settings, "LOG_DIR", str(tmp_path))

    observability.setup_api_logging()

    child_logger = logging.getLogger("opencohost.api.engine_host")
    child_logger.error("token leak check: Bearer abc123token")

    for handler in clean_api_logger.handlers:
        handler.flush()

    log_path = tmp_path / "opencohost_api.log"
    assert log_path.exists()
    content = log_path.read_text(encoding="utf-8")
    assert "<redacted>" in content
    assert "abc123token" not in content


def test_setup_api_logging_sets_info_level_on_api_logger_tree(clean_api_logger):
    """The shared ``opencohost.api`` logger must be raised to INFO so lifecycle
    lines from every child (ptt_session, engine_host, agenda_driver, ...)
    actually reach the attached handlers instead of falling to the stdlib
    default of WARNING."""
    observability.setup_api_logging()

    assert logging.getLogger("opencohost.api.ptt_session").isEnabledFor(logging.INFO)


def test_setup_api_logging_persists_child_info_record(tmp_path, monkeypatch, clean_api_logger):
    """An INFO record logged on a child logger must reach the file handler
    attached to the parent -- proving the tree-level setLevel(INFO), not just
    isEnabledFor(), actually lets the record through."""
    from opencohost.config import settings

    monkeypatch.setattr(settings, "LOG_DIR", str(tmp_path))

    observability.setup_api_logging()

    child_logger = logging.getLogger("opencohost.api.ptt_session")
    child_logger.info("[PTT] flushing test")

    for handler in clean_api_logger.handlers:
        handler.flush()

    log_path = tmp_path / "opencohost_api.log"
    assert log_path.exists()
    content = log_path.read_text(encoding="utf-8")
    assert "[PTT] flushing test" in content


# ──────────────────────────────────────────────────────────────────────────
# WU-B: per-request audit trail (audit_middleware)
# ──────────────────────────────────────────────────────────────────────────

_DEFAULT_TEST_ORIGINS = ["http://localhost:5173"]
_AUDIT_WHITELIST_KEYS = {
    "ts",
    "method",
    "path",
    "status",
    "duration_ms",
    "role",
    "idempotency_key",
}


@pytest.fixture(autouse=True)
def _reset_host_active():
    """create_app()'s in-process double-start guard is a module global --
    reset it around every test in this file so a TestClient in one test
    never fails create_app() in the next (same pattern as test_api_auth.py)."""
    main_mod._host_active = False
    yield
    main_mod._host_active = False


@pytest.fixture
def clean_audit_logger():
    """Isolate the shared ``opencohost.api.audit`` logger for each test --
    same rationale as clean_api_logger above (process-wide singleton via
    logging.getLogger, Windows file-lock-safe teardown)."""
    logger = logging.getLogger("opencohost.api.audit")
    saved_handlers = list(logger.handlers)
    saved_propagate = logger.propagate
    logger.handlers = []
    yield logger
    for handler in logger.handlers:
        handler.close()
    logger.handlers = saved_handlers
    logger.propagate = saved_propagate


def _app(host_factory=FakeHost):
    return main_mod.create_app(host_factory=host_factory, cors_origins=_DEFAULT_TEST_ORIGINS)


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _read_audit_lines(tmp_path):
    for handler in logging.getLogger("opencohost.api.audit").handlers:
        handler.flush()
    log_path = tmp_path / "api_audit.jsonl"
    assert log_path.exists()
    raw = log_path.read_text(encoding="utf-8")
    lines = [json.loads(line) for line in raw.strip().splitlines()]
    return lines, raw


def test_audit_line_has_exact_whitelist_keys_and_no_leaked_secrets(
    tmp_path, monkeypatch, clean_audit_logger
):
    """A sentinel body value, the raw Authorization token, and the query
    string must never reach the audit file -- only the closed metadata
    whitelist."""
    monkeypatch.setattr(settings, "LOG_DIR", str(tmp_path))
    sentinel = "SENTINEL_BODY_MARKER_do_not_leak"
    app = _app()
    with TestClient(app) as client:
        resp = client.post(
            "/api/commands?secret=1",
            json={"command": "clear_history", "payload": {"note": sentinel}},
            headers=_bearer("not-a-real-token"),
        )
        assert resp.status_code == 200

    lines, raw = _read_audit_lines(tmp_path)
    assert len(lines) == 1
    entry = lines[0]
    assert set(entry.keys()) == _AUDIT_WHITELIST_KEYS
    assert entry["method"] == "POST"
    assert entry["path"] == "/api/commands"
    assert entry["status"] == 200
    assert isinstance(entry["duration_ms"], (int, float))
    assert entry["duration_ms"] >= 0
    assert isinstance(entry["ts"], (int, float))
    # Invalid token on a non-agent, warn-only surface -> unresolved role.
    assert entry["role"] is None
    assert entry["idempotency_key"] is None

    assert sentinel not in raw
    assert "not-a-real-token" not in raw
    assert "secret" not in raw


def test_audit_role_resolves_operator_for_valid_token(tmp_path, monkeypatch, clean_audit_logger):
    monkeypatch.setattr(settings, "LOG_DIR", str(tmp_path))
    auth.ensure_tokens()
    tokens = auth.load_tokens()
    app = _app()
    with TestClient(app) as client:
        resp = client.post(
            "/api/commands",
            json={"command": "clear_history"},
            headers=_bearer(tokens["operator"]),
        )
        assert resp.status_code == 200

    lines, _ = _read_audit_lines(tmp_path)
    assert lines[-1]["role"] == "operator"


def test_audit_records_401_for_unauthenticated_agent_surface(
    tmp_path, monkeypatch, clean_audit_logger
):
    """audit_middleware wraps auth_middleware, so a rejection the auth gate
    makes on its own (never reaching a route handler) is still audited."""
    monkeypatch.setattr(settings, "LOG_DIR", str(tmp_path))
    app = _app()
    with TestClient(app) as client:
        resp = client.post("/api/agent/topics", json={})
        assert resp.status_code == 401

    lines, _ = _read_audit_lines(tmp_path)
    assert lines[-1]["status"] == 401
    assert lines[-1]["path"] == "/api/agent/topics"
    assert lines[-1]["role"] is None


def test_audit_middleware_does_not_change_response(tmp_path, monkeypatch, clean_audit_logger):
    """Neutrality: registering audit_middleware must not alter an existing
    endpoint's response body or status (design D-neutrality)."""
    monkeypatch.setattr(settings, "LOG_DIR", str(tmp_path))
    app = _app()
    with TestClient(app) as client:
        resp = client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "engine_alive": True}


def test_audit_middleware_logs_500_and_reraises_for_unhandled_exception(
    tmp_path, monkeypatch, clean_audit_logger
):
    """An unhandled route exception is the request most worth auditing --
    it must still produce one audit line (status 500), and the response
    behavior (a 500) must stay unchanged."""
    monkeypatch.setattr(settings, "LOG_DIR", str(tmp_path))
    app = _app()

    @app.get("/api/_test_boom")
    def _boom():
        raise RuntimeError("boom")

    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.get("/api/_test_boom")
        assert resp.status_code == 500

    lines, _ = _read_audit_lines(tmp_path)
    assert lines[-1]["status"] == 500
    assert lines[-1]["path"] == "/api/_test_boom"


# ──────────────────────────────────────────────────────────────────────────
# WU-A: acciones.jsonl motor-event parity (log_motor_accion)
# ──────────────────────────────────────────────────────────────────────────


def test_log_motor_accion_writes_ctk_compatible_line(tmp_path, monkeypatch):
    """Same schema CTK's advanced_panel._guardar_accion writes: exactly
    {"ts", "msg"}, msg carries the raw status prefixed with "[api] "."""
    acciones_file = tmp_path / "acciones.jsonl"
    monkeypatch.setattr(settings, "ACCIONES_LOG_FILE", str(acciones_file))

    observability.log_motor_accion("speaking_start")

    lines = acciones_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert set(entry.keys()) == {"ts", "msg"}
    assert entry["msg"] == "[api] speaking_start"
    assert isinstance(entry["ts"], (int, float))


def test_engine_host_dispatch_motor_event_mirrors_to_acciones(tmp_path, monkeypatch):
    """EngineHost() (constructor only, no start()) registers log_motor_accion
    into _motor_event_handlers -- _dispatch_motor_event alone must produce
    the acciones.jsonl line, with no engine/motor started."""
    acciones_file = tmp_path / "acciones.jsonl"
    monkeypatch.setattr(settings, "ACCIONES_LOG_FILE", str(acciones_file))

    host = engine_host_mod.EngineHost(lock_path=str(tmp_path / "engine.lock"))
    host._dispatch_motor_event("speaking_end")

    lines = acciones_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert set(entry.keys()) == {"ts", "msg"}
    assert "speaking_end" in entry["msg"]


def test_dispatch_motor_event_contains_raising_sink(tmp_path, monkeypatch):
    """A raising handler ahead of log_motor_accion in the router must not
    stop later handlers from running, and must never propagate out of
    _dispatch_motor_event (per-handler exception guard)."""
    acciones_file = tmp_path / "acciones.jsonl"
    monkeypatch.setattr(settings, "ACCIONES_LOG_FILE", str(acciones_file))

    host = engine_host_mod.EngineHost(lock_path=str(tmp_path / "engine.lock"))

    def _boom(_status):
        raise RuntimeError("boom")

    host._motor_event_handlers.insert(0, _boom)
    host._dispatch_motor_event("speaking_start")  # must not raise

    lines = acciones_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1


def test_log_motor_accion_trims_to_5000_lines(tmp_path, monkeypatch):
    """Mirrors advanced_panel._rotate_acciones(max_lines=5000): the file
    never grows past the cap, and the newest line survives the trim."""
    acciones_file = tmp_path / "acciones.jsonl"
    monkeypatch.setattr(settings, "ACCIONES_LOG_FILE", str(acciones_file))
    with open(acciones_file, "w", encoding="utf-8") as f:
        for i in range(5001):
            f.write(json.dumps({"ts": 0.0, "msg": f"seed-{i}"}) + "\n")

    observability.log_motor_accion("speaking_end")

    lines = acciones_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 5000
    assert json.loads(lines[-1])["msg"] == "[api] speaking_end"


def test_log_motor_accion_holds_module_lock_during_append(tmp_path, monkeypatch):
    """log_motor_accion must serialize its append+trim pair on the shared
    _ACCIONES_LOCK -- a concurrent caller must block while another thread
    holds it (the multi-threaded ui_callback race the lock exists for)."""
    acciones_file = tmp_path / "acciones.jsonl"
    monkeypatch.setattr(settings, "ACCIONES_LOG_FILE", str(acciones_file))

    done = threading.Event()

    observability._ACCIONES_LOCK.acquire()
    try:
        t = threading.Thread(
            target=lambda: (observability.log_motor_accion("blocked"), done.set())
        )
        t.start()
        t.join(timeout=0.2)
        assert not done.is_set()  # still waiting on the held lock
    finally:
        observability._ACCIONES_LOCK.release()

    t.join(timeout=2)
    assert done.is_set()
    lines = acciones_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1


# ──────────────────────────────────────────────────────────────────────────
# conftest.py: autouse isolation of settings.LOG_DIR / ACCIONES_LOG_FILE
# ──────────────────────────────────────────────────────────────────────────


def test_autouse_fixture_isolates_log_dir_and_acciones_file(tmp_path):
    """conftest's autouse ``_isolate_api_log_dir`` fixture must repoint
    settings.LOG_DIR/ACCIONES_LOG_FILE away from the real repo-root logs/
    for EVERY test -- not just the ones that opt in with their own
    monkeypatch -- so a real EngineHost/TestClient run in an unrelated test
    (e.g. test_engine_host_agenda_load.py, test_obs_runtime.py) never writes
    into the developer's real E:/VoiceAI/logs/."""
    from opencohost.config import settings as settings_mod

    assert settings_mod.LOG_DIR == str(tmp_path / "logs")
    assert settings_mod.ACCIONES_LOG_FILE == str(tmp_path / "logs" / "acciones.jsonl")
