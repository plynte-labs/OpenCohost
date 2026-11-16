"""Global (profile-independent) streamer personalization store.

Design: sdd/kira-personalization-onboarding-20260705 (design.md §1/§2).
Mirrors opencohost/core/profiles/profiles.py's atomic-write pattern (mkstemp +
os.replace, fail-open, never raises, logs path + exception type only —
never field content, per CLAUDE.md).

Unlike per-profile Kira personas, this store is global: streamer identity
(nickname/occupation/interests/custom instructions) is invariant across
Kira personas, so there is exactly one file, not one per profile.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
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
from opencohost.i18n.tags import primary as i18n_primary

logger = get_logger()

# label is deliberately "style" for custom_instructions (design §2: locale-
# neutral data labels, not scaffolding) — order also fixes the on-wire order.
_FIELD_LABELS = (
    ("nickname", "nickname"),
    ("occupation", "occupation"),
    ("interests", "interests"),
    ("custom_instructions", "style"),
)

# Task B (kira-natural-replies-20261111): identity/directive fields stay in the
# stable system message; topic facts (occupation, interests) are conditional.
_IDENTITY_FIELDS = frozenset({"nickname", "custom_instructions"})
_FACT_FIELDS = frozenset({"occupation", "interests"})

# Per-path mtime cache — per-turn engine reads are usually zero-I/O (design §1)
# while keeping es and en entries strictly isolated.
_cache_state: dict[str, dict] = {}


def _resolve_locale(locale: str | None = None) -> str:
    tag = locale if locale is not None else i18n_active.get_active_bundle().code
    return "en" if i18n_primary(tag) == "en" else "es"


def _personalization_path(locale: str | None = None) -> str:
    if _resolve_locale(locale) == "en":
        base = Path(PERSONALIZATION_FILE)
        return str(base.with_name(f"{base.stem}_en{base.suffix}"))
    return PERSONALIZATION_FILE


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


def load_personalization(locale: str | None = None) -> dict:
    """Load the personalization store. Fail-open to defaults on any error —
    absent file, corrupt JSON, or a non-dict payload never raises.

    Cached on the file's mtime per path: repeated calls within the same turn
    (or across turns with no intervening save) never re-hit disk, while es and
    en entries stay isolated.
    """
    path = _personalization_path(locale)
    if not os.path.exists(path):
        _cache_state.pop(path, None)
        return _defaults()

    try:
        mtime = os.stat(path).st_mtime
    except OSError as e:
        logger.warning(f"Could not stat personalization file {path}: {type(e).__name__}")
        return _defaults()

    cached = _cache_state.get(path)
    if cached is not None and cached["mtime"] == mtime and cached["data"] is not None:
        return dict(cached["data"])

    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if not isinstance(raw, dict):
            raise ValueError("personalization file did not contain a JSON object")
        data = _clamp({**_defaults(), **raw})
    except Exception as e:
        logger.warning(f"Could not load personalization from {path}: {type(e).__name__}")
        return _defaults()

    _cache_state[path] = {"mtime": mtime, "data": data}
    return dict(data)


def save_personalization(data: dict, locale: str | None = None) -> bool:
    """Write the personalization store atomically. Never raises; returns
    False on failure so callers (the API handlers) can surface a 503 instead
    of a false 200. Never logs field content — only the path and exception
    type (CLAUDE.md: never expose raw chat/profile content in logs).
    """
    path = _personalization_path(locale)
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
    _cache_state[path] = {"mtime": mtime, "data": payload}
    return True


def clear_personalization(locale: str | None = None) -> bool:
    """Reset the store to defaults (purge action)."""
    return save_personalization(_defaults(), locale=locale)


def build_injection_block(
    sanitize_fn: Callable[[str], str],
    part: str = "all",
    locale: str | None = None,
) -> str:
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

    ``part`` selects the fields: "all" (default, every field), "identity"
    (nickname + style, the stable system part) or "facts" (occupation +
    interests, the conditional preamble part). Each part gets the same
    backstop independently.
    """
    try:
        data = load_personalization() if locale is None else load_personalization(locale=locale)
        if not data.get("enabled", True):
            return ""

        lines = []
        for field, label in _FIELD_LABELS:
            if part == "identity" and field not in _IDENTITY_FIELDS:
                continue
            if part == "facts" and field not in _FACT_FIELDS:
                continue
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


# --- conditional facts gate (task B) ---------------------------------------

_SELF_REFERENCE = re.compile(
    r"\b(?:"
    r"quien\s+soy|que\s+sabes\s+de\s+mi|me\s+conoces|a\s+que\s+me\s+dedico"
    r"|mi\s+trabajo|mi\s+profesion|mis\s+gustos|mis\s+intereses"
    r"|who\s+am\s+i|what\s+do\s+you\s+know\s+about\s+me|do\s+you\s+know\s+me"
    r"|my\s+job|my\s+work|my\s+interests"
    r")\b"
)

_STOPWORDS = frozenset(
    """
    para como pero porque cuando donde desde hasta sobre entre este esta estos estas esto
    ese esa esos esas aquel con sin por que del las los una unos unas muy mas menos tambien
    sido ser son era fue hay tiene tengo tienes algo todo todos nada otro otra cada solo
    that this these those with from have has had been being were was will would could should
    about into over than then them they their there what when where which while your yours
    some such only also just very more most much many each other
    """.split()
)


def _fold(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", str(text or ""))
    return "".join(c for c in decomposed if not unicodedata.combining(c)).casefold()


def _stems(text: str) -> set[str]:
    return {
        tok[:5]
        for tok in re.findall(r"[a-z0-9]+", _fold(text))
        if len(tok) >= 4 and tok not in _STOPWORDS
    }


def facts_gate(user_text: str, data: dict) -> str | None:
    """Deterministic relevance gate for the occupation/interests block.

    Returns "self_reference" when the user text asks about the owner,
    "overlap" when it shares a content-token stem with the facts, else None.
    """
    folded = _fold(user_text)
    if not folded.strip():
        return None
    if _SELF_REFERENCE.search(folded):
        return "self_reference"
    fact_stems = _stems(" ".join(str(data.get(f) or "") for f in ("occupation", "interests")))
    if fact_stems and (_stems(user_text) & fact_stems):
        return "overlap"
    return None
