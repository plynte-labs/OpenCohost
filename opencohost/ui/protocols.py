"""
Callback protocols for VoiceAI inter-module communication.

Defines type-safe Protocol classes for all module-to-UI callbacks and a
CallbackDispatcher that replaces silent ``try/except: pass`` with proper
error logging and multi-subscriber support.
"""

import inspect
import logging
from typing import Any, Callable, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────
# Protocol definitions
# ──────────────────────────────────────────────


@runtime_checkable
class MotorEventCallback(Protocol):
    """Callbacks from MotorVocalIA (LLM/TTS engine) to UI.

    The engine sends string status codes through a single callback.
    Known status values:
        ready, ollama_unavailable, processing, idle,
        speaking_start, speaking_end, model_changed,
        download_start, download_done, download_error
    """

    def __call__(self, status: str) -> None: ...


@runtime_checkable
class SmartAggregatorCallbacks(Protocol):
    """Callbacks from Smart Aggregator to UI."""

    def on_filtered_message(self, message: dict) -> None: ...
    def on_vibe_update(self, vibe: dict) -> None: ...
    def on_activity_trigger(self, data: dict) -> None: ...
    def on_aggregated_context(self, data: dict) -> None: ...
    def on_source_error(self, error: str) -> None: ...
    def on_source_connect(self, info: dict) -> None: ...
    def on_source_disconnect(self) -> None: ...


@runtime_checkable
class StreamAdminCallbacks(Protocol):
    """Callbacks from Stream Admin to UI."""

    def on_log(self, msg: str) -> None: ...
    def on_state(self, state: dict) -> None: ...
    def on_metadata(self, metadata: dict) -> None: ...
    def on_pending_action(self, pending: dict) -> None: ...
    def on_analytics(self, snapshot: dict) -> None: ...


@runtime_checkable
class ProfileSaveCallback(Protocol):
    """Callback from profile editor window to main app."""

    def __call__(self, nuevos_perfiles: dict) -> None: ...


# ──────────────────────────────────────────────
# CallbackDispatcher
# ──────────────────────────────────────────────


class CallbackDispatcher:
    """Wraps callback invocations with error logging and multi-subscriber support.

    Replaces ``try/except: pass`` patterns with proper exception handling:

        dispatcher = CallbackDispatcher("SmartAggregator")
        dispatcher.subscribe("on_filtered_message", ui_handler)
        dispatcher.dispatch("on_filtered_message", message)

    If a callback raises, the error is logged and remaining subscribers
    still receive the event.
    """

    def __init__(self, source: str = "unknown"):
        self._source = source
        self._subscribers: dict[str, list[Callable]] = {}
        self._protocol: type[Protocol] | None = None

    def set_protocol(self, protocol: type[Protocol]) -> None:
        """Attach a Protocol class for runtime signature validation."""
        self._protocol = protocol

    def subscribe(self, event: str, callback: Callable) -> None:
        """Register a callback for *event*.

        If a protocol is set, the callback signature is validated against
        the protocol method before registration.
        """
        if self._protocol is not None:
            self._validate_signature(event, callback)

        self._subscribers.setdefault(event, []).append(callback)

    def unsubscribe(self, event: str, callback: Callable) -> bool:
        """Remove a previously registered callback. Returns True if found."""
        subs = self._subscribers.get(event, [])
        try:
            subs.remove(callback)
            return True
        except ValueError:
            return False

    def unsubscribe_all(self, event: str) -> int:
        """Remove all callbacks for *event*. Returns count removed."""
        count = len(self._subscribers.get(event, []))
        self._subscribers.pop(event, None)
        return count

    def dispatch(self, event: str, *args: Any, **kwargs: Any) -> None:
        """Invoke all subscribers for *event* with safe error handling.

        If a callback raises ``Exception``, the error is logged and the
        remaining subscribers are still called.
        """
        for cb in list(self._subscribers.get(event, [])):
            try:
                cb(*args, **kwargs)
            except Exception as exc:
                logger.error(
                    "[%s] Callback error on '%s': %s",
                    self._source,
                    event,
                    exc,
                    exc_info=True,
                )

    def has_subscribers(self, event: str) -> bool:
        """Return True if at least one callback is registered for *event*."""
        return bool(self._subscribers.get(event))

    def subscriber_count(self, event: str) -> int:
        """Return the number of callbacks registered for *event*."""
        return len(self._subscribers.get(event, []))

    def clear(self) -> None:
        """Remove all registered callbacks for all events."""
        self._subscribers.clear()

    # ── helpers ──────────────────────────────

    def _validate_signature(self, event: str, callback: Callable) -> None:
        """Check that *callback* is compatible with the protocol method *event*."""
        if self._protocol is None:
            return

        proto_method = getattr(self._protocol, event, None)
        if proto_method is None:
            return

        try:
            proto_sig = inspect.signature(proto_method)
            cb_sig = inspect.signature(callback)
        except (ValueError, TypeError):
            return

        proto_params = list(proto_sig.parameters.values())
        cb_params = list(cb_sig.parameters.values())

        # Skip 'self' on protocol method
        if proto_params and proto_params[0].name == "self":
            proto_params = proto_params[1:]

        if len(cb_params) < len(proto_params):
            raise TypeError(
                f"[{self._source}] Callback '{callback.__name__}' for event "
                f"'{event}' expects {len(cb_params)} args but protocol requires "
                f"{len(proto_params)}"
            )


# ──────────────────────────────────────────────
# Convenience factory
# ──────────────────────────────────────────────


def create_dispatcher_for(
    source: str,
    protocol: type[Protocol] | None = None,
) -> CallbackDispatcher:
    """Create a CallbackDispatcher optionally bound to a Protocol."""
    disp = CallbackDispatcher(source=source)
    if protocol is not None:
        disp.set_protocol(protocol)
    return disp
