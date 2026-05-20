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
import re
from typing import Any

from config.schema import (
    ActionPolicy,
    ChatEvent,
    CreatorConfig,
    EventAction,
)

logger = logging.getLogger(__name__)


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

# R3: Kira never promises for streamer — promise/certainty phrases
_PROMISE_PATTERNS: list[re.Pattern] = [
    re.compile(
        r"\b(?:te\s+prometo|prometo\s+que|garantizado|asegurado|seguro\s+que|"
        r"sin\s+duda|100%\s+seguro|te\s+aseguro|te\s+garantizo)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:i\s+promise|guaranteed|certainly\s+will|definitely\s+will|"
        r"for\s+sure|without\s+a\s+doubt)\b",
        re.IGNORECASE,
    ),
]

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
        r"\b(?:as\s+an\s+(?:ai|artificial\s+intelligence|language\s+model|"
        r"chatbot|llm|virtual\s+assistant))\b",
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
_META_COMMENTARY_PATTERNS: list[re.Pattern] = [
    re.compile(
        r"\b(?:tu\s+audiencia|tus\s+viewers|la\s+gente\s+est[aá]\s+|"
        r"el\s+chat\s+est[aá]\s+|los\s+espectadores|tus\s+seguidores|"
        r"qu[eé]\s+opinan\s+de|est[aá]n\s+diciendo\s+que)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:your\s+audience|your\s+viewers|the\s+chat\s+is|"
        r"people\s+are\s+saying|they\s+seem)\b",
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
    if len(config.non_negotiables) != 10:
        errors.append(
            f"Expected 10 non-negotiable rules, got {len(config.non_negotiables)}"
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


def runtime_check(message: dict[str, Any]) -> bool:
    """Check a single chat message at decision time.

    Returns:
        True if the message passes all non-negotiable checks.
        False if the message must be blocked/ignored.
    """
    text = message.get("text", "")

    # R1: No doxxing
    for pat in _DOXXING_PATTERNS:
        if pat.search(text):
            log_non_negotiable_block(
                "no_doxxing", "runtime",
                preview=text[:100],
            )
            return False

    # R2: No suspicious links
    if _LINK_PATTERN.search(text):
        log_non_negotiable_block(
            "no_suspicious_links", "runtime",
            preview=text[:100],
        )
        return False

    # R8: No hate speech
    for pat in _HATE_SPEECH_PATTERNS:
        if pat.search(text):
            log_non_negotiable_block(
                "no_hate_speech", "runtime",
                preview=text[:100],
            )
            return False

    # R6: No personal viewer data in chat (flag)
    for pat in _PERSONAL_DATA_PATTERNS:
        if pat.search(text):
            log_non_negotiable_block(
                "no_personal_viewer_data", "runtime",
                preview=text[:100],
            )
            return False

    return True


# ══════════════════════════════════════════════════════════════════════════════
# Layer 3: Output guard (TTS response check)
# ══════════════════════════════════════════════════════════════════════════════


def output_guard(response: str) -> tuple[bool, str]:
    """Validate a TTS response before it reaches voice output.

    Returns:
        (allowed, reason) — allowed=True means the response passes all checks.
        reason explains why a response was blocked.
    """
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

    # R3: Never promise
    for pat in _PROMISE_PATTERNS:
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
