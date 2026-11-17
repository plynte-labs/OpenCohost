"""Separate cloud vision model as a vision source
(cloud-providers-vision-20261111 P3). HTTP to providers is always mocked."""
from __future__ import annotations

import json
import logging
import threading
from dataclasses import replace
from types import SimpleNamespace

import pytest
import requests
from fastapi.testclient import TestClient

from opencohost.config import settings
from opencohost.config import vision_settings as vs
from opencohost.config.vision_settings import VisionSettings
from opencohost.core.providers.cloud import cloud_llm_client as client
from opencohost.core.vision import cloud_describer as cd
from opencohost.core.vision.controller import VisionController
from opencohost.core.vision.describer import OllamaVisionDescriber, VisionDescribeError
from tests.test_api_phase1 import FakeHost

_KEY = "sk-SENTINEL-vision-key-123456"
_JPEG = b"\xff\xd8\xff\xe0SENTINEL-IMAGE-BYTES"
_PNG = b"\x89PNG\r\n\x1a\nSENTINEL-IMAGE-BYTES"
_ORIGINS = ["http://localhost:5173"]

BASE = VisionSettings(
    enabled=True, look_on_ptt=False, capture_delay_s=0.0, rule_gate_enabled=False,
    port=11435, model="qwen3-vl:4b-instruct", describe_timeout_s=8.0,
)
CLOUD = replace(
    BASE, vision_source="cloud_model", vision_cloud_preset="gemini",
    vision_cloud_model="gemini-3.5-flash-lite",
)


class _Resp:
    def __init__(self, status=200, data=None, headers=None, text=""):
        self.status_code = status
        self._data = data
        self.headers = headers or {}
        self.text = text
        self.reason = "x"

    def raise_for_status(self):
        if self.status_code >= 400:
            err = requests.exceptions.HTTPError("boom")
            err.response = self
            raise err

    def json(self):
        if self._data is None:
            raise ValueError("no json")
        return self._data


def _ok(content="A red button is visible."):
    return _Resp(200, {"choices": [{"message": {"content": content}}], "usage": {}})


def _capture(monkeypatch, response=None):
    seen = {}

    def fake_post(url, json=None, headers=None, timeout=None, **kw):
        seen.update(url=url, body=json, headers=headers, timeout=timeout)
        if isinstance(response, Exception):
            raise response
        return response if response is not None else _ok()

    monkeypatch.setattr(client.requests, "post", fake_post)
    return seen


def _describer(preset="gemini", model="m1", key=_KEY):
    return cd.CloudVisionDescriber(preset, model, key_provider=lambda: key, lang="en")


# -- request body -------------------------------------------------------------


def test_request_has_one_image_part_per_frame_and_prompt(monkeypatch):
    seen = _capture(monkeypatch)
    text = _describer().describe([_JPEG, _PNG], "what is this?", timeout_s=5)
    assert text == "A red button is visible."
    body = seen["body"]
    assert body["model"] == "m1"
    assert seen["url"].endswith("/chat/completions")
    system, user = body["messages"]
    assert system["role"] == "system" and "describe a screenshot" in system["content"]
    parts = user["content"]
    assert parts[0]["type"] == "text" and "what is this?" in parts[0]["text"]
    urls = [p["image_url"]["url"] for p in parts[1:]]
    assert len(urls) == 2
    assert urls[0].startswith("data:image/jpeg;base64,")
    assert urls[1].startswith("data:image/png;base64,")
    assert body["max_tokens"] <= 500


def test_single_bytes_frame_is_accepted(monkeypatch):
    seen = _capture(monkeypatch)
    _describer().describe(_JPEG, "q", timeout_s=5)
    assert len(seen["body"]["messages"][1]["content"]) == 2


def test_preset_headers_and_bearer_key(monkeypatch):
    seen = _capture(monkeypatch)
    _describer(preset="openrouter").describe(_JPEG, "q", timeout_s=5)
    assert seen["headers"]["Authorization"] == f"Bearer {_KEY}"
    assert seen["headers"]["X-OpenRouter-Title"] == "OpenCohost"
    assert seen["body"]["reasoning"] == {"exclude": True}


def test_anthropic_drops_sampling_params(monkeypatch):
    seen = _capture(monkeypatch)
    _describer(preset="anthropic", model="claude-haiku-5-5").describe(_JPEG, "q", timeout_s=5)
    assert "temperature" not in seen["body"] and "top_p" not in seen["body"]


def test_deepseek_thinking_disabled(monkeypatch):
    seen = _capture(monkeypatch)
    _describer(preset="deepseek", model="deepseek-flash").describe(_JPEG, "q", timeout_s=5)
    assert seen["body"]["thinking"] == {"type": "disabled"}


def test_no_nim_kwargs_for_gemini(monkeypatch):
    seen = _capture(monkeypatch)
    _describer().describe(_JPEG, "q", timeout_s=5)
    assert "chat_template_kwargs" not in seen["body"]


def test_timeout_is_the_describe_timeout(monkeypatch):
    seen = _capture(monkeypatch)
    _describer().describe(_JPEG, "q", timeout_s=7.5)
    assert seen["timeout"] == 7.5


def test_hint_is_appended(monkeypatch):
    seen = _capture(monkeypatch)
    _describer().describe(_JPEG, "q", timeout_s=5, hint="HINT-X")
    assert "HINT-X" in seen["body"]["messages"][1]["content"][0]["text"]


# -- failure mapping ------------------------------------------------------------


@pytest.mark.parametrize(
    "response,reason,detail",
    [
        (requests.exceptions.Timeout("t"), "timeout", ""),
        (requests.exceptions.ConnectionError("c"), "unavailable", ""),
        (_Resp(401), "http", "bad_key"),
        (_Resp(403), "http", "bad_key"),
        (_Resp(429, headers={"Retry-After": "3"}), "http", "rate_limited"),
        (_Resp(500), "http", "status=500"),
        (_Resp(200, {"choices": [{"message": {"content": "  "}}]}), "empty", ""),
    ],
)
def test_failures_map_to_describe_error(monkeypatch, response, reason, detail):
    _capture(monkeypatch, response)
    with pytest.raises(VisionDescribeError) as info:
        _describer().describe(_JPEG, "q", timeout_s=5)
    assert info.value.reason == reason
    assert info.value.detail == detail


def test_missing_key_is_unavailable(monkeypatch):
    seen = _capture(monkeypatch)
    with pytest.raises(VisionDescribeError) as info:
        _describer(key=None).describe(_JPEG, "q", timeout_s=5)
    assert (info.value.reason, info.value.detail) == ("unavailable", "no_key")
    assert not seen


def test_cancel_before_call(monkeypatch):
    seen = _capture(monkeypatch)
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(VisionDescribeError) as info:
        _describer().describe(_JPEG, "q", timeout_s=5, cancel=cancel)
    assert info.value.reason == "cancelled" and not seen


def test_deadline_stops_a_hung_call(monkeypatch):
    release = threading.Event()

    def slow_post(*a, **kw):
        release.wait(5)
        return _ok()

    monkeypatch.setattr(client.requests, "post", slow_post)
    try:
        with pytest.raises(VisionDescribeError) as info:
            _describer().describe(_JPEG, "q", timeout_s=0.2)
        assert info.value.reason == "timeout"
    finally:
        release.set()


def test_no_empty_frames(monkeypatch):
    _capture(monkeypatch)
    with pytest.raises(VisionDescribeError) as info:
        _describer().describe([], "q", timeout_s=5)
    assert info.value.reason == "empty"


def test_logs_never_carry_image_question_description_or_key(monkeypatch, caplog):
    _capture(monkeypatch, _ok("DESCRIPTION-SENTINEL"))
    caplog.set_level(logging.DEBUG)
    _describer().describe(_JPEG, "QUESTION-SENTINEL", timeout_s=5)
    _capture(monkeypatch, _Resp(401))
    with pytest.raises(VisionDescribeError):
        _describer().describe(_JPEG, "QUESTION-SENTINEL", timeout_s=5)
    text = caplog.text
    for secret in ("SENTINEL", _KEY, "base64"):
        assert secret not in text
    assert "[VISION]" in text


# -- factory + key resolution ---------------------------------------------------


def test_factory_picks_by_source():
    key = lambda: _KEY  # noqa: E731
    assert isinstance(cd.make_describer(BASE, "http://127.0.0.1:11435", key), OllamaVisionDescriber)
    assert isinstance(cd.make_describer(CLOUD, None, key), cd.CloudVisionDescriber)
    chat = replace(BASE, vision_source="chat_model")
    direct_only = cd.make_describer(chat, None, key)
    with pytest.raises(VisionDescribeError) as info:
        direct_only.describe(_JPEG, "q", timeout_s=5)
    assert (info.value.reason, info.value.detail) == ("unavailable", "direct_only")


class _Store:
    def __init__(self, data):
        self.data = data

    def load(self, profile_id):
        return self.data.get(profile_id)


def test_resolve_key_uses_chat_key_of_same_preset():
    cfg = {"profiles": {"mine": {"preset": "gemini"}, "other": {"preset": "openai"}}}
    store = _Store({"mine": {"api_key": "CHAT"}, "vision_gemini": {"api_key": "OWN"}})
    on = replace(CLOUD, vision_cloud_use_chat_key=True)
    off = replace(CLOUD, vision_cloud_use_chat_key=False)
    assert cd.resolve_cloud_key(on, cfg, store) == "CHAT"
    assert cd.resolve_cloud_key(off, cfg, store) == "OWN"


def test_resolve_key_falls_back_to_own_and_none():
    store = _Store({"vision_gemini": {"api_key": "OWN"}})
    on = replace(CLOUD, vision_cloud_use_chat_key=True)
    assert cd.resolve_cloud_key(on, {"profiles": {}}, store) == "OWN"
    assert cd.resolve_cloud_key(on, {"profiles": {}}, _Store({})) is None


# -- settings --------------------------------------------------------------------


def test_source_defaults_local_and_validates():
    assert BASE.vision_source == "local"
    assert vs.with_updates(BASE, {"vision_source": "cloud_model"}).vision_source == "cloud_model"
    assert vs.with_updates(BASE, {"vision_source": "bogus"}).vision_source == "local"
    assert vs.with_updates(BASE, {"vision_cloud_preset": "nope"}).vision_cloud_preset == ""
    assert vs.with_updates(BASE, {"vision_cloud_preset": "gemini"}).vision_cloud_preset == "gemini"
    assert vs.with_updates(BASE, {"vision_cloud_model": "bad model!"}).vision_cloud_model == ""
    assert vs.with_updates(BASE, {"vision_cloud_use_chat_key": False}).vision_cloud_use_chat_key is False


def test_migration_from_p2_opt_in():
    migrated = vs.with_updates(BASE, {"cloud_direct_allowed": True})
    assert migrated.vision_source == "chat_model" and migrated.cloud_direct_allowed is True
    back = vs.with_updates(migrated, {"cloud_direct_allowed": False})
    assert back.vision_source == "local" and back.cloud_direct_allowed is False


def test_source_drives_the_direct_opt_in():
    chat = vs.with_updates(BASE, {"vision_source": "chat_model"})
    assert chat.cloud_direct_allowed is True
    cloud = vs.with_updates(chat, {"vision_source": "cloud_model"})
    assert cloud.cloud_direct_allowed is False
    assert vs.with_updates(chat, {"vision_source": "local"}).cloud_direct_allowed is False


def test_load_old_file_migrates(tmp_path):
    path = tmp_path / "v.json"
    path.write_text(json.dumps({"enabled": True, "cloud_direct_allowed": True}), encoding="utf-8")
    assert vs.load(str(path), base=BASE).vision_source == "chat_model"
    path.write_text(json.dumps({"enabled": True}), encoding="utf-8")
    assert vs.load(str(path), base=BASE).vision_source == "local"


def test_save_roundtrip_keeps_source(tmp_path):
    path = str(tmp_path / "v.json")
    assert vs.save(CLOUD, path)
    assert vs.load(path, base=BASE).vision_source == "cloud_model"


# -- controller: no daemon for non-local sources ----------------------------------


class _Daemon:
    base_url = "http://127.0.0.1:11435"

    def __init__(self):
        self.calls = []

    def ensure_running(self):
        self.calls.append("ensure")
        return SimpleNamespace(state="running", last_error=None)

    def status(self):
        return SimpleNamespace(state="running")

    def stop(self):
        self.calls.append("stop")

    def unload(self, model):
        self.calls.append("unload")

    def warm(self, *a, **k):
        self.calls.append("warm")
        return True

    def model_loaded(self, model):
        return True

    def model_installed(self, model):
        return True


def _controller(daemons, described):
    def daemon_factory(values):
        daemon = _Daemon()
        daemons.append(daemon)
        return daemon

    class _Desc:
        def __init__(self, tag):
            self.tag = tag

        def describe(self, jpeg, question, *, timeout_s, **kw):
            described.append(self.tag)
            return "seen"

    return VisionController(
        daemon_factory=daemon_factory,
        describer_factory=lambda v, base: _Desc(v.vision_source),
        capture_factory=lambda: None,
        speak_bridge=lambda text: None,
        spawn=lambda fn: fn(),
        sleep=lambda s: None,
    )


@pytest.mark.parametrize("source", ["cloud_model", "chat_model"])
def test_non_local_source_never_builds_or_starts_the_daemon(source):
    daemons, described = [], []
    controller = _controller(daemons, described)
    controller.apply(replace(BASE, vision_source=source))
    assert daemons == []
    status = controller.status()
    assert status["state"] == "ready" and status["enabled"] is True
    assert controller._ready() is None
    assert controller.step is not None


def test_cloud_source_describes_pasted_image_without_daemon():
    daemons, described = [], []
    controller = _controller(daemons, described)
    controller.apply(CLOUD)
    assert controller.describe_image(_JPEG, "q") == "seen"
    assert described == ["cloud_model"] and daemons == []


def test_switching_source_rebuilds_without_restart():
    daemons, described = [], []
    controller = _controller(daemons, described)
    controller.apply(BASE)
    assert len(daemons) == 1
    controller.apply(CLOUD)
    assert controller.status()["state"] == "ready"
    assert "stop" in daemons[0].calls
    controller.describe_image(_JPEG, "q")
    assert described[-1] == "cloud_model"
    controller.apply(BASE)
    assert len(daemons) == 2  # local again starts a fresh daemon


def test_changing_cloud_model_rebuilds_describer():
    daemons, described = [], []
    controller = _controller(daemons, described)
    controller.apply(CLOUD)
    first = controller.step._describer
    controller.apply(replace(CLOUD, vision_cloud_model="gemini-3.8-flash"))
    assert controller.step._describer is not first


# -- route: strict validation and key handling ---------------------------------------


class _VisionHost(FakeHost):
    def __init__(self):
        super().__init__()
        self.applied = []

    def vision_status(self):
        return {"state": "ready", "last_error": None, "daemon_state": "stopped",
                "model_installed": None, "model_loaded": None, "capture_available": True,
                "armed": False, "looking": False, "last_result": None, "enabled": True}

    def vision_apply_settings(self, updates, *, persist=True):
        self.applied.append(dict(updates))
        new = vs.with_updates(vs.current(), updates)
        vs.apply(new)
        return new


@pytest.fixture
def api(tmp_path, monkeypatch):
    import opencohost.api.main as main_mod
    import opencohost.config.llm_provider as llm_provider_mod

    main_mod._host_active = False
    for attr in vs._ATTRS.values():
        monkeypatch.setattr(settings, attr, getattr(settings, attr))
    monkeypatch.setattr(llm_provider_mod, "LLM_PROVIDER_CONFIG_FILE", str(tmp_path / "p.json"))
    monkeypatch.setattr(main_mod, "LLM_KEYS_FILE", str(tmp_path / "keys.json"))
    app = main_mod.create_app(host_factory=_VisionHost, cors_origins=_ORIGINS)
    test_client = TestClient(app)
    test_client.__enter__()
    import opencohost.api.auth as auth

    auth.ensure_tokens()
    test_client.headers.update({"Authorization": f"Bearer {auth.load_tokens()['operator']}"})
    yield app, test_client, str(tmp_path / "keys.json")
    test_client.__exit__(None, None, None)
    main_mod._host_active = False


_FULL = {
    "vision_source": "cloud_model", "vision_cloud_preset": "gemini",
    "vision_cloud_model": "gemini-3.5-flash-lite", "vision_cloud_use_chat_key": False,
}


def test_put_stores_key_never_returns_it(api):
    app, http, keys_file = api
    resp = http.put("/api/vision", json={**_FULL, "vision_cloud_api_key": _KEY})
    assert resp.status_code == 200, resp.text
    assert _KEY not in resp.text
    body = resp.json()
    assert body["vision_source"] == "cloud_model" and body["vision_cloud_key_set"] is True
    assert "vision_cloud_api_key" not in body
    assert "vision_cloud_api_key" not in app.state.host.applied[0]
    from opencohost.stream_admin.oauth_store import OAuthStore

    assert OAuthStore(keys_file).load("vision_gemini") == {"api_key": _KEY}
    assert _KEY not in http.get("/api/vision").text


def test_put_cloud_source_without_key_is_422(api):
    _, http, _ = api
    resp = http.put("/api/vision", json=_FULL)
    assert resp.status_code == 422
    assert resp.json()["detail"] == "invalid_vision_settings"
    assert {"field": "vision_cloud_api_key", "code": "required"} in resp.json()["errors"]


def test_put_cloud_source_incomplete_is_422(api):
    _, http, _ = api
    resp = http.put("/api/vision", json={"vision_source": "cloud_model"})
    assert resp.status_code == 422
    fields = {e["field"] for e in resp.json()["errors"]}
    assert {"vision_cloud_preset", "vision_cloud_model"} <= fields


@pytest.mark.parametrize(
    "payload,field",
    [
        ({"vision_source": "bogus"}, "vision_source"),
        ({"vision_cloud_preset": "nope"}, "vision_cloud_preset"),
        ({"vision_cloud_model": "bad model!"}, "vision_cloud_model"),
        ({"vision_cloud_use_chat_key": "yes"}, "vision_cloud_use_chat_key"),
        ({"vision_cloud_api_key": 12}, "vision_cloud_api_key"),
    ],
)
def test_put_rejects_bad_values(api, payload, field):
    _, http, _ = api
    resp = http.put("/api/vision", json=payload)
    assert resp.status_code == 422
    assert field in {e["field"] for e in resp.json()["errors"]}


def test_use_chat_key_needs_no_own_key_when_chat_key_exists(api):
    app, http, keys_file = api
    from opencohost.stream_admin.oauth_store import OAuthStore

    http.put("/api/llm/provider", json={
        "profile_id": "mine", "preset": "gemini", "model": "gemini-3.5-flash-lite",
        "api_key": _KEY,
    })
    resp = http.put("/api/vision", json={**_FULL, "vision_cloud_use_chat_key": True})
    assert resp.status_code == 200, resp.text
    assert resp.json()["vision_cloud_chat_key_set"] is True
    assert OAuthStore(keys_file).load("vision_gemini") is None


def test_clearing_the_key(api):
    _, http, keys_file = api
    http.put("/api/vision", json={**_FULL, "vision_cloud_api_key": _KEY})
    resp = http.put("/api/vision", json={"vision_source": "local", "vision_cloud_api_key": ""})
    assert resp.status_code == 200
    assert resp.json()["vision_cloud_key_set"] is False


def test_key_not_logged(api, caplog):
    _, http, _ = api
    caplog.set_level(logging.DEBUG)
    http.put("/api/vision", json={**_FULL, "vision_cloud_api_key": _KEY})
    assert _KEY not in caplog.text


def test_vision_key_works_with_the_test_route(api, monkeypatch):
    """The stored `vision_<preset>` entry is reachable by the existing key test."""
    _, http, _ = api
    http.put("/api/vision", json={**_FULL, "vision_cloud_api_key": _KEY})
    import opencohost.api.routers.llm_provider as mod

    seen = {}

    def fake_get(url, headers=None, timeout=None, **kw):
        seen["headers"] = headers
        return SimpleNamespace(status_code=200, json=lambda: {"data": [{"id": "m"}]})

    monkeypatch.setattr(mod.requests, "get", fake_get)
    resp = http.post("/api/llm/provider/test", json={"preset": "gemini", "profile_id": "vision_gemini"})
    assert resp.json()["ok"] is True
    assert seen["headers"]["Authorization"] == f"Bearer {_KEY}"
