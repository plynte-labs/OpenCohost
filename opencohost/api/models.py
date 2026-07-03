"""Pydantic wire models for the Kira FastAPI API layer (Phase 1).

Field names on `StatusResponse` are pinned to the `is_`-prefixed forms
(design v2.1 A-M1) to match the FE wire contract (spec R1) — NOT the
shortened `ready`/`speaking`/`processing` forms.
"""

from typing import Optional

from pydantic import BaseModel


class HealthState(BaseModel):
    """Mirrors `opencohost.core.health_monitor.MonitorState`."""

    vram_status: str
    rtf_status: str
    ollama_status: str
    qwen_status: str
    overall_status: str
    ollama_lifecycle: str
    qwen_lifecycle: str
    free_vram_mb: float
    rtf_rolling_avg: Optional[float]
    last_updated: float


class StatusResponse(BaseModel):
    is_ready: bool
    current_model: Optional[str]
    is_speaking: bool
    is_processing: bool
    active_profile: str
    health: HealthState
    state_version: int


class ProfilesListResponse(BaseModel):
    """Profile NAMES only — never persona text, prompts, or other fields."""

    profiles: list[str]


class SwitchProfileRequest(BaseModel):
    name: str
    idempotency_key: Optional[str] = None


class CommandRequest(BaseModel):
    """Wire shape for POST /api/commands (Phase 2, B2).

    `command` is checked against a server-side whitelist in main.py — this
    model accepts any string so the handler controls the reject status code
    (422) instead of pydantic's enum-validation 422 with a different body.
    """

    command: str
    payload: dict = {}
    idempotency_key: Optional[str] = None


class SwitchProfileResponse(BaseModel):
    accepted: bool
    command_id: str
    status: str


class RejectedResponse(BaseModel):
    accepted: bool = False
    reason: str


class HealthResponse(BaseModel):
    """Fast liveness probe — no engine work, no queue. R8: no chat/model detail."""

    status: str
    engine_alive: bool
