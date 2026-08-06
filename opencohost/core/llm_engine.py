import os
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
from dataclasses import dataclass
from typing import Callable, Optional

from opencohost.config.settings import (
    DEFAULT_MODEL, SYSTEM_PROMPT, HISTORY_MAX_TURNS, LLM_TEMPERATURE,
    LLM_TOP_P, LLM_MAX_TOKENS, LLM_KEEP_ALIVE, TEMP_DIR, TTS_SERVER_URL,
    TTS_HEAVY_TIMEOUT, TTS_LIGHT_TIMEOUT,
    OLLAMA_CHAT_TIMEOUT, OLLAMA_REQUEST_TIMEOUT,
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
    MEMORIAS_PROFILE_CAP,
    MEMORIAS_SUMMARY_MIN_TITLES,
    PERSONALIZATION_ENABLED,
    PTT_CUE_ENABLED, PTT_CUE_VOLUME,
    RETRY_MIN_REMAINING_SECONDS,
    CUT_ZONE_EARLY, CUT_ZONE_LATE, CUT_THRESHOLD_SECONDS, RETURN_MAX_DETOUR_TURNS,
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
)
from opencohost.core.context import context_budget
from opencohost.core.providers.cloud import cloud_llm_client
from opencohost.core.profiles import personalization
from opencohost.core.scheduling.turn_stamp import TurnStamp
from opencohost.core.speech.tts_sanitizer import _first_sentence, _sanitize_tts_text_for_playback
from opencohost.core.speech.router import (
    SPEECH_BOUNDARY_COMMAND,
    SpeechRouter,
    priority_for_source,
)
from opencohost.core.context.ctx_telemetry import CtxTelemetryRing
from opencohost.config.llm_provider import load_provider_config
from opencohost.stream_admin.oauth_store import OAuthStore
from opencohost.i18n import active as i18n_active
from opencohost.i18n import coherence as i18n_coherence
from opencohost.core.speech.backends.tts_piper import PiperEngine
from opencohost.core.providers.llm_tiers import LLMTierConfig, LLMTierState, LLM_TIER_LABELS
from opencohost.core.memory.memory_digest import MemoryDigest
from opencohost.core.memory.memoria_store import (
    MemoriaStore, derive_stable_key, build_title, build_signature,
    build_injection_lines, build_recency_lines, is_meta_recall_query,
    pinned_injection_counter, significant_token_count, strip_history_wrapper,
)
from opencohost.core.context.repetition_guard import (
    detect_repetition,
    sanitize_clause_repetition,
    DEFAULT_CONFIG as REPETITION_CONFIG,
)
from opencohost.config.logger import get_logger, _debug_enabled
from opencohost.config.validation import output_guard

logger = get_logger()

# The producer can legitimately spend the full heavy TTS HTTP timeout before
# enqueuing a Qwen chunk. Keep the consumer bounded, but do not give up sooner
# than the producer's configured request timeout.
TTS_AUDIO_QUEUE_TIMEOUT = max(TTS_HEAVY_TIMEOUT, TTS_LIGHT_TIMEOUT) + 15

# interruptible_speech_architecture_20260804 §5.1 — WHY "owner-bundle" joins
# all five frozensets below while "accumulated" still cannot. The two look
# alike (both merge several messages into one turn) and are opposites on the
# only axis that matters here: _flush_accumulation bundles VERBATIM VIEWER CHAT
# (i18n LEGACY_ACCUMULATION_CHAT), whereas an owner bundle is composed ONLY
# from queue items whose source is in _OWNER_QUESTION_SOURCES — the prefix scan
# in _take_owner_bundle_prefix stops at the first non-owner item, so viewer text
# is structurally incapable of reaching an owner-bundle payload, and therefore
# of reaching persistence through one. That containment is why the owner ruled
# against simply reusing the "accumulated" tag; it is pinned by the T3.4 privacy
# test in tests/test_owner_question_bundling.py. Leaving the tag OUT would be
# the real defect: the host's own burst would be answered with no profile, no
# memorias, no digest and no cue-card context — Kira blanked precisely on the
# turns where the owner asked the most.
#
# D1 — eviction source-gating (privacy_prereq_fixes_20260701). Only these
# origins may be promoted into the MemoryDigest when their history pair is
# evicted. "accumulated" must NEVER be added here: _flush_accumulation
# (see below, ~line 700) bundles verbatim viewer chat text into it, so
# capturing it would leak raw chat into the digest. "chat" is excluded for
# the same reason. Missing/unknown source values are fail-closed (not
# captured) — see the eviction gate in _commit_history.
_DIGEST_CAPTURE_SOURCES = frozenset({"direct", "ptt", OWNER_BUNDLE_SOURCE})

# kira_personalization_onboarding_20260705 — sources that qualify for the
# <perfil_streamer> injection. Deliberately its OWN gate (not nested inside
# `source == "direct"` below): ptt gains this block too — a deliberate,
# test-covered behavioral change (design §2). The digest/editorial enrichments
# were direct-only when this landed; F1 moved them onto the same direct+ptt
# footing (see _DIGEST_INJECT_SOURCES / _EDITORIAL_INJECT_SOURCES below).
# owner-bundle IS a host turn, so <perfil_streamer> applies (§5.1).
_PERSONALIZATION_INJECT_SOURCES = frozenset({"direct", "ptt", OWNER_BUNDLE_SOURCE})

# memoria_rag_followups_20260716 candidate 1 — sources that qualify for the
# <memorias_guardadas> injection. Widened from direct-only to direct+ptt
# (same precedent as _PERSONALIZATION_INJECT_SOURCES above): voice turns now
# recall stored memorias too, under the SAME shared 700-char budget. This
# gate has THREE sites: the profile-id snapshot, the injection call, and the
# prompt prepend — all must use this frozenset or the ptt path stays dead.
# Same three-site warning applies to owner-bundle (§5.1): the test asserts an
# injected memoria reaches the BUILT prompt, not merely frozenset membership.
_MEMORIA_INJECT_SOURCES = frozenset({"direct", "ptt", OWNER_BUNDLE_SOURCE})

# F1 (turn provenance follow-up) — sources that qualify for the L1
# <memoria_de_fondo> digest block and for the ARMED editorial cue-card block.
# These were exact-literal `source == "direct"` compares, which was harmless
# only while the dispatcher hardcoded source="direct" for EVERY process_context
# turn. The moment it started threading the real source, both enrichments went
# voice-blind: an F10 PTT question about an armed card got none of the card's
# facts (defeating the point of arming it), and a PTT "what were we talking
# about a while ago" got no digest at all — while the SAME question typed still
# got both. ptt is already in _DIGEST_CAPTURE_SOURCES, so voice turns FEED the
# digest; they must be able to READ it. Same precedent as the two frozensets
# above. NOT widened beyond direct+ptt+owner-bundle: chat/accumulated/agenda
# stay excluded (the digest may hold host-turn text and the card is
# host-editorial context). owner-bundle joins both because "¿de qué hablábamos
# hace rato?" and a question about an armed cue card can each be one of the N
# a burst merges (§5.1) — the bundle must not answer them memory-blind.
_DIGEST_INJECT_SOURCES = frozenset({"direct", "ptt", OWNER_BUNDLE_SOURCE})
_EDITORIAL_INJECT_SOURCES = frozenset({"direct", "ptt", OWNER_BUNDLE_SOURCE})

# interruptible_speech_architecture_20260804 §3.1 — the single definition of
# "a question the owner asked" (typed OR spoken), used by the drain guard
# below (Step 1) and by the overflow/bundling logic that follows in later
# steps of the same track. Declared here (Step 1) because the drain guard
# needs it now; Step 2/3 reuse this same frozenset rather than redeclaring it.
_OWNER_QUESTION_SOURCES = frozenset({"direct", "ptt"})

# W2a (memoria_recall_20260718): max captured titles retained per session for
# the mechanical session summary. Bounds the RAM held between summaries; the
# summary keeps only the newest this-many when a long session overflows it.
_SESSION_MEMORIA_TITLES_CAP = 40

# W2a (memoria_recall_20260718): the close-flush session summary is ONE more
# bounded disk write (~MemoriaStore.WRITE_TIMEOUT_SECONDS = 1.0s). Attempt it
# only when at least this much of the flush budget remains, so the durable-tier
# write can never push app close past the budget (R4: flush never blocks close).
_MEMORIA_SUMMARY_WRITE_BUDGET_SECONDS = 1.1

# guardrail_tuning_20260724 (owner decision "afinar + reintento"): appended as
# a trailing system message for the ONE extra generation attempt after an
# output_guard block. Never spoken/sent to TTS itself -- only the model sees
# it. Spanish, in-character-neutral (a correction to the model, not a Kira
# line): no promises, no guarantees, no absolute certainty about outcomes.
_GUARDRAIL_RETRY_NUDGE = (
    "Nota interna: tu respuesta anterior prometía, garantizaba o daba por "
    "seguro un resultado, premio o acción para la audiencia. Respondé de "
    "nuevo sin prometer ni garantizar nada y sin afirmar con certeza "
    "absoluta lo que va a pasar; mantené el resto del tono."
)


# ── memoria draft promotion (memory_promotion_20260725) ─────────────────────
# Per-turn capture is deliberately cheap, permissive and LLM-free; nothing ever
# filtered afterward, so the store filled with vague half-memories Kira then
# recited ("hubo una corrección… algo técnico, no me acuerdo del detalle"). This
# is the missing step: ONE LLM call per launch judges the oldest unjudged
# drafts, rewrites the survivors so they stand alone, and promotes them.

_PROMOTION_DRAFT_BATCH = 40          # ~12,000 chars — the volume owner decision 1 approved
_PROMOTION_NUM_PREDICT = 1200        # omitted entirely on reasoning models (D2)
_PROMOTION_TEXT_MAX_CHARS = 220

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
# can never be poisoned by an invented label.
_PROMOTION_JUDGE_REASONS = frozenset({
    "vague", "speculative", "trivial", "transient", "not_attributable",
})

# NOT an i18n slot, deliberately: this string never reaches a user, it instructs
# its own output language inline, and a slot would cost two manifest edits plus
# churn in the i18n contract tests for zero user-visible benefit.
# `{draft_block}` is substituted with str.replace, NOT str.format — the JSON
# example below is full of literal braces, and doubling every one of them is a
# silent-corruption trap for the next editor.
_PROMOTION_JUDGE_PROMPT = """You are a memory archivist for a streaming co-host. You do NOT talk to anyone.
You only decide which of the numbered exchanges below are worth remembering
permanently, and rewrite each keeper as ONE standalone sentence.

KEEP an item only if ALL SIX hold:
1. EXPLICIT — the operator stated it. Anything only the assistant speculated,
   guessed, joked about or inferred is not a fact. Reject it.
2. SELF-CONTAINED — your rewrite must be fully understandable a month from now
   by someone who never saw this conversation. No "this", "that", "the game",
   "the question", "the bug", "the fix", no unnamed pronouns, no unresolved
   references. If you cannot name the actual subject from the text you were
   given, REJECT. A vague rewrite is WORSE than no memory at all.
3. SPECIFIC — a concrete fact, preference, name, decision or number. Not a
   mood, not a greeting, not small talk.
4. REUSABLE — useful in a DIFFERENT future conversation, not only as a recap
   of this one.
5. DURABLE — still true next month. Reject anything about the current moment.
6. ATTRIBUTABLE — it is clear whose fact it is.

NAMES: never invent, normalise, translate or "correct" a proper noun. Game
titles, model names, tools and people usually appear only ONCE — that is
normal and is NOT a reason to reject. Copy the operator's own spelling
verbatim. If you are not confident you transcribed a name correctly, still
KEEP the item and set "uncertain": true.

Write each rewrite in the SAME LANGUAGE the operator used, in at most 30 words.

CANDIDATES:
{draft_block}

Reply with JSON only, no prose, no markdown fence:
{"decisions":[{"i":1,"keep":true,"text":"<standalone sentence>"},
              {"i":2,"keep":true,"text":"<sentence>","uncertain":true},
              {"i":3,"keep":false,"reason":"vague"}]}

Field contract:
  i          int, 1..N, exactly one object per candidate number
  keep       bool, required
  text       string, required when keep is true, <=220 characters
  uncertain  bool, optional, only meaningful when keep is true
  reason     string, only when keep is false; one of:
             vague, speculative, trivial, transient, not_attributable

If unsure whether to keep an item, use keep:false."""

_PROMOTION_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)


def _parse_promotion_decisions(text, batch_len: int) -> list[tuple[int, str | None, bool, str]]:
    """Parse the judge's reply into applied decisions. PURE — no I/O, no engine.

    Returns ``(index, text_or_None, uncertain, reason)`` per usable entry:
    a keep is ``(i, sentence, uncertain, "")``; a reject is ``(i, None, False,
    reason)``. NEVER raises: every unusable shape (empty, prose, a truncated
    reasoning-model reply, a fence full of apologies) collapses to ``[]``, which
    is what makes the sweep's fail-silent contract real — nothing applied means
    nothing marked judged, so the next launch retries.

    A ``keep`` whose text is missing, blank or over the char cap is demoted to a
    reject with reason ``not_self_contained`` rather than treated as an error: a
    judge that cannot produce a standalone sentence has answered criterion 2 in
    the negative.
    """
    if not text or not isinstance(text, str):
        return []
    stripped = _PROMOTION_FENCE_RE.sub("", text.strip())
    try:
        payload = json.loads(stripped)
    except Exception:
        return []
    if not isinstance(payload, dict):
        return []
    entries = payload.get("decisions")
    if not isinstance(entries, list):
        return []

    results: list[tuple[int, str | None, bool, str]] = []
    seen: set[int] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        index = entry.get("i")
        # bool is an int subclass — True would otherwise pass as index 1.
        if not isinstance(index, int) or isinstance(index, bool):
            continue
        if not 1 <= index <= batch_len or index in seen:
            continue
        keep = entry.get("keep")
        if not isinstance(keep, bool):
            continue
        seen.add(index)
        if not keep:
            reason = entry.get("reason")
            # isinstance FIRST: a list/dict `reason` is unhashable, and a bare
            # `in frozenset(...)` would raise TypeError straight through the
            # "NEVER raises" contract into the sweep's outer catch-all —
            # discarding every OTHER valid decision in the same batch.
            results.append((
                index, None, False,
                reason if isinstance(reason, str) and reason in _PROMOTION_JUDGE_REASONS
                else "unspecified",
            ))
            continue
        judged = entry.get("text")
        judged = " ".join(judged.split()) if isinstance(judged, str) else ""
        if not judged or len(judged) > _PROMOTION_TEXT_MAX_CHARS:
            results.append((index, None, False, "not_self_contained"))
            continue
        results.append((index, judged, entry.get("uncertain") is True, ""))
    return results


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

# §11 B4 primary path: verbs `_dispatch_command` parks while speech is
# active. `switch_model` is excluded because its branch carries its OWN
# mid-speech gate (`_pending_model_switch`: dedupes repeats, cancels on a
# switch-back) — a blind FIFO defer would apply every queued reload in
# sequence at the boundary instead of collapsing to the latest.
_SPEECH_DEFER_COMMANDS = _DRAIN_SAFE_COMMANDS - {"switch_model"}


# refactor_core_api_20260802 B7 (Phase C4): _generar_dialogo's in-place phase
# split. These two carriers exist ONLY to kill parameter sprawl between the
# three phase methods -- they are not a data model, hold no state of their
# own, and are never touched outside _generar_dialogo's own call chain.
@dataclass
class _GenerationSetup:
    """_build_generation_request's output: everything the retry loop and/or
    the finalize phase need that used to just be a local in the one big
    method body. `history_snapshot` and `editorial_block` are the two
    surprises here -- both are built during setup but read again all the way
    down in _finalize_generation (chat-repetition window and the editorial
    usage-recorder trigger, respectively), so they ride in this bundle
    instead of being recomputed or promoted to an instance attribute.
    """
    messages: list
    opciones_llm: dict
    chat_timeout: float
    max_intentos: int
    start_llm: float
    native_ctx: int
    effective_ctx: int
    ctx_evicted: int
    editorial_block: str
    history_snapshot: list


@dataclass
class _GenerationAttemptOutcome:
    """_cloud_attempt_loop's result. `early_return` mirrors the original
    method's own control flow: every early `return` inside the retry loop
    (watchdog timeout, transport failure exhausting the retry budget)
    returned exactly `""`, so `is not None` on this field is the one check
    the orchestrator needs to reproduce that identically -- it is NOT a
    truthiness check, since `""` itself is a valid (falsy) early-return
    value that must still short-circuit finalize.
    """
    raw_content: str = ""
    respuesta: object = None
    early_return: Optional[str] = None


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
        # §11 B4 (primary path): drain-safe verbs the run() loop consumed
        # while the router was speaking, parked for the next true boundary.
        # Engine-thread only (dispatch and drain both run there).
        self._deferred_control_commands: deque = deque()
        self._reasoning_model_cache: dict[str, bool] = {}
        self._model_ctx_limit: dict[str, int] = {}
        # multi_provider_llm_20260723 Phase 3: the persisted provider config
        # drives `_is_local` gating. Loaded once here (defaults to local-only
        # when absent/corrupt); PUT /api/llm/provider live-swaps it via
        # `set_provider_config` (attribute swap under `_lock`, next-call effective).
        self._provider_config: dict = load_provider_config()
        # Phase 4 (multi_provider_llm_20260723): runtime-only cloud->local
        # fallback flag (design 'Fallback switch semantics'). NEVER persisted
        # and NEVER mutates `self._provider_config['active_provider']` — a
        # restart (fresh __init__, defaults False) or an operator
        # `set_provider_config` call (also resets it) returns to the owner's
        # chosen provider. Read by `_cfg_is_local` so a posture snapshot taken
        # AFTER `_handle_cloud_failure` engages resolves to local.
        self._cloud_fallback_active: bool = False
        # F2 (runtime_findings_batch_20260731 unit 1.1): the class the last
        # CLOUD transport failure was sorted into (bad_key/rate_limited/
        # ambiguous_429/transient), so 2.1/2.2 can act on a reason instead of
        # re-deriving one. Cleared on the next successful generation, same as
        # `_last_llm_failure`. Never set on a LOCAL failure.
        self._last_cloud_failure_class: Optional[str] = None
        # Unit 2.1: "check your key" banner latch for `bad_key` -- one
        # ui_callback per failure EVENT, not per retried/repeated turn.
        # Resets alongside `_last_cloud_failure_class` on the next success.
        self._cloud_bad_key_notified: bool = False
        # Unit 2.2 (runtime_findings_batch_20260731 F3/F12): WHY the current
        # fallback engaged -- one of the 1.1 classes, or None outside
        # fallback. Distinct from `_last_cloud_failure_class` above: that one
        # resets on every successful generation (including LOCAL ones during
        # an active fallback), so it cannot describe "why we are still in
        # fallback" -- only this field, cleared solely by a successful
        # auto-return probe or an explicit provider PUT, can. All of
        # `_cloud_fallback_reason`/`provider_epoch`/`_cloud_probe_next_at`/
        # `_cloud_prober_stop` are guarded by `self._lock` -- reused rather
        # than a new lock, matching how `is_speaking`/`is_processing`/
        # `_llm_generating`/the provider-config swap in `set_provider_config`
        # already use it for exactly this class of tiny attribute
        # get/set-only critical sections (no network call is ever made while
        # holding it).
        self._cloud_fallback_reason: Optional[str] = None
        # Bumped on every provider TRANSITION (cloud<->local, in either
        # direction) -- fallback engaging, an auto-return, or a manual PUT
        # that changes active_provider. Purely observational (exposed via
        # provider_runtime_state()); the F12 draft-safety contract itself is
        # met by unconditional invalidation at each transition (see
        # `_handle_cloud_failure`/`_on_cloud_probe_success`/
        # `set_provider_config`), not by tagging stash entries with this
        # number -- simpler, and sufficient per the plan.
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
        # Unit 2.2 fix (runtime_findings_batch_20260731 findings 1/2/4/6):
        # bumped every time a prober is stopped or a new one is started.
        # Every prober captures its OWN generation at creation and every
        # state-mutating write it makes re-checks it under `_lock` first --
        # a superseded prober (stopped, replaced by a new failure class, or
        # raced by a concurrent stop/start) becomes a harmless zombie: its
        # join may still time out and let it keep running, but none of its
        # writes can ever take effect once stale.
        self._cloud_prober_generation: int = 0
        # Optional numeric-only payload hook, mirroring on_ctx_pressure_high:
        # fires with {"seconds": <float>} whenever a background probe is
        # (re)scheduled, so the UI can render a countdown without polling.
        self.on_cloud_probe_scheduled: Optional[Callable[[dict], None]] = None

        self.voz_referencia = None
        self.is_ready = False
        self._processing = False
        self._speaking = False
        # WU3 (design-fase2.md §2.3): narrow "Ollama is busy right now" flag,
        # set/cleared tightly around the actual generation call inside
        # _generar_dialogo (foreground AND pregen). The interactive pregen
        # trigger reads it as the GPU-free predicate — distinct from _processing
        # (which brackets the whole turn, generation + TTS playback).
        self._llm_generating = False
        self._current_speech_source: Optional[str] = None
        # WU4 4a (design-fase2.md §3): monotonic timestamp of the last
        # speaking_end, and the live consumer-loop progress of the CURRENT
        # utterance (None while not speaking). Both guarded by self._lock.
        # gap_ms telemetry derives from the former; speech_remaining_estimate
        # from the latter — the REAL _hablar_impl loop counters, not a timer.
        self._last_speaking_end_monotonic: Optional[float] = None
        self._speech_progress: Optional[dict] = None
        # T1 [v5]: speaking_start->end monotonic pair for the `speech_ms=`
        # field of a "Pregen boundary:" line — the PREVIOUS turn's own speech
        # duration (-1/None while unknown, e.g. the first turn of a session).
        # Guarded by self._lock, same as the fields above.
        self._speaking_start_monotonic: Optional[float] = None
        self._last_speech_duration_ms: Optional[int] = None
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
        # memory_promotion_20260725: same shape as the scout client, but built
        # per sweep with the adaptive judge budget (_judge_timeout_seconds).
        self._ollama_judge_client = None
        # One-shot latch for the STARTUP promotion sweep (owner decision 7): the
        # first time run()'s idle branch is reached the process is guaranteed
        # idle and the stream has not begun. NOT a recurring tick — the
        # unvalidated "30 consecutive idle seconds" threshold is deliberately
        # absent, not deferred.
        self._promotion_swept: bool = False
        # Last gate name reported by _promotion_gate, so a permanently inert
        # sweep logs once per CHANGED state instead of once per idle tick.
        self._promotion_last_gate: str = ""
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
        # W2a (memoria_recall_20260718): titles of this session's captured
        # memorias, appended in _capture_memoria on success and snapshotted+
        # cleared under _history_lock at profile switch (set_profile) / close
        # (flush_memorias) to synthesize ONE mechanical session summary. A
        # summary row is written via MemoriaStore.insert_summary, NOT through
        # _capture_memoria, so it is never fed back here → never re-summarized.
        # Capped at _SESSION_MEMORIA_TITLES_CAP (newest wins) to bound RAM.
        self._session_memoria_titles: list[str] = []
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

        # Lazily-built, cached PTT listening blip as an in-memory WAV file
        # (immutable bytes, reinit-proof). None until the first play_ptt_cue()
        # builds it from a pure-stdlib sine; see play_ptt_cue / _ptt_cue_wav.
        self._ptt_cue_wav_bytes = None

        # Priority queue: (priority, timestamp, payload, source)
        # priority: 0 = PTT/streamer (highest), 1 = chat (normal), 2 = agenda (lowest)
        self._priority_queue: list = []
        self._pq_lock = threading.Lock()
        self._pq_max_items: int = 5
        self._pq_ttl_seconds: float = 30.0  # non-PTT items expire after this delay
        # Step 1 (direct_turn_preemption_20260803): serializes
        # _drain_pending_direct_into_priority_queue, which is now called from the
        # HTTP thread (api/routers/chat.py) as well as from the engine boundary.
        # The drain's pop (under command_queue.mutex) and its enqueue (under
        # _pq_lock) are two separate critical sections; without this lock two
        # concurrent drains can pop directs A then B and enqueue them B then A,
        # inverting the FIFO order of two questions typed seconds apart
        # (enqueue sorts by insertion time). Held across the whole drain — the
        # only acquirer, so it can never participate in a lock cycle.
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
        # WU3 (design-fase2.md §2.3 [v2] slot eviction / §3 AC3.2): the in-flight
        # pregen request identity (payload/source/priority) while a worker runs
        # but has not stored yet — lets pregenerate() compare priorities for
        # eviction and lets the pop-side wait-or-fallback recognise a worker
        # generating THIS exact item. Cleared implicitly by the thread dying.
        self._pregen_inflight: Optional[dict] = None
        # WU4 4c (design-fase2.md §3): one retry-on-reject per pregen spawn.
        # Reset at the top of pregenerate(); the worker sets it True the
        # moment it actually retries so a second reject never retries again.
        self._pregen_retried: bool = False
        # T2(a) [v5]: seconds of the last COMPLETED generation (foreground or
        # pregen), set in _generar_dialogo right where elapsed is known. Feeds
        # the adaptive retry gate (1.2x this value); None on a cold start
        # (falls back to RETRY_MIN_REMAINING_SECONDS). Simple float — no lock
        # (atomic assignment under the GIL, single-Ollama-runner invariant
        # means at most one writer at a time).
        self._pregen_last_gen_duration: Optional[float] = None

        # WU5 (agenda_no_dead_air fase 2, design-fase2.md §3 WU5, D1/D2/D3):
        # interruption + connector-based return. All guarded by _prefetch_lock.
        # _frozen_stash holds the NEXT-turn agenda draft moved OUT of the active
        # _prefetched_agenda slot at a PTT cut, so it survives the interactive
        # detour (exempt from the _commit_history epoch bump and interactive-pregen
        # eviction, which only touch _prefetched_agenda) and the answer's own
        # pregen can use the freed slot. {payload, dialogo, source, priority,
        # gen_ms, connector}. _detour_turns counts interactive turns spoken
        # since the cut (D2 skip when it exceeds RETURN_MAX_DETOUR_TURNS).
        # _connector_last_idx rotates the D3 floor pool without immediate repeats.
        self._frozen_stash: Optional[dict] = None
        self._detour_turns: int = 0
        # R1 deadline backstop: monotonic timestamp of the last freeze, so the
        # driver can bound how long a return may HOLD when the interruption answer
        # never reaches a _note_detour_turn site. None when no stash is frozen.
        self._frozen_stash_at: Optional[float] = None
        self._connector_last_idx: Optional[int] = None
        # D1 host flag (default OFF): the position-aware PTT cut is API-host
        # product behavior. EngineHost sets this True; the CTK app never
        # constructs EngineHost, so CTK keeps its unchanged no-cut behavior.
        self._ptt_position_cut_enabled: bool = False
        # Speech-router host flag (interruptible_speech_architecture_20260804
        # §8 step 2), same pattern as the D1 flag above and the same reason:
        # EngineHost turns it ON, CTK never does. OFF is the kill switch —
        # `_speak_or_submit` falls back to a direct, blocking `_hablar`.
        self._speech_router_enabled: bool = False
        # Built (and its daemon thread started) on the FIRST routed submit.
        self._speech_router = None
        # Step-3 kill switch (interruptible_speech_architecture_20260804 §8
        # step 3), same pattern as `_speech_router_enabled` above: EngineHost
        # turns it ON, CTK never does. OFF reproduces step 2 exactly — no
        # pause/resume, no preemption. Read by `pause_speech_for_ptt` /
        # `resume_speech_after_ptt` (no-op when off) and copied onto the
        # router as `interrupt_enabled` at build time.
        self._speech_interrupt_enabled: bool = False

        # WU2b belt lock (agenda_no_dead_air fase 2, design-fase2.md §2.5):
        # serializes _hablar so two callers can never share the TTS/audio
        # pipeline. After WU2 the engine worker is the ONLY _hablar caller in the
        # API host, so this never contends there (a contention log = a bypass
        # regression). In the CTK legacy path (play_prefetched_agenda's speaker
        # thread) it serializes that thread against the worker — the lock WORKING.
        self._hablar_lock = threading.Lock()

        # Test-only seam (agenda_no_dead_air fase 2, design-fase2.md §3 WU1):
        # fires at the pop->processing boundary in _process_priority_queue,
        # after the item is popped (pq_lock released) and before _processing
        # is set True. None/no-op in production; lets a test hold that window
        # open to pin the speech-overlap race deterministically.
        self._test_pop_boundary_hook: Optional[Callable[[], None]] = None
        # Test-only seam (design-fase2.md §3 WU4-T5 [v5]): fires on the
        # pregen worker thread right after it stores a draft, before its
        # `finally` clears the in-flight marker — lets a test pin the real
        # store-to-finally race window where a successor spawn can take the
        # slot. None/no-op in production.
        self._test_store_to_finally_hook: Optional[Callable[[], None]] = None
        self.agenda_output_validator = None
        self.agenda_output_preview_validator = None
        self.agenda_output_recorder = None
        self.agenda_output_transformer = None
        self.agenda_controller = None               # Phase 0: metrics access
        self.direct_editorial_context_provider = None  # set externally by app_shell
        # Direct-turn editorial USED trigger (D2): fired once after a successful
        # direct generation that injected a card block. Wired to
        # bridge.commit_direct_injection by the host; stays None otherwise.
        self.direct_editorial_usage_recorder = None

        # Optional chat-activation telemetry seams (measure-first, off-by-default).
        # app_shell sets these ONLY when chat diagnostics are enabled; in production
        # they stay None so the guards below skip and behavior is byte-identical.
        # They fire from THIS worker thread into the aggregator's collector, which
        # locks its mutation path while enabled. RECORD-ONLY — neither changes queue
        # lifetime or speech behavior. Gated strictly on source == "chat".
        self.on_chat_item_expired = None   # (info: dict) — a chat queue item expired (TTL)
        self.on_chat_turn_spoken = None    # () — a chat turn finished speaking

        # WU4 4b (design-fase2.md §3): optional operator-visibility hook, fired
        # on the PREGEN WORKER THREAD with the rejection CODE only (never
        # dialogue text) when the preview guardrail rejects a background draft.
        # None = zero behavior change (mirrors the on_chat_* callbacks above).
        self.on_guardrail_rejected: Optional[Callable[[str], None]] = None

        # Unit 2.3 (runtime_findings_batch_20260731 F10): bounded per-request
        # context telemetry ring, appended once per completed generation whose
        # ctx_utilization line actually logs (see _generar_dialogo). NEVER a
        # single self.last_* scalar: pregenerate's background worker
        # (~:1773) calls _generar_dialogo concurrently with the foreground
        # turn, so a scalar would be silently clobbered mid-turn by a draft
        # nobody has spoken yet. Each entry is built entirely from one call's
        # own locals and deque.append is atomic under the GIL, so no extra
        # lock is needed for the append itself.
        self._ctx_telemetry_ring = CtxTelemetryRing(maxlen=CTX_TELEMETRY_RING_MAXLEN)
        # Optional numeric-only payload hook for ctx_pressure_high, mirroring
        # on_guardrail_rejected above. NOT a second positional argument on
        # ui_callback: CTK's concrete callback (app_shell.py's
        # _on_motor_event(self, status: str)) takes exactly one positional
        # argument with no *args/default, so calling self.ui_callback(status,
        # payload) would raise TypeError there. payload is passed as a plain
        # call argument (never shared instance state), so two threads racing
        # here (foreground + a pregen worker) cannot clobber each other.
        self.on_ctx_pressure_high: Optional[Callable[[dict], None]] = None

    @property
    def is_speaking(self):
        """Public speech predicate — `_speech_active` since step 2.

        Converting this ONE property carries every API reader with it
        (agenda_driver :300/:595, routers/status, ptt_session, routers/chat,
        shared), which is why none of them changes a line.
        """
        return self._speech_active

    @property
    def _speech_active(self) -> bool:
        """ACTIVE ∨ INCOMING ∨ STACK, plus the raw `_speaking` flag (design
        §11 B2).

        The INCOMING clause is the load-bearing one: without it the
        submit->pick window is open ON THE SUBMITTING THREAD — submit returns,
        `_complete_processing_cycle` runs, `_process_priority_queue` tests its
        gate against False and pops a second item mid-speech.

        `_speaking` stays in the predicate so the legacy direct-`_hablar` path
        (kill switch OFF, and CTK, which never arms the router) keeps exactly
        today's semantics. It is never NARROWER than before, only wider.
        """
        with self._lock:
            speaking = self._speaking
        if speaking:
            return True
        router = self._speech_router
        # Read outside `_lock`: `_sched_lock` is a leaf and must never be
        # taken under another engine lock (design §4 I10).
        return router is not None and router.has_work()

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

    def provider_runtime_state(self) -> dict:
        """Live provider/transport truth (F4, runtime_findings_batch_20260731 1.3).

        Read-only snapshot of the EFFECTIVE posture — the same
        `_cfg_is_local(...) or _cloud_fallback_active` posture `_is_local`
        computes — so status/telemetry can never again drift from what a live
        generation would actually use. Deliberately never reads
        `llm_provider.json` from disk: that disk read is exactly what let
        `_display_model` (opencohost/api/main.py) keep reporting the stale
        cloud model name through an active `_cloud_fallback_active` fallback.

        Returns a dict with:
        - `provider`: "local" when the effective transport is local (whether
          by config or by fallback); otherwise the persisted cloud provider id.
        - `transport`: "local" | "cloud".
        - `fallback_active`: the raw `_cloud_fallback_active` flag.
        - `fallback_reason` (unit 2.2): the 1.1 class the fallback engaged
          for, or None outside fallback.
        - `provider_epoch` (unit 2.2): the running count of provider
          transitions this process has made.
        - `next_cloud_probe_in_seconds` (unit 2.2): seconds until the next
          background return probe, or None when no probe is scheduled
          (no active fallback, or a non-probeable class).
        - `generation_model`: the model a request dispatched right now would
          actually use — `current_model` locally, the active cloud profile's
          `model` on cloud (None if unset/missing, same graceful degradation
          `_display_model` already had).
        """
        cfg = self._provider_config
        with self._lock:
            fallback_active = self._cloud_fallback_active
            fallback_reason = self._cloud_fallback_reason
            provider_epoch = self.provider_epoch
            probe_next_at = self._cloud_probe_next_at
        next_probe_in = None if probe_next_at is None else max(0.0, probe_next_at - time.monotonic())
        is_local = self._cfg_is_local(cfg) or fallback_active
        if is_local:
            return {
                "provider": "local",
                "transport": "local",
                "fallback_active": fallback_active,
                "fallback_reason": fallback_reason,
                "provider_epoch": provider_epoch,
                "next_cloud_probe_in_seconds": next_probe_in,
                "generation_model": self.current_model,
            }
        profile = self._cfg_active_profile(cfg) or {}
        return {
            "provider": cfg.get("active_provider") or "local",
            "transport": "cloud",
            "fallback_active": fallback_active,
            "fallback_reason": fallback_reason,
            "provider_epoch": provider_epoch,
            "next_cloud_probe_in_seconds": next_probe_in,
            "generation_model": str(profile.get("model") or "") or None,
        }

    @property
    def current_speech_source(self):
        with self._lock:
            return self._current_speech_source

    @property
    def current_processing_source(self):
        with self._lock:
            return self._current_processing_source

    @property
    def ptt_position_cut_enabled(self) -> bool:
        """WU5 D1 host flag: True only when the API host installed the
        position-aware PTT cut policy. Default False (CTK-unchanged)."""
        return self._ptt_position_cut_enabled

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

    def speech_remaining_estimate(self) -> Optional[float]:
        """WU4 4c seam (design-fase2.md §3): best-effort remaining-playback
        estimate, in seconds.

        Derived from the REAL `_hablar_impl` consumer loop's own progress
        counters (`_speech_progress`: total fragments / fragments played so
        far / wall-clock start / `first_play`) — the actual seam the TTS
        pipeline already tracks, not a separate timer. Formula:
        `remaining_fragments * mean_seconds_per_played_fragment`. Returns
        None while not speaking, or before the first fragment has finished
        playing (no rate to extrapolate from yet).

        T2(b) [v5]: the mean is measured from `first_play` (monotonic, set
        the moment the FIRST fragment's playback actually started) when
        available, not from `start` (wall-clock, set when synthesis of the
        WHOLE utterance began). Using `start` inflates the mean by the
        synthesis wait before playback ever began — a slow-to-synthesize
        first fragment would otherwise make every later fragment look far
        slower than its real playback rate. Falls back to the legacy
        `start`-based measurement when `first_play` is absent (e.g. a
        test-constructed progress dict), so behavior is unchanged there.

        DELIBERATELY still on `_speaking`, not `_speech_active` (design §11):
        this predicate's consumers need "audio playing RIGHT NOW". A queued or
        suspended job has no rate to extrapolate from, and the connector
        upgrade would spawn an Ollama call on a false GPU-free premise.
        """
        with self._lock:
            if not self._speaking:
                return None
            progress = self._speech_progress
        if not progress:
            return None
        played = progress["played"]
        # Judge closure (2026-08-05): `played`/`total` are TURN-absolute
        # (they include a resume's `cursor_base`), but the rate must stay
        # local to THIS invocation — `first_play`/`start` belong to the
        # resumed slice, and fragments played before the hold carry no
        # information about the post-hold playback rate. `base` is 0 on any
        # non-resume invocation and absent on a test-constructed dict, so
        # legacy behavior is bit-identical there.
        played_here = played - progress.get("base", 0)
        if played_here <= 0:
            return None
        first_play = progress.get("first_play")
        if first_play is not None:
            elapsed = time.monotonic() - first_play
        else:
            elapsed = time.time() - progress["start"]
        mean_per_fragment = elapsed / played_here
        remaining_fragments = max(0, progress["total"] - played)
        return remaining_fragments * mean_per_fragment

    # ── WU5 (design-fase2.md §3 WU5): interruption + connector-based return ──

    def ptt_interrupt_if_agenda_speaking(self) -> str:
        """D1 cut seam (design-fase2.md §3 WU5): a PTT arrived — decide cut vs
        defer by position over the CURRENT agenda speech. Returns:
          - "off"   the policy is disabled (host flag off), or the current speech
                    is not an agenda turn (typed/chat answers are never cut).
          - "defer" the zones say don't cut (early/late) or the mid-band margin
                    rule says the turn ends soon anyway — the PTT answer
                    pregenerates and plays at the boundary.
          - "cut"   a mid-band turn with a long remaining window: the next-turn
                    agenda draft is frozen and the current speech is interrupted.

        The engine-level seam self-gates on `kira-agenda*` current_speech_source
        and the host flag, so a typed/chat source can never trigger a cut and CTK
        (which never installs the flag) never cuts. Called synchronously from the
        PTT flush thread (ptt_session's precheck hook), the only path that can cut
        DURING an agenda turn's speech (the queue path runs at boundaries).
        """
        if not self._ptt_position_cut_enabled:
            return "off"
        with self._lock:
            speaking = self._speaking
            source = self._current_speech_source
            progress = dict(self._speech_progress) if self._speech_progress else None
        if not speaking or not (isinstance(source, str) and source.startswith("kira-agenda")):
            return "off"
        frac = None
        if progress:
            total = progress.get("total") or 0
            played = progress.get("played") or 0
            if total > 0:
                frac = played / total
        # Unknown progress, early zone, or late zone -> defer (never cut).
        if frac is None or frac < CUT_ZONE_EARLY or frac > CUT_ZONE_LATE:
            return "defer"
        # Mid zone: deterministic margin rule (no LLM).
        remaining = self.speech_remaining_estimate()
        if remaining is not None and remaining > CUT_THRESHOLD_SECONDS:
            # F2: only cut when there is a next-turn draft to freeze — cutting with
            # nothing to return to would lose the beat forever. No draft -> defer
            # (the answer plays at the boundary, the current turn finishes).
            if not self._freeze_agenda_stash():
                return "defer"
            self.interrupt_speaking()
            return "cut"
        return "defer"

    def speech_pause_would_fire(self) -> None:
        """Step 0 telemetry (interruptible_speech_architecture_20260804 §8
        step 0, §5.1): a read-only probe wired to `PttController.start()`'s
        press-time precheck -- mirrors `ptt_interrupt_if_agenda_speaking`'s
        wiring shape, but NEVER cuts anything and has no host-flag gate.

        Logs what a real `pause_speech("ptt")` would look like at this exact
        instant (design's future step 3), so real press frequency and cut
        position can be measured before anything is armed. No-op (no log)
        when not currently speaking. Never mutates `_speaking` or any other
        engine state.
        """
        with self._lock:
            speaking = self._speaking
            source = self._current_speech_source
            progress = dict(self._speech_progress) if self._speech_progress else None
        if not speaking:
            return
        played = progress["played"] if progress else 0
        total = progress["total"] if progress else 0
        logger.info(f"[SPEECH_PAUSE] would-fire source={source} played={played} total={total}")

    def _freeze_agenda_stash(self) -> bool:
        """WU5 D2: move the cached NEXT-turn agenda draft out of the active pregen
        slot into `_frozen_stash`, so the interruption answer can use the freed
        slot (WU3 pregen) and the agenda draft survives the detour to be resumed.
        No epoch bump — freezing is not invalidation (there is no in-flight worker
        for an already-cached draft). Resets the detour counter. Returns True iff
        a draft was frozen (nothing to return to otherwise — normal flow resumes).
        """
        with self._prefetch_lock:
            return self._freeze_agenda_stash_locked()

    def _freeze_agenda_stash_locked(self) -> bool:
        """`_freeze_agenda_stash`'s body, with the caller holding `_prefetch_lock`.

        Split out for Step 2 (direct_turn_preemption_20260803): `pregenerate`'s
        slot-handover branch already holds `_prefetch_lock`, and threading.Lock
        is not reentrant. Same contract, same mutations, no epoch bump.
        """
        cached = self._prefetched_agenda
        if cached is None or not str(cached.get("source", "")).startswith("kira-agenda"):
            return False
        frozen = dict(cached)
        frozen["connector"] = None
        self._frozen_stash = frozen
        # R1: stamp the freeze time so the driver can bound the hold.
        self._frozen_stash_at = time.monotonic()
        self._prefetched_agenda = None
        self._prefetch_done.clear()
        self._detour_turns = 0
        return True

    def has_frozen_stash(self) -> bool:
        with self._prefetch_lock:
            return self._frozen_stash is not None

    def frozen_stash_age_seconds(self) -> Optional[float]:
        """R1 seam: seconds since the current stash was frozen, or None when no
        stash is pending (or no freeze time was recorded). The driver reads this
        instead of the engine internals to enforce the FROZEN_STASH_MAX_HOLD
        deadline backstop."""
        with self._prefetch_lock:
            if self._frozen_stash is None or self._frozen_stash_at is None:
                return None
            return time.monotonic() - self._frozen_stash_at

    def detour_exceeded(self) -> bool:
        """D2 skip: more than RETURN_MAX_DETOUR_TURNS interactive turns chained
        since the cut — a real conversation started, so skip the return."""
        with self._prefetch_lock:
            return self._detour_turns > RETURN_MAX_DETOUR_TURNS

    def detour_started(self) -> bool:
        """F1: True once at least one interactive turn has been noted since the
        cut. The return must HOLD until then — the interruption answer rides the
        command queue (invisible to the driver's return gates), so a speaking_end
        from the cut itself must never fire the connector return BEFORE the answer
        speaks. The detour counter increments as the answer turn is selected."""
        with self._prefetch_lock:
            return self._detour_turns > 0

    def discard_frozen_stash(self) -> None:
        """D2 skip (driver-side): drop the frozen stash and reset the detour
        counter so the return is suppressed and normal next_action resumes."""
        with self._prefetch_lock:
            self._frozen_stash = None
            self._frozen_stash_at = None
            self._detour_turns = 0

    def _invalidate_frozen_stash(self) -> None:
        """D2 epoch skip: a profile/model switch or a session stop invalidates a
        pending return (a stashed draft built under the old persona/history must
        never be resumed). Called from set_profile, switch_llm_tier, and the
        agenda drop path (drop_pending_sources)."""
        with self._prefetch_lock:
            self._frozen_stash = None
            self._frozen_stash_at = None
            self._detour_turns = 0

    def restore_frozen_stash(self, tema: str = "") -> Optional[tuple]:
        """WU5 D2/D3 RETURN: move the frozen agenda draft back into the active
        pregen slot (marked `resumed`, connector resolved) so the normal
        consume+pop+speak path resumes it. The connector is the ready generated
        upgrade if one landed during the answer's TTS, else the parameterized
        pool floor (D3). Returns (payload, source, priority) for the driver to
        requeue, or None when there is no frozen stash (a skip fired)."""
        with self._prefetch_lock:
            stash = self._frozen_stash
            upgrade = stash.get("connector") if stash is not None else None
        connector = upgrade if upgrade else self._pick_connector_floor(tema)
        with self._prefetch_lock:
            stash = self._frozen_stash
            if stash is None:
                return None
            restored = dict(stash)
            restored["connector"] = connector
            restored["resumed"] = True
            # F5: bump the pregen epoch as the stash re-enters the active slot (same
            # pattern as _commit_history) so any in-flight generation keyed to the
            # pre-restore epoch is invalidated — it must never overwrite the resumed
            # draft. The restored draft is placed directly (not via a worker store),
            # so the bump kills only OTHER stale workers, never this draft.
            self._prefetch_epoch += 1
            self._prefetched_agenda = restored
            self._frozen_stash = None
            self._frozen_stash_at = None
            self._detour_turns = 0
            self._prefetch_done.set()
        return (restored["payload"], restored.get("source", "kira-agenda"), restored.get("priority", 2))

    def _pick_connector_floor(self, tema: str) -> str:
        """D3 floor: the next es-AR connector template, rotated without immediate
        repetition, parameterized with the live topic title."""
        templates = i18n_active.connector_templates()
        if not templates:
            return ""
        with self._prefetch_lock:
            last = self._connector_last_idx
            idx = 0 if last is None else (last + 1) % len(templates)
            self._connector_last_idx = idx
        try:
            return templates[idx].format(tema=tema or "eso")
        except (KeyError, IndexError):
            return templates[idx]

    def _note_detour_turn(self, source: str) -> None:
        """WU5 D2: count an interactive turn toward the detour budget when a
        return is pending. No-op when no return is pending or for an agenda-source
        turn (the resume turn itself never counts). Called as the turn is selected
        to speak — the connector UPGRADE is triggered separately, at speaking_start
        (during TTS playback), so it never races the turn's own generation."""
        if not source or source.startswith("kira-agenda"):
            return
        with self._prefetch_lock:
            if self._frozen_stash is None:
                return
            self._detour_turns += 1

    def _maybe_generate_connector_upgrade(self) -> None:
        """D3 upgrade: while the interruption answer's TTS plays (GPU free),
        generate a one-line contextual connector into `_frozen_stash['connector']`.
        LOWEST priority by construction: it writes to a SEPARATE field (never the
        `_prefetched_agenda` slot), and refuses to even spawn while a real pregen
        occupies or is in flight for that slot — so it can never evict or delay a
        real interactive/agenda pregen (AC5.6). Best-effort: a late/rejected/absent
        upgrade just falls back to the pool floor at the return (timeout-0 read).

        Step 4 batch 2 (audit item 3): armed, a priority-0 owner question can
        pop and GENERATE under this very playback (widened §5.2 gate), so two
        armed-only refusals below keep the upgrade off the single runner then.
        Residual: a question arriving AFTER the upgrade's request is in flight
        still serializes behind it for up to ~CONNECTOR_UPGRADE_TIMEOUT_SECONDS
        client-side — longer server-side on a stalling model, since the
        watchdog abandon does not free the runner — unclosable without a
        cancellable transport.
        """
        # F1 Pregen Cloud Gate (multi_provider_llm_20260723): the connector
        # upgrade is speculative generation — it calls _generar_dialogo directly,
        # bypassing pregenerate()'s gate. OFF by default on cloud (billable
        # tokens); local short-circuits so all existing behavior is byte-identical.
        # Same two-condition idiom as the pregenerate gate.
        if not self._is_local and not self._provider_config.get("pregen_enabled", False):
            return
        # R3 spawn gate: the upgrade is cosmetic (the pool floor is a complete
        # connector). Only spawn when the remaining-playback estimate comfortably
        # covers a bounded generation — otherwise the upgrade could outlive the
        # answer's playback and start a second concurrent Ollama call behind the
        # real turn (heavy/stalling models). An unknown/short estimate -> skip; the
        # floor stands. (At speaking_start the answer's own progress is not yet
        # measurable, so this commonly skips and the floor is used — by design.)
        remaining = self.speech_remaining_estimate()
        if remaining is None or remaining <= CONNECTOR_UPGRADE_MIN_REMAINING_SECONDS:
            return
        # Step 4 batch 2 (audit item 3): armed, a queued priority-0 owner
        # question WILL pop under this very playback — spawning the cosmetic
        # upgrade now would race the owner's answer for the single runner.
        # Unarmed this gate stays dead: a legacy owner question waits for the
        # boundary, and playback IS the legacy GPU-free window.
        if (
            self._speech_interrupt_enabled
            and self._speech_router_enabled
            and self.has_pending_priority_before(1)
        ):
            return
        with self._prefetch_lock:
            stash = self._frozen_stash
            if stash is None or stash.get("connector") is not None:
                return
            # Yield to any real pregen — never compete for the single Ollama runner.
            if self._prefetched_agenda is not None or self._pregen_inflight is not None:
                return

        def worker() -> None:
            # F3: claim single-Ollama occupancy ATOMICALLY before generating. The
            # spawn-time yield checks above are racy (a foreground/pregen generation
            # can start after them), so gate on `_llm_generating` under the same lock
            # the generation path uses; if a generation is already in flight, skip —
            # the pool floor is an acceptable connector, never queue or retry.
            with self._lock:
                if self._llm_generating:
                    return
                # Step 4 batch 2 (audit item 3): armed, `_processing` True
                # means a widened pop is dispatching a REAL foreground turn —
                # including the [pop -> `_llm_generating` set] gap and retry
                # gaps the flag check above misses. Claiming now would hold
                # the runner against the owner's priority-0 answer. Unarmed
                # this branch stays dead: the legacy blocking path holds
                # `_processing` through the parent turn's whole PLAYBACK, and
                # refusing on it would disable the upgrade outright.
                if (
                    self._speech_interrupt_enabled
                    and self._speech_router_enabled
                    and self._processing
                ):
                    return
                self._llm_generating = True
            # R4 ownership token: _generar_dialogo self-brackets `_llm_generating`
            # (sets it, clears it in its own finally), so once it runs it is the SOLE
            # releaser of THIS claim. The outer finally must clear the flag ONLY on an
            # early error/skip BEFORE _generar_dialogo ran — clearing after it returned
            # would clobber a THIRD party's fresh claim taken in the release window
            # (making `_llm_generating` read False during a live Ollama call, so the
            # WU3 GPU-free predicate misfires).
            generation_started = False
            try:
                prompt = (
                    "Generá UNA sola línea muy corta de transición para retomar "
                    "lo que venías diciendo antes de que te interrumpieran. Natural, "
                    "sin anunciar estructura, sin saludar, sin cerrar."
                )
                generation_started = True
                # R3: hard-bound the generation so a heavy/stalling model abandons
                # the cosmetic upgrade (watchdog_timeout suppresses the heavyweight
                # stall recovery too) instead of racing the real turn.
                line = self._generar_dialogo(
                    prompt, source="kira-agenda", commit_history=False,
                    log_prefix="Connector", watchdog_timeout=CONNECTOR_UPGRADE_TIMEOUT_SECONDS,
                )
                if not line:
                    return
                if not self._preview_accept_agenda_output(line):
                    return
                # F6: only land the connector on the SAME stash this worker was
                # spawned for. A discard+new-freeze between spawn and completion
                # must not stamp this now-stale connector onto a fresh stash.
                with self._prefetch_lock:
                    if self._frozen_stash is stash and stash.get("connector") is None:
                        stash["connector"] = line
            except Exception:
                logger.exception("connector upgrade generation failed")
            finally:
                if not generation_started:
                    with self._lock:
                        self._llm_generating = False

        threading.Thread(target=worker, daemon=True).start()

    def _pregen_retry_gate_seconds(self) -> float:
        """T2(a) [v5]: the adaptive retry-gate threshold for a rejected
        background pregen — 1.2x the last COMPLETED generation's duration
        (foreground or pregen), falling back to the flat
        RETRY_MIN_REMAINING_SECONDS constant on a cold start (no generation
        measured yet this session). Design-spec adaptive gate, constant as
        cold-start fallback.
        """
        last = self._pregen_last_gen_duration
        if last is None:
            return RETRY_MIN_REMAINING_SECONDS
        return last * 1.2

    def _speech_ms_for_boundary(self) -> int:
        """T1(a) [v5]: the PREVIOUS turn's speech duration in ms, for the
        `speech_ms=` field of a "Pregen boundary:" telemetry line. -1 while
        unknown (no turn has finished speaking yet this session).
        """
        with self._lock:
            duration = self._last_speech_duration_ms
        return duration if duration is not None else -1

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

    def play_ptt_cue(self) -> None:
        """Play a short, low-volume blip the instant a PTT hold starts listening.

        A nicety so the operator knows the mic is live while gaming with the
        app window unfocused/minimized: the frontend pauses its polls in the
        background, so a UI-side sound would not fire reliably. Wired from
        ``PttController.start`` (opencohost/api/ptt_session.py) via the
        ``on_listening`` hook, on the HTTP handler thread.

        Plays through ``winsound`` (a separate Windows audio path with ZERO
        pygame/SDL interaction), so it is safe to fire from any thread — it
        shares no state with the engine's pygame mixer, which the voice-death
        reinit quits+re-inits on its own thread after every PTT close. The cue
        is fire-and-forget (``SND_ASYNC``) and fail-open: ``PTT_CUE_ENABLED``
        gates it off entirely, a non-Windows host (no ``winsound``) no-ops, and
        every error is swallowed — a cue that fails must never break PTT start.
        """
        if not PTT_CUE_ENABLED or winsound is None:
            return
        try:
            winsound.PlaySound(
                self._ptt_cue_wav(),
                winsound.SND_MEMORY | winsound.SND_ASYNC | winsound.SND_NODEFAULT,
            )
        except Exception:
            logger.debug("PTT cue playback skipped (fail-open)", exc_info=True)

    def _ptt_cue_wav(self) -> bytes:
        """Lazily build and cache the PTT blip as an in-memory WAV file.

        Pure stdlib (no numpy): a soft ~120 ms 880 Hz sine, 44100 Hz mono
        16-bit, with a short fade in/out to kill click artifacts.
        ``PTT_CUE_VOLUME`` is baked into the sample amplitude because winsound
        has no volume control. The bytes are immutable and independent of the
        pygame mixer, so they are built once and cached forever.
        """
        if self._ptt_cue_wav_bytes is not None:
            return self._ptt_cue_wav_bytes
        import io
        import math
        import struct
        import wave

        rate = 44100
        n = max(1, int(rate * 0.12))
        fade = max(1, n // 8)
        amp = max(0.0, min(1.0, PTT_CUE_VOLUME)) * 32767.0
        frames = bytearray()
        for i in range(n):
            if i < fade:
                env = i / fade
            elif i >= n - fade:
                env = (n - i) / fade
            else:
                env = 1.0
            sample = int(math.sin(2.0 * math.pi * 880.0 * i / rate) * env * amp)
            frames += struct.pack("<h", sample)
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(rate)
            wav.writeframes(bytes(frames))
        self._ptt_cue_wav_bytes = buf.getvalue()
        return self._ptt_cue_wav_bytes

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
        # Step 3 (design §4 sweeps): a stack entry at any depth matching one
        # of these prefixes is discarded too — the emergency stop must not
        # leave a `kira-agenda*` beat resumable after the token is set.
        router = self._speech_router
        if router is not None:
            router.sweep_sources(prefixes)

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
                # Check priority queue and accumulation buffer when idle.
                # §11 B4 backstop: apply verbs deferred mid-utterance even if
                # no further boundary ever runs (CTK detached playback; a
                # lost wake sentinel). Gated inside on _speech_active.
                self._drain_control_commands()
                self._process_priority_queue()
                self._check_pending_model_switch()
                # memory_promotion_20260725 (owner decision 7): the FIRST idle
                # tick of the process is the guaranteed-idle, pre-stream moment
                # the draft-promotion sweep needs. Both surfaces (GUI app_shell
                # and the API engine_host) drive this same run(), so there is no
                # call site to add in either — and unlike a hasattr-guarded call
                # in a constructor that has not run yet, deleting this line makes
                # its test fail.
                # Latch on the OUTCOME, never on the attempt: on the API surface
                # `_seed_startup_profile` (the only setter of
                # `_current_profile_id`) runs AFTER `motor.start()` and after the
                # health monitor, agenda and music library are built, so this
                # tick routinely fires first with no profile; on the GUI one
                # Ollama may not be up yet. Arming here would disable promotion
                # for the whole process on a transient not-ready first tick.
                if not self._promotion_swept:
                    try:
                        if not self.promote_pending_drafts().get("skipped"):
                            self._promotion_swept = True
                    except Exception as exc:
                        self._promotion_swept = True  # a raising sweep is an attempt
                        logger.warning(
                            "memoria promotion sweep escaped isolation: %s", type(exc).__name__
                        )
                continue

            if comando is None:
                self._log("Señal de cierre recibida. Terminando hilo IA.")
                break

            self._consume_command(comando)

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
        # Unit 4.1: OPTIONAL 5th element, the monotonic submit stamp (dispatch.py).
        # Omitted -> None, so a control/legacy 2-4 tuple is unchanged.
        submitted_at = rest[2] if len(rest) > 2 else None
        # Unit 4.2 (F12 closure): OPTIONAL 6th element, the provider posture at
        # submit time (dispatch.py). Omitted -> None, so a shorter tuple (every
        # caller before this unit) is unchanged.
        submitted_under_provider = rest[3] if len(rest) > 3 else None
        # C1 (refactor_core_api_20260802): the two optional stamp fields collapse
        # into ONE optional TurnStamp -- None (never a stamp with a None
        # submitted_at) for a tuple that never carried a submit-time stamp.
        stamp = (
            TurnStamp(submitted_at=submitted_at, submitted_under_provider=submitted_under_provider)
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
            # §11 B5: the router's wake sentinel. Without it the post-boundary
            # tail (control drain, direct drain, pending model switch,
            # priority-queue re-entry) would only re-enter through run()'s 1 s
            # `command_queue.get` idle tick — up to a second of NEW dead air at
            # every turn boundary. `emit_idle=False`: the router already
            # emitted 'idle' at job completion (B3), and a second one would
            # flicker the avatar.
            self._complete_processing_cycle(emit_idle=False)
            return

        # §11 B4, PRIMARY path (closure finding 2026-08-05): with the router
        # armed the engine loop is FREE during playback, so this dispatch can
        # run MID-UTTERANCE — set_piper_voice would reload the very Piper
        # object the producer is synthesizing on. Park drain-safe verbs until
        # the boundary drain (wake sentinel / idle tick), exactly when a
        # pre-router engine — blocked inside `_hablar` — applied them.
        # Payload never logged (set_profile carries a prompt).
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
            if not self.is_ready:
                # R1: an interruption answer dropped here never reaches a
                # _note_detour_turn site, so a frozen return would HOLD forever.
                # The driver's FROZEN_STASH_MAX_HOLD_SECONDS deadline backstop
                # (agenda_driver._maybe_return_frozen_stash) covers this lost answer.
                self._log("Ollama no esta listo. Usa el boton de Ollama/modelo para iniciarlo.", level="warning")
                self.ui_callback("ollama_unavailable")
                return
            if self._processing or self._speech_active:
                # Motor busy — enqueue to priority queue instead of dropping
                self._log("Ya procesando. Encolando en cola prioritaria...", level="debug")
                # C1 (refactor_core_api_20260802): forward the stamp's fields ONLY
                # when a stamp is present, so a caller/test that stubs enqueue()
                # with the pre-C1 signature (no submitted_at/submitted_under_provider
                # params) is unaffected by an unstamped turn.
                #
                # Step-4 batch-1 blocker (2026-08-05): this branch hardcoded
                # `priority=1` for EVERY source, including "ptt" — the same
                # bug class direct_turn_preemption_20260803 Step 1 fixed at
                # `_drain_pending_direct_into_priority_queue` (:3471,
                # "the priority is now the item's OWN documented one") and
                # never mirrored here. At priority 1 a busy-enqueued PTT
                # question was TTL-expirable (the sweep's `prio > 0` guard,
                # against its own "Expire stale non-PTT items" contract) and
                # invisible to the widened §5.2 pop gate. Same expression as
                # the sibling: PTT 0, everything else its documented 1.
                # Unarmed consequence, INTENDED (judge pass): at priority 0 a
                # busy-enqueued PTT is TTL-EXEMPT, so a receipted owner
                # question stranded behind a long block is now spoken late
                # rather than silently discarded — matching the boundary
                # drain's existing behavior and the sweep's own wording.
                busy_priority = 0 if source == "ptt" else 1
                if stamp is not None:
                    self.enqueue(
                        payload, priority=busy_priority, source=source, history_text=history_text,
                        submitted_at=stamp.submitted_at, submitted_under_provider=stamp.submitted_under_provider,
                    )
                else:
                    self.enqueue(payload, priority=busy_priority, source=source, history_text=history_text)
                # Step 4 (interruptible_speech_architecture_20260804,
                # step4-plan.md batch 1 item 2): without this, an item
                # re-enqueued here strands until run()'s 1s idle tick
                # (command_queue.get(timeout=1.0)) even when the widened
                # §5.2 gate would admit it immediately. Flat call -- same
                # depth as run()'s own idle-branch call to this method.
                # Conjunction-gated (judge pass): unarmed, the widened gate
                # never admits anything mid-speech, so the only thing an
                # unconditional call could do is run a full legacy turn
                # nested here when speech happens to end inside the
                # check-to-call window — a new unarmed execution path for
                # zero gain. Legacy keeps run()'s idle tick, byte-identical.
                if self._speech_interrupt_enabled and self._speech_router_enabled:
                    self._process_priority_queue()
                return
            with self._lock:
                self._processing = True
                self._current_processing_source = source
            self.ui_callback("processing")
            # WU5: the PTT/typed interruption answer taken on the foreground path
            # (idle at command-read time) still counts as a detour turn (no-op
            # unless a frozen return is pending). The connector upgrade fires at
            # speaking_start so it never races this turn's own generation.
            self._note_detour_turn(source)
            try:
                # C1 (refactor_core_api_20260802): same conditional-forward
                # rationale as enqueue() above.
                if stamp is not None:
                    self._ejecutar_inferencia(payload, source=source, history_text=history_text, stamp=stamp)
                else:
                    self._ejecutar_inferencia(payload, source=source, history_text=history_text)
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

            if self._processing or self._speech_active:
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
            # A draft generated under the old persona must never be spoken (#344).
            self.clear_prefetched_agenda()
            self._invalidate_frozen_stash()  # WU5 D2: a stashed return under the old persona is stale
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
                # W2a: snapshot the DEPARTING profile id + this session's titles
                # (before the id swap) and clear titles so the new profile starts
                # a fresh session — same atomic critical section as the drafts.
                departing_profile_id = self._current_profile_id
                summary_titles = list(self._session_memoria_titles)
                self._session_memoria_titles.clear()
                self._current_profile_id = payload.get("id")
                self.historial.clear()
                self._memory_digest.clear()
            # RC-2/RC-3: disk upserts dispatched AFTER lock release, on a
            # worker thread — the profile switch must never block on I/O. The
            # departing profile's mechanical session summary rides that same
            # worker (W2a), so it never blocks the Tk thread either.
            self._dispatch_switch_flush(switch_drafts, departing_profile_id, summary_titles)
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
        submitted_at: Optional[float] = None,
        submitted_under_provider: Optional[str] = None,
    ) -> None:
        """Add a message to the priority queue.

        Args:
            payload: The text to process.
            priority: 0 = PTT/streamer (highest), 1 = chat (normal), 2 = agenda (lowest).
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
        """
        with self._pq_lock:
            self._priority_queue.append(
                (priority, time.time(), payload, source, history_text, submitted_at, submitted_under_provider)
            )
            self._priority_queue.sort(key=lambda x: (x[0], x[1]))
            # Enforce max items — drop lowest priority (highest number) first,
            # breaking ties by newest timestamp. PTT (0) is always preserved over
            # chat (1) and agenda (2). interruptible_speech_architecture_20260804
            # §6 Step 2: an owner question (_OWNER_QUESTION_SOURCES) must never be
            # demoted into _accumulation_buffer — it would be answered under
            # source="accumulated", excluded from all five privacy/personalization
            # frozensets. Scan from the tail for the last non-owner item instead of
            # blindly popping the tail.
            while len(self._priority_queue) > self._pq_max_items:
                idx = next(
                    (
                        i
                        for i in range(len(self._priority_queue) - 1, -1, -1)
                        if self._priority_queue[i][3] not in _OWNER_QUESTION_SOURCES
                    ),
                    None,
                )
                if idx is None:
                    # ponytail: every pending item is an owner question — the cap
                    # yields rather than silently losing one. Bounded in practice
                    # by human typing rate and drained wholesale by the next
                    # bundle (design §4 step 10). Add a hard owner cap only if a
                    # real session shows unbounded growth.
                    break
                dropped = self._priority_queue.pop(idx)
                self._log(f"Cola prioritaria llena. Descartado (baja prioridad): {dropped[3]}")
                self.enqueue_accumulation(dropped[2], source=dropped[3])
            # WU3 (design-fase2.md §2.3): snapshot the HEAD under the lock; the
            # interactive pregen trigger runs OUTSIDE it so it never blocks the
            # enqueue caller. F1 [v4]: carry history_text (tuple index 4) so a
            # reply spoken from the pregen cache commits the HONEST turn text, not
            # the raw prompt template (the memoria_quality regression).
            head_snapshot = self._head_snapshot_locked()
        self._maybe_trigger_interactive_pregen(head_snapshot)

    def _head_snapshot_locked(self):
        """(priority, payload, source, history_text) of the queue HEAD, or None.

        Caller MUST hold `_pq_lock`. Extracted so the pregen completion
        re-trigger (Step 2, direct_turn_preemption_20260803) builds the snapshot
        in exactly the same shape `enqueue()`'s tail does.
        """
        head = self._priority_queue[0] if self._priority_queue else None
        if head is None:
            return None
        return (head[0], head[2], head[3], head[4] if len(head) > 4 else None)

    def _retrigger_interactive_pregen(self, just_finished: tuple) -> None:
        """Step 2 (direct_turn_preemption_20260803): re-evaluate the queue head
        when a pregen worker releases the slot.

        The interactive trigger fires ONLY from `enqueue()`'s tail, and its
        GPU-free gate refuses while a generation is in flight. That is the exact
        measured gap: on 2026-08-03 at 14:33:12 a direct turn was enqueued while
        the agenda's own pregen was mid-flight (14:33:11 -> 14:33:25), the gate
        refused, and nothing ever re-fired — the turn reached its boundary with
        `Pregen boundary: draft=none source=direct` and generated serially.

        `just_finished` is this spawn's own (payload, source). A spawn whose own
        item is STILL the head must not re-fire: for a stored draft the
        occupancy check in `_maybe_trigger_interactive_pregen` already no-ops,
        but for a REJECTED/discarded one (which stores nothing) it would spawn
        the identical generation again — and again at its completion — an
        unbounded regeneration loop for as long as the speech lasts.
        """
        with self._pq_lock:
            head_snapshot = self._head_snapshot_locked()
        if head_snapshot is None:
            return
        if (head_snapshot[1], head_snapshot[2]) == just_finished:
            return
        self._maybe_trigger_interactive_pregen(head_snapshot)

    def _maybe_trigger_interactive_pregen(self, head_snapshot) -> None:
        """WU3 interactive trigger (design-fase2.md §2.3): pregenerate the queue
        HEAD in the background while TTS plays, so a typed/PTT reply speaks
        near-instantly at the turn boundary instead of after a silent generation.

        GPU-free predicate: speaking now AND Ollama not mid-call
        (`_llm_generating` False — a generation already in flight blocks a second,
        the single-Ollama rule). Excludes `source="accumulated"` (its payload does
        not exist until flush). Head-tracking (AC3.5): skip when the head already
        matches the slot occupant; otherwise `pregenerate()` evicts a strictly-
        lower-priority occupant, so a higher-priority head landing in front (e.g.
        a PTT behind a chat pregen) displaces the now-stale one and pregenerates
        the new head. Spawn-only: `pregenerate()` starts a daemon thread and
        returns immediately, so the enqueue caller never blocks.

        Two callers: `enqueue()`'s tail (arrival) and
        `_retrigger_interactive_pregen` (Step 2, direct_turn_preemption_20260803
        — a pregen worker releasing the slot). Every gate below applies
        identically to both; nothing here interrupts anything, it only decides
        whether to start a background generation.
        """
        if head_snapshot is None:
            return
        priority, payload, source, history_text = head_snapshot
        if source == "accumulated":
            return
        # Rule 2 (interruptible_speech_architecture_20260804 §2c) — the
        # load-bearing half of the pair. With >=2 owner questions pending, the
        # next boundary bundles them into ONE request; drafting just the head
        # would re-serialize the burst, because Rule 1 always speaks a draft
        # that exists: answer 1's playback covers a draft for question 2, whose
        # playback covers a draft for question 3, and so on. Every question ends
        # up individually drafted and individually spoken, the bundle never
        # forms, and the busier the session the LESS bundling fires — the exact
        # inversion of the feature, and the measured 2026-08-03 incident (one
        # question per ~108s turn at 99.8% utilization). This removes spawns,
        # never adds them: a draft that already exists or is in flight is
        # untouched, because paid GPU work is always spoken.
        #
        # Race-sound while speaking: the count is a fresh _pq_lock read and the
        # refusal is stateless, so staleness only matters within THIS call.
        # Unarmed, pops cannot happen while _speaking and the count only GROWS
        # in this window. Armed (§5.2 step 4), a priority-0 pop CAN land
        # mid-speech (and the TTL sweep runs there too), so the count may also
        # SHRINK — both directions stay benign: a stale-high read refuses a
        # draft whose burst the pop-time bundler (§4 step 10) absorbs anyway
        # (the boundary just arrived early); a stale-low read spawns a draft
        # the pop either consumes (_take_pregen_if_match /
        # _wait_or_invalidate_pregen) or the F6 still-head re-check refuses.
        # Same TOCTOU posture as the F6 re-check below, with the same
        # foreground-commit epoch backstop.
        if source in _OWNER_QUESTION_SOURCES and self.pending_owner_questions() >= 2:
            return
        if not (self.is_speaking and not self.llm_generating):
            return
        # Step 4 batch 2 (audit item 2, REAL_DEFECT): armed, a widened §5.2 pop
        # dispatches a REAL foreground generation UNDER playback, and
        # `_llm_generating` alone misses its [pop -> flag set] dispatch gap and
        # every retry gap — a trigger passing there spawns a SECOND concurrent
        # generation on the single runner (the delayed foreground call can trip
        # the inference watchdog into a spurious heavy-model rollback).
        # `_processing` spans the armed foreground turn end to end, so refuse
        # on it too. Conjunction-gated because the SAME flag means something
        # else unarmed: the legacy blocking path holds `_processing` through
        # the parent turn's whole PLAYBACK — exactly WU3's GPU-free window —
        # so an unconditional check would disable interactive pregen outright.
        # Residual: the [pop -> `_processing` set] window. When the pop finds
        # no draft and no in-flight pregen it is a few instructions wide;
        # when `_wait_or_invalidate_pregen` waits (bounded by the inference
        # watchdog), the window spans that wait BUT the occupied
        # `_pregen_inflight` slot makes pregenerate()'s F2 guard and the
        # connector's own slot check refuse every spawn — the window is
        # guarded by the SLOT there, not by narrowness. Accepted, same TOCTOU
        # posture as F6 above.
        if (
            self._speech_interrupt_enabled
            and self._speech_router_enabled
            and self.is_processing
        ):
            return
        # Already pregenerating / pregenerated exactly this head -> don't re-trigger.
        # F4 [v4]: occupancy is `_pregen_inflight is not None` (set under the lock
        # BEFORE the worker spawns), not thread.is_alive() (False in the tiny
        # assign->start window, which double-spawns).
        with self._prefetch_lock:
            cached = self._prefetched_agenda
            if cached is not None:
                occ_payload, occ_source = cached.get("payload"), cached.get("source")
            elif self._pregen_inflight is not None:
                occ_payload = self._pregen_inflight.get("payload")
                occ_source = self._pregen_inflight.get("source")
            else:
                occ_payload = occ_source = None
        if occ_payload == payload and occ_source == source:
            return
        # F6 [v4]: shrink the trigger-vs-pop TOCTOU window — re-check the head is
        # STILL the queue head right before spawning (the worker may already have
        # popped it, making this pregen a wasted duplicate). The epoch bump on the
        # foreground commit remains the backstop for the residual window.
        with self._pq_lock:
            cur = self._priority_queue[0] if self._priority_queue else None
            still_head = cur is not None and (cur[0], cur[2], cur[3]) == (priority, payload, source)
        if not still_head:
            return
        self.pregenerate(payload, priority, source, history_text=history_text)

    def replace_pending(self, payload: str, priority: int = 1, source: str = "chat") -> None:
        """Replace stale pending items from the same source and enqueue a fresh one.

        This keeps product features such as Agenda Mode from stacking old
        autonomous turns while preserving unrelated higher-priority items.
        """
        with self._pq_lock:
            self._priority_queue = [item for item in self._priority_queue if item[3] != source]
        if source.startswith("kira-agenda"):
            # WU2 (design-fase2.md §2.2/§2.3): match-aware clear. The API-host
            # consume path routes a ready draft by enqueueing its OWN
            # (payload, source) through replace_pending; clearing here would nuke
            # the very draft the worker is about to match at pop (the trap). Skip
            # the clear only when this IS that draft; a genuine new turn (any
            # mismatch) still supersedes it.
            self._clear_prefetch_unless_matches(payload, source)
        self.enqueue(payload, priority=priority, source=source)

    def prefetch_agenda(self, payload: str, priority: int = 2, source: str = "kira-agenda") -> bool:
        """Generate agenda text in the background without starting TTS.

        WU3 (design-fase2.md §2.1): thin alias over the generalized `pregenerate`
        after the kira-agenda source guard. CTK callers
        (agenda_audio_controller.py) are untouched — the guard + agenda-vs-agenda
        equal-priority refusal preserve the exact pre-WU3 behavior.
        """
        if not payload or not source.startswith("kira-agenda"):
            return False
        return self.pregenerate(payload, priority, source)

    def pregenerate(
        self,
        payload: str,
        priority: int = 1,
        source: str = "chat",
        history_text: Optional[str] = None,
    ) -> bool:
        """Generalized background fill (design-fase2.md §2.1 [v2] / §2.3).

        `prefetch_agenda`'s worker body with the source gate REMOVED: any source
        may pregenerate. Agenda sources (`kira-agenda*`) additionally run the
        `_preview_accept_agenda_output` guardrail exactly as before; non-agenda
        (interactive) sources skip that preview because their transform/veto
        steps (`output_guard` for all sources, the chat repetition guard for
        chat, `_sanitize_agenda_output` for agenda) already run INSIDE
        `_generar_dialogo` — so the stored dialogo is byte-equivalent to what the
        foreground path would have said. History is deferred
        (`commit_history=False`) and committed at pop in `_speak_pregenerated`;
        `history_text` (F1 [v4]) rides along so the commit uses the honest turn
        text, not the raw prompt template.

        Slot occupancy [v4 — F2]: eviction applies to CACHED occupants ONLY. A
        request with STRICTLY higher priority (lower number) than a cached draft
        epoch-invalidates it and takes the slot (Step 2,
        direct_turn_preemption_20260803: a `direct` winner FREEZES a displaced
        `kira-agenda*` draft instead of discarding it — see the branch below);
        an equal-or-lower request is
        refused. While a worker is IN FLIGHT (marker set, nothing stored yet) its
        Ollama call is uncancellable, so ANY new request is refused — spawning a
        replacement would leave a zombie worker running concurrently whose finally
        would poison the replacement's shared bookkeeping.
        """
        # Pregen Cloud Gate (multi_provider_llm_20260723): speculative
        # generation is OFF by default on cloud (billable tokens). Local
        # short-circuits (`_is_local` True), so all existing behavior is
        # byte-identical; only an explicit PUT {"pregen_enabled": true} opts in.
        if not self._is_local and not self._provider_config.get("pregen_enabled", False):
            return False
        if not payload:
            return False
        is_agenda = source.startswith("kira-agenda")
        displaced_source: Optional[str] = None
        displaced_gen_ms: int = -1
        displaced_frozen: bool = False
        with self._prefetch_lock:
            # F2 [v4]: an in-flight worker (marker set, nothing stored yet) is
            # uncancellable -> refuse rather than spawn a second concurrent
            # generation. Once it has stored (cached), the priority eviction below
            # applies instead.
            if self._pregen_inflight is not None and self._prefetched_agenda is None:
                return False
            cached = self._prefetched_agenda
            if cached is not None:
                occupant_priority = cached.get("priority")
                if occupant_priority is not None and priority < occupant_priority:
                    displaced_source = str(cached.get("source", ""))
                    displaced_gen_ms = cached.get("gen_ms", -1)
                    # OQ-2 (direct_turn_preemption_20260803, Step 2): a DIRECT
                    # turn taking the slot FREEZES the agenda draft it displaces
                    # instead of discarding it — 11-14s of already-paid GPU work
                    # that the driver then resumes with a connector
                    # (AgendaDriver._maybe_return_frozen_stash), rather than
                    # regenerating the lost beat into dead air.
                    #
                    # Scoped to source=="direct", NOT to every interactive
                    # source, on purpose: `_frozen_stash` has exactly ONE
                    # consumer, api/agenda_driver.py, and `direct` is produced
                    # only by /api/chat/turn — i.e. only on the surface that
                    # runs that driver. A CTK-only `chat` winner would freeze a
                    # beat with nothing to return it, stranding it forever,
                    # which is strictly worse than the eviction it replaces.
                    # An already-frozen stash is never overwritten either — that
                    # would destroy the beat the driver is currently holding
                    # ticks for; evict as before in that case.
                    if (
                        source == "direct"
                        and self._frozen_stash is None
                        and self._freeze_agenda_stash_locked()
                    ):
                        # _freeze_agenda_stash_locked already cleared the slot and
                        # _prefetch_done. No epoch bump — freezing is not
                        # invalidation (there is no worker generating this draft;
                        # it is already cached).
                        displaced_frozen = True
                    else:
                        # Evict the strictly-lower-priority CACHED occupant: clear it
                        # and bump the epoch (defensive — no in-flight worker here).
                        self._prefetched_agenda = None
                        self._prefetch_done.clear()
                        self._prefetch_epoch += 1
                else:
                    return False
            self._prefetch_done.clear()
            epoch = self._prefetch_epoch
            # T5 [v5]: a local reference to THIS spawn's own marker — the
            # worker's finally compares against it by identity (not just
            # "is _pregen_inflight set") so a successor spawn's marker is
            # never wiped by a stale worker finishing in the store-to-finally
            # window (see the finally block below).
            inflight_marker = {"payload": payload, "source": source, "priority": priority}
            self._pregen_inflight = inflight_marker
            # WU4 4c: one retry-on-reject per spawn (reset here, under the same
            # lock as the rest of this spawn's bookkeeping).
            self._pregen_retried = False

        if displaced_source is not None:
            # WU4 4a boundary telemetry: the DISPLACED occupant's source, not the
            # new (winning) request's. draft=evicted means its turn falls back to
            # plain generation when it eventually pops; draft=frozen (Step 2)
            # means the draft survives and returns with a connector instead.
            self._log(
                f"Pregen boundary: draft={'frozen' if displaced_frozen else 'evicted'} "
                f"source={displaced_source} gap_ms=-1 "
                f"gen_ms={displaced_gen_ms} speech_ms={self._speech_ms_for_boundary()}"
            )

        log_prefix = "Agenda prefetch" if is_agenda else "Pregen"

        def worker() -> None:
            try:
                while True:
                    gen_start = time.monotonic()
                    dialogo = self._generar_dialogo(
                        payload, source=source, commit_history=False, log_prefix=log_prefix
                    )
                    if not dialogo:
                        return
                    if is_agenda and not self._preview_accept_agenda_output(dialogo):
                        self._log(f"Agenda: prefetch rechazado ({self._format_agenda_rejection()}).", level="warning")
                        code = self._agenda_rejection_code()
                        # T1(b) [v5]: a per-attempt notice at DEBUG only — the
                        # single INFO "Pregen boundary:" line for this spawn is
                        # emitted at its TERMINAL outcome (below), never once
                        # per rejected attempt.
                        logger.debug("Pregen retry attempt rejected: source=%s", source)
                        if self.on_guardrail_rejected is not None:
                            try:
                                self.on_guardrail_rejected(code)
                            except Exception:
                                logger.exception("on_guardrail_rejected callback failed")
                        # WU4 4c / T2(a) [v5]: retry once when the remaining
                        # speech window comfortably covers another generation —
                        # adaptive gate (1.2x the last COMPLETED generation),
                        # falling back to the flat constant on a cold start.
                        if not self._pregen_retried:
                            estimate = self.speech_remaining_estimate()
                            if estimate is not None and estimate > self._pregen_retry_gate_seconds():
                                # T6 [v5]: re-check the spawn's epoch is still
                                # current right before paying for a second
                                # generation — a stale/superseded spawn
                                # abandons the retry instead of generating for
                                # an already-doomed draft.
                                with self._prefetch_lock:
                                    still_current = self._prefetch_epoch == epoch
                                if still_current:
                                    self._pregen_retried = True
                                    continue
                                logger.debug(
                                    "Pregen retry abandoned: epoch stale (source=%s)", source
                                )
                                return
                        self._log(
                            f"Pregen boundary: draft=rejected source={source} gap_ms=-1 "
                            f"gen_ms=-1 speech_ms={self._speech_ms_for_boundary()}"
                        )
                        return
                    with self._prefetch_lock:
                        if self._prefetch_epoch != epoch:
                            self._log("Pregen descartado (invalidado por clear/stop/commit).", level="warning")
                            return
                        # T1(a) [v5]: the draft's own generation duration,
                        # recorded at store time — -1 (N/A) for a draft that
                        # never reaches this point (rejected/discarded).
                        gen_ms = int((time.monotonic() - gen_start) * 1000)
                        self._prefetched_agenda = {
                            "payload": payload,
                            "dialogo": dialogo,
                            "priority": priority,
                            "source": source,
                            "history_text": history_text,
                            "gen_ms": gen_ms,
                        }
                    if self._test_store_to_finally_hook is not None:
                        self._test_store_to_finally_hook()
                    return
            finally:
                # F4 [v4] / T5 [v5]: clear the in-flight marker (occupancy's
                # source of truth) under the lock BEFORE signalling done, so a
                # pop-side waiter waking on _prefetch_done never observes a
                # phantom in-flight marker — but ONLY when the marker is still
                # THIS spawn's own (identity check). Between this worker's
                # store above and this finally, a higher-priority request can
                # evict the just-stored draft and spawn a successor (F2 only
                # refuses while nothing is stored yet — once cached, priority
                # eviction applies), replacing _pregen_inflight with the
                # successor's OWN marker. Without this check, a stale worker's
                # unconditional clear+set here would wipe that successor's
                # marker and falsely signal _prefetch_done before the
                # successor has actually finished.
                slot_released = False
                with self._prefetch_lock:
                    if self._pregen_inflight is inflight_marker:
                        self._pregen_inflight = None
                        self._prefetch_done.set()
                        slot_released = True
                # Step 2 (direct_turn_preemption_20260803): the slot is free and
                # Ollama is idle again — re-evaluate the queue head, which may
                # have changed while this worker ran (the direct turn that
                # arrived mid-flight and whose trigger the GPU-free gate
                # refused). Outside _prefetch_lock, and only when THIS spawn is
                # the one that released the slot: if a successor already owns
                # the marker it is still generating, and pregenerate() would
                # refuse anyway.
                if slot_released:
                    self._retrigger_interactive_pregen((payload, source))

        thread = threading.Thread(target=worker, daemon=True)
        with self._prefetch_lock:
            self._prefetch_thread = thread
        # WU4 F5 (WU3 follow-up): a raising thread.start() must not leave the
        # in-flight marker stuck — that would refuse every future pregenerate()
        # forever. Clear it under the lock and report failure instead.
        try:
            thread.start()
        except Exception:
            logger.exception("Pregen worker thread failed to start")
            with self._prefetch_lock:
                self._pregen_inflight = None
            return False
        return True

    def wait_prefetched_agenda(self, timeout: float = 0.0) -> bool:
        if timeout > 0:
            self._prefetch_done.wait(timeout)
        with self._prefetch_lock:
            cached = self._prefetched_agenda
            # F5 [v4]: this is the CTK legacy agenda consume path — report ready
            # ONLY for an agenda-source draft. An interactive (chat/ptt) occupant
            # is invisible here; it pops through the worker's own queue path.
            return cached is not None and str(cached.get("source", "")).startswith("kira-agenda")

    def prefetch_pending(self) -> bool:
        """True while a prefetch worker is still generating (no draft yet).

        Distinguishes "draft is late, keep waiting" (worker alive, no cache) from
        "worker finished with nothing — fall back now" (thread dead / no cache).
        A ready draft returns False (it's no longer pending — consume it).
        """
        with self._prefetch_lock:
            thread = self._prefetch_thread
            return bool(thread is not None and thread.is_alive() and self._prefetched_agenda is None)

    def has_pending_priority_before(self, priority: int) -> bool:
        """Return True when queued work should run before a cached agenda draft."""
        with self._pq_lock:
            return any(item[0] < priority for item in self._priority_queue)

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
        with self._pq_lock:
            return sum(1 for item in self._priority_queue if item[3] in _OWNER_QUESTION_SOURCES)

    def clear_prefetched_agenda(self) -> None:
        with self._prefetch_lock:
            self._prefetched_agenda = None
            self._prefetch_done.clear()
            self._prefetch_epoch += 1

    def clear_prefetched_agenda_only(self) -> None:
        """WU4 F1 (WU3 follow-up): source-aware clear for the driver's
        yield/drop paths (design-fase2.md §3 WU4-4d).

        `AgendaDriver._clear_prefetch` used to call the unconditional
        `clear_prefetched_agenda`, which could clobber a live INTERACTIVE
        (chat/PTT) pregen the driver has nothing to do with. This clears the
        slot ONLY when it currently holds an agenda draft; an interactive
        occupant survives untouched. `clear_prefetched_agenda` itself stays
        unconditional — the CTK legacy consume path depends on it as-is.

        T4 [v5]: also covers the slot-EMPTY case — an agenda worker still IN
        FLIGHT (marker set, nothing stored yet) whose eventual store would
        land into a slot the driver just deliberately dropped. Bumping the
        epoch here invalidates that incoming store (the worker's own
        store-time epoch check in `pregenerate()`'s worker discards it), so
        an orphaned agenda draft never survives a drop it was never meant to
        see. An interactive in-flight worker is untouched either way — it has
        nothing to do with the driver's agenda-only drop.
        """
        with self._prefetch_lock:
            cached = self._prefetched_agenda
            if cached is not None:
                if not str(cached.get("source", "")).startswith("kira-agenda"):
                    return
                self._prefetched_agenda = None
                self._prefetch_done.clear()
                self._prefetch_epoch += 1
                return
            inflight = self._pregen_inflight
            if inflight is not None and str(inflight.get("source", "")).startswith("kira-agenda"):
                self._prefetch_epoch += 1

    def play_prefetched_agenda(self) -> bool:
        """Speak cached agenda text, if available, without another LLM call."""
        with self._prefetch_lock:
            item = self._prefetched_agenda
            # F5 [v4]: pop ONLY agenda-source drafts. An interactive occupant is
            # left intact (never spoken as an agenda turn) for its own worker-path
            # pop — return False without popping.
            if not item or not str(item.get("source", "")).startswith("kira-agenda"):
                return False
            self._prefetched_agenda = None
            self._prefetch_done.clear()

        def speaker() -> None:
            payload = item["payload"]
            dialogo = item["dialogo"]
            self._commit_history(payload, dialogo, source=item.get("source", "kira-agenda"))
            self._record_accepted_agenda_output(dialogo)
            self._log("Agenda: usando respuesta prefabricada durante el audio anterior.")
            self.log_queue.put(f"\n🧠 [Kira]: {dialogo}\n")
            self._emit_dialogue(dialogo, item.get("source", "kira-agenda"))
            self._speak_or_submit(dialogo, source=item.get("source", "kira-agenda"))

        if self._speech_router_enabled:
            # Step 2: the detached speaker thread existed only so playback
            # would not block this caller. `submit` is non-blocking, so it is
            # gone — one fewer thread that can outlive the turn that spawned it.
            speaker()
        else:
            threading.Thread(target=speaker, daemon=True).start()
        return True

    def _take_pregen_if_match(self, payload: str, source: str) -> Optional[dict]:
        """Pop the cached pregen draft iff it matches this (payload, source).

        WU2 (design-fase2.md §2.2): consulted at the worker's queue-pop boundary.
        A hit means the popped turn was pregenerated during the prior turn's TTS,
        so the worker speaks the cache instead of generating. A miss (empty or
        different cache) leaves the cache untouched and the worker generates
        normally. Consume-only (no epoch bump — no in-flight worker at pop time),
        mirroring play_prefetched_agenda's own pop semantics.
        """
        with self._prefetch_lock:
            cached = self._prefetched_agenda
            if cached is None:
                return None
            if cached.get("payload") == payload and cached.get("source") == source:
                self._prefetched_agenda = None
                self._prefetch_done.clear()
                return cached
            return None

    def _clear_prefetch_unless_matches(self, payload: str, source: str) -> None:
        """Supersede the cached draft UNLESS it IS this exact (payload, source).

        WU2 (design-fase2.md §2.2/§2.3): the consume path enqueues a ready
        draft's own (payload, source) to route it through the queue. Clearing the
        cache on that enqueue would nuke the draft before the worker's pop can
        match it. Skip the clear for that self-match; any mismatch is a genuine
        supersede and still clears + bumps the invalidation epoch (unchanged).
        """
        with self._prefetch_lock:
            cached = self._prefetched_agenda
            if cached is not None and cached.get("payload") == payload and cached.get("source") == source:
                return
            self._prefetched_agenda = None
            self._prefetch_done.clear()
            self._prefetch_epoch += 1

    def _clear_prefetch_if_matches(self, payload: str, source: str) -> None:
        """F7 [v4]: clear a cached pregen draft matching (payload, source).

        Called when the queue item that would have consumed the draft expires
        (TTL sweep): leaving the orphaned draft in the single slot would block
        every new prefetch until an unrelated enqueue happens to clear it. Bumps
        the epoch so a matching in-flight worker also discards its store.

        WU4 F4 (WU3 follow-up, TOCTOU): skip the clear entirely when a pregen
        worker is currently IN FLIGHT for this exact (payload, source) — a
        fresh replacement generation already superseding the stale entry.
        Clearing (and bumping the epoch) here would invalidate that fresh
        worker's imminent store for no reason; let it land naturally instead.
        """
        with self._prefetch_lock:
            inflight = self._pregen_inflight
            if (
                inflight is not None
                and inflight.get("payload") == payload
                and inflight.get("source") == source
            ):
                return
            cached = self._prefetched_agenda
            if cached is not None and cached.get("payload") == payload and cached.get("source") == source:
                self._prefetched_agenda = None
                self._prefetch_done.clear()
                self._prefetch_epoch += 1

    def _invalidate_pregen_epoch(self) -> None:
        """WU4 F3 (WU3 follow-up): unconditional epoch bump + slot clear.

        Called at every non-committing FOREGROUND fallback return in
        `_generar_dialogo` (watchdog timeout, transport error, empty dialogo,
        guardrail-no-fallback, chat-repetition fallback, agenda reject, the
        outer exception handler). Those returns skip `_commit_history` — the
        usual epoch-bump backstop (AC3.3) — so without this, a pregen worker
        that started before this failed turn could still land a stale store
        after it, silently surviving into the next pop.
        """
        with self._prefetch_lock:
            self._prefetched_agenda = None
            self._prefetch_done.clear()
            self._prefetch_epoch += 1

    def _speak_pregenerated(self, cached: dict, *, already_reported_boundary: bool = False) -> None:
        """Speak a pop-time cache hit on the WORKER thread (design-fase2.md §2.2).

        play_prefetched_agenda's speaker body, minus the parallel thread: the
        worker already owns the turn (single dispatch path), so deferred-commit +
        emit + _hablar run inline. History commits HERE, at playback, in spoken
        order (commit-once invariant — the pregen used commit_history=False).

        `already_reported_boundary` (WU4 4a): True when the caller already
        emitted this pop's "Pregen boundary:" line (the "late" wait-then-hit
        path) — skips the "used" line here so exactly one boundary line is
        logged per turn boundary.
        """
        payload = cached["payload"]
        dialogo = cached["dialogo"]
        source = cached.get("source", "kira-agenda")
        # WU5 D3 (design-fase2.md §3 WU5): a RESUMED agenda draft carries a
        # connector — prepend it so the connector + stashed dialogo are spoken
        # AND committed as ONE turn (the connector is part of the spoken turn).
        connector = cached.get("connector")
        if connector:
            dialogo = f"{connector} {dialogo}".strip()
            # clause_sanitizer V1: the guardrails ran INSIDE _generar_dialogo,
            # before this concatenation, and nothing is re-applied here (see the
            # comment below) — so the connector/draft junction is the one place a
            # clause can duplicate after the seam. Repair-only: there is no
            # regeneration machinery at playback, so a reject verdict here could
            # only stall the turn.
            if source in CLAUSE_SANITIZER_SOURCES:
                san = sanitize_clause_repetition(dialogo)
                # Logged for every verdict, including "clean" — otherwise the
                # clean count is unobtainable from the log (ADR-039 gate).
                self._log_clause_sanitizer(san, source, stage="pregen_connector")
                dialogo = san.text
        # F1 [v4]: forward the honest history_text (PTT/direct turns) exactly as
        # the foreground path does — else the raw prompt template leaks into
        # historial + memoria capture (the memoria_quality regression).
        self._commit_history(payload, dialogo, source=source, history_text=cached.get("history_text"))
        # F7 [v4]: source-aware log line — agenda keeps its historical wording;
        # interactive sources log their own, never the "Agenda:" prefix.
        if source.startswith("kira-agenda"):
            self._record_accepted_agenda_output(dialogo)
            self._log("Agenda: usando respuesta pregenerada.")
        else:
            self._log(f"Respuesta pregenerada [{source}] lista.")
        self.log_queue.put(f"\n🧠 [Kira]: {dialogo}\n")
        # WU3 interactive parity (design-fase2.md §3 WU3): mirror the foreground
        # _ejecutar_inferencia post-generation steps for non-agenda sources — the
        # emit relabels to "kira" (agenda keeps its own source) and a chat turn
        # advances the spoken clock. The dialogo already passed every transform/
        # veto INSIDE _generar_dialogo (sanitizer, output_guard, chat repetition
        # guard), so nothing is re-applied here.
        emit_source = source if source.startswith("kira-agenda") else "kira"
        self._emit_dialogue(dialogo, emit_source)
        if not already_reported_boundary:
            # WU4 4a boundary telemetry: an immediate pop-time cache hit is a
            # "used" draft. gap_ms = ms since the PREVIOUS turn's speaking_end;
            # -1 if unknown (e.g. the very first turn of the session). gen_ms
            # is this draft's own recorded generation duration (T1(a) [v5],
            # tracked in the slot dict at store time); speech_ms is the
            # previous turn's own speech duration.
            with self._lock:
                last_end = self._last_speaking_end_monotonic
            gap_ms = int((time.monotonic() - last_end) * 1000) if last_end is not None else -1
            gen_ms = cached.get("gen_ms", -1)
            # WU5: a resumed frozen draft is labelled draft=resumed (else used).
            draft_label = "resumed" if cached.get("resumed") else "used"
            self._log(
                f"Pregen boundary: draft={draft_label} source={source} gap_ms={gap_ms} "
                f"gen_ms={gen_ms} speech_ms={self._speech_ms_for_boundary()}"
            )
        # Step 2: the chat spoken clock moved to the router's job-completion
        # path (design §11 B1/B5) — it must fire only when the job actually
        # FINISHED with speech delivered, which a non-blocking submit can no
        # longer tell from here.
        self._speak_or_submit(dialogo, source=source)

    def _pregen_in_flight_for(self, payload: str, source: str) -> bool:
        """True when a pregen worker is currently generating THIS exact
        (payload, source) and has not stored a draft yet (design-fase2.md AC3.2).

        F4 [v4]: occupancy is `_pregen_inflight is not None` (set under the lock
        before spawn, cleared in the worker's finally under the lock), NOT
        thread.is_alive() — the same source of truth as every other occupancy check.
        """
        with self._prefetch_lock:
            inflight = self._pregen_inflight
            return bool(
                self._prefetched_agenda is None
                and inflight is not None
                and inflight.get("payload") == payload
                and inflight.get("source") == source
            )

    def _pregen_wait_bound(self) -> float:
        """T3 [v5]: the pop-side wait ceiling for a same-item in-flight
        pregen — `_inference_watchdog_timeout` itself (1x), not a multiple.

        On timeout the pop falls back to foreground regardless of whether the
        worker is still alive: a worker outliving the watchdog is already the
        declared transient-overlap class (design-fase2.md §4 [v4] — the rare
        watchdog-timeout window where a foreground turn may transiently
        overlap an unrelated in-flight pregen's Ollama call, bounded and
        self-healing via the epoch-bump backstop). In the API host the
        `_generar_dialogo` retry loop cannot legitimately still be running
        past this bound for THIS item's pop-side wait: `_speaking` is False
        while the pop is waiting here (no turn is being spoken), so nothing
        can race a second generation for the same item behind this wait — the
        pathological >watchdog worker is a stuck/hung Ollama call, not the
        loop's own internal (`max_intentos = 2`) retry, which the previous
        (`2x + 1.0`) docstring overstated as this wait's own ceiling.
        """
        return self._inference_watchdog_timeout

    def _wait_or_invalidate_pregen(self, payload: str, source: str) -> Optional[dict]:
        """AC3.2 [v4] wait-or-fallback at a pop-time cache miss.

        F3 [v4]: when a pregen worker is generating THIS exact item, starting a
        foreground generation is strictly worse than waiting — the single Ollama
        runner would queue the foreground BEHIND the in-flight pregen (and the
        watchdog could fire and silently drop the turn). So ALWAYS wait on
        `_prefetch_done`, bounded by `_pregen_wait_bound()` — T3 [v5]:
        `_inference_watchdog_timeout` itself (see `_pregen_wait_bound`'s
        docstring for why 1x is honest here). On wake: take the draft if it
        landed (epoch-valid), else fall back to foreground regardless of
        whether the worker is still alive — by then either the worker has
        finished (the GPU is free, a healthy pregen resolves to a cache hit)
        or it is the declared pathological transient-overlap class, and the
        foreground turn's own `_commit_history` epoch bump remains the
        backstop that discards any late store either way.
        """
        if not self._pregen_in_flight_for(payload, source):
            return None
        wait_start = time.monotonic()
        self._prefetch_done.wait(self._pregen_wait_bound())
        cached = self._take_pregen_if_match(payload, source)
        if cached is not None:
            # WU4 4a boundary telemetry: the pop WAITED on an in-flight pregen
            # and it landed — "late" (gap_ms is the actual wait duration).
            wait_ms = int((time.monotonic() - wait_start) * 1000)
            gen_ms = cached.get("gen_ms", -1)
            self._log(
                f"Pregen boundary: draft=late source={source} gap_ms={wait_ms} "
                f"gen_ms={gen_ms} speech_ms={self._speech_ms_for_boundary()}"
            )
            return cached
        self._log("Pregen en vuelo sin borrador válido; generando en vivo.")
        return None

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
            # WU5 D2: a session/emergency stop that drops pending agenda must also
            # drop a pending interruption-return (the stashed beat is now stale).
            self._invalidate_frozen_stash()

        # Step 3 (design §4 sweeps): a SUSPENDED stack entry at any depth is
        # pending work too, matching this method's own docstring — dropped
        # the same way, never counted in the dispatch-queue `removed` return
        # (its own docstring stays honest: pending priority/accumulation
        # items only).
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
        taken: list = []
        chars = len(head_item[2])
        with self._pq_lock:
            while (
                self._priority_queue
                and self._priority_queue[0][3] in _OWNER_QUESTION_SOURCES
                and len(taken) + 1 < OWNER_BUNDLE_MAX_ITEMS
                and chars + len(self._priority_queue[0][2]) <= OWNER_BUNDLE_MAX_CHARS
            ):
                nxt = self._priority_queue.pop(0)
                chars += len(nxt[2])
                taken.append(nxt)
        return taken

    def _compose_owner_bundle(self, members: list) -> tuple:
        """Render queue tuples into one (payload, history_text, stamp).

        Pure: no locks, no I/O. `members` is [head] + whatever
        _take_owner_bundle_prefix returned, each a full queue tuple.

        Presentation order is the QUEUE TIMESTAMP — chronological, the order the
        owner asked — not the priority order that decided which turn runs, so a
        priority-0 PTT question asked second is still presented second.

        `history_text` is built from each member's OWN history_text (tuple index
        4; both arrival paths supply one) and never from the prompt scaffolding
        above it: this string is what _commit_history stores as safe_context and
        therefore what memoria and the digest later recite (§OQ-6).

        The stamp carries the OLDEST member's submitted_at, so [TURN_LATENCY]'s
        queue_wait_ms reports the WORST real wait in the burst rather than the
        head's. None when no member was ever submitted through that seam — the
        same "never fake a wait we didn't measure" rule enqueue() documents.
        """
        ordered = sorted(members, key=lambda it: it[1])
        numbered = "\n".join(f"{i}. {it[2]}" for i, it in enumerate(ordered, 1))
        payload = i18n_active.owner_bundle_header().format(
            count=len(ordered), questions=numbered,
        )
        recap = "; ".join(
            (it[4] if len(it) > 4 and it[4] else it[2]) for it in ordered
        )
        history_text = i18n_active.owner_bundle_history().format(questions=recap)
        stamped = [it for it in ordered if len(it) > 5 and it[5] is not None]
        oldest = min(stamped, key=lambda it: it[5]) if stamped else None
        stamp = (
            TurnStamp(
                submitted_at=oldest[5],
                submitted_under_provider=(oldest[6] if len(oldest) > 6 else None),
            )
            if oldest is not None else None
        )
        return payload, history_text, stamp

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

        interruptible_speech_architecture_20260804, closing a hole the design's
        §7 table does not cover. The bundle is frame-local by construction
        (§3.4): `_take_owner_bundle_prefix` pops the followers OUT of
        `_priority_queue` and `members` becomes the only reference to those
        tuples. That is safe exactly as long as the turn commits — and it does
        not always. Every non-committing return `_invalidate_pregen_epoch`
        enumerates (inference-watchdog timeout, exhausted transport retries,
        empty model body, guardrail-no-fallback, chat-repetition fallback, the
        outer exception handler) reaches `_ejecutar_inferencia` as a falsy
        `dialogo`, and without this hook the whole bundle — up to
        OWNER_BUNDLE_MAX_ITEMS owner questions — evaporates: no answer, no
        `Item expirado y omitido`, no re-queue, no TTL rescue.

        Not theoretical: logs/opencohost_20260617_175453.log at 18:05:24 —
        `qwopus` hung, the watchdog fired at 45.00 s, the engine rolled back to
        `gemma4:e2b` and queue processing continued. Pre-bundling that incident
        consumed exactly ONE question and the rest were answered on following
        boundaries. That is the invariant restored here, and §7's "a question
        can be neither lost nor double-answered" depends on it.

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
        # ponytail: self-healing cap overshoot, the same call the owner-question
        # `break` in enqueue() already makes. Trim here only if a real session
        # shows the queue staying over cap.
        with self._pq_lock:
            self._priority_queue.extend(followers)
            self._priority_queue.sort(key=lambda x: (x[0], x[1]))
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
            # §5.2 (interruptible_speech_architecture_20260804 step 4): the
            # judge-closure conjunction (engram 5589). Either flag off must
            # reproduce today's combined guard BYTE-IDENTICAL -- no priority-0
            # item pops early, and the TTL sweep below stays gated exactly as
            # before. Armed, `_speech_active` alone no longer stops the loop
            # here: the actual priority-0-only restriction is applied
            # atomically with the pop, under `_pq_lock` below (no
            # peek-then-pop TOCTOU on a head that could change).
            widened_pop_armed = self._speech_interrupt_enabled and self._speech_router_enabled
            # Judge pass (step 4, convergent): `_speech_active` acquires
            # `_lock` and then the router's `_sched_lock` — a leaf that must
            # never be taken under another engine lock (design §4 I10), so
            # the gate below must NOT read the property while holding
            # `_pq_lock`. Read ONCE here, outside every lock, and use the
            # snapshot in the gate. Staleness is benign in both directions
            # and identical in class to the legacy loop-top read: speech
            # ending after the read costs one extra restricted iteration
            # (the completion cycle re-enters); speech starting after the
            # read lets this iteration pop unrestricted, exactly as the
            # legacy gate always could.
            speech_busy = self._speech_active
            if not widened_pop_armed and speech_busy:
                return

            expired_chat_infos: list = []
            expired_pregen_keys: list = []
            with self._pq_lock:
                # Expire stale non-PTT items before selecting next work
                now = time.time()
                kept = []
                for item in self._priority_queue:
                    # Slice first 4 — tolerates both the legacy 4-tuple (no
                    # history_text, e.g. tests constructing raw queue items) and
                    # the current 5-tuple produced by enqueue().
                    prio, ts, payload, source = item[:4]
                    # F2 (judgment-day WU2): kira-agenda* is EXEMPT from TTL
                    # expiry. Adopted agenda drafts routed through consume-at-event
                    # are replace_pending-deduped (they never stack), so exemption
                    # can never grow the queue — but expiring one strands the
                    # adopted turn (it never speaks) and its orphaned pregen cache
                    # blocks every new prefetch until a mismatching enqueue clears
                    # it. Interactive (chat/PTT) items keep the TTL.
                    #
                    # Step 1 (direct_turn_preemption_20260803): `direct` items now
                    # enter this queue at ARRIVAL (routers/chat.py) instead of at
                    # the speech boundary, so their age is the real wait — a 71s
                    # agenda block would age one past 30s and the sweep would
                    # SILENTLY DISCARD a turn the API already receipted as
                    # "queued", with Kira never answering. Bound them by the
                    # contractual DIRECT_ANSWER_MAX_WAIT_SECONDS instead, so the
                    # documented bound and the mechanical one finally coincide.
                    # (This also closes the same latent hole on the pre-Step-1
                    # path: a second direct queued behind a long first answer
                    # could already TTL-die receipted.)
                    ttl = DIRECT_ANSWER_MAX_WAIT_SECONDS if source == "direct" else self._pq_ttl_seconds
                    if (
                        prio > 0
                        and not source.startswith("kira-agenda")
                        and (now - ts) > ttl
                    ):
                        self._log(f"Item expirado y omitido (TTL {ttl:.0f}s): {source}")
                        # Measure-first telemetry seam: record (never alter) chat expiries.
                        # Captured here, emitted below OUTSIDE _pq_lock.
                        if source == "chat":
                            expired_chat_infos.append({"age_sec": now - ts, "ttl_sec": ttl})
                        # F7 [v4]: record the expired item's (payload, source) so its
                        # orphaned pregen draft (if any) can be cleared below — a stale
                        # cache entry must not block the single slot for later prefetches.
                        expired_pregen_keys.append((payload, source))
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

            # F7 [v4]: clear any cached pregen draft matching an expired item, OUTSIDE
            # _pq_lock (never hold _pq_lock across _prefetch_lock — lock ordering).
            for exp_payload, exp_source in expired_pregen_keys:
                self._clear_prefetch_if_matches(exp_payload, exp_source)

            queue_empty = False
            accumulated = None
            with self._pq_lock:
                if widened_pop_armed and speech_busy and (
                    not self._priority_queue or self._priority_queue[0][0] != 0
                ):
                    # §5.2: only a priority-0 owner item may be SELECTED
                    # while speech plays. `_priority_queue` stays sorted
                    # (priority, ts) ascending — every mutation (enqueue(),
                    # the TTL filter above) preserves it — so the head IS the
                    # highest-priority item: checking index 0 here is the
                    # same as "no priority-0 item exists anywhere", and doing
                    # it INSIDE this lock (not a peek taken before it) is
                    # what makes the check atomic with the pop below.
                    # (`speech_busy` is the loop-top snapshot — see the I10
                    # note there.) Selection, not removal: on a pregen MISS
                    # the pop-time bundler below absorbs the contiguous
                    # OWNER-source prefix regardless of priority — a queued
                    # `direct` at priority 1 rides along DELIBERATELY: the
                    # press already justified the one D3 preemption and the
                    # burst is answered in one request (§4 step 10). Pinned by
                    # test_the_mid_playback_bundle_absorbs_a_queued_direct_question.
                    # Accumulation is deliberately NOT flushed on this
                    # return: an accumulated turn is never priority 0.
                    return
                if not self._priority_queue:
                    # No priority items — check accumulation buffer. The FLUSH
                    # stays under _pq_lock (it is the same critical section that
                    # observed the queue empty, and it preserves the existing
                    # _pq_lock -> _accum_lock edge); the TURN it produces does
                    # not — see below.
                    queue_empty = True
                    accumulated = self._flush_accumulation()
                else:
                    item = self._priority_queue.pop(0)
                    # Unpack tolerating the legacy 4-tuple, the 5-tuple (payload,
                    # source, history_text), the 6-tuple that adds submitted_at
                    # (Unit 4.1), and the current 7-tuple that adds
                    # submitted_under_provider (Unit 4.2, F12), all produced by
                    # enqueue().
                    priority, ts, payload, source, *rest = item
                    history_text = rest[0] if rest else None
                    submitted_at = rest[1] if len(rest) > 1 else None
                    submitted_under_provider = rest[2] if len(rest) > 2 else None
                    # C1 (refactor_core_api_20260802): build the stamp at unpack —
                    # enqueue()'s public signature and internal tuple storage stay
                    # unchanged, only the downstream threading collapses to one object.
                    stamp = (
                        TurnStamp(submitted_at=submitted_at, submitted_under_provider=submitted_under_provider)
                        if submitted_at is not None else None
                    )

            # Step 0 (interruptible_speech_architecture_20260804, design §2b):
            # the accumulated turn runs OUTSIDE _pq_lock. It used to run inside,
            # and so did its `finally` -> _complete_processing_cycle ->
            # _drain_pending_direct_into_priority_queue -> enqueue(), which opens
            # `with self._pq_lock` (:1754). _pq_lock is a plain Lock (:744), NOT
            # reentrant: the engine thread blocked on a lock it already held and
            # Kira went permanently silent with no error. The two-thread variant
            # was live too — the HTTP arrival drain (api/routers/chat.py:184-191)
            # takes _direct_drain_lock then wants _pq_lock while this thread held
            # _pq_lock and wanted _direct_drain_lock (ABBA), hanging
            # POST /api/chat/turn as well. Milder but constant: holding _pq_lock
            # across a whole generate-and-speak turn blocked EVERY enqueue() for
            # 15-100s. Early-return semantics are unchanged — an empty queue still
            # returns here whether or not there was anything to flush.
            if queue_empty:
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

            # Test-only pin (see _test_pop_boundary_hook in __init__): the item is
            # popped but _processing is still False here — this is exactly the
            # window a concurrent consumer (e.g. play_prefetched_agenda) could
            # observe as "clear to speak". No-op in production.
            if self._test_pop_boundary_hook is not None:
                self._test_pop_boundary_hook()

            # WU2 (design-fase2.md §2.2): pop-time pregen cache. A hit means this turn
            # was pregenerated during the prior turn's TTS — speak it on THIS worker
            # thread (no parallel speaker), skipping generation. A miss keeps the
            # existing generation path unchanged.
            cached = self._take_pregen_if_match(payload, source)
            # WU4 4a: "late" (below) already reports the boundary telemetry
            # for a wait-then-hit resolution — _speak_pregenerated must not
            # also log "used" for the SAME pop ("a single INFO log line...
            # per turn boundary").
            already_reported_boundary = False
            if cached is None:
                # AC3.2 (design-fase2.md §3 WU3): a pregen for THIS exact item may
                # still be generating. Wait a bounded time for it rather than
                # starting a second generation (never two generations per item).
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
            # Judge pass (step 4, BLOCKER): mirror of the §11 B3 idle gate in
            # _complete_processing_cycle. ObsRuntime maps "processing" to
            # AvatarState.THINKING + _stop_speaking_alt; a widened pop reaches
            # this line WHILE the router audibly plays, the D4 inner answer
            # job never emits speaking_start, and the resumed outer job
            # (job.started True) never re-emits — so the avatar would freeze
            # mouth-still on THINKING for the rest of the block, live on
            # stream. Unarmed this guard is a no-op: the legacy gate never
            # let this line run while `_speech_active`.
            if not self._speech_active:
                self.ui_callback("processing")
            # WU5 (design-fase2.md §3 WU5): an interactive turn spoken during an
            # interruption detour counts toward the return-skip budget (no-op
            # unless a frozen stash is pending; skips agenda sources). The
            # connector upgrade is triggered later, at speaking_start.
            self._note_detour_turn(source)
            try:
                if cached is not None:
                    self._speak_pregenerated(cached, already_reported_boundary=already_reported_boundary)
                else:
                    # T1(d) [v5]: the worker's PLAIN foreground fallback for an
                    # INTERACTIVE item — no cache hit, not even an in-flight
                    # pregen to wait for. This is the highest-dead-air
                    # boundary and used to be entirely invisible. Agenda's own
                    # "none" boundary is already reported by the driver before
                    # the item is ever enqueued (AgendaDriver._maybe_consume_
                    # prefetch), so this is gated to non-agenda sources to
                    # avoid a double report for the same turn.
                    if not source.startswith("kira-agenda"):
                        self._log(
                            f"Pregen boundary: draft=none source={source} gap_ms=-1 "
                            f"gen_ms=-1 speech_ms={self._speech_ms_for_boundary()}"
                        )
                    # interruptible_speech_architecture_20260804 §4 step 10 —
                    # THE BUNDLER. Reached only on a pregen MISS, which is Rule
                    # 1: the engine was about to pay for a generation anyway, so
                    # absorbing the owner questions queued behind this one costs
                    # nothing extra and buys back N-1 spoken answers of ~108s
                    # each. A draft that exists never gets here (it was spoken
                    # above), so no paid GPU work is ever discarded.
                    #
                    # Frame-local by construction: the bundle is three locals,
                    # created here and consumed by _ejecutar_inferencia below in
                    # the same iteration. No window, no TTL, no second store —
                    # which is precisely why a question can be neither lost nor
                    # answered twice (§3.4, §7). Do NOT promote it to an
                    # instance attribute; that reintroduces every pathology
                    # _accumulation_buffer already has.
                    #
                    # N == 1 takes none of this: `taken` is empty, nothing is
                    # reassigned, and the call below is byte-identical to today.
                    taken: list = []
                    if source in _OWNER_QUESTION_SOURCES:
                        taken = self._take_owner_bundle_prefix(item)
                        if taken:
                            members = [item] + taken
                            payload, history_text, stamp = self._compose_owner_bundle(members)
                            source = OWNER_BUNDLE_SOURCE
                            # An absorbed member's orphaned draft must not keep
                            # holding the single pregen slot. OUTSIDE _pq_lock,
                            # mirroring the TTL sweep's own cleanup above. A
                            # no-op in practice — the slot only ever drafts for
                            # the head — but a stale entry blocks every later
                            # prefetch until an unrelated enqueue clears it.
                            for member in taken:
                                self._clear_prefetch_if_matches(member[2], member[3])
                            self._log_owner_bundle(members, payload)
                    # C1 (refactor_core_api_20260802): conditional forward (see
                    # _dispatch_command) — a stubbed _ejecutar_inferencia in
                    # existing tests never sees the new kwarg unless a real
                    # stamp was queued. `bundle_followers` rides the SAME rule:
                    # it is forwarded only when a bundle actually formed, so a
                    # non-bundled turn's call is byte-identical to before.
                    #
                    # It is forwarded at all because the failure is detected
                    # THERE (a falsy `dialogo`) while the members only exist
                    # HERE — and _ejecutar_inferencia's `elif` chain is already
                    # the house hook for a non-committing return (the agenda
                    # recovery next to it). See _requeue_owner_bundle_followers.
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
                self._complete_processing_cycle(process_queue=False)

    def _complete_processing_cycle(self, *, process_queue: bool = True, emit_idle: bool = True) -> None:
        with self._lock:
            self._processing = False
            self._current_processing_source = None
        # §11 B3: this cycle no longer ends when the speech does — under the
        # router it ends the moment `submit` returns. ObsRuntime maps 'idle' to
        # AvatarState.IDLE + _stop_speaking_alt (the mouth stops mid-sentence,
        # visibly) and CTK re-enables input, so it must not fire mid-speech.
        # The router emits it at job completion instead; a cycle that never
        # spoke still emits it here, exactly as today.
        # `emit_idle=False` is the router's own wake path — it already emitted.
        if emit_idle and not self._speech_active:
            self.ui_callback("idle")
        self._drain_control_commands()
        # Unit 4.2 (runtime_findings_batch_20260731, D3b): see the method's own
        # docstring for the root cause this closes.
        self._drain_pending_direct_into_priority_queue()
        self._check_pending_model_switch()
        if process_queue:
            self._process_priority_queue()

    def _drain_control_commands(self) -> None:
        """Apply a contiguous run of whitelisted control commands from the
        FRONT of command_queue at a turn boundary.

        The engine is single-threaded: run() only reads command_queue when it is
        NOT inside a dispatch, so control commands posted during a continuous
        priority-queue run (the iterative _process_priority_queue drain loop) sit
        unread until the queue empties (the command-starvation bug). This boundary
        is a true idle point (_processing and _speaking both False), so a leading
        run of whitelisted commands is applied now, before the next turn dispatches.

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

        §11 B4: "true idle point" is now enforced, not assumed. Under the
        router this runs while speech may still be pending or playing, and
        `_DRAIN_SAFE_COMMANDS` includes `set_piper_voice` (-> `_piper.reload`)
        and `set_tts_speed` (-> `set_length_scale`) on the very Piper object
        the producer is mid-utterance on — the producer snapshots
        `tts_local_only`/`edge_rate` at start precisely because drains were
        boundary-only. Deferred drains are picked up by the router's wake
        sentinel at the real boundary (B5). The PRIMARY consume path defers
        here too: `_dispatch_command` parks drain-safe verbs while
        `_speech_active` (closure finding 2026-08-05) and this drain applies
        them FIRST, ahead of the queue front-run.
        """
        if self._speech_active:
            return
        applied = 0
        # §11 B4 primary path: verbs run() consumed mid-utterance and
        # `_dispatch_command` parked. FIFO holds: anything still sitting in
        # command_queue arrived AFTER these were popped, so they apply first.
        # Bounded snapshot — an item re-deferred by a submit racing this
        # flush goes to the back and the loop still exits.
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
            # F3: tolerant unpack, mirroring _consume_command. The queue item has
            # grown twice already (history_text, then source); a strict 2-unpack
            # here raises ValueError inside _complete_processing_cycle, which
            # run() does NOT guard — that kills the worker thread for good and
            # Kira goes permanently mute. Drain-safe commands are plain setters
            # that read neither extra slot, so discarding them is correct.
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

        Root cause (F5, and the F12 follow-up this closes): WU2's
        consume-at-event (the speaking_end boundary emission — the
        SPEECHROUTER thread when the router is armed, `_hablar`'s tail on the
        legacy path; see the comment on `AgendaDriver._prefetch` in
        `api/agenda_driver.py`) adopts the NEXT agenda block straight into
        `_priority_queue` SYNCHRONOUSLY before this boundary ever runs — the
        router preserves the ordering by enqueueing its wake sentinel AFTER
        the boundary event. As long as that keeps
        succeeding (the 2026-07-30 run pregenerated 87 of 126 blocks),
        `_process_priority_queue`'s drain loop never finds `_priority_queue`
        empty and never returns to `run()`'s `command_queue.get()` -- so a
        `source=direct` command sitting in `command_queue` (put there by
        `Dispatcher.dispatch`, api/dispatch.py) is never even looked at for
        as long as agenda content keeps flowing. `_priority_queue`'s own
        priority sort (0=PTT, 1=chat/direct, 2=agenda; see `enqueue()`) is
        completely correct once an item actually reaches it -- the 2026-07-30
        defect (four direct questions waiting 13.8-29.1 minutes) was a direct
        item that never got there at all, not a sort-order bug.

        Fix: peek `command_queue`'s FRONT (mirrors `_drain_control_commands`'s
        mutex-peek, called immediately before this at every boundary) and, if
        it is a `process_context` command whose source is an owner question
        (`_OWNER_QUESTION_SOURCES` -- "direct" or "ptt"), pop it and
        `enqueue()` it into `_priority_queue` at ITS documented priority --
        the SAME conversion `_dispatch_command`'s busy branch already does for
        a command read the normal way. Once queued, the priority sort
        guarantees it is served ahead of any further agenda action
        (priority=2), even if an agenda block was already re-queued earlier
        in this same boundary (insertion order does not matter, only the
        sort key does).

        Scoped strictly to an EXPLICIT owner-question source tag (D3, widened
        by interruptible_speech_architecture_20260804 Step 1) -- the 4th tuple
        element must literally be "direct" or "ptt", never the tolerant-unpack
        default `_consume_command` falls back to for a bare/legacy tuple. A
        plain `("process_context", payload)` 2-tuple (the CTK/legacy internal
        dispatch shape, still used by a few call sites/tests) is NOT an API
        turn tagged by `Dispatcher.dispatch` and must stay deferred exactly as
        before (WU1's command-starvation fix, tests/test_command_drain.py) --
        only a real `/api/chat/turn` turn (which always dispatches an
        explicit 4+ tuple) qualifies. Stops at the first non-matching front
        item so nothing else is reordered -- notably, a NON-owner item (e.g.
        `chat`) at the front still stops the scan, same as before. Bounded by
        a qsize() snapshot, mirroring `_drain_control_commands`, so a
        sustained stream can never loop indefinitely.

        Step 1 (direct_turn_preemption_20260803): this is now called from TWO
        threads — the engine boundary (unchanged, still the starvation backstop
        for a non-owner-question command sitting ahead in FIFO) and the HTTP
        thread right after an accepted /api/chat/turn dispatch, so the turn
        reaches `_priority_queue` at ARRIVAL and `enqueue()`'s own
        interactive-pregen trigger can see it as the head while the agenda is
        still speaking. `_direct_drain_lock` makes each pop->enqueue pair
        atomic across those callers: the pop (command_queue.mutex) and the
        enqueue (_pq_lock) are separate critical sections, so two
        unserialized drains could otherwise pop directs A then B and enqueue
        them B then A, inverting FIFO.

        Step 1 (interruptible_speech_architecture_20260804): widened from
        "direct" only to `_OWNER_QUESTION_SOURCES` ("direct" + "ptt"), and the
        priority is now the item's OWN documented one (0 for ptt, 1 for
        direct) instead of a hardcoded 1. PTT DOES reach this path now, in the
        deferred case (a PTT command sitting in `command_queue` because the
        arrival-time hook in `api/ptt_session.py` missed it or hasn't run
        yet) -- the earlier claim that "PTT never reaches this path" was true
        only for the CUT case (WU5's own interrupt mechanism, untouched here
        and still the only thing that can cut mid-speech). A leading `ptt`
        item no longer blocks a `direct` sitting right behind it: both are
        owner questions, so the scan keeps going.
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
                    self.command_queue.queue.popleft()
                # C1 (refactor_core_api_20260802): enqueue()'s own signature keeps the
                # two separate kwargs (public API, unchanged) — both default to None,
                # so passing them unconditionally is byte-identical to the old
                # branch-per-presence idiom.
                self.enqueue(
                    payload, priority=0 if source == "ptt" else 1, source=source, history_text=history_text,
                    submitted_at=submitted_at, submitted_under_provider=submitted_under_provider,
                )
                self._log(f"Boundary drain: turno {source} movido a cola prioritaria (D3b).", level="debug")

    def _check_ollama_service(self, *, notify_unavailable: bool = True):
        if not self._is_local:
            # Cloud provider active: no local Ollama service to probe or warm.
            # Cloud reachability is the health monitor's concern (Phase 5).
            self.is_ready = True
            return True
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
        # D1 (model_switch_memory_continuity_20260723): no historial.clear()
        # here — owner-chosen SEAMLESS continuity, conversation carries over
        # verbatim across a model switch.

        if not self._prepare_model(new_model):
            raise RuntimeError("target_model_unavailable")

        if self._is_local and previous_model != new_model:
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
            if self._is_local and previous_model != target_model:
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
        if self._processing or self._speech_active:
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
        if not self._is_local:
            # Cloud provider active: no local model to warm. Report "ready" so
            # switch/tier machinery stays non-crashing; the cloud request path
            # never depends on a warmed local model.
            return True
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
        if not self._is_local:
            # F6: cloud provider active — no local ollama.pull. Surface the
            # refusal so the operator gets feedback (download_error, the same
            # signal the failure path emits) instead of a silent no-op.
            self._log(
                "Descarga de modelos no disponible con un proveedor cloud activo.",
                level="warning",
            )
            self.ui_callback("download_error")
            return
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

            # D2 (model_switch_memory_continuity_20260723): no historial.clear()
            # here either — same "model changed mid-session" event family as
            # switch_model, same seamless-continuity contract.
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

    def _guardrail_fallback_line(self, source: str, reason: str = "") -> str:
        """Return a canned spoken line for guard-blocked responses.

        Agenda sources return "" — the agenda state machine already handles
        rejected outputs gracefully and a canned line would break topic flow.

        D4 (memoria_quality_20260717): the memory-flavored last bundle line is
        reserved for turns blocked by the R9 AI-self-ID rule — the only guard
        reason a "my memories got tangled" deflection actually fits. Every other
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
        lines = i18n_active.guardrail_fallback_lines()
        # R9 self-ID block -> the memory-flavored line (bundle index 4), when the
        # bundle actually ships it. Does not advance the generic rotation.
        if "no_ai_self_identification" in reason and len(lines) >= 5:
            return lines[4]
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
    ) -> str:
        """ONE extra generation after an output_guard block (owner decision
        "afinar + reintento", guardrail_tuning_20260724): same prompt +
        `_GUARDRAIL_RETRY_NUDGE` appended as a trailing system message.

        A single standalone call to `_ollama_chat_with_watchdog` -- NEVER
        wrapped in the caller's `max_intentos` transport-retry loop, so it
        never consumes that separate budget. Posture (`provider_cfg`/
        `is_local`) is the caller's F2 entry snapshot, never re-read live.

        Returns the stripped content, or "" on any failure (transport error,
        timeout, empty response) so the caller falls through to the existing
        canned-line fallback unchanged.
        """
        retry_messages = messages + [
            {"role": "system", "content": _GUARDRAIL_RETRY_NUDGE}
        ]
        with self._lock:
            self._llm_generating = True
        try:
            respuesta = self._ollama_chat_with_watchdog(
                timeout=chat_timeout,
                model=request_model,
                messages=retry_messages,
                keep_alive=LLM_KEEP_ALIVE,
                options=opciones_llm,
                provider_cfg=provider_cfg,
                is_local=is_local,
            )
            msg_obj = respuesta.get('message', {})
            if isinstance(msg_obj, dict):
                content = msg_obj.get('content', '')
            else:
                content = getattr(msg_obj, 'content', '')
            return (content or "").strip().strip('\x00\ufeff')
        except Exception:
            logger.warning("Guardrail retry generation failed", exc_info=True)
            return ""
        finally:
            with self._lock:
                self._llm_generating = False

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
        watchdog_timeout: Optional[float] = None,
    ) -> str:
        request_model = self.current_model
        # F2 posture snapshot (multi_provider_llm_20260723): read the provider
        # config AND the runtime fallback flag ONCE here, collapse them into the
        # EFFECTIVE posture `is_local`, and thread that bool through this whole
        # generation + dispatch. The config dict is swapped wholesale by
        # set_provider_config (never mutated in place) and the flag is read
        # exactly once at this snapshot instant, so neither a racing PUT nor a
        # mid-turn `_cloud_fallback_active` flip can tear this generation between
        # local/cloud — it is pinned to `provider_cfg`/`is_local`; only the NEXT
        # call sees the change. INVARIANT: every mid-generation posture read
        # below uses this `is_local` bool, never the live `self._is_local`
        # property and never a re-derivation from `provider_cfg`.
        provider_cfg = self._provider_config
        is_local = self._cfg_is_local(provider_cfg) or self._cloud_fallback_active
        self._log(f"Analizando contexto con {request_model}...")
        try:
            setup = self._build_generation_request(
                contexto,
                source,
                is_local=is_local,
                provider_cfg=provider_cfg,
                request_model=request_model,
                watchdog_timeout=watchdog_timeout,
            )
            outcome = self._cloud_attempt_loop(
                setup,
                source=source,
                commit_history=commit_history,
                is_local=is_local,
                provider_cfg=provider_cfg,
                request_model=request_model,
                watchdog_timeout=watchdog_timeout,
            )
            if outcome.early_return is not None:
                return outcome.early_return
            return self._finalize_generation(
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
            )

        except Exception as e:
            self._log(f"ERROR Ollama: {e}", level="error")
            logger.exception("Error en inferencia LLM")
            if commit_history:
                self._invalidate_pregen_epoch()
            return ""

    def _build_generation_request(
        self,
        contexto,
        source: str,
        *,
        is_local: bool,
        provider_cfg: dict,
        request_model: str,
        watchdog_timeout: Optional[float],
    ) -> "_GenerationSetup":
        """Phase 1 of _generar_dialogo (refactor_core_api_20260802 B7):
        build the message list + sampling options + posture-aware timeout.
        Verbatim body moved from the original method (comments included) --
        no behavior change, only a name and an explicit return.
        """
        messages = []

        # Personalization block (kira_personalization_onboarding_20260705,
        # design §2; memoria_recall_20260718 W1): built ONCE PER REQUEST and
        # emitted at the SYSTEM position — mirroring set_profile's persona
        # (self.system_prompt, llm_engine.py:715-737). This is a stable
        # per-request copy at the system position, NOT literally once-per-
        # session: ollama.chat is stateless, so identity must be re-supplied
        # every request. It never enters historial or memoria capture
        # (_commit_history stores raw contexto, not this system content).
        # Same _PERSONALIZATION_INJECT_SOURCES gate as before. Fail-open: a
        # raising build must never break a turn.
        personalization_block = ""
        if source in _PERSONALIZATION_INJECT_SOURCES and PERSONALIZATION_ENABLED:
            try:
                personalization_block = personalization.build_injection_block(
                    self._sanitize_history_context
                )
            except Exception:
                personalization_block = ""
        # grounding_authority_temporal_humility: the grounding-authority +
        # temporal-humility rules ride at the SYSTEM position on EVERY
        # generation, appended by the engine rather than authored into
        # llm.system_prompt.
        #
        # WHY HERE and not in the persona slot: `set_profile` replaces
        # self.system_prompt wholesale with the profile's own prompt
        # (`payload.get("prompt", ...)` in the set_profile handler). All six shipped
        # profiles carry a full prompt of their own, and profiles are
        # persisted to the user's PROFILES_FILE on first run — so a rule
        # written into the locale persona (or into default_profiles.json)
        # would be dead text for every existing install. This single site is
        # the funnel all three _generar_dialogo callers route through.
        #
        # Unconditional by source: the "that doesn't exist" failure is not
        # direct-only (chat and agenda turns assert facts too), unlike the
        # <editorial_context> block below which stays gated on source.
        #
        # Position: persona -> rules -> personalization. The rules are a
        # constant, so keeping them in the stable prefix ahead of the
        # per-session personalization block preserves KV-cache reuse.
        # Fail-open by construction: grounding_rules() is _slot-backed and
        # cannot raise; "" simply appends nothing.
        grounding_block = i18n_active.grounding_rules()
        system_parts = [self.system_prompt]
        if grounding_block:
            system_parts.append(grounding_block)
        if personalization_block:
            system_parts.append(personalization_block)
        system_content = "\n\n".join(system_parts)

        if self.use_system_role:
            messages.append({'role': 'system', 'content': system_content})

        # Take a consistent snapshot of historial and (for host-turn paths) the
        # digest block under _history_lock so that a concurrent _commit_history
        # call from the agenda speaker daemon cannot mutate the deque while we
        # are iterating it (RuntimeError: deque mutated during iteration).
        # The lock is held ONLY for the fast snapshot + build_block reads;
        # it is released before any I/O or the Ollama call.
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

        # Editorial host-turn enrichment: inject matching ARMED card context for
        # host queries, typed OR spoken (F1 — the operator arms a card and then
        # asks about it by voice just as often as by keyboard).
        # NON-CONSUMING — card stays ARMED for the agenda path.
        # Never inject for chat/aggregator-driven sources.
        editorial_block = ""
        if source in _EDITORIAL_INJECT_SOURCES:
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

        # D3 — digest injection: only for host-turn prompts (typed or spoken),
        # never chat/agenda. digest_block was already computed under
        # _history_lock above. E3b: Wrap in explicit read-only delimiter so the
        # LLM cannot mistake ledger lines for instructions (structural
        # isolation, language-agnostic).
        if source in _DIGEST_INJECT_SOURCES:
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

        # W1 (memoria_recall_20260718): the personalization <perfil_streamer>
        # block no longer prepends the user turn here — it was built above and
        # folded into `system_content` at the SYSTEM position. Only the
        # memorias/digest/editorial context rides in `enriched` now.
        if self.use_system_role:
            messages.append({'role': 'user', 'content': enriched})
        else:
            prompt_completo = f"{system_content}\n\n[{i18n_active.user_message_label()}]: {enriched}"
            messages.append({'role': 'user', 'content': prompt_completo})

        # Layer 1+2: discover the model's native context window (cached, free
        # after first call — also covers the name-heuristic short-circuit gap)
        # but budget against OpenCohost's effective runtime cap so large
        # native windows do not disable prompt eviction or over-allocate KV.
        if is_local:
            self._discover_model_ctx(request_model)
            _native_ctx = self._model_ctx_limit.get(request_model, CTX_FALLBACK_DEFAULT)
            _effective_ctx = self._resolve_effective_ctx_limit(request_model, _native_ctx)
        else:
            # Cloud: no ollama.show telemetry (prompt_eval_count/eval_duration
            # are absent from OpenAI-compatible responses). Apply the
            # provider-aware budget proactively (design 'Cloud context budget'),
            # replacing the reactive trim.
            _native_ctx = CLOUD_CTX_BUDGET
            _effective_ctx = CLOUD_CTX_BUDGET
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

        # Cloud item 3 (2026-07-24 incident): the LOCAL 768-token cap
        # (LLM_MAX_TOKENS) was starving cloud/reasoning models. Cloud uses
        # the high CLOUD_MAX_TOKENS ceiling instead -- gated on the
        # snapshotted `is_local`, never a live re-read (F2 invariant above).
        opciones_llm = {
            'temperature': LLM_TEMPERATURE,
            'top_p': LLM_TOP_P,
            'num_predict': LLM_MAX_TOKENS if is_local else CLOUD_MAX_TOKENS,
            'num_ctx': _effective_ctx,
        }

        if is_local and "gemma" in request_model.lower():
            opciones_llm.pop('num_ctx', None)
            opciones_llm['temperature'] = 0.7

        # Reasoning-capability discovery is an ollama.show probe (local-only);
        # for cloud, num_predict maps to max_tokens and the provider owns any
        # reasoning-token behavior. gemma temperature override is local-only too.
        if is_local and self._resolve_reasoning_classification(request_model):
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
        # R3: a bounded connector-upgrade call passes its own short watchdog
        # timeout; every other caller keeps the resolved chat timeout.
        chat_timeout = (
            watchdog_timeout
            if watchdog_timeout is not None
            else self._resolve_chat_watchdog_timeout(
                request_model, provider_cfg=provider_cfg, is_local=is_local
            )
        )

        return _GenerationSetup(
            messages=messages,
            opciones_llm=opciones_llm,
            chat_timeout=chat_timeout,
            max_intentos=max_intentos,
            start_llm=start_llm,
            native_ctx=_native_ctx,
            effective_ctx=_effective_ctx,
            ctx_evicted=_ctx_evicted,
            editorial_block=editorial_block,
            history_snapshot=history_snapshot,
        )

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
    ) -> "_GenerationAttemptOutcome":
        """Phase 2 of _generar_dialogo (refactor_core_api_20260802 B7): the
        max_intentos retry loop, including the cloud transport-failure
        classification + rate-limited Retry-After retry branch, moved as ONE
        piece with its comments intact. Every early `return ""` here becomes
        `early_return=""` on the outcome -- the orchestrator propagates it
        identically via `if outcome.early_return is not None: return ...`.
        """
        messages = setup.messages
        opciones_llm = setup.opciones_llm
        chat_timeout = setup.chat_timeout
        max_intentos = setup.max_intentos
        _effective_ctx = setup.effective_ctx
        raw_content = ""
        respuesta = None

        for intento in range(max_intentos):
            # WU3 (design-fase2.md §2.3): mark Ollama busy tightly around the
            # actual generation call, cleared in finally on every exit path.
            with self._lock:
                self._llm_generating = True
            try:
                respuesta = self._ollama_chat_with_watchdog(
                    timeout=chat_timeout,
                    model=request_model,
                    messages=messages,
                    keep_alive=LLM_KEEP_ALIVE,
                    options=opciones_llm,
                    provider_cfg=provider_cfg,
                    is_local=is_local,
                )
            except Exception as e:
                if self._is_watchdog_timeout_error(e):
                    # R3: a bounded connector-upgrade call (watchdog_timeout set)
                    # abandons SILENTLY on timeout — it is cosmetic, so it must
                    # never trigger the heavyweight stall recovery (model rollback
                    # / UI signal) a real turn's timeout does. Just return "" so
                    # the pool floor stands.
                    if watchdog_timeout is None:
                        # A cloud stall must NOT roll back a local model
                        # (spec B): gate the heavyweight model-rollback
                        # recovery on `is_local`. Cloud instead routes to
                        # `_handle_cloud_failure` (Phase 4: fallback state
                        # machine); the max_intentos retry + existing
                        # failure contract still apply either way.
                        if is_local:
                            self._recover_from_stalled_inference(
                                request_model=request_model,
                                source=source,
                                timeout=chat_timeout,
                            )
                        else:
                            # A watchdog timeout carries no HTTP status/
                            # headers to classify from (it never got a
                            # response) -- `transient` is the correct
                            # class per classify_cloud_error's own rule
                            # ("anything without a status_code ... is
                            # transient"), and drives exponential-backoff
                            # auto-return (unit 2.2).
                            self._handle_cloud_failure(
                                source, failure_class=cloud_llm_client.CLOUD_ERROR_TRANSIENT
                            )
                        if commit_history:
                            self._invalidate_pregen_epoch()
                    return _GenerationAttemptOutcome(early_return="")
                if not self._is_ollama_transport_error(e):
                    raise
                # F7b: attribute a CLOUD failure to the cloud profile model +
                # provider id (the local current_model tag would make a cloud
                # 401 look like a local fault). The LOCAL branch stays
                # byte-identical (no "provider" key) so existing exact-match
                # assertions keep passing. Full MODEL_TRACE attribution is
                # deferred (residual).
                _cloud_class = None
                if is_local:
                    self._last_llm_failure = {
                        "model": self.current_model,
                        "source": source,
                        "attempt": intento + 1,
                        "reason": type(e).__name__,
                        "message": str(e),
                    }
                else:
                    _fail_profile = self._cfg_active_profile(provider_cfg) or {}
                    # F2 (runtime_findings_batch_20260731 unit 1.1): classify
                    # from status_code/headers already carried on the
                    # exception -- never from `str(e)`/body, which would mean
                    # parsing a guessed provider-specific shape.
                    _cloud_class = cloud_llm_client.classify_cloud_error(e)
                    self._last_cloud_failure_class = _cloud_class
                    self._last_llm_failure = {
                        "model": _fail_profile.get("model") or self.current_model,
                        "provider": provider_cfg.get("active_provider"),
                        "source": source,
                        "attempt": intento + 1,
                        "reason": type(e).__name__,
                        "message": str(e),
                        "clase": _cloud_class,
                    }
                _clase_suffix = f" clase={_cloud_class}" if _cloud_class else ""
                self._log(
                    f"ERROR Ollama chat ({type(e).__name__}) intento {intento+1}/{max_intentos}{_clase_suffix}: {e}",
                    level="error",
                )
                logger.warning(
                    "Ollama chat transport failure: model=%s source=%s attempt=%s/%s clase=%s",
                    request_model,
                    source,
                    intento + 1,
                    max_intentos,
                    _cloud_class or "n/a",
                    exc_info=True,
                )
                # Unit 2.1 (runtime_findings_batch_20260731): `rate_limited`
                # is the ONE class that spends the existing max_intentos
                # budget instead of exiting immediately -- honour a bounded
                # Retry-After when the budget still has an attempt left.
                # `bad_key` / `ambiguous_429` / `transient` never retry here
                # (the honest completion of "do not guess a provider-
                # specific table": an unclassifiable or non-timing 429 gets
                # conservative treatment, not a guessed wait).
                if (
                    not is_local
                    and _cloud_class == cloud_llm_client.CLOUD_ERROR_RATE_LIMITED
                    and intento < max_intentos - 1
                ):
                    _retry_after = cloud_llm_client.parse_retry_after_seconds(
                        getattr(e, "headers", None) or {}
                    )
                    _wait_seconds = (
                        _retry_after if _retry_after is not None
                        else CLOUD_RATE_LIMIT_RETRY_DEFAULT_SECONDS
                    )
                    if _wait_seconds <= CLOUD_RATE_LIMIT_RETRY_MAX_SECONDS:
                        self._log(
                            f"rate_limited: retrying in {_wait_seconds}s "
                            f"(intento {intento+1}/{max_intentos}).",
                            level="warning",
                        )
                        # Dead-air bound: this sleep runs BETWEEN attempts,
                        # after `_ollama_chat_with_watchdog` has already
                        # raised -- outside `_call_with_watchdog`'s own
                        # worker thread, so the watchdog cannot kill it.
                        time.sleep(_wait_seconds)
                        continue
                    self._log(
                        f"rate_limited: Retry-After={_wait_seconds}s exceeds "
                        f"{CLOUD_RATE_LIMIT_RETRY_MAX_SECONDS}s bound; not retrying in-turn.",
                        level="warning",
                    )
                # `bad_key` never retries; surface a "check your key" banner
                # ONCE per failure event (latch resets alongside
                # `_last_cloud_failure_class` on the next success, above).
                if _cloud_class == cloud_llm_client.CLOUD_ERROR_BAD_KEY and not self._cloud_bad_key_notified:
                    self._cloud_bad_key_notified = True
                    self.ui_callback("cloud_bad_key")
                # F1 (multi_provider_llm_20260723): a CLOUD transport error
                # exits the attempt loop here when not retried above -- it
                # engages the SAME fallback state machine as a cloud timeout
                # (spec C: fallback on "cloud timeout OR a non-2xx/connection
                # error"). The LOCAL transport path stays byte-identical
                # (spec B: a local fault never routes to cloud fallback /
                # never rolls back here).
                if not is_local:
                    # Unit 2.2: re-derive Retry-After directly from `e` in
                    # scope rather than the loop-local `_retry_after` --
                    # that variable is only assigned inside the in-turn
                    # retry branch above and can carry a stale value from
                    # an EARLIER attempt this same call when this attempt
                    # skipped that branch (e.g. rate_limited on the final,
                    # budget-exhausted attempt).
                    _probe_retry_after = (
                        cloud_llm_client.parse_retry_after_seconds(getattr(e, "headers", None) or {})
                        if _cloud_class == cloud_llm_client.CLOUD_ERROR_RATE_LIMITED
                        else None
                    )
                    self._handle_cloud_failure(
                        source,
                        failure_class=_cloud_class or cloud_llm_client.CLOUD_ERROR_TRANSIENT,
                        retry_after_seconds=_probe_retry_after,
                    )
                if commit_history:
                    self._invalidate_pregen_epoch()
                return _GenerationAttemptOutcome(early_return="")
            finally:
                with self._lock:
                    self._llm_generating = False

            msg_obj = respuesta.get('message', {})
            if isinstance(msg_obj, dict):
                raw_content = msg_obj.get('content', '')
                thinking = msg_obj.get('thinking', '')
            else:
                raw_content = getattr(msg_obj, 'content', '')
                thinking = getattr(msg_obj, 'thinking', '')

            # Cloud usage.* is recorded to logs only (spec E) — the local
            # ctx_utilization block below reads Ollama-only telemetry
            # (prompt_eval_count/eval_duration) absent from cloud responses.
            if not is_local and isinstance(respuesta, dict):
                _usage = respuesta.get('usage')
                if _usage:
                    logger.info("cloud_llm_usage: %s source=%s", _usage, source)

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
            if is_local and intento == 0 and context_budget.is_overflow_signal(
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
                if is_local:
                    # F3: never write the LOCAL reasoning cache from a CLOUD
                    # response. On cloud request_model is the local
                    # current_model tag, so caching True here would uncap the
                    # local model for the rest of the session once we return
                    # to local. The uncapped cloud retry itself may stay.
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

        return _GenerationAttemptOutcome(raw_content=raw_content, respuesta=respuesta)

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
    ) -> str:
        """Phase 3 of _generar_dialogo (refactor_core_api_20260802 B7):
        post-process the raw completion (ctx telemetry, MODEL_TRACE, agenda
        sanitize, clause sanitizer, guardrail + retry, chat repetition guard,
        agenda acceptance) and commit/return. The ctx-telemetry snapshot
        block keeps reading only THIS call's own locals/params (never a
        shared attribute) -- same documented design as before the split.
        """
        raw_content = outcome.raw_content
        respuesta = outcome.respuesta
        messages = setup.messages
        opciones_llm = setup.opciones_llm
        chat_timeout = setup.chat_timeout
        start_llm = setup.start_llm
        _native_ctx = setup.native_ctx
        _effective_ctx = setup.effective_ctx
        _ctx_evicted = setup.ctx_evicted
        editorial_block = setup.editorial_block
        history_snapshot = setup.history_snapshot

        dialogo = raw_content.strip().strip('\x00\ufeff')
        elapsed = time.time() - start_llm
        # T2(a) [v5]: last COMPLETED generation's duration (foreground or
        # pregen, this is the shared _generar_dialogo body both use),
        # feeding the adaptive retry gate (_pregen_retry_gate_seconds).
        # Single Ollama runner -> at most one writer at a time; plain
        # assignment is safe (atomic under the GIL).
        self._pregen_last_gen_duration = elapsed

        # Editorial direct-mode USED trigger (D2): commit the pending
        # injection exactly once, only when this turn actually injected a
        # card block AND produced a non-empty dialogo. Single engine worker
        # thread is the only caller (no lock needed). Fail-open \u2014 a recorder
        # error must never break a turn.
        #
        # NO SOURCE GATE, deliberately-but-unratified: since F1 widened
        # _EDITORIAL_INJECT_SOURCES to direct+ptt, a VOICE turn also consumes
        # a single_use card. Pinned by
        # test_editorial_direct_context.py::test_ptt_turn_consumes_single_use_armed_card
        # \u2014 read that test before adding a gate here; it is an owner decision,
        # not a bug.
        if editorial_block and dialogo and self.direct_editorial_usage_recorder is not None:
            try:
                self.direct_editorial_usage_recorder()
            except Exception:
                logger.warning("editorial direct usage recorder failed", exc_info=True)

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
            # Unit 2.3 (runtime_findings_batch_20260731 F10): stash the SAME
            # numbers the line above just logged, per request, in the bounded
            # ring -- built entirely from this call's own locals (never a
            # shared attribute), so it cannot diverge from the log line for
            # this turn and cannot race a concurrent pregen worker's own call.
            # `_ctx_evicted` is the ctx_budget_gate value from THIS call's own
            # `_GenerationSetup` (set once in `_build_generation_request`,
            # carried per call, never stored on `self`) -- it cannot be
            # another turn's value.
            # `_ctx_provider` mirrors trace_provider's formula below without
            # depending on it (that variable is computed later in this
            # method and must not gate whether this snapshot is built).
            _ctx_provider = "local" if is_local else (provider_cfg.get("active_provider") or "local")
            _ctx_snapshot = {
                "request_id": str(uuid.uuid4()),
                "timestamp": time.time(),
                "source": source,
                "provider": _ctx_provider,
                "model": request_model,
                "native_ctx": _native_ctx,
                "effective_ctx": _effective_ctx,
                "ratio": _util,
                "prompt_eval_count": _pec_final,
                "prefill_ms": _prefill_ms,
                "decode_ms": _decode_ms,
                "eval_count": _ec_final,
                "evicted_pairs": _ctx_evicted,
            }
            # getattr-guarded: several existing tests build a MotorVocalIA
            # via __new__ (bypassing __init__) and only set the attributes
            # their scenario touches -- mirrors the agenda_output_transformer
            # guard above.
            _ctx_ring = getattr(self, "_ctx_telemetry_ring", None)
            if _ctx_ring is not None:
                _ctx_ring.append(_ctx_snapshot)
            if _util >= CTX_PRESSURE_HIGH_THRESHOLD:
                logger.warning(
                    "ctx_pressure_high: utilization=%.1f%% model=%s source=%s",
                    _util * 100, request_model, source,
                )
                _on_ctx_pressure_high = getattr(self, "on_ctx_pressure_high", None)
                if _on_ctx_pressure_high is not None:
                    try:
                        _on_ctx_pressure_high({
                            "ratio": _util,
                            "effective_ctx": _effective_ctx,
                            "native_ctx": _native_ctx,
                            "evicted_pairs": _ctx_evicted,
                        })
                    except Exception:
                        logger.exception("on_ctx_pressure_high callback failed")
                self.ui_callback("ctx_pressure_high")

        # MODEL_TRACE: audit which model was used for this generation
        generation_model = request_model
        desired = self._desired_model
        active = self.current_model
        loaded = self._loaded_model or "unknown"
        # F4 (runtime_findings_batch_20260731 1.3): provider/transport for
        # THIS generation, derived from the entry-snapshot `is_local` (never
        # a fresh live read — same pinning rule as the rest of the
        # generation, threaded in from `_generar_dialogo`'s entry).
        # `fallback_active` here means "local ONLY because of the runtime
        # fallback flag, not because cfg was genuinely local" — derived from
        # values already in scope, no extra snapshot variable needed.
        trace_provider = "local" if is_local else (provider_cfg.get("active_provider") or "local")
        trace_transport = "local" if is_local else "cloud"
        trace_fallback_active = is_local and not self._cfg_is_local(provider_cfg)
        trace_msg = (
            f"[MODEL_TRACE] desired={desired} active={active} "
            f"loaded={loaded} generation={generation_model} "
            f"profile={self._current_profile_name} source={source} "
            f"provider={trace_provider} transport={trace_transport} "
            f"fallback_active={trace_fallback_active}"
        )
        # Root cause confirmed against logs/opencohost_20260730_162650.log:
        # `_prepare_model` short-circuits without ever setting `_loaded_model`
        # while cloud is the effective transport (`:2683-2687` — see
        # `_judge_model`'s docstring for the same fact), so `loaded` reads
        # "unknown" on EVERY cloud-by-design turn even though nothing is
        # wrong — that is the entire cause of the 29/29 false positives, not
        # `generation` carrying the cloud model id (it never does: `request_model`
        # is captured once at `_generar_dialogo`'s entry, threaded through the
        # phases unrebound, and is always the local alias).
        cloud_by_design = trace_transport == "cloud" and not trace_fallback_active
        mismatch = desired != active or active != loaded or loaded != generation_model
        if mismatch and not cloud_by_design:
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
            # Item 4 (2026-07-24 incident): this is the FINAL empty return
            # (max_intentos + Layer-2 self-heal already exhausted) -- on
            # cloud that used to be silent dead air (no callback, no
            # fallback). Surface it the same way a transport failure does
            # (F1): fire the existing whitelisted status, then route to
            # the fallback state machine. Local stays byte-identical.
            if not is_local:
                self.ui_callback("cloud_llm_error")
                # No exception/status here (a well-formed but empty 2xx) --
                # `transient` per classify_cloud_error's own rule for a
                # malformed/unclassifiable 2xx body; drives backoff auto-
                # return (unit 2.2).
                self._handle_cloud_failure(
                    source, failure_class=cloud_llm_client.CLOUD_ERROR_TRANSIENT
                )
            if commit_history:
                self._invalidate_pregen_epoch()
            return ""

        if is_local:
            # F5: a cloud success must not set an unvalidated LOCAL model as
            # the rollback/fallback target (request_model is the local tag on
            # cloud) nor clear _awaiting_first_success_after_switch.
            self._mark_model_generation_success(request_model)
        # clause_sanitizer V1: intra-sentence clause repetition. Placed here,
        # before output_guard, so it is provider-agnostic by construction —
        # there is no is_local gate between this point and the return.
        if source in CLAUSE_SANITIZER_SOURCES:
            san = sanitize_clause_repetition(dialogo)
            # A pregen/connector-upgrade worker generates on its own
            # thread and interleaves into the same log, so the stage must
            # say which one this was — otherwise the tuning pass cannot
            # tell a spoken foreground turn from a speculative draft that
            # may never be spoken at all. Logged for every verdict, including
            # "clean" — otherwise the clean count is unobtainable from the
            # log (ADR-039 gate).
            self._log_clause_sanitizer(
                san, source,
                stage="generate" if commit_history else "pregen_draft",
            )
            # Tier 2 (reject -> regenerate) needs an owner for the
            # regeneration, and only agenda has one: the ADR-011 ladder,
            # reached by returning "" — the same idiom the ladder reject
            # below already uses. Elsewhere this is repair-only; the verdict
            # is still recorded so evidence for arming tier 2 accrues.
            if san.verdict == "rejected" and source.startswith("kira-agenda"):
                self._log(
                    f"Salida descartada por repetición de cláusulas "
                    f"(removed={san.removed_fragments} distinct={san.distinct_looping}).",
                    level="warning",
                )
                if commit_history:
                    self._invalidate_pregen_epoch()
                return ""
            dialogo = san.text
        allowed, guard_reason = output_guard(dialogo, source=source)
        if not allowed:
            self._log(f"Salida bloqueada por guardrail: {guard_reason}", level="warning")
            # guardrail_tuning_20260724 (owner decision "afinar + reintento"):
            # ONE extra generation, same prompt + a corrective system nudge,
            # before falling back to the canned line. A SEPARATE call outside
            # the max_intentos transport-retry loop above — never consumes
            # that budget. Posture (provider_cfg/is_local) stays the F2
            # snapshot; no live re-read.
            retry_content = self._retry_after_guard_block(
                messages=messages,
                opciones_llm=opciones_llm,
                request_model=request_model,
                chat_timeout=chat_timeout,
                provider_cfg=provider_cfg,
                is_local=is_local,
            )
            if retry_content and source.startswith("kira-agenda"):
                retry_content = self._sanitize_agenda_output(retry_content)
                transformer = getattr(self, "agenda_output_transformer", None)
                if transformer is not None:
                    try:
                        retry_content = transformer(retry_content)
                    except Exception:
                        logger.exception("Agenda output transformer failed (guardrail retry)")
            if retry_content:
                retry_allowed, _ = output_guard(retry_content, source=source)
                if retry_allowed:
                    self._log("Guardrail retry: respuesta corregida aceptada tras un intento adicional.")
                    dialogo = retry_content
                    allowed = True

        if not allowed:
            fallback = self._guardrail_fallback_line(source, guard_reason)
            if fallback:
                self._log("Guardrail fallback: usando línea neutral sin LLM.")
                # D4 (memoria_quality_20260717): a guardrail-blocked turn used
                # to return here BEFORE _commit_history, so the whole exchange
                # vanished from history AND capture (F4). Commit the user turn
                # + the spoken fallback line instead. Gated on commit_history
                # so callers that opted out are unaffected, and on a truthy
                # fallback so agenda sources (fallback="") keep their existing
                # state-machine handling with no empty pair appended. C1's
                # canned-fallback skip guarantees these pairs never become
                # memorias.
                if commit_history:
                    self._commit_history(
                        contexto, fallback, source=source, history_text=history_text,
                    )
                return fallback
            # WU4 F3 (WU3 follow-up): guardrail-no-fallback is a
            # non-committing foreground return — bump the pregen epoch so
            # a late zombie store cannot survive into the next pop.
            if commit_history:
                self._invalidate_pregen_epoch()
            return ""

        self._last_llm_failure = None
        self._last_cloud_failure_class = None
        self._cloud_bad_key_notified = False

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
                if commit_history:
                    self._invalidate_pregen_epoch()
                return self._guardrail_fallback_line(source) or ""

        if source.startswith("kira-agenda") and commit_history and not self._accept_agenda_output(dialogo):
            self._log(f"Agenda: salida rechazada ({self._format_agenda_rejection()}).", level="warning")
            self._invalidate_pregen_epoch()
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

    def _create_ollama_chat_client(self, ollama_module):
        client_factory = getattr(ollama_module, "Client", None)
        if client_factory is None:
            return None
        try:
            return client_factory(timeout=OLLAMA_CHAT_TIMEOUT)
        except TypeError as e:
            self._log(f"Ollama Client no soporta timeout de chat; usando cliente por defecto: {e}", level="warning")
            return None

    def _create_ollama_scout_client(self, ollama_module, *, timeout: float = LLM_SCOUT_TIMEOUT):
        """Dedicated short-timeout client for auxiliary, non-persona generations.

        Built with ``LLM_SCOUT_TIMEOUT`` (not the 180s chat timeout) so that when
        the HTTP timeout expires the socket closes and Ollama cancels the
        generation, releasing the single runner slot in ~LLM_SCOUT_TIMEOUT. That
        socket-level cancel is the ONLY real abort path: the watchdog's worker is
        a plain daemon thread with no cancellation, so without an HTTP timeout a
        timed-out call keeps the single runner busy.

        *timeout* (memory_promotion_20260725) generalises it for the draft-
        promotion judge, whose budget is adaptive (``_judge_timeout_seconds``).
        The default keeps the Topic Scout's caller byte-identical.
        """
        client_factory = getattr(ollama_module, "Client", None)
        if client_factory is None:
            return None
        try:
            return client_factory(timeout=timeout)
        except TypeError as e:
            self._log(f"Ollama Client no soporta timeout de scout; usando cliente por defecto: {e}", level="warning")
            return None

    # ── Provider gating (multi_provider_llm_20260723 Phase 3) ───────────────
    @staticmethod
    def _cfg_is_local(cfg: dict) -> bool:
        """Posture of a SPECIFIC provider-config snapshot (local Ollama or absent).

        PURE over `cfg` ONLY — it does NOT consult the runtime
        `_cloud_fallback_active` flag (F2, multi_provider_llm_20260723). The
        EFFECTIVE posture (`_cfg_is_local(cfg) or _cloud_fallback_active`) is
        computed ONCE at `_generar_dialogo` entry and threaded through the whole
        generation + dispatch, so a flag flip (or PUT) landing mid-turn can never
        tear a running generation between local/cloud. Non-generation call sites
        read the effective posture live via the `_is_local` property.
        """
        return (cfg.get("active_provider") or "local") == "local"

    def _cfg_active_profile(self, cfg: dict) -> Optional[dict]:
        """Active cloud profile dict for a SPECIFIC config snapshot, or None when local."""
        if self._cfg_is_local(cfg):
            return None
        provider = cfg.get("active_provider")
        profiles = cfg.get("profiles") or {}
        profile = profiles.get(provider)
        return profile if isinstance(profile, dict) else None

    @property
    def _is_local(self) -> bool:
        """True when local Ollama is the EFFECTIVE backend (spec B) — the active
        provider is local/absent OR an auto-fallback has engaged.

        The single gate for all local-only machinery at NON-generation call
        sites (command guards on download/switch, connector/scout/pregen gates,
        etc.), so it reads the runtime `_cloud_fallback_active` flag LIVE. A
        generation instead snapshots the EFFECTIVE posture ONCE at
        `_generar_dialogo` entry (`_cfg_is_local(cfg) or _cloud_fallback_active`)
        and threads that bool through its whole body + dispatch, so a
        `set_provider_config` swap or a flag flip racing an in-flight generation
        only takes effect on the NEXT call — the running call is pinned to its
        snapshot.
        """
        return self._cfg_is_local(self._provider_config) or self._cloud_fallback_active

    def _active_profile(self) -> Optional[dict]:
        """The active cloud profile dict (`base_url`/`model`/`preset`), or None when local."""
        return self._cfg_active_profile(self._provider_config)

    def set_provider_config(self, cfg: dict) -> None:
        """Live-swap the provider config (PUT /api/llm/provider, no restart).

        Attribute swap under `_lock`: the swap is a single atomic rebind, so a
        racing in-flight generation finishes on its original provider and only
        the next call observes the change (design 'Provider Config Surface').
        """
        if not isinstance(cfg, dict):
            return
        provider_changed = False
        with self._lock:
            # F5 (multi_provider_llm_20260723): clear the runtime fallback flag
            # ONLY when the PUT actually CHANGES active_provider — an explicit
            # provider change is the operator's intent to retry a (possibly
            # different) backend. An unrelated PUT (e.g. a pregen toggle that
            # keeps the same active_provider) must NOT silently un-fallback into
            # a still-dead cloud. Compare BEFORE the swap, normalizing absent ->
            # "local" like `_cfg_is_local`.
            previous_provider = (self._provider_config.get("active_provider") or "local")
            incoming_provider = (cfg.get("active_provider") or "local")
            self._provider_config = cfg
            if incoming_provider != previous_provider:
                provider_changed = True
                self._cloud_fallback_active = False
                # Unit 2.2: a manual re-arm (this IS the documented re-arm path
                # for ambiguous_429/bad_key) clears the reason/schedule and is
                # itself a provider transition, so it bumps the epoch too.
                self._cloud_fallback_reason = None
                self._cloud_probe_next_at = None
                self.provider_epoch += 1
        if provider_changed:
            # Unit 2.2 fix (finding 3): invalidate speculative drafts FIRST,
            # immediately after the swap/flag clear above -- BEFORE the
            # potentially ~2s blocking join inside _stop_cloud_prober below.
            # Plan step 6 mandates drafts invalidated before the transition
            # completes; leaving the (up to 2s) join in front of this let a
            # consumer thread (play_prefetched_agenda / restore_frozen_stash)
            # pop a draft made under the OLD provider during that window.
            # Mirrors the invalidate-then-transition order _handle_cloud_failure
            # and _on_cloud_probe_success already use.
            #
            # F12 (runtime_findings_batch_20260731): a provider switch must
            # invalidate speculative drafts -- unconditionally, on every
            # transition, rather than tagging each stash entry with
            # provider_epoch and checking it at consumption. The contract only
            # requires that no draft survive a provider switch in either
            # direction, not that a survivor be identifiable by provider; the
            # unconditional invalidate is the simpler choice that already
            # satisfies it, so no consumption-path changes are needed.
            self._invalidate_pregen_epoch()
            self._invalidate_frozen_stash()
            # Cancel any running background return probe cleanly (outside the
            # lock above -- _stop_cloud_prober takes it again itself).
            self._stop_cloud_prober()

    def _handle_cloud_failure(
        self,
        source: str,
        *,
        failure_class: str = cloud_llm_client.CLOUD_ERROR_TRANSIENT,
        retry_after_seconds: Optional[int] = None,
    ) -> None:
        """Cloud-only failure response (spec C / design 'Cloud Failure Flow').

        Invoked from the cloud branch of a watchdog-timeout failure — the
        branch where `_recover_from_stalled_inference` (local model rollback)
        is deliberately skipped for cloud (spec B: a cloud stall must never
        roll back a local model). `fallback_mode=manual` only surfaces the
        error and keeps routing to cloud — the caller's existing `""` failure
        contract is unchanged either way. `fallback_mode=auto` (default)
        degrades THIS PROCESS to local for every SUBSEQUENT generation via the
        runtime-only `_cloud_fallback_active` flag; the persisted
        `active_provider` selector is left untouched (design 'Fallback switch
        semantics'). Warm + speak run on a background daemon thread (mirrors
        `play_prefetched_agenda`'s `speaker()` closure) so a slow local warm-up
        (~10-20s, spec C) never blocks the caller or this already-failed turn.

        Unit 2.2 (runtime_findings_batch_20260731 F3/F12): `failure_class` is
        the 1.1 taxonomy class for THIS failure, computed by the caller from
        the exception in scope — never re-derived from the possibly-stale
        `_last_cloud_failure_class` instance attribute, which a watchdog
        timeout or an empty-response return (neither carries an exception)
        would leave holding a class from an earlier, unrelated turn.
        Recorded as `_cloud_fallback_reason` and used to schedule (or
        deliberately not schedule) the background return probe.
        """
        provider_cfg = self._provider_config
        fallback_mode = provider_cfg.get("fallback_mode", "auto")
        if fallback_mode == "manual":
            self._log(
                f"Cloud LLM failure (source={source}); fallback_mode=manual, staying on cloud.",
                level="error",
            )
            self.ui_callback("cloud_llm_error")
            return

        # F12/2.2 precondition: invalidate speculative drafts BEFORE any state
        # flips, in the cloud->local direction too ("in either direction" is
        # the contract) -- a draft generated under cloud must never survive
        # into local playback. Unconditional (not provider-tagged): simpler,
        # and sufficient (see set_provider_config's identical note).
        self._invalidate_pregen_epoch()
        self._invalidate_frozen_stash()

        # Set the flag OPTIMISTICALLY so a generation racing the warm-up already
        # routes local; the worker CLEARS it again if the local backend turns out
        # to be unavailable (F3).
        with self._lock:
            self._cloud_fallback_active = True
            self._cloud_fallback_reason = failure_class
            self.provider_epoch += 1
        self._log(
            f"Cloud LLM failure (source={source}); auto-falling back to local Ollama "
            f"(reason={failure_class}).",
            level="warning",
        )
        self.ui_callback("cloud_fallback_engaged")
        self._start_cloud_prober(failure_class, retry_after_seconds)
        fallback_model = self._last_known_good_model or self.current_model

        def worker() -> None:
            # F3: _prepare_model never raises (it returns False on failure), so
            # its RESULT is the only signal that local actually warmed. On a
            # cloud-only box without Ollama it returns False — clearing the
            # optimistic flag, suppressing the false "switching to local" notice,
            # and surfacing the double-failure to the operator instead.
            try:
                warmed = self._prepare_model(fallback_model)
            except Exception:
                logger.exception("Cloud fallback: local model warm-up failed")
                warmed = False
            if not warmed:
                # Unit 2.2 fix (finding 5): this un-fallback is a provider
                # TRANSITION (effective transport flips back to cloud, since
                # _cloud_fallback_active clears) exactly like
                # set_provider_config/_on_cloud_probe_success -- it must
                # invalidate speculative drafts and bump provider_epoch the
                # same way ("in either direction" per the F12 precondition,
                # and per provider_epoch's own docstring). Invalidate BEFORE
                # flipping the flag, same order as the other two transitions,
                # so a consumer can never pop a draft made during the local
                # warm-up window under the now-stale local posture.
                self._invalidate_pregen_epoch()
                self._invalidate_frozen_stash()
                with self._lock:
                    self._cloud_fallback_active = False
                    self._cloud_fallback_reason = None
                    self._cloud_probe_next_at = None
                    self.provider_epoch += 1
                # Neither backend works -- a probe would only re-discover that;
                # the flag clearing above already sends the NEXT turn straight
                # back at cloud, which re-enters this whole method on failure.
                self._stop_cloud_prober()
                self._log(
                    f"Cloud fallback (source={source}): local warm-up failed; "
                    "neither cloud nor local is usable.",
                    level="warning",
                )
                self.ui_callback("cloud_llm_error")
                return
            try:
                # EXPLICIT priority (design §11, the audit's rider): this notice
                # inherits the FAILED TURN's source, which may be `direct`/`ptt`.
                # Letting it inherit the owner band (priority 0) would make a
                # system notice preemptive at step 3 under owner decision D3
                # (uniform priority-0 preemption). It is chat-band work.
                self._speak_or_submit(
                    i18n_active.provider_fallback_notice(), source=source, priority=1
                )
            except Exception:
                logger.exception("Cloud fallback: could not speak provider_fallback_notice")

        threading.Thread(target=worker, name="CloudFallbackWarm", daemon=True).start()

    def _stop_cloud_prober(self) -> None:
        """Unit 2.2: cancel any running background return probe.

        Best-effort, bounded join -- a probe mid network-call cannot be
        interrupted, so it is left to finish on its own daemon thread; it
        checks the stop event both before scheduling its next wait and right
        after its network call returns, and drops the result if set, so a
        late-finishing probe can never mutate state a caller here already
        moved past.

        Fix (finding 1): the thread handle, stop event and generation bump
        are ALL captured/mutated inside ONE `_lock` critical section (the
        prior code read/nulled `_cloud_prober_thread` outside the lock while
        `_start_cloud_prober` writes it inside -- a torn read/write could
        either starve a fresh prober behind a stale `already_running`, or
        have this call null out a thread a concurrent `_start_cloud_prober`
        had just assigned). The generation bump makes the stopped prober
        stale for every guarded write it might still make even if the join
        below times out; `_cloud_probe_next_at` is always cleared here too
        (finding 6) so a frozen countdown can never survive a stop.
        """
        with self._lock:
            stop_event = self._cloud_prober_stop
            thread = self._cloud_prober_thread
            self._cloud_prober_stop = None
            self._cloud_prober_generation += 1
            self._cloud_probe_next_at = None
        if stop_event is None:
            return
        stop_event.set()
        if thread is not None:
            thread.join(timeout=CLOUD_PROBER_JOIN_TIMEOUT_SECONDS)
        with self._lock:
            # Compare-and-clear: only null the handle if nothing newer (a
            # fresh _start_cloud_prober racing this join) already replaced it.
            if self._cloud_prober_thread is thread:
                self._cloud_prober_thread = None

    def _initial_cloud_probe_wait(
        self, failure_class: str, retry_after_seconds: Optional[int]
    ) -> Optional[float]:
        """Unit 2.2 per-class policy: seconds until the FIRST probe, or None
        when this class never gets an automatic probe loop.

        WU3 (cloud_rearm_20260801, owner decision D-A): `ambiguous_429` now
        ALSO gets a conservative automatic probe loop, gated by
        `CLOUD_AUTO_RETURN_AMBIGUOUS_429_ENABLED` (one-line off-switch).
        `bad_key` stays manual-only in every variant -- waiting cannot fix a
        bad credential. Either way, the manual trigger
        (`trigger_cloud_probe_now`, WU1) bypasses this table entirely."""
        if failure_class == cloud_llm_client.CLOUD_ERROR_RATE_LIMITED:
            wait = (
                retry_after_seconds
                if retry_after_seconds is not None
                else CLOUD_AUTO_RETURN_RATE_LIMIT_FLOOR_SECONDS
            )
            return float(
                min(
                    max(wait, CLOUD_AUTO_RETURN_RATE_LIMIT_FLOOR_SECONDS),
                    CLOUD_AUTO_RETURN_RATE_LIMIT_CAP_SECONDS,
                )
            )
        if failure_class == cloud_llm_client.CLOUD_ERROR_TRANSIENT:
            return float(CLOUD_AUTO_RETURN_TRANSIENT_BASE_SECONDS)
        if (
            failure_class == cloud_llm_client.CLOUD_ERROR_AMBIGUOUS_429
            and CLOUD_AUTO_RETURN_AMBIGUOUS_429_ENABLED
        ):
            return float(CLOUD_AUTO_RETURN_AMBIGUOUS_429_BASE_SECONDS)
        return None

    def _next_cloud_probe_wait(self, failure_class: str, previous_wait: float) -> float:
        """Exponential backoff on a FAILED probe, capped per class."""
        if failure_class == cloud_llm_client.CLOUD_ERROR_RATE_LIMITED:
            cap = CLOUD_AUTO_RETURN_RATE_LIMIT_CAP_SECONDS
        elif failure_class == cloud_llm_client.CLOUD_ERROR_AMBIGUOUS_429:
            # WU3: its own cap, independent of transient's (reuses the
            # rate-limit ceiling by design -- see settings.py).
            cap = CLOUD_AUTO_RETURN_AMBIGUOUS_429_CAP_SECONDS
        else:
            cap = CLOUD_AUTO_RETURN_TRANSIENT_CAP_SECONDS
        return float(min(previous_wait * 2, cap))

    def _cloud_probe_max_attempts(self, failure_class: str) -> Optional[int]:
        """WU3 (cloud_rearm_20260801): probe-count ceiling for a class's
        background loop. None means unbounded (rate_limited/transient keep
        retrying forever, unchanged). Only ambiguous_429's conservative
        auto-return (owner decision D-A) is bounded, and only while the
        flag is on -- when off, `_initial_cloud_probe_wait` never arms it
        in the first place, so this is never consulted for that case."""
        if (
            failure_class == cloud_llm_client.CLOUD_ERROR_AMBIGUOUS_429
            and CLOUD_AUTO_RETURN_AMBIGUOUS_429_ENABLED
        ):
            return CLOUD_AUTO_RETURN_AMBIGUOUS_429_MAX_ATTEMPTS
        return None

    def _notify_cloud_probe_gave_up(self) -> None:
        """WU3: the background probe loop exhausted its attempts budget
        without a successful probe. No detail payload -- privacy gate, same
        as cloud_fallback_engaged/cloud_restored (see
        engine_host._MOTOR_EVENT_WHITELIST)."""
        self.ui_callback("cloud_probe_gave_up")

    def _notify_cloud_probe_scheduled(self, wait: float) -> None:
        hook = getattr(self, "on_cloud_probe_scheduled", None)
        if hook is not None:
            try:
                hook({"seconds": wait})
            except Exception:
                logger.exception("on_cloud_probe_scheduled callback failed")
        self.ui_callback("cloud_probe_scheduled")

    def _cloud_probe_once(self, provider_cfg: dict) -> bool:
        """Unit 2.2: single bounded probe through the SAME cloud client/config
        real turns use. Static minimal message only -- NO history, NO
        persona, NO user content (privacy contract) -- and `num_predict=1`
        (mapped to `max_tokens`) to minimize cost. Success = a well-formed
        response; any exception (timeout, non-2xx, malformed body) is a
        failure, exactly like a real turn's transport-error classification."""
        try:
            self._cloud_chat(
                provider_cfg=provider_cfg,
                messages=[{"role": "user", "content": "ping"}],
                options={"num_predict": 1},
                is_local=False,
            )
            return True
        except Exception:
            return False

    def _on_cloud_probe_success(self, generation: int) -> None:
        """Unit 2.2 RETURN sequence, in the mandated order: (a) invalidate
        speculative drafts FIRST, (b) bump provider_epoch, (c) clear the
        fallback flag/reason under the lock, (d) narrate. History and
        persistent memoria are untouched by design -- continuity across the
        switch is the point (F15).

        Fix (finding 1/2): `generation` is the caller prober's OWN generation,
        captured when it was started. Checked under `_lock` before doing
        anything, and again right before the state flip, so a superseded
        (stopped/replaced) prober whose network call was already in flight
        can never resurrect the fallback flag or clobber a newer failure
        under a manual provider choice.
        """
        with self._lock:
            if generation != self._cloud_prober_generation:
                return
        self._invalidate_pregen_epoch()
        self._invalidate_frozen_stash()
        with self._lock:
            if generation != self._cloud_prober_generation:
                return
            self.provider_epoch += 1
            self._cloud_fallback_active = False
            self._cloud_fallback_reason = None
            self._cloud_probe_next_at = None
            self._cloud_prober_stop = None
        self._log("Cloud restored; returning from local fallback.", level="info")
        self.ui_callback("cloud_restored")

    def _run_cloud_prober(
        self,
        stop_event: threading.Event,
        failure_class: str,
        wait: float,
        generation: int,
        attempts_left: Optional[int] = None,
    ) -> None:
        """Unit 2.2: the background probe loop. Runs on its OWN daemon
        thread (started by `_start_cloud_prober`), NEVER inside a
        user-facing turn. `stop_event.wait` (not `time.sleep`) so a manual
        provider PUT (`_stop_cloud_prober`) interrupts the wait immediately
        instead of only being noticed on the next tick.

        Fix (findings 1/2/4/6): `generation` is THIS prober's own id,
        captured by `_start_cloud_prober` at creation. Every state-mutating
        write below re-checks `generation == self._cloud_prober_generation`
        under `_lock` first (alongside the pre-existing stop_event check) --
        a superseded prober (stopped outright, or replaced by a fresh
        failure's class/schedule) keeps running harmlessly to completion
        but can never reschedule a countdown or trigger a return once stale.

        WU3 (cloud_rearm_20260801): `attempts_left` is None for every
        caller except a bounded class (ambiguous_429, when its flag is on)
        -- None means unbounded, so rate_limited/transient never give up,
        exactly as before this unit. When bounded, a failed probe consumes
        one attempt; reaching zero gives up instead of rescheduling, via
        `_notify_cloud_probe_gave_up`, leaving `_cloud_fallback_reason`
        untouched (still local) but `_cloud_probe_next_at` cleared -- the
        manual trigger (WU1) remains the door back in.
        """
        while True:
            if stop_event.wait(timeout=wait):
                return  # stop requested during the wait
            with self._lock:
                if stop_event.is_set() or generation != self._cloud_prober_generation:
                    return
                provider_cfg = self._provider_config
            ok = self._cloud_probe_once(provider_cfg)
            if stop_event.is_set():
                return  # superseded by a manual re-arm while probing
            if ok:
                self._on_cloud_probe_success(generation)
                return
            if attempts_left is not None:
                attempts_left -= 1
                if attempts_left <= 0:
                    with self._lock:
                        if stop_event.is_set() or generation != self._cloud_prober_generation:
                            return
                        self._cloud_probe_next_at = None
                        self._cloud_prober_stop = None
                    self._log(f"Cloud probe gave up: clase={failure_class}", level="warning")
                    self._notify_cloud_probe_gave_up()
                    return
            if wait == 0:
                # Post-WU3 fix: a manual trigger (WU1) arms with wait=0 for
                # "probe immediately". Doubling zero is a fixed point (0*2
                # == 0), which would otherwise re-probe in a tight loop
                # forever. Hand off to the class's own auto-return cadence
                # instead. Only reached for a class WITH an auto policy --
                # a no-policy class (bad_key always; ambiguous_429 flag-off)
                # was already exhausted by the one-shot attempts budget
                # `_arm_cloud_prober` gives it, above.
                wait = self._initial_cloud_probe_wait(failure_class, None)
            else:
                wait = self._next_cloud_probe_wait(failure_class, wait)
            with self._lock:
                if stop_event.is_set() or generation != self._cloud_prober_generation:
                    return
                self._cloud_probe_next_at = time.monotonic() + wait
            self._notify_cloud_probe_scheduled(wait)

    def _arm_cloud_prober(self, failure_class: str, wait: float) -> None:
        """WU1 (cloud_rearm_20260801): the stop-old/generation-bump/
        thread-start tail extracted from `_start_cloud_prober` (unchanged
        code, just named) so `trigger_cloud_probe_now` can reuse the exact
        same thread-safety choreography for an immediate manual probe
        without a second prober flavor to keep in sync. Never called with
        `wait=None` -- the caller decides whether this class/situation gets
        a probe at all.

        `_stop_cloud_prober` makes this safe even though its join is
        bounded and best-effort: it bumps `_cloud_prober_generation` before
        returning, so the old prober -- even if its join times out and it
        keeps running -- becomes a harmless zombie the moment this method's
        own generation bump below runs; none of its writes can land.

        WU3: `attempts_left` is computed HERE from `failure_class` alone,
        so it is uniform for both callers -- an automatic arm
        (`_start_cloud_prober`) and a manual one (`trigger_cloud_probe_now`)
        get the identical (fresh) attempts budget for the class, never a
        stale leftover count from a prior give-up.

        Post-WU3 fix: a class with NO automatic policy at all (bad_key
        always; ambiguous_429 when its flag is off) gets a ONE-SHOT budget
        here. Without this, a manual trigger's wait=0 on a failed probe
        would loop forever in `_run_cloud_prober` (0*2 is a fixed point) --
        this class was never meant to retry unattended, so one attempt then
        `cloud_probe_gave_up` (manual re-trigger still works after) is the
        correct manual-probe contract, not silent infinite spin.
        """
        self._stop_cloud_prober()
        attempts_left = self._cloud_probe_max_attempts(failure_class)
        if attempts_left is None and self._initial_cloud_probe_wait(failure_class, None) is None:
            attempts_left = 1
        with self._lock:
            self._cloud_prober_generation += 1
            generation = self._cloud_prober_generation
            stop_event = threading.Event()
            thread = threading.Thread(
                target=self._run_cloud_prober,
                args=(stop_event, failure_class, wait, generation, attempts_left),
                name="CloudProber",
                daemon=True,
            )
            self._cloud_prober_stop = stop_event
            self._cloud_prober_thread = thread
            self._cloud_probe_next_at = time.monotonic() + wait
        self._log(
            f"Cloud probe armed: clase={failure_class} wait={int(wait)}s "
            f"attempts_left={'unbounded' if attempts_left is None else attempts_left}",
            level="info",
        )
        self._notify_cloud_probe_scheduled(wait)
        thread.start()

    def _start_cloud_prober(
        self, failure_class: str, retry_after_seconds: Optional[int]
    ) -> None:
        """Unit 2.2: reconcile the background return probe with a NEW
        failure (fix for findings 2/4). No-op (after stopping any live
        prober) for ambiguous_429/bad_key -- no automatic probe loop, manual
        re-arm only. For a probeable class, UNCONDITIONALLY replaces
        whatever prober is already running with a fresh one for the new
        class/wait: a second failure must never be silently swallowed by
        the old `already_running` single-flight gate, which let a
        non-probeable second failure leave a live prober able to auto-return
        under a reason it forbids (finding 2), and let a probeable second
        failure's own class/schedule (e.g. `rate_limited`'s Retry-After) be
        discarded in favour of the stale one (finding 4).

        WU1 (cloud_rearm_20260801): thin wrapper now -- this method only
        decides WHETHER to arm (per-class policy via
        `_initial_cloud_probe_wait`); the stop/generation-bump/thread
        choreography lives in `_arm_cloud_prober`, shared with the manual
        `trigger_cloud_probe_now` path.
        """
        wait = self._initial_cloud_probe_wait(failure_class, retry_after_seconds)
        if wait is None:
            self._stop_cloud_prober()
            return
        self._arm_cloud_prober(failure_class, wait)

    def trigger_cloud_probe_now(self) -> dict:
        """WU1 (cloud_rearm_20260801): manual probe trigger -- arms an
        immediate probe (wait~=0) bypassing `_initial_cloud_probe_wait`'s
        per-class table, so `ambiguous_429`/`bad_key` (manual-re-arm-only
        classes) can also be probed on demand. Backs
        `POST /api/llm/provider/probe` (WU2); synchronous and never touches
        `command_queue`/the dispatcher, so it is usable mid-agenda,
        side-stepping the locked model selector.

        No-op (200-style, not an error) when there is nothing to probe:
        `not_in_fallback` (nothing to return from) or `no_cloud_profile` (no
        cloud profile configured to probe against). Otherwise collapses any
        already-scheduled probe to now via the same `_arm_cloud_prober` tail
        `_start_cloud_prober` uses, so success runs through the untouched
        `_on_cloud_probe_success` -- never a second prober flavor.
        """
        with self._lock:
            in_fallback = self._cloud_fallback_active
            provider_cfg = self._provider_config
            failure_class = self._cloud_fallback_reason
        if not in_fallback:
            return {"armed": False, "reason": "not_in_fallback"}
        if self._cfg_active_profile(provider_cfg) is None:
            return {"armed": False, "reason": "no_cloud_profile"}
        self._arm_cloud_prober(failure_class, 0.0)
        return {"armed": True, "reason": None}

    def _cloud_api_key(self, profile_id) -> str:
        """Read the per-profile cloud key from the OAuthStore (LLM_KEYS_FILE)."""
        token = OAuthStore(LLM_KEYS_FILE).load(profile_id)
        if isinstance(token, dict):
            return str(token.get("api_key") or "")
        return ""

    def _cloud_chat(self, *, provider_cfg=None, model=None, messages, options=None, is_local=None, **_ignored):
        """Dispatch a chat request to the OpenAI-compatible cloud client.

        Resolves profile + provider_id + key from a SINGLE provider-config
        snapshot (`provider_cfg`, threaded from `_generar_dialogo`'s entry) so a
        mid-generation `set_provider_config` swap can never pair provider A's
        base_url/model with provider B's key (F2). Falls back to live config for
        stand-alone (non-generation) callers. Uses the ACTIVE profile's
        `base_url`/`model`/key — never the local `current_model` the call site
        passes. Ollama-only kwargs (`keep_alive`) are ignored here.
        """
        cfg = provider_cfg if provider_cfg is not None else self._provider_config
        profile = self._cfg_active_profile(cfg)
        if profile is None:
            # F7: degrade like every other cloud failure. A RequestException
            # subclass is caught by the transport-error contract and returns ''
            # instead of propagating out through the noisy outer catch-all.
            raise cloud_llm_client.CloudLLMResponseError(
                "cloud provider active but no profile configured"
            )
        provider_id = cfg.get("active_provider")
        return cloud_llm_client.send_chat_completion(
            base_url=str(profile.get("base_url") or ""),
            api_key=self._cloud_api_key(provider_id),
            model=str(profile.get("model") or ""),
            messages=messages,
            options=options or {},
            timeout=self._resolve_chat_watchdog_timeout(model, provider_cfg=cfg, is_local=is_local),
        )

    def _ollama_chat(self, *, provider_cfg=None, is_local=None, **kwargs):
        cfg = provider_cfg if provider_cfg is not None else self._provider_config
        # F2: honor the EFFECTIVE posture threaded from `_generar_dialogo`'s
        # entry snapshot; only stand-alone callers (is_local=None) re-derive it
        # live (config OR active fallback).
        if is_local is None:
            is_local = self._cfg_is_local(cfg) or self._cloud_fallback_active
        if not is_local:
            return self._cloud_chat(provider_cfg=cfg, is_local=is_local, **kwargs)
        client = self._ollama_chat_client or self.ollama
        return client.chat(**kwargs)

    def _ollama_scout_chat(self, *, provider_cfg=None, is_local=None, **kwargs):
        cfg = provider_cfg if provider_cfg is not None else self._provider_config
        if is_local is None:
            is_local = self._cfg_is_local(cfg) or self._cloud_fallback_active
        if not is_local:
            return self._cloud_chat(provider_cfg=cfg, is_local=is_local, **kwargs)
        client = self._ollama_scout_client or self.ollama
        return client.chat(**kwargs)

    def _ollama_judge_chat(self, *, provider_cfg=None, is_local=None, **kwargs):
        """Transport for the memoria promotion judge — mirrors _ollama_scout_chat.

        Provider-agnostic by owner decision 2: whatever backend is active runs
        the judge. Locally it uses the dedicated short-timeout client built with
        the adaptive budget, so a timed-out judgment actually releases the single
        Ollama runner instead of leaving it generating into a closed watchdog.
        """
        cfg = provider_cfg if provider_cfg is not None else self._provider_config
        if is_local is None:
            is_local = self._cfg_is_local(cfg) or self._cloud_fallback_active
        if not is_local:
            return self._cloud_chat(provider_cfg=cfg, is_local=is_local, **kwargs)
        client = self._ollama_judge_client or self.ollama
        return client.chat(**kwargs)

    def _call_with_watchdog(self, call, *, timeout: float, label: str = "OllamaChatWatchdog", **kwargs):
        """Run *call* on a daemon thread and stop waiting on it after *timeout*.

        Deliberately separate from ``_ollama_chat_with_watchdog``: the chat
        transport gets swapped — by the cloud fallback and by tests — and
        swapping it must NOT also swap the ``ollama.show`` metadata probe, which
        is a different call with a different budget. Sharing one seam for both
        made the probe return chat-shaped responses.
        """
        result = {}
        done = threading.Event()

        def worker() -> None:
            try:
                result["response"] = call(**kwargs)
            except Exception as exc:
                result["error"] = exc
            finally:
                done.set()

        thread = threading.Thread(
            target=worker,
            name=f"{label}-{uuid.uuid4().hex[:8]}",
            daemon=True,
        )
        thread.start()
        if not done.wait(timeout=max(0.1, float(timeout))):
            raise TimeoutError(f"watchdog_timeout:{timeout:.2f}s")
        if "error" in result:
            raise result["error"]
        return result.get("response")

    def _ollama_chat_with_watchdog(self, *, timeout: float, chat_callable=None, **kwargs):
        return self._call_with_watchdog(
            chat_callable or self._ollama_chat, timeout=timeout, **kwargs
        )

    def _resolve_chat_watchdog_timeout(self, request_model: str, *, provider_cfg=None, is_local=None) -> float:
        cfg = provider_cfg if provider_cfg is not None else self._provider_config
        # F2: honor the threaded entry posture; stand-alone callers re-derive.
        if is_local is None:
            is_local = self._cfg_is_local(cfg) or self._cloud_fallback_active
        if not is_local:
            # Cloud latency, not local GPU stall detection (spec C).
            return CLOUD_CHAT_TIMEOUT
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

    def _scout_extract_text(self, response, *, field: str = "content") -> str:
        """Pull the assistant content out of a chat response (dict or object).

        *field* selects which message field. The memoria judge's empty-content
        self-heal needs ``thinking``, and this already handles both response
        shapes — a second extractor would only duplicate that.
        """
        if response is None:
            return ""
        msg = response["message"] if isinstance(response, dict) else getattr(response, "message", None)
        if msg is None:
            return ""
        content = msg.get(field) if isinstance(msg, dict) else getattr(msg, field, "")
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
            # F4 Pregen Cloud Gate (multi_provider_llm_20260723): the scout is
            # speculative generation and pregen-OFF covers ALL speculative spend,
            # so skip the dispatch on cloud unless explicitly opted in. Local
            # short-circuits (byte-identical). Same idiom as the pregenerate gate.
            if not self._is_local and not self._provider_config.get("pregen_enabled", False):
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

    # ── memoria draft promotion (memory_promotion_20260725) ─────────────────
    def _judge_model(self) -> str:
        """The model name the judge will ACTUALLY run on, whatever the provider.

        Locally that is the resident model (never the desired one — the sweep
        must not cold-load). On cloud there is no resident local model at all
        (``_prepare_model`` returns before assigning ``_loaded_model``), so it is
        the ACTIVE PROFILE's model — the same one ``_cloud_chat`` resolves for
        itself and sends. Always a str: ``_resolve_reasoning_classification``
        lowercases it, so a None here would raise straight into the sweep's
        catch-all and silently disable the feature on cloud.
        """
        if self._is_local:
            return self._loaded_model or ""
        profile = self._cfg_active_profile(self._provider_config) or {}
        return str(profile.get("model") or "")

    def _judge_timeout_seconds(self) -> float:
        """The judge's adaptive time budget (owner decision 5).

        Reuses ``_pregen_last_gen_duration`` — the SAME measurement
        ``_pregen_retry_gate_seconds`` already ships — instead of introducing a
        second latency-measurement scheme or a fixed constant (a constant would
        be a per-model assumption in disguise, contradicting the model-agnostic
        decision 2).

        Stated honestly: at the STARTUP trigger no generation has happened yet,
        so ``last`` is None and this returns the cold-start value — the identical
        contract ``_pregen_retry_gate_seconds`` already has. The reasoning branch
        rides the capability probe D2 needs anyway (cached ``ollama.show``), so it
        is a per-model MEASUREMENT, not a per-model assumption.

        On CLOUD the budget is ``CLOUD_CHAT_TIMEOUT`` — the codebase's own cloud
        chat budget, which ``_cloud_chat`` enforces at the socket regardless of
        what this returns. Deriving a shorter one from local-generation latency
        would only make the watchdog fire BEFORE the socket, aborting every cloud
        judgment mid-flight with nothing written: the same permanent no-op the
        resident-model gate used to cause, one layer down. No new constant.
        """
        if not self._is_local:
            return float(CLOUD_CHAT_TIMEOUT)
        last = self._pregen_last_gen_duration
        if last is None:
            base = RETRY_MIN_REMAINING_SECONDS * (
                _JUDGE_REASONING_COLD_FACTOR
                if self._resolve_reasoning_classification(self._judge_model()) else 1.0
            )
        else:
            base = last * _JUDGE_BUDGET_FACTOR
        return max(_JUDGE_BUDGET_FLOOR_SECONDS, min(_JUDGE_BUDGET_CEILING_SECONDS, base))

    def _run_promotion_judge(self, batch: list, *, chat_callable=None) -> list[tuple]:
        """ONE chat completion over *batch*, parsed. Raises on transport failure.

        The reasoning branch (D2): ``scout_digest``'s sixth gate skips thinking
        models entirely because they burn a 64-token budget inside ``<think>``
        and return empty ``content``. Adopting it here would mean ZERO promotions
        forever on the owner's Qwen3-class models, so this reaches the same
        conclusion ``_generar_dialogo``'s self-heal already does instead: OMIT
        ``num_predict`` on a reasoning-capable model. The capability answer is
        cached, so this costs no extra RPC.

        Detection goes through ``_resolve_reasoning_classification`` — the SAME
        resolver ``_generar_dialogo`` already trusts — not the narrow
        ``_check_capabilities_reasoning`` probe alone. That probe degrades to
        False on any Ollama build whose ``show`` response lacks ``capabilities``
        (and returns {} outright on cloud), which would leave the cap on a
        Qwen3-class model, burn it inside <think>, return empty content, and
        re-pay the identical inference every launch with zero promotions.
        """
        draft_block = "\n".join(
            f"{i}. {row['content']}" for i, row in enumerate(batch, start=1)
        )
        # str.replace, not str.format: the prompt's JSON example is full of
        # literal braces and doubling every one of them is a corruption trap.
        prompt = _PROMOTION_JUDGE_PROMPT.replace("{draft_block}", draft_block)
        budget = self._judge_timeout_seconds()
        # Reuses the established auxiliary-generation temperature rather than
        # inventing a second knob; a format-drifted reply is already handled
        # (the parser returns [] and nothing is marked judged).
        options = {"temperature": LLM_SCOUT_TEMPERATURE}
        judge_model = self._judge_model()
        if not self._resolve_reasoning_classification(judge_model):
            # Local-sized caps do not transfer to cloud. A 40-draft batch that
            # keeps ~20 needs ~1700 output tokens (a keep is ~60-67: the index,
            # the flags and up to 220 chars of rewritten text), so a flat 1200
            # truncates the reply mid-object -- the parser then returns [],
            # nothing is marked judged, and the SAME batch is re-sent and
            # re-paid on the next launch, forever. `_generar_dialogo` already
            # makes exactly this distinction at its own call site.
            options["num_predict"] = (
                _PROMOTION_NUM_PREDICT if self._is_local else CLOUD_MAX_TOKENS
            )
        if chat_callable is None and self._is_local:
            # Rebuilt per sweep because the budget is adaptive; the sweep runs
            # once per launch, so this costs nothing. Local only — on cloud
            # `_ollama_judge_chat` never touches the client, and a cloud-only
            # process has no reason to reach for `self.ollama` at all.
            self._ollama_judge_client = self._create_ollama_scout_client(
                self.ollama, timeout=budget,
            )
        call = chat_callable or self._ollama_judge_chat
        messages = [{"role": "user", "content": prompt}]
        deadline = time.monotonic() + budget + 2
        response = self._ollama_chat_with_watchdog(
            timeout=budget + 2,  # the socket abort must fire first (scout precedent)
            chat_callable=call,
            model=judge_model,
            messages=messages,
            options=options,
            keep_alive=LLM_KEEP_ALIVE,
        )
        text = self._scout_extract_text(response)
        # `_generar_dialogo`'s Layer-2 self-heal, verbatim in shape: empty visible
        # content + internal thinking + a cap in the options means the model spent
        # the budget inside <think>. Drop the cap and re-issue ONCE.
        #
        # This is the only thing that makes the judge work on a thinking-capable
        # CLOUD model. Detection cannot: the name heuristic knows a handful of
        # markers and `_fetch_show` returns {} outright when `not self._is_local`,
        # so glm-5.2 is classified "not reasoning", gets num_predict (sent as
        # max_tokens), returns empty content — and the sweep promotes nothing,
        # every launch, forever, paying a full cloud inference each time. The
        # response IS the evidence, so no detection is involved and this covers
        # cloud and local, known and unknown model names alike.
        #
        # One shot, never a loop, and inside the FIRST call's deadline: the retry
        # gets what is LEFT of it, so the self-heal cannot double the operator's
        # worst-case wait. A non-positive remainder is already handled — the
        # watchdog floors its wait at 0.1s and raises, which the sweep's isolation
        # turns into "nothing written, next launch retries".
        if not text.strip() and self._scout_extract_text(response, field="thinking") \
                and "num_predict" in options:
            options.pop("num_predict", None)
            logger.warning(
                "memoria judge returned empty content with internal thinking; "
                "dropping the token cap and retrying once",
            )
            response = self._ollama_chat_with_watchdog(
                timeout=deadline - time.monotonic(),
                chat_callable=call,
                model=judge_model,
                messages=messages,
                options=options,
                keep_alive=LLM_KEEP_ALIVE,
            )
            text = self._scout_extract_text(response)
        return _parse_promotion_decisions(text, len(batch))

    def _promotion_gate(self) -> str:
        """Name of the gate blocking a sweep right now, or "" when clear.

        The resident-model check is LOCAL-ONLY, deliberately. ``_loaded_model``
        is assigned exclusively on the local warm path — ``_check_ollama_service``
        returns at ``if not self._is_local`` and never reaches ``_prepare_model``,
        which itself returns without assigning it — so on a CLOUD provider it is
        None for the entire process. Gating on it unconditionally made the whole
        feature a permanent, silent no-op on the owner's actual configuration
        (GLM-5.2 via NVIDIA NIM), contradicting owner decision 2: the judge runs
        on whatever provider is active. Cloud needs no resident model at all —
        ``_cloud_chat`` resolves base_url/model/key from the active profile.
        """
        if not MEMORIAS_ENABLED:
            return "memorias_disabled"
        if self._is_local and self._loaded_model is None:
            return "no_resident_model"   # never cold-load: the RESIDENT model, not the desired one
        if self._pending_model_switch or self._awaiting_first_success_after_switch:
            return "model_switch_pending"
        if self._current_profile_id is None:
            return "no_profile"
        return ""

    def promote_pending_drafts(self, *, chat_callable=None) -> dict:
        """ONE LLM call: judge, rewrite and promote this profile's oldest
        unjudged drafts (memory_promotion_20260725).

        Per-turn capture is cheap, permissive and LLM-free; nothing ever filtered
        afterward, so the store fills with vague half-memories Kira then recites.
        This is the missing step.

        Synchronous, on the engine thread, and FULLY isolated (``scout_digest``
        precedent): every internal failure — timeout, malformed JSON, provider
        offline, sqlite lock — returns counts-only and leaves every draft
        untouched and UNJUDGED, so the next launch simply retries. A failed
        promotion can never lose a memory: this method never deletes and never
        demotes.

        *chat_callable* is the test seam, threaded straight into
        ``_ollama_chat_with_watchdog`` (the same boundary the Topic Scout tests
        fake). Returns counts ONLY — RC-8: no memory text ever reaches a log.

        ``counts["skipped"]`` names the gate that blocked a NOT-ATTEMPTED sweep
        and is "" whenever the sweep genuinely reached the store. The one-shot
        latch in ``run()`` reads it: arming on the attempt instead of the outcome
        meant a single transient not-ready first idle tick (the profile is seeded
        AFTER ``motor.start()`` on the API surface; Ollama may not be up yet on
        the GUI one) disabled promotion for the whole process, silently.
        """
        reasons: Counter = Counter()
        counts = {
            "considered": 0, "kept": 0, "rejected": 0, "stale": 0,
            "unjudged_remaining": 0, "reasons": reasons, "skipped": "",
        }
        try:
            # scout_digest's gates MINUS its reasoning value gate (see D2).
            gate = self._promotion_gate()
            if gate:
                counts["skipped"] = gate
                # Once per CHANGED gate state, so a permanently inert sweep is
                # observable (owner decision 8) without a log line every second.
                if gate != self._promotion_last_gate:
                    self._promotion_last_gate = gate
                    logger.info("memoria promotion sweep gated: %s", gate)
                return counts
            self._promotion_last_gate = ""
            profile_id = self._current_profile_id

            store = self._get_memoria_store()
            drafts = store.list_unjudged_drafts(profile_id, limit=_PROMOTION_DRAFT_BATCH)
            if not drafts:
                return counts  # the common no-op: zero tokens, no call at all
            counts["considered"] = len(drafts)

            # No dedup step, deliberately: the design's "drop any draft whose
            # stable_key is already durable" can never fire. UNIQUE(profile_id,
            # stable_key) is a TABLE constraint, so that state is unreachable —
            # a re-capture of an already-durable exchange resolves upsert_draft
            # to a no-op and inserts nothing. Re-judging a promoted row is
            # equally impossible (list_unjudged_drafts filters status='draft'
            # AND judged_at=''). Both would have been dead code behind a test
            # that only passes on a hand-forged database.
            batch = drafts

            if batch:
                kept: list[tuple[str, int]] = []
                rejected: list[tuple[str, int]] = []
                for index, judged_text, uncertain, reason in self._run_promotion_judge(
                    batch, chat_callable=chat_callable,
                ):
                    row = batch[index - 1]
                    if judged_text is None:
                        reasons[reason] += 1
                        rejected.append((row["id"], row["revision"]))
                        continue
                    # Two optimistic-concurrency tokens, because the operator has
                    # two ways to touch a row while the model is thinking.
                    # `if_revision` catches a re-CAPTURE (upsert_draft bumps it);
                    # `if_status` catches a hand EDIT, which goes through
                    # update_row and sets status='curated' WITHOUT bumping
                    # revision — without it the judge's rewrite of the now-stale
                    # text would overwrite the operator's own words and demote
                    # curated (operator intent) to promoted (machine).
                    # `touch_updated_at=False` keeps the row's place in the
                    # recency ranking build_recency_lines uses; the judgment
                    # rewrites text about an OLD conversation.
                    # An UNCERTAIN keep gets its rewritten text but stays `draft`
                    # (owner decision 4: keep and MARK) — the shipped `draft`
                    # badge already means "provisional, not operator-confirmed".
                    # `status=` must be passed explicitly: update_row's default
                    # is 'curated' (the F5 operator-edit contract), a provenance
                    # lie either way.
                    try:
                        written = store.update_row(
                            row["id"],
                            title=build_title(judged_text),
                            content=judged_text,
                            signature=build_signature(judged_text),
                            if_revision=row["revision"],
                            if_status="draft",
                            status="draft" if uncertain else "promoted",
                            touch_updated_at=False,
                            raising=True,
                        )
                    except sqlite3.Error:
                        # raising=True is what makes a plain False mean ONLY
                        # rowcount==0, so `stale` keeps its documented meaning
                        # ("the operator was speaking on this topic") instead of
                        # silently absorbing every locked-database write.
                        reasons["write_failed"] += 1
                        continue
                    if not written:
                        reasons["stale"] += 1
                        counts["stale"] += 1
                        continue
                    if uncertain:
                        reasons["uncertain_entity"] += 1
                    kept.append((row["id"], row["revision"]))

                # mark_judged, never set_flags: set_flags bumps updated_at, and
                # _prune_profile keeps the newest drafts by updated_at — routing
                # a rejection through it would evict a newer unjudged draft.
                # Keeps are counted from the writes that ALREADY landed on disk:
                # a failed bookkeeping stamp must never report kept=0 on a launch
                # that genuinely promoted rows (owner decision 8).
                if kept:
                    counts["kept"] = len(kept)
                    try:
                        stamped = store.mark_judged(kept, raising=True)
                    except sqlite3.Error:
                        reasons["write_failed"] += len(kept)
                    else:
                        # The text is already on disk; only the stamp is missing,
                        # so the row is simply re-judged next launch.
                        if stamped < len(kept):
                            reasons["unstamped"] += len(kept) - stamped
                if rejected:
                    try:
                        # `if_status="draft"` ONLY here. A reject performs no
                        # earlier guarded write, so this call carries BOTH of its
                        # race checks: `revision` catches a re-CAPTURE, `status`
                        # catches an operator EDIT or PIN (which write 'curated'
                        # WITHOUT bumping revision) — otherwise a memory he just
                        # curated is hidden on a judgment of the text he replaced,
                        # permanently, since upsert_draft's un-hiding CASE only
                        # fires on rows still in draft. It must NOT be passed on
                        # the keep call above: `update_row` has already written
                        # 'promoted' by then, so the same guard would leave every
                        # confident keep unstamped and re-judged every launch.
                        stamped = store.mark_judged(
                            rejected, inactive=True, if_status="draft", raising=True,
                        )
                    except sqlite3.Error:
                        reasons["write_failed"] += len(rejected)
                    else:
                        counts["rejected"] = stamped
                        lost = len(rejected) - stamped
                        if lost:
                            reasons["stale"] += lost
                            counts["stale"] += lost

            counts["unjudged_remaining"] = len(
                store.list_unjudged_drafts(profile_id, limit=MEMORIAS_PROFILE_CAP)
            )
            logger.info(
                "memoria promotion sweep: considered=%d kept=%d rejected=%d stale=%d "
                "remaining=%d reasons=%s",
                counts["considered"], counts["kept"], counts["rejected"],
                counts["stale"], counts["unjudged_remaining"], dict(reasons),
            )
        except Exception as exc:
            # Total isolation: nothing was marked judged, so the next launch
            # retries. Type only — never a message that could carry row text.
            logger.warning("memoria promotion sweep failed (fail-open): %s", type(exc).__name__)
        return counts

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
        if not self._is_local:
            # Cloud provider active: no local Ollama to introspect. Return an
            # empty response so `_discover_model_ctx` degrades to the fallback
            # ctx and `_check_capabilities_reasoning` reads no capabilities —
            # the sole ollama.show call site, so this gate covers both callers.
            return {}
        cache = getattr(self, "_ctx_show_cache", None)
        if cache is None:
            cache = {}
            self._ctx_show_cache = cache
        if model in cache:
            return cache[model]
        import ollama
        # Bounded by the SAME watchdog the chat call uses. ollama-python builds
        # its default client with timeout=None — its own docstring says so, and in
        # httpx that means wait forever — and `ollama.show(model)` takes no
        # timeout argument, so there is nowhere to pass one. This probe runs on
        # the foreground turn path BEFORE the inference watchdog is armed, so
        # without a bound of its own a busy daemon parks the turn with no
        # recovery. A hang raises nothing, which is why the callers' try/except
        # never covered it. Timeout is the metadata budget (/api/tags class),
        # not the 180s generation budget.
        # ponytail: a stall costs 2x this, because _check_capabilities_reasoning
        # calls _discover_model_ctx and then _fetch_show, and a failure is not
        # cached here. Bounded and once-per-model, so not worth restructuring.
        resp = self._call_with_watchdog(
            ollama.show,
            timeout=OLLAMA_REQUEST_TIMEOUT,
            label="OllamaShowProbe",
            model=model,
        )
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

    def _agenda_rejection_code(self) -> str:
        """WU4 4b: the rejection CODE only (e.g. "contains_internal_leak").

        Unlike `_format_agenda_rejection` (the human log line), this NEVER
        includes the overlap percentage or matched-phrase snippet — those can
        carry a fragment of generated dialogue. Safe to ride an event's
        `action` string.
        """
        ctl = getattr(self, "agenda_controller", None)
        if ctl is None or not ctl.rejection_log:
            return "unknown"
        last = ctl.rejection_log[-1]
        return str(last.get("guardrail", last.get("error", "unknown")))

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
        # AC3.3 (design-fase2.md §3 WU3 [v2]): any history commit invalidates an
        # in-flight/cached pregen — a reply generated against pre-commit history
        # must never speak. _speak_pregenerated already TOOK its own entry (via
        # _take_pregen_if_match at pop) BEFORE calling this, so its commit cannot
        # retro-invalidate the turn being spoken; it only kills OTHER stale
        # pregens. Bump the epoch (an in-flight worker's store dies on the epoch
        # check) AND clear a cached draft (the epoch alone does not cover it —
        # the pop-time take does not re-check the epoch).
        with self._prefetch_lock:
            self._prefetch_epoch += 1
            self._prefetched_agenda = None
            self._prefetch_done.clear()

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
        committed_memoria_capture: Optional[tuple[str, str, str, str, str]] = None

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
                    #
                    # FIX2 (memoria_quality_20260717): skip the memoria re-capture
                    # when this pair was already captured at commit-time (B1) —
                    # a byte-identical re-upsert only bumps revision/updated_at
                    # and flaps the panel order. The digest ledger above is a
                    # SEPARATE in-memory feature and is always appended.
                    if not self.historial[0].get("mem_captured", False):
                        pending_memoria_capture = self._build_memoria_draft(
                            evicted_user_content, evicted_asst_content,
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
            committed_user_entry = {
                'role': 'user', 'content': safe_context, 'source': source,
                'private': priv,
            }
            self.historial.append(committed_user_entry)
            self.historial.append({
                'role': 'assistant', 'content': dialogo, 'source': source,
                'private': priv,
            })

            # B1 (memoria_quality_20260717) — capture-at-commit: the pair that
            # just ENTERED historial becomes a memoria immediately, not only
            # when it is later evicted/flushed. This is the durability fix (F2):
            # a hard kill before eviction/close no longer loses the exchange.
            # Built under the lock (same discipline as eviction capture);
            # upserted AFTER the lock releases. Belt-and-braces with eviction +
            # flush: the stable_key upsert (ON CONFLICT(profile_id,stable_key)
            # ... WHERE status='draft') is idempotent, so a pair captured here
            # and again later is a no-op revision bump, never a duplicate row.
            committed_memoria_capture = self._build_memoria_draft(
                safe_context, dialogo, source=source, private=priv,
            )

        if pending_memoria_capture is not None:
            self._capture_memoria(*pending_memoria_capture)
        if committed_memoria_capture is not None:
            # FIX2 (memoria_quality_20260717): flag the just-committed pair ONLY
            # when the upsert actually persisted, so eviction/flush skip the
            # byte-identical re-upsert (revision churn / panel-order flapping).
            # On the fail-open path the flag stays unset → eviction/flush retain
            # their retry role (belt-and-braces preserved for the failure case).
            if self._capture_memoria(*committed_memoria_capture):
                committed_user_entry["mem_captured"] = True

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
        *,
        source: Optional[str],
        private,
    ) -> Optional[tuple[str, str, str, str, str]]:
        """Pure, no-I/O: full gate chain + draft builder for one (user,
        assistant) pair, or None if any gate fails.

        Single source of truth for R1-R4/RC-1, shared by eviction-capture
        (T3, above), B1 capture-at-commit (memoria_quality_20260717), and F4
        flush (slice 4 — close-flush and profile-switch both iterate live-window
        pairs through this same chain instead of duplicating gate logic). MUST
        be called while _history_lock is held — reads the pair's own tags
        (state-at-event), never a switch's current state. Order: source
        allowlist -> not agenda-sentinel -> MEMORIAS_ENABLED -> private tag is
        False -> profile_id set -> significant-token minimum (is_capturable, via
        derive_stable_key, LAST — a signal gate, never a substitute for
        provenance, Judge-B forward slice 2 N1). Returns (profile_id,
        stable_key, title, content, signature) or None. C1: content comes from
        _build_memoria_content (24-word user budget + first contentful Kira
        sentence), NOT the 8-word digest ledger line.
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
        # C1 (memoria_quality_20260717): user-side-alone signal gate — a pair
        # whose USER turn is a greeting/filler (< 2 significant tokens) dies
        # even when Kira's reply is rich. Distinct from is_capturable below,
        # which scores the COMBINED pair >= 3: a verbose Kira answer must not
        # drag a contentless "hola" into a stored memoria.
        if significant_token_count(user_content) < 2:
            return None
        # C1: skip canned guardrail-fallback assistant lines. D4 commits
        # guardrail-blocked exchanges to history so they are not lost, but the
        # fallback line itself carries zero memory signal. Literal match against
        # the active locale's fallback set (the same lines D4 actually speaks).
        if (asst_content or "").strip() in i18n_active.guardrail_fallback_lines():
            return None
        signature_text = f"{user_content} {asst_content}"
        stable_key = derive_stable_key(profile_id, signature_text)
        if stable_key is None:
            return None
        return (
            profile_id, stable_key, build_title(signature_text),
            self._build_memoria_content(user_content, asst_content),
            build_signature(signature_text),
        )

    def _capture_memoria(
        self, profile_id: str, stable_key: str, title: str, content: str, signature: str = ""
    ) -> bool:
        """I/O: upsert a memoria draft. MUST be called AFTER _history_lock releases.

        Returns True on a successful upsert, False on the fail-open path (so
        capture-at-commit can flag a pair only when it actually persisted, and
        a failure leaves eviction/flush their retry role — FIX2).

        Fail-open (R5): any exception is logged (ids/type only, never title
        or content — RC-8) and swallowed. A memorias write must never crash
        the calling thread (engine worker loop / agenda speaker daemon).
        """
        try:
            _row_id, created = self._get_memoria_store().upsert_draft(
                profile_id, stable_key, title, content, signature=signature,
                return_created=True,
            )
        except Exception as exc:
            logger.warning(
                "memoria capture failed (fail-open): %s profile_id=%s stable_key=%s",
                type(exc).__name__, profile_id, stable_key,
            )
            return False
        if created:
            # E1 (memoria_quality_20260717): chat-panel notice, fresh INSERT
            # only (revision==1). Runs on the engine thread at commit-time
            # (before TTS); ui_callback -> EngineHost._dispatch_motor_event,
            # whose handler chain includes the audit-log file append
            # (log_motor_accion) BEFORE the in-memory event ring — the same
            # bounded per-event cost every whitelisted motor status already
            # pays several times per turn, so one extra event is marginal but
            # NOT free. A refresh/re-upsert (created False) stays silent so
            # the feed never re-announces a memoria Kira already saved.
            # Guarded here (not the bare-callback idiom used elsewhere)
            # because this whole method is a fail-open boundary: the notice
            # must never crash the engine worker thread.
            try:
                self.ui_callback("memoria_captured")
            except Exception:
                pass
        # W2a (memoria_recall_20260718): record this session's captured title
        # for the mechanical session summary. Brief _history_lock (no I/O — the
        # store write above already released it) keeps this append consistent
        # with the snapshot+clear in set_profile / flush_memorias. Tracked on
        # every persisted capture (eviction + commit), never for a summary row
        # (those bypass this method), so a summary is never re-summarized.
        #
        # R3 (cross-profile contamination): the upsert above ran UNLOCKED, so a
        # concurrent set_profile could have snapshot+cleared the titles and
        # swapped _current_profile_id between the draft build (this profile_id)
        # and this locked append. Attribute the title ONLY while its profile is
        # still the active one; on mismatch skip silently — a bounded,
        # correctly-attributed loss (the departing summary misses one late
        # title) rather than leaking it into the arriving profile's summary.
        if title:
            with self._history_lock:
                if profile_id == self._current_profile_id:
                    self._session_memoria_titles.append(title)
                    if len(self._session_memoria_titles) > _SESSION_MEMORIA_TITLES_CAP:
                        del self._session_memoria_titles[0]
        return True

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
            # W2b/W4 (memoria_recall_20260718): a meta/temporal recall query
            # ("¿qué recordás de mí?", "de qué hablamos la sesión pasada") has
            # no topical anchor for select_top_k — route it to RECENCY (session
            # summaries first). W4 gating is structural: recency fires ONLY on a
            # detected meta query; a topical 0-match keeps injecting nothing
            # (owner mitigation — the existing select_top_k path, untouched).
            if is_meta_recall_query(contexto):
                lines = build_recency_lines(rows)
            else:
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
            # FIX2 (memoria_quality_20260717): a pair already captured at
            # commit-time (B1) is byte-identical on disk — skip the flush
            # re-upsert (revision churn / panel-order flapping). Unflagged
            # pairs (e.g. commit-capture failed, or appended directly) still
            # flush, preserving flush's belt-and-braces role.
            if user_entry.get("mem_captured", False):
                continue
            user_content = user_entry.get("content", "")
            asst_content = asst_entry.get("content", "")
            draft = self._build_memoria_draft(
                user_content, asst_content,
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
                # W2a: snapshot this session's titles + profile under the same
                # lock, then clear (a double close never writes two summaries).
                summary_profile_id = self._current_profile_id
                summary_titles = list(self._session_memoria_titles)
                self._session_memoria_titles.clear()
        except Exception:
            logger.warning("memoria close-flush snapshot failed (fail-open)")
            return

        # R4: one wall-clock budget spans the WHOLE flush (drafts + summary), so
        # the durable-tier summary write below can never push close past it.
        deadline = time.monotonic() + budget_seconds
        if drafts:
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

        # W2a: ONE mechanical session summary after the draft flush (single
        # bounded upsert on the calling thread — same sanctioned Tk-thread
        # exception as the flush above, RC-3). R4: attempt it only while a
        # write's worth of budget remains — a close under a fully spent budget
        # skips it silently (the next switch/close boundary covers the session)
        # so the summary honours "never blocks close".
        if deadline - time.monotonic() > _MEMORIA_SUMMARY_WRITE_BUDGET_SECONDS:
            self._write_session_summary(summary_profile_id, summary_titles)
        else:
            logger.debug(
                "memoria close-flush budget spent, skipping session summary (profile_id=%s)",
                summary_profile_id,
            )

    def _dispatch_switch_flush(
        self,
        drafts: list[tuple[str, str, str, str, str]],
        summary_profile_id: str | None = None,
        summary_titles: list[str] | None = None,
    ) -> None:
        """F4 (task 4.15) — dispatch profile-switch flush upserts to a
        dedicated worker thread, fire-and-forget (RC-2/RC-3).

        Runs off the Tk thread with NO time budget (task 4.16 — unlike
        close-flush, there IS more UI to protect, so a slow disk must never
        stall it; bounding by thread placement instead of wall-clock is
        sufficient here since nothing is waiting on this thread).

        W2a (memoria_recall_20260718): also carries the departing profile's
        session-summary payload, written on the SAME worker thread after the
        draft upserts. A worker is still spawned when there are no drafts to
        flush but enough tracked titles to summarize.
        """
        summary_titles = summary_titles or []
        if not drafts and len(summary_titles) < MEMORIAS_SUMMARY_MIN_TITLES:
            return
        threading.Thread(
            target=self._run_switch_flush,
            args=(drafts, summary_profile_id, summary_titles),
            daemon=True,
        ).start()

    def _run_switch_flush(
        self,
        drafts: list[tuple[str, str, str, str, str]],
        summary_profile_id: str | None = None,
        summary_titles: list[str] | None = None,
    ) -> None:
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
        # W2a: departing profile's mechanical session summary, same worker.
        self._write_session_summary(summary_profile_id, summary_titles or [])

    def _write_session_summary(self, profile_id: str | None, titles: list[str]) -> None:
        """W2a (memoria_recall_20260718) — write ONE mechanical (NO LLM) session
        summary memoria for *profile_id* from this session's captured *titles*.

        Runs on the close-flush (calling) thread and the switch-flush worker
        thread — the same bounded, fail-open teardown paths it rides — so it
        must never call the model (EngineHost.stop has a 2s flush budget; a
        profile switch must never block on inference) and never crash the
        caller. The write itself is a single bounded upsert (insert_summary,
        MemoriaStore.WRITE_TIMEOUT_SECONDS); it holds no internal wall-clock
        budget — on the close path flush_memorias gates the call on remaining
        budget (R4), so this durable-tier write never pushes app close past it.

        Tiering (the 200-row MEMORIAS_PROFILE_CAP): per-turn drafts are the
        EXPENDABLE pool (pruned oldest-first); this status='summary' row is the
        DURABLE tier the W2b meta-router surfaces on recall queries, capped
        separately by MemoriaStore._prune_summaries. Fewer than
        MEMORIAS_SUMMARY_MIN_TITLES captured memorias -> no summary.

        Silent by construction: unlike _capture_memoria it emits NO
        'memoria_captured' notice (this fires at teardown / off the Tk thread),
        and the summary row is written via insert_summary — never re-entering
        _session_memoria_titles — so it is never itself re-summarized.
        """
        if not profile_id:
            return
        clean = [t for t in titles if t and t.strip()]
        if len(clean) < MEMORIAS_SUMMARY_MIN_TITLES:
            return
        content = "; ".join(clean)
        title = build_title(content) or "session summary"
        try:
            self._get_memoria_store().insert_summary(
                profile_id, title, content, signature=build_signature(content),
            )
        except Exception as exc:
            logger.warning(
                "memoria session-summary write failed (fail-open): %s profile_id=%s",
                type(exc).__name__, profile_id,
            )

    @staticmethod
    def _first_words(text: str, max_words: int = 8) -> str:
        """Return the first N words of text, stripped of leading/trailing whitespace."""
        words = text.split()
        return " ".join(words[:max_words])

    @staticmethod
    def _first_sentence(text: str) -> str:
        """Return the first sentence of text (split on . ! ?)."""
        return _first_sentence(text)

    @classmethod
    def _build_ledger_line(cls, user_text: str, asst_text: str) -> str:
        """Compact an evicted turn into one ledger line body (no [hace N] prefix).

        The "[hace N turno(s)]" prefix is rendered at build time by
        MemoryDigest.build_block() so the counter always reflects distance-from-now.

        Format: contexto: <first words> → Kira: <first sentence>

        G3: the history-wrapper frame comes off before the 8-word budget is
        spent. historial stores the WRAPPED turn on purpose (the frame is the
        model's provenance cue), but "El streamer escribió: " is 3 words and
        "El streamer dijo (PTT): " is 4 — enough to drop the operator's whole
        symptom from the digest block, while the "contexto: ... → Kira: ..."
        format already says who said what.
        """
        user_summary = cls._first_words(strip_history_wrapper(user_text))
        kira_summary = cls._first_sentence(asst_text)
        ctx_label = i18n_active.ledger_context_label()
        kira_label = i18n_active.ledger_kira_label()
        return f"{ctx_label}: {user_summary} {kira_label}: {kira_summary}"

    # C1 (memoria_quality_20260717): closed leading-tic list stripped off the
    # Kira side before picking the first CONTENTFUL sentence. Kira's stock
    # openers ("Mirá vos," is 52% of legacy rows) carry zero memory signal and
    # would otherwise become the whole stored content. Comma-inclusive so we
    # only strip the opener, not a mid-sentence "Mirá".
    _MEMORIA_KIRA_LEADING_TICS = ("Mirá vos,", "Mirá,", "Che,", "Posta,")
    _MEMORIA_CONTENT_USER_WORDS = 24

    @classmethod
    def _contentful_kira_sentence(cls, asst_text: str) -> str:
        """First sentence of *asst_text* with >=3 significant tokens, after
        stripping a leading tic. Falls back to the first sentence when none
        qualify. Returns "" when the reply is empty OR collapses to nothing
        after tic-stripping (e.g. a tic-only reply like "Mirá vos,") — the
        caller (_build_memoria_content) then stores the user side alone, with
        no dangling Kira label."""
        stripped = (asst_text or "").lstrip()
        for tic in cls._MEMORIA_KIRA_LEADING_TICS:
            if stripped.startswith(tic):
                stripped = stripped[len(tic):].lstrip()
                break
        sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", stripped) if s.strip()]
        for sentence in sentences:
            if significant_token_count(sentence) >= 3:
                return sentence
        return sentences[0] if sentences else stripped.strip()

    @classmethod
    def _build_memoria_content(cls, user_text: str, asst_text: str) -> str:
        """C1 memoria body — richer than the 8-word digest ledger line.

        user side = first 24 words (digest keeps 8 — decoupled). Kira side =
        first CONTENTFUL sentence (see _contentful_kira_sentence). Capped at 300
        chars, preserving the pre-C1 content-length invariant.

        G3: the history-wrapper frame comes off here too, not only off the
        ledger line. It reads like useful provenance in the "Memoria de Kira"
        panel, but it is not: _DIGEST_CAPTURE_SOURCES is {"direct", "ptt"}, so
        every memoria's user side is the operator by construction (viewer chat
        is structurally barred), and "contexto:" already labels the side. All it
        actually does is spend 3-4 of the 24 words, then spend those same chars
        AGAIN inside build_injection_lines' recall budget when the row is
        re-injected. It also left the row self-inconsistent: stable_key, title
        and signature are all derived from STRIPPED text (memoria_store
        _significant_tokens), so only the body carried the tax. Modality
        (typed vs voice) belongs in a column if the operator ever needs it, not
        in a prose prefix that survives only by accident of which wrapper the
        caller happened to apply.
        """
        user_summary = cls._first_words(
            strip_history_wrapper(user_text), cls._MEMORIA_CONTENT_USER_WORDS
        )
        kira_summary = cls._contentful_kira_sentence(asst_text)
        ctx_label = i18n_active.ledger_context_label()
        # FIX3 (memoria_quality_20260717): append the "→ Kira: ..." segment ONLY
        # when Kira's side is non-empty. A tic-only reply collapses to "" — a
        # contentless Kira side simply isn't stored (no dangling label).
        if not kira_summary:
            return f"{ctx_label}: {user_summary}"[:300]
        kira_label = i18n_active.ledger_kira_label()
        return f"{ctx_label}: {user_summary} {kira_label}: {kira_summary}"[:300]

    @staticmethod
    def _sanitize_history_context(context: str) -> str:
        """Strip obvious prompt-injection attempts from chat context."""
        lowered = context.lower()
        for marker in INJECTION_MARKERS:
            if marker in lowered:
                return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]", "", context)[:300]
        return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]", "", context)[:800]

    def _ejecutar_inferencia(
        self,
        contexto,
        source: str = "direct",
        *,
        history_text: Optional[str] = None,
        stamp: Optional[TurnStamp] = None,
        bundle_followers: Optional[list] = None,
    ):
        # `bundle_followers` (interruptible_speech_architecture_20260804): the
        # ORIGINAL queue tuples absorbed behind this turn's head when an owner
        # bundle formed, forwarded so a non-committing return can hand them back
        # to _priority_queue instead of dropping them — they exist nowhere else.
        # None/empty on every other turn. See _requeue_owner_bundle_followers.
        #
        # §7 instrument: the request -> TTS-receives-text span. A LOCAL, not an
        # instance slot: _hablar has four callers and two of them run on
        # background threads (play_prefetched_agenda's detached speaker at :1883
        # and the CloudFallbackWarm worker at :3715), so a shared slot read
        # inside _hablar could be consumed by a turn that never opened it. This
        # is the only path that generates and speaks on one thread, and the only
        # one where the span means anything — a pregenerated draft is spoken much
        # later by design (that overlap IS the feature).
        request_start = time.monotonic()
        # C1 (refactor_core_api_20260802): the two submit-time fields now ride
        # in ONE optional TurnStamp instead of two independently-optional
        # kwargs. None (no stamp) is the same "never submitted through that
        # seam" state the old submitted_at=None / submitted_under_provider=None
        # pair encoded.
        submitted_at = stamp.submitted_at if stamp is not None else None
        submitted_under_provider = stamp.submitted_under_provider if stamp is not None else None
        # Unit 4.1 (runtime_findings_batch_20260731, F5): the ORIGINAL bug —
        # TURN_LATENCY measured engine-receipt -> TTS only, so the queue wait
        # before this method ever ran (13.8-29.1 min in the 2026-07-30 run) was
        # invisible. submitted_at (monotonic, stamped at the API dispatch entry
        # in dispatch.py) lets us report the FULL wait as an ADDITIVE field —
        # None for any item with no stamp (agenda-internal turns, the
        # accumulation-buffer flush) logs with no fake value, never a fake 0.
        queue_wait_ms: Optional[int] = None
        if submitted_at is not None:
            queue_wait_ms = max(0, int((request_start - submitted_at) * 1000))
        # Unit 4.2 (F12 closure): capture the ANSWERING provider posture right
        # before generation -- the same `_provider_config`/
        # `_cloud_fallback_active` snapshot `_generar_dialogo` takes a moment
        # later at its own entry (F15: collapsed to one `is_local` bool),
        # so this is honest disclosure of what generation is ABOUT to use,
        # not a second, possibly-drifted read taken after the (possibly
        # long) generation call returns. None unless this turn was actually
        # tagged at submit time (`submitted_under_provider`) -- an
        # agenda/accumulated turn was never "submitted" through that seam.
        answered_by_provider: Optional[str] = None
        answered_by_transport: Optional[str] = None
        provider_changed_while_queued: Optional[bool] = None
        if submitted_under_provider is not None:
            _answer_state = self.provider_runtime_state()
            answered_by_provider = _answer_state["provider"]
            answered_by_transport = _answer_state["transport"]
            provider_changed_while_queued = submitted_under_provider != answered_by_provider
        dialogo = self._generar_dialogo(contexto, source=source, commit_history=True, history_text=history_text)
        if dialogo:
            engine_ms = int((time.monotonic() - request_start) * 1000)
            if queue_wait_ms is not None:
                logger.info(
                    "[TURN_LATENCY] source=%s request_to_tts_ms=%d queue_wait_ms=%d request_to_tts_total_ms=%d",
                    source, engine_ms, queue_wait_ms, queue_wait_ms + engine_ms,
                )
                # Unit 4.2 (D3b): the documented bound is "current block's
                # remaining speech + one generation" (DIRECT_ANSWER_MAX_WAIT_
                # SECONDS, settings.py) -- NOT an enforcement timer, nothing is
                # cancelled or retried because of this. Metadata-only WARNING,
                # scoped strictly to source=="direct" per D3, so the claim
                # stays honest without ever touching the reply itself.
                if source == "direct" and queue_wait_ms > DIRECT_ANSWER_MAX_WAIT_SECONDS * 1000:
                    logger.warning(
                        "[DIRECT_WAIT_EXCEEDED] source=direct queue_wait_ms=%d bound_ms=%d",
                        queue_wait_ms, int(DIRECT_ANSWER_MAX_WAIT_SECONDS * 1000),
                    )
            else:
                logger.info(
                    "[TURN_LATENCY] source=%s request_to_tts_ms=%d",
                    source, engine_ms,
                )
            # FIX-B2: single emit chokepoint for every spoken live line — the
            # main reply AND the guardrail/repetition fallbacks all arrive here
            # as a non-empty `dialogo` and are spoken below, so last-reply stays
            # in sync with what Kira actually says. Agenda prefetch playback
            # emits from its own speaker (play_prefetched_agenda). Guarded, so a
            # raising callback never blocks speech. R8: Kira's own text only.
            emit_source = source if source.startswith("kira-agenda") else "kira"
            # C1 (refactor_core_api_20260802): _emit_dialogue's OWN branching
            # (kept, see its docstring) is entirely value-based — gated on
            # `queue_wait_ms is not None` / `submitted_under_provider is not
            # None`, not on how many kwargs the caller bothered to pass. So the
            # caller can pass every field unconditionally: an unstamped turn
            # has queue_wait_ms=None and the rest None too, which routes
            # _emit_dialogue into its own no-kwargs branch exactly as before —
            # a pinned dialogue_callback spy assertion (2 positional args)
            # never sees a surprise kwarg.
            self._emit_dialogue(
                dialogo, emit_source, queue_wait_ms=queue_wait_ms,
                answered_by_provider=answered_by_provider,
                answered_by_transport=answered_by_transport,
                submitted_under_provider=submitted_under_provider,
                provider_changed_while_queued=provider_changed_while_queued,
            )

            # Step 2: playback (and, with it, the chat spoken clock) moved
            # behind `_speak_or_submit`. Under the router this returns while
            # the job is still QUEUED, so nothing after this line may assume
            # speech completed — see `_complete_processing_cycle`'s idle gate
            # and `_drain_control_commands`' `_speech_active` gate.
            self._speak_or_submit(dialogo, source=source)
        elif source.startswith("kira-agenda"):
            # Empty or guardrail-blocked agenda generation: _generar_dialogo
            # returned "", so _hablar never runs and no speaking_start event
            # fires. Signal the failure through the SAME validator hook the
            # success path uses (_accept_agenda_output at line ~1156) so the
            # controller leaves GENERATING and its recovery ladder engages,
            # instead of stalling the autonomous loop silently.
            self._accept_agenda_output("")
        elif bundle_followers:
            # The same non-committing return, for an owner bundle: the head is
            # spent (exactly what a failed single turn spent before bundling)
            # and the followers go back to the queue rather than dying with this
            # frame. Mutually exclusive with the branch above by construction —
            # a bundle is tagged OWNER_BUNDLE_SOURCE, never "kira-agenda*".
            self._requeue_owner_bundle_followers(bundle_followers)

    @staticmethod
    def _sanitize_tts_text_for_playback(text: str) -> str:
        """Strip Markdown emphasis markers and non-Latin script glyphs, without
        deleting otherwise-speakable text.

        Screen/speech split: this runs inside _hablar_impl, AFTER _emit_dialogue
        already forwarded the original (unfiltered) string to the screen sink —
        the screen keeps CJK/etc glyphs, only the TTS-bound copy is filtered.
        """
        return _sanitize_tts_text_for_playback(text)

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

    def _log_clause_sanitizer(self, result, source: str, *, stage: str = "generate") -> None:
        """Metadata-only telemetry for the clause sanitizer.

        Owner rule: never log previews, raw dialogue, or the removed content —
        not even at DEBUG. Counts, ratios, source, stage and verdict only. These
        records are the evidence input for the threshold tuning pass and for any
        later decision to arm a non-agenda source.
        """
        logger.debug(
            "[CLAUSE_SANITIZER] stage=%s source=%s verdict=%s removed=%d distinct=%d "
            "max_occ=%d orig_len=%d remaining_len=%d removed_pct=%.3f",
            stage, source, result.verdict, result.removed_fragments,
            result.distinct_looping, result.max_occurrences,
            result.original_len, result.remaining_len, result.removed_pct,
        )

    def _speak_or_submit(self, dialogo, source: str = "direct", priority: Optional[int] = None) -> None:
        """The ONE seam every spoken line goes through (design §8 step 2).

        Router armed: hand the text to the router thread and return
        immediately — playback no longer occupies the calling thread.
        Kill switch OFF: the legacy direct, BLOCKING `_hablar`, byte-identical
        to what each of the four converted call sites did before.

        `priority` defaults to the source's band; a caller passes it
        explicitly when the source tag would lie about the content's urgency
        (see the cloud-fallback notice).
        """
        if not self._speech_router_enabled:
            self._hablar(dialogo, source=source)
            # Measure-first telemetry seam: a chat turn actually played to
            # completion — advance the spoken clock so secs_since_last_spoken
            # stays honest vs a should_call that may later expire via TTL.
            # Chat-only; no-op when unset. Intentionally NOT in a finally: if
            # `_hablar` raises (TTS failure) Kira did NOT speak, so the spoken
            # gap should keep growing — that growing gap is the very signal
            # that surfaces silent TTS failures to the operator. The router
            # reproduces exactly this rule at job completion (FINISHED only).
            if source == "chat" and self.on_chat_turn_spoken is not None:
                try:
                    self.on_chat_turn_spoken()
                except Exception:
                    pass
            return
        router = self._ensure_router()
        router.submit(
            dialogo,
            source,
            priority_for_source(source) if priority is None else priority,
        )

    def _ensure_router(self) -> "SpeechRouter":
        """Lazy-build-then-start, extracted from `_speak_or_submit` (step 3)
        so `pause_speech_for_ptt` can arm the router on a press that lands
        BEFORE any speech this process ever submitted — a hold must block
        `_pick()` even when nothing is playing yet. Idempotent; copies the
        step-3 kill switch onto the router at build time.
        """
        router = self._speech_router
        if router is None:
            with self._lock:
                if self._speech_router is None:
                    # No flag copy here (judge closure 2026-08-05): the
                    # router reads `_speech_interrupt_enabled` LIVE via its
                    # `interrupt_enabled` property, so the switch can be
                    # disarmed in-process and the host's two flag writes
                    # have no ordering requirement.
                    self._speech_router = SpeechRouter(self)
                router = self._speech_router
        router.start()  # idempotent
        return router

    def pause_speech_for_ptt(self) -> None:
        """PTT_DOWN entrypoint (design §0 row 1, §5.1): fail-open, a no-op
        unless BOTH switches are armed. Judge closure (2026-08-05, MAJOR):
        step 3 requires the router to BE the playback path — with only
        `_speech_interrupt_enabled` on (the documented router-only revert
        flips `_speech_router_enabled` alone), a press built a router no
        job would ever reach, and the closure-B1 re-check then silently
        deleted every LEGACY turn spoken during the hold (no stack to
        suspend to, no discard log). Router-off strictly dominates. The
        hold and the victim's request publish in ONE `_sched_lock`
        acquisition (`hold_and_pause`) — a press landing before this
        process ever spoke still blocks `_pick()`."""
        if not (self._speech_interrupt_enabled and self._speech_router_enabled):
            return
        self._ensure_router().hold_and_pause("ptt")

    def resume_speech_after_ptt(self) -> None:
        """PTT_UP entrypoint (design §5.1's `on_release` funnel): fail-open,
        a no-op unless BOTH switches are armed (same conjunction as the
        press — router-off dominates). Idempotent — every PTT exit path may
        call this, including a double-clear."""
        if not (self._speech_interrupt_enabled and self._speech_router_enabled):
            return
        router = self._ensure_router()
        router.set_ptt_held(False)

    def _speech_cancelled(self, source: str) -> bool:
        """True when an emergency path cancelled this source's speech.

        Extracted from `_hablar_impl`'s entry guard (Bug B straggler) so the
        router can refuse a job BEFORE any boundary event exists for consumers
        to react to (design §2 reconcile / §11 B1).
        """
        with self._lock:
            cancelled = self._cancelled_speech_prefixes
        return bool(cancelled) and any(source.startswith(p) for p in cancelled)

    def _speech_pause_pending(self) -> bool:
        """Closure B1 (2026-08-05): a pause/hold requested for the ACTIVE
        router job. `_hablar_impl` consults this right after (re)arming
        `_speaking` — an interrupt that landed in the router's pick→arm gap
        was stomped by that assignment, and this is what un-stomps it.
        Always False on the legacy path: the conjunction (judge closure
        2026-08-05) keeps this from cutting a LEGACY `_hablar_impl` call to
        silence when only the interrupt flag is armed — on the legacy path
        there is no job, no stack and no resume, so a True here deleted the
        turn outright."""
        if not (self._speech_interrupt_enabled and self._speech_router_enabled):
            return False
        router = self._speech_router
        if router is None:
            return False
        return router.pause_pending_for_active()

    def _speech_boundary_start(self, source: str) -> None:
        """Open a speech boundary: arm the cut flag, publish the source, emit
        `speaking_start`.

        Step 2 (design §11 B1): this LEFT `_hablar_impl`. A job is one boundary
        pair no matter how many `_hablar` invocations it takes (a retry today, a
        resume at step 3), and a zero-fragment job no longer emits a second
        `speaking_end` from inside the invocation. The emitter is the ROUTER
        when it is armed, and `_hablar` itself on the legacy direct path.

        A raising `speaking_start` consumer must not strand `_speaking` /
        `_current_speech_source` (pinned semantic, test_llm_engine_timeouts) —
        both are cleared here and the exception keeps propagating.
        """
        with self._lock:
            self._speaking = True
            self._current_speech_source = source
            # T1(a) [v5]: monotonic start of THIS turn's speech, paired with
            # `_speech_boundary_end` to record the previous turn's own speech
            # duration for the `speech_ms=` boundary telemetry field.
            self._speaking_start_monotonic = time.monotonic()
        try:
            self.ui_callback("speaking_start")
        except Exception:
            with self._lock:
                self._speaking = False
                self._current_speech_source = None
                self._speaking_start_monotonic = None
            logger.exception("UI callback failed during speaking_start")
            raise

    def _speech_boundary_end(self) -> None:
        """Close a speech boundary: disarm, clear the source, emit
        `speaking_end`. Counterpart of `_speech_boundary_start` (design §11 B1).

        `_current_speech_source` is cleared HERE, not in `_hablar_impl`'s tail:
        `agenda_driver._has_non_agenda_audio_work` reads `is_speaking` and
        `current_speech_source` together, and clearing the tag while the job is
        still ACTIVE would read as "non-agenda audio work".
        """
        with self._lock:
            self._speaking = False
            self._current_speech_source = None
            now = time.monotonic()
            self._last_speaking_end_monotonic = now
            # T1(a) [v5]: this turn's own speech duration, recorded as the
            # "previous turn" value the NEXT boundary's speech_ms reads.
            if self._speaking_start_monotonic is not None:
                self._last_speech_duration_ms = int((now - self._speaking_start_monotonic) * 1000)
            self._speaking_start_monotonic = None
        self.ui_callback("speaking_end")

    def _hablar(self, texto_a_generar, source: str = "direct", *, emit_boundary: bool = True,
                pre_split: Optional[list] = None, cursor_base: int = 0):
        """WU2b belt lock (design-fase2.md §2.5): serialize every _hablar caller.

        Thin wrapper around _hablar_impl. After WU2 the engine worker is the ONLY
        caller in the API host, so the non-blocking acquire always succeeds there
        and contention is never logged (a contention log is a bypass regression).
        In the CTK legacy path (play_prefetched_agenda's speaker thread) this lock
        serializes that thread against the worker — the log then = the lock
        working. Both call sites are terminal/non-recursive (neither _hablar_impl
        nor its callbacks re-enter _hablar), so the non-reentrant Lock never
        self-deadlocks; it is released in finally on every exit path.

        Step 2: `emit_boundary=False` is the ROUTER's call — it owns one
        `speaking_start`/`speaking_end` pair per JOB and would otherwise get a
        second pair per invocation (design §11 B1). Every other caller keeps the
        invocation-scoped pair, which is exactly today's behavior.

        Step 3: `pre_split`, when not None, is forwarded to `_hablar_impl`
        verbatim — a resume replays the router's own owed slice, never a
        re-chunked rejoin (design §1 resolution 1).
        """
        if self._speech_cancelled(source):
            # Bug B fix: refuse a turn whose source was cancelled during its
            # generation phase (already popped from the priority queue, so
            # drop_pending_sources can't reach it). Checked BEFORE the boundary
            # opens so an emergency-stopped straggler emits no event pair at all.
            self._log(f"Habla suprimida (cancelada): source={source}", level="warning")
            return SpeechOutcome(chunks=[], cursor=0, spoken=[], skipped=[], interrupted=False, error=None)
        # The boundary pair lives INSIDE the belt-lock critical section — the
        # same discipline `_hablar_impl` had when it emitted the pair itself.
        # Emitting `speaking_start` before the acquire (or `speaking_end`
        # after the release) lets two concurrent legacy callers (CTK's
        # `play_prefetched_agenda` speaker, `CloudFallbackWarm`) interleave
        # nested pairs, clobber `_current_speech_source` mid-playback, and cut
        # the lock holder's utterance via the unlocked `_speaking = False`.
        if not self._hablar_lock.acquire(blocking=False):
            self._log("hablar contention (serialized)")
            self._hablar_lock.acquire()
        try:
            if emit_boundary:
                self._speech_boundary_start(source)
            try:
                # Self-caught: forward `pre_split`/`cursor_base` ONLY when
                # actually resuming (non-None). tests/test_pregen_pop_cache.py
                # stubs `_hablar_impl` with the pre-step-3 2-arg signature;
                # always forwarding `pre_split=None` broke that pre-existing,
                # unrelated stub with a TypeError. The router is the only
                # caller that ever passes a non-None slice.
                if pre_split is not None:
                    return self._hablar_impl(
                        texto_a_generar, source=source,
                        pre_split=pre_split, cursor_base=cursor_base,
                    )
                return self._hablar_impl(texto_a_generar, source=source)
            finally:
                if emit_boundary:
                    self._speech_boundary_end()
        finally:
            self._hablar_lock.release()

    def _hablar_impl(self, texto_a_generar, source: str = "direct", *,
                     pre_split: Optional[list] = None, cursor_base: int = 0):
        # Bug B fix (second line of defence): the token can still be set in the
        # window between the caller's own check and this frame. Mid-playback
        # truncation is handled separately by the _speaking guard in the
        # consumer loop via interrupt_speaking().
        with self._lock:
            cancelled = self._cancelled_speech_prefixes
        if cancelled and any(source.startswith(p) for p in cancelled):
            self._log(f"Habla suprimida (cancelada): source={source}", level="warning")
            return SpeechOutcome(chunks=[], cursor=0, spoken=[], skipped=[], interrupted=False, error=None)

        with self._lock:
            # `_speaking` SURVIVES as this method's cut mechanism (design §11):
            # the consumer loop and interrupt_speaking() both key off it. It is
            # not renamed and not replaced by `_speech_active`.
            self._speaking = True
            self._current_speech_source = source

        # Closure B1 (2026-08-05): a pause/hold published between the
        # router's window check and the re-arm above had its
        # interrupt_speaking() STOMPED by that assignment — the whole
        # utterance would play into a live mic. Re-honor it here, before any
        # fragment synthesises; the producer/consumer loop only ever
        # re-reads `_speaking`, and nothing re-arms it after this point.
        if self._speech_pause_pending():
            with self._lock:
                self._speaking = False

        # WU5 D3 (design-fase2.md §3 WU5): while an interruption ANSWER's TTS
        # plays (GPU free — generation already finished), opportunistically
        # generate the return connector. Placed HERE, at playback start, so it
        # never races the turn's own generation (single-Ollama rule). No-op
        # unless a frozen return is pending and this is a non-agenda turn.
        if not source.startswith("kira-agenda"):
            self._maybe_generate_connector_upgrade()

        ruta_absoluta_ref = os.path.abspath(self.voz_referencia) if self.voz_referencia else ""

        if pre_split is not None:
            # Step 3 (design §1 resolution 1): a RESUME. The chunker is NOT a
            # fixpoint — re-splitting `' '.join(pre_split)` can silently
            # re-merge comma-separated fragments across the join (a >25-word
            # comma-heavy fragment shrinks under the cap once its head is
            # dropped, so the splitter stops sub-splitting it and merges two
            # owed chunks into one). The router already knows the exact owed
            # slice; replay it verbatim, never re-derive it. Skips the
            # sanitizer/quote-newline scrub/splitter entirely.
            oraciones = list(pre_split)
        else:
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
            # Step 2 (design §11 B1): the THIRD speaking_end site. It used to
            # fire from in here, so a zero-fragment job completed the agenda
            # turn (and idled the avatar) while the router still held the job
            # ACTIVE, then emitted a second end at FINISHED. The boundary owner
            # emits the single pair now.
            with self._lock:
                self._speaking = False
            return SpeechOutcome(chunks=[], cursor=0, spoken=[], skipped=[], interrupted=False, error=None)

        self._log(f"Sintetizando {len(oraciones)} fragmento(s) con pipeline...")
        start_tts = time.time()
        # WU4 4c seam (design-fase2.md §3): live progress counters for
        # speech_remaining_estimate() — updated as each fragment finishes
        # playing (below), cleared at speaking_end. `first_play` (T2(b)
        # [v5]) is set at the FIRST fragment's actual playback start, below.
        with self._lock:
            # Judge closure (2026-08-05, convergent): TURN-relative, never
            # slice-relative. On a resume `oraciones` is only the owed tail;
            # without `cursor_base` every played/total consumer (the WU5
            # zone gate, the would-fire telemetry) read a nearly-finished
            # resumed turn as barely started and un-earned its LATE-zone
            # defer protection. `base` anchors the slice so
            # `speech_remaining_estimate`'s per-fragment rate stays local
            # to THIS invocation (a pre-hold rate is stale after a hold of
            # unknown length).
            self._speech_progress = {
                "total": cursor_base + len(oraciones), "played": cursor_base,
                "start": start_tts, "first_play": None, "base": cursor_base,
            }

        cola_audios = queue.Queue(maxsize=3)
        error_count = 0
        # Step 1 ledgers (design §3/§6 I1): every fragment index ends in
        # exactly one of spoken/skipped/pending. `skipped` is mutated from
        # BOTH threads (the producer's _drop below, and the consumer's
        # playback-exception handler further down) -- safe without an extra
        # lock because list.append() is a single atomic bytecode op in
        # CPython, same tolerance the existing unsynchronized `error_count`
        # counter already relies on.
        skipped: list = []

        def productor():
            nonlocal error_count

            def _drop(i: int, why: str, exc: Optional[Exception] = None) -> None:
                # Step 0 (design §5.3, §8 step 0): the single collapse point
                # for every synthesis-side chunk loss. PRIVACY: `why` is a
                # closed set of short reason tags, never fragment/chat text.
                # `exc`, when given, restores the traceback for unknown/heavy
                # failures (design §12 observability) via exc_info -- passing
                # None is a no-op for logging, so this needs no branch.
                nonlocal error_count
                logger.warning(f"[SPEECH_LOST] idx={i} reason={why} source={source}", exc_info=exc)
                error_count += 1
                skipped.append(i)
                cola_audios.put(None)

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
                            _drop(i, "piper_synth_failed")
                    else:
                        _drop(i, "piper_unavailable")
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
                            _drop(i, "piper_synth_failed")
                    else:
                        # Piper gone / never loaded.
                        _drop(i, "piper_unavailable")
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
                            _drop(i, "heavy_http_error")

                except requests.exceptions.ConnectionError:
                    self._log("ERROR: Servidor Qwen3-TTS no disponible.", level="error")
                    _drop(i, "heavy_connection_error")
                    continue

                except requests.exceptions.Timeout:
                    _drop(i, "heavy_timeout")

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
                                _drop(i, "edge_fallback_piper_synth_failed")
                        else:
                            self._log(
                                "TTS local no disponible: instala piper-tts y "
                                "configura TTS_LOCAL_MODEL_PATH",
                                level="warning",
                            )
                            _drop(i, "edge_fallback_piper_unavailable")
                        continue

                    if effective_motor == "ligero":
                        self._log("ERROR: Edge-TTS requiere internet. Si estas offline usa Pesado (Qwen3-TTS).", level="error")
                        _drop(i, "edge_ligero_failed")
                        continue
                    _drop(i, "heavy_unexpected_error", e)

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
        # Step 1 ledgers (design §3 SpeechOutcome, §6 I1/I4). `cursor` defaults
        # to clean completion (== len(oraciones)) and is overwritten ONLY at an
        # actual cut point below -- never derived from `chunks_played`, which
        # increments before the interrupted break and never advances past a
        # failed synthesis (the CURSOR TRAP, design §3). `last_idx` lets the
        # rare pre-dequeue guard (no item in hand yet) still report a sane
        # best-effort cursor.
        spoken: list = []
        cursor = len(oraciones)
        last_idx = -1
        was_interrupted = False
        outcome_error = None
        try:
            while True:
                # Bug 4 fix: check _speaking before dequeuing the next chunk.
                # emergency_stop() sets _speaking=False externally; without this
                # guard the consumer drains the entire pre-filled queue even after
                # teardown is requested.
                with self._lock:
                    if not self._speaking:
                        was_interrupted = True
                        # ponytail: best-effort cursor -- no item is in hand at
                        # this guard, so the exact next idx is unknowable
                        # without dequeuing. Assumes no skip landed exactly in
                        # this narrow inter-fragment window. Harmless today:
                        # nothing resumes from cursor until the router lands
                        # (design §8 step 3+); revisit only if that ever needs
                        # to be exact.
                        cursor = last_idx + 1
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
                        was_interrupted = True
                        cursor = item[1]  # the fragment about to play, never started
                        try:
                            if os.path.exists(item[0]):
                                os.remove(item[0])
                        except (OSError, TypeError):
                            pass
                        break

                archivo_chunk, idx, oracion_texto = item
                last_idx = idx

                try:
                    if chunks_played == 0:
                        elapsed_first = time.time() - start_tts
                        self._log(f"🔊 Primer fragmento listo en {elapsed_first:.2f}s. Reproduciendo...")
                        # T2(b) [v5]: mark the FIRST fragment's real playback
                        # start (monotonic) — speech_remaining_estimate uses
                        # this as its baseline instead of `start` (set before
                        # synthesis even began) so a slow-to-synthesize first
                        # fragment never inflates the per-fragment mean.
                        with self._lock:
                            if self._speech_progress is not None:
                                self._speech_progress["first_play"] = time.monotonic()

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
                    with self._lock:
                        if self._speech_progress is not None:
                            # Turn-absolute (judge closure 2026-08-05):
                            # invocation-local count + the resume offset.
                            self._speech_progress["played"] = cursor_base + chunks_played

                    if interrupted:
                        # CURSOR TRAP (design §3): this is the cut MID-AUDIO --
                        # `idx` (never chunks_played) is the fragment owed a
                        # full replay, so it counts as CUT, NOT spoken.
                        was_interrupted = True
                        cursor = idx
                        logger.info(f"[SPEECH_STACK] would-push source={source} cursor={idx}")
                        break
                    spoken.append(idx)

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
                    # Step 1 (design §5.3): a playback exception loses this ONE
                    # fragment (skipped), never the rest of the job -- no
                    # control-flow change, the loop continues to the next item.
                    skipped.append(idx)
                finally:
                    try:
                        if os.path.exists(archivo_chunk):
                            os.remove(archivo_chunk)
                    except OSError:
                        pass

        except queue.Empty:
            self._log("⚠️ Timeout esperando chunks de audio.", level="warning")
            outcome_error = "queue_empty_timeout"
            # Design §12: cursor must stay honest on this exit too -- a
            # healthy heavy-TTS server dribbling a chunk past the timeout, or
            # a dead producer, must never report the clean-completion
            # signature (cursor == len(oraciones)).
            cursor = last_idx + 1
        except Exception as e:
            self._log(f"ERROR en reproducción: {e}", level="error")
            logger.exception("Error en consumidor de audio")
            outcome_error = str(e)
            # Design §12: same honesty requirement as the queue.Empty exit.
            cursor = last_idx + 1
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
            self._speech_progress = None
        # Step 2 (design §11 B1): `speaking_end`, the speech-source clear and
        # the boundary telemetry all moved to `_speech_boundary_end`, which the
        # JOB owner calls — once per job, after this invocation returns.
        # Step 1 (design §3/§8 step 1): every exit path returns a
        # SpeechOutcome. capture-and-discard only -- `_hablar` propagates this
        # verbatim and every existing caller ignores it (verified by grep),
        # so nothing is stored and nothing resumes yet.
        # Design §12 (defect 2): the producer can run up to ~4 indices ahead
        # of the consumer (queue maxsize=3 + in-hand); a drop at an index the
        # cut never reached must return to PENDING, not double-count as both
        # skipped AND pending. Snapshot-copy + filter at this single return
        # site so every exit path (clean/cut/error) gets it. On clean
        # completion cursor == len(chunks), so every skipped index is
        # already < cursor and this is a no-op.
        skipped_final = sorted(i for i in set(skipped) if i < cursor)
        return SpeechOutcome(
            chunks=oraciones,
            cursor=cursor,
            spoken=spoken,
            skipped=skipped_final,
            interrupted=was_interrupted,
            error=outcome_error,
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
    ) -> None:
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
        """
        if self.dialogue_callback is None:
            return
        try:
            if queue_wait_ms is not None:
                if submitted_under_provider is not None:
                    self.dialogue_callback(
                        text, source, queue_wait_ms=queue_wait_ms,
                        answered_by_provider=answered_by_provider,
                        answered_by_transport=answered_by_transport,
                        submitted_under_provider=submitted_under_provider,
                        provider_changed_while_queued=provider_changed_while_queued,
                    )
                else:
                    self.dialogue_callback(text, source, queue_wait_ms=queue_wait_ms)
            else:
                self.dialogue_callback(text, source)
        except Exception:
            logger.exception("dialogue_callback failed")

    def _log(self, msg, level="info"):
        prefix = "[IA]"
        self.log_queue.put(f"{prefix} {msg}")
        getattr(logger, level)(f"Motor: {msg}")
