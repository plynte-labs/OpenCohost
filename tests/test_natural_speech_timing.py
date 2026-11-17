"""Tests for natural speech timing, punctuation hierarchy, and bracket sanitization."""

import pytest
from opencohost.core.speech.speech_rate import calculate_speech_pause
from opencohost.core.speech.tts_sanitizer import _sanitize_tts_text_for_playback
from opencohost.core import llm_engine
SpeechPipelineMixin = llm_engine.SpeechPipelineMixin


def test_calculate_speech_pause_ellipsis():
    pause_dots = calculate_speech_pause("Pensando en eso...")
    assert 0.45 <= pause_dots <= 0.65

    pause_unicode = calculate_speech_pause("Pensando en eso…")
    assert 0.45 <= pause_unicode <= 0.65


def test_calculate_speech_pause_minor_clause():
    for punct in [",", ";", ":"]:
        pause = calculate_speech_pause(f"En primer lugar{punct}")
        assert 0.14 <= pause <= 0.24


def test_calculate_speech_pause_standard_sentence():
    for term in [".", "!", "?"]:
        pause = calculate_speech_pause(f"Esta es una oración corta{term}")
        assert 0.35 <= pause <= 0.50


def test_calculate_speech_pause_long_sentence():
    words = " ".join(f"palabra{i}" for i in range(22))
    long_sentence = f"{words}."
    pause = calculate_speech_pause(long_sentence)
    assert 0.45 <= pause <= 0.60


def test_calculate_speech_pause_fallback():
    pause = calculate_speech_pause("texto sin puntuacion final")
    assert 0.25 <= pause <= 0.38

    pause_empty = calculate_speech_pause("")
    assert 0.20 <= pause_empty <= 0.30


def test_sanitizer_cleans_conversational_parentheses_and_braces():
    # Conversational parentheses become commas for natural breathing
    assert _sanitize_tts_text_for_playback("Hola (amigo), ¿cómo estás?") == "Hola, amigo, ¿cómo estás?"
    assert _sanitize_tts_text_for_playback("Esto es importante (como ya sabes).") == "Esto es importante, como ya sabes."

    # Empty brackets and braces are removed
    assert _sanitize_tts_text_for_playback("Texto con () y {} vacíos.") == "Texto con y vacíos."

    # Code / dictionary braces are stripped
    assert _sanitize_tts_text_for_playback("Objeto {clave: valor} recibido.") == "Objeto clave: valor recibido."


def test_sanitizer_preserves_math_parentheses_and_ellipsis():
    # Math notation like O(N) or f(x) remains intact
    assert _sanitize_tts_text_for_playback("Complejidad O(N) y O(1).") == "Complejidad O(N) y O(1)."

    # Ellipsis is preserved as reflective pause punctuation
    assert _sanitize_tts_text_for_playback("No lo sé... tal vez mañana.") == "No lo sé... tal vez mañana."


def test_sentence_splitter_splits_on_ellipsis():
    sentences = SpeechPipelineMixin._sanitize_for_tts("Esperemos un momento... Ya está listo.")
    assert sentences == ["Esperemos un momento...", "Ya está listo."]


def test_fragmenter_subsplits_on_colons():
    long_sentence = "Primero preparamos todos los ingredientes necesarios para cocinar el plato de hoy: luego mezclamos suavemente la harina y el agua con mucho cuidado."
    chunks = SpeechPipelineMixin._fragment_for_tts([long_sentence])
    assert len(chunks) >= 2
    assert any(":" in c for c in chunks)
