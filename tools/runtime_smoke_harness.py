"""Deterministic runtime smoke harness for OpenCohost.

This module is intentionally separate from the default pytest suite. Its first
mode stays deterministic by faking LLM/TTS/audio dependencies while exercising
the runtime contracts that matter for cohost/direct audio arbitration.
"""

from __future__ import annotations

import argparse
import json
import queue
import sys
import tempfile
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from core import llm_engine
from smart_aggregator.kira_agenda_controller import AgendaAction
from ui import app_shell


REQUIRED_INVARIANTS = (
    "process_survived",
    "speaking_events_balanced",
    "no_agenda_direct_overlap",
    "no_stale_speech_source",
    "shutdown_completed",
)


@dataclass(frozen=True)
class SmokeEvent:
    """Single observable event captured by the smoke harness."""

    name: str
    source: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "source": self.source,
            "detail": self.detail,
            "timestamp": self.timestamp,
        }


@dataclass
class SmokeReport:
    """Structured result for a runtime smoke scenario."""

    mode: str
    scenario: str
    exit_code: int
    timed_out: bool
    events: list[SmokeEvent]
    counters: dict[str, int]
    invariants: dict[str, bool]

    @property
    def failures(self) -> list[str]:
        failures: list[str] = []
        if self.exit_code != 0:
            failures.append(f"exit_code:{self.exit_code}")
        if self.timed_out:
            failures.append("timeout")
        for name in REQUIRED_INVARIANTS:
            if not self.invariants.get(name, False):
                failures.append(f"invariant_failed:{name}")
        return failures

    @property
    def passed(self) -> bool:
        return not self.failures

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "scenario": self.scenario,
            "exit_code": self.exit_code,
            "timed_out": self.timed_out,
            "passed": self.passed,
            "failures": self.failures,
            "events": [event.to_dict() for event in self.events],
            "counters": dict(self.counters),
            "invariants": dict(self.invariants),
        }


class _Trace:
    def __init__(self) -> None:
        self.events: list[SmokeEvent] = []
        self.counters: Counter[str] = Counter()
        self.speaking_started = threading.Event()

    def record(self, name: str, *, source: str | None = None, **detail: Any) -> None:
        self.events.append(SmokeEvent(name=name, source=source, detail=detail))
        self.counters[name] += 1
        if name == "speaking_start":
            self.speaking_started.set()


class _FakeResponse:
    status_code = 200
    content = b"RIFF deterministic-smoke"
    text = "ok"

    @staticmethod
    def json() -> dict[str, str]:
        return {"ok": "true"}


class _FakeMusic:
    def __init__(self, trace: _Trace) -> None:
        self._trace = trace
        self._busy_until = 0.0

    def load(self, path: str) -> None:
        self._trace.record("pygame_music_load", path=path)

    def play(self) -> None:
        self._busy_until = time.monotonic() + 0.12
        self._trace.record("pygame_music_play")

    def get_busy(self) -> bool:
        return time.monotonic() < self._busy_until

    def unload(self) -> None:
        self._trace.record("pygame_music_unload")


class _FakeMixer:
    def __init__(self, trace: _Trace) -> None:
        self.music = _FakeMusic(trace)


class _FakePygame:
    def __init__(self, trace: _Trace) -> None:
        self.mixer = _FakeMixer(trace)


class _FakeAgendaController:
    def __init__(self, trace: _Trace) -> None:
        self._trace = trace

    def start_prefetched_action(self, action: AgendaAction) -> None:
        self._trace.record(
            "prefetched_action_started",
            source=action.source,
            priority=action.priority,
        )


def _build_fake_app(motor: llm_engine.MotorVocalIA, trace: _Trace) -> app_shell.VocalAIApp:
    app = object.__new__(app_shell.VocalAIApp)
    app.motor_ia = motor
    app.kira_agenda = _FakeAgendaController(trace)
    app._kira_agenda_update_status = lambda: trace.record("agenda_status_updated")
    app._on_stream_admin_log = lambda msg: trace.record("stream_admin_log", message=msg)
    return app


def run_deterministic_cohost_direct_scenario(timeout: float = 5.0) -> SmokeReport:
    """Run deterministic cohost/direct interruption smoke scenario."""

    trace = _Trace()
    trace.record("process_started")
    errors: list[str] = []
    original_post = llm_engine.requests.post

    def ui_callback(event_name: str) -> None:
        source = motor.current_speech_source if event_name.startswith("speaking") else None
        trace.record(event_name, source=source)

    motor = llm_engine.MotorVocalIA(queue.Queue(), ui_callback)
    motor.is_ready = True
    motor.motor_tts = "pesado"
    motor.pygame = _FakePygame(trace)

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as ref:
        ref.write(b"RIFF reference")
        motor.voz_referencia = ref.name

    thread: threading.Thread | None = None
    timed_out = False
    try:
        app = _build_fake_app(motor, trace)
        llm_engine.requests.post = lambda *args, **kwargs: _FakeResponse()

        def speak_agenda() -> None:
            try:
                motor._hablar(
                    (
                        "Respuesta agenda suficientemente larga para generar audio "
                        "determinista y sostener la ventana de interrupcion directa."
                    ),
                    source="kira-agenda",
                )
            except Exception as exc:  # pragma: no cover - diagnostic path
                errors.append(f"{type(exc).__name__}:{exc}")
                trace.record("scenario_exception", message=str(exc))

        thread = threading.Thread(target=speak_agenda, daemon=True)
        thread.start()

        if not trace.speaking_started.wait(timeout=timeout):
            trace.record("scenario_timeout", phase="waiting_for_speaking_start")
            timed_out = True
        else:
            motor._prefetched_agenda = {
                "payload": "prefetched agenda prompt",
                "dialogo": "prefetched agenda response",
                "priority": 2,
                "source": "kira-agenda",
            }
            motor._prefetch_done.set()
            app._kira_agenda_prefetched_action = AgendaAction(
                kind="enqueue",
                prompt="prefetched agenda prompt",
                priority=2,
                source="kira-agenda",
            )

            motor._dispatch_command("process_context", "operator direct interaction")
            if any(item[3] == "direct" for item in motor._priority_queue):
                trace.record("direct_interaction_queued", source="direct")

            played_prefetch = app._kira_agenda_play_prefetched_if_ready()
            if not played_prefetch and motor._prefetched_agenda is None:
                trace.record("prefetch_skipped_for_direct", source="kira-agenda")
            elif played_prefetch:
                trace.record("prefetch_overlap_detected", source="kira-agenda")

        thread.join(timeout=timeout)
        if thread.is_alive():
            timed_out = True
            trace.record("scenario_timeout", phase="waiting_for_speaking_end")
    finally:
        llm_engine.requests.post = original_post
        Path(motor.voz_referencia).unlink(missing_ok=True)
    trace.record("shutdown_completed")

    counters = {
        "speaking_start": trace.counters.get("speaking_start", 0),
        "speaking_end": trace.counters.get("speaking_end", 0),
    }
    invariants = {
        "process_survived": not errors,
        "speaking_events_balanced": counters["speaking_start"] == counters["speaking_end"],
        "no_agenda_direct_overlap": trace.counters.get("prefetch_overlap_detected", 0) == 0,
        "no_stale_speech_source": not motor.is_speaking and motor.current_speech_source is None,
        "shutdown_completed": trace.counters.get("shutdown_completed", 0) == 1,
    }
    exit_code = 0 if not errors else 1
    return SmokeReport(
        mode="deterministic",
        scenario="cohost_direct_interruption",
        exit_code=exit_code,
        timed_out=timed_out,
        events=trace.events,
        counters=counters,
        invariants=invariants,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run OpenCohost runtime smoke scenarios.")
    parser.add_argument("--mode", choices=["deterministic"], default="deterministic")
    parser.add_argument("--json", dest="json_path", help="Optional path to write JSON report.")
    args = parser.parse_args(argv)

    report = run_deterministic_cohost_direct_scenario()
    payload = report.to_dict()

    if args.json_path:
        Path(args.json_path).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
