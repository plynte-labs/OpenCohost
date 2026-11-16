"""clock-and-compact-polish-20261010 T1: the ``clock`` decision.

The pure rule (``core/context/clock.py``) detects explicit time/date asks;
Laya may only turn a rule ``no_time`` into ``time`` (mode ``on``, confidence
at or above the threshold). Owner voice sources only (``direct``, ``ptt``),
default mode ``on``. Laya is asked inside the turn's single DecisionBus call,
and only when a loose lexical pre-check matches, so a plain turn never asks.
"""
from __future__ import annotations

import logging
import queue
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from opencohost.config import decisions as dc
from opencohost.config import settings
from opencohost.core import llm_engine
from opencohost.core.context import clock
from opencohost.core.decision import runtime as decision_runtime
from opencohost.core.decision.bus import DecisionBus
from opencohost.core.decision.consumers import apply_clock_decision
from opencohost.core.decision.questions import QUESTIONS, SOURCES, VALUES
from opencohost.core.decision.types import CLOCK, DECISION_IDS


# --- the rule -----------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "¿Qué hora es?",
        "oye kira, que horas son",
        "¿Qué día es hoy?",
        "qué día es",
        "¿A cuántos estamos?",
        "¿qué fecha es hoy?",
        "¿En qué año estamos?",
        "What time is it?",
        "what day is it today",
        "What's today's date?",
        "dime la hora porfa",
    ],
)
def test_rule_detects_explicit_time_asks(text):
    assert clock.clock_rule(text) == clock.TIME


@pytest.mark.parametrize(
    "text",
    [
        "hola",
        "ahora sí, cuéntame del juego",
        "qué opinas del juego de hoy",
        "explícame cómo funciona el mapa en detalle",
        "es hora de jugar",
        "buen día kira",
        "",
        None,
    ],
)
def test_rule_ignores_everything_else(text):
    assert clock.clock_rule(text) == clock.NO_TIME


@pytest.mark.parametrize(
    "text, expected",
    [
        ("¿Qué hora es?", True),
        ("oye, ¿sabes qué día cae hoy?", True),
        ("what's the date", True),
        ("hola", False),
        ("ahora cuéntame del juego", False),
        ("explícame cómo funciona el mapa en detalle", False),
        (None, False),
    ],
)
def test_loose_precheck(text, expected):
    assert clock.may_ask_time(text) is expected


# --- the decision schema and defaults -----------------------------------------


def test_clock_is_a_decision_with_two_values_for_owner_voice_only():
    assert CLOCK == "clock"
    assert "clock" in DECISION_IDS
    assert VALUES["clock"] == ("time", "no_time")
    assert QUESTIONS["clock"]["type"] == "choice"
    assert SOURCES["clock"] == frozenset({"direct", "ptt"})


def test_clock_defaults_to_on(monkeypatch):
    monkeypatch.delenv("OPENCOHOST_DECISION_CLOCK", raising=False)
    out = settings._resolve_decision_defaults()
    assert out["decisions"]["clock"] == {"mode": "on", "threshold": 0.9}
    assert dc.get_mode("clock") == "on"


@pytest.mark.parametrize("raw", ["off", "shadow", "on"])
def test_clock_env_mode(monkeypatch, raw):
    monkeypatch.setenv("OPENCOHOST_DECISION_CLOCK", raw)
    assert settings._resolve_decision_defaults()["decisions"]["clock"]["mode"] == raw


# --- the consumer ----------------------------------------------------------------


class FakeBackend:
    def __init__(self, choice="time", conf=0.95):
        self.choice = choice
        self.conf = conf
        self.calls = []

    def ask(self, state, questions, timeout_s):
        self.calls.append((state, tuple(questions)))
        answers = {}
        for q in questions:
            value = self.choice if q == "clock" else VALUES[q][0]
            answers[q] = {"choice": value, "answer_confidence": self.conf}
        return {"ok": True, "answers": answers}


def _turn(choice="time", conf=0.95, mode="on"):
    dc.set_decision("clock", mode=mode)
    return DecisionBus(FakeBackend(choice, conf)).decide("x", "direct", ids=("clock",))


def test_consumer_none_is_the_rule():
    assert apply_clock_decision(None, "time") == "time"
    assert apply_clock_decision(None, "no_time") == "no_time"


def test_rule_time_always_wins():
    assert apply_clock_decision(_turn("no_time"), "time") == "time"


def test_confident_laya_on_adds_time():
    assert apply_clock_decision(_turn("time", 0.95), "no_time") == "time"


def test_low_confidence_or_shadow_keeps_rule():
    assert apply_clock_decision(_turn("time", 0.5), "no_time") == "no_time"
    assert apply_clock_decision(_turn("time", 0.95, mode="shadow"), "no_time") == "no_time"


def test_resolution_logs_a_decision_line(caplog):
    caplog.set_level(logging.INFO, logger="OpenCohost")
    apply_clock_decision(_turn("time", 0.95), "no_time")
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("[DECISION] id=clock")]
    assert len(lines) == 1
    assert "acted_by=laya" in lines[0]


# --- engine: clock joins the single per-turn decide() -------------------------


def _motor(monkeypatch):
    m = llm_engine.MotorVocalIA(queue.Queue(), lambda event: None)
    m.current_model = "llama3"
    m.use_system_role = True
    m.ollama = MagicMock()
    m._speech_router_enabled = False
    m._vision = SimpleNamespace(step=None, enabled=False)
    return m


def test_clock_joins_the_turn_call_only_when_the_precheck_matches(monkeypatch):
    backend = FakeBackend()
    decision_runtime.install_backend(backend)
    m = _motor(monkeypatch)
    m._turn_decisions("¿qué hora es?", "direct", allow_vision=False)
    m._turn_decisions("hola", "direct", allow_vision=False)
    assert [ids for _, ids in backend.calls] == [
        ("memory", "reply_length", "clock"),
        ("memory", "reply_length"),
    ]


@pytest.mark.parametrize("source", ["chat", settings.OWNER_BUNDLE_SOURCE])
def test_clock_never_asked_for_other_sources(monkeypatch, source):
    backend = FakeBackend()
    decision_runtime.install_backend(backend)
    m = _motor(monkeypatch)
    m._turn_decisions("¿qué hora es?", source, allow_vision=False)
    assert all("clock" not in ids for _, ids in backend.calls)


def test_clock_off_is_not_asked(monkeypatch):
    dc.set_decision("clock", mode="off")
    backend = FakeBackend()
    decision_runtime.install_backend(backend)
    m = _motor(monkeypatch)
    m._turn_decisions("¿qué hora es?", "direct", allow_vision=False)
    assert [ids for _, ids in backend.calls] == [("memory", "reply_length")]
