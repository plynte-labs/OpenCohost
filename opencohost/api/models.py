"""Pydantic wire models for the Kira FastAPI API layer (Phase 1).

Field names on `StatusResponse` are pinned to the `is_`-prefixed forms
(design v2.1 A-M1) to match the FE wire contract (spec R1) — NOT the
shortened `ready`/`speaking`/`processing` forms.
"""

from typing import Annotated, Optional

from pydantic import BaseModel, StringConstraints

# Agent provenance name — REQUIRED non-blank in every agent write (design
# 'Provenance'). Blank/whitespace would be stored as '' — which
# editorial_cards defines as OPERATOR provenance — so all /api/agent/*
# write models share this 422-at-the-boundary constraint.
AgentName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


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
    # Stable profile UUID (R12) mirrored from MotorVocalIA._current_profile_id
    # so the FE can tag /api/memoria/* calls with the id memoria rows are
    # actually stored under (profile_id column), not the display name. None
    # until the first profile switch — the engine does not seed this at
    # startup for the initial "default" profile (see main.py get_status).
    active_profile_id: Optional[str] = None
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
    # FIX-B: real OBS websocket connection state from the API-hosted
    # ObsRuntime. None when no runtime exists (construction failed, or a test
    # host double without one) — the FE treats None as "unknown", not "down".
    obs_connected: Optional[bool] = None


class ProfilesListResponse(BaseModel):
    """Profile NAMES only — never persona text, prompts, or other fields."""

    profiles: list[str]


class SwitchProfileRequest(BaseModel):
    name: str
    idempotency_key: Optional[str] = None


class ProfileDetailResponse(BaseModel):
    """GET /api/perfiles/{name} response (R8/D5).

    `id` is included so the FE can target this (possibly non-active)
    profile's memoria rows, which are stored keyed by the stable uuid `id`,
    not the display name. prompt/use_system/id ONLY — never chat content or
    any other field. The handler builds this from explicit field picks
    (never `**data`), so a new persisted field can never leak through this
    endpoint.
    """

    name: str
    id: str
    prompt: str
    use_system: bool
    # D7 (kira_bilingual_e2e): data-only locale declaration for the T4
    # coherence gate (opencohost/i18n/coherence.py). None means "ungoverned"
    # (today's PROFILE_PERSONA_UNGOVERNED behavior) — profile always wins.
    locale: Optional[str] = None


class ProfileCreateRequest(BaseModel):
    """POST /api/perfiles body — create a new profile. A stable `id` is
    minted server-side (never accepted from the wire)."""

    name: str
    prompt: str
    use_system: bool = False
    locale: Optional[str] = None


class ProfileUpdateRequest(BaseModel):
    """PUT /api/perfiles/{name} body — a partial update.

    Every field optional; an omitted field leaves the stored value unchanged.
    `new_name` renames the profile while preserving its stable on-disk `id`
    (R12).
    """

    new_name: Optional[str] = None
    prompt: Optional[str] = None
    use_system: Optional[bool] = None
    locale: Optional[str] = None


class I18nBundleInfo(BaseModel):
    """One entry of GET /api/i18n's `available` list (design §7.1).

    Mirrors a discovered locale bundle's `meta` section — `display`/`status`
    come straight from the manifest (`opencohost/locales/{code}/manifest.yaml`),
    `tier` is the registry's authoritative (source-directory-stamped) tier,
    never the manifest's own claim (security boundary, registry.py:8-10)."""

    code: str
    display: str
    tier: str
    status: str


class I18nWarning(BaseModel):
    """One `opencohost.i18n.coherence.CoherenceWarning` (stable `code`, human `message`)."""

    code: str
    message: str


class I18nStateResponse(BaseModel):
    """GET/PUT /api/i18n response (design §7.1).

    `active_locale` is what THIS running process loaded at startup;
    `persisted_locale` is what the NEXT start will load (D6: next-boot only,
    no hot-swap). `pending_restart` is true when they disagree — the config
    surface (API/CTK/Tauri) uses it to show a "restart required" state."""

    active_locale: str
    persisted_locale: str
    pending_restart: bool
    available: list[I18nBundleInfo]
    warnings: list[I18nWarning]


class I18nSetLocaleRequest(BaseModel):
    """PUT /api/i18n body. `locale` is normalized (BCP-47) and matched
    against the discovered bundles server-side; unmatched -> 422."""

    locale: str


class PersonalizationResponse(BaseModel):
    """GET/PUT/DELETE /api/personalization response (design §4).

    Explicit field picks only — never `**data` — mirrors
    `ProfileDetailResponse`: a future persisted field (e.g. the internal
    `version` key) can never leak through this endpoint.
    """

    enabled: bool
    nickname: str
    occupation: str
    interests: str
    custom_instructions: str
    updated_at: Optional[str]


class PersonalizationUpdateRequest(BaseModel):
    """PUT /api/personalization body — a partial update.

    Every field optional; an omitted field leaves the stored value
    unchanged. Mirrors `ProfileUpdateRequest`.
    """

    enabled: Optional[bool] = None
    nickname: Optional[str] = None
    occupation: Optional[str] = None
    interests: Optional[str] = None
    custom_instructions: Optional[str] = None


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


class EventOut(BaseModel):
    """One entry in GET /api/events?since=cursor (item B).

    PRIVACY: `source`/`action` are drawn from EngineHost's closed
    `_MOTOR_EVENT_WHITELIST` (engine_host.py) -- never free text. `detail`
    is reserved for schema parity with the client-side event bus (item A,
    appEvents.ts) but is always null today; it must never carry dialogue,
    chat, or request/response bodies if a future caller populates it.
    """

    seq: int
    ts: float
    source: str
    action: str
    detail: Optional[str] = None


class EventLogResponse(BaseModel):
    """GET /api/events?since=cursor (item B).

    `cursor` is the sink's current max `seq` -- callers poll again with
    `since=cursor`. `boot` changes across a process restart (`seq` resets
    to 0 too); a client must reset its own `since` to 0 when `boot` changes,
    or a stale (too-high) `since` silently returns an empty backfill
    forever (see EventLogSink docstring, engine_host.py).
    """

    events: list[EventOut]
    cursor: int
    boot: float


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
    and the valid `tracks` in the bucket, then plays client-side. When the
    requested mood's bucket is empty, `tracks` is populated with the same
    normal->any fallback pool and `fallback` is True so the client can rotate
    instead of always replaying `suggested_track_id`."""

    active_mood: str
    tracks: list[MusicTrackOut]
    suggested_track_id: Optional[str]
    fallback: bool = False


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
    # FIX-C: POST /api/agenda/session/action outcome flags. `applied` is False
    # with `reason="empty_queue"` when `enable` is refused for an empty queue
    # (CTK parity), or `reason="guardrails_missing"` when the controller's
    # fail-closed gate refuses (state stays OFF); every other agenda response
    # leaves both null.
    applied: Optional[bool] = None
    reason: Optional[str] = None


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
    # FIX-A: `saved_memorias`/`pinned` are per-profile counts when the request
    # carries `profile_id` (so the FE headline agrees with the per-profile
    # list); they fall back to the global totals when it does not.
    saved_memorias: int
    pinned: int
    # Global totals, always present regardless of `profile_id` — kept as
    # separate fields for compatibility and honest "N of total" surfacing.
    saved_memorias_total: int = 0
    pinned_total: int = 0
    editorial_cards_by_status: dict[str, int]


class MemoriaListItem(BaseModel):
    """One row of GET /api/memoria/list (Tier B).

    WU-H (operator viewing decision, 2026-07-05): `title` is now included in
    the list projection — a deliberate, scoped relaxation of the prior
    metadata-only rule so the operator can recognize a row before deciding
    to load it. `content` is still deliberately excluded, and the endpoint's
    SQL SELECT never reads that column off disk in the first place (defense
    in depth, not just field omission here) — content is only readable one
    row at a time via GET /api/memoria/row/{id}. R8 (raw viewer chat never
    leaves the API) is unaffected: memoria title/content is Kira's
    curated/derived memory, not raw chat.
    """

    id: str
    title: str
    created_at: str
    updated_at: str
    revision: int
    pinned: bool
    private: bool
    inactive: bool
    # memoria_import_20260718 (WU3): True when status='imported' — an
    # externally-imported row, so the UI can render the "importada" badge
    # next to pinned/private/inactive. Metadata-only; no content exposure.
    imported: bool


class MemoriaListResponse(BaseModel):
    """GET /api/memoria/list response body."""

    items: list[MemoriaListItem]


class MemoriaRowResponse(BaseModel):
    """GET /api/memoria/row/{id} response body (WU-H, operator viewing decision).

    The ONLY memoria surface that returns `content` — on-demand, one row at a
    time, never preloaded alongside GET /api/memoria/list. Mirrors the
    ownership-guard shape used by POST /api/memoria/{delete,update}: the
    handler's store.get(raising=True) pre-check owns the 404 for a
    missing/wrong-profile row (R7: identical 404 body either way).
    """

    id: str
    title: str
    content: str
    created_at: str
    updated_at: str
    pinned: bool
    private: bool
    inactive: bool


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


class MemoriaImportRequest(BaseModel):
    """POST /api/memoria/import body — a bounded external-AI export (memoria_
    import_20260718).

    `content` is the raw markdown/plain-text export (parsed server-side into
    per-profile 'imported' rows); `source_label` is a short provenance tag
    ("Gemini", "ChatGPT", …) that becomes the row title prefix. R8: content
    flows INBOUND only; the response is counts-only and never echoes it.
    The route enforces the 64 KB / 100-item / 40-char-label / import-cap
    trust-boundary bounds before any write.
    """

    profile_id: str
    source_label: str
    content: str


class MemoriaImportResponse(BaseModel):
    """POST /api/memoria/import response — counts ONLY (R8), one field per
    per-item outcome of the four-state insert_imported contract.

    `imported` = fresh rows created; `skipped_duplicates` = ON-CONFLICT
    collisions (already present, nothing overwritten); `skipped_too_short` =
    claims with too few significant tokens to store; `skipped_cap` = creatable
    claims left unattempted because the profile's import cap headroom was
    exhausted in-loop (D6 — only `created` rows consume headroom); `failed` =
    fail-open store errors (a lock loss is surfaced honestly here, NEVER counted
    as a duplicate — R4). `ok` is False for the disabled no-op or when any item
    failed to write.
    """

    ok: bool
    imported: int
    skipped_duplicates: int
    skipped_too_short: int
    skipped_cap: int
    failed: int


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


class AgentTopicRequest(BaseModel):
    """POST /api/agent/topics body (agent_context_gateway Phase 2).

    Maps 1:1 to ``TopicInboxStore.propose(title, angle, tags, source=agent)``
    — the store owns ALL content validation (TITLE_MAX=120, ANGLE_MAX=600,
    TAGS_MAX=8x40, SOURCE_MAX=80, code/HTML rejection), so this model stays
    permissive and the handler surfaces the store's own error string as the
    422 detail (mirrors the ``CommandRequest`` whitelist pattern). ``agent``
    is the self-reported provenance name stored in ``topic_inbox.source``
    and rendered to the operator in the inbox UI.
    """

    agent: AgentName
    title: str
    angle: str = ""
    tags: list[str] = []


class AgentTopicResponse(BaseModel):
    """POST /api/agent/topics response.

    Owner decision D1: a proposal lands in the human-gated topic inbox as
    'proposed' — never in the agenda queue. ``deduped=True`` means the title
    slug matched an existing proposed row, which was updated in place: the
    store's dedupe-upsert IS the idempotency contract (repeat POSTs are
    safe, no Idempotency-Key header).
    """

    id: str
    status: str
    deduped: bool


class AgentStatusResponse(BaseModel):
    """GET /api/agent/status (read-only, any valid token).

    COUNTS ONLY — never add a field carrying topic/card/notice text.
    ``notices_undismissed`` counts agent_notices rows still 'proposed'
    (wired in Phase 3).
    """

    topics_pending: int
    cards: dict[str, int]
    notices_undismissed: int


class AgentCardRequest(BaseModel):
    """POST /api/agent/cards body (agent_context_gateway Phase 3).

    Maps to ``EditorialCardStore.upsert`` with the ``EditorialCard``
    dataclass's own validation (SUMMARY_MAX=1200, STREAMER_TAKE_MAX=800,
    TOPIC_MAX=120, ITEM_MAX=240x8, raw_chat/raw_page rejection), so this
    model stays permissive and the handler surfaces the dataclass's error
    string as the 422 detail. ``agent`` is the self-reported provenance
    name written to the ``editorial_cards.origin`` column ('' = operator).
    ``expires_at`` is an optional ISO-8601 timestamp.
    """

    agent: AgentName
    topic: str
    summary: str
    streamer_take: str
    counterpoints: list[str] = []
    discussion_hooks: list[str] = []
    triggers: list[str] = []
    expires_at: str | None = None


class AgentCardResponse(BaseModel):
    """POST /api/agent/cards response.

    Demotion rule (design 'POST /api/agent/cards'): a new card always lands
    DRAFT; when the upsert hit an existing ARMED/ACTIVE card it is set back
    to DRAFT and ``demoted`` is true — the operator must re-arm via CLI/UI
    before the rewritten content can auto-fire again.
    """

    id: str
    topic_slug: str
    status: str
    demoted: bool


class AgentCardListItem(BaseModel):
    """One row of GET /api/agent/cards — light projection (design D4): no
    summary/streamer_take/list fields, the UI never needs card bodies here."""

    id: str
    topic: str
    topic_slug: str
    status: str
    origin: str
    expires_at: str | None
    updated_at: str


class AgentCardsListResponse(BaseModel):
    cards: list[AgentCardListItem]


class AgentCardArmResponse(BaseModel):
    id: str
    status: str


class AgentNoticeRequest(BaseModel):
    """POST /api/agent/notice body — a short agent-to-operator message.

    ``AgentNoticeStore`` owns validation (text <= 280, source <= 80,
    code/HTML rejection); the handler surfaces its error string as the
    422 detail.
    """

    agent: AgentName
    text: str


class AgentNoticeResponse(BaseModel):
    """POST /api/agent/notice response. ``deduped=True`` means an identical
    (source, text) pair already existed among undismissed notices and that
    row was returned — repeat POSTs are safe (natural idempotency)."""

    id: str
    deduped: bool


class AgentNoticeOut(BaseModel):
    """One undismissed notice as shown to the operator."""

    id: str
    text: str
    source: str
    created_at: str


class AgentNoticesResponse(BaseModel):
    """GET /api/agent/notices response (operator surface). Only rows that
    pass read-time validation are surfaced — hostile direct-DB rows stay
    quarantined in the store's invalid bucket."""

    notices: list[AgentNoticeOut]


class AgentNoticeDismissResponse(BaseModel):
    """POST /api/agent/notices/{id}/dismiss response (operator surface)."""

    dismissed: bool


# ──────────────────────────────────────────────────────────────────────────
# Push-to-Talk (liveaudio_ptt_tauri_20260710)
#
# PRIVACY (hard rule 2): NONE of these models carries transcript text. The
# operator dictation travels WhisperLive WS -> PttSession buffer (RAM) ->
# process_context dispatch and NEVER crosses HTTP. ``buffered_chars`` is an int
# count only; ``last_error`` is a fixed enum-like string (stt_unreachable|
# stt_lost); ``state`` is a lifecycle literal. The Tauri client literally never
# receives the transcript, so it cannot leak client-side.
# ──────────────────────────────────────────────────────────────────────────


class PttKeepaliveRequest(BaseModel):
    """POST /api/ptt/keepalive body. The client posts this ~1 Hz while the key
    is held; the stream IS the server-side proof-of-life (guillotine)."""

    session_id: str


class PttStopRequest(BaseModel):
    """POST /api/ptt/stop body. ``session_id`` is optional — stop is fully
    idempotent, so an unknown/absent id still returns 200."""

    session_id: Optional[str] = None


class PttStartResponse(BaseModel):
    session_id: str
    state: str


class PttKeepaliveResponse(BaseModel):
    """State rides the keepalive so the holding client needs no extra poll.
    ``buffered_chars`` is a count, NEVER the text."""

    session_id: str
    state: str
    buffered_chars: int


class PttStopResponse(BaseModel):
    state: str


class PttStateResponse(BaseModel):
    """GET /api/ptt/state. ``buffered_chars`` is a count, NEVER the text.

    ``stt_ws_url`` is the LiveAudio/WhisperLive viewer WebSocket URL the
    backend itself consumes — exposed so the webview can open its OWN
    recv-only connection (the OBS-browser-source pattern) for the transcript
    echo. A URL/port is not transcript text; the privacy rule is untouched.
    """

    state: str
    session_id: Optional[str] = None
    buffered_chars: int = 0
    last_error: Optional[str] = None
    stt_ws_url: Optional[str] = None
