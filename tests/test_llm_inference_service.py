"""Tests for the isolated ``LLMInferenceService``."""

from __future__ import annotations

from unittest.mock import MagicMock
import pytest

import opencohost.core.engine.llm_inference_service as llm_inference_service
from opencohost.core.engine.llm_inference_service import (
    InferenceRequest,
    InferenceResponse,
    LLMInferenceService,
    configured_cloud_model,
)
from opencohost.core.providers.cloud.cloud_llm_client import CloudLLMResponseError
from opencohost.core.speech.sentence_splitter import SentenceSplitter


def test_local_chat_execution_success():
    fake_response = {"message": {"content": "Hola mundo local"}}
    mock_ollama = MagicMock()
    mock_ollama.chat.return_value = fake_response

    service = LLMInferenceService(ollama_client=mock_ollama)
    request = InferenceRequest(
        messages=[{"role": "user", "content": "Hola"}],
        model="qwen3:8b",
        is_local=True,
    )

    result = service.execute_chat(request)
    assert isinstance(result, InferenceResponse)
    assert result.text == "Hola mundo local"
    assert result.provider == "local"
    assert result.is_local is True
    assert result.gen_ms >= 0
    assert result.error is None
    mock_ollama.chat.assert_called_once_with(
        model="qwen3:8b",
        messages=[{"role": "user", "content": "Hola"}],
        options={},
    )


def test_cloud_chat_execution_success():
    fake_response = {"text": "Hola mundo cloud"}
    mock_cloud = MagicMock()
    mock_cloud.send_chat_completion.return_value = fake_response

    service = LLMInferenceService(
        cloud_client_module=mock_cloud,
        api_key_resolver=lambda _: "test_key",
    )
    request = InferenceRequest(
        messages=[{"role": "user", "content": "Hola"}],
        model="deepseek-v3",
        is_local=False,
        provider_config={
            "active_provider": "deepseek",
            "profiles": {
                "deepseek": {
                    "base_url": "https://api.deepseek.com",
                    "model": "deepseek-v3",
                }
            },
        },
    )

    result = service.execute_chat(request)
    assert isinstance(result, InferenceResponse)
    assert result.text == "Hola mundo cloud"
    assert result.provider == "deepseek"
    assert result.is_local is False
    assert result.gen_ms >= 0
    assert result.error is None
    mock_cloud.send_chat_completion.assert_called_once()


def test_missing_cloud_profile_has_no_identity_model_and_dispatch_still_fails_closed():
    provider_cfg = {
        "active_provider": "nvidia_nim",
        "profiles": {"openai": {"model": "gpt-test"}},
    }
    api_key_resolver = MagicMock(return_value="unused_fake_key")
    service = LLMInferenceService(api_key_resolver=api_key_resolver)

    assert configured_cloud_model(provider_cfg) == ""
    with pytest.raises(CloudLLMResponseError, match="no profile configured"):
        service._resolve_cloud_dispatch(provider_cfg, {})
    api_key_resolver.assert_not_called()


def test_watchdog_refuses_stream():
    service = LLMInferenceService()
    with pytest.raises(ValueError, match="cannot supervise stream=True"):
        service.call_with_watchdog(lambda: None, timeout=5.0, stream=True)


# ──────────────────────────────────────────────────────────────────────────
# cloud_chat — reasoning kwargs (nim-cloud-reasoning-off, T1/T2)
# ──────────────────────────────────────────────────────────────────────────

_REASONING_CFG = {
    "active_provider": "nvidia_nim",
    "profiles": {
        "nvidia_nim": {
            "base_url": "https://integrate.api.nvidia.com/v1",
            "model": "nemotron-3.5-lightning",
        }
    },
}

_REASONING_KWARGS_400 = (
    '{"message":"chat_template_kwargs is not supported for this model",'
    '"type":"Bad Request","code":400}'
)


def _reasoning_400():
    return CloudLLMResponseError(
        "cloud chat HTTP 400",
        status_code=400,
        headers={},
        body_excerpt=_REASONING_KWARGS_400,
    )


class _RecordingCloud:
    """Records every send_chat_completion call, always succeeds."""

    CloudLLMResponseError = CloudLLMResponseError

    def __init__(self):
        self.calls: list[dict] = []

    def send_chat_completion(self, **kwargs):
        self.calls.append(kwargs)
        return {"message": {"content": "ok", "thinking": ""}}


class _RejectsReasoningKwargsCloud:
    """Fails with a chat_template_kwargs 400 until disable_reasoning is False."""

    CloudLLMResponseError = CloudLLMResponseError

    def __init__(self):
        self.calls: list[dict] = []

    def send_chat_completion(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs.get("disable_reasoning", True):
            raise _reasoning_400()
        return {"message": {"content": "pong", "thinking": ""}}


def test_cloud_chat_sends_disable_reasoning_true_by_default():
    fake = _RecordingCloud()
    service = LLMInferenceService(cloud_client_module=fake, api_key_resolver=lambda _: "k")
    service.cloud_chat(provider_cfg=_REASONING_CFG, messages=[], options={}, timeout=5.0)
    assert fake.calls[0]["disable_reasoning"] is True


def test_cloud_chat_respects_disabled_setting(monkeypatch):
    monkeypatch.setattr(llm_inference_service, "CLOUD_DISABLE_REASONING", False)
    fake = _RecordingCloud()
    service = LLMInferenceService(cloud_client_module=fake, api_key_resolver=lambda _: "k")
    service.cloud_chat(provider_cfg=_REASONING_CFG, messages=[], options={}, timeout=5.0)
    assert fake.calls[0]["disable_reasoning"] is False


def test_cloud_chat_retries_once_without_reasoning_kwargs_on_400():
    fake = _RejectsReasoningKwargsCloud()
    service = LLMInferenceService(cloud_client_module=fake, api_key_resolver=lambda _: "k")
    out = service.cloud_chat(
        provider_cfg=_REASONING_CFG,
        messages=[{"role": "user", "content": "ping"}],
        options={},
        timeout=5.0,
    )
    assert out["message"]["content"] == "pong"
    assert len(fake.calls) == 2
    assert fake.calls[0]["disable_reasoning"] is True
    assert fake.calls[1]["disable_reasoning"] is False


def test_cloud_chat_caches_no_reasoning_kwargs_per_base_url_and_model():
    fake = _RejectsReasoningKwargsCloud()
    service = LLMInferenceService(cloud_client_module=fake, api_key_resolver=lambda _: "k")
    kwargs = dict(
        provider_cfg=_REASONING_CFG,
        messages=[{"role": "user", "content": "ping"}],
        options={},
        timeout=5.0,
    )
    service.cloud_chat(**kwargs)
    service.cloud_chat(**kwargs)
    # First turn: 2 calls (400 then retry). Second turn skips the 400 entirely.
    assert len(fake.calls) == 3
    assert fake.calls[2]["disable_reasoning"] is False


def test_cloud_chat_reasoning_kwargs_retry_does_not_loop_if_still_failing():
    class AlwaysReject(_RejectsReasoningKwargsCloud):
        def send_chat_completion(self, **kwargs):
            self.calls.append(kwargs)
            raise _reasoning_400()

    fake = AlwaysReject()
    service = LLMInferenceService(cloud_client_module=fake, api_key_resolver=lambda _: "k")
    with pytest.raises(CloudLLMResponseError):
        service.cloud_chat(provider_cfg=_REASONING_CFG, messages=[], options={}, timeout=5.0)
    # One original attempt + one retry-without-kwargs attempt, then re-raise.
    assert len(fake.calls) == 2


def test_cloud_chat_non_reasoning_400_still_reraises_without_retry():
    """A 400 that does NOT mention chat_template_kwargs must not trigger the
    reasoning-kwargs retry path -- only the existing immutable-param path."""

    class Vague400(_RecordingCloud):
        def send_chat_completion(self, **kwargs):
            self.calls.append(kwargs)
            raise CloudLLMResponseError(
                "cloud chat HTTP 400",
                status_code=400,
                headers={},
                body_excerpt='{"message":"Bad Request"}',
            )

    fake = Vague400()
    service = LLMInferenceService(cloud_client_module=fake, api_key_resolver=lambda _: "k")
    with pytest.raises(CloudLLMResponseError):
        service.cloud_chat(provider_cfg=_REASONING_CFG, messages=[], options={}, timeout=5.0)
    assert len(fake.calls) == 1


def test_chat_streaming_yields_chunks_and_closes():
    mock_stream = MagicMock()
    mock_stream.__iter__.return_value = [
        {"message": {"content": "Hola "}},
        {"message": {"content": "mundo!"}},
    ]
    mock_client = MagicMock()
    mock_client.chat.return_value = mock_stream

    service = LLMInferenceService()
    chunks = list(
        service.chat_streaming(
            timeout=5.0,
            chat_client=mock_client,
            model="qwen3:8b",
            messages=[{"role": "user", "content": "test"}],
        )
    )
    assert len(chunks) == 2
    mock_stream.close.assert_called_once()


class _RecordingStreamCloud:
    """Records every stream_chat_completion call; yields the given chunks (an
    item that is an exception instance is raised in place of a yield, for a
    mid-stream failure)."""

    CloudLLMResponseError = CloudLLMResponseError

    def __init__(self, chunks=None):
        self.calls: list[dict] = []
        self._chunks = list(chunks or [])

    def stream_chat_completion(self, **kwargs):
        self.calls.append(kwargs)
        chunks = self._chunks

        def _gen():
            for item in chunks:
                if isinstance(item, BaseException):
                    raise item
                yield item

        return _gen()


class _RejectsReasoningKwargsStreamCloud:
    """Fails with a chat_template_kwargs 400 BEFORE any chunk until
    disable_reasoning is False (T8's streaming analogue of
    _RejectsReasoningKwargsCloud above)."""

    CloudLLMResponseError = CloudLLMResponseError

    def __init__(self):
        self.calls: list[dict] = []

    def stream_chat_completion(self, **kwargs):
        self.calls.append(kwargs)
        disable_reasoning = kwargs.get("disable_reasoning", True)

        def _gen():
            if disable_reasoning:
                raise _reasoning_400()
            yield {"message": {"content": "pong", "thinking": ""}, "done": False}
            yield {"done": True, "usage": {}}

        return _gen()


def test_chat_streaming_dispatches_to_cloud_when_not_local():
    fake = _RecordingStreamCloud(
        [
            {"message": {"content": "Hola ", "thinking": ""}, "done": False},
            {"message": {"content": "mundo", "thinking": ""}, "done": False},
            {"done": True, "usage": {"completion_tokens": 3}},
        ]
    )
    service = LLMInferenceService(cloud_client_module=fake, api_key_resolver=lambda _: "k")

    chunks = list(
        service.chat_streaming(
            timeout=5.0,
            is_local=False,
            provider_cfg=_REASONING_CFG,
            messages=[{"role": "user", "content": "hi"}],
            options={},
        )
    )

    content_chunks = [c for c in chunks if not c.get("done")]
    assert content_chunks == [
        {"message": {"content": "Hola ", "thinking": ""}, "done": False},
        {"message": {"content": "mundo", "thinking": ""}, "done": False},
    ]
    assert chunks[-1] == {"done": True, "usage": {"completion_tokens": 3}}
    assert len(fake.calls) == 1
    assert fake.calls[0]["disable_reasoning"] is True


def test_cloud_streaming_applies_cached_immutable_params():
    fake = _RecordingStreamCloud([{"done": True, "usage": {}}])
    service = LLMInferenceService(cloud_client_module=fake, api_key_resolver=lambda _: "k")
    cache_key = ("https://integrate.api.nvidia.com/v1", "nemotron-3.5-lightning")
    service._immutable_param_cache[cache_key] = {"top_p": 0.95}

    list(
        service.chat_streaming(
            timeout=5.0,
            is_local=False,
            provider_cfg=_REASONING_CFG,
            messages=[],
            options={"top_p": 0.9},
        )
    )

    assert fake.calls[0]["options"]["top_p"] == 0.95


def test_cloud_streaming_retries_once_without_reasoning_kwargs_on_400_before_first_chunk():
    fake = _RejectsReasoningKwargsStreamCloud()
    service = LLMInferenceService(cloud_client_module=fake, api_key_resolver=lambda _: "k")

    chunks = list(
        service.chat_streaming(
            timeout=5.0,
            is_local=False,
            provider_cfg=_REASONING_CFG,
            messages=[{"role": "user", "content": "ping"}],
            options={},
        )
    )

    content = "".join(c["message"]["content"] for c in chunks if not c.get("done"))
    assert content == "pong"
    assert len(fake.calls) == 2
    assert fake.calls[0]["disable_reasoning"] is True
    assert fake.calls[1]["disable_reasoning"] is False


def test_cloud_streaming_no_reasoning_kwargs_cache_shared_with_buffered_cloud_chat():
    """The learned per-(base_url, model) cache is ONE cache, read by both
    transports -- a lesson learned on the streaming leg must not be re-paid
    by a later buffered call for the same model."""

    class Mixed:
        CloudLLMResponseError = CloudLLMResponseError

        def __init__(self):
            self.stream_calls: list[dict] = []
            self.send_calls: list[dict] = []

        def stream_chat_completion(self, **kwargs):
            self.stream_calls.append(kwargs)
            disable_reasoning = kwargs.get("disable_reasoning", True)

            def _gen():
                if disable_reasoning:
                    raise _reasoning_400()
                yield {"done": True, "usage": {}}

            return _gen()

        def send_chat_completion(self, **kwargs):
            self.send_calls.append(kwargs)
            return {"message": {"content": "ok", "thinking": ""}}

    fake = Mixed()
    service = LLMInferenceService(cloud_client_module=fake, api_key_resolver=lambda _: "k")

    list(
        service.chat_streaming(
            timeout=5.0, is_local=False, provider_cfg=_REASONING_CFG, messages=[], options={}
        )
    )
    assert len(fake.stream_calls) == 2

    service.cloud_chat(provider_cfg=_REASONING_CFG, messages=[], options={}, timeout=5.0)
    assert fake.send_calls[0]["disable_reasoning"] is False


def test_cloud_streaming_first_chunk_timeout_raises_watchdog_error(monkeypatch):
    monkeypatch.setattr(llm_inference_service, "CLOUD_STREAM_FIRST_CHUNK_TIMEOUT_SECONDS", 5)
    monkeypatch.setattr(llm_inference_service, "CLOUD_STREAM_IDLE_TIMEOUT_SECONDS", 3)
    fake = _RecordingStreamCloud(
        [{"message": {"content": "late", "thinking": ""}, "done": False}]
    )
    service = LLMInferenceService(cloud_client_module=fake, api_key_resolver=lambda _: "k")
    times = iter([0.0, 10.0])  # started=0.0; the one chunk arrives at t=10 (> 5s budget)

    with pytest.raises(TimeoutError, match=r"watchdog_timeout:5\.00s"):
        list(
            service.chat_streaming(
                timeout=60.0,
                is_local=False,
                provider_cfg=_REASONING_CFG,
                messages=[],
                options={},
                time_fn=lambda: next(times),
            )
        )


def test_cloud_streaming_idle_timeout_raises_watchdog_error(monkeypatch):
    monkeypatch.setattr(llm_inference_service, "CLOUD_STREAM_FIRST_CHUNK_TIMEOUT_SECONDS", 30)
    monkeypatch.setattr(llm_inference_service, "CLOUD_STREAM_IDLE_TIMEOUT_SECONDS", 5)
    fake = _RecordingStreamCloud(
        [
            {"message": {"content": "uno", "thinking": ""}, "done": False},
            {"message": {"content": "dos", "thinking": ""}, "done": False},
        ]
    )
    service = LLMInferenceService(cloud_client_module=fake, api_key_resolver=lambda _: "k")
    # started=0.0; chunk 1 at t=1 (within the 30s first-chunk budget); chunk 2
    # at t=20 -- 19s since chunk 1, over the 5s IDLE budget (well under the
    # 60s total, so only the idle check can be what fires).
    times = iter([0.0, 1.0, 20.0])
    gen = service.chat_streaming(
        timeout=60.0,
        is_local=False,
        provider_cfg=_REASONING_CFG,
        messages=[],
        options={},
        time_fn=lambda: next(times),
    )

    first = next(gen)
    assert first["message"]["content"] == "uno"
    with pytest.raises(TimeoutError, match=r"watchdog_timeout:5\.00s"):
        next(gen)


def test_cloud_streaming_total_budget_bounded_by_timeout(monkeypatch):
    monkeypatch.setattr(llm_inference_service, "CLOUD_STREAM_FIRST_CHUNK_TIMEOUT_SECONDS", 30)
    monkeypatch.setattr(llm_inference_service, "CLOUD_STREAM_IDLE_TIMEOUT_SECONDS", 30)
    fake = _RecordingStreamCloud(
        [
            {"message": {"content": "uno", "thinking": ""}, "done": False},
            {"message": {"content": "dos", "thinking": ""}, "done": False},
        ]
    )
    service = LLMInferenceService(cloud_client_module=fake, api_key_resolver=lambda _: "k")
    # total budget=10 (the `timeout` kwarg); chunk 2 at t=12 exceeds the TOTAL
    # cap even though its 11s idle gap is well under the 30s idle budget.
    times = iter([0.0, 1.0, 12.0])
    gen = service.chat_streaming(
        timeout=10.0,
        is_local=False,
        provider_cfg=_REASONING_CFG,
        messages=[],
        options={},
        time_fn=lambda: next(times),
    )

    next(gen)
    with pytest.raises(TimeoutError, match=r"watchdog_timeout:10\.00s"):
        next(gen)


def test_cloud_streaming_mid_stream_failure_propagates_without_retry():
    """Once a chunk has already been handed to the caller, a transport
    failure must propagate as-is -- retrying here would silently regenerate
    content that may already be on air (the orchestrator's job, per
    design.md §7, not this service's)."""
    fake = _RecordingStreamCloud(
        [
            {"message": {"content": "uno", "thinking": ""}, "done": False},
            ConnectionError("stream broke"),
        ]
    )
    service = LLMInferenceService(cloud_client_module=fake, api_key_resolver=lambda _: "k")
    gen = service.chat_streaming(
        timeout=5.0, is_local=False, provider_cfg=_REASONING_CFG, messages=[], options={}
    )

    next(gen)
    with pytest.raises(ConnectionError):
        next(gen)
    assert len(fake.calls) == 1


def test_stream_sentences_with_splitter():
    mock_stream = [
        {"message": {"content": "Primera frase. "}},
        {"message": {"content": "Segunda "}},
        {"message": {"content": "frase!"}},
    ]
    mock_ollama = MagicMock()
    mock_ollama.chat.return_value = iter(mock_stream)

    service = LLMInferenceService(ollama_client=mock_ollama)
    request = InferenceRequest(
        messages=[{"role": "user", "content": "Hola"}],
        model="qwen3:8b",
        is_local=True,
    )

    sentences = list(service.stream_sentences(request))
    assert len(sentences) >= 2
    assert "Primera frase." in sentences[0]
