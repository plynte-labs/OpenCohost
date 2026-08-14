"""Stress tests for the streaming sentence splitter.

Covers real LLM streaming patterns: char-by-char deltas, unicode
punctuation, trailing incomplete text, multi-sentence bursts, and
abbreviation edge cases that a TTS pipeline will actually encounter.

The splitter must require whitespace (or explicit end-of-stream via
flush()) after a terminator before treating it as a sentence boundary —
matching production's `re.split(r'(?<=[.!?])\\s+', ...)` (B_ws). Without
that requirement, a bare terminator mid-token (a URL, an email, a phone
number, a decimal) gets shredded even though nothing follows it on the
same line yet.
"""

from opencohost.core.speech.sentence_splitter import SentenceSplitter


def test_streaming_splitter_buffers_abbreviations_and_spanish_questions():
    splitter = SentenceSplitter()

    assert splitter.feed("El Dr. Ramos llegó. ¿") == ["El Dr. Ramos llegó."]
    assert splitter.feed("Cómo estás? ") == ["¿Cómo estás?"]
    assert splitter.feed("Bien.") == []
    assert splitter.flush() == ["Bien."]


def test_streaming_splitter_ignores_common_spanish_abbreviations():
    splitter = SentenceSplitter()

    assert splitter.feed("Esto es ej. una prueba vs. ") == []
    assert splitter.feed("otra opción de la pág. 3. ") == ["Esto es ej. una prueba vs. otra opción de la pág. 3."]
    assert splitter.feed("Listo.") == []
    assert splitter.flush() == ["Listo."]


def test_char_by_char_streaming_like_real_llm():
    """Real LLMs send token-by-token, sometimes char-by-char."""
    splitter = SentenceSplitter()
    text = "Hola mundo. ¿Qué tal?"
    all_sentences: list[str] = []

    for char in text:
        all_sentences.extend(splitter.feed(char))
    all_sentences.extend(splitter.flush())

    assert all_sentences == ["Hola mundo.", "¿Qué tal?"]


def test_unicode_ellipsis_does_not_split():
    """Unicode … (U+2026) is NOT a sentence boundary — only ASCII . is."""
    splitter = SentenceSplitter()

    result = splitter.feed("Bueno… no sé. ")
    assert result == ["Bueno… no sé."]


def test_ascii_ellipsis_splits_after_the_final_dot_when_followed_by_space():
    """'...' is three terminator chars; only the last one is immediately
    followed by whitespace, so — matching B_ws — the split lands right
    after it, not before it."""
    splitter = SentenceSplitter()

    result = splitter.feed("Bueno... y entonces pasó algo. ")
    assert result == ["Bueno...", "y entonces pasó algo."]


def test_decimal_number_does_not_split():
    """A dot between digits has no whitespace after it — never a boundary."""
    splitter = SentenceSplitter()

    result = splitter.feed("Creo que son 3.5 segundos en total. ")
    assert result == ["Creo que son 3.5 segundos en total."]


def test_bare_domain_url_survives_as_one_chunk():
    """A dotted domain with no scheme (no 'https://') has no whitespace
    between its internal dots — must not be shredded into fragments that
    break the output_guard link-detection rule (R2)."""
    splitter = SentenceSplitter()

    assert splitter.feed("Mira www.misitio.com ahí está todo.") == []
    assert splitter.flush() == ["Mira www.misitio.com ahí está todo."]


def test_email_address_survives_as_one_chunk():
    """Must not be shredded into fragments that break the output_guard
    doxxing-detection rule (R1)."""
    splitter = SentenceSplitter()

    assert splitter.feed("Escríbele a soporte@midominio.com y listo.") == []
    assert splitter.flush() == ["Escríbele a soporte@midominio.com y listo."]


def test_dotted_phone_number_survives_as_one_chunk():
    """Must not be shredded into fragments that break the output_guard
    doxxing-detection rule (R1)."""
    splitter = SentenceSplitter()

    assert splitter.feed("Su teléfono es 555.123.4567 por si acaso.") == []
    assert splitter.flush() == ["Su teléfono es 555.123.4567 por si acaso."]


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
    ]
    assert splitter.flush() == ["Ahora sí."]


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
    assert splitter.feed("Hola. ") == ["Hola."]
    splitter.feed("")
    assert splitter.feed("Chau.") == []
    assert splitter.flush() == ["Chau."]


def test_sentence_boundary_split_across_two_feed_calls():
    """The terminator can land in one feed() call and its confirming
    whitespace in the next — the sentence must not complete until then."""
    splitter = SentenceSplitter()

    assert splitter.feed("Hola.") == []
    assert splitter.feed(" Mundo.") == ["Hola."]
    assert splitter.flush() == ["Mundo."]


def test_flush_emits_trailing_sentence_with_no_confirming_whitespace():
    """A stream's last sentence has nothing after its terminator — flush()
    is what releases it instead of feed() waiting forever."""
    splitter = SentenceSplitter()

    assert splitter.feed("Última frase sin espacio final.") == []
    assert splitter.flush() == ["Última frase sin espacio final."]


def test_flush_on_empty_buffer_returns_nothing():
    splitter = SentenceSplitter()

    assert splitter.flush() == []
