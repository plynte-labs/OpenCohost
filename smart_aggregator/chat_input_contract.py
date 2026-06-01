"""Universal Input Contract — Phase A (Shadow Mode).

ChatEventDetector: 12 universal event types via structural heuristics.
ChatContextPacket: clean, structured input for the LLM.

Shadow mode only: built and logged, NOT used for decisions.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Optional

# ── Feature flags ────────────────────────────────────────────────────────────
INPUT_CONTRACT_SHADOW_MODE = True
USE_INPUT_CONTRACT_PROMPT = False  # Phase B: set True to use ChatContextPacket as LLM prompt source


# ══════════════════════════════════════════════════════════════════════════════
# 12 Universal Chat Events (detected structurally, NOT by domain keywords)
# ══════════════════════════════════════════════════════════════════════════════

CHAT_EVENTS = (
    "direct_question",
    "viewer_request",
    "poll_or_vote_suggestion",
    "greeting_or_shoutout",
    "correction_or_clarification",
    "repeated_topic",
    "joke_or_meme",
    "hype_or_emotion",
    "complaint_or_confusion",
    "moderation_or_risk",
    "factual_update",
    "low_signal_noise",
)


# ══════════════════════════════════════════════════════════════════════════════
# ChatEventDetector — structural heuristics, zero domain keywords
# ══════════════════════════════════════════════════════════════════════════════

class ChatEventDetector:
    """Detect universal chat events via structural patterns.

    NO domain-specific keywords. Detection uses:
    - punctuation (? ¿ !)
    - caps ratio
    - message length
    - user diversity (repetition)
    - emoji density
    - structural markers
    """

    # Question markers (expandable, language-agnostic starters)
    _QUESTION_PATTERNS: list[re.Pattern] = [
        re.compile(r"[?¿]", re.IGNORECASE),
        re.compile(
            r"\b(?:qu[e\u00e9]|cu[a\u00e1]ndo|d[o\u00f3]nde|c[o\u00f3]mo|"
            r"qui[e\u00e9]n|por\s+qu[e\u00e9]|cu[a\u00e1]l|cu[a\u00e1]nto)",
            re.IGNORECASE,
        ),
        re.compile(
            r"\b(?:what|when|where|how|who|why|which|can\s+(?:you|i|we|someone))",
            re.IGNORECASE,
        ),
    ]

    # Poll/vote markers
    _POLL_PATTERNS: list[re.Pattern] = [
        re.compile(
            r"\b(?:encuesta|votaci[o\u00f3]n|voten|votar|like|opciones?|"
            r"elijan?|escojan?|decidan?|cu[a\u00e1]l prefieren)",
            re.IGNORECASE,
        ),
        re.compile(
            r"\b(?:poll|vote|choose|pick|options|which one)",
            re.IGNORECASE,
        ),
    ]

    # Greeting markers
    _GREETING_PATTERNS: list[re.Pattern] = [
        re.compile(
            r"\b(?:hola|holis|buenas?|saludos?|saludame|buen\s+d[i\u00ed]a|"
            r"buenas\s+(?:tardes|noches)|hey|qu[e\u00e9]\s+tal)",
            re.IGNORECASE,
        ),
        re.compile(
            r"\b(?:hi|hello|hey|greetings|good\s+(?:morning|evening|night))",
            re.IGNORECASE,
        ),
    ]

    # Correction markers
    _CORRECTION_PATTERNS: list[re.Pattern] = [
        re.compile(
            r"\b(?:se\s+(?:escribe|dice|llama|pronuncia)|"
            r"no\s+es\s+as[i\u00ed]|correcci[o\u00f3]n|"
            r"en\s+realidad\s+(?:es|se)|te\s+equivocas)",
            re.IGNORECASE,
        ),
    ]

    # Complaint markers
    _COMPLAINT_PATTERNS: list[re.Pattern] = [
        re.compile(
            r"\b(?:no\s+se\s+(?:escucha|ve|oye|entiende)|"
            r"(?:est[a\u00e1]|va)\s+(?:muy\s+)?(?:mal|lento|trabado|lag|ca[i\u00ed]do)|"
            r"no\s+funciona|fall[a\u00f3]|bug|error|se\s+congel[o\u00f3])",
            re.IGNORECASE,
        ),
    ]

    # Moderation/risk markers
    _MODERATION_PATTERNS: list[re.Pattern] = [
        re.compile(
            r"\b(?:m[a\u00e1]tate|suic[i\u00ed]date|muerte|asesin|"
            r"viola|abuso|doxx|doxeo|hack|estafa|phishing)",
            re.IGNORECASE,
        ),
    ]

    def __init__(self) -> None:
        pass

    def classify_message(self, text: str) -> str:
        """Classify a single message into one of 12 event types."""
        if not text or not text.strip():
            return "low_signal_noise"

        t = text.strip()

        # Very short messages → low signal
        if len(t) < 4:
            return "low_signal_noise"

        # Moderation/risk first (safety gate)
        for pat in self._MODERATION_PATTERNS:
            if pat.search(t):
                return "moderation_or_risk"

        # Question detection
        has_question = any(pat.search(t) for pat in self._QUESTION_PATTERNS)

        # Poll/vote detection
        has_poll = any(pat.search(t) for pat in self._POLL_PATTERNS)

        # Greeting detection
        has_greeting = any(pat.search(t) for pat in self._GREETING_PATTERNS)

        # Correction detection
        has_correction = any(pat.search(t) for pat in self._CORRECTION_PATTERNS)

        # Complaint detection
        has_complaint = any(pat.search(t) for pat in self._COMPLAINT_PATTERNS)

        # Structural signals
        caps_ratio = sum(1 for c in t if c.isupper()) / max(len(t), 1)
        exclamation_count = t.count("!") + t.count("\u00a1")
        non_alpha_ratio = sum(1 for c in t if not c.isalnum() and c != " ") / max(len(t), 1)
        length = len(t)

        # Hype: many exclamation marks or high caps ratio
        # NOTE: hype takes priority over question classification
        # when the message is clearly emotional (no actual ? mark)
        if exclamation_count >= 3 or caps_ratio > 0.5:
            return "hype_or_emotion"

        # Emoji-heavy → joke/meme or low signal
        if non_alpha_ratio > 0.4 and length < 30:
            return "low_signal_noise"

        # Order matters: more specific patterns first
        if has_question:
            if has_poll:
                return "poll_or_vote_suggestion"
            if has_complaint:
                return "complaint_or_confusion"
            return "direct_question"

        if has_poll:
            return "poll_or_vote_suggestion"

        if has_correction:
            return "correction_or_clarification"

        if has_complaint:
            return "complaint_or_confusion"

        if has_greeting and length < 60:
            return "greeting_or_shoutout"

        # Joke/meme: high non-alpha, moderate length
        if non_alpha_ratio > 0.25 and length < 50:
            return "joke_or_meme"

        # Long message with substance → factual_update or viewer_request
        if length > 60:
            return "factual_update"

        # Medium-length statements that don't match other categories
        # are likely conversational contributions
        if length > 25 and caps_ratio < 0.5:
            return "factual_update"

        return "low_signal_noise"

    def detect_events(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        """Analyze a batch of messages and return event summary.

        Returns dict with:
        - top_events: list of (event_type, count)
        - total_messages: int
        - unique_users: int
        - repeated_topic: str or None
        - primary_event: str (dominant event type)
        """
        if not messages:
            return {
                "top_events": [],
                "total_messages": 0,
                "unique_users": 0,
                "repeated_topic": None,
                "primary_event": "low_signal_noise",
            }

        # Classify all messages
        event_counts: Counter[str] = Counter()
        texts: list[str] = []
        users: set[str] = set()

        for msg in messages:
            text = msg.get("text", "")
            user = msg.get("user", msg.get("author", ""))
            event = self.classify_message(text)
            event_counts[event] += 1
            texts.append(text)
            if user:
                users.add(user)

        # Detect repeated topic: check for similar messages from different users
        repeated_topic = self._detect_repeated_topic(texts, users)

        # Primary event: most common, excluding low_signal_noise
        significant = {k: v for k, v in event_counts.items() if k != "low_signal_noise"}
        primary = max(significant, key=significant.get) if significant else "low_signal_noise"

        # Top events sorted by count
        top = event_counts.most_common(5)

        return {
            "top_events": [(e, c) for e, c in top if e != "low_signal_noise"][:3],
            "total_messages": len(messages),
            "unique_users": len(users),
            "repeated_topic": repeated_topic,
            "primary_event": primary,
        }

    def _detect_repeated_topic(self, texts: list[str], users: set[str]) -> Optional[str]:
        """If 3+ unique users post similar text, return the representative text."""
        if len(users) < 3:
            return None

        # Simple similarity: normalize and compare
        normalized = [_normalize_for_comparison(t) for t in texts]

        # Count occurrences of each normalized text
        counts: Counter[str] = Counter()
        reps: dict[str, str] = {}
        for orig, norm in zip(texts, normalized):
            if len(norm) > 10:  # meaningful minimum
                counts[norm] += 1
                reps[norm] = orig

        # Find texts with 3+ occurrences
        for norm, count in counts.most_common(3):
            if count >= 3:
                return reps[norm][:200]

        return None


def _normalize_for_comparison(text: str) -> str:
    """Normalize text for similarity comparison."""
    t = text.lower().strip()
    # Remove punctuation, extra spaces
    t = re.sub(r"[^\w\s]", "", t)
    t = re.sub(r"\s+", " ", t)
    return t[:80]


# ══════════════════════════════════════════════════════════════════════════════
# ChatContextPacket — clean structured input for the LLM
# ══════════════════════════════════════════════════════════════════════════════


@dataclass
class ChatContextPacket:
    """Clean, structured input packet for the LLM.

    This replaces the defective 'compact_chat' string that currently
    carries only 'No hay un tema dominante claro...'
    """

    # Event detection
    primary_event: str  # one of CHAT_EVENTS
    top_events: list[tuple[str, int]]  # (event_type, count)

    # Selected content
    selected_highlight: Optional[dict[str, Any]] = None  # {author, text, reason}
    supporting_comments: list[dict[str, str]] = field(default_factory=list)  # [{author, text}]

    # Topic clustering
    topic_clusters: list[dict[str, Any]] = field(default_factory=list)  # [{label, count, unique_users, example}]
    repeated_topic: Optional[str] = None

    # Activity
    total_messages: int = 0
    unique_users: int = 0

    # Decision
    response_goal: str = "stay_silent"  # summarize_trend, surface_to_streamer, etc.
    confidence: float = 0.0
    should_call_llm: bool = False

    # Noise
    ignored_noise_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "primary_event": self.primary_event,
            "top_events": self.top_events,
            "selected_highlight": self.selected_highlight,
            "supporting_comments": self.supporting_comments,
            "topic_clusters": self.topic_clusters,
            "repeated_topic": self.repeated_topic,
            "total_messages": self.total_messages,
            "unique_users": self.unique_users,
            "response_goal": self.response_goal,
            "confidence": self.confidence,
            "should_call_llm": self.should_call_llm,
            "ignored_noise_count": self.ignored_noise_count,
        }

    def to_prompt_context(self) -> str:
        """Convert to a human-readable context string for the LLM prompt."""
        parts = []

        if self.topic_clusters:
            cluster_lines = []
            for c in self.topic_clusters[:3]:
                label = c.get("label", "tema")
                count = c.get("count", 0)
                example = c.get("example", "")
                cluster_lines.append(f"- {label} ({count} mensajes): \"{example[:120]}\"")
            parts.append("Temas detectados en el chat:\n" + "\n".join(cluster_lines))

        if self.selected_highlight:
            h = self.selected_highlight
            parts.append(
                f"Comentario destacado: [{h.get('author', '?')}] {h.get('text', '')[:200]}"
            )

        if self.supporting_comments:
            lines = []
            for c in self.supporting_comments[:5]:
                lines.append(f"- [{c.get('author', '?')}] {c.get('text', '')[:150]}")
            parts.append("Comentarios relacionados:\n" + "\n".join(lines))

        if self.repeated_topic:
            parts.append(f"Tema repetido por varios usuarios: \"{self.repeated_topic[:200]}\"")

        if not parts:
            parts.append("No se detectaron temas claros en el chat reciente.")

        parts.append(
            f"Actividad: {self.total_messages} mensajes, {self.unique_users} usuarios. "
            f"Evento principal: {self.primary_event}."
        )

        return "\n".join(parts)


# ══════════════════════════════════════════════════════════════════════════════
# Packet builder: constructs ChatContextPacket from raw messages
# ══════════════════════════════════════════════════════════════════════════════


class ChatContextPacketBuilder:
    """Build a ChatContextPacket from filtered chat messages."""

    def __init__(self, detector: Optional[ChatEventDetector] = None) -> None:
        self.detector = detector or ChatEventDetector()

    def build(self, messages: list[dict[str, Any]]) -> ChatContextPacket:
        """Build a ChatContextPacket from a list of message dicts.

        Each message dict should have: text, user/author.
        """
        if not messages:
            return ChatContextPacket(
                primary_event="low_signal_noise",
                top_events=[],
            )

        # 1. Classify all messages
        classified: list[tuple[dict[str, Any], str]] = []
        low_signal_count = 0
        for msg in messages:
            text = msg.get("text", "")
            event = self.detector.classify_message(text)
            if event == "low_signal_noise" and len(text) < 20:
                low_signal_count += 1
            classified.append((msg, event))

        # 2. Event summary
        event_summary = self.detector.detect_events(messages)

        # 3. Select highlight: highest-value message
        highlight = self._select_highlight(classified)

        # 4. Supporting comments: 3-5 messages related to the primary event
        primary = event_summary["primary_event"]
        supporting = self._select_supporting(classified, primary, exclude=highlight)

        # 5. Topic clusters from repeated content
        topic_clusters = self._build_topic_clusters(messages, classified)

        # 6. Response decision
        goal, confidence, should_call = self._decide_response(event_summary, highlight)

        return ChatContextPacket(
            primary_event=primary,
            top_events=event_summary["top_events"],
            selected_highlight=highlight,
            supporting_comments=supporting,
            topic_clusters=topic_clusters,
            repeated_topic=event_summary.get("repeated_topic"),
            total_messages=event_summary["total_messages"],
            unique_users=event_summary["unique_users"],
            response_goal=goal,
            confidence=confidence,
            should_call_llm=should_call,
            ignored_noise_count=low_signal_count,
        )

    def _select_highlight(
        self, classified: list[tuple[dict[str, Any], str]]
    ) -> Optional[dict[str, Any]]:
        """Select the highest-value message as highlight."""
        best_score = -1.0
        best_msg: Optional[dict[str, Any]] = None
        best_event = ""

        for msg, event in classified:
            text = msg.get("text", "")
            if not text or len(text) < 10:
                continue

            # Score: questions > polls > corrections > long messages
            score = 0.0
            if event == "direct_question":
                score = 10.0
            elif event == "poll_or_vote_suggestion":
                score = 8.0
            elif event == "viewer_request":
                score = 7.0
            elif event == "correction_or_clarification":
                score = 6.0
            elif event == "factual_update":
                score = 5.0
            elif event in ("hype_or_emotion", "complaint_or_confusion"):
                score = 3.0
            elif event == "greeting_or_shoutout":
                score = 1.0

            # Bonus for length (substance)
            score += min(len(text) / 200, 2.0)

            # Bonus for containing ?
            if "?" in text or "\u00bf" in text:
                score += 2.0

            if score > best_score:
                best_score = score
                best_msg = msg
                best_event = event

        if best_msg:
            return {
                "author": best_msg.get("user", best_msg.get("author", "?")),
                "text": best_msg.get("text", ""),
                "reason": f"Highest-value message (event={best_event}, score={best_score:.1f})",
            }
        return None

    def _select_supporting(
        self,
        classified: list[tuple[dict[str, Any], str]],
        primary_event: str,
        exclude: Optional[dict[str, Any]] = None,
    ) -> list[dict[str, str]]:
        """Select 3-5 supporting comments related to the primary event."""
        exclude_text = (exclude or {}).get("text", "")
        supporting: list[dict[str, str]] = []

        for msg, event in classified:
            text = msg.get("text", "")
            if not text or text == exclude_text or len(text) < 15:
                continue
            # Prefer messages matching the primary event
            if event == primary_event or event not in ("low_signal_noise",):
                supporting.append({
                    "author": msg.get("user", msg.get("author", "?")),
                    "text": text[:200],
                })
            if len(supporting) >= 5:
                break

        return supporting

    def _build_topic_clusters(
        self,
        messages: list[dict[str, Any]],
        classified: list[tuple[dict[str, Any], str]],
    ) -> list[dict[str, Any]]:
        """Build topic clusters from message similarity."""
        clusters: list[dict[str, Any]] = []
        texts_by_user: dict[str, list[str]] = {}

        for msg, event in classified:
            if event == "low_signal_noise":
                continue
            user = msg.get("user", msg.get("author", ""))
            text = msg.get("text", "")
            if user and len(text) > 20:
                texts_by_user.setdefault(user, []).append(text)

        # Simple word-frequency clustering
        all_text = " ".join(msg.get("text", "") for msg, _ in classified)
        words = re.findall(r"\b[a-z\u00e1\u00e9\u00ed\u00f3\u00fa\u00f1]{4,}\b", all_text.lower())
        word_counts = Counter(words)

        # Top words suggest topic clusters
        cluster_words = [w for w, c in word_counts.most_common(10) if c >= 3 and w not in _STOP_WORDS]
        for word in cluster_words[:3]:
            # Find example messages containing this word
            examples = []
            users_with_word: set[str] = set()
            for msg in messages:
                text = msg.get("text", "")
                if word in text.lower():
                    user = msg.get("user", msg.get("author", ""))
                    if user:
                        users_with_word.add(user)
                    examples.append(text[:120])
            if len(users_with_word) >= 2:
                clusters.append({
                    "label": word.capitalize(),
                    "count": word_counts[word],
                    "unique_users": len(users_with_word),
                    "example": examples[0] if examples else "",
                    "all_examples": examples[:3],
                })

        return clusters

    def _decide_response(
        self,
        event_summary: dict[str, Any],
        highlight: Optional[dict[str, Any]],
    ) -> tuple[str, float, bool]:
        """Decide response goal, confidence, and whether to call LLM."""
        primary = event_summary["primary_event"]
        total = event_summary["total_messages"]
        users = event_summary["unique_users"]
        top_events = event_summary["top_events"]

        if primary == "low_signal_noise" and total < 3:
            return "stay_silent", 0.0, False

        if primary == "direct_question" and highlight:
            return "answer_direct_question", 0.7, True

        if primary == "poll_or_vote_suggestion":
            return "summarize_trend", 0.6, True

        if primary in ("hype_or_emotion", "complaint_or_confusion") and users >= 2:
            return "add_color", 0.5, True

        if event_summary.get("repeated_topic") and users >= 3:
            return "summarize_trend", 0.8, True

        if users >= 3 and total >= 5:
            return "summarize_trend", 0.5, True

        if highlight and total >= 2:
            return "surface_to_streamer", 0.4, True

        return "stay_silent", 0.2, False


# ── Stop words for topic clustering (Spanish + English) ─────────────────

_STOP_WORDS: set[str] = {
    "para", "como", "esto", "porque", "cuando", "donde", "sobre",
    "that", "this", "with", "from", "have", "been", "were", "they",
    "what", "when", "where", "which", "there", "their", "about",
}
