"""Thread-safe UI state container with observer pattern.

Provides a framework-agnostic, thread-safe container for all UI-visible
state in OpenCohost.  Replaces the scattered `self.ws_connected`,
`self._pipeline_state`, `self._ollama_state`, etc. attributes that were
previously read/written from multiple threads without synchronization.

All public methods are thread-safe and may be called from any thread.
Observer callbacks are dispatched in a background thread so they never
block the setter thread.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from typing import Any, Callable, Dict, Optional

# ---------------------------------------------------------------------------
# Valid status literals – used for runtime validation
# ---------------------------------------------------------------------------

VALID_MODEL_STATUSES = frozenset({"loading", "ready", "error", "offline"})
VALID_MIC_STATUSES = frozenset({"disconnected", "idle", "listening", "recording"})
VALID_TTS_STATUSES = frozenset({"idle", "generating", "speaking", "paused", "error"})
VALID_CHAT_STATUSES = frozenset({"disconnected", "connecting", "connected", "error"})
VALID_PIPELINE_STATES = frozenset(
    {"idle", "listening", "processing", "speaking", "playing", "downloading", "init", "error"}
)
VALID_OLLAMA_STATES = frozenset(
    {"checking", "ready", "app_missing", "package_missing", "service_stopped"}
)
VALID_HEALTH_STATUSES = frozenset({"unknown", "green", "yellow", "red"})
# Engine (voice) badge statuses — single source of truth lives in core.qwen_markers (SV-A),
# so ui and core can never diverge on the valid set.
from opencohost.core.qwen_markers import ENGINE_STATUSES as VALID_ENGINE_STATUSES

# ---------------------------------------------------------------------------
# Observer callback type
# ---------------------------------------------------------------------------
ObserverCallback = Callable[[str, Any], None]


class UIState:
    """Thread-safe UI state container with observer pattern.

    Thread safety is guaranteed via a single `threading.Lock` that guards
    all reads and writes to the internal state dictionary.  A
    `threading.Condition` (backed by the same lock) is used to signal
    observers when state changes.

    Observer callbacks are dispatched to a dedicated background thread so
    they never block the calling thread of a setter.  This prevents
    deadlocks when an observer callback happens to call a setter.

    Usage::

        state = UIState()

        # Subscribe to all changes
        def on_change(key: str, value: Any) -> None:
            print(f"{key} -> {value}")

        sub_id = state.subscribe(on_change)

        # Set individual values (thread-safe)
        state.set_pipeline_state("listening")

        # Batch update – observers notified once
        state.update(ws_connected=True, pipeline_state="idle")

        # Unsubscribe when done
        state.unsubscribe(sub_id)
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._condition = threading.Condition(self._lock)

        # Internal state dictionary
        self._state: Dict[str, Any] = {
            "model_status": "loading",
            "mic_status": "disconnected",
            "tts_status": "idle",
            "chat_status": "disconnected",
            "pipeline_state": "idle",
            "ollama_state": "checking",
            "ws_connected": False,
            "ws_should_reconnect": False,
            "smart_agg_connected": False,
            "smart_agg_connecting": False,
            "stream_admin_chat_connected": False,
            "current_model": "",
            "current_profile": "",
            "ptt_active": False,
            "ptt_enabled": False,
            "advanced_mode": False,
            "health_status": "unknown",
            "engine_status": "unknown",  # effective TTS engine badge (SV-A)
            "engine_reason": "",         # free-form fallback reason (unvalidated)
        }

        # Observer registry: subscription_id -> callback
        self._observers: Dict[int, ObserverCallback] = OrderedDict()
        self._next_observer_id: int = 0

        # Background dispatcher thread for observer callbacks
        self._dispatch_queue: list[tuple[str, Any]] = []
        self._dispatch_event = threading.Event()
        self._dispatch_stop = threading.Event()
        self._dispatch_thread = threading.Thread(
            target=self._dispatch_loop,
            name="UIState-ObserverDispatcher",
            daemon=True,
        )
        self._dispatch_thread.start()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def shutdown(self, timeout: float = 5.0) -> None:
        """Stop the observer dispatch thread.

        After calling this method, no further observer callbacks will be
        invoked.  State setters remain safe but notifications are dropped.
        """
        self._dispatch_stop.set()
        self._dispatch_event.set()
        self._dispatch_thread.join(timeout=timeout)

    # ------------------------------------------------------------------
    # Observer pattern
    # ------------------------------------------------------------------

    def subscribe(self, callback: ObserverCallback) -> int:
        """Register an observer callback.

        The callback receives ``(key, new_value)`` every time a state
        field changes (including during batch updates – one call per
        changed key).

        Returns a subscription ID that can be passed to
        :meth:`unsubscribe` to remove the callback.

        Thread-safe.
        """
        with self._lock:
            observer_id = self._next_observer_id
            self._next_observer_id += 1
            self._observers[observer_id] = callback
            return observer_id

    def unsubscribe(self, observer_id: int) -> None:
        """Remove a previously registered observer.

        No-op if the ID is not found.
        Thread-safe.
        """
        with self._lock:
            self._observers.pop(observer_id, None)

    def _notify_observers(self, key: str, value: Any) -> None:
        """Enqueue a notification for the dispatch thread.

        Must be called while holding ``self._lock``.
        """
        self._dispatch_queue.append((key, value))
        self._dispatch_event.set()

    def _dispatch_loop(self) -> None:
        """Background loop that drains the notification queue.

        Runs on a dedicated daemon thread.  Drains the queue in batches
        to avoid holding the lock while callbacks execute.
        """
        while not self._dispatch_stop.is_set():
            self._dispatch_event.wait(timeout=0.05)
            self._dispatch_event.clear()

            # Snapshot the queue under the lock, then release it
            with self._lock:
                batch = list(self._dispatch_queue)
                self._dispatch_queue.clear()

            # Invoke callbacks outside the lock
            for key, value in batch:
                if self._dispatch_stop.is_set():
                    return
                with self._lock:
                    observers = list(self._observers.values())
                for callback in observers:
                    try:
                        callback(key, value)
                    except Exception:
                        # Swallow observer errors – they must not crash
                        # the dispatch thread or block other observers.
                        pass

    # ------------------------------------------------------------------
    # Generic getters / setters
    # ------------------------------------------------------------------

    def get(self, key: str, default: Any = None) -> Any:
        """Return the value for *key*, or *default* if not found.

        Thread-safe.
        """
        with self._lock:
            return self._state.get(key, default)

    def set(self, key: str, value: Any) -> None:
        """Set a single state field and notify observers.

        Thread-safe.
        """
        with self._lock:
            self._state[key] = value
            self._notify_observers(key, value)
            self._condition.notify_all()

    def update(self, **kwargs: Any) -> None:
        """Update multiple fields atomically.

        Observers are notified once per changed key, but all changes
        happen under a single lock acquisition so readers see a
        consistent snapshot.

        Thread-safe.
        """
        with self._lock:
            for key, value in kwargs.items():
                self._state[key] = value
                self._notify_observers(key, value)
            self._condition.notify_all()

    def snapshot(self) -> Dict[str, Any]:
        """Return a shallow copy of the entire state dictionary.

        Thread-safe.
        """
        with self._lock:
            return dict(self._state)

    # ------------------------------------------------------------------
    # Typed property getters
    # ------------------------------------------------------------------

    @property
    def model_status(self) -> str:
        with self._lock:
            return self._state["model_status"]

    @property
    def mic_status(self) -> str:
        with self._lock:
            return self._state["mic_status"]

    @property
    def tts_status(self) -> str:
        with self._lock:
            return self._state["tts_status"]

    @property
    def chat_status(self) -> str:
        with self._lock:
            return self._state["chat_status"]

    @property
    def pipeline_state(self) -> str:
        with self._lock:
            return self._state["pipeline_state"]

    @property
    def ollama_state(self) -> str:
        with self._lock:
            return self._state["ollama_state"]

    @property
    def ws_connected(self) -> bool:
        with self._lock:
            return self._state["ws_connected"]

    @property
    def ws_should_reconnect(self) -> bool:
        with self._lock:
            return self._state["ws_should_reconnect"]

    @property
    def smart_agg_connected(self) -> bool:
        with self._lock:
            return self._state["smart_agg_connected"]

    @property
    def smart_agg_connecting(self) -> bool:
        with self._lock:
            return self._state["smart_agg_connecting"]

    @property
    def stream_admin_chat_connected(self) -> bool:
        with self._lock:
            return self._state["stream_admin_chat_connected"]

    @property
    def current_model(self) -> str:
        with self._lock:
            return self._state["current_model"]

    @property
    def current_profile(self) -> str:
        with self._lock:
            return self._state["current_profile"]

    @property
    def ptt_active(self) -> bool:
        with self._lock:
            return self._state["ptt_active"]

    @property
    def ptt_enabled(self) -> bool:
        with self._lock:
            return self._state["ptt_enabled"]

    @property
    def advanced_mode(self) -> bool:
        with self._lock:
            return self._state["advanced_mode"]

    # ------------------------------------------------------------------
    # Typed property setters (with validation)
    # ------------------------------------------------------------------

    @model_status.setter
    def model_status(self, value: str) -> None:
        if value not in VALID_MODEL_STATUSES:
            raise ValueError(
                f"Invalid model_status '{value}'. Must be one of {sorted(VALID_MODEL_STATUSES)}"
            )
        with self._lock:
            self._state["model_status"] = value
            self._notify_observers("model_status", value)
            self._condition.notify_all()

    @mic_status.setter
    def mic_status(self, value: str) -> None:
        if value not in VALID_MIC_STATUSES:
            raise ValueError(
                f"Invalid mic_status '{value}'. Must be one of {sorted(VALID_MIC_STATUSES)}"
            )
        with self._lock:
            self._state["mic_status"] = value
            self._notify_observers("mic_status", value)
            self._condition.notify_all()

    @tts_status.setter
    def tts_status(self, value: str) -> None:
        if value not in VALID_TTS_STATUSES:
            raise ValueError(
                f"Invalid tts_status '{value}'. Must be one of {sorted(VALID_TTS_STATUSES)}"
            )
        with self._lock:
            self._state["tts_status"] = value
            self._notify_observers("tts_status", value)
            self._condition.notify_all()

    @chat_status.setter
    def chat_status(self, value: str) -> None:
        if value not in VALID_CHAT_STATUSES:
            raise ValueError(
                f"Invalid chat_status '{value}'. Must be one of {sorted(VALID_CHAT_STATUSES)}"
            )
        with self._lock:
            self._state["chat_status"] = value
            self._notify_observers("chat_status", value)
            self._condition.notify_all()

    @pipeline_state.setter
    def pipeline_state(self, value: str) -> None:
        if value not in VALID_PIPELINE_STATES:
            raise ValueError(
                f"Invalid pipeline_state '{value}'. Must be one of {sorted(VALID_PIPELINE_STATES)}"
            )
        with self._lock:
            self._state["pipeline_state"] = value
            self._notify_observers("pipeline_state", value)
            self._condition.notify_all()

    @ollama_state.setter
    def ollama_state(self, value: str) -> None:
        if value not in VALID_OLLAMA_STATES:
            raise ValueError(
                f"Invalid ollama_state '{value}'. Must be one of {sorted(VALID_OLLAMA_STATES)}"
            )
        with self._lock:
            self._state["ollama_state"] = value
            self._notify_observers("ollama_state", value)
            self._condition.notify_all()

    @ws_connected.setter
    def ws_connected(self, value: bool) -> None:
        with self._lock:
            self._state["ws_connected"] = value
            self._notify_observers("ws_connected", value)
            self._condition.notify_all()

    @ws_should_reconnect.setter
    def ws_should_reconnect(self, value: bool) -> None:
        with self._lock:
            self._state["ws_should_reconnect"] = value
            self._notify_observers("ws_should_reconnect", value)
            self._condition.notify_all()

    @smart_agg_connected.setter
    def smart_agg_connected(self, value: bool) -> None:
        with self._lock:
            self._state["smart_agg_connected"] = value
            self._notify_observers("smart_agg_connected", value)
            self._condition.notify_all()

    @smart_agg_connecting.setter
    def smart_agg_connecting(self, value: bool) -> None:
        with self._lock:
            self._state["smart_agg_connecting"] = value
            self._notify_observers("smart_agg_connecting", value)
            self._condition.notify_all()

    @stream_admin_chat_connected.setter
    def stream_admin_chat_connected(self, value: bool) -> None:
        with self._lock:
            self._state["stream_admin_chat_connected"] = value
            self._notify_observers("stream_admin_chat_connected", value)
            self._condition.notify_all()

    @current_model.setter
    def current_model(self, value: str) -> None:
        with self._lock:
            self._state["current_model"] = value
            self._notify_observers("current_model", value)
            self._condition.notify_all()

    @current_profile.setter
    def current_profile(self, value: str) -> None:
        with self._lock:
            self._state["current_profile"] = value
            self._notify_observers("current_profile", value)
            self._condition.notify_all()

    @ptt_active.setter
    def ptt_active(self, value: bool) -> None:
        with self._lock:
            self._state["ptt_active"] = value
            self._notify_observers("ptt_active", value)
            self._condition.notify_all()

    @ptt_enabled.setter
    def ptt_enabled(self, value: bool) -> None:
        with self._lock:
            self._state["ptt_enabled"] = value
            self._notify_observers("ptt_enabled", value)
            self._condition.notify_all()

    @advanced_mode.setter
    def advanced_mode(self, value: bool) -> None:
        with self._lock:
            self._state["advanced_mode"] = value
            self._notify_observers("advanced_mode", value)
            self._condition.notify_all()

    @property
    def health_status(self) -> str:
        with self._lock:
            return self._state["health_status"]

    @health_status.setter
    def health_status(self, value: str) -> None:
        if value not in VALID_HEALTH_STATUSES:
            raise ValueError(
                f"Invalid health_status '{value}'. Must be one of {sorted(VALID_HEALTH_STATUSES)}"
            )
        with self._lock:
            self._state["health_status"] = value
            self._notify_observers("health_status", value)
            self._condition.notify_all()

    @property
    def engine_status(self) -> str:
        with self._lock:
            return self._state["engine_status"]

    @engine_status.setter
    def engine_status(self, value: str) -> None:
        if value not in VALID_ENGINE_STATUSES:
            raise ValueError(
                f"Invalid engine_status '{value}'. Must be one of {sorted(VALID_ENGINE_STATUSES)}"
            )
        with self._lock:
            self._state["engine_status"] = value
            self._notify_observers("engine_status", value)
            self._condition.notify_all()

    # ------------------------------------------------------------------
    # Convenience: wait for a condition
    # ------------------------------------------------------------------

    def wait_for(self, key: str, value: Any, timeout: float = 10.0) -> bool:
        """Block until ``state[key] == value`` or *timeout* elapses.

        Returns ``True`` if the condition was met, ``False`` on timeout.
        Thread-safe.
        """
        deadline = time.monotonic() + timeout
        with self._condition:
            while self._state.get(key) != value:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(timeout=remaining)
            return True
