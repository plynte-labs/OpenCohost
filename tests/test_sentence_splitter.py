"""Red tests for the future streaming sentence splitter contract."""

from core.sentence_splitter import SentenceSplitter


def test_streaming_splitter_buffers_abbreviations_and_spanish_questions():
    splitter = SentenceSplitter()

    assert splitter.feed("El Dr. Ramos llegó. ¿") == ["El Dr. Ramos llegó."]
    assert splitter.feed("Cómo estás? ") == ["¿Cómo estás?"]
    assert splitter.feed("Bien.") == ["Bien."]


def test_streaming_splitter_ignores_common_spanish_abbreviations():
    splitter = SentenceSplitter()

    assert splitter.feed("Esto es ej. una prueba vs. ") == []
    assert splitter.feed("otra opción de la pág. 3. ") == ["Esto es ej. una prueba vs. otra opción de la pág. 3."]
    assert splitter.feed("Listo.") == ["Listo."]
