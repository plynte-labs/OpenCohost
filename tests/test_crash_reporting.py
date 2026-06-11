import io
from types import SimpleNamespace
import sys
import threading

from opencohost.ui import crash_reporting


class FakeFaultHandler:
    def __init__(self):
        self.enabled_file = None
        self.all_threads = None

    def enable(self, *, file, all_threads):
        self.enabled_file = file
        self.all_threads = all_threads


class FailingFaultHandler:
    def enable(self, *, file, all_threads):
        raise RuntimeError("faulthandler unavailable")


def test_write_crash_creates_log_and_writes_stderr(tmp_path):
    crash_log = tmp_path / "logs" / "crash.log"
    stderr = io.StringIO()

    crash_reporting.write_crash(
        "Traceback text\nRuntimeError: boom\n",
        crash_log=str(crash_log),
        stderr=stderr,
        timestamp="2026-06-06 20:00:00",
        context_text="",
    )

    log_text = crash_log.read_text(encoding="utf-8")
    assert "CRASH at 2026-06-06 20:00:00" in log_text
    assert "Thread: MainThread" in log_text
    assert "RuntimeError: boom" in log_text
    assert "[OpenCohost CRASH]" in stderr.getvalue()
    assert "RuntimeError: boom" in stderr.getvalue()


def test_write_crash_falls_back_to_stderr_when_log_write_fails(tmp_path):
    stderr = io.StringIO()

    crash_reporting.write_crash(
        "RuntimeError: boom\n",
        crash_log=str(tmp_path),
        stderr=stderr,
        timestamp="2026-06-06 20:00:00",
        context_text="",
    )

    stderr_text = stderr.getvalue()
    assert "[OpenCohost CRASH LOG WRITE FAILED]" in stderr_text
    assert "RuntimeError: boom" in stderr_text


def test_installed_sys_hook_writes_crash_log(tmp_path):
    crash_log = tmp_path / "crash.log"
    original_excepthook = sys.excepthook
    original_threading_excepthook = threading.excepthook
    original_sys_dunder_excepthook = sys.__excepthook__
    sys.__excepthook__ = lambda exc_type, exc_value, exc_tb: None

    try:
        crash_reporting.install_crash_handler(
            crash_log=str(crash_log),
            fatal_log=str(tmp_path / "fatal.log"),
        )
        try:
            raise RuntimeError("hook boom")
        except RuntimeError:
            exc_type, exc_value, exc_tb = sys.exc_info()
            sys.excepthook(exc_type, exc_value, exc_tb)
    finally:
        sys.excepthook = original_excepthook
        threading.excepthook = original_threading_excepthook
        sys.__excepthook__ = original_sys_dunder_excepthook
        crash_reporting._close_fatal_log_for_tests()

    assert "RuntimeError: hook boom" in crash_log.read_text(encoding="utf-8")


def test_installed_thread_hook_writes_crash_log(tmp_path):
    crash_log = tmp_path / "thread-crash.log"
    original_excepthook = sys.excepthook
    original_threading_excepthook = threading.excepthook
    original_sys_dunder_excepthook = sys.__excepthook__
    sys.__excepthook__ = lambda exc_type, exc_value, exc_tb: None

    try:
        crash_reporting.install_crash_handler(
            crash_log=str(crash_log),
            fatal_log=str(tmp_path / "fatal.log"),
        )
        try:
            raise RuntimeError("thread hook boom")
        except RuntimeError:
            exc_type, exc_value, exc_tb = sys.exc_info()
            threading.excepthook(
                SimpleNamespace(
                    exc_type=exc_type,
                    exc_value=exc_value,
                    exc_traceback=exc_tb,
                    thread=threading.current_thread(),
                )
            )
    finally:
        sys.excepthook = original_excepthook
        threading.excepthook = original_threading_excepthook
        sys.__excepthook__ = original_sys_dunder_excepthook
        crash_reporting._close_fatal_log_for_tests()

    assert "RuntimeError: thread hook boom" in crash_log.read_text(encoding="utf-8")


def test_installed_tk_hook_uses_bound_method_signature(tmp_path):
    crash_log = tmp_path / "tk-crash.log"
    original_excepthook = sys.excepthook
    original_threading_excepthook = threading.excepthook
    original_sys_dunder_excepthook = sys.__excepthook__
    sys.__excepthook__ = lambda exc_type, exc_value, exc_tb: None

    import tkinter as tk

    original_tk_hook = tk.Tk.report_callback_exception

    try:
        crash_reporting.install_crash_handler(
            crash_log=str(crash_log),
            fatal_log=str(tmp_path / "fatal.log"),
        )
        try:
            raise RuntimeError("tk hook boom")
        except RuntimeError:
            exc_type, exc_value, exc_tb = sys.exc_info()
            tk.Tk.report_callback_exception(
                object(),
                exc_type,
                exc_value,
                exc_tb,
            )
    finally:
        sys.excepthook = original_excepthook
        threading.excepthook = original_threading_excepthook
        sys.__excepthook__ = original_sys_dunder_excepthook
        tk.Tk.report_callback_exception = original_tk_hook
        crash_reporting._close_fatal_log_for_tests()

    assert "RuntimeError: tk hook boom" in crash_log.read_text(encoding="utf-8")


def test_build_safe_crash_context_points_to_logs_without_contents():
    context = crash_reporting.build_safe_crash_context(
        ["server_qwen_stderr.log", "ollama_startup_stderr.log"]
    )

    assert "server_qwen_stderr.log" in context
    assert "ollama_startup_stderr.log" in context
    assert "contents not copied" in context
    assert "chat" not in context.lower()
    assert "prompt" not in context.lower()


def test_enable_fatal_log_creates_log_and_enables_all_threads(tmp_path):
    fatal_log = tmp_path / "fatal.log"
    fake_faulthandler = FakeFaultHandler()

    try:
        enabled = crash_reporting.enable_fatal_log(
            fatal_log=str(fatal_log),
            faulthandler_module=fake_faulthandler,
        )

        assert enabled is True
        assert fake_faulthandler.enabled_file is not None
        assert fake_faulthandler.all_threads is True
        assert "FATAL LOG ENABLED" in fatal_log.read_text(encoding="utf-8")
    finally:
        crash_reporting._close_fatal_log_for_tests()


def test_enable_fatal_log_falls_back_to_stderr_when_setup_fails(tmp_path):
    fatal_log = tmp_path / "fatal.log"
    stderr = io.StringIO()

    enabled = crash_reporting.enable_fatal_log(
        fatal_log=str(fatal_log),
        stderr=stderr,
        faulthandler_module=FailingFaultHandler(),
    )

    stderr_text = stderr.getvalue()
    assert enabled is False
    assert "[OpenCohost FATAL LOG SETUP FAILED]" in stderr_text
    assert "faulthandler unavailable" in stderr_text
