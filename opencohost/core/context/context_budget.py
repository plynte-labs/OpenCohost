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
from typing import Optional

# Architecture-specific context keys tried in priority order. The first key
# yielding a positive integer wins (design §2 Layer 1).
_ARCH_CTX_KEYS = (
    "llama.context_length",
    "gemma.context_length",
    "gemma4.context_length",
    "phi3.context_length",
    "qwen2.context_length",
    "qwen3.context_length",
    "mistral.context_length",
)

_CTX_FLOOR = 512

# Suffix shared by every architecture-specific ctx key Ollama reports
# (`<arch>.context_length`). `_ARCH_CTX_KEYS` above cannot stay exhaustive —
# new model families (e.g. Qwen3.5's `qwen35`) ship faster than this
# whitelist — so any key with this suffix is accepted as a fallback layer.
_ARCH_CTX_KEY_SUFFIX = ".context_length"

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


def _generic_arch_ctx_value(model_info) -> int | None:
    """Accept any ``<arch>.context_length`` key not covered by ``_ARCH_CTX_KEYS``.

    Prefers the key matching ``general.architecture`` when present (e.g.
    reported arch ``qwen35`` -> ``qwen35.context_length``), so an explicitly
    reported architecture always wins over incidental key order. Otherwise
    takes the first positive-int match in iteration order.
    """
    if not isinstance(model_info, dict):
        return None

    arch = model_info.get("general.architecture")
    if isinstance(arch, str) and arch:
        value = model_info.get(f"{arch}{_ARCH_CTX_KEY_SUFFIX}")
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return value

    for key, value in model_info.items():
        if (
            isinstance(key, str)
            and key.endswith(_ARCH_CTX_KEY_SUFFIX)
            and isinstance(value, int)
            and not isinstance(value, bool)
            and value > 0
        ):
            return value
    return None


def parse_model_ctx(show_response, *, fallback: int) -> int:
    """Resolve a model's native context length from an ``ollama.show`` response.

    Tries each architecture key in :data:`_ARCH_CTX_KEYS` (first positive int
    wins). If none is found, parses the Modelfile ``parameters`` block for a
    ``num_ctx <int>`` line and prefers it (design §0.3 gemma refinement). Only
    then falls back to ``fallback``. The result is clamped to ``max(512, value)``.

    ``show_response`` may be a ``dict`` or an attribute-style object (e.g. an
    Ollama ``ShowResponse`` Pydantic model). Any missing field degrades safely.
    """
    # Real ollama ShowResponse exposes this field as the attribute `.modelinfo`
    # (Pydantic; `model_info` is an alias-only key). Raw JSON and test dicts use
    # the `model_info` key. Try the attribute name first, then the dict/legacy key.
    # (Reading only "model_info" left ctx-discovery dead on every real response —
    # always the fallback, never the model's native ctx; realenv R1.)
    model_info = _get_field(show_response, "modelinfo")
    if model_info is None:
        model_info = _get_field(show_response, "model_info")

    # 1. Architecture-specific context keys, in priority order.
    for key in _ARCH_CTX_KEYS:
        value = _get_field(model_info, key)
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return max(_CTX_FLOOR, value)

    # 1b. Unknown architecture: any `<arch>.context_length` key. Keeping an
    # exhaustive per-arch whitelist is a losing race against new model
    # families (Qwen3.5 reports `qwen35`, unknown to `_ARCH_CTX_KEYS`).
    generic_value = _generic_arch_ctx_value(model_info)
    if generic_value is not None:
        return max(_CTX_FLOOR, generic_value)

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


_SPILL_RESIDENCY_THRESHOLD = 0.98
_HEADROOM_MIN_BYTES = 1024 ** 3
_HEADROOM_FRACTION = 0.15


def clamp_ctx_for_vram(
    requested: int,
    *,
    native: int,
    residency_ratio: float | None = None,
    free_bytes: int | None = None,
    total_bytes: int | None = None,
    floor: int = 4096,
) -> tuple[int, str]:
    """Grant a context size for ``requested`` given VRAM evidence (ADR-056).

    Returns ``(granted, reason)``. Single-step design: at most ONE halving per
    decision. The caller reloads the model at the granted size and the next
    decision re-evaluates with fresh evidence, so a larger overshoot converges
    over successive loads instead of guessing a KV-cache size model here.

    Evidence, in priority order:
      - ``residency_ratio`` (Ollama ``size_vram / size`` of the load being
        judged): below 0.98 means layers spilled to host RAM → halve
        (``spill_step_down``), or ``spill_at_floor`` if already at the floor.
        At or above 0.98 → keep (``resident_fits``).
      - NVML ``free_bytes`` / ``total_bytes`` (both must be > 0): free below
        the headroom ``max(1 GiB, 15% of total)`` → halve
        (``vram_headroom_step_down`` / ``vram_headroom_at_floor``); otherwise
        ``vram_ok``.
      - No evidence → no clamp (``vram_unknown``). Missing data is never a
        reason to shrink.

    The result never exceeds ``native`` and never halves below
    ``min(floor, requested)``. When nothing clamps but ``native`` capped the
    request, the reason is ``native_cap``.
    """
    cap = int(requested)
    native_capped = native > 0 and cap > native
    if native_capped:
        cap = int(native)
    effective_floor = min(int(floor), cap)

    def _halve(prefix: str) -> tuple[int, str]:
        halved = max(effective_floor, cap // 2)
        if halved >= cap:
            return cap, f"{prefix}_at_floor"
        return halved, f"{prefix}_step_down"

    if residency_ratio is not None:
        if residency_ratio < _SPILL_RESIDENCY_THRESHOLD:
            return _halve("spill")
        reason = "resident_fits"
    elif free_bytes is not None and total_bytes and total_bytes > 0:
        headroom = max(_HEADROOM_MIN_BYTES, _HEADROOM_FRACTION * total_bytes)
        if free_bytes < headroom:
            return _halve("vram_headroom")
        reason = "vram_ok"
    else:
        reason = "vram_unknown"

    if native_capped:
        reason = "native_cap"
    return cap, reason


MAX_TURNS_CAP_FLOOR = 10
MAX_TURNS_CAP_CEILING = 40
_TOKENS_PER_TURN_PAIR = 350


def max_turns_cap_for_ctx(ctx, *, max_output: int) -> int:
    """Return the profile ``max_turns`` cap allowed by an effective ``ctx``.

    ``max(10, min(40, (ctx - max_output) // 350))`` — 350 tokens is a rough
    user+assistant pair. The floor keeps today's fixed cap of 10 at the
    default 4096 ctx; an unknown / non-positive ctx also yields the floor. The
    cap only bounds the history deque: the char budget gate still evicts by
    size, so a high cap can never overflow the context window.
    """
    if isinstance(ctx, bool) or not isinstance(ctx, int) or ctx <= 0:
        return MAX_TURNS_CAP_FLOOR
    derived = (ctx - int(max_output)) // _TOKENS_PER_TURN_PAIR
    return max(MAX_TURNS_CAP_FLOOR, min(MAX_TURNS_CAP_CEILING, derived))


def apply_char_budget_pure(
    messages: list[dict],
    *,
    ctx_limit: int,
    max_output_tokens: int,
    safety_factor: float,
) -> tuple[list[dict], list[tuple[dict, dict]], int]:
    """Pure, non-mutating context budget partitioning.

    Returns:
        (retained_messages, evicted_pairs, n_pairs_evicted)
    where:
        - retained_messages is a new list of dicts within the budget.
        - evicted_pairs is a list of (user_msg, assistant_msg) tuples that were
          evicted, preserved in chronological order.
        - n_pairs_evicted is the count of evicted pairs.
    """
    char_budget = (ctx_limit - max_output_tokens) * safety_factor

    def _total(msgs: list[dict]) -> int:
        return sum(len(m.get("content", "")) for m in msgs)

    if _total(messages) <= char_budget:
        return list(messages), [], 0

    retained = list(messages)
    front = 1 if (retained and isinstance(retained[0], dict) and retained[0].get("role") == "system") else 0
    tail = 2 if (len(retained) >= 2 and isinstance(retained[-2], dict) and retained[-2].get("role") == "system") else 1
    evicted_pairs: list[tuple[dict, dict]] = []
    n_pairs_evicted = 0

    while _total(retained) > char_budget and (len(retained) - front - tail) >= 1:
        if (len(retained) - front - tail) >= 2:
            u_msg = retained[front]
            a_msg = retained[front + 1]
            evicted_pairs.append((u_msg, a_msg))
            del retained[front:front + 2]
        else:
            u_msg = retained[front]
            evicted_pairs.append((u_msg, {"role": "assistant", "content": ""}))
            del retained[front]
        n_pairs_evicted += 1

    return retained, evicted_pairs, n_pairs_evicted


# ADR-064 section 3: head eviction in chunks. Evicting one pair per turn moves
# the first kept message on every turn once the budget is full, so the KV prefix
# restarts from the new head each time. Evicting a chunk down to a low-water
# mark moves the head once every EVICTION_CHUNK_PAIRS turns or so.
EVICTION_CHUNK_PAIRS = 4
EVICTION_LOW_WATER = 0.75


def history_anchor(messages: list[dict]) -> tuple:
    """Identity of the first kept history pair (roles + content hashes)."""
    import hashlib

    def _h(m: dict) -> str:
        raw = f"{m.get('role', '')}{m.get('content', '')}"
        return hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()

    return tuple(_h(m) for m in messages[:2])


def apply_chunked_char_budget(
    messages: list[dict],
    *,
    ctx_limit: int,
    max_output_tokens: int,
    safety_factor: float,
    anchor: Optional[tuple] = None,
    chunk_pairs: int = EVICTION_CHUNK_PAIRS,
    low_water: float = EVICTION_LOW_WATER,
    stats: Optional[dict] = None,
) -> tuple[list[dict], list[tuple[dict, dict]], int, Optional[tuple]]:
    """Pure chunked variant of :func:`apply_char_budget_pure`.

    ``anchor`` is the :func:`history_anchor` of the head kept by the previous
    turn. History before it stays evicted (hysteresis), so the head does not
    move while the rest still fits. When the prompt exceeds the budget, at
    least ``chunk_pairs`` pairs are dropped and then more, until the prompt is
    at or under ``low_water`` of the budget. Returns ``(retained, evicted_pairs,
    n_evicted, new_anchor)``; ``evicted_pairs`` holds every pair not in the
    prompt, hidden-before-anchor ones included, like the unchunked variant.
    ``new_anchor`` is None until an eviction has happened.

    ``stats`` (optional, filled in place) separates the two kinds of eviction
    for the trace: ``hidden_pairs`` are pairs kept out by the anchor hysteresis
    (already counted on earlier turns) and ``new_pairs`` are pairs dropped by
    the budget on this turn.
    """
    char_budget = (ctx_limit - max_output_tokens) * safety_factor

    def _total(msgs: list[dict]) -> int:
        return sum(len(m.get("content", "")) for m in msgs)

    retained = list(messages)
    front = 1 if (retained and isinstance(retained[0], dict) and retained[0].get("role") == "system") else 0
    tail = 2 if (len(retained) >= 2 and isinstance(retained[-2], dict) and retained[-2].get("role") == "system") else 1
    evicted_pairs: list[tuple[dict, dict]] = []
    if stats is not None:
        stats["hidden_pairs"] = 0
        stats["new_pairs"] = 0

    def _drop_front(n_msgs: int) -> None:
        for _ in range(n_msgs // 2):
            evicted_pairs.append((retained[front], retained[front + 1]))
            del retained[front:front + 2]
        if n_msgs % 2:
            evicted_pairs.append((retained[front], {"role": "assistant", "content": ""}))
            del retained[front]

    new_anchor: Optional[tuple] = None
    if anchor is not None:
        body = retained[front:len(retained) - tail]
        for i in range(len(body)):
            if history_anchor(body[i:]) == anchor:
                if i:
                    _drop_front(i)
                if stats is not None:
                    stats["hidden_pairs"] = len(evicted_pairs)
                new_anchor = anchor  # found: history before it stays evicted
                break
        # not found: the history was reset or rotated past it; start over.

    if _total(retained) > char_budget:
        target = char_budget * low_water
        already_hidden = len(evicted_pairs)
        dropped = 0
        while (len(retained) - front - tail) >= 1 and (
            dropped < chunk_pairs * 2 or _total(retained) > target
        ):
            take = 2 if (len(retained) - front - tail) >= 2 else 1
            _drop_front(take)
            dropped += take
        new_anchor = history_anchor(retained[front:len(retained) - tail])
        if stats is not None:
            stats["new_pairs"] = len(evicted_pairs) - already_hidden

    return retained, evicted_pairs, len(evicted_pairs), new_anchor


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
    retained, _, n_pairs_evicted = apply_char_budget_pure(
        messages,
        ctx_limit=ctx_limit,
        max_output_tokens=max_output_tokens,
        safety_factor=safety_factor,
    )
    if n_pairs_evicted > 0:
        messages.clear()
        messages.extend(retained)
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
