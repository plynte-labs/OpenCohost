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


def test_load_reports_restored_count_to_operator_log(tmp_path: Path) -> None:
    """Runtime-gate observability: a successful restore must be visible in
    the operator log; an empty restore stays silent (no first-run noise)."""
    seeder, db = make_persistence(tmp_path)
    seeder.save_if_changed(controller_with_queue())

    messages: list[str] = []
    AgendaPersistence(db, log_fn=messages.append).load_into(KiraAgendaController())
    assert any("3" in m for m in messages), "restore count must reach the operator log"

    empty_db = str(tmp_path / "empty.db")
    silent: list[str] = []
    AgendaPersistence(empty_db, log_fn=silent.append).load_into(KiraAgendaController())
    assert silent == []


def test_load_sets_fingerprint_so_startup_does_not_rewrite(tmp_path: Path) -> None:
    persistence, db = make_persistence(tmp_path)
    persistence.save_if_changed(controller_with_queue())

    fresh = KiraAgendaController()
    loader = AgendaPersistence(db)
    loader.load_into(fresh)

    # Right after load, nothing changed — no disk write
    assert loader.save_if_changed(fresh) is False


# ---------------------------------------------------------------------------
# Judge findings — load-failure wipe, warn-flag reset, hostile rows, threads
# ---------------------------------------------------------------------------

def test_failed_load_makes_persistence_read_only_and_preserves_disk(tmp_path: Path) -> None:
    """BLOCKER regression: a transient lock at startup must NOT let the empty
    session wipe the queue that is sitting intact on disk."""
    seeder, db = make_persistence(tmp_path)
    seeder.save_if_changed(controller_with_queue())  # 3 topics on disk

    warnings: list[str] = []
    loader = AgendaPersistence(db, log_fn=warnings.append)
    with patch(
        "opencohost.core.agenda_persistence.sqlite3.connect",
        side_effect=sqlite3.OperationalError("database is locked"),
    ):
        assert loader.load_into(KiraAgendaController()) == 0

    # Lock cleared; a save from the (empty) session must be refused
    assert loader.save_if_changed(KiraAgendaController()) is False
    assert loader.save_if_changed(KiraAgendaController()) is False
    assert len(warnings) == 1, "read-only degradation warns the operator once"

    # Disk untouched: a later healthy launch restores everything
    fresh = KiraAgendaController()
    assert AgendaPersistence(db).load_into(fresh) == 3


def test_warn_flag_resets_after_successful_write(tmp_path: Path) -> None:
    """Two distinct failure episodes must warn twice, not once per process."""
    warnings: list[str] = []
    persistence, _db = make_persistence(tmp_path, log_fn=warnings.append)
    ctrl = controller_with_queue()

    with patch(
        "opencohost.core.agenda_persistence.sqlite3.connect",
        side_effect=sqlite3.OperationalError("disk I/O error"),
    ):
        persistence.save_if_changed(ctrl)
    assert len(warnings) == 1

    assert persistence.save_if_changed(ctrl) is True  # healthy again

    extra = ctrl.add_topic("Tema extra", approved=True)
    ctrl.queue_topic(extra.id)
    with patch(
        "opencohost.core.agenda_persistence.sqlite3.connect",
        side_effect=sqlite3.OperationalError("disk I/O error"),
    ):
        persistence.save_if_changed(ctrl)
    assert len(warnings) == 2


def test_oversized_constraints_row_is_skipped(tmp_path: Path) -> None:
    """A hostile row with a huge constraints array must not stall the launch
    sanitizing 100k elements — it is skipped outright."""
    import json as _json

    persistence, db = make_persistence(tmp_path)
    persistence.save_if_changed(controller_with_queue())

    huge = _json.dumps(["x"] * 100_000)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO agenda_topics (position, title, angle, constraints, priority, response_length, status, editorial_card_id, editorial_card_consumed) "
            "VALUES (50, 'Tema hostil', '', ?, 'normal', 'normal', 'queued', NULL, 0)",
            (huge,),
        )

    fresh = KiraAgendaController()
    restored = AgendaPersistence(db).load_into(fresh)

    assert restored == 3
    assert all(t.title != "Tema hostil" for t in fresh.topics)


def test_constructor_fails_open_when_directory_creation_fails(tmp_path: Path) -> None:
    with patch("opencohost.core.agenda_persistence.Path.mkdir", side_effect=PermissionError("denied")):
        persistence = AgendaPersistence(str(tmp_path / "sub" / "cards.db"))
    assert persistence is not None  # launch never breaks


def test_concurrent_saves_do_not_corrupt_disk(tmp_path: Path) -> None:
    """save_if_changed can be reached from the chat-source thread and the Tk
    thread; concurrent calls must serialize, never raise, and leave disk
    matching the controller."""
    import threading

    persistence, db = make_persistence(tmp_path)
    ctrl = controller_with_queue()
    errors: list[Exception] = []

    def worker() -> None:
        try:
            for _ in range(10):
                persistence.save_if_changed(ctrl)
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert errors == []
    fresh = KiraAgendaController()
    assert AgendaPersistence(db).load_into(fresh) == 3


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

    def test_app_shell_pushes_restored_settings_into_panel(self) -> None:
        assert "apply_session_settings" in self._source()

    def test_settings_push_happens_after_restore_not_inside_build_ui(self) -> None:
        """Crash regression (2026-06-12 17:02): apply_session_settings was
        called inside _build_ui, which runs BEFORE kira_agenda exists —
        the app died at launch (tkinter masks the missing attribute as a
        '_tkinter.tkapp has no attribute' error). The push must live in
        __init__, AFTER load_into populates the controller."""
        import ast

        tree = ast.parse(self._source())

        def attribute_calls(fn_node: ast.FunctionDef) -> list[str]:
            return [
                node.func.attr
                for node in ast.walk(fn_node)
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            ]

        app_cls = next(
            n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "VocalAIApp"
        )
        fns = {n.name: n for n in app_cls.body if isinstance(n, ast.FunctionDef)}

        assert "apply_session_settings" not in attribute_calls(fns["_build_ui"]), (
            "_build_ui runs before kira_agenda exists; pushing settings there crashes launch"
        )
        init_calls = attribute_calls(fns["__init__"])
        assert "apply_session_settings" in init_calls, "settings push missing from __init__"
        assert init_calls.index("load_into") < init_calls.index("apply_session_settings"), (
            "settings must be pushed AFTER the restore populates the controller"
        )

    def test_init_refreshes_agenda_ui_after_restore(self) -> None:
        """Runtime-gate regression (2026-06-12): topics restored correctly
        into the controller but the panel never rendered them — nothing
        triggered a status update after load_into, so the operator saw an
        empty queue and concluded the restore failed."""
        import ast

        tree = ast.parse(self._source())
        app_cls = next(
            n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "VocalAIApp"
        )
        init_fn = next(
            n for n in app_cls.body if isinstance(n, ast.FunctionDef) and n.name == "__init__"
        )
        init_calls = [
            node.func.attr
            for node in ast.walk(init_fn)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        ]
        assert "_kira_agenda_update_status" in init_calls, (
            "__init__ must refresh the agenda UI after restoring topics"
        )
        assert init_calls.index("apply_session_settings") < init_calls.index(
            "_kira_agenda_update_status"
        ), "the refresh must come after both restore and settings push"
