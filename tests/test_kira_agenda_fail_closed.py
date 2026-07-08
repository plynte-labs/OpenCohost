"""Tests for the agenda fail-closed enable() gate + controller i18n wiring (P1b).

The ship-blocker fix: KiraAgendaController.enable() is the single chokepoint
through which agenda (autonomous) speech turns on (UI app_shell.py, API
engine_host.py both call it). Once the controller reads its character-contract
patterns from the active locale bundle instead of hard-coded Spanish class
constants, a locale that ships no guardrails.agenda.character_contract must
make enable() refuse -- agenda speaks unsupervised, so a missing guard set
must never run unguarded (design D2/D4, section 4).
"""
from __future__ import annotations

import logging
import queue

import pytest

from opencohost.core import llm_engine
from opencohost.i18n import active
from opencohost.i18n.contract import TIER_OFFICIAL, LocaleBundle
from opencohost.i18n.registry import (
    build_chain,
    discover_bundles,
    official_locales_dir,
    validate_bundle,
)
from opencohost.i18n.startup import _minimal_base, resolve_active_bundle
from opencohost.smart_aggregator.kira_agenda_controller import (
    AgendaState,
    ErrorCode,
    KiraAgendaController,
)


@pytest.fixture
def official():
    """The real on-disk official registry (es + en)."""
    return discover_bundles(official_locales_dir(), "official")


@pytest.fixture(autouse=True)
def _reset_active():
    active.reset_active_bundle()
    yield
    active.reset_active_bundle()


def _bare_bundle(code: str = "xx") -> LocaleBundle:
    return LocaleBundle(code=code, tier=TIER_OFFICIAL, data={"meta": {"code": code}})


def _activate(code, official):
    active.set_active_bundle(resolve_active_bundle(locale=code, registry=official))


# --- fail-closed gate (design section 4 + 8) --------------------------------

def test_bare_bundle_enable_refuses_and_sets_error(caplog):
    active.set_active_bundle(_bare_bundle("xx"))
    controller = KiraAgendaController()
    assert controller._character_patterns is None

    with caplog.at_level(logging.WARNING, logger="OpenCohost"):
        controller.enable()

    assert controller.state == AgendaState.OFF
    assert controller.recovery.error_code == ErrorCode.GUARDRAILS_MISSING
    assert any(r.levelno == logging.WARNING for r in caplog.records)
    assert "xx" in caplog.text


def test_es_bundle_enable_succeeds(official):
    _activate("es", official)
    controller = KiraAgendaController()
    assert controller._character_patterns is not None

    controller.enable()

    assert controller.state == AgendaState.IDLE
    assert controller.recovery.error_code == ErrorCode.NONE


def test_en_bundle_enable_succeeds(official):
    _activate("en", official)
    controller = KiraAgendaController()
    assert controller._character_patterns is not None

    controller.enable()

    assert controller.state == AgendaState.IDLE
    assert controller.recovery.error_code == ErrorCode.NONE


def test_minimal_base_startup_degrade_refuses_enable():
    # _minimal_base() is the last-resort inline bundle (startup.py) used when
    # even the shipped es bundle fails to load. It carries meta + tts only.
    active.set_active_bundle(_minimal_base())
    controller = KiraAgendaController()
    assert controller._character_patterns is None

    controller.enable()

    assert controller.state == AgendaState.OFF
    assert controller.recovery.error_code == ErrorCode.GUARDRAILS_MISSING


def test_soft_stop_unaffected_while_gate_refused():
    active.set_active_bundle(_bare_bundle())
    controller = KiraAgendaController()
    controller.enable()
    assert controller.state == AgendaState.OFF

    action = controller.soft_stop()

    assert controller.state == AgendaState.OFF
    assert action is not None


def test_emergency_stop_unaffected_while_gate_refused():
    active.set_active_bundle(_bare_bundle())
    controller = KiraAgendaController()
    controller.enable()
    assert controller.state == AgendaState.OFF

    controller.emergency_stop()  # must not raise

    assert controller.state == AgendaState.OFF


def test_guardrails_missing_error_code_has_a_human_label():
    assert ErrorCode.GUARDRAILS_MISSING.human()


def _bundle_with_character_contract(character_contract: dict, banned_closures=("x",)) -> LocaleBundle:
    guardrails = {"character_contract": character_contract}
    if banned_closures is not None:
        guardrails["banned_closures"] = list(banned_closures)
    return LocaleBundle(
        code="xx",
        tier=TIER_OFFICIAL,
        data={"meta": {"code": "xx"}, "guardrails": {"agenda": guardrails}},
    )


def test_unknown_category_keys_compile_to_none_not_empty_tuple():
    # A community-authored bundle shipping only unknown/typo'd category keys
    # must fail-closed (None), not silently produce an empty-but-truthy tuple
    # that slips past `enable()`'s `is None` check.
    active.set_active_bundle(
        _bundle_with_character_contract({"promptleak_typo": ["oops"]})
    )
    assert KiraAgendaController._compile_character_patterns() is None


def test_partial_category_set_compiles_to_none():
    # Only one of the five required categories present — must still refuse.
    active.set_active_bundle(
        _bundle_with_character_contract({"meta_question": ["quieres"]})
    )
    assert KiraAgendaController._compile_character_patterns() is None


def test_partial_category_set_refuses_enable():
    active.set_active_bundle(
        _bundle_with_character_contract({"meta_question": ["quieres"]})
    )
    controller = KiraAgendaController()
    assert controller._character_patterns is None

    controller.enable()

    assert controller.state == AgendaState.OFF
    assert controller.recovery.error_code == ErrorCode.GUARDRAILS_MISSING


def test_malformed_regex_compiles_to_none_instead_of_raising():
    # An invalid regex string (unterminated subpattern) in a bundle must not
    # crash controller construction / app boot — it must fail-closed.
    full_categories = {
        category: ["("]
        for category in KiraAgendaController._CHARACTER_CONTRACT_CATEGORIES
    }
    active.set_active_bundle(_bundle_with_character_contract(full_categories))

    assert KiraAgendaController._compile_character_patterns() is None
    # Construction itself must not raise re.error.
    controller = KiraAgendaController()
    assert controller._character_patterns is None


def test_malformed_regex_refuses_enable_instead_of_crashing_boot():
    full_categories = {
        category: ["("]
        for category in KiraAgendaController._CHARACTER_CONTRACT_CATEGORIES
    }
    active.set_active_bundle(_bundle_with_character_contract(full_categories))
    controller = KiraAgendaController()

    controller.enable()

    assert controller.state == AgendaState.OFF
    assert controller.recovery.error_code == ErrorCode.GUARDRAILS_MISSING


def test_missing_banned_closures_refuses_enable_even_with_full_character_contract():
    # design D2: the gated slot is "validator patterns + banned closures".
    # A full character_contract with no banned_closures must still refuse.
    full_categories = {
        category: ["some pattern"]
        for category in KiraAgendaController._CHARACTER_CONTRACT_CATEGORIES
    }
    active.set_active_bundle(
        _bundle_with_character_contract(full_categories, banned_closures=None)
    )
    controller = KiraAgendaController()
    assert controller._character_patterns is not None

    controller.enable()

    assert controller.state == AgendaState.OFF
    assert controller.recovery.error_code == ErrorCode.GUARDRAILS_MISSING


# --- strict-gate CI (parent track T5 exit criterion) ------------------------

def test_official_es_and_en_bundles_pass_strict_validation(official):
    es = build_chain("es", official)
    en = build_chain("en", official)

    assert validate_bundle(es, strict=True).ok
    assert validate_bundle(en, strict=True).ok


# --- llm_engine banned-closures i18n swap -----------------------------------

def _make_motor():
    return llm_engine.MotorVocalIA(queue.Queue(), lambda event: None)


def test_sanitizer_uses_active_locale_banned_closures_en(official):
    _activate("en", official)
    motor = _make_motor()

    sanitized = motor._sanitize_agenda_output(
        "Well, that's it for today. See you next time!"
    )

    assert "see you next time" not in sanitized.lower()


def test_sanitizer_skips_banned_check_when_locale_has_none():
    active.set_active_bundle(_bare_bundle())
    motor = _make_motor()

    text = "Nos vemos, hasta luego."
    sanitized = motor._sanitize_agenda_output(text)

    # No guardrails.agenda.banned_closures for this locale -> the check is
    # skipped (agenda can't be enabled in this locale anyway), text passes
    # through unchanged rather than silently being replaced.
    assert sanitized == text
