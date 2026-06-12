"""Tests for opencohost.ui.topic_inbox_bridge — TopicInboxBridge.

The bridge merges valid pending inbox proposals into the agenda suggestions
list (tagged with the robot emoji, angle visible), polls the store fail-open,
and routes approve/discard for ti_-prefixed suggestion ids.

Headless: no customtkinter import — the bridge is pure logic with an
injectable scheduler.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from opencohost.core.topic_inbox import TopicInboxStore
from opencohost.smart_aggregator.kira_agenda_controller import (
    KiraAgendaController,
    TopicStatus,
)
from opencohost.ui.topic_inbox_bridge import (
    INBOX_ID_PREFIX,
    POLL_INTERVAL_MS,
    TopicInboxBridge,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_bridge(tmp_path: Path, log_fn=None) -> tuple[TopicInboxBridge, TopicInboxStore]:
    store = TopicInboxStore(str(tmp_path / "cards.db"))
    return TopicInboxBridge(store, log_fn=log_fn), store


class FakeScheduler:
    """Collect scheduled callbacks; fire them manually with tick()."""

    def __init__(self) -> None:
        self.pending: list = []
        self.delays: list[int] = []

    def after(self, fn, delay_ms) -> None:
        self.pending.append(fn)
        self.delays.append(delay_ms)

    def tick(self) -> None:
        fns, self.pending = self.pending, []
        for fn in fns:
            fn()


# ---------------------------------------------------------------------------
# Suggestion building
# ---------------------------------------------------------------------------

def test_pending_suggestions_tagged_with_robot_and_angle(tmp_path: Path) -> None:
    bridge, store = make_bridge(tmp_path)
    store.propose(title="Retro Revival", angle="Why the 90s are back.", tags=[], source="research-bot")
    bridge.refresh()

    suggestions = bridge.pending_suggestions()

    assert len(suggestions) == 1
    s = suggestions[0]
    assert s["topic_id"].startswith(INBOX_ID_PREFIX)
    assert s["title"] == "Retro Revival"
    assert s["angle"] == "Why the 90s are back."  # angle must reach the UI
    assert "🤖" in s["source"]
    assert "research-bot" in s["source"]


def test_build_suggestions_merges_drafted_and_inbox(tmp_path: Path) -> None:
    bridge, store = make_bridge(tmp_path)
    store.propose(title="Inbox Proposal Topic", angle="Agent angle.", tags=[], source="bot")
    bridge.refresh()

    ctrl = KiraAgendaController()
    ctrl.suggest_topics([{"title": "Suggester Topic", "angle": "SA angle.", "confidence": "HIGH", "source": "vibe"}])

    merged = bridge.build_suggestions(ctrl)

    titles = [s["title"] for s in merged]
    assert "Suggester Topic" in titles
    assert "Inbox Proposal Topic" in titles
    # Every merged suggestion carries the fields the panel renders
    for s in merged:
        assert {"title", "angle", "confidence", "source", "topic_id"} <= set(s)


def test_invalid_rows_never_become_suggestions(tmp_path: Path) -> None:
    import sqlite3
    from datetime import datetime, timezone

    bridge, store = make_bridge(tmp_path)
    store.propose(title="Legit", angle="ok", tags=[], source="bot")
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(str(tmp_path / "cards.db")) as conn:
        conn.execute(
            """INSERT INTO topic_inbox (id, title, angle, tags, source, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, 'proposed', ?, ?)""",
            ("ti_evil", "<script>x</script>", "x", "[]", "attacker", now, now),
        )
    bridge.refresh()

    ids = [s["topic_id"] for s in bridge.pending_suggestions()]
    assert "ti_evil" not in ids


# ---------------------------------------------------------------------------
# Polling
# ---------------------------------------------------------------------------

def test_refresh_returns_true_only_on_change(tmp_path: Path) -> None:
    bridge, store = make_bridge(tmp_path)
    assert bridge.refresh() is False  # empty → empty: no change

    store.propose(title="New Topic", angle="a", tags=[], source="bot")
    assert bridge.refresh() is True   # gained a row
    assert bridge.refresh() is False  # stable


def test_refresh_fails_open_on_store_error(tmp_path: Path) -> None:
    bridge, store = make_bridge(tmp_path)
    store.propose(title="Cached Topic", angle="a", tags=[], source="bot")
    bridge.refresh()

    bridge._store = MagicMock()
    bridge._store.list_pending.side_effect = RuntimeError("disk gone")

    assert bridge.refresh() is False
    # Cache survives the failure — UI keeps showing the last known state
    assert len(bridge.pending_suggestions()) == 1


def test_start_polling_reschedules_and_notifies_on_change(tmp_path: Path) -> None:
    bridge, store = make_bridge(tmp_path)
    sched = FakeScheduler()
    changes: list[int] = []

    bridge.start_polling(sched.after, lambda: changes.append(1))
    assert sched.delays == [POLL_INTERVAL_MS]

    sched.tick()                     # poll 1: empty, no change
    assert changes == []
    assert len(sched.pending) == 1   # rescheduled

    store.propose(title="Arrives Later", angle="a", tags=[], source="bot")
    sched.tick()                     # poll 2: new row → notify
    assert changes == [1]
    sched.tick()                     # poll 3: stable → no extra notify
    assert changes == [1]


def test_polling_survives_on_change_exception(tmp_path: Path) -> None:
    bridge, store = make_bridge(tmp_path)
    sched = FakeScheduler()

    def explode() -> None:
        raise RuntimeError("UI died")

    bridge.start_polling(sched.after, explode)
    store.propose(title="Boom Topic", angle="a", tags=[], source="bot")
    sched.tick()                     # on_change raises — must not propagate

    assert len(sched.pending) == 1   # loop keeps running


# ---------------------------------------------------------------------------
# Approve routing
# ---------------------------------------------------------------------------

def test_approve_creates_queued_agenda_topic_and_consumes_row(tmp_path: Path) -> None:
    bridge, store = make_bridge(tmp_path)
    row = store.propose(title="Approved Topic", angle="The angle.", tags=[], source="bot")
    bridge.refresh()
    ctrl = KiraAgendaController()

    assert bridge.approve(row["id"], ctrl) is True

    queued = ctrl.queued_topics()
    assert len(queued) == 1
    assert queued[0].title == "Approved Topic"
    assert queued[0].angle == "The angle."
    # Store row consumed
    assert all(r["id"] != row["id"] for r in store.list_pending()["valid"])
    # No longer suggested
    assert all(s["topic_id"] != row["id"] for s in bridge.pending_suggestions())


def test_approve_sanitizer_rejection_keeps_row_pending(tmp_path: Path) -> None:
    logs: list[str] = []
    bridge, store = make_bridge(tmp_path, log_fn=logs.append)
    # 100-char title: valid for the inbox (max 120), rejected by the agenda
    # controller sanitizer (max 90)
    row = store.propose(title="T" * 100, angle="a", tags=[], source="bot")
    bridge.refresh()
    ctrl = KiraAgendaController()

    assert bridge.approve(row["id"], ctrl) is False

    assert ctrl.queued_topics() == []
    assert any(r["id"] == row["id"] for r in store.list_pending()["valid"])
    assert logs, "operator must see why the approval failed"


def test_approve_rolls_back_topic_when_row_already_consumed(tmp_path: Path) -> None:
    bridge, store = make_bridge(tmp_path)
    row = store.propose(title="Raced Topic", angle="a", tags=[], source="bot")
    bridge.refresh()
    store.discard(row["id"])  # someone else consumed it behind the cache
    ctrl = KiraAgendaController()

    assert bridge.approve(row["id"], ctrl) is False

    assert ctrl.queued_topics() == []
    assert all(t.title != "Raced Topic" for t in ctrl.topics)


def test_approve_unknown_id_returns_false(tmp_path: Path) -> None:
    bridge, _store = make_bridge(tmp_path)
    ctrl = KiraAgendaController()
    assert bridge.approve("ti_nope", ctrl) is False


def test_approve_routes_controller_drafted_topics_too(tmp_path: Path) -> None:
    """Non-ti_ ids follow the legacy path: APPROVED then QUEUED."""
    bridge, _store = make_bridge(tmp_path)
    ctrl = KiraAgendaController()
    created = ctrl.suggest_topics([{"title": "SA Drafted", "angle": "x", "confidence": "HIGH", "source": "vibe"}])

    assert bridge.approve(created[0].id, ctrl) is True

    queued = ctrl.queued_topics()
    assert len(queued) == 1 and queued[0].title == "SA Drafted"


def test_reject_routes_both_namespaces(tmp_path: Path) -> None:
    bridge, store = make_bridge(tmp_path)
    ctrl = KiraAgendaController()
    created = ctrl.suggest_topics([{"title": "SA Drafted", "angle": "x", "confidence": "LOW", "source": "vibe"}])
    row = store.propose(title="Inbox Proposal", angle="a", tags=[], source="bot")
    bridge.refresh()

    assert bridge.reject(created[0].id, ctrl) is True
    assert created[0].status is TopicStatus.SKIPPED

    assert bridge.reject(row["id"], ctrl) is True
    assert store.list_pending()["valid"] == []


# ---------------------------------------------------------------------------
# Discard routing + id namespace
# ---------------------------------------------------------------------------

def test_discard_removes_row_and_suggestion(tmp_path: Path) -> None:
    bridge, store = make_bridge(tmp_path)
    row = store.propose(title="Discarded Topic", angle="a", tags=[], source="bot")
    bridge.refresh()

    assert bridge.discard(row["id"]) is True

    assert store.list_pending()["valid"] == []
    assert bridge.pending_suggestions() == []


def test_is_inbox_id_namespacing() -> None:
    assert TopicInboxBridge.is_inbox_id("ti_abc123") is True
    assert TopicInboxBridge.is_inbox_id("topic-uuid-from-controller") is False
    assert TopicInboxBridge.is_inbox_id("") is False
    assert TopicInboxBridge.is_inbox_id(None) is False


# ---------------------------------------------------------------------------
# app_shell wiring (source-level, no CTk import)
# ---------------------------------------------------------------------------

class TestAppShellInboxWiring:
    def _source(self) -> str:
        src_path = os.path.join(ROOT_DIR, "opencohost", "ui", "app_shell.py")
        with open(src_path, "r", encoding="utf-8") as f:
            return f.read()

    def test_app_shell_builds_bridge_and_polls(self) -> None:
        src = self._source()
        assert "TopicInboxBridge" in src
        assert "start_polling" in src

    def test_app_shell_routes_inbox_ids_in_approve_and_reject(self) -> None:
        src = self._source()
        assert "_topic_inbox_bridge.approve(" in src
        assert "_topic_inbox_bridge.reject(" in src
