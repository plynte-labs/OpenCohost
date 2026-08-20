"""
PTT (Push-to-Talk) Manager for OpenCohost.

Encapsulates hotkey configuration, pynput listener lifecycle, and
PTT state management. Thread-safe for concurrent UI and audio access.
"""

import os
import sys
import json
import ctypes
import threading
from typing import Any, Callable, Optional

from pynput import keyboard, mouse

from opencohost.config.settings import (
    PTT_DEFAULT_HOTKEY,
    PTT_HOTKEY_LIST,
    PTT_CONFIG_FILE,
)


# ── Mapeo de teclas PTT (display → pynput) ──
_PTT_KB_MAP = {f"F{i}": getattr(keyboard.Key, f"f{i}") for i in range(1, 13)}
_PTT_KB_MAP.update({
    "ScrollLock": keyboard.Key.scroll_lock,
    "Insert": keyboard.Key.insert,
    "Pause": keyboard.Key.pause,
})
_mouse_x1 = getattr(mouse.Button, "x1", getattr(mouse.Button, "button8", mouse.Button.unknown))
_mouse_x2 = getattr(mouse.Button, "x2", getattr(mouse.Button, "button9", mouse.Button.unknown))

_PTT_MOUSE_MAP = {
    "Mouse4": _mouse_x2,
    "Mouse5": _mouse_x1,
}

# ── Physical Win32 VK codes for the GetAsyncKeyState probe ──
# Keyboard keys carry their VK directly via `.value.vk` (no parallel table to
# drift). Mouse Buttons do NOT — `Button.x1.value` is a tuple, not a VK — so the
# X-buttons need an explicit map KEYED OFF THE PYNPUT Button OBJECT. Keying off
# the Button (x1 -> 0x05, x2 -> 0x06) sidesteps the inverted display labels in
# `_PTT_MOUSE_MAP`, which only affect UI text.
_PTT_MOUSE_VK_MAP = {
    _mouse_x1: 0x05,  # VK_XBUTTON1
    _mouse_x2: 0x06,  # VK_XBUTTON2
}

# Mapa inverso: pynput key → display name
_PTT_REVERSE_MAP: dict = {}
_PTT_REVERSE_MAP.update({v: k for k, v in _PTT_KB_MAP.items()})
_PTT_REVERSE_MAP.update({v: k for k, v in _PTT_MOUSE_MAP.items()})


# ── Physical key-state probe (stdlib ctypes; fail-open everywhere) ──

def _resolve_get_async_key_state():
    """Resolve user32.GetAsyncKeyState once at import; None if unavailable.

    Fail-open: on non-Windows, a missing `windll`, or any ctypes error this
    returns None and the reconcile becomes a byte-for-byte no-op.
    """
    if sys.platform != "win32" or not hasattr(ctypes, "windll"):
        return None
    try:
        fn = ctypes.windll.user32.GetAsyncKeyState
        fn.argtypes = [ctypes.c_int]
        fn.restype = ctypes.c_short
        return fn
    except Exception:
        return None


_GET_ASYNC_KEY_STATE = _resolve_get_async_key_state()


def _is_vk_physically_down(vk):
    """Return True/False for the real physical key state, or None if unprobeable.

    [E1 — load-bearing] `restype = c_short` returns a SIGNED value, so a held key
    reads NEGATIVE (e.g. -32768). The high bit (& 0x8000) is the 'currently down'
    flag and is correct because Python's arbitrary-precision two's complement makes
    `-32768 & 0x8000 == 0x8000`. Do NOT 'simplify' this to `fn(vk) > 0`: a held key
    would then read False and the reconcile would fire a spurious release on every
    real hold. The low bit (pressed-since-last-call / toggle) is ignored.

    Fail-open: None when the probe is unavailable, the VK is unresolved, or the
    ctypes call raises — the reconcile then behaves exactly as today.
    """
    fn = _GET_ASYNC_KEY_STATE
    if fn is None or vk is None:
        return None
    try:
        return bool(fn(vk) & 0x8000)
    except Exception:
        return None


class PTTManager:
    """Manages Push-to-Talk hotkey configuration and listener lifecycle.

    Thread safety:
    - Listener lifecycle (_listener_lock): RLock protects start/stop/ensure
      to prevent race conditions when the UI toggles PTT while audio threads
      query state.
    - Config file access (_config_lock): Lock serializes reads/writes to
      ptt_settings.json to avoid corrupt writes from concurrent saves.
    - State properties (enabled, active, mapping): Simple bools read/written
      under the appropriate lock or from the main thread only.
    """

    # Consecutive "physically up" polls required before declaring a missed
    # key-up. Biblia-safe: a real hold reads down on every poll so the counter
    # never accumulates; a missed key-up is persistent and trips within ~500ms.
    _MISSED_UP_DEBOUNCE = 2

    def __init__(
        self,
        config_file: str = PTT_CONFIG_FILE,
        default_hotkey: str = PTT_DEFAULT_HOTKEY,
        hotkey_list: list[str] | None = None,
        logger: Any = None,
    ) -> None:
        self._config_file = config_file
        self._default_hotkey = default_hotkey
        self._hotkey_list = hotkey_list if hotkey_list is not None else list(PTT_HOTKEY_LIST)
        self._logger = logger

        # Listener lifecycle
        self._listener_lock = threading.RLock()
        self._listener: Optional[Any] = None  # pynput Listener
        self._target: Optional[tuple[str, Any]] = None  # (kind, pynput_key)

        # Mapping mode (hotkey remapping)
        self._mapping = False
        self._mapping_listeners: list[Any] = []

        # Config file lock (must be before _load_config)
        self._config_lock = threading.Lock()

        # PTT state
        self._enabled = False
        self._pressed = False  # Is the hotkey currently held down?
        self._hotkey = self._load_config()

        # Physical key-state reconciliation (missed key-up self-heal).
        # Stash the OUTER release/click callbacks so a synthesized release routes
        # the full path (app_shell wrapper → on_ptt_release → voice flush), not
        # just the avatar reset. Cleared in stop_listener.
        self._on_release_cb: Optional[Callable] = None
        self._on_click_cb: Optional[Callable] = None
        self._missed_up_count = 0

        # Callbacks (set by UI)
        self._on_status_change: Optional[Callable[[str, str], None]] = None
        self._on_state_change: Optional[Callable[[str], None]] = None
        self._on_log: Optional[Callable[[str], None]] = None

    # ── Callback registration ──

    def set_status_callback(self, callback: Callable[[str, str], None]) -> None:
        """Register a callback invoked when PTT status text should change.

        Signature: callback(text: str, color: str) -> None
        Called on the listener's thread; UI should marshal to main thread.
        """
        self._on_status_change = callback

    def set_state_callback(self, callback: Callable[[str], None]) -> None:
        """Register a callback invoked when pipeline state should change.

        Signature: callback(state: str) -> None  (e.g. "listening", "idle")
        """
        self._on_state_change = callback

    def set_log_callback(self, callback: Callable[[str], None]) -> None:
        """Register a callback for PTT log messages.

        Signature: callback(message: str) -> None
        """
        self._on_log = callback

    # ── Config management ──

    def load_config(self) -> str:
        """(Re)load hotkey from config file. Returns the loaded hotkey."""
        self._hotkey = self._load_config()
        return self._hotkey

    def save_config(self, hotkey: str) -> bool:
        """Persist hotkey to config file. Returns True on success."""
        return self._save_config(hotkey)

    def get_hotkey(self) -> str:
        """Return the current hotkey display name."""
        return self._hotkey

    def set_hotkey(self, hotkey: str) -> bool:
        """Set and persist a new hotkey. Returns True if valid and saved."""
        if not self.is_valid_hotkey(hotkey):
            return False
        self._hotkey = hotkey
        return self._save_config(hotkey)

    def is_valid_hotkey(self, hotkey: str) -> bool:
        """Check if a hotkey name is in the supported list."""
        return hotkey in self._hotkey_list

    def get_all_supported_hotkeys(self) -> list[str]:
        """Return the list of all supported hotkey display names."""
        return list(self._hotkey_list)

    # ── Hotkey mapping ──

    def build_pynput_target(self, hotkey_name: str) -> tuple[str, Any]:
        """Resolve a display name to (kind, pynput_target).

        Returns ("keyboard", keyboard.Key) or ("mouse", mouse.Button)
        or (None, None) if the hotkey is not recognized.
        """
        if hotkey_name in _PTT_KB_MAP:
            return ("keyboard", _PTT_KB_MAP[hotkey_name])
        if hotkey_name in _PTT_MOUSE_MAP:
            return ("mouse", _PTT_MOUSE_MAP[hotkey_name])
        return (None, None)

    def get_reverse_mapping(self) -> dict:
        """Return the inverse mapping: pynput object → display name."""
        return dict(_PTT_REVERSE_MAP)

    # ── Listener lifecycle ──

    def start_listener(
        self,
        on_press: Callable,
        on_release: Callable,
        on_click: Callable,
    ) -> bool:
        """Start the PTT listener for the current hotkey.

        Must be called with the appropriate callbacks for keyboard press/release
        and mouse click events. Thread-safe: acquires _listener_lock.

        Returns True if listener started, False if hotkey is unsupported.
        """
        with self._listener_lock:
            self.stop_listener()
            kind, target = self.build_pynput_target(self._hotkey)
            if kind == "keyboard":
                self._listener = keyboard.Listener(
                    on_press=on_press,
                    on_release=on_release,
                )
            elif kind == "mouse":
                self._listener = mouse.Listener(
                    on_click=on_click,
                )
            else:
                self._log(f"[PTT] Tecla no soportada: {self._hotkey}")
                return False
            self._listener.daemon = True
            self._target = (kind, target)
            # Stash the outer callbacks so the reconcile can re-inject a missed
            # key-up through the SAME path a real event takes (incl. the flush).
            self._on_release_cb = on_release
            self._on_click_cb = on_click
            self._listener.start()
            self._log(f"[PTT] Listener iniciado: kind={kind} target={target}")
            return True

    def stop_listener(self) -> None:
        """Stop the active PTT listener. Thread-safe: acquires _listener_lock."""
        with self._listener_lock:
            if self._listener:
                try:
                    self._listener.stop()
                except Exception as e:
                    self._log(f"[PTT] Error stopping listener: {e}")
                self._listener = None
            self._pressed = False
            # Clear reconcile stash + counter so a stale callback can never fire
            # after the listener is gone.
            self._on_release_cb = None
            self._on_click_cb = None
            self._missed_up_count = 0
            self._log("[PTT] Listener detenido")

    def ensure_listener(
        self,
        on_press: Callable,
        on_release: Callable,
        on_click: Callable,
    ) -> bool:
        """Reconcile listener state: start if enabled, not mapping, and not running.

        Thread-safe: acquires _listener_lock. Does NOT call start_listener
        to avoid double-locking; duplicates the inline start logic.

        Returns True if a listener was (re)started, False otherwise.
        """
        with self._listener_lock:
            if self._enabled and self._listener is None and not self._mapping:
                self._log("[PTT] Reconciliando listener...")
                kind, target = self.build_pynput_target(self._hotkey)
                if kind == "keyboard":
                    self._listener = keyboard.Listener(
                        on_press=on_press,
                        on_release=on_release,
                    )
                elif kind == "mouse":
                    self._listener = mouse.Listener(
                        on_click=on_click,
                    )
                else:
                    return False
                self._listener.daemon = True
                self._target = (kind, target)
                # Re-stash here (restart branch) where the params are in scope.
                # The early-return path keeps the prior stash valid — the bound
                # wrappers are identity-stable across motor restart.
                self._on_release_cb = on_release
                self._on_click_cb = on_click
                self._listener.start()
                self._log(f"[PTT] Listener reconciliado: kind={kind} target={target}")
                return True
            return False

    def is_listener_active(self) -> bool:
        """Return True if a pynput listener is currently running."""
        with self._listener_lock:
            return self._listener is not None

    # ── Mapping mode ──

    def start_mapping(
        self,
        on_key: Callable,
        on_mouse: Callable,
    ) -> None:
        """Enter hotkey remapping mode. Starts temporary keyboard+mouse listeners."""
        if self._mapping:
            return
        self.stop_listener()
        self._mapping = True
        self._mapping_listeners = []

        kb = keyboard.Listener(on_press=on_key)
        kb.daemon = True
        kb.start()

        ms = mouse.Listener(on_click=on_mouse)
        ms.daemon = True
        ms.start()

        self._mapping_listeners = [kb, ms]
        self._log("[PTT] Modo mapeo: esperando pulsacion...")

    def stop_mapping(self) -> None:
        """Exit mapping mode and stop temporary listeners."""
        for lst in self._mapping_listeners:
            try:
                lst.stop()
            except Exception:
                pass
        self._mapping_listeners = []
        self._mapping = False

    def on_mapping_key(self, key) -> Optional[bool]:
        """Default handler for mapping keyboard events.

        Returns False to stop the listener if a valid key was captured.
        """
        display = _PTT_REVERSE_MAP.get(key)
        if display:
            return False  # signal to stop listener
        return None

    def on_mapping_mouse(self, x, y, button, pressed) -> Optional[bool]:
        """Default handler for mapping mouse events.

        Returns False to stop the listener if a valid button was captured.
        """
        if pressed:
            display = _PTT_REVERSE_MAP.get(button)
            if display:
                return False
        return None

    def apply_mapped_hotkey(self, hotkey: str) -> None:
        """Save a newly mapped hotkey. UI is responsible for restarting the listener."""
        if not self.is_valid_hotkey(hotkey):
            self._log(f"[PTT] Hotkey inválida: {hotkey}")
            return
        self._hotkey = hotkey
        self._save_config(hotkey)
        self._log(f"[PTT] Hotkey aplicada: {hotkey}")

    # ── Physical key-state reconciliation (missed key-up self-heal) ──

    def _resolve_target_vk(self) -> Optional[int]:
        """Resolve the Win32 VK for the currently stored pynput target.

        Keyed off `self._target[1]` (the pynput object), NEVER the display label,
        so the inverted mouse display map never leaks in. Keyboard keys expose the
        VK via `.value.vk`; mouse X-buttons use the explicit `_PTT_MOUSE_VK_MAP`.
        Returns None on any failure → fail-open.
        """
        target = self._target
        if not target:
            return None
        kind, obj = target
        if kind == "keyboard":
            try:
                return obj.value.vk
            except Exception:
                return None
        if kind == "mouse":
            return _PTT_MOUSE_VK_MAP.get(obj)
        return None

    def _reconcile_step(self) -> None:
        """Reconcile a held PTT key against the REAL physical key state.

        Pure/synchronous; driven by app_shell's perpetual ``after(250)`` loop.
        While ``_pressed``, polls ``GetAsyncKeyState``. If the key has been
        physically up for ``_MISSED_UP_DEBOUNCE`` consecutive polls, the key-up
        event was dropped → re-inject the release THROUGH the stored OUTER
        callback so the buffer flush fires (not just the avatar reset).

        Load-bearing details:
        - NO time limit: a real multi-minute hold reads down on essentially every
          poll, so the counter never accumulates (biblia-safe).
        - Fail-open: a None probe result (non-Windows / unresolved VK / ctypes
          error) is a no-op, byte-for-byte identical to today.
        - Does NOT pre-clear ``_pressed``: the outer wrapper reads ``was_active``
          BEFORE the inner clear, so ``_pressed`` MUST still be True when the
          synthesized callback runs or the flush is skipped. Let the inner
          ``on_ptt_release`` flip it.
        - Must NOT hold ``_listener_lock`` while invoking the stashed callback —
          the callback path acquires it; avoid self-deadlock. This runs on the Tk
          main thread, so the lock is simply not held across the call.
        """
        if not self._pressed:
            self._missed_up_count = 0
            return
        down = _is_vk_physically_down(self._resolve_target_vk())
        if down is None:                       # fail-open → behave as today
            return
        if down:
            self._missed_up_count = 0
            return
        self._missed_up_count += 1
        if self._missed_up_count < self._MISSED_UP_DEBOUNCE:
            return
        self._missed_up_count = 0
        kind, target = self._target or (None, None)
        if kind == "keyboard" and self._on_release_cb:
            # → app_shell._on_ptt_release → PTTManager.on_ptt_release → flush
            self._on_release_cb(target)
        elif kind == "mouse" and self._on_click_cb:
            self._on_click_cb(0, 0, target, False)

    # ── PTT event handlers (for use with pynput) ──

    def on_ptt_press(self, key) -> None:
        """Handle keyboard press event for PTT. Call from pynput listener."""
        kind, target = self._target or (None, None)
        if kind == "keyboard" and key == target and not self._pressed:
            with self._listener_lock:
                self._pressed = True
            self._log(f"[PTT] MATCH press: {key}")
            self._emit_status("\U0001f534 ESCUCHANDO...", "#44ff44")
            self._emit_state("listening")

    def on_ptt_release(self, key) -> None:
        """Handle keyboard release event for PTT. Call from pynput listener."""
        kind, target = self._target or (None, None)
        if kind == "keyboard" and key == target and self._pressed:
            with self._listener_lock:
                self._pressed = False
            self._log(f"[PTT] MATCH release: {key}")
            self._emit_status(
                f"Manten presionado [{self._hotkey}] para hablar",
                "#888888",
            )
            self._emit_state("idle")

    def on_ptt_click(self, x, y, button, pressed) -> None:
        """Handle mouse click event for PTT. Call from pynput listener."""
        kind, target = self._target or (None, None)
        if kind == "mouse" and button == target:
            with self._listener_lock:
                self._pressed = pressed
            self._log(f"[PTT] MOUSE {'PRESS' if pressed else 'RELEASE'}: {button} -> ptt_pressed={pressed}")
            if pressed:
                self._emit_status("\U0001f534 ESCUCHANDO...", "#44ff44")
                self._emit_state("listening")
            else:
                self._emit_status(
                    f"Manten presionado [{self._hotkey}] para hablar",
                    "#888888",
                )
                self._emit_state("idle")

    # ── State properties ──

    @property
    def enabled(self) -> bool:
        """Whether PTT mode is globally enabled."""
        return self._enabled

    @enabled.setter
    def enabled(self, value: bool) -> None:
        with self._listener_lock:
            self._enabled = value

    @property
    def active(self) -> bool:
        """Whether the PTT hotkey is currently held down (listening state)."""
        return self._pressed

    @property
    def mapping(self) -> bool:
        """Whether the manager is in hotkey remapping mode."""
        return self._mapping

    @property
    def hotkey(self) -> str:
        """Current hotkey display name."""
        return self._hotkey

    @hotkey.setter
    def hotkey(self, value: str) -> None:
        if not self.is_valid_hotkey(value):
            self._log(f"[PTT] Hotkey inválida: {value}")
            return
        self._hotkey = value

    @property
    def target(self) -> Optional[tuple[str, Any]]:
        """Current (kind, pynput_target) tuple."""
        return self._target

    @property
    def listener(self) -> Optional[Any]:
        """Current pynput listener instance."""
        with self._listener_lock:
            return self._listener

    @property
    def mapping_listeners(self) -> list:
        """List of temporary pynput listeners used during mapping mode."""
        return list(self._mapping_listeners)

    @mapping_listeners.setter
    def mapping_listeners(self, value: list) -> None:
        self._mapping_listeners = value

    # ── Internal helpers ──

    def _load_config(self) -> str:
        """Load hotkey from JSON config file. Returns default on failure."""
        with self._config_lock:
            try:
                if os.path.exists(self._config_file):
                    with open(self._config_file, "r", encoding="utf-8") as f:
                        data = json.load(f)
                        hotkey = data.get("hotkey", self._default_hotkey)
                        if hotkey in self._hotkey_list:
                            return hotkey
                        else:
                            self._log(f"[PTT] Hotkey en archivo no valida: '{hotkey}', usando default")
            except Exception as e:
                self._log(f"[PTT] Error cargando config PTT: {e}")
            return self._default_hotkey

    def _save_config(self, hotkey: str) -> bool:
        """Persist hotkey to JSON config file. Returns True on success."""
        with self._config_lock:
            try:
                os.makedirs(os.path.dirname(self._config_file), exist_ok=True)
                # MERGE, never clobber: ptt_settings.json is SHARED state. This
                # manager owns "hotkey"; PUT /api/ptt/config owns "stt_ws_uri"
                # (liveaudio_ws_uri_config_20260724). A plain overwrite here
                # would silently reset the operator's LiveAudio URL back to the
                # default every time they remapped the key.
                data = {}
                try:
                    with open(self._config_file, "r", encoding="utf-8") as f:
                        loaded = json.load(f)
                    if isinstance(loaded, dict):
                        data = loaded
                except Exception:
                    data = {}
                data["hotkey"] = hotkey
                with open(self._config_file, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2)
                return True
            except Exception as e:
                self._log(f"No se pudo guardar config PTT: {e}")
                return False

    def _emit_status(self, text: str, color: str) -> None:
        """Fire the status change callback if registered."""
        if self._on_status_change:
            self._on_status_change(text, color)

    def _emit_state(self, state: str) -> None:
        """Fire the state change callback if registered."""
        if self._on_state_change:
            self._on_state_change(state)

    def _log(self, message: str) -> None:
        """Log a message via the registered logger or log callback."""
        try:
            if self._logger:
                self._logger.debug(message)
        except Exception:
            pass
        try:
            if self._on_log:
                self._on_log(message)
        except Exception:
            pass
