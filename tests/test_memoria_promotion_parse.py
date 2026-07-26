"""Strict-TDD tests for the memoria promotion judge's PURE parser + prompt
constant (memory_promotion_20260725, WU2).

`_parse_promotion_decisions` is pure: no I/O, no engine, no model. It turns
whatever the judge actually returned — prose, a markdown fence, half-valid
JSON, an empty string — into a list of applied decisions, and it NEVER raises.
Every malformed shape collapses to "apply nothing", which is what keeps the
sweep's fail-silent contract honest: an unparseable reply must leave every
draft untouched and unjudged for the next launch.

Tuple contract: (index, text_or_None, uncertain, reason)
  keep   -> (i, "<standalone sentence>", uncertain_bool, "")
  reject -> (i, None, False, "<reason>")
"""

from __future__ import annotations

import json

import pytest

from opencohost.core.llm_engine import (
    _PROMOTION_JUDGE_PROMPT,
    _PROMOTION_TEXT_MAX_CHARS,
    _parse_promotion_decisions,
)


def _payload(*decisions) -> str:
    return json.dumps({"decisions": list(decisions)})


# ---------------------------------------------------------------------------
# Garbage in -> nothing applied (the fail-silent contract)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "",
    "   ",
    "Claro, aca van mis decisiones:",
    "{not json at all",
    "[]",
    "null",
    '{"decisions": "not a list"}',
    '{"decisions": {"i": 1}}',
    '{"otro": [{"i": 1, "keep": true, "text": "algo"}]}',
    '"a bare json string"',
    "42",
])
def test_unusable_reply_yields_no_decisions(text: str) -> None:
    assert _parse_promotion_decisions(text, 3) == []


def test_none_text_yields_no_decisions() -> None:
    assert _parse_promotion_decisions(None, 3) == []


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------

def test_valid_keep_and_reject_are_parsed_in_order() -> None:
    text = _payload(
        {"i": 1, "keep": True, "text": "El streamer juega Hollow Knight Silksong los martes."},
        {"i": 2, "keep": False, "reason": "vague"},
    )
    assert _parse_promotion_decisions(text, 2) == [
        (1, "El streamer juega Hollow Knight Silksong los martes.", False, ""),
        (2, None, False, "vague"),
    ]


def test_markdown_json_fence_is_stripped() -> None:
    inner = _payload({"i": 1, "keep": True, "text": "El streamer usa Ollama en local."})
    for fenced in (f"```json\n{inner}\n```", f"```\n{inner}\n```", f"  ```json\n{inner}\n```  "):
        assert _parse_promotion_decisions(fenced, 1) == [
            (1, "El streamer usa Ollama en local.", False, "")
        ]


def test_keep_text_is_whitespace_collapsed() -> None:
    text = _payload({"i": 1, "keep": True, "text": "  El streamer   prefiere\n  synthwave.  "})
    assert _parse_promotion_decisions(text, 1) == [
        (1, "El streamer prefiere synthwave.", False, "")
    ]


def test_uncertain_true_is_carried_and_non_bool_uncertain_is_false() -> None:
    text = _payload(
        {"i": 1, "keep": True, "text": "El streamer mencionó a Luke Oxide.", "uncertain": True},
        {"i": 2, "keep": True, "text": "El streamer usa Piper para TTS.", "uncertain": "yes"},
        {"i": 3, "keep": True, "text": "El streamer transmite de noche."},
    )
    assert _parse_promotion_decisions(text, 3) == [
        (1, "El streamer mencionó a Luke Oxide.", True, ""),
        (2, "El streamer usa Piper para TTS.", False, ""),
        (3, "El streamer transmite de noche.", False, ""),
    ]


# ---------------------------------------------------------------------------
# Per-entry validation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad_i", [0, 4, -1, "1", 1.0, None, True])
def test_index_outside_one_to_batch_len_is_skipped(bad_i) -> None:
    text = _payload({"i": bad_i, "keep": True, "text": "una frase autocontenida"})
    assert _parse_promotion_decisions(text, 3) == []


@pytest.mark.parametrize("bad_keep", ["true", 1, 0, None])
def test_non_bool_keep_is_skipped(bad_keep) -> None:
    text = _payload({"i": 1, "keep": bad_keep, "text": "una frase autocontenida"})
    assert _parse_promotion_decisions(text, 3) == []


def test_non_dict_entries_are_skipped() -> None:
    text = json.dumps({"decisions": ["nope", 7, None, {"i": 1, "keep": False, "reason": "trivial"}]})
    assert _parse_promotion_decisions(text, 2) == [(1, None, False, "trivial")]


def test_duplicate_index_first_wins() -> None:
    text = _payload(
        {"i": 1, "keep": True, "text": "primera frase"},
        {"i": 1, "keep": False, "reason": "vague"},
    )
    assert _parse_promotion_decisions(text, 2) == [(1, "primera frase", False, "")]


@pytest.mark.parametrize("bad_text", [None, "", "   ", 123, ["a"]])
def test_keep_without_usable_text_becomes_a_not_self_contained_reject(bad_text) -> None:
    """Criterion 2 answered in the negative: a judge that cannot produce a
    standalone sentence has rejected the item, not errored."""
    entry = {"i": 1, "keep": True}
    if bad_text is not None:
        entry["text"] = bad_text
    assert _parse_promotion_decisions(json.dumps({"decisions": [entry]}), 1) == [
        (1, None, False, "not_self_contained")
    ]


def test_keep_text_over_the_char_cap_becomes_a_not_self_contained_reject() -> None:
    long_text = "a" * (_PROMOTION_TEXT_MAX_CHARS + 1)
    text = _payload({"i": 1, "keep": True, "text": long_text})
    assert _parse_promotion_decisions(text, 1) == [(1, None, False, "not_self_contained")]

    at_cap = "b" * _PROMOTION_TEXT_MAX_CHARS
    assert _parse_promotion_decisions(_payload({"i": 1, "keep": True, "text": at_cap}), 1) == [
        (1, at_cap, False, "")
    ]


@pytest.mark.parametrize("reason", ["vague", "speculative", "trivial", "transient", "not_attributable"])
def test_known_reject_reasons_round_trip(reason: str) -> None:
    text = _payload({"i": 1, "keep": False, "reason": reason})
    assert _parse_promotion_decisions(text, 1) == [(1, None, False, reason)]


@pytest.mark.parametrize("reason", [
    None, "", "porque si", 7, "not_self_contained",
    # UNHASHABLE: `reason in frozenset(...)` raises TypeError on these, and the
    # exception escapes the "NEVER raises" contract all the way to the sweep's
    # outer catch-all — discarding every OTHER valid decision in the batch and
    # re-paying the identical inference next launch.
    ["vague", "trivial"], {"r": 1},
])
def test_unknown_or_absent_reject_reason_becomes_unspecified(reason) -> None:
    entry = {"i": 1, "keep": False}
    if reason is not None:
        entry["reason"] = reason
    assert _parse_promotion_decisions(json.dumps({"decisions": [entry]}), 1) == [
        (1, None, False, "unspecified")
    ]


def test_partially_valid_payload_applies_only_the_valid_entries() -> None:
    text = json.dumps({"decisions": [
        {"i": 1, "keep": True, "text": "El streamer usa Qwen3 en local."},
        {"i": 99, "keep": True, "text": "fuera de rango"},
        {"i": 2, "keep": "maybe", "text": "keep no booleano"},
        {"i": 3, "keep": False, "reason": "transient"},
    ]})
    assert _parse_promotion_decisions(text, 3) == [
        (1, "El streamer usa Qwen3 en local.", False, ""),
        (3, None, False, "transient"),
    ]


# ---------------------------------------------------------------------------
# The prompt constant
# ---------------------------------------------------------------------------

def test_prompt_carries_the_draft_block_placeholder_only() -> None:
    assert "{draft_block}" in _PROMOTION_JUDGE_PROMPT
    # Step 4 dedups arithmetically via stable_key — the judge is never asked to.
    assert "{existing_block}" not in _PROMOTION_JUDGE_PROMPT


def test_prompt_keeps_the_self_contained_criterion_and_the_uncertain_field() -> None:
    lowered = _PROMOTION_JUDGE_PROMPT.lower()
    assert "self-contained" in lowered
    assert "uncertain" in lowered


def test_prompt_no_longer_rejects_names_that_appear_only_once() -> None:
    """Owner decision 4: a game/model/person name essentially never repeats
    inside a <=24-word capture window, so the old single-occurrence rule would
    have rejected most of what a co-host needs to remember."""
    lowered = _PROMOTION_JUDGE_PROMPT.lower()
    assert "not a reason to reject" in lowered
    assert "only once" in lowered
    for banned in ("appearing exactly once", "appears exactly once", "repeated by the operator"):
        assert banned not in lowered
