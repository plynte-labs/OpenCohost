"""R4 — chaos stream: concurrent flood of RANDOM short prompts, real pipeline.

Drives genuine concurrent processing: producer threads call the real, _pq_lock-
guarded ``enqueue`` while a single drain thread runs the real
``_process_priority_queue`` -> ``_ejecutar_inferencia`` -> real ``_generar_dialogo``
(real Ollama). Only TTS is stubbed (``_hablar`` -> no-op). Asserts no queue /
accumulation-buffer corruption after real concurrent inference.

Concurrency / timeout bounds (kept deliberately small):
  - Model: gemma4:e2b (fast); ``LLM_MAX_TOKENS`` monkeypatched to 24 and the
    reasoning cache forced False so each real generation stays short (~1-2s).
  - 3 producer threads x 2 enqueues each = 6 prompts (mixed priority/source).
  - 1 drain thread polling at 10ms. Total workers = 4 (within the 3-5 budget).
  - ``_pq_max_items`` = 5 (default) -> at most 1 prompt overflows to accumulation.
  - Hard timeout: the whole orchestration is wrapped in run_bounded(seconds=90).
    ~6 short gens serialized through one drain (+ first-load) is well under 90s.
  - Default ``_pq_ttl_seconds`` (30s) is kept so fresh items are not TTL-dropped
    before they are really processed.

Invariants are sampled DURING the concurrent phase (in the drain loop) and again
after every thread joins; any violation is recorded and fails the test.
"""
from __future__ import annotations

import queue
import random
import threading
import time

import pytest

from tests.realenv._helpers import require_model, run_bounded

pytestmark = pytest.mark.realenv

MODEL = "gemma4:e2b"
PROMPTS = ("hola", "que tal", "decime algo", "contame un chiste corto", "buenas", "como va")
PRIO_SOURCES = ((0, "ptt"), (1, "chat"), (2, "kira-agenda"))


def _queue_item_ok(item) -> bool:
    return (
        isinstance(item, tuple)
        and len(item) == 4
        and isinstance(item[0], int)
        and isinstance(item[1], float)
        and isinstance(item[2], str)
        and isinstance(item[3], str)
    )


def test_chaos_stream_no_state_corruption(monkeypatch):
    require_model(MODEL)
    import ollama
    import opencohost.core.llm_engine as llm_engine
    from opencohost.core.llm_engine import MotorVocalIA

    # Keep real generations short and capped (gemma is not flagged as reasoning here).
    monkeypatch.setattr(llm_engine, "LLM_MAX_TOKENS", 24)

    m = MotorVocalIA(queue.Queue(), lambda *a, **k: None)
    m.ollama = ollama
    m._ollama_chat_client = None
    m.is_ready = True
    m.current_model = MODEL
    m._last_known_good_model = MODEL
    m._reasoning_model_cache = {MODEL: False}   # keep num_predict cap -> short gens
    m._hablar = lambda *a, **k: None            # no real TTS
    m._pq_max_items = 5

    errors: list = []   # (where, exc) — any thread error or sampled invariant break
    stop = threading.Event()

    def _check_invariants():
        with m._pq_lock:
            q_snap = list(m._priority_queue)
        if len(q_snap) > m._pq_max_items:
            errors.append(("queue-len", AssertionError(f"{len(q_snap)} > {m._pq_max_items}")))
        for item in q_snap:
            if not _queue_item_ok(item):
                errors.append(("queue-shape", AssertionError(f"malformed: {item!r}")))
        with m._accum_lock:
            a_snap = list(m._accumulation_buffer)
        if len(a_snap) > m._accum_max_items:
            errors.append(("accum-len", AssertionError(f"{len(a_snap)} > {m._accum_max_items}")))
        if sum(len(p) for _, p, _ in a_snap) > m._accum_max_chars:
            errors.append(("accum-chars", AssertionError("accumulation chars over limit")))

    def producer(idx: int):
        try:
            for _ in range(2):
                prio, source = random.choice(PRIO_SOURCES)
                m.enqueue(random.choice(PROMPTS), priority=prio, source=source)
                time.sleep(random.uniform(0.0, 0.05))
        except BaseException as exc:  # noqa: BLE001
            errors.append((f"producer-{idx}", exc))

    def drain():
        try:
            while not stop.is_set():
                _check_invariants()
                m._process_priority_queue()
                time.sleep(0.01)
        except BaseException as exc:  # noqa: BLE001
            errors.append(("drain", exc))

    def orchestrate():
        drain_thread = threading.Thread(target=drain, name="chaos-drain", daemon=True)
        drain_thread.start()
        producers = [
            threading.Thread(target=producer, args=(i,), name=f"chaos-prod-{i}", daemon=True)
            for i in range(3)
        ]
        for p in producers:
            p.start()
        for p in producers:
            p.join(30)
        # Wait (bounded) for the queue to drain and the motor to go idle.
        deadline = time.time() + 60
        while time.time() < deadline:
            with m._pq_lock:
                queue_empty = not m._priority_queue
            if queue_empty and not m.is_processing:
                break
            time.sleep(0.05)
        stop.set()
        drain_thread.join(10)

    run_bounded(orchestrate, seconds=90)

    # No producer / drain thread crashed and no sampled invariant was violated.
    assert not errors, f"chaos run produced errors: {errors}"

    # At least one REAL generation actually committed (proves inference ran end to
    # end, not just enqueue): a committed turn appends user + assistant.
    assert len(m.historial) >= 2

    # Final state integrity after genuine concurrent real inference.
    with m._pq_lock:
        final_queue = list(m._priority_queue)
    assert len(final_queue) <= m._pq_max_items
    for item in final_queue:
        assert _queue_item_ok(item), f"malformed final queue item: {item!r}"
    with m._accum_lock:
        final_accum = list(m._accumulation_buffer)
    assert len(final_accum) <= m._accum_max_items
    assert sum(len(p) for _, p, _ in final_accum) <= m._accum_max_chars

    assert m.is_processing is False
    assert m.is_speaking is False
