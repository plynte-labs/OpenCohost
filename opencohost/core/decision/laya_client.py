"""Laya worker client: the ``DecisionBackend`` behind the DecisionBus
(laya-decision-layer-20261009).

Runs ``laya_worker.py`` under ``<python> -I`` and talks JSON lines over stdio.
Two engines:

  * ``onnx`` (default): the exported model (``laya_onnx.py``) under this
    interpreter, or ``OPENCOHOST_LAYA_PYTHON`` when set. Without a complete
    export in the ONNX dir (stat check here, sha256 check in the worker) the
    worker is ``unavailable`` with a reason code and is never spawned.
  * ``torch``: ``laya`` itself under ``OPENCOHOST_LAYA_PYTHON`` (the XTTS
    precedent: the model lives in its own env).

  * ``start()`` returns at once; the spawn and the model preload (minutes
    on a first download) run on a background thread, so neither app startup nor a
    turn ever waits for them. Until the worker answers its first ping as
    ready, ``available()`` is False and the bus takes the rule path.
  * ``ask()`` waits at most the turn budget. A timeout returns None and the
    request id is dropped, so a late answer is discarded.
  * A worker that exits is restarted once; after a second exit the client
    stays in ``error`` until ``restart()`` (owner action).
  * ``stop()`` closes stdin, terminates and reaps the process (app shutdown).

Nothing here logs or keeps the state text.
"""
from __future__ import annotations

import itertools
import json
import logging
import os
import subprocess
import sys
import threading
from typing import Optional

from opencohost.core.decision import laya_onnx

logger = logging.getLogger("OpenCohost")

WORKER_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "laya_worker.py")
# A first load downloads the model (~93 s measured on CPU); a cached load is faster.
STARTUP_TIMEOUT_S = 300.0
STOP_WAIT_S = 2.0

STATE_OFF = "off"
STATE_STARTING = "starting"
STATE_READY = "ready"
STATE_ERROR = "error"
STATE_UNAVAILABLE = "unavailable"

ENGINE_ONNX = "onnx"
ENGINE_TORCH = "torch"
# Reasons a worker reports that no restart can fix: the export itself.
_UNAVAILABLE_CODES = frozenset({laya_onnx.CODE_MISSING, laya_onnx.CODE_MANIFEST, laya_onnx.CODE_SHA256})


class _Slot:
    __slots__ = ("event", "reply")

    def __init__(self) -> None:
        self.event = threading.Event()
        self.reply: Optional[dict] = None


class LayaWorkerClient:
    """``DecisionBackend`` over one Laya worker subprocess."""

    def __init__(
        self,
        python: Optional[str],
        worker_path: Optional[str] = None,
        *,
        checkpoint: str = "",
        engine: str = ENGINE_ONNX,
        onnx_dir: str = "",
        startup_timeout_s: float = STARTUP_TIMEOUT_S,
        max_restarts: int = 1,
        popen=subprocess.Popen,
    ) -> None:
        self.engine = ENGINE_TORCH if (engine or "").strip().lower() == ENGINE_TORCH else ENGINE_ONNX
        self.python = (python or "").strip()
        if self.engine == ENGINE_ONNX and not self.python:
            self.python = sys.executable
        self.onnx_dir = (onnx_dir or "").strip() or laya_onnx.resolve_onnx_dir()
        self.worker_path = worker_path or WORKER_PATH
        self.checkpoint = (checkpoint or "").strip()
        self.startup_timeout_s = startup_timeout_s
        self.max_restarts = max_restarts
        self._popen = popen
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._ids = itertools.count(1)
        self._pending: dict[object, _Slot] = {}
        self._proc: Optional[subprocess.Popen] = None
        self._generation = 0
        self._restarts = 0
        self._state = STATE_OFF if self.configured else STATE_UNAVAILABLE
        self._last_error: Optional[str] = None
        self._ready_event = threading.Event()

    # -- status ---------------------------------------------------------------

    @property
    def configured(self) -> bool:
        return self._unconfigured_reason() is None

    def _unconfigured_reason(self) -> Optional[str]:
        """Why the worker cannot run, or None. ``""`` is the historical
        reason-less unavailable (no interpreter or no worker script)."""
        if not (self.python and os.path.isfile(self.python) and os.path.isfile(self.worker_path)):
            return ""
        if self.engine == ENGINE_ONNX and not laya_onnx.artifacts_present(self.onnx_dir):
            return laya_onnx.CODE_MISSING
        return None

    def status(self) -> dict:
        reason = self._unconfigured_reason()
        with self._lock:
            if reason is not None:
                return {
                    "configured": False,
                    "state": STATE_UNAVAILABLE,
                    "checkpoint": None,
                    "last_error": reason or None,
                    "engine": self.engine,
                }
            return {
                "configured": True,
                "state": self._state,
                "checkpoint": self.checkpoint or None,
                "last_error": self._last_error,
                "engine": self.engine,
            }

    def available(self) -> bool:
        with self._lock:
            return self._state == STATE_READY

    def wait_ready(self, timeout: float) -> bool:
        """Block until ready (tests, diagnostics). Never used on the turn path."""
        self._ready_event.wait(timeout)
        return self.available()

    # -- lifecycle ------------------------------------------------------------

    def start(self) -> None:
        """Spawn the worker in the background. No-op when unconfigured or running."""
        if not self.configured:
            return
        with self._lock:
            if self._state in (STATE_STARTING, STATE_READY):
                return
        self._spawn_async()

    def restart(self) -> None:
        """Owner-requested restart: resets the crash budget and respawns."""
        if not self.configured:
            return
        self._terminate()
        with self._lock:
            self._restarts = 0
            self._last_error = None
        self._spawn_async()

    def stop(self) -> None:
        """Terminate the worker. Idempotent, never raises."""
        self._terminate()
        with self._lock:
            if self.configured:
                self._state = STATE_OFF

    def _spawn_async(self) -> None:
        with self._lock:
            generation = self._begin_spawn_locked()
        self._launch_thread(generation)

    def _begin_spawn_locked(self) -> int:
        self._generation += 1
        self._state = STATE_STARTING
        self._ready_event.clear()
        return self._generation

    def _launch_thread(self, generation: int) -> None:
        threading.Thread(
            target=self._launch, args=(generation,), name="laya-worker-start", daemon=True
        ).start()

    def _launch(self, generation: int) -> None:
        env = dict(os.environ)
        env["OPENCOHOST_LAYA_CHECKPOINT"] = self.checkpoint
        env["OPENCOHOST_LAYA_ENGINE"] = self.engine
        env["OPENCOHOST_LAYA_ONNX_DIR"] = self.onnx_dir
        env["HF_HUB_DISABLE_SYMLINKS_WARNING"] = "1"
        kwargs = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            proc = self._popen(
                [self.python, "-I", self.worker_path],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=env,
                **kwargs,
            )
        except Exception as exc:
            self._fail(generation, f"spawn_{type(exc).__name__}")
            return
        with self._lock:
            if generation != self._generation:
                _kill(proc)
                return
            self._proc = proc
        threading.Thread(
            target=self._read_loop, args=(proc, generation), name="laya-worker-reader", daemon=True
        ).start()
        reply = self._request({"op": "ping"}, self.startup_timeout_s, generation)
        with self._lock:
            if generation != self._generation:
                return
            if isinstance(reply, dict) and reply.get("ready"):
                self._state = STATE_READY
                self._ready_event.set()
                logger.info(
                    "[DECISION] Laya worker ready engine=%s checkpoint=%s",
                    self.engine, self.checkpoint or "auto",
                )
                return
        code = reply.get("error") if isinstance(reply, dict) else None
        if code in _UNAVAILABLE_CODES:
            # The export is missing or tampered: no restart can fix it, so
            # stop the idle worker and report the reason; the rules act.
            with self._lock:
                if generation != self._generation:
                    return
                self._generation += 1  # the reader must not treat this kill as a crash
                self._proc = None
                self._state = STATE_UNAVAILABLE
                self._last_error = code
                self._ready_event.set()
            _kill(proc)
            logger.warning("[DECISION] Laya worker unavailable=%s; rules act", code)
            return
        if reply is None and _exited(proc):
            return  # exited: the reader owns the crash path
        if reply is None:
            with self._lock:
                self._generation += 1  # the reader must not treat this kill as a crash
            _kill(proc)
            self._fail(None, "startup_timeout")
        else:
            self._fail(generation, code if isinstance(code, str) and code else "not_ready")

    def _fail(self, generation: Optional[int], code: str) -> None:
        with self._lock:
            if generation is not None and generation != self._generation:
                return
            self._state = STATE_ERROR
            self._last_error = code
            self._ready_event.set()
        logger.warning("[DECISION] Laya worker error=%s; rules act", code)

    def _read_loop(self, proc, generation: int) -> None:
        stdout = proc.stdout
        try:
            for raw in stdout:
                try:
                    msg = json.loads(raw.decode("utf-8"))
                    msg_id = msg.get("id")
                except Exception:
                    with self._lock:
                        self._last_error = "malformed_output"
                    continue
                with self._lock:
                    slot = self._pending.pop(msg_id, None)
                if slot is not None:
                    slot.reply = msg
                    slot.event.set()
        except Exception:
            pass
        self._on_exit(proc, generation)

    def _on_exit(self, proc, generation: int) -> None:
        try:
            proc.wait(timeout=STOP_WAIT_S)
        except Exception:
            pass
        with self._lock:
            if generation != self._generation:
                return  # stopped, restarted or superseded on purpose
            self._fail_pending_locked()
            self._last_error = "worker_exited"
            self._proc = None
            restart = self._restarts < self.max_restarts
            if restart:
                self._restarts += 1
                generation = self._begin_spawn_locked()
            else:
                self._state = STATE_ERROR
                self._ready_event.set()
        if restart:
            logger.warning("[DECISION] Laya worker exited; restarting once")
            self._launch_thread(generation)
        else:
            logger.warning("[DECISION] Laya worker exited again; rules act until restart")

    def _fail_pending_locked(self) -> None:
        pending, self._pending = self._pending, {}
        for slot in pending.values():
            slot.event.set()

    def _terminate(self) -> None:
        with self._lock:
            self._generation += 1
            proc, self._proc = self._proc, None
            self._fail_pending_locked()
            self._ready_event.set()
        if proc is not None:
            _kill(proc)

    # -- requests -------------------------------------------------------------

    def _request(self, payload: dict, timeout_s: float, generation: int) -> Optional[dict]:
        msg_id = next(self._ids)
        slot = _Slot()
        with self._lock:
            proc = self._proc
            if proc is None or generation != self._generation:
                return None
            self._pending[msg_id] = slot
        line = (json.dumps({**payload, "id": msg_id}, ensure_ascii=False) + "\n").encode("utf-8")
        try:
            with self._write_lock:
                proc.stdin.write(line)
                proc.stdin.flush()
        except Exception:
            with self._lock:
                self._pending.pop(msg_id, None)
            return None
        slot.event.wait(max(0.0, timeout_s))
        with self._lock:
            self._pending.pop(msg_id, None)
        return slot.reply

    def ask(self, state: str, questions: dict, timeout_s: float) -> Optional[dict]:
        """One forward pass for every question, or None on timeout/unavailable."""
        with self._lock:
            if self._state != STATE_READY:
                return None
            generation = self._generation
        return self._request({"state": state, "questions": questions}, timeout_s, generation)


def _exited(proc, wait_s: float = 0.5) -> bool:
    """True when the process is gone (gives a dying process ``wait_s``)."""
    try:
        proc.wait(timeout=wait_s)
        return True
    except Exception:
        return proc.poll() is not None


def _kill(proc) -> None:
    try:
        if proc.stdin is not None:
            proc.stdin.close()
    except Exception:
        pass
    try:
        proc.terminate()
        proc.wait(timeout=STOP_WAIT_S)
    except Exception:
        try:
            proc.kill()
            proc.wait(timeout=STOP_WAIT_S)
        except Exception:
            pass
    for stream in (proc.stdout,):
        try:
            if stream is not None:
                stream.close()
        except Exception:
            pass
