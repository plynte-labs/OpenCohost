"""Owner reflection memory (ADR-065): the pure domain.

While the engine is idle it distils the owner's recent exchanges into a few durable
Q&A facts. This module holds only the parts that need no I/O and no engine: the
owner-only eligibility rule, the bounded RAM ring, the prompt builder, the reply
parser, the provenance mapping, the job cancellation handle and the log line.

Privacy is structural here, not conventional:

* ``OwnerExchange`` is only ever built by ``owner_exchange_from_turn``, which returns
  ``None`` for any source other than ``direct`` / ``ptt``, for a private turn and for
  the agenda sentinel.
* ``ReflectionRing`` and ``build_distillation_prompt`` accept ``OwnerExchange`` and
  nothing else, so a viewer exchange cannot reach the prompt through them.
* Only the owner's side of an exchange is placed in the prompt. Kira's reply stays in
  the ring for the repetition check and is never sent to the model.
* Nothing here logs, and ``format_log_line`` emits counters and fixed reason codes only.
"""
from __future__ import annotations

import json
import re
import secrets
import threading
from collections import deque
from dataclasses import dataclass
from typing import Optional, Sequence

from opencohost.core.context.repetition_guard import DEFAULT_CONFIG, _char_shingles, _jaccard, _norm
from opencohost.core.memory.memoria_store import _strip_accents, is_recall_question

ELIGIBLE_SOURCES = frozenset({"direct", "ptt"})
TEXT_CAP = 300
RING_MAX = 16
BATCH_MAX = 8
KIRA_LINES_WINDOW = 6
AGENDA_SENTINEL = "[agenda segura"
QUESTION_MAX_CHARS = 80
ANSWER_MAX_CHARS = 220
REPLY_MAX_CHARS = 8000

_REASON_RE = re.compile(r"[a-z_]+")
_OUTCOMES = frozenset({"ok", "skipped", "cancelled", "failed"})
_COUNTER_FIELDS = (
    "considered", "eligible", "distilled", "repeated", "schema_rejected", "ungrounded",
    "unkeyed", "written", "elapsed_ms",
)


def _clean(text, cap: int) -> str:
    return " ".join(str(text or "").split())[:cap]


# memory-grounding T8: an explicit "remember that ..." request states a fact, so it
# stays eligible even though recall-question detection matches its verb.
_REMEMBER_REQUEST_RE = re.compile(
    r"\b(quiero\s+que\s+(record|recuerd)\w*|recuerda\s+que|recuerdame|acuerdate\s+(de\s+)?que|"
    r"remember\s+that|i\s+want\s+you\s+to\s+remember)\b"
)


def _is_recall_exchange(user_text: str) -> bool:
    """A recall exchange asks about earlier conversation; Kira's answer to it comes from
    memory (possibly absent), so it is excluded from distillation input."""
    if not is_recall_question(user_text):
        return False
    return not _REMEMBER_REQUEST_RE.search(_strip_accents(str(user_text).lower()))


# memory-write-quality T1: a sentence is split on . ! ? and the Spanish openers; a
# question ends with "?" or starts with "¿"; a request opens (after a vocative such as
# "Kira," or "oye") with an ask to search, look up, tell, show or explain. Matched on
# lower-cased, accent-stripped text.
_SENTENCE_RE = re.compile(r"[¿¡]*[^.!?¿¡]+[.!?]*")
_LEAD_RE = re.compile(r"^(?:(?:oye|hey|kira|ok|okay|bueno|y|and|so|please|por\s+favor)\b[\s,]*)+")
_REQUEST_RE = re.compile(
    r"^(?:puedes|podrias|pudieras|busca(?:r|me|lo|la|los|las|nos)?|googlea(?:r|me|lo)?|"
    r"dime|dinos|explica(?:me|nos|lo)?|muestra(?:me|nos)?|ensena(?:me|nos)?|"
    r"search|look\s+up|can\s+you|could\s+you|would\s+you|will\s+you|tell\s+me|show\s+me|"
    r"explain|find\s+out|google(?=\s+(?:it|that|this|for|the|a|an|what|who|where|when|how|why)\b))\b"
)


def _sentence_kind(sentence: str) -> str:
    """``"ask"`` (question or request), ``"fragment"`` (one word, such as an echoed place
    name) or ``"statement"``."""
    stripped = sentence.strip()
    body = _strip_accents(stripped.lower()).strip("¿¡.!? ")
    if _REMEMBER_REQUEST_RE.search(body):
        return "statement"  # "remember that ..." carries the fact it asks to keep
    if stripped.endswith("?") or stripped.startswith("¿"):
        return "ask"
    if _REQUEST_RE.match(_LEAD_RE.sub("", body)):
        return "ask"
    return "fragment" if len(re.findall(r"\w+", body)) < 2 else "statement"


def is_question_or_request(user_text: str) -> bool:
    """True when the text asks something and states nothing: at least one question or
    request and no declarative sentence. A mixed exchange is not one."""
    kinds = [
        _sentence_kind(s) for s in _SENTENCE_RE.findall(str(user_text or ""))
        if re.search(r"\w", s)
    ]
    return "ask" in kinds and "statement" not in kinds


@dataclass(frozen=True)
class OwnerExchange:
    seq: int
    user_text: str
    kira_text: str
    # Both are excluded from distillation (pending_batch / counters) and still kept in
    # the ring for the repetition check.
    recall: bool = False
    inquiry: bool = False  # every sentence is a question or a request

    @property
    def excluded(self) -> bool:
        return self.recall or self.inquiry


@dataclass(frozen=True)
class FactCandidate:
    question: str
    answer: str
    turns: tuple  # 1-based positions inside the batch, already validated


def owner_exchange_from_turn(source, private, seq: int, user_text, kira_text) -> Optional[OwnerExchange]:
    """The only constructor of ``OwnerExchange``. Fail-closed: anything that is not a
    non-private ``direct`` / ``ptt`` owner turn returns ``None``."""
    if source not in ELIGIBLE_SOURCES or private is not False:
        return None
    user = _clean(user_text, TEXT_CAP)
    if not user or user.startswith(AGENDA_SENTINEL):
        return None
    return OwnerExchange(
        seq=int(seq), user_text=user, kira_text=_clean(kira_text, TEXT_CAP),
        recall=_is_recall_exchange(user), inquiry=is_question_or_request(user),
    )


class ReflectionRing:
    """Bounded, RAM-only ring of owner exchanges. Never persisted, never logged."""

    def __init__(self, maxlen: int = RING_MAX, session_token: Optional[str] = None) -> None:
        self._lock = threading.Lock()
        self._ring: deque = deque(maxlen=maxlen)
        self._seq = 0
        self.session_token = session_token or secrets.token_hex(4)
        self.last_distilled_seq = 0
        self.owner_turns_since_reflection = 0

    def offer(self, source, private, user_text, kira_text) -> Optional[OwnerExchange]:
        """Build an exchange through the owner gate and keep it. ``None`` = not eligible."""
        with self._lock:
            exchange = owner_exchange_from_turn(source, private, self._seq + 1, user_text, kira_text)
            if exchange is None:
                return None
            self._seq += 1
            self._ring.append(exchange)
            if not exchange.excluded:
                self.owner_turns_since_reflection += 1
            return exchange

    def add(self, exchange: OwnerExchange) -> None:
        if not isinstance(exchange, OwnerExchange):
            raise TypeError("ReflectionRing accepts OwnerExchange only")
        with self._lock:
            self._seq = max(self._seq, exchange.seq)
            self._ring.append(exchange)
            if not exchange.excluded:
                self.owner_turns_since_reflection += 1

    def snapshot(self) -> tuple:
        with self._lock:
            return tuple(self._ring)

    def due(self, every_n: int) -> bool:
        with self._lock:
            return self.owner_turns_since_reflection >= max(1, int(every_n))

    def pending_count(self) -> int:
        with self._lock:
            return sum(1 for e in self._ring if e.seq > self.last_distilled_seq and not e.excluded)

    def pending_batch(self, max_n: int = BATCH_MAX) -> tuple:
        """``(batch, evicted)``: up to *max_n* undistilled exchanges, oldest first, and how
        many undistilled ones the ring already dropped."""
        with self._lock:
            pending = [e for e in self._ring if e.seq > self.last_distilled_seq and not e.excluded]
            if not pending:
                return (), 0
            # A recall or inquiry exchange still in the ring is excluded on purpose, not evicted.
            skipped = sum(
                1 for e in self._ring
                if e.excluded and self.last_distilled_seq < e.seq < pending[0].seq
            )
            evicted = max(0, pending[0].seq - self.last_distilled_seq - 1 - skipped)
            return tuple(pending[:max_n]), evicted

    def mark_distilled(self, upto_seq: int, consumed: int = 0) -> None:
        """A run completed: advance the cursor. The counter becomes the number of
        undistilled exchanges the ring still holds, so exchanges evicted before they were
        ever distilled can never leave it stuck above the threshold. *consumed* is kept
        for call-site compatibility and ignored."""
        with self._lock:
            self.last_distilled_seq = max(self.last_distilled_seq, int(upto_seq))
            self._reconcile_locked()

    def _reconcile_locked(self) -> None:
        self.owner_turns_since_reflection = sum(
            1 for e in self._ring if e.seq > self.last_distilled_seq and not e.excluded
        )

    def reconcile(self) -> None:
        """Make the counter match what the ring actually holds."""
        with self._lock:
            self._reconcile_locked()

    def recent_kira_lines(self, n: int = KIRA_LINES_WINDOW) -> tuple:
        with self._lock:
            lines = [e.kira_text for e in list(self._ring)[-n:] if e.kira_text]
        return tuple(lines)

    def provenance_id(self, seq: int) -> str:
        return f"{self.session_token}:{int(seq)}"

    def reset(self) -> None:
        with self._lock:
            self._ring.clear()
            self._seq = 0
            self.last_distilled_seq = 0
            self.owner_turns_since_reflection = 0
            self.session_token = secrets.token_hex(4)


class JobToken:
    __slots__ = ("gen", "cancel", "done")

    def __init__(self, gen: int) -> None:
        self.gen = gen
        self.cancel = threading.Event()
        self.done = threading.Event()


class JobControl:
    """Cancellation handle for the one in-flight distillation job.

    ``cancel`` never waits for the job: it flags the token, bumps the generation (so a
    late result is discarded) and closes the in-flight HTTP response from the CALLING
    thread, which is the only way to release the single Ollama runner.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._gen = 0
        self._active: Optional[JobToken] = None
        self._handle = None

    @property
    def active(self) -> Optional[JobToken]:
        with self._lock:
            return self._active

    def begin(self) -> Optional[JobToken]:
        with self._lock:
            if self._active is not None:
                return None
            self._gen += 1
            self._active = JobToken(self._gen)
            return self._active

    def attach(self, token: JobToken, handle) -> bool:
        """Register the response to close on cancel. False (and the handle is closed) when
        the token was already cancelled."""
        with self._lock:
            live = token is self._active and not token.cancel.is_set()
            if live:
                self._handle = handle
        if not live:
            _safe_close(handle)
        return live

    def cancel(self) -> bool:
        with self._lock:
            token = self._active
            if token is None:
                return False
            token.cancel.set()
            self._gen += 1
            handle, self._handle = self._handle, None
        _safe_close(handle)
        return True

    def is_current(self, token: JobToken) -> bool:
        with self._lock:
            return token is self._active and token.gen == self._gen and not token.cancel.is_set()

    def finish(self, token: JobToken) -> None:
        with self._lock:
            if token is self._active:
                self._active = None
                self._handle = None
        token.done.set()


def _safe_close(handle) -> None:
    if handle is None:
        return
    try:
        handle.close()
    except Exception:
        pass


_PROMPT_HEAD = (
    "You extract durable facts that the OWNER (the streamer) stated about themselves, their "
    "setup, their preferences or their world.\n"
    "Below are numbered messages written by the owner. Use ONLY these messages.\n"
    "Return ONLY JSON of the form "
    '{"facts":[{"q":"<short question the fact answers>","a":"<one complete, self-contained '
    'sentence stating the fact>","turns":[<message numbers>]}]}.\n'
    "Rules:\n"
    "- At most {max_facts} facts. Return {\"facts\":[]} when nothing durable was stated.\n"
    "- Only explicit statements by the owner. Skip questions, jokes, requests and chit-chat.\n"
    "- Never turn a question or a request (for example a request to search the web or look "
    "something up) into a fact.\n"
    "- Asking about a place, person or topic says nothing about the owner's own life.\n"
    "- Never infer where the owner lives or works, or what they own, unless they say it "
    "explicitly.\n"
    "- Each \"a\" must make sense on its own, without the question or the conversation.\n"
    "- \"turns\" lists the message numbers the fact came from.\n"
    "OWNER MESSAGES:\n"
)


def build_distillation_prompt(batch: Sequence[OwnerExchange], max_facts: int = 3) -> str:
    """Prompt for one run. Accepts ``OwnerExchange`` only and uses the owner side only."""
    lines = []
    for position, exchange in enumerate(batch, start=1):
        if not isinstance(exchange, OwnerExchange):
            raise TypeError("build_distillation_prompt accepts OwnerExchange only")
        lines.append(f"{position}. {exchange.user_text}")
    return _PROMPT_HEAD.replace("{max_facts}", str(int(max_facts))) + "\n".join(lines)


def _valid_turns(raw, batch_size: int) -> Optional[tuple]:
    if not isinstance(raw, list) or not raw:
        return None
    turns = []
    for value in raw:
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= batch_size:
            return None
        turns.append(value)
    return tuple(sorted(set(turns)))


def parse_distillation(text: str, batch_size: int, max_facts: int = 3) -> tuple:
    """``(candidates, schema_rejected)``. Anything malformed, or pointing outside the batch,
    is counted and dropped; nothing from the reply is ever raised or logged."""
    try:
        payload = json.loads((text or "").strip())
        items = payload["facts"]
        if not isinstance(items, list):
            raise ValueError("facts is not a list")
    except (ValueError, KeyError, TypeError):
        return [], 1
    candidates: list = []
    rejected = 0
    for item in items:
        if len(candidates) >= max_facts:
            break
        if not isinstance(item, dict):
            rejected += 1
            continue
        question, answer = item.get("q"), item.get("a")
        turns = _valid_turns(item.get("turns"), batch_size)
        if not isinstance(question, str) or not isinstance(answer, str) or turns is None:
            rejected += 1
            continue
        question = _clean(question, QUESTION_MAX_CHARS)
        answer = _clean(answer, ANSWER_MAX_CHARS)
        if not question or not answer:
            rejected += 1
            continue
        candidates.append(FactCandidate(question=question, answer=answer, turns=turns))
    return candidates, rejected


def drop_ungrounded(candidates: Sequence[FactCandidate], batch: Sequence[OwnerExchange]) -> tuple:
    """``(kept, dropped)``: a fact whose cited turns are ALL questions or requests states
    nothing the owner said about themselves, so it is dropped. Turns are already
    validated against the batch by ``parse_distillation``."""
    kept = [
        c for c in candidates
        if not all(is_question_or_request(batch[t - 1].user_text) for t in c.turns)
    ]
    return kept, len(candidates) - len(kept)


def provenance_for(candidate: FactCandidate, batch: Sequence[OwnerExchange], session_token: str) -> str:
    """JSON list of opaque owner-turn ids (``"<8 hex>:<seq>"``). Built in code from the
    validated batch positions; the model never supplies it."""
    return json.dumps([f"{session_token}:{batch[t - 1].seq}" for t in candidate.turns])


def fact_key_text(candidate: FactCandidate) -> str:
    """Text the stable key is derived from: the ANSWER first, because the key keeps only the
    first few significant tokens and a long question would otherwise push the slot value
    (the part a correction changes) out of it."""
    return f"{candidate.answer} {candidate.question}"


def _shingles(text: str) -> set:
    return _char_shingles(_norm(text), DEFAULT_CONFIG.shingle_k)


def similarity(a: str, b: str) -> float:
    """Char-4-shingle Jaccard of two texts, after the repetition guard's normalisation
    (lower case, accents stripped, edge punctuation trimmed)."""
    return _jaccard(_shingles(a), _shingles(b))


def repeats_existing(candidate: FactCandidate, existing_texts: Sequence[str], threshold: float) -> bool:
    """True when the ANSWER is a near-duplicate of any existing fact CONTENT.

    Only the answer is compared: promotion rewrites titles to three tokens, and a long
    shared question would otherwise inflate the similarity of two different answers. The
    threshold is chosen so a corrected fact (one changed slot, such as another name)
    stays below it and reaches the judge, where supersession can act on it."""
    mine = _shingles(candidate.answer)
    return any(_jaccard(mine, _shingles(text)) >= threshold for text in existing_texts)


def repeats_spoken(candidate: FactCandidate, kira_lines: Sequence[str], threshold: float) -> bool:
    """True when the ANSWER near-duplicates one of Kira's recent lines. The lines are only
    compared in code; they are never sent to the model."""
    mine = _shingles(candidate.answer)
    return any(_jaccard(mine, _shingles(line)) >= threshold for line in kira_lines)


def format_log_line(outcome: str, reason: str, **counters: int) -> str:
    """The one per-run line: fixed fields, counters and a fixed reason code only."""
    outcome = outcome if outcome in _OUTCOMES else "failed"
    reason = reason if _REASON_RE.fullmatch(reason or "") else "unknown"
    fields = " ".join(f"{name}={max(0, int(counters.get(name, 0)))}" for name in _COUNTER_FIELDS)
    return f"[OWNER_REFLECTION] outcome={outcome} reason={reason} {fields}"
