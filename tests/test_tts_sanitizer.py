"""Tests for TTS sanitizer localization and math symbol verbalization.

Verifies:
1. Spanish verbalization maps: ±, ×, ÷, →, ➔, ➜, ⇒, ←.
2. English verbalization maps: ±, ×, ÷, →, ➔, ➜, ⇒, ←.
3. Default behavior preserves Spanish verbalization for existing callers.
4. Active bundle integration and explicit lang parameter.
"""

from opencohost.core.speech.tts_sanitizer import (
    _TTS_MATH_SYMBOL_VERBALIZATION_EN,
    _TTS_MATH_SYMBOL_VERBALIZATION_ES,
    sanitize_for_tts,
    _sanitize_tts_text_for_playback,
)
from opencohost.i18n import active as i18n_active
from opencohost.i18n.startup import resolve_active_bundle


def test_spanish_math_symbol_verbalization():
    """Verify Spanish verbalization mapping matches expected translations."""
    assert _TTS_MATH_SYMBOL_VERBALIZATION_ES["±"] == " más menos "
    assert _TTS_MATH_SYMBOL_VERBALIZATION_ES["×"] == " por "
    assert _TTS_MATH_SYMBOL_VERBALIZATION_ES["÷"] == " entre "
    assert _TTS_MATH_SYMBOL_VERBALIZATION_ES["→"] == " pasa a "
    assert _TTS_MATH_SYMBOL_VERBALIZATION_ES["➔"] == " pasa a "
    assert _TTS_MATH_SYMBOL_VERBALIZATION_ES["➜"] == " pasa a "
    assert _TTS_MATH_SYMBOL_VERBALIZATION_ES["⇒"] == " implica "
    assert _TTS_MATH_SYMBOL_VERBALIZATION_ES["←"] == " viene de "

    res = sanitize_for_tts("Tolerancia ±2, 3×4, 10÷2, A→B, C➔D, E➜F, P⇒Q, X←Y", lang="es")
    assert "más menos" in res
    assert "por" in res
    assert "entre" in res
    assert "pasa a" in res
    assert "implica" in res
    assert "viene de" in res


def test_english_math_symbol_verbalization():
    """Verify English verbalization mapping matches expected translations."""
    assert _TTS_MATH_SYMBOL_VERBALIZATION_EN["±"] == " plus or minus "
    assert _TTS_MATH_SYMBOL_VERBALIZATION_EN["×"] == " times "
    assert _TTS_MATH_SYMBOL_VERBALIZATION_EN["÷"] == " divided by "
    assert _TTS_MATH_SYMBOL_VERBALIZATION_EN["→"] == " leads to "
    assert _TTS_MATH_SYMBOL_VERBALIZATION_EN["➔"] == " leads to "
    assert _TTS_MATH_SYMBOL_VERBALIZATION_EN["➜"] == " leads to "
    assert _TTS_MATH_SYMBOL_VERBALIZATION_EN["⇒"] == " implies "
    assert _TTS_MATH_SYMBOL_VERBALIZATION_EN["←"] == " comes from "

    res = sanitize_for_tts("Tolerance ±2, 3×4, 10÷2, A→B, C➔D, E➜F, P⇒Q, X←Y", lang="en")
    assert "plus or minus" in res
    assert "times" in res
    assert "divided by" in res
    assert "leads to" in res
    assert "implies" in res
    assert "comes from" in res


def test_default_call_without_lang_uses_spanish():
    """Callers omitting lang default to Spanish verbalization."""
    i18n_active.reset_active_bundle()
    res = sanitize_for_tts("La tolerancia es 5±2 grados")
    assert res == "La tolerancia es 5 más menos 2 grados"

    res_mult = _sanitize_tts_text_for_playback("3×4 es 12")
    assert res_mult == "3 por 4 es 12"


def test_active_bundle_detected_when_lang_omitted():
    """When active bundle is set to English, sanitize_for_tts adapts automatically."""
    try:
        i18n_active.set_active_bundle(resolve_active_bundle(locale="en"))
        res = sanitize_for_tts("Tolerance 5±2, step A → step B")
        assert "plus or minus" in res
        assert "leads to" in res
    finally:
        i18n_active.reset_active_bundle()


def test_explicit_lang_overrides_active_bundle():
    """Passing lang explicitly overrides the active bundle."""
    try:
        i18n_active.set_active_bundle(resolve_active_bundle(locale="en"))
        # Force Spanish via lang argument
        res = sanitize_for_tts("5±2", lang="es")
        assert "más menos" in res
    finally:
        i18n_active.reset_active_bundle()
