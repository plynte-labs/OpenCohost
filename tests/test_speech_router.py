"""Strict TDD for step 2 of the speech-router landing sequence
(conductor/tracks/interruptible_speech_architecture_20260804/speech-router-design.md
§8 step 2, §11 B1-B6).

Playback moves to ONE router thread that is the only caller of `_hablar` in
the process. Behavior-preserving: no stack, no pause/resume, no `_ptt_held`,
no preemption of the active job -- jobs run to completion in priority band,
then arrival order within a band. Divergence from legacy plain call-order is
bounded to sub-ms simultaneity races between two submitters.

Step 3 (stack, pause/resume, `_ptt_held`, D3-uniform preemption, D4
outermost-only emission) lives in tests/test_speech_router_stack.py, gated
behind the SEPARATE `interrupt_enabled`/`_speech_interrupt_enabled` kill
switch (default False) -- every test in THIS file still runs with that
switch off and stays a byte-identical pin of step 2's behavior, even though
the router underneath is now the step-3 implementation.

Section 7 discipline, inherited verbatim from
tests/test_speech_outcome_capture.py: a real `MotorVocalIA`, a REAL router
thread, the REAL `_hablar_impl` producer/consumer loop. ONLY Piper synthesis
and `pygame.mixer` are faked (with a controllable `get_busy()`).
`threading.Event` handshakes only -- never `time.sleep` as a synchronizer,
never a hand-set field the code is supposed to derive.
"""
from __future__ import annotations

import logging
import queue
import threading
import time

import pytest

from opencohost.core import llm_engine
from opencohost.core.speech.router import (
    SPEECH_BOUNDARY_COMMAND,
    SpeechJob,
    SpeechJobState,
)
from tests.test_speech_outcome_capture import (
    _ScriptedMixerMusic,
    _make_motor,
    _sentences,
)


# ──────────────────────────────────────────────────────────────────────────
# Harness
# ──────────────────────────────────────────────────────────────────────────


class _Recorder:
    """`ui_callback` spy that samples the derived state AT EVENT TIME (never
    afterwards), so a gap in `_speech_active` cannot hide between two
    assertions. `hooks` lets a test re-enter the engine from inside an event
    consumer -- which is exactly what CTK's `on_motor_speaking_end` does."""

    def __init__(self, motor):
        self._motor = motor
        self._lock = threading.Lock()
        self.events: list = []
        self.hooks: dict = {}

    def __call__(self, event):
        # Read the public predicates from INSIDE the callback: if the router
        # emitted while holding _sched_lock, is_speaking would deadlock here.
        sample = (event, self._motor.current_speech_source, self._motor.is_speaking)
        with self._lock:
            self.events.append(sample)
        hook = self.hooks.get(event)
        if hook is not None:
            hook()

    def names(self) -> list:
        with self._lock:
            return [e[0] for e in self.events]

    def boundary_sources(self) -> list:
        with self._lock:
            return [(e[0], e[1]) for e in self.events if e[0] in ("speaking_start", "speaking_end")]

    def started_sources(self) -> list:
        with self._lock:
            return [e[1] for e in self.events if e[0] == "speaking_start"]

    def wait_for(self, event: str, count: int, timeout: float = 10.0) -> bool:
        """Event-driven poll on a value the PRODUCTION path derives. Not a
        sleep-as-synchronizer: the deadline is the failure assertion, and the
        sampling interval only bounds how fast the test notices."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.names().count(event) >= count:
                return True
            time.sleep(0.005)
        return False


@pytest.fixture
def router_motors():
    """Every motor a test arms is stopped here -- the router is a daemon
    thread, and 5000+ leaked ones would make the suite unreadable."""
    made: list = []
    yield made
    for motor in made:
        router = getattr(motor, "_speech_router", None)
        if router is not None:
            router.stop(timeout=5.0)


def _armed(router_motors, *, mixer_music=None, synthesize=None, enabled=True):
    motor, log_q, _ = _make_motor(mixer_music=mixer_music, synthesize=synthesize)
    rec = _Recorder(motor)
    motor.ui_callback = rec
    motor._speech_router_enabled = enabled
    router_motors.append(motor)
    return motor, rec, log_q


class _GateMixerMusic:
    """`_ScriptedMixerMusic` with more than one gate: blocks the consumer at
    each load index in `block_at` until the test releases that index. Same
    mechanism (the production busy-wait trips a real Event), just scriptable
    across two jobs."""

    def __init__(self, block_at=()):
        self.block_at = set(block_at)
        self.load_count = 0
        self.stopped = False
        self.entered = {i: threading.Event() for i in self.block_at}
        self._released = {i: threading.Event() for i in self.block_at}

    def load(self, _path):
        self.load_count += 1

    def play(self):
        pass

    def get_busy(self):
        idx = self.load_count - 1
        if idx in self.block_at and not self._released[idx].is_set():
            self.entered[idx].set()
            return True
        return False

    def stop(self):
        self.stopped = True

    def unload(self):
        pass

    def release(self, idx):
        self._released[idx].set()


def _text(n: int = 3) -> str:
    return " ".join(_sentences(n))


# ──────────────────────────────────────────────────────────────────────────
# (1) submit order — priority then arrival, and NO preemption
# ──────────────────────────────────────────────────────────────────────────


def test_jobs_run_in_priority_then_arrival_order_without_preemption(router_motors):
    mixer = _ScriptedMixerMusic(block_at=0)
    motor, rec, _ = _armed(router_motors, mixer_music=mixer)

    motor._speak_or_submit(_text(2), source="kira-agenda:a")
    assert mixer.entered_block.wait(5.0), "router never reached playback of the first job"

    # Everything below arrives while job A is ACTIVE.
    motor._speak_or_submit(_text(1), source="chat")          # priority 1
    motor._speak_or_submit(_text(1), source="direct")        # priority 0 (owner)
    motor._speak_or_submit(_text(1), source="kira-agenda:d")  # priority 2

    mixer.release()
    assert rec.wait_for("speaking_end", 4), rec.names()

    assert rec.started_sources() == [
        "kira-agenda:a",  # already ACTIVE — an owner submit does NOT preempt at step 2
        "direct",         # priority 0 first
        "chat",           # then priority 1
        "kira-agenda:d",  # then priority 2
    ]


def test_submit_is_non_blocking_and_returns_before_speech_completes(router_motors):
    mixer = _ScriptedMixerMusic(block_at=0)
    motor, rec, _ = _armed(router_motors, mixer_music=mixer)

    motor._speak_or_submit(_text(3), source="kira-agenda:t1")

    # The caller is back while the router is still mid-playback.
    assert mixer.entered_block.wait(5.0)
    assert "speaking_end" not in rec.names()
    mixer.release()
    assert rec.wait_for("speaking_end", 1), rec.names()


# ──────────────────────────────────────────────────────────────────────────
# (2) THE step-2 assertion: _speech_active has no submit->pick gap
# ──────────────────────────────────────────────────────────────────────────


def test_speech_active_is_continuously_true_from_submit_to_final_speaking_end(router_motors):
    """No gap while the router still has work — the submit->pick window AND
    the inter-job boundary. (The very last drop clears it BEFORE emitting that
    job's speaking_end, exactly as `_hablar`'s tail always did: a speaking_end
    consumer must observe "speech has ended".)"""
    # Loads: job1 fragment 0 -> 0, job1 fragment 1 -> 1, job2 fragment 0 -> 2.
    mixer = _GateMixerMusic(block_at=(0, 2))
    motor, rec, _ = _armed(router_motors, mixer_music=mixer)

    # The window the audit found (§11 B2): submit returns, the engine thread
    # runs on to _complete_processing_cycle -> _process_priority_queue and
    # tests the pop gate BEFORE the router has picked anything.
    motor._speak_or_submit(_text(2), source="kira-agenda:t1")
    assert motor.is_speaking is True, "submit->pick window left is_speaking False"

    seen_false = []
    stop = threading.Event()

    def _sampler():
        # Sampling loop, NOT a synchronizer: the handshakes below are Events.
        while not stop.is_set():
            if not motor.is_speaking:
                seen_false.append(time.monotonic())
            time.sleep(0.001)

    sampler = threading.Thread(target=_sampler, daemon=True)
    sampler.start()
    try:
        assert mixer.entered[0].wait(5.0)
        # A second job queued behind the active one closes the inter-job window.
        motor._speak_or_submit(_text(1), source="kira-agenda:t2")
        mixer.release(0)
        # Job 1 completes, is reconciled and dropped, job 2 is picked and is now
        # blocked mid-playback: the whole boundary has been crossed.
        assert mixer.entered[2].wait(5.0), "job 2 never started"
    finally:
        stop.set()
        sampler.join(5.0)

    assert seen_false == [], "is_speaking dropped False across the job boundary"
    # The FIRST job's speaking_end still reports speech active: job 2 is queued.
    first_end = [e for e in rec.events if e[0] == "speaking_end"][0]
    assert first_end[2] is True, "inter-job boundary opened a gap"

    mixer.release(2)
    assert rec.wait_for("speaking_end", 2), rec.names()


# ──────────────────────────────────────────────────────────────────────────
# (3) B1 — exactly one boundary pair per job, including a zero-fragment job
# ──────────────────────────────────────────────────────────────────────────


def test_exactly_one_boundary_pair_per_job_including_a_zero_fragment_job(router_motors):
    motor, rec, _ = _armed(router_motors)

    motor._speak_or_submit(_text(2), source="kira-agenda:t1")
    assert rec.wait_for("speaking_end", 1), rec.names()
    # "ab" survives the splitter but is filtered by the len>3 rule -> zero
    # fragments -> the §11 B1 third emission site.
    motor._speak_or_submit("ab", source="kira-agenda:t2")
    assert rec.wait_for("speaking_end", 2), rec.names()

    assert rec.boundary_sources() == [
        ("speaking_start", "kira-agenda:t1"),
        ("speaking_end", None),
        ("speaking_start", "kira-agenda:t2"),
        ("speaking_end", None),
    ]


def test_hablar_impl_emits_no_boundary_events_of_its_own(router_motors):
    """B1: the emission is the ROUTER's (or the legacy `_hablar` wrapper's).
    `_hablar_impl` must be silent, else a resumed job (step 3) gets a second
    pair and the agenda controller completes a turn that is still speaking."""
    motor, rec, _ = _armed(router_motors, enabled=False)

    motor._hablar_impl(_text(2), source="kira-agenda:t1")
    motor._hablar_impl("ab", source="kira-agenda:t2")  # the zero-fragment path

    assert rec.boundary_sources() == []


def test_only_one_hablar_caller_never_contends_on_the_belt_lock(router_motors, caplog):
    """I11: one caller means the non-blocking acquire always wins, and a
    contention line becomes a genuine bypass-regression alarm."""
    caplog.set_level(logging.INFO, logger="OpenCohost")
    motor, rec, _ = _armed(router_motors)

    for i in range(4):
        motor._speak_or_submit(_text(2), source=f"kira-agenda:t{i}")
    assert rec.wait_for("speaking_end", 4), rec.names()

    contention = [r.getMessage() for r in caplog.records if "hablar contention" in r.getMessage()]
    assert contention == []


# ──────────────────────────────────────────────────────────────────────────
# (4) B3 — 'idle' never mid-speech, exactly once after the last job
# ──────────────────────────────────────────────────────────────────────────


def test_idle_never_fires_mid_speech_and_exactly_once_after_the_last_job(router_motors):
    mixer = _ScriptedMixerMusic(block_at=0)
    motor, rec, _ = _armed(router_motors, mixer_music=mixer)

    motor._speak_or_submit(_text(2), source="kira-agenda:t1")
    assert mixer.entered_block.wait(5.0)
    motor._speak_or_submit(_text(1), source="kira-agenda:t2")

    # This is what _ejecutar_inferencia's `finally` does the instant submit
    # returns. Today it fired ui_callback("idle") -> ObsRuntime AvatarState.IDLE
    # + CTK input re-enable, mid-sentence.
    motor._complete_processing_cycle(process_queue=False)
    assert "idle" not in rec.names(), "idle fired while speech was still pending"

    mixer.release()
    assert rec.wait_for("speaking_end", 2), rec.names()
    assert rec.wait_for("idle", 1), rec.names()

    names = rec.names()
    assert names.count("idle") == 1
    assert names.index("idle") > len(names) - 1 - names[::-1].index("speaking_end")


def test_idle_still_fires_on_a_non_speech_cycle(router_motors):
    """The other half of B3's net-behavior claim: a cycle that never speaks
    must keep emitting idle exactly as today."""
    motor, rec, _ = _armed(router_motors)

    motor._complete_processing_cycle(process_queue=False)

    assert rec.names().count("idle") == 1


# ──────────────────────────────────────────────────────────────────────────
# (5) B4 — control commands never drain into a live utterance
# ──────────────────────────────────────────────────────────────────────────


def test_control_commands_do_not_drain_while_speech_is_active(router_motors):
    mixer = _ScriptedMixerMusic(block_at=0)
    motor, rec, _ = _armed(router_motors, mixer_music=mixer)
    applied: list = []
    motor._dispatch_command = lambda tipo, payload, **kw: applied.append(tipo)

    motor._speak_or_submit(_text(2), source="kira-agenda:t1")
    assert mixer.entered_block.wait(5.0)
    motor.command_queue.put(("set_tts_speed", 1.4))

    motor._drain_control_commands()
    assert applied == [], "a TTS-mutating command drained mid-utterance"

    mixer.release()
    assert rec.wait_for("speaking_end", 1), rec.names()
    motor._drain_control_commands()
    assert applied == ["set_tts_speed"]


def test_a_command_consumed_by_the_run_loop_mid_utterance_is_deferred(
    router_motors, monkeypatch
):
    """§11 B4, PRIMARY path (2026-08-05 closure finding): the boundary drain
    is gated, but with the router armed the engine loop is FREE during
    playback — run() pops a control verb within its 1 s tick and
    `_dispatch_command` applies it MID-UTTERANCE. `set_tts_speed` mutates the
    very Piper object the producer is synthesizing on (pre-router this was
    impossible on the main paths: `_hablar` occupied the engine thread).
    Dispatch must DEFER drain-safe verbs while `_speech_active`, and the
    boundary drain must apply them — never lose them."""
    mixer = _ScriptedMixerMusic(block_at=0)
    motor, rec, _ = _armed(router_motors, mixer_music=mixer)
    applied: list = []
    monkeypatch.setattr(
        motor._piper, "set_length_scale", lambda v: applied.append(v)
    )
    monkeypatch.setattr(llm_engine, "save_tts_speed", lambda *_a, **_k: None)

    consumed = threading.Event()
    real_dispatch = motor._dispatch_command

    def _spy(tipo, payload, **kw):
        real_dispatch(tipo, payload, **kw)
        if tipo == "set_tts_speed":
            consumed.set()

    motor._dispatch_command = _spy

    stop = threading.Event()
    pump = _pump(motor, stop)
    try:
        motor._speak_or_submit(_text(2), source="kira-agenda:t1")
        assert mixer.entered_block.wait(5.0)

        motor.command_queue.put(("set_tts_speed", 1.4))
        assert consumed.wait(5.0), "run() never consumed the command"
        assert applied == [], "a TTS-mutating verb was applied mid-utterance"

        mixer.release()
        assert rec.wait_for("speaking_end", 1), rec.names()
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not applied:
            time.sleep(0.005)
        assert applied == [1.4], "the deferred command was lost at the boundary"
    finally:
        stop.set()
        pump.join(5.0)


# ──────────────────────────────────────────────────────────────────────────
# (6) B5 — the router wakes the engine loop at a job boundary
# ──────────────────────────────────────────────────────────────────────────


def _pump(motor, stop: threading.Event) -> threading.Thread:
    """run()'s command loop WITHOUT its idle branch: a `queue.Empty` here does
    nothing at all. Any progress this harness makes therefore proves the
    router's wake sentinel arrived -- never the 1 s idle tick (§11 B5)."""

    def _loop():
        while not stop.is_set():
            try:
                comando = motor.command_queue.get(timeout=0.05)
            except queue.Empty:
                continue
            if comando is None:
                return
            motor._consume_command(comando)

    t = threading.Thread(target=_loop, daemon=True)
    t.start()
    return t


def test_router_wakes_the_engine_loop_so_the_next_item_pops_promptly(router_motors):
    mixer = _ScriptedMixerMusic(block_at=0)
    motor, rec, _ = _armed(router_motors, mixer_music=mixer)
    motor.is_ready = True
    popped = threading.Event()
    seen: list = []

    def _fake_inferencia(payload, source="direct", **kw):
        seen.append((payload, source))
        popped.set()

    motor._ejecutar_inferencia = _fake_inferencia

    stop = threading.Event()
    pump = _pump(motor, stop)
    try:
        motor._speak_or_submit(_text(2), source="kira-agenda:t1")
        assert mixer.entered_block.wait(5.0)
        motor.enqueue("siguiente bloque", priority=2, source="kira-agenda:t2")
        assert not popped.is_set(), "the queue popped while speech was active"

        mixer.release()
        assert popped.wait(1.0), "no wake at the speech boundary — the turn ate a full idle tick"
        assert seen == [("siguiente bloque", "kira-agenda:t2")]
    finally:
        stop.set()
        pump.join(5.0)


def test_wake_sentinel_is_enqueued_after_speaking_end_not_before(router_motors):
    """WU2 ordering (engine_host.py:674-679): `on_agenda_speaking_end` consumes
    the ready draft on the event, and that enqueue must land BEFORE the engine's
    post-boundary pop. The router therefore emits the boundary event first and
    enqueues the wake sentinel only afterwards."""
    motor, rec, _ = _armed(router_motors)
    motor.is_ready = True
    order: list = []
    popped = threading.Event()

    def _consume():
        # engine_host's on_agenda_speaking_end analog.
        order.append("consume")
        motor.enqueue("bloque adoptado", priority=2, source="kira-agenda:t2")

    rec.hooks["speaking_end"] = _consume

    def _fake_inferencia(payload, source="direct", **kw):
        order.append("pop")
        popped.set()

    motor._ejecutar_inferencia = _fake_inferencia

    stop = threading.Event()
    pump = _pump(motor, stop)
    try:
        motor._speak_or_submit(_text(2), source="kira-agenda:t1")
        assert popped.wait(5.0), "the adopted block never popped"
        assert order == ["consume", "pop"]
    finally:
        rec.hooks.pop("speaking_end", None)
        stop.set()
        pump.join(5.0)


# ──────────────────────────────────────────────────────────────────────────
# (7) B6 — a re-entrant submit from inside a boundary consumer is lock-safe
# ──────────────────────────────────────────────────────────────────────────


def test_submit_from_inside_a_speaking_end_consumer_does_not_deadlock(router_motors):
    """CTK's on_motor_speaking_end re-enters play_prefetched_agenda -> submit
    from INSIDE the event. The router runs that consumer on its own thread, so
    holding `_sched_lock` across the emission would self-deadlock a plain Lock."""
    motor, rec, _ = _armed(router_motors)
    again = {"done": False}

    def _resubmit():
        if again["done"]:
            return
        again["done"] = True
        motor._speak_or_submit(_text(1), source="kira-agenda:t2")

    rec.hooks["speaking_end"] = _resubmit
    motor._speak_or_submit(_text(2), source="kira-agenda:t1")

    assert rec.wait_for("speaking_end", 2, timeout=10.0), rec.names()
    assert rec.started_sources() == ["kira-agenda:t1", "kira-agenda:t2"]


# ──────────────────────────────────────────────────────────────────────────
# (8) the chat spoken clock only advances on a DELIVERED, finished job
# ──────────────────────────────────────────────────────────────────────────


def test_chat_spoken_clock_advances_when_the_job_finishes(router_motors):
    motor, rec, _ = _armed(router_motors)
    ticks: list = []
    motor.on_chat_turn_spoken = lambda: ticks.append(1)

    motor._speak_or_submit(_text(2), source="chat")
    assert rec.wait_for("speaking_end", 1), rec.names()

    assert ticks == [1]


def test_chat_spoken_clock_advances_on_an_interrupted_job(router_motors):
    """LEGACY parity: pre-router, the clock advanced whenever `_hablar`
    RETURNED without raising -- and an interruption returns normally, having
    (partially) spoken. Reachable in production: the agenda emergency stop
    calls `interrupt_speaking()` unconditionally, chat turn or not."""
    mixer = _ScriptedMixerMusic(block_at=1)
    motor, rec, _ = _armed(router_motors, mixer_music=mixer)
    ticks: list = []
    motor.on_chat_turn_spoken = lambda: ticks.append(1)

    motor._speak_or_submit(_text(4), source="chat")
    assert mixer.entered_block.wait(5.0)
    motor.interrupt_speaking()

    assert rec.wait_for("speaking_end", 1), rec.names()
    assert ticks == [1]


def test_chat_spoken_clock_does_not_advance_on_a_failed_job(router_motors):
    """A TTS failure (a RAISE, legacy's only exemption) must NOT advance the
    spoken clock -- the growing gap is the very signal that surfaces silent
    TTS failures to the operator."""
    motor, rec, _ = _armed(router_motors)
    ticks: list = []
    motor.on_chat_turn_spoken = lambda: ticks.append(1)

    def _always_raises(texto, source="direct", **kw):
        raise RuntimeError("dead mixer")

    motor._hablar = _always_raises
    motor._speak_or_submit(_text(2), source="chat")

    assert rec.wait_for("speaking_end", 1), rec.names()
    assert ticks == []


def test_chat_spoken_clock_does_not_advance_on_a_nonraising_error_return(router_motors):
    """DELIBERATE divergence from legacy (closure residual, 2026-08-05):
    `_hablar_impl` can fail WITHOUT raising — `queue_empty_timeout` returns a
    normal outcome with `error` set (§12: 195 s is per-socket-op, so this is
    reachable with zero exceptions). Legacy ticked the clock on any
    non-raising return; the router treats this as what it is — a silent TTS
    failure — and lets the gap grow. Pinned as chosen behavior, not accident.
    A non-raising error return must not retry either (retry is raise-only)."""
    motor, rec, _ = _armed(router_motors)
    ticks: list = []
    motor.on_chat_turn_spoken = lambda: ticks.append(1)
    calls: list = []

    def _returns_error_outcome(texto, source="direct", **kw):
        calls.append(source)
        return llm_engine.SpeechOutcome(
            chunks=["frag-a", "frag-b"], cursor=0, spoken=[], skipped=[],
            interrupted=False, error="queue_empty_timeout",
        )

    motor._hablar = _returns_error_outcome
    motor._speak_or_submit(_text(2), source="chat")

    assert rec.wait_for("speaking_end", 1), rec.names()
    assert ticks == []
    assert calls == ["chat"]


def test_agenda_source_never_advances_the_chat_clock(router_motors):
    motor, rec, _ = _armed(router_motors)
    ticks: list = []
    motor.on_chat_turn_spoken = lambda: ticks.append(1)

    motor._speak_or_submit(_text(2), source="kira-agenda:t1")
    assert rec.wait_for("speaking_end", 1), rec.names()

    assert ticks == []


# ──────────────────────────────────────────────────────────────────────────
# (9) reconcile — discard reasons, and retry-once on a whole-invocation raise
# ──────────────────────────────────────────────────────────────────────────


def test_bare_interruption_discards_the_tail_and_nothing_resumes(router_motors, caplog):
    caplog.set_level(logging.INFO, logger="OpenCohost")
    mixer = _ScriptedMixerMusic(block_at=1)
    motor, rec, _ = _armed(router_motors, mixer_music=mixer)

    motor._speak_or_submit(_text(5), source="kira-agenda:t1")
    assert mixer.entered_block.wait(5.0)
    motor.interrupt_speaking()
    assert rec.wait_for("speaking_end", 1), rec.names()

    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("[SPEECH_DISCARD]")]
    assert len(lines) == 1, lines
    assert "reason=interrupted" in lines[0]
    assert "Fragmento" not in lines[0], "PRIVACY: no fragment text in telemetry"
    # Stop means stop: no second activation of the same job.
    assert rec.started_sources() == ["kira-agenda:t1"]


def test_cancelled_source_is_discarded_without_a_boundary_pair(router_motors, caplog):
    caplog.set_level(logging.INFO, logger="OpenCohost")
    motor, rec, _ = _armed(router_motors)
    motor.cancel_speech_for_sources(("kira-agenda",))

    motor._speak_or_submit(_text(2), source="kira-agenda:t1")
    motor._speak_or_submit(_text(2), source="direct")
    assert rec.wait_for("speaking_end", 1), rec.names()

    assert rec.started_sources() == ["direct"], "an emergency-stopped straggler spoke"
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("[SPEECH_DISCARD]")]
    assert len(lines) == 1 and "reason=cancelled" in lines[0], lines


def test_whole_invocation_exception_retries_once_then_discards(router_motors, caplog):
    caplog.set_level(logging.INFO, logger="OpenCohost")
    motor, rec, _ = _armed(router_motors)
    real_hablar = motor._hablar
    attempts: list = []

    def _flaky(texto, source="direct", **kw):
        attempts.append(source)
        if len(attempts) == 1:
            raise RuntimeError("transient mixer failure")
        return real_hablar(texto, source=source, **kw)

    motor._hablar = _flaky
    motor._speak_or_submit(_text(2), source="kira-agenda:t1")

    assert rec.wait_for("speaking_end", 1), rec.names()
    assert len(attempts) == 2, "a transient failure must be retried exactly once"
    # One retry, still ONE boundary pair for the job.
    assert rec.boundary_sources() == [
        ("speaking_start", "kira-agenda:t1"),
        ("speaking_end", None),
    ]


def test_second_exception_discards_the_tail_with_speech_lost(router_motors, caplog):
    caplog.set_level(logging.INFO, logger="OpenCohost")
    motor, rec, _ = _armed(router_motors)

    def _always_raises(texto, source="direct", **kw):
        raise RuntimeError("dead mixer")

    motor._hablar = _always_raises
    motor._speak_or_submit(_text(2), source="kira-agenda:t1")

    assert rec.wait_for("speaking_end", 1), rec.names()
    lost = [r.getMessage() for r in caplog.records if r.getMessage().startswith("[SPEECH_LOST]")]
    assert len(lost) == 1, lost
    assert "job=" in lost[0] and "reason=error" in lost[0]
    assert "Fragmento" not in lost[0], "PRIVACY: no fragment text in telemetry"


# ──────────────────────────────────────────────────────────────────────────
# (10) the cloud-fallback notice carries an EXPLICIT priority
# ──────────────────────────────────────────────────────────────────────────


def test_cloud_fallback_notice_submits_with_an_explicit_priority(router_motors):
    """Its source is the FAILED TURN's (`direct`/`ptt`). Inheriting priority 0
    from that source would make a system notice preemptive under D3-uniform at
    step 3 -- so the call site names priority 1 itself."""
    motor, _, _ = _armed(router_motors)
    calls: list = []
    motor._speak_or_submit = lambda dialogo, source="direct", priority=None: calls.append(
        (source, priority)
    )
    motor._prepare_model = lambda model: True
    motor._start_cloud_prober = lambda *a, **kw: None
    motor.ui_callback = lambda event: None

    motor._handle_cloud_failure("ptt")

    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and not calls:
        time.sleep(0.005)
    assert calls == [("ptt", 1)]


# ──────────────────────────────────────────────────────────────────────────
# (11) the host kill-switch reverts to the legacy blocking path
# ──────────────────────────────────────────────────────────────────────────


def test_kill_switch_off_speaks_on_the_calling_thread_with_no_router(router_motors):
    mixer = _ScriptedMixerMusic()
    motor, rec, _ = _armed(router_motors, mixer_music=mixer, enabled=False)

    motor._speak_or_submit(_text(3), source="direct")

    # Blocking: the whole utterance is already delivered when the call returns.
    assert rec.boundary_sources() == [
        ("speaking_start", "direct"),
        ("speaking_end", None),
    ]
    assert mixer.load_count == 3
    assert getattr(motor, "_speech_router", None) is None, "no router thread on the legacy path"


def test_kill_switch_off_still_honours_the_cancel_token(router_motors):
    motor, rec, _ = _armed(router_motors, enabled=False)
    motor.cancel_speech_for_sources(("kira-agenda",))

    motor._speak_or_submit(_text(2), source="kira-agenda:t1")

    assert rec.boundary_sources() == []
    assert motor._speaking is False


# ──────────────────────────────────────────────────────────────────────────
# (13) review closure — the confirmed step-2 findings stay closed
# ──────────────────────────────────────────────────────────────────────────


def test_two_concurrent_legacy_callers_never_nest_boundary_pairs(router_motors):
    """Review closure (BLOCKER): the boundary pair must live INSIDE the
    `_hablar_lock` critical section on the legacy path. Emitting `start`
    before the acquire let a second caller (CTK's prefetched-agenda speaker,
    CloudFallbackWarm) open a nested pair — [start,start,end,end] — and
    clobber `_current_speech_source` while the first caller was mid-playback,
    which flips the zone seam's self-gate and completes agenda turns that are
    still speaking."""
    mixer = _GateMixerMusic(block_at=(0,))
    motor, rec, _ = _armed(router_motors, mixer_music=mixer, enabled=False)

    done_a = threading.Event()
    done_b = threading.Event()

    def _caller_a():
        motor._hablar(_text(2), source="kira-agenda:a")
        done_a.set()

    def _caller_b():
        motor._hablar(_text(1), source="ptt")
        done_b.set()

    threading.Thread(target=_caller_a, daemon=True).start()
    assert mixer.entered[0].wait(5.0), "caller A never reached playback"

    threading.Thread(target=_caller_b, daemon=True).start()
    # Stability observation (the deterministic assertion is the pair order
    # below): while A is provably mid-playback, B must have NO visible effect
    # before it holds the belt lock.
    deadline = time.monotonic() + 0.3
    while time.monotonic() < deadline:
        assert motor.current_speech_source == "kira-agenda:a", (
            "caller B clobbered the published source mid-playback"
        )
        time.sleep(0.01)

    mixer.release(0)
    assert done_a.wait(5.0) and done_b.wait(5.0)
    assert [e[0] for e in rec.events if e[0] in ("speaking_start", "speaking_end")] == [
        "speaking_start", "speaking_end", "speaking_start", "speaking_end",
    ], rec.boundary_sources()
    assert rec.started_sources() == ["kira-agenda:a", "ptt"]


def test_concurrent_first_submitters_spawn_exactly_one_router_loop():
    """Review closure (MAJOR): `start()` used to launch the thread OUTSIDE
    `_sched_lock`; a constructed-but-unstarted Thread is not alive, so two
    concurrent first callers both passed the guard and spawned TWO loops —
    two `_hablar` callers, I5 and I11 both dead. 96% reproducible pre-fix."""
    from opencohost.core.speech.router import SpeechRouter

    for _ in range(25):
        baseline = {id(t) for t in threading.enumerate()}
        router = SpeechRouter(None)
        barrier = threading.Barrier(4)

        def _racer():
            barrier.wait(5.0)
            router.start()

        racers = [threading.Thread(target=_racer, daemon=True) for _ in range(4)]
        try:
            for t in racers:
                t.start()
            for t in racers:
                t.join(5.0)
            new_loops = [
                t
                for t in threading.enumerate()
                if id(t) not in baseline and t.name == "SpeechRouter" and t.is_alive()
            ]
            assert len(new_loops) == 1, f"{len(new_loops)} router loops spawned"
        finally:
            router.stop(timeout=5.0)


def test_cancel_landing_inside_the_check_window_is_a_logged_discard(router_motors, caplog):
    """Review closure: a token landing between the router's cancel check and
    `_hablar`'s own check made `_hablar` refuse with an EMPTY outcome, which
    reconcile classified FINISHED — a completed turn that never played, with
    no discard log. It must be DISCARDED reason=cancelled, one line."""
    caplog.set_level(logging.INFO, logger="OpenCohost")
    motor, rec, _ = _armed(router_motors)
    fired = {"done": False}

    def _cancel_in_window():
        if not fired["done"]:
            fired["done"] = True
            motor.cancel_speech_for_sources(("kira-agenda",))

    # speaking_start fires AFTER the router's own check and BEFORE `_hablar`'s.
    rec.hooks["speaking_start"] = _cancel_in_window
    motor._speak_or_submit(_text(2), source="kira-agenda:t1")

    assert rec.wait_for("speaking_end", 1), rec.names()
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("[SPEECH_DISCARD]")]
    assert len(lines) == 1 and "reason=cancelled" in lines[0], lines
    assert rec.started_sources() == ["kira-agenda:t1"]


def test_a_retried_job_keeps_its_place_over_a_same_priority_late_arrival(router_motors):
    """Pins the retry-ordering contract under the (priority, job_id) key: a
    retry keeps its ORIGINAL id, so it precedes any same-band job that arrived
    during its failed attempt (a lower-band arrival still goes first, by
    design)."""
    motor, rec, _ = _armed(router_motors)
    real_hablar = motor._hablar
    attempts: list = []
    b_submitted = threading.Event()
    a_entered = threading.Event()

    def _flaky(texto, source="direct", **kw):
        attempts.append(source)
        if len(attempts) == 1:
            a_entered.set()
            assert b_submitted.wait(5.0)
            raise RuntimeError("transient failure with a competitor queued")
        return real_hablar(texto, source=source, **kw)

    motor._hablar = _flaky
    motor._speak_or_submit(_text(1), source="kira-agenda:t1")
    assert a_entered.wait(5.0)
    motor._speak_or_submit(_text(1), source="kira-agenda:t2")  # same band, later id
    b_submitted.set()

    assert rec.wait_for("speaking_end", 2), rec.names()
    assert attempts == ["kira-agenda:t1", "kira-agenda:t1", "kira-agenda:t2"]


# ──────────────────────────────────────────────────────────────────────────
# (12) SpeechJob shape (design §3 field table)
# ──────────────────────────────────────────────────────────────────────────


def test_speech_job_defaults_match_the_design_field_table():
    job = SpeechJob(job_id=1, text="hola", source="kira-agenda:t1", priority=2)

    assert job.chunks is None          # None until the first activation
    assert job.cursor == 0
    assert job.state is SpeechJobState.QUEUED
    assert job.spoken == [] and job.skipped == []
    assert job.suspensions == 0
    assert job.connector is None
    assert isinstance(job.created_at, float)
    assert not hasattr(job, "parent_job_id"), "REJECTED in design §3"
    # Step 3: SUSPENDED is now reachable (see test_speech_router_stack.py) --
    # this default-field assertion is unaffected either way.
    assert SpeechJobState.SUSPENDED is not None


def test_job_ids_are_monotonic(router_motors):
    motor, rec, _ = _armed(router_motors)
    assert motor._speech_router is None, "the router is built on the FIRST routed submit"

    motor._speak_or_submit(_text(1), source="direct")
    assert rec.wait_for("speaking_end", 1), rec.names()
    motor._speak_or_submit(_text(1), source="direct")
    assert rec.wait_for("speaking_end", 2), rec.names()

    ids = motor._speech_router.completed_job_ids()
    assert ids == sorted(set(ids)) and len(ids) == 2


def test_speech_boundary_command_is_ignored_by_the_control_drain(router_motors):
    """The wake sentinel is not a control verb: it must never be applied by
    `_drain_control_commands`, only consumed by run()'s normal read."""
    assert SPEECH_BOUNDARY_COMMAND not in llm_engine._DRAIN_SAFE_COMMANDS
