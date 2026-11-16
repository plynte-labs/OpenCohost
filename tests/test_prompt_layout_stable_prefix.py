"""T5 (memory-grounding-and-recall-budget-20261016): ADR-064 guard.

The memory block (facts, episodes, the T4 no-match note) lives in the volatile
turn context. The v2 system message, the stable prefix, must stay byte-identical
across turns whose memory blocks differ, including a no-match-note turn.
"""
from __future__ import annotations

import pytest

from opencohost.i18n import active as i18n_active
from tests.test_memory_recall_merge import FACT_LEXICAL, QUERY, _block_lines, _make_motor
from tests.test_prompt_layout_v2 import _assemble, _final, _sys

PROFILE = "p-stable"


def _builder_returning(text):
    return lambda profile_id, contexto: text


def _wrap(*lines):
    return (
        i18n_active.memorias_block_open() + "\n" + "\n".join(lines) + "\n"
        + i18n_active.memorias_block_close()
    )


def _turn(text, block):
    return _assemble(
        text,
        memorias_enabled=True,
        memorias_profile_id=PROFILE,
        memorias_builder=_builder_returning(block),
    )


def test_stable_prefix_unchanged_across_memory_blocks():
    note = _wrap(
        i18n_active.memorias_no_match_line().format(topic="zqtesla"),
        i18n_active.memorias_no_match_rule(),
    )
    turns = [
        _turn("hola", ""),
        _turn("recuerdas zqtesla", note),
        _turn("recuerdas zqorbit", _wrap("el streamer fotografia nebulosas", "otro dato " * 20)),
    ]
    systems = [_sys(t) for t in turns]

    assert systems[0] == systems[1] == systems[2]
    # The varying memory lives after the stable system message, in the turn context.
    assert "zqtesla" not in systems[1]
    assert i18n_active.memorias_no_match_rule() in _final(turns[1])
    assert i18n_active.memorias_no_match_rule() not in _final(turns[0])
    assert _final(turns[0]) != _final(turns[1]) != _final(turns[2])


def test_no_match_rule_text_is_not_part_of_any_system_message():
    note = _wrap(
        i18n_active.memorias_no_match_line().format(topic="zqtesla"),
        i18n_active.memorias_no_match_rule(),
    )
    with_note = _turn("recuerdas zqtesla", note)
    assert i18n_active.memorias_no_match_rule() not in _sys(with_note)


def test_real_builder_note_and_match_turns_share_one_system_message(monkeypatch, tmp_path):
    import logging  # noqa: F401

    rows = [{"id": "lex", "content": FACT_LEXICAL, "signature": ""}]
    motor = _make_motor(monkeypatch, tmp_path, rows)
    motor.use_system_role = True
    motor.current_model = "llama3"

    def build(text):
        return motor._build_generation_request(
            text, source="direct", is_local=True, provider_cfg={},
            request_model="m", watchdog_timeout=10.0,
        )

    miss = build("recuerdas zqtesla zqmodelo")
    plain = build("hola que tal")
    assert _block_lines(_final(miss))
    assert miss.messages[0]["content"] == plain.messages[0]["content"]
    assert i18n_active.memorias_no_match_rule() not in miss.messages[0]["content"]
