"""Pydantic wire models for the Kira FastAPI API layer (Phase 1).

Field names on `StatusResponse` are pinned to the `is_`-prefixed forms
(design v2.1 A-M1) to match the FE wire contract (spec R1) — NOT the
shortened `ready`/`speaking`/`processing` forms.
"""

from typing import Optional

from pydantic import BaseModel


class ModelCatalogEntry(BaseModel):
    """Mirrors one entry of `opencohost.config.settings.MODELS_CATALOG`."""

    display: str
    desc: str
    size_gb: float
    family: str


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


class ModelsResponse(BaseModel):
    """GET /api/models (Tier B, direct read).

    `discovered` degrades to `[]` when live Ollama discovery times out or
    errors — the catalog + tiers still resolve to safe catalog-only values.
    """

    catalog: dict[str, ModelCatalogEntry]
    discovered: list[str]
    current_model: Optional[str]
    tiers: dict[str, str]
    active_tier: str


class TTSConfigResponse(BaseModel):
    """GET /api/tts/config (Tier B, direct read). Pure config read, no I/O timeout needed."""

    piper_voice: str
    local_only: bool
    speed: float
    engine: str
    heavy_available: bool


class StreamChatLiveResponse(BaseModel):
    """GET /api/stream/chat-live (Tier C, R8-CRITICAL).

    CONNECTION STATE + LIMITS ONLY — never add a field carrying viewer
    message text. Mirrors opencohost.smart_aggregator.aggregator.Aggregator
    state accessors (`_source`, `.activity`, `._spam_max_messages`,
    `.get_filter_policy()`) verbatim.
    """

    connected: bool
    platform: Optional[str]
    source_id: Optional[str]
    threshold_per_second: float
    cooldown_seconds: float
    max_messages_per_user: int
    filter_policy: str


class StreamConnectRequest(BaseModel):
    url: str


class StreamLimitsRequest(BaseModel):
    threshold_per_second: Optional[float] = None
    cooldown_seconds: Optional[float] = None
    max_messages_per_user: Optional[int] = None
    filter_policy: Optional[str] = None


class MemoriaStatsResponse(BaseModel):
    """GET /api/memoria/stats (Tier B, R8-CRITICAL).

    COUNTS ONLY — never add a field carrying memoria/card title, content,
    transcript, or any other text. See `_DIGEST_CAPTURE_SOURCES` /
    `memory_inspector_snapshot` in opencohost/core/llm_engine.py (the only
    provenance gates — reused verbatim, never re-derived).
    """

    session_turns: int
    digest_entries: int
    saved_memorias: int
    pinned: int
    editorial_cards_by_status: dict[str, int]
