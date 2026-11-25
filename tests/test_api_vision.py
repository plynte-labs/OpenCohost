"""/api/vision -- owner vision settings, status and one-shot look (vision-on-demand V1.6)."""
import pytest
from fastapi.testclient import TestClient

from opencohost.config import settings
from opencohost.config import vision_settings as vs
from tests.test_api_phase1 import FakeHost

_ORIGINS = ["http://localhost:5173"]

_STATUS = {
    "enabled": True,
    "state": "ready",
    "last_error": None,
    "model_loaded": True,
    "last_result": None,
    "daemon_state": "running",
    "model_installed": True,
    "capture_available": True,
    "armed": False,
    "looking": False,
    "model": "qwen3-vl:4b-instruct",
    "port": 11435,
}


class _VisionHost(FakeHost):
    def __init__(self):
        super().__init__()
        self.status = dict(_STATUS)
        self.applied = []
        self.fail_write = False
        self.arm_result = True
        self.thumbs = {}

    def vision_thumbnails(self, look_id):
        return self.thumbs.get(look_id)

    def vision_status(self):
        return dict(self.status)

    def vision_arm_look(self):
        return self.arm_result

    def vision_apply_settings(self, updates, *, persist=True):
        self.applied.append((dict(updates), persist))
        if self.fail_write:
            return None
        new = vs.with_updates(vs.current(), updates)
        vs.apply(new)
        return new


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    import opencohost.api.main as main_mod

    main_mod._host_active = False
    for attr in vs._ATTRS.values():
        monkeypatch.setattr(settings, attr, getattr(settings, attr))
    yield
    main_mod._host_active = False


def _operator_headers():
    import opencohost.api.auth as auth

    auth.ensure_tokens()
    return {"Authorization": f"Bearer {auth.load_tokens()['operator']}"}


def _client(host_factory=_VisionHost):
    import opencohost.api.main as main_mod

    app = main_mod.create_app(host_factory=host_factory, cors_origins=_ORIGINS)
    client = TestClient(app)
    client.__enter__()
    return app, client


def test_get_returns_settings_and_status():
    app, client = _client()
    try:
        body = client.get("/api/vision").json()
        assert set(body) == {
            "enabled", "look_on_ptt", "capture_delay_s", "rule_gate_enabled",
            "model", "port", "describe_timeout_s", "status",
            "frames", "frame_interval_s", "show_thumbnails", "max_images",
            "cloud_direct_allowed", "vision_source", "vision_cloud_preset",
            "vision_cloud_model", "vision_cloud_use_chat_key",
            "vision_cloud_key_set", "vision_cloud_chat_key_set",
            "chat_model_eligible", "chat_model_reason",
            "vram_warning", "vram_needed_gib", "vram_free_gib", "vram_total_gib",
        }
        assert body["status"] == {
            "state": "ready", "last_error": None,
            "daemon_state": "running", "model_installed": True, "model_loaded": True,
            "capture_available": True, "armed": False, "looking": False,
            "last_result": None,
        }
        assert body["enabled"] is bool(settings.VISION_ENABLED)
    finally:
        client.__exit__(None, None, None)


def test_put_partial_update_applies_and_returns_shape():
    app, client = _client()
    try:
        resp = client.put("/api/vision", json={"enabled": True, "capture_delay_s": 2.5})
        assert resp.status_code == 200
        body = resp.json()
        assert body["enabled"] is True and body["capture_delay_s"] == 2.5
        assert "status" in body
        assert app.state.host.applied == [({"enabled": True, "capture_delay_s": 2.5}, True)]
    finally:
        client.__exit__(None, None, None)


@pytest.mark.parametrize(
    "payload,field",
    [
        ({"enabled": "yes"}, "enabled"),
        ({"capture_delay_s": "fast"}, "capture_delay_s"),
        ({"port": 80}, "port"),
        ({"model": ""}, "model"),
    ],
)
def test_put_invalid_value_is_422_and_applies_nothing(payload, field):
    app, client = _client()
    try:
        resp = client.put("/api/vision", json=payload)
        assert resp.status_code == 422
        detail = resp.json()["detail"]
        assert detail == "invalid_vision_settings"
        assert [e["field"] for e in resp.json()["errors"]] == [field]
        assert "code" in resp.json()["errors"][0]
        assert app.state.host.applied == []
    finally:
        client.__exit__(None, None, None)


def test_put_non_object_body_is_422():
    app, client = _client()
    try:
        resp = client.put("/api/vision", json=[1])
        assert resp.status_code == 422
    finally:
        client.__exit__(None, None, None)


def test_put_unknown_keys_are_ignored():
    app, client = _client()
    try:
        resp = client.put("/api/vision", json={"bogus": 1, "look_on_ptt": True})
        assert resp.status_code == 200
        assert app.state.host.applied == [({"look_on_ptt": True}, True)]
    finally:
        client.__exit__(None, None, None)


def test_put_write_failure_is_503():
    app, client = _client()
    try:
        app.state.host.fail_write = True
        resp = client.put("/api/vision", json={"enabled": True})
        assert resp.status_code == 503
        assert resp.json() == {"detail": "vision_write_failed"}
    finally:
        client.__exit__(None, None, None)


def test_look_arms_when_enabled():
    app, client = _client()
    try:
        resp = client.post("/api/vision/look")
        assert resp.status_code == 200
        assert resp.json() == {"armed": True}
    finally:
        client.__exit__(None, None, None)


def test_look_409_when_vision_disabled():
    app, client = _client()
    try:
        app.state.host.status["enabled"] = False
        resp = client.post("/api/vision/look")
        assert resp.status_code == 409
        assert resp.json() == {"detail": "vision_disabled"}
    finally:
        client.__exit__(None, None, None)


def test_look_409_when_controller_refuses_to_arm():
    app, client = _client()
    try:
        app.state.host.arm_result = False
        assert client.post("/api/vision/look").status_code == 409
    finally:
        client.__exit__(None, None, None)


def test_missing_host_is_503_engine_unavailable():
    app, client = _client()
    try:
        app.state.host = None
        for resp in (
            client.get("/api/vision"),
            client.put("/api/vision", json={"enabled": True}),
            client.post("/api/vision/look"),
        ):
            assert resp.status_code == 503
            assert resp.json()["detail"]["error"] == "ENGINE_UNAVAILABLE"
    finally:
        client.__exit__(None, None, None)


def test_get_reports_the_last_look_result_without_content():
    app, client = _client()
    try:
        last = {"outcome": "describe_timeout", "capture_ms": 172, "describe_ms": 8000, "at": 1.5}
        app.state.host.status.update(state="failed", last_error="warm_failed", last_result=last)
        body = client.get("/api/vision").json()
        assert body["status"]["state"] == "failed"
        assert body["status"]["last_error"] == "warm_failed"
        assert body["status"]["last_result"] == last
    finally:
        client.__exit__(None, None, None)


# --------------------------------------------------------- V1.11 thumbnails


def test_put_accepts_the_burst_settings():
    app, client = _client()
    try:
        resp = client.put("/api/vision", json={"frames": 2, "frame_interval_s": 0.5, "show_thumbnails": False})
        assert resp.status_code == 200
        body = resp.json()
        assert (body["frames"], body["frame_interval_s"], body["show_thumbnails"]) == (2, 0.5, False)
    finally:
        client.__exit__(None, None, None)


@pytest.mark.parametrize(
    "payload,field",
    [({"frames": "3"}, "frames"), ({"frame_interval_s": "x"}, "frame_interval_s"),
     ({"show_thumbnails": 1}, "show_thumbnails")],
)
def test_put_rejects_invalid_burst_values(payload, field):
    app, client = _client()
    try:
        resp = client.put("/api/vision", json=payload)
        assert resp.status_code == 422
        assert resp.json()["errors"] == [{"field": field, "code": "invalid_value"}]
    finally:
        client.__exit__(None, None, None)


def test_thumbnails_route_returns_data_urls():
    app, client = _client()
    try:
        app.state.host.thumbs["abc"] = ["data:image/jpeg;base64,AAAA", "data:image/jpeg;base64,BBBB"]
        resp = client.get("/api/vision/looks/abc/thumbnails", headers=_operator_headers())
        assert resp.status_code == 200
        assert resp.json() == {
            "id": "abc",
            "thumbnails": ["data:image/jpeg;base64,AAAA", "data:image/jpeg;base64,BBBB"],
        }
    finally:
        client.__exit__(None, None, None)


def test_thumbnails_route_404_for_unknown_or_hidden_look():
    app, client = _client()
    try:
        resp = client.get("/api/vision/looks/nope/thumbnails", headers=_operator_headers())
        assert resp.status_code == 404
        assert resp.json() == {"detail": "look_not_found"}
    finally:
        client.__exit__(None, None, None)


def test_thumbnails_route_404_when_host_has_no_thumbnail_support():
    class _Old(_VisionHost):
        vision_thumbnails = None

    app, client = _client(_Old)
    try:
        assert client.get("/api/vision/looks/x/thumbnails", headers=_operator_headers()).status_code == 404
    finally:
        client.__exit__(None, None, None)


def test_thumbnails_route_503_without_host():
    app, client = _client()
    try:
        app.state.host = None
        resp = client.get("/api/vision/looks/x/thumbnails", headers=_operator_headers())
        assert resp.status_code == 503
        assert resp.json()["detail"]["error"] == "ENGINE_UNAVAILABLE"
    finally:
        client.__exit__(None, None, None)


# -- V1.17: GET /api/vision/models ------------------------------------------------

_MODELS = {
    "current": "qwen3-vl:4b-instruct",
    "installed": [
        {"tag": "llama3", "vision": False, "size_bytes": 4700000000},
        {"tag": "mystery:1b", "vision": None, "size_bytes": None},
        {"tag": "qwen3-vl:4b-instruct", "vision": True, "size_bytes": 3300000000},
    ],
    "suggested": [
        {"tag": "qwen3-vl:4b-instruct", "display": "Qwen3-VL 4B Instruct", "size_gb": 3.3, "desc": "d"},
    ],
    "ollama_reachable": True,
    "benchmarks": {
        "qwen3-vl:4b-instruct": {
            "verdict": "recommended",
            "vram_gib": 3.29,
            "describe_s": 1.46,
            "spanish": True,
            "note": "n",
            "measured_on": "2026-10-02",
        },
        "gemma4:e2b": {
            "verdict": "broken",
            "vram_gib": None,
            "describe_s": None,
            "spanish": None,
            "note": "n",
            "measured_on": "2026-10-01",
        },
    },
}


class _ModelsHost(_VisionHost):
    def vision_models(self):
        return _MODELS


def test_models_route_returns_the_contract_shape():
    app, client = _client(_ModelsHost)
    try:
        resp = client.get("/api/vision/models")
        assert resp.status_code == 200
        assert resp.json() == _MODELS
    finally:
        client.__exit__(None, None, None)


def test_models_route_503_without_host():
    app, client = _client(_ModelsHost)
    try:
        app.state.host = None
        resp = client.get("/api/vision/models")
        assert resp.status_code == 503
        assert resp.json()["detail"]["error"] == "ENGINE_UNAVAILABLE"
    finally:
        client.__exit__(None, None, None)


def test_models_route_503_when_host_has_no_inventory():
    app, client = _client()
    try:
        resp = client.get("/api/vision/models")
        assert resp.status_code == 503
    finally:
        client.__exit__(None, None, None)


# ---------------------------------------------------------------- A3: chat_model

_LOCAL_CFG = {"active_provider": "local", "profiles": {}}
_NO_VISION_CFG = {
    "active_provider": "p1",
    "profiles": {"p1": {"preset": "deepseek", "base_url": "https://x/v1", "model": "deepseek-v4-pro"}},
}
_VISION_CFG = {
    "active_provider": "p1",
    "profiles": {"p1": {"preset": "custom", "base_url": "https://x/v1", "model": "m", "model_vision": True}},
}


def _provider(monkeypatch, cfg):
    from opencohost.api import deps

    monkeypatch.setattr(deps, "load_provider_config", lambda: cfg)


@pytest.mark.parametrize(
    "cfg,eligible,reason",
    [
        (_LOCAL_CFG, False, "chat_model_requires_cloud_provider"),
        (_NO_VISION_CFG, False, "chat_model_not_vision_capable"),
        (_VISION_CFG, True, None),
    ],
)
def test_get_exposes_chat_model_eligibility(monkeypatch, cfg, eligible, reason):
    _provider(monkeypatch, cfg)
    app, client = _client()
    try:
        body = client.get("/api/vision").json()
        assert body["chat_model_eligible"] is eligible
        assert body["chat_model_reason"] == reason
    finally:
        client.__exit__(None, None, None)


@pytest.mark.parametrize(
    "cfg,code",
    [
        (_LOCAL_CFG, "chat_model_requires_cloud_provider"),
        (_NO_VISION_CFG, "chat_model_not_vision_capable"),
    ],
)
@pytest.mark.parametrize("payload", [{"vision_source": "chat_model"}, {"cloud_direct_allowed": True}])
def test_put_rejects_chat_model_when_the_provider_cannot_see(monkeypatch, cfg, code, payload):
    _provider(monkeypatch, cfg)
    app, client = _client()
    try:
        before = settings.VISION_SOURCE
        resp = client.put("/api/vision", json=payload, headers=_operator_headers())
        assert resp.status_code == 422
        assert resp.json()["detail"] == "invalid_vision_settings"
        assert resp.json()["errors"] == [{"field": "vision_source", "code": code}]
        assert settings.VISION_SOURCE == before
    finally:
        client.__exit__(None, None, None)


def test_put_accepts_chat_model_for_a_vision_capable_provider(monkeypatch):
    _provider(monkeypatch, _VISION_CFG)
    app, client = _client()
    try:
        resp = client.put("/api/vision", json={"vision_source": "chat_model"}, headers=_operator_headers())
        assert resp.status_code == 200 and resp.json()["vision_source"] == "chat_model"
    finally:
        client.__exit__(None, None, None)


def test_put_unrelated_settings_is_not_blocked_by_an_already_ineligible_chat_model(monkeypatch):
    _provider(monkeypatch, _LOCAL_CFG)
    monkeypatch.setattr(settings, "VISION_SOURCE", "chat_model")
    monkeypatch.setattr(settings, "VISION_CLOUD_DIRECT_ALLOWED", True)
    app, client = _client()
    try:
        resp = client.put("/api/vision", json={"capture_delay_s": 1.5}, headers=_operator_headers())
        assert resp.status_code == 200
        assert resp.json()["chat_model_eligible"] is False
        back = client.put("/api/vision", json={"vision_source": "local"}, headers=_operator_headers())
        assert back.status_code == 200
    finally:
        client.__exit__(None, None, None)


# ------------------------------------------------------------------ A4: VRAM


class _VramHost(_VisionHost):
    def __init__(self):
        super().__init__()
        self.vram = (1024.0, 12288.0)  # (free_mb, total_mb)
        self.status["model_loaded"] = False

    def vision_vram_mb(self):
        return self.vram


def test_get_warns_when_the_local_vlm_does_not_fit_next_to_the_local_chat(monkeypatch):
    _provider(monkeypatch, _LOCAL_CFG)
    monkeypatch.setattr(settings, "VISION_SOURCE", "local")
    app, client = _client(_VramHost)
    try:
        body = client.get("/api/vision").json()
        assert body["vram_warning"] == "insufficient"
        assert body["vram_free_gib"] == 1.0 and body["vram_total_gib"] == 12.0
        assert body["vram_needed_gib"] > 3.29
    finally:
        client.__exit__(None, None, None)


def test_get_reports_unknown_vram_without_gpu_detection(monkeypatch):
    _provider(monkeypatch, _LOCAL_CFG)
    monkeypatch.setattr(settings, "VISION_SOURCE", "local")
    app, client = _client()  # FakeHost has no detection
    try:
        assert client.get("/api/vision").json()["vram_warning"] == "unknown"
    finally:
        client.__exit__(None, None, None)


def test_get_has_no_vram_warning_for_a_cloud_chat(monkeypatch):
    _provider(monkeypatch, _VISION_CFG)
    monkeypatch.setattr(settings, "VISION_SOURCE", "local")
    app, client = _client(_VramHost)
    try:
        assert client.get("/api/vision").json()["vram_warning"] is None
    finally:
        client.__exit__(None, None, None)
