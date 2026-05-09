"""
PTT (Push-to-Talk) Manager for VoiceAI.

Encapsulates hotkey configuration, pynput listener lifecycle, and
PTT state management. Thread-safe for concurrent UI and audio access.
"""

import os
import json
import threading
from typing import Any, Callable, Optional

from pynput import keyboard, mouse

from config.settings import (
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
_PTT_MOUSE_MAP = {
    "Mouse4": mouse.Button.x2,
    "Mouse5": mouse.Button.x1,
}

# Mapa inverso: pynput key → display name
_PTT_REVERSE_MAP: dict = {}
_PTT_REVERSE_MAP.update({v: k for k, v in _PTT_KB_MAP.items()})
_PTT_REVERSE_MAP.update({v: k for k, v in _PTT_MOUSE_MAP.items()})


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
                with open(self._config_file, "w", encoding="utf-8") as f:
                    json.dump({"hotkey": hotkey}, f, indent=2)
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
