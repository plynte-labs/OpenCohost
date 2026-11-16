"""Edge-TTS failure -> Piper for ANY failure (TA2), breaker wiring (TA3), log contract (TA4).

Drives the real MotorVocalIA._hablar()/productor() with boundary mocks only.
"""
from __future__ import annotations

import asyncio
import logging
import socket

import aiohttp
import pytest

from opencohost.core.speech.speech_rate import EdgeBreaker
from tests.test_llm_engine_piper_fallback import (
    _drive_hablar,
    _make_mock_piper,
    _make_motor,
)

SENTINEL = "zxqv-secret-sentence"


def _handshake(status=403):
    url = aiohttp.client.URL("wss://example.invalid/x?token=SECRET_TOKEN")
    ri = aiohttp.RequestInfo(url=url, method="GET", headers={}, real_url=url)
    return aiohttp.WSServerHandshakeError(ri, (), status=status, message="Invalid response status")


def _motor(piper_available=True):
    motor, *_ = _make_motor()
    motor.tts_local_only = False
    motor._edge_tts_offline = False
    motor._piper = _make_mock_piper(available=piper_available)
    return motor


@pytest.mark.parametrize(
    "exc",
    [_handshake(403), asyncio.TimeoutError(), RuntimeError("NoAudioReceived")],
    ids=["http403", "timeout", "other"],
)
def test_transient_failure_is_spoken_by_piper_without_speech_lost(exc, caplog):
    motor = _motor()
    with caplog.at_level(logging.WARNING):
        loaded, piper, communicate = _drive_hablar(motor, "Hola mundo", edge_save_side_effect=exc)
    assert communicate.called
    assert piper.synthesize.called
    assert len(loaded) == 1 and loaded[0].endswith(".wav"), "exactly one spoken file (no double-speak)"
    assert motor._edge_tts_offline is False
    assert "SPEECH_LOST" not in caplog.text


def test_transient_failure_without_piper_drops_with_reason(caplog):
    motor = _motor(piper_available=False)
    with caplog.at_level(logging.WARNING):
        loaded, piper, _ = _drive_hablar(motor, "Hola mundo", edge_save_side_effect=_handshake(403))
    assert loaded == []
    piper.synthesize.assert_not_called()
    assert "reason=edge_fallback_piper_unavailable" in caplog.text
    assert motor._edge_tts_offline is False


def test_offline_failure_still_latches_flag():
    motor = _motor()
    _drive_hablar(motor, "Hola", edge_save_side_effect=socket.gaierror("dns"))
    assert motor._edge_tts_offline is True


def test_breaker_opens_after_three_failures_and_skips_edge():
    class Clock:
        t = 1000.0

        def __call__(self):
            return self.t

    clock = Clock()
    motor = _motor()
    motor._edge_breaker = EdgeBreaker(3, 120, 900, clock=clock)
    for _ in range(3):
        _drive_hablar(motor, "Hola", edge_save_side_effect=_handshake(403))
    assert motor._edge_breaker.degraded is True
    loaded, piper, communicate = _drive_hablar(motor, "Hola otra vez")
    assert not communicate.called, "Edge must not be attempted while the breaker is open"
    assert loaded and loaded[-1].endswith(".wav")
    # cooldown elapsed: one probe goes to Edge and success closes the breaker
    clock.t += 121
    loaded, _, communicate = _drive_hablar(motor, "Hola probe")
    assert communicate.called
    assert motor._edge_breaker.degraded is False


def test_open_breaker_does_not_skip_edge_when_piper_unavailable():
    motor = _motor(piper_available=False)
    for _ in range(3):
        motor._edge_breaker.record_failure("http_403")
    assert motor._edge_breaker.degraded
    _, _, communicate = _drive_hablar(motor, "Hola")
    assert communicate.called


def test_failure_log_has_type_and_status_but_never_text(caplog):
    motor = _motor()
    with caplog.at_level(logging.DEBUG):
        _drive_hablar(motor, SENTINEL, edge_save_side_effect=_handshake(403))
    fail = [r.getMessage() for r in caplog.records if "[EDGE_TTS_FAIL]" in r.getMessage()]
    assert fail, "expected an [EDGE_TTS_FAIL] line"
    line = fail[0]
    assert "type=WSServerHandshakeError" in line and "status=403" in line
    assert SENTINEL not in caplog.text
    assert "SECRET_TOKEN" not in caplog.text
    assert "Qwen3-TTS" not in caplog.text


LONG = (
    "Primera frase bastante larga para que se separe sola. "
    "Segunda frase igualmente larga para que se separe aparte. "
    "Tercera frase tambien larga para cerrar el ejemplo completo."
)


def _drive_sequence(motor, text, outcomes):
    """Like _drive_hablar but Edge outcome is per call: None = ok, exception = raise."""
    from unittest.mock import AsyncMock, MagicMock, patch

    motor.pygame = MagicMock()
    motor.pygame.mixer.music.get_busy.return_value = False
    motor.health_monitor = None
    motor.voz_referencia = None
    motor.motor_tts = "ligero"
    it = iter(outcomes)

    def _save(path, *a, **k):
        exc = next(it)
        if exc is not None:
            raise exc
        open(path, "wb").close()

    communicate = MagicMock()
    communicate.return_value.save = AsyncMock(side_effect=_save)
    with patch("opencohost.core.llm_engine.edge_tts.Communicate", communicate):
        motor._hablar(text, source="direct")
    return [c.args[0] for c in motor.pygame.mixer.music.load.call_args_list]


def test_mixed_fragments_keep_order_mp3_wav_mp3():
    motor = _motor()
    loaded = _drive_sequence(motor, LONG, [None, _handshake(403), None])
    exts = [p.rsplit(".", 1)[-1] for p in loaded]
    assert exts == ["mp3", "wav", "mp3"], exts


def test_barge_in_during_failed_edge_does_not_run_piper_or_leave_partial_file():
    import os
    from unittest.mock import AsyncMock, MagicMock, patch

    motor = _motor()
    motor.pygame = MagicMock()
    motor.pygame.mixer.music.get_busy.return_value = False
    motor.health_monitor = None
    motor.voz_referencia = None
    motor.motor_tts = "ligero"
    paths = []

    def _save(path, *a, **k):
        paths.append(path)
        open(path, "wb").close()  # partial file
        motor._speaking = False  # barge-in while Edge was in flight
        raise _handshake(403)

    communicate = MagicMock()
    communicate.return_value.save = AsyncMock(side_effect=_save)
    with patch("opencohost.core.llm_engine.edge_tts.Communicate", communicate):
        motor._hablar("Hola mundo", source="direct")
    assert paths
    motor._piper.synthesize.assert_not_called()
    assert not os.path.exists(paths[0]), "partial mp3 must be removed"


def test_probe_failure_reopens_with_doubled_cooldown_and_logs(caplog):
    class Clock:
        t = 1000.0

        def __call__(self):
            return self.t

    clock = Clock()
    motor = _motor()
    motor._edge_breaker = EdgeBreaker(3, 120, 900, clock=clock)
    for _ in range(3):
        _drive_hablar(motor, "Hola", edge_save_side_effect=_handshake(403))
    clock.t += 121
    with caplog.at_level(logging.WARNING):
        _, _, communicate = _drive_hablar(motor, "Hola probe", edge_save_side_effect=_handshake(403))
    assert communicate.called
    assert motor._edge_breaker.cooldown_s == 240 and motor._edge_breaker.degraded
    assert "state=half_open" in caplog.text
    assert "state=open cooldown_s=240" in caplog.text


def test_offline_arriving_while_degraded_latches_flag():
    motor = _motor()
    for _ in range(3):
        motor._edge_breaker.record_failure("http_403")
    motor._edge_breaker._until = 0.0  # cooldown elapsed -> probe goes to Edge
    _drive_hablar(motor, "Hola", edge_save_side_effect=socket.gaierror("dns"))
    assert motor._edge_tts_offline is True
