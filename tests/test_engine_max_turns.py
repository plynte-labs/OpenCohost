"""Unit tests for MotorVocalIA dynamic max_turns hot-reloading (per-profile memory window)."""

import queue
from opencohost.core.llm_engine import MotorVocalIA

import pytest
# B-W4: this module pins the legacy prompt layout (system-message placement, one-pair
# history deque, legacy goldens). The v2 placement is tested in tests/test_prompt_layout_v2.py.
pytestmark = pytest.mark.usefixtures("legacy_prompt_layout")


def test_motor_initializes_with_default_max_turns():
    log_q = queue.Queue()
    motor = MotorVocalIA(log_q, lambda event: None)
    assert motor._max_turns == 3
    assert motor.historial.maxlen == 6


def test_motor_set_profile_updates_max_turns_and_historial_maxlen():
    log_q = queue.Queue()
    motor = MotorVocalIA(log_q, lambda event: None)

    # Dispatch set_profile with max_turns = 5
    motor._dispatch_command("set_profile", {"id": "p-5", "prompt": "Prompt 5", "max_turns": 5})
    assert motor._max_turns == 5
    assert motor.historial.maxlen == 10

    # Clamp upper bound > 10
    motor._dispatch_command("set_profile", {"id": "p-20", "prompt": "Prompt 20", "max_turns": 25})
    assert motor._max_turns == 10
    assert motor.historial.maxlen == 20

    # Clamp lower bound < 1
    motor._dispatch_command("set_profile", {"id": "p-0", "prompt": "Prompt 0", "max_turns": 0})
    assert motor._max_turns == 1
    assert motor.historial.maxlen == 2

    # Default if omitted
    motor._dispatch_command("set_profile", {"id": "p-def", "prompt": "Prompt Def"})
    assert motor._max_turns == 3
    assert motor.historial.maxlen == 6


def _local_ready_motor(ctx):
    """Motor whose active local model resolves to ``ctx`` (no Ollama calls)."""
    motor = MotorVocalIA(queue.Queue(), lambda event: None)
    motor.is_ready = True
    # Native ctx already cached by the warm-up (the cap never calls ollama.show).
    motor._model_ctx_limit = {motor.current_model: 131072, "big:ctx": 131072}
    motor._resolve_local_num_ctx = lambda model: ctx
    motor._resolve_reasoning_settings = lambda model: {"enabled": True, "budget_tokens": 2048}
    return motor


def test_set_profile_cap_stays_10_at_default_ctx():
    motor = _local_ready_motor(4096)
    motor._dispatch_command("set_profile", {"id": "p", "prompt": "P", "max_turns": 25})
    assert motor._max_turns == 10
    assert motor.historial.maxlen == 20
    assert motor._max_turns_cap == 10


def test_set_profile_cap_stays_10_when_ctx_unknown():
    motor = _local_ready_motor(None)
    motor._dispatch_command("set_profile", {"id": "p", "prompt": "P", "max_turns": 25})
    assert motor._max_turns == 10
    assert motor._max_turns_cap == 10


def test_set_profile_cap_grows_with_larger_ctx():
    motor = _local_ready_motor(16384)
    motor._dispatch_command("set_profile", {"id": "p", "prompt": "P", "max_turns": 25})
    assert motor._max_turns == 25
    assert motor.historial.maxlen == 50
    assert motor._max_turns_cap == 40

    motor._dispatch_command("set_profile", {"id": "p2", "prompt": "P", "max_turns": 99})
    assert motor._max_turns == 40
    assert motor.historial.maxlen == 80


def test_refresh_cap_reclamps_requested_turns_and_keeps_history():
    motor = _local_ready_motor(4096)
    motor._dispatch_command("set_profile", {"id": "p", "prompt": "P", "max_turns": 25})
    assert motor._max_turns == 10
    for i in range(4):
        motor.historial.append({"role": "user", "content": f"u{i}"})

    # The active model now resolves a bigger ctx (switch / warm-up): the cap
    # grows up to the profile's requested turns, history carried over.
    motor._resolve_local_num_ctx = lambda model: 16384
    motor._refresh_max_turns_cap()
    assert motor._max_turns == 25
    assert motor.historial.maxlen == 50
    assert [m["content"] for m in motor.historial] == ["u0", "u1", "u2", "u3"]

    # And back down.
    motor._resolve_local_num_ctx = lambda model: 4096
    motor._refresh_max_turns_cap()
    assert motor._max_turns == 10
    assert motor.historial.maxlen == 20
    assert len(motor.historial) == 4


def test_not_ready_engine_never_resolves_ctx():
    motor = MotorVocalIA(queue.Queue(), lambda event: None)
    motor.is_ready = False

    def _boom(model):
        raise AssertionError("must not resolve ctx before Ollama is ready")

    motor._resolve_local_num_ctx = _boom
    motor._dispatch_command("set_profile", {"id": "p", "prompt": "P", "max_turns": 25})
    assert motor._max_turns == 10
    assert motor._max_turns_cap == 10


def test_model_switch_recomputes_cap_for_new_model():
    motor = _local_ready_motor(None)
    motor._resolve_local_num_ctx = lambda model: 16384 if model == "big:ctx" else 4096
    motor._prepare_model = lambda model: True
    motor.ollama = type("_O", (), {"generate": staticmethod(lambda **kw: None)})()
    motor._dispatch_command("set_profile", {"id": "p", "prompt": "P", "max_turns": 25})
    assert motor._max_turns == 10

    motor._switch_and_prepare_model("big:ctx")
    assert motor._max_turns_cap == 40
    assert motor._max_turns == 25
    assert motor.historial.maxlen == 50


def test_cap_not_resolved_before_native_ctx_is_cached():
    motor = _local_ready_motor(16384)
    motor._model_ctx_limit = {}
    motor._dispatch_command("set_profile", {"id": "p", "prompt": "P", "max_turns": 25})
    assert motor._max_turns_cap == 10
    assert motor._max_turns == 10
