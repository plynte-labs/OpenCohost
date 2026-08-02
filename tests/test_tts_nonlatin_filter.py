"""Unit 1.2 (runtime_findings_batch_20260731) — TTS non-Latin filter.

Defect (2026-07-30 run): a cloud model emitted CJK glyphs (e.g. "承认"). The
screen showed the glyph; espeak-ng then generated a spoken *description* of
the glyph from the character itself, so Kira spoke a Spanish gloss aloud.
That description is not in our text -- there is no marker to filter, only
raw characters to strip before espeak/Piper ever sees them.

Owner ruling: the screen keeps the characters, the speech drops them; never
speak a placeholder.

`_sanitize_tts_text_for_playback` runs inside `_hablar_impl`, AFTER
`_emit_dialogue` already forwarded the ORIGINAL string to the screen sink
(llm_engine.py _ejecutar_inferencia: _emit_dialogue at the emit site, then
_hablar). That ordering is what gives the screen/speech split for free.
"""
from __future__ import annotations

import logging
import queue
from unittest.mock import MagicMock

import pytest

from opencohost.core import llm_engine
from opencohost.core.llm_engine import MotorVocalIA
from tests.test_tts_local_only_switch import _drive_hablar, _make_mock_piper, _make_motor


def _resp(text):
    return {"message": {"content": text}}


def _motor():
    return MotorVocalIA(queue.Queue(), lambda event: None)


# ---------------------------------------------------------------------------
# Table-driven: strip everything non-Latin, by script
# ---------------------------------------------------------------------------

STRIP_CASES = [
    ("cjk_inline", "Ella dijo 承认 y siguió", "Ella dijo y siguió"),
    ("cjk_alone", "承认", ""),
    ("kana", "Hola こんにちは mundo", "Hola mundo"),
    ("hangul", "Hola 안녕하세요 mundo", "Hola mundo"),
    ("arabic", "Hola مرحبا mundo", "Hola mundo"),
    ("greek", "Hola αβγ mundo", "Hola mundo"),
    ("cyrillic", "Hola привет mundo", "Hola mundo"),
    ("emoji", "Hola 😀 mundo", "Hola mundo"),
    ("box_drawing", "Hola ┌─┐ mundo", "Hola mundo"),
    ("mixed_multi_script", "A 你好 B привет C مرحبا D", "A B C D"),
]


@pytest.mark.parametrize("text,expected", [(c[1], c[2]) for c in STRIP_CASES],
                         ids=[c[0] for c in STRIP_CASES])
def test_strips_non_latin_script(text, expected):
    motor = _motor()
    assert motor._sanitize_tts_text_for_playback(text) == expected


def test_all_foreign_chars_return_empty_string():
    motor = _motor()
    assert motor._sanitize_tts_text_for_playback("承认안녕مرحبا") == ""


def test_none_input_returns_empty_string():
    motor = _motor()
    assert motor._sanitize_tts_text_for_playback(None) == ""


# ---------------------------------------------------------------------------
# Latin/Spanish passthrough — must be byte-identical (and object-identical:
# the fast path shared with the pre-existing markdown stage)
# ---------------------------------------------------------------------------

def test_spanish_accents_and_punctuation_pass_through_identical():
    motor = _motor()
    text = "¿Cómo estás, ñoño? ¡Qué bien, muchísimas gracias! Aquí hay «comillas» — así."
    assert motor._sanitize_tts_text_for_playback(text) is text


def test_no_foreign_chars_returns_identical_object():
    motor = _motor()
    text = "Respuesta normal sin caracteres raros."
    assert motor._sanitize_tts_text_for_playback(text) is text


def test_ascii_math_symbols_still_untouched_by_new_filter():
    """Regression guard: `=` `%` `$` are ASCII and must keep reaching espeak
    untouched, exactly like the pre-existing
    test_tts_sanitizer_keeps_math_and_code_like_asterisks pins."""
    motor = _motor()
    text = "Cinco por diez es 5*10=50. Descuento del 20% por $5."
    assert motor._sanitize_tts_text_for_playback(text) == text


# ---------------------------------------------------------------------------
# Verbalization allowlist (tiny, conservative)
# ---------------------------------------------------------------------------

def test_verbalizes_plus_minus():
    motor = _motor()
    assert motor._sanitize_tts_text_for_playback("La tolerancia es 5±2 grados") == (
        "La tolerancia es 5 más menos 2 grados"
    )


def test_verbalizes_multiplication_sign():
    motor = _motor()
    assert motor._sanitize_tts_text_for_playback("3×4 es 12") == "3 por 4 es 12"


def test_verbalizes_division_sign():
    motor = _motor()
    assert motor._sanitize_tts_text_for_playback("10÷2 es 5") == "10 entre 2 es 5"


# ---------------------------------------------------------------------------
# Cleanup: no dangling punctuation / orphaned clauses
# ---------------------------------------------------------------------------

def test_dangling_comma_from_stripped_clause_is_cleaned():
    motor = _motor()
    out = motor._sanitize_tts_text_for_playback("Dijo 你好 , y siguió hablando")
    assert out == "Dijo, y siguió hablando"


def test_orphaned_clause_between_periods_collapses():
    motor = _motor()
    out = motor._sanitize_tts_text_for_playback(
        "Fin del tema. 你好. Continuando con la agenda."
    )
    assert out == "Fin del tema. Continuando con la agenda."


def test_leading_glyph_leaves_no_leading_comma():
    motor = _motor()
    out = motor._sanitize_tts_text_for_playback("你好, hola equipo")
    assert out == "hola equipo"


# ---------------------------------------------------------------------------
# Metadata-only telemetry — counts and category names, never the text
# ---------------------------------------------------------------------------

def test_debug_log_emits_metadata_only_never_the_stripped_text(caplog):
    motor = _motor()
    with caplog.at_level(logging.DEBUG, logger="OpenCohost"):
        motor._sanitize_tts_text_for_playback("Hola 你好 mundo")

    records = [r.getMessage() for r in caplog.records if "[TTS_SANITIZE]" in r.getMessage()]
    assert len(records) == 1
    message = records[0]
    assert "你好" not in message
    assert "chars=2" in message
    assert "cjk" in message


def test_debug_log_silent_on_clean_turn(caplog):
    motor = _motor()
    with caplog.at_level(logging.DEBUG, logger="OpenCohost"):
        motor._sanitize_tts_text_for_playback("Todo tranquilo por acá.")

    assert not [r for r in caplog.records if "[TTS_SANITIZE]" in r.getMessage()]


# ---------------------------------------------------------------------------
# Screen/speech split — _emit_dialogue gets the ORIGINAL string; the text
# that actually reaches Piper synthesis is the filtered one.
# ---------------------------------------------------------------------------

def test_screen_keeps_glyphs_speech_strips_them():
    spy = MagicMock()
    motor = MotorVocalIA(queue.Queue(), lambda event: None, dialogue_callback=spy)
    motor.ollama = MagicMock()
    motor.pygame = MagicMock()
    motor.pygame.mixer.music.get_busy.return_value = False
    motor.is_ready = True
    motor.current_model = "llama3"
    motor._reasoning_model_cache["llama3"] = False
    motor._piper = _make_mock_piper(available=True, synthesize_ok=True)
    motor.motor_tts = "ligero"
    motor.tts_local_only = True  # forces Piper, no Edge-TTS network needed

    dialogo = "Ella dijo 承认 y siguió"
    motor._ollama_chat = MagicMock(return_value=_resp(dialogo))

    motor._ejecutar_inferencia("hola", source="chat")

    # Screen: _emit_dialogue got the untouched original string.
    spy.assert_called_once_with(dialogo, "kira")

    # Speech: Piper never saw the CJK glyphs.
    synth_texts = [c.args[0] for c in motor._piper.synthesize.call_args_list]
    assert synth_texts, "Piper.synthesize was never called"
    combined = " ".join(synth_texts)
    assert "承认" not in combined
    assert "Ella dijo" in combined and "siguió" in combined


# ---------------------------------------------------------------------------
# Empty-after-stripping must skip playback cleanly, not crash
# ---------------------------------------------------------------------------

def test_all_foreign_text_skips_playback_without_crashing():
    motor, *_ = _make_motor()
    motor._piper = _make_mock_piper(available=True, synthesize_ok=True)

    loaded, piper, communicate = _drive_hablar(motor, "承认")

    assert loaded == []
    piper.synthesize.assert_not_called()
    assert motor._speaking is False
