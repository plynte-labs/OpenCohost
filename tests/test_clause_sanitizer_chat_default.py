"""Clause sanitizer armed for `chat` by default (ADR-039 gate).

Real-session evidence (logs/opencohost_20260812_194539.log, 2026-08-12): 42
chat-sourced replies went out with zero [CLAUSE_SANITIZER] records, while all
6 agenda-sourced replies were checked (verdict=clean every time). Chat is the
busiest path to the audience and was the only unprotected one. See
docs/deferred-20260729-clause-sanitizer-scope.md §2.1 for the prior
BLOCKED-BY-EVIDENCE rationale this evidence resolves.

Harness matches tests/test_clause_sanitizer_seam.py: only `_hablar` is
mocked, no real audio/TTS/STT.
"""
from __future__ import annotations

import queue
from unittest.mock import MagicMock

from opencohost.config.settings import CLAUSE_SANITIZER_DEFAULT_SOURCES
from opencohost.core.llm_engine import MotorVocalIA

INCIDENT = "No había roadmap, no había monetización, no había roadmap, no había roadmap."
INCIDENT_REPAIRED = "No había roadmap, no había monetización."


def _resp(text):
    return {"message": {"content": text}}


def _motor():
    motor = MotorVocalIA(queue.Queue(), lambda event: None)
    motor.ollama = MagicMock()
    motor.pygame = MagicMock()
    motor.is_ready = True
    motor.current_model = "llama3"
    motor._reasoning_model_cache["llama3"] = False
    motor._hablar = MagicMock()
    return motor


def test_chat_is_armed_by_default():
    """The frozenset must contain "chat" -- but this alone is not proof the
    seam actually uses it (a stale/shadowed constant would still pass this).
    Kept alongside the behavioral test below as the cheap fast-fail check.
    """
    assert "chat" in CLAUSE_SANITIZER_DEFAULT_SOURCES


def test_chat_sourced_generation_is_sanitized_by_default():
    """The real regression test: a chat-sourced reply with an intra-sentence
    repetition must come out REPAIRED through the actual seam
    (`_generar_dialogo`), using the real default config -- no monkeypatching
    `CLAUSE_SANITIZER_SOURCES`. If the default reverts to agenda-only, this
    fails because `out` is the raw, unrepaired `INCIDENT` text, not because a
    frozenset lost a string.
    """
    motor = _motor()
    motor._ollama_chat = MagicMock(return_value=_resp(INCIDENT))

    out = motor._generar_dialogo("pregunta de un viewer", source="chat",
                                 commit_history=True)

    assert out == INCIDENT_REPAIRED
    raw_fragment = "no había roadmap, no había roadmap"
    assert raw_fragment not in out.lower()
