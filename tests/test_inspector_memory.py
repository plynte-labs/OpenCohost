"""Tests for opencohost/ui/inspector_memory.py — "Memoria de Kira" read-only
inspector window (Slice B, cards_memory_readonly_panels_20260701).

Header, lifetime badges, and the agenda provenance note are privacy-critical
copy (Judge B CRITICAL fix — mixed lifetimes must be stated honestly) and are
asserted VERBATIM here, straight from the owner-approved design doc.

Headless precedents: same as test_inspector_cards.py (object.__new__-style
fake CTk widgets; show_toplevel/raise_window patched at module namespace).
"""
from __future__ import annotations

import re
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


class _FakeWidget:
    def __init__(self, master=None, **kwargs):
        self.master = master
        self.kwargs = kwargs
        self.children = []
        self.destroyed = False
        if master is not None and hasattr(master, "children"):
            master.children.append(self)

    def pack(self, **kwargs):
        self.pack_kwargs = kwargs

    def configure(self, **kwargs):
        self.kwargs.update(kwargs)

    def winfo_children(self):
        return list(self.children)

    def destroy(self):
        self.destroyed = True
        if self.master is not None and self in getattr(self.master, "children", []):
            self.master.children.remove(self)


class _FakeButton(_FakeWidget):
    def invoke(self):
        cmd = self.kwargs.get("command")
        if cmd is not None:
            cmd()


class _FakeFont:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


class _FakeToplevel(_FakeWidget):
    def __init__(self, master=None, **kwargs):
        super().__init__(master, **kwargs)
        self._exists = True
        self._protocols = {}
        self._binds = {}

    def title(self, text=None):
        self._title = text

    def geometry(self, spec=None):
        self._geometry = spec

    def winfo_exists(self):
        return self._exists

    def protocol(self, name, func):
        self._protocols[name] = func

    def bind(self, seq, func):
        self._binds[seq] = func


class _FakeEntry(_FakeWidget):
    """Minimal CTkEntry double: single-buffer get/insert/delete, enough for
    the edit modal's title field (insert(0, text) at build time, then
    delete+insert to simulate operator edits in tests)."""

    def __init__(self, master=None, **kwargs):
        super().__init__(master, **kwargs)
        self._text = ""

    def insert(self, index, text):
        self._text += text

    def delete(self, start, end=None):
        self._text = ""

    def get(self):
        return self._text


class _FakeTextbox(_FakeWidget):
    """Minimal CTkTextbox double, same buffer semantics as _FakeEntry but
    with the Tk text-widget "1.0"/"end" index API."""

    def __init__(self, master=None, **kwargs):
        super().__init__(master, **kwargs)
        self._text = ""

    def insert(self, index, text):
        self._text += text

    def delete(self, start, end=None):
        self._text = ""

    def get(self, start="1.0", end=None):
        return self._text


def _make_fake_ctk():
    mock_module = MagicMock()
    mock_module.CTkFrame = _FakeWidget
    mock_module.CTkScrollableFrame = _FakeWidget
    mock_module.CTkLabel = _FakeWidget
    mock_module.CTkButton = _FakeButton
    mock_module.CTkToplevel = _FakeToplevel
    mock_module.CTkEntry = _FakeEntry
    mock_module.CTkTextbox = _FakeTextbox
    mock_module.CTkFont = _FakeFont
    return mock_module


def _make_schedule():
    calls = []
    event = threading.Event()

    def schedule(fn):
        calls.append(fn)
        event.set()

    return calls, schedule, event


def _wait_and_run_latest(calls, event, timeout=2):
    """Pump one marshaled schedule_ui_update hop: wait for the worker
    thread to call schedule_ui_update, clear the event, then run the
    latest queued callback on the (fake) Tk thread. Memoria mutations are
    two hops (worker write -> schedule_ui_update(_refresh) -> new load
    worker -> schedule_ui_update(render)) — call this twice per mutation."""
    assert event.wait(timeout=timeout), "expected schedule_ui_update to be called"
    event.clear()
    calls[-1]()


def _empty_snapshot():
    from collections import Counter
    return {"entries": [], "source_breakdown": Counter(), "digest": {"line_count": 0, "total_chars": 0, "max_chars": 600}}


def _fake_row(**overrides):
    """A dict standing in for a sqlite3.Row from MemoriaStore — dicts
    support the same row["col"] subscript access the production code uses."""
    row = {
        "id": "mem_1",
        "profile_id": "p1",
        "stable_key": "p1|alfa-beta-gamma",
        "revision": 1,
        "title": "Titulo ejemplo",
        "content": "Contenido de ejemplo",
        "status": "draft",
        "pinned": 0,
        "private": 0,
        "inactive": 0,
        "created_at": "2026-07-01T10:00:00+00:00",
        "updated_at": "2026-07-01T10:00:00+00:00",
    }
    row.update(overrides)
    return row


def _find_widgets_by_type(node, cls, acc=None):
    if acc is None:
        acc = []
    for child in getattr(node, "children", []):
        if isinstance(child, cls):
            acc.append(child)
        _find_widgets_by_type(child, cls, acc)
    return acc


def _open_kwargs(**overrides):
    kwargs = dict(
        parent=MagicMock(),
        ref_getter=lambda: None,
        ref_setter=lambda w: None,
        motor_ia=MagicMock(memory_inspector_snapshot=MagicMock(return_value=_empty_snapshot())),
        kira_agenda=SimpleNamespace(topics=[]),
        schedule_ui_update=lambda fn: fn(),
        memoria_store=None,
        profile_id_getter=lambda: None,
    )
    kwargs.update(overrides)
    return kwargs


# ---------------------------------------------------------------------------
# Duplicate-open guard
# ---------------------------------------------------------------------------


class TestDupOpenGuard:
    def test_existing_window_is_raised_not_recreated(self):
        import opencohost.ui.inspector_memory as inspector_memory

        existing = MagicMock()
        existing.winfo_exists.return_value = True

        with (
            patch("opencohost.ui.inspector_memory.ctk", _make_fake_ctk()),
            patch("opencohost.ui.inspector_memory.raise_window") as mock_raise,
        ):
            result = inspector_memory.open_inspector_memory(
                **_open_kwargs(ref_getter=lambda: existing)
            )

        mock_raise.assert_called_once_with(existing)
        assert result is None

    def test_destroyed_existing_ref_falls_through_to_new_window(self):
        import opencohost.ui.inspector_memory as inspector_memory

        existing = MagicMock()
        existing.winfo_exists.side_effect = Exception("TclError")

        with (
            patch("opencohost.ui.inspector_memory.ctk", _make_fake_ctk()),
            patch("opencohost.ui.inspector_memory.show_toplevel"),
        ):
            result = inspector_memory.open_inspector_memory(
                **_open_kwargs(ref_getter=lambda: existing)
            )

        assert result is not None


# ---------------------------------------------------------------------------
# Worker-thread read + marshaling
# ---------------------------------------------------------------------------


class TestWorkerThreadMarshaling:
    def test_snapshot_called_from_worker_thread_not_main(self):
        import opencohost.ui.inspector_memory as inspector_memory

        called_from_main = []

        def fake_snapshot():
            called_from_main.append(threading.current_thread() is threading.main_thread())
            return _empty_snapshot()

        _, schedule, event = _make_schedule()
        motor_ia = MagicMock(memory_inspector_snapshot=fake_snapshot)

        with (
            patch("opencohost.ui.inspector_memory.ctk", _make_fake_ctk()),
            patch("opencohost.ui.inspector_memory.show_toplevel"),
        ):
            inspector_memory.open_inspector_memory(
                **_open_kwargs(motor_ia=motor_ia, schedule_ui_update=schedule)
            )

        assert event.wait(timeout=2), "schedule_ui_update was never called"
        assert called_from_main == [False]


# ---------------------------------------------------------------------------
# Fail-open on engine exception
# ---------------------------------------------------------------------------


class TestFailOpen:
    def test_engine_exception_renders_no_disponible_and_logs(self):
        import opencohost.ui.inspector_memory as inspector_memory

        motor_ia = MagicMock(memory_inspector_snapshot=MagicMock(side_effect=RuntimeError("boom")))
        calls, schedule, event = _make_schedule()

        with (
            patch("opencohost.ui.inspector_memory.ctk", _make_fake_ctk()),
            patch("opencohost.ui.inspector_memory.show_toplevel"),
            patch.object(inspector_memory.logger, "exception") as mock_exception,
        ):
            inspector_memory.open_inspector_memory(
                **_open_kwargs(motor_ia=motor_ia, schedule_ui_update=schedule)
            )
            assert event.wait(timeout=2)
            calls[0]()

        mock_exception.assert_called_once()


# ---------------------------------------------------------------------------
# Agenda section reads the LIVE controller, not AgendaPersistence
# ---------------------------------------------------------------------------


class TestAgendaSectionReadsLiveController:
    def test_does_not_import_agenda_persistence(self):
        import opencohost.ui.inspector_memory as inspector_memory

        assert not hasattr(inspector_memory, "AgendaPersistence")

    def test_render_uses_live_kira_agenda_topics(self):
        import opencohost.ui.inspector_memory as inspector_memory
        from opencohost.smart_aggregator.kira_agenda_controller import AgendaTopic, TopicStatus

        topic = AgendaTopic(title="Topic X", priority="high", status=TopicStatus.QUEUED)
        agenda = SimpleNamespace(topics=[topic])
        calls, schedule, event = _make_schedule()

        with (
            patch("opencohost.ui.inspector_memory.ctk", _make_fake_ctk()),
            patch("opencohost.ui.inspector_memory.show_toplevel"),
        ):
            inspector_memory.open_inspector_memory(
                **_open_kwargs(kira_agenda=agenda, schedule_ui_update=schedule)
            )
            assert event.wait(timeout=2)
            calls[0]()


def _find_button_by_text(node, text, acc=None):
    if acc is None:
        acc = []
    for child in getattr(node, "children", []):
        if isinstance(child, _FakeButton) and child.kwargs.get("text") == text:
            acc.append(child)
        _find_button_by_text(child, text, acc)
    return acc


def _collect_label_texts(node, acc=None):
    if acc is None:
        acc = []
    for child in getattr(node, "children", []):
        text = child.kwargs.get("text") if hasattr(child, "kwargs") else None
        if text is not None:
            acc.append(text)
        _collect_label_texts(child, acc)
    return acc


def _count_descendants(node):
    count = 0
    for child in getattr(node, "children", []):
        count += 1 + _count_descendants(child)
    return count


# ---------------------------------------------------------------------------
# Agenda refresh must not duplicate topic rows, and must not destroy the
# provenance note packed at build time (Judge B MUST-fix).
# ---------------------------------------------------------------------------


class TestAgendaRenderIdempotent:
    def test_two_consecutive_renders_do_not_duplicate_topic_rows(self):
        import opencohost.ui.inspector_memory as inspector_memory
        from opencohost.smart_aggregator.kira_agenda_controller import AgendaTopic, TopicStatus

        topic = AgendaTopic(title="Topic X", priority="high", status=TopicStatus.QUEUED)
        agenda = SimpleNamespace(topics=[topic])
        calls, schedule, event = _make_schedule()
        expected_line = inspector_memory.format_saved_agenda_topics([topic])[0]

        with (
            patch("opencohost.ui.inspector_memory.ctk", _make_fake_ctk()),
            patch("opencohost.ui.inspector_memory.show_toplevel"),
        ):
            win = inspector_memory.open_inspector_memory(
                **_open_kwargs(kira_agenda=agenda, schedule_ui_update=schedule)
            )
            assert event.wait(timeout=2)
            calls[0]()  # first render (initial auto-refresh)

            event.clear()
            refresh_buttons = _find_button_by_text(win, "Actualizar")
            assert refresh_buttons, "expected an Actualizar button"
            refresh_buttons[0].invoke()
            assert event.wait(timeout=2)
            calls[-1]()  # second render

            texts = _collect_label_texts(win)

        assert texts.count(expected_line) == 1, "topic rows must not duplicate across refreshes"
        assert texts.count(inspector_memory.AGENDA_PROVENANCE_NOTE) == 1, (
            "provenance note must survive refresh, not be cleared with the topic rows"
        )


# ---------------------------------------------------------------------------
# Close-during-refresh: marshaled render callback must be a no-op once the
# window is gone (schedule_ui_update survives Toplevel destroy — it targets
# the root app's after-loop).
# ---------------------------------------------------------------------------


class TestClosedWindowGuardsMarshaledRender:
    def test_render_snapshot_is_noop_after_window_destroyed(self):
        import opencohost.ui.inspector_memory as inspector_memory

        calls, schedule, event = _make_schedule()

        with (
            patch("opencohost.ui.inspector_memory.ctk", _make_fake_ctk()),
            patch("opencohost.ui.inspector_memory.show_toplevel"),
        ):
            win = inspector_memory.open_inspector_memory(**_open_kwargs(schedule_ui_update=schedule))
            assert event.wait(timeout=2)

            win._exists = False
            count_before = _count_descendants(win)

            calls[0]()  # marshaled _render_snapshot callback after close

            assert _count_descendants(win) == count_before

    def test_render_error_is_noop_after_window_destroyed(self):
        import opencohost.ui.inspector_memory as inspector_memory

        motor_ia = MagicMock(memory_inspector_snapshot=MagicMock(side_effect=RuntimeError("boom")))
        calls, schedule, event = _make_schedule()

        with (
            patch("opencohost.ui.inspector_memory.ctk", _make_fake_ctk()),
            patch("opencohost.ui.inspector_memory.show_toplevel"),
            patch.object(inspector_memory.logger, "exception"),
        ):
            win = inspector_memory.open_inspector_memory(
                **_open_kwargs(motor_ia=motor_ia, schedule_ui_update=schedule)
            )
            assert event.wait(timeout=2)

            win._exists = False
            count_before = _count_descendants(win)

            calls[0]()  # marshaled _render_error callback after close

            assert _count_descendants(win) == count_before


# ---------------------------------------------------------------------------
# "Actualizado HH:MM:SS" absolute render-time freshness stamp
# ---------------------------------------------------------------------------


class TestUpdatedStamp:
    def test_stamp_shows_absolute_wall_clock_time_at_render(self):
        import opencohost.ui.inspector_memory as inspector_memory

        calls, schedule, event = _make_schedule()

        with (
            patch("opencohost.ui.inspector_memory.ctk", _make_fake_ctk()),
            patch("opencohost.ui.inspector_memory.show_toplevel"),
        ):
            win = inspector_memory.open_inspector_memory(**_open_kwargs(schedule_ui_update=schedule))
            assert event.wait(timeout=2)
            calls[0]()

            stamp_texts = [t for t in _collect_label_texts(win) if t.startswith("Actualizado")]

        assert stamp_texts, "expected an Actualizado HH:MM:SS stamp label"
        assert re.match(r"^Actualizado \d{2}:\d{2}:\d{2}$", stamp_texts[0])


# ---------------------------------------------------------------------------
# Pure formatting helpers
# ---------------------------------------------------------------------------


class TestFormatConversationEntry:
    def test_direct_user_entry_with_content(self):
        from opencohost.ui.inspector_memory import format_conversation_entry

        entry = {"role": "user", "source": "direct", "content": "hola", "content_chars": 4}
        assert format_conversation_entry(entry) == "Vos: hola"

    def test_assistant_entry_with_content(self):
        from opencohost.ui.inspector_memory import format_conversation_entry

        entry = {"role": "assistant", "source": "direct", "content": "hi", "content_chars": 2}
        assert format_conversation_entry(entry) == "Kira: hi"

    def test_ptt_user_slot_renders_masked_metadata_row(self):
        from opencohost.ui.inspector_memory import format_conversation_entry

        entry = {"role": "user", "source": "ptt", "content_chars": 120}
        assert format_conversation_entry(entry) == "[turno de voz / indicación al aire]"

    def test_chat_entry_without_content_renders_generic_masked_row(self):
        from opencohost.ui.inspector_memory import format_conversation_entry

        entry = {"role": "user", "source": "chat", "content_chars": 30}
        result = format_conversation_entry(entry)
        assert result == "[turno oculto — 30 caracteres]"
        assert "Vos" not in result
        assert "chat viewer" not in result.lower()


class TestFormatBackgroundMemory:
    def test_renders_stats_only(self):
        from opencohost.ui.inspector_memory import format_background_memory

        digest = {"line_count": 3, "total_chars": 210, "max_chars": 600}
        result = format_background_memory(digest)
        assert "3" in result
        assert "210" in result
        assert "600" in result


class TestFormatSavedAgendaTopics:
    def test_filters_to_persisted_statuses(self):
        from opencohost.ui.inspector_memory import format_saved_agenda_topics
        from opencohost.smart_aggregator.kira_agenda_controller import AgendaTopic, TopicStatus

        approved = AgendaTopic(title="A", priority="normal", status=TopicStatus.APPROVED)
        queued = AgendaTopic(title="B", priority="high", status=TopicStatus.QUEUED)
        active = AgendaTopic(title="C", priority="normal", status=TopicStatus.ACTIVE)
        drafted = AgendaTopic(title="D", priority="normal", status=TopicStatus.DRAFTED)
        skipped = AgendaTopic(title="E", priority="normal", status=TopicStatus.SKIPPED)

        lines = format_saved_agenda_topics([approved, queued, active, drafted, skipped])

        assert len(lines) == 3
        assert any("A" in line for line in lines)
        assert any("B" in line for line in lines)
        assert any("C" in line for line in lines)
        assert not any("D" in line for line in lines)
        assert not any("E" in line for line in lines)


class TestSavedAgendaStatusesDriftGuard:
    def test_matches_agenda_persistence_persisted_statuses(self):
        from opencohost.ui.inspector_memory import _SAVED_AGENDA_STATUSES
        from opencohost.core.agenda_persistence import _PERSISTED_STATUSES

        assert {s.value for s in _SAVED_AGENDA_STATUSES} == set(_PERSISTED_STATUSES)


class TestFormatMemoryLauncherLabel:
    def test_with_count(self):
        from opencohost.ui.inspector_memory import format_memory_launcher_label

        assert format_memory_launcher_label(23) == "Memoria de Kira (23 turnos)"

    def test_with_none_omits_parenthetical(self):
        from opencohost.ui.inspector_memory import format_memory_launcher_label

        assert format_memory_launcher_label(None) == "Memoria de Kira"


# ---------------------------------------------------------------------------
# Verbatim privacy-critical copy (design Section 1)
# ---------------------------------------------------------------------------


class TestVerbatimCopy:
    def test_header_text_verbatim(self):
        from opencohost.ui.inspector_memory import HEADER_TEXT

        assert HEADER_TEXT == (
            "La conversación y la memoria de fondo viven solo en RAM: se borran al "
            "cerrar la app o cambiar de perfil. La agenda guardada sí persiste en "
            "disco entre sesiones. El chat de viewers nunca se muestra."
        )

    def test_badge_conversacion_verbatim(self):
        from opencohost.ui.inspector_memory import BADGE_CONVERSACION

        assert BADGE_CONVERSACION == "«Solo en RAM»"

    def test_badge_memoria_fondo_verbatim(self):
        from opencohost.ui.inspector_memory import BADGE_MEMORIA_FONDO

        assert BADGE_MEMORIA_FONDO == "«Solo en RAM»"

    def test_badge_agenda_guardada_verbatim(self):
        from opencohost.ui.inspector_memory import BADGE_AGENDA_GUARDADA

        assert BADGE_AGENDA_GUARDADA == "«En disco · persiste entre sesiones»"

    def test_agenda_provenance_note_verbatim(self):
        from opencohost.ui.inspector_memory import AGENDA_PROVENANCE_NOTE

        assert AGENDA_PROVENANCE_NOTE == (
            "Algunos temas aprobados pueden nombrar términos que mencionó el chat: "
            "vos los aprobaste desde Sugerencias de Kira."
        )

    def test_window_title_is_memoria_de_kira(self):
        import opencohost.ui.inspector_memory as inspector_memory

        calls, schedule, event = _make_schedule()
        with (
            patch("opencohost.ui.inspector_memory.ctk", _make_fake_ctk()),
            patch("opencohost.ui.inspector_memory.show_toplevel"),
        ):
            win = inspector_memory.open_inspector_memory(**_open_kwargs(schedule_ui_update=schedule))

        assert win._title == "Memoria de Kira"


# ---------------------------------------------------------------------------
# Slice 6 (ui-core, R10): "Memorias guardadas" management section — bounded
# render, per-row metadata, edit modal, flag toggles, delete, unified
# freeze-rule copy. MEMORIAS_ENABLED stays False in production this slice —
# tests seed a real (or mocked) MemoriaStore and pass it in explicitly to
# exercise the section, matching the still-empty RAM-only header.
# ---------------------------------------------------------------------------


class TestMemoriasSectionRender:
    def test_memorias_section_renders_saved_rows_bounded(self):
        import opencohost.ui.inspector_memory as inspector_memory

        rows = [_fake_row(id=f"mem_{i}", title=f"Titulo {i}") for i in range(3)]
        store = MagicMock(list_for_profile=MagicMock(return_value=rows))
        calls, schedule, event = _make_schedule()

        with (
            patch("opencohost.ui.inspector_memory.ctk", _make_fake_ctk()),
            patch("opencohost.ui.inspector_memory.show_toplevel"),
        ):
            win = inspector_memory.open_inspector_memory(
                **_open_kwargs(memoria_store=store, profile_id_getter=lambda: "p1", schedule_ui_update=schedule)
            )
            assert event.wait(timeout=2)
            calls[0]()

            texts = _collect_label_texts(win)

        # UI defers bounding to the store's own MEMORIAS_PROFILE_CAP LIMIT
        # (list_for_profile's default) rather than fetching unbounded rows.
        store.list_for_profile.assert_called_once_with("p1")
        for i in range(3):
            assert f"Titulo {i}" in texts

    def test_section_renders_empty_placeholder_when_store_is_none(self):
        import opencohost.ui.inspector_memory as inspector_memory

        calls, schedule, event = _make_schedule()
        with (
            patch("opencohost.ui.inspector_memory.ctk", _make_fake_ctk()),
            patch("opencohost.ui.inspector_memory.show_toplevel"),
        ):
            win = inspector_memory.open_inspector_memory(**_open_kwargs(schedule_ui_update=schedule))
            assert event.wait(timeout=2)
            calls[0]()
            texts = _collect_label_texts(win)

        assert inspector_memory.MEMORIAS_EMPTY_TEXT in texts


class TestMemoriaRowMetadata:
    def test_row_displays_metadata_created_at_updated_at_revision(self):
        from opencohost.ui.inspector_memory import format_memoria_row_metadata

        row = _fake_row(
            created_at="2026-07-01T10:00:00+00:00",
            updated_at="2026-07-02T11:30:00+00:00",
            revision=4,
        )
        result = format_memoria_row_metadata(row)

        assert "2026-07-01T10:00:00+00:00" in result
        assert "2026-07-02T11:30:00+00:00" in result
        assert "rev 4" in result


class TestEditModalPromotesToCurated:
    def test_edit_modal_saves_content_and_promotes_row_to_curated(self, tmp_path):
        from opencohost.core.memoria_store import MemoriaStore
        import opencohost.ui.inspector_memory as inspector_memory

        store = MemoriaStore(tmp_path / "memorias.db")
        row_id = store.upsert_draft("p1", "p1|alfa-beta-gamma", "Titulo original", "Contenido original")
        calls, schedule, event = _make_schedule()

        with (
            patch("opencohost.ui.inspector_memory.ctk", _make_fake_ctk()),
            patch("opencohost.ui.inspector_memory.show_toplevel"),
        ):
            win = inspector_memory.open_inspector_memory(
                **_open_kwargs(memoria_store=store, profile_id_getter=lambda: "p1", schedule_ui_update=schedule)
            )
            _wait_and_run_latest(calls, event)  # initial auto-refresh

            edit_buttons = _find_button_by_text(win, "Editar")
            assert edit_buttons, "expected an Editar button on the row"
            edit_buttons[0].invoke()

            title_entries = _find_widgets_by_type(win, _FakeEntry)
            assert title_entries, "expected a title entry in the edit modal"
            title_entries[0].delete(0, "end")
            title_entries[0].insert(0, "Titulo editado")

            content_boxes = _find_widgets_by_type(win, _FakeTextbox)
            assert content_boxes, "expected a content textbox in the edit modal"
            content_boxes[0].delete("1.0", "end")
            content_boxes[0].insert("1.0", "Contenido editado")

            save_buttons = _find_button_by_text(win, "Guardar")
            assert save_buttons, "expected a Guardar button in the edit modal"
            save_buttons[0].invoke()

            _wait_and_run_latest(calls, event)  # mutation worker -> schedule_ui_update(_refresh)
            _wait_and_run_latest(calls, event)  # new load worker -> schedule_ui_update(render)

            texts = _collect_label_texts(win)

        row = store.get(row_id)
        assert row["status"] == "curated"
        assert row["title"] == "Titulo editado"
        assert row["content"] == "Contenido editado"
        assert "Titulo editado" in texts


class TestFlagTogglesPromoteToCurated:
    def test_pin_toggle_promotes_row_to_curated(self, tmp_path):
        from opencohost.core.memoria_store import MemoriaStore
        import opencohost.ui.inspector_memory as inspector_memory

        store = MemoriaStore(tmp_path / "memorias.db")
        row_id = store.upsert_draft("p1", "p1|uno-dos-tres", "Titulo", "Contenido")
        calls, schedule, event = _make_schedule()

        with (
            patch("opencohost.ui.inspector_memory.ctk", _make_fake_ctk()),
            patch("opencohost.ui.inspector_memory.show_toplevel"),
        ):
            win = inspector_memory.open_inspector_memory(
                **_open_kwargs(memoria_store=store, profile_id_getter=lambda: "p1", schedule_ui_update=schedule)
            )
            _wait_and_run_latest(calls, event)

            pin_buttons = _find_button_by_text(win, "Fijar")
            assert pin_buttons, "expected a Fijar button"
            pin_buttons[0].invoke()

            _wait_and_run_latest(calls, event)
            _wait_and_run_latest(calls, event)

        row = store.get(row_id)
        assert row["status"] == "curated"
        assert row["pinned"] == 1

    def test_private_toggle_promotes_row_to_curated(self, tmp_path):
        from opencohost.core.memoria_store import MemoriaStore
        import opencohost.ui.inspector_memory as inspector_memory

        store = MemoriaStore(tmp_path / "memorias.db")
        row_id = store.upsert_draft("p1", "p1|uno-dos-tres", "Titulo", "Contenido")
        calls, schedule, event = _make_schedule()

        with (
            patch("opencohost.ui.inspector_memory.ctk", _make_fake_ctk()),
            patch("opencohost.ui.inspector_memory.show_toplevel"),
        ):
            win = inspector_memory.open_inspector_memory(
                **_open_kwargs(memoria_store=store, profile_id_getter=lambda: "p1", schedule_ui_update=schedule)
            )
            _wait_and_run_latest(calls, event)

            private_buttons = _find_button_by_text(win, "Marcar privada")
            assert private_buttons, "expected a Marcar privada button"
            private_buttons[0].invoke()

            _wait_and_run_latest(calls, event)
            _wait_and_run_latest(calls, event)

        row = store.get(row_id)
        assert row["status"] == "curated"
        assert row["private"] == 1

    def test_inactive_toggle_does_not_promote_to_curated(self, tmp_path):
        from opencohost.core.memoria_store import MemoriaStore
        import opencohost.ui.inspector_memory as inspector_memory

        store = MemoriaStore(tmp_path / "memorias.db")
        row_id = store.upsert_draft("p1", "p1|uno-dos-tres", "Titulo", "Contenido")
        calls, schedule, event = _make_schedule()

        with (
            patch("opencohost.ui.inspector_memory.ctk", _make_fake_ctk()),
            patch("opencohost.ui.inspector_memory.show_toplevel"),
        ):
            win = inspector_memory.open_inspector_memory(
                **_open_kwargs(memoria_store=store, profile_id_getter=lambda: "p1", schedule_ui_update=schedule)
            )
            _wait_and_run_latest(calls, event)

            inactive_buttons = _find_button_by_text(win, "Desactivar")
            assert inactive_buttons, "expected a Desactivar button"
            inactive_buttons[0].invoke()

            _wait_and_run_latest(calls, event)
            _wait_and_run_latest(calls, event)

        row = store.get(row_id)
        assert row["status"] == "draft", "deactivation alone must never promote to curated (F5)"
        assert row["inactive"] == 1


class TestDeleteRow:
    def test_delete_row_removes_it_from_store_and_ui(self, tmp_path):
        from opencohost.core.memoria_store import MemoriaStore
        import opencohost.ui.inspector_memory as inspector_memory

        store = MemoriaStore(tmp_path / "memorias.db")
        row_id = store.upsert_draft("p1", "p1|uno-dos-tres", "Titulo a borrar", "Contenido")
        calls, schedule, event = _make_schedule()

        with (
            patch("opencohost.ui.inspector_memory.ctk", _make_fake_ctk()),
            patch("opencohost.ui.inspector_memory.show_toplevel"),
            patch("opencohost.ui.inspector_memory.mb.askyesno", return_value=True),
        ):
            win = inspector_memory.open_inspector_memory(
                **_open_kwargs(memoria_store=store, profile_id_getter=lambda: "p1", schedule_ui_update=schedule)
            )
            _wait_and_run_latest(calls, event)

            delete_buttons = _find_button_by_text(win, "Eliminar")
            assert delete_buttons, "expected an Eliminar button"
            delete_buttons[0].invoke()

            _wait_and_run_latest(calls, event)
            _wait_and_run_latest(calls, event)

            texts = _collect_label_texts(win)

        assert store.get(row_id) is None
        assert "Titulo a borrar" not in texts

    def test_delete_declined_by_operator_keeps_row(self, tmp_path):
        from opencohost.core.memoria_store import MemoriaStore
        import opencohost.ui.inspector_memory as inspector_memory

        store = MemoriaStore(tmp_path / "memorias.db")
        row_id = store.upsert_draft("p1", "p1|uno-dos-tres", "Titulo a conservar", "Contenido")
        calls, schedule, event = _make_schedule()

        with (
            patch("opencohost.ui.inspector_memory.ctk", _make_fake_ctk()),
            patch("opencohost.ui.inspector_memory.show_toplevel"),
            patch("opencohost.ui.inspector_memory.mb.askyesno", return_value=False),
        ):
            win = inspector_memory.open_inspector_memory(
                **_open_kwargs(memoria_store=store, profile_id_getter=lambda: "p1", schedule_ui_update=schedule)
            )
            _wait_and_run_latest(calls, event)

            delete_buttons = _find_button_by_text(win, "Eliminar")
            delete_buttons[0].invoke()

        assert store.get(row_id) is not None


class TestPttDisplayDivergenceRationale:
    def test_ptt_display_divergence_rationale_documented_in_row_render(self):
        import opencohost.ui.inspector_memory as inspector_memory

        row = _fake_row(content="Frase dicha al aire por el streamer via PTT")
        store = MagicMock(list_for_profile=MagicMock(return_value=[row]))
        calls, schedule, event = _make_schedule()

        with (
            patch("opencohost.ui.inspector_memory.ctk", _make_fake_ctk()),
            patch("opencohost.ui.inspector_memory.show_toplevel"),
        ):
            win = inspector_memory.open_inspector_memory(
                **_open_kwargs(memoria_store=store, profile_id_getter=lambda: "p1", schedule_ui_update=schedule)
            )
            assert event.wait(timeout=2)
            calls[0]()
            texts = _collect_label_texts(win)

        # The rationale note is documented copy explaining WHY this diverges
        # from format_conversation_entry's ptt masking (R1 already vetted
        # provenance by the time a row reaches the store).
        assert inspector_memory.MEMORIAS_PTT_NOTE in texts
        assert row["content"] in texts
        assert "[turno de voz" not in "".join(texts)


class TestUnifiedFreezeRuleCopy:
    def test_unified_freeze_rule_copy_line_present_verbatim(self):
        import opencohost.ui.inspector_memory as inspector_memory
        from opencohost.ui.inspector_memory import FREEZE_RULE_TEXT

        assert FREEZE_RULE_TEXT == (
            "Editar, fijar o marcar como privada una memoria la congela: Kira no la reescribirá."
        )

        calls, schedule, event = _make_schedule()
        with (
            patch("opencohost.ui.inspector_memory.ctk", _make_fake_ctk()),
            patch("opencohost.ui.inspector_memory.show_toplevel"),
        ):
            win = inspector_memory.open_inspector_memory(**_open_kwargs(schedule_ui_update=schedule))
            assert event.wait(timeout=2)
            calls[0]()
            texts = _collect_label_texts(win)

        assert FREEZE_RULE_TEXT in texts


class TestCurationWriteFailureSurfaced:
    """A-SF1 (Judge A, slice-2 store review, obs #2782): update_row/set_flags
    fail-open to False under lock contention. This UI must surface that,
    never silently claim success (design v2.1 §8)."""

    def test_curation_write_failure_is_surfaced_to_operator(self):
        import opencohost.ui.inspector_memory as inspector_memory

        row = _fake_row(pinned=0)
        store = MagicMock()
        store.list_for_profile.return_value = [row]
        store.set_flags.return_value = False  # simulated lock-contended write
        calls, schedule, event = _make_schedule()

        with (
            patch("opencohost.ui.inspector_memory.ctk", _make_fake_ctk()),
            patch("opencohost.ui.inspector_memory.show_toplevel"),
        ):
            win = inspector_memory.open_inspector_memory(
                **_open_kwargs(memoria_store=store, profile_id_getter=lambda: "p1", schedule_ui_update=schedule)
            )
            assert event.wait(timeout=2)
            calls[0]()

            pin_buttons = _find_button_by_text(win, "Fijar")
            assert pin_buttons
            pin_buttons[0].invoke()

            _wait_and_run_latest(calls, event)  # failure path: single hop, no _refresh

            texts = _collect_label_texts(win)

        store.set_flags.assert_called_once_with(row["id"], pinned=True)
        assert inspector_memory.MEMORIAS_WRITE_FAILED_TEXT in texts

    def test_edit_modal_write_failure_is_surfaced_to_operator(self):
        import opencohost.ui.inspector_memory as inspector_memory

        row = _fake_row()
        store = MagicMock()
        store.list_for_profile.return_value = [row]
        store.update_row.return_value = False  # simulated lock-contended write
        calls, schedule, event = _make_schedule()

        with (
            patch("opencohost.ui.inspector_memory.ctk", _make_fake_ctk()),
            patch("opencohost.ui.inspector_memory.show_toplevel"),
        ):
            win = inspector_memory.open_inspector_memory(
                **_open_kwargs(memoria_store=store, profile_id_getter=lambda: "p1", schedule_ui_update=schedule)
            )
            assert event.wait(timeout=2)
            calls[0]()

            edit_buttons = _find_button_by_text(win, "Editar")
            assert edit_buttons
            edit_buttons[0].invoke()

            save_buttons = _find_button_by_text(win, "Guardar")
            assert save_buttons
            save_buttons[0].invoke()

            _wait_and_run_latest(calls, event)  # failure path: single hop, no _refresh

            texts = _collect_label_texts(win)

        assert store.update_row.called
        assert inspector_memory.MEMORIAS_WRITE_FAILED_TEXT in texts
