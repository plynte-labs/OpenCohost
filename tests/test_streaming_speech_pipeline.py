"""Contract tests for the minimal streaming speech pipeline."""

from core.streaming_speech import StreamingSpeechPipeline


class FakeLLM:
    def __init__(self) -> None:
        self.completed = False

    def stream(self, prompt: str):
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
