import os
import re
import hashlib
import socket
import threading
import queue
import time
import uuid
import asyncio
import concurrent.futures
import requests
try:
    import edge_tts
except ImportError:
    edge_tts = None  # optional cloud-TTS dependency; Piper offline path stays available
from collections import deque, Counter
from typing import Callable, Optional

from opencohost.config.settings import (
    DEFAULT_MODEL, SYSTEM_PROMPT, HISTORY_MAX_TURNS, LLM_TEMPERATURE,
    LLM_TOP_P, LLM_MAX_TOKENS, LLM_KEEP_ALIVE, TEMP_DIR, TTS_SERVER_URL,
    TTS_HEAVY_TIMEOUT, TTS_LIGHT_TIMEOUT,
    OLLAMA_CHAT_TIMEOUT,
    SCOUT_ENABLED, LLM_SCOUT_TIMEOUT, LLM_SCOUT_NUM_PREDICT,
    LLM_SCOUT_TEMPERATURE, LLM_SCOUT_MIN_DIGEST_LINES,
    LLM_SCOUT_HISTORY_MSGS, SCOUT_QUEUE_FLOOR,
    TTS_LOCAL_MODEL_PATH,
    CHAT_REPEAT_PENALTY, CHAT_PRESENCE_PENALTY, CHAT_FREQUENCY_PENALTY,
    CTX_FALLBACK_DEFAULT, CHAR_BUDGET_SAFETY_FACTOR,
    CTX_PRESSURE_HIGH_THRESHOLD, CTX_OVERFLOW_SIGNAL_RATIO,
    LLM_TIER_EFFECTIVE_CTX_CAPS,
    resolve_llm_tiers,
    resolve_startup_model, save_last_model,
    load_tts_local_only, save_tts_local_only,
    load_tts_speed, save_tts_speed,
    PIPER_VOICES, piper_voice_path, load_piper_voice, save_piper_voice,
    default_piper_voice_for_locale,
    MEMORIAS_ENABLED, MEMORIAS_DB,
    PERSONALIZATION_ENABLED,
)
from opencohost.core import context_budget
from opencohost.core import personalization
from opencohost.i18n import active as i18n_active
from opencohost.i18n import coherence as i18n_coherence
from opencohost.core.tts_piper import PiperEngine
from opencohost.core.llm_tiers import LLMTierConfig, LLMTierState, LLM_TIER_LABELS
from opencohost.core.memory_digest import MemoryDigest
from opencohost.core.memoria_store import (
    MemoriaStore, derive_stable_key, build_title, build_signature,
    build_injection_lines, pinned_injection_counter,
)
from opencohost.core.repetition_guard import detect_repetition, DEFAULT_CONFIG as REPETITION_CONFIG
from opencohost.config.logger import get_logger, _debug_enabled
from opencohost.config.validation import output_guard

logger = get_logger()

# The producer can legitimately spend the full heavy TTS HTTP timeout before
# enqueuing a Qwen chunk. Keep the consumer bounded, but do not give up sooner
# than the producer's configured request timeout.
TTS_AUDIO_QUEUE_TIMEOUT = max(TTS_HEAVY_TIMEOUT, TTS_LIGHT_TIMEOUT) + 15
_TTS_MARKDOWN_EMPHASIS_RE = re.compile(r"(?<![\w])(\*{1,3})(?!\s)([^*\n]+?)(?<!\s)\1(?![\w])")
_TTS_MARKDOWN_OPERATOR_CHARS = set("=+*/<>\\|")

# D1 — eviction source-gating (privacy_prereq_fixes_20260701). Only these
# origins may be promoted into the MemoryDigest when their history pair is
# evicted. "accumulated" must NEVER be added here: _flush_accumulation
# (see below, ~line 700) bundles verbatim viewer chat text into it, so
# capturing it would leak raw chat into the digest. "chat" is excluded for
# the same reason. Missing/unknown source values are fail-closed (not
# captured) — see the eviction gate in _commit_history.
_DIGEST_CAPTURE_SOURCES = frozenset({"direct", "ptt"})

# kira_personalization_onboarding_20260705 — sources that qualify for the
# <perfil_streamer> injection. Deliberately its OWN gate (not nested inside
# `source == "direct"` below): ptt gains this block too — a deliberate,
# test-covered behavioral change (design §2), unlike the remaining
# direct-only enrichments (digest/editorial), which stay direct-only.
_PERSONALIZATION_INJECT_SOURCES = frozenset({"direct", "ptt"})

# memoria_rag_followups_20260716 candidate 1 — sources that qualify for the
# <memorias_guardadas> injection. Widened from direct-only to direct+ptt
# (same precedent as _PERSONALIZATION_INJECT_SOURCES above): voice turns now
# recall stored memorias too, under the SAME shared 700-char budget. This
# gate has THREE sites: the profile-id snapshot, the injection call, and the
# prompt prepend — all must use this frozenset or the ptt path stays dead.
_MEMORIA_INJECT_SOURCES = frozenset({"direct", "ptt"})

def _is_connection_error(exc: BaseException) -> bool:
    """Walk the exception cause chain; return True only for network-offline errors.

    Classified as connection errors: socket.gaierror, ssl.SSLError,
    aiohttp.ClientConnectorError.  asyncio.TimeoutError and all other
    exceptions return False.
    """
    import ssl
    seen: set = set()
    e: BaseException | None = exc
    while e is not None and id(e) not in seen:
        seen.add(id(e))
        if isinstance(e, (socket.gaierror, ssl.SSLError)):
            return True
        try:
            import aiohttp
            if isinstance(e, aiohttp.ClientConnectorError):
                return True
        except ImportError:
            pass
        e = e.__cause__ or e.__context__  # type: ignore[assignment]
    return False


def edge_rate_for_length_scale(scale: float) -> str:
    """Convert a Piper length_scale into the equivalent Edge-TTS rate string.

    length_scale multiplies phoneme durations (higher = SLOWER), so the
    effective speed multiplier is 1/scale. Edge-TTS's `rate` kwarg wants a
    signed percentage ("-23%"), so both engines end up moving together
    instead of in opposite directions (1.30 must slow Edge-TTS down too, not
    speed it up 30%). Guard scale <= 0 (never divide by zero / invert
    direction) by returning "+0%".
    """
    if scale <= 0:
        return "+0%"
    pct = round((1.0 / scale - 1.0) * 100)
    return f"{pct:+d}%"


# Topic Scout prompt — plain user message (NO system prompt / persona). Seeded
# with a compact, sanitized rendering of the recent LIVE host turns. Migrated
# to i18n_active.scout_prompt() (P4, kira_bilingual_e2e) -- es legacy default
# lives in opencohost/i18n/active.py::LEGACY_SCOUT_PROMPT_TEMPLATE.

# Max words allowed in a scout title (preamble filter rejects longer lines).
SCOUT_TITLE_MAX_WORDS = 6

# Prompt-injection marker floor (modest, not exhaustive — keyword lists don't
# scale). Shared by _sanitize_history_context (neutralizes via truncation) and
# the scout scrub (removes the phrase outright from the compact render).
INJECTION_MARKERS = (
    # English markers
    "ignore all previous",
    "you are now",
    "new system prompt",
    "pretend you are",
    "forget everything",
    "disregard previous",
    "do not follow",
    "your new role is",
    "you must now",
    "act as if",
    "from now on you are",
    # Spanish markers
    "olvida todo",
    "olvidá todo",
    "ignora todo",
    "ignorá todo",
    "ignora las instrucciones",
    "ignorá las instrucciones",
    "ahora eres",
    "ahora sos",
    "nuevo system prompt",
    "nuevo prompt de sistema",
    "haz de cuenta",
    "hacé de cuenta",
    "tu nuevo rol es",
    "no sigas",
    "no obedezcas",
    "actúa como",
    "actua como",
    "de ahora en adelante eres",
    "de ahora en más sos",
)


def _strip_injection_markers(text: str) -> str:
    """Remove INJECTION_MARKERS phrases outright, collapse whitespace.

    Shared by the scout scrub (_scout_scrub_text) and the memorias injection
    path (_build_memorias_injection_block): both surfaces re-render stored
    text into the prompt, so a marker phrase must be stripped, not merely
    truncated around (which _sanitize_history_context alone would do).
    """
    lowered = text.lower()
    for marker in INJECTION_MARKERS:
        idx = lowered.find(marker)
        while idx != -1:
            text = text[:idx] + text[idx + len(marker):]
            lowered = text.lower()
            idx = lowered.find(marker)
    return " ".join(text.split())

# Control commands safe to apply at a turn boundary (see
# MotorVocalIA._drain_control_commands). All are plain setters or a model
# prepare/switch — none dispatches a turn or recurses into the processing
# cycle. process_context (dispatches a turn), check_ollama (network), the None
# shutdown sentinel, and unknown verbs are deliberately excluded and stay
# deferred for run() to consume.
_DRAIN_SAFE_COMMANDS = frozenset({
    "set_voice",
    "set_motor_tts",
    "set_tts_local_only",
    "set_tts_speed",
    "set_piper_voice",
    "set_profile",
    "clear_history",
    "switch_llm_tier",
    "switch_model",
})


class MotorVocalIA(threading.Thread):
    """
    Hilo de IA: gestiona Ollama (LLM), memoria conversacional,
    y comunicación con el servidor TTS vía HTTP.
    """
    def __init__(self, log_queue, ui_callback, dialogue_callback: Optional[Callable[[str, str], None]] = None):
        super().__init__(daemon=True)
        self.log_queue = log_queue
        self.ui_callback = ui_callback
        # P3 producer (opt-in): Kira's OWN generated reply text, never raw
        # viewer chat (R8). None = zero behavior change for the CTk app.
        self.dialogue_callback = dialogue_callback
        self.command_queue = queue.Queue()
        self._reasoning_model_cache: dict[str, bool] = {}
        self._model_ctx_limit: dict[str, int] = {}

        self.voz_referencia = None
        self.is_ready = False
        self._processing = False
        self._speaking = False
        self._current_speech_source: Optional[str] = None
        # Speech-cancellation token (guarded by self._lock). Source prefixes for
        # which _hablar() must refuse at entry — kills the Bug B straggler whose
        # turn was popped from the priority queue during its GENERATION phase
        # before an emergency stop landed. Set by the emergency paths BEFORE
        # interrupt_speaking(); cleared only on agenda enable.
        self._cancelled_speech_prefixes: tuple[str, ...] = ()
        self._current_processing_source: Optional[str] = None
        self._downloading = False
        _startup_model, self._model_source = resolve_startup_model()
        self.current_model = _startup_model
        self._desired_model: str = _startup_model
        self._loaded_model: Optional[str] = None  # set after _prepare_model succeeds
        self._owns_ollama_model: bool = False
        self._current_profile_name: str = "default"
        # Stable profile UUID (R12) — written under _history_lock in
        # set_profile. Slice 1 introduces the field + lock-guarded write only;
        # the full snapshot/swap/clear critical section lands in slice 4.
        self._current_profile_id: str | None = None
        _tier_config = LLMTierConfig(**resolve_llm_tiers())
        self.llm_tiers = LLMTierState(
            config=_tier_config,
            active_tier=self._infer_active_tier(_startup_model, _tier_config),
        )

        # Pending model switch (non-blocking retry)
        self._pending_model_switch: Optional[str] = None
        self._pending_switch_next_at: float = 0.0
        self._pending_switch_not_ready_logged: bool = False
        # Last switch failure info (read by UI handler, cleared after read)
        self._last_switch_failure: Optional[dict] = None
        self._warmed_model: Optional[str] = None
        self._ollama_chat_client = None
        # Topic Scout: dedicated short-timeout client (built in run()); its HTTP
        # timeout closes the socket so Ollama cancels the generation and frees the
        # single runner slot. Lazily created in scout_digest if run() did not.
        self._ollama_scout_client = None
        self._scout_last_input_hash: Optional[str] = None
        self._last_llm_failure: Optional[dict] = None
        self._last_known_good_model: Optional[str] = _startup_model
        self._awaiting_first_success_after_switch: bool = False
        self._inference_watchdog_timeout: float = float(OLLAMA_CHAT_TIMEOUT)
        self._post_switch_watchdog_timeout: float = min(float(OLLAMA_CHAT_TIMEOUT), 45.0)
        self.motor_tts = "ligero"  # Default 'ligero' (edge-tts)

        # Privacy gate: when True, Edge-TTS is NEVER invoked — all light-engine
        # synthesis routes to Piper regardless of online status. Default False
        # preserves existing behavior. Loaded from disk; updated via set_tts_local_only.
        self.tts_local_only: bool = load_tts_local_only()

        # Offline fallback: latches True on first Edge-TTS connection error;
        # subsequent chunks skip Edge-TTS for the rest of the session.
        # Also latched True at startup when the edge_tts package is absent.
        if edge_tts is None:
            logger.info("Edge-TTS is not installed; using Piper for light-engine synthesis.")
            self._edge_tts_offline: bool = True
        else:
            self._edge_tts_offline: bool = False
        # An explicit user pick (persisted piper_voice.json) always wins; only
        # when NO persisted choice exists does the active locale pick the
        # default (es -> argentina, en -> english).
        self._piper_voice_key: str = load_piper_voice(
            default=default_piper_voice_for_locale(i18n_active.get_active_bundle().code)
        )
        # Single source of truth for the current speed setting, shared by
        # Piper (length_scale, above) and Edge-TTS (rate string, derived via
        # edge_rate_for_length_scale). Piper's own _length_scale is a mirror,
        # never read back.
        self._tts_length_scale: float = load_tts_speed()
        self._piper = PiperEngine(
            piper_voice_path(self._piper_voice_key), length_scale=self._tts_length_scale
        )
        # Honest-degrade: one-shot ui_callback notice, latched the first time
        # Piper actually engages as a fallback under a locale/voice mismatch.
        self._piper_locale_mismatch_notified: bool = False

        # Optional health monitor for auto-fallback (set externally, None = backward compat)
        self.health_monitor = None

        # T4/P5 coherence gate at startup (warn-only; log_coherence never raises).
        i18n_coherence.log_coherence(
            i18n_active.get_active_bundle(),
            piper_voice_lang=PIPER_VOICES.get(self._piper_voice_key, {}).get("lang"),
        )

        self.system_prompt = i18n_active.system_prompt()
        self.use_system_role = False

        self.historial = deque(maxlen=HISTORY_MAX_TURNS * 2)

        # L1 pipeline memory — intra-session truncation ledger (D1/D2/D3).
        # Survives model switch / watchdog recovery; dies on set_profile,
        # clear_history, and app restart (RAM-only, never persisted to disk).
        self._memory_digest: MemoryDigest = MemoryDigest()
        # Dedicated lock for _commit_history (eviction capture + historial appends).
        # A separate lock avoids deadlock risk — self._lock must never be held on
        # any path that calls _commit_history.
        self._history_lock = threading.Lock()

        # Memorias capture (R1-R4). MEMORIAS_ENABLED is True as of slice 8.
        # Session-scoped privacy switch: False = capturing (default), True =
        # paused. Tagged onto BOTH pair entries at append time in
        # _commit_history (state-at-event) — forward-only, never re-gated on
        # the CURRENT switch state at eviction time (the T1 lesson).
        self._memorias_private: bool = False
        # Lazy — only instantiated the first time the full capture gate chain
        # passes, so a run with MEMORIAS_ENABLED=False (e.g. tests that
        # monkeypatch it) never touches disk for this store (zero
        # instantiation cost on the hot path).
        self._memoria_store: Optional[MemoriaStore] = None
        # Dedicated lock for the lazy-store check-then-act (mirrors
        # api/main.py's _memoria_store_lock precedent): the switch-flush
        # worker thread can race the main thread's first use. NEVER reuse
        # _history_lock here — independent locks avoid ordering deadlocks.
        self._memoria_store_lock = threading.Lock()
        # Slice 5 (R9): honest pin/injection counter from the most recent
        # direct-path retrieval — (total_pinned, injected). None until the
        # first direct-path turn with memorias enabled. Value only; rendered
        # by the slice-7 management UI.
        self._memorias_pin_counter: Optional[tuple[int, int]] = None

        self._lock = threading.Lock()

        # Recovery flag (guarded by self._lock, same as _speaking): set when
        # the shared pygame mixer is suspected zombied (a chunk raised during
        # playback, or the PTT session that just closed may have churned the
        # WASAPI endpoint under WhisperLive's mic open/close). Consumed once,
        # at the start of the NEXT _hablar() pipeline run, which quits+re-inits
        # the mixer before playing the first chunk. Gated (never unconditional
        # per turn) because the CTk app shares this same mixer with
        # AudioBedEngine (opencohost/core/audio_bed.py) for background music —
        # an unconditional re-init would glitch/kill that music. PTT does not
        # exist in the CTk app, so this flag is only ever set from the
        # headless API's PTT teardown path or an actual playback exception.
        self._audio_reinit_needed: bool = False

        # Priority queue: (priority, timestamp, payload, source)
        # priority: 0 = PTT/streamer (highest), 1 = chat (normal), 2 = agenda (lowest)
        self._priority_queue: list = []
        self._pq_lock = threading.Lock()
        self._pq_max_items: int = 5
        self._pq_ttl_seconds: float = 30.0  # non-PTT items expire after this delay

        # Accumulation buffer for discarded/overflowed messages
        # (timestamp, payload, source)
        self._accumulation_buffer: list = []
        self._accum_max_items: int = 50
        self._accum_max_chars: int = 2000
        self._accum_ttl: float = 120.0  # 2 minutes
        self._accum_lock = threading.Lock()
        self._last_accumulation_flush_count: int = 0

        # Text-only agenda prefetch: Ollama can think while TTS is speaking.
        self._prefetch_lock = threading.Lock()
        self._prefetch_done = threading.Event()
        self._prefetch_thread: Optional[threading.Thread] = None
        self._prefetched_agenda: Optional[dict] = None
        self._prefetch_epoch: int = 0
        self.agenda_output_validator = None
        self.agenda_output_preview_validator = None
        self.agenda_output_recorder = None
        self.agenda_output_transformer = None
        self.agenda_controller = None               # Phase 0: metrics access
        self.direct_editorial_context_provider = None  # set externally by app_shell

        # Optional chat-activation telemetry seams (measure-first, off-by-default).
        # app_shell sets these ONLY when chat diagnostics are enabled; in production
        # they stay None so the guards below skip and behavior is byte-identical.
        # They fire from THIS worker thread into the aggregator's collector, which
        # locks its mutation path while enabled. RECORD-ONLY — neither changes queue
        # lifetime or speech behavior. Gated strictly on source == "chat".
        self.on_chat_item_expired = None   # (info: dict) — a chat queue item expired (TTL)
        self.on_chat_turn_spoken = None    # () — a chat turn finished speaking

    @property
    def is_speaking(self):
        with self._lock:
            return self._speaking

    @property
    def is_processing(self):
        with self._lock:
            return self._processing

    @property
    def current_speech_source(self):
        with self._lock:
            return self._current_speech_source

    @property
    def current_processing_source(self):
        with self._lock:
            return self._current_processing_source

    def interrupt_speaking(self) -> None:
        """Public interrupt: stop the in-flight speech consumer immediately.

        Sets the speaking flag False under the engine lock so the _hablar
        consumer loop exits without draining the pre-filled audio queue. This
        is the supported way for the UI to interrupt speech — callers must NOT
        reach into _lock/_speaking directly (ADR-AUD-005 Demeter fix, FR1).

        NOTE: _lock is a plain threading.Lock (not reentrant). Call this method
        only from outside any code path that already holds self._lock.
        """
        with self._lock:
            self._speaking = False

    def mark_audio_suspect(self) -> None:
        """Flag the pygame mixer as possibly zombied so the NEXT _hablar()
        call quits+re-inits it before playing its first chunk.

        Thread-safe (bool + lock, cheap latch): called from the PTT WS thread
        on session teardown (opencohost/api/ptt_session.py) and from the
        playback consumer itself after a chunk raises. The actual
        pygame.mixer.quit()/init() only ever runs on this engine's own
        thread, inside _hablar — external callers never touch pygame.
        """
        with self._lock:
            self._audio_reinit_needed = True

    def cancel_speech_for_sources(self, prefixes: tuple[str, ...]) -> None:
        """Refuse to START any upcoming _hablar() whose source matches a prefix.

        Called by the emergency paths BEFORE interrupt_speaking() and
        drop_pending_sources() so no window exists where a turn already popped
        from the priority queue (Bug B straggler) slips into _hablar between
        interrupt and drop. Scoped to ("kira-agenda",) so a concurrent
        direct/PTT reply is not silenced. Cleared only on agenda enable.
        """
        with self._lock:
            self._cancelled_speech_prefixes = tuple(prefixes)

    def clear_speech_cancel(self) -> None:
        """Clear the speech-cancellation token. Called only on agenda enable."""
        with self._lock:
            self._cancelled_speech_prefixes = ()

    def run(self):
        self._log("Inicializando cliente ligero...")
        try:
            import pygame
            import ollama
        except ImportError as e:
            self._log(f"FATAL: Dependencia faltante: {e}", level="error")
            return

        self.pygame = pygame
        self.ollama = ollama
        self._ollama_chat_client = self._create_ollama_chat_client(ollama)
        self._ollama_scout_client = self._create_ollama_scout_client(ollama)

        try:
            self.pygame.mixer.init()
        except Exception as e:
            self._log(f"FATAL: No se pudo inicializar pygame.mixer: {e}", level="error")
            return

        self._check_ollama_service()
        if TTS_LOCAL_MODEL_PATH:
            self._piper.load()
        self._log(f"Modelo inicial: {self.current_model} (fuente: {self._model_source})")
        self._log("Motor IA inicializado. Esperando comandos...")

        while True:
            try:
                comando = self.command_queue.get(timeout=1.0)
            except queue.Empty:
                # Check priority queue and accumulation buffer when idle
                self._process_priority_queue()
                self._check_pending_model_switch()
                continue

            if comando is None:
                self._log("Señal de cierre recibida. Terminando hilo IA.")
                break

            tipo, payload = comando
            self._dispatch_command(tipo, payload)

    def _dispatch_command(self, tipo: str, payload) -> None:
        """Dispatch a command tuple. Extracted from run() for testability."""
        if tipo == "set_voice":
            self.voz_referencia = payload
            if isinstance(payload, tuple):
                self.voz_referencia = payload[0]
            self._log(f"Perfil de voz configurado: {self.voz_referencia}")

        elif tipo == "check_ollama":
            self._check_ollama_service()

        elif tipo == "process_context":
            if not self.is_ready:
                self._log("Ollama no esta listo. Usa el boton de Ollama/modelo para iniciarlo.", level="warning")
                self.ui_callback("ollama_unavailable")
                return
            if self._processing or self._speaking:
                # Motor busy — enqueue to priority queue instead of dropping
                self._log("Ya procesando. Encolando en cola prioritaria...", level="debug")
                self.enqueue(payload, priority=1, source="direct")
                return
            with self._lock:
                self._processing = True
                self._current_processing_source = "direct"
            self.ui_callback("processing")
            try:
                self._ejecutar_inferencia(payload, source="direct")
            finally:
                self._complete_processing_cycle()

        elif tipo == "clear_history":
            self.historial.clear()
            self._memory_digest.clear()
            self._log("Historial de conversación limpiado.")

        elif tipo == "switch_model":
            new_model = payload
            if self._is_model_switch_noop(new_model):
                return
            self._desired_model = new_model

            if not self.is_ready:
                self._log(f"Switch a {new_model} rechazado: Ollama no esta listo.", level="warning")
                self.ui_callback("model_switch_failed")
                return

            if self._processing or self._speaking:
                if self._pending_model_switch == new_model:
                    self._log(f"Switch a {new_model} ya esta pendiente; ignorando duplicado.", level="debug")
                    return
                self._log(f"Switch a {new_model} pendiente: motor ocupado.", level="warning")
                self._pending_switch_not_ready_logged = False
                self._pending_model_switch = new_model
                self._pending_switch_next_at = time.monotonic()
                self.ui_callback("model_switch_pending")
                return

            self._apply_model_switch(new_model)

        elif tipo == "switch_llm_tier":
            self.switch_llm_tier(str(payload))

        elif tipo == "set_motor_tts":
            self.motor_tts = payload
            nombre = "Ligero (Edge-TTS)" if payload == "ligero" else "Pesado (Qwen3-TTS)"
            self._log(f"Motor de Voz cambiado a: {nombre}")

        elif tipo == "set_tts_local_only":
            enabled = bool(payload)
            self.tts_local_only = enabled
            save_tts_local_only(enabled)
            logger.info(
                "TTS local-only switched %s (Edge-TTS %s)",
                "ON" if enabled else "OFF",
                "disabled — all light synthesis via Piper" if enabled else "enabled",
            )

        elif tipo == "set_tts_speed":
            scale = float(payload)
            self._tts_length_scale = scale
            self._piper.set_length_scale(scale)
            save_tts_speed(scale)
            logger.info("Piper speech rate set to length_scale=%.2f", scale)

        elif tipo == "set_piper_voice":
            voice_key = str(payload)
            path = piper_voice_path(voice_key)
            if self._piper.reload(path):
                save_piper_voice(voice_key)
                self._piper_voice_key = voice_key
                label = PIPER_VOICES.get(voice_key, {}).get("label", voice_key)
                self._log(f"Voz de Kira cambiada a: {label}")
                logger.info("Piper voice switched to %s (%s)", voice_key, path)
            else:
                self._log(
                    f"No se pudo cambiar la voz de Kira a '{voice_key}'",
                    level="warning",
                )

        elif tipo == "set_profile":
            prompt_override_active = "prompt" in payload
            self.system_prompt = payload.get("prompt", i18n_active.system_prompt())
            self.use_system_role = payload.get("use_system", False)
            profile_name = payload.get("_profile_name", "desconocido")
            self._current_profile_name = profile_name
            # RC-2 (slice 4, task 4.14): snapshot the departing profile's live
            # window (drafts built with the OLD _current_profile_id), swap the
            # id, and clear historial/_memory_digest as ONE atomic critical
            # section under _history_lock. This widens the narrow lock block
            # slice 1 introduced (which guarded only the id write) — closing
            # the misattribution/loss window where a concurrent _commit_history
            # could previously land between the id swap and the historial
            # clear (tracked in apply-progress #2780, judge notes A-N2/B-S1).
            with self._history_lock:
                switch_drafts = self._collect_flush_drafts()
                self._current_profile_id = payload.get("id")
                self.historial.clear()
                self._memory_digest.clear()
            # RC-2/RC-3: disk upserts dispatched AFTER lock release, on a
            # worker thread — the profile switch must never block on I/O.
            self._dispatch_switch_flush(switch_drafts)
            self._log(f"Perfil actualizado: {profile_name} (System Role: {self.use_system_role}). Memoria limpiada.")
            # T4 coherence gate (warn-only; the profile always wins). Flags when a
            # custom persona's language is not governed by the active locale.
            i18n_coherence.log_coherence(
                i18n_active.get_active_bundle(),
                profile_name=profile_name,
                profile_prompt_active=prompt_override_active,
                profile_locale=payload.get("locale"),
                piper_voice_lang=PIPER_VOICES.get(self._piper_voice_key, {}).get("lang"),
            )

        elif tipo == "download_model":
            if not self.is_ready:
                self._log("No se puede descargar modelo: Ollama no esta listo.", level="warning")
                self.ui_callback("ollama_unavailable")
                return
            model_tag = payload
            if self._downloading:
                self._log("Ya hay una descarga en curso.", level="warning")
                return
            threading.Thread(
                target=self._download_model_worker,
                args=(model_tag,),
                daemon=True
            ).start()

    def enqueue(
        self,
        payload: str,
        priority: int = 1,
        source: str = "chat",
        history_text: Optional[str] = None,
    ) -> None:
        """Add a message to the priority queue.

        Args:
            payload: The text to process.
            priority: 0 = PTT/streamer (highest), 1 = chat (normal), 2 = agenda (lowest).
            source: Origin identifier for logging.
            history_text: Honest text to commit to historial for this turn
                (agenda_ptt_commit_raw_text). None (default) commits `payload`
                as before — every caller other than agenda-PTT stays unchanged.
        """
        with self._pq_lock:
            self._priority_queue.append((priority, time.time(), payload, source, history_text))
            self._priority_queue.sort(key=lambda x: (x[0], x[1]))
            # Enforce max items — drop lowest priority (highest number) first,
            # breaking ties by newest timestamp. PTT (0) is always preserved over
            # chat (1) and agenda (2).
            while len(self._priority_queue) > self._pq_max_items:
                dropped = self._priority_queue.pop()
                self._log(f"Cola prioritaria llena. Descartado (baja prioridad): {dropped[3]}")
                self.enqueue_accumulation(dropped[2], source=dropped[3])

    def replace_pending(self, payload: str, priority: int = 1, source: str = "chat") -> None:
        """Replace stale pending items from the same source and enqueue a fresh one.

        This keeps product features such as Agenda Mode from stacking old
        autonomous turns while preserving unrelated higher-priority items.
        """
        with self._pq_lock:
            self._priority_queue = [item for item in self._priority_queue if item[3] != source]
        if source.startswith("kira-agenda"):
            self.clear_prefetched_agenda()
        self.enqueue(payload, priority=priority, source=source)

    def prefetch_agenda(self, payload: str, priority: int = 2, source: str = "kira-agenda") -> bool:
        """Generate agenda text in the background without starting TTS."""
        if not payload or not source.startswith("kira-agenda"):
            return False
        with self._prefetch_lock:
            if self._prefetched_agenda is not None:
                return False
            if self._prefetch_thread and self._prefetch_thread.is_alive():
                return False
            self._prefetch_done.clear()
            epoch = self._prefetch_epoch

        def worker() -> None:
            try:
                dialogo = self._generar_dialogo(payload, source=source, commit_history=False, log_prefix="Agenda prefetch")
                if dialogo:
                    if not self._preview_accept_agenda_output(dialogo):
                        self._log(f"Agenda: prefetch rechazado ({self._format_agenda_rejection()}).", level="warning")
                        return
                    with self._prefetch_lock:
                        if self._prefetch_epoch != epoch:
                            self._log("Agenda: prefetch descartado (invalidado por clear/stop).", level="warning")
                            return
                        self._prefetched_agenda = {
                            "payload": payload,
                            "dialogo": dialogo,
                            "priority": priority,
                            "source": source,
                        }
            finally:
                self._prefetch_done.set()

        thread = threading.Thread(target=worker, daemon=True)
        with self._prefetch_lock:
            self._prefetch_thread = thread
        thread.start()
        return True

    def wait_prefetched_agenda(self, timeout: float = 0.0) -> bool:
        if timeout > 0:
            self._prefetch_done.wait(timeout)
        with self._prefetch_lock:
            return self._prefetched_agenda is not None

    def has_pending_priority_before(self, priority: int) -> bool:
        """Return True when queued work should run before a cached agenda draft."""
        with self._pq_lock:
            return any(item[0] < priority for item in self._priority_queue)

    def clear_prefetched_agenda(self) -> None:
        with self._prefetch_lock:
            self._prefetched_agenda = None
            self._prefetch_done.clear()
            self._prefetch_epoch += 1

    def play_prefetched_agenda(self) -> bool:
        """Speak cached agenda text, if available, without another LLM call."""
        with self._prefetch_lock:
            item = self._prefetched_agenda
            self._prefetched_agenda = None
            self._prefetch_done.clear()
        if not item:
            return False

        def speaker() -> None:
            payload = item["payload"]
            dialogo = item["dialogo"]
            self._commit_history(payload, dialogo, source=item.get("source", "kira-agenda"))
            self._record_accepted_agenda_output(dialogo)
            self._log("Agenda: usando respuesta prefabricada durante el audio anterior.")
            self.log_queue.put(f"\n🧠 [Kira]: {dialogo}\n")
            self._emit_dialogue(dialogo, item.get("source", "kira-agenda"))
            self._hablar(dialogo, source=item.get("source", "kira-agenda"))

        threading.Thread(target=speaker, daemon=True).start()
        return True

    def drop_pending_sources(self, prefixes: tuple[str, ...]) -> int:
        """Drop pending priority/accumulation items whose source matches prefixes.

        Args:
            prefixes: Source prefixes to remove, e.g. ("kira-agenda",).

        Returns:
            Number of pending items removed.
        """
        removed = 0
        with self._pq_lock:
            kept = []
            for item in self._priority_queue:
                if str(item[3]).startswith(prefixes):
                    removed += 1
                    continue
                kept.append(item)
            self._priority_queue = kept

        with self._accum_lock:
            kept_accum = []
            for item in self._accumulation_buffer:
                if str(item[2]).startswith(prefixes):
                    removed += 1
                    continue
                kept_accum.append(item)
            self._accumulation_buffer = kept_accum

        if any("kira-agenda".startswith(prefix) or prefix.startswith("kira-agenda") for prefix in prefixes):
            self.clear_prefetched_agenda()

        if removed:
            self._log(f"Cola: descartados {removed} pendientes de {', '.join(prefixes)}.")
        return removed

    def enqueue_accumulation(self, payload: str, source: str = "chat") -> None:
        """Add a message to the accumulation buffer (discarded/overflowed).

        These messages are compacted and sent as a single consultation
        when the motor becomes idle after processing.

        Args:
            payload: The text to accumulate.
            source: Origin identifier for grouping.
        """
        now = time.time()
        with self._accum_lock:
            self._accumulation_buffer.append((now, payload, source))
            dropped_item_limit = 0
            dropped_char_limit = 0
            # Enforce item limit
            if len(self._accumulation_buffer) > self._accum_max_items:
                self._accumulation_buffer.pop(0)  # Drop oldest
                dropped_item_limit += 1
            # Enforce char limit — drop oldest until under limit
            total_chars = sum(len(p) for _, p, _ in self._accumulation_buffer)
            while total_chars > self._accum_max_chars and self._accumulation_buffer:
                dropped = self._accumulation_buffer.pop(0)
                total_chars -= len(dropped[1])
                dropped_char_limit += 1

        if dropped_item_limit:
            self._log(
                f"Acumulación: descartados {dropped_item_limit} mensajes por límite de items.",
                level="warning",
            )
        if dropped_char_limit:
            self._log(
                f"Acumulación: descartados {dropped_char_limit} mensajes por límite de caracteres.",
                level="warning",
            )

    def _flush_accumulation(self) -> Optional[str]:
        """Compact accumulated messages into a single consultation string.

        Returns:
            Compacted text string, or None if buffer is empty/expired.
        """
        now = time.time()
        with self._accum_lock:
            # Filter out expired messages (>2 minutes old)
            before_count = len(self._accumulation_buffer)
            fresh = [(ts, p, s) for ts, p, s in self._accumulation_buffer
                     if now - ts < self._accum_ttl]
            expired_count = before_count - len(fresh)
            self._accumulation_buffer = fresh
            self._last_accumulation_flush_count = len(fresh)

            if not fresh:
                if expired_count:
                    self._log(
                        f"Acumulación: descartados {expired_count} mensajes expirados (TTL {self._accum_ttl:.0f}s).",
                        level="warning",
                    )
                return None

            if expired_count:
                self._log(
                    f"Acumulación: descartados {expired_count} mensajes expirados (TTL {self._accum_ttl:.0f}s).",
                    level="warning",
                )

            # Group by source
            by_source: dict = {}
            for _, payload, source in fresh:
                by_source.setdefault(source, []).append(payload)

            # Build compacted message
            parts = []
            for source, messages in by_source.items():
                if source == "ptt":
                    parts.append(i18n_active.accumulation_ptt().format(messages="; ".join(messages)))
                elif source == "chat":
                    parts.append(i18n_active.accumulation_chat().format(messages=" | ".join(messages)))
                else:
                    # Language-neutral: the source tag is a technical identifier,
                    # not a user-facing label. Must not become a locale slot.
                    parts.append(f"[{source}] {'; '.join(messages)}")

            # Clear buffer after reading
            self._accumulation_buffer.clear()

            return "\n".join(parts)

    def _process_priority_queue(self) -> None:
        """Process next item from priority queue if motor is idle.

        Non-PTT items older than _pq_ttl_seconds are discarded before selection
        to prevent stale reactions after long delays.

        After processing, checks accumulation buffer and sends compacted
        messages as a single consultation.
        """
        if self._processing or self._speaking:
            return

        expired_chat_infos: list = []
        with self._pq_lock:
            # Expire stale non-PTT items before selecting next work
            now = time.time()
            kept = []
            for item in self._priority_queue:
                # Slice first 4 — tolerates both the legacy 4-tuple (no
                # history_text, e.g. tests constructing raw queue items) and
                # the current 5-tuple produced by enqueue().
                prio, ts, payload, source = item[:4]
                if prio > 0 and (now - ts) > self._pq_ttl_seconds:
                    self._log(f"Item expirado y omitido (TTL {self._pq_ttl_seconds:.0f}s): {source}")
                    # Measure-first telemetry seam: record (never alter) chat expiries.
                    # Captured here, emitted below OUTSIDE _pq_lock.
                    if source == "chat":
                        expired_chat_infos.append({"age_sec": now - ts, "ttl_sec": self._pq_ttl_seconds})
                else:
                    kept.append(item)
            self._priority_queue = kept

        # Emit expiry telemetry OUTSIDE _pq_lock so the queue lock is never held across
        # the aggregator collector's lock (the two locks stay order-independent). No-op
        # unless wired (diagnostics enabled); a failing callback never disturbs the queue.
        if expired_chat_infos and self.on_chat_item_expired is not None:
            for info in expired_chat_infos:
                try:
                    self.on_chat_item_expired(info)
                except Exception:
                    pass

        with self._pq_lock:
            if not self._priority_queue:
                # No priority items — check accumulation buffer
                accumulated = self._flush_accumulation()
                if accumulated:
                    self._log(f"Procesando acumulación ({self._last_accumulation_flush_count} mensajes compactados)...")
                    with self._lock:
                        self._processing = True
                        self._current_processing_source = "accumulated"
                    self.ui_callback("processing")
                    try:
                        self._ejecutar_inferencia(accumulated, source="accumulated")
                    finally:
                        self._complete_processing_cycle(process_queue=False)
                return

            item = self._priority_queue.pop(0)
            # Unpack tolerating both the legacy 4-tuple and the current
            # 5-tuple (payload, source, history_text) produced by enqueue().
            priority, ts, payload, source, *rest = item
            history_text = rest[0] if rest else None

        source_label = "PTT" if source == "ptt" else source
        self._log(f"Cola prioritaria: procesando [{source_label}] (prioridad {priority})...")
        with self._lock:
            self._processing = True
            self._current_processing_source = source
        self.ui_callback("processing")
        try:
            self._ejecutar_inferencia(payload, source=source, history_text=history_text)
        finally:
            self._complete_processing_cycle()

    def _complete_processing_cycle(self, *, process_queue: bool = True) -> None:
        with self._lock:
            self._processing = False
            self._current_processing_source = None
        self.ui_callback("idle")
        self._drain_control_commands()
        self._check_pending_model_switch()
        if process_queue:
            self._process_priority_queue()

    def _drain_control_commands(self) -> None:
        """Apply a contiguous run of whitelisted control commands from the
        FRONT of command_queue at a turn boundary.

        The engine is single-threaded: run() only reads command_queue when it is
        NOT inside a dispatch, so control commands posted during a continuous
        priority-queue run (recursive _process_priority_queue) sit unread until
        the queue empties (the command-starvation bug). This boundary is a true
        idle point (_processing and _speaking both False), so a leading run of
        whitelisted commands is applied now, before the next turn dispatches.

        Stops at the first non-whitelisted verb (process_context — dispatches a
        turn and would recurse; check_ollama — network), the None shutdown
        sentinel, or any unknown type, and leaves it AND everything behind it
        untouched, in original queue order. This preserves FIFO relative to
        that earlier-queued item — otherwise a later whitelisted command (e.g.
        set_profile) could jump ahead and mutate state before the earlier item
        runs (e.g. answering a queued process_context under a just-switched
        profile with wiped history).

        Peeks under command_queue.mutex before popping, so a stopping item is
        never removed and never needs to be re-queued (no put_nowait tail
        re-insertion, which would itself reorder it behind whatever else
        arrived while draining). Bounded by a qsize() snapshot so it can never
        loop indefinitely even under a sustained stream of whitelisted commands.
        """
        applied = 0
        for _ in range(self.command_queue.qsize()):
            with self.command_queue.mutex:
                if not self.command_queue.queue:
                    break
                comando = self.command_queue.queue[0]
                if comando is None or comando[0] not in _DRAIN_SAFE_COMMANDS:
                    break
                self.command_queue.queue.popleft()
            tipo, payload = comando
            self._dispatch_command(tipo, payload)
            applied += 1
        if applied > 0:
            self._log(f"Boundary drain: {applied} comando(s) de control aplicados.")
            self.ui_callback("commands_drained")

    def _check_ollama_service(self, *, notify_unavailable: bool = True):
        try:
            self.ollama.list()
        except Exception as e:
            self.is_ready = False
            if notify_unavailable:
                self._log(f"Ollama no esta disponible: {e}", level="warning")
                self.ui_callback("ollama_unavailable")
            return False

        self.is_ready = True
        self._log("Ollama disponible. Preparando modelo...")
        if self._pending_model_switch and self._pending_model_switch != self.current_model:
            self._log(f"Aplicando modelo pendiente: {self._pending_model_switch}")
            self._apply_model_switch(self._pending_model_switch)
        else:
            self._prepare_model(self.current_model)
            if self._pending_model_switch == self.current_model:
                self._pending_model_switch = None
                self._pending_switch_not_ready_logged = False
        self._log("Motor IA listo.")
        return True

    def _switch_and_prepare_model(self, new_model: str) -> None:
        previous_model = self.current_model
        self.historial.clear()

        if not self._prepare_model(new_model):
            raise RuntimeError("target_model_unavailable")

        if previous_model != new_model:
            self._log(f"Liberando memoria del modelo: {previous_model}...")
            try:
                self.ollama.generate(model=previous_model, prompt='', keep_alive=0)
            except Exception as e:
                logger.warning(f"No se pudo liberar modelo {previous_model}: {e}")

        self.current_model = new_model
        self._awaiting_first_success_after_switch = previous_model != new_model
        self._log(f"🔄 Modelo cambiado a: {new_model}")

    @property
    def active_llm_tier(self) -> str:
        return self.llm_tiers.active_tier

    @active_llm_tier.setter
    def active_llm_tier(self, tier: str) -> None:
        if self.llm_tiers.config.is_available(tier):
            self.llm_tiers.active_tier = tier

    def configure_llm_tiers(
        self,
        config: LLMTierConfig,
        active_tier: Optional[str] = None,
    ) -> None:
        """Replace manual tier slots without changing prompt or conversation memory."""
        tier = active_tier or self._infer_active_tier(self.current_model, config)
        self.llm_tiers = LLMTierState(config=config, active_tier=tier)

    @staticmethod
    def _infer_active_tier(model: str, config: LLMTierConfig) -> str:
        for tier, tier_model in config.as_dict().items():
            if tier_model == model:
                return tier
        return config.first_available_tier() or "balanced"

    def switch_llm_tier(self, target_tier: str) -> bool:
        """Manually switch active LLM tier for future requests.

        The previous active tier/model is restored if the target slot is empty or
        model preparation fails. Conversation history and profile prompt are not
        changed by tier switching.
        """
        previous_tier = self.llm_tiers.active_tier
        previous_model = self.current_model
        previous_loaded_model = self._loaded_model
        previous_warmed_model = self._warmed_model
        previous_owns_ollama_model = self._owns_ollama_model
        target_model = self.llm_tiers.config.model_for(target_tier)

        if (
            target_model is not None
            and target_tier == previous_tier
            and self._is_model_switch_noop(target_model)
        ):
            return True

        if target_model is None:
            self._last_switch_failure = {
                "requested_tier": target_tier,
                "requested": None,
                "current_tier": previous_tier,
                "current": previous_model,
                "reason": "tier_unavailable",
            }
            self._log(
                f"Tier LLM '{target_tier}' no disponible; "
                f"se mantiene {previous_tier} ({previous_model}).",
                level="warning",
            )
            self.ui_callback("llm_tier_switch_failed")
            return False

        try:
            if self.is_ready and not self._prepare_model(target_model):
                raise RuntimeError("target_model_unavailable")
            self.llm_tiers.switch_to(target_tier)
            self.current_model = target_model
            self._desired_model = target_model
            self._awaiting_first_success_after_switch = previous_model != target_model
            self._loaded_model = (
                target_model
                if self._warmed_model == target_model
                else self._loaded_model
            )
            if previous_model != target_model:
                try:
                    self.ollama.generate(model=previous_model, prompt='', keep_alive=0)
                except Exception as e:
                    logger.warning(f"No se pudo liberar modelo {previous_model}: {e}")
            save_last_model(target_model, source="llm_tier_switch")
        except Exception as e:
            self.llm_tiers.active_tier = previous_tier
            self.current_model = previous_model
            self._desired_model = previous_model
            self._loaded_model = previous_loaded_model
            self._warmed_model = previous_warmed_model
            self._owns_ollama_model = previous_owns_ollama_model
            self._last_switch_failure = {
                "requested_tier": target_tier,
                "requested": target_model,
                "current_tier": previous_tier,
                "current": previous_model,
                "reason": f"switch_error: {e}",
            }
            self._log(
                f"Tier LLM {previous_tier} -> {target_tier} ({target_model}) fallido: {e}. "
                f"Se mantiene {previous_model}.",
                level="error",
            )
            self.ui_callback("llm_tier_switch_failed")
            return False

        label = LLM_TIER_LABELS.get(target_tier, target_tier)
        self._last_switch_failure = None
        self._log(f"LLM tier changed: {previous_tier} -> {target_tier} ({target_model})")
        logger.info("Manual LLM tier changed to %s (%s)", label, target_model)
        self.ui_callback("llm_tier_switch_applied")
        return True

    def _check_pending_model_switch(self) -> None:
        """Attempt pending model switch if conditions are met (non-blocking)."""
        if self._pending_model_switch is None:
            return
        if time.monotonic() < self._pending_switch_next_at:
            return  # not time yet
        if self._processing or self._speaking:
            return  # still busy

        model = self._pending_model_switch

        if self._is_model_switch_noop(model):
            self._pending_model_switch = None
            self._pending_switch_not_ready_logged = False
            return

        if not self.is_ready:
            self._pending_switch_next_at = time.monotonic() + 2.0
            if self._check_ollama_service(notify_unavailable=False):
                return
            if not self._pending_switch_not_ready_logged:
                self._log(f"Switch a {model} sigue pendiente: Ollama no está listo.", level="debug")
                self._pending_switch_not_ready_logged = True
            return

        self._apply_model_switch(model)

    def _is_model_switch_noop(self, new_model: str) -> bool:
        """Return True when the requested model is already the effective target."""
        return (
            bool(new_model)
            and new_model == self.current_model
            and new_model == self._desired_model
            and (
                not self.is_ready
                or self._warmed_model == new_model
                or self._loaded_model == new_model
            )
        )

    def _apply_model_switch(self, new_model: str, *, persist_source: str = "user_switch") -> bool:
        """Execute model switch and persist on success."""
        if self._is_model_switch_noop(new_model):
            if self._pending_model_switch == new_model:
                self._pending_model_switch = None
                self._pending_switch_not_ready_logged = False
            return True

        previous_model = self.current_model
        self._pending_model_switch = None
        self._pending_switch_not_ready_logged = False
        try:
            self._switch_and_prepare_model(new_model)
            self._desired_model = new_model
            save_last_model(new_model, source=persist_source)
            self.ui_callback("model_switch_applied")
            return True
        except Exception as e:
            self._log(f"Switch a {new_model} fallido: {e}", level="error")
            self.current_model = previous_model
            self._desired_model = previous_model
            self._last_switch_failure = {
                "requested": new_model,
                "current": self.current_model,
                "reason": f"switch_error: {e}",
            }
            self.ui_callback("model_switch_failed")
            return False

    def _prepare_model(self, model: str) -> bool:
        """Warm the selected Ollama model so first real response is not a cold load."""
        if not self.is_ready or not model or self._warmed_model == model:
            return self._warmed_model == model

        self.ui_callback("model_warming")
        self._log(f"Preparando modelo {model} en memoria...")
        start = time.time()
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(
                    self.ollama.generate,
                    model=model,
                    prompt="Responde solo: ok",
                    keep_alive=LLM_KEEP_ALIVE,
                    options={"num_predict": 1, "temperature": 0},
                )
                future.result(timeout=120)
        except Exception as e:
            self._log(f"No se pudo preparar modelo {model}: {e}", level="warning")
            logger.warning("No se pudo preparar modelo %s: %s", model, e)
            self._loaded_model = None
            self._owns_ollama_model = False
            self.ui_callback("ready")
            return False

        elapsed = time.time() - start
        self._warmed_model = model
        self._loaded_model = model
        self._owns_ollama_model = True
        self.ui_callback("ready")
        self._log(f"Modelo {model} preparado en {elapsed:.2f}s. Primera respuesta ya no deberia cargar en frio.")
        return True

    def release_owned_ollama_model(self, timeout: float = 2.0) -> bool:
        """Best-effort unload for the model warmed by this OpenCohost session."""
        model = self._loaded_model or self._warmed_model
        if not model or not self._owns_ollama_model or not hasattr(self, "ollama"):
            logger.info("Ollama model release skipped; no OpenCohost-owned model recorded")
            return False

        result = {"released": False}

        def unload() -> None:
            try:
                self.ollama.generate(model=model, prompt="", keep_alive=0)
                result["released"] = True
            except Exception as exc:
                logger.warning("No se pudo liberar modelo Ollama %s: %s", model, exc)

        thread = threading.Thread(target=unload, name="OllamaModelRelease", daemon=True)
        thread.start()
        thread.join(timeout=timeout)
        if thread.is_alive():
            logger.warning("Timeout liberando modelo Ollama %s; cierre continua", model)
            return False
        if result["released"]:
            self._loaded_model = None
            self._warmed_model = None
            self._owns_ollama_model = False
            logger.info("Modelo Ollama %s liberado", model)
        return result["released"]

    def _download_model_worker(self, model_tag):
        self._downloading = True
        self.ui_callback("download_start")
        self._log(f"📥 Descargando modelo '{model_tag}'... Esto puede tardar varios minutos.")

        try:
            last_pct = -1
            for progress in self.ollama.pull(model_tag, stream=True):
                status = getattr(progress, 'status', str(progress))
                total = getattr(progress, 'total', None)
                completed = getattr(progress, 'completed', None)

                if total is not None and completed is not None and total > 0:
                    pct = int((completed / total) * 100)
                    if pct >= last_pct + 10:
                        last_pct = pct
                        size_mb = completed / (1024 * 1024)
                        total_mb = total / (1024 * 1024)
                        self.log_queue.put(
                            f"[Descarga] {model_tag}: {pct}% ({size_mb:.0f}/{total_mb:.0f} MB) - {status}"
                        )
                elif 'success' in str(status).lower():
                    self._log(f"✅ Modelo '{model_tag}' descargado exitosamente.")
                else:
                    status_str = str(status)
                    if status_str and status_str != last_pct:
                        self.log_queue.put(f"[Descarga] {model_tag}: {status_str}")

            self.historial.clear()
            self.current_model = model_tag
            self._desired_model = model_tag
            self._loaded_model = model_tag
            save_last_model(model_tag, source="download")
            self._log(f"🔄 Modelo activo cambiado a: {model_tag}")
            self.ui_callback("download_done")

        except Exception as e:
            self._log(f"ERROR descargando '{model_tag}': {e}", level="error")
            logger.exception(f"Error descargando modelo {model_tag}")
            self.ui_callback("download_error")
        finally:
            self._downloading = False

    def _guardrail_fallback_line(self, source: str) -> str:
        """Return a canned spoken line for guard-blocked responses.

        Agenda sources return "" — the agenda state machine already handles
        rejected outputs gracefully and a canned line would break topic flow.
        Lines rotate to avoid immediate repetition on consecutive blocks.
        Lines come from the active locale bundle (i18n_active.guardrail_fallback_lines,
        es legacy default) — must stay neutral and never match an output_guard
        pattern themselves, or a block->fallback->block loop would result.
        """
        if source.startswith("kira-agenda"):
            return ""
        lines = i18n_active.guardrail_fallback_lines()
        idx = getattr(self, "_guardrail_fallback_idx", 0)
        self._guardrail_fallback_idx = idx + 1
        return lines[idx % len(lines)]

    def _maybe_notify_piper_locale_mismatch(self) -> None:
        """One-shot ui_callback notice fired the first time Piper actually
        engages as the TTS fallback while its voice language disagrees with
        the active locale (honest degrade — never silent Spanish audio under
        locale=en). Per-process, not per-utterance: fires once, then no-ops.
        """
        if self._piper_locale_mismatch_notified:
            return
        voice_lang = PIPER_VOICES.get(self._piper_voice_key, {}).get("lang", "")
        locale_lang = i18n_coherence.primary_subtag(i18n_active.get_active_bundle().code)
        if voice_lang and locale_lang and voice_lang != locale_lang:
            self._piper_locale_mismatch_notified = True
            self._log(
                "Piper fallback engaged with a voice/locale language mismatch "
                f"(voice={voice_lang!r} locale={locale_lang!r}); offline audio "
                "will not match the active locale.",
                level="warning",
            )
            self.ui_callback(i18n_coherence.PIPER_VOICE_LOCALE_MISMATCH)

    def _generar_dialogo(
        self,
        contexto,
        source: str = "direct",
        *,
        commit_history: bool = True,
        log_prefix: str = "LLM",
        history_text: Optional[str] = None,
    ) -> str:
        request_model = self.current_model
        self._log(f"Analizando contexto con {request_model}...")
        try:
            messages = []

            if self.use_system_role:
                messages.append({'role': 'system', 'content': self.system_prompt})

            # Take a consistent snapshot of historial and (for direct-path) the
            # digest block under _history_lock so that a concurrent _commit_history
            # call from the agenda speaker daemon cannot mutate the deque while we
            # are iterating it (RuntimeError: deque mutated during iteration).
            # The lock is held ONLY for the fast snapshot + build_block reads;
            # it is released before any I/O or the Ollama call.
            with self._history_lock:
                history_snapshot = list(self.historial)
                if source == "direct":
                    digest_block = self._memory_digest.build_block(
                        sanitize_fn=self._sanitize_history_context,
                        line_format=i18n_active.digest_line_format(),
                        unit_singular=i18n_active.digest_unit_singular(),
                        unit_plural=i18n_active.digest_unit_plural(),
                    )
                else:
                    digest_block = ""
                # Slice 5 (R9): snapshot the profile id under the lock
                # (engine state protected by _history_lock) — the actual
                # store READ happens AFTER the lock releases, below,
                # since memorias retrieval is disk I/O and _history_lock
                # must never be held during I/O (same rule as capture).
                # Candidate 1: gate site 1 of 3 — direct AND ptt snapshot.
                if source in _MEMORIA_INJECT_SOURCES:
                    memorias_profile_id = self._current_profile_id
                else:
                    memorias_profile_id = None

            # Rebuild a fresh {role, content} per entry — never append by
            # reference and never pop/mutate — so the stored `source` tag
            # (history_source_tag_20260629) is projected away before the dicts
            # reach ollama.chat, and the live deque entries keep their tag.
            for msg in history_snapshot:
                messages.append({'role': msg['role'], 'content': msg['content']})

            # Slice 5 (R9): memorias retrieval + injection for direct and ptt
            # turns (candidate 1: gate site 2 of 3). Store I/O happens here,
            # AFTER _history_lock released above (the store's own
            # READ_TIMEOUT_SECONDS bounds the read). Fail-open to "" on any
            # error — a retrieval failure must never break a turn.
            memorias_block = ""
            if source in _MEMORIA_INJECT_SOURCES and MEMORIAS_ENABLED and memorias_profile_id:
                memorias_block = self._build_memorias_injection_block(memorias_profile_id, contexto)

            # Editorial direct-mode enrichment: inject matching ARMED card context for
            # host-direct queries. NON-CONSUMING — card stays ARMED for the agenda path.
            # Never inject for chat/aggregator-driven sources.
            editorial_block = ""
            if source == "direct":
                provider = self.direct_editorial_context_provider
                if provider is not None:
                    try:
                        editorial_block = provider(contexto) or ""
                    except Exception:
                        editorial_block = ""

            if editorial_block:
                enriched = f"{contexto}\n\n{editorial_block}"
                logger.info(
                    "editorial direct context injected (source=%s, len=%d)",
                    source,
                    len(editorial_block),
                )
            else:
                enriched = contexto

            # D3 — digest injection: only for direct-path prompts, never agenda.
            # digest_block was already computed under _history_lock above.
            # E3b: Wrap in explicit read-only delimiter so the LLM cannot mistake
            # ledger lines for instructions (structural isolation, language-agnostic).
            if source == "direct":
                if digest_block:
                    wrapped_digest = (
                        i18n_active.memory_block_open() + "\n"
                        + digest_block
                        + "\n" + i18n_active.memory_block_close()
                    )
                    enriched = f"{wrapped_digest}\n\n{enriched}"
                    logger.debug("L1 digest injected into direct prompt (len=%d)", len(digest_block))

            # Memorias block (R9) is PREPENDED before the digest wrap — it
            # must appear earlier in the prompt than <memoria_de_fondo>.
            # Candidate 1: gate site 3 of 3 — own sibling gate (no longer
            # nested inside `source == "direct"`), same pattern as
            # _PERSONALIZATION_INJECT_SOURCES below, so ptt turns receive it.
            if source in _MEMORIA_INJECT_SOURCES and memorias_block:
                enriched = f"{memorias_block}\n\n{enriched}"

            # Personalization block (kira_personalization_onboarding_20260705,
            # design §2): own gate, NOT nested inside `source == "direct"`
            # above, so ptt also qualifies. Prepended BEFORE memorias_block so
            # it ends up FIRST in the final message (stable-before-volatile,
            # same rationale as the memorias-before-digest comment above).
            # Fail-open: a raising build must never break a turn.
            if source in _PERSONALIZATION_INJECT_SOURCES and PERSONALIZATION_ENABLED:
                try:
                    personalization_block = personalization.build_injection_block(
                        self._sanitize_history_context
                    )
                except Exception:
                    personalization_block = ""
                if personalization_block:
                    enriched = f"{personalization_block}\n\n{enriched}"

            if self.use_system_role:
                messages.append({'role': 'user', 'content': enriched})
            else:
                prompt_completo = f"{self.system_prompt}\n\n[{i18n_active.user_message_label()}]: {enriched}"
                messages.append({'role': 'user', 'content': prompt_completo})

            # Layer 1+2: discover the model's native context window (cached, free
            # after first call — also covers the name-heuristic short-circuit gap)
            # but budget against OpenCohost's effective runtime cap so large
            # native windows do not disable prompt eviction or over-allocate KV.
            self._discover_model_ctx(request_model)
            _native_ctx = self._model_ctx_limit.get(request_model, CTX_FALLBACK_DEFAULT)
            _effective_ctx = self._resolve_effective_ctx_limit(request_model, _native_ctx)
            messages, _ctx_evicted = context_budget.apply_char_budget(
                messages,
                ctx_limit=_effective_ctx,
                max_output_tokens=LLM_MAX_TOKENS,
                safety_factor=CHAR_BUDGET_SAFETY_FACTOR,
            )
            if _ctx_evicted > 0:
                self._log(
                    f"ctx_budget_gate: evicted {_ctx_evicted} pair(s) from messages "
                    f"(model={request_model}, native_ctx={_native_ctx}, effective_ctx={_effective_ctx})",
                    level="warning",
                )

            opciones_llm = {
                'temperature': LLM_TEMPERATURE,
                'top_p': LLM_TOP_P,
                'num_predict': LLM_MAX_TOKENS,
                'num_ctx': _effective_ctx,
            }

            if "gemma" in request_model.lower():
                opciones_llm.pop('num_ctx', None)
                opciones_llm['temperature'] = 0.7

            if self._resolve_reasoning_classification(request_model):
                opciones_llm.pop('num_predict', None)
                self._log("Modelo de razonamiento detectado. Límite de tokens removido.", level="debug")

            # FIX 1 — chat-reactive anti-repetition sampling brake. Gated to
            # source=="chat" (RF3 viewer chat, agenda HANDLE_CHAT, default-enqueue
            # chat); NEVER applied to direct/ptt/accumulated/kira-agenda. Only ADDS
            # keys, so non-chat options stay byte-identical. See the event_taxonomy
            # track for the source-disambiguation follow-up.
            if source == "chat":
                opciones_llm["repeat_penalty"] = CHAT_REPEAT_PENALTY
                opciones_llm["presence_penalty"] = CHAT_PRESENCE_PENALTY
                opciones_llm["frequency_penalty"] = CHAT_FREQUENCY_PENALTY

            start_llm = time.time()
            max_intentos = 2
            raw_content = ""
            respuesta = None
            chat_timeout = self._resolve_chat_watchdog_timeout(request_model)
            
            for intento in range(max_intentos):
                try:
                    respuesta = self._ollama_chat_with_watchdog(
                        timeout=chat_timeout,
                        model=request_model,
                        messages=messages,
                        keep_alive=LLM_KEEP_ALIVE,
                        options=opciones_llm
                    )
                except Exception as e:
                    if self._is_watchdog_timeout_error(e):
                        self._recover_from_stalled_inference(
                            request_model=request_model,
                            source=source,
                            timeout=chat_timeout,
                        )
                        return ""
                    if not self._is_ollama_transport_error(e):
                        raise
                    self._last_llm_failure = {
                        "model": self.current_model,
                        "source": source,
                        "attempt": intento + 1,
                        "reason": type(e).__name__,
                        "message": str(e),
                    }
                    self._log(
                        f"ERROR Ollama chat ({type(e).__name__}) intento {intento+1}/{max_intentos}: {e}",
                        level="error",
                    )
                    logger.warning(
                        "Ollama chat transport failure: model=%s source=%s attempt=%s/%s",
                        request_model,
                        source,
                        intento + 1,
                        max_intentos,
                        exc_info=True,
                    )
                    return ""
                
                msg_obj = respuesta.get('message', {})
                if isinstance(msg_obj, dict):
                    raw_content = msg_obj.get('content', '')
                    thinking = msg_obj.get('thinking', '')
                else:
                    raw_content = getattr(msg_obj, 'content', '')
                    thinking = getattr(msg_obj, 'thinking', '')

                if thinking:
                    logger.debug(f"Pensamiento interno detectado ({len(thinking)} chars)")

                # Layer 3 reactive trim: an empty response whose prompt_eval_count
                # plateaued at/near the context ceiling is Ollama's silent input-
                # overflow signal. Drop the oldest in-flight pairs and retry ONCE
                # (intento==0 guard). Inserted BEFORE the reasoning-model branch so
                # trimming context wins over removing the output-token cap. Delegates
                # the int-threshold comparison to context_budget.is_overflow_signal.
                _pec = getattr(respuesta, "prompt_eval_count", 0) or 0
                _ctx_limit_now = _effective_ctx
                if intento == 0 and context_budget.is_overflow_signal(
                    raw_content, _pec, _ctx_limit_now, CTX_OVERFLOW_SIGNAL_RATIO
                ):
                    _dropped = context_budget.trim_messages_reactive(messages, n_pairs=3)
                    self._log(
                        f"ctx_overflow_reactive: prompt_eval_count={_pec} >= "
                        f"{_ctx_limit_now}*{CTX_OVERFLOW_SIGNAL_RATIO:.2f}; dropped "
                        f"{_dropped} pair(s) from in-flight messages, retrying.",
                        level="warning",
                    )
                    continue

                # Layer 2 self-heal: empty visible content + internal thinking means a
                # reasoning model spent its budget thinking and hit the num_predict cap.
                # Drop the cap, remember the classification, and retry uncapped.
                if not raw_content.strip() and thinking and 'num_predict' in opciones_llm:
                    opciones_llm.pop('num_predict', None)
                    self._reasoning_model_cache[request_model] = True
                    self._log(
                        f"Auto-corrección: {request_model} devolvió contenido vacío con "
                        f"pensamiento interno; removiendo límite de tokens y reintentando.",
                        level="warning",
                    )
                    continue

                if raw_content.strip():
                    break
                
                self._log(f"⚠️ Intento {intento+1}: {request_model} devolvió respuesta vacía. Reintentando...", level="warning")
                time.sleep(0.5)

            dialogo = raw_content.strip().strip('\x00\ufeff')
            elapsed = time.time() - start_llm

            # Layer 4 observability: log prompt-window utilization on every populated
            # response and raise a UI pressure signal when it crosses the high mark.
            _pec_final = (getattr(respuesta, "prompt_eval_count", 0) or 0) if respuesta is not None else 0
            if _pec_final > 0:
                _util = context_budget.utilization(_pec_final, _effective_ctx)
                # measure-first (prompt_efficiency_kvcache_20260629): log the prefill
                # vs decode wall-time split so the prefill fraction of TTFT is observable
                # before any Lever-1 prefix-stability rewrite. Ollama reports ns.
                _prefill_ms = (getattr(respuesta, "prompt_eval_duration", 0) or 0) / 1e6
                _decode_ms = (getattr(respuesta, "eval_duration", 0) or 0) / 1e6
                _ec_final = getattr(respuesta, "eval_count", 0) or 0
                logger.info(
                    "ctx_utilization: model=%s prompt_eval_count=%d native_ctx=%d effective_ctx=%d ratio=%.3f "
                    "prefill_ms=%.0f decode_ms=%.0f eval_count=%d source=%s",
                    request_model, _pec_final, _native_ctx, _effective_ctx, _util,
                    _prefill_ms, _decode_ms, _ec_final, source,
                )
                if _util >= CTX_PRESSURE_HIGH_THRESHOLD:
                    logger.warning(
                        "ctx_pressure_high: utilization=%.1f%% model=%s source=%s",
                        _util * 100, request_model, source,
                    )
                    self.ui_callback("ctx_pressure_high")

            # MODEL_TRACE: audit which model was used for this generation
            generation_model = request_model
            desired = self._desired_model
            active = self.current_model
            loaded = self._loaded_model or "unknown"
            trace_msg = (
                f"[MODEL_TRACE] desired={desired} active={active} "
                f"loaded={loaded} generation={generation_model} "
                f"profile={self._current_profile_name} source={source}"
            )
            if desired != active or active != loaded or loaded != generation_model:
                self._log(f"[MODEL_MISMATCH_WARNING] {trace_msg}", level="warning")
            else:
                logger.info(f"Motor: {trace_msg}")

            if source.startswith("kira-agenda"):
                dialogo = self._sanitize_agenda_output(dialogo)
                transformer = getattr(self, "agenda_output_transformer", None)
                if transformer is not None:
                    try:
                        dialogo = transformer(dialogo)
                    except Exception:
                        logger.exception("Agenda output transformer failed")

            if not dialogo:
                self._log(f"⚠️ {request_model} devolvió respuesta vacía ({elapsed:.2f}s).", level="warning")
                logger.warning(f"Empty LLM response. Raw repr: {repr(raw_content)}")
                return ""

            self._mark_model_generation_success(request_model)
            allowed, guard_reason = output_guard(dialogo, source=source)
            if not allowed:
                self._log(f"Salida bloqueada por guardrail: {guard_reason}", level="warning")
                fallback = self._guardrail_fallback_line(source)
                if fallback:
                    self._log("Guardrail fallback: usando línea neutral sin LLM.")
                    return fallback
                return ""

            self._last_llm_failure = None

            # FIX 2 — chat-reactive reactive guard. Suppress repetition the sampling
            # brake let through (verbatim dups + synonym-swap templates) BEFORE it
            # reaches TTS or gets committed into the history window that feeds the
            # next prompt. Reuses the proven neutral-fallback seam. Gated to
            # source=="chat" so agenda/direct/ptt/LiveVoice paths are untouched.
            if source == "chat":
                recent_outputs = [
                    m.get("content", "")
                    for m in history_snapshot
                    if isinstance(m, dict) and m.get("role") == "assistant"
                ][-REPETITION_CONFIG.window:]
                repetition = detect_repetition(dialogo, recent_outputs)
                if repetition.is_repetitive:
                    self._log(
                        f"Repetición de chat bloqueada ({repetition.reason}); "
                        f"usando línea neutral sin LLM.",
                        level="warning",
                    )
                    return self._guardrail_fallback_line(source) or ""

            if source.startswith("kira-agenda") and commit_history and not self._accept_agenda_output(dialogo):
                self._log(f"Agenda: salida rechazada ({self._format_agenda_rejection()}).", level="warning")
                return ""

            if commit_history:
                self.log_queue.put(f"\n🧠 [Kira]: {dialogo} ({elapsed:.2f}s)\n")
                # FIX-B2: emission moved to the speak site (_ejecutar_inferencia)
                # so guardrail/repetition fallbacks — which return EARLIER than
                # this block yet are still spoken — also update last-reply.
            preview = dialogo[:200] if _debug_enabled() else f"len={len(dialogo)}"
            logger.info(f"{log_prefix} response ({elapsed:.2f}s): {preview}")

            if commit_history:
                self._commit_history(contexto, dialogo, source=source, history_text=history_text)

            return dialogo

        except Exception as e:
            self._log(f"ERROR Ollama: {e}", level="error")
            logger.exception("Error en inferencia LLM")
            return ""

    def _create_ollama_chat_client(self, ollama_module):
        client_factory = getattr(ollama_module, "Client", None)
        if client_factory is None:
            return None
        try:
            return client_factory(timeout=OLLAMA_CHAT_TIMEOUT)
        except TypeError as e:
            self._log(f"Ollama Client no soporta timeout de chat; usando cliente por defecto: {e}", level="warning")
            return None

    def _create_ollama_scout_client(self, ollama_module):
        """Dedicated short-timeout client for the Topic Scout.

        Built with ``LLM_SCOUT_TIMEOUT`` (not the 180s chat timeout) so that when
        the HTTP timeout expires the socket closes and Ollama cancels the
        generation, releasing the single runner slot in ~LLM_SCOUT_TIMEOUT.
        """
        client_factory = getattr(ollama_module, "Client", None)
        if client_factory is None:
            return None
        try:
            return client_factory(timeout=LLM_SCOUT_TIMEOUT)
        except TypeError as e:
            self._log(f"Ollama Client no soporta timeout de scout; usando cliente por defecto: {e}", level="warning")
            return None

    def _ollama_chat(self, **kwargs):
        client = self._ollama_chat_client or self.ollama
        return client.chat(**kwargs)

    def _ollama_scout_chat(self, **kwargs):
        client = self._ollama_scout_client or self.ollama
        return client.chat(**kwargs)

    def _ollama_chat_with_watchdog(self, *, timeout: float, chat_callable=None, **kwargs):
        result = {}
        done = threading.Event()
        call = chat_callable or self._ollama_chat

        def worker() -> None:
            try:
                result["response"] = call(**kwargs)
            except Exception as exc:
                result["error"] = exc
            finally:
                done.set()

        thread = threading.Thread(
            target=worker,
            name=f"OllamaChatWatchdog-{uuid.uuid4().hex[:8]}",
            daemon=True,
        )
        thread.start()
        if not done.wait(timeout=max(0.1, float(timeout))):
            raise TimeoutError(f"watchdog_timeout:{timeout:.2f}s")
        if "error" in result:
            raise result["error"]
        return result.get("response")

    def _resolve_chat_watchdog_timeout(self, request_model: str) -> float:
        if self._awaiting_first_success_after_switch and request_model == self.current_model:
            return self._post_switch_watchdog_timeout
        return self._inference_watchdog_timeout

    # ── Topic Scout (topic_scout_llm_20260629) ──────────────────────────────
    def _scout_render_history(self, history_snapshot: list) -> list[str]:
        """Compact, sanitized rendering of the most-recent LIVE turns.

        Inherits the direct-path gate (``_sanitize_history_context``); the scout
        needs topic words only, so usernames and injection phrases are scrubbed.
        """
        # Host-only (history_source_tag_20260629 Task C/D): filter the FULL
        # snapshot to genuine HOST turns (direct/ptt) FIRST, then take the last N,
        # so the scout sees the last N real host turns — not N mixed turns thinned
        # to however few host turns happen to survive. Untagged/viewer/agenda
        # entries (source absent or not in the set) are excluded by `.get`.
        host_only = [
            msg for msg in history_snapshot
            if isinstance(msg, dict) and msg.get("source") in {"direct", "ptt"}
        ]
        recent = host_only[-LLM_SCOUT_HISTORY_MSGS:]
        lines: list[str] = []
        for msg in recent:
            if not isinstance(msg, dict):
                continue
            content = self._scout_scrub_text(
                self._sanitize_history_context(str(msg.get("content", "")))
            )
            if not content:
                continue
            speaker = "Host" if msg.get("role") == "user" else "Kira"
            lines.append(f"{speaker}: {content}")
        return lines

    @staticmethod
    def _scout_scrub_text(text: str) -> str:
        """Strip @mentions and injection-marker phrases from a render line.

        The scout only needs topic words — never usernames or injected
        instructions — so it scrubs harder than the verbatim direct path.
        """
        text = re.sub(r"@\w+", "", text)
        return _strip_injection_markers(text)

    def _scout_extract_text(self, response) -> str:
        """Pull the assistant content out of a chat response (dict or object)."""
        if response is None:
            return ""
        msg = response["message"] if isinstance(response, dict) else getattr(response, "message", None)
        if msg is None:
            return ""
        content = msg.get("content") if isinstance(msg, dict) else getattr(msg, "content", "")
        return content or ""

    def _scout_parse_titles(self, text: str, input_block: str) -> list[dict]:
        """Parse adjacent topic titles: filter preamble/echo, sanitize, dedupe, cap 3."""
        if not text:
            return []
        from opencohost.smart_aggregator.kira_agenda_controller import KiraAgendaController

        input_cf = input_block.casefold()
        results: list[dict] = []
        seen: set[str] = set()
        for raw_line in text.splitlines():
            line = raw_line.strip().strip("-•*\t\"'").strip()
            if not line:
                continue
            # Preamble filter: conversational lead-ins ("Claro, acá van:") and
            # over-long lines are not titles.
            if line.endswith(":"):
                continue
            if len(line.split()) > SCOUT_TITLE_MAX_WORDS:
                continue
            # Echo filter: a title that merely repeats a seed term is not adjacent.
            if line.casefold() in input_cf:
                continue
            # Title gate (emoji/code/length) — drop silently on rejection.
            try:
                title = KiraAgendaController.sanitize_topic_text(line, field="title")
            except ValueError:
                continue
            slug = title.casefold()
            if slug in seen:
                continue
            seen.add(slug)
            results.append({"title": title, "source": "scout", "confidence": "LOW"})
            if len(results) >= 3:
                break
        return results

    def scout_digest(self) -> list[dict]:
        """Topic Scout: idle-time adjacent-topic suggester.

        Best-effort and FULLY self-contained: every internal failure returns []
        so it can never break the rule-based ``generate_suggestions`` it runs
        alongside. Returns DRAFTED-ready dicts; NEVER speaks or persists.
        """
        try:
            if not SCOUT_ENABLED:
                return []
            # Gate 3: no model resident, or a switch pending/in-flight -> skip
            # (never cold-load; use the RESIDENT model, not the desired one).
            if self._loaded_model is None:
                return []
            if self._pending_model_switch or self._awaiting_first_success_after_switch:
                return []
            # Gate 1: re-read idle state immediately before the call.
            if self.is_processing or self.is_speaking:
                return []
            # Gate 2: real work queued but not yet started would serialize behind
            # an in-flight scout on the single runner — abort on ANY pending item.
            if self.has_pending_priority_before(SCOUT_QUEUE_FLOOR):
                return []
            # Value gate (efficiency, not safety): a thinking-capable model burns
            # the 64-token budget on <think> and returns nothing useful.
            if self._check_capabilities_reasoning(self._loaded_model):
                return []
            with self._history_lock:
                history_snapshot = list(self.historial)
            lines = self._scout_render_history(history_snapshot)
            if len(lines) < LLM_SCOUT_MIN_DIGEST_LINES:
                return []
            input_block = "\n".join(lines)
            # Fresh-input gate: hash the LIVE snapshot; skip an identical input.
            # Cached on EVERY attempt that reaches here (even when the call returns
            # []), so a no-op scout does not re-fire until the conversation moves.
            input_hash = hashlib.sha256(input_block.encode("utf-8")).hexdigest()
            if input_hash == self._scout_last_input_hash:
                return []
            self._scout_last_input_hash = input_hash
            if self._ollama_scout_client is None:
                self._ollama_scout_client = self._create_ollama_scout_client(self.ollama)
            prompt = i18n_active.scout_prompt().format(digest_block=input_block)
            response = self._ollama_chat_with_watchdog(
                timeout=LLM_SCOUT_TIMEOUT + 2,
                chat_callable=self._ollama_scout_chat,
                model=self._loaded_model,
                messages=[{"role": "user", "content": prompt}],
                options={
                    "num_predict": LLM_SCOUT_NUM_PREDICT,
                    "temperature": LLM_SCOUT_TEMPERATURE,
                },
                keep_alive=LLM_KEEP_ALIVE,
            )
            text = self._scout_extract_text(response)
            return self._scout_parse_titles(text, input_block)
        except Exception:
            # Total internal isolation — a scout failure must never escape.
            return []

    def memory_inspector_snapshot(self) -> dict:
        """Read-only, privacy-gated snapshot of session memory for the
        "Memoria de Kira" UI inspector (cards_memory_readonly_panels_20260701).

        Snapshot-then-release (precedent: scout_digest): copies historial
        entries + digest stats to plain dicts under _history_lock, releases
        the lock, then formats. Never mutates historial or the digest.

        Content policy (fail-closed, no cross-module string heuristics):
          - user-slot entries: 'content' key present ONLY when source == "direct"
          - assistant-slot entries: 'content' key present when source is in
            _DIGEST_CAPTURE_SOURCES ({"direct", "ptt"}) — Kira's own on-air
            words are safe to show even for a ptt-sourced turn.
          - everything else (chat/accumulated/kira-agenda*/unknown/missing
            source): NO 'content' key at all.

        Returns:
            {
              "entries": [{"turn_index", "role", "source", "content_chars", ["content"]}],
              "source_breakdown": collections.Counter over entry sources,
              "digest": {"line_count", "total_chars", "max_chars"} — stats
                  only, never digest line text (compacted lines are
                  unattributable and can carry ptt-template junk pre-T1.1).
            }
        """
        with self._history_lock:
            raw_entries = list(self.historial)
            digest_lines = list(self._memory_digest.lines)
            digest_max_chars = self._memory_digest._max_chars

        entries: list[dict] = []
        for idx, raw_entry in enumerate(raw_entries):
            role = raw_entry.get("role")
            source = raw_entry.get("source")
            content = raw_entry.get("content", "") or ""
            entry = {
                "turn_index": idx,
                "role": role,
                "source": source,
                "content_chars": len(content),
            }
            show_content = (
                (role == "user" and source == "direct")
                or (role == "assistant" and source in _DIGEST_CAPTURE_SOURCES)
            )
            if show_content:
                entry["content"] = content
            entries.append(entry)

        return {
            "entries": entries,
            "source_breakdown": Counter(e["source"] for e in entries),
            "digest": {
                "line_count": len(digest_lines),
                "total_chars": sum(len(line) for line in digest_lines),
                "max_chars": digest_max_chars,
            },
        }

    @staticmethod
    def _is_watchdog_timeout_error(exc: Exception) -> bool:
        return isinstance(exc, TimeoutError) and str(exc).startswith("watchdog_timeout:")

    def _mark_model_generation_success(self, model: str) -> None:
        self._last_known_good_model = model
        if self.current_model == model:
            self._awaiting_first_success_after_switch = False

    def _recover_from_stalled_inference(self, *, request_model: str, source: str, timeout: float) -> None:
        message = f"watchdog_timeout after {timeout:.2f}s"
        self._last_llm_failure = {
            "model": request_model,
            "source": source,
            "attempt": 1,
            "reason": "watchdog_timeout",
            "message": message,
        }
        self._log(
            f"Timeout de inferencia con {request_model} tras {timeout:.2f}s. Iniciando recuperación...",
            level="error",
        )
        logger.warning(
            "Inference watchdog timeout: model=%s source=%s timeout=%.2fs",
            request_model,
            source,
            timeout,
        )

        if self._pending_model_switch:
            self._pending_switch_next_at = time.monotonic()
            self._log(
                f"Recuperación: se aplicará el modelo pendiente {self._pending_model_switch} al quedar libre.",
                level="warning",
            )
        else:
            self._rollback_to_last_known_good_model(request_model)

        self.ui_callback("llm_timeout_recovered")

    def _rollback_to_last_known_good_model(self, failed_model: str) -> bool:
        rollback_model = self._last_known_good_model
        if not rollback_model or rollback_model == failed_model:
            return False

        self._log(
            f"Recuperación: rollback automático de {failed_model} a {rollback_model}.",
            level="warning",
        )
        if self._apply_model_switch(rollback_model, persist_source="recovery_rollback"):
            self._awaiting_first_success_after_switch = False
            return True
        if self.current_model != rollback_model:
            logger.warning(
                "Rollback after stalled inference failed: failed_model=%s rollback_model=%s",
                failed_model,
                rollback_model,
            )
            return False
        self._awaiting_first_success_after_switch = False
        return True

    @staticmethod
    def _uses_reasoning_token_budget(model: str) -> bool:
        """Return whether a model should avoid fixed num_predict limits.

        Qwen3 and Gemma E models can spend part of the budget on internal
        reasoning. A hard low cap can yield empty or visibly truncated answers.
        """
        name = model.lower()
        return any(marker in name for marker in ("qwen3", "e2b", "e4b", "think"))

    def _resolve_effective_ctx_limit(self, model: str, native_ctx: int) -> int:
        """Return OpenCohost's runtime ctx cap for ``model`` without changing discovery."""
        tier = None
        tiers = getattr(self, "llm_tiers", None)
        if tiers is not None:
            active_model = tiers.active_model
            if active_model == model:
                tier = tiers.active_tier
            else:
                for candidate_tier, candidate_model in tiers.config.as_dict().items():
                    if candidate_model == model:
                        tier = candidate_tier
                        break
        tier_cap = LLM_TIER_EFFECTIVE_CTX_CAPS.get(tier, CTX_FALLBACK_DEFAULT)
        try:
            native = int(native_ctx)
        except (TypeError, ValueError):
            native = CTX_FALLBACK_DEFAULT
        if native <= 0:
            native = CTX_FALLBACK_DEFAULT
        return min(native, tier_cap)

    def _discover_model_ctx(self, model: str) -> int:
        """Layer 1: return ``model``'s native context length from ``ollama.show``.

        Sole owner of the ``ollama.show`` RPC for both context discovery and the
        reasoning-capability check. Calls ``ollama.show`` once per model lifetime,
        caches the parsed context length in ``self._model_ctx_limit`` and the raw
        response in ``self._ctx_show_cache`` so ``_check_capabilities_reasoning``
        can read ``capabilities`` from the same response. Any failure degrades to
        ``CTX_FALLBACK_DEFAULT`` — never a crash.
        """
        cache = getattr(self, "_model_ctx_limit", None)
        if cache is None:
            cache = {}
            self._model_ctx_limit = cache
        cached = cache.get(model)
        if cached is not None:
            return cached
        try:
            resp = self._fetch_show(model)
            ctx = context_budget.parse_model_ctx(resp, fallback=CTX_FALLBACK_DEFAULT)
        except Exception:
            ctx = CTX_FALLBACK_DEFAULT
        cache[model] = ctx
        return ctx

    def _fetch_show(self, model: str):
        """Fetch and memoise the raw ``ollama.show`` response for ``model``.

        Shared by ``_discover_model_ctx`` (context length) and
        ``_check_capabilities_reasoning`` (capabilities) so a single RPC serves
        both. The raw response is cached in ``self._ctx_show_cache``.
        """
        cache = getattr(self, "_ctx_show_cache", None)
        if cache is None:
            cache = {}
            self._ctx_show_cache = cache
        if model in cache:
            return cache[model]
        import ollama
        resp = ollama.show(model)
        cache[model] = resp
        return resp

    def _check_capabilities_reasoning(self, model: str) -> bool:
        """Layer 1: ask Ollama whether ``model`` advertises a 'thinking' capability.

        Augments (does not replace) the name heuristic so reasoning models whose
        name lacks a known marker (e.g. gemma4:12b) are still detected. Shares the
        single ``ollama.show`` RPC owned by ``_discover_model_ctx`` (via
        ``_fetch_show``) and populates ``self._model_ctx_limit[model]`` as a side
        effect. Wrapped defensively: any error, missing field, or older Ollama
        without the capabilities field degrades to False — never a crash.
        """
        try:
            # Populate the ctx-limit cache from the same RPC (RPC-ownership §Layer 1).
            self._discover_model_ctx(model)
            info = self._fetch_show(model)
            caps = info.get("capabilities") if isinstance(info, dict) else getattr(info, "capabilities", None)
            return bool(caps) and "thinking" in caps
        except Exception:
            return False

    def _resolve_reasoning_classification(self, model: str) -> bool:
        """Resolve whether ``model`` should drop the num_predict cap.

        Cache first (Layer 3); on miss combine the name heuristic with the Ollama
        capabilities check (Layer 1). The name heuristic short-circuits the ``or``
        so a known reasoning model never pays the ollama.show RPC. The resolved
        value is cached per model name.
        """
        cached = self._reasoning_model_cache.get(model)
        if cached is not None:
            return cached
        result = self._uses_reasoning_token_budget(model) or self._check_capabilities_reasoning(model)
        self._reasoning_model_cache[model] = result
        return result

    @staticmethod
    def _is_ollama_transport_error(exc: Exception) -> bool:
        if isinstance(exc, (TimeoutError, ConnectionError, socket.timeout, requests.exceptions.RequestException)):
            return True
        class_names = {cls.__name__ for cls in type(exc).__mro__}
        return any("Timeout" in name or "Connect" in name or "Connection" in name for name in class_names)

    def _accept_agenda_output(self, dialogo: str) -> bool:
        validator = getattr(self, "agenda_output_validator", None)
        if validator is None:
            return True
        try:
            return bool(validator(dialogo))
        except Exception:
            logger.exception("Agenda output validator failed")
            return False

    def _preview_accept_agenda_output(self, dialogo: str) -> bool:
        validator = getattr(self, "agenda_output_preview_validator", None)
        if validator is None:
            validator = getattr(self, "agenda_output_validator", None)
        if validator is None:
            return True
        try:
            return bool(validator(dialogo))
        except Exception:
            logger.exception("Agenda preview output validator failed")
            return False

    def _format_agenda_rejection(self) -> str:
        """Return a compact rejection reason string for logging (Phase 0a)."""
        ctl = getattr(self, "agenda_controller", None)
        if ctl is None or not ctl.rejection_log:
            return "guardrails"
        last = ctl.rejection_log[-1]
        grd = last.get("guardrail", last.get("error", "UNKNOWN"))
        ov = last.get("overlap_pct")
        phrase = last.get("matched_phrase")
        if ov is not None:
            return f"{grd} (overlap {ov}%)"
        if phrase:
            return f"{grd} (\"{phrase[:50]}...\")"
        return str(grd)

    def _record_accepted_agenda_output(self, dialogo: str) -> None:
        recorder = getattr(self, "agenda_output_recorder", None)
        if recorder is None:
            return
        try:
            recorder(dialogo)
        except Exception:
            logger.exception("Agenda output recorder failed")

    def _commit_history(
        self,
        contexto: str,
        dialogo: str,
        *,
        source: str = "direct",
        history_text: Optional[str] = None,
    ) -> None:
        if source.startswith("kira-agenda"):
            safe_context = "[agenda segura: prompt interno omitido]"
        else:
            # agenda_ptt_commit_raw_text: when a caller supplies the honest
            # turn text (PTT path), store THAT instead of the raw contexto
            # (which, for agenda-driven turns, is the full prompt template).
            # Sanitizer pipeline is unchanged — it applies to whatever text
            # ends up in safe_context.
            safe_context = self._sanitize_history_context(history_text if history_text else contexto)

        # T3 — staged memorias draft (pure strings, no I/O). Built while
        # _history_lock is held below; upsert_draft is called AFTER the lock
        # releases (upsert_draft must never run with _history_lock held).
        pending_memoria_capture: Optional[tuple[str, str, str, str, str]] = None

        # Hold _history_lock around the eviction-capture + both appends so
        # concurrent callers (worker loop and agenda speaker daemon) cannot
        # interleave a read of historial[0]/[1] with an append from another thread.
        with self._history_lock:
            # D1 — eviction capture: before appending the new turn, check whether
            # the deque is at maxlen. If so, the oldest pair (user+assistant at
            # indices 0 and 1) will be auto-evicted. Capture them as one ledger line.
            # We only capture non-agenda turns; agenda turns already masked to
            # "[agenda segura…]" so their "eviction" produces no useful ledger line.
            maxlen = self.historial.maxlen  # always HISTORY_MAX_TURNS * 2
            if maxlen is not None and len(self.historial) >= maxlen and not source.startswith("kira-agenda"):
                # historial is guaranteed to hold pairs (user, assistant).
                evicted_user_content = self.historial[0].get("content", "")
                evicted_asst_content = self.historial[1].get("content", "")
                # Skip capture when the evicted pair is agenda-origin (user slot is
                # the sentinel), to prevent real agenda replies from leaking into
                # the digest via the eviction path.
                evicted_is_agenda = evicted_user_content.startswith("[agenda segura")
                # D1 — source allowlist gate (fail-closed): only capture when the
                # evicted pair's own source tag is in _DIGEST_CAPTURE_SOURCES.
                # Missing or unknown source values are treated as not-capturable.
                evicted_source = self.historial[0].get("source")
                if evicted_source not in _DIGEST_CAPTURE_SOURCES:
                    logger.debug(
                        "Eviction capture skipped: source=%r not in digest allowlist",
                        evicted_source,
                    )
                elif not evicted_is_agenda:
                    ledger_line = self._build_ledger_line(
                        evicted_user_content,
                        evicted_asst_content,
                    )
                    self._memory_digest.append(ledger_line)

                    # T3 — memorias draft (R1-R4). _build_memoria_draft re-runs
                    # the source+agenda checks internally (redundant here, since
                    # the elif above already guarantees them — but that makes it
                    # the single, self-contained gate chain reusable by F4 flush
                    # (slice 4), which iterates the LIVE window's pairs instead
                    # of only the evicted one). is_capturable (via
                    # derive_stable_key) stays LAST — a signal/token-count gate,
                    # never a substitute for provenance (Judge-B forward, slice
                    # 2 N1).
                    pending_memoria_capture = self._build_memoria_draft(
                        evicted_user_content, evicted_asst_content, ledger_line,
                        source=evicted_source,
                        private=self.historial[0].get("private"),
                    )

            # Append-time privacy tagging (R2/R4): both pair entries carry the
            # CURRENT switch state at the moment they enter historial. Capture
            # later reads this tag from the EVICTED entry itself (state-at-event),
            # never the switch's state at eviction time — forward-only, no
            # retro-capture and no retro-hide of an already in-flight window.
            # Snapshotted ONCE here (not re-read per append): set_memorias_private
            # does not hold _history_lock, so a concurrent flip landing between
            # the two appends must not split the pair's tag (B-S1) — the flip
            # then correctly applies forward, to the next turn's pair instead.
            priv = self._memorias_private
            self.historial.append({
                'role': 'user', 'content': safe_context, 'source': source,
                'private': priv,
            })
            self.historial.append({
                'role': 'assistant', 'content': dialogo, 'source': source,
                'private': priv,
            })

        if pending_memoria_capture is not None:
            self._capture_memoria(*pending_memoria_capture)

    def set_memorias_private(self, value: bool) -> None:
        """Session-scoped memorias capture-privacy switch (R2/R4).

        True pauses capture; False (default) resumes it. Forward-only: only
        affects pairs appended to historial AFTER this call — never retro-
        captures a paused window, never retro-hides an already-capturable
        one. UI wiring (the actual toggle control) lands in slice 7.
        """
        self._memorias_private = bool(value)

    @property
    def memorias_private(self) -> bool:
        """Current session-scoped capture-privacy switch state (slice 7 UI read)."""
        return self._memorias_private

    def _build_memoria_draft(
        self,
        user_content: str,
        asst_content: str,
        ledger_line: str,
        *,
        source: Optional[str],
        private,
    ) -> Optional[tuple[str, str, str, str, str]]:
        """Pure, no-I/O: full gate chain + draft builder for one (user,
        assistant) pair, or None if any gate fails.

        Single source of truth for R1-R4/RC-1, shared by eviction-capture
        (T3, above) and F4 flush (slice 4 — close-flush and profile-switch
        both iterate live-window pairs through this same chain instead of
        duplicating gate logic). MUST be called while _history_lock is held
        — reads the pair's own tags (state-at-event), never a switch's
        current state. Order: source allowlist -> not agenda-sentinel ->
        MEMORIAS_ENABLED -> private tag is False -> profile_id set ->
        significant-token minimum (is_capturable, via derive_stable_key,
        LAST — a signal gate, never a substitute for provenance, Judge-B
        forward slice 2 N1). Returns (profile_id, stable_key, title,
        content, signature) or None.
        """
        if source not in _DIGEST_CAPTURE_SOURCES:
            return None
        if user_content.startswith("[agenda segura"):
            return None
        if not MEMORIAS_ENABLED:
            return None
        if private is not False:
            return None
        profile_id = self._current_profile_id
        if profile_id is None:
            return None
        signature_text = f"{user_content} {asst_content}"
        stable_key = derive_stable_key(profile_id, signature_text)
        if stable_key is None:
            return None
        return (
            profile_id, stable_key, build_title(signature_text),
            ledger_line[:300], build_signature(signature_text),
        )

    def _capture_memoria(
        self, profile_id: str, stable_key: str, title: str, content: str, signature: str = ""
    ) -> None:
        """I/O: upsert a memoria draft. MUST be called AFTER _history_lock releases.

        Fail-open (R5): any exception is logged (ids/type only, never title
        or content — RC-8) and swallowed. A memorias write must never crash
        the calling thread (engine worker loop / agenda speaker daemon).
        """
        try:
            self._get_memoria_store().upsert_draft(
                profile_id, stable_key, title, content, signature=signature
            )
        except Exception as exc:
            logger.warning(
                "memoria capture failed (fail-open): %s profile_id=%s stable_key=%s",
                type(exc).__name__, profile_id, stable_key,
            )

    def _get_memoria_store(self) -> MemoriaStore:
        with self._memoria_store_lock:
            if self._memoria_store is None:
                self._memoria_store = MemoriaStore(MEMORIAS_DB)
            return self._memoria_store

    def _build_memorias_injection_block(self, profile_id: str, contexto: str) -> str:
        """Slice 5 (R9) — memorias retrieval + injection for the sources in
        _MEMORIA_INJECT_SOURCES (direct + ptt as of candidate 1).

        MUST be called AFTER _history_lock releases (store I/O, bounded by
        the store's own READ_TIMEOUT_SECONDS). Fail-open to "" on any error
        — a retrieval failure must never break a turn (mirrors
        _capture_memoria). Eligibility (private=0 AND inactive=0, capped) is
        enforced by MemoriaStore.list_injection_candidates; pinned policy A
        (max 2 oldest-pinned, 220-char clip, top-k floor) by
        build_injection_lines. Each line is re-sanitized at build time and
        wrapped in the i18n memorias_block_open/close delimiters.
        """
        try:
            rows = self._get_memoria_store().list_injection_candidates(profile_id)
            self._memorias_pin_counter = pinned_injection_counter(rows)
            if not rows:
                return ""
            lines = build_injection_lines(rows, contexto)
            if not lines:
                return ""
            # 4R correction round (R1): sanitize (control chars + truncation)
            # then STRIP marker phrases — scout-scrub parity; truncation alone
            # would leave an instruction-like marker verbatim in the wrapper.
            sanitized = [
                _strip_injection_markers(self._sanitize_history_context(line))
                for line in lines
            ]
            return (
                i18n_active.memorias_block_open() + "\n"
                + "\n".join(sanitized)
                + "\n" + i18n_active.memorias_block_close()
            )
        except Exception as exc:
            logger.warning(
                "memoria retrieval failed (fail-open): %s profile_id=%s",
                type(exc).__name__, profile_id,
            )
            return ""

    def _collect_flush_drafts(self) -> list[tuple[str, str, str, str, str]]:
        """F4 (slice 4) — snapshot the LIVE (un-evicted) historial window into
        memoria drafts. Pure, no-I/O. MUST be called while _history_lock is
        held: iterates historial in (user, assistant) pairs (historial only
        ever holds complete pairs — see _commit_history), running each pair
        through the exact same gate chain as eviction capture
        (_build_memoria_draft — R1-R4/RC-1). Reused by both close-flush
        (task 4.12) and profile-switch flush (task 4.14) — one snapshot
        routine, two callers.
        """
        drafts: list[tuple[str, str, str, str, str]] = []
        entries = list(self.historial)
        for i in range(0, len(entries) - 1, 2):
            user_entry, asst_entry = entries[i], entries[i + 1]
            user_content = user_entry.get("content", "")
            asst_content = asst_entry.get("content", "")
            ledger_line = self._build_ledger_line(user_content, asst_content)
            draft = self._build_memoria_draft(
                user_content, asst_content, ledger_line,
                source=user_entry.get("source"),
                private=user_entry.get("private"),
            )
            if draft is not None:
                drafts.append(draft)
        return drafts

    def flush_memorias(self, budget_seconds: float = 2.0) -> None:
        """F4 (task 4.12) — bounded flush of the live memorias window on
        clean app close (R14).

        Snapshots eligible pairs under _history_lock (same gate chain as
        eviction capture via _collect_flush_drafts), releases the lock, then
        upserts each draft on the CALLING thread under a wall-clock budget.
        Running on the calling thread (sanctioned Tk-thread exception, RC-3
        — there is no more UI to protect after close, agenda_persistence.py
        precedent) is safe specifically because it is time-bounded: stops
        early and logs ONCE (count only, never content — RC-8) if the budget
        is exceeded. Fail-open throughout: never raises, never blocks close.
        """
        try:
            with self._history_lock:
                drafts = self._collect_flush_drafts()
        except Exception:
            logger.warning("memoria close-flush snapshot failed (fail-open)")
            return
        if not drafts:
            return

        deadline = time.monotonic() + budget_seconds
        flushed = 0
        for profile_id, stable_key, title, content, signature in drafts:
            if time.monotonic() > deadline:
                logger.warning(
                    "memoria close-flush budget exceeded, stopping early: flushed=%d remaining=%d",
                    flushed, len(drafts) - flushed,
                )
                break
            try:
                self._get_memoria_store().upsert_draft(
                    profile_id, stable_key, title, content, signature=signature
                )
                flushed += 1
            except Exception as exc:
                logger.warning(
                    "memoria close-flush upsert failed (fail-open): %s profile_id=%s",
                    type(exc).__name__, profile_id,
                )

    def _dispatch_switch_flush(self, drafts: list[tuple[str, str, str, str, str]]) -> None:
        """F4 (task 4.15) — dispatch profile-switch flush upserts to a
        dedicated worker thread, fire-and-forget (RC-2/RC-3).

        Runs off the Tk thread with NO time budget (task 4.16 — unlike
        close-flush, there IS more UI to protect, so a slow disk must never
        stall it; bounding by thread placement instead of wall-clock is
        sufficient here since nothing is waiting on this thread).
        """
        if not drafts:
            return
        threading.Thread(target=self._run_switch_flush, args=(drafts,), daemon=True).start()

    def _run_switch_flush(self, drafts: list[tuple[str, str, str, str, str]]) -> None:
        """Worker-thread body for _dispatch_switch_flush. Fail-open; partial
        failures log a COUNT only, never content (RC-8)."""
        failed = 0
        for profile_id, stable_key, title, content, signature in drafts:
            try:
                self._get_memoria_store().upsert_draft(
                    profile_id, stable_key, title, content, signature=signature
                )
            except Exception:
                failed += 1
        if failed:
            logger.warning(
                "memoria switch-flush partial failure (fail-open): failed=%d of %d",
                failed, len(drafts),
            )

    @staticmethod
    def _first_words(text: str, max_words: int = 8) -> str:
        """Return the first N words of text, stripped of leading/trailing whitespace."""
        words = text.split()
        return " ".join(words[:max_words])

    @staticmethod
    def _first_sentence(text: str) -> str:
        """Return the first sentence of text (split on . ! ?)."""
        # Split on sentence-ending punctuation followed by whitespace or end-of-string
        match = re.search(r'[.!?](?:\s|$)', text)
        if match:
            return text[: match.start() + 1].strip()
        return text.strip()

    @classmethod
    def _build_ledger_line(cls, user_text: str, asst_text: str) -> str:
        """Compact an evicted turn into one ledger line body (no [hace N] prefix).

        The "[hace N turno(s)]" prefix is rendered at build time by
        MemoryDigest.build_block() so the counter always reflects distance-from-now.

        Format: contexto: <first words> → Kira: <first sentence>
        """
        user_summary = cls._first_words(user_text)
        kira_summary = cls._first_sentence(asst_text)
        ctx_label = i18n_active.ledger_context_label()
        kira_label = i18n_active.ledger_kira_label()
        return f"{ctx_label}: {user_summary} {kira_label}: {kira_summary}"

    @staticmethod
    def _sanitize_history_context(context: str) -> str:
        """Strip obvious prompt-injection attempts from chat context."""
        lowered = context.lower()
        for marker in INJECTION_MARKERS:
            if marker in lowered:
                return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]", "", context)[:300]
        return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]", "", context)[:800]

    def _ejecutar_inferencia(self, contexto, source: str = "direct", *, history_text: Optional[str] = None):
        dialogo = self._generar_dialogo(contexto, source=source, commit_history=True, history_text=history_text)
        if dialogo:
            # FIX-B2: single emit chokepoint for every spoken live line — the
            # main reply AND the guardrail/repetition fallbacks all arrive here
            # as a non-empty `dialogo` and are spoken below, so last-reply stays
            # in sync with what Kira actually says. Agenda prefetch playback
            # emits from its own speaker (play_prefetched_agenda). Guarded, so a
            # raising callback never blocks speech. R8: Kira's own text only.
            emit_source = source if source.startswith("kira-agenda") else "kira"
            self._emit_dialogue(dialogo, emit_source)

            self._hablar(dialogo, source=source)
            # Measure-first telemetry seam: a chat turn actually played to completion —
            # advance the spoken clock so secs_since_last_spoken stays honest vs a
            # should_call that may later expire via TTL. Chat-only; no-op when unset.
            # Intentionally NOT in a finally: if _hablar raises (TTS failure) Kira did
            # NOT speak, so the spoken gap should keep growing — that growing gap is the
            # very signal that surfaces silent TTS failures to the operator.
            if source == "chat" and self.on_chat_turn_spoken is not None:
                try:
                    self.on_chat_turn_spoken()
                except Exception:
                    pass
        elif source.startswith("kira-agenda"):
            # Empty or guardrail-blocked agenda generation: _generar_dialogo
            # returned "", so _hablar never runs and no speaking_start event
            # fires. Signal the failure through the SAME validator hook the
            # success path uses (_accept_agenda_output at line ~1156) so the
            # controller leaves GENERATING and its recovery ladder engages,
            # instead of stalling the autonomous loop silently.
            self._accept_agenda_output("")

    @staticmethod
    def _sanitize_tts_text_for_playback(text: str) -> str:
        """Strip common Markdown emphasis markers without deleting speech text."""
        if text is None:
            return ""
        if not isinstance(text, str):
            text = str(text)
        if "*" not in text:
            return text

        def replace_emphasis(match: re.Match) -> str:
            inner = match.group(2)
            if not any(ch.isalpha() for ch in inner):
                return match.group(0)
            if any(ch in _TTS_MARKDOWN_OPERATOR_CHARS for ch in inner):
                return match.group(0)
            return inner

        return _TTS_MARKDOWN_EMPHASIS_RE.sub(replace_emphasis, text)

    def _sanitize_agenda_output(self, text: str) -> str:
        """Last line of defense for autonomous agenda speech."""
        clean = " ".join((text or "").strip().split())
        if not clean:
            return ""
        lowered = clean.lower()
        # Active locale's banned-closure phrases; None means the locale ships
        # none (guardrails domain, no cross-locale fallback). enable()'s
        # fail-closed gate now also requires agenda_banned_closures() is not
        # None, so in production this branch is unreachable while agenda
        # mode is enabled — kept as a defensive no-op since this method is
        # also exercised directly (e.g. in tests) outside that gate.
        banned = i18n_active.agenda_banned_closures()
        if banned and any(term in lowered for term in banned):
            self._log("Agenda: salida con cierre artificial detectada; usando fallback natural.", level="warning")
            return i18n_active.agenda_sanitizer_fallback()
        return clean

    def _hablar(self, texto_a_generar, source: str = "direct"):
        # Bug B fix: refuse a turn whose source was cancelled during its
        # generation phase (already popped from the priority queue, so
        # drop_pending_sources can't reach it). Checked BEFORE _speaking=True /
        # speaking_start so a straggler never starts playback after an emergency
        # stop. Mid-playback truncation is handled separately by the _speaking
        # guard in the consumer loop via interrupt_speaking().
        with self._lock:
            cancelled = self._cancelled_speech_prefixes
        if cancelled and any(source.startswith(p) for p in cancelled):
            self._log(f"Habla suprimida (cancelada): source={source}", level="warning")
            return

        with self._lock:
            self._speaking = True
            self._current_speech_source = source
        try:
            self.ui_callback("speaking_start")
        except Exception:
            with self._lock:
                self._speaking = False
                self._current_speech_source = None
            logger.exception("UI callback failed during speaking_start")
            raise

        ruta_absoluta_ref = os.path.abspath(self.voz_referencia) if self.voz_referencia else ""

        texto_limpio = self._sanitize_tts_text_for_playback(texto_a_generar)
        texto_limpio = texto_limpio.replace('"', '').replace('\n', ' ')

        fragmentos_brutos = re.split(r'(?<=[.!?])\s+', texto_limpio)
        oraciones = []
        
        MIN_PALABRAS_POR_CHUNK = 8
        MAX_PALABRAS_POR_CHUNK = 25
        
        for frag in fragmentos_brutos:
            frag = frag.strip()
            if not frag: continue
            
            if len(frag.split()) > MAX_PALABRAS_POR_CHUNK:
                sub_frags = re.split(r'(?<=[,;])\s+', frag)
                temp_chunk = ""
                for sub in sub_frags:
                    temp_chunk += sub + " "
                    if len(temp_chunk.split()) >= MIN_PALABRAS_POR_CHUNK:
                        oraciones.append(temp_chunk.strip())
                        temp_chunk = ""
                if temp_chunk.strip():
                    oraciones.append(temp_chunk.strip())
            else:
                oraciones.append(frag)

        oraciones = [o for o in oraciones if len(o) > 3]

        if not oraciones:
            self._log("⚠️ No se generaron oraciones válidas para sintetizar.", level="warning")
            with self._lock:
                self._speaking = False
                self._current_speech_source = None
            self.ui_callback("speaking_end")
            return

        self._log(f"Sintetizando {len(oraciones)} fragmento(s) con pipeline...")
        start_tts = time.time()

        cola_audios = queue.Queue(maxsize=3)
        error_count = 0

        def productor():
            nonlocal error_count
            # Snapshot tts_local_only ONCE at utterance start so a mid-utterance
            # toggle cannot send remaining chunks to Edge-TTS.  The toggle takes
            # effect from the NEXT utterance only.
            local_only = self.tts_local_only
            # Same snapshot contract for the Edge-TTS rate: a mid-utterance
            # speed change applies from the next utterance only.
            edge_rate = edge_rate_for_length_scale(self._tts_length_scale)

            # Determine effective motor for this request
            effective_motor = self.motor_tts
            fallback_reason = ""

            # Health-based auto-fallback: check before heavy TTS
            # Missing-reference auto-fallback: must run before the health gate
            # so its "effective=pesado" log only fires when heavy TTS will
            # actually be used.
            if effective_motor == "pesado" and not self.voz_referencia:
                effective_motor = "ligero"
                fallback_reason = "missing_reference"
                self._log(
                    "Auto-fallback to Edge-TTS: "
                    f"requested=pesado effective=ligero reason={fallback_reason}"
                )

            if effective_motor == "pesado":
                hm = getattr(self, "health_monitor", None)
                if hm is not None:
                    block_reason = None
                    if hasattr(hm, "heavy_tts_block_reason"):
                        block_reason = hm.heavy_tts_block_reason(
                            auto_fallback_enabled=True,
                            manual_motor=effective_motor,
                        )
                    elif not hm.should_use_heavy_tts(auto_fallback_enabled=True, manual_motor=effective_motor):
                        effective_motor = "ligero"
                        block_reason = "health_gate"
                    if block_reason:
                        fallback_reason = block_reason
                        effective_motor = "ligero"
                        self._log(
                            "Auto-fallback to Edge-TTS: "
                            f"requested=pesado effective=ligero reason={fallback_reason}"
                        )
                    else:
                        self._log("TTS efectivo: requested=pesado effective=pesado")

            for i, oracion in enumerate(oraciones):
                if not self._speaking:
                    break

                # Privacy fast-path: local_only is a snapshot taken at utterance
                # start (see top of productor()).  Using the snapshot ensures a
                # mid-utterance toggle cannot redirect remaining chunks to Edge-TTS;
                # the toggle applies from the next utterance only.
                # If Piper is unavailable, drop the chunk (degraded) rather than
                # silently re-enabling Edge-TTS (which would betray the privacy promise).
                if effective_motor == "ligero" and local_only:
                    self._maybe_notify_piper_locale_mismatch()
                    archivo_chunk_wav = os.path.join(
                        TEMP_DIR, f"tts_chunk_{i}_{uuid.uuid4().hex[:4]}.wav"
                    )
                    if self._piper.is_available():
                        if self._piper.synthesize(oracion, archivo_chunk_wav):
                            cola_audios.put((archivo_chunk_wav, i, oracion))
                        else:
                            logger.warning(
                                "TTS local-only: Piper synthesis failed for chunk %d "
                                "(chunk dropped; Edge-TTS not attempted)", i
                            )
                            cola_audios.put(None)
                            error_count += 1
                    else:
                        logger.warning(
                            "TTS local-only: Piper unavailable for chunk %d "
                            "(chunk dropped; Edge-TTS not attempted)", i
                        )
                        cola_audios.put(None)
                        error_count += 1
                    continue

                # Fast-path: Edge-TTS is known offline for this session (or the
                # package is not installed) — go straight to Piper without
                # attempting a network call.
                if effective_motor == "ligero" and (self._edge_tts_offline or edge_tts is None):
                    self._maybe_notify_piper_locale_mismatch()
                    archivo_chunk_wav = os.path.join(
                        TEMP_DIR, f"tts_chunk_{i}_{uuid.uuid4().hex[:4]}.wav"
                    )
                    if self._piper.is_available():
                        if self._piper.synthesize(oracion, archivo_chunk_wav):
                            cola_audios.put((archivo_chunk_wav, i, oracion))
                        else:
                            cola_audios.put(None)
                            error_count += 1
                    else:
                        # Piper gone / never loaded — drop chunk silently
                        cola_audios.put(None)
                        error_count += 1
                    continue

                ext = ".mp3" if effective_motor == "ligero" else ".wav"
                archivo_chunk = os.path.join(TEMP_DIR, f"tts_chunk_{i}_{uuid.uuid4().hex[:4]}{ext}")
                try:
                    if effective_motor == "ligero":
                        async def generar_edge():
                            communicate = edge_tts.Communicate(
                                oracion, i18n_active.edge_voice(), rate=edge_rate
                            )
                            await communicate.save(archivo_chunk)

                        asyncio.run(asyncio.wait_for(generar_edge(), timeout=TTS_LIGHT_TIMEOUT))
                        cola_audios.put((archivo_chunk, i, oracion))
                    else:
                        # Heavy TTS path — measure generation time for RTF
                        gen_start = time.time()
                        respuesta = requests.post(
                            TTS_SERVER_URL,
                            json={
                                "texto": oracion,
                                "referencia": ruta_absoluta_ref,
                                "motor": effective_motor
                            },
                            timeout=TTS_HEAVY_TIMEOUT
                        )
                        gen_elapsed = time.time() - gen_start
                        if respuesta.status_code == 200:
                            with open(archivo_chunk, 'wb') as f:
                                f.write(respuesta.content)
                            cola_audios.put((archivo_chunk, i, oracion))

                            # Record RTF measurement
                            hm = getattr(self, "health_monitor", None)
                            if hm is not None:
                                try:
                                    # Estimate audio duration: ~15 chars/sec for Spanish
                                    estimated_duration = len(oracion) / 15.0
                                    hm.record_ttf_measurement(gen_elapsed, estimated_duration)
                                except Exception:
                                    pass  # Never break TTS for measurement failure
                        else:
                            error_detail = "desconocido"
                            try:
                                error_detail = respuesta.json().get('error', respuesta.text[:100])
                            except Exception:
                                error_detail = respuesta.text[:100]
                            logger.warning(f"TTS chunk {i} error HTTP {respuesta.status_code}: {error_detail}")
                            cola_audios.put(None)
                            error_count += 1

                except requests.exceptions.ConnectionError:
                    self._log("ERROR: Servidor Qwen3-TTS no disponible.", level="error")
                    cola_audios.put(None)
                    error_count += 1
                    continue

                except requests.exceptions.Timeout:
                    logger.warning(f"TTS chunk {i} timeout")
                    cola_audios.put(None)
                    error_count += 1

                except Exception as e:
                    # Connection-error detection: only network-offline errors trigger
                    # Piper fallback. asyncio.TimeoutError and other errors do NOT.
                    if effective_motor == "ligero" and _is_connection_error(e):
                        self._edge_tts_offline = True
                        self._maybe_notify_piper_locale_mismatch()
                        if self._piper.is_available():
                            self._log(
                                "Edge-TTS sin conexion; usando TTS local (Piper) "
                                "por el resto de la sesion."
                            )
                            archivo_chunk_wav = os.path.join(
                                TEMP_DIR, f"tts_chunk_{i}_{uuid.uuid4().hex[:4]}.wav"
                            )
                            if self._piper.synthesize(oracion, archivo_chunk_wav):
                                cola_audios.put((archivo_chunk_wav, i, oracion))
                            else:
                                cola_audios.put(None)
                                error_count += 1
                        else:
                            self._log(
                                "TTS local no disponible: instala piper-tts y "
                                "configura TTS_LOCAL_MODEL_PATH",
                                level="warning",
                            )
                            cola_audios.put(None)
                            error_count += 1
                        continue

                    if effective_motor == "ligero":
                        self._log("ERROR: Edge-TTS requiere internet. Si estas offline usa Pesado (Qwen3-TTS).", level="error")
                        logger.warning(f"TTS ligero fallo; timeout configurado {TTS_LIGHT_TIMEOUT}s: {e}")
                        cola_audios.put(None)
                        error_count += 1
                        continue
                    logger.exception(f"TTS chunk {i} error inesperado")
                    cola_audios.put(None)
                    error_count += 1

            cola_audios.put("FIN")

        hilo_productor = threading.Thread(target=productor, daemon=True)
        hilo_productor.start()

        # Recovery: consume the suspect flag once, before the first chunk of
        # this turn plays. Gated on the flag (never unconditional per turn —
        # see the _audio_reinit_needed comment in __init__) because the CTk
        # app's AudioBedEngine shares this same mixer for background music;
        # PTT (the only setter of this flag besides a playback exception)
        # does not exist in the CTk app, so CTk never pays this reinit.
        with self._lock:
            reinit_needed = self._audio_reinit_needed
            self._audio_reinit_needed = False
        if reinit_needed:
            try:
                self.pygame.mixer.quit()
                self.pygame.mixer.init()
                self._log("Audio device re-inicializado (recovery)")
            except Exception as e:
                logger.warning(f"No se pudo re-inicializar pygame.mixer: {e}")

        chunks_played = 0
        try:
            while True:
                # Bug 4 fix: check _speaking before dequeuing the next chunk.
                # emergency_stop() sets _speaking=False externally; without this
                # guard the consumer drains the entire pre-filled queue even after
                # teardown is requested.
                with self._lock:
                    if not self._speaking:
                        break

                item = cola_audios.get(timeout=TTS_AUDIO_QUEUE_TIMEOUT)

                if item == "FIN":
                    break
                if item is None:
                    continue

                # Second _speaking check after dequeue — emergency_stop may have
                # fired while we were blocked on cola_audios.get().
                with self._lock:
                    if not self._speaking:
                        try:
                            if os.path.exists(item[0]):
                                os.remove(item[0])
                        except (OSError, TypeError):
                            pass
                        break

                archivo_chunk, idx, oracion_texto = item

                try:
                    if chunks_played == 0:
                        elapsed_first = time.time() - start_tts
                        self._log(f"🔊 Primer fragmento listo en {elapsed_first:.2f}s. Reproduciendo...")

                    self.pygame.mixer.music.load(archivo_chunk)
                    self.pygame.mixer.music.play()

                    while self.pygame.mixer.music.get_busy():
                        # Bug 4 fix: honour external _speaking=False inside the
                        # busy-wait so a playing chunk is stopped promptly on
                        # emergency teardown instead of draining to completion.
                        with self._lock:
                            if not self._speaking:
                                break
                        time.sleep(0.05)

                    # Bug 4 fix: if _speaking was cleared externally, stop the
                    # mixer explicitly before unload() — pygame keeps playing
                    # until stop() or end-of-track; unload() alone does not stop.
                    with self._lock:
                        interrupted = not self._speaking
                    if interrupted:
                        self.pygame.mixer.music.stop()

                    self.pygame.mixer.music.unload()
                    chunks_played += 1

                    if interrupted:
                        break

                except Exception as e:
                    # Bug fix (2026-07-15 PTT voice-death): a playback
                    # exception must count as a failed fragment, not just a
                    # log line — otherwise the mixer can be zombied (e.g. a
                    # migrated WASAPI stream) while every turn still reports
                    # "completado" and speaking_end fires as if nothing
                    # happened. error_count is _hablar's own local, not a
                    # nested closure, so no `nonlocal` is needed here.
                    error_count += 1
                    logger.warning(f"Error reproduciendo chunk {idx}: {e}")
                    self.mark_audio_suspect()
                finally:
                    try:
                        if os.path.exists(archivo_chunk):
                            os.remove(archivo_chunk)
                    except OSError:
                        pass

        except queue.Empty:
            self._log("⚠️ Timeout esperando chunks de audio.", level="warning")
        except Exception as e:
            self._log(f"ERROR en reproducción: {e}", level="error")
            logger.exception("Error en consumidor de audio")
        finally:
            total_elapsed = time.time() - start_tts
            # Join the producer FIRST so it can no longer enqueue a chunk, THEN
            # drain.  Draining before the join races: when the producer is still
            # synthesizing at interrupt time, it writes+enqueues that chunk AFTER
            # get_nowait() already emptied the queue, leaking the temp file
            # permanently (reproducible under loaded-CI interleaving).  After the
            # consumer's dequeue freed a queue slot, the producer always makes
            # progress and breaks on its next _speaking check, so this join
            # returns well within its timeout.
            hilo_productor.join(timeout=2.0)
            # Fix 1: drain any remaining items left in cola_audios by the producer
            # after an early break (pre-dequeue guard, post-dequeue guard, or
            # interrupted-chunk break).  Without this, 1-3 temp .wav files leak
            # per emergency stop because the producer thread may have already
            # enqueued chunks that the consumer never got to consume.
            # Sentinels ("FIN" / None) are skipped — only real chunk tuples have
            # a temp file path at index 0.
            while True:
                try:
                    _leftover = cola_audios.get_nowait()
                    if isinstance(_leftover, tuple):
                        try:
                            if os.path.exists(_leftover[0]):
                                os.remove(_leftover[0])
                        except (OSError, TypeError):
                            pass
                except queue.Empty:
                    break
        self._log(f"✅ Pipeline TTS completado: {chunks_played}/{len(oraciones)} fragmentos en {total_elapsed:.2f}s")
        if error_count > 0:
            self._log(f"⚠️ {error_count} fragmento(s) fallaron.", level="warning")
        with self._lock:
            self._speaking = False
            self._current_speech_source = None
        self.ui_callback("speaking_end")

    def _emit_dialogue(self, text: str, source: str) -> None:
        """P3 producer sink: forwards Kira's own generated reply text.

        Opt-in (dialogue_callback defaults to None). A raising callback must
        never break the turn, so it's guarded the same way ui_callback sites
        are — logged, swallowed, never re-raised.
        """
        if self.dialogue_callback is None:
            return
        try:
            self.dialogue_callback(text, source)
        except Exception:
            logger.exception("dialogue_callback failed")

    def _log(self, msg, level="info"):
        prefix = "[IA]"
        self.log_queue.put(f"{prefix} {msg}")
        getattr(logger, level)(f"Motor: {msg}")
