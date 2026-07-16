"""Persist and redact the ``opencohost.api`` logger tree (WU-C); per-request
audit trail (WU-B).

The API's own loggers (``opencohost.api``, ``opencohost.api.engine_host``,
``opencohost.api.agenda_driver``, ``opencohost.api.auth``, ...) live under a
DIFFERENT tree from the ``OpenCohost`` engine logger that
opencohost/config/logger.py configures. With no handler attached to
``opencohost.api``, any record logged under that tree (e.g. the
``_logger.exception(...)`` sites in engine_host.py / agenda_driver.py) falls
through to ``logging.lastResort``: stderr-only, WARNING+, unpersisted, and
UNREDACTED.

``setup_api_logging()`` attaches a rotating file handler + a redacted stream
handler directly to ``logging.getLogger("opencohost.api")``. Every child
logger under that tree propagates its records up to these handlers by
default stdlib logging behavior, so ONE attachment covers the whole tree.

Filtering is applied at the HANDLER level (not the logger level): a filter
added to an ancestor LOGGER is only evaluated for records that logger itself
emits, not for records propagated up from a child logger. A filter added to
a HANDLER runs for every record that reaches that handler, regardless of
which logger produced it -- so redaction MUST live on the handlers here for
messages logged from child loggers (engine_host, agenda_driver, auth) to be
redacted too.

``audit_middleware`` (WU-B) records one metadata-only JSONL line per request
(ts, method, path, status, duration_ms, role, idempotency_key). It is
registered in main.py AFTER auth_middleware and BEFORE CORSMiddleware --
later registration = outer in Starlette's stack -- so it wraps auth and
audits its own 401/403/429/503 rejections too. Never the request/response
body, never the Authorization value, never query-string content.

``log_motor_accion`` (WU-A) mirrors a motor status event into the SAME
``acciones.jsonl`` file CTK's ``ui/advanced_panel.py::_guardar_accion``
writes (``settings.ACCIONES_LOG_FILE``), using the identical schema
(``{"ts", "msg"}``) and trim-to-5000-lines rotation, so operators find one
file regardless of which surface produced the line. Registered into
``EngineHost._motor_event_handlers`` in ``EngineHost.__init__`` -- it
deliberately has NO internal try/except, reusing the per-handler exception
guard ``_dispatch_motor_event`` already applies to every registered handler.
"""

import json
import logging
import logging.handlers
import os
import threading
import time

from opencohost.api.auth import TokenFileError, _bearer_token, _resolve_role
from opencohost.config import settings
from opencohost.config.logger import SensitiveDataFilter, log_formatter

_API_LOGGER_NAME = "opencohost.api"
_FILE_HANDLER_NAME = "opencohost-api-file"
_STREAM_HANDLER_NAME = "opencohost-api-stream"

_FILE_MAX_BYTES = 5 * 1024 * 1024
_FILE_BACKUP_COUNT = 3

_AUDIT_LOGGER_NAME = "opencohost.api.audit"
_AUDIT_FILE_HANDLER_NAME = "opencohost-api-audit-file"
_AUDIT_MAX_BYTES = 5 * 1024 * 1024
_AUDIT_BACKUP_COUNT = 3
_audit_json_formatter = logging.Formatter("%(message)s")

_ACCIONES_MAX_LINES = 5000
# Guards the append+trim pair in log_motor_accion below -- see its docstring.
_ACCIONES_LOCK = threading.Lock()


def setup_api_logging() -> None:
    """Idempotently attach persistence + redaction to the API logger tree.

    Safe to call on every ``create_app()``/lifespan entry (including
    repeated TestClient cycles in one process): skips attachment when a
    handler with the reserved file-handler name is already present.
    """
    logger = logging.getLogger(_API_LOGGER_NAME)
    logger.setLevel(logging.INFO)  # INFO lifecycle lines must reach the handlers
    if any(getattr(h, "name", None) == _FILE_HANDLER_NAME for h in logger.handlers):
        return

    os.makedirs(settings.LOG_DIR, exist_ok=True)

    file_handler = logging.handlers.RotatingFileHandler(
        os.path.join(settings.LOG_DIR, "opencohost_api.log"),
        maxBytes=_FILE_MAX_BYTES,
        backupCount=_FILE_BACKUP_COUNT,
        delay=True,
        encoding="utf-8",
    )
    file_handler.name = _FILE_HANDLER_NAME
    file_handler.setFormatter(log_formatter)
    file_handler.addFilter(SensitiveDataFilter())

    stream_handler = logging.StreamHandler()
    stream_handler.name = _STREAM_HANDLER_NAME
    stream_handler.setFormatter(log_formatter)
    stream_handler.addFilter(SensitiveDataFilter())

    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)


def _setup_audit_logging() -> logging.Logger:
    """Idempotently attach the audit JSONL sink to ``opencohost.api.audit``.

    ``propagate=False`` keeps audit lines out of ``opencohost_api.log`` (the
    handlers ``setup_api_logging()`` attaches above) -- this is its own
    dedicated file. Called from ``audit_middleware`` on every request; the
    named-handler guard makes repeat calls a cheap no-op.
    """
    logger = logging.getLogger(_AUDIT_LOGGER_NAME)
    logger.propagate = False
    logger.setLevel(logging.INFO)
    if any(getattr(h, "name", None) == _AUDIT_FILE_HANDLER_NAME for h in logger.handlers):
        return logger

    os.makedirs(settings.LOG_DIR, exist_ok=True)

    handler = logging.handlers.RotatingFileHandler(
        os.path.join(settings.LOG_DIR, "api_audit.jsonl"),
        maxBytes=_AUDIT_MAX_BYTES,
        backupCount=_AUDIT_BACKUP_COUNT,
        delay=True,
        encoding="utf-8",
    )
    handler.name = _AUDIT_FILE_HANDLER_NAME
    handler.setFormatter(_audit_json_formatter)
    # Defense-in-depth: the whitelist below never puts a body/token/query in
    # the message, but a redaction pass costs nothing extra here.
    handler.addFilter(SensitiveDataFilter())
    logger.addHandler(handler)
    return logger


def _audit_role(request) -> "str | None":
    """Best-effort role for the audit line ONLY -- never gates the request
    (auth_middleware alone decides accept/reject). Resolved only when an
    Authorization header is present; any resolution error (invalid token,
    unreadable token file) reads as None -- mirrors main.py's
    ``_agent_request_role`` fail-closed-to-None pattern.
    """
    token = _bearer_token(request)
    if token is None:
        return None
    try:
        return _resolve_role(token)
    except TokenFileError:
        return None


async def audit_middleware(request, call_next):
    """Per-request audit trail (WU-B): one JSONL line, metadata only.

    Registered to wrap auth_middleware (see module docstring), so auth's own
    401/403/429/503 rejections are audited too. ``call_next`` itself is now
    wrapped in a try/except: an unhandled route exception is the request
    most worth auditing, so it is logged with status 500 and always
    re-raised unchanged -- response behavior stays byte-identical, only the
    audit write is best-effort (``_emit_audit_line`` never raises).
    """
    start = time.monotonic()
    try:
        response = await call_next(request)
    except Exception:
        _emit_audit_line(request, start, 500)
        raise
    _emit_audit_line(request, start, response.status_code)
    return response


def _emit_audit_line(request, start: float, status_code: int) -> None:
    """Best-effort JSONL audit write -- never raises, so a logging failure
    (or the act of auditing a 500) can never turn into (or mask) a 500."""
    try:
        logger = _setup_audit_logging()
        logger.info(
            json.dumps(
                {
                    "ts": time.time(),
                    "method": request.method,
                    "path": request.url.path,
                    "status": status_code,
                    "duration_ms": round((time.monotonic() - start) * 1000, 3),
                    "role": _audit_role(request),
                    "idempotency_key": request.headers.get("Idempotency-Key"),
                }
            )
        )
    except Exception:
        pass


def _rotate_acciones(acciones_file: str, max_lines: int = _ACCIONES_MAX_LINES) -> None:
    """Keep only the last ``max_lines`` entries -- mirrors
    ``advanced_panel.py::_rotate_acciones`` (CTK parity, same trim
    behavior). No try/except here: see ``log_motor_accion`` docstring.
    """
    with open(acciones_file, "r", encoding="utf-8") as f:
        lines = f.readlines()
    if len(lines) > max_lines:
        with open(acciones_file, "w", encoding="utf-8") as f:
            f.writelines(lines[-max_lines:])


def log_motor_accion(status) -> None:
    """Append a motor status event to ``acciones.jsonl`` for CTK parity (WU-A).

    Writes the exact CTK schema (``{"ts": time.time(), "msg": ...}``) to
    ``settings.ACCIONES_LOG_FILE`` -- resolved lazily (read at call time, not
    import time) so tests can monkeypatch it, mirroring auth.py's
    ``_tokens_path()`` pattern. ``msg`` carries the raw status string with an
    ``"[api] "`` prefix (no ui/motor_event_handlers.py message map -- this
    module must never import ``opencohost.ui``) so interleaved CTK/API lines
    stay disambiguated in one shared file.

    ``_ACCIONES_LOCK`` serializes the append+trim pair: unlike CTK (where the
    motor-event dispatcher funnels onto the Tk main thread), the API path is
    invoked directly from multiple threads (the engine turn loop, the model
    download worker, the ollama-wake thread), so two concurrent calls could
    otherwise interleave a read-modify-write in ``_rotate_acciones`` and lose
    a line, or tear a line on Windows. This lock only covers concurrent
    writers WITHIN this process -- a simultaneous CTK process writing the
    same file is still a known, accepted cross-process race (the owner's ask
    was file-location parity, not cross-process coordination).

    Deliberately has NO try/except: this is registered into
    ``EngineHost._motor_event_handlers`` and the caller,
    ``EngineHost._dispatch_motor_event``, already wraps every handler in its
    own per-handler exception guard -- a second guard here would be dead code.
    """
    acciones_file = settings.ACCIONES_LOG_FILE
    os.makedirs(os.path.dirname(acciones_file), exist_ok=True)
    with _ACCIONES_LOCK:
        with open(acciones_file, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": time.time(), "msg": f"[api] {status}"}, ensure_ascii=False) + "\n")
        _rotate_acciones(acciones_file)
