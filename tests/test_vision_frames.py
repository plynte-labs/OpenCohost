"""Burst compaction and the in-memory thumbnail ring (vision-on-demand V1.11).

Pure functions over fake frames: no screen, no Pillow decode, no disk.
"""
from __future__ import annotations

import base64
import builtins

import pytest

from opencohost.core.vision.capture import CapturedFrame
from opencohost.core.vision.frames import (
    DEDUP_MEAN_ABS_DIFF,
    MAX_FRAMES_SENT,
    SIGNATURE_SIZE,
    ThumbnailRing,
    compact,
    mean_abs_diff,
    to_data_url,
)

_N = SIGNATURE_SIZE[0] * SIGNATURE_SIZE[1]


def _frame(tag: str, level: int | None = None) -> CapturedFrame:
    sig = bytes([level]) * _N if level is not None else b""
    return CapturedFrame(
        jpeg=tag.encode(), width=1280, height=720, source_width=1920, source_height=1080,
        is_black=False, grab_ms=1.0, encode_ms=1.0, signature=sig, thumbnail=b"t-" + tag.encode(),
    )


def test_signature_is_a_64x36_grayscale_thumbnail():
    assert SIGNATURE_SIZE == (64, 36)


def test_threshold_is_a_small_luma_difference():
    assert 0 < DEDUP_MEAN_ABS_DIFF <= 4.0
    assert MAX_FRAMES_SENT == 3


def test_mean_abs_diff_of_identical_signatures_is_zero():
    assert mean_abs_diff(b"\x10" * _N, b"\x10" * _N) == 0.0


def test_mean_abs_diff_is_the_average_per_pixel_luma_delta():
    assert mean_abs_diff(b"\x10" * _N, b"\x14" * _N) == pytest.approx(4.0)


@pytest.mark.parametrize("a,b", [(b"", b"\x00" * _N), (b"\x00" * _N, b""), (b"\x00" * 3, b"\x00" * 4)])
def test_mean_abs_diff_is_none_when_not_comparable(a, b):
    assert mean_abs_diff(a, b) is None


def test_identical_burst_collapses_to_the_newest_frame():
    frames = [_frame("a", 50), _frame("b", 50), _frame("c", 50)]
    assert [f.jpeg for f in compact(frames)] == [b"c"]


def test_changed_frames_are_all_kept_in_time_order():
    frames = [_frame("a", 10), _frame("b", 60), _frame("c", 120)]
    assert [f.jpeg for f in compact(frames)] == [b"a", b"b", b"c"]


def test_near_duplicate_middle_frame_is_dropped():
    frames = [_frame("a", 10), _frame("b", 11), _frame("c", 120)]
    assert [f.jpeg for f in compact(frames)] == [b"a", b"c"]


def test_newest_frame_is_always_kept_even_when_it_duplicates_the_last_kept():
    frames = [_frame("a", 10), _frame("b", 120), _frame("c", 121)]
    assert [f.jpeg for f in compact(frames)] == [b"a", b"c"]


def test_frames_without_a_signature_are_never_dropped():
    frames = [_frame("a"), _frame("b"), _frame("c")]
    assert len(compact(frames)) == 3


def test_compaction_caps_at_three_keeping_the_newest():
    frames = [_frame(t, i * 40) for i, t in enumerate("abcde")]
    assert [f.jpeg for f in compact(frames)] == [b"c", b"d", b"e"]


def test_compact_of_nothing_is_nothing():
    assert compact([]) == []


def test_data_url_is_base64_jpeg():
    assert to_data_url(b"\xff\xd8") == "data:image/jpeg;base64," + base64.b64encode(b"\xff\xd8").decode()


def test_ring_keeps_the_last_ten_looks():
    ring = ThumbnailRing()
    for i in range(12):
        ring.put(f"id{i}", [b"x%d" % i])
    assert ring.get("id0") is None and ring.get("id1") is None
    assert ring.get("id2") == [b"x2"]
    assert ring.get("id11") == [b"x11"]
    assert len(ring) == 10


def test_ring_unknown_id_is_none_and_clear_empties_it():
    ring = ThumbnailRing()
    ring.put("a", [b"1"])
    assert ring.get("zzz") is None
    ring.clear()
    assert ring.get("a") is None and len(ring) == 0


def test_ring_returns_a_copy():
    ring = ThumbnailRing()
    ring.put("a", [b"1"])
    ring.get("a").append(b"2")
    assert ring.get("a") == [b"1"]


def test_ring_repr_never_shows_bytes():
    ring = ThumbnailRing()
    ring.put("a", [b"SECRETBYTES"])
    assert "SECRETBYTES" not in repr(ring)
    assert "SECRETBYTES" not in str(ring)


def test_frame_repr_hides_signature_and_thumbnail():
    frame = _frame("a", 7)
    frame = CapturedFrame(**{**frame.__dict__, "thumbnail": b"THUMBSECRET", "signature": b"SIGSECRET"})
    assert "THUMBSECRET" not in repr(frame) and "SIGSECRET" not in repr(frame)


def test_ring_never_writes_to_disk(tmp_path, monkeypatch):
    workdir = tmp_path / "cwd"
    workdir.mkdir()
    monkeypatch.chdir(workdir)
    opened: list = []
    real_open = builtins.open

    def spy_open(file, mode="r", *args, **kwargs):
        opened.append((file, mode))
        return real_open(file, mode, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", spy_open)
    ring = ThumbnailRing()
    ring.put("a", [b"\xff\xd8" * 100])
    [to_data_url(t) for t in ring.get("a")]
    ring.clear()
    monkeypatch.setattr(builtins, "open", real_open)

    assert list(workdir.iterdir()) == []
    assert not [m for _, m in opened if any(c in str(m) for c in "wax+")]
