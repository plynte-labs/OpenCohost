# Design: API Observability (C persist+redact, B audit trail, A acciones parity)

Track: `api_observability_20260708` · Scope: `opencohost/api/*` only · Pure add-on: no endpoint behavior changes.

## Problem (proposal)

An API run is a black box beyond the shared engine log. Verified against current code:

- **C**: API loggers live under `opencohost.api.*` (engine_host.py:50, agenda_driver.py:30, auth.py:41) — a different tree from the configured `OpenCohost` logger (config/logger.py:48). The ~10 `_logger.exception` sites (engine_host.py:260/287/394/415/424/432/442/452, agenda_driver.py:61/169) fall through to `logging.lastResort`: stderr-only, WARNING+, unpersisted, **unredacted**.
- **B**: No per-request audit trail. auth_middleware (main.py:695) rejects/allows silently.
- **A**: `acciones.jsonl` is CTK-only (advanced_panel.py:396-409 writes `{"ts": epoch, "msg": str}` to `ACCIONES_LOG_FILE` = `USER_DATA_DIR/logs/acciones.jsonl`, settings.py:277 — same dir as `LOG_DIR`). The API fan-out `engine_host._dispatch_motor_event` (engine_host.py:274-287) has no equivalent sink.

## Technical Approach

One new module `opencohost/api/observability.py` holds all three sinks; `main.py` and `engine_host.py` each gain one wiring line. All paths resolved lazily via `settings.LOG_DIR` / `settings.ACCIONES_LOG_FILE` (mirrors auth.py `_tokens_path()`) so tests can monkeypatch.

## Architecture Decisions

| Decision | Choice | Rejected | Rationale |
|---|---|---|---|
| C attach point | `setup_api_logging()` at top of `lifespan` (main.py:671), before `ensure_tokens()` | `EngineHost.__init__` | Covers auth.py/main.py loggers too; `ensure_tokens()` logs before host exists; tests swap `host_factory` for fakes, which would skip attachment |
| C filter placement | `SensitiveDataFilter` on each **handler** | On the `opencohost.api` logger | stdlib gotcha: ancestor-logger filters are NOT applied during propagation from child loggers; only handler filters run. Logger-level filter would leave everything unredacted |
| C handlers | Named `RotatingFileHandler` (`LOG_DIR/opencohost_api.log`, maxBytes=5 MB, backupCount=3, delay=True) + named redacted `StreamHandler`; reuse `log_formatter` from config/logger.py | Timestamped per-run files like `opencohost_*.log` | Fixed name + rotation = one predictable operator path, bounded disk; console handler replaces lastResort's unredacted stderr; `delay=True` avoids empty files |
| C idempotency | Skip attach when a handler with the reserved name exists on `logging.getLogger("opencohost.api")` | Module-level flag | Survives repeated `create_app()`/TestClient cycles in one process; testable |
| B shape | `audit_middleware` registered between auth (main.py:695) and CORS (main.py:696) — later registration = outer, so it wraps auth and records 401/403/429/503 rejections | Inside auth (innermost) | Rejections are the audit trail's main value; CORS-preflight OPTIONS short-circuited by CORSMiddleware stay unaudited (accepted noise). `call_next` result returned untouched; log write in try/except — behavior-neutral by construction |
| B role | Resolve via auth.py `_bearer_token` + `_resolve_role` only when an Authorization header is present; errors → `null` | Stash role in `request.state` from auth | Zero changes to auth = zero behavior risk; auth doesn't resolve roles on GET anyway |
| B sink | Logger `opencohost.api.audit`, `propagate=False`, `RotatingFileHandler(LOG_DIR/api_audit.jsonl, 5 MB, 3)`, `"%(message)s"` formatter, message = `json.dumps` of whitelist; SensitiveDataFilter added as defense-in-depth | Hand-rolled file writer | Rotation free from stdlib; `propagate=False` keeps JSONL out of opencohost_api.log |
| A file | Same file CTK uses: `settings.ACCIONES_LOG_FILE`, same schema `{"ts", "msg"}`, msg = `f"[api] {status}"` (raw status string), trim-to-5000-lines rotation mirroring `_rotate_acciones` | api-only file; duplicating the ui/motor_event_handlers.py message map | Owner asked for location parity; raw statuses avoid a map to keep in sync; `[api]` prefix disambiguates interleaved CTK lines. Accepted risk: simultaneous CTK+API runs can race the trim (unsupported config) |
| A wiring | Append `log_motor_accion` to `self._motor_event_handlers` in `EngineHost.__init__` | Inline write in `_dispatch_motor_event` | Reuses the existing per-handler exception guard. One-line local append on the engine thread is acceptable (CTK does the same synchronously); relax the docstring to "no slow/network I/O" |
| uvicorn access log | **Deferred** | `--log-config` in run-api.bat | B records method/path/status/duration for every request — supersedes it |

## Privacy Whitelist (B) — exact and closed

`ts, method, path (no query string), status, duration_ms, role, idempotency_key`. NEVER: request/response bodies, Authorization value, query-string content, chat/dialogue.

## File Changes

| File | Action |
|---|---|
| `opencohost/api/observability.py` | Create — `setup_api_logging()`, `audit_middleware()`, `log_motor_accion()` |
| `opencohost/api/main.py` | Modify — lifespan calls setup; register audit middleware after auth |
| `opencohost/api/engine_host.py` | Modify — register acciones handler in `__init__` |
| `tests/test_api_observability.py` | Create — grown per work unit |

## Testing Strategy (Strict TDD, RED first)

Runner: `E:/Miniconda/envs/flux_env/python.exe -m pytest -p no:cacheprovider --basetemp=E:/VoiceAI/temp/pytest-piper-clean`

| Gap | Assertions |
|---|---|
| C | Double `setup_api_logging()` → exactly one named file handler. Log `"Bearer abc123..."` via `logging.getLogger("opencohost.api.engine_host")` → file contains `<redacted>`, not the token (proves handler-level filter through propagation) |
| B | TestClient POST with sentinel body + Authorization + `?secret=1` → JSONL line has whitelist keys only; sentinel and token absent; path has no query; valid operator token → `role: "operator"`; missing agent-surface token → audited 401 line |
| A | `EngineHost()` (no `start()`), `_dispatch_motor_event("speaking_start")` with monkeypatched `ACCIONES_LOG_FILE` → one `{"ts", "msg"}` line containing `speaking_start` |
| Neutrality | Existing API suites pass unchanged; audit test asserts response body/status identical with middleware active |

## Migration / Open Questions

No migration. No open questions — uvicorn access-log persistence deferred with justification above.
