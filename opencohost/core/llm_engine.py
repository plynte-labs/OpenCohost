"""
opencohost/core/llm_engine.py
"""
import contextlib
import os
import sys
import re
import hashlib
import json
import socket
import sqlite3
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
try:
    import winsound  # Windows-only stdlib; drives the PTT listening cue
except ImportError:
    winsound = None  # non-Windows: the PTT cue is a silent no-op
from collections import deque, Counter
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional

from opencohost.config import settings
from opencohost.config.settings import (
    DEFAULT_MODEL, SYSTEM_PROMPT, HISTORY_MAX_TURNS, LLM_TEMPERATURE,
    LLM_TOP_P, LLM_MAX_TOKENS, LLM_KEEP_ALIVE, TEMP_DIR, TTS_SERVER_URL,
    TTS_HEAVY_TIMEOUT, TTS_LIGHT_TIMEOUT,
    EDGE_BREAKER_THRESHOLD, EDGE_BREAKER_COOLDOWN_S, EDGE_BREAKER_MAX_COOLDOWN_S,
    OLLAMA_CHAT_TIMEOUT, OLLAMA_REQUEST_TIMEOUT,
    SCOUT_ENABLED, LLM_SCOUT_TIMEOUT, LLM_SCOUT_NUM_PREDICT,
    LLM_SCOUT_TEMPERATURE, LLM_SCOUT_MIN_DIGEST_LINES,
    LLM_SCOUT_HISTORY_MSGS, SCOUT_QUEUE_FLOOR,
    TTS_LOCAL_MODEL_PATH,
    CHAT_REPEAT_PENALTY, CHAT_PRESENCE_PENALTY, CHAT_FREQUENCY_PENALTY,
    CTX_FALLBACK_DEFAULT, CHAR_BUDGET_SAFETY_FACTOR,
    CTX_PRESSURE_HIGH_THRESHOLD, CTX_OVERFLOW_SIGNAL_RATIO,
    resolve_llm_tiers,
    resolve_startup_model, save_last_model,  # Retained on module for test mock compatibility
    load_tts_local_only, save_tts_local_only,
    load_tts_speed, save_tts_speed,
    load_discard_speech_residue, save_discard_speech_residue,
    load_interrupt_on_direct, save_interrupt_on_direct,
    load_motor_tts, save_motor_tts,
    PIPER_VOICES, DEFAULT_PIPER_VOICE, piper_voice_path, load_piper_voice, save_piper_voice,
    default_piper_voice_for_locale,
    load_piper_voice_for_locale, save_piper_voice_for_locale, PIPER_SPEAKER_ID_MAX,
    load_edge_voice_for_locale, save_edge_voice_for_locale, is_valid_edge_voice,
    MEMORIAS_ENABLED, MEMORIAS_DB,
    MEMORIAS_PROFILE_CAP, MEMORIAS_PROMOTION_REARM_S,
    MEMORIAS_PROMOTION_QUIET_S,
    MEMORIAS_SUMMARY_MIN_TITLES,
    PERSONALIZATION_ENABLED,
    PTT_CUE_ENABLED, PTT_CUE_VOLUME,
    RETRY_MIN_REMAINING_SECONDS,
    RETURN_MAX_DETOUR_TURNS,
    CONNECTOR_UPGRADE_MIN_REMAINING_SECONDS, CONNECTOR_UPGRADE_TIMEOUT_SECONDS,
    CLOUD_CHAT_TIMEOUT, CLOUD_CTX_BUDGET, CLOUD_MAX_TOKENS, LLM_KEYS_FILE,
    CLAUSE_SANITIZER_SOURCES,
    CLOUD_RATE_LIMIT_RETRY_DEFAULT_SECONDS, CLOUD_RATE_LIMIT_RETRY_MAX_SECONDS,
    CTX_TELEMETRY_RING_MAXLEN,
    CLOUD_AUTO_RETURN_RATE_LIMIT_FLOOR_SECONDS, CLOUD_AUTO_RETURN_RATE_LIMIT_CAP_SECONDS,
    CLOUD_AUTO_RETURN_TRANSIENT_BASE_SECONDS, CLOUD_AUTO_RETURN_TRANSIENT_CAP_SECONDS,
    CLOUD_AUTO_RETURN_AMBIGUOUS_429_ENABLED, CLOUD_AUTO_RETURN_AMBIGUOUS_429_BASE_SECONDS,
    CLOUD_AUTO_RETURN_AMBIGUOUS_429_CAP_SECONDS, CLOUD_AUTO_RETURN_AMBIGUOUS_429_MAX_ATTEMPTS,
    CLOUD_PROBER_JOIN_TIMEOUT_SECONDS,
    DIRECT_ANSWER_MAX_WAIT_SECONDS,
    OWNER_BUNDLE_SOURCE, OWNER_BUNDLE_MAX_ITEMS, OWNER_BUNDLE_MAX_CHARS,
    LLM_STREAMING_ENABLED, CLOUD_STREAMING_ENABLED,
)
from opencohost.core.context import context_budget
from opencohost.core.context import echo_guard as _echo_guard
from opencohost.core.context import prompt_echo as _prompt_echo
from opencohost.core.context import search_claim as _search_claim
# Module import, never a from-import of the values: STREAM_OVER_AGENDA /
# STREAM_TTL_SECONDS are runtime-mutable (PUT /api/stream/chat-live/limits
# rebinds them), and the functions read the module globals live.
from opencohost.core import turn_priority
from opencohost.core.scheduling.turn_stamp import TurnStamp as TurnStamp
from opencohost.core.turn_scheduler import (
    OWNER_QUESTION_SOURCES as _OWNER_QUESTION_SOURCES,
    TurnScheduler,
    compose_owner_bundle as compose_owner_bundle,
)
from opencohost.core.memory.promotion_parser import (
    _PROMOTION_JUDGE_PROMPT as _PROMOTION_JUDGE_PROMPT,
    _PROMOTION_FENCE_RE as _PROMOTION_FENCE_RE,
    _PROMOTION_EXISTING_BLOCK as _PROMOTION_EXISTING_BLOCK,
    PromotionParseDiagnostics as PromotionParseDiagnostics,
    _PromotionState as _PromotionState,
    _parse_promotion_decisions as _parse_promotion_decisions,
    _parse_promotion_diagnostics as _parse_promotion_diagnostics,
)
from opencohost.core.context.injection_markers import (
    SCOUT_TITLE_MAX_WORDS as SCOUT_TITLE_MAX_WORDS,
    INJECTION_MARKERS as INJECTION_MARKERS,
    _strip_injection_markers as _strip_injection_markers,
)
from opencohost.core.speech.speech_rate import (
    edge_rate_for_length_scale as edge_rate_for_length_scale,
    _is_connection_error as _is_connection_error,
    classify_edge_failure as classify_edge_failure,
    EdgeBreaker as EdgeBreaker,
    calculate_speech_pause as calculate_speech_pause,
)
from opencohost.core.context.repetition_guard import (
    DEFAULT_CONFIG as REPETITION_CONFIG,
    RepetitionResult,
    detect_repetition,
    sanitize_clause_repetition,
)
from opencohost.core.context.generation_evaluator import (
    GenerationEvaluator,
    EvaluationResult,
    output_guard_with_tts_check as _output_guard_with_tts_check_pure,
    guard_rule_id as _guard_rule_id_pure,
)
from opencohost.core.context.prompt_assembler import (
    DIGEST_CAPTURE_SOURCES,
    PERSONALIZATION_INJECT_SOURCES,
    MEMORIA_INJECT_SOURCES,
    DIGEST_INJECT_SOURCES,
    EDITORIAL_INJECT_SOURCES,
    HISTORY_ASSISTANT_ONLY_SOURCES,
    OWNER_POLICY_SOURCES,
    SCREEN_INJECT_SOURCES,
    GenerationSetup,
    _GenerationSetup,
    PromptContextAssembler,
)
from opencohost.core.context import prompt_profile as _prompt_profile
from opencohost.core.context.reply_length import resolve_reply_level
from opencohost.core.engine import prompt_trace, turn_state, turn_timing
from opencohost.core.usage import ledger as usage_ledger
from opencohost.core.engine.turn_state import TurnEvent
from opencohost.core.engine.llm_inference_service import (
    LLMInferenceService,
    configured_cloud_model,
    get_inference_service,
)
from opencohost.core.web import sources as _web_sources
from opencohost.core.engine.generation_orchestrator import (
    GenerationAttemptOutcome,
    GenerationOrchestrator,
    GenerationResult,
    StreamAttemptState,
    _GenerationAttemptOutcome,
    _StreamAttemptState,
)
from opencohost.core.providers.cloud import cloud_llm_client
from opencohost.core.providers.think_tags import strip_think_blocks
from opencohost.core.profiles import personalization
from opencohost.core.speech.tts_sanitizer import (
    _first_sentence,
    _sanitize_tts_text_for_display,
    _sanitize_tts_text_for_playback,
    _spoken_text_for_fragment,
)
from opencohost.core.speech.router import (
    SPEECH_BOUNDARY_COMMAND,
    SpeechJob,
    SpeechRouter,
    priority_for_source,
)
from opencohost.core.speech.sentence_splitter import SentenceSplitter
from opencohost.core.context.ctx_telemetry import CtxTelemetryRing
from opencohost.config.llm_provider import load_provider_config
from opencohost.stream_admin.oauth_store import OAuthStore
from opencohost.i18n import active as i18n_active
from opencohost.i18n import coherence as i18n_coherence
from opencohost.core.speech import edge_voices, piper_catalog
from opencohost.core.speech.backends.tts_piper import PiperEngine
from opencohost.core.providers.llm_tiers import LLMTierConfig, LLMTierState, LLM_TIER_LABELS
from opencohost.core.memory.memory_digest import MemoryDigest
from opencohost.core.memory.memoria_store import (
    MemoriaStore, derive_stable_key, build_title, build_signature,
    build_injection_lines, build_recency_lines, is_clarification_turn, is_meta_recall_query,
    pinned_injection_counter, significant_token_count, strip_history_wrapper,
)
from opencohost.core.context.repetition_guard import (
    detect_repetition,
    sanitize_clause_repetition,
    DEFAULT_CONFIG as REPETITION_CONFIG,
)
from opencohost.config.logger import get_logger, _debug_enabled
from opencohost.config.validation import log_non_negotiable_block, output_guard
from opencohost.core.safety.crisis import detect_crisis
from opencohost.core.safety.turn import crisis_turn, is_crisis_turn
from opencohost.config import reply_length as reply_length_setting
from opencohost.config import warmth as warmth_state
from opencohost.config.owner_gender import DEFAULT_OWNER_GENDER, normalize_owner_gender
from opencohost.config.warmth import DEFAULT_WARMTH, normalize_warmth
from opencohost.core.engine.llm_engine_scout import ScoutPromotionMixin
from opencohost.core.engine.llm_engine_memorias import MemoriaCaptureMixin
from opencohost.core.engine.llm_engine_reflection import OwnerReflectionMixin
from opencohost.core.engine.llm_engine_agenda import AgendaStashMixin
from opencohost.core.engine.llm_engine_models import ModelManagementMixin
from opencohost.core.engine.llm_engine_cloud import CloudFallbackMixin
from opencohost.core.engine.llm_engine_pregen import PregenCacheMixin
from opencohost.core.engine.llm_engine_speech import SpeechPipelineMixin
from opencohost.core.engine.llm_engine_vision import NO_LOOK, VisionTurnMixin
from opencohost.core.vision import direct_cloud
from opencohost.core.engine.llm_engine_decisions import DecisionTurnMixin, decision_kwargs
from opencohost.core.engine.llm_engine_web import WebTurnMixin
from opencohost.core.decision.consumers import apply_reply_length, memory_recall_skipped

logger = get_logger()

# The producer can legitimately spend the full heavy TTS HTTP timeout before
# enqueuing a Qwen chunk. Keep the consumer bounded, but do not give up sooner
# than the producer's configured request timeout.
TTS_AUDIO_QUEUE_TIMEOUT = max(TTS_HEAVY_TIMEOUT, TTS_LIGHT_TIMEOUT) + 15

_DIGEST_CAPTURE_SOURCES = frozenset({"direct", "ptt", OWNER_BUNDLE_SOURCE})

_PERSONALIZATION_INJECT_SOURCES = frozenset({"direct", "ptt", OWNER_BUNDLE_SOURCE})

_MEMORIA_INJECT_SOURCES = frozenset({"direct", "ptt", OWNER_BUNDLE_SOURCE})

_DIGEST_INJECT_SOURCES = frozenset({"direct", "ptt", OWNER_BUNDLE_SOURCE})
_EDITORIAL_INJECT_SOURCES = frozenset({"direct", "ptt", OWNER_BUNDLE_SOURCE})

_HISTORY_ASSISTANT_ONLY_SOURCES = frozenset({"chat"})

_SESSION_MEMORIA_TITLES_CAP = 40

_MEMORIA_SUMMARY_WRITE_BUDGET_SECONDS = 1.1

# guardrail_tuning_20260724 (owner decision "afinar + reintento") and
# S1: sources whose text is the owner's own words (the only turns that can be
# marked as a crisis turn).
_CRISIS_SOURCES = frozenset({"direct", "ptt", settings.OWNER_BUNDLE_SOURCE})

# qwen-runtime-fixes-20260928 T2: ONE extra generation after an output_guard
# block, with a rule-aware corrective note appended to the last user message.
# Only the model sees it; it is never spoken. K4 (warmth-guardrails-runtime-
# 20261006): the notes live in the locale manifests
# (`llm.guardrail_retry_nudges`, keys never_promise / no_ai_self_identification
# / generic) and are read through `i18n_active.guardrail_retry_nudge`, so an
# en session no longer gets a Spanish (voseo) correction.
# Parses the rule id out of an `output_guard` block reason, formatted as
# "Non-negotiable violation [<rule_id>]: ..." (opencohost/config/validation.py).
_GUARD_REASON_RULE_ID_RE = re.compile(r"^Non-negotiable violation \[([^\]]+)\]")


def _guardrail_retry_nudge_for_reason(guard_reason: Optional[str]) -> str:
    """Pick the active locale's retry nudge for the rule named in ``guard_reason``.

    Falls back to the locale's generic nudge when ``guard_reason`` is missing,
    does not match the expected "Non-negotiable violation [<rule_id>]: ..."
    shape, or names a rule with no dedicated nudge.
    """
    rule_id = None
    if isinstance(guard_reason, str):
        match = _GUARD_REASON_RULE_ID_RE.match(guard_reason)
        if match:
            rule_id = match.group(1)
    return i18n_active.guardrail_retry_nudge(rule_id)


def _append_guardrail_nudge(messages: list, nudge: str) -> list:
    """Return a NEW messages list with ``nudge`` appended to the LAST user
    message's content -- never a trailing system message (Qwen3.5's chat
    template raises HTTP 400 "System message must be at the beginning" on
    one; other families silently drop it) and never mutating ``messages``
    or any dict inside it.
    """
    new_messages = list(messages)
    last_user_idx = None
    for i in range(len(new_messages) - 1, -1, -1):
        msg = new_messages[i]
        role = msg.get("role") if isinstance(msg, dict) else getattr(msg, "role", None)
        if role == "user":
            last_user_idx = i
            break

    if last_user_idx is None:
        # No user message found (should not happen in practice) -- append a
        # new user turn rather than ever emitting a system message here.
        new_messages.append({"role": "user", "content": nudge})
        return new_messages

    original = new_messages[last_user_idx]
    if isinstance(original, dict):
        updated = dict(original)
        original_content = updated.get("content", "") or ""
        updated["content"] = f"{original_content}\n\n{nudge}"
        new_messages[last_user_idx] = updated
    else:
        original_content = getattr(original, "content", "") or ""
        new_messages[last_user_idx] = {"role": "user", "content": f"{original_content}\n\n{nudge}"}
    return new_messages


# ── memoria draft promotion (memory_promotion_20260725) ─────────────────────
# Per-turn capture is deliberately cheap, permissive and LLM-free; nothing ever
# filtered afterward, so the store filled with vague half-memories Kira then
# recited ("hubo una corrección… algo técnico, no me acuerdo del detalle"). This
# is the missing step: ONE LLM call per launch judges the oldest unjudged
# drafts, rewrites the survivors so they stand alone, and promotes them.

from opencohost.core.memory.promotion_backoff_store import PromotionBackoffStore

# Batch and output cap are sized together: one KEEP decision (index, verdict,
# rewritten text up to _PROMOTION_TEXT_MAX_CHARS, ADR-062 `origin`) costs
# ~70-90 output tokens, so 8 decisions overflowed 512 and the array came back
# truncated (live: `considered=8 decided=1 missing_decision=7`). 4 x ~120
# worst-case tokens fits 1024 with headroom.
_PROMOTION_DRAFT_BATCH = 4
# Monotonic clock for the sweep's drain/skip cooldowns (a test seam).
_promotion_monotonic = time.monotonic
# S1: clock for the v5 evidence-retention job (patched in tests).
_retention_monotonic = time.monotonic
_MEMORY_RETENTION_INTERVAL_S = 6 * 3600
_PROMOTION_DRAFT_CHARS = 5000
_PROMOTION_NUM_PREDICT = 1024
_PROMOTION_TEXT_MAX_CHARS = 220
_PROMOTION_RESIDENCY_TIMEOUT_SECONDS = 2.0

# Adaptive judge budget (owner decision 5). A fixed 30s/90s would be a per-model
# assumption in disguise, contradicting the model-agnostic decision. Derived
# instead from `_pregen_last_gen_duration` — the SAME measurement
# `_pregen_retry_gate_seconds` already ships, with the SAME cold-start fallback
# (RETRY_MIN_REMAINING_SECONDS). No second measurement scheme.
_JUDGE_BUDGET_FACTOR = 2.0            # a judge reply is longer than a turn's
_JUDGE_REASONING_COLD_FACTOR = 3.0    # cold start on a thinking model (no num_predict cap, D2)
_JUDGE_BUDGET_FLOOR_SECONDS = 20.0
_JUDGE_BUDGET_CEILING_SECONDS = 90.0

# Reject reasons the judge itself may return. Anything else (including the
# parser-owned `not_self_contained`) collapses to "unspecified" so the counter
# can never be poisoned by an invented label. `assistant_sourced` (T1,
# memory-noise-amplification-20260928) may ALSO be applied deterministically,
# after the fact, by `_apply_assistant_subject_guard` when a KEEP's own
# rewritten text has Kira/the assistant as its subject — so this reason can
# show up even when the judge itself answered keep:true. `duplicate` (T2) is
# applied only by the store's post-judge dedup check and is never emitted by
# the judge model, so it is intentionally absent from this set.
_PROMOTION_JUDGE_REASONS = frozenset({
    "vague", "speculative", "trivial", "transient", "not_attributable",
    "assistant_sourced", "general_knowledge",
})

_PromotionParseDiagnostics = PromotionParseDiagnostics


def _output_guard_with_tts_check(text: str, source: str) -> tuple[bool, str]:
    """Delegate to generation_evaluator.output_guard_with_tts_check."""
    return _output_guard_with_tts_check_pure(text, source=source, output_guard_fn=output_guard)


def _guard_rule_id(reason: str) -> str:
    """Delegate to generation_evaluator.guard_rule_id."""
    return _guard_rule_id_pure(reason)


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
    "set_edge_voice",
    "set_profile",
    "clear_history",
    "switch_llm_tier",
    "switch_model",
    "set_discard_speech_residue",
    "set_interrupt_on_direct",
})

# §11 B4 primary path: verbs `_dispatch_command` parks while speech is
# active. `switch_model` is excluded because its branch carries its OWN
# mid-speech gate (`_pending_model_switch`: dedupes repeats, cancels on a
# switch-back) — a blind FIFO defer would apply every queued reload in
# sequence at the boundary instead of collapsing to the latest.
# `set_discard_speech_residue` is also excluded: the operator's toggle is
# immediately effective under `_lock` rather than delayed until speech boundary.
# `set_interrupt_on_direct` is excluded for the same reason (ADR-069): the
# switch must take effect while Kira is mid-speech, which is when it matters.
_SPEECH_DEFER_COMMANDS = _DRAIN_SAFE_COMMANDS - {
    "switch_model", "set_discard_speech_residue", "set_interrupt_on_direct",
}


# Carriers for _generar_dialogo's phase split are defined in and imported from
# opencohost.core.engine.generation_orchestrator (_StreamAttemptState, _GenerationAttemptOutcome).


@dataclass
class SpeechOutcome:
    """Step 1 (interruptible_speech_architecture_20260804 §3/§8): what one
    `_hablar_impl` invocation actually did, returned on EVERY exit path
    (clean completion, interruption, exception). Capture-and-discard only --
    nothing consumes this yet. `_hablar` propagates it verbatim; every
    existing caller ignores the return value (verified by grep), so this is
    purely additive.

    `cursor` is the index of the fragment OWED a full replay: on clean
    completion `cursor == len(chunks)`; on a mid-audio cut it is the exact
    in-flight fragment index unpacked from the queue item, NEVER
    `chunks_played` (that counter increments before the interrupted break and
    never advances past a failed synthesis -- see the CURSOR TRAP note at the
    interrupted branch in `_hablar_impl`).

    Every fragment index of `chunks` ends in exactly one of `spoken`,
    `skipped` (logged via `[SPEECH_LOST]`), or pending (`chunks[cursor:]`).
    """
    chunks: list
    cursor: int
    spoken: list
    skipped: list
    interrupted: bool
    error: Optional[str] = None


def _note_turn_arrival(engine) -> None:
    """Tell the owner-reflection job a turn is arriving (ADR-065). Tolerates the lightweight
    harnesses that call ``MotorVocalIA`` methods on a bare namespace."""
    hook = getattr(engine, "note_owner_turn_arrival", None)
    if hook is not None:
        hook()


def _log_turn_outcome(source: str, event: TurnEvent) -> None:
    """A turn that ends before it runs (refused, expired): its begin line and
    its one terminal line. Module level so engine stand-ins need no method.
    Never raises."""
    turn_state.open_turn(source)
    turn_state.emit(event)
    turn_state.close_turn()


def _get_turn_scheduler(engine) -> TurnScheduler:
    """Return the TurnScheduler for engine or bind one for test harnesses."""
    scheduler = getattr(engine, "_turn_scheduler", None)
    if scheduler is not None and isinstance(scheduler, TurnScheduler):
        return scheduler
    scheduler = TurnScheduler(
        max_items=getattr(engine, "_pq_max_items", 5),
        default_ttl_seconds=getattr(engine, "_pq_ttl_seconds", 30.0),
        direct_ttl_seconds=DIRECT_ANSWER_MAX_WAIT_SECONDS,
        direct_ttl_resolver=lambda: DIRECT_ANSWER_MAX_WAIT_SECONDS,
        priority_resolver=turn_priority.dispatch_priority_for_source,
        stream_ttl_resolver=turn_priority.effective_stream_ttl,
    )
    if hasattr(engine, "_priority_queue"):
        scheduler._replace_items_for_compat(engine._priority_queue)
    if hasattr(engine, "_pq_lock"):
        scheduler._replace_lock_for_compat(engine._pq_lock)
    try:
        object.__setattr__(engine, "_turn_scheduler", scheduler)
    except Exception:
        try:
            engine._turn_scheduler = scheduler
        except Exception:
            pass
    return scheduler


def _reply_sources(web_look, setup, source: str) -> tuple:
    """The sources that ride this turn's reply (runtime-fixes-20261110 T5): those of
    the web step, only when its text really is in the prompt. Logs one line with a
    count and the bare hosts; a URL is never logged."""
    found = getattr(web_look, "sources", ())
    if not isinstance(found, tuple) or not found or not _web_sources.carries_web_context(setup):
        return ()
    logger.info(
        "[WEB_SOURCES] source=%s count=%d hosts=%s", source, len(found), _web_sources.hosts_for_log(found)
    )
    return found


class MotorVocalIA(
    ScoutPromotionMixin,
    MemoriaCaptureMixin,
    OwnerReflectionMixin,
    AgendaStashMixin,
    ModelManagementMixin,
    CloudFallbackMixin,
    PregenCacheMixin,
    SpeechPipelineMixin,
    VisionTurnMixin,
    DecisionTurnMixin,
    WebTurnMixin,
    threading.Thread,
):
    """
    Hilo de IA: gestiona Ollama (LLM), memoria conversacional,
    y comunicación con el servidor TTS vía HTTP.
    """

    # Transitional compatibility surface for the existing characterization
    # suite. Queue state itself lives only in TurnScheduler; production methods
    # below delegate through its public operations.
    @property
    def _priority_queue(self):
        return self._turn_scheduler._items_for_compat()

    @_priority_queue.setter
    def _priority_queue(self, items):
        self._turn_scheduler._replace_items_for_compat(items)

    @property
    def _pq_lock(self):
        return self._turn_scheduler.lock

    @_pq_lock.setter
    def _pq_lock(self, lock):
        self._turn_scheduler._replace_lock_for_compat(lock)

    @property
    def _pq_max_items(self) -> int:
        return self._turn_scheduler.max_items

    @_pq_max_items.setter
    def _pq_max_items(self, value: int) -> None:
        self._turn_scheduler.max_items = value

    @property
    def _pq_ttl_seconds(self) -> float:
        return self._turn_scheduler.default_ttl_seconds

    @_pq_ttl_seconds.setter
    def _pq_ttl_seconds(self, value: float) -> None:
        self._turn_scheduler.default_ttl_seconds = value

    def _register_usage_profile_provider(self) -> None:
        """Let the usage ledger read the active profile id (id only; weakly held)."""
        try:
            import weakref

            from opencohost.core.usage import ledger

            ref = weakref.ref(self)
            ledger.set_profile_provider(
                lambda: getattr(ref(), "_current_profile_id", None) if ref() is not None else None
            )
        except Exception:
            logger.debug("usage ledger profile provider failed", exc_info=True)

    def __init__(self, log_queue, ui_callback, dialogue_callback: Optional[Callable[[str, str], None]] = None):
        super().__init__(daemon=True)
        self._init_communication_and_events(log_queue, ui_callback, dialogue_callback)
        self._init_provider_and_model_state()
        self._init_speech_and_tts_state()
        self._init_memory_and_context_state()
        self._init_scheduler_and_prefetch_state()
        self._init_services_and_telemetry()

    def _init_communication_and_events(self, log_queue, ui_callback, dialogue_callback):
        self.log_queue = log_queue
        self.ui_callback = ui_callback
        # P3 producer (opt-in): Kira's OWN generated reply text, never raw
        # viewer chat (R8). None = zero behavior change for the CTk app.
        self.dialogue_callback = dialogue_callback
        self.command_queue = queue.Queue()
        # §11 B4 (primary path): drain-safe verbs the run() loop consumed
        # while the router was speaking, parked for the next true boundary.
        # Engine-thread only (dispatch and drain both run there).
        self._deferred_control_commands: deque = deque()
        self.on_cloud_probe_scheduled: Optional[Callable[[dict], None]] = None
        self.on_chat_item_expired = None   # (info: dict) — a chat queue item expired (TTL)
        self.on_chat_turn_spoken = None    # () — a chat turn finished speaking
        self.on_guardrail_rejected: Optional[Callable[[str], None]] = None
        self.on_ctx_pressure_high: Optional[Callable[[dict], None]] = None
        self._lock = threading.Lock()

    def _init_provider_and_model_state(self):
        self._reasoning_model_cache: dict[str, bool] = {}
        self._model_ctx_limit: dict[str, int] = {}
        self._provider_config: dict = load_provider_config()
        self._cloud_fallback_active: bool = False
        self._last_cloud_failure_class: Optional[str] = None
        # Unit 2.1: "check your key" banner latch for `bad_key` -- one
        # ui_callback per failure EVENT, not per retried/repeated turn.
        # Resets alongside `_last_cloud_failure_class` on the next success.
        self._cloud_bad_key_notified: bool = False
        # B2: same latch for `quota_exhausted` (`cloud_quota_exhausted` event).
        self._cloud_quota_notified: bool = False
        self._cloud_fallback_reason: Optional[str] = None
        self.provider_epoch: int = 0
        # Monotonic deadline of the next scheduled background probe, or None
        # when no probe is scheduled (no active fallback, or a
        # non-probeable class: ambiguous_429/bad_key).
        self._cloud_probe_next_at: Optional[float] = None
        # The currently-running prober's cancellation flag + thread handle
        # (None when no prober is active). A manual provider PUT sets the
        # event so an in-flight wait/probe can bail without touching state
        # it no longer owns (`set_provider_config` below).
        self._cloud_prober_stop: Optional[threading.Event] = None
        self._cloud_prober_thread: Optional[threading.Thread] = None
        self._cloud_prober_generation: int = 0
        self._pending_fallback_notice_job: Optional[SpeechJob] = None

        self.is_ready = not self._is_local
        # False until run() finishes the first service check (and model warm-up).
        self.startup_check_done: bool = False
        _startup_model, self._model_source = resolve_startup_model()
        # S5: a startup model picked while Ollama could not be verified is
        # re-resolved once Ollama reports ready (_check_ollama_service).
        self._startup_model_value: str = _startup_model
        self._startup_model_unverified: bool = self._model_source in (
            "default", "invalid_saved_fallback", "saved_unverified",
        )
        self.current_model = _startup_model
        self._desired_model: str = _startup_model
        self._loaded_model: Optional[str] = None  # set after _prepare_model succeeds
        self._owns_ollama_model: bool = False
        self._current_profile_name: str = "default"
        self._current_profile_locale: Optional[str] = None
        self._extended_responses: bool = False
        # K5: per-profile warmth level ("cold" until a profile sets one).
        self._warmth: str = DEFAULT_WARMTH
        # P5 (kira-personal-tone-20261007): optional owner grammatical gender.
        self._owner_gender: str = DEFAULT_OWNER_GENDER
        # Stable profile UUID (R12) — written under _history_lock in
        # set_profile. Slice 1 introduces the field + lock-guarded write only;
        # the full snapshot/swap/clear critical section lands in slice 4.
        self._current_profile_id: str | None = None
        self._register_usage_profile_provider()
        _tier_config = LLMTierConfig(**resolve_llm_tiers())
        self.llm_tiers = LLMTierState(
            config=_tier_config,
            active_tier=self._infer_active_tier(_startup_model, _tier_config),
        )

        # Pending model switch (non-blocking retry)
        self._pending_model_switch: Optional[str] = None
        self._pending_switch_next_at: float = 0.0
        self._pending_switch_not_ready_logged: bool = False
        self._pending_ctx_reload_model: Optional[str] = None
        # Last switch failure info (read by UI handler, cleared after read)
        self._last_switch_failure: Optional[dict] = None
        self._warmed_model: Optional[str] = None
        self._ollama_chat_client = None
        self._ollama_scout_client = None
        # memory_promotion_20260725: same shape as the scout client, but built
        # per sweep with the adaptive judge budget (_judge_timeout_seconds).
        self._ollama_judge_client = None
        self._promotion_state: _PromotionState = _PromotionState.NOT_ELIGIBLE
        self._promotion_idle_since: Optional[float] = None
        self._promotion_ptt_held: bool = False
        self._promotion_swept: bool = False
        # memory-write-quality-20261020 (T2), monotonic deadlines: when a swept
        # backlog may be drained again, and when a gated (skipped) sweep may
        # retry. None = no deadline.
        self._promotion_drain_at: Optional[float] = None
        self._promotion_skip_until: Optional[float] = None
        # S4: set when a new draft is captured; re-arms a drained sweep after
        # the quiet window. Consumed by the sweep it triggers.
        self._promotion_dirty: bool = False
        # Monotonic time the run() loop last finished a dequeued command.
        self._last_command_at: Optional[float] = None
        # Last gate name reported by _promotion_gate, so a permanently inert
        # sweep logs once per CHANGED state instead of once per idle tick.
        self._promotion_last_gate: str = ""
        self._promotion_backoff_store = None
        self._scout_last_input_hash: Optional[str] = None
        self._last_llm_failure: Optional[dict] = None
        self._last_known_good_model: Optional[str] = _startup_model
        self._awaiting_first_success_after_switch: bool = False
        self._cold_warming_notified: bool = False
        self._inference_watchdog_timeout: float = float(OLLAMA_CHAT_TIMEOUT)
        self._post_switch_watchdog_timeout: float = min(float(OLLAMA_CHAT_TIMEOUT), 45.0)

    def _init_speech_and_tts_state(self):
        self.voz_referencia = None
        self._processing = False
        self._speaking = False
        self._llm_generating = False
        self._current_speech_source: Optional[str] = None
        # Source of the most recent speech boundary; kept past its end (see
        # `_speech_boundary_start`). None until the first boundary.
        self._speech_boundary_source: Optional[str] = None
        self._last_speaking_end_monotonic: Optional[float] = None
        self._speech_progress: Optional[dict] = None
        self._speaking_start_monotonic: Optional[float] = None
        self._last_speech_duration_ms: Optional[int] = None
        self._cancelled_speech_prefixes: tuple[str, ...] = ()
        self._current_processing_source: Optional[str] = None

        self.motor_tts = load_motor_tts()  # Default 'ligero' (edge-tts)

        self.tts_local_only: bool = load_tts_local_only()

        # Discard speech residue: when True, residual audio chunks from non-agenda
        # turns are discarded on interruption/arrival. Default False.
        with self._lock:
            self._discard_speech_residue: bool = load_discard_speech_residue()

        # Interrupt on direct (ADR-069): opt-in cut of non-owner speech when a
        # typed direct message ARRIVES. Default False = today's non-preemptive
        # arrival (the cut happens when the reply is submitted).
        with self._lock:
            self._interrupt_on_direct: bool = load_interrupt_on_direct()
        self._interrupt_on_direct_unarmed_logged: bool = False
        self._current_turn_tokens: frozenset = frozenset()

        # Per-locale Edge voice override (edge_voice.json). Read-only here: startup never writes it.
        # None -> the locale bundle's voice applies. The locale never hot-swaps, so one value suffices.
        self._edge_voice_override = load_edge_voice_for_locale(
            i18n_coherence.primary_subtag(i18n_active.get_active_bundle().code)
        )
        self._last_edge_switch_failure = None

        # Offline fallback: latches True on first Edge-TTS connection error;
        # subsequent chunks skip Edge-TTS for the rest of the session.
        # Also latched True at startup when the edge_tts package is absent.
        if edge_tts is None:
            logger.info("Edge-TTS is not installed; using Piper for light-engine synthesis.")
            self._edge_tts_offline: bool = True
        else:
            self._edge_tts_offline: bool = False
        # Transient Edge failures (HTTP 403/429/5xx, timeouts, unknown) never latch
        # the flag above; this breaker skips Edge for a cooldown after N in a row.
        self._edge_breaker = EdgeBreaker(
            EDGE_BREAKER_THRESHOLD, EDGE_BREAKER_COOLDOWN_S, EDGE_BREAKER_MAX_COOLDOWN_S
        )
        # The active locale's saved voice (piper_voice.json, one entry per locale)
        # wins. A missing or unresolvable entry falls back to the locale default
        # for THIS run only: the saved choice is never overwritten here.
        active_lang = i18n_coherence.primary_subtag(i18n_active.get_active_bundle().code)
        default_voice = default_piper_voice_for_locale(active_lang)
        self._piper_voice_key = default_voice
        self._piper_catalog_lang = None
        self._last_piper_switch_failure = None
        self._piper_startup_default = None
        initial_path = piper_voice_path(default_voice)
        initial_speaker = None
        saved = load_piper_voice_for_locale(active_lang)
        if saved is not None:
            try:
                saved_path, saved_lang = piper_catalog.resolve_voice(saved["voice"])
                if os.path.isfile(saved_path):
                    self._piper_voice_key = saved["voice"]
                    self._piper_catalog_lang = saved_lang
                    initial_path, initial_speaker = saved_path, saved["speaker_id"]
                    self._piper_startup_default = (default_voice, piper_voice_path(default_voice))
            except piper_catalog.PiperVoiceError:
                logger.warning("Saved Piper voice for the active locale is unavailable; using the default")
        self._tts_length_scale: float = load_tts_speed()
        self._piper = PiperEngine(
            initial_path, length_scale=self._tts_length_scale, speaker_id=initial_speaker
        )
        # Honest-degrade: one-shot ui_callback notice, latched the first time
        # Piper actually engages as a fallback under a locale/voice mismatch.
        self._piper_locale_mismatch_notified: bool = False

        # Optional health monitor for auto-fallback (set externally, None = backward compat)
        self.health_monitor = None

        # T4/P5 coherence gate at startup (warn-only; log_coherence never raises).
        i18n_coherence.log_coherence(
            i18n_active.get_active_bundle(),
            piper_voice_lang=self._active_piper_voice_lang(),
        )

        self._audio_reinit_needed: bool = False
        self._audio_disabled: bool = False

        # Lazily-built, cached PTT listening blip as an in-memory WAV file
        # (immutable bytes, reinit-proof). None until the first play_ptt_cue()
        # builds it from a pure-stdlib sine; see play_ptt_cue / _ptt_cue_wav.
        self._ptt_cue_wav_bytes = None
        # ...and the temp file those bytes are spilled to once, because
        # winsound refuses SND_ASYNC from memory (see _ptt_cue_wav_file).
        self._ptt_cue_wav_path = None

        # Speech-router host flag (interruptible_speech_architecture_20260804
        # §8 step 2): EngineHost turns it ON, CTK never does. OFF is the kill
        # switch — `_speak_or_submit` falls back to a direct, blocking `_hablar`.
        self._speech_router_enabled: bool = False
        # Built (and its daemon thread started) on the FIRST routed submit.
        self._speech_router = None
        self._streamed_turn_job: Optional[SpeechJob] = None
        self._streamed_turn_prefix: Optional[str] = None
        self._live_stream_state: Optional["_StreamAttemptState"] = None
        self._speech_interrupt_enabled: bool = False
        self._hablar_lock = threading.Lock()

    def _init_memory_and_context_state(self):
        self.system_prompt = i18n_active.system_prompt()
        self.use_system_role = False
        self._max_turns: int = HISTORY_MAX_TURNS
        # Profile-requested turns, clamped to ``_max_turns_cap`` into
        # ``_max_turns``. The cap follows the active model's effective ctx
        # (``_refresh_max_turns_cap``); 10 until Ollama is ready.
        self._requested_max_turns: int = HISTORY_MAX_TURNS
        self._max_turns_cap: int = context_budget.MAX_TURNS_CAP_FLOOR
        self.historial = deque(maxlen=self._history_maxlen(self._max_turns))

        # L1 pipeline memory — intra-session truncation ledger (D1/D2/D3).
        # Survives model switch / watchdog recovery; dies on set_profile,
        # clear_history, and app restart (RAM-only, never persisted to disk).
        self._memory_digest: MemoryDigest = MemoryDigest()
        # F4: a view file from a previous run never outlives it (best effort).
        self._session_topics_remove_view()
        # Sidecar watermark tracking turns already converted to digest lines
        # to prevent duplicate digest entries when deque rotates later (INV-DEDUP).
        self._digested_turn_keys: set[str] = set()
        # Dedicated lock for _commit_history (eviction capture + historial appends).
        # A separate lock avoids deadlock risk — self._lock must never be held on
        # any path that calls _commit_history.
        self._history_lock = threading.Lock()

        self._memorias_private: bool = False
        self._session_memoria_titles: list[str] = []
        self._memoria_store: Optional[MemoriaStore] = None
        self._memoria_store_lock = threading.Lock()
        self._memorias_pin_counter: Optional[tuple[int, int]] = None
        self._init_memory_v5()

    def _init_memory_v5(self) -> None:
        """Build the Memory v5 subsystem and log ONE startup line.

        Fail-open (a failure never blocks the engine) but never silent: the
        line goes to the ``OpenCohost`` logger, the only one routed to
        ``logs/opencohost_*.log``. It carries mode/status codes and, on
        failure, the exception TYPE name only -- never a message, path or
        content.
        """
        # OFF default avoids importing v5 domain modules.
        self._memory = None
        self._memory_v5_logged_reasons = set()
        req = "OFF"
        exc_type: Optional[str] = None
        self._memory_init_status_fallback = {"requested": "OFF", "effective": "OFF", "reason_code": "off_default"}
        try:
            from opencohost.config.settings import MEMORY_V5_MODE

            req = str(MEMORY_V5_MODE).upper() if isinstance(MEMORY_V5_MODE, str) else "OFF"
            self._memory_init_status_fallback = {"requested": req, "effective": "OFF", "reason_code": "off_default"}
            if req in ("SHADOW", "ACTIVE"):
                from opencohost.core.memory_v5_shadow.subsystem import MemorySubsystem

                self._memory = MemorySubsystem.from_settings()
                exc_type = getattr(self._memory, "init_exception_type", None)
        except Exception as exc:
            exc_type = type(exc).__name__
            self._memory = None
            self._memory_init_status_fallback = {"requested": req, "effective": "INIT_FAILED", "reason_code": "init_exception"}

        status = self._memory_init_status
        effective = status.get("effective", "OFF")
        failed = effective == "INIT_FAILED"
        line = "memory_v5: mode=%s effective=%s init=%s reason=%s" % (
            status.get("requested", req),
            effective,
            "failed" if failed else "ok",
            status.get("reason_code", "unknown"),
        )
        if exc_type:
            line += " exc=%s" % exc_type
        line += " " + self._memory_v5_db_fields()
        if failed:
            logger.warning(line)
        else:
            logger.info(line)

    @staticmethod
    def _memory_v5_db_fields() -> str:
        """Startup-line suffix naming the Memory v5 DBs in use. Never raises.

        Basenames only (full paths carry the OS username). ``override`` says
        which of the two env vars is set, so an inherited override pointing at
        a stale DB is visible. Reuses the paths already resolved by settings.
        """
        shadow, cache, override = "unknown", "unknown", "none"
        try:
            import os

            from opencohost.config import settings as _settings

            def _base(value: object) -> str:
                text = str(value).strip() if isinstance(value, (str, os.PathLike)) else ""
                if not text:
                    return "unknown"
                return text.replace("\\", "/").rsplit("/", 1)[-1] or "unknown"

            shadow = _base(getattr(_settings, "MEMORY_V5_SHADOW_DB", None))
            cache = _base(getattr(_settings, "MEMORY_V5_SEMANTIC_CACHE_DB", None))
            s_set = bool(os.environ.get("OPENCOHOST_MEMORY_V5_SHADOW_DB", "").strip())
            c_set = bool(os.environ.get("OPENCOHOST_MEMORY_V5_SEMANTIC_CACHE_DB", "").strip())
            override = "both" if s_set and c_set else "shadow" if s_set else "cache" if c_set else "none"
        except Exception:
            pass
        return "shadow_db=%s cache_db=%s override=%s" % (shadow, cache, override)

    def _init_scheduler_and_prefetch_state(self):
        # TurnScheduler owns dispatch queue state, sorting, TTL, and capacity bounds.
        self._turn_scheduler = TurnScheduler(
            max_items=5,
            default_ttl_seconds=30.0,
            direct_ttl_seconds=DIRECT_ANSWER_MAX_WAIT_SECONDS,
            direct_ttl_resolver=lambda: DIRECT_ANSWER_MAX_WAIT_SECONDS,
            priority_resolver=turn_priority.dispatch_priority_for_source,
            stream_ttl_resolver=turn_priority.effective_stream_ttl,
        )
        self._direct_drain_lock = threading.Lock()

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
        self._pregen_inflight: Optional[dict] = None
        # pregen-retrigger P3: (source, hash(payload)) of the last head whose cloud-gate
        # refusal was logged, so a retrigger does not repeat the line for the same head.
        self._pregen_refusal_logged_key: Optional[tuple] = None
        self._pregen_retried: bool = False
        self._pregen_last_gen_duration: Optional[float] = None
        self._frozen_stash: Optional[dict] = None
        self._detour_turns: int = 0
        self._frozen_stash_at: Optional[float] = None
        self._connector_last_idx: Optional[int] = None

        self._test_pop_boundary_hook: Optional[Callable[[], None]] = None
        self._test_store_to_finally_hook: Optional[Callable[[], None]] = None
        self.agenda_output_validator = None
        self.agenda_output_preview_validator = None
        self.agenda_output_recorder = None
        self.agenda_output_transformer = None
        self.agenda_controller = None
        self.direct_editorial_context_provider = None  # set externally by app_shell
        self.direct_editorial_usage_recorder = None

    def _init_services_and_telemetry(self):
        # LLMInferenceService owns pure multi-provider model execution and token streams.
        self._inference_service = get_inference_service(self)
        self._prompt_assembler = PromptContextAssembler(
            sanitize_history_fn=self._sanitize_history_context
        )
        self._generation_evaluator = GenerationEvaluator(
            output_guard_fn=lambda t, source="": output_guard(t, source=source),
            guardrail_fallback_fn=self._guardrail_fallback_line,
            detect_repetition_fn=lambda cand, rec, **kw: getattr(
                sys.modules.get("opencohost.core.llm_engine"),
                "detect_repetition",
                detect_repetition,
            )(cand, rec, **kw),
            sanitize_clause_fn=lambda text: getattr(
                sys.modules.get("opencohost.core.llm_engine"),
                "sanitize_clause_repetition",
                sanitize_clause_repetition,
            )(text),
        )
        self._generation_orchestrator = GenerationOrchestrator(self)
        self._ctx_telemetry_ring = CtxTelemetryRing(maxlen=CTX_TELEMETRY_RING_MAXLEN)
        from opencohost.core.engine.inference_telemetry import InferenceTelemetryTracker
        self._inference_telemetry = InferenceTelemetryTracker()

    @property
    def is_speaking(self):
        """Public speech predicate — `_speech_active` since step 2.

        Converting this ONE property carries every API reader with it
        (agenda_driver :300/:595, routers/status, ptt_session, routers/chat,
        shared), which is why none of them changes a line.
        """
        return self._speech_active

    @property
    def is_processing(self):
        with self._lock:
            return self._processing

    @property
    def llm_generating(self) -> bool:
        """WU3 GPU-free predicate (design-fase2.md §2.3): True only while an
        Ollama generation call is actually in flight (foreground or pregen)."""
        with self._lock:
            return self._llm_generating

    @property
    def current_speech_source(self):
        with self._lock:
            return self._current_speech_source

    @property
    def current_processing_source(self):
        with self._lock:
            return self._current_processing_source

    @property
    def inference_telemetry(self):
        """ADR-056 WU1: Thread-safe observational decode telemetry tracker."""
        tracker = getattr(self, "_inference_telemetry", None)
        if tracker is None:
            from opencohost.core.engine.inference_telemetry import InferenceTelemetryTracker
            tracker = InferenceTelemetryTracker()
            self._inference_telemetry = tracker
        return tracker

    def _resolve_reasoning_settings(self, model: str) -> dict:
        """ADR-056 WU3/WU4: Resolve reasoning and budget settings for a model."""
        try:
            from opencohost.config.model_parameters import get_model_reasoning_settings
            return get_model_reasoning_settings(model)
        except Exception:
            return {"enabled": False, "budget_tokens": 512, "preset": "balanced"}

    # Backward-compatible Memory v5 facade properties delegating to self._memory
    @property
    def _memory_runtime(self):
        return self._memory.runtime if getattr(self, "_memory", None) is not None else None

    @_memory_runtime.setter
    def _memory_runtime(self, val):
        if getattr(self, "_memory", None) is None:
            from opencohost.core.memory_v5_shadow.subsystem import MemorySubsystem

            self._memory = MemorySubsystem(mode="SHADOW", runtime=val)
        else:
            self._memory._runtime = val

    @property
    def _memory_run_id(self):
        return self._memory.run_id if getattr(self, "_memory", None) is not None else None

    @_memory_run_id.setter
    def _memory_run_id(self, val):
        if getattr(self, "_memory", None) is None:
            from opencohost.core.memory_v5_shadow.subsystem import MemorySubsystem

            self._memory = MemorySubsystem(mode="SHADOW")
            self._memory._run_id = val
        else:
            self._memory._run_id = val

    @property
    def _memory_stream_seq(self):
        return self._memory.stream_seq if getattr(self, "_memory", None) is not None else 0

    @_memory_stream_seq.setter
    def _memory_stream_seq(self, val):
        if getattr(self, "_memory", None) is not None:
            self._memory._stream_seq = val

    @property
    def _memory_init_status(self):
        if getattr(self, "_memory", None) is not None:
            return self._memory.status
        return getattr(
            self,
            "_memory_init_status_fallback",
            {"requested": "OFF", "effective": "OFF", "reason_code": "off_default"},
        )

    @_memory_init_status.setter
    def _memory_init_status(self, val):
        self._memory_init_status_fallback = val
        if getattr(self, "_memory", None) is not None:
            self._memory._status = val

    @property
    def _semantic_cache_store(self):
        return self._memory.cache_store if getattr(self, "_memory", None) is not None else None

    @property
    def _semantic_worker(self):
        return self._memory.worker if getattr(self, "_memory", None) is not None else None

    @property
    def _semantic_indexer(self):
        return self._memory.indexer if getattr(self, "_memory", None) is not None else None

    @property
    def _episodic_recall_coordinator(self):
        return self._memory.coordinator if getattr(self, "_memory", None) is not None else None

    @_episodic_recall_coordinator.setter
    def _episodic_recall_coordinator(self, val):
        if getattr(self, "_memory", None) is None:
            from opencohost.core.memory_v5_shadow.subsystem import MemorySubsystem

            self._memory = MemorySubsystem(coordinator=val)
        else:
            self._memory._coordinator = val

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
        # Pre-warm the post-switch watchdog's 45s client too (memoized by
        # timeout in `_ollama_chat_clients`) so `_ollama_chat` never falls
        # back to the 180s default during the post-switch window -- see
        # `_create_ollama_chat_client`'s docstring for the root cause.
        self._create_ollama_chat_client(ollama, timeout=self._post_switch_watchdog_timeout)
        self._ollama_scout_client = self._create_ollama_scout_client(ollama)

        try:
            self.pygame.mixer.init()
        except Exception as e:
            self._log(
                f"ADVERTENCIA: No se pudo inicializar pygame.mixer ({e}). Continuando en modo headless/audio deshabilitado.",
                level="warning",
            )
            self._audio_disabled = True

        if TTS_LOCAL_MODEL_PATH:
            if not self._piper.load():
                self._piper_startup_fallback()
        try:
            self._check_ollama_service()
        finally:
            self.startup_check_done = True
        threading.Thread(target=self._prewarm_tts, name="TTSPrewarm", daemon=True).start()
        self._log(f"Modelo inicial: {self.current_model} (fuente: {self._model_source})")
        self._log("Motor IA inicializado. Esperando comandos...")

        while True:
            try:
                comando = self.command_queue.get(timeout=1.0)
            except queue.Empty:
                self._drain_control_commands()
                self._process_priority_queue()
                self._check_pending_model_switch()
                self._ollama_reprobe_tick()
                self._owner_reflection_tick()
                self._promotion_idle_step()
                self._memory_retention_tick()
                continue

            if comando is None:
                self._log("Señal de cierre recibida. Terminando hilo IA.")
                break

            self._consume_command(comando)
            # Stamped after the command (not at dequeue) so a long turn counts
            # from when it ended, which is when the owner is likely to speak again.
            self._stamp_command_done()

    def _stamp_command_done(self) -> None:
        self._last_command_at = _promotion_monotonic()

    def _promotion_quiet(self, now: float) -> bool:
        """True when no command finished within MEMORIAS_PROMOTION_QUIET_S."""
        last = self._last_command_at
        return last is None or now - last >= MEMORIAS_PROMOTION_QUIET_S

    def _memory_retention_tick(self) -> None:
        """Engine thread, idle tick: run the bounded v5 evidence-retention job
        at most once per _MEMORY_RETENTION_INTERVAL_S (first run on the first
        idle tick after startup). Fail-open; logs counts only."""
        mem = getattr(self, "_memory", None)
        if mem is None:
            return
        now = _retention_monotonic()
        last = getattr(self, "_memory_retention_at", None)
        if last is not None and now - last < _MEMORY_RETENTION_INTERVAL_S:
            return
        self._memory_retention_at = now
        try:
            out = mem.run_retention()
            if out.get("events") or out.get("error"):
                logger.info(
                    "memory_v5: retention events=%d episodes=%d%s",
                    int(out.get("events", 0)),
                    int(out.get("episodes", 0)),
                    f" error={out['error']}" if out.get("error") else "",
                )
        except Exception as exc:
            logger.warning("memory_v5: retention failed exc=%s", type(exc).__name__)

    def _promotion_mark_dirty(self) -> None:
        """A draft was captured: a drained sweep may re-arm after the quiet window."""
        self._promotion_dirty = True

    def _promotion_idle_step(self) -> None:
        """Engine thread, idle tick only: run at most one bounded promotion batch.

        A sweep leaves ``_promotion_swept`` set; while it left unjudged drafts
        behind, ``_promotion_drain_at`` re-arms it once MEMORIAS_PROMOTION_REARM_S
        have passed since it finished. A drained sweep re-arms only when a new
        draft was captured (``_promotion_dirty``), after the quiet window. A gated (skipped) sweep waits the same
        cooldown before retrying. A queued command always wins the tick.
        """
        if not self.command_queue.empty():
            return
        now = _promotion_monotonic()
        if self._promotion_swept:
            if self._promotion_drain_at is None:
                # Drained: only a freshly captured draft re-arms it.
                if not self._promotion_dirty:
                    return
            elif now < self._promotion_drain_at:
                return
            if not self._promotion_quiet(now):
                return
            self._promotion_swept = False
        elif self._promotion_skip_until is not None:
            if now < self._promotion_skip_until or not self._promotion_quiet(now):
                return
        self._promotion_drain_at = None
        self._promotion_skip_until = None
        self._promotion_dirty = False
        try:
            result = self.promote_pending_drafts()
        except Exception as exc:
            self._promotion_swept = True  # a raising sweep is an attempt
            logger.warning(
                "memoria promotion sweep escaped isolation: %s", type(exc).__name__
            )
            return
        finished = _promotion_monotonic()
        if result.get("skipped"):
            self._promotion_skip_until = finished + MEMORIAS_PROMOTION_REARM_S
            return
        self._promotion_swept = True
        if result.get("unjudged_remaining", 0) > 0:
            self._promotion_drain_at = finished + MEMORIAS_PROMOTION_REARM_S

    def _consume_command(self, comando) -> None:
        """Unpack one command tuple and dispatch it. Extracted from run() for
        testability (mirrors _dispatch_command's own extraction).

        Tolerant unpack (A1, memoria_quality_20260717): legacy 2-tuples
        (app_shell chat + control verbs) carry no history_text; the F10 PTT
        Dispatcher (dispatch.py) and the legacy voice_control.py idle path put a
        3-tuple (command, payload, history_text). Mirrors the priority-queue
        4->5-tuple precedent in _process_priority_queue
        (prio, ts, payload, source, *rest).

        B2 (turn provenance): an OPTIONAL 4th element carries the real source
        ((command, payload, history_text, source)) — see _dispatch_command. A
        shorter tuple keeps the "direct" default, so no existing caller changes.
        """
        tipo, payload, *rest = comando
        history_text = rest[0] if rest else None
        source = rest[1] if len(rest) > 1 and rest[1] else "direct"
        submitted_at = rest[2] if len(rest) > 2 else None
        submitted_under_provider = rest[3] if len(rest) > 3 else None
        # runtime-fixes-20261110 T2: the door the turn came through, stamped by
        # the receiving route (core/turn_origin.py). Absent -> unknown -> not desktop.
        origin = rest[4] if len(rest) > 4 else None
        stamp = (
            TurnStamp(
                submitted_at=submitted_at, submitted_under_provider=submitted_under_provider, origin=origin
            )
            if submitted_at is not None else None
        )
        self._dispatch_command(tipo, payload, history_text=history_text, source=source, stamp=stamp)

    def _dispatch_command(
        self,
        tipo: str,
        payload,
        history_text: Optional[str] = None,
        source: str = "direct",
        stamp: Optional[TurnStamp] = None,
    ) -> None:
        """Dispatch a command tuple. Extracted from run() for testability.

        history_text (A1, memoria_quality_20260717), when present, is the honest
        text committed to historial for a process_context turn — carried by the
        F10 PTT Dispatcher path and the legacy voice_control.py idle path. None
        keeps the pre-existing behavior (payload committed as-is) for every
        chat/control caller.

        source (B2, turn provenance) is the honest origin of a process_context
        turn, threaded from the Dispatcher (api/dispatch.py) via _consume_command.
        It replaces the hardcoded "direct" this branch used on BOTH the idle and
        busy re-enqueue paths, which made every F10 PTT turn log/telemeter as
        "direct". "direct" stays the default, so every existing caller (app_shell
        chat, control verbs, the boundary drain) is unchanged.

        NOTE (F1): threading the real source must not cost a voice turn any
        prompt enrichment. "direct" and "ptt" are both allowlisted for digest
        capture, personalization, memorias injection AND — since F1 —
        _DIGEST_INJECT_SOURCES / _EDITORIAL_INJECT_SOURCES, so an F10 PTT turn
        now behaves like the legacy voice_control PTT path (which has always
        dispatched source="ptt") with every enrichment a typed turn gets. The
        one remaining direct-only gate is the read-only memory inspector's
        user-content dump (memory_inspector_snapshot), which is a privacy
        display gate, not a prompt gate."""
        if tipo == SPEECH_BOUNDARY_COMMAND:
            self._complete_processing_cycle(emit_idle=False)
            return

        if self._speech_active and tipo in _SPEECH_DEFER_COMMANDS:
            self._deferred_control_commands.append((tipo, payload))
            logger.info("Control command '%s' deferred until the speech boundary", tipo)
            return

        if tipo == "set_voice":
            self.voz_referencia = payload
            if isinstance(payload, tuple):
                self.voz_referencia = payload[0]
            self._log(f"Perfil de voz configurado: {self.voz_referencia}")

        elif tipo == "check_ollama":
            self._check_ollama_service()

        elif tipo == "process_context":
            if not self.is_ready and self._is_local:
                # Stale flag recovery: Ollama may have come up after the startup
                # check failed. At most one probe per window; refusals stay cheap.
                now = time.monotonic()
                if now >= self._turn_recover_next_at:
                    self._turn_recover_next_at = now + self._TURN_RECOVER_MIN_INTERVAL_S
                    self._recover_local_readiness()
            if not self.is_ready:
                self._log(
                    "Ollama no esta listo. Se reintenta en segundo plano; "
                    "verifica que Ollama este en ejecucion.",
                    level="warning",
                )
                self.ui_callback("ollama_unavailable")
                _log_turn_outcome(source, TurnEvent.REFUSE)
                if self._interrupt_on_direct and stamp is not None:
                    # ADR-069: a refused direct turn never replies; clear ITS hold.
                    self._release_arrival_cut("refused", token=stamp.submitted_at)
                return
            if self._processing or self._speech_active:
                # Motor busy — enqueue to priority queue instead of dropping
                self._log("Ya procesando. Encolando en cola prioritaria...", level="debug")
                busy_priority = turn_priority.dispatch_priority_for_source(source)
                if stamp is not None:
                    self.enqueue(
                        payload, priority=busy_priority, source=source, history_text=history_text,
                        submitted_at=stamp.submitted_at, submitted_under_provider=stamp.submitted_under_provider,
                        **({"origin": stamp.origin} if stamp.origin is not None else {}),
                    )
                else:
                    self.enqueue(payload, priority=busy_priority, source=source, history_text=history_text)
                if self._speech_interrupt_enabled and self._speech_router_enabled:
                    self._process_priority_queue()
                return
            with self._lock:
                self._processing = True
                self._current_processing_source = source
                # ADR-069: same token the priority-queue path publishes, so a
                # direct turn consumed from command_queue owns (and, at turn
                # end, releases) the hold its arrival set.
                self._current_turn_tokens = frozenset(
                    t for t in (stamp.submitted_at if stamp is not None else None,)
                    if t is not None and source in _OWNER_QUESTION_SOURCES
                )
            self.ui_callback("processing")
            self._note_detour_turn(source)
            turn_state.open_turn(source)
            try:
                if stamp is not None:
                    self._ejecutar_inferencia(payload, source=source, history_text=history_text, stamp=stamp)
                else:
                    self._ejecutar_inferencia(payload, source=source, history_text=history_text)
            finally:
                turn_state.close_turn()
                self._complete_processing_cycle()

        elif tipo == "clear_history":
            self.historial.clear()
            self._memory_digest.clear()
            self._digested_turn_keys.clear()
            self._session_topics_reset()
            if getattr(self, "_active_topics", None) is not None:
                self._active_topics.clear()
            self._owner_reflection_reset()
            self._log("Historial de conversación limpiado.")

        elif tipo == "switch_model":
            new_model = payload
            if self._is_model_switch_noop(new_model):
                if not self.is_ready and self._is_local:
                    # Same model while not ready: the owner is retrying a
                    # recovery, so reconcile instead of swallowing the request.
                    if not self._recover_local_readiness():
                        self._log(
                            f"Switch a {new_model} rechazado: Ollama no esta listo.",
                            level="warning",
                        )
                        self.ui_callback("model_switch_failed")
                return

            if not self.is_ready and self._is_local:
                self._reconcile_local_readiness()

            if not self.is_ready:
                self._log(f"Switch a {new_model} rechazado: Ollama no esta listo.", level="warning")
                self.ui_callback("model_switch_failed")
                return

            self._desired_model = new_model

            if not self._processing and self._speech_active:
                # K2: only speaking, no generation in flight -> cut the
                # playback and switch now instead of waiting out the speech
                # (a 76 s stall in the 2026-10-05 session).
                self._log(f"Switch a {new_model}: deteniendo el habla para aplicarlo ya.", level="info")
                self._pending_model_switch = None
                self.stop_all_speech()
            elif self._processing:
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
            # A draft generated under the old tier must never be spoken (#344).
            self.clear_prefetched_agenda()
            self._invalidate_frozen_stash()  # WU5 D2: a stashed return under the old tier is stale
            self.switch_llm_tier(str(payload))

        elif tipo == "set_motor_tts":
            choice = str(payload).strip() if isinstance(payload, str) else ""
            if choice not in ("ligero", "pesado"):
                logger.warning("Ignoring invalid motor_tts payload: %r", payload)
            elif choice == "pesado" and not settings.EXPERIMENTAL_HEAVY_TTS_ENABLED:
                logger.warning("Ignoring set_motor_tts='pesado' while heavy TTS is disabled")
            else:
                self.motor_tts = choice
                save_motor_tts(choice)
                nombre = "Ligero (Edge-TTS)" if choice == "ligero" else "Pesado (Qwen3-TTS)"
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

        elif tipo == "set_discard_speech_residue":
            val = bool(payload)
            with self._lock:
                self._discard_speech_residue = val
            save_discard_speech_residue(val)
            logger.info("Discard speech residue set to %s", val)

        elif tipo == "set_interrupt_on_direct":
            val = bool(payload)
            with self._lock:
                self._interrupt_on_direct = val
            save_interrupt_on_direct(val)
            if not val:
                self._release_arrival_cut("switch_off")
            logger.info("Interrupt on direct set to %s", val)

        elif tipo == "set_tts_speed":
            scale = float(payload)
            self._tts_length_scale = scale
            self._piper.set_length_scale(scale)
            save_tts_speed(scale)
            logger.info("Piper speech rate set to length_scale=%.2f", scale)

        elif tipo == "set_piper_voice":
            self._switch_piper_voice(payload)

        elif tipo == "set_edge_voice":
            self._switch_edge_voice(payload)

        elif tipo == "set_profile":
            # A draft generated under the old persona must never be spoken (#344).
            self.clear_prefetched_agenda()
            self._invalidate_frozen_stash()  # WU5 D2: a stashed return under the old persona is stale
            prompt_override_active = "prompt" in payload
            self.system_prompt = payload.get("prompt", i18n_active.system_prompt())
            self.use_system_role = payload.get("use_system", False)
            # kira-identity-core-rules-20261007 C1: only a persona that really
            # differs from the locale prompt gets the harness core rules.
            self._persona_custom = bool(
                prompt_override_active
                and str(self.system_prompt).strip() != str(i18n_active.system_prompt()).strip()
            )
            self._extended_responses = bool(payload.get("extended_responses", False))
            # K5: a missing/unknown warmth is cold; the output guard reads the
            # same level (human/body-claim check runs only while warm).
            self._warmth = normalize_warmth(payload.get("warmth"))
            warmth_state.set_active_warmth(self._warmth)
            self._owner_gender = normalize_owner_gender(payload.get("owner_gender"))
            profile_name = payload.get("_profile_name", "desconocido")
            self._current_profile_name = profile_name
            self._departing_switch_locale = self._resolve_turn_lang()
            raw_locale = payload.get("locale")
            self._current_profile_locale = (
                i18n_coherence.primary_subtag(raw_locale) if raw_locale else None
            )
            raw_max_turns = payload.get("max_turns")
            try:
                requested_turns = (
                    max(1, int(raw_max_turns)) if raw_max_turns is not None else HISTORY_MAX_TURNS
                )
            except (ValueError, TypeError):
                requested_turns = HISTORY_MAX_TURNS
            self._requested_max_turns = requested_turns
            # Cap derived from the active model's effective ctx (10 at 4096 or
            # when unknown); recomputed on model switch / engine ready.
            self._max_turns_cap = self._compute_max_turns_cap()
            self._max_turns = min(requested_turns, self._max_turns_cap)
            with self._history_lock:
                switch_drafts = self._collect_flush_drafts()
                departing_profile_id = self._current_profile_id
                summary_titles = list(self._session_memoria_titles)
                self._session_memoria_titles.clear()
                self._current_profile_id = payload.get("id")
                self.historial = deque(maxlen=self._history_maxlen(self._max_turns))
                self._memory_digest.clear()
                self._digested_turn_keys.clear()
                self._session_topics_reset()
                self._owner_reflection_reset()
                mem = getattr(self, "_memory", None)
                if mem is not None:
                    mem.on_profile_switch(departing_profile_id, payload.get("id"))
            self._dispatch_switch_flush(switch_drafts, departing_profile_id, summary_titles)
            self._log(f"Perfil actualizado: {profile_name} (System Role: {self.use_system_role}). Memoria limpiada.")
            # T4 coherence gate (warn-only; the profile always wins). Flags when a
            # custom persona's language is not governed by the active locale.
            i18n_coherence.log_coherence(
                i18n_active.get_active_bundle(),
                profile_name=profile_name,
                profile_prompt_active=prompt_override_active,
                profile_locale=payload.get("locale"),
                piper_voice_lang=self._active_piper_voice_lang(),
            )

    def enqueue(
        self,
        payload: str,
        priority: Optional[int] = None,
        source: str = "chat",
        history_text: Optional[str] = None,
        submitted_at: Optional[float] = None,
        submitted_under_provider: Optional[str] = None,
        origin: Optional[str] = None,
    ) -> None:
        """Add a message to the priority queue.

        Args:
            payload: The text to process.
            priority: Dispatch tier. 0 = PTT/voice, 1 = direct (always above
                stream), 2/3 = stream vs agenda — their relative order is the
                streamer's STREAM_OVER_AGENDA setting (turn_priority module).
                None (the default) resolves from `source` AT ENQUEUE TIME via
                turn_priority.dispatch_priority_for_source, so the live
                setting is honored. The old default (`priority=1`) predates
                stream and put viewer chat in the direct tier — a typed owner
                question then queued FIFO behind viewer reactions.
            source: Origin identifier for logging.
            history_text: Honest text to commit to historial for this turn
                (agenda_ptt_commit_raw_text). None (default) commits `payload`
                as before — every caller other than agenda-PTT stays unchanged.
            submitted_at: Unit 4.1 (runtime_findings_batch_20260731, F5) — the
                monotonic stamp of the original front-end submission (API
                dispatch entry), carried through so `_process_priority_queue`
                can compute an honest queue_wait_ms at pop time. None for every
                internally-generated item (agenda's own turns, accumulation) —
                those never fake a wait they didn't measure.
            submitted_under_provider: Unit 4.2 (F12 closure) — the provider
                posture at submit time, carried through so a fallback/return
                that happens while the item sits queued can be disclosed on
                the reply instead of silently answering under a different
                provider. None for every internally-generated item.
            origin: runtime-fixes-20261110 T2 -- the door the turn came through
                ("desktop" / "remote", core/turn_origin.py), carried to the
                pop so a link is read only for a desktop turn. None = unknown
                = not desktop.
        """
        _note_turn_arrival(self)
        result = _get_turn_scheduler(self).enqueue(
            payload,
            priority=priority,
            source=source,
            history_text=history_text,
            submitted_at=submitted_at,
            submitted_under_provider=submitted_under_provider,
            **({"origin": origin} if origin is not None else {}),
        )
        for dropped in result.dropped:
            self._log(f"Cola prioritaria llena. Descartado (baja prioridad): {dropped[3]}")
            self.enqueue_accumulation(dropped[2], source=dropped[3])
        self._maybe_trigger_interactive_pregen(result.head_snapshot)

    def replace_pending(self, payload: str, priority: Optional[int] = None, source: str = "chat") -> None:
        """Replace stale pending items from the same source and enqueue a fresh one.

        This keeps product features such as Agenda Mode from stacking old
        autonomous turns while preserving unrelated higher-priority items.
        """
        _note_turn_arrival(self)
        _get_turn_scheduler(self).drop_source(source)
        if source.startswith("kira-agenda"):
            self._clear_prefetch_unless_matches(payload, source)
        self.enqueue(payload, priority=priority, source=source)

    def pending_owner_questions(self) -> int:
        """Owner questions (typed OR spoken) waiting in the priority queue.

        interruptible_speech_architecture_20260804 §OQ-3. Read-only, one
        `_pq_lock` acquisition, same shape as `has_pending_priority_before`
        above. Two readers: Rule 2 in `_maybe_trigger_interactive_pregen` (do
        not draft a single question a bundle is about to supersede) and the
        agenda driver's turn gate (do not take the mic while the owner waits).
        Honest only because Step 1 made both arrival paths drain into this queue
        at arrival — a question still sitting in `command_queue` is invisible
        here, which is exactly the gap those hooks close.
        """
        return _get_turn_scheduler(self).pending_owner_questions()

    def drop_pending_sources(self, prefixes: tuple[str, ...]) -> int:
        """Drop pending priority/accumulation items whose source matches prefixes.

        Args:
            prefixes: Source prefixes to remove, e.g. ("kira-agenda",).

        Returns:
            Number of pending items removed.
        """
        removed = len(_get_turn_scheduler(self).drop_sources(prefixes))

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
            self._invalidate_frozen_stash()

        router = self._speech_router
        if router is not None:
            router.sweep_sources(prefixes)

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
        _note_turn_arrival(self)
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

    def _take_owner_bundle_prefix(self, head_item) -> list:
        """Remove a bounded CONTIGUOUS prefix of owner items from _priority_queue.

        interruptible_speech_architecture_20260804 §4 step 10. `head_item` is
        the tuple ALREADY popped by the caller; it is not re-taken here, only
        counted against the char cap.

        PREFIX-ONLY on purpose. Because it can never reach past a viewer `chat`
        or an agenda item, it can never reorder anything relative to one: the
        split's ordering guarantee stays a property of the existing
        (priority, timestamp) sort rather than of this function. It is also what
        makes viewer text structurally unable to enter an owner bundle (§5.1).

        Over cap it DEFERS, it never drops. The remainder is simply not taken —
        it stays in the queue, still sorted, and forms the next bundle at the
        next boundary. This is the deliberate divergence from
        _flush_accumulation's drop-oldest caps: dropping viewer chat is a
        defensible policy, dropping the host's own question is data loss.

        Caller must NOT hold _pq_lock. Acquires and releases it, and calls
        nothing while holding it — not _flush_accumulation, not
        _clear_prefetch_if_matches, not _log — so no new lock edge is created
        and the "never hold _pq_lock across _prefetch_lock" invariant survives.
        """
        return _get_turn_scheduler(self).take_owner_prefix(head_item)

    def _compose_owner_bundle(self, members: list) -> tuple:
        """Render queue tuples into one (payload, history_text, stamp). Pure: no locks, no I/O."""
        return _get_turn_scheduler(self).compose_bundle(members)

    def _log_owner_bundle(self, members: list, payload: str) -> None:
        """One INFO line per bundle (§OQ-8). The owner validates this feature by
        grepping logs, so this line is a deliverable, not a nicety.

        METADATA ONLY — per-source counts, never text — same rule as
        [CLAUSE_SANITIZER] and the project's "never expose raw chat in prompts,
        logs, or persistence".

        `oldest_wait_ms` is deliberately the SUBMIT clock (monotonic, stamped at
        the API dispatch entry), not the queue-insertion TTL clock: the two
        start at different moments whenever an item sat in `command_queue`
        before being drained, and only the submit clock is the honest
        end-to-end wait. -1 when no member carried a stamp.

        `deferred` counts owner questions still in `_priority_queue` after this
        bundle closed — the split signal. Slightly broader than "because a cap
        tripped": a viewer `chat` item between two owner questions also ends the
        prefix, and those deferred questions are counted too. That is the number
        the owner actually wants ("how many of mine are still waiting").
        """
        stamps = [it[5] for it in members if len(it) > 5 and it[5] is not None]
        oldest_wait_ms = max(0, int((time.monotonic() - min(stamps)) * 1000)) if stamps else -1
        sources = ",".join(
            f"{src}:{n}" for src, n in sorted(Counter(it[3] for it in members).items())
        )
        logger.info(
            "[BUNDLE] count=%d sources=%s chars=%d oldest_wait_ms=%d deferred=%d",
            len(members), sources, len(payload), oldest_wait_ms,
            self.pending_owner_questions(),
        )

    def _requeue_owner_bundle_followers(self, followers: list) -> None:
        """Put a FAILED bundle's followers back into _priority_queue.
        The HEAD is deliberately NOT re-queued. Each failure still burns exactly
        one question, so a persistently failing model DRAINS the backlog instead
        of spinning on it forever.

        ORIGINAL tuples, never rebuilt: original priority and original insertion
        timestamp, so TTL and ordering behave exactly as if the prefix had never
        been taken. A rescued question can therefore still TTL-expire on a later
        sweep — correct, it is the pre-bundling outcome and the sweep logs it.

        Same lock + re-sort discipline as `enqueue`. It deliberately does NOT
        re-run enqueue's max-items trim: the cap held a moment ago, so it can
        only be exceeded by arrivals during the failed generation, and the very
        next `enqueue()` trims back to `_pq_max_items` on its own.
        """
        _get_turn_scheduler(self).requeue(followers)
        # pregen-retrigger: no re-evaluation here on purpose. The failed turn is
        # still _processing, so the armed trigger would refuse; the drain's
        # blocked-head hook (_retrigger_interactive_pregen_for_blocked_head)
        # re-evaluates the requeued head right after this turn's cycle closes.
        # METADATA ONLY, same rule as [BUNDLE] and [CLAUSE_SANITIZER]. WARNING,
        # not INFO: a silent recovery is only marginally better than a silent
        # loss, and this line is how the owner greps that it happened at all.
        sources = ",".join(
            f"{src}:{n}" for src, n in sorted(Counter(it[3] for it in followers).items())
        )
        logger.warning(
            "[BUNDLE_FAILED] requeued=%d sources=%s head_consumed=1",
            len(followers), sources,
        )

    def _process_priority_queue(self) -> None:
        """Process priority-queue items while the motor is idle.

        Non-PTT items older than _pq_ttl_seconds are discarded before selection
        to prevent stale reactions after long delays (kira-agenda* is exempt,
        and `direct` is bounded by DIRECT_ANSWER_MAX_WAIT_SECONDS instead — see
        the F2 and Step 1 notes in the sweep). After the queue empties, checks the
        accumulation buffer and sends compacted messages as a single
        consultation.

        Drains ITERATIVELY, one item per loop iteration (F1, judgment-day WU2).
        WU2's consume-at-event enqueues the next agenda turn INSIDE this turn's
        _hablar tail (before the per-item finally), so recursing through
        _complete_processing_cycle -> _process_priority_queue would nest 2 frames
        per consecutive ready boundary and eventually RecursionError the engine
        thread (permanent silence). The loop keeps the EXACT per-item cadence —
        idle callback + _drain_control_commands + _check_pending_model_switch run
        per item via _complete_processing_cycle(process_queue=False) — with a
        flat stack.
        """
        while True:
            if self._processing:
                return
            widened_pop_armed = self._speech_interrupt_enabled and self._speech_router_enabled
            speech_busy = self._speech_active
            if not widened_pop_armed and speech_busy:
                return

            expired_turns = _get_turn_scheduler(self).expire()
            expired_chat_infos: list = []
            for exp in expired_turns:
                prio, ts, payload, source = exp.item[:4]
                self._log(f"Item expirado y omitido (TTL {exp.ttl_seconds:.0f}s): {source}")
                if source == "direct" and self._interrupt_on_direct and len(exp.item) > 5:
                    self._release_arrival_cut("expired", token=exp.item[5])
                _log_turn_outcome(source, TurnEvent.EXPIRE)
                if source == "chat":
                    expired_chat_infos.append({"age_sec": exp.age_seconds, "ttl_sec": exp.ttl_seconds})
                self._clear_prefetch_if_matches(payload, source)

            if expired_chat_infos and self.on_chat_item_expired is not None:
                for info in expired_chat_infos:
                    try:
                        self.on_chat_item_expired(info)
                    except Exception:
                        pass

            queue_empty = False
            accumulated = None
            required_priority = None
            if widened_pop_armed and speech_busy:
                # PTT only, plus the direct tier while an ADR-069 arrival cut
                # holds the router: the cut speech is not coming back until
                # the reply plays, so the direct turn must start generating.
                required_priority = (0, 1) if (
                    self._interrupt_on_direct and self._arrival_cut_holding()
                ) else 0
            dequeue_res = _get_turn_scheduler(self).dequeue_or_else(
                on_empty=self._flush_accumulation,
                required_priority=required_priority,
            )
            if dequeue_res.blocked:
                self._retrigger_interactive_pregen_for_blocked_head()
                return
            if dequeue_res.empty:
                queue_empty = True
                accumulated = dequeue_res.empty_value
            else:
                item = dequeue_res.item
                priority, ts, payload, source, *rest = item
                history_text = rest[0] if rest else None
                submitted_at = rest[1] if len(rest) > 1 else None
                submitted_under_provider = rest[2] if len(rest) > 2 else None
                origin = rest[3] if len(rest) > 3 else None
                stamp = (
                    TurnStamp(
                        submitted_at=submitted_at, submitted_under_provider=submitted_under_provider, origin=origin
                    )
                    if submitted_at is not None else None
                )

            if queue_empty:
                if accumulated:
                    self._log(f"Procesando acumulación ({self._last_accumulation_flush_count} mensajes compactados)...")
                    with self._lock:
                        self._processing = True
                        self._current_processing_source = "accumulated"
                    self.ui_callback("processing")
                    turn_state.open_turn("accumulated")
                    try:
                        self._ejecutar_inferencia(accumulated, source="accumulated")
                    finally:
                        turn_state.close_turn()
                        self._complete_processing_cycle(process_queue=False)
                return

            if self._test_pop_boundary_hook is not None:
                self._test_pop_boundary_hook()
            cached = self._take_pregen_if_match(payload, source)
            already_reported_boundary = False
            if cached is None:
                cached = self._wait_or_invalidate_pregen(payload, source)
                already_reported_boundary = cached is not None
            source_label = "PTT" if source == "ptt" else source
            if cached is not None:
                self._log(f"Cola prioritaria: respuesta pregenerada [{source_label}] (prioridad {priority}).")
            else:
                self._log(f"Cola prioritaria: procesando [{source_label}] (prioridad {priority})...")
            with self._lock:
                self._processing = True
                self._current_processing_source = source
                # ADR-069: stamps of the owner turn(s) this cycle answers, so
                # the router can tell WHOSE reply is submitting.
                self._current_turn_tokens = frozenset(
                    t for t in (submitted_at,) if t is not None and source in _OWNER_QUESTION_SOURCES
                )
            if not self._speech_active:
                self.ui_callback("processing")
            self._note_detour_turn(source)
            turn_state.open_turn(source)
            try:
                if cached is not None:
                    if cached.get("failure_reason") == "reasoning_budget_exhausted":
                        self._log(
                            f"Pregen agotó presupuesto de razonamiento [{source_label}]; no se reintenta en vivo."
                        )
                        if source in ("ptt", "direct", OWNER_BUNDLE_SOURCE):
                            self.ui_callback("reasoning_budget_exhausted")
                        turn_state.emit(TurnEvent.DROP)
                    else:
                        if submitted_at is not None:
                            logger.info(
                                "[TURN_LATENCY] source=%s request_to_tts_total_ms=%d path=pregen %s%s",
                                source, int((time.monotonic() - submitted_at) * 1000),
                                turn_timing.stage_fields({}),
                                f" tid={turn_state.current_tid()}" if turn_state.current_tid() else "",
                            )
                        self._speak_pregenerated(cached, already_reported_boundary=already_reported_boundary)
                        turn_state.emit(TurnEvent.SERVED_PREGEN)
                else:
                    if not source.startswith("kira-agenda"):
                        self._log(
                            f"Pregen boundary: draft=none source={source} gap_ms=-1 "
                            f"gen_ms=-1 speech_ms={self._speech_ms_for_boundary()}"
                        )
                    taken: list = []
                    if source in _OWNER_QUESTION_SOURCES:
                        taken = self._take_owner_bundle_prefix(item)
                        if taken:
                            members = [item] + taken
                            with self._lock:
                                self._current_turn_tokens = frozenset(
                                    m[5] for m in members if len(m) > 5 and m[5] is not None
                                )
                            payload, history_text, stamp = self._compose_owner_bundle(members)
                            source = OWNER_BUNDLE_SOURCE
                            for member in taken:
                                self._clear_prefetch_if_matches(member[2], member[3])
                            self._log_owner_bundle(members, payload)

                    forward = {"stamp": stamp} if stamp is not None else {}
                    if taken:
                        forward["bundle_followers"] = taken
                    self._ejecutar_inferencia(
                        payload, source=source, history_text=history_text, **forward
                    )
            finally:
                # process_queue=False: the loop (not recursion) drains the next
                # ready item, keeping the stack flat (F1). The per-item idle
                # callback + control-command drain + model-switch check still run
                # here, once per item — the same cadence the old recursion had.
                turn_state.close_turn()
                self._complete_processing_cycle(process_queue=False)

    def _complete_processing_cycle(self, *, process_queue: bool = True, emit_idle: bool = True) -> None:
        with self._lock:
            self._processing = False
            self._current_processing_source = None
            finished_tokens = getattr(self, "_current_turn_tokens", frozenset())
            self._current_turn_tokens = frozenset()
        if self._interrupt_on_direct and finished_tokens:
            # ADR-069 safety net: a direct turn that ended without submitting
            # speech (error, blocked, empty reply) must not leave ITS arrival
            # hold engaged. Keyed by the turn's stamp: a PTT turn or an older
            # direct turn finishing never releases someone else's hold.
            for token in finished_tokens:
                self._release_arrival_cut("turn_end", token=token)
        self._owner_reflection_stamp_turn_done()
        self._stamp_command_done()  # opens the promotion quiet window (priority-queue turns too)
        if emit_idle and not self._speech_active:
            self.ui_callback("idle")
        self._drain_control_commands()
        # Unit 4.2 (runtime_findings_batch_20260731, D3b): see the method's own
        # docstring for the root cause this closes.
        self._drain_pending_direct_into_priority_queue()
        self._check_pending_model_switch()
        if process_queue:
            self._process_priority_queue()

    def _retrigger_interactive_pregen_for_blocked_head(self) -> None:
        """pregen-retrigger: re-evaluate the queue head the drain refused to pop.

        The interactive trigger used to fire only from `enqueue()` and from a
        pregen worker releasing the slot. A turn that arrived while a
        FOREGROUND generation ran was refused (`llm_generating` / armed
        `is_processing`) and never looked at again, so Kira then spoke for 20s+
        with an idle GPU and generated that answer serially at the boundary.

        Why here: the drain's `dequeue_or_else(...).blocked` return is exactly
        "armed, speech busy, head is not priority 0": the head must wait for the
        boundary, so a background draft is the only way to use the wait. It is
        reached after `_complete_processing_cycle` cleared `_processing` (the
        first instant every gate in `_maybe_trigger_interactive_pregen` can
        pass) and also after a `[BUNDLE_FAILED]` requeue. Hooking the
        `_llm_generating` clear instead would run while `_processing` is still
        set (refused by the armed gate) and inside the retry loop; hooking the
        cycle end would draft a priority-0 head the very next loop iteration
        pops, moving its generation off the foreground path for no gain.

        Safe by construction: same trigger, same gates (speaking, not
        generating, armed `is_processing`, bundling, slot occupancy,
        still-head), so it cannot start a second generation on the single
        runner; a head that already has a draft or a worker in flight is a
        no-op (idempotent), which also covers the repeated idle-tick drains.
        Never raises into the drain.
        """
        try:
            with self._pq_lock:
                head_snapshot = self._head_snapshot_locked()
            self._maybe_trigger_interactive_pregen(head_snapshot)
        except Exception:
            logger.exception("Interactive pregen re-trigger for blocked head failed")

    def _drain_control_commands(self) -> None:
        """Apply a contiguous run of whitelisted control commands from the
        FRONT of command_queue at a turn boundary.

        The engine is single-threaded: run() only reads command_queue when it is
        NOT inside a dispatch, so control commands posted during a continuous
        priority-queue run (the iterative _process_priority_queue drain loop) sit
        unread until the queue empties (the command-starvation bug). This boundary
        is a true idle point (_processing and _speaking both False), so a leading
        run of whitelisted commands is applied now, before the next turn dispatches.

        """
        if self._speech_active:
            return
        applied = 0
        for _ in range(len(self._deferred_control_commands)):
            tipo, payload = self._deferred_control_commands.popleft()
            self._dispatch_command(tipo, payload)
            applied += 1
        for _ in range(self.command_queue.qsize()):
            with self.command_queue.mutex:
                if not self.command_queue.queue:
                    break
                comando = self.command_queue.queue[0]
                if comando is None or comando[0] not in _DRAIN_SAFE_COMMANDS:
                    break
                self.command_queue.queue.popleft()
            tipo, payload, *_extra = comando
            self._dispatch_command(tipo, payload)
            applied += 1
        if applied > 0:
            self._log(f"Boundary drain: {applied} comando(s) de control aplicados.")
            self.ui_callback("commands_drained")

    def _drain_pending_direct_into_priority_queue(self) -> None:
        """Unit 4.2 (runtime_findings_batch_20260731, D3b): move a queued
        source=direct command from `command_queue` into `_priority_queue` at
        every turn boundary.
        """
        with self._direct_drain_lock:
            for _ in range(self.command_queue.qsize()):
                with self.command_queue.mutex:
                    if not self.command_queue.queue:
                        return
                    comando = self.command_queue.queue[0]
                    if (
                        comando is None
                        or comando[0] != "process_context"
                        or len(comando) < 4
                        or comando[3] not in _OWNER_QUESTION_SOURCES
                    ):
                        return
                    _tipo, payload, history_text, source, *rest = comando
                    submitted_at = rest[0] if rest else None
                    submitted_under_provider = rest[1] if len(rest) > 1 else None
                    origin = rest[2] if len(rest) > 2 else None
                    self.command_queue.queue.popleft()
                self.enqueue(
                    payload, priority=turn_priority.dispatch_priority_for_source(source),
                    source=source, history_text=history_text,
                    submitted_at=submitted_at, submitted_under_provider=submitted_under_provider,
                    **({"origin": origin} if origin is not None else {}),
                )
                self._log(f"Boundary drain: turno {source} movido a cola prioritaria (D3b).", level="debug")


    def _guardrail_fallback_line(self, source: str, reason: str = "") -> str:
        """Return a canned spoken line for guard-blocked responses.

        Agenda sources return "" — the agenda state machine already handles
        rejected outputs gracefully and a canned line would break topic flow.

        D4 (memoria_quality_20260717): the memory-flavored last bundle line is
        reserved for turns blocked by the R9 AI-self-ID rule — the only guard
        reason a plain in-character AI disclosure actually fits (S3). Every other
        block reason (and the reason-less chat-repetition path) round-robins over
        the FIRST FOUR generic lines only, so the memory line is never a
        non-sequitur for an unrelated block. Bundles that ship fewer than five
        lines round-robin over whatever they have (no reserved memory line).

        Lines come from the active locale bundle (i18n_active.guardrail_fallback_lines,
        es legacy default) — must stay neutral and never match an output_guard
        pattern themselves, or a block->fallback->block loop would result.
        """
        if source.startswith("kira-agenda"):
            return ""
        # S1: a crisis turn never falls back to a joke or a "tongue-tied" line.
        if is_crisis_turn():
            return i18n_active.crisis_reply()
        lines = i18n_active.guardrail_fallback_lines()
        # R9 self-ID block -> the memory-flavored line (bundle index 4), when the
        # bundle actually ships it. Does not advance the generic rotation.
        if "no_ai_self_identification" in reason and len(lines) >= 5:
            return lines[4]
        # no_code_output block -> its dedicated lines (bundle indexes 5-6), when
        # the bundle ships them; same rotation counter, separate pool.
        if "no_code_output" in reason and len(lines) >= 7:
            code_lines = lines[5:7]
            idx = getattr(self, "_guardrail_fallback_idx", 0)
            self._guardrail_fallback_idx = idx + 1
            return code_lines[idx % len(code_lines)]
        generic = lines[:4] if len(lines) >= 5 else lines
        idx = getattr(self, "_guardrail_fallback_idx", 0)
        self._guardrail_fallback_idx = idx + 1
        return generic[idx % len(generic)]

    def _retry_after_guard_block(
        self,
        *,
        messages: list,
        opciones_llm: dict,
        request_model: str,
        chat_timeout: float,
        provider_cfg: dict,
        is_local: bool,
        guard_reason: Optional[str] = None,
        think: Optional[bool] = None,
        prompt_profile: Optional[str] = None,
    ) -> str:
        """ONE extra generation after an output_guard block (owner decision
        "afinar + reintento", guardrail_tuning_20260724): same prompt + a
        rule-aware nudge appended to the LAST USER message's content.

        Returns the stripped content, or "" on any failure (transport error,
        timeout, empty response) so the caller falls through to the existing
        canned-line fallback unchanged.
        """
        nudge = _guardrail_retry_nudge_for_reason(guard_reason)
        retry_messages = _append_guardrail_nudge(messages, nudge)
        # K2: carry the turn's resolved think exactly like the main attempt
        # (inside options; `_lift_think` moves it to the top-level kwarg), on a
        # copy so the caller's options are never mutated.
        retry_options = dict(opciones_llm)
        if think is not None:
            retry_options["think"] = think
        with self._lock:
            self._llm_generating = True
        try:
            _trace = prompt_trace.begin(
                source="guardrail_retry",
                model=usage_ledger.call_model_name(
                    request_model, is_local=is_local, provider_cfg=provider_cfg
                ),
                is_local=is_local,
                messages=retry_messages,
                options=retry_options,
                path="buffered",
                # compact-prompt-profile-20261009: the retry resends the turn's
                # own messages/options, so it ran under the turn's profile.
                profile=prompt_profile,
            )
            try:
                respuesta = self._ollama_chat_with_watchdog(
                    timeout=chat_timeout,
                    model=request_model,
                    messages=retry_messages,
                    keep_alive=LLM_KEEP_ALIVE,
                    options=retry_options,
                    provider_cfg=provider_cfg,
                    is_local=is_local,
                )
            except BaseException as _trace_exc:
                if _trace is not None:
                    _trace.fail(_trace_exc)
                raise
            if _trace is not None:
                _trace.finish(respuesta)
            msg_obj = respuesta.get('message', {})
            if isinstance(msg_obj, dict):
                content = msg_obj.get('content', '')
            else:
                content = getattr(msg_obj, 'content', '')
            if is_local:
                # L1 (live-session-fixes-20261005): same think split as the
                # main local attempt -- reasoning never becomes the reply.
                content, _ = strip_think_blocks(content or "", "")
            return (content or "").strip().strip('\x00\ufeff')
        except Exception as retry_exc:
            turn_state.fail_reason("guard_retry_failed", retry_exc)
            logger.warning("Guardrail retry generation failed", exc_info=True)
            return ""
        finally:
            with self._lock:
                self._llm_generating = False

    def _generar_dialogo(self, contexto, source: str = "direct", **kwargs):
        """S1 (guardrails-vulnerable-users): mark the whole generation as a
        crisis turn when the OWNER's own words signal self-harm, then run the
        normal funnel. Owner turns only (direct, ptt, owner bundle; the remote
        gateway submits "direct"); chat and agenda text never mark a turn.
        The flag is read by output_guard, the stream guard and the fallback
        line, and is gone when the generation returns. Logs carry no text."""
        crisis = (
            source in _CRISIS_SOURCES
            and isinstance(contexto, str)
            and detect_crisis(contexto)
        )
        if crisis:
            logger.warning("[CRISIS_TURN] source=%s detected=yes", source)
        with crisis_turn(crisis):
            return self._generar_dialogo_core(contexto, source=source, **kwargs)

    def _generar_dialogo_core(
        self,
        contexto,
        source: str = "direct",
        *,
        intent: str = "chat",
        commit_history: bool = True,
        log_prefix: str = "LLM",
        history_text: Optional[str] = None,
        watchdog_timeout: Optional[float] = None,
        allow_vision: Optional[bool] = None,
        origin: Optional[str] = None,
    ) -> str:
        request_model = self.current_model
        if source.startswith("kira-agenda") and intent == "chat":
            intent = "agenda"
        provider_cfg = self._provider_config
        is_local = self._cfg_is_local(provider_cfg) or self._cloud_fallback_active
        self._log(f"Analizando contexto con {request_model}...")
        cloud_fallback_retry_done = False
        image_reject_retry_done = False
        # V1.33: a turn never inherits the previous turn's reading token.
        # V1.34: only a FOREGROUND turn (commit_history) clears it. A pregen or
        # connector generation (commit_history=False) can run between the
        # reading and the same turn's buffered reaction submit; clearing there
        # would let the reaction cut its own reading when the toggle is on.
        if commit_history:
            self._vision_read_turn_id = None
        try:
            # vision-on-demand V1.5: at most one look per turn, finished BEFORE
            # any generation attempt (buffered and streaming share this funnel)
            # and reused by the cloud->local retry below instead of looking twice.
            # V1.13: `allow_vision` (None = the old `commit_history` default) lets
            # interactive owner pregen look too; a pregen look is stashed so a
            # discarded draft regenerated for the same payload does not look twice.
            if allow_vision is None:
                allow_vision = commit_history
            # laya-decision-layer-20261009: one decide() per turn, before the
            # look, reused by the cloud->local retry below. None = base path:
            # no new kwarg reaches any seam.
            decision_kw = decision_kwargs(
                self._turn_decisions(contexto, source, allow_vision=allow_vision)
            )
            look = (
                self._run_vision_look(contexto, source, stash=not commit_history, **decision_kw)
                if allow_vision
                else NO_LOOK
            )
            screen_block, vision_note = look.block, look.note
            vision_read_note = look.read_note
            # P2: images for the cloud call: the direct screen look's frames, then
            # the owner's pasted images for this exact turn text (direct only).
            turn_images = list(look.images)
            if source == "direct":
                turn_images += self.peek_turn_images(contexto)
            # V1.15: a failed REQUESTED look never reaches the LLM; the canned
            # line is the reply (None = the shortcut failed, generate normally).
            if look.fail_line:
                fail_reply = self._vision_fail_reply(
                    look, contexto, source,
                    commit_history=commit_history, history_text=history_text,
                )
                if fail_reply is not None:
                    return fail_reply
            # web-fetch-laya-20261009: at most one web lookup per turn, after
            # the look and reused by the cloud->local retry. Switch off (the
            # default) = no kwarg reaches any seam.
            # runtime-fixes-20261110 T2: ``origin`` (desktop / remote) decides
            # whether a link in the message may be read; None is not desktop.
            web_look = self._run_web_step(
                contexto, source, speak=commit_history,
                **({"origin": origin} if origin is not None else {}), **decision_kw
            )
            web_kw = web_look.kwargs()
            while True:
                attempt_was_cloud = not is_local
                build_mark = turn_timing.mark(exclude="recall_ms")
                # P2: images only ride a cloud attempt. A local attempt (cloud fell
                # back) drops them and carries a fixed note instead of the claim
                # that an image is attached.
                attempt_block, attempt_note = screen_block, vision_note
                direct_kw: dict = {}
                if (turn_images and is_local) or image_reject_retry_done:  # B1 retry drops images too
                    if look.images:
                        attempt_block = ""
                    if not attempt_block:
                        attempt_note = direct_cloud.IMAGE_UNSEEN_NOTE
                elif look.images:
                    direct_kw["screen_direct"] = True
                setup = self._build_generation_request(
                    contexto,
                    source,
                    intent=intent,
                    is_local=is_local,
                    provider_cfg=provider_cfg,
                    request_model=request_model,
                    identity_model=(
                        request_model if is_local else configured_cloud_model(provider_cfg)
                    ),
                    watchdog_timeout=watchdog_timeout,
                    screen_block=attempt_block,
                    vision_note=attempt_note,
                    vision_read_note=vision_read_note,
                    **direct_kw,
                    **decision_kw,
                    **web_kw,
                )
                if turn_images and not is_local:
                    setup.cloud_images = tuple(turn_images)
                turn_timing.close(build_mark, "prompt_build_ms")
                if commit_history:
                    turn_state.note(
                        model=request_model if is_local else configured_cloud_model(provider_cfg)
                    )
                    turn_state.emit(
                        TurnEvent.PROMPT_BUILT,
                        ms=int((time.monotonic() - build_mark[0]) * 1000),
                    )
                outcome = self._cloud_attempt_loop(
                    setup,
                    source=source,
                    commit_history=commit_history,
                    is_local=is_local,
                    provider_cfg=provider_cfg,
                    request_model=request_model,
                    watchdog_timeout=watchdog_timeout,
                    contexto=contexto,
                    history_text=history_text,
                )
                if outcome.early_return is not None:
                    result = outcome.early_return
                else:
                    result = self._finalize_generation(
                        setup,
                        outcome,
                        contexto,
                        source=source,
                        commit_history=commit_history,
                        log_prefix=log_prefix,
                        history_text=history_text,
                        is_local=is_local,
                        provider_cfg=provider_cfg,
                        request_model=request_model,
                        streamed_job=(
                            outcome.stream.job if outcome.stream is not None else None
                        ),
                    )
                if (
                    getattr(outcome, "image_rejected", False)
                    and attempt_was_cloud
                    and not image_reject_retry_done
                    and not (outcome.stream is not None and outcome.stream.job is not None)
                ):
                    # B1: same turn, same cloud model, no images, fixed unseen
                    # note. Once only; never the cloud->local fallback.
                    image_reject_retry_done = True
                    turn_images = []
                    self._log(
                        f"cloud_image_rejected: retrying turn without images on the same "
                        f"cloud model (source={source}).",
                        level="warning",
                    )
                    continue
                if (
                    not result
                    and attempt_was_cloud
                    and not self._cloud_fallback_active
                    and provider_cfg.get("fallback_mode", "auto") == "manual"
                    and not (outcome.stream is not None and outcome.stream.job is not None)
                ):
                    manual_reply = None
                    if turn_images or screen_block:
                        # A2: manual mode keeps routing to a cloud that just
                        # failed; a look that carried images must not end in silence.
                        manual_reply = self._vision_cloud_failure_reply(
                            contexto, source, commit_history=commit_history,
                            history_text=history_text,
                        )
                    elif (
                        getattr(outcome, "cloud_class", None)
                        == cloud_llm_client.CLOUD_ERROR_QUOTA_EXHAUSTED
                        and source in ("direct", "ptt")
                    ):
                        # B2: out of credits: say so once per turn, owner turns only.
                        manual_reply = self._vision_cloud_failure_reply(
                            contexto, source, commit_history=commit_history,
                            history_text=history_text, outcome="cloud_quota_exhausted",
                        )
                    if manual_reply is not None:
                        return manual_reply
                if (
                    result
                    or cloud_fallback_retry_done
                    or not attempt_was_cloud
                    or not self._cloud_fallback_active
                    or self._cloud_fallback_reason != cloud_llm_client.CLOUD_ERROR_TRANSIENT
                    or (outcome.stream is not None and outcome.stream.job is not None)
                ):
                    failure_reason = getattr(outcome, "failure_reason", None)
                    answered_by_provider = (
                        "local" if is_local else (provider_cfg.get("active_provider") or "local")
                    )
                    answered_by_transport = "local" if is_local else "cloud"
                    return GenerationResult(
                        result,
                        failure_reason=failure_reason,
                        answered_by_provider=answered_by_provider,
                        answered_by_transport=answered_by_transport,
                        cloud_fallback_retry=cloud_fallback_retry_done,
                        web_searched=bool(getattr(setup, "web_searched", False)),
                        web_sources=_reply_sources(web_look, setup, source) if result else (),
                    )
                cloud_fallback_retry_done = True
                if commit_history:
                    turn_state.emit(TurnEvent.CLOUD_RETRY)
                is_local = True
                request_model = self._last_known_good_model or self.current_model
                self._log(
                    f"cloud_fallback_retry: retrying turn locally with "
                    f"{request_model} (source={source}).",
                    level="warning",
                )

        except Exception as e:
            # Class and site only: the message can quote the prompt or the reply.
            self._log(
                f"ERROR Ollama: {type(e).__name__} code=engine_error", level="error"
            )
            logger.error(
                "Error en inferencia LLM: %s site=%s", type(e).__name__, turn_state.exc_site(e)
            )
            if commit_history:
                turn_state.fail_reason("engine_error", e)
                turn_state.emit(TurnEvent.FAIL)
            _orphan = getattr(self, "_live_stream_state", None)
            if _orphan is not None and _orphan.job is not None and not _orphan.handled:
                try:
                    self._stream_partial_exit(
                        _orphan, reason=f"engine_error:{type(e).__name__}"
                    )
                except Exception:
                    logger.exception("stream orphan belt failed")
            if commit_history:
                self._invalidate_pregen_epoch()
            return GenerationResult("")
        finally:
            # V1.13: the reply is ready to play; a status line that never started is moot.
            self._retire_vision_status()
            # P2: the committed turn is done with its pasted images.
            if commit_history and source == "direct":
                self.take_turn_images(contexto)

    def _build_generation_request(
        self,
        contexto,
        source: str,
        *,
        intent: str = "chat",
        is_local: bool,
        provider_cfg: dict,
        request_model: str,
        identity_model: Optional[str] = None,
        watchdog_timeout: Optional[float],
        screen_block: str = "",
        vision_note: str = "",
        vision_read_note: str = "",
        screen_direct: bool = False,
        decisions=None,
        web_block: str = "",
        web_note: str = "",
        link_block: str = "",
        link_note: str = "",
    ) -> "_GenerationSetup":
        """Phase 1 of _generar_dialogo: build message list, sampling options, and timeout.
        Delegates pure assembly to PromptContextAssembler while taking minimal lock snapshots.
        ``decisions`` is the turn's TurnDecisions (laya-decision-layer-20261009);
        None is the base behavior. ``web_block``/``web_note`` carry this turn's
        web lookup (web-fetch-laya-20261009); "" is the base behavior.
        """
        with self._history_lock:
            history_snapshot = list(self.historial)
            if source in _DIGEST_INJECT_SOURCES:
                digest_block = self._memory_digest.build_block(
                    sanitize_fn=self._sanitize_history_context,
                    line_format=i18n_active.digest_line_format(),
                    unit_singular=i18n_active.digest_unit_singular(),
                    unit_plural=i18n_active.digest_unit_plural(),
                )
            else:
                digest_block = ""
            memorias_profile_id = (
                self._current_profile_id if source in _MEMORIA_INJECT_SOURCES else None
            )

        def _on_evicted(evicted_pairs, native_ctx, effective_ctx):
            self._log(
                f"ctx_budget_gate: evicted {len(evicted_pairs)} pair(s) from messages "
                f"(model={request_model}, native_ctx={native_ctx}, effective_ctx={effective_ctx})",
                level="warning",
            )
            with self._history_lock:
                for u_msg, a_msg in evicted_pairs:
                    u_content = u_msg.get("content", "") if isinstance(u_msg, dict) else ""
                    a_content = a_msg.get("content", "") if isinstance(a_msg, dict) else ""
                    if not u_content or u_content.startswith("[agenda segura"):
                        continue
                    from opencohost.core.engine.llm_engine_memorias import _digest_fragment_key
                    from opencohost.core.memory.memoria_store import strip_history_wrapper
                    clean_u = strip_history_wrapper(u_content)
                    turn_key = f"{clean_u}::{a_content}"
                    if turn_key not in self._digested_turn_keys:
                        ledger_line = self._build_ledger_line(clean_u, a_content)
                        self._memory_digest.append(
                            ledger_line, key=_digest_fragment_key(clean_u)
                        )
                        self._digested_turn_keys.add(turn_key)

        def _native_ctx_res(model_name: str) -> int:
            self._discover_model_ctx(model_name)
            return self._model_ctx_limit.get(model_name, CTX_FALLBACK_DEFAULT)

        assembler = getattr(self, "_prompt_assembler", None)
        if assembler is None:
            assembler = PromptContextAssembler(
                sanitize_history_fn=getattr(self, "_sanitize_history_context", None)
            )

        pers_enabled = getattr(
            sys.modules.get("opencohost.core.llm_engine"),
            "PERSONALIZATION_ENABLED",
            getattr(settings, "PERSONALIZATION_ENABLED", True),
        )
        mem_enabled = getattr(
            sys.modules.get("opencohost.core.llm_engine"),
            "MEMORIAS_ENABLED",
            getattr(settings, "MEMORIAS_ENABLED", True),
        )

        episodic_memory_block = ""
        mem = getattr(self, "_memory", None)
        # v4/v5 memory selection is deterministic and application-side — the
        # local LLM never decides whether memory applies (ADR-059 below).
        clean_contexto = contexto
        if source in ("ptt", "direct", settings.OWNER_BUNDLE_SOURCE):
            try:
                from opencohost.core.memory.memoria_store import strip_history_wrapper
                clean_contexto = strip_history_wrapper(contexto)
            except Exception:
                clean_contexto = contexto

        recall = None
        # T3: a short "that made no sense / what did you say" turn has no topic
        # of its own; recall against it only surfaces unrelated memory.
        inject_skip = (
            "correction_turn"
            if memorias_profile_id and is_clarification_turn(clean_contexto)
            else None
        )
        # laya-decision-layer-20261009: Laya may only skip a recall the rule runs.
        if memorias_profile_id and not inject_skip and not memory_recall_skipped(decisions):
            recall = self._run_episodic_recall(mem, clean_contexto, memorias_profile_id)
        raw_profile_locale = getattr(self, "_current_profile_locale", None)
        effective_lang = (
            i18n_coherence.primary_subtag(raw_profile_locale) if raw_profile_locale else None
        )
        if not effective_lang:
            try:
                effective_lang = i18n_coherence.primary_subtag(
                    i18n_active.get_active_bundle().code
                )
            except Exception:
                effective_lang = "es"
        v4_builder, recall_stats = self._memorias_builder_with_recall(
            recall, skip_reason=inject_skip, lang=effective_lang
        )
        self._bind_active_card_vector(clean_contexto, recall)
        profile = self._resolve_prompt_profile(request_model, is_local)
        session_topics_block = self._session_topics_block(
            source, recall, history_snapshot, query_text=clean_contexto,
            compact=bool(is_local) and profile.profile == _prompt_profile.PROFILE_COMPACT,
        )
        # The memory builder runs later (inside assemble): tell it this turn
        # already carries earlier-session context so it never claims "nothing".
        recall_stats["session_topics"] = bool(session_topics_block)
        reply_level = self._resolve_reply_level(
            source, clean_contexto, screen_block, **decision_kwargs(decisions)
        )
        clock_kw = self._clock_kwargs(source, clean_contexto, decisions)
        setup = assembler.assemble(
            contexto,
            source,
            system_prompt=self.system_prompt,
            use_system_role=self.use_system_role,
            is_local=is_local,
            provider_cfg=provider_cfg,
            request_model=request_model,
            identity_model=identity_model,
            history_snapshot=history_snapshot,
            digest_block=digest_block,
            memorias_profile_id=memorias_profile_id,
            memorias_builder=v4_builder,
            editorial_provider=getattr(self, "direct_editorial_context_provider", None),
            native_ctx_resolver=_native_ctx_res,
            effective_ctx_resolver=getattr(self, "_resolve_effective_ctx_limit", None),
            on_evicted_pairs=_on_evicted,
            is_reasoning_model=getattr(self, "_resolve_reasoning_classification", None),
            timeout_resolver=getattr(self, "_resolve_chat_watchdog_timeout", None),
            watchdog_timeout=watchdog_timeout,
            personalization_enabled=pers_enabled,
            memorias_enabled=mem_enabled,
            episodic_memory_block=episodic_memory_block,
            session_topics_block=session_topics_block,
            intent=intent,
            reasoning_settings_resolver=getattr(self, "_resolve_reasoning_settings", None),
            telemetry_tracker=getattr(self, "inference_telemetry", None),
            residency_ratio_resolver=getattr(self, "_resolve_model_residency_ratio", None),
            extended_responses=getattr(self, "_extended_responses", False),
            warmth=getattr(self, "_warmth", DEFAULT_WARMTH),
            owner_gender=getattr(self, "_owner_gender", DEFAULT_OWNER_GENDER),
            screen_block=screen_block,
            vision_note=vision_note,
            vision_enabled=self._vision_enabled(),
            vision_read_note=vision_read_note,
            **({"screen_direct": True} if screen_direct else {}),
            self_knowledge=getattr(self, "self_knowledge_service", None),
            active_card_context=getattr(self, "active_card_service", None),
            persona_custom=bool(getattr(self, "_persona_custom", False)),
            reply_level=reply_level,
            prompt_profile=profile.profile,
            web_block=web_block,
            web_note=web_note,
            link_block=link_block,
            link_note=link_note,
            lang=effective_lang,
            **clock_kw,
        )
        if recall is not None or inject_skip:
            self._log_recall_telemetry(recall, recall_stats)
        if decisions is not None:
            try:  # prompt-trace opt-in record: outcomes only, never new text
                setup.decisions = decisions.outcomes()
            except Exception:
                pass
        return setup

    def _resolve_prompt_profile(self, request_model: str, is_local: bool):
        """compact-prompt-profile-20261009: the prompt profile for this turn.

        The parameter size comes from the ``ollama.show`` response that
        ``_discover_model_ctx`` already fetches and caches for the context
        budget (the assembler calls it on every local turn anyway), so this
        adds no RPC. The size is cached per model name. Cloud turns skip the
        lookup. Logs one content-free ``[PROMPT_PROFILE]`` line. Never raises:
        any failure is the full profile.
        """
        try:
            size = None
            if is_local:
                cache = getattr(self, "_prompt_profile_sizes", None)
                if cache is None:
                    cache = {}
                    self._prompt_profile_sizes = cache
                if request_model in cache:
                    size = cache[request_model]
                else:
                    try:
                        self._discover_model_ctx(request_model)
                        show = (getattr(self, "_ctx_show_cache", None) or {}).get(request_model)
                        size = _prompt_profile.parameter_size_from_show(show)
                    except Exception:
                        size = None
                    cache[request_model] = size
            decision = _prompt_profile.resolve(
                request_model,
                is_local=is_local,
                override=getattr(settings, "PROMPT_PROFILE", "auto"),
                parameter_size=size,
                max_b=getattr(settings, "PROMPT_COMPACT_MAX_B", _prompt_profile.DEFAULT_COMPACT_MAX_B),
            )
        except Exception:
            decision = _prompt_profile.FULL_UNKNOWN
        try:
            # ADR-064: the layout this turn will use (compact and single-prompt
            # turns stay legacy), appended to the profile line.
            layout = _prompt_profile.resolve_layout(
                getattr(settings, "PROMPT_LAYOUT", _prompt_profile.LAYOUT_LEGACY),
                compact=bool(is_local) and decision.profile == _prompt_profile.PROFILE_COMPACT,
                use_system_role=bool(getattr(self, "use_system_role", True)),
            )
            logger.info(decision.log_line(layout=layout))
        except Exception:
            pass
        return decision

    def _resolve_reply_level(
        self, source: str, clean_text: str, screen_block: str, decisions=None
    ) -> Optional[str]:
        """reply-length-selector-20261009: the reply level for an owner turn,
        from the owner's setting (in memory) and the clean user text; None for
        every other source, which keeps their prompt unchanged. Logs one line
        with the mode, level and reason only, never the user text.
        laya-decision-layer-20261009: ``decisions`` may replace an auto pick."""
        if source not in OWNER_POLICY_SOURCES:
            return None
        try:
            mode = reply_length_setting.get_reply_length_mode()
            level, reason = resolve_reply_level(
                mode,
                clean_text,
                vision_turn=bool(screen_block) and source in SCREEN_INJECT_SOURCES,
                extended_responses=bool(getattr(self, "_extended_responses", False)),
            )
            if decisions is not None:
                level, reason = apply_reply_length(decisions, level, reason)
        except Exception:
            return None
        logger.info("[REPLY_LENGTH] mode=%s level=%s reason=%s", mode, level, reason)
        return level

    def _bind_active_card_vector(self, text: str, recall) -> None:
        """Hand the turn's recall query vector to the active-card service.

        Active-card retrieval reuses the MiniLM query vector the memory recall
        already computed for this turn (no second embed on the turn path). With
        no recall (viewer chat, agenda) nothing is bound and the service embeds
        the query itself, bounded. Never raises.
        """
        bind = getattr(getattr(self, "active_card_service", None), "bind_turn_vector", None)
        if bind is None or recall is None:
            return
        try:
            # Cold fact index: reuse the turn vector so cards never embed twice.
            vec = recall.fact_query_vec
            if vec is None:
                vec = recall.query_vec
            attempted = vec is not None or recall.fact_fallback in ("none", "timeout", "error")
            bind(text, vec, attempted=attempted)
        except Exception:
            pass

    def _cloud_attempt_loop(
        self,
        setup: "_GenerationSetup",
        *,
        source: str,
        commit_history: bool,
        is_local: bool,
        provider_cfg: dict,
        request_model: str,
        watchdog_timeout: Optional[float],
        contexto=None,
        history_text: Optional[str] = None,
    ) -> "_GenerationAttemptOutcome":
        orchestrator = getattr(self, "_generation_orchestrator", None)
        if orchestrator is None:
            orchestrator = GenerationOrchestrator(self)
            self._generation_orchestrator = orchestrator
        return orchestrator.execute_generation_attempt(
            setup,
            source=source,
            commit_history=commit_history,
            is_local=is_local,
            provider_cfg=provider_cfg,
            request_model=request_model,
            watchdog_timeout=watchdog_timeout,
            contexto=contexto,
            history_text=history_text,
        )

    def _run_streaming_attempt(
        self,
        setup: "_GenerationSetup",
        *,
        source: str,
        request_model: str,
        contexto,
        history_text: Optional[str],
        is_local: bool = True,
        provider_cfg: Optional[dict] = None,
    ) -> tuple:
        return self._generation_orchestrator.run_streaming_attempt(
            setup,
            source=source,
            request_model=request_model,
            contexto=contexto,
            history_text=history_text,
            is_local=is_local,
            provider_cfg=provider_cfg,
        )

    def _handle_stream_sentence(
        self, sentence: str, state: "_StreamAttemptState", setup: "_GenerationSetup"
    ) -> str:
        return self._generation_orchestrator.handle_stream_sentence(
            sentence, state, setup
        )

    def _stream_partial_exit(self, state: "_StreamAttemptState", *, reason: str) -> None:
        return self._generation_orchestrator.stream_partial_exit(state, reason=reason)

    _rebuild_stream_response = staticmethod(GenerationOrchestrator.rebuild_stream_response)

    def _apply_stream_guard_verdict(
        self, state: "_StreamAttemptState", dialogo: str, source: str
    ) -> str:
        return self._generation_orchestrator.apply_stream_guard_verdict(
            state, dialogo, source
        )

    def _apply_echo_guard(
        self,
        setup: "_GenerationSetup",
        dialogo: str,
        *,
        source: str,
        streamed: bool,
        messages: list,
        opciones_llm: dict,
        request_model: str,
        provider_cfg: dict,
        is_local: bool,
    ) -> str:
        """kira-personal-tone-20261007 P4: a reply near-identical to the
        previous assistant turn gets ONE retry with the `no_echo` nudge. A retry
        that is empty or blocked by the output guard keeps the original reply;
        a retry that echoes again is accepted -- never a loop.

        Streamed turns (`streamed=True`) already put their audio on air before
        any verdict, so they cannot be retried: the echo is only logged
        (`[ECHO_GUARD] observed`). Agenda sources are exempt (their state
        machine owns acceptance). Logs carry source and ratio, never text.
        """
        if not dialogo or source.startswith("kira-agenda"):
            return dialogo
        previous = _echo_guard.last_assistant_text(getattr(setup, "history_snapshot", None))
        echo, ratio = _echo_guard.is_echo(dialogo, previous)
        if not echo:
            return dialogo
        if streamed:
            logger.warning("[ECHO_GUARD] observed source=%s ratio=%.2f streamed=1", source, ratio)
            return dialogo
        logger.warning("[ECHO_GUARD] blocked source=%s ratio=%.2f", source, ratio)
        retry_content = self._retry_after_guard_block(
            messages=messages,
            opciones_llm=opciones_llm,
            request_model=request_model,
            chat_timeout=setup.chat_timeout,
            provider_cfg=provider_cfg,
            is_local=is_local,
            guard_reason=f"Non-negotiable violation [{_echo_guard.ECHO_RULE_ID}]: echo",
            think=getattr(setup, "think", None),
            prompt_profile=getattr(setup, "prompt_profile", None),
        )
        if retry_content:
            retry_allowed, _ = _output_guard_with_tts_check(retry_content, source=source)
            if retry_allowed:
                return retry_content
        return dialogo

    def _finalize_generation(
        self,
        setup: "_GenerationSetup",
        outcome: "_GenerationAttemptOutcome",
        contexto,
        *,
        source: str,
        commit_history: bool,
        log_prefix: str,
        history_text: Optional[str],
        is_local: bool,
        provider_cfg: dict,
        request_model: str,
        streamed_job: Optional[SpeechJob] = None,
    ) -> str:
        """Phase 3 of _generar_dialogo (refactor_core_api_20260802 B7):
        post-process the raw completion (ctx telemetry, MODEL_TRACE, agenda
        sanitize, clause sanitizer, guardrail + retry, chat repetition guard,
        agenda acceptance) and commit/return. The ctx-telemetry snapshot
        block keeps reading only THIS call's own locals/params (never a
        shared attribute) -- same documented design as before the split.

        `streamed_job` (llm_output_streaming_20260813 §5/§7): non-None only
        when this turn's audio already went out through the router's growing
        job. It gates exactly the streamed divergence points — clause
        sanitizer verdict-log-only, guard-trip truncation instead of
        retry/fallback, seal-on-success — and NOTHING else; a buffered turn
        (streamed_job=None) runs this method byte-identically to before.
        """
        evaluator = getattr(self, "_generation_evaluator", None)
        if evaluator is None:
            evaluator = GenerationEvaluator(
                output_guard_fn=output_guard,
                guardrail_fallback_fn=getattr(self, "_guardrail_fallback_line", None),
                detect_repetition_fn=lambda cand, rec, **kw: getattr(
                    sys.modules.get("opencohost.core.llm_engine"),
                    "detect_repetition",
                    detect_repetition,
                )(cand, rec, **kw),
            )

        eval_res = evaluator.evaluate(
            outcome.raw_content,
            outcome.respuesta,
            setup,
            source=source,
            is_local=is_local,
            provider_cfg=provider_cfg,
            request_model=request_model,
            desired_model=getattr(self, "_desired_model", request_model),
            current_model=getattr(self, "current_model", request_model),
            loaded_model=getattr(self, "_loaded_model", None),
            current_profile_name=getattr(self, "_current_profile_name", "default"),
            cfg_is_local_fn=getattr(self, "_cfg_is_local", None),
            agenda_output_sanitizer=getattr(self, "_sanitize_agenda_output", None),
            agenda_output_transformer=getattr(self, "agenda_output_transformer", None),
            clause_sanitizer_sources=getattr(
                sys.modules.get("opencohost.core.llm_engine"),
                "CLAUSE_SANITIZER_SOURCES",
                settings.CLAUSE_SANITIZER_SOURCES,
            ),
            sanitize_clause_fn=getattr(
                sys.modules.get("opencohost.core.llm_engine"),
                "sanitize_clause_repetition",
                sanitize_clause_repetition,
            ),
            is_streamed=(streamed_job is not None),
        )

        dialogo = eval_res.dialogo
        if dialogo:
            try:
                from opencohost.core.speech.tts_sanitizer import _tts_strip_speaker_prefix
                dialogo = _tts_strip_speaker_prefix(dialogo)
            except Exception:
                pass
        if dialogo and _web_sources.carries_web_context(setup):
            # runtime-fixes-20261110 T5: the app shows the real sources itself, so a
            # trailing "Fuente: ..." line the model wrote (it can be invented) is
            # removed before the reply is spoken, shown or committed.
            dialogo = self._drop_source_line(dialogo, source)
        elapsed = eval_res.elapsed
        self._pregen_last_gen_duration = elapsed

        # 1. Editorial usage recorder trigger
        if setup.editorial_block and dialogo and getattr(self, "direct_editorial_usage_recorder", None) is not None:
            try:
                self.direct_editorial_usage_recorder()
            except Exception:
                logger.warning("editorial direct usage recorder failed", exc_info=True)

        # 2. Context telemetry logging & ring append
        if eval_res.telemetry is not None:
            tel = eval_res.telemetry
            logger.info(
                "ctx_utilization: model=%s prompt_eval_count=%d native_ctx=%d effective_ctx=%d ratio=%.3f "
                "load_ms=%.0f prefill_ms=%.0f decode_ms=%.0f eval_count=%d source=%s",
                request_model, tel.pec, setup.native_ctx, setup.effective_ctx, tel.utilization,
                tel.load_ms, tel.prefill_ms, tel.decode_ms, tel.eval_count, source,
            )
            ring = getattr(self, "_ctx_telemetry_ring", None)
            if ring is not None:
                ring.append(tel.snapshot)
            # ADR-056 WU1: Record empirical inference decode telemetry (observational only)
            tracker = getattr(self, "_inference_telemetry", None)
            if tracker is not None and tel.eval_count > 0:
                eval_dur_ns = int(getattr(tel, "eval_duration_ns", 0) or (tel.decode_ms * 1e6))
                if eval_dur_ns > 0:
                    size_b = 0
                    vram_b = 0
                    model_digest = None
                    monitor = getattr(self, "health_monitor", None) or getattr(self, "_health_monitor", None)
                    snap = getattr(monitor, "residency_snapshot", None) if monitor else None
                    if snap is not None:
                        size_b = snap.size_bytes or 0
                        vram_b = snap.size_vram_bytes or 0
                        model_digest = snap.digest
                    else:
                        probe = getattr(monitor, "residency_probe", None) or getattr(monitor, "_ollama_residency", None) if monitor else None
                        if probe is not None:
                            res_mb = probe.resident_mb
                            v_mb = probe.vram_mb
                            if res_mb is not None:
                                size_b = int(res_mb * 1024 * 1024)
                            if v_mb is not None:
                                vram_b = int(v_mb * 1024 * 1024)
                            model_digest = probe.digest

                    if not model_digest:
                        resp_obj = getattr(outcome, "respuesta", None)
                        if resp_obj is not None:
                            resp_digest = (
                                resp_obj.get("digest") or resp_obj.get("model_digest")
                                if isinstance(resp_obj, dict)
                                else (getattr(resp_obj, "digest", None) or getattr(resp_obj, "model_digest", None))
                            )
                            model_digest = resp_digest or request_model
                    if not model_digest:
                        model_digest = request_model

                    try:
                        tracker.record_turn(
                            provider="local" if is_local else "cloud",
                            model_id=request_model,
                            model_digest=model_digest,
                            allocated_context=setup.effective_ctx,
                            eval_count=tel.eval_count,
                            eval_duration_ns=eval_dur_ns,
                            size_bytes=size_b,
                            size_vram_bytes=vram_b,
                        )
                    except Exception:
                        logger.debug("Inference telemetry recording skipped defensively", exc_info=True)
            if tel.pressure_high:
                logger.warning(
                    "ctx_pressure_high: utilization=%.1f%% model=%s source=%s",
                    tel.utilization * 100, request_model, source,
                )
                on_high = getattr(self, "on_ctx_pressure_high", None)
                if on_high is not None:
                    try:
                        on_high({
                            "ratio": tel.utilization,
                            "effective_ctx": setup.effective_ctx,
                            "native_ctx": setup.native_ctx,
                            "evicted_pairs": setup.ctx_evicted,
                        })
                    except Exception:
                        logger.exception("on_ctx_pressure_high callback failed")
                if getattr(self, "ui_callback", None) is not None:
                    self.ui_callback("ctx_pressure_high")

        # 3. Model trace & mismatch warning
        if eval_res.model_trace.is_mismatch:
            self._log(f"[MODEL_MISMATCH_WARNING] {eval_res.model_trace.trace_msg}", level="warning")
        else:
            logger.info(f"Motor: {eval_res.model_trace.trace_msg}")

        # runtime-fixes-20261110 T1: a streamed attempt reverted by a guard before any
        # audio. The carried rule id drives the buffered recovery below, so it never
        # depends on the text (an unterminated dump can be empty after trimming).
        reverted_rule = (
            getattr(outcome.stream, "reverted_rule", None)
            if streamed_job is None and outcome.stream is not None
            else None
        )

        # 4. Empty output handling
        if not dialogo and not reverted_rule:
            self._log(f"⚠️ {request_model} devolvió respuesta vacía ({elapsed:.2f}s).", level="warning")
            logger.warning(f"Empty LLM response. Raw repr: {repr(outcome.raw_content)}")
            if getattr(self, "_cold_warming_notified", False):
                self._cold_warming_notified = False
                if getattr(self, "ui_callback", None) is not None:
                    self.ui_callback("ready")
            if not is_local:
                if getattr(self, "ui_callback", None) is not None:
                    self.ui_callback("cloud_llm_error")
                self._handle_cloud_failure(
                    source, failure_class=cloud_llm_client.CLOUD_ERROR_TRANSIENT
                )
            if (
                streamed_job is not None
                and outcome.stream is not None
                and not outcome.stream.handled
            ):
                # tts-first-chunk-boost-20261015 C2: audio is already on air
                # (e.g. a soft first chunk) but the final text is empty (a
                # length cut with no terminator). Seal the job and commit the
                # spoken prefix, or the router waits on it forever.
                self._stream_partial_exit(outcome.stream, reason="empty_final_text")
            elif is_crisis_turn() and streamed_job is None:
                # S1: a crisis message is never answered with silence.
                crisis_line = self._guardrail_fallback_line(source, "")
                if crisis_line:
                    turn_timing.mark_outcome("fallback")
                    if commit_history:
                        self._commit_history(
                            contexto, crisis_line, source=source, history_text=history_text,
                        )
                    return crisis_line
            if commit_history:
                self._invalidate_pregen_epoch()
            return ""

        if is_local:
            self._mark_model_generation_success(request_model)

        # 5. Clause sanitizer handling
        if eval_res.clause_verdict is not None:
            san = eval_res.clause_verdict
            self._log_clause_sanitizer(
                san, source,
                stage="generate" if commit_history else "pregen_draft",
            )
            if san.verdict == "rejected" and source.startswith("kira-agenda"):
                self._log(
                    f"Salida descartada por repetición de cláusulas "
                    f"(removed={san.removed_fragments} distinct={san.distinct_looping}).",
                    level="warning",
                )
                if commit_history:
                    self._invalidate_pregen_epoch()
                return ""
            if streamed_job is None:
                dialogo = san.text

        # 6. Stream guard or output guard
        if streamed_job is not None:
            dialogo = self._apply_stream_guard_verdict(outcome.stream, dialogo, source)
            allowed = True
        else:
            allowed = eval_res.guard_allowed

        guard_reason = eval_res.guard_reason
        if reverted_rule:
            allowed = False
            guard_reason = (
                f"Non-negotiable violation [{reverted_rule}]: "
                "streamed attempt reverted before any audio"
            )

        if not allowed:
            self._log(f"Salida bloqueada por guardrail: {guard_reason}", level="warning")
            if commit_history:
                turn_state.emit(TurnEvent.GUARD_BLOCK)
                turn_state.emit(TurnEvent.GUARD_RETRY)
            retry_content = self._retry_after_guard_block(
                messages=setup.messages,
                opciones_llm=setup.opciones_llm,
                request_model=request_model,
                chat_timeout=setup.chat_timeout,
                provider_cfg=provider_cfg,
                is_local=is_local,
                guard_reason=guard_reason,
                think=getattr(setup, "think", None),
                prompt_profile=getattr(setup, "prompt_profile", None),
            )
            if retry_content and source.startswith("kira-agenda"):
                retry_content = self._sanitize_agenda_output(retry_content)
                transformer = getattr(self, "agenda_output_transformer", None)
                if transformer is not None:
                    try:
                        retry_content = transformer(retry_content)
                    except Exception as transform_exc:
                        turn_state.fail_reason("agenda_transform_failed", transform_exc)
                        logger.exception("Agenda output transformer failed (guardrail retry)")
            if retry_content:
                retry_allowed, _ = _output_guard_with_tts_check(retry_content, source=source)
                retry_claim = _search_claim.violated_rule(retry_content, setup) if retry_allowed else None
                if retry_claim is not None:
                    _search_claim.log_claim(
                        retry_claim, stage="guard_retry", source=source, chars=len(retry_content)
                    )
                    retry_allowed = False
                if retry_allowed and _prompt_echo.detector_for(setup).text_has_echo(retry_content):
                    _prompt_echo.log_prompt_echo(
                        stage="guard_retry", source=source, chars=len(retry_content)
                    )
                    retry_allowed = False
                retry_leak = _prompt_echo.leak_kind(retry_content, source) if retry_allowed else None
                if retry_leak is not None:
                    _prompt_echo.log_prompt_leak(
                        stage="guard_retry", source=source, kind=retry_leak, chars=len(retry_content)
                    )
                    retry_allowed = False
                if retry_allowed:
                    self._log("Guardrail retry: respuesta corregida aceptada tras un intento adicional.")
                    turn_timing.mark_outcome("retried")
                    dialogo = retry_content
                    if _web_sources.carries_web_context(setup):
                        dialogo = self._drop_source_line(dialogo, source)
                    allowed = True
            if not allowed and commit_history:
                turn_state.emit(TurnEvent.GUARD_BLOCK)

        if not allowed:
            fallback = self._guardrail_fallback_line(source, guard_reason)
            if fallback:
                turn_timing.mark_outcome("fallback")
                self._log("Guardrail fallback: usando línea neutral sin LLM.")
                if commit_history:
                    self._commit_history(
                        contexto, fallback, source=source, history_text=history_text,
                    )
                return fallback
            if commit_history:
                self._invalidate_pregen_epoch()
            return ""

        dialogo = self._apply_echo_guard(
            setup, dialogo, source=source, streamed=(streamed_job is not None),
            messages=setup.messages, opciones_llm=setup.opciones_llm,
            request_model=request_model, provider_cfg=provider_cfg, is_local=is_local,
        )

        self._last_llm_failure = None
        self._last_cloud_failure_class = None
        self._cloud_bad_key_notified = False
        self._cloud_quota_notified = False

        # 7. Chat repetition guard
        if source == "chat" and eval_res.repetition_verdict is not None and eval_res.repetition_verdict.is_repetitive:
            self._log(
                f"Repetición de chat bloqueada ({eval_res.repetition_verdict.reason}); "
                f"usando línea neutral sin LLM.",
                level="warning",
            )
            if commit_history:
                self._invalidate_pregen_epoch()
            repetition_line = self._guardrail_fallback_line(source) or ""
            if repetition_line:
                turn_timing.mark_outcome("fallback")
            return repetition_line

        # 8. Agenda acceptance
        if source.startswith("kira-agenda") and commit_history and not self._accept_agenda_output(dialogo):
            self._log(f"Agenda: salida rechazada ({self._format_agenda_rejection()}).", level="warning")
            self._invalidate_pregen_epoch()
            return ""

        # 9. History commit & UI queue
        if commit_history:
            if getattr(self, "log_queue", None) is not None:
                self.log_queue.put(f"\n🧠 [Kira]: {dialogo} ({elapsed:.2f}s)\n")
        preview = dialogo[:200] if _debug_enabled() else f"len={len(dialogo)}"
        logger.info(f"{log_prefix} response ({elapsed:.2f}s): {preview}")

        if commit_history:
            self._commit_history(contexto, dialogo, source=source, history_text=history_text)

        return dialogo

    @staticmethod
    def _drop_source_line(dialogo: str, source: str) -> str:
        """``dialogo`` without a trailing source attribution. Logs a char count only."""
        stripped = _web_sources.strip_trailing_source(dialogo)
        if stripped != dialogo:
            logger.info(
                "[SOURCE_LINE] removed source=%s chars=%d", source, len(dialogo) - len(stripped)
            )
        return stripped

    def ctx_telemetry_snapshot(self, sources: Optional[tuple] = None) -> dict:
        """Unit 2.3 (runtime_findings_batch_20260731 F10): read-only view of the
        per-request context telemetry ring for API/status consumers (unit 2.5).
        Thin delegate to CtxTelemetryRing.snapshot() (Phase C3, core/ctx_telemetry.py)
        -- see that method's docstring for the full "latest"/"ring" contract.

        getattr-guarded: several existing tests build a MotorVocalIA via
        `__new__` (bypassing __init__) and never set `_ctx_telemetry_ring`;
        this must still degrade to an empty snapshot instead of raising.
        """
        ring = getattr(self, "_ctx_telemetry_ring", None)
        if ring is None:
            return {"latest": None, "ring": []}
        return ring.snapshot(sources)

    def _ejecutar_inferencia(
        self,
        contexto,
        source: str = "direct",
        *,
        history_text: Optional[str] = None,
        stamp: Optional[TurnStamp] = None,
        bundle_followers: Optional[list] = None,
    ):
        request_start = time.monotonic()
        submitted_at = stamp.submitted_at if stamp is not None else None
        submitted_under_provider = stamp.submitted_under_provider if stamp is not None else None
        queue_wait_ms: Optional[int] = None
        if submitted_at is not None:
            queue_wait_ms = max(0, int((request_start - submitted_at) * 1000))
        self._streamed_turn_job = None
        self._streamed_turn_prefix = None
        origin = stamp.origin if stamp is not None else None
        with turn_timing.turn_clock() as stage_ms:
            dialogo = self._generar_dialogo(
                contexto, source=source, commit_history=True, history_text=history_text,
                **({"origin": origin} if origin is not None else {}),
            )
        streamed_job = self._streamed_turn_job
        self._streamed_turn_job = None
        streamed_prefix = self._streamed_turn_prefix
        self._streamed_turn_prefix = None
        failure_reason = getattr(dialogo, "failure_reason", None)
        answered_by_provider: Optional[str] = None
        answered_by_transport: Optional[str] = None
        provider_changed_while_queued: Optional[bool] = None
        if submitted_under_provider is not None:
            answered_by_provider = getattr(dialogo, "answered_by_provider", None)
            answered_by_transport = getattr(dialogo, "answered_by_transport", None)
            if answered_by_provider is None:
                _answer_state = self.provider_runtime_state()
                answered_by_provider = _answer_state["provider"]
                answered_by_transport = _answer_state["transport"]
            provider_changed_while_queued = submitted_under_provider != answered_by_provider
        # Every foreground turn ends with exactly one [TURN_LATENCY] line: ok,
        # retried or fallback when it produced a reply, dropped when it did not.
        engine_ms = int((time.monotonic() - request_start) * 1000)
        turn_path = "stream" if streamed_job is not None else "buffered"
        if dialogo:
            self._log_turn_latency(
                source, engine_ms, queue_wait_ms, stage_ms, turn_path,
                turn_timing.outcome_of(stage_ms),
            )
            turn_state.emit(TurnEvent.COMPLETE, ms=engine_ms)
            if queue_wait_ms is not None and source == "direct" and queue_wait_ms > DIRECT_ANSWER_MAX_WAIT_SECONDS * 1000:
                logger.warning(
                    "[DIRECT_WAIT_EXCEEDED] source=direct queue_wait_ms=%d bound_ms=%d",
                    queue_wait_ms, int(DIRECT_ANSWER_MAX_WAIT_SECONDS * 1000),
                )
            feed_turn_id = self._emit_dialogue(
                dialogo, source, queue_wait_ms=queue_wait_ms,
                answered_by_provider=answered_by_provider,
                answered_by_transport=answered_by_transport,
                submitted_under_provider=submitted_under_provider,
                provider_changed_while_queued=provider_changed_while_queued,
                web_searched=bool(getattr(dialogo, "web_searched", False)),
                web_sources=getattr(dialogo, "web_sources", ()),
            )

            if streamed_job is None:
                if getattr(dialogo, "cloud_fallback_retry", False):
                    self._maybe_discard_stale_fallback_notice(source)
                if feed_turn_id is not None:
                    self._speak_or_submit(dialogo, source=source, feed_turn_id=feed_turn_id)
                else:
                    self._speak_or_submit(dialogo, source=source)
        else:
            self._log_turn_latency(source, engine_ms, queue_wait_ms, stage_ms, turn_path, "dropped")
            turn_state.emit(TurnEvent.DROP, ms=engine_ms)
            if streamed_prefix:
                self._emit_dialogue(
                    streamed_prefix, source,
                    web_searched=bool(getattr(dialogo, "web_searched", False)),
                )
            if source.startswith("kira-agenda"):
                self._accept_agenda_output("")
            elif bundle_followers:
                self._requeue_owner_bundle_followers(bundle_followers)
                if failure_reason == "reasoning_budget_exhausted" and not streamed_prefix:
                    self.ui_callback("reasoning_budget_exhausted")
                elif not streamed_prefix:
                    self.ui_callback("turn_dropped")
            elif source in ("ptt", "direct", OWNER_BUNDLE_SOURCE) and not streamed_prefix:
                if failure_reason == "reasoning_budget_exhausted":
                    self.ui_callback("reasoning_budget_exhausted")
                else:
                    self.ui_callback("turn_dropped")


    def _log_turn_latency(
        self,
        source: str,
        engine_ms: int,
        queue_wait_ms: Optional[int],
        stage_ms: dict,
        turn_path: str,
        outcome: str,
    ) -> None:
        """The one ``[TURN_LATENCY]`` line of a foreground turn. Numbers and enum
        codes only: no reply text is logged on any exit. ``tid=`` (when a
        foreground turn is open) joins it to the other per-turn lines."""
        tid = turn_state.current_tid()
        tid_part = f" tid={tid}" if tid else ""
        turn_state.record_latency(
            engine_ms=engine_ms, queue_wait_ms=queue_wait_ms, stage_ms=stage_ms,
            path=turn_path, outcome=outcome,
        )
        if queue_wait_ms is not None:
            logger.info(
                "[TURN_LATENCY] source=%s request_to_tts_ms=%d queue_wait_ms=%d request_to_tts_total_ms=%d %s path=%s outcome=%s%s",
                source, engine_ms, queue_wait_ms, queue_wait_ms + engine_ms,
                turn_timing.stage_fields(stage_ms), turn_path, outcome, tid_part,
            )
        else:
            logger.info(
                "[TURN_LATENCY] source=%s request_to_tts_ms=%d %s path=%s outcome=%s%s",
                source, engine_ms, turn_timing.stage_fields(stage_ms), turn_path, outcome, tid_part,
            )

    def _emit_dialogue(
        self,
        text: str,
        source: str,
        queue_wait_ms: Optional[int] = None,
        answered_by_provider: Optional[str] = None,
        answered_by_transport: Optional[str] = None,
        submitted_under_provider: Optional[str] = None,
        provider_changed_while_queued: Optional[bool] = None,
        web_searched: bool = False,
        web_sources: tuple = (),
    ) -> Optional[int]:
        """P3 producer sink: forwards Kira's own generated reply text.

        Opt-in (dialogue_callback defaults to None). A raising callback must
        never break the turn, so it's guarded the same way ui_callback sites
        are — logged, swallowed, never re-raised.

        This branch IS the dialogue_callback contract (C1, TurnStamp): the
        caller passes every kwarg unconditionally, and the VALUE gates below
        (queue_wait_ms, then submitted_under_provider) reduce the invocation
        to exactly one of three shapes — 2 positional; + queue_wait_ms
        (Unit 4.1, runtime_findings_batch_20260731 F5); + the four
        provider-disclosure kwargs together (Unit 4.2, F12 closure) — so
        every existing caller/test pinned on an exact shape (e.g.
        ChatReplySink.record's 2-positional assert) stays byte-identical.

        Returns the feed turn id the sink recorded (a real int), else None, so
        the caller can tag the speech job it submits for the same reply.
        """
        if self.dialogue_callback is None:
            return None
        # web_searched is this turn's own value (never instance state, so pregen
        # daemon threads cannot steal or clear a foreground turn's flag). The
        # kwarg is only sent when True so every existing callback shape holds.
        web_kw = {"web_searched": True} if web_searched else {}
        if web_sources:
            # runtime-fixes-20261110 T5: only the desktop reply sink gets them, and
            # only when a lookup produced some (an exact-shape callback is unchanged).
            web_kw["web_sources"] = _web_sources.as_dicts(web_sources)
        try:
            if queue_wait_ms is not None:
                if submitted_under_provider is not None:
                    recorded = self.dialogue_callback(
                        text, source, queue_wait_ms=queue_wait_ms,
                        answered_by_provider=answered_by_provider,
                        answered_by_transport=answered_by_transport,
                        submitted_under_provider=submitted_under_provider,
                        provider_changed_while_queued=provider_changed_while_queued,
                        **web_kw,
                    )
                else:
                    recorded = self.dialogue_callback(
                        text, source, queue_wait_ms=queue_wait_ms, **web_kw
                    )
            else:
                recorded = self.dialogue_callback(text, source, **web_kw)
        except Exception as callback_exc:
            turn_state.fail_reason("dialogue_callback_failed", callback_exc)
            logger.exception("dialogue_callback failed")
            return None
        if isinstance(recorded, int) and not isinstance(recorded, bool):
            return recorded
        return None

    def _emit_stream_chunk(self, state: Any) -> None:
        """Forward in-flight streaming chunks to ChatReplySink if supported."""
        if self.dialogue_callback is None:
            return
        stream_sink = getattr(self.dialogue_callback, "__self__", None)
        recorder = getattr(stream_sink, "record_stream_chunk", None)
        if callable(recorder):
            job_id = getattr(getattr(state, "job", None), "job_id", id(state))
            stream_id = f"stream-job-{job_id}"
            display_text = getattr(state, "display_prefix", lambda: "")()
            if not display_text:
                display_text = getattr(state, "spoken_prefix", lambda: "")()
            if not display_text and hasattr(state, "raw_sentences") and state.raw_sentences:
                display_text = " ".join(state.raw_sentences).strip()
            if not display_text and hasattr(state, "appended_sentences"):
                display_text = " ".join(state.appended_sentences).strip()
            chunks = list(getattr(getattr(state, "job", None), "chunks", []))
            try:
                recorded_turn_id = recorder(
                    stream_id=stream_id,
                    text=display_text,
                    fragments=chunks,
                    source=getattr(state, "source", "direct"),
                    web_searched=bool(getattr(state, "web_searched", False)),
                )
                if state.job is not None and getattr(state.job, "turn_id", None) is None and recorded_turn_id:
                    state.job.turn_id = int(recorded_turn_id)
                if state.job is not None and recorded_turn_id:
                    state.job.feed_turn_id = int(recorded_turn_id)
            except Exception:
                logger.exception("record_stream_chunk failed")

    def _active_piper_voice_lang(self):
        """Language family of the active Piper voice (registry first, then the catalog's)."""
        return PIPER_VOICES.get(self._piper_voice_key, {}).get("lang") or getattr(self, "_piper_catalog_lang", None)

    def _piper_startup_fallback(self) -> None:
        """The saved voice did not load at startup: use the locale default for this run only.

        The saved choice stays on disk untouched, so it comes back once the file is fixed.
        """
        fallback = self._piper_startup_default
        if not fallback:
            return
        key, path = fallback
        self._piper_startup_default = None
        logger.warning("Saved Piper voice failed to load; using the locale default for this run")
        try:
            ok = bool(self._piper.reload(path, speaker_id=None))
        except Exception as exc:
            logger.warning("Piper default voice reload failed: %s", type(exc).__name__)
            ok = False
        # Success or not, the saved voice is not loaded: report the locale default, never the
        # unloaded saved voice (config then shows "piper unavailable" for the default's file).
        self._piper_voice_key = key
        self._piper_catalog_lang = None
        if not ok:
            try:
                self._piper.set_speaker_id(None)
            except Exception:
                pass

    def _piper_switch_failed(self, requested, reason: str) -> None:
        """Keep the old voice, persist nothing, and tell the UI (event + reason code)."""
        self._last_piper_switch_failure = {"requested": str(requested)[:256], "reason": reason}
        logger.warning("Piper voice switch rejected (reason=%s)", reason)
        self._log("No se pudo cambiar la voz de Kira", level="warning")
        self.ui_callback("piper_voice_switch_failed")

    def _switch_piper_voice(self, payload) -> None:
        """``set_piper_voice``: a legacy registry key OR a catalog voice id, plus an optional speaker.

        ``payload`` is the voice string, or ``{"value": voice, "speaker_id": n}``. The path is
        resolved only through the registry or the confined catalog. On ANY failure the old voice
        and speaker stay active, nothing is persisted, and ``piper_voice_switch_failed`` is emitted
        with the reason held in ``_last_piper_switch_failure`` (surfaced by GET /api/tts/config).
        """
        speaker_id = None
        if isinstance(payload, dict):
            voice = payload.get("value")
            speaker_id = payload.get("speaker_id")
        else:
            voice = payload
        voice = voice if isinstance(voice, str) else ""
        if speaker_id is not None and (
            isinstance(speaker_id, bool) or not isinstance(speaker_id, int)
            or speaker_id < 0 or speaker_id > PIPER_SPEAKER_ID_MAX
        ):
            self._piper_switch_failed(voice, "invalid_speaker_id")
            return
        try:
            path, lang = piper_catalog.resolve_voice(voice)
        except piper_catalog.PiperVoiceError as exc:
            self._piper_switch_failed(voice, exc.code)
            return
        try:
            reload_ok = bool(self._piper.reload(path, speaker_id=speaker_id))
        except Exception as exc:
            logger.warning("Piper voice reload failed: %s", type(exc).__name__)
            self._piper_switch_failed(voice, "load_failed")
            return
        if not reload_ok:
            code = getattr(self._piper, "last_error", None)
            self._piper_switch_failed(voice, code if isinstance(code, str) and code else "load_failed")
            return
        active_lang = i18n_coherence.primary_subtag(i18n_active.get_active_bundle().code)
        if not save_piper_voice_for_locale(active_lang, voice, speaker_id):
            logger.warning("Piper voice applied but could not be saved")
        self._piper_voice_key = voice
        self._piper_catalog_lang = lang
        self._piper_locale_mismatch_notified = False
        self._last_piper_switch_failure = None
        label = PIPER_VOICES.get(voice, {}).get("label", voice)
        self._log(f"Voz de Kira cambiada a: {label}")
        logger.info("Piper voice switched (speaker=%s)", speaker_id)
        self.ui_callback("piper_voice_switch_applied")

    def effective_edge_voice(self) -> str:
        """Edge voice for the next sentence: the user's per-locale override, else the bundle voice."""
        override = getattr(self, "_edge_voice_override", None)
        return override if override else i18n_active.edge_voice()

    def _edge_switch_failed(self, requested, reason: str) -> None:
        """Keep the old Edge voice, persist nothing, and tell the UI (event + reason code)."""
        self._last_edge_switch_failure = {"requested": str(requested)[:80], "reason": reason}
        logger.warning("Edge voice switch rejected (reason=%s)", reason)
        self._log("No se pudo cambiar la voz de Edge de Kira", level="warning")
        self.ui_callback("edge_voice_switch_failed")

    def _switch_edge_voice(self, payload) -> None:
        """``set_edge_voice``: payload is the Edge ShortName (e.g. ``es-MX-DaliaNeural``).

        Format-checked, then confirmed against the CACHED catalog only (no network on this thread):
        a voice absent from a live/stale cache is rejected; no cache accepts a well-formed name.
        Applies to the next sentence and persists for the active locale only. On any failure the
        old voice stays, nothing is persisted, and ``edge_voice_switch_failed`` is emitted (reason
        in ``_last_edge_switch_failure``, surfaced as edge_voice_failure_reason by /api/tts/config).
        """
        if not is_valid_edge_voice(payload):
            self._edge_switch_failed(payload if isinstance(payload, str) else "", "invalid_edge_voice")
            return
        known = edge_voices.DEFAULT_CATALOG.peek()
        if known is not None and payload not in known:
            self._edge_switch_failed(payload, "edge_voice_unknown")
            return
        active_lang = i18n_coherence.primary_subtag(i18n_active.get_active_bundle().code)
        if not save_edge_voice_for_locale(active_lang, payload):
            self._edge_switch_failed(payload, "config_write_failed")
            return
        self._edge_voice_override = payload
        self._last_edge_switch_failure = None
        voice_lang = i18n_coherence.primary_subtag(payload)
        if voice_lang and active_lang and voice_lang != active_lang:
            logger.warning("Edge voice language '%s' differs from the active locale '%s'", voice_lang, active_lang)
        self._log(f"Voz Edge de Kira cambiada a: {payload}")
        logger.info("Edge voice switched")
        self.ui_callback("edge_voice_switch_applied")

    def _log(self, msg, level="info"):
        prefix = "[IA]"
        self.log_queue.put(f"{prefix} {msg}")
        getattr(logger, level)(f"Motor: {msg}")
