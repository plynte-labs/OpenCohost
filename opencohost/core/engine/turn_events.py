"""One structured JSONL event per foreground turn (turn-events-20261112 T2).

``turn_state.close_turn`` hands the closed turn's accumulator to ``write``,
which appends exactly one JSON line to ``<LOG_DIR>/turn_events.jsonl`` (size
bounded, rotated). The line is built ONLY from the allowlist below: a fixed key
set whose values are integers, booleans or short code strings matching a narrow
pattern. Unknown keys and values of the wrong shape are dropped, so prompt,
chat, reply and provider free text can never reach the file.

Everything here is exception-safe: a failure to build or write an event is
swallowed (debug log) and never breaks or noticeably delays a turn.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import re
import threading
from typing import Any, Callable, Dict, Optional

from opencohost.config import settings

logger = logging.getLogger("OpenCohost")

FILE_NAME = "turn_events.jsonl"
MAX_BYTES = 5 * 1024 * 1024
BACKUP_COUNT = 3
SCHEMA_VERSION = 1

CODE_RE = re.compile(r"^[a-z0-9_.:\-]{1,64}$")
TID_RE = re.compile(r"^t[0-9a-f]{8}$")
# A model id may carry a registry path (``org/name``) and a tag (``:8b``).
MODEL_RE = re.compile(r"^[A-Za-z0-9_.:/\-]{1,100}$")
EXC_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")

# The only timing keys an event may carry (whole milliseconds).
TIMING_KEYS = (
    "engine_ms", "queue_wait_ms", "total_ms",
    "recall_ms", "web_ms", "prompt_build_ms", "llm_ms", "ttfa_ms",
)
_MAX_LIST = 32

# Failure reason codes (turn-events T3): the ONLY values ``reasons[].reason`` may take.
REASON_CODES = frozenset({
    "engine_error",            # _generar_dialogo catch-all
    "watchdog_timeout",        # inference stalled (local or cloud)
    "ollama_transport_error",  # local transport failure
    "cloud_bad_key",           # cloud transport failure, by class
    "cloud_rate_limited",
    "cloud_transient",
    "cloud_error",
    "stream_error",            # streaming attempt raised
    "stream_seal_failed",      # partial-exit seal raised
    "guard_retry_failed",      # guardrail retry generation raised
    "agenda_transform_failed", # agenda output transformer raised
    "dialogue_callback_failed",
    "tts_submit_failed",       # router-off inline speech raised
    "turn_aborted",            # exception escaped the turn (closed as failed)
    "turn_unclosed",           # turn left open without an exception
})


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _code(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and CODE_RE.match(value) else None


def _tid(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and TID_RE.match(value) else None


def _model(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and MODEL_RE.match(value) else None


def _count(value: Any) -> Optional[int]:
    return value if _is_int(value) else None


def _flag(value: Any) -> Optional[bool]:
    return value if isinstance(value, bool) else None


def _code_list(value: Any) -> Optional[list]:
    if not isinstance(value, (list, tuple)):
        return None
    out = [item for item in value if _code(item)]
    return out[:_MAX_LIST]


def _counts(value: Any) -> Optional[dict]:
    if not isinstance(value, dict):
        return None
    return {k: v for k, v in value.items() if _code(k) and _is_int(v)}


def _timings(value: Any) -> Optional[dict]:
    if not isinstance(value, dict):
        return None
    return {k: value[k] for k in TIMING_KEYS if k in value and _is_int(value[k])}


def _reasons(value: Any) -> Optional[list]:
    if not isinstance(value, (list, tuple)):
        return None
    out = []
    for item in value:
        if not isinstance(item, dict):
            continue
        reason, exc = item.get("reason"), item.get("exc")
        if reason in REASON_CODES and isinstance(exc, str) and EXC_RE.match(exc):
            out.append({"reason": reason, "exc": exc})
    return out[:_MAX_LIST]


# key -> validator. A key not in this table is never written.
ALLOWLIST: Dict[str, Callable[[Any], Any]] = {
    "v": _count,
    "ts": _count,
    "tid": _tid,
    "source": _code,
    "model": _model,
    "outcome": _code,
    "latency_outcome": _code,
    "path": _code,
    "phases": _code_list,
    "events": _counts,
    "guards": _code_list,
    "attempts": _count,
    "cloud_retry": _flag,
    "timings": _timings,
    "reasons": _reasons,
    "speech": _code,
}


def sanitize(raw: Any) -> Dict[str, Any]:
    """The allowlisted subset of ``raw``; unknown keys and bad values dropped."""
    if not isinstance(raw, dict):
        return {}
    event: Dict[str, Any] = {}
    for key, check in ALLOWLIST.items():
        if key not in raw:
            continue
        try:
            value = check(raw[key])
        except Exception:
            value = None
        if value is not None:
            event[key] = value
    return event


_lock = threading.Lock()
_sink: Optional[logging.Logger] = None
_sink_path: Optional[str] = None


def _get_sink(path: str) -> logging.Logger:
    """The dedicated rotating-file logger for ``path`` (rebuilt if the log dir moved)."""
    global _sink, _sink_path
    with _lock:
        if _sink is not None and _sink_path == path:
            return _sink
        sink = logging.getLogger("opencohost.turn_events")
        for handler in list(sink.handlers):
            sink.removeHandler(handler)
            handler.close()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            path, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, delay=True, encoding="utf-8"
        )
        handler.setFormatter(logging.Formatter("%(message)s"))
        sink.addHandler(handler)
        sink.setLevel(logging.INFO)
        sink.propagate = False  # never into the main log
        _sink, _sink_path = sink, path
        return sink


def write(raw: Any) -> bool:
    """Append one sanitized event line. Never raises; False when nothing written."""
    try:
        event = sanitize(raw)
        if not event.get("tid"):
            return False
        event["v"] = SCHEMA_VERSION
        path = os.path.join(str(settings.LOG_DIR), FILE_NAME)
        line = json.dumps(event, ensure_ascii=True, separators=(",", ":"), sort_keys=False)
        _get_sink(path).info(line)
        return True
    except Exception:
        logger.debug("[TURN_EVENT] write failed", exc_info=True)
        return False
