"""Tests for POST /api/music/mood empty-bucket fallback pool (WU1, backend).

Bug: when the requested mood bucket has no tracks, the endpoint returned an
empty `tracks` list but a single deterministic `suggested_track_id`. The
Tauri client rotates over `tracks` when non-empty, but falls back to the
lone `suggested_track_id` when `tracks` is empty — so an empty-bucket mood
always played the SAME song.

Fix: when the mood bucket is empty, populate `tracks` with the same
fallback pool `MusicLibrary.select_for_mood` draws from (normal-tagged
valid tracks, else all valid tracks) and set `fallback=True`. A populated
mood bucket is unchanged (`fallback=False`, byte-identical response).

Fixtures mirror tests/test_api_music_state.py exactly.
"""

import pytest
from fastapi.testclient import TestClient

from opencohost.core.music.music_library import MusicLibrary, MusicTrack
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
    return MusicLibrary(library_dir=tmp_path / "music", config_file=tmp_path / "music_library.json")


def _write_wav(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"RIFF____WAVE")


def _track(tmp_path, track_id, mood):
    wav = tmp_path / "music" / f"{track_id}.wav"
    _write_wav(wav)
    return MusicTrack(id=track_id, original_name=f"{track_id}.wav", path=str(wav), mood=mood, label=mood)


def test_post_music_mood_empty_bucket_returns_normal_fallback_pool(tmp_path):
    """Empty 'sad' bucket, but a 'normal' bucket exists -> tracks is the
    normal pool (not empty), fallback=True, suggested_track_id is in the pool."""
    library = _make_library(tmp_path)
    library.tracks = {
        "n1": _track(tmp_path, "n1", "normal"),
        "n2": _track(tmp_path, "n2", "normal"),
    }

    app = _app(_host_with_library(library))
    with TestClient(app) as client:
        resp = client.post("/api/music/mood", json={"mood": "sad"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["active_mood"] == "sad"
        assert {t["id"] for t in body["tracks"]} == {"n1", "n2"}
        assert body["fallback"] is True
        assert body["suggested_track_id"] in {"n1", "n2"}


def test_post_music_mood_empty_bucket_no_normal_falls_back_to_any(tmp_path):
    """Empty 'sad' bucket AND empty 'normal' bucket -> tracks falls back to
    ALL valid tracks (mirrors select_for_mood's mood->normal->any chain)."""
    library = _make_library(tmp_path)
    library.tracks = {"h1": _track(tmp_path, "h1", "hype")}

    app = _app(_host_with_library(library))
    with TestClient(app) as client:
        resp = client.post("/api/music/mood", json={"mood": "sad"})
        assert resp.status_code == 200
        body = resp.json()
        assert [t["id"] for t in body["tracks"]] == ["h1"]
        assert body["fallback"] is True
        assert body["suggested_track_id"] == "h1"


def test_post_music_mood_populated_bucket_is_byte_identical(tmp_path):
    """A populated mood bucket is UNCHANGED: same tracks, fallback=False."""
    library = _make_library(tmp_path)
    library.tracks = {
        "h1": _track(tmp_path, "h1", "hype"),
        "n1": _track(tmp_path, "n1", "normal"),
    }

    app = _app(_host_with_library(library))
    with TestClient(app) as client:
        resp = client.post("/api/music/mood", json={"mood": "hype"})
        assert resp.status_code == 200
        body = resp.json()
        assert [t["id"] for t in body["tracks"]] == ["h1"]
        assert body["suggested_track_id"] == "h1"
        assert body["fallback"] is False
        assert set(body.keys()) == {"active_mood", "tracks", "suggested_track_id", "fallback"}


def test_post_music_mood_unknown_mood_still_422(tmp_path):
    library = _make_library(tmp_path)
    library.tracks = {"n1": _track(tmp_path, "n1", "normal")}

    app = _app(_host_with_library(library))
    with TestClient(app) as client:
        resp = client.post("/api/music/mood", json={"mood": "not_a_real_mood"})
        assert resp.status_code == 422
