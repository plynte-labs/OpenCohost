"""Integration specs for the chat-activation telemetry wiring (A-lite, measure-first).

Drives a real Aggregator and asserts each in-aggregator seam emits a metadata-only
record, that NO raw chat text leaks end-to-end, and that behavior is identical when
the flag is off. The cross-module seams (TTL expiry, spoken clock) live in llm_engine
and are wired separately.
"""
from __future__ import annotations

import json
import time
from unittest.mock import MagicMock

from opencohost.smart_aggregator.aggregator import Aggregator
from opencohost.smart_aggregator.filter_telemetry import FilterStage, ReasonCode


def _agg(enabled: bool = True) -> Aggregator:
    agg = Aggregator()
    agg._telemetry.enabled = enabled
    # Isolate the seam under test from the vibe/intent machinery.
    agg.thermometer = MagicMock()
    agg.thermometer.compute_vibe.return_value = {"temperature": 50.0}
    agg.intent_aggregator = MagicMock()
    return agg


def _msg(text: str, user: str = "viewer", t=None) -> dict:
    return {"text": text, "user": user, "timestamp": t if t is not None else time.time()}


_CLEAN = "me encanta este juego que estas mostrando hoy en el stream"


def test_message_filter_reject_is_recorded():
    agg = _agg()
    agg.process_message(_msg(""))  # empty -> message_filter reject
    recs = agg._telemetry.recent()
    assert any(r.stage == FilterStage.MESSAGE_FILTER.value and not r.accepted for r in recs)


def test_accept_and_sampling_are_recorded():
    agg = _agg()
    agg.process_message(_msg(_CLEAN))
    stages = {r.stage for r in agg._telemetry.recent()}
    assert FilterStage.QUALITY_GATE.value in stages
    assert FilterStage.CONTEXT_SAMPLING.value in stages
    assert any(
        r.stage == FilterStage.QUALITY_GATE.value and r.accepted
        for r in agg._telemetry.recent()
    )


def test_no_raw_chat_text_end_to_end():
    agg = _agg()
    agg.process_message(_msg("ZZ_SECRET_PAYLOAD_42 hola mundo test de chat real"))
    blob = json.dumps([r.as_log_dict() for r in agg._telemetry.recent()])
    blob += json.dumps(agg._telemetry.rollup())
    assert "ZZ_SECRET_PAYLOAD_42" not in blob


def test_disabled_emits_nothing_identical_behavior():
    agg = _agg(enabled=False)
    agg.process_message(_msg(_CLEAN))
    agg.process_message(_msg(""))
    assert agg._telemetry.recent() == []
    assert agg.get_diagnostics().get("activation_telemetry") is None


def test_activity_trigger_decisions_are_recorded():
    agg = _agg()
    t = 1000.0  # threshold_per_second=1.0, window=5 -> rate reaches 1.0 at the 5th msg
    for _ in range(7):
        agg.activity.on_message({"timestamp": t})
    reasons = {
        r.reason_code
        for r in agg._telemetry.recent()
        if r.stage == FilterStage.ACTIVITY_TRIGGER.value
    }
    assert ReasonCode.TRIGGER_FIRED.value in reasons
    assert ReasonCode.COOLDOWN_SUPPRESSED.value in reasons


def test_get_diagnostics_merges_activation_telemetry():
    agg = _agg()
    agg.process_message(_msg(_CLEAN))
    diag = agg.get_diagnostics()
    assert "activation_telemetry" in diag
    assert diag["activation_telemetry"]["total"] >= 1
