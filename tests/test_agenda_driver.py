"""FIX-C: the headless AgendaDriver + its pure helpers.

`AgendaDriver` replicates the CTK tick loop (app_shell.py:1539-1632) for the
FastAPI host so a passive `KiraAgendaController` actually drives Kira.  These
tests use a fake motor (records enqueue/replace_pending, exposes
is_processing/is_speaking flags) and a REAL controller — the controller and
motor need zero changes, so the fake only has to be a faithful stand-in for
the two motor methods the driver calls.
"""

import threading

from opencohost.api.agenda_driver import (
    AgendaDriver,
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

    def enqueue(self, payload, priority=1, source="chat", history_text=None):
        self.enqueued.append(
            {"payload": payload, "priority": priority, "source": source, "history_text": history_text}
        )

    def replace_pending(self, payload, priority=1, source="chat"):
        self.replaced.append({"payload": payload, "priority": priority, "source": source})

    def clear_prefetched_agenda(self):
        self.cleared_prefetch += 1

    def drop_pending_sources(self, prefixes):
        self.dropped_prefixes.append(prefixes)
        return 0

    def interrupt_speaking(self):
        self.interrupted += 1

    def reset(self):
        self.enqueued.clear()
        self.replaced.clear()


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
