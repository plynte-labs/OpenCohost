"""Owner decision-layer settings (laya-decision-layer-20261009).

Per decision (``vision | memory | reply_length | respond``): a mode
``off | shadow | on`` and the confidence threshold Laya must clear to act in
``on``. ``respond`` (viewer chat) is shadow-only in this branch.

Same shape as ``reply_length``:
  * The settings live in memory; the getters never touch disk, so the turn
    path pays nothing.
  * Nothing reads disk until ``load_decisions()`` (API startup), which keeps
    unrelated tests hermetic.
  * The env (``OPENCOHOST_DECISION_*``, resolved in settings.py) gives the
    defaults; the persisted file wins per field. A missing, corrupt or invalid
    file or field falls back to the default.
  * Writes are atomic (temp file + ``os.replace``) and always carry the full
    state; the in-memory state only changes after a successful write.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from typing import Optional

from opencohost.config import settings
from opencohost.config.logger import get_logger
from opencohost.config.settings import DECISIONS_FILE

logger = get_logger()

DECISION_IDS: tuple[str, ...] = settings.DECISION_IDS
MODES: tuple[str, ...] = settings.DECISION_MODES
SHADOW_ONLY = settings.DECISION_SHADOW_ONLY
THRESHOLD_MIN = settings.DECISION_THRESHOLD_MIN
THRESHOLD_MAX = settings.DECISION_THRESHOLD_MAX
_FIELDS = frozenset({"mode", "threshold"})

_lock = threading.Lock()
_state: dict[str, dict] = {}


def is_valid_mode(decision_id, mode) -> bool:
    """A known mode; ``on`` is rejected for shadow-only decisions."""
    if not isinstance(mode, str) or mode not in MODES:
        return False
    return not (mode == "on" and decision_id in SHADOW_ONLY)


def is_valid_threshold(value) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and THRESHOLD_MIN <= float(value) <= THRESHOLD_MAX
    )


def _defaults() -> dict[str, dict]:
    base = settings.DECISION_DEFAULTS["decisions"]
    return {d: {"mode": base[d]["mode"], "threshold": float(base[d]["threshold"])} for d in DECISION_IDS}


def _install(state: dict[str, dict]) -> None:
    global _state
    with _lock:
        _state = {d: dict(state[d]) for d in DECISION_IDS}


def _install_entry(decision_id: str, mode: str, threshold: float) -> None:
    """Install one entry without validation (tests, hand-edited files)."""
    with _lock:
        _state[decision_id] = {"mode": mode, "threshold": float(threshold)}


def get_decision_settings() -> dict[str, dict]:
    """A copy of ``{id: {mode, threshold}}``, in memory only."""
    with _lock:
        return {d: dict(entry) for d, entry in _state.items()}


def get_mode(decision_id: str) -> str:
    with _lock:
        entry = _state.get(decision_id)
        return entry["mode"] if entry else "off"


def get_threshold(decision_id: str) -> float:
    with _lock:
        entry = _state.get(decision_id)
        return entry["threshold"] if entry else THRESHOLD_MAX


def set_decision(decision_id: str, *, mode: Optional[str] = None, threshold=None) -> None:
    """Install one decision in memory only (tests, callers that persist elsewhere)."""
    merged = _merge(get_decision_settings(), {decision_id: _partial(mode, threshold)})
    _install(merged)


def reset_decisions() -> None:
    """Back to the env defaults. Does not touch disk."""
    _install(_defaults())


def _partial(mode, threshold) -> dict:
    entry = {}
    if mode is not None:
        entry["mode"] = mode
    if threshold is not None:
        entry["threshold"] = threshold
    return entry


def _merge(current: dict[str, dict], update) -> dict[str, dict]:
    """Validate a partial ``{id: {mode?, threshold?}}`` and merge it onto
    ``current``. Raises ``ValueError`` on anything invalid."""
    if not isinstance(update, dict):
        raise ValueError("decisions update must be an object")
    merged = {d: dict(entry) for d, entry in current.items()}
    for decision_id, entry in update.items():
        if decision_id not in DECISION_IDS:
            raise ValueError(f"unknown decision: {decision_id!r}")
        if not isinstance(entry, dict) or not set(entry) <= _FIELDS:
            raise ValueError(f"invalid decision entry: {decision_id!r}")
        if "mode" in entry:
            if not is_valid_mode(decision_id, entry["mode"]):
                raise ValueError(f"invalid mode for {decision_id!r}")
            merged[decision_id]["mode"] = entry["mode"]
        if "threshold" in entry:
            if not is_valid_threshold(entry["threshold"]):
                raise ValueError(f"invalid threshold for {decision_id!r}")
            merged[decision_id]["threshold"] = float(entry["threshold"])
    return merged


def _overlay_file(state: dict[str, dict], data) -> int:
    """Apply each valid field of a loaded file onto ``state``; returns the
    number of invalid fields skipped."""
    entries = data.get("decisions") if isinstance(data, dict) else None
    if not isinstance(entries, dict):
        raise ValueError("decisions file has no decisions object")
    skipped = 0
    for decision_id, entry in entries.items():
        if decision_id not in DECISION_IDS or not isinstance(entry, dict):
            skipped += 1
            continue
        mode = entry.get("mode")
        if mode is not None:
            if is_valid_mode(decision_id, mode):
                state[decision_id]["mode"] = mode
            else:
                skipped += 1
        threshold = entry.get("threshold")
        if threshold is not None:
            if is_valid_threshold(threshold):
                state[decision_id]["threshold"] = float(threshold)
            else:
                skipped += 1
    return skipped


def _summary(state: dict[str, dict]) -> str:
    return " ".join(f"{d}={state[d]['mode']}@{state[d]['threshold']:.2f}" for d in DECISION_IDS)


def load_decisions() -> dict[str, dict]:
    """Load the file over the env defaults. Missing or corrupt -> defaults;
    invalid fields fall back one by one."""
    path = DECISIONS_FILE
    state = _defaults()
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            loaded = _defaults()
            skipped = _overlay_file(loaded, data)
            state = loaded
            if skipped:
                logger.warning("[DECISION] settings file had %d invalid field(s), defaults used", skipped)
        except Exception as e:
            logger.warning("[DECISION] unreadable settings file (%s), using defaults", type(e).__name__)
    _install(state)
    logger.info("[DECISION] settings %s (loaded)", _summary(state))
    return get_decision_settings()


def save_decisions(update) -> Optional[dict[str, dict]]:
    """Validate a partial update, write the full state atomically and install.

    Returns the saved state, or ``None`` when the write failed (callers map
    that to 503). Raises ``ValueError`` for an invalid update (callers map that
    to 422). The in-memory state only changes on success.
    """
    merged = _merge(get_decision_settings(), update)
    path = DECISIONS_FILE
    target_dir = os.path.dirname(path) or "."
    tmp_path = None
    try:
        os.makedirs(target_dir, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=target_dir, prefix=".decisions_", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"decisions": merged}, f, indent=2)
        os.replace(tmp_path, path)
    except Exception as e:
        logger.warning("Failed to save decision settings: %s", type(e).__name__)
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        return None
    _install(merged)
    logger.info("[DECISION] settings %s (saved)", _summary(merged))
    return get_decision_settings()


reset_decisions()
