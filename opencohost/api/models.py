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
    # S4 (P2): eager-wake warming visibility — True while the daemon-thread
    # `EngineHost._wake_ollama_eager` wake is still waiting on Ollama.
    ollama_warming: bool = False
    # F4: coarse avatar/pipeline state for the FE avatar view. The motor
    # (MotorVocalIA) does NOT expose the UI-layer AvatarStateBridge, so this
    # is DERIVED from is_speaking/is_processing/is_ready in the /api/status
    # handler. Only "speaking"/"thinking"/"idle"/"sleeping" can be produced
    # here; the richer states (listening/speaking_alt/angry) live solely in
    # app_shell's bridge and are not reachable from the API host.
    avatar_state: str = "idle"


class ProfilesListResponse(BaseModel):
    """Profile NAMES only — never persona text, prompts, or other fields."""

    profiles: list[str]


class SwitchProfileRequest(BaseModel):
    name: str
    idempotency_key: Optional[str] = None


class ProfileDetailResponse(BaseModel):
    """GET /api/perfiles/{name} response (R8/D5).

    prompt + use_system ONLY — never the stored `id` or any other field. The
    handler builds this from explicit field picks (never `**data`), so a new
    persisted field can never leak through this endpoint.
    """

    name: str
    prompt: str
    use_system: bool


class ProfileCreateRequest(BaseModel):
    """POST /api/perfiles body — create a new profile. A stable `id` is
    minted server-side (never accepted from the wire)."""

    name: str
    prompt: str
    use_system: bool = False


class ProfileUpdateRequest(BaseModel):
    """PUT /api/perfiles/{name} body — a partial update.

    Every field optional; an omitted field leaves the stored value unchanged.
    `new_name` renames the profile while preserving its stable on-disk `id`
    (R12).
    """

    new_name: Optional[str] = None
    prompt: Optional[str] = None
    use_system: Optional[bool] = None


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


class ChatLastReplyResponse(BaseModel):
    """GET /api/chat/last-reply (P3, R8-safe).

    Surfaces Kira's OWN generated reply text ONLY — mirrors the `_Drain` /
    `ChatReplySink` privacy contract in engine_host.py. This is NOT the
    viewer/operator chat that triggered the reply; that text never crosses
    HTTP (R8). Before any turn: `{text: null, source: null, turn_id: 0,
    ts: null}`.
    """

    text: Optional[str]
    source: Optional[str]
    turn_id: int
    ts: Optional[float]


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


class MusicTrackOut(BaseModel):
    """Mirrors `opencohost.core.music_library.MusicTrack` (READ-ONLY slice,
    WS3 slice 4). `status` is derived, never the raw `missing`/`invalid`
    booleans: ok (file present + valid signature), faltante (path gone),
    invalido (file present but fails the audio-signature check).
    """

    id: str
    label: str
    mood: str
    status: str


class MusicLibraryResponse(BaseModel):
    """GET /api/music/library (Tier B, direct read-only, no queue, no audio).

    Server-side audio playback (`request_mood`) is deferred — this is a
    pure library listing.
    """

    tracks: list[MusicTrackOut]
    count: int
    moods: list[str]


class MusicMoodRequest(BaseModel):
    """POST /api/music/mood body. `mood` is case/whitespace-normalized then
    checked against KNOWN_MOODS in main.py (422 on unknown) — a stricter
    contract than music_library.normalize_mood's silent fallback to 'normal'.
    """

    mood: str


class MusicFadeOut(BaseModel):
    """A recorded fade INTENT (client-side-playback model). Populated by WU2's
    POST /api/music/fade; carried on MusicStateResponse.fade so a polling
    client executes the fade when it sees a `seq` greater than its last."""

    direction: str  # "in" | "out"
    duration_ms: int
    seq: int
    ts: float


class MusicFadeRequest(BaseModel):
    """POST /api/music/fade body. `direction` ('in'|'out') is checked against
    _MUSIC_FADE_DIRECTIONS in main.py (422 on unknown). `duration_ms` defaults
    server-side to _MUSIC_FADE_DEFAULT_MS when omitted, then range-capped to
    (0, _MUSIC_FADE_MAX_MS] — the fade is an INTENT the client executes."""

    direction: str
    duration_ms: Optional[int] = None


class MusicStateResponse(BaseModel):
    """GET /api/music/state (and the POST mood/fade responses). Orchestration
    state ONLY (music-orchestration-model): the API never plays audio; the
    client reads this and plays. `fade` is None until a fade intent is set."""

    active_mood: str
    fade: Optional[MusicFadeOut] = None


class MusicMoodResponse(BaseModel):
    """POST /api/music/mood response. No playback/audio field — the client
    reads `suggested_track_id` (select_for_mood's mood->normal->any fallback)
    and the valid `tracks` in the bucket, then plays client-side."""

    active_mood: str
    tracks: list[MusicTrackOut]
    suggested_track_id: Optional[str]


class MusicImportRequest(BaseModel):
    """POST /api/music/import body (WU3). `path` is an absolute local audio
    file the server copies into the managed library dir; `mood` is
    case/whitespace-normalized then checked against KNOWN_MOODS (422 on
    unknown). Client and server share a filesystem (local-first product)."""

    path: str
    mood: str


class MusicImportResponse(BaseModel):
    """POST /api/music/import response. Orchestration fields only — the nested
    `track` carries NO filesystem `path` (resolution 2914: audio is delivered
    via a FileResponse endpoint, so the raw path is never leaked)."""

    track: MusicTrackOut


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
    """POST /api/agenda/topic body.

    `constraints`/`priority`/`response_length` are sanitized/normalized by
    `KiraAgendaController.add_topic` — an unknown priority/response_length
    silently normalizes to "normal" (controller contract, no 422). A
    constraint whose text fails `sanitize_topic_text` (emoji/code/too long)
    raises ValueError, surfaced as 422 by the existing handler path.
    """

    title: str
    angle: Optional[str] = ""
    constraints: Optional[list[str]] = None
    priority: Optional[str] = None
    response_length: Optional[str] = None


class AgendaProfileRequest(BaseModel):
    style: Optional[str] = None


class AgendaTopicActionRequest(BaseModel):
    """POST /api/agenda/topic/action body.

    `action` is checked against a server-side whitelist in main.py (mirrors
    `CommandRequest.command`) — accepts any string so the handler controls
    the 422 body shape. Whitelisted verbs: approve, queue, remove, move,
    reject. `direction` is only consumed by `move` (>=0 means move later/down,
    <0 means move earlier/up — passed straight through to `move_queued_topic`).
    """

    action: str
    topic_id: str
    direction: Optional[int] = None


class AgendaSessionActionRequest(BaseModel):
    """POST /api/agenda/session/action body.

    `action` is checked against a server-side whitelist in main.py (mirrors
    `AgendaTopicActionRequest`) — accepts any string so the handler controls
    the 422 body shape rather than pydantic's enum-validation 422.
    """

    action: str


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


class MemoriaListItem(BaseModel):
    """One row of GET /api/memoria/list (Tier B, R8-CRITICAL).

    METADATA ONLY — mirrors MemoriaStatsResponse's contract. Deliberately
    has no `title`/`content` field, and the endpoint's SQL SELECT never
    reads those columns in the first place (defense in depth, not just
    field omission here).
    """

    id: str
    created_at: str
    updated_at: str
    revision: int
    pinned: bool
    private: bool
    inactive: bool


class MemoriaListResponse(BaseModel):
    """GET /api/memoria/list response body."""

    items: list[MemoriaListItem]


class MemoriaFlagsRequest(BaseModel):
    """POST /api/memoria/flags body — a partial flag update.

    Every flag is optional; an omitted flag is left unchanged. The handler
    rejects a body with all three omitted (422) rather than issuing a no-op
    write. F5: pin/private promote status='curated' in the store (same
    statement); un-pin never demotes; `inactive` never touches status.
    """

    profile_id: str
    id: str
    pinned: Optional[bool] = None
    private: Optional[bool] = None
    inactive: Optional[bool] = None


class MemoriaDeleteRequest(BaseModel):
    """POST /api/memoria/delete body — hard-delete one row by id.

    Reuses MemoriaMutationResponse. The handler's store.get pre-check owns
    the 404 for a missing/wrong-profile row, so delete_row's False can only
    mean a genuine write failure (503), never "already gone".
    """

    profile_id: str
    id: str


class MemoriaUpdateRequest(BaseModel):
    """POST /api/memoria/update body — operator edit of one row's text.

    R8 note: title/content flow INBOUND only; the response never echoes them
    (MemoriaMutationResponse is ok-only) and read-back stays metadata-only via
    /api/memoria/list. The handler strips + length-caps both before the write;
    update_row then whitespace-normalizes and promotes status='curated' (F5).
    """

    profile_id: str
    id: str
    title: str
    content: str


class MemoriaCaptureRequest(BaseModel):
    """POST /api/memoria/capture body — session-scoped capture-privacy toggle.

    `paused=True` pauses auto-capture; `False` resumes it. The handler calls
    `host.motor.set_memorias_private(paused)` DIRECTLY (mirrors the CTK path in
    inspector_memory.py), NOT via /api/commands (design resolution 2905/D3).
    """

    paused: bool


class MemoriaMutationResponse(BaseModel):
    """POST /api/memoria/{flags,delete,update} response. R8: never echoes
    title/content — read-back stays metadata-only via /api/memoria/list."""

    ok: bool


class MemoriaNoticeResponse(BaseModel):
    """GET/POST /api/memoria/notice response — F1 disclosure-banner state.

    `dismissed=False` means the operator has not yet dismissed the banner, so
    it shows. Fails open to False (banner shows) when the flag is absent.
    """

    dismissed: bool


class MemoriaNoticeRequest(BaseModel):
    """POST /api/memoria/notice body — sets the F1 disclosure-banner state."""

    dismissed: bool


class CohostProfileOut(BaseModel):
    """One cohost profile as returned by GET/POST /api/agenda/cohost-profiles."""

    name: str
    style: str
    default_priority: str
    default_response_length: str


class CohostProfilesResponse(BaseModel):
    """GET/POST /api/agenda/cohost-profiles response.

    NO `selected` field by design: profile selection is stateless (RAM-only on
    the running controller), never persisted. GET returns the on-disk profiles;
    clients default to "Natural" (CTK parity).
    """

    profiles: list[CohostProfileOut]


class CohostProfileSaveRequest(BaseModel):
    """POST /api/agenda/cohost-profiles body. `priority`/`length` map to the
    stored default_priority/default_response_length (CTK save-form parity)."""

    name: str
    style: str
    priority: Optional[str] = None
    length: Optional[str] = None


class CohostProfileSelectRequest(BaseModel):
    """POST /api/agenda/cohost-profiles/select body."""

    name: str


class CohostProfileSelectResponse(BaseModel):
    """POST /api/agenda/cohost-profiles/select response — RAM-only apply."""

    selected: str


class MemoriaPurgeRequest(BaseModel):
    """POST /api/memoria/purge body."""

    profile_id: str


class MemoriaPurgeResponse(BaseModel):
    """POST /api/memoria/purge response body."""

    deleted: int
