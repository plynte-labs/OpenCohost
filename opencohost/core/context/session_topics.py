"""In-session topic index (session-topic-folder-20261016).

A pure domain object: it groups the owner's exchanges of the CURRENT session by
topic so a topic that returns after its pairs left the history window can be
recalled in one or two compact lines. No I/O, no embedding worker, no settings:
callers inject vectors and thresholds.

Layout choice: this lives in ``core/context/`` next to the prompt assembler and
the context budget because it only decides what extra context a turn receives;
the embedding worker (memory subsystem) is wired in by the engine, never here.

Tunable thresholds (defaults are starting points, set from settings by the
engine): ``assign_threshold`` 0.55 (cosine to join a topic), ``retrieve_threshold``
0.60 (cosine to recall one), ``min_words`` 4, ``max_topics`` 24,
``max_fragments`` 6 per topic.
"""
from __future__ import annotations

import hashlib
import math
import threading
from collections import deque
from dataclasses import dataclass
from typing import Collection, Optional, Sequence

USER_SUMMARY_MAX_WORDS = 8
USER_SUMMARY_MAX_CHARS = 100


def compact_user_text(text: str, max_words: int = USER_SUMMARY_MAX_WORDS, max_chars: int = USER_SUMMARY_MAX_CHARS) -> str:
    """First *max_words* words of *text*, hard-capped at *max_chars*."""
    words = (text or "").split()
    return " ".join(words[:max_words])[:max_chars]


def fragment_key(user_text: str) -> str:
    """Stable identity of a user turn (whitespace-normalised), used to match
    fragments against the pairs still inside the history window."""
    flat = " ".join((user_text or "").split())
    return hashlib.sha1(flat.encode("utf-8")).hexdigest()[:16]


def _norm(vec: Sequence[float]) -> float:
    return math.sqrt(sum(x * x for x in vec))


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    na, nb = _norm(a), _norm(b)
    if na < 1e-9 or nb < 1e-9:
        return 0.0
    return sum(x * y for x, y in zip(a, b)) / (na * nb)


@dataclass(frozen=True)
class TopicFragment:
    key: str
    turn_seq: int
    user: str
    kira: str


@dataclass(frozen=True)
class TopicHit:
    topic_id: int
    score: float
    fragments: tuple[TopicFragment, ...]
    total_fragments: int


class _Topic:
    __slots__ = ("topic_id", "sum_vec", "unit", "count", "last_seq", "fragments")

    def __init__(self, topic_id: int, vec: Sequence[float], max_fragments: int) -> None:
        self.topic_id = topic_id
        self.sum_vec = list(vec)
        self.unit: list[float] = []
        self.refresh_unit()
        self.count = 0
        self.last_seq = 0
        self.fragments: deque[TopicFragment] = deque(maxlen=max_fragments)

    def refresh_unit(self) -> None:
        """Keep the running sum, cache its unit vector for comparisons."""
        n = _norm(self.sum_vec)
        self.unit = [x / n for x in self.sum_vec] if n >= 1e-9 else []

    def score(self, vec: Sequence[float], vec_norm: float) -> float:
        if not self.unit or len(self.unit) != len(vec) or vec_norm < 1e-9:
            return 0.0
        return sum(x * y for x, y in zip(self.unit, vec)) / vec_norm


class SessionTopicIndex:
    """Online nearest-centroid clustering of owner fragments (thread-safe)."""

    def __init__(
        self,
        *,
        assign_threshold: float = 0.55,
        retrieve_threshold: float = 0.60,
        min_words: int = 4,
        max_topics: int = 24,
        max_fragments: int = 6,
    ) -> None:
        self._assign = float(assign_threshold)
        self._retrieve = float(retrieve_threshold)
        self._min_words = int(min_words)
        self._max_topics = max(1, int(max_topics))
        self._max_fragments = max(1, int(max_fragments))
        self._topics: list[_Topic] = []
        self._next_id = 1
        self._lock = threading.Lock()
        self._generation = 0

    @property
    def generation(self) -> int:
        """Bumped by ``reset()``; lets an in-flight writer detect a stale session."""
        return self._generation

    def reset(self) -> None:
        with self._lock:
            self._topics = []
            self._next_id = 1
            self._generation += 1

    def topic_count(self) -> int:
        with self._lock:
            return len(self._topics)

    def fragment_count(self) -> int:
        with self._lock:
            return sum(len(t.fragments) for t in self._topics)

    def add_fragment(
        self,
        vec: Optional[Sequence[float]],
        user_text: str,
        kira_summary: str,
        turn_seq: int,
        generation: Optional[int] = None,
    ) -> Optional[int]:
        """Place one owner exchange; returns its topic id, None when skipped.
        A *generation* captured before a ``reset()`` is rejected inside the lock."""
        if not vec or _norm(vec) < 1e-9:
            return None
        if len((user_text or "").split()) < self._min_words:
            return None
        fragment = TopicFragment(
            key=fragment_key(user_text),
            turn_seq=int(turn_seq),
            user=compact_user_text(user_text),
            kira=kira_summary or "",
        )
        vec_norm = _norm(vec)
        with self._lock:
            if generation is not None and generation != self._generation:
                return None
            best, best_score = None, 0.0
            for topic in self._topics:
                score = topic.score(vec, vec_norm)
                if score > best_score:
                    best, best_score = topic, score
            if best is None or best_score < self._assign:
                best = _Topic(self._next_id, vec, self._max_fragments)
                self._next_id += 1
                self._topics.append(best)
                if len(self._topics) > self._max_topics:
                    oldest = min(self._topics[:-1], key=lambda t: t.last_seq)
                    self._topics.remove(oldest)
            else:
                best.sum_vec = [x + y for x, y in zip(best.sum_vec, vec)]
                best.refresh_unit()
            best.count += 1
            best.last_seq = fragment.turn_seq
            best.fragments.append(fragment)
            return best.topic_id

    def query(
        self,
        vec: Optional[Sequence[float]],
        *,
        exclude_keys: Collection[str] = (),
        max_return: int = 2,
    ) -> Optional[TopicHit]:
        """Best topic above the retrieve threshold that still has fragments
        outside *exclude_keys* (the pairs in the history window). Pure dot
        products over in-memory centroids: safe on the turn path."""
        if not vec or _norm(vec) < 1e-9:
            return None
        excluded = set(exclude_keys)
        vec_norm = _norm(vec)
        with self._lock:
            scored = sorted(
                ((t.score(vec, vec_norm), t) for t in self._topics),
                key=lambda p: p[0],
                reverse=True,
            )
            for score, topic in scored:
                if score < self._retrieve:
                    break
                out = [f for f in reversed(topic.fragments) if f.key not in excluded]
                if out:
                    return TopicHit(
                        topic_id=topic.topic_id,
                        score=score,
                        fragments=tuple(out[: max(1, int(max_return))]),
                        total_fragments=len(topic.fragments),
                    )
        return None

    def snapshot(self) -> tuple[dict, ...]:
        """Detached copy for the view writer and future distillation."""
        with self._lock:
            return tuple(
                {
                    "topic_id": t.topic_id,
                    "count": t.count,
                    "last_seq": t.last_seq,
                    "centroid": tuple(t.sum_vec),
                    "fragments": tuple(
                        {"key": f.key, "turn_seq": f.turn_seq, "user": f.user, "kira": f.kira}
                        for f in t.fragments
                    ),
                }
                for t in self._topics
            )


SESSION_TOPICS_OPEN = "<session_topics>"
SESSION_TOPICS_CLOSE = "</session_topics>"
_MIN_TAIL_CHARS = 20


def render_session_topics_block(
    hit: Optional[TopicHit],
    *,
    ctx_label: str,
    kira_label: str,
    max_lines: int = 2,
    max_chars: int = 240,
) -> str:
    """Compact block for the turn preamble: at most *max_lines* lines and
    *max_chars* characters of content (tags excluded). Empty string on a miss."""
    if hit is None or not hit.fragments:
        return ""
    lines: list[str] = []
    remaining = max(0, int(max_chars))
    for frag in hit.fragments[: max(1, int(max_lines))]:
        line = f"{ctx_label}: {frag.user} {kira_label}: {frag.kira}".strip()
        cost = len(line) + (1 if lines else 0)
        if cost > remaining:
            room = remaining - (1 if lines else 0)
            if room < _MIN_TAIL_CHARS:
                break
            line = line[: room - 1].rstrip() + "…"
            cost = len(line) + (1 if lines else 0)
        lines.append(line)
        remaining -= cost
    if not lines:
        return ""
    return "\n".join([SESSION_TOPICS_OPEN, *lines, SESSION_TOPICS_CLOSE])


def render_session_markdown(snapshot: Sequence[dict], *, generated_at: str) -> str:
    """Human-readable view of the session's topics (compact summaries only:
    no vectors, no keys, no raw turn text beyond the stored 8-word/100-char
    user summary and the one-sentence Kira summary)."""
    lines = ["# Session topics", "", f"Generated: {generated_at}", ""]
    if not snapshot:
        lines.append("No topics yet.")
        return "\n".join(lines) + "\n"
    for topic in sorted(snapshot, key=lambda t: t["last_seq"], reverse=True):
        lines.append(f"## Topic {topic['topic_id']} ({topic['count']} exchanges)")
        lines.append("")
        for frag in topic["fragments"]:
            lines.append(f"- #{frag['turn_seq']} {frag['user']} -> {frag['kira']}".rstrip())
        lines.append("")
    return "\n".join(lines)
