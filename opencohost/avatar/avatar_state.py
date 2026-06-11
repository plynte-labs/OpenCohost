"""Avatar runtime state bridge for VoiceAI.

Provides a simple, Tkinter-independent enum and pub/sub bridge so that
core modules (voice, chat, TTS) can signal avatar state changes without
knowing about UI widgets.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from enum import Enum
from typing import Callable, Optional

# ---------------------------------------------------------------------------
# Avatar states
# ---------------------------------------------------------------------------

class AvatarState(str, Enum):
    IDLE = "idle"
    LISTENING = "listening"
    THINKING = "thinking"
    SPEAKING = "speaking"
    SPEAKING_ALT = "speaking_alt"
    SLEEPING = "sleeping"
    ANGRY = "angry"
    ERROR = "error"


# ---------------------------------------------------------------------------
# Bridge
# ---------------------------------------------------------------------------

AvatarStateListener = Callable[[AvatarState], None]


class AvatarStateBridge:
    """Thread-safe avatar state container with observer pattern.

    Usage::

        bridge = AvatarStateBridge()
        sub_id = bridge.subscribe(lambda s: print(f"Avatar -> {s}"))
        bridge.set_state(AvatarState.LISTENING)
        bridge.unsubscribe(sub_id)
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state: AvatarState = AvatarState.IDLE
        self._speech_text: str = ""
        self._observers: dict[int, AvatarStateListener] = OrderedDict()
        self._next_id: int = 0

    # -- state -----------------------------------------------------------

    def set_state(self, state: AvatarState) -> None:
        """Set the current avatar state and notify listeners."""
        with self._lock:
            if self._state == state:
                return
            self._state = state
            listeners = list(self._observers.values())
        for cb in listeners:
            try:
                cb(state)
            except Exception:
                pass

    def get_state(self) -> AvatarState:
        """Return the current avatar state."""
        with self._lock:
            return self._state

    # -- speech text -----------------------------------------------------

    def set_speech_text(self, text: str) -> None:
        """Store the text Kira is currently speaking."""
        with self._lock:
            self._speech_text = text

    def get_speech_text(self) -> str:
        with self._lock:
            return self._speech_text

    # -- observers -------------------------------------------------------

    def subscribe(self, listener: AvatarStateListener) -> int:
        """Register a state-change listener. Returns subscription ID."""
        with self._lock:
            sid = self._next_id
            self._next_id += 1
            self._observers[sid] = listener
            return sid

    def unsubscribe(self, subscription_id: int) -> None:
        with self._lock:
            self._observers.pop(subscription_id, None)

    # -- convenience -----------------------------------------------------

    @staticmethod
    def from_pipeline_state(pipeline_state: str) -> AvatarState:
        """Map a pipeline state string to an AvatarState."""
        mapping = {
            "listening": AvatarState.LISTENING,
            "processing": AvatarState.THINKING,
            "speaking": AvatarState.SPEAKING,
            "playing": AvatarState.SPEAKING,
            "idle": AvatarState.IDLE,
            "error": AvatarState.ERROR,
            "downloading": AvatarState.IDLE,
            "init": AvatarState.IDLE,
        }
        return mapping.get(pipeline_state, AvatarState.IDLE)
