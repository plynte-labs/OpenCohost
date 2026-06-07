"""Local-first crash reporting hooks for OpenCohost.

The crash reporter is intentionally defensive: if writing the crash log fails,
it still tries to emit evidence to stderr before the process exits.
"""
from __future__ import annotations

from datetime import datetime
import os
import sys
import threading
import traceback
from typing import TextIO


DEFAULT_CRASH_LOG = os.environ.get(
    "VOICEAI_CRASH_LOG",
    os.path.join("logs", "crash.log"),
)


def _default_timestamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _safe_stderr_write(stderr: TextIO, text: str) -> None:
    try:
        stderr.write(text)
        stderr.flush()
    except Exception:
        pass


def write_crash(
    tb_text: str,
    *,
    crash_log: str | None = None,
    stderr: TextIO | None = None,
    timestamp: str | None = None,
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


def install_crash_handler(*, crash_log: str | None = None) -> None:
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
