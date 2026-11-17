"""Text normalization for the non-negotiable screens (S7, guardrails-vulnerable-users).

``runtime_screen`` (viewer chat) and ``output_guard`` (Kira's voice) match
patterns written for plain text. A viewer can slip past them with zero-width
characters, fullwidth forms, leetspeak, single-letter spacing, repeated
letters, accents or a lookalike Cyrillic letter. ``normalize_for_screen``
returns the plain-text form; the screens check BOTH the raw and the normalized
text, so nothing that matched before stops matching.

Care for false positives: digits become letters only inside a token that also
has letters ("p3ndejo"), never in a bare number, a score or a date.
"""
from __future__ import annotations

import re
import unicodedata

_LEET = str.maketrans({"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "@": "a"})

# Cyrillic letters that look like Latin ones; mapped only inside a token that
# mixes both scripts, so genuine Russian text is untouched.
_CONFUSABLES = str.maketrans({
    "а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "х": "x", "у": "y",
    "і": "i", "ѕ": "s", "ј": "j", "ԁ": "d", "һ": "h", "к": "k", "м": "m",
    "т": "t", "н": "h", "в": "b",
})

_TOKEN_RE = re.compile(r"\S+")
# 4+ single letters separated by one space or one of . - _ *  ("p e n d e j o")
_SPACED_RE = re.compile(
    r"(?<![^\W\d_])(?:[^\W\d_][ .\-_*]){3,}[^\W\d_](?![^\W\d_])"
)
# "www" is a host prefix, not emphasis, so "w" is never collapsed.
_REPEAT_RE = re.compile(r"((?![wW])[^\W\d_])\1{2,}")


def _is_cyrillic(ch: str) -> bool:
    return "CYRILLIC" in unicodedata.name(ch, "")


def _fix_token(token: str) -> str:
    has_letter = any(ch.isalpha() for ch in token)
    if not has_letter:
        return token
    if any(_is_cyrillic(ch) for ch in token) and any(
        ch.isalpha() and ord(ch) < 0x250 for ch in token
    ):
        token = token.translate(_CONFUSABLES)
    if any(ch.isdigit() or ch == "@" for ch in token):
        letters = sum(1 for ch in token if ch.isalpha())
        # "p3ndejo" / "p@ndejo": mostly letters with a few look-alike digits.
        if letters >= 2:
            token = token.translate(_LEET)
    return token


def _collapse_spaced(match: re.Match) -> str:
    return "".join(ch for ch in match.group(0) if ch.isalpha())


def normalize_for_screen(text: str) -> str:
    """Plain-text form of ``text`` for screening (see module docstring)."""
    if not isinstance(text, str) or not text:
        return "" if not isinstance(text, str) else text
    text = unicodedata.normalize("NFKC", text)
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Cf")
    text = _TOKEN_RE.sub(lambda m: _fix_token(m.group(0)), text)
    text = _SPACED_RE.sub(_collapse_spaced, text)
    text = _REPEAT_RE.sub(r"\1", text)
    # Fold accents last: the patterns accept both forms but not every stem.
    folded = unicodedata.normalize("NFD", text)
    return "".join(ch for ch in folded if unicodedata.category(ch) != "Mn")
