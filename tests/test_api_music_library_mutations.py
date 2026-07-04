"""Tests for music library mutations (phase 2 WU3): POST /api/music/import.

Import is backend library management under the client-side-playback model
([[tauri/music-orchestration-model]]): the API validates + copies the source
audio into the managed library dir and registers the track. It never plays
audio. The import response exposes orchestration fields only — NO `path`
(resolution 2914: audio is delivered via a FileResponse endpoint, so the raw
filesystem path is never leaked to the client).

Fixtures mirror tests/test_api_music.py / test_api_music_state.py exactly: a
REAL, Tk-free MusicLibrary rooted at `tmp_path` (never the real config/dir),
`FakeHost` from tests/test_api_phase1 (always carrying `music_state`/
`music_lock`), `_reset_host_active` autouse, `create_app(host_factory=...)`.
Valid wav bytes: `b"RIFF____WAVE"`; invalid: `b"NOT_AUDIO_DATA"`.
"""

import json

import pytest
from fastapi.testclient import TestClient

from opencohost.core.music_library import MusicLibrary, MusicTrack
from tests.test_api_phase1 import FakeHost

_DEFAULT_TEST_ORIGINS = ["http://localhost:5173"]


@pytest.fixture(autouse=True)
def _reset_host_active():
    import opencohost.api.main as main_mod

    main_mod._host_active = False
    yield
    main_mod._host_active = False


def _host_with_library(library):
    def factory():
        host = FakeHost()
        host.music_library = library
        return host

    return factory


def _app(host_factory):
    import opencohost.api.main as main_mod

    return main_mod.create_app(host_factory=host_factory, cors_origins=_DEFAULT_TEST_ORIGINS)


def _make_library(tmp_path):
    return MusicLibrary(
        library_dir=tmp_path / "music", config_file=tmp_path / "music_library.json"
    )


def _write_wav(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"RIFF____WAVE")


# ──────────────────────────────────────────────────────────────────────────
# POST /api/music/import — happy path: validate, copy, register
# ──────────────────────────────────────────────────────────────────────────


def test_import_valid_wav_copies_and_registers(tmp_path):
    library = _make_library(tmp_path)
    source = tmp_path / "incoming" / "song.wav"
    _write_wav(source)

    app = _app(_host_with_library(library))
    with TestClient(app) as client:
        resp = client.post("/api/music/import", json={"path": str(source), "mood": "hype"})
        assert resp.status_code == 200
        track = resp.json()["track"]
        assert track["mood"] == "hype"
        assert track["status"] == "ok"

        # The file is physically copied into the managed library dir under a
        # uuid-suffixed name (never the caller's filename — traversal-proof).
        library_dir = tmp_path / "music"
        copied = list(library_dir.glob("*.wav"))
        assert len(copied) == 1
        assert copied[0].name != "song.wav"

        # music_library.json on disk carries the new track.
        config = json.loads((tmp_path / "music_library.json").read_text(encoding="utf-8"))
        assert [t["id"] for t in config["tracks"]] == [track["id"]]

        # A follow-up GET /api/music/library includes it.
        lib = client.get("/api/music/library").json()
        assert track["id"] in {t["id"] for t in lib["tracks"]}


def test_import_response_has_no_path_field(tmp_path):
    """Resolution 2914 regression guard: audio is delivered via a FileResponse
    endpoint, so the raw filesystem path is NEVER exposed in the response."""
    library = _make_library(tmp_path)
    source = tmp_path / "incoming" / "song.wav"
    _write_wav(source)

    app = _app(_host_with_library(library))
    with TestClient(app) as client:
        resp = client.post("/api/music/import", json={"path": str(source), "mood": "calm"})
        assert resp.status_code == 200
        assert "path" not in resp.json()["track"]


# ──────────────────────────────────────────────────────────────────────────
# POST /api/music/import — validation guards (nothing written on rejection)
# ──────────────────────────────────────────────────────────────────────────


def test_import_relative_path_422(tmp_path):
    library = _make_library(tmp_path)

    app = _app(_host_with_library(library))
    with TestClient(app) as client:
        resp = client.post(
            "/api/music/import", json={"path": "relative/song.wav", "mood": "hype"}
        )
        assert resp.status_code == 422
        assert library.tracks == {}
        assert not (tmp_path / "music_library.json").exists()


def test_import_empty_path_422(tmp_path):
    library = _make_library(tmp_path)

    app = _app(_host_with_library(library))
    with TestClient(app) as client:
        resp = client.post("/api/music/import", json={"path": "   ", "mood": "hype"})
        assert resp.status_code == 422
        assert library.tracks == {}


def test_import_missing_file_422(tmp_path):
    library = _make_library(tmp_path)
    ghost = tmp_path / "incoming" / "ghost.wav"  # absolute, .wav, never created

    app = _app(_host_with_library(library))
    with TestClient(app) as client:
        resp = client.post("/api/music/import", json={"path": str(ghost), "mood": "hype"})
        assert resp.status_code == 422
        assert library.tracks == {}
        assert not (tmp_path / "music_library.json").exists()


def test_import_disallowed_extension_422_before_io(tmp_path):
    library = _make_library(tmp_path)
    source = tmp_path / "incoming" / "notes.txt"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"whatever")

    app = _app(_host_with_library(library))
    with TestClient(app) as client:
        resp = client.post("/api/music/import", json={"path": str(source), "mood": "hype"})
        assert resp.status_code == 422
        # Rejected on extension before any copy — no managed dir, no config.
        assert not (tmp_path / "music").exists()
        assert not (tmp_path / "music_library.json").exists()


def test_import_bad_signature_422_no_residue(tmp_path):
    """A .wav-named file with non-audio bytes fails the magic-bytes signature
    check inside add_file (raised BEFORE copy) — 422, no track, no residue."""
    library = _make_library(tmp_path)
    source = tmp_path / "incoming" / "fake.wav"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"NOT_AUDIO_DATA")

    app = _app(_host_with_library(library))
    with TestClient(app) as client:
        resp = client.post("/api/music/import", json={"path": str(source), "mood": "hype"})
        assert resp.status_code == 422
        assert library.tracks == {}
        library_dir = tmp_path / "music"
        assert not library_dir.exists() or list(library_dir.glob("*")) == []
        assert not (tmp_path / "music_library.json").exists()


def test_import_oversize_422_no_copy(tmp_path, monkeypatch):
    import opencohost.api.main as main_mod

    monkeypatch.setattr(main_mod, "_MUSIC_IMPORT_MAX_BYTES", 4)  # 12-byte wav exceeds this
    library = _make_library(tmp_path)
    source = tmp_path / "incoming" / "big.wav"
    _write_wav(source)

    app = _app(_host_with_library(library))
    with TestClient(app) as client:
        resp = client.post("/api/music/import", json={"path": str(source), "mood": "hype"})
        assert resp.status_code == 422
        assert library.tracks == {}
        assert not (tmp_path / "music").exists()


def test_import_unknown_mood_422_no_copy(tmp_path):
    library = _make_library(tmp_path)
    source = tmp_path / "incoming" / "song.wav"
    _write_wav(source)

    app = _app(_host_with_library(library))
    with TestClient(app) as client:
        resp = client.post(
            "/api/music/import", json={"path": str(source), "mood": "not_a_real_mood"}
        )
        assert resp.status_code == 422
        assert library.tracks == {}
        assert not (tmp_path / "music").exists()


def test_import_copy_oserror_503_registry_unchanged(tmp_path, monkeypatch):
    """A copy failure (OSError from shutil.copy2) surfaces as 503
    music_write_failed and leaves the registry/config untouched."""
    import opencohost.core.music_library as music_mod

    def _boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(music_mod.shutil, "copy2", _boom)
    library = _make_library(tmp_path)
    source = tmp_path / "incoming" / "song.wav"
    _write_wav(source)

    app = _app(_host_with_library(library))
    with TestClient(app) as client:
        resp = client.post("/api/music/import", json={"path": str(source), "mood": "hype"})
        assert resp.status_code == 503
        assert resp.json() == {"detail": "music_write_failed"}
        assert library.tracks == {}
        assert not (tmp_path / "music_library.json").exists()


def test_import_503_when_library_none(tmp_path):
    source = tmp_path / "incoming" / "song.wav"
    _write_wav(source)

    app = _app(_host_with_library(None))
    with TestClient(app) as client:
        resp = client.post("/api/music/import", json={"path": str(source), "mood": "hype"})
        assert resp.status_code == 503
        assert resp.json() == {"detail": "music_unavailable"}


# ──────────────────────────────────────────────────────────────────────────
# DELETE /api/music/track/{id} — backend library mgmt (managed-dir guarded,
# idempotent: mirrors delete_perfil — a repeat DELETE is 404, never a 2nd 200)
# ──────────────────────────────────────────────────────────────────────────


def _import_one(client, tmp_path, mood="hype"):
    source = tmp_path / "incoming" / "song.wav"
    _write_wav(source)
    resp = client.post("/api/music/import", json={"path": str(source), "mood": mood})
    assert resp.status_code == 200
    return resp.json()["track"]["id"]


def test_delete_existing_track_200_unlinks_and_deregisters(tmp_path):
    library = _make_library(tmp_path)

    app = _app(_host_with_library(library))
    with TestClient(app) as client:
        track_id = _import_one(client, tmp_path)
        library_dir = tmp_path / "music"
        assert list(library_dir.glob("*.wav"))  # copied file present pre-delete

        resp = client.delete(f"/api/music/track/{track_id}")
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}

        # Managed file physically unlinked, track gone from registry + config.
        assert list(library_dir.glob("*.wav")) == []
        assert track_id not in library.tracks
        config = json.loads((tmp_path / "music_library.json").read_text(encoding="utf-8"))
        assert track_id not in {t["id"] for t in config["tracks"]}


def test_delete_is_idempotent_second_delete_404(tmp_path):
    library = _make_library(tmp_path)

    app = _app(_host_with_library(library))
    with TestClient(app) as client:
        track_id = _import_one(client, tmp_path)

        assert client.delete(f"/api/music/track/{track_id}").status_code == 200
        # Repeat DELETE of the same id -> 404, never a second 200 (idempotency).
        resp = client.delete(f"/api/music/track/{track_id}")
        assert resp.status_code == 404
        assert resp.json() == {"detail": "track not found"}


def test_delete_unknown_id_404(tmp_path):
    library = _make_library(tmp_path)

    app = _app(_host_with_library(library))
    with TestClient(app) as client:
        resp = client.delete("/api/music/track/never-existed")
        assert resp.status_code == 404
        assert resp.json() == {"detail": "track not found"}


def test_delete_track_outside_library_dir_deregisters_but_keeps_external_file(tmp_path):
    """A registry entry whose path points OUTSIDE library_dir (hand-crafted, not
    created via add_file) is deregistered on DELETE, but _delete_managed_file's
    is_relative_to guard refuses to unlink the external file — proving the guard
    is honored end-to-end through the API, not bypassed."""
    library = _make_library(tmp_path)
    external = tmp_path / "outside" / "external.wav"
    _write_wav(external)
    track = MusicTrack(
        id="external-track",
        original_name="external.wav",
        path=str(external),
        mood="hype",
        label="hype",
    )
    library.tracks[track.id] = track
    library.save()

    app = _app(_host_with_library(library))
    with TestClient(app) as client:
        resp = client.delete(f"/api/music/track/{track.id}")
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}

        # Deregistered from the library...
        assert track.id not in library.tracks
        # ...but the file OUTSIDE library_dir survives (managed-dir guard).
        assert external.exists()


def test_delete_503_when_library_none():
    app = _app(_host_with_library(None))
    with TestClient(app) as client:
        resp = client.delete("/api/music/track/anything")
        assert resp.status_code == 503
        assert resp.json() == {"detail": "music_unavailable"}


# ──────────────────────────────────────────────────────────────────────────
# GET /api/music/track/{id}/audio — FileResponse delivery (WU5, resolution
# 2914). The API is the single point that knows whether the audio is
# available/valid: missing/moved/invalid/out-of-library files all surface as
# 404 here instead of the client hitting a phantom file unnoticed. Content-Type
# is extension-driven (mimetypes), never sniffed.
# ──────────────────────────────────────────────────────────────────────────


def _write_mp3(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    # ID3-tagged header passes is_supported_audio_path's mp3 signature check.
    path.write_bytes(b"ID3" + b"\x00" * 9)


def test_get_audio_valid_wav_200_exact_bytes(tmp_path):
    library = _make_library(tmp_path)

    app = _app(_host_with_library(library))
    with TestClient(app) as client:
        track_id = _import_one(client, tmp_path)
        copied = list((tmp_path / "music").glob("*.wav"))[0]

        resp = client.get(f"/api/music/track/{track_id}/audio")
        assert resp.status_code == 200
        # Bytes served are exactly the managed copy's bytes.
        assert resp.content == copied.read_bytes()
        assert resp.headers["content-type"].startswith("audio/")


def test_get_audio_valid_mp3_200_mpeg_content_type(tmp_path):
    library = _make_library(tmp_path)
    source = tmp_path / "incoming" / "song.mp3"
    _write_mp3(source)

    app = _app(_host_with_library(library))
    with TestClient(app) as client:
        resp = client.post("/api/music/import", json={"path": str(source), "mood": "hype"})
        assert resp.status_code == 200
        track_id = resp.json()["track"]["id"]

        audio = client.get(f"/api/music/track/{track_id}/audio")
        assert audio.status_code == 200
        # Extension-driven (mimetypes), not sniffed. Assert the audio/* family
        # rather than an exact subtype: mimetypes.guess_type can read the
        # Windows registry, which maps .mp3 to audio/mpeg or audio/mp3
        # depending on the machine — an exact-match assert is cross-machine flaky.
        assert audio.headers["content-type"].startswith("audio/")


def test_get_audio_unknown_track_id_404(tmp_path):
    library = _make_library(tmp_path)

    app = _app(_host_with_library(library))
    with TestClient(app) as client:
        resp = client.get("/api/music/track/never-existed/audio")
        assert resp.status_code == 404
        assert resp.json() == {"detail": "track not found"}


def test_get_audio_file_deleted_out_of_band_404(tmp_path):
    """The registry entry exists but the managed file was removed out-of-band
    since import — the API reports 404, not a phantom 200."""
    library = _make_library(tmp_path)

    app = _app(_host_with_library(library))
    with TestClient(app) as client:
        track_id = _import_one(client, tmp_path)
        list((tmp_path / "music").glob("*.wav"))[0].unlink()

        resp = client.get(f"/api/music/track/{track_id}/audio")
        assert resp.status_code == 404


def test_get_audio_invalid_signature_404(tmp_path):
    """The on-disk bytes fail the audio signature check (corrupted after import)
    — 404 even though the registry entry and the file both exist."""
    library = _make_library(tmp_path)

    app = _app(_host_with_library(library))
    with TestClient(app) as client:
        track_id = _import_one(client, tmp_path)
        copied = list((tmp_path / "music").glob("*.wav"))[0]
        copied.write_bytes(b"NOT_AUDIO_DATA")

        resp = client.get(f"/api/music/track/{track_id}/audio")
        assert resp.status_code == 404


def test_get_audio_path_outside_library_dir_404(tmp_path):
    """A hand-crafted registry entry whose (valid) file lives OUTSIDE library_dir
    is refused by the path-safety guard — mirrors the WU4 delete-guard test."""
    library = _make_library(tmp_path)
    external = tmp_path / "outside" / "external.wav"
    _write_wav(external)
    track = MusicTrack(
        id="external-track",
        original_name="external.wav",
        path=str(external),
        mood="hype",
        label="hype",
    )
    library.tracks[track.id] = track
    library.save()

    app = _app(_host_with_library(library))
    with TestClient(app) as client:
        resp = client.get(f"/api/music/track/{track.id}/audio")
        assert resp.status_code == 404


def test_get_audio_503_when_library_none():
    app = _app(_host_with_library(None))
    with TestClient(app) as client:
        resp = client.get("/api/music/track/anything/audio")
        assert resp.status_code == 503
        assert resp.json() == {"detail": "music_unavailable"}


def test_no_music_response_leaks_path_field(tmp_path):
    """Cross-cutting regression (resolution 2914): `path` is absent from EVERY
    music JSON body — import, library list, and mood — confirming design D6 was
    fully voided and the client only ever reaches audio via the FileResponse
    route."""
    library = _make_library(tmp_path)
    source = tmp_path / "incoming" / "song.wav"
    _write_wav(source)

    app = _app(_host_with_library(library))
    with TestClient(app) as client:
        imported = client.post(
            "/api/music/import", json={"path": str(source), "mood": "hype"}
        ).json()
        assert "path" not in imported["track"]

        lib = client.get("/api/music/library").json()
        assert all("path" not in t for t in lib["tracks"])

        mood = client.post("/api/music/mood", json={"mood": "hype"}).json()
        assert all("path" not in t for t in mood["tracks"])
