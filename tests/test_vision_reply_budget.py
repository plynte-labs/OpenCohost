"""V1.29: a vision turn answers as short spoken speech and never burns its budget thinking."""
from __future__ import annotations

import pytest

from opencohost.core.context import prompt_assembler as pa
from opencohost.core.context.prompt_assembler import PromptContextAssembler
from opencohost.core.vision.sanitize import build_screen_block

# B-W4: this module pins the legacy prompt layout (system-message placement, one-pair
# history deque, legacy goldens). The v2 placement is tested in tests/test_prompt_layout_v2.py.
pytestmark = pytest.mark.usefixtures("legacy_prompt_layout")

BLOCK = build_screen_block("A table with p50 and p99 columns.")


def _assemble(source="direct", screen_block="", reasoning=False, extended=False, model="llama3"):
    kwargs = {}
    if reasoning:
        kwargs["is_reasoning_model"] = lambda _m: True
        kwargs["reasoning_settings_resolver"] = lambda _m: {
            "enabled": True, "budget_tokens": 4096, "preset": "balanced",
        }
    return PromptContextAssembler().assemble(
        "que opinas de esto",
        source,
        system_prompt="Eres Kira.",
        use_system_role=True,
        is_local=True,
        provider_cfg={},
        request_model=model,
        history_snapshot=[],
        personalization_enabled=False,
        memorias_enabled=False,
        screen_block=screen_block,
        extended_responses=extended,
        **kwargs,
    )


def test_vision_turn_system_prompt_demands_short_spoken_speech():
    system = _assemble(screen_block=BLOCK).messages[0]["content"]

    assert pa.VISION_SPEECH_INSTRUCTION in system
    low = pa.VISION_SPEECH_INSTRUCTION.lower()
    for word in ("markdown", "table", "header", "sentences"):
        assert word in low


def test_vision_turn_caps_output_tokens():
    setup = _assemble(screen_block=BLOCK)

    assert setup.opciones_llm["num_predict"] <= pa.VISION_REPLY_MAX_TOKENS


def test_vision_cap_beats_extended_responses_and_reasoning_budget():
    setup = _assemble(screen_block=BLOCK, reasoning=True, extended=True)

    assert setup.opciones_llm["num_predict"] <= pa.VISION_REPLY_MAX_TOKENS


def test_vision_turn_on_reasoning_model_disables_thinking():
    plain = _assemble(reasoning=True)
    vision = _assemble(screen_block=BLOCK, reasoning=True)

    assert plain.think is True
    assert vision.think is False


def test_vision_turn_on_non_reasoning_model_leaves_think_unset():
    assert _assemble(screen_block=BLOCK).think is None


def test_cap_never_raises_a_smaller_budget():
    base = _assemble().opciones_llm["num_predict"]
    capped = _assemble(screen_block=BLOCK).opciones_llm["num_predict"]

    assert capped == min(base, pa.VISION_REPLY_MAX_TOKENS)


@pytest.mark.parametrize("reasoning", [False, True])
def test_non_vision_turn_is_unchanged(reasoning):
    # extended=True isolates the vision budget from the short-reply owner cap (C4).
    setup = _assemble(reasoning=reasoning, extended=True)

    assert pa.VISION_SPEECH_INSTRUCTION not in setup.messages[0]["content"]
    assert setup.opciones_llm["num_predict"] > pa.VISION_REPLY_MAX_TOKENS


@pytest.mark.parametrize("source", ["chat", "kira-agenda", "livevoice"])
def test_non_owner_source_with_block_gets_no_vision_budget(source, monkeypatch):
    # Legacy agenda budget: progression pinned OFF (its fixed 110 cap is by design).
    from opencohost.config import settings
    monkeypatch.setattr(settings, "AGENDA_PROGRESSION_VARIANT", "")
    monkeypatch.setattr(settings, "AGENDA_PROGRESSION_ENABLED", False)
    setup = _assemble(source=source, screen_block=BLOCK, reasoning=True)

    assert pa.VISION_SPEECH_INSTRUCTION not in setup.messages[0]["content"]
    assert setup.think is True
    assert setup.opciones_llm["num_predict"] > pa.VISION_REPLY_MAX_TOKENS
