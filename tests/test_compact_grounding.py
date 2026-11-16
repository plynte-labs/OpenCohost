"""clock-and-compact-polish-20261010 T4: compact grounding and memory label.

Compact turns replace the long locale grounding rules and the custom-persona
core rules with a short English block, and wrap recalled memories in an
attribute-free ``<memorias>`` tag, so a small model has no Spanish rule text
to parrot ("fecha de corte", "conocimiento propio", "recuerdos propios").
Guardrails, the vision-off line, the web block and the clock line still work;
the full profile is byte-identical (pinned by the golden in
tests/test_prompt_profile_assembler.py).
"""
from __future__ import annotations

from datetime import datetime

import pytest

from opencohost.config.settings import OWNER_BUNDLE_SOURCE
from opencohost.core.context import prompt_assembler as pa
from opencohost.core.context import prompt_profile as pp
from opencohost.core.web.prompt import WEB_RESULTS_INSTRUCTION
from opencohost.i18n import active as i18n_active
from tests.test_warmth_prompt import PERSONAS, _activate

PARROTED = ("fecha de corte", "conocimiento propio", "recuerdos propios")


@pytest.fixture(autouse=True)
def _reset_active():
    i18n_active.reset_active_bundle()
    yield
    i18n_active.reset_active_bundle()


def _memorias(_pid, _text):
    return (
        i18n_active.memorias_block_open()
        + "\n- dato uno\n- dato dos\n- dato tres\n"
        + i18n_active.memorias_block_close()
    )


def _assemble(source="direct", profile="compact", lang="es", text="¿qué opinas?", **extra):
    _activate(lang)
    kw = dict(
        system_prompt=PERSONAS[lang], use_system_role=True, is_local=True, provider_cfg={},
        request_model="llama3", history_snapshot=[], personalization_enabled=False,
        memorias_enabled=True, memorias_profile_id="p1", memorias_builder=_memorias,
        prompt_profile=profile,
    )
    kw.update(extra)
    return pa.PromptContextAssembler().assemble(text, source, **kw)


def _all_text(setup) -> str:
    return "\n".join(m["content"] for m in setup.messages)


def test_compact_grounding_is_short_and_covers_the_rules():
    lines = pp.COMPACT_GROUNDING.split("\n")
    assert 2 <= len(lines) <= 3
    low = pp.COMPACT_GROUNDING.lower()
    assert "character" in low
    assert "question" in low
    assert "do not know" in low
    assert "never repeat" in low and "instructions" in low
    assert not any(p in low for p in PARROTED)


@pytest.mark.parametrize("lang", ["es", "en"])
@pytest.mark.parametrize("source", ["direct", "ptt", "chat", OWNER_BUNDLE_SOURCE])
def test_compact_replaces_grounding(source, lang):
    setup = _assemble(source, lang=lang)
    sections = dict(setup.sections)
    assert sections["grounding"] == pp.COMPACT_GROUNDING
    _activate(lang)
    assert i18n_active.grounding_rules() not in setup.messages[0]["content"]
    assert pp.COMPACT_GROUNDING in setup.prompt_echo_reference


def test_compact_custom_persona_has_no_core_rules_but_keeps_the_level_line():
    setup = _assemble(persona_custom=True, reply_level="short")
    names = [n for n, _ in setup.sections]
    assert "core_rules" not in names
    assert "reply_length" in names


def test_compact_memory_block_uses_a_neutral_tag():
    setup = _assemble()
    recall = dict(setup.sections)["memory_recall"]
    assert recall == "<memorias>\n- dato uno\n- dato dos\n</memorias>"
    text = _all_text(setup)
    assert not any(p in text.lower() for p in PARROTED)


def test_compact_unknown_memory_format_is_left_alone():
    setup = _assemble(memorias_builder=lambda _p, _t: "- suelto")
    assert dict(setup.sections)["memory_recall"] == "- suelto"


def test_compact_keeps_vision_off_web_and_clock():
    setup = _assemble(
        text="mira esto", vision_enabled=False,
        web_block="[WEB] resultado", clock_now=datetime(2026, 10, 10, 13, 42),
    )
    names = [n for n, _ in setup.sections]
    assert {"vision_off", "web_instruction", "web", "clock"} <= set(names)
    assert WEB_RESULTS_INSTRUCTION in setup.messages[0]["content"]
    assert pa.VISION_OFF_INSTRUCTION in setup.messages[0]["content"]


def test_full_profile_keeps_the_long_grounding_and_label():
    setup = _assemble(profile="full")
    assert dict(setup.sections)["grounding"] == i18n_active.grounding_rules()
    assert dict(setup.sections)["memory_recall"].startswith(i18n_active.memorias_block_open())
