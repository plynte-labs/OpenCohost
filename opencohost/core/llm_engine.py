"""
opencohost/core/llm_engine.py
"""
import contextlib
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
from dataclasses import dataclass, field
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
    LLM_STREAMING_ENABLED,
)
from opencohost.core.context import context_budget
# Module import, never a from-import of the values: STREAM_OVER_AGENDA /
# STREAM_TTL_SECONDS are runtime-mutable (PUT /api/stream/chat-live/limits
# rebinds them), and the functions read the module globals live.
from opencohost.core import turn_priority
from opencohost.core.providers.cloud import cloud_llm_client
from opencohost.core.profiles import personalization
from opencohost.core.scheduling.turn_stamp import TurnStamp
from opencohost.core.speech.tts_sanitizer import _first_sentence, _sanitize_tts_text_for_playback
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
from opencohost.config.validation import log_non_negotiable_block, output_guard
from opencohost.core.engine.llm_engine_scout import ScoutPromotionMixin
from opencohost.core.engine.llm_engine_memorias import MemoriaCaptureMixin
from opencohost.core.engine.llm_engine_agenda import AgendaStashMixin
from opencohost.core.engine.llm_engine_models import ModelManagementMixin
from opencohost.core.engine.llm_engine_cloud import CloudFallbackMixin
from opencohost.core.engine.llm_engine_pregen import PregenCacheMixin
from opencohost.core.engine.llm_engine_speech import SpeechPipelineMixin

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

# tauri_stream_chat_20260812 §3.2 phase 2 (owner Q3): sources whose prompt
# keeps ONLY the assistant slots of `historial` — Kira's own last replies —
# and drops the user slots. Unlike the five allowlists above, this is a
# DENY-list of exactly the evidenced source: in the 2026-08-12 42-turn Twitch
# session (logs/opencohost_20260812_194539.log) every chat prompt carried
# ~2400 chars of stored user slots that were pure waste — a chat user slot is
# _sanitize_history_context's 800-char cut of the ~1200-char RF3 task
# template, so the chat fence and all viewer content fall past the cut — and
# the source-mixed deque made chat replies continue agenda monologues instead
# of the chat (22:07:04, 22:13:52, at 2838-2906 prompt tokens). Assistant
# slots MUST stay: FIX 2 (_finalize_generation's chat repetition guard) and
# the model's own don't-repeat-yourself signal both live on them — gate the
# APPEND, never the history_snapshot in _GenerationSetup.
# NOT widened to "accumulated" (also viewer chat) or unknown sources: the
# owner ruled on stream turns only, and a mutating gate ships armed only
# where the incident evidence is. A plain constant, not a turn_priority-style
# runtime knob: stream-vs-agenda order is a streamer preference; what history
# a chat prompt carries is a settled engine decision with no live-tuning ask.
_HISTORY_ASSISTANT_ONLY_SOURCES = frozenset({"chat"})

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


def _output_guard_with_tts_check(text: str, source: str) -> tuple[bool, str]:
    """output_guard runs on the RAW LLM text, but `_sanitize_tts_text_for_
    playback` strips markdown emphasis (`*x*`) LATER, only inside
    `_hablar_impl`. R9/R4's patterns join tokens with `\\s+`, and `*` is not
    whitespace, so a markdown-wrapped violation ('Como *IA*, no puedo
    opinar.') passes the raw-text guard and is then spoken clean once the
    sanitizer strips the asterisks. Guard BOTH surfaces: raw text is what
    reaches `historial`/`/api/chat/last-reply`, the sanitized copy is what
    actually reaches TTS.

    The sanitized pass only runs when the raw pass already allowed the text
    (cheap: no double-sanitizing, no sanitizing when raw already blocked). A
    block only the sanitized pass caught is tagged `[tts-sanitized]` so it
    reads as distinct from a raw-text block in the log line.
    """
    allowed, reason = output_guard(text, source=source)
    if not allowed:
        return allowed, reason
    sanitized = _sanitize_tts_text_for_playback(text)
    allowed, reason = output_guard(sanitized, source=source)
    if not allowed:
        return allowed, f"[tts-sanitized] {reason}"
    return True, ""


# output_guard's reason strings carry the rule id in brackets
# ("Non-negotiable violation [no_ai_self_identification]: ..."). The character
# class deliberately excludes "-" so the "[tts-sanitized]" tag a
# _output_guard_with_tts_check block prepends is never mistaken for a rule id.
_GUARD_RULE_ID_RE = re.compile(r"\[([a-z0-9_]+)\]")


def _guard_rule_id(reason: str) -> str:
    match = _GUARD_RULE_ID_RE.search(reason or "")
    return match.group(1) if match else "unknown_rule"


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
class _StreamAttemptState:
    """Per-attempt state of the streaming loop (llm_output_streaming_20260813
    design §3/§5/§7). Carries everything the divergence points downstream need:
    the lazily-created job (None until the first clean sentence closes — which
    is what keeps every pre-commit failure path byte-identical to the buffered
    path), the appended-sentence prefix (what Kira is owed to have said on
    air), and the trip/abort verdicts the finalize divergence gates on.

    `contexto`/`history_text` ride here because the partial-turn exit (§7)
    commits the spoken prefix to history from exception paths that never reach
    `_finalize_generation`.
    """
    source: str
    contexto: object = None
    history_text: Optional[str] = None
    router: object = None
    job: Optional[SpeechJob] = None
    appended_sentences: list = field(default_factory=list)
    sentence_index: int = 0
    # Pre-submit guard trip (§5 "before anything is spoken"): silently revert
    # to buffered semantics — keep consuming the stream, never append.
    consume_only: bool = False
    # Post-submit guard trip (§5 "after audio is out"):
    # (rule_id, tripping sentence index, spoken_upto).
    trip: Optional[tuple] = None
    # Cancel token / append_chunks refusal (§6): the turn is dead.
    abort_reason: Optional[str] = None
    # True once the partial-turn exit (or the finalize divergence) ran, so the
    # orphan belt in _generar_dialogo's catch-all never double-commits.
    handled: bool = False

    def spoken_prefix(self) -> str:
        return " ".join(self.appended_sentences).strip()


@dataclass
class _GenerationAttemptOutcome:
    """_cloud_attempt_loop's result. `early_return` mirrors the original
    method's own control flow: every early `return` inside the retry loop
    (watchdog timeout, transport failure exhausting the retry budget)
    returned exactly `""`, so `is not None` on this field is the one check
    the orchestrator needs to reproduce that identically -- it is NOT a
    truthiness check, since `""` itself is a valid (falsy) early-return
    value that must still short-circuit finalize.

    `stream` is None for every buffered attempt; a stream-eligible attempt
    carries its `_StreamAttemptState` so `_finalize_generation` can gate the
    §5/§7 divergence points on `stream.job` (llm_output_streaming_20260813).
    """
    raw_content: str = ""
    respuesta: object = None
    early_return: Optional[str] = None
    stream: Optional["_StreamAttemptState"] = None


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


class MotorVocalIA(
    ScoutPromotionMixin,
    MemoriaCaptureMixin,
    AgendaStashMixin,
    ModelManagementMixin,
    CloudFallbackMixin,
    PregenCacheMixin,
    SpeechPipelineMixin,
    threading.Thread,
):
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
        # ...and the temp file those bytes are spilled to once, because
        # winsound refuses SND_ASYNC from memory (see _ptt_cue_wav_file).
        self._ptt_cue_wav_path = None

        # Priority queue: (priority, timestamp, payload, source)
        # priority tiers (turn_priority module, tauri_stream_chat_20260812):
        #   0 = PTT/voice, 1 = direct (always above stream), 2/3 = stream vs
        #   agenda — their relative order is the STREAM_OVER_AGENDA setting.
        self._priority_queue: list = []
        self._pq_lock = threading.Lock()
        self._pq_max_items: int = 5
        # Base TTL for non-exempt sources. Stream ("chat") items do NOT use
        # this any more — their window is turn_priority.effective_stream_ttl()
        # (configurable + agenda-first floor); direct has its own bound.
        self._pq_ttl_seconds: float = 30.0
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

        # WU5 D2/D3 (ADR-038): interruption detour + connector-based return.
        # All guarded by _prefetch_lock. _frozen_stash holds the NEXT-turn
        # agenda draft moved OUT of the active _prefetched_agenda slot when a
        # direct turn takes the slot, so it survives the interactive
        # detour (exempt from the _commit_history epoch bump and interactive-pregen
        # eviction, which only touch _prefetched_agenda) and the answer's own
        # pregen can use the freed slot. {payload, dialogo, source, priority,
        # gen_ms, connector}. _detour_turns counts interactive turns spoken
        # since the freeze (D2 skip when it exceeds RETURN_MAX_DETOUR_TURNS).
        # _connector_last_idx rotates the D3 floor pool without immediate repeats.
        self._frozen_stash: Optional[dict] = None
        self._detour_turns: int = 0
        # R1 deadline backstop: monotonic timestamp of the last freeze, so the
        # driver can bound how long a return may HOLD when the interruption answer
        # never reaches a _note_detour_turn site. None when no stash is frozen.
        self._frozen_stash_at: Optional[float] = None
        self._connector_last_idx: Optional[int] = None
        # Speech-router host flag (interruptible_speech_architecture_20260804
        # §8 step 2): EngineHost turns it ON, CTK never does. OFF is the kill
        # switch — `_speak_or_submit` falls back to a direct, blocking `_hablar`.
        self._speech_router_enabled: bool = False
        # Built (and its daemon thread started) on the FIRST routed submit.
        self._speech_router = None
        # llm_output_streaming_20260813 §3: per-turn handoff between
        # _finalize_generation and _ejecutar_inferencia — the SpeechJob of a
        # turn whose audio already went through submit_streaming/append, so
        # _speak_or_submit must NOT run again for it. Single engine thread:
        # written by finalize, read-and-cleared by _ejecutar_inferencia.
        self._streamed_turn_job: Optional[SpeechJob] = None
        # llm_output_streaming_20260813 §7: the spoken prefix of a turn that
        # took the partial exit. Streaming creates a state that could not
        # exist before -- generation returned "" and yet audio ALREADY went
        # out -- so the two consumers of an empty return need to tell that
        # case apart from a genuinely silent drop. Same single-thread
        # write/read-and-clear discipline as `_streamed_turn_job`.
        self._streamed_turn_prefix: Optional[str] = None
        # Orphan belt (§7): the live _StreamAttemptState while a streamed
        # attempt runs. If an exception escapes after the first submit but
        # before finalize handled the job, _generar_dialogo's catch-all uses
        # this to seal + commit the spoken prefix instead of leaving the
        # router starving forever on an unsealed job.
        self._live_stream_state: Optional["_StreamAttemptState"] = None
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
            self._log(f"FATAL: No se pudo inicializar pygame.mixer: {e}", level="error")
            return

        self._check_ollama_service()
        if TTS_LOCAL_MODEL_PATH:
            self._piper.load()
        # llm_output_streaming_20260813 (design.md §1 non-goals, §10): pay
        # Piper's 2.25s first-synthesis cost off-air now instead of on the
        # session's first turn. Same neighbourhood as the a8830bb chat-client
        # pre-warm above, but on a daemon thread -- that one is a cheap object
        # construction while this one is real inference, and the "Motor IA
        # inicializado" line below must not wait 2.25s for it. `_prewarm_tts`
        # no-ops when Piper never loaded, so this needs no TTS_LOCAL_MODEL_PATH
        # guard of its own.
        threading.Thread(target=self._prewarm_tts, name="TTSPrewarm", daemon=True).start()
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
                #
                # Tier split (tauri_stream_chat_20260812): the sibling
                # expression was `0 if ptt else 1`, which promoted ANY
                # non-ptt source (chat included) into the direct tier.
                # Resolve the item's OWN documented tier instead — same
                # principle the Step-4 fix above established for PTT.
                busy_priority = turn_priority.dispatch_priority_for_source(source)
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
        priority: Optional[int] = None,
        source: str = "chat",
        history_text: Optional[str] = None,
        submitted_at: Optional[float] = None,
        submitted_under_provider: Optional[str] = None,
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
        """
        if priority is None:
            # Resolved per item at enqueue time. A mid-session flip of
            # STREAM_OVER_AGENDA leaves already-queued items with their old
            # numbers until served — bounded by _pq_max_items (5) and one or
            # two pops, so the misorder is transient; retagging the queue
            # under _pq_lock for that window is not worth the machinery.
            priority = turn_priority.dispatch_priority_for_source(source)
        with self._pq_lock:
            self._priority_queue.append(
                (priority, time.time(), payload, source, history_text, submitted_at, submitted_under_provider)
            )
            self._priority_queue.sort(key=lambda x: (x[0], x[1]))
            # Enforce max items — drop lowest priority (highest number) first,
            # breaking ties by newest timestamp. PTT (0) and direct (1) are
            # always preserved over stream/agenda (2/3, whichever way the
            # streamer ordered them). interruptible_speech_architecture_20260804
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

    def replace_pending(self, payload: str, priority: Optional[int] = None, source: str = "chat") -> None:
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

        AMENDED for streamed bundle turns (llm_output_streaming_20260813 phase
        2, owner decision 1, ratified 2026-08-13). On a bundle that STREAMS and
        dies after its first `submit_streaming`, the composed answer may have
        addressed a follower inside the already-spoken prefix, so requeuing it
        can answer that question twice on air. The owner ruled: requeue anyway.
        For those turns the invariant reads "a question can never be LOST; it
        may be double-answered on a partially-spoken streamed bundle death."
        Rationale on the record — loss is the incident-grade failure this
        function exists to prevent, a double answer is recoverable social
        awkwardness on air, and a mid-stream death implies a short spoken
        prefix. Scope is strictly streamed bundle turns: on the buffered path
        nothing was spoken before the failure, so "neither lost nor
        double-answered" holds there exactly as written above.

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
                    #
                    # Stream ("chat") items read the LIVE configurable window
                    # (turn_priority.effective_stream_ttl), not the static
                    # _pq_ttl_seconds. The effective value is FLOORED when the
                    # streamer selects agenda-first — without that floor, the
                    # "agenda first + short TTL" combination expires every
                    # stream item that waits out an agenda monologue and the
                    # co-host goes MUTE toward its audience with no visible
                    # error anywhere (the §3.2 TRAP). See turn_priority.
                    if source == "direct":
                        ttl = DIRECT_ANSWER_MAX_WAIT_SECONDS
                    elif source == "chat":
                        ttl = turn_priority.effective_stream_ttl()
                    else:
                        ttl = self._pq_ttl_seconds
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
                    # F8 optional (runtime_findings_batch_20260807): a pregen
                    # hit skips _ejecutar_inferencia entirely, so it never hit
                    # the [TURN_LATENCY] emit above -- 6 of ~15 answers were
                    # invisible to the metric. `submitted_at` is already in
                    # hand from the unpack above; this is the tts-handoff
                    # instant for a cache hit. path=pregen distinguishes it
                    # from the foreground metric's two-field split (no
                    # separate generation phase to subtract here).
                    if submitted_at is not None:
                        logger.info(
                            "[TURN_LATENCY] source=%s request_to_tts_total_ms=%d path=pregen",
                            source, int((time.monotonic() - submitted_at) * 1000),
                        )
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
        priority sort (0=PTT, 1=direct, 2/3=stream-vs-agenda per the
        streamer's STREAM_OVER_AGENDA setting; see `enqueue()` and the
        turn_priority module) is
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
        (the agenda tier), even if an agenda block was already re-queued earlier
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
                    # Only _OWNER_QUESTION_SOURCES reach here (guard above), so
                    # this resolves to ptt=0 / direct=1 — same numbers as the
                    # old `0 if ptt else 1`, now via the single tier resolver.
                    payload, priority=turn_priority.dispatch_priority_for_source(source),
                    source=source, history_text=history_text,
                    submitted_at=submitted_at, submitted_under_provider=submitted_under_provider,
                )
                self._log(f"Boundary drain: turno {source} movido a cola prioritaria (D3b).", level="debug")


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
        # F1 (interruptible_speech_architecture_20260804, runtime-findings
        # 2026-08-07): a CLOUD attempt failing here used to burn the turn
        # outright (return ""), even though `_handle_cloud_failure` had just
        # armed a known-good LOCAL fallback for every SUBSEQUENT turn — the
        # owner's question was the one turn that never got to use it. This
        # loop is the generation funnel all three cloud-failure exits pass
        # through (watchdog timeout / transport error -> outcome.early_return;
        # well-formed-but-empty 2xx body -> _finalize_generation's own ""),
        # so ONE retry here covers all three. The burn-the-turn policy for
        # LOCAL persistent failures (:3055-3057, `_requeue_owner_bundle_
        # followers`'s docstring) is untouched: `attempt_was_cloud` is only
        # True when THIS attempt actually dispatched to the cloud transport,
        # so a local failure never loops. `cloud_fallback_retry_done` bounds
        # it to exactly one retry per call.
        cloud_fallback_retry_done = False
        try:
            while True:
                attempt_was_cloud = not is_local
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
                    result
                    or cloud_fallback_retry_done
                    or not attempt_was_cloud
                    or not self._cloud_fallback_active
                    or self._cloud_fallback_reason != cloud_llm_client.CLOUD_ERROR_TRANSIENT
                ):
                    return result
                # The cloud attempt above failed and `_handle_cloud_failure`
                # engaged auto-fallback (fallback_mode=="manual" leaves
                # `_cloud_fallback_active` False, which the check above
                # already excludes) -- retry ONCE on the known-good local
                # model instead of discarding the question.
                #
                # Ceiling (runtime evidence 2026-08-07, F1/F12): retry is
                # TRANSIENT-ONLY. All three evidenced silent-turn incidents
                # were watchdog timeouts (class=transient); bad_key and
                # ambiguous_429 turns still burn (the pre-existing contract).
                # `_cloud_fallback_reason` is the class `_handle_cloud_failure`
                # was JUST invoked with for THIS attempt -- set synchronously
                # under `_lock` before that call's background warm-up thread
                # even starts (~5230), unlike `_last_cloud_failure_class`,
                # which the watchdog-timeout branch never touches and which a
                # rescued LOCAL success on the retry itself clears (finalize,
                # ~4985) before this gate could read it. Widening this ceiling
                # to other failure classes needs owner ratification first.
                cloud_fallback_retry_done = True
                is_local = True
                request_model = self._last_known_good_model or self.current_model
                self._log(
                    f"cloud_fallback_retry: retrying turn locally with "
                    f"{request_model} (source={source}).",
                    level="warning",
                )

        except Exception as e:
            self._log(f"ERROR Ollama: {e}", level="error")
            logger.exception("Error en inferencia LLM")
            # llm_output_streaming_20260813 §7 orphan belt: if a streamed turn
            # created a job and the exception escaped every closer hook (e.g.
            # finalize itself raised), the job must still be sealed and the
            # spoken prefix committed — an unsealed ACTIVE job would starve
            # the router forever (no idle, mouth stuck "speaking").
            _orphan = self._live_stream_state
            if _orphan is not None and _orphan.job is not None and not _orphan.handled:
                try:
                    self._stream_partial_exit(
                        _orphan, reason=f"engine_error:{type(e).__name__}"
                    )
                except Exception:
                    logger.exception("stream orphan belt failed")
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
        #
        # §3.2 phase 2 gate: a stream turn appends assistant slots only (see
        # _HISTORY_ASSISTANT_ONLY_SOURCES for the evidence). The filter lives
        # HERE and not on history_snapshot: the snapshot rides to
        # _finalize_generation, where FIX 2 derives recent_outputs from its
        # assistant entries — filtering the snapshot itself would blind the
        # guard that exists precisely because chat reactions repeat.
        #
        # Owner ruling 2026-08-13: of those assistant slots, the AGENDA-sourced
        # ones collapse to the most recent one. Symptom it fixes (log
        # opencohost_20260812_194539, 22:07:04): Kira answers a viewer and then
        # keeps monologuing the agenda topic without ever mentioning the chat,
        # because three inherited agenda turns outweighed the one comment she
        # was actually replying to. Keeping the LAST one — not zero — is
        # deliberate: it is what she just said out loud on stream, so she stays
        # coherent with it instead of contradicting herself mid-segment.
        #
        # Entries without a `source` key (stream_admin_ui.py:1340 appends one
        # such shape) fail OPEN and are kept: an untagged slot is not proven to
        # be agenda, and dropping it would silently shrink the window.
        history_assistant_only = source in _HISTORY_ASSISTANT_ONLY_SOURCES
        last_agenda_idx = -1
        if history_assistant_only:
            for idx, msg in enumerate(history_snapshot):
                if msg['role'] == 'assistant' and str(msg.get('source', '')).startswith('kira-agenda'):
                    last_agenda_idx = idx
        for idx, msg in enumerate(history_snapshot):
            if history_assistant_only:
                if msg['role'] != 'assistant':
                    continue
                if idx != last_agenda_idx and str(msg.get('source', '')).startswith('kira-agenda'):
                    continue
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
        contexto=None,
        history_text: Optional[str] = None,
    ) -> "_GenerationAttemptOutcome":
        """Phase 2 of _generar_dialogo (refactor_core_api_20260802 B7): the
        max_intentos retry loop, including the cloud transport-failure
        classification + rate-limited Retry-After retry branch, moved as ONE
        piece with its comments intact. Every early `return ""` here becomes
        `early_return=""` on the outcome -- the orchestrator propagates it
        identically via `if outcome.early_return is not None: return ...`.

        `contexto`/`history_text` exist only for the streaming path
        (llm_output_streaming_20260813): they ride to the §7 partial-turn
        exit, which commits the spoken prefix from exception paths that never
        reach finalize. A buffered turn never reads them.
        """
        messages = setup.messages
        opciones_llm = setup.opciones_llm
        chat_timeout = setup.chat_timeout
        max_intentos = setup.max_intentos
        _effective_ctx = setup.effective_ctx
        raw_content = ""
        respuesta = None
        stream_state: Optional["_StreamAttemptState"] = None

        # llm_output_streaming_20260813 §3 eligibility gate. `commit_history`
        # already tags every speculative path (pregen worker, connector
        # upgrade) False, so those stay buffered by construction; agenda is
        # phase 3 and chat is never (§8); cloud is phase 4 (§8);
        # `_speech_router_enabled` is the CTk/kill-switch gate and
        # LLM_STREAMING_ENABLED the revert lever. An ineligible turn takes the
        # buffered call below byte-identically.
        #
        # Phase 2 (§10) adds OWNER_BUNDLE_SOURCE. `_process_priority_queue`
        # relabels a bundled turn to that source before calling
        # _ejecutar_inferencia, so the allow-list is the ONLY thing that ever
        # gated bundles — the phase-1 `_turn_bundle_followers` check beside it
        # was redundant and is gone with the attribute. A bundle that dies
        # after its first submit requeues every follower (owner decision 1);
        # the fork is resolved in _ejecutar_inferencia, which still has the
        # followers as a local parameter.
        stream_eligible = (
            is_local
            and source in ("direct", "ptt", OWNER_BUNDLE_SOURCE)
            and commit_history
            and self._speech_router_enabled
            and LLM_STREAMING_ENABLED
        )

        for intento in range(max_intentos):
            # WU3 (design-fase2.md §2.3): mark Ollama busy tightly around the
            # actual generation call, cleared in finally on every exit path.
            with self._lock:
                self._llm_generating = True
            try:
                if stream_eligible:
                    respuesta, stream_state = self._run_streaming_attempt(
                        setup,
                        source=source,
                        request_model=request_model,
                        contexto=contexto,
                        history_text=history_text,
                    )
                    if stream_state.abort_reason is not None:
                        # §6: cancel token or append refusal — the turn is
                        # dead. Post-submit, _run_streaming_attempt already
                        # sealed and committed the spoken prefix; pre-submit
                        # nothing was spoken and nothing committed. Either
                        # way this is a non-committing return for the
                        # pregen epoch, same idiom as the watchdog branch.
                        if commit_history:
                            self._invalidate_pregen_epoch()
                        return _GenerationAttemptOutcome(early_return="")
                else:
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

        return _GenerationAttemptOutcome(
            raw_content=raw_content, respuesta=respuesta, stream=stream_state
        )

    def _run_streaming_attempt(
        self,
        setup: "_GenerationSetup",
        *,
        source: str,
        request_model: str,
        contexto,
        history_text: Optional[str],
    ) -> tuple:
        """One stream-eligible generation attempt (llm_output_streaming_20260813
        §3): iterate `_ollama_chat_streaming` synchronously on the engine
        thread, close sentences with the B_ws-parity SentenceSplitter, and run
        each closed sentence through the load-bearing order cancel → sanitize →
        guard(sanitized, sentence granularity) → fragment → lazy submit/append.

        Returns `(respuesta, state)`: `respuesta` is the final (done=true)
        chunk with `message.content` substituted by the accumulated text so
        every downstream telemetry read works unchanged, and `state` carries
        the job/trip/abort verdicts for the §5/§7 divergence points.

        The generator is closed on EVERY exit path via contextlib.closing —
        the socket close is the only real server-side abort for Ollama (§6),
        so it must never be left to garbage collection. Any exception after
        the first submit takes the §7 partial-turn exit (seal, commit the
        spoken prefix, one metadata log line) and then re-raises unchanged, so
        the attempt loop's watchdog/transport classification — including
        `_recover_from_stalled_inference` — still runs exactly as today.
        """
        state = _StreamAttemptState(
            source=source, contexto=contexto, history_text=history_text
        )
        self._live_stream_state = state
        splitter = SentenceSplitter()
        accumulated = ""
        accumulated_thinking = ""
        final_chunk = None
        stream = self._ollama_chat_streaming(
            timeout=setup.chat_timeout,
            model=request_model,
            messages=setup.messages,
            keep_alive=LLM_KEEP_ALIVE,
            options=setup.opciones_llm,
        )
        try:
            with contextlib.closing(stream):
                stopped = False
                for chunk in stream:
                    final_chunk = chunk
                    msg = getattr(chunk, "message", None)
                    delta = (getattr(msg, "content", "") or "") if msg is not None else ""
                    accumulated += delta
                    accumulated_thinking += (
                        (getattr(msg, "thinking", "") or "") if msg is not None else ""
                    )
                    if state.consume_only:
                        # §5 pre-submit trip: buffered semantics — keep
                        # consuming to completion, never append.
                        continue
                    for sentence in splitter.feed(delta):
                        action = self._handle_stream_sentence(sentence, state, setup)
                        if action == "revert":
                            state.consume_only = True
                            break
                        if action != "continue":
                            stopped = True
                            break
                    if stopped:
                        # §5/§6: abort the stream — closing() closes the
                        # socket, which frees the single Ollama runner.
                        break
                if not stopped and not state.consume_only:
                    for sentence in splitter.flush():
                        action = self._handle_stream_sentence(sentence, state, setup)
                        if action != "continue":
                            break
        except BaseException as exc:
            # §7: after the first submit the turn can no longer "not have
            # happened" — seal at the last appended sentence and commit the
            # spoken prefix BEFORE re-raising into the attempt loop's
            # unchanged watchdog/transport handling. Pre-submit (no job),
            # every failure keeps its exact legacy semantics: re-raise only.
            if state.job is not None and not state.handled:
                self._stream_partial_exit(state, reason=type(exc).__name__)
            raise
        if state.abort_reason is not None and state.job is not None and not state.handled:
            self._stream_partial_exit(state, reason=state.abort_reason)
        if state.job is not None:
            # The closing bookend to [STREAM_TTFA]. Without it a clean streamed
            # turn logs when it STARTED speaking and never logs when it stopped,
            # so the one comparison this whole track exists to make — audible at
            # N ms against the buffered path's audible-at-total — is not
            # computable from the log. `sentences` also turns TTFA into a rate.
            # The exception paths are covered by [STREAM_PARTIAL_EXIT] instead;
            # a pre-submit revert never creates a job and is a buffered turn,
            # already covered by [TURN_LATENCY].
            logger.info(
                "[STREAM_DONE] source=%s sentences=%d total_ms=%d outcome=%s",
                source,
                len(state.appended_sentences),
                max(0, int((time.time() - setup.start_llm) * 1000)),
                "trip" if state.trip else ("abort" if state.abort_reason else "clean"),
            )
        respuesta = self._rebuild_stream_response(
            final_chunk, accumulated, accumulated_thinking
        )
        return respuesta, state

    def _handle_stream_sentence(
        self, sentence: str, state: "_StreamAttemptState", setup: "_GenerationSetup"
    ) -> str:
        """One closed sentence through the §3 loop, in EXACTLY this order:

        1. cancel-token check (§6) — set ⇒ abort the turn;
        2. sanitize, THEN guard — the guard MUST see the SANITIZED sentence
           (guarding raw text reproduces the markdown evasion: 'Como *IA*, no
           puedo opinar.' passes the raw guard, the sanitizer strips the
           asterisks, and the broadcast line is the one R9 exists to block),
           and at SENTENCE granularity, never fragment granularity (the
           >25-word comma sub-split cuts inside R4/R3-discourse patterns);
        3. fragment — the `_fragment_for_tts` stage only;
        4. lazy submit / append — the first clean sentence creates the job
           (which is the instant the turn becomes committing, §7).

        Returns "continue" | "revert" | "truncate" | "abort".
        """
        source = state.source
        if self._speech_cancelled(source):
            state.abort_reason = "speech_cancelled"
            return "abort"
        state.sentence_index += 1
        pieces = [p for p in self._sanitize_for_tts(sentence) if p.strip()]
        sanitized = " ".join(p.strip() for p in pieces)
        if not sanitized:
            return "continue"
        # BOTH forms, exactly like the buffered path (`f07b360`) and the
        # full-text backstop below: raw-only misses the markdown evasion
        # ('Como *IA*, no puedo opinar.' passes R9 because its tokens join on
        # `\s+` and `*` is not whitespace), and sanitized-only would miss
        # anything the sanitizer happens to mangle out of a pattern. Guarding
        # only the sanitized form would leave the streamed path WEAKER than
        # the buffered one on that second axis, which is exactly the kind of
        # asymmetry this track must not introduce.
        allowed, reason = _output_guard_with_tts_check(sentence, source=source)
        if not allowed:
            if state.job is None:
                # §5 "before anything is spoken": silently revert the whole
                # turn to buffered semantics. No job is ever created;
                # _finalize_generation then runs exactly as today (full
                # guard, retry nudge, canned fallback, non-committing "").
                return "revert"
            state.trip = (
                _guard_rule_id(reason),
                state.sentence_index,
                len(state.appended_sentences),
            )
            return "truncate"
        fragments = self._fragment_for_tts(pieces)
        if not fragments:
            # Clean but unspeakable (len<=3 filter): nothing owed to the air.
            return "continue"
        if state.job is None:
            state.router = self._ensure_router()
            state.job = state.router.submit_streaming(
                source, priority_for_source(source)
            )
            if not state.router.append_chunks(state.job, fragments):
                state.abort_reason = "append_refused"
                return "abort"
            state.appended_sentences.append(sanitized)
            # Size travels WITH the timing or the timing is unreadable: 1.5s
            # to first audio on a four-word opener and 1.5s on a forty-word one
            # are different systems, and only the second is evidence that the
            # sentence boundary is where the latency actually lands. Metadata
            # only — counts, never the text.
            logger.info(
                "[STREAM_TTFA] source=%s first_audio_submit_ms=%d "
                "first_sentence_words=%d first_sentence_chars=%d fragments=%d",
                source,
                max(0, int((time.time() - setup.start_llm) * 1000)),
                len(sanitized.split()),
                len(sanitized),
                len(fragments),
            )
            return "continue"
        if not state.router.append_chunks(state.job, fragments):
            state.abort_reason = "append_refused"
            return "abort"
        state.appended_sentences.append(sanitized)
        return "continue"

    def _stream_partial_exit(self, state: "_StreamAttemptState", *, reason: str) -> None:
        """§7 partial-turn exit for a streamed turn that dies AFTER its first
        submit: seal the job at the last appended sentence and commit the
        spoken prefix to history — Kira said it on air; the next prompt must
        know. Never regenerates, never retries. Metadata-only log line."""
        try:
            state.router.seal(state.job)
        except Exception:
            logger.exception("stream partial exit: seal failed")
        prefix = state.spoken_prefix()
        if prefix:
            self._commit_history(
                state.contexto, prefix, source=state.source,
                history_text=state.history_text,
            )
            # Publish it for `_ejecutar_inferencia`: this turn returns "" but
            # its head ALREADY aired, so the owner must not be told the turn
            # was dropped and the transcript must show what the audience
            # actually heard.
            self._streamed_turn_prefix = prefix
        logger.warning(
            "[STREAM_PARTIAL_EXIT] source=%s reason=%s spoken_upto=%d "
            "sentences_closed=%d committed=%s",
            state.source, reason, len(state.appended_sentences),
            state.sentence_index, bool(prefix),
        )
        state.handled = True
        self._live_stream_state = None

    @staticmethod
    def _rebuild_stream_response(final_chunk, accumulated: str, accumulated_thinking: str):
        """§3 'at stream end': the final (done=true) chunk carries eval_count /
        eval_duration / prompt_eval_* (earlier chunks have them as None), so
        keep that chunk and substitute `message.content` with the accumulated
        text. Every downstream `getattr(respuesta, "prompt_eval_count", 0)`
        and the ctx_utilization / prefill-decode / load_ms telemetry then
        work unchanged. A zero-chunk stream degrades to a dict-shaped empty
        response, which the attempt loop's empty-retry branch already handles.
        """
        if final_chunk is None:
            return {"message": {"content": accumulated, "thinking": accumulated_thinking}}
        if isinstance(final_chunk, dict):
            msg = final_chunk.setdefault("message", {})
        else:
            msg = getattr(final_chunk, "message", None)
        if isinstance(msg, dict):
            msg["content"] = accumulated
            msg["thinking"] = accumulated_thinking
        elif msg is not None:
            msg.content = accumulated
            msg.thinking = accumulated_thinking
        return final_chunk

    def _apply_stream_guard_verdict(
        self, state: "_StreamAttemptState", dialogo: str, source: str
    ) -> str:
        """The §5/§7 finalize divergence for a turn whose audio is already on
        air (a job exists). Two cases:

        - No in-loop trip: run the unchanged full-text backstop. Allowed ⇒
          success — seal the job (chunks are final) and return the full text.
        - In-loop trip, or the backstop tripped: truncation protocol — one
          metadata-only log line (rule id, tripping sentence index,
          spoken_upto) via log_non_negotiable_block, seal at the last clean
          sentence, and return the spoken prefix for the normal commit flow.
          NO `_retry_after_guard_block` (its output has no relationship to the
          spoken prefix — an audible non-sequitur) and NO canned fallback line
          (the head already filled the air).
        """
        trip = state.trip
        if trip is None:
            allowed, reason = _output_guard_with_tts_check(dialogo, source=source)
            if allowed:
                state.router.seal(state.job)
                state.handled = True
                self._live_stream_state = None
                self._streamed_turn_job = state.job
                return dialogo
            # Backstop trip at finalize: the sentence index is unknown by
            # construction (the per-sentence pass allowed every sentence).
            trip = (_guard_rule_id(reason), -1, len(state.appended_sentences))
        rule_id, sentence_index, spoken_upto = trip
        # Metadata only in `preview` — deliberately never the blocked text.
        log_non_negotiable_block(
            rule_id,
            "stream_truncation",
            preview=f"sentence_index={sentence_index} spoken_upto={spoken_upto}",
        )
        state.router.seal(state.job)
        state.handled = True
        self._live_stream_state = None
        self._streamed_turn_job = state.job
        return state.spoken_prefix()

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
            # llm_output_streaming_20260813: Ollama reports the COLD MODEL LOAD
            # separately from prefill, and this line never read it -- so a slow
            # turn was one undifferentiated lump with no way to tell "the model
            # had to be loaded" from "the model stalled". That distinction is
            # what the streaming stall detector has to be built on: with
            # LLM_KEEP_ALIVE="7m" and `_prepare_model` called ONLY at startup,
            # on a switch and on cloud fallback (never on the turn path), any
            # idle gap over 7 minutes makes the NEXT turn pay a cold load
            # inside the chat call itself. Measure it before choosing a timeout.
            _load_ms = (getattr(respuesta, "load_duration", 0) or 0) / 1e6
            _ec_final = getattr(respuesta, "eval_count", 0) or 0
            logger.info(
                "ctx_utilization: model=%s prompt_eval_count=%d native_ctx=%d effective_ctx=%d ratio=%.3f "
                "load_ms=%.0f prefill_ms=%.0f decode_ms=%.0f eval_count=%d source=%s",
                request_model, _pec_final, _native_ctx, _effective_ctx, _util,
                _load_ms, _prefill_ms, _decode_ms, _ec_final, source,
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
                "load_ms": _load_ms,
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
        # `generation` is the ENGINE's model label and stays that on a cloud
        # turn -- logs/opencohost_20260804_191446.log reads
        # `generation=gemma4:e4b provider=nvidia_nim transport=cloud`, which
        # names the tier, not what NVIDIA actually ran. That made the 49-64s
        # latencies CLOUD_CHAT_TIMEOUT was sized against (settings.py:137-143)
        # unattributable two weeks later, when a provider-model swap to
        # z-ai/glm-5.2 pushed a measured call to 123.69s and there was no way
        # to say what the old number had been measured on.
        # Cloud-only, because on a local turn `generation` already IS the model.
        # A model id is a public name, never a credential -- the key lives in
        # LLM_KEYS_FILE and never comes near this string.
        if trace_transport == "cloud":
            profiles = provider_cfg.get("profiles")
            profile_cfg = profiles.get(trace_provider) if isinstance(profiles, dict) else None
            cloud_model = (
                profile_cfg.get("model") if isinstance(profile_cfg, dict) else None
            ) or "unknown"
            trace_msg += f" cloud_model={cloud_model}"
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
            if streamed_job is None:
                dialogo = san.text
            # else: verdict-log-only (§5) — the ADR-039 counters accrued in
            # the _log_clause_sanitizer call above, but _repair_sentence
            # mutations are never applied to a turn that is already speaking.
        if streamed_job is not None:
            # §5/§7: the per-sentence guard already ran on every sanitized
            # sentence in the streaming loop; this applies the full-text
            # backstop / truncation protocol and seals the job. It never
            # returns an empty string (a job implies >=1 appended sentence),
            # so the retry/fallback blocks below are structurally skipped.
            dialogo = self._apply_stream_guard_verdict(outcome.stream, dialogo, source)
            allowed, guard_reason = True, ""
        else:
            allowed, guard_reason = _output_guard_with_tts_check(dialogo, source=source)
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
                retry_allowed, _ = _output_guard_with_tts_check(retry_content, source=source)
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
        # llm_output_streaming_20260813: fresh turn — clear the streamed-turn
        # handoffs BEFORE generating so a stale value from a direct
        # _generar_dialogo call (tests, future callers) can never leak into
        # this turn's speak decision. Single engine thread, so the write/read
        # pair cannot interleave with another turn.
        self._streamed_turn_job = None
        self._streamed_turn_prefix = None
        dialogo = self._generar_dialogo(
            contexto, source=source, commit_history=True, history_text=history_text
        )
        streamed_job = self._streamed_turn_job
        self._streamed_turn_job = None
        streamed_prefix = self._streamed_turn_prefix
        self._streamed_turn_prefix = None
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
            #
            # The TRUE source travels — this used to relabel every non-agenda
            # source to "kira" right here, which destroyed the only record of
            # whether a reply answered chat, direct or PTT before any consumer
            # could read it. The uniform "kira" attribution is a PRESENTATION
            # concern of the one thing that surfaces it, so it now lives at that
            # boundary (ChatReplySink.record, api/engine_host.py) where the
            # published `source` field is still byte-identical. Anything wired
            # here that wants uniform attribution must derive it the same way,
            # NOT push the relabel back up into the engine.
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
                dialogo, source, queue_wait_ms=queue_wait_ms,
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
            #
            # llm_output_streaming_20260813 §3: a turn that STREAMED already
            # owes its audio to the router's growing job (submitted lazily at
            # the first clean sentence, sealed at finalize) — submitting the
            # full text again would speak the whole turn twice.
            if streamed_job is None:
                self._speak_or_submit(dialogo, source=source)
        else:
            # llm_output_streaming_20260813 §7. `streamed_prefix` is NOT part
            # of the chain below — phase 2 (owner decision 1) proved it must
            # not be. A streamed BUNDLE that partial-exits has both a spoken
            # prefix and followers, and as an `elif` the prefix branch
            # swallowed the requeue and lost every absorbed owner question
            # silently: the exact incident-grade failure
            # `_requeue_owner_bundle_followers` exists to prevent. Emitting
            # and requeueing are independent facts about the same turn, so
            # they are independent statements.
            if streamed_prefix:
                # Partial-turn exit: generation returned "" but this turn's
                # head ALREADY went out through the router's growing job, and
                # `_stream_partial_exit` already sealed it and committed the
                # prefix to history.
                #
                # The chain below was written on the premise that an empty
                # return means SILENCE — the `turn_dropped` toast says so in
                # its own comment ("instead of leaving them to infer it from
                # silence"). That premise is false here: the audience heard
                # the head, so the transcript must show what was spoken.
                self._emit_dialogue(streamed_prefix, source)
            if source.startswith("kira-agenda"):
                # Empty or guardrail-blocked agenda generation: _generar_dialogo
                # returned "", so _hablar never runs and no speaking_start event
                # fires. Signal the failure through the SAME validator hook the
                # success path uses (_accept_agenda_output at line ~1156) so the
                # controller leaves GENERATING and its recovery ladder engages,
                # instead of stalling the autonomous loop silently.
                self._accept_agenda_output("")
            elif bundle_followers:
                # The same non-committing return, for an owner bundle: the head
                # is spent (exactly what a failed single turn spent before
                # bundling) and the followers go back to the queue rather than
                # dying with this frame. Mutually exclusive with the branch
                # above by construction — a bundle is tagged
                # OWNER_BUNDLE_SOURCE, never "kira-agenda*".
                #
                # Phase 2: this now ALSO runs when the bundle streamed and died
                # after its first submit, on top of the emit above. Owner
                # decision 1, 2026-08-13 — see `_requeue_owner_bundle_followers`.
                self._requeue_owner_bundle_followers(bundle_followers)
            elif source in ("ptt", "direct") and not streamed_prefix:
                # F1 companion (runtime-findings 2026-08-07): a plain ptt/direct
                # turn matches no branch above when generation still comes back
                # empty (guardrail block with no fallback line, or both the cloud
                # attempt AND its one-shot local retry failing) -- tell the owner
                # the turn was dropped instead of leaving them to infer it from
                # silence. Not when a prefix aired: it audibly did not drop.
                self.ui_callback("turn_dropped")


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
