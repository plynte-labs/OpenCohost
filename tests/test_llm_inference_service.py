"""Tests for the isolated ``LLMInferenceService``."""

from __future__ import annotations

from unittest.mock import MagicMock
import pytest

from opencohost.core.engine.llm_inference_service import (
    InferenceRequest,
    InferenceResponse,
    LLMInferenceService,
)
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


def test_watchdog_refuses_stream():
    service = LLMInferenceService()
    with pytest.raises(ValueError, match="cannot supervise stream=True"):
        service.call_with_watchdog(lambda: None, timeout=5.0, stream=True)


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
