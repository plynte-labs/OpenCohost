"""Strict TDD for the router growing-job API
(conductor/tracks/llm_output_streaming_20260813/design.md §2).

A streaming turn is ONE job whose `chunks` grow while it plays:
`submit_streaming` creates it unsealed with `chunks=[]`, `append_chunks`
extends it, `seal` finalizes it. `_reconcile`'s drained branch finishes the
job only when SEALED; otherwise `_run_job` parks in the starved wait —
ACTIVE, boundary pair open, no `idle` — until chunks, a seal, a cancel or a
hold arrives. Everything else (D3 preempt, D4 outermost-only pairs, the
suspend/resume stack, sweeps) applies unchanged per slice.

Harness discipline inherited verbatim from tests/test_speech_router.py: a
real `MotorVocalIA`, a REAL router thread, the REAL `_hablar_impl`
producer/consumer loop. ONLY Piper synthesis and `pygame.mixer` are faked.
`threading.Event` handshakes only — never `time.sleep` as a synchronizer.
"""
from __future__ import annotations

import logging
import queue
import time

import pytest

from opencohost.core.speech.router import (
    PRIORITY_AGENDA,
    PRIORITY_OWNER,
    SPEECH_BOUNDARY_COMMAND,
    SpeechJob,
    SpeechJobState,
)
from tests.test_speech_outcome_capture import _make_motor
from tests.test_speech_router import _GateMixerMusic, _Recorder
from tests.test_speech_router_stack import _synth_logger, _wait_until


# ──────────────────────────────────────────────────────────────────────────
# Harness
# ──────────────────────────────────────────────────────────────────────────


@pytest.fixture
def router_motors():
    made: list = []
    yield made
    for motor in made:
        router = getattr(motor, "_speech_router", None)
        if router is not None:
            router.stop(timeout=5.0)


def _armed(router_motors, *, mixer_music=None, synthesize=None, interrupt_enabled=False):
    motor, log_q, _ = _make_motor(mixer_music=mixer_music, synthesize=synthesize)
    rec = _Recorder(motor)
    motor.ui_callback = rec
    motor._speech_router_enabled = True
    motor._speech_interrupt_enabled = interrupt_enabled
    router_motors.append(motor)
    return motor, rec, log_q


def _stream_chunks(tag: str, n: int) -> list:
    """Pre-split TTS fragments, the shape the streaming producer appends
    (post `_split_for_tts`): short, distinct, > 3 chars each."""
    return [f"{tag} sentencia numero {i} en vivo." for i in range(n)]


# ──────────────────────────────────────────────────────────────────────────
# (1) default-sealed — every existing job is byte-identical (design §2)
# ──────────────────────────────────────────────────────────────────────────


def test_speech_job_defaults_to_sealed():
    """The single most important property of this unit: `sealed` defaults
    True, so every job built anywhere but `submit_streaming` keeps today's
    reconcile behavior bit-for-bit (the regression net is the whole existing
    router suite, run unchanged)."""
    job = SpeechJob(job_id=1, text="hola", source="direct", priority=0)
    assert job.sealed is True


def test_submit_creates_a_sealed_job(router_motors):
    motor, _, _ = _armed(router_motors)
    router = motor._ensure_router()
    job = router.submit("texto normal de prueba.", "direct", PRIORITY_OWNER)
    assert job.sealed is True


# ──────────────────────────────────────────────────────────────────────────
# (2) a streaming job plays across MULTIPLE slice invocations as it grows
# ──────────────────────────────────────────────────────────────────────────


def test_streaming_job_plays_across_multiple_slice_invocations(router_motors):
    synth_log: list = []
    motor, rec, _ = _armed(router_motors, synthesize=_synth_logger(synth_log))
    router = motor._ensure_router()
    c = _stream_chunks("S", 3)

    real_hablar = motor._hablar
    slices: list = []

    def _spy(texto, source="direct", **kw):
        slices.append((kw.get("cursor_base", 0), list(kw.get("pre_split") or [])))
        return real_hablar(texto, source=source, **kw)

    motor._hablar = _spy

    job = router.submit_streaming("direct", PRIORITY_OWNER)
    assert job.sealed is False
    assert job.chunks == [] and job.text == ""

    assert router.append_chunks(job, [c[0]]) is True
    assert _wait_until(lambda: job.cursor == 1, timeout=10.0), "slice 1 never played"
    assert job.state is SpeechJobState.ACTIVE, "a drained UNSEALED job must stay ACTIVE (starved)"

    assert router.append_chunks(job, [c[1], c[2]]) is True
    assert _wait_until(lambda: job.cursor == 3, timeout=10.0), "slice 2 never played"

    router.seal(job)
    assert _wait_until(lambda: job.state is SpeechJobState.FINISHED, timeout=10.0)
    assert sorted(job.spoken) == [0, 1, 2]
    assert synth_log == c, "chunks must play in append order, each exactly once"

    # The resume path IS the slice mechanism: >= 2 real invocations, each a
    # verbatim tail of the growing list (empty pre-append picks are benign).
    played = [s for s in slices if s[1]]
    assert played == [(0, [c[0]]), (1, [c[1], c[2]])], slices
    assert len(slices) >= 2

    # ONE boundary pair for the whole growing job (D4), then idle, then ONE
    # engine wake at `_finish` — never one per slice.
    assert rec.wait_for("speaking_end", 1), rec.names()
    assert rec.boundary_sources() == [("speaking_start", "direct"), ("speaking_end", None)]
    assert rec.wait_for("idle", 1), rec.names()
    assert _wait_until(lambda: not motor.command_queue.empty(), timeout=5.0)
    drained: list = []
    while True:
        try:
            drained.append(motor.command_queue.get_nowait())
        except queue.Empty:
            break
    assert [cmd for cmd, _ in drained].count(SPEECH_BOUNDARY_COMMAND) == 1, drained


# ──────────────────────────────────────────────────────────────────────────
# (3) submit_streaming keeps submit's D3 single priority-0 preempt
# ──────────────────────────────────────────────────────────────────────────


def test_submit_streaming_priority_zero_preempts_the_active_job(router_motors):
    mixer = _GateMixerMusic(block_at=(0,))
    motor, rec, _ = _armed(router_motors, mixer_music=mixer, interrupt_enabled=True)
    router = motor._ensure_router()

    agenda_job = router.submit(
        "AGENDA fragmento unico de la prueba.", "kira-agenda:a", PRIORITY_AGENDA
    )
    assert mixer.entered[0].wait(5.0), "agenda job never reached playback"

    job = router.submit_streaming("direct", PRIORITY_OWNER)
    assert _wait_until(lambda: len(router._stack) == 1, timeout=10.0), (
        "the streaming submit never preempted the active job"
    )
    assert router._stack[0] is agenda_job

    d = _stream_chunks("D", 1)
    assert router.append_chunks(job, d) is True
    router.seal(job)

    # The streaming job (inner, silent under D4) finishes; the agenda owner
    # resumes, replays, and closes the ONLY boundary pair.
    assert rec.wait_for("speaking_end", 1, timeout=10.0), rec.names()
    assert router.completed_job_ids() == [job.job_id, agenda_job.job_id]
    assert rec.boundary_sources() == [
        ("speaking_start", "kira-agenda:a"),
        ("speaking_end", None),
    ]


# ──────────────────────────────────────────────────────────────────────────
# (4) append_chunks refuses on a dead job — the producer's liveness signal
# ──────────────────────────────────────────────────────────────────────────


def test_append_after_finished_returns_false_and_drops(router_motors, caplog):
    caplog.set_level(logging.INFO, logger="OpenCohost")
    motor, rec, _ = _armed(router_motors)
    router = motor._ensure_router()
    c = _stream_chunks("F", 1)

    job = router.submit_streaming("direct", PRIORITY_OWNER)
    assert router.append_chunks(job, c) is True
    router.seal(job)
    assert _wait_until(lambda: job.state is SpeechJobState.FINISHED, timeout=10.0)

    assert router.append_chunks(job, _stream_chunks("LATE", 1)) is False
    assert job.chunks == c, "a refused append must drop the chunks"
    refused = [r.getMessage() for r in caplog.records if "append refused" in r.getMessage()]
    assert len(refused) == 1, refused
    assert "sentencia" not in refused[0], "PRIVACY: no chunk text in telemetry"


def test_append_after_discarded_returns_false(router_motors):
    motor, rec, _ = _armed(router_motors)
    router = motor._ensure_router()
    job = router.submit_streaming("kira-agenda:t1", PRIORITY_AGENDA)
    assert router.append_chunks(job, _stream_chunks("A", 1)) is True
    assert _wait_until(lambda: job.cursor == 1, timeout=10.0)

    motor.cancel_speech_for_sources(("kira-agenda",))
    assert _wait_until(lambda: job.state is SpeechJobState.DISCARDED, timeout=10.0)

    assert router.append_chunks(job, _stream_chunks("B", 1)) is False
    assert len(job.chunks) == 1
    motor.clear_speech_cancel()


# ──────────────────────────────────────────────────────────────────────────
# (5) seal is idempotent
# ──────────────────────────────────────────────────────────────────────────


def test_seal_is_idempotent_including_on_a_finished_job(router_motors):
    motor, rec, _ = _armed(router_motors)
    router = motor._ensure_router()

    job = router.submit_streaming("direct", PRIORITY_OWNER)
    router.seal(job)
    router.seal(job)  # double seal: no error, no state churn
    assert job.sealed is True

    # A zero-chunk sealed streaming job drains immediately — the existing
    # zero-fragment shape: one pair, FINISHED.
    assert _wait_until(lambda: job.state is SpeechJobState.FINISHED, timeout=10.0)
    router.seal(job)  # sealing a finished job stays a no-op
    assert job.state is SpeechJobState.FINISHED
    assert rec.boundary_sources() == [("speaking_start", "direct"), ("speaking_end", None)]


# ──────────────────────────────────────────────────────────────────────────
# (6) the starved state — ACTIVE, has_work() True, no idle; seal finishes
# ──────────────────────────────────────────────────────────────────────────


def test_starved_unsealed_job_keeps_has_work_true_and_never_idles(router_motors):
    motor, rec, _ = _armed(router_motors)
    router = motor._ensure_router()
    c = _stream_chunks("H", 1)

    job = router.submit_streaming("direct", PRIORITY_OWNER)
    assert router.append_chunks(job, c) is True
    assert _wait_until(lambda: job.cursor == 1, timeout=10.0)

    # Starved: drained, unsealed. The job is still open work — the boundary
    # pair stays open and the engine must not see idle.
    assert job.state is SpeechJobState.ACTIVE
    assert router.has_work() is True
    deadline = time.monotonic() + 0.4
    while time.monotonic() < deadline:
        assert "idle" not in rec.names(), "idle fired while a streaming job starved"
        assert "speaking_end" not in rec.names(), "the boundary closed while starved"
        time.sleep(0.01)

    # Seal-while-starved exits the wait and finishes the job.
    router.seal(job)
    assert _wait_until(lambda: job.state is SpeechJobState.FINISHED, timeout=10.0)
    assert rec.wait_for("speaking_end", 1), rec.names()
    assert rec.wait_for("idle", 1), rec.names()
    assert rec.names().count("idle") == 1


# ──────────────────────────────────────────────────────────────────────────
# (7) cancel during starvation — DISCARDED via _finish, logged
# ──────────────────────────────────────────────────────────────────────────


def test_cancel_during_starvation_discards_the_job(router_motors, caplog):
    caplog.set_level(logging.INFO, logger="OpenCohost")
    motor, rec, _ = _armed(router_motors)
    router = motor._ensure_router()

    job = router.submit_streaming("kira-agenda:t1", PRIORITY_AGENDA)
    assert router.append_chunks(job, _stream_chunks("C", 1)) is True
    assert _wait_until(lambda: job.cursor == 1, timeout=10.0)
    assert job.state is SpeechJobState.ACTIVE  # starved

    motor.cancel_speech_for_sources(("kira-agenda",))
    assert _wait_until(lambda: job.state is SpeechJobState.DISCARDED, timeout=10.0), (
        "the starved wait never honoured the cancel token"
    )
    assert rec.wait_for("speaking_end", 1), rec.names()
    lines = [
        r.getMessage() for r in caplog.records
        if r.getMessage().startswith("[SPEECH_DISCARD]") and "reason=cancelled" in r.getMessage()
    ]
    assert len(lines) == 1, lines
    motor.clear_speech_cancel()


# ──────────────────────────────────────────────────────────────────────────
# (8) a suspended unsealed job keeps growing and resumes owing everything
# ──────────────────────────────────────────────────────────────────────────


def test_suspended_unsealed_job_grows_on_the_stack_and_resume_plays_everything(router_motors):
    mixer = _GateMixerMusic(block_at=(0,))
    motor, rec, _ = _armed(router_motors, mixer_music=mixer, interrupt_enabled=True)
    router = motor._ensure_router()
    c = _stream_chunks("G", 3)

    job = router.submit_streaming("direct", PRIORITY_OWNER)
    assert router.append_chunks(job, c[:2]) is True
    assert mixer.entered[0].wait(5.0), "the streaming job never reached playback"

    motor.pause_speech_for_ptt()
    assert _wait_until(lambda: len(router._stack) == 1, timeout=10.0), "job never suspended"
    assert router._stack[0] is job
    assert job.cursor == 0, "the cut fragment is owed and replays"

    # The hold does not stop the producer: appends land on the SUSPENDED job.
    assert router.append_chunks(job, [c[2]]) is True
    assert job.chunks == c
    router.seal(job)
    assert job.state is SpeechJobState.SUSPENDED, "seal must not resurrect a suspended job"

    motor.resume_speech_after_ptt()
    assert _wait_until(lambda: job.state is SpeechJobState.FINISHED, timeout=10.0)
    assert sorted(job.spoken) == [0, 1, 2], (
        "resume must play everything owed, including the chunk appended during the hold"
    )
    assert rec.boundary_sources() == [("speaking_start", "direct"), ("speaking_end", None)]
