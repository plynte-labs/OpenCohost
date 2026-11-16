"""Provider templates (cloud-providers-vision-20261111 P1): schema + per-preset request shaping."""

from __future__ import annotations

import pytest

from opencohost.config.settings import LLM_PROVIDER_PRESETS
from opencohost.core.engine.llm_inference_service import LLMInferenceService
from opencohost.core.providers.cloud import cloud_llm_client as client
from opencohost.core.providers.cloud.cloud_llm_client import (
    map_options_to_openai,
    send_chat_completion,
    stream_chat_completion,
)

_REASONING_STYLES = {"nim_kwargs", "deepseek_thinking", "openrouter_exclude", "none"}
_MSGS = [{"role": "user", "content": "hi"}]
_OPTS = {"temperature": 0.7, "top_p": 0.9, "num_predict": 50}


class _Resp:
    status_code = 200
    reason = "OK"
    headers: dict = {}
    text = ""

    def raise_for_status(self):
        pass

    def json(self):
        return {"choices": [{"message": {"content": "ok"}}], "usage": {}}


def _capture(monkeypatch):
    seen = {}

    def fake_post(url, json=None, headers=None, timeout=None, **_kw):
        seen.update(url=url, json=json, headers=headers)
        return _Resp()

    monkeypatch.setattr(client.requests, "post", fake_post)
    return seen


# -- schema ------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(LLM_PROVIDER_PRESETS))
def test_every_preset_has_full_schema(name):
    p = LLM_PROVIDER_PRESETS[name]
    assert p["label"] and p["base_url"].startswith("https://")
    assert p["key_url"].startswith("https://")
    assert isinstance(p["extra_headers"], dict)
    assert p["reasoning_style"] in _REASONING_STYLES
    assert isinstance(p["drop_params"], list)
    for m in p["models"]:
        assert isinstance(m["id"], str) and isinstance(m["vision"], bool)
    ids = [m["id"] for m in p["models"]]
    if ids:
        assert p["default_model"] in ids


def test_expected_presets_exist():
    assert {"openai", "nvidia_nim", "gemini", "deepseek", "anthropic", "openrouter"} <= set(
        LLM_PROVIDER_PRESETS
    )


def test_preset_facts():
    P = LLM_PROVIDER_PRESETS
    assert P["gemini"]["base_url"] == "https://generativelanguage.googleapis.com/v1beta/openai/"
    assert P["gemini"]["default_model"] == "gemini-3.5-flash-lite"
    assert P["deepseek"]["reasoning_style"] == "deepseek_thinking"
    vis = {m["id"]: m["vision"] for m in P["deepseek"]["models"]}
    assert vis == {"deepseek-flash": True, "deepseek-v4-pro": False}
    assert P["anthropic"]["drop_params"] == ["temperature", "top_p"]
    assert P["anthropic"]["default_model"] == "claude-haiku-5-5"
    assert P["openrouter"]["extra_headers"]["X-OpenRouter-Title"] == "OpenCohost"
    assert P["nvidia_nim"]["reasoning_style"] == "nim_kwargs"
    assert P["openai"]["reasoning_style"] == "none"
    assert P["openai"]["base_url"] == "https://api.openai.com/v1"
    assert P["nvidia_nim"]["base_url"] == "https://integrate.api.nvidia.com/v1"


# -- body shaping ------------------------------------------------------------


def test_legacy_default_body_unchanged():
    body = map_options_to_openai("m", _MSGS, _OPTS)
    assert body == {
        "model": "m",
        "messages": _MSGS,
        "temperature": 0.7,
        "top_p": 0.9,
        "max_tokens": 50,
        "chat_template_kwargs": {"enable_thinking": False, "thinking": False},
    }


def test_nim_kwargs_only_for_nim_style():
    nim = map_options_to_openai("m", _MSGS, _OPTS, reasoning_style="nim_kwargs")
    assert "chat_template_kwargs" in nim
    for style in ("none", "deepseek_thinking", "openrouter_exclude"):
        assert "chat_template_kwargs" not in map_options_to_openai(
            "m", _MSGS, _OPTS, reasoning_style=style
        )


def test_deepseek_disables_thinking():
    body = map_options_to_openai("m", _MSGS, _OPTS, reasoning_style="deepseek_thinking")
    assert body["thinking"] == {"type": "disabled"}


def test_deepseek_think_true_leaves_thinking_alone():
    body = map_options_to_openai(
        "m", _MSGS, {"think": True}, reasoning_style="deepseek_thinking"
    )
    assert "thinking" not in body


def test_openrouter_excludes_reasoning():
    body = map_options_to_openai("m", _MSGS, _OPTS, reasoning_style="openrouter_exclude")
    assert body["reasoning"] == {"exclude": True}


def test_disable_reasoning_false_adds_nothing():
    for style in ("nim_kwargs", "deepseek_thinking", "openrouter_exclude"):
        body = map_options_to_openai(
            "m", _MSGS, _OPTS, disable_reasoning=False, reasoning_style=style
        )
        assert not ({"chat_template_kwargs", "thinking", "reasoning"} & set(body))


def test_drop_params_removes_sampling():
    body = map_options_to_openai(
        "m", _MSGS, _OPTS, reasoning_style="none", drop_params=["temperature", "top_p"]
    )
    assert "temperature" not in body and "top_p" not in body
    assert body["max_tokens"] == 50


def test_send_applies_headers_and_style(monkeypatch):
    seen = _capture(monkeypatch)
    send_chat_completion(
        "https://openrouter.ai/api/v1", "KEY", "m", _MSGS, _OPTS, 5,
        reasoning_style="openrouter_exclude",
        extra_headers={"X-OpenRouter-Title": "OpenCohost"},
    )
    assert seen["headers"]["X-OpenRouter-Title"] == "OpenCohost"
    assert seen["headers"]["Authorization"] == "Bearer KEY"
    assert seen["json"]["reasoning"] == {"exclude": True}
    assert "chat_template_kwargs" not in seen["json"]


def test_stream_applies_headers_and_drop(monkeypatch):
    seen = {}

    class S(_Resp):
        def iter_lines(self):
            return iter([b"data: [DONE]"])

        def close(self):
            pass

    def fake_post(url, json=None, headers=None, **_kw):
        seen.update(json=json, headers=headers)
        return S()

    monkeypatch.setattr(client.requests, "post", fake_post)
    list(
        stream_chat_completion(
            "https://api.anthropic.com/v1/", "KEY", "m", _MSGS, _OPTS, 5,
            reasoning_style="none", drop_params=["temperature", "top_p"],
            extra_headers={"X-A": "1"},
        )
    )
    assert seen["headers"]["X-A"] == "1"
    assert "temperature" not in seen["json"] and "top_p" not in seen["json"]


# -- service threads the profile preset --------------------------------------


class _FakeCloud:
    CloudLLMResponseError = client.CloudLLMResponseError

    def __init__(self):
        self.calls = []

    def send_chat_completion(self, **kw):
        self.calls.append(kw)
        return {"message": {"content": "x", "thinking": ""}}


def _chat(profile):
    fake = _FakeCloud()
    svc = LLMInferenceService(cloud_client_module=fake, api_key_resolver=lambda _: "k")
    cfg = {"active_provider": "p", "profiles": {"p": profile}}
    svc.cloud_chat(provider_cfg=cfg, messages=_MSGS, options={})
    return fake.calls[0]


def test_service_passes_preset_settings():
    call = _chat({"base_url": "https://api.anthropic.com/v1/", "model": "m", "preset": "anthropic"})
    assert call["drop_params"] == ["temperature", "top_p"]
    assert call["reasoning_style"] == "none"


def test_service_passes_openrouter_headers():
    call = _chat({"base_url": "https://openrouter.ai/api/v1", "model": "m", "preset": "openrouter"})
    assert call["extra_headers"]["X-OpenRouter-Title"] == "OpenCohost"
    assert call["reasoning_style"] == "openrouter_exclude"


def test_service_custom_profile_keeps_legacy_call_shape():
    call = _chat({"base_url": "https://x.example/v1", "model": "m"})
    assert "reasoning_style" not in call and "extra_headers" not in call
    assert "drop_params" not in call
