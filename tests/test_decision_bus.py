"""laya-decision-layer-20261009 T1: decision types, questions and the DecisionBus.

The bus never imports Laya: every test drives it with an in-process fake
backend. Settings come from the in-memory store (``opencohost.config.decisions``),
reset per test by the autouse ``_isolate_decisions`` fixture in conftest.py.
"""
from __future__ import annotations

import logging

import pytest

from opencohost.config import decisions as dc
from opencohost.core.decision import (
    DECISION_IDS,
    QUESTIONS,
    SOURCES,
    VALUES,
    DecisionBus,
    DecisionOutcome,
)

SECRET = "zanahoriaconfidencial mira la pantalla"


class FakeBackend:
    def __init__(self, answers=None, *, ready=True, response=None):
        self.answers = answers or {}
        self.ready = ready
        self.response = response
        self.calls = []

    def available(self):
        return self.ready

    def ask(self, state, questions, timeout_s):
        self.calls.append({"state": state, "questions": dict(questions), "timeout_s": timeout_s})
        if self.response is not None:
            return self.response
        return {"ok": True, "answers": {q: self.answers[q] for q in questions if q in self.answers}}


def _ans(choice, conf=0.95):
    return {"choice": choice, "answer_confidence": conf}


def _logs(caplog):
    return [r.getMessage() for r in caplog.records if "[DECISION]" in r.getMessage()]


# --- types and questions -----------------------------------------------------


def test_decision_ids_and_values():
    assert DECISION_IDS == ("vision", "memory", "reply_length", "respond", "web", "clock")
    assert VALUES["vision"] == ("look", "no_look")
    assert VALUES["memory"] == ("recall", "skip")
    assert VALUES["reply_length"] == ("short", "medium", "long")
    assert VALUES["respond"] == ("respond", "ignore")
    assert VALUES["web"] == ("search", "no_search")
    assert VALUES["clock"] == ("time", "no_time")


@pytest.mark.parametrize("decision_id", ["vision", "memory", "reply_length", "respond"])
def test_questions_are_laya_choice_schemas(decision_id):
    q = QUESTIONS[decision_id]
    assert q["type"] == "choice"
    assert isinstance(q["instructions"], str) and q["instructions"]
    assert tuple(q["criteria"]) == VALUES[decision_id]
    assert all(isinstance(v, str) and v for v in q["criteria"].values())


def test_sources():
    assert SOURCES["vision"] == frozenset({"direct", "ptt"})
    assert SOURCES["memory"] == frozenset({"direct", "ptt", "owner-bundle"})
    assert SOURCES["reply_length"] == frozenset({"direct", "ptt", "owner-bundle"})
    assert SOURCES["respond"] == frozenset({"chat"})


def test_outcome_dict_has_no_text():
    o = DecisionOutcome(
        id="vision", mode="shadow", rule_value="no_look", laya_value="look",
        confidence=0.9, acted_by="rule", latency_ms=12, status="ok",
    )
    assert o.value == "no_look"
    assert o.to_dict() == {
        "id": "vision", "mode": "shadow", "rule_value": "no_look", "laya_value": "look",
        "confidence": 0.9, "acted_by": "rule", "latency_ms": 12, "status": "ok", "reason": "",
    }


# --- bus: who gets asked -----------------------------------------------------


def test_no_backend_returns_none_and_logs_once(caplog):
    caplog.set_level(logging.INFO, logger="OpenCohost")
    bus = DecisionBus(None)
    assert bus.decide("hola", "direct") is None
    assert bus.decide("hola", "direct") is None
    lines = _logs(caplog)
    assert len(lines) == 1 and "unavailable" in lines[0]


def test_backend_not_ready_returns_none_without_asking():
    backend = FakeBackend(ready=False)
    assert DecisionBus(backend).decide("hola", "direct") is None
    assert backend.calls == []


def test_all_off_returns_none_without_asking(caplog):
    caplog.set_level(logging.INFO, logger="OpenCohost")
    for d in DECISION_IDS:
        dc.set_decision(d, mode="off")
    backend = FakeBackend()
    assert DecisionBus(backend).decide("hola", "direct") is None
    assert backend.calls == []
    assert _logs(caplog) == []


def test_one_backend_call_with_only_active_applicable_questions():
    dc.set_decision("memory", mode="off")
    dc.set_decision("web", mode="off")
    dc.set_decision("clock", mode="off")
    backend = FakeBackend({"vision": _ans("look"), "reply_length": _ans("short")})
    turn = DecisionBus(backend, budget_ms=lambda: 600).decide("hola", "direct")
    assert len(backend.calls) == 1
    call = backend.calls[0]
    assert set(call["questions"]) == {"vision", "reply_length"}
    assert call["questions"]["vision"] == QUESTIONS["vision"]
    assert call["state"] == "hola"
    assert call["timeout_s"] == pytest.approx(0.6)
    assert turn.asked("vision") and turn.asked("reply_length")
    assert not turn.asked("memory") and not turn.asked("respond")


def test_ids_filter_and_source_filter():
    backend = FakeBackend({"vision": _ans("look")})
    bus = DecisionBus(backend)
    assert bus.decide("hola", "chat", ids=("vision", "memory")) is None  # no source match
    turn = bus.decide("hola", "ptt", ids=("vision",))
    assert set(backend.calls[-1]["questions"]) == {"vision"}
    assert turn.asked("vision")


def test_respond_on_is_clamped_to_shadow():
    dc._install_entry("respond", "on", 0.8)  # bypass validation: a hand-edited file
    backend = FakeBackend({"respond": _ans("ignore", 0.99)})
    turn = DecisionBus(backend).decide("hola", "chat")
    out = turn.resolve("respond", "respond")
    assert out.mode == "shadow"
    assert out.acted_by == "rule" and out.value == "respond"


# --- resolve: mode and threshold --------------------------------------------


def _turn(answers, **modes):
    for d, m in modes.items():
        dc.set_decision(d, mode=m)
    return DecisionBus(FakeBackend(answers)).decide("hola", "direct")


def test_shadow_logs_but_rule_acts(caplog):
    caplog.set_level(logging.INFO, logger="OpenCohost")
    turn = _turn({"vision": _ans("look", 0.97)}, vision="shadow")
    out = turn.resolve("vision", "no_look")
    assert (out.acted_by, out.value, out.status, out.laya_value) == ("rule", "no_look", "ok", "look")
    line = [l for l in _logs(caplog) if "id=vision" in l][0]
    for part in ("mode=shadow", "rule=no_look", "laya=look", "conf=0.97", "acted_by=rule", "status=ok"):
        assert part in line
    assert "latency_ms=" in line


def test_on_confident_laya_acts():
    turn = _turn({"vision": _ans("look", 0.9)}, vision="on")
    out = turn.resolve("vision", "no_look")
    assert (out.acted_by, out.value, out.status) == ("laya", "look", "ok")


def test_on_low_confidence_rule_acts():
    dc.set_decision("vision", mode="on", threshold=0.95)
    turn = DecisionBus(FakeBackend({"vision": _ans("look", 0.9)})).decide("hola", "direct")
    out = turn.resolve("vision", "no_look")
    assert (out.acted_by, out.value, out.status) == ("rule", "no_look", "low_confidence")


def test_on_agreeing_answer_is_acted_by_rule():
    turn = _turn({"vision": _ans("look", 0.99)}, vision="on")
    out = turn.resolve("vision", "look")
    assert (out.acted_by, out.value) == ("rule", "look")


def test_on_not_allowed_value_is_skipped():
    turn = _turn({"memory": _ans("recall", 0.99)}, memory="on")
    out = turn.resolve("memory", "skip", allowed=("skip",))
    assert (out.acted_by, out.value, out.status, out.reason) == ("rule", "skip", "skipped", "not_allowed")


def test_confidence_fallbacks():
    answers = {
        "vision": {"choice": "look", "confidence": 0.91},
        "memory": {"choice": "skip", "probabilities": {"recall": 0.2, "skip": 0.8}},
        "reply_length": {"choice": "long"},
    }
    turn = _turn(answers, vision="on", memory="on", reply_length="on")
    assert turn.resolve("vision", "no_look").confidence == pytest.approx(0.91)
    assert turn.resolve("memory", "recall").confidence == pytest.approx(0.8)
    out = turn.resolve("reply_length", "medium")
    assert out.confidence is None and out.status == "low_confidence" and out.acted_by == "rule"


def test_bad_choice_is_error_and_rule_acts():
    turn = _turn({"vision": {"choice": "banana", "answer_confidence": 0.99}}, vision="on")
    out = turn.resolve("vision", "no_look")
    assert (out.acted_by, out.status, out.laya_value) == ("rule", "error", None)


def test_backend_none_is_timeout_and_logs(caplog):
    caplog.set_level(logging.INFO, logger="OpenCohost")
    backend = FakeBackend()
    backend.ask = lambda *a, **k: None
    turn = DecisionBus(backend).decide("hola", "direct")
    out = turn.resolve("vision", "look")
    assert (out.status, out.acted_by, out.value) == ("timeout", "rule", "look")
    assert any("status=timeout" in l for l in _logs(caplog))


def test_backend_error_response_is_error():
    backend = FakeBackend(response={"ok": False, "error": "predict_failed"})
    out = DecisionBus(backend).decide("hola", "direct").resolve("memory", "recall")
    assert (out.status, out.reason, out.acted_by) == ("error", "predict_failed", "rule")


def test_backend_exception_never_escapes():
    backend = FakeBackend()

    def boom(*a, **k):
        raise RuntimeError("x")

    backend.ask = boom
    out = DecisionBus(backend).decide("hola", "direct").resolve("memory", "recall")
    assert out.status == "error" and out.acted_by == "rule"


def test_resolve_unasked_returns_none():
    dc.set_decision("memory", mode="off")
    turn = DecisionBus(FakeBackend({"vision": _ans("look")})).decide("hola", "direct")
    assert turn.resolve("memory", "recall") is None


def test_resolve_is_cached_and_logged_once(caplog):
    caplog.set_level(logging.INFO, logger="OpenCohost")
    turn = _turn({"vision": _ans("look")}, vision="shadow")
    first = turn.resolve("vision", "no_look")
    assert turn.resolve("vision", "no_look") is first
    assert len([l for l in _logs(caplog) if "id=vision" in l]) == 1
    assert [o["id"] for o in turn.outcomes()] == ["vision"]


def test_log_never_carries_the_state(caplog):
    caplog.set_level(logging.DEBUG)
    turn = DecisionBus(FakeBackend({d: _ans(VALUES[d][0]) for d in DECISION_IDS})).decide(SECRET, "direct")
    for d in ("vision", "memory", "reply_length"):
        turn.resolve(d, VALUES[d][1])
    assert all("zanahoria" not in r.getMessage() for r in caplog.records)
