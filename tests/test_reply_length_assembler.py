"""reply-length-selector-20261009 T2: reply level inside PromptContextAssembler.

``reply_level=None`` is today's behavior; ``"medium"`` must be byte-identical
to it. ``"very_long"`` is the profile ``extended_responses`` path. ``"short"``
and ``"long"`` keep the concise policy, swap the 2-4 sentence line for a level
line and cap local owner turns at 120 / 640 tokens.
"""
from __future__ import annotations

import pytest

from opencohost.core.context import prompt_assembler as pa
from opencohost.core.vision.sanitize import build_screen_block
from opencohost.i18n import active as i18n_active
from tests.test_warmth_prompt import (  # noqa: F401  (autouse fixture re-exported)
    CASES,
    _activate,
    _assemble,
    _case_id,
    _render,
    _reset_active,
)

# B-W4: this module pins the legacy prompt layout (system-message placement, one-pair
# history deque, legacy goldens). The v2 placement is tested in tests/test_prompt_layout_v2.py.
pytestmark = pytest.mark.usefixtures("legacy_prompt_layout")

BLOCK = build_screen_block("A table with p50 and p99 columns.")
LINES = {
    "es": {
        "short": "Respondes en 1-2 oraciones habladas, directo al punto.",
        "long": "Puedes extenderte a unas 5-8 oraciones habladas cuando aporte, sin monólogos.",
    },
    "en": {
        "short": "You reply in 1-2 spoken sentences, straight to the point.",
        "long": "You may go on to about 5-8 spoken sentences when it adds something, no monologues.",
    },
}
REASONING = {
    "is_reasoning_model": lambda _m: True,
    "reasoning_settings_resolver": lambda _m: {
        "enabled": True,
        "budget_tokens": 256,
        "preset": "balanced",
    },
}


def _system(setup) -> str:
    first = setup.messages[0]
    return first["content"] if first["role"] == "system" else str(first["content"])


def _full(setup) -> dict:
    return {
        **_render(setup),
        "opciones_llm": setup.opciones_llm,
        "think": setup.think,
        "sections": setup.sections,
        "answer_budget": setup.answer_budget,
    }


# --- medium / None byte identity --------------------------------------------


@pytest.mark.parametrize("persona_custom", [False, True])
@pytest.mark.parametrize("case", CASES, ids=_case_id)
def test_medium_is_byte_identical_to_none(case, persona_custom):
    base = _assemble(case, persona_custom=persona_custom)
    medium = _assemble(case, persona_custom=persona_custom, reply_level="medium")
    assert _full(medium) == _full(base)


@pytest.mark.parametrize("persona_custom", [False, True])
def test_medium_is_byte_identical_on_vision_and_reasoning(persona_custom):
    case = ("es", "direct", True, False)
    for extra in ({"screen_block": BLOCK}, REASONING):
        base = _assemble(case, persona_custom=persona_custom, **extra)
        medium = _assemble(case, persona_custom=persona_custom, reply_level="medium", **extra)
        assert _full(medium) == _full(base)


# --- very_long == extended path ---------------------------------------------


@pytest.mark.parametrize("persona_custom", [False, True])
@pytest.mark.parametrize("locale", ["es", "en"])
def test_very_long_is_the_extended_path(locale, persona_custom):
    extended = _assemble((locale, "direct", True, True), persona_custom=persona_custom)
    very_long = _assemble(
        (locale, "direct", True, False), persona_custom=persona_custom, reply_level="very_long"
    )
    assert _full(very_long) == _full(extended)
    assert pa.OWNER_RESPONSE_POLICY_EXTENDED in _system(very_long)
    assert very_long.opciones_llm["num_predict"] >= 1024


# --- short / long instruction -----------------------------------------------


@pytest.mark.parametrize("level", ["short", "long"])
@pytest.mark.parametrize("locale", ["es", "en"])
def test_level_line_replaces_two_to_four_on_default_persona(locale, level):
    setup = _assemble((locale, "direct", True, False), reply_level=level)
    system = _system(setup)
    assert "2-4" not in system
    assert pa.OWNER_RESPONSE_POLICY_CONCISE in system
    assert pa.OWNER_RESPONSE_POLICY_EXTENDED not in system
    assert LINES[locale][level] in system
    names = [name for name, _ in setup.sections]
    assert "reply_length" in names
    assert "core_rules" not in names
    assert LINES[locale][level] in setup.prompt_echo_reference


@pytest.mark.parametrize("level", ["short", "long"])
@pytest.mark.parametrize("locale", ["es", "en"])
def test_level_line_lives_in_core_rules_on_custom_persona(locale, level):
    setup = _assemble((locale, "direct", True, False), persona_custom=True, reply_level=level)
    sections = dict(setup.sections)
    assert "reply_length" not in sections
    rules = sections["core_rules"]
    assert LINES[locale][level] in rules
    assert "2-4" not in rules
    assert "2-4" not in sections["persona"]
    # Same rules block otherwise: title and body intact.
    assert rules.splitlines()[0] == i18n_active.core_rules(False).splitlines()[0]
    assert rules.splitlines()[2:] == i18n_active.core_rules(False).splitlines()[2:]


@pytest.mark.parametrize("level", ["short", "long"])
def test_builtin_core_rules_carry_level_lines(level, monkeypatch):
    from opencohost.i18n.contract import SlotNotFound

    def boom(*a, **k):
        raise SlotNotFound("llm.core_rules")

    monkeypatch.setattr(i18n_active, "resolve", boom)
    assert LINES["en"][level] in i18n_active.core_rules(False, level=level)
    assert LINES["en"][level] in i18n_active.reply_length_line(level)


@pytest.mark.parametrize("locale", ["es", "en"])
def test_reply_length_line_per_locale(locale):
    _activate(locale)
    assert i18n_active.reply_length_line("short") == LINES[locale]["short"]
    assert i18n_active.reply_length_line("long") == LINES[locale]["long"]
    assert i18n_active.reply_length_line("medium") == ""
    assert i18n_active.reply_length_line(None) == ""


def test_builtin_level_lines_match_en_manifest():
    _activate("en")
    from opencohost.i18n.active import _CORE_RULES_BUILTIN, get_active_bundle, resolve

    data = resolve(get_active_bundle(), "llm.core_rules", default=None)
    for key in ("length_short", "length_long"):
        assert data[key] == _CORE_RULES_BUILTIN[key]


def test_es_level_lines_are_neutral_spanish():
    _activate("es")
    for level in ("short", "long"):
        text = i18n_active.reply_length_line(level).lower()
        for voseo in ("vos ", "podés", "respondés", "sos "):
            assert voseo not in text


# --- caps --------------------------------------------------------------------


def _predict(level, *, source="direct", **extra):
    return _assemble(("es", source, True, False), reply_level=level, **extra).opciones_llm[
        "num_predict"
    ]


def test_cap_per_level_local_owner():
    base = _predict(None)
    assert base == pa.OWNER_REPLY_MAX_TOKENS == 320
    assert _predict("medium") == 320
    assert _predict("short") == 120
    assert _predict("long") == min(640, pa.settings.LLM_MAX_TOKENS)
    assert pa.OWNER_REPLY_MAX_TOKENS_BY_LEVEL == {"short": 120, "medium": 320, "long": 640}


def test_long_cap_never_raises_a_smaller_governed_budget(monkeypatch):
    # Documented: min() against the governed budget. Below 640 the cap stays
    # at the governed value, so "long" can be shorter than 640 tokens.
    from opencohost.core.engine.llm_budget_engine import (
        BudgetResolution,
        BudgetVerdict,
        InferenceIntent,
    )

    def governed(**_kw):
        return BudgetResolution(
            intent=InferenceIntent.CHAT,
            verdict=BudgetVerdict.ALLOWED,
            effective_budget=500,
            think=False,
            allocated_context=4096,
            remaining_context=3000,
            clamp_reason=None,
            calibrated_tps=0.0,
            tts_eligible=True,
            prompt_tokens=700,
        )

    assert _predict(None, budget_resolver=governed) == 320
    assert _predict("long", budget_resolver=governed) == 500
    assert _predict("short", budget_resolver=governed) == 120


def test_thinking_adds_reasoning_budget_on_top_of_level_cap():
    assert _predict("short", **REASONING) == 120 + 256
    assert _predict(None, **REASONING) == 320 + 256
    assert _predict("long", **REASONING) == 640 + 256


def test_vision_caps_per_level():
    assert _predict(None, screen_block=BLOCK) == pa.VISION_REPLY_MAX_TOKENS
    assert _predict("medium", screen_block=BLOCK) == pa.VISION_REPLY_MAX_TOKENS
    assert _predict("short", screen_block=BLOCK) == min(pa.VISION_REPLY_MAX_TOKENS, 120)
    assert _predict("long", screen_block=BLOCK) == 640


def test_cloud_gets_instruction_but_no_local_cap():
    def cloud(level):
        return pa.PromptContextAssembler().assemble(
            "explícame algo",
            "direct",
            system_prompt="Eres Kira.",
            use_system_role=True,
            is_local=False,
            provider_cfg={},
            request_model="gpt",
            history_snapshot=[],
            personalization_enabled=False,
            memorias_enabled=False,
            reply_level=level,
        )

    _activate("es")
    assert cloud("short").opciones_llm["num_predict"] == cloud(None).opciones_llm["num_predict"]
    assert LINES["es"]["short"] in _system(cloud("short"))


def test_non_owner_source_ignores_level():
    case = ("es", "chat", True, False)
    assert _full(_assemble(case, reply_level="short")) == _full(_assemble(case))
    assert _full(_assemble(case, reply_level="long")) == _full(_assemble(case))
