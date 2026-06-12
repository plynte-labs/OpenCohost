"""Tests for opencohost.core.agenda_persistence — AgendaPersistence.

Design contract (engram sdd/agenda-persistence/design, owner-approved):
  - Restart survival only: APPROVED/QUEUED topic definitions + session
    settings persist; ACTIVE saves as queued; runtime counters never hit disk.
  - Write-through on real change: full set in one transaction, preceded by a
    fingerprint comparison; bounded timeouts; fail-open with a ONE-time
    visible operator warning.
  - Restore: filtered SELECT with cap, controller sanitizer per row, invalid
    rows skipped, agenda switch stays OFF, never blocks launch.

Headless: no UI imports.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from opencohost.core.agenda_persistence import (
    READ_TIMEOUT_SECONDS,
    RESTORE_CAP,
    SCHEMA_VERSION,
    WRITE_TIMEOUT_SECONDS,
    AgendaPersistence,
)
from opencohost.smart_aggregator.kira_agenda_controller import (
    AgendaState,
    KiraAgendaController,
    TopicStatus,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_persistence(tmp_path: Path, log_fn=None) -> tuple[AgendaPersistence, str]:
    db = str(tmp_path / "cards.db")
    return AgendaPersistence(db, log_fn=log_fn), db


def controller_with_queue() -> KiraAgendaController:
    """Two queued topics (alta then normal) plus one approved-not-queued."""
    ctrl = KiraAgendaController(max_turns_per_topic=4, turn_batch_size=2)
    ctrl.set_session_settings(rhythm="calmo", response_length="corta", safety_mode="monologue")
    t1 = ctrl.add_topic("Tema urgente", "Angulo uno.", ["una frase corta"], approved=True, priority="alta")
    ctrl.queue_topic(t1.id)
    t2 = ctrl.add_topic("Tema normal", "Angulo dos.", approved=True, priority="normal")
    ctrl.queue_topic(t2.id)
    ctrl.add_topic("Tema aprobado sin encolar", "Angulo tres.", approved=True)
    return ctrl


# ---------------------------------------------------------------------------
# Roundtrip
# ---------------------------------------------------------------------------

def test_roundtrip_preserves_queue_order_statuses_and_settings(tmp_path: Path) -> None:
    persistence, db = make_persistence(tmp_path)
    ctrl = controller_with_queue()

    assert persistence.save_if_changed(ctrl) is True

    fresh = KiraAgendaController()
    restored = AgendaPersistence(db).load_into(fresh)

    assert restored == 3
    queued = fresh.queued_topics()
    assert [t.title for t in queued] == ["Tema urgente", "Tema normal"]
    assert queued[0].priority == "alta"
    assert queued[0].constraints == ["una frase corta"]
    approved_only = [t for t in fresh.topics if t.status == TopicStatus.APPROVED]
    assert [t.title for t in approved_only] == ["Tema aprobado sin encolar"]
    # Session settings restored
    assert fresh.rhythm == "calmo"
    assert fresh.response_length == "corta"
    assert fresh.safety_mode == "monologue"
    assert fresh.max_turns_per_topic == 4


def test_same_priority_insertion_order_is_preserved(tmp_path: Path) -> None:
    persistence, db = make_persistence(tmp_path)
    ctrl = KiraAgendaController()
    for title in ("Primero", "Segundo", "Tercero"):
        topic = ctrl.add_topic(title, approved=True)
        ctrl.queue_topic(topic.id)
    persistence.save_if_changed(ctrl)

    fresh = KiraAgendaController()
    AgendaPersistence(db).load_into(fresh)

    assert [t.title for t in fresh.queued_topics()] == ["Primero", "Segundo", "Tercero"]


def test_active_topic_saves_as_queued_without_runtime_counters(tmp_path: Path) -> None:
    persistence, db = make_persistence(tmp_path)
    ctrl = controller_with_queue()
    active = ctrl.queued_topics()[0]
    active.status = TopicStatus.ACTIVE
    active.turns_spoken = 3
    ctrl.active_topic = active

    persistence.save_if_changed(ctrl)
    fresh = KiraAgendaController()
    AgendaPersistence(db).load_into(fresh)

    restored = next(t for t in fresh.topics if t.title == "Tema urgente")
    assert restored.status == TopicStatus.QUEUED
    assert restored.turns_spoken == 0


def test_completed_skipped_and_drafted_are_not_persisted(tmp_path: Path) -> None:
    persistence, db = make_persistence(tmp_path)
    ctrl = controller_with_queue()
    done = ctrl.add_topic("Tema terminado", approved=True)
    done.status = TopicStatus.COMPLETED
    skipped = ctrl.add_topic("Tema salteado", approved=True)
    skipped.status = TopicStatus.SKIPPED
    ctrl.suggest_topics([{"title": "Sugerencia efimera", "angle": "x"}])

    persistence.save_if_changed(ctrl)
    fresh = KiraAgendaController()
    restored = AgendaPersistence(db).load_into(fresh)

    titles = {t.title for t in fresh.topics}
    assert restored == 3
    assert "Tema terminado" not in titles
    assert "Tema salteado" not in titles
    assert "Sugerencia efimera" not in titles


def test_editorial_card_link_and_consumed_flag_survive(tmp_path: Path) -> None:
    persistence, db = make_persistence(tmp_path)
    ctrl = KiraAgendaController()
    topic = ctrl.add_topic("Tema con card", "Angulo.", approved=True, editorial_card_id="ec_abc123")
    ctrl.queue_topic(topic.id)
    topic.editorial_card_consumed = True

    persistence.save_if_changed(ctrl)
    fresh = KiraAgendaController()
    AgendaPersistence(db).load_into(fresh)

    restored = fresh.queued_topics()[0]
    assert restored.editorial_card_id == "ec_abc123"
    assert restored.editorial_card_consumed is True


# ---------------------------------------------------------------------------
# Write-through on change only
# ---------------------------------------------------------------------------

def test_save_if_changed_skips_disk_when_state_is_identical(tmp_path: Path) -> None:
    persistence, _db = make_persistence(tmp_path)
    ctrl = controller_with_queue()

    assert persistence.save_if_changed(ctrl) is True
    with patch("opencohost.core.agenda_persistence.sqlite3.connect", wraps=sqlite3.connect) as connect:
        assert persistence.save_if_changed(ctrl) is False
        connect.assert_not_called()

    # A real mutation writes again
    extra = ctrl.add_topic("Tema nuevo", approved=True)
    ctrl.queue_topic(extra.id)
    assert persistence.save_if_changed(ctrl) is True


# ---------------------------------------------------------------------------
# Fail-open + one-time operator warning
# ---------------------------------------------------------------------------

def test_save_fails_open_on_corrupt_db_and_warns_once(tmp_path: Path) -> None:
    warnings: list[str] = []
    db = str(tmp_path / "corrupt.db")
    Path(db).write_bytes(b"not sqlite\x00\x01")
    persistence = AgendaPersistence(db, log_fn=warnings.append)
    ctrl = controller_with_queue()

    assert persistence.save_if_changed(ctrl) is False
    extra = ctrl.add_topic("Otro tema", approved=True)
    ctrl.queue_topic(extra.id)
    assert persistence.save_if_changed(ctrl) is False

    assert len(warnings) == 1, "operator warning must fire exactly once, not spam"


def test_load_fails_open_on_corrupt_db(tmp_path: Path) -> None:
    db = str(tmp_path / "corrupt.db")
    Path(db).write_bytes(b"not sqlite\x00\x01")

    fresh = KiraAgendaController()
    assert AgendaPersistence(db).load_into(fresh) == 0
    assert fresh.topics == []


def test_load_never_changes_agenda_switch_state(tmp_path: Path) -> None:
    persistence, db = make_persistence(tmp_path)
    persistence.save_if_changed(controller_with_queue())

    fresh = KiraAgendaController()
    AgendaPersistence(db).load_into(fresh)

    assert fresh.state == AgendaState.OFF
    assert fresh.active_topic is None


# ---------------------------------------------------------------------------
# Hostile / invalid rows on load
# ---------------------------------------------------------------------------

def test_invalid_rows_are_skipped_on_load(tmp_path: Path) -> None:
    persistence, db = make_persistence(tmp_path)
    persistence.save_if_changed(controller_with_queue())

    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO agenda_topics (position, title, angle, constraints, priority, response_length, status, editorial_card_id, editorial_card_consumed) "
            "VALUES (99, ?, ?, '[]', 'normal', 'normal', 'queued', NULL, 0)",
            ("<script>alert(1)</script>", "x"),
        )

    fresh = KiraAgendaController()
    restored = AgendaPersistence(db).load_into(fresh)

    assert restored == 3  # the hostile row was skipped, the rest loaded
    assert all("script" not in t.title for t in fresh.topics)


def test_restore_cap_bounds_the_load(tmp_path: Path) -> None:
    persistence, db = make_persistence(tmp_path)
    persistence.save_if_changed(controller_with_queue())  # creates schema

    with sqlite3.connect(db) as conn:
        conn.execute("DELETE FROM agenda_topics")
        for i in range(RESTORE_CAP + 10):
            conn.execute(
                "INSERT INTO agenda_topics (position, title, angle, constraints, priority, response_length, status, editorial_card_id, editorial_card_consumed) "
                "VALUES (?, ?, '', '[]', 'normal', 'normal', 'queued', NULL, 0)",
                (i, f"Tema numero {i:03d}"),
            )

    fresh = KiraAgendaController()
    restored = AgendaPersistence(db).load_into(fresh)

    assert restored == RESTORE_CAP


# ---------------------------------------------------------------------------
# Hygiene: timeouts, closed connections, schema version
# ---------------------------------------------------------------------------

def test_bounded_timeouts_on_save_and_load(tmp_path: Path) -> None:
    persistence, db = make_persistence(tmp_path)
    ctrl = controller_with_queue()

    with patch("opencohost.core.agenda_persistence.sqlite3.connect", wraps=sqlite3.connect) as connect:
        persistence.save_if_changed(ctrl)
        assert connect.call_args.kwargs.get("timeout") == WRITE_TIMEOUT_SECONDS

    with patch("opencohost.core.agenda_persistence.sqlite3.connect", wraps=sqlite3.connect) as connect:
        AgendaPersistence(db).load_into(KiraAgendaController())
        assert connect.call_args.kwargs.get("timeout") == READ_TIMEOUT_SECONDS

    assert WRITE_TIMEOUT_SECONDS < 5.0 and READ_TIMEOUT_SECONDS < 5.0


def test_connections_are_closed(tmp_path: Path) -> None:
    persistence, db = make_persistence(tmp_path)
    created: list[sqlite3.Connection] = []
    real_connect = sqlite3.connect

    def recording_connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        created.append(conn)
        return conn

    with patch("opencohost.core.agenda_persistence.sqlite3.connect", side_effect=recording_connect):
        persistence.save_if_changed(controller_with_queue())
        AgendaPersistence(db).load_into(KiraAgendaController())

    assert created
    for conn in created:
        with pytest.raises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")


def test_schema_version_is_stamped(tmp_path: Path) -> None:
    persistence, db = make_persistence(tmp_path)
    persistence.save_if_changed(controller_with_queue())

    with sqlite3.connect(db) as conn:
        row = conn.execute("SELECT value FROM agenda_meta WHERE key='schema_version'").fetchone()
    assert row is not None and int(row[0]) == SCHEMA_VERSION


def test_load_sets_fingerprint_so_startup_does_not_rewrite(tmp_path: Path) -> None:
    persistence, db = make_persistence(tmp_path)
    persistence.save_if_changed(controller_with_queue())

    fresh = KiraAgendaController()
    loader = AgendaPersistence(db)
    loader.load_into(fresh)

    # Right after load, nothing changed — no disk write
    assert loader.save_if_changed(fresh) is False


# ---------------------------------------------------------------------------
# app_shell wiring (source-level, no CTk import)
# ---------------------------------------------------------------------------

class TestAppShellPersistenceWiring:
    def _source(self) -> str:
        src_path = os.path.join(ROOT_DIR, "opencohost", "ui", "app_shell.py")
        with open(src_path, "r", encoding="utf-8") as f:
            return f.read()

    def test_app_shell_loads_agenda_on_startup(self) -> None:
        src = self._source()
        assert "AgendaPersistence" in src
        assert "load_into" in src

    def test_app_shell_saves_on_status_updates_and_wires_inbox_persist(self) -> None:
        src = self._source()
        assert "save_if_changed" in src
        assert "persist_fn" in src
