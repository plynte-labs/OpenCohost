"""Supervisor for one headless ``liveaudio-service`` child process.

Contract (implemented by LiveAudio unit 1, read-only from here):

- CLI ``liveaudio-service --parent-pid <PID> [--health-file PATH]``.
- stdout carries versioned JSON Lines: ``service_state``, ``ws_port``,
  ``asr_state``, ``fatal`` (sanitized: codes/ports/states/counters only).
- The WS server binds immediately on ``base..base+9``; audio/ASR start lazy
  on the first WS client (a probe connection counts as first client).
- The service exits by itself when the parent PID dies; a lock rejects a
  second instance; the config snapshot is read-only.
- Every WS client first receives
  ``hello {app: 'liveaudio', proto: 1, port: <effective>}``.

This module NEVER touches the LiveAudio sources: it only spawns the
installed ``liveaudio-service`` entry point and speaks the stdout/WS
contract above. No shell, no remote control, no transcript/audio logging.
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import shlex
import shutil
import subprocess
import sys
import threading
import time
from typing import Callable, Optional

logger = logging.getLogger("opencohost.stt.supervisor")

SERVICE_CMD = "liveaudio-service"
EXPLICIT_ENV_VAR = "OPENCOHOST_LIVEAUDIO_CMD"
# Official LiveAudio install-root override (packaging/launcher.py
# resolve_install_root precedence 1; testing/automation hook there too).
INSTALL_ROOT_ENV_VAR = "LIVEAUDIO_INSTALL_ROOT"
INSTALL_LOCATION_FILE = "install_location.json"
INSTALLED_MARKER_FILE = "installed.json"

# ASR readiness as reported by the ``asr_state`` stdout event.
ASR_READY = "ready"
ASR_LOADING_STATES = frozenset({"starting", "loading"})

# Bounded waits (seconds). The WS port arrives immediately after spawn — the
# service binds WS before any model load — so the port bound is a liveness
# bound, not a model-load bound. HTTP worst case stays inside a documented
# budget (see routers/ptt.py): port wait + one bounded readiness wait, never
# chained waits.
DEFAULT_PORT_TIMEOUT = 8.0
DEFAULT_READY_TIMEOUT = 5.0

# Keys allowed out of a stdout event into logs/status. Everything else
# (notably anything that could carry transcript/audio/path material) is
# dropped even if a future service version adds it.
_EVENT_FIELDS = frozenset(
    {
        "schema",
        "version",
        "service_pid",
        "parent_pid",
        "type",
        "state",
        "asr_state",
        "code",
        "base_port",
        "effective_port",
        "port",
        "base",
        "children",
        "error_code",
    }
)


def _service_exe_for_root(install_root: str, platform: str) -> Optional[str]:
    """Sibling ``liveaudio-service`` binary inside an install root.

    Layout mirrors ``venv_app_exe`` in the official launcher: the service is
    a ``[project.scripts]`` entry point, so it sits next to the ``liveaudio``
    gui-script inside ``<root>/app/.venv`` — ``Scripts/liveaudio-service.exe``
    on Windows, ``bin/liveaudio-service`` on POSIX. Returns the path only
    when it is a real file; fixed depth, no recursion, no execution.
    """
    if not isinstance(install_root, str) or not install_root:
        return None
    if platform == "win32":
        candidate = os.path.join(install_root, "app", ".venv", "Scripts", "liveaudio-service.exe")
    else:
        candidate = os.path.join(install_root, "app", ".venv", "bin", "liveaudio-service")
    try:
        return candidate if os.path.isfile(candidate) else None
    except Exception:
        return None


def _metadata_location_file(platform: str, environ) -> str:
    """Path of the official ``install_location.json`` (launcher contract).

    ``%APPDATA%/LiveAudio`` on Windows, ``$XDG_CONFIG_HOME/liveaudio`` (else
    ``~/.config/liveaudio``) on POSIX. Read-only from here; never written.
    """
    if platform == "win32":
        base = environ.get("APPDATA") or os.path.expanduser("~")
        return os.path.join(base, "LiveAudio", INSTALL_LOCATION_FILE)
    base = environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "liveaudio", INSTALL_LOCATION_FILE)


def _read_metadata_root(path: str) -> Optional[str]:
    """``install_root`` from an ``install_location.json`` file, or None.

    Corrupt/missing/non-dict/empty values all resolve to None — a broken
    metadata file degrades to the next resolution step, never to a crash.
    """
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    root = data.get("install_root")
    if not isinstance(root, str) or not root.strip():
        return None
    return os.path.abspath(root.strip())


def _default_install_root(platform: str, environ) -> str:
    """Launcher default root: ``%LOCALAPPDATA%/LiveAudio`` (Windows) or
    ``~/.local/share/liveaudio`` (POSIX). No user hardcoding — every segment
    comes from the process environment or the OS home directory."""
    if platform == "win32":
        base = environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return os.path.join(base, "LiveAudio")
    return os.path.join(os.path.expanduser("~"), ".local", "share", "liveaudio")


def resolve_installed_service_exe(environ=None, platform=None) -> Optional[str]:
    """Locate the installed ``liveaudio-service`` binary via official metadata.

    Order (mirrors the launcher's own precedence, read-only):
    1. ``LIVEAUDIO_INSTALL_ROOT`` env override → sibling exe when present.
    2. ``install_location.json`` → recorded ``install_root`` → sibling exe.
    3. Launcher default root — accepted ONLY when ``installed.json`` marks a
       completed install AND the sibling exe is a real file.

    No shell, no recursive search, no user-path hardcoding. Returns None when
    nothing official resolves, so callers keep manual-mode compatibility.
    """
    platform = platform or sys.platform
    environ = os.environ if environ is None else environ

    override = environ.get(INSTALL_ROOT_ENV_VAR)
    if isinstance(override, str) and override.strip():
        found = _service_exe_for_root(os.path.abspath(override.strip()), platform)
        if found:
            return found

    recorded = _read_metadata_root(_metadata_location_file(platform, environ))
    if recorded:
        found = _service_exe_for_root(recorded, platform)
        if found:
            return found

    default_root = _default_install_root(platform, environ)
    try:
        marked = os.path.isfile(os.path.join(default_root, INSTALLED_MARKER_FILE))
    except Exception:
        marked = False
    if marked:
        found = _service_exe_for_root(default_root, platform)
        if found:
            return found
    return None


def resolve_liveaudio_command(explicit: Optional[str] = None, environ=None, platform=None) -> Optional[list]:
    """Resolve the ``liveaudio-service`` argv (without ``--parent-pid``).

    Precedence: explicit setting (test/dev only) → ``OPENCOHOST_LIVEAUDIO_CMD``
    env (test/dev only) → ``liveaudio-service`` on PATH → official installer
    metadata (``LIVEAUDIO_INSTALL_ROOT`` / ``install_location.json`` /
    default root with ``installed.json`` marker). Returns None when not
    installed so callers keep manual-LiveAudio compatibility with an actionable
    message instead of crashing. Never uses a shell.
    """
    raw = explicit if explicit is not None else (environ or os.environ).get(EXPLICIT_ENV_VAR, "")
    raw = (raw or "").strip()
    if raw:
        try:
            argv = shlex.split(raw, posix=((platform or sys.platform) != "win32"))
        except ValueError:
            return None
        return argv or None
    found = shutil.which(SERVICE_CMD)
    if found:
        return [found]
    exe = resolve_installed_service_exe(environ=environ, platform=platform)
    return [exe] if exe else None


def parse_service_line(line: str) -> Optional[dict]:
    """Parse one stdout JSON Line into an allowlisted event dict.

    Returns None for blank/non-JSON/non-dict lines or dicts without a ``type``.
    Only allowlisted scalar fields survive — raw lines are never logged.
    """
    if not line or not line.strip():
        return None
    try:
        data = json.loads(line)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict) or not data.get("type"):
        return None
    event = {k: v for k, v in data.items() if k in _EVENT_FIELDS}
    event["type"] = data["type"]
    return event


def _event_port(event: dict) -> Optional[int]:
    """Effective port from a ``ws_port`` event.

    Accepts the supervisor stdout shape (``effective_port``/``base_port``)
    and the WS-child direct shape (``port``/``base``) for forward/backward
    compatibility. Returns None when absent or non-numeric.
    """
    for key in ("effective_port", "port"):
        if key in event:
            try:
                port = int(event[key])
            except (TypeError, ValueError):
                return None
            if 1 <= port <= 65535:
                return port
            return None
    return None


def _event_base(event: dict) -> Optional[int]:
    for key in ("base_port", "base"):
        if key in event:
            try:
                return int(event[key])
            except (TypeError, ValueError):
                return None
    return None


class LiveAudioSupervisor:
    """Owns at most one ``liveaudio-service`` child.

    Single-flight: :meth:`ensure_started` is atomic — N concurrent callers
    produce exactly one spawn attempt. :meth:`shutdown` is idempotent and
    safe to call from lifespan teardown AND atexit (the service also exits
    on its own via the parent-PID watchdog, so a missed shutdown only
    costs a bounded orphan window, never a leak).
    """

    def __init__(
        self,
        *,
        command: Optional[list] = None,
        parent_pid: Optional[int] = None,
        port_timeout: float = DEFAULT_PORT_TIMEOUT,
        ready_timeout: float = DEFAULT_READY_TIMEOUT,
        popen_factory: Callable = subprocess.Popen,
    ) -> None:
        self._argv = list(command) if command is not None else resolve_liveaudio_command()
        self._parent_pid = int(parent_pid) if parent_pid else os.getpid()
        self._port_timeout = port_timeout
        self._ready_timeout = ready_timeout
        self._popen_factory = popen_factory

        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        # Serializes full ensure cycles (spawn + wait + probe + repoint)
        # across concurrent requests; distinct from _lock so network I/O
        # never blocks status()/shutdown() readers.
        self.ensure_lock = threading.Lock()
        self._proc = None
        self._pump_thread: Optional[threading.Thread] = None
        self._effective_port: Optional[int] = None
        self._base_port: Optional[int] = None
        self._service_state = "unknown"
        self._asr_state = "unknown"
        self._fatal_code: Optional[str] = None
        self._last_warning: Optional[str] = None
        self._returncode: Optional[int] = None
        self._exited = False
        self._shutdown = False
        self._atexit_registered = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def ensure_started(self) -> bool:
        """Spawn the service if not running. Returns True when a child is
        alive afterwards. False when no binary is installed (manual-mode
        fallback) or the spawn itself failed. Single-flight under _lock."""
        with self._lock:
            if self._shutdown:
                return False
            if self._proc is not None and self._proc.poll() is None:
                return True
            self._proc = None
            self._exited = False
            if not self._argv:
                return False
            argv = list(self._argv) + ["--parent-pid", str(self._parent_pid)]
            try:
                self._proc = self._popen_factory(
                    argv,
                    shell=False,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    bufsize=1,
                )
            except Exception:
                logger.warning("liveaudio spawn failed")
                self._proc = None
                return False
            self._pump_thread = threading.Thread(
                target=self._pump_stdout, name="liveaudio-stdout", daemon=True
            )
            self._pump_thread.start()
            if not self._atexit_registered:
                self._atexit_registered = True
                try:
                    atexit.register(self.shutdown)
                except Exception:
                    pass
            logger.info("liveaudio service spawned (parent_pid=%d)", self._parent_pid)
            return True

    def wait_for_port(self, timeout: Optional[float] = None) -> Optional[int]:
        """Block (bounded) until the ``ws_port`` event arrives. Returns the
        effective port, or None on timeout/fatal/exit/shutdown."""
        deadline = time.monotonic() + (self._port_timeout if timeout is None else timeout)
        with self._cond:
            while self._effective_port is None:
                if self._fatal_code is not None or self._exited or self._shutdown:
                    return None
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._cond.wait(timeout=remaining)
            return self._effective_port

    def wait_for_ready(self, timeout: Optional[float] = None) -> bool:
        """Block (bounded) until ``asr_state`` is ``ready``."""
        deadline = time.monotonic() + (self._ready_timeout if timeout is None else timeout)
        with self._cond:
            while self._asr_state != ASR_READY:
                if self._fatal_code is not None or self._exited or self._shutdown:
                    return False
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return self._asr_state == ASR_READY
                self._cond.wait(timeout=remaining)
            return True

    def shutdown(self) -> None:
        """Idempotent teardown: terminate, bounded join, kill fallback."""
        with self._lock:
            if self._shutdown:
                return
            self._shutdown = True
            proc, pump = self._proc, self._pump_thread
            self._proc = None
            self._cond.notify_all()
        if proc is not None:
            try:
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=3.0)
                    except Exception:
                        try:
                            proc.kill()
                        except Exception:
                            pass
            except Exception:
                pass
            try:
                stdout = getattr(proc, "stdout", None)
                if stdout is not None:
                    stdout.close()
            except Exception:
                pass
        if pump is not None and pump is not threading.current_thread():
            try:
                pump.join(timeout=2.0)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Read surface (no I/O, safe from any thread)
    # ------------------------------------------------------------------

    def proc_running(self) -> bool:
        with self._lock:
            return self._proc is not None and self._proc.poll() is None

    def effective_port(self) -> Optional[int]:
        with self._lock:
            return self._effective_port

    def asr_state(self) -> str:
        with self._lock:
            return self._asr_state

    def is_loading(self) -> bool:
        with self._lock:
            return self._proc is not None and self._asr_state in ASR_LOADING_STATES

    def status(self) -> dict:
        """Small sanitized snapshot for GET /api/ptt/state (additive).

        ``last_warning`` is the latest warning CODE from stdout (fixed
        literals like ``child-restart-scheduled``); ``returncode`` is the
        child exit code once observed. Neither ever carries stderr text,
        transcripts, or paths.
        """
        with self._lock:
            returncode = self._returncode
            if returncode is None and self._proc is not None:
                try:
                    polled = self._proc.poll()
                    returncode = polled if isinstance(polled, int) else None
                except Exception:
                    returncode = None
            return {
                "spawned": self._proc is not None,
                "running": self._proc is not None and self._proc.poll() is None,
                "port": self._effective_port,
                "base_port": self._base_port,
                "service_state": self._service_state,
                "asr_state": self._asr_state,
                "fatal": self._fatal_code,
                "last_warning": self._last_warning,
                "returncode": returncode,
            }

    # ------------------------------------------------------------------
    # Stdout pump (daemon thread; never logs raw lines)
    # ------------------------------------------------------------------

    def _pump_stdout(self) -> None:
        proc = self._proc
        try:
            stdout = getattr(proc, "stdout", None)
            if stdout is None:
                return
            for line in iter(stdout.readline, ""):
                if not line:
                    break
                event = parse_service_line(line)
                if event is None:
                    continue
                self._handle_event(event)
                with self._lock:
                    if self._shutdown:
                        break
        except Exception:
            pass
        finally:
            with self._lock:
                if self._proc is proc:
                    self._exited = True
                    try:
                        polled = proc.poll() if proc is not None else None
                        self._returncode = polled if isinstance(polled, int) else self._returncode
                    except Exception:
                        pass
                self._cond.notify_all()

    def _handle_event(self, event: dict) -> None:
        etype = event.get("type")
        with self._lock:
            if etype == "ws_port":
                port = _event_port(event)
                if port is not None:
                    self._effective_port = port
                    base = _event_base(event)
                    if base is not None:
                        self._base_port = base
                    logger.info("liveaudio ws_port effective=%d", port)
                    self._cond.notify_all()
            elif etype == "asr_state":
                state = event.get("asr_state")
                if isinstance(state, str) and state:
                    self._asr_state = state
                    self._cond.notify_all()
            elif etype == "service_state":
                state = event.get("state")
                if isinstance(state, str) and state:
                    self._service_state = state
                    self._cond.notify_all()
            elif etype == "fatal":
                code = event.get("code")
                self._fatal_code = code if isinstance(code, str) else "unknown"
                logger.warning("liveaudio fatal code=%s", self._fatal_code)
                self._cond.notify_all()
            elif etype == "warning":
                code = event.get("code")
                if isinstance(code, str) and code:
                    self._last_warning = code
