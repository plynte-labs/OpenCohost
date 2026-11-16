"""API contract for /api/models/downloads and /api/models/hf/.../quants
(model-download-manager T3).

The download manager is injected through ``create_app``'s
``download_manager_factory`` with a scripted pull, Ollama reachability and
VRAM detection are monkeypatched, and the HF API is faked -- nothing here
reaches a real Ollama or the network.
"""

from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient

from opencohost.core.engine import llm_readiness
from opencohost.core.model_downloads import hf_quants
from opencohost.core.model_downloads.manager import ModelDownloadManager
from tests.test_api_phase1 import FakeHost
from tests.test_model_download_manager import SUCCESS_TAIL, Recorder, ScriptedPull, layer

_ORIGINS = ["http://localhost:5173"]


@pytest.fixture(autouse=True)
def _reset_host_active():
    import opencohost.api.main as main_mod

    main_mod._host_active = False
    yield
    main_mod._host_active = False


@pytest.fixture
def reachable(monkeypatch):
    state = {"up": True}
    monkeypatch.setattr(llm_readiness, "_ping_ollama", lambda timeout=1.5: (state["up"], "fake", []))
    monkeypatch.setattr(llm_readiness, "_detect_gpu_hardware", lambda: ("Fake GPU", 8192))
    return state


def _app(tmp_path, pull):
    import opencohost.api.main as main_mod

    rec = Recorder()

    def factory(host):
        return ModelDownloadManager(
            tmp_path / "model_downloads.json",
            pull_fn=pull,
            discover_fn=lambda: ["llama3"],
            on_event=rec.event,
            wait_fn=rec.wait,
        )

    return main_mod.create_app(
        host_factory=FakeHost, cors_origins=_ORIGINS, download_manager_factory=factory
    )


def test_post_starts_job_and_get_reports_progress(tmp_path, reachable):
    pull = ScriptedPull([layer("sha256:a", 100, 50), layer("sha256:a", 100, 100), *SUCCESS_TAIL])
    app = _app(tmp_path, pull)
    with TestClient(app) as client:
        resp = client.post("/api/models/downloads", json={"tag": "llama3"})
        assert resp.status_code == 202
        job = resp.json()
        assert job["tag"] == "llama3"
        assert job["state"] in {"queued", "downloading", "completed"}
        assert app.state.download_manager.join(5)

        listing = client.get("/api/models/downloads")
        assert listing.status_code == 200
        body = listing.json()
        assert body["active_job_id"] is None
        assert body["jobs"][0]["id"] == job["id"]
        assert body["jobs"][0]["state"] == "completed"
        assert body["jobs"][0]["percent"] == 100.0
        assert body["jobs"][0]["installed"] is True
        assert body["max_retries"] == 3


@pytest.mark.parametrize("payload", [{"tag": "https://evil/x"}, {"tag": "../x"}, {"tag": ""}])
def test_post_invalid_tag_is_422(tmp_path, reachable, payload):
    with TestClient(_app(tmp_path, ScriptedPull())) as client:
        resp = client.post("/api/models/downloads", json=payload)
        assert resp.status_code == 422
        assert resp.json()["detail"] == "invalid_tag"


def test_post_missing_tag_is_422(tmp_path, reachable):
    with TestClient(_app(tmp_path, ScriptedPull())) as client:
        assert client.post("/api/models/downloads", json={}).status_code == 422


def test_post_when_ollama_unreachable_is_503(tmp_path, reachable):
    reachable["up"] = False
    pull = ScriptedPull()
    with TestClient(_app(tmp_path, pull)) as client:
        resp = client.post("/api/models/downloads", json={"tag": "llama3"})
        assert resp.status_code == 503
        assert resp.json()["detail"] == "ollama_unreachable"
    assert pull.calls == []


def test_post_while_active_is_409(tmp_path, reachable):
    release = threading.Event()
    started = threading.Event()

    def block():
        started.set()
        release.wait(5)

    pull = ScriptedPull([layer("sha256:a", 10, 1), block, layer("sha256:a", 10, 10), *SUCCESS_TAIL])
    app = _app(tmp_path, pull)
    with TestClient(app) as client:
        first = client.post("/api/models/downloads", json={"tag": "llama3"}).json()
        assert started.wait(5)
        resp = client.post("/api/models/downloads", json={"tag": "gemma4:e2b"})
        assert resp.status_code == 409
        assert resp.json() == {"detail": "download_active", "active_job_id": first["id"]}
        listing = client.get("/api/models/downloads").json()
        assert listing["active_job_id"] == first["id"]
        release.set()
        assert app.state.download_manager.join(5)


def test_pause_resume_cancel_contract(tmp_path, reachable):
    release = threading.Event()
    started = threading.Event()

    def block():
        started.set()
        release.wait(5)

    pull = ScriptedPull(
        [layer("sha256:a", 10, 1), block, layer("sha256:a", 10, 2)],
        [layer("sha256:a", 10, 3), *SUCCESS_TAIL[:0]],
    )
    app = _app(tmp_path, pull)
    mgr_join = lambda: app.state.download_manager.join(5)  # noqa: E731
    with TestClient(app) as client:
        job_id = client.post("/api/models/downloads", json={"tag": "llama3"}).json()["id"]
        assert started.wait(5)

        paused = client.post(f"/api/models/downloads/{job_id}/pause")
        assert paused.status_code == 200
        assert paused.json()["state"] == "paused"
        release.set()
        assert mgr_join()

        assert client.post(f"/api/models/downloads/{job_id}/pause").status_code == 409

        reachable["up"] = False
        assert client.post(f"/api/models/downloads/{job_id}/resume").status_code == 503
        reachable["up"] = True

        cancelled = client.post(f"/api/models/downloads/{job_id}/cancel")
        assert cancelled.status_code == 200
        assert cancelled.json()["state"] == "cancelled"
        assert client.post(f"/api/models/downloads/{job_id}/resume").status_code == 409
        assert client.post(f"/api/models/downloads/{job_id}/cancel").status_code == 409


@pytest.mark.parametrize("action", ["pause", "resume", "cancel"])
def test_unknown_job_is_404(tmp_path, reachable, action):
    with TestClient(_app(tmp_path, ScriptedPull())) as client:
        resp = client.post(f"/api/models/downloads/nope/{action}")
        assert resp.status_code == 404
        assert resp.json()["detail"] == "job_not_found"


def test_get_without_jobs_is_empty(tmp_path, reachable):
    with TestClient(_app(tmp_path, ScriptedPull())) as client:
        body = client.get("/api/models/downloads").json()
        assert body["jobs"] == []
        assert body["active_job_id"] is None


def test_hf_quants_endpoint_shape(tmp_path, reachable, monkeypatch):
    tree = [
        {"type": "file", "path": "M-Q4_K_M.gguf", "size": 4_500_000_000, "lfs": {"size": 4_500_000_000}},
        {"type": "file", "path": "M-Q8_0.gguf", "size": 9_000_000_000, "lfs": {"size": 9_000_000_000}},
    ]

    class Resp:
        status_code = 200

        def json(self):
            return tree

    monkeypatch.setattr(hf_quants, "_http_get", lambda url, **kw: Resp())
    with TestClient(_app(tmp_path, ScriptedPull())) as client:
        resp = client.get("/api/models/hf/bartowski/M-GGUF/quants")
        assert resp.status_code == 200
        body = resp.json()
        assert body["owner"] == "bartowski"
        assert body["repo"] == "M-GGUF"
        assert body["vram_mb"] == 8192
        assert body["quants"] == [
            {
                "quant": "Q4_K_M",
                "size_bytes": 4_500_000_000,
                "file_count": 1,
                "tag": "hf.co/bartowski/M-GGUF:Q4_K_M",
                "vram_fit": "fits",
            },
            {
                "quant": "Q8_0",
                "size_bytes": 9_000_000_000,
                "file_count": 1,
                "tag": "hf.co/bartowski/M-GGUF:Q8_0",
                "vram_fit": "too_large",
            },
        ]


def test_hf_quants_not_found_is_404(tmp_path, reachable, monkeypatch):
    class Resp:
        status_code = 404

        def json(self):
            return {}

    monkeypatch.setattr(hf_quants, "_http_get", lambda url, **kw: Resp())
    with TestClient(_app(tmp_path, ScriptedPull())) as client:
        resp = client.get("/api/models/hf/a/b/quants")
        assert resp.status_code == 404
        assert resp.json()["detail"] == "hf_repo_not_found"


def test_hf_quants_upstream_error_is_502(tmp_path, reachable, monkeypatch):
    class Resp:
        status_code = 500

        def json(self):
            return {}

    monkeypatch.setattr(hf_quants, "_http_get", lambda url, **kw: Resp())
    with TestClient(_app(tmp_path, ScriptedPull())) as client:
        resp = client.get("/api/models/hf/a/b/quants")
        assert resp.status_code == 502
        assert resp.json()["detail"] == "hf_unavailable"


def test_hf_quants_invalid_name_is_422(tmp_path, reachable, monkeypatch):
    calls = []
    monkeypatch.setattr(hf_quants, "_http_get", lambda url, **kw: calls.append(url))
    with TestClient(_app(tmp_path, ScriptedPull())) as client:
        resp = client.get("/api/models/hf/bad$name/repo/quants")
        assert resp.status_code == 422
        assert resp.json()["detail"] == "invalid_hf_repo"
    assert calls == []


def test_default_app_manager_never_uses_real_pull_in_tests(tmp_path, reachable):
    """The conftest guard replaces the real Ollama pull: a default-built app
    that is asked to download fails the job instead of touching Ollama."""
    import opencohost.api.main as main_mod

    app = main_mod.create_app(host_factory=FakeHost, cors_origins=_ORIGINS)
    with TestClient(app) as client:
        resp = client.post("/api/models/downloads", json={"tag": "llama3"})
        assert resp.status_code == 202
        assert app.state.download_manager.join(5)
        job = client.get("/api/models/downloads").json()["jobs"][0]
        assert job["state"] == "failed"
