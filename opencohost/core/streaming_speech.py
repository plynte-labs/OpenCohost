"""Minimal streaming speech pipeline contract."""

from __future__ import annotations

from opencohost.core.sentence_splitter import SentenceSplitter


class StreamingSpeechPipeline:
    """Send completed streamed sentences to playback as soon as they exist."""

    def __init__(self, llm, playback) -> None:
        self._llm = llm
        self._playback = playback
        self._splitter = SentenceSplitter()

    def run(self, prompt: str) -> None:
        for delta in self._llm.stream(prompt):
            for sentence in self._splitter.feed(delta):
                self._playback.speak(sentence)
