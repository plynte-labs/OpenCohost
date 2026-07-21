"""Fase 1 (no-dead-air): engine-level prefetch invalidation.

`prefetch_pending()` distinguishes "worker still generating N+1" (keep waiting)
from "worker finished with nothing" (fall back to a fresh generation). Model and
profile switches must invalidate any cached draft — a draft built under the old
tier/persona must never reach TTS (staleness class #344).
"""

import queue
import threading

from opencohost.core.llm_engine import MotorVocalIA


def _motor():
    return MotorVocalIA(queue.Queue(), lambda event: None)


def test_prefetch_pending_reflects_worker_lifecycle():
    motor = _motor()

    # No worker at all -> not pending.
    assert motor.prefetch_pending() is False

    # A live worker with no draft yet -> pending (keep waiting).
    gate = threading.Event()
    worker = threading.Thread(target=gate.wait, daemon=True)
    worker.start()
    motor._prefetch_thread = worker
    motor._prefetched_agenda = None
    assert motor.prefetch_pending() is True

    # Draft landed while the worker is still alive -> ready, not pending.
    motor._prefetched_agenda = {"dialogo": "x"}
    assert motor.prefetch_pending() is False

    # Worker finished with nothing -> not pending (fell back).
    gate.set()
    worker.join(timeout=1.0)
    motor._prefetched_agenda = None
    assert motor.prefetch_pending() is False


def test_set_profile_clears_prefetched_agenda():
    motor = _motor()
    motor._prefetched_agenda = {"dialogo": "stale persona line"}

    motor._dispatch_command(
        "set_profile",
        {"prompt": "nueva persona", "use_system": False, "_profile_name": "Test"},
    )

    assert motor._prefetched_agenda is None, "a draft from the old persona must not survive"


def test_switch_llm_tier_clears_prefetched_agenda():
    motor = _motor()
    motor._prefetched_agenda = {"dialogo": "stale tier line"}

    motor._dispatch_command("switch_llm_tier", "balanced")

    assert motor._prefetched_agenda is None, "a draft from the old tier must not survive"
