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
        from opencohost.core.agenda.agenda_persistence import _PERSISTED_STATUSES

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
        """Slice 8 (flip+disclosure, R13): rewritten so it no longer implies
        EVERYTHING is RAM-only — memorias guardadas now persist too, same as
        the agenda."""
        from opencohost.ui.inspector_memory import HEADER_TEXT

        assert HEADER_TEXT == (
            "La conversación y la memoria de fondo viven solo en RAM: se borran al "
            "cerrar la app o cambiar de perfil. Las memorias guardadas y la agenda "
            "guardada sí persisten en disco entre sesiones. El chat de viewers nunca "
            "se muestra."
        )

    def test_badge_memorias_verbatim(self):
        """Slice 8 (task 8.11, R13): the «En preparación» placeholder from
        slice 6 becomes the real, accurate on-disk badge once MEMORIAS_ENABLED
        flips True — same wording as BADGE_AGENDA_GUARDADA."""
        from opencohost.ui.inspector_memory import BADGE_MEMORIAS

        assert BADGE_MEMORIAS == "«En disco · persiste entre sesiones»"

    def test_memorias_provenance_note_verbatim(self):
        """Slice 8 (task 8.12, R13): durable analogue of
        AGENDA_PROVENANCE_NOTE — memorias come only from the streamer's own
        direct/voice turns, never viewer chat, but may name terms from
        chat-originated topics the operator already approved; uses
        "extractos" (structural truncation, B-N1), never "destiladas"."""
        from opencohost.ui.inspector_memory import MEMORIAS_PROVENANCE_NOTE

        assert MEMORIAS_PROVENANCE_NOTE == (
            "Las memorias se arman solo con tus propios turnos (voz o texto directo), "
            "nunca del chat de viewers, aunque pueden nombrar términos de temas que vos "
            "aprobaste desde Sugerencias de Kira. Son extractos que persisten entre "
            "sesiones."
        )
        assert "destiladas" not in MEMORIAS_PROVENANCE_NOTE

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
# freeze-rule copy. Written when MEMORIAS_ENABLED still defaulted False;
# slice 8 flips it True in production, but these tests keep passing a real
# (or mocked) MemoriaStore explicitly regardless of the flag's value.
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


class TestMemoriasRenderIdempotent:
    """A-N4: memorias-rows analog of TestAgendaRenderIdempotent — two
    consecutive renders against a fixed row set must not duplicate rows."""

    def test_two_consecutive_renders_do_not_duplicate_memoria_rows(self):
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
            calls[0]()  # first render (initial auto-refresh)

            event.clear()
            refresh_buttons = _find_button_by_text(win, "Actualizar")
            assert refresh_buttons, "expected an Actualizar button"
            refresh_buttons[0].invoke()
            assert event.wait(timeout=2)
            calls[-1]()  # second render

            texts = _collect_label_texts(win)

        for i in range(3):
            assert texts.count(f"Titulo {i}") == 1, "memoria rows must not duplicate across refreshes"
        assert texts.count(inspector_memory.MEMORIAS_PTT_NOTE) == 1, (
            "PTT note must survive refresh, not be cleared with the memoria rows"
        )
        assert texts.count(inspector_memory.FREEZE_RULE_TEXT) == 1, (
            "freeze-rule note must survive refresh, not be cleared with the memoria rows"
        )


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
        from opencohost.core.memory.memoria_store import MemoriaStore
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
        from opencohost.core.memory.memoria_store import MemoriaStore
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
        from opencohost.core.memory.memoria_store import MemoriaStore
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
        from opencohost.core.memory.memoria_store import MemoriaStore
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
        from opencohost.core.memory.memoria_store import MemoriaStore
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
        from opencohost.core.memory.memoria_store import MemoriaStore
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
            event.clear()  # A-N1: consume the initial-refresh signal so the
            # next _wait_and_run_latest below waits for the mutation
            # worker's OWN schedule_ui_update call instead of finding the
            # (already-set) event and racing calls[-1]() before the worker
            # thread has appended its error callback.

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
            event.clear()  # A-N1: see rationale in the analogous set_flags test above.

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


# ---------------------------------------------------------------------------
# Slice 7 (ui-purge+switch): active-profile purge, capture switch + RC-4
# copy, F6b honest pinned counter. Written when MEMORIAS_ENABLED still
# defaulted False; switch/indicator tests explicitly monkeypatch the flag to
# the value each test needs (design v2.1 §5/§8, B-SF6: the whole switch UI
# is absent, not just empty, while the flag is False) so they stay correct
# after slice 8 flips the production default to True.
# ---------------------------------------------------------------------------


class TestPurgeActiveProfile:
    def test_purge_button_scoped_to_active_profile_only_with_confirm_dialog(self, tmp_path):
        from opencohost.core.memory.memoria_store import MemoriaStore
        import opencohost.ui.inspector_memory as inspector_memory

        store = MemoriaStore(tmp_path / "memorias.db")
        store.upsert_draft("p1", "p1|uno-dos-tres", "Titulo P1", "Contenido P1")
        store.upsert_draft("p2", "p2|siete-ocho-nueve", "Titulo P2", "Contenido P2")
        calls, schedule, event = _make_schedule()

        with (
            patch("opencohost.ui.inspector_memory.ctk", _make_fake_ctk()),
            patch("opencohost.ui.inspector_memory.show_toplevel"),
            patch("opencohost.ui.inspector_memory.mb.askyesno", return_value=True) as mock_confirm,
        ):
            win = inspector_memory.open_inspector_memory(
                **_open_kwargs(memoria_store=store, profile_id_getter=lambda: "p1", schedule_ui_update=schedule)
            )
            _wait_and_run_latest(calls, event)

            purge_buttons = _find_button_by_text(win, inspector_memory.MEMORIAS_PURGE_BUTTON_TEXT)
            assert purge_buttons, "expected a purge button"
            purge_buttons[0].invoke()

            _wait_and_run_latest(calls, event)  # mutation worker -> schedule_ui_update(_refresh)
            _wait_and_run_latest(calls, event)  # new load worker -> schedule_ui_update(render)

        assert mock_confirm.called
        assert store.list_for_profile("p1") == []
        assert len(store.list_for_profile("p2")) == 1, "purge must never touch a different profile"

    def test_purge_shows_row_count_and_curated_warning_before_confirm(self, tmp_path):
        from opencohost.core.memory.memoria_store import MemoriaStore
        import opencohost.ui.inspector_memory as inspector_memory

        store = MemoriaStore(tmp_path / "memorias.db")
        store.upsert_draft("p1", "p1|uno-dos-tres", "Titulo A", "Contenido A")
        store.upsert_draft("p1", "p1|cuatro-cinco-seis", "Titulo B", "Contenido B")
        calls, schedule, event = _make_schedule()

        with (
            patch("opencohost.ui.inspector_memory.ctk", _make_fake_ctk()),
            patch("opencohost.ui.inspector_memory.show_toplevel"),
            patch("opencohost.ui.inspector_memory.mb.askyesno", return_value=False) as mock_confirm,
        ):
            win = inspector_memory.open_inspector_memory(
                **_open_kwargs(memoria_store=store, profile_id_getter=lambda: "p1", schedule_ui_update=schedule)
            )
            _wait_and_run_latest(calls, event)

            purge_buttons = _find_button_by_text(win, inspector_memory.MEMORIAS_PURGE_BUTTON_TEXT)
            purge_buttons[0].invoke()

        assert mock_confirm.called
        message = mock_confirm.call_args[0][1]
        assert "2" in message
        assert "fijadas" in message.lower()
        assert "curadas" in message.lower()
        # declined -> nothing purged
        assert len(store.list_for_profile("p1")) == 2

    def test_purge_confirm_uses_uncapped_count_not_capped_list_length(self, tmp_path):
        """SF-1: the confirm count must come from count_all (uncapped), not
        len(list_for_profile(...)) which caps display reads at
        MEMORIAS_PROFILE_CAP — a >200-row profile must show its true count."""
        import opencohost.ui.inspector_memory as inspector_memory

        store = MagicMock()
        store.count_all.return_value = 205
        calls, schedule, event = _make_schedule()

        with (
            patch("opencohost.ui.inspector_memory.ctk", _make_fake_ctk()),
            patch("opencohost.ui.inspector_memory.show_toplevel"),
            patch("opencohost.ui.inspector_memory.mb.askyesno", return_value=False) as mock_confirm,
        ):
            win = inspector_memory.open_inspector_memory(
                **_open_kwargs(memoria_store=store, profile_id_getter=lambda: "p1", schedule_ui_update=schedule)
            )
            _wait_and_run_latest(calls, event)

            purge_buttons = _find_button_by_text(win, inspector_memory.MEMORIAS_PURGE_BUTTON_TEXT)
            purge_buttons[0].invoke()

        message = mock_confirm.call_args[0][1]
        assert "205" in message
        store.count_all.assert_called_once_with("p1")

    def test_purge_confirm_shows_honest_unknown_count_on_count_read_exception(self, tmp_path):
        """SF-1 facet 2: a count-read failure must NEVER show "0" — that
        would misleadingly suggest nothing is at stake while purge_profile
        still deletes every row for the profile."""
        import opencohost.ui.inspector_memory as inspector_memory

        store = MagicMock()
        store.count_all.side_effect = RuntimeError("boom")
        calls, schedule, event = _make_schedule()

        with (
            patch("opencohost.ui.inspector_memory.ctk", _make_fake_ctk()),
            patch("opencohost.ui.inspector_memory.show_toplevel"),
            patch("opencohost.ui.inspector_memory.mb.askyesno", return_value=False) as mock_confirm,
        ):
            win = inspector_memory.open_inspector_memory(
                **_open_kwargs(memoria_store=store, profile_id_getter=lambda: "p1", schedule_ui_update=schedule)
            )
            _wait_and_run_latest(calls, event)

            purge_buttons = _find_button_by_text(win, inspector_memory.MEMORIAS_PURGE_BUTTON_TEXT)
            purge_buttons[0].invoke()

        assert mock_confirm.called
        message = mock_confirm.call_args[0][1]
        assert message == inspector_memory.MEMORIAS_PURGE_CONFIRM_UNKNOWN_COUNT_TEXT
        assert "0" not in message


class TestCaptureSwitch:
    def _make_motor_ia(self, paused: bool):
        return MagicMock(
            memory_inspector_snapshot=MagicMock(return_value=_empty_snapshot()),
            memorias_private=paused,
        )

    def test_capture_switch_toggle_flips_session_scoped_private_flag(self):
        import opencohost.ui.inspector_memory as inspector_memory

        motor_ia = self._make_motor_ia(paused=False)
        calls, schedule, event = _make_schedule()

        with (
            patch("opencohost.ui.inspector_memory.ctk", _make_fake_ctk()),
            patch("opencohost.ui.inspector_memory.show_toplevel"),
            patch("opencohost.ui.inspector_memory.MEMORIAS_ENABLED", True),
        ):
            win = inspector_memory.open_inspector_memory(
                **_open_kwargs(motor_ia=motor_ia, schedule_ui_update=schedule)
            )
            assert event.wait(timeout=2)
            calls[0]()

            switch_buttons = _find_button_by_text(win, "Pausar guardado")
            assert switch_buttons, "expected a Pausar guardado switch button"
            switch_buttons[0].invoke()

        motor_ia.set_memorias_private.assert_called_once_with(True)

    def test_capture_indicator_label_reflects_current_switch_state_on_off(self):
        import opencohost.ui.inspector_memory as inspector_memory

        motor_ia = self._make_motor_ia(paused=False)
        calls, schedule, event = _make_schedule()

        with (
            patch("opencohost.ui.inspector_memory.ctk", _make_fake_ctk()),
            patch("opencohost.ui.inspector_memory.show_toplevel"),
            patch("opencohost.ui.inspector_memory.MEMORIAS_ENABLED", True),
        ):
            win = inspector_memory.open_inspector_memory(
                **_open_kwargs(motor_ia=motor_ia, schedule_ui_update=schedule)
            )
            assert event.wait(timeout=2)
            calls[0]()

            assert inspector_memory.MEMORIAS_INDICATOR_ON in _collect_label_texts(win)

            switch_buttons = _find_button_by_text(win, "Pausar guardado")
            switch_buttons[0].invoke()

            texts = _collect_label_texts(win)

        assert inspector_memory.MEMORIAS_INDICATOR_PAUSED in texts
        assert inspector_memory.MEMORIAS_INDICATOR_ON not in texts

    def test_indicator_hidden_when_memorias_enabled_flag_false(self):
        import opencohost.ui.inspector_memory as inspector_memory

        calls, schedule, event = _make_schedule()
        with (
            patch("opencohost.ui.inspector_memory.ctk", _make_fake_ctk()),
            patch("opencohost.ui.inspector_memory.show_toplevel"),
            # Slice 8 release-gate hunt: MEMORIAS_ENABLED now defaults True in
            # production, so this OFF-behavior test must patch it explicitly
            # instead of relying on the old default.
            patch("opencohost.ui.inspector_memory.MEMORIAS_ENABLED", False),
        ):
            win = inspector_memory.open_inspector_memory(**_open_kwargs(schedule_ui_update=schedule))
            assert event.wait(timeout=2)
            calls[0]()
            texts = _collect_label_texts(win)

        assert inspector_memory.MEMORIAS_INDICATOR_ON not in texts
        assert inspector_memory.MEMORIAS_INDICATOR_PAUSED not in texts
        assert not _find_button_by_text(win, "Pausar guardado")

    def test_switch_pause_feedback_copy_matches_rc4_wording(self):
        import opencohost.ui.inspector_memory as inspector_memory

        assert inspector_memory.MEMORIAS_PAUSE_FEEDBACK_TEXT == (
            "Desde ahora no se guardarán nuevas memorias. Lo anterior puede "
            "terminar de guardarse si ya estaba marcado para guardar."
        )

        motor_ia = self._make_motor_ia(paused=False)
        calls, schedule, event = _make_schedule()

        with (
            patch("opencohost.ui.inspector_memory.ctk", _make_fake_ctk()),
            patch("opencohost.ui.inspector_memory.show_toplevel"),
            patch("opencohost.ui.inspector_memory.MEMORIAS_ENABLED", True),
        ):
            win = inspector_memory.open_inspector_memory(
                **_open_kwargs(motor_ia=motor_ia, schedule_ui_update=schedule)
            )
            assert event.wait(timeout=2)
            calls[0]()

            switch_buttons = _find_button_by_text(win, "Pausar guardado")
            switch_buttons[0].invoke()

            texts = _collect_label_texts(win)

        assert inspector_memory.MEMORIAS_PAUSE_FEEDBACK_TEXT in texts

    def test_window_copy_states_pause_does_not_delete_or_block_already_capturable(self):
        import opencohost.ui.inspector_memory as inspector_memory

        assert inspector_memory.MEMORIAS_PAUSE_WINDOW_NOTE == (
            "La pausa no borra ni bloquea memorias ya capturables."
        )

        motor_ia = self._make_motor_ia(paused=False)
        calls, schedule, event = _make_schedule()

        with (
            patch("opencohost.ui.inspector_memory.ctk", _make_fake_ctk()),
            patch("opencohost.ui.inspector_memory.show_toplevel"),
            patch("opencohost.ui.inspector_memory.MEMORIAS_ENABLED", True),
        ):
            win = inspector_memory.open_inspector_memory(
                **_open_kwargs(motor_ia=motor_ia, schedule_ui_update=schedule)
            )
            assert event.wait(timeout=2)
            calls[0]()
            texts = _collect_label_texts(win)

        assert inspector_memory.MEMORIAS_PAUSE_WINDOW_NOTE in texts


def _make_pinned_rows(store, profile_id, count, private_count=0):
    """Create *count* distinct, pinned rows for profile_id via the real
    upsert+set_flags path (not _fake_row) — count_all_pinned/
    list_injection_candidates are real SQL queries, so the counter tests
    below need real rows, not dicts."""
    row_ids = []
    for i in range(count):
        row_id = store.upsert_draft(
            profile_id,
            f"{profile_id}|palabra{i}-token{i}-extra{i}",
            f"Titulo {i}",
            f"Contenido {i}",
        )
        store.set_flags(row_id, pinned=True)
        row_ids.append(row_id)
    for row_id in row_ids[:private_count]:
        store.set_flags(row_id, private=True)
    return row_ids


class TestPinnedCounter:
    def test_pinned_counter_shows_fijadas_n_se_inyectan_2_when_n_greater_than_2(self, tmp_path):
        from opencohost.core.memory.memoria_store import MemoriaStore
        import opencohost.ui.inspector_memory as inspector_memory

        store = MemoriaStore(tmp_path / "memorias.db")
        _make_pinned_rows(store, "p1", count=3)
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

        assert "Fijadas: 3 · se inyectan 2" in texts

    def test_pinned_counter_shows_fewer_than_2_when_n_less_than_2(self, tmp_path):
        from opencohost.core.memory.memoria_store import MemoriaStore
        import opencohost.ui.inspector_memory as inspector_memory

        store = MemoriaStore(tmp_path / "memorias.db")
        _make_pinned_rows(store, "p1", count=1)
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

        assert "Fijadas: 1 · se inyectan 1" in texts


class TestPinnedCounterF6bSemantics:
    """F6b (owner decision, obs #2770): N = ALL pinned rows for the profile
    — including pinned+private and pinned+inactive — never the
    injection-filtered set (that was slice-5's A-SF1 undercount). M =
    injectable-pinned only, capped at MEMORIAS_MAX_PINNED_INJECT."""

    def test_n_counts_all_pinned_including_private_rows(self, tmp_path):
        from opencohost.core.memory.memoria_store import MemoriaStore
        import opencohost.ui.inspector_memory as inspector_memory

        store = MemoriaStore(tmp_path / "memorias.db")
        _make_pinned_rows(store, "p1", count=5, private_count=2)
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

        assert "Fijadas: 5 · se inyectan 2" in texts

    def test_m_shrinks_further_as_more_pinned_rows_go_private(self, tmp_path):
        from opencohost.core.memory.memoria_store import MemoriaStore
        import opencohost.ui.inspector_memory as inspector_memory

        store = MemoriaStore(tmp_path / "memorias.db")
        _make_pinned_rows(store, "p1", count=5, private_count=4)
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

        assert "Fijadas: 5 · se inyectan 1" in texts

    def test_m_is_zero_when_all_pinned_rows_are_private(self, tmp_path):
        """B-N3: the overclaim guard — pinning N rows and marking ALL of
        them private must show «se inyectan 0», proving M never overclaims
        injection when every pinned row is excluded from the candidate set."""
        from opencohost.core.memory.memoria_store import MemoriaStore
        import opencohost.ui.inspector_memory as inspector_memory

        store = MemoriaStore(tmp_path / "memorias.db")
        _make_pinned_rows(store, "p1", count=3, private_count=3)
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

        assert "Fijadas: 3 · se inyectan 0" in texts


# ---------------------------------------------------------------------------
# Slice 8 (flip+disclosure): F1 passive disclosure banner, shown every
# launch until the operator dismisses it once, then never again (dismiss
# state persisted to a small notice file, same atomic-write pattern as
# save_tts_local_only). Passive = a label + button in the window body, never
# a blocking modal.
# ---------------------------------------------------------------------------


class TestF1DisclosureBanner:
    def test_banner_shown_until_dismissed_then_persists_dismissed_state(self, tmp_path):
        import opencohost.ui.inspector_memory as inspector_memory
        from opencohost.config.settings import load_memorias_notice_dismissed

        notice_file = str(tmp_path / "memorias_notice.json")
        calls, schedule, event = _make_schedule()

        with (
            patch("opencohost.ui.inspector_memory.ctk", _make_fake_ctk()),
            patch("opencohost.ui.inspector_memory.show_toplevel"),
        ):
            win = inspector_memory.open_inspector_memory(
                **_open_kwargs(schedule_ui_update=schedule, notice_file=notice_file)
            )
            texts = _collect_label_texts(win)
            assert inspector_memory.MEMORIAS_BANNER_TEXT in texts, "banner must show pre-dismiss"

            dismiss_buttons = _find_button_by_text(win, "Entendido")
            assert dismiss_buttons, "expected a banner dismiss button"
            dismiss_buttons[0].invoke()

        assert load_memorias_notice_dismissed(notice_file) is True, (
            "dismiss must persist to the notice file"
        )

    def test_banner_not_shown_again_after_dismiss_across_restarts(self, tmp_path):
        """A fresh open_inspector_memory call (simulating a new app launch /
        a second window open) must not show the banner once the notice file
        already records dismissed=True."""
        import opencohost.ui.inspector_memory as inspector_memory
        from opencohost.config.settings import save_memorias_notice_dismissed

        notice_file = str(tmp_path / "memorias_notice.json")
        save_memorias_notice_dismissed(True, notice_file)
        calls, schedule, event = _make_schedule()

        with (
            patch("opencohost.ui.inspector_memory.ctk", _make_fake_ctk()),
            patch("opencohost.ui.inspector_memory.show_toplevel"),
        ):
            win = inspector_memory.open_inspector_memory(
                **_open_kwargs(schedule_ui_update=schedule, notice_file=notice_file)
            )
            texts = _collect_label_texts(win)

        assert inspector_memory.MEMORIAS_BANNER_TEXT not in texts
        assert not _find_button_by_text(win, "Entendido")


# ---------------------------------------------------------------------------
# Slice 8 (flip+disclosure, R13): PRIVACY.md / TRUST_MODEL.md must disclose
# the new memorias.db persistence path now that MEMORIAS_ENABLED is True in
# production — the old blanket "RAM only, never written to disk" claim no
# longer covers Kira's saved memorias.
# ---------------------------------------------------------------------------


def _read_doc(relative_path):
    import os

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, relative_path), "r", encoding="utf-8") as f:
        return f.read()


class TestDisclosureDocsUpdated:
    def test_privacy_md_discloses_conversation_ram_only_vs_memorias_persisted(self):
        text = _read_doc("docs/PRIVACY.md")
        assert "Held in RAM only" in text  # conversation/digest claim still stands
        assert "memorias.db" in text
        assert "Kira's saved memorias" in text

    def test_privacy_md_discloses_pause_does_not_retro_delete_or_block_already_capturable(self):
        text = _read_doc("docs/PRIVACY.md")
        assert "disk-only" in text
        assert "does not retroactively delete" in text.lower()

    def test_privacy_md_discloses_hard_crash_residual_loss_of_live_window(self):
        text = _read_doc("docs/PRIVACY.md")
        assert "hard crash" in text.lower()
        assert "~10" in text

    def test_trust_model_md_updated_for_memorias_persistence_disclosure(self):
        text = _read_doc("docs/TRUST_MODEL.md")
        assert "memorias.db" in text
        assert "local-only write" in text.lower()
        assert "local, per-profile sqlite" in text.lower()
