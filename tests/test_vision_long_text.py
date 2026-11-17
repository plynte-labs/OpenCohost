"""Long text + progress lines (vision-on-demand V1.28). Synthetic only."""
from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from opencohost.core import llm_engine
from opencohost.core.engine import llm_engine_vision
from opencohost.core.speech.router import PRIORITY_OWNER, SpeechJobState
from opencohost.core.vision.capture import CapturedFrame
from opencohost.core.vision.describer import OllamaVisionDescriber
from opencohost.core.vision.gate import MODE_CLIP, MODE_DEFAULT, MODE_TEXT, VisionDecision
from opencohost.core.vision.sanitize import SCREEN_BLOCK_CLOSE, build_screen_block
from opencohost.core.vision.turn_step import (
    MODE_PROFILES,
    PROGRESS_INTERVAL_S,
    PROGRESS_LINES,
    VisionTurnStep,
)


@dataclass
class _Settings:
    describe_timeout_s: float = 8.0
    frames: int = 1
    frame_interval_s: float = 0.0
    show_thumbnails: bool = False


class _Gate:
    def __init__(self, mode):
        self.d = VisionDecision(True, ("explicit_look",), 0.0, mode)

    def decide(self, text, source):
        return self.d


class _Capture:
    def capture(self, **kwargs):
        return CapturedFrame(
            jpeg=b"\xff\xd8x", width=10, height=10, source_width=10, source_height=10,
            is_black=False, grab_ms=1.0, encode_ms=1.0, signature=b"\x01" * 2304, thumbnail=b"t",
        )


class _Describer:
    def __init__(self, text="Texto leido.", hold=None):
        self.kwargs = None
        self.text = text
        self.hold = hold

    def describe(self, jpeg, question, *, timeout_s, cancel=None, **extra):
        self.kwargs = {"timeout_s": timeout_s, **extra}
        if self.hold is not None:
            self.hold()
        return self.text


def _step(mode, describer=None, **kw):
    describer = describer or _Describer()
    step = VisionTurnStep(
        _Gate(mode), _Capture(), describer, lambda line: None, settings=_Settings(),
        sleep=lambda s: None, choice=lambda seq: seq[0], **kw,
    )
    return step, describer


# ------------------------------------------------------------ mode profiles


def test_profile_table_per_mode():
    text, clip, default = MODE_PROFILES[MODE_TEXT], MODE_PROFILES[MODE_CLIP], MODE_PROFILES[MODE_DEFAULT]
    assert (text.num_predict, text.max_chars, text.timeout_s) == (1000, 3500, 20.0)
    assert (clip.num_predict, clip.max_chars, clip.timeout_s) == (None, 1200, None)
    assert (default.num_predict, default.max_chars, default.timeout_s) == (None, 1200, None)


def test_text_mode_uses_text_profile_for_describe():
    step, describer = _step(MODE_TEXT)
    assert step.run("lee eso", "direct").outcome == "ok"
    assert describer.kwargs["num_predict"] == 1000
    assert describer.kwargs["timeout_s"] == 20.0
    assert describer.kwargs["num_ctx"] == 4096


def test_text_mode_never_shortens_a_longer_configured_timeout():
    step, describer = _step(MODE_TEXT)
    step.update_settings(_Settings(describe_timeout_s=25.0))
    step.run("lee eso", "direct")
    assert describer.kwargs["timeout_s"] == 25.0


@pytest.mark.parametrize("mode", [MODE_DEFAULT, MODE_CLIP])
def test_other_modes_keep_their_limits(mode):
    step, describer = _step(mode)
    step.run("mira esto", "direct")
    assert "num_predict" not in describer.kwargs
    assert describer.kwargs["timeout_s"] == 8.0


def _body(block):
    return block.split("\n", 1)[1].rsplit("\n", 1)[0]


def test_text_mode_keeps_up_to_3500_chars_in_the_block():
    long = " ".join(["palabra"] * 600)  # about 4800 chars
    step, _ = _step(MODE_TEXT, _Describer(long))
    body = _body(step.run("lee eso", "direct").screen_block)
    assert 3000 < len(body) <= 3500


def test_default_mode_still_caps_at_1200():
    long = " ".join(["palabra"] * 600)
    step, _ = _step(MODE_DEFAULT, _Describer(long))
    body = _body(step.run("mira esto", "direct").screen_block)
    assert len(body) <= 1200


def test_build_screen_block_accepts_a_cap():
    long = " ".join(["palabra"] * 600)
    assert len(_body(build_screen_block(long, max_chars=3500))) > 1200
    assert len(_body(build_screen_block(long))) <= 1200
    assert build_screen_block("x").endswith(SCREEN_BLOCK_CLOSE)


def test_describer_payload_num_predict_override():
    d = OllamaVisionDescriber()
    assert d._payload([b"x"], "q")["options"]["num_predict"] == 350
    assert d._payload([b"x"], "q", num_predict=1000)["options"]["num_predict"] == 1000


# ----------------------------------------------------------- progress lines


class _Ticks:
    """Fake injectable wait: grants ``allowed`` ticks, then blocks until stopped."""

    def __init__(self, allowed):
        self.allowed = allowed
        self.calls = []
        self.lock = threading.Lock()

    def __call__(self, stop, seconds):
        with self.lock:
            self.calls.append(seconds)
            if self.allowed > 0:
                self.allowed -= 1
                return False
        return stop.wait(5)


def _wait_for(pred, timeout=2.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.005)
    return False


def _progress_threads():
    return [t for t in threading.enumerate() if t.name == "vision-progress" and t.is_alive()]


def _gone(timeout=2.0):
    return _wait_for(lambda: not _progress_threads(), timeout)


def test_constants():
    assert PROGRESS_INTERVAL_S == 5.0
    assert PROGRESS_LINES["es"] == ("Sigo leyendo…", "Ya casi…", "Un momento más…")
    assert PROGRESS_LINES["en"] == ("Still reading…", "Almost there…", "One more moment…")


def test_progress_lines_rotate_while_describe_runs_then_stop():
    spoken = []
    ticks = _Ticks(allowed=4)
    describer = _Describer(hold=lambda: _wait_for(lambda: len(spoken) >= 4))
    step, _ = _step(
        MODE_TEXT, describer, speak_progress=spoken.append, progress_wait=ticks,
    )
    step.run("lee eso", "direct")
    assert spoken == ["Sigo leyendo…", "Ya casi…", "Un momento más…", "Sigo leyendo…"]
    assert ticks.calls[0] == PROGRESS_INTERVAL_S
    assert _gone()
    assert len(spoken) == 4


def test_progress_language_follows_the_question():
    spoken = []
    describer = _Describer(hold=lambda: _wait_for(lambda: len(spoken) >= 1))
    step, _ = _step(
        MODE_TEXT, describer, speak_progress=spoken.append, progress_wait=_Ticks(1),
    )
    step.run("what does it say", "direct")
    assert spoken == ["Still reading…"]
    assert _gone()


def test_fast_describe_speaks_no_progress():
    spoken = []
    step, _ = _step(MODE_TEXT, speak_progress=spoken.append, progress_wait=_Ticks(0))
    step.run("lee eso", "direct")
    assert spoken == [] and _gone()


def test_tick_racing_completion_does_not_speak():
    spoken = []
    entered, gate = threading.Event(), threading.Event()

    def wait(stop, seconds):
        entered.set()
        gate.wait(5)  # ignores ``stop`` on purpose: the tick fires after completion
        return False

    describer = _Describer(hold=lambda: entered.wait(2))
    step, _ = _step(MODE_TEXT, describer, speak_progress=spoken.append, progress_wait=wait)
    step.run("lee eso", "direct")
    gate.set()
    assert _gone()
    assert spoken == []


def test_progress_stops_on_cancel_and_on_describe_error():
    from opencohost.core.vision.describer import VisionDescribeError

    spoken = []

    def boom():
        _wait_for(lambda: len(spoken) >= 1)
        raise VisionDescribeError("cancelled")

    step, _ = _step(
        MODE_TEXT, _Describer(hold=boom), speak_progress=spoken.append, progress_wait=_Ticks(1),
    )
    result = step.run("lee eso", "direct")
    assert result.outcome == "describe_cancelled"
    assert _gone() and len(spoken) == 1


def test_speak_progress_errors_are_swallowed():
    calls = []

    def bad(line):
        calls.append(line)
        raise RuntimeError("router down")

    describer = _Describer(hold=lambda: _wait_for(lambda: len(calls) >= 2))
    step, _ = _step(MODE_TEXT, describer, speak_progress=bad, progress_wait=_Ticks(2))
    assert step.run("lee eso", "direct").outcome == "ok"
    assert _gone()


def test_progress_applies_to_every_mode():
    spoken = []
    describer = _Describer(hold=lambda: _wait_for(lambda: len(spoken) >= 1))
    step, _ = _step(
        MODE_DEFAULT, describer, speak_progress=spoken.append, progress_wait=_Ticks(1),
    )
    step.run("mira esto", "direct")
    assert spoken == ["Sigo leyendo…"] and _gone()


def test_progress_logs_only_a_count(caplog):
    import logging

    spoken = []
    describer = _Describer(hold=lambda: _wait_for(lambda: len(spoken) >= 2))
    step, _ = _step(
        MODE_TEXT, describer, speak_progress=spoken.append, progress_wait=_Ticks(2),
    )
    with caplog.at_level(logging.INFO, logger="OpenCohost"):
        step.run("lee eso", "direct")
    line = next(r.getMessage() for r in caplog.records if "[VISION] decision=" in r.getMessage())
    assert "progress_lines=2" in line and "Sigo leyendo" not in line


def test_no_ticker_without_a_speak_progress_callback():
    step, _ = _step(MODE_TEXT, progress_wait=_Ticks(5))
    step.run("lee eso", "direct")
    assert _progress_threads() == []


# ------------------------------------------------------------- engine side


class _Job:
    def __init__(self, text, source, priority):
        self.text, self.source, self.priority = text, source, priority
        self.state = SpeechJobState.QUEUED


class _Router:
    def __init__(self):
        self.submitted, self.discarded = [], []

    def submit(self, text, source, priority, turn_id=None):
        job = _Job(text, source, priority)
        self.submitted.append(job)
        return job

    def discard_job(self, job):
        self.discarded.append(job)
        job.state = SpeechJobState.DISCARDED


@pytest.fixture
def motor():
    m = llm_engine.MotorVocalIA(queue.Queue(), lambda event: None)
    m.ollama = MagicMock()
    m._speech_router_enabled = True
    r = _Router()
    m._ensure_router = lambda: r
    m._speech_router = r
    m._r = r
    return m


def test_progress_line_goes_on_the_owner_band_with_its_own_suffix(motor):
    motor._vision_bridge_source = "ptt"
    motor._vision_speak_progress("Sigo leyendo…")
    job = motor._r.submitted[0]
    assert (job.text, job.priority) == ("Sigo leyendo…", PRIORITY_OWNER)
    assert job.source == "ptt" + llm_engine_vision.VISION_PROGRESS_SUFFIX == "ptt-vision-progress"


def test_progress_line_skipped_while_the_previous_one_is_still_speaking(motor):
    motor._vision_speak_progress("Sigo leyendo…")
    first = motor._r.submitted[0]
    first.state = SpeechJobState.ACTIVE
    motor._vision_speak_progress("Ya casi…")
    assert len(motor._r.submitted) == 1
    first.state = SpeechJobState.FINISHED
    motor._vision_speak_progress("Ya casi…")
    assert len(motor._r.submitted) == 2


def test_progress_line_skipped_while_the_bridge_is_still_speaking(motor):
    motor._vision_speak_bridge("A ver…")
    motor._r.submitted[0].state = SpeechJobState.ACTIVE
    motor._vision_speak_progress("Sigo leyendo…")
    assert len(motor._r.submitted) == 1


def test_no_progress_without_the_speech_router(motor):
    motor._speech_router_enabled = False
    motor._vision_speak_progress("Sigo leyendo…")
    assert motor._r.submitted == []


def test_queued_progress_line_is_retired_when_the_look_ends(motor):
    class Step:
        def run(self, text, source, *, cancel=None):
            motor._vision_speak_progress("Sigo leyendo…")
            return SimpleNamespace(screen_block="", note="", outcome="describe_cancelled",
                                   status_line="", reason_codes=())

    motor._vision = SimpleNamespace(step=Step())
    motor._run_vision_look("lee eso", "direct")
    assert motor._r.discarded == [motor._r.submitted[0]]
