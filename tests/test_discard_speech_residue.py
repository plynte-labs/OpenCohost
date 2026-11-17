"""Tests for discard_speech_residue preference.

Covers:
1. Config loading & saving in opencohost.config.settings and opencohost.api.deps
2. TTSConfigResponse wire model & GET /api/tts/config response
3. POST /api/commands validation & engine dispatch for set_discard_speech_residue
4. MotorVocalIA._discard_speech_residue state & command handling
5. SpeechRouter:
   - discard_speech_residue property
   - Baseline preemption (flag=False): suspended & resumed
   - Discard preemption (flag=True): non-agenda active job cut with discard & discarded
   - Agenda preservation (flag=True): agenda active job suspended & resumed
   - Non-agenda stack sweeping (flag=True): non-agenda stack jobs purged, agenda stack jobs kept
"""
from __future__ import annotations

import json
import threading
import time
from typing import Optional
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from opencohost.api.models import CommandRequest, TTSConfigResponse
from opencohost.core.speech.router import (
    PRIORITY_AGENDA,
    PRIORITY_CHAT,
    PRIORITY_OWNER,
    SpeechJob,
    SpeechJobState,
    SpeechRouter,
)
from tests.test_api_phase1 import FakeHost


# ──────────────────────────────────────────────────────────────────────────
# 1. Config Loading & Saving
# ──────────────────────────────────────────────────────────────────────────


def test_load_discard_speech_residue_defaults_and_corrupt(tmp_path):
    from opencohost.config.settings import (
        load_discard_speech_residue,
        save_discard_speech_residue,
    )

    non_existent = str(tmp_path / "tts_settings_missing.json")
    assert load_discard_speech_residue(non_existent) is True

    corrupt_file = tmp_path / "corrupt.json"
    corrupt_file.write_text("invalid json content!@#$", encoding="utf-8")
    assert load_discard_speech_residue(str(corrupt_file)) is True

    non_dict_file = tmp_path / "non_dict.json"
    non_dict_file.write_text(json.dumps(["not", "a", "dict"]), encoding="utf-8")
    assert load_discard_speech_residue(str(non_dict_file)) is True

    # Explicit false in JSON still returns False
    explicit_false = tmp_path / "explicit_false.json"
    explicit_false.write_text(json.dumps({"discard_speech_residue": False}), encoding="utf-8")
    assert load_discard_speech_residue(str(explicit_false)) is False


def test_save_and_load_discard_speech_residue(tmp_path):
    from opencohost.config.settings import (
        load_discard_speech_residue,
        save_discard_speech_residue,
    )

    cfg_file = str(tmp_path / "tts_settings.json")

    # Initial state (unconfigured / missing file defaults to True)
    assert load_discard_speech_residue(cfg_file) is True

    # Save False
    save_discard_speech_residue(False, config_file=cfg_file)
    assert load_discard_speech_residue(cfg_file) is False

    # Inspect json on disk
    with open(cfg_file, "r", encoding="utf-8") as f:
        data = json.load(f)
    assert data.get("discard_speech_residue") is False
    assert "saved_at" in data

    # Save True
    save_discard_speech_residue(True, config_file=cfg_file)
    assert load_discard_speech_residue(cfg_file) is True


def test_save_discard_speech_residue_preserves_existing_keys(tmp_path):
    from opencohost.config.settings import (
        load_discard_speech_residue,
        save_discard_speech_residue,
    )

    cfg_file = tmp_path / "tts_settings.json"
    cfg_file.write_text(
        json.dumps({"existing_key": "some_value", "discard_speech_residue": False}),
        encoding="utf-8",
    )

    save_discard_speech_residue(True, config_file=str(cfg_file))
    assert load_discard_speech_residue(str(cfg_file)) is True

    with open(str(cfg_file), "r", encoding="utf-8") as f:
        data = json.load(f)
    assert data.get("existing_key") == "some_value"
    assert data.get("discard_speech_residue") is True


def test_deps_re_exports_discard_speech_residue():
    from opencohost.api import deps
    from opencohost.api import main as main_mod

    assert hasattr(deps, "load_discard_speech_residue")
    assert hasattr(deps, "save_discard_speech_residue")
    assert hasattr(main_mod, "load_discard_speech_residue")
    assert hasattr(main_mod, "save_discard_speech_residue")


# ──────────────────────────────────────────────────────────────────────────
# 2. TTSConfigResponse & GET /api/tts/config
# ──────────────────────────────────────────────────────────────────────────


def test_tts_config_response_model_has_field():
    resp = TTSConfigResponse(
        piper_voice="es_MX",
        local_only=False,
        speed=1.0,
        engine="ligero",
        heavy_available=False,
    )
    assert resp.discard_speech_residue is True

    resp_false = TTSConfigResponse(
        piper_voice="es_MX",
        local_only=False,
        speed=1.0,
        engine="ligero",
        heavy_available=False,
        discard_speech_residue=False,
    )
    assert resp_false.discard_speech_residue is False


def test_get_tts_config_endpoint_surfaces_flag(monkeypatch):
    import opencohost.api.main as main_mod

    flag_val = [False]
    monkeypatch.setattr(main_mod, "load_discard_speech_residue", lambda: flag_val[0])

    app = main_mod.create_app(host_factory=FakeHost)
    with TestClient(app) as client:
        # Flag is False
        resp = client.get("/api/tts/config")
        assert resp.status_code == 200
        assert resp.json()["discard_speech_residue"] is False

        # Flag is True
        flag_val[0] = True
        resp = client.get("/api/tts/config")
        assert resp.status_code == 200
        assert resp.json()["discard_speech_residue"] is True


# ──────────────────────────────────────────────────────────────────────────
# 3. POST /api/commands Validation & Dispatch
# ──────────────────────────────────────────────────────────────────────────


def test_api_commands_set_discard_speech_residue_validation():
    import opencohost.api.main as main_mod

    app = main_mod.create_app(host_factory=FakeHost)
    with TestClient(app) as client:
        # Non-boolean values rejected with 422
        for bad_val in ["true", 1, 0, None, [], {}]:
            resp = client.post(
                "/api/commands",
                json={"command": "set_discard_speech_residue", "payload": {"value": bad_val}},
            )
            assert resp.status_code == 422
            assert resp.json()["detail"] == "set_discard_speech_residue requires a boolean value"

        # Valid boolean accepted
        resp = client.post(
            "/api/commands",
            json={"command": "set_discard_speech_residue", "payload": {"value": True}},
        )
        assert resp.status_code == 200
        assert resp.json()["accepted"] is True


# ──────────────────────────────────────────────────────────────────────────
# 4. MotorVocalIA Command Dispatch
# ──────────────────────────────────────────────────────────────────────────


def test_motor_dispatches_set_discard_speech_residue(monkeypatch, tmp_path):
    from opencohost.core.llm_engine import MotorVocalIA
    import opencohost.config.settings as settings_mod

    cfg_file = str(tmp_path / "tts_settings.json")
    monkeypatch.setattr(settings_mod, "TTS_SETTINGS_FILE", cfg_file)

    motor = MotorVocalIA.__new__(MotorVocalIA)
    motor._lock = threading.Lock()
    motor._speaking = False
    motor._speech_router = None
    motor._deferred_control_commands = []
    motor._discard_speech_residue = False

    # Dispatch command to set True
    motor._dispatch_command("set_discard_speech_residue", True)
    assert motor._discard_speech_residue is True
    assert settings_mod.load_discard_speech_residue(cfg_file) is True

    # Dispatch command to set False
    motor._dispatch_command("set_discard_speech_residue", False)
    assert motor._discard_speech_residue is False
    assert settings_mod.load_discard_speech_residue(cfg_file) is False


# ──────────────────────────────────────────────────────────────────────────
# 5. SpeechRouter discard_speech_residue
# ──────────────────────────────────────────────────────────────────────────


class _FakeMotor:
    def __init__(self):
        self._lock = threading.Lock()
        self._speech_router_enabled = True
        self._speech_interrupt_enabled = True
        self._discard_speech_residue = False
        self.interrupted = False
        self.started_events = []
        self.ended_events = []

    def _speech_cancelled(self, source: str) -> bool:
        return False

    def _speech_boundary_start(self, source: str) -> None:
        self.started_events.append(source)

    def _speech_boundary_end(self) -> None:
        self.ended_events.append(time.monotonic())

    def interrupt_speaking(self) -> None:
        self.interrupted = True

    def ui_callback(self, status: str) -> None:
        pass


def test_speech_router_discard_speech_residue_property():
    motor = _FakeMotor()
    router = SpeechRouter(motor)
    assert router.discard_speech_residue is False
    motor._discard_speech_residue = True
    assert router.discard_speech_residue is True


def test_speech_router_baseline_preemption_when_flag_false():
    """Baseline: discard_speech_residue is False.
    Preempting an active job sets pause_requested='preempt', which pushes
    the victim onto _stack on reconcile.
    """
    motor = _FakeMotor()
    motor._discard_speech_residue = False
    router = SpeechRouter(motor)

    # Simulate an active non-agenda job
    victim = SpeechJob(
        job_id=1,
        text="Victim speech",
        source="direct",
        priority=PRIORITY_OWNER,
        chunks=["chunk1", "chunk2"],
        cursor=0,
        state=SpeechJobState.ACTIVE,
    )
    router._active = victim

    # Submit an incoming owner job that preempts victim
    preemptor = router.submit("Interruption", source="ptt", priority=PRIORITY_OWNER)

    # Under flag=False, victim receives 'preempt', NOT 'discard'
    assert victim.pause_requested == "preempt"
    assert motor.interrupted is True

    # Reconciling the victim pushes it to the stack
    state, _ = router._reconcile(victim, outcome=None, exc=None)
    # When exc is None and outcome is None, wait:
    # If interrupted, reconcile checks pause_reason in ("ptt", "preempt")
    outcome_stub = MagicMock()
    outcome_stub.chunks = ["chunk1", "chunk2"]
    outcome_stub.spoken = [0]
    outcome_stub.skipped = []
    outcome_stub.cursor = 1
    outcome_stub.interrupted = True
    outcome_stub.error = False

    victim.pause_requested = "preempt"
    state, _ = router._reconcile(victim, outcome=outcome_stub, exc=None)
    assert state == SpeechJobState.SUSPENDED
    assert victim in router._stack


def test_speech_router_preempts_non_agenda_victim_with_discard_when_flag_true():
    """When discard_speech_residue is True, an active non-agenda victim is cut
    with pause_requested='discard', so reconcile marks it DISCARDED and does
    NOT push it to _stack.
    """
    motor = _FakeMotor()
    motor._discard_speech_residue = True
    router = SpeechRouter(motor)

    # Active non-agenda job
    victim = SpeechJob(
        job_id=1,
        text="Victim speech",
        source="direct",
        priority=PRIORITY_OWNER,
        chunks=["chunk1", "chunk2"],
        cursor=0,
        state=SpeechJobState.ACTIVE,
    )
    router._active = victim

    # Submit non-agenda owner job
    preemptor = router.submit("Interruption", source="ptt", priority=PRIORITY_OWNER)

    # Victim must have pause_requested='discard'
    assert victim.pause_requested == "discard"
    assert motor.interrupted is True

    # Reconciling the victim:
    outcome_stub = MagicMock()
    outcome_stub.chunks = ["chunk1", "chunk2"]
    outcome_stub.spoken = [0]
    outcome_stub.skipped = []
    outcome_stub.cursor = 1
    outcome_stub.interrupted = True
    outcome_stub.error = False

    state, reason = router._reconcile(victim, outcome=outcome_stub, exc=None)
    assert state == SpeechJobState.DISCARDED
    assert victim not in router._stack


def test_speech_router_preempts_agenda_victim_with_suspend_even_when_flag_true():
    """When discard_speech_residue is True, an active AGENDA victim (kira-agenda)
    must still be preempted with pause_requested='preempt', preserving stack resume.
    """
    motor = _FakeMotor()
    motor._discard_speech_residue = True
    router = SpeechRouter(motor)

    # Active agenda job
    agenda_victim = SpeechJob(
        job_id=1,
        text="Agenda topic beat",
        source="kira-agenda-beat",
        priority=PRIORITY_AGENDA,
        chunks=["chunk1", "chunk2"],
        cursor=0,
        state=SpeechJobState.ACTIVE,
    )
    router._active = agenda_victim

    # Submit owner job
    preemptor = router.submit("Owner question", source="direct", priority=PRIORITY_OWNER)

    # Agenda victim must receive 'preempt', NOT 'discard'
    assert agenda_victim.pause_requested == "preempt"
    assert motor.interrupted is True

    # Reconciling pushes agenda job to stack
    outcome_stub = MagicMock()
    outcome_stub.chunks = ["chunk1", "chunk2"]
    outcome_stub.spoken = [0]
    outcome_stub.skipped = []
    outcome_stub.cursor = 1
    outcome_stub.interrupted = True
    outcome_stub.error = False

    agenda_victim.pause_requested = "preempt"
    state, _ = router._reconcile(agenda_victim, outcome=outcome_stub, exc=None)
    assert state == SpeechJobState.SUSPENDED
    assert agenda_victim in router._stack


def test_speech_router_sweeps_non_agenda_stack_when_flag_true():
    """When discard_speech_residue is True and a non-agenda job is submitted:
    - Any non-agenda jobs on _stack are swept and marked DISCARDED.
    - Agenda jobs on _stack are NOT swept.
    """
    motor = _FakeMotor()
    motor._discard_speech_residue = True
    router = SpeechRouter(motor)

    # Put a non-agenda job and an agenda job on _stack
    non_agenda_job = SpeechJob(
        job_id=1,
        text="Old viewer chat",
        source="chat",
        priority=PRIORITY_CHAT,
        chunks=["chunk1"],
        state=SpeechJobState.SUSPENDED,
    )
    agenda_job = SpeechJob(
        job_id=2,
        text="Agenda beat",
        source="kira-agenda",
        priority=PRIORITY_AGENDA,
        chunks=["chunk2"],
        state=SpeechJobState.SUSPENDED,
    )
    router._stack = [agenda_job, non_agenda_job]

    # Submit a new non-agenda job (e.g. direct)
    new_job = router.submit("New turn", source="direct", priority=PRIORITY_OWNER)

    # non_agenda_job must be discarded, agenda_job must stay in _stack
    assert non_agenda_job.state == SpeechJobState.DISCARDED
    assert non_agenda_job not in router._stack
    assert agenda_job in router._stack
    assert agenda_job.state == SpeechJobState.SUSPENDED


def test_speech_router_streaming_preempts_with_discard_when_flag_true():
    """submit_streaming also respects discard_speech_residue."""
    motor = _FakeMotor()
    motor._discard_speech_residue = True
    router = SpeechRouter(motor)

    # Active non-agenda job
    victim = SpeechJob(
        job_id=1,
        text="Victim speech",
        source="direct",
        priority=PRIORITY_OWNER,
        chunks=["chunk1", "chunk2"],
        cursor=0,
        state=SpeechJobState.ACTIVE,
    )
    router._active = victim

    # Submit streaming job
    preemptor = router.submit_streaming(source="ptt", priority=PRIORITY_OWNER)

    assert victim.pause_requested == "discard"
    assert motor.interrupted is True
