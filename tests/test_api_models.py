"""Unit tests for opencohost.api.models — pydantic wire-shape contracts.

Pins field names per design v2.1 (A-M1): StatusResponse MUST serialize
`is_ready`/`is_speaking`/`is_processing` (NOT `ready`/`speaking`/`processing`).
"""

from opencohost.api.models import (
    StatusResponse,
    HealthState,
    SwitchProfileRequest,
    SwitchProfileResponse,
    RejectedResponse,
)


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
        "health",
        "state_version",
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
    health = HealthState(**_health_kwargs())
    dumped = health.model_dump()
    assert set(dumped.keys()) == {
        "vram_status",
        "rtf_status",
        "ollama_status",
        "qwen_status",
        "overall_status",
        "ollama_lifecycle",
        "qwen_lifecycle",
        "free_vram_mb",
        "rtf_rolling_avg",
        "last_updated",
    }


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
