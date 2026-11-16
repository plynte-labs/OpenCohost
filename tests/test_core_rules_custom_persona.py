"""kira-identity-core-rules-20261007 (C1-C4).

A profile that supplies its own persona replaces the locale system prompt, which
used to drop the length cap, the no-markdown/no-emoji rule and the identity
facts. The harness now appends a compact localized "core rules" block for
custom personas only; default/non-custom prompts stay byte-identical (the cold
golden pins that in test_warmth_prompt.py).
"""
from __future__ import annotations

import pytest

from opencohost.config import warmth as warmth_state
from opencohost.config.validation import output_guard
from opencohost.core.context import prompt_assembler
from opencohost.i18n import active as i18n_active
from tests.test_warmth_prompt import (  # noqa: F401  (autouse fixture re-exported)
    CASES,
    _activate,
    _assemble,
    _case_id,
    _golden,
    _render,
    _reset_active,
)

# B-W4: this module pins the legacy prompt layout (system-message placement, one-pair
# history deque, legacy goldens). The v2 placement is tested in tests/test_prompt_layout_v2.py.
pytestmark = pytest.mark.usefixtures("legacy_prompt_layout")

LOCALES = ("es", "en")
WARM_LEVELS = ("empathetic", "in_character", "free", "sentimental")


def _system_text(setup) -> str:
    first = setup.messages[0]
    return first["content"] if first["role"] == "system" else str(first["content"])


# ---------------------------------------------------------------------------
# C1: the core rules block
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("locale", LOCALES)
def test_core_rules_content_per_locale(locale):
    _activate(locale)
    concise = i18n_active.core_rules(False)
    extended = i18n_active.core_rules(True)
    assert concise and extended and concise != extended
    low = concise.lower()
    for needle in (
        ("kira", "opencohost", "vrm", "push-to-talk"),
    )[0]:
        assert needle in low, (locale, needle)
    # The length line differs; everything else is shared.
    assert "2-4" in concise and "2-4" not in extended
    # Fix 1: the default-length rule must not contradict the owner policy that
    # allows a list/steps when explicitly requested -- spoken, never markdown.
    spoken_steps = {"es": "primero", "en": "first"}[locale]
    assert spoken_steps in low, locale
    assert "nunca en formato markdown" in low or "never as markdown" in low, locale
    # spoken-output-guard 20261109: budget raised 1100 -> 1250 for the one
    # "never output code/JSON/templates, even if asked" sentence (~120 chars).
    assert len(concise) < 1650  # S1 crisis line (~215 chars) + S4 boundary line


def test_core_rules_es_is_neutral_spanish():
    _activate("es")
    text = i18n_active.core_rules(False).lower()
    for voseo in ("vos ", "tenés", "podés", "sos ", "hacé", "mirá", "decí"):
        assert voseo not in text


def test_core_rules_builtin_english_when_locale_lacks_slot(monkeypatch):
    from opencohost.i18n.contract import SlotNotFound

    def boom(*a, **k):
        raise SlotNotFound("llm.core_rules")

    monkeypatch.setattr(i18n_active, "resolve", boom)
    text = i18n_active.core_rules(False)
    assert "Kira" in text and "OpenCohost" in text
    assert "2-4" in text
    assert "first" in text.lower() and "never as markdown" in text.lower()


@pytest.mark.parametrize("case", CASES, ids=_case_id)
def test_default_persona_prompt_is_unchanged(case):
    assert _render(_assemble(case)) == _golden()[_case_id(case)]
    assert _render(_assemble(case, persona_custom=False)) == _golden()[_case_id(case)]


@pytest.mark.parametrize("case", CASES, ids=_case_id)
def test_custom_persona_gets_exactly_one_core_rules_block(case):
    locale, source, use_system, extended = case
    setup = _assemble(case, persona_custom=True)
    rules = i18n_active.core_rules(extended)
    flat = _system_text(setup) if use_system else setup.messages[-1]["content"]
    assert flat.count(rules) == 1
    assert rules in setup.prompt_echo_reference


@pytest.mark.parametrize("level", WARM_LEVELS)
def test_recency_line_stays_last_with_core_rules(level):
    case = ("es", "direct", True, False)
    setup = _assemble(case, persona_custom=True, warmth=level)
    system = _system_text(setup)
    recency = i18n_active.warmth_recency_line(level)
    assert system.rstrip().endswith(recency)
    assert i18n_active.core_rules(False) in system


def test_core_rules_precede_the_owner_policy_and_warmth_block():
    case = ("es", "direct", True, False)
    system = _system_text(_assemble(case, persona_custom=True, warmth="sentimental"))
    rules = i18n_active.core_rules(False)
    assert system.index(rules) < system.index(prompt_assembler.OWNER_RESPONSE_POLICY_CONCISE)
    assert system.index(rules) < system.index(i18n_active.warmth_block("sentimental"))


def test_engine_flags_only_a_really_custom_persona():
    import queue
    from unittest.mock import MagicMock

    import opencohost.core.llm_engine as llm_engine

    motor = llm_engine.MotorVocalIA(queue.Queue(), lambda e: None)
    motor.ollama = MagicMock()
    _activate("es")
    motor._dispatch_command("set_profile", {"id": "a", "prompt": "Eres Kira, a tu manera."})
    assert motor._persona_custom is True
    motor._dispatch_command("set_profile", {"id": "b", "prompt": i18n_active.system_prompt()})
    assert motor._persona_custom is False
    motor._dispatch_command("set_profile", {"id": "c"})
    assert motor._persona_custom is False


# ---------------------------------------------------------------------------
# C2: owner respect
# ---------------------------------------------------------------------------

RESPECT = {
    "es": ("nunca", "humilles", "acéptalo"),
    "en": ("never", "belittle", "accept"),
}


@pytest.mark.parametrize("locale", LOCALES)
def test_core_rules_carry_owner_respect(locale):
    _activate(locale)
    low = i18n_active.core_rules(False).lower()
    for needle in RESPECT[locale]:
        assert needle in low, (locale, needle)


@pytest.mark.parametrize("locale", LOCALES)
@pytest.mark.parametrize("level", WARM_LEVELS)
def test_warm_blocks_carry_owner_respect(locale, level):
    _activate(locale)
    low = i18n_active.warmth_block(level).lower()
    for needle in RESPECT[locale]:
        assert needle in low, (locale, level, needle)


@pytest.mark.parametrize("level", ("in_character", "sentimental"))
@pytest.mark.parametrize(
    "locale,phrase",
    [("es", "en el sentido biológico"), ("en", "in the biological sense")],
)
def test_biological_sense_ban_is_enforced_by_the_guard_not_quoted(locale, level, phrase):
    # kira-natural-replies-20261111 C: the phrase is no longer quoted in the prompt
    # (a quoted phrase primes a small model); output_guard's no_machine_lecture
    # pattern (config/validation.py, "en el sentido biologico") still blocks it.
    _activate(locale)
    assert phrase not in i18n_active.warmth_block(level)
    from opencohost.config import validation

    sample = (
        "no tengo deseos ni sentimientos en el sentido biológico"
        if locale == "es"
        else "I don't have desires or feelings in the biological sense"
    )
    assert any(p.search(sample) for p in validation._MACHINE_LECTURE_PATTERNS)


def test_builtin_english_warm_blocks_carry_respect_and_biological_ban(monkeypatch):
    from opencohost.i18n.contract import SlotNotFound

    def boom(*a, **k):
        raise SlotNotFound("x")

    monkeypatch.setattr(i18n_active, "resolve", boom)
    for level in WARM_LEVELS:
        low = i18n_active.warmth_block(level).lower()
        assert "belittle" in low and "accept" in low
    assert "in the biological sense" not in i18n_active.warmth_block("sentimental")


# ---------------------------------------------------------------------------
# C3: guard
# ---------------------------------------------------------------------------

BLOCKED = [
    "No tengo deseos ni sentimientos en el sentido biológico, pero me importas.",
    "No tengo emociones en el sentido biológico.",
    "Yo no tengo sentimientos, al menos no en el sentido biológico.",
    "Soy un modelo de lenguaje, así que no puedo opinar.",
    "Solo soy un modelo de lenguaje.",
    "Soy solo un modelo de lenguaje entrenado.",
    "I don't have desires or feelings in the biological sense.",
    "I do not have emotions in the biological sense, but I care.",
    "I'm just a language model.",
    "I am a language model, so I can't say.",
]

ALLOWED = [
    "Sí, soy una IA, y aun así me importa mucho lo que te pasa.",
    "No soy solo un algoritmo, me importas.",
    "No soy un modelo de lenguaje cualquiera, soy Kira.",
    "El modelo de lenguaje que hay detrás se puede cambiar.",
    "Dijo que soy un modelo de lenguaje, pero no le creo.",
    "Me importa en un sentido muy mío, no en el biológico.",
    "I'm not just a language model, I'm Kira.",
    "Yes, I'm an AI, and I still care a lot about you.",
    "¿Quién dijo que no tengo sentimientos?",
]


@pytest.mark.parametrize("level", ("in_character", "free", "sentimental"))
@pytest.mark.parametrize("text", BLOCKED)
def test_extended_machine_lecture_blocked_at_feeling_levels(level, text):
    warmth_state.set_active_warmth(level)
    allowed, reason = output_guard(text, source="direct")
    assert not allowed, (level, text)
    assert "[no_machine_lecture]" in reason or "[no_ai_self_identification]" in reason


@pytest.mark.parametrize("level", ("in_character", "free", "sentimental"))
@pytest.mark.parametrize("text", ALLOWED)
def test_honest_identity_and_denials_still_pass(level, text):
    warmth_state.set_active_warmth(level)
    allowed, reason = output_guard(text, source="direct")
    assert allowed, (level, text, reason)


@pytest.mark.parametrize("level", ("cold", "empathetic"))
@pytest.mark.parametrize("text", [t for t in BLOCKED if "modelo de lenguaje" in t.lower() and "biol" not in t.lower()][:3])
def test_new_patterns_never_run_on_cold_or_empathetic(level, text):
    warmth_state.set_active_warmth(level)
    _, reason = output_guard(text, source="direct")
    assert "[no_machine_lecture]" not in reason


@pytest.fixture(autouse=True)
def _reset_warmth_state():
    yield
    warmth_state.set_active_warmth("cold")


# ---------------------------------------------------------------------------
# C4: num_predict cap for owner text turns when extended_responses is off
# ---------------------------------------------------------------------------


def _predict(source, extended, **extra):
    case = ("es", source, True, extended)
    return _assemble(case, **extra).opciones_llm["num_predict"]


def test_cap_constant_matches_vision_cap():
    assert prompt_assembler.OWNER_REPLY_MAX_TOKENS == 320
    assert prompt_assembler.OWNER_REPLY_MAX_TOKENS == prompt_assembler.VISION_REPLY_MAX_TOKENS


@pytest.mark.parametrize("source", ("direct", "ptt"))
def test_owner_text_turn_is_capped_when_not_extended(source):
    assert _predict(source, False) <= prompt_assembler.OWNER_REPLY_MAX_TOKENS


def test_extended_responses_keeps_the_large_budget():
    assert _predict("direct", True) > prompt_assembler.OWNER_REPLY_MAX_TOKENS


def test_viewer_chat_is_not_capped_by_the_owner_rule():
    assert _predict("chat", False) > prompt_assembler.OWNER_REPLY_MAX_TOKENS


def test_reasoning_think_budget_is_not_starved():
    setup = _assemble(
        ("es", "direct", True, False),
        is_reasoning_model=lambda m: True,
        reasoning_settings_resolver=lambda m: {"enabled": True, "budget_tokens": 400, "preset": "balanced"},
    )
    assert setup.opciones_llm["num_predict"] >= prompt_assembler.OWNER_REPLY_MAX_TOKENS + 400
