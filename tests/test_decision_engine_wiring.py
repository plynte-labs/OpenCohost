"""laya-decision-layer-20261009 T3: engine, prompt trace and aggregator wiring.

Real MotorVocalIA with fake LLM transports and a fake decision backend
installed in the process runtime. Pins:
- one ``decide()`` per turn, before the vision look, reused by the cloud retry
  and by a pregen run for the same payload;
- with no backend, every decision off, or every decision in shadow, the
  assembled prompt, the vision step call, the memory recall and the reply
  level are identical to the base path;
- ``on`` decisions act only as the consumer rules allow;
- the opt-in prompt trace gains a ``decisions`` key only when decisions ran;
- viewer chat gets a fire-and-forget shadow ``respond`` decision.
"""
from __future__ import annotations

import json
import logging
import os
import queue
import time
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import yaml

from opencohost.config import decisions as dc
from opencohost.config import settings
from opencohost.core import llm_engine
from opencohost.core.decision import runtime as decision_runtime
from opencohost.core.engine import prompt_trace
from opencohost.core.memory import episodic_merge
from opencohost.core.decision.consumers import apply_vision_decision
from opencohost.core.vision.gate import CompositeGate, ExplicitTriggerGate, RuleVisionGate
from opencohost.core.vision.turn_step import VisionTurnResult
from opencohost.core.vision.sanitize import build_screen_block

BLOCK = build_screen_block("Barra de vida al 30%.")
SECRET = "zanahoriaconfidencial hola"
ALL = ("vision", "memory", "reply_length", "respond")


class FakeBackend:
    """Disagrees with every rule by default: look, skip, long."""

    def __init__(self, answers=None, conf=0.99):
        self.answers = answers or {"vision": "look", "memory": "skip", "reply_length": "long", "respond": "ignore"}
        self.conf = conf
        self.calls = []

    def ask(self, state, questions, timeout_s):
        self.calls.append((state, tuple(questions)))
        return {
            "ok": True,
            "answers": {q: {"choice": self.answers[q], "answer_confidence": self.conf} for q in questions},
        }


class FakeStep:
    """Records its calls; resolves the vision decision like the real step
    (rule gate + ``apply_vision_decision``) but never captures."""

    def __init__(self):
        self.calls = []
        self.gate = CompositeGate(ExplicitTriggerGate(False, 0.0), RuleVisionGate())

    def run(self, text, source, **kwargs):
        self.calls.append((text, source, kwargs))
        decision = self.gate.decide(text, source)
        if kwargs.get("decisions") is not None:
            decision = apply_vision_decision(
                decision, kwargs["decisions"], source=source, rule_gate=self.gate.secondary
            )
        return VisionTurnResult("", decision.reason_codes, "no_vision")


def _modes(mode):
    for d in ALL:
        dc._install_entry(d, mode if not (d == "respond" and mode == "on") else "shadow", 0.9)


def _motor(monkeypatch):
    m = llm_engine.MotorVocalIA(queue.Queue(), lambda event: None)
    m.current_model = "llama3"
    m.use_system_role = True
    m.ollama = MagicMock()
    m._speech_router_enabled = False
    monkeypatch.setattr(llm_engine, "LLM_STREAMING_ENABLED", False)
    seen = {"messages": [], "builds": [], "recalls": 0, "levels": []}

    def _chat(**kwargs):
        seen["messages"].append(kwargs["messages"])
        return {"message": {"content": "Claro que sí, aquí estoy."}}

    m._ollama_chat_with_watchdog = _chat
    m._vision = SimpleNamespace(step=FakeStep(), enabled=True)
    m._current_profile_id = "profile-1"
    m._memory = None

    def _recall(mem, text, profile_id):
        seen["recalls"] += 1
        return episodic_merge.RecallOutcome.disabled()

    m._run_episodic_recall = _recall
    original_build = m._build_generation_request

    def _build(*args, **kwargs):
        seen["builds"].append(kwargs)
        return original_build(*args, **kwargs)

    m._build_generation_request = _build
    original_level = m._resolve_reply_level

    def _level(*args, **kwargs):
        level = original_level(*args, **kwargs)
        seen["levels"].append(level)
        return level

    m._resolve_reply_level = _level
    return m, seen


def _run(monkeypatch, text="hola", source="direct", **kwargs):
    motor, seen = _motor(monkeypatch)
    motor._generar_dialogo(text, source=source, commit_history=True, **kwargs)
    return motor, seen


# --- base equivalence --------------------------------------------------------


def _fingerprint(motor, seen):
    step_calls = [(t, s, k) for t, s, k in motor._vision.step.calls]
    return {
        "messages": seen["messages"],
        "step_calls": step_calls,
        "recalls": seen["recalls"],
        "levels": seen["levels"],
        "build_kwargs": [sorted(k) for k in seen["builds"]],
    }


@pytest.mark.parametrize("text", ["hola", "explícame cómo funciona el mapa en detalle", "qué es eso"])
def test_no_backend_off_and_shadow_are_identical_to_base(monkeypatch, text):
    base = _fingerprint(*_run(monkeypatch, text))
    assert base["step_calls"] == [(text, "direct", {})]
    assert all("decisions" not in k for k in base["build_kwargs"])

    decision_runtime.install_backend(FakeBackend())
    _modes("off")
    off = _fingerprint(*_run(monkeypatch, text))
    assert off == base

    _modes("shadow")
    backend = FakeBackend()
    decision_runtime.install_backend(backend)
    motor, seen = _run(monkeypatch, text)
    shadow = _fingerprint(motor, seen)
    assert len(backend.calls) == 1
    assert shadow["messages"] == base["messages"]
    assert shadow["recalls"] == base["recalls"] == 1
    assert shadow["levels"] == base["levels"]
    assert [c[:2] for c in shadow["step_calls"]] == [(text, "direct")]


def test_unready_backend_is_base(monkeypatch):
    base = _fingerprint(*_run(monkeypatch))
    backend = FakeBackend()
    backend.available = lambda: False
    decision_runtime.install_backend(backend)
    assert _fingerprint(*_run(monkeypatch)) == base
    assert backend.calls == []


def test_backend_timeout_is_base(monkeypatch):
    base = _fingerprint(*_run(monkeypatch))
    backend = FakeBackend()
    backend.ask = lambda *a, **k: None
    decision_runtime.install_backend(backend)
    _modes("on")
    motor, seen = _run(monkeypatch)
    fp = _fingerprint(motor, seen)
    assert fp["messages"] == base["messages"]
    assert fp["recalls"] == base["recalls"] and fp["levels"] == base["levels"]


# --- one decide per turn -----------------------------------------------------


def test_one_decide_per_turn_with_clean_state_and_applicable_ids(monkeypatch):
    backend = FakeBackend()
    decision_runtime.install_backend(backend)
    wrapped = llm_engine.i18n_active.ptt_wrapper().format(text=SECRET)
    motor, seen = _run(monkeypatch, wrapped, source="ptt")
    assert backend.calls == [(SECRET, ("vision", "memory", "reply_length"))]
    assert "decisions" in seen["builds"][0]
    assert "decisions" in motor._vision.step.calls[0][2]


def test_vision_not_asked_when_vision_is_off_or_not_allowed(monkeypatch):
    backend = FakeBackend()
    decision_runtime.install_backend(backend)
    motor, _ = _motor(monkeypatch)
    motor._vision = SimpleNamespace(step=FakeStep(), enabled=False)
    motor._generar_dialogo("hola", source="direct", commit_history=True)
    motor2, _ = _motor(monkeypatch)
    motor2._generar_dialogo("hola otra vez", source="direct", commit_history=False, allow_vision=False)
    assert [ids for _, ids in backend.calls] == [("memory", "reply_length")] * 2


def test_non_owner_sources_never_ask(monkeypatch):
    backend = FakeBackend()
    decision_runtime.install_backend(backend)
    _run(monkeypatch, "hola chat", source="chat")
    assert backend.calls == []


def test_same_payload_reuses_the_turn_decisions(monkeypatch):
    backend = FakeBackend()
    decision_runtime.install_backend(backend)
    motor, _ = _motor(monkeypatch)
    first = motor._turn_decisions("hola", "direct", allow_vision=True)
    assert motor._turn_decisions("hola", "direct", allow_vision=True) is first
    assert len(backend.calls) == 1
    motor._turn_decisions("adiós", "direct", allow_vision=True)
    assert len(backend.calls) == 2


def test_turn_decisions_expire(monkeypatch):
    backend = FakeBackend()
    decision_runtime.install_backend(backend)
    motor, _ = _motor(monkeypatch)
    clock = [100.0]
    monkeypatch.setattr("opencohost.core.engine.llm_engine_decisions._monotonic", lambda: clock[0])
    motor._turn_decisions("hola", "direct", allow_vision=True)
    clock[0] += 120.0
    motor._turn_decisions("hola", "direct", allow_vision=True)
    assert len(backend.calls) == 2


def test_cloud_retry_reuses_the_decisions(monkeypatch):
    backend = FakeBackend()
    decision_runtime.install_backend(backend)
    motor, seen = _motor(monkeypatch)
    calls = {"n": 0}
    original = motor._cloud_attempt_loop

    def _attempt(setup, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            motor._cloud_fallback_active = True
            motor._cloud_fallback_reason = llm_engine.cloud_llm_client.CLOUD_ERROR_TRANSIENT
            return SimpleNamespace(early_return="", stream=None)
        return original(setup, **kwargs)

    motor._cloud_attempt_loop = _attempt
    motor._provider_config = {"active_provider": "openai", "mode": "cloud"}
    motor._cfg_is_local = lambda cfg: False
    motor._cloud_fallback_active = False
    motor._generar_dialogo("hola", source="direct", commit_history=True)
    assert len(seen["builds"]) == 2
    assert seen["builds"][0]["decisions"] is seen["builds"][1]["decisions"]
    assert len(backend.calls) == 1


# --- on mode -----------------------------------------------------------------


def test_on_mode_skips_recall_and_sets_reply_level(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="OpenCohost")
    decision_runtime.install_backend(FakeBackend())
    _modes("on")
    motor, seen = _run(monkeypatch, SECRET)
    assert seen["recalls"] == 0
    assert seen["levels"] == ["long"]
    lines = [r.getMessage() for r in caplog.records if "[DECISION]" in r.getMessage()]
    assert any("id=memory" in l and "acted_by=laya" in l for l in lines)
    assert any("id=reply_length" in l and "acted_by=laya" in l for l in lines)
    assert any("[REPLY_LENGTH]" in r.getMessage() and "reason=laya" in r.getMessage() for r in caplog.records)
    assert "zanahoria" not in caplog.text


def test_on_mode_fixed_reply_setting_wins(monkeypatch):
    from opencohost.config import reply_length as rl

    rl.set_reply_length_mode("short")
    decision_runtime.install_backend(FakeBackend())
    _modes("on")
    _, seen = _run(monkeypatch, SECRET)
    assert seen["levels"] == ["short"]


def test_on_mode_low_confidence_keeps_rules(monkeypatch):
    base = _fingerprint(*_run(monkeypatch, SECRET))
    decision_runtime.install_backend(FakeBackend(conf=0.5))
    _modes("on")
    fp = _fingerprint(*_run(monkeypatch, SECRET))
    assert fp["messages"] == base["messages"] and fp["recalls"] == 1 and fp["levels"] == base["levels"]


# --- prompt trace --------------------------------------------------------------


@pytest.fixture
def trace_on(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "LOG_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "PROMPT_TRACE_ENABLED", True)
    prompt_trace._reset_for_tests()
    return tmp_path / "prompt_trace"


def _records(trace_dir):
    out = []
    for f in sorted(trace_dir.glob("*.jsonl")):
        out.extend(json.loads(l) for l in f.read_text(encoding="utf-8").splitlines())
    return out


def test_trace_has_no_decisions_key_without_decisions(monkeypatch, trace_on):
    _run(monkeypatch)
    rec = _records(trace_on)[0]
    assert "decisions" not in rec


def test_trace_records_decision_outcomes(monkeypatch, trace_on):
    decision_runtime.install_backend(FakeBackend())
    _run(monkeypatch)
    rec = _records(trace_on)[0]
    ids = [d["id"] for d in rec["decisions"]]
    assert ids == ["vision", "memory", "reply_length"]
    vision = rec["decisions"][0]
    assert vision["rule_value"] == "no_look" and vision["laya_value"] == "look"
    assert vision["acted_by"] == "rule" and vision["mode"] == "shadow"


# --- aggregator respond shadow --------------------------------------------------


def _aggregator(smart_aggregator_config, temp_dir):
    from opencohost.smart_aggregator.aggregator import Aggregator

    cfg = deepcopy(smart_aggregator_config)
    cfg["history"]["db_path"] = os.path.join(temp_dir, "sessions.db")
    cfg["history"]["jsonl_path"] = os.path.join(temp_dir, "chat_log.jsonl")
    cfg["activity"]["threshold_per_second"] = 1000.0
    path = os.path.join(temp_dir, "smart_aggregator.yaml")
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(cfg, f, allow_unicode=True)
    agg = Aggregator(config_path=path)
    agg.start_session("youtube", "headless_test")
    return agg


MSG = {"user": "u1", "text": "Kira qué opinas del boss final de este juego", "timestamp": None}


def test_aggregator_submits_shadow_respond(smart_aggregator_config, temp_dir, monkeypatch):
    calls = []
    monkeypatch.setattr(decision_runtime, "shadow_respond", lambda text, rule, on_result=None: calls.append((text, rule)) or True)
    agg = _aggregator(smart_aggregator_config, temp_dir)
    agg.process_message({**MSG, "timestamp": time.time()})
    agg.process_message({"user": "u2", "text": "a", "timestamp": time.time()})  # rejected
    assert calls == [(MSG["text"], "ignore")]


def test_aggregator_survives_a_failing_hook(smart_aggregator_config, temp_dir, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("x")

    monkeypatch.setattr(decision_runtime, "shadow_respond", boom)
    agg = _aggregator(smart_aggregator_config, temp_dir)
    agg.process_message({**MSG, "timestamp": time.time()})
    assert agg.activity.trigger_count == 0


def test_shadow_respond_logs_without_text(caplog):
    caplog.set_level(logging.INFO, logger="OpenCohost")
    decision_runtime.install_backend(FakeBackend())
    assert decision_runtime.shadow_respond(SECRET, "ignore") is True
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not any("id=respond" in r.getMessage() for r in caplog.records):
        time.sleep(0.02)
    line = [r.getMessage() for r in caplog.records if "id=respond" in r.getMessage()][0]
    assert "mode=shadow" in line and "acted_by=rule" in line
    assert "zanahoria" not in caplog.text


def test_shadow_respond_drops_while_busy():
    import threading

    gate = threading.Event()

    class SlowBackend(FakeBackend):
        def ask(self, *a, **k):
            gate.wait(5)
            return super().ask(*a, **k)

    decision_runtime.install_backend(SlowBackend())
    assert decision_runtime.shadow_respond("uno", "ignore") is True
    assert decision_runtime.shadow_respond("dos", "ignore") is False
    gate.set()
