"""Look modes (vision-on-demand V1.26 clip, V1.27 text).

Synthetic frames only: no real screen, no Ollama, no GPU.
"""
from __future__ import annotations

import io
import logging
from dataclasses import dataclass

import pytest

from opencohost.core.vision.capture import CapturedFrame, MssCapture
from opencohost.core.vision.describer import MOSAIC_HINT, OllamaVisionDescriber
from opencohost.core.vision.gate import (
    MODE_CLIP,
    MODE_DEFAULT,
    MODE_TEXT,
    ExplicitTriggerGate,
    RuleVisionGate,
    look_mode,
)
from opencohost.core.vision.turn_step import (
    CLIP_FRAME_INTERVAL_S,
    CLIP_FRAMES,
    CLIP_NUM_CTX,
    TEXT_JPEG_QUALITY,
    TEXT_MAX_SIDE,
    TEXT_NUM_CTX,
    VisionTurnStep,
)

pytest.importorskip("PIL")
from PIL import Image  # noqa: E402


# --------------------------------------------------------------------- gate


@pytest.mark.parametrize(
    "text",
    [
        "mira este clip", "qué pasa en el video", "¿qué pasa en el vídeo?", "qué está pasando",
        "mira la animación", "mira el movimiento", "mira mi gameplay", "watch this",
        "what's happening here", "look at this video", "MIRA ESTE CLIP",
    ],
)
def test_clip_words_select_clip_mode(text):
    assert look_mode(text) == MODE_CLIP


@pytest.mark.parametrize(
    "text",
    [
        "qué dice ahí", "léeme esto", "lee eso", "mira el texto", "la letra de esto",
        "read this", "what does it say", "what does that sign say", "QUÉ DICE ESO",
    ],
)
def test_text_words_select_text_mode(text):
    assert look_mode(text) == MODE_TEXT


@pytest.mark.parametrize("text", ["mira esto", "check this out", "qué jefe es este", ""])
def test_everything_else_is_default_mode(text):
    assert look_mode(text) == MODE_DEFAULT


def test_both_match_explicit_clip_word_wins():
    assert look_mode("mira este video y lee el texto") == MODE_CLIP


def test_both_match_soft_clip_phrase_loses_to_text():
    assert look_mode("qué pasa, léeme el texto") == MODE_TEXT


def test_soft_clip_phrase_alone_is_clip():
    assert look_mode("¿qué pasa ahí?") == MODE_CLIP


def test_look_mode_never_raises_on_non_text():
    assert look_mode(None) == MODE_DEFAULT  # type: ignore[arg-type]


def test_rule_gate_triggers_on_unambiguous_clip_words():
    gate = RuleVisionGate()
    assert gate.decide("mira este clip", "direct").needs_vision
    assert gate.decide("qué pasa en este video", "direct").needs_vision
    assert not gate.decide("qué pasa", "direct").needs_vision  # too chatty to trigger alone


def test_armed_look_carries_its_mode_once():
    gate = ExplicitTriggerGate(look_on_ptt=False, capture_delay_s=0.0)
    gate.arm_look("clip")
    first = gate.decide("hola", "direct")
    assert first.needs_vision and first.mode == MODE_CLIP
    assert gate.decide("hola", "direct").needs_vision is False


def test_armed_look_without_mode_is_default():
    gate = ExplicitTriggerGate(look_on_ptt=False, capture_delay_s=0.0)
    gate.arm_look()
    assert gate.decide("hola", "direct").mode == MODE_DEFAULT


# ------------------------------------------------------------------ capture


class _Shot:
    def __init__(self, width, height):
        self.size = (width, height)
        self.bgra = b"\x40\x80\xc0\xff" * (width * height)


class _Grabber:
    def __init__(self, width, height):
        self._shot = _Shot(width, height)
        self.monitors = [{}] * 2

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def grab(self, monitor):
        return self._shot


def _mss(width, height):
    return MssCapture(grabber_factory=lambda: _Grabber(width, height))


def test_capture_long_side_cap_keeps_aspect_and_ignores_the_1280_width():
    frame = _mss(2560, 1440).capture(max_side=1920)
    assert (frame.width, frame.height) == (1920, 1080)


def test_capture_long_side_cap_never_upscales_and_keeps_full_resolution():
    frame = _mss(1920, 1080).capture(max_side=1920)
    assert (frame.width, frame.height) == (1920, 1080)


def test_capture_long_side_cap_handles_portrait():
    frame = _mss(1080, 2400).capture(max_side=1920)
    assert (frame.width, frame.height) == (864, 1920)


def test_capture_jpeg_quality_override_changes_size():
    low = _mss(640, 360).capture(jpeg_quality=30).jpeg
    high = _mss(640, 360).capture(jpeg_quality=95).jpeg
    assert len(high) >= len(low)
    noisy = MssCapture(grabber_factory=lambda: _NoisyGrabber())
    assert len(noisy.capture(jpeg_quality=95).jpeg) > len(noisy.capture(jpeg_quality=30).jpeg)


class _NoisyGrabber(_Grabber):
    def __init__(self):
        super().__init__(640, 360)
        import os

        self._shot.bgra = os.urandom(640 * 360 * 4)


def test_capture_default_call_is_unchanged():
    frame = _mss(1920, 1080).capture()
    assert (frame.width, frame.height) == (1280, 720)


# ----------------------------------------------------------------- describer


def _payload(describer, frames, **kw):
    return describer._payload(frames, "q", **kw)


def test_payload_num_ctx_override_and_default():
    d = OllamaVisionDescriber(num_ctx=4096)
    assert _payload(d, [b"x"])["options"]["num_ctx"] == 4096
    assert _payload(d, [b"x"], num_ctx=8192)["options"]["num_ctx"] == 8192


def test_payload_hint_applies_to_a_single_image():
    d = OllamaVisionDescriber(lang="en")
    content = _payload(d, [b"x"], hint=MOSAIC_HINT)["messages"][1]["content"]
    assert MOSAIC_HINT in content
    assert "frames in time order; describe" not in content  # not the multi-frame hint


def test_mosaic_hint_wording():
    assert "single image is a grid of frames in time order" in MOSAIC_HINT
    assert "index" in MOSAIC_HINT and "quote visible text" in MOSAIC_HINT


# -------------------------------------------------------------------- mosaic


def _jpeg(color, size=(1280, 720)):
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "JPEG", quality=90)
    return buf.getvalue()


def test_mosaic_is_one_1920x720_jpeg_with_cells_in_order():
    from opencohost.core.vision.mosaic import build_mosaic

    colors = [(250, 0, 0), (0, 250, 0), (0, 0, 250), (250, 250, 0), (250, 0, 250), (0, 250, 250)]
    img = Image.open(io.BytesIO(build_mosaic([_jpeg(c) for c in colors])))
    assert img.format == "JPEG" and img.size == (1920, 720)
    for i, color in enumerate(colors):
        px = img.convert("RGB").getpixel(((i % 3) * 640 + 320, (i // 3) * 360 + 150))
        assert max(abs(a - b) for a, b in zip(px, color)) < 40, (i, px)


def test_mosaic_label_sits_bottom_right_and_leaves_the_center_clean():
    from opencohost.core.vision.mosaic import build_mosaic

    img = Image.open(io.BytesIO(build_mosaic([_jpeg((120, 120, 120))] * 6))).convert("RGB")
    corner = [img.getpixel((640 - x, 360 - y)) for x in range(8, 62) for y in range(4, 34)]
    assert any(p[1] > 180 and p[0] < 100 for p in corner)  # green index glyph
    assert max(abs(a - 120) for a in img.getpixel((320, 180))) < 8


def test_mosaic_letterboxes_other_aspect_ratios():
    from opencohost.core.vision.mosaic import build_mosaic

    tall = _jpeg((250, 250, 250), size=(500, 1000))
    img = Image.open(io.BytesIO(build_mosaic([tall] * 6))).convert("RGB")
    assert img.getpixel((5, 180))[0] < 40  # left bar is black
    assert img.getpixel((320, 180))[0] > 200  # content in the middle


def test_mosaic_fewer_frames_leave_empty_cells():
    from opencohost.core.vision.mosaic import build_mosaic

    img = Image.open(io.BytesIO(build_mosaic([_jpeg((250, 250, 250))] * 4))).convert("RGB")
    assert img.size == (1920, 720)
    assert img.getpixel((1280 + 320, 360 + 150))[0] < 20  # 6th cell is empty


def test_preview_is_a_small_jpeg():
    from opencohost.core.vision.mosaic import make_preview

    img = Image.open(io.BytesIO(make_preview(_jpeg((1, 2, 3), size=(1920, 720)))))
    assert img.format == "JPEG" and img.width == 960


# ----------------------------------------------------------------- turn step


@dataclass
class _Settings:
    describe_timeout_s: float = 8.0
    frames: int = 3
    frame_interval_s: float = 1.0
    show_thumbnails: bool = True


class _Gate:
    def __init__(self, mode=MODE_DEFAULT):
        from opencohost.core.vision.gate import VisionDecision

        self.decision = VisionDecision(True, ("explicit_look",), 0.0, mode)

    def decide(self, text, source):
        return self.decision


class _Capture:
    def __init__(self, size=(1280, 720)):
        self.calls = []
        self.size = size
        self.n = 0

    def capture(self, **kwargs):
        self.calls.append(kwargs)
        self.n += 1
        # distinct frames so compaction would not merge them even if applied
        jpeg = _jpeg((self.n * 30 % 250, 90, 40), size=self.size)
        return CapturedFrame(
            jpeg=jpeg, width=self.size[0], height=self.size[1], source_width=1920,
            source_height=1080, is_black=False, grab_ms=1.0, encode_ms=1.0,
            signature=bytes([self.n * 30 % 250]) * 2304, thumbnail=b"thumb%d" % self.n,
        )


class _Describer:
    def __init__(self):
        self.kwargs = None
        self.args = None

    def describe(self, jpeg, question, *, timeout_s, cancel=None, **extra):
        self.args = jpeg
        self.kwargs = {"timeout_s": timeout_s, **extra}
        return "Se ve un contador subiendo."


def _step(mode=MODE_DEFAULT, settings=None, capture=None, thumbs=None):
    sleeps = []
    capture = capture or _Capture()
    describer = _Describer()
    step = VisionTurnStep(
        _Gate(mode), capture, describer, lambda line: None,
        settings=settings or _Settings(),
        sleep=sleeps.append, choice=lambda seq: seq[0],
        on_thumbnails=(lambda i, t: thumbs.append((i, t))) if thumbs is not None else None,
    )
    return step, capture, describer, sleeps


def test_constants():
    assert CLIP_FRAMES == 6
    assert 0.3 <= CLIP_FRAME_INTERVAL_S <= 1.0  # 5 gaps spread over about 3 s
    assert CLIP_NUM_CTX == 4096 and TEXT_NUM_CTX == 4096
    assert TEXT_MAX_SIDE == 1920 and TEXT_JPEG_QUALITY == 92


def test_default_mode_is_unchanged():
    step, capture, describer, _ = _step()
    result = step.run("mira esto", "direct")
    assert result.outcome == "ok" and result.mode == MODE_DEFAULT
    assert len(describer.args) == 3 and capture.calls == [{}, {}, {}]
    assert describer.kwargs == {"timeout_s": 8.0}


def test_clip_mode_sends_one_mosaic_with_hint_and_ctx():
    step, capture, describer, sleeps = _step(MODE_CLIP)
    result = step.run("mira este clip", "direct")
    assert result.outcome == "ok" and result.mode == MODE_CLIP
    assert "mode_clip" in result.reason_codes
    assert len(capture.calls) == CLIP_FRAMES and all(c == {} for c in capture.calls)
    assert sleeps == [CLIP_FRAME_INTERVAL_S] * (CLIP_FRAMES - 1)
    assert len(describer.args) == 1
    assert Image.open(io.BytesIO(describer.args[0])).size == (1920, 720)
    assert describer.kwargs == {"timeout_s": 8.0, "num_ctx": CLIP_NUM_CTX, "hint": MOSAIC_HINT}
    assert result.frames_captured == CLIP_FRAMES and result.frames_sent == CLIP_FRAMES


def test_clip_mode_does_not_compact_identical_frames():
    class Static(_Capture):
        def capture(self, **kwargs):
            frame = super().capture(**kwargs)
            return CapturedFrame(**{**frame.__dict__, "signature": b"\x10" * 2304})

    step, _, describer, _ = _step(MODE_CLIP, capture=Static())
    result = step.run("mira este clip", "direct")
    assert result.frames_sent == CLIP_FRAMES  # time spread kept


def test_clip_ring_frame_is_the_mosaic_sent_to_the_model_not_a_preview():
    thumbs = []
    step, _, describer, _ = _step(MODE_CLIP, thumbs=thumbs)
    step.run("mira este clip", "direct")
    assert len(thumbs) == 1 and len(thumbs[0][1]) == 1
    assert thumbs[0][1] == list(describer.args)


def test_clip_with_a_single_usable_frame_falls_back_to_a_plain_frame():
    from opencohost.core.vision.capture import CaptureUnavailable

    class OneThenFail(_Capture):
        def capture(self, **kwargs):
            if self.n >= 1:
                raise CaptureUnavailable("x")
            return super().capture(**kwargs)

    step, _, describer, _ = _step(MODE_CLIP, capture=OneThenFail())
    result = step.run("mira este clip", "direct")
    assert result.outcome == "ok"
    assert Image.open(io.BytesIO(describer.args[0])).size == (1280, 720)
    assert "hint" not in describer.kwargs


def test_text_mode_sends_one_max_quality_frame():
    thumbs = []
    step, capture, describer, sleeps = _step(MODE_TEXT, thumbs=thumbs)
    result = step.run("qué dice ahí", "direct")
    assert result.outcome == "ok" and result.mode == MODE_TEXT
    assert "mode_text" in result.reason_codes
    assert capture.calls == [{"max_side": TEXT_MAX_SIDE, "jpeg_quality": TEXT_JPEG_QUALITY}]
    assert sleeps == []
    assert len(describer.args) == 1
    assert describer.kwargs == {"timeout_s": 20.0, "num_ctx": TEXT_NUM_CTX, "num_predict": 1000}  # V1.28
    assert thumbs[0][1] == list(describer.args)  # the sent frame, not the preview


def test_mode_is_decided_from_the_utterance_when_the_gate_says_default():
    step, capture, describer, _ = _step(MODE_DEFAULT)
    assert step.run("mira este clip", "direct").mode == MODE_CLIP
    assert step.run("lee eso", "direct").mode == MODE_TEXT


def test_armed_mode_beats_the_utterance():
    step, *_ = _step(MODE_CLIP)
    assert step.run("lee eso", "direct").mode == MODE_CLIP


def test_log_line_carries_mode_and_no_content(caplog):
    step, *_ = _step(MODE_CLIP)
    with caplog.at_level(logging.INFO, logger="OpenCohost"):
        step.run("mira este clip", "direct")
    line = next(r.getMessage() for r in caplog.records if "[VISION] decision=" in r.getMessage())
    assert "mode=clip" in line and "contador" not in line


def test_self_capture_still_applies_per_clip_frame():
    class Probe:
        def __init__(self):
            self.kinds = iter(["main", "none", "main", "none", "none", "none"])

        def foreground_kind(self):
            return next(self.kinds)

    capture = _Capture()
    step = VisionTurnStep(
        _Gate(MODE_CLIP), capture, _Describer(), lambda line: None, settings=_Settings(),
        sleep=lambda s: None, choice=lambda seq: seq[0], foreground_probe=Probe(),
    )
    result = step.run("mira este clip", "direct")
    assert result.self_capture_skipped == 2 and result.frames_captured == 4


# ------------------------------------------------------- controller / API arm


def test_api_look_accepts_optional_mode_and_rejects_unknown():
    from tests.test_api_vision import _client

    app, client = _client()
    try:
        calls = []
        app.state.host.vision_arm_look = lambda mode=None: calls.append(mode) or True
        assert client.post("/api/vision/look").json() == {"armed": True}
        assert client.post("/api/vision/look", json={"mode": "clip"}).json() == {
            "armed": True, "mode": "clip",
        }
        assert client.post("/api/vision/look", json={"mode": "texto"}).json()["mode"] == "text"
        assert client.post("/api/vision/look", json={"mode": "bogus"}).status_code == 422
        assert calls == [None, "clip", "text"]
    finally:
        client.__exit__(None, None, None)


def test_controller_arm_look_passes_the_mode_to_the_next_look():
    from tests.test_vision_controller import ON, Harness

    h = Harness()
    h.controller.apply(ON)
    assert h.controller.arm_look("clip") is True
    decision = h.controller.step.gate.decide("hola", "direct")
    assert decision.needs_vision and decision.mode == MODE_CLIP
