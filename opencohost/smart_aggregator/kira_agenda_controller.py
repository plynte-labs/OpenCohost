"""Deterministic controller for Kira Co-host Agenda Mode.

The controller owns agenda state and prompt construction.  It deliberately does
not call Ollama, TTS, OBS, or UI widgets; callers decide how to enqueue returned
actions into the existing engine.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import re
import time as _time_module
from typing import Callable, Iterable, Optional
from uuid import uuid4

from opencohost.config.logger import get_logger
from opencohost.core.context.repetition_guard import detect_repetition
# Module import, not from-import: stream/agenda tiers are resolved LIVE from
# the streamer's STREAM_OVER_AGENDA setting at mint time (see turn_priority).
from opencohost.core import turn_priority
from opencohost.i18n import active as i18n_active

logger = get_logger()


# ── Untrusted viewer-chat containment (prompt-injection defense) ──────────
# Viewer chat can reach an LLM prompt on more than one path in this codebase
# (KiraAgendaController._build_prompt below, and SmartAggregatorUI's RF3
# prompt at opencohost/ui/smart_aggregator_ui.py). wrap_untrusted_chat() is
# the single containment implementation shared by both: it wraps arbitrary
# text in read-only data delimiters and scrubs the chat body of the
# delimiter token so it cannot forge its way out of the data region.
_UNTRUSTED_CHAT_OPEN = "===CHAT_VIEWERS_DATO_NO_CONFIABLE_INICIO==="
_UNTRUSTED_CHAT_CLOSE = "===CHAT_VIEWERS_DATO_NO_CONFIABLE_FIN==="


def wrap_untrusted_chat(text: str, *, empty_fallback: str = "") -> str:
    """Wrap arbitrary untrusted viewer-chat text in read-only data delimiters.

    The delimiters are the only ``===`` runs in the output, so any run of 2+
    ``=`` in ``text`` is collapsed to a single ``=``. That makes it impossible
    for viewer text to contain ``===`` and therefore impossible to forge
    either marker — defeating single-pass-replace reconstruction
    (prefix+token+suffix recombining into a token) and case variants alike,
    since both still require a ``===`` run. Plain ``.replace`` of the whole
    token is NOT enough.
    """
    stripped = (text or "").strip()
    if not stripped:
        stripped = empty_fallback
    else:
        stripped = re.sub(r"={2,}", "=", stripped)
    return f"{_UNTRUSTED_CHAT_OPEN}\n{stripped}\n{_UNTRUSTED_CHAT_CLOSE}"


class AgendaState(str, Enum):
    OFF = "OFF"
    IDLE = "IDLE"
    SELECT_TOPIC = "SELECT_TOPIC"
    OPEN_TOPIC = "OPEN_TOPIC"
    GENERATING = "GENERATING"
    REGENERATING_SAFE = "REGENERATING_SAFE"
    SPEAKING = "SPEAKING"
    WAITING_SIGNAL = "WAITING_SIGNAL"
    HANDLE_STREAMER = "HANDLE_STREAMER"
    HANDLE_CHAT = "HANDLE_CHAT"
    CONTINUE_TOPIC = "CONTINUE_TOPIC"
    TOPIC_CLOSING = "TOPIC_CLOSING"
    PAUSED_NEEDS_OPERATOR = "PAUSED_NEEDS_OPERATOR"
    HARD_PAUSED = "HARD_PAUSED"


class ErrorCode(str, Enum):
    """Machine-readable error codes surfaced to the streamer UI."""
    NONE = ""
    GUARDRAIL_LOOPING = "ERR_GUARDRAIL_LOOPING"
    GUARDRAIL_LEAK = "ERR_GUARDRAIL_LEAK"
    GUARDRAIL_SIMILAR = "ERR_GUARDRAIL_SIMILAR"
    GUARDRAIL_EMPTY = "ERR_GUARDRAIL_EMPTY"
    LLM_TIMEOUT = "ERR_LLM_TIMEOUT"
    OLLAMA_DOWN = "ERR_OLLAMA_DOWN"
    GUARDRAIL_BREAKS_CHARACTER = "ERR_GUARDRAIL_BREAKS_CHARACTER"
    GUARDRAILS_MISSING = "ERR_GUARDRAILS_MISSING"

    def human(self) -> str:
        """Streamer-facing label. Migrated to ``ui.agenda_errors.*`` (P4,
        kira_bilingual_e2e) -- keyed by the lowercased enum member name, es
        legacy dict on any i18n failure. ``NONE`` stays a hardcoded "" (never
        a real message, no manifest key)."""
        if self is ErrorCode.NONE:
            return ""
        return i18n_active.agenda_errors().get(self.name.lower(), str(self.value))


@dataclass(frozen=True)
class CharacterContractResult:
    """Carries metadata about a detected character contract break."""
    ok: bool
    subtype: str
    matched_pattern: str
    matched_text: str


class RecoveryPolicy:
    """Degradation ladder for autonomous recovery when guardrails reject output.

    The controller calls :meth:`record_failure` on every rejection and
    :meth:`record_success` when a response passes.  Between calls the policy
    tells the controller whether to degrade, auto-retry, or hard-pause.

    Design invariants:
    - Never infinite-loop: after 3 spaced auto-retries the policy enters
      HARD_PAUSED and demands operator intervention.
    - Before PAUSED, degrade response ambition (expandida → normal → corta)
      so simpler prompts have a better chance of passing guardrails.
    - Pending chat is preserved during pauses so Kira resumes with context.
    """

    # ── configuration ──────────────────────────────────────────────────
    MAX_FAILURES_BEFORE_DEGRADE = 3       # failures before lowering ambition
    DEGRADE_FALLBACKS = ("corta",)        # response_length fallback chain
    MAX_FAILURES_BEFORE_PAUSE = 5         # failures before entering PAUSED
    RETRY_DELAYS_SECONDS = (60, 120, 240) # escalating cooldowns per retry attempt
    MAX_HISTORY = 20                      # how many failure reasons to keep

    def __init__(self) -> None:
        self._failures: int = 0
        self._retry_attempt: int = 0
        self._last_failure_time: float = 0.0
        self._last_error: ErrorCode = ErrorCode.NONE
        self._reasons: list[str] = []     # human-readable, max MAX_HISTORY
        # D2 valve fix (2026-07-31, finding 2): consecutive model-health
        # failures (GUARDRAIL_EMPTY/LLM_TIMEOUT/OLLAMA_DOWN), separate from
        # the shared `_failures` counter. Any content-class failure or a
        # success resets it to 0, so one content rejection followed by one
        # empty generation no longer trips the "model is dead" valve.
        self._consecutive_model_health_failures: int = 0

    # ── public API ─────────────────────────────────────────────────────

    def record_failure(self, error: ErrorCode, reason: str = "") -> None:
        self._failures += 1
        self._last_failure_time = _time_module.time()
        self._last_error = error
        if error in _MODEL_HEALTH_ERRORS:
            self._consecutive_model_health_failures += 1
        else:
            self._consecutive_model_health_failures = 0
        if reason:
            self._reasons.append(reason)
            self._reasons = self._reasons[-self.MAX_HISTORY:]

    def record_success(self) -> None:
        self._failures = 0
        self._retry_attempt = 0
        self._last_error = ErrorCode.NONE
        self._consecutive_model_health_failures = 0

    def set_error(self, error: ErrorCode) -> None:
        """Set the last error without touching failure/retry counters.

        Used by callers that need to surface an error code (e.g. the agenda
        fail-closed ``enable()`` gate) without participating in the
        failure/backoff ladder that :meth:`record_failure` drives.
        """
        self._last_error = error

    @property
    def error_code(self) -> ErrorCode:
        return self._last_error

    @property
    def last_failure_reason(self) -> str:
        return self._reasons[-1] if self._reasons else ""

    @property
    def last_reasons(self) -> list[str]:
        return list(self._reasons)

    @property
    def failure_count(self) -> int:
        return self._failures

    @property
    def consecutive_model_health_failures(self) -> int:
        return self._consecutive_model_health_failures

    @property
    def retry_attempt(self) -> int:
        return self._retry_attempt

    def should_degrade(self) -> bool:
        """Return True when the response_length should be lowered one step."""
        return self._failures > 0 and self._failures % self.MAX_FAILURES_BEFORE_DEGRADE == 0

    def should_auto_retry(self) -> bool:
        """Return True when the policy permits one more retry attempt."""
        if self._retry_attempt >= len(self.RETRY_DELAYS_SECONDS):
            return False
        delay = self.RETRY_DELAYS_SECONDS[self._retry_attempt]
        elapsed = _time_module.time() - self._last_failure_time
        return elapsed >= delay

    def is_hard_paused(self) -> bool:
        """Return True when all retry attempts are exhausted."""
        return self._retry_attempt >= len(self.RETRY_DELAYS_SECONDS) and self._failures >= self.MAX_FAILURES_BEFORE_PAUSE

    def consume_retry(self) -> None:
        """Mark a retry attempt as consumed (caller decided to auto-resume)."""
        self._retry_attempt += 1
        self._last_failure_time = 0.0  # reset timer for next cooldown

    def should_pause(self) -> bool:
        """Return True when failures exceed the PAUSED threshold."""
        return self._failures >= self.MAX_FAILURES_BEFORE_PAUSE

    @property
    def next_retry_seconds(self) -> float:
        """Estimated seconds until the next retry is allowed, or 0 if ready."""
        if self._retry_attempt >= len(self.RETRY_DELAYS_SECONDS):
            return -1.0  # exhausted
        delay = self.RETRY_DELAYS_SECONDS[self._retry_attempt]
        elapsed = _time_module.time() - self._last_failure_time
        return max(0.0, delay - elapsed)

    def degraded_length(self, current: str) -> str:
        """Return the next response_length in the degradation chain, or current."""
        if not self.should_degrade():
            return current
        known = ("expandida", "normal", "corta")
        if current in known:
            idx = known.index(current)
            if idx + 1 < len(known):
                return known[idx + 1]
        return "corta"  # unknown or already at minimum → shortest


class TopicStatus(str, Enum):
    DRAFTED = "drafted"
    APPROVED = "approved"
    QUEUED = "queued"
    ACTIVE = "active"
    CLOSING = "closing"
    COMPLETED = "completed"
    SKIPPED = "skipped"


# Terminal statuses never block a fresh add_topic dedup (WU4/D6).
_TERMINAL_TOPIC_STATUSES = frozenset({TopicStatus.COMPLETED, TopicStatus.SKIPPED})

# Model-health safety valve (D2, 2026-07-31): force-kill only fires for these
# errors. A model that returns nothing / times out / is down twice in a row is
# dead — a "different angle" cannot resurrect it. Content errors (SIMILAR,
# LOOPING, LEAK, BREAKS_CHARACTER) never force-kill; each rejection debits an
# attempt instead (register_failure), so a chronically-repeating topic burns
# its own budget and closes naturally.
# Coverage note: today only GUARDRAIL_EMPTY actually reaches register_failure
# on the live path (via _validate_output's empty branch). LLM_TIMEOUT and
# OLLAMA_DOWN have no caller that registers them — a timeout/down surfaces to
# the controller as an empty generation (GUARDRAIL_EMPTY), not as those two
# codes. They stay in this set for forward-looking callers that may one day
# report them directly; removing them would silently narrow the valve for
# that future caller with no test able to catch it.
_MODEL_HEALTH_ERRORS = frozenset({ErrorCode.GUARDRAIL_EMPTY, ErrorCode.LLM_TIMEOUT, ErrorCode.OLLAMA_DOWN})


@dataclass
class AgendaTopic:
    title: str
    angle: str = ""
    constraints: list[str] = field(default_factory=list)
    priority: str = "normal"
    response_length: str = "normal"
    id: str = field(default_factory=lambda: f"topic-{uuid4()}")
    status: TopicStatus = TopicStatus.DRAFTED
    # Attempts spent (D1 2026-07-31): accepted, rejected and empty live
    # generations each debit 1; the closing (kira-agenda-stop) never does.
    turns_spoken: int = 0
    confidence: str = "LOW"   # Suggester metadata: HIGH | MEDIUM | LOW
    source: str = ""           # Suggester metadata: "entity:<name>" | "vibe" | "transition"
    editorial_card_id: str | None = None
    editorial_card_consumed: bool = False
    editorial_attach_attempted: bool = False


@dataclass(frozen=True)
class AgendaAction:
    kind: str
    prompt: str = ""
    source: str = "kira-agenda"
    priority: int = 2
    topic_id: Optional[str] = None
    turns: int = 1
    # Honest text to commit to historial for this turn (agenda_ptt_commit_raw_text).
    # None (default) means "commit the prompt/contexto as before" — every caller
    # other than the PTT path stays byte-identical.
    history_text: Optional[str] = None

    @classmethod
    def none(cls) -> "AgendaAction":
        return cls(kind="none", source="none", priority=99)


class KiraAgendaController:
    """Small state machine for semi-autonomous agenda hosting.

    Public methods are intentionally event-oriented so tests and UI wiring can
    drive the controller without a real clock or background thread.
    """

    INTERNAL_PHRASES = (
        "contexto privado",
        "resumen",
        "intención dominante",
        "intencion dominante",
        "mensaje destacado",
        "el chat dice",
        "parece que el chat",
        "cantidad de mensajes",
        "número de mensajes",
        "numero de mensajes",
        "hasta luego",
        "eso es todo",
        "próximo episodio",
        "proximo episodio",
        "siguiente tema",
        "finaliza el tema",
        "cerrar el tema",
    )
    PRIORITY_ORDER = {"alta": 0, "normal": 1, "baja": 2}
    MIN_TURNS_PER_TOPIC = 1
    MAX_TURNS_PER_TOPIC = 20
    DEFAULT_TURNS_PER_TOPIC = 3
    TITLE_MAX_CHARS = 90
    ANGLE_MAX_CHARS = 1000
    CONSTRAINT_MAX_CHARS = 120
    MAX_CONSTRAINTS = 12
    SHORT_RESPONSE_TARGET_CHARS = 450
    NORMAL_RESPONSE_TARGET_CHARS = 1500
    EXPANDED_RESPONSE_HARD_CAP_CHARS = 6000
    LIVE_SAFETY_MODE_RULES = {
        "live_safe": {
            "label": "live-safe",
            "cap_chars": 1100,
            "rule": "modo live-safe: intervención corta para directo grande; hard cap 1100 caracteres (~25-40s), una idea fuerte y salida respirable.",
            "interruptible": True,
        },
        "monologue": {
            "label": "monólogo",
            "cap_chars": 3000,
            "rule": "modo monólogo: permite desarrollo largo pero interruptible; hard cap 3000 caracteres y no encadenes continuación si hay PTT/chat pendiente.",
            "interruptible": True,
        },
        "test": {
            "label": "test",
            "cap_chars": 6000,
            "rule": "modo test: permite bloques largos controlados para pruebas; hard cap 6000 caracteres (~60-90s), no usar en directos masivos salvo decisión humana.",
            "interruptible": False,
        },
    }
    LIVE_SAFETY_MODE_ALIASES = {
        "live": "live_safe",
        "seguro": "live_safe",
        "segura": "live_safe",
        "monologo": "monologue",
        "monólogo": "monologue",
        "prueba": "test",
    }
    RESPONSE_LENGTH_RULES = {
        "corta": "intervención breve pero útil: apuntá a ~450 caracteres, una idea clara con remate natural, sin desarrollar de más.",
        "normal": "mini monólogo natural y rico: apuntá a ~1500 caracteres; desarrollá una postura con ejemplos o contraste, ritmo de stream y sin sonar a cierre de sección.",
        "expandida": "monólogo largo expandido para test: desarrollá con profundidad si el modelo local puede sostenerlo; hard cap 6000 caracteres; conectá varias ideas sin repetir ni cerrar en círculo.",
    }
    RESPONSE_LENGTH_ALIASES = {
        "extensa": "expandida",
        "extendida": "expandida",
        "largo": "expandida",
        "larga": "expandida",
    }
    RHYTHM_RULES = {
        "calmo": "ritmo calmo: frases respirables, transiciones suaves y menos remates por minuto.",
        "normal": "ritmo natural de stream: fluido, conversacional, sin apurarse ni estirarse artificialmente.",
        "dinamico": "ritmo dinámico: más energía, frases ágiles y cambios de foco claros sin atropellar.",
        "dinámico": "ritmo dinámico: más energía, frases ágiles y cambios de foco claros sin atropellar.",
    }
    # Accent normalization for "dinámico" -> "dinamico" (P3b fix, kira_bilingual_e2e).
    # The i18n rules.rhythm manifest slot collapses RHYTHM_RULES's duplicate
    # accented/unaccented key into one canonical "dinamico" entry (P3a design
    # decision), so normalize_rhythm must resolve the accented input to that
    # SAME canonical key before any i18n lookup -- otherwise "dinámico" silently
    # falls back to "normal" text once _build_prompt reads locale-neutral IDs
    # from the (now accent-collapsed) bundle instead of this class-level dict.
    # Mirrors RESPONSE_LENGTH_ALIASES / LIVE_SAFETY_MODE_ALIASES's existing pattern.
    RHYTHM_ALIASES = {
        "dinámico": "dinamico",
    }
    CODE_PATTERNS = (
        r"```",
        r"<\/?[a-z][^>]*>",
        r"\b(function|class|import|from|select|insert|update|delete|drop|script|console\.log)\b",
        r"[{};]{3,}",
        r"=>",
    )

    CHARACTER_CONTRACT_ENABLED: bool = True

    # ── Character Contract Validator: precompiled regex patterns ──
    # Order matters: most specific → least specific. Short-circuits on first match.

    _PROMPT_LEAK_RE = re.compile(
        r"\b(he|has)\s+(procesado|recibido|analizado|revisado)\s+(el|la|lo)\s+(contexto|sesi[oó]n|instrucci[oó]n|prompt)"
        r"|"
        r"\b(seg[uú]n|como\s+dicen|de\s+acuerdo\s+a)\s+(mis|las)\s+(instrucciones|indicaciones|directrices)",
        re.IGNORECASE,
    )

    _PROCESSING_LEAK_RE = re.compile(
        r"\b(he|has)\s+(procesado|recibido|analizado|revisado)\s+(el|la|lo|tu|su)\s+(contexto|sesi[oó]n|reflexi[oó]n|tema|informaci[oó]n)"
        r"|"
        r"\b(recib[ií]|proces[eé]|analic[eé]|revis[eé])\s+(el|la|lo|tu|su)\s+(contexto|sesi[oó]n|reflexi[oó]n|informaci[oó]n)",
        re.IGNORECASE,
    )

    _ASSISTANT_IDENTITY_RE = re.compile(
        r"\b(soy|yo\s+soy)\s+(una|un)\s+(ia|inteligencia\s+artificial|asistente|asistente\s+virtual|modelo\s+de\s+lenguaje|llm|chatbot)"
        r"|"
        r"\b(como|en\s+mi\s+calidad\s+de)\s+(ia|asistente|modelo)",
        re.IGNORECASE,
    )

    _ASSISTANT_OFFER_RE = re.compile(
        r"\b(estoy|me\s+encuentro)\s+(lista|listo|preparada|preparado|disponible)\s+para\s+(continuar|seguir|desarrollar|ayudarte|ayudar|colaborar|asistirte)",
        re.IGNORECASE,
    )

    _META_QUESTION_RE = re.compile(
        r"\b(quieres|quer[eé]s|deseas|prefieres)\s+que\s+(sigamos|continuemos|hablemos|profundicemos|cambiemos)"
        r"|"
        r"\bde\s+qu[eé]\s+(m[aá]s|otro)\s+(podr[ií]amos|quisieras|te\s+gustar[ií]a)\s+hablar",
        re.IGNORECASE,
    )

    # NOTE: the five regexes above are frozen HISTORICAL anchors — P1a's
    # characterization test (tests/test_i18n_agenda_guardrails.py) binds the
    # es manifest's guardrails.agenda.character_contract lists to these
    # `.pattern` strings byte-for-byte, so they must not change or disappear.
    # Runtime validation no longer reads them directly: `breaks_character` and
    # `enable()`'s fail-closed gate both resolve patterns fresh from the
    # ACTIVE locale bundle via `_compile_character_patterns()` (design D2/D4)
    # — a locale that ships no guardrails.agenda gets None, never a
    # borrowed/legacy fallback.

    _CHARACTER_CONTRACT_CATEGORIES = (
        "prompt_leak",
        "processing_leak",
        "assistant_identity",
        "assistant_offer",
        "meta_question",
    )

    @staticmethod
    def _compile_character_patterns() -> tuple | None:
        """Compile ordered ``(category, compiled_regex)`` pairs from the
        active locale's ``guardrails.agenda.character_contract``, or ``None``
        when the locale ships none, an incomplete set, or malformed patterns
        (fail-closed — design D2/D4).

        ponytail: recompiled on every call rather than cached — agenda output
        is validated per accepted/rejected turn (seconds/minutes apart), not
        per token, so rejoining ~5 short pattern lists is negligible. Add an
        active-bundle-identity cache if profiling ever says otherwise.
        """
        raw = i18n_active.agenda_guardrails()
        if not raw:
            return None
        if any(category not in raw for category in KiraAgendaController._CHARACTER_CONTRACT_CATEGORIES):
            # A community-authored bundle with only unknown/typo'd category
            # keys (or a subset of the 5) must not validate with a partial
            # guard set — refuse the whole set rather than run half-guarded.
            return None
        try:
            return tuple(
                (category, re.compile("|".join(raw[category]), re.IGNORECASE))
                for category in KiraAgendaController._CHARACTER_CONTRACT_CATEGORIES
            )
        except (re.error, TypeError):
            # Malformed regex strings in a bundle must not crash controller
            # construction (app boot) or per-call recompilation in
            # breaks_character() — fail closed instead.
            return None

    def __init__(
        self,
        *,
        max_turns_per_topic: int = DEFAULT_TURNS_PER_TOPIC,
        max_failures: int = 3,
        response_length: str = "normal",
        rhythm: str = "normal",
        safety_mode: str = "live_safe",
        chat_cadence_blocks: int = 2,
    ) -> None:
        self.state = AgendaState.OFF
        self.stop_requested = False
        self.recovery = RecoveryPolicy()
        # Compiled character-contract patterns for the ACTIVE locale, read
        # once here (locale is process-immutable). None means the active
        # bundle ships no guardrails.agenda — enable() fail-closes on it.
        self._character_patterns = self._compile_character_patterns()
        self.failure_count = 0  # legacy — prefer self.recovery.failure_count
        self.max_failures = max_failures
        self.max_turns_per_topic = self.clamp_turn_limit(max_turns_per_topic)
        self.response_length = self.normalize_response_length(response_length)
        self.rhythm = self.normalize_rhythm(rhythm)
        self.safety_mode = self.normalize_safety_mode(safety_mode)
        self.chat_cadence_blocks = self.clamp_chat_cadence(chat_cadence_blocks)
        self.blocks_since_chat_check = 0
        # Prompt-only knob (D1 2026-07-31): feeds {block_size} in _build_prompt
        # so the i18n template can render "representa N beat(s)". Always 1 on
        # every production path; accounting (mark_speech_complete,
        # register_failure) never reads this field.
        self._pending_turns_spoken = 1
        self._pending_action_source = ""
        self.topics: list[AgendaTopic] = []
        self.active_topic: Optional[AgendaTopic] = None
        self.last_outputs: list[str] = []
        # ── Metrics / audit (Phase 0: zero behaviour change) ────────
        self.rejection_log: list[dict[str, object]] = []
        self._last_similarity_overlap: float = 0.0
        self._last_matched_phrase: str = ""
        # Cooldown / suggestion tracking (used by TopicSuggester integration)
        self._last_suggestion_time: float = 0.0
        self._session_suggestion_count: int = 0
        self._suggestion_cooldown_seconds: float = 120.0
        self._suggestion_session_cap: int = 5
        self._character_repair_needed: bool = False
        self.profile: dict[str, str] = {
            "style": i18n_active.agenda_defaults()["style"],
        }
        self._editorial_context_provider: Callable[[str], str | None] | None = None
        self._auto_attach_provider: Callable[[object], bool] | None = None
        # ── Regeneration ladder state ────────────────────────────────────
        self._rejected_phrases: list[str] = []
        self._regen_active: bool = False
        self._regen_retry_count: int = 0
        # TODO(next-slice): inject a short spoken filler line to cover regen latency
        #   when _regen_active=True so listeners don't hear silence during retry.

    def set_profile(self, profile: dict[str, str]) -> None:
        style = self.sanitize_topic_text((profile or {}).get("style", ""), field="profile_style", required=False)
        self.profile = {"style": style or self.profile.get("style", "")}

    def set_editorial_context_provider(self, provider: Callable[[str], str | None] | None) -> None:
        """Set the callback used to resolve one-turn Editorial Cue Card context."""

        self._editorial_context_provider = provider

    def set_auto_attach_provider(self, provider: Callable[[object], bool] | None) -> None:
        """Set the callback used to attempt auto-attach of editorial cards to topics."""

        self._auto_attach_provider = provider

    # ------------------------------------------------------------------
    # Topic lifecycle
    # ------------------------------------------------------------------

    def add_topic(
        self,
        title: str,
        angle: str = "",
        constraints: Optional[Iterable[str]] = None,
        *,
        approved: bool = False,
        priority: str = "normal",
        response_length: str = "normal",
        editorial_card_id: str | None = None,
    ) -> AgendaTopic:
        safe_title = self.sanitize_topic_text(title, field="title")
        safe_angle = self.sanitize_topic_text(angle, field="angle", required=False)
        safe_constraints = [
            self.sanitize_topic_text(c, field="constraint", required=False)
            for c in (constraints or [])
            if c and c.strip()
        ][: self.MAX_CONSTRAINTS]
        # Idempotency (WU4/D6): a double-click or retry re-sends the same
        # (title, angle). Return the existing non-terminal topic instead of
        # appending a duplicate. A COMPLETED/SKIPPED topic never blocks a fresh
        # add. This is the root-cause guard shared by CTK and the API (2939).
        for existing in self.topics:
            if (
                existing.status not in _TERMINAL_TOPIC_STATUSES
                and existing.title.casefold() == safe_title.casefold()
                and existing.angle.casefold() == safe_angle.casefold()
            ):
                return existing
        topic = AgendaTopic(
            title=safe_title,
            angle=safe_angle,
            constraints=safe_constraints,
            priority=self.normalize_priority(priority),
            response_length=self.normalize_response_length(response_length),
            status=TopicStatus.APPROVED if approved else TopicStatus.DRAFTED,
            editorial_card_id=self._sanitize_optional_identifier(editorial_card_id),
        )
        self.topics.append(topic)
        return topic

    def set_max_turns_per_topic(self, value: object) -> int:
        """Set the global topic-depth knob used by all agenda topics."""
        self.max_turns_per_topic = self.clamp_turn_limit(value)
        return self.max_turns_per_topic

    def set_session_settings(self, *, max_turns_per_topic: object | None = None, rhythm: object | None = None, response_length: object | None = None, safety_mode: object | None = None) -> None:
        """Set global agenda pacing knobs. Topics never own rhythm/length."""
        if max_turns_per_topic is not None:
            self.set_max_turns_per_topic(max_turns_per_topic)
        if rhythm is not None:
            self.rhythm = self.normalize_rhythm(rhythm)
        if response_length is not None:
            self.response_length = self.normalize_response_length(str(response_length))
        if safety_mode is not None:
            self.safety_mode = self.normalize_safety_mode(str(safety_mode))

    @classmethod
    def sanitize_topic_text(cls, value: str, *, field: str, required: bool = True) -> str:
        text = " ".join((value or "").replace("\n", " ").replace("\r", " ").split())
        if required and not text:
            raise ValueError("Agenda topic title is required")
        max_len = {
            "title": cls.TITLE_MAX_CHARS,
            "angle": cls.ANGLE_MAX_CHARS,
            "constraint": cls.CONSTRAINT_MAX_CHARS,
            "profile_style": 600,
        }.get(field, cls.CONSTRAINT_MAX_CHARS)
        if len(text) > max_len:
            raise ValueError(f"Agenda {field} is too long; max {max_len} characters")
        if cls.contains_emoji_or_symbol(text):
            raise ValueError(f"Agenda {field} contains unsupported emoji/symbol characters")
        if cls.looks_like_code(text):
            raise ValueError(f"Agenda {field} looks like code or markup")
        return text

    @staticmethod
    def contains_emoji_or_symbol(text: str) -> bool:
        return any(ord(ch) > 0xFFFF or 0x2600 <= ord(ch) <= 0x27BF for ch in text)

    @classmethod
    def looks_like_code(cls, text: str) -> bool:
        return any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in cls.CODE_PATTERNS)

    @classmethod
    def normalize_priority(cls, priority: str) -> str:
        normalized = (priority or "normal").strip().lower()
        return normalized if normalized in cls.PRIORITY_ORDER else "normal"

    @classmethod
    def normalize_response_length(cls, response_length: str) -> str:
        normalized = (response_length or "normal").strip().lower()
        normalized = cls.RESPONSE_LENGTH_ALIASES.get(normalized, normalized)
        return normalized if normalized in cls.RESPONSE_LENGTH_RULES else "normal"

    @classmethod
    def normalize_rhythm(cls, rhythm: object) -> str:
        normalized = str(rhythm or "normal").strip().lower()
        normalized = cls.RHYTHM_ALIASES.get(normalized, normalized)
        return normalized if normalized in cls.RHYTHM_RULES else "normal"

    @classmethod
    def normalize_safety_mode(cls, safety_mode: object) -> str:
        normalized = str(safety_mode or "live_safe").strip().lower().replace("-", "_")
        normalized = cls.LIVE_SAFETY_MODE_ALIASES.get(normalized, normalized)
        return normalized if normalized in cls.LIVE_SAFETY_MODE_RULES else "live_safe"

    @classmethod
    def clamp_turn_limit(cls, value: object) -> int:
        try:
            turns = int(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            turns = cls.DEFAULT_TURNS_PER_TOPIC
        return max(cls.MIN_TURNS_PER_TOPIC, min(cls.MAX_TURNS_PER_TOPIC, turns))

    @staticmethod
    def clamp_chat_cadence(value: object) -> int:
        try:
            cadence = int(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            cadence = 2
        return max(1, min(10, cadence))

    def approve_topic(self, topic_id: str) -> None:
        topic = self._topic(topic_id)
        if topic.status != TopicStatus.DRAFTED:
            raise ValueError("Only drafted topics can be approved")
        topic.status = TopicStatus.APPROVED

    def queue_topic(self, topic_id: str) -> None:
        topic = self._topic(topic_id)
        if topic.status != TopicStatus.APPROVED:
            raise ValueError("Only approved topics can enter the agenda queue")
        topic.status = TopicStatus.QUEUED

    def queued_topics(self) -> list[AgendaTopic]:
        queued = [t for t in self.topics if t.status == TopicStatus.QUEUED]
        return sorted(queued, key=lambda topic: (self.PRIORITY_ORDER.get(topic.priority, 1), self.topics.index(topic)))

    def can_suggest(self, now: float | None = None) -> bool:
        """True if cooldown and session cap allow a new suggestion batch.

        Cooldown: ≥120 s since last suggestion.
        Session cap: <5 suggestion batches this session.
        """
        if now is None:
            now = _time_module.time()
        if self._session_suggestion_count >= self._suggestion_session_cap:
            return False
        if self._last_suggestion_time > 0 and (now - self._last_suggestion_time) < self._suggestion_cooldown_seconds:
            return False
        return True

    def drafted_topics(self) -> list[AgendaTopic]:
        """Return all topics whose status is DRAFTED, for UI rendering."""
        return [t for t in self.topics if t.status == TopicStatus.DRAFTED]

    def suggest_topics(self, suggestions: list[dict]) -> list[AgendaTopic]:
        """Create DRAFTED AgendaTopic entries from raw suggestion dicts.

        Sanitizes titles and angles via the existing sanitizer, enforces
        cooldown tracking, and returns the created topics.
        """
        now = _time_module.time()
        created: list[AgendaTopic] = []
        for suggestion in suggestions:
            try:
                title = self.sanitize_topic_text(str(suggestion.get("title", "")), field="title")
                angle = self.sanitize_topic_text(str(suggestion.get("angle", "")), field="angle", required=False)
            except ValueError:
                # Skip malformed suggestions silently — rule-based source can be noisy
                continue
            topic = AgendaTopic(
                title=title,
                angle=angle,
                priority="normal",
                response_length="normal",
                status=TopicStatus.DRAFTED,
                confidence=suggestion.get("confidence", "LOW"),
                source=suggestion.get("source", ""),
            )
            self.topics.append(topic)
            created.append(topic)
        if created:
            self._last_suggestion_time = now
            self._session_suggestion_count += 1
        return created

    def remove_queued_topic(self, topic_id: str) -> None:
        topic = self._topic(topic_id)
        if topic.status != TopicStatus.QUEUED:
            raise ValueError("Only queued topics can be removed from the queue")
        topic.status = TopicStatus.SKIPPED

    def reject_topic(self, topic_id: str) -> None:
        topic = self._topic(topic_id)
        if topic.status != TopicStatus.DRAFTED:
            raise ValueError("Only drafted suggestions can be rejected")
        topic.status = TopicStatus.SKIPPED

    def move_queued_topic(self, topic_id: str, direction: int) -> None:
        topic = self._topic(topic_id)
        if topic.status != TopicStatus.QUEUED:
            raise ValueError("Only queued topics can be reordered")
        idx = self.topics.index(topic)
        step = -1 if direction < 0 else 1
        target = idx + step
        while 0 <= target < len(self.topics):
            other = self.topics[target]
            if other.status == TopicStatus.QUEUED and other.priority == topic.priority:
                self.topics[idx], self.topics[target] = self.topics[target], self.topics[idx]
                return
            target += step

    # ------------------------------------------------------------------
    # Mode controls
    # ------------------------------------------------------------------

    def enable(self) -> None:
        if self._character_patterns is None or i18n_active.agenda_banned_closures() is None:
            # Active locale shipped no character-contract patterns and/or no
            # banned_closures — fail CLOSED (design D2/D4): the gated
            # guardrails.agenda slot is "validator patterns + banned
            # closures" together, so either one missing must refuse to
            # enable, never run partially unguarded. State stays OFF.
            locale_code = i18n_active.get_active_bundle().code
            self.recovery.set_error(ErrorCode.GUARDRAILS_MISSING)
            logger.warning(
                "Agenda refused: guardrails.agenda incomplete for active "
                "locale %s; agenda mode is fail-closed.",
                locale_code,
            )
            return
        if self.state == AgendaState.OFF:
            self.state = AgendaState.IDLE
        self.stop_requested = False

    def soft_stop(self) -> AgendaAction:
        logger.info("Agenda stop: mode=soft")
        self.stop_requested = True
        if self.state in {AgendaState.OFF, AgendaState.IDLE, AgendaState.WAITING_SIGNAL} and not self.active_topic:
            self.state = AgendaState.OFF
            return AgendaAction.none()
        if self.state in {AgendaState.IDLE, AgendaState.WAITING_SIGNAL}:
            return self._closing_action()
        return AgendaAction.none()

    def _reset_ladder_state(self) -> None:
        """Clear all four regeneration-ladder fields.

        Called at every exit point where ``active_topic`` becomes ``None``
        so the invariant "ladder state is clean whenever active_topic is None"
        holds unconditionally.
        """
        self._regen_active = False
        self._rejected_phrases = []
        self._regen_retry_count = 0
        self._last_matched_phrase = ""

    def emergency_stop(self) -> None:
        logger.info("Agenda stop: mode=emergency")
        self.stop_requested = False
        self.state = AgendaState.OFF
        self.active_topic = None
        self.recovery.record_success()
        self.failure_count = 0
        # Clear regeneration ladder state so it does not survive across sessions
        self._reset_ladder_state()

    def can_auto_resume(self) -> bool:
        """Return True when the recovery policy permits an automatic retry."""
        if self.state != AgendaState.PAUSED_NEEDS_OPERATOR:
            return False
        if self.recovery.is_hard_paused():
            self.state = AgendaState.HARD_PAUSED
            return False
        if not self.recovery.should_auto_retry():
            return False
        self.recovery.consume_retry()
        self.state = AgendaState.IDLE
        return True

    def resume(self) -> None:
        if self.state in {AgendaState.PAUSED_NEEDS_OPERATOR, AgendaState.HARD_PAUSED}:
            self.recovery.record_success()
            self.failure_count = 0
            self.state = AgendaState.IDLE

    # ------------------------------------------------------------------
    # Event handling
    # ------------------------------------------------------------------

    def next_action(
        self,
        *,
        motor_busy: bool = False,
        kira_speaking: bool = False,
        ptt_text: str = "",
        compact_chat: str = "",
    ) -> AgendaAction:
        """Return the next agenda action without touching external systems."""
        if self.state in {AgendaState.OFF, AgendaState.PAUSED_NEEDS_OPERATOR, AgendaState.HARD_PAUSED}:
            return AgendaAction.none()
        if motor_busy or kira_speaking or self.state in {AgendaState.GENERATING, AgendaState.SPEAKING}:
            return AgendaAction.none()
        # Recovery: REGENERATING_SAFE means the last generation was rejected.
        # Transition to WAITING_SIGNAL so the tick retries with a fresh prompt.
        if self.state == AgendaState.REGENERATING_SAFE:
            self.state = AgendaState.WAITING_SIGNAL
        if self.stop_requested:
            return self._closing_action()

        if ptt_text.strip():
            self.state = AgendaState.HANDLE_STREAMER
            return self._streamer_action(ptt_text.strip())

        if self.state == AgendaState.IDLE:
            if self.active_topic is not None:
                # A pause (should_pause, fc>=5) or its auto-resume never
                # clears active_topic — only the recovery state moves to
                # PAUSED_NEEDS_OPERATOR and back to IDLE. Re-enter the
                # still-ACTIVE topic via the WAITING_SIGNAL path (continue,
                # close if the budget is spent) instead of selecting/
                # overwriting with a queued topic — resuming where it left
                # off, never stranding it (finding 1, 2026-07-31).
                self.state = AgendaState.WAITING_SIGNAL
                return self.next_action(
                    motor_busy=motor_busy,
                    kira_speaking=kira_speaking,
                    ptt_text=ptt_text,
                    compact_chat=compact_chat,
                )
            selected = self._select_next_topic()
            if not selected:
                return AgendaAction.none()
            self.active_topic = selected
            self._character_repair_needed = False
            # Clear regeneration ladder state on topic transition so no phrase
            # rejection from a previous topic leaks into the new topic's prompt.
            self._reset_ladder_state()
            selected.status = TopicStatus.ACTIVE
            self.state = AgendaState.OPEN_TOPIC
            return self._topic_action(i18n_active.agenda_instructions()["topic_open"])

        if self.state == AgendaState.WAITING_SIGNAL:
            if compact_chat.strip() and self._chat_due():
                self.state = AgendaState.HANDLE_CHAT
                self.blocks_since_chat_check = 0
                return self._chat_action(compact_chat.strip())
            if self._topic_complete():
                return self._closing_action()
            self.state = AgendaState.CONTINUE_TOPIC
            return self._topic_action(i18n_active.agenda_instructions()["topic_continue"])

        return AgendaAction.none()

    def prefetch_action_after_current_speech(self) -> AgendaAction:
        """Preview the next autonomous agenda action while current TTS is playing.

        This does not mutate state. AppShell may ask MotorVocalIA to generate the
        text-only response in the background, then call start_prefetched_action()
        if the cached text is still relevant once speech finishes.
        """
        if self.state != AgendaState.SPEAKING or not self.active_topic:
            return AgendaAction.none()
        if self.active_topic.status == TopicStatus.CLOSING:
            # The closing line is already playing; prefetching again would
            # generate a second goodbye for the same topic.
            return AgendaAction.none()
        projected_turns = min(
            self.max_turns_per_topic,
            self.active_topic.turns_spoken + 1,
        )
        if self.stop_requested or projected_turns >= self.max_turns_per_topic:
            # Shares the `closing` slot with _closing_action: their pre-migration
            # texts were already byte-identical (see i18n_active docstring).
            return self._preview_topic_action(
                i18n_active.agenda_instructions()["closing"],
                source="kira-agenda-stop",
                turns=1,
            )
        return self._preview_topic_action(
            i18n_active.agenda_instructions()["preview_continue"],
            source="kira-agenda",
            turns=1,
        )

    def start_prefetched_action(self, action: AgendaAction) -> bool:
        """Adopt a previously previewed agenda action right before cached TTS.

        Returns True when adopted; False means the action is stale (e.g. the
        topic already completed) and the cached audio must not be played.
        """
        if action.kind != "enqueue" or not self.active_topic:
            return False
        self._pending_turns_spoken = max(1, action.turns)
        self._pending_action_source = action.source
        if action.source == "kira-agenda-stop":
            self.active_topic.status = TopicStatus.CLOSING
            self.state = AgendaState.TOPIC_CLOSING
        else:
            self.state = AgendaState.GENERATING
        return True

    def chat_signal_due(self) -> bool:
        """Return True when a compact chat pulse may steer the next agenda beat."""
        return self.state == AgendaState.WAITING_SIGNAL and self._chat_due()

    def mark_generation_accepted(self) -> None:
        if self.state in {
            AgendaState.GENERATING,
            AgendaState.REGENERATING_SAFE,
            AgendaState.HANDLE_STREAMER,
            AgendaState.HANDLE_CHAT,
            AgendaState.CONTINUE_TOPIC,
            AgendaState.OPEN_TOPIC,
            AgendaState.TOPIC_CLOSING,
        }:
            self.state = AgendaState.SPEAKING

    def mark_speech_complete(self) -> None:
        if self.active_topic and self.state in {AgendaState.SPEAKING, AgendaState.GENERATING}:
            if self._pending_action_source == "kira-agenda":
                self.blocks_since_chat_check += 1
            elif self._pending_action_source == "chat":
                self.blocks_since_chat_check = 0
            # D1 (2026-07-31): one generation = one attempt = one debit. The
            # closing (kira-agenda-stop) never debits — its budget was already
            # spent on the normal path; this guard makes non-consumption
            # structural rather than relying on the min() cap to absorb it.
            if self.active_topic.status != TopicStatus.CLOSING:
                self.active_topic.turns_spoken = min(
                    self.max_turns_per_topic,
                    self.active_topic.turns_spoken + 1,
                )
            self._pending_turns_spoken = 1
            self._pending_action_source = ""
            if self.active_topic.status == TopicStatus.CLOSING:
                self.active_topic.status = TopicStatus.COMPLETED
                self.active_topic = None
                self._reset_ladder_state()
                self.state = AgendaState.OFF if self.stop_requested else AgendaState.IDLE
                self.stop_requested = False
                return
        # Transition to WAITING_SIGNAL from any speaking-related state.
        # GENERATING is included as a safety net: if mark_generation_accepted
        # was never called (e.g. a fire-and-forget controller action), the
        # state machine still recovers.
        if self.state in {AgendaState.SPEAKING, AgendaState.GENERATING}:
            self.state = AgendaState.WAITING_SIGNAL

    def register_failure(self, error: ErrorCode = ErrorCode.NONE, reason: str = "") -> None:
        self.recovery.record_failure(error, reason)
        self.failure_count = self.recovery.failure_count  # keep legacy counter in sync
        # ── Ladder: capture the matched/repeated phrase for negative guidance ──
        if error in (ErrorCode.GUARDRAIL_SIMILAR, ErrorCode.GUARDRAIL_LOOPING):
            phrase = self._last_matched_phrase
            if not phrase and self.last_outputs:
                # For exact repetition (is_repetition), _last_matched_phrase is not set;
                # fall back to the most recent accepted output as the phrase to avoid.
                phrase = self.last_outputs[-1]
            if phrase and phrase not in self._rejected_phrases:
                self._rejected_phrases.append(phrase)
            self._regen_active = True
            self._regen_retry_count += 1
        if error == ErrorCode.GUARDRAIL_BREAKS_CHARACTER:
            self._character_repair_needed = True
        # D1 (2026-07-31): a rejected/empty live generation still spent the
        # attempt — debit one, same as an accepted generation. The closing
        # never debits (its budget is already spent by the time it fires).
        if self.active_topic is not None and self.active_topic.status != TopicStatus.CLOSING:
            self.active_topic.turns_spoken = min(
                self.max_turns_per_topic, self.active_topic.turns_spoken + 1
            )
        # Force-complete: if the topic is already closing and we've failed
        # to generate a closing line 3+ times, just mark it done silently.
        # Prevents the infinite kira-agenda-stop retry cascade seen under
        # heavy chat + guardrail stress (20+ LLM calls in a row).
        if self.active_topic and self.active_topic.status == TopicStatus.CLOSING and self.recovery.failure_count >= 3:
            self.active_topic.status = TopicStatus.COMPLETED
            self.active_topic = None
            self._reset_ladder_state()
            self.state = AgendaState.OFF if self.stop_requested else AgendaState.IDLE
            self.stop_requested = False
            self.recovery.record_success()  # reset counter for next topic
            return
        # D2 (2026-07-31): force-kill is now a model-health safety valve only.
        # A model returning nothing / timing out / being down twice in a row
        # is dead — no "different angle" can fix that. Content errors (SIMILAR,
        # LOOPING, LEAK, BREAKS_CHARACTER) never force-kill; they debit above
        # and fall through to the degrade/pause ladder below, so a
        # chronically-repeating topic burns its own budget and closes
        # naturally instead of being killed on the first rejection.
        if self.active_topic and error in _MODEL_HEALTH_ERRORS and self.recovery.consecutive_model_health_failures >= 2:
            self.active_topic.turns_spoken = self.max_turns_per_topic
            self._character_repair_needed = False
            self.state = AgendaState.WAITING_SIGNAL
            return
        # Degradation: lower response_length before pausing
        if self.recovery.should_degrade():
            self.response_length = self.recovery.degraded_length(self.response_length)
        if self.recovery.should_pause():
            self.state = AgendaState.PAUSED_NEEDS_OPERATOR
        elif self.active_topic is None and (self.stop_requested or self.queued_topics()):
            # Belt-and-suspenders for the None-loop: a failure with no active topic
            # must never land in REGENERATING_SAFE while there is still session
            # work pending (a queued topic to advance to, or a pending stop) —
            # without a topic that state never returns to IDLE, so the queue would
            # never advance. Route to a terminal/idle state and reset the recovery
            # ladder like force-complete does (:963): IDLE selects the next topic,
            # OFF ends the session. A topic-less controller with no queue and no
            # pending stop still climbs the normal recovery ladder (degrade/pause).
            self.state = AgendaState.OFF if self.stop_requested else AgendaState.IDLE
            self.stop_requested = False
            self.recovery.record_success()
        else:
            self.state = AgendaState.REGENERATING_SAFE

    def preview_accept_output(self, output: str) -> bool:
        """Validate speculative prefetch output without mutating agenda state."""
        return self._validate_output(output, mutate=False)

    def accept_output(self, output: str) -> bool:
        """Validate an LLM output before TTS."""
        return self._validate_output(output, mutate=True)

    def record_accepted_output(self, output: str) -> None:
        """Record already accepted/spoken agenda output for future anti-loop checks."""
        clean = " ".join((output or "").strip().split())
        if not clean:
            return
        self.last_outputs.append(clean.lower())
        self.last_outputs = self.last_outputs[-5:]

    def _validate_output(self, output: str, *, mutate: bool) -> bool:
        clean = " ".join((output or "").strip().split())
        if not clean:
            if mutate:
                if self.active_topic is None:
                    # The engine fires a trailing accept_output("") after every
                    # agenda turn (llm_engine.py:5328). When the preceding
                    # rejection already force-completed the topic (active_topic is
                    # now None), re-running the failure ladder here double-counts
                    # the same rejection and, with no active topic, drops into
                    # REGENERATING_SAFE — the None-loop. Treat this trailing empty
                    # as a lightweight state-nudge instead: do NOT register a
                    # second failure; just settle into a terminal/idle state so the
                    # queue advances (IDLE) or the session ends (OFF on soft_stop).
                    if self.state not in {
                        AgendaState.OFF,
                        AgendaState.IDLE,
                        AgendaState.PAUSED_NEEDS_OPERATOR,
                        AgendaState.HARD_PAUSED,
                    }:
                        self.state = AgendaState.OFF if self.stop_requested else AgendaState.IDLE
                        self.stop_requested = False
                elif self.state in {AgendaState.GENERATING, AgendaState.TOPIC_CLOSING}:
                    # The engine fires a trailing accept_output("") after every
                    # agenda turn (llm_engine.py:5328). GENERATING/TOPIC_CLOSING
                    # are the only states a genuinely-empty live generation can
                    # arrive in — any other state means the non-empty rejection
                    # for this same turn already registered a failure a few
                    # lines earlier in the same engine call; registering again
                    # here would double-count one rejected generation into two
                    # debits (F7's double-count hazard). State-based, not
                    # caller-based, so no caller sequence can reintroduce it.
                    self.register_failure(error=ErrorCode.GUARDRAIL_EMPTY, reason="El modelo devolvió vacío")
            return False
        # ── HIGH-2: reset matched phrase so register_failure never captures a stale
        #    cross-generation value from a previous validation call.
        self._last_matched_phrase = ""
        # ── Ladder Step 2–3: trim trailing repeated sentences ──────────────
        if mutate:
            trimmed, salvageable = self.trim_trailing_repeated_sentences(clean)
            if trimmed and trimmed != clean:
                if salvageable:
                    # Use the trimmed version — commit the clean text, not the dup
                    self.recovery.record_success()
                    self.failure_count = 0
                    self._regen_active = False
                    self._rejected_phrases = []
                    self._regen_retry_count = 0
                    self.record_accepted_output(trimmed)
                    return True
                # else: can't salvage — fall through to full guardrail checks below
        # ── existing guardrail checks ──────────────────────────────────────
        error: ErrorCode = ErrorCode.NONE
        reason: str = ""
        guardrail: str = ""                         # Phase 0a: specific sub-type
        extra: dict[str, object] = {}               # Phase 0a: sub-type metadata
        if self.contains_internal_leak(clean):
            error, reason, guardrail = ErrorCode.GUARDRAIL_LEAK, "Dijo frase interna prohibida (ej. 'próximo episodio')", "contains_internal_leak"
            matched = self._matched_internal_leak_phrase(clean)
            if matched:
                extra["matched_phrase"] = matched
        elif self.is_repetition(clean):
            error, reason, guardrail = ErrorCode.GUARDRAIL_LOOPING, "Repitió exactamente una respuesta anterior", "exact_repetition"
        elif self.has_looping_lines(output):
            error, reason, guardrail = ErrorCode.GUARDRAIL_LOOPING, "Repetía líneas dentro de la misma respuesta", "looping_lines"
        elif self.repeats_recent_line(clean):
            error, reason, guardrail = ErrorCode.GUARDRAIL_SIMILAR, "Repitió una frase de respuestas recientes", "repeats_recent_line"
            if self._last_matched_phrase:
                extra["matched_phrase"] = self._last_matched_phrase[:80]
        elif self.is_too_similar_to_recent(clean):
            error, reason, guardrail = ErrorCode.GUARDRAIL_SIMILAR, "Respuesta muy parecida a las últimas 3", "token_overlap"
            extra["overlap_pct"] = round(self._last_similarity_overlap * 100, 1)
        elif (skeleton_match := detect_repetition(clean, self.last_outputs)).is_repetitive:
            # WU2: skeleton/synonym-swap detector (dead on agenda turns until now —
            # llm_engine.py:1558 gates it to source=="chat"). Runs here instead, in
            # the shared validator every caller (CTK, API, prefetch preview) routes
            # through. No raw model text goes into rejection_log — only the sub-type.
            error, reason, guardrail = ErrorCode.GUARDRAIL_SIMILAR, "Repitió el mismo esqueleto con sinónimos", "skeleton_repetition"
            if skeleton_match.detail:
                self._last_matched_phrase = skeleton_match.detail
        elif self.reuses_looping_opening(clean):
            error, reason, guardrail = ErrorCode.GUARDRAIL_LOOPING, "Reutilizó apertura tipo 'Y eso...'", "reused_opening"
        elif self.claims_inner_life(clean):
            error, reason, guardrail = ErrorCode.GUARDRAIL_LEAK, "Afirmó tener conciencia o estar viva", "claims_inner_life"
        elif self.CHARACTER_CONTRACT_ENABLED:
            result = self.breaks_character(clean)
            if result is not None:
                error, reason, guardrail = (
                    ErrorCode.GUARDRAIL_BREAKS_CHARACTER,
                    f"Kira rompió contrato de personaje: {result.subtype}",
                    "breaks_character",
                )
                extra = {
                    "subtype": result.subtype,
                    "matched_pattern": result.matched_pattern,
                    "matched_text": result.matched_text,
                }
                if mutate:
                    self._character_repair_needed = True
        if error != ErrorCode.NONE:
            if mutate:
                self.register_failure(error=error, reason=reason)
            # ── Phase 0a metrics: record every rejection with sub-type ──
            record: dict[str, object] = {
                "error": error.value,
                "guardrail": guardrail,
                "reason": reason,
                "length": self.response_length,
                "state": self.state.value,
            }
            record.update(extra)
            self.rejection_log.append(record)
            return False
        if mutate:
            self.recovery.record_success()
            self.failure_count = 0
            self._regen_active = False
            self._rejected_phrases = []
            self._regen_retry_count = 0
            self.record_accepted_output(clean)
        return True

    # ── Phase 0: Metrics (zero behaviour change) ────────────────────

    def get_metrics(self) -> dict[str, object]:
        """Return aggregated rejection metrics for causal audit / dashboard."""
        total = len(self.rejection_log)
        by_type: dict[str, int] = {}
        by_subtype: dict[str, int] = {}
        total_overlap: float = 0.0
        overlap_count: int = 0
        for r in self.rejection_log:
            err = str(r.get("error", "UNKNOWN"))
            by_type[err] = by_type.get(err, 0) + 1
            grd = str(r.get("guardrail", err))
            by_subtype[grd] = by_subtype.get(grd, 0) + 1
            ov = r.get("overlap_pct")
            if isinstance(ov, (int, float)):
                total_overlap += float(ov)
                overlap_count += 1
        return {
            "total_rejections": total,
            "by_error_code": by_type,
            "by_guardrail": by_subtype,
            "avg_similarity_overlap_pct": round(total_overlap / overlap_count, 1) if overlap_count else None,
            "current_state": self.state.value,
            "failure_count": self.recovery.failure_count,
            "response_length": self.response_length,
            "active_topic": self.active_topic.title if self.active_topic else None,
            "topics_queued": len([t for t in self.topics if t.status == TopicStatus.QUEUED]),
            "last_outputs_count": len(self.last_outputs),
        }

    def enforce_live_safety_cap(self, output: str) -> str:
        """Trim agenda output to the configured live-safety cap on sentence boundaries.

        Also strips trailing near-duplicate sentences (against last_outputs) so the
        trimmed text — not the un-trimmed original — is what reaches TTS.  The trim
        runs BEFORE the length cap so the cap always sees the de-duped text.
        """
        clean = " ".join((output or "").strip().split())
        if not clean:
            return ""
        # ── Ladder Step 2–3: strip trailing repeated sentences (transformer phase) ──
        # This must happen HERE (in the transformer) so the spoken text is clean.
        # The validator (accept_output) will then see the already-trimmed text;
        # its own trim check will be a no-op (trimmed == clean) and fall through
        # to guardrail checks, which now pass on the clean text.
        trimmed_dup, salvageable = self.trim_trailing_repeated_sentences(clean)
        if trimmed_dup and trimmed_dup != clean and salvageable:
            clean = trimmed_dup
        # ── Length cap on sentence boundaries ──────────────────────────────────────
        cap = int(self.LIVE_SAFETY_MODE_RULES[self.safety_mode]["cap_chars"])
        if len(clean) <= cap:
            return clean
        window = clean[:cap].rstrip()
        last_boundary = max(window.rfind("."), window.rfind("!"), window.rfind("?"), window.rfind("…"))
        if last_boundary >= max(120, int(cap * 0.55)):
            return window[: last_boundary + 1].strip()
        return window.rstrip(" ,;:-") + "…"

    def trim_trailing_repeated_sentences(self, text: str) -> tuple[str, bool]:
        """Trim trailing repeated sentences from output before committing to history.

        Delegates to sentence_trim.trim_trailing_repeated_sentences.
        Uses self.last_outputs as the recent context.
        """
        from opencohost.smart_aggregator.sentence_trim import (
            trim_trailing_repeated_sentences as _trim,
        )
        return _trim(text, list(self.last_outputs))

    # ------------------------------------------------------------------
    # Prompt/sanitizer helpers
    # ------------------------------------------------------------------

    @classmethod
    def contains_internal_leak(cls, output: str) -> bool:
        lowered = output.lower()
        return any(phrase in lowered for phrase in cls.INTERNAL_PHRASES)

    @classmethod
    def _matched_internal_leak_phrase(cls, output: str) -> str | None:
        """Which INTERNAL_PHRASES entry tripped contains_internal_leak, or None.

        Separate helper so contains_internal_leak's boolean contract is
        untouched for its other callers. The return value is always a literal
        from the closed INTERNAL_PHRASES vocabulary, never a slice of
        `output` — that is what makes it safe to put in rejection_log.
        """
        lowered = output.lower()
        for phrase in cls.INTERNAL_PHRASES:
            if phrase in lowered:
                return phrase
        return None

    def is_repetition(self, output: str) -> bool:
        normalized = " ".join((output or "").lower().split())
        return bool(normalized and normalized in self.last_outputs)

    @staticmethod
    def _sentences(text: str) -> list[str]:
        return [" ".join(s.lower().split()) for s in re.split(r"[.!?¡¿\n]+", text or "") if s.strip()]

    @staticmethod
    def has_looping_lines(output: str) -> bool:
        lines = [" ".join(line.lower().split()) for line in (output or "").splitlines() if line.strip()]
        if len(lines) >= 2 and lines[-1] == lines[-2]:
            return True
        sentences = [s.strip().lower() for s in re.split(r"[.!?¡¿]+", output or "") if s.strip()]
        repeated = {sentence for sentence in sentences if sentences.count(sentence) > 1 and len(sentence) > 24}
        return bool(repeated)

    def repeats_recent_line(self, output: str) -> bool:
        current_sentences = {s for s in self._sentences(output) if len(s) > 24}
        if not current_sentences:
            return False
        for recent in self.last_outputs[-5:]:
            matches = current_sentences.intersection({s for s in self._sentences(recent) if len(s) > 24})
            if matches:
                self._last_matched_phrase = next(iter(matches))
                return True
        return False

    @staticmethod
    def _tokens(text: str) -> set[str]:
        return {token for token in re.findall(r"[a-záéíóúñü0-9]{4,}", (text or "").lower()) if token not in {"porque", "entonces", "también", "tambien", "sobre"}}

    def is_too_similar_to_recent(self, output: str) -> bool:
        tokens = self._tokens(output)
        if len(tokens) < 8:
            return False
        for recent in self.last_outputs[-3:]:
            recent_tokens = self._tokens(recent)
            if len(recent_tokens) < 8:
                continue
            overlap = len(tokens & recent_tokens) / max(1, min(len(tokens), len(recent_tokens)))
            if overlap >= 0.78:
                self._last_similarity_overlap = overlap
                return True
        return False

    def reuses_looping_opening(self, output: str) -> bool:
        opening = " ".join((output or "").lower().split())[:28]
        looping = ("y eso", "y en eso", "y ahí", "y ahi", "y así nos damos cuenta", "y asi nos damos cuenta")
        if not opening.startswith(looping):
            return False
        return any((recent or "").startswith(looping) for recent in self.last_outputs[-3:])

    @staticmethod
    def claims_inner_life(output: str) -> bool:
        lowered = (output or "").lower()
        forbidden = (
            "me siento viva",
            "estoy viva",
            "tengo alma",
            "mi alma",
            "soy diferente a otras ia",
            "soy distinta a otras ia",
            "soy consciente",
            "tengo conciencia",
        )
        return any(phrase in lowered for phrase in forbidden)

    @staticmethod
    def breaks_character(text: str) -> CharacterContractResult | None:
        """Return CharacterContractResult if text breaks character contract, else None.

        Iterates the active locale's compiled character-contract patterns in
        priority order. Short-circuits on the first match. Returns None for
        empty/whitespace-only input, or when the active locale ships no
        patterns (nothing to check against — the fail-closed `enable()` gate
        is what keeps agenda mode off in that case, not this validator).
        """
        if not text or not text.strip():
            return None
        patterns = KiraAgendaController._compile_character_patterns()
        if not patterns:
            return None
        lowered = text.lower()
        for category, pattern in patterns:
            m = pattern.search(lowered)
            if m:
                return CharacterContractResult(
                    ok=False,
                    subtype=category,
                    matched_pattern=category,
                    matched_text=m.group(0)[:80],
                )
        return None

    def _topic_action(self, instruction: str) -> AgendaAction:
        self._pending_turns_spoken = 1
        self._pending_action_source = "kira-agenda"
        prompt = self._build_prompt(
            instruction=instruction,
            editorial_context=self._consume_editorial_context_block(),
        )
        self.state = AgendaState.GENERATING
        return AgendaAction(kind="enqueue", prompt=prompt, source="kira-agenda", priority=turn_priority.agenda_priority(), topic_id=self.active_topic.id if self.active_topic else None, turns=self._pending_turns_spoken)

    def _preview_topic_action(self, instruction: str, *, source: str, turns: int) -> AgendaAction:
        previous_turns = self._pending_turns_spoken
        try:
            self._pending_turns_spoken = max(1, turns)
            prompt = self._build_prompt(instruction=instruction)
        finally:
            self._pending_turns_spoken = previous_turns
        return AgendaAction(kind="enqueue", prompt=prompt, source=source, priority=turn_priority.agenda_priority(), topic_id=self.active_topic.id if self.active_topic else None, turns=max(1, turns))

    def _chat_action(self, compact_chat: str) -> AgendaAction:
        self._pending_turns_spoken = 1
        self._pending_action_source = "chat"
        prompt = self._build_prompt(
            instruction=i18n_active.agenda_instructions()["chat"],
            compact_chat=compact_chat,
        )
        self.state = AgendaState.GENERATING
        # Failure mode 4 (tauri_stream_chat_20260812 §3.2): this used to mint
        # priority=1 — the DIRECT tier — so the agenda's own chat replies
        # skipped its tier and outranked a typed owner question. Viewer chat
        # answered from agenda mode is stream content: it takes the stream
        # tier, wherever the streamer ordered it relative to agenda.
        return AgendaAction(kind="enqueue", prompt=prompt, source="chat", priority=turn_priority.stream_priority(), topic_id=self.active_topic.id if self.active_topic else None, turns=1)

    def _streamer_action(self, ptt_text: str) -> AgendaAction:
        self._pending_turns_spoken = 1
        self._pending_action_source = "ptt"
        prompt = self._build_prompt(
            instruction=i18n_active.agenda_instructions()["ptt"],
            ptt_text=ptt_text,
        )
        self.state = AgendaState.GENERATING
        # Honest history_text: the streamer's real words, not the full prompt
        # template. Own dedicated i18n slot (ptt_history_wrapper), NOT shared
        # with voice_control's ptt_wrapper: the wording already differed
        # pre-migration ("dijo" vs "acaba de decir") and
        # test_kira_agenda_controller.py pins this exact wording byte-for-byte
        # — see active.py's LEGACY_PTT_HISTORY_WRAPPER note. Only called from
        # next_action() with a guaranteed non-empty ptt_text.strip() (see the
        # `if ptt_text.strip():` guard above) — no empty-text fallback needed.
        history_text = i18n_active.ptt_history_wrapper().format(text=ptt_text)
        return AgendaAction(kind="enqueue", prompt=prompt, source="ptt", priority=turn_priority.PRIORITY_PTT, topic_id=self.active_topic.id if self.active_topic else None, turns=1, history_text=history_text)

    def _closing_action(self) -> AgendaAction:
        if self.active_topic:
            self.active_topic.status = TopicStatus.CLOSING
        self.state = AgendaState.TOPIC_CLOSING
        self._pending_turns_spoken = 1
        self._pending_action_source = "kira-agenda-stop"
        prompt = self._build_prompt(instruction=i18n_active.agenda_instructions()["closing"])
        self.state = AgendaState.GENERATING
        return AgendaAction(kind="enqueue", prompt=prompt, source="kira-agenda-stop", priority=turn_priority.agenda_priority(), topic_id=self.active_topic.id if self.active_topic else None, turns=1)

    # ── Untrusted viewer-chat containment (prompt-injection defense) ──
    # Class-level names kept for backward compatibility (tests reference
    # them directly); the implementation lives in wrap_untrusted_chat()
    # at module scope, shared with SmartAggregatorUI's RF3 prompt path.
    _CHAT_DATA_OPEN = _UNTRUSTED_CHAT_OPEN
    _CHAT_DATA_CLOSE = _UNTRUSTED_CHAT_CLOSE

    def _wrap_untrusted_chat(self, compact_chat: str) -> str:
        """Wrap viewer chat in read-only data delimiters. See wrap_untrusted_chat()."""
        return wrap_untrusted_chat(compact_chat, empty_fallback="- sin chat compacto fresco")

    def _build_prompt(self, *, instruction: str, compact_chat: str = "", ptt_text: str = "", editorial_context: str = "") -> str:
        # ── Assembled from i18n bundle slots (kira_bilingual_e2e P3b) ──
        # Engine keeps: ladder state (repair/dup_block triggers below),
        # _wrap_untrusted_chat (untouched — out of scope for this migration),
        # block-size math, and rule *selection* (the .get(...) lookups below
        # are unchanged logic, only the source dict is now locale-resolved).
        repair_prefix = ""
        if self.CHARACTER_CONTRACT_ENABLED and self._character_repair_needed:
            self._character_repair_needed = False
            repair_prefix = i18n_active.agenda_repair_prefix()
        # ── Regeneration ladder: negative guidance for repeated phrases ──
        dup_block = ""
        if self._regen_active and self._rejected_phrases:
            phrases = "; ".join(f'"{p[:60]}"' for p in self._rejected_phrases[:5])
            dup_block = i18n_active.agenda_anti_repetition_template().format(phrases=phrases)
        topic = self.active_topic
        defaults = i18n_active.agenda_defaults()
        title = topic.title if topic else defaults["title"]
        angle = topic.angle if topic and topic.angle else defaults["angle"]
        constraints = "\n".join(f"- {c}" for c in (topic.constraints if topic else [])) or defaults["constraints"]
        rules_length = i18n_active.agenda_rules_length()
        rules_rhythm = i18n_active.agenda_rules_rhythm()
        rules_safety = i18n_active.agenda_rules_safety()
        response_rule = rules_length.get(self.response_length, rules_length["normal"])
        rhythm_rule = rules_rhythm.get(self.rhythm, rules_rhythm["normal"])
        safety_rule = rules_safety.get(self.safety_mode, rules_safety["live_safe"])
        block_size = self._pending_turns_spoken if topic else 1
        last_lines = "\n".join(f"- {line}" for line in self.last_outputs[-3:]) or defaults["last_lines"]
        style = self.profile.get("style") or defaults["style_fallback"]
        parts = dict(
            short_target=self.SHORT_RESPONSE_TARGET_CHARS,
            normal_target=self.NORMAL_RESPONSE_TARGET_CHARS,
            expanded_cap=self.EXPANDED_RESPONSE_HARD_CAP_CHARS,
            instruction=instruction,
            block_size=block_size,
            style=style,
            title=title,
            angle=angle,
            rhythm_rule=rhythm_rule,
            response_rule=response_rule,
            safety_rule=safety_rule["rule"],
            interruption_rule=safety_rule["interruption_rule"],
            constraints=constraints,
            # ptt_text is operator-controlled (streamer PTT hardware/STT) — trusted at
            # this layer, so it is intentionally NOT delimited like untrusted viewer
            # chat. Do not route untrusted input through ptt_text without adding the
            # same containment used for compact_chat.
            ptt_block=ptt_text or defaults["ptt_block"],
            chat_block=self._wrap_untrusted_chat(compact_chat),
            editorial_block=editorial_context or defaults["editorial_block"],
            last_lines=last_lines,
        )
        return repair_prefix + dup_block + i18n_active.agenda_task_template().format(**parts)

    def _consume_editorial_context_block(self) -> str:
        topic = self.active_topic
        if not topic:
            return ""
        # Auto-attach: attempt once per topic if no card is already linked
        if (
            topic.editorial_card_id is None
            and not topic.editorial_attach_attempted
            and self._auto_attach_provider is not None
        ):
            topic.editorial_attach_attempted = True
            try:
                self._auto_attach_provider(topic)
            except Exception:
                pass
        # Original logic
        if not topic.editorial_card_id or topic.editorial_card_consumed:
            return ""
        topic.editorial_card_consumed = True
        provider = self._editorial_context_provider
        if provider is None:
            return ""
        try:
            return provider(topic.editorial_card_id) or ""
        except Exception:
            return ""

    @staticmethod
    def _sanitize_optional_identifier(value: str | None) -> str | None:
        text = " ".join((value or "").split())
        return text[:120] if text else None

    def _select_next_topic(self) -> Optional[AgendaTopic]:
        queued = self.queued_topics()
        return queued[0] if queued else None

    def _topic_complete(self) -> bool:
        return bool(self.active_topic and self.active_topic.turns_spoken >= self.max_turns_per_topic)

    def _chat_due(self) -> bool:
        return self.blocks_since_chat_check >= self.chat_cadence_blocks

    def _topic(self, topic_id: str) -> AgendaTopic:
        for topic in self.topics:
            if topic.id == topic_id:
                return topic
        raise KeyError(f"Unknown agenda topic: {topic_id}")
