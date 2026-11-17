"""K2 (warmth-guardrails-runtime-20261006): a model switch requested while Kira
is only SPEAKING (no generation in flight) applies promptly -- current playback
is discarded through the speech router -- instead of waiting for the speech to
finish (76 s in the 2026-10-05 session). A switch requested while a generation
is in flight still defers exactly as before.
"""
from __future__ import annotations

import queue
from unittest.mock import MagicMock

import pytest

from opencohost.core.speech.router import SpeechJobState
from tests.test_speech_router import _GateMixerMusic
from tests.test_speech_router_stack import _armed, _job_text, _wait_until


def _plain_motor():
    from opencohost.core.llm_engine import MotorVocalIA

    events = []
    motor = MotorVocalIA(queue.Queue(), events.append)
    motor.ollama = MagicMock()
    motor.pygame = MagicMock()
    motor.is_ready = True
    motor._apply_model_switch = MagicMock()
    return motor, events


def test_switch_while_only_speaking_applies_immediately_and_stops_speech():
    motor, events = _plain_motor()
    motor._speaking = True  # legacy path: audio playing, no router
    assert motor._processing is False

    motor._dispatch_command("switch_model", "otro-modelo:latest")

    motor._apply_model_switch.assert_called_once_with("otro-modelo:latest")
    assert motor._pending_model_switch is None
    assert motor._speaking is False  # interrupt_speaking() was used
    assert "model_switch_pending" not in events


def test_switch_while_processing_still_defers():
    motor, events = _plain_motor()
    motor._processing = True
    motor._speaking = True

    motor._dispatch_command("switch_model", "otro-modelo:latest")

    motor._apply_model_switch.assert_not_called()
    assert motor._pending_model_switch == "otro-modelo:latest"
    assert motor._speaking is True  # speech untouched while a generation runs
    assert "model_switch_pending" in events


def test_switch_while_idle_unchanged():
    motor, _ = _plain_motor()

    motor._dispatch_command("switch_model", "otro-modelo:latest")

    motor._apply_model_switch.assert_called_once_with("otro-modelo:latest")


@pytest.fixture
def router_motors():
    made: list = []
    yield made
    for motor in made:
        router = getattr(motor, "_speech_router", None)
        if router is not None:
            router.stop(timeout=5.0)


def test_switch_while_router_job_active_discards_it_then_applies(router_motors):
    mixer = _GateMixerMusic(block_at=(1,))
    motor, _, _ = _armed(router_motors, mixer_music=mixer, interrupt_enabled=True)
    motor._apply_model_switch = MagicMock()

    motor._speak_or_submit(_job_text("U", 3), source="chat")
    assert mixer.entered[1].wait(5.0), "never reached fragment 1"
    router = motor._speech_router
    job = router._active
    assert motor._processing is False

    motor._dispatch_command("switch_model", "otro-modelo:latest")

    motor._apply_model_switch.assert_called_once_with("otro-modelo:latest")
    assert motor._pending_model_switch is None
    mixer.release(1)
    assert _wait_until(lambda: job.state is SpeechJobState.DISCARDED, timeout=5.0), (
        "the active speech job must be CUT (discarded), not left playing"
    )
