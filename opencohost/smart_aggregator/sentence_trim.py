"""Sentence-level trailing duplicate trimmer.

Pure module — no imports from the rest of the package, no mutable state.

Public API
----------
trim_trailing_repeated_sentences(text, recent_texts, threshold=0.85)
    -> tuple[str, bool]

The function splits *text* into sentences, then walks backward from the end
dropping any sentence that is a near-duplicate of a sentence already seen in
*recent_texts* or in previously kept sentences of the same *text*.

Near-duplicate is defined by difflib.SequenceMatcher ratio >= *threshold*
applied to lowercase, whitespace-collapsed strings.

Only TRAILING duplicates are removed.  A duplicate that appears in the middle
of *text* is never touched — the caller must not rely on this function to
de-dup the interior of an utterance.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher


# ---------------------------------------------------------------------------
# Sentence splitting
# ---------------------------------------------------------------------------

# Matches sentence-ending punctuation (., !, ?, …) possibly followed by
# whitespace, while keeping the delimiter attached to the preceding text.
_SENTENCE_END_RE = re.compile(r"(?<=[.!?…])\s+")


def _split_sentences(text: str) -> list[str]:
    """Split *text* into sentences at boundary punctuation.

    Returns a list of stripped, non-empty strings.  Keeps the punctuation
    attached to each sentence so callers can re-join with a single space.
    """
    # Primary split: after sentence-ending punctuation followed by whitespace
    parts = _SENTENCE_END_RE.split(text.strip())
    result: list[str] = []
    for part in parts:
        stripped = part.strip()
        if stripped:
            result.append(stripped)
    return result


def _normalize(sentence: str) -> str:
    """Lowercase and collapse internal whitespace for comparison."""
    return " ".join(sentence.lower().split())


def _is_near_dup(a: str, b: str, threshold: float) -> bool:
    """Return True if normalized *a* and *b* are near-duplicates."""
    na = _normalize(a)
    nb = _normalize(b)
    if not na or not nb:
        return False
    ratio = SequenceMatcher(None, na, nb, autojunk=False).ratio()
    return ratio >= threshold


def _ends_at_sentence_boundary(text: str) -> bool:
    """Return True when *text* ends with sentence-ending punctuation."""
    stripped = text.rstrip()
    return bool(stripped) and stripped[-1] in ".!?…"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def trim_trailing_repeated_sentences(
    text: str,
    recent_texts: list[str],
    threshold: float = 0.85,
) -> tuple[str, bool]:
    """Remove trailing near-duplicate sentences from *text*.

    Parameters
    ----------
    text:
        The candidate utterance to trim.
    recent_texts:
        Previously accepted utterances used as the duplicate reference pool.
    threshold:
        SequenceMatcher ratio at or above which a sentence is considered a
        near-duplicate (default 0.85 = 85 % similarity).

    Returns
    -------
    (trimmed_text, salvageable)
        *trimmed_text*: the original text with trailing dup sentences removed,
        or ``""`` if the entire text is a duplicate.
        *salvageable*: True only when *trimmed_text* is non-empty, ends at a
        sentence boundary, and contains at least one full sentence.

    Guarantees
    ----------
    - Only trailing sentences are removed; mid-text content is never altered.
    - The cut always happens at a sentence boundary (., !, ?, …).
    - Word-level or sub-sentence comparison is never performed.
    """
    if not text or not text.strip():
        return ("", False)

    sentences = _split_sentences(text)
    if not sentences:
        return ("", False)

    # Build a pool of normalized reference sentences from recent_texts.
    reference_sentences: list[str] = []
    for recent in (recent_texts or []):
        for sent in _split_sentences(recent):
            norm = _normalize(sent)
            if norm:
                reference_sentences.append(norm)

    # Walk backward from the end, dropping trailing dups.
    # We never touch sentences that are not at the current tail.
    keep_up_to = len(sentences)  # exclusive upper index of sentences to keep

    for i in range(len(sentences) - 1, -1, -1):
        sent = sentences[i]
        norm = _normalize(sent)
        if not norm:
            keep_up_to = i
            continue

        is_dup = any(
            _is_near_dup(norm, ref, threshold)
            for ref in reference_sentences
        )
        if not is_dup:
            # This sentence is NOT a dup — stop trimming here.
            break
        # It is a dup — exclude it from the kept range.
        keep_up_to = i

    kept = sentences[:keep_up_to]
    if not kept:
        return ("", False)

    trimmed = " ".join(kept)
    # `kept` is non-empty (guarded above); only sentence-boundary check is needed.
    salvageable = _ends_at_sentence_boundary(trimmed)
    return (trimmed, salvageable)
