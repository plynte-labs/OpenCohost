"""Local ``think`` transport and thinking-only vision turns (K1, 2026-10-06).

Live session 2026-10-05 (``logs/ollama_startup_stderr.log`` 18:46:38, 18:47:32):
Ollama logged ``invalid option provided option=think`` on every turn. The
orchestrator put ``think`` inside ``options``; the Ollama client takes it as a
top-level ``chat(think=...)`` argument, so the server dropped it and gemma4
reasoned on every turn even with reasoning off. Vision turns, capped at
``VISION_REPLY_MAX_TOKENS``, then spent the whole budget thinking and ended in
``reasoning_budget_exhausted`` under a custom preset.
"""

import logging
import queue
import threading
import time
from unittest.mock import MagicMock

import pytest

from opencohost.core import llm_engine
from opencohost.core.context.generation_evaluator import GenerationEvaluator
from opencohost.core.context.prompt_assembler import (
    VISION_REPLY_MAX_TOKENS,
    GenerationSetup,
)
from opencohost.core.engine import llm_inference_service as service_mod
from opencohost.core.engine.generation_orchestrator import GenerationOrchestrator
from opencohost.core.engine.llm_inference_service import LLMInferenceService


# ------------------------------------------------------------ fake clients


class _Msg:
    def __init__(self, content="", thinking=""):
        self.content = content
        self.thinking = thinking


class _Chunk:
    def __init__(self, content="", done=False, **stats):
        self.message = _Msg(content)
        self.done = done
        for key, value in stats.items():
            setattr(self, key, value)

    def get(self, key, default=None):
        return getattr(self, key, default)


class _ThinkAwareClient:
    """Mirrors the ollama 0.6.x ``Client.chat`` signature."""

    def __init__(self, stream_chunks=None):
        self.calls = []
        self._stream_chunks = stream_chunks or [_Chunk("Hola.", done=True, done_reason="stop")]

    def chat(
        self,
        model="",
        messages=None,
        *,
        tools=None,
        stream=False,
        think=None,
        format=None,
        options=None,
        keep_alive=None,
    ):
        self.calls.append(
            {"model": model, "stream": stream, "think": think, "options": dict(options or {})}
        )
        if stream:
            return iter(list(self._stream_chunks))
        return {"message": {"content": "Hola.", "thinking": ""}, "done_reason": "stop"}


class _LegacyClient:
    """A pre-0.5 style client: ``chat`` has no ``think`` parameter."""

    def __init__(self):
        self.calls = []

    def chat(self, model="", messages=None, *, stream=False, format=None, options=None, keep_alive=None):
        self.calls.append({"stream": stream, "options": dict(options or {})})
        if stream:
            return iter([_Chunk("Hola.", done=True)])
        return {"message": {"content": "Hola."}}


class _FakeOllamaModule:
    """Stands in for the ``ollama`` module: ``Client(timeout=...)`` and ``chat``."""

    def __init__(self, client):
        self._client = client

    def Client(self, timeout=None):  # noqa: N802 - mirrors the module API
        return self._client

    def chat(self, *args, **kwargs):
        return self._client.chat(*args, **kwargs)


# ------------------------------------------------- service: local transport


@pytest.mark.parametrize("think", [False, True])
def test_local_buffered_chat_sends_think_as_top_level_kwarg(think):
    client = _ThinkAwareClient()
    service = LLMInferenceService(ollama_client=client)
    options = {"num_predict": 320, "think": think}

    service.chat(is_local=True, model="gemma4:e2b", messages=[], options=options)

    assert client.calls[0]["think"] is think
    assert "think" not in client.calls[0]["options"]
    assert client.calls[0]["options"]["num_predict"] == 320
    # The caller's dict is reused across attempts: never mutate it.
    assert options == {"num_predict": 320, "think": think}


def test_local_streaming_chat_sends_think_as_top_level_kwarg():
    client = _ThinkAwareClient()
    service = LLMInferenceService(ollama_client=client)

    list(
        service.chat_streaming(
            timeout=30.0,
            chat_client=client,
            model="gemma4:e2b",
            messages=[],
            options={"num_predict": 320, "think": False},
        )
    )

    assert client.calls[0]["stream"] is True
    assert client.calls[0]["think"] is False
    assert "think" not in client.calls[0]["options"]


def test_local_chat_without_think_leaves_the_request_unchanged():
    client = _ThinkAwareClient()
    service = LLMInferenceService(ollama_client=client)

    service.chat(is_local=True, model="llama3", messages=[], options={"num_predict": 64})

    assert client.calls[0]["think"] is None
    assert client.calls[0]["options"] == {"num_predict": 64}


def test_client_without_think_parameter_degrades_and_logs_once(caplog, monkeypatch):
    monkeypatch.setattr(service_mod, "_THINK_UNSUPPORTED_WARNED", False)
    client = _LegacyClient()
    service = LLMInferenceService(ollama_client=client)

    with caplog.at_level(logging.WARNING, logger="OpenCohost"):
        service.chat(is_local=True, model="m", messages=[], options={"think": False})
        list(
            service.chat_streaming(
                timeout=30.0, chat_client=client, model="m", messages=[], options={"think": False}
            )
        )

    assert len(client.calls) == 2
    assert all("think" not in call["options"] for call in client.calls)
    warnings = [r for r in caplog.records if "think" in r.getMessage() and r.levelno == logging.WARNING]
    assert len(warnings) == 1


def test_cloud_chat_keeps_think_inside_options():
    """The cloud client maps ``options["think"]`` itself; it stays untouched."""
    sent = {}

    class _Cloud:
        @staticmethod
        def send_chat_completion(**kwargs):
            sent.update(kwargs)
            return {"message": {"content": "ok"}}

    service = LLMInferenceService(cloud_client_module=_Cloud, api_key_resolver=lambda pid: "k")
    cfg = {"active_provider": "p", "profiles": {"p": {"base_url": "http://x", "model": "m"}}}

    service.chat(is_local=False, provider_cfg=cfg, messages=[], options={"think": True})

    assert sent["options"]["think"] is True


# ------------------------------------------------ engine: end to end, local


@pytest.fixture
def motor(monkeypatch):
    m = llm_engine.MotorVocalIA(queue.Queue(), lambda event: None)
    m.current_model = "gemma4:e2b"
    m.use_system_role = True
    m._speech_router_enabled = True
    m._resolve_reasoning_settings = lambda model: {"enabled": False, "budget_tokens": None}
    return m


def test_engine_buffered_turn_reaches_ollama_with_top_level_think(motor, monkeypatch):
    monkeypatch.setattr(llm_engine, "LLM_STREAMING_ENABLED", False)
    client = _ThinkAwareClient()
    motor.ollama = _FakeOllamaModule(client)
    motor._ollama_chat_clients = {}
    motor._ollama_chat_client = client
    motor._inference_service = None
    motor._resolve_reasoning_classification = lambda model: True

    motor._generar_dialogo("hola", source="direct", commit_history=True)

    assert client.calls, "the local client was never called"
    assert client.calls[0]["think"] is False
    assert "think" not in client.calls[0]["options"]


def test_engine_streamed_turn_reaches_ollama_with_top_level_think(motor, monkeypatch):
    monkeypatch.setattr(llm_engine, "LLM_STREAMING_ENABLED", True)
    client = _ThinkAwareClient()
    motor.ollama = _FakeOllamaModule(client)
    motor._ollama_chat_clients = {}
    motor._inference_service = None
    motor._resolve_reasoning_classification = lambda model: True

    class _Router:
        def submit_streaming(self, source, priority):
            return object()

        def append_chunks(self, job, chunks):
            return True

        def seal(self, job):
            pass

    motor._ensure_router = lambda: _Router()

    motor._generar_dialogo("hola", source="direct", commit_history=True)

    streamed = [c for c in client.calls if c["stream"]]
    assert streamed, "the turn never streamed"
    assert streamed[0]["think"] is False
    assert "think" not in streamed[0]["options"]


# --------------------------------------- orchestrator: thinking-only vision


class _Resp:
    def __init__(self, content="", thinking="", prompt_eval_count=100):
        self._message = {"content": content, "thinking": thinking}
        self.prompt_eval_count = prompt_eval_count

    def get(self, key, default=None):
        if key == "message":
            return self._message
        if key == "prompt_eval_count":
            return self.prompt_eval_count
        return default


class _Host:
    def __init__(self):
        self.current_model = "gemma4:e2b"
        self._lock = threading.Lock()
        self._llm_generating = False
        self._logs = []
        self._reasoning_model_cache = {}
        self._speech_router_enabled = False

    def _log(self, msg, level="info"):
        self._logs.append((level, msg))

    def _is_watchdog_timeout_error(self, e):
        return False

    def _is_ollama_transport_error(self, e):
        return False


def _vision_setup(*, preset="custom", think=True, vision_turn=True, num_predict=VISION_REPLY_MAX_TOKENS):
    return GenerationSetup(
        messages=[{"role": "user", "content": "mira mi pantalla"}],
        opciones_llm={"num_predict": num_predict},
        chat_timeout=30.0,
        max_intentos=2,
        start_llm=time.time(),
        native_ctx=131072,
        effective_ctx=8192,
        ctx_evicted=0,
        editorial_block="",
        history_snapshot=[],
        think=think,
        preset=preset,
        vision_turn=vision_turn,
    )


def _run(setup, responses):
    host = _Host()
    calls = []

    def _chat(*, options, **kwargs):
        calls.append(dict(options))
        return responses.pop(0)

    host._ollama_chat_with_watchdog = _chat
    outcome = GenerationOrchestrator(host).execute_generation_attempt(
        setup,
        source="direct",
        commit_history=True,
        is_local=True,
        provider_cfg={},
        request_model="gemma4:e2b",
        watchdog_timeout=30.0,
    )
    return outcome, calls, host


@pytest.mark.parametrize("preset", ["custom", "balanced"])
def test_thinking_only_vision_turn_retries_once_with_think_off(preset):
    setup = _vision_setup(preset=preset, think=True)
    outcome, calls, host = _run(
        setup, [_Resp(content="", thinking="hmm " * 200), _Resp(content="Veo tu editor.")]
    )

    assert len(calls) == 2
    assert calls[0]["think"] is True
    assert calls[1]["think"] is False
    assert VISION_REPLY_MAX_TOKENS < calls[1]["num_predict"] <= VISION_REPLY_MAX_TOKENS * 2
    assert outcome.raw_content == "Veo tu editor."
    assert outcome.failure_reason is None
    assert any("visión" in msg or "vision" in msg for _, msg in host._logs)


def test_thinking_only_vision_turn_retries_only_once():
    setup = _vision_setup(preset="custom", think=None)
    outcome, calls, _ = _run(
        setup, [_Resp(content="", thinking="a"), _Resp(content="", thinking="b")]
    )

    assert len(calls) == 2
    assert outcome.raw_content == ""
    assert outcome.failure_reason == "reasoning_budget_exhausted"


def test_thinking_only_text_turn_under_custom_preset_is_unchanged():
    setup = _vision_setup(preset="custom", think=False, vision_turn=False, num_predict=500)
    outcome, calls, _ = _run(setup, [_Resp(content="", thinking="a")])

    assert len(calls) == 1
    assert outcome.failure_reason == "reasoning_budget_exhausted"
    assert setup.opciones_llm["num_predict"] == 500


def test_assembler_marks_vision_turns():
    from opencohost.core.context.prompt_assembler import PromptContextAssembler

    common = dict(
        system_prompt="Eres Kira.",
        use_system_role=True,
        is_local=True,
        provider_cfg={},
        request_model="llama3",
        history_snapshot=[],
    )
    plain = PromptContextAssembler().assemble("hola", "direct", **common)
    looked = PromptContextAssembler().assemble(
        "hola", "direct", screen_block="[SCREEN] un editor [/SCREEN]", **common
    )

    assert plain.vision_turn is False
    assert looked.vision_turn is True


# ------------------------------------------------------ num_predict in logs


def test_model_trace_logs_the_computed_num_predict():
    setup = _vision_setup(vision_turn=False, num_predict=777)
    res = GenerationEvaluator().evaluate(
        "Hola.",
        {"message": {"content": "Hola."}},
        setup,
        source="direct",
        is_local=True,
        provider_cfg={},
        request_model="llama3",
        desired_model="llama3",
        current_model="llama3",
        loaded_model="llama3",
        current_profile_name="default",
    )

    assert "num_predict=777" in res.model_trace.trace_msg


# ------------------------------- think-incapable models (regression of K1)
#
# Live log 2026-10-06 (opencohost_20261006_222407.log): once ``think`` became a
# top-level kwarg, Ollama answered 400 '"<model>" does not support thinking'
# for models without the capability, killing EVERY turn. Before K1 the option
# was silently dropped. The local transport retries once without ``think`` and
# remembers the model so later calls omit it up front.

try:
    from ollama import ResponseError as _OllamaResponseError
except ImportError:  # pragma: no cover - ollama is a runtime dependency
    class _OllamaResponseError(Exception):
        def __init__(self, error, status_code=-1):
            super().__init__(error)
            self.error = error
            self.status_code = status_code


def _think_400(model):
    return _OllamaResponseError(f'"{model}" does not support thinking', 400)


class _NoThinkingModelClient(_ThinkAwareClient):
    """Rejects any request that carries ``think`` like Ollama does for a
    model without the thinking capability. Streaming raises lazily, on the
    first iteration, exactly as ollama-python's generator does."""

    def __init__(self, stream_chunks=None, error=None):
        super().__init__(stream_chunks)
        self._error = error

    def chat(self, model="", messages=None, *, tools=None, stream=False, think=None,
             format=None, options=None, keep_alive=None):
        self.calls.append(
            {"model": model, "stream": stream, "think": think, "options": dict(options or {})}
        )
        if think is not None:
            error = self._error or _think_400(model)
            if stream:
                def _lazy():
                    raise error
                    yield  # pragma: no cover
                return _lazy()
            raise error
        if stream:
            return iter(list(self._stream_chunks))
        return {"message": {"content": "Hola.", "thinking": ""}, "done_reason": "stop"}


@pytest.fixture
def fresh_think_cache(monkeypatch):
    monkeypatch.setattr(service_mod, "_THINK_UNSUPPORTED_MODELS", set())


def test_buffered_think_unsupported_retries_once_without_think_and_remembers(
    fresh_think_cache, caplog
):
    client = _NoThinkingModelClient()
    service = LLMInferenceService(ollama_client=client)
    model = "hf.co/unsloth/gemma-4-E4B-it-GGUF:IQ4_NL"

    with caplog.at_level(logging.WARNING, logger="OpenCohost"):
        reply = service.chat(is_local=True, model=model, messages=[], options={"think": False})

    assert reply["message"]["content"] == "Hola."
    assert [c["think"] for c in client.calls] == [False, None]
    warnings = [r.getMessage() for r in caplog.records if "[THINK_UNSUPPORTED]" in r.getMessage()]
    assert warnings == [f"[THINK_UNSUPPORTED] model={model}"]

    # A later call for the same model omits think up front: one round-trip.
    client.calls.clear()
    service.chat(is_local=True, model=model, messages=[], options={"think": True})
    assert [c["think"] for c in client.calls] == [None]


def test_streaming_think_unsupported_on_first_iteration_retries_without_think(fresh_think_cache):
    client = _NoThinkingModelClient()
    service = LLMInferenceService(ollama_client=client)

    chunks = list(
        service.chat_streaming(
            timeout=30.0, chat_client=client, model="m", messages=[], options={"think": False}
        )
    )

    assert [c.message.content for c in chunks] == ["Hola."]
    assert [(c["stream"], c["think"]) for c in client.calls] == [(True, False), (True, None)]

    client.calls.clear()
    list(
        service.chat_streaming(
            timeout=30.0, chat_client=client, model="m", messages=[], options={"think": False}
        )
    )
    assert [c["think"] for c in client.calls] == [None]


def test_streaming_think_error_after_a_chunk_was_yielded_is_not_retried(fresh_think_cache):
    error = _think_400("m")

    class _MidStreamClient(_ThinkAwareClient):
        def chat(self, model="", messages=None, *, stream=False, think=None, options=None, **_kw):
            self.calls.append({"think": think})

            def _gen():
                yield _Chunk("Ho")
                raise error
            return _gen()

    client = _MidStreamClient()
    service = LLMInferenceService(ollama_client=client)
    got = []
    with pytest.raises(_OllamaResponseError):
        for chunk in service.chat_streaming(
            timeout=30.0, chat_client=client, model="m", messages=[], options={"think": False}
        ):
            got.append(chunk)

    assert len(got) == 1
    assert len(client.calls) == 1
    assert "m" not in service_mod._THINK_UNSUPPORTED_MODELS


@pytest.mark.parametrize("stream", [False, True])
def test_other_400_errors_still_propagate(fresh_think_cache, stream):
    client = _NoThinkingModelClient(error=_OllamaResponseError("invalid model format", 400))
    service = LLMInferenceService(ollama_client=client)

    with pytest.raises(_OllamaResponseError):
        if stream:
            list(service.chat_streaming(
                timeout=30.0, chat_client=client, model="m", messages=[], options={"think": False}
            ))
        else:
            service.chat(is_local=True, model="m", messages=[], options={"think": False})

    assert len(client.calls) == 1
    assert service_mod._THINK_UNSUPPORTED_MODELS == set()


def test_thinking_capable_model_keeps_receiving_think(fresh_think_cache):
    bad = _NoThinkingModelClient()
    LLMInferenceService(ollama_client=bad).chat(
        is_local=True, model="no-think", messages=[], options={"think": False}
    )
    good = _ThinkAwareClient()
    service = LLMInferenceService(ollama_client=good)

    service.chat(is_local=True, model="gemma4:e2b", messages=[], options={"think": False})
    list(service.chat_streaming(
        timeout=30.0, chat_client=good, model="gemma4:e2b", messages=[], options={"think": True}
    ))

    assert [c["think"] for c in good.calls] == [False, True]


def test_promotion_judge_transport_survives_a_think_incapable_model(fresh_think_cache):
    """The memoria judge passes ``think`` straight to its client; a model
    without the capability must not kill every promotion sweep."""
    from opencohost.core.engine.llm_engine_models import ModelManagementMixin

    client = _NoThinkingModelClient()
    host = MagicMock()
    host._ollama_judge_client = client

    reply = ModelManagementMixin._ollama_judge_chat(
        host, model="m", messages=[], think=False, options={"temperature": 0}
    )

    assert reply["message"]["content"] == "Hola."
    assert [c["think"] for c in client.calls] == [False, None]
