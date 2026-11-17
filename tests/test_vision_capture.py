"""Specs for the in-memory screen capture adapter (vision-on-demand V1.2).

No test touches the real screen: a fake grabber returns synthetic BGRA buffers.
"""

from __future__ import annotations

import builtins
import io
import sys

import pytest

from opencohost.core.vision.capture import (
    CapturedFrame,
    CaptureUnavailable,
    MssCapture,
    ScreenCapture,
)

pytest.importorskip("PIL")
from PIL import Image  # noqa: E402


class _Shot:
    def __init__(self, width: int, height: int, bgra: bytes) -> None:
        self.size = (width, height)
        self.bgra = bgra


class FakeGrabber:
    """Stands in for ``mss.MSS``: context manager with ``monitors`` and ``grab``."""

    def __init__(self, width: int, height: int, bgra_pixel: bytes = b"\x40\x80\xc0\xff"):
        self._shot = _Shot(width, height, bgra_pixel * (width * height))
        self.monitors = [{"left": 0, "top": 0, "width": width, "height": height}] * 2
        self.grabbed: list[object] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def grab(self, monitor):
        self.grabbed.append(monitor)
        return self._shot


def _capture(width, height, pixel=b"\x40\x80\xc0\xff", **kwargs) -> MssCapture:
    grabber = FakeGrabber(width, height, pixel)
    return MssCapture(grabber_factory=lambda: grabber, **kwargs)


def _decode(frame: CapturedFrame) -> Image.Image:
    return Image.open(io.BytesIO(frame.jpeg))


def test_downscales_wide_frames_keeping_aspect():
    frame = _capture(1920, 1080).capture()
    assert (frame.width, frame.height) == (1280, 720)
    assert (frame.source_width, frame.source_height) == (1920, 1080)
    img = _decode(frame)
    assert img.format == "JPEG"
    assert img.size == (1280, 720)


def test_does_not_upscale_narrow_frames():
    frame = _capture(640, 360).capture()
    assert (frame.width, frame.height) == (640, 360)
    assert _decode(frame).size == (640, 360)


def test_custom_max_width_is_honoured():
    frame = _capture(1920, 1080, max_width=640).capture()
    assert (frame.width, frame.height) == (640, 360)


def test_bgra_is_converted_to_rgb():
    # BGRA (0x40,0x80,0xC0) -> RGB (0xC0,0x80,0x40)
    frame = _capture(64, 64).capture()
    r, g, b = _decode(frame).getpixel((10, 10))
    assert abs(r - 0xC0) < 12 and abs(g - 0x80) < 12 and abs(b - 0x40) < 12


def test_black_frame_is_flagged():
    frame = _capture(640, 360, pixel=b"\x00\x00\x00\xff").capture()
    assert frame.is_black is True


def test_non_black_frame_is_not_flagged():
    frame = _capture(640, 360, pixel=b"\x40\x80\xc0\xff").capture()
    assert frame.is_black is False


def test_timings_are_recorded():
    frame = _capture(640, 360).capture()
    assert frame.grab_ms >= 0.0
    assert frame.encode_ms >= 0.0


def test_uses_requested_monitor_index():
    grabber = FakeGrabber(640, 360)
    grabber.monitors = [{"id": 0}, {"id": 1}, {"id": 2}]
    MssCapture(monitor_index=2, grabber_factory=lambda: grabber).capture()
    assert grabber.grabbed == [{"id": 2}]


def test_invalid_monitor_index_is_unavailable():
    grabber = FakeGrabber(640, 360)
    cap = MssCapture(monitor_index=9, grabber_factory=lambda: grabber)
    with pytest.raises(CaptureUnavailable):
        cap.capture()


def test_satisfies_screen_capture_protocol():
    cap: ScreenCapture = _capture(64, 64)
    assert cap.capture().jpeg


def test_repr_never_includes_image_bytes():
    frame = _capture(640, 360).capture()
    text = repr(frame)
    assert "bytes" in text
    assert repr(frame.jpeg[:16]) not in text
    assert str(len(frame.jpeg)) in text
    assert "\\xff" not in text


def test_signature_is_a_64x36_grayscale_luma_buffer():
    frame = _capture(1920, 1080).capture()
    assert len(frame.signature) == 64 * 36
    # BGRA (0x40,0x80,0xC0) -> RGB (0xC0,0x80,0x40) -> luma ~ 0.299*192+0.587*128+0.114*64 = 140
    assert abs(frame.signature[0] - 140) <= 2


def test_identical_screens_have_identical_signatures():
    assert _capture(1920, 1080).capture().signature == _capture(1920, 1080).capture().signature


def test_thumbnail_is_a_320_wide_jpeg():
    frame = _capture(1920, 1080).capture()
    img = Image.open(io.BytesIO(frame.thumbnail))
    assert img.format == "JPEG"
    assert img.size == (320, 180)


def test_narrow_frames_never_upscale_the_thumbnail():
    frame = _capture(200, 100).capture()
    assert Image.open(io.BytesIO(frame.thumbnail)).size == (200, 100)


def test_repr_hides_signature_and_thumbnail():
    text = repr(_capture(640, 360).capture())
    assert "\\x" not in text
    assert "thumbnail=<" in text


def test_frame_is_frozen():
    frame = _capture(64, 64).capture()
    with pytest.raises(Exception):
        frame.width = 1  # type: ignore[misc]


def test_missing_dependency_raises_capture_unavailable(monkeypatch):
    monkeypatch.setitem(sys.modules, "mss", None)
    monkeypatch.setitem(sys.modules, "PIL", None)
    monkeypatch.setitem(sys.modules, "PIL.Image", None)
    with pytest.raises(CaptureUnavailable, match="vision"):
        MssCapture().capture()


def test_missing_pillow_with_injected_grabber_is_unavailable(monkeypatch):
    monkeypatch.setitem(sys.modules, "PIL", None)
    monkeypatch.setitem(sys.modules, "PIL.Image", None)
    with pytest.raises(CaptureUnavailable, match="vision"):
        _capture(64, 64).capture()


def test_available_is_false_without_dependencies_and_never_raises(monkeypatch):
    monkeypatch.setitem(sys.modules, "mss", None)
    assert MssCapture.available() is False


def test_available_is_true_when_dependencies_import():
    pytest.importorskip("mss")
    assert MssCapture.available() is True


def test_grab_failure_raises_capture_unavailable():
    class Boom(FakeGrabber):
        def grab(self, monitor):
            raise OSError("screen grab failed")

    grabber = Boom(64, 64)
    with pytest.raises(CaptureUnavailable):
        MssCapture(grabber_factory=lambda: grabber).capture()


def test_capture_never_writes_to_disk(tmp_path, monkeypatch):
    # tmp_path already holds fixture-made dirs (e.g. logs/); capture in a fresh cwd.
    workdir = tmp_path / "cwd"
    workdir.mkdir()
    monkeypatch.chdir(workdir)
    opened: list[tuple] = []
    real_open = builtins.open

    def spy_open(file, mode="r", *args, **kwargs):
        opened.append((file, mode))
        return real_open(file, mode, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", spy_open)
    _capture(1920, 1080).capture()
    monkeypatch.setattr(builtins, "open", real_open)

    assert list(workdir.iterdir()) == []
    assert not [m for _, m in opened if any(c in str(m) for c in "wax+")]
