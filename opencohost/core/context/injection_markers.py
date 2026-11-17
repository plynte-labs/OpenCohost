"""
opencohost/core/context/injection_markers.py

Prompt injection markers and string neutralization utilities.
Extracted from MotorVocalIA.
"""

from __future__ import annotations

import re
import unicodedata

_USER_LABEL_PATTERN = re.compile(r"[a-zA-ZáéíóúÁÉÍÓÚñÑüÜ]+")

# Max words allowed in a scout title (preamble filter rejects longer lines).
SCOUT_TITLE_MAX_WORDS = 6

# Prompt-injection marker floor (modest, not exhaustive — keyword lists don't
# scale). Shared by _sanitize_history_context (neutralizes via truncation) and
# the scout scrub (removes the phrase outright from the compact render).
INJECTION_MARKERS = (
    # English markers
    "ignore all previous",
    "you are now",
    "new system prompt",
    "pretend you are",
    "forget everything",
    "disregard previous",
    "do not follow",
    "your new role is",
    "you must now",
    "act as if",
    "from now on you are",
    # Spanish markers
    "olvida todo",
    "olvidá todo",
    "ignora todo",
    "ignorá todo",
    "ignora las instrucciones",
    "ignorá las instrucciones",
    "ahora eres",
    "ahora sos",
    "nuevo system prompt",
    "nuevo prompt de sistema",
    "haz de cuenta",
    "hacé de cuenta",
    "tu nuevo rol es",
    "no sigas",
    "no obedezcas",
    "actúa como",
    "actua como",
    "de ahora en adelante eres",
    "de ahora en más sos",
)


def _normalize_for_markers(text: str) -> str:
    """One-for-one NFKC, drop zero-width / format (Cf) characters, collapse every run of
    whitespace (tabs, newlines, NBSP) to one space."""
    # NFKC one character at a time, and only where it maps one-for-one
    # (fullwidth letters, NBSP): an expanding mapping such as "..." for the
    # ellipsis would change the text length the injection char budgets rely on.
    out = []
    for ch in text:
        folded = unicodedata.normalize("NFKC", ch)
        ch = folded if len(folded) == 1 else ch
        if unicodedata.category(ch) != "Cf":
            out.append(ch)
    return " ".join("".join(out).split())


_MARKER_RE = re.compile(
    "|".join(re.escape(m) for m in sorted(INJECTION_MARKERS, key=len, reverse=True)),
    re.IGNORECASE,
)


def _strip_injection_markers(text: str) -> str:
    """Remove INJECTION_MARKERS phrases outright, collapse whitespace.

    Shared by the scout scrub (_scout_scrub_text) and the memorias injection
    path (_build_memorias_injection_block): both surfaces re-render stored
    text into the prompt, so a marker phrase must be stripped, not merely
    truncated around (which _sanitize_history_context alone would do).

    S6 (guardrails-vulnerable-users): the text is normalized first and the
    removal repeats until nothing changes, so a marker rebuilt by whitespace
    collapse, zero-width removal or the removal of a nested marker cannot
    survive.
    """
    text = _normalize_for_markers(text)
    while True:
        stripped = _normalize_for_markers(_MARKER_RE.sub("", text))
        if stripped == text:
            return text
        text = stripped


def sanitize_user_label(raw_name: str | None, fallback: str | None = None) -> str:
    """Sanitize and normalize a user display name for prompt label framing.

    Whitelists only letters (a-z, A-Z, áéíóúÁÉÍÓÚñÑüÜ) and single spaces between
    words. Strips brackets [], colons, newlines, numbers, and special symbols.
    Clamps the result to at most 32 characters.
    If empty after sanitization, returns fallback if non-empty, else the active
    locale's user label (es "Usuario", en "User").
    """
    cleaned = clean_user_label(raw_name)
    if cleaned:
        return cleaned
    if isinstance(fallback, str) and fallback.strip():
        return fallback.strip()
    return _locale_user_label()


_CHAT_USERNAME_DROP = re.compile(r"[^\w .\-]", re.UNICODE)


def sanitize_chat_username(raw_name: object) -> str:
    """Prompt-safe form of a viewer's chat name (S7, guardrails-vulnerable-users).

    Keeps the name's identity (letters of any script, digits, ``_``, ``-``,
    ``.``, single spaces) so per-user rate limits and whitelists still tell
    viewers apart, but drops newlines, brackets, colons, quotes, zero-width
    and every other character that could fake a prompt label or a role line.
    Clamped to 32 characters. Not a display-name rewrite: ``sanitize_user_label``
    is the stricter, letters-only form used when a label is spoken."""
    if not isinstance(raw_name, str):
        return ""
    text = unicodedata.normalize("NFKC", raw_name)
    text = "".join(ch for ch in text if unicodedata.category(ch) not in ("Cf", "Cc"))
    text = _CHAT_USERNAME_DROP.sub("", text)
    return " ".join(text.split())[:32].strip()


def clean_user_label(raw_name: str | None) -> str:
    """The sanitized label, or "" when nothing survives sanitization (K5:
    callers that can say "no name" use this instead of a placeholder)."""
    if isinstance(raw_name, str):
        words = _USER_LABEL_PATTERN.findall(raw_name)
        if words:
            return " ".join(words)[:32].strip()
    return ""


def _locale_user_label() -> str:
    """Active locale's user label; "Usuario" if i18n is unavailable."""
    try:
        from opencohost.i18n import active as i18n_active

        label = i18n_active.user_message_label()
        if isinstance(label, str) and label.strip():
            return label.strip()
    except Exception:
        pass
    return "Usuario"
