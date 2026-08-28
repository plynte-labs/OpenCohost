"""
opencohost/core/memory/promotion_parser.py

Pure parsing functions and diagnostics for memory promotion judge replies.
Extracted from MotorVocalIA.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import Enum
from typing import Optional


# Prompt for memory promotion judge
_PROMOTION_JUDGE_PROMPT = """You are a memory archivist for a streaming co-host. You do NOT talk to anyone.
You only decide which of the numbered exchanges below are worth remembering
permanently, and rewrite each keeper as ONE standalone sentence.

KEEP an item only if ALL SIX hold:
1. EXPLICIT — the operator stated it. Anything only the assistant speculated,
   guessed, joked about or inferred is not a fact. Reject it.
2. SELF-CONTAINED — your rewrite must be fully understandable a month from now
   by someone who never saw this conversation. No "this", "that", "the game",
   "the question", "the bug", "the fix", no unnamed pronouns, no unresolved
   references. If you cannot name the actual subject from the text you were
   given, REJECT. A vague rewrite is WORSE than no memory at all.
3. SPECIFIC — a concrete fact, preference, name, decision or number. Not a
   mood, not a greeting, not small talk.
4. REUSABLE — useful in a DIFFERENT future conversation, not only as a recap
   of this one.
5. DURABLE — still true next month. Reject anything about the current moment.
6. ATTRIBUTABLE — it is clear whose fact it is.

NAMES: never invent, normalise, translate or "correct" a proper noun. Game
titles, model names, tools and people usually appear only ONCE — that is
normal and is NOT a reason to reject. Copy the operator's own spelling
verbatim. If you are not confident you transcribed a name correctly, still
KEEP the item and set "uncertain": true.

Write each rewrite in the SAME LANGUAGE the operator used, in at most 30 words.

CANDIDATES:
{draft_block}

Reply with JSON only, no prose, no markdown fence:
{"decisions":[{"i":1,"keep":true,"text":"<standalone sentence>"},
              {"i":2,"keep":true,"text":"<sentence>","uncertain":true},
              {"i":3,"keep":false,"reason":"vague"}]}

Field contract:
  i          int, 1..N, exactly one object per candidate number
  keep       bool, required
  text       string, required when keep is true, <=220 characters
  uncertain  bool, optional, only meaningful when keep is true
  reason     string, only when keep is false; one of:
             vague, speculative, trivial, transient, not_attributable

If unsure whether to keep an item, use keep:false."""

_PROMOTION_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)


@dataclass(frozen=True)
class PromotionParseDiagnostics:
    decisions: list[tuple[int, str | None, bool, str]]
    top_level_valid: bool
    unresolved: dict[int, str]


class _PromotionState(Enum):
    NOT_ELIGIBLE = "not_eligible"
    WAITING_IDLE = "waiting_idle"
    RUNNING = "running"


def _parse_promotion_decisions(text: str, batch_len: int) -> list[tuple[int, str | None, bool, str]]:
    """Parse the judge's reply into applied decisions. PURE — no I/O, no engine.

    Returns ``(index, text_or_None, uncertain, reason)`` per usable entry:
    a keep is ``(i, sentence, uncertain, "")``; a reject is ``(i, None, False,
    reason)``. NEVER raises: every unusable shape (empty, prose, a truncated
    reasoning-model reply, a fence full of apologies) collapses to ``[]``.
    """
    if not text or not isinstance(text, str):
        return []
    stripped = _PROMOTION_FENCE_RE.sub("", text.strip())
    try:
        raw_dict = json.loads(stripped)
    except Exception:
        return []
    if not isinstance(raw_dict, dict):
        return []
    try:
        from opencohost.core.memory.models import MemoryJudgeResult
        parsed = MemoryJudgeResult.model_validate_json(stripped)
    except Exception:
        return []

    raw_entries = raw_dict.get("decisions")
    raw_counts: dict[int, int] = {}
    if isinstance(raw_entries, list):
        for entry in raw_entries:
            if isinstance(entry, dict):
                idx = entry.get("i")
                if isinstance(idx, int) and not isinstance(idx, bool):
                    raw_counts[idx] = raw_counts.get(idx, 0) + 1

    results: list[tuple[int, str | None, bool, str]] = []
    seen: set[int] = set()
    for decision in parsed.decisions:
        index = decision.i
        if not 1 <= index <= batch_len or raw_counts.get(index, 0) > 1 or index in seen:
            continue
        seen.add(index)
        if decision.keep:
            results.append((index, decision.text or "", bool(decision.uncertain), ""))
        else:
            results.append((index, None, False, decision.reason or "unspecified"))
    return results


def _parse_promotion_diagnostics(text: str, batch_len: int) -> PromotionParseDiagnostics:
    """Parse the judge's reply into applied decisions and per-draft diagnostics."""
    if not text or not isinstance(text, str):
        return PromotionParseDiagnostics(decisions=[], top_level_valid=False, unresolved={})
    stripped = _PROMOTION_FENCE_RE.sub("", text.strip())
    try:
        payload = json.loads(stripped)
    except Exception:
        return PromotionParseDiagnostics(decisions=[], top_level_valid=False, unresolved={})
    if not isinstance(payload, dict) or set(payload.keys()) != {"decisions"}:
        return PromotionParseDiagnostics(decisions=[], top_level_valid=False, unresolved={})
    entries = payload.get("decisions")
    if not isinstance(entries, list):
        return PromotionParseDiagnostics(decisions=[], top_level_valid=False, unresolved={})

    decisions = _parse_promotion_decisions(text, batch_len)
    decision_indices = {d[0] for d in decisions}

    raw_counts: dict[int, int] = {}
    present_indices: set[int] = set()
    for entry in entries:
        if isinstance(entry, dict):
            idx = entry.get("i")
            if isinstance(idx, int) and not isinstance(idx, bool):
                raw_counts[idx] = raw_counts.get(idx, 0) + 1
                present_indices.add(idx)

    unresolved: dict[int, str] = {}
    for i in range(1, batch_len + 1):
        if i in decision_indices:
            continue
        if raw_counts.get(i, 0) > 1:
            unresolved[i] = "duplicate_index"
        elif i in present_indices:
            unresolved[i] = "invalid_decision"
        else:
            unresolved[i] = "missing_decision"

    return PromotionParseDiagnostics(
        decisions=decisions,
        top_level_valid=True,
        unresolved=unresolved,
    )
