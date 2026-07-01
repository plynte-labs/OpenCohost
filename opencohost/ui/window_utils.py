"""Window management utilities for CTkToplevel windows on Windows.

Problem solved: CTkToplevel.__init__ on Windows does an internal
``after(5, deiconify)`` dance for dark-mode title-bar colouring and NEVER
lifts the window above the still-focused parent. Calling ``lift()`` at
creation is immediately undone by that async deiconify, so the raise must
be DEFERRED past the framework's own scheduled callbacks.

Two public helpers are provided:

show_toplevel(win, parent, *, modal=False)
    Called once when a new Toplevel is first created.  Sets transient,
    then schedules a deferred raise that lands well after CTkToplevel's
    internal after(5, ...).

raise_window(win)
    Called on the "bring to front" / re-click path where the window is
    already open. Identical deferred raise, no re-transient needed.

Both helpers guard against the window being destroyed between scheduling
and callback execution (winfo_exists guard + TclError catch).
"""
from __future__ import annotations

from typing import Any

# Delay in milliseconds for the deferred raise.
# Must be > 5 ms (CTkToplevel's internal after(5, deiconify)) so our raise
# lands in the event queue AFTER the framework's own deiconify completes.
# 60 ms is comfortable headroom without being perceptibly laggy.
_RAISE_DELAY_MS: int = 60

# Must exceed CustomTkinter's own after(200, ...) icon reset: CTkToplevel
# unconditionally schedules after(200, iconbitmap(<feather .ico>)) on Windows and
# the root CTk window has the same (conditional) reset — both stomp any icon set
# earlier, including one inherited via the root's iconphoto(True, ...) default.
# Scheduling ours past that window wins the race for the root AND every Toplevel.
_ICON_DELAY_MS: int = 250
_icon_image = None  # module-level cache + the required GC-keepalive strong ref


def _load_icon_image():
    """Load + cache the app icon as a Tk PhotoImage (resized from the 1000x1000
    source PNG). Module-scoped cache so it decodes once AND keeps a permanent strong
    reference — ImageTk.PhotoImage is GC'd the instant nothing references it, which
    silently blanks the window icon."""
    global _icon_image
    if _icon_image is not None:
        return _icon_image
    try:
        import os
        from opencohost.config.settings import BASE_DIR
        from PIL import Image, ImageTk
        img = Image.open(os.path.join(BASE_DIR, "assets", "opencohost_ico.png"))
        img.thumbnail((256, 256), Image.Resampling.LANCZOS)
        _icon_image = ImageTk.PhotoImage(img)
    except Exception:
        return None
    return _icon_image


def apply_app_icon(win: Any) -> None:
    """Set the OpenCohost logo as *win*'s window icon (defeating CustomTkinter's
    default feather icon). Call once right after constructing the root CTk window or
    ANY CTkToplevel. Deferred via after() so it lands after CustomTkinter's own
    after(200, ...) icon reset (see _ICON_DELAY_MS). Silently no-ops on failure."""
    image = _load_icon_image()
    if image is None:
        return

    def _set() -> None:
        try:
            if win.winfo_exists():
                win.iconphoto(True, image)
        except Exception:
            pass

    try:
        win.after(_ICON_DELAY_MS, _set)
    except Exception:
        pass


def _make_raise_callback(win: Any, *, grab: bool = False):
    """Return the deferred raise closure for *win*.

    The closure is safe to schedule via ``win.after(delay, callback)``; it
    guards against the window being destroyed before it fires.
    """

    def _raise() -> None:
        # Guard: window may have been closed between scheduling and execution.
        try:
            if not win.winfo_exists():
                return
        except Exception:
            return

        try:
            win.deiconify()
            win.lift()
            win.attributes("-topmost", True)
            # Reset topmost after next idle cycle — we want the raise, not a
            # permanently always-on-top window.
            win.after_idle(lambda: win.attributes("-topmost", False))
            win.focus_force()
            if grab:
                win.grab_set()
        except Exception:
            # TclError or similar if window is destroyed mid-sequence.
            pass

    return _raise


def show_toplevel(win: Any, parent: Any, *, modal: bool = False) -> None:
    """Prepare and raise a freshly created CTkToplevel window.

    Call this ONCE after the window's widgets have been built, before
    returning the window to the caller.

    Steps:
      1. ``win.transient(parent)`` — attaches the popover to the parent so it
         minimises with the parent and stays logically above it.
      2. ``win.update_idletasks()`` — flushes pending geometry so the window
         has a real size before the raise fires.
      3. Schedules a deferred raise via ``win.after(_RAISE_DELAY_MS, _raise)``
         so the raise lands after CTkToplevel's own ``after(5, deiconify)``.

    Args:
        win: The CTkToplevel (or Toplevel) instance to show.
        parent: The parent window to attach to via ``transient``.
        modal: If True, calls ``win.grab_set()`` inside the deferred raise to
               make the window modal. The gear popover uses modal=False.
    """
    win.transient(parent)
    win.update_idletasks()
    win.after(_RAISE_DELAY_MS, _make_raise_callback(win, grab=modal))


def raise_window(win: Any) -> None:
    """Bring an already-open CTkToplevel back to the front.

    Use this on the re-click path (duplicate-open guard) instead of a bare
    ``win.focus()`` call. The same deferred raise is used here because the
    parent window may have re-grabbed focus between the user's click and the
    handler running.

    Does NOT call ``transient`` — that was set at window creation.

    Args:
        win: The existing CTkToplevel instance to raise.
    """
    win.after(_RAISE_DELAY_MS, _make_raise_callback(win, grab=False))
