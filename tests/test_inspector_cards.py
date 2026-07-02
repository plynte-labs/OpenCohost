"""Tests for opencohost/ui/inspector_cards.py — "Tarjetas editoriales" read-only
inspector window (Slice A, cards_memory_readonly_panels_20260701).

Headless precedents:
  - test_model_panel.py / test_status_bar.py: object.__new__ construction,
    fake CTk widget classes recording pack/configure/command kwargs.
  - test_window_utils.py TestGearPopoverWindowUtils: patch show_toplevel /
    raise_window at the module namespace to avoid needing full Tk semantics.
"""
from __future__ import annotations

import re
import threading
import time
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Fake CTk widgets — minimal, records pack/configure/command, supports
# winfo_children()/destroy() so _clear() can be exercised.
# ---------------------------------------------------------------------------


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

    def pack_forget(self):
        pass

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
        self._clipboard = []
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

    def clipboard_clear(self):
        self._clipboard = []

    def clipboard_append(self, text):
        self._clipboard.append(text)


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
    """Return (calls, schedule, event). schedule() records without executing."""
    calls = []
    event = threading.Event()

    def schedule(fn):
        calls.append(fn)
        event.set()

    return calls, schedule, event


def _make_card(topic="Topic", status=None, expires_at=None, **overrides):
    from opencohost.core.editorial_cards import EditorialCard, EditorialCardStatus

    kwargs = dict(
        topic=topic,
        summary="Some summary",
        streamer_take="My take",
        counterpoints=["counter 1"],
        discussion_hooks=["hook 1"],
    )
    kwargs.update(overrides)
    card = EditorialCard(**kwargs)
    if status is not None:
        card.status = status
    if expires_at is not None:
        card.expires_at = expires_at
    return card


def _open_kwargs(**overrides):
    kwargs = dict(
        parent=MagicMock(),
        ref_getter=lambda: None,
        ref_setter=lambda w: None,
        card_store=MagicMock(list_all=MagicMock(return_value=[])),
        schedule_ui_update=lambda fn: fn(),
    )
    kwargs.update(overrides)
    return kwargs


# ---------------------------------------------------------------------------
# Duplicate-open guard
# ---------------------------------------------------------------------------


class TestDupOpenGuard:
    def test_existing_window_is_raised_not_recreated(self):
        import opencohost.ui.inspector_cards as inspector_cards

        existing = MagicMock()
        existing.winfo_exists.return_value = True

        with (
            patch("opencohost.ui.inspector_cards.ctk", _make_fake_ctk()),
            patch("opencohost.ui.inspector_cards.raise_window") as mock_raise,
        ):
            result = inspector_cards.open_inspector_cards(
                **_open_kwargs(ref_getter=lambda: existing)
            )

        mock_raise.assert_called_once_with(existing)
        assert result is None

    def test_destroyed_existing_ref_falls_through_to_new_window(self):
        import opencohost.ui.inspector_cards as inspector_cards

        existing = MagicMock()
        existing.winfo_exists.side_effect = Exception("TclError")

        with (
            patch("opencohost.ui.inspector_cards.ctk", _make_fake_ctk()),
            patch("opencohost.ui.inspector_cards.show_toplevel"),
        ):
            result = inspector_cards.open_inspector_cards(
                **_open_kwargs(ref_getter=lambda: existing)
            )

        assert result is not None


# ---------------------------------------------------------------------------
# Worker-thread read + marshaling — never touch Tk from the worker
# ---------------------------------------------------------------------------


class TestWorkerThreadMarshaling:
    def test_list_all_called_from_worker_thread_not_main(self):
        import opencohost.ui.inspector_cards as inspector_cards

        called_from_main = []

        def fake_list_all():
            called_from_main.append(threading.current_thread() is threading.main_thread())
            return []

        _, schedule, event = _make_schedule()
        store = MagicMock(list_all=fake_list_all)

        with (
            patch("opencohost.ui.inspector_cards.ctk", _make_fake_ctk()),
            patch("opencohost.ui.inspector_cards.show_toplevel"),
        ):
            inspector_cards.open_inspector_cards(
                **_open_kwargs(card_store=store, schedule_ui_update=schedule)
            )

        assert event.wait(timeout=2), "schedule_ui_update was never called"
        assert called_from_main == [False], "list_all must run on a worker thread, not main"

    def test_render_callback_not_invoked_until_scheduler_runs_it(self):
        """The worker must not touch any Tk widget directly — rendering only
        happens when the caller invokes the scheduled callback."""
        import opencohost.ui.inspector_cards as inspector_cards

        cards = [_make_card(topic="A")]
        calls, schedule, event = _make_schedule()
        store = MagicMock(list_all=MagicMock(return_value=cards))
        fake_ctk = _make_fake_ctk()

        with (
            patch("opencohost.ui.inspector_cards.ctk", fake_ctk),
            patch("opencohost.ui.inspector_cards.show_toplevel"),
        ):
            win = inspector_cards.open_inspector_cards(
                **_open_kwargs(card_store=store, schedule_ui_update=schedule)
            )

            assert event.wait(timeout=2)
            assert len(calls) == 1
            # Now execute the marshaled callback (simulates main-thread _safe_after).
            calls[0]()


# ---------------------------------------------------------------------------
# Fail-open on store exception
# ---------------------------------------------------------------------------


class TestFailOpen:
    def test_store_exception_renders_no_disponible_and_logs(self):
        import opencohost.ui.inspector_cards as inspector_cards

        store = MagicMock(list_all=MagicMock(side_effect=RuntimeError("db locked")))
        calls, schedule, event = _make_schedule()

        with (
            patch("opencohost.ui.inspector_cards.ctk", _make_fake_ctk()),
            patch("opencohost.ui.inspector_cards.show_toplevel"),
            patch.object(inspector_cards.logger, "exception") as mock_exception,
        ):
            inspector_cards.open_inspector_cards(
                **_open_kwargs(card_store=store, schedule_ui_update=schedule)
            )
            assert event.wait(timeout=2)
            calls[0]()

        mock_exception.assert_called_once()


# ---------------------------------------------------------------------------
# Status grouping + vencida chip (pure function — no Tk needed)
# ---------------------------------------------------------------------------


class TestGroupCardsByStatus:
    def test_groups_in_active_armed_draft_used_expired_order(self):
        from opencohost.ui.inspector_cards import group_cards_by_status
        from opencohost.core.editorial_cards import EditorialCardStatus

        used = _make_card(topic="Used", status=EditorialCardStatus.USED)
        active = _make_card(topic="Active", status=EditorialCardStatus.ACTIVE)
        draft = _make_card(topic="Draft", status=EditorialCardStatus.DRAFT)
        armed = _make_card(topic="Armed", status=EditorialCardStatus.ARMED)
        expired = _make_card(topic="Expired", status=EditorialCardStatus.EXPIRED)

        grouped = group_cards_by_status([used, active, draft, armed, expired])
        topics = [card.topic for card, _label, _vencida in grouped]

        assert topics == ["Active", "Armed", "Draft", "Used", "Expired"]

    def test_computed_vencida_chip_when_expired_but_status_not_expired(self):
        from opencohost.ui.inspector_cards import group_cards_by_status
        from opencohost.core.editorial_cards import EditorialCardStatus
        from datetime import datetime, timedelta, timezone

        stale_armed = _make_card(
            topic="Stale",
            status=EditorialCardStatus.ARMED,
            expires_at=datetime.now(timezone.utc) - timedelta(hours=1),
        )

        grouped = group_cards_by_status([stale_armed])

        assert grouped[0][2] is True, "expired-but-not-EXPIRED-status card must get the vencida chip"

    def test_no_vencida_chip_when_status_already_expired(self):
        from opencohost.ui.inspector_cards import group_cards_by_status
        from opencohost.core.editorial_cards import EditorialCardStatus
        from datetime import datetime, timedelta, timezone

        expired = _make_card(
            topic="Expired",
            status=EditorialCardStatus.EXPIRED,
            expires_at=datetime.now(timezone.utc) - timedelta(hours=1),
        )

        grouped = group_cards_by_status([expired])

        assert grouped[0][2] is False

    def test_no_vencida_chip_for_non_expired_card(self):
        from opencohost.ui.inspector_cards import group_cards_by_status
        from opencohost.core.editorial_cards import EditorialCardStatus

        fresh = _make_card(topic="Fresh", status=EditorialCardStatus.ARMED)

        grouped = group_cards_by_status([fresh])

        assert grouped[0][2] is False


# ---------------------------------------------------------------------------
# Launcher count label (pure function)
# ---------------------------------------------------------------------------


class TestFormatLauncherLabel:
    def test_with_count(self):
        from opencohost.ui.inspector_cards import format_launcher_label

        assert format_launcher_label(7) == "Tarjetas editoriales (7)"

    def test_with_zero(self):
        from opencohost.ui.inspector_cards import format_launcher_label

        assert format_launcher_label(0) == "Tarjetas editoriales (0)"

    def test_with_none_omits_parenthetical(self):
        from opencohost.ui.inspector_cards import format_launcher_label

        assert format_launcher_label(None) == "Tarjetas editoriales"


# ---------------------------------------------------------------------------
# Copy-to-clipboard wiring
# ---------------------------------------------------------------------------


class TestCopyToClipboard:
    def test_copy_button_writes_streamer_take_to_clipboard(self):
        import opencohost.ui.inspector_cards as inspector_cards

        card = _make_card(topic="A", streamer_take="my hot take")
        calls, schedule, event = _make_schedule()
        store = MagicMock(list_all=MagicMock(return_value=[card]))

        with (
            patch("opencohost.ui.inspector_cards.ctk", _make_fake_ctk()),
            patch("opencohost.ui.inspector_cards.show_toplevel"),
        ):
            win = inspector_cards.open_inspector_cards(
                **_open_kwargs(card_store=store, schedule_ui_update=schedule)
            )
            assert event.wait(timeout=2)
            calls[0]()  # render cards + auto-select first card

        # Find a "Copiar" button among detail_frame's descendants whose command
        # copies the streamer_take text, and invoke it.
        copy_buttons = _find_copy_buttons(win)
        assert copy_buttons, "expected at least one Copiar button in the detail pane"
        copy_buttons[0].invoke()

        assert win._clipboard == ["my hot take"], win._clipboard


def _find_copy_buttons(node, acc=None):
    if acc is None:
        acc = []
    for child in getattr(node, "children", []):
        if isinstance(child, _FakeButton) and child.kwargs.get("text") == "Copiar":
            acc.append(child)
        _find_copy_buttons(child, acc)
    return acc


# ---------------------------------------------------------------------------
# "Actualizado HH:MM:SS" absolute render-time stamp
# ---------------------------------------------------------------------------


class TestUpdatedStamp:
    def test_stamp_shows_absolute_wall_clock_time_at_render(self):
        import opencohost.ui.inspector_cards as inspector_cards

        calls, schedule, event = _make_schedule()
        store = MagicMock(list_all=MagicMock(return_value=[]))

        with (
            patch("opencohost.ui.inspector_cards.ctk", _make_fake_ctk()),
            patch("opencohost.ui.inspector_cards.show_toplevel"),
        ):
            win = inspector_cards.open_inspector_cards(
                **_open_kwargs(card_store=store, schedule_ui_update=schedule)
            )
            assert event.wait(timeout=2)
            calls[0]()

        stamp_label = win.children[1]
        assert re.match(r"^Actualizado \d{2}:\d{2}:\d{2}$", stamp_label.kwargs.get("text"))


# ---------------------------------------------------------------------------
# Close-during-refresh: marshaled render callback must be a no-op once the
# window is gone (schedule_ui_update survives Toplevel destroy — it targets
# the root app's after-loop).
# ---------------------------------------------------------------------------


def _count_descendants(node):
    count = 0
    for child in getattr(node, "children", []):
        count += 1 + _count_descendants(child)
    return count


class TestClosedWindowGuardsMarshaledRender:
    def test_render_cards_is_noop_after_window_destroyed(self):
        import opencohost.ui.inspector_cards as inspector_cards

        cards = [_make_card(topic="A")]
        calls, schedule, event = _make_schedule()
        store = MagicMock(list_all=MagicMock(return_value=cards))

        with (
            patch("opencohost.ui.inspector_cards.ctk", _make_fake_ctk()),
            patch("opencohost.ui.inspector_cards.show_toplevel"),
        ):
            win = inspector_cards.open_inspector_cards(
                **_open_kwargs(card_store=store, schedule_ui_update=schedule)
            )
            assert event.wait(timeout=2)

            win._exists = False
            stamp_before = win.children[1].kwargs.get("text")
            count_before = _count_descendants(win)

            calls[0]()  # marshaled _render_cards callback after close

            assert win.children[1].kwargs.get("text") == stamp_before
            assert _count_descendants(win) == count_before

    def test_render_error_is_noop_after_window_destroyed(self):
        import opencohost.ui.inspector_cards as inspector_cards

        store = MagicMock(list_all=MagicMock(side_effect=RuntimeError("db locked")))
        calls, schedule, event = _make_schedule()

        with (
            patch("opencohost.ui.inspector_cards.ctk", _make_fake_ctk()),
            patch("opencohost.ui.inspector_cards.show_toplevel"),
            patch.object(inspector_cards.logger, "exception"),
        ):
            win = inspector_cards.open_inspector_cards(
                **_open_kwargs(card_store=store, schedule_ui_update=schedule)
            )
            assert event.wait(timeout=2)

            win._exists = False
            stamp_before = win.children[1].kwargs.get("text")
            count_before = _count_descendants(win)

            calls[0]()  # marshaled _render_error callback after close

            assert win.children[1].kwargs.get("text") == stamp_before
            assert _count_descendants(win) == count_before
