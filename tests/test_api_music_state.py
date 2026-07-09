"""Tests for music mood + state orchestration (phase 2 WU1).

POST /api/music/mood and GET /api/music/state under the client-side-playback
model ([[tauri/music-orchestration-model]]): the API holds WHAT should be
playing (active mood) and mediates the library; it NEVER drives backend audio.
The headless host has no AudioBedEngine — these tests prove mood succeeds
anyway and the response carries only orchestration fields (no playback/audio).

Fixtures mirror tests/test_api_music.py exactly: a REAL, Tk-free MusicLibrary
rooted at `tmp_path` (never the real config/dir), `FakeHost` from
tests/test_api_phase1 (now always carrying `music_state`/`music_lock`),
`_reset_host_active` autouse, `create_app(host_factory=...)`.
"""

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


def _track(tmp_path, track_id, mood):
    wav = tmp_path / "music" / f"{track_id}.wav"
    _write_wav(wav)
    return MusicTrack(
        id=track_id, original_name=f"{track_id}.wav", path=str(wav), mood=mood, label=mood
    )


# ──────────────────────────────────────────────────────────────────────────
# POST /api/music/mood — sets orchestration state, returns bucket + suggestion
# ──────────────────────────────────────────────────────────────────────────


def test_post_music_mood_sets_active_mood_and_state_reflects(tmp_path):
    library = _make_library(tmp_path)
    library.tracks = {"h1": _track(tmp_path, "h1", "hype")}

    app = _app(_host_with_library(library))
    with TestClient(app) as client:
        resp = client.post("/api/music/mood", json={"mood": "hype"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["active_mood"] == "hype"
        assert [t["id"] for t in body["tracks"]] == ["h1"]
        assert body["suggested_track_id"] == "h1"

        # A reconnecting/second client can recover the mood via the read route.
        state = client.get("/api/music/state")
        assert state.status_code == 200
        assert state.json()["active_mood"] == "hype"


def test_post_music_mood_normalizes_case_and_whitespace(tmp_path):
    library = _make_library(tmp_path)
    library.tracks = {"h1": _track(tmp_path, "h1", "hype")}

    app = _app(_host_with_library(library))
    with TestClient(app) as client:
        resp = client.post("/api/music/mood", json={"mood": "  HYPE  "})
        assert resp.status_code == 200
        assert resp.json()["active_mood"] == "hype"


def test_post_music_mood_unknown_422_and_state_unchanged(tmp_path):
    library = _make_library(tmp_path)
    library.tracks = {"n1": _track(tmp_path, "n1", "normal")}

    app = _app(_host_with_library(library))
    with TestClient(app) as client:
        # Set a known mood first (calm is a KNOWN_MOODS value even with an
        # empty bucket — POST mood never 422s on an empty bucket, only on an
        # unknown mood name).
        assert client.post("/api/music/mood", json={"mood": "calm"}).status_code == 200

        bad = client.post("/api/music/mood", json={"mood": "not_a_real_mood"})
        assert bad.status_code == 422

        # No partial mutation: the rejected mood never touched the state.
        state = client.get("/api/music/state")
        assert state.json()["active_mood"] == "calm"


def test_post_music_mood_empty_bucket_falls_back_to_normal(tmp_path):
    library = _make_library(tmp_path)
    # Only a normal-bucket track exists; the requested 'sad' bucket is empty.
    library.tracks = {"n1": _track(tmp_path, "n1", "normal")}

    app = _app(_host_with_library(library))
    with TestClient(app) as client:
        resp = client.post("/api/music/mood", json={"mood": "sad"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["active_mood"] == "sad"
        # No 'sad' valid tracks -> tracks falls back to the normal pool so the
        # client can still rotate (tests/test_api_music_mood_fallback.py).
        assert [t["id"] for t in body["tracks"]] == ["n1"]
        assert body["fallback"] is True
        # ...and select_for_mood's mood->normal->any chain still suggests one.
        assert body["suggested_track_id"] == "n1"


def test_music_mood_and_state_503_when_library_none():
    app = _app(_host_with_library(None))
    with TestClient(app) as client:
        m = client.post("/api/music/mood", json={"mood": "hype"})
        assert m.status_code == 503
        assert m.json() == {"detail": "music_unavailable"}

        s = client.get("/api/music/state")
        assert s.status_code == 503
        assert s.json() == {"detail": "music_unavailable"}


def test_post_music_mood_no_backend_audio(tmp_path):
    """Orchestration-only proof: the headless host has NO AudioBedEngine, the
    mood POST still succeeds, and the response body carries only orchestration
    fields — never a playback/audio field."""
    library = _make_library(tmp_path)
    library.tracks = {"h1": _track(tmp_path, "h1", "hype")}

    app = _app(_host_with_library(library))
    with TestClient(app) as client:
        host = app.state.host
        assert not hasattr(host, "audio_bed_engine")

        resp = client.post("/api/music/mood", json={"mood": "hype"})
        assert resp.status_code == 200
        assert set(resp.json().keys()) == {"active_mood", "tracks", "suggested_track_id", "fallback"}


def test_get_music_state_default_before_any_post(tmp_path):
    library = _make_library(tmp_path)

    app = _app(_host_with_library(library))
    with TestClient(app) as client:
        resp = client.get("/api/music/state")
        assert resp.status_code == 200
        body = resp.json()
        assert body["active_mood"] == "normal"
        assert body["fade"] is None


# ──────────────────────────────────────────────────────────────────────────
# POST /api/music/fade — records a fade INTENT (client executes it), WU2
# ──────────────────────────────────────────────────────────────────────────


def test_post_music_fade_seq_is_monotonic(tmp_path):
    library = _make_library(tmp_path)

    app = _app(_host_with_library(library))
    with TestClient(app) as client:
        first = client.post("/api/music/fade", json={"direction": "out"})
        assert first.status_code == 200
        assert first.json()["fade"]["seq"] == 1

        # A second fade (either direction) advances seq — never resets.
        second = client.post("/api/music/fade", json={"direction": "in"})
        assert second.status_code == 200
        assert second.json()["fade"]["seq"] == 2


def test_post_music_fade_state_reflects_same_intent(tmp_path):
    library = _make_library(tmp_path)

    app = _app(_host_with_library(library))
    with TestClient(app) as client:
        posted = client.post(
            "/api/music/fade", json={"direction": "out", "duration_ms": 3000}
        ).json()["fade"]

        # GET carries the SAME intent (state, not just an ack).
        state = client.get("/api/music/state").json()["fade"]
        assert state["seq"] == posted["seq"]
        assert state["direction"] == posted["direction"] == "out"
        assert state["duration_ms"] == posted["duration_ms"] == 3000


def test_post_music_fade_unknown_direction_422_and_state_unchanged(tmp_path):
    library = _make_library(tmp_path)

    app = _app(_host_with_library(library))
    with TestClient(app) as client:
        assert client.post("/api/music/fade", json={"direction": "out"}).status_code == 200

        bad = client.post("/api/music/fade", json={"direction": "sideways"})
        assert bad.status_code == 422

        # No partial mutation: the rejected fade never advanced seq/state.
        state = client.get("/api/music/state").json()["fade"]
        assert state["seq"] == 1
        assert state["direction"] == "out"


def test_post_music_fade_defaults_duration(tmp_path):
    library = _make_library(tmp_path)

    app = _app(_host_with_library(library))
    with TestClient(app) as client:
        resp = client.post("/api/music/fade", json={"direction": "in"})
        assert resp.status_code == 200
        assert resp.json()["fade"]["duration_ms"] == 6000  # _MUSIC_FADE_DEFAULT_MS


def test_post_music_fade_duration_out_of_range_422(tmp_path):
    library = _make_library(tmp_path)

    app = _app(_host_with_library(library))
    with TestClient(app) as client:
        # 0 is below the (0, MAX] open-lower bound.
        assert client.post(
            "/api/music/fade", json={"direction": "out", "duration_ms": 0}
        ).status_code == 422
        # 70000 exceeds _MUSIC_FADE_MAX_MS (60000).
        assert client.post(
            "/api/music/fade", json={"direction": "out", "duration_ms": 70000}
        ).status_code == 422


def test_post_music_fade_503_when_library_none():
    app = _app(_host_with_library(None))
    with TestClient(app) as client:
        resp = client.post("/api/music/fade", json={"direction": "out"})
        assert resp.status_code == 503
        assert resp.json() == {"detail": "music_unavailable"}

        # No seq increment: the fade intent was never recorded.
        assert app.state.host.music_state.snapshot()["fade"] is None


def test_post_music_fade_no_backend_audio(tmp_path):
    """Orchestration-only proof: the headless host has NO AudioBedEngine, the
    fade POST still succeeds, and the recorded intent carries only
    direction/duration_ms/seq/ts — never a volume/playback field."""
    library = _make_library(tmp_path)

    app = _app(_host_with_library(library))
    with TestClient(app) as client:
        host = app.state.host
        assert not hasattr(host, "audio_bed_engine")

        resp = client.post("/api/music/fade", json={"direction": "out"})
        assert resp.status_code == 200
        assert set(resp.json()["fade"].keys()) == {"direction", "duration_ms", "seq", "ts"}
