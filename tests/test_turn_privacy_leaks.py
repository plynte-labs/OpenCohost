"""Provider and exception text never reaches failure state or logs (turn-events T1).

An exception message can quote the request (chat text) or the provider's reply,
so only the exception class name and fixed codes may be recorded.
"""

from __future__ import annotations

import logging
from unittest.mock import patch

from tests.test_llm_is_local_gating import _SEND, _cloud_config, _make_motor
from tests.test_streaming_adverse_path_fake_ollama import (  # noqa: F401  (fixtures)
    _run_bounded,
    engine,
    fake_ollama,
)

SENTINEL = "SENTINEL-chat-text-7f3a"


def _all_text(motor, caplog) -> str:
    queued = " ".join(str(item) for item in list(motor.log_queue.queue))
    return caplog.text + " " + queued


def test_local_transport_failure_keeps_exception_text_out_of_state_and_logs(
    engine, fake_ollama, caplog, monkeypatch
):
    def boom(*_a, **_k):
        raise ConnectionError(SENTINEL)

    monkeypatch.setattr(engine._generation_orchestrator, "run_streaming_attempt", boom)

    with caplog.at_level(logging.DEBUG, logger="OpenCohost"):
        engine.enqueue("hola", priority=0, source="direct")
        _run_bounded(engine._process_priority_queue)

    failure = engine._last_llm_failure
    assert failure is not None and failure["reason"] == "ConnectionError"
    assert SENTINEL not in repr(failure)
    assert SENTINEL not in _all_text(engine, caplog)
    assert "ERROR Ollama chat (ConnectionError)" in _all_text(engine, caplog)


def test_catch_all_logs_class_and_code_not_the_message(engine, fake_ollama, caplog, monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError(SENTINEL)

    monkeypatch.setattr(engine, "_build_generation_request", boom)

    with caplog.at_level(logging.DEBUG, logger="OpenCohost"):
        engine.enqueue("hola", priority=0, source="direct")
        _run_bounded(engine._process_priority_queue)

    text = _all_text(engine, caplog)
    assert SENTINEL not in text
    assert "ERROR Ollama: RuntimeError" in text


def test_cloud_transport_failure_keeps_provider_text_out_of_state_and_logs(tmp_path, caplog):
    from opencohost.core.providers.cloud.cloud_llm_client import CloudLLMResponseError

    motor, _, _ = _make_motor(tmp_path, provider_config=_cloud_config())
    motor._handle_cloud_failure = lambda *a, **k: None
    with caplog.at_level(logging.DEBUG, logger="OpenCohost"):
        with patch(_SEND, side_effect=CloudLLMResponseError(f"bad request {SENTINEL}")):
            motor._generar_dialogo("hola", source="direct", commit_history=False)

    failure = motor._last_llm_failure
    assert failure is not None and failure["reason"] == "CloudLLMResponseError"
    assert failure["model"] == "gpt-cloud" and failure["provider"] == "openai"
    assert SENTINEL not in repr(failure)
    assert SENTINEL not in _all_text(motor, caplog)
