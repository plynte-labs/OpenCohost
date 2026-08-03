"""WU5 lock scope / TOCTOU / bounded shutdown (design id 2936).

Three reliability guards:
1. The (up to 200MB) import copy runs OUTSIDE host.music_lock — it must not
   block concurrent control-plane reads. The lock is taken only to register
   the finished track, and a save failure rolls back (pop + unlink staged) so
   a 503 never leaves a live track or an orphan file behind.
2. get_music_track_audio opens the file handle under the lock and hands it to
   FileResponse via BackgroundTask(fh.close). On Windows an open handle blocks
   unlink, so a concurrent DELETE surfaces the existing retryable 503 instead
   of tearing a live stream (or 500ing).
3. EngineHost.stop() bounds the Ollama unload with Client(timeout=2.0) so a
   hung Ollama cannot stall API shutdown.

Fixtures mirror tests/test_music_import_dedup.py / test_api_music_library_mutations.py.
"""

from __future__ import annotations

import os
import types
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


# ── 1. Copy outside the lock ────────────────────────────────────────────────


def test_import_copies_outside_music_lock(tmp_path: Path, monkeypatch):
    """The 200MB copy must not run under host.music_lock. Probe shutil.copy2:
    if the lock is free (acquirable) during the copy, the copy is outside the
    lock. On the old code the copy ran inside `with host.music_lock`, so a
    same-thread non-reentrant acquire returns False."""
    import opencohost.core.music.music_library as ml

    library = _make_library(tmp_path)
    source = tmp_path / "incoming" / "song.wav"
    _write_wav(source)
    app = _app(_host_with_library(library))

    real_copy2 = ml.shutil.copy2
    observed = {}

    with TestClient(app) as client:
        host = app.state.host

        def probe(src, dst, *a, **k):
            acquired = host.music_lock.acquire(blocking=False)
            observed["lock_free"] = acquired
            if acquired:
                host.music_lock.release()
            return real_copy2(src, dst, *a, **k)

        monkeypatch.setattr(ml.shutil, "copy2", probe)
        resp = client.post("/api/music/import", json={"path": str(source), "mood": "hype"})
        assert resp.status_code == 200

    assert observed.get("lock_free") is True


# ── 2. Rollback on save failure ─────────────────────────────────────────────


def test_import_save_failure_rolls_back_track_and_staged_file(tmp_path: Path, monkeypatch):
    """A save failure during register must leave NO live track in RAM and NO
    orphan copy on disk — a 503 must be a clean no-op, not a half-import."""
    import opencohost.core.music.music_library as ml

    library = _make_library(tmp_path)
    source = tmp_path / "incoming" / "song.wav"
    _write_wav(source)
    app = _app(_host_with_library(library))

    def boom(self):
        raise OSError("disk full")

    monkeypatch.setattr(ml.MusicLibrary, "save", boom)

    with TestClient(app) as client:
        resp = client.post("/api/music/import", json={"path": str(source), "mood": "hype"})
        assert resp.status_code == 503
        assert resp.json() == {"detail": "music_write_failed"}

    assert library.tracks == {}  # no live track left behind
    copied = list((tmp_path / "music").glob("*")) if (tmp_path / "music").exists() else []
    assert copied == []  # staged copy unlinked on rollback


# ── 3. Delete-during-stream: open handle -> retryable 503, not 500 ──────────


@pytest.mark.skipif(os.name != "nt", reason="handle-as-delete-guard is Windows semantics")
def test_delete_while_handle_open_returns_503_not_500(tmp_path: Path):
    library = _make_library(tmp_path)
    source = tmp_path / "incoming" / "song.wav"
    _write_wav(source)
    app = _app(_host_with_library(library))

    with TestClient(app) as client:
        imp = client.post("/api/music/import", json={"path": str(source), "mood": "hype"})
        track_id = imp.json()["track"]["id"]
        copied = list((tmp_path / "music").glob("*.wav"))[0]

        # Simulate an in-flight audio stream holding the file handle open.
        fh = copied.open("rb")
        try:
            resp = client.delete(f"/api/music/track/{track_id}")
            assert resp.status_code == 503
            assert resp.json() == {"detail": "music_write_failed"}
            # Retryable: the track is still registered (not dropped mid-stream).
            assert track_id in library.tracks
        finally:
            fh.close()

        # After the stream closes, DELETE succeeds.
        resp2 = client.delete(f"/api/music/track/{track_id}")
        assert resp2.status_code == 200


def test_remove_raising_true_propagates_and_keeps_track(tmp_path: Path, monkeypatch):
    """API path: a locked managed file -> _delete_managed_file raises -> remove()
    re-raises AND keeps the track registered, so the handler can return a
    retryable 503 (WU5/D8)."""
    library = _make_library(tmp_path)
    source = tmp_path / "incoming" / "song.wav"
    _write_wav(source)
    track = library.add_file(str(source), "hype")

    def boom(self, _t):
        raise OSError("handle open")

    monkeypatch.setattr(MusicLibrary, "_delete_managed_file", boom)
    with pytest.raises(OSError):
        library.remove(track.id, delete_file=True, raising=True)
    assert track.id in library.tracks  # kept for retryable 503


def test_remove_raising_false_swallows_and_deregisters(tmp_path: Path, monkeypatch):
    """CTK desktop default (raising=False): a locked managed file must NOT crash
    the Tk callback. remove() swallows the OSError, deregisters the track, and
    returns True (fail-open, file left orphaned — pre-WU5 behavior)."""
    library = _make_library(tmp_path)
    source = tmp_path / "incoming" / "song.wav"
    _write_wav(source)
    track = library.add_file(str(source), "hype")

    def boom(self, _t):
        raise OSError("locked")

    monkeypatch.setattr(MusicLibrary, "_delete_managed_file", boom)
    assert library.remove(track.id, delete_file=True) is True
    assert track.id not in library.tracks


def test_get_audio_still_serves_bytes(tmp_path: Path):
    """Regression: opening the handle under the lock + BackgroundTask close
    must not change the served bytes."""
    library = _make_library(tmp_path)
    source = tmp_path / "incoming" / "song.wav"
    _write_wav(source)
    app = _app(_host_with_library(library))

    with TestClient(app) as client:
        imp = client.post("/api/music/import", json={"path": str(source), "mood": "hype"})
        track_id = imp.json()["track"]["id"]
        resp = client.get(f"/api/music/track/{track_id}/audio")
        assert resp.status_code == 200
        assert resp.content == b"RIFF____WAVE"


# ── 4. Bounded Ollama unload on shutdown ────────────────────────────────────


def test_stop_bounds_ollama_unload_with_timeout_client():
    """stop() must unload via a timeout-bounded Client, NOT the unbounded
    module-level ollama.generate(). A fake cannot exercise real httpx timeouts
    (the runtime drill covers that); this is the contract test."""
    import opencohost.api.engine_host as eh

    calls: dict = {}

    class RecorderClient:
        def __init__(self, **kwargs):
            calls["client_kwargs"] = kwargs

        def generate(self, **kwargs):
            calls["generate_kwargs"] = kwargs

    fake_ollama = types.SimpleNamespace(
        Client=RecorderClient,
        generate=lambda **k: calls.setdefault("module_generate", k),
    )

    host = eh.EngineHost.__new__(eh.EngineHost)
    host._lock_fd = None
    host.aggregator = None
    host.monitor = None
    host.motor = types.SimpleNamespace(
        current_model="qwen3", command_queue=types.SimpleNamespace(put=lambda _x: None)
    )

    import unittest.mock as mock

    with mock.patch.object(eh, "ollama", fake_ollama):
        host.stop()

    assert calls.get("client_kwargs") == {"timeout": 2.0}
    assert calls.get("generate_kwargs", {}).get("keep_alive") == 0
    assert calls["generate_kwargs"]["model"] == "qwen3"
    assert "module_generate" not in calls  # bounded client, not the unbounded call
