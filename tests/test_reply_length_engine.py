"""reply-length-selector-20261009 T2: engine plumbing and the [REPLY_LENGTH] log.

Real MotorVocalIA with a fake assembler that records the kwargs it receives.
"""
from __future__ import annotations

import logging
import queue
from unittest.mock import MagicMock

import pytest

from opencohost.config import reply_length as rl
from opencohost.config.settings import OWNER_BUNDLE_SOURCE
from opencohost.core import llm_engine
from opencohost.core.vision.sanitize import build_screen_block

BLOCK = build_screen_block("Barra de vida al 30%.")
SECRET = "explícame zanahoriaconfidencial en detalle por favor"


class FakeAssembler:
    def __init__(self):
        self.calls = []

    def assemble(self, contexto, source, **kwargs):
        self.calls.append({"contexto": contexto, "source": source, **kwargs})
        return "setup"


@pytest.fixture
def motor():
    m = llm_engine.MotorVocalIA(queue.Queue(), lambda event: None)
    m.current_model = "llama3"
    m.ollama = MagicMock()
    m._current_profile_id = None
    m._prompt_assembler = FakeAssembler()
    return m


def _build(motor, text, source="direct", screen_block=""):
    motor._build_generation_request(
        text,
        source,
        is_local=True,
        provider_cfg={},
        request_model="llama3",
        watchdog_timeout=None,
        screen_block=screen_block,
    )
    return motor._prompt_assembler.calls[-1]


def _reply_logs(caplog):
    return [r.getMessage() for r in caplog.records if "[REPLY_LENGTH]" in r.getMessage()]


@pytest.mark.parametrize("source", ["direct", "ptt", OWNER_BUNDLE_SOURCE])
def test_owner_sources_get_a_resolved_level(motor, source):
    assert _build(motor, "hola", source)["reply_level"] == "short"
    assert _build(motor, SECRET, source)["reply_level"] == "long"
    assert _build(motor, "qué opinas del juego que estoy jugando ahora", source)[
        "reply_level"
    ] == "medium"


@pytest.mark.parametrize("source", ["chat", "kira-agenda", "event"])
def test_non_owner_sources_pass_none(motor, source, caplog):
    caplog.set_level(logging.INFO, logger="OpenCohost")
    assert _build(motor, "hola", source)["reply_level"] is None
    assert _reply_logs(caplog) == []


def test_store_mode_wins_over_auto(motor):
    rl.set_reply_length_mode("long")
    assert _build(motor, "hola")["reply_level"] == "long"


def test_profile_extended_forces_very_long(motor):
    motor._extended_responses = True
    assert _build(motor, "hola")["reply_level"] == "very_long"


def test_vision_turn_is_medium_on_auto(motor):
    assert _build(motor, SECRET, screen_block=BLOCK)["reply_level"] == "medium"


def test_heuristic_reads_text_without_history_wrapper(motor, monkeypatch):
    import opencohost.core.memory.memoria_store as ms

    monkeypatch.setattr(ms, "strip_history_wrapper", lambda text: "hola")
    assert _build(motor, "wrapped explícame todo en detalle por favor")["reply_level"] == "short"


def test_one_log_line_without_user_text(motor, caplog):
    caplog.set_level(logging.INFO, logger="OpenCohost")
    _build(motor, SECRET)
    lines = _reply_logs(caplog)
    assert lines == ["[REPLY_LENGTH] mode=auto level=long reason=depth_request"]
    full = "\n".join(r.getMessage() for r in caplog.records)
    assert "zanahoriaconfidencial" not in full
    records = [r for r in caplog.records if "[REPLY_LENGTH]" in r.getMessage()]
    assert all(r.name == "OpenCohost" for r in records)


def test_log_reports_setting_mode(motor, caplog):
    caplog.set_level(logging.INFO, logger="OpenCohost")
    rl.set_reply_length_mode("short")
    _build(motor, SECRET)
    assert _reply_logs(caplog) == ["[REPLY_LENGTH] mode=short level=short reason=setting"]
