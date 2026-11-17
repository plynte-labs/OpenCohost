"""[EDGE_TTS_SYNTH] per-synthesis timing log (edge-tts-prewarm E1 / B0).

Drives the real MotorVocalIA._hablar()/productor() with boundary mocks only
(edge_tts.Communicate = network, pygame = audio), the same way
tests/test_edge_fallback_any_failure.py does. After a SUCCESSFUL Edge
synthesis one INFO line carries the 0-based sentence index inside this
_hablar_impl call, the wall milliseconds around asyncio.run(...), and the
character count of the synthesized text. The text itself is never logged.
"""
from __future__ import annotations

import logging
import re

import aiohttp

from tests.test_llm_engine_piper_fallback import (
    _drive_hablar,
    _make_mock_piper,
    _make_motor,
)

SYNTH_RE = re.compile(r"\[EDGE_TTS_SYNTH\] idx=(\d+) ms=(\d+) chars=(\d+)")
SENTINEL = "zxqv-secret-sentence"
TWO_SENTENCES = (
    "Primera frase bastante larga para que se separe sola. "
    "Segunda frase igualmente larga para que se separe aparte."
)


def _handshake(status=403):
    url = aiohttp.client.URL("wss://example.invalid/x?token=SECRET_TOKEN")
    ri = aiohttp.RequestInfo(url=url, method="GET", headers={}, real_url=url)
    return aiohttp.WSServerHandshakeError(ri, (), status=status, message="Invalid response status")


def _motor():
    motor, *_ = _make_motor()
    motor.tts_local_only = False
    motor._edge_tts_offline = False
    motor._piper = _make_mock_piper(available=True)
    return motor


def test_successful_edge_synthesis_logs_timing_line(caplog):
    motor = _motor()
    text = "Hola mundo."
    with caplog.at_level(logging.INFO):
        _, _, communicate = _drive_hablar(motor, text)
    assert communicate.called
    match = SYNTH_RE.search(caplog.text)
    assert match, "expected an [EDGE_TTS_SYNTH] line after a successful Edge synthesis"
    assert match.group(1) == "0"
    assert int(match.group(2)) >= 0
    assert int(match.group(3)) == len(text)


def test_two_sentences_log_idx_zero_then_one(caplog):
    motor = _motor()
    with caplog.at_level(logging.INFO):
        _drive_hablar(motor, TWO_SENTENCES)
    indices = [m.group(1) for m in SYNTH_RE.finditer(caplog.text)]
    assert indices == ["0", "1"], indices


def test_timing_line_never_contains_sentence_text(caplog):
    motor = _motor()
    text = f"Hola {SENTINEL} mundo."
    with caplog.at_level(logging.DEBUG):
        _drive_hablar(motor, text)
    synth_lines = [r.getMessage() for r in caplog.records if "[EDGE_TTS_SYNTH]" in r.getMessage()]
    assert synth_lines, "expected an [EDGE_TTS_SYNTH] line"
    assert all(SENTINEL not in line for line in synth_lines)
    assert SENTINEL not in caplog.text


def test_failed_edge_call_logs_no_timing_line_and_still_falls_back_to_piper(caplog):
    motor = _motor()
    with caplog.at_level(logging.INFO):
        loaded, piper, _ = _drive_hablar(
            motor, "Hola mundo.", edge_save_side_effect=_handshake(403)
        )
    assert "[EDGE_TTS_SYNTH]" not in caplog.text
    assert piper.synthesize.called
    assert len(loaded) == 1 and loaded[0].endswith(".wav")
