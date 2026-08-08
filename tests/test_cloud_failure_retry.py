"""F1 (interruptible_speech_architecture_20260804, runtime-findings
2026-08-07): a CLOUD generation failure that engages auto-fallback must not
burn the owner's turn -- it gets exactly one in-turn local retry at the
generation funnel (`_generar_dialogo`) before the existing "" contract
applies. Mocking style mirrors tests/test_llm_engine_timeouts.py.
"""

import queue
from unittest.mock import MagicMock

from opencohost.core import llm_engine
from opencohost.core.providers.cloud import cloud_llm_client


def _cloud_motor(provider: str = "openai", fallback_mode: str = "auto"):
    motor = llm_engine.MotorVocalIA(queue.Queue(), lambda event: None)
    motor._provider_config = {
        "active_provider": provider,
        "fallback_mode": fallback_mode,
        "profiles": {provider: {"base_url": "https://api.example.com/v1", "model": "gpt-cloud"}},
    }
    motor.current_model = "llama3"
    motor._last_known_good_model = "llama3"
    motor.use_system_role = True
    # Hermetic local retry: seed both ollama.show-backed caches so the local
    # retry attempt never reaches the live probe (mirrors
    # test_interruption_connector.py's _bare_motor helper).
    motor._reasoning_model_cache["llama3"] = False
    motor._model_ctx_limit = {"llama3": 8192}
    return motor


def _engage_fallback_stub(motor):
    """Stand in for `_handle_cloud_failure`'s auto-mode side effect (setting
    `_cloud_fallback_active` + `_cloud_fallback_reason`) without its real
    background warm-up thread. Every failure simulated in this file
    (watchdog timeout, connection error, empty 2xx body) is real-code
    transient (llm_engine.py's own transient-only retry gate), so the stub
    always engages with that class -- a non-transient scenario belongs in
    its own test (see test_bad_key_cloud_failure_never_retries below)."""
    def fake(source, **kwargs):
        motor._cloud_fallback_active = True
        motor._cloud_fallback_reason = cloud_llm_client.CLOUD_ERROR_TRANSIENT
    return MagicMock(side_effect=fake)


def test_cloud_watchdog_timeout_retries_once_locally_and_answers():
    motor = _cloud_motor()
    motor._handle_cloud_failure = _engage_fallback_stub(motor)
    calls = []

    def fake_watchdog(*, is_local, **kwargs):
        calls.append(is_local)
        if not is_local:
            raise TimeoutError("watchdog_timeout:75.00s")
        return {"message": {"content": "Respuesta de respaldo local."}}

    motor._ollama_chat_with_watchdog = MagicMock(side_effect=fake_watchdog)

    result = motor._generar_dialogo("hola", source="ptt", commit_history=False)

    assert result == "Respuesta de respaldo local."
    assert calls == [False, True]
    motor._handle_cloud_failure.assert_called_once()


def test_cloud_transport_error_retries_once_locally_and_answers():
    motor = _cloud_motor()
    motor._handle_cloud_failure = _engage_fallback_stub(motor)
    calls = []

    def fake_watchdog(*, is_local, **kwargs):
        calls.append(is_local)
        if not is_local:
            raise ConnectionError("cloud transport refused")
        return {"message": {"content": "Respuesta de respaldo local."}}

    motor._ollama_chat_with_watchdog = MagicMock(side_effect=fake_watchdog)

    result = motor._generar_dialogo("hola", source="ptt", commit_history=False)

    assert result == "Respuesta de respaldo local."
    assert calls == [False, True]
    motor._handle_cloud_failure.assert_called_once()


def test_cloud_empty_body_retries_once_locally_and_answers(monkeypatch):
    monkeypatch.setattr(llm_engine.time, "sleep", lambda *_a, **_k: None)
    motor = _cloud_motor()
    motor._handle_cloud_failure = _engage_fallback_stub(motor)
    calls = []

    def fake_watchdog(*, is_local, **kwargs):
        calls.append(is_local)
        if not is_local:
            # Well-formed 2xx, empty content -- exit 3 (llm_engine.py ~4813-
            # 4824), reached only after max_intentos exhausts with no content.
            return {"message": {"content": "", "thinking": ""}}
        return {"message": {"content": "Respuesta de respaldo local."}}

    motor._ollama_chat_with_watchdog = MagicMock(side_effect=fake_watchdog)

    result = motor._generar_dialogo("hola", source="ptt", commit_history=False)

    assert result == "Respuesta de respaldo local."
    # Two exhausted cloud attempts (max_intentos), then one local retry.
    assert calls == [False, False, True]
    motor._handle_cloud_failure.assert_called_once()


def test_cloud_failure_local_retry_also_fails_preserves_empty_contract():
    motor = _cloud_motor()
    motor._handle_cloud_failure = _engage_fallback_stub(motor)
    calls = []

    def fake_watchdog(*, is_local, **kwargs):
        calls.append(is_local)
        raise TimeoutError("watchdog_timeout:75.00s" if not is_local else "watchdog_timeout:45.00s")

    motor._ollama_chat_with_watchdog = MagicMock(side_effect=fake_watchdog)

    result = motor._generar_dialogo("hola", source="ptt", commit_history=False)

    assert result == ""
    # Exactly one retry -- no infinite loop even though the local attempt
    # also failed.
    assert calls == [False, True]
    motor._handle_cloud_failure.assert_called_once()


def test_bad_key_cloud_failure_never_retries():
    """FIX B boundary (2026-08-08 reviewer finding): the retry gate is
    transient-only. `bad_key` still engages auto-fallback (`_handle_cloud_
    failure` runs for real here, unstubbed) but must NOT get the in-turn
    local rescue -- the turn burns exactly like pre-F1, and the bad_key
    bookkeeping (banner latch, `_last_cloud_failure_class`) survives because
    no local retry ever runs to clear it via a successful `_finalize_
    generation`. Mirrors tests/test_cloud_rate_limit_retry.py's own
    bad_key test, which pins the same real-`_handle_cloud_failure`,
    unmocked-fallback-thread pattern this file otherwise stubs out."""
    events = []
    motor = llm_engine.MotorVocalIA(queue.Queue(), events.append)
    motor._provider_config = {
        "active_provider": "openai",
        "fallback_mode": "auto",
        "profiles": {"openai": {"base_url": "https://api.example.com/v1", "model": "gpt-cloud"}},
    }
    motor.current_model = "llama3"
    motor._last_known_good_model = "llama3"
    motor.use_system_role = True
    calls = []

    def fake_watchdog(*, is_local, **kwargs):
        calls.append(is_local)
        raise cloud_llm_client.CloudLLMResponseError("HTTP 401", status_code=401)

    motor._ollama_chat_with_watchdog = MagicMock(side_effect=fake_watchdog)

    result = motor._generar_dialogo("hola", source="direct", commit_history=False)

    assert result == ""
    assert calls == [False]  # no local retry attempt
    assert motor._last_cloud_failure_class == "bad_key"
    assert events.count("cloud_bad_key") == 1


def test_manual_fallback_mode_never_retries():
    """FIX C (coverage gap): `fallback_mode="manual"` leaves
    `_cloud_fallback_active` False (llm_engine.py comment ~4139-4143), so the
    retry gate cannot fire -- true by code reading, but every other test in
    this file stubs `_handle_cloud_failure` out, so the manual branch (its
    early `return` before the flag is ever set) never actually executed.
    Real, unstubbed `_handle_cloud_failure` here closes that gap."""
    motor = _cloud_motor(fallback_mode="manual")
    calls = []

    def fake_watchdog(*, is_local, **kwargs):
        calls.append(is_local)
        raise TimeoutError("watchdog_timeout:75.00s")

    motor._ollama_chat_with_watchdog = MagicMock(side_effect=fake_watchdog)

    result = motor._generar_dialogo("hola", source="direct", commit_history=False)

    assert result == ""
    assert calls == [False]
    assert motor._cloud_fallback_active is False


def test_local_failure_does_not_retry_burns_turn():
    """A LOCAL failure (no cloud posture involved) must never retry -- the
    burn-the-turn policy (llm_engine.py ~3055-3057) stays intact."""
    motor = llm_engine.MotorVocalIA(queue.Queue(), lambda event: None)
    motor.current_model = "llama3"
    motor.use_system_role = True
    motor.ollama = MagicMock()
    motor.ollama.chat.side_effect = TimeoutError("chat stalled")
    motor._recover_from_stalled_inference = MagicMock()
    motor._handle_cloud_failure = MagicMock()

    result = motor._generar_dialogo("hola", source="direct", commit_history=False)

    assert result == ""
    assert motor.ollama.chat.call_count == 1
    motor._handle_cloud_failure.assert_not_called()
    assert motor._cloud_fallback_active is False


def test_dropped_turn_ui_callback_fires_on_empty_dialogo_ptt_path():
    """Companion (F1): _ejecutar_inferencia notifies the owner instead of
    letting a dropped ptt/direct turn evaporate silently."""
    events = []
    motor = llm_engine.MotorVocalIA(queue.Queue(), events.append)
    motor._generar_dialogo = MagicMock(return_value="")

    motor._ejecutar_inferencia("hola", source="ptt")

    assert "turn_dropped" in events
