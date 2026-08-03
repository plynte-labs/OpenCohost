"""WU4 idempotency: MusicLibrary import dedup by (source_sig, mood).

A retry / double-click / reconnect re-sends the SAME source path. Importing
it again must return the existing track instead of copying the audio a second
time. Dedup key = (source_sig, mood) where source_sig = resolved-path|size|
mtime_ns. The same file to a DIFFERENT mood is intentionally a new variant.

Fixtures mirror tests/test_api_music_library_mutations.py: a REAL Tk-free
MusicLibrary rooted at tmp_path, FakeHost, create_app(host_factory=...).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from opencohost.core.music.music_library import MusicLibrary
from tests.test_api_phase1 import FakeHost

_DEFAULT_TEST_ORIGINS = ["http://localhost:5173"]


def _write_wav(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"RIFF____WAVE")


def _make_library(tmp_path: Path) -> MusicLibrary:
    return MusicLibrary(
        library_dir=tmp_path / "music", config_file=tmp_path / "music_library.json"
    )


# ── Direct-unit dedup ──────────────────────────────────────────────────────


def test_add_file_same_source_same_mood_returns_existing_track(tmp_path: Path):
    source = tmp_path / "incoming" / "song.wav"
    _write_wav(source)
    library = _make_library(tmp_path)

    first = library.add_file(source, "hype")
    second = library.add_file(source, "hype")

    assert first.id == second.id
    assert len(library.tracks) == 1
    # Exactly one on-disk copy — the second import must NOT re-copy.
    copied = list((tmp_path / "music").glob("*.wav"))
    assert len(copied) == 1


def test_add_file_same_source_different_mood_creates_variant(tmp_path: Path):
    source = tmp_path / "incoming" / "song.wav"
    _write_wav(source)
    library = _make_library(tmp_path)

    a = library.add_file(source, "hype")
    b = library.add_file(source, "calm")

    assert a.id != b.id
    assert len(library.tracks) == 2
    assert len(list((tmp_path / "music").glob("*.wav"))) == 2


def test_add_file_source_sig_persists_and_dedups_after_reload(tmp_path: Path):
    source = tmp_path / "incoming" / "song.wav"
    _write_wav(source)
    library = _make_library(tmp_path)
    first = library.add_file(source, "hype")
    assert first.source_sig  # signature was computed and stored

    reloaded = _make_library(tmp_path)
    reloaded.load()
    assert reloaded.tracks[first.id].source_sig == first.source_sig

    # Re-importing the same source into a fresh library instance still dedups
    # because the signature was persisted.
    again = reloaded.add_file(source, "hype")
    assert again.id == first.id
    assert len(reloaded.tracks) == 1


# ── API-level dedup (POST /api/music/import twice) ─────────────────────────


def _host_with_library(library):
    def factory():
        host = FakeHost()
        host.music_library = library
        return host

    return factory


def _app(host_factory):
    import opencohost.api.main as main_mod

    return main_mod.create_app(host_factory=host_factory, cors_origins=_DEFAULT_TEST_ORIGINS)


@pytest.fixture(autouse=True)
def _reset_host_active():
    import opencohost.api.main as main_mod

    main_mod._host_active = False
    yield
    main_mod._host_active = False


def test_post_import_twice_is_idempotent(tmp_path: Path):
    library = _make_library(tmp_path)
    source = tmp_path / "incoming" / "song.wav"
    _write_wav(source)
    app = _app(_host_with_library(library))
    with TestClient(app) as client:
        body = {"path": str(source), "mood": "hype"}
        first = client.post("/api/music/import", json=body)
        second = client.post("/api/music/import", json=body)
        assert first.status_code == 200
        assert second.status_code == 200
        assert first.json()["track"]["id"] == second.json()["track"]["id"]

    assert len(library.tracks) == 1
    assert len(list((tmp_path / "music").glob("*.wav"))) == 1
