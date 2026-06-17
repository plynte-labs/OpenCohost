"""Local-first crash reporting hooks for OpenCohost.

The crash reporter is intentionally defensive: if writing the crash log fails,
it still tries to emit evidence to stderr before the process exits.
"""
from __future__ import annotations

from datetime import datetime
import faulthandler
import os
import sys
import threading
import traceback
from typing import Iterable, TextIO


DEFAULT_CRASH_LOG = os.environ.get(
    "OPENCOHOST_CRASH_LOG",
    os.path.join("logs", "crash.log"),
)
DEFAULT_FATAL_LOG = os.environ.get(
    "OPENCOHOST_FATAL_LOG",
    os.path.join("logs", "fatal.log"),
)
_FATAL_LOG_HANDLE: TextIO | None = None
SAFE_RELATED_LOG_FILES = (
    "server_qwen_stdout.log",
    "server_qwen_stderr.log",
    "ollama_startup_stdout.log",
    "ollama_startup_stderr.log",
)


def _default_timestamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _safe_stderr_write(stderr: TextIO, text: str) -> None:
    try:
        stderr.write(text)
        stderr.flush()
    except Exception:
        pass


def build_safe_crash_context(
    related_log_files: Iterable[str] = SAFE_RELATED_LOG_FILES,
) -> str:
    """Return privacy-preserving crash context.

    The crash report points operators to useful child-process logs without
    copying their contents. This avoids leaking raw chat, prompts, tokens, or
    local file payloads into the crash artifact.
    """
    files = [str(name) for name in related_log_files if str(name).strip()]
    if not files:
        return ""
    lines = [
        "Related local logs to inspect (filenames only; contents not copied):"
    ]
    lines.extend(f"- {name}" for name in files)
    return "\n".join(lines) + "\n"


def write_crash(
    tb_text: str,
    *,
    crash_log: str | None = None,
    stderr: TextIO | None = None,
    timestamp: str | None = None,
    context_text: str | None = None,
) -> None:
    """Write crash evidence to the configured file and stderr.

    This function must not raise. A broken crash reporter is worse than a noisy
    one because it loses the only evidence operators have after a silent exit.
    """
    log_path = crash_log or DEFAULT_CRASH_LOG
    error_stream = stderr or sys.stderr
    now = timestamp or _default_timestamp()
    entry = (
        f"\n{'=' * 60}\n"
        f"CRASH at {now}\n"
        f"Thread: {threading.current_thread().name}\n"
        f"{context_text if context_text is not None else build_safe_crash_context()}"
        f"{tb_text}"
    )

    try:
        log_dir = os.path.dirname(log_path)
        if log_dir:
            os.makedirs(log_dir, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(entry)
    except Exception as write_error:
        _safe_stderr_write(
            error_stream,
            (
                f"\n[OpenCohost CRASH LOG WRITE FAILED] {now}\n"
                f"Path: {log_path}\n"
                f"Error: {write_error!r}\n"
            ),
        )

    _safe_stderr_write(error_stream, f"\n[OpenCohost CRASH] {now}\n{tb_text}\n")


def enable_fatal_log(
    *,
    fatal_log: str | None = None,
    stderr: TextIO | None = None,
    faulthandler_module=faulthandler,
) -> bool:
    """Enable best-effort fatal/native crash dumps.

    Python hooks do not catch native audio/Torch/Tcl faults reliably. The
    faulthandler log is a best-effort evidence trail for those process-level
    failures; it is not a recovery mechanism.
    """
    global _FATAL_LOG_HANDLE

    log_path = fatal_log or DEFAULT_FATAL_LOG
    error_stream = stderr or sys.stderr
    now = _default_timestamp()
    handle = None

    try:
        log_dir = os.path.dirname(log_path)
        if log_dir:
            os.makedirs(log_dir, exist_ok=True)
        handle = open(log_path, "a", encoding="utf-8")
        handle.write(f"\n{'=' * 60}\n")
        handle.write(f"FATAL LOG ENABLED at {now}\n")
        handle.flush()
        faulthandler_module.enable(file=handle, all_threads=True)
    except Exception as setup_error:
        _safe_stderr_write(
            error_stream,
            (
                f"\n[OpenCohost FATAL LOG SETUP FAILED] {now}\n"
                f"Path: {log_path}\n"
                f"Error: {setup_error!r}\n"
            ),
        )
        try:
            if handle is not None:
                handle.close()
        except Exception:
            pass
        return False

    old_handle = _FATAL_LOG_HANDLE
    _FATAL_LOG_HANDLE = handle
    if old_handle is not None and old_handle is not handle:
        try:
            old_handle.close()
        except Exception:
            pass
    return True


def _close_fatal_log_for_tests() -> None:
    """Close the retained fatal log handle in tests."""
    global _FATAL_LOG_HANDLE

    handle = _FATAL_LOG_HANDLE
    _FATAL_LOG_HANDLE = None
    if handle is not None:
        try:
            faulthandler.disable()
        except Exception:
            pass
        try:
            handle.close()
        except Exception:
            pass


def install_crash_handler(
    *,
    crash_log: str | None = None,
    fatal_log: str | None = None,
) -> None:
    """Install Python, thread, and Tk exception hooks."""

    def _handler(exc_type, exc_value, exc_tb):
        write_crash(
            "".join(traceback.format_exception(exc_type, exc_value, exc_tb)),
            crash_log=crash_log,
        )
        sys.__excepthook__(exc_type, exc_value, exc_tb)

    def _thread_handler(args):
        write_crash(
            "".join(
                traceback.format_exception(
                    args.exc_type,
                    args.exc_value,
                    args.exc_traceback,
                )
            ),
            crash_log=crash_log,
        )
        sys.__excepthook__(args.exc_type, args.exc_value, args.exc_traceback)

    sys.excepthook = _handler
    threading.excepthook = _thread_handler

    try:
        import tkinter as _tk

        def _tk_handler(self, exc_type, exc_value, exc_tb):
            _handler(exc_type, exc_value, exc_tb)

        _tk.Tk.report_callback_exception = _tk_handler
    except Exception:
        pass

    enable_fatal_log(fatal_log=fatal_log)
