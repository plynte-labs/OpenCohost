"""Stress tests for the streaming sentence splitter.

Covers real LLM streaming patterns: char-by-char deltas, unicode
punctuation, trailing incomplete text, multi-sentence bursts, and
abbreviation edge cases that a TTS pipeline will actually encounter.
"""

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


def test_char_by_char_streaming_like_real_llm():
    """Real LLMs send token-by-token, sometimes char-by-char."""
    splitter = SentenceSplitter()
    text = "Hola mundo. ¿Qué tal?"
    all_sentences: list[str] = []

    for char in text:
        all_sentences.extend(splitter.feed(char))

    assert all_sentences == ["Hola mundo.", "¿Qué tal?"]


def test_unicode_ellipsis_does_not_split():
    """Unicode … (U+2026) is NOT a sentence boundary — only ASCII . is."""
    splitter = SentenceSplitter()

    result = splitter.feed("Bueno… no sé. ")
    assert result == ["Bueno… no sé."]


def test_multiple_sentences_in_single_delta():
    """LLM sometimes returns multiple complete sentences in one token burst."""
    splitter = SentenceSplitter()

    result = splitter.feed("Primera. Segunda. Tercera! Cuarta? ")
    assert result == ["Primera.", "Segunda.", "Tercera!", "Cuarta?"]


def test_trailing_incomplete_text_stays_buffered():
    """Text without terminal punctuation must stay in the buffer."""
    splitter = SentenceSplitter()

    assert splitter.feed("Esto es una ") == []
    assert splitter.feed("frase incompleta que sigue ") == []
    assert splitter.feed("y sigue") == []
    assert splitter.feed(". Ahora sí.") == [
        "Esto es una frase incompleta que sigue y sigue.",
        "Ahora sí.",
    ]


def test_exclamation_and_question_marks_as_boundaries():
    """Both ! and ? must split, including inverted Spanish punctuation."""
    splitter = SentenceSplitter()

    result = splitter.feed("¡Increíble! ¿Verdad? Sí. ")
    assert result == ["¡Increíble!", "¿Verdad?", "Sí."]


def test_empty_deltas_do_not_corrupt_state():
    """Empty strings between real deltas must not break anything."""
    splitter = SentenceSplitter()

    splitter.feed("")
    splitter.feed("")
    assert splitter.feed("Hola.") == ["Hola."]
    splitter.feed("")
    assert splitter.feed(" Chau.") == ["Chau."]
