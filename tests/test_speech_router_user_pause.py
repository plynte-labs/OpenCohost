"""Strict TDD for the user pause hold on `SpeechRouter` (readaloud-speech T1).

The owner's Pause/Resume control is a hold INDEPENDENT of the PTT mic hold
(`_ptt_held`): it suspends the active job resumably (same request-before-cut
discipline as `hold_and_pause`), refuses `_pick()` while set, and a release
resumes the suspended job from its cursor. A PTT release must not clear the
user pause, and a user release must not clear a live PTT hold.

Same harness discipline as tests/test_speech_router_stack.py: real
`MotorVocalIA`, REAL router thread, REAL `_hablar_impl`; only Piper synthesis
and the mixer are faked. Event handshakes, never `sleep` as a synchronizer.
"""
from __future__ import annotations

import logging

import pytest

from opencohost.core.speech.router import SpeechJobState
from tests.test_speech_router import _GateMixerMusic
from tests.test_speech_router_stack import _armed, _job_text, _wait_until


@pytest.fixture
def router_motors():
    made: list = []
    yield made
    for motor in made:
        router = getattr(motor, "_speech_router", None)
        if router is not None:
            router.stop(timeout=5.0)


def _messages(caplog) -> list:
    return [r.getMessage() for r in caplog.records]


def test_engage_suspends_active_job_and_release_resumes_from_cursor(router_motors):
    mixer = _GateMixerMusic(block_at=(1,))
    motor, _, _ = _armed(router_motors, mixer_music=mixer, interrupt_enabled=True)

    motor._speak_or_submit(_job_text("U", 3), source="chat")
    assert mixer.entered[1].wait(5.0), "never reached fragment 1"
    router = motor._speech_router
    job = router._active

    router.engage_user_pause()
    assert router.user_paused is True
    assert _wait_until(lambda: job.state is SpeechJobState.SUSPENDED, timeout=5.0), (
        "a user pause must SUSPEND the active job (resumable), never discard it"
    )
    assert job.cursor == 1, "the cut must land on the fragment boundary already played"
    assert job in router._stack

    router.release_user_pause()
    assert router.user_paused is False
    assert _wait_until(lambda: job.state is SpeechJobState.FINISHED, timeout=10.0)
    assert sorted(job.spoken) == [0, 1, 2], "lossless across the pause"


def test_hold_blocks_pick_for_work_arriving_while_paused(router_motors):
    motor, _, _ = _armed(router_motors, interrupt_enabled=True)
    router = motor._ensure_router()

    router.engage_user_pause()  # nothing active: the hold still arms
    assert router.user_paused is True
    job = router.submit(_job_text("B", 2), "chat", 1)
    assert router._pick() is None, "_pick() must refuse while the user hold is set"
    assert job.state is SpeechJobState.QUEUED

    router.release_user_pause()
    assert _wait_until(lambda: job.state is SpeechJobState.FINISHED, timeout=10.0)


def test_pause_pending_for_active_reports_the_user_hold(router_motors):
    motor, _, _ = _armed(router_motors, interrupt_enabled=True)
    router = motor._ensure_router()
    assert router.pause_pending_for_active() is False
    router.engage_user_pause()
    assert router.pause_pending_for_active() is True, (
        "closure B1 inside _hablar_impl must see the user hold"
    )
    router.release_user_pause()
    assert router.pause_pending_for_active() is False


def test_ptt_release_does_not_clear_the_user_pause(router_motors):
    motor, _, _ = _armed(router_motors, interrupt_enabled=True)
    router = motor._ensure_router()

    router.engage_user_pause()
    router.hold_and_pause("ptt")
    router.set_ptt_held(False)  # PTT release
    assert router.user_paused is True
    job = router.submit(_job_text("P", 2), "chat", 1)
    assert router._pick() is None, "PTT release must not lift the user pause"
    assert job.state is SpeechJobState.QUEUED

    router.release_user_pause()
    assert _wait_until(lambda: job.state is SpeechJobState.FINISHED, timeout=10.0)


def test_user_release_does_not_clear_a_live_ptt_hold(router_motors):
    motor, _, _ = _armed(router_motors, interrupt_enabled=True)
    router = motor._ensure_router()

    router.hold_and_pause("ptt")
    router.engage_user_pause()
    router.release_user_pause()
    assert router.user_paused is False
    job = router.submit(_job_text("Q", 2), "chat", 1)
    assert router._pick() is None, "a user release must not lift the PTT hold"

    router.set_ptt_held(False)
    assert _wait_until(lambda: job.state is SpeechJobState.FINISHED, timeout=10.0)


def test_engage_and_release_are_idempotent_and_log_once(router_motors, caplog):
    caplog.set_level(logging.INFO, logger="OpenCohost")
    motor, _, _ = _armed(router_motors, interrupt_enabled=True)
    router = motor._ensure_router()

    router.release_user_pause()  # release with no hold: silent
    router.engage_user_pause()
    router.engage_user_pause()
    router.release_user_pause()
    router.release_user_pause()

    msgs = _messages(caplog)
    engaged = [m for m in msgs if "user_pause engaged" in m]
    released = [m for m in msgs if "user_pause released" in m]
    assert len(engaged) == 1 and "victim=none" in engaged[0]
    assert len(released) == 1 and "held_ms=" in released[0]
    assert router.user_paused is False


def test_engage_log_names_the_victim_source_never_text(router_motors, caplog):
    caplog.set_level(logging.INFO, logger="OpenCohost")
    mixer = _GateMixerMusic(block_at=(0,))
    motor, _, _ = _armed(router_motors, mixer_music=mixer, interrupt_enabled=True)
    text = _job_text("SECRETWORD", 2)
    motor._speak_or_submit(text, source="chat")
    assert mixer.entered[0].wait(5.0)
    router = motor._speech_router

    router.engage_user_pause()
    assert _wait_until(lambda: len(router._stack) == 1, timeout=5.0)
    router.release_user_pause()

    engaged = [m for m in _messages(caplog) if "user_pause engaged" in m]
    assert engaged and "victim=chat" in engaged[0]
    assert not any("SECRETWORD" in m for m in _messages(caplog))
