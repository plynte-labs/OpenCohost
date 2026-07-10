"""VocalAIApp shell — thin composition and wiring layer.
This module contains the ``VocalAIApp`` class that is ONLY responsible for:
- Creating UIState and all panel instances
- Wiring callbacks between panels
- Calling panel.build() methods
- Setting up window geometry
- Delegating cleanup to all panels
No UI construction code is inline — all delegated to panels.
"""
from __future__ import annotations
import json
#import logging
import os
import queue
import threading
#import time
from typing import Any, Optional
import customtkinter as ctk
import numpy as np
import sounddevice as sd
import soundfile as sf
from tkinter import filedialog, TclError
import tkinter.messagebox as messagebox
#from pynput import keyboard, mouse
from opencohost.ui.state import UIState
from opencohost.ui.crash_reporting import install_crash_handler
from opencohost.ui.protocols import CallbackDispatcher, SmartAggregatorCallbacks
from opencohost.ui.ptt_manager import PTTManager
from opencohost.ui.voice_control import VoiceControlPanel
from opencohost.ui.model_panel import ModelPanel
from opencohost.ui.profile_panel import ProfilePanel
from opencohost.ui.status_bar import StatusBar
from opencohost.ui.smart_aggregator_ui import SmartAggregatorUI
from opencohost.ui.stream_admin_ui import StreamAdminUI
from opencohost.ui.cohost_agenda_panel import CoHostAgendaPanel
from opencohost.ui.music_panel import MusicPanel
from opencohost.ui.advanced_panel import AdvancedModePanel
from opencohost.ui.profiles_window import ConfiguradorPerfiles
from opencohost.ui.window_utils import apply_app_icon
from opencohost.ui.avatar_panel import AvatarPanel
from opencohost.avatar.obs_client import OBSClient, OBSConfig
from opencohost.avatar.avatar_state import AvatarState, AvatarStateBridge
from opencohost.config.settings import (
    DEFAULT_MODEL, MODELS_CATALOG, BASE_DIR, PACKAGE_CONFIG_DIR, TEMP_DIR,
    RECORDING_DURATION, RECORDING_SAMPLERATE, MIN_AUDIO_RMS,
    PTT_DEFAULT_HOTKEY, PTT_HOTKEY_LIST, PTT_CONFIG_FILE,
    WINDOW_GEOMETRY_FILE, ACCIONES_LOG_FILE,
    EDITORIAL_CARDS_DB, REFERENCE_WAV_PATH,
    MEMORIAS_ENABLED,
    EXPERIMENTAL_HEAVY_TTS_ENABLED,
    STREAM_ADMIN_ENABLED,
    load_tts_local_only,
    PIPER_VOICES, load_piper_voice, piper_voice_path, DEFAULT_PIPER_VOICE,
    default_piper_voice_for_locale,
)
from opencohost.config.logger import get_logger
from opencohost.i18n import active as i18n_active
from opencohost.core.agenda_persistence import AgendaPersistence
from opencohost.core.topic_inbox import TopicInboxStore
from opencohost.ui.topic_inbox_bridge import TopicInboxBridge
from opencohost.ui.tts_speed_control import build_tts_speed_selector
from opencohost.ui import locale_control
from opencohost.core.profiles import cargar_perfiles, guardar_perfiles
from opencohost.core.cohost_profiles import load_cohost_profiles, save_cohost_profiles, normalize_cohost_profile, sanitize_profile_name
from opencohost.core.audio_bed import AudioBedEngine
from opencohost.core.editorial_agenda_bridge import EditorialAgendaBridge
from opencohost.core.editorial_cards import EditorialCard, EditorialCardStore
from opencohost.core.llm_engine import MotorVocalIA
from opencohost.core.health_monitor import HealthMonitor
from opencohost.core.temp_file_cleanup import cleanup_opencohost_temp_artifacts
from opencohost.core.music_library import MusicLibrary
from opencohost.smart_aggregator import AgendaAction, AgendaState, Aggregator, generate_suggestions, KiraAgendaController
logger = get_logger()
def _cargar_geometria() -> dict | None:
    try:
        if os.path.exists(WINDOW_GEOMETRY_FILE):
            with open(WINDOW_GEOMETRY_FILE, "r") as f:
                return json.load(f)
    except Exception:
        pass
    return None
def _guardar_geometria(x: int, y: int, w: int, h: int) -> None:
    try:
        os.makedirs(os.path.dirname(WINDOW_GEOMETRY_FILE), exist_ok=True)
        with open(WINDOW_GEOMETRY_FILE, "w") as f:
            json.dump({"x": x, "y": y, "width": w, "height": h}, f)
    except Exception:
        pass
class _EntryStub:
    """Stand-in for CTkEntry widgets removed from sidebar (moved to Stream Admin)."""
    def __init__(self, default: str = "") -> None:
        self._text = default
    def get(self) -> str:
        return self._text
    def delete(self, first: object, last: object = None) -> None:
        self._text = ""
    def insert(self, index: object, value: str) -> None:
        self._text = value
    def pack(self, **kw: object) -> None:
        pass
class _ButtonStub:
    """Stand-in for CTkButton widgets removed from sidebar."""
    def configure(self, **kw: object) -> None:
        pass
    def pack(self, **kw: object) -> None:
        pass
# ── Global crash handler: log unhandled exceptions before the process dies ──
install_crash_handler()


def _agenda_audio_deps(shell) -> dict:
    """Deps for agenda_audio_controller functions (Phase 7 extraction).

    Module-level function (NOT a method) so unbound
    VocalAIApp.<delegate>(SimpleNamespace) calls in test_audio_teardown keep
    working.  ``_d`` is the LIVE instance dict — the getter/setter lambdas
    below see later writes.  Shared state stays owned by the shell as instance
    attributes; agenda_audio_controller never duplicates it.

    Intra-cluster callbacks are the shell-bound delegates (resolved via
    _get_attr, instance-first) so instance-level MagicMock stubs in existing
    tests keep intercepting — same contract as _motor_handler_deps.
    """
    _d = vars(shell)
    g = VocalAIApp._get_attr  # object.__getattribute__ with default (no CTk recursion)
    return dict(
        # ── collaborators (snapshot per call, like _motor_handler_deps) ──
        kira_agenda=_d.get("kira_agenda"),
        motor_ia=_d.get("motor_ia"),
        smart_agg=_d.get("smart_agg"),
        smart_agg_ui=_d.get("smart_agg_ui"),
        audio_bed=_d.get("audio_bed"),
        ui_state=_d.get("_ui_state"),
        status_bar=_d.get("status_bar"),
        stream_admin_ui=_d.get("stream_admin_ui"),
        cohost_agenda_panel=_d.get("cohost_agenda_panel"),
        agenda_persistence=_d.get("_agenda_persistence"),
        get_topic_inbox_bridge=lambda: _d.get("_topic_inbox_bridge"),  # set later by _on_agenda_loaded
        # Injected (not imported by the module) so tests that patch
        # app_shell.generate_suggestions keep working (obs_client_cls precedent).
        generate_suggestions=generate_suggestions,
        # Worker thread factory resolved from app_shell.threading at deps-build
        # time so tests that patch app_shell.threading (e.g. the _SyncThread shim
        # in test_topic_scout_integration) still drive the idle-suggestion worker
        # synchronously after the tick body moved to agenda_audio_controller.
        thread_cls=threading.Thread,
        # ── marshaling / timers (frozen-boundary callables) ──
        schedule_ui_update=g(shell, "_safe_after"),
        after=g(shell, "after"),
        after_cancel=g(shell, "after_cancel"),
        # ── logging / operator ──
        print_log=g(shell, "_print_log"),
        log_accion=g(shell, "_log_accion"),
        stream_admin_log=g(shell, "_on_stream_admin_log"),
        notify_operator=g(shell, "_notify_operator"),
        # ── shell services that STAY on VocalAIApp ──
        dispatch_audio_play=g(shell, "_dispatch_audio_play"),
        clear_obs_joyita=g(shell, "_clear_obs_joyita"),
        is_kira_agenda_speech_source=g(shell, "_is_kira_agenda_speech_source"),
        # ── intra-cluster callbacks: ALWAYS the shell-bound delegates ──
        kira_agenda_tick=g(shell, "_kira_agenda_tick"),
        kira_agenda_schedule_tick=g(shell, "_kira_agenda_schedule_tick"),
        kira_agenda_update_status=g(shell, "_kira_agenda_update_status"),
        enqueue_kira_agenda_action=g(shell, "_enqueue_kira_agenda_action"),
        kira_agenda_force_strict_chat_filter=g(shell, "_kira_agenda_force_strict_chat_filter"),
        kira_agenda_restore_chat_filter=g(shell, "_kira_agenda_restore_chat_filter"),
        kira_agenda_has_higher_priority_pending=g(shell, "_kira_agenda_has_higher_priority_pending"),
        kira_agenda_has_non_agenda_audio_work=g(shell, "_kira_agenda_has_non_agenda_audio_work"),
        kira_agenda_clear_prefetch=g(shell, "_kira_agenda_clear_prefetch"),
        kira_agenda_play_prefetched_if_ready=g(shell, "_kira_agenda_play_prefetched_if_ready"),
        check_pending_audio_bed_stop=g(shell, "_check_pending_audio_bed_stop"),
        dispatch_suggestion_recompute=g(shell, "_dispatch_suggestion_recompute"),
        apply_idle_suggestions=g(shell, "_apply_idle_suggestions"),
        # ── shared state: LIVE getter/setter pairs over the instance dict ──
        is_closing=lambda: _d.get("_closing", False),
        get_pending_audio_bed_stop=lambda: _d.get("_pending_audio_bed_stop", False),
        set_pending_audio_bed_stop=lambda v: _d.__setitem__("_pending_audio_bed_stop", v),
        get_kira_agenda_tick_id=lambda: _d.get("_kira_agenda_tick_id"),
        set_kira_agenda_tick_id=lambda v: _d.__setitem__("_kira_agenda_tick_id", v),
        get_prefetch_retry_id=lambda: _d.get("_prefetch_retry_id"),
        set_prefetch_retry_id=lambda v: _d.__setitem__("_prefetch_retry_id", v),
        get_prefetched_action=lambda: _d.get("_kira_agenda_prefetched_action"),
        set_prefetched_action=lambda v: _d.__setitem__("_kira_agenda_prefetched_action", v),
        get_idle_ticks=lambda: _d.get("_idle_ticks", 0),
        set_idle_ticks=lambda v: _d.__setitem__("_idle_ticks", v),
        get_suggestion_gen=lambda: _d.get("_suggestion_gen", 0),
        set_suggestion_gen=lambda v: _d.__setitem__("_suggestion_gen", v),
        get_pending_compact_chat=lambda: _d.get("_kira_agenda_pending_compact_chat", ""),
        set_pending_compact_chat=lambda v: _d.__setitem__("_kira_agenda_pending_compact_chat", v),
        # save-once guard — preserves hasattr/delattr semantics exactly:
        has_previous_filter_policy=lambda: "_kira_agenda_previous_filter_policy" in _d,
        get_previous_filter_policy=lambda: _d.get("_kira_agenda_previous_filter_policy"),
        set_previous_filter_policy=lambda v: _d.__setitem__("_kira_agenda_previous_filter_policy", v),
        clear_previous_filter_policy=lambda: _d.pop("_kira_agenda_previous_filter_policy", None),
        get_agenda_was_paused=lambda: _d.get("_agenda_was_paused", False),
        set_agenda_was_paused=lambda v: _d.__setitem__("_agenda_was_paused", v),
    )


def _agenda_audio_call(shell, fn: str, **kw):
    """Call agenda_audio_controller.<fn> with the injected deps (Phase 7)."""
    import opencohost.ui.agenda_audio_controller as _a  # lazy import (obs/motor precedent)
    return getattr(_a, fn)(**_agenda_audio_deps(shell), **kw)


class VocalAIApp(ctk.CTk):
    """Thin composition layer — delegates all work to panel modules."""
    def __init__(self) -> None:
        super().__init__()
        self.title("Kira — OpenCohost")
        apply_app_icon(self)  # OpenCohost logo instead of CustomTkinter's default feather icon
        geo = _cargar_geometria()
        if geo:
            try:
                self.geometry(f"{geo['width']}x{geo['height']}+{geo['x']}+{geo['y']}")
            except Exception:
                self.geometry("1280x800")
        else:
            self.geometry("1280x800")
        self.minsize(800, 500)
        self.log_queue = queue.Queue()
        self._ui_task_queue = queue.Queue()
        self._run_startup_janitor()
        self.dispositivo_seleccionado: int | None = None
        self._modo_compacto: bool = False
        self._compacto_active: bool = False  # Bug C fix: product workspace visible at startup (ADR-SD-002 2026-06-25)
        self._logs_visible_active: bool = False  # logs hidden by default; toggled via gear popover
        self._ptt_accept_logged: bool = False
        self._logs_panel_visible: bool = False  # compact-is-default: logs start hidden
        self._closing: bool = False
        self._motor_started: bool = False
        self._motor_heartbeat_failure_reported: bool = False
        self._kira_first_ready: bool = False  # one-shot "Kira lista" cue in the response box
        self._kira_avatar_preview_after_id: Any = None
        self.perfiles = cargar_perfiles()
        self.cohost_profiles = load_cohost_profiles()
        self._current_cohost_profile = "Natural" if "Natural" in self.cohost_profiles else next(iter(self.cohost_profiles), "")
        self.music_library = MusicLibrary()
        self.music_library.load()
        self.audio_bed = AudioBedEngine(self.music_library, on_log=lambda msg: self._print_log(msg))
        self.editorial_cards = EditorialCardStore(EDITORIAL_CARDS_DB)
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)
        self.lista_dispositivos = self._obtener_dispositivos_entrada()
        # Shared UIState
        self._ui_state = UIState()
        # Fix: audit/ui-security-perf-2026-05-17 — store subscription ID for cleanup.
        # Without unsubscribe, UIState holds a reference to VocalAIApp preventing GC
        # after destroy(). Same pattern used by StatusBar, ModelPanel, etc.
        self._ui_state_sub_id = self._ui_state.subscribe(self._on_ui_state_change)
        # Callback dispatchers
        self._model_dispatcher = CallbackDispatcher(source="ModelPanel")
        self._model_dispatcher.subscribe("on_switch_model", lambda tag: self.motor_ia.command_queue.put(("switch_model", tag)))
        self._model_dispatcher.subscribe("on_switch_llm_tier", lambda tier: self.motor_ia.command_queue.put(("switch_llm_tier", tier)))
        self._model_dispatcher.subscribe("on_download_model", lambda tag: self.motor_ia.command_queue.put(("download_model", tag)))
        self._profile_dispatcher = CallbackDispatcher(source="ProfilePanel")
        self._profile_dispatcher.subscribe("on_set_profile", lambda p: self.motor_ia.command_queue.put(("set_profile", p)))
        self._smart_agg_dispatcher = CallbackDispatcher(source="SmartAggregatorUI")
        self._smart_agg_dispatcher.set_protocol(SmartAggregatorCallbacks)
        # PTT Manager
        self.ptt = PTTManager(logger=logger)
        self.ptt.set_status_callback(self._on_ptt_status_change)
        self.ptt.set_state_callback(
            lambda estado: self._safe_after(lambda: self._actualizar_pipeline(estado))
        )
        self.ptt.set_log_callback(lambda msg: self._print_log(msg))
        # Avatar state bridge (independent from Tkinter)
        self._avatar_bridge = AvatarStateBridge()
        self._speaking_alt_timer_id: str | None = None
        self._speaking_is_alt: bool = False
        self._inactivity_timer_id: str | None = None
        self._joyita_obs_timer_id: str | None = None
        self._inactivity_timeout_ms: int = 2 * 60 * 1000  # 2 minutes to sleeping
        # OBS WebSocket client (initialized after UI build to access config)
        self._obs_client: Optional[OBSClient] = None
        self._obs_retry_thread: threading.Thread | None = None
        self._obs_retry_cancel: threading.Event | None = None
        # ── Build UI structure ──
        self._build_ui()
        # Motor IA (deferred until mainloop is running to avoid
        # "main thread is not in main loop" race condition)
        self.motor_ia = MotorVocalIA(self.log_queue, self._on_motor_event)
        self.kira_agenda = KiraAgendaController()
        self.editorial_agenda = EditorialAgendaBridge(self.editorial_cards, self.kira_agenda)
        self.editorial_agenda.register_provider()
        self.motor_ia.direct_editorial_context_provider = self.editorial_agenda.resolve_direct_context
        self.motor_ia.agenda_output_validator = self.kira_agenda.accept_output
        self.motor_ia.agenda_output_preview_validator = self.kira_agenda.preview_accept_output
        self.motor_ia.agenda_output_recorder = self._record_accepted_kira_agenda_output
        self.motor_ia.agenda_output_transformer = self.kira_agenda.enforce_live_safety_cap
        self.motor_ia.agenda_controller = self.kira_agenda            # Phase 0: metrics access
        # Health Monitor — system health daemon (graceful if init fails)
        self.health_monitor: HealthMonitor | None = None
        try:
            self.health_monitor = HealthMonitor()
            self.motor_ia.health_monitor = self.health_monitor  # wire for TTS fallback
        except Exception as e:
            logger.warning(f"HealthMonitor init failed (non-fatal): {e}")
        if self._current_cohost_profile:
            self.kira_agenda.set_profile(self.cohost_profiles.get(self._current_cohost_profile, {}))
        self._kira_agenda_tick_id: str | None = None
        self._kira_agenda_prefetched_action: AgendaAction | None = None
        self._prefetch_retry_id: str | None = None  # fix #B: non-blocking prefetch poll guard
        self._idle_ticks: int = 0
        self._suggestion_gen: int = 0  # FR2: supersession counter for idle recompute
        self._kira_agenda_pending_compact_chat: str = ""
        self._agenda_persistence = AgendaPersistence(EDITORIAL_CARDS_DB, log_fn=self._on_stream_admin_log)
        # fix #C: load agenda off main thread to avoid sqlite blocking startup.
        # _topic_inbox_bridge is None until _on_agenda_loaded fires.
        self._topic_inbox_bridge = None
        import threading as _threading
        def _load_agenda_worker():
            try:
                self._agenda_persistence.load_into(self.kira_agenda)
            except Exception:
                logger.exception("Agenda load failed; starting with empty agenda")
            self._safe_after(self._on_agenda_loaded, delay_ms=0)
        _threading.Thread(target=_load_agenda_worker, daemon=True).start()
        self.after(100, self._start_motor)
        # Wire motor_ia to voice control panel
        if hasattr(self, "voice_panel"):
            self.voice_panel.set_motor_ia(self.motor_ia)
        # Initialize smart aggregator
        self._init_smart_aggregator()
        # Start log and cross-thread UI task processing
        self.after(50, self._process_ui_tasks)
        self.after(100, self._process_logs)
        self.after(500, self._aplicar_perfil_actual)
        # Drive the PTT physical key-state reconcile (self.ptt exists since :159).
        # Single perpetual after(250); removing this line disables the whole
        # missed-key-up feature (kill switch).
        self.after(250, self._ptt_reconcile_tick)
        self._print_log(f"[Sistema] PTT hotkey cargada: {self.ptt.hotkey}")
        logger.info("Aplicación OpenCohost iniciada.")

    def _on_agenda_loaded(self) -> None:
        """Called on the main thread after the daemon thread finishes load_into.

        Runs all operations that depend on the loaded agenda state:
        session settings, TopicInboxBridge construction, polling start, and
        initial status render.  Guards against teardown with _closing check.
        """
        if self.__dict__.get("_closing", False):
            return
        self.cohost_agenda_panel.apply_session_settings(
            self.kira_agenda.max_turns_per_topic,
            self.kira_agenda.rhythm,
            self.kira_agenda.response_length,
            self.kira_agenda.safety_mode,
        )
        self._topic_inbox_bridge = TopicInboxBridge(
            TopicInboxStore(EDITORIAL_CARDS_DB),
            log_fn=self._on_stream_admin_log,
            persist_fn=lambda: self._agenda_persistence.save_if_changed(self.kira_agenda),
        )
        # TODO(pipeline_memory_followups): TopicInboxBridge.start_polling has no stop
        # mechanism — polling continues until the process exits.  This is a pre-existing
        # teardown concern; route to the pipeline_memory_followups track before adding
        # a graceful shutdown path.
        self._topic_inbox_bridge.start_polling(
            lambda fn, delay: self._safe_after(fn, delay),
            self._kira_agenda_update_status,
        )
        # Render the restored queue (needs the bridge above): without this
        # refresh the panel keeps its empty built state and the restore is
        # invisible to the operator.
        self._kira_agenda_update_status()

    def _start_motor(self) -> None:
        """Start the IA motor thread after mainloop is running.
        Deferring this avoids 'main thread is not in main loop' errors
        when the motor calls ui_callback before Tkinter's mainloop starts.
        """
        self.motor_ia.start()
        self._motor_started = True
        # Start health monitor daemon
        if self.health_monitor:
            # Try to attach to manually-started Qwen server first
            try:
                self.health_monitor.qwen_manager.attach_existing()
            except Exception as e:
                logger.debug(f"HealthMonitor: could not attach existing server: {e}")
            self.health_monitor.start()
        # Begin UI health status polling and passive motor heartbeat checks.
        self._poll_health_status()
    def _run_startup_janitor(self) -> None:
        """Recover only known OpenCohost temp leftovers from a previous run."""
        try:
            stats = cleanup_opencohost_temp_artifacts(TEMP_DIR, logger, min_age_seconds=60.0)
        except Exception as exc:
            logger.warning("Startup temp janitor failed: %s", exc)
            return
        if stats.get("removed"):
            logger.info("Startup temp janitor removed %s legacy app artifact(s)", stats["removed"])
    def _poll_health_status(self) -> None:
        """Poll health monitor state and push to UI state (thread-safe via after)."""
        if self.health_monitor and not getattr(self, "_motor_heartbeat_failure_reported", False):
            try:
                state = self.health_monitor.state
                self._ui_state.health_status = state.overall_status
            except Exception:
                pass
        self._check_motor_heartbeat()
        # Reschedule every 2 seconds for UI responsiveness
        self.after(2000, self._poll_health_status)

    def _ptt_reconcile_tick(self) -> None:
        """Drive PTTManager's physical key-state reconcile from the Tk main thread.

        Perpetual after(250) loop. When the PTT key is held but its key-up event
        was dropped, the reconcile re-injects the release here — on the main
        thread, which is strictly safer than the pynput thread for UI mutation.
        Costs one branch when idle and one GetAsyncKeyState call only while held.

        [D5] Teardown-guarded: a tick can fire once during Tk teardown and touch
        self.ptt after destroy (TclError). The _closing guard ends the loop
        cleanly; the try/except blocks are belt-and-suspenders so a teardown race
        or transient probe error never crashes the loop or the app.
        """
        if getattr(self, "_closing", False):
            return
        try:
            ptt = getattr(self, "ptt", None)
            if ptt is not None:
                ptt._reconcile_step()
        except Exception:
            # Teardown race (TclError) or transient probe error — never crash.
            pass
        if not getattr(self, "_closing", False):
            try:
                self.after(250, self._ptt_reconcile_tick)
            except (RuntimeError, TclError):
                pass

    def _check_motor_heartbeat(self) -> None:
        """Surface an operator-visible warning if MotorVocalIA dies after startup."""
        motor = getattr(self, "motor_ia", None)
        if (
            motor is None
            or not getattr(self, "_motor_started", False)
            or getattr(self, "_closing", False)
            or getattr(self, "_motor_heartbeat_failure_reported", False)
        ):
            return
        try:
            alive = motor.is_alive()
        except Exception:
            logger.exception("No se pudo verificar el heartbeat del motor de Kira")
            return
        if alive:
            return
        self._motor_heartbeat_failure_reported = True
        logger.critical("Kira's engine thread died unexpectedly; UI remains open but Kira is offline")
        try:
            self._ui_state.health_status = "red"
        except Exception:
            logger.exception("No se pudo marcar health_status tras fallo de MotorVocalIA")
        try:
            self._print_log("[CRITICO] El motor de Kira se detuvo inesperadamente. Kira esta offline; reinicia la app.")
        except Exception:
            logger.exception("No se pudo informar en UI el fallo de MotorVocalIA")
    def _on_ui_state_change(self, key: str, value: Any) -> None:
        if key == "ws_connected":
            def update_btn():
                if hasattr(self, "btn_ws"):
                    if value:
                        self.btn_ws.configure(text="Desconectar LiveAudio", fg_color="darkred")
                    else:
                        self.btn_ws.configure(text="Conectar LiveAudio", fg_color="#555555")
            self.after(0, update_btn)
    def _on_avatar_state_for_preview(self, state: AvatarState) -> None:
        """Update the left-panel avatar preview when bridge state changes."""
        previous_after_id = getattr(self, "_kira_avatar_preview_after_id", None)
        if previous_after_id is not None:
            try:
                self.after_cancel(previous_after_id)
            except Exception:
                logger.debug("No se pudo cancelar update pendiente de preview de avatar", exc_info=True)
            finally:
                self._kira_avatar_preview_after_id = None
        def update():
            self._kira_avatar_preview_after_id = None
            if self._kira_avatar_label is None or not self._kira_avatar_label.winfo_exists():
                return
            from opencohost.avatar.avatar_config import load_avatar_config
            config = load_avatar_config()
            image_path = config.get_image_for_state(state.value)
            if image_path and os.path.isfile(image_path):
                try:
                    from PIL import Image
                    img = Image.open(image_path)
                    # Portrait Kira PNGs: HEIGHT binds Image.thumbnail() (the 440 width
                    # is never reached). Enlarge via the 2nd value AND the label height to
                    # avoid crop; this static fit trades fill for no-crop (responsive
                    # <Configure> zoom deferred to the track's sdd-proposal).
                    img.thumbnail((440, 200), Image.Resampling.LANCZOS)
                    # Keep BOTH references alive to prevent Tkinter image GC.
                    # CTkImage wraps the PIL Image but doesn't always hold a
                    # strong reference to it — if the PIL Image is collected,
                    # Tkinter raises "image pyimageN doesn't exist".
                    self._kira_avatar_pil = img
                    ctk_img = ctk.CTkImage(light_image=img, dark_image=img, size=img.size)
                    self._kira_avatar_ref = ctk_img
                    if state.value == "idle" or image_path == config.state_images.get("idle"):
                        self._kira_avatar_idle_pil = img
                        self._kira_avatar_idle_ref = ctk_img
                    self._kira_avatar_label.configure(image=ctk_img, text="")
                except Exception as e:
                    if not self._show_cached_idle_avatar_preview():
                        self._kira_avatar_pil = None
                        self._kira_avatar_ref = None
                        self._kira_avatar_label.configure(
                            image=None,
                            text=f"Error al cargar avatar: {state.value}",
                            text_color="#aa5555",
                        )
                    self._print_log(f"[Avatar] No se pudo cargar preview '{state.value}' desde {image_path}: {e}")
            else:
                if not self._show_cached_idle_avatar_preview():
                    self._kira_avatar_pil = None
                    self._kira_avatar_ref = None
                    self._kira_avatar_label.configure(
                        image=None,
                        text=f"Sin imagen para: {state.value}",
                        text_color="#6b7b8d",
                    )
                self._print_log(f"[Avatar] Sin imagen configurada o accesible para '{state.value}'")
        self._kira_avatar_preview_after_id = self.after(0, update)
    def _show_cached_idle_avatar_preview(self) -> bool:
        """Keep a known idle avatar visible when a transient state cannot load."""
        idle_ref = getattr(self, "_kira_avatar_idle_ref", None)
        label = getattr(self, "_kira_avatar_label", None)
        if not idle_ref or label is None:
            return False
        self._kira_avatar_pil = getattr(self, "_kira_avatar_idle_pil", None)
        self._kira_avatar_ref = idle_ref
        label.configure(image=idle_ref, text="")
        return True
    # ──────────────────────────────────────────────
    # UI Construction — delegates to panels
    # ──────────────────────────────────────────────
    def _build_ui(self) -> None:
        self.configure(fg_color="#0b0f14")
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=0)
        self.grid_rowconfigure(1, weight=1)
        self.grid_rowconfigure(2, weight=0, minsize=0)
        self.grid_rowconfigure(3, weight=0, minsize=0)
        self.grid_rowconfigure(4, weight=0, minsize=0)
        self.grid_rowconfigure(5, weight=0, minsize=0)
        # Status bar
        status_bar_frame = ctk.CTkFrame(self, fg_color="#111820", corner_radius=14)
        status_bar_frame.grid(row=0, column=0, sticky="ew", padx=12, pady=(12, 8))
        self.status_bar = StatusBar(status_bar_frame, self._ui_state, schedule_ui_update=self._safe_after)
        self.status_bar.create_status_pills()
        self.lbl_status = self.status_bar.lbl_status
        # RF4 pills (lbl_oauth_status_pill, lbl_memory_status_pill,
        # lbl_moderation_status_pill) and the Mostrar-logs / Compacto switches
        # have been moved to the gear popover (⚙) — see _open_gear_popover().
        # The lbl_author brand link also lives in the popover.
        # stream_admin_ui.py accesses the RF4 pills via self._widget() which
        # returns None when not registered → the existing if-guards are safe no-ops.
        # _limpiar_historial uses hasattr(self, "lbl_memory_status_pill") → also safe.

        # Gear button — opens the settings popover
        self.btn_gear = ctk.CTkButton(
            status_bar_frame,
            text="⚙",
            width=32,
            height=28,
            fg_color="#1a2535",
            hover_color="#253347",
            font=ctk.CTkFont(size=14),
            command=self._open_gear_popover,
        )
        self.btn_gear.pack(side="right", padx=(4, 12), pady=8)
        self._gear_popover: Any = None

        # Product shell: Kira stays visible on the left; configuration and
        # stream operations live on the right.  Phase 2 only moves containers,
        # not callbacks or business logic.
        app_shell = ctk.CTkFrame(self, fg_color="transparent")
        app_shell.grid(row=1, column=0, sticky="nsew", padx=12, pady=(0, 8))
        app_shell.grid_columnconfigure(0, weight=0, minsize=460)
        app_shell.grid_columnconfigure(1, weight=1)
        app_shell.grid_rowconfigure(0, weight=1)
        # Persistent Kira panel — pinned to a fixed width via grid_propagate(False)
        # so inner content (long Kira replies, the per-state strings written into
        # text_kira_response) can't inflate the card and shift the column.
        main_panel = ctk.CTkFrame(app_shell, fg_color="#10161d", corner_radius=18, width=560)
        main_panel.grid(row=0, column=0, sticky="nsew", padx=(0, 8), pady=0)
        main_panel.grid_columnconfigure(0, weight=1)
        main_panel.grid_rowconfigure(0, weight=1)
        main_panel.grid_propagate(False)
        main_content = ctk.CTkFrame(main_panel, fg_color="transparent")
        main_content.grid(row=0, column=0, sticky="nsew", padx=16, pady=16)
        main_content.grid_columnconfigure(0, weight=1)
        main_content.grid_rowconfigure(0, weight=1)
        tab_main_kira = ctk.CTkFrame(main_content, fg_color="transparent")
        tab_main_kira.grid(row=0, column=0, sticky="nsew")
        self._main_view_buttons: dict[str, Any] = {}
        self._main_view_frames = {"Kira": tab_main_kira}
        tab_main_kira.grid_columnconfigure(0, weight=1)
        tab_main_kira.grid_rowconfigure(1, weight=3)  # avatar is the visual hero
        tab_main_kira.grid_rowconfigure(2, weight=0)
        tab_main_kira.grid_rowconfigure(3, weight=0)  # response hugs its content, no wasted gap
        # Kira header
        kira_header = ctk.CTkFrame(tab_main_kira, fg_color="transparent")
        kira_header.grid(row=0, column=0, sticky="ew", padx=16, pady=(16, 8))
        kira_header.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(kira_header, text="Kira", font=ctk.CTkFont(size=22, weight="bold"), anchor="w").grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(kira_header, text="Experiencia principal", text_color="#8fa3b8", anchor="e").grid(row=0, column=1, sticky="e")
        # Avatar preview in left Kira panel
        self._kira_avatar_label: ctk.CTkLabel | None = None
        self._kira_avatar_ref: Any = None
        # radius 16 unifies the 4 sibling cards (voice/bottom were already 16); not a
        # theme token (scale is 8/12/18) — full theme-token sweep deferred to the
        # customtkinter_visual_refinement track, kept literal like the rest of this file.
        avatar_preview_frame = ctk.CTkFrame(tab_main_kira, fg_color="#0c1117", corner_radius=16)
        avatar_preview_frame.grid(row=1, column=0, sticky="nsew", padx=16, pady=(0, 16))
        avatar_preview_frame.grid_columnconfigure(0, weight=1)
        avatar_preview_frame.grid_rowconfigure(0, weight=1)
        self._kira_avatar_label = ctk.CTkLabel(
            avatar_preview_frame, text="",
            text_color="#6b7b8d",
            font=ctk.CTkFont(size=12),
            height=280, #Alejamiento Avatar
            corner_radius=8,
        )
        self._kira_avatar_label.grid(row=0, column=0, sticky="nsew", padx=8, pady=8)
        # Subscribe avatar bridge to update left-panel preview
        self._avatar_bridge.subscribe(self._on_avatar_state_for_preview)
        # subscribe() only registers for FUTURE state changes, and the bridge defaults
        # to IDLE so a same-state set_state() would no-op — force one initial render of
        # the current state so the avatar shows at startup (was blank until the first real
        # pipeline transition). Mirrors AvatarPanel.build()'s one-shot _update_preview().
        self._on_avatar_state_for_preview(self._avatar_bridge.get_state())
        # Primary action button — Hablar (prominent, at Kira level)
        self._primary_speak_btn = ctk.CTkButton(
            tab_main_kira,
            text="Hablar",
            command=None,  # Wired after VoiceControlPanel is created
            state="disabled",
            height=40,
            font=ctk.CTkFont(size=16, weight="bold"),
            fg_color="#1f7a5a",
            hover_color="#24946c",
        )
        self._primary_speak_btn.grid(row=2, column=0, sticky="ew", padx=16, pady=(0, 16))
        # Kira response: compact scrollable panel so the avatar remains the hero.
        kira_response_shell = ctk.CTkFrame(tab_main_kira, fg_color="#0c1117", corner_radius=16)
        kira_response_shell.grid(row=3, column=0, sticky="ew", padx=16, pady=(0, 16))
        kira_response_shell.grid_columnconfigure(0, weight=1)
        kira_response_shell.grid_rowconfigure(1, weight=0)
        ctk.CTkLabel(kira_response_shell, text="Respuesta de Kira", font=ctk.CTkFont(size=13, weight="bold"), text_color="#d8e2ef", anchor="w").grid(row=0, column=0, sticky="ew", padx=16, pady=(12, 4))
        self.text_kira_response = ctk.CTkTextbox(kira_response_shell, font=ctk.CTkFont(size=14), fg_color="#090d12", border_width=1, border_color="#1f2b38", state="disabled", height=100, wrap="word")
        self.text_kira_response.grid(row=1, column=0, sticky="ew", padx=16, pady=(0, 16))
        self.text_kira_response.configure(state="normal")
        self.text_kira_response.insert("end", "Iniciando Kira… esperá unos segundos mientras se prepara.\n")
        self.text_kira_response.configure(state="disabled")
        # Voice panel (compact — primary button is at Kira level)
        voice_panel_frame = ctk.CTkFrame(tab_main_kira, fg_color="#121d27", corner_radius=16)
        voice_panel_frame.grid(row=4, column=0, sticky="ew", padx=16, pady=(0, 16))
        voice_panel_frame.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(voice_panel_frame, text="Entrada de voz / PTT", font=ctk.CTkFont(size=12, weight="bold"), text_color="#d8e2ef").grid(row=0, column=0, sticky="w", padx=12, pady=(6, 2))
        self.voice_panel = VoiceControlPanel(
            parent_frame=voice_panel_frame,
            ui_state=self._ui_state,
            logger=logger,
            on_log=self._print_log,
            on_motor_event=self._on_motor_event,
            on_pipeline_change=self._actualizar_pipeline,
            dispositivo_seleccionado=self.dispositivo_seleccionado,
            schedule_ui_update=self._safe_after,
            external_primary_button=self._primary_speak_btn,
        )
        self.voice_panel.create_voice_panel()
        # Expose widgets for backward compatibility
        self.lbl_kira_voice_state = self.voice_panel.lbl_kira_voice_state
        self.lbl_kira_tts_state = self.voice_panel.lbl_kira_tts_state
        self.lbl_kira_memory_state = self.voice_panel.lbl_kira_memory_state
        self.lbl_kira_chat_state = self.voice_panel.lbl_kira_chat_state
        self.lbl_voice_hint = self.voice_panel.lbl_voice_hint
        self.btn_primary_voice = self.voice_panel.btn_primary_voice
        # Wire primary button to VoiceControlPanel's toggle
        self._primary_speak_btn.configure(command=self.voice_panel._toggle_websocket)
        # Chat entry — integrated into the voice card (same card as the state pills,
        # no separate floating card / whitespace gap).
        frame_bottom = ctk.CTkFrame(voice_panel_frame, fg_color="transparent")
        frame_bottom.grid(row=3, column=0, sticky="ew", padx=12, pady=(4, 10))
        frame_bottom.grid_columnconfigure(0, weight=1)
        self.entry_chat = ctk.CTkEntry(frame_bottom, placeholder_text="Escribe un mensaje para Kira (contexto o pregunta)...", height=40, corner_radius=10, fg_color="#0c1117", border_width=1, border_color="#1f2b38")
        self.entry_chat.grid(row=0, column=0, sticky="ew", padx=(0, 8), pady=0)
        self.entry_chat.bind("<Return>", lambda e: self._enviar_contexto_manual())
        self.btn_enviar = ctk.CTkButton(frame_bottom, text="Enviar a IA", command=self._enviar_contexto_manual, width=120, height=40, corner_radius=10, state="disabled", fg_color="#2f5f8f", hover_color="#3670aa")
        self.btn_enviar.grid(row=0, column=1, padx=0, pady=0)
        # Product workspace: current configuration plus full Stream Admin.
        side_panel = ctk.CTkFrame(app_shell, fg_color="#0f151c", corner_radius=18)
        side_panel.grid(row=0, column=1, sticky="nsew", padx=(8, 0), pady=0)
        side_panel.grid_columnconfigure(0, weight=1)
        side_panel.grid_rowconfigure(3, weight=1)
        # Product tabs — custom buttons (full-width, clear active state)
        product_tab_bar = ctk.CTkFrame(side_panel, fg_color="transparent")
        product_tab_bar.grid(row=1, column=0, sticky="ew", padx=10, pady=(6, 6))
        for col in range(5):
            product_tab_bar.grid_columnconfigure(col, weight=1, uniform="product_tab")
        product_content = ctk.CTkFrame(side_panel, fg_color="#0f151c", corner_radius=18)
        product_content.grid(row=3, column=0, sticky="nsew", padx=10, pady=(0, 12))
        product_content.grid_columnconfigure(0, weight=1)
        product_content.grid_rowconfigure(0, weight=1)
        self._product_tab_data: dict[str, dict] = {}
        self._active_product_tab: str = "config"
        TAB_DEFS = [
            ("config", "Configuración"),
            ("stream", "Stream"),
            ("cohost", "Co-host"),
            ("music", "Música"),
            ("avatar", "Avatar / OBS"),
        ]
        # Icons are prepended at render only — the (key, label) tuples stay clean
        # (source-guard tests pin them) and _switch_product_tab keys off `key`, not text.
        TAB_ICONS = {"config": "⚙", "stream": "📡", "cohost": "🤖", "music": "🎵", "avatar": "🎭"}
        # Render each tab icon as a small color-emoji IMAGE placed with compound="left" so it
        # vertically CENTERS with the label. Inline emoji text sits below the letters because
        # Tk's emoji font-fallback baseline differs from the label font. Safe fallback: if the
        # emoji font / Pillow color rendering isn't available, revert to an inline-text prefix.
        self._tab_icon_refs: list = []
        try:
            from PIL import ImageFont as _ImageFont
            _emoji_font = _ImageFont.truetype("C:/Windows/Fonts/seguiemj.ttf", 18)
        except Exception:
            _emoji_font = None  # non-Windows / font missing → tabs fall back to a text prefix

        def _tab_icon(emoji: str):
            if not emoji or _emoji_font is None:
                return None
            try:
                from PIL import Image, ImageDraw
                _im = Image.new("RGBA", (22, 22), (0, 0, 0, 0))
                ImageDraw.Draw(_im).text((2, 1), emoji, font=_emoji_font, embedded_color=True)
                _ci = ctk.CTkImage(light_image=_im, dark_image=_im, size=(18, 18))
                self._tab_icon_refs.append(_ci)
                return _ci
            except Exception:
                return None

        for idx, (key, label) in enumerate(TAB_DEFS):
            is_active = (key == self._active_product_tab)
            _icon_img = _tab_icon(TAB_ICONS.get(key, ""))
            btn = ctk.CTkButton(
                product_tab_bar,
                text=(label if _icon_img else f"{TAB_ICONS.get(key, '')} {label}".strip()),
                image=_icon_img,
                compound="left",
                font=ctk.CTkFont(size=13, weight="bold"),
                fg_color="#2f5f8f" if is_active else "#151d26",
                hover_color="#3670aa" if is_active else "#1d2a38",
                text_color="#ffffff" if is_active else "#6b7b8d",
                corner_radius=8,
                height=36,
            )
            btn.grid(row=0, column=idx, sticky="ew", padx=2, pady=2)
            btn.configure(command=lambda k=key: self._switch_product_tab(k))
            self._product_tab_data[key] = {"button": btn}
        # Sub-tab container — same module as product tabs, shows context-specific subtabs
        sub_tab_container = ctk.CTkFrame(side_panel, fg_color="transparent")
        sub_tab_container.grid(row=2, column=0, sticky="ew", padx=10, pady=(4, 0))
        sub_tab_container.grid_columnconfigure(0, weight=1)
        # Config sub-tabs (visible when "Configuración" is active)
        cfg_sub_bar = ctk.CTkFrame(sub_tab_container, fg_color="transparent")
        cfg_sub_bar.grid(row=0, column=0, sticky="ew", padx=2, pady=0)
        for col in range(3):
            cfg_sub_bar.grid_columnconfigure(col, weight=1, uniform="cfg_subtab")
        self._cfg_subtab_data: dict[str, dict] = {}
        self._active_cfg_subtab: str = "modelo_perfil"
        CFG_SUBTABS = [("modelo_perfil", "M/Perfil"), ("audio_tts", "Audio/TTS"), ("ayuda", "Ayuda")]
        for idx, (key, label) in enumerate(CFG_SUBTABS):
            is_active = (key == self._active_cfg_subtab)
            btn = ctk.CTkButton(cfg_sub_bar, text=label,
                font=ctk.CTkFont(size=11, weight="bold"),
                fg_color="#1e4060" if is_active else "#0f151c",
                hover_color="#255478" if is_active else "#151d26",
                text_color="#d8e2ef" if is_active else "#6b7b8d",
                corner_radius=6, height=28)
            btn.grid(row=0, column=idx, sticky="ew", padx=2, pady=0)
            btn.configure(command=lambda k=key: self._switch_cfg_subtab(k))
            self._cfg_subtab_data[key] = {"button": btn}
        # Stream sub-tabs (visible when "Stream" is active, initially hidden)
        stream_sub_bar = ctk.CTkFrame(sub_tab_container, fg_color="transparent")
        stream_sub_bar.grid(row=0, column=0, sticky="ew", padx=2, pady=0)
        stream_sub_bar.grid_remove()
        for col in range(2):
            stream_sub_bar.grid_columnconfigure(col, weight=1, uniform="stream_subtab")
        self._stream_subtab_data: dict[str, dict] = {}
        self._active_stream_subtab: str = "emision"
        STREAM_SUBTABS = [("emision", "Emisión"), ("acciones", "Acciones")]
        for idx, (key, label) in enumerate(STREAM_SUBTABS):
            is_active = (key == self._active_stream_subtab)
            btn = ctk.CTkButton(stream_sub_bar, text=label,
                font=ctk.CTkFont(size=11, weight="bold"),
                fg_color="#1e4060" if is_active else "#0f151c",
                hover_color="#255478" if is_active else "#151d26",
                text_color="#d8e2ef" if is_active else "#6b7b8d",
                corner_radius=6, height=28)
            btn.grid(row=0, column=idx, sticky="ew", padx=2, pady=0)
            btn.configure(command=lambda k=key: self._switch_stream_subtab(k))
            self._stream_subtab_data[key] = {"button": btn}
        # Store sub-bar references for visibility toggling
        self._cfg_sub_bar = cfg_sub_bar
        self._stream_sub_bar = stream_sub_bar
        # Content frames — only active one visible
        tab_product_config = ctk.CTkFrame(product_content, fg_color="transparent")
        tab_product_config.grid(row=0, column=0, sticky="nsew", padx=0, pady=0)
        tab_product_config.grid_columnconfigure(0, weight=1)
        tab_product_config.grid_rowconfigure(0, weight=1)
        self._product_tab_data["config"]["frame"] = tab_product_config
        tab_product_stream = ctk.CTkFrame(product_content, fg_color="transparent")
        tab_product_stream.grid(row=0, column=0, sticky="nsew", padx=0, pady=0)
        tab_product_stream.grid_columnconfigure(0, weight=1)
        tab_product_stream.grid_rowconfigure(0, weight=1)
        tab_product_stream.grid_remove()
        self._product_tab_data["stream"]["frame"] = tab_product_stream
        tab_product_cohost = ctk.CTkFrame(product_content, fg_color="transparent")
        tab_product_cohost.grid(row=0, column=0, sticky="nsew", padx=0, pady=0)
        tab_product_cohost.grid_columnconfigure(0, weight=1)
        tab_product_cohost.grid_rowconfigure(0, weight=1)
        tab_product_cohost.grid_remove()
        self._product_tab_data["cohost"]["frame"] = tab_product_cohost
        tab_product_music = ctk.CTkFrame(product_content, fg_color="transparent")
        tab_product_music.grid(row=0, column=0, sticky="nsew", padx=0, pady=0)
        tab_product_music.grid_columnconfigure(0, weight=1)
        tab_product_music.grid_rowconfigure(0, weight=1)
        tab_product_music.grid_remove()
        self._product_tab_data["music"]["frame"] = tab_product_music
        tab_product_avatar = ctk.CTkFrame(product_content, fg_color="transparent")
        tab_product_avatar.grid(row=0, column=0, sticky="nsew", padx=0, pady=0)
        tab_product_avatar.grid_columnconfigure(0, weight=1)
        tab_product_avatar.grid_rowconfigure(0, weight=1)
        tab_product_avatar.grid_remove()
        self._product_tab_data["avatar"]["frame"] = tab_product_avatar
        # Config content frames — directly in tab_product_config (sub-tabs at side_panel level)
        tab_cfg_model_profile = ctk.CTkScrollableFrame(tab_product_config, fg_color="transparent", scrollbar_button_color="#2f5f8f", scrollbar_button_hover_color="#3670aa")
        tab_cfg_model_profile.grid(row=0, column=0, sticky="nsew", padx=0, pady=0)
        tab_cfg_model_profile.grid_columnconfigure(0, weight=1)
        tab_cfg_model_profile.grid_columnconfigure(1, weight=1)
        self._cfg_subtab_data["modelo_perfil"]["frame"] = tab_cfg_model_profile
        tab_cfg_audio_voice = ctk.CTkScrollableFrame(tab_product_config, fg_color="transparent", scrollbar_button_color="#2f5f8f", scrollbar_button_hover_color="#3670aa")
        tab_cfg_audio_voice.grid(row=0, column=0, sticky="nsew", padx=0, pady=0)
        tab_cfg_audio_voice.grid_columnconfigure(0, weight=1)
        tab_cfg_audio_voice.grid_remove()
        self._cfg_subtab_data["audio_tts"]["frame"] = tab_cfg_audio_voice
        tab_cfg_admin = ctk.CTkFrame(tab_product_config, fg_color="transparent")
        tab_cfg_admin.grid(row=0, column=0, sticky="nsew", padx=0, pady=0)
        tab_cfg_admin.grid_columnconfigure(0, weight=1)
        tab_cfg_admin.grid_remove()
        self._cfg_subtab_data["ayuda"]["frame"] = tab_cfg_admin
        tab_cfg_admin.grid_rowconfigure(0, weight=1)
        # Model panel
        frame_model = ctk.CTkFrame(tab_cfg_model_profile, fg_color="#151d26", corner_radius=14)
        frame_model.grid(row=0, column=0, sticky="nsew", padx=(8, 4), pady=8)
        self.model_panel = ModelPanel(
            parent_frame=frame_model,
            ui_state=self._ui_state,
            dispatcher=self._model_dispatcher,
            on_log=self._print_log,
            schedule_ui_update=self._safe_after,
            on_check_ollama=lambda: self.motor_ia.command_queue.put(("check_ollama", None)),
        )
        self.model_panel.build()
        self.combo_modelos = self.model_panel.combo_modelos
        self.btn_download = self.model_panel.btn_download
        self.lbl_modelo_info = self.model_panel.lbl_modelo_info
        self.progress_download = self.model_panel.progress_download
        self._model_display_to_tag = self.model_panel._model_display_to_tag
        self._model_tag_to_display = self.model_panel._model_tag_to_display
        self.after(250, self.model_panel.refresh_ollama_state)
        # Profile panel — stacked with the PTT card in a right-column wrapper so the two
        # short cards fill the column height instead of leaving a big empty gap under Perfil.
        frame_right = ctk.CTkFrame(tab_cfg_model_profile, fg_color="transparent")
        frame_right.grid(row=0, column=1, sticky="nsew", padx=(4, 8), pady=8)
        frame_right.grid_columnconfigure(0, weight=1)
        frame_right.grid_rowconfigure(1, weight=1)  # PTT stretches so the right column's bottom aligns with the taller Modelo column
        frame_profile = ctk.CTkFrame(frame_right, fg_color="#151d26", corner_radius=14)
        frame_profile.grid(row=0, column=0, sticky="ew", padx=0, pady=(0, 16))
        # memoria_store_getter is a DEFERRED callable: self.motor_ia is created AFTER
        # _build_ui() (see __init__ order, motor_ia assigned ~line 183), so we must not
        # access it at build time. The lambda resolves motor_ia only when the getter is
        # actually invoked (on an explicit profile delete, long after init).
        self.profile_panel = ProfilePanel(parent_frame=frame_profile, ui_state=self._ui_state, dispatcher=self._profile_dispatcher, on_log=self._print_log, configurador_class=ConfiguradorPerfiles, schedule_ui_update=self._safe_after, memoria_store_getter=((lambda: self.motor_ia._get_memoria_store()) if MEMORIAS_ENABLED else None))
        self.profile_panel.set_profiles(self.perfiles)
        self.profile_panel.build()
        self.combo_perfiles = self.profile_panel.combo_perfiles
        self.btn_editar_perfiles = self.profile_panel.btn_editar_perfiles
        # Read-only product inspectors — Tarjetas editoriales + Memoria de Kira
        # (cards_memory_readonly_panels_20260701). Shared mini-frame, fail-open counts.
        from opencohost.ui.inspector_cards import format_launcher_label
        frame_inspectors_launcher = ctk.CTkFrame(frame_right, fg_color="transparent")
        frame_inspectors_launcher.grid(row=2, column=0, sticky="ew", padx=0, pady=(8, 0))
        self._editorial_cards_inspector: Any = None
        try:
            _cards_count = len(self.editorial_cards.list_all())
        except Exception:
            _cards_count = None
        self.btn_editorial_cards = ctk.CTkButton(
            frame_inspectors_launcher,
            text=format_launcher_label(_cards_count),
            command=self._open_inspector_cards,
        )
        self.btn_editorial_cards.pack(fill="x", pady=(0, 4))
        from opencohost.ui.inspector_memory import format_memory_launcher_label
        self._kira_memory_inspector: Any = None
        self.btn_kira_memory = ctk.CTkButton(
            frame_inspectors_launcher,
            text=format_memory_launcher_label(None),  # motor_ia does not exist yet at build time
            command=self._open_inspector_memory,
        )
        self.btn_kira_memory.pack(fill="x")
        # Audio tab
        frame_audio = ctk.CTkFrame(tab_cfg_audio_voice, fg_color="#151d26", corner_radius=14)
        frame_audio.grid(row=0, column=0, sticky="ew", padx=8, pady=8)
        # Reference-voice cluster (mic device selector + record/load WAV) — heavy-TTS
        # ONLY. It exists solely to provide a reference voice for Qwen3-TTS
        # (motor=pesado): the recorded/loaded WAV feeds voz_referencia, read only on
        # the heavy path. Mic capture for LiveVoice/PTT happens in the external STT
        # server, not here. Widgets are always CREATED (attributes always exist) but
        # only shown when the experimental heavy path is opted in; otherwise hidden.
        # (qwen_tts_extirpation_20260627 WU 1.2.)
        lbl_dispositivo = ctk.CTkLabel(frame_audio, text="Dispositivo de audio", font=ctk.CTkFont(size=13, weight="bold"), anchor="w")
        self.combo_dispositivos = ctk.CTkOptionMenu(frame_audio, values=self.lista_dispositivos, command=self._al_seleccionar_dispositivo, width=300)
        if self.lista_dispositivos:
            self.combo_dispositivos.set(self.lista_dispositivos[0])
            self.dispositivo_seleccionado = int(self.lista_dispositivos[0].split(":")[0])
            if self.status_bar:
                self.status_bar.update_mic_status("idle")
        else:
            self.combo_dispositivos.set("Sin dispositivos de audio")
            if self.status_bar:
                self.status_bar.update_mic_status("disconnected")
        audio_buttons = ctk.CTkFrame(frame_audio, fg_color="transparent")
        self.btn_grabar = ctk.CTkButton(audio_buttons, text="🎤 Grabar", command=self._iniciar_grabacion, state="disabled", width=90, fg_color="#555555", hover_color="#666666")
        self.btn_voz = ctk.CTkButton(audio_buttons, text="📂 Cargar WAV", command=self._cargar_voz, state="disabled", fg_color="#555555", width=110)
        if EXPERIMENTAL_HEAVY_TTS_ENABLED:
            lbl_dispositivo.pack(fill="x", padx=10, pady=(10, 4))
            self.combo_dispositivos.pack(fill="x", padx=10, pady=4)
            audio_buttons.pack(fill="x", padx=10, pady=4)
            self.btn_grabar.pack(side="left", expand=True, fill="x", padx=(0, 4))
            self.btn_voz.pack(side="left", expand=True, fill="x", padx=(4, 0))
        # LiveAudio section
        frame_liveaudio = ctk.CTkFrame(frame_audio, fg_color="#101923", corner_radius=10)
        frame_liveaudio.pack(fill="x", padx=10, pady=(8, 0))
        ctk.CTkLabel(
            frame_liveaudio,
            text="LiveAudio es el motor que permite a Kira escuchar tu voz en tiempo real.",
            font=ctk.CTkFont(size=10),
            text_color="#8fa3b8",
            anchor="w",
            justify="left",
            wraplength=400,
        ).pack(fill="x", padx=10, pady=(8, 4))
        self.btn_ws = ctk.CTkButton(
            frame_liveaudio, text="Conectar LiveAudio",
            command=self._toggle_websocket,
            fg_color="#2f5f8f", hover_color="#3670aa",
            state="disabled",
        )
        self.btn_ws.pack(fill="x", padx=10, pady=(0, 10))
        # Voz de Kira card  (raw hex kept for parity with this un-migrated file;
        # #151d26 == theme.SURFACE, #101923 == theme.SURFACE_NESTED, #8fa3b8 == theme.TEXT_DIM — future token sweep)
        frame_tts_memory = ctk.CTkFrame(tab_cfg_audio_voice, fg_color="#151d26", corner_radius=14)
        frame_tts_memory.grid(row=1, column=0, sticky="ew", padx=8, pady=8)
        ctk.CTkLabel(frame_tts_memory, text="Voz de Kira", font=ctk.CTkFont(size=13, weight="bold"), anchor="w").pack(fill="x", padx=10, pady=(10, 6))
        # Two fully-offline Piper voices (Argentina ↔ Neutral). Selecting one forces
        # local Piper synthesis so the change is audible immediately and never
        # depends on the Edge-TTS cloud (demo-safe). The recessed sub-frame groups
        # the voice picker with its privacy escape hatch (one decision, one place).
        frame_voice = ctk.CTkFrame(frame_tts_memory, fg_color="#101923", corner_radius=10)
        frame_voice.pack(fill="x", padx=10, pady=(4, 8))
        self._kira_voice_labels = {info["label"]: key for key, info in PIPER_VOICES.items()}
        self.seg_kira_voice = ctk.CTkSegmentedButton(
            frame_voice,
            values=list(self._kira_voice_labels.keys()),
            command=self._on_kira_voice_change,
        )
        self.seg_kira_voice.pack(fill="x", padx=10, pady=(10, 2))
        _saved_voice = load_piper_voice(
            default=default_piper_voice_for_locale(i18n_active.get_active_bundle().code)
        )
        _saved_label = next(
            (lbl for lbl, key in self._kira_voice_labels.items() if key == _saved_voice),
            next(iter(self._kira_voice_labels), ""),
        )
        if _saved_label:
            self.seg_kira_voice.set(_saved_label)
        ctk.CTkLabel(frame_voice, text="Ambas voces son 100% offline (Piper).", font=ctk.CTkFont(size=10), text_color="#8fa3b8", anchor="w", justify="left", wraplength=400).pack(fill="x", padx=10, pady=(0, 6))
        # Privacy switch: when ON, Edge-TTS is never invoked — all light synthesis uses Piper.
        self.switch_local_only = ctk.CTkSwitch(frame_voice, text="Solo TTS local (Piper)", onvalue=True, offvalue=False, command=self._al_cambiar_tts_local_only)
        self.switch_local_only.pack(fill="x", padx=10, pady=(4, 0))
        if load_tts_local_only():
            self.switch_local_only.select()
        ctk.CTkLabel(frame_voice, text="OFF envía el texto a Edge-TTS (nube) para una voz más natural.", font=ctk.CTkFont(size=10), text_color="#8fa3b8", anchor="w", justify="left", wraplength=400).pack(fill="x", padx=10, pady=(0, 10))
        # Heavy-TTS switch stays CREATED + selected so the attribute always exists,
        # but it is only shown (packed) when the experimental heavy path is enabled.
        self.switch_modo_ligero = ctk.CTkSwitch(frame_tts_memory, text="🎛️ TTS: Ligero", onvalue="ligero", offvalue="pesado", command=self._al_cambiar_motor_tts)
        self.switch_modo_ligero.select()
        if EXPERIMENTAL_HEAVY_TTS_ENABLED:
            self.switch_modo_ligero.pack(fill="x", padx=10, pady=4)
            ctk.CTkLabel(frame_tts_memory, text="Ligero: rápido, usa Edge-TTS (cloud). Pesado: Qwen3-TTS local, requiere descarga previa del modelo.", font=ctk.CTkFont(size=10), text_color="#8fa3b8", anchor="w", justify="left", wraplength=400).pack(fill="x", padx=10, pady=(0, 6))
        else:
            # Heavy-TTS is experimental; hidden in packaged builds.
            # Engine-side gates and auto-fallback remain active regardless.
            self.switch_modo_ligero.configure(state="disabled")
        # Velocidad group — its own recessed sub-frame so it reads as a distinct section
        # under "Voz de Kira", parallel to the voice-selection group (frame_voice) above,
        # instead of floating loose in the card.
        frame_speed = ctk.CTkFrame(frame_tts_memory, fg_color="#101923", corner_radius=10)
        frame_speed.pack(fill="x", padx=10, pady=(0, 10))
        self.tts_speed_selector = build_tts_speed_selector(frame_speed, lambda scale: self.motor_ia.command_queue.put(("set_tts_speed", scale)))
        locale_control.mount(frame_tts_memory)  # Idioma group (extracted, kira_bilingual_e2e_20260705 P7)
        # Memoria card — separate concern from voice; clearing wipes conversation context only.
        frame_memory = ctk.CTkFrame(tab_cfg_audio_voice, fg_color="#151d26", corner_radius=14)
        frame_memory.grid(row=2, column=0, sticky="ew", padx=8, pady=8)
        ctk.CTkLabel(frame_memory, text="Memoria", font=ctk.CTkFont(size=13, weight="bold"), anchor="w").pack(fill="x", padx=10, pady=(10, 6))
        self.btn_clear = ctk.CTkButton(frame_memory, text="🗑️ Limpiar Memoria", command=self._limpiar_historial, width=130, fg_color="#555555", hover_color="#777777")
        self.btn_clear.pack(fill="x", padx=10, pady=(4, 10))
        ctk.CTkLabel(frame_memory, text="Kira olvidará el contexto previo de la conversación.", font=ctk.CTkFont(size=10), text_color="#8fa3b8", anchor="w", justify="left", wraplength=400).pack(fill="x", padx=10, pady=(0, 10))
        # PTT controls live with model/profile because they configure how Kira is operated.
        frame_ptt = ctk.CTkFrame(frame_right, fg_color="#151d26", corner_radius=14)
        frame_ptt.grid(row=1, column=0, sticky="nsew", padx=0, pady=0)
        ctk.CTkLabel(frame_ptt, text="PTT (Push-to-Talk)", font=ctk.CTkFont(size=13, weight="bold"), anchor="w").pack(fill="x", padx=10, pady=(10, 2))
        ctk.CTkLabel(frame_ptt, text="Activá PTT y mantené presionada la tecla para hablar. Soltá para que Kira procese y responda. Con PTT OFF, Kira escucha continuamente (modo LiveAudio).", font=ctk.CTkFont(size=10), text_color="#8fa3b8", anchor="w", justify="left", wraplength=400).pack(fill="x", padx=10, pady=(0, 4))
        # Compact single row: the toggle + key config together (no separate switch row,
        # no dead gap in the middle).
        ptt_hotkey_row = ctk.CTkFrame(frame_ptt, fg_color="transparent")
        ptt_hotkey_row.pack(fill="x", padx=10, pady=4)
        self.switch_ptt = ctk.CTkSwitch(ptt_hotkey_row, text="PTT OFF", command=self._al_toggle_ptt, onvalue=True, offvalue=False)
        self.switch_ptt.pack(side="left", padx=(0, 16))
        ctk.CTkLabel(ptt_hotkey_row, text="Tecla:", font=ctk.CTkFont(size=12)).pack(side="left", padx=(0, 6))
        self.lbl_hotkey = ctk.CTkLabel(ptt_hotkey_row, text=self.ptt.hotkey, font=ctk.CTkFont(size=13, weight="bold"))
        self.lbl_hotkey.pack(side="left", padx=(0, 8))
        self.btn_mapear = ctk.CTkButton(ptt_hotkey_row, text="Mapear", command=self._mapear_hotkey, width=70, fg_color="#555555", hover_color="#666666")
        self.btn_mapear.pack(side="left", padx=3)
        self.lbl_ptt_status = ctk.CTkLabel(frame_ptt, text="", font=ctk.CTkFont(size=12), text_color="#888888", anchor="w", justify="left")
        self.lbl_ptt_status.pack(fill="x", padx=10, pady=(0, 10))
        # Ayuda tab — contextual help for each product tab
        # YouTube chat controls moved to Stream Admin > Acciones > Chat Live (RF3).
        # These stubs preserve backward compat with existing methods that reference them.
        self.entry_youtube_video = _EntryStub()
        self.btn_youtube_chat = _ButtonStub()
        self.entry_youtube_user_limit = _EntryStub("10")
        self.entry_youtube_threshold = _EntryStub("1.0")
        # Backward compat stubs (stream_admin_ui references them via _widget lookup; skips when None)
        self.lbl_oauth_side_status = None
        self.lbl_moderation_side_status = None
        # Ayuda tab — collapsible help cards per section
        frame_ayuda = ctk.CTkScrollableFrame(tab_cfg_admin, fg_color="transparent")
        frame_ayuda.grid(row=0, column=0, sticky="nsew", padx=0, pady=0)
        frame_ayuda.grid_columnconfigure(0, weight=1)

        # Keep the Vista card at the top
        frame_view = ctk.CTkFrame(frame_ayuda, fg_color="#151d26", corner_radius=14)
        frame_view.grid(row=0, column=0, sticky="ew", padx=8, pady=8)
        ctk.CTkLabel(frame_view, text="Vista", font=ctk.CTkFont(size=13, weight="bold"), anchor="w").pack(fill="x", padx=10, pady=(10, 4))
        self.switch_logs = ctk.CTkSwitch(frame_view, text="Registrar logs en avanzado", onvalue=True, offvalue=False)
        self.switch_logs.pack(fill="x", padx=10, pady=(4, 10))
        self.switch_logs.select()

        # Help sections — one per product tab, collapsible
        help_sections = [
            ("Configuración", "Modelo/Perfil: elegí el modelo LLM (Ollama) y el perfil de personalidad de Kira.\n\nAudio/TTS: seleccioná el dispositivo de entrada y la voz de Kira (Argentina o Neutral) con el selector «Voz de Kira». Activá «Solo TTS local (Piper)» para mantener toda la síntesis de voz en tu máquina; por defecto está desactivado y la voz ligera usa Edge-TTS (nube).\n\nPTT (Push-to-Talk): mantené presionada la tecla asignada para hablar; soltá para que Kira responda."),
            ("Stream", "Emisión: conectá tu cuenta de YouTube (OAuth), gestioná metadata del stream (título, categoría, tags, descripción).\n\nAcciones: monitoreá el chat en vivo (Chat Live) para que Kira reaccione a tu audiencia."),
            ("Co-host", "Creá una agenda de temas aprobados para que Kira los desarrolle en vivo. Importá temas en lote desde texto estructurado. Controlá la sesión: Activar, Stop suave, Emergencia."),
            ("Música", "Importá loops de audio .mp3/.wav etiquetados por mood (Normal, Calmo, Épico, etc.). Probá cada mood con un clic. El sistema hace fade, ducking y fallback automático."),
            ("Avatar / OBS", "Cambiá la imagen de Kira para cada estado (idle, hablando, escuchando, etc.). Conectá con OBS Studio vía WebSocket para reflejar los cambios en vivo."),
        ]

        self._help_expanded = {}
        row_num = 1
        for title, description in help_sections:
            key = title.lower().replace(" ", "_").replace("/", "_")

            # Card frame
            card = ctk.CTkFrame(frame_ayuda, fg_color="#151d26", corner_radius=14)
            card.grid(row=row_num, column=0, sticky="ew", padx=8, pady=(0, 8))
            card.grid_columnconfigure(0, weight=1)

            # Toggle header
            btn = ctk.CTkButton(
                card, text=f"▶ {title}",
                anchor="w",
                fg_color="transparent", text_color="#d8e2ef",
                hover_color="#1d2a38",
                font=ctk.CTkFont(size=13, weight="bold"),
            )
            btn.grid(row=0, column=0, sticky="ew", padx=10, pady=(8, 4))

            # Content frame
            content = ctk.CTkFrame(card, fg_color="#101923", corner_radius=12)
            content.grid(row=1, column=0, sticky="ew", padx=10, pady=(0, 10))
            content.grid_columnconfigure(0, weight=1)
            content.grid_remove()

            # Use CTkTextbox instead of CTkLabel — guarantees text is always visible.
            # Height is sized to the content (approx wrapped lines) so an expanded section
            # shows the FULL help text with no nested scroll; the outer scroll frame handles
            # overflow. ~60 chars/line is a deliberately conservative estimate (slightly over-
            # sizes rather than clipping); tune if a section renders with a visible scrollbar.
            _help_paras = description.split("\n\n")
            _help_lines = sum(max(1, (len(p) + 59) // 60) for p in _help_paras) + (len(_help_paras) - 1)
            textbox = ctk.CTkTextbox(
                content,
                font=ctk.CTkFont(size=11),
                fg_color="#101923",
                text_color="#a9bdd3",
                wrap="word",
                height=_help_lines * 20 + 20,
                border_width=0,
            )
            textbox.grid(row=0, column=0, sticky="ew", padx=8, pady=8)
            textbox.insert("end", description)
            textbox.configure(state="disabled")

            self._help_expanded[key] = False
            btn.configure(command=lambda k=key, c=content, b=btn: self._toggle_help_section(k, c, b))
            row_num += 1

        # Stream Admin panel — internals preserved, only the container moved
        # into the Stream product workspace.
        stream_admin_panel = ctk.CTkFrame(tab_product_stream, fg_color="#0f151c", corner_radius=18)
        stream_admin_panel.grid(row=0, column=0, sticky="nsew", padx=0, pady=0)
        stream_admin_panel.grid_columnconfigure(0, weight=1)
        stream_admin_panel.grid_rowconfigure(0, weight=1)

        self.stream_admin_ui = StreamAdminUI(
            ui_state=self._ui_state,
            dispatcher=CallbackDispatcher(source="StreamAdminUI"),
            on_log=self._on_stream_admin_log,
            schedule_ui_update=self._safe_after,
        )
        self.stream_admin_ui.build(stream_admin_panel)
        # Wire stream content frames to side_panel sub-tabs
        self._stream_subtab_data["emision"]["frame"] = self.stream_admin_ui.tab_stream_live
        self._stream_subtab_data["acciones"]["frame"] = self.stream_admin_ui.tab_stream_actions
        self._wire_stream_admin_callbacks()

        cohost_panel_frame = ctk.CTkFrame(tab_product_cohost, fg_color="#0f151c", corner_radius=18)
        cohost_panel_frame.grid(row=0, column=0, sticky="nsew", padx=0, pady=0)
        cohost_panel_frame.grid_columnconfigure(0, weight=1)
        cohost_panel_frame.grid_rowconfigure(0, weight=1)
        self.cohost_agenda_panel = CoHostAgendaPanel(
            on_add_topic=lambda title, angle, constraints, priority, length, turns: self._kira_agenda_add_topic(title, angle, constraints, priority, length, turns),
            on_remove_topic=lambda index: self._kira_agenda_remove_topic(index),
            on_move_topic=lambda index, direction: self._kira_agenda_move_topic(index, direction),
            on_select_profile=lambda name: self._kira_agenda_select_profile(name),
            on_save_profile=lambda name, style, priority, length: self._kira_agenda_save_profile(name, style, priority, length),
            on_session_settings=lambda turns, rhythm, length, safety_mode: self._kira_agenda_set_session_settings(turns, rhythm, length, safety_mode),
            on_enable=lambda: self._kira_agenda_enable(),
            on_soft_stop=lambda: self._kira_agenda_soft_stop(),
            on_emergency_stop=lambda: self._kira_agenda_emergency_stop(),
            on_approve_suggestion=lambda topic_id: self._kira_agenda_approve_suggestion(topic_id),
            on_reject_suggestion=lambda topic_id: self._kira_agenda_reject_suggestion(topic_id),
            # Phase 3 batch rendering: delayed yield between suggestion chunks
            # routed through _safe_after (main-thread and cross-thread safe).
            schedule_ui_update_after=lambda delay, fn: self._safe_after(fn, delay),
        )
        self.cohost_agenda_panel.build(cohost_panel_frame)
        self.cohost_agenda_panel.set_profiles(self.cohost_profiles, self._current_cohost_profile)

        music_panel_frame = ctk.CTkFrame(tab_product_music, fg_color="#0f151c", corner_radius=18)
        music_panel_frame.grid(row=0, column=0, sticky="nsew", padx=0, pady=0)
        music_panel_frame.grid_columnconfigure(0, weight=1)
        music_panel_frame.grid_rowconfigure(0, weight=1)
        self.music_panel = MusicPanel(
            on_import=lambda mood: self._music_import_track(mood),
            on_play_mood=lambda mood: self._music_play_mood(mood),
            on_stop=lambda: self._music_stop(),
            on_cleanup_missing=lambda: self._music_cleanup_missing(),
            on_delete_track=lambda track_id: self._music_delete_track(track_id),
        )
        self.music_panel.build(music_panel_frame)
        self._music_update_panel()

        # Avatar / OBS panel
        avatar_panel_frame = ctk.CTkFrame(tab_product_avatar, fg_color="#0f151c", corner_radius=18)
        avatar_panel_frame.grid(row=0, column=0, sticky="nsew", padx=0, pady=0)
        avatar_panel_frame.grid_columnconfigure(0, weight=1)
        avatar_panel_frame.grid_rowconfigure(0, weight=1)

        self._avatar_panel = AvatarPanel(
            parent_frame=avatar_panel_frame,
            on_log=lambda msg: self._print_log(msg),
            schedule_ui_update=self._safe_after,
            on_obs_enable=lambda: self._obs_start_from_config(),
            on_obs_disable=lambda: self._obs_stop_runtime(),
            on_obs_connect=lambda: self._obs_connect_now(),
        )
        self._avatar_panel.build()
        self._avatar_panel.set_state_bridge(self._avatar_bridge)

        # OBS WebSocket client — initialize and connect if enabled
        self._init_obs_client()

        # Advanced panel
        self._advanced_panel = AdvancedModePanel(
            parent_frame=self,
            ui_state=self._ui_state,
            dispatcher=CallbackDispatcher(source="AdvancedModePanel"),
            log_queue=self.log_queue,
            text_kira_response=self.text_kira_response,
            on_log_action=None,
            schedule_ui_update=self._safe_after,
            # Phase 2 debounce: routes delayed callbacks through _safe_after which
            # handles both main-thread (self.after) and cross-thread (task queue) paths.
            schedule_ui_update_after=lambda delay, fn: self._safe_after(fn, delay),
        )
        self._advanced_mode_panel = self._advanced_panel.build()
        self.consola = self._advanced_panel.consola
        self.consola_acciones = self._advanced_panel.consola_acciones
        self.consola_youtube = self._advanced_panel.consola_youtube
        self.text_stream_admin_log = self._advanced_panel.text_stream_admin_log
        self.tabview = self._advanced_panel.tabview

        # Store frame references
        self._frame_model = frame_model
        self._frame_profile = frame_profile
        self._frame_bottom = frame_bottom
        self._app_shell = app_shell
        self._main_interaction_panel = main_panel
        self._side_config_panel = side_panel
        self._product_workspace_panel = side_panel

        self._show_main_view("Kira")
        # Bug C fix (ADR-SD-002 2026-06-25): _compacto_active is now False so
        # _toggle_modo_compacto() routes through the else-branch: shows the
        # product workspace (side_config_panel.grid()) and hides logs (via
        # _toggle_logs_panel → _logs_visible_active=False). Kira hero view is
        # already set by the _show_main_view("Kira") call above, decoupled from
        # compact mode. Reverses ui_declutter_20260614 compact-is-default.
        self._toggle_modo_compacto()

    # ──────────────────────────────────────────────
    # Music bed wiring
    # ──────────────────────────────────────────────

    def _music_import_track(self, mood: str) -> None:
        paths = filedialog.askopenfilenames(
            title="Elegir música para mood",
            filetypes=[("Audio", "*.mp3 *.wav")],
        )
        if not paths:
            return
        imported = 0
        errors: list[str] = []
        for path in paths:
            try:
                track = self.music_library.add_file(path, mood)
                imported += 1
                self._print_log(f"[Música] Importado como {track.label}: {track.original_name}")
            except ValueError as exc:
                errors.append(f"{os.path.basename(path)}: {exc}")
        self._music_update_panel()
        if errors:
            self._notify_operator("Música", "\n".join(errors[:6]))
        elif imported:
            self._print_log(f"[Música] {imported} track(s) importados para {mood}.")

    def _dispatch_audio_play(self, fn) -> None:
        """Dispatch an audio play callable to a worker thread.

        Keeps the pygame mixer.Sound disk decode off the Tk main thread for
        user-triggered and agenda-triggered mood changes (FR3).
        The callable is expected to be a zero-argument lambda wrapping
        audio_bed.request_mood(...).
        """
        import threading as _threading

        def _worker():
            try:
                fn()
            except Exception as e:
                logger.warning("Audio play worker error: %s", e)

        _threading.Thread(target=_worker, daemon=True).start()

    # Agenda/audio delegates — bodies in agenda_audio_controller.py (Phase 7)
    def _dispatch_suggestion_recompute(self, gen: int, existing_topics: list, last_outputs: list, session_id: str) -> None:
        _agenda_audio_call(self, "dispatch_suggestion_recompute", gen=gen, existing_topics=existing_topics, last_outputs=last_outputs, session_id=session_id)

    def _apply_idle_suggestions(self, gen: int, suggestions: list) -> None:
        _agenda_audio_call(self, "apply_idle_suggestions", gen=gen, suggestions=suggestions)

    def _music_play_mood(self, mood: str) -> None:
        # Bug A fix: single-flight for preview buttons — at most ONE worker, last-click
        # wins (coalesce), hard-stop before play so no two channels overlap.
        if not hasattr(self, "_preview_lock"):
            self._preview_lock = threading.Lock()
            self._preview_in_flight, self._preview_latest_mood = False, ""
        with self._preview_lock:
            self._preview_latest_mood = mood
            if self._preview_in_flight:
                self._music_update_panel()
                return
            self._preview_in_flight = True
        def _preview_worker() -> None:
            try:
                while True:
                    with self._preview_lock:
                        current_mood = self._preview_latest_mood
                    self.audio_bed.stop(emergency=True)
                    self.audio_bed.request_mood(current_mood, force=True, boundary=True)
                    with self._preview_lock:
                        if self._preview_latest_mood == current_mood:
                            self._preview_in_flight = False
                            return
            except Exception as exc:
                logger.warning("Preview worker error: %s", exc)
                with self._preview_lock:
                    self._preview_in_flight = False
        threading.Thread(target=_preview_worker, daemon=True).start()
        self._music_update_panel()

    def _music_stop(self) -> None:
        self.audio_bed.stop()
        self._print_log("[Música] Fade out solicitado.")

    def _music_cleanup_missing(self) -> None:
        if not messagebox.askyesno(
            "Limpiar faltantes",
            "¿Quitar de la biblioteca todos los tracks cuyo archivo ya no existe?\n\nNo borra archivos de música; solo limpia metadata faltante.",
        ):
            return
        removed = self.music_library.cleanup_missing()
        self._music_update_panel()
        self._print_log(f"[Música] Tracks faltantes limpiados: {removed}.")

    def _music_delete_track(self, track_id: str) -> None:
        track = self.music_library.tracks.get(track_id)
        if not track:
            self._music_update_panel()
            self._print_log("[Música] El track ya no existe en la biblioteca.")
            return
        if not messagebox.askyesno(
            "Eliminar track",
            f"¿Eliminar '{track.original_name}' de la biblioteca de música?\n\n"
            "Se borrará la metadata y, si el archivo importado está dentro de la carpeta administrada por la app, también ese archivo interno. "
            "No se borran archivos fuente externos.",
        ):
            return
        removed = self.music_library.remove(track_id, delete_file=True)
        self._music_update_panel()
        if removed:
            self._print_log(f"[Música] Track eliminado: {track.label} — {track.original_name}")
        else:
            self._print_log("[Música] No se pudo eliminar: el track ya no existe.")

    def _music_update_panel(self) -> None:
        if hasattr(self, "music_panel"):
            self.music_panel.update_library(
                self.music_library.counts_by_mood(),
                list(self.music_library.all_tracks()),
            )

    # ──────────────────────────────────────────────
    # Stream Admin wiring
    # ──────────────────────────────────────────────

    def _wire_stream_admin_callbacks(self) -> None:
        sa = self.stream_admin_ui
        sa.set_connect_chat_live_callback(lambda: self._on_stream_admin_connect_chat_live())
        sa.set_connect_chat_twitch_callback(lambda: self._on_stream_admin_button_twitch())
        sa.set_threshold_preset_callback(lambda v: self._on_stream_admin_threshold_preset(v))
        sa.set_cooldown_preset_callback(lambda v: self._on_stream_admin_cooldown_preset(v))

    # Agenda/audio delegate — body in agenda_audio_controller.py (Phase 7)
    def _kira_agenda_add_topic(self, title: str, angle: str, constraints: list[str], priority: str = "normal", response_length: str = "normal", max_turns: int | None = None) -> None:
        _agenda_audio_call(self, "kira_agenda_add_topic", title=title, angle=angle, constraints=constraints, priority=priority, response_length=response_length, max_turns=max_turns)

    def _editorial_card_create_or_update(
        self,
        *,
        topic: str,
        summary: str,
        streamer_take: str,
        counterpoints: list[str] | None = None,
        discussion_hooks: list[str] | None = None,
        triggers: list[str] | None = None,
    ) -> EditorialCard:
        return self.editorial_agenda.create_or_update_card(
            topic=topic,
            summary=summary,
            streamer_take=streamer_take,
            counterpoints=counterpoints,
            discussion_hooks=discussion_hooks,
            triggers=triggers,
        )

    def _editorial_card_arm(self, card_id: str) -> bool:
        return self.editorial_agenda.arm_card(card_id)

    def _editorial_card_link_to_agenda_topic(self, topic_id: str, card_id: str) -> bool:
        return self.editorial_agenda.link_card_to_topic(topic_id, card_id)

    def _record_accepted_kira_agenda_output(self, output: str) -> None:
        self.kira_agenda.record_accepted_output(output)
        if self.editorial_agenda.mark_used_after_successful_generation():
            self._on_stream_admin_log("[Editorial Cards] Cue card usada y marcada como consumida.")

    def _kira_agenda_set_session_settings(self, turns: int, rhythm: str, response_length: str, safety_mode: str = "live_safe") -> None:
        self.kira_agenda.set_session_settings(max_turns_per_topic=turns, rhythm=rhythm, response_length=response_length, safety_mode=safety_mode)
        self._kira_agenda_update_status()

    def _kira_agenda_select_profile(self, name: str) -> None:
        if name not in self.cohost_profiles:
            return
        self._current_cohost_profile = name
        self.kira_agenda.set_profile(self.cohost_profiles[name])
        self._on_stream_admin_log(f"[Kira Agenda] Perfil Co-host activo: {name}")

    def _kira_agenda_save_profile(self, name: str, style: str, priority: str, response_length: str) -> None:
        safe_name = sanitize_profile_name(name)
        if not safe_name:
            self._notify_operator("Kira Agenda", "El perfil necesita un nombre.")
            return
        try:
            normalized = normalize_cohost_profile({
                "style": self.kira_agenda.sanitize_topic_text(style, field="profile_style", required=True),
                "default_priority": priority,
                "default_response_length": response_length,
            })
            self.kira_agenda.set_profile(normalized)
        except ValueError as e:
            self._notify_operator("Kira Agenda", str(e))
            return
        self.cohost_profiles[safe_name] = normalized
        self._current_cohost_profile = safe_name
        save_cohost_profiles(self.cohost_profiles)
        self.cohost_agenda_panel.set_profiles(self.cohost_profiles, safe_name)
        self._on_stream_admin_log(f"[Kira Agenda] Perfil Co-host guardado: {safe_name}")

    def _kira_agenda_topic_by_queue_index(self, index: int):
        queued = self.kira_agenda.queued_topics()
        if index < 1 or index > len(queued):
            self._notify_operator("Kira Agenda", "Elegí un número válido de la cola.")
            return None
        return queued[index - 1]

    def _kira_agenda_remove_topic(self, index: int) -> None:
        topic = self._kira_agenda_topic_by_queue_index(index)
        if not topic:
            return
        if not messagebox.askyesno(
            "Eliminar tema de agenda",
            f"¿Eliminar de la cola el tema '{topic.title}'?\n\nLa acción no borra perfiles ni historial, solo este tema pendiente.",
        ):
            return
        self.kira_agenda.remove_queued_topic(topic.id)
        self._on_stream_admin_log(f"[Kira Agenda] Tema eliminado de la cola: {topic.title}")
        self._kira_agenda_update_status()

    def _kira_agenda_move_topic(self, index: int, direction: int) -> None:
        topic = self._kira_agenda_topic_by_queue_index(index)
        if not topic:
            return
        self.kira_agenda.move_queued_topic(topic.id, direction)
        self._on_stream_admin_log(f"[Kira Agenda] Tema reordenado: {topic.title}")
        self._kira_agenda_update_status()

    # Agenda/audio delegates — bodies in agenda_audio_controller.py (Phase 7)
    def _kira_agenda_enable(self) -> None:
        _agenda_audio_call(self, "kira_agenda_enable")

    def _kira_agenda_soft_stop(self) -> None:
        _agenda_audio_call(self, "kira_agenda_soft_stop")

    def _check_pending_audio_bed_stop(self) -> None:
        _agenda_audio_call(self, "check_pending_audio_bed_stop")

    def _kira_agenda_emergency_stop(self) -> None:
        _agenda_audio_call(self, "kira_agenda_emergency_stop")

    def _kira_agenda_force_strict_chat_filter(self) -> None:
        _agenda_audio_call(self, "kira_agenda_force_strict_chat_filter")

    def _kira_agenda_restore_chat_filter(self) -> None:
        _agenda_audio_call(self, "kira_agenda_restore_chat_filter")

    def _kira_agenda_approve_suggestion(self, topic_id: str) -> None:
        """Approve a suggestion (DRAFTED topic or inbox proposal) and queue it."""
        if self._topic_inbox_bridge is None:
            return
        self._topic_inbox_bridge.approve(topic_id, self.kira_agenda)
        self._kira_agenda_update_status()

    def _kira_agenda_reject_suggestion(self, topic_id: str) -> None:
        """Reject a suggestion: skip DRAFTED topics, discard inbox proposals."""
        if self._topic_inbox_bridge is None:
            return
        self._topic_inbox_bridge.reject(topic_id, self.kira_agenda)
        self._kira_agenda_update_status()

    # Agenda/audio delegates — bodies in agenda_audio_controller.py (Phase 7)
    def _kira_agenda_tick(self) -> None:
        _agenda_audio_call(self, "kira_agenda_tick")

    def _kira_agenda_schedule_tick(self, delay_ms: int) -> None:
        _agenda_audio_call(self, "kira_agenda_schedule_tick", delay_ms=delay_ms)

    def _enqueue_kira_agenda_action(self, action: AgendaAction) -> None:
        _agenda_audio_call(self, "enqueue_kira_agenda_action", action=action)

    def _kira_agenda_has_higher_priority_pending(self, action: AgendaAction) -> bool:
        return _agenda_audio_call(self, "kira_agenda_has_higher_priority_pending", action=action)

    def _kira_agenda_has_non_agenda_audio_work(self) -> bool:
        return _agenda_audio_call(self, "kira_agenda_has_non_agenda_audio_work")

    def _kira_agenda_consume_pending_chat_if_due(self) -> bool:
        return _agenda_audio_call(self, "kira_agenda_consume_pending_chat_if_due")

    def _kira_agenda_prefetch_while_speaking(self) -> None:
        _agenda_audio_call(self, "kira_agenda_prefetch_while_speaking")

    def _kira_agenda_play_prefetched_if_ready(self) -> bool:
        return _agenda_audio_call(self, "kira_agenda_play_prefetched_if_ready")

    def _kira_agenda_clear_prefetch(self) -> bool:
        return _agenda_audio_call(self, "kira_agenda_clear_prefetch")

    def _is_kira_agenda_speech_source(self) -> bool:
        source = getattr(self.motor_ia, "current_speech_source", "") or ""
        return str(source).startswith("kira-agenda")

    def _kira_agenda_update_status(self) -> None:
        _agenda_audio_call(self, "kira_agenda_update_status")

    def _on_stream_admin_connect_chat_live(self) -> None:
        entry_url = self.stream_admin_ui._widget("entry_stream_chat_url")
        if not entry_url:
            return
        raw = entry_url.get().strip()
        raw = raw.replace("\x00", "").replace("\n", "").replace("\r", "")[:500]

        from opencohost.smart_aggregator.url_parser import parse_chat_url

        try:
            parsed = parse_chat_url(raw)
        except ValueError:
            self._notify_operator("Chat Live", "URL no valida o no soportada")
            return

        platform = parsed["platform"]
        source_id = parsed["source_id"]

        if self._ui_state.smart_agg_connected or self._ui_state.smart_agg_connecting:
            self.smart_agg_ui.toggle_connection()
            lbl = self.stream_admin_ui._widget("lbl_stream_chat_live_status")
            if lbl:
                lbl.configure(text="Desconectado", text_color="#aa4444")
            return

        threshold_entry = self.stream_admin_ui._widget("entry_stream_chat_threshold")
        cooldown_entry = self.stream_admin_ui._widget("entry_stream_chat_cooldown")
        if threshold_entry or cooldown_entry:
            import math
            try:
                thr = float(threshold_entry.get().strip()) if threshold_entry else 1.0
                if not math.isfinite(thr):
                    thr = 1.0
                thr = max(0.1, min(thr, 100.0))
            except (ValueError, TypeError):
                thr = 1.0
            try:
                cd = float(cooldown_entry.get().strip()) if cooldown_entry else 45.0
                if not math.isfinite(cd):
                    cd = 45.0
                cd = max(5.0, min(cd, 3600.0))
            except (ValueError, TypeError):
                cd = 45.0
            self.smart_agg.set_activity_limits(threshold_per_second=thr, cooldown_seconds=cd, reset=True)

        spam_entry = self.stream_admin_ui._widget("entry_stream_chat_spam")
        if spam_entry:
            try:
                raw_sp = spam_entry.get().strip()[:4]
                sp = max(1, min(int(raw_sp), 9999))
            except (ValueError, TypeError):
                sp = 10
            self.smart_agg.set_spam_limits(max_messages_per_user=sp)

        previous_preset = self.smart_agg.get_filter_policy()
        preset = "twitch_relaxed" if platform == "twitch" else "balanced"
        self.smart_agg.set_filter_policy(preset)

        if self.smart_agg_ui.connect_to(source_id, platform=platform):
            self.entry_youtube_video.delete(0, "end")
            self.entry_youtube_video.insert(0, source_id)
            lbl = self.stream_admin_ui._widget("lbl_stream_chat_live_status")
            if lbl:
                lbl.configure(text=f"Conectado [{platform}]: {source_id}", text_color="#44aa44")
            self._on_stream_admin_log(f"[StreamAdmin] Chat Live conectado [{platform}]: {source_id}")
        else:
            self.smart_agg.set_filter_policy(previous_preset)

    def _on_stream_admin_button_twitch(self) -> None:
        """Connect to a Twitch chat live using the URL entry."""
        from opencohost.smart_aggregator.url_parser import parse_chat_url

        entry_url = self.stream_admin_ui._widget("entry_stream_chat_url")
        if not entry_url:
            return

        raw = entry_url.get().strip()
        raw = raw.replace("\x00", "").replace("\n", "").replace("\r", "")[:500]

        if not raw:
            self._notify_operator("Twitch Chat", "Ingresa una URL de Twitch (twitch.tv/canal).")
            return

        try:
            parsed = parse_chat_url(raw)
        except ValueError:
            self._notify_operator("Twitch Chat", "URL no valida o no soportada")
            return

        if parsed["platform"] != "twitch":
            self._notify_operator("Twitch Chat", "La URL no es de Twitch. Usa un enlace twitch.tv/canal.")
            return

        if self._ui_state.smart_agg_connected or self._ui_state.smart_agg_connecting:
            self.smart_agg_ui.toggle_connection()
            lbl = self.stream_admin_ui._widget("lbl_stream_chat_live_status")
            if lbl:
                lbl.configure(text="Desconectado", text_color="#aa4444")
            return

        source_id = parsed["source_id"]
        previous_preset = self.smart_agg.get_filter_policy()
        self.smart_agg.set_filter_policy("twitch_relaxed")

        if self.smart_agg_ui.connect_to(source_id, platform="twitch"):
            self.entry_youtube_video.delete(0, "end")
            self.entry_youtube_video.insert(0, source_id)
            lbl = self.stream_admin_ui._widget("lbl_stream_chat_live_status")
            if lbl:
                lbl.configure(text=f"Conectado [twitch]: {source_id}", text_color="#44aa44")
            self._on_stream_admin_log(f"[StreamAdmin] Chat Live Twitch conectado: {source_id}")
        else:
            self.smart_agg.set_filter_policy(previous_preset)

    def _on_stream_admin_threshold_preset(self, value: str) -> None:
        import math
        try:
            thr = float(value)
            if not math.isfinite(thr):
                thr = 1.0
            thr = max(0.1, min(thr, 100.0))
        except (ValueError, TypeError):
            thr = 1.0
        if self.smart_agg:
            self.smart_agg.set_activity_limits(threshold_per_second=thr, reset=True)
            self._on_stream_admin_log(f"[StreamAdmin] Umbral de actividad: {thr:.1f} msg/s.")

    def _on_stream_admin_cooldown_preset(self, value: str) -> None:
        import math
        try:
            cd = float(value)
            if not math.isfinite(cd):
                cd = 45.0
            cd = max(5.0, min(cd, 3600.0))
        except (ValueError, TypeError):
            cd = 45.0
        if self.smart_agg:
            self.smart_agg.set_activity_limits(cooldown_seconds=cd, reset=True)
            self._on_stream_admin_log(f"[StreamAdmin] Cooldown de actividad: {int(cd)}s.")

    def _on_threshold_preset(self, value: str) -> None:
        if hasattr(self, "entry_youtube_threshold"):
            self.entry_youtube_threshold.delete(0, "end")
            self.entry_youtube_threshold.insert(0, value)
        if hasattr(self, "smart_agg_ui"):
            self.smart_agg_ui.apply_threshold(log=True)

    def _stream_admin_track_chat_user(self, message: dict) -> None:
        self.stream_admin_ui.track_chat_user(message)
        self._safe_after(self.stream_admin_ui.refresh_user_list)

    def _on_stream_admin_log(self, msg: str) -> None:
        self._safe_after(lambda m=msg: self._append_stream_admin_log(m))
        clean = msg.replace("[StreamAdmin] ", "")
        self._safe_after(lambda m=clean: self._log_accion(m))

    def _append_stream_admin_log(self, msg: str) -> None:
        if not hasattr(self, "_advanced_panel"):
            return
        self._advanced_panel.append_to_textbox(self.text_stream_admin_log, msg, max_lines=1000)

    def _stream_admin_ingest_rf3_event(self, event_type: str, payload: dict) -> None:
        self.stream_admin_ui.ingest_rf3_event(event_type, payload)

    # ──────────────────────────────────────────────
    # Smart Aggregator initialization
    # ──────────────────────────────────────────────

    def _init_smart_aggregator(self) -> None:
        try:
            config_path = os.path.join(PACKAGE_CONFIG_DIR, "smart_aggregator.yaml")
            self.smart_agg = Aggregator(config_path=config_path, llm_interface=None)
        except Exception as e:
            self.smart_agg = None
            logger.exception("No se pudo inicializar Smart Aggregator")
            self.log_queue.put(f"[SmartAggregator] No disponible: {e}")
            return

        self.smart_agg_ui = SmartAggregatorUI(
            ui_state=self._ui_state,
            dispatcher=self._smart_agg_dispatcher,
            smart_agg=self.smart_agg,
            motor_ia=self.motor_ia,
            entry_youtube_video=self.entry_youtube_video,
            btn_youtube_chat=self.btn_youtube_chat,
            entry_youtube_user_limit=self.entry_youtube_user_limit,
            entry_youtube_threshold=self.entry_youtube_threshold,
            consola_youtube=self.consola_youtube,
            lbl_kira_chat_state=self.lbl_kira_chat_state,
            status_bar=self.status_bar,
            on_log=lambda msg: self.log_queue.put(msg),
            schedule_ui_update=self._safe_after,
            on_track_chat_user=lambda msg: self._stream_admin_track_chat_user(msg),
            on_ingest_rf3=lambda evt, data: self._stream_admin_ingest_rf3_event(evt, data),
            on_joyita=lambda text: self._on_joyita_to_obs(text),
            health_monitor=self.health_monitor,
        )
        self.smart_agg_ui.initialize()

        self.smart_agg.set_busy_callback(self.smart_agg_ui.is_busy)
        self.smart_agg.set_llm_interface(self.smart_agg_ui.llm_interface)
        self.smart_agg.on_filtered_message = self.smart_agg_ui.on_filtered_message
        self.smart_agg.on_vibe_update = self.smart_agg_ui.on_vibe_update
        self.smart_agg.on_activity_trigger = self.smart_agg_ui.on_activity_trigger
        self.smart_agg.on_aggregated_context = self._on_smart_aggregated_context
        self.smart_agg.on_live_safety_log = self._on_stream_admin_log
        self.smart_agg.on_source_error = self.smart_agg_ui.on_source_error
        self.smart_agg.on_source_connect = self.smart_agg_ui.on_source_connect
        self.smart_agg.on_source_disconnect = self.smart_agg_ui.on_source_disconnect
        self.smart_agg.attach_motor_telemetry_seams(self.motor_ia)  # measure-first; no-op unless enabled

        self.stream_admin_ui.set_smart_agg(self.smart_agg)
        self.stream_admin_ui.set_motor_ia(self.motor_ia)
        self.stream_admin_ui.set_smart_agg_defaults({
            "threshold": self.smart_agg.activity.threshold_per_second,
            "cooldown": self.smart_agg.activity.cooldown_seconds,
        })
        self.log_queue.put("[SmartAggregator] RF3 listo. Ingresa un video_id/URL de YouTube Live para conectar chat.")

    def _on_smart_aggregated_context(self, data: dict) -> None:
        _agenda_audio_call(self, "on_smart_aggregated_context", data=data)

    # ──────────────────────────────────────────────
    # View switching
    # ──────────────────────────────────────────────

    def _show_main_view(self, name: str) -> None:
        frames = getattr(self, "_main_view_frames", {})
        if name not in frames:
            return
        frames[name].tkraise()
        for view_name, button in getattr(self, "_main_view_buttons", {}).items():
            if view_name == name:
                button.configure(fg_color="#2f5f8f")
            else:
                button.configure(fg_color="#151d26")

    # ──────────────────────────────────────────────
    # Pipeline and UI state
    # ──────────────────────────────────────────────

    def _actualizar_pipeline(self, estado: str) -> None:
        self._ui_state.pipeline_state = estado
        if self.status_bar:
            self.status_bar.update_pipeline_state(estado)
        if hasattr(self, "voice_panel"):
            self.voice_panel.update_tts_label(estado)

        # Kira transparency: surface what she's doing in the response box so she
        # doesn't read as a black box. Overwritten by the real reply on arrival.
        self._update_kira_response_status(estado)

        # Safe automatic avatar transition from pipeline state.
        # NOTE: "speaking" is handled by _on_motor_speaking_start + alternation timer,
        # so we skip it here to avoid fighting with the timer.
        if hasattr(self, "_avatar_bridge") and estado != "speaking":
            avatar_state = AvatarStateBridge.from_pipeline_state(estado)
            self._avatar_bridge.set_state(avatar_state)

        # Reset inactivity timer on meaningful activity
        if estado in ("listening", "processing", "idle"):
            self._reset_inactivity_timer()

        def update_status_details():
            if estado == "listening":
                if hasattr(self, "lbl_kira_voice_state"):
                    self.lbl_kira_voice_state.configure(text="🎤 escuchando", fg_color="#1f5a3a")
            elif self.dispositivo_seleccionado is None:
                if hasattr(self, "lbl_kira_voice_state"):
                    self.lbl_kira_voice_state.configure(text="🎤 sin mic", fg_color="#4a2630")
            else:
                if hasattr(self, "lbl_kira_voice_state"):
                    self.lbl_kira_voice_state.configure(text="🎤 listo", fg_color="#1b2633")

        self.after(0, update_status_details)

    def _update_kira_response_status(self, estado: str) -> None:
        """Show a human-readable activity cue in the Kira response box.

        Writes ONLY for the transient "processing" state and a one-shot "ready"
        cue on the first idle. It never writes on "listening", later "idle", or
        "speaking", so it can never erase Kira's actual reply (rendered by
        update_kira_response()). The listening state is already surfaced by the
        avatar (listening.png) and the voice strip, so nothing is lost here.
        """
        box = getattr(self, "text_kira_response", None)
        if box is None:
            return
        msg: str | None = None
        if estado == "processing":
            msg = "Kira está pensando…"
        elif estado == "idle" and not self._kira_first_ready:
            self._kira_first_ready = True
            msg = "¡Kira lista! Tocá «Hablar» o escribile abajo para empezar."
        if msg is None:
            return

        def apply() -> None:
            try:
                if not box.winfo_exists():
                    return
                box.configure(state="normal")
                box.delete("1.0", "end")
                box.insert("end", msg + "\n")
                box.configure(state="disabled")
            except Exception:
                logger.debug("No se pudo actualizar el estado en la caja de respuesta", exc_info=True)

        self.after(0, apply)

    def _toggle_modo_compacto(self) -> None:
        # _compacto_active is set by the gear-popover toggle (replaces switch_compacto.get()).
        self._modo_compacto = self._compacto_active
        if self._modo_compacto:
            if hasattr(self, "_side_config_panel"):
                self._side_config_panel.grid_remove()
            self._show_main_view("Kira")
            if hasattr(self, "_advanced_panel"):
                self._set_logs_panel_visible(False)
        else:
            if hasattr(self, "_side_config_panel"):
                self._side_config_panel.grid()
            self._toggle_logs_panel()

    def _set_logs_panel_visible(self, visible: bool) -> None:
        if not hasattr(self, "_advanced_panel"):
            return
        self._advanced_panel.set_logs_visible(visible)
        self._logs_panel_visible = visible

    def _toggle_logs_panel(self) -> None:
        if not hasattr(self, "_advanced_panel"):
            return
        # In compact mode the logs panel is always hidden regardless of the logs toggle.
        if getattr(self, "_compacto_active", False):
            self._set_logs_panel_visible(False)
            return
        # Use the internal bool managed by the gear popover (replaces switch_advanced.get()).
        visible = bool(getattr(self, "_logs_visible_active", False))
        self._set_logs_panel_visible(visible)

    def _open_gear_popover(self) -> None:
        """Open (or focus) the gear-settings popover.

        Delegates construction to opencohost.ui.gear_popover.open_gear_popover.
        The gear popover now lives in its own module (ui_declutter_20260614 FIX C).
        """
        from opencohost.ui.gear_popover import open_gear_popover
        result = open_gear_popover(
            parent=self,
            popover_ref_getter=lambda: self._gear_popover,
            popover_ref_setter=lambda p: setattr(self, "_gear_popover", p),
            compacto_active=getattr(self, "_compacto_active", True),
            logs_visible=getattr(self, "_logs_visible_active", False),
            on_compacto_toggle=self._toggle_modo_compacto,
            on_logs_toggle=self._toggle_logs_panel,
            on_compacto_state_write=lambda v: setattr(self, "_compacto_active", v),
            on_logs_state_write=lambda v: setattr(self, "_logs_visible_active", v),
        )
        if result is not None:
            self._gear_popover = result

    def _open_inspector_cards(self) -> None:
        """Open (or focus) the "Tarjetas editoriales" read-only inspector.

        Delegates construction to opencohost.ui.inspector_cards.open_inspector_cards.
        """
        from opencohost.ui.inspector_cards import open_inspector_cards
        result = open_inspector_cards(
            parent=self,
            ref_getter=lambda: self._editorial_cards_inspector,
            ref_setter=lambda w: setattr(self, "_editorial_cards_inspector", w),
            card_store=self.editorial_cards,
            schedule_ui_update=self._safe_after,
        )
        if result is not None:
            self._editorial_cards_inspector = result

    def _open_inspector_memory(self) -> None:
        """Open (or focus) the "Memoria de Kira" read-only inspector.

        Delegates construction to opencohost.ui.inspector_memory.open_inspector_memory.
        """
        from opencohost.ui.inspector_memory import open_inspector_memory
        result = open_inspector_memory(
            parent=self,
            ref_getter=lambda: self._kira_memory_inspector,
            ref_setter=lambda w: setattr(self, "_kira_memory_inspector", w),
            motor_ia=self.motor_ia,
            kira_agenda=self.kira_agenda,
            schedule_ui_update=self._safe_after,
            # deviation-2 (slice 6->7): real store, guarded on the flag so a
            # False run never touches disk for it (dormant-store invariant).
            memoria_store=(self.motor_ia._get_memoria_store() if MEMORIAS_ENABLED else None),
            profile_id_getter=lambda: self.motor_ia._current_profile_id,
        )
        if result is not None:
            self._kira_memory_inspector = result

    def _toggle_help_section(self, key: str, frame: Any, button: Any) -> None:
        expanded = not self._help_expanded.get(key, False)
        self._help_expanded[key] = expanded
        if expanded:
            frame.grid()
            button.configure(text=button.cget("text").replace("▶", "▼"))
        else:
            frame.grid_remove()
            button.configure(text=button.cget("text").replace("▼", "▶"))

    def _switch_product_tab(self, key: str) -> None:
        """Switch the active product tab (custom button tabs)."""
        if key == self._active_product_tab:
            return
        prev = self._product_tab_data.get(self._active_product_tab)
        if prev:
            prev["button"].configure(fg_color="#151d26", hover_color="#1d2a38", text_color="#6b7b8d")
            prev["frame"].grid_remove()
        curr = self._product_tab_data.get(key)
        if curr:
            curr["button"].configure(fg_color="#2f5f8f", hover_color="#3670aa", text_color="#ffffff")
            curr["frame"].grid()
        self._active_product_tab = key
        # Toggle sub-tab bar visibility
        self._cfg_sub_bar.grid() if key == "config" else self._cfg_sub_bar.grid_remove()
        self._stream_sub_bar.grid() if key == "stream" else self._stream_sub_bar.grid_remove()

    def _switch_cfg_subtab(self, key: str) -> None:
        """Switch the active Configuración sub-tab."""
        if key == self._active_cfg_subtab:
            return
        prev = self._cfg_subtab_data.get(self._active_cfg_subtab)
        if prev:
            prev["button"].configure(fg_color="#0f151c", hover_color="#151d26", text_color="#6b7b8d")
            prev["frame"].grid_remove()
        curr = self._cfg_subtab_data.get(key)
        if curr:
            curr["button"].configure(fg_color="#1e4060", hover_color="#255478", text_color="#d8e2ef")
            curr["frame"].grid()
        self._active_cfg_subtab = key

    def _switch_stream_subtab(self, key: str) -> None:
        """Switch the active Stream sub-tab (called from side_panel-level buttons)."""
        if key == self._active_stream_subtab:
            return
        prev = self._stream_subtab_data.get(self._active_stream_subtab)
        if prev:
            prev["button"].configure(fg_color="#0f151c", hover_color="#151d26", text_color="#6b7b8d")
            prev["frame"].grid_remove()
        curr = self._stream_subtab_data.get(key)
        if curr:
            curr["button"].configure(fg_color="#1e4060", hover_color="#255478", text_color="#d8e2ef")
            curr["frame"].grid()
        self._active_stream_subtab = key

    def _log_accion(self, msg: str) -> None:
        self._advanced_panel.log_action(msg)

    # ──────────────────────────────────────────────
    # Motor event handler
    # ──────────────────────────────────────────────

    def _process_ui_tasks(self) -> None:
        task_queue = self.__dict__.get("_ui_task_queue")
        if task_queue is None:
            return

        while True:
            try:
                delay_ms, func = task_queue.get_nowait()
            except queue.Empty:
                break
            try:
                self.after(delay_ms, func)
            except RuntimeError:
                pass

        if not self.__dict__.get("_closing", False):
            try:
                self.after(50, self._process_ui_tasks)
            except RuntimeError:
                pass

    def _safe_after(self, func, delay_ms: int = 0) -> None:
        """Schedule a UI update on the main thread, safely handling startup race conditions.

        During startup the motor thread may fire events before Tkinter enters
        mainloop().  ``self.after()`` raises RuntimeError in that case.  We
        silently skip — the UI will be in its initial state and subsequent
        events will update it once the loop is running.
        """
        if threading.current_thread() is not threading.main_thread():
            task_queue = self.__dict__.get("_ui_task_queue")
            if task_queue is not None:
                task_queue.put((delay_ms, func))
                return
        try:
            self.after(delay_ms, func)
        except RuntimeError:
            pass

    def _on_motor_event(self, status: str) -> None:
        if threading.current_thread() is not threading.main_thread():
            self._safe_after(lambda status=status: self._handle_motor_event(status))
            return
        self._handle_motor_event(status)

    def _handle_motor_event(self, status: str) -> None:
        from opencohost.ui.motor_event_handlers import STATUS_TO_HANDLER
        name = STATUS_TO_HANDLER.get(status)
        if name:
            getattr(self, "_" + name)()

    @staticmethod
    def _get_attr(obj: object, name: str, default=None):
        """object.__getattribute__ lookup — raises AttributeError without CTk recursion."""
        try:
            return object.__getattribute__(obj, name)
        except AttributeError:
            return default

    def _motor_handler_deps(self) -> dict:  # deps for motor_event_handlers.get_handler_map()
        # Use _get_attr (object.__getattribute__) — avoids triggering CTk __getattr__
        # on partially-initialised object.__new__ stubs used in unit tests.
        g = self._get_attr
        _d = vars(self)
        return dict(
            ui_state=_d.get("_ui_state"),
            motor_ia=_d.get("motor_ia"),
            model_panel=_d.get("model_panel"),
            voice_panel=_d.get("voice_panel"),
            ptt=_d.get("ptt"),
            audio_bed=_d.get("audio_bed"),
            avatar_bridge=_d.get("_avatar_bridge"),
            kira_agenda=_d.get("kira_agenda"),
            btn_grabar=_d.get("btn_grabar"),
            btn_voz=_d.get("btn_voz"),
            btn_ws=_d.get("btn_ws"),
            btn_primary_voice=_d.get("btn_primary_voice"),
            btn_enviar=_d.get("btn_enviar"),
            btn_download=_d.get("btn_download"),
            btn_mapear=_d.get("btn_mapear"),
            combo_modelos=_d.get("combo_modelos"),
            switch_ptt=_d.get("switch_ptt"),
            progress_download=_d.get("progress_download"),
            schedule_ui_update=g(self, "_safe_after"),
            actualizar_pipeline=g(self, "_actualizar_pipeline"),
            log_accion=g(self, "_log_accion"),
            print_log=g(self, "_print_log"),
            notify_operator=g(self, "_notify_operator"),
            check_pending_audio_bed_stop=g(self, "_check_pending_audio_bed_stop"),
            dispatch_audio_play=g(self, "_dispatch_audio_play"),
            kira_agenda_schedule_tick=g(self, "_kira_agenda_schedule_tick"),
            kira_agenda_update_status=g(self, "_kira_agenda_update_status"),
            kira_agenda_prefetch_while_speaking=g(self, "_kira_agenda_prefetch_while_speaking"),
            kira_agenda_clear_prefetch=g(self, "_kira_agenda_clear_prefetch"),
            kira_agenda_consume_pending_chat_if_due=g(self, "_kira_agenda_consume_pending_chat_if_due"),
            kira_agenda_play_prefetched_if_ready=g(self, "_kira_agenda_play_prefetched_if_ready"),
            on_ptt_press=g(self, "_on_ptt_press"),
            on_ptt_release=g(self, "_on_ptt_release"),
            on_ptt_click=g(self, "_on_ptt_click"),
            # Timer state — getter/setter pairs (obs_lifecycle pattern)
            get_speaking_is_alt=lambda: _d.get("_speaking_is_alt"),
            set_speaking_is_alt=lambda v: setattr(self, "_speaking_is_alt", v),
            get_speaking_alt_timer_id=lambda: _d.get("_speaking_alt_timer_id"),
            set_speaking_alt_timer_id=lambda v: setattr(self, "_speaking_alt_timer_id", v),
            get_inactivity_timer_id=lambda: _d.get("_inactivity_timer_id"),
            set_inactivity_timer_id=lambda v: setattr(self, "_inactivity_timer_id", v),
            inactivity_timeout_ms=_d.get("_inactivity_timeout_ms"),
            after=g(self, "after"),
            after_cancel=g(self, "after_cancel"),
            set_title=g(self, "title"),
            winfo_exists=g(self, "winfo_exists"),
        )

    # Motor-event handler delegates — bodies in motor_event_handlers.py (Phase 6 Stage 3).
    def _dispatch_motor_handler(self, fn: str) -> None:  # call motor_event_handlers.<fn> with injected deps
        import opencohost.ui.motor_event_handlers as _m
        getattr(_m, fn)(**self._motor_handler_deps())

    def _on_motor_ready(self) -> None: self._dispatch_motor_handler("on_motor_ready")
    def _on_motor_model_warming(self) -> None: self._dispatch_motor_handler("on_motor_model_warming")
    def _on_motor_ollama_unavailable(self) -> None: self._dispatch_motor_handler("on_motor_ollama_unavailable")
    def _on_motor_processing(self) -> None: self._dispatch_motor_handler("on_motor_processing")
    def _on_motor_idle(self) -> None: self._dispatch_motor_handler("on_motor_idle")
    def _on_motor_llm_timeout_recovered(self) -> None: self._dispatch_motor_handler("on_motor_llm_timeout_recovered")
    def _on_motor_speaking_start(self) -> None: self._dispatch_motor_handler("on_motor_speaking_start")
    def _on_motor_speaking_end(self) -> None: self._dispatch_motor_handler("on_motor_speaking_end")
    def _start_speaking_alt_timer(self) -> None: self._dispatch_motor_handler("start_speaking_alt_timer")
    def _stop_speaking_alt_timer(self) -> None: self._dispatch_motor_handler("stop_speaking_alt_timer")
    def _tick_speaking_alt(self) -> None: self._dispatch_motor_handler("tick_speaking_alt")
    def _reset_inactivity_timer(self) -> None: self._dispatch_motor_handler("reset_inactivity_timer")
    def _stop_inactivity_timer(self) -> None: self._dispatch_motor_handler("stop_inactivity_timer")
    def _on_inactivity_timeout(self) -> None: self._dispatch_motor_handler("on_inactivity_timeout")
    def _on_motor_model_changed(self) -> None: self._dispatch_motor_handler("on_motor_model_changed")
    def _on_motor_switch_pending(self) -> None: self._dispatch_motor_handler("on_motor_switch_pending")
    def _on_motor_switch_failed(self) -> None: self._dispatch_motor_handler("on_motor_switch_failed")
    def _on_motor_download_start(self) -> None: self._dispatch_motor_handler("on_motor_download_start")
    def _on_motor_download_done(self) -> None: self._dispatch_motor_handler("on_motor_download_done")
    def _on_motor_download_error(self) -> None: self._dispatch_motor_handler("on_motor_download_error")
    def _on_ctx_pressure_high(self) -> None: self._dispatch_motor_handler("on_ctx_pressure_high")
    def _on_piper_voice_locale_mismatch(self) -> None: self._dispatch_motor_handler("on_piper_voice_locale_mismatch")

    # ------------------------------------------------------------------
    # OBS lifecycle delegates — bodies live in opencohost/ui/obs_lifecycle.py
    # (ui_rendering_optimization_20260609 Phase 6 Stage 1 extraction).
    # Keep these thin delegates so object.__new__ fixtures and on_closing/
    # _build_ui callers keep resolving the same method names unchanged.
    # ------------------------------------------------------------------

    def _on_joyita_to_obs(self, text: str) -> None:
        """Delegate → obs_lifecycle.on_joyita_to_obs."""
        from opencohost.ui.obs_lifecycle import on_joyita_to_obs
        on_joyita_to_obs(
            text=text,
            get_obs_client=lambda: getattr(self, "_obs_client", None),
            get_joyita_timer_id=lambda: getattr(self, "_joyita_obs_timer_id", None),
            set_joyita_timer_id=lambda v: setattr(self, "_joyita_obs_timer_id", v),
            after=self.after,
            after_cancel=self.after_cancel,
            clear_obs_joyita=self._clear_obs_joyita,
        )

    def _clear_obs_joyita(self, source_name: str) -> None:
        """Delegate → obs_lifecycle.clear_obs_joyita."""
        from opencohost.ui.obs_lifecycle import clear_obs_joyita
        clear_obs_joyita(
            source_name=source_name,
            get_obs_client=lambda: getattr(self, "_obs_client", None),
            set_joyita_timer_id=lambda v: setattr(self, "_joyita_obs_timer_id", v),
        )

    def _init_obs_client(self) -> None:
        """Initialize OBS WebSocket client if enabled in config."""
        self._obs_start_from_config()

    def _obs_connect_now(self) -> None:
        """Start or refresh the live OBS runtime connection from current config."""
        self._obs_start_from_config()

    def _obs_start_from_config(self, retry_delay: float = 5) -> bool:
        """Delegate → obs_lifecycle.start_from_config.

        obs_client_cls and thread_cls are forwarded from the app_shell module
        namespace so that existing tests that patch app_shell.OBSClient /
        app_shell.threading.Thread keep working unchanged.
        """
        from opencohost.ui.obs_lifecycle import start_from_config
        return start_from_config(
            print_log=self._print_log,
            schedule_ui_update=self._safe_after,
            avatar_panel=getattr(self, "_avatar_panel", None),
            avatar_bridge=getattr(self, "_avatar_bridge", None),
            get_obs_client=lambda: getattr(self, "_obs_client", None),
            set_obs_client=lambda c: setattr(self, "_obs_client", c),
            get_retry_thread=lambda: getattr(self, "_obs_retry_thread", None),
            set_retry_thread=lambda t: setattr(self, "_obs_retry_thread", t),
            get_retry_cancel=lambda: getattr(self, "_obs_retry_cancel", None),
            set_retry_cancel=lambda e: setattr(self, "_obs_retry_cancel", e),
            winfo_exists=lambda: self.winfo_exists(),
            retry_delay=retry_delay,
            obs_client_cls=OBSClient,
            obs_config_cls=OBSConfig,
            thread_cls=threading.Thread,
        )

    def _obs_stop_runtime(self) -> None:
        """Delegate → obs_lifecycle.stop_runtime."""
        from opencohost.ui.obs_lifecycle import stop_runtime
        stop_runtime(
            print_log=self._print_log,
            avatar_panel=getattr(self, "_avatar_panel", None),
            get_obs_client=lambda: getattr(self, "_obs_client", None),
            set_obs_client=lambda c: setattr(self, "_obs_client", c),
            get_retry_cancel=lambda: getattr(self, "_obs_retry_cancel", None),
        )

    def _connect_obs_loop(
        self,
        cancel_event: threading.Event | None = None,
        obs_client: OBSClient | None = None,
        retry_delay: float = 5,
    ) -> None:
        """Delegate → obs_lifecycle.connect_obs_loop."""
        from opencohost.ui.obs_lifecycle import connect_obs_loop
        connect_obs_loop(
            cancel_event=cancel_event,
            obs_client=obs_client,
            get_obs_client=lambda: getattr(self, "_obs_client", None),
            print_log=self._print_log,
            schedule_ui_update=self._safe_after,
            avatar_panel=getattr(self, "_avatar_panel", None),
            avatar_bridge=getattr(self, "_avatar_bridge", None),
            winfo_exists=lambda: self.winfo_exists(),
            retry_delay=retry_delay,
        )

    # ──────────────────────────────────────────────
    # Audio, TTS, Chat, PTT helpers
    # ──────────────────────────────────────────────

    def _al_cambiar_motor_tts(self) -> None:
        motor_seleccionado = self.switch_modo_ligero.get()
        self.motor_ia.command_queue.put(("set_motor_tts", motor_seleccionado))
        modo_texto = "Ligero" if motor_seleccionado == "ligero" else "Pesado"
        self.switch_modo_ligero.configure(text=f"🎛️ TTS: {modo_texto}")
        if self.status_bar:
            self.status_bar.update_tts_status("idle")
        if hasattr(self, "lbl_kira_tts_state"):
            self.lbl_kira_tts_state.configure(text="🔊 idle", fg_color="#1b2633")

    def _al_cambiar_tts_local_only(self) -> None:
        """Dispatch set_tts_local_only to the engine (persists; immediate effect)."""
        enabled = bool(self.switch_local_only.get())
        self.motor_ia.command_queue.put(("set_tts_local_only", enabled))

    def _on_kira_voice_change(self, label: str) -> None:
        """Switch Kira's voice (Argentina ↔ Neutral), both offline Piper voices.

        Selecting a voice forces local Piper synthesis so the change is audible
        immediately and never routes to the Edge-TTS cloud. The engine persists
        the choice; the privacy switch is kept visually in sync. No-op if the
        motor is not up yet (e.g. a programmatic .set() during UI build).
        """
        motor = getattr(self, "motor_ia", None)
        if motor is None:
            return
        voice_key = self._kira_voice_labels.get(label, DEFAULT_PIPER_VOICE)
        # Guard: never switch to a voice whose Piper .onnx model is missing.
        # Dispatching set_piper_voice would fail on the motor thread (reload
        # raises), leaving the engine on the old voice while the UI already shows
        # the new label + flipped local-only — a broken, persisted desync.
        if not os.path.isfile(piper_voice_path(voice_key)):
            prev_key = load_piper_voice(
                default=default_piper_voice_for_locale(i18n_active.get_active_bundle().code)
            )
            prev_label = next(
                (lbl for lbl, key in self._kira_voice_labels.items() if key == prev_key),
                label,
            )
            # .set() updates the button WITHOUT re-firing this command.
            self.seg_kira_voice.set(prev_label)
            self._print_log(
                f"[Voz] La voz «{label}» no está disponible (falta el modelo). "
                "Mantengo la voz actual."
            )
            return
        motor.command_queue.put(("set_piper_voice", voice_key))
        motor.command_queue.put(("set_tts_local_only", True))
        if hasattr(self, "switch_local_only"):
            self.switch_local_only.select()  # keep the privacy switch in sync

    def _obtener_dispositivos_entrada(self) -> list:
        dispositivos_validos = []
        try:
            dispositivos = sd.query_devices()
            for i, d in enumerate(dispositivos):
                if d["max_input_channels"] > 0:
                    dispositivos_validos.append(f"{i}: {d['name']}")
        except Exception as e:
            logger.error(f"No se pudieron listar dispositivos de audio: {e}")
        return dispositivos_validos

    def _al_seleccionar_dispositivo(self, seleccion: str) -> None:
        try:
            self.dispositivo_seleccionado = int(seleccion.split(":")[0])
            if self.status_bar:
                self.status_bar.update_mic_status("idle")
            if hasattr(self, "lbl_kira_voice_state"):
                self.lbl_kira_voice_state.configure(text="🎤 listo", fg_color="#1b2633")
            self._print_log(f"[Sistema] Fuente de audio: ID {self.dispositivo_seleccionado}")
        except (ValueError, IndexError):
            self.dispositivo_seleccionado = None
            if self.status_bar:
                self.status_bar.update_mic_status("disconnected")
            if hasattr(self, "lbl_kira_voice_state"):
                self.lbl_kira_voice_state.configure(text="🎤 sin mic", fg_color="#4a2630")

    def _iniciar_grabacion(self) -> None:
        if self.dispositivo_seleccionado is None:
            self._notify_operator("Atención", "Selecciona una fuente de audio primero.")
            return
        dialog = ctk.CTkInputDialog(text=f"Grabarás {RECORDING_DURATION} segundos de audio para calibrar la voz.\nHabla con tu tono natural.\n\nPresiona OK para empezar.", title="Confirmar Grabación")
        res = dialog.get_input()
        if res is not None:
            threading.Thread(target=self._hilo_grabacion, daemon=True).start()
        else:
            self._print_log("[Grabación] Acción cancelada.")

    def _hilo_grabacion(self) -> None:
        filepath = REFERENCE_WAV_PATH
        self._print_log(f"\n[Grabación] 🔴 GRABANDO {RECORDING_DURATION}s... Habla ahora.")
        self._safe_after(lambda: self.btn_grabar.configure(state="disabled", text="Grabando...", fg_color="darkred"))
        try:
            recording = sd.rec(int(RECORDING_DURATION * RECORDING_SAMPLERATE), samplerate=RECORDING_SAMPLERATE, channels=1, dtype="float32", device=self.dispositivo_seleccionado)
            sd.wait()
            rms = np.sqrt(np.mean(recording ** 2))
            if rms < MIN_AUDIO_RMS:
                self._print_log("[Grabación] ⚠️ Audio demasiado bajo o silencio. Intenta de nuevo.")
                logger.warning(f"Grabación descartada por RMS bajo: {rms:.6f}")
                return
            sf.write(filepath, recording, RECORDING_SAMPLERATE)
            self._print_log(f"[Grabación] ⏹️ Audio capturado (RMS: {rms:.4f})")
            logger.info(f"Grabación guardada: {filepath}, RMS={rms:.4f}")
            response = {"use": False}
            response_ready = threading.Event()
            def ask_use_audio():
                response["use"] = messagebox.askyesno("Grabación Finalizada", "Audio capturado correctamente.\n\n¿Usar como voz de referencia para la IA?")
                response_ready.set()
            self._safe_after(ask_use_audio)
            if not response_ready.wait(timeout=120):
                self._print_log("[Grabación] Confirmación agotó tiempo; audio descartado por seguridad.")
            if response["use"]:
                self._print_log("[Sistema] Perfil de voz enviado a la IA.")
                self.motor_ia.command_queue.put(("set_voice", filepath))
                self._safe_after(lambda: self.btn_ws.configure(state="normal", fg_color="#555555"))
                self._safe_after(lambda: self.btn_enviar.configure(state="normal"))
            else:
                self._print_log("[Grabación] ❌ Descartada.")
                if os.path.exists(filepath):
                    os.remove(filepath)
        except Exception as e:
            self._print_log(f"[ERROR Grabación]: {e}")
            logger.exception("Error durante grabación")
        finally:
            self._safe_after(lambda: self.btn_grabar.configure(state="normal", text="🎤 Grabar", fg_color="#555555"))

    def _cargar_voz(self) -> None:
        filepath = filedialog.askopenfilename(title="Seleccionar muestra de voz", filetypes=[("Audio WAV", "*.wav")])
        if filepath:
            self.btn_voz.configure(text="Cargando WAV...", fg_color="#cc8800")
            self.update_idletasks()
            try:
                data, sr = sf.read(filepath)
                duration = len(data) / sr
                if duration < 2.0:
                    self._notify_operator("Audio muy corto", "El audio debe durar al menos 2 segundos.")
                    self.btn_voz.configure(text="📂 Cargar WAV", fg_color="#555555")
                    return
                if duration > 30.0:
                    self._notify_operator("Audio muy largo", "El audio no debe durar más de 30 segundos.")
                    self.btn_voz.configure(text="📂 Cargar WAV", fg_color="#555555")
                    return
            except Exception as e:
                self._notify_operator("Error", f"No se pudo leer el archivo de audio:\n{e}", level="error")
                self.btn_voz.configure(text="📂 Cargar WAV", fg_color="#555555")
                return
            self.motor_ia.command_queue.put(("set_voice", filepath))
            self.btn_ws.configure(state="normal", fg_color="#555555")
            self.btn_enviar.configure(state="normal")
            self.btn_voz.configure(text="WAV Cargado ✅", fg_color="#1f5a3a")
            self._print_log(f"[Sistema] Perfil de voz cargado ({duration:.1f}s).")
            self._safe_after(lambda: self.btn_voz.configure(text="📂 Cargar WAV", fg_color="#555555"), delay_ms=2000)

    def _enviar_contexto_manual(self) -> None:
        texto = self.entry_chat.get().strip()
        if texto:
            self._print_log(f"\n[Tú]: {texto}")
            self.motor_ia.command_queue.put(("process_context", texto))
            self.entry_chat.delete(0, "end")

    def _limpiar_historial(self) -> None:
        # NOTE (customtkinter_visual_refinement): the gear-popover memory pill keeps the
        # "Memoria: …" text format on purpose — it belongs to the popover pill-set (with
        # OAuth/moderation), not the left panel. Only the left-panel pill (lbl_kira_memory_state)
        # carries the 🧠 icon in Phase 1. Icon-ize the popover set together in its own phase.
        if hasattr(self, "lbl_memory_status_pill"):
            self.lbl_memory_status_pill.configure(text="Memoria: limpiando", fg_color="#5f461b")
        if hasattr(self, "lbl_kira_memory_state"):
            self.lbl_kira_memory_state.configure(text="🧠 limpiando", fg_color="#5f461b")
        self.motor_ia.command_queue.put(("clear_history", None))
        self._print_log("[Sistema] 🗑️ Memoria de conversación limpiada.")
        self.after(800, lambda: self.lbl_memory_status_pill.configure(text="Memoria: disponible", fg_color="#1b2633") if hasattr(self, "lbl_memory_status_pill") else None)
        self.after(800, lambda: self.lbl_kira_memory_state.configure(text="🧠 disponible", fg_color="#1b2633") if hasattr(self, "lbl_kira_memory_state") else None)

    def _toggle_websocket(self) -> None:
        if hasattr(self, "voice_panel"):
            self.voice_panel._toggle_websocket()

    # ──────────────────────────────────────────────
    # PTT
    # ──────────────────────────────────────────────

    def _on_ptt_status_change(self, text: str, color: str) -> None:
        def update():
            self.lbl_ptt_status.configure(text=text, text_color=color)
            if self.status_bar and text:
                if "ESCUCHANDO" in text:
                    self.status_bar.update_mic_status("listening")
                    if hasattr(self, "_avatar_bridge"):
                        self._avatar_bridge.set_state(AvatarState.LISTENING)
                else:
                    self.status_bar.update_mic_status("idle")
            elif self.status_bar:
                self.status_bar.update_mic_status("idle")
        self.after(0, update)

    def _al_toggle_ptt(self) -> None:
        nuevo_estado = self.switch_ptt.get()
        if nuevo_estado == self.ptt.enabled:
            return
        self.ptt.enabled = nuevo_estado
        self._ui_state.ptt_enabled = nuevo_estado
        if self.ptt.enabled:
            self.switch_ptt.configure(text="PTT ON")
            if not self.ptt.mapping:
                self.ptt.start_listener(on_press=self._on_ptt_press, on_release=self._on_ptt_release, on_click=self._on_ptt_click)
                self._on_ptt_status_change(f"Manten presionado [{self.ptt.hotkey}] para hablar", "#888888")
            self._print_log(f"[PTT] Activado — hotkey: {self.ptt.hotkey}")
        else:
            self.switch_ptt.configure(text="PTT OFF")
            self.ptt.stop_listener()
            self.ptt.stop_mapping()
            self._on_ptt_status_change("", "#888888")
            self._print_log("[PTT] Desactivado — modo continuo WebSocket")

    def _mapear_hotkey(self) -> None:
        if self.ptt.mapping:
            return
        self.btn_mapear.configure(text="Escuchando...", state="disabled", fg_color="#cc8800")
        self._on_ptt_status_change("Presiona la tecla o boton deseado...", "#cc8800")
        self.ptt.start_mapping(on_key=self._on_mapear_key, on_mouse=self._on_mapear_mouse)

    def _on_mapear_key(self, key) -> bool:
        display = self.ptt.get_reverse_mapping().get(key)
        if display:
            self._save_mapped_hotkey(display)
            return False

    def _on_mapear_mouse(self, x, y, button, pressed) -> bool:
        if pressed:
            display = self.ptt.get_reverse_mapping().get(button)
            if display:
                self._save_mapped_hotkey(display)
                return False

    def _save_mapped_hotkey(self, hotkey: str) -> None:
        self.ptt.hotkey = hotkey
        self.ptt.save_config(hotkey)
        self.after(0, lambda: self.lbl_hotkey.configure(text=hotkey))
        self.after(0, lambda: self.btn_mapear.configure(text="Mapear", state="normal", fg_color="#555555"))
        self._print_log(f"[PTT] Tecla mapeada y guardada: {hotkey}")
        self.ptt.stop_mapping()
        if self.ptt.enabled:
            self.ptt.start_listener(on_press=self._on_ptt_press, on_release=self._on_ptt_release, on_click=self._on_ptt_click)
            self._on_ptt_status_change(f"Manten presionado [{hotkey}] para hablar", "#888888")

    def _on_ptt_press(self, key) -> None:
        was_active = self.ptt.active
        self.ptt.on_ptt_press(key)
        if was_active or not self.ptt.active:
            return
        self._ui_state.ptt_active = True
        self._ptt_accept_logged = False
        # Clear buffer for new accumulation cycle
        if hasattr(self, "voice_panel"):
            self.voice_panel.clear_ptt_buffer()

    def _on_ptt_release(self, key) -> None:
        was_active = self.ptt.active
        self.ptt.on_ptt_release(key)
        if not was_active or self.ptt.active:
            return
        self._ui_state.ptt_active = False
        self._ptt_accept_logged = False
        # Start grace period immediately (don't wait for WS message)
        if hasattr(self, "voice_panel"):
            self.voice_panel.on_ptt_release()

    def _on_ptt_click(self, x, y, button, pressed) -> None:
        was_active = self.ptt.active
        self.ptt.on_ptt_click(x, y, button, pressed)
        is_active = self.ptt.active
        if was_active == is_active:
            return
        self._ui_state.ptt_active = is_active
        self._ptt_accept_logged = False
        if is_active and hasattr(self, "voice_panel"):
            self.voice_panel.clear_ptt_buffer()
        elif hasattr(self, "voice_panel"):
            self.voice_panel.on_ptt_release()

    # ──────────────────────────────────────────────
    # Logging
    # ──────────────────────────────────────────────

    def _notify_operator(self, title: str, message: str, level: str = "warning") -> None:
        level_name = (level or "warning").lower()
        log_method = getattr(logger, level_name, logger.warning)
        if not callable(log_method):
            log_method = logger.warning
        log_method("%s: %s", title, message)

        text = f"[{level_name.upper()}] {title}: {message}"
        printed = False
        printer = getattr(self, "_print_log", None)
        if callable(printer):
            try:
                printer(text)
                printed = True
            except Exception:
                logger.debug("No se pudo imprimir notificacion de operador", exc_info=True)

        if not printed:
            log_queue = getattr(self, "log_queue", None)
            if log_queue is not None:
                log_queue.put(text)

    def _print_log(self, msg: str) -> None:
        self._advanced_panel.print_log(msg)

    def _process_logs(self) -> None:
        self._advanced_panel.process_logs()
        self.after(100, self._process_logs)

    def _aplicar_perfil_actual(self) -> None:
        if hasattr(self, "profile_panel"):
            self.profile_panel.apply_current_profile()

    # ──────────────────────────────────────────────
    # Cleanup
    # ──────────────────────────────────────────────

    def on_closing(self) -> None:
        self._closing = True
        # F4 slice 4 (task 4.13): bounded memorias flush, first on close.
        # hasattr-guarded (older stubs lack it); fail-open (flush_memorias
        # itself never raises — this is the last line of defense).
        if hasattr(self, "motor_ia") and hasattr(self.motor_ia, "flush_memorias"):
            try:
                self.motor_ia.flush_memorias()
            except Exception as exc:
                logger.warning("memoria close-flush call failed (fail-open): %s", type(exc).__name__)
        logger.info("Cerrando aplicación...")
        # Cancel prefetch retry timer (fix #B) to avoid callbacks after teardown.
        if self.__dict__.get("_prefetch_retry_id") is not None:
            try:
                self.after_cancel(self._prefetch_retry_id)
            except Exception:
                pass
            self._prefetch_retry_id = None

        # Fix: audit/ui-security-perf-2026-05-17 — unsubscribe UIState observer to
        # release GC reference. Matches pattern used by all panel cleanup methods.
        if hasattr(self, "_ui_state_sub_id"):
            self._ui_state.unsubscribe(self._ui_state_sub_id)

        if hasattr(self, "voice_panel"):
            self.voice_panel.cleanup()
        if self.status_bar:
            self.status_bar.cleanup()
        if hasattr(self, "model_panel") and self.model_panel is not None:
            self.model_panel.cleanup()
        if hasattr(self, "_advanced_panel"):
            self._advanced_panel.cleanup()
        if hasattr(self, "cohost_agenda_panel"):
            self.cohost_agenda_panel.cleanup()
        if hasattr(self, "smart_agg_ui"):
            self.smart_agg_ui.cleanup()
        if hasattr(self, "stream_admin_ui"):
            self.stream_admin_ui.cleanup()
        if hasattr(self, "_avatar_panel"):
            self._avatar_panel.cleanup()

        self._stop_speaking_alt_timer()
        self._stop_inactivity_timer()

        # Disconnect OBS WebSocket and cancel any pending retry loop.
        self._obs_stop_runtime()

        self.ptt.stop_listener()

        # Stop health monitor daemon
        if self.health_monitor:
            try:
                self.health_monitor.stop()
            except Exception as e:
                logger.warning(f"HealthMonitor cleanup error: {e}")

        # FR4 (ui_thread_hardening_agenda_audio_20260624): graceful audio-bed teardown.
        # shutdown() hard-stops the channel and cancels the daemon _idle_check_timer,
        # which otherwise lingers until process exit.
        if getattr(self, "audio_bed", None) is not None:
            try:
                self.audio_bed.shutdown()
            except Exception as e:
                logger.warning(f"AudioBed shutdown error: {e}")

        # Set avatar to sleeping on close
        if hasattr(self, "_avatar_bridge"):
            self._avatar_bridge.set_state(AvatarState.SLEEPING)

        try:
            if self.smart_agg:
                self.smart_agg.disconnect()
        except Exception as e:
            logger.warning(f"No se pudo desconectar Smart Aggregator: {e}")

        try:
            x = self.winfo_x()
            y = self.winfo_y()
            w = self.winfo_width()
            h = self.winfo_height()
            _guardar_geometria(x, y, w, h)
        except Exception:
            pass

        try:
            release_model = getattr(self.motor_ia, "release_owned_ollama_model", None)
            if callable(release_model):
                release_model(timeout=2.0)
        except Exception as e:
            logger.warning(f"No se pudo liberar memoria Ollama al salir: {e}")

        self.motor_ia.command_queue.put(None)

        try:
            cleanup_opencohost_temp_artifacts(TEMP_DIR, logger, min_age_seconds=0.0)
        except Exception as e:
            logger.warning(f"No se pudo limpiar temporales de la app al salir: {e}")

        self.destroy()
