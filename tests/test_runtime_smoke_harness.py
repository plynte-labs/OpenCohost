"""Runtime smoke harness contract tests.

These tests keep the harness deterministic and out of real audio/device
dependencies. Real/semi-real audio smoke remains opt-in by design.
"""

from __future__ import annotations

import json
import queue

from core import llm_engine
from tools import runtime_smoke_harness as harness


def test_smoke_report_passes_only_when_all_invariants_are_true():
    report = harness.SmokeReport(
        mode="deterministic",
        scenario="cohost_direct_interruption",
        exit_code=0,
        timed_out=False,
        events=[
            harness.SmokeEvent("process_started"),
            harness.SmokeEvent("speaking_start", source="kira-agenda"),
            harness.SmokeEvent("speaking_end", source="kira-agenda"),
            harness.SmokeEvent("shutdown_completed"),
        ],
        counters={"speaking_start": 1, "speaking_end": 1},
        invariants={
            "process_survived": True,
            "speaking_events_balanced": True,
            "no_agenda_direct_overlap": True,
            "no_stale_speech_source": True,
            "shutdown_completed": True,
        },
    )

    assert report.passed is True
    assert report.failures == []


def test_smoke_report_fails_with_unbalanced_speech_events():
    report = harness.SmokeReport(
        mode="deterministic",
        scenario="cohost_direct_interruption",
        exit_code=0,
        timed_out=False,
        events=[harness.SmokeEvent("speaking_start", source="kira-agenda")],
        counters={"speaking_start": 1, "speaking_end": 0},
        invariants={
            "process_survived": True,
            "speaking_events_balanced": False,
            "no_agenda_direct_overlap": True,
            "no_stale_speech_source": True,
            "shutdown_completed": True,
        },
    )

    assert report.passed is False
    assert "invariant_failed:speaking_events_balanced" in report.failures


def test_deterministic_cohost_direct_scenario_survives_and_blocks_prefetch():
    report = harness.run_deterministic_cohost_direct_scenario()

    assert report.passed is True
    assert report.counters["speaking_start"] == report.counters["speaking_end"]
    assert report.invariants["no_agenda_direct_overlap"] is True
    assert report.invariants["no_stale_speech_source"] is True
    assert "direct_interaction_queued" in [event.name for event in report.events]
    assert "prefetch_skipped_for_direct" in [event.name for event in report.events]


def test_cli_writes_json_report_for_deterministic_mode(tmp_path):
    report_path = tmp_path / "runtime-smoke.json"

    exit_code = harness.main(["--mode", "deterministic", "--json", str(report_path)])

    assert exit_code == 0
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert payload["mode"] == "deterministic"
    assert payload["scenario"] == "cohost_direct_interruption"
    assert payload["passed"] is True


def test_empty_tts_text_balances_speech_events_and_clears_source():
    events = []

    motor = llm_engine.MotorVocalIA(
        queue.Queue(),
        lambda event: events.append((event, motor.current_speech_source)),
    )

    motor._hablar("   ", source="kira-agenda")

    assert events == [
        ("speaking_start", "kira-agenda"),
        ("speaking_end", None),
    ]
    assert motor.is_speaking is False
    assert motor.current_speech_source is None
