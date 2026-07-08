"""Global (profile-independent) streamer personalization store.

Design: sdd/kira-personalization-onboarding-20260705 (design.md §1/§2).
Mirrors opencohost/core/profiles.py's atomic-write pattern (mkstemp +
os.replace, fail-open, never raises, logs path + exception type only —
never field content, per CLAUDE.md).

Unlike per-profile Kira personas, this store is global: streamer identity
(nickname/occupation/interests/custom instructions) is invariant across
Kira personas, so there is exactly one file, not one per profile.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from typing import Callable

from opencohost.config.settings import (
    PERSONALIZATION_FILE,
    PERSONALIZATION_MAX_INJECT_CHARS,
    PERSONALIZATION_NICKNAME_MAX,
    PERSONALIZATION_OCCUPATION_MAX,
    PERSONALIZATION_INTERESTS_MAX,
    PERSONALIZATION_INSTRUCTIONS_MAX,
)
from opencohost.config.logger import get_logger
from opencohost.i18n import active as i18n_active

logger = get_logger()

# label is deliberately "style" for custom_instructions (design §2: locale-
# neutral data labels, not scaffolding) — order also fixes the on-wire order.
_FIELD_LABELS = (
    ("nickname", "nickname"),
    ("occupation", "occupation"),
    ("interests", "interests"),
    ("custom_instructions", "style"),
)

# mtime cache — per-turn engine reads are usually zero-I/O (design §1).
_cache_state: dict = {"path": None, "mtime": None, "data": None}


def _defaults() -> dict:
    return {
        "version": 1,
        "enabled": True,
        "nickname": "",
        "occupation": "",
        "interests": "",
        "custom_instructions": "",
        "updated_at": None,
    }


def _clamp(data: dict) -> dict:
    """Clamp every field to its configured cap — defends hand-edited files."""
    clamped = dict(data)
    clamped["nickname"] = str(clamped.get("nickname") or "")[:PERSONALIZATION_NICKNAME_MAX]
    clamped["occupation"] = str(clamped.get("occupation") or "")[:PERSONALIZATION_OCCUPATION_MAX]
    clamped["interests"] = str(clamped.get("interests") or "")[:PERSONALIZATION_INTERESTS_MAX]
    clamped["custom_instructions"] = str(clamped.get("custom_instructions") or "")[
        :PERSONALIZATION_INSTRUCTIONS_MAX
    ]
    clamped["enabled"] = bool(clamped.get("enabled", True))
    clamped["version"] = 1
    # A hand-edited file may carry a non-string `updated_at` (e.g. a bare
    # int) — valid JSON, wrong type. Normalize to str-or-None here so it
    # never reaches PersonalizationResponse (typed Optional[str]) raw and
    # trips a ValidationError -> 500 (design §9: fail-open on hand-edited
    # files, not raise).
    updated_at = clamped.get("updated_at")
    clamped["updated_at"] = str(updated_at) if updated_at is not None else None
    return clamped


def load_personalization() -> dict:
    """Load the personalization store. Fail-open to defaults on any error —
    absent file, corrupt JSON, or a non-dict payload never raises.

    Cached on the file's mtime: repeated calls within the same turn (or
    across turns with no intervening save) never re-hit disk.
    """
    path = PERSONALIZATION_FILE
    if not os.path.exists(path):
        _cache_state.update(path=path, mtime=None, data=None)
        return _defaults()

    try:
        mtime = os.stat(path).st_mtime
    except OSError as e:
        logger.warning(f"Could not stat personalization file {path}: {type(e).__name__}")
        return _defaults()

    if (
        _cache_state["path"] == path
        and _cache_state["mtime"] == mtime
        and _cache_state["data"] is not None
    ):
        return dict(_cache_state["data"])

    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if not isinstance(raw, dict):
            raise ValueError("personalization file did not contain a JSON object")
        data = _clamp({**_defaults(), **raw})
    except Exception as e:
        logger.warning(f"Could not load personalization from {path}: {type(e).__name__}")
        return _defaults()

    _cache_state.update(path=path, mtime=mtime, data=data)
    return dict(data)


def save_personalization(data: dict) -> bool:
    """Write the personalization store atomically. Never raises; returns
    False on failure so callers (the API handlers) can surface a 503 instead
    of a false 200. Never logs field content — only the path and exception
    type (CLAUDE.md: never expose raw chat/profile content in logs).
    """
    path = PERSONALIZATION_FILE
    target_dir = os.path.dirname(path) or "."
    payload = _clamp({**_defaults(), **data})
    payload["updated_at"] = datetime.now(timezone.utc).isoformat()

    tmp_path = None
    try:
        os.makedirs(target_dir, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=target_dir, prefix=".personalization_", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=4, ensure_ascii=False)
        os.replace(tmp_path, path)
    except Exception as e:
        logger.warning(f"Failed to save personalization to {path}: {type(e).__name__}")
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        return False

    try:
        mtime = os.stat(path).st_mtime
    except OSError:
        mtime = None
    _cache_state.update(path=path, mtime=mtime, data=payload)
    return True


def clear_personalization() -> bool:
    """Reset the store to defaults (purge action)."""
    return save_personalization(_defaults())


def build_injection_block(sanitize_fn: Callable[[str], str]) -> str:
    """Build the read-only <perfil_streamer> block, or "" when disabled/empty.

    Each non-empty field is sanitized independently (data-not-instructions,
    same treatment as memorias lines) before being wrapped. Any exception
    anywhere in the build (load, sanitize, or assembly) fails the whole
    block open to "" — mirrors _build_memorias_injection_block, so a
    sanitizer failure never lets a raw, unsanitized field value reach the
    prompt.

    Applies a hard-truncation backstop at PERSONALIZATION_MAX_INJECT_CHARS:
    the field caps alone (60+120+240+400) do not account for real
    label/delimiter overhead (149 chars), so worst case at max caps is 969
    chars. The backstop truncates the joined field lines — never the
    closing tag — so the read-only wrapper is always syntactically closed.
    """
    try:
        data = load_personalization()
        if not data.get("enabled", True):
            return ""

        lines = []
        for field, label in _FIELD_LABELS:
            value = (data.get(field) or "").strip()
            if not value:
                continue
            safe_value = sanitize_fn(value)
            if not safe_value:
                continue
            lines.append(f"{label}: {safe_value}")

        if not lines:
            return ""

        open_tag = i18n_active.personalization_block_open()
        close_tag = i18n_active.personalization_block_close()
        joined = "\n".join(lines)

        # 2 accounts for the newline after open_tag and the newline before
        # close_tag.
        available = PERSONALIZATION_MAX_INJECT_CHARS - len(open_tag) - len(close_tag) - 2
        joined = joined[: max(available, 0)]

        return open_tag + "\n" + joined + "\n" + close_tag
    except Exception as e:
        logger.warning(f"Failed to build personalization injection block: {type(e).__name__}")
        return ""
