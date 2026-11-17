"""Reply-length level resolution (reply-length-selector-20261009).

Pure function, no I/O and no LLM call: given the owner's setting and the clean
user text of an owner turn, pick the reply level the prompt assembler applies
(``short | medium | long | very_long``) plus a reason code for the log.

Precedence:
  1. The profile's ``extended_responses=True`` forces ``very_long``
     (reason ``profile_extended``), so existing profiles keep working.
  2. A fixed setting returns itself (reason ``setting``).
  3. ``auto`` (and any unknown mode):
       * a vision turn -> ``medium`` (``vision``); the screen reply has its own cap;
       * an explicit depth request -> ``long`` (``depth_request``). Checked
         before the short-input rule so "explícame eso" is not answered in one
         sentence;
       * at most 3 words, or only greetings / laughs / acknowledgements
         -> ``short`` (``brief_input``);
       * otherwise ``medium`` (``default``).
     Auto never picks ``very_long``.

Matching is on whole words after lowercasing and stripping accents, so
"lista" matches "lista" but not "listado", "list" does not match "listener",
"por qué" does not match "porque" and "compara" does not match "comparado".
"""

from __future__ import annotations

import re
import unicodedata
from typing import Optional

LEVELS: tuple[str, ...] = ("short", "medium", "long", "very_long")
BRIEF_MAX_WORDS = 3

# Single-word greetings, acknowledgements and fillers (es + en), accent-free.
BRIEF_WORDS: frozenset[str] = frozenset(
    {
        # es
        "hola", "holi", "holis", "buenas", "buenos", "buen", "dia", "dias", "tardes",
        "noches", "que", "tal", "ok", "oki", "okey", "vale", "va", "gracias", "muchas",
        "mil", "si", "sip", "no", "claro", "genial", "perfecto", "bien", "dale",
        "listo", "xd", "kira", "jaja", "jeje",
        # en
        "hi", "hello", "hey", "yo", "thanks", "thank", "you", "thx", "ty", "lol", "lmao",
        "yes", "yeah", "yep", "nope", "sure", "cool", "nice", "great", "okay", "good",
        "morning", "evening", "night", "haha",
    }
)
# Laughs of any length: jaja, jajajaj, jeje, jiji, haha, hehe, jsjs, xdd.
_LAUGH_RE = re.compile(r"^(?:(?:j[aei]|h[ae]|js|sj)+[jh]?|x+d+)$")

# Single-word depth requests (es + en), accent-free.
DEPTH_WORDS: frozenset[str] = frozenset(
    {
        # es
        "explica", "explicame", "explicas", "explicar", "explicarme", "detalla",
        "detallame", "lista", "pasos", "compara", "comparame",
        # en
        "explain", "why", "list", "steps", "compare",
    }
)
# Multi-word depth requests, as accent-free word tuples.
DEPTH_PHRASES: tuple[tuple[str, ...], ...] = (
    ("por", "que"),
    ("como", "funciona"),
    ("en", "detalle"),
    ("a", "fondo"),
    ("how", "does"),
    ("in", "detail"),
)

_WORD_RE = re.compile(r"[a-z0-9]+")


def _normalize(text: Optional[str]) -> list[str]:
    """Lowercased, accent-free word list."""
    if not text:
        return []
    decomposed = unicodedata.normalize("NFKD", str(text).lower())
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return _WORD_RE.findall(stripped)


def _is_brief_word(word: str) -> bool:
    return word in BRIEF_WORDS or bool(_LAUGH_RE.match(word))


def _has_depth_request(words: list[str]) -> bool:
    if any(w in DEPTH_WORDS for w in words):
        return True
    for phrase in DEPTH_PHRASES:
        n = len(phrase)
        for i in range(len(words) - n + 1):
            if tuple(words[i : i + n]) == phrase:
                return True
    return False


def resolve_reply_level(
    mode: str,
    user_text: Optional[str],
    *,
    vision_turn: bool,
    extended_responses: bool,
) -> tuple[str, str]:
    """Return ``(level, reason)`` for one owner turn. Never raises."""
    if extended_responses:
        return "very_long", "profile_extended"
    if mode in LEVELS:
        return mode, "setting"
    if vision_turn:
        return "medium", "vision"
    words = _normalize(user_text)
    if _has_depth_request(words):
        return "long", "depth_request"
    if len(words) <= BRIEF_MAX_WORDS or all(_is_brief_word(w) for w in words):
        return "short", "brief_input"
    return "medium", "default"
