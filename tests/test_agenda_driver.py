"""FIX-C: the headless AgendaDriver + its pure helpers.

`AgendaDriver` replicates the CTK tick loop (app_shell.py:1539-1632) for the
FastAPI host so a passive `KiraAgendaController` actually drives Kira.  These
tests use a fake motor (records enqueue/replace_pending, exposes
is_processing/is_speaking flags) and a REAL controller — the controller and
motor need zero changes, so the fake only has to be a faithful stand-in for
the two motor methods the driver calls.
"""

import logging
import threading
from types import SimpleNamespace

from opencohost.api.agenda_driver import (
    AgendaDriver,
    PrefetchState,
    enqueue_agenda_action,
    route_motor_event_to_agenda,
)
from opencohost.smart_aggregator.kira_agenda_controller import (
    AgendaAction,
    AgendaState,
    KiraAgendaController,
    TopicStatus,
)


class FakeMotor:
    """Minimal MotorVocalIA stand-in: records enqueue/replace_pending calls."""

    def __init__(self):
        self.is_processing = False
        self.is_speaking = False
        self.enqueued = []  # dicts: payload/priority/source/history_text
        self.replaced = []  # dicts: payload/priority/source
        self.dropped_prefixes = []
        self.interrupted = 0
        self.cleared_prefetch = 0
        # ── prefetch surface (Fase 1: no-dead-air) ────────────────────────
        # Deterministic stand-in for the engine prefetch primitives. Tests
        # toggle these flags to model the worker lifecycle without threads.
        self.current_speech_source = None
        self.current_processing_source = ""
        self.prefetch_calls = []  # dicts: payload/priority/source
        self.prefetch_started = True  # what prefetch_agenda returns
        self.draft_ready = False  # wait_prefetched_agenda(0) result
        self.pending = False  # prefetch_pending() result
        # WU2: engine pregen-cache model — (payload, source) of the cached draft,
        # or None. Set by prefetch_agenda, consulted by _clear_prefetch_unless_matches
        # so the "route the draft's own payload without clearing it" trap is testable.
        self.cached_draft = None
        # Priorities of queued items. Mirrors the real engine, which answers
        # has_pending_priority_before(p) as any(item_priority < p) — NOT an
        # exact-key lookup — so a wrong-priority-arg regression is catchable.
        self.queued_priorities = []
        self.wait_timeouts = []  # every timeout passed to wait_prefetched_agenda
        # T1(c) [v5]: records every motor._log(...) call — the surface the
        # driver's own "Pregen boundary: draft=none" line must reach.
        self.logged = []  # (msg, level) tuples
        # WU5 (design-fase2.md §3 WU5): frozen interruption-return surface. The
        # driver queries these to mirror the engine's frozen stash and drive the
        # return. Defaults model "no interruption pending" so every pre-WU5 test
        # sees unchanged behavior.
        self.has_frozen = False
        self.frozen_key = None       # (payload, source, priority) returned by restore
        self.restored_tema = None    # captures the {tema} the driver passes at restore
        self.discarded = False
        self.detour_over = False     # detour_exceeded() result
        # F1: detour_started() — True once the interruption answer turn has
        # committed since the cut. Defaults False (honest post-cut state: the
        # answer rides the command queue and has not spoken yet), so the return
        # HOLDS until a test flips it True.
        self.detour_has_started = False
        # R1: frozen_stash_age_seconds() — seconds since the stash was frozen, or
        # None when no stash is pending. Defaults None (no clock) so the deadline
        # backstop never fires unless a test sets a concrete age.
        self.frozen_age = None

    def has_frozen_stash(self):
        return self.has_frozen

    def frozen_stash_age_seconds(self):
        return self.frozen_age

    def detour_started(self):
        return self.detour_has_started

    def restore_frozen_stash(self, tema=""):
        self.restored_tema = tema
        self.has_frozen = False
        key = self.frozen_key
        if key is not None:
            # Mirror the real engine: the restored draft is back in the active
            # slot, so the match-aware clear keeps it when routed through the queue.
            self.cached_draft = (key[0], key[1])
        return key

    def discard_frozen_stash(self):
        self.discarded = True
        self.has_frozen = False

    def detour_exceeded(self):
        return self.detour_over

    def _log(self, msg, level="info"):
        self.logged.append((msg, level))

    def enqueue(self, payload, priority=1, source="chat", history_text=None):
        self.enqueued.append(
            {"payload": payload, "priority": priority, "source": source, "history_text": history_text}
        )

    def replace_pending(self, payload, priority=1, source="chat"):
        self.replaced.append({"payload": payload, "priority": priority, "source": source})

    def clear_prefetched_agenda(self):
        self.cleared_prefetch += 1
        self.draft_ready = False
        self.pending = False
        self.cached_draft = None

    def clear_prefetched_agenda_only(self):
        # WU4 F1: mirror the real engine's source-aware clear — only touches
        # an AGENDA-sourced occupant; an interactive occupant survives.
        if self.cached_draft is not None and str(self.cached_draft[1]).startswith("kira-agenda"):
            self.clear_prefetched_agenda()

    def _clear_prefetch_unless_matches(self, payload, source):
        # WU2 match-aware clear: keep the cache when the incoming (payload, source)
        # IS the cached draft being routed through the queue; clear on any mismatch.
        if self.cached_draft == (payload, source):
            return
        self.clear_prefetched_agenda()

    def prefetch_agenda(self, payload, priority=2, source="kira-agenda"):
        self.prefetch_calls.append({"payload": payload, "priority": priority, "source": source})
        if self.prefetch_started:
            self.cached_draft = (payload, source)
        return self.prefetch_started

    def wait_prefetched_agenda(self, timeout=0.0):
        self.wait_timeouts.append(timeout)
        return self.draft_ready

    def prefetch_pending(self):
        return self.pending

    def play_prefetched_agenda(self):
        # WU2: the API-host consume path routes the draft through the queue+worker
        # (design-fase2.md §2.1 [v3]); it must NEVER call the legacy parallel
        # speaker. A call here is a regression — fail loudly.
        raise AssertionError(
            "play_prefetched_agenda must not be called on the API-host consume "
            "path after WU2 (the draft is routed through the queue instead)"
        )

    def has_pending_priority_before(self, priority):
        return any(p < priority for p in self.queued_priorities)

    def drop_pending_sources(self, prefixes):
        self.dropped_prefixes.append(prefixes)
        return 0

    def interrupt_speaking(self):
        self.interrupted += 1

    def reset(self):
        self.enqueued.clear()
        self.replaced.clear()
        # Establish a clean baseline for prefetch observation counters so tests
        # measure only the action under test, not opening-tick bookkeeping.
        self.cleared_prefetch = 0
        self.prefetch_calls.clear()
        self.wait_timeouts.clear()
        self.logged.clear()


class RejectingFakeMotor(FakeMotor):
    """FakeMotor that reproduces the real engine reject contract.

    The engine validates every dequeued agenda generation through the
    controller guardrail callback (``llm_engine.py:1573`` ->
    ``accept_output(dialogo)``) and then ALWAYS fires a trailing
    ``accept_output("")`` (``llm_engine.py:2461``).  A mode-collapse turn is
    rejected on the first call; the trailing empty is rejected on the second.
    The stock ``FakeMotor`` only ever drives ACCEPTED outputs, so the reject
    path / double register_failure / None-loop are untested.  This stand-in
    replays that exact double-signal synchronously the moment an agenda turn is
    enqueued, driving the controller state machine down the reject path.
    """

    #: Pre-seeded into ``controller.last_outputs`` so ``is_repetition`` rejects
    #: it deterministically — no LLM, no randomness.
    DUP_LINE = "kira comenta el tema de prueba"

    def __init__(self, controller):
        super().__init__()
        self._controller = controller

    def _reject_cycle(self, source):
        if not source.startswith("kira-agenda"):
            return
        # Mirror llm_engine.py:1573 (reject the generated dup) then :2461
        # (trailing empty re-signal) for one rejected agenda turn.
        self._controller.accept_output(self.DUP_LINE)
        self._controller.accept_output("")

    def enqueue(self, payload, priority=1, source="chat", history_text=None):
        super().enqueue(payload, priority=priority, source=source, history_text=history_text)
        self._reject_cycle(source)

    def replace_pending(self, payload, priority=1, source="chat"):
        super().replace_pending(payload, priority=priority, source=source)
        self._reject_cycle(source)


def _driver(controller, motor, tick_seconds=4.5):
    return AgendaDriver(
        get_agenda=lambda: controller,
        get_motor=lambda: motor,
        agenda_lock=threading.Lock(),
        tick_seconds=tick_seconds,
    )


def _controller_with_queued_topic(**kwargs):
    controller = KiraAgendaController(**kwargs)
    topic = controller.add_topic("Tema uno", "angulo", approved=True)
    controller.queue_topic(topic.id)
    return controller, topic


def _speak_cycle(controller):
    route_motor_event_to_agenda(controller, "speaking_start")
    route_motor_event_to_agenda(controller, "speaking_end")


# ── driver tick: opens a queued topic ─────────────────────────────────────


def test_driver_enqueues_kira_agenda_source_within_one_tick():
    controller, _ = _controller_with_queued_topic()
    controller.enable()
    motor = FakeMotor()
    driver = _driver(controller, motor)

    driver.tick_once()

    # First tick opens the topic and replace_pending's a kira-agenda turn.
    assert controller.state == AgendaState.GENERATING
    assert controller.active_topic is not None
    assert motor.replaced, "expected a replace_pending for the agenda turn"
    assert motor.replaced[-1]["source"] == "kira-agenda"


def test_driver_skips_while_motor_busy():
    controller, _ = _controller_with_queued_topic()
    controller.enable()
    motor = FakeMotor()
    motor.is_processing = True
    driver = _driver(controller, motor)

    driver.tick_once()

    # next_action returns none() while the motor is busy — no turn enqueued.
    assert motor.enqueued == []
    assert motor.replaced == []


# ── motor-event routing feedback ──────────────────────────────────────────


def test_speaking_routing_advances_states_and_increments_turns():
    controller, topic = _controller_with_queued_topic(max_turns_per_topic=3, turn_batch_size=1)
    controller.enable()
    motor = FakeMotor()
    driver = _driver(controller, motor)

    driver.tick_once()
    assert controller.state == AgendaState.GENERATING

    route_motor_event_to_agenda(controller, "speaking_start")
    assert controller.state == AgendaState.SPEAKING

    route_motor_event_to_agenda(controller, "speaking_end")
    assert controller.state == AgendaState.WAITING_SIGNAL
    assert topic.turns_spoken == 1


def test_speaking_end_nudge_fires_only_when_speech_completed():
    controller, _ = _controller_with_queued_topic()
    controller.enable()
    motor = FakeMotor()
    driver = _driver(controller, motor)
    driver.tick_once()  # -> GENERATING
    route_motor_event_to_agenda(controller, "speaking_start")  # -> SPEAKING

    calls = []
    route_motor_event_to_agenda(controller, "speaking_end", on_speech_complete=lambda: calls.append(1))
    assert calls == [1]

    # A stray speaking_end when the controller is not agenda-speaking must NOT
    # fire the nudge (guarded by the controller-state check).
    calls.clear()
    route_motor_event_to_agenda(controller, "speaking_end", on_speech_complete=lambda: calls.append(1))
    assert calls == []


def test_topic_completes_after_max_turns():
    controller, topic = _controller_with_queued_topic(max_turns_per_topic=2, turn_batch_size=1)
    controller.enable()
    motor = FakeMotor()
    driver = _driver(controller, motor)

    for _ in range(12):
        driver.tick_once()
        _speak_cycle(controller)
        if topic.status == TopicStatus.COMPLETED:
            break

    assert topic.status == TopicStatus.COMPLETED
    assert topic.turns_spoken >= 2


# ── auto-exit ─────────────────────────────────────────────────────────────


def test_auto_exit_to_off_on_empty_queue():
    controller = KiraAgendaController()
    controller.enable()  # OFF -> IDLE
    assert controller.state == AgendaState.IDLE
    motor = FakeMotor()
    driver = _driver(controller, motor)

    driver.tick_once()

    assert controller.state == AgendaState.OFF
    assert motor.enqueued == []
    assert motor.replaced == []


# ── soft_stop ─────────────────────────────────────────────────────────────


def test_soft_stop_enqueues_single_closing_action_then_off():
    controller, topic = _controller_with_queued_topic(max_turns_per_topic=3, turn_batch_size=1)
    controller.enable()
    motor = FakeMotor()
    driver = _driver(controller, motor)
    driver.tick_once()  # open topic
    _speak_cycle(controller)  # -> WAITING_SIGNAL, active topic mid-way
    motor.reset()

    action = controller.soft_stop()
    enqueue_agenda_action(motor, action)

    # Exactly one closing action, via replace_pending (source kira-agenda-stop).
    assert len(motor.replaced) == 1
    assert motor.replaced[0]["source"] == "kira-agenda-stop"
    assert motor.enqueued == []

    _speak_cycle(controller)  # deliver the closing line
    assert controller.state == AgendaState.OFF
    assert topic.status == TopicStatus.COMPLETED


# ── emergency_stop ────────────────────────────────────────────────────────


def test_emergency_stop_makes_driver_tick_inert():
    controller, _ = _controller_with_queued_topic()
    controller.enable()
    motor = FakeMotor()
    driver = _driver(controller, motor)
    driver.tick_once()  # open topic

    controller.emergency_stop()  # state OFF, active_topic cleared
    motor.reset()
    driver.tick_once()  # OFF -> inert

    assert controller.state == AgendaState.OFF
    assert motor.enqueued == []
    assert motor.replaced == []


# ── PAUSED auto-resume ────────────────────────────────────────────────────


def test_paused_auto_resume_advances_via_driver():
    controller, _ = _controller_with_queued_topic()
    controller.enable()
    controller.state = AgendaState.PAUSED_NEEDS_OPERATOR
    # Drive the recovery policy into "retry allowed now": 5 failures reach the
    # PAUSED threshold and a zeroed last-failure time bypasses the cooldown.
    controller.recovery._failures = 5
    controller.recovery._retry_attempt = 0
    controller.recovery._last_failure_time = 0.0
    motor = FakeMotor()
    driver = _driver(controller, motor)

    driver.tick_once()

    assert controller.state != AgendaState.PAUSED_NEEDS_OPERATOR
    assert motor.replaced, "auto-resume should fall through to next_action"
    assert motor.replaced[-1]["source"] == "kira-agenda"


def test_paused_stays_when_cooldown_not_ready():
    controller, _ = _controller_with_queued_topic()
    controller.enable()
    controller.state = AgendaState.PAUSED_NEEDS_OPERATOR
    controller.recovery._failures = 5
    controller.recovery._retry_attempt = 0
    controller.recovery._last_failure_time = 10**12  # far future -> cooldown not elapsed
    motor = FakeMotor()
    driver = _driver(controller, motor)

    driver.tick_once()

    assert controller.state == AgendaState.PAUSED_NEEDS_OPERATOR
    assert motor.replaced == []
    assert motor.enqueued == []


def test_paused_auto_resume_reachable_with_frozen_stash_pending():
    """R2: a pending frozen interruption-return must not make the
    PAUSED_NEEDS_OPERATOR auto-resume branch unreachable. With a stash mirrored and
    the engine reporting a frozen stash, the tick still reaches can_auto_resume, and
    the stash survives recovery (never discarded). Without R2 the return would HOLD
    every tick and the agenda could never auto-recover after a PTT cut."""
    controller, topic = _controller_with_queued_topic()
    controller.enable()
    motor = FakeMotor()
    driver = _driver(controller, motor)
    driver.tick_once()  # open the topic -> active_topic set
    assert controller.active_topic is not None
    motor.reset()

    # A prior PTT cut froze the next-turn draft; the driver mirrors it. A health
    # pause (guardrail failures) then lands with the return still pending.
    driver._prefetch = PrefetchState(
        action=SimpleNamespace(
            kind="enqueue", prompt="AG2", source="kira-agenda", priority=2, history_text=None
        ),
        topic_id=topic.id,
    )
    motor.has_frozen = True
    motor.detour_has_started = False
    motor.frozen_age = None  # no deadline pressure from the R1 backstop
    controller.state = AgendaState.PAUSED_NEEDS_OPERATOR
    controller.recovery._failures = 5
    controller.recovery._retry_attempt = 0
    controller.recovery._last_failure_time = 0.0

    driver.tick_once()

    assert controller.state != AgendaState.PAUSED_NEEDS_OPERATOR, (
        "auto-resume ran — unreachable before R2 (the frozen return held every tick)"
    )
    assert motor.has_frozen is True, "the frozen stash survives recovery (not discarded)"
    assert motor.discarded is False


# ── thread lifecycle ──────────────────────────────────────────────────────


def test_shutdown_joins_the_thread():
    controller = KiraAgendaController()
    motor = FakeMotor()
    driver = _driver(controller, motor, tick_seconds=0.05)

    driver.start()
    assert driver.is_running()

    driver.stop(timeout=2.0)
    assert not driver.is_running()


def test_nudge_triggers_a_tick_on_the_running_thread():
    controller, _ = _controller_with_queued_topic()
    controller.enable()
    motor = FakeMotor()
    # Long cadence so only the nudge can produce a tick within the test window.
    driver = _driver(controller, motor, tick_seconds=30.0)
    driver.start()
    try:
        driver.nudge()
        deadline = threading.Event()
        # Poll for the enqueue produced by the nudged tick.
        for _ in range(200):
            if motor.replaced:
                break
            deadline.wait(0.01)
        assert motor.replaced, "nudge should have produced an immediate tick"
        assert motor.replaced[-1]["source"] == "kira-agenda"
    finally:
        driver.stop(timeout=2.0)


# ── enqueue helper units ──────────────────────────────────────────────────


def test_enqueue_helper_ignores_none_action():
    motor = FakeMotor()
    enqueue_agenda_action(motor, AgendaAction.none())
    assert motor.enqueued == []
    assert motor.replaced == []


def test_enqueue_helper_uses_enqueue_for_non_agenda_source():
    motor = FakeMotor()
    action = AgendaAction(
        kind="enqueue",
        prompt="hola",
        source="ptt",
        priority=0,
        history_text="El streamer dijo (PTT): x",
    )
    enqueue_agenda_action(motor, action)
    assert motor.replaced == []
    assert motor.enqueued[0]["source"] == "ptt"
    assert motor.enqueued[0]["history_text"] == "El streamer dijo (PTT): x"


def test_enqueue_helper_uses_replace_pending_for_agenda_source():
    motor = FakeMotor()
    action = AgendaAction(kind="enqueue", prompt="hola", source="kira-agenda", priority=2)
    enqueue_agenda_action(motor, action)
    assert motor.enqueued == []
    assert motor.replaced[0]["source"] == "kira-agenda"


# ── inert states ──────────────────────────────────────────────────────────


def test_tick_noop_when_agenda_none():
    motor = FakeMotor()
    driver = AgendaDriver(
        get_agenda=lambda: None,
        get_motor=lambda: motor,
        agenda_lock=threading.Lock(),
    )
    driver.tick_once()  # must not raise
    assert motor.enqueued == []
    assert motor.replaced == []


def test_tick_noop_when_state_off():
    controller, _ = _controller_with_queued_topic()  # queued topic, but state OFF
    motor = FakeMotor()
    driver = _driver(controller, motor)

    driver.tick_once()

    assert controller.state == AgendaState.OFF
    assert motor.enqueued == []
    assert motor.replaced == []


# ── reject path: the None-loop after a rejected closing turn (WU1) ─────────


def _rejecting_setup(num_topics=1, **kwargs):
    """Enabled controller + RejectingFakeMotor + driver, with the dup seeded.

    Every kira-agenda* turn the driver enqueues is rejected twice (dup + trailing
    empty), reproducing the runtime None-loop trigger.  tick1 forces the open
    topic to its turn cap (WAITING_SIGNAL); tick2 rejects the closing line and
    hits the >=3 force-complete gate — the trailing empty then runs with the
    topic already gone (the bug point).
    """
    controller = KiraAgendaController(**kwargs)
    topics = []
    for i in range(num_topics):
        topic = controller.add_topic(f"Tema {i + 1}", "angulo", approved=True)
        controller.queue_topic(topic.id)
        topics.append(topic)
    controller.enable()
    controller.record_accepted_output(RejectingFakeMotor.DUP_LINE)
    motor = RejectingFakeMotor(controller)
    driver = _driver(controller, motor)
    return controller, motor, driver, topics


def test_rejected_closing_turn_advances_to_next_topic():
    controller, motor, driver, topics = _rejecting_setup(
        num_topics=2, max_turns_per_topic=2, turn_batch_size=1
    )
    t1, t2 = topics

    driver.tick_once()  # tick1: open turn rejected -> turns forced, WAITING_SIGNAL
    driver.tick_once()  # tick2: closing turn rejected -> force-complete + trailing empty

    # The trailing empty signal runs with active_topic already None. On the buggy
    # code it lands in REGENERATING_SAFE and never returns to IDLE; fixed, it
    # settles on IDLE so the queue can advance.
    assert controller.state == AgendaState.IDLE
    assert controller.active_topic is None
    assert t1.status == TopicStatus.COMPLETED

    driver.tick_once()  # tick3: IDLE must select the next queued topic
    assert controller.active_topic is not None
    assert controller.active_topic.id == t2.id


def test_rejected_closing_turn_with_empty_queue_reaches_off():
    controller, motor, driver, _ = _rejecting_setup(
        num_topics=1, max_turns_per_topic=2, turn_batch_size=1
    )

    driver.tick_once()  # open turn rejected
    driver.tick_once()  # closing turn rejected -> force-complete, empty queue
    driver.tick_once()  # IDLE + empty queue -> driver auto-exit to OFF

    assert controller.state == AgendaState.OFF


def test_soft_stop_terminates_after_rejected_closing():
    controller, motor, driver, _ = _rejecting_setup(
        num_topics=1, max_turns_per_topic=2, turn_batch_size=1
    )

    driver.tick_once()  # open turn rejected
    driver.tick_once()  # closing turn rejected -> force-complete settles state

    motor.reset()
    action = controller.soft_stop()

    # From IDLE with no active topic, soft_stop must drop straight to OFF. On the
    # buggy code the controller is stuck in REGENERATING_SAFE and soft_stop
    # returns none() without terminating.
    assert controller.state == AgendaState.OFF
    assert action.kind == "none"

    driver.tick_once()  # OFF is inert -> no further motor traffic
    assert motor.enqueued == []
    assert motor.replaced == []


# ── Fase 1: no-dead-air prefetch wiring ────────────────────────────────────


def _arm_prefetch(controller, driver, motor, source="kira-agenda"):
    """Open a topic, enter SPEAKING, and fire the speaking_start prefetch hook.

    Mirrors what EngineHost does on a real speaking_start: after
    mark_generation_accepted flips the controller to SPEAKING, the driver
    previews N+1 and spawns a background generation.
    """
    driver.tick_once()  # open topic -> GENERATING, replace_pending turn N
    route_motor_event_to_agenda(controller, "speaking_start")  # -> SPEAKING
    motor.current_speech_source = source
    driver.maybe_start_prefetch(controller, motor)


def test_speaking_start_triggers_prefetch_for_agenda_speech():
    controller, _ = _controller_with_queued_topic(max_turns_per_topic=3, turn_batch_size=1)
    controller.enable()
    motor = FakeMotor()
    driver = _driver(controller, motor)

    _arm_prefetch(controller, driver, motor)

    assert motor.prefetch_calls, "speaking_start should spawn a background prefetch"
    assert motor.prefetch_calls[-1]["source"].startswith("kira-agenda")
    assert isinstance(driver._prefetch, PrefetchState)
    assert driver._prefetch.topic_id == controller.active_topic.id


def test_speaking_start_skips_prefetch_for_non_agenda_speech_source():
    controller, _ = _controller_with_queued_topic()
    controller.enable()
    motor = FakeMotor()
    driver = _driver(controller, motor)
    driver.tick_once()
    route_motor_event_to_agenda(controller, "speaking_start")
    motor.current_speech_source = "ptt"  # a human owns the mic (#344)

    driver.maybe_start_prefetch(controller, motor)

    assert motor.prefetch_calls == []
    assert driver._prefetch is None


def test_speaking_start_skips_prefetch_when_interactive_pending():
    controller, _ = _controller_with_queued_topic()
    controller.enable()
    motor = FakeMotor()
    motor.queued_priorities = [1]  # a chat item (priority 1) queued ahead of agenda (#338)
    driver = _driver(controller, motor)
    driver.tick_once()
    route_motor_event_to_agenda(controller, "speaking_start")
    motor.current_speech_source = "kira-agenda"

    driver.maybe_start_prefetch(controller, motor)

    assert motor.prefetch_calls == []
    assert driver._prefetch is None


def test_prefetch_stash_survives_mid_speech_tick():
    # The 4.5s cadence fires MANY ticks inside a 60s speech window. A tick while
    # the turn is still SPEAKING must NOT drop or consume the in-flight draft.
    controller, _ = _controller_with_queued_topic(max_turns_per_topic=3, turn_batch_size=1)
    controller.enable()
    motor = FakeMotor()
    driver = _driver(controller, motor)
    _arm_prefetch(controller, driver, motor)
    motor.reset()
    motor.is_speaking = True  # motor still speaking turn N

    driver.tick_once()

    assert driver._prefetch is not None, "mid-speech tick must not drop the draft"
    assert motor.replaced == [] and motor.enqueued == [], "a mid-speech tick must not consume"
    assert motor.cleared_prefetch == 0


def test_speaking_end_consumes_ready_draft_without_new_generation():
    controller, _ = _controller_with_queued_topic(max_turns_per_topic=3, turn_batch_size=1)
    controller.enable()
    motor = FakeMotor()
    driver = _driver(controller, motor)
    _arm_prefetch(controller, driver, motor)
    draft_payload = driver._prefetch.action.prompt  # the draft being routed
    motor.reset()  # drop the opening replace_pending
    motor.draft_ready = True

    route_motor_event_to_agenda(controller, "speaking_end")  # -> WAITING_SIGNAL
    driver.tick_once()  # consume BEFORE next_action

    # WU2: the draft is ROUTED through the queue (its own payload), not spoken by
    # a parallel thread — one replace_pending carrying the cached draft's payload.
    assert len(motor.replaced) == 1, "the cached draft must be routed through the queue"
    assert motor.replaced[0]["payload"] == draft_payload
    assert motor.replaced[0]["source"].startswith("kira-agenda")
    assert motor.enqueued == [], "no fallback enqueue — the draft carried its own payload"
    assert motor.cleared_prefetch == 0, "routing the draft must NOT clear its own cache (the trap)"
    assert driver._prefetch is None
    assert all(t == 0 for t in motor.wait_timeouts), "consume must never block on the lock"


def test_valid_draft_is_consumed_before_next_action_can_clobber_it():
    controller, _ = _controller_with_queued_topic(max_turns_per_topic=3, turn_batch_size=1)
    controller.enable()
    motor = FakeMotor()
    driver = _driver(controller, motor)
    _arm_prefetch(controller, driver, motor)
    motor.reset()
    motor.draft_ready = True

    route_motor_event_to_agenda(controller, "speaking_end")
    driver.tick_once()

    # Exactly one enqueue — the draft's own routing — so next_action never runs
    # (consume returns True and short-circuits the tick), i.e. it can't clobber it.
    assert len(motor.replaced) == 1 and motor.enqueued == []
    assert motor.cleared_prefetch == 0, "a routed draft is never cleared by its own enqueue"


def test_consume_yields_to_pending_ptt_and_clears_draft():
    controller, _ = _controller_with_queued_topic(max_turns_per_topic=3, turn_batch_size=1)
    controller.enable()
    motor = FakeMotor()
    driver = _driver(controller, motor)
    _arm_prefetch(controller, driver, motor)
    motor.reset()
    motor.draft_ready = True

    route_motor_event_to_agenda(controller, "speaking_end")  # -> WAITING_SIGNAL
    motor.queued_priorities = [0]  # a PTT (priority 0) arrived while the draft waited

    driver.tick_once()

    # Interactive work runs first — the draft is dropped (yielded), never routed.
    # (play_prefetched_agenda raising if ever called is the belt-and-braces guard.)
    assert driver._prefetch is None
    assert motor.cleared_prefetch >= 1


def test_consume_drops_draft_when_topic_vanished():
    # active_topic went away before consume (e.g. emergency clear): the topic-None
    # staleness guard drops the draft and falls back to a normal tick.
    controller, _ = _controller_with_queued_topic(max_turns_per_topic=3, turn_batch_size=1)
    controller.enable()
    motor = FakeMotor()
    driver = _driver(controller, motor)
    _arm_prefetch(controller, driver, motor)
    motor.reset()
    motor.draft_ready = True

    route_motor_event_to_agenda(controller, "speaking_end")
    controller.active_topic = None  # topic vanished before consume

    driver.tick_once()

    assert driver._prefetch is None
    assert motor.cleared_prefetch >= 1


def test_consume_drops_draft_on_topic_id_mismatch():
    # A DIFFERENT (non-None) topic is active at consume time — the draft was
    # pinned to the old topic id, so the id-mismatch guard drops it (this is the
    # branch a topic-None test can't reach).
    controller, topic1 = _controller_with_queued_topic(max_turns_per_topic=3, turn_batch_size=1)
    topic2 = controller.add_topic("Tema dos", "angulo", approved=True)
    controller.queue_topic(topic2.id)
    controller.enable()
    motor = FakeMotor()
    driver = _driver(controller, motor)
    _arm_prefetch(controller, driver, motor)  # opens topic1, stash pinned to topic1.id
    assert driver._prefetch.topic_id == topic1.id
    motor.reset()
    motor.draft_ready = True

    route_motor_event_to_agenda(controller, "speaking_end")  # -> WAITING_SIGNAL
    controller.active_topic = topic2  # a different topic is active now (id mismatch)

    driver.tick_once()

    # A draft pinned to the old topic must not be routed — it is dropped as stale.
    assert driver._prefetch is None
    assert motor.cleared_prefetch >= 1


def test_late_draft_requests_short_retick_without_enqueue():
    controller, _ = _controller_with_queued_topic(max_turns_per_topic=3, turn_batch_size=1)
    controller.enable()
    motor = FakeMotor()
    driver = _driver(controller, motor)
    _arm_prefetch(controller, driver, motor)
    motor.reset()
    motor.draft_ready = False  # not ready at speaking_end
    motor.pending = True  # worker still generating

    route_motor_event_to_agenda(controller, "speaking_end")  # -> WAITING_SIGNAL
    driver.tick_once()

    assert motor.enqueued == [] and motor.replaced == [], "no fallback while draft is in flight"
    assert driver._prefetch is not None, "draft kept for the re-tick"
    assert driver._next_wait == 0.25


def test_failed_prefetch_falls_back_to_plain_generation():
    controller, _ = _controller_with_queued_topic(max_turns_per_topic=3, turn_batch_size=1)
    controller.enable()
    motor = FakeMotor()
    driver = _driver(controller, motor)
    _arm_prefetch(controller, driver, motor)
    motor.reset()
    motor.draft_ready = False
    motor.pending = False  # worker finished with nothing (error/reject)

    route_motor_event_to_agenda(controller, "speaking_end")  # -> WAITING_SIGNAL
    driver.tick_once()

    assert driver._prefetch is None
    assert motor.replaced, "must fall back to a normal generation"


# NOTE (WU2): the old `test_play_false_after_start_restores_waiting_signal` was
# removed. Its driver-side play-False restore branch no longer exists — the
# consume path now enqueues the draft and a pop-time cache miss (raced clear)
# falls back to plain generation on the WORKER (design-fase2.md §2.2, edge
# cases), not on the driver. That worker-side fallback is covered engine-side by
# test_pregen_pop_cache.py::test_pop_time_miss_falls_back_to_generation.


def test_stop_turn_prefetch_full_cycle_no_topic_closing_deadlock():
    # #468: consuming a kira-agenda-stop prefetch drives the controller through
    # TOPIC_CLOSING. The speaking_start guard must include TOPIC_CLOSING so the
    # closing turn completes instead of deadlocking.
    controller, topic = _controller_with_queued_topic(max_turns_per_topic=2, turn_batch_size=1)
    controller.enable()
    motor = FakeMotor()
    driver = _driver(controller, motor)

    def consume():
        motor.draft_ready = True
        route_motor_event_to_agenda(controller, "speaking_end")
        driver.tick_once()

    # Turn 1: continue draft prefetched during its speech, then consumed.
    _arm_prefetch(controller, driver, motor)
    assert motor.prefetch_calls[-1]["source"] == "kira-agenda"
    consume()  # adopt+play continue -> GENERATING (turn 2 queued)

    # Turn 2 is the last: its prefetch must be a STOP turn.
    route_motor_event_to_agenda(controller, "speaking_start")  # GENERATING -> SPEAKING
    motor.current_speech_source = "kira-agenda"
    driver.maybe_start_prefetch(controller, motor)
    assert motor.prefetch_calls[-1]["source"] == "kira-agenda-stop"
    consume()  # adopt stop -> TOPIC_CLOSING, play

    # The closing turn speaks — TOPIC_CLOSING must reach SPEAKING (the #468 fix).
    route_motor_event_to_agenda(controller, "speaking_start")
    assert controller.state == AgendaState.SPEAKING
    motor.current_speech_source = "kira-agenda"
    driver.maybe_start_prefetch(controller, motor)
    assert driver._prefetch is None, "no prefetch after a closing turn (topic CLOSING)"
    route_motor_event_to_agenda(controller, "speaking_end")

    assert topic.status == TopicStatus.COMPLETED
    assert controller.active_topic is None
    assert controller.state in {AgendaState.IDLE, AgendaState.OFF}


def test_enqueue_agenda_action_still_clears_on_new_generation():
    # The other half of the conditional-clear property: a GENUINE new generation
    # (reached only when no valid draft was consumed) supersedes the engine draft
    # and drops the driver stash.
    controller, _ = _controller_with_queued_topic()
    motor = FakeMotor()
    driver = _driver(controller, motor)
    driver._prefetch = PrefetchState(action=object(), topic_id="stale")
    action = AgendaAction(kind="enqueue", prompt="hola", source="kira-agenda", priority=2)

    enqueue_agenda_action(motor, action, driver=driver)

    assert motor.cleared_prefetch == 1, "a new generation clears the engine draft"
    assert driver._prefetch is None, "and drops the driver stash"


def test_prefetch_never_blocks_under_the_lock():
    # The lock is held across the whole tick body, so the driver must only SPAWN
    # (prefetch_agenda) and poll NON-BLOCKING (wait timeout=0); a real ~17s
    # generation must never be awaited under the lock.
    controller, _ = _controller_with_queued_topic(max_turns_per_topic=3, turn_batch_size=1)
    controller.enable()
    motor = FakeMotor()
    driver = _driver(controller, motor)
    _arm_prefetch(controller, driver, motor)
    motor.draft_ready = True

    route_motor_event_to_agenda(controller, "speaking_end")
    driver.tick_once()  # consume

    assert motor.wait_timeouts, "consume polled readiness at least once"
    assert all(t == 0 for t in motor.wait_timeouts), "every readiness poll was non-blocking"


def test_run_honors_next_wait_retick_for_late_draft():
    # Prove the RUNNING loop honors the short _next_wait re-tick (not the full
    # cadence) when a draft is still generating at speaking_end. A 30s cadence
    # would never catch the draft; the 0.25s re-tick must.
    controller, _ = _controller_with_queued_topic(max_turns_per_topic=3, turn_batch_size=1)
    controller.enable()
    motor = FakeMotor()
    driver = _driver(controller, motor, tick_seconds=30.0)
    # Arm deterministically on this thread before the loop starts.
    driver.tick_once()  # open topic -> GENERATING
    route_motor_event_to_agenda(controller, "speaking_start")  # -> SPEAKING
    motor.current_speech_source = "kira-agenda"
    driver.maybe_start_prefetch(controller, motor)
    motor.reset()
    motor.draft_ready = False
    motor.pending = True  # worker still generating at speaking_end
    route_motor_event_to_agenda(controller, "speaking_end")  # -> WAITING_SIGNAL

    driver.start()
    try:
        driver.nudge()  # first tick: late draft -> requests a 0.25s re-tick
        wait = threading.Event()
        wait.wait(0.3)  # let a couple of short re-ticks elapse
        motor.draft_ready = True  # draft lands
        motor.pending = False
        for _ in range(200):  # up to ~2s for a re-tick to consume it
            if motor.replaced:
                break
            wait.wait(0.01)
        assert motor.replaced, "the 0.25s re-tick (not the 30s cadence) consumed the draft"
    finally:
        driver.stop(timeout=2.0)


# ── WU4 F1 (WU3 follow-up): driver clear must be source-aware ──────────────


def test_clear_prefetch_leaves_interactive_draft_but_clears_agenda_draft():
    controller, _ = _controller_with_queued_topic()
    motor = FakeMotor()
    driver = _driver(controller, motor)

    # An interactive (chat) draft occupies the engine slot — a driver-side
    # yield/drop must NOT clobber it (it has nothing to do with the driver).
    motor.cached_draft = ("chat prompt", "chat")

    driver._clear_prefetch(motor)

    assert motor.cached_draft == ("chat prompt", "chat"), "an interactive occupant must survive"
    assert motor.cleared_prefetch == 0

    # An agenda draft occupies the slot -> the driver's own clear DOES apply.
    motor.cached_draft = ("agenda prompt", "kira-agenda")

    driver._clear_prefetch(motor)

    assert motor.cached_draft is None
    assert motor.cleared_prefetch == 1


# ── WU4 4a: boundary telemetry — "none" (driver consume miss, no draft ever) ─


def test_none_boundary_telemetry_on_driver_fallback_with_no_draft(caplog):
    controller, _ = _controller_with_queued_topic(max_turns_per_topic=3, turn_batch_size=1)
    controller.enable()
    motor = FakeMotor()
    driver = _driver(controller, motor)
    _arm_prefetch(controller, driver, motor)
    motor.reset()
    motor.draft_ready = False
    motor.pending = False  # worker finished with nothing (error/reject)

    route_motor_event_to_agenda(controller, "speaking_end")  # -> WAITING_SIGNAL
    caplog.set_level(logging.DEBUG, logger="opencohost.api.agenda_driver")
    driver.tick_once()

    assert driver._prefetch is None
    assert motor.replaced, "must fall back to a normal generation"
    # T1(c) [v5]: the INFO line reaches the SAME surface as every other
    # boundary line — routed through motor._log (the UI log_queue + the
    # motor's own logger), not the driver's own module-level logger.
    assert len(motor.logged) == 1
    msg, level = motor.logged[0]
    assert "Pregen boundary:" in msg
    assert "draft=none" in msg
    assert "source=kira-agenda" in msg
    assert "gap_ms=-1" in msg
    assert "gen_ms=-1" in msg
    # The driver's own module logger keeps a DEBUG-level trace only.
    debug_matches = [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.DEBUG and "Pregen boundary:" in r.getMessage()
    ]
    assert len(debug_matches) == 1


# ── WU4 4d: stash/engine pairing structural guard ───────────────────────────


def test_tick_debug_logs_when_stash_present_but_engine_has_no_cache_or_pending(caplog):
    controller, _ = _controller_with_queued_topic()
    controller.enable()
    motor = FakeMotor()
    driver = _driver(controller, motor)
    # Force desync: the driver believes a draft is stashed, but the engine
    # reports neither a ready cache nor an in-flight worker.
    driver._prefetch = PrefetchState(action=object(), topic_id="stale-topic")
    motor.draft_ready = False
    motor.pending = False

    caplog.set_level(logging.DEBUG, logger="opencohost.api.agenda_driver")
    driver.tick_once()

    assert any("desync" in r.getMessage() for r in caplog.records)


def test_tick_debug_logs_when_engine_has_draft_but_driver_forgot_its_stash(caplog):
    controller, _ = _controller_with_queued_topic()
    controller.enable()
    motor = FakeMotor()
    driver = _driver(controller, motor)
    driver.tick_once()  # opens the topic -> GENERATING (agenda-active state)
    driver._prefetch = None  # driver forgot its own stash
    motor.draft_ready = True  # but the engine still reports a ready draft

    caplog.set_level(logging.DEBUG, logger="opencohost.api.agenda_driver")
    driver.tick_once()

    assert any("desync" in r.getMessage() for r in caplog.records)


def test_tick_no_desync_log_when_stash_and_engine_agree(caplog):
    controller, _ = _controller_with_queued_topic(max_turns_per_topic=3, turn_batch_size=1)
    controller.enable()
    motor = FakeMotor()
    driver = _driver(controller, motor)
    _arm_prefetch(controller, driver, motor)  # stash present, worker spawned
    motor.pending = True  # honest post-spawn state: worker genuinely in flight

    caplog.set_level(logging.DEBUG, logger="opencohost.api.agenda_driver")
    driver.tick_once()

    assert not any("desync" in r.getMessage() for r in caplog.records)
