"""Unit tests for the ctx-derived profile max_turns cap (vram-aware-context-budget T4)."""

import pytest

from opencohost.core.context.context_budget import max_turns_cap_for_ctx


@pytest.mark.parametrize(
    "ctx, max_output, expected",
    [
        (4096, 768, 10),  # default ctx keeps today's hard cap
        (4096, 2048, 10),  # reasoning reserve: formula gives 5, floor wins
        (16384, 2048, 40),  # (16384 - 2048) // 350 = 40
        (16384, 768, 40),  # 44 → ceiling
        (8192, 768, 21),  # (8192 - 768) // 350 = 21
        (131072, 2048, 40),  # huge ctx never exceeds the ceiling
    ],
)
def test_cap_follows_formula(ctx, max_output, expected):
    assert max_turns_cap_for_ctx(ctx, max_output=max_output) == expected


@pytest.mark.parametrize("ctx", [None, 0, -1, "junk"])
def test_unknown_ctx_falls_back_to_floor(ctx):
    assert max_turns_cap_for_ctx(ctx, max_output=768) == 10


def test_output_reserve_larger_than_ctx_falls_back_to_floor():
    assert max_turns_cap_for_ctx(2048, max_output=4096) == 10
