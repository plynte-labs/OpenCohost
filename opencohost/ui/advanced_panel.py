"""Advanced Mode Panel module for OpenCohost.

Encapsulates the advanced mode panel that contains:
- Log display (general log, Kira actions, YouTube chat, stream admin log)
- Log queue processing
- Action logging with file persistence
- Kira response panel updates
- Logs panel visibility toggling

This module is decoupled from app.py internals and uses UIState for
all state communication.  Widget references are injected at construction
time so the panel can update the UI without knowing about app.py structure.

Usage::

    panel = AdvancedModePanel(
        parent_frame=root,
        ui_state=UIState(),
        dispatcher=CallbackDispatcher(source="AdvancedModePanel"),
        log_queue=queue.Queue(),
        text_kira_response=kira_textbox,
        on_log_action=None,
        schedule_ui_update=root.after,
    )
    frame = panel.build()
    frame.grid(row=2, column=0, ...)

    # Process logs periodically
    root.after(100, panel.process_logs)
"""

from __future__ import annotations

import json
import logging
import os
import queue
import time
from typing import Any, Callable, Optional

import customtkinter as ctk

from opencohost.config.settings import ACCIONES_LOG_FILE
from opencohost.ui.state import UIState

logger = logging.getLogger(__name__)


class AdvancedModePanel:
    """Manages the advanced mode panel with logs, actions, and Kira responses.

    The panel does NOT own top-level application state.  It reads/writes
    through the injected ``ui_state`` object and uses the ``dispatcher``
    for safe callback invocation.

    Usage::

        panel = AdvancedModePanel(
            parent_frame=root,
            ui_state=UIState(),
            dispatcher=CallbackDispatcher(source="AdvancedModePanel"),
            log_queue=log_queue,
            text_kira_response=kira_textbox,
            on_log_action=None,
            schedule_ui_update=lambda fn: root.after(0, fn),
        )
        frame = panel.build()
    """

    # Maximum log entries processed in a single tick (ADR-007 Phase 1).
    PROCESS_LOGS_CHUNK_LIMIT: int = 20

    # Debounce window for Kira token-stream writes, in milliseconds (ADR-007 Phase 2).
    # Accumulates incoming dialogue updates and flushes at most once per window,
    # converting O(tokens) full layout reflows into at most one per 75 ms.
    KIRA_FLUSH_INTERVAL_MS: int = 75

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    def __init__(
        self,
        parent_frame: ctk.CTkFrame,
        ui_state: UIState,
        dispatcher: Any,
        log_queue: queue.Queue,
        on_log_action: Optional[Callable[[str], None]],
        schedule_ui_update: Optional[Callable[[Callable[[], None]], None]] = None,
        schedule_ui_update_after: Optional[Callable[[int, Callable[[], None]], None]] = None,
        acciones_file: str = ACCIONES_LOG_FILE,
        text_kira_response: Optional[ctk.CTkTextbox] = None,
    ) -> None:
        """Initialize the advanced mode panel.

        Args:
            parent_frame: CTkFrame where the panel will be managed (show/hide).
            ui_state: Thread-safe UIState for reading/writing state.
            dispatcher: CallbackDispatcher for safe callback invocation.
            log_queue: Queue of log messages from other threads.
            on_log_action: Optional callback when an action is logged.
            schedule_ui_update: Function to schedule zero-delay UI updates (default: direct call).
            schedule_ui_update_after: Function to schedule a delayed UI update;
                signature ``(delay_ms: int, fn: Callable[[], None]) -> None``.
                Defaults to an immediate call (``fn()``), suitable for tests.
            acciones_file: Path to the actions log file.
            text_kira_response: Optional CTkTextbox for Kira response display.
        """
        self._parent_frame = parent_frame
        self._ui_state = ui_state
        self._dispatcher = dispatcher
        self._log_queue = log_queue
        self._on_log_action = on_log_action
        self._schedule_ui_update = schedule_ui_update or (lambda fn: fn())
        self._schedule_ui_update_after = schedule_ui_update_after or (lambda _delay, fn: fn())
        self._acciones_file = acciones_file
        self.text_kira_response = text_kira_response

        # Internal state
        self._logs_panel_visible: bool = False
        self._observer_id: int | None = None

        # Kira token-stream debounce state (ADR-007 Phase 2).
        # _kira_response_buffer holds the latest incoming content;
        # _kira_flush_pending is True while a scheduled flush is in flight.
        self._kira_response_buffer: str | None = None
        self._kira_flush_pending: bool = False

        # Widget references (set by build())
        self._frame: ctk.CTkFrame | None = None
        self.tabview: ctk.CTkTabview | None = None
        self.consola: ctk.CTkTextbox | None = None
        self.consola_acciones: ctk.CTkTextbox | None = None
        self.consola_youtube: ctk.CTkTextbox | None = None
        self.text_stream_admin_log: ctk.CTkTextbox | None = None

    # ------------------------------------------------------------------
    # Panel building
    # ------------------------------------------------------------------

    def build(self) -> ctk.CTkFrame:
        """Build the advanced mode panel UI.

        Creates the frame with tabview containing:
        - Log General (consola)
        - Kira Acciones (consola_acciones)
        - YT Chat (consola_youtube)
        - Stream Log (text_stream_admin_log)

        Returns:
            The created CTkFrame.
        """
        self._frame = ctk.CTkFrame(
            self._parent_frame,
            fg_color="#0f151c",
            corner_radius=18,
            height=260,
        )
        self._frame.grid(row=2, column=0, sticky="nsew", padx=12, pady=(0, 12))
        self._frame.grid_propagate(False)
        self._frame.grid_columnconfigure(0, weight=1)
        self._frame.grid_rowconfigure(1, weight=1)

        ctk.CTkLabel(
            self._frame,
            text="Logs / Terminales",
            font=ctk.CTkFont(size=15, weight="bold"),
            anchor="w",
        ).grid(row=0, column=0, sticky="ew", padx=12, pady=(10, 4))

        self.tabview = ctk.CTkTabview(
            self._frame,
            command=lambda: None,
            height=210,
        )
        self.tabview.grid(row=1, column=0, sticky="nsew", padx=10, pady=(0, 10))

        tab_log = self.tabview.add("Log General")
        tab_acciones = self.tabview.add("Kira Acciones")
        tab_youtube = self.tabview.add("YT Chat")
        tab_stream_log = self.tabview.add("Stream Log")

        self.consola = ctk.CTkTextbox(
            tab_log,
            font=ctk.CTkFont(family="Consolas", size=13),
            state="disabled",
        )
        self.consola.pack(fill="both", expand=True)

        self.consola_acciones = ctk.CTkTextbox(
            tab_acciones,
            font=ctk.CTkFont(family="Consolas", size=13),
            state="disabled",
        )
        self.consola_acciones.pack(fill="both", expand=True)

        self.consola_youtube = ctk.CTkTextbox(
            tab_youtube,
            font=ctk.CTkFont(family="Consolas", size=13),
            state="disabled",
        )
        self.consola_youtube.pack(fill="both", expand=True)

        self.text_stream_admin_log = ctk.CTkTextbox(
            tab_stream_log,
            font=ctk.CTkFont(family="Consolas", size=12),
            state="disabled",
        )
        self.text_stream_admin_log.pack(fill="both", expand=True)

        # Load existing actions
        self._load_acciones()

        # Subscribe to UIState observer for advanced_mode changes
        self._observer_id = self._ui_state.subscribe(self._on_state_change)

        return self._frame

    def _load_acciones(self) -> None:
        """Load existing actions from file and populate consola_acciones."""
        if self.consola_acciones is None:
            return

        acciones = self._cargar_acciones()
        for accion in acciones:
            self.append_to_textbox(self.consola_acciones, accion, max_lines=1000)

        # Demo messages if no actions exist
        if not acciones:
            mensajes_demo = [
                "🎮 [Sistema] Modo de juego detectado: Streaming",
                "📺 [Kira] Título del stream actualizado a 'Jugando con la IA'",
                "🔇 [Kira] Slow Mode activado (5 min)",
                "🎵 [Sistema] Audio de fondo: OFF",
                "📋 [Kira] Descripción actualizada en canal",
            ]
            for msg in mensajes_demo:
                self.append_to_textbox(self.consola_acciones, f"[demo] {msg}", max_lines=1000)

    # ------------------------------------------------------------------
    # Visibility control
    # ------------------------------------------------------------------

    def set_logs_visible(self, visible: bool) -> None:
        """Show or hide the logs panel.

        Args:
            visible: True to show, False to hide.
        """
        if self._frame is None:
            return
        if self._logs_panel_visible == visible:
            return

        self._logs_panel_visible = visible

        if visible:
            self._parent_frame.grid_rowconfigure(2, weight=0, minsize=260)
            self._frame.grid()
        else:
            self._parent_frame.grid_rowconfigure(2, weight=0, minsize=0)
            self._frame.grid_remove()

    def toggle_logs(self) -> None:
        """Toggle the logs panel visibility."""
        self.set_logs_visible(not self._logs_panel_visible)

    # ------------------------------------------------------------------
    # Log display
    # ------------------------------------------------------------------

    def print_log(self, msg: str) -> None:
        """Print a log message to the general console.

        Also updates the Kira response panel if the message contains
        "[Kira]:".

        Args:
            msg: Log message to display.
        """
        msg_str = str(msg)
        self.update_kira_response(msg_str)

        if not self._logs_panel_visible:
            return
        if self.consola is None:
            return

        self.append_to_textbox(self.consola, msg_str, max_lines=1500)

    def append_to_textbox(self, widget: ctk.CTkTextbox, line: Any, max_lines: int = 1000) -> None:
        """Append a line to a textbox, trimming excess lines.

        Args:
            widget: CTkTextbox to append to.
            line: Content to append (converted to string).
            max_lines: Maximum number of lines to keep.
        """
        self.append_batch_to_textbox(widget, [line], max_lines=max_lines)

    def append_batch_to_textbox(
        self, widget: ctk.CTkTextbox, lines: list, max_lines: int = 1000
    ) -> None:
        """Append a batch of lines to a textbox with a single layout pass.

        Layout queries (``index``) and viewport shifts (``see``) run once per
        batch instead of once per line, avoiding layout thrashing when log
        bursts arrive (ADR-007).

        Args:
            widget: CTkTextbox to append to.
            lines: Contents to append (each converted to string).
            max_lines: Maximum number of lines to keep.
        """
        if not lines:
            return
        batch_text = (
            "\n".join(str(line).replace("\r", " ").replace("\n", " ") for line in lines) + "\n"
        )
        widget.configure(state="normal")
        widget.insert("end", batch_text)
        try:
            total_lines = int(widget.index("end-1c").split(".")[0])
            excess = total_lines - int(max_lines)
            if excess > 0:
                widget.delete("1.0", f"{excess + 1}.0")
        except Exception:
            pass
        widget.see("end")
        widget.configure(state="disabled")

    # ------------------------------------------------------------------
    # Log queue processing
    # ------------------------------------------------------------------

    def process_logs(self) -> None:
        """Process pending log messages, bounded per GUI tick.

        Should be called periodically (e.g. via root.after(100, ...)).
        Processes at most ``PROCESS_LOGS_CHUNK_LIMIT`` messages, flushes the
        console in a single batched write, and reschedules itself when the
        queue still has items so the event loop never starves.
        """
        batch_messages: list[str] = []
        processed = 0
        while processed < self.PROCESS_LOGS_CHUNK_LIMIT:
            try:
                msg = self._log_queue.get_nowait()
            except queue.Empty:
                break
            processed += 1
            msg_str = str(msg)
            self.update_kira_response(msg_str)
            if self._logs_panel_visible and self.consola is not None:
                batch_messages.append(msg_str)

        if batch_messages:
            self.append_batch_to_textbox(self.consola, batch_messages, max_lines=1500)

        if processed >= self.PROCESS_LOGS_CHUNK_LIMIT and not self._log_queue.empty():
            self._schedule_ui_update(self.process_logs)

    # ------------------------------------------------------------------
    # Action logging
    # ------------------------------------------------------------------

    def log_action(self, msg: str) -> None:
        """Log an action to the Kira actions console and file.

        Args:
            msg: Action message to log.
        """
        ts = time.strftime("%H:%M:%S")
        entrada = f"[{ts}] {msg}"

        if self.consola_acciones is not None:
            self.append_to_textbox(self.consola_acciones, entrada, max_lines=1000)

        self._guardar_accion(msg)

        # Dispatch to any subscribers
        self._dispatcher.dispatch("on_log_action", msg)

        if self._on_log_action is not None:
            try:
                self._on_log_action(msg)
            except Exception as exc:
                logger.error("[AdvancedModePanel] on_log_action callback error: %s", exc)

    def _guardar_accion(self, msg: str) -> None:
        """Save an action to the actions log file.

        Args:
            msg: Action message to save.
        """
        try:
            os.makedirs(os.path.dirname(self._acciones_file), exist_ok=True)
            with open(self._acciones_file, "a", encoding="utf-8") as f:
                f.write(json.dumps({"ts": time.time(), "msg": msg}, ensure_ascii=False) + "\n")
            # Fix: audit/ui-security-perf-2026-05-17 — prevent unbounded action log growth
            self._rotate_acciones(max_lines=5000)
        except Exception as exc:
            logger.warning("[AdvancedModePanel] Failed to save action: %s", exc)

    def _rotate_acciones(self, max_lines: int = 5000) -> None:
        """Keep only the last max_lines entries in the action log file."""
        # Fix: audit/ui-security-perf-2026-05-17 — prevent unbounded action log growth
        try:
            with open(self._acciones_file, "r", encoding="utf-8") as f:
                lines = f.readlines()
            if len(lines) > max_lines:
                with open(self._acciones_file, "w", encoding="utf-8") as f:
                    f.writelines(lines[-max_lines:])
        except Exception:
            pass  # Non-critical — log rotation failure shouldn't break the app

    def _cargar_acciones(self, limite: int = 50) -> list[str]:
        """Load recent actions from the actions log file.

        Args:
            limite: Maximum number of recent actions to load.

        Returns:
            List of formatted action strings.
        """
        acciones = []
        try:
            if os.path.exists(self._acciones_file):
                with open(self._acciones_file, "r", encoding="utf-8") as f:
                    for line in f:
                        try:
                            entry = json.loads(line)
                            acciones.append(
                                f"[{time.strftime('%H:%M:%S', time.localtime(entry['ts']))}] {entry['msg']}"
                            )
                        except Exception:
                            continue
                return acciones[-limite:]
        except Exception as exc:
            logger.warning("[AdvancedModePanel] Failed to load actions: %s", exc)
        return []

    # ------------------------------------------------------------------
    # Kira response panel
    # ------------------------------------------------------------------

    def update_kira_response(self, msg: str) -> None:
        """Buffer a Kira response update and schedule a debounced flush.

        During LLM token streaming this method is called once per token.
        Instead of triggering a full delete/insert reflow on every call,
        we accumulate the latest content in a buffer and schedule a single
        flush after KIRA_FLUSH_INTERVAL_MS (ADR-007 Phase 2, FR2/AC3).

        While a flush is pending further calls only overwrite the buffer;
        no additional timers are created (coalescing).  When the flush fires
        it writes the most-recent buffered content in one pass and clears
        the pending flag so the next incoming update can schedule a new
        window.

        Args:
            msg: Message to check and potentially display.
        """
        if self.text_kira_response is None:
            return
        if "[Kira]:" not in msg:
            return

        response = msg.strip().replace("🧠 ", "")

        # Overwrite the buffer with the latest content.
        self._kira_response_buffer = response

        # If a flush is already scheduled, coalescing is enough — no second timer.
        if self._kira_flush_pending:
            return

        self._kira_flush_pending = True
        self._schedule_ui_update_after(self.KIRA_FLUSH_INTERVAL_MS, self._flush_kira_response)

    def _flush_kira_response(self) -> None:
        """Write the buffered Kira response to the textbox (called by the debounce timer).

        Performs a single delete/insert cycle for the most-recent content that
        accumulated in the buffer during the debounce window.  The equality
        guard (ADR-007 Phase 1) still short-circuits the reflow when the
        buffered content matches what is already displayed.
        """
        content = self._kira_response_buffer
        self._kira_response_buffer = None
        self._kira_flush_pending = False

        if content is None or self.text_kira_response is None:
            return

        # Skip redundant rewrites: identical content would only trigger a
        # full delete/insert layout reflow with no visible change (ADR-007).
        try:
            current_text = self.text_kira_response.get("1.0", "end-1c").strip()
            if current_text == content:
                return
        except Exception:
            pass

        self.text_kira_response.configure(state="normal")
        self.text_kira_response.delete("1.0", "end")
        self.text_kira_response.insert("end", content + "\n")
        self.text_kira_response.configure(state="disabled")

    # ------------------------------------------------------------------
    # UIState observer callback
    # ------------------------------------------------------------------

    def _on_state_change(self, key: str, value: Any) -> None:
        """UIState observer callback — handles advanced_mode changes."""
        if key == "advanced_mode":
            self._schedule_ui_update(lambda: self.set_logs_visible(bool(value)))

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def cleanup(self) -> None:
        """Unsubscribe from UIState observer and disarm pending debounce timers.

        Call this before the parent window is destroyed to prevent
        stale callbacks.  Clearing ``text_kira_response`` to ``None`` ensures
        that any debounce timer scheduled within the last 75 ms (KIRA_FLUSH_INTERVAL_MS)
        will hit the existing None guard in ``_flush_kira_response`` and exit early
        instead of calling ``configure`` on a destroyed widget.
        """
        if self._observer_id is not None:
            self._ui_state.unsubscribe(self._observer_id)
            self._observer_id = None

        # Clear the Kira textbox reference so any in-flight debounce timer
        # short-circuits harmlessly rather than operating on a destroyed widget.
        self.text_kira_response = None
        self._kira_flush_pending = False
        self._kira_response_buffer = None
