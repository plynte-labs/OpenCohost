"""Strict-TDD tests for the T1 deterministic post-parse guard
(memory-noise-amplification-20260928): a judge KEEP whose own rewritten TEXT
has Kira/the assistant as its grammatical subject is downgraded to a REJECT
(``assistant_sourced``), even when the judge itself answered keep:true.

All strings below are synthetic — never real captured rows.
"""

from __future__ import annotations

import json

import pytest

from opencohost.core.memory.promotion_parser import (
    _ASSISTANT_SUBJECT_RE,
    _apply_assistant_subject_guard,
    _parse_promotion_decisions,
    _parse_promotion_diagnostics,
)


def _payload(*decisions) -> str:
    return json.dumps({"decisions": list(decisions)})


# ---------------------------------------------------------------------------
# The guard fires: assistant-as-subject rewrites are downgraded
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "Soy Kira, la copresentadora del stream.",
    "I am Kira, the co-host of this stream.",
    "Kira dijo que el streamer deberia dormir mas.",
    "Kira recomienda usar teclados mecanicos.",
    "Kira es una inteligencia artificial creada para acompañar streams.",
    "Kira states that the operator should take a break.",
    "The assistant recommends switching microphones.",
    "La asistente afirma que el juego es dificil.",
    "El asistente dijo que hacia calor.",
    "I'm Kira and I like synthwave.",
])
def test_assistant_subject_keep_is_downgraded_to_reject(text: str) -> None:
    decisions = _parse_promotion_decisions(
        _payload({"i": 1, "keep": True, "text": text}), 1,
    )
    assert decisions == [(1, None, False, "assistant_sourced")]


def test_guard_only_touches_the_matching_entry_not_its_siblings() -> None:
    text = _payload(
        {"i": 1, "keep": True, "text": "Soy Kira y me gusta el synthwave."},
        {"i": 2, "keep": True, "text": "El streamer juega Hollow Knight los martes."},
        {"i": 3, "keep": False, "reason": "vague"},
    )
    decisions = _parse_promotion_decisions(text, 3)
    assert decisions == [
        (1, None, False, "assistant_sourced"),
        (2, "El streamer juega Hollow Knight los martes.", False, ""),
        (3, None, False, "vague"),
    ]


def test_guard_downgrades_an_uncertain_keep_too() -> None:
    """uncertain is only meaningful on a keep; a downgraded entry carries none."""
    text = _payload({"i": 1, "keep": True, "text": "Kira es la mejor co-host.", "uncertain": True})
    assert _parse_promotion_decisions(text, 1) == [(1, None, False, "assistant_sourced")]


def test_diagnostics_reflect_the_guard_and_drop_the_origin() -> None:
    text = _payload(
        {"i": 1, "keep": True, "text": "Soy Kira.", "origin": "self_stated"},
        {"i": 2, "keep": True, "text": "El streamer usa Ollama en local.", "origin": "self_stated"},
    )
    diagnostics = _parse_promotion_diagnostics(text, 2)
    assert diagnostics.decisions == [
        (1, None, False, "assistant_sourced"),
        (2, "El streamer usa Ollama en local.", False, ""),
    ]
    assert diagnostics.origins == {2: "self_stated"}


# ---------------------------------------------------------------------------
# The guard does NOT fire: Kira as an object, or unrelated keepers
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "El operador creo a Kira para su stream.",
    "El streamer configuro a Kira para hablar mas rapido.",
    "El operador eligio el nombre Kira para su asistente de IA.",
    "El streamer prefiere Hollow Knight Silksong los martes.",
    "The operator built Kira for his stream using Ollama.",
])
def test_kira_as_object_is_never_downgraded(text: str) -> None:
    decisions = _parse_promotion_decisions(
        _payload({"i": 1, "keep": True, "text": text}), 1,
    )
    assert decisions == [(1, text, False, "")]


def test_operator_opinion_about_kira_is_also_downgraded_by_design() -> None:
    """Parent review (2026-09-28): "El operador dijo que Kira es graciosa" is
    an OPERATOR opinion ABOUT Kira, not Kira's own words -- and it is still
    downgraded, on purpose. An operator's passing opinion about the co-host
    is low value, and NOT catching this would mean relaxing the "Kira es"
    alternative itself, which is exactly what must fire on Kira's own
    self-description ("Kira es una inteligencia artificial..."). The regex is
    intentionally not narrowed for this case."""
    decisions = _parse_promotion_decisions(
        _payload({"i": 1, "keep": True, "text": "El operador dijo que Kira es graciosa."}), 1,
    )
    assert decisions == [(1, None, False, "assistant_sourced")]


def test_reject_entries_pass_through_untouched() -> None:
    decisions = [(1, None, False, "vague"), (2, "keeper", False, "")]
    assert _apply_assistant_subject_guard(decisions) == [
        (1, None, False, "vague"), (2, "keeper", False, ""),
    ]


def test_assistant_subject_regex_is_case_insensitive() -> None:
    assert _ASSISTANT_SUBJECT_RE.search("SOY KIRA Y ME GUSTA ESTO")
    assert not _ASSISTANT_SUBJECT_RE.search("el operador creo a kira para su stream")


# ---------------------------------------------------------------------------
# End-to-end: the sweep never promotes an assistant-sourced keep
# ---------------------------------------------------------------------------

def test_sweep_never_promotes_an_assistant_sourced_keep(monkeypatch, tmp_path):
    from tests.test_memoria_promotion import _Recorder, _decisions, _make_motor, _row, _seed_draft, _store

    motor = _make_motor(monkeypatch, tmp_path)
    row_id = _seed_draft(
        _store(tmp_path), "profile-1", "k1",
        "streamer: le pregunto a Kira quien es ella",
    )
    stub = _Recorder(_decisions(
        {"i": 1, "keep": True, "text": "Soy Kira, la co-host de este stream.",
         "origin": "shared_content"},
    ))

    counts = motor.promote_pending_drafts(chat_callable=stub)

    row = _row(tmp_path, row_id)
    assert counts["kept"] == 0
    assert counts["rejected"] == 1
    assert counts["reasons"]["assistant_sourced"] == 1
    assert row["status"] == "draft"
    assert row["inactive"] == 1
    assert row["content"] == "" and row["title"] == ""
