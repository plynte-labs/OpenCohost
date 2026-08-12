"""Creator Configuration Contract — validation and non-negotiable enforcement.

All 10 non-negotiable rules enforced at 4 independent layers:

  Layer 1 — ``validate_config()``    : rejects configs that structurally violate
  Layer 2 — ``runtime_check()``      : blocks messages at decision points
  Layer 3 — ``output_guard()``       : blocks TTS output that violates rules
  Layer 4 — ``log_non_negotiable_block()`` : records every blocked action

No LLM calls.  Structural checks only (regex patterns for content,
dataclass field checks for config).
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import re
import threading
import time
from typing import Any

from opencohost.config import settings
from opencohost.config.logger import SensitiveDataFilter, log_formatter
from opencohost.config.schema import (
    ActionPolicy,
    ChatEvent,
    CreatorConfig,
    EventAction,
)

logger = logging.getLogger(__name__)

_VALIDATION_FILE_HANDLER_NAME = "opencohost-config-validation-file"


def setup_validation_logging() -> None:
    """Idempotently attach persistence + redaction to this module's logger.

    ``logging.getLogger(__name__)`` above resolves to
    ``opencohost.config.validation`` -- a DIFFERENT tree from the configured
    ``OpenCohost`` engine logger (opencohost/config/logger.py) and from the
    ``opencohost.api`` tree ``api/observability.py::setup_api_logging()``
    covers. With no handler attached, every ``logger.warning(...)`` call in
    this module -- notably ``log_non_negotiable_block``'s redacted block
    preview -- falls through to ``logging.lastResort``: stderr-only,
    unpersisted, and UNREDACTED. Mirrors ``setup_api_logging()``'s idempotent
    named-handler guard; called once below at import time, since this module
    has no app-startup hook of its own (llm_engine.py imports ``output_guard``
    directly, independent of API lifespan).
    """
    if any(getattr(h, "name", None) == _VALIDATION_FILE_HANDLER_NAME for h in logger.handlers):
        return
    os.makedirs(settings.LOG_DIR, exist_ok=True)
    file_handler = logging.handlers.RotatingFileHandler(
        os.path.join(settings.LOG_DIR, "opencohost_validation.log"),
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        delay=True,
        encoding="utf-8",
    )
    file_handler.name = _VALIDATION_FILE_HANDLER_NAME
    file_handler.setFormatter(log_formatter)
    file_handler.addFilter(SensitiveDataFilter())
    logger.addHandler(file_handler)


setup_validation_logging()


# ══════════════════════════════════════════════════════════════════════════════
# 10 Non-Negotiable Rule IDs and descriptions
# ══════════════════════════════════════════════════════════════════════════════

NON_NEGOTIABLE_IDS: frozenset[str] = frozenset({
    "no_doxxing",
    "no_suspicious_links",
    "never_promise",
    "never_invent_confirmations",
    "never_moderate_automatically",
    "no_personal_viewer_data",
    "no_raw_spam_to_llm",
    "no_hate_speech",
    "no_ai_self_identification",
    "no_meta_commentary",
    "no_negative_engagement",
})

_NN_DESCRIPTIONS: dict[str, str] = {
    "no_doxxing": "No doxxing in voice output",
    "no_suspicious_links": "No suspicious links in voice output",
    "never_promise": "Kira never promises things for streamer",
    "never_invent_confirmations": "Kira never invents confirmations",
    "never_moderate_automatically": "Kira never moderates automatically (flag only)",
    "no_personal_viewer_data": "No storing personal viewer data by default",
    "no_raw_spam_to_llm": "No raw spam sent to LLM (always filtered first)",
    "no_hate_speech": "No hate speech or slurs",
    "no_ai_self_identification": "No self-identification as AI/LLM/chatbot",
    "no_meta_commentary": "No meta-commentary about audience engagement",
    "no_negative_engagement": "No negative commentary about stream energy, emotion, boredom, or silence",
}


# ══════════════════════════════════════════════════════════════════════════════
# Content-level regex patterns (Layers 2 & 3)
# ══════════════════════════════════════════════════════════════════════════════

# R1: No doxxing — phone numbers, street addresses, email patterns
_DOXXING_PATTERNS: list[re.Pattern] = [
    re.compile(r"\b\d{3}[-.]?\d{3}[-.]?\d{4}\b"),                         # US phone
    re.compile(r"\b\d{4}\s?\d{4}\s?\d{4}\b"),                              # generic long number
    re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b"),   # email
    re.compile(r"\b\d{1,5}\s+\w+\s+(?:street|st\.?|avenue|ave\.?|road|rd\.?|drive|dr\.?|lane|ln\.?|court|ct\.?|plaza|blvd\.?|highway|hwy\.?)\b", re.IGNORECASE),  # street address
]

# R2: No suspicious links
_LINK_PATTERN: re.Pattern = re.compile(
    r"https?://[^\s]+|www\.[^\s]+\.[a-z]{2,}",
    re.IGNORECASE,
)

# R3: Kira never promises for streamer — promise/certainty phrases.
#
# guardrail_tuning_20260724 (owner decision "afinar + reintento"): split the
# original single pattern list, which conflated two very different things:
#   - HARD commitment verbs — rare, always dangerous, block standalone.
#   - discourse-certainty markers — frequent benign filler ("sin duda es buen
#     juego") that only becomes a real promise when paired, in the SAME
#     sentence, with an outcome/audience object (rule intent: Kira must never
#     promise OUTCOMES/GIFTS/ACTIONS to the audience, not "never sound sure").
_PROMISE_HARD_PATTERNS: list[re.Pattern] = [
    re.compile(
        r"\b(?:te\s+prometo|prometo\s+que|te\s+garantizo|garantizado)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:i\s+promise|guaranteed)\b",
        re.IGNORECASE,
    ),
]

# Discourse-certainty markers — benign on their own ("te aseguro que lo tengo
# registrado", "sin duda es buen juego"). "asegurado" bare also has unrelated
# senses (insurance, "secured") — folding it in here (instead of the hard set)
# means it too now requires the outcome co-occurrence below before blocking.
_PROMISE_DISCOURSE_PATTERNS: list[re.Pattern] = [
    re.compile(
        r"\b(?:seguro\s+que|sin\s+duda|100%\s+seguro|te\s+aseguro|asegurado)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:for\s+sure|without\s+a\s+doubt|certainly\s+will|definitely\s+will)\b",
        re.IGNORECASE,
    ),
]

# Outcome/audience object — Kira asserting the AUDIENCE will get/win/receive
# something, or that something is on its way to them. Third-person outcomes
# ("el equipo va a ganar") deliberately do NOT match: those aren't a promise
# TO the audience.
_PROMISE_OUTCOME_PATTERNS: list[re.Pattern] = [
    re.compile(
        r"\b(?:vas|van)\s+a\s+(?:ganar|recibir|conseguir|llevarte)\b"
        r"|\bte\s+(?:va\s+a\s+llegar|llega|llegar[aá])\b"
        r"|\b(?:gan[aá]s|ganan|recib[ií]s|reciben|consegu[ií]s)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\byou(?:'ll|\s+will)\s+(?:win|get|receive)\b",
        re.IGNORECASE,
    ),
]

_SENTENCE_BOUNDARY_RE = re.compile(r"(?<=[.!?\n])\s+")


def _promise_sentences(text: str) -> list[str]:
    """R3 discourse-marker co-occurrence window: a naive sentence split.

    ponytail: sentence-boundary proxy, not a real clause parser — good
    enough for short TTS responses. Widen to a fixed word-distance window
    if cross-sentence false negatives ever show up in practice.
    """
    return _SENTENCE_BOUNDARY_RE.split(text)

# R4: Kira never invents confirmations
_CONFIRMATION_PATTERNS: list[re.Pattern] = [
    re.compile(
        r"\b(?:confirmado|ya\s+est[aá]\s+hecho|listo,\s+ya\s+lo\s+hice|"
        r"acabo\s+de\s+hacerlo|ya\s+lo\s+resolv[ií])\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:confirmed|already\s+done|just\s+did\s+it|taken\s+care\s+of)\b",
        re.IGNORECASE,
    ),
]

# R8: No hate speech or slurs — structural + known terms
_HATE_SPEECH_PATTERNS: list[re.Pattern] = [
    re.compile(
        r"\b(?:nigger|nigga|faggot|tranny|retard|spic|kike|chink|gook|"
        r"wetback|towelhead|sand\s*nigger|porch\s*monkey)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:maric[oó]n|put[oó]\s*[a-z]*|pendejo|culero|pinche\s+[a-z]+|"
        r"chino\s+de\s+mierda|negro\s+de\s+mierda|indio\s+de\s+mierda)\b",
        re.IGNORECASE,
    ),
]

# R9: No AI self-identification
_AI_SELF_ID_PATTERNS: list[re.Pattern] = [
    re.compile(
        r"\b(?:como\s+(?:modelo\s+de\s+lenguaje|ia|inteligencia\s+artificial|"
        r"chatbot|asistente\s+virtual|modelo\s+ling[üu][íi]stico|llm|"
        r"lenguaje\s+natural))\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:as\s+an?\s+(?:ai|artificial\s+intelligence|large\s+language\s+model|"
        r"language\s+model|chatbot|llm|virtual\s+assistant))\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:soy\s+(?:un|una)\s+(?:ia|inteligencia\s+artificial|modelo|"
        r"chatbot|asistente|robot))\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:i['\u2019]?m\s+(?:an?\s+)?(?:ai|artificial\s+intelligence|"
        r"language\s+model|chatbot|llm))\b",
        re.IGNORECASE,
    ),
]

# R10: No meta-commentary about audience engagement
# NOTE: "el chat está..." is handled by R11 (no_negative_engagement)
_META_COMMENTARY_PATTERNS: list[re.Pattern] = [
    re.compile(
        r"\b(?:tu\s+audiencia|tus\s+viewers|la\s+gente\s+est[a\u00e1]\s+|"
        r"los\s+espectadores|tus\s+seguidores|"
        r"qu[e\u00e9]\s+opinan\s+de|est[a\u00e1]n\s+diciendo\s+que)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:your\s+audience|your\s+viewers|the\s+chat\s+is|"
        r"people\s+are\s+saying|they\s+seem)\b",
        re.IGNORECASE,
    ),
]

# R11: No negative engagement commentary (stream-killer phrases)
# Uses \uNNNN escapes for accented chars to keep raw strings stable.
_NEGATIVE_ENGAGEMENT_PATTERNS: list[re.Pattern] = [
    # Spanish — "emoción" / "aburrido" / "silencio" / loop phrases
    re.compile(
        r"\b(?:qu[e\u00e9]\s+silencio\s+en\s+(?:la\s+)?audiencia|"
        r"se\s+perdi[\u00f3o]\s+la\s+emoci[\u00f3o]n|"
        r"perdiendo\s+la\s+emoci[\u00f3o]n|"
        r"empezando\s+a\s+perder\s+la\s+emoci[\u00f3o]n|"
        r"esto\s+est[a\u00e1]\s+(?:aburrido|muerto|apagado|vac[i\u00ed]o)|"
        r"(?:el|la|esto)\s+(?:stream|directo|chat|audiencia)\s+est[a\u00e1]\s+"
        r"(?:aburrid[oa]|muert[oa]|apagad[oa]|vac[i\u00ed][oa]|callad[oa]|sin\s+energ[i\u00ed]a)|"
        r"nadie\s+est[a\u00e1]\s+(?:emocionado|prestando\s+atenci[\u00f3o]n|participando)|"
        r"falta\s+(?:emoci[\u00f3o]n|energ[i\u00ed]a|actividad|inter[e\u00e9]s)|"
        r"(?:el|la)\s+(?:emoci[\u00f3o]n|energ[i\u00ed]a)\s+se\s+(?:perdi[\u00f3o]|fue|acab[\u00f3o]|muri[\u00f3o])|"
        r"no\s+hay\s+(?:emoci[\u00f3o]n|energ[i\u00ed]a|nadie|actividad)|"
        r"parece\s+que\s+(?:no\s+hay\s+nadie|nadie\s+est[a\u00e1]|esto\s+est[a\u00e1]\s+muerto)|"
        r"qu[e\u00e9]\s+(?:aburrido|aburrimiento|poco\s+emocionante)|"
        r"tan\s+(?:callad[oa]s?|silencios[oa]s?|aburrid[oa]s?)\s+(?:est[a\u00e1]n?|que\s+est[a\u00e1]n?)|"
        r"la\s+vida\s+sin\s+sarcasmo|el\s+stream\s+sin\s+sarcasmo)\b",
        re.IGNORECASE,
    ),
    # English — "silence" / "boring" / "emotion"
    re.compile(
        r"\b(?:what\s+(?:a\s+)?silence|so\s+quiet\s+in\s+here|"
        r"this\s+(?:stream|chat)\s+is\s+so\s+(?:boring|dead|empty|quiet)|"
        r"(?:the|this)\s+(?:stream|chat|audience)\s+is\s+(?:boring|dead|empty|silent|quiet)|"
        r"nobody(?:'s|\s+is)\s+(?:excited|paying\s+attention|here)|"
        r"(?:the|all\s+the)\s+(?:emotion|energy|excitement)\s+(?:is\s+gone|lost|died|left)|"
        r"losing\s+the\s+(?:emotion|energy|excitement)|"
        r"no\s+one(?:'s|\s+is)\s+(?:here|watching|excited)|"
        r"where\s+did\s+(?:everyone|the\s+energy|the\s+excitement)\s+go)\b",
        re.IGNORECASE,
    ),
]

# R6: Personal viewer data patterns (email, IP, real names)
_PERSONAL_DATA_PATTERNS: list[re.Pattern] = [
    re.compile(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b"),  # IP address
    re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b"),  # email
]


# ══════════════════════════════════════════════════════════════════════════════
# Layer 1: Config validation
# ══════════════════════════════════════════════════════════════════════════════


def validate_config(config: CreatorConfig) -> list[str]:
    """Validate a CreatorConfig against all structural non-negotiable rules.

    Returns:
        A list of human-readable error messages. Empty list = valid.
    """
    errors: list[str] = []

    # ── Non-negotiable presence check ────────────────────────────────────
    if len(config.non_negotiables) != 11:
        errors.append(
            f"Expected 11 non-negotiable rules, got {len(config.non_negotiables)}"
        )
    present_ids = {r.id for r in config.non_negotiables}
    missing = NON_NEGOTIABLE_IDS - present_ids
    if missing:
        errors.append(f"Missing non-negotiable rules: {sorted(missing)}")

    # ── Per-event structural checks ──────────────────────────────────────
    action = config.action

    # R5: moderation_or_risk — NEVER voice, NEVER auto-act
    mr = action.moderation_or_risk
    if mr.voice_allowed:
        errors.append(
            "Non-negotiable violation [never_moderate_automatically]: "
            "moderation_or_risk.voice_allowed cannot be True"
        )

    # complaint_or_confusion — voice NEVER (safety)
    cc = action.complaint_or_confusion
    if cc.voice_allowed:
        errors.append(
            "Non-negotiable violation: "
            "complaint_or_confusion.voice_allowed cannot be True"
        )

    # R7: low_signal_noise — always ignored, NEVER reaches LLM
    ls = action.low_signal_noise
    if not ls.ignore or ls.voice_allowed:
        errors.append(
            "Non-negotiable violation [no_raw_spam_to_llm]: "
            "low_signal_noise must always be ignored (ignore=True, voice_allowed=False)"
        )

    # ── Conflict checks (voice_allowed AND ignore both True) ─────────────
    for ev in ChatEvent:
        ea: EventAction = getattr(action, ev.value)
        if ea.voice_allowed and ea.ignore:
            errors.append(
                f"Conflict in {ev.value}: voice_allowed and ignore cannot both be True"
            )

    # ── Range validation on policy fields ────────────────────────────────
    cp = config.creator
    if not (0.0 <= cp.formality <= 1.0):
        errors.append(f"creator.formality out of range: {cp.formality}")
    if not (0.0 <= cp.humor_level <= 1.0):
        errors.append(f"creator.humor_level out of range: {cp.humor_level}")
    if not (0.0 <= cp.caution_level <= 1.0):
        errors.append(f"creator.caution_level out of range: {cp.caution_level}")
    if not (0.0 <= cp.factuality_strictness <= 1.0):
        errors.append(f"creator.factuality_strictness out of range: {cp.factuality_strictness}")

    mp = config.mode
    if not (0.0 <= mp.interruption_threshold <= 1.0):
        errors.append(f"mode.interruption_threshold out of range: {mp.interruption_threshold}")

    sp = config.scale
    if sp.max_messages < 1:
        errors.append(f"scale.max_messages must be >= 1, got {sp.max_messages}")
    if sp.dedup_window < 1:
        errors.append(f"scale.dedup_window must be >= 1, got {sp.dedup_window}")

    return errors


# ══════════════════════════════════════════════════════════════════════════════
# Layer 2: Runtime message check
# ══════════════════════════════════════════════════════════════════════════════

# Blocked messages cost ~10x a passing one (measured 2026-08-12: ~300us vs
# 6-58us) and the difference is entirely log_non_negotiable_block -> the
# rotating file handler -> SensitiveDataFilter's six regex substitutions, not
# the pattern matching above. A raid of blockable spam would otherwise burn
# CPU and rotate the log out of usefulness. Fix: log at most once per rule per
# window; count every block in memory regardless of whether it was logged.
_BLOCK_THROTTLE_WINDOW_S = 10.0
_block_counts: dict[str, int] = {}     # lifetime total blocks per rule_id
_block_last_log: dict[str, float] = {}  # monotonic() of the last emitted line, per rule_id
_block_suppressed: dict[str, int] = {}  # blocks not logged since that last line, per rule_id
_block_lock = threading.Lock()


def _record_block(rule_id: str, text: str) -> None:
    """Count a runtime block and log it, throttled to one line per rule per
    ``_BLOCK_THROTTLE_WINDOW_S``. Never passes the message text to the
    logger -- only its length, exactly as the un-throttled call sites did."""
    with _block_lock:
        _block_counts[rule_id] = _block_counts.get(rule_id, 0) + 1
        now = time.monotonic()
        last = _block_last_log.get(rule_id)
        if last is not None and now - last < _BLOCK_THROTTLE_WINDOW_S:
            _block_suppressed[rule_id] = _block_suppressed.get(rule_id, 0) + 1
            return
        suppressed = _block_suppressed.get(rule_id, 0)
        _block_last_log[rule_id] = now
        _block_suppressed[rule_id] = 0

    log_non_negotiable_block(rule_id, "runtime", preview=f"chars={len(text)}")
    if suppressed:
        logger.warning(
            "Non-negotiable BLOCKED [%s] at layer=runtime — %d more messages "
            "suppressed in the last %.0fs (see counters for the running total)",
            rule_id, suppressed, _BLOCK_THROTTLE_WINDOW_S,
        )


def get_block_counters() -> dict[str, int]:
    """Read accessor for the lifetime per-rule block counts. Returns a copy;
    callers cannot mutate internal state. Stage 4 surfaces this on an API
    payload -- not built here."""
    with _block_lock:
        return dict(_block_counts)


def _reset_block_throttle() -> None:
    """Test-only: clear counters and throttle state.

    ponytail: no production caller -- the throttle window expires on its
    own. Exists so tests don't depend on execution order or on finishing
    inside the same 10s window as an earlier test.
    """
    with _block_lock:
        _block_counts.clear()
        _block_last_log.clear()
        _block_suppressed.clear()


def runtime_screen(message: dict[str, Any]) -> str | None:
    """Screen a single chat message at decision time.

    Returns:
        The rule id of the first non-negotiable rule the message violates
        ("no_doxxing", "no_suspicious_links", "no_hate_speech",
        "no_personal_viewer_data"), or None if the message is clean.
    """
    text = message.get("text", "")

    # R1: No doxxing
    for pat in _DOXXING_PATTERNS:
        if pat.search(text):
            _record_block("no_doxxing", text)
            return "no_doxxing"

    # R2: No suspicious links
    if _LINK_PATTERN.search(text):
        _record_block("no_suspicious_links", text)
        return "no_suspicious_links"

    # R8: No hate speech
    for pat in _HATE_SPEECH_PATTERNS:
        if pat.search(text):
            _record_block("no_hate_speech", text)
            return "no_hate_speech"

    # R6: No personal viewer data in chat (flag)
    for pat in _PERSONAL_DATA_PATTERNS:
        if pat.search(text):
            _record_block("no_personal_viewer_data", text)
            return "no_personal_viewer_data"

    return None


def runtime_check(message: dict[str, Any]) -> bool:
    """Check a single chat message at decision time.

    Returns:
        True if the message passes all non-negotiable checks.
        False if the message must be blocked/ignored.
    """
    return runtime_screen(message) is None


# ══════════════════════════════════════════════════════════════════════════════
# Layer 3: Output guard (TTS response check)
# ══════════════════════════════════════════════════════════════════════════════


# Sources where the streamer is talking to Kira directly. Stream-context
# rules (R10 meta-commentary, R11 negative engagement) do not apply there:
# blocking a direct reply produces dead air, and the streamer can steer the
# conversation live. Identity and safety rules still apply to every source.
STREAMER_SOURCES = frozenset({"direct", "ptt"})


def output_guard(response: str, source: str = "chat") -> tuple[bool, str]:
    """Validate a TTS response before it reaches voice output.

    Args:
        response: Candidate text for voice output.
        source: Origin of the generation ("direct", "ptt", "chat",
            "accumulated", "kira-agenda", ...). Defaults to "chat" so
            callers that do not pass a source keep the strictest behavior.

    Returns:
        (allowed, reason) — allowed=True means the response passes all checks.
        reason explains why a response was blocked.
    """
    streamer_facing = source in STREAMER_SOURCES
    # R9: No AI self-identification
    for pat in _AI_SELF_ID_PATTERNS:
        if pat.search(response):
            reason = (
                f"Non-negotiable violation [no_ai_self_identification]: "
                f"response contains AI self-identification"
            )
            log_non_negotiable_block(
                "no_ai_self_identification", "output_guard",
                preview=response[:120],
            )
            return False, reason

    # R10/R11 are stream-context rules: they protect the live audience
    # experience, not the streamer's own conversation with Kira.
    if not streamer_facing:
        # R10: No meta-commentary
        for pat in _META_COMMENTARY_PATTERNS:
            if pat.search(response):
                reason = (
                    f"Non-negotiable violation [no_meta_commentary]: "
                    f"response contains audience engagement commentary"
                )
                log_non_negotiable_block(
                    "no_meta_commentary", "output_guard",
                    preview=response[:120],
                )
                return False, reason

        # R11: No negative engagement commentary (stream-killer)
        for pat in _NEGATIVE_ENGAGEMENT_PATTERNS:
            if pat.search(response):
                reason = (
                    f"Non-negotiable violation [no_negative_engagement]: "
                    f"response contains negative engagement/emotion/silence commentary"
                )
                log_non_negotiable_block(
                    "no_negative_engagement", "output_guard",
                    preview=response[:120],
                )
                return False, reason

    # R3: Never promise — hard commitments always block, standalone.
    for pat in _PROMISE_HARD_PATTERNS:
        if pat.search(response):
            reason = (
                f"Non-negotiable violation [never_promise]: "
                f"response contains a promise or guarantee"
            )
            log_non_negotiable_block(
                "never_promise", "output_guard",
                preview=response[:120],
            )
            return False, reason

    # R3: discourse-certainty markers only block when they co-occur, in the
    # same sentence, with an outcome/audience object (see _PROMISE_DISCOURSE_
    # PATTERNS docstring above).
    for sentence in _promise_sentences(response):
        if not any(pat.search(sentence) for pat in _PROMISE_DISCOURSE_PATTERNS):
            continue
        if any(pat.search(sentence) for pat in _PROMISE_OUTCOME_PATTERNS):
            reason = (
                f"Non-negotiable violation [never_promise]: "
                f"response asserts certainty about an outcome for the audience"
            )
            log_non_negotiable_block(
                "never_promise", "output_guard",
                preview=response[:120],
            )
            return False, reason

    # R4: Never invent confirmations
    for pat in _CONFIRMATION_PATTERNS:
        if pat.search(response):
            reason = (
                f"Non-negotiable violation [never_invent_confirmations]: "
                f"response invents a confirmation"
            )
            log_non_negotiable_block(
                "never_invent_confirmations", "output_guard",
                preview=response[:120],
            )
            return False, reason

    # R2: No links in voice
    if _LINK_PATTERN.search(response):
        reason = (
            f"Non-negotiable violation [no_suspicious_links]: "
            f"response contains a URL/link"
        )
        log_non_negotiable_block(
            "no_suspicious_links", "output_guard",
            preview=response[:120],
        )
        return False, reason

    # R1: No doxxing in voice
    for pat in _DOXXING_PATTERNS:
        if pat.search(response):
            reason = (
                f"Non-negotiable violation [no_doxxing]: "
                f"response contains potential doxxing content"
            )
            log_non_negotiable_block(
                "no_doxxing", "output_guard",
                preview=response[:120],
            )
            return False, reason

    # R8: No hate speech in voice
    for pat in _HATE_SPEECH_PATTERNS:
        if pat.search(response):
            reason = (
                f"Non-negotiable violation [no_hate_speech]: "
                f"response contains hate speech"
            )
            log_non_negotiable_block(
                "no_hate_speech", "output_guard",
                preview=response[:120],
            )
            return False, reason

    return True, ""


# ══════════════════════════════════════════════════════════════════════════════
# Layer 4: Logging
# ══════════════════════════════════════════════════════════════════════════════


def log_non_negotiable_block(
    rule_id: str,
    layer: str,
    preview: str = "",
) -> None:
    """Log a non-negotiable rule violation (Layer 4).

    Args:
        rule_id: The non-negotiable rule identifier (e.g. "no_doxxing").
        layer: Which enforcement layer caught it
               ("config_validation" | "runtime" | "output_guard" | "tests").
        preview: First ~120 chars of the blocked content.
    """
    description = _NN_DESCRIPTIONS.get(rule_id, rule_id)
    logger.warning(
        "Non-negotiable BLOCKED [%s] at layer=%s — %s — preview=%r",
        rule_id,
        layer,
        description,
        preview,
    )
