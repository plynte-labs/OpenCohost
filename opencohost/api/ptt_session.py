"""Headless server-side Push-to-Talk session (liveaudio_ptt_tauri_20260710).

A recv-only WebSocket consumer of an EXTERNAL WhisperLive-style STT server.
WhisperLive captures the mic ITSELF and pushes transcript JSON ``{"text": ...}``
to any connected client; this session only ever calls ``recv()`` — zero audio
bytes cross Python, Rust, or the webview (see the CTK ``voice_control.py``
verdict: ``_ws_listener`` never sends, no sounddevice/pyaudio anywhere).

COMPOSITION, not extraction: the CTK ``voice_control.py`` / ``ptt_manager.py``
classes are NOT touched. This is a ~200-line headless port that mirrors their
PTT buffer + 1.2s-grace-flush + missed-key-up "guillotine" behaviour for the
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
import os
import re
import threading
import time
import uuid
from typing import Callable, Optional

import websockets

from opencohost.config.logger import get_logger
from opencohost.i18n import active as i18n_active

logger = logging.getLogger("opencohost.api.ptt_session")
# K2 (warmth-guardrails-runtime-20261006): the `opencohost.api.*` tree only
# reaches `opencohost_api.log`, so silent PTT drops were invisible in the
# session log (`logs/opencohost_*.log`). Outcome lines go to the session
# logger instead. COUNTS AND OUTCOMES ONLY -- never transcript text.
_session_log = get_logger()

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

# Upper bound on the post-grace drain for a final STT segment already in
# flight when the grace deadline hits (K2). Hard-capped so the added latency
# can never exceed 500 ms.
_MAX_LATE_DRAIN = 0.5

# A segment arriving during grace pushes the deadline out by at most this much
# (ptt-grace-latency-20261013). The adaptive grace (silence_timeout + margin,
# ~2.1s) exists to outlast the FIRST ASR tail after release; re-arming the full
# value on every arrival compounded the release->flush delay to ~2x grace.
_TAIL_EXTENSION_S = 0.8

# Flush outcome literal for a hold whose words go to the chat composer instead
# of becoming a turn (ptt-dictation-20261111). Fixed literal, never text.
_DICTATED = "dictated"


def _liveaudio_config_path(environ) -> str:
    """Same resolution as LiveAudio's get_data_home() (config/mod.rs)."""
    home = (environ.get("LIVEAUDIO_HOME") or "").strip()
    if home:
        return os.path.join(home, "config.json")
    base = environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "LiveAudio", "config.json")


def _read_silence_timeout(path: str) -> Optional[float]:
    """LiveAudio ``silence_timeout`` or None (missing/invalid/out of range)."""
    from opencohost.config import settings as _s

    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        v = data["silence_timeout"]
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return None
        v = float(v)
    except Exception:
        return None
    lo, hi = _s.LIVEAUDIO_SILENCE_TIMEOUT_RANGE_S
    return v if lo <= v <= hi else None


def _env_float(environ, name: str) -> Optional[float]:
    try:
        v = float(str(environ.get(name, "")).strip())
    except ValueError:
        return None
    return v if v == v and v >= 0 else None


def resolve_ptt_grace(
    environ=None,
    config_path: Optional[str] = None,
    grace_mode: str = "auto",
    grace_s: Optional[float] = None,
):
    """Return ``(grace_s, source, silence_timeout_s)``; never raises.

    source: ``override`` | ``fast_preset`` | ``custom`` | ``liveaudio_config``
    | ``default``. The env var wins over the persisted mode.
    """
    from opencohost.config import settings as _s

    env = os.environ if environ is None else environ
    st = _read_silence_timeout(config_path or _liveaudio_config_path(env))
    source = "liveaudio_config"
    if st is None:
        st, source = _s.LIVEAUDIO_DEFAULT_SILENCE_TIMEOUT_S, "default"
    override = _env_float(env, "OPENCOHOST_PTT_GRACE_S")
    if override is not None:
        return override, "override", st
    if grace_mode == "fast":
        return _s.PTT_FAST_GRACE_S, "fast_preset", st
    if grace_mode == "custom" and _s.is_valid_ptt_grace_s(grace_s):
        return float(grace_s), "custom", st
    margin = _env_float(env, "OPENCOHOST_PTT_ASR_MARGIN_S")
    if margin is None:
        margin = _s.PTT_ASR_MARGIN_S
    grace = max(_s.PTT_GRACE_MIN_S, min(_s.PTT_GRACE_MAX_S, st + margin))
    return grace, source, st


class PttUnreachable(Exception):
    """Raised by :meth:`PttSession.start` when the WhisperLive WS connect fails
    within ``ws_open_timeout``. The caller frees the slot and returns an honest
    503 ``stt_unreachable`` — the session never fake-listens against an absent
    server."""


class SessionActive(Exception):
    """Raised by :meth:`PttController.start` when a session already exists
    (listening OR flushing). One operator, one mic, one session -> 409."""


def probe_stt_ws(uri: str, open_timeout: float = 2.0) -> None:
    """Open a bare WS connection to *uri*, then close it immediately.

    Backs POST /api/ptt/test so the operator can confirm LiveAudio is up
    BEFORE going live, instead of discovering it with a dead F10 hold.

    Deliberately NOT a :class:`PttSession`: no buffer, no flush, no event, no
    single-slot claim, no dispatcher reference. Nothing reachable from here can
    put text on the ``process_context`` path, which is why probing an arbitrary
    URL is inert — the injection surface is the CONFIGURED url, gated by
    ``settings.is_valid_stt_ws_uri``. Safe to run mid-hold.

    Raises whatever the connect raises; the caller decides how to report it.
    """

    async def _open_and_close() -> None:
        async with websockets.connect(uri, open_timeout=open_timeout, close_timeout=2):
            return

    asyncio.run(_open_and_close())


class PttSession:
    """One recv-only WhisperLive WS connection with buffer + grace + watchdog.

    Timings are constructor params (with production defaults) so tests inject
    tiny values and never need a real STT server or real 5s waits. The default
    grace (1.2s) gives adequate margin for local LiveAudio ASR tail chunks
    (~400ms VAD silence + ~300ms Whisper transcribe) without the 5-second
    dead latency.

    Callbacks (all invoked from the WS thread, all must be thread-safe):
      - ``on_flush(text)``  : dispatch the buffered dictation as ONE turn.
                              Called only when the buffer holds >= 2 words.
                              Returning "dictated" means no turn was made
                              (dictation mode); the event is then "dictated"
                              instead of "flushed".
      - ``on_event(action)``: record a lifecycle event. ``action`` is one of the
                              fixed literals started|stopped|auto_stopped|
                              flushed|dictated|empty|error|buffer_full. NEVER
                              carries transcript text.
      - ``on_close(err)``   : (optional) the slot is now free; ``err`` is the
                              final ``last_error`` (None on a clean stop).
      - ``on_release()``    : (optional, interruptible_speech_architecture_
                              20260804 §5.1) fired from `_begin_grace`, the
                              single funnel every exit from `_LISTENING`
                              passes through (explicit stop, keepalive
                              starvation, WS drop) -- clears a PTT-held
                              speech hold at PTT_UP, not after grace.
    """

    def __init__(
        self,
        ws_uri: str,
        on_flush: Callable[[str], None],
        on_event: Callable[[str], None],
        *,
        on_close: Optional[Callable[[Optional[str]], None]] = None,
        on_release: Optional[Callable[[], None]] = None,
        grace: float = 1.2,
        late_drain: float = 0.4,
        tail_extension: float = _TAIL_EXTENSION_S,
        keepalive_timeout: float = 8.0,
        watchdog_tick: float = 0.5,
        ws_open_timeout: float = 2.0,
        max_chars: int = 2000,
    ) -> None:
        self._ws_uri = ws_uri
        self._on_flush = on_flush
        self._on_event = on_event
        self._on_close = on_close
        self._on_release = on_release
        self._grace = grace
        self._tail_extension = max(0.0, min(float(tail_extension), float(grace)))
        self._late_drain = max(0.0, min(float(late_drain), _MAX_LATE_DRAIN))
        self._keepalive_timeout = keepalive_timeout
        self._watchdog_tick = watchdog_tick
        self._ws_open_timeout = ws_open_timeout
        self._max_chars = max_chars

        self.session_id = f"ptt_{uuid.uuid4().hex}"

        self._lock = threading.RLock()
        self._state = _IDLE
        self._buffer = ""
        self._grace_deadline = 0.0
        self._last_keepalive = 0.0
        self._last_error: Optional[str] = None
        self._cap_emitted = False
        self._hold_started = 0.0
        # Timing instrumentation (numbers only): see _timing_fields().
        self._stop_at = 0.0
        self._extensions = 0
        self._late_segments = 0
        self._replays_dropped = 0
        self._replay_max_delay_ms = 0
        self._replays_live_kept = 0
        # Wall clock (LiveAudio's created_at is epoch seconds) at WS connect.
        self._connected_wall = 0.0

        self._thread: Optional[threading.Thread] = None
        self._connected_evt = threading.Event()
        self._connect_ok = False
        self._stop_requested: bool = False

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
        immediately. If the session already left _LISTENING (auto_stopped or error),
        ensure the physical release is still delivered to clear any speech hold."""
        with self._lock:
            listening = self._state == _LISTENING
            if not listening:
                self._stop_requested = True
        if listening:
            self._begin_grace("stopped")
        else:
            if self._on_release is not None:
                try:
                    self._on_release()
                except Exception:
                    logger.exception("PTT on_release hook failed")

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
                    self._hold_started = self._last_keepalive
                    self._connected_wall = time.time()
                self._connect_ok = True
                # Emit BEFORE releasing start(): the "started" event is then
                # always observable once start() returns (the session-log write
                # widened the old set-then-emit race).
                _session_log.info("[PTT] started")
                self._emit("started")
                self._connected_evt.set()
                with self._lock:
                    if self._stop_requested:
                        self._begin_grace("stopped_early")
                        return
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
        finally:
            if connected:
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
            # Grace expired -> drain briefly for a final segment already in
            # flight (K2), then return so the caller flushes + closes.
            if state == _FLUSHING and now >= deadline:
                await self._drain_late_segments(ws)
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

    async def _drain_late_segments(self, ws) -> None:
        """Bounded post-grace drain (K2, warmth-guardrails-runtime-20261006).

        WhisperLive can emit the FINAL segment of an utterance a few hundred ms
        after the grace window closes; the UI echo showed it but Kira never got
        it. Keep receiving for at most ``late_drain`` (<= 500 ms) so a segment
        already in flight is still buffered. Not extended by arrivals: the cap
        is absolute, so added latency is bounded."""
        if self._late_drain <= 0:
            return
        end = time.monotonic() + self._late_drain
        while True:
            remaining = end - time.monotonic()
            if remaining <= 0:
                return
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=min(remaining, self._watchdog_tick))
            except asyncio.TimeoutError:
                continue
            except (websockets.exceptions.ConnectionClosed, ConnectionError, OSError):
                return
            self._ingest(msg)

    # ------------------------------------------------------------------
    # Buffer + flush
    # ------------------------------------------------------------------

    def _ingest(self, msg) -> None:
        # LiveAudio replays recent cues to every late-joining client (and each
        # PTT hold opens a new socket). Those frames carry ``is_replay: true``;
        # they are the previous hold's utterance and must never be dispatched.
        # Frames without the field (older LiveAudio builds) are kept.
        try:
            data = json.loads(msg)
        except (json.JSONDecodeError, TypeError, ValueError):
            return
        if not isinstance(data, dict):
            return
        if data.get("is_replay") is True:
            # LiveAudio also flags delayed LIVE cues (age > 1 s under its auto
            # backlog policy). A cue created after this socket connected is the
            # owner's own speech from this hold: keep it. Older cues (or ones
            # without created_at) are the late-joiner replay burst: drop them.
            # Counted either way, numbers only.
            created = data.get("created_at")
            with self._lock:
                connected_wall = self._connected_wall
            if (
                isinstance(created, (int, float))
                and connected_wall > 0
                and created >= connected_wall
            ):
                with self._lock:
                    self._replays_live_kept += 1
            else:
                delay = data.get("total_delay")
                delay_ms = int(delay * 1000) if isinstance(delay, (int, float)) and delay > 0 else 0
                with self._lock:
                    self._replays_dropped += 1
                    self._replay_max_delay_ms = max(self._replay_max_delay_ms, delay_ms)
                return
        raw_text = data.get("text")
        text = raw_text.strip() if isinstance(raw_text, str) else ""
        if not text and "segments" in data and isinstance(data["segments"], list):
            text = " ".join(
                s["text"].strip()
                for s in data["segments"]
                if isinstance(s, dict) and isinstance(s.get("text"), str) and s["text"].strip()
            ).strip()
        if not text and "transcript" in data:
            raw_tr = data.get("transcript")
            text = raw_tr.strip() if isinstance(raw_tr, str) else ""
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
                    now = time.monotonic()
                    if now < self._grace_deadline:
                        self._extensions += 1
                    else:
                        self._late_segments += 1
                    self._grace_deadline = max(
                        self._grace_deadline, now + self._tail_extension
                    )
                buffered = len(self._buffer)
        if emit_cap:
            # Once per instance (guarded by self._cap_emitted above), emitted
            # OUTSIDE the lock — same convention as _begin_grace/_flush_buffer.
            self._emit("buffer_full")
        if cap_hit:
            return
        logger.debug("[PTT] buffered %d chars", buffered)

    def _timing_fields(self) -> str:
        """Numbers-only release->flush timing for the [PTT] outcome lines."""
        with self._lock:
            stop_at = self._stop_at
            ext = self._extensions
            late = self._late_segments
            replays = self._replays_dropped
            replay_delay = self._replay_max_delay_ms
            live_kept = self._replays_live_kept
        since = int((time.monotonic() - stop_at) * 1000) if stop_at else 0
        return (
            "since_stop_ms=%d grace_ms=%d extensions=%d late_segments=%d "
            "replays_dropped=%d replay_max_delay_ms=%d replays_live_kept=%d"
        ) % (since, int(self._grace * 1000), ext, late, replays, replay_delay, live_kept)

    def _flush_buffer(self) -> None:
        with self._lock:
            text = self._buffer.strip()
            self._buffer = ""
        words = text.split()
        if len(words) < _MIN_WORDS:
            # Honest no-op: under two words is not a turn. No LLM call.
            _session_log.info(
                "[PTT] empty words=%d reason=%s %s",
                len(words),
                "below_min_words" if words else "no_transcript",
                self._timing_fields(),
            )
            self._emit("empty")
            return
        logger.info("[PTT] flushing %d chars as one turn", len(text))
        _session_log.info(
            "[PTT] flushed words=%d chars=%d %s", len(words), len(text), self._timing_fields()
        )
        outcome = None
        try:
            outcome = self._on_flush(text)
        except Exception:
            logger.exception("PTT flush dispatch failed")
        # The callback may report the closed alternative outcome "dictated"
        # (ptt-dictation-20261111): the words went to the composer, not to a
        # turn. Anything else, None included, keeps the "flushed" literal.
        self._emit(_DICTATED if outcome == _DICTATED else "flushed")

    def _begin_grace(self, action: str, last_error: Optional[str] = None) -> None:
        """Transition listening -> flushing exactly once (first caller wins).

        Explicit stop, watchdog auto-stop, and WS drop all route here; a double
        call (retry / watchdog race / double-guillotine) is a safe no-op."""
        emit = False
        with self._lock:
            if self._state == _LISTENING:
                self._state = _FLUSHING
                self._stop_at = time.monotonic()
                self._grace_deadline = self._stop_at + self._grace
                if last_error:
                    self._last_error = last_error
                elif action == "auto_stopped":
                    self._last_error = "keepalive_starvation"
                emit = True
        if emit:
            hold_ms = int((time.monotonic() - self._hold_started) * 1000)
            if action == "error":
                _session_log.warning(
                    "[PTT] error last_error=%s hold_ms=%d", last_error or "unknown", hold_ms
                )
            else:
                _session_log.info("[PTT] %s hold_ms=%d", action, hold_ms)
            self._emit(action)
            # Physical release contract: only explicit "stopped" (PTT_UP) clears the
            # speech hold immediately. Transport anomalies ("auto_stopped", "error")
            # preserve the acoustic hold so Kira does not talk over an open physical press.
            if action == "stopped" and self._on_release is not None:
                try:
                    self._on_release()
                except Exception:
                    logger.exception("PTT on_release hook failed")

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
        on_listening: Optional[Callable[[], None]] = None,
        on_press_precheck: Optional[Callable[[], None]] = None,
        on_release: Optional[Callable[[], None]] = None,
        motor=None,
        grace: Optional[float] = None,
        dictation_mode: str = "send",
        companion_dictation: bool = False,
        grace_mode: str = "auto",
        grace_s: Optional[float] = None,
        focus_clock: Optional[Callable[[], float]] = None,
        **session_kwargs,
    ):
        from opencohost.config import settings as _s

        self._ws_uri = ws_uri
        # Dictation mode (ptt-dictation-20261111). In memory; seeded from
        # ptt_settings.json at boot and live-applied by PUT /api/ptt/config.
        if dictation_mode not in _s.PTT_DICTATION_MODES:
            dictation_mode = _s.PTT_DEFAULT_DICTATION_MODE
        self._dictation_mode = dictation_mode
        # P8: whether a companion hold may dictate (in `always` mode only).
        self._companion_dictation = companion_dictation is True
        # Configurable grace (ptt-live-dictation-20261125); read per press.
        self._grace_mode = grace_mode if grace_mode in _s.PTT_GRACE_MODES else "auto"
        self._grace_s = (
            float(grace_s) if _s.is_valid_ptt_grace_s(grace_s) else _s.PTT_DEFAULT_GRACE_S
        )
        self._focus_stale_s = float(_s.PTT_FOCUS_STALE_S)
        self._focus_clock = focus_clock or time.monotonic
        # Latest main-window focus report: (focused, monotonic_at) or None.
        # Memory only; never persisted.
        self._focus: Optional[tuple] = None
        # Decision locked at the START of a press: (epoch, dictate). Release
        # and flush use it as is (ptt-live-dictation-20261125).
        self._hold_decision: Optional[tuple] = None
        # Press epoch of a hold made from the desktop companion window, which
        # counts as background in `foreground` mode (it never takes focus, so
        # the main window may still report focused while it is used).
        self._background_epoch: Optional[int] = None
        self._dispatcher = dispatcher
        self._event_log = event_log
        # Step 1 (interruptible_speech_architecture_20260804): the motor
        # reference backing the arrival-time drain hook in `_dispatch` below --
        # mirrors `chat.py`'s `host.motor` access (`:184-191`) exactly, rather
        # than a narrower single-bound-method seam like the three hooks below,
        # because the hook needs BOTH the busy check (`is_processing` /
        # `is_speaking`) and the drain method. Optional so tests / minimal
        # host doubles need not supply it.
        self._motor = motor
        self._session_factory = session_factory
        # None -> resolved adaptively per press (resolve_ptt_grace).
        if grace is not None:
            session_kwargs.setdefault("grace", grace)
        self._session_kwargs = session_kwargs
        # Recovery hook (2026-07-15 PTT voice-death fix): the smallest
        # possible surface into MotorVocalIA — a bound
        # ``motor.mark_audio_suspect`` callback, never the whole engine.
        # Optional so unit tests / minimal host doubles need not supply it.
        self._on_audio_suspect = on_audio_suspect
        # Listening cue hook (ptt_cue_20260717): a bound ``motor.play_ptt_cue``
        # fired once the session reaches listening, mirroring the on_audio_
        # suspect surface — a nicety so the operator hears the mic go live even
        # with the app window unfocused. Optional so tests / minimal host
        # doubles need not supply it.
        self._on_listening = on_listening
        # Step 3 (interruptible_speech_architecture_20260804 §5.1, §8 step 3):
        # the REAL pause hook — fired by the press that wins the slot, before
        # session.start() blocks on the STT connect. Fail-open: a raising or
        # absent callback never blocks the press. Optional so tests / minimal
        # host doubles need not supply it.
        self._on_press_precheck = on_press_precheck
        # Step 3 (design §5.1, I6): the smallest possible surface into the
        # engine — a bound ``motor.resume_speech_after_ptt`` callback, fired
        # at every one of the four PTT exit paths via `_release_hold` below.
        # Optional so tests / minimal host doubles need not supply it.
        self._on_release = on_release
        self._lock = threading.Lock()
        self._session: Optional[PttSession] = None
        # Survives after the session frees the slot so GET /api/ptt/state can
        # honestly report the last exit reason (e.g. stt_lost). Cleared on the
        # next successful start.
        self._last_error: Optional[str] = None
        self._last_session_id: Optional[str] = None
        # Closure M6 (2026-08-05): press generation. Bumped under `_lock` at
        # every press that BUILDS a session; each session's close/release
        # callbacks capture their own epoch, so a stale session's LATE close
        # (descheduled across a newer press) can never clear the hold that
        # newer press armed.
        self._press_epoch: int = 0
        # Exactly-once release tracking: set to active epoch on press, cleared
        # atomically at the very first release call.
        self._held_epoch: Optional[int] = None

    def _record_event(self, action: str) -> None:
        # PRIVACY: fixed literal action, detail ALWAYS None — the transcript
        # never reaches here.
        self._event_log.record("ptt", action, None)

    def _release_hold(self, epoch: Optional[int] = None) -> None:
        """Exactly-once release funnel for the active hold epoch.

        Atomically checks epoch == self._held_epoch and sets self._held_epoch = None
        under lock. Multiple callers (session._on_release, Controller.stop, clean
        _on_session_closed backstop, or late deadman timer) safely collapse to
        exactly one execution of the engine release hook."""
        if self._on_release is None or epoch is None:
            return
        with self._lock:
            if epoch != self._held_epoch:
                return
            self._held_epoch = None
            self._fire_release()

    def _fire_release(self) -> None:
        try:
            self._on_release()
        except Exception:
            logger.exception("PTT on_release hook failed")

    # ------------------------------------------------------------------
    # Dictation mode (ptt-dictation-20261111)
    # ------------------------------------------------------------------

    @property
    def dictation_mode(self) -> str:
        with self._lock:
            return self._dictation_mode

    def set_dictation_mode(self, mode: str) -> None:
        """Live-apply a dictation mode. A hold already released keeps the
        decision it took at release; a hold still open picks this up when it
        is released."""
        from opencohost.config import settings as _s

        if mode not in _s.PTT_DICTATION_MODES:
            raise ValueError(f"Invalid dictation mode: {mode}")
        with self._lock:
            self._dictation_mode = mode

    @property
    def companion_dictation(self) -> bool:
        with self._lock:
            return self._companion_dictation

    def set_companion_dictation(self, value: bool) -> None:
        """Live-apply the companion switch; read at release like the mode."""
        if not isinstance(value, bool):
            raise ValueError("companion_dictation must be a boolean")
        with self._lock:
            self._companion_dictation = value

    @property
    def grace_mode(self) -> str:
        with self._lock:
            return self._grace_mode

    @property
    def grace_s(self) -> float:
        with self._lock:
            return self._grace_s

    def set_grace(self, mode: str, grace_s: Optional[float] = None) -> None:
        """Live-apply the grace config; the NEXT hold picks it up."""
        from opencohost.config import settings as _s

        if mode not in _s.PTT_GRACE_MODES:
            raise ValueError(f"Invalid grace mode: {mode}")
        if grace_s is not None and not _s.is_valid_ptt_grace_s(grace_s):
            raise ValueError("Invalid grace_s")
        with self._lock:
            self._grace_mode = mode
            if grace_s is not None:
                self._grace_s = float(grace_s)

    def report_focus(self, focused: bool) -> None:
        """The main webview's focus state (memory only, time-stamped)."""
        with self._lock:
            self._focus = (bool(focused), self._focus_clock())

    def focus_is_fresh_and_focused(self) -> bool:
        with self._lock:
            return self._focus_fresh_locked()

    def _focus_fresh_locked(self) -> bool:
        # Unknown or stale counts as background: the turn is sent.
        if self._focus is None:
            return False
        focused, at = self._focus
        return focused and (self._focus_clock() - at) <= self._focus_stale_s

    def mark_hold_background(self, session_id: str) -> bool:
        """Flag the live hold as made outside the main window (companion)."""
        with self._lock:
            session = self._session
            if session is None or session.session_id != session_id:
                return False
            self._background_epoch = self._press_epoch
            # The companion marker lands right after start(): re-lock.
            self._hold_decision = (
                self._press_epoch,
                self._should_dictate_locked(self._press_epoch),
            )
            return True

    def _should_dictate_locked(self, epoch: Optional[int]) -> bool:
        mode = self._dictation_mode
        companion = epoch is not None and epoch == self._background_epoch
        if mode == "always":
            # A companion hold dictates only when the owner switched it on.
            return self._companion_dictation if companion else True
        if mode == "foreground":
            if companion:
                return False
            return self._focus_fresh_locked()
        return False

    def _on_flush(self, text: str, epoch: Optional[int] = None) -> Optional[str]:
        """Session flush callback: dictate (no turn) or dispatch.

        Uses the decision locked at the start of this press; only a press
        without one (never the case for a real hold) decides here. Dictation skips the dispatch AND both drain hooks:
        those exist to make room for a new turn, and none is created. The
        speech-hold release is not on this path (ADR-048), so it is untouched.
        """
        with self._lock:
            decision = self._hold_decision
            if decision is not None and decision[0] == epoch:
                dictate = decision[1]
            else:
                dictate = self._should_dictate_locked(epoch)
            mode = self._dictation_mode
        if dictate and text and text.strip():
            # Literals only: never the words, never their count beyond what
            # the session's own "[PTT] flushed" line already records.
            _session_log.info("[PTT] dictated mode=%s", mode)
            return _DICTATED
        self._dispatch(text)
        return None

    def _dispatch(self, text: str) -> None:
        if not text or not text.strip():
            logger.debug("PTT dispatch dropped: empty transcript")
            return
        # Honest history_text (memoria_quality_20260717 A1): the motor commits
        # "El streamer dijo (PTT): ..." to historial instead of the raw
        # "acaba de decir (PTT)" prompt wrapper, so PTT-derived memorias no
        # longer bury the streamer's real words under boilerplate. Runs in
        # parallel to the legacy voice_control.py idle path — the two PTT
        # sources stay distinct and are never merged (project safety rule).
        # Honest source (B2, turn provenance): this controller is the only place
        # that KNOWS the turn arrived through PTT — the engine used to hardcode
        # source="direct" for every dispatched turn, so PTT turns logged and
        # telemetered as direct. Rides as the 4th tuple element, like history_text
        # rides as the 3rd.
        # Unit 4.1 (runtime_findings_batch_20260731, F5): stamp submitted_at HERE
        # too — the same seam covers PTT "for free" per the unit's scope (no
        # separate seam per source). Lets queue_wait_ms report an honest wait
        # if a PTT flush lands while the motor is busy.
        self._dispatcher.dispatch(
            "process_context",
            i18n_active.ptt_wrapper().format(text=text),
            history_text=i18n_active.ptt_history_wrapper().format(text=text),
            source="ptt",
            submitted_at=time.monotonic(),
        )
        # Step 1 (interruptible_speech_architecture_20260804): the same
        # best-effort arrival-time drain call chat.py:184-191 makes, so a
        # deferred PTT question is visible in `_priority_queue` at ARRIVAL
        # (priority 0) instead of sitting invisible in `command_queue` until
        # the next boundary -- during which the agenda tick thread could read
        # `pending_owner_questions() == 0` and start a fresh agenda turn.
        # Best-effort by the same rule as chat.py: a raising drain must never
        # break the WS flush -- the dispatch above already succeeded, and the
        # engine-boundary drain remains the backstop this one cannot cover.
        motor = self._motor
        if motor is not None:
            drain_speech = getattr(motor, "drain_speech_for_new_turn", None)
            if callable(drain_speech):
                try:
                    drain_speech("ptt")
                except Exception:
                    logger.exception("PTT dispatch drain_speech_for_new_turn failed")
        if motor is not None and (motor.is_processing or motor.is_speaking):
            drain = getattr(motor, "_drain_pending_direct_into_priority_queue", None)
            if callable(drain):
                try:
                    drain()
                except Exception:
                    logger.exception("PTT arrival-time drain failed; the boundary drain still covers it")

    def start(self) -> str:
        with self._lock:
            if self._session is not None:
                # Judge closure (2026-08-05, BLOCKER, both blind judges): a
                # press that loses the slot has armed NOTHING — the precheck
                # fires below, only for the slot winner — so there is nothing
                # to release here. The old order (precheck at the top,
                # unconditional release on 409) cleared the LIVE hold of a
                # still-LISTENING session on any duplicate press (hotkey
                # auto-repeat, client retry, second window): `_pick()`
                # unblocked and Kira played into the open microphone, her own
                # TTS round-tripping through STT as an owner turn. It also
                # broke closure M6's own premise in a second interleaving: a
                # grace expiry landing between press N's precheck and this
                # lock let press N+1 win and bump the epoch, and press N's
                # epoch-less 409 release then cleared press N+1's live hold.
                raise SessionActive()
            # Closure M6: this press owns the hold from here on. Each
            # callback captures ITS epoch so a previous session's late close
            # cannot clear it.
            self._press_epoch += 1
            epoch = self._press_epoch
            # A raising factory needs no release: the precheck has not fired
            # yet, so this press holds nothing (judge closure — the old
            # "raising factory escapes with the hold set" hole is now
            # unreachable by construction, not by a compensating release).
            from opencohost.config import settings as _s
            # Lock dictate-vs-send NOW (focus at press + mode + companion).
            self._hold_decision = (epoch, self._should_dictate_locked(epoch))
            kwargs = dict(self._session_kwargs)
            if "grace" not in kwargs:
                eff, src, st = resolve_ptt_grace(
                    grace_mode=self._grace_mode, grace_s=self._grace_s
                )
                kwargs["grace"] = eff
                if src == "fast_preset":
                    kwargs.setdefault("tail_extension", _s.PTT_FAST_TAIL_EXTENSION_S)
                    kwargs.setdefault("late_drain", _s.PTT_FAST_LATE_DRAIN_S)
                elif src == "custom":
                    kwargs.setdefault("tail_extension", min(_TAIL_EXTENSION_S, eff))
                _session_log.info(
                    "[PTT] grace effective=%.2fs source=%s silence_timeout=%.2f",
                    eff, src, st,
                )
            session = self._session_factory(
                self._ws_uri,
                on_flush=lambda text, _e=epoch: self._on_flush(text, _e),
                on_event=self._record_event,
                on_close=lambda err, _e=epoch: self._on_session_closed(err, _e),
                on_release=lambda _e=epoch: self._release_hold(_e),
                **kwargs,
            )
            self._session = session
            self._last_session_id = session.session_id
            self._last_error = None
            self._held_epoch = epoch
            # Step 3: Fire the pause hook INSIDE the lock so no concurrent stop()
            # can slip in before the hold is applied.
            if self._on_press_precheck is not None:
                try:
                    self._on_press_precheck()
                except Exception:
                    logger.exception("PTT press precheck failed")
        try:
            session.start()  # blocks until connect result is known
        except PttUnreachable:
            with self._lock:
                if self._session is session:
                    self._session = None
                self._last_error = "stt_unreachable"
            self._release_hold(epoch)
            raise
        except BaseException:
            # Step 3: any OTHER raise from session.start() (e.g. a
            # Thread.start() RuntimeError) is the same hole — it never
            # raises PttUnreachable today, so it would otherwise escape with
            # the hold set and no session left to ever clear it.
            with self._lock:
                if self._session is session:
                    self._session = None
                self._last_error = "stt_unreachable"
            self._release_hold(epoch)
            raise
        # Session is now listening (start() returns only on success). Fire the
        # cue AFTER — never on the stt_unreachable path, where nothing listens.
        self._notify_listening()
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

    def _arm_deadman(self, epoch: int, timeout: float = 15.0) -> None:
        """Fail-safe: if a session dropped on transport error or starvation and the
        operator never sends an explicit stop (client crash/kill), the deadman timer
        releases the SpeechRouter hold after timeout so Kira is never permanently muted."""
        def _deadman_fire():
            self._release_hold(epoch)
        t = threading.Timer(timeout, _deadman_fire)
        t.daemon = True
        t.start()

    def stop(self, session_id: Optional[str] = None) -> dict:
        session = None
        epoch_to_release = None
        with self._lock:
            if self._session is not None:
                if session_id is None or session_id == self._session.session_id:
                    session = self._session
                    epoch_to_release = self._press_epoch
                    # The dictation decision was locked at press; stop reuses it.
            else:
                # If session already idled/closed, release if session_id matches the last session (or was omitted)
                if session_id is None or session_id == self._last_session_id:
                    epoch_to_release = self._press_epoch
        if session is not None:
            session.stop()  # idempotent, returns immediately; watcher flushes
        if epoch_to_release is not None:
            self._release_hold(epoch_to_release)
        return {"state": _FLUSHING if session is not None else _IDLE}

    def _dictation_locked(self) -> bool:
        d = self._hold_decision
        return bool(d is not None and d[0] == self._press_epoch and d[1])

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
                    "dictation": False,
                }
            return {
                "state": session.state,
                "session_id": session.session_id,
                "buffered_chars": session.buffered_chars,
                "last_error": session.last_error,
                "stt_ws_url": self._ws_uri,
                "dictation": self._dictation_locked(),
            }

    def set_ws_uri(self, ws_uri: str) -> None:
        """Repoint the controller at a different LiveAudio/WhisperLive socket.

        The operator regularly launches OpenCohost before LiveAudio (or moves
        LiveAudio to a capture PC), and killing the backend to fix that is
        unacceptable. Only the NEXT ``start()`` picks this up: an in-flight
        PttSession captured its own ``_ws_uri`` at construction (see
        ``PttSession.__init__``) and is deliberately never repointed mid-hold —
        a live socket swap under a running recv loop would silently drop the
        buffered turn. Guarded by the same lock as ``state()``/``start()`` so
        an HTTP handler thread can call it while a hold is running.
        """
        with self._lock:
            self._ws_uri = ws_uri

    def _notify_listening(self) -> None:
        """Fire the optional listening hook (the PTT audio cue) once the
        session has reached listening. Fail-open: a hook error never fails the
        start — the operator is already live and the cue is only a nicety."""
        if self._on_listening is None:
            return
        try:
            self._on_listening()
        except Exception:
            logger.exception("PTT listening hook failed")

    def _on_session_closed(self, last_error: Optional[str], epoch: Optional[int] = None) -> None:
        with self._lock:
            self._session = None
            self._last_error = last_error
        # Physical hold fail-safe:
        # If clean close (last_error is None), release immediately as backstop.
        # Keepalive starvation (auto_stopped): the STT session is already closed
        # and flushed, so there is no live mic left to talk over; release now
        # instead of making the reply wait for the 15 s deadman (ptt-stall G1).
        # _release_hold's epoch guard keeps a newer press untouched.
        # Other abnormal closes (transport drop, error) keep the deadman timer.
        if epoch is not None:
            if last_error is None or last_error == "keepalive_starvation":
                self._release_hold(epoch)
            else:
                self._arm_deadman(epoch, timeout=15.0)
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
