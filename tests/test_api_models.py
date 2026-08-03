"""Unit tests for opencohost.api.models — pydantic wire-shape contracts.

Pins field names per design v2.1 (A-M1): StatusResponse MUST serialize
`is_ready`/`is_speaking`/`is_processing` (NOT `ready`/`speaking`/`processing`).
"""

import dataclasses

from opencohost.api.models import (
    StatusResponse,
    HealthState,
    SwitchProfileRequest,
    SwitchProfileResponse,
    RejectedResponse,
)

# Import from core is fine in a test — only the opencohost/api/ PACKAGE itself
# must avoid core/ui imports.
from opencohost.core.observability.health_monitor import MonitorState


def _health_kwargs():
    return dict(
        vram_status="ok",
        rtf_status="ok",
        ollama_status="ok",
        qwen_status="ok",
        overall_status="ok",
        ollama_lifecycle="running",
        qwen_lifecycle="running",
        free_vram_mb=1024.0,
        rtf_rolling_avg=0.5,
        last_updated=123.456,
    )


def test_status_response_wire_keys_exact():
    health = HealthState(**_health_kwargs())
    resp = StatusResponse(
        is_ready=True,
        current_model="qwen3:8b",
        is_speaking=False,
        is_processing=False,
        active_profile="default",
        health=health,
        state_version=0,
    )
    dumped = resp.model_dump()
    assert set(dumped.keys()) == {
        "is_ready",
        "current_model",
        "is_speaking",
        "is_processing",
        "active_profile",
        "active_profile_id",
        "health",
        "state_version",
        "ollama_warming",
        "session_mode",
        "llm_generating",
        "pending_commands_count",
        "avatar_state",
        "obs_connected",
        "provider",
        "transport",
        "fallback_active",
        "fallback_reason",
        "next_cloud_probe_in_seconds",
        "ctx_telemetry",
    }
    # A-M1 pin: NOT the shortened forms.
    assert "ready" not in dumped
    assert "speaking" not in dumped
    assert "processing" not in dumped


def test_status_response_current_model_nullable():
    health = HealthState(**_health_kwargs())
    resp = StatusResponse(
        is_ready=False,
        current_model=None,
        is_speaking=False,
        is_processing=False,
        active_profile="default",
        health=health,
        state_version=0,
    )
    assert resp.model_dump()["current_model"] is None


def test_health_state_mirrors_monitor_state_fields():
    # Derived from the source of truth (not hardcoded): must fail if
    # MonitorState gains/loses a field that HealthState does not mirror.
    expected_fields = {f.name for f in dataclasses.fields(MonitorState)}

    health = HealthState(**_health_kwargs())
    dumped = health.model_dump()
    assert set(dumped.keys()) == expected_fields


def test_health_state_rtf_rolling_avg_nullable():
    kwargs = _health_kwargs()
    kwargs["rtf_rolling_avg"] = None
    health = HealthState(**kwargs)
    assert health.model_dump()["rtf_rolling_avg"] is None


def test_switch_profile_request_shape():
    req = SwitchProfileRequest(name="streamer_mode", idempotency_key="abc123")
    dumped = req.model_dump()
    assert dumped["name"] == "streamer_mode"
    assert dumped["idempotency_key"] == "abc123"


def test_switch_profile_request_idempotency_key_optional():
    req = SwitchProfileRequest(name="streamer_mode")
    assert req.model_dump()["idempotency_key"] is None


def test_switch_profile_response_shape():
    resp = SwitchProfileResponse(accepted=True, command_id="cmd_" + "a" * 32, status="queued")
    dumped = resp.model_dump()
    assert dumped == {
        "accepted": True,
        "command_id": "cmd_" + "a" * 32,
        "status": "queued",
    }


def test_rejected_response_shape():
    resp = RejectedResponse(accepted=False, reason="queue_full")
    dumped = resp.model_dump()
    assert dumped == {"accepted": False, "reason": "queue_full"}
