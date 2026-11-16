"""POST /api/llm/provider/test + GET /api/llm/provider/presets
(cloud-providers-vision-20261111 P1). HTTP to providers is always mocked."""

import logging

import pytest
import requests
from fastapi.testclient import TestClient

from tests.test_api_phase1 import FakeHost

_ORIGINS = ["http://localhost:5173"]
_KEY = "sk-SENTINEL-do-not-leak-123456"


@pytest.fixture(autouse=True)
def _reset_host_active():
    import opencohost.api.main as main_mod

    main_mod._host_active = False
    yield
    main_mod._host_active = False


@pytest.fixture(autouse=True)
def _isolated_files(tmp_path, monkeypatch):
    import opencohost.api.main as main_mod
    import opencohost.config.llm_provider as llm_provider_mod

    monkeypatch.setattr(
        llm_provider_mod, "LLM_PROVIDER_CONFIG_FILE", str(tmp_path / "llm_provider.json")
    )
    monkeypatch.setattr(main_mod, "LLM_KEYS_FILE", str(tmp_path / "llm_keys.json"))


def _client():
    import opencohost.api.main as main_mod

    return TestClient(main_mod.create_app(host_factory=FakeHost, cors_origins=_ORIGINS))


class _Resp:
    def __init__(self, status=200, data=None, headers=None):
        self.status_code = status
        self._data = data
        self.headers = headers or {}
        self.text = ""

    def json(self):
        if self._data is None:
            raise ValueError("no json")
        return self._data


def _patch_get(monkeypatch, result):
    seen = {}

    def fake_get(url, headers=None, timeout=None, **kw):
        seen.update(url=url, headers=headers, timeout=timeout)
        if isinstance(result, Exception):
            raise result
        return result

    import opencohost.api.routers.llm_provider as mod

    monkeypatch.setattr(mod.requests, "get", fake_get)
    return seen


def _models(*ids):
    return {"data": [{"id": i} for i in ids]}


def test_success_lists_models_and_recommended(monkeypatch):
    seen = _patch_get(monkeypatch, _Resp(200, _models("deepseek-flash", "other", "deepseek-v4-pro")))
    with _client() as c:
        r = c.post("/api/llm/provider/test", json={"preset": "deepseek", "api_key": _KEY})
    body = r.json()
    assert r.status_code == 200 and body["ok"] is True and body["error_code"] is None
    assert body["models"] == ["deepseek-flash", "other", "deepseek-v4-pro"]
    assert body["recommended"] == ["deepseek-flash", "deepseek-v4-pro"]
    assert seen["url"] == "https://api.deepseek.com/models"
    assert seen["headers"]["Authorization"] == f"Bearer {_KEY}"
    assert seen["timeout"] == 10
    assert _KEY not in r.text


@pytest.mark.parametrize(
    "base,expected",
    [
        ("https://generativelanguage.googleapis.com/v1beta/openai/", "https://generativelanguage.googleapis.com/v1beta/openai/models"),
        ("https://api.anthropic.com/v1/", "https://api.anthropic.com/v1/models"),
        ("https://openrouter.ai/api/v1", "https://openrouter.ai/api/v1/models"),
        ("https://api.example.com/v1//", "https://api.example.com/v1/models"),
    ],
)
def test_models_url_joining(monkeypatch, base, expected):
    seen = _patch_get(monkeypatch, _Resp(200, _models("a")))
    with _client() as c:
        c.post("/api/llm/provider/test", json={"base_url": base, "api_key": _KEY})
    assert seen["url"] == expected


def test_strips_models_prefix(monkeypatch):
    _patch_get(monkeypatch, _Resp(200, _models("models/gemini-3.5-flash-lite")))
    with _client() as c:
        body = c.post(
            "/api/llm/provider/test", json={"preset": "gemini", "api_key": _KEY}
        ).json()
    assert body["models"] == ["gemini-3.5-flash-lite"]
    assert body["recommended"] == ["gemini-3.5-flash-lite"]


def test_sends_preset_extra_headers(monkeypatch):
    seen = _patch_get(monkeypatch, _Resp(200, _models("x")))
    with _client() as c:
        c.post("/api/llm/provider/test", json={"preset": "openrouter", "api_key": _KEY})
    assert seen["headers"]["X-OpenRouter-Title"] == "OpenCohost"


@pytest.mark.parametrize("status", [401, 403])
def test_invalid_key(monkeypatch, status):
    _patch_get(monkeypatch, _Resp(status))
    with _client() as c:
        body = c.post("/api/llm/provider/test", json={"preset": "openai", "api_key": _KEY}).json()
    assert body["ok"] is False and body["error_code"] == "invalid_key" and body["models"] == []


def test_rate_limited(monkeypatch):
    _patch_get(monkeypatch, _Resp(429))
    with _client() as c:
        body = c.post("/api/llm/provider/test", json={"preset": "openai", "api_key": _KEY}).json()
    assert body["error_code"] == "rate_limited"


@pytest.mark.parametrize(
    "exc", [requests.exceptions.Timeout("t"), requests.exceptions.ConnectionError("c")]
)
def test_unreachable(monkeypatch, exc):
    _patch_get(monkeypatch, exc)
    with _client() as c:
        body = c.post("/api/llm/provider/test", json={"preset": "openai", "api_key": _KEY}).json()
    assert body["ok"] is False and body["error_code"] == "unreachable"


@pytest.mark.parametrize("resp", [_Resp(500), _Resp(200, None), _Resp(200, {"nope": 1})])
def test_unexpected(monkeypatch, resp):
    _patch_get(monkeypatch, resp)
    with _client() as c:
        body = c.post("/api/llm/provider/test", json={"preset": "openai", "api_key": _KEY}).json()
    assert body["ok"] is False and body["error_code"] == "unexpected"


def test_uses_stored_key_for_profile(monkeypatch):
    seen = _patch_get(monkeypatch, _Resp(200, _models("a")))
    with _client() as c:
        c.put("/api/llm/provider", json={"profile_id": "p1", "preset": "openai", "api_key": _KEY})
        body = c.post("/api/llm/provider/test", json={"profile_id": "p1"}).json()
    assert body["ok"] is True
    assert seen["headers"]["Authorization"] == f"Bearer {_KEY}"
    assert seen["url"] == "https://api.openai.com/v1/models"


def test_missing_key_is_invalid_key_without_http(monkeypatch):
    seen = _patch_get(monkeypatch, _Resp(200, _models("a")))
    with _client() as c:
        body = c.post("/api/llm/provider/test", json={"preset": "openai"}).json()
    assert body["error_code"] == "invalid_key" and "url" not in seen


def test_missing_base_url_is_unexpected(monkeypatch):
    _patch_get(monkeypatch, _Resp(200, _models("a")))
    with _client() as c:
        body = c.post("/api/llm/provider/test", json={"api_key": _KEY}).json()
    assert body["error_code"] == "unexpected"


def test_key_never_in_logs(monkeypatch, caplog):
    _patch_get(monkeypatch, _Resp(401))
    caplog.set_level(logging.DEBUG)
    with _client() as c:
        r = c.post("/api/llm/provider/test", json={"preset": "openai", "api_key": _KEY})
    assert _KEY not in r.text
    assert _KEY not in caplog.text
    assert "[LLM_PROVIDER_TEST] preset=openai outcome=invalid_key models=0" in caplog.text


def test_unknown_preset_logs_custom(monkeypatch, caplog):
    _patch_get(monkeypatch, _Resp(200, _models("a", "b")))
    caplog.set_level(logging.INFO)
    with _client() as c:
        c.post(
            "/api/llm/provider/test",
            json={"preset": "evil\nname", "base_url": "https://x.example/v1", "api_key": _KEY},
        )
    assert "[LLM_PROVIDER_TEST] preset=custom outcome=ok models=2" in caplog.text


def test_presets_endpoint_exposes_schema():
    with _client() as c:
        r = c.get("/api/llm/provider/presets")
    assert r.status_code == 200
    presets = r.json()["presets"]
    assert {"openai", "nvidia_nim", "gemini", "deepseek", "anthropic", "openrouter"} <= set(presets)
    g = presets["gemini"]
    assert g["key_url"] == "https://aistudio.google.com/apikey"
    assert g["default_model"] == "gemini-3.5-flash-lite"
    assert g["models"][0] == {"id": "gemini-3.5-flash-lite", "vision": True}
    assert "extra_headers" not in g and "drop_params" not in g


# --- S11: second stage, one tiny chat completion to the chosen model -------


class _ChatResp:
    def __init__(self, status=200, data=None, text="", reason="X"):
        self.status_code = status
        self._data = data
        self.text = text
        self.reason = reason
        self.headers = {}

    def raise_for_status(self):
        if self.status_code >= 400:
            err = requests.exceptions.HTTPError(f"{self.status_code}")
            err.response = self
            raise err

    def json(self):
        if self._data is None:
            raise ValueError("no json")
        return self._data

    def iter_lines(self):
        # The chat stage streams like the chat path (gemini-chat-parity G1).
        import json as _json

        for choice in (self._data or {}).get("choices") or []:
            content = (choice.get("message") or {}).get("content")
            if content:
                yield ("data: " + _json.dumps({"choices": [{"delta": {"content": content}}]})).encode()
        yield b"data: [DONE]"

    def close(self):
        pass


def _ok_chat(content="OK"):
    return _ChatResp(200, {"choices": [{"message": {"content": content}}]})


def _patch_post(monkeypatch, result):
    seen = {"calls": 0}

    def fake_post(url, json=None, headers=None, timeout=None, **kw):
        seen.update(url=url, body=json, headers=headers, timeout=timeout)
        seen["calls"] += 1
        if isinstance(result, Exception):
            raise result
        return result

    import opencohost.api.routers.llm_provider as mod

    monkeypatch.setattr(mod.requests, "post", fake_post)
    return seen


def _test(preset="gemini", **extra):
    payload = {"preset": preset, "api_key": _KEY, "check_chat": True, **extra}
    with _client() as c:
        return c.post("/api/llm/provider/test", json=payload).json()


def test_chat_stage_passes_and_sends_tiny_request(monkeypatch):
    _patch_get(monkeypatch, _Resp(200, _models("gemini-3.5-flash-lite")))
    seen = _patch_post(monkeypatch, _ok_chat())
    body = _test(model="gemini-3.8-flash")
    assert body["ok"] is True and body["models_ok"] is True and body["chat_ok"] is True
    assert body["chat_error_code"] is None and body["http_status"] == 200
    assert seen["url"] == "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
    assert seen["body"]["model"] == "gemini-3.8-flash"
    assert seen["body"]["max_tokens"] == 16
    assert seen["timeout"] == 10
    assert seen["headers"]["Authorization"] == f"Bearer {_KEY}"
    assert "chat_template_kwargs" not in seen["body"]  # gemini reasoning_style none
    assert _KEY not in str(body)


def test_empty_content_still_counts_as_chat_ok(monkeypatch):
    _patch_get(monkeypatch, _Resp(200, _models("a")))
    _patch_post(monkeypatch, _ok_chat(content=None))
    assert _test("deepseek")["chat_ok"] is True


def test_chat_model_falls_back_to_stored_then_preset_default(monkeypatch):
    _patch_get(monkeypatch, _Resp(200, _models("a")))
    seen = _patch_post(monkeypatch, _ok_chat())
    _test("gemini")
    assert seen["body"]["model"] == "gemini-3.5-flash-lite"
    with _client() as c:
        c.put("/api/llm/provider", json={"profile_id": "p1", "preset": "openai", "model": "gpt-4o", "api_key": _KEY})
        c.post("/api/llm/provider/test", json={"profile_id": "p1", "check_chat": True})
    assert seen["body"]["model"] == "gpt-4o"


def test_chat_stage_respects_drop_params_and_extra_headers(monkeypatch):
    _patch_get(monkeypatch, _Resp(200, _models("a")))
    seen = _patch_post(monkeypatch, _ok_chat())
    _test("anthropic")
    assert "temperature" not in seen["body"] and "top_p" not in seen["body"]
    _test("openrouter", model="x/y")
    assert seen["headers"]["X-OpenRouter-Title"] == "OpenCohost"
    assert seen["body"]["reasoning"] == {"exclude": True}


@pytest.mark.parametrize(
    "status,code",
    [(401, "cloud_auth"), (403, "cloud_auth"), (402, "cloud_rate_limit"), (429, "cloud_rate_limit"),
     (400, "cloud_bad_request"), (404, "cloud_bad_request"), (422, "cloud_bad_request"), (500, "cloud_error")],
)
def test_chat_failure_taxonomy_and_message(monkeypatch, status, code):
    _patch_get(monkeypatch, _Resp(200, _models("a")))
    text = '{"error": {"message": "Insufficient balance ' + _KEY + '"}}'
    _patch_post(monkeypatch, _ChatResp(status, None, text=text))
    body = _test("openai")
    assert body["ok"] is True and body["models_ok"] is True and body["chat_ok"] is False
    assert body["chat_error_code"] == code and body["http_status"] == status
    assert "Insufficient balance" in body["provider_message"]
    assert _KEY not in str(body)


def test_chat_timeout(monkeypatch):
    _patch_get(monkeypatch, _Resp(200, _models("a")))
    _patch_post(monkeypatch, requests.exceptions.Timeout("slow"))
    body = _test("openai")
    assert body["chat_ok"] is False and body["chat_error_code"] == "timeout"
    assert body["http_status"] is None and body["provider_message"] is None


def test_chat_stage_skipped_when_not_requested_or_models_fail(monkeypatch):
    _patch_get(monkeypatch, _Resp(200, _models("a")))
    seen = _patch_post(monkeypatch, _ok_chat())
    with _client() as c:
        body = c.post("/api/llm/provider/test", json={"preset": "openai", "api_key": _KEY}).json()
    assert body["chat_ok"] is None and seen["calls"] == 0
    _patch_get(monkeypatch, _Resp(401))
    body = _test("openai")
    assert body["models_ok"] is False and body["chat_ok"] is None and seen["calls"] == 0
    assert body["http_status"] == 401


def test_chat_stage_log_has_codes_only(monkeypatch, caplog):
    _patch_get(monkeypatch, _Resp(200, _models("a")))
    _patch_post(monkeypatch, _ChatResp(402, None, text='{"error": {"message": "Out of FreeText credits"}}'))
    caplog.set_level(logging.INFO)
    _test("openai")
    assert "[LLM_PROVIDER_TEST] chat preset=openai outcome=cloud_rate_limit http_status=402" in caplog.text
    assert "FreeText" not in caplog.text and _KEY not in caplog.text
