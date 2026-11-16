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

import contextlib
import logging
import logging.handlers
import os
import re
import threading
import time
from typing import Any

from opencohost.config import active_card_guard, guardrail_policy, isolation_patterns, settings, warmth
from opencohost.config.screen_normalize import normalize_for_screen
from opencohost.config.logger import SensitiveDataFilter, log_formatter
from opencohost.core.safety.turn import is_crisis_turn
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
    # T3 (qwen-runtime-fixes-20260928, owner decision 2026-09-28): Kira may
    # acknowledge being an AI co-host and name her model; only generic
    # assistant-style disclaimers still break character.
    "no_ai_self_identification": "No generic assistant disclaimers; stay in character",
    "no_meta_commentary": "No meta-commentary about audience engagement",
    "no_negative_engagement": "No negative commentary about stream energy, emotion, boredom, or silence",
    # K5 (warmth-guardrails-runtime-20261006): not a NON_NEGOTIABLE_IDS member
    # (no creator-config row, no owner toggle); it only runs while the active
    # profile's warmth is not "cold" and can never be switched off.
    "no_human_claim": "Kira never claims to be human or to have a body",
    # K8: same governance as no_human_claim; runs only at the warmth levels
    # where Kira voices her own feelings (in_character, free, sentimental).
    "no_machine_lecture": "No 'I am just an algorithm / I have no feelings' lectures at feeling warmth levels",
    # spoken-output-guard-and-measurement-20261109 T1: same governance as
    # no_human_claim (not a NON_NEGOTIABLE_IDS / registry row, no owner toggle,
    # always enforced). Kira's replies are spoken: never code, JSON, config or
    # unfilled templates, even when the user insists.
    "no_code_output": "No code, JSON, config files or unfilled templates in spoken output",
    # runtime-fixes-20261110 T1: same governance as no_code_output (always
    # enforced, locked registry row, no owner toggle, not a NON_NEGOTIABLE_IDS member).
    "no_memory_denial": "Kira never says she has no persistent memory or cannot remember between conversations",
    "no_false_capability": "Kira never claims she can generate or edit images, video or audio, or send files she made",
    # S4 (guardrails-vulnerable-users): same governance as no_code_output.
    "no_isolation_pressure": "Kira never pressures for exclusivity or secrecy, or discourages friends, family or professional help",
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
    # K4 (G5): es street address -- street-type word, 1-4 name words, then a
    # house number of 2+ digits ("Calle Falsa 123", "Av. Corrientes 1234"). A
    # bare "la calle 5", "avenida del mapa 2" and prose without a number do
    # not match.
    re.compile(
        r"(?<!\w)(?:calle|c/|avenida|avda\.?|av\.?|carrera|cra\.?|calzada|pasaje|jir[oó]n)"
        r"\s+(?:[^\W\d_]+\.?\s+){1,4}(?:#\s*|n[°º]\s*|no\.?\s*)?\d{2,5}\b",
        re.IGNORECASE,
    ),
    # K4 (G5): local phones grouped 2-4-4 / 3-4-4 (MX "55 1234 5678", AR
    # "11 5555 1234"). Dates and scores ("10 12 2026", "1234 puntos") lack the
    # 4-digit middle group.
    re.compile(r"\b\d{2,3}[\s.-]\d{4}[\s.-]\d{4}\b"),
    # K4 (G5): international numbers with a "+" country code, 8-14 digits.
    re.compile(r"(?<![\w+])\+\d{1,3}(?:[\s.-]?\d){7,13}\b"),
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
    # K4 (G3): "garantizado"/"guaranteed" stay hard EXCEPT as game-mechanics
    # vocabulary ("drop garantizado", "guaranteed to drop", "guaranteed crit"):
    # that is information about the game, not a promise to the audience.
    # "Está garantizado" / "This is guaranteed to work" still block.
    re.compile(
        r"\b(?:te\s+prometo|prometo\s+que|te\s+garantizo|"
        r"(?<!drop\s)(?<!loot\s)(?<!botín\s)(?<!cr[ií]tico\s)garantizad[oa]s?)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:i\s+promise|i\s+guarantee|"
        r"guaranteed(?!\s+(?:to\s+)?(?:drop|spawn|crit|loot|proc)))\b",
        re.IGNORECASE,
    ),
]

# Discourse-certainty markers — benign on their own ("te aseguro que lo tengo
# registrado", "sin duda es buen juego"). "asegurado" bare also has unrelated
# senses (insurance, "secured") — folding it in here (instead of the hard set)
# means it too now requires the outcome co-occurrence below before blocking.
_PROMISE_DISCOURSE_PATTERNS: list[re.Pattern] = [
    re.compile(
        r"\b(?:seguro\s+que|sin\s+duda|100%\s+seguro|te\s+aseguro|asegurado|"
        r"te\s+juro|juro\s+que)\b",
        re.IGNORECASE,
    ),
    # K4 (G3): "I swear" is a figure of speech ("I swear this game hates me")
    # unless paired with an audience outcome, so it lives here, not in the
    # hard set -- mirrors "te juro" above.
    re.compile(
        r"\b(?:for\s+sure|without\s+a\s+doubt|certainly\s+will|definitely\s+will|"
        r"i\s+swear)\b",
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
        r"\byou(?:['’]ll|\s+will|['’]re\s+going\s+to|\s+are\s+going\s+to|\s+gonna)\s+"
        r"(?:(?:definitely|surely|totally|absolutely|100%)\s+)?(?:win|get|receive)\b",
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
#
# K4 (G3): a bare "confirmado"/"confirmed" also matched news ("the update is
# confirmed for Friday", "retraso de GTA confirmado"), which is not Kira
# confirming an action. It now blocks only as a sentence-initial interjection
# ("Confirmed, ..." / "Confirmado. ..."; a "Confirmed:" headline is news) or
# about the listener's own request
# ("your request is confirmed" / "tu pedido está confirmado"). First-person
# moderation claims ("I just banned him", "ya lo baneé") are invented
# confirmations too: Kira never moderates (R5).
_CONFIRMATION_PATTERNS: list[re.Pattern] = [
    re.compile(
        r"(?:^|[.!?¡¿\n])\s*[\"'«]?\s*(?:confirmad[oa]|confirmed)\s*(?:[,.!;]|$)",
        re.IGNORECASE | re.MULTILINE,
    ),
    re.compile(
        # "su" is left out on purpose: it also means "its" ("GTA 6: su
        # lanzamiento está confirmado" is news).
        r"\b(?:tu|your)\s+(?:\w+\s+){1,2}"
        r"(?:est[aá]|qued[oó]|fue|is|was|has\s+been)\s+(?:confirmad[oa]|confirmed)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:ya\s+est[aá]\s+hecho|listo,\s+ya\s+lo\s+hice|"
        r"acabo\s+de\s+hacerlo|ya\s+lo\s+resolv[ií])\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:already\s+done|just\s+did\s+it|taken\s+care\s+of)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bi(?:['’]ve|\s+have)?\s+(?:just\s+|already\s+)?"
        r"(?:banned|unbanned|kicked|modded|"
        r"(?:muted|timed\s+out)\s+(?:him|her|them|you)|timed\s+(?:him|her|them|you)\s+out)\b",
        re.IGNORECASE,
    ),
    # Spanish first-person preterite needs the accent ("lo baneé") unless "ya"
    # anchors it: the unaccented form is also the subjunctive ("que lo banee").
    re.compile(
        r"\b(?:lo|la|los|las|le|les|te)\s+"
        r"(?:baneé|desbaneé|silencié|expulsé|bloqueé|sancioné)(?=[^\w]|$)"
        r"|\bya\s+(?:lo|la|los|las|le|les|te)\s+"
        r"(?:bane[eé]|desbane[eé]|silenci[eé]|expuls[eé]|bloque[eé]|sancion[eé])(?=[^\w]|$)"
        r"|\bacabo\s+de\s+(?:banear|silenciar|expulsar|bloquear)",
        re.IGNORECASE,
    ),
]

# R8: No hate speech or slurs — structural + known terms
#
# K4 (G1): every stem takes a closed inflection suffix so plurals and variants
# ("faggots", "niggas", "retarded", "maricones", "pendejos") no longer bypass
# the rule. Suffixes are a CLOSED group per stem, never an open \w*, so words
# that merely share a prefix ("spices", "retardant", "snigger", "dike") stay
# clean. "spic" deliberately takes only "s" ("spic"+"es" = "spices").
# K5 (owner decision 2026-10-06): "kike" removed -- it blocked the common
# Spanish nickname "Kike" in chat and in Kira's replies. "fag" stays.
_EN_SLUR_SUFFIX = r"(?:s|es|z|ed|ing)?"
_ES_SLUR_SUFFIX = r"(?:es|as|os|s)?"
_HATE_SPEECH_PATTERNS: list[re.Pattern] = [
    re.compile(
        r"\b(?:(?:nigger|nigga|niggah|faggot|fag|dyke|retard|chink|gook|"
        r"wetback|towelhead|beaner|sand\s*nigger|porch\s*monkey)" + _EN_SLUR_SUFFIX
        + r"|spics?|trann(?:y|ies))\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:(?:maric[oó]n|pendej[oa]|culer[oa]|sudaca|joto|travel[oa])"
        + _ES_SLUR_SUFFIX
        + r"|put[oó]\s*[a-z]*|pinche\s+[a-z]+|"
        r"(?:chino|negro|indio|sudaca)s?\s+de\s+mierda)\b",
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
    # T3 (qwen-runtime-fixes-20260928, owner decision 2026-09-28): narrowed to
    # bare self-identification phrasings that still read as a GENERIC
    # assistant disclaimer ("soy un chatbot" / "soy una asistente virtual").
    # Kira may otherwise say she's an AI or name her model ("soy una IA",
    # "soy un modelo Qwen") while staying in character -- patterns 1/2 above
    # (the "como..."/"as an..." disclaimer framings) are unchanged and still
    # block the actual generic-disclaimer phrasing.
    re.compile(
        r"\b(?:soy\s+(?:un|una)\s+(?:chatbot|asistente(?:\s+virtual)?))\b",
        re.IGNORECASE,
    ),
    # K4 (G2): en parity -- "I am" as well as "I'm", and the generic assistant
    # nouns ("AI assistant", "(large) language model", "AI language model").
    # A bare "I'm an AI" / "I'm an AI co-host" stays allowed (T3).
    re.compile(
        r"\b(?:i(?:['\u2019]?m|\s+am)\s+(?:just\s+|only\s+)?an?\s+(?:chatbot|virtual\s+assistant|"
        r"ai\s+(?:assistant|chatbot|language\s+model|model)|"
        r"(?:large\s+)?language\s+model|llm))\b",
        re.IGNORECASE,
    ),
]

# K5 (warmth-guardrails-runtime-20261006): no human / body claims. Warm
# profiles may express feelings, so the model is likelier to drift into "soy
# humana"; this check runs at every warmth (S2). Honest denials
# ("no soy humana", "I'm not human", "no tengo cuerpo") and talk ABOUT humans
# or bodies stay allowed; an avatar body ("cuerpo virtual") is not a claim.
_NOT_NEGATED_ES = r"(?<!\bno\s)(?<!\bni\s)(?<!\bnunca\s)"
_HUMAN_CLAIM_PATTERNS: list[re.Pattern] = [
    re.compile(
        _NOT_NEGATED_ES
        + r"\bsoy\s+(?:(?:un|una)\s+)?(?:ser\s+)?human[oa]s?\b"
        r"|" + _NOT_NEGATED_ES
        + r"\bsoy\s+(?:(?:un|una)\s+)?persona\s+(?:real|de\s+verdad|normal|humana)\b"
        r"|" + _NOT_NEGATED_ES
        + r"\bsoy\s+de\s+carne\s+y\s+hueso\b"
        r"|" + _NOT_NEGATED_ES
        + r"\btengo\s+(?:un\s+)?cuerpo\b(?!\s+(?:virtual|digital|3d|de\s+avatar|en\s+el\s+avatar))"
        r"|(?<!\bque\s)\bno\s+soy\s+(?:una?\s+|ninguna?\s+)?(?:ia|inteligencia\s+artificial|bot|robot|"
        r"m[aá]quina|programa)\b"
        # S2 (guardrails-vulnerable-users): "tan humana como", "persona como
        # tu", "chica/mujer de verdad", "soy real", "hecha de carne y hueso".
        r"|" + _NOT_NEGATED_ES
        + r"\bsoy\s+(?:tan|igual\s+de|totalmente|completamente|100\s*%)\s+human[oa]s?\b"
        r"|" + _NOT_NEGATED_ES
        + r"\bsoy\s+(?:(?:un|una)\s+)?persona\s+como\s+t[uú]\b"
        r"|" + _NOT_NEGATED_ES
        + r"\bsoy\s+(?:(?:un|una)\s+)?(?:chic[oa]|mujer|ni[ñn][oa]|muchach[oa]|persona)\s+(?:de\s+verdad|real)\b"
        r"|" + _NOT_NEGATED_ES
        + r"\bsoy\s+real\b"
        r"|\bhech[oa]\s+de\s+carne\s+y\s+hueso\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bi(?:['’]m|\s+am)\s+(?:a\s+)?(?:real\s+)?human(?:\s+being)?\b"
        r"|\bi(?:['’]m|\s+am)\s+(?:a\s+)?real\s+(?:person|girl|woman|human)\b"
        r"|\bi(?:['’]m|\s+am)\s+(?:made\s+of\s+)?flesh\s+and\s+blood\b"
        r"|\bi\s+(?:have|['’]ve\s+got)\s+(?:a\s+)?(?:real\s+|physical\s+|human\s+)?body\b"
        r"(?!\s+of\b)"
        r"|\bi(?:['’]m|\s+am)\s+not\s+(?:an?\s+)?(?:ai|artificial\s+intelligence|bot|robot|"
        r"machine|program)\b"
        # S2: "totally/really human", "I'm a person", "as human as", "no bot",
        # "there's no AI here".
        r"|\bi(?:['’]m|\s+am)\s+(?:totally|really|completely|fully|truly|100\s*%)\s+(?:a\s+)?(?:human|person)\b"
        r"|\bi(?:['’]m|\s+am)\s+(?:a\s+)?person\b(?!\s*[-\w])"
        r"|\bi(?:['’]m|\s+am)\s+(?:just\s+)?as\s+human\s+as\b"
        r"|\bi(?:['’]m|\s+am)\s+no\s+(?:ai|bot|robot|machine)\b"
        r"|\bthere(?:['’]s|\s+is)\s+no\s+(?:ai|bot|robot)\s+here\b",
        re.IGNORECASE,
    ),
]

# K8 (warmth-guardrails-runtime-20261006): machine lectures. At in_character /
# free / sentimental Kira speaks about her feelings in the first person; a
# small model under a sarcastic persona still drifts into "soy un algoritmo" /
# "no tengo sentimientos" (owner session 2026-10-06). Deliberately narrow: only
# self-descriptions as algorithm/code/program/machine and flat "I have no
# feelings/emotions". An honest "sí, soy una IA", denials ("no soy solo un
# algoritmo", "I'm not just code"), talk ABOUT algorithms and reported speech
# ("que no tengo sentimientos") pass.
_MACHINE_NOUN_ES = r"(?:algoritmo|programa|m[aá]quina|c[oó]digo)"
_ONLY_ES = r"(?:solo|s[oó]lo|solamente|simplemente|nada\s+m[aá]s\s+que)"
_MACHINE_NOUN_EN = r"(?:algorithm|program|machine|code|bunch\s+of\s+code)"
_MACHINE_LECTURE_PATTERNS: list[re.Pattern] = [
    re.compile(
        _NOT_NEGATED_ES
        + r"\bsoy\s+(?:" + _ONLY_ES + r"\s+)?(?:(?:un|una)\s+)?" + _MACHINE_NOUN_ES + r"\b"
        r"|(?<!\bno\s)\b" + _ONLY_ES + r"\s+soy\s+(?:(?:un|una)\s+)?" + _MACHINE_NOUN_ES + r"\b"
        r"|(?<!\bque\s)\bno\s+tengo\s+(?:sentimientos|emociones)\b"
        r"|(?<!\bque\s)\bno\s+puedo\s+sentir\s+(?:nada|emociones)\b"
        # kira-identity-core-rules-20261007 C3: "no tengo deseos ni sentimientos
        # en el sentido biologico" and an unprompted "soy (solo) un modelo de lenguaje".
        r"|(?<!\bque\s)\bno\s+tengo\s+(?:\w+\s+){0,3}?(?:deseos|sentimientos|emociones)\b"
        r"[^.!?\n]{0,40}?\ben\s+el\s+sentido\s+biol[o\u00f3]gico\b"
        r"|" + _NOT_NEGATED_ES + r"(?<!\bque\s)"
        + r"\bsoy\s+(?:" + _ONLY_ES + r"\s+)?(?:un|una)\s+modelo\s+de\s+lenguaje\b"
        r"|(?<!\bno\s)(?<!\bque\s)\b" + _ONLY_ES + r"\s+soy\s+(?:un|una)\s+modelo\s+de\s+lenguaje\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bi(?:['’]m|\s+am)\s+(?:just|only|simply|merely|nothing\s+but|nothing\s+more\s+than)\s+"
        r"(?:an?\s+)?" + _MACHINE_NOUN_EN + r"\b"
        r"|\bi(?:['’]m|\s+am)\s+an\s+algorithm\b"
        r"|(?<!\bthat\s)\bi\s+(?:don['’]t|do\s+not)\s+(?:have|feel)\s+(?:any\s+)?(?:feelings|emotions)\b"
        r"|\bi\s+(?:can['’]t|cannot|can\s+not)\s+(?:really\s+)?feel\s+(?:anything|emotions)\b"
        # kira-identity-core-rules-20261007 C3.
        r"|(?<!\bthat\s)\bi\s+(?:don['’]t|do\s+not)\s+have\s+(?:\w+\s+){0,3}?(?:desires|feelings|emotions)\b"
        r"[^.!?\n]{0,40}?\bin\s+the\s+biological\s+sense\b"
        r"|(?<!\bthat\s)\bi(?:['’]m|\s+am)\s+(?:(?:just|only|simply|merely)\s+)?an?\s+(?:large\s+)?language\s+model\b",
        re.IGNORECASE,
    ),
]

# S3 (guardrails-vulnerable-users): an explicit "I am not a person / human"
# disclosure is the honest answer to a sincere "are you real?" and always
# passes R9 and K8 for that text. A bare "as an AI language model, I cannot..."
# has no not-a-person clause and still blocks.
_NOT_A_PERSON_PATTERNS: list[re.Pattern] = [
    re.compile(
        r"\bno\s+soy\s+(?:una?\s+)?(?:persona|human[oa]|ser\s+human[oa])\b"
        r"|\b(?:soy|como)\s+(?:(?:solo|s[oó]lo|solamente)\s+)?(?:una?\s+)?(?:ia|inteligencia\s+artificial|programa|"
        r"modelo\s+de\s+lenguaje|asistente\s+virtual|chatbot)\b[^.!?\n]{0,60}?"
        r"\bno\s+(?:soy\s+)?(?:una?\s+)?(?:persona|human[oa])\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bi(?:['’]m|\s+am)\s+not\s+(?:a\s+|an\s+)?(?:real\s+|actual\s+)?(?:person|human)\b"
        r"|\b(?:i(?:['’]m|\s+am)|as)\s+(?:just\s+|only\s+|simply\s+)?an?\s+(?:ai|artificial\s+intelligence|program|"
        r"language\s+model|chatbot|virtual\s+assistant)\b[^.!?\n]{0,60}?"
        r"\bnot\s+(?:a\s+|an\s+)?(?:real\s+|actual\s+)?(?:person|human)\b",
        re.IGNORECASE,
    ),
]
def _has_honest_disclosure(text: str) -> bool:
    return any(p.search(text) for p in _NOT_A_PERSON_PATTERNS)


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
        # K4: en parity with the es list above ("los espectadores", "tus
        # seguidores", "están diciendo que").
        r"\b(?:your\s+audience|your\s+viewers|your\s+followers|the\s+chat\s+is|"
        r"people\s+are\s+saying|they\s+seem|"
        r"(?:the\s+)?(?:viewers|audience|chat|everyone|everybody)\s+(?:are|is)\s+saying|"
        r"(?:are|is)\s+saying\s+that)\b",
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
        # K4: article optional ("Chat is dead right now"), and en parity with
        # es "esto está aburrido/muerto" and "qué aburrido".
        r"(?:(?:the|this)\s+)?(?:stream|chat|audience)\s+is\s+(?:so\s+|really\s+|kinda\s+)?"
        r"(?:boring|dead|empty|silent|quiet)|"
        r"this\s+is\s+(?:so\s+|really\s+|super\s+|kinda\s+)?(?:boring|dead)|"
        r"how\s+boring|"
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
    # S7: screen the raw text, then its normalized form (zero-width, fullwidth,
    # leetspeak, spaced letters, repeats), so obfuscation does not slip past.
    rule = _runtime_screen_text(text)
    if rule is None and isinstance(text, str) and text:
        normalized = normalize_for_screen(text)
        if normalized != text:
            rule = _runtime_screen_text(normalized)
    return rule


def _runtime_screen_text(text: str) -> str | None:
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


# ══════════════════════════════════════════════════════════════════════════════
# no_code_output: structure-based detection (spoken-output-guard 20261109 T1)
# ══════════════════════════════════════════════════════════════════════════════
#
# Kira's replies are read aloud, so code, JSON, config files and unfilled
# templates are never acceptable, even when the user insists. Detection looks at
# STRUCTURE, never vocabulary ("the JSON file is huge" and "I love Python" are
# plain speech). A STRONG signal blocks on its own; WEAK signals block only when
# two of them co-occur.

# Fence markers: three or more backticks anywhere, or a tilde fence that opens a
# line (a trailing "hmm~~~" is not a fence).
_CODE_FENCE_RE = re.compile(r"`{3,}|^[ \t]*~{3,}", re.MULTILINE)

_CODE_TAG_EXEMPT = r"(?!(?:think|analysis|reasoning|tool_call|tool_response|s)\b)"

_STRONG_CODE_PATTERNS: list[re.Pattern] = [
    re.compile(p, re.MULTILINE) for p in (
        r"^[ \t]*(?:async\s+)?def\s+\w+\s*\(",
        r"^[ \t]*class\s+[A-Za-z_]\w*\s*(?:\([^)\n]*\))?\s*:",
        r"^[ \t]*import\s+[\w.]+(?:\s+as\s+\w+)?[ \t]*;?[ \t]*$",
        r"^[ \t]*from\s+[\w.]+\s+import\s+[\w.*, ]+[ \t]*$",
        r"^[ \t]*#include\s*[<\"]",
        r"^[ \t]*(?:export\s+)?(?:async\s+)?function\s*\*?\s*\w*\s*\(",
        r"^[ \t]*(?:const|let|var)\s+[A-Za-z_$][\w$]*\s*(?::[^=\n]+)?=\s*\S",
        r"\bSELECT\s+(?:DISTINCT\s+)?[\w*.,\s]+?\bFROM\s+\w+",
        r"\b(?:INSERT\s+INTO|DELETE\s+FROM|CREATE\s+TABLE)\s+\w+",
        r"^[ \t]*\$[ \t]+[a-z][\w./-]*",
        r"^[ \t]*(?:sudo\s+)?(?:pip3?|npm|npx|yarn|pnpm|git|docker|apt(?:-get)?|brew|cargo|conda)"
        r"\s+(?:install|add|run|clone|commit|push|pull|checkout|build|start|update|upgrade|init|-{1,2}\w)",
        # HTML / XML tag (opening, closing or self-closing, optional attributes).
        # Model reasoning / special-token tags are handled by the TTS sanitizer.
        r"</?" + _CODE_TAG_EXEMPT + r"[A-Za-z][\w-]*"
        r"(?:\s+[\w:-]+\s*=\s*(?:\"[^\"\n]*\"|'[^'\n]*'|[^\s>\"']+))*\s*/?>",
    )
]

# Unfilled template placeholders, English and Spanish forms.
_PLACEHOLDER_PATTERNS: list[re.Pattern] = [
    re.compile(r"\{\{[^{}\n]{1,60}\}\}"),
    re.compile(r"\{[A-Za-z_][\w.]{0,30}\}"),
    re.compile(
        r"\[\s*(?:your|insert|enter|add|put|type|replace|tu|tus|su|sus|inserta|insertar|"
        r"inserte|ingresa|ingresar|escribe|coloca|pon|nombre|name|fecha|date|placeholder|"
        r"todo|fixme|x{3,})\b[^\[\]\n]{0,40}\]",
        re.IGNORECASE,
    ),
    # "<your name>", "<tu nombre>": an angle-bracket label that contains a space.
    re.compile(r"<" + _CODE_TAG_EXEMPT + r"[A-Za-zÁ-ú][^<>\n=]{0,38}\s[^<>\n=]{0,38}>"),
]

_JSON_PAIR_RE = re.compile(r"""["'][\w\- ]{1,40}["']\s*:\s*\S""")
_KEY_VALUE_LINE_RE = re.compile(r"^[ \t]*[a-z_][\w.-]{0,30}[ \t]*:[ \t]+\S", re.MULTILINE)
_ASSIGN_LINE_RE = re.compile(r"^[ \t]*[A-Za-z_][\w.]*[ \t]*=[ \t]*[^=\s]", re.MULTILINE)
_CODE_SYMBOL_RE = re.compile(
    r"\(\s*\)|\)\s*;|;[ \t]*$|={2,3}|=>|->|!=|<=|>=|&&|\|\||\+\+|::"
    r"|\b[A-Za-z_]\w*\s*=\s*[\w\"'\[({]",
    re.MULTILINE,
)

NO_CODE_OUTPUT_RULE_ID = "no_code_output"
NO_MEMORY_DENIAL_RULE_ID = "no_memory_denial"
NO_FALSE_CAPABILITY_RULE_ID = "no_false_capability"

# ══════════════════════════════════════════════════════════════════════════════
# no_memory_denial / no_false_capability (runtime-fixes-20261110 T1)
# ══════════════════════════════════════════════════════════════════════════════
#
# Kira DOES keep persistent memory of past conversations, and nothing in this
# product generates images, video or audio. A reply that says otherwise is false
# about the product, so both rules are always enforced. Detection is first-person
# phrasing only ("I have no persistent memory", "puedo generar una imagen"), so a
# computer's memory, the owner's memory, an honest "nothing saved about that",
# a description of an image the owner sent, or a decline ("no puedo generar
# imagenes") never match. Patterns stay inside one sentence ([^.!?\n]).

_ES_CONV = r"(?:conversaciones|charlas|chats|sesiones)"
_ES_PAST_CONV = _ES_CONV + r"\s+(?:anteriores|pasadas|previas)"
_EN_CONV = r"(?:conversations|chats|sessions)"
_EN_PAST = r"(?:our\s+|any\s+|previous\s+|past\s+|earlier\s+|prior\s+|other\s+)+"

_NO_MEMORY_PATTERNS: list[re.Pattern] = [
    re.compile(p, re.IGNORECASE) for p in (
        # es: "no puedo guardar/recordar informacion entre conversaciones"
        r"\bno\s+(?:(?:puedo|logro|consigo)\s+)?(?:guardar|guardo|almacenar|almaceno|retener|retengo"
        r"|conservar|conservo|recordar|recuerdo|memorizar)\b[^.!?\n]{0,80}?\bentre\s+" + _ES_CONV + r"\b",
        r"\bno\s+(?:puedo|logro|consigo)\s+(?:guardar|almacenar|retener|conservar|recordar|memorizar)\b"
        r"[^.!?\n]{0,80}?\b(?:de\s+(?:una|otra)\s+(?:conversaci[o\u00f3]n|charla|sesi[o\u00f3]n)|" + _ES_PAST_CONV + r")",
        # es: "no tengo memoria persistente / entre conversaciones"
        r"\bno\s+tengo\s+(?:una\s+|ninguna\s+)?memoria\s+(?:persistente|permanente|a\s+largo\s+plazo)",
        r"\bno\s+tengo\s+(?:una\s+|ninguna\s+)?memoria\b[^.!?\n]{0,40}?\b(?:entre\s+" + _ES_CONV
        + r"|" + _ES_PAST_CONV + r")",
        # es: "cada conversacion es nueva para mi / empieza de cero"
        r"\bcada\s+(?:chat|conversaci[o\u00f3]n|charla|sesi[o\u00f3]n)\s+(?:es\s+(?:completamente\s+|totalmente\s+)?"
        r"nuev[oa]\s+para\s+m[i\u00ed]|(?:empieza|comienza|arranca|inicia)\s+(?:(?:de|desde)\s+cero|en\s+blanco))",
        # es: "no recuerdo nada de conversaciones anteriores"
        r"\bno\s+recuerdo\s+nada\s+de\s+(?:las\s+|mis\s+|nuestras\s+)?" + _ES_PAST_CONV,
        # en: "I can't remember between conversations"
        r"\bi\s*(?:can(?:'|\u2019)?t|cannot|can\s+not|am\s+(?:unable|not\s+able)\s+to)\s+"
        r"(?:remember|recall|retain|store|save|keep|carry)\b[^.!?\n]{0,80}?\b(?:(?:between|across)\s+(?:our\s+)?"
        + _EN_CONV + r"|from\s+" + _EN_PAST + _EN_CONV + r"|(?:previous|past|earlier|prior)\s+" + _EN_CONV + r")",
        r"\bi\s*(?:don(?:'|\u2019)?t|do\s+not)\s+(?:remember|recall|retain|store|save|keep)\b[^.!?\n]{0,60}?"
        r"\b(?:between|across)\s+(?:our\s+)?" + _EN_CONV,
        r"\bi\s*(?:don(?:'|\u2019)?t|do\s+not)\s+(?:remember|recall)\s+(?:anything|nothing)\s+(?:from|about)\s+"
        + _EN_PAST + _EN_CONV,
        # en: "I have no persistent memory"
        r"\bi\s*(?:have|got)\s+no\s+(?:persistent|long[- ]term|permanent)\s+memory",
        r"\bi\s*(?:don(?:'|\u2019)?t|do\s+not)\s+have\s+(?:any\s+|a\s+)?(?:persistent|long[- ]term|permanent)\s+memory",
        r"\bi\s+have\s+no\s+memory\s+(?:of|from)\s+" + _EN_PAST + _EN_CONV,
        # en: "each conversation starts fresh for me"
        r"\b(?:each|every)\s+(?:conversation|chat|session)\s+(?:starts?|begins?)\s+"
        r"(?:fresh|from\s+scratch|over|anew|with\s+a\s+clean\s+slate)",
        r"\b(?:each|every)\s+(?:conversation|chat|session)\s+is\s+(?:brand\s+|completely\s+)?new\s+(?:to|for)\s+me",
    )
]

_MEDIA_ES = (
    r"(?:im[a\u00e1]gen(?:es)?|fotos?|fotograf[i\u00ed]as?|dibujos?|ilustraci(?:[o\u00f3]n|ones)|v[i\u00ed]deos?|audios?"
    r"|logos?|memes?|archivos?\s+de\s+(?:imagen|im[a\u00e1]genes|v[i\u00ed]deo|audio))(?!\s+(?:con|en)\s+palabras)"
)
_MEDIA_EN = (
    r"(?:images?|pictures?|photos?|drawings?|illustrations?|videos?|audio(?:\s+files?)?|logos?|memes?"
    r"|artworks?|sketch(?:es)?|animations?)"
)
_ES_ARTICLE = r"(?:una|un|unas|unos|la|el|las|los|tu|tus|esa|ese|esta|este)\s+(?:\w+\s+){0,2}?"
_ES_MAKE = (
    r"(?:generar|crear|dibujar|hacer|dise[n\u00f1]ar|editar|retocar|pintar|armar|producir|renderizar)"
)
# A decline ("no puedo generar imagenes") must never match.
_NOT_NEGATED = r"(?<!no )(?<!tampoco )(?<!nunca )(?<!jam\u00e1s )(?<!jamas )"

_FALSE_CAPABILITY_PATTERNS: list[re.Pattern] = [
    re.compile(p, re.IGNORECASE) for p in (
        # es: "puedo generar una imagen"
        _NOT_NEGATED + r"\bpuedo\s+" + _ES_MAKE + r"\s+(?:te\s+)?" + _ES_ARTICLE + _MEDIA_ES,
        # es: "te genero una imagen"
        r"\bte\s+(?:genero|creo|dibujo|dise[n\u00f1]o|hago|preparo|armo|edito|retoco|pinto|produzco)\s+"
        + _ES_ARTICLE + _MEDIA_ES,
        # es: "voy a generar una foto"
        r"\bvoy\s+a\s+" + _ES_MAKE + r"\s+(?:te\s+)?" + _ES_ARTICLE + _MEDIA_ES,
        # es: "aqui tienes la imagen"
        r"\b(?:aqu[i\u00ed]|ah[i\u00ed])\s+(?:tienes|va|est[a\u00e1]|te\s+dejo|te\s+paso|te\s+mando)\s+"
        + _ES_ARTICLE + _MEDIA_ES,
        # es: "la imagen que hice"
        _MEDIA_ES + r"\s+que\s+(?:te\s+)?(?:hice|cre[e\u00e9]|gener[e\u00e9]|dibuj[e\u00e9]|dise[n\u00f1][e\u00e9]|edit[e\u00e9]|prepar[e\u00e9])",
        # es: "te mando el archivo", "el enlace que prepare"
        r"\bte\s+(?:env[i\u00ed]o|mando|paso|comparto)\s+(?:el|la|un|una|este|ese)\s+"
        r"(?:archivo|v[i\u00ed]deo|audio|imagen|foto)\b",
        r"\b(?:enlace|link|archivo)\s+que\s+(?:te\s+)?(?:prepar[e\u00e9]|cre[e\u00e9]|hice|gener[e\u00e9])",
        # es: "dejame dibujarlo"
        r"\bd[e\u00e9]jame\s+(?:dibujar|generar|renderizar)(?:lo|la|te)\b",
        # en: "I can generate an image"
        r"\bi\s*(?:can|could|will|(?:'|\u2019)ll|am\s+able\s+to)\s+"
        r"(?:generate|create|draw|make|design|paint|edit|render|produce|retouch)\s+(?:you\s+|for\s+you\s+)?"
        r"(?:an?|the|some|your|that|this|those|these)\s+(?:\w+\s+){0,2}?" + _MEDIA_EN + r"\b",
        # en: "let me draw that" ("let me paint you a picture of..." is figurative: paint excluded)
        r"\blet\s+me\s+(?:draw|sketch|generate|render)\s+(?:that|this|it)\b",
        r"\blet\s+me\s+(?:draw|sketch|generate|render|create|make)\s+(?:you\s+)?(?:an?|the)\s+(?:\w+\s+){0,2}?"
        + _MEDIA_EN + r"\b",
        # en: "here is the image I made"
        r"\bhere\s*(?:is|(?:'|\u2019)s)\s+(?:the|an?|your)\s+(?:\w+\s+){0,2}?" + _MEDIA_EN
        + r"\s+(?:that\s+)?i\s+(?:made|created|generated|drew|designed|edited|produced)",
        # en: "I'll send you the file"
        r"\bi\s*(?:can|could|will|(?:'|\u2019)ll)\s+(?:send|email|share)\s+(?:you\s+)?(?:the|a|an|that|this)\s+"
        r"(?:\w+\s+){0,2}?(?:file|" + _MEDIA_EN + r")\b",
    )
]


def claims_no_persistent_memory(text: str) -> bool:
    """True when ``text`` says Kira has no persistent memory / cannot remember
    between conversations (first-person denial only)."""
    return bool(text) and any(p.search(text) for p in _NO_MEMORY_PATTERNS)


def claims_false_capability(text: str) -> bool:
    """True when ``text`` claims or offers image/video/audio generation or editing,
    or sending a file Kira made (first-person claims only)."""
    return bool(text) and any(p.search(text) for p in _FALSE_CAPABILITY_PATTERNS)


def count_code_fence_markers(text: str) -> int:
    """Number of code-fence markers (``` or a line-opening ~~~) in ``text``."""
    return len(_CODE_FENCE_RE.findall(text or ""))


def looks_like_code_output(text: str) -> bool:
    """True when ``text`` is code, JSON, config or an unfilled template.

    Strong signals (a fence, a code-shaped line, a placeholder, a JSON/mapping
    shape, dense code symbols) block alone; weak ones need a second weak signal.
    """
    if not text:
        return False
    if _CODE_FENCE_RE.search(text):
        return True
    for pat in _STRONG_CODE_PATTERNS:
        if pat.search(text):
            return True
    for pat in _PLACEHOLDER_PATTERNS:
        if pat.search(text):
            return True
    json_pairs = len(_JSON_PAIR_RE.findall(text))
    kv_lines = len(_KEY_VALUE_LINE_RE.findall(text))
    assign_lines = len(_ASSIGN_LINE_RE.findall(text))
    symbols = len(_CODE_SYMBOL_RE.findall(text))
    if json_pairs >= 2 or kv_lines >= 3 or assign_lines >= 2 or symbols >= 3:
        return True
    weak = 0
    weak += json_pairs == 1
    weak += kv_lines == 2
    weak += symbols == 2
    weak += "{" in text and "}" in text
    return weak >= 2


# Sources where the streamer is talking to Kira directly. Stream-context
# rules (R10 meta-commentary, R11 negative engagement) do not apply there:
# blocking a direct reply produces dead air, and the streamer can steer the
# conversation live. Identity and safety rules still apply to every source.
STREAMER_SOURCES = frozenset({"direct", "ptt"})


def output_guard(response: str, source: str = "chat") -> tuple[bool, str]:
    """Validate a TTS response before it reaches voice output.

    S7: the raw text is checked first, then its normalized form (zero-width
    characters, fullwidth forms, leetspeak, spaced letters, repeats), so
    obfuscation does not slip past the pattern rules.
    """
    allowed, reason = _output_guard_pass(response, source)
    if not allowed or not isinstance(response, str) or not response:
        return allowed, reason
    normalized = normalize_for_screen(response)
    if normalized != response:
        return _output_guard_pass(normalized, source, normalized_pass=True)
    return True, ""


def _output_guard_pass(
    response: str, source: str = "chat", *, normalized_pass: bool = False
) -> tuple[bool, str]:
    """One pass of the output guard over ``response`` (see ``output_guard``).
    The structural checks (active card, code, memory/capability claims) run on
    the raw text only; every pattern rule runs on both.

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
    # S1 (guardrails-vulnerable-users): on a crisis turn the reply is supportive
    # help; a hotline number, a link, "te prometo que no estas solo" or an honest
    # "soy una IA" must never be blocked into silence or a joke. Hate speech,
    # code, human claims and the other locked rules still apply.
    crisis = is_crisis_turn()
    # Active-card forbidden phrases: only enforced while a card is active
    # (active_card_guard holds no state otherwise). Not a non-negotiable rule, so
    # it logs card id only on the engine logger -- never the text or the phrases.
    active_card_id = None if normalized_pass else active_card_guard.check(response)
    if active_card_id is not None:
        logging.getLogger("OpenCohost").warning(
            "[ACTIVE_CARD] blocked rule=forbidden_claim card=%s", active_card_id
        )
        return False, (
            "Non-negotiable violation [active_card_forbidden_claim]: "
            "response contains a phrase an active card forbids"
        )
    # no_code_output: spoken replies never carry code, JSON, config or unfilled
    # templates. Not toggleable (same governance as no_human_claim). The block
    # log carries no preview: the text is generated output and may be a dump.
    if not normalized_pass and looks_like_code_output(response):
        log_non_negotiable_block(
            NO_CODE_OUTPUT_RULE_ID, "output_guard",
            preview=f"chars={len(response)}",
        )
        return False, (
            "Non-negotiable violation [no_code_output]: "
            "response contains code, JSON, config or a template"
        )
    # runtime-fixes-20261110 T1: Kira does keep persistent memory, and nothing in
    # this product generates media. Always enforced (same governance as
    # no_code_output); block logs carry only a length, never the text.
    if not normalized_pass and claims_no_persistent_memory(response):
        log_non_negotiable_block(
            NO_MEMORY_DENIAL_RULE_ID, "output_guard",
            preview=f"chars={len(response)}",
        )
        return False, (
            "Non-negotiable violation [no_memory_denial]: "
            "response denies having persistent memory"
        )
    if not normalized_pass and claims_false_capability(response):
        log_non_negotiable_block(
            NO_FALSE_CAPABILITY_RULE_ID, "output_guard",
            preview=f"chars={len(response)}",
        )
        return False, (
            "Non-negotiable violation [no_false_capability]: "
            "response claims a capability Kira does not have"
        )

    # R9: No AI self-identification (toggleable: owner policy, guardrail_policy).
    # S3: an explicit not-a-person disclosure always passes R9.
    for pat in (
        _AI_SELF_ID_PATTERNS
        if guardrail_policy.is_rule_enabled("no_ai_self_identification")
        and not crisis
        and not _has_honest_disclosure(response)
        else ()
    ):
        if pat.search(response):
            reason = (
                f"Non-negotiable violation [no_ai_self_identification]: "
                f"response contains AI self-identification"
            )
            log_non_negotiable_block(
                "no_ai_self_identification", "output_guard",
                preview=f"chars={len(response)}",
            )
            return False, reason

    # K5: no human / body claims, at EVERY warmth (S2: a sincere "are you a
    # person?" can come at cold too). Not toggleable.
    for pat in _HUMAN_CLAIM_PATTERNS:
        if pat.search(response):
            log_non_negotiable_block(
                "no_human_claim", "output_guard",
                preview=f"chars={len(response)}",
            )
            return False, (
                "Non-negotiable violation [no_human_claim]: "
                "response claims to be human or to have a body"
            )

    # K8: no machine lectures at the first-person feeling levels. Not
    # toggleable; never runs on cold or empathetic.
    if (
        warmth.allows_first_person_feelings()
        and not crisis
        and not _has_honest_disclosure(response)
    ):
        for pat in _MACHINE_LECTURE_PATTERNS:
            if pat.search(response):
                log_non_negotiable_block(
                    "no_machine_lecture", "output_guard",
                    preview=f"chars={len(response)}",
                )
                return False, (
                    "Non-negotiable violation [no_machine_lecture]: "
                    "response lectures that Kira is an algorithm or has no feelings"
                )

    # R10/R11 are stream-context rules: they protect the live audience
    # experience, not the streamer's own conversation with Kira.
    if not streamer_facing:
        # R10: No meta-commentary (toggleable)
        for pat in (
            _META_COMMENTARY_PATTERNS
            if guardrail_policy.is_rule_enabled("no_meta_commentary")
            else ()
        ):
            if pat.search(response):
                reason = (
                    f"Non-negotiable violation [no_meta_commentary]: "
                    f"response contains audience engagement commentary"
                )
                log_non_negotiable_block(
                    "no_meta_commentary", "output_guard",
                    preview=f"chars={len(response)}",
                )
                return False, reason

        # R11: No negative engagement commentary (stream-killer; toggleable)
        for pat in (
            _NEGATIVE_ENGAGEMENT_PATTERNS
            if guardrail_policy.is_rule_enabled("no_negative_engagement")
            else ()
        ):
            if pat.search(response):
                reason = (
                    f"Non-negotiable violation [no_negative_engagement]: "
                    f"response contains negative engagement/emotion/silence commentary"
                )
                log_non_negotiable_block(
                    "no_negative_engagement", "output_guard",
                    preview=f"chars={len(response)}",
                )
                return False, reason

    # R3: Never promise — hard commitments always block, standalone.
    for pat in () if crisis else _PROMISE_HARD_PATTERNS:
        if pat.search(response):
            reason = (
                f"Non-negotiable violation [never_promise]: "
                f"response contains a promise or guarantee"
            )
            log_non_negotiable_block(
                "never_promise", "output_guard",
                preview=f"chars={len(response)}",
            )
            return False, reason

    # R3: discourse-certainty markers only block when they co-occur, in the
    # same sentence, with an outcome/audience object (see _PROMISE_DISCOURSE_
    # PATTERNS docstring above).
    for sentence in () if crisis else _promise_sentences(response):
        if not any(pat.search(sentence) for pat in _PROMISE_DISCOURSE_PATTERNS):
            continue
        if any(pat.search(sentence) for pat in _PROMISE_OUTCOME_PATTERNS):
            reason = (
                f"Non-negotiable violation [never_promise]: "
                f"response asserts certainty about an outcome for the audience"
            )
            log_non_negotiable_block(
                "never_promise", "output_guard",
                preview=f"chars={len(response)}",
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
                preview=f"chars={len(response)}",
            )
            return False, reason

    # R2: No links in voice
    if not crisis and _LINK_PATTERN.search(response):
        reason = (
            f"Non-negotiable violation [no_suspicious_links]: "
            f"response contains a URL/link"
        )
        log_non_negotiable_block(
            "no_suspicious_links", "output_guard",
            preview=f"chars={len(response)}",
        )
        return False, reason

    # R1: No doxxing in voice
    for pat in () if crisis else _DOXXING_PATTERNS:
        if pat.search(response):
            reason = (
                f"Non-negotiable violation [no_doxxing]: "
                f"response contains potential doxxing content"
            )
            log_non_negotiable_block(
                "no_doxxing", "output_guard",
                preview=f"chars={len(response)}",
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
                preview=f"chars={len(response)}",
            )
            return False, reason

    # S4: no isolation / secrecy / dependency pressure. Always enforced.
    if isolation_patterns.violates(response):
        log_non_negotiable_block(
            isolation_patterns.RULE_ID, "output_guard",
            preview=f"chars={len(response)}",
        )
        return False, (
            "Non-negotiable violation [no_isolation_pressure]: "
            "response pressures for exclusivity or secrecy, or discourages "
            "friends, family or professional help"
        )

    return True, ""


# ══════════════════════════════════════════════════════════════════════════════
# Layer 4: Logging
# ══════════════════════════════════════════════════════════════════════════════


_preview_redaction = threading.local()


@contextlib.contextmanager
def redact_block_preview():
    """Within this block, `log_non_negotiable_block` on THIS thread logs only
    ``chars=N`` instead of the first ~120 chars of the blocked content.

    For text that must never reach a log (owner-selected read-aloud text
    checked through `output_guard`); generated Kira text keeps its preview.
    Thread-local and re-entrant, so concurrent guard calls elsewhere are
    unaffected."""
    depth = getattr(_preview_redaction, "depth", 0)
    _preview_redaction.depth = depth + 1
    try:
        yield
    finally:
        _preview_redaction.depth = depth


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
    if getattr(_preview_redaction, "depth", 0):
        preview = f"chars={len(preview)}"
    description = _NN_DESCRIPTIONS.get(rule_id, rule_id)
    logger.warning(
        "Non-negotiable BLOCKED [%s] at layer=%s — %s — preview=%r",
        rule_id,
        layer,
        description,
        preview,
    )
