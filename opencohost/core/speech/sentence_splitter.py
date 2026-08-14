"""Small streaming sentence splitter used before TTS chunking."""

from __future__ import annotations

import re


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

    def __init__(self) -> None:
        self._buffer = ""

    def feed(self, delta: str) -> list[str]:
        self._buffer += delta
        completed: list[str] = []

        start = 0
        buffer_len = len(self._buffer)
        for index, char in enumerate(self._buffer):
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
        return [remainder] if remainder else []

    def _is_abbreviation(self, period_index: int) -> bool:
        match = re.search(r"\b\w+\.$", self._buffer[: period_index + 1])
        return bool(match and match.group(0).lower() in self._ABBREVIATIONS)
