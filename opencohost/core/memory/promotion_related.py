"""
opencohost/core/memory/promotion_related.py

F2 (memory-paraphrase-contradiction-20260928): pure helpers that let the
promotion judge see the related EXISTING durable facts and decide, in the SAME
call, whether a candidate is new, a duplicate or an update of one of them.

No I/O, no engine, no worker: retrieval scores arrive as plain floats, the
prompt is built from plain rows, and the validator is deterministic. Never
logs or returns raw text beyond what the caller passes in.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from opencohost.core.memory.memoria_store import _SCORING_STOPWORDS, _significant_tokens

# MiniLM cosine band inside which an existing fact is "related" to a draft
# (calibration, F1): 0.35 broadens cross-session contradiction recall across
# paraphrases and bilingual updates while staying bounded per draft.
RELATED_BAND_LOW = 0.35
RELATED_BAND_HIGH = 0.97
# Up to this many existing facts per draft (a batch is at most 4 drafts).
RELATED_PER_DRAFT = 3
# Dedicated character budget for EXISTING FACTS lines so full-size draft batches
# (~5000 chars) never starve contradiction context.
EXISTING_BUDGET_CHARS = 2500
# Each EXISTING line is clipped to this many characters (text only).
EXISTING_CLIP_CHARS = 220
# Lexical fallback: 1 shared token when no vector exists; 2 when a known vector
# scored below RELATED_BAND_LOW.
LEXICAL_MIN_SHARED_TOKENS = 1
LEXICAL_BELOW_BAND_MIN_SHARED = 2

_DURABLE_TARGET_STATUSES = frozenset({"promoted", "imported", "curated"})


@dataclass(frozen=True)
class ExistingFact:
    """Snapshot of one existing durable row, as retrieved for a sweep."""

    id: str
    profile_id: str
    status: str
    revision: int
    content: str
    inactive: int = 0


def rank_related(
    draft_text: str,
    facts: list[ExistingFact],
    semantic_scores: dict[str, float] | None,
    *,
    limit: int = RELATED_PER_DRAFT,
) -> list[ExistingFact]:
    """Up to *limit* facts related to *draft_text*, best first.

    A fact with an entry in *semantic_scores* (fact id -> cosine) in
    ``[RELATED_BAND_LOW, RELATED_BAND_HIGH)`` qualifies semantically. A fact
    below ``RELATED_BAND_LOW`` (or without a vector) still checks lexical
    overlap (``>= 2`` shared non-stopword tokens when scored below band, ``>= 1``
    when uncached), capped strictly below ``RELATED_BAND_LOW`` so semantic hits
    rank ahead.
    """
    scores = semantic_scores or {}
    draft_tokens = set(_significant_tokens(draft_text)) - _SCORING_STOPWORDS
    ranked: list[tuple[float, int, ExistingFact]] = []
    for order, fact in enumerate(facts):
        score = scores.get(fact.id)
        if score is not None:
            if RELATED_BAND_LOW <= score < RELATED_BAND_HIGH:
                ranked.append((score, order, fact))
                continue
            if score >= RELATED_BAND_HIGH:
                continue
        fact_tokens = set(_significant_tokens(fact.content)) - _SCORING_STOPWORDS
        shared = len(draft_tokens & fact_tokens)
        min_needed = LEXICAL_MIN_SHARED_TOKENS if score is None else LEXICAL_BELOW_BAND_MIN_SHARED
        if shared >= min_needed:
            # Cap below RELATED_BAND_LOW so semantic hits always rank first.
            ranked.append((min(shared, 34) / 100.0, order, fact))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    return [fact for _, _, fact in ranked[:limit]]


@dataclass(frozen=True)
class RelatedContext:
    draft_block: str
    existing_block: str
    existing_map: dict[str, ExistingFact] = field(default_factory=dict)


def _clip(text: str, limit: int = EXISTING_CLIP_CHARS) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _render(batch, related_by_draft, labels: dict[str, str]) -> str:
    lines = []
    for i, row in enumerate(batch, start=1):
        tags = [labels[f.id] for f in related_by_draft.get(i, []) if f.id in labels]
        suffix = f" [related: {', '.join(tags)}]" if tags else ""
        lines.append(f"{i}. {row['content']}{suffix}")
    return "\n".join(lines)


def build_related_context(
    batch: list,
    related_by_draft: dict[int, list[ExistingFact]],
    *,
    budget_chars: int,
    existing_budget_chars: int = EXISTING_BUDGET_CHARS,
) -> RelatedContext:
    """Draft block plus the ``EXISTING FACTS`` block within budget.

    *existing_budget_chars* caps the rendered ``EXISTING FACTS`` section so
    large draft batches never starve contradiction detection, while
    *budget_chars* preserves the overall combined cap when tightened.
    """
    ordered: list[ExistingFact] = []
    seen: set[str] = set()
    for i in range(1, len(batch) + 1):
        for fact in related_by_draft.get(i, []):
            if fact.id not in seen:
                seen.add(fact.id)
                ordered.append(fact)

    kept: list[ExistingFact] = []
    for fact in ordered:
        trial = kept + [fact]
        labels = {f.id: f"E{n}" for n, f in enumerate(trial, start=1)}
        existing_chars = sum(len(f"E{n}. {_clip(f.content)}\n") for n, f in enumerate(trial, start=1))
        used = len(_render(batch, related_by_draft, labels)) + existing_chars
        if existing_chars > existing_budget_chars or used > budget_chars:
            break
        kept.append(fact)

    if not kept:
        return RelatedContext(_render(batch, {}, {}), "", {})
    labels = {f.id: f"E{n}" for n, f in enumerate(kept, start=1)}
    existing_block = "\n".join(
        f"E{n}. {_clip(f.content)}" for n, f in enumerate(kept, start=1)
    )
    return RelatedContext(
        draft_block=_render(batch, related_by_draft, labels),
        existing_block=existing_block,
        existing_map={labels[f.id]: f for f in kept},
    )


@dataclass(frozen=True)
class ResolvedActions:
    decisions: list
    updates: dict[int, tuple[str, int]]
    stats: Counter


def resolve_judge_actions(
    decisions: list,
    actions: dict[int, tuple[str, str]],
    origins: dict[int, str | None],
    existing_map: dict[str, ExistingFact],
    profile_id: str,
) -> ResolvedActions:
    """Deterministic validator between the judge and the store.

    Every KEEP whose judge action is ``duplicate``/``update`` is checked; ANY
    failed check degrades it to ``new`` (the keep is left untouched) and is
    counted as ``degraded_<reason>``. A valid ``duplicate`` becomes a reject
    with reason ``duplicate`` (the existing scrub path); a valid ``update``
    is returned in *updates* as ``{index: (target fact id, revision)}`` for
    the store to apply. ``stats`` holds counts only (new/duplicate/update/
    degraded plus per-reason), never text.

    Degrade rules: unknown label, target of another profile, an ``update`` of
    a ``curated`` target (machine output never touches operator-touched rows;
    a ``duplicate`` of one is allowed, it only rejects the draft), a target that
    is not promoted/imported, an inactive target, an ``uncertain`` candidate,
    and a candidate whose origin is not ``self_stated`` (``shared_content``
    or missing -- the store defaults a missing origin to shared_content).
    """
    stats: Counter = Counter()
    out: list = []
    updates: dict[int, tuple[str, int]] = {}
    for decision in decisions:
        index, text, uncertain, _reason = decision
        action = actions.get(index) if text is not None else None
        if action is None:
            if text is not None:
                stats["new"] += 1
            out.append(decision)
            continue
        kind, label = action
        fact = existing_map.get(label)
        origin = origins.get(index)
        if fact is None:
            failure = "unknown_target"
        elif fact.profile_id != profile_id:
            failure = "cross_profile"
        elif fact.status == "curated" and kind == "update":
            # Only an update writes to the target. A duplicate merely rejects
            # the new draft, so it is allowed against a curated row.
            failure = "curated_target"
        elif fact.status not in _DURABLE_TARGET_STATUSES:
            failure = "invalid_target_status"
        elif fact.inactive:
            failure = "inactive_target"
        elif uncertain:
            failure = "uncertain"
        elif origin == "shared_content":
            failure = "shared_content"
        elif origin != "self_stated":
            failure = "origin_unknown"
        else:
            failure = ""
        if failure:
            stats["degraded"] += 1
            stats[f"degraded_{failure}"] += 1
            stats["new"] += 1
            out.append(decision)
        elif kind == "duplicate":
            stats["duplicate"] += 1
            out.append((index, None, False, "duplicate"))
        else:
            stats["update"] += 1
            updates[index] = (fact.id, fact.revision)
            out.append(decision)
    return ResolvedActions(decisions=out, updates=updates, stats=stats)
