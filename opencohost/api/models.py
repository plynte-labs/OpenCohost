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


class ChatTurnRequest(BaseModel):
    """Wire shape for POST /api/chat/turn (design v2.1 build-order step 4).

    `text` is the raw viewer/operator message — dispatched verbatim as the
    `process_context` payload (see `MotorVocalIA._dispatch_command` in
    llm_engine.py). R8: `text` is NEVER echoed back in any response.
    """

    text: str
    idempotency_key: Optional[str] = None


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


class ObsConfigResponse(BaseModel):
    """GET/PUT /api/obs/config response (Tier C).

    R8/secret: NEVER carries the stored password — `password_set` is a bool
    derived from it (`bool(non-empty stored password)`), nothing more.
    """

    enabled: bool
    host: str
    port: int
    source: str
    password_set: bool


class ObsConfigRequest(BaseModel):
    """PUT /api/obs/config body — a partial update.

    Every field is optional; an omitted field leaves the stored value
    unchanged. `password` is write-only: provide it to set it, omit it to
    leave the stored password untouched (omitting never clears it).

    Reused as-is for POST /api/obs/test's optional `{host, port, password}`
    override (its `enabled`/`source` fields are simply ignored there).
    """

    enabled: Optional[bool] = None
    host: Optional[str] = None
    port: Optional[int] = None
    source: Optional[str] = None
    password: Optional[str] = None


class ObsTestResponse(BaseModel):
    """POST /api/obs/test response. Never carries the password."""

    ok: bool
    error: Optional[str] = None


class AvatarConfigResponse(BaseModel):
    """GET/PUT /api/avatar/config response (Tier C). Paths only, no secrets."""

    enabled: bool
    mode: str
    assets_folder: str
    state_images: dict[str, str]


class AvatarConfigRequest(BaseModel):
    """PUT /api/avatar/config body — a partial update.

    `state_images` keys are validated against `VALID_STATES` server-side
    (unknown state -> 422) before anything is applied. Image UPLOAD is
    deferred (owner decision) — values are paths only, never multipart.
    """

    enabled: Optional[bool] = None
    mode: Optional[str] = None
    state_images: Optional[dict[str, str]] = None


class AgendaTopicOut(BaseModel):
    """Mirrors `opencohost.smart_aggregator.kira_agenda_controller.AgendaTopic`.

    Operator-authored/sanitized fields only (title/angle already pass
    through `sanitize_topic_text`) — never raw viewer chat.
    """

    id: str
    title: str
    angle: str
    priority: str
    response_length: str
    status: str
    turns_spoken: int
    confidence: str
    source: str


class AgendaSessionSettings(BaseModel):
    """Global agenda pacing knobs — mirrors controller attrs set via
    `set_session_settings()`/`set_profile()`."""

    max_turns_per_topic: int
    rhythm: str
    response_length: str
    safety_mode: str
    profile_style: str


class AgendaMetrics(BaseModel):
    """GET /api/agenda counts (Tier C, R8-CRITICAL).

    Explicit field whitelist of `KiraAgendaController.get_metrics()` —
    COUNTS/STATE ONLY. `get_metrics()` never includes `rejection_log`
    itself (which carries `matched_text`, i.e. raw viewer phrases); this
    model must never grow a field sourced from that log's raw text.
    """

    total_rejections: int
    by_error_code: dict[str, int]
    by_guardrail: dict[str, int]
    avg_similarity_overlap_pct: Optional[float]
    current_state: str
    failure_count: int
    response_length: str
    active_topic: Optional[str]
    topics_queued: int
    last_outputs_count: int


class AgendaResponse(BaseModel):
    """GET/POST/PUT /api/agenda* shared response shape (Tier C, WS3 slice 3)."""

    state: str
    active_topic: Optional[AgendaTopicOut]
    queued_topics: list[AgendaTopicOut]
    drafted_topics: list[AgendaTopicOut]
    session_settings: AgendaSessionSettings
    metrics: AgendaMetrics


class AgendaTopicRequest(BaseModel):
    """POST /api/agenda/topic body."""

    title: str
    angle: Optional[str] = ""


class AgendaProfileRequest(BaseModel):
    style: Optional[str] = None


class AgendaTopicActionRequest(BaseModel):
    """POST /api/agenda/topic/action body.

    `action` is checked against a server-side whitelist in main.py (mirrors
    `CommandRequest.command`) — accepts any string so the handler controls
    the 422 body shape. `direction` is only consumed by `move` (>=0 means
    move later/down, <0 means move earlier/up — passed straight through to
    `move_queued_topic`).
    """

    action: str
    topic_id: str
    direction: Optional[int] = None


class AgendaSessionRequest(BaseModel):
    """PUT /api/agenda/session body — a partial update.

    Every field optional; an omitted field leaves the stored value
    unchanged (mirrors `ObsConfigRequest`).
    """

    profile: Optional[AgendaProfileRequest] = None
    max_turns_per_topic: Optional[int] = None
    rhythm: Optional[str] = None
    response_length: Optional[str] = None
    safety_mode: Optional[str] = None


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
