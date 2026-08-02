"""Tests for the OpenAI-compatible cloud LLM client (multi_provider_llm_20260723 Phase 2).

Design 'Option Mapping' + 'Architecture Decisions / Cloud HTTP client': pure
`map_options_to_openai(model, messages, options)` mapping (num_predict ->
max_tokens rename; num_ctx/repeat_penalty/keep_alive dropped; temperature/
top_p/presence_penalty/frequency_penalty pass through) plus a thin
non-streaming client whose response is Ollama-shaped (`message.content`,
`message.thinking` <- `reasoning_content`, `usage.*`) so llm_engine.py's
existing parsing (Phase 3) needs no new branch. This module has zero engine
imports -- it must be importable standalone.
"""

import requests
import pytest

from opencohost.core.cloud_llm_client import (
    CloudLLMResponseError,
    map_options_to_openai,
    send_chat_completion,
)


class _FakeRequest:
    """Mirrors requests.PreparedRequest closely enough for the leak test:
    a real Response's ``.request`` carries the headers actually sent,
    including the Authorization bearer key."""

    def __init__(self, headers):
        self.headers = headers


class _FakeResponse:
    def __init__(
        self,
        status_code=200,
        json_data=None,
        text="",
        raise_json_error=False,
        request_headers=None,
        headers=None,
    ):
        self.status_code = status_code
        self._json_data = json_data
        self.text = text
        self._raise_json_error = raise_json_error
        self.reason = text or "Error"
        self.request = _FakeRequest(request_headers or {})
        # F2 (runtime_findings_batch_20260731 unit 1.1): real requests.Response
        # always has .headers -- send_chat_completion now reads it (bounded
        # subset) to build CloudLLMResponseError.headers.
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            # Mirrors real requests: HTTPError(response=self), and self.request
            # (the PreparedRequest actually sent) carries the Authorization header.
            raise requests.exceptions.HTTPError(f"{self.status_code} error", response=self)

    def json(self):
        if self._raise_json_error:
            raise ValueError("not valid json")
        return self._json_data


_SENTINEL_KEY = "sk-super-secret-do-not-leak-12345"


# ──────────────────────────────────────────────────────────────────────────
# map_options_to_openai — pure mapping (full table in one pass + omission case)
# ──────────────────────────────────────────────────────────────────────────


def test_map_options_full_table_rename_drop_and_passthrough():
    messages = [{"role": "user", "content": "hi"}]
    body = map_options_to_openai(
        "gpt-4o-mini",
        messages,
        {
            "num_predict": 512,
            "num_ctx": 8192,
            "repeat_penalty": 1.1,
            "keep_alive": "5m",
            "temperature": 0.65,
            "top_p": 0.9,
            "presence_penalty": 0.2,
            "frequency_penalty": 0.3,
        },
    )
    assert body["model"] == "gpt-4o-mini"
    assert body["messages"] == messages
    assert body["max_tokens"] == 512
    assert body["temperature"] == 0.65
    assert body["top_p"] == 0.9
    assert body["presence_penalty"] == 0.2
    assert body["frequency_penalty"] == 0.3
    for dropped in ("num_predict", "num_ctx", "repeat_penalty", "keep_alive"):
        assert dropped not in body


def test_map_options_omits_max_tokens_when_num_predict_absent():
    """Reasoning-model retry pops num_predict from options; the mapping must
    not invent a max_tokens cap -- it only renames what's present."""
    body = map_options_to_openai("o1-mini", [{"role": "user", "content": "hi"}], {})
    assert "max_tokens" not in body


# ──────────────────────────────────────────────────────────────────────────
# send_chat_completion — request shape + response adapter
# ──────────────────────────────────────────────────────────────────────────


def test_send_posts_to_chat_completions_with_bearer_auth_and_timeout(monkeypatch):
    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        captured["headers"] = headers
        captured["timeout"] = timeout
        return _FakeResponse(
            json_data={"choices": [{"message": {"content": "hi there"}}], "usage": {}}
        )

    monkeypatch.setattr("opencohost.core.cloud_llm_client.requests.post", fake_post)

    send_chat_completion(
        base_url="https://api.example.com/v1",
        api_key=_SENTINEL_KEY,
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": "hi"}],
        options={"num_predict": 100},
        timeout=42,
    )

    assert captured["url"] == "https://api.example.com/v1/chat/completions"
    assert captured["headers"]["Authorization"] == f"Bearer {_SENTINEL_KEY}"
    assert captured["timeout"] == 42
    assert captured["json"]["max_tokens"] == 100


def test_send_normalizes_trailing_slash_on_base_url(monkeypatch):
    """base_url with a trailing slash must not produce a double-slash path
    (e.g. '/v1//chat/completions'), which 404s on strict OpenAI-compatible
    servers."""
    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured["url"] = url
        return _FakeResponse(json_data={"choices": [{"message": {"content": "hi"}}], "usage": {}})

    monkeypatch.setattr("opencohost.core.cloud_llm_client.requests.post", fake_post)

    send_chat_completion(
        base_url="https://api.example.com/v1/",
        api_key=_SENTINEL_KEY,
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": "hi"}],
        options={},
        timeout=30,
    )

    assert captured["url"] == "https://api.example.com/v1/chat/completions"


@pytest.mark.parametrize(
    "message_body, expected_content, expected_thinking, expected_usage",
    [
        (
            {"content": "the answer is 42", "reasoning_content": "let me think step by step"},
            "the answer is 42",
            "let me think step by step",
            {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        ),
        ({"content": "plain answer"}, "plain answer", "", {}),
    ],
)
def test_response_adapter_maps_content_thinking_and_usage(
    monkeypatch, message_body, expected_content, expected_thinking, expected_usage
):
    def fake_post(url, json=None, headers=None, timeout=None):
        return _FakeResponse(
            json_data={"choices": [{"message": message_body}], "usage": expected_usage}
        )

    monkeypatch.setattr("opencohost.core.cloud_llm_client.requests.post", fake_post)

    result = send_chat_completion(
        base_url="https://api.example.com/v1",
        api_key=_SENTINEL_KEY,
        model="o1-mini",
        messages=[{"role": "user", "content": "what is 6*7"}],
        options={},
        timeout=30,
    )

    assert result["message"]["content"] == expected_content
    assert result["message"]["thinking"] == expected_thinking
    assert result["usage"] == expected_usage


# ──────────────────────────────────────────────────────────────────────────
# error surface — network / timeout / HTTP status / malformed body
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raised",
    [
        requests.exceptions.ConnectionError(
            "connection refused", request=_FakeRequest({"Authorization": f"Bearer {_SENTINEL_KEY}"})
        ),
        requests.exceptions.Timeout(
            "read timed out", request=_FakeRequest({"Authorization": f"Bearer {_SENTINEL_KEY}"})
        ),
    ],
)
def test_send_propagates_network_and_timeout_errors(monkeypatch, raised):
    """ConnectionError/Timeout carry a ``.request`` attribute by requests'
    own design (unlike the HTTP-status path this module wraps in F2, these
    pass through unmodified) -- the engine never serializes that object, so
    only the string form is asserted key-free here."""

    def fake_post(url, json=None, headers=None, timeout=None):
        raise raised

    monkeypatch.setattr("opencohost.core.cloud_llm_client.requests.post", fake_post)

    with pytest.raises(requests.exceptions.RequestException) as excinfo:
        send_chat_completion(
            base_url="https://api.example.com/v1",
            api_key=_SENTINEL_KEY,
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": "hi"}],
            options={},
            timeout=30,
        )
    assert _SENTINEL_KEY not in str(excinfo.value)


def test_send_raises_on_http_error_status(monkeypatch):
    """The key must be unreachable from the raised exception's object graph,
    not merely absent from str/repr: no .response, no .request, no __cause__/
    __context__ chain back to the HTTPError that actually carried it."""

    def fake_post(url, json=None, headers=None, timeout=None):
        return _FakeResponse(
            status_code=401,
            text="Unauthorized",
            request_headers={"Authorization": f"Bearer {_SENTINEL_KEY}"},
        )

    monkeypatch.setattr("opencohost.core.cloud_llm_client.requests.post", fake_post)

    with pytest.raises(CloudLLMResponseError) as excinfo:
        send_chat_completion(
            base_url="https://api.example.com/v1",
            api_key=_SENTINEL_KEY,
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": "hi"}],
            options={},
            timeout=30,
        )
    exc = excinfo.value
    assert _SENTINEL_KEY not in str(exc)
    assert _SENTINEL_KEY not in repr(exc)
    assert getattr(exc, "response", None) is None
    assert getattr(exc, "request", None) is None
    assert exc.__cause__ is None
    assert exc.__context__ is None


@pytest.mark.parametrize(
    "response_kwargs",
    [
        {"raise_json_error": True, "text": "<html>not json</html>"},
        {"json_data": {"usage": {}}},  # missing 'choices' entirely
        {"json_data": {"choices": [{}], "usage": {}}},  # missing 'message'
        {"json_data": {"choices": [], "usage": {}}},  # empty 'choices' list
        {"json_data": [1, 2, 3]},  # top-level body is a JSON array, not an object
    ],
)
def test_send_raises_cloud_llm_response_error_on_malformed_body(monkeypatch, response_kwargs):
    def fake_post(url, json=None, headers=None, timeout=None):
        return _FakeResponse(**response_kwargs)

    monkeypatch.setattr("opencohost.core.cloud_llm_client.requests.post", fake_post)

    with pytest.raises(CloudLLMResponseError) as excinfo:
        send_chat_completion(
            base_url="https://api.example.com/v1",
            api_key=_SENTINEL_KEY,
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": "hi"}],
            options={},
            timeout=30,
        )
    # Must still be catchable by the engine's existing RequestException-based
    # transport-error classifier without any llm_engine.py change -- but must
    # NOT be (or chain from) the raw HTTPError that carries the response/key
    # object graph (F2); a bare isinstance-RequestException check alone is
    # tautological since CloudLLMResponseError is declared as one.
    assert isinstance(excinfo.value, requests.exceptions.RequestException)
    assert not isinstance(excinfo.value, requests.exceptions.HTTPError)
    assert _SENTINEL_KEY not in str(excinfo.value)


def test_response_adapter_defaults_usage_when_key_absent_entirely(monkeypatch):
    """Pin actual behavior: a 2xx body with no 'usage' key at all (not even
    an empty one) still succeeds -- the adapter defaults usage to {}."""

    def fake_post(url, json=None, headers=None, timeout=None):
        return _FakeResponse(json_data={"choices": [{"message": {"content": "hi"}}]})

    monkeypatch.setattr("opencohost.core.cloud_llm_client.requests.post", fake_post)

    result = send_chat_completion(
        base_url="https://api.example.com/v1",
        api_key=_SENTINEL_KEY,
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": "hi"}],
        options={},
        timeout=30,
    )
    assert result["usage"] == {}
