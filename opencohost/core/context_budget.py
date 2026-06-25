"""Pure context-budget logic for the context-overflow guardrail.

This module holds the PURE, engine-free logic split out of ``llm_engine.py``
(see ``conductor/tracks/context_overflow_guardrail_20260623/design.md`` §0.1).

Design constraints (load-bearing):
  - Every function is pure: all inputs arrive as arguments. There is NO module-
    level mutable state, NO engine ``self``, and NO ``settings`` import. Callers
    pass the relevant constants (``fallback``, ``safety_factor``, ``ratio``).
  - This module MUST NOT import ``llm_engine`` or ``app_shell`` and pulls in
    nothing heavy (no ``ollama``): it receives the already-fetched ``ollama.show``
    response as an argument.
  - Eviction protects the FINAL message (current turn, index -1) always, and
    index 0 ONLY when it carries ``role='system'``. In the ``use_system_role=False``
    path the system prompt is folded into the FINAL combined ``role='user'`` message
    (index -1), so the oldest history turns at the front ARE evictable. Pinning
    index 0 unconditionally would preserve the oldest history user turn and evict
    newer context instead, which is the opposite of what is intended.
"""

from __future__ import annotations

import re

# Architecture-specific context keys tried in priority order. The first key
# yielding a positive integer wins (design §2 Layer 1).
_ARCH_CTX_KEYS = (
    "llama.context_length",
    "gemma.context_length",
    "phi3.context_length",
    "qwen2.context_length",
    "mistral.context_length",
)

_CTX_FLOOR = 512

# Matches a Modelfile parameters line like ``num_ctx   8192`` (design §0.3).
_NUM_CTX_PARAM_RE = re.compile(r"^\s*num_ctx\s+(\d+)\s*$", re.MULTILINE)


def _get_field(obj, name):
    """Read ``name`` from ``obj`` handling dict OR attribute-style objects.

    Mirrors the dual handling already used in ``llm_engine._check_capabilities_reasoning``:
    ``info.get(key) if isinstance(info, dict) else getattr(info, key, None)``.
    """
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def parse_model_ctx(show_response, *, fallback: int) -> int:
    """Resolve a model's native context length from an ``ollama.show`` response.

    Tries each architecture key in :data:`_ARCH_CTX_KEYS` (first positive int
    wins). If none is found, parses the Modelfile ``parameters`` block for a
    ``num_ctx <int>`` line and prefers it (design §0.3 gemma refinement). Only
    then falls back to ``fallback``. The result is clamped to ``max(512, value)``.

    ``show_response`` may be a ``dict`` or an attribute-style object (e.g. an
    Ollama ``ShowResponse`` Pydantic model). Any missing field degrades safely.
    """
    model_info = _get_field(show_response, "model_info")

    # 1. Architecture-specific context keys, in priority order.
    for key in _ARCH_CTX_KEYS:
        value = _get_field(model_info, key)
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return max(_CTX_FLOOR, value)

    # 2. Modelfile parameters block: `num_ctx <int>` (gemma gap, §0.3).
    parameters = _get_field(show_response, "parameters")
    if isinstance(parameters, str):
        match = _NUM_CTX_PARAM_RE.search(parameters)
        if match:
            parsed = int(match.group(1))
            if parsed > 0:
                return max(_CTX_FLOOR, parsed)

    # 3. Fallback (still clamped to the floor).
    return max(_CTX_FLOOR, fallback)


def apply_char_budget(
    messages: list[dict],
    *,
    ctx_limit: int,
    max_output_tokens: int,
    safety_factor: float,
) -> tuple[list[dict], int]:
    """Evict oldest history pairs from ``messages`` until it fits the char budget.

    The budget is ``(ctx_limit - max_output_tokens) * safety_factor`` characters.

    Eviction invariants:
      - The FINAL message (index -1, the current user turn) is always protected.
      - Index 0 is protected ONLY when ``messages[0].get("role") == "system"``.
        In the ``use_system_role=False`` engine path, index 0 is the OLDEST
        history user turn and is fully evictable.
      - ``front`` is 1 (skip system at index 0) or 0 (index 0 is evictable).
      - Pairs are removed from ``messages[front:-1]`` oldest-first; an odd
        unmatched front entry is evicted alone.

    ``messages`` is modified IN PLACE. Returns ``(messages, n_pairs_evicted)``.
    """
    char_budget = (ctx_limit - max_output_tokens) * safety_factor

    def _total() -> int:
        return sum(len(m.get("content", "")) for m in messages)

    if _total() <= char_budget:
        return messages, 0

    # Determine the front anchor: protect index 0 only when it is a system message.
    front = 1 if (messages and isinstance(messages[0], dict) and messages[0].get("role") == "system") else 0

    n_pairs_evicted = 0
    # Evict from the front of the evictable slice messages[front:-1].
    # Need at least one evictable message between front and the protected final turn.
    while _total() > char_budget and (len(messages) - front - 1) >= 1:
        if (len(messages) - front - 1) >= 2:
            del messages[front:front + 2]
        else:
            del messages[front]
        n_pairs_evicted += 1

    return messages, n_pairs_evicted


def trim_messages_reactive(messages: list[dict], *, n_pairs: int = 3) -> int:
    """Drop up to ``n_pairs`` oldest pairs from the evictable slice, in place.

    Eviction invariants (same as ``apply_char_budget``):
      - The FINAL message (index -1, the current user turn) is always protected.
      - Index 0 is protected ONLY when ``messages[0].get("role") == "system"``.
        In the ``use_system_role=False`` engine path, index 0 is the OLDEST
        history user turn and is fully evictable.
      - A pair is two consecutive messages spliced from the front of the
        evictable slice ``messages[front:-1]``.

    Returns the number of complete pairs actually removed.
    """
    # Determine the front anchor: protect index 0 only when it is a system message.
    front = 1 if (messages and isinstance(messages[0], dict) and messages[0].get("role") == "system") else 0

    removed = 0
    for _ in range(n_pairs):
        # Need at least 2 evictable messages between front and the final turn.
        if (len(messages) - front - 1) < 2:
            break
        del messages[front:front + 2]
        removed += 1
    return removed


def is_overflow_signal(
    content: str,
    prompt_eval_count: int | None,
    ctx_limit: int,
    ratio: float,
) -> bool:
    """Return True iff the response is an input-overflow signal.

    The signal is: empty/whitespace-only content AND a positive
    ``prompt_eval_count`` that has reached at least ``ctx_limit * ratio``.

    ``prompt_eval_count`` is coerced to 0 when None (Ollama omits the field
    for KV-cache-served prompts; the ChatResponse attribute exists but is None).
    """
    # H1 guard: Ollama KV-cache responses set prompt_eval_count=None.
    # Coerce to 0 so the positive-count check below degrades safely.
    prompt_eval_count = prompt_eval_count or 0
    # Compare against the truncated integer threshold so the documented boundary
    # (prompt_eval_count == int(ctx_limit * ratio)) fires under a `>=` test.
    # prompt_eval_count is always an integer token count, so an int threshold is
    # the natural, drift-safe comparison (design §4.5 boundary tests).
    threshold = int(ctx_limit * ratio)
    return (
        not content.strip()
        and prompt_eval_count > 0
        and prompt_eval_count >= threshold
    )


def utilization(prompt_eval_count: int | None, ctx_limit: int) -> float:
    """Return ``prompt_eval_count / ctx_limit`` (0.0 if either is non-positive).

    ``prompt_eval_count`` is coerced to 0 when None (Ollama KV-cache path).
    """
    # H1 guard: coerce None to 0 so the non-positive check below degrades safely.
    prompt_eval_count = prompt_eval_count or 0
    if ctx_limit <= 0 or prompt_eval_count <= 0:
        return 0.0
    return prompt_eval_count / ctx_limit
