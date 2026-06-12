import os
import re
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
from collections import deque
from typing import Optional

from opencohost.config.settings import (
    DEFAULT_MODEL, SYSTEM_PROMPT, HISTORY_MAX_TURNS, LLM_TEMPERATURE,
    LLM_TOP_P, LLM_MAX_TOKENS, TEMP_DIR, TTS_SERVER_URL,
    TTS_HEAVY_TIMEOUT, TTS_LIGHT_TIMEOUT,
    OLLAMA_CHAT_TIMEOUT,
    TTS_LOCAL_MODEL_PATH,
    resolve_llm_tiers,
    resolve_startup_model, save_last_model,
    load_tts_local_only, save_tts_local_only,
)
from opencohost.core.tts_piper import PiperEngine
from opencohost.core.llm_tiers import LLMTierConfig, LLMTierState, LLM_TIER_LABELS
from opencohost.config.logger import get_logger
from opencohost.config.validation import output_guard

logger = get_logger()

# The producer can legitimately spend the full heavy TTS HTTP timeout before
# enqueuing a Qwen chunk. Keep the consumer bounded, but do not give up sooner
# than the producer's configured request timeout.
TTS_AUDIO_QUEUE_TIMEOUT = max(TTS_HEAVY_TIMEOUT, TTS_LIGHT_TIMEOUT) + 15
_TTS_MARKDOWN_EMPHASIS_RE = re.compile(r"(?<![\w])(\*{1,3})(?!\s)([^*\n]+?)(?<!\s)\1(?![\w])")
_TTS_MARKDOWN_OPERATOR_CHARS = set("=+*/<>\\|")

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


# Spoken when the output guard blocks a response, so Kira never goes silent
# mid-conversation. No LLM call involved. Lines must stay neutral and must
# not match any output_guard pattern themselves. Agenda sources are excluded
# (the agenda state machine has its own rejection/recovery path).
GUARDRAIL_FALLBACK_LINES = (
    "Uy, se me trabó la lengua. ¿Qué me decías?",
    "Perdón, me distraje un segundo. ¿Puedes repetirlo?",
    "Espera, se me cruzaron los cables. Dame un momento.",
    "Mmm, mejor lo dejo ahí. ¿Seguimos con otra cosa?",
)

class MotorVocalIA(threading.Thread):
    """
    Hilo de IA: gestiona Ollama (LLM), memoria conversacional,
    y comunicación con el servidor TTS vía HTTP.
    """
    def __init__(self, log_queue, ui_callback):
        super().__init__(daemon=True)
        self.log_queue = log_queue
        self.ui_callback = ui_callback
        self.command_queue = queue.Queue()

        self.voz_referencia = None
        self.is_ready = False
        self._processing = False
        self._speaking = False
        self._current_speech_source: Optional[str] = None
        self._current_processing_source: Optional[str] = None
        self._downloading = False
        _startup_model, self._model_source = resolve_startup_model()
        self.current_model = _startup_model
        self._desired_model: str = _startup_model
        self._loaded_model: Optional[str] = None  # set after _prepare_model succeeds
        self._owns_ollama_model: bool = False
        self._current_profile_name: str = "default"
        _tier_config = LLMTierConfig(**resolve_llm_tiers())
        self.llm_tiers = LLMTierState(
            config=_tier_config,
            active_tier=self._infer_active_tier(_startup_model, _tier_config),
        )

        # Pending model switch (non-blocking retry)
        self._pending_model_switch: Optional[str] = None
        self._pending_switch_retries: int = 0
        self._pending_switch_next_at: float = 0.0
        self._pending_switch_not_ready_logged: bool = False
        # Last switch failure info (read by UI handler, cleared after read)
        self._last_switch_failure: Optional[dict] = None
        self._warmed_model: Optional[str] = None
        self._ollama_chat_client = None
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
        self._piper = PiperEngine(TTS_LOCAL_MODEL_PATH)

        # Optional health monitor for auto-fallback (set externally, None = backward compat)
        self.health_monitor = None
        
        self.system_prompt = SYSTEM_PROMPT
        self.use_system_role = False

        self.historial = deque(maxlen=HISTORY_MAX_TURNS * 2)

        self._lock = threading.Lock()

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
            self._log("Historial de conversación limpiado.")

        elif tipo == "switch_model":
            new_model = payload
            self._desired_model = new_model

            if not self.is_ready:
                self._log(f"Switch a {new_model} rechazado: Ollama no esta listo.", level="warning")
                self.ui_callback("model_switch_failed")
                return

            if self._processing or self._speaking:
                self._log(f"Switch a {new_model} pendiente: motor ocupado.", level="warning")
                self._pending_switch_not_ready_logged = False
                self._pending_model_switch = new_model
                self._pending_switch_retries = 3
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

        elif tipo == "set_profile":
            self.system_prompt = payload.get("prompt", SYSTEM_PROMPT)
            self.use_system_role = payload.get("use_system", False)
            profile_name = payload.get("_profile_name", "desconocido")
            self._current_profile_name = profile_name
            self.historial.clear()
            self._log(f"Perfil actualizado: {profile_name} (System Role: {self.use_system_role}). Memoria limpiada.")

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

    def enqueue(self, payload: str, priority: int = 1, source: str = "chat") -> None:
        """Add a message to the priority queue.

        Args:
            payload: The text to process.
            priority: 0 = PTT/streamer (highest), 1 = chat (normal), 2 = agenda (lowest).
            source: Origin identifier for logging.
        """
        with self._pq_lock:
            self._priority_queue.append((priority, time.time(), payload, source))
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
                    parts.append(f"El streamer dijo (acumulado): {'; '.join(messages)}")
                elif source == "chat":
                    parts.append(f"Mientras procesabas, llegaron estos mensajes del chat: {' | '.join(messages)}")
                else:
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

        with self._pq_lock:
            # Expire stale non-PTT items before selecting next work
            now = time.time()
            kept = []
            for item in self._priority_queue:
                prio, ts, payload, source = item
                if prio > 0 and (now - ts) > self._pq_ttl_seconds:
                    self._log(f"Item expirado y omitido (TTL {self._pq_ttl_seconds:.0f}s): {source}")
                else:
                    kept.append(item)
            self._priority_queue = kept

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

            priority, ts, payload, source = self._priority_queue.pop(0)

        source_label = "PTT" if source == "ptt" else source
        self._log(f"Cola prioritaria: procesando [{source_label}] (prioridad {priority})...")
        with self._lock:
            self._processing = True
            self._current_processing_source = source
        self.ui_callback("processing")
        try:
            self._ejecutar_inferencia(payload, source=source)
        finally:
            self._complete_processing_cycle()

    def _complete_processing_cycle(self, *, process_queue: bool = True) -> None:
        with self._lock:
            self._processing = False
            self._current_processing_source = None
        self.ui_callback("idle")
        self._check_pending_model_switch()
        if process_queue:
            self._process_priority_queue()

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
                self._pending_switch_retries = 0
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

        if not self.is_ready:
            self._pending_switch_next_at = time.monotonic() + 2.0
            if self._check_ollama_service(notify_unavailable=False):
                return
            if not self._pending_switch_not_ready_logged:
                self._log(f"Switch a {model} sigue pendiente: Ollama no está listo.", level="debug")
                self._pending_switch_not_ready_logged = True
            return

        self._apply_model_switch(model)

    def _apply_model_switch(self, new_model: str, *, persist_source: str = "user_switch") -> bool:
        """Execute model switch and persist on success."""
        previous_model = self.current_model
        self._pending_model_switch = None
        self._pending_switch_retries = 0
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
                    keep_alive=-1,
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
        """Best-effort unload for the model warmed by this VoiceAI session."""
        model = self._loaded_model or self._warmed_model
        if not model or not self._owns_ollama_model or not hasattr(self, "ollama"):
            logger.info("Ollama model release skipped; no VoiceAI-owned model recorded")
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
        """
        if source.startswith("kira-agenda"):
            return ""
        idx = getattr(self, "_guardrail_fallback_idx", 0)
        self._guardrail_fallback_idx = idx + 1
        return GUARDRAIL_FALLBACK_LINES[idx % len(GUARDRAIL_FALLBACK_LINES)]

    def _generar_dialogo(self, contexto, source: str = "direct", *, commit_history: bool = True, log_prefix: str = "LLM") -> str:
        request_model = self.current_model
        self._log(f"Analizando contexto con {request_model}...")
        try:
            messages = []
            
            if self.use_system_role:
                messages.append({'role': 'system', 'content': self.system_prompt})

            for msg in self.historial:
                messages.append(msg)

            if self.use_system_role:
                messages.append({'role': 'user', 'content': contexto})
            else:
                prompt_completo = f"{self.system_prompt}\n\n[Mensaje del usuario]: {contexto}"
                messages.append({'role': 'user', 'content': prompt_completo})

            opciones_llm = {
                'temperature': LLM_TEMPERATURE,
                'top_p': LLM_TOP_P,
                'num_predict': LLM_MAX_TOKENS,
                'num_ctx': 4096,
            }

            if "gemma" in request_model.lower():
                opciones_llm.pop('num_ctx', None)
                opciones_llm['temperature'] = 0.7

            if self._uses_reasoning_token_budget(request_model):
                opciones_llm.pop('num_predict', None)
                self._log("Modelo de razonamiento detectado. Límite de tokens removido.", level="debug")

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
                        keep_alive=-1,
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

                if raw_content.strip():
                    break
                
                self._log(f"⚠️ Intento {intento+1}: {request_model} devolvió respuesta vacía. Reintentando...", level="warning")
                time.sleep(0.5)

            dialogo = raw_content.strip().strip('\x00\ufeff')
            elapsed = time.time() - start_llm

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

            if source.startswith("kira-agenda") and commit_history and not self._accept_agenda_output(dialogo):
                self._log(f"Agenda: salida rechazada ({self._format_agenda_rejection()}).", level="warning")
                return ""

            if commit_history:
                self.log_queue.put(f"\n🧠 [Kira]: {dialogo} ({elapsed:.2f}s)\n")
            logger.info(f"{log_prefix} response ({elapsed:.2f}s): {dialogo[:200]}")

            if commit_history:
                self._commit_history(contexto, dialogo, source=source)

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

    def _ollama_chat(self, **kwargs):
        client = self._ollama_chat_client or self.ollama
        return client.chat(**kwargs)

    def _ollama_chat_with_watchdog(self, *, timeout: float, **kwargs):
        result = {}
        done = threading.Event()

        def worker() -> None:
            try:
                result["response"] = self._ollama_chat(**kwargs)
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

    def _commit_history(self, contexto: str, dialogo: str, *, source: str = "direct") -> None:
        if source.startswith("kira-agenda"):
            safe_context = "[agenda segura: prompt interno omitido]"
        else:
            safe_context = self._sanitize_history_context(contexto)
        self.historial.append({'role': 'user', 'content': safe_context})
        self.historial.append({'role': 'assistant', 'content': dialogo})
        max_mensajes = HISTORY_MAX_TURNS * 2
        if len(self.historial) > max_mensajes:
            self.historial = self.historial[-max_mensajes:]

    @staticmethod
    def _sanitize_history_context(context: str) -> str:
        """Strip obvious prompt-injection attempts from chat context."""
        import re
        lowered = context.lower()
        injection_markers = (
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
        )
        for marker in injection_markers:
            if marker in lowered:
                return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]", "", context)[:300]
        return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]", "", context)[:800]

    def _ejecutar_inferencia(self, contexto, source: str = "direct"):
        dialogo = self._generar_dialogo(contexto, source=source, commit_history=True)
        if dialogo:

            self._hablar(dialogo, source=source)

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
        banned = (
            "hasta luego",
            "próximo episodio",
            "proximo episodio",
            "eso es todo",
            "siguiente tema",
            "finaliza",
            "cerrar el tema",
            "abordando este tema",
            "voy a hablar",
            "vamos a seguir hablando",
        )
        if any(term in lowered for term in banned):
            self._log("Agenda: salida con cierre artificial detectada; usando fallback natural.", level="warning")
            return "Me quedo con una idea: esto da para mirarlo con más matices, porque no es tan simple como parece a primera vista."
        return clean

    def _hablar(self, texto_a_generar, source: str = "direct"):
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
                            communicate = edge_tts.Communicate(oracion, "es-MX-DaliaNeural")
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

        chunks_played = 0
        try:
            while True:
                item = cola_audios.get(timeout=TTS_AUDIO_QUEUE_TIMEOUT)

                if item == "FIN":
                    break
                if item is None:
                    continue

                archivo_chunk, idx, oracion_texto = item

                try:
                    if chunks_played == 0:
                        elapsed_first = time.time() - start_tts
                        self._log(f"🔊 Primer fragmento listo en {elapsed_first:.2f}s. Reproduciendo...")

                    self.pygame.mixer.music.load(archivo_chunk)
                    self.pygame.mixer.music.play()

                    while self.pygame.mixer.music.get_busy():
                        time.sleep(0.05)

                    self.pygame.mixer.music.unload()
                    chunks_played += 1

                except Exception as e:
                    logger.warning(f"Error reproduciendo chunk {idx}: {e}")
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
        self._log(f"✅ Pipeline TTS completado: {chunks_played}/{len(oraciones)} fragmentos en {total_elapsed:.2f}s")
        if error_count > 0:
            self._log(f"⚠️ {error_count} fragmento(s) fallaron.", level="warning")
        with self._lock:
            self._speaking = False
            self._current_speech_source = None
        self.ui_callback("speaking_end")

        hilo_productor.join(timeout=2.0)

    def _log(self, msg, level="info"):
        prefix = "[IA]"
        self.log_queue.put(f"{prefix} {msg}")
        getattr(logger, level)(f"Motor: {msg}")
