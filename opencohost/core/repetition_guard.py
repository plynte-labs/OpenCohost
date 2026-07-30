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


# ---------------------------------------------------------------------------
# Intra-sentence clause sanitizer (clause_sanitizer V1, 2026-07-29)
# ---------------------------------------------------------------------------
# A real ~16-minute agenda session emitted, inside ONE sentence:
#   "No había roadmap, no había monetización, no había roadmap, no había roadmap."
# Every existing defense is structurally blind to that shape: detect_repetition
# above compares whole candidates ACROSS turns, has_looping_lines splits on
# [.!?¡¿] and filters out anything <=24 chars, trim_trailing_repeated_sentences
# only compares against recent_texts, and the TTS comma-chunker runs after all
# of them. So this tier works strictly INSIDE one response and inside one
# sentence, and never looks at history.
#
# That intra-response scope is also the safety property, not just a limitation:
# an operator can legitimately ask Kira to repeat something, but the repeat
# lands in a LATER turn, and nothing here can see a later turn. No intent
# detection exists — no phrase list, no request-matching regex, no LLM call.

# A sentence boundary is a closing terminator followed by whitespace. The
# separator is captured so inter-sentence whitespace (including newlines)
# survives a rebuild byte-for-byte.
_SENT_SPLIT_RE = re.compile(r"((?<=[.!?…])\s+)")
# A clause delimiter counts ONLY when followed by whitespace, which is what
# protects decimals (3,5 / 3.5), times (20:30) and URLs (ejemplo.com/ruta).
_CLAUSE_SPLIT_RE = re.compile(r"(?<=[,;:])\s+")
_TERMINATOR_RE = re.compile(r"[.!?…]+$")

# Severity thresholds. Every value is reasoned, not measured — anchored to the
# repo's own agenda turn sizes (844-char average / 1079 max over 46 generations,
# ~84 chars per TTS fragment, corta target 450). The two length axes are gated
# behind LENGTH_AXES_FLOOR because an ungated draft of these same numbers
# rejected the confirmed incident, which must classify as repairable.
SANITIZE_REJECT_FRAGMENTS = 5        # total clauses dropped, any distribution
SANITIZE_REJECT_DISTINCT = 2         # distinct clause keys dropped >=2x each
SANITIZE_REJECT_REMOVED_PCT = 0.30   # share of characters removed
SANITIZE_MIN_REMAINING_CHARS = 100   # a long turn gutted down to nothing
SANITIZE_SHORT_EXPR_MAX_CHARS = 12   # eligibility floor on the normalized key
SANITIZE_LENGTH_AXES_FLOOR = 300     # arms the two length-variant axes
# Floor for the percentage axis: below 3 drops a high percentage just means the
# turn was short, which SANITIZE_LENGTH_AXES_FLOOR alone does not exclude.
_REMOVED_PCT_MIN_DROPS = 3


@dataclass(frozen=True)
class ClauseSanitizeResult:
    """Outcome of one sanitize pass. Every field but ``text`` is metadata.

    ``verdict`` is ``clean`` (nothing dropped), ``repaired`` (``text`` is safe to
    speak) or ``rejected`` (degenerate beyond repair — the caller decides what to
    do, and only the agenda path has a regeneration owner).
    """
    text: str
    verdict: str
    removed_fragments: int = 0
    distinct_looping: int = 0
    max_occurrences: int = 0
    original_len: int = 0
    remaining_len: int = 0
    removed_pct: float = 0.0


def _clause_key(fragment: str) -> tuple:
    """Normalized identity of one clause: (opens_inverted, token string).

    The inverted-mark flag keeps a statement and its echoed question distinct
    ("...que lo borraste todo, ¿que lo borraste todo?") — same words, different
    speech act, so it is rhetoric rather than degeneration.
    """
    inverted = fragment.lstrip()[:1] in ("¿", "¡")
    return (inverted, " ".join(_tokens(_norm(fragment))))


def _repair_sentence(sentence: str) -> tuple:
    """Collapse repeated clauses inside ONE sentence.

    Returns ``(repaired, drops_by_key, max_occurrences)``. The sentence is
    returned untouched (identical object) when nothing is dropped.
    """
    match = _TERMINATOR_RE.search(sentence)
    terminator = match.group(0) if match else ""
    body = sentence[: match.start()] if match else sentence

    fragments = _CLAUSE_SPLIT_RE.split(body)
    if len(fragments) < 2:
        return sentence, {}, 0

    positions: dict = {}
    for index, fragment in enumerate(fragments):
        key = _clause_key(fragment)
        if len(key[1]) <= SANITIZE_SHORT_EXPR_MAX_CHARS:
            continue  # short interjections ("sí, sí", "que no, que no") are rhetoric
        positions.setdefault(key, []).append(index)

    drop: set = set()
    drops_by_key: dict = {}
    max_occurrences = 0
    for key, indexes in positions.items():
        if len(indexes) < 2:
            continue
        max_occurrences = max(max_occurrences, len(indexes))
        if len(indexes) >= 3:
            dropped = indexes[1:]
        elif indexes[1] - indexes[0] == 1:
            # An adjacent long double is degeneration. A non-adjacent one
            # (A, B, A.) is deliberate structure and is left alone.
            dropped = indexes[1:]
        else:
            continue
        drop.update(dropped)
        drops_by_key[key] = len(dropped)

    if not drop:
        return sentence, {}, max_occurrences

    kept = [f for i, f in enumerate(fragments) if i not in drop]
    # The dropped tail held the sentence terminator's slot, so promote the new
    # last fragment's clause delimiter back to that terminator.
    rebuilt = " ".join(kept).rstrip().rstrip(",;:")
    return rebuilt + terminator, drops_by_key, max_occurrences


def sanitize_clause_repetition(text: str) -> ClauseSanitizeResult:
    """Repair intra-sentence clause repetition and rate what was found.

    Pure: no I/O, no engine state, no history. Idempotent — a repaired text
    sanitizes to itself with verdict ``clean``.
    """
    original_len = len(text)
    if not text or not text.strip():
        return ClauseSanitizeResult(
            text=text, verdict="clean",
            original_len=original_len, remaining_len=original_len,
        )

    parts = _SENT_SPLIT_RE.split(text)
    drops_by_key: dict = {}
    max_occurrences = 0
    for i in range(0, len(parts), 2):
        repaired, drops, occurrences = _repair_sentence(parts[i])
        parts[i] = repaired
        max_occurrences = max(max_occurrences, occurrences)
        for key, count in drops.items():
            drops_by_key[key] = drops_by_key.get(key, 0) + count

    removed_fragments = sum(drops_by_key.values())
    if not removed_fragments:
        return ClauseSanitizeResult(
            text=text, verdict="clean",
            max_occurrences=max_occurrences,
            original_len=original_len, remaining_len=original_len,
        )

    result_text = "".join(parts)
    remaining_len = len(result_text)
    removed_pct = (original_len - remaining_len) / original_len if original_len else 0.0
    distinct_looping = sum(1 for count in drops_by_key.values() if count >= 2)
    length_axes_armed = original_len >= SANITIZE_LENGTH_AXES_FLOOR

    rejected = (
        removed_fragments >= SANITIZE_REJECT_FRAGMENTS
        or distinct_looping >= SANITIZE_REJECT_DISTINCT
        or (
            removed_fragments >= _REMOVED_PCT_MIN_DROPS
            and length_axes_armed
            and removed_pct >= SANITIZE_REJECT_REMOVED_PCT
        )
        or (length_axes_armed and remaining_len < SANITIZE_MIN_REMAINING_CHARS)
    )

    return ClauseSanitizeResult(
        text=result_text,
        verdict="rejected" if rejected else "repaired",
        removed_fragments=removed_fragments,
        distinct_looping=distinct_looping,
        max_occurrences=max_occurrences,
        original_len=original_len,
        remaining_len=remaining_len,
        removed_pct=removed_pct,
    )
