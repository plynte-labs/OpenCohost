"""Tests for the PTT listening cue (ptt_cue_20260717).

A short, low-volume blip plays the instant a PTT hold starts listening so the
operator knows the mic is live while gaming with the app window unfocused (the
frontend pauses its polls in the background, so the cue must come from the
engine side, not the UI).

The cue plays through ``winsound`` (a separate Windows audio path with ZERO
pygame/SDL interaction) so it is safe to fire from the HTTP handler thread even
while the engine thread quits+re-inits its pygame mixer after every PTT close.
STRICT TDD, RED first. winsound is ALWAYS mocked — no real audio device is
touched. The load-bearing behaviours under test: the cue fires async from a
cached temp FILE when enabled, no-ops when disabled or off-Windows, never
breaks PTT start on error, and bakes PTT_CUE_VOLUME into a once-built WAV.

Regression (2026-08-09): the cue shipped as ``SND_MEMORY | SND_ASYNC``, which
CPython's winsound hard-rejects ("Cannot play asynchronously from memory")
before it ever reaches an audio call — the cue had never played once. Mocked
winsound is exactly why it shipped, so the flag combination is now pinned.
"""
from __future__ import annotations

import io
import os
import queue
import struct
import wave
from unittest.mock import MagicMock

import pytest

from opencohost.api.ptt_session import PttController, PttUnreachable

try:
    import winsound
except ImportError:  # non-Windows: the cue no-ops, nothing to assert against
    winsound = None

requires_winsound = pytest.mark.skipif(
    winsound is None, reason="PTT cue uses the Windows winsound stdlib"
)


# ──────────────────────────────────────────────────────────────────────────
# Controller wiring — on_listening fires when (and only when) listening begins
# ──────────────────────────────────────────────────────────────────────────


class _FakeSession:
    """Minimal PttSession stand-in whose start() succeeds into listening."""

    def __init__(self, ws_uri, on_flush, on_event, on_close, **kwargs):
        self.session_id = "ptt_fake"
        self.state = "listening"

    def start(self):
        pass  # connect ok -> listening


class _UnreachableSession(_FakeSession):
    def start(self):
        raise PttUnreachable()


def test_controller_fires_listening_hook_on_successful_start():
    calls = []
    controller = PttController(
        "ws://test/whisperlive",
        MagicMock(),
        MagicMock(),
        session_factory=_FakeSession,
        on_listening=lambda: calls.append(1),
    )
    sid = controller.start()
    assert sid == "ptt_fake"
    assert calls == [1]  # fired exactly once, on listening


def test_controller_does_not_fire_listening_hook_when_unreachable():
    calls = []
    controller = PttController(
        "ws://test/whisperlive",
        MagicMock(),
        MagicMock(),
        session_factory=_UnreachableSession,
        on_listening=lambda: calls.append(1),
    )
    with pytest.raises(PttUnreachable):
        controller.start()
    assert calls == []  # no listening -> no cue


def test_listening_hook_error_never_breaks_start():
    def boom():
        raise RuntimeError("cue exploded")

    controller = PttController(
        "ws://test/whisperlive",
        MagicMock(),
        MagicMock(),
        session_factory=_FakeSession,
        on_listening=boom,
    )
    # Fail-open: a hook error must never fail the start — the operator is
    # already live and the cue is only a nicety.
    assert controller.start() == "ptt_fake"


# ──────────────────────────────────────────────────────────────────────────
# Engine cue — play_ptt_cue() via winsound: gating, fail-open, cached WAV
# ──────────────────────────────────────────────────────────────────────────


def _make_cue_motor():
    """MotorVocalIA with no pygame needed — the cue is a pure winsound path."""
    from opencohost.core.llm_engine import MotorVocalIA

    return MotorVocalIA(queue.Queue(), lambda event: None)


def _patch_winsound(monkeypatch):
    """Swap llm.winsound for a spy that keeps the real SND_* flag ints, so the
    flag assertions are meaningful without touching a real audio device."""
    import opencohost.core.llm_engine as llm

    spy = MagicMock()
    spy.SND_MEMORY = winsound.SND_MEMORY
    spy.SND_ASYNC = winsound.SND_ASYNC
    spy.SND_FILENAME = winsound.SND_FILENAME
    spy.SND_NODEFAULT = winsound.SND_NODEFAULT
    monkeypatch.setattr(llm, "winsound", spy)
    return spy


@requires_winsound
def test_play_ptt_cue_plays_via_winsound_when_enabled(monkeypatch):
    spy = _patch_winsound(monkeypatch)
    motor = _make_cue_motor()
    motor.play_ptt_cue()

    spy.PlaySound.assert_called_once()
    path, flags = spy.PlaySound.call_args.args
    # async + from a real file on disk: fire-and-forget from any thread, no
    # SDL involvement — and the only async form winsound actually accepts.
    assert isinstance(path, str)
    assert os.path.exists(path)
    assert flags & winsound.SND_FILENAME
    assert flags & winsound.SND_ASYNC


@requires_winsound
def test_play_ptt_cue_never_asks_for_async_from_memory(monkeypatch):
    # Regression pin: winsound raises RuntimeError("Cannot play asynchronously
    # from memory") for SND_MEMORY|SND_ASYNC, so this combination means the cue
    # is silently dead in production while every mocked test still passes.
    spy = _patch_winsound(monkeypatch)
    motor = _make_cue_motor()
    motor.play_ptt_cue()

    _, flags = spy.PlaySound.call_args.args
    assert not (flags & winsound.SND_MEMORY and flags & winsound.SND_ASYNC)


@requires_winsound
def test_play_ptt_cue_skipped_when_disabled(monkeypatch):
    import opencohost.core.llm_engine as llm

    monkeypatch.setattr(llm, "PTT_CUE_ENABLED", False)
    spy = _patch_winsound(monkeypatch)
    motor = _make_cue_motor()
    motor.play_ptt_cue()

    spy.PlaySound.assert_not_called()


@requires_winsound
def test_play_ptt_cue_failopen_on_playback_error(monkeypatch):
    spy = _patch_winsound(monkeypatch)
    spy.PlaySound.side_effect = RuntimeError("audio endpoint gone")
    motor = _make_cue_motor()
    motor.play_ptt_cue()  # must not raise — a cue error never breaks PTT start


def test_play_ptt_cue_noop_when_winsound_absent(monkeypatch):
    # Non-Windows host: the guarded import leaves winsound = None -> silent
    # no-op. Simulated here so the off-Windows path is proven on any runner.
    import opencohost.core.llm_engine as llm

    monkeypatch.setattr(llm, "winsound", None)
    motor = _make_cue_motor()
    motor.play_ptt_cue()  # must not raise


@requires_winsound
def test_ptt_cue_wav_generated_once_and_cached(monkeypatch):
    spy = _patch_winsound(monkeypatch)
    motor = _make_cue_motor()
    assert motor._ptt_cue_wav_bytes is None

    motor.play_ptt_cue()
    first = motor._ptt_cue_wav_bytes
    assert isinstance(first, bytes) and len(first) > 44  # RIFF header + PCM data

    motor.play_ptt_cue()
    # Built exactly once: the same cached bytes, spilled to one cached temp
    # file, are handed to winsound twice.
    assert motor._ptt_cue_wav_bytes is first
    calls = spy.PlaySound.call_args_list
    assert len(calls) == 2
    assert calls[0].args[0] == calls[1].args[0] == motor._ptt_cue_wav_path
    with open(motor._ptt_cue_wav_path, "rb") as f:
        assert f.read() == first  # the file holds exactly the cached WAV bytes


def test_ptt_cue_wav_amplitude_respects_volume():
    # winsound has no volume knob, so PTT_CUE_VOLUME must live in the samples.
    # Parse the generated WAV and prove the baked peak is low (a nicety, not
    # TTS) yet non-silent.
    import opencohost.core.llm_engine as llm

    motor = _make_cue_motor()
    data = motor._ptt_cue_wav()

    with wave.open(io.BytesIO(data), "rb") as wav:
        assert wav.getnchannels() == 1
        assert wav.getsampwidth() == 2
        assert wav.getframerate() == 44100
        raw = wav.readframes(wav.getnframes())

    samples = struct.unpack("<%dh" % (len(raw) // 2), raw)
    peak = max(abs(s) for s in samples)
    ceiling = llm.PTT_CUE_VOLUME * 32767
    assert 0 < peak <= ceiling + 1   # baked to the configured (low) volume
    assert peak < 0.5 * 32767        # unmistakably a quiet blip, not full-scale
