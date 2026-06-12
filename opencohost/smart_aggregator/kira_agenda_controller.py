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

    def human(self) -> str:
        _MAP = {
            ErrorCode.NONE: "",
            ErrorCode.GUARDRAIL_LOOPING: "Respuesta repetitiva",
            ErrorCode.GUARDRAIL_LEAK: "Frase interna filtrada",
            ErrorCode.GUARDRAIL_SIMILAR: "Respuesta muy parecida a la anterior",
            ErrorCode.GUARDRAIL_EMPTY: "El modelo no generó respuesta",
            ErrorCode.LLM_TIMEOUT: "El modelo tardó demasiado",
            ErrorCode.OLLAMA_DOWN: "Ollama no está respondiendo",
            ErrorCode.GUARDRAIL_BREAKS_CHARACTER: "Kira rompió el contrato de personaje",
        }
        return _MAP.get(self, str(self.value))


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

    # ── public API ─────────────────────────────────────────────────────

    def record_failure(self, error: ErrorCode, reason: str = "") -> None:
        self._failures += 1
        self._last_failure_time = _time_module.time()
        self._last_error = error
        if reason:
            self._reasons.append(reason)
            self._reasons = self._reasons[-self.MAX_HISTORY:]

    def record_success(self) -> None:
        self._failures = 0
        self._retry_attempt = 0
        self._last_error = ErrorCode.NONE

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


@dataclass
class AgendaTopic:
    title: str
    angle: str = ""
    constraints: list[str] = field(default_factory=list)
    priority: str = "normal"
    response_length: str = "normal"
    id: str = field(default_factory=lambda: f"topic-{uuid4()}")
    status: TopicStatus = TopicStatus.DRAFTED
    turns_spoken: int = 0
    confidence: str = "LOW"   # Suggester metadata: HIGH | MEDIUM | LOW
    source: str = ""           # Suggester metadata: "entity:<name>" | "vibe" | "transition"
    editorial_card_id: str | None = None
    editorial_card_consumed: bool = False


@dataclass(frozen=True)
class AgendaAction:
    kind: str
    prompt: str = ""
    source: str = "kira-agenda"
    priority: int = 2
    topic_id: Optional[str] = None
    turns: int = 1

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

    _CHARACTER_PATTERNS: tuple = (
        ("prompt_leak",        _PROMPT_LEAK_RE),
        ("processing_leak",    _PROCESSING_LEAK_RE),
        ("assistant_identity", _ASSISTANT_IDENTITY_RE),
        ("assistant_offer",    _ASSISTANT_OFFER_RE),
        ("meta_question",      _META_QUESTION_RE),
    )

    def __init__(
        self,
        *,
        max_turns_per_topic: int = DEFAULT_TURNS_PER_TOPIC,
        max_failures: int = 3,
        response_length: str = "normal",
        rhythm: str = "normal",
        safety_mode: str = "live_safe",
        turn_batch_size: int = 2,
        chat_cadence_blocks: int = 2,
    ) -> None:
        self.state = AgendaState.OFF
        self.stop_requested = False
        self.recovery = RecoveryPolicy()
        self.failure_count = 0  # legacy — prefer self.recovery.failure_count
        self.max_failures = max_failures
        self.max_turns_per_topic = self.clamp_turn_limit(max_turns_per_topic)
        self.response_length = self.normalize_response_length(response_length)
        self.rhythm = self.normalize_rhythm(rhythm)
        self.safety_mode = self.normalize_safety_mode(safety_mode)
        self.turn_batch_size = self.clamp_turn_batch_size(turn_batch_size)
        self.chat_cadence_blocks = self.clamp_chat_cadence(chat_cadence_blocks)
        self.blocks_since_chat_check = 0
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
            "style": "Soná como co-host natural de stream: cercana, con humor seco, sin anunciar estructura ni despedirte entre ideas.",
        }
        self._editorial_context_provider: Callable[[str], str | None] | None = None

    def set_profile(self, profile: dict[str, str]) -> None:
        style = self.sanitize_topic_text((profile or {}).get("style", ""), field="profile_style", required=False)
        self.profile = {"style": style or self.profile.get("style", "")}

    def set_editorial_context_provider(self, provider: Callable[[str], str | None] | None) -> None:
        """Set the callback used to resolve one-turn Editorial Cue Card context."""

        self._editorial_context_provider = provider

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
        if normalized == "dinamico":
            return "dinamico"
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
    def clamp_turn_batch_size(value: object) -> int:
        try:
            turns = int(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            turns = 2
        return max(1, min(4, turns))

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
        if self.state == AgendaState.OFF:
            self.state = AgendaState.IDLE
        self.stop_requested = False

    def soft_stop(self) -> AgendaAction:
        self.stop_requested = True
        if self.state in {AgendaState.OFF, AgendaState.IDLE, AgendaState.WAITING_SIGNAL} and not self.active_topic:
            self.state = AgendaState.OFF
            return AgendaAction.none()
        if self.state in {AgendaState.IDLE, AgendaState.WAITING_SIGNAL}:
            return self._closing_action()
        return AgendaAction.none()

    def emergency_stop(self) -> None:
        self.stop_requested = False
        self.state = AgendaState.OFF
        self.active_topic = None
        self.recovery.record_success()
        self.failure_count = 0

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
            selected = self._select_next_topic()
            if not selected:
                return AgendaAction.none()
            self.active_topic = selected
            self._character_repair_needed = False
            selected.status = TopicStatus.ACTIVE
            self.state = AgendaState.OPEN_TOPIC
            return self._topic_action("Entrá al tema como comentario orgánico de stream. No anuncies que estás abriendo un tema.")

        if self.state == AgendaState.WAITING_SIGNAL:
            if compact_chat.strip() and self._chat_due():
                self.state = AgendaState.HANDLE_CHAT
                self.blocks_since_chat_check = 0
                return self._chat_action(compact_chat.strip())
            if self._topic_complete():
                return self._closing_action()
            self.state = AgendaState.CONTINUE_TOPIC
            return self._topic_action("Seguí desarrollando la postura como si fuera una conversación viva. No digas que vas al siguiente tema ni que estás cerrando.")

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
            self.active_topic.turns_spoken + max(1, self._pending_turns_spoken),
        )
        if self.stop_requested or projected_turns >= self.max_turns_per_topic:
            return self._preview_topic_action(
                "Hacé una transición natural a otra idea sin despedirte, sin decir 'tema', 'episodio', 'eso es todo' ni anunciar estructura.",
                source="kira-agenda-stop",
                turns=1,
            )
        turns = min(self.turn_batch_size, self.max_turns_per_topic - projected_turns)
        return self._preview_topic_action(
            "Seguí desarrollando la postura como bloque fluido de stream. Sumá un ángulo nuevo, no repitas ni cierres en círculo.",
            source="kira-agenda",
            turns=max(1, turns),
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
            self.active_topic.turns_spoken = min(
                self.max_turns_per_topic,
                self.active_topic.turns_spoken + max(1, self._pending_turns_spoken),
            )
            self._pending_turns_spoken = 1
            self._pending_action_source = ""
            if self.active_topic.status == TopicStatus.CLOSING:
                self.active_topic.status = TopicStatus.COMPLETED
                self.active_topic = None
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
        if error == ErrorCode.GUARDRAIL_BREAKS_CHARACTER:
            self._character_repair_needed = True
        # Force-complete: if the topic is already closing and we've failed
        # to generate a closing line 3+ times, just mark it done silently.
        # Prevents the infinite kira-agenda-stop retry cascade seen under
        # heavy chat + guardrail stress (20+ LLM calls in a row).
        if self.active_topic and self.active_topic.status == TopicStatus.CLOSING and self.recovery.failure_count >= 3:
            self.active_topic.status = TopicStatus.COMPLETED
            self.active_topic = None
            self.state = AgendaState.OFF if self.stop_requested else AgendaState.IDLE
            self.stop_requested = False
            self.recovery.record_success()  # reset counter for next topic
            return
        if self.active_topic and self.recovery.failure_count >= 2:
            self.active_topic.turns_spoken = self.max_turns_per_topic
            self._character_repair_needed = False
            self.state = AgendaState.WAITING_SIGNAL
            return
        # Degradation: lower response_length before pausing
        if self.recovery.should_degrade():
            self.response_length = self.recovery.degraded_length(self.response_length)
        if self.recovery.should_pause():
            self.state = AgendaState.PAUSED_NEEDS_OPERATOR
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
                self.register_failure(error=ErrorCode.GUARDRAIL_EMPTY, reason="El modelo devolvió vacío")
            return False
        error: ErrorCode = ErrorCode.NONE
        reason: str = ""
        guardrail: str = ""                         # Phase 0a: specific sub-type
        extra: dict[str, object] = {}               # Phase 0a: sub-type metadata
        if self.contains_internal_leak(clean):
            error, reason, guardrail = ErrorCode.GUARDRAIL_LEAK, "Dijo frase interna prohibida (ej. 'próximo episodio')", "contains_internal_leak"
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
        """Trim agenda output to the configured live-safety cap on sentence boundaries."""
        clean = " ".join((output or "").strip().split())
        if not clean:
            return ""
        cap = int(self.LIVE_SAFETY_MODE_RULES[self.safety_mode]["cap_chars"])
        if len(clean) <= cap:
            return clean
        window = clean[:cap].rstrip()
        last_boundary = max(window.rfind("."), window.rfind("!"), window.rfind("?"), window.rfind("…"))
        if last_boundary >= max(120, int(cap * 0.55)):
            return window[: last_boundary + 1].strip()
        return window.rstrip(" ,;:-") + "…"

    # ------------------------------------------------------------------
    # Prompt/sanitizer helpers
    # ------------------------------------------------------------------

    @classmethod
    def contains_internal_leak(cls, output: str) -> bool:
        lowered = output.lower()
        return any(phrase in lowered for phrase in cls.INTERNAL_PHRASES)

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

        Iterates all precompiled CHARACTER_PATTERNS in priority order.
        Short-circuits on the first match.  Returns None for empty/whitespace-only input.
        """
        if not text or not text.strip():
            return None
        lowered = text.lower()
        for category, pattern in KiraAgendaController._CHARACTER_PATTERNS:
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
        self._pending_turns_spoken = self._next_block_size()
        self._pending_action_source = "kira-agenda"
        prompt = self._build_prompt(
            instruction=instruction,
            editorial_context=self._consume_editorial_context_block(),
        )
        self.state = AgendaState.GENERATING
        return AgendaAction(kind="enqueue", prompt=prompt, source="kira-agenda", priority=2, topic_id=self.active_topic.id if self.active_topic else None, turns=self._pending_turns_spoken)

    def _preview_topic_action(self, instruction: str, *, source: str, turns: int) -> AgendaAction:
        previous_turns = self._pending_turns_spoken
        try:
            self._pending_turns_spoken = max(1, turns)
            prompt = self._build_prompt(instruction=instruction)
        finally:
            self._pending_turns_spoken = previous_turns
        return AgendaAction(kind="enqueue", prompt=prompt, source=source, priority=2, topic_id=self.active_topic.id if self.active_topic else None, turns=max(1, turns))

    def _chat_action(self, compact_chat: str) -> AgendaAction:
        self._pending_turns_spoken = 1
        self._pending_action_source = "chat"
        prompt = self._build_prompt(
            instruction="Integrá el contexto compacto si suma, sin decir que viene del chat. Si no suma, seguí natural con la idea actual.",
            compact_chat=compact_chat,
        )
        self.state = AgendaState.GENERATING
        return AgendaAction(kind="enqueue", prompt=prompt, source="chat", priority=1, topic_id=self.active_topic.id if self.active_topic else None, turns=1)

    def _streamer_action(self, ptt_text: str) -> AgendaAction:
        self._pending_turns_spoken = 1
        self._pending_action_source = "ptt"
        prompt = self._build_prompt(
            instruction="Respondé o ajustá el rumbo según esta indicación del streamer. No inventes que dijo otra cosa.",
            ptt_text=ptt_text,
        )
        self.state = AgendaState.GENERATING
        return AgendaAction(kind="enqueue", prompt=prompt, source="ptt", priority=0, topic_id=self.active_topic.id if self.active_topic else None, turns=1)

    def _closing_action(self) -> AgendaAction:
        if self.active_topic:
            self.active_topic.status = TopicStatus.CLOSING
        self.state = AgendaState.TOPIC_CLOSING
        self._pending_turns_spoken = 1
        self._pending_action_source = "kira-agenda-stop"
        prompt = self._build_prompt(instruction="Hacé una transición natural a otra idea sin despedirte, sin decir 'tema', 'episodio', 'eso es todo' ni anunciar estructura.")
        self.state = AgendaState.GENERATING
        return AgendaAction(kind="enqueue", prompt=prompt, source="kira-agenda-stop", priority=2, topic_id=self.active_topic.id if self.active_topic else None, turns=1)

    def _build_prompt(self, *, instruction: str, compact_chat: str = "", ptt_text: str = "", editorial_context: str = "") -> str:
        repair_prefix = ""
        if self.CHARACTER_CONTRACT_ENABLED and self._character_repair_needed:
            self._character_repair_needed = False
            repair_prefix = (
                "REESCRIBE: hablá como Kira, la co-host. "
                "No menciones contexto, sesión, reflexión, procesamiento. "
                "No preguntes qué hacer ni ofrezcas ayuda. "
                "No digas que sos una IA. "
                "Solo hablá natural como co-host de stream.\n\n"
            )
        topic = self.active_topic
        title = topic.title if topic else "sin tema activo"
        angle = topic.angle if topic and topic.angle else "mantenerlo concreto, entretenido y seguro"
        constraints = "\n".join(f"- {c}" for c in (topic.constraints if topic else [])) or "- 1-2 frases cortas.\n- Una idea por turno."
        response_rule = self.RESPONSE_LENGTH_RULES.get(self.response_length, self.RESPONSE_LENGTH_RULES["normal"])
        rhythm_rule = self.RHYTHM_RULES.get(self.rhythm, self.RHYTHM_RULES["normal"])
        safety_rule = self.LIVE_SAFETY_MODE_RULES.get(self.safety_mode, self.LIVE_SAFETY_MODE_RULES["live_safe"])
        block_size = self._pending_turns_spoken if topic else 1
        last = "\n".join(f"- {line}" for line in self.last_outputs[-3:]) or "- nada todavía"
        style = self.profile.get("style") or "Soná natural, como co-host de stream."
        return repair_prefix + (
            "TAREA: respondé al aire como Kira, co-host del stream, no como streamer.\n"
            "SALIDA PERMITIDA: solo la frase final que Kira diría por TTS.\n"
            "Debe sonar como una intervención natural en vivo, no como guion de presentación ni cierre de episodio.\n"
            "Usá el contexto en privado; NO describas el contexto ni tu proceso.\n"
            "PROHIBIDO decir 'resumen', 'contexto privado', 'intención dominante', 'mensaje destacado', 'el chat dice', cantidades de mensajes/autores, 'hasta luego', 'próximo episodio', 'eso es todo', 'siguiente tema' o 'finaliza'.\n"
            "No afirmes que el streamer dijo/hizo algo salvo que venga en PTT.\n"
            "No tomes posturas extremas como verdad absoluta: si el tema es polémico, opiná con picante pero con matiz y sin declararte superior/dios/invencible.\n"
            "Si el contexto es débil, seguí con una observación concreta o una pregunta liviana.\n\n"
            "ANTI-LOOP: no repitas la misma idea, no repitas la última línea exacta, no reutilices la misma metáfora en turnos consecutivos y no cierres en círculo.\n"
            "ANTI-LOOP AVANZADO: evitá aperturas repetidas tipo 'Y eso...' / 'Y en eso...'; cada turno debe sumar un ángulo nuevo, no parafrasear el anterior.\n"
            "IDENTIDAD: podés reflexionar sobre IA y humanos, pero NO digas que estás viva, que tenés alma, conciencia, sentimientos reales o que sos especial frente a otras IA.\n"
            f"Si la longitud es corta, rondá {self.SHORT_RESPONSE_TARGET_CHARS} caracteres. Si es normal, rondá {self.NORMAL_RESPONSE_TARGET_CHARS}. Si es expandida, sostené desarrollo largo pero cortá antes de {self.EXPANDED_RESPONSE_HARD_CAP_CHARS} caracteres.\n\n"
            f"INSTRUCCIÓN: {instruction}\n\n"
            f"CADENCIA DE BLOQUE: esta llamada representa {block_size} beat(s) de agenda. Generá un bloque cohesivo, no una frase de relleno aislada; desarrollá, conectá y dejá aire para continuar sin cerrar artificialmente.\n"
            f"ESTILO CONFIGURADO POR EL OPERADOR, RESPETAR SIN ROMPER REGLAS: {style}\n\n"
            f"TEMA APROBADO: {title}\n"
            f"ÁNGULO: {angle}\n"
            f"RITMO GLOBAL: {rhythm_rule}\n"
            f"LONGITUD DE RESPUESTA: {response_rule}\n"
            f"MODO DE SEGURIDAD EN VIVO: {safety_rule['rule']}\n"
            f"INTERRUPCIÓN HUMANA: {'si entra PTT/chat, no continúes este bloque largo en el próximo turno.' if safety_rule['interruptible'] else 'modo de prueba; aun así respetá stop/emergencia del operador.'}\n"
            f"RESTRICCIONES:\n{constraints}\n\n"
            f"PTT DEL STREAMER, SI EXISTE:\n{ptt_text or '- sin PTT'}\n\n"
            f"CHAT COMPACTO FILTRADO, SI EXISTE:\n{compact_chat or '- sin chat compacto fresco'}\n\n"
            f"EDITORIAL CUE CARD, SI EXISTE; USAR UNA SOLA VEZ Y NO MENCIONAR LA ESTRUCTURA:\n{editorial_context or '- sin cue card editorial activo'}\n\n"
            f"ÚLTIMAS LÍNEAS DE KIRA; NO REPETIR NI PARAFRASEAR:\n{last}"
        )

    def _consume_editorial_context_block(self) -> str:
        topic = self.active_topic
        if not topic or not topic.editorial_card_id or topic.editorial_card_consumed:
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

    def _next_block_size(self) -> int:
        if not self.active_topic:
            return 1
        remaining = max(1, self.max_turns_per_topic - self.active_topic.turns_spoken)
        return min(self.turn_batch_size, remaining)

    def _chat_due(self) -> bool:
        return self.blocks_since_chat_check >= self.chat_cadence_blocks

    def _topic(self, topic_id: str) -> AgendaTopic:
        for topic in self.topics:
            if topic.id == topic_id:
                return topic
        raise KeyError(f"Unknown agenda topic: {topic_id}")
