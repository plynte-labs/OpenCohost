"""laya-decision-layer-20261009 T3: how each consumer applies a decision.

Vision precedence, memory skip-only and reply length auto-only, driven by a
real DecisionBus over an in-process fake backend.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from opencohost.config import decisions as dc
from opencohost.core.decision import DecisionBus
from opencohost.core.decision.consumers import (
    apply_reply_length,
    apply_vision_decision,
    memory_recall_skipped,
)
from opencohost.core.vision.gate import CompositeGate, ExplicitTriggerGate, RuleVisionGate
from opencohost.core.vision.turn_step import VisionTurnStep


class FakeBackend:
    def __init__(self, answers):
        self.answers = answers

    def ask(self, state, questions, timeout_s):
        return {"ok": True, "answers": {q: self.answers[q] for q in questions if q in self.answers}}


def _turn(decision_id, choice, conf=0.95, mode="on", threshold=0.8, source="direct"):
    dc.set_decision(decision_id, mode=mode, threshold=threshold)
    backend = FakeBackend({decision_id: {"choice": choice, "answer_confidence": conf}})
    return DecisionBus(backend).decide("x", source, ids=(decision_id,))


def _gate(look_on_ptt=False, rule=True, delay=1.5):
    explicit = ExplicitTriggerGate(look_on_ptt, delay)
    return explicit, CompositeGate(explicit, RuleVisionGate(delay) if rule else None)


def _apply(gate, text, source, turn):
    decision = gate.decide(text, source)
    return decision, apply_vision_decision(decision, turn, source=source, rule_gate=gate.secondary)


# --- vision ------------------------------------------------------------------


def test_vision_none_turn_returns_the_same_decision():
    _, gate = _gate()
    decision = gate.decide("hola", "direct")
    assert apply_vision_decision(decision, None, source="direct", rule_gate=gate.secondary) is decision


def test_vision_on_confident_look_turns_rule_no_into_look():
    _, gate = _gate()
    rule, out = _apply(gate, "cuéntame algo del juego", "direct", _turn("vision", "look"))
    assert rule.needs_vision is False
    assert out.needs_vision is True
    assert out.reason_codes == ("no_trigger", "laya_look")
    assert out.capture_delay_s == 1.5


def test_vision_on_ptt_look_has_no_delay():
    _, gate = _gate()
    _, out = _apply(gate, "cuéntame algo del juego", "ptt", _turn("vision", "look"))
    assert out.needs_vision is True and out.capture_delay_s == 0.0


def test_vision_shadow_only_logs():
    _, gate = _gate()
    turn = _turn("vision", "look", mode="shadow")
    rule, out = _apply(gate, "cuéntame algo del juego", "direct", turn)
    assert out is rule
    assert turn.resolve("vision", "no_look").acted_by == "rule"


def test_vision_low_confidence_keeps_rule():
    _, gate = _gate()
    rule, out = _apply(gate, "cuéntame algo", "direct", _turn("vision", "look", conf=0.6))
    assert out is rule


def test_vision_suppressor_wins_over_laya():
    _, gate = _gate()
    turn = _turn("vision", "look", conf=0.99)
    rule, out = _apply(gate, "mira esto, ayer jugué", "direct", turn)  # suppressed_past
    assert any(c.startswith("suppressed_") for c in rule.reason_codes)
    assert out is rule
    assert turn.resolve("vision", "no_look").status == "skipped"


def test_vision_explicit_look_wins_over_laya_no():
    explicit, gate = _gate()
    explicit.arm_look()
    rule, out = _apply(gate, "hola", "direct", _turn("vision", "no_look", conf=0.99))
    assert rule.needs_vision is True and out is rule


def test_vision_ptt_switch_wins_over_laya_no():
    _, gate = _gate(look_on_ptt=True)
    rule, out = _apply(gate, "hola", "ptt", _turn("vision", "no_look", conf=0.99))
    assert rule.reason_codes == ("ptt_switch",) and out is rule


@pytest.mark.parametrize("text", ["mira esto", "qué hay en la pantalla"])
def test_vision_requested_rule_positive_is_protected(text):
    _, gate = _gate()
    rule, out = _apply(gate, text, "direct", _turn("vision", "no_look", conf=0.99))
    assert rule.needs_vision is True and out is rule


def test_vision_non_requested_rule_positive_can_be_turned_off():
    _, gate = _gate()
    rule, out = _apply(gate, "qué es eso", "direct", _turn("vision", "no_look", conf=0.99))
    assert rule.needs_vision is True and "deictic_query" in rule.reason_codes
    assert out.needs_vision is False
    assert out.reason_codes == (*rule.reason_codes, "laya_skip")


def test_vision_rule_gate_disabled_blocks_laya_look():
    _, gate = _gate(rule=False)
    rule, out = _apply(gate, "cuéntame algo", "direct", _turn("vision", "look", conf=0.99))
    assert out is rule and out.needs_vision is False


def test_vision_step_applies_the_decision():
    _, gate = _gate()
    step = VisionTurnStep(
        gate, None, None, lambda line: None, settings=SimpleNamespace(describe_timeout_s=5.0)
    )
    plain = step.run("cuéntame algo del juego", "direct")
    assert plain.outcome == "no_vision"
    looked = step.run("cuéntame algo del juego", "direct", decisions=_turn("vision", "look"))
    assert looked.outcome == "capture_unavailable"
    assert "laya_look" in looked.reason_codes


# --- memory ------------------------------------------------------------------


def test_memory_none_never_skips():
    assert memory_recall_skipped(None) is False


def test_memory_on_confident_skip_skips():
    assert memory_recall_skipped(_turn("memory", "skip")) is True


@pytest.mark.parametrize(
    "kwargs", [dict(mode="shadow"), dict(conf=0.5), dict(choice="recall")]
)
def test_memory_otherwise_recalls(kwargs):
    args = {"choice": "skip", **kwargs}
    choice = args.pop("choice")
    assert memory_recall_skipped(_turn("memory", choice, **args)) is False


def test_memory_unasked_recalls():
    dc.set_decision("memory", mode="off")
    assert memory_recall_skipped(_turn("vision", "look")) is False


# --- reply length --------------------------------------------------------------


@pytest.mark.parametrize("reason", ["brief_input", "default", "depth_request"])
def test_reply_length_auto_choices_can_be_replaced(reason):
    turn = _turn("reply_length", "long")
    level = {"brief_input": "short", "default": "medium", "depth_request": "long"}[reason]
    out_level, out_reason = apply_reply_length(turn, level, reason)
    if level == "long":
        assert (out_level, out_reason) == ("long", reason)
    else:
        assert (out_level, out_reason) == ("long", "laya")


@pytest.mark.parametrize(
    "level,reason", [("short", "setting"), ("very_long", "profile_extended"), ("medium", "vision")]
)
def test_reply_length_fixed_extended_and_vision_are_untouched(level, reason):
    turn = _turn("reply_length", "long")
    assert apply_reply_length(turn, level, reason) == (level, reason)
    assert turn.outcomes() == []  # not eligible: never resolved, never logged


def test_reply_length_shadow_and_none():
    assert apply_reply_length(None, "short", "brief_input") == ("short", "brief_input")
    turn = _turn("reply_length", "long", mode="shadow")
    assert apply_reply_length(turn, "short", "brief_input") == ("short", "brief_input")
    assert turn.outcomes()[0]["laya_value"] == "long"
