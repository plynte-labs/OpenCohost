"""Drive real producer/consumer playback, not hand-written state changes."""
import threading
from pathlib import Path

import pytest

from tests.test_speech_outcome_capture import _make_motor, _ScriptedMixerMusic, _sentences


@pytest.mark.parametrize("interrupt", [False, True])
def test_snapshot_is_started_chunk_not_future_and_cleanup_survives(interrupt):
    generated = threading.Event()
    paths = []

    def synth(text, path):
        paths.append(path)
        Path(path).write_bytes(bytes([len(paths)]))
        if len(paths) == 3:
            generated.set()
        return True

    mixer = _ScriptedMixerMusic(block_at=0)
    motor, _, _ = _make_motor(mixer_music=mixer, synthesize=synth)
    worker = threading.Thread(target=motor._hablar_impl, args=(" ".join(_sentences(3)),))
    worker.start()
    try:
        assert mixer.entered_block.wait(5)
        assert generated.wait(5)
        snapshot = motor._vrm_audio_snapshot
        assert snapshot.data == b"\x01"
        assert snapshot.sequence == 1 and snapshot.active
        assert snapshot.content_type == "audio/wav"
    finally:
        if interrupt:
            motor.interrupt_speaking()
        mixer.release()
        worker.join(5)
    assert not worker.is_alive()
    assert not motor._vrm_audio_snapshot.active
    assert motor._vrm_audio_snapshot.sequence == (1 if interrupt else 3)
    assert all(not Path(path).exists() for path in paths)


def test_unreadable_snapshot_never_prevents_speech(monkeypatch):
    from opencohost.core.speech import vrm_audio
    monkeypatch.setattr(vrm_audio, "MAX_AUDIO_BYTES", 1)
    motor, _, _ = _make_motor()
    outcome = motor._hablar_impl(" ".join(_sentences(2)))
    assert outcome.spoken == [0, 1]
    assert not motor._vrm_audio_snapshot.active


def test_playback_failure_does_not_leave_active_snapshot():
    class FailingMixer(_ScriptedMixerMusic):
        def get_busy(self):
            raise RuntimeError("device failure")

    motor, _, _ = _make_motor(mixer_music=FailingMixer())
    outcome = motor._hablar_impl(" ".join(_sentences(1)))
    assert outcome.skipped == [0]
    assert not motor._vrm_audio_snapshot.active
    assert motor._vrm_audio_snapshot.data == b""
