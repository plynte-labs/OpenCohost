"""Behavior tests for VisionTurnStep (vision-on-demand V1.5).

Everything is faked: no screen, no Ollama, no GPU. The recorded call list
pins the owner requirement that the bridge line is the FIRST thing that
happens on a vision turn, strictly before capture and describe.
"""
from __future__ import annotations

import builtins
import logging
import threading
from dataclasses import dataclass

import pytest

from opencohost.core.vision.capture import CaptureUnavailable, CapturedFrame
from opencohost.core.vision.describer import VisionDescribeError
from opencohost.core.vision.gate import ExplicitTriggerGate, VisionDecision
from opencohost.core.vision.sanitize import SCREEN_BLOCK_CLOSE, SCREEN_BLOCK_OPEN
from opencohost.core.vision.turn_step import (
    BRIDGE_LINES,
    VisionTurnResult,
    VisionTurnStep,
    detect_language,
)

SECRET = "zanahoriapurpura"  # distinctive word that must never reach a log


@dataclass
class _Settings:
    describe_timeout_s: float = 8.0


class FakeGate:
    def __init__(self, decision: VisionDecision, calls: list):
        self.decision = decision
        self.calls = calls

    def decide(self, text, source):
        self.calls.append(("gate", source))
        return self.decision


class FakeCapture:
    def __init__(self, calls, *, black=False, error=None):
        self.calls = calls
        self.black = black
        self.error = error

    def capture(self, **kwargs):  # V1.27: text mode passes max_side/jpeg_quality
        self.calls.append(("capture",))
        if self.error is not None:
            raise self.error
        return CapturedFrame(
            jpeg=b"\xff\xd8fakejpeg",
            width=1280,
            height=720,
            source_width=1920,
            source_height=1080,
            is_black=self.black,
            grab_ms=10.0,
            encode_ms=5.0,
        )


class FakeDescriber:
    def __init__(self, calls, *, text="Barra de vida al 30%, 12 balas.", error=None):
        self.calls = calls
        self.text = text
        self.error = error
        self.kwargs = None

    def describe(self, jpeg, question, *, timeout_s, cancel=None, **extra):
        self.calls.append(("describe",))
        self.kwargs = {"jpeg": jpeg, "question": question, "timeout_s": timeout_s, "cancel": cancel}
        if self.error is not None:
            raise self.error
        return self.text


def _look(delay=3.0, reasons=("explicit_look",)):
    return VisionDecision(True, tuple(reasons), delay)


def _build(decision=None, *, capture="ok", describer=None, ready=None, calls=None,
           settings=None, choice=None, on_looking=None, on_result=None):
    calls = [] if calls is None else calls
    if capture == "ok":
        capture = FakeCapture(calls)
    describer = describer if describer is not None else FakeDescriber(calls)
    step = VisionTurnStep(
        FakeGate(decision or _look(), calls),
        capture,
        describer,
        speak_bridge=lambda line: calls.append(("bridge", line)),
        settings=settings or _Settings(),
        ready_check=ready,
        clock=lambda: 0.0,
        sleep=lambda s: calls.append(("sleep", s)),
        choice=choice or (lambda seq: seq[0]),
        on_looking=on_looking,
        on_result=on_result,
    )
    return step, calls, describer


# ------------------------------------------------------------------ no vision


def test_no_vision_decision_costs_nothing():
    step, calls, _ = _build(VisionDecision(False, ("no_trigger",), 0.0))

    result = step.run("hola", "direct")

    assert isinstance(result, VisionTurnResult)
    assert result.screen_block == ""
    assert result.outcome == "no_vision"
    assert "no_trigger" in result.reason_codes
    assert calls == [("gate", "direct")]  # no bridge, no sleep, no capture


# -------------------------------------------------------------------- success


def test_vision_turn_orders_bridge_then_sleep_then_capture_then_describe():
    step, calls, describer = _build(_look(delay=3.0))

    result = step.run("¿qué hago con este jefe?", "direct")

    kinds = [c[0] for c in calls]
    assert kinds == ["gate", "bridge", "sleep", "capture", "describe"]
    assert calls[2] == ("sleep", 3.0)
    assert result.outcome == "ok"
    assert result.screen_block.startswith(SCREEN_BLOCK_OPEN)
    assert result.screen_block.endswith(SCREEN_BLOCK_CLOSE)
    assert "12 balas" in result.screen_block
    assert describer.kwargs["question"] == "¿qué hago con este jefe?"
    # V1.11: the step always sends a time-ordered list of frames.
    assert describer.kwargs["jpeg"] == [b"\xff\xd8fakejpeg"]


def test_ptt_decision_with_zero_delay_never_sleeps():
    step, calls, _ = _build(_look(delay=0.0, reasons=("ptt_switch",)))

    result = step.run("mira esto", "ptt")

    assert [c[0] for c in calls] == ["gate", "bridge", "capture", "describe"]
    assert result.outcome == "ok"


def test_ptt_with_real_explicit_gate_captures_instantly():
    calls = []
    gate = ExplicitTriggerGate(look_on_ptt=True, capture_delay_s=5.0)
    step = VisionTurnStep(
        gate, FakeCapture(calls), FakeDescriber(calls),
        speak_bridge=lambda line: calls.append(("bridge", line)),
        settings=_Settings(), clock=lambda: 0.0,
        sleep=lambda s: calls.append(("sleep", s)),
    )

    step.run("mira esto", "ptt")

    assert ("sleep", 5.0) not in calls
    assert [c[0] for c in calls] == ["bridge", "capture", "describe"]


def test_describe_uses_timeout_from_settings_and_passes_cancel():
    cancel = threading.Event()
    step, _, describer = _build(settings=_Settings(describe_timeout_s=4.5))

    step.run("look at this", "direct", cancel=cancel)

    assert describer.kwargs["timeout_s"] == 4.5
    assert describer.kwargs["cancel"] is cancel


def test_settings_update_applies_to_next_turn():
    step, _, describer = _build()
    step.update_settings(_Settings(describe_timeout_s=2.0))

    step.run("look at this", "direct")

    assert describer.kwargs["timeout_s"] == 2.0


def test_timings_are_reported():
    # clock() is read at capture start/end and describe start/end, in order.
    ticks = iter([0.0, 0.05, 0.05, 1.25])
    calls = []
    step = VisionTurnStep(
        FakeGate(_look(delay=0.0), calls), FakeCapture(calls), FakeDescriber(calls),
        speak_bridge=lambda line: None, settings=_Settings(),
        clock=lambda: next(ticks, 1.25), sleep=lambda s: None,
    )

    result = step.run("mira", "direct")

    assert result.timings["capture_ms"] == 50
    assert result.timings["describe_ms"] == 1200


# ---------------------------------------------------------------- bridge line


@pytest.mark.parametrize(
    "text,lang",
    [
        ("¿qué hago con este jefe?", "es"),
        ("mira esto", "es"),
        ("¿cuánta vida me queda?", "es"),
        ("what's this boss weak to?", "en"),
        ("look at this", "en"),
        ("how much HP do I have", "en"),
    ],
)
def test_detect_language(text, lang):
    assert detect_language(text, default="es") == lang
    assert detect_language(text, default="en") == lang


def test_detect_language_falls_back_to_default_when_unclear():
    assert detect_language("...", default="en") == "en"
    assert detect_language("", default="es") == "es"


@pytest.mark.parametrize("text,lang", [("mira esto", "es"), ("look at this", "en")])
def test_bridge_line_follows_question_language(text, lang):
    step, calls, _ = _build()

    step.run(text, "direct")

    line = next(c[1] for c in calls if c[0] == "bridge")
    assert line in BRIDGE_LINES[lang]


def test_bridge_lines_are_short_fixed_lists():
    for lang in ("es", "en"):
        assert BRIDGE_LINES[lang]
        assert all(0 < len(line) <= 40 for line in BRIDGE_LINES[lang])


def test_bridge_failure_does_not_stop_the_look():
    calls = []

    def _boom(line):
        raise RuntimeError("tts down")

    step = VisionTurnStep(
        FakeGate(_look(delay=0.0), calls), FakeCapture(calls), FakeDescriber(calls),
        speak_bridge=_boom, settings=_Settings(), clock=lambda: 0.0, sleep=lambda s: None,
    )

    result = step.run("mira", "direct")

    assert result.outcome == "ok"
    assert "bridge_failed" in result.reason_codes


# ------------------------------------------------------------------ fail-open


def test_daemon_not_ready_fails_open_before_bridge():
    step, calls, _ = _build(ready=lambda: "daemon_not_ready")

    result = step.run("mira", "direct")

    assert result.screen_block == ""
    assert result.outcome == "daemon_not_ready"
    assert [c[0] for c in calls] == ["gate"]


def test_missing_capture_fails_open_with_capture_unavailable_before_bridge():
    step, calls, _ = _build(capture=None)

    result = step.run("mira", "direct")

    assert result.screen_block == ""
    assert result.outcome == "capture_unavailable"
    assert [c[0] for c in calls] == ["gate"]


def test_capture_unavailable_error_fails_open():
    calls = []
    step, calls, _ = _build(capture=FakeCapture(calls, error=CaptureUnavailable("no mss")), calls=calls)

    result = step.run("mira", "direct")

    assert result.screen_block == ""
    assert result.outcome == "capture_unavailable"
    assert "describe" not in [c[0] for c in calls]


def test_black_frame_skips_describe():
    calls = []
    step, calls, _ = _build(capture=FakeCapture(calls, black=True), calls=calls)

    result = step.run("mira", "direct")

    assert result.screen_block == ""
    assert result.outcome == "black_frame"
    assert "black_frame" in result.reason_codes
    assert "describe" not in [c[0] for c in calls]


@pytest.mark.parametrize("reason", ["timeout", "http", "empty", "cancelled", "unavailable"])
def test_describe_error_fails_open_with_reason(reason):
    calls = []
    describer = FakeDescriber(calls, error=VisionDescribeError(reason))
    step, _, _ = _build(describer=describer, calls=calls)

    result = step.run("mira", "direct")

    assert result.screen_block == ""
    assert result.outcome == f"describe_{reason}"
    assert f"describe_{reason}" in result.reason_codes


def test_description_empty_after_sanitize_fails_open():
    calls = []
    step, _, _ = _build(describer=FakeDescriber(calls, text="   "), calls=calls)

    result = step.run("mira", "direct")

    assert result.screen_block == ""
    assert result.outcome == "describe_empty"


@pytest.mark.parametrize("where", ["gate", "capture", "describe", "ready"])
def test_unexpected_exception_never_raises(where):
    calls = []

    class Boom(Exception):
        pass

    def boom(*a, **k):
        raise Boom("x")

    gate = FakeGate(_look(), calls)
    capture = FakeCapture(calls)
    describer = FakeDescriber(calls)
    ready = None
    if where == "gate":
        gate.decide = boom
    elif where == "capture":
        capture.capture = boom
    elif where == "describe":
        describer.describe = boom
    else:
        ready = boom
    step = VisionTurnStep(
        gate, capture, describer, speak_bridge=lambda line: None,
        settings=_Settings(), ready_check=ready, clock=lambda: 0.0, sleep=lambda s: None,
    )

    result = step.run("mira", "direct")

    assert result.screen_block == ""
    assert result.outcome == "error"
    assert "error_Boom" in result.reason_codes


def test_cancel_before_capture_skips_capture():
    cancel = threading.Event()
    calls = []

    def _sleep(seconds):
        calls.append(("sleep", seconds))
        cancel.set()

    step = VisionTurnStep(
        FakeGate(_look(delay=3.0), calls), FakeCapture(calls), FakeDescriber(calls),
        speak_bridge=lambda line: calls.append(("bridge", line)),
        settings=_Settings(), clock=lambda: 0.0, sleep=_sleep,
    )

    result = step.run("mira", "direct", cancel=cancel)

    assert result.outcome == "describe_cancelled"
    assert "capture" not in [c[0] for c in calls]


# ------------------------------------------------------------ looking flag


def test_looking_flag_is_set_only_during_the_look():
    seen = []
    events = []
    calls = []
    step_box = {}

    class SpyDescriber(FakeDescriber):
        def describe(self, *a, **k):
            seen.append(step_box["step"].looking)
            return super().describe(*a, **k)

    step = VisionTurnStep(
        FakeGate(_look(delay=0.0), calls), FakeCapture(calls), SpyDescriber(calls),
        speak_bridge=lambda line: seen.append(step_box["step"].looking),
        settings=_Settings(), clock=lambda: 0.0, sleep=lambda s: None,
        on_looking=events.append,
    )
    step_box["step"] = step

    assert step.looking is False
    step.run("mira", "direct")

    assert seen == [True, True]
    assert step.looking is False
    assert events == [True, False]


def test_looking_flag_resets_after_failure():
    events = []
    calls = []
    step, _, _ = _build(
        describer=FakeDescriber(calls, error=VisionDescribeError("timeout")),
        calls=calls, on_looking=events.append,
    )

    step.run("mira", "direct")

    assert step.looking is False
    assert events == [True, False]


def test_on_looking_callback_failure_is_contained():
    def _boom(value):
        raise RuntimeError("ui gone")

    step, _, _ = _build(on_looking=_boom)

    result = step.run("mira", "direct")

    assert result.outcome == "ok"
    assert step.looking is False


# ------------------------------------------------------------------ privacy


def test_log_line_carries_codes_and_latency_never_text(caplog):
    calls = []
    describer = FakeDescriber(calls, text=f"La pantalla dice {SECRET} en grande.")
    step, _, _ = _build(describer=describer, calls=calls)

    with caplog.at_level(logging.DEBUG):
        result = step.run(f"¿qué dice {SECRET}?", "direct")

    assert SECRET in result.screen_block  # it does reach the prompt
    vision_lines = [r.getMessage() for r in caplog.records if "[VISION]" in r.getMessage()]
    assert len(vision_lines) == 1
    line = vision_lines[0]
    for field in ("decision=look", "reasons=explicit_look", "capture_ms=", "describe_ms=", "outcome=ok"):
        assert field in line
    assert all(SECRET not in r.getMessage() for r in caplog.records)


def test_failure_log_never_carries_text(caplog):
    calls = []
    describer = FakeDescriber(calls, error=VisionDescribeError("http", f"detail {SECRET}"))
    step, _, _ = _build(describer=describer, calls=calls)

    with caplog.at_level(logging.DEBUG):
        step.run(f"mira {SECRET}", "direct")

    assert all(SECRET not in r.getMessage() for r in caplog.records)


def test_no_vision_logs_one_skip_line(caplog):
    step, _, _ = _build(VisionDecision(False, ("no_trigger",), 0.0))

    with caplog.at_level(logging.DEBUG):
        step.run(f"hola {SECRET}", "direct")

    lines = [r.getMessage() for r in caplog.records if "[VISION]" in r.getMessage()]
    assert len(lines) == 1
    assert "decision=skip" in lines[0] and "reasons=no_trigger" in lines[0]
    assert SECRET not in lines[0]


def test_vision_turn_writes_no_file(monkeypatch):
    opened = []
    real_open = builtins.open

    def spy_open(file, mode="r", *args, **kwargs):
        if any(flag in mode for flag in ("w", "a", "x", "+")):
            opened.append(file)
        return real_open(file, mode, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", spy_open)
    step, _, _ = _build()

    step.run("mira", "direct")

    assert opened == []


def test_result_never_keeps_image_bytes():
    step, _, _ = _build()

    result = step.run("mira", "direct")

    assert b"fakejpeg" not in repr(result).encode()
    assert not any(isinstance(v, (bytes, bytearray)) for v in vars(result).values())


# ------------------------------------------------- V1.8: outcome + failure note

OUTCOME_CODES = {
    "ok", "black_frame", "capture_unavailable", "describe_timeout", "describe_http",
    "describe_empty", "describe_cancelled", "describe_unavailable", "daemon_not_ready",
    "model_warming", "model_missing", "error", "self_capture",
}


@pytest.mark.parametrize("code", ["daemon_not_ready", "model_warming", "model_missing"])
def test_not_ready_fails_open_before_bridge_without_a_note(code):
    step, calls, _ = _build(ready=lambda: code)

    result = step.run("mira", "direct")

    assert result.outcome == code
    assert result.note == ""
    assert "bridge" not in [c[0] for c in calls]


def test_successful_look_carries_no_note():
    step, _, _ = _build()

    result = step.run("mira", "direct")

    assert result.outcome == "ok"
    assert result.note == ""


@pytest.mark.parametrize(
    "kind, code",
    [
        ("timeout", "describe_timeout"),
        ("http", "describe_http"),
        ("unavailable", "describe_unavailable"),
        ("black", "black_frame"),
        ("capture", "capture_unavailable"),
        ("empty", "describe_empty"),
        ("boom", "error"),
    ],
)
def test_failed_look_after_the_bridge_carries_a_note_with_the_code(kind, code):
    calls = []
    capture = FakeCapture(calls)
    describer = FakeDescriber(calls)
    if kind in ("timeout", "http", "unavailable"):
        describer = FakeDescriber(calls, error=VisionDescribeError(kind))
    elif kind == "black":
        capture = FakeCapture(calls, black=True)
    elif kind == "capture":
        capture = FakeCapture(calls, error=CaptureUnavailable("no mss"))
    elif kind == "empty":
        describer = FakeDescriber(calls, text="   ")
    else:
        def _boom(*a, **k):
            raise RuntimeError("x")
        describer.describe = _boom
    step, calls, _ = _build(capture=capture, describer=describer, calls=calls)

    result = step.run("mira", "direct")

    assert result.outcome == code
    assert result.screen_block == ""
    assert f"(reason: {code})" in result.note
    assert "could not" in result.note
    assert "do not claim they showed you nothing" in result.note
    assert ("bridge", BRIDGE_LINES["es"][0]) in calls


def test_note_never_carries_the_question_or_the_description():
    calls = []
    describer = FakeDescriber(calls, error=VisionDescribeError("timeout"))
    step, _, _ = _build(describer=describer, calls=calls)

    result = step.run(f"mira {SECRET}", "direct")

    assert SECRET not in result.note


def test_on_result_reports_every_look_outcome_with_timings():
    seen = []
    step, _, _ = _build(on_result=seen.append)

    step.run("mira", "direct")

    assert len(seen) == 1
    assert seen[0].outcome == "ok"
    assert set(seen[0].timings) == {"capture_ms", "describe_ms"}


def test_on_result_reports_not_ready_looks():
    seen = []
    step, _, _ = _build(ready=lambda: "model_warming", on_result=seen.append)

    step.run("mira", "direct")

    assert [r.outcome for r in seen] == ["model_warming"]


def test_on_result_is_silent_when_the_gate_does_not_look():
    seen = []
    step, _, _ = _build(VisionDecision(False, ("no_trigger",), 0.0), on_result=seen.append)

    step.run("hola", "direct")

    assert seen == []


def test_on_result_failure_is_contained():
    def _boom(_result):
        raise RuntimeError("x")

    step, _, _ = _build(on_result=_boom)

    assert step.run("mira", "direct").outcome == "ok"


@pytest.mark.parametrize(
    "build",
    [
        lambda c: dict(ready=lambda: "daemon_not_ready"),
        lambda c: dict(capture=None),
        lambda c: dict(capture=FakeCapture(c, black=True)),
        lambda c: dict(capture=FakeCapture(c, error=CaptureUnavailable("x"))),
        lambda c: dict(describer=FakeDescriber(c, text="  ")),
        lambda c: dict(describer=FakeDescriber(c, error=VisionDescribeError("timeout"))),
        lambda c: dict(describer=FakeDescriber(c, error=VisionDescribeError("cancelled"))),
        lambda c: dict(),
    ],
)
def test_every_look_outcome_is_a_published_code(build):
    calls = []
    seen = []
    step, _, _ = _build(calls=calls, on_result=seen.append, **build(calls))

    step.run("mira", "direct")

    assert seen and seen[0].outcome in OUTCOME_CODES


# ------------------------------------------------------- V1.11 burst frames

_SIG_N = 64 * 36


@dataclass
class _BurstSettings:
    describe_timeout_s: float = 8.0
    frames: int = 3
    frame_interval_s: float = 1.0
    show_thumbnails: bool = True


def _sig_frame(tag, level=None, *, black=False):
    return CapturedFrame(
        jpeg=b"\xff\xd8" + tag.encode(), width=1280, height=720, source_width=1920,
        source_height=1080, is_black=black, grab_ms=1.0, encode_ms=1.0,
        signature=(bytes([level]) * _SIG_N) if level is not None else b"",
        thumbnail=b"thumb-" + tag.encode(),
    )


class SeqCapture:
    def __init__(self, calls, frames):
        self.calls = calls
        self.frames = list(frames)

    def capture(self, **kwargs):  # V1.27: text mode passes max_side/jpeg_quality
        self.calls.append(("capture",))
        item = self.frames.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class SeqProbe:
    def __init__(self, calls, answers):
        self.calls = calls
        self.answers = list(answers)

    def is_self_foreground(self):
        self.calls.append(("probe",))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def _burst(frames, *, probe=None, settings=None, delay=0.0, describer_error=None,
           ids=None, on_result=None):
    calls = []
    describer = FakeDescriber(calls, error=describer_error)
    stored = []
    step = VisionTurnStep(
        FakeGate(_look(delay=delay), calls),
        SeqCapture(calls, frames),
        describer,
        speak_bridge=lambda line: calls.append(("bridge", line)),
        settings=settings or _BurstSettings(),
        clock=lambda: 0.0,
        sleep=lambda s: calls.append(("sleep", s)),
        choice=lambda seq: seq[0],
        foreground_probe=probe,
        on_thumbnails=lambda look_id, t: stored.append((look_id, list(t))),
        new_id=(lambda: ids.pop(0)) if ids else (lambda: "look-1"),
        on_result=on_result,
    )
    return step, calls, describer, stored


def test_burst_captures_n_frames_spaced_by_the_interval_after_the_bridge():
    step, calls, describer, _ = _burst([_sig_frame("a", 10), _sig_frame("b", 90), _sig_frame("c", 200)])

    result = step.run("mira", "direct")

    assert [c for c in calls if c[0] != "gate"] == [
        ("bridge", BRIDGE_LINES["es"][0]),
        ("capture",), ("sleep", 1.0), ("capture",), ("sleep", 1.0), ("capture",),
        ("describe",),
    ]
    assert result.outcome == "ok"
    assert describer.kwargs["jpeg"] == [b"\xff\xd8a", b"\xff\xd8b", b"\xff\xd8c"]
    assert (result.frames_captured, result.frames_sent, result.self_capture_skipped) == (3, 3, 0)


def test_optional_capture_delay_precedes_the_first_frame_only():
    step, calls, _, _ = _burst([_sig_frame("a", 10), _sig_frame("b", 90)],
                               settings=_BurstSettings(frames=2, frame_interval_s=0.5), delay=2.0)

    step.run("mira", "direct")

    assert [c for c in calls if c[0] in ("sleep", "capture")] == [
        ("sleep", 2.0), ("capture",), ("sleep", 0.5), ("capture",),
    ]


def test_identical_frames_collapse_and_the_newest_is_sent():
    step, _, describer, _ = _burst([_sig_frame("a", 40), _sig_frame("b", 40), _sig_frame("c", 40)])

    result = step.run("mira", "direct")

    assert describer.kwargs["jpeg"] == [b"\xff\xd8c"]
    assert (result.frames_captured, result.frames_sent) == (3, 1)


def test_black_frames_are_dropped_and_the_rest_are_sent():
    step, _, describer, _ = _burst(
        [_sig_frame("a", 0, black=True), _sig_frame("b", 90), _sig_frame("c", 200)]
    )

    result = step.run("mira", "direct")

    assert result.outcome == "ok"
    assert describer.kwargs["jpeg"] == [b"\xff\xd8b", b"\xff\xd8c"]
    assert (result.frames_captured, result.frames_sent) == (3, 2)


def test_all_black_burst_is_black_frame_and_never_describes():
    step, calls, _, stored = _burst([_sig_frame(t, 0, black=True) for t in "abc"])

    result = step.run("mira", "direct")

    assert result.outcome == "black_frame"
    assert ("describe",) not in calls
    assert "black_frame" in result.note
    assert (result.frames_captured, result.frames_sent) == (3, 0)
    assert stored == []


def test_frames_with_the_app_in_front_are_skipped():
    probe_calls = []
    step, _, describer, _ = _burst([_sig_frame("b", 90), _sig_frame("c", 200)],
                                   probe=SeqProbe(probe_calls, [True, False, False]))

    result = step.run("mira", "direct")

    assert result.outcome == "ok"
    assert describer.kwargs["jpeg"] == [b"\xff\xd8b", b"\xff\xd8c"]
    assert (result.frames_captured, result.frames_sent, result.self_capture_skipped) == (2, 2, 1)
    assert len(probe_calls) == 3


def test_every_frame_self_is_self_capture_with_a_failure_note():
    step, calls, _, stored = _burst([], probe=SeqProbe([], [True, True, True]))

    result = step.run(f"¿qué ves {SECRET}?", "direct")

    assert result.outcome == "self_capture"
    assert result.screen_block == ""
    assert "self_capture" in result.note and SECRET not in result.note
    assert ("capture",) not in calls and ("describe",) not in calls
    assert (result.frames_captured, result.frames_sent, result.self_capture_skipped) == (0, 0, 3)
    assert stored == []


def test_probe_error_never_skips_a_frame():
    step, _, _, _ = _burst([_sig_frame("a", 10)], settings=_BurstSettings(frames=1),
                           probe=SeqProbe([], [RuntimeError("x")]))

    result = step.run("mira", "direct")

    assert result.outcome == "ok"
    assert result.self_capture_skipped == 0


def test_capture_error_mid_burst_keeps_the_frames_already_taken():
    step, _, describer, _ = _burst([_sig_frame("a", 10), CaptureUnavailable("x"), _sig_frame("c", 200)])

    result = step.run("mira", "direct")

    assert result.outcome == "ok"
    assert describer.kwargs["jpeg"] == [b"\xff\xd8a"]
    assert result.frames_captured == 1


def test_cancel_between_frames_stops_the_look():
    cancel = threading.Event()
    calls = []

    class CancellingCapture(SeqCapture):
        def capture(self, **kwargs):  # V1.27: text mode passes max_side/jpeg_quality
            cancel.set()
            return super().capture()

    step = VisionTurnStep(
        FakeGate(_look(delay=0.0), calls),
        CancellingCapture(calls, [_sig_frame("a", 10), _sig_frame("b", 90)]),
        FakeDescriber(calls),
        speak_bridge=lambda line: None,
        settings=_BurstSettings(frames=2),
        clock=lambda: 0.0,
        sleep=lambda s: None,
    )

    result = step.run("mira", "direct", cancel=cancel)

    assert result.outcome == "describe_cancelled"
    assert ("describe",) not in calls


def test_frames_setting_is_clamped_to_three():
    frames = [_sig_frame(t, i * 40) for i, t in enumerate("abcde")]
    step, calls, _, _ = _burst(frames, settings=_BurstSettings(frames=9))

    step.run("mira", "direct")

    assert calls.count(("capture",)) == 3


def test_settings_without_burst_fields_take_one_frame():
    step, calls, _ = _build()

    result = step.run("mira", "direct")

    assert calls.count(("capture",)) == 1
    assert (result.frames_captured, result.frames_sent) == (1, 1)


def test_the_sent_frames_are_stored_under_the_look_id():
    step, _, _, stored = _burst([_sig_frame("a", 40), _sig_frame("b", 40), _sig_frame("c", 200)],
                                ids=["abc123"])

    result = step.run("mira", "direct")

    assert result.look_id == "abc123"
    assert stored == [("abc123", [b"\xff\xd8a", b"\xff\xd8c"])]  # b duplicates the kept a


def test_thumbnails_are_not_stored_when_disabled():
    step, _, _, stored = _burst([_sig_frame("a", 10)],
                                settings=_BurstSettings(frames=1, show_thumbnails=False))

    step.run("mira", "direct")

    assert stored == []


def test_thumbnails_are_kept_even_when_the_describe_fails():
    step, _, _, stored = _burst([_sig_frame("a", 10)], settings=_BurstSettings(frames=1),
                                describer_error=VisionDescribeError("timeout"))

    result = step.run("mira", "direct")

    assert result.outcome == "describe_timeout"
    assert stored == [("look-1", [b"\xff\xd8a"])]


def test_every_published_look_has_an_id():
    seen = []
    step, _, _, _ = _burst([_sig_frame("a", 10)], settings=_BurstSettings(frames=1),
                           ids=["x1"], on_result=seen.append)

    step.run("mira", "direct")

    assert seen[0].look_id == "x1"


def test_default_look_id_is_a_fresh_hex_token():
    step, _, _ = _build()
    first = step.run("mira", "direct").look_id
    second = step.run("mira", "direct").look_id
    assert first and second and first != second
    assert all(c in "0123456789abcdef" for c in first)


def test_burst_log_line_carries_frame_counts_only(caplog):
    step, _, _, _ = _burst([_sig_frame("a", 40), _sig_frame("b", 40)],
                           probe=SeqProbe([], [False, False, True]))

    with caplog.at_level(logging.INFO):
        step.run(f"mira {SECRET}", "direct")

    lines = [r.getMessage() for r in caplog.records if "[VISION] decision=" in r.getMessage()]
    assert len(lines) == 1
    assert "frames=2/1" in lines[0] and "self_skipped=1" in lines[0]
    assert SECRET not in lines[0] and "thumb" not in lines[0]


def test_burst_result_repr_never_carries_image_bytes():
    step, _, _, _ = _burst([_sig_frame("a", 10)], settings=_BurstSettings(frames=1))
    assert b"thumb-a" not in repr(step.run("mira", "direct")).encode()


# ---------------------------------- V1.13: self kind, describing phase, status

from opencohost.core.vision.turn_step import STATUS_LINES  # noqa: E402


class KindProbe:
    """A V1.13 probe: reports the coarse foreground kind per frame."""

    def __init__(self, kinds):
        self.kinds = list(kinds)

    def foreground_kind(self):
        return self.kinds.pop(0)

    def is_self_foreground(self):  # pragma: no cover - the step prefers the kind
        raise AssertionError("kind-aware probes are asked for the kind")


def _decision_lines(caplog):
    return [r.getMessage() for r in caplog.records if "[VISION] decision=" in r.getMessage()]


def test_overlay_in_front_is_captured_and_logged_as_overlay(caplog):
    step, _, describer, _ = _burst([_sig_frame("a", 10), _sig_frame("b", 90), _sig_frame("c", 200)],
                                   probe=KindProbe(["overlay", "overlay", "none"]))

    with caplog.at_level(logging.INFO):
        result = step.run("mira", "direct")

    assert result.outcome == "ok"
    assert result.self_capture_skipped == 0
    assert len(describer.kwargs["jpeg"]) == 3
    assert "self_kind=overlay" in _decision_lines(caplog)[0]


def test_main_window_frames_are_skipped_and_logged_as_main(caplog):
    step, _, _, _ = _burst([_sig_frame("b", 90)], probe=KindProbe(["main", "overlay", "main"]))

    with caplog.at_level(logging.INFO):
        result = step.run("mira", "direct")

    assert result.self_capture_skipped == 2
    assert result.frames_captured == 1
    assert "self_kind=main" in _decision_lines(caplog)[0]


def test_no_app_window_logs_self_kind_none(caplog):
    step, _, _, _ = _burst([_sig_frame("a", 10)], settings=_BurstSettings(frames=1))

    with caplog.at_level(logging.INFO):
        step.run("mira", "direct")

    assert "self_kind=none" in _decision_lines(caplog)[0]


def test_legacy_boolean_probe_maps_to_main(caplog):
    step, _, _, _ = _burst([_sig_frame("b", 90)], settings=_BurstSettings(frames=2),
                           probe=SeqProbe([], [True, False]))

    with caplog.at_level(logging.INFO):
        result = step.run("mira", "direct")

    assert result.self_capture_skipped == 1
    assert "self_kind=main" in _decision_lines(caplog)[0]


def test_skip_decision_logs_self_kind_none(caplog):
    step, _, _ = _build(VisionDecision(False, ("no_trigger",), 0.0))

    with caplog.at_level(logging.INFO):
        step.run("hola", "direct")

    assert "self_kind=none" in _decision_lines(caplog)[0]


def _with_describing(frames, **kw):
    step, calls, describer, stored = _burst(frames, **kw)
    step._on_describing = lambda: calls.append(("describing",))
    return step, calls, describer


def test_describing_phase_fires_after_capture_and_before_describe():
    step, calls, _ = _with_describing([_sig_frame("a", 10), _sig_frame("b", 90)],
                                      settings=_BurstSettings(frames=2))

    step.run("mira", "direct")

    kinds = [c[0] for c in calls]
    assert kinds.count("describing") == 1
    assert kinds.index("describing") == kinds.index("describe") - 1
    assert kinds.index("describing") > max(i for i, k in enumerate(kinds) if k == "capture")


@pytest.mark.parametrize(
    "frames,probe",
    [
        ([], KindProbe(["main", "main", "main"])),  # self_capture
        ([_sig_frame(t, 0, black=True) for t in "abc"], None),  # black_frame
        ([CaptureUnavailable("x")], None),  # capture_unavailable
    ],
)
def test_describing_phase_never_fires_without_a_describe(frames, probe):
    step, calls, _ = _with_describing(frames, probe=probe)

    step.run("mira", "direct")

    assert ("describing",) not in calls


def test_describing_constructor_callback_and_failure_is_contained():
    def _boom():
        raise RuntimeError("ui gone")

    calls = []
    step = VisionTurnStep(
        FakeGate(_look(delay=0.0), calls), FakeCapture(calls), FakeDescriber(calls),
        speak_bridge=lambda line: None, settings=_Settings(), clock=lambda: 0.0,
        sleep=lambda s: None, on_describing=_boom,
    )

    assert step.run("mira", "direct").outcome == "ok"


@pytest.mark.parametrize("text,lang", [("¿qué ves en esto?", "es"), ("what do you see on this?", "en")])
def test_successful_look_carries_a_status_line_in_the_bridge_language(text, lang):
    step, _, _ = _build(_look(delay=0.0))

    result = step.run(text, "direct")

    assert result.outcome == "ok"
    assert result.status_line in STATUS_LINES[lang]


def test_status_lines_are_short_fixed_constants():
    assert STATUS_LINES["es"] == ("Ya vi, dame un segundo.",)
    assert STATUS_LINES["en"] == ("Got it, one sec.",)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"describer_error": VisionDescribeError("timeout")},
        {"frames": [_sig_frame(t, 0, black=True) for t in "abc"]},
    ],
)
def test_failed_look_carries_no_status_line(kwargs):
    frames = kwargs.pop("frames", [_sig_frame("a", 10)])
    step, _, _, _ = _burst(frames, settings=_BurstSettings(frames=len(frames)), **kwargs)

    result = step.run("mira", "direct")

    assert result.outcome != "ok"
    assert result.status_line == ""


def test_no_status_line_when_the_bridge_was_never_spoken():
    calls = []

    def _bridge_fails(line):
        raise RuntimeError("router gone")

    step = VisionTurnStep(
        FakeGate(_look(delay=0.0), calls), FakeCapture(calls), FakeDescriber(calls),
        speak_bridge=_bridge_fails, settings=_Settings(), clock=lambda: 0.0,
        sleep=lambda s: None,
    )

    result = step.run("mira", "direct")

    assert result.outcome == "ok"
    assert result.status_line == ""


def test_no_vision_carries_no_status_line():
    step, _, _ = _build(VisionDecision(False, ("no_trigger",), 0.0))
    assert step.run("hola", "direct").status_line == ""


# ---------------------------------------------- V1.15: canned failure lines

from opencohost.core.vision.turn_step import (  # noqa: E402
    VISION_FAILURE_SHORT_HINT,
    failure_line,
)


@pytest.mark.parametrize(
    "outcome,lang,expected",
    [
        ("describe_timeout", "es",
         "No alcancé a ver tu pantalla, la GPU va muy cargada. Inténtalo de nuevo."),
        ("describe_timeout", "en", "I couldn't see your screen in time, the GPU is busy. Try again."),
        ("self_capture", "es", "Solo veo OpenCohost en pantalla. Pon delante lo que quieres que vea."),
        ("self_capture", "en", "All I can see is OpenCohost. Bring what you want me to see to the front."),
        ("describe_unavailable", "es", "No pude ver tu pantalla esta vez. Inténtalo de nuevo."),
        ("black_frame", "en", "I couldn't see your screen this time. Try again."),
    ],
)
def test_failure_line_by_outcome_and_language(outcome, lang, expected):
    assert failure_line(outcome, lang) == expected


def test_failure_line_unknown_language_falls_back_to_spanish():
    assert failure_line("describe_timeout", "fr") == failure_line("describe_timeout", "es")


@pytest.mark.parametrize("outcome", ["ok", "no_vision", "describe_cancelled", "error", ""])
def test_no_failure_line_for_success_skip_cancel_or_error(outcome):
    # cancelled = the owner stopped the turn; error = fail open to generation.
    assert failure_line(outcome, "es") == ""


def test_short_hint_is_an_instruction_without_user_text():
    assert "short" in VISION_FAILURE_SHORT_HINT.lower()
    assert "{" not in VISION_FAILURE_SHORT_HINT


# ------------------------------------------------ V1.23: bounded wait for warming

from opencohost.core.vision.turn_step import WARMING_WAIT_S  # noqa: E402


def _warm_step(ready, *, calls=None, wait=None):
    calls = [] if calls is None else calls
    kwargs = {} if wait is None else {"warming_wait_s": wait}
    step = VisionTurnStep(
        FakeGate(_look(delay=0.0), calls), FakeCapture(calls), FakeDescriber(calls),
        speak_bridge=lambda line: calls.append(("bridge", line)),
        settings=_Settings(), ready_check=ready, clock=lambda: 0.0,
        sleep=lambda s: calls.append(("sleep", s)), choice=lambda seq: seq[0], **kwargs,
    )
    return step, calls


def _script(*answers):
    items = list(answers)
    return lambda: items.pop(0) if len(items) > 1 else items[0]


def test_warming_wait_default_is_ten_seconds():
    assert WARMING_WAIT_S == 10.0


def test_look_waits_for_ready_then_proceeds():
    step, calls = _warm_step(_script("model_warming", "model_warming", None))

    result = step.run("mira esto", "direct")

    assert result.outcome == "ok"
    assert [c[0] for c in calls] == ["gate", "sleep", "sleep", "bridge", "capture", "describe"]
    assert "warming_waited" in result.reason_codes


def test_still_warming_after_the_budget_keeps_model_warming_without_bridge():
    step, calls = _warm_step(lambda: "model_warming")

    result = step.run("mira esto", "direct")

    assert result.outcome == "model_warming"
    assert result.note == ""
    assert "bridge" not in [c[0] for c in calls]
    assert "model_warming" in result.reason_codes
    slept = sum(c[1] for c in calls if c[0] == "sleep")
    assert 0 < slept <= WARMING_WAIT_S + 1e-9


def test_wait_budget_is_configurable_and_zero_means_no_wait():
    step, calls = _warm_step(lambda: "model_warming", wait=0.0)

    assert step.run("mira esto", "direct").outcome == "model_warming"
    assert [c[0] for c in calls] == ["gate"]


def test_wait_ends_early_when_another_reason_replaces_warming():
    step, calls = _warm_step(_script("model_warming", "model_missing"))

    assert step.run("mira esto", "direct").outcome == "model_missing"


def test_cancelled_turn_does_not_wait_for_warming():
    step, calls = _warm_step(lambda: "model_warming")
    cancel = threading.Event()
    cancel.set()

    result = step.run("mira esto", "direct", cancel=cancel)

    assert result.outcome == "model_warming"
    assert "sleep" not in [c[0] for c in calls]


def test_not_warming_reasons_never_wait():
    step, calls = _warm_step(lambda: "daemon_not_ready")

    assert step.run("mira", "direct").outcome == "daemon_not_ready"
    assert "sleep" not in [c[0] for c in calls]


def test_real_clock_budget_also_ends_the_wait():
    ticks = iter([0.0, 3.0, 11.0, 11.0, 11.0, 11.0])
    calls = []
    step = VisionTurnStep(
        FakeGate(_look(delay=0.0), calls), FakeCapture(calls), FakeDescriber(calls),
        speak_bridge=lambda line: None, settings=_Settings(),
        ready_check=lambda: "model_warming",
        clock=lambda: next(ticks, 11.0), sleep=lambda s: calls.append(("sleep", s)),
    )

    assert step.run("mira esto", "direct").outcome == "model_warming"
    assert len([c for c in calls if c[0] == "sleep"]) <= 2


@pytest.mark.parametrize("lang,expected", [
    ("es", "Todavía me estoy despertando la vista, pídemelo otra vez en unos segundos."),
    ("en", "My eyes are still waking up, ask me again in a few seconds."),
])
def test_warming_has_its_own_canned_line(lang, expected):
    from opencohost.core.vision.turn_step import failure_line

    assert failure_line("model_warming", lang) == expected


# --------------------------------------------- V1.25: self-look on request


def _self_burst(text, *, reasons=("explicit_look",), kinds=("main", "main", "main")):
    calls = []
    frames = [_sig_frame("a", 10), _sig_frame("b", 90), _sig_frame("c", 200)]
    step = VisionTurnStep(
        FakeGate(VisionDecision(True, tuple(reasons), 0.0), calls),
        SeqCapture(calls, frames), FakeDescriber(calls),
        speak_bridge=lambda line: calls.append(("bridge", line)),
        settings=_BurstSettings(), clock=lambda: 0.0, sleep=lambda s: None,
        choice=lambda seq: seq[0], foreground_probe=KindProbe(kinds),
    )
    return step.run(text, "direct")


@pytest.mark.parametrize("text", [
    "mira OpenCohost", "look at opencohost", "mira la app", "mira la aplicación",
    "mira la interfaz", "look at the app", "check the interface",
])
def test_self_look_requested_keeps_main_window_frames(text, caplog):
    with caplog.at_level(logging.INFO):
        result = _self_burst(text)

    assert result.outcome == "ok"
    assert result.self_capture_skipped == 0
    assert result.frames_captured == 3
    assert "self_look_requested" in result.reason_codes
    line = _decision_lines(caplog)[-1]
    assert "self_look_requested" in line and text not in line


@pytest.mark.parametrize("text", ["", "mira esto", "¿qué ves en pantalla?", "look at this"])
def test_without_app_wording_main_window_is_still_self_capture(text):
    result = _self_burst(text)

    assert result.outcome == "self_capture"
    assert result.self_capture_skipped == 3
    assert "self_look_requested" not in result.reason_codes


def test_plain_look_button_with_no_text_keeps_skipping_self():
    result = _self_burst("", reasons=("explicit_look",))

    assert result.outcome == "self_capture"


def test_self_look_reason_only_when_a_main_frame_was_actually_kept():
    result = _self_burst("mira la app", kinds=("overlay", "none", "none"))

    assert result.outcome == "ok"
    assert "self_look_requested" not in result.reason_codes


def test_self_look_applies_to_look_turns_from_the_rule_gate():
    result = _self_burst("mira la app", reasons=("look_verb",))

    assert result.outcome == "ok"
    assert "self_look_requested" in result.reason_codes
