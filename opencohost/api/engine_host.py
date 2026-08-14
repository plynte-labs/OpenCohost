"""Standalone engine host for the Kira FastAPI API layer (Phase 1).

The ONLY file in `opencohost/api/` that imports the core engine classes.
`EngineHost` constructs and owns a private `MotorVocalIA` + `HealthMonitor`
pair for a standalone process — wired the same way `app_shell.py` wires
the Tk app's engine, but never shared with it.
"""

import collections
import logging
import os
import shutil
import tempfile
import threading
import time
from typing import Optional

import ollama

from opencohost.config.settings import (
    EDITORIAL_CARDS_DB,
    LOG_DIR,
    OLLAMA_MODELS_DIR,
    load_last_profile,
)
from opencohost.core.agenda.agenda_persistence import AgendaPersistence
from opencohost.core.editorial.editorial_agenda_bridge import EditorialAgendaBridge
from opencohost.core.editorial.editorial_cards import EditorialCardStore
from opencohost.core.observability.health_monitor import HealthMonitor, OllamaWatchdog
from opencohost.core.llm_engine import MotorVocalIA
from opencohost.core.profiles.profiles import cargar_perfiles
from opencohost.core.music.music_library import MusicLibrary
from opencohost.core.providers.local.ollama_startup import OllamaStartupManager
from opencohost.avatar.obs_runtime import ObsRuntime
from opencohost.api.agenda_driver import (
    AgendaDriver,
    enqueue_agenda_action,
    route_motor_event_to_agenda,
)
from opencohost.api.observability import log_motor_accion
from opencohost.smart_aggregator.aggregator import Aggregator
# chat_reaction is the CTK routing extracted to neutral ground precisely so
# this package can reuse it without breaking its never-imports-opencohost.ui
# contract (see main.py's module docstring and _MOTOR_EVENT_WHITELIST below).
from opencohost.smart_aggregator.chat_reaction import (
    ChatReactionCore,
    kira_agenda_consume_pending_chat_if_due,
    on_smart_aggregated_context,
)
from opencohost.smart_aggregator.kira_agenda_controller import AgendaState, KiraAgendaController

try:
    import msvcrt
except ImportError:  # pragma: no cover - Phase 1 targets Windows only
    msvcrt = None

_LOCK_PATH = os.path.join(tempfile.gettempdir(), "opencohost_api_engine.lock")

# opencohost/api/engine_host.py -> opencohost/ -> opencohost/config/smart_aggregator.yaml
_AGGREGATOR_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "config",
    "smart_aggregator.yaml",
)

_logger = logging.getLogger(__name__)

# Item B (event-engine-a-20260709 follow-up): CLOSED whitelist of motor
# status strings that may reach GET /api/events. Duplicated (not imported)
# from ui/motor_event_handlers.py's dispatch table -- this package must
# never import opencohost.ui (see _find_ollama_executable above / main.py
# module docstring). Any status NOT in this set is dropped silently by
# EngineHost._record_motor_event, never forwarded verbatim -- PRIVACY: this
# is the only data that ever reaches EventLogSink, so the set below IS the
# gate. Keep in sync manually with motor_event_handlers.py's dispatch keys.
_MOTOR_EVENT_WHITELIST = frozenset(
    {
        "ready",
        "model_warming",
        "ollama_unavailable",
        "processing",
        "idle",
        "llm_timeout_recovered",
        "speaking_start",
        "speaking_end",
        "model_changed",
        "model_switch_pending",
        "model_switch_applied",
        "model_switch_failed",
        "llm_tier_switch_applied",
        "llm_tier_switch_failed",
        "download_start",
        "download_done",
        "download_error",
        "ctx_pressure_high",
        "piper_voice_locale_mismatch",
        # multi_provider_llm_20260723 F4: cloud LLM failure surfaced to the
        # operator (manual-mode failure, or an auto-fallback whose local warm-up
        # also failed). Server-side whitelist entry so the event persists into
        # GET /api/events -> the Tauri client. Detail stays None (whitelist
        # contract). NOT added to the CTK ui/motor_event_handlers dispatch table
        # (that surface is out of scope per owner decision); the Tauri client's
        # own status subset is a separate UI-repo change.
        "cloud_llm_error",
        # Unit 2.1 (runtime_findings_batch_20260731): cloud auth/key failure
        # (`bad_key` class) -- a "check your key" banner, latched once per
        # failure event on the engine side (never per-turn spam). Same
        # pattern as `cloud_llm_error` right above: NOT added to the CTK
        # ui/motor_event_handlers dispatch table (out of scope per owner
        # decision, and unnecessary -- that table only wires the legacy CTK
        # UI's own callback, not this whitelist/Tauri emit path).
        "cloud_bad_key",
        # E1 (memoria_quality_20260717): fresh-memoria notice for the Tauri
        # chat-panel feed. Detail stays None (whitelist contract) — the event
        # only says "a memoria was saved", never which one. Not added to the
        # CTK ui/motor_event_handlers dispatch table on purpose: that frontend
        # drops unmapped statuses as a no-op, so the desktop app ignores it.
        "memoria_captured",
        # Unit 2.2 (runtime_findings_batch_20260731): cloud auto-return
        # narration -- fallback engaged, a background probe (re)scheduled, and
        # a successful return to cloud. Detail stays None except
        # cloud_probe_scheduled (see _MOTOR_EVENT_DETAIL_FIELDS below); the
        # reason CLASS is exposed separately via GET /api/status
        # (StatusResponse.fallback_reason), never riding this event's detail.
        "cloud_fallback_engaged",
        "cloud_probe_scheduled",
        "cloud_restored",
        # WU3 (cloud_rearm_20260801): the ambiguous_429 conservative
        # auto-return exhausted its attempts budget without a successful
        # probe. Detail stays None -- NOT added to _MOTOR_EVENT_DETAIL_FIELDS
        # (privacy gate, same as cloud_fallback_engaged/cloud_restored).
        "cloud_probe_gave_up",
        # F1 companion (interruptible_speech_architecture_20260804, runtime-
        # findings 2026-08-07): a ptt/direct turn came back empty after every
        # retry -- surfaced so the owner sees it instead of inferring the
        # loss from silence. Detail stays None (same privacy gate).
        "turn_dropped",
    }
)

# Unit 2.3 (runtime_findings_batch_20260731 F10): per-event detail schema,
# relaxing the detail=None-ALWAYS privacy gate ONLY for the statuses listed
# here. `_on_ctx_pressure_high` copies ONLY these keys, and ONLY when the
# value is numeric (int/float, not bool) -- a text/malicious value for a key
# drops that key rather than forwarding it. Every whitelisted status NOT
# listed here keeps detail=None, unconditionally, in _record_motor_event.
_MOTOR_EVENT_DETAIL_FIELDS: dict = {
    "ctx_pressure_high": ("ratio", "effective_ctx", "native_ctx", "evicted_pairs"),
    # Unit 2.2: numeric seconds-until-next-probe, same numeric-only gate.
    "cloud_probe_scheduled": ("seconds",),
    # 2026-08-14: how many drafts the promotion sweep KEPT. The notice moved
    # from per-capture to per-sweep, and without a count a sweep keeping 20
    # rendered exactly like one keeping 1. Numeric-only gate as above; the
    # event still says nothing about WHICH memorias, so the privacy contract
    # is unchanged.
    "memoria_captured": ("kept",),
}


class _Drain:
    """No-op log sink.

    PRIVACY (design v2.1 B-MF1): the engine only ever calls `.put()` on this
    queue-like object (it never reads back). A pure drain both prevents
    unbounded growth AND, critically, MUST NOT log or persist the message —
    `log_queue.put` sites carry raw generated dialogue (llm_engine.py).
    Logging it here would leak Kira's conversation content to disk.
    """

    def put(self, _msg):
        pass


def _noop_event(*_args, **_kwargs):
    pass


def _find_ollama_executable():
    """Locate the Ollama executable.

    ponytail: duplicated (not imported) from
    `opencohost/ui/model_panel.py:_find_ollama_executable` — that file is
    forbidden for this slice, and importing `opencohost.ui` here would
    violate this package's "never imports opencohost.ui" contract (see
    main.py module docstring). Accept the ~15-line duplication instead.
    Keep both copies in sync if the discovery order ever changes.
    """
    ollama_exe = shutil.which("ollama")
    if ollama_exe:
        return ollama_exe

    candidates = []
    local_appdata = os.environ.get("LOCALAPPDATA")
    if local_appdata:
        candidates.append(os.path.join(local_appdata, "Programs", "Ollama", "ollama.exe"))

    program_files = os.environ.get("ProgramFiles")
    if program_files:
        candidates.append(os.path.join(program_files, "Ollama", "ollama.exe"))

    for candidate in candidates:
        if os.path.exists(candidate):
            return candidate

    return None


class ChatReplySink:
    """Bounded, thread-safe sink for GET /api/chat/last-reply (P3, R8-safe).

    PRIVACY (mirrors `_Drain` above): this only ever receives Kira's OWN
    generated reply text via `MotorVocalIA.dialogue_callback` — never raw
    viewer/operator chat. R8 permits surfacing Kira's own words; it forbids
    surfacing the trigger. Guarded by a lock, mirrors `aggregator_lock`
    (one process, one sink) — ponytail: one global lock, not per-source.
    """

    def __init__(self, maxlen: int = 16):
        self._replies = collections.deque(maxlen=maxlen)
        self._lock = threading.Lock()
        self._turn_id = 0

    def record(
        self,
        text: str,
        source: str,
        queue_wait_ms: Optional[int] = None,
        answered_by_provider: Optional[str] = None,
        answered_by_transport: Optional[str] = None,
        submitted_under_provider: Optional[str] = None,
        provider_changed_while_queued: Optional[bool] = None,
    ) -> None:
        # `source` arrives as the TRUE generating source ("chat", "direct",
        # "ptt", "accumulated", "kira-agenda", "kira-agenda-stop"). The uniform
        # "kira" attribution the published `source` field has always carried is
        # derived HERE, at the only boundary that publishes it — the engine used
        # to relabel before emitting, which meant nothing downstream could ever
        # tell a chat reply from a direct one.
        #
        # R8 holds: every value in that set is a fixed internal label, never
        # viewer/operator text, so `origin` discloses which SURFACE spoke and
        # never one character of what it said.
        #
        # A literal "kira" means the origin was already destroyed upstream (a
        # legacy or hand-rolled caller), so it reports null rather than laundering
        # the placeholder into a fake origin.
        display_source = source if source.startswith("kira-agenda") else "kira"
        origin = source if source and source != "kira" else None
        with self._lock:
            self._turn_id += 1
            self._replies.append(
                {
                    "text": text,
                    "source": display_source,
                    "origin": origin,
                    "turn_id": self._turn_id,
                    "ts": time.time(),
                    # Unit 4.1 (runtime_findings_batch_20260731, F5): the queue wait
                    # for a front-originated (direct/chat/ptt) turn, in ms. None for
                    # any turn with no submitted_at stamp (agenda, accumulated) —
                    # never a fake 0. Exposed so GET /api/chat/last-reply (4.2) can
                    # report it without a second seam.
                    "queue_wait_ms": queue_wait_ms,
                    # Unit 4.2 (F12 closure): the ANSWERING provider/transport,
                    # captured at generation time (not live global state), and
                    # the provider the item was dispatched UNDER, so the front
                    # can disclose a fallback/return that happened while the
                    # item sat queued. All None unless this turn was tagged at
                    # submit time (agenda/accumulated/legacy turns never are).
                    "answered_by_provider": answered_by_provider,
                    "answered_by_transport": answered_by_transport,
                    "submitted_under_provider": submitted_under_provider,
                    "provider_changed_while_queued": provider_changed_while_queued,
                }
            )

    def last(self) -> dict:
        with self._lock:
            if not self._replies:
                return {"text": None, "source": None, "turn_id": 0, "ts": None}
            return dict(self._replies[-1])


class EventLogSink:
    """Bounded, thread-safe ring buffer for GET /api/events?since=cursor
    (item B -- engine-thread visibility item A's client-only mutation bus
    can't see, see conductor/tracks.md's Tauri Event Engine A entry).
    Mirrors ChatReplySink's shape (deque+lock) with a monotonic `seq`
    cursor instead of a turn id.

    PRIVACY: `record()` only ever stores (source, action) pairs already
    passed through the closed `_MOTOR_EVENT_WHITELIST` gate below (see
    `EngineHost._record_motor_event`) -- this class does not itself
    validate, so no other caller may feed it a raw/unmapped string.
    `detail` exists for schema parity with item A's event shape but is
    never populated here (the motor callback carries no extra payload) --
    it must never carry free text if a future caller adds one.

    `boot` is this sink's own construction time: it changes every process
    restart (when `seq` also resets to 0), so a polling client can tell a
    smaller `cursor` apart from a stale/empty poll and knows to reset its
    own `since` cursor instead of silently missing events forever (see
    src/api/events.ts::useServerEvents).
    """

    def __init__(self, maxlen: int = 200):
        self._events = collections.deque(maxlen=maxlen)
        self._lock = threading.Lock()
        self._seq = 0
        self.boot = time.time()

    def record(self, source: str, action: str, detail: "str | None" = None) -> None:
        with self._lock:
            self._seq += 1
            self._events.append(
                {"seq": self._seq, "ts": time.time(), "source": source, "action": action, "detail": detail}
            )

    def since(self, cursor: int) -> dict:
        with self._lock:
            events = [dict(e) for e in self._events if e["seq"] > cursor]
            return {"events": events, "cursor": self._seq, "boot": self.boot}


class ChatFeedSink:
    """Bounded, thread-safe ring buffer for GET /api/stream/chat-live/messages
    (RF3 -- see conductor/tracks/tauri_stream_chat_20260812/proposal.md 5).
    Mirrors EventLogSink's shape (deque+lock+monotonic seq+boot) exactly.

    PRIVACY: `record()` is assigned directly as `Aggregator.on_filtered_message`
    -- it only ever receives a message that already survived MessageFilter,
    the spam gate, AND the input safety floor (`runtime_screen` at
    aggregator.py's process_message, design.md 3.1/8). It stores ONLY what the
    UI needs to render (author, text, timestamp, seq) -- no quality score, no
    raw platform payload -- and never logs or persists any of it; this is
    post-filter viewer chat, in memory only, for the lifetime of the process.

    `boot` mirrors EventLogSink.boot: it changes every process restart (when
    `seq` also resets to 0), so a polling client can tell a smaller `cursor`
    apart from a stale/empty poll and knows to resync instead of silently
    missing messages forever.

    `session` is the same signal one level down: it changes every time the
    aggregator connects to a chat source (see `new_session`), so the operator
    switching from #canalA to #canalB does not get one continuous list that
    silently mixes both channels.
    """

    def __init__(self, maxlen: int = 200):
        self._messages = collections.deque(maxlen=maxlen)
        self._lock = threading.Lock()
        self._seq = 0
        self.boot = time.time()
        self.session = 0

    def new_session(self) -> None:
        """Drop the buffered chat and bump `session`. Wired as
        `Aggregator.on_source_changed`, so it fires on every connect --
        including a reconnect to the SAME channel, which is still a new
        session because the buffer from before the disconnect is not
        guaranteed to be contiguous with what follows.

        `seq` is deliberately NOT reset: it stays monotonic for the life of
        the process. Resetting it would make a client still polling with
        `since=150` skip the new channel's first 150 messages -- silently, and
        forever. `session` carries the reset signal instead, exactly the role
        `boot` already plays for a process restart: a client that sees it
        change clears its rendered list instead of appending to it.

        DELIBERATE ASYMMETRY: `Aggregator.disconnect()` does NOT call this.
        The operator who just ended a stream can still scroll back over the
        last 200 messages; only the next connect wipes them. Do not "fix"
        this into symmetry.
        """
        with self._lock:
            self._messages.clear()
            self.session += 1

    def record(self, message: dict) -> None:
        author = message.get("user", "")
        text = message.get("text", "")
        ts = message.get("timestamp") or time.time()
        with self._lock:
            self._seq += 1
            self._messages.append({"seq": self._seq, "author": author, "text": text, "ts": ts})

    def since(self, cursor: int) -> dict:
        with self._lock:
            messages = [dict(m) for m in self._messages if m["seq"] > cursor]
            return {
                "messages": messages,
                "cursor": self._seq,
                "boot": self.boot,
                "session": self.session,
            }


class MusicState:
    """Client-side-playback orchestration state (music-orchestration-model).

    The API holds WHAT should be playing (active mood, later a fade intent);
    the Tauri client reads it and PLAYS. This NEVER drives backend audio — the
    headless host has no AudioBedEngine (that lives only in the CTK process).
    Lock-guarded and always constructed, mirroring `ChatReplySink` above.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._active_mood = "normal"
        # {"direction","duration_ms","seq","ts"} — recorded by set_fade. `seq`
        # is monotonic (never resets) so a polling client runs a fade exactly
        # once, when it sees a seq greater than its last.
        self._fade = None
        self._fade_seq = 0

    def set_mood(self, mood: str) -> None:
        with self._lock:
            self._active_mood = mood

    def set_fade(self, direction: str, duration_ms: int) -> None:
        with self._lock:
            self._fade_seq += 1
            self._fade = {
                "direction": direction,
                "duration_ms": duration_ms,
                "seq": self._fade_seq,
                "ts": time.time(),
            }

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "active_mood": self._active_mood,
                "fade": dict(self._fade) if self._fade else None,
            }


class EngineHost:
    """Owns a standalone MotorVocalIA + HealthMonitor pair for this process."""

    def __init__(self, lock_path: str = _LOCK_PATH):
        self._lock_path = lock_path
        self._lock_fd = None
        self.motor = None
        self.monitor = None
        # FIX-B: headless OBS push chain (mirrors the CTK AvatarStateBridge +
        # OBSClient wiring). None until start() constructs it; stays None if
        # construction fails (resilient, like aggregator/agenda/music below).
        self.obs_runtime = None
        # Extensible motor-event router: the callback passed to MotorVocalIA
        # fans each status string out to every handler in this list. Kept a
        # list (not a hardcoded call) so a later work unit can also route these
        # events to the agenda without touching the callback plumbing.
        # WU-A: log_motor_accion is registered here (not in start()) so
        # acciones.jsonl parity holds even for a constructed-but-not-started
        # host, and so the existing per-handler exception guard in
        # _dispatch_motor_event contains any failure.
        # Item B: bounded event log for GET /api/events?since=cursor, always
        # constructed (mirrors chat_sink/music_state below) so it's live even
        # before start(). Registered into the SAME extensible router as
        # log_motor_accion, right next to it, for the same reason (holds even
        # for a constructed-but-not-started host).
        self.event_log = EventLogSink()
        self._motor_event_handlers = [log_motor_accion, self._record_motor_event]
        # RF3: bounded sink for post-filter viewer chat (GET
        # /api/stream/chat-live/messages), always constructed -- mirrors
        # event_log/chat_sink above -- so it exists before the Aggregator is
        # built and can be wired unconditionally at construction time below.
        self.chat_feed = ChatFeedSink()
        self.aggregator = None
        # RF3 control-plane single-flight guard for POST .../connect —
        # ponytail: one global lock, not per-source, since one process owns
        # exactly one Aggregator.
        self.aggregator_lock = threading.Lock()
        self.agenda = None
        # KiraAgendaController is plain lists/attrs, not thread-safe — one
        # global lock, mirrors aggregator_lock (one process, one controller).
        self.agenda_lock = threading.Lock()
        # Chat feeder (the missing CTK mirror that left Kira deaf to viewer
        # chat in this host): the RF3 reaction core is built in start() once
        # the motor exists; the pending compact-chat stash mirrors the CTK
        # shell's _kira_agenda_pending_compact_chat and is read/written ONLY
        # under agenda_lock (writers live on the chat source reader thread
        # AND the motor speaking_end thread — unguarded, one would clobber
        # the other's stash).
        self._chat_reactor = None
        self._pending_compact_chat = ""
        # FIX-C: the daemon thread that actually drives the passive controller
        # (CTK's _kira_agenda_tick has no headless equivalent). None until
        # start() wires it; stays None if agenda construction/wiring fails.
        self._agenda_driver = None
        # Editorial cue-card bridge. None until start() wires it; stays None if
        # wiring fails (D4) — leaving the direct-injection provider unset, the
        # status-quo behavior before this fix.
        self.editorial_bridge = None
        self.music_library = None
        # Phase 2: API-only music orchestration state + its guard. Always
        # constructed (like chat_sink), even before start(). music_lock guards
        # ALL MusicLibrary access (D4) — the library has no internal lock and
        # FastAPI's threadpool serves concurrent requests. Mirrors agenda_lock.
        self.music_state = MusicState()
        self.music_lock = threading.Lock()
        # P3: bounded sink for Kira's own generated reply text (R8-safe —
        # see ChatReplySink docstring). Always present, even before start().
        self.chat_sink = ChatReplySink()
        # S4: single-flight guard for the eager Ollama wake below — one
        # process, one wake attempt (mirrors aggregator_lock/agenda_lock).
        self.ollama_wake_lock = threading.Lock()
        self.ollama_warming = False

    def _wake_ollama_eager(self) -> None:
        """S4 (P2): eager, non-blocking Ollama server wake.

        REJECTED alternative: lazy wake inside dispatch.py — rejected
        because dispatch.py is a pure in-memory `queue.put_nowait`; probing
        the network there would re-block the exact request path P2 exists
        to protect. Owner's model is "like a microservice": warm eagerly on
        boot, never block a request waiting for it.

        Single-flight via `ollama_wake_lock.acquire(blocking=False)`: the
        lock is acquired here, synchronously, before any thread is spawned,
        so two concurrent callers can never both win and both spawn
        `ollama serve`. The readiness probe (reused `OllamaWatchdog`, same
        `/api/tags` check health_monitor already uses) and the actual
        `start_and_wait` blocking wait both run ONLY on the daemon thread —
        this method itself always returns immediately.
        """
        if not self.ollama_wake_lock.acquire(blocking=False):
            return
        ollama_exe = _find_ollama_executable()
        if ollama_exe is None:
            self.ollama_wake_lock.release()
            return

        self.ollama_warming = True

        def _worker() -> None:
            watchdog = OllamaWatchdog()

            def _is_ready() -> bool:
                watchdog.poll()
                return watchdog.status == "healthy"

            try:
                # S4/FIX-B1: same log-file redirection as model_panel.py's UI
                # path — the default stdout=PIPE/stderr=PIPE is undrained
                # here, so `ollama serve` blocks once the OS pipe buffer
                # fills and the whole engine stalls.
                manager = OllamaStartupManager(
                    is_ready=_is_ready,
                    stdout_log_path=os.path.join(LOG_DIR, "ollama_startup_stdout.log"),
                    stderr_log_path=os.path.join(LOG_DIR, "ollama_startup_stderr.log"),
                )
                manager.start_and_wait(ollama_exe, OLLAMA_MODELS_DIR)
            except Exception:
                _logger.exception("Eager Ollama wake failed")
                # FIX-BE: release only on failure so a later retry can
                # re-acquire — otherwise a raised start_and_wait leaves the
                # lock held forever and Ollama can never be re-woken
                # without a process restart. On success the lock stays
                # held (matches prior behavior): single-flight for
                # concurrent triggers depends on it, and a successful wake
                # never needs a retry anyway.
                self.ollama_wake_lock.release()
            finally:
                self.ollama_warming = False

        threading.Thread(target=_worker, daemon=True, name="ollama-wake").start()

    def _record_motor_event(self, status) -> None:
        """Item B: record engine-thread motor lifecycle into `event_log`.

        Gated by `_MOTOR_EVENT_WHITELIST` -- an unmapped status is dropped
        silently, never forwarded verbatim (privacy gate for GET /api/events).
        Registered into `_motor_event_handlers` in `__init__`, so like
        `log_motor_accion` it relies on `_dispatch_motor_event`'s per-handler
        exception guard below rather than its own try/except.
        """
        if status not in _MOTOR_EVENT_WHITELIST:
            return
        if status in _MOTOR_EVENT_DETAIL_FIELDS:
            # Unit 2.3: this status carries a numeric detail payload, recorded
            # by its own dedicated callback (see on_ctx_pressure_high wiring
            # in start() / _on_ctx_pressure_high below) because the detail
            # needs THIS generation's own locals, not a second positional
            # argument threaded through the shared `ui_callback` (CTK's
            # concrete callback takes exactly one argument -- see
            # MotorVocalIA.on_ctx_pressure_high's docstring). Skip here so the
            # event is recorded exactly once, with real detail, not twice.
            return
        self.event_log.record("motor", status)

    def _on_ctx_pressure_high(self, payload: dict) -> None:
        """Unit 2.3 (runtime_findings_batch_20260731 F10): record
        ctx_pressure_high with a numeric-only detail payload.

        Wired as `motor.on_ctx_pressure_high` in start() -- fires
        synchronously on whichever thread produced this generation
        (foreground or a pregen worker), with `payload` as a plain call
        argument (never shared instance state), so two concurrent
        generations crossing the pressure threshold cannot clobber each
        other's detail. Only the whitelisted keys survive, and only when the
        value is numeric (bool excluded -- True/False are not utilization
        numbers); anything else is dropped, never forwarded verbatim
        (privacy gate, same contract as _MOTOR_EVENT_WHITELIST above).
        """
        fields = _MOTOR_EVENT_DETAIL_FIELDS.get("ctx_pressure_high", ())
        detail = {
            key: payload[key]
            for key in fields
            if key in payload
            and isinstance(payload[key], (int, float))
            and not isinstance(payload[key], bool)
        }
        self.event_log.record("motor", "ctx_pressure_high", detail or None)

    def _on_memoria_promoted(self, payload: dict) -> None:
        """Record memoria_captured with the sweep's kept COUNT, mirroring
        `_on_ctx_pressure_high` exactly. Wired as `motor.on_memoria_promoted`
        in start().

        The notice moved from per-capture to per-sweep on 2026-08-14 (a capture
        is an unjudged draft), and `_dispatch_motor_event` drops extra args, so
        the count could not ride the shared `ui_callback`. Without this a sweep
        keeping 20 memorias rendered identically to one keeping 1."""
        fields = _MOTOR_EVENT_DETAIL_FIELDS.get("memoria_captured", ())
        detail = {
            key: payload[key]
            for key in fields
            if key in payload
            and isinstance(payload[key], (int, float))
            and not isinstance(payload[key], bool)
        }
        self.event_log.record("motor", "memoria_captured", detail or None)

    def _on_cloud_probe_scheduled(self, payload: dict) -> None:
        """Unit 2.2 (runtime_findings_batch_20260731): record
        cloud_probe_scheduled with a numeric-only detail payload, mirroring
        `_on_ctx_pressure_high` exactly. Wired as `motor.on_cloud_probe_scheduled`
        in start()."""
        fields = _MOTOR_EVENT_DETAIL_FIELDS.get("cloud_probe_scheduled", ())
        detail = {
            key: payload[key]
            for key in fields
            if key in payload
            and isinstance(payload[key], (int, float))
            and not isinstance(payload[key], bool)
        }
        self.event_log.record("motor", "cloud_probe_scheduled", detail or None)

    def _dispatch_motor_event(self, status, *_args, **_kwargs) -> None:
        """Fan a motor status string out to every registered handler.

        Passed to MotorVocalIA as ``ui_callback`` (replacing the old
        ``_noop_event``), so this runs on the ENGINE thread. Each handler is
        guarded — a slow or failing handler must never break Kira's turn loop.
        Handlers must not perform slow/network I/O here (ObsRuntime, for one,
        only enqueues — see its threading contract); log_motor_accion's local
        file append is the accepted exception, same as CTK's synchronous
        _guardar_accion.
        """
        for handler in self._motor_event_handlers:
            try:
                handler(status)
            except Exception:
                _logger.exception("motor-event handler failed for status %r", status)

    def _wire_agenda_driver(self) -> None:
        """FIX-C: wire guardrails + motor-event feedback + the driver thread.

        Mirrors the CTK agenda wiring (app_shell.py:188-192) with each guardrail
        wrapped to take `agenda_lock` (the controller is not thread-safe and the
        motor calls these back from the ENGINE thread). The motor-event feedback
        is REGISTERED into the existing extensible event router (not a replace),
        and the driver thread is constructed exactly once.
        """
        agenda = self.agenda
        motor = self.motor
        lock = self.agenda_lock

        def locked(fn):
            def wrapper(*args, **kwargs):
                with lock:
                    return fn(*args, **kwargs)

            return wrapper

        # Guardrails: identical set to CTK (app_shell.py:188-192), lock-wrapped.
        motor.agenda_output_validator = locked(agenda.accept_output)
        motor.agenda_output_preview_validator = locked(agenda.preview_accept_output)
        motor.agenda_output_transformer = locked(agenda.enforce_live_safety_cap)
        motor.agenda_controller = agenda
        # Speech-router host flag (interruptible_speech_architecture_20260804
        # §8 step 2): the API host arms it, CTK never constructs an EngineHost
        # and keeps the legacy blocking `_hablar`. Flip to False to revert
        # playback to that legacy path — the kill switch for the whole router.
        motor._speech_router_enabled = True
        # Step-3 kill switch (interruptible_speech_architecture_20260804 §8
        # step 3), same pattern and same scope: the API host arms it, CTK
        # never does. Flip to False to revert to step 2 (behavior-preserving:
        # no stack, no pause/resume, no preemption) — the whole step 3 feature.
        # Router-off dominates (judge closure 2026-08-05): every step-3
        # entrypoint requires BOTH flags, and the router reads them LIVE, so
        # flipping `_speech_router_enabled` alone is a full, safe revert and
        # these two writes have no ordering requirement between them.
        motor._speech_interrupt_enabled = True
        # WU4 4b (design-fase2.md §3): surface a preview-guardrail rejection to
        # the operator. Fires on the PREGEN WORKER THREAD — no lock wrapper
        # needed here (EventLogSink.record guards its own append with a lock,
        # safe from any thread; the engine itself guards the callback call).
        motor.on_guardrail_rejected = self._on_guardrail_rejected

        # Register agenda feedback into the extensible router (append, not
        # replace) so the OBS runtime handler keeps receiving events too.
        self._motor_event_handlers.append(self._handle_agenda_motor_event)

        driver = AgendaDriver(
            get_agenda=lambda: self.agenda,
            get_motor=lambda: self.motor,
            agenda_lock=self.agenda_lock,
        )
        driver.start()
        self._agenda_driver = driver

    def _wire_editorial_bridge(self) -> None:
        """Wire the editorial cue-card bridge's direct-injection provider (D1).

        Constructs store + bridge unconditionally: resolve_direct_context is
        store-only (D2) and must work even when the agenda failed to construct.
        register_provider() touches the controller, so it only runs when
        self.agenda is not None (skipped keeps the store-only direct path alive).
        """
        store = EditorialCardStore(EDITORIAL_CARDS_DB)
        bridge = EditorialAgendaBridge(store, self.agenda)
        if self.agenda is not None:
            with self.agenda_lock:
                bridge.register_provider()
            # USED transition (D3): mirror the CTK agenda_output_recorder
            # (app_shell.py:289), invoked by the engine on the agenda commit
            # path (llm_engine.py:900, ENGINE thread). record + mark_used run
            # under a single agenda_lock — same lock as register_provider /
            # guardrails, since both mutate the not-thread-safe controller.
            agenda, lock = self.agenda, self.agenda_lock

            def _on_accepted_output(output: str) -> None:
                with lock:
                    agenda.record_accepted_output(output)
                    if bridge.mark_used_after_successful_generation():
                        _logger.info("editorial cue card marked USED")

            self.motor.agenda_output_recorder = _on_accepted_output
        self.motor.direct_editorial_context_provider = bridge.resolve_direct_context
        # Direct-turn USED trigger (D2): store-only, fail-open, no controller —
        # wired even when agenda is None (same scope as the provider above). The
        # engine fires it once after a successful direct generation.
        self.motor.direct_editorial_usage_recorder = bridge.commit_direct_injection
        self.editorial_bridge = bridge

    def _seed_startup_profile(self) -> None:
        """FIX-A: dispatch a set_profile for the startup profile at boot.

        Root cause: MotorVocalIA._current_profile_id is None until the first
        set_profile dispatch, so /api/status.active_profile_id is null and the
        FE MemoryCard can neither list memorias nor explain why. Seeding here
        gives the API a real profile_id from boot.

        Resolution order (mirrors last_model.json's resolve_startup_model):
          1. the persisted last-used profile NAME, if it still exists on disk
          2. otherwise the first profile in perfiles.json

        Goes through the REAL dispatch path — a normal ("set_profile", payload)
        on the motor's command_queue, exactly like switch_profile builds it
        (main.py) — so the engine's set_profile handler applies the id under
        _history_lock (llm_engine.py). The private _current_profile_id is never
        poked directly.
        """
        perfiles = cargar_perfiles()
        if not isinstance(perfiles, dict) or not perfiles:
            return
        last_used = load_last_profile()
        if last_used is not None and last_used in perfiles:
            name = last_used
        else:
            # First profile in on-disk order (dicts preserve insertion order).
            name = next(iter(perfiles))
        if not isinstance(perfiles.get(name), dict):
            return
        # dict(perfiles[name]) includes the stable uuid 'id' the engine writes
        # to _current_profile_id; _profile_name mirrors switch_profile's payload.
        payload = dict(perfiles[name])
        payload["_profile_name"] = name
        self.motor.command_queue.put(("set_profile", payload))

    def _on_guardrail_rejected(self, code: str) -> None:
        """WU4 4b (design-fase2.md §3): surface a preview-guardrail rejection.

        Wired as `motor.on_guardrail_rejected`; fires on the PREGEN WORKER
        THREAD. This is a DIRECT host-side emission (not gated by
        `_MOTOR_EVENT_WHITELIST`, which only covers `ui_callback` status
        strings) — `code` is already a closed guardrail identifier (e.g.
        "contains_internal_leak"), never dialogue text, mirroring the
        EventLogSink privacy contract. `detail` stays None (schema parity).
        """
        self.event_log.record("kira-agenda", f"guardrail:{code}", None)

    def _handle_agenda_motor_event(self, status) -> None:
        """Feed a motor status event into the agenda controller (FIX-C).

        Runs on whichever thread emits the boundary event (via
        _dispatch_motor_event): the SPEECHROUTER thread when the router is
        armed (speaking_start/speaking_end emit there; `idle` emits there at
        job completion but ALSO from the ENGINE thread on a cycle that never
        spoke — `_complete_processing_cycle`), or the engine/speaker
        thread on the legacy kill-switch-OFF path. Either way `agenda_lock`
        is acquired from the emitting thread. Takes
        `agenda_lock` and applies the SAME controller-state guards as
        motor_event_handlers.py:352-356/404-407: `speaking_start` accepts the
        in-flight generation, `speaking_end` completes the turn and nudges the
        driver for an immediate re-tick (CTK's schedule_tick(1200)).
        """
        agenda = self.agenda
        if agenda is None:
            return
        driver = self._agenda_driver
        nudge = driver.nudge if driver is not None else None
        # On speaking_start of an agenda turn, spawn a background prefetch of the
        # NEXT turn so generation overlaps this turn's TTS (Fase 1: no-dead-air).
        start_prefetch = (
            (lambda: driver.maybe_start_prefetch(agenda, self.motor))
            if driver is not None
            else None
        )
        # WU2 (design-fase2.md §2.4): on an agenda speaking_end, consume the ready
        # draft SYNCHRONOUSLY here. Router armed (step 2): this runs on the
        # SPEECHROUTER thread inside the boundary emission, and the causal
        # ordering survives because the router enqueues its engine-wake sentinel
        # AFTER the boundary event — so this enqueue still lands BEFORE the
        # engine's post-boundary _process_priority_queue pop. Legacy path:
        # engine/speaker thread inside _hablar's tail, as before. Either way the
        # accumulation-flush race window stays closed and the pop finds the item
        # (pregen cache hit). The driver-tick consume remains the late-draft
        # fallback.
        # Lock safety: the emitting thread takes agenda_lock (here) THEN motor
        # locks (in replace_pending) — the SAME order the driver tick uses; no
        # inversion. The router holds NO router lock while emitting (§11 B6).
        def consume() -> None:
            # CTK parity (motor_event_handlers.py:409-415): at an agenda
            # speaking_end the stashed chat signal outranks the prefetched
            # draft — otherwise a compact_chat stashed while the motor was
            # busy would rot until the next activity spike overwrote it,
            # and Kira would ignore the very chat burst that triggered it.
            # Runs inside route_motor_event_to_agenda's callback, so the
            # caller already holds agenda_lock (the stash's only guard).
            if kira_agenda_consume_pending_chat_if_due(
                kira_agenda=agenda,
                get_pending_compact_chat=lambda: self._pending_compact_chat,
                set_pending_compact_chat=self._set_pending_compact_chat,
                enqueue_kira_agenda_action=(
                    lambda action: enqueue_agenda_action(self.motor, action, driver=driver)
                ),
                kira_agenda_update_status=lambda: None,
            ):
                return
            if driver is not None:
                driver._maybe_consume_prefetch(agenda, self.motor)

        with self.agenda_lock:
            route_motor_event_to_agenda(
                agenda,
                status,
                on_speech_complete=nudge,
                on_agenda_speaking_start=start_prefetch,
                on_agenda_speaking_end=consume,
            )
        # WU5 (design-fase2.md §3 WU5): after ANY turn's speaking_end, if an
        # interruption-return is pending, nudge the driver so the resume fires at
        # the next boundary instead of waiting out the ~4.5s cadence. The
        # interruption answer (a direct/PTT turn) does not advance agenda state,
        # so route_motor_event_to_agenda's own nudge (agenda-turn-only) never
        # fires for it — this covers that gap.
        if status == "speaking_end" and driver is not None:
            has_frozen = getattr(self.motor, "has_frozen_stash", None)
            if callable(has_frozen):
                try:
                    if has_frozen():
                        driver.nudge()
                except Exception:
                    _logger.exception("frozen-return nudge check failed")

    def _set_pending_compact_chat(self, value: str) -> None:
        """Callers MUST hold agenda_lock — see the stash comment in __init__."""
        self._pending_compact_chat = value

    def _on_aggregated_context(self, data: dict) -> None:
        """Route an aggregated chat context into Kira (the missing CTK mirror
        of app_shell._on_smart_aggregated_context).

        THREADING: fires on the chat SOURCE reader thread —
        Aggregator.process_message is the source's on_message callback and
        _on_activity_trigger runs inline in it — never the engine thread. The
        whole routed call therefore takes agenda_lock (the controller is not
        thread-safe), exactly like _wire_agenda_driver's guardrails; the
        motor enqueue inside happens agenda_lock->motor-locks, the SAME order
        the driver tick uses, so no inversion.

        Adapters, and why each is shaped this way:
        - enqueue_kira_agenda_action -> agenda_driver.enqueue_agenda_action
          (this host's one agenda->motor enqueue path; caller-holds-lock is
          its documented contract and we do).
        - kira_agenda_update_status -> no-op: it repaints CTK status labels;
          agenda state itself is served by GET /api/agenda.
        - log_accion -> _log_chat_reaction, an ALLOWLIST (see there). Only one
          of chat_reaction's two InputContract lines is chat-derived; dropping
          both would cost the operator the only signal that distinguishes
          "Kira stayed silent on purpose" from "nothing reached Kira at all".

        The outer try/except mirrors _dispatch_motor_event's guard: a routing
        failure must never break the source reader loop that feeds the chat
        UI. (Aggregator._on_activity_trigger also swallows, but that is its
        insurance, not ours to lean on.)
        """
        if self._chat_reactor is None:
            return  # start() has not wired the feeder (or aggregator failed)
        try:
            with self.agenda_lock:
                on_smart_aggregated_context(
                    data=data,
                    kira_agenda=self.agenda,
                    motor_ia=self.motor,
                    smart_agg_ui=self._chat_reactor,
                    enqueue_kira_agenda_action=(
                        lambda action: enqueue_agenda_action(
                            self.motor, action, driver=self._agenda_driver
                        )
                    ),
                    kira_agenda_update_status=lambda: None,
                    set_pending_compact_chat=self._set_pending_compact_chat,
                    log_accion=self._log_chat_reaction,
                )
        except Exception:
            _logger.exception("aggregated-context routing failed")

    # ALLOWLIST, not a denylist -- an unrecognized line is DROPPED, so a future
    # log_accion() call added to chat_reaction.py cannot start leaking through
    # here by default. It has to be allowed in deliberately, after someone
    # checks what it interpolates.
    #
    # chat_reaction emits exactly two InputContract lines and only ONE is safe:
    #   stay_silent  -> msgs/users/event/confidence. Pure metadata: two ints, a
    #                   fixed enum from CHAT_EVENTS, a float. No viewer text.
    #   using packet -> interpolates `old_compact[:80]!r`, which is
    #                   intent_summary["prompt"] -- viewer-DERIVED. Never
    #                   logged here (project rule: no raw chat in logs).
    #
    # stay_silent is the line worth keeping: when Input Contract is ON, a quiet
    # Kira is the CORRECT outcome for low-signal chat, and without this the
    # operator cannot tell that apart from a dead pipeline. Same metadata-only
    # precedent as runtime_screen(), which counts and logs blocks by rule id
    # and never the text (aggregator.py's safety-floor comment).
    _CHAT_REACTION_LOG_ALLOWED = ("[InputContract] stay_silent:",)

    def _log_chat_reaction(self, message: str) -> None:
        if message.startswith(self._CHAT_REACTION_LOG_ALLOWED):
            _logger.info("%s", message)

    def _on_chat_source_changed(self) -> None:
        """Wired as `Aggregator.on_source_changed` (fires on every connect,
        including a reconnect to the same channel -- see the call-site
        comment in aggregator.py). Two independent per-source resets live
        behind this one signal:

        1. The display buffer (`ChatFeedSink.new_session`, pre-existing).
        2. The motor's OWN pending queue (judgment day 2026-08-13, Finding
           2): without this, a `source="chat"` reaction queued against the
           OLD channel could still pop and speak minutes later -- after the
           operator switched channels -- and the Tauri UI would route that
           reply into the NEW channel's Stream tab. `drop_pending_sources`
           matches by PREFIX; "chat" cannot prefix-match any other live
           source tag (ptt, direct, kira-agenda*), so this cannot reach past
           its own queue slice.
        """
        self.chat_feed.new_session()
        if self.motor is not None and hasattr(self.motor, "drop_pending_sources"):
            self.motor.drop_pending_sources(("chat",))

    def start(self) -> None:
        self._acquire_lock()
        try:
            self._wake_ollama_eager()
            # FIX-B: headless OBS push chain — resilient construction (mirrors
            # the Aggregator/Agenda/MusicLibrary pattern below): a failure here
            # must not brick the host. Register the handler only after
            # start_from_config succeeds so a failed runtime never routes events.
            try:
                obs_runtime = ObsRuntime()
                obs_runtime.start_from_config()
                self.obs_runtime = obs_runtime
                self._motor_event_handlers.append(obs_runtime.handle_motor_event)
            except Exception:
                self.obs_runtime = None
                _logger.exception("ObsRuntime construction failed; OBS push chain disabled")
            self.motor = MotorVocalIA(
                _Drain(),
                ui_callback=self._dispatch_motor_event,
                dialogue_callback=self.chat_sink.record,
            )
            # Unit 2.3: wired unconditionally (unlike the agenda-only guardrail
            # hook below) -- ctx_pressure_high is a pure LLM/context concern,
            # unrelated to whether the agenda constructed successfully.
            self.motor.on_ctx_pressure_high = self._on_ctx_pressure_high
            # Unit 2.2: same rationale as ctx_pressure_high above -- wired
            # unconditionally, a pure cloud/fallback concern independent of
            # agenda construction.
            self.motor.on_cloud_probe_scheduled = self._on_cloud_probe_scheduled
            # 2026-08-14: same rationale again -- the promotion sweep is a pure
            # memoria concern, unrelated to agenda construction. Carries the
            # kept COUNT, which cannot ride the shared ui_callback.
            self.motor.on_memoria_promoted = self._on_memoria_promoted
            self.monitor = HealthMonitor()
            self.motor.health_monitor = self.monitor  # mirrors app_shell.py:196-197
            self.motor.start()
            try:
                self.monitor.qwen_manager.attach_existing()
            except Exception:
                pass
            self.monitor.start()
            # RF3 stream chat-live control-plane: resilient by design — a
            # missing config or optional chat-source dependency must not
            # brick the engine/profiles/commands surface.
            try:
                self.aggregator = Aggregator(config_path=_AGGREGATOR_CONFIG_PATH, llm_interface=None)
                # RF3: wire the chat feed sink directly -- process_message
                # already runs runtime_screen (design.md 3.1/8, input safety
                # floor) before this callback fires, so chat_feed only ever
                # receives screened, filtered messages. See ChatFeedSink
                # docstring.
                self.aggregator.on_filtered_message = self.chat_feed.record
                # Source-change signal: every connect (a different channel OR
                # a reconnect to the same one) clears the feed and bumps
                # `session`, so #canalA's messages never render as a silent
                # prefix of #canalB's -- AND drops any pending source="chat"
                # reaction still queued in the motor from the old channel
                # (see _on_chat_source_changed). Plain attribute assignment
                # of a bound method -- re-running this block (aggregator
                # rebuild) just rebinds the same callable, so it is
                # idempotent.
                self.aggregator.on_source_changed = self._on_chat_source_changed
                # Chat->Kira feeder: without this the aggregator built its
                # context payloads and dropped them on a None callback, so
                # viewer chat rendered in the UI but never reached the motor
                # (the bug this wiring fixes). on_log stays the default no-op:
                # the CTK lines it would carry ("Tema dominante: <label>")
                # embed viewer-derived text, and this host's log discipline
                # (EventLogSink whitelist / "never expose raw chat in logs")
                # has no sink that may take free text. on_joyita stays no-op:
                # the OBS highlight overlay is a desktop-shell feature.
                self._chat_reactor = ChatReactionCore(self.motor)
                self.aggregator.on_aggregated_context = self._on_aggregated_context
                # Deliberately NOT wired (smallest change that fixes the bug):
                # - set_llm_interface stays None, so the Vibe thermometer can
                #   never call an LLM here and every vibe resolves to the
                #   neutral fallback. Wiring it is a separate feature (the CTK
                #   uses its own busy-gated ollama call), not part of this fix.
                # - set_busy_callback only feeds that same thermometer's
                #   busy-fallback branch; with no LLM interface both branches
                #   produce the identical neutral vibe, so wiring it would
                #   change nothing observable.
                # - on_vibe_update / on_activity_trigger / on_live_safety_log /
                #   on_source_error|connect|disconnect drive CTK UI log lines;
                #   Kira reacting does not need them, and EventLogSink's closed
                #   whitelist forbids the free text they carry.
            except Exception:
                self.aggregator = None
                _logger.exception("Aggregator construction failed; stream chat-live control-plane disabled")
            # WS3 slice 3: headless agenda controller — pure state, no threads,
            # no Ollama/TTS/OBS/UI calls. A construction failure must not brick
            # the rest of the host, mirrors the Aggregator pattern above.
            try:
                self.agenda = KiraAgendaController()
                AgendaPersistence(EDITORIAL_CARDS_DB).load_into(self.agenda)
            except Exception:
                self.agenda = None
                _logger.exception("KiraAgendaController construction failed; agenda endpoints disabled")
            # FIX-C: make the agenda actually drive Kira headlessly. Resilient
            # like the constructions above — a wiring failure must not brick the
            # host, it only leaves the agenda passive (endpoints still work).
            if self.agenda is not None:
                try:
                    self._wire_agenda_driver()
                except Exception:
                    _logger.exception("Agenda driver wiring failed; agenda stays passive")
            # Editorial cue-card bridge — resilient (D4): a wiring failure must
            # not brick the host, it only leaves the direct-injection provider
            # unset (status quo). Runs regardless of agenda state so the
            # store-only direct path survives an agenda-construction failure.
            try:
                self._wire_editorial_bridge()
            except Exception:
                self.editorial_bridge = None
                _logger.exception("Editorial bridge wiring failed; cue-card injection disabled")
            # WS3 slice 4: headless music library — pure file/JSON state read,
            # no pygame, no audio device (server-side `request_mood` playback
            # is deferred). A construction failure must not brick the rest of
            # the host, mirrors the Aggregator/Agenda pattern above.
            try:
                self.music_library = MusicLibrary()
                self.music_library.load()
            except Exception:
                self.music_library = None
                _logger.exception("MusicLibrary construction failed; music endpoints disabled")
            # FIX-A: seed the active profile LAST, once the engine thread is up
            # and the rest of the host has finished wiring. Placed at the tail of
            # the successful-start path (not mid-start) so a partial-start failure
            # tears down with ONLY the stop sentinel on the command queue. The
            # seed itself is resilient — a failure must not brick the host, it
            # only leaves the profile unset until the operator's first switch.
            try:
                self._seed_startup_profile()
            except Exception:
                _logger.exception("Startup profile seed failed; active profile stays unset until first switch")
        except Exception:
            self.stop()
            raise

    def stop(self) -> None:
        """Best-effort teardown — each step swallows exceptions.

        Eager-wake orphan (WU5/D10, intentional): _wake_ollama_eager may have
        left `ollama serve` running (or a daemon wake thread mid-probe). We do
        NOT kill it here — the owner's model keeps Ollama warm across API
        restarts, and the wake thread is a daemon with log-file-redirected pipes
        (no undrained-pipe stall). Killing it at shutdown would be an anti-feature.
        """
        # FIX-C: stop the agenda driver first so its tick thread cannot enqueue
        # onto a motor that is about to be torn down. getattr-guarded: stop() is
        # best-effort and may run on a partially-constructed host.
        agenda_driver = getattr(self, "_agenda_driver", None)
        if agenda_driver is not None:
            try:
                agenda_driver.stop()
            except Exception:
                pass
        # ponytail: KiraAgendaController and MusicLibrary are both pure state
        # (no threads, no close()/reset() method) — nothing to tear down here.
        # FIX-B: stop the OBS runtime before motor teardown (ordering mirror of
        # the aggregator disconnect below) — cancel its retry loop and drop the
        # live client so no OBS socket work outlives the engine. getattr-guarded:
        # stop() is best-effort and may run on a partially-constructed host.
        obs_runtime = getattr(self, "obs_runtime", None)
        if obs_runtime is not None:
            try:
                obs_runtime.stop()
            except Exception:
                pass
        if self.aggregator is not None:
            try:
                # connect() spawns a daemon source thread; disconnect() joins
                # it. Must run before the motor/lock teardown below.
                self.aggregator.disconnect()
            except Exception:
                pass
        if self.motor is not None:
            # B2a (memoria_quality_20260717): flush the live memorias window on a
            # clean API shutdown BEFORE the stop sentinel, so un-evicted pairs
            # survive. Belt-and-braces with B1 capture-at-commit (which already
            # persists each pair at commit). flush_memorias is itself bounded +
            # fail-open, but guard anyway so a flush error never blocks teardown.
            try:
                self.motor.flush_memorias()
            except Exception:
                pass
            try:
                self.motor.command_queue.put(None)  # only engine-thread stop path
            except Exception:
                pass
        if self.monitor is not None:
            try:
                self.monitor.stop()
            except Exception:
                pass
        try:
            model = getattr(self.motor, "current_model", None)
            if model:
                # WU5/D10: bound the unload. The module-level ollama.generate()
                # uses the default client with NO timeout — a hung Ollama would
                # stall API shutdown indefinitely. A dedicated Client(timeout=2.0)
                # caps this teardown step at ~2s.
                ollama.Client(timeout=2.0).generate(model=model, prompt="", keep_alive=0)
        except Exception:
            pass
        self._release_lock()

    def _acquire_lock(self) -> None:
        fd = os.open(self._lock_path, os.O_CREAT | os.O_RDWR)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            os.close(fd)
            raise RuntimeError(
                f"Another OpenCohost API engine host is already running (lock: {self._lock_path})"
            ) from exc
        os.write(fd, str(os.getpid()).encode())
        self._lock_fd = fd

    def _release_lock(self) -> None:
        if self._lock_fd is None:
            return
        try:
            os.lseek(self._lock_fd, 0, os.SEEK_SET)
            msvcrt.locking(self._lock_fd, msvcrt.LK_UNLCK, 1)
        except Exception:
            pass
        try:
            os.close(self._lock_fd)
        except Exception:
            pass
        self._lock_fd = None
