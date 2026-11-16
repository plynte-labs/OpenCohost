"""Merge Memory v5 episodic recall into the memorias injection lines (ADR-059).

Pure helpers, no I/O. The prompt gets ONE memorias block: lexical fact lines
first (unchanged, built by build_injection_lines / build_recency_lines), then
at most MEMORY_RECALL_MAX_EPISODES compact episode lines, all inside the same
MEMORIAS_MAX_INJECT_CHARS budget. Episodes are trimmed or skipped to fit;
fact lines are never cut to make room for them.

Telemetry (`memory_recall:`) is numbers and codes only — never query, fact or
episode text (RC-8, same rule as the memorias injection debug line).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone, tzinfo
from typing import Any, Optional, Sequence

from opencohost.config.settings import (
    MEMORIAS_MAX_INJECT_CHARS,
    MEMORIAS_PINNED_CLIP_CHARS,
)
from opencohost.core.memory.memoria_store import _clip_for_injection
from opencohost.i18n import active as _i18n_active

# One episode line never exceeds the pinned-fact clip, so two episodes cannot
# crowd out more than a pinned pair would.
EPISODE_LINE_MAX_CHARS = MEMORIAS_PINNED_CLIP_CHARS
# Below this, prefix + a clipped user side is noise, not context: skip.
EPISODE_LINE_MIN_CHARS = 80
_SIDE_MIN_CHARS = 12

FALLBACK_NONE = "none"
FALLBACK_TIMEOUT = "timeout"
FALLBACK_ERROR = "error"
FALLBACK_DISABLED = "disabled"
FALLBACK_NO_HIT = "no_hit"
FALLBACK_NO_ROOM = "no_room"

# Fact-ranking fallback codes (ADR-060), mirrored from fact_index.
FACT_FALLBACK_NONE = "none"
FACT_FALLBACK_DISABLED = "disabled"
_FACT_FALLBACK_CODES = frozenset({"none", "disabled", "cold", "timeout", "error", "skipped"})


@dataclass(frozen=True)
class RecallOutcome:
    """What one recall attempt contributes to the prompt, plus telemetry codes."""

    episodes: tuple[Any, ...]
    decision: str
    scope: str
    top_score: Optional[float]
    ms: int
    fallback: str
    # ADR-060: the turn's query vector for fact ranking (never logged) and
    # why fact ranking fell back to lexical when it is None.
    fact_query_vec: Optional[tuple[float, ...]] = None
    fact_fallback: str = FACT_FALLBACK_DISABLED
    # Stage telemetry (memory-grounding T1): ms + fixed device code, never text.
    embed_ms: Optional[int] = None
    worker_wait_ms: Optional[int] = None
    retrieval_ms: Optional[int] = None
    device: Optional[str] = None
    # The turn's raw query embedding (episodic stage), even when fact ranking is
    # cold or the episodic recall timed out (T2/T3). Never logged.
    query_vec: Optional[tuple[float, ...]] = field(default=None, repr=False)
    # memory-write-quality T3: telemetry only. 1 when the finished recall took
    # longer than the budget; it never changes whether the recall is used.
    over_budget: int = 0
    # Decision telemetry (T4): codes and counts only. None = not measured.
    intent: Optional[str] = None
    trigger: Optional[str] = None
    anchors: Optional[int] = None
    accepted_n: Optional[int] = None
    best_reason: Optional[str] = None

    @classmethod
    def disabled(cls) -> "RecallOutcome":
        return cls((), "DISABLED", "NONE", None, 0, FALLBACK_DISABLED)

    @classmethod
    def error(cls, ms: int) -> "RecallOutcome":
        return cls((), "RECALL_EXCEPTION", "NONE", None, ms, FALLBACK_ERROR)


def _stage_fields(decision: Any) -> dict:
    out: dict = {}
    for name in ("embed_ms", "worker_wait_ms", "retrieval_ms"):
        value = getattr(decision, name, None)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            out[name] = int(value)
    device = getattr(decision, "device", None)
    if isinstance(device, str):
        out["device"] = device
    for name in ("anchors", "accepted_n"):
        value = getattr(decision, name, None)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            out[name] = int(value)
    for name in ("intent", "trigger", "best_reason"):
        value = getattr(decision, name, None)
        if isinstance(value, str):
            out[name] = value
    vec = getattr(decision, "query_vec", None)
    if isinstance(vec, (tuple, list)) and vec:
        out["query_vec"] = tuple(float(x) for x in vec)
    return out


def resolve_recall_outcome(decision: Any, ms: int, budget_ms: int, max_episodes: int) -> RecallOutcome:
    """Map a subsystem RecallDecision + wall time to a RecallOutcome.

    WORKER_UNAVAILABLE is what the coordinator returns when the worker IPC
    call yields nothing — with the budget-capped timeout that is a timeout.
    The budget caps that IPC wait; it is not a post-hoc discard. A recall that
    finished and hit is used whatever its wall time (the latency is already
    paid), and ``over_budget`` records the overrun as telemetry only.
    """
    reason = str(getattr(decision, "reason", "") or "NO_RECALL")
    scope = str(getattr(decision, "scope", "") or "NONE")
    top = getattr(decision, "top_score", None)
    episodes = tuple(getattr(decision, "episodes", ()) or ())
    stage = _stage_fields(decision)
    stage["over_budget"] = 1 if ms > budget_ms else 0

    if reason == "DISABLED":
        return RecallOutcome((), reason, scope, top, ms, FALLBACK_DISABLED, **stage)
    if reason == "RECALL_EXCEPTION":
        return RecallOutcome((), reason, scope, top, ms, FALLBACK_ERROR, **stage)
    if reason == "WORKER_UNAVAILABLE":
        return RecallOutcome((), reason, scope, top, ms, FALLBACK_TIMEOUT, **stage)
    if not getattr(decision, "hit", False) or not episodes:
        return RecallOutcome((), reason, scope, top, ms, FALLBACK_NO_HIT, **stage)
    return RecallOutcome(episodes[: max(0, max_episodes)], reason, scope, top, ms, FALLBACK_NONE, **stage)


def _code(value: Any) -> str:
    """Codes only in telemetry: anything outside [A-Z_] becomes "_"."""
    return re.sub(r"[^A-Z_]", "_", str(value or "NONE").upper())[:48] or "NONE"


def _family_code(value: Any) -> str:
    """Fixed-vocabulary family code: lowercase [a-z_] only, "-" when absent."""
    code = re.sub(r"[^a-z_]", "_", str(value or "").lower())[:24]
    return code if value and code.strip("_") else "-"


def _best_reason_code(value: Any) -> str:
    """Gate-outcome code: [A-Z_] only, "-" when nothing was scored."""
    if not value or str(value) == "-":
        return "-"
    return _code(value)


def _device_code(value: Any) -> str:
    """Fixed-vocabulary device code: lowercase [a-z0-9_] only, never free text."""
    code = re.sub(r"[^a-z0-9_]", "_", str(value or "unknown").lower())[:12]
    return code if code in {"cpu", "gpu", "cuda", "dummy"} else "unknown"


def _collapse(text: Any) -> str:
    return " ".join(str(text or "").split())


def _best_exchange(episode: Any) -> Any:
    exchanges = list(getattr(episode, "exchanges", None) or [])
    if not exchanges:
        return None
    key = getattr(episode, "best_exchange_key", "") or ""
    if key:
        for ex in exchanges:
            if f"{ex.user_event_id}:{ex.assistant_event_id}" == key:
                return ex
    return exchanges[0]


def _episode_date_label(
    started_at: Any, now: Optional[datetime] = None, tz: Optional[tzinfo] = None
) -> str:
    """``YYYY-MM-DD, <relative age>`` in LOCAL time, or the raw date prefix.

    Local like the prompt's "now" line (``datetime.now()``): an evening session
    stored as the next UTC day is labelled with the day the owner lived it, and
    the age counts local calendar days so the model does no date arithmetic.
    *now* and *tz* are injectable for tests. Unparseable input degrades to the
    first 10 characters without an age.
    """
    raw = str(started_at or "")
    try:
        started = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        started = started.astimezone(tz)  # tz=None: the machine's local zone
        current = (now or datetime.now().astimezone()).astimezone(tz)
    except (ValueError, OverflowError, OSError):
        return raw[:10]
    days = (current.date() - started.date()).days
    labels = _i18n_active.episode_age_labels()
    if days <= 0:
        age = labels["today"]
    elif days == 1:
        age = labels["yesterday"]
    elif days < 14:
        age = labels["days"].format(n=days)
    elif days < 60:
        age = labels["weeks"].format(n=days // 7)
    else:
        age = labels["months"].format(n=days // 30)
    return f"{started.date().isoformat()}, {age}"


def format_episode_line(
    episode: Any,
    limit: int,
    *,
    now: Optional[datetime] = None,
    tz: Optional[tzinfo] = None,
) -> str:
    """One compact line for *episode*, at most *limit* chars, or "".

    Shape: ``[past chat YYYY-MM-DD] <user words>``. The date marks it as past
    context. USER SIDE ONLY (S2, release-050-stabilization): Kira's reply is
    generated, not observed, so a fabrication in it must never come back as
    memory. The assistant text is deliberately never read here.
    """
    ex = _best_exchange(episode)
    if ex is None:
        return ""
    label = _episode_date_label(getattr(episode, "started_at", ""), now, tz)
    prefix = f"[past chat {label}] "
    room = limit - len(prefix)
    if room < _SIDE_MIN_CHARS:
        return ""
    user = _collapse(ex.user_text)
    if not user:
        return ""
    return prefix + _clip_for_injection(user, min(len(user), room))


def append_episode_lines(
    lines: list[str],
    episodes: Sequence[Any],
    *,
    max_chars: int = MEMORIAS_MAX_INJECT_CHARS,
    max_episodes: int = 2,
    now: Optional[datetime] = None,
    tz: Optional[tzinfo] = None,
) -> int:
    """Append up to *max_episodes* episode lines AFTER the existing fact
    *lines* (mutated in place) without exceeding *max_chars* for the whole
    "\\n"-joined list. Existing lines are never touched. Returns how many
    episode lines were appended.
    """
    used = len("\n".join(lines))
    appended = 0
    for episode in list(episodes)[: max(0, max_episodes)]:
        sep = 1 if lines else 0
        limit = min(max_chars - used - sep, EPISODE_LINE_MAX_CHARS)
        if limit < EPISODE_LINE_MIN_CHARS:
            break
        line = format_episode_line(episode, limit, now=now, tz=tz)
        if not line:
            continue
        lines.append(line)
        used += sep + len(line)
        appended += 1
    return appended


def format_recall_telemetry(
    outcome: RecallOutcome,
    lexical: int,
    episodes: int,
    semantic: int = 0,
    fact_fallback: Optional[str] = None,
    inject_skip: Optional[str] = None,
    note: Optional[str] = None,
) -> str:
    """The single per-attempt INFO line. Numbers and codes only.

    *lexical* counts fact lines (pinned + automatic top-k); *semantic* is how
    many of those the fact ranker selected on a semantic score (ADR-060);
    *fact_fallback* says why fact ranking stayed lexical (``none`` = it did not).
    *inject_skip* (T3) is a closed reason code appended as
    ``inject=skipped reason=<code>`` when the memorias block was withheld on
    purpose (``lexical_only_weak`` / ``correction_turn``); absent otherwise.
    """
    fallback = outcome.fallback
    if fallback == FALLBACK_NONE and episodes == 0:
        fallback = FALLBACK_NO_ROOM
    top = "none" if outcome.top_score is None else f"{float(outcome.top_score):.3f}"
    fact_code = fact_fallback if fact_fallback is not None else outcome.fact_fallback
    if fact_code not in _FACT_FALLBACK_CODES:
        fact_code = "error"
    line = (
        f"memory_recall: lexical={int(lexical)} semantic={int(semantic)} episodes={int(episodes)} "
        f"decision={_code(outcome.decision)} scope={_code(outcome.scope)} top_score={top} "
        f"ms={int(outcome.ms)} fallback={fallback} fact_fallback={fact_code} "
        f"over_budget={1 if outcome.over_budget else 0}"
    )
    if outcome.embed_ms is not None:
        line += (
            f" embed_ms={int(outcome.embed_ms)}"
            f" worker_wait_ms={int(outcome.worker_wait_ms or 0)}"
            f" retrieval_ms={int(outcome.retrieval_ms or 0)}"
            f" device={_device_code(outcome.device)}"
        )
    if outcome.intent is not None:
        line += (
            f" intent={_code(outcome.intent)}"
            f" trigger={_family_code(outcome.trigger)}"
            f" anchors={int(outcome.anchors or 0)}"
            f" accepted_n={int(outcome.accepted_n or 0)}"
            f" best_reason={_best_reason_code(outcome.best_reason)}"
        )
    if inject_skip:
        reason = re.sub(r"[^a-z_]", "_", str(inject_skip).lower())[:32]
        line += f" inject=skipped reason={reason}"
    if note:
        line += " note=" + re.sub(r"[^a-z_]", "_", str(note).lower())[:24]
    return line
