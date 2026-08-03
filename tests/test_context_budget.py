"""Pure-function unit tests for opencohost.core.context.context_budget.

These tests cover the PURE-LOGIC slice of the context_overflow_guardrail track
(design.md §0.1 modular split). Every function under test is pure: it takes all
inputs as arguments (no engine state, no self, no settings import). ``ctx_limit``
is supplied as a plain int instead of populating an engine cache.

Sections map to design.md §4:
  - §4.2  parse_model_ctx            (Layer 1, pure form)
  - §0.3  parse_model_ctx params block (gemma num_ctx Modelfile parse refinement)
  - §4.3  apply_char_budget          (Layer 2, pure form)
  - §4.4  trim_messages_reactive     (Layer 3, pure form)
  - §4.5  is_overflow_signal         (Layer 3 fire condition, boundary tests)
  - §4.6  utilization                (Layer 4 ratio, threshold cases)
"""

from types import SimpleNamespace

import pytest

from opencohost.core.context import context_budget as cb

FALLBACK = 4096


# ──────────────────────────────────────────────────────────────────────────
# §4.2 — parse_model_ctx (Layer 1, pure)
# ──────────────────────────────────────────────────────────────────────────

def test_parse_ctx_reads_llama_context_length():
    show = {"model_info": {"llama.context_length": 32768}}
    assert cb.parse_model_ctx(show, fallback=FALLBACK) == 32768


def test_parse_ctx_tries_gemma_key_when_llama_absent():
    show = {"model_info": {"gemma.context_length": 8192}}
    assert cb.parse_model_ctx(show, fallback=FALLBACK) == 8192


def test_parse_ctx_tries_phi3_key():
    show = {"model_info": {"phi3.context_length": 131072}}
    assert cb.parse_model_ctx(show, fallback=FALLBACK) == 131072


def test_parse_ctx_tries_qwen2_key():
    show = {"model_info": {"qwen2.context_length": 40960}}
    assert cb.parse_model_ctx(show, fallback=FALLBACK) == 40960


def test_parse_ctx_tries_mistral_key():
    show = {"model_info": {"mistral.context_length": 32768}}
    assert cb.parse_model_ctx(show, fallback=FALLBACK) == 32768


def test_parse_ctx_key_priority_order_llama_first():
    # When multiple keys exist, the ordered list must pick llama first.
    show = {"model_info": {
        "gemma.context_length": 8192,
        "llama.context_length": 32768,
    }}
    assert cb.parse_model_ctx(show, fallback=FALLBACK) == 32768


def test_parse_ctx_fallback_when_no_key_matches():
    show = {"model_info": {}}
    assert cb.parse_model_ctx(show, fallback=FALLBACK) == FALLBACK


def test_parse_ctx_fallback_when_model_info_missing():
    show = {}
    assert cb.parse_model_ctx(show, fallback=FALLBACK) == FALLBACK


def test_parse_ctx_minimum_clamp_on_zero():
    show = {"model_info": {"llama.context_length": 0}}
    # Zero is not a positive int -> falls through to fallback, then clamped.
    assert cb.parse_model_ctx(show, fallback=FALLBACK) == FALLBACK


def test_parse_ctx_negative_value_falls_through_to_fallback():
    show = {"model_info": {"llama.context_length": -100}}
    assert cb.parse_model_ctx(show, fallback=FALLBACK) == FALLBACK


def test_parse_ctx_clamps_small_fallback_to_512_floor():
    # If the caller supplies an absurdly small fallback, clamp to 512.
    show = {"model_info": {}}
    assert cb.parse_model_ctx(show, fallback=100) == 512


def test_parse_ctx_clamps_small_positive_context_to_512_floor():
    show = {"model_info": {"llama.context_length": 256}}
    assert cb.parse_model_ctx(show, fallback=FALLBACK) == 512


def test_parse_ctx_attribute_style_response():
    # show_response as attribute-style object (model_info is itself attr/dict).
    show = SimpleNamespace(model_info={"llama.context_length": 16384})
    assert cb.parse_model_ctx(show, fallback=FALLBACK) == 16384


def test_parse_ctx_attribute_style_no_keys_falls_back():
    show = SimpleNamespace(model_info={})
    assert cb.parse_model_ctx(show, fallback=FALLBACK) == FALLBACK


def test_parse_ctx_does_not_crash_on_none_response():
    assert cb.parse_model_ctx(None, fallback=FALLBACK) == FALLBACK


def test_parse_ctx_reads_real_modelinfo_attribute_not_alias():
    # Real ollama ShowResponse exposes the dict under the `modelinfo` attribute
    # (`model_info` is an alias-only field, unreachable via getattr). Reading only
    # `model_info` left ctx-discovery dead on every real response. (realenv R1.)
    show = SimpleNamespace(modelinfo={"llama.context_length": 8192})
    assert cb.parse_model_ctx(show, fallback=FALLBACK) == 8192


def test_parse_ctx_tries_gemma4_key():
    show = {"model_info": {"gemma4.context_length": 131072}}
    assert cb.parse_model_ctx(show, fallback=FALLBACK) == 131072


def test_parse_ctx_tries_qwen3_key():
    show = {"model_info": {"qwen3.context_length": 40960}}
    assert cb.parse_model_ctx(show, fallback=FALLBACK) == 40960


# ──────────────────────────────────────────────────────────────────────────
# §0.3 — parse_model_ctx parameters-block num_ctx parse (gemma refinement)
# ──────────────────────────────────────────────────────────────────────────

def test_parse_ctx_reads_num_ctx_from_parameters_block():
    # gemma4:e2b: model_info has no context_length key, but the Modelfile
    # parameters block declares `num_ctx 8192`. Prefer it over the fallback.
    show = {
        "model_info": {},
        "parameters": "stop \"<end>\"\nnum_ctx   8192\ntemperature 0.7\n",
    }
    assert cb.parse_model_ctx(show, fallback=FALLBACK) == 8192


def test_parse_ctx_model_info_key_wins_over_parameters_block():
    # When a model_info context key exists, it is preferred over parameters.
    show = {
        "model_info": {"llama.context_length": 32768},
        "parameters": "num_ctx 8192\n",
    }
    assert cb.parse_model_ctx(show, fallback=FALLBACK) == 32768


def test_parse_ctx_parameters_block_attribute_style():
    show = SimpleNamespace(model_info={}, parameters="num_ctx 8192\n")
    assert cb.parse_model_ctx(show, fallback=FALLBACK) == 8192


def test_parse_ctx_parameters_block_ignored_when_no_num_ctx():
    show = {"model_info": {}, "parameters": "temperature 0.7\nstop \"<eos>\"\n"}
    assert cb.parse_model_ctx(show, fallback=FALLBACK) == FALLBACK


def test_parse_ctx_parameters_block_num_ctx_clamped():
    show = {"model_info": {}, "parameters": "num_ctx 128\n"}
    assert cb.parse_model_ctx(show, fallback=FALLBACK) == 512


# ──────────────────────────────────────────────────────────────────────────
# §4.3 — apply_char_budget (Layer 2, pure)
# ──────────────────────────────────────────────────────────────────────────

def _msg(role, n):
    return {"role": role, "content": "x" * n}


def test_budget_no_eviction_when_under_budget():
    messages = [
        _msg("system", 100),
        _msg("user", 100),
        _msg("assistant", 100),
        _msg("user", 100),
    ]
    out, evicted = cb.apply_char_budget(
        messages, ctx_limit=4096, max_output_tokens=768, safety_factor=3.5
    )
    assert evicted == 0
    assert out is messages
    assert len(out) == 4


def test_budget_evicts_oldest_pair_when_over_budget():
    # budget = (4096 - 768) * 3.5 = 11648
    # system(100) + u(3000)+a(3000)+u(3000)+a(3000) + final user(100) = 12200
    messages = [
        _msg("system", 100),
        _msg("user", 3000),
        _msg("assistant", 3000),
        _msg("user", 3000),
        _msg("assistant", 3000),
        _msg("user", 100),
    ]
    out, evicted = cb.apply_char_budget(
        messages, ctx_limit=4096, max_output_tokens=768, safety_factor=3.5
    )
    assert evicted == 1
    # system preserved at index 0
    assert out[0]["role"] == "system"
    # final user turn preserved at the end
    assert out[-1]["content"] == "x" * 100
    total = sum(len(m["content"]) for m in out)
    assert total <= (4096 - 768) * 3.5


def test_budget_evicts_multiple_pairs_when_far_over_budget():
    messages = [_msg("system", 100)]
    for i in range(3):
        messages.append(_msg("user", 4000))
        messages.append(_msg("assistant", 4000))
    messages.append(_msg("user", 100))
    out, evicted = cb.apply_char_budget(
        messages, ctx_limit=4096, max_output_tokens=768, safety_factor=3.5
    )
    assert evicted >= 2
    assert out[0]["role"] == "system"
    assert out[-1]["content"] == "x" * 100
    total = sum(len(m["content"]) for m in out)
    assert total <= (4096 - 768) * 3.5


def test_budget_preserves_system_and_final_user():
    messages = [
        _msg("system", 100),
        _msg("user", 5000),
        _msg("assistant", 5000),
        _msg("user", 200),
    ]
    out, _ = cb.apply_char_budget(
        messages, ctx_limit=4096, max_output_tokens=768, safety_factor=3.5
    )
    assert out[0]["role"] == "system"
    assert out[-1]["content"] == "x" * 200


def test_budget_stops_when_evictable_slice_exhausted():
    # Only system + one oversized user turn. Nothing is evictable.
    messages = [
        _msg("system", 100),
        _msg("user", 50000),
    ]
    out, evicted = cb.apply_char_budget(
        messages, ctx_limit=4096, max_output_tokens=768, safety_factor=3.5
    )
    assert evicted == 0
    assert out[0]["role"] == "system"
    assert out[-1]["content"] == "x" * 50000


def test_budget_uses_larger_ctx_limit_no_eviction():
    # Moderately large list that overflows at 4096 but fits at 131072.
    messages = [_msg("system", 100)]
    for _ in range(2):
        messages.append(_msg("user", 4000))
        messages.append(_msg("assistant", 4000))
    messages.append(_msg("user", 100))
    out, evicted = cb.apply_char_budget(
        messages, ctx_limit=131072, max_output_tokens=768, safety_factor=3.5
    )
    assert evicted == 0


def test_budget_no_system_role_protects_final_combined_message():
    # use_system_role=False: the system prompt is folded into the FINAL combined
    # role='user' message (index -1), NOT index 0. Index 0 here is the OLDEST
    # history user turn and is fully evictable. The combined current turn at
    # index -1 must always be preserved.
    old_history = {"role": "user", "content": "OLD_HIST " + ("x" * 4000)}
    combined_current = {"role": "user", "content": "SYSTEM PROMPT COMBINED " + ("x" * 100)}
    messages = [
        old_history,
        _msg("assistant", 4000),
        _msg("user", 4000),
        _msg("assistant", 4000),
        combined_current,  # final message — must be preserved
    ]
    out, evicted = cb.apply_char_budget(
        messages, ctx_limit=4096, max_output_tokens=768, safety_factor=3.5
    )
    assert evicted >= 1
    # Combined current turn at index -1 must be preserved.
    assert out[-1] is combined_current
    assert out[-1]["content"].startswith("SYSTEM PROMPT COMBINED")
    # Old history at index 0 is evictable in the use_system_role=False path.
    contents_start = [m["content"][:8] for m in out]
    assert "OLD_HIST" not in contents_start


def test_budget_evicts_odd_unmatched_front_message_alone():
    # Evictable middle has an odd count (unmatched front entry). Distinct
    # content markers so membership reflects identity, not value-equality.
    messages = [
        {"role": "system", "content": "SYS" + "x" * 100},
        {"role": "user", "content": "OLD1" + "x" * 8000},
        {"role": "assistant", "content": "OLD2" + "x" * 8000},
        {"role": "user", "content": "OLD3" + "x" * 8000},
        {"role": "user", "content": "CUR" + "x" * 100},
    ]
    out, evicted = cb.apply_char_budget(
        messages, ctx_limit=4096, max_output_tokens=768, safety_factor=3.5
    )
    contents = [m["content"][:4] for m in out]
    # The oldest evictable pair must be gone.
    assert "OLD1" not in contents
    assert "OLD2" not in contents
    assert out[0]["content"].startswith("SYS")
    assert out[-1]["content"].startswith("CUR")
    assert evicted >= 1


def test_budget_modifies_in_place_returns_same_object():
    messages = [
        _msg("system", 100),
        _msg("user", 5000),
        _msg("assistant", 5000),
        _msg("user", 100),
    ]
    out, _ = cb.apply_char_budget(
        messages, ctx_limit=4096, max_output_tokens=768, safety_factor=3.5
    )
    assert out is messages


# ──────────────────────────────────────────────────────────────────────────
# §4.4 — trim_messages_reactive (Layer 3, pure)
# ──────────────────────────────────────────────────────────────────────────

def _build_history(n_pairs):
    # [sys, u1, a1, ..., un, an, u_current]
    msgs = [_msg("system", 10)]
    for i in range(n_pairs):
        msgs.append({"role": "user", "content": f"u{i}"})
        msgs.append({"role": "assistant", "content": f"a{i}"})
    msgs.append({"role": "user", "content": "current"})
    return msgs


def test_trim_reactive_drops_n_pairs():
    messages = _build_history(3)  # sys + 6 + current = 8
    removed = cb.trim_messages_reactive(messages, n_pairs=3)
    assert removed == 3
    assert messages[0]["role"] == "system"
    assert messages[-1]["content"] == "current"
    # All 6 history messages dropped -> only sys + current remain.
    assert len(messages) == 2


def test_trim_reactive_partial_when_fewer_pairs_available():
    messages = _build_history(1)  # sys + u + a + current
    removed = cb.trim_messages_reactive(messages, n_pairs=3)
    assert removed == 1
    assert messages[0]["role"] == "system"
    assert messages[-1]["content"] == "current"


def test_trim_reactive_zero_when_no_evictable():
    messages = [_msg("system", 10), {"role": "user", "content": "current"}]
    removed = cb.trim_messages_reactive(messages, n_pairs=3)
    assert removed == 0
    assert len(messages) == 2


def test_trim_reactive_modifies_in_place_identity():
    messages = _build_history(2)
    before = messages
    cb.trim_messages_reactive(messages, n_pairs=1)
    assert messages is before


def test_trim_reactive_preserves_system_and_last_user():
    messages = _build_history(2)
    cb.trim_messages_reactive(messages, n_pairs=5)
    assert messages[0]["role"] == "system"
    assert messages[-1]["content"] == "current"


def test_trim_reactive_default_n_pairs_is_three():
    messages = _build_history(5)  # 10 history messages
    removed = cb.trim_messages_reactive(messages)
    assert removed == 3


# ──────────────────────────────────────────────────────────────────────────
# §4.5 — is_overflow_signal (Layer 3 fire condition, boundary tests)
# ──────────────────────────────────────────────────────────────────────────

RATIO = 0.95


def test_overflow_signal_true_at_exact_boundary():
    pec = int(4096 * RATIO)  # 3891
    assert cb.is_overflow_signal("", pec, 4096, RATIO) is True


def test_overflow_signal_false_one_below_boundary():
    pec = int(4096 * RATIO) - 1  # 3890
    assert cb.is_overflow_signal("", pec, 4096, RATIO) is False


def test_overflow_signal_false_when_content_present():
    pec = int(4096 * RATIO)
    assert cb.is_overflow_signal("Hello", pec, 4096, RATIO) is False


def test_overflow_signal_false_when_content_is_whitespace_only_is_true():
    # whitespace-only content counts as empty (not content.strip()).
    pec = int(4096 * RATIO)
    assert cb.is_overflow_signal("   \n  ", pec, 4096, RATIO) is True


def test_overflow_signal_false_when_pec_zero():
    assert cb.is_overflow_signal("", 0, 4096, RATIO) is False


def test_overflow_signal_false_when_pec_negative():
    assert cb.is_overflow_signal("", -5, 4096, RATIO) is False


def test_overflow_signal_well_above_boundary_true():
    assert cb.is_overflow_signal("", 8191, 8192, RATIO) is True


# ──────────────────────────────────────────────────────────────────────────
# §4.6 — utilization (Layer 4 ratio, threshold cases)
# ──────────────────────────────────────────────────────────────────────────

def test_utilization_basic_ratio():
    assert cb.utilization(2000, 4096) == pytest.approx(2000 / 4096)


def test_utilization_above_pressure_threshold():
    # 3400 / 4096 ≈ 0.83 > 0.80
    assert cb.utilization(3400, 4096) > 0.80


def test_utilization_below_pressure_threshold():
    # 2000 / 4096 ≈ 0.49 < 0.80
    assert cb.utilization(2000, 4096) < 0.80


def test_utilization_zero_when_ctx_nonpositive():
    assert cb.utilization(2000, 0) == 0.0
    assert cb.utilization(2000, -1) == 0.0


def test_utilization_zero_when_pec_nonpositive():
    assert cb.utilization(0, 4096) == 0.0
    assert cb.utilization(-10, 4096) == 0.0


# ──────────────────────────────────────────────────────────────────────────
# H1 — None-coercion guards (prompt_eval_count=None from Ollama KV-cache)
# ──────────────────────────────────────────────────────────────────────────

def test_is_overflow_signal_none_pec_returns_false():
    # H1: prompt_eval_count=None (Ollama KV-cache hit) must not raise TypeError.
    # is_overflow_signal("", None, 4096, 0.95) must return False without crashing.
    assert cb.is_overflow_signal("", None, 4096, 0.95) is False


def test_utilization_none_pec_returns_zero():
    # H1: utilization(None, 4096) must return 0.0 without crashing.
    assert cb.utilization(None, 4096) == 0.0


# ──────────────────────────────────────────────────────────────────────────
# MEDIUM-1 — use_system_role=False eviction correctness
# ──────────────────────────────────────────────────────────────────────────

def _no_system_shape():
    """Messages in the use_system_role=False shape:
    [ OLD_user, OLD_asst, NEW_user, NEW_asst, COMBINED_current ]

    Sized so the total exceeds the char budget used in tests
    (ctx_limit=4096, max_output_tokens=768, safety_factor=3.5 → budget=11648).
    OLD pair carries 6000 chars each to push the total over the budget.
    """
    return [
        {"role": "user",      "content": "OLD_user"  + "x" * 6000},
        {"role": "assistant", "content": "OLD_asst"  + "x" * 6000},
        {"role": "user",      "content": "NEW_user"  + "x" * 100},
        {"role": "assistant", "content": "NEW_asst"  + "x" * 100},
        {"role": "user",      "content": "COMBINED_current" + "x" * 100},
    ]


def test_apply_char_budget_no_system_role_drops_oldest_pair():
    # MEDIUM-1: with use_system_role=False, messages[0] is an OLD history user
    # turn — it must be evictable. The CURRENT combined turn (index -1) must
    # survive. Today's code pins index 0 and drops indices 1-2 (NEW pair),
    # leaving the stale OLD_user and losing NEW context — this test is RED.
    messages = _no_system_shape()
    combined_ref = messages[-1]  # the object we must keep
    out, evicted = cb.apply_char_budget(
        messages,
        ctx_limit=4096,
        max_output_tokens=768,
        safety_factor=3.5,
    )
    assert evicted >= 1
    contents = [m["content"][:8] for m in out]
    # Oldest pair must be gone.
    assert "OLD_user" not in contents
    assert "OLD_asst" not in contents
    # Newer pair must survive.
    assert "NEW_user" in contents
    assert "NEW_asst" in contents
    # Current combined turn must be preserved (object identity).
    assert out[-1] is combined_ref


def test_trim_messages_reactive_no_system_role_drops_oldest_pair():
    # MEDIUM-1 mirror for trim_messages_reactive: same shape, n_pairs=1.
    messages = _no_system_shape()
    combined_ref = messages[-1]
    removed = cb.trim_messages_reactive(messages, n_pairs=1)
    assert removed == 1
    contents = [m["content"][:8] for m in messages]
    assert "OLD_user" not in contents
    assert "OLD_asst" not in contents
    assert "NEW_user" in contents
    assert "NEW_asst" in contents
    assert messages[-1] is combined_ref


def test_apply_char_budget_system_at_index0_unchanged():
    # Regression: when index 0 IS a system message, behaviour is identical to
    # before — front=1 path, oldest history pair (indices 1-2) is evicted.
    messages = [
        _msg("system", 100),
        _msg("user",   4000),
        _msg("assistant", 4000),
        _msg("user",   4000),
        _msg("assistant", 4000),
        _msg("user",   100),
    ]
    out, evicted = cb.apply_char_budget(
        messages, ctx_limit=4096, max_output_tokens=768, safety_factor=3.5
    )
    assert evicted >= 1
    assert out[0]["role"] == "system"
    assert out[-1]["content"] == "x" * 100


def test_trim_messages_reactive_system_at_index0_unchanged():
    # Regression: system at index 0 still protected after the fix.
    messages = _build_history(3)  # sys + 6 pairs + current
    removed = cb.trim_messages_reactive(messages, n_pairs=2)
    assert removed == 2
    assert messages[0]["role"] == "system"
    assert messages[-1]["content"] == "current"


# ──────────────────────────────────────────────────────────────────────────
# Phase 0 (memoria_recall_20260718 W1) — grown system message safety net
# ──────────────────────────────────────────────────────────────────────────

def test_budget_preserves_grown_w1_system_message_byte_identical():
    # W1 appends the <perfil_streamer> personalization block to the system
    # message, growing it to ~900+ chars (system_prompt + perfil_streamer).
    # Under a tight ctx_limit that forces eviction, index 0 must be preserved
    # BYTE-IDENTICAL (role AND content) — only history pairs may be evicted.
    # This pins apply_char_budget's unconditional system pin (context_budget.py
    # line 126, front=1 branch) against W1's larger system payload. Pre-existing
    # invariant, independent of content size — expected green immediately.
    system_content = "SYSTEM_PROMPT\n\n<perfil_streamer>\n" + ("s" * 900)  # ~933 chars
    system_msg = {"role": "system", "content": system_content}
    messages = [
        system_msg,
        _msg("user", 2000),
        _msg("assistant", 2000),
        _msg("user", 2000),
        _msg("assistant", 2000),
        {"role": "user", "content": "CURRENT" + "x" * 100},
    ]
    # budget = (2048 - 512) * 1.0 = 1536; grown system (~933) + final (~107) fit,
    # so every history pair evicts but the system message must survive intact.
    out, evicted = cb.apply_char_budget(
        messages, ctx_limit=2048, max_output_tokens=512, safety_factor=1.0
    )
    assert evicted >= 1  # history WAS evicted (non-vacuous)
    # Index 0 preserved byte-identical: same object, same role, same content.
    assert out[0] is system_msg
    assert out[0]["role"] == "system"
    assert out[0]["content"] == system_content
    # Final current turn preserved at the end.
    assert out[-1]["content"].startswith("CURRENT")
