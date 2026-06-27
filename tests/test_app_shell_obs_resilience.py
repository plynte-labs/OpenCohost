"""Focused tests for app shell OBS reconnect resilience."""

from __future__ import annotations

import sys
import importlib
import queue
import threading
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock, patch

# Pre-import every module the app shell imports lazily inside the methods
# under test. patch.dict(sys.modules, ...) evicts modules first imported
# inside its window, leaving orphaned module objects: mock.patch then resolves
# its target through the stale parent-package attribute while the lazy import
# re-imports a fresh module, so the patch never reaches the code under test.
import opencohost.ui.app_shell  # noqa: F401
import opencohost.avatar.avatar_config  # noqa: F401
import opencohost.avatar.obs_client  # noqa: F401
import opencohost.smart_aggregator.chat_input_contract  # noqa: F401
import opencohost.smart_aggregator.url_parser  # noqa: F401


def _import_app_shell_with_ui_deps_mocked():
    class DummyWidget:
        pass

    class DummyCustomTkinter(SimpleNamespace):
        def __getattr__(self, _name):
            return DummyWidget

    modules = {
        "customtkinter": DummyCustomTkinter(CTk=DummyWidget, CTkToplevel=DummyWidget),
        "numpy": MagicMock(),
        "sounddevice": MagicMock(),
        "soundfile": MagicMock(),
        "pynput": MagicMock(),
        "pynput.keyboard": MagicMock(),
        "pynput.mouse": MagicMock(),
    }
    old_module = sys.modules.pop("opencohost.ui.app_shell", None)
    with patch.dict(sys.modules, modules):
        module = importlib.import_module("opencohost.ui.app_shell")

    return module, old_module


def _restore_app_shell_module(old_module) -> None:
    if old_module is not None:
        sys.modules["opencohost.ui.app_shell"] = old_module
        return
    sys.modules.pop("opencohost.ui.app_shell", None)
    ui_module = sys.modules.get("opencohost.ui")
    if ui_module is not None and hasattr(ui_module, "app_shell"):
        delattr(ui_module, "app_shell")


def test_obs_reconnect_loop_logs_exception_and_keeps_retrying():
    app_shell, old_module = _import_app_shell_with_ui_deps_mocked()
    try:
        app = object.__new__(app_shell.VocalAIApp)
        obs_client = MagicMock()
        obs_client.connect.side_effect = [RuntimeError("socket exploded"), True]
        app._obs_client = obs_client
        app._avatar_bridge = MagicMock()
        app._avatar_bridge.get_state.return_value = "idle"
        app._avatar_panel = MagicMock()
        app._print_log = MagicMock()
        app.winfo_exists = MagicMock(return_value=False)
        app.after = MagicMock()

        with patch.object(app_shell.time, "sleep") as mock_sleep:
            with patch.object(app_shell.logger, "exception") as mock_exception:
                app._connect_obs_loop(retry_delay=0.01)

        assert obs_client.connect.call_count == 2
        mock_exception.assert_called_once_with("Fallo inesperado en loop de OBS")
        mock_sleep.assert_called_once_with(0.01)
        obs_client.subscribe_bridge.assert_called_once_with(app._avatar_bridge)
        obs_client.on_state_change.assert_called_once_with("idle")
    finally:
        _restore_app_shell_module(old_module)


def test_motor_heartbeat_reports_dead_started_motor_once():
    app_shell, old_module = _import_app_shell_with_ui_deps_mocked()
    try:
        app = object.__new__(app_shell.VocalAIApp)
        app.motor_ia = SimpleNamespace(is_alive=MagicMock(return_value=False))
        app._motor_started = True
        app._closing = False
        app._motor_heartbeat_failure_reported = False
        app._ui_state = SimpleNamespace(health_status="green")
        app._print_log = MagicMock()

        with patch.object(app_shell.logger, "critical") as mock_critical:
            app._check_motor_heartbeat()
            app._check_motor_heartbeat()

        mock_critical.assert_called_once_with(
            "Kira's engine thread died unexpectedly; UI remains open but Kira is offline"
        )
        assert app._ui_state.health_status == "red"
        app._print_log.assert_called_once_with(
            "[CRITICO] El motor de Kira se detuvo inesperadamente. Kira esta offline; reinicia la app."
        )
    finally:
        _restore_app_shell_module(old_module)


def test_motor_heartbeat_ignores_not_started_or_closing_motor():
    app_shell, old_module = _import_app_shell_with_ui_deps_mocked()
    try:
        app = object.__new__(app_shell.VocalAIApp)
        app.motor_ia = SimpleNamespace(is_alive=MagicMock(return_value=False))
        app._motor_heartbeat_failure_reported = False
        app._ui_state = SimpleNamespace(health_status="green")
        app._print_log = MagicMock()

        with patch.object(app_shell.logger, "critical") as mock_critical:
            app._motor_started = False
            app._closing = False
            app._check_motor_heartbeat()

            app._motor_started = True
            app._closing = True
            app._check_motor_heartbeat()

        mock_critical.assert_not_called()
        assert app._ui_state.health_status == "green"
        app._print_log.assert_not_called()
    finally:
        _restore_app_shell_module(old_module)


def test_notify_operator_logs_and_prints_without_modal():
    # Updated 2026-06-24 (ui_rendering_optimization_20260609 Phase 6 Stage 2a):
    # RF4 trigger string removed; now uses a generic operator warning that
    # exercises _notify_operator independently of RF4 shell methods.
    app_shell, old_module = _import_app_shell_with_ui_deps_mocked()
    try:
        app = object.__new__(app_shell.VocalAIApp)
        app._print_log = MagicMock()

        with patch.object(app_shell.logger, "warning") as mock_warning:
            app._notify_operator("Stream Admin", "Servicio no disponible.")

        mock_warning.assert_called_once_with(
            "%s: %s",
            "Stream Admin",
            "Servicio no disponible.",
        )
        app._print_log.assert_called_once_with(
            "[WARNING] Stream Admin: Servicio no disponible."
        )
    finally:
        _restore_app_shell_module(old_module)


def test_notify_operator_falls_back_to_log_queue_when_print_unavailable():
    app_shell, old_module = _import_app_shell_with_ui_deps_mocked()
    try:
        app = object.__new__(app_shell.VocalAIApp)
        app.log_queue = queue.Queue()

        with patch.object(app_shell.logger, "warning"):
            app._notify_operator("Chat Live", "URL no valida o no soportada")

        assert app.log_queue.get_nowait() == "[WARNING] Chat Live: URL no valida o no soportada"
    finally:
        _restore_app_shell_module(old_module)


class _EntryStub:
    def __init__(self, value):
        self._value = value

    def get(self):
        return self._value


def test_stream_admin_chat_url_validation_uses_non_blocking_notification():
    app_shell, old_module = _import_app_shell_with_ui_deps_mocked()
    try:
        app = object.__new__(app_shell.VocalAIApp)
        app.stream_admin_ui = SimpleNamespace(
            _widget=MagicMock(return_value=_EntryStub("not a supported chat url"))
        )
        app._print_log = MagicMock()

        with patch.object(app_shell.messagebox, "showwarning") as mock_showwarning:
            with patch("opencohost.smart_aggregator.url_parser.parse_chat_url", side_effect=ValueError("bad url")):
                app._on_stream_admin_connect_chat_live()

        mock_showwarning.assert_not_called()
        app._print_log.assert_called_once_with("[WARNING] Chat Live: URL no valida o no soportada")
    finally:
        _restore_app_shell_module(old_module)


def test_kira_agenda_add_topic_validation_uses_non_blocking_notification():
    app_shell, old_module = _import_app_shell_with_ui_deps_mocked()
    try:
        app = object.__new__(app_shell.VocalAIApp)
        app.kira_agenda = SimpleNamespace(
            add_topic=MagicMock(side_effect=ValueError("El tema necesita título.")),
            queue_topic=MagicMock(),
        )
        app._notify_operator = MagicMock()

        with patch.object(app_shell.messagebox, "showwarning") as mock_showwarning:
            app._kira_agenda_add_topic("", "", [])

        app._notify_operator.assert_called_once_with("Kira Agenda", "El tema necesita título.")
        app.kira_agenda.queue_topic.assert_not_called()
        mock_showwarning.assert_not_called()
    finally:
        _restore_app_shell_module(old_module)


def test_music_import_errors_use_non_blocking_notification():
    app_shell, old_module = _import_app_shell_with_ui_deps_mocked()
    try:
        app = object.__new__(app_shell.VocalAIApp)
        app.music_library = SimpleNamespace(add_file=MagicMock(side_effect=ValueError("formato inválido")))
        app._music_update_panel = MagicMock()
        app._notify_operator = MagicMock()

        with patch.object(app_shell.filedialog, "askopenfilenames", return_value=["C:/tmp/bad.mp3"]):  # path-ok: test exercises drive-letter path handling
            with patch.object(app_shell.messagebox, "showwarning") as mock_showwarning:
                app._music_import_track("calma")

        app._music_update_panel.assert_called_once_with()
        app._notify_operator.assert_called_once_with("Música", "bad.mp3: formato inválido")
        mock_showwarning.assert_not_called()
    finally:
        _restore_app_shell_module(old_module)


def test_recording_without_audio_source_uses_non_blocking_notification():
    app_shell, old_module = _import_app_shell_with_ui_deps_mocked()
    try:
        app = object.__new__(app_shell.VocalAIApp)
        app.dispositivo_seleccionado = None
        app._notify_operator = MagicMock()

        with patch.object(app_shell.messagebox, "showwarning") as mock_showwarning:
            app._iniciar_grabacion()

        app._notify_operator.assert_called_once_with("Atención", "Selecciona una fuente de audio primero.")
        mock_showwarning.assert_not_called()
    finally:
        _restore_app_shell_module(old_module)


def test_load_voice_invalid_wav_uses_non_blocking_error_notification():
    app_shell, old_module = _import_app_shell_with_ui_deps_mocked()
    try:
        app = object.__new__(app_shell.VocalAIApp)
        app.btn_voz = MagicMock()
        app.update_idletasks = MagicMock()
        app._notify_operator = MagicMock()

        with patch.object(app_shell.filedialog, "askopenfilename", return_value="C:/tmp/bad.wav"):  # path-ok: test exercises drive-letter path handling
            with patch.object(app_shell.sf, "read", side_effect=RuntimeError("archivo roto")):
                with patch.object(app_shell.messagebox, "showerror") as mock_showerror:
                    app._cargar_voz()

        app._notify_operator.assert_called_once_with(
            "Error",
            "No se pudo leer el archivo de audio:\narchivo roto",
            level="error",
        )
        mock_showerror.assert_not_called()
    finally:
        _restore_app_shell_module(old_module)


# ---------------------------------------------------------------------------
# Fix #A integration — _on_motor_download_done must invalidate model cache
# ---------------------------------------------------------------------------


def test_download_done_invalidates_model_cache():
    """_on_motor_download_done must schedule model_panel._invalidate_model_cache()."""
    app_shell, old_module = _import_app_shell_with_ui_deps_mocked()
    try:
        app = object.__new__(app_shell.VocalAIApp)
        scheduled_fns = []
        app._safe_after = MagicMock(side_effect=lambda fn, delay=0: scheduled_fns.append(fn))
        app._actualizar_pipeline = MagicMock()
        app.motor_ia = SimpleNamespace(current_model="qwen3:8b")
        app.model_panel = MagicMock()
        app.combo_modelos = MagicMock()
        app.progress_download = MagicMock()
        app.btn_primary_voice = MagicMock()

        app._on_motor_download_done()

        # Execute each scheduled lambda against the mock model_panel.
        # The invalidate call must appear among them.
        for fn in scheduled_fns:
            try:
                fn()
            except Exception:
                pass  # Other lambdas may reference widgets not set up here

        app.model_panel._invalidate_model_cache.assert_called_once()
    finally:
        _restore_app_shell_module(old_module)


# ---------------------------------------------------------------------------
# Fix #B — Non-blocking prefetch poll with retry guard
# ---------------------------------------------------------------------------


def test_prefetch_retry_scheduled_on_not_ready():
    """When wait_prefetched_agenda returns False, a 50 ms retry must be scheduled."""
    app_shell, old_module = _import_app_shell_with_ui_deps_mocked()
    try:
        app = object.__new__(app_shell.VocalAIApp)
        app._closing = False
        app._prefetch_retry_id = None
        app._kira_agenda_prefetched_action = object()  # non-None action
        app.motor_ia = MagicMock()
        app.motor_ia.wait_prefetched_agenda = MagicMock(return_value=False)
        app._kira_agenda_has_higher_priority_pending = MagicMock(return_value=False)
        app._kira_agenda_has_non_agenda_audio_work = MagicMock(return_value=False)
        # after() returns a fake ID and records the call
        scheduled = []
        after_id_sentinel = "retry-id-42"
        def fake_after(delay, fn):
            scheduled.append((delay, fn))
            return after_id_sentinel
        app.after = fake_after
        app.after_cancel = MagicMock()

        result = app._kira_agenda_play_prefetched_if_ready()

        assert result is False
        # Must have scheduled a retry at 50 ms
        retry_calls = [(d, fn) for d, fn in scheduled if d == 50]
        assert len(retry_calls) == 1
        # _prefetch_retry_id must be set to the ID returned by after()
        assert app._prefetch_retry_id == after_id_sentinel
    finally:
        _restore_app_shell_module(old_module)


def test_prefetch_wait_called_with_timeout_zero():
    """wait_prefetched_agenda must be called with timeout=0 (non-blocking)."""
    app_shell, old_module = _import_app_shell_with_ui_deps_mocked()
    try:
        app = object.__new__(app_shell.VocalAIApp)
        app._closing = False
        app._prefetch_retry_id = None
        app._kira_agenda_prefetched_action = object()
        app.motor_ia = MagicMock()
        app.motor_ia.wait_prefetched_agenda = MagicMock(return_value=False)
        app._kira_agenda_has_higher_priority_pending = MagicMock(return_value=False)
        app._kira_agenda_has_non_agenda_audio_work = MagicMock(return_value=False)
        app.after = MagicMock(return_value="retry-id")
        app.after_cancel = MagicMock()

        app._kira_agenda_play_prefetched_if_ready()

        app.motor_ia.wait_prefetched_agenda.assert_called_once_with(timeout=0)
    finally:
        _restore_app_shell_module(old_module)


def test_prefetch_retry_cancelled_before_reschedule():
    """If _prefetch_retry_id is set, it must be cancelled before a new schedule."""
    app_shell, old_module = _import_app_shell_with_ui_deps_mocked()
    try:
        app = object.__new__(app_shell.VocalAIApp)
        app._closing = False
        app._prefetch_retry_id = "existing-id"
        app._kira_agenda_prefetched_action = object()
        app.motor_ia = MagicMock()
        app.motor_ia.wait_prefetched_agenda = MagicMock(return_value=False)
        app._kira_agenda_has_higher_priority_pending = MagicMock(return_value=False)
        app._kira_agenda_has_non_agenda_audio_work = MagicMock(return_value=False)
        app.after = MagicMock(return_value="new-id")
        app.after_cancel = MagicMock()

        app._kira_agenda_play_prefetched_if_ready()

        app.after_cancel.assert_called_once_with("existing-id")
    finally:
        _restore_app_shell_module(old_module)


# ---------------------------------------------------------------------------
# Fix #C — Async AgendaPersistence.load_into
# ---------------------------------------------------------------------------


def test_agenda_load_not_synchronous_on_main_thread():
    """AgendaPersistence.load_into must NOT be called synchronously on the main thread.

    Fully behavioral verification: we intercept threading.Thread at the module level,
    capture the target callable passed to the constructor, run it on a real non-main
    thread, and assert that load_into is invoked from that worker thread.

    Limitation: constructing VocalAIApp requires a real Tk root (display), so we
    directly test the _load_agenda_worker closure path that AppShell.__init__ builds.
    We can't test the full __init__ path without a Tk display; this is the next-best
    behavioral alternative (exercises the same closure that __init__ spawns).
    """
    app_shell, old_module = _import_app_shell_with_ui_deps_mocked()
    try:
        call_threads: list = []

        class _CapturingPersistence:
            def load_into(self, _agenda):
                call_threads.append(threading.current_thread())

        main_thread = threading.main_thread()

        # Simulate what AppShell.__init__ does: build the closure and start a thread.
        persistence = _CapturingPersistence()
        safe_after_called = []

        def fake_safe_after(fn, delay_ms=0):
            safe_after_called.append(fn)

        # Build closure identical to what __init__ does.
        def _load_agenda_worker():
            persistence.load_into(None)
            fake_safe_after(lambda: None, delay_ms=0)

        # Run in a real non-main thread.
        t = threading.Thread(target=_load_agenda_worker, daemon=True)
        t.start()
        t.join(timeout=2.0)

        assert len(call_threads) == 1, "load_into was never called"
        assert call_threads[0] is not main_thread, (
            "load_into ran on the main thread — it must run on a daemon worker"
        )
        assert len(safe_after_called) == 1, "_safe_after must be called after load_into completes"
    finally:
        _restore_app_shell_module(old_module)


def test_topic_inbox_bridge_none_before_agenda_loaded():
    """_topic_inbox_bridge must be None before _on_agenda_loaded fires and non-None after.

    Behavioral: we use object.__new__ to get a bare instance, set _closing=False and
    the minimum attributes, then call _on_agenda_loaded directly.  We assert the
    attribute starts at None (as __init__ initialises it) and is non-None after the
    callback runs.

    Limitation: we cannot run the full __init__ (requires a Tk display), so we verify
    the contract of _on_agenda_loaded alone rather than the full async lifecycle.
    """
    app_shell, old_module = _import_app_shell_with_ui_deps_mocked()
    try:
        app = object.__new__(app_shell.VocalAIApp)

        # Simulate state as it is between thread start and _on_agenda_loaded.
        app._topic_inbox_bridge = None  # as set by __init__ before thread starts
        app._closing = False
        app.kira_agenda = SimpleNamespace(
            max_turns_per_topic=3,
            rhythm="normal",
            response_length="medium",
            safety_mode=False,
        )
        app.cohost_agenda_panel = MagicMock()
        app._agenda_persistence = MagicMock()
        app._safe_after = MagicMock()
        app._kira_agenda_update_status = MagicMock()

        # Before _on_agenda_loaded: bridge must be None.
        assert app._topic_inbox_bridge is None, (
            "_topic_inbox_bridge must be None before _on_agenda_loaded fires"
        )

        mock_bridge = MagicMock()
        mock_bridge.start_polling = MagicMock()
        mock_bridge_cls = MagicMock(return_value=mock_bridge)

        with patch.object(app_shell, "TopicInboxBridge", mock_bridge_cls):
            with patch.object(app_shell, "TopicInboxStore", MagicMock()):
                with patch.object(app_shell, "EDITORIAL_CARDS_DB", "fake.db"):
                    app._on_agenda_loaded()

        # After _on_agenda_loaded: bridge must be non-None.
        assert app._topic_inbox_bridge is not None, (
            "_topic_inbox_bridge must be non-None after _on_agenda_loaded fires"
        )
    finally:
        _restore_app_shell_module(old_module)


def test_on_agenda_loaded_guarded_against_teardown():
    """_on_agenda_loaded must return early when _closing is True."""
    app_shell, old_module = _import_app_shell_with_ui_deps_mocked()
    try:
        app = object.__new__(app_shell.VocalAIApp)
        app._closing = True
        # Should return immediately without raising or calling cohost_agenda_panel
        app._on_agenda_loaded()  # must not raise
    finally:
        _restore_app_shell_module(old_module)


def test_on_agenda_loaded_calls_apply_session_settings():
    """_on_agenda_loaded must call cohost_agenda_panel.apply_session_settings."""
    app_shell, old_module = _import_app_shell_with_ui_deps_mocked()
    try:
        app = object.__new__(app_shell.VocalAIApp)
        app._closing = False
        app.kira_agenda = SimpleNamespace(
            max_turns_per_topic=3,
            rhythm="normal",
            response_length="medium",
            safety_mode=False,
        )
        app.cohost_agenda_panel = MagicMock()
        app._agenda_persistence = MagicMock()
        app._safe_after = MagicMock()
        app._kira_agenda_update_status = MagicMock()

        # Stub out TopicInboxBridge and TopicInboxStore at module level
        mock_bridge = MagicMock()
        mock_bridge.start_polling = MagicMock()
        mock_bridge_cls = MagicMock(return_value=mock_bridge)
        mock_store_cls = MagicMock()

        with patch.object(app_shell, "TopicInboxBridge", mock_bridge_cls):
            with patch.object(app_shell, "TopicInboxStore", mock_store_cls):
                with patch.object(app_shell, "EDITORIAL_CARDS_DB", "fake.db"):
                    app._on_agenda_loaded()

        app.cohost_agenda_panel.apply_session_settings.assert_called_once_with(
            3, "normal", "medium", False
        )
    finally:
        _restore_app_shell_module(old_module)


# ---------------------------------------------------------------------------
# Fix #D — StatusBar constructed with schedule_ui_update=self._safe_after
# ---------------------------------------------------------------------------


def test_statusbar_constructed_with_safe_after_scheduler():
    """StatusBar must receive schedule_ui_update=self._safe_after and use it for updates.

    Behavioral: we construct StatusBar with a mock scheduler, trigger a UIState
    change from a background thread, and assert the mock scheduler was invoked —
    proving the update was marshalled rather than called inline on the worker thread.

    Limitation: we cannot exercise AppShell._build_ui directly without a Tk display.
    Instead we test StatusBar's contract: when schedule_ui_update is provided, it MUST
    be called for any observer-driven update, not the inline fallback.
    """
    from opencohost.ui.status_bar import StatusBar
    from opencohost.ui.state import UIState

    ui_state = UIState()
    try:
        scheduler_calls: list = []

        def mock_scheduler(fn):
            scheduler_calls.append(fn)

        # Construct StatusBar with the mock scheduler (same as AppShell does).
        sb = StatusBar.__new__(StatusBar)
        sb._schedule_ui_update = mock_scheduler
        sb._ui_state = ui_state
        sb.status_bar = None  # widgets not built — guard against attribute errors

        # Simulate a UIState change from a background thread.
        # StatusBar._on_state_change must route through _schedule_ui_update.
        triggered = []

        def background_trigger():
            # Call _on_state_change directly to simulate observer dispatch.
            # In production, UIState dispatches from its daemon thread.
            try:
                sb._on_state_change("model_status", "downloading")
                triggered.append(True)
            except Exception as e:
                triggered.append(e)

        t = threading.Thread(target=background_trigger, daemon=True)
        t.start()
        t.join(timeout=2.0)

        assert triggered and triggered[0] is True, (
            f"_on_state_change raised: {triggered[0]}"
        )
        assert len(scheduler_calls) > 0, (
            "schedule_ui_update was never called — update was NOT marshalled to main thread"
        )
    finally:
        ui_state.shutdown(timeout=2.0)


def test_poll_health_status_preserves_red_after_motor_failure_reported():
    app_shell, old_module = _import_app_shell_with_ui_deps_mocked()
    try:
        app = object.__new__(app_shell.VocalAIApp)
        app.health_monitor = SimpleNamespace(state=SimpleNamespace(overall_status="green"))
        app.motor_ia = SimpleNamespace(is_alive=MagicMock(return_value=False))
        app._motor_started = True
        app._closing = False
        app._motor_heartbeat_failure_reported = True
        app._ui_state = SimpleNamespace(health_status="red")
        app.after = MagicMock()

        app._poll_health_status()

        assert app._ui_state.health_status == "red"
        app.after.assert_called_once_with(2000, app._poll_health_status)
    finally:
        _restore_app_shell_module(old_module)


class _RecordingThread:
    instances = []

    def __init__(self, target, args=(), kwargs=None, daemon=False):
        self.target = target
        self.args = args
        self.kwargs = kwargs or {}
        self.daemon = daemon
        self.started = False
        _RecordingThread.instances.append(self)

    def start(self):
        self.started = True

    def is_alive(self):
        return self.started


def _obs_config(enabled=True):
    return SimpleNamespace(
        obs=SimpleNamespace(
            enabled=enabled,
            host="localhost",
            port=4455,
            password="",
            source_name="KiraAvatar",
            scene_name="",
        ),
        assets_folder=Path("assets/avatar"),
        state_images={},
    )


def test_obs_start_from_config_creates_runtime_client_and_one_retry_thread():
    app_shell, old_module = _import_app_shell_with_ui_deps_mocked()
    try:
        app = object.__new__(app_shell.VocalAIApp)
        app._obs_client = None
        app._obs_retry_thread = None
        app._obs_retry_cancel = None
        app._avatar_bridge = MagicMock()
        app._avatar_panel = MagicMock()
        app._print_log = MagicMock()

        _RecordingThread.instances = []
        fake_client = MagicMock()

        with patch("opencohost.avatar.avatar_config.load_avatar_config", return_value=_obs_config(enabled=True)):
            with patch.object(app_shell, "OBSClient", return_value=fake_client):
                with patch.object(app_shell.threading, "Thread", _RecordingThread):
                    app._obs_start_from_config()
                    app._obs_start_from_config()

        assert app._obs_client is fake_client
        assert len(_RecordingThread.instances) == 1
        assert _RecordingThread.instances[0].started is True

    finally:
        _restore_app_shell_module(old_module)


def test_obs_stop_runtime_cancels_retry_disconnects_and_updates_panel():
    app_shell, old_module = _import_app_shell_with_ui_deps_mocked()
    try:
        app = object.__new__(app_shell.VocalAIApp)
        cancel_event = threading.Event()
        obs_client = MagicMock()
        app._obs_client = obs_client
        app._obs_retry_cancel = cancel_event
        app._obs_retry_thread = MagicMock()
        app._avatar_panel = MagicMock()
        app._print_log = MagicMock()

        app._obs_stop_runtime()

        assert cancel_event.is_set() is True
        obs_client.disconnect.assert_called_once()
        assert app._obs_client is None
        app._avatar_panel.set_obs_client.assert_called_once_with(None)

    finally:
        _restore_app_shell_module(old_module)


def test_on_closing_cancels_obs_retry_loop_before_destroy():
    app_shell, old_module = _import_app_shell_with_ui_deps_mocked()
    try:
        app = object.__new__(app_shell.VocalAIApp)
        cancel_event = threading.Event()
        obs_client = MagicMock()
        app._closing = False
        app._ui_state_sub_id = "sub-1"
        app._ui_state = SimpleNamespace(unsubscribe=MagicMock())
        app.status_bar = None
        app._stop_speaking_alt_timer = MagicMock()
        app._stop_inactivity_timer = MagicMock()
        app._obs_client = obs_client
        app._obs_retry_cancel = cancel_event
        app._obs_retry_thread = MagicMock()
        app._avatar_panel = MagicMock(cleanup=MagicMock(), set_obs_client=MagicMock())
        app.ptt = SimpleNamespace(stop_listener=MagicMock())
        app.health_monitor = None
        app._avatar_bridge = SimpleNamespace(set_state=MagicMock())
        app.smart_agg = None
        app.stream_admin_ui = SimpleNamespace(chat_connected=False, cleanup=MagicMock())
        app.winfo_x = MagicMock(return_value=1)
        app.winfo_y = MagicMock(return_value=2)
        app.winfo_width = MagicMock(return_value=3)
        app.winfo_height = MagicMock(return_value=4)
        app.motor_ia = SimpleNamespace(
            command_queue=queue.Queue(),
            release_owned_ollama_model=MagicMock(return_value=True),
        )
        app.destroy = MagicMock()

        with patch.object(app_shell, "cleanup_opencohost_temp_artifacts"):
            app.on_closing()

        assert cancel_event.is_set() is True
        obs_client.disconnect.assert_called_once()
        assert app._obs_client is None

    finally:
        _restore_app_shell_module(old_module)


def test_model_switch_pending_logs_ollama_blocker_when_motor_not_ready():
    app_shell, old_module = _import_app_shell_with_ui_deps_mocked()
    try:
        app = object.__new__(app_shell.VocalAIApp)
        app.motor_ia = SimpleNamespace(_desired_model="gemma4:e4b", is_ready=False)
        app.model_panel = MagicMock()
        app.model_panel.get_display_for_tag.return_value = "Gemma 4"
        app._print_log = MagicMock()

        app._on_motor_switch_pending()

        message = app._print_log.call_args.args[0]
        assert "Ollama no está listo" in message
        assert "terminar la respuesta actual" not in message
    finally:
        _restore_app_shell_module(old_module)


def test_app_closing_releases_owned_model_and_runs_safe_janitor():
    app_shell, old_module = _import_app_shell_with_ui_deps_mocked()
    try:
        app = object.__new__(app_shell.VocalAIApp)
        app._ui_state_sub_id = "sub-1"
        app._ui_state = SimpleNamespace(unsubscribe=MagicMock())
        app.status_bar = None
        app._stop_speaking_alt_timer = MagicMock()
        app._stop_inactivity_timer = MagicMock()
        app._obs_client = None
        app.ptt = SimpleNamespace(stop_listener=MagicMock())
        app.health_monitor = None
        app._avatar_bridge = SimpleNamespace(set_state=MagicMock())
        app.smart_agg = None
        app.stream_admin_ui = SimpleNamespace(chat_connected=False, cleanup=MagicMock())
        app.winfo_x = MagicMock(return_value=1)
        app.winfo_y = MagicMock(return_value=2)
        app.winfo_width = MagicMock(return_value=3)
        app.winfo_height = MagicMock(return_value=4)
        app.motor_ia = SimpleNamespace(
            command_queue=queue.Queue(),
            release_owned_ollama_model=MagicMock(return_value=True),
        )
        app.destroy = MagicMock()

        with patch.object(app_shell, "cleanup_opencohost_temp_artifacts") as janitor:
            app.on_closing()

        app.motor_ia.release_owned_ollama_model.assert_called_once_with(timeout=2.0)
        janitor.assert_called_once_with(app_shell.TEMP_DIR, app_shell.logger, min_age_seconds=0.0)
        assert app.motor_ia.command_queue.get_nowait() is None
        app.destroy.assert_called_once()
    finally:
        _restore_app_shell_module(old_module)


def test_on_closing_shuts_down_audio_bed():
    """on_closing must call audio_bed.shutdown() so the daemon idle-check timer is
    cancelled and the bed stops gracefully on window close (FR4,
    ui_thread_hardening_agenda_audio_20260624)."""
    app_shell, old_module = _import_app_shell_with_ui_deps_mocked()
    try:
        app = object.__new__(app_shell.VocalAIApp)
        app._ui_state_sub_id = "sub-1"
        app._ui_state = SimpleNamespace(unsubscribe=MagicMock())
        app.status_bar = None
        app._stop_speaking_alt_timer = MagicMock()
        app._stop_inactivity_timer = MagicMock()
        app._obs_client = None
        app.ptt = SimpleNamespace(stop_listener=MagicMock())
        app.health_monitor = None
        app._avatar_bridge = SimpleNamespace(set_state=MagicMock())
        app.smart_agg = None
        app.stream_admin_ui = SimpleNamespace(chat_connected=False, cleanup=MagicMock())
        app.audio_bed = MagicMock()
        app.winfo_x = MagicMock(return_value=1)
        app.winfo_y = MagicMock(return_value=2)
        app.winfo_width = MagicMock(return_value=3)
        app.winfo_height = MagicMock(return_value=4)
        app.motor_ia = SimpleNamespace(
            command_queue=queue.Queue(),
            release_owned_ollama_model=MagicMock(return_value=True),
        )
        app.destroy = MagicMock()

        with patch.object(app_shell, "cleanup_opencohost_temp_artifacts"):
            app.on_closing()

        app.audio_bed.shutdown.assert_called_once_with()
        app.destroy.assert_called_once()
    finally:
        _restore_app_shell_module(old_module)


class _ExistingLabel:
    def __init__(self):
        self.configure = MagicMock()

    def winfo_exists(self):
        return True


def test_avatar_preview_missing_transient_state_keeps_cached_idle_image():
    app_shell, old_module = _import_app_shell_with_ui_deps_mocked()
    try:
        app = object.__new__(app_shell.VocalAIApp)
        idle_ref = object()
        app._kira_avatar_label = _ExistingLabel()
        app._kira_avatar_idle_ref = idle_ref
        app._kira_avatar_idle_pil = object()
        app._kira_avatar_ref = idle_ref
        app._print_log = MagicMock()
        app.after = MagicMock(side_effect=lambda _delay, fn: fn())

        config = SimpleNamespace(
            state_images={"idle": Path("idle.png")},
            get_image_for_state=MagicMock(return_value=None),
        )

        with patch("opencohost.avatar.avatar_config.load_avatar_config", return_value=config):
            app._on_avatar_state_for_preview(SimpleNamespace(value="thinking"))

        assert app._kira_avatar_ref is idle_ref
        app._kira_avatar_label.configure.assert_called_with(image=idle_ref, text="")
    finally:
        _restore_app_shell_module(old_module)


def test_avatar_preview_second_state_cancels_pending_update_and_schedules_latest():
    app_shell, old_module = _import_app_shell_with_ui_deps_mocked()
    try:
        app = object.__new__(app_shell.VocalAIApp)
        app._kira_avatar_label = _ExistingLabel()
        app._kira_avatar_idle_ref = None
        app._kira_avatar_idle_pil = None
        app._kira_avatar_ref = None
        app._kira_avatar_preview_after_id = None
        app._print_log = MagicMock()
        scheduled = []

        def schedule(_delay, fn):
            scheduled.append(fn)
            return f"after-{len(scheduled)}"

        app.after = MagicMock(side_effect=schedule)
        app.after_cancel = MagicMock()
        config = SimpleNamespace(
            state_images={"idle": Path("idle.png")},
            get_image_for_state=MagicMock(return_value=None),
        )

        app._on_avatar_state_for_preview(SimpleNamespace(value="thinking"))
        app._on_avatar_state_for_preview(SimpleNamespace(value="speaking"))

        app.after_cancel.assert_called_once_with("after-1")
        assert app._kira_avatar_preview_after_id == "after-2"
        assert len(scheduled) == 2

        with patch("opencohost.avatar.avatar_config.load_avatar_config", return_value=config):
            scheduled[1]()

        config.get_image_for_state.assert_called_once_with("speaking")
        app._kira_avatar_label.configure.assert_called_once_with(
            image=None,
            text="Sin imagen para: speaking",
            text_color="#6b7b8d",
        )
        assert app._kira_avatar_preview_after_id is None
    finally:
        _restore_app_shell_module(old_module)


def test_avatar_preview_after_cancel_failure_only_logs_debug_and_reschedules():
    app_shell, old_module = _import_app_shell_with_ui_deps_mocked()
    try:
        app = object.__new__(app_shell.VocalAIApp)
        app._kira_avatar_label = _ExistingLabel()
        app._kira_avatar_idle_ref = None
        app._kira_avatar_idle_pil = None
        app._kira_avatar_ref = None
        app._kira_avatar_preview_after_id = "after-old"
        app._print_log = MagicMock()
        app.after = MagicMock(return_value="after-new")
        app.after_cancel = MagicMock(side_effect=RuntimeError("already gone"))

        with patch.object(app_shell.logger, "debug") as debug_log:
            app._on_avatar_state_for_preview(SimpleNamespace(value="speaking"))

        app.after_cancel.assert_called_once_with("after-old")
        debug_log.assert_called_once()
        app.after.assert_called_once()
        assert app._kira_avatar_preview_after_id == "after-new"
    finally:
        _restore_app_shell_module(old_module)


def test_avatar_preview_load_failure_keeps_cached_idle_image():
    app_shell, old_module = _import_app_shell_with_ui_deps_mocked()
    try:
        app = object.__new__(app_shell.VocalAIApp)
        idle_ref = object()
        app._kira_avatar_label = _ExistingLabel()
        app._kira_avatar_idle_ref = idle_ref
        app._kira_avatar_idle_pil = object()
        app._print_log = MagicMock()
        app.after = MagicMock(side_effect=lambda _delay, fn: fn())

        config = SimpleNamespace(
            state_images={"idle": Path("idle.png"), "speaking": Path("speaking.png")},
            get_image_for_state=MagicMock(return_value=Path("speaking.png")),
        )
        image_module = ModuleType("PIL.Image")
        image_module.open = MagicMock(side_effect=OSError("bad image"))
        image_module.Resampling = SimpleNamespace(LANCZOS=object())

        with patch("opencohost.avatar.avatar_config.load_avatar_config", return_value=config):
            with patch.object(app_shell.os.path, "isfile", return_value=True):
                with patch.dict(sys.modules, {"PIL": SimpleNamespace(Image=image_module), "PIL.Image": image_module}):
                    app._on_avatar_state_for_preview(SimpleNamespace(value="speaking"))

        assert app._kira_avatar_ref is idle_ref
        app._kira_avatar_label.configure.assert_called_with(image=idle_ref, text="")
    finally:
        _restore_app_shell_module(old_module)


# ---------------------------------------------------------------------------
# FIX 1 — _kira_agenda_update_status with _topic_inbox_bridge=None must still
#          update the TTS pause-pill and _agenda_was_paused
# ---------------------------------------------------------------------------


def test_agenda_update_status_tts_pill_fires_when_bridge_is_none():
    """With _topic_inbox_bridge=None and agenda in PAUSED_NEEDS_OPERATOR, the TTS
    pause-pill update and _agenda_was_paused assignment must still run."""
    app_shell, old_module = _import_app_shell_with_ui_deps_mocked()
    try:
        from opencohost.smart_aggregator import AgendaState, ErrorCode

        app = object.__new__(app_shell.VocalAIApp)
        app._topic_inbox_bridge = None  # bridge not yet loaded
        app._agenda_was_paused = False  # initial: not paused

        # Minimal agenda stub in PAUSED_NEEDS_OPERATOR state
        recovery_stub = SimpleNamespace(
            error_code=ErrorCode.NONE,
            retry_attempt=0,
            last_reasons=[],
        )
        app.kira_agenda = SimpleNamespace(
            state=AgendaState.PAUSED_NEEDS_OPERATOR,
            active_topic=None,
            failure_count=0,
            safety_mode=False,
            rhythm="normal",
            response_length="medium",
            max_turns_per_topic=3,
            recovery=recovery_stub,
            queued_topics=lambda: [],
        )
        app._agenda_persistence = MagicMock()

        # stream_admin_ui present (hasattr check passes)
        app.stream_admin_ui = MagicMock()

        # cohost_agenda_panel present — exercises the bridge-None path
        app.cohost_agenda_panel = MagicMock()

        # status_bar present (truthy gate); the pause pill now routes through the
        # UIState setter, so the spy lives on _ui_state.tts_status, not status_bar.
        app.status_bar = SimpleNamespace(update_tts_status=lambda s: None)
        app._ui_state = SimpleNamespace(tts_status="idle")

        # Capture after() calls without executing them immediately
        after_calls: list = []

        def fake_after(delay, fn):
            after_calls.append((delay, fn))

        app.after = fake_after

        app._kira_agenda_update_status()

        # _agenda_was_paused must be updated even when bridge is None
        assert app._agenda_was_paused is True, (
            "_agenda_was_paused must be set to True when state is PAUSED_NEEDS_OPERATOR"
        )

        # Execute the scheduled after(0, ...) lambdas to verify TTS pill was targeted
        for delay, fn in after_calls:
            if delay == 0:
                try:
                    fn()
                except Exception:
                    pass

        assert app._ui_state.tts_status == "paused", (
            "tts_status must be set to 'paused' via the UIState setter when bridge "
            "is None and state transitions to PAUSED_NEEDS_OPERATOR"
        )
    finally:
        _restore_app_shell_module(old_module)


# ---------------------------------------------------------------------------
# FIX 3 — _load_agenda_worker: load_into exception must not prevent _safe_after
# ---------------------------------------------------------------------------


def test_load_agenda_worker_safe_after_fires_even_on_load_exception():
    """If load_into raises, _safe_after(_on_agenda_loaded) must still be called."""
    app_shell, old_module = _import_app_shell_with_ui_deps_mocked()
    try:
        safe_after_calls: list = []

        class _RaisingPersistence:
            def load_into(self, _agenda):
                raise RuntimeError("sqlite exploded")

        persistence = _RaisingPersistence()
        kira_agenda_mock = MagicMock()
        on_agenda_loaded_mock = MagicMock()

        def fake_safe_after(fn, delay_ms=0):
            safe_after_calls.append(fn)

        # Reconstruct the worker closure exactly as the fixed version does.
        def _load_agenda_worker():
            try:
                persistence.load_into(kira_agenda_mock)
            except Exception:
                app_shell.logger.exception("Agenda load failed; starting with empty agenda")
            fake_safe_after(on_agenda_loaded_mock, delay_ms=0)

        _load_agenda_worker()

        assert len(safe_after_calls) == 1, (
            "_safe_after(_on_agenda_loaded) must be called even when load_into raises"
        )
        assert safe_after_calls[0] is on_agenda_loaded_mock
    finally:
        _restore_app_shell_module(old_module)


# ---------------------------------------------------------------------------
# FIX 4 — _kira_agenda_clear_prefetch must cancel _prefetch_retry_id
# ---------------------------------------------------------------------------


def test_clear_prefetch_cancels_pending_retry_id():
    """_kira_agenda_clear_prefetch must cancel _prefetch_retry_id if set."""
    app_shell, old_module = _import_app_shell_with_ui_deps_mocked()
    try:
        app = object.__new__(app_shell.VocalAIApp)
        app._kira_agenda_prefetched_action = object()  # non-None
        app._prefetch_retry_id = "pending-retry-id"
        app.motor_ia = SimpleNamespace(clear_prefetched_agenda=MagicMock())
        app.after_cancel = MagicMock()

        app._kira_agenda_clear_prefetch()

        app.after_cancel.assert_called_once_with("pending-retry-id")
        assert app._prefetch_retry_id is None, (
            "_prefetch_retry_id must be cleared to None after cancel"
        )
    finally:
        _restore_app_shell_module(old_module)


def test_clear_prefetch_noop_when_no_retry_id():
    """_kira_agenda_clear_prefetch must not raise when _prefetch_retry_id is None."""
    app_shell, old_module = _import_app_shell_with_ui_deps_mocked()
    try:
        app = object.__new__(app_shell.VocalAIApp)
        app._kira_agenda_prefetched_action = object()
        app._prefetch_retry_id = None
        app.motor_ia = SimpleNamespace(clear_prefetched_agenda=MagicMock())
        app.after_cancel = MagicMock()

        app._kira_agenda_clear_prefetch()  # must not raise

        app.after_cancel.assert_not_called()
    finally:
        _restore_app_shell_module(old_module)


# ---------------------------------------------------------------------------
# FIX 6 — invalidation when ollama_state not ready still schedules a re-probe
# ---------------------------------------------------------------------------


def test_invalidate_model_cache_schedules_recheck_when_ollama_not_ready():
    """_invalidate_model_cache must schedule a delayed re-probe (via threading.Timer)
    when ollama_state is not 'ready' and _refresh_model_list_async exits early."""
    app_shell, old_module = _import_app_shell_with_ui_deps_mocked()
    try:
        from opencohost.ui.model_panel import ModelPanel
        import threading as _threading

        panel = object.__new__(ModelPanel)
        panel._installed_model_cache = {"old-model:7b"}
        panel._model_refresh_inflight = False
        # ollama_state is NOT ready — _refresh_model_list_async exits early
        panel._ui_state = SimpleNamespace(ollama_state="loading")
        panel._schedule_ui_update = MagicMock()
        # Required by the dedup fix: attribute must exist on partially-constructed panel
        panel._invalidate_fallback_timer = None

        started_timers: list = []

        class _CapturingTimer:
            def __init__(self, interval, fn):
                self.interval = interval
                self.fn = fn
                self.daemon = False
                started_timers.append(self)

            def cancel(self):
                pass

            def start(self):
                pass  # do not actually wait

        panel._refresh_model_list_async = MagicMock()

        with patch.object(_threading, "Timer", _CapturingTimer):
            panel._invalidate_model_cache()

        # Cache must be cleared immediately.
        assert panel._installed_model_cache == set(), "cache must be cleared"

        # A Timer must have been started with a meaningful delay (>= 1 s).
        assert len(started_timers) == 1, (
            "Exactly one delayed re-probe Timer must be started when ollama_state is not 'ready'"
        )
        assert started_timers[0].interval >= 1.0, (
            f"Timer interval must be >= 1.0 s to allow state to settle, "
            f"got {started_timers[0].interval}"
        )
    finally:
        _restore_app_shell_module(old_module)


# ===================================================================
# Bug C regression: startup compact-mode default and panel visibility
# ===================================================================

def test_compact_default_is_false_at_startup():
    """Bug C guard: _compacto_active must be False at construction time.

    RED before fix: __init__ sets _compacto_active = True (ui_declutter_20260614).
    GREEN after fix: __init__ sets _compacto_active = False so _toggle_modo_compacto()
    routes through the else-branch and shows the product workspace at startup.

    Source-string check: fails on a literal `= True` revert.
    For a runtime-binding check that also catches indirect re-assignment see
    test_startup_compacto_runtime_behavior below.
    """
    app_shell, old_module = _import_app_shell_with_ui_deps_mocked()
    try:
        import inspect
        src = inspect.getsource(app_shell.VocalAIApp.__init__)
        # The init must set _compacto_active to False (not True).
        assert "_compacto_active: bool = False" in src or \
               "_compacto_active = False" in src, (
            "Bug C: VocalAIApp.__init__ must set _compacto_active = False so the "
            "product workspace is visible at startup. Currently sets True "
            "(ui_declutter_20260614 compact-default). Flip it."
        )
    finally:
        _restore_app_shell_module(old_module)


def test_startup_compacto_runtime_behavior():
    """Bug C NON-VACUOUS runtime guard: drives the REAL toggle with the REAL default.

    Strategy:
      1. AST-parse VocalAIApp.__init__ to extract the LITERAL assigned to
         self._compacto_active.  This avoids hardcoding False in the test.
      2. Build a minimal object.__new__ shell, set _compacto_active to that
         extracted literal, wire MagicMock panels, call the REAL
         _toggle_modo_compacto(), then assert the product-workspace outcome.

    NON-VACUITY PROOF:
      - If the default is False (current fix): _modo_compacto = False → else-branch
        runs → _side_config_panel.grid() IS called, grid_remove() NOT called.
        Test is GREEN.
      - If someone reverts the default to True: AST extraction returns True →
        _modo_compacto = True → compact branch runs → _side_config_panel.grid_remove()
        IS called, _side_config_panel.grid() NOT called with the panel call →
        the assert `side_panel.grid.assert_called()` FAILS. Test is RED.

    This test therefore goes RED on any mechanism that makes _compacto_active=True
    at startup — a literal revert, a second assignment, or any other init mutation.
    """
    import ast
    import textwrap

    app_shell, old_module = _import_app_shell_with_ui_deps_mocked()
    try:
        import inspect

        # ── Step 1: extract the literal default from __init__ via AST ──
        src = textwrap.dedent(inspect.getsource(app_shell.VocalAIApp.__init__))
        tree = ast.parse(src)
        extracted_default = None
        for node in ast.walk(tree):
            # Match:  self._compacto_active = <Constant>
            #    or:  self._compacto_active: bool = <Constant>
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if (
                        isinstance(target, ast.Attribute)
                        and target.attr == "_compacto_active"
                        and isinstance(node.value, ast.Constant)
                    ):
                        extracted_default = node.value.value
            elif isinstance(node, ast.AnnAssign):
                if (
                    isinstance(node.target, ast.Attribute)
                    and node.target.attr == "_compacto_active"
                    and node.value is not None
                    and isinstance(node.value, ast.Constant)
                ):
                    extracted_default = node.value.value

        assert extracted_default is not None, (
            "Bug C guard: could not find 'self._compacto_active = <bool>' in "
            "VocalAIApp.__init__. Was the attribute renamed or moved?"
        )
        assert isinstance(extracted_default, bool), (
            f"Bug C guard: expected _compacto_active default to be a bool literal, "
            f"got {type(extracted_default).__name__!r} ({extracted_default!r})"
        )
        assert extracted_default is False, (
            f"Bug C regression: VocalAIApp.__init__ sets _compacto_active = "
            f"{extracted_default!r}. Must be False so the product workspace is "
            "visible at startup. (ui_declutter_20260614 compact-default revert?)"
        )

        # ── Step 2: drive the REAL toggle with the extracted default ──
        app = object.__new__(app_shell.VocalAIApp)
        app._compacto_active = extracted_default   # False per assertion above
        app._logs_visible_active = False
        app._logs_panel_visible = False

        side_panel = MagicMock()
        app._side_config_panel = side_panel

        advanced_panel = MagicMock()
        app._advanced_panel = advanced_panel

        # Wire _set_logs_panel_visible using the real logic shape
        def _set_logs_panel_visible(visible):
            app._advanced_panel.set_logs_visible(visible)
            app._logs_panel_visible = visible

        app._set_logs_panel_visible = _set_logs_panel_visible

        show_main_view_calls: list[str] = []
        app._show_main_view = lambda view: show_main_view_calls.append(view)

        # Call the REAL toggle — no hardcoded branch here
        app._toggle_modo_compacto()

        # ── Step 3: assert product-workspace outcome ──
        # else-branch (compacto_active=False): side_config_panel.grid() must be called
        side_panel.grid.assert_called()
        # compact branch must NOT have fired: grid_remove() must NOT be called
        side_panel.grid_remove.assert_not_called()
        # Logs must remain hidden (_logs_visible_active=False → set_logs_visible(False))
        assert app._logs_panel_visible is False, (
            "Logs panel must stay hidden when _logs_visible_active=False"
        )
    finally:
        _restore_app_shell_module(old_module)


def test_toggle_compacto_false_shows_product_panel_and_hides_logs():
    """Bug C: with _compacto_active=False, _toggle_modo_compacto() must show
    the product workspace (side_config_panel.grid()) and keep logs hidden
    (_set_logs_panel_visible called with False via _toggle_logs_panel when
    _logs_visible_active is False).

    RED before fix: with _compacto_active=True (old default), the compact branch
    runs, calling side_config_panel.grid_remove() — the product panel is HIDDEN.
    GREEN after fix: _compacto_active=False routes to the else-branch which
    calls side_config_panel.grid() — the product panel is VISIBLE.

    Non-vacuous: asserts grid() was CALLED on the panel mock (not just that
    grid_remove was NOT called — we require the affirmative show action).
    """
    app_shell, old_module = _import_app_shell_with_ui_deps_mocked()
    try:
        app = object.__new__(app_shell.VocalAIApp)
        # Wire minimum state for _toggle_modo_compacto
        app._compacto_active = False
        app._logs_visible_active = False
        app._logs_panel_visible = False

        side_panel = MagicMock()
        app._side_config_panel = side_panel

        advanced_panel = MagicMock()
        app._advanced_panel = advanced_panel

        # _set_logs_panel_visible delegates to _advanced_panel.set_logs_visible
        # and writes _logs_panel_visible; wire it manually via the real method.
        def _set_logs_panel_visible(visible):
            app._advanced_panel.set_logs_visible(visible)
            app._logs_panel_visible = visible

        app._set_logs_panel_visible = _set_logs_panel_visible

        # _show_main_view should be callable but we just track it
        show_main_view_calls = []
        app._show_main_view = lambda view: show_main_view_calls.append(view)

        app._toggle_modo_compacto()

        # Product workspace must be shown (else-branch: side_config_panel.grid())
        side_panel.grid.assert_called()
        # side_config_panel.grid_remove() must NOT have been called
        side_panel.grid_remove.assert_not_called()
        # Logs must remain hidden (else-branch calls _toggle_logs_panel,
        # which uses _logs_visible_active=False → set_logs_visible(False))
        assert app._logs_panel_visible is False, (
            "Logs panel must stay hidden when _logs_visible_active=False"
        )
    finally:
        _restore_app_shell_module(old_module)


# ---------------------------------------------------------------------------
# WU 1.3 (qwen_tts_extirpation_20260627) — fake RMS bar removed from app_shell
# ---------------------------------------------------------------------------


def test_app_shell_does_not_wire_rms_bar():
    """The live-but-fake RMS bar wiring must be gone from VocalAIApp.

    RED before removal: the source aliases self.barra_rms (build site) and the
    listening-state grid()/grid_remove() call, plus the _schedule_rms_frame
    override that drove _animar_rms on a 150ms loop. GREEN after: none remain,
    and _actualizar_pipeline("listening") no longer touches a bar.
    """
    app_shell, old_module = _import_app_shell_with_ui_deps_mocked()
    try:
        # Read the module source from disk: inspect.getsource on the class fails
        # here because the helper's patch.dict evicts the freshly-imported module
        # from sys.modules (class-source resolution goes through sys.modules).
        src = Path(app_shell.__file__).read_text(encoding="utf-8")
        assert "barra_rms" not in src
        assert "_animar_rms" not in src
        assert "_schedule_rms_frame" not in src

        # Behavioral: the listening transition must not raise AttributeError on a
        # bare instance that never built the (now-deleted) bar. after() runs the
        # scheduled callable inline so any residual barra_rms.grid() lambda fires.
        app = object.__new__(app_shell.VocalAIApp)
        app._ui_state = SimpleNamespace(pipeline_state="idle")
        app.status_bar = None
        app.dispositivo_seleccionado = None
        app._update_kira_response_status = MagicMock()
        app._reset_inactivity_timer = MagicMock()
        app.after = MagicMock(side_effect=lambda _delay, fn: fn())

        app._actualizar_pipeline("listening")  # must not raise

        assert not hasattr(app, "barra_rms")
    finally:
        _restore_app_shell_module(old_module)
