"""Tests for the isolated ``LLMInferenceService``."""

from __future__ import annotations

from types import SimpleNamespace
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
        stream=False,
    )


def test_cloud_chat_execution_success():
    fake_response = {"text": "Hola mundo cloud"}
    mock_cloud = MagicMock()
    mock_cloud.send_chat_completion.return_value = fake_response

    service = LLMInferenceService(cloud_client_module=mock_cloud)
    request = InferenceRequest(
        messages=[{"role": "user", "content": "Hola"}],
        model="deepseek-v3",
        is_local=False,
        provider_config={"active_provider": "deepseek"},
    )

    result = service.execute_chat(request)
    assert isinstance(result, InferenceResponse)
    assert result.text == "Hola mundo cloud"
    assert result.provider == "deepseek"
    assert result.is_local is False
    assert result.gen_ms >= 0
    assert result.error is None
    mock_cloud.send_chat_completion.assert_called_once()


def test_local_chat_error_handling():
    mock_ollama = MagicMock()
    mock_ollama.chat.side_effect = RuntimeError("Ollama connection refused")

    service = LLMInferenceService(ollama_client=mock_ollama)
    request = InferenceRequest(
        messages=[{"role": "user", "content": "Hola"}],
        model="qwen3:8b",
        is_local=True,
    )

    result = service.execute_chat(request)
    assert result.text == ""
    assert result.error == "Ollama connection refused"
    assert result.provider == "local"


def test_stream_tokens_local():
    chunks = [
        {"message": {"content": "Hola "}},
        {"message": {"content": "amigos "}},
        {"message": {"content": "mios."}},
    ]
    mock_ollama = MagicMock()
    mock_ollama.chat.return_value = iter(chunks)

    service = LLMInferenceService(ollama_client=mock_ollama)
    request = InferenceRequest(
        messages=[{"role": "user", "content": "Hola"}],
        model="qwen3:8b",
        is_local=True,
    )

    tokens = list(service.stream_tokens(request))
    assert tokens == ["Hola ", "amigos ", "mios."]


def test_stream_sentences_with_splitter():
    chunks = [
        {"message": {"content": "Primera frase. "}},
        {"message": {"content": "Segunda "}},
        {"message": {"content": "frase!"}},
    ]
    mock_ollama = MagicMock()
    mock_ollama.chat.return_value = iter(chunks)

    service = LLMInferenceService(ollama_client=mock_ollama)
    request = InferenceRequest(
        messages=[{"role": "user", "content": "Hola"}],
        model="qwen3:8b",
        is_local=True,
    )

    sentences = list(service.stream_sentences(request))
    assert len(sentences) >= 2
    assert "Primera frase." in sentences[0]
