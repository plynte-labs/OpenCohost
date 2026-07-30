"""Test C — live adversarial repetition probe.

A small, explicit number of generations against the operator's real cloud
provider, with settings that RAISE the probability of degeneration without
demanding a repeat (no "repeat this three times" instruction — that would be an
instructed repetition, a different thing entirely).

Pass/fail contract, deliberately asymmetric:
  * It must NOT fail merely because no generation degenerated. Degeneration is
    probabilistic; absence of it is data, not a defect.
  * It MUST fail on a contract error, a secret in the logs, raw text in the
    sanitizer telemetry, or a broken pipeline.

Reports counts only: how many runs repeated, how many were repaired, how many
were rejected, and how long text took to reach the TTS boundary.
Opt-in only: OPENCOHOST_LIVE_CLOUD_TESTS=1.
"""
from __future__ import annotations

import logging
import time

import pytest

from tests.live_cloud.conftest import MAX_CALLS_PER_TEST

pytestmark = pytest.mark.live_cloud

# Raises repetition probability structurally: an emphatic enumeration over a
# single subject, which is the shape the confirmed incident degenerated inside.
# It never asks for a repeat.
PROBE_PROMPT = (
    "Quejate en una sola frase larga, con varias cláusulas separadas por comas, "
    "de todo lo que faltaba en ese proyecto."
)


def test_live_repetition_probe(live_motor, live_posture, caplog):
    motor = live_motor
    runs = []

    for index in range(MAX_CALLS_PER_TEST):
        motor._hablar.reset_mock()
        motor._dialogue_spy.reset_mock()
        caplog.clear()

        started = time.monotonic()
        with caplog.at_level(logging.DEBUG, logger="OpenCohost"):
            motor._ejecutar_inferencia(PROBE_PROMPT, source="kira-agenda")
        to_tts_ms = int((time.monotonic() - started) * 1000)

        records = [r.getMessage() for r in caplog.records
                   if "[CLAUSE_SANITIZER]" in r.getMessage()]

        # ── Contract: the pipeline must not break, whatever the model did ────
        rejected = any("verdict=rejected" in m for m in records)
        if rejected:
            # A rejection hands "" to the ladder, so nothing is spoken. That is
            # correct behavior, not a pipeline break.
            assert motor._hablar.call_count == 0
        else:
            assert motor._hablar.call_count == 1, "TTS boundary not reached"
            spoken = motor._hablar.call_args[0][0]
            assert spoken.strip()
            assert motor._dialogue_spy.call_args[0][0] == spoken
            assert motor.historial[-1]["content"] == spoken

        # ── Contract: no secret, no raw text in telemetry ────────────────────
        for message in records:
            assert "api_key" not in message
            assert "nvapi-" not in message
            assert "verdict=" in message and "removed=" in message

        runs.append({
            "run": index + 1,
            "detected": bool(records),
            "repaired": any("verdict=repaired" in m for m in records),
            "rejected": rejected,
            "to_tts_ms": to_tts_ms,
        })

    detected = sum(1 for r in runs if r["detected"])
    repaired = sum(1 for r in runs if r["repaired"])
    rejected = sum(1 for r in runs if r["rejected"])
    latencies = [r["to_tts_ms"] for r in runs]

    print(
        f"\n[live_probe] provider={live_posture['provider_id']} "
        f"model={live_posture['model']} runs={len(runs)} detected={detected} "
        f"repaired={repaired} rejected={rejected} "
        f"to_tts_ms_min={min(latencies)} to_tts_ms_max={max(latencies)}"
    )

    # Deliberately NOT an assertion on `detected`: a probe that never triggers
    # is a report, not a failure. The only assertions above are contract ones.
    assert len(runs) == MAX_CALLS_PER_TEST
