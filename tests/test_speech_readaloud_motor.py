"""Strict TDD for the read-aloud motor seam (readaloud-speech T2).

Router helpers (`discard_source`, `source_counts`) and the thin motor methods
(`speak_readaloud`, `stop_readaloud`, `user_pause_speech`,
`user_resume_speech`, `readaloud_state`). Same harness discipline as
tests/test_speech_router_stack.py: real `MotorVocalIA`, REAL router thread,
REAL `_hablar_impl`; only Piper synthesis and the mixer are faked.

Privacy pin: the text never reaches historial, the dialogue event, the
last-reply sink or the log.
"""
from __future__ import annotations

import logging
from unittest.mock import MagicMock

import pytest

from opencohost.core.speech.router import (
    PRIORITY_CHAT,
    PRIORITY_OWNER,
    SpeechJobState,
)
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


# ── router helpers ────────────────────────────────────────────────────────


def test_discard_source_removes_active_incoming_and_stack_jobs(router_motors):
    mixer = _GateMixerMusic(block_at=(0,))
    motor, _, _ = _armed(router_motors, mixer_music=mixer, interrupt_enabled=True)
    router = motor._ensure_router()

    active = router.submit(_job_text("A", 2), "readaloud", PRIORITY_CHAT)
    assert mixer.entered[0].wait(5.0)
    queued_ra = router.submit(_job_text("B", 2), "readaloud", PRIORITY_CHAT)
    queued_chat = router.submit(_job_text("C", 2), "chat", PRIORITY_CHAT)

    removed = router.discard_source("readaloud")
    assert removed == 2
    assert _wait_until(lambda: active.state is SpeechJobState.DISCARDED, timeout=5.0), (
        "the ACTIVE readaloud job is cut, not suspended"
    )
    assert queued_ra.state is SpeechJobState.DISCARDED
    mixer.release(0)
    assert _wait_until(lambda: queued_chat.state is SpeechJobState.FINISHED, timeout=10.0), (
        "a non-readaloud job must be untouched"
    )


def test_discard_source_also_removes_a_suspended_stack_job(router_motors):
    mixer = _GateMixerMusic(block_at=(1,))
    motor, _, _ = _armed(router_motors, mixer_music=mixer, interrupt_enabled=True)
    router = motor._ensure_router()

    job = router.submit(_job_text("S", 3), "readaloud", PRIORITY_CHAT)
    assert mixer.entered[1].wait(5.0)
    router.engage_user_pause()
    assert _wait_until(lambda: job.state is SpeechJobState.SUSPENDED, timeout=5.0)

    assert router.discard_source("readaloud") == 1
    assert job.state is SpeechJobState.DISCARDED
    assert router.source_counts("readaloud") == (0, 0)
    router.release_user_pause()


def test_source_counts_split_active_from_pending(router_motors):
    mixer = _GateMixerMusic(block_at=(0,))
    motor, _, _ = _armed(router_motors, mixer_music=mixer, interrupt_enabled=True)
    router = motor._ensure_router()
    assert router.source_counts("readaloud") == (0, 0)

    router.submit(_job_text("A", 2), "readaloud", PRIORITY_CHAT)
    assert mixer.entered[0].wait(5.0)
    router.submit(_job_text("B", 2), "readaloud", PRIORITY_CHAT)
    router.submit(_job_text("C", 2), "chat", PRIORITY_CHAT)

    assert router.source_counts("readaloud") == (1, 1)
    assert router.source_counts("chat") == (0, 1)
    mixer.release(0)


# ── motor methods ─────────────────────────────────────────────────────────


def test_speak_readaloud_queue_uses_chat_band_and_readaloud_source(router_motors):
    mixer = _GateMixerMusic(block_at=(0,))
    motor, _, _ = _armed(router_motors, mixer_music=mixer, interrupt_enabled=True)

    job = motor.speak_readaloud(_job_text("Q", 2), "queue")
    assert job is not None
    assert job.source == "readaloud"
    assert job.priority == PRIORITY_CHAT
    mixer.release(0)
    assert _wait_until(lambda: job.state is SpeechJobState.FINISHED, timeout=10.0)


def test_speak_readaloud_now_preempts_and_the_cut_job_resumes(router_motors):
    mixer = _GateMixerMusic(block_at=(0,))
    motor, _, _ = _armed(router_motors, mixer_music=mixer, interrupt_enabled=True)
    # Documented interaction (feature doc): with discard_speech_residue ON a
    # non-agenda owner-band submit DISCARDS the cut job like every other
    # source. This test pins the OFF behavior: the cut reply is suspended.
    motor._discard_speech_residue = False
    router = motor._ensure_router()

    reply = router.submit(_job_text("R", 3), "chat", PRIORITY_CHAT)
    assert mixer.entered[0].wait(5.0)

    job = motor.speak_readaloud(_job_text("N", 2), "now")
    assert job.priority == PRIORITY_OWNER and job.source == "readaloud"
    assert _wait_until(lambda: reply.state is SpeechJobState.SUSPENDED, timeout=5.0), (
        "read-now must SUSPEND the cut reply (resumable), not discard it"
    )
    mixer.release(0)
    assert _wait_until(lambda: job.state is SpeechJobState.FINISHED, timeout=10.0)
    assert _wait_until(lambda: reply.state is SpeechJobState.FINISHED, timeout=10.0), (
        "the cut reply resumes after the read-aloud job"
    )


def test_speak_readaloud_returns_none_and_never_blocks_when_router_disabled(router_motors):
    motor, _, _ = _armed(router_motors, enabled=False)
    motor._hablar = MagicMock()

    assert motor.speak_readaloud("hola mundo.", "queue") is None
    motor._hablar.assert_not_called()


def test_stop_readaloud_discards_only_readaloud_jobs(router_motors):
    mixer = _GateMixerMusic(block_at=(0,))
    motor, _, _ = _armed(router_motors, mixer_music=mixer, interrupt_enabled=True)

    first = motor.speak_readaloud(_job_text("A", 2), "queue")
    assert mixer.entered[0].wait(5.0)
    second = motor.speak_readaloud(_job_text("B", 2), "queue")
    chat = motor._ensure_router().submit(_job_text("C", 2), "chat", PRIORITY_CHAT)

    assert motor.stop_readaloud() == 2
    assert first.state is not SpeechJobState.QUEUED
    assert second.state is SpeechJobState.DISCARDED
    mixer.release(0)
    assert _wait_until(lambda: chat.state is SpeechJobState.FINISHED, timeout=10.0)
    assert motor.readaloud_state() == {
        "paused": False, "readaloud_active": False, "readaloud_pending": 0,
    }


def test_user_pause_and_resume_methods_toggle_the_router_hold(router_motors):
    motor, _, _ = _armed(router_motors, interrupt_enabled=True)

    assert motor.user_pause_speech() is True
    assert motor.readaloud_state()["paused"] is True
    assert motor.user_resume_speech() is True
    assert motor.readaloud_state()["paused"] is False


def test_user_pause_requires_the_router(router_motors):
    motor, _, _ = _armed(router_motors, enabled=False)
    assert motor.user_pause_speech() is False
    assert motor.user_resume_speech() is False
    assert motor.readaloud_state() == {
        "paused": False, "readaloud_active": False, "readaloud_pending": 0,
    }


def test_readaloud_text_never_reaches_logs_history_or_dialogue(router_motors, caplog):
    caplog.set_level(logging.DEBUG, logger="OpenCohost")
    motor, rec, _ = _armed(router_motors, interrupt_enabled=True)
    history_before = list(getattr(motor, "historial", []))
    secret = "SECRETPHRASE alpha. SECRETPHRASE beta."

    job = motor.speak_readaloud(secret, "queue")
    assert _wait_until(lambda: job.state is SpeechJobState.FINISHED, timeout=10.0)

    assert not any("SECRETPHRASE" in r.getMessage() for r in caplog.records)
    assert list(getattr(motor, "historial", [])) == history_before
    assert "dialogue" not in " ".join(str(n) for n in rec.names())
