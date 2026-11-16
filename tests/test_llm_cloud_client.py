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

from opencohost.core.providers.cloud.cloud_llm_client import (
    CloudLLMResponseError,
    map_options_to_openai,
    send_chat_completion,
    stream_chat_completion,
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
# map_options_to_openai — reasoning kwargs (nim-cloud-reasoning-off, T1)
# ──────────────────────────────────────────────────────────────────────────


def test_map_options_disables_reasoning_by_default():
    body = map_options_to_openai("nemotron-3.5-lightning", [{"role": "user", "content": "hi"}], {})
    assert body["chat_template_kwargs"] == {"enable_thinking": False, "thinking": False}


def test_map_options_think_true_enables_reasoning():
    body = map_options_to_openai(
        "nemotron-3.5-lightning", [{"role": "user", "content": "hi"}], {"think": True}
    )
    assert body["chat_template_kwargs"] == {"enable_thinking": True, "thinking": True}


def test_map_options_think_true_wins_over_disable_reasoning_off():
    body = map_options_to_openai(
        "nemotron-3.5-lightning",
        [{"role": "user", "content": "hi"}],
        {"think": True},
        disable_reasoning=False,
    )
    assert body["chat_template_kwargs"] == {"enable_thinking": True, "thinking": True}


def test_map_options_omits_kwargs_when_disable_reasoning_off_and_think_not_true():
    body = map_options_to_openai(
        "nemotron-3.5-lightning", [{"role": "user", "content": "hi"}], {}, disable_reasoning=False
    )
    assert "chat_template_kwargs" not in body


@pytest.mark.parametrize("think_value", [True, False, None])
def test_map_options_never_sends_think_as_top_level_key(think_value):
    body = map_options_to_openai(
        "nemotron-3.5-lightning", [{"role": "user", "content": "hi"}], {"think": think_value}
    )
    assert "think" not in body


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

    monkeypatch.setattr("opencohost.core.providers.cloud.cloud_llm_client.requests.post", fake_post)

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


def test_send_default_disable_reasoning_sends_kwargs(monkeypatch):
    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured["json"] = json
        return _FakeResponse(json_data={"choices": [{"message": {"content": "hi"}}], "usage": {}})

    monkeypatch.setattr("opencohost.core.providers.cloud.cloud_llm_client.requests.post", fake_post)

    send_chat_completion(
        base_url="https://api.example.com/v1",
        api_key=_SENTINEL_KEY,
        model="nemotron-3.5-lightning",
        messages=[{"role": "user", "content": "hi"}],
        options={},
        timeout=30,
    )

    assert captured["json"]["chat_template_kwargs"] == {"enable_thinking": False, "thinking": False}


def test_send_disable_reasoning_false_omits_kwargs(monkeypatch):
    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured["json"] = json
        return _FakeResponse(json_data={"choices": [{"message": {"content": "hi"}}], "usage": {}})

    monkeypatch.setattr("opencohost.core.providers.cloud.cloud_llm_client.requests.post", fake_post)

    send_chat_completion(
        base_url="https://api.example.com/v1",
        api_key=_SENTINEL_KEY,
        model="nemotron-3.5-lightning",
        messages=[{"role": "user", "content": "hi"}],
        options={},
        timeout=30,
        disable_reasoning=False,
    )

    assert "chat_template_kwargs" not in captured["json"]


def test_send_normalizes_trailing_slash_on_base_url(monkeypatch):
    """base_url with a trailing slash must not produce a double-slash path
    (e.g. '/v1//chat/completions'), which 404s on strict OpenAI-compatible
    servers."""
    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured["url"] = url
        return _FakeResponse(json_data={"choices": [{"message": {"content": "hi"}}], "usage": {}})

    monkeypatch.setattr("opencohost.core.providers.cloud.cloud_llm_client.requests.post", fake_post)

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

    monkeypatch.setattr("opencohost.core.providers.cloud.cloud_llm_client.requests.post", fake_post)

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
# _adapt_response — inline <think> stripping (nim-cloud-reasoning-off, T3)
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "message_body, expected_content, expected_thinking",
    [
        (
            {"content": "<think>let me reason</think>the answer is 42"},
            "the answer is 42",
            "let me reason",
        ),
        (
            {
                "content": "<think>step one</think>final answer",
                "reasoning_content": "prior reasoning",
            },
            "final answer",
            "prior reasoning\nstep one",
        ),
        (
            # Truncated stream: opening tag never closes -- whole content is reasoning.
            {"content": "<think>never closes, still going"},
            "",
            "never closes, still going",
        ),
        (
            {"content": "plain answer, no tags"},
            "plain answer, no tags",
            "",
        ),
        (
            {"content": "<THINK>upper case tag</THINK>done"},
            "done",
            "upper case tag",
        ),
        (
            {"content": "  <think> padded </think>  padded answer  "},
            "padded answer",
            "padded",
        ),
        (
            # Template-injected opening tag (DeepSeek-R1 style): only the
            # closing tag reaches content -- everything before it is reasoning.
            {"content": "the user asks, as an AI I should</think>the answer is 42"},
            "the answer is 42",
            "the user asks, as an AI I should",
        ),
    ],
)
def test_response_adapter_strips_inline_think_blocks(
    monkeypatch, message_body, expected_content, expected_thinking
):
    def fake_post(url, json=None, headers=None, timeout=None):
        return _FakeResponse(json_data={"choices": [{"message": message_body}], "usage": {}})

    monkeypatch.setattr("opencohost.core.providers.cloud.cloud_llm_client.requests.post", fake_post)

    result = send_chat_completion(
        base_url="https://api.example.com/v1",
        api_key=_SENTINEL_KEY,
        model="nemotron-3.5-lightning",
        messages=[{"role": "user", "content": "what is 6*7"}],
        options={},
        timeout=30,
    )

    assert result["message"]["content"] == expected_content
    assert result["message"]["thinking"] == expected_thinking


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

    monkeypatch.setattr("opencohost.core.providers.cloud.cloud_llm_client.requests.post", fake_post)

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

    monkeypatch.setattr("opencohost.core.providers.cloud.cloud_llm_client.requests.post", fake_post)

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

    monkeypatch.setattr("opencohost.core.providers.cloud.cloud_llm_client.requests.post", fake_post)

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

    monkeypatch.setattr("opencohost.core.providers.cloud.cloud_llm_client.requests.post", fake_post)

    result = send_chat_completion(
        base_url="https://api.example.com/v1",
        api_key=_SENTINEL_KEY,
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": "hi"}],
        options={},
        timeout=30,
    )
    assert result["usage"] == {}


# ──────────────────────────────────────────────────────────────────────────
# stream_chat_completion — SSE streaming (live_session_fixes_20260925 T8)
# ──────────────────────────────────────────────────────────────────────────


class _FakeStreamResponse:
    """Minimal ``requests.Response`` stand-in for ``stream=True`` -- iterable
    SSE lines plus the same status/headers/close surface the streaming client
    uses."""

    def __init__(self, *, status_code=200, lines=None, text="", headers=None, request_headers=None):
        self.status_code = status_code
        self._lines = list(lines or [])
        self.text = text
        self.reason = text or "Error"
        self.headers = headers or {}
        self.request = _FakeRequest(request_headers or {})
        self.closed = False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"{self.status_code} error", response=self)

    def iter_lines(self, decode_unicode=True):
        for line in self._lines:
            yield line

    def close(self):
        self.closed = True


def _sse(*objs):
    """One ``data: <json>`` line per object, terminated by ``data: [DONE]``."""
    import json as _json

    lines = [f"data: {_json.dumps(obj)}" for obj in objs]
    lines.append("data: [DONE]")
    return lines


def test_stream_yields_content_deltas_then_final_usage_chunk(monkeypatch):
    fake = _FakeStreamResponse(
        lines=_sse(
            {"choices": [{"delta": {"content": "Hola "}}]},
            {"choices": [{"delta": {"content": "mundo"}}]},
            {"choices": [{"delta": {}}], "usage": {"completion_tokens": 5, "total_tokens": 20}},
        )
    )
    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None, stream=None):
        captured.update(url=url, json=json, headers=headers, timeout=timeout, stream=stream)
        return fake

    monkeypatch.setattr("opencohost.core.providers.cloud.cloud_llm_client.requests.post", fake_post)

    chunks = list(
        stream_chat_completion(
            base_url="https://api.example.com/v1",
            api_key=_SENTINEL_KEY,
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": "hi"}],
            options={},
            timeout=30,
        )
    )

    assert captured["stream"] is True
    assert captured["json"]["stream"] is True
    assert captured["json"]["stream_options"] == {"include_usage": True}
    assert captured["headers"]["Authorization"] == f"Bearer {_SENTINEL_KEY}"

    content = "".join(c["message"]["content"] for c in chunks if not c.get("done"))
    assert content == "Hola mundo"
    assert chunks[-1] == {"done": True, "usage": {"completion_tokens": 5, "total_tokens": 20}}
    assert fake.closed is True


def test_stream_skips_blank_and_comment_lines(monkeypatch):
    fake = _FakeStreamResponse(
        lines=[
            "",
            ": keepalive",
            *_sse({"choices": [{"delta": {"content": "hola"}}]}),
        ]
    )
    monkeypatch.setattr(
        "opencohost.core.providers.cloud.cloud_llm_client.requests.post",
        lambda *a, **kw: fake,
    )

    chunks = list(
        stream_chat_completion(
            base_url="https://api.example.com/v1",
            api_key=_SENTINEL_KEY,
            model="gpt-4o-mini",
            messages=[],
            options={},
            timeout=30,
        )
    )
    content = "".join(c["message"]["content"] for c in chunks if not c.get("done"))
    assert content == "hola"


def test_stream_routes_reasoning_content_into_thinking(monkeypatch):
    fake = _FakeStreamResponse(
        lines=_sse(
            {"choices": [{"delta": {"reasoning_content": "let me think"}}]},
            {"choices": [{"delta": {"content": "42"}}]},
        )
    )
    monkeypatch.setattr(
        "opencohost.core.providers.cloud.cloud_llm_client.requests.post",
        lambda *a, **kw: fake,
    )

    chunks = list(
        stream_chat_completion(
            base_url="https://api.example.com/v1",
            api_key=_SENTINEL_KEY,
            model="nemotron-3.5-lightning",
            messages=[],
            options={},
            timeout=30,
        )
    )
    thinking = "".join(c["message"]["thinking"] for c in chunks if not c.get("done"))
    content = "".join(c["message"]["content"] for c in chunks if not c.get("done"))
    assert thinking == "let me think"
    assert content == "42"


@pytest.mark.parametrize(
    "deltas, expected_content, expected_thinking",
    [
        # <think> open tag split across two deltas, close tag whole.
        (["<thi", "nk>reasoning here</think>content after"], "content after", "reasoning here"),
        # Close tag split across deltas too.
        (["<think>reasoning", " here</thi", "nk>content after"], "content after", "reasoning here"),
        # Orphan </think> at stream start (template-injected opening, no <think> ever sent).
        (["the model reasoned</thi", "nk>final answer"], "final answer", "the model reasoned"),
        # No tags at all -- passes through as content untouched.
        (["plain ", "content ", "only"], "plain content only", ""),
    ],
)
def test_stream_think_tag_state_machine_across_chunk_boundaries(
    monkeypatch, deltas, expected_content, expected_thinking
):
    fake = _FakeStreamResponse(
        lines=_sse(*[{"choices": [{"delta": {"content": d}}]} for d in deltas])
    )
    monkeypatch.setattr(
        "opencohost.core.providers.cloud.cloud_llm_client.requests.post",
        lambda *a, **kw: fake,
    )

    chunks = list(
        stream_chat_completion(
            base_url="https://api.example.com/v1",
            api_key=_SENTINEL_KEY,
            model="nemotron-3.5-lightning",
            messages=[],
            options={},
            timeout=30,
        )
    )
    content = "".join(c["message"]["content"] for c in chunks if not c.get("done"))
    thinking = "".join(c["message"]["thinking"] for c in chunks if not c.get("done"))
    assert content == expected_content
    assert thinking == expected_thinking


def test_stream_non_2xx_raises_sanitized_error_before_any_chunk(monkeypatch):
    """Same F2 security posture as send_chat_completion: the key must be
    unreachable from the raised exception's object graph, and the error
    surfaces on the FIRST iteration, before any content chunk is produced."""
    fake = _FakeStreamResponse(
        status_code=401,
        text="Unauthorized",
        request_headers={"Authorization": f"Bearer {_SENTINEL_KEY}"},
    )
    monkeypatch.setattr(
        "opencohost.core.providers.cloud.cloud_llm_client.requests.post",
        lambda *a, **kw: fake,
    )

    gen = stream_chat_completion(
        base_url="https://api.example.com/v1",
        api_key=_SENTINEL_KEY,
        model="gpt-4o-mini",
        messages=[],
        options={},
        timeout=30,
    )
    with pytest.raises(CloudLLMResponseError) as excinfo:
        next(gen)
    exc = excinfo.value
    assert _SENTINEL_KEY not in str(exc)
    assert _SENTINEL_KEY not in repr(exc)
    assert getattr(exc, "response", None) is None
    assert getattr(exc, "request", None) is None
    assert exc.__cause__ is None
    assert exc.__context__ is None
    assert fake.closed is True


def test_stream_closes_response_on_early_generator_close(monkeypatch):
    """An aborted turn (guard trip, cancel token) closes the generator early
    -- the underlying HTTP response/socket must close too, freeing the
    connection the same way the local streaming seam frees the Ollama
    runner."""
    # Long enough to clear the pre-first-tag holdback (_PRE_FIRST_TAG_HOLDBACK)
    # so the first line streams out immediately, exercising a real mid-stream
    # abort rather than "nothing was ever yielded yet".
    first_delta = "uno dos tres " * 6  # 78 chars, no '<' anywhere: zero tag risk
    fake = _FakeStreamResponse(
        lines=_sse(
            {"choices": [{"delta": {"content": first_delta}}]},
            {"choices": [{"delta": {"content": "cuatro"}}]},
            {"choices": [{"delta": {"content": "cinco"}}]},
        )
    )
    monkeypatch.setattr(
        "opencohost.core.providers.cloud.cloud_llm_client.requests.post",
        lambda *a, **kw: fake,
    )

    gen = stream_chat_completion(
        base_url="https://api.example.com/v1",
        api_key=_SENTINEL_KEY,
        model="gpt-4o-mini",
        messages=[],
        options={},
        timeout=30,
    )
    first = next(gen)
    # Some non-empty prefix streams immediately (the pre-first-tag holdback
    # keeps a bounded trailing slice in reserve -- see
    # _ThinkTagStreamSplitter -- so this need not be the WHOLE first delta).
    assert first["message"]["content"]
    assert first_delta.startswith(first["message"]["content"])
    assert fake.closed is False
    gen.close()
    assert fake.closed is True


def test_stream_closes_response_on_normal_exhaustion(monkeypatch):
    fake = _FakeStreamResponse(lines=_sse({"choices": [{"delta": {"content": "hola"}}]}))
    monkeypatch.setattr(
        "opencohost.core.providers.cloud.cloud_llm_client.requests.post",
        lambda *a, **kw: fake,
    )

    list(
        stream_chat_completion(
            base_url="https://api.example.com/v1",
            api_key=_SENTINEL_KEY,
            model="gpt-4o-mini",
            messages=[],
            options={},
            timeout=30,
        )
    )
    assert fake.closed is True


def test_stream_ignores_unparseable_data_lines(monkeypatch):
    """A malformed JSON payload on one SSE line must not kill the whole
    stream -- skip it and keep consuming."""
    fake = _FakeStreamResponse(
        lines=[
            "data: {not valid json",
            *_sse({"choices": [{"delta": {"content": "hola"}}]}),
        ]
    )
    monkeypatch.setattr(
        "opencohost.core.providers.cloud.cloud_llm_client.requests.post",
        lambda *a, **kw: fake,
    )

    chunks = list(
        stream_chat_completion(
            base_url="https://api.example.com/v1",
            api_key=_SENTINEL_KEY,
            model="gpt-4o-mini",
            messages=[],
            options={},
            timeout=30,
        )
    )
    content = "".join(c["message"]["content"] for c in chunks if not c.get("done"))
    assert content == "hola"


def test_stream_sends_disable_reasoning_kwargs_by_default(monkeypatch):
    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None, stream=None):
        captured["json"] = json
        return _FakeStreamResponse(lines=_sse())

    monkeypatch.setattr("opencohost.core.providers.cloud.cloud_llm_client.requests.post", fake_post)

    list(
        stream_chat_completion(
            base_url="https://api.example.com/v1",
            api_key=_SENTINEL_KEY,
            model="nemotron-3.5-lightning",
            messages=[],
            options={},
            timeout=30,
        )
    )
    assert captured["json"]["chat_template_kwargs"] == {"enable_thinking": False, "thinking": False}


class _Latin1DefaultStreamResponse(_FakeStreamResponse):
    """Mirrors real ``requests`` for ``text/event-stream`` with no charset:
    the body arrives as UTF-8 bytes, but ``decode_unicode=True`` decodes with
    the ISO-8859-1 default -- the mojibake seen live on 2026-09-25."""

    encoding = "ISO-8859-1"

    def iter_lines(self, decode_unicode=False):
        for line in self._lines:
            raw = line.encode("utf-8")
            yield raw.decode(self.encoding) if decode_unicode else raw


def test_stream_decodes_utf8_even_when_server_omits_charset(monkeypatch):
    import json as _json

    lines = [
        "data: " + _json.dumps({"choices": [{"delta": {"content": "¿Qué tal? configuración"}}]}, ensure_ascii=False),
        "data: [DONE]",
    ]
    fake = _Latin1DefaultStreamResponse(lines=lines)
    monkeypatch.setattr(
        "opencohost.core.providers.cloud.cloud_llm_client.requests.post",
        lambda *a, **k: fake,
    )

    chunks = list(
        stream_chat_completion(
            base_url="https://api.example.com/v1",
            api_key=_SENTINEL_KEY,
            model="z-ai/glm-5.3-flash",
            messages=[{"role": "user", "content": "hola"}],
            options={},
            timeout=30,
        )
    )

    content = "".join(c["message"]["content"] for c in chunks if not c.get("done"))
    assert content == "¿Qué tal? configuración"
