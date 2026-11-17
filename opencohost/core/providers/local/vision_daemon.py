"""Lifecycle of a dedicated Ollama daemon for the vision model.

The vision model runs in its own ``ollama serve`` on a separate port (11435 by
default) so it stays resident next to the voice model held by the main daemon
on 11434 instead of evicting it (vision-on-demand V0.2: both resident, 0
evictions). Both daemons share the same model store.

Rules:
- A healthy Ollama already answering on the port is reused and never stopped.
- Only the process this object spawned is ever killed (with its runner tree).
- Nothing is registered globally; the caller owns start/stop.
- Logs carry state transitions and reason codes only.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Optional, Protocol

_logger = logging.getLogger(__name__)

DEFAULT_VISION_HOST = "127.0.0.1"
DEFAULT_VISION_PORT = 11435

# Reason codes (stable strings; logged and surfaced through status()).
REASON_OLLAMA_NOT_FOUND = "ollama_not_found"
REASON_PORT_IN_USE = "port_in_use"
REASON_SPAWN_FAILED = "spawn_failed"
REASON_PROCESS_EXITED_EARLY = "process_exited_early"
REASON_STARTUP_TIMEOUT = "startup_timeout"
REASON_STOP_FAILED = "stop_failed"

_PROBE_OLLAMA = "ollama"
_PROBE_ABSENT = "absent"
_PROBE_FOREIGN = "foreign"


class _ProcessLike(Protocol):
    pid: int

    def poll(self) -> int | None: ...


class HttpTransport(Protocol):
    def get(self, url: str, timeout: float) -> tuple[int, str]: ...

    def post_json(self, url: str, payload: dict, timeout: float) -> tuple[int, str]: ...


class UrllibTransport:
    """Loopback HTTP via the standard library.

    Connection failures raise ``OSError``; HTTP error statuses are returned as
    ``(status, body)`` so the caller can tell "nothing listening" apart from
    "something else is listening".
    """

    def get(self, url: str, timeout: float) -> tuple[int, str]:
        return self._send(urllib.request.Request(url, method="GET"), timeout)

    def post_json(self, url: str, payload: dict, timeout: float) -> tuple[int, str]:
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        return self._send(request, timeout)

    @staticmethod
    def _send(request: urllib.request.Request, timeout: float) -> tuple[int, str]:
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.status, response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            try:
                body = exc.read().decode("utf-8", errors="replace")
            except Exception:
                body = ""
            return exc.code, body


def kill_process_tree(process: _ProcessLike) -> None:
    """Kill ``process`` and its children (the Ollama runner is a child).

    Windows: ``taskkill /PID <pid> /T /F``. Elsewhere: terminate, then kill.
    """
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,
            timeout=10,
            check=False,
        )
        if process.poll() is None:
            getattr(process, "kill", lambda: None)()
        return
    getattr(process, "terminate", lambda: None)()
    try:
        getattr(process, "wait", lambda timeout=None: None)(timeout=5)
    except subprocess.TimeoutExpired:
        getattr(process, "kill", lambda: None)()


@dataclass(frozen=True)
class VisionDaemonStatus:
    state: str  # stopped | starting | running | failed | external
    owned: bool
    pid: Optional[int]
    port: int
    last_error: Optional[str]


def _normalize_tag(model: str) -> str:
    model = model.strip()
    return model if ":" in model else f"{model}:latest"


class VisionDaemon:
    """Start, reuse, probe and stop the dedicated vision ``ollama serve``."""

    def __init__(
        self,
        *,
        ollama_exe: Optional[str],
        models_dir: str,
        host: str = DEFAULT_VISION_HOST,
        port: int = DEFAULT_VISION_PORT,
        startup_timeout_s: float = 30.0,
        poll_interval_s: float = 0.25,
        probe_timeout_s: float = 2.0,
        popen: Callable[..., _ProcessLike] = subprocess.Popen,
        http: Optional[HttpTransport] = None,
        kill_tree: Callable[[_ProcessLike], None] = kill_process_tree,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        base_env: Optional[Mapping[str, str]] = None,
        creationflags: Optional[int] = None,
        stdout_log_path: Optional[str] = None,
        stderr_log_path: Optional[str] = None,
    ) -> None:
        self._ollama_exe = ollama_exe
        self._models_dir = models_dir
        self._host = host
        self._port = port
        self._startup_timeout_s = startup_timeout_s
        self._poll_interval_s = poll_interval_s
        self._probe_timeout_s = probe_timeout_s
        self._popen = popen
        self._http = http if http is not None else UrllibTransport()
        self._kill_tree = kill_tree
        self._sleep = sleep
        self._monotonic = monotonic
        self._base_env = base_env
        self._creationflags = creationflags
        self._stdout_log_path = stdout_log_path
        self._stderr_log_path = stderr_log_path

        self._lock = threading.RLock()
        self._process: Optional[_ProcessLike] = None
        self._state = "stopped"
        self._last_error: Optional[str] = None

    # -- public API -------------------------------------------------------------

    @property
    def base_url(self) -> str:
        return f"http://{self._host}:{self._port}"

    def status(self) -> VisionDaemonStatus:
        with self._lock:
            process = self._process
            return VisionDaemonStatus(
                state=self._state,
                owned=process is not None,
                pid=getattr(process, "pid", None) if process is not None else None,
                port=self._port,
                last_error=self._last_error,
            )

    def ensure_running(self) -> VisionDaemonStatus:
        with self._lock:
            if self._process is not None:
                if self._process.poll() is None and self._probe() == _PROBE_OLLAMA:
                    return self.status()
                # Owned daemon died or stopped answering: release it, start over.
                self._release_owned()

            probe = self._probe()
            if probe == _PROBE_OLLAMA:
                self._transition("external", None)
                return self.status()
            if probe == _PROBE_FOREIGN:
                return self._fail(REASON_PORT_IN_USE)
            if not self._ollama_exe:
                return self._fail(REASON_OLLAMA_NOT_FOUND)

            self._transition("starting", None)
            try:
                self._process = self._spawn()
            except Exception:
                self._process = None
                return self._fail(REASON_SPAWN_FAILED)
            return self._wait_until_healthy()

    def stop(self) -> None:
        with self._lock:
            if self._state == "stopped" and self._process is None:
                return
            error = self._release_owned()
            self._transition("stopped", error)

    def model_installed(self, model: str) -> bool:
        if not model or not model.strip():
            return False
        wanted = _normalize_tag(model)
        try:
            status_code, body = self._http.get(f"{self.base_url}/api/tags", self._probe_timeout_s)
            if status_code != 200:
                return False
            models = json.loads(body).get("models")
        except Exception:
            return False
        if not isinstance(models, list):
            return False
        for entry in models:
            if not isinstance(entry, dict):
                continue
            for key in ("name", "model"):
                name = entry.get(key)
                if isinstance(name, str) and name and _normalize_tag(name) == wanted:
                    return True
        return False

    def warm(self, model: str, *, keep_alive: int | str = -1, timeout_s: float = 120.0) -> bool:
        """Load ``model`` into memory without generating (vision-on-demand V1.8).

        A ``/api/generate`` with no prompt is Ollama's documented load-only
        call: it returns as soon as the runner holds the model. ``timeout_s``
        is long because this is the cold load (~3-7 s measured) and it only
        ever runs on the controller's background thread.
        """
        try:
            status_code, _ = self._http.post_json(
                f"{self.base_url}/api/generate",
                {"model": model, "keep_alive": keep_alive},
                timeout_s,
            )
        except Exception:
            return False
        return status_code == 200

    def loaded_models(self) -> Optional[list[str]]:
        """Models resident on this daemon per ``/api/ps``; None when unreachable."""
        try:
            status_code, body = self._http.get(f"{self.base_url}/api/ps", self._probe_timeout_s)
            if status_code != 200:
                return None
            models = json.loads(body).get("models")
        except Exception:
            return None
        if not isinstance(models, list):
            return None
        names: list[str] = []
        for entry in models:
            if not isinstance(entry, dict):
                continue
            name = entry.get("name") or entry.get("model")
            if isinstance(name, str) and name.strip():
                names.append(_normalize_tag(name))
        return names

    def model_loaded(self, model: str) -> Optional[bool]:
        """True/False per ``/api/ps``; None when the daemon cannot be asked."""
        names = self.loaded_models()
        if names is None:
            return None
        return bool(model and model.strip()) and _normalize_tag(model) in names

    def unload(self, model: str) -> bool:
        try:
            status_code, _ = self._http.post_json(
                f"{self.base_url}/api/generate",
                {"model": model, "keep_alive": 0},
                self._probe_timeout_s,
            )
        except Exception:
            return False
        return status_code == 200

    # -- internals ----------------------------------------------------------------

    def _probe(self) -> str:
        """Classify whatever answers on the port: ollama, absent or foreign."""
        try:
            status_code, body = self._http.get(f"{self.base_url}/api/version", self._probe_timeout_s)
        except Exception:
            return _PROBE_ABSENT
        if status_code != 200:
            return _PROBE_FOREIGN
        try:
            payload = json.loads(body)
        except (TypeError, ValueError):
            return _PROBE_FOREIGN
        if isinstance(payload, dict) and isinstance(payload.get("version"), str):
            return _PROBE_OLLAMA
        return _PROBE_FOREIGN

    def _build_env(self) -> dict[str, str]:
        env = dict(self._base_env if self._base_env is not None else os.environ)
        # Hard overrides (not setdefault like the main daemon): the vision daemon
        # must bind its own port and hold exactly one model, whatever the parent
        # exported for the main daemon.
        env["OLLAMA_HOST"] = f"{self._host}:{self._port}"
        env["OLLAMA_MODELS"] = self._models_dir
        env["OLLAMA_MAX_LOADED_MODELS"] = "1"
        env["OLLAMA_NUM_PARALLEL"] = "1"
        return env

    def _spawn(self) -> _ProcessLike:
        creationflags = self._creationflags
        if creationflags is None:
            creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

        # Same log-file redirection as ollama_startup; DEVNULL instead of an
        # undrained PIPE when no log path is given (a full pipe stalls serve).
        stdout_target: object = subprocess.DEVNULL
        stderr_target: object = subprocess.DEVNULL
        handles = []
        try:
            if self._stdout_log_path:
                Path(self._stdout_log_path).parent.mkdir(parents=True, exist_ok=True)
                stdout_target = open(self._stdout_log_path, "a", encoding="utf-8")
                handles.append(stdout_target)
            if self._stderr_log_path:
                Path(self._stderr_log_path).parent.mkdir(parents=True, exist_ok=True)
                stderr_target = open(self._stderr_log_path, "a", encoding="utf-8")
                handles.append(stderr_target)
            return self._popen(
                [self._ollama_exe, "serve"],
                stdout=stdout_target,
                stderr=stderr_target,
                text=True,
                creationflags=creationflags,
                env=self._build_env(),
            )
        finally:
            for handle in handles:
                handle.close()

    def _wait_until_healthy(self) -> VisionDaemonStatus:
        process = self._process
        assert process is not None
        deadline = self._monotonic() + self._startup_timeout_s
        while True:
            if self._probe() == _PROBE_OLLAMA:
                self._transition("running", None)
                return self.status()
            if process.poll() is not None:
                self._process = None
                return self._fail(REASON_PROCESS_EXITED_EARLY)
            now = self._monotonic()
            if now >= deadline:
                self._release_owned()
                return self._fail(REASON_STARTUP_TIMEOUT)
            self._sleep(min(self._poll_interval_s, max(0.0, deadline - now)))

    def _release_owned(self) -> Optional[str]:
        """Kill the owned process tree, if any. Returns a reason code on error."""
        process, self._process = self._process, None
        if process is None:
            return None
        try:
            self._kill_tree(process)
        except Exception:
            return REASON_STOP_FAILED
        return None

    def _fail(self, reason: str) -> VisionDaemonStatus:
        self._transition("failed", reason)
        return self.status()

    def _transition(self, state: str, reason: Optional[str]) -> None:
        if state != self._state or reason != self._last_error:
            _logger.info(
                "[VISION_DAEMON] state=%s->%s port=%s reason=%s",
                self._state,
                state,
                self._port,
                reason or "none",
            )
        self._state = state
        self._last_error = reason
