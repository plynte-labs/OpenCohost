"""Tests for the headless Music Library endpoint (WS3 slice 4): GET
/api/music/library — Tier-B sync-def direct read, no queue, no audio.

MusicLibrary is a real, Tk-free, pure file/JSON state reader (no pygame, no
audio device) — tests use the REAL library with `library_dir`/`config_file`
pointed at `tmp_path`, never the real music config/dir. Server-side audio
playback (`request_mood`) is deferred — this slice is READ ONLY.
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


def _write_wav(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"RIFF____WAVE")


# ──────────────────────────────────────────────────────────────────────────
# GET /api/music/library — shape + statuses + 503 when unavailable
# ──────────────────────────────────────────────────────────────────────────


def test_get_music_library_reflects_track_statuses(tmp_path):
    library = MusicLibrary(library_dir=tmp_path / "music", config_file=tmp_path / "music_library.json")

    ok_path = tmp_path / "music" / "ok_track.wav"
    _write_wav(ok_path)
    invalid_path = tmp_path / "music" / "bad_track.wav"
    invalid_path.parent.mkdir(parents=True, exist_ok=True)
    invalid_path.write_bytes(b"NOT_AUDIO_DATA")
    missing_path = tmp_path / "music" / "gone_track.wav"

    library.tracks = {
        "ok-1": MusicTrack(id="ok-1", original_name="ok.wav", path=str(ok_path), mood="hype", label="hype"),
        "missing-1": MusicTrack(
            id="missing-1", original_name="gone.wav", path=str(missing_path), mood="sad", label="sad"
        ),
        "invalid-1": MusicTrack(
            id="invalid-1", original_name="bad.wav", path=str(invalid_path), mood="calm", label="calm"
        ),
    }

    app = _app(_host_with_library(library))
    with TestClient(app) as client:
        resp = client.get("/api/music/library")
        assert resp.status_code == 200
        body = resp.json()
        assert body["count"] == 3
        by_id = {track["id"]: track for track in body["tracks"]}
        assert by_id["ok-1"]["status"] == "ok"
        assert by_id["ok-1"]["mood"] == "hype"
        assert by_id["missing-1"]["status"] == "faltante"
        assert by_id["invalid-1"]["status"] == "invalido"
        assert set(body["moods"]) == {"hype", "sad", "calm"}


def test_get_music_library_503_when_unavailable():
    app = _app(_host_with_library(None))
    with TestClient(app) as client:
        resp = client.get("/api/music/library")
        assert resp.status_code == 503
        assert resp.json()["detail"] == "music_unavailable"


def test_get_music_library_never_writes_real_config(tmp_path, monkeypatch):
    """Guard against the test accidentally touching the real MUSIC_CONFIG_FILE
    — the library instance under test must be rooted at tmp_path only."""
    library = MusicLibrary(library_dir=tmp_path / "music", config_file=tmp_path / "music_library.json")
    assert str(tmp_path) in str(library.config_file)
    assert str(tmp_path) in str(library.library_dir)


# ──────────────────────────────────────────────────────────────────────────
# EngineHost resilient music library construction (mirrors the Agenda/
# Aggregator pattern)
# ──────────────────────────────────────────────────────────────────────────


def test_engine_host_start_survives_music_library_construction_failure(tmp_path, monkeypatch):
    """A host whose MusicLibrary ctor raises still start()s with
    music_library=None — engine/profiles/commands must not be bricked by a
    music-library failure. GET 503 for this state is covered generically by
    test_get_music_library_503_when_unavailable via FakeHost."""
    import opencohost.api.engine_host as engine_host_mod
    from queue import Queue
    from unittest.mock import MagicMock

    fake_motor = MagicMock()
    fake_motor.command_queue = Queue()
    fake_motor.current_model = None
    fake_monitor = MagicMock()

    monkeypatch.setattr(engine_host_mod, "MotorVocalIA", lambda *a, **kw: fake_motor)
    monkeypatch.setattr(engine_host_mod, "HealthMonitor", lambda: fake_monitor)
    monkeypatch.setattr(engine_host_mod, "ollama", MagicMock())
    monkeypatch.setattr(
        engine_host_mod,
        "MusicLibrary",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("music ctor boom")),
    )

    host = engine_host_mod.EngineHost(lock_path=str(tmp_path / "engine.lock"))
    try:
        host.start()  # must not raise
        assert host.music_library is None
        assert host.motor is fake_motor  # engine still came up
    finally:
        host.stop()
