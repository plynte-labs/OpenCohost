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
        for index, char in enumerate(self._buffer):
            if char not in ".?!":
                continue
            if char == "." and self._is_abbreviation(index):
                continue

            sentence = self._buffer[start : index + 1].strip()
            if sentence:
                completed.append(sentence)
            start = index + 1

        self._buffer = self._buffer[start:].lstrip()
        return completed

    def _is_abbreviation(self, period_index: int) -> bool:
        match = re.search(r"\b\w+\.$", self._buffer[: period_index + 1])
        return bool(match and match.group(0).lower() in self._ABBREVIATIONS)
