"""WU1 (agenda_no_dead_air fase 2, design-fase2.md §3): deterministic RED
integration test pinning the speech-overlap race (design.md §10, Judge A's
residual finding; design-fase2.md §2).

On Fase 1 code, the REAL API-host consume path — `AgendaDriver.tick_once()`
-> `_maybe_consume_prefetch()` -- calls `play_prefetched_agenda()`, which
spawns its own speaker thread that calls `_hablar` directly. That races the
engine worker's `_process_priority_queue` -> `_processing=True` boundary
(llm_engine.py:_process_priority_queue, ~1110-1125): between the moment an
item is popped off `_priority_queue` (pq_lock released) and the moment
`_processing` is set True, a concurrent prefetch consume can see the motor as
"clear to speak" and start its own `_hablar` call — two threads can end up
inside `_hablar` (and the shared TTS/audio pipeline) at once.

WU2 re-routes THIS SAME API-host consume path (`_maybe_consume_prefetch`)
through the single queue+worker instead of `play_prefetched_agenda` (pop-time
cache hit, design-fase2.md §2.2). It does NOT delete `play_prefetched_agenda`
-- that stays as CTK's legacy consume path, untouched (design-fase2.md §2.1
[v2], ADR-036 §2.1). This test therefore drives a real `KiraAgendaController`
+ `AgendaDriver` and consumes via `driver.tick_once()`, never poking the
engine's `_prefetched_agenda` cache or calling `play_prefetched_agenda()`
directly — so it keeps gating the real API-host entry point both before and
after WU2 lands, instead of pinning a call path WU2 leaves untouched.

This test MUST fail (max simultaneous `_hablar` occupancy reaches 2) until
WU2 lands — see the `xfail(strict=True)` marker below. WU2 removes the
marker.

HARNESS-FAILURE audit note: every setup/scaffolding check below raises
`RuntimeError("HARNESS-FAILURE: ...")` instead of asserting. The ONLY
`AssertionError` this test can produce is the final occupancy check at the
bottom. So an xfail whose longrepr is a HARNESS-FAILURE `RuntimeError` means
the TEST HARNESS is broken, not that the race was reproduced — audit with
`--runxfail` and check the exception type before trusting a raw-red run.
"""

import queue
import threading
from unittest.mock import MagicMock

import pytest

from opencohost.api.agenda_driver import AgendaDriver, route_motor_event_to_agenda
from opencohost.core import llm_engine
from opencohost.core.llm_engine import MotorVocalIA
from opencohost.smart_aggregator.kira_agenda_controller import KiraAgendaController


class _FakeMusic:
    """No-op pygame.mixer.music stand-in: get_busy() False means the
    consumer loop's busy-wait never spins, so playback is instant."""

    def load(self, path):
        pass

    def play(self):
        pass

    def get_busy(self):
        return False

    def unload(self):
        pass


def _fast_hablar_motor(voz_referencia_path) -> MotorVocalIA:
    """A motor wired so `_hablar` runs its full real body fast and without
    real audio: heavy-TTS path (motor_tts="pesado" + a real reference file)
    with pygame mocked out — same convention as
    tests/test_llm_engine_timeouts.py's heavy-TTS tests. The actual network
    call (`requests.post`) is gated per-test via an Event, not mocked away
    unconditionally, so the test can hold a call "inside" `_hablar`."""
    motor = MotorVocalIA(queue.Queue(), lambda event: None)
    motor.motor_tts = "pesado"
    motor.voz_referencia = str(voz_referencia_path)
    motor.pygame = MagicMock()
    motor.pygame.mixer.music = _FakeMusic()
    return motor


def _driver(controller, motor, agenda_lock, tick_seconds=4.5):
    """Mirrors tests/test_agenda_driver.py's `_driver` helper."""
    return AgendaDriver(
        get_agenda=lambda: controller,
        get_motor=lambda: motor,
        agenda_lock=agenda_lock,
        tick_seconds=tick_seconds,
    )


def _controller_with_queued_topic(**kwargs):
    """Mirrors tests/test_agenda_driver.py's `_controller_with_queued_topic`."""
    controller = KiraAgendaController(**kwargs)
    topic = controller.add_topic("Tema uno", "angulo", approved=True)
    controller.queue_topic(topic.id)
    return controller, topic


@pytest.mark.xfail(
    strict=True,
    reason="WU2 pending: speech serialization — the race is real on Fase 1 code (design-fase2.md §3 WU1)",
)
def test_prefetch_speaker_and_worker_never_speak_concurrently(tmp_path, monkeypatch):
    """Pins Judge A's interleaving through the REAL API-host consume path:
    a cached agenda draft, consumed via `AgendaDriver.tick_once()` (today
    calling `play_prefetched_agenda()` under the hood), while the worker is
    mid-dispatch on a higher-priority (PTT) item, must never overlap inside
    `_hablar`.

    Correctness assertion: max simultaneous `_hablar` occupancy == 1.
    MUST FAIL today (occupancy reaches 2) — that IS the red this test
    proves; WU2 makes it green and removes the xfail marker.
    """
    ref = tmp_path / "voice.wav"
    ref.write_bytes(b"ref")
    motor = _fast_hablar_motor(ref)

    # --- Concurrency instrumentation: entry/exit counter under a lock,
    # recording max simultaneous occupancy of _hablar. ---
    occupancy_lock = threading.Lock()
    state = {"current": 0, "max": 0}
    entered_once = threading.Event()  # first _hablar call is now inside
    reached_two = threading.Event()  # a second call entered concurrently
    all_done = threading.Event()  # occupancy returned to zero

    original_hablar = motor._hablar  # bound method, real class implementation

    def instrumented_hablar(texto, source="direct"):
        with occupancy_lock:
            state["current"] += 1
            state["max"] = max(state["max"], state["current"])
            occ = state["current"]
        entered_once.set()
        if occ >= 2:
            reached_two.set()
        try:
            return original_hablar(texto, source=source)
        finally:
            with occupancy_lock:
                state["current"] -= 1
                done = state["current"] == 0
            if done:
                all_done.set()

    motor._hablar = instrumented_hablar

    # --- Event-gated fake playback: both calls share the heavy-TTS path
    # (motor_tts="pesado"), so gating `requests.post` holds each call
    # "inside" _hablar deterministically — no sleeps, no real network/audio.
    release_tts = threading.Event()

    def fake_post(url, json=None, timeout=None):
        if not release_tts.wait(timeout=5.0):
            raise RuntimeError("HARNESS-FAILURE: release_tts never fired")
        return MagicMock(status_code=200, content=b"wav")

    monkeypatch.setattr(llm_engine.requests, "post", fake_post)

    # Both the PTT item's foreground generation and the agenda draft's
    # background pregeneration go through this same patched seam (rename-
    # proof for WU2: whichever call site invokes _generar_dialogo next still
    # gets text instantly) — only the pop->processing interleaving is under
    # test, not generation latency.
    motor._generar_dialogo = lambda *a, **kw: "Respuesta suficientemente larga para hablar."

    # --- Arm the prefetch draft through the REAL API-host path: a real
    # controller + driver, driven exactly like tests/test_agenda_driver.py's
    # `_arm_prefetch` convention — never poking `_prefetched_agenda` directly.
    agenda_lock = threading.Lock()
    controller, _topic = _controller_with_queued_topic()
    controller.enable()
    driver = _driver(controller, motor, agenda_lock)

    driver.tick_once()  # open topic -> GENERATING, replace_pending turn N
    route_motor_event_to_agenda(controller, "speaking_start")  # -> SPEAKING
    # current_speech_source is a read-only property (no public setter); this
    # mirrors what _hablar itself writes under the hood (llm_engine.py:3149).
    motor._current_speech_source = "kira-agenda"
    driver.maybe_start_prefetch(controller, motor)
    if driver._prefetch is None:
        raise RuntimeError("HARNESS-FAILURE: maybe_start_prefetch did not arm the driver stash")
    if not motor.wait_prefetched_agenda(timeout=5.0):
        raise RuntimeError("HARNESS-FAILURE: prefetch draft never became ready")
    route_motor_event_to_agenda(controller, "speaking_end")  # -> WAITING_SIGNAL

    # Step 2: enqueue a priority-0 (PTT) item.
    motor.enqueue("mensaje del streamer", priority=0, source="ptt")

    # Step 3: pin the pop->processing boundary open with the test-only hook
    # (llm_engine.py __init__: _test_pop_boundary_hook).
    hook_entered = threading.Event()
    pop_boundary_release = threading.Event()

    def pop_boundary_hook():
        if motor.is_processing is not False:
            raise RuntimeError(
                "HARNESS-FAILURE: hook fired outside the pop→processing window"
            )
        hook_entered.set()
        pop_boundary_release.wait(timeout=5.0)

    motor._test_pop_boundary_hook = pop_boundary_hook

    worker_thread = threading.Thread(target=motor._process_priority_queue, daemon=True)

    try:
        # Worker pops the PTT item and blocks in the hook: pop() is done
        # (pq_lock released), _processing is still False.
        worker_thread.start()
        if not hook_entered.wait(timeout=5.0):
            raise RuntimeError("HARNESS-FAILURE: worker never reached the pop boundary hook")

        # Step 4: while the boundary window is held open, consume the
        # prefetched draft through the driver's REAL API-host consume path —
        # exactly what EngineHost/AgendaDriver would do once it observes both
        # guards clear. Reproduces Judge A's interleaving. TODAY
        # `_maybe_consume_prefetch` calls `play_prefetched_agenda()` (a
        # background speaker thread that enters `_hablar` independently of
        # the boundary release, below); post-WU2 it enqueues instead (no
        # immediate speaker — the worker alone ends up speaking).
        driver.tick_once()
        if driver._prefetch is not None:
            raise RuntimeError(
                "HARNESS-FAILURE: driver did not consume the stashed prefetch at the held boundary"
            )

        # Step 5: release the boundary — the worker proceeds to
        # _processing=True -> _ejecutar_inferencia (patched _generar_dialogo)
        # -> _hablar. Check readiness AFTER releasing: only past this point
        # is entry into _hablar guaranteed in BOTH worlds (today: the
        # prefetch speaker thread may already be inside, spawned above;
        # post-WU2: nothing entered yet until the worker's own dispatch runs).
        pop_boundary_release.set()
        if not entered_once.wait(timeout=5.0):
            raise RuntimeError("HARNESS-FAILURE: nobody entered _hablar after the boundary released")

        # `reached_two` is a BOUNDED SYNC AID, not a correctness assertion —
        # on a serialized (post-WU2) engine it never fires. `release_tts`
        # fires unconditionally right after, regardless of outcome, so the
        # choreography terminates in both worlds: on Fase 1 code the
        # overlapping pair unblocks and drains; post-WU2 the lone pinned
        # thread(s) unblock and drain the same way.
        overlapped = reached_two.wait(timeout=2.0)  # noqa: F841 (diagnostic only)
        release_tts.set()

        if not all_done.wait(timeout=5.0):
            raise RuntimeError("HARNESS-FAILURE: hablar occupancy never returned to zero")
    finally:
        # Release first (unblocks anything still parked), THEN join — patches
        # (requests.post) are restored automatically by monkeypatch teardown.
        pop_boundary_release.set()
        release_tts.set()
        worker_thread.join(timeout=5.0)
        if worker_thread.is_alive():
            raise RuntimeError("HARNESS-FAILURE: worker thread leaked")

    # Step 6: the correctness assertion — the SOLE AssertionError this test
    # can raise. Passes only once WU2 removes the parallel speaker call from
    # the API-host consume path; today occupancy reaches 2.
    assert state["max"] == 1, (
        "expected at most one thread inside _hablar at a time, observed "
        f"max simultaneous occupancy={state['max']}"
    )
