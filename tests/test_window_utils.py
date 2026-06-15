"""Tests for opencohost/ui/window_utils.py — headless-safe, mock-based.

Covers:
- show_toplevel: calls transient(parent), schedules deferred raise via after(),
  invoked callback calls lift, focus_force, topmost True-then-False pulse,
  grab_set only when modal=True, destroyed-window guard does not raise.
- raise_window: deferred deiconify+lift+topmost pulse+focus_force, guard on
  destroyed window does not raise.
- gear_popover integration: open_gear_popover calls show_toplevel on fresh
  window; re-click path calls raise_window.
"""
from __future__ import annotations

from typing import Any, Callable
from unittest.mock import MagicMock, call, patch

import pytest


# ---------------------------------------------------------------------------
# Fake window helpers
# ---------------------------------------------------------------------------


class _FakeWin:
    """Lightweight fake CTkToplevel for testing window_utils without a display."""

    def __init__(self, *, exists: bool = True):
        self._exists = exists
        self.calls: list[tuple[str, Any]] = []

    # Tk introspection
    def winfo_exists(self) -> bool:
        return self._exists

    # Window management stubs — record every call
    def transient(self, parent: Any) -> None:
        self.calls.append(("transient", parent))

    def update_idletasks(self) -> None:
        self.calls.append(("update_idletasks",))

    def deiconify(self) -> None:
        self.calls.append(("deiconify",))

    def lift(self) -> None:
        self.calls.append(("lift",))

    def attributes(self, attr: str, value: Any = None) -> None:
        self.calls.append(("attributes", attr, value))

    def after_idle(self, func: Callable[[], None]) -> None:
        self.calls.append(("after_idle", func))
        # Execute immediately so we can verify the lambda body
        func()

    def after(self, delay: int, func: Callable[[], None]) -> None:
        self.calls.append(("after", delay, func))
        # Store the scheduled callback but DON'T execute it automatically —
        # tests that want to trigger the raise call it manually.

    def focus_force(self) -> None:
        self.calls.append(("focus_force",))

    def grab_set(self) -> None:
        self.calls.append(("grab_set",))

    # Helper: find the deferred raise callback stored via after()
    def _deferred_raise_callback(self) -> Callable[[], None]:
        for item in self.calls:
            if item[0] == "after":
                return item[2]
        raise AssertionError("No after() call found — show_toplevel did not schedule a deferred raise")

    def _call_names(self) -> list[str]:
        return [c[0] for c in self.calls]


class _DestroyedWin(_FakeWin):
    """Simulates a window destroyed between scheduling and callback execution."""

    def __init__(self):
        super().__init__(exists=False)

    def deiconify(self):
        raise Exception("TclError: invalid command name")

    def lift(self):
        raise Exception("TclError: invalid command name")

    def focus_force(self):
        raise Exception("TclError: invalid command name")


# ---------------------------------------------------------------------------
# show_toplevel — core contract
# ---------------------------------------------------------------------------


class TestShowToplevel:
    def _import(self):
        from opencohost.ui.window_utils import show_toplevel
        return show_toplevel

    def test_calls_transient_with_parent(self):
        """show_toplevel must call win.transient(parent) to attach to the parent window."""
        show_toplevel = self._import()
        win = _FakeWin()
        parent = object()

        show_toplevel(win, parent)

        assert ("transient", parent) in win.calls, (
            "show_toplevel must call win.transient(parent)"
        )

    def test_schedules_deferred_raise_via_after(self):
        """show_toplevel must schedule a deferred raise using win.after()."""
        show_toplevel = self._import()
        win = _FakeWin()

        show_toplevel(win, parent=object())

        after_calls = [c for c in win.calls if c[0] == "after"]
        assert len(after_calls) >= 1, "show_toplevel must call win.after() to schedule a deferred raise"
        delay = after_calls[0][1]
        assert isinstance(delay, int) and delay > 0, (
            f"Deferred raise delay must be a positive int; got {delay!r}"
        )

    def test_deferred_callback_calls_lift(self):
        """The scheduled callback must call win.lift()."""
        show_toplevel = self._import()
        win = _FakeWin()

        show_toplevel(win, parent=object())
        cb = win._deferred_raise_callback()
        cb()

        assert "lift" in win._call_names(), "Deferred raise callback must call win.lift()"

    def test_deferred_callback_calls_focus_force(self):
        """The scheduled callback must call win.focus_force()."""
        show_toplevel = self._import()
        win = _FakeWin()

        show_toplevel(win, parent=object())
        cb = win._deferred_raise_callback()
        cb()

        assert "focus_force" in win._call_names(), (
            "Deferred raise callback must call win.focus_force()"
        )

    def test_deferred_callback_sets_topmost_true_then_false(self):
        """The callback must do a topmost pulse: set True then after_idle back to False."""
        show_toplevel = self._import()
        win = _FakeWin()

        show_toplevel(win, parent=object())
        cb = win._deferred_raise_callback()
        cb()

        attr_calls = [(c[1], c[2]) for c in win.calls if c[0] == "attributes"]
        assert ("-topmost", True) in attr_calls, (
            "Deferred raise must set -topmost True"
        )
        assert ("-topmost", False) in attr_calls, (
            "Deferred raise must set -topmost back to False via after_idle"
        )
        # True must appear before False in call order
        names = win._call_names()
        first_attr = next(
            i for i, c in enumerate(win.calls) if c[0] == "attributes" and c[2] is True
        )
        last_attr = next(
            i for i, c in enumerate(reversed(win.calls))
            if win.calls[len(win.calls) - 1 - i][0] == "attributes" and win.calls[len(win.calls) - 1 - i][2] is False
        )
        last_attr_real = len(win.calls) - 1 - last_attr
        assert first_attr < last_attr_real, "topmost=True must come before topmost=False"

    def test_no_grab_set_when_non_modal(self):
        """show_toplevel with modal=False must NOT call grab_set."""
        show_toplevel = self._import()
        win = _FakeWin()

        show_toplevel(win, parent=object(), modal=False)
        cb = win._deferred_raise_callback()
        cb()

        assert "grab_set" not in win._call_names(), (
            "Non-modal show_toplevel must not call grab_set"
        )

    def test_grab_set_called_when_modal(self):
        """show_toplevel with modal=True must call win.grab_set()."""
        show_toplevel = self._import()
        win = _FakeWin()

        show_toplevel(win, parent=object(), modal=True)
        cb = win._deferred_raise_callback()
        cb()

        assert "grab_set" in win._call_names(), (
            "Modal show_toplevel must call win.grab_set()"
        )

    def test_destroyed_window_guard_does_not_raise(self):
        """If win is destroyed before the deferred callback fires, no exception should propagate."""
        show_toplevel = self._import()
        win = _DestroyedWin()

        show_toplevel(win, parent=object())
        cb = win._deferred_raise_callback()

        # Must not raise — guard swallows TclError / winfo_exists=False
        try:
            cb()
        except Exception as exc:
            pytest.fail(
                f"show_toplevel deferred callback raised on destroyed window: {exc!r}"
            )

    def test_deiconify_called_in_deferred_callback(self):
        """The deferred callback must call deiconify to ensure window is visible."""
        show_toplevel = self._import()
        win = _FakeWin()

        show_toplevel(win, parent=object())
        cb = win._deferred_raise_callback()
        cb()

        assert "deiconify" in win._call_names(), (
            "Deferred raise callback must call win.deiconify()"
        )


# ---------------------------------------------------------------------------
# raise_window — re-click "bring to front" path
# ---------------------------------------------------------------------------


class TestRaiseWindow:
    def _import(self):
        from opencohost.ui.window_utils import raise_window
        return raise_window

    def test_schedules_deferred_raise(self):
        """raise_window must schedule a deferred raise via win.after()."""
        raise_window = self._import()
        win = _FakeWin()

        raise_window(win)

        after_calls = [c for c in win.calls if c[0] == "after"]
        assert len(after_calls) >= 1, "raise_window must call win.after() to schedule the raise"

    def test_deferred_callback_calls_lift_and_focus_force(self):
        """raise_window deferred callback must call lift and focus_force."""
        raise_window = self._import()
        win = _FakeWin()

        raise_window(win)
        cb = win._deferred_raise_callback()
        cb()

        names = win._call_names()
        assert "lift" in names, "raise_window deferred callback must call lift"
        assert "focus_force" in names, "raise_window deferred callback must call focus_force"

    def test_deferred_callback_does_topmost_pulse(self):
        """raise_window deferred callback must do a -topmost True-then-False pulse."""
        raise_window = self._import()
        win = _FakeWin()

        raise_window(win)
        cb = win._deferred_raise_callback()
        cb()

        attr_calls = [(c[1], c[2]) for c in win.calls if c[0] == "attributes"]
        assert ("-topmost", True) in attr_calls
        assert ("-topmost", False) in attr_calls

    def test_does_not_call_transient(self):
        """raise_window must NOT call transient — that was already set at creation."""
        raise_window = self._import()
        win = _FakeWin()

        raise_window(win)
        cb = win._deferred_raise_callback()
        cb()

        assert "transient" not in win._call_names(), (
            "raise_window must not re-call transient"
        )

    def test_destroyed_window_guard_does_not_raise(self):
        """If win is destroyed before callback fires, no exception must propagate."""
        raise_window = self._import()
        win = _DestroyedWin()

        raise_window(win)
        cb = win._deferred_raise_callback()

        try:
            cb()
        except Exception as exc:
            pytest.fail(
                f"raise_window deferred callback raised on destroyed window: {exc!r}"
            )


# ---------------------------------------------------------------------------
# Triangulation — show_toplevel delay value
# ---------------------------------------------------------------------------


class TestShowToplevelDelay:
    def test_delay_is_at_least_10ms(self):
        """Defer delay must be >5ms to land after CTkToplevel's internal after(5, deiconify)."""
        from opencohost.ui.window_utils import show_toplevel
        win = _FakeWin()

        show_toplevel(win, parent=object())

        after_calls = [c for c in win.calls if c[0] == "after"]
        delay = after_calls[0][1]
        assert delay >= 10, (
            f"Deferred raise delay must be >=10ms to beat CTkToplevel's after(5); got {delay}"
        )

    def test_delay_is_at_most_500ms(self):
        """Defer delay must not be so large that the window feels laggy (<=500ms)."""
        from opencohost.ui.window_utils import show_toplevel
        win = _FakeWin()

        show_toplevel(win, parent=object())

        after_calls = [c for c in win.calls if c[0] == "after"]
        delay = after_calls[0][1]
        assert delay <= 500, (
            f"Deferred raise delay must be <=500ms to feel responsive; got {delay}"
        )


# ---------------------------------------------------------------------------
# gear_popover integration — show_toplevel and raise_window wiring
# ---------------------------------------------------------------------------


class TestGearPopoverWindowUtils:
    """Verify gear_popover calls show_toplevel on fresh open and raise_window on re-click.

    Patch targets use the gear_popover module namespace (``opencohost.ui.gear_popover.show_toplevel``
    and ``opencohost.ui.gear_popover.raise_window``) because gear_popover imports those names
    directly via ``from opencohost.ui.window_utils import ...``. Patching the window_utils module
    directly would not intercept calls already bound in gear_popover's namespace.
    """

    def _build_kwargs(self):
        return dict(
            parent=MagicMock(),
            popover_ref_getter=lambda: None,
            popover_ref_setter=lambda p: None,
            compacto_active=False,
            logs_visible=False,
            on_compacto_toggle=lambda: None,
            on_logs_toggle=lambda: None,
            on_compacto_state_write=lambda v: None,
            on_logs_state_write=lambda v: None,
        )

    def test_fresh_open_calls_show_toplevel(self):
        """open_gear_popover must call show_toplevel after building widgets."""
        mock_popover = MagicMock()
        mock_ctk = MagicMock()
        mock_ctk.CTkToplevel.return_value = mock_popover

        import opencohost.ui.gear_popover as gear_popover

        with (
            patch("opencohost.ui.gear_popover.ctk", mock_ctk),
            patch("opencohost.ui.gear_popover.show_toplevel") as mock_show,
        ):
            gear_popover.open_gear_popover(**self._build_kwargs())

        mock_show.assert_called_once()
        call_args = mock_show.call_args
        assert call_args is not None, "show_toplevel must have been called with arguments"
        # modal must be False for the gear (non-modal by design)
        kwargs = call_args.kwargs if hasattr(call_args, "kwargs") else call_args[1]
        assert kwargs.get("modal", False) is False, (
            "Gear popover must be opened with modal=False"
        )

    def test_fresh_open_passes_parent_to_show_toplevel(self):
        """show_toplevel must receive the same parent object that was passed to open_gear_popover."""
        mock_popover = MagicMock()
        mock_ctk = MagicMock()
        mock_ctk.CTkToplevel.return_value = mock_popover
        the_parent = MagicMock()

        import opencohost.ui.gear_popover as gear_popover

        kwargs = self._build_kwargs()
        kwargs["parent"] = the_parent

        with (
            patch("opencohost.ui.gear_popover.ctk", mock_ctk),
            patch("opencohost.ui.gear_popover.show_toplevel") as mock_show,
        ):
            gear_popover.open_gear_popover(**kwargs)

        positional_args = mock_show.call_args.args
        assert the_parent in positional_args, (
            "show_toplevel must receive the original parent window"
        )

    def test_reclick_calls_raise_window(self):
        """Re-clicking gear (existing window) must call raise_window, not bare focus()."""
        existing = MagicMock()
        existing.winfo_exists.return_value = True
        ref_holder = [existing]

        kwargs = dict(
            parent=MagicMock(),
            popover_ref_getter=lambda: ref_holder[0],
            popover_ref_setter=lambda p: None,
            compacto_active=False,
            logs_visible=False,
            on_compacto_toggle=lambda: None,
            on_logs_toggle=lambda: None,
            on_compacto_state_write=lambda v: None,
            on_logs_state_write=lambda v: None,
        )

        import opencohost.ui.gear_popover as gear_popover

        with patch("opencohost.ui.gear_popover.raise_window") as mock_raise:
            result = gear_popover.open_gear_popover(**kwargs)

        mock_raise.assert_called_once_with(existing)
        assert result is None, "Re-click must return None (existing window was raised)"
