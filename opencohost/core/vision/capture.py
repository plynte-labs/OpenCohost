"""Screen capture port and its ``mss`` adapter (vision-on-demand V1.2).

Frames live in memory only: nothing here writes to disk, and nothing logs image
content. ``mss`` and Pillow belong to the optional ``vision`` extra, so they are
imported lazily and this module imports fine without them.
"""

from __future__ import annotations

import io
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Protocol, runtime_checkable

_EXTRA_HINT = "install the 'vision' extra (pip install opencohost[vision]) to enable screen capture"

_BLACK_LUMA_MAX = 8  # luma below this counts as near-black
_BLACK_FRACTION = 0.98  # share of near-black pixels that marks a frame as black
_THUMB_SIZE = (320, 180)
# V1.11: grayscale signature for burst near-duplicate compaction (frames.py),
# and the ~320 px preview the UI may show of what Kira saw.
SIGNATURE_SIZE = (64, 36)
PREVIEW_WIDTH = 320
_PREVIEW_QUALITY = 70


class CaptureUnavailable(Exception):
    """Capture cannot run: a dependency is missing or the grab failed."""


@dataclass(frozen=True)
class CapturedFrame:
    """One downscaled JPEG frame. ``repr`` never exposes the image bytes."""

    jpeg: bytes = field(repr=False)
    width: int
    height: int
    source_width: int
    source_height: int
    is_black: bool
    grab_ms: float
    encode_ms: float
    # V1.11: 64x36 luma bytes (empty = not comparable) and a ~320 px JPEG preview.
    signature: bytes = field(default=b"", repr=False)
    thumbnail: bytes = field(default=b"", repr=False)

    def __repr__(self) -> str:
        return (
            f"CapturedFrame(jpeg=<{len(self.jpeg)} bytes>, {self.width}x{self.height}, "
            f"source={self.source_width}x{self.source_height}, is_black={self.is_black}, "
            f"grab_ms={self.grab_ms:.1f}, encode_ms={self.encode_ms:.1f}, "
            f"signature=<{len(self.signature)} bytes>, thumbnail=<{len(self.thumbnail)} bytes>)"
        )


@runtime_checkable
class ScreenCapture(Protocol):
    """Port: grab the current screen as a downscaled in-memory JPEG."""

    def capture(self) -> CapturedFrame: ...
    # ``MssCapture`` also accepts ``max_side=`` / ``jpeg_quality=`` keyword
    # overrides (V1.27 text mode); the turn step passes them only in that mode.


def _default_grabber_factory() -> Any:
    try:
        import mss
    except ImportError as exc:
        raise CaptureUnavailable(f"mss is not installed: {_EXTRA_HINT}") from exc
    # ``mss.mss`` is deprecated in favour of ``mss.MSS``.
    ctor = getattr(mss, "MSS", None) or mss.mss
    return ctor()


class MssCapture:
    """Capture one monitor with ``mss``, downscale, and encode as JPEG in memory."""

    def __init__(
        self,
        max_width: int = 1280,
        jpeg_quality: int = 80,
        monitor_index: int = 1,
        grabber_factory: Optional[Callable[[], Any]] = None,
    ) -> None:
        self._max_width = max_width
        self._jpeg_quality = jpeg_quality
        self._monitor_index = monitor_index
        self._grabber_factory = grabber_factory or _default_grabber_factory

    @staticmethod
    def available() -> bool:
        """True when ``mss`` and Pillow import. Never raises."""
        try:
            import mss  # noqa: F401
            from PIL import Image  # noqa: F401
        except Exception:
            return False
        return True

    def capture(
        self, *, max_side: Optional[int] = None, jpeg_quality: Optional[int] = None
    ) -> CapturedFrame:
        """Grab one frame. ``max_side`` replaces the width cap with a LONG-side
        cap (never upscales) and ``jpeg_quality`` the encode quality, both for
        this call only (V1.27 text mode sends full resolution at high quality)."""
        try:
            from PIL import Image
        except ImportError as exc:
            raise CaptureUnavailable(f"Pillow is not installed: {_EXTRA_HINT}") from exc

        t0 = time.perf_counter()
        try:
            with self._grabber_factory() as grabber:
                shot = grabber.grab(grabber.monitors[self._monitor_index])
                size = tuple(shot.size)
                img = Image.frombytes("RGB", size, shot.bgra, "raw", "BGRX")
        except CaptureUnavailable:
            raise
        except Exception as exc:
            raise CaptureUnavailable(f"screen capture failed: {type(exc).__name__}") from exc
        t1 = time.perf_counter()

        source_w, source_h = img.size
        is_black = self._is_black(img)
        signature = img.convert("L").resize(SIGNATURE_SIZE, Image.BILINEAR).tobytes()
        if max_side is not None:
            long_side = max(source_w, source_h)
            if long_side > max_side:
                scale = max_side / long_side
                img = img.resize(
                    (max(1, round(source_w * scale)), max(1, round(source_h * scale))),
                    Image.BILINEAR,
                )
        elif source_w > self._max_width:
            img = img.resize(
                (self._max_width, max(1, round(source_h * self._max_width / source_w))),
                Image.BILINEAR,
            )
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=jpeg_quality or self._jpeg_quality)
        thumbnail = self._preview(img)
        t2 = time.perf_counter()

        return CapturedFrame(
            jpeg=buf.getvalue(),
            width=img.width,
            height=img.height,
            source_width=source_w,
            source_height=source_h,
            is_black=is_black,
            grab_ms=(t1 - t0) * 1000.0,
            encode_ms=(t2 - t1) * 1000.0,
            signature=signature,
            thumbnail=thumbnail,
        )

    @staticmethod
    def _preview(img: Any) -> bytes:
        from PIL import Image

        if img.width > PREVIEW_WIDTH:
            img = img.resize(
                (PREVIEW_WIDTH, max(1, round(img.height * PREVIEW_WIDTH / img.width))),
                Image.BILINEAR,
            )
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=_PREVIEW_QUALITY)
        return buf.getvalue()

    @staticmethod
    def _is_black(img: Any) -> bool:
        from PIL import Image

        hist = img.convert("L").resize(_THUMB_SIZE, Image.BILINEAR).histogram()
        total = sum(hist)
        return total > 0 and sum(hist[:_BLACK_LUMA_MAX]) / total > _BLACK_FRACTION
