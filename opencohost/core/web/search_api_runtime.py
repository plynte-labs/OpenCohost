"""Runtime manager for dedicated search API worker process (Phase 3, A5/A6).

Owns:
    - Single reusable child process running opencohost.core.web.search_api_worker.
    - Single active slot (concurrent requests return api_reason='busy').
    - Request and process generation tracking.
    - Two-phase handshake:
        prepare(provider, query, key, deadline) -> (bool, reason)
        dispatch(provider, query, key, deadline) -> (list[SearchResult], reason)
    - Monotonic absolute deadline enforcement (slow child killed and reaped).
    - Crash resilience and bounded frames (<=8 KiB request, <=64 KiB response).
    - get_runtime() / close_runtime() lifecycle.
"""
from __future__ import annotations

import json
import logging
import os
import queue
import subprocess
import sys
import threading
import time
import uuid
from typing import Callable, Optional

from opencohost.core.web.search import SearchResult
from opencohost.core.web.search_usage import ReservationResult
from opencohost.core.web.search_api import (
    REASON_AUTH,
    REASON_BLOCKED,
    REASON_DNS,
    REASON_OTHER,
    REASON_PARSE,
    REASON_TIMEOUT,
)
from opencohost.core.web.search_api_worker import (
    MAX_REQUEST_FRAME_BYTES,
    MAX_RESPONSE_FRAME_BYTES,
    FrameError,
    FrameOverflowError,
)

logger = logging.getLogger("OpenCohost")

# Runtime-specific closed reasons
REASON_BUSY = "busy"
REASON_WORKER_UNAVAILABLE = "worker_unavailable"


class SearchApiRuntime:
    """Manages single reusable search API worker child process and IPC handshake."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        env: Optional[dict[str, str]] = None,
        python_exe: Optional[str] = None,
    ) -> None:
        self._clock = clock
        self._custom_env = env
        self._python_exe = python_exe or sys.executable
        self._lock = threading.RLock()

        self._child: Optional[subprocess.Popen] = None
        self._child_pid: Optional[int] = None
        self._generation: int = 1
        self._slot_owner: Optional[str] = None
        # A killed child whose exit was not confirmed; no new child until it is.
        self._unreaped: Optional[subprocess.Popen] = None
        self._queue: Optional[queue.Queue] = None
        self._reader_thread: Optional[threading.Thread] = None

    @property
    def generation(self) -> int:
        """Current child process generation ID."""
        with self._lock:
            return self._generation

    @property
    def is_running(self) -> bool:
        """Check if child process is currently alive."""
        with self._lock:
            return self._child is not None and self._child.poll() is None

    @property
    def child_pid(self) -> Optional[int]:
        """Child process PID, if running."""
        with self._lock:
            return self._child_pid

    def _get_project_root(self) -> str:
        return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))

    def _terminate_and_reap(self, deadline: Optional[float] = None) -> None:
        """Kill the child, wait for exit (bounded) and advance the generation.

        The wait never exceeds ``deadline`` (small floor so a just-expired
        deadline still gets a chance to observe the exit) nor 1 s. When exit
        is not confirmed the process is remembered in ``_unreaped`` and
        ``_ensure_child`` refuses to launch a replacement.
        """
        proc = self._child
        self._child = None
        self._child_pid = None
        self._generation += 1
        self._slot_owner = None
        self._queue = None
        self._reader_thread = None

        if proc is None:
            return
        wait_s = 1.0
        if deadline is not None:
            wait_s = min(1.0, max(0.05, deadline - self._clock()))
        try:
            proc.kill()
        except OSError:
            pass
        try:
            proc.wait(timeout=wait_s)
        except (subprocess.TimeoutExpired, OSError):
            pass
        if proc.poll() is None:
            self._unreaped = proc

    def _ensure_child(self) -> subprocess.Popen:
        """Ensure child process is running, launching a new one if needed."""
        if getattr(sys, "frozen", False):
            raise RuntimeError("Frozen distributions require a supported worker launch path")

        if self._unreaped is not None:
            if self._unreaped.poll() is None:
                # A SEND kill reaps for only a few ms when its deadline has
                # passed; give that exit a bounded moment before refusing, so a
                # retry right after the kill is not failed as worker_unavailable.
                try:
                    self._unreaped.wait(timeout=1.0)
                except (subprocess.TimeoutExpired, OSError):
                    pass
            if self._unreaped.poll() is None:
                raise RuntimeError("Previous worker exit not confirmed")
            self._unreaped = None

        if self._child is not None and self._child.poll() is None:
            return self._child

        # If previous child crashed or exited
        if self._child is not None:
            self._terminate_and_reap()
            if self._unreaped is not None:
                raise RuntimeError("Previous worker exit not confirmed")

        project_root = self._get_project_root()
        creationflags = 0
        if sys.platform == "win32":
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)

        cmd = [self._python_exe, "-u", "-m", "opencohost.core.web.search_api_worker"]

        child_env = dict(self._custom_env if self._custom_env is not None else os.environ)
        # Never inherit raw API keys in environment
        child_env.pop("OPENCOHOST_TAVILY_API_KEY", None)
        child_env.pop("OPENCOHOST_BRAVE_API_KEY", None)

        existing_pythonpath = child_env.get("PYTHONPATH", "")
        child_env["PYTHONPATH"] = (
            f"{project_root}{os.pathsep}{existing_pythonpath}"
            if existing_pythonpath
            else project_root
        )

        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            cwd=project_root,
            shell=False,
            creationflags=creationflags,
            env=child_env,
        )

        self._child = proc
        self._child_pid = proc.pid
        q: queue.Queue = queue.Queue(maxsize=16)
        self._queue = q

        def _reader_loop() -> None:
            stdout = proc.stdout
            if not stdout:
                q.put((None, EOFError("No stdout")))
                return
            while True:
                try:
                    line = stdout.readline(MAX_RESPONSE_FRAME_BYTES + 2)
                    if not line:
                        q.put((None, EOFError("EOF")))
                        break
                    q.put((line, None))
                except Exception as exc:
                    q.put((None, exc))
                    break

        t = threading.Thread(
            target=_reader_loop,
            daemon=True,
            name=f"search-api-worker-reader-gen{self._generation}",
        )
        t.start()
        self._reader_thread = t
        return proc

    def _send_request(self, frame: dict) -> None:
        """Serialize and send request frame to child stdin."""
        data = json.dumps(frame, ensure_ascii=False).encode("utf-8")
        if len(data) > MAX_REQUEST_FRAME_BYTES:
            raise FrameOverflowError(
                f"Request frame exceeds maximum limit of {MAX_REQUEST_FRAME_BYTES}"
            )
        proc = self._child
        if proc is None or proc.poll() is not None or proc.stdin is None:
            raise EOFError("Child not running")
        try:
            proc.stdin.write(data + b"\n")
            proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            self._terminate_and_reap()
            raise EOFError("Failed to write to child stdin") from exc

    def _read_response(self, deadline: Optional[float]) -> dict:
        """Read and validate a response frame from child within deadline."""
        if self._queue is None:
            self._terminate_and_reap(deadline)
            raise EOFError("No reader queue available")

        if deadline is None:
            timeout = 5.0
        else:
            timeout = deadline - self._clock()
            if timeout <= 0:
                self._terminate_and_reap(deadline)
                raise TimeoutError("Deadline expired")

        try:
            line, exc = self._queue.get(timeout=max(0.001, timeout))
        except queue.Empty:
            self._terminate_and_reap(deadline)
            raise TimeoutError("Deadline expired") from None

        if exc is not None or line is None:
            self._terminate_and_reap(deadline)
            raise EOFError("Child exited unexpectedly")

        if len(line) > MAX_RESPONSE_FRAME_BYTES or not line.endswith(b"\n"):
            self._terminate_and_reap(deadline)
            raise FrameOverflowError("Response frame exceeded maximum allowed size")

        stripped = line.rstrip(b"\r\n")
        if len(stripped) > MAX_RESPONSE_FRAME_BYTES:
            self._terminate_and_reap(deadline)
            raise FrameOverflowError("Response frame exceeded maximum allowed size")

        try:
            frame = json.loads(stripped.decode("utf-8"))
        except Exception as e:
            self._terminate_and_reap(deadline)
            raise FrameError(f"Malformed JSON response: {e}") from e

        if not isinstance(frame, dict):
            self._terminate_and_reap(deadline)
            raise FrameError("Response frame must be a JSON object")

        if frame.get("generation") != self._generation:
            raise FrameError(
                f"Rejected frame with old generation {frame.get('generation')}, expected {self._generation}"
            )

        return frame

    def acquire_slot(
        self, deadline: Optional[float] = None, *, request_id: Optional[str] = None
    ) -> ReservationResult:
        """Atomically claim the single active slot for ``request_id``.

        The slot stays owned through ``prepare``/``dispatch`` until released.
        """
        req_id = request_id or uuid.uuid4().hex
        with self._lock:
            if self._slot_owner is not None and self._slot_owner != req_id:
                return ReservationResult(False, REASON_BUSY, req_id)
            self._slot_owner = req_id
        return ReservationResult(True, "", req_id)

    def prepare(
        self,
        provider: str,
        query: str,
        key: str,
        deadline: Optional[float] = None,
        *,
        request_id: Optional[str] = None,
    ) -> tuple[bool, str]:
        """Execute PREPARE phase of handshake.

        Returns (ok, reason).
        If ok is False, quota reservation can be released with zero consumed.
        """
        if getattr(sys, "frozen", False):
            return False, REASON_WORKER_UNAVAILABLE

        req_id = request_id or uuid.uuid4().hex

        with self._lock:
            if self._slot_owner is not None and self._slot_owner != req_id:
                return False, REASON_BUSY
            self._slot_owner = req_id

        req_frame = {
            "cmd": "PREPARE",
            "request_id": req_id,
            "generation": self._generation,
            "provider": provider,
            "query": query,
            "key": key,
            "deadline": deadline,
        }
        encoded = json.dumps(req_frame, ensure_ascii=False).encode("utf-8")
        if len(encoded) > MAX_REQUEST_FRAME_BYTES:
            with self._lock:
                if self._slot_owner == req_id:
                    self._slot_owner = None
            return False, REASON_PARSE

        try:
            with self._lock:
                self._ensure_child()
                req_frame["generation"] = self._generation
                self._send_request(req_frame)

            resp = self._read_response(deadline)
            if resp.get("cmd") != "READY" or resp.get("request_id") != req_id:
                with self._lock:
                    self._slot_owner = None
                return False, REASON_PARSE

            ok = bool(resp.get("ok", False))
            reason = str(resp.get("reason", ""))
            if not ok:
                with self._lock:
                    if self._slot_owner == req_id:
                        self._slot_owner = None
                return False, reason or REASON_PARSE

            # Successful PREPARE keeps slot_owner = req_id
            return True, ""

        except TimeoutError:
            with self._lock:
                self._slot_owner = None
            return False, REASON_TIMEOUT
        except EOFError:
            with self._lock:
                self._slot_owner = None
            return False, REASON_WORKER_UNAVAILABLE
        except FrameOverflowError:
            with self._lock:
                self._slot_owner = None
            return False, REASON_PARSE
        except FrameError:
            with self._lock:
                self._slot_owner = None
            return False, REASON_PARSE
        except Exception:
            with self._lock:
                self._slot_owner = None
            return False, REASON_WORKER_UNAVAILABLE

    def dispatch(
        self,
        provider: str,
        query: str,
        key: str,
        deadline: Optional[float] = None,
        *,
        request_id: Optional[str] = None,
    ) -> tuple[list[SearchResult], str]:
        """Execute SEND phase of handshake.

        Returns (results, reason). Always releases active slot when finished.
        """
        if getattr(sys, "frozen", False):
            return [], REASON_WORKER_UNAVAILABLE

        with self._lock:
            if self._slot_owner is None:
                return [], REASON_BUSY
            if request_id is not None and self._slot_owner != request_id:
                return [], REASON_BUSY
            req_id = self._slot_owner

        req_frame = {
            "cmd": "SEND",
            "request_id": req_id,
            "generation": self._generation,
            "provider": provider,
            "query": query,
            "key": key,
            "deadline": deadline,
        }
        encoded = json.dumps(req_frame, ensure_ascii=False).encode("utf-8")
        if len(encoded) > MAX_REQUEST_FRAME_BYTES:
            with self._lock:
                self._slot_owner = None
            return [], REASON_PARSE

        try:
            with self._lock:
                if self._child is None or self._child.poll() is not None:
                    self._slot_owner = None
                    return [], REASON_WORKER_UNAVAILABLE
                self._send_request(req_frame)

            resp = self._read_response(deadline)
            if resp.get("cmd") != "RESULT" or resp.get("request_id") != req_id:
                return [], REASON_PARSE

            raw_results = resp.get("results") or []
            reason = str(resp.get("reason", ""))
            results = [
                SearchResult(
                    title=str(item.get("title", "")),
                    url=str(item.get("url", "")),
                    snippet=str(item.get("snippet", "")),
                )
                for item in raw_results
                if isinstance(item, dict)
            ]
            return results, reason

        except TimeoutError:
            return [], REASON_TIMEOUT
        except EOFError:
            return [], REASON_WORKER_UNAVAILABLE
        except FrameOverflowError:
            return [], REASON_PARSE
        except FrameError:
            return [], REASON_PARSE
        except Exception:
            return [], REASON_WORKER_UNAVAILABLE
        finally:
            with self._lock:
                if self._slot_owner == req_id:
                    self._slot_owner = None

    def release_slot(self, request_id: Optional[str] = None) -> None:
        """Release active slot reservation without dispatching."""
        with self._lock:
            if request_id is None or self._slot_owner == request_id:
                self._slot_owner = None

    def close(self) -> None:
        """Terminate child process and reap resources."""
        with self._lock:
            proc = self._child
            self._child = None
            self._child_pid = None
            self._slot_owner = None
            self._queue = None
            self._reader_thread = None

            if proc is not None:
                if proc.poll() is None and proc.stdin is not None:
                    try:
                        proc.stdin.write(b'{"cmd":"QUIT"}\n')
                        proc.stdin.flush()
                    except OSError:
                        pass
                try:
                    proc.terminate()
                except OSError:
                    pass
                try:
                    proc.wait(timeout=0.5)
                except (subprocess.TimeoutExpired, OSError):
                    try:
                        proc.kill()
                        proc.wait(timeout=0.5)
                    except OSError:
                        pass


_RUNTIME_LOCK = threading.Lock()
_GLOBAL_RUNTIME: Optional[SearchApiRuntime] = None


def get_runtime() -> SearchApiRuntime:
    """Get or lazily initialize shared SearchApiRuntime singleton."""
    global _GLOBAL_RUNTIME
    with _RUNTIME_LOCK:
        if _GLOBAL_RUNTIME is None:
            _GLOBAL_RUNTIME = SearchApiRuntime()
        return _GLOBAL_RUNTIME


def close_runtime() -> None:
    """Close shared SearchApiRuntime singleton and reap child process."""
    global _GLOBAL_RUNTIME
    with _RUNTIME_LOCK:
        if _GLOBAL_RUNTIME is not None:
            try:
                _GLOBAL_RUNTIME.close()
            finally:
                _GLOBAL_RUNTIME = None
