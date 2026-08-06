"""Strict TDD for step 4 batch 1 of the speech-router landing sequence
(conductor/tracks/interruptible_speech_architecture_20260804/speech-router-design.md
§5.2, §0 timeline rows 20.3-32.5; conductor/tracks/interruptible_speech_architecture_20260804/
step4-plan.md batch 1).

Today `_process_priority_queue` refuses to pop anything while
`self._processing or self._speech_active`. Batch 1 widens that gate per
§5.2's normative predicate: `if self._processing: return; if
self._speech_active and top_of_queue.priority != 0: return` -- ONLY when the
step-3/4 conjunction (`_speech_interrupt_enabled and _speech_router_enabled`)
is armed. Either switch off keeps the legacy combined guard byte-identical
(step-2 semantics). The `:1741` tail-call in `_dispatch_command`'s
`process_context` busy-enqueue branch closes the arrival-latency hole: a
command dispatched while busy used to strand its re-enqueued item until
run()'s 1s idle tick.

Section 7 discipline, inherited verbatim from test_speech_router.py /
test_speech_router_stack.py: real MotorVocalIA, REAL router thread, REAL
`_hablar_impl` producer/consumer loop, REAL `_process_priority_queue`. ONLY
Piper synthesis and `pygame.mixer` are faked -- `_generar_dialogo` is
stubbed at the LLM-call boundary only, so the REAL `_ejecutar_inferencia` ->
`_speak_or_submit` -> router path carries the generated answer.
`threading.Event` handshakes only -- never `time.sleep` as a synchronizer,
never a hand-set field the code is supposed to derive.
"""
from __future__ import annotations

import threading

from tests.test_speech_router import _GateMixerMusic, _pump
from tests.test_speech_router_stack import (
    _armed,
    _job_sentences,
    _job_text,
    _synth_logger,
    _wait_until,
    router_motors,
)


# ──────────────────────────────────────────────────────────────────────────
# (1) the widened predicate — generation runs UNDER the filler
# ──────────────────────────────────────────────────────────────────────────


def test_a_priority0_item_pops_and_generates_while_the_filler_plays(router_motors):
    """design §5.2 + §0 rows 20.3-32.5: a priority-0 owner item pops and
    GENERATES while the filler (an agenda job) is still ACTIVE on the
    router. The answer's submit then D3-preempts the filler (uniform
    preemption, unchanged from step 3); the filler resumes and finishes --
    nothing lost (I1)."""
    mixer = _GateMixerMusic(block_at=(0, 1))
    synth_log: list = []
    motor, rec, _ = _armed(
        router_motors, mixer_music=mixer, synthesize=_synth_logger(synth_log), interrupt_enabled=True
    )
    motor.is_ready = True

    f0, f1 = _job_sentences("FILLER", 2)
    answer_text = _job_text("ANSWER", 1)
    gen_started = threading.Event()

    def _gen(contexto, source="direct", commit_history=True, history_text=None, **kw):
        # Fired by the widened pop's own worker — the ONLY possible origin,
        # since the interactive-pregen trigger is neutralised below. The
        # filler's mixer gate (index 0) is never released before this test
        # checks this event, so the filler is structurally still ACTIVE/gated
        # at this exact moment: the generation genuinely overlapped playback.
        gen_started.set()
        return answer_text

    motor._generar_dialogo = _gen
    # Batch-1 scope isolation: `enqueue()`'s own tail unconditionally offers
    # the new head to the PRE-EXISTING interactive-pregen trigger (WU3),
    # which independently calls `_generar_dialogo` whenever `is_speaking`
    # is true -- true here by construction (the filler is gated). That is a
    # confound for these tests (which assert on POP-triggered generation
    # specifically), not a behavior batch 1 touches; the pregen/widened-pop
    # interaction is explicitly step4-plan.md batch 2's audit, not this
    # one's. Neutralised here so `_gen`'s call log unambiguously reflects
    # only `_process_priority_queue`'s own pop.
    motor._maybe_trigger_interactive_pregen = lambda *a, **kw: None

    stop = threading.Event()
    pump = _pump(motor, stop)
    try:
        motor._speak_or_submit(_job_text("FILLER", 2), source="kira-agenda:t1")
        assert mixer.entered[0].wait(5.0), "filler never reached playback"

        motor.enqueue("pregunta directa del streamer", priority=0, source="direct")
        worker = threading.Thread(target=motor._process_priority_queue, daemon=True)
        worker.start()

        assert gen_started.wait(5.0), "generation never started while the filler was active"

        router = motor._speech_router
        assert _wait_until(lambda: len(router._stack) == 1), "the filler never suspended for the answer"
        assert router._stack[0].source == "kira-agenda:t1"
        assert router._stack[0].cursor == 0
        assert mixer.entered[1].wait(5.0), "the generated answer never preempted the filler"
        worker.join(5.0)
        assert not worker.is_alive(), "the widened pop's worker never returned"

        mixer.release(1)
        assert rec.wait_for("speaking_end", 1, timeout=10.0), rec.names()
    finally:
        stop.set()
        pump.join(5.0)

    assert router._stack == []
    # I1: every fragment index accounted for. f0/f1 each synthesised twice
    # (the cut attempt + the real resumed play — f1 was eagerly produced
    # ahead of the gated consumer on the first activation, same as the
    # step-3 sibling test_answer_preempts_the_filler_again_after_a_ptt_up_resume).
    assert synth_log.count(f0) == 2
    assert synth_log.count(f1) == 2
    assert synth_log.count(answer_text) == 1
    assert rec.boundary_sources() == [
        ("speaking_start", "kira-agenda:t1"),
        ("speaking_end", None),
    ]


# ──────────────────────────────────────────────────────────────────────────
# (2) pin — chat/agenda never join the widened selection
# ──────────────────────────────────────────────────────────────────────────


def test_chat_and_agenda_stay_queued_under_playback(router_motors):
    """PIN (§5.2): priority-1 (chat) and priority-2 (agenda) items enqueued
    while the filler is ACTIVE are not priority-0 -- the widened gate
    restricts this iteration's selection to priority-0 only, so they wait
    for a real boundary (job FINISHED), then process in priority order."""
    mixer = _GateMixerMusic(block_at=(0,))
    order: list = []

    def _gen(contexto, source="direct", commit_history=True, history_text=None, **kw):
        order.append(source)
        return f"respuesta {source}"

    motor, rec, _ = _armed(router_motors, mixer_music=mixer, interrupt_enabled=True)
    motor.is_ready = True
    motor._generar_dialogo = _gen
    # Batch-1 scope isolation: `enqueue()`'s own tail unconditionally offers
    # the new head to the PRE-EXISTING interactive-pregen trigger (WU3),
    # which independently calls `_generar_dialogo` whenever `is_speaking`
    # is true -- true here by construction (the filler is gated). That is a
    # confound for these tests (which assert on POP-triggered generation
    # specifically), not a behavior batch 1 touches; the pregen/widened-pop
    # interaction is explicitly step4-plan.md batch 2's audit, not this
    # one's. Neutralised here so `_gen`'s call log unambiguously reflects
    # only `_process_priority_queue`'s own pop.
    motor._maybe_trigger_interactive_pregen = lambda *a, **kw: None

    stop = threading.Event()
    pump = _pump(motor, stop)
    try:
        motor._speak_or_submit(_job_text("FILLER", 1), source="kira-agenda:t1")
        assert mixer.entered[0].wait(5.0), "filler never reached playback"

        motor.enqueue("mensaje del chat", priority=1, source="chat")
        motor.enqueue("bloque de agenda", priority=2, source="kira-agenda:t2")

        motor._process_priority_queue()
        assert order == [], "a non-owner item popped while speech was active"
        assert [item[3] for item in motor._priority_queue] == ["chat", "kira-agenda:t2"], (
            "the widened gate must not reorder or drop queued items"
        )

        mixer.release(0)
        assert rec.wait_for("speaking_end", 1, timeout=10.0), rec.names()
        assert _wait_until(lambda: order == ["chat", "kira-agenda:t2"], timeout=10.0), order
    finally:
        stop.set()
        pump.join(5.0)


# ──────────────────────────────────────────────────────────────────────────
# (3) pin — kill switch off is byte-identical step-2 behaviour
# ──────────────────────────────────────────────────────────────────────────


def test_widened_pop_is_inert_with_the_interrupt_switch_off(router_motors):
    """PIN: with `_speech_interrupt_enabled` off the conjunction never
    arms -- the legacy combined guard (`if self._processing or
    self._speech_active: return`) applies byte-identical to step 2, even
    for a priority-0 item."""
    mixer = _GateMixerMusic(block_at=(0,))
    generated: list = []

    def _gen(contexto, source="direct", commit_history=True, history_text=None, **kw):
        generated.append(source)
        return "no deberia hablarse"

    motor, rec, _ = _armed(router_motors, mixer_music=mixer, interrupt_enabled=False)
    motor.is_ready = True
    motor._generar_dialogo = _gen
    # Batch-1 scope isolation: `enqueue()`'s own tail unconditionally offers
    # the new head to the PRE-EXISTING interactive-pregen trigger (WU3),
    # which independently calls `_generar_dialogo` whenever `is_speaking`
    # is true -- true here by construction (the filler is gated). That is a
    # confound for these tests (which assert on POP-triggered generation
    # specifically), not a behavior batch 1 touches; the pregen/widened-pop
    # interaction is explicitly step4-plan.md batch 2's audit, not this
    # one's. Neutralised here so `_gen`'s call log unambiguously reflects
    # only `_process_priority_queue`'s own pop.
    motor._maybe_trigger_interactive_pregen = lambda *a, **kw: None

    motor._speak_or_submit(_job_text("FILLER", 1), source="kira-agenda:t1")
    assert mixer.entered[0].wait(5.0), "filler never reached playback"

    motor.enqueue("pregunta directa del streamer", priority=0, source="direct")
    motor._process_priority_queue()

    assert generated == [], "a priority-0 item popped mid-playback with the interrupt switch off"
    assert [item[3] for item in motor._priority_queue] == ["direct"]

    mixer.release(0)
    assert rec.wait_for("speaking_end", 1, timeout=10.0), rec.names()


# ──────────────────────────────────────────────────────────────────────────
# (4) the arrival-latency hole — the :1741 tail-call
# ──────────────────────────────────────────────────────────────────────────


def test_a_ptt_answer_enqueued_mid_playback_pops_without_the_idle_tick(router_motors):
    """step4-plan.md batch 1 item 2: the `_dispatch_command` process_context
    busy-enqueue branch must tail-call `_process_priority_queue` so a
    priority-0 item that arrives mid-playback pops NOW on the engine
    thread. Driven through `motor.command_queue` with `_pump` (run()'s
    command loop WITHOUT its idle branch) -- any progress here proves the
    tail-call, never the 1s idle tick."""
    mixer = _GateMixerMusic(block_at=(0,))
    popped = threading.Event()

    def _gen(contexto, source="direct", commit_history=True, history_text=None, **kw):
        popped.set()
        return "respuesta ptt"

    motor, rec, _ = _armed(router_motors, mixer_music=mixer, interrupt_enabled=True)
    motor.is_ready = True
    motor._generar_dialogo = _gen
    # Batch-1 scope isolation: `enqueue()`'s own tail unconditionally offers
    # the new head to the PRE-EXISTING interactive-pregen trigger (WU3),
    # which independently calls `_generar_dialogo` whenever `is_speaking`
    # is true -- true here by construction (the filler is gated). That is a
    # confound for these tests (which assert on POP-triggered generation
    # specifically), not a behavior batch 1 touches; the pregen/widened-pop
    # interaction is explicitly step4-plan.md batch 2's audit, not this
    # one's. Neutralised here so `_gen`'s call log unambiguously reflects
    # only `_process_priority_queue`'s own pop.
    motor._maybe_trigger_interactive_pregen = lambda *a, **kw: None

    stop = threading.Event()
    pump = _pump(motor, stop)
    try:
        motor._speak_or_submit(_job_text("FILLER", 1), source="kira-agenda:t1")
        assert mixer.entered[0].wait(5.0), "filler never reached playback"

        motor.command_queue.put(("process_context", "pregunta ptt", "hist-ptt", "ptt"))

        assert popped.wait(5.0), "no pop at arrival -- the turn ate a full idle tick"
    finally:
        stop.set()
        pump.join(5.0)


# ──────────────────────────────────────────────────────────────────────────
# (5) the batch-1 blocker — busy-enqueue must use the documented priority
# ──────────────────────────────────────────────────────────────────────────


def test_a_busy_enqueued_ptt_lands_at_priority_zero_like_the_boundary_drain(router_motors):
    """PIN (step-4 batch-1 blocker, fixed 2026-08-05): `_dispatch_command`'s
    busy-enqueue branch hardcoded `priority=1` for EVERY source — the exact
    class direct_turn_preemption_20260803 Step 1 fixed in
    `_drain_pending_direct_into_priority_queue` (0 for ptt, per enqueue()'s
    own docstring) and never mirrored to this second call site. At priority
    1 a busy-enqueued PTT question was TTL-expirable (the sweep's `prio > 0`
    guard, against its own "Expire stale non-PTT items" contract) and
    invisible to the widened §5.2 selection. The fix is UNCONDITIONAL, like
    the sibling drain's — pinned here with the interrupt switch OFF so the
    items stay queued long enough to inspect (and to prove the priority is
    not a step-4 behavior but the queue's documented contract)."""
    mixer = _GateMixerMusic(block_at=(0,))
    generated: list = []

    motor, rec, _ = _armed(router_motors, mixer_music=mixer, interrupt_enabled=False)
    motor.is_ready = True
    motor._generar_dialogo = lambda *a, **kw: generated.append(a) or "sin uso"
    motor._maybe_trigger_interactive_pregen = lambda *a, **kw: None

    motor._speak_or_submit(_job_text("FILLER", 1), source="kira-agenda:t1")
    assert mixer.entered[0].wait(5.0), "filler never reached playback"

    # The REAL production entry run() uses (`_consume_command` on the engine
    # thread) — the busy branch fires because the filler holds _speech_active.
    motor._consume_command(("process_context", "pregunta ptt", "hist-ptt", "ptt"))
    motor._consume_command(("process_context", "pregunta escrita", "hist-d", "direct"))

    assert [(item[0], item[3]) for item in motor._priority_queue] == [
        (0, "ptt"),
        (1, "direct"),
    ], "busy-enqueue must use each source's documented priority (0=ptt, 1=direct)"
    assert generated == [], "nothing may pop with the interrupt switch off"

    mixer.release(0)
    assert rec.wait_for("speaking_end", 1, timeout=10.0), rec.names()


# ──────────────────────────────────────────────────────────────────────────
# (6) design §5.2 hold path — the pop does silent work under a live mic
# ──────────────────────────────────────────────────────────────────────────


def test_pop_during_hold_generates_silently_and_the_answer_waits_for_release(router_motors):
    """design §5.2 hold path: a PTT question that arrives while the mic is
    LIVE still pops -- the suspended filler keeps `_speech_active` True
    through the router stack (`has_work()`), so the busy-enqueue branch
    fires and the widened gate admits the priority-0 head. The generation
    is useful SILENT work: the answer lands in the router's INCOMING lane,
    where `_ptt_held` blocks `_pick()` until release -- nothing reaches the
    mixer while held. On release INCOMING beats STACK (`_pick` order): the
    answer plays FIRST, then the held filler resumes from its owed cursor
    and finishes. Lossless end to end (I1)."""
    mixer = _GateMixerMusic(block_at=(1, 2))
    synth_log: list = []
    gen_called = threading.Event()
    motor, rec, _ = _armed(
        router_motors, mixer_music=mixer, synthesize=_synth_logger(synth_log), interrupt_enabled=True
    )
    motor.is_ready = True

    f0, f1 = _job_sentences("FILLER", 2)
    a0, = _job_sentences("ANSWER", 1)

    def _gen(contexto, source="direct", commit_history=True, history_text=None, **kw):
        gen_called.set()
        return _job_text("ANSWER", 1)

    motor._generar_dialogo = _gen
    # Batch-1 scope isolation: `enqueue()`'s own tail unconditionally offers
    # the new head to the PRE-EXISTING interactive-pregen trigger (WU3),
    # which independently calls `_generar_dialogo` whenever `is_speaking`
    # is true -- true here by construction (the filler is gated). That is a
    # confound for these tests (which assert on POP-triggered generation
    # specifically), not a behavior batch 1 touches; the pregen/widened-pop
    # interaction is explicitly step4-plan.md batch 2's audit, not this
    # one's. Neutralised here so `_gen`'s call log unambiguously reflects
    # only `_process_priority_queue`'s own pop.
    motor._maybe_trigger_interactive_pregen = lambda *a, **kw: None

    stop = threading.Event()
    pump = _pump(motor, stop)
    try:
        motor._speak_or_submit(_job_text("FILLER", 2), source="kira-agenda:t1")
        assert mixer.entered[1].wait(5.0), "filler never reached its second fragment"

        # PTT press through the PRODUCTION path: hold armed and the filler
        # suspended losslessly at its in-flight cursor, in one acquisition.
        motor.pause_speech_for_ptt()
        router = motor._speech_router
        assert _wait_until(lambda: len(router._stack) == 1), "filler never suspended on the press"
        filler_job = router._stack[0]
        assert filler_job.cursor == 1 and filler_job.spoken == [0]
        loads_before = mixer.load_count

        # While HELD: the question arrives (the busy-enqueue idiom -- the
        # stack keeps `_speech_active` True). It must pop and generate NOW.
        motor.command_queue.put(("process_context", "pregunta ptt", "hist-ptt", "ptt"))
        assert gen_called.wait(5.0), "the priority-0 item never popped during the hold"
        assert motor._priority_queue == [], "the pop must have emptied the queue"
        assert _wait_until(lambda: len(router._incoming) == 1), "the answer never reached INCOMING"
        answer_job = router._incoming[0]
        assert answer_job.source == "ptt"
        # Silence invariant: the mic is live, so the answer WAITS -- no new
        # fragment reaches the mixer while held.
        assert router._ptt_held is True and router._active is None
        # Honest settle (judge NIT): give a broken `_ptt_held` the beat it
        # would need to schedule the answer before reading the counter —
        # entered[2] is the answer's own gate, so this wait doubles as the
        # regression detector without consuming the later positive wait.
        assert not mixer.entered[2].wait(0.25), "the answer started playing while the mic was live"
        assert mixer.load_count == loads_before, "a fragment reached the mixer while the mic was live"

        # Release: INCOMING beats STACK -- the answer plays FIRST...
        motor.resume_speech_after_ptt()
        assert mixer.entered[2].wait(5.0), "the answer never played on release"
        assert router._active is answer_job
        assert router._stack and router._stack[0] is filler_job and filler_job.cursor == 1, (
            "the filler must still be suspended while the answer plays"
        )

        # ...then the filler resumes from its owed cursor and finishes.
        mixer.release(2)
        assert rec.wait_for("speaking_end", 1, timeout=10.0), rec.names()
    finally:
        stop.set()
        pump.join(5.0)

    # Lossless ledger (I1): pre-pause fragment + full answer + owed fragment,
    # in that completion order, nothing dropped or repeated.
    assert router._stack == [] and router._active is None
    assert router.completed_job_ids() == [answer_job.job_id, filler_job.job_id]
    assert sorted(filler_job.spoken) == [0, 1] and filler_job.cursor == 2
    assert answer_job.spoken == [0]
    assert synth_log.count(f0) == 1, "the pre-pause fragment plays once, never replayed"
    assert synth_log.count(f1) == 2, "the owed fragment: the cut attempt + the resumed replay"
    assert synth_log.count(a0) == 1
    assert rec.boundary_sources() == [
        ("speaking_start", "kira-agenda:t1"),
        ("speaking_end", None),
    ]


# ──────────────────────────────────────────────────────────────────────────
# (7) invariant I7 — one runner: generation serialises under playback
# ──────────────────────────────────────────────────────────────────────────


def test_generation_serialises_under_playback(router_motors):
    """PIN (I7): the widened gate lets a priority-0 item GENERATE while
    speech plays, but generation itself stays serialised on the single
    Ollama runner -- `_processing` is the loop-top early-return. While A's
    generation is in flight a second priority-0 item must stay QUEUED (no
    second `_generar_dialogo` starts), and it pops strictly AFTER A
    completes, on the same drain loop. Both answers eventually play."""
    mixer = _GateMixerMusic(block_at=(0,))
    order: list = []
    a_started = threading.Event()
    b_started = threading.Event()
    a_release = threading.Event()

    def _gen(contexto, source="direct", commit_history=True, history_text=None, **kw):
        order.append(history_text)
        if history_text == "hist-a":
            a_started.set()
            a_release.wait(10.0)  # A blocks at the LLM boundary (_processing True)
            return _job_text("ANSA", 1)
        b_started.set()
        return _job_text("ANSB", 1)

    motor, rec, _ = _armed(router_motors, mixer_music=mixer, interrupt_enabled=True)
    motor.is_ready = True
    motor._generar_dialogo = _gen
    # Batch-1 scope isolation: `enqueue()`'s own tail unconditionally offers
    # the new head to the PRE-EXISTING interactive-pregen trigger (WU3),
    # which independently calls `_generar_dialogo` whenever `is_speaking`
    # is true -- true here by construction (the filler is gated). That is a
    # confound for these tests (which assert on POP-triggered generation
    # specifically), not a behavior batch 1 touches; the pregen/widened-pop
    # interaction is explicitly step4-plan.md batch 2's audit, not this
    # one's. Neutralised here so `_gen`'s call log unambiguously reflects
    # only `_process_priority_queue`'s own pop.
    motor._maybe_trigger_interactive_pregen = lambda *a, **kw: None

    stop = threading.Event()
    pump = _pump(motor, stop)
    try:
        motor._speak_or_submit(_job_text("FILLER", 1), source="kira-agenda:t1")
        router = motor._speech_router
        assert mixer.entered[0].wait(5.0), "filler never reached playback"

        # A arrives mid-playback: busy-enqueue at priority 0, the tail-call
        # pops it, and generation starts UNDER the filler, blocked in the stub.
        motor.command_queue.put(("process_context", "pregunta A", "hist-a", "ptt"))
        assert a_started.wait(5.0), "A never popped under playback"
        assert motor.is_processing is True

        # B arrives while A generates: the `_processing` early-return must
        # keep it queued -- never a second concurrent generation.
        motor.enqueue("pregunta B", priority=0, source="ptt", history_text="hist-b")
        motor._process_priority_queue()  # immediate return: _processing is True
        assert order == ["hist-a"], "a second generation started while A was in flight"
        assert not b_started.is_set()
        assert [(item[0], item[3]) for item in motor._priority_queue] == [(0, "ptt")], (
            "B must stay queued while A generates"
        )

        # A completes -> the SAME drain loop pops B, strictly after.
        a_release.set()
        assert b_started.wait(5.0), "B never popped after A completed"
        assert order == ["hist-a", "hist-b"], "generation must serialise A then B"

        mixer.release(0)
        # Everything eventually plays to completion: filler + both answers.
        assert _wait_until(lambda: len(router.completed_job_ids()) == 3, timeout=10.0), (
            router.completed_job_ids()
        )
        assert rec.wait_for("speaking_end", 1, timeout=10.0), rec.names()
    finally:
        a_release.set()
        stop.set()
        pump.join(5.0)

    assert motor._priority_queue == []
    assert router._stack == [] and router._active is None


# ──────────────────────────────────────────────────────────────────────────
# (8) batch-2 defect 2 — the pregen trigger yields to a foreground turn
# ──────────────────────────────────────────────────────────────────────────


def test_the_pregen_trigger_yields_to_an_in_flight_foreground_turn(router_motors):
    """step4-plan.md batch-2 audit item 2 (REAL_DEFECT): armed, a widened pop
    dispatches a foreground generation UNDER playback — a state gate 3 of
    `_maybe_trigger_interactive_pregen` predates. Its GPU-free predicate
    (`is_speaking and not llm_generating`) reads True in the whole
    [pop -> `_llm_generating` set] dispatch gap and in every retry gap, so an
    enqueue landing there spawned a pregen worker whose generation ran
    CONCURRENTLY with the foreground one (two Ollama calls on the single
    runner; the delayed foreground call can trip the inference watchdog into
    a spurious heavy-model rollback). Armed, the trigger must ALSO refuse on
    `is_processing` — which spans the foreground turn end to end.
    Construction: the trigger is DARK for A's arrival (so A's pop is a
    guaranteed cache miss and generation runs in the foreground), then the
    REAL trigger is restored before B's enqueue — B's arrival trigger,
    evaluated with A parked at the LLM boundary, is the subject."""
    mixer = _GateMixerMusic(block_at=(0,))
    order: list = []
    a_started = threading.Event()
    a_release = threading.Event()

    def _gen(contexto, source="direct", commit_history=True, history_text=None, **kw):
        order.append(history_text)
        if history_text == "hist-a":
            a_started.set()
            a_release.wait(10.0)  # A parks at the LLM boundary: _processing
            return _job_text("ANSA", 1)  # True, _llm_generating still False —
        return _job_text("ANSB", 1)  # exactly the dispatch-gap state.

    motor, rec, _ = _armed(router_motors, mixer_music=mixer, interrupt_enabled=True)
    motor.is_ready = True
    motor._generar_dialogo = _gen
    # DARK for A's arrival only: without this, A's own arrival trigger
    # (running with `_processing` False — a legitimate pregen window) drafts
    # A and the pop consumes the draft, so no foreground generation ever
    # parks in the gap. Restored (del -> class method) before B's enqueue.
    motor._maybe_trigger_interactive_pregen = lambda *a, **kw: None

    stop = threading.Event()
    pump = _pump(motor, stop)
    try:
        motor._speak_or_submit(_job_text("FILLER", 1), source="kira-agenda:t1")
        router = motor._speech_router
        assert mixer.entered[0].wait(5.0), "filler never reached playback"

        motor.command_queue.put(("process_context", "pregunta A", "hist-a", "ptt"))
        assert a_started.wait(5.0), "A never popped under playback"
        assert motor.is_processing is True

        # The REAL trigger is back — it is the subject from here on.
        del motor._maybe_trigger_interactive_pregen

        # B arrives while A is parked in the gap. `pregenerate()` marks
        # `_pregen_inflight` under `_prefetch_lock` BEFORE spawning (F4), on
        # THIS calling thread — so a spawned draft is visible synchronously.
        motor.enqueue("pregunta B", priority=0, source="ptt", history_text="hist-b")
        with motor._prefetch_lock:
            spawned = motor._pregen_inflight is not None or motor._prefetched_agenda is not None
        assert not spawned, "the pregen trigger spawned a draft while a foreground turn was in flight"
        assert order == ["hist-a"], "a second generation ran concurrently with A's"

        # Release A: its answer plays, and the SAME drain loop pops B after.
        a_release.set()
        assert _wait_until(lambda: order == ["hist-a", "hist-b"], timeout=10.0), order

        mixer.release(0)
        assert _wait_until(lambda: len(router.completed_job_ids()) == 3, timeout=10.0), (
            router.completed_job_ids()
        )
        assert rec.wait_for("speaking_end", 1, timeout=10.0), rec.names()
    finally:
        a_release.set()
        stop.set()
        pump.join(5.0)

    assert motor._priority_queue == []


# ──────────────────────────────────────────────────────────────────────────
# (9) judge-pass BLOCKER — no THINKING flip while Kira audibly speaks
# ──────────────────────────────────────────────────────────────────────────


def test_a_mid_playback_pop_does_not_flip_the_avatar_to_thinking(router_motors):
    """Judge pass (BLOCKER, 2026-08-05): the pop path emitted
    `ui_callback("processing")` unconditionally. Armed, a mid-playback pop
    reached it with `_speech_active` True — ObsRuntime maps "processing" to
    AvatarState.THINKING + `_stop_speaking_alt()`, the D4 inner answer job
    never emits `speaking_start`, and the resumed outer job (`job.started`
    True) never re-emits — so the avatar froze mouth-still on THINKING for
    the rest of the block, live on stream. Mirror of the §11 B3 `idle` gate:
    `processing` must not fire while `_speech_active`. An IDLE pop keeps
    emitting it — the THINKING cue is legacy UX."""
    mixer = _GateMixerMusic(block_at=(0,))
    popped = threading.Event()
    idle_popped = threading.Event()

    def _gen(contexto, source="direct", commit_history=True, history_text=None, **kw):
        (idle_popped if history_text == "h2" else popped).set()
        return "respuesta"

    motor, rec, _ = _armed(router_motors, mixer_music=mixer, interrupt_enabled=True)
    motor.is_ready = True
    motor._generar_dialogo = _gen
    # Batch-1 scope isolation: the pregen trigger independently calls
    # `_generar_dialogo` while speaking — a confound for the event assert.
    motor._maybe_trigger_interactive_pregen = lambda *a, **kw: None

    stop = threading.Event()
    pump = _pump(motor, stop)
    try:
        motor._speak_or_submit(_job_text("FILLER", 1), source="kira-agenda:t1")
        assert mixer.entered[0].wait(5.0), "filler never reached playback"

        motor.command_queue.put(("process_context", "pregunta ptt", "hist-ptt", "ptt"))
        assert popped.wait(5.0), "the mid-playback pop never happened"
        assert "processing" not in rec.names(), (
            "a mid-playback pop must not flip the avatar to THINKING while Kira audibly speaks"
        )

        mixer.release(0)
        assert rec.wait_for("speaking_end", 1, timeout=10.0), rec.names()

        # An IDLE pop still announces THINKING — the legacy cue is untouched.
        motor.enqueue("otra pregunta", priority=0, source="ptt", history_text="h2")
        motor._process_priority_queue()
        assert idle_popped.wait(5.0), "the idle pop never ran"
        assert "processing" in rec.names(), "the idle pop must keep the legacy THINKING cue"
    finally:
        stop.set()
        pump.join(5.0)


# ──────────────────────────────────────────────────────────────────────────
# (10) judge-pass pin — the bundler absorbs a queued direct question
# ──────────────────────────────────────────────────────────────────────────


def test_the_mid_playback_bundle_absorbs_a_queued_direct_question(router_motors):
    """PIN (judge pass, convergent finding): the widened gate restricts the
    SELECTION to a priority-0 head, but the pop-time bundler (§4 step 10)
    then absorbs the contiguous OWNER-source prefix regardless of priority —
    a queued priority-1 `direct` question rides along. DELIBERATE: the press
    already justified the single D3 preemption, the burst is answered in ONE
    request (Rule 2's goal), nothing is dropped or reordered — the typed
    question simply arrives sooner. This pins the selection-vs-removal
    distinction the §5.2 gate comment documents."""
    mixer = _GateMixerMusic(block_at=(0,))
    gen_calls: list = []
    gen_fired = threading.Event()

    def _gen(contexto, source="direct", commit_history=True, history_text=None, **kw):
        gen_calls.append(source)
        gen_fired.set()
        return "respuesta combinada"

    motor, rec, _ = _armed(router_motors, mixer_music=mixer, interrupt_enabled=True)
    motor.is_ready = True
    motor._generar_dialogo = _gen
    motor._maybe_trigger_interactive_pregen = lambda *a, **kw: None

    stop = threading.Event()
    pump = _pump(motor, stop)
    try:
        motor._speak_or_submit(_job_text("FILLER", 1), source="kira-agenda:t1")
        assert mixer.entered[0].wait(5.0), "filler never reached playback"

        motor.enqueue("pregunta escrita", priority=1, source="direct", history_text="h-d")
        motor.enqueue("pregunta ptt", priority=0, source="ptt", history_text="h-p")

        worker = threading.Thread(target=motor._process_priority_queue, daemon=True)
        worker.start()
        assert gen_fired.wait(5.0), "the priority-0 head never popped under playback"
        worker.join(5.0)
        assert not worker.is_alive()

        assert len(gen_calls) == 1, f"the burst must be ONE request, got {gen_calls}"
        assert motor._priority_queue == [], (
            "the direct question must ride the bundle, not wait for the boundary"
        )

        mixer.release(0)
        assert rec.wait_for("speaking_end", 1, timeout=10.0), rec.names()
    finally:
        stop.set()
        pump.join(5.0)


# ──────────────────────────────────────────────────────────────────────────
# (11) judge-pass pin — the OTHER kill-switch half
# ──────────────────────────────────────────────────────────────────────────


def test_widened_pop_is_inert_with_the_router_switch_off(router_motors):
    """PIN (judge NIT): the other half of the conjunction. Router OFF +
    interrupt ON must keep the legacy combined guard — this exact
    combination was a step-3 MAJOR (router-off deleted legacy turns), so it
    gets its own pin even though the predicate is a symmetric `and`. Legacy
    playback here is the REAL blocking `_hablar` on a side thread (no
    router exists to hold `_speech_active`) — no hand-set state."""
    mixer = _GateMixerMusic(block_at=(0,))
    generated: list = []

    motor, rec, _ = _armed(
        router_motors, mixer_music=mixer, enabled=False, interrupt_enabled=True
    )
    motor.is_ready = True
    motor._generar_dialogo = lambda *a, **kw: generated.append(a) or "sin uso"
    motor._maybe_trigger_interactive_pregen = lambda *a, **kw: None

    speaker = threading.Thread(
        target=motor._speak_or_submit,
        args=(_job_text("FILLER", 1),),
        kwargs={"source": "kira-agenda:t1"},
        daemon=True,
    )
    speaker.start()
    assert mixer.entered[0].wait(5.0), "legacy playback never reached the mixer"

    motor.enqueue("pregunta directa", priority=0, source="ptt", history_text="h")
    motor._process_priority_queue()

    assert generated == [], "a priority-0 item popped mid-playback with the router switch off"
    assert [item[3] for item in motor._priority_queue] == ["ptt"]

    mixer.release(0)
    speaker.join(10.0)
    assert not speaker.is_alive(), "the legacy speaker thread never finished"
