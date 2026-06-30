"""R3 — heavy-model recovery via a REAL inference watchdog trip.

Proves the watchdog interrupts a genuine in-flight Ollama generation (not a
cooperative threading.Event like the unit suite) and that recovery fires.

Flow under test (all real except TTS, which never runs on this path):
  _generar_dialogo -> _ollama_chat_with_watchdog (real ollama.chat in a daemon
  thread) -> done.wait(max(0.1, timeout)) misses -> TimeoutError
  "watchdog_timeout:..." -> _is_watchdog_timeout_error -> _recover_from_stalled_
  inference (sets _last_llm_failure reason, fires ui_callback, rolls back) -> "".

Bounds:
  - Fast model: gemma4:e2b (~5-8s/gen, >> the 0.1s watchdog floor).
  - Watchdog timeout 0.05s -> clamped to 0.1s; any real HTTP+inference round trip
    exceeds it, so the trip is deterministic.
  - _last_known_good_model == current_model makes rollback a no-op (no 2nd load).
  - Hard timeout: 30s for the whole call (one real ollama.show + ~0.1s wait).
  - The orphaned generation thread is daemon and never joined -> cannot hang.
"""
from __future__ import annotations

import queue

import pytest

from tests.realenv._helpers import require_model, run_bounded

pytestmark = pytest.mark.realenv

MODEL = "gemma4:e2b"


def test_watchdog_interrupts_real_generation_and_recovery_fires():
    require_model(MODEL)
    import ollama
    from opencohost.core.llm_engine import MotorVocalIA

    ui_events: list[str] = []
    m = MotorVocalIA(queue.Queue(), ui_events.append)  # full __init__

    m.ollama = ollama
    m._ollama_chat_client = None
    m.is_ready = True
    m.current_model = MODEL
    m._last_known_good_model = MODEL          # rollback no-ops -> no 2nd model load
    m._awaiting_first_success_after_switch = False
    m._inference_watchdog_timeout = 0.05      # clamped to 0.1s; real gen >> 0.1s

    result = run_bounded(
        lambda: m._generar_dialogo("decime hola", source="direct", commit_history=False),
        seconds=30,
    )

    # The real generation was interrupted: empty result, recovery recorded + signalled.
    assert result == ""
    assert m._last_llm_failure is not None
    assert m._last_llm_failure["reason"] == "watchdog_timeout"
    assert "llm_timeout_recovered" in ui_events
