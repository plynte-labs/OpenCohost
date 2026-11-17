"""session-2107-fixes F3: with think on, num_predict = answer budget + reasoning budget.

Evidence (21:07 session): a direct turn ran with num_predict=242 think=True and a
second attempt of 484, both cut off (done=length) while still thinking; agenda
turns ran 768 think=on and 4 of 5 hit done=length. Root cause: under Auto
(no per-model budget_tokens) the resolver never added a reasoning allowance, so
the total was only the latency-derived ANSWER estimate.
"""
from __future__ import annotations

import pytest

from opencohost.config import settings
from opencohost.core.context import prompt_assembler as pa
from opencohost.core.context.prompt_assembler import PromptContextAssembler
from opencohost.core.engine import llm_budget_engine as lbe
from opencohost.core.engine.llm_budget_engine import (
    DEFAULT_REASONING_BUDGET,
    InferenceIntent,
    resolve_generation_budget,
)


def test_default_reasoning_budget_is_1024():
    assert DEFAULT_REASONING_BUDGET == 1024


def test_auto_think_adds_default_reasoning_allowance_to_answer_estimate():
    # 60.5 tok/s * 4s chat target = 242 answer tokens (the live number).
    off = resolve_generation_budget(
        intent=InferenceIntent.CHAT, allocated_context=8192, prompt_tokens=500,
        tps_ewma=60.5, reasoning_enabled=False,
    )
    on = resolve_generation_budget(
        intent=InferenceIntent.CHAT, allocated_context=8192, prompt_tokens=500,
        tps_ewma=60.5, reasoning_enabled=True,
    )
    assert off.effective_budget == 242
    assert on.effective_budget == 242 + DEFAULT_REASONING_BUDGET
    assert on.think is True


def test_auto_think_uncalibrated_agenda_adds_allowance_to_768():
    res = resolve_generation_budget(
        intent=InferenceIntent.AGENDA, allocated_context=8192, prompt_tokens=500,
        tps_ewma=0.0, reasoning_enabled=True,
    )
    assert res.effective_budget == lbe.DEFAULT_FALLBACK_BUDGET + DEFAULT_REASONING_BUDGET


def test_auto_think_still_bounded_by_context():
    res = resolve_generation_budget(
        intent=InferenceIntent.CHAT, allocated_context=1500, prompt_tokens=500,
        tps_ewma=60.5, reasoning_enabled=True,
    )
    assert res.effective_budget == 1500 - 500 - lbe.SAFETY_RESERVE_TOKENS


def test_think_off_is_unchanged():
    res = resolve_generation_budget(
        intent=InferenceIntent.CHAT, allocated_context=8192, prompt_tokens=500,
        tps_ewma=0.0, reasoning_enabled=False,
    )
    assert res.effective_budget == lbe.DEFAULT_FALLBACK_BUDGET


def test_manual_budget_is_still_added_not_replaced():
    res = resolve_generation_budget(
        intent=InferenceIntent.CHAT, allocated_context=8192, prompt_tokens=500,
        tps_ewma=60.5, reasoning_enabled=True, requested_budget=400, preset="balanced",
    )
    assert res.effective_budget == 242 + 400


def test_custom_preset_is_exact():
    res = resolve_generation_budget(
        intent=InferenceIntent.CHAT, allocated_context=8192, prompt_tokens=500,
        tps_ewma=60.5, reasoning_enabled=True, requested_budget=512, preset="custom",
    )
    assert res.effective_budget == 512


def _assemble(source, *, think, budget_tokens=None, tps=60.5, extended=False):
    kwargs = {}
    if think:
        kwargs["is_reasoning_model"] = lambda _m: True
        kwargs["reasoning_settings_resolver"] = lambda _m: {
            "enabled": True, "budget_tokens": budget_tokens, "preset": "balanced",
        }

    def resolver(*, intent, allocated_context, request_model, is_local):
        return resolve_generation_budget(
            intent=intent, allocated_context=allocated_context, prompt_tokens=400,
            tps_ewma=tps, reasoning_enabled=think,
            requested_budget=budget_tokens,
        )

    return PromptContextAssembler().assemble(
        "que opinas de esto",
        source,
        system_prompt="Eres Kira.",
        use_system_role=True,
        is_local=True,
        provider_cfg={},
        request_model="hf.co/unsloth/gemma-4-E4B-it-GGUF:IQ4_NL",
        history_snapshot=[],
        personalization_enabled=False,
        memorias_enabled=False,
        extended_responses=extended,
        budget_resolver=resolver,
        **kwargs,
    )


@pytest.mark.parametrize("source", ["direct", "ptt", "owner-bundle"])
def test_owner_turn_think_auto_gets_answer_cap_plus_default_reasoning(source):
    setup = _assemble(source, think=True)
    assert setup.think is True
    # 242 answer estimate + 1024 reasoning = 1266, under 320 + 1024 = 1344.
    assert setup.opciones_llm["num_predict"] == 242 + DEFAULT_REASONING_BUDGET


@pytest.mark.parametrize("source", ["direct", "ptt", "owner-bundle"])
def test_owner_cap_honors_the_reasoning_allowance_when_estimate_is_large(source):
    setup = _assemble(source, think=True, tps=500.0)  # 2000 answer estimate
    assert setup.opciones_llm["num_predict"] == pa.OWNER_REPLY_MAX_TOKENS + DEFAULT_REASONING_BUDGET


def test_owner_cap_uses_configured_budget_tokens_when_set():
    setup = _assemble("direct", think=True, budget_tokens=600, tps=500.0)
    assert setup.opciones_llm["num_predict"] == pa.OWNER_REPLY_MAX_TOKENS + 600


@pytest.mark.parametrize("source", ["direct", "ptt", "owner-bundle"])
def test_owner_think_off_keeps_the_320_cap(source):
    setup = _assemble(source, think=False, tps=500.0)
    assert setup.opciones_llm["num_predict"] == pa.OWNER_REPLY_MAX_TOKENS


def test_agenda_think_auto_is_answer_plus_default_reasoning(monkeypatch):
    # Legacy (progression OFF): uncapped agenda answer estimate + reasoning.
    monkeypatch.setattr(settings, "AGENDA_PROGRESSION_VARIANT", "")
    monkeypatch.setattr(settings, "AGENDA_PROGRESSION_ENABLED", False)
    setup = _assemble("kira-agenda", think=True, tps=0.0)
    assert setup.think is True
    assert setup.opciones_llm["num_predict"] == lbe.DEFAULT_FALLBACK_BUDGET + DEFAULT_REASONING_BUDGET


def _pin_progression_off(monkeypatch):
    monkeypatch.setattr(settings, "AGENDA_PROGRESSION_VARIANT", "")
    monkeypatch.setattr(settings, "AGENDA_PROGRESSION_ENABLED", False)


def test_agenda_progression_uses_fixed_cap_without_reasoning_budget():
    # Accepted by design (owner, 2026-11-16): progression turns carry a fixed
    # 110-token cap and NO additive reasoning budget, so thinking models may
    # return short or empty agenda lines. Users can turn progression off.
    setup = _assemble("kira-agenda", think=True, tps=0.0)
    assert setup.opciones_llm["num_predict"] == settings.AGENDA_V2_MAX_TOKENS
    assert setup.opciones_llm["repeat_penalty"] == settings.AGENDA_REPEAT_PENALTY


def test_fallback_path_default_reasoning_is_1024_when_auto(monkeypatch):
    _pin_progression_off(monkeypatch)
    # No resolver result available: the defensive fallback adds the default too.
    setup = PromptContextAssembler().assemble(
        "hola", "kira-agenda",
        system_prompt="Eres Kira.", use_system_role=True, is_local=True,
        provider_cfg={}, request_model="qwen3:4b", history_snapshot=[],
        personalization_enabled=False, memorias_enabled=False,
        is_reasoning_model=lambda _m: True,
        reasoning_settings_resolver=lambda _m: {"enabled": True, "budget_tokens": None, "preset": "balanced"},
        budget_resolver=lambda **_k: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    assert setup.opciones_llm["num_predict"] >= DEFAULT_REASONING_BUDGET + 32


# ── Exhaustion: one retry with thinking OFF and the answer budget ────────────

def _motor(model="gemma4:12b"):
    import queue
    from unittest.mock import MagicMock

    from opencohost.core.llm_engine import MotorVocalIA

    m = MotorVocalIA(queue.Queue(), lambda event: None)
    m.ollama = MagicMock()
    m.pygame = MagicMock()
    m.is_ready = True
    m.current_model = model
    m._desired_model = model
    m._loaded_model = model
    m._mark_model_generation_success = lambda *a, **k: None
    m._resolve_chat_watchdog_timeout = lambda model, provider_cfg=None, is_local=None: 30.0
    return m


def _thinking_only(*_a, **_k):
    return {"message": {"content": "", "thinking": "hmm, let me think again"}}


def _run(monkeypatch, source, responses, preset_custom=False):
    _pin_progression_off(monkeypatch)
    import opencohost.core.llm_engine as le
    from tests.test_reasoning_token_budget import _raise_show

    monkeypatch.setattr("ollama.show", _raise_show)
    monkeypatch.setattr(le, "output_guard", lambda dialogo, source="chat": (True, ""))
    monkeypatch.setattr(
        "opencohost.config.model_parameters.get_model_reasoning_settings",
        lambda model=None, config_dict=None, config_file=None: {
            "enabled": True,
            "budget_tokens": 512 if preset_custom else None,
            "preset": "custom" if preset_custom else "balanced",
        },
    )
    m = _motor()
    m._reasoning_model_cache[m.current_model] = True
    calls = []

    def fake_chat(*, timeout, **kwargs):
        calls.append(dict(kwargs.get("options", {})) | {"_think": kwargs.get("think")})
        return responses[min(len(calls) - 1, len(responses) - 1)]

    m._ollama_chat_with_watchdog = fake_chat
    result = m._generar_dialogo("hola", source=source, commit_history=False)
    return result, calls


@pytest.mark.parametrize("source", ["chat", "direct", "kira-agenda"])
def test_budget_exhausted_retries_once_with_think_off_and_answer_budget(monkeypatch, source):
    answer = {"message": {"content": "Respuesta directa.", "thinking": ""}}
    result, calls = _run(monkeypatch, source, [_thinking_only(), answer])

    assert result == "Respuesta directa."
    assert len(calls) == 2
    assert calls[0]["think"] is True
    assert calls[1]["think"] is False
    assert calls[1]["num_predict"] < calls[0]["num_predict"]
    assert calls[1]["num_predict"] == calls[0]["num_predict"] - DEFAULT_REASONING_BUDGET


def test_custom_preset_keeps_respecting_user_budget(monkeypatch):
    result, calls = _run(monkeypatch, "chat", [_thinking_only()], preset_custom=True)
    assert len(calls) == 1
    assert calls[0]["num_predict"] == 512
