"""Strict TDD for step 3 of the speech-router landing sequence
(conductor/tracks/interruptible_speech_architecture_20260804/speech-router-design.md
§2, §3, §4, §5, §6, §8 step 3, §10 D3/D4, §11 step-3 rider).

Stack, pause/resume, `_ptt_held`, D3-uniform preemption, D4 outermost-only
boundary emission, depth sweeps. Section 7 discipline, inherited verbatim:
real `MotorVocalIA`, REAL router thread, REAL `_hablar_impl` producer/consumer
loop. ONLY Piper synthesis and `pygame.mixer` are faked. `threading.Event`
handshakes only -- never `time.sleep` as a synchronizer, never a hand-set
field the code is supposed to derive.
"""
from __future__ import annotations

import logging
import queue
import re
import threading
import time

import pytest

from opencohost.core import llm_engine
from opencohost.core.speech.router import SpeechJobState
from tests.test_speech_outcome_capture import _make_motor, _sentences, _write_wav
from tests.test_speech_router import _GateMixerMusic, _ScriptedMixerMusic, _Recorder, _pump


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


def _armed(router_motors, *, mixer_music=None, synthesize=None, enabled=True, interrupt_enabled=False):
    """Mirrors tests/test_speech_router.py::_armed, plus the step-3 kill
    switch -- defaults to OFF (the real production default), so a test that
    wants pause/preemption must opt in explicitly."""
    motor, log_q, _ = _make_motor(mixer_music=mixer_music, synthesize=synthesize)
    rec = _Recorder(motor)
    motor.ui_callback = rec
    motor._speech_router_enabled = enabled
    motor._speech_interrupt_enabled = interrupt_enabled
    router_motors.append(motor)
    return motor, rec, log_q


def _text(n: int = 3) -> str:
    return " ".join(_sentences(n))


def _comma_heavy_text() -> str:
    """A >25-word comma-heavy fragment followed by a short sentence, PROVEN
    (scratch probe) to diverge under re-chunking at cursor=1: dropping the
    first 15-word comma-group brings the remaining 18 words under the
    25-word cap, so the splitter STOPS sub-splitting them and merges two
    owed chunks into one. `_sentences` is a fixpoint at every cursor and
    would make a resume test vacuously green on this exact axis (design §1
    resolution 1b) -- this fixture is the one that actually fails wrong."""
    g0 = " ".join(f"g0w{i}" for i in range(15))
    g1 = " ".join(f"g1w{i}" for i in range(8))
    g2 = " ".join(f"g2w{i}" for i in range(10))
    long_sentence = f"{g0}, {g1}, {g2}."
    short_sentence = "Gracias por escuchar de verdad."
    return f"{long_sentence} {short_sentence}"


# ──────────────────────────────────────────────────────────────────────────
# (1) pre_split — the chunker is NOT a fixpoint (resolution 1 / 1b)
# ──────────────────────────────────────────────────────────────────────────


def test_pre_split_is_replayed_verbatim_and_a_rejoined_slice_is_not():
    text = _comma_heavy_text()

    # First, derive the REAL splitter's full output using the production
    # path (no pre_split) so this test doesn't hand-encode the chunk
    # boundaries -- it reads them back from the real code.
    baseline_motor, _, _ = _make_motor(mixer_music=_ScriptedMixerMusic())
    full_outcome = baseline_motor._hablar_impl(text, source="direct")
    full_chunks = full_outcome.chunks
    assert len(full_chunks) >= 3, "fixture must produce a resumable multi-chunk split"

    cursor = 1
    slice_ = full_chunks[cursor:]

    # PROOF the fixture actually diverges under re-chunking (never re-derive
    # this from the design doc's numbers -- assert it directly against the
    # real splitter): re-joining and re-splitting the tail must NOT equal the
    # tail itself.
    rejoin_motor, _, _ = _make_motor(mixer_music=_ScriptedMixerMusic())
    rejoined_outcome = rejoin_motor._hablar_impl(" ".join(slice_), source="direct")
    assert rejoined_outcome.chunks != slice_, (
        "fixture is a re-chunking fixpoint -- pick a different comma-heavy text"
    )

    # Now the real assertion: pre_split bypasses the sanitizer/scrub/splitter
    # entirely and replays the slice VERBATIM.
    synth_log: list = []

    def _synth(text_, out_path):
        synth_log.append(text_)
        return _write_wav(out_path)

    motor, _, _ = _make_motor(mixer_music=_ScriptedMixerMusic(), synthesize=_synth)
    outcome = motor._hablar_impl(text, source="direct", pre_split=slice_)

    assert outcome.chunks == slice_, "pre_split must be used verbatim, never re-split"
    assert synth_log == slice_, "the producer must synthesize exactly the given slice, in order"


def test_hablar_forwards_pre_split_to_hablar_impl(router_motors):
    motor, _, _ = _armed(router_motors, enabled=False)
    captured: list = []
    real_impl = motor._hablar_impl

    def _spy(texto, source="direct", pre_split=None, cursor_base=0):
        captured.append(pre_split)
        return real_impl(texto, source=source, pre_split=pre_split, cursor_base=cursor_base)

    motor._hablar_impl = _spy
    slice_ = ["fragmento uno.", "fragmento dos."]

    motor._hablar("texto original ignorado", source="direct", pre_split=slice_)

    assert captured == [slice_]


def _job_sentences(tag: str, n: int) -> list:
    """Job-unique, chunk-unique sentences: distinct source tags must never
    collide on text (`_sentences` always numbers from 0), which would break
    a synth-log-based order proof."""
    return [f"{tag} fragmento numero {i} de la prueba." for i in range(n)]


def _job_text(tag: str, n: int) -> str:
    return " ".join(_job_sentences(tag, n))


def _wait_until(predicate, timeout: float = 5.0, interval: float = 0.005) -> bool:
    """Event-driven poll on a value the PRODUCTION path derives (never a
    hand-set field) -- the deadline is the failure assertion."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate())


def _synth_logger(log: list):
    def _synth(text, out_path):
        log.append(text)
        return _write_wav(out_path)

    return _synth


# ──────────────────────────────────────────────────────────────────────────
# (2) pause mechanics — reason-before-cut, no-op when idle
# ──────────────────────────────────────────────────────────────────────────


def test_owed_fragment_is_synthesised_twice_and_the_job_resumes_from_its_cursor(router_motors):
    mixer = _GateMixerMusic(block_at=(1,))
    synth_log: list = []
    motor, rec, _ = _armed(
        router_motors, mixer_music=mixer, synthesize=_synth_logger(synth_log), interrupt_enabled=True
    )
    text = _job_text("A", 3)

    motor._speak_or_submit(text, source="kira-agenda:t1")
    assert mixer.entered[1].wait(5.0), "producer/consumer never reached fragment 1"

    motor.pause_speech_for_ptt()
    router = motor._speech_router
    assert _wait_until(lambda: len(router._stack) == 1), "job never suspended onto the stack"
    job = router._stack[0]
    assert job.cursor == 1, "cursor must be the in-flight idx, never chunks_played"
    assert job.spoken == [0]

    motor.resume_speech_after_ptt()
    assert rec.wait_for("speaking_end", 1, timeout=10.0), rec.names()

    fragment_1_text = _job_sentences("A", 3)[1]
    assert synth_log.count(fragment_1_text) == 2, "the owed fragment must be synthesised twice"
    assert job.cursor == 3 and job.state.value == "finished"
    assert sorted(job.spoken) == [0, 1, 2]


def test_pause_with_no_active_job_is_a_noop_beyond_ptt_held(router_motors):
    motor, rec, _ = _armed(router_motors, interrupt_enabled=True)

    motor.pause_speech_for_ptt()  # arms the router with nothing ACTIVE
    router = motor._speech_router
    assert router._ptt_held is True

    motor.resume_speech_after_ptt()
    assert router._ptt_held is False
    # Nothing was active at the press, so no pause request exists anywhere —
    # the request is JOB-local after closure M4, there is no global slot to
    # linger and poison the next pick.
    assert router.pause_pending_for_active() is False

    motor._speak_or_submit(_text(1), source="direct")
    assert rec.wait_for("speaking_end", 1), rec.names()
    assert rec.started_sources() == ["direct"], "the following job must not spuriously suspend"


def test_a_pause_that_lands_on_the_last_fragment_does_not_suspend_the_next_job(router_motors):
    """Design's "STALE PAUSE REASON" risk: `_reconcile` checks FINISHED
    before the pause branch, so a reason published after a fragment already
    finished cleanly must be discarded, never suspend the next job. Engineers
    the exact race deterministically (real playback still runs; only the
    ORDERING between "outcome complete" and "reason published" is fixed) --
    the same pattern as the existing `motor._hablar` stub in
    test_speech_router.py::test_chat_spoken_clock_does_not_advance_on_a_nonraising_error_return."""
    motor, rec, _ = _armed(router_motors, interrupt_enabled=True)
    real_hablar = motor._hablar
    pause_fired = threading.Event()

    def _hablar_that_races_a_late_pause(texto, source="direct", **kw):
        outcome = real_hablar(texto, source=source, **kw)  # real, clean finish
        motor._speech_router.pause_speech("ptt")  # reason published TOO LATE
        pause_fired.set()
        return outcome

    motor._hablar = _hablar_that_races_a_late_pause
    motor._speak_or_submit(_job_text("A", 1), source="kira-agenda:t1")

    assert rec.wait_for("speaking_end", 1, timeout=10.0), rec.names()
    assert pause_fired.is_set()
    router = motor._speech_router
    assert router._stack == [], "a pause reason published after a clean FINISH must not suspend it"
    # Closure M4: the request lives on the (now finished) job and dies with
    # it — no global slot exists for the next job to trip over.
    assert router.pause_pending_for_active() is False

    motor._speak_or_submit(_job_text("B", 1), source="kira-agenda:t2")
    assert rec.wait_for("speaking_end", 2, timeout=10.0), rec.names()
    assert router._stack == [], "the stale reason must not leak into the next job's reconcile"


# ──────────────────────────────────────────────────────────────────────────
# (3) _ptt_held — blocks INCOMING as well as STACK
# ──────────────────────────────────────────────────────────────────────────


def test_nothing_reaches_the_mixer_while_ptt_is_held_including_new_arrivals(router_motors):
    mixer = _GateMixerMusic(block_at=(0,))
    motor, rec, _ = _armed(router_motors, mixer_music=mixer, interrupt_enabled=True)

    motor._speak_or_submit(_job_text("A", 1), source="kira-agenda:t1")
    assert mixer.entered[0].wait(5.0)
    motor.pause_speech_for_ptt()

    router = motor._speech_router
    assert _wait_until(lambda: len(router._stack) == 1), "job never suspended"
    frozen_loads = mixer.load_count

    # A fresh priority-0 submit must not reach the mixer either -- point 3
    # is "whatever readies", not "resumes only".
    motor._speak_or_submit(_job_text("B", 1), source="direct")
    deadline = time.monotonic() + 0.3
    while time.monotonic() < deadline:
        assert mixer.load_count == frozen_loads, "mixer reached while PTT was held"
        time.sleep(0.01)

    motor.resume_speech_after_ptt()
    # D4: A claimed the boundary and never finished until it resumes -- B
    # plays silently as an inner job, so exactly ONE pair fires, at A's end.
    assert rec.wait_for("speaking_end", 1, timeout=10.0), rec.names()
    assert mixer.load_count > frozen_loads


# ──────────────────────────────────────────────────────────────────────────
# (4) precedence — INCOMING beats STACK after a release
# ──────────────────────────────────────────────────────────────────────────


def test_incoming_always_beats_the_stack_after_a_release(router_motors):
    mixer = _GateMixerMusic(block_at=(0,))
    synth_log: list = []
    motor, rec, _ = _armed(
        router_motors, mixer_music=mixer, synthesize=_synth_logger(synth_log), interrupt_enabled=True
    )

    motor._speak_or_submit(_job_text("A", 1), source="kira-agenda:t1")
    assert mixer.entered[0].wait(5.0)
    motor.pause_speech_for_ptt()
    router = motor._speech_router
    assert _wait_until(lambda: len(router._stack) == 1)

    motor._speak_or_submit(_job_text("B", 1), source="direct")  # INCOMING, while held
    motor.resume_speech_after_ptt()

    # D4: A owns the only pair (B never claims it); wait on the SYNTH LOG
    # reaching its final length instead of a second speaking_end that never
    # comes.
    a_text, b_text = _job_text("A", 1), _job_text("B", 1)
    assert _wait_until(lambda: len(synth_log) >= 3, timeout=10.0), synth_log
    assert rec.wait_for("speaking_end", 1, timeout=10.0), rec.names()
    assert synth_log == [a_text, b_text, a_text]


# ──────────────────────────────────────────────────────────────────────────
# (6) resolution 9 — the pick->playback window, incl. mid-emission
# ──────────────────────────────────────────────────────────────────────────


def test_a_press_landing_between_pick_and_playback_still_suspends(router_motors):
    """The window spans the FULL boundary-start emission (design's unlisted
    resolution 9), not just the gap before it -- fire the press from INSIDE
    the `speaking_start` consumer itself via the re-entry seam already used
    at test_speech_router.py:842 (`_Recorder.hooks`)."""
    mixer = _ScriptedMixerMusic()  # never blocks; the proof is 0 loads
    motor, rec, _ = _armed(router_motors, mixer_music=mixer, interrupt_enabled=True)
    rec.hooks["speaking_start"] = motor.pause_speech_for_ptt

    motor._speak_or_submit(_job_text("A", 1), source="kira-agenda:t1")

    router = motor._speech_router
    assert _wait_until(lambda: len(router._stack) == 1), "job never suspended in the window"
    assert mixer.load_count == 0, "no audio may reach the mixer once a hold lands in the window"
    job = router._stack[0]
    assert job.cursor == 0
    assert job.source == "kira-agenda:t1"


# ──────────────────────────────────────────────────────────────────────────
# (5) D3 uniform preemption — priority 0 always cuts; 1/2 never do
# ──────────────────────────────────────────────────────────────────────────


def test_priority_zero_submit_preempts_uniformly_and_lower_bands_never_do(router_motors):
    """Replaces the obsoleted step-2 pin
    (test_speech_router.py::test_jobs_run_in_priority_then_arrival_order_without_preemption,
    design §2 [CORRECTED] predicate + §10 D3/D4) -- see the step2_updates
    note for the design clause. `_speech_interrupt_enabled` defaults False
    (the real production default), so that step-2 test is untouched and
    still passes unmodified; this test explicitly arms the kill switch."""
    mixer = _GateMixerMusic(block_at=(0, 1, 2))
    synth_log: list = []
    motor, rec, _ = _armed(
        router_motors, mixer_music=mixer, synthesize=_synth_logger(synth_log), interrupt_enabled=True
    )

    agenda_text = _job_text("AGENDA", 1)
    chat_text = _job_text("CHAT", 1)
    agenda2_text = _job_text("AGENDA2", 1)
    direct_text = _job_text("DIRECT", 1)
    direct2_text = _job_text("DIRECT2", 1)

    motor._speak_or_submit(agenda_text, source="kira-agenda:a")
    assert mixer.entered[0].wait(5.0), "agenda job never reached playback"

    # Lower bands must NEVER preempt, even while queued behind an active job.
    motor._speak_or_submit(chat_text, source="chat")
    motor._speak_or_submit(agenda2_text, source="kira-agenda:d")
    deadline = time.monotonic() + 0.2
    while time.monotonic() < deadline:
        assert mixer.load_count == 1, "a non-owner submit preempted the active job"
        time.sleep(0.01)

    # D3 uniform: a priority-0 submit cuts the active job mid-sentence
    # WITHOUT the mixer ever releasing its block -- interrupt_speaking()
    # breaks the busy-wait independently of get_busy().
    motor._speak_or_submit(direct_text, source="direct")
    assert mixer.entered[1].wait(5.0), "the owner submit never preempted the agenda job"

    router = motor._speech_router
    assert _wait_until(lambda: len(router._stack) == 1)
    assert router._stack[0].source == "kira-agenda:a"
    assert router._stack[0].cursor == 0

    # Uniform at any priority: a SECOND priority-0 submit preempts the FIRST
    # one too (the design §1 t=55 case: C preempts B, both priority 0).
    motor._speak_or_submit(direct2_text, source="direct")
    assert mixer.entered[2].wait(5.0), "a priority-0 job must preempt another priority-0 job"
    assert _wait_until(lambda: len(router._stack) == 2)
    assert [j.source for j in router._stack] == ["kira-agenda:a", "direct"]

    mixer.release(2)
    assert rec.wait_for("speaking_end", 1, timeout=10.0), rec.names()

    assert synth_log == [
        agenda_text, direct_text, direct2_text, chat_text, agenda2_text, direct_text, agenda_text,
    ]
    # D4: AGENDA claimed the boundary first and is the ONLY job that ever
    # finishes on its own terms -- exactly one pair for the whole scenario,
    # despite DIRECT/DIRECT2 both playing real audio as inner (silent) jobs.
    assert rec.boundary_sources() == [
        ("speaking_start", "kira-agenda:a"),
        ("speaking_end", None),
    ]


def test_the_outer_pair_spans_a_preemption_and_the_inner_job_is_silent(router_motors):
    mixer = _GateMixerMusic(block_at=(0,))
    motor, rec, _ = _armed(router_motors, mixer_music=mixer, interrupt_enabled=True)

    motor._speak_or_submit(_job_text("AGENDA", 1), source="kira-agenda:a")
    assert mixer.entered[0].wait(5.0)
    motor._speak_or_submit(_job_text("DIRECT", 1), source="direct")  # preempts

    # DIRECT and AGENDA's resume both play unblocked (nothing else in
    # block_at) -- the proof here is the boundary pair, not stack depth.
    assert rec.wait_for("speaking_end", 1, timeout=10.0), rec.names()

    assert rec.boundary_sources() == [
        ("speaking_start", "kira-agenda:a"),
        ("speaking_end", None),
    ]
    names = rec.names()
    assert names.count("speaking_start") == 1 and names.count("speaking_end") == 1, (
        "the pair must be strictly serial, never nested -- " + repr(names)
    )


# ──────────────────────────────────────────────────────────────────────────
# (7) D4 discard-at-depth — sweeping the owner ends its pair once
# ──────────────────────────────────────────────────────────────────────────


def test_discarding_the_boundary_owner_at_depth_ends_its_pair_once(router_motors):
    mixer = _GateMixerMusic(block_at=(0, 1))
    motor, rec, _ = _armed(router_motors, mixer_music=mixer, interrupt_enabled=True)
    end_threads: list = []
    rec.hooks["speaking_end"] = lambda: end_threads.append(threading.current_thread().name)

    motor._speak_or_submit(_job_text("AGENDA", 1), source="kira-agenda:a")
    assert mixer.entered[0].wait(5.0)
    motor._speak_or_submit(_job_text("DIRECT", 1), source="direct")  # preempts -> AGENDA suspended
    assert mixer.entered[1].wait(5.0), "DIRECT never reached playback"

    router = motor._speech_router
    assert _wait_until(lambda: len(router._stack) == 1)

    # Sweep the owner off the stack WHILE the inner job is still ACTIVE --
    # mirrors the emergency-stop shape without the real HTTP path.
    motor.cancel_speech_for_sources(("kira-agenda",))
    assert _wait_until(lambda: router._stack == []), "the swept owner must leave the stack"

    mixer.release(1)  # let DIRECT finish naturally; the owed end fires after
    assert rec.wait_for("speaking_end", 1, timeout=10.0), rec.names()

    assert rec.boundary_sources() == [
        ("speaking_start", "kira-agenda:a"),
        ("speaking_end", None),
    ]
    assert end_threads == ["SpeechRouter"], "the owed end must fire on the ROUTER thread"
    # Self-caught (has_work() fix): 'idle' must never fire before the owed
    # end -- DIRECT's own _finish ran with the stack already swept empty,
    # and without `_owed_end` in has_work() it saw "no work" and idled
    # BEFORE the owner's speaking_end, so the avatar would idle then receive
    # an already-stale end a moment later. Also confirms 'idle' still fires
    # AT ALL after the owed end drains (a second self-caught regression: the
    # has_work() fix alone left nothing re-checking it once the sweep's own
    # _finish saw work still pending).
    assert rec.wait_for("idle", 1, timeout=10.0), rec.names()
    names = rec.names()
    assert names.index("idle") > names.index("speaking_end"), (
        "idle fired before the owed speaking_end -- " + repr(names)
    )
    motor.clear_speech_cancel()


# ──────────────────────────────────────────────────────────────────────────
# (8) emergency stop — T7/I8, edges 21-22
# ──────────────────────────────────────────────────────────────────────────


def test_emergency_stop_at_depth_two_discards_agenda_jobs_at_any_depth(router_motors, caplog):
    """Drives the REAL emergency sequence
    (routers/agenda.py:329-334's shape): cancel_speech_for_sources ->
    interrupt_speaking -> drop_pending_sources. AGENDA_A sits at the BOTTOM
    of a depth-2 stack (agenda + owner mixed); DIRECT_B survives the sweep
    (owner-origin, any depth) and eventually resumes and finishes; AGENDA_A
    is gone for good."""
    caplog.set_level(logging.INFO, logger="OpenCohost")
    mixer = _GateMixerMusic(block_at=(0, 1, 2))
    motor, rec, _ = _armed(router_motors, mixer_music=mixer, interrupt_enabled=True)

    motor._speak_or_submit(_job_text("AGENDA_A", 1), source="kira-agenda:a")
    assert mixer.entered[0].wait(5.0)
    motor._speak_or_submit(_job_text("DIRECT_B", 1), source="direct")  # preempts AGENDA_A
    assert mixer.entered[1].wait(5.0)
    motor._speak_or_submit(_job_text("DIRECT_C", 1), source="direct")  # preempts DIRECT_B
    assert mixer.entered[2].wait(5.0)

    router = motor._speech_router
    assert _wait_until(lambda: len(router._stack) == 2), "depth 2 never reached"
    assert [j.source for j in router._stack] == ["kira-agenda:a", "direct"]

    # The real emergency sequence (routers/agenda.py:288-334).
    motor.cancel_speech_for_sources(("kira-agenda",))
    motor.interrupt_speaking()  # cuts DIRECT_C, the currently-ACTIVE job
    removed = motor.drop_pending_sources(("kira-agenda",))
    assert removed == 0, "nothing was pending in the dispatch queue in this scenario"

    assert _wait_until(lambda: len(router._stack) == 1), "AGENDA_A never left the stack"
    assert router._stack[0].source == "direct", "DIRECT_B (owner-origin) must survive the sweep"

    mixer.release(2)  # let DIRECT_C's cut resolve; DIRECT_B then resumes+finishes on its own
    assert rec.wait_for("speaking_end", 2, timeout=10.0), rec.names()

    discard_lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("[SPEECH_DISCARD]")]
    cancelled_lines = [ln for ln in discard_lines if "reason=cancelled" in ln]
    assert len(cancelled_lines) == 1, discard_lines
    assert "source=kira-agenda:a" in cancelled_lines[0]
    assert "fragmento" not in cancelled_lines[0].lower(), "PRIVACY: no fragment text in the discard line"
    interrupted_lines = [ln for ln in discard_lines if "reason=interrupted" in ln]
    assert len(interrupted_lines) == 1 and "source=direct" in interrupted_lines[0], discard_lines

    motor.clear_speech_cancel()


def test_emergency_sweep_does_not_deadlock_when_the_caller_holds_agenda_lock(router_motors):
    """BLOCKER (design §4 sweeps / risk "THE DEADLOCK"): reproduces
    routers/agenda.py:288's shape -- the emergency-stop caller holds a
    NON-REENTRANT `agenda_lock` across cancel_speech_for_sources ->
    interrupt_speaking -> drop_pending_sources, and the boundary consumer
    (engine_host.py's `_handle_agenda_motor_event`) re-acquires that SAME
    lock. A synchronous emission from the sweep's own thread would
    self-deadlock. The failure mode is a HANG -- run the sweep on a worker
    thread and assert `join` actually completed."""
    mixer = _GateMixerMusic(block_at=(0, 1))
    agenda_lock = threading.Lock()
    motor, rec, _ = _armed(router_motors, mixer_music=mixer, interrupt_enabled=True)
    real_ui_callback = motor.ui_callback

    def _locking_ui_callback(event):
        with agenda_lock:  # mirrors engine_host.py's _handle_agenda_motor_event
            pass
        real_ui_callback(event)

    motor.ui_callback = _locking_ui_callback

    motor._speak_or_submit(_job_text("AGENDA", 1), source="kira-agenda:a")
    assert mixer.entered[0].wait(5.0)
    motor._speak_or_submit(_job_text("DIRECT", 1), source="direct")  # preempts -> AGENDA suspended
    assert mixer.entered[1].wait(5.0)

    router = motor._speech_router
    assert _wait_until(lambda: len(router._stack) == 1)

    done = threading.Event()

    def _emergency_stop_holding_agenda_lock():
        with agenda_lock:
            motor.cancel_speech_for_sources(("kira-agenda",))
            motor.interrupt_speaking()
            motor.drop_pending_sources(("kira-agenda",))
        done.set()

    worker = threading.Thread(target=_emergency_stop_holding_agenda_lock, daemon=True)
    worker.start()
    worker.join(5.0)
    assert done.is_set(), "the emergency sweep hung while the caller held agenda_lock"

    mixer.release(1)
    assert rec.wait_for("speaking_end", 1, timeout=10.0), rec.names()
    motor.clear_speech_cancel()


# ──────────────────────────────────────────────────────────────────────────
# (9) chat clock / idle under a pause — point 8 (confirms _finish needed no edit)
# ──────────────────────────────────────────────────────────────────────────


def test_a_paused_job_never_ticks_the_chat_clock_or_emits_idle(router_motors):
    mixer = _GateMixerMusic(block_at=(0,))
    motor, rec, _ = _armed(router_motors, mixer_music=mixer, interrupt_enabled=True)
    ticks: list = []
    motor.on_chat_turn_spoken = lambda: ticks.append(1)

    motor._speak_or_submit(_job_text("CHAT", 2), source="chat")
    assert mixer.entered[0].wait(5.0)

    motor.pause_speech_for_ptt()
    router = motor._speech_router
    assert _wait_until(lambda: len(router._stack) == 1)
    assert ticks == [], "the chat clock must not advance at a pause"
    assert "idle" not in rec.names(), "idle must not fire while the stack is non-empty"

    motor.resume_speech_after_ptt()
    assert rec.wait_for("speaking_end", 1, timeout=10.0), rec.names()
    assert rec.wait_for("idle", 1, timeout=10.0), rec.names()
    assert ticks == [1], "the chat clock must tick exactly once, at the real finish"
    assert rec.names().count("idle") == 1


# ──────────────────────────────────────────────────────────────────────────
# (10) step-3 kill switch — OFF reproduces step 2 exactly
# ──────────────────────────────────────────────────────────────────────────


def test_step_three_kill_switch_off_reproduces_step_two_behaviour(router_motors):
    mixer = _ScriptedMixerMusic(block_at=0)
    motor, rec, _ = _armed(router_motors, mixer_music=mixer, interrupt_enabled=False)

    motor._speak_or_submit(_job_text("AGENDA", 2), source="kira-agenda:a")
    assert mixer.entered_block.wait(5.0)

    motor._speak_or_submit(_job_text("CHAT", 1), source="chat")
    motor._speak_or_submit(_job_text("DIRECT", 1), source="direct")

    # Both no-ops with the switch off: the priority-0 submit above did not
    # preempt, and the PTT hook does nothing.
    motor.pause_speech_for_ptt()
    router = motor._speech_router
    assert router._stack == [] and router._ptt_held is False

    mixer.release()
    assert rec.wait_for("speaking_end", 3, timeout=10.0), rec.names()
    assert rec.started_sources() == ["kira-agenda:a", "direct", "chat"]


# ──────────────────────────────────────────────────────────────────────────
# (11) depth 3 — strict LIFO unwind, each job's LATEST cursor (I2, edge 19)
# ──────────────────────────────────────────────────────────────────────────


def test_depth_three_unwinds_strict_lifo_from_each_job_latest_cursor(router_motors):
    """NOTE on the synth log: the PRODUCER races ahead of the CONSUMER (the
    audio queue is `maxsize=3`), so within ONE `_hablar_impl` invocation both
    of A's fragments get synthesised regardless of where playback is cut --
    synth ORDER is not a reliable proxy for PLAYBACK order here. The unwind
    proof instead uses `completed_job_ids()` (append-ordered by REAL
    completion) and the mixer's own per-fragment `entered[]` gate, which
    fires exactly once per fragment in true playback order."""
    from opencohost.core.speech.router import PRIORITY_AGENDA

    mixer = _GateMixerMusic(block_at=(0, 2, 3, 4))
    synth_log: list = []
    motor, rec, _ = _armed(
        router_motors, mixer_music=mixer, synthesize=_synth_logger(synth_log), interrupt_enabled=True
    )
    a0, a1 = _job_sentences("A", 2)
    b0, = _job_sentences("B", 1)
    c0, = _job_sentences("C", 1)
    router = motor._speech_router  # None until the first submit -- built below

    # Suspend A mid FIRST fragment.
    job_a = motor._ensure_router().submit(_job_text("A", 2), "kira-agenda:a", PRIORITY_AGENDA)
    assert mixer.entered[0].wait(5.0)
    motor.pause_speech_for_ptt()
    router = motor._speech_router
    assert _wait_until(lambda: len(router._stack) == 1 and router._stack[0].cursor == 0)

    # Resume: A0 replays and plays through FREELY this time, advancing to
    # A1, where it is cut AGAIN -- proving the LATEST cursor (1), not the
    # first (0), is what survives.
    motor.resume_speech_after_ptt()
    assert mixer.entered[2].wait(5.0), "A never reached its second fragment on resume"
    motor.pause_speech_for_ptt()
    assert _wait_until(lambda: len(router._stack) == 1 and router._stack[0].cursor == 1)

    # While held, queue B and C -- neither can reach the mixer yet.
    job_b = router.submit(_job_text("B", 1), "kira-agenda:b", PRIORITY_AGENDA)
    job_c = router.submit(_job_text("C", 1), "kira-agenda:c", PRIORITY_AGENDA)

    motor.resume_speech_after_ptt()  # INCOMING beats STACK -> B picked first
    assert mixer.entered[3].wait(5.0)
    motor.pause_speech_for_ptt()
    assert _wait_until(lambda: len(router._stack) == 2)

    motor.resume_speech_after_ptt()  # INCOMING=[C] beats STACK -> C picked
    assert mixer.entered[4].wait(5.0)
    motor.pause_speech_for_ptt()
    assert _wait_until(lambda: len(router._stack) == 3), "depth 3 never reached"

    # Peak-depth snapshot: strict LIFO order, each job present exactly once,
    # A's cursor is its LATEST (1), never its first (0).
    assert [j.source for j in router._stack] == ["kira-agenda:a", "kira-agenda:b", "kira-agenda:c"]
    assert [j.cursor for j in router._stack] == [1, 0, 0]
    assert len(router._stack) == len(set(id(j) for j in router._stack))

    motor.resume_speech_after_ptt()
    assert rec.wait_for("speaking_end", 1, timeout=10.0), rec.names()

    # Strict LIFO unwind: C finishes first, then B, then A -- exactly the
    # reverse of push order (I2).
    assert router.completed_job_ids() == [job_c.job_id, job_b.job_id, job_a.job_id]
    # I4: every owed fragment was synthesised more than once (cut + resumed);
    # count-based, since the producer's own race makes exact order
    # untrustworthy. a0 is IN the pre_split slice for A's first two
    # activations only (cursor 0, then 0 again) = 2; a1 is in the slice for
    # ALL THREE of A's activations (cursor 0, 0, then 1) = 3 -- eagerly
    # synthesised each time even on the two that never got to play it.
    assert synth_log.count(a0) == 2
    assert synth_log.count(a1) == 3
    assert synth_log.count(b0) == 2 and synth_log.count(c0) == 2
    # D4: only A (the depth-0 owner) ever emits a boundary pair.
    assert rec.boundary_sources() == [
        ("speaking_start", "kira-agenda:a"),
        ("speaking_end", None),
    ]


# ──────────────────────────────────────────────────────────────────────────
# (12) integration — §5.2/point 7: PTT suspend, resume-as-filler, THEN preempt
# ──────────────────────────────────────────────────────────────────────────


def test_answer_preempts_the_filler_again_after_a_ptt_up_resume(router_motors):
    """Press -> suspend A -> release -> A resumes AS FILLER -> a priority-0
    answer preempts A again, at its LATEST cursor (not its first) -> the
    answer plays -> A resumes and finishes. Nothing lost. Submits directly
    on the router surface (`_speak_or_submit`), not through the engine's
    `:2982` pop gate (step 4, out of scope here)."""
    mixer = _GateMixerMusic(block_at=(0, 2, 3))
    synth_log: list = []
    motor, rec, _ = _armed(
        router_motors, mixer_music=mixer, synthesize=_synth_logger(synth_log), interrupt_enabled=True
    )
    a0, a1 = _job_sentences("A", 2)
    b0, = _job_sentences("B", 1)

    motor._speak_or_submit(_job_text("A", 2), source="kira-agenda:a")
    assert mixer.entered[0].wait(5.0)
    motor.pause_speech_for_ptt()
    router = motor._speech_router
    assert _wait_until(lambda: len(router._stack) == 1 and router._stack[0].cursor == 0)

    # PTT_UP: A resumes as filler, plays a0 through, and is still speaking
    # (as filler) when it reaches a1.
    motor.resume_speech_after_ptt()
    assert mixer.entered[2].wait(5.0), "A never reached its second fragment as filler"

    # RESPONSE_READY: a priority-0 answer preempts the filler AGAIN, at its
    # LATEST cursor (1 -- a0 was genuinely spoken this time), not its first.
    motor._speak_or_submit(_job_text("B", 1), source="direct")
    assert _wait_until(lambda: len(router._stack) == 1 and router._stack[0].cursor == 1)
    assert mixer.entered[3].wait(5.0), "the answer never preempted the filler"

    mixer.release(3)  # let the answer finish; A then resumes and finishes on its own
    assert rec.wait_for("speaking_end", 1, timeout=10.0), rec.names()

    assert router._stack == []
    # I1: every fragment index is accounted for -- both of A's spoken (once
    # for real, on the activation that actually reached FIN) and B's.
    assert synth_log.count(a0) == 2  # eager first attempt + the real play
    assert synth_log.count(a1) == 3  # eager twice (cut, cut again) + the real play
    assert synth_log.count(b0) == 1  # never interrupted, never resumed
    assert rec.boundary_sources() == [
        ("speaking_start", "kira-agenda:a"),
        ("speaking_end", None),
    ]


# ──────────────────────────────────────────────────────────────────────────
# (9) review closure (2026-08-05) — the five probe-proven races, pinned
# ──────────────────────────────────────────────────────────────────────────


def test_a_press_swallowed_in_the_hablar_entry_gap_still_suspends(router_motors):
    """Closure B1 (BLOCKER): a press landing AFTER `_run_job`'s window check
    but BEFORE `_hablar_impl` re-arms `_speaking` had its interrupt STOMPED
    by that assignment — the whole utterance played into a live mic (probe:
    4/4 fragments loaded, job FINISHED, hold still up). The re-check inside
    `_hablar_impl` (`pause_pending_for_active`) un-stomps it. The press is
    landed via the second `_speech_cancelled` call — the one `_hablar`
    itself makes INSIDE the gap."""
    mixer = _ScriptedMixerMusic()
    motor, rec, _ = _armed(router_motors, mixer_music=mixer, interrupt_enabled=True)

    real_cancelled = motor._speech_cancelled
    calls = {"n": 0}

    def _press_inside_the_gap(source):
        calls["n"] += 1
        if calls["n"] == 2:  # after the window check, before the re-arm
            t = threading.Thread(target=motor.pause_speech_for_ptt, daemon=True)
            t.start()
            t.join(5.0)
        return real_cancelled(source)

    motor._speech_cancelled = _press_inside_the_gap
    motor._speak_or_submit(_text(4), source="kira-agenda:t1")
    router = motor._speech_router

    assert _wait_until(lambda: len(router._stack) == 1, timeout=10.0), (
        "the swallowed press never suspended the job"
    )
    assert mixer.load_count == 0, (
        f"{mixer.load_count} fragments played into a live mic"
    )
    assert router._ptt_held is True

    motor._speech_cancelled = real_cancelled
    motor.resume_speech_after_ptt()
    assert rec.wait_for("speaking_end", 1, timeout=10.0), rec.names()
    assert rec.names().count("speaking_start") == 1, "one job, ONE pair (D4)"
    assert mixer.load_count == 4, "the full utterance must replay after the hold"


def test_a_sweep_racing_the_claim_cannot_nest_boundary_pairs(router_motors):
    """Closure B2 (BLOCKER): a sweep clearing `_boundary_owner` between the
    loop-top owed-end flush and `_run_job`'s claim let the fresh job emit
    `speaking_start` with the swept owner's `speaking_end` still owed —
    start,start,end,end (probe-proven; the trailing end completes an agenda
    turn that never ran). The owed end is now collected ATOMICALLY with the
    claim. The sweep lands inside the fresh job's own cancel-check window,
    exactly where a real emergency-stop thread gets scheduled."""
    mixer = _GateMixerMusic(block_at=(0,))
    motor, rec, _ = _armed(router_motors, mixer_music=mixer, interrupt_enabled=True)

    real_cancelled = motor._speech_cancelled
    swept = threading.Event()

    def _sweep_inside_the_window(source):
        if source == "chat" and not swept.is_set():
            swept.set()
            t = threading.Thread(
                target=motor.cancel_speech_for_sources,
                args=(("kira-agenda",),),
                daemon=True,
            )
            t.start()
            t.join(5.0)
        return real_cancelled(source)

    motor._speech_cancelled = _sweep_inside_the_window

    motor._speak_or_submit(_text(4), source="kira-agenda:t1")
    router = motor._speech_router
    assert rec.wait_for("speaking_start", 1, 10.0)
    assert mixer.entered[0].wait(10.0), "agenda job never reached fragment 0"

    motor._speak_or_submit(_text(2), source="chat")
    router.pause_speech("preempt")  # owner cut and pushed, still the owner
    mixer.release(0)

    assert rec.wait_for("speaking_end", 2, timeout=10.0), rec.names()
    pairs = [n for n in rec.names() if n in ("speaking_start", "speaking_end")]
    for i, n in enumerate(pairs):
        assert n == ("speaking_start" if i % 2 == 0 else "speaking_end"), (
            f"NESTED PAIR: {pairs}"
        )


def test_a_stale_preempt_aborts_instead_of_cutting_the_successor(router_motors):
    """Closure M3: `submit` observed job A active, but its pause landed
    after A left and the preemptor's own answer B was already playing — the
    cut suspended B (audible stutter + replay from cursor 0, probe-proven).
    `pause_speech` now takes the observed TARGET and aborts wholesale —
    interrupt included — when it is no longer the active job."""
    from opencohost.core.speech.router import PRIORITY_OWNER, SpeechJob

    mixer = _GateMixerMusic(block_at=(0, 1))
    motor, rec, _ = _armed(router_motors, mixer_music=mixer, interrupt_enabled=True)

    motor._speak_or_submit(_text(4), source="kira-agenda:t1")
    router = motor._speech_router
    assert rec.wait_for("speaking_start", 1, 10.0)
    assert mixer.entered[0].wait(10.0), "agenda job never reached fragment 0"
    victim = router._active
    assert victim is not None

    # submit()'s two halves, split exactly like the probe: enqueue+decision
    # under the lock; the pause deferred as if the thread were descheduled.
    with router._sched_lock:
        router._next_job_id += 1
        owner = SpeechJob(
            job_id=router._next_job_id, text=_text(3),
            source="direct", priority=PRIORITY_OWNER,
        )
        router._incoming.append(owner)
    router._wake.set()

    # ...meanwhile the observed job is cut/finished by another path and the
    # router moves on to the owner job itself.
    motor.cancel_speech_for_sources(("kira-agenda",))
    motor.interrupt_speaking()
    mixer.release(0)
    assert mixer.entered[1].wait(10.0), "owner job never reached the mixer"
    assert router._active is owner

    # NOW the stale preempt lands — with the target it observed back then.
    router.pause_speech("preempt", target=victim)
    mixer.release(1)
    assert rec.wait_for("speaking_end", 2, timeout=10.0), rec.names()
    assert owner.suspensions == 0, "a stale preempt cut the job it did not observe"
    assert owner.state is SpeechJobState.FINISHED


def test_a_tap_between_reconcile_and_finish_cannot_leak_into_the_next_job(router_motors):
    """Closure M4: a pause landing after a job's reconcile consumed its
    request (but before `_finish` cleared `_active`) used to park a reason
    in a global slot; the NEXT job's window check then suspended it with
    `_active=None` — starved forever behind every later arrival under D1
    (probe-proven). The request is JOB-LOCAL now and dies with its job."""
    motor, rec, _ = _armed(
        router_motors, mixer_music=_ScriptedMixerMusic(), interrupt_enabled=True
    )
    motor._speak_or_submit(_text(2), source="chat")
    router = motor._speech_router

    real_reconcile = router._reconcile
    tapped = threading.Event()

    def _tap_in_the_gap(job, outcome, exc):
        result = real_reconcile(job, outcome, exc)
        if result[0] is SpeechJobState.FINISHED and not tapped.is_set():
            tapped.set()

            def _tap():
                motor.pause_speech_for_ptt()      # accidental short press
                motor.resume_speech_after_ptt()   # ...released immediately

            t = threading.Thread(target=_tap, daemon=True)
            t.start()
            t.join(5.0)
        return result

    router._reconcile = _tap_in_the_gap

    assert rec.wait_for("speaking_end", 1, 10.0), "first job never finished"
    assert tapped.is_set()

    job2 = router.submit(_text(2), "chat", 1)
    assert rec.wait_for("speaking_end", 2, 10.0), rec.names()
    assert job2.suspensions == 0, "a dead job's pause request leaked into the next job"
    assert job2.state is SpeechJobState.FINISHED


def test_a_raise_during_a_published_pause_suspends_instead_of_discarding(router_motors):
    """Closure MINOR (§2 reconcile order): the cut was REQUESTED, so the
    job is owed a resume even though the unwind raised — discarding it here
    lost the untold tail of a paused turn (contract point 7)."""
    motor, rec, _ = _armed(router_motors, interrupt_enabled=True)
    router = motor._ensure_router()

    def _publish_pause_then_raise(texto, source="direct", **kw):
        motor.pause_speech_for_ptt()  # the request lands on the ACTIVE job
        raise RuntimeError("mixer died mid-unwind")

    motor._hablar = _publish_pause_then_raise
    job = router.submit(_text(3), "kira-agenda:t1", 2)

    assert _wait_until(lambda: len(router._stack) == 1, timeout=10.0), (
        "the paused job was discarded instead of suspended"
    )
    assert router._stack[0] is job
    assert job.state is SpeechJobState.SUSPENDED
    assert job.retries == 0 and job.suspensions == 1


def test_a_suspended_job_survives_a_tier_switch(router_motors):
    """I9 pin (closure MINOR): a suspended, partly-spoken, GPU-paid job
    survives a tier flip. Holds by construction today — the depth sweep
    lives ONLY in the cancel/drop paths, never in `_invalidate_frozen_stash`
    — and this pins that separation so a future step cannot wire the stash
    chokepoint to the speech stack."""
    mixer = _GateMixerMusic(block_at=(0,))
    motor, rec, _ = _armed(router_motors, mixer_music=mixer, interrupt_enabled=True)
    motor._speak_or_submit(_text(3), source="kira-agenda:t1")
    router = motor._speech_router
    assert mixer.entered[0].wait(10.0)
    motor.pause_speech_for_ptt()
    assert _wait_until(lambda: len(router._stack) == 1)
    job = router._stack[0]

    motor.switch_llm_tier("balanced")  # an _invalidate_frozen_stash site

    assert router._stack == [job], "the tier switch swept the speech stack"
    assert job.state is SpeechJobState.SUSPENDED

    motor.resume_speech_after_ptt()
    assert rec.wait_for("speaking_end", 1, timeout=10.0), rec.names()
    assert job.state is SpeechJobState.FINISHED


def test_stack_depth_high_water_logs_at_four(router_motors, caplog):
    """§4's ONLY guard on the unbounded stack (D1: no expiry). Closure NIT:
    sampled at the PUSH site now — a spike that forms and unwinds inside
    one iteration can no longer dodge it — and pinned here for the first
    time. Also re-verifies D4 at depth: one tower, ONE boundary pair."""
    mixer = _GateMixerMusic(block_at=(0, 1, 2, 3, 4))
    motor, rec, _ = _armed(router_motors, mixer_music=mixer, interrupt_enabled=True)

    with caplog.at_level(logging.WARNING, logger="OpenCohost"):
        motor._speak_or_submit(_text(2), source="kira-agenda:t1")
        router = motor._speech_router
        assert mixer.entered[0].wait(10.0)
        for i in range(1, 5):
            motor._speak_or_submit(_text(2), source="direct")  # prio-0 preempt
            assert _wait_until(lambda: len(router._stack) == i, timeout=10.0), (
                f"depth never reached {i}"
            )
            if i < 4:
                assert mixer.entered[i].wait(10.0), f"job {i} never reached the mixer"

    assert any("[SPEECH_STACK] depth=4" in r.message for r in caplog.records), (
        "the high-water guard never fired at depth 4"
    )
    assert not any("depth=3" in r.message for r in caplog.records), "threshold is >=4"

    for i in range(5):
        mixer.release(i)
    assert rec.wait_for("speaking_end", 1, timeout=15.0), rec.names()
    assert rec.names().count("speaking_start") == 1, "one tower, ONE pair (D4)"


# ──────────────────────────────────────────────────────────────────────────
# (10) judge closure (2026-08-05) — two blind opus judges over the
# uncommitted step-3 diff, every finding probe-reproduced before fixing
# ──────────────────────────────────────────────────────────────────────────


def test_router_off_with_interrupt_on_is_pure_legacy_and_loses_nothing(router_motors):
    """Judge closure (2026-08-05, MAJOR, judge A): flipping ONLY
    `_speech_router_enabled` False — the documented whole-router kill switch,
    the natural incident revert — left step 3 armed: a PTT press built a
    router no job would ever reach, armed its hold, and `_hablar_impl`'s
    closure-B1 re-check then cleared `_speaking` on the LEGACY path before
    the first fragment synthesised. No stack to suspend to, no discard log:
    every turn spoken during the hold silently vanished. The step-3
    entrypoints now require the router to BE the playback path (conjunction),
    so router-off strictly dominates and the single-flag revert is never
    worse than step 2."""
    synth_log: list = []
    motor, rec, _ = _armed(
        router_motors, mixer_music=_ScriptedMixerMusic(),
        synthesize=_synth_logger(synth_log), enabled=False, interrupt_enabled=True,
    )

    motor.pause_speech_for_ptt()  # press: must be a no-op END TO END
    assert motor._speech_router is None, (
        "a press must not build a router the playback path never uses"
    )
    assert motor._speech_pause_pending() is False

    motor._speak_or_submit(_job_text("L", 2), source="kira-agenda:a")  # legacy, BLOCKING

    assert synth_log == _job_sentences("L", 2), (
        "the legacy turn was cut to silence by the half-armed step-3 switch"
    )

    motor.resume_speech_after_ptt()  # PTT_UP funnel: still a safe no-op
    assert motor._speech_router is None


def test_disarming_the_interrupt_switch_in_process_stops_preemption(router_motors):
    """Judge closure (2026-08-05, MINOR, judge A): `interrupt_enabled` was a
    build-time snapshot copied onto the router — flipping
    `motor._speech_interrupt_enabled = False` after the router existed
    half-disarmed step 3: the pause/resume entrypoints died while a
    priority-0 submit STILL preempted and suspended the victim, the exact
    combination the switch exists to prevent (and the two engine_host writes
    carried an unenforced ordering requirement). The router now reads the
    motor flags LIVE, so the switch means what it says at any moment."""
    mixer = _GateMixerMusic(block_at=(0,))
    synth_log: list = []
    motor, rec, _ = _armed(
        router_motors, mixer_music=mixer, synthesize=_synth_logger(synth_log),
        interrupt_enabled=True,
    )

    motor._speak_or_submit(_job_text("A", 2), source="kira-agenda:a")
    assert mixer.entered[0].wait(5.0)
    router = motor._speech_router
    job_a = router._active
    assert job_a is not None and job_a.state is SpeechJobState.ACTIVE

    motor._speech_interrupt_enabled = False  # in-process disarm
    motor._speak_or_submit(_job_text("B", 1), source="direct")  # prio-0 submit

    # Preemption publishes synchronously inside submit(): with the switch
    # read live, NEITHER the request nor a suspension may exist afterwards.
    assert job_a.pause_requested is None and job_a.suspensions == 0, (
        "a priority-0 submit preempted with the interrupt switch OFF"
    )

    mixer.release(0)
    assert _wait_until(lambda: len(router.completed_job_ids()) == 2, timeout=10.0)
    assert job_a.suspensions == 0 and job_a.state is SpeechJobState.FINISHED
    assert synth_log == _job_sentences("A", 2) + _job_sentences("B", 1), (
        "disarmed = strictly serial playback, A fully before B"
    )


def test_a_resumed_job_keeps_turn_relative_progress(router_motors):
    """Judge closure (2026-08-05, convergent MAJOR: judge A minor, judge B
    major — same mechanism found blind): a resume re-armed
    `_speech_progress` with the owed slice as denominator (`total` =
    len(pre_split), `played` from 0), so every `played/total` consumer — the
    WU5 zone gate, the would-fire telemetry — read a nearly-finished resumed
    turn as barely started and un-earned its LATE-zone defer protection
    (probe: honest 0.833 read as 0.5). The router now hands `_hablar_impl`
    the turn-absolute `cursor_base`; `total`/`played` stay turn-relative and
    `base` keeps `speech_remaining_estimate`'s rate slice-local."""
    mixer = _GateMixerMusic(block_at=(1, 3))
    motor, rec, _ = _armed(router_motors, mixer_music=mixer, interrupt_enabled=True)

    motor._speak_or_submit(_job_text("P", 6), source="kira-agenda:a")
    assert mixer.entered[1].wait(5.0)  # fragment 0 played; fragment 1 gated
    router = motor._speech_router

    motor.pause_speech_for_ptt()  # press mid-fragment-1
    assert _wait_until(lambda: len(router._stack) == 1, timeout=5.0)
    job = router._stack[-1]
    assert job.cursor == 1, "cursor honesty: the gated fragment replays"

    motor.resume_speech_after_ptt()  # tail = chunks[1:], resumed by _pick()
    # Global load 2 = absolute fragment 1 (replayed); global load 3 =
    # absolute fragment 2, gated — one tail fragment has finished playing.
    assert mixer.entered[3].wait(5.0)

    with motor._lock:
        progress = dict(motor._speech_progress)
    assert progress["total"] == 6, (
        "progress must count the TURN, not the owed slice"
    )
    assert progress["played"] == 2, "base (1) + one tail fragment played"
    assert progress["base"] == 1, "slice-local rate anchor for the estimate"

    mixer.release(3)
    assert _wait_until(lambda: job.state is SpeechJobState.FINISHED, timeout=10.0)
    assert job.cursor == 6 and sorted(job.spoken) == list(range(6))


def test_hold_and_pause_arms_and_stamps_in_one_acquisition(router_motors):
    """Judge closure (2026-08-05, MINOR, judge B): `pause_speech_for_ptt`
    published the hold and the victim's request in TWO `_sched_lock`
    acquisitions (`set_ptt_held(True)` then `pause_speech("ptt")`); a
    closure-B1 cut landing in the gap between them reached reconcile with
    no request and DISCARDED the held job — whole utterance lost while the
    mic was live. `hold_and_pause` publishes both under ONE acquisition (the
    gap no longer exists), still interrupts OUTSIDE the lock (I10), and the
    hold set in the same acquisition means no successor can arm — the
    stale-cut residual `pause_speech` documents cannot happen on this path."""
    mixer = _GateMixerMusic(block_at=(0,))
    motor, rec, _ = _armed(router_motors, mixer_music=mixer, interrupt_enabled=True)

    motor._speak_or_submit(_job_text("H", 2), source="kira-agenda:a")
    assert mixer.entered[0].wait(5.0)
    router = motor._speech_router
    job = router._active

    router.hold_and_pause("ptt")
    assert router._ptt_held is True
    assert _wait_until(lambda: job.state is SpeechJobState.SUSPENDED, timeout=5.0), (
        "a held cut must SUSPEND (request stamped with the hold), never discard"
    )
    assert job.cursor == 0

    router.set_ptt_held(False)  # release: _pick() pops and replays the tail
    assert _wait_until(lambda: job.state is SpeechJobState.FINISHED, timeout=10.0)
    assert sorted(job.spoken) == [0, 1], "lossless across the atomic press"

    # A press with nothing active still arms the hold (a press landing
    # before this process ever spoke must block `_pick()`).
    motor2, _, _ = _armed(router_motors, interrupt_enabled=True)
    router2 = motor2._ensure_router()
    router2.hold_and_pause("ptt")
    assert router2._ptt_held is True
