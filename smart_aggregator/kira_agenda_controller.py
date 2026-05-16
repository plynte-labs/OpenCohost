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
from typing import Iterable, Optional
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
        self.failure_count = 0
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
        # Cooldown / suggestion tracking (used by TopicSuggester integration)
        self._last_suggestion_time: float = 0.0
        self._session_suggestion_count: int = 0
        self._suggestion_cooldown_seconds: float = 120.0
        self._suggestion_session_cap: int = 5
        self.profile: dict[str, str] = {
            "style": "Soná como co-host natural de stream: cercana, con humor seco, sin anunciar estructura ni despedirte entre ideas.",
        }

    def set_profile(self, profile: dict[str, str]) -> None:
        style = self.sanitize_topic_text((profile or {}).get("style", ""), field="profile_style", required=False)
        self.profile = {"style": style or self.profile.get("style", "")}

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
        self.failure_count = 0

    def resume(self) -> None:
        if self.state == AgendaState.PAUSED_NEEDS_OPERATOR:
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
        if self.state in {AgendaState.OFF, AgendaState.PAUSED_NEEDS_OPERATOR}:
            return AgendaAction.none()
        if motor_busy or kira_speaking or self.state in {AgendaState.GENERATING, AgendaState.SPEAKING}:
            return AgendaAction.none()
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

    def start_prefetched_action(self, action: AgendaAction) -> None:
        """Adopt a previously previewed agenda action right before cached TTS."""
        if action.kind != "enqueue" or not self.active_topic:
            return
        self._pending_turns_spoken = max(1, action.turns)
        self._pending_action_source = action.source
        if action.source == "kira-agenda-stop":
            self.active_topic.status = TopicStatus.CLOSING
            self.state = AgendaState.TOPIC_CLOSING
        else:
            self.state = AgendaState.GENERATING

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

    def register_failure(self) -> None:
        self.failure_count += 1
        if self.active_topic and self.failure_count >= 2:
            self.active_topic.turns_spoken = self.max_turns_per_topic
            self.state = AgendaState.WAITING_SIGNAL
            return
        self.state = AgendaState.PAUSED_NEEDS_OPERATOR if self.failure_count >= self.max_failures else AgendaState.REGENERATING_SAFE

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
                self.register_failure()
            return False
        if (
            self.contains_internal_leak(clean)
            or self.is_repetition(clean)
            or self.has_looping_lines(output)
            or self.repeats_recent_line(clean)
            or self.is_too_similar_to_recent(clean)
            or self.reuses_looping_opening(clean)
            or self.claims_inner_life(clean)
        ):
            if mutate:
                self.register_failure()
            return False
        if mutate:
            self.failure_count = 0
            self.record_accepted_output(clean)
        return True

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
            if current_sentences.intersection({s for s in self._sentences(recent) if len(s) > 24}):
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

    def _topic_action(self, instruction: str) -> AgendaAction:
        self._pending_turns_spoken = self._next_block_size()
        self._pending_action_source = "kira-agenda"
        prompt = self._build_prompt(instruction=instruction)
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

    def _build_prompt(self, *, instruction: str, compact_chat: str = "", ptt_text: str = "") -> str:
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
        return (
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
            f"ÚLTIMAS LÍNEAS DE KIRA; NO REPETIR NI PARAFRASEAR:\n{last}"
        )

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
