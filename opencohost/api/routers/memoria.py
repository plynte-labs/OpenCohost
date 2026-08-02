"""/api/memoria/* -- stats, list, row, purge, flags, delete, update, import,
capture, notice (moved verbatim from main.py, refactor_core_api_20260802 B6).

`MEMORIAS_DB`, `MEMORIAS_ENABLED`, `MEMORIAS_IMPORT_CAP` ARE monkeypatched
directly on `opencohost.api.main` (test_api_memoria_*.py, test_api_reads.py,
test_api_write_failures.py -- every memoria test isolates its own db path and
flips the feature flag), so every read goes through `deps.memorias_db()` /
`deps.memorias_enabled()` / `deps.memorias_import_cap()`. `EDITORIAL_CARDS_DB`
is ALSO monkeypatched (test_api_reads.py, for GET /api/memoria/stats) and
already has `deps.editorial_cards_db()` from the B5 agent.py move, reused
here. `cargar_perfiles` (called by `_legacy_profile_key`) is monkeypatched
too (test_api_phase1.py, test_api_memoria_list_legacy_rekey.py) and already
has `deps.cargar_perfiles()` from the B5 perfiles.py move.

The shared MemoriaStore singleton -- `_memoria_store`, `_memoria_store_lock`,
`_get_memoria_store()`, `_memoria_store_or_none()` -- STAYS in
`opencohost.api.main`, NOT here: tests reset it with a bare `main_mod.
_memoria_store = None` autouse fixture AND monkeypatch `main_mod.
_get_memoria_store` / `main_mod._memoria_store_or_none` directly
(test_api_memoria_row.py, test_api_write_failures.py) -- both only work if
the module-level global and the functions that close over it stay put. This
router reads it through `deps.memoria_store_or_none()`, a new accessor
mirroring the existing late-import idiom.

`MEMORIAS_IMPORT_MAX_BYTES`/`MEMORIAS_IMPORT_MAX_ITEMS` (settings.py),
`load_memorias_notice_dismissed`/`save_memorias_notice_dismissed`
(settings.py), and `parse_import`/`strip_control_chars`
(core.memoria_import)/`build_signature`/`build_title`/`derive_import_key`/
`is_capturable` (core.memoria_store) are never monkeypatched (confirmed by
grep), so they import directly here. `_list_memoria_metadata`,
`_purge_memoria`, `_legacy_profile_key`, `_rekey_legacy_memorias`, and the
`_MEMORIA_WRITE_TIMEOUT_SECONDS`/`_MEMORIA_IMPORT_LABEL_MAX_LENGTH`/
`_MAX_CONSECUTIVE_IMPORT_ERRORS` constants are used ONLY by this family, so
they relocate here wholesale. `_MEMORIA_TITLE_MAX_LENGTH`/
`_MEMORIA_CONTENT_MAX_LENGTH` are ALSO memoria-only but
test_api_memoria_mutations.py reads them directly as `main_mod.
_MEMORIA_TITLE_MAX_LENGTH` / `main_mod._MEMORIA_CONTENT_MAX_LENGTH` -- so
they live in `opencohost.api.shared` instead (never-monkeypatched, main.py
re-exports both, same reasoning as `_OBS_TEST_TIMEOUT_SECONDS`).

CAUTION (path ambiguity, verified during the move): every literal path here
(`stats|list|purge|flags|delete|update|import|capture|notice`) sits at
segment count 3 after `/api/`; the one template, `/api/memoria/row/
{row_id}`, sits at segment count 4 (`api`, `memoria`, `row`, `{row_id}`) --
no literal path shares that count, so there is no ambiguity and relative
registration order is cosmetic (same reasoning documented in
routers/music.py/perfiles.py for their own template/literal mixes).

Endpoint count note: proposal.md's target layout estimated this family at
"(10)"; the actual inline surface in main.py was 11 decorators over 10
unique paths (GET and POST /api/memoria/notice share one path) -- all 11
move here verbatim.
"""

import os
import sqlite3
from contextlib import closing
from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from opencohost.api import deps
from opencohost.api.models import (
    MemoriaCaptureRequest,
    MemoriaDeleteRequest,
    MemoriaFlagsRequest,
    MemoriaImportRequest,
    MemoriaImportResponse,
    MemoriaListItem,
    MemoriaListResponse,
    MemoriaMutationResponse,
    MemoriaNoticeRequest,
    MemoriaNoticeResponse,
    MemoriaPurgeRequest,
    MemoriaPurgeResponse,
    MemoriaRowResponse,
    MemoriaStatsResponse,
    MemoriaUpdateRequest,
)
from opencohost.api.shared import (
    _MEMORIA_CONTENT_MAX_LENGTH,
    _MEMORIA_TITLE_MAX_LENGTH,
    _STATS_DB_READ_TIMEOUT_SECONDS,
    _count_sql,
    _editorial_cards_by_status,
    logger,
)
from opencohost.config.settings import (
    MEMORIAS_IMPORT_MAX_BYTES,
    MEMORIAS_IMPORT_MAX_ITEMS,
    load_memorias_notice_dismissed,
    save_memorias_notice_dismissed,
)
from opencohost.core.memoria_import import parse_import, strip_control_chars
from opencohost.core.memoria_store import build_signature, build_title, derive_import_key, is_capturable

router = APIRouter()

# POST /api/memoria/purge -- mirrors memoria_store.py's WRITE_TIMEOUT_SECONDS.
_MEMORIA_WRITE_TIMEOUT_SECONDS = 1.0

# POST /api/memoria/import source-label cap (memoria_import_20260718). The
# label becomes the row title prefix `[{label}] `; 40 chars matches the
# client-side <Input maxLength> and bounds the untrusted provenance tag.
_MEMORIA_IMPORT_LABEL_MAX_LENGTH = 40

# POST /api/memoria/import early-abort threshold: after this many CONSECUTIVE
# insert_imported 'error' outcomes the DB is provably unavailable for this
# request, so the remaining items are not attempted and count into `failed`
# (R4) -- bounds a locked-DB import to ~3 * WRITE_TIMEOUT_SECONDS, not ~100s.
_MAX_CONSECUTIVE_IMPORT_ERRORS = 3


def _list_memoria_metadata(db_path: str, profile_id: str) -> list[dict]:
    """Bounded, fail-open metadata read for GET /api/memoria/list.

    WU-H (operator viewing decision, 2026-07-05): the SELECT now ALSO reads
    `title` -- a deliberate, scoped relaxation of the prior metadata-only
    rule so the operator can recognize a row before deciding to load it.
    `content` still stays off this SELECT entirely; it is only readable one
    row at a time via GET /api/memoria/row/{id} (R8 unaffected -- memoria
    title/content is Kira's curated/derived memory, not raw viewer chat)."""
    if not db_path or not os.path.exists(db_path):
        return []
    try:
        with closing(sqlite3.connect(db_path, timeout=_STATS_DB_READ_TIMEOUT_SECONDS)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT id, title, created_at, updated_at, revision, pinned, private, inactive, status "
                "FROM memorias WHERE profile_id = ? ORDER BY updated_at DESC, id ASC",
                (profile_id,),
            ).fetchall()
            return [dict(row) for row in rows]
    except sqlite3.Error:
        return []


def _purge_memoria(db_path: str, profile_id: str) -> int:
    """Bounded, fail-open hard-delete for POST /api/memoria/purge."""
    if not db_path or not os.path.exists(db_path):
        return 0
    try:
        with closing(sqlite3.connect(db_path, timeout=_MEMORIA_WRITE_TIMEOUT_SECONDS)) as conn, conn:
            cur = conn.execute("DELETE FROM memorias WHERE profile_id = ?", (profile_id,))
            return cur.rowcount
    except sqlite3.Error:
        return 0


def _legacy_profile_key(profile_id: str) -> "str | None":
    """The profile's current display NAME whose stable `id` == profile_id.

    Memorias were historically saved keyed by the display name; the FE now
    lists by the stable UUID, so the name is the legacy key we must migrate
    from. Returns None when no profile carries this id (a deleted profile --
    nothing to migrate) or when the resolved name would equal the UUID (never
    the case in practice; guards against a pointless self-re-key). Renamed
    profiles whose legacy rows sit under an OLD name are unreachable here --
    `deps.cargar_perfiles()` only knows the current name -- and are out of
    scope.
    """
    perfiles = deps.cargar_perfiles()
    if not isinstance(perfiles, dict):
        return None
    for name, data in perfiles.items():
        if isinstance(data, dict) and data.get("id") == profile_id and name != profile_id:
            return name
    return None


def _rekey_legacy_memorias(db_path: str, legacy_key: str, profile_id: str) -> int:
    """Migrate-on-read: point legacy name-keyed rows at the profile's UUID.

    Additive re-key of the profile_id column only (zero deletions), scoped to
    legacy_key -- the requested profile's current display name -- so a
    different profile's name-keyed rows are never swallowed. One bounded
    transaction; idempotent (once re-keyed the name rows are gone, so the
    next call updates nothing). Returns the rows re-keyed, fail-open to 0 on
    a missing db or any sqlite error.

    stable_key is deliberately left untouched: it embeds the old name
    (`{name}|tokens`), but rewriting it to `{uuid}|tokens` could collide with
    a row already captured fresh under the UUID and roll the WHOLE migration
    back -- leaving the reported bug (invisible legacy rows) unfixed. Keeping
    the stale prefix keeps every row's (profile_id, stable_key) pair unique,
    so the UPDATE never rolls back. ponytail: the only residual cost is that
    a future auto-capture of identical content derives a fresh
    `{uuid}|tokens` key and inserts a duplicate draft (self-heals via the
    growth-cap prune for drafts); rewrite stable_key too only if duplicate
    curated rows become a real problem.
    """
    if not db_path or not os.path.exists(db_path) or not legacy_key or legacy_key == profile_id:
        return 0
    try:
        with closing(sqlite3.connect(db_path, timeout=_MEMORIA_WRITE_TIMEOUT_SECONDS)) as conn, conn:
            cur = conn.execute(
                "UPDATE memorias SET profile_id = ? WHERE profile_id = ?",
                (profile_id, legacy_key),
            )
            return cur.rowcount
    except sqlite3.Error:
        return 0


@router.get("/api/memoria/stats", response_model=MemoriaStatsResponse)
def get_memoria_stats(request: Request, profile_id: Optional[str] = None) -> MemoriaStatsResponse:
    host = request.app.state.host
    # R8: reuse the ONLY provenance gate (memory_inspector_snapshot
    # already applies _DIGEST_CAPTURE_SOURCES) -- take counts only, never
    # touch entry["content"] / digest line text.
    snapshot = host.motor.memory_inspector_snapshot()
    session_turns = len(snapshot["entries"])
    digest_entries = snapshot["digest"]["line_count"]

    # FIX-A: when `profile_id` is present, `saved_memorias`/`pinned` filter
    # to that profile (semantics parity with MemoriaStore.count_all /
    # count_all_pinned) so the headline count agrees with the per-profile
    # GET /api/memoria/list. `saved_memorias_total`/`pinned_total` keep the
    # global figures as separate fields. Without `profile_id`, the two
    # halves coincide (both global) -- preserves the pre-FIX-A contract for
    # any old caller.
    saved_total = 0
    pinned_total = 0
    saved_memorias = 0
    pinned = 0
    if deps.memorias_enabled():
        saved_total = _count_sql(deps.memorias_db(), "SELECT COUNT(*) FROM memorias")
        pinned_total = _count_sql(deps.memorias_db(), "SELECT COUNT(*) FROM memorias WHERE pinned = 1")
        if profile_id:
            saved_memorias = _count_sql(
                deps.memorias_db(), "SELECT COUNT(*) FROM memorias WHERE profile_id = ?", (profile_id,)
            )
            pinned = _count_sql(
                deps.memorias_db(),
                "SELECT COUNT(*) FROM memorias WHERE profile_id = ? AND pinned = 1",
                (profile_id,),
            )
        else:
            saved_memorias = saved_total
            pinned = pinned_total

    return MemoriaStatsResponse(
        session_turns=session_turns,
        digest_entries=digest_entries,
        saved_memorias=saved_memorias,
        pinned=pinned,
        saved_memorias_total=saved_total,
        pinned_total=pinned_total,
        editorial_cards_by_status=_editorial_cards_by_status(deps.editorial_cards_db()),
    )


@router.get("/api/memoria/list", response_model=MemoriaListResponse)
def get_memoria_list(profile_id: str) -> MemoriaListResponse:
    if not deps.memorias_enabled():
        return MemoriaListResponse(items=[])
    # Migrate-on-read: legacy rows keyed by the profile's display NAME are
    # invisible to this UUID-scoped list. Re-key them to the UUID once, on
    # the first list for this profile (the exact action that surfaced "No
    # hay memorias guardadas"), then read normally.
    legacy_key = _legacy_profile_key(profile_id)
    if legacy_key:
        count = _rekey_legacy_memorias(deps.memorias_db(), legacy_key, profile_id)
        if count:
            logger.info("rekeyed %d legacy memorias rows for profile %s", count, profile_id)
    rows = _list_memoria_metadata(deps.memorias_db(), profile_id)
    return MemoriaListResponse(
        items=[
            MemoriaListItem(
                id=row["id"],
                title=row["title"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
                revision=row["revision"],
                pinned=bool(row["pinned"]),
                private=bool(row["private"]),
                inactive=bool(row["inactive"]),
                imported=bool(row["status"] == "imported"),
                draft=bool(row["status"] == "draft"),
                promoted=bool(row["status"] == "promoted"),
            )
            for row in rows
        ]
    )


@router.get("/api/memoria/row/{row_id}", response_model=MemoriaRowResponse)
def get_memoria_row(row_id: str, profile_id: str):
    """One row's full content, on-demand (WU-H, operator viewing decision).

    Mirrors the ownership-guard pattern from POST /api/memoria/{delete,
    update}: store.get(raising=True) pre-check, then the SAME 404 body
    for a missing row and a wrong-profile row (R7: no cross-profile
    existence oracle). Disabled feature also maps to 404 — there is no
    "empty" analog for a single-row GET the way list/stats have.
    """
    if not deps.memorias_enabled():
        return JSONResponse(status_code=404, content={"detail": "memoria not found"})
    store = deps.memoria_store_or_none()
    if store is None:
        return JSONResponse(status_code=503, content={"detail": "memoria_unavailable"})
    try:
        row = store.get(row_id, raising=True)
    except sqlite3.Error:
        return JSONResponse(status_code=503, content={"detail": "memoria_unavailable"})
    if row is None or row["profile_id"] != profile_id:
        return JSONResponse(status_code=404, content={"detail": "memoria not found"})
    return MemoriaRowResponse(
        id=row["id"],
        title=row["title"],
        content=row["content"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        pinned=bool(row["pinned"]),
        private=bool(row["private"]),
        inactive=bool(row["inactive"]),
        draft=bool(row["status"] == "draft"),
        promoted=bool(row["status"] == "promoted"),
    )


@router.post("/api/memoria/purge", response_model=MemoriaPurgeResponse)
def post_memoria_purge(body: MemoriaPurgeRequest) -> MemoriaPurgeResponse:
    if not deps.memorias_enabled():
        return MemoriaPurgeResponse(deleted=0)
    deleted = _purge_memoria(deps.memorias_db(), body.profile_id)
    return MemoriaPurgeResponse(deleted=deleted)


@router.post("/api/memoria/flags", response_model=MemoriaMutationResponse)
def post_memoria_flags(body: MemoriaFlagsRequest):
    if not deps.memorias_enabled():
        return MemoriaMutationResponse(ok=False)  # benign no-op, mirrors purge deleted=0
    if body.pinned is None and body.private is None and body.inactive is None:
        return JSONResponse(status_code=422, content={"detail": "no flags provided"})
    store = deps.memoria_store_or_none()
    if store is None:
        return JSONResponse(status_code=503, content={"detail": "memoria_unavailable"})
    try:
        row = store.get(body.id, raising=True)
    except sqlite3.Error:
        # Transient lock/db error on the pre-check must NOT masquerade as a
        # missing row (404) -- surface it as unavailable (503).
        return JSONResponse(status_code=503, content={"detail": "memoria_unavailable"})
    # Same 404 for a wrong-profile row as for a missing one (R7: no
    # cross-profile existence oracle -- identical shape and detail).
    if row is None or row["profile_id"] != body.profile_id:
        return JSONResponse(status_code=404, content={"detail": "memoria not found"})
    # F5: set_flags enforces the freeze rule (pin/private promote curated,
    # un-pin never demotes). A genuine write failure raises (raising=True) --
    # it MUST surface as 503, never be swallowed, or the next auto-capture
    # could overwrite a row the operator believed frozen.
    try:
        applied = store.set_flags(
            body.id, pinned=body.pinned, private=body.private, inactive=body.inactive, raising=True
        )
    except sqlite3.Error:
        return JSONResponse(status_code=503, content={"detail": "memoria_write_failed"})
    if not applied:
        # rowcount==0 on a pre-found row: it vanished in the check-then-act
        # race window -- that is not-found, not a write failure.
        return JSONResponse(status_code=404, content={"detail": "memoria not found"})
    return MemoriaMutationResponse(ok=True)


@router.post("/api/memoria/delete", response_model=MemoriaMutationResponse)
def post_memoria_delete(body: MemoriaDeleteRequest):
    if not deps.memorias_enabled():
        return MemoriaMutationResponse(ok=False)  # benign no-op, mirrors flags/purge
    store = deps.memoria_store_or_none()
    if store is None:
        return JSONResponse(status_code=503, content={"detail": "memoria_unavailable"})
    try:
        row = store.get(body.id, raising=True)
    except sqlite3.Error:
        return JSONResponse(status_code=503, content={"detail": "memoria_unavailable"})
    # Same 404 for a wrong-profile row as for a missing one (R7: no
    # cross-profile existence oracle). This pre-check owns the "already
    # gone" case, so delete_row's False below can only mean a real write
    # failure -- never "the row wasn't there" (delete_row is idempotent).
    if row is None or row["profile_id"] != body.profile_id:
        return JSONResponse(status_code=404, content={"detail": "memoria not found"})
    if not store.delete_row(body.id):
        return JSONResponse(status_code=503, content={"detail": "memoria_write_failed"})
    return MemoriaMutationResponse(ok=True)


@router.post("/api/memoria/update", response_model=MemoriaMutationResponse)
def post_memoria_update(body: MemoriaUpdateRequest):
    if not deps.memorias_enabled():
        return MemoriaMutationResponse(ok=False)  # benign no-op, mirrors flags/delete
    # Empty-check is API-side: update_row whitespace-normalizes without
    # validating emptiness, so an all-whitespace body would write ''.
    title, content = body.title.strip(), body.content.strip()
    if not title or not content:
        return JSONResponse(
            status_code=422, content={"detail": "title and content must not be empty"}
        )
    if len(title) > _MEMORIA_TITLE_MAX_LENGTH or len(content) > _MEMORIA_CONTENT_MAX_LENGTH:
        return JSONResponse(
            status_code=422, content={"detail": "title or content exceeds max length"}
        )
    store = deps.memoria_store_or_none()
    if store is None:
        return JSONResponse(status_code=503, content={"detail": "memoria_unavailable"})
    try:
        row = store.get(body.id, raising=True)
    except sqlite3.Error:
        return JSONResponse(status_code=503, content={"detail": "memoria_unavailable"})
    # Same 404 for a wrong-profile row as for a missing one (R7: no
    # cross-profile existence oracle -- identical shape and detail).
    if row is None or row["profile_id"] != body.profile_id:
        return JSONResponse(status_code=404, content={"detail": "memoria not found"})
    # F5: update_row promotes status='curated' in the same statement. A
    # genuine write failure raises (raising=True) -> 503; a plain False now
    # means only rowcount==0 (the row vanished in the check-then-act race)
    # -> 404, never a misleading write-failed.
    try:
        applied = store.update_row(body.id, title=title, content=content, raising=True)
    except sqlite3.Error:
        return JSONResponse(status_code=503, content={"detail": "memoria_write_failed"})
    if not applied:
        return JSONResponse(status_code=404, content={"detail": "memoria not found"})
    return MemoriaMutationResponse(ok=True)


@router.post("/api/memoria/import", response_model=MemoriaImportResponse)
def post_memoria_import(body: MemoriaImportRequest):
    """Import an external-AI export into the per-profile store as 'imported'
    rows (memoria_import_20260718, WU3).

    Posture mirrors post_memoria_update: MEMORIAS_ENABLED no-op, 422 for the
    trust-boundary caps, 503 memoria_unavailable when the store is down, same
    loopback auth as the sibling routes (no rate limiting — single operator).

    Counts-only response (R8 — never echoes claim/title/content). Every parsed
    claim maps to exactly one of the four insert_imported outcomes so a lock
    loss is reported as `failed`, never a `duplicate` (R4).

    Bounded work: the byte cap bounds parse cost, the 100-item cap bounds the
    per-item loop, and each insert_imported is bounded by WRITE_TIMEOUT_SECONDS.
    Under a sustained DB lock the loop early-aborts after 3 consecutive `error`
    outcomes (remaining items count into `failed`, R4), so the worst case is
    ~3s of lock probing plus parse time — not ~100s of grinding every item.

    D6 cap honesty: a profile already at MEMORIAS_IMPORT_CAP is rejected
    pre-flight (422); otherwise the cap is enforced in-loop — only `created`
    rows consume headroom, and creatable items past the cap are counted in
    `skipped_cap`, so a full-duplicate re-import near the cap lands 0 rows and
    is never falsely rejected (duplicates/too-short never consume headroom).
    """
    if not deps.memorias_enabled():
        # Benign no-op, mirrors flags/delete/update. Zeroed counts.
        return MemoriaImportResponse(
            ok=False, imported=0, skipped_duplicates=0,
            skipped_too_short=0, skipped_cap=0, failed=0,
        )
    # profile_id is the store's per-profile scope key; a blank one would pass
    # Pydantic (non-empty str is optional) but make insert_imported raise
    # MemoriaValidationError (uncaught -> 500). Reject it at the boundary,
    # matching the sibling routes' 422 error style.
    if not (body.profile_id or "").strip():
        return JSONResponse(status_code=422, content={"detail": "invalid profile_id"})
    content = body.content or ""
    if not content.strip():
        return JSONResponse(status_code=422, content={"detail": "content must not be empty"})
    # Sanitize the untrusted provenance tag before it becomes a title prefix:
    # strip control chars (an embedded NUL raises ValueError inside sqlite3,
    # escaping the store's sqlite3.Error catch -> 500) then collapse whitespace.
    label = " ".join(strip_control_chars(body.source_label).split())
    if len(label) > _MEMORIA_IMPORT_LABEL_MAX_LENGTH:
        return JSONResponse(
            status_code=422, content={"detail": "source_label exceeds max length"}
        )
    # Byte cap first -- bounds the parser's input before any work.
    if len(content.encode("utf-8")) > MEMORIAS_IMPORT_MAX_BYTES:
        return JSONResponse(status_code=422, content={"detail": "content exceeds max size"})
    items = parse_import(content)
    if len(items) > MEMORIAS_IMPORT_MAX_ITEMS:
        return JSONResponse(
            status_code=422, content={"detail": "too many items, split the file"}
        )
    store = deps.memoria_store_or_none()
    if store is None:
        return JSONResponse(status_code=503, content={"detail": "memoria_unavailable"})
    # D2/D6 import cap -- pre-flight reject ONLY when the profile is already
    # full (no silent pruning of deliberate imports). A -1 fail-open count
    # means the read itself failed -> 503. The batch itself is bounded
    # in-loop below, so a full-duplicate re-import near the cap is never
    # falsely rejected (0 new rows would land).
    existing = store.count_imported(body.profile_id)
    if existing < 0:
        return JSONResponse(status_code=503, content={"detail": "memoria_unavailable"})
    if existing >= deps.memorias_import_cap():
        return JSONResponse(status_code=422, content={"detail": "import cap exceeded"})
    headroom = deps.memorias_import_cap() - existing  # >= 1 (guaranteed above)

    imported = skipped_duplicates = skipped_too_short = skipped_cap = failed = 0
    consecutive_errors = 0
    for idx, item in enumerate(items):
        claim = item.content
        if not is_capturable(claim):
            skipped_too_short += 1
            continue
        # D6: only `created` rows consume cap headroom. Once it is spent, a
        # further creatable claim is not attempted (would breach the cap) --
        # counted as skipped_cap, honestly distinct from a duplicate.
        if imported >= headroom:
            skipped_cap += 1
            continue
        source_key = derive_import_key(body.profile_id, claim) or ""
        title = f"[{label}] {build_title(claim)}" if label else build_title(claim)
        signature = build_signature(f"{item.section} {claim}")
        outcome = store.insert_imported(
            body.profile_id, title, claim, signature=signature, source_key=source_key
        )
        if outcome == "created":
            imported += 1
            consecutive_errors = 0
        elif outcome == "duplicate":
            skipped_duplicates += 1
            consecutive_errors = 0
        elif outcome == "skipped":
            skipped_too_short += 1
            consecutive_errors = 0
        else:  # "error" -- fail-open store failure, surfaced honestly (R4)
            failed += 1
            consecutive_errors += 1
            if consecutive_errors >= _MAX_CONSECUTIVE_IMPORT_ERRORS:
                # DB provably unavailable for this request: stop grinding,
                # count every remaining (unattempted) item as failed.
                failed += len(items) - (idx + 1)
                break

    return MemoriaImportResponse(
        ok=failed == 0,
        imported=imported,
        skipped_duplicates=skipped_duplicates,
        skipped_too_short=skipped_too_short,
        skipped_cap=skipped_cap,
        failed=failed,
    )


@router.post("/api/memoria/capture", response_model=MemoriaMutationResponse)
def post_memoria_capture(request: Request, body: MemoriaCaptureRequest):
    if not deps.memorias_enabled():
        return MemoriaMutationResponse(ok=False)  # gated no-op, mirrors flags/delete/update
    # Direct motor call (design resolution 2905/D3): the CTK toggles this on
    # the motor directly (inspector_memory.py), NOT via the command queue.
    # No llm_engine.py change, no _COMMAND_WHITELIST entry.
    request.app.state.host.motor.set_memorias_private(body.paused)
    return MemoriaMutationResponse(ok=True)


@router.get("/api/memoria/notice", response_model=MemoriaNoticeResponse)
def get_memoria_notice() -> MemoriaNoticeResponse:
    # Fail-open to False (banner shows) when the flag file is absent -- no
    # host state, works even before host init. No lock: single-flag read.
    return MemoriaNoticeResponse(dismissed=load_memorias_notice_dismissed())


@router.post("/api/memoria/notice", response_model=MemoriaNoticeResponse)
def post_memoria_notice(body: MemoriaNoticeRequest) -> MemoriaNoticeResponse:
    # Atomic single-flag write (os.replace), no read-modify-write, so no
    # lock. Re-read from disk so the response reflects actual persisted
    # state (save swallows write errors by design -- CTK fail-open parity).
    save_memorias_notice_dismissed(body.dismissed)
    return MemoriaNoticeResponse(dismissed=load_memorias_notice_dismissed())
