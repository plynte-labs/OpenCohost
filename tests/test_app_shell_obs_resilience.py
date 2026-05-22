"""Focused tests for app shell OBS reconnect resilience."""

from __future__ import annotations

import sys
import importlib
import queue
import threading
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import ANY, MagicMock, patch


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
    old_module = sys.modules.pop("ui.app_shell", None)
    with patch.dict(sys.modules, modules):
        module = importlib.import_module("ui.app_shell")

    return module, old_module


def _restore_app_shell_module(old_module) -> None:
    if old_module is not None:
        sys.modules["ui.app_shell"] = old_module
        return
    sys.modules.pop("ui.app_shell", None)
    ui_module = sys.modules.get("ui")
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


class _TrackingLock:
    def __init__(self):
        self.locked = False
        self.enter_count = 0

    def __enter__(self):
        self.locked = True
        self.enter_count += 1

    def __exit__(self, exc_type, exc, tb):
        self.locked = False


class _GuardedSeenIds:
    def __init__(self, values, lock):
        self._values = set(values)
        self._lock = lock

    def _assert_locked(self):
        assert self._lock.locked, "_seen_chat_ids accessed without _chat_lock"

    def __contains__(self, value):
        self._assert_locked()
        return value in self._values

    def add(self, value):
        self._assert_locked()
        self._values.add(value)

    def __len__(self):
        self._assert_locked()
        return len(self._values)

    def __iter__(self):
        self._assert_locked()
        return iter(self._values)


def test_authenticated_chat_duplicate_gate_uses_stream_admin_lock():
    app_shell, old_module = _import_app_shell_with_ui_deps_mocked()
    try:
        lock = _TrackingLock()
        stream_admin_ui = SimpleNamespace(
            _chat_lock=lock,
            _seen_chat_ids=_GuardedSeenIds({"already-seen"}, lock),
        )

        assert app_shell._stream_admin_should_process_chat_message(stream_admin_ui, "already-seen") is False
        assert app_shell._stream_admin_should_process_chat_message(stream_admin_ui, "new-id") is True
        assert lock.enter_count == 2
    finally:
        _restore_app_shell_module(old_module)


def test_authenticated_chat_duplicate_gate_preserves_missing_id_and_truncation_behavior():
    app_shell, old_module = _import_app_shell_with_ui_deps_mocked()
    try:
        stream_admin_ui = SimpleNamespace(
            _chat_lock=threading.Lock(),
            _seen_chat_ids={str(i) for i in range(2000)},
        )

        assert app_shell._stream_admin_should_process_chat_message(stream_admin_ui, None) is True
        assert len(stream_admin_ui._seen_chat_ids) == 2000

        assert app_shell._stream_admin_should_process_chat_message(stream_admin_ui, "new-id") is True
        assert len(stream_admin_ui._seen_chat_ids) == 1000
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
            "MotorVocalIA thread died unexpectedly; UI remains open but Kira is offline"
        )
        assert app._ui_state.health_status == "red"
        app._print_log.assert_called_once_with(
            "[CRITICO] MotorVocalIA se detuvo inesperadamente. Kira esta offline; reinicia la app."
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
    app_shell, old_module = _import_app_shell_with_ui_deps_mocked()
    try:
        app = object.__new__(app_shell.VocalAIApp)
        app._print_log = MagicMock()

        with patch.object(app_shell.logger, "warning") as mock_warning:
            app._notify_operator("Stream Admin", "RF4 no inicializado. Revisa config/stream_admin.yaml.")

        mock_warning.assert_called_once_with(
            "%s: %s",
            "Stream Admin",
            "RF4 no inicializado. Revisa config/stream_admin.yaml.",
        )
        app._print_log.assert_called_once_with(
            "[WARNING] Stream Admin: RF4 no inicializado. Revisa config/stream_admin.yaml."
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


class _ImmediateThread:
    def __init__(self, target, daemon=False):
        self._target = target
        self.daemon = daemon

    def start(self):
        self._target()


def test_stream_admin_chat_url_validation_uses_non_blocking_notification():
    app_shell, old_module = _import_app_shell_with_ui_deps_mocked()
    try:
        app = object.__new__(app_shell.VocalAIApp)
        app.stream_admin_ui = SimpleNamespace(
            _widget=MagicMock(return_value=_EntryStub("not a supported chat url"))
        )
        app._print_log = MagicMock()

        with patch.object(app_shell.messagebox, "showwarning") as mock_showwarning:
            with patch("smart_aggregator.url_parser.parse_chat_url", side_effect=ValueError("bad url")):
                app._on_stream_admin_connect_chat_live()

        mock_showwarning.assert_not_called()
        app._print_log.assert_called_once_with("[WARNING] Chat Live: URL no valida o no soportada")
    finally:
        _restore_app_shell_module(old_module)


def test_stream_admin_send_chat_readonly_uses_non_blocking_notification():
    app_shell, old_module = _import_app_shell_with_ui_deps_mocked()
    try:
        app = object.__new__(app_shell.VocalAIApp)
        app._stream_admin_can_write = MagicMock(return_value=False)
        app._notify_operator = MagicMock()

        with patch.object(app_shell.messagebox, "showwarning") as mock_showwarning:
            app._stream_admin_send_chat()

        app._notify_operator.assert_called_once_with(
            "Stream Admin",
            "Modo solo lectura activo. Reconecta escritura antes de enviar mensajes al chat.",
        )
        mock_showwarning.assert_not_called()
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


def test_stream_admin_worker_error_uses_non_blocking_notification():
    app_shell, old_module = _import_app_shell_with_ui_deps_mocked()
    try:
        app = object.__new__(app_shell.VocalAIApp)
        app.stream_admin = object()
        app.after = MagicMock(side_effect=lambda _delay, callback: callback())
        app._notify_operator = MagicMock()
        app._on_stream_admin_log = MagicMock()

        def fail():
            raise RuntimeError("boom")

        with patch.object(app_shell.threading, "Thread", _ImmediateThread):
            with patch.object(app_shell.messagebox, "showerror") as mock_showerror:
                app._run_stream_admin_task("Acción", fail)

        app.after.assert_called_once_with(0, ANY)
        app._notify_operator.assert_called_once_with("Stream Admin", "boom", level="error")
        app._on_stream_admin_log.assert_called_once_with("[StreamAdmin] Acción falló: boom")
        mock_showerror.assert_not_called()
    finally:
        _restore_app_shell_module(old_module)


def test_music_import_errors_use_non_blocking_notification():
    app_shell, old_module = _import_app_shell_with_ui_deps_mocked()
    try:
        app = object.__new__(app_shell.VocalAIApp)
        app.music_library = SimpleNamespace(add_file=MagicMock(side_effect=ValueError("formato inválido")))
        app._music_update_panel = MagicMock()
        app._notify_operator = MagicMock()

        with patch.object(app_shell.filedialog, "askopenfilenames", return_value=["C:/tmp/bad.mp3"]):
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

        with patch.object(app_shell.filedialog, "askopenfilename", return_value="C:/tmp/bad.wav"):
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

        with patch("avatar.avatar_config.load_avatar_config", return_value=config):
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

        with patch("avatar.avatar_config.load_avatar_config", return_value=config):
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

        with patch("avatar.avatar_config.load_avatar_config", return_value=config):
            with patch.object(app_shell.os.path, "isfile", return_value=True):
                with patch.dict(sys.modules, {"PIL": SimpleNamespace(Image=image_module), "PIL.Image": image_module}):
                    app._on_avatar_state_for_preview(SimpleNamespace(value="speaking"))

        assert app._kira_avatar_ref is idle_ref
        app._kira_avatar_label.configure.assert_called_with(image=idle_ref, text="")
    finally:
        _restore_app_shell_module(old_module)
