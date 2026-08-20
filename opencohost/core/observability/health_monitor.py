"""Health Monitor — system health daemon for OpenCohost.

Provides real-time monitoring of:
- GPU VRAM availability (via pynvml, graceful degradation)
- Ollama service health (via /api/tags)
- TTS Real-Time Factor (RTF) tracking
- Qwen server subprocess lifecycle
- Overall system health aggregation (green/yellow/red)

All components are thread-safe. HealthMonitor runs as a daemon thread.
"""

from __future__ import annotations

import logging
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

import requests

from opencohost.config.llm_provider import load_provider_config
from opencohost.config.storage import resolve_xtts_python
from opencohost.config.settings import (
    HEALTH_POLL_INTERVAL,
    LOG_DIR,
    OLLAMA_FAILURE_THRESHOLD,
    OLLAMA_POLL_INTERVAL,
    OLLAMA_REQUEST_TIMEOUT,
    QWEN_IDLE_TTL,
    QWEN_STARTUP_TIMEOUT,
    RTF_HIGH_THRESHOLD,
    RTF_POLL_WINDOW,
    RTF_RECOVERY_COUNT,
    RTF_RECOVERY_THRESHOLD,
    VRAM_CRITICAL_THRESHOLD_MB,
    VRAM_LOW_THRESHOLD_MB,
    VRAM_POLL_INTERVAL,
)

logger = logging.getLogger("HealthMonitor")

LIFECYCLE_STARTING = "starting"
LIFECYCLE_WAITING = "waiting"
LIFECYCLE_DEGRADED = "degraded"
LIFECYCLE_READY = "ready"
LIFECYCLE_FAILED = "failed"
LIFECYCLE_STOPPED = "stopped"  # cleanly stopped (distinct from FAILED) — contract B2/F4


# ──────────────────────────────────────────────
# VRAM Guard
# ──────────────────────────────────────────────

class VRAMGuard:
    """Polls GPU free VRAM via pynvml. Graceful degradation when unavailable."""

    def __init__(self) -> None:
        self._pynvml_available = False
        self._handle = None
        self._pynvml = None
        self._free_mb: float = 0.0
        self._total_mb: float = 0.0
        self._used_mb: float = 0.0
        self._status: str = "unavailable"
        self._lock = threading.Lock()

        # Try to import pynvml; fail gracefully
        try:
            import pynvml
            pynvml.nvmlInit()
            self._handle = pynvml.nvmlDeviceGetHandleByIndex(0)
            self._pynvml = pynvml
            self._pynvml_available = True
            logger.info("VRAMGuard: pynvml initialized successfully")
        except Exception:
            logger.debug("VRAMGuard: pynvml not available; graceful degradation mode")

    def poll(self) -> None:
        """Poll current free VRAM and update status."""
        if not self._pynvml_available:
            with self._lock:
                self._status = "unavailable"
                self._free_mb = 0.0
                self._total_mb = 0.0
                self._used_mb = 0.0
            return

        try:
            # F9 (runtime_findings_batch_20260731): total/used come back on the
            # SAME nvmlDeviceGetMemoryInfo call as free — they were being read
            # and discarded.
            info = self._pynvml.nvmlDeviceGetMemoryInfo(self._handle)
            free_mb = info.free / (1024 * 1024)
            total_mb = info.total / (1024 * 1024)
            used_mb = info.used / (1024 * 1024)
            with self._lock:
                self._free_mb = free_mb
                self._total_mb = total_mb
                self._used_mb = used_mb
                if free_mb < VRAM_CRITICAL_THRESHOLD_MB:
                    self._status = "critical"
                elif free_mb < VRAM_LOW_THRESHOLD_MB:
                    self._status = "low"
                else:
                    self._status = "normal"
        except Exception as e:
            logger.warning(f"VRAMGuard: poll failed: {e}")
            with self._lock:
                self._status = "unavailable"
                self._free_mb = 0.0
                self._total_mb = 0.0
                self._used_mb = 0.0

    @property
    def status(self) -> str:
        with self._lock:
            return self._status

    @property
    def free_mb(self) -> float:
        with self._lock:
            return self._free_mb

    @property
    def total_mb(self) -> float:
        with self._lock:
            return self._total_mb

    @property
    def used_mb(self) -> float:
        with self._lock:
            return self._used_mb


# ──────────────────────────────────────────────
# Ollama Watchdog
# ──────────────────────────────────────────────

class OllamaWatchdog:
    """Polls Ollama /api/tags to check service health."""

    def __init__(self) -> None:
        self._consecutive_failures = 0
        self._status: str = "unknown"
        self._lifecycle_state: str = LIFECYCLE_STARTING
        self._message: str = "Ollama startup check pending"
        self._lock = threading.Lock()
        self._base_url = "http://127.0.0.1:11434"

    def poll(self) -> None:
        """Poll Ollama /api/tags endpoint."""
        try:
            resp = requests.get(
                f"{self._base_url}/api/tags",
                timeout=OLLAMA_REQUEST_TIMEOUT,
            )
            if resp.status_code == 200:
                with self._lock:
                    self._consecutive_failures = 0
                    self._status = "healthy"
                    self._lifecycle_state = LIFECYCLE_READY
                    self._message = "Ollama ready"
                return
        except Exception:
            pass

        with self._lock:
            self._consecutive_failures += 1
            if self._consecutive_failures >= OLLAMA_FAILURE_THRESHOLD:
                self._status = "down"
                self._lifecycle_state = LIFECYCLE_DEGRADED
                self._message = "Ollama unavailable after startup retries"
            else:
                self._status = "unknown"
                self._lifecycle_state = LIFECYCLE_WAITING
                self._message = "Waiting for Ollama startup"

    @property
    def status(self) -> str:
        with self._lock:
            return self._status

    @property
    def consecutive_failures(self) -> int:
        with self._lock:
            return self._consecutive_failures

    @property
    def lifecycle_state(self) -> str:
        with self._lock:
            return self._lifecycle_state

    @property
    def message(self) -> str:
        with self._lock:
            return self._message


# ──────────────────────────────────────────────
# Ollama Residency Probe (F9 — Ollama's own accounting, never process RSS)
# ──────────────────────────────────────────────

class OllamaResidencyProbe:
    """Polls `ollama.ps()` (`/api/ps`) for the loaded model's memory split.

    `OLLAMA_MAX_LOADED_MODELS=1` (ollama_startup.py) — at most one entry ever
    comes back; only models[0] is read. Every number here is OLLAMA's OWN
    ESTIMATE of where the model sits (`size` total vs. `size_vram` in VRAM),
    NOT process RSS (F9, runtime_findings_batch_20260731) — hence the `_est`
    suffix on the numeric fields.

    Bounded via a short-timeout `ollama.Client`: the package's module-level
    default client uses `timeout=None` and would let a busy/stalling daemon
    hang this poll (and the whole health-monitor thread) forever.
    """

    def __init__(self) -> None:
        self._ollama_module = None
        try:
            import ollama
            self._ollama_module = ollama
        except ImportError:
            logger.debug("OllamaResidencyProbe: ollama package not available")

        self._resident_mb: Optional[float] = None
        self._vram_mb: Optional[float] = None
        self._spill_mb: Optional[float] = None
        self._processor_split: Optional[str] = None
        self._lock = threading.Lock()

    def _clear(self) -> None:
        with self._lock:
            self._resident_mb = None
            self._vram_mb = None
            self._spill_mb = None
            self._processor_split = None

    def poll(self, ollama_up: bool) -> None:
        """Refresh residency estimates.

        Skipped entirely while Ollama is not reachable (per the existing
        OllamaWatchdog flag) so a dying daemon never adds a probe on top of
        the reachability check that already failed. Values are CLEARED
        rather than left stale from the last healthy read.
        """
        if not ollama_up or self._ollama_module is None:
            self._clear()
            return

        try:
            client_factory = getattr(self._ollama_module, "Client", None)
            if client_factory is None:
                self._clear()
                return
            try:
                client = client_factory(timeout=OLLAMA_REQUEST_TIMEOUT)
            except TypeError:
                # Mirrors llm_engine._create_ollama_chat_client's defensive
                # fallback for an ollama-python version that rejects timeout.
                client = client_factory()
            response = client.ps()
            models = getattr(response, "models", None) or []
            if not models:
                self._clear()
                return

            model = models[0]
            size = getattr(model, "size", None) or 0
            size_vram = getattr(model, "size_vram", None) or 0
            resident_mb = float(size) / (1024 * 1024)
            vram_mb = float(size_vram) / (1024 * 1024)
            spill_mb = max(0.0, resident_mb - vram_mb)

            # ollama-python's ProcessResponse.Model has no `processor` field
            # today (checked: model/name/digest/expires_at/size/size_vram/
            # details/context_length) — the CLI's PROCESSOR column is
            # derived, not returned. Prefer it if a future version adds it;
            # else derive the split ourselves.
            processor_split = getattr(model, "processor", None)
            if not processor_split:
                if resident_mb > 0:
                    pct_gpu = round(vram_mb / resident_mb * 100)
                    processor_split = f"{pct_gpu}% GPU / {100 - pct_gpu}% CPU"
                else:
                    processor_split = None

            with self._lock:
                self._resident_mb = resident_mb
                self._vram_mb = vram_mb
                self._spill_mb = spill_mb
                self._processor_split = processor_split
        except Exception as e:
            logger.warning(f"OllamaResidencyProbe: poll failed: {e}")
            self._clear()

    @property
    def resident_mb(self) -> Optional[float]:
        with self._lock:
            return self._resident_mb

    @property
    def vram_mb(self) -> Optional[float]:
        with self._lock:
            return self._vram_mb

    @property
    def spill_mb(self) -> Optional[float]:
        with self._lock:
            return self._spill_mb

    @property
    def processor_split(self) -> Optional[str]:
        with self._lock:
            return self._processor_split


# ──────────────────────────────────────────────
# RTF Tracker
# ──────────────────────────────────────────────

class RTFTracker:
    """Tracks Real-Time Factor for TTS generation quality.

    RTF = generation_time / audio_duration
    Lower is better. RTF > 2.0 means generation is slower than real-time.
    """

    def __init__(self) -> None:
        self._measurements: deque = deque(maxlen=RTF_POLL_WINDOW)
        self._lock = threading.Lock()

    def record(self, generation_time: float, audio_duration: float) -> None:
        """Record an RTF measurement. Excludes audio_duration < 0.5s."""
        if audio_duration < 0.5:
            return
        rtf = generation_time / audio_duration if audio_duration > 0 else 0.0
        with self._lock:
            self._measurements.append(rtf)

    @property
    def rolling_average(self) -> Optional[float]:
        with self._lock:
            if not self._measurements:
                return None
            return sum(self._measurements) / len(self._measurements)

    @property
    def status(self) -> str:
        avg = self.rolling_average
        if avg is None:
            return "unknown"
        if avg > RTF_HIGH_THRESHOLD:
            return "degraded"
        return "normal"

    @property
    def count(self) -> int:
        with self._lock:
            return len(self._measurements)


# ──────────────────────────────────────────────
# Qwen Process Manager
# ──────────────────────────────────────────────

class QwenProcessManager:
    """Manages the Qwen TTS server subprocess lifecycle.

    Can start, stop, and attach to existing server processes.
    Detects manually-started servers and avoids killing them.
    """

    # Three levels up from this file is the opencohost package root, where
    # server_qwen.py lives. Adding a directory level to this module's path
    # silently repoints this at a file that does not exist -- every start()
    # test mocks Popen, so only test_qwen_server_script_resolves_to_an_existing_file
    # catches it.
    SERVER_SCRIPT = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "server_qwen.py",
    )
    HEALTH_URL = "http://127.0.0.1:5000/health"
    APP_ID = "opencohost-qwen-tts"

    def __init__(self) -> None:
        self._process: Optional[subprocess.Popen] = None
        self._is_manual: bool = False
        self._last_health_time: float = 0.0
        self._lock = threading.Lock()
        self._stdout_log = None
        self._stderr_log = None
        self._lifecycle_state: str = LIFECYCLE_STARTING

    def _open_subprocess_logs(self):
        """Open dedicated files that preserve Qwen startup tracebacks."""
        os.makedirs(LOG_DIR, exist_ok=True)
        stdout_log = open(os.path.join(LOG_DIR, "server_qwen_stdout.log"), "a", encoding="utf-8")
        stderr_log = open(os.path.join(LOG_DIR, "server_qwen_stderr.log"), "a", encoding="utf-8")
        return stdout_log, stderr_log

    def _close_subprocess_logs(self) -> None:
        for handle_name in ("_stdout_log", "_stderr_log"):
            handle = getattr(self, handle_name, None)
            if handle is not None:
                try:
                    handle.close()
                except Exception:
                    logger.debug("QwenProcessManager: failed to close subprocess log", exc_info=True)
                setattr(self, handle_name, None)

    def start(self) -> bool:
        """Launch server_qwen.py via subprocess. Returns True if healthy.

        When XTTS_PYTHON cannot be resolved (env var unset, storage.yaml "auto"),
        logs an actionable warning and returns False without raising — mirrors the
        PiperEngine.load() never-raise contract.
        """
        with self._lock:
            if self._process is not None and self._process.poll() is None:
                logger.debug("QwenProcessManager: server already running")
                return True

        xtts_python = resolve_xtts_python()
        if xtts_python is None:
            logger.warning(
                "QwenProcessManager: XTTS_PYTHON interpreter not configured — "
                "set the XTTS_PYTHON environment variable or set "
                "tools.xtts_python in config/storage.yaml. Qwen TTS unavailable."
            )
            self._lifecycle_state = LIFECYCLE_FAILED
            return False

        cmd = [str(xtts_python), self.SERVER_SCRIPT]
        logger.info(f"QwenProcessManager: starting Qwen server: {' '.join(cmd)}")

        try:
            creationflags = 0
            if sys.platform == "win32":
                c_no_win = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
                c_new_grp = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
                creationflags = c_no_win | c_new_grp

            # If a previous managed Qwen process exited unexpectedly and start()
            # is called again before stop(), close stale log handles before
            # replacing them. This keeps the diagnostic upgrade leak-free across
            # restart attempts.
            self._close_subprocess_logs()
            self._stdout_log, self._stderr_log = self._open_subprocess_logs()

            self._process = subprocess.Popen(
                cmd,
                stdout=self._stdout_log,
                stderr=self._stderr_log,
                creationflags=creationflags,
            )
            self._is_manual = False
            self._lifecycle_state = LIFECYCLE_WAITING
        except Exception as e:
            self._close_subprocess_logs()
            self._lifecycle_state = LIFECYCLE_FAILED
            logger.error(f"QwenProcessManager: failed to start: {e}")
            return False

        # Wait for /health to respond
        deadline = time.time() + QWEN_STARTUP_TIMEOUT
        while time.time() < deadline:
            if self._check_health():
                with self._lock:
                    self._last_health_time = time.time()
                    self._lifecycle_state = LIFECYCLE_READY
                logger.info("QwenProcessManager: server started and healthy")
                return True
            time.sleep(1.0)

        self._lifecycle_state = LIFECYCLE_FAILED
        logger.warning("QwenProcessManager: startup timeout waiting for /health")
        self.stop()
        return False

    def stop(self, graceful_timeout: float = 3.0, kill_timeout: float = 2.0) -> None:
        """Stop the managed server process. Does NOT stop manual servers."""
        with self._lock:
            if self._is_manual:
                logger.info("QwenProcessManager: external/manual server detected; shutdown skipped")
                return
            if self._process is None:
                return

            proc = self._process
            self._process = None
            # Owned shutdown: reset lifecycle + idle clock so the engine/health pill
            # and idle_seconds do not misreport a stale READY after stop (contract B2/F4).
            # Preserve a FAILED verdict: a failed start() calls stop() to clean up, and that
            # failure must not be masked as a clean STOPPED.
            if self._lifecycle_state != LIFECYCLE_FAILED:
                self._lifecycle_state = LIFECYCLE_STOPPED
            self._last_health_time = 0.0

        if proc.poll() is not None:
            self._close_subprocess_logs()
            return  # Already exited

        try:
            if sys.platform == "win32":
                sig = getattr(signal, "CTRL_BREAK_EVENT", getattr(signal, "SIGTERM", 15))
                proc.send_signal(sig)
            else:
                proc.terminate()
        except Exception as e:
            logger.warning(f"QwenProcessManager: graceful stop failed, killing server: {e}")
            try:
                proc.kill()
                proc.wait(timeout=kill_timeout)
                logger.info("QwenProcessManager: server killed after graceful stop failure")
            except Exception as kill_error:
                logger.warning(f"QwenProcessManager: forced kill failed: {kill_error}")
            finally:
                self._close_subprocess_logs()
            return

        self._close_subprocess_logs()

        # Wait briefly, then force-kill only the owned child process.
        try:
            proc.wait(timeout=graceful_timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=kill_timeout)
        logger.info("QwenProcessManager: server stopped")

    def attach_existing(self) -> bool:
        """Check if a server is already running on port 5000 and attach to it."""
        if self._is_port_in_use(5000):
            if self._check_health():
                with self._lock:
                    self._is_manual = True
                    self._process = None
                    self._last_health_time = time.time()
                    self._lifecycle_state = LIFECYCLE_READY
                logger.info("QwenProcessManager: attached to existing manual server")
                return True
        return False

    @property
    def is_running(self) -> bool:
        with self._lock:
            if self._is_manual:
                return self._is_port_in_use(5000)
            if self._process is None:
                return False
            return self._process.poll() is None

    @property
    def is_healthy(self) -> bool:
        if self._check_health():
            with self._lock:
                self._last_health_time = time.time()
            return True
        return False

    @property
    def is_manual(self) -> bool:
        with self._lock:
            return self._is_manual

    @property
    def ownership(self) -> str:
        with self._lock:
            if self._is_manual:
                return "external"
            if self._process is not None:
                return "owned"
            return "none"

    @property
    def lifecycle_state(self) -> str:
        with self._lock:
            return self._lifecycle_state

    @property
    def idle_seconds(self) -> float:
        with self._lock:
            if self._last_health_time == 0.0:
                return 0.0
            return time.time() - self._last_health_time

    def _check_health(self) -> bool:
        try:
            resp = requests.get(self.HEALTH_URL, timeout=3)
            if resp.status_code != 200:
                return False

            try:
                payload = resp.json()
            except ValueError:
                # Preserve attach compatibility with older/manual servers that
                # only expose an HTTP 200 health probe.
                return True

            if "app" in payload and payload.get("app") != self.APP_ID:
                return False
            if "model_loaded" in payload:
                status_ok = "status" not in payload or payload.get("status") == "ok"
                return payload.get("model_loaded") is True and status_ok
            if "status" in payload:
                return payload.get("status") == "ok"
            return True
        except Exception:
            return False

    @staticmethod
    def _is_port_in_use(port: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            return s.connect_ex(("127.0.0.1", port)) == 0


# ──────────────────────────────────────────────
# MonitorState
# ──────────────────────────────────────────────

@dataclass
class MonitorState:
    """Thread-safe snapshot of overall system health."""
    vram_status: str = "unknown"
    rtf_status: str = "unknown"
    ollama_status: str = "unknown"
    qwen_status: str = "unknown"
    overall_status: str = "unknown"
    ollama_lifecycle: str = LIFECYCLE_STARTING
    qwen_lifecycle: str = LIFECYCLE_STARTING
    free_vram_mb: float = 0.0
    rtf_rolling_avg: Optional[float] = None
    last_updated: float = 0.0
    # F9/2.4 (runtime_findings_batch_20260731): same nvmlDeviceGetMemoryInfo
    # call as free_vram_mb — total/used were being read and discarded.
    total_vram_mb: float = 0.0
    used_vram_mb: float = 0.0
    # Ollama residency ESTIMATES from ollama.ps() (size/size_vram) — NOT
    # process RSS. None while no model is loaded or Ollama is unreachable.
    model_resident_mb_est: Optional[float] = None
    model_vram_mb_est: Optional[float] = None
    model_ram_spill_mb_est: Optional[float] = None
    model_processor_split: Optional[str] = None


# ──────────────────────────────────────────────
# HealthMonitor Daemon
# ──────────────────────────────────────────────

class HealthMonitor(threading.Thread):
    """Main health monitoring daemon.

    Runs on its own thread, polls all sub-components, and exposes
    thread-safe health state via the `state` property.
    """

    def __init__(self) -> None:
        super().__init__(name="HealthMonitor", daemon=True)
        self._vram = VRAMGuard()
        self._ollama = OllamaWatchdog()
        self._ollama_residency = OllamaResidencyProbe()
        self._rtf = RTFTracker()
        self._qwen = QwenProcessManager()

        self._state = MonitorState()
        self._lock = threading.Lock()
        self._stop_event = threading.Event()

    def run(self) -> None:
        """Main polling loop. Runs until stop() is called."""
        logger.info("HealthMonitor: daemon started")
        while not self._stop_event.is_set():
            try:
                self._poll_all()
            except Exception:
                logger.exception("HealthMonitor: polling cycle failed")
                self._mark_poll_unknown()
            self._stop_event.wait(HEALTH_POLL_INTERVAL)
        logger.info("HealthMonitor: daemon stopped")

    def _mark_poll_unknown(self) -> None:
        """Avoid exposing stale healthy state after an unexpected poll failure."""
        with self._lock:
            self._state = MonitorState(last_updated=time.time())

    def stop(self) -> None:
        """Signal the daemon to stop and clean up resources."""
        self._stop_event.set()
        self._qwen.stop()
        self.join(timeout=5)

    def _poll_all(self) -> None:
        """Poll all sub-components and update state atomically."""
        self._vram.poll()
        self._ollama.poll()
        # No new timer/thread — piggybacks on this same 5s (HEALTH_POLL_INTERVAL)
        # cadence. Only probes while OllamaWatchdog already says "healthy".
        self._ollama_residency.poll(ollama_up=self._ollama.status == "healthy")
        qwen_healthy = self._qwen.is_healthy  # triggers health check + idle TTL reset

        qwen_status = self._qwen_status(qwen_healthy)
        overall = self._compute_overall(qwen_status)

        with self._lock:
            self._state = MonitorState(
                vram_status=self._vram.status,
                rtf_status=self._rtf.status,
                ollama_status=self._ollama.status,
                qwen_status=qwen_status,
                overall_status=overall,
                ollama_lifecycle=self._ollama.lifecycle_state,
                qwen_lifecycle=self._qwen.lifecycle_state,
                free_vram_mb=self._vram.free_mb,
                rtf_rolling_avg=self._rtf.rolling_average,
                last_updated=time.time(),
                total_vram_mb=self._vram.total_mb,
                used_vram_mb=self._vram.used_mb,
                model_resident_mb_est=self._ollama_residency.resident_mb,
                model_vram_mb_est=self._ollama_residency.vram_mb,
                model_ram_spill_mb_est=self._ollama_residency.spill_mb,
                model_processor_split=self._ollama_residency.processor_split,
            )

    def _qwen_status(self, qwen_healthy: bool) -> str:
        if qwen_healthy:
            return "healthy"
        if self._qwen.is_running:
            return "unhealthy"
        if self._qwen.is_manual:
            return "unavailable"
        if self._qwen.ownership == "owned":
            return "unhealthy"  # we spawned it and it died — real failure
        return "unavailable"  # never started / not attached — not in use (Piper/Edge session)

    def _is_cloud_active(self) -> bool:
        """Provider-aware aggregation (multi_provider_llm_20260723 Phase 5).

        Read fresh per poll — a cheap local JSON read, no caching — so a
        live provider switch (PUT /api/llm/provider) is reflected on the
        very next health poll without an engine restart. Any read failure
        defaults to local (byte-identical pre-track behavior; load_provider_
        config() already fails open on a missing/corrupt file, this is a
        second guard against an unexpected raise).
        """
        try:
            return load_provider_config().get("active_provider", "local") != "local"
        except Exception:
            return False

    def _compute_overall(self, qwen_status: Optional[str] = None) -> str:
        """Compute overall health: green, yellow, or red."""
        vram = self._vram.status
        rtf = self._rtf.status
        if qwen_status is None:
            qwen_status = self._qwen_status(self._qwen.is_healthy)
        ollama = self._ollama.status

        # RED conditions: any single critical factor
        if vram == "critical":
            return "red"
        # OllamaWatchdog stays informational-only when cloud active (spec D)
        # — a stopped Ollama must not mark a pure-cloud profile unhealthy.
        # `ollama_status` itself is untouched (still visible in MonitorState).
        if ollama == "down" and not self._is_cloud_active():
            return "red"
        if qwen_status not in ("healthy", "unavailable"):
            return "red"

        # YELLOW conditions: any single degraded factor
        if vram == "low":
            return "yellow"
        if rtf == "degraded":
            return "yellow"

        return "green"

    @property
    def state(self) -> MonitorState:
        """Return a thread-safe snapshot of current health state."""
        with self._lock:
            return MonitorState(
                vram_status=self._state.vram_status,
                rtf_status=self._state.rtf_status,
                ollama_status=self._state.ollama_status,
                qwen_status=self._state.qwen_status,
                overall_status=self._state.overall_status,
                ollama_lifecycle=self._state.ollama_lifecycle,
                qwen_lifecycle=self._state.qwen_lifecycle,
                free_vram_mb=self._state.free_vram_mb,
                rtf_rolling_avg=self._state.rtf_rolling_avg,
                last_updated=self._state.last_updated,
                total_vram_mb=self._state.total_vram_mb,
                used_vram_mb=self._state.used_vram_mb,
                model_resident_mb_est=self._state.model_resident_mb_est,
                model_vram_mb_est=self._state.model_vram_mb_est,
                model_ram_spill_mb_est=self._state.model_ram_spill_mb_est,
                model_processor_split=self._state.model_processor_split,
            )

    def should_use_heavy_tts(self, auto_fallback_enabled: bool, manual_motor: str) -> bool:
        """Decide whether heavy TTS (Qwen) should be used.

        Args:
            auto_fallback_enabled: Whether auto-fallback is active.
            manual_motor: Current manual motor selection ("ligero" or "pesado").

        Returns:
            True if heavy TTS should be used, False if fallback to Edge-TTS.
        """
        return self.heavy_tts_block_reason(auto_fallback_enabled, manual_motor) is None

    def heavy_tts_block_reason(self, auto_fallback_enabled: bool, manual_motor: str) -> Optional[str]:
        """Return why Qwen heavy TTS is blocked, or None when it is allowed.

        This is intentionally side-effect-free so the TTS pipeline can log the
        exact fallback reason seen by the streamer without changing policy.
        """
        if not auto_fallback_enabled:
            # Backward compat: if auto-fallback disabled, always respect manual
            return None if manual_motor == "pesado" else "manual_motor=ligero"

        # Manual "ligero" always uses Edge-TTS regardless of health
        if manual_motor == "ligero":
            return "manual_motor=ligero"

        # Check health gates
        s = self.state
        if s.vram_status in ("low", "critical"):
            reason = (
                f"vram_{s.vram_status} "
                f"free={s.free_vram_mb:.0f}MB "
                f"required>={VRAM_LOW_THRESHOLD_MB}MB"
            )
            logger.info(f"HealthMonitor: heavy TTS blocked — {reason}")
            return reason
        if s.rtf_status == "degraded":
            reason = f"rtf_degraded avg={s.rtf_rolling_avg}"
            logger.info(f"HealthMonitor: heavy TTS blocked — {reason}")
            return reason
        if s.qwen_status not in ("healthy", "unavailable"):
            reason = f"qwen_{s.qwen_status}"
            logger.info(f"HealthMonitor: heavy TTS blocked — {reason}")
            return reason

        return None

    def can_vibe_call(self) -> bool:
        """Check if Vibe LLM calls are allowed.

        Returns False when VRAM is low/critical or Ollama is down.
        """
        s = self.state
        if s.vram_status in ("low", "critical"):
            return False
        if s.ollama_status == "down":
            return False
        return True

    def record_ttf_measurement(self, generation_time: float, audio_duration: float) -> None:
        """Record an RTF measurement from a completed TTS generation."""
        try:
            self._rtf.record(generation_time, audio_duration)
        except Exception as e:
            logger.warning(f"HealthMonitor: RTF measurement failed: {e}")

    @property
    def vram_guard(self) -> VRAMGuard:
        return self._vram

    @property
    def ollama_watchdog(self) -> OllamaWatchdog:
        return self._ollama

    @property
    def rtf_tracker(self) -> RTFTracker:
        return self._rtf

    @property
    def qwen_manager(self) -> QwenProcessManager:
        return self._qwen
