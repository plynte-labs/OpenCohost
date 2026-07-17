"""Headless server-side Push-to-Talk session (liveaudio_ptt_tauri_20260710).

A recv-only WebSocket consumer of an EXTERNAL WhisperLive-style STT server.
WhisperLive captures the mic ITSELF and pushes transcript JSON ``{"text": ...}``
to any connected client; this session only ever calls ``recv()`` — zero audio
bytes cross Python, Rust, or the webview (see the CTK ``voice_control.py``
verdict: ``_ws_listener`` never sends, no sounddevice/pyaudio anywhere).

COMPOSITION, not extraction: the CTK ``voice_control.py`` / ``ptt_manager.py``
classes are NOT touched. This is a ~200-line headless port that mirrors their
PTT buffer + 5s-grace-flush + missed-key-up "guillotine" behaviour for the
FastAPI sidecar, so the Tauri client gets full parity with zero CTK regression
risk.

PRIVACY (hard rule 2): the operator transcript is host speech. It travels
WhisperLive WS -> :pyattr:`PttSession._buffer` (RAM, under lock) -> the
``on_flush`` dispatch callback (the R8-vetted ``process_context`` path shared
with ``POST /api/chat/turn``) -> gone. It NEVER crosses HTTP and is NEVER
logged: this module logs char counts and state transitions ONLY — no ``[:30]``
previews (deliberately stricter than CTK's truncated previews).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import time
import uuid
from typing import Callable, Optional

import websockets

from opencohost.i18n import active as i18n_active

logger = logging.getLogger("opencohost.api.ptt_session")

# Anti-Loop Whisper filter, copied verbatim from voice_control.py:456 — collapse
# a phrase/word repeated 3+ times in a row down to one occurrence. WhisperLive
# emits these stutter artifacts; CTK sanitizes them the same way.
_ANTILOOP_RE = re.compile(r"\b(.+?)(?:\s+\1\b){2,}", flags=re.IGNORECASE)

# Session lifecycle states. `last_error` (stt_unreachable|stt_lost) rides the
# state responses; it is never a sticky state — every exit path converges on
# idle.
_IDLE = "idle"
_CONNECTING = "connecting"
_LISTENING = "listening"
_FLUSHING = "flushing"

# Minimum words before a flush is dispatched as a turn (CTK parity,
# voice_control.py:232-235). Under two words -> honest no-op, no LLM call.
_MIN_WORDS = 2


class PttUnreachable(Exception):
    """Raised by :meth:`PttSession.start` when the WhisperLive WS connect fails
    within ``ws_open_timeout``. The caller frees the slot and returns an honest
    503 ``stt_unreachable`` — the session never fake-listens against an absent
    server."""


class SessionActive(Exception):
    """Raised by :meth:`PttController.start` when a session already exists
    (listening OR flushing). One operator, one mic, one session -> 409."""


class PttSession:
    """One recv-only WhisperLive WS connection with buffer + grace + watchdog.

    Timings are constructor params (with production defaults) so tests inject
    tiny values and never need a real STT server or real 5s waits.

    Callbacks (all invoked from the WS thread, all must be thread-safe):
      - ``on_flush(text)``  : dispatch the buffered dictation as ONE turn.
                              Called only when the buffer holds >= 2 words.
      - ``on_event(action)``: record a lifecycle event. ``action`` is one of the
                              fixed literals started|stopped|auto_stopped|
                              flushed|empty|error|buffer_full. NEVER carries
                              transcript text.
      - ``on_close(err)``   : (optional) the slot is now free; ``err`` is the
                              final ``last_error`` (None on a clean stop).
    """

    def __init__(
        self,
        ws_uri: str,
        on_flush: Callable[[str], None],
        on_event: Callable[[str], None],
        *,
        on_close: Optional[Callable[[Optional[str]], None]] = None,
        grace: float = 5.0,
        keepalive_timeout: float = 3.0,
        watchdog_tick: float = 0.5,
        ws_open_timeout: float = 2.0,
        max_chars: int = 2000,
    ) -> None:
        self._ws_uri = ws_uri
        self._on_flush = on_flush
        self._on_event = on_event
        self._on_close = on_close
        self._grace = grace
        self._keepalive_timeout = keepalive_timeout
        self._watchdog_tick = watchdog_tick
        self._ws_open_timeout = ws_open_timeout
        self._max_chars = max_chars

        self.session_id = f"ptt_{uuid.uuid4().hex}"

        self._lock = threading.Lock()
        self._state = _IDLE
        self._buffer = ""
        self._grace_deadline = 0.0
        self._last_keepalive = 0.0
        self._last_error: Optional[str] = None
        self._cap_emitted = False

        self._thread: Optional[threading.Thread] = None
        self._connected_evt = threading.Event()
        self._connect_ok = False

    # ------------------------------------------------------------------
    # Thread-safe read surface (HTTP handlers touch these from other threads)
    # ------------------------------------------------------------------

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    @property
    def buffered_chars(self) -> int:
        """Int count ONLY — never the text (privacy)."""
        with self._lock:
            return len(self._buffer)

    @property
    def last_error(self) -> Optional[str]:
        with self._lock:
            return self._last_error

    # ------------------------------------------------------------------
    # Lifecycle control (called from HTTP handler threads)
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Spawn the WS thread and BLOCK until the connect result is known.

        Returns on success (state == listening). Raises :class:`PttUnreachable`
        if the connect fails within ``ws_open_timeout`` — an honest immediate
        error, never a perpetual reconnect loop against an absent server.
        """
        with self._lock:
            self._state = _CONNECTING
            self._cap_emitted = False
        self._thread = threading.Thread(target=self._run, name="ptt-ws", daemon=True)
        self._thread.start()
        # +2s margin over the WS open timeout for thread spin-up / GC stalls.
        if not self._connected_evt.wait(self._ws_open_timeout + 2.0):
            with self._lock:
                self._state = _IDLE
                self._last_error = "stt_unreachable"
            raise PttUnreachable()
        if not self._connect_ok:
            raise PttUnreachable()

    def keepalive(self) -> None:
        """Refresh the proof-of-life timestamp (the client posts this ~1 Hz
        while the key is held)."""
        with self._lock:
            self._last_keepalive = time.monotonic()

    def stop(self) -> None:
        """Cut an active listening session: begin the grace period, return
        immediately. The background WS loop flushes at the deadline. Idempotent
        — a stop on a connecting/flushing/idle session is a no-op."""
        self._begin_grace("stopped")

    # ------------------------------------------------------------------
    # WS thread
    # ------------------------------------------------------------------

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._ws_main())
        except Exception:
            logger.exception("PTT WS thread crashed")
            if not self._connected_evt.is_set():
                with self._lock:
                    self._state = _IDLE
                    self._last_error = "stt_unreachable"
                self._connect_ok = False
                self._connected_evt.set()
        finally:
            loop.close()

    async def _ws_main(self) -> None:
        connected = False
        try:
            async with websockets.connect(
                self._ws_uri,
                open_timeout=self._ws_open_timeout,
                ping_interval=20,
                ping_timeout=10,
                close_timeout=5,
            ) as ws:
                connected = True
                with self._lock:
                    self._state = _LISTENING
                    self._last_keepalive = time.monotonic()
                self._connect_ok = True
                self._connected_evt.set()
                self._emit("started")
                # Runs listening + grace; returns with the WS still open so a
                # segment arriving during grace can extend the deadline.
                await self._recv_loop(ws)
                # Flush while the WS is still open (CTK ordering parity), THEN
                # the context manager closes it below.
                self._flush_buffer()
        except Exception:
            if not connected:
                # Connect failure -> honest stt_unreachable, slot freed by caller.
                with self._lock:
                    self._state = _IDLE
                    self._last_error = "stt_unreachable"
                self._connect_ok = False
                self._connected_evt.set()
                return
            logger.exception("PTT WS session error after connect")
            # Best-effort deliver whatever we heard before the failure.
            self._flush_buffer()
        with self._lock:
            self._state = _IDLE
        self._notify_close()

    async def _recv_loop(self, ws) -> None:
        """Drain segments until the grace period expires (or the WS drops).

        The recv is wrapped in ``wait_for(..., timeout=watchdog_tick)`` so the
        loop wakes every tick even in silence — that tick IS the server-side
        guillotine cadence (keepalive starvation -> synthesized auto-stop).
        """
        while True:
            now = time.monotonic()
            with self._lock:
                state = self._state
                deadline = self._grace_deadline
                last_ka = self._last_keepalive
            # Watchdog: a held session REQUIRES the keepalive stream. Starvation
            # (client crash, dropped pointer-up, alt-tab, network drop, app kill)
            # -> synthesize a stop through the SAME grace+flush path. The buffer
            # is DELIVERED, not discarded (exact CTK missed-key-up parity).
            if state == _LISTENING and (now - last_ka) > self._keepalive_timeout:
                self._begin_grace("auto_stopped")
                continue
            # Grace expired -> return so the caller flushes + closes.
            if state == _FLUSHING and now >= deadline:
                return
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=self._watchdog_tick)
            except asyncio.TimeoutError:
                continue
            except (websockets.exceptions.ConnectionClosed, ConnectionError, OSError):
                # WS drop mid-listening: no in-session reconnect (deliver what
                # was heard). Mark stt_lost, emit error, return to flush.
                with self._lock:
                    listening = self._state == _LISTENING
                if listening:
                    self._begin_grace("error", last_error="stt_lost")
                return
            self._ingest(msg)

    # ------------------------------------------------------------------
    # Buffer + flush
    # ------------------------------------------------------------------

    def _ingest(self, msg) -> None:
        try:
            data = json.loads(msg)
        except (json.JSONDecodeError, TypeError, ValueError):
            return
        if not isinstance(data, dict):
            return
        text = (data.get("text") or "").strip()
        if not text:
            return
        text = _ANTILOOP_RE.sub(r"\1", text).strip()
        if not text:
            return
        emit_cap = False
        with self._lock:
            if len(self._buffer) >= self._max_chars:
                # Soft cap reached (CTK _ptt_max_chars parity) — drop the rest.
                logger.warning("[PTT] buffer cap %d reached, dropping segment", self._max_chars)
                if not self._cap_emitted:
                    self._cap_emitted = True
                    emit_cap = True
                cap_hit = True
            else:
                cap_hit = False
                self._buffer = text if not self._buffer else self._buffer + " " + text
                # A segment arriving during grace extends the deadline (CTK
                # voice_control.py:500-501 parity).
                if self._state == _FLUSHING:
                    self._grace_deadline = time.monotonic() + self._grace
                buffered = len(self._buffer)
        if emit_cap:
            # Once per instance (guarded by self._cap_emitted above), emitted
            # OUTSIDE the lock — same convention as _begin_grace/_flush_buffer.
            self._emit("buffer_full")
        if cap_hit:
            return
        logger.debug("[PTT] buffered %d chars", buffered)

    def _flush_buffer(self) -> None:
        with self._lock:
            text = self._buffer.strip()
            self._buffer = ""
        words = text.split()
        if len(words) < _MIN_WORDS:
            # Honest no-op: under two words is not a turn. No LLM call.
            self._emit("empty")
            return
        logger.info("[PTT] flushing %d chars as one turn", len(text))
        try:
            self._on_flush(text)
        except Exception:
            logger.exception("PTT flush dispatch failed")
        self._emit("flushed")

    def _begin_grace(self, action: str, last_error: Optional[str] = None) -> None:
        """Transition listening -> flushing exactly once (first caller wins).

        Explicit stop, watchdog auto-stop, and WS drop all route here; a double
        call (retry / watchdog race / double-guillotine) is a safe no-op."""
        emit = False
        with self._lock:
            if self._state == _LISTENING:
                self._state = _FLUSHING
                self._grace_deadline = time.monotonic() + self._grace
                if last_error:
                    self._last_error = last_error
                emit = True
        if emit:
            self._emit(action)

    def _emit(self, action: str) -> None:
        try:
            self._on_event(action)
        except Exception:
            logger.exception("PTT event emit failed")

    def _notify_close(self) -> None:
        if self._on_close is None:
            return
        try:
            self._on_close(self._last_error)
        except Exception:
            logger.exception("PTT close callback failed")


class PttController:
    """Single-slot owner of the one live :class:`PttSession` (one operator, one
    mic). Wired in ``main.py`` create_app with the app dispatcher + the host
    event log. Composes the session with two callbacks:

      - flush -> ``dispatcher.dispatch("process_context", ptt_wrapper(text))``
        (the SAME R8-vetted path as POST /api/chat/turn — already verified not
        to persist the trigger text).
      - event -> ``event_log.record("ptt", action, None)`` with fixed literal
        actions and ``detail`` ALWAYS None (same closed-set discipline as
        _MOTOR_EVENT_WHITELIST).

    All four HTTP handlers hit this from FastAPI's threadpool, so a lock guards
    the single slot.
    """

    def __init__(
        self,
        ws_uri,
        dispatcher,
        event_log,
        *,
        session_factory=PttSession,
        on_audio_suspect: Optional[Callable[[], None]] = None,
        **session_kwargs,
    ):
        self._ws_uri = ws_uri
        self._dispatcher = dispatcher
        self._event_log = event_log
        self._session_factory = session_factory
        self._session_kwargs = session_kwargs
        # Recovery hook (2026-07-15 PTT voice-death fix): the smallest
        # possible surface into MotorVocalIA — a bound
        # ``motor.mark_audio_suspect`` callback, never the whole engine.
        # Optional so unit tests / minimal host doubles need not supply it.
        self._on_audio_suspect = on_audio_suspect
        self._lock = threading.Lock()
        self._session: Optional[PttSession] = None
        # Survives after the session frees the slot so GET /api/ptt/state can
        # honestly report the last exit reason (e.g. stt_lost). Cleared on the
        # next successful start.
        self._last_error: Optional[str] = None

    def _record_event(self, action: str) -> None:
        # PRIVACY: fixed literal action, detail ALWAYS None — the transcript
        # never reaches here.
        self._event_log.record("ptt", action, None)

    def _dispatch(self, text: str) -> None:
        self._dispatcher.dispatch(
            "process_context", i18n_active.ptt_wrapper().format(text=text)
        )

    def start(self) -> str:
        with self._lock:
            if self._session is not None:
                raise SessionActive()
            session = self._session_factory(
                self._ws_uri,
                on_flush=self._dispatch,
                on_event=self._record_event,
                on_close=self._on_session_closed,
                **self._session_kwargs,
            )
            self._session = session
            self._last_error = None
        try:
            session.start()  # blocks until connect result is known
        except PttUnreachable:
            with self._lock:
                if self._session is session:
                    self._session = None
                self._last_error = "stt_unreachable"
            raise
        return session.session_id

    def keepalive(self, session_id: str) -> Optional[dict]:
        with self._lock:
            session = self._session
            if session is None or session.session_id != session_id or session.state == _IDLE:
                return None
        session.keepalive()
        return {
            "session_id": session.session_id,
            "state": session.state,
            "buffered_chars": session.buffered_chars,
        }

    def stop(self, session_id: Optional[str] = None) -> dict:
        with self._lock:
            session = self._session
            if session is None or (session_id is not None and session_id != session.session_id):
                return {"state": _IDLE}
        session.stop()  # idempotent, returns immediately; watcher flushes
        return {"state": _FLUSHING}

    def state(self) -> dict:
        # stt_ws_url: the LiveAudio viewer WS address (a URL, never text) —
        # present in EVERY state (idle included) because the webview needs it
        # at hold start to open its own recv-only transcript connection.
        with self._lock:
            session = self._session
            if session is None:
                return {
                    "state": _IDLE,
                    "session_id": None,
                    "buffered_chars": 0,
                    "last_error": self._last_error,
                    "stt_ws_url": self._ws_uri,
                }
            return {
                "state": session.state,
                "session_id": session.session_id,
                "buffered_chars": session.buffered_chars,
                "last_error": session.last_error,
                "stt_ws_url": self._ws_uri,
            }

    def _on_session_closed(self, last_error: Optional[str]) -> None:
        with self._lock:
            self._session = None
            self._last_error = last_error
        # Recovery hook: EVERY PTT session close (stopped, auto_stopped, or
        # error all route through PttSession._notify_close -> here) flags the
        # shared pygame mixer suspect, regardless of last_error -- WASAPI
        # endpoint churn from WhisperLive's mic open/close is a silent
        # failure mode with no exception of its own to catch. Called
        # DIRECTLY (not through the queued command channel), so the flag is
        # already set before the NEXT _hablar() turn starts, not applied late
        # after one is already mid-playback.
        if self._on_audio_suspect is not None:
            try:
                self._on_audio_suspect()
            except Exception:
                logger.exception("PTT audio-suspect hook failed")
