"""Persisted self-knowledge settings: two toggles and the identity card.

``{"enabled": bool, "viewers": bool, "creator": str, "repository": str,
"description": str}``. Read live per turn through an (mtime, size) cache so the
common case is one ``stat``; a missing, corrupt or wrongly typed file means the
defaults (master ON, viewers OFF, empty identity -> built-in defaults).
Strings are clamped and flattened to one line because they reach the prompt.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from dataclasses import dataclass

from opencohost.config.logger import get_logger
from opencohost.config.settings import SELF_KNOWLEDGE_FILE

logger = get_logger()

CREATOR_MAX = 80
REPOSITORY_MAX = 200
DESCRIPTION_MAX = 400

_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")
# http(s) only, and no ``user:pass@`` credentials in the authority part.
_REPO_URL = re.compile(r"^https?://[^/@\s]+(?:/\S*)?$", re.IGNORECASE)
_lock = threading.Lock()
_cache: dict = {"key": None, "value": None}


@dataclass(frozen=True)
class SelfKnowledgeSettings:
    enabled: bool = True
    viewers: bool = False
    creator: str = ""
    repository: str = ""
    description: str = ""


def clean_text(value, limit: int) -> str:
    """One line, no control characters, trimmed and clamped. Non-str -> ""."""
    if not isinstance(value, str):
        return ""
    return _CONTROL.sub(" ", value).strip()[:limit].strip()


def _parse(raw) -> SelfKnowledgeSettings:
    if not isinstance(raw, dict):
        return SelfKnowledgeSettings()
    enabled = raw.get("enabled")
    viewers = raw.get("viewers")
    return SelfKnowledgeSettings(
        enabled=enabled if isinstance(enabled, bool) else True,
        viewers=viewers if isinstance(viewers, bool) else False,
        creator=clean_text(raw.get("creator"), CREATOR_MAX),
        repository=clean_text(raw.get("repository"), REPOSITORY_MAX),
        description=clean_text(raw.get("description"), DESCRIPTION_MAX),
    )


def reset_cache() -> None:
    with _lock:
        _cache["key"] = None
        _cache["value"] = None


def load_settings() -> SelfKnowledgeSettings:
    """Current settings; never raises."""
    path = SELF_KNOWLEDGE_FILE
    try:
        st = os.stat(path)
        key = (path, st.st_mtime_ns, st.st_size)
    except OSError:
        key = (path, None, None)
    with _lock:
        if _cache["key"] == key:
            return _cache["value"]
    value = SelfKnowledgeSettings()
    if key[1] is not None:
        try:
            with open(path, "r", encoding="utf-8") as f:
                value = _parse(json.load(f))
        except Exception as e:
            logger.warning("Could not load self-knowledge settings: %s", type(e).__name__)
            value = SelfKnowledgeSettings()
    with _lock:
        _cache["key"] = key
        _cache["value"] = value
    return value


_BOOL_FIELDS = ("enabled", "viewers")
_TEXT_LIMITS = {
    "creator": CREATOR_MAX,
    "repository": REPOSITORY_MAX,
    "description": DESCRIPTION_MAX,
}


def validate_update(raw) -> tuple[dict, list[dict]]:
    """Validate a partial update. Returns ``(clean_fields, errors)``; errors are
    ``{"field", "code": "unknown" | "invalid" | "too_long"}``. Text fields must
    be single-line strings (empty resets to the built-in default); the
    repository must be an ``http(s)`` URL without embedded credentials."""
    if not isinstance(raw, dict):
        return {}, [{"field": "", "code": "invalid"}]
    clean: dict = {}
    errors: list[dict] = []
    for key, value in raw.items():
        if key in _BOOL_FIELDS:
            if isinstance(value, bool):
                clean[key] = value
            else:
                errors.append({"field": key, "code": "invalid"})
        elif key in _TEXT_LIMITS:
            if not isinstance(value, str) or _CONTROL.search(value):
                errors.append({"field": key, "code": "invalid"})
            elif len(value.strip()) > _TEXT_LIMITS[key]:
                errors.append({"field": key, "code": "too_long"})
            elif key == "repository" and value.strip() and not _REPO_URL.match(value.strip()):
                errors.append({"field": key, "code": "invalid"})
            else:
                clean[key] = value.strip()
        else:
            errors.append({"field": str(key), "code": "unknown"})
    return clean, errors


def save_settings(raw):
    """Validate, merge into the current settings and write atomically.

    Returns ``[]`` on success, the error list on validation failure, or ``None``
    when the write failed (callers map that to 503). Nothing changes unless the
    write succeeds.
    """
    clean, errors = validate_update(raw)
    if errors:
        return errors
    current = load_settings()
    merged = {
        "enabled": current.enabled,
        "viewers": current.viewers,
        "creator": current.creator,
        "repository": current.repository,
        "description": current.description,
        **clean,
    }
    path = SELF_KNOWLEDGE_FILE
    target_dir = os.path.dirname(path) or "."
    tmp_path = None
    try:
        os.makedirs(target_dir, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=target_dir, prefix=".self_knowledge_", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(merged, f, indent=2)
        os.replace(tmp_path, path)
    except Exception as e:
        logger.warning("Failed to save self-knowledge settings: %s", type(e).__name__)
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        return None
    reset_cache()
    logger.info(
        "[SELF_KNOWLEDGE] settings saved enabled=%s viewers=%s", merged["enabled"], merged["viewers"]
    )
    return []
