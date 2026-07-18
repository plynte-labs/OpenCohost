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

import ollama

from opencohost.config.settings import (
    EDITORIAL_CARDS_DB,
    LOG_DIR,
    OLLAMA_MODELS_DIR,
    load_last_profile,
)
from opencohost.core.agenda_persistence import AgendaPersistence
from opencohost.core.editorial_agenda_bridge import EditorialAgendaBridge
from opencohost.core.editorial_cards import EditorialCardStore
from opencohost.core.health_monitor import HealthMonitor, OllamaWatchdog
from opencohost.core.llm_engine import MotorVocalIA
from opencohost.core.profiles import cargar_perfiles
from opencohost.core.music_library import MusicLibrary
from opencohost.core.ollama_startup import OllamaStartupManager
from opencohost.avatar.obs_runtime import ObsRuntime
from opencohost.api.agenda_driver import AgendaDriver, route_motor_event_to_agenda
from opencohost.api.observability import log_motor_accion
from opencohost.smart_aggregator.aggregator import Aggregator
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
        # E1 (memoria_quality_20260717): fresh-memoria notice for the Tauri
        # chat-panel feed. Detail stays None (whitelist contract) — the event
        # only says "a memoria was saved", never which one. Not added to the
        # CTK ui/motor_event_handlers dispatch table on purpose: that frontend
        # drops unmapped statuses as a no-op, so the desktop app ignores it.
        "memoria_captured",
    }
)


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

    def record(self, text: str, source: str) -> None:
        with self._lock:
            self._turn_id += 1
            self._replies.append(
                {"text": text, "source": source, "turn_id": self._turn_id, "ts": time.time()}
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
        self.aggregator = None
        # RF3 control-plane single-flight guard for POST .../connect —
        # ponytail: one global lock, not per-source, since one process owns
        # exactly one Aggregator.
        self.aggregator_lock = threading.Lock()
        self.agenda = None
        # KiraAgendaController is plain lists/attrs, not thread-safe — one
        # global lock, mirrors aggregator_lock (one process, one controller).
        self.agenda_lock = threading.Lock()
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
        self.event_log.record("motor", status)

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

    def _handle_agenda_motor_event(self, status) -> None:
        """Feed a motor status event into the agenda controller (FIX-C).

        Runs on the ENGINE thread (via _dispatch_motor_event). Takes
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
        with self.agenda_lock:
            route_motor_event_to_agenda(agenda, status, on_speech_complete=nudge)

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
