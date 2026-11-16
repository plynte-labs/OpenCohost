"""Engine-side reload for a per-model context change (T2,
model_context_auto_config_20260928).

``MotorVocalIA.request_model_context_reload(model)`` is called by
``PUT /api/models/context`` right after persisting a config change. For the
ACTIVE model it resets the ctx-plan ceiling and forces a fresh warm-up at the
new target -- deferred exactly like ``switch_model`` when a generation or
speech turn is in flight (``_processing`` / ``_speech_active``). For any
OTHER model it is a no-op: the new config simply applies whenever that model
is next loaded.
"""

import queue

from opencohost.core.llm_engine import MotorVocalIA
from opencohost.core.context import context_budget


class _FakeOllama:
    def __init__(self):
        self.generate_calls = []

    def generate(self, **kwargs):
        self.generate_calls.append(kwargs)
        return {"done": True}


def _motor():
    motor = MotorVocalIA(queue.Queue(), lambda event: None)
    motor.is_ready = True
    motor.ollama = _FakeOllama()
    return motor


def test_reload_is_noop_for_a_non_active_model():
    motor = _motor()
    motor.current_model = "gemma4:e4b"
    motor._warmed_model = "gemma4:e4b"
    motor._ctx_plan_state = {"gemma4:e4b": {"target": 16384, "ceiling": 16384, "logged": None}}

    applied = motor.request_model_context_reload("llama3")

    assert applied is False
    assert motor.ollama.generate_calls == []
    assert motor._warmed_model == "gemma4:e4b"  # untouched
    assert motor._ctx_plan_state["gemma4:e4b"]["ceiling"] == 16384  # untouched


def test_reload_applies_immediately_for_active_model_when_idle():
    motor = _motor()
    motor.current_model = "gemma4:e4b"
    motor._warmed_model = "gemma4:e4b"  # already warmed at the OLD ctx
    motor._ctx_plan_state = {"gemma4:e4b": {"target": 16384, "ceiling": 8192, "logged": ("x",)}}
    motor._processing = False
    # motor._speech_active defaults False on a fresh, never-spoken motor.

    applied = motor.request_model_context_reload("gemma4:e4b")

    assert applied is True
    # ceiling reset: the warm-up re-resolved the plan from scratch and is no
    # longer stuck at the stale, previously-ratcheted-down 8192 ceiling.
    assert motor._ctx_plan_state["gemma4:e4b"]["ceiling"] == 16384
    # `_prepare_model` re-ran the warm-up generate call (bypassing the
    # `_warmed_model == model` short-circuit that would otherwise no-op it).
    assert len(motor.ollama.generate_calls) == 1
    assert motor._warmed_model == "gemma4:e4b"


def test_reload_defers_while_processing():
    motor = _motor()
    motor.current_model = "gemma4:e4b"
    motor._warmed_model = "gemma4:e4b"
    motor._processing = True

    applied = motor.request_model_context_reload("gemma4:e4b")

    assert applied is True
    # Deferred, not applied yet: no warm-up call ran.
    assert motor.ollama.generate_calls == []
    assert motor._pending_ctx_reload_model == "gemma4:e4b"

    # The idle-tick busy gate later drains it (mirrors `_check_pending_model_switch`).
    motor._processing = False
    motor._check_pending_model_switch()
    assert len(motor.ollama.generate_calls) == 1
    assert motor._pending_ctx_reload_model is None


def test_reload_defers_while_speech_active():
    motor = _motor()
    motor.current_model = "gemma4:e4b"
    motor._warmed_model = "gemma4:e4b"
    motor._speaking = True  # `_speech_active` reads `_speaking` (design §11 B2)

    applied = motor.request_model_context_reload("gemma4:e4b")

    assert applied is True
    assert motor.ollama.generate_calls == []
    assert motor._pending_ctx_reload_model == "gemma4:e4b"


def test_reload_refreshes_max_turns_cap():
    motor = _motor()
    motor.current_model = "gemma4:e4b"
    motor._model_ctx_limit["gemma4:e4b"] = 131072
    motor._warmed_model = "gemma4:e4b"
    motor._resolve_local_num_ctx = lambda model: 16384

    motor.request_model_context_reload("gemma4:e4b")

    assert motor._max_turns_cap > context_budget.MAX_TURNS_CAP_FLOOR
