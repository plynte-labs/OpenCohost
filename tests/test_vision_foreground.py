"""Foreground self-capture guard (vision-on-demand V1.11).

The Windows probe is exercised through an injected fake API: tests never call
the real GetForegroundWindow.
"""
from __future__ import annotations

import pytest

from opencohost.core.vision import foreground as fg


@pytest.mark.parametrize(
    "title,process",
    [
        ("OpenCohost", None),
        ("  opencohost ", None),
        (None, r"C:\Program Files\OpenCohost\OpenCohost.exe"),
        ("", r"E:\x\target\debug\opencohost_tauri_shell.exe"),
        ("whatever", "/usr/bin/OPENCOHOST_TAURI_SHELL.EXE"),
    ],
)
def test_self_window_matches_title_or_process(title, process):
    assert fg.is_self_window(title, process) is True


@pytest.mark.parametrize(
    "title,process",
    [
        ("OpenCohost - GitHub - Google Chrome", r"C:\chrome.exe"),
        ("Elden Ring", r"C:\games\eldenring.exe"),
        (None, None),
        ("", ""),
        ("Kira OpenCohost notes.txt - Notepad", r"C:\Windows\notepad.exe"),
    ],
)
def test_other_windows_are_not_self(title, process):
    assert fg.is_self_window(title, process) is False


class FakeApi:
    def __init__(self, hwnd=42, title="OpenCohost", process=None, error=None):
        self.hwnd = hwnd
        self.title = title
        self.process = process
        self.error = error

    def foreground_window(self):
        if self.error:
            raise self.error
        return self.hwnd

    def window_title(self, hwnd):
        return self.title

    def process_path(self, hwnd):
        return self.process


def test_windows_probe_detects_the_app_window():
    assert fg.WindowsForegroundProbe(api=FakeApi()).is_self_foreground() is True


def test_windows_probe_detects_the_app_process():
    api = FakeApi(title="", process=r"C:\a\OpenCohost.exe")
    assert fg.WindowsForegroundProbe(api=api).is_self_foreground() is True


def test_windows_probe_other_window_is_not_self():
    api = FakeApi(title="Elden Ring", process=r"C:\g\eldenring.exe")
    assert fg.WindowsForegroundProbe(api=api).is_self_foreground() is False


def test_windows_probe_without_foreground_window_is_not_self():
    assert fg.WindowsForegroundProbe(api=FakeApi(hwnd=0)).is_self_foreground() is False


def test_windows_probe_fails_open_on_error():
    api = FakeApi(error=OSError("boom"))
    assert fg.WindowsForegroundProbe(api=api).is_self_foreground() is False


def test_process_lookup_error_still_checks_the_title():
    class Api(FakeApi):
        def process_path(self, hwnd):
            raise OSError("access denied")

    assert fg.WindowsForegroundProbe(api=Api(title="OpenCohost")).is_self_foreground() is True
    assert fg.WindowsForegroundProbe(api=Api(title="Game")).is_self_foreground() is False


def test_never_self_probe():
    assert fg.NeverSelfProbe().is_self_foreground() is False


def test_default_probe_is_never_self_off_windows(monkeypatch):
    monkeypatch.setattr(fg.sys, "platform", "linux")
    assert isinstance(fg.default_foreground_probe(), fg.NeverSelfProbe)


def test_default_probe_on_windows_is_lazy_and_never_calls_win32_at_build(monkeypatch):
    monkeypatch.setattr(fg.sys, "platform", "win32")
    probe = fg.default_foreground_probe()
    assert isinstance(probe, fg.WindowsForegroundProbe)


# ----------------------------------------- V1.13: main window vs small overlay

WORK_AREA = (0, 0, 1920, 1040)  # 1080p monitor minus the taskbar


class GeoApi(FakeApi):
    """FakeApi plus the window/monitor geometry the V1.13 probe reads."""

    def __init__(self, rect=None, work=WORK_AREA, geo_error=None, **kw):
        super().__init__(**kw)
        self.rect = rect
        self.work = work
        self.geo_error = geo_error

    def window_rect(self, hwnd):
        if self.geo_error:
            raise self.geo_error
        return self.rect

    def monitor_work_area(self, hwnd):
        return self.work


@pytest.mark.parametrize(
    "rect",
    [
        (0, 0, 1920, 1040),  # maximized
        (-8, -8, 1928, 1048),  # maximized with the invisible resize border
        (200, 100, 1600, 900),  # 1400x800 = 56% of the work area
    ],
)
def test_app_window_covering_most_of_the_monitor_is_main(rect):
    probe = fg.WindowsForegroundProbe(api=GeoApi(rect=rect))
    assert probe.foreground_kind() == fg.SELF_KIND_MAIN
    assert probe.is_self_foreground() is True


def test_small_app_window_is_an_overlay_and_is_captured():
    # the desktop companion: 320x440, same process as the main window
    api = GeoApi(rect=(1500, 500, 1820, 940), title="kira-desktop",
                 process=r"E:\x\target\debug\opencohost_tauri_shell.exe")
    probe = fg.WindowsForegroundProbe(api=api)
    assert probe.foreground_kind() == fg.SELF_KIND_OVERLAY
    assert probe.is_self_foreground() is False


def test_window_mostly_off_its_monitor_counts_only_the_visible_part():
    # 1400x800 window, but only a 400x800 sliver is on the work area
    api = GeoApi(rect=(1520, 100, 2920, 900))
    assert fg.WindowsForegroundProbe(api=api).foreground_kind() == fg.SELF_KIND_OVERLAY


@pytest.mark.parametrize(
    "api",
    [
        GeoApi(rect=None),
        GeoApi(rect=(0, 0, 320, 440), work=None),
        GeoApi(rect=(0, 0, 320, 440), work=(0, 0, 0, 0)),
        GeoApi(geo_error=OSError("no geometry")),
        FakeApi(),  # an api without the geometry calls at all
    ],
)
def test_unknown_geometry_keeps_the_conservative_self_verdict(api):
    probe = fg.WindowsForegroundProbe(api=api)
    assert probe.foreground_kind() == fg.SELF_KIND_MAIN
    assert probe.is_self_foreground() is True


def test_other_window_kind_is_none_and_geometry_is_not_read():
    api = GeoApi(geo_error=AssertionError("geometry read for a foreign window"),
                 title="Elden Ring", process=r"C:\g\eldenring.exe")
    assert fg.WindowsForegroundProbe(api=api).foreground_kind() == fg.SELF_KIND_NONE


def test_probe_error_kind_is_none():
    assert fg.WindowsForegroundProbe(api=GeoApi(error=OSError("x"))).foreground_kind() == fg.SELF_KIND_NONE


def test_never_self_probe_kind_is_none():
    assert fg.NeverSelfProbe().foreground_kind() == fg.SELF_KIND_NONE


@pytest.mark.parametrize(
    "rect,work,expected",
    [
        ((0, 0, 100, 100), (0, 0, 100, 100), True),
        ((0, 0, 100, 50), (0, 0, 100, 100), True),  # exactly half
        ((0, 0, 100, 49), (0, 0, 100, 100), False),
        ((200, 200, 300, 300), (0, 0, 100, 100), False),  # no overlap
        (None, (0, 0, 100, 100), None),
        ((0, 0, 100, 100), None, None),
        ((0, 0, 100, 100), (0, 0, 0, 100), None),
    ],
)
def test_covers_monitor(rect, work, expected):
    assert fg.covers_monitor(rect, work) is expected
