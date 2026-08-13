"""Cross-module specs for the chat-activation telemetry seams.

The in-aggregator seams (message filter, quality gate, context sampling, activity
trigger, should_call) live in the aggregator and are covered by
``test_chat_activation_telemetry.py``. THIS file covers the two seams that cross the
motor-worker / chat-source thread boundary:

  1. QUEUE_TTL — a ``source == "chat"`` item that expires in the motor's priority
     queue fires ``MotorVocalIA.on_chat_item_expired`` (RECORD-ONLY; item lifetime
     unchanged). Non-chat expiries (agenda) must NOT fire the chat-only seam.
  2. spoken clock — a finished ``source == "chat"`` turn fires
     ``MotorVocalIA.on_chat_turn_spoken`` -> ``aggregator.on_chat_turn_spoken`` ->
     ``FilterTelemetry.mark_spoken()``.

Both seams are off-by-default (callbacks are ``None`` in production), gated strictly
on ``source == "chat"``, and must be no-ops when their callback is unset.

RED today: the callbacks/handlers do not exist yet.
"""
from __future__ import annotations

import queue
import time
from unittest.mock import MagicMock

from opencohost.core import turn_priority
from opencohost.core.llm_engine import MotorVocalIA
from opencohost.smart_aggregator.aggregator import Aggregator
from opencohost.smart_aggregator.filter_telemetry import FilterStage, ReasonCode


# ── helpers ──────────────────────────────────────────────────────────────────

def _chat_motor() -> MotorVocalIA:
    """A MotorVocalIA wired for deterministic chat generation (no real Ollama)."""
    log_q = queue.Queue()
    motor = MotorVocalIA(log_q, lambda event: None)
    motor.ollama = MagicMock()
    motor.pygame = MagicMock()
    motor.is_ready = True
    motor.current_model = "llama3"
    motor._reasoning_model_cache["llama3"] = False
    return motor


def _resp(text: str) -> dict:
    return {"message": {"content": text}}


def _agg(enabled: bool = True) -> Aggregator:
    agg = Aggregator()
    agg._telemetry.enabled = enabled
    return agg


# ── seam 1: QUEUE_TTL expiry (llm_engine) ────────────────────────────────────

def test_default_seam_callbacks_are_none():
    """Off-by-default: a fresh motor has both seams unset, so production never fires."""
    motor = _chat_motor()
    assert motor.on_chat_item_expired is None
    assert motor.on_chat_turn_spoken is None


def test_chat_item_ttl_expiry_fires_callback(monkeypatch):
    # Tier split (tauri_stream_chat_20260812): the stream discard window is
    # the turn_priority module setting now, not the engine's _pq_ttl_seconds.
    monkeypatch.setattr(turn_priority, "STREAM_TTL_SECONDS", 0.01)
    motor = _chat_motor()
    stale_ts = time.time() - 5.0
    motor._priority_queue = [(2, stale_ts, "hola chat", "chat")]  # prio 2 = stream tier
    captured: list = []
    motor.on_chat_item_expired = lambda info: captured.append(info)

    motor._process_priority_queue()

    assert len(captured) == 1
    assert captured[0]["age_sec"] >= 5.0
    assert captured[0]["ttl_sec"] == 0.01
    assert motor._priority_queue == []  # expired, as before — lifetime unchanged


def test_non_chat_ttl_expiry_does_not_fire_chat_seam():
    # An agenda item (prio 2) also expires, but the chat-only seam must stay silent.
    motor = _chat_motor()
    motor._pq_ttl_seconds = 0.01
    stale_ts = time.time() - 5.0
    motor._priority_queue = [(2, stale_ts, "agenda text", "kira-agenda")]
    captured: list = []
    motor.on_chat_item_expired = lambda info: captured.append(info)

    motor._process_priority_queue()

    assert captured == []
    assert motor._priority_queue == []  # still expired — gating did not change lifetime


def test_ttl_expiry_with_no_callback_is_safe(monkeypatch):
    monkeypatch.setattr(turn_priority, "STREAM_TTL_SECONDS", 0.01)
    motor = _chat_motor()
    stale_ts = time.time() - 5.0
    motor._priority_queue = [(2, stale_ts, "hola chat", "chat")]
    assert motor.on_chat_item_expired is None

    motor._process_priority_queue()  # must not raise

    assert motor._priority_queue == []


# ── seam 2: spoken clock (llm_engine) ────────────────────────────────────────

def test_chat_turn_fires_spoken_callback():
    motor = _chat_motor()
    motor._ollama_chat = MagicMock(return_value=_resp("una linea de chat cualquiera para el test"))
    motor._hablar = MagicMock()  # do not run the real TTS pipeline
    spoken: list = []
    motor.on_chat_turn_spoken = lambda: spoken.append(True)

    motor._ejecutar_inferencia("hola", source="chat")

    assert motor._hablar.called
    assert spoken == [True]


def test_non_chat_turn_does_not_fire_spoken_seam():
    motor = _chat_motor()
    motor._ollama_chat = MagicMock(return_value=_resp("una linea directa cualquiera para el test"))
    motor._hablar = MagicMock()
    spoken: list = []
    motor.on_chat_turn_spoken = lambda: spoken.append(True)

    motor._ejecutar_inferencia("hola", source="direct")

    assert motor._hablar.called
    assert spoken == []  # spoken clock is chat-only


def test_spoken_callback_with_no_callback_is_safe():
    motor = _chat_motor()
    motor._ollama_chat = MagicMock(return_value=_resp("una linea de chat cualquiera para el test"))
    motor._hablar = MagicMock()
    assert motor.on_chat_turn_spoken is None

    motor._ejecutar_inferencia("hola", source="chat")  # must not raise

    assert motor._hablar.called


# ── aggregator handlers ──────────────────────────────────────────────────────

def test_diagnostics_enabled_property_reflects_flag():
    assert _agg(enabled=True).diagnostics_enabled is True
    assert _agg(enabled=False).diagnostics_enabled is False


def test_on_chat_item_expired_records_queue_ttl_metadata_only():
    agg = _agg(enabled=True)
    agg.on_chat_item_expired({"age_sec": 7.5, "ttl_sec": 30.0})

    ttl_recs = [r for r in agg._telemetry.recent() if r.stage == FilterStage.QUEUE_TTL.value]
    assert len(ttl_recs) == 1
    r = ttl_recs[-1]
    assert r.reason_code == ReasonCode.TTL_EXPIRED.value
    assert r.accepted is False
    assert r.score == 7.5          # age carried in score
    assert r.threshold == 30.0     # ttl carried in threshold


def test_on_chat_item_expired_disabled_is_noop():
    agg = _agg(enabled=False)
    agg.on_chat_item_expired({"age_sec": 7.5, "ttl_sec": 30.0})
    assert agg._telemetry.recent() == []


def test_on_chat_turn_spoken_advances_spoken_clock(monkeypatch):
    agg = _agg(enabled=True)
    calls: list = []
    monkeypatch.setattr(agg._telemetry, "mark_spoken", lambda: calls.append(True))
    agg.on_chat_turn_spoken()
    assert calls == [True]


def test_on_chat_turn_spoken_disabled_is_noop(monkeypatch):
    agg = _agg(enabled=False)
    calls: list = []
    monkeypatch.setattr(agg._telemetry, "mark_spoken", lambda: calls.append(True))
    agg.on_chat_turn_spoken()
    assert calls == []


# ── composition-root wiring (attach_motor_telemetry_seams) ───────────────────

def test_attach_seams_when_enabled_wires_motor_callbacks():
    agg = _agg(enabled=True)
    motor = _chat_motor()
    agg.attach_motor_telemetry_seams(motor)
    assert motor.on_chat_item_expired == agg.on_chat_item_expired
    assert motor.on_chat_turn_spoken == agg.on_chat_turn_spoken


def test_attach_seams_when_disabled_leaves_motor_callbacks_none():
    agg = _agg(enabled=False)
    motor = _chat_motor()
    agg.attach_motor_telemetry_seams(motor)
    assert motor.on_chat_item_expired is None
    assert motor.on_chat_turn_spoken is None
