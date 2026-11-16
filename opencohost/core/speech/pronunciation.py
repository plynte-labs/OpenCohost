"""Owner-editable TTS pronunciation dictionary (tts-pronunciation-dictionary).

Literal text -> spoken replacement, applied as the LAST text stage (after
every safety stage, followed by a control-block re-check) through
``tts_sanitizer._spoken_text_for_fragment``: per speech fragment at synthesis
time, and over the whole text in ``_sanitize_tts_text_for_playback`` so the
output guard judges the spoken form. Fragments shown to the UI never carry
replacements (follow-reading-pronunciation R1).

Design constraints:
  * Literal only. Entries are ``re.escape``d into ONE compiled alternation,
    longest first, case-insensitive, with word boundaries on the sides where
    the entry edge is a word character. Python ``re`` has no timeout and this
    runs on the speech producer thread, so users never supply a regex.
  * Replacements are literal (callable ``sub``), so no backreferences.
  * Replacements may not contain sentence terminators, newlines, ``<``, ``>``
    or ``*`` so sentence/chunk boundaries and markup never move.
  * The module default is an EMPTY in-memory dictionary. Nothing reads disk
    until ``load_pronunciation()`` is called (API startup), which keeps unrelated
    tests hermetic.
  * Logs carry counts and exception types only, never entries or text.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from pathlib import Path

from opencohost.config.logger import get_logger
from opencohost.config.settings import PRONUNCIATION_FILE
from opencohost.i18n import active as i18n_active
from opencohost.i18n.tags import primary as i18n_primary

logger = get_logger()

MAX_ENTRIES = 200
MATCH_MAX = 64
REPLACE_MAX = 128
LIMITS = {"max_entries": MAX_ENTRIES, "match_max": MATCH_MAX, "replace_max": REPLACE_MAX}

_FORBIDDEN_REPLACE_CHARS = frozenset(".!?…\n\r<>*")

_lock = threading.Lock()
# Per-locale compiled state: locale -> (compiled alternation or None, lowercase match -> replacement, ordered entries)
_EMPTY_STATE: tuple = (None, {}, [])
_state_by_locale: dict[str, tuple] = {}


def _resolve_locale(locale: str | None = None) -> str:
    if locale is not None:
        tag = locale
    else:
        try:
            tag = i18n_active.get_active_bundle().code
        except Exception as exc:  # noqa: BLE001 - TTS must never break on locale lookup
            # Fail safe to the legacy "es" table: before locale isolation there was
            # a single dictionary persisted to the unsuffixed pronunciation.json.
            logger.warning("pronunciation: active locale lookup failed (%s); using es", type(exc).__name__)
            tag = "es"
    return "en" if i18n_primary(tag) == "en" else "es"


def _pronunciation_path(locale: str | None = None) -> str:
    if _resolve_locale(locale) == "en":
        base = Path(PRONUNCIATION_FILE)
        return str(base.with_name(f"{base.stem}_en{base.suffix}"))
    return PRONUNCIATION_FILE


def _get_state(locale: str | None = None) -> tuple:
    loc = _resolve_locale(locale)
    with _lock:
        return _state_by_locale.get(loc, _EMPTY_STATE)


def validate_entries(raw) -> tuple[list[dict], list[dict]]:
    """Validate a raw entries list. Returns ``(clean_entries, errors)``.

    Errors are ``{"index": int, "field": str, "code": str}``; entry-level
    problems use ``field == "entries"``. ``clean_entries`` is only meaningful
    when ``errors`` is empty.
    """
    errors: list[dict] = []
    if not isinstance(raw, list):
        return [], [{"index": 0, "field": "entries", "code": "not_list"}]
    if len(raw) > MAX_ENTRIES:
        errors.append({"index": 0, "field": "entries", "code": "too_many_entries"})
    clean: list[dict] = []
    seen: set[str] = set()
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            errors.append({"index": i, "field": "entries", "code": "not_object"})
            continue
        match, replace = item.get("match"), item.get("replace")
        ok = True
        if not isinstance(match, str):
            errors.append({"index": i, "field": "match", "code": "not_string"})
            ok = False
        else:
            match = match.strip()
            if not match:
                errors.append({"index": i, "field": "match", "code": "empty"})
                ok = False
            elif len(match) > MATCH_MAX:
                errors.append({"index": i, "field": "match", "code": "too_long"})
                ok = False
            elif match.lower() in seen:
                errors.append({"index": i, "field": "match", "code": "duplicate"})
                ok = False
        if not isinstance(replace, str):
            errors.append({"index": i, "field": "replace", "code": "not_string"})
            ok = False
        else:
            replace = replace.strip()
            if len(replace) > REPLACE_MAX:
                errors.append({"index": i, "field": "replace", "code": "too_long"})
                ok = False
            elif any(ch in _FORBIDDEN_REPLACE_CHARS for ch in replace):
                errors.append({"index": i, "field": "replace", "code": "forbidden_char"})
                ok = False
        if ok:
            seen.add(match.lower())
            clean.append({"match": match, "replace": replace})
    return clean, errors


def _alt(match: str) -> str:
    # Word boundary only where the entry edge is a word char, so entries such
    # as "C++" or "(x)" still match next to punctuation.
    left = r"(?<!\w)" if (match[0].isalnum() or match[0] == "_") else ""
    right = r"(?!\w)" if (match[-1].isalnum() or match[-1] == "_") else ""
    return f"{left}{re.escape(match)}{right}"


def _compile(entries: list[dict]) -> tuple:
    if not entries:
        return (None, {}, [])
    ordered = sorted(entries, key=lambda e: len(e["match"]), reverse=True)
    pattern = re.compile("|".join(_alt(e["match"]) for e in ordered), re.IGNORECASE)
    table = {e["match"].lower(): e["replace"] for e in entries}
    return (pattern, table, [dict(e) for e in entries])


def set_entries(raw, locale: str | None = None) -> list[dict]:
    """Validate and install entries in memory only. Returns errors ([] on
    success); on errors the previous dictionary stays active."""
    clean, errors = validate_entries(raw)
    if errors:
        return errors
    loc = _resolve_locale(locale)
    compiled = _compile(clean)
    with _lock:
        _state_by_locale[loc] = compiled
    return []


def reset_pronunciation(locale: str | None = None) -> None:
    """Drop in-memory dictionaries (or one locale's dictionary when specified). Does not touch disk."""
    with _lock:
        if locale is None:
            _state_by_locale.clear()
        else:
            _state_by_locale.pop(_resolve_locale(locale), None)


def get_entries(locale: str | None = None) -> list[dict]:
    return [dict(e) for e in _get_state(locale)[2]]


def apply_pronunciation(text: str, locale: str | None = None) -> str:
    """Single regex pass; returns ``text`` untouched when no dictionary is active."""
    pattern, table, _ = _get_state(locale)
    if pattern is None or not text:
        return text
    out = pattern.sub(lambda m: table.get(m.group(0).lower(), m.group(0)), text)
    if "" in table.values():
        # A silenced word leaves a double space behind; the sanitizer's own
        # punctuation cleanup handles the rest.
        out = re.sub(r"[ \t]{2,}", " ", out).strip()
    return out


def _load_one_locale(loc: str) -> int:
    path = _pronunciation_path(loc)
    entries: list = []
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if not isinstance(raw, dict):
                raise ValueError("not an object")
            entries, errors = validate_entries(raw.get("entries"))
            if errors:
                raise ValueError("invalid entries")
        except Exception as e:
            logger.warning(f"Could not load pronunciation dictionary: {type(e).__name__}")
            entries = []
    set_entries(entries, locale=loc)
    logger.info(f"Pronunciation dictionary ({loc}) loaded: {len(entries)} entries")
    return len(entries)


def load_pronunciation(locale: str | None = None) -> int:
    """Load the dictionary from disk into memory. Fails open to an empty
    dictionary on a missing/corrupt/invalid file. When ``locale`` is None,
    loads both ``es`` and ``en`` and returns the active locale's entry count."""
    if locale is not None:
        return _load_one_locale(_resolve_locale(locale))
    counts = {loc: _load_one_locale(loc) for loc in ("es", "en")}
    return counts[_resolve_locale(None)]


def save_pronunciation(raw, locale: str | None = None):
    """Validate, write atomically (temp + os.replace) and install.

    Returns ``[]`` on success, the error list on validation failure, or
    ``None`` when the write failed (callers map that to 503). The in-memory
    dictionary only changes on success.
    """
    clean, errors = validate_entries(raw)
    if errors:
        return errors
    loc = _resolve_locale(locale)
    path = _pronunciation_path(loc)
    target_dir = os.path.dirname(path) or "."
    tmp_path = None
    try:
        os.makedirs(target_dir, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=target_dir, prefix=".pronunciation_", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"version": 1, "entries": clean}, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, path)
    except Exception as e:
        logger.warning(f"Failed to save pronunciation dictionary: {type(e).__name__}")
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        return None
    set_entries(clean, locale=loc)
    logger.info(f"Pronunciation dictionary ({loc}) saved: {len(clean)} entries")
    return []
