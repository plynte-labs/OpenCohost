"""F1 (interruptible_speech_architecture_20260804, runtime-findings
2026-08-07): a CLOUD generation failure that engages auto-fallback must not
burn the owner's turn -- it gets exactly one in-turn local retry at the
generation funnel (`_generar_dialogo`) before the existing "" contract
applies. Mocking style mirrors tests/test_llm_engine_timeouts.py.
"""

import queue
from unittest.mock import MagicMock

from opencohost.core import llm_engine
from opencohost.core.engine.generation_orchestrator import GenerationResult
from opencohost.core.providers.cloud import cloud_llm_client
from opencohost.i18n import active as i18n_active


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


def test_cloud_watchdog_timeout_retry_reports_local_provider_attribution():
    """T3 (live session 2026-09-25): a turn that fails cloud and succeeds on
    the in-call local retry must be ATTRIBUTED to local, not the cloud
    provider it started under -- the exact mislabel the owner saw
    (MODEL_TRACE showed provider=local transport=local fallback_active=True,
    but the reply was reported under the cloud provider id). `_generar_dialogo`
    returns a `GenerationResult` (str subclass); its `answered_by_provider`/
    `answered_by_transport` must reflect the ACTUAL attempt that produced the
    text, not the posture snapshotted before the retry loop ran."""
    motor = _cloud_motor(provider="nvidia_nim")
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
    assert result.answered_by_provider == "local"
    assert result.answered_by_transport == "local"


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


def test_cloud_success_reports_cloud_provider_attribution():
    """Companion to the retry-attribution test above: a turn that succeeds
    on the FIRST cloud attempt (no fallback engaged) must still report the
    cloud provider id it actually answered under -- pins that the fix does
    not flip every turn to "local"."""
    motor = _cloud_motor(provider="nvidia_nim")

    def fake_watchdog(*, is_local, **kwargs):
        assert is_local is False
        return {"message": {"content": "Respuesta en la nube."}}

    motor._ollama_chat_with_watchdog = MagicMock(side_effect=fake_watchdog)

    result = motor._generar_dialogo("hola", source="ptt", commit_history=False)

    assert result == "Respuesta en la nube."
    assert result.answered_by_provider == "nvidia_nim"
    assert result.answered_by_transport == "cloud"


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


# ---------------------------------------------------------------------------
# T4 (live_session_fixes_20260925): "discard stale speech when a regenerated
# reply arrives" -- owner setting, default OFF. See
# MotorVocalIA._maybe_discard_stale_fallback_notice's docstring for the full
# root-cause writeup (llm_engine_speech.py) of why an UN-fixed D3 preempt
# SUSPENDS the fallback notice instead of discarding it, so it resumes and
# plays again after the reply.
# ---------------------------------------------------------------------------


def test_ejecutar_inferencia_discards_stale_notice_only_on_a_cloud_fallback_retry_reply():
    """`_ejecutar_inferencia` must call `_maybe_discard_stale_fallback_notice`
    BEFORE speaking the reply, but ONLY when `_generar_dialogo` actually took
    the same-call cloud->local retry branch this turn
    (`GenerationResult.cloud_fallback_retry=True`)."""
    motor = llm_engine.MotorVocalIA(queue.Queue(), lambda event: None)
    motor._generar_dialogo = MagicMock(
        return_value=GenerationResult("Respuesta de respaldo local.", cloud_fallback_retry=True)
    )
    calls = []
    motor._maybe_discard_stale_fallback_notice = MagicMock(
        side_effect=lambda source: calls.append(("discard", source))
    )
    motor._speak_or_submit = MagicMock(
        side_effect=lambda dialogo, source=None, **kw: calls.append(("speak", dialogo, source))
    )

    motor._ejecutar_inferencia("hola", source="ptt")

    assert calls == [
        ("discard", "ptt"),
        ("speak", "Respuesta de respaldo local.", "ptt"),
    ]


def test_ejecutar_inferencia_never_discards_for_an_ordinary_local_reply():
    """A turn that was local FROM THE START (no fallback event this call)
    must never trigger the discard -- there is no notice to discard."""
    motor = llm_engine.MotorVocalIA(queue.Queue(), lambda event: None)
    motor._generar_dialogo = MagicMock(
        return_value=GenerationResult("Respuesta normal.", cloud_fallback_retry=False)
    )
    motor._maybe_discard_stale_fallback_notice = MagicMock()
    motor._speak_or_submit = MagicMock()

    motor._ejecutar_inferencia("hola", source="ptt")

    motor._maybe_discard_stale_fallback_notice.assert_not_called()
    motor._speak_or_submit.assert_called_once_with("Respuesta normal.", source="ptt")


def test_ejecutar_inferencia_never_discards_for_a_plain_string_dialogo():
    """`GenerationResult` is the real return type, but a plain str (a test
    double or a future caller) has no `cloud_fallback_retry` attribute at
    all -- the `getattr(..., False)` read must default safely closed rather
    than raise."""
    motor = llm_engine.MotorVocalIA(queue.Queue(), lambda event: None)
    motor._generar_dialogo = MagicMock(return_value="Respuesta normal.")
    motor._maybe_discard_stale_fallback_notice = MagicMock()
    motor._speak_or_submit = MagicMock()

    motor._ejecutar_inferencia("hola", source="ptt")

    motor._maybe_discard_stale_fallback_notice.assert_not_called()
    motor._speak_or_submit.assert_called_once_with("Respuesta normal.", source="ptt")


def test_maybe_discard_stale_fallback_notice_off_by_default_is_a_noop():
    motor = llm_engine.MotorVocalIA(queue.Queue(), lambda event: None)
    motor._speech_router_enabled = True
    mock_router = MagicMock()
    motor._speech_router = mock_router
    pending_job = object()
    motor._pending_fallback_notice_job = pending_job
    motor._speak_or_submit = MagicMock()

    motor._maybe_discard_stale_fallback_notice("ptt")

    mock_router.discard_job.assert_not_called()
    motor._speak_or_submit.assert_not_called()
    assert motor._pending_fallback_notice_job is pending_job, "OFF must leave state untouched"


def test_maybe_discard_stale_fallback_notice_on_discards_job_and_speaks_transition_before_reply():
    motor = llm_engine.MotorVocalIA(queue.Queue(), lambda event: None)
    motor._provider_config["discard_stale_speech_on_regenerated_reply"] = True
    motor._speech_router_enabled = True
    mock_router = MagicMock()
    motor._speech_router = mock_router
    pending_job = object()
    motor._pending_fallback_notice_job = pending_job
    motor._speak_or_submit = MagicMock()

    motor._maybe_discard_stale_fallback_notice("ptt")

    mock_router.discard_job.assert_called_once_with(pending_job)
    assert motor._pending_fallback_notice_job is None, "the job reference must be consumed"
    motor._speak_or_submit.assert_called_once_with(
        i18n_active.provider_fallback_transition_notice(), source="ptt", priority=1
    )


def test_maybe_discard_stale_fallback_notice_on_without_a_pending_job_still_speaks_transition():
    """No notice was tracked (already finished on its own, or the fallback
    never spoke one) -- discard is skipped, but the transition phrase still
    plays: it is the "reply is landing now" marker, not conditioned on the
    notice's exact timing."""
    motor = llm_engine.MotorVocalIA(queue.Queue(), lambda event: None)
    motor._provider_config["discard_stale_speech_on_regenerated_reply"] = True
    motor._speech_router_enabled = True
    mock_router = MagicMock()
    motor._speech_router = mock_router
    motor._pending_fallback_notice_job = None
    motor._speak_or_submit = MagicMock()

    motor._maybe_discard_stale_fallback_notice("ptt")

    mock_router.discard_job.assert_not_called()
    motor._speak_or_submit.assert_called_once_with(
        i18n_active.provider_fallback_transition_notice(), source="ptt", priority=1
    )


def test_maybe_discard_stale_fallback_notice_on_but_router_disabled_is_a_noop():
    """The router is the playback path this whole mechanism depends on --
    without it there is no queue/stack to discard from, and the legacy
    blocking `_hablar` path has no interleaving to fix."""
    motor = llm_engine.MotorVocalIA(queue.Queue(), lambda event: None)
    motor._provider_config["discard_stale_speech_on_regenerated_reply"] = True
    motor._speech_router_enabled = False
    motor._pending_fallback_notice_job = object()
    motor._speak_or_submit = MagicMock()

    motor._maybe_discard_stale_fallback_notice("ptt")

    motor._speak_or_submit.assert_not_called()


def test_handle_cloud_failure_worker_tracks_the_notice_job_when_router_enabled(tmp_path):
    """`_handle_cloud_failure`'s worker must capture the `SpeechJob`
    `_speak_or_submit` returns under the router, so a same-turn retry reply
    can later discard it."""
    import time as _time

    motor = _cloud_motor()
    motor._speech_router_enabled = True
    mock_router = MagicMock()
    sentinel_job = object()
    mock_router.submit = MagicMock(return_value=sentinel_job)
    motor._speech_router = mock_router
    motor._prepare_model = MagicMock(return_value=True)

    motor._handle_cloud_failure("ptt")

    deadline = _time.monotonic() + 2.0
    while _time.monotonic() < deadline and motor._pending_fallback_notice_job is None:
        _time.sleep(0.01)

    assert motor._pending_fallback_notice_job is sentinel_job


def test_handle_cloud_failure_worker_leaves_no_pending_job_under_the_legacy_path(tmp_path):
    """Router disabled (kill switch OFF, or CTk): `_speak_or_submit` returns
    None, so nothing is ever tracked -- OFF stays byte-identical, and ON
    would find nothing to discard either, which is the correct outcome
    since there is no queue/stack under the legacy path."""
    import time as _time

    motor = _cloud_motor()
    assert motor._speech_router_enabled is False
    motor._hablar = MagicMock()
    motor._prepare_model = MagicMock(return_value=True)

    motor._handle_cloud_failure("ptt")

    deadline = _time.monotonic() + 2.0
    while _time.monotonic() < deadline and not motor._hablar.called:
        _time.sleep(0.01)

    assert motor._pending_fallback_notice_job is None
