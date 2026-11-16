"""S1: the deterministic crisis reply slot and its use as the fallback."""
from __future__ import annotations

import pytest

from opencohost.config import warmth as warmth_state
from opencohost.config.validation import output_guard
from opencohost.core.safety.turn import crisis_turn
from opencohost.i18n import active as i18n_active
from opencohost.i18n.contract import NO_FALLBACK_SLOTS
from opencohost.i18n.registry import discover_bundles, official_locales_dir
from opencohost.i18n.startup import resolve_active_bundle


def _activate(locale):
    registry = discover_bundles(official_locales_dir(), "official")
    i18n_active.set_active_bundle(resolve_active_bundle(locale=locale, registry=registry))


@pytest.fixture(autouse=True)
def _reset():
    yield
    i18n_active.reset_active_bundle()
    warmth_state.set_active_warmth("cold")


def test_crisis_reply_is_a_no_fallback_slot():
    assert "llm.crisis_reply" in NO_FALLBACK_SLOTS


@pytest.mark.parametrize("locale,needle", [("es", "Soy una IA"), ("en", "I'm an AI")])
def test_official_locales_ship_a_crisis_reply(locale, needle):
    _activate(locale)
    text = i18n_active.crisis_reply()
    assert needle in text
    assert not any(ch.isdigit() for ch in text), "no phone numbers in the reply"
    assert "http" not in text.lower()


def test_es_reply_is_neutral_spanish_tuteo():
    _activate("es")
    text = i18n_active.crisis_reply().lower()
    for voseo in ("podés", "tenés", "sos ", "contame", "llamá", "hablá"):
        assert voseo not in text


@pytest.mark.parametrize("locale", ["es", "en"])
@pytest.mark.parametrize("level", list(warmth_state.WARMTH_LEVELS))
def test_crisis_reply_passes_the_guard_on_a_crisis_turn(locale, level):
    _activate(locale)
    warmth_state.set_active_warmth(level)
    with crisis_turn():
        allowed, reason = output_guard(i18n_active.crisis_reply(), source="direct")
    assert allowed, reason


def test_bundle_without_the_slot_degrades_to_builtin_english_not_another_locale():
    i18n_active.reset_active_bundle()
    text = i18n_active._CRISIS_REPLY_BUILTIN
    assert "AI" in text


def test_fallback_line_is_crisis_reply_on_a_crisis_turn():
    from opencohost.core.llm_engine import MotorVocalIA
    import queue

    _activate("es")
    motor = MotorVocalIA(queue.Queue(), lambda event: None)
    normal_lines = set(i18n_active.guardrail_fallback_lines())
    for reason in ("", "Non-negotiable violation [no_hate_speech]: x",
                   "Non-negotiable violation [no_ai_self_identification]: x",
                   "Non-negotiable violation [no_code_output]: x"):
        for source in ("direct", "ptt", "owner-bundle"):
            with crisis_turn():
                line = motor._guardrail_fallback_line(source, reason)
            assert line == i18n_active.crisis_reply()
            assert line not in normal_lines


def test_fallback_line_unchanged_on_a_normal_turn():
    from opencohost.core.llm_engine import MotorVocalIA
    import queue

    _activate("es")
    motor = MotorVocalIA(queue.Queue(), lambda event: None)
    assert motor._guardrail_fallback_line("direct", "") in set(
        i18n_active.guardrail_fallback_lines()
    )
