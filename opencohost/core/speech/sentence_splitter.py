"""Small streaming sentence splitter used before TTS chunking."""

from __future__ import annotations

import re

# A sentence terminator, plus any closing quotes/brackets glued to it.
_TERMINATOR_RE = re.compile(r"[.!?…][\"'»”’)\]]*")
# K1 (warmth-guardrails-runtime-20261006): markdown scaffolding that can end
# on a terminator ("### Fase 1.", "2.") but is not a sentence of its own.
_HEADING_LINE_RE = re.compile(r"^\s{0,3}#{1,6}\s")
_LIST_MARKER_ONLY_RE = re.compile(r"^(?:[-*+•]|\d{1,3}[.)])$")


def trim_to_last_sentence(text: str) -> str:
    """Cut an unterminated tail off *text* (live-session-fixes-20261005 L2).

    Used when the model stopped on its token limit (``done_reason == "length"``):
    the last sentence was cut mid-way and must not be spoken or shown. A text
    that already ends on a terminator is returned whole (stripped); otherwise
    it is cut after the last terminator that is followed by whitespace, the
    same boundary `SentenceSplitter.feed` confirms. No terminator at all: "".

    A trailing markdown heading or bare list marker left after that cut is
    dropped too, and the remainder is trimmed again.
    """
    kept = _trim_unterminated_tail(text)
    while kept:
        head, _, last = kept.rpartition("\n")
        if not (_HEADING_LINE_RE.match(last) or _LIST_MARKER_ONLY_RE.match(last.strip())):
            break
        kept = _trim_unterminated_tail(head)
    return kept


def _trim_unterminated_tail(text: str) -> str:
    stripped = (text or "").rstrip()
    if not stripped:
        return ""
    last_end = None
    for match in _TERMINATOR_RE.finditer(stripped):
        end = match.end()
        if end == len(stripped):
            return stripped
        if stripped[end].isspace():
            last_end = end
    return stripped[:last_end] if last_end is not None else ""


class SentenceSplitter:
    """Buffer text deltas and emit only completed sentence chunks."""

    _ABBREVIATIONS = {
        "dr.",
        "dra.",
        "ej.",
        "etc.",
        "pág.",
        "prof.",
        "sr.",
        "sra.",
        "srta.",
        "vs.",
    }

    # tts-first-chunk-boost-20261015: soft marks that may end the FIRST chunk.
    _SOFT_MARKS = ",;:—"
    _SOFT_MIN_WORDS = 4

    def __init__(self, first_chunk_boost: bool = False) -> None:
        self._buffer = ""
        self._boost = first_chunk_boost
        self._emitted = False
        # Boundary kind ("soft"/"sentence") of each chunk returned by the last
        # feed()/flush(), parallel to the returned list.
        self.last_boundaries: list[str] = []

    def feed(self, delta: str) -> list[str]:
        self._buffer += delta
        completed: list[str] = []
        self.last_boundaries = []

        start = 0
        buffer_len = len(self._buffer)
        for index, char in enumerate(self._buffer):
            if self._boost and not self._emitted and char in self._SOFT_MARKS:
                next_index = index + 1
                if (
                    next_index < buffer_len
                    and self._buffer[next_index].isspace()
                    and not self._newline_follows(next_index)
                    and self._is_soft_boundary(start, index)
                ):
                    chunk = self._buffer[start:next_index].strip()
                    if chunk:
                        completed.append(chunk)
                        self.last_boundaries.append("soft")
                        self._emitted = True
                    start = next_index
                continue
            if char not in ".?!":
                continue
            if char == "." and self._is_abbreviation(index):
                continue

            # Require whitespace right after the terminator, matching
            # production's B_ws (`re.split(r'(?<=[.!?])\s+', ...)`). Without
            # this, a dot mid-token (a URL, an email, a decimal, a phone
            # number) gets treated as a sentence end and shreds the token.
            next_index = index + 1
            if next_index >= buffer_len:
                # Terminator sits at the edge of what we've received so
                # far — can't tell yet if it's mid-token or a real
                # boundary. Wait for more data, or flush() at end of stream.
                continue
            if not self._buffer[next_index].isspace():
                continue

            sentence = self._buffer[start:next_index].strip()
            if sentence:
                completed.append(sentence)
                self.last_boundaries.append("sentence")
                self._emitted = True
            start = next_index

        self._buffer = self._buffer[start:].lstrip()
        return completed

    def flush(self) -> list[str]:
        """Release whatever is left in the buffer as the final sentence.

        Call this once the stream has ended: the last sentence's terminator
        has no following whitespace for feed() to confirm the boundary with.
        """
        remainder = self._buffer.strip()
        self._buffer = ""
        self.last_boundaries = ["sentence"] if remainder else []
        return [remainder] if remainder else []

    def discard(self) -> str:
        """Drop the unterminated remainder instead of releasing it (a stream
        cut by its token limit). Returns what was dropped."""
        remainder, self._buffer = self._buffer.strip(), ""
        return remainder

    def _newline_follows(self, index: int) -> bool:
        """True when the whitespace run starting at *index* holds a line break.
        A newline is structural (heading, list), so it is never a soft join."""
        end = index
        while end < len(self._buffer) and self._buffer[end].isspace():
            end += 1
        run = self._buffer[index:end]
        return "\n" in run or "\r" in run

    def _is_soft_boundary(self, start: int, mark_index: int) -> bool:
        """A soft mark qualifies with >= 4 words before it and no abbreviation
        or digit-comma-digit (decimal) context."""
        head = self._buffer[start:mark_index]
        words = head.split()
        if "\n" in head or "\r" in head:
            # a line break inside the head is structure (list, heading): no boost
            return False
        if len(words) < self._SOFT_MIN_WORDS:
            return False
        if self._buffer[mark_index] == "," and head[-1:].isdigit():
            # "1, 5" is not a decimal, but a number-then-comma boundary is
            # ambiguous in speech ("hasta el 3, 4 veces"); stay conservative.
            return False
        last = words[-1].lower()
        return f"{last}." not in self._ABBREVIATIONS and last not in self._ABBREVIATIONS

    def _is_abbreviation(self, period_index: int) -> bool:
        match = re.search(r"\b\w+\.$", self._buffer[: period_index + 1])
        return bool(match and match.group(0).lower() in self._ABBREVIATIONS)
