"""Strict-TDD tests for the memoria promotion judge's PURE parser + prompt
constant (memory_promotion_20260725, WU2).

`_parse_promotion_decisions` is pure: no I/O, no engine, no model. It turns
whatever the judge actually returned — prose, a markdown fence, half-valid
JSON, an empty string — into a list of applied decisions, and it NEVER raises.
Every malformed shape collapses to "apply nothing", which is what keeps the
sweep's fail-silent contract honest: an unparseable reply must leave every
draft untouched and unjudged for a later eligible sweep.

Tuple contract: (index, text_or_None, uncertain, reason)
  keep   -> (i, "<standalone sentence>", uncertain_bool, "")
  reject -> (i, None, False, "<reason>")
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from opencohost.core.llm_engine import (
    _PROMOTION_JUDGE_PROMPT,
    _PROMOTION_TEXT_MAX_CHARS,
    _parse_promotion_diagnostics,
    _parse_promotion_decisions,
)
from opencohost.core.memory.models import MemoryJudgeResult


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


def test_top_level_schema_requires_decisions_and_forbids_extra_fields() -> None:
    with pytest.raises(ValidationError):
        MemoryJudgeResult.model_validate({})
    with pytest.raises(ValidationError):
        MemoryJudgeResult.model_validate({"decisions": [], "extra": "forbidden"})


def test_parser_runs_the_payload_through_pydantic(monkeypatch) -> None:
    spy = MagicMock(wraps=MemoryJudgeResult.model_validate_json)
    monkeypatch.setattr(MemoryJudgeResult, "model_validate_json", spy)

    result = _parse_promotion_decisions(
        _payload({"i": 1, "keep": False, "reason": "vague"}), 1,
    )

    assert result == [(1, None, False, "vague")]
    spy.assert_called_once()


def test_top_level_extra_field_fails_open_without_applying_entries() -> None:
    text = json.dumps({
        "decisions": [
            {"i": 1, "keep": True, "text": "El streamer usa Ollama en local."},
        ],
        "extra": "forbidden",
    })

    assert _parse_promotion_decisions(text, 1) == []


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


def test_uncertain_must_be_strict_bool_without_losing_valid_siblings() -> None:
    text = _payload(
        {"i": 1, "keep": True, "text": "El streamer mencionó a Luke Oxide.", "uncertain": True},
        {"i": 2, "keep": True, "text": "El streamer usa Piper para TTS.", "uncertain": "yes"},
        {"i": 3, "keep": True, "text": "El streamer transmite de noche."},
    )
    assert _parse_promotion_decisions(text, 3) == [
        (1, "El streamer mencionó a Luke Oxide.", True, ""),
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


def test_duplicate_indexes_are_all_skipped_without_losing_valid_sibling() -> None:
    text = _payload(
        {"i": 1, "keep": True, "text": "primera frase"},
        {"i": 2, "keep": True, "text": "El streamer usa Ollama en local."},
        {"i": 1, "keep": False, "reason": "vague"},
    )
    assert _parse_promotion_decisions(text, 2) == [
        (2, "El streamer usa Ollama en local.", False, ""),
    ]


def test_valid_index_is_suppressed_by_later_schema_invalid_duplicate() -> None:
    text = _payload(
        {"i": 1, "keep": True, "text": "duplicated valid decision"},
        {"i": 1, "keep": "invalid", "text": "schema-invalid duplicate"},
        {"i": 2, "keep": True, "text": "unrelated unique decision"},
    )

    assert _parse_promotion_decisions(text, 2) == [
        (2, "unrelated unique decision", False, ""),
    ]


def test_valid_index_is_suppressed_by_earlier_schema_invalid_duplicate() -> None:
    text = _payload(
        {"i": 1, "keep": "invalid", "text": "schema-invalid duplicate"},
        {"i": 1, "keep": True, "text": "duplicated valid decision"},
        {"i": 2, "keep": True, "text": "unrelated unique decision"},
    )

    assert _parse_promotion_decisions(text, 2) == [
        (2, "unrelated unique decision", False, ""),
    ]


@pytest.mark.parametrize("bad_text", [None, "", "   ", 123, ["a"]])
def test_keep_without_strict_text_is_skipped_without_losing_valid_sibling(
    bad_text,
) -> None:
    entry = {"i": 1, "keep": True}
    if bad_text is not None:
        entry["text"] = bad_text
    text = json.dumps({"decisions": [
        entry,
        {"i": 2, "keep": False, "reason": "trivial"},
    ]})
    assert _parse_promotion_decisions(text, 2) == [
        (2, None, False, "trivial"),
    ]


def test_keep_text_over_the_char_cap_is_skipped_without_losing_valid_sibling() -> None:
    long_text = "a" * (_PROMOTION_TEXT_MAX_CHARS + 1)
    text = _payload(
        {"i": 1, "keep": True, "text": long_text},
        {"i": 2, "keep": False, "reason": "vague"},
    )
    assert _parse_promotion_decisions(text, 2) == [(2, None, False, "vague")]

    at_cap = "b" * _PROMOTION_TEXT_MAX_CHARS
    assert _parse_promotion_decisions(_payload({"i": 1, "keep": True, "text": at_cap}), 1) == [
        (1, at_cap, False, "")
    ]


@pytest.mark.parametrize("reason", ["vague", "speculative", "trivial", "transient", "not_attributable"])
def test_known_reject_reasons_round_trip(reason: str) -> None:
    text = _payload({"i": 1, "keep": False, "reason": reason})
    assert _parse_promotion_decisions(text, 1) == [(1, None, False, reason)]


@pytest.mark.parametrize("reason", [None, ""])
def test_absent_reject_reason_becomes_unspecified(reason) -> None:
    entry = {"i": 1, "keep": False}
    if reason is not None:
        entry["reason"] = reason
    assert _parse_promotion_decisions(json.dumps({"decisions": [entry]}), 1) == [
        (1, None, False, "unspecified")
    ]


@pytest.mark.parametrize("reason", [
    "porque si", 7, "not_self_contained", ["vague", "trivial"], {"r": 1},
])
def test_invalid_reason_is_skipped_without_losing_valid_sibling(reason) -> None:
    text = json.dumps({"decisions": [
        {"i": 1, "keep": False, "reason": reason},
        {"i": 2, "keep": True, "text": "El streamer usa Qwen3 en local."},
    ]})
    assert _parse_promotion_decisions(text, 2) == [
        (2, "El streamer usa Qwen3 en local.", False, ""),
    ]


def test_partially_valid_payload_applies_only_the_valid_entries() -> None:
    text = json.dumps({"decisions": [
        {"i": 1, "keep": True, "text": "El streamer usa Qwen3 en local."},
        {"i": 99, "keep": True, "text": "fuera de rango"},
        {"i": 2, "keep": "maybe", "text": "keep no booleano"},
        {"i": 3, "keep": False, "reason": "transient"},
        {"i": 2, "keep": True, "text": "entrada con campo extra", "extra": True},
    ]})
    assert _parse_promotion_decisions(text, 3) == [
        (1, "El streamer usa Qwen3 en local.", False, ""),
        (3, None, False, "transient"),
    ]


def test_cross_field_violations_never_apply_or_hide_unrelated_valid_entries() -> None:
    text = _payload(
        {"i": 1, "keep": True, "text": "keeper", "reason": "vague"},
        {"i": 2, "keep": False, "text": "rejects cannot carry text", "reason": "vague"},
        {"i": 3, "keep": False, "uncertain": True, "reason": "trivial"},
        {"i": 4, "keep": True, "text": "El streamer usa Ollama en local."},
    )

    assert _parse_promotion_decisions(text, 4) == [
        (4, "El streamer usa Ollama en local.", False, ""),
    ]


# ---------------------------------------------------------------------------
# Phase 3 diagnostic attribution; Phase 1 wrapper remains exact
# ---------------------------------------------------------------------------

def test_diagnostics_preserve_the_phase1_compatibility_projection() -> None:
    text = _payload(
        {"i": 3, "keep": False, "reason": "vague"},
        {"i": 1, "keep": True, "text": "A valid first draft."},
        {"i": 2, "keep": "invalid"},
    )

    diagnostics = _parse_promotion_diagnostics(text, 3)

    assert diagnostics.decisions == _parse_promotion_decisions(text, 3)
    assert diagnostics.decisions == [
        (3, None, False, "vague"),
        (1, "A valid first draft.", False, ""),
    ]


def test_diagnostics_classify_missing_duplicate_and_invalid() -> None:
    text = _payload(
        {"i": 1, "keep": True, "text": "A valid sibling."},
        {"i": 2, "keep": "invalid"},
        {"i": 4, "keep": True, "text": "First duplicate."},
        {"i": 4, "keep": "invalid"},
        {"i": 99, "keep": "invalid"},
    )

    diagnostics = _parse_promotion_diagnostics(text, 4)

    assert diagnostics.top_level_valid is True
    assert diagnostics.decisions == [
        (1, "A valid sibling.", False, ""),
    ]
    assert diagnostics.unresolved == {
        2: "invalid_decision",
        3: "missing_decision",
        4: "duplicate_index",
    }
    assert 99 not in diagnostics.unresolved


@pytest.mark.parametrize("invalid_first", [False, True])
def test_raw_invalid_duplicate_is_diagnostic_in_both_orders(
    invalid_first,
) -> None:
    valid = {"i": 1, "keep": True, "text": "Valid-looking duplicate."}
    invalid = {"i": 1, "keep": "invalid"}
    pair = [invalid, valid] if invalid_first else [valid, invalid]
    text = _payload(*pair, {"i": 2, "keep": False, "reason": "trivial"})

    diagnostics = _parse_promotion_diagnostics(text, 2)

    assert diagnostics.unresolved == {1: "duplicate_index"}
    assert diagnostics.decisions == [(2, None, False, "trivial")]
    assert _parse_promotion_decisions(text, 2) == diagnostics.decisions


@pytest.mark.parametrize("text", [
    "not json",
    "{}",
    '{"decisions":"wrong"}',
    '{"decisions":[],"extra":true}',
])
def test_malformed_top_level_has_no_draft_attribution(text: str) -> None:
    diagnostics = _parse_promotion_diagnostics(text, 3)

    assert diagnostics.top_level_valid is False
    assert diagnostics.decisions == []
    assert diagnostics.unresolved == {}


def test_valid_empty_decision_list_attributes_every_missing_index() -> None:
    diagnostics = _parse_promotion_diagnostics(_payload(), 3)

    assert diagnostics.top_level_valid is True
    assert diagnostics.unresolved == {
        1: "missing_decision",
        2: "missing_decision",
        3: "missing_decision",
    }


def test_out_of_range_entry_never_charges_complete_current_siblings() -> None:
    text = _payload(
        {"i": 1, "keep": False, "reason": "vague"},
        {"i": 2, "keep": False, "reason": "trivial"},
        {"i": 99, "keep": "invalid"},
    )

    diagnostics = _parse_promotion_diagnostics(text, 2)

    assert diagnostics.top_level_valid is True
    assert diagnostics.unresolved == {}
    assert diagnostics.decisions == [
        (1, None, False, "vague"),
        (2, None, False, "trivial"),
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
