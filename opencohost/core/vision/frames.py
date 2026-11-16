"""Burst compaction and the in-memory thumbnail ring (vision-on-demand V1.11).

A look takes a short burst of frames. Sending three identical screenshots
costs ~0.7 s of VL prompt eval each and tells the model nothing, so the burst
is compacted on a tiny grayscale signature before the describe call.

Threshold choice (``DEDUP_MEAN_ABS_DIFF``): the signature is a 64x36 luma
thumbnail computed from the raw grab, before JPEG encoding, so an unchanged
screen differs by exactly 0 -- there is no codec noise to absorb. A 1920x1080
frame shrinks ~30x per axis, so a local change is diluted by its area:
a moving cursor or blinking caret scores ~0.05, a changed line of text ~0.3-0.5,
while a scene change, a scroll or a new window scores well above 5. A
threshold of 2.0 (under 1% of the 0-255 range) drops "nothing really moved"
frames and keeps every frame where the picture changed. Small HUD changes can
fall under it; that is acceptable because the newest frame is always kept, so
the latest value is still what the model sees. Not yet measured on real game
footage -- tune here if the live run shows otherwise.

Privacy: thumbnails live only in this process's memory, in a bounded ring;
nothing here touches the disk or logs bytes (``repr`` shows counts only).
"""
from __future__ import annotations

import base64
import threading
from collections import OrderedDict
from typing import Optional, Sequence

from opencohost.core.vision.capture import SIGNATURE_SIZE, CapturedFrame

DEDUP_MEAN_ABS_DIFF = 2.0
MAX_FRAMES_SENT = 3
THUMBNAIL_RING_SIZE = 10

__all__ = [
    "DEDUP_MEAN_ABS_DIFF",
    "MAX_FRAMES_SENT",
    "SIGNATURE_SIZE",
    "THUMBNAIL_RING_SIZE",
    "ThumbnailRing",
    "compact",
    "mean_abs_diff",
    "to_data_url",
]


def mean_abs_diff(a: bytes, b: bytes) -> Optional[float]:
    """Mean per-pixel luma difference, or None when the signatures cannot be
    compared (missing or different sizes)."""
    if not a or not b or len(a) != len(b):
        return None
    return sum(abs(x - y) for x, y in zip(a, b)) / len(a)


def _duplicate(frame: CapturedFrame, kept: CapturedFrame, threshold: float) -> bool:
    diff = mean_abs_diff(frame.signature, kept.signature)
    return diff is not None and diff < threshold


def compact(
    frames: Sequence[CapturedFrame],
    *,
    threshold: float = DEDUP_MEAN_ABS_DIFF,
    cap: int = MAX_FRAMES_SENT,
) -> list[CapturedFrame]:
    """Time-ordered frames with near-duplicates dropped.

    Each frame is compared to the last KEPT one (a stable anchor, so a slow
    drift is not collapsed step by step). A near-duplicate is dropped, except
    the newest frame, which replaces the kept one it duplicates: what the
    model sees last is always the most recent screen. At most ``cap`` frames,
    keeping the newest.
    """
    kept: list[CapturedFrame] = []
    last = len(frames) - 1
    for index, frame in enumerate(frames):
        if kept and _duplicate(frame, kept[-1], threshold):
            if index == last:
                kept[-1] = frame
            continue
        kept.append(frame)
    return kept[-cap:] if cap > 0 else []


def to_data_url(jpeg: bytes) -> str:
    return "data:image/jpeg;base64," + base64.b64encode(jpeg).decode("ascii")


class ThumbnailRing:
    """The last ``capacity`` looks' frames (the JPEGs sent to the model), keyed
    by look id, plus each look's description text. Thread-safe.

    The description lives and dies with its frames: it can only be set for a
    look whose frames are in the ring, and eviction/clear drop both. Neither
    appears in ``repr``.
    """

    def __init__(self, capacity: int = THUMBNAIL_RING_SIZE) -> None:
        self._capacity = capacity
        self._items: "OrderedDict[str, list[bytes]]" = OrderedDict()
        self._descriptions: dict[str, str] = {}
        self._lock = threading.Lock()

    def put(self, look_id: str, thumbnails: Sequence[bytes]) -> None:
        with self._lock:
            self._items.pop(look_id, None)
            self._descriptions.pop(look_id, None)
            self._items[look_id] = list(thumbnails)
            while len(self._items) > self._capacity:
                evicted, _ = self._items.popitem(last=False)
                self._descriptions.pop(evicted, None)

    def set_description(self, look_id: str, text: str) -> None:
        """Attach ``text`` to a look whose frames are still held; else a no-op."""
        with self._lock:
            if look_id in self._items and text:
                self._descriptions[look_id] = text

    def get_description(self, look_id: str) -> Optional[str]:
        with self._lock:
            return self._descriptions.get(look_id)

    def get(self, look_id: str) -> Optional[list[bytes]]:
        with self._lock:
            items = self._items.get(look_id)
            return list(items) if items is not None else None

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
            self._descriptions.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)

    def __repr__(self) -> str:
        return f"ThumbnailRing(looks={len(self)}, capacity={self._capacity})"
