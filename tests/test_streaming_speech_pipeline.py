"""Contract tests for the streaming speech pipeline.

Covers the TTFA (time-to-first-audio) contract and stress scenarios
that users will hit: empty streams, long bursts, trailing text, and
token-by-token delivery.
"""

from opencohost.core.streaming_speech import StreamingSpeechPipeline


class FakeLLM:
    def __init__(self, deltas=None) -> None:
        self.completed = False
        self._deltas = deltas

    def stream(self, prompt: str):
        if self._deltas is not None:
            for delta in self._deltas:
                yield delta
            self.completed = True
            return
        assert prompt == "saludo"
        yield "Hola mundo."
        yield " Después seguimos."
        self.completed = True


class FakePlayback:
    def __init__(self, llm: FakeLLM) -> None:
        self.llm = llm
        self.events = []

    def speak(self, sentence: str) -> None:
        self.events.append(("speak", sentence, self.llm.completed))


def test_speaks_first_completed_sentence_before_llm_stream_finishes():
    llm = FakeLLM()
    playback = FakePlayback(llm)
    pipeline = StreamingSpeechPipeline(llm=llm, playback=playback)

    pipeline.run("saludo")

    assert playback.events == [
        ("speak", "Hola mundo.", False),
        ("speak", "Después seguimos.", False),
    ]
    assert llm.completed is True


def test_empty_stream_produces_no_playback():
    """If LLM returns nothing, playback must not be called."""
    llm = FakeLLM(deltas=[])
    playback = FakePlayback(llm)
    pipeline = StreamingSpeechPipeline(llm=llm, playback=playback)

    pipeline.run("silencio")

    assert playback.events == []
    assert llm.completed is True


def test_long_burst_of_sentences_in_single_delta():
    """LLM returns 10+ sentences in one delta — all must reach playback."""
    sentences = [f"Oración número {i}." for i in range(1, 21)]
    text = " ".join(sentences)
    llm = FakeLLM(deltas=[text])
    playback = FakePlayback(llm)
    pipeline = StreamingSpeechPipeline(llm=llm, playback=playback)

    pipeline.run("burst")

    assert len(playback.events) == 20
    for i, event in enumerate(playback.events, 1):
        assert event[0] == "speak"
        assert event[1] == f"Oración número {i}."
        assert event[2] is False  # LLM not yet completed when spoken


def test_char_by_char_delivery():
    """Simulate token-by-token LLM delivery — TTS still gets full sentences."""
    text = "Hola mundo. Chau."
    llm = FakeLLM(deltas=list(text))  # One char per delta
    playback = FakePlayback(llm)
    pipeline = StreamingSpeechPipeline(llm=llm, playback=playback)

    pipeline.run("charstream")

    spoken_texts = [e[1] for e in playback.events]
    assert "Hola mundo." in spoken_texts
    assert "Chau." in spoken_texts

