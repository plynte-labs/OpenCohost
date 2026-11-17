"""In-app Ollama model download manager (ADR-063, model-download-manager T2).

One job at a time streams Ollama's ``/api/pull`` on its own daemon thread,
completely outside the engine command queue, so a download never blocks or
delays a turn.

State machine::

    queued -> downloading -> completed
                 |   ^  \\-> failed      (non-retryable error, or retries exhausted)
                 |   |   \\-> retrying -> downloading   (bounded backoff)
                 v   |
               paused ----> (resume re-pulls)
    any non-terminal -> cancelled;  failed -> (resume = manual retry)

Pause and cancel close the pull stream. Ollama keeps its ``*-partial`` blobs
and a re-pull resumes from them (verified in T1 against Ollama 0.30.11), so
resume is simply a new pull; progress after a resume always comes from
Ollama's own chunks, never from the pre-pause numbers -- after a crash Ollama
may resume from an earlier checkpoint than the last value the UI showed.
Cancel does NOT delete partials: Ollama exposes no API for that.

Only ONE job may be active (queued/downloading/retrying). A second ``start``
raises ``ActiveDownloadError`` (HTTP 409) rather than queueing: a hidden
queue of multi-GB downloads is surprising on a metered/unstable link, and
the UI can simply offer the next download when the current one finishes.
``start`` for a tag that already has a paused/failed job resumes that job.

The job record (``MODEL_DOWNLOADS_FILE``) is written atomically on every
transition and at most every ``PERSIST_INTERVAL_SECONDS`` during progress. A
corrupt or unreadable record fails open (empty history). On load, a job that
was active when the process died surfaces as ``paused`` with
``interrupted=True``: a large download never restarts without the user.

Privacy: logs and the wire carry the model tag, numbers, state, the error
category and the exception TYPE only -- never exception text, which may
embed upstream URLs or local paths.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional

from opencohost.config.settings import _canonical_model_tag
from opencohost.config.storage import USER_DATA_DIR, atomic_write_text
from opencohost.core.model_downloads.tags import is_valid_model_tag

logger = logging.getLogger("opencohost.model_downloads")

MODEL_DOWNLOADS_FILE = os.path.join(str(USER_DATA_DIR), "config", "model_downloads.json")
RECORD_VERSION = 1
MAX_JOBS_KEPT = 10

# Bounded auto-retry for network/transport failures: MAX_RETRIES retries after
# the first attempt, waiting RETRY_BACKOFF_SECONDS[i] before retry i+1. The
# budget resets whenever an attempt makes byte progress, so a long download
# over a flaky link keeps going as long as each attempt moves forward.
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = (2.0, 8.0, 30.0)

SPEED_EMA_ALPHA = 0.3
SPEED_MIN_SAMPLE_SECONDS = 0.5
PERSIST_INTERVAL_SECONDS = 5.0
RESUME_JOIN_TIMEOUT_SECONDS = 5.0

# Pull transport: connect fast, but let a slow registry hand over chunks.
# A read stall longer than PULL_READ_TIMEOUT_SECONDS is a transport error and
# goes through the retry path.
PULL_CONNECT_TIMEOUT_SECONDS = 10.0
PULL_READ_TIMEOUT_SECONDS = 60.0

STATE_QUEUED = "queued"
STATE_DOWNLOADING = "downloading"
STATE_PAUSED = "paused"
STATE_RETRYING = "retrying"
STATE_COMPLETED = "completed"
STATE_FAILED = "failed"
STATE_CANCELLED = "cancelled"

ALL_STATES = frozenset(
    {STATE_QUEUED, STATE_DOWNLOADING, STATE_PAUSED, STATE_RETRYING, STATE_COMPLETED, STATE_FAILED, STATE_CANCELLED}
)
ACTIVE_STATES = frozenset({STATE_QUEUED, STATE_DOWNLOADING, STATE_RETRYING})
TERMINAL_STATES = frozenset({STATE_COMPLETED, STATE_CANCELLED})
RESUMABLE_STATES = frozenset({STATE_PAUSED, STATE_FAILED})

# Motor-event names the host whitelists for GET /api/events (engine_host.py).
EVENT_START = "download_start"
EVENT_DONE = "download_done"
EVENT_ERROR = "download_error"

_NETWORK_HINTS = (
    "max retries exceeded",
    "connection reset",
    "connection refused",
    "timeout",
    "timed out",
    "eof",
    "tls",
    "i/o timeout",
    "no such host",
    "network",
    "broken pipe",
)
_NOT_FOUND_HINTS = ("file does not exist", "not found", "manifest unknown")
_DISK_HINTS = ("no space left", "disk full", "not enough space")


class ActiveDownloadError(Exception):
    """Another job is already active (HTTP 409)."""

    def __init__(self, active_job_id: Optional[str]) -> None:
        super().__init__("download_active")
        self.active_job_id = active_job_id


class JobNotFoundError(Exception):
    """Unknown job id (HTTP 404)."""


class InvalidTransitionError(Exception):
    """The requested action is not valid in the job's state (HTTP 409)."""


class _StreamEndedError(Exception):
    """The pull stream closed before Ollama reported ``success``."""


def classify_error(exc: BaseException) -> tuple[str, bool]:
    """Map a pull failure to ``(category, retryable)``.

    Categories: ``network`` (retryable), ``not_found``, ``rejected``,
    ``disk_full``, ``pull_error``, ``internal``.
    """
    try:
        import httpx

        if isinstance(exc, httpx.TransportError):
            return ("network", True)
    except ImportError:  # pragma: no cover - httpx is a hard dependency
        pass
    if isinstance(exc, _StreamEndedError):
        return ("network", True)
    if type(exc).__name__ == "ResponseError" and hasattr(exc, "status_code"):
        status = getattr(exc, "status_code", -1) or -1
        message = str(getattr(exc, "error", "") or exc).lower()
        if status == 404 or any(h in message for h in _NOT_FOUND_HINTS):
            return ("not_found", False)
        if any(h in message for h in _DISK_HINTS):
            return ("disk_full", False)
        if 400 <= status < 500:
            return ("rejected", False)
        if status >= 500 or any(h in message for h in _NETWORK_HINTS):
            return ("network", True)
        return ("pull_error", False)
    if isinstance(exc, (ConnectionError, TimeoutError)):
        return ("network", True)
    return ("internal", False)


class _OllamaPullStream:
    """Closable ``/api/pull`` stream through the ollama client.

    ``ollama.Client`` resolves the host from ``OLLAMA_HOST`` exactly like
    every other Ollama call in the app. ``abort()`` closes the underlying
    httpx client from the control thread -- best effort, so a pause/cancel
    need not wait for the next chunk of a stalled stream (the worker also
    checks its stop event between chunks either way).
    """

    def __init__(self, tag: str) -> None:
        import httpx
        import ollama

        self._tag = tag
        self._client = ollama.Client(
            timeout=httpx.Timeout(PULL_CONNECT_TIMEOUT_SECONDS, read=PULL_READ_TIMEOUT_SECONDS)
        )
        self._gen = None

    def __iter__(self):
        self._gen = self._client.pull(self._tag, stream=True)
        for part in self._gen:
            if isinstance(part, dict):
                yield part
            else:
                yield {
                    "status": getattr(part, "status", None),
                    "digest": getattr(part, "digest", None),
                    "total": getattr(part, "total", None),
                    "completed": getattr(part, "completed", None),
                }

    def close(self) -> None:
        if self._gen is not None:
            try:
                self._gen.close()
            except Exception:
                pass
        self.abort()

    def abort(self) -> None:
        try:
            self._client._client.close()
        except Exception:
            pass


def default_pull_stream(tag: str) -> Iterable[dict]:
    return _OllamaPullStream(tag)


def _default_wait(stop_event: threading.Event, seconds: float) -> bool:
    """Sleep up to *seconds*; True when interrupted by pause/cancel/shutdown."""
    return stop_event.wait(seconds)


@dataclass
class _Job:
    id: str
    tag: str
    state: str
    created_at: float
    updated_at: float
    total_bytes: Optional[int] = None
    completed_bytes: Optional[int] = None
    phase: Optional[str] = None
    speed_bps: Optional[float] = None
    eta_seconds: Optional[float] = None
    attempt: int = 0
    retries_used: int = 0
    error_category: Optional[str] = None
    error_type: Optional[str] = None
    interrupted: bool = False
    installed: Optional[bool] = None
    # Per-attempt transient state (never persisted).
    layers: dict = field(default_factory=dict)
    speed_sample: Optional[tuple] = None

    _PERSISTED = (
        "id", "tag", "state", "created_at", "updated_at", "total_bytes", "completed_bytes",
        "phase", "attempt", "retries_used", "error_category", "error_type", "interrupted", "installed",
    )

    def percent(self) -> Optional[float]:
        if not self.total_bytes or self.completed_bytes is None:
            return None
        return round(min(100.0, 100.0 * self.completed_bytes / self.total_bytes), 2)

    def to_public(self) -> dict:
        data = {key: getattr(self, key) for key in self._PERSISTED}
        data["percent"] = self.percent()
        data["speed_bps"] = self.speed_bps
        data["eta_seconds"] = self.eta_seconds
        return data

    def to_record(self) -> dict:
        return {key: getattr(self, key) for key in self._PERSISTED}

    @classmethod
    def from_record(cls, raw: Any) -> Optional["_Job"]:
        if not isinstance(raw, dict):
            return None
        job_id, tag, state = raw.get("id"), raw.get("tag"), raw.get("state")
        if not isinstance(job_id, str) or not job_id or len(job_id) > 64:
            return None
        if not is_valid_model_tag(tag) or state not in ALL_STATES:
            return None

        def _int(key):
            value = raw.get(key)
            return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None

        def _num(key, default):
            value = raw.get(key)
            return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else default

        def _str(key):
            value = raw.get(key)
            return value if isinstance(value, str) and len(value) <= 64 else None

        job = cls(
            id=job_id,
            tag=tag,
            state=state,
            created_at=_num("created_at", 0.0),
            updated_at=_num("updated_at", 0.0),
            total_bytes=_int("total_bytes"),
            completed_bytes=_int("completed_bytes"),
            phase=_str("phase"),
            attempt=_int("attempt") or 0,
            retries_used=_int("retries_used") or 0,
            error_category=_str("error_category"),
            error_type=_str("error_type"),
            interrupted=raw.get("interrupted") is True,
            installed=raw.get("installed") if isinstance(raw.get("installed"), bool) else None,
        )
        if job.state in ACTIVE_STATES:
            # The process died mid-download: offer Resume, never auto-restart.
            job.state = STATE_PAUSED
            job.interrupted = True
        return job


@dataclass
class _Run:
    job_id: str
    stop: threading.Event
    thread: Optional[threading.Thread] = None
    stream: Any = None


class ModelDownloadManager:
    """Owns the download jobs. Thread-safe; every public method is non-blocking
    except ``join``/``shutdown`` (bounded) and ``resume`` (bounded join of a
    previous worker that is still closing its stream)."""

    def __init__(
        self,
        record_path: Any = None,
        *,
        pull_fn: Optional[Callable[[str], Iterable[dict]]] = None,
        discover_fn: Optional[Callable[[], Iterable[str]]] = None,
        on_event: Optional[Callable[[str], None]] = None,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        wait_fn: Callable[[threading.Event, float], bool] = _default_wait,
    ) -> None:
        self._record_path = str(record_path) if record_path is not None else MODEL_DOWNLOADS_FILE
        self._pull_fn = pull_fn
        self._discover_fn = discover_fn
        self._on_event = on_event
        self._clock = clock
        self._wall_clock = wall_clock
        self._wait = wait_fn
        self._lock = threading.RLock()
        self._jobs: list[_Job] = []  # newest first
        self._run: Optional[_Run] = None
        self._last_persist = 0.0
        self._load()

    # ── public API ──────────────────────────────────────────────────────

    def list_jobs(self) -> list[dict]:
        with self._lock:
            return [job.to_public() for job in self._jobs]

    def get_job(self, job_id: str) -> dict:
        with self._lock:
            return self._find(job_id).to_public()

    def active_job_id(self) -> Optional[str]:
        with self._lock:
            active = self._active_job()
            return active.id if active else None

    def start(self, tag: str) -> dict:
        if not is_valid_model_tag(tag):
            raise ValueError("invalid_tag")
        with self._lock:
            active = self._active_job()
            if active is not None:
                raise ActiveDownloadError(active.id)
            resumable = next(
                (job.id for job in self._jobs if job.tag == tag and job.state in RESUMABLE_STATES),
                None,
            )
        if resumable is not None:
            # Outside the lock: resume may briefly join a closing worker.
            return self.resume(resumable)
        with self._lock:
            active = self._active_job()
            if active is not None:
                raise ActiveDownloadError(active.id)
            now = self._wall_clock()
            job = _Job(id=uuid.uuid4().hex[:16], tag=tag, state=STATE_QUEUED, created_at=now, updated_at=now)
            self._jobs.insert(0, job)
            del self._jobs[MAX_JOBS_KEPT:]
            self._launch(job)
            return job.to_public()

    def pause(self, job_id: str) -> dict:
        with self._lock:
            job = self._find(job_id)
            if job.state not in ACTIVE_STATES:
                raise InvalidTransitionError(job.state)
            self._stop_run(job.id)
            self._set_state(job, STATE_PAUSED)
            self._log_state(job)
            return job.to_public()

    def resume(self, job_id: str) -> dict:
        with self._lock:
            job = self._find(job_id)
            if job.state not in RESUMABLE_STATES:
                raise InvalidTransitionError(job.state)
            active = self._active_job()
            if active is not None:
                raise ActiveDownloadError(active.id)
            previous = self._run
        if previous is not None and previous.thread is not None and previous.thread is not threading.current_thread():
            previous.thread.join(RESUME_JOIN_TIMEOUT_SECONDS)
            if previous.thread.is_alive():
                raise InvalidTransitionError("stream_closing")
        with self._lock:
            if job.state not in RESUMABLE_STATES:
                raise InvalidTransitionError(job.state)
            job.retries_used = 0
            job.error_category = None
            job.error_type = None
            job.interrupted = False
            self._set_state(job, STATE_QUEUED)
            self._launch(job)
            return job.to_public()

    def cancel(self, job_id: str) -> dict:
        with self._lock:
            job = self._find(job_id)
            if job.state in TERMINAL_STATES:
                raise InvalidTransitionError(job.state)
            self._stop_run(job.id)
            job.speed_bps = None
            job.eta_seconds = None
            self._set_state(job, STATE_CANCELLED)
            self._log_state(job)
            return job.to_public()

    def join(self, timeout: Optional[float] = None) -> bool:
        """Wait for the current worker thread; True when none is running."""
        with self._lock:
            run = self._run
        thread = run.thread if run else None
        if thread is None or thread is threading.current_thread():
            return True
        thread.join(timeout)
        return not thread.is_alive()

    def shutdown(self, timeout: Optional[float] = 2.0) -> None:
        """App exit: stop the active pull and record it as an interrupted pause."""
        with self._lock:
            job = self._active_job()
            if job is not None:
                self._stop_run(job.id)
                job.interrupted = True
                job.speed_bps = None
                job.eta_seconds = None
                self._set_state(job, STATE_PAUSED)
                self._log_state(job)
        self.join(timeout)

    # ── worker ──────────────────────────────────────────────────────────

    def _launch(self, job: _Job) -> None:
        run = _Run(job_id=job.id, stop=threading.Event())
        run.thread = threading.Thread(
            target=self._worker, args=(job, run), name="model-download", daemon=True
        )
        self._run = run
        self._emit(EVENT_START)
        self._log_state(job)
        run.thread.start()

    def _pull(self, tag: str):
        pull_fn = self._pull_fn
        if pull_fn is None:
            # Resolved at call time so tests can replace the module attribute.
            pull_fn = globals()["default_pull_stream"]
        return pull_fn(tag)

    def _worker(self, job: _Job, run: _Run) -> None:
        while True:
            with self._lock:
                if run.stop.is_set():
                    return
                job.attempt += 1
                job.layers = {}
                job.speed_sample = None
                job.speed_bps = None
                job.eta_seconds = None
                self._set_state(job, STATE_DOWNLOADING)
            try:
                succeeded = self._consume(job, run)
                if succeeded is None:
                    return  # stopped by pause/cancel/shutdown
                if succeeded:
                    self._complete(job, run)
                    return
                raise _StreamEndedError()
            except Exception as exc:  # noqa: BLE001 - classified here, never re-raised
                delay = self._on_attempt_failed(job, run, exc)
            finally:
                self._close_stream(run)
            if delay is None or self._wait(run.stop, delay):
                return

    def _on_attempt_failed(self, job: _Job, run: _Run, exc: BaseException) -> Optional[float]:
        """Record a failed attempt. Returns the backoff delay, or None to stop."""
        category, retryable = classify_error(exc)
        with self._lock:
            if run.stop.is_set():
                return None
            job.error_category = category
            job.error_type = type(exc).__name__
            job.speed_bps = None
            job.eta_seconds = None
            if retryable and job.retries_used < MAX_RETRIES:
                delay = RETRY_BACKOFF_SECONDS[min(job.retries_used, len(RETRY_BACKOFF_SECONDS) - 1)]
                job.retries_used += 1
                self._set_state(job, STATE_RETRYING)
                logger.warning(
                    "[MODEL_DL] retry tag=%s error_type=%s category=%s retry=%d/%d delay_s=%.0f",
                    job.tag, job.error_type, category, job.retries_used, MAX_RETRIES, delay,
                )
                return delay
            self._set_state(job, STATE_FAILED)
            logger.warning(
                "[MODEL_DL] failed tag=%s error_type=%s category=%s attempt=%d",
                job.tag, job.error_type, category, job.attempt,
            )
            self._emit(EVENT_ERROR)
            return None

    def _consume(self, job: _Job, run: _Run) -> Optional[bool]:
        """Apply chunks until success/end. None when stopped, else success flag."""
        stream = self._pull(job.tag)
        with self._lock:
            run.stream = stream
            if run.stop.is_set():
                return None
        success = False
        for chunk in stream:
            with self._lock:
                if run.stop.is_set():
                    return None
                if self._apply_chunk(job, chunk):
                    success = True
        with self._lock:
            if run.stop.is_set():
                return None
        return success

    def _apply_chunk(self, job: _Job, chunk: Any) -> bool:
        if not isinstance(chunk, dict):
            return False
        status = str(chunk.get("status") or "")
        lowered = status.lower()
        if lowered == "success":
            job.phase = "success"
            return True
        digest, total, completed = chunk.get("digest"), chunk.get("total"), chunk.get("completed")
        if lowered.startswith("pulling") and isinstance(digest, str) and isinstance(total, int) and total > 0:
            job.phase = "pulling"
            done = completed if isinstance(completed, int) and completed >= 0 else 0
            job.layers[digest] = (total, min(done, total))
            before = job.completed_bytes
            job.total_bytes = sum(t for t, _ in job.layers.values())
            job.completed_bytes = sum(c for _, c in job.layers.values())
            if before is not None and job.completed_bytes > before and job.retries_used:
                job.retries_used = 0  # the link recovered: fresh retry budget
            self._update_speed(job)
            self._touch(job, progress=True)
        elif lowered.startswith("pulling"):
            job.phase = "manifest" if "manifest" in lowered else "pulling"
        elif lowered.startswith("verifying"):
            job.phase = "verifying"
            self._touch(job)
        elif lowered.startswith("writing"):
            job.phase = "writing"
            self._touch(job)
        return False

    def _update_speed(self, job: _Job) -> None:
        now = self._clock()
        completed = job.completed_bytes or 0
        sample = job.speed_sample
        if sample is None:
            job.speed_sample = (now, completed)
        else:
            then, before = sample
            elapsed = now - then
            delta = completed - before
            if delta < 0:
                job.speed_sample = (now, completed)
            elif elapsed >= SPEED_MIN_SAMPLE_SECONDS:
                instant = delta / elapsed
                job.speed_bps = (
                    instant if job.speed_bps is None
                    else SPEED_EMA_ALPHA * instant + (1 - SPEED_EMA_ALPHA) * job.speed_bps
                )
                job.speed_sample = (now, completed)
        if job.speed_bps and job.speed_bps > 0 and job.total_bytes:
            job.eta_seconds = max(0.0, (job.total_bytes - completed) / job.speed_bps)
        else:
            job.eta_seconds = None

    def _complete(self, job: _Job, run: _Run) -> None:
        installed: Optional[bool] = None
        if self._discover_fn is not None:
            try:
                discovered = {_canonical_model_tag(t) for t in self._discover_fn() or []}
                installed = _canonical_model_tag(job.tag) in discovered
            except Exception as exc:  # noqa: BLE001 - discovery is best-effort
                logger.warning("[MODEL_DL] discovery_refresh_failed tag=%s error_type=%s", job.tag, type(exc).__name__)
        with self._lock:
            if run.stop.is_set():
                return
            job.installed = installed
            job.error_category = None
            job.error_type = None
            job.speed_bps = None
            job.eta_seconds = 0.0
            if job.total_bytes:
                job.completed_bytes = job.total_bytes
            self._set_state(job, STATE_COMPLETED)
            self._log_state(job)
            self._emit(EVENT_DONE)

    def _close_stream(self, run: _Run) -> None:
        with self._lock:
            stream, run.stream = run.stream, None
        close = getattr(stream, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass

    def _stop_run(self, job_id: str) -> None:
        run = self._run
        if run is None or run.job_id != job_id:
            return
        run.stop.set()
        abort = getattr(run.stream, "abort", None)
        if callable(abort):
            try:
                abort()
            except Exception:
                pass

    # ── bookkeeping ─────────────────────────────────────────────────────

    def _find(self, job_id: str) -> _Job:
        for job in self._jobs:
            if job.id == job_id:
                return job
        raise JobNotFoundError(job_id)

    def _active_job(self) -> Optional[_Job]:
        for job in self._jobs:
            if job.state in ACTIVE_STATES:
                return job
        return None

    def _set_state(self, job: _Job, state: str) -> None:
        job.state = state
        self._touch(job, force=True)

    def _touch(self, job: _Job, *, progress: bool = False, force: bool = False) -> None:
        job.updated_at = self._wall_clock()
        now = self._clock()
        if force or not progress or now - self._last_persist >= PERSIST_INTERVAL_SECONDS:
            self._persist()

    def _persist(self) -> None:
        self._last_persist = self._clock()
        payload = {"version": RECORD_VERSION, "jobs": [job.to_record() for job in self._jobs]}
        try:
            atomic_write_text(self._record_path, json.dumps(payload, indent=2))
        except Exception as exc:  # noqa: BLE001 - persistence is best-effort
            logger.warning("[MODEL_DL] record_write_failed error_type=%s", type(exc).__name__)

    def _load(self) -> None:
        try:
            with open(self._record_path, encoding="utf-8") as handle:
                data = json.load(handle)
        except FileNotFoundError:
            return
        except Exception as exc:  # noqa: BLE001 - corrupt record fails open
            logger.warning("[MODEL_DL] record_unreadable error_type=%s", type(exc).__name__)
            return
        raw_jobs = data.get("jobs") if isinstance(data, dict) else None
        if not isinstance(raw_jobs, list):
            logger.warning("[MODEL_DL] record_unreadable error_type=shape")
            return
        seen: set[str] = set()
        for raw in raw_jobs[:MAX_JOBS_KEPT]:
            job = _Job.from_record(raw)
            if job is None or job.id in seen:
                continue
            seen.add(job.id)
            self._jobs.append(job)
            if job.interrupted and job.state == STATE_PAUSED:
                self._log_state(job)

    def _log_state(self, job: _Job) -> None:
        logger.info(
            "[MODEL_DL] state=%s tag=%s completed=%s total=%s attempt=%d",
            job.state, job.tag, job.completed_bytes, job.total_bytes, job.attempt,
        )

    def _emit(self, event: str) -> None:
        if self._on_event is None:
            return
        try:
            self._on_event(event)
        except Exception as exc:  # noqa: BLE001 - event sinks never break a download
            logger.warning("[MODEL_DL] event_sink_failed error_type=%s", type(exc).__name__)
