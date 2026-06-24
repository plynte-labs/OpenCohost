"""Pure, dependency-free detector for chat-reactive repetition.

Catches the two failure modes observed in real RF3 "Chat Live" runs where a weak
model collapses into a loop:

  * exact / near-exact duplicate lines (e.g. the same line emitted verbatim 4x);
  * synonym-swap TEMPLATE repetition ("Cada partida es un abismo sin fin" /
    "Cada derrota es un abismo sin salida") that a content-token overlap detector
    provably misses, because the swapped content words are exactly what it measures.

No I/O, no engine state, no embeddings (the project is local-first and bans
torch-scale deps). stdlib only, so it stays trivially unit-testable and cheap to
call on every chat turn.

Layer 2 (the scaffold detector) is deliberately THRESHOLD-FREE: it masks every
non-function-word token to ``#`` regardless of length, collapses consecutive
masks, and compares the resulting skeleton SEQUENCES by equality. A scalar
similarity threshold cannot both accept a rotating-noun template and reject a
merely-similar pair, so equality + a ``min_slots`` rail is used instead.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Sequence

# Spanish function words kept verbatim in the skeleton; everything else is masked.
# Conservative set: articles, prepositions, conjunctions, common copulas, and the
# highest-frequency determiners/pronouns. Kept small on purpose — a larger set
# makes skeletons sparser and risks false positives.
_FUNCTION_WORDS = frozenset({
    "el", "la", "los", "las", "un", "una", "unos", "unas", "lo", "al", "del",
    "a", "ante", "bajo", "con", "contra", "de", "desde", "en", "entre", "hacia",
    "hasta", "para", "por", "segun", "sin", "so", "sobre", "tras",
    "y", "e", "o", "u", "ni", "pero", "sino", "que", "porque", "pues", "aunque",
    "si", "como", "cuando", "donde", "quien",
    "es", "son", "esta", "estan", "ser", "estar", "hay", "fue", "era", "soy",
    "sos", "somos", "esto", "este", "esta", "estos", "estas", "ese", "esa",
    "eso", "esos", "esas", "aquel", "aquella",
    "cada", "todo", "toda", "todos", "todas", "mucho", "muy", "mas", "menos",
    "se", "me", "te", "nos", "le", "les", "su", "sus", "mi", "tu", "vos", "yo",
    "ella", "no", "ya", "tan",
})

_WORD_RE = re.compile(r"\w+", re.UNICODE)


@dataclass(frozen=True)
class RepetitionConfig:
    window: int = 4                  # compare only against the last N prior lines
    min_chars: int = 24              # ignore short interjections
    min_slots: int = 2               # a scaffold needs >= 2 masked content slots
    near_exact_threshold: float = 0.92   # char-shingle Jaccard over the full line
    opening_words: int = 6
    shingle_k: int = 4


DEFAULT_CONFIG = RepetitionConfig()


@dataclass(frozen=True)
class RepetitionResult:
    is_repetitive: bool
    reason: str = ""   # '' | exact_dup | near_exact_dup | scaffold_repeat | opening_ngram_repeat
    detail: str = ""   # the matched prior line (original text), for owner-review logging


def _strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def _norm(s: str) -> str:
    s = _strip_accents(s.lower())
    s = re.sub(r"\s+", " ", s).strip()
    return s.strip("¡!¿?.…\"' ").strip()


def _tokens(norm_line: str) -> list:
    return _WORD_RE.findall(norm_line)


def _char_shingles(s: str, k: int) -> set:
    compact = s.replace(" ", "")
    if len(compact) < k:
        return {compact} if compact else set()
    return {compact[i:i + k] for i in range(len(compact) - k + 1)}


def _jaccard(a: set, b: set) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _skeleton(tokens: Sequence[str]) -> list:
    """Mask every non-function-word token to '#'; collapse consecutive masks."""
    out: list = []
    for tok in tokens:
        slot = tok if tok in _FUNCTION_WORDS else "#"
        if slot == "#" and out and out[-1] == "#":
            continue
        out.append(slot)
    return out


def detect_repetition(
    candidate: str,
    recent: Sequence[str],
    *,
    cfg: RepetitionConfig = DEFAULT_CONFIG,
) -> RepetitionResult:
    """Return whether ``candidate`` repeats one of the ``recent`` prior lines.

    ``recent`` is the speaker's own last few lines (oldest first). The candidate
    is only ever compared to PRIOR lines, so a first occurrence is never flagged.
    """
    cand_norm = _norm(candidate)
    if len(cand_norm) < cfg.min_chars:
        return RepetitionResult(False)

    window = [r for r in recent if r and r.strip()][-cfg.window:]
    if not window:
        return RepetitionResult(False)

    cand_shingles = _char_shingles(cand_norm, cfg.shingle_k)

    # Layer 1 — exact / near-exact duplicate.
    for prior in window:
        prior_norm = _norm(prior)
        if not prior_norm:
            continue
        if cand_norm == prior_norm:
            return RepetitionResult(True, "exact_dup", prior)
        if _jaccard(cand_shingles, _char_shingles(prior_norm, cfg.shingle_k)) >= cfg.near_exact_threshold:
            return RepetitionResult(True, "near_exact_dup", prior)

    cand_tokens = _tokens(cand_norm)
    cand_skeleton = _skeleton(cand_tokens)
    cand_slots = cand_skeleton.count("#")

    # Layer 2 — scaffold / synonym-swap (threshold-free skeleton equality).
    if cand_slots >= cfg.min_slots and len(cand_skeleton) >= 2:
        for prior in window:
            if _skeleton(_tokens(_norm(prior))) == cand_skeleton:
                return RepetitionResult(True, "scaffold_repeat", prior)

    # Layer 3 — reused opening n-gram (data-driven generalization of a fixed rule).
    cand_opening = tuple(cand_tokens[:cfg.opening_words])
    if len(cand_opening) >= cfg.opening_words:
        for prior in window:
            if tuple(_tokens(_norm(prior))[:cfg.opening_words]) == cand_opening:
                return RepetitionResult(True, "opening_ngram_repeat", prior)

    return RepetitionResult(False)
