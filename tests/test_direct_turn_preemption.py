"""Steps 1 + 2 of `direct_turn_preemption_20260803` — a typed direct turn
enters the priority queue on ARRIVAL and pregenerates under cover of the
agenda's remaining speech.

Measured problem (logs/opencohost_20260803_135755.log, 2026-08-03): a typed
question waited for the whole in-flight agenda turn and only THEN started
generating — 81.3s and 65.5s from send to first spoken word, with
`Pregen boundary: draft=none source=direct` at both boundaries. The wait and
the generation were serial because (a) the turn sat in `command_queue`, invisible
to the interactive-pregen trigger which works off the priority-queue HEAD, and
(b) the trigger fires only from `enqueue()`, so a refusal while the agenda's own
pregen was in flight was never re-evaluated.

NEITHER step interrupts anything: the priority queue is non-preemptive and no
cut seam is touched, so the audible contract is unchanged (that is Step 3, which
lands separately). `test_nothing_here_ever_interrupts_the_agenda_speech` pins it.

Convention mirrors tests/test_interactive_pregen.py and
tests/test_interruption_connector.py: a constructed-but-unstarted motor with
`_generar_dialogo`/`_hablar`/`_ejecutar_inferencia` patched so no real
Ollama/TTS runs, the worker loop driven synchronously, and every cross-thread
sequence driven by `threading.Event` — never `time.sleep`.
"""

import logging
import queue
import threading
import time
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from opencohost.api.dispatch import Dispatcher
from opencohost.api.ptt_session import PttController
from opencohost.config.settings import DIRECT_ANSWER_MAX_WAIT_SECONDS
from opencohost.core.llm_engine import MotorVocalIA
from tests.test_api_phase1 import FakeHost


def _bare_motor() -> MotorVocalIA:
    # Hermetic (test_interactive_pregen.py:_bare_motor): pin the model and seed
    # both caches so _generar_dialogo never reaches the live ollama.show probe.
    motor = MotorVocalIA(queue.Queue(), lambda event: None)
    motor.current_model = "llama3"
    motor._reasoning_model_cache["llama3"] = False
    motor._model_ctx_limit = {"llama3": 8192}
    return motor


def _wait_until(pred, timeout: float = 5.0, interval: float = 0.005) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(interval)
    return bool(pred())


def _seed_queue(motor, items):
    with motor._pq_lock:
        motor._priority_queue = list(items)


def _item(payload, source, *, priority=1, age_seconds=0.0):
    """A 7-tuple in the shape enqueue() stores, aged by `age_seconds`."""
    return (priority, time.time() - age_seconds, payload, source, None, None, None)


def _agenda_draft():
    return {
        "payload": "AG2",
        "dialogo": "Siguiente beat de agenda.",
        "priority": 2,
        "source": "kira-agenda",
        "gen_ms": 12000,
    }


def _boundary_lines(caplog):
    return [r.getMessage() for r in caplog.records if "Pregen boundary:" in r.getMessage()]


# ══════════════════════════════════════════════════════════════════════════
# Step 1 — direct turns enter the priority queue on arrival
# ══════════════════════════════════════════════════════════════════════════


class _RealMotorHost(FakeHost):
    """FakeHost wired to a REAL MotorVocalIA, so `create_app` builds its
    Dispatcher over the real `command_queue` and the router's arrival-time hook
    exercises the real `_drain_pending_direct_into_priority_queue`."""

    def __init__(self):
        super().__init__()
        self.motor = _bare_motor()


@pytest.fixture
def busy_app():
    import opencohost.api.main as main_mod

    main_mod._host_active = False
    app = main_mod.create_app(host_factory=_RealMotorHost, cors_origins=["http://localhost:5173"])
    try:
        yield app
    finally:
        main_mod._host_active = False


def test_busy_arrival_moves_the_direct_turn_into_the_priority_queue(busy_app):
    """OQ-6: the chat router calls the existing drain after an accepted dispatch
    when the engine is busy, so the turn is a queue HEAD the pregen trigger can
    see instead of sitting unread in command_queue for the whole agenda turn."""
    with TestClient(busy_app) as client:
        motor = busy_app.state.host.motor
        motor._speaking = True  # mid-agenda-turn: run() is not reading command_queue

        resp = client.post("/api/chat/turn", json={"text": "que opinas de esto?"})

        assert resp.status_code == 200
        assert resp.json()["accepted"] is True
        assert motor.command_queue.empty(), "the turn must not still be waiting in command_queue"
        assert len(motor._priority_queue) == 1
        priority, _ts, payload, source, *_rest = motor._priority_queue[0]
        assert (priority, payload, source) == (1, "que opinas de esto?", "direct")


def test_idle_arrival_leaves_the_turn_on_the_normal_command_path(busy_app):
    """The hook is scoped to a BUSY engine. Idle, run() reads command_queue
    directly and dispatches the turn immediately — draining it into the priority
    queue would only add a hop."""
    with TestClient(busy_app) as client:
        motor = busy_app.state.host.motor

        resp = client.post("/api/chat/turn", json={"text": "hola"})

        assert resp.status_code == 200
        assert motor._priority_queue == []
        assert motor.command_queue.get_nowait()[:2] == ("process_context", "hola")


def test_arrival_hook_failure_never_breaks_the_receipt(busy_app):
    """The dispatch already succeeded, so the ack IS the receipt. A raising
    drain must never turn a queued turn into a 500 the client would retry."""
    with TestClient(busy_app) as client:
        motor = busy_app.state.host.motor
        motor._speaking = True
        motor._drain_pending_direct_into_priority_queue = MagicMock(side_effect=RuntimeError("boom"))

        resp = client.post("/api/chat/turn", json={"text": "hola"})

        assert resp.status_code == 200
        assert resp.json()["accepted"] is True
        # Still recoverable: the item is untouched in command_queue and the
        # engine-boundary drain (D3b) remains the backstop.
        assert motor.command_queue.qsize() == 1


# ── TTL exemption: the highest-risk line in Step 1 ─────────────────────────


def test_direct_item_aged_past_the_30s_ttl_is_not_swept_and_still_answers():
    """Arrival-time entry means a direct item ages for the WHOLE agenda turn.
    The 30s sweep would silently discard a turn the API already receipted as
    "queued" — the owner's question would simply never be answered."""
    motor = _bare_motor()
    answered = []
    motor._ejecutar_inferencia = lambda payload, **kw: answered.append((payload, kw.get("source")))
    _seed_queue(motor, [_item("pregunta directa", "direct", age_seconds=71.0)])

    motor._process_priority_queue()

    assert answered == [("pregunta directa", "direct")]
    assert motor._priority_queue == []


def test_direct_item_aged_past_the_documented_bound_is_swept():
    """The exemption is bounded, not unbounded: DIRECT_ANSWER_MAX_WAIT_SECONDS
    is the contractual bound Unit 4.2 already documents, so the mechanical bound
    and the promised one finally coincide."""
    motor = _bare_motor()
    answered = []
    motor._ejecutar_inferencia = lambda payload, **kw: answered.append(payload)
    _seed_queue(
        motor,
        [_item("pregunta vieja", "direct", age_seconds=DIRECT_ANSWER_MAX_WAIT_SECONDS + 10)],
    )

    motor._process_priority_queue()

    assert answered == []
    assert motor._priority_queue == []


def test_direct_item_just_under_the_documented_bound_still_answers():
    motor = _bare_motor()
    answered = []
    motor._ejecutar_inferencia = lambda payload, **kw: answered.append(payload)
    _seed_queue(
        motor,
        [_item("pregunta", "direct", age_seconds=DIRECT_ANSWER_MAX_WAIT_SECONDS - 10)],
    )

    motor._process_priority_queue()

    assert answered == ["pregunta"]


def test_chat_item_keeps_a_real_ttl(monkeypatch):
    """The exemption is scoped to source=="direct" only — a viewer `chat` item
    keeps the stale-reaction guard the TTL exists for. Tier split
    (tauri_stream_chat_20260812): the window is the configurable
    turn_priority.STREAM_TTL_SECONDS (default 120s), no longer a fixed 30s."""
    from opencohost.core import turn_priority

    monkeypatch.setattr(turn_priority, "STREAM_TTL_SECONDS", 30.0)
    motor = _bare_motor()
    answered = []
    motor._ejecutar_inferencia = lambda payload, **kw: answered.append(payload)
    _seed_queue(motor, [_item("mensaje viejo", "chat", age_seconds=45.0)])

    motor._process_priority_queue()

    assert answered == []
    assert motor._priority_queue == []


# ── Drain atomicity: FIFO between two directs typed seconds apart ──────────


class _ObservedLock:
    """threading.Lock wrapper that reports when a second thread has to WAIT.

    Duck-typed context manager, swapped in for `_direct_drain_lock`. Makes the
    serialization POSITIVELY observable (`contended` is set only on a real
    blocking acquire) instead of inferred from a sleep.
    """

    def __init__(self):
        self._inner = threading.Lock()
        self.contended = threading.Event()

    def __enter__(self):
        if not self._inner.acquire(blocking=False):
            self.contended.set()
            self._inner.acquire()
        return self

    def __exit__(self, *exc):
        self._inner.release()
        return False


def test_two_concurrent_drains_keep_direct_turns_in_fifo_order():
    """Two HTTP threads draining at once. The pop (command_queue.mutex) and the
    enqueue (_pq_lock) are separate critical sections, so without
    `_direct_drain_lock` the second drain can enqueue its item while the first
    is still between them — and enqueue sorts by insertion time, so the two
    questions come out in the wrong order."""
    motor = _bare_motor()
    lock = _ObservedLock()
    motor._direct_drain_lock = lock
    motor.command_queue.put(("process_context", "primera", "hist-1", "direct"))

    enqueued: list = []
    first_enqueue_entered = threading.Event()
    release_first = threading.Event()
    real_enqueue = motor.enqueue

    def _blocking_enqueue(payload, **kwargs):
        if not first_enqueue_entered.is_set():
            first_enqueue_entered.set()
            assert release_first.wait(5.0)
        enqueued.append(payload)
        real_enqueue(payload, **kwargs)

    motor.enqueue = _blocking_enqueue

    first = threading.Thread(target=motor._drain_pending_direct_into_priority_queue, daemon=True)
    first.start()
    assert first_enqueue_entered.wait(5.0), "the first drain never reached its enqueue"

    # The second question arrives while the first drain is parked mid-pair.
    motor.command_queue.put(("process_context", "segunda", "hist-2", "direct"))
    second = threading.Thread(target=motor._drain_pending_direct_into_priority_queue, daemon=True)
    second.start()
    assert lock.contended.wait(5.0), "the second drain never had to wait — pop->enqueue is not atomic"

    release_first.set()
    first.join(5.0)
    second.join(5.0)
    assert not first.is_alive() and not second.is_alive()

    assert enqueued == ["primera", "segunda"]
    assert [item[2] for item in motor._priority_queue] == ["primera", "segunda"]


def test_boundary_drain_backstop_still_moves_a_direct_behind_a_control_command():
    """D3b is KEPT, not replaced: the arrival-time hook only pops a LEADING
    direct, so a control command sitting ahead of it in FIFO still needs the
    engine boundary (which drains control commands first, then the direct)."""
    motor = _bare_motor()
    motor.command_queue.put(("clear_history", None))
    motor.command_queue.put(("process_context", "pregunta directa", "hist", "direct"))

    # The arrival-time hook cannot see it — a non-direct command is at the front.
    motor._drain_pending_direct_into_priority_queue()
    assert motor._priority_queue == []

    motor._complete_processing_cycle(process_queue=False)

    assert [item[2] for item in motor._priority_queue] == ["pregunta directa"]


# ── PTT joins the queue at its documented priority (interruptible_speech_ ──
# ── architecture_20260804, design.md §6 Step 1) ────────────────────────────


def test_boundary_drain_rescues_ptt_at_priority_zero():
    """Pre-Step-1 the guard was `comando[3] != "direct"`, so a leading `ptt`
    command was never even inspected — PTT was hardcoded to priority=1 on the
    API path despite enqueue()'s own docstring ("0 = PTT/streamer, highest").
    """
    motor = _bare_motor()
    motor.command_queue.put(("process_context", "pregunta ptt", "hist", "ptt"))

    motor._drain_pending_direct_into_priority_queue()

    assert len(motor._priority_queue) == 1
    priority, _ts, payload, source, *_rest = motor._priority_queue[0]
    assert (priority, payload, source) == (0, "pregunta ptt", "ptt")


def test_boundary_drain_rescues_a_direct_behind_a_ptt():
    """Pre-Step-1 the guard `return`ed (did not skip) on a non-"direct" front
    item, so a leading `ptt` also BLOCKED the `direct` sitting right behind it
    in FIFO from ever being rescued — impossible today, mirroring the
    control-command backstop at :292-306."""
    motor = _bare_motor()
    motor.command_queue.put(("process_context", "pregunta ptt", "hist-1", "ptt"))
    motor.command_queue.put(("process_context", "pregunta directa", "hist-2", "direct"))

    motor._drain_pending_direct_into_priority_queue()

    assert [item[3] for item in motor._priority_queue] == ["ptt", "direct"]


def test_ptt_dispatch_drains_at_arrival_when_the_engine_is_busy():
    """Mirrors the chat.py arrival hook (:184-191): without this, a deferred
    PTT question sits invisible in command_queue until the next boundary,
    during which the agenda tick thread can read
    pending_owner_questions() == 0 and start a fresh agenda turn."""
    motor = _bare_motor()
    motor._speaking = True  # mid-agenda-turn: run() is not reading command_queue
    dispatcher = Dispatcher(motor.command_queue)
    controller = PttController("ws://test/whisperlive", dispatcher, MagicMock(), motor=motor)

    controller._dispatch("pregunta ptt")

    assert motor.command_queue.empty(), "the turn must not still be waiting in command_queue"
    assert len(motor._priority_queue) == 1
    priority, _ts, _payload, source, *_rest = motor._priority_queue[0]
    assert (priority, source) == (0, "ptt")


def test_ptt_arrival_drain_failure_never_breaks_the_flush():
    """Best-effort by the same rule as chat.py: a raising drain must never
    turn a successfully dispatched PTT turn into a broken WS flush."""
    motor = _bare_motor()
    motor._speaking = True
    motor._drain_pending_direct_into_priority_queue = MagicMock(side_effect=RuntimeError("boom"))
    dispatcher = Dispatcher(motor.command_queue)
    controller = PttController("ws://test/whisperlive", dispatcher, MagicMock(), motor=motor)

    controller._dispatch("pregunta ptt")  # must not raise

    # Still recoverable: the item is untouched in command_queue and the
    # engine-boundary drain (D3b) remains the backstop.
    assert motor.command_queue.qsize() == 1


# ══════════════════════════════════════════════════════════════════════════
# Step 2 — pregen picks the direct turn up under cover of speech
# ══════════════════════════════════════════════════════════════════════════


def test_pregen_completion_retriggers_for_a_direct_head_that_arrived_mid_flight():
    """The measured gap: at 14:33:12 the direct turn was enqueued while the
    agenda's own pregen ran (14:33:11 -> 14:33:25). The GPU-free gate refused,
    and nothing re-evaluated the queue when that generation finished."""
    motor = _bare_motor()
    motor._speaking = True  # under cover of the agenda's speech
    generated: list = []
    agenda_generating = threading.Event()
    let_agenda_finish = threading.Event()

    def _gen(contexto, source="direct", commit_history=True, history_text=None, log_prefix="LLM"):
        generated.append((contexto, source))
        if source == "kira-agenda":
            motor._llm_generating = True  # the single-Ollama runner flag
            agenda_generating.set()
            assert let_agenda_finish.wait(5.0)
            motor._llm_generating = False
        return f"texto de {contexto}"

    motor._generar_dialogo = _gen

    # 1. the agenda pregenerates its own next turn.
    assert motor.pregenerate("AG2", priority=2, source="kira-agenda") is True
    assert agenda_generating.wait(5.0)

    # 2. the direct turn arrives mid-flight — enqueue's own trigger is refused
    #    by the GPU-free gate, exactly as measured.
    motor.enqueue("pregunta directa", priority=1, source="direct")
    assert generated == [("AG2", "kira-agenda")]

    # 3. the agenda pregen completes -> the completion re-trigger takes the
    #    freed slot for the direct head, still under cover of the same speech.
    let_agenda_finish.set()
    assert _wait_until(lambda: ("pregunta directa", "direct") in generated), \
        "the pregen completion never re-evaluated the queue head"
    assert _wait_until(lambda: (motor._prefetched_agenda or {}).get("source") == "direct")
    assert motor._prefetched_agenda["dialogo"] == "texto de pregunta directa"


def test_completion_retrigger_skips_when_its_own_item_is_still_the_head():
    """A spawn that stored nothing (rejected/empty draft) leaves the slot empty
    with its own item still at the head. Re-firing there would spawn the
    identical generation again — and again at ITS completion — an unbounded
    regeneration loop for as long as the speech lasts."""
    motor = _bare_motor()
    motor._speaking = True
    spawned: list = []
    motor.pregenerate = lambda *args, **kwargs: (spawned.append(args), True)[1]
    _seed_queue(motor, [_item("pregunta directa", "direct")])

    motor._retrigger_interactive_pregen(("pregunta directa", "direct"))
    assert spawned == [], "a spawn must not re-fire for its own still-head item"

    # A DIFFERENT head (the agenda pregen that just finished) does re-fire.
    motor._retrigger_interactive_pregen(("AG2", "kira-agenda"))
    assert spawned == [("pregunta directa", 1, "direct")]


def test_completion_retrigger_is_a_noop_on_an_empty_queue():
    motor = _bare_motor()
    motor._speaking = True
    spawned: list = []
    motor.pregenerate = lambda *args, **kwargs: (spawned.append(args), True)[1]

    motor._retrigger_interactive_pregen(("AG2", "kira-agenda"))

    assert spawned == []


def test_completion_retrigger_still_honours_the_gpu_free_gate():
    """The re-trigger adds a call site, not an exemption: every existing gate in
    `_maybe_trigger_interactive_pregen` re-applies unchanged."""
    motor = _bare_motor()
    spawned: list = []
    motor.pregenerate = lambda *args, **kwargs: (spawned.append(args), True)[1]
    _seed_queue(motor, [_item("pregunta directa", "direct")])

    motor._speaking = False  # not speaking -> nothing to hide a generation under
    motor._retrigger_interactive_pregen(("AG2", "kira-agenda"))
    assert spawned == []

    motor._speaking = True
    motor._llm_generating = True  # a generation already in flight
    motor._retrigger_interactive_pregen(("AG2", "kira-agenda"))
    assert spawned == []

    motor._llm_generating = False
    motor._retrigger_interactive_pregen(("AG2", "kira-agenda"))
    assert spawned == [("pregunta directa", 1, "direct")]


# ── Slot handover: freeze, don't evict (OQ-2) ──────────────────────────────


def test_direct_pregen_freezes_the_displaced_agenda_draft(caplog):
    """OQ-2: 11-14s of already-paid GPU work survives the detour and resumes
    with a connector, instead of being discarded and regenerated into dead air."""
    motor = _bare_motor()
    motor._generar_dialogo = lambda *args, **kwargs: ""  # store nothing; isolate the handover
    motor._prefetched_agenda = _agenda_draft()
    epoch_before = motor._prefetch_epoch

    caplog.set_level(logging.INFO, logger="OpenCohost")
    assert motor.pregenerate("pregunta directa", priority=1, source="direct") is True
    motor._prefetch_thread.join(5.0)

    assert motor._frozen_stash is not None
    assert motor._frozen_stash["dialogo"] == "Siguiente beat de agenda."
    assert motor._frozen_stash["source"] == "kira-agenda"
    assert motor._frozen_stash["connector"] is None  # resolved at the return
    assert motor._frozen_stash_at is not None
    assert motor._prefetched_agenda is None, "the slot must be freed for the direct draft"
    assert motor._prefetch_epoch == epoch_before, "freezing is not invalidation — no epoch bump"

    lines = _boundary_lines(caplog)
    assert len(lines) == 1
    assert "draft=frozen" in lines[0]
    assert "source=kira-agenda" in lines[0], "the DISPLACED occupant's source"
    assert "gen_ms=12000" in lines[0]


def test_chat_pregen_still_evicts_the_displaced_agenda_draft(caplog):
    """Scoped to source=="direct" on purpose: `_frozen_stash` has exactly one
    consumer, api/agenda_driver.py, and `direct` only exists on the surface that
    runs that driver. A CTK-only `chat` winner would freeze a beat with nothing
    to return it — strictly worse than the eviction it would replace."""
    motor = _bare_motor()
    motor._generar_dialogo = lambda *args, **kwargs: ""
    motor._prefetched_agenda = _agenda_draft()
    epoch_before = motor._prefetch_epoch

    caplog.set_level(logging.INFO, logger="OpenCohost")
    assert motor.pregenerate("mensaje de viewer", priority=1, source="chat") is True
    motor._prefetch_thread.join(5.0)

    assert motor._frozen_stash is None
    assert motor._prefetched_agenda is None
    assert motor._prefetch_epoch == epoch_before + 1
    assert "draft=evicted" in _boundary_lines(caplog)[0]


def test_direct_pregen_never_overwrites_an_already_frozen_stash():
    """An overwrite would destroy the beat the driver is currently holding ticks
    for. Fall back to the pre-existing eviction instead."""
    motor = _bare_motor()
    motor._generar_dialogo = lambda *args, **kwargs: ""
    motor._frozen_stash = {"payload": "AG1", "dialogo": "Beat anterior.", "source": "kira-agenda",
                           "priority": 2, "connector": None}
    motor._frozen_stash_at = time.monotonic()
    motor._prefetched_agenda = _agenda_draft()
    epoch_before = motor._prefetch_epoch

    assert motor.pregenerate("pregunta directa", priority=1, source="direct") is True
    motor._prefetch_thread.join(5.0)

    assert motor._frozen_stash["payload"] == "AG1", "the pending return must survive"
    assert motor._prefetched_agenda is None
    assert motor._prefetch_epoch == epoch_before + 1


def test_frozen_beat_resumes_with_a_connector_after_the_direct_answer():
    """End of the OQ-2 chain, all existing machinery: the direct answer counts
    as a detour turn and the beat comes back with a connector instead of being
    regenerated from scratch."""
    motor = _bare_motor()
    motor._generar_dialogo = lambda *args, **kwargs: ""
    motor._prefetched_agenda = _agenda_draft()

    assert motor.pregenerate("pregunta directa", priority=1, source="direct") is True
    motor._prefetch_thread.join(5.0)
    assert motor.has_frozen_stash() is True

    # The direct answer is selected to speak at the boundary.
    motor._note_detour_turn("direct")
    assert motor.detour_started() is True
    assert motor.detour_exceeded() is False

    key = motor.restore_frozen_stash("el tema de hoy")

    assert key == ("AG2", "kira-agenda", 2)
    restored = motor._prefetched_agenda
    assert restored["dialogo"] == "Siguiente beat de agenda."
    assert restored["resumed"] is True
    assert restored["connector"], "the beat must resume with a connector, not cold"
    assert motor.has_frozen_stash() is False


# ── Contention: a direct turn wins the next slot, never an in-flight call ──


def test_direct_pregen_never_preempts_a_generation_already_in_flight():
    """§5: `_call_with_watchdog` only stops WAITING — the Ollama call keeps
    computing server-side, so cancelling buys zero GPU seconds and a second
    spawn would leave a zombie worker poisoning shared bookkeeping. The direct
    turn waits out the in-flight pregen and takes the slot at its completion."""
    motor = _bare_motor()
    agenda_generating = threading.Event()
    let_agenda_finish = threading.Event()

    def _gen(contexto, source="direct", commit_history=True, history_text=None, log_prefix="LLM"):
        if source == "kira-agenda":
            agenda_generating.set()
            assert let_agenda_finish.wait(5.0)
        return f"texto de {contexto}"

    motor._generar_dialogo = _gen
    assert motor.pregenerate("AG2", priority=2, source="kira-agenda") is True
    assert agenda_generating.wait(5.0)

    # Nothing is cached yet — the in-flight refusal (F2) is load-bearing.
    assert motor.pregenerate("pregunta directa", priority=1, source="direct") is False
    assert motor._frozen_stash is None, "an in-flight draft is never frozen mid-generation"

    let_agenda_finish.set()
    assert _wait_until(lambda: (motor._prefetched_agenda or {}).get("source") == "kira-agenda")


def test_nothing_here_ever_interrupts_the_agenda_speech():
    """The audible contract is UNCHANGED by Steps 1+2 — the reply is merely
    ready at the boundary. Cutting is Step 3 and lands alone."""
    motor = _bare_motor()
    motor._generar_dialogo = lambda *args, **kwargs: "RESPUESTA"
    motor.interrupt_speaking = MagicMock()
    motor._speaking = True
    motor._current_speech_source = "kira-agenda"
    motor._prefetched_agenda = _agenda_draft()
    motor.command_queue.put(("process_context", "pregunta directa", "hist", "direct"))

    # Arrival-time entry -> enqueue's trigger -> pregen -> slot handover ->
    # completion re-trigger. The whole Step 1 + Step 2 chain.
    motor._drain_pending_direct_into_priority_queue()
    assert _wait_until(lambda: (motor._prefetched_agenda or {}).get("source") == "direct")

    motor.interrupt_speaking.assert_not_called()
    assert motor.is_speaking is True
    assert motor._current_speech_source == "kira-agenda"
    assert motor._priority_queue[0][3] == "direct", "the answer waits for the boundary"


# ── Overflow: an owner question is never silently demoted (interruptible_ ──
# ── speech_architecture_20260804, design.md §6 Step 2 / T2.1) ──────────────


def test_overflow_never_demotes_owner_questions_lets_the_queue_grow():
    """Eight `direct` items over a cap of 5: today's `pop()` blindly demotes
    the newest-lowest-priority item into `_accumulation_buffer`, where it is
    answered under source="accumulated" -- excluded from all five privacy/
    personalization frozensets. An owner question must never take that path;
    the queue is allowed to exceed `_pq_max_items` instead."""
    motor = _bare_motor()
    assert motor._pq_max_items == 5

    for i in range(8):
        motor.enqueue(f"pregunta {i}", priority=1, source="direct")

    assert motor._accumulation_buffer == []
    assert len(motor._priority_queue) == 8
    assert [item[2] for item in motor._priority_queue] == [f"pregunta {i}" for i in range(8)]


def test_overflow_still_demotes_viewer_chat_ahead_of_owner_questions():
    """Mixed queue: only `chat` items may be demoted on overflow; every
    `direct` item survives."""
    motor = _bare_motor()

    for i in range(5):
        motor.enqueue(f"chat {i}", priority=1, source="chat")
    for i in range(3):
        motor.enqueue(f"pregunta {i}", priority=1, source="direct")

    # Accumulation-buffer tuples are (ts, payload, source) -- not the
    # priority-queue's (priority, ts, payload, source, ...) shape.
    assert len(motor._accumulation_buffer) == 3
    assert all(source == "chat" for _ts, _payload, source in motor._accumulation_buffer)

    assert len(motor._priority_queue) == 5
    surviving_direct = [item[2] for item in motor._priority_queue if item[3] == "direct"]
    assert surviving_direct == ["pregunta 0", "pregunta 1", "pregunta 2"], (
        "no direct item may ever be demoted, regardless of which chat items survive"
    )
