"""Strict-TDD tests for Reasoning-Model Token Budget (reasoning_model_token_budget_20260623).

ADR-014: detect reasoning models by Ollama capabilities (augment, not replace, the
name heuristic) + runtime self-heal when a capped generation returns empty content
with non-empty internal thinking. Per-model cache so ollama.show is paid at most once.

All Ollama I/O is mocked; no real model or network call is made.
"""

from __future__ import annotations

import threading

import pytest

from opencohost.core.llm_engine import MotorVocalIA


def _raise_show(model):
    raise RuntimeError("ollama.show unavailable")


def _make_motor(model="gemma4:12b"):
    """A MotorVocalIA with __init__ bypassed and only the attributes _generar_dialogo
    touches on the source='chat', commit_history=False path."""
    m = MotorVocalIA.__new__(MotorVocalIA)
    m._provider_config = {"active_provider": "local", "profiles": {}}
    m._cloud_fallback_active = False
    m._reasoning_model_cache = {}
    m.current_model = model
    m._desired_model = model
    m._loaded_model = model
    m._current_profile_name = "default"
    m.use_system_role = True
    m.system_prompt = "sys"
    m._history_lock = threading.RLock()
    m._lock = threading.Lock()
    m.historial = []
    m._last_llm_failure = None
    m._log = lambda *a, **k: None
    m._mark_model_generation_success = lambda *a, **k: None
    m._resolve_chat_watchdog_timeout = lambda model, provider_cfg=None, is_local=None: 30.0
    return m


# ── Name heuristic preserved (regression guard) ─────────────────────────────


def test_name_heuristic_qwen3_still_detected():
    assert MotorVocalIA._uses_reasoning_token_budget("qwen3:4b") is True


def test_name_heuristic_llama3_returns_false():
    assert MotorVocalIA._uses_reasoning_token_budget("llama3") is False


# ── Layer 1: capabilities augmentation ──────────────────────────────────────


def test_capabilities_thinking_flag_detects_gemma4_12b(monkeypatch):
    monkeypatch.setattr("ollama.show", lambda model: {"capabilities": ["completion", "tools", "thinking"]})
    m = _make_motor()
    assert m._check_capabilities_reasoning("gemma4:12b") is True


def test_capabilities_no_thinking_flag_returns_false(monkeypatch):
    monkeypatch.setattr("ollama.show", lambda model: {"capabilities": ["completion", "tools"]})
    m = _make_motor()
    assert m._check_capabilities_reasoning("llama3") is False


def test_capabilities_ollama_show_exception_falls_back_gracefully(monkeypatch):
    monkeypatch.setattr("ollama.show", _raise_show)
    m = _make_motor()
    # No crash, and gemma4:12b (not in the name heuristic) classifies as False.
    assert m._resolve_reasoning_classification("gemma4:12b") is False


def test_capabilities_missing_key_returns_false(monkeypatch):
    monkeypatch.setattr("ollama.show", lambda model: {"modelfile": "..."})
    m = _make_motor()
    assert m._check_capabilities_reasoning("gemma4:12b") is False


# ── Layer 3: per-model cache ────────────────────────────────────────────────


def test_capabilities_cached_after_first_call(monkeypatch):
    calls = {"n": 0}

    def fake_show(model):
        calls["n"] += 1
        return {"capabilities": ["thinking"]}

    monkeypatch.setattr("ollama.show", fake_show)
    m = _make_motor()
    assert m._resolve_reasoning_classification("gemma4:12b") is True
    assert m._resolve_reasoning_classification("gemma4:12b") is True
    assert calls["n"] == 1


def test_capabilities_cache_is_per_model(monkeypatch):
    def fake_show(model):
        return {"capabilities": ["thinking"]} if model == "gemma4:12b" else {"capabilities": ["completion"]}

    monkeypatch.setattr("ollama.show", fake_show)
    m = _make_motor()
    assert m._resolve_reasoning_classification("gemma4:12b") is True
    assert m._resolve_reasoning_classification("llama3") is False


def test_known_reasoning_model_skips_capabilities_call(monkeypatch):
    calls = {"n": 0}

    def fake_show(model):
        calls["n"] += 1
        return {"capabilities": ["thinking"]}

    monkeypatch.setattr("ollama.show", fake_show)
    m = _make_motor()
    # qwen3 wins on the name heuristic — capabilities must NOT be queried.
    assert m._resolve_reasoning_classification("qwen3:4b") is True
    assert calls["n"] == 0


# ── Layer 2: runtime self-heal (drives _generar_dialogo) ────────────────────


def test_self_heal_removes_num_predict_on_empty_content_with_thinking(monkeypatch):
    import opencohost.core.llm_engine as le

    monkeypatch.setattr("ollama.show", _raise_show)  # caps can't classify it
    monkeypatch.setattr(le, "output_guard", lambda dialogo, source="chat": (True, ""))
    m = _make_motor("gemma4:12b")
    captured = []

    def fake_chat(*, timeout, **kwargs):
        captured.append(dict(kwargs.get("options", {})))
        if len(captured) == 1:
            return {"message": {"content": "", "thinking": "let me reason about this"}}
        return {"message": {"content": "Here is the real answer.", "thinking": ""}}

    m._ollama_chat_with_watchdog = fake_chat
    result = m._generar_dialogo("hola", source="chat", commit_history=False)

    assert result == "Here is the real answer."
    assert "num_predict" in captured[0]          # first attempt was capped
    assert "num_predict" not in captured[1]       # self-heal removed the cap
    assert m._reasoning_model_cache["gemma4:12b"] is True


def test_self_heal_does_not_reclassify_without_thinking(monkeypatch):
    import opencohost.core.llm_engine as le

    monkeypatch.setattr("ollama.show", _raise_show)
    monkeypatch.setattr(le, "output_guard", lambda dialogo, source="chat": (True, ""))
    m = _make_motor("llama3")
    captured = []

    def fake_chat(*, timeout, **kwargs):
        captured.append(dict(kwargs.get("options", {})))
        if len(captured) == 1:
            return {"message": {"content": "", "thinking": ""}}  # empty, no thinking
        return {"message": {"content": "answer", "thinking": ""}}

    m._ollama_chat_with_watchdog = fake_chat
    m._generar_dialogo("hola", source="chat", commit_history=False)

    assert "num_predict" in captured[0]
    assert "num_predict" in captured[1]   # NOT removed — no thinking signal
    assert m._reasoning_model_cache.get("llama3") is not True


def test_self_heal_not_triggered_on_successful_first_generation(monkeypatch):
    import opencohost.core.llm_engine as le

    monkeypatch.setattr("ollama.show", _raise_show)
    monkeypatch.setattr(le, "output_guard", lambda dialogo, source="chat": (True, ""))
    m = _make_motor("llama3")
    captured = []

    def fake_chat(*, timeout, **kwargs):
        captured.append(dict(kwargs.get("options", {})))
        return {"message": {"content": "immediate answer", "thinking": ""}}

    m._ollama_chat_with_watchdog = fake_chat
    result = m._generar_dialogo("hola", source="chat", commit_history=False)

    assert result == "immediate answer"
    assert len(captured) == 1
    assert "num_predict" in captured[0]
