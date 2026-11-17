"""Process-wide decision runtime (laya-decision-layer-20261009).

Owns the one ``DecisionBus`` of the process and the Laya worker client behind
it. The API lifespan calls ``start()``/``stop()``; the engine and the chat
aggregator only call ``get_bus()``/``shadow_respond()``. Until ``start()``
installs a configured worker (an ONNX export, or ``OPENCOHOST_LAYA_PYTHON`` for
the torch engine), the bus has no
backend and every turn takes the rule path, which is also what the legacy
CTk surface gets.
"""
from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from opencohost.config import settings
from opencohost.core.decision.bus import DecisionBus
from opencohost.core.decision.types import RESPOND

logger = logging.getLogger("OpenCohost")

_lock = threading.Lock()
_bus = DecisionBus(None)
_client = None
_respond_pool: Optional[ThreadPoolExecutor] = None
_respond_slot = threading.Semaphore(1)


def get_bus() -> DecisionBus:
    return _bus


def get_client():
    return _client


def install_backend(backend) -> None:
    """Swap the bus backend (tests, ``start``). ``None`` makes the bus unavailable."""
    global _bus
    with _lock:
        _bus = DecisionBus(backend)


def start(client_factory=None):
    """Create the Laya worker client when configured and start it in the
    background. Never blocks app startup and never raises."""
    global _client
    try:
        if client_factory is None:
            from opencohost.core.decision.laya_client import LayaWorkerClient

            def client_factory():
                return LayaWorkerClient(
                    settings.LAYA_PYTHON,
                    checkpoint=settings.LAYA_CHECKPOINT,
                    engine=settings.LAYA_ENGINE,
                    onnx_dir=settings.LAYA_ONNX_DIR,
                )

        client = client_factory()
    except Exception as exc:
        logger.warning("[DECISION] worker client unavailable: %s", type(exc).__name__)
        return None
    with _lock:
        _client = client
    if client.status().get("configured"):
        client.start()
        install_backend(client)
    else:
        install_backend(None)
        logger.info("[DECISION] Laya worker not configured; rules act")
    return client


def stop() -> None:
    """Stop the worker and the respond shadow pool. Idempotent, never raises."""
    global _client, _respond_pool
    with _lock:
        client, _client = _client, None
        pool, _respond_pool = _respond_pool, None
    install_backend(None)
    if client is not None:
        try:
            client.stop()
        except Exception:
            pass
    if pool is not None:
        pool.shutdown(wait=False, cancel_futures=True)


def worker_status() -> dict:
    client = _client
    if client is None:
        return {
            "configured": False, "state": "unavailable", "checkpoint": None, "last_error": None,
            "engine": settings.LAYA_ENGINE,
        }
    try:
        return client.status()
    except Exception:
        return {
            "configured": True, "state": "error", "checkpoint": None, "last_error": "status_failed",
            "engine": settings.LAYA_ENGINE,
        }


def restart_worker() -> dict:
    client = _client
    if client is not None and client.status().get("configured"):
        client.restart()
        install_backend(client)
    return worker_status()


def _run_respond(state: str, rule_value: str, on_result=None) -> None:
    result = None
    try:
        turn = _bus.decide(state, "chat", ids=(RESPOND,))
        if turn is not None:
            outcome = turn.resolve(RESPOND, rule_value)
            if outcome is not None and outcome.laya_value is not None and outcome.confidence is not None:
                result = (outcome.laya_value, outcome.confidence)
    except Exception:
        logger.debug("respond shadow failed", exc_info=True)
    finally:
        _respond_slot.release()
    if on_result is not None:
        # Always called once the decision finished, with None when Laya gave no
        # usable answer (timeout, error, unavailable), so a waiting consumer
        # can fail open. The slot is already free; the callback never blocks it.
        try:
            on_result(*(result or (None, None)))
        except Exception:
            logger.debug("respond result hook failed", exc_info=True)


def shadow_respond(state, rule_value: str, on_result=None) -> bool:
    """Fire-and-forget shadow ``respond`` decision for one viewer message.

    Returns at once: nothing is submitted while the bus has no backend, and a
    message that arrives while the previous one is still in flight is dropped,
    so a chat flood never queues work or blocks the aggregator. True when a
    decision was submitted. ``on_result(label, confidence)`` runs on the
    decision thread after it finishes (``None, None`` without a usable answer);
    it is not called when nothing was submitted.
    """
    global _respond_pool
    if _bus.backend is None or not isinstance(state, str) or not state.strip():
        return False
    if not _respond_slot.acquire(blocking=False):
        return False
    try:
        with _lock:
            if _respond_pool is None:
                _respond_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="decision-respond")
            pool = _respond_pool
        pool.submit(_run_respond, state, rule_value, on_result)
        return True
    except Exception:
        _respond_slot.release()
        return False


def _reset_for_tests() -> None:
    stop()
    install_backend(None)
