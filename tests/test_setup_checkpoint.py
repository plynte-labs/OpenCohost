"""Setup checkpoint (setup-wizard-simple S10): resume an interrupted setup.

The checkpoint lives in its own file next to the setup status, so a corrupt
checkpoint can be moved aside without touching the status. Ollama and the key
store are faked at the ``llm_readiness`` seams.
"""

import json
import logging
import os

import pytest
from fastapi.testclient import TestClient

from opencohost.config import settings, setup_checkpoint
from opencohost.core.engine import llm_readiness
from tests.test_api_phase1 import FakeHost

_ORIGINS = ["http://localhost:5173"]


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    import opencohost.api.main as main_mod

    main_mod._host_active = False
    monkeypatch.setattr(settings, "SETUP_STATE_FILE", str(tmp_path / "setup_state.json"))
    monkeypatch.setattr(settings, "SETUP_CHECKPOINT_FILE", str(tmp_path / "setup_checkpoint.json"))
    monkeypatch.setattr(llm_readiness, "_ping_ollama", lambda *a, **k: (False, None, []))
    monkeypatch.setattr(llm_readiness, "_check_cloud_key_configured", lambda *a, **k: False)
    yield
    main_mod._host_active = False


def _app():
    import opencohost.api.main as main_mod

    return main_mod.create_app(host_factory=FakeHost, cors_origins=_ORIGINS)


_ANSWERS = {
    "language": "en",
    "hear_kira": False,
    "engine": "cpu",
    "model": "qwen3:1.7b",
    "model_ready": True,
    "voice": "ok",
    "voice_kind": "piper",
    "test_verdict": "passed",
    "personalize": True,
    "avatar": "2d",
}


def _write_raw(text: str) -> None:
    with open(settings.SETUP_CHECKPOINT_FILE, "w", encoding="utf-8") as f:
        f.write(text)


def _aside_files(tmp_path):
    return sorted(p.name for p in tmp_path.iterdir() if ".corrupt-" in p.name)


# ── store ────────────────────────────────────────────────────────────────────


def test_missing_file_means_no_checkpoint_and_no_reset():
    result = setup_checkpoint.load_checkpoint()
    assert result.checkpoint is None and result.reset is None


def test_save_then_load_round_trips_allowlisted_answers():
    setup_checkpoint.save_checkpoint({"version": 1, "step": "voice", "answers": _ANSWERS})
    result = setup_checkpoint.load_checkpoint()
    assert result.reset is None
    assert result.checkpoint["step"] == "voice"
    assert result.checkpoint["version"] == 1
    assert result.checkpoint["answers"]["model"] == "qwen3:1.7b"
    assert result.checkpoint["answers"]["avatar"] == "2d"
    assert isinstance(result.checkpoint["updated_at"], str) and result.checkpoint["updated_at"]


def test_save_drops_unknown_and_secret_fields():
    answers = {**_ANSWERS, "api_key": "sk-secret-123", "tavily_key": "tvly-xyz", "chat": "hola"}
    setup_checkpoint.save_checkpoint({"version": 1, "step": "voice", "answers": answers, "api_key": "sk-top"})
    with open(settings.SETUP_CHECKPOINT_FILE, encoding="utf-8") as f:
        raw = f.read()
    assert "sk-secret" not in raw and "tvly" not in raw and "hola" not in raw and "sk-top" not in raw
    stored = json.loads(raw)
    assert set(stored) == {"version", "step", "answers", "updated_at"}


def test_save_writes_atomically_through_the_shared_helper(monkeypatch):
    calls = []

    def fake(path, text):
        calls.append(path)

    monkeypatch.setattr(setup_checkpoint, "atomic_write_text", fake)
    setup_checkpoint.save_checkpoint({"version": 1, "step": "engine", "answers": {}})
    assert calls == [settings.SETUP_CHECKPOINT_FILE]


@pytest.mark.parametrize(
    "raw",
    [
        "{not json",
        json.dumps(["x"]),
        json.dumps({"version": 1, "step": "voice", "answers": {"engine": "rocket"}}),
        json.dumps({"version": 1, "step": "has space", "answers": {}}),
        json.dumps({"version": 1, "answers": {}}),
        json.dumps({"version": 1, "step": "voice", "answers": "nope"}),
    ],
)
def test_corrupt_file_is_moved_aside_and_reported(tmp_path, raw, caplog):
    _write_raw(raw)
    with caplog.at_level(logging.WARNING):
        result = setup_checkpoint.load_checkpoint()
    assert result.checkpoint is None and result.reset == "corrupt"
    assert not os.path.exists(settings.SETUP_CHECKPOINT_FILE)
    assert len(_aside_files(tmp_path)) == 1
    # One line, reason only: never the file content.
    assert all(raw not in record.getMessage() for record in caplog.records)
    # The next read is clean: the bad file is gone.
    again = setup_checkpoint.load_checkpoint()
    assert again.checkpoint is None and again.reset is None


@pytest.mark.parametrize("version", [0, 2, 99, "1", None])
def test_unknown_version_is_moved_aside(tmp_path, version):
    _write_raw(json.dumps({"version": version, "step": "voice", "answers": {}}))
    result = setup_checkpoint.load_checkpoint()
    assert result.checkpoint is None and result.reset == "unsupported_version"
    assert len(_aside_files(tmp_path)) == 1


def test_oversized_file_is_treated_as_corrupt(tmp_path):
    _write_raw(json.dumps({"version": 1, "step": "voice", "answers": {}, "pad": "x" * 40_000}))
    result = setup_checkpoint.load_checkpoint()
    assert result.checkpoint is None and result.reset == "corrupt"


def test_move_aside_failure_never_raises(tmp_path, monkeypatch):
    _write_raw("{bad")

    def fail(*a, **k):
        raise OSError("locked")

    monkeypatch.setattr(setup_checkpoint.os, "replace", fail)
    monkeypatch.setattr(setup_checkpoint.os, "remove", fail)
    result = setup_checkpoint.load_checkpoint()
    assert result.checkpoint is None and result.reset == "corrupt"


def test_clear_removes_the_file_and_missing_is_fine():
    setup_checkpoint.save_checkpoint({"version": 1, "step": "voice", "answers": {}})
    setup_checkpoint.clear_checkpoint()
    assert not os.path.exists(settings.SETUP_CHECKPOINT_FILE)
    setup_checkpoint.clear_checkpoint()  # already gone: no error


def test_save_rejects_unknown_version():
    with pytest.raises(ValueError):
        setup_checkpoint.save_checkpoint({"version": 7, "step": "voice", "answers": {}})


# ── API ──────────────────────────────────────────────────────────────────────


def test_state_reports_no_checkpoint_by_default():
    with TestClient(_app()) as client:
        body = client.get("/api/setup/state").json()
    assert body["checkpoint"] is None and body["checkpoint_reset"] is None


def test_put_checkpoint_then_state_returns_it():
    with TestClient(_app()) as client:
        resp = client.put("/api/setup/checkpoint", json={"version": 1, "step": "model", "answers": _ANSWERS})
        assert resp.status_code == 200
        assert resp.json()["checkpoint"]["step"] == "model"
        body = client.get("/api/setup/state").json()
    assert body["checkpoint"]["step"] == "model"
    assert body["checkpoint"]["answers"]["voice_kind"] == "piper"
    assert "api_key" not in json.dumps(body)


def test_put_checkpoint_drops_secret_fields_from_the_response():
    with TestClient(_app()) as client:
        resp = client.put(
            "/api/setup/checkpoint",
            json={"version": 1, "step": "apiKey", "answers": {"engine": "cloud", "api_key": "sk-leak"}},
        )
    assert resp.status_code == 200 and "sk-leak" not in resp.text


@pytest.mark.parametrize(
    "payload",
    [
        {"version": 2, "step": "voice", "answers": {}},
        {"version": 1, "step": "x" * 200, "answers": {}},
        {"version": 1, "step": "voice", "answers": {"model": "m" * 500}},
        {"version": 1, "step": "voice", "answers": {"engine": "rocket"}},
        {"step": "voice", "answers": {}},
    ],
)
def test_put_invalid_checkpoint_is_422(payload):
    with TestClient(_app()) as client:
        assert client.put("/api/setup/checkpoint", json=payload).status_code == 422


def test_put_checkpoint_write_failure_is_503(monkeypatch):
    def fail(*a, **k):
        raise OSError("disk")

    monkeypatch.setattr(setup_checkpoint, "atomic_write_text", fail)
    with TestClient(_app()) as client:
        resp = client.put("/api/setup/checkpoint", json={"version": 1, "step": "voice", "answers": {}})
    assert resp.status_code == 503
    assert resp.json()["detail"] == "setup_checkpoint_write_failed"


def test_delete_checkpoint_clears_it():
    with TestClient(_app()) as client:
        client.put("/api/setup/checkpoint", json={"version": 1, "step": "voice", "answers": {}})
        resp = client.delete("/api/setup/checkpoint")
        assert resp.status_code == 200 and resp.json() == {"checkpoint": None}
        assert client.get("/api/setup/state").json()["checkpoint"] is None


def test_completing_setup_clears_the_checkpoint():
    with TestClient(_app()) as client:
        client.put("/api/setup/checkpoint", json={"version": 1, "step": "voice", "answers": {}})
        body = client.put("/api/setup/state", json={"status": "completed"}).json()
        assert body["checkpoint"] is None
    assert not os.path.exists(settings.SETUP_CHECKPOINT_FILE)


def test_skipping_setup_keeps_the_checkpoint():
    with TestClient(_app()) as client:
        client.put("/api/setup/checkpoint", json={"version": 1, "step": "voice", "answers": {}})
        body = client.put("/api/setup/state", json={"status": "skipped"}).json()
    assert body["checkpoint"]["step"] == "voice"


def test_corrupt_checkpoint_is_reported_once_through_state(tmp_path):
    _write_raw("{oops")
    with TestClient(_app()) as client:
        first = client.get("/api/setup/state").json()
        second = client.get("/api/setup/state").json()
    assert first["checkpoint"] is None and first["checkpoint_reset"] == "corrupt"
    assert second["checkpoint_reset"] is None
    # The status itself is untouched by a bad checkpoint.
    assert first["status"] == "pending"


def test_corrupt_checkpoint_never_breaks_a_completed_status(tmp_path):
    with open(settings.SETUP_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump({"status": "completed"}, f)
    _write_raw("{oops")
    with TestClient(_app()) as client:
        body = client.get("/api/setup/state").json()
    assert body["status"] == "completed"
