"""WU3 — Edge-TTS rate wired to the speed setting.

Strict TDD for Bug C: set_tts_speed only touches Piper's length_scale via
self._piper.set_length_scale(); edge_tts.Communicate never receives a rate
argument, so speed changes are silently inert whenever Edge-TTS is the active
engine. edge_rate_for_length_scale() converts the persisted Piper length_scale
(higher = slower) into the equivalent signed Edge-TTS rate string, and
_hablar()'s productor() snapshots it once per utterance and passes it to
Communicate(rate=...).
"""
from __future__ import annotations

import queue
import types
from unittest.mock import MagicMock

import pytest

from opencohost.core import llm_engine
from opencohost.core.llm_engine import MotorVocalIA, edge_rate_for_length_scale


def _make_motor():
    log_q: queue.Queue = queue.Queue()
    events: list = []
    motor = MotorVocalIA(log_q, events.append)
    motor.ollama = MagicMock()
    motor.pygame = MagicMock()
    motor.pygame.mixer.music.get_busy.return_value = False
    motor.is_ready = True
    return motor, events


@pytest.mark.parametrize(
    "scale, expected",
    [
        (1.0, "+0%"),
        (1.15, "-13%"),
        (1.30, "-23%"),
        (1.45, "-31%"),
        (0.0, "+0%"),  # guard: never divide by zero / invert direction
    ],
)
def test_rate_formula_table(scale, expected):
    assert edge_rate_for_length_scale(scale) == expected


def test_hablar_passes_rate_to_edge(monkeypatch):
    """set_tts_speed(1.30) -> the next Edge-TTS utterance carries rate="-23%"."""
    motor, _ = _make_motor()
    motor.tts_local_only = False
    motor._edge_tts_offline = False
    motor.motor_tts = "ligero"
    motor._piper = MagicMock()
    monkeypatch.setattr(llm_engine, "save_tts_speed", lambda *_a, **_k: None)

    recorded: dict = {}

    class FakeCommunicate:
        def __init__(self, text, voice, **kw):
            recorded["rate"] = kw.get("rate")

        async def save(self, path):
            open(path, "wb").close()

    fake_edge = types.SimpleNamespace(Communicate=FakeCommunicate)
    monkeypatch.setattr(llm_engine, "edge_tts", fake_edge)

    motor._dispatch_command("set_tts_speed", 1.30)
    motor._hablar("frase de prueba para un solo fragmento corto", source="direct")

    assert recorded["rate"] == "-23%"


def test_set_tts_speed_updates_single_source_of_truth(monkeypatch):
    motor, _ = _make_motor()
    fake_piper = MagicMock()
    motor._piper = fake_piper
    monkeypatch.setattr(llm_engine, "save_tts_speed", lambda *_a, **_k: None)

    motor._dispatch_command("set_tts_speed", 1.45)

    assert motor._tts_length_scale == 1.45
    fake_piper.set_length_scale.assert_called_once_with(1.45)
