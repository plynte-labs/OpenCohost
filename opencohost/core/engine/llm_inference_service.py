"""Pure LLM inference service extracted from ``MotorVocalIA``.

This service encapsulates model dispatching to local Ollama runners and cloud
providers without coupling to audio, speech queues, or UI loops.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
import time
from typing import Any, Callable, Iterator, Optional

try:
    import ollama
except ImportError:
    ollama = None

from opencohost.config.settings import (
    OLLAMA_CHAT_TIMEOUT,
    OLLAMA_REQUEST_TIMEOUT,
)
from opencohost.core.providers.cloud import cloud_llm_client
from opencohost.core.speech.sentence_splitter import SentenceSplitter

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class InferenceRequest:
    """Normalized inference request across local and cloud providers."""

    messages: list[dict[str, Any]]
    model: str
    options: dict[str, Any] = field(default_factory=dict)
    is_local: bool = True
    provider_config: dict[str, Any] = field(default_factory=dict)
    watchdog_timeout: Optional[float] = None
    stream: bool = False


@dataclass
class InferenceResponse:
    """Normalized inference result."""

    text: str
    gen_ms: int = -1
    provider: str = "local"
    is_local: bool = True
    raw: Any = None
    error: Optional[str] = None


class LLMInferenceService:
    """Pure LLM inference runner supporting local Ollama and Cloud providers."""

    def __init__(
        self,
        *,
        ollama_client: Any = None,
        cloud_client_module: Any = None,
    ) -> None:
        self._ollama = ollama if ollama_client is None else ollama_client
        self._cloud_client = (
            cloud_llm_client if cloud_client_module is None else cloud_client_module
        )

    def execute_chat(
        self,
        request: InferenceRequest,
        *,
        on_raw_chunk: Optional[Callable[[Any], None]] = None,
    ) -> InferenceResponse:
        """Execute a synchronous chat completion."""
        start_time = time.monotonic()
        if request.is_local:
            return self._execute_local_chat(request, start_time)
        return self._execute_cloud_chat(request, start_time)

    def _execute_local_chat(
        self, request: InferenceRequest, start_time: float
    ) -> InferenceResponse:
        if self._ollama is None:
            return InferenceResponse(
                text="",
                provider="local",
                is_local=True,
                error="Ollama client is not installed or available",
            )
        try:
            timeout = request.watchdog_timeout or OLLAMA_CHAT_TIMEOUT
            response = self._ollama.chat(
                model=request.model,
                messages=request.messages,
                options=request.options,
                stream=False,
            )
            gen_ms = max(0, int((time.monotonic() - start_time) * 1000))
            content = ""
            if isinstance(response, dict):
                content = response.get("message", {}).get("content", "")
            elif hasattr(response, "message") and hasattr(response.message, "content"):
                content = response.message.content
            return InferenceResponse(
                text=content,
                gen_ms=gen_ms,
                provider="local",
                is_local=True,
                raw=response,
            )
        except Exception as exc:
            gen_ms = max(0, int((time.monotonic() - start_time) * 1000))
            logger.exception("Local Ollama chat inference failed: %s", exc)
            return InferenceResponse(
                text="",
                gen_ms=gen_ms,
                provider="local",
                is_local=True,
                error=str(exc),
            )

    def _execute_cloud_chat(
        self, request: InferenceRequest, start_time: float
    ) -> InferenceResponse:
        provider_name = request.provider_config.get("active_provider") or "cloud"
        try:
            timeout = request.watchdog_timeout or OLLAMA_REQUEST_TIMEOUT
            response = self._cloud_client.send_chat_completion(
                messages=request.messages,
                provider_cfg=request.provider_config,
                timeout=timeout,
            )
            gen_ms = max(0, int((time.monotonic() - start_time) * 1000))
            content = ""
            if isinstance(response, dict):
                content = response.get("text", "")
            return InferenceResponse(
                text=content,
                gen_ms=gen_ms,
                provider=provider_name,
                is_local=False,
                raw=response,
            )
        except Exception as exc:
            gen_ms = max(0, int((time.monotonic() - start_time) * 1000))
            logger.exception("Cloud chat inference failed: %s", exc)
            return InferenceResponse(
                text="",
                gen_ms=gen_ms,
                provider=provider_name,
                is_local=False,
                error=str(exc),
            )

    def stream_tokens(
        self,
        request: InferenceRequest,
    ) -> Iterator[str]:
        """Stream raw tokens from local or cloud providers."""
        if request.is_local:
            if self._ollama is None:
                return
            response_stream = self._ollama.chat(
                model=request.model,
                messages=request.messages,
                options=request.options,
                stream=True,
            )
            for chunk in response_stream:
                token = ""
                if isinstance(chunk, dict):
                    token = chunk.get("message", {}).get("content", "")
                elif hasattr(chunk, "message") and hasattr(chunk.message, "content"):
                    token = chunk.message.content
                if token:
                    yield token
        else:
            # Cloud streaming delegation
            timeout = request.watchdog_timeout or OLLAMA_REQUEST_TIMEOUT
            response = self._cloud_client.send_chat_completion(
                messages=request.messages,
                provider_cfg=request.provider_config,
                timeout=timeout,
            )
            if isinstance(response, dict):
                yield response.get("text", "")

    def stream_sentences(
        self,
        request: InferenceRequest,
        splitter: Optional[SentenceSplitter] = None,
    ) -> Iterator[str]:
        """Stream complete sentence clauses using SentenceSplitter."""
        sent_splitter = splitter or SentenceSplitter()
        for token in self.stream_tokens(request):
            for sentence in sent_splitter.feed(token):
                yield sentence
        final_sentence = sent_splitter.flush()
        if final_sentence:
            yield final_sentence
