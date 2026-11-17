"""Foreground self-capture guard (vision-on-demand V1.11).

V1.7 second run: with the OpenCohost window in front, Kira described her own
UI. Before each burst frame the turn step asks a ``ForegroundProbe`` whether
the foreground window is the app itself and skips that frame if so.

Self rule (``is_self_window``): the foreground window is OpenCohost when
- its title, trimmed and case-folded, is exactly ``opencohost`` (the Tauri
  main window title in ``src-tauri/tauri.conf.json``); an exact match, so a
  browser tab or a document that merely mentions OpenCohost does not count; or
- its process image basename is ``opencohost.exe`` (bundled build, named
  after ``productName``) or ``opencohost_tauri_shell.exe`` (the cargo binary
  that ``pnpm tauri:debug`` runs).

V1.13 main vs overlay (V1.7 third run): the desktop companion
(``kira-desktop``, a 320x440 always-on-top window) runs in the same process,
so a focused companion counted as "OpenCohost in front" although the screen
behind it was visible. A self window is now ``main`` (skip the frame) only
when it covers at least ``MAIN_WINDOW_COVERAGE`` of its monitor's work area
(``covers_monitor``); a smaller one is an ``overlay`` and the frame is
captured normally. Unknown geometry keeps the conservative ``main`` verdict.

Fail-open: off Windows, or on any probe error, a frame is never skipped.
The probe reads a window title, a process path and two rectangles; only the
coarse kind (``main``/``overlay``/``none``) is ever logged.
"""
from __future__ import annotations

import ntpath
import sys
from typing import Any, Optional, Protocol, runtime_checkable

SELF_WINDOW_TITLES = frozenset({"opencohost"})
SELF_PROCESS_NAMES = frozenset({"opencohost.exe", "opencohost_tauri_shell.exe"})

SELF_KIND_MAIN = "main"
SELF_KIND_OVERLAY = "overlay"
SELF_KIND_NONE = "none"
MAIN_WINDOW_COVERAGE = 0.5

Rect = tuple[int, int, int, int]  # (left, top, right, bottom)


def is_self_window(title: Optional[str], process_path: Optional[str]) -> bool:
    if title and title.strip().casefold() in SELF_WINDOW_TITLES:
        return True
    if process_path:
        # ntpath splits both separators, so a POSIX-style path works too.
        name = ntpath.basename(process_path.strip()).casefold()
        if name in SELF_PROCESS_NAMES:
            return True
    return False


def covers_monitor(
    window: Optional[Rect], work_area: Optional[Rect], threshold: float = MAIN_WINDOW_COVERAGE
) -> Optional[bool]:
    """True when the part of ``window`` on ``work_area`` covers at least
    ``threshold`` of it; None when either rectangle is unknown or empty."""
    if window is None or work_area is None:
        return None
    wl, wt, wr, wb = work_area
    area = max(0, wr - wl) * max(0, wb - wt)
    if area <= 0:
        return None
    left, top, right, bottom = window
    overlap = max(0, min(right, wr) - max(left, wl)) * max(0, min(bottom, wb) - max(top, wt))
    return overlap >= threshold * area


@runtime_checkable
class ForegroundProbe(Protocol):
    def is_self_foreground(self) -> bool: ...


class NeverSelfProbe:
    """Non-Windows default: never skips a frame."""

    def is_self_foreground(self) -> bool:
        return False

    def foreground_kind(self) -> str:
        return SELF_KIND_NONE


class _Win32Api:
    """Thin ctypes wrapper over user32/kernel32; built lazily, Windows only."""

    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    _MONITOR_DEFAULTTONEAREST = 2

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        self._ctypes = ctypes
        self._wintypes = wintypes
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._user32.GetForegroundWindow.restype = wintypes.HWND
        self._user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        self._user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        self._user32.GetWindowThreadProcessId.argtypes = [
            wintypes.HWND, ctypes.POINTER(wintypes.DWORD)
        ]
        self._kernel32.OpenProcess.restype = wintypes.HANDLE
        self._kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self._kernel32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)
        ]
        self._kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self._user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        self._user32.MonitorFromWindow.restype = wintypes.HMONITOR
        self._user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]

        class _MonitorInfo(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.DWORD),
                ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT),
                ("dwFlags", wintypes.DWORD),
            ]

        self._MonitorInfo = _MonitorInfo
        self._user32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.POINTER(_MonitorInfo)]

    def foreground_window(self) -> Any:
        return self._user32.GetForegroundWindow()

    def window_title(self, hwnd: Any) -> str:
        length = self._user32.GetWindowTextLengthW(hwnd)
        buf = self._ctypes.create_unicode_buffer(max(1, length + 1))
        self._user32.GetWindowTextW(hwnd, buf, len(buf))
        return buf.value

    def process_path(self, hwnd: Any) -> Optional[str]:
        pid = self._wintypes.DWORD(0)
        self._user32.GetWindowThreadProcessId(hwnd, self._ctypes.byref(pid))
        if not pid.value:
            return None
        handle = self._kernel32.OpenProcess(self._PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
        if not handle:
            return None
        try:
            size = self._wintypes.DWORD(1024)
            buf = self._ctypes.create_unicode_buffer(size.value)
            if not self._kernel32.QueryFullProcessImageNameW(handle, 0, buf, self._ctypes.byref(size)):
                return None
            return buf.value
        finally:
            self._kernel32.CloseHandle(handle)


    def window_rect(self, hwnd: Any) -> Optional[Rect]:
        rect = self._wintypes.RECT()
        if not self._user32.GetWindowRect(hwnd, self._ctypes.byref(rect)):
            return None
        return (rect.left, rect.top, rect.right, rect.bottom)

    def monitor_work_area(self, hwnd: Any) -> Optional[Rect]:
        monitor = self._user32.MonitorFromWindow(hwnd, self._MONITOR_DEFAULTTONEAREST)
        if not monitor:
            return None
        info = self._MonitorInfo()
        info.cbSize = self._ctypes.sizeof(self._MonitorInfo)
        if not self._user32.GetMonitorInfoW(monitor, self._ctypes.byref(info)):
            return None
        work = info.rcWork
        return (work.left, work.top, work.right, work.bottom)


class WindowsForegroundProbe:
    """``GetForegroundWindow`` + title, process image name and geometry.
    ``api`` is injectable; the real ctypes API is built on first use, never
    at import."""

    def __init__(self, api: Any = None) -> None:
        self._api = api

    def is_self_foreground(self) -> bool:
        return self.foreground_kind() == SELF_KIND_MAIN

    def foreground_kind(self) -> str:
        """``main`` (skip the frame), ``overlay`` (a small OpenCohost window:
        capture) or ``none``. Never raises."""
        try:
            if self._api is None:
                self._api = _Win32Api()
            api = self._api
            hwnd = api.foreground_window()
            if not hwnd:
                return SELF_KIND_NONE
            title = api.window_title(hwnd)
            try:
                process = api.process_path(hwnd)
            except Exception:
                process = None  # access denied etc.: the title still decides
            if not is_self_window(title, process):
                return SELF_KIND_NONE
        except Exception:
            return SELF_KIND_NONE  # fail-open: never skip a frame on a probe error
        try:
            covers = covers_monitor(api.window_rect(hwnd), api.monitor_work_area(hwnd))
        except Exception:
            covers = None
        # Unknown geometry: keep the conservative pre-V1.13 verdict (self).
        return SELF_KIND_OVERLAY if covers is False else SELF_KIND_MAIN


def default_foreground_probe() -> ForegroundProbe:
    if sys.platform == "win32":
        return WindowsForegroundProbe()
    return NeverSelfProbe()
