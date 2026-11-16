"""Whisper silence-hallucination filter for owner speech (vision-and-prompt-leaks T4).

On silence or noise Whisper emits stock phrases from its training subtitles
("Gracias por ver este video.", "Subtítulos realizados por la comunidad de
Amara.org"). Live 2026-10-17 they reached the owner transcript and the drafts.

Only a sentence that IS a known hallucination is removed (whole-sentence match
after case, accent and punctuation folding). A sentence that merely contains a
trigger word ("Gracias por ver este video conmigo, Kira") is kept: precision
matters more than recall. When nothing is left the caller drops the turn.

Pure module: no engine imports, no logging, the text is only matched.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

_NON_WORD_RE = re.compile(r"[^0-9a-z]+")
_SENTENCE_BOUNDARY_RE = re.compile(r"(?<=[.!?…])\s+")

# Matched against the whole normalized sentence (see ``_fold``).
_HALLUCINATION_RE = re.compile(
    r"^(?:"
    r"gracias por ver (?:este|el|mi) video"
    r"|muchas gracias a todos aquellos"
    r"|subtitul\w+ (?:realizados? )?por (?:la )?comunidad de amara org"
    r"|thanks for watching(?: this video)?"
    r"|thank you for watching(?: this video)?"
    r")$"
)


@dataclass(frozen=True)
class HallucinationResult:
    """``text`` is what is left; ``dropped`` means nothing is left and the turn goes."""

    text: str
    removed: int
    dropped: bool


def _fold(sentence: str) -> str:
    decomposed = unicodedata.normalize("NFKD", sentence.lower())
    no_accents = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return _NON_WORD_RE.sub(" ", no_accents).strip()


def is_hallucination(sentence: str) -> bool:
    """True when this whole sentence is a known Whisper silence hallucination."""
    return bool(_HALLUCINATION_RE.match(_fold(sentence or "")))


def filter_owner_speech(text) -> HallucinationResult:
    """Strip whole-sentence hallucinations from *text*. Never raises."""
    try:
        if not isinstance(text, str) or not text.strip():
            return HallucinationResult("", 0, False)
        parts = [p for p in _SENTENCE_BOUNDARY_RE.split(text.strip()) if p.strip()]
        kept = [p for p in parts if not is_hallucination(p)]
        removed = len(parts) - len(kept)
        if removed == 0:
            return HallucinationResult(text, 0, False)
        return HallucinationResult(" ".join(kept), removed, not kept)
    except Exception:
        return HallucinationResult(text if isinstance(text, str) else "", 0, False)
