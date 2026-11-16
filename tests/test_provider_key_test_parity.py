"""Key test <-> chat path request parity (gemini-chat-parity-20261112 G1).

"Key OK" must mean "chat works": the provider key test sends the same cloud
request shape the real chat path sends (sampling params, penalties, streaming
with stream_options, reasoning params, dropped params, extra headers). Only
the reply cap and the messages differ. HTTP is always faked: no network.
"""

import json

import pytest
import requests
from fastapi.testclient import TestClient

from opencohost.config import settings
from opencohost.config.settings import LLM_PROVIDER_PRESETS
from tests.test_api_phase1 import FakeHost

_ORIGINS = ["http://localhost:5173"]
_KEY = "sk-SENTINEL-parity-do-not-leak-987654"


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    import opencohost.api.main as main_mod
    import opencohost.config.llm_provider as llm_provider_mod

    main_mod._host_active = False
    monkeypatch.setattr(
        llm_provider_mod, "LLM_PROVIDER_CONFIG_FILE", str(tmp_path / "llm_provider.json")
    )
    monkeypatch.setattr(main_mod, "LLM_KEYS_FILE", str(tmp_path / "llm_keys.json"))
    yield
    main_mod._host_active = False


class _ModelsResp:
    status_code = 200
    headers: dict = {}
    text = ""

    def json(self):
        return {"data": [{"id": "gemini-3.5-flash-lite"}, {"id": "x/y"}]}


class _StreamResp:
    """Fake SSE response; records how many lines were read and closing."""

    def __init__(self, status=200, pieces=("OK",), text="", reason="Bad Request"):
        self.status_code = status
        self.reason = reason
        self.text = text
        self.headers: dict = {}
        self._pieces = list(pieces)
        self.lines_read = 0
        self.closed = False

    def raise_for_status(self):
        if self.status_code >= 400:
            err = requests.exceptions.HTTPError(str(self.status_code))
            err.response = self
            raise err

    def iter_lines(self):
        for piece in self._pieces:
            self.lines_read += 1
            yield ("data: " + json.dumps({"choices": [{"delta": {"content": piece}}]})).encode()
        self.lines_read += 1
        yield b"data: [DONE]"

    def close(self):
        self.closed = True


def _capture_post(monkeypatch, resp_factory, url_prefix=""):
    """Patch ``requests.post`` and record the provider calls.

    ``requests.post`` is process-global, so a daemon thread left running by an
    earlier test (speech, health) can post while it is patched. Only calls to
    ``url_prefix`` (the provider under test) are recorded, which keeps the
    one-request-per-probe assertions independent of test order and timing.
    """
    calls = []

    def fake_post(url, json=None, headers=None, timeout=None, stream=False, **kw):
        resp = resp_factory()
        if str(url).startswith(url_prefix):
            calls.append(
                {"url": url, "body": json, "headers": headers, "timeout": timeout,
                 "stream": stream, "resp": resp}
            )
        return resp

    monkeypatch.setattr(requests, "post", fake_post)
    monkeypatch.setattr(requests, "get", lambda *a, **k: _ModelsResp())
    return calls


def _chat_path_request(monkeypatch, preset_id, model):
    """One real chat-path cloud turn (source=chat) through the streaming dispatch."""
    from opencohost.core.context.prompt_assembler import sampling_options
    from opencohost.core.engine.llm_inference_service import LLMInferenceService

    preset = LLM_PROVIDER_PRESETS[preset_id]
    calls = _capture_post(monkeypatch, _StreamResp, preset["base_url"])
    cfg = {
        "active_provider": "p1",
        "profiles": {"p1": {"preset": preset_id, "base_url": preset["base_url"], "model": model}},
    }
    # Same option dict the assembler builds for a chat turn on a cloud model.
    options = {**sampling_options("chat"), "num_predict": 300, "num_ctx": 8192}
    svc = LLMInferenceService(api_key_resolver=lambda _: _KEY)
    list(
        svc._cloud_chat_streaming(
            provider_cfg=cfg, messages=[{"role": "user", "content": "hi"}], options=options
        )
    )
    assert len(calls) == 1
    return calls[0]


def _key_test_request(monkeypatch, preset_id, model, resp_factory=_StreamResp):
    import opencohost.api.main as main_mod

    calls = _capture_post(monkeypatch, resp_factory, LLM_PROVIDER_PRESETS[preset_id]["base_url"])
    with TestClient(main_mod.create_app(host_factory=FakeHost, cors_origins=_ORIGINS)) as c:
        body = c.post(
            "/api/llm/provider/test",
            json={"preset": preset_id, "api_key": _KEY, "check_chat": True, "model": model},
        ).json()
    return body, calls


@pytest.mark.parametrize(
    "preset_id,model",
    [("gemini", "gemini-3.5-flash-lite"), ("openrouter", "x/y"), ("anthropic", "claude-haiku-5-5"),
     ("nvidia_nim", "meta/llama-3.1-70b-instruct"), ("deepseek", "deepseek-flash")],
)
def test_key_test_sends_the_chat_path_request_shape(monkeypatch, preset_id, model):
    chat = _chat_path_request(monkeypatch, preset_id, model)
    body, calls = _key_test_request(monkeypatch, preset_id, model)
    assert body["chat_ok"] is True and len(calls) == 1
    probe = calls[0]

    ignored = {"max_tokens", "messages"}
    assert set(probe["body"]) - ignored == set(chat["body"]) - ignored
    for key in set(chat["body"]) - ignored:
        assert probe["body"][key] == chat["body"][key], key
    assert probe["stream"] is True and chat["stream"] is True
    assert probe["url"] == chat["url"]
    assert probe["headers"] == chat["headers"]
    assert probe["body"]["max_tokens"] == 16
    assert _KEY not in str(body)


def test_key_test_chat_shape_carries_chat_penalties_and_stream_options(monkeypatch):
    # Gemini drops the penalties (G2); openrouter keeps the full chat shape.
    _, calls = _key_test_request(monkeypatch, "openrouter", "x/y")
    sent = calls[0]["body"]
    assert sent["presence_penalty"] == settings.CHAT_PRESENCE_PENALTY
    assert sent["frequency_penalty"] == settings.CHAT_FREQUENCY_PENALTY
    assert sent["stream"] is True and sent["stream_options"] == {"include_usage": True}
    assert calls[0]["timeout"] == 10


def test_key_test_stops_after_first_content_and_closes_stream(monkeypatch):
    made = []

    def factory():
        # Longer than the think-tag splitter's 64-char first-tag holdback, so
        # the first SSE line already yields a content delta.
        made.append(_StreamResp(pieces=("OK. " * 20, "more", "and more")))
        return made[-1]

    body, _ = _key_test_request(monkeypatch, "gemini", "gemini-3.5-flash-lite", factory)
    assert body["chat_ok"] is True and body["http_status"] == 200
    assert made[0].lines_read == 1
    assert made[0].closed is True


def test_key_test_empty_stream_still_counts_as_chat_ok(monkeypatch):
    body, _ = _key_test_request(
        monkeypatch, "gemini", "gemini-3.5-flash-lite", lambda: _StreamResp(pieces=())
    )
    assert body["chat_ok"] is True and body["chat_error_code"] is None


def test_key_test_400_on_the_real_shape_is_reported_sanitized(monkeypatch):
    text = (
        '{"error": {"code": 400, "message": "Invalid JSON payload received. Unknown name '
        '\\"frequency_penalty\\" ' + _KEY + '", "status": "INVALID_ARGUMENT"}}'
    )
    body, calls = _key_test_request(
        monkeypatch, "gemini", "gemini-3.5-flash-lite", lambda: _StreamResp(400, text=text)
    )
    assert body["models_ok"] is True and body["chat_ok"] is False
    assert body["http_status"] == 400
    assert body["chat_error_code"] == "cloud_bad_request"
    assert "frequency_penalty" in body["provider_message"]
    assert _KEY not in str(body)
    assert calls[0]["resp"].closed is True


def test_sampling_options_by_source():
    from opencohost.core.context.prompt_assembler import OWNER_POLICY_SOURCES, sampling_options

    chat = sampling_options("chat")
    assert chat == {
        "temperature": settings.LLM_TEMPERATURE,
        "top_p": settings.LLM_TOP_P,
        "repeat_penalty": settings.CHAT_REPEAT_PENALTY,
        "presence_penalty": settings.CHAT_PRESENCE_PENALTY,
        "frequency_penalty": settings.CHAT_FREQUENCY_PENALTY,
    }
    owner = sampling_options(next(iter(OWNER_POLICY_SOURCES)))
    assert owner == {
        "temperature": settings.LLM_TEMPERATURE,
        "top_p": settings.LLM_TOP_P,
        "repeat_penalty": settings.OWNER_REPEAT_PENALTY,
        "presence_penalty": settings.OWNER_PRESENCE_PENALTY,
    }
    assert sampling_options("other") == {
        "temperature": settings.LLM_TEMPERATURE,
        "top_p": settings.LLM_TOP_P,
    }


def test_preset_request_kwargs_is_the_one_shared_preset_shape():
    from opencohost.core.engine import setup_inference
    from opencohost.core.engine.llm_inference_service import LLMInferenceService
    from opencohost.core.providers.cloud.cloud_llm_client import preset_request_kwargs

    preset = LLM_PROVIDER_PRESETS["openrouter"]
    expected = {
        "reasoning_style": preset["reasoning_style"],
        "extra_headers": dict(preset["extra_headers"]),
        "drop_params": list(preset["drop_params"]),
    }
    assert preset_request_kwargs(preset) == expected
    assert preset_request_kwargs(None) == {}
    cfg = {"active_provider": "p", "profiles": {"p": {"preset": "openrouter"}}}
    assert LLMInferenceService._cloud_preset_kwargs(cfg) == expected
    assert setup_inference._preset_kwargs({"preset": "openrouter"}) == expected


def test_drop_params_also_drops_stream_options(monkeypatch):
    from opencohost.core.providers.cloud.cloud_llm_client import stream_chat_completion

    calls = _capture_post(monkeypatch, _StreamResp)
    list(
        stream_chat_completion(
            "https://x.test/v1", _KEY, "m", [], {"num_predict": 4}, 5,
            drop_params=["stream_options", "stream"],
        )
    )
    sent = calls[0]["body"]
    assert "stream_options" not in sent
    assert sent["stream"] is True  # never droppable: the client parses SSE


# --- G2: Gemini rejects the penalty parameters (live evidence 2026-11-12) -----

_PENALTIES = {"presence_penalty", "frequency_penalty"}


class _BufferedResp:
    status_code = 200
    headers: dict = {}
    text = ""

    def raise_for_status(self):
        return None

    def json(self):
        return {"choices": [{"message": {"content": "ok"}}], "usage": {}}


def _chat_dispatch_body(monkeypatch, preset_id, source, streaming):
    from opencohost.core.context.prompt_assembler import sampling_options
    from opencohost.core.engine.llm_inference_service import LLMInferenceService

    calls = _capture_post(
        monkeypatch, _StreamResp if streaming else _BufferedResp
    )
    preset = LLM_PROVIDER_PRESETS[preset_id]
    cfg = {
        "active_provider": "p1",
        "profiles": {
            "p1": {"preset": preset_id, "base_url": preset["base_url"], "model": "m"}
        },
    }
    options = {**sampling_options(source), "num_predict": 300}
    svc = LLMInferenceService(api_key_resolver=lambda _: _KEY)
    msgs = [{"role": "user", "content": "hi"}]
    if streaming:
        list(svc._cloud_chat_streaming(provider_cfg=cfg, messages=msgs, options=options))
    else:
        svc.cloud_chat(provider_cfg=cfg, messages=msgs, options=options)
    assert len(calls) == 1
    return calls[0]["body"]


def test_gemini_preset_drops_both_penalties():
    assert _PENALTIES <= set(LLM_PROVIDER_PRESETS["gemini"]["drop_params"])


@pytest.mark.parametrize("source", ["chat", "direct"])
@pytest.mark.parametrize("streaming", [True, False])
def test_gemini_chat_request_has_no_penalty_keys(monkeypatch, source, streaming):
    sent = _chat_dispatch_body(monkeypatch, "gemini", source, streaming)
    assert not (_PENALTIES & set(sent))
    assert "temperature" in sent


def test_gemini_key_test_request_has_no_penalty_keys(monkeypatch):
    body, calls = _key_test_request(monkeypatch, "gemini", "gemini-3.5-flash-lite")
    assert body["chat_ok"] is True
    assert not (_PENALTIES & set(calls[0]["body"]))


def test_gemini_setup_speed_test_preset_kwargs_drop_penalties():
    from opencohost.core.engine import setup_inference

    kw = setup_inference._preset_kwargs({"preset": "gemini"})
    assert _PENALTIES <= set(kw["drop_params"])


@pytest.mark.parametrize("preset_id", ["openai", "openrouter", "deepseek", "nvidia_nim"])
def test_other_presets_keep_their_penalties(monkeypatch, preset_id):
    sent = _chat_dispatch_body(monkeypatch, preset_id, "chat", True)
    assert _PENALTIES <= set(sent)
