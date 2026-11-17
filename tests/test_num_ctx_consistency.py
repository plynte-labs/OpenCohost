"""num_ctx consistency across local Ollama calls (vram-aware-context-budget, T3).

Warm-up (``_prepare_model``), the Topic Scout and the memoria promotion judge
must send the SAME ``options.num_ctx`` the chat path would resolve for the
model they call. A call without ``num_ctx`` runs at Ollama's default context,
which differs from the chat allocation and forces a reload between calls.

All Ollama I/O is faked; no real model or network call is made.
"""

from __future__ import annotations

import queue
from unittest.mock import MagicMock

from opencohost.core import llm_engine
from opencohost.core.engine import llm_engine_models
from opencohost.core.llm_engine import MotorVocalIA


def _requested_policy(monkeypatch, tokens=16384):
    monkeypatch.setattr(
        llm_engine_models,
        "get_model_context_settings",
        lambda *a, **k: {"policy": "requested", "requested_tokens": tokens},
    )


def _motor(monkeypatch, native=131072):
    motor = MotorVocalIA(queue.Queue(), lambda e: None)
    motor.ollama = MagicMock()
    motor._provider_config = {"active_provider": "local"}
    motor._cloud_fallback_active = False
    motor.health_monitor = None
    motor._discover_model_ctx = lambda model: native
    return motor


def _chat_path_ctx(motor, model):
    return motor._resolve_effective_ctx_limit(model, motor._discover_model_ctx(model))


# ── warm-up ──────────────────────────────────────────────────────────────────


def test_warmup_sends_resolved_num_ctx(monkeypatch):
    _requested_policy(monkeypatch)
    motor = _motor(monkeypatch)
    motor.is_ready = True

    assert motor._prepare_model("gemma4:e4b") is True

    options = motor.ollama.generate.call_args.kwargs["options"]
    assert options["num_ctx"] == 16384 == _chat_path_ctx(motor, "gemma4:e4b")
    assert options["num_predict"] == 1


def test_warmup_default_policy_matches_chat_path(monkeypatch):
    # Unset config: T1 Auto (min(native, 16384), VRAM-clamped) replaces the
    # old flat 4096 default for a 128K Gemma; warm-up must still match chat.
    from opencohost.config.model_parameters import AUTO_CONTEXT_TOKENS

    motor = _motor(monkeypatch)
    motor.is_ready = True

    motor._prepare_model("gemma4:e4b")

    options = motor.ollama.generate.call_args.kwargs["options"]
    assert options["num_ctx"] == AUTO_CONTEXT_TOKENS == _chat_path_ctx(motor, "gemma4:e4b")


def test_warmup_omits_num_ctx_when_resolution_fails(monkeypatch):
    motor = _motor(monkeypatch)
    motor.is_ready = True

    def boom(*a, **k):
        raise RuntimeError("no resolver")

    motor._resolve_effective_ctx_limit = boom

    assert motor._prepare_model("gemma4:e4b") is True
    assert "num_ctx" not in motor.ollama.generate.call_args.kwargs["options"]


# ── scout ────────────────────────────────────────────────────────────────────


def test_scout_sends_resolved_num_ctx_for_loaded_model(monkeypatch):
    _requested_policy(monkeypatch, 12288)
    monkeypatch.setattr(llm_engine, "SCOUT_ENABLED", True)
    motor = _motor(monkeypatch)
    motor._loaded_model = "gemma4:e4b"
    motor._pending_model_switch = None
    motor._awaiting_first_success_after_switch = False
    motor._check_capabilities_reasoning = lambda model: False
    motor.historial.append({"role": "user", "content": "hablamos de modelos", "source": "direct"})
    motor.historial.append({"role": "assistant", "content": "claro que si", "source": "direct"})
    calls = []

    def fake_scout_chat(**kwargs):
        calls.append(kwargs)
        return {"message": {"content": "Tema uno"}}

    motor._ollama_scout_client = object()
    motor._ollama_scout_chat = fake_scout_chat

    motor.scout_digest()

    assert len(calls) == 1
    assert calls[0]["model"] == "gemma4:e4b"
    assert calls[0]["options"]["num_ctx"] == 12288 == _chat_path_ctx(motor, "gemma4:e4b")


# ── promotion judge ──────────────────────────────────────────────────────────


def test_judge_sends_resolved_num_ctx_for_judge_model(monkeypatch):
    _requested_policy(monkeypatch, 8192)
    motor = _motor(monkeypatch, native=40960)
    motor._judge_model = lambda: "qwen3:8b"
    calls = []

    def stub(**kwargs):
        calls.append(kwargs)
        return {"message": {"content": '{"decisions": []}'}}

    motor._run_promotion_judge([{"content": "streamer: algo"}], chat_callable=stub)

    assert len(calls) == 1
    assert calls[0]["model"] == "qwen3:8b"
    assert calls[0]["options"]["num_ctx"] == 8192 == _chat_path_ctx(motor, "qwen3:8b")


def test_judge_omits_num_ctx_under_cloud_foreground(monkeypatch):
    # Cloud active: no local ollama.show, so native ctx is unknown — keep
    # today's behavior (no num_ctx) rather than invent a number.
    _requested_policy(monkeypatch, 8192)
    motor = _motor(monkeypatch)
    motor._provider_config = {
        "active_provider": "nvidia_nim",
        "profiles": {"nvidia_nim": {"base_url": "https://x/v1", "model": "glm"}},
    }
    motor._judge_model = lambda: "qwen3:8b"
    calls = []

    def stub(**kwargs):
        calls.append(kwargs)
        return {"message": {"content": '{"decisions": []}'}}

    motor._run_promotion_judge([{"content": "streamer: algo"}], chat_callable=stub)

    assert "num_ctx" not in calls[0]["options"]
