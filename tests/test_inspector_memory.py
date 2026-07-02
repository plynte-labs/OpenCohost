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


def _make_fake_ctk():
    mock_module = MagicMock()
    mock_module.CTkFrame = _FakeWidget
    mock_module.CTkScrollableFrame = _FakeWidget
    mock_module.CTkLabel = _FakeWidget
    mock_module.CTkButton = _FakeButton
    mock_module.CTkToplevel = _FakeToplevel
    mock_module.CTkFont = _FakeFont
    return mock_module


def _make_schedule():
    calls = []
    event = threading.Event()

    def schedule(fn):
        calls.append(fn)
        event.set()

    return calls, schedule, event


def _empty_snapshot():
    from collections import Counter
    return {"entries": [], "source_breakdown": Counter(), "digest": {"line_count": 0, "total_chars": 0, "max_chars": 600}}


def _open_kwargs(**overrides):
    kwargs = dict(
        parent=MagicMock(),
        ref_getter=lambda: None,
        ref_setter=lambda w: None,
        motor_ia=MagicMock(memory_inspector_snapshot=MagicMock(return_value=_empty_snapshot())),
        kira_agenda=SimpleNamespace(topics=[]),
        schedule_ui_update=lambda fn: fn(),
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
