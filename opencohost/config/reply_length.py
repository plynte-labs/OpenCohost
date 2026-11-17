"""Owner reply-length setting (reply-length-selector-20261009).

One global mode for Kira's owner turns: ``auto`` (a per-turn heuristic picks
the level, see ``opencohost.core.context.reply_length``) or a fixed level
``short | medium | long | very_long``.

Design constraints (same shape as ``guardrail_policy``):
  * The mode lives in memory; ``get_reply_length_mode()`` never touches disk,
    so the turn path pays nothing.
  * Nothing reads disk until ``load_reply_length()`` (API startup), which keeps
    unrelated tests hermetic.
  * A missing, corrupt or invalid file means ``auto``.
  * Writes are atomic (temp file + ``os.replace``); the in-memory mode only
    changes after a successful write.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from typing import Optional

from opencohost.config.logger import get_logger
from opencohost.config.settings import REPLY_LENGTH_FILE

logger = get_logger()

MODES: tuple[str, ...] = ("auto", "short", "medium", "long", "very_long")
DEFAULT_MODE = "auto"

_lock = threading.Lock()
_mode: str = DEFAULT_MODE


def is_valid_mode(value) -> bool:
    return isinstance(value, str) and value in MODES


def _install(mode: str) -> None:
    global _mode
    with _lock:
        _mode = mode


def get_reply_length_mode() -> str:
    """The active mode, in memory only."""
    return _mode


def set_reply_length_mode(mode: str) -> None:
    """Install in memory only (tests, callers that persist elsewhere)."""
    if not is_valid_mode(mode):
        raise ValueError(f"invalid reply length mode: {mode!r}")
    _install(mode)


def reset_reply_length() -> None:
    """Back to the default mode. Does not touch disk."""
    _install(DEFAULT_MODE)


def load_reply_length() -> str:
    """Load the mode from disk into memory. Missing, corrupt or invalid -> auto."""
    path = REPLY_LENGTH_FILE
    mode = DEFAULT_MODE
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            candidate = data.get("mode") if isinstance(data, dict) else None
            if is_valid_mode(candidate):
                mode = candidate
            else:
                logger.warning("[REPLY_LENGTH] invalid file, using %s", DEFAULT_MODE)
        except Exception as e:
            logger.warning("[REPLY_LENGTH] unreadable file (%s), using %s", type(e).__name__, DEFAULT_MODE)
    _install(mode)
    logger.info("[REPLY_LENGTH] setting mode=%s (loaded)", mode)
    return mode


def save_reply_length(mode) -> Optional[str]:
    """Validate, write atomically and install.

    Returns the saved mode, or ``None`` when the write failed (callers map that
    to 503). Raises ``ValueError`` for an invalid mode (callers map that to
    422). The in-memory mode only changes on success.
    """
    if not is_valid_mode(mode):
        raise ValueError(f"invalid reply length mode: {mode!r}")
    path = REPLY_LENGTH_FILE
    target_dir = os.path.dirname(path) or "."
    tmp_path = None
    try:
        os.makedirs(target_dir, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=target_dir, prefix=".reply_length_", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"mode": mode}, f, indent=2)
        os.replace(tmp_path, path)
    except Exception as e:
        logger.warning("Failed to save reply length setting: %s", type(e).__name__)
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        return None
    _install(mode)
    logger.info("[REPLY_LENGTH] setting mode=%s (saved)", mode)
    return mode
