"""/api/music/* -- library, mood, state, fade, import, track delete/audio
(moved verbatim from main.py, refactor_core_api_20260802 B5).

`_music_track_status`, `_music_track_out`, `_normalize_mood_strict`, and the
`_MUSIC_FADE_*`/`_MUSIC_IMPORT_PATH_MAX_LENGTH` constants are used ONLY by
this family (confirmed by grep across the rest of main.py before the move),
so they relocate here wholesale rather than through a shared module.
`_MUSIC_IMPORT_MAX_BYTES` IS monkeypatched directly on `opencohost.api.main`
(test_api_music_library_mutations.py, down to 4 bytes so a 12-byte wav
fixture deterministically exceeds it), so it goes through
`deps.music_import_max_bytes()` -- a plain top-level import would freeze the
pre-patch 200MB value at router import time. `_music_lock` is not a name
here: `host.music_lock` (request-scoped, on the EngineHost double/instance)
is unrelated to the module-level file-write locks the other families use.

CAUTION (path ambiguity, verified during the move): the literal paths
(`/api/music/library|mood|state|fade|import`) and the two templates
(`/api/music/track/{track_id}`, `/api/music/track/{track_id}/audio`) all
share the `/api/music/...` prefix, but no literal segment collides with the
templates' fixed `track` segment -- `library`/`mood`/`state`/`fade`/`import`
are distinct literal 3rd segments, never `track`. No ambiguity, so relative
registration order here is cosmetic (same reasoning documented in
routers/perfiles.py for its own template/literal mix).

CAUTION (file-serving, verified during the move): `get_music_track_audio`
keeps its exact response class (`FileResponse`), headers (media type via
`mimetypes.guess_type`), and streaming/close behavior (`BackgroundTask(fh.close)`
closing the handle only once the stream finishes) — byte-identical to the
pre-move handler.
"""

import mimetypes
import re
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse
from starlette.background import BackgroundTask

from opencohost.api import deps
from opencohost.api.models import (
    MusicFadeRequest,
    MusicImportRequest,
    MusicImportResponse,
    MusicLibraryResponse,
    MusicMoodRequest,
    MusicMoodResponse,
    MusicStateResponse,
    MusicTrackOut,
)
from opencohost.core.music_library import ALLOWED_AUDIO_EXTENSIONS, KNOWN_MOODS, is_supported_audio_path

router = APIRouter()

# POST /api/music/fade — a fade INTENT (client executes it, never backend audio).
_MUSIC_FADE_DIRECTIONS = frozenset({"in", "out"})
_MUSIC_FADE_DEFAULT_MS = 6000
_MUSIC_FADE_MAX_MS = 60000

# POST /api/music/import — bound the source path + copy size at the trust boundary.
_MUSIC_IMPORT_PATH_MAX_LENGTH = 1024


def _music_track_status(track) -> str:
    if track.missing:
        return "faltante"
    if track.invalid:
        return "invalido"
    return "ok"


def _music_track_out(track) -> MusicTrackOut:
    return MusicTrackOut(
        id=track.id, label=track.label, mood=track.mood, status=_music_track_status(track)
    )


def _normalize_mood_strict(mood: str) -> "str | None":
    """Case/whitespace-normalize `mood`, then REQUIRE KNOWN_MOODS membership.

    Mirrors music_library.normalize_mood's regex normalization but returns
    None on an unknown mood instead of silently falling back to 'normal'. That
    silent fallback is safe for the CTK dropdown (it can't send garbage) but a
    footgun at an API trust boundary — a typo like 'hyep' would otherwise file
    the track under 'normal' with no signal (design D5)."""
    normalized = re.sub(r"[^a-z0-9_]+", "_", (mood or "").strip().lower()).strip("_")
    return normalized if normalized in KNOWN_MOODS else None


@router.get("/api/music/library", response_model=MusicLibraryResponse)
def get_music_library(request: Request):
    host = request.app.state.host
    library = getattr(host, "music_library", None)
    if library is None:
        return JSONResponse(status_code=503, content={"detail": "music_unavailable"})
    # D4: guard every library read/mutation on host.music_lock — the
    # library has no internal lock and all_tracks() iterates self.tracks,
    # which a concurrent import/delete could mutate mid-iteration.
    with host.music_lock:
        tracks = [_music_track_out(t) for t in library.all_tracks()]
    return MusicLibraryResponse(
        tracks=tracks, count=len(tracks), moods=sorted({t.mood for t in tracks})
    )


@router.post("/api/music/mood", response_model=MusicMoodResponse)
def post_music_mood(request: Request, body: MusicMoodRequest):
    host = request.app.state.host
    library = getattr(host, "music_library", None)
    if library is None:
        return JSONResponse(status_code=503, content={"detail": "music_unavailable"})
    mood = _normalize_mood_strict(body.mood)
    if mood is None:
        return JSONResponse(status_code=422, content={"detail": "unknown mood"})
    # State only — orchestration, NEVER backend audio (2911). The Tauri
    # client reads this mood and plays the corresponding track itself.
    host.music_state.set_mood(mood)
    with host.music_lock:
        valid = library.valid_tracks()
        matching = [t for t in valid if t.mood == mood]
        suggested = library.select_for_mood(mood)
        fallback = not matching
        if fallback:
            # Mirror select_for_mood's mood->normal->any fallback chain so
            # `tracks` and `suggested_track_id` stay consistent — an empty
            # bucket must never leave the client rotating over nothing.
            normal_pool = [t for t in valid if t.mood == "normal"]
            pool = normal_pool if normal_pool else valid
        else:
            pool = matching
    return MusicMoodResponse(
        active_mood=mood,
        tracks=[_music_track_out(t) for t in pool],
        suggested_track_id=suggested.id if suggested else None,
        fallback=fallback,
    )


@router.get("/api/music/state", response_model=MusicStateResponse)
def get_music_state(request: Request):
    host = request.app.state.host
    if getattr(host, "music_library", None) is None:
        return JSONResponse(status_code=503, content={"detail": "music_unavailable"})
    return MusicStateResponse(**host.music_state.snapshot())


@router.post("/api/music/fade", response_model=MusicStateResponse)
def post_music_fade(request: Request, body: MusicFadeRequest):
    host = request.app.state.host
    if getattr(host, "music_library", None) is None:
        return JSONResponse(status_code=503, content={"detail": "music_unavailable"})
    if body.direction not in _MUSIC_FADE_DIRECTIONS:
        return JSONResponse(status_code=422, content={"detail": "unknown direction"})
    duration_ms = _MUSIC_FADE_DEFAULT_MS if body.duration_ms is None else body.duration_ms
    if not 0 < duration_ms <= _MUSIC_FADE_MAX_MS:
        return JSONResponse(status_code=422, content={"detail": "duration_ms out of range"})
    # Intent only — the Tauri client runs the fade; the API never touches
    # AudioBedEngine (2911, headless host has none).
    host.music_state.set_fade(body.direction, duration_ms)
    return MusicStateResponse(**host.music_state.snapshot())


@router.post("/api/music/import", response_model=MusicImportResponse)
def post_music_import(request: Request, body: MusicImportRequest):
    host = request.app.state.host
    library = getattr(host, "music_library", None)
    if library is None:
        return JSONResponse(status_code=503, content={"detail": "music_unavailable"})
    mood = _normalize_mood_strict(body.mood)
    if mood is None:
        return JSONResponse(status_code=422, content={"detail": "unknown mood"})
    raw = body.path.strip()
    if not raw or len(raw) > _MUSIC_IMPORT_PATH_MAX_LENGTH:
        return JSONResponse(status_code=422, content={"detail": "invalid path"})
    source = Path(raw)
    if not source.is_absolute():
        return JSONResponse(status_code=422, content={"detail": "path must be absolute"})
    if source.suffix.lower() not in ALLOWED_AUDIO_EXTENSIONS:
        return JSONResponse(status_code=422, content={"detail": "only .mp3/.wav files"})
    try:
        if not source.is_file():
            return JSONResponse(status_code=422, content={"detail": "file not found"})
        if source.stat().st_size > deps.music_import_max_bytes():
            return JSONResponse(status_code=422, content={"detail": "file too large"})
    except OSError:
        return JSONResponse(status_code=422, content={"detail": "file not readable"})
    # WU5: dedup pre-check under the lock, then do the (up-to-200MB) copy
    # OUTSIDE the lock so it never blocks concurrent control-plane reads,
    # then re-take the lock only to register the finished track. stage_copy
    # re-validates the audio signature and copies into the managed dir under
    # a uuid name (traversal-proof); register_staged rechecks dedup and rolls
    # back (pop + unlink) on save failure. No backend audio.
    with host.music_lock:
        existing = library.find_existing(source, mood)
    if existing is not None:
        return MusicImportResponse(track=_music_track_out(existing))
    try:
        staged = library.stage_copy(source)
    except ValueError as exc:
        return JSONResponse(status_code=422, content={"detail": str(exc)})
    except OSError:
        return JSONResponse(status_code=503, content={"detail": "music_write_failed"})
    try:
        with host.music_lock:
            track = library.register_staged(staged, source, mood)
    except OSError:
        return JSONResponse(status_code=503, content={"detail": "music_write_failed"})
    return MusicImportResponse(track=_music_track_out(track))


@router.delete("/api/music/track/{track_id}")
def delete_music_track(request: Request, track_id: str):
    host = request.app.state.host
    library = getattr(host, "music_library", None)
    if library is None:
        return JSONResponse(status_code=503, content={"detail": "music_unavailable"})
    # remove(delete_file=True) unlinks only files inside library_dir
    # (_delete_managed_file's is_relative_to guard); an externally-pathed
    # entry is deregistered but its file survives. No backend audio.
    try:
        with host.music_lock:
            removed = library.remove(track_id, delete_file=True, raising=True)
    except OSError:
        return JSONResponse(status_code=503, content={"detail": "music_write_failed"})
    if not removed:
        return JSONResponse(status_code=404, content={"detail": "track not found"})
    return {"ok": True}  # mirrors delete_perfil; repeat DELETE -> 404


@router.get("/api/music/track/{track_id}/audio")
def get_music_track_audio(request: Request, track_id: str):
    # Client-side-playback model (2911) + resolution 2914: the API is the
    # single point that mediates audio access. It never plays audio; it
    # streams the managed file and reports availability. Missing/moved/
    # corrupted/out-of-library files all surface as 404 instead of the
    # client hitting a phantom source unnoticed.
    host = request.app.state.host
    library = getattr(host, "music_library", None)
    if library is None:
        return JSONResponse(status_code=503, content={"detail": "music_unavailable"})
    # WU5/D8 TOCTOU: resolve, validate, AND open the file handle all under
    # the lock, then hand the handle to FileResponse via BackgroundTask so
    # it is closed only when the stream finishes. On Windows the open handle
    # blocks a concurrent DELETE's unlink -> that DELETE gets a retryable
    # 503 instead of pulling the file out from under a live stream (500).
    # ponytail: handle-as-delete-guard is Windows semantics; POSIX would
    # need fd-based serving to get the same interlock.
    with host.music_lock:
        track = library.tracks.get(track_id)
        if track is None:
            return JSONResponse(status_code=404, content={"detail": "track not found"})
        library_root = library.library_dir.resolve()
        track_path = Path(track.path).resolve()
        # Path-safety: serve ONLY files inside library_dir (mirrors
        # _delete_managed_file's is_relative_to guard). is_supported_audio_path
        # then covers both missing-on-disk and failed-signature in one check.
        if not track_path.is_relative_to(library_root):
            return JSONResponse(status_code=404, content={"detail": "track not found"})
        if not is_supported_audio_path(track_path):
            return JSONResponse(status_code=404, content={"detail": "track not found"})
        try:
            fh = track_path.open("rb")
        except OSError:
            return JSONResponse(status_code=404, content={"detail": "track not found"})
    media_type = mimetypes.guess_type(str(track_path))[0] or "application/octet-stream"
    return FileResponse(
        track_path, media_type=media_type, background=BackgroundTask(fh.close)
    )
