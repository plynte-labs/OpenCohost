"""Integration tests for Topic Scout wiring into the idle-suggestion worker.

The Scout is merged as a THIRD source inside ``_dispatch_suggestion_recompute``
(after the rule-based ``generate_suggestions``). It must:
  - call ``motor_ia.scout_digest()`` on the worker thread,
  - merge its titles into the suggestions handed to ``_apply_idle_suggestions``,
  - never persist (T8) — only the DRAFTED RAM path via ``suggest_topics``.

The app is built with ``__init__`` bypassed; the worker thread is forced
synchronous via a threading shim so the merge can be asserted deterministically.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import queue

from opencohost.ui import app_shell
from opencohost.ui.app_shell import VocalAIApp
from opencohost.smart_aggregator import AgendaState
from opencohost.core import llm_engine
from opencohost.core.llm_engine import MotorVocalIA
from opencohost.core.agenda import topic_inbox
from opencohost.smart_aggregator import session_history as session_history_mod
from opencohost.smart_aggregator.kira_agenda_controller import (
    KiraAgendaController,
    TopicStatus,
)
from opencohost.ui.topic_inbox_bridge import TopicInboxBridge


class _SyncThread:
    """Runs the target inline on .start() so the worker is deterministic."""

    def __init__(self, target=None, daemon=None, name=None, **kwargs):
        self._target = target

    def start(self):
        if self._target is not None:
            self._target()


def _make_app(monkeypatch, *, scout_return, rule_return):
    monkeypatch.setattr(app_shell, "threading", SimpleNamespace(Thread=_SyncThread))
    monkeypatch.setattr(app_shell, "generate_suggestions", lambda **kwargs: list(rule_return))

    app = VocalAIApp.__new__(VocalAIApp)
    app.smart_agg = SimpleNamespace(
        intent_aggregator=SimpleNamespace(summarize=lambda: {}),
        thermometer=SimpleNamespace(compute_vibe=lambda: {"temperature": 50}),
        history=SimpleNamespace(get_recent_context_snapshots=lambda sid, max_items=3: []),
    )
    app.motor_ia = SimpleNamespace(scout_digest=MagicMock(return_value=list(scout_return)))
    app._safe_after = lambda fn, *a, **k: fn()
    captured = {}
    app._apply_idle_suggestions = lambda gen, suggestions: captured.update(
        gen=gen, suggestions=suggestions
    )
    return app, captured


def test_scout_merged_into_idle_suggestions(monkeypatch):
    rule = [{"title": "tema por reglas", "source": "rules", "confidence": "LOW"}]
    scout = [{"title": "regulación de LLMs", "source": "scout", "confidence": "LOW"}]
    app, captured = _make_app(monkeypatch, scout_return=scout, rule_return=rule)

    app._dispatch_suggestion_recompute(7, [], [], "sid")

    app.motor_ia.scout_digest.assert_called_once()
    assert captured["gen"] == 7
    # rule source first, then the scout titles appended.
    assert captured["suggestions"] == rule + scout


def test_scout_noop_leaves_rule_suggestions_untouched(monkeypatch):
    rule = [{"title": "tema por reglas", "source": "rules", "confidence": "LOW"}]
    app, captured = _make_app(monkeypatch, scout_return=[], rule_return=rule)

    app._dispatch_suggestion_recompute(3, [], [], "sid")

    app.motor_ia.scout_digest.assert_called_once()
    assert captured["suggestions"] == rule


def test_scout_exception_does_not_drop_rule_suggestions(monkeypatch):
    rule = [{"title": "tema por reglas", "source": "rules", "confidence": "LOW"}]
    app, captured = _make_app(monkeypatch, scout_return=[], rule_return=rule)
    app.motor_ia.scout_digest = MagicMock(side_effect=RuntimeError("scout blew up"))

    app._dispatch_suggestion_recompute(9, [], [], "sid")

    # Rule suggestions still reach apply even if the scout call raises.
    assert captured["suggestions"] == rule


def test_apply_idle_suggestions_discards_stale_generation():
    app = VocalAIApp.__new__(VocalAIApp)
    app._closing = False
    app._suggestion_gen = 5
    app.kira_agenda = SimpleNamespace(state=AgendaState.IDLE, suggest_topics=MagicMock())
    app._kira_agenda_update_status = lambda: None

    # Stale generation -> nothing applied.
    app._apply_idle_suggestions(4, [{"title": "x"}])
    app.kira_agenda.suggest_topics.assert_not_called()

    # Matching generation -> applied.
    app._apply_idle_suggestions(5, [{"title": "x"}])
    app.kira_agenda.suggest_topics.assert_called_once()


# ── T8: scout never persists before the human approval gate ──────────────────


def _build_scout_motor(monkeypatch):
    monkeypatch.setattr(llm_engine, "SCOUT_ENABLED", True)
    motor = MotorVocalIA(queue.Queue(), lambda e: None)
    motor._loaded_model = "m"
    motor._pending_model_switch = None
    motor._awaiting_first_success_after_switch = False
    motor._check_capabilities_reasoning = lambda model: False
    # Tagged host turns so the host-only scout filter (Task C) renders them.
    motor.historial.append({"role": "user", "content": "hablamos de viajes espaciales", "source": "direct"})
    motor.historial.append({"role": "assistant", "content": "si, la exploración de Marte da para rato", "source": "direct"})

    def chat(**kwargs):
        return {"message": {"content": "colonización de Marte\nturismo orbital"}}

    motor.ollama = type("F", (), {"Client": lambda self, **k: type(
        "C", (), {"chat": lambda self, **kw: chat(**kw)})()})()
    return motor


def test_scout_no_persist_before_approval(monkeypatch):
    # Spy the two persistence seams the scout must NEVER touch while scouting.
    propose_spy = MagicMock()
    snapshot_spy = MagicMock()
    monkeypatch.setattr(topic_inbox.TopicInboxStore, "propose", propose_spy)
    monkeypatch.setattr(session_history_mod.SessionHistory, "add_context_snapshot", snapshot_spy)

    motor = _build_scout_motor(monkeypatch)
    scout = motor.scout_digest()
    assert scout, "scout should have produced draft-ready titles for this test"

    controller = KiraAgendaController()
    created = controller.suggest_topics(scout)

    # During scouting + drafting: RAM-only DRAFTED, nothing persisted, nothing queued.
    assert created and all(t.status == TopicStatus.DRAFTED for t in created)
    assert controller.queued_topics() == []
    propose_spy.assert_not_called()
    snapshot_spy.assert_not_called()

    # Only the explicit human approval gate promotes a scout topic out of DRAFTED.
    bridge = TopicInboxBridge(store=MagicMock())
    assert bridge.approve(created[0].id, controller) is True
    assert created[0].status == TopicStatus.QUEUED
    assert created[0] in controller.queued_topics()

    # Still no pre-approval inbox/store persistence was triggered by the scout path.
    propose_spy.assert_not_called()
    snapshot_spy.assert_not_called()
