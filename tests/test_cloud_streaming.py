"""Cloud LLM output streaming (live_session_fixes_20260925 T8).

Cloud (OpenAI-compatible / NVIDIA NIM) direct/ptt/owner-bundle turns stream
sentence-by-sentence to TTS through the SAME seam local streaming already
uses (`GenerationOrchestrator.run_streaming_attempt`, the sentence splitter,
the per-sentence guard, rollback/abort) -- see
the 2026-09-25 live-session findings (T8) and the
LLM output streaming design, section 7 (the "one-line
gate" for a cloud attempt that already spoke).

Covers the orchestrator-level integration: eligibility, a clean end-to-end
cloud streaming turn through a fake client, a pre-first-chunk cloud failure
falling back to a same-call local retry with correct provider attribution,
and the "no job created" gate that refuses to retry-regenerate a cloud
attempt that already spoke on air.
"""

import logging
import queue
import socket
from unittest.mock import MagicMock

import pytest

from opencohost.core import llm_engine
from opencohost.core.providers.cloud import cloud_llm_client


class FakeMessage:
    def __init__(self, content="", thinking=None):
        self.content = content
        self.thinking = thinking


class FakeChunk:
    """Attr-shaped like ollama's ChatResponse chunks -- used for the LOCAL
    retry leg of a fallback scenario, matching test_llm_streaming_loop.py."""

    def __init__(self, content="", done=False, **stats):
        self.message = FakeMessage(content)
        self.done = done
        for key, value in stats.items():
            setattr(self, key, value)

    def get(self, key, default=None):
        return getattr(self, key, default)


def _cloud_chunk(content="", thinking="", done=False, usage=None):
    """Dict-shaped like cloud_llm_client.stream_chat_completion's yielded chunks."""
    if done:
        return {"done": True, "usage": usage or {}}
    return {"message": {"content": content, "thinking": thinking}, "done": False}


class FakeJob:
    def __init__(self, job_id):
        self.job_id = job_id
        self.sealed = False


class FakeRouter:
    def __init__(self, append_results=None):
        self.jobs = []
        self.appends = []
        self.sealed = []
        self._append_results = list(append_results or [])

    def submit_streaming(self, source, priority):
        job = FakeJob(len(self.jobs) + 1)
        self.jobs.append((job, source, priority))
        return job

    def append_chunks(self, job, chunks):
        self.appends.append((job, list(chunks)))
        if self._append_results:
            return self._append_results.pop(0)
        return True

    def seal(self, job):
        job.sealed = True
        self.sealed.append(job)


_CLOUD_PROFILE = {
    "active_provider": "nvidia_nim",
    "fallback_mode": "auto",
    "profiles": {
        "nvidia_nim": {
            "base_url": "https://integrate.api.nvidia.com/v1",
            "model": "nemotron-3.5-lightning",
        }
    },
}


@pytest.fixture
def cloud_motor(monkeypatch):
    m = llm_engine.MotorVocalIA(queue.Queue(), lambda event: None)
    m.current_model = "llama3"
    m._last_known_good_model = "llama3"
    m.use_system_role = True
    m.ollama = MagicMock()
    m._speech_router_enabled = True
    # Hermetic local retry leg: seed the caches the buffered/streaming local
    # path would otherwise hit a live probe for (mirrors
    # test_cloud_failure_retry.py's _cloud_motor helper).
    m._reasoning_model_cache["llama3"] = False
    m._model_ctx_limit = {"llama3": 8192}
    m._provider_config = dict(_CLOUD_PROFILE)
    monkeypatch.setattr(llm_engine, "LLM_STREAMING_ENABLED", True)
    monkeypatch.setattr(llm_engine, "CLOUD_STREAMING_ENABLED", True)
    router = FakeRouter()
    m._ensure_router = lambda: router
    m._speech_router = router
    m._fake_router = router
    return m


def _arm_dispatch(motor, *, cloud_chunks=None, cloud_error=None, local_chunks=None):
    """Fake `_llm_chat_streaming` that branches on `is_local`, mirroring
    `llm_engine_models.py::_llm_chat_streaming`'s real dispatch contract --
    always returns a real generator (so `contextlib.closing` in
    run_streaming_attempt has a `.close()` to call)."""
    record = {"cloud_calls": 0, "local_calls": 0, "attempts": []}

    def _dispatch(*, is_local, provider_cfg=None, timeout, model, messages, options):
        record["attempts"].append(
            {"is_local": is_local, "model": model, "messages": messages}
        )
        if not is_local:
            record["cloud_calls"] += 1

            def _cloud_gen():
                if cloud_error is not None:
                    raise cloud_error
                for item in cloud_chunks or []:
                    if isinstance(item, BaseException):
                        raise item
                    yield item

            return _cloud_gen()

        record["local_calls"] += 1

        def _local_gen():
            for item in local_chunks or []:
                if isinstance(item, BaseException):
                    raise item
                yield item

        return _local_gen()

    motor._llm_chat_streaming = _dispatch
    return record


def _deny_network_connections(monkeypatch):
    def deny(*_args, **_kwargs):
        raise AssertionError("T9 model identity tests must use fake transports only")

    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket.socket, "connect_ex", deny)
    monkeypatch.setattr(socket, "create_connection", deny)


# ---------------------------------------------------------------- eligibility


def test_cloud_turn_is_stream_eligible_by_default(cloud_motor):
    record = _arm_dispatch(
        cloud_motor,
        cloud_chunks=[
            _cloud_chunk("Respuesta desde la nube."),
            _cloud_chunk(done=True, usage={"completion_tokens": 4}),
        ],
    )

    dialogo = cloud_motor._generar_dialogo("hola", source="direct", commit_history=True)

    assert dialogo == "Respuesta desde la nube."
    assert record["cloud_calls"] == 1
    assert record["local_calls"] == 0
    assert len(cloud_motor._fake_router.jobs) == 1


# ------------------------------------------------------------------- success


def test_cloud_streaming_turn_speaks_sentences_in_order_with_per_sentence_guard(
    cloud_motor, caplog
):
    _arm_dispatch(
        cloud_motor,
        cloud_chunks=[
            _cloud_chunk("Primera frase limpia. "),
            _cloud_chunk("Segunda frase tranquila."),
            _cloud_chunk(done=True, usage={"completion_tokens": 12, "total_tokens": 40}),
        ],
    )

    with caplog.at_level(logging.INFO, logger="OpenCohost"):
        dialogo = cloud_motor._generar_dialogo("hola", source="direct", commit_history=True)

    assert dialogo == "Primera frase limpia. Segunda frase tranquila."
    router = cloud_motor._fake_router
    assert len(router.jobs) == 1
    job, source, priority = router.jobs[0]
    assert source == "direct"
    assert priority == 0
    assert router.appends == [
        (job, ["Primera frase limpia."]),
        (job, ["Segunda frase tranquila."]),
    ]
    assert router.sealed == [job]
    ttfa = [r for r in caplog.records if "[STREAM_TTFA]" in r.getMessage()]
    assert len(ttfa) == 1
    usage_lines = [r for r in caplog.records if "cloud_llm_usage" in r.getMessage()]
    assert len(usage_lines) == 1
    assert "completion_tokens" in usage_lines[0].getMessage()


def test_streamed_cloud_payload_and_owner_identity_use_active_profile_model(
    cloud_motor, monkeypatch
):
    _deny_network_connections(monkeypatch)
    local_model = "gemma4:e4b"
    cloud_model = "nvidia/nemotron-3.5-lightning-30b-a3b"
    fake_api_key = "FAKE_T9_STREAM_API_KEY_MUST_NOT_ENTER_PROMPT"
    cloud_motor.current_model = local_model
    cloud_motor._provider_config["profiles"]["nvidia_nim"]["model"] = cloud_model
    from types import SimpleNamespace
    from opencohost.core.self_knowledge.providers import ModelProvider
    from opencohost.core.self_knowledge.service import SelfKnowledgeService

    cloud_motor.self_knowledge_service = SelfKnowledgeService(
        providers=[ModelProvider()],
        settings_reader=lambda: SimpleNamespace(enabled=True, viewers=False),
        lang_reader=lambda: "en",
    )
    cloud_motor._inference_service._api_key_resolver = lambda _profile_id: fake_api_key
    captured = {}

    def capture_cloud_stream(**kwargs):
        captured.update(kwargs)
        yield _cloud_chunk("Respuesta desde la nube.")
        yield _cloud_chunk(done=True, usage={"completion_tokens": 4})

    monkeypatch.setattr(cloud_llm_client, "stream_chat_completion", capture_cloud_stream)

    dialogo = cloud_motor._generar_dialogo(
        "what model are you", source="direct", commit_history=True
    )

    assert dialogo == "Respuesta desde la nube."
    assert captured["model"] == cloud_model
    prompt = "\n".join(str(message.get("content", "")) for message in captured["messages"])
    assert cloud_model in prompt
    assert local_model not in prompt
    assert fake_api_key not in prompt


def test_cloud_streaming_guard_trip_before_first_submit_reverts_to_buffered(cloud_motor):
    """A guard trip on sentence 1 (before any job exists) must revert to
    buffered semantics for the whole turn, exactly like local streaming."""
    _arm_dispatch(
        cloud_motor,
        cloud_chunks=[
            _cloud_chunk("Como IA no puedo opinar sobre eso. "),
            _cloud_chunk(done=True),
        ],
    )

    dialogo = cloud_motor._generar_dialogo("hola", source="direct", commit_history=True)

    assert cloud_motor._fake_router.jobs == []
    # The canned fallback / retry-nudge machinery decides the exact final
    # text; what matters here is that no audio was ever queued.
    assert isinstance(dialogo, str)


# --------------------------------------------------------- pre-first-chunk failure


def test_pre_first_chunk_cloud_failure_falls_back_to_local_retry_with_attribution(
    cloud_motor, monkeypatch
):
    """design.md §7's cloud->local retry, now reachable for a STREAMING cloud
    attempt: a transport failure before any sentence closes creates no job,
    so the existing same-call local retry still applies, and the reply must
    be attributed to local (T3), not the cloud provider it started under."""
    _deny_network_connections(monkeypatch)
    cloud_motor._handle_cloud_failure = MagicMock(
        side_effect=lambda source, **kw: (
            setattr(cloud_motor, "_cloud_fallback_active", True),
            setattr(
                cloud_motor,
                "_cloud_fallback_reason",
                cloud_llm_client.CLOUD_ERROR_TRANSIENT,
            ),
        )
    )
    record = _arm_dispatch(
        cloud_motor,
        cloud_error=ConnectionError("cloud transport refused"),
        local_chunks=[
            FakeChunk("Respuesta de respaldo local. "),
            FakeChunk("", done=True),
        ],
    )

    result = cloud_motor._generar_dialogo("hola", source="ptt", commit_history=True)

    assert result == "Respuesta de respaldo local."
    assert record["cloud_calls"] == 1
    assert record["local_calls"] == 1
    assert result.answered_by_provider == "local"
    assert result.answered_by_transport == "local"
    assert result.cloud_fallback_retry is True
    cloud_prompt = "\n".join(
        str(message.get("content", "")) for message in record["attempts"][0]["messages"]
    )
    local_prompt = "\n".join(
        str(message.get("content", "")) for message in record["attempts"][1]["messages"]
    )
    assert "nemotron-3.5-lightning" in cloud_prompt
    assert "llama3" not in cloud_prompt
    assert "llama3" in local_prompt
    assert "nemotron-3.5-lightning" not in local_prompt
    # The cloud attempt never created a job (it died before any sentence
    # closed); the local retry streamed and created exactly one.
    router = cloud_motor._fake_router
    assert len(router.jobs) == 1
    _, source, _ = router.jobs[0]
    assert source == "ptt"

    next_turn = cloud_motor._generar_dialogo(
        "what model are you", source="ptt", commit_history=True
    )
    assert next_turn.startswith("Respuesta de respaldo local.")
    assert record["local_calls"] == 2
    next_turn_prompt = "\n".join(
        str(message.get("content", "")) for message in record["attempts"][2]["messages"]
    )
    assert record["attempts"][2]["is_local"] is True
    assert "llama3" in next_turn_prompt
    assert "nemotron-3.5-lightning" not in next_turn_prompt


# -------------------------------------------------- mid-stream: "no job created" gate


def test_mid_stream_cloud_failure_after_speaking_never_retries_the_whole_turn(cloud_motor):
    """design.md §7's explicit forward-spec: once a cloud STREAMING attempt
    has already created a job (spoken a prefix on air), the cloud_fallback_
    retry branch must NOT regenerate-and-speak a second reply on top of it --
    even though `_cloud_fallback_active`/reason would otherwise qualify."""
    cloud_motor._handle_cloud_failure = MagicMock(
        side_effect=lambda source, **kw: (
            setattr(cloud_motor, "_cloud_fallback_active", True),
            setattr(
                cloud_motor,
                "_cloud_fallback_reason",
                cloud_llm_client.CLOUD_ERROR_TRANSIENT,
            ),
        )
    )
    record = _arm_dispatch(
        cloud_motor,
        cloud_chunks=[
            _cloud_chunk("Primera frase limpia. "),
            TimeoutError("watchdog_timeout:20.00s"),
        ],
        local_chunks=[FakeChunk("Nunca debe hablarse.", done=True)],
    )

    result = cloud_motor._generar_dialogo("hola", source="direct", commit_history=True)

    # Empty result (the existing non-committing-after-first-submit contract);
    # the cloud attempt's own partial-turn exit already committed/sealed the
    # spoken prefix -- see the streaming loop's own tests for that mechanism.
    assert result == ""
    assert result.cloud_fallback_retry is False
    assert record["cloud_calls"] == 1
    assert record["local_calls"] == 0
    router = cloud_motor._fake_router
    # Exactly the ONE job the cloud attempt itself created before dying --
    # never a second one from a forbidden retry.
    assert len(router.jobs) == 1
    assert router.sealed == [router.jobs[0][0]]
