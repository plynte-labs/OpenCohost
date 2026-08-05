"""Step 0 of `interruptible_speech_architecture_20260804` — the accumulated
turn must not run while `_pq_lock` is held.

Measured defect (design §2b, re-verified against the live tree 2026-08-04):
`_process_priority_queue` opened `with self._pq_lock:` at :2663 and ran a FULL
generate-and-speak turn inside it — `_ejecutar_inferencia(accumulated, ...)` at
:2674 — with `_complete_processing_cycle(process_queue=False)` in the `finally`
at :2676, still inside the same block. That cycle calls
`_drain_pending_direct_into_priority_queue` (:2774), which takes
`_direct_drain_lock` (:2885) and then `self.enqueue(...)` (:2906), whose body
opens `with self._pq_lock:` (:1754). `_pq_lock` is a plain `threading.Lock()`
(:744) — NOT reentrant — so the engine thread blocked on a lock it already held
and Kira went permanently silent with no error.

Two distinct hangs, one root cause, one test each:
  (a) single thread — the engine's own `finally` re-enters `enqueue()`;
  (b) two threads (ABBA) — the HTTP arrival drain
      (`api/routers/chat.py:184-191`) takes `_direct_drain_lock` then blocks on
      `_pq_lock`, while the engine thread holds `_pq_lock` and blocks on
      `_direct_drain_lock`. `POST /api/chat/turn` never returns.

Reachable in production: the only writer of `_accumulation_buffer` is
`enqueue()`'s overflow path (:1762-1765) and `_pq_max_items` is 5 (:745), so a
saturated session crosses it routinely.

Convention mirrors tests/test_direct_turn_preemption.py: a constructed-but-
unstarted motor with `_ejecutar_inferencia` faked so no Ollama/TTS runs, and
every cross-thread sequence driven by `threading.Event` — never `time.sleep`.
A hung engine must FAIL these tests, not hang the suite, so every worker is a
daemon thread joined with a timeout and asserted dead afterwards.
"""

import queue
import threading

from opencohost.core.llm_engine import MotorVocalIA


def _bare_motor() -> MotorVocalIA:
    # Hermetic (test_direct_turn_preemption.py:_bare_motor): pin the model and
    # seed both caches so nothing reaches the live ollama.show probe.
    motor = MotorVocalIA(queue.Queue(), lambda event: None)
    motor.current_model = "llama3"
    motor._reasoning_model_cache["llama3"] = False
    motor._model_ctx_limit = {"llama3": 8192}
    return motor


class _EnteredLock:
    """threading.Lock wrapper that reports the moment it is HELD.

    Duck-typed context manager, swapped in for `_direct_drain_lock`. Makes the
    ABBA staging deterministic: once `entered` is set the drain thread provably
    owns that lock, so releasing the engine thread from its inference lands it
    on a lock the drain will not give back until it gets `_pq_lock` — which is
    exactly the cycle, with no sleep anywhere.
    """

    def __init__(self):
        self._inner = threading.Lock()
        self.entered = threading.Event()

    def __enter__(self):
        self._inner.acquire()
        self.entered.set()
        return self

    def __exit__(self, *exc):
        self._inner.release()
        return False


def test_accumulated_flush_with_a_direct_pending_does_not_self_deadlock():
    """(a) One thread. The `finally`'s drain finds a `direct` command at the
    front of `command_queue`, calls `enqueue()`, and re-acquires the
    non-reentrant `_pq_lock` the same thread already holds."""
    motor = _bare_motor()
    answered: list = []
    motor._ejecutar_inferencia = lambda payload, **kw: answered.append((payload, kw.get("source")))

    motor.enqueue_accumulation("mensaje desbordado", source="chat")
    motor.command_queue.put(("process_context", "pregunta directa", "hist", "direct"))

    worker = threading.Thread(target=motor._process_priority_queue, daemon=True)
    worker.start()
    worker.join(5.0)

    assert not worker.is_alive(), (
        "the engine thread never returned — _pq_lock is still held across "
        "_ejecutar_inferencia and its finally re-enters enqueue()"
    )
    assert [source for _payload, source in answered] == ["accumulated"]
    # The drain still did its job: the queued question is now visible to the
    # scheduler instead of stranded behind a hang.
    assert [item[2] for item in motor._priority_queue] == ["pregunta directa"]


def test_arrival_drain_does_not_deadlock_against_an_accumulated_turn():
    """(b) Two threads, opposite lock orders. The HTTP arrival drain holds
    `_direct_drain_lock` and wants `_pq_lock`; the engine thread holds
    `_pq_lock` and wants `_direct_drain_lock`."""
    motor = _bare_motor()
    drain_lock = _EnteredLock()
    motor._direct_drain_lock = drain_lock

    inference_entered = threading.Event()
    release_inference = threading.Event()

    def _fake_inferencia(payload, **kw):
        inference_entered.set()
        assert release_inference.wait(5.0), "the test never released the inference"

    motor._ejecutar_inferencia = _fake_inferencia
    motor.enqueue_accumulation("mensaje desbordado", source="chat")

    engine = threading.Thread(target=motor._process_priority_queue, daemon=True)
    engine.start()
    assert inference_entered.wait(5.0), "the accumulated turn never started"

    # A typed question arrives mid-turn: the chat router drains it on the HTTP
    # thread (api/routers/chat.py:184-191) because the motor is busy.
    motor.command_queue.put(("process_context", "pregunta directa", "hist", "direct"))
    http = threading.Thread(target=motor._drain_pending_direct_into_priority_queue, daemon=True)
    http.start()
    assert drain_lock.entered.wait(5.0), "the arrival drain never took _direct_drain_lock"

    release_inference.set()
    engine.join(5.0)
    http.join(5.0)

    assert not engine.is_alive(), (
        "the engine thread hung on _direct_drain_lock while holding _pq_lock"
    )
    assert not http.is_alive(), (
        "POST /api/chat/turn never returned — the arrival drain hung on _pq_lock"
    )
    assert [item[2] for item in motor._priority_queue] == ["pregunta directa"]
