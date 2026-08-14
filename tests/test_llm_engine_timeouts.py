"""Focused tests for LLM/TTS timeout coordination."""

import logging
import queue
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest

from opencohost.core import llm_engine
from opencohost.core.engine import llm_engine_models
from opencohost.config import settings
from opencohost.config.settings import CLOUD_CHAT_TIMEOUT, TTS_HEAVY_TIMEOUT, TTS_LIGHT_TIMEOUT
from opencohost.i18n import active as i18n_active


def test_audio_queue_timeout_waits_for_heavy_tts_plus_margin():
    """The consumer must not time out before a heavy TTS request can finish."""
    assert llm_engine.TTS_AUDIO_QUEUE_TIMEOUT > TTS_HEAVY_TIMEOUT
    assert llm_engine.TTS_AUDIO_QUEUE_TIMEOUT == max(TTS_HEAVY_TIMEOUT, TTS_LIGHT_TIMEOUT) + 15


def test_prepare_model_warms_selected_ollama_model():
    events = []
    motor = llm_engine.MotorVocalIA(queue.Queue(), events.append)
    motor.is_ready = True
    motor.ollama = MagicMock()

    assert motor._prepare_model("llama3") is True

    motor.ollama.generate.assert_called_once()
    call = motor.ollama.generate.call_args.kwargs
    assert call["model"] == "llama3"
    assert call["keep_alive"] == llm_engine.LLM_KEEP_ALIVE
    assert call["options"]["num_predict"] == 1
    assert motor._warmed_model == "llama3"
    assert motor._owns_ollama_model is True
    assert "model_warming" in events
    assert events[-1] == "ready"


def test_release_owned_ollama_model_uses_short_timeout_and_clears_owned_state():
    motor = llm_engine.MotorVocalIA(queue.Queue(), lambda event: None)
    motor.ollama = MagicMock()
    motor._loaded_model = "llama3"
    motor._warmed_model = "llama3"
    motor._owns_ollama_model = True

    assert motor.release_owned_ollama_model(timeout=0.2) is True

    motor.ollama.generate.assert_called_once_with(model="llama3", prompt="", keep_alive=0)
    assert motor._loaded_model is None
    assert motor._warmed_model is None
    assert motor._owns_ollama_model is False


def test_release_owned_ollama_model_skips_when_not_owned():
    motor = llm_engine.MotorVocalIA(queue.Queue(), lambda event: None)
    motor.ollama = MagicMock()
    motor._loaded_model = "llama3"
    motor._owns_ollama_model = False

    assert motor.release_owned_ollama_model(timeout=0.2) is False

    motor.ollama.generate.assert_not_called()


def test_check_ollama_service_warms_current_model_after_service_ready():
    events = []
    motor = llm_engine.MotorVocalIA(queue.Queue(), events.append)
    motor.current_model = "llama3"
    motor.ollama = MagicMock()

    assert motor._check_ollama_service() is True

    motor.ollama.list.assert_called_once()
    motor.ollama.generate.assert_called_once()
    assert motor._warmed_model == "llama3"


def test_ollama_chat_client_uses_configured_timeout():
    motor = llm_engine.MotorVocalIA(queue.Queue(), lambda event: None)
    created = {}

    class FakeOllamaModule:
        @staticmethod
        def Client(**kwargs):
            created.update(kwargs)
            return MagicMock()

    client = motor._create_ollama_chat_client(FakeOllamaModule)

    assert client is not None
    assert created == {"timeout": llm_engine.OLLAMA_CHAT_TIMEOUT}


# ---------------------------------------------------------------------------
# P0 fix (heavy_model_inference_recovery follow-up): the chat client's HTTP
# timeout must track the watchdog budget it is used under, instead of being
# pinned at OLLAMA_CHAT_TIMEOUT (180s). Root cause: with a 45s post-switch
# watchdog but a 180s HTTP timeout, the watchdog stops WAITING at 45s but the
# HTTP request (and Ollama's single runner) stays busy for up to 180s, so a
# rollback attempt right after cannot get a runner and fails with
# target_model_unavailable. See logs/opencohost_20260813_114819.log:195-222.
# ---------------------------------------------------------------------------


def test_ollama_chat_client_is_memoized_by_timeout():
    """Two calls with the same timeout must not build two ollama.Client objects."""
    motor = llm_engine.MotorVocalIA(queue.Queue(), lambda event: None)
    calls = []

    class FakeOllamaModule:
        @staticmethod
        def Client(**kwargs):
            calls.append(kwargs)
            return MagicMock()

    first = motor._create_ollama_chat_client(FakeOllamaModule, timeout=45.0)
    second = motor._create_ollama_chat_client(FakeOllamaModule, timeout=45.0)

    assert first is second
    assert len(calls) == 1


def test_post_switch_watchdog_budget_builds_45s_chat_client():
    """The chat client for the post-switch (45s) watchdog budget must be built
    with an HTTP timeout of 45s, not the steady-state 180s."""
    motor = llm_engine.MotorVocalIA(queue.Queue(), lambda event: None)
    created = {}

    class FakeOllamaModule:
        @staticmethod
        def Client(**kwargs):
            created.setdefault(kwargs["timeout"], MagicMock())
            return created[kwargs["timeout"]]

    assert motor._post_switch_watchdog_timeout == 45.0

    client_45 = motor._create_ollama_chat_client(
        FakeOllamaModule, timeout=motor._post_switch_watchdog_timeout
    )

    assert 45.0 in created
    assert 180 not in created and 180.0 not in created
    assert client_45 is created[45.0]


def test_ollama_chat_selects_client_matching_resolved_watchdog_timeout():
    """`_ollama_chat` must route to the client memoized for its `chat_timeout`,
    not the steady-state default -- this is the fix itself: the transport used
    for a call must match the HTTP timeout the watchdog budget for that call
    was resolved with."""
    motor = llm_engine.MotorVocalIA(queue.Queue(), lambda event: None)
    motor.ollama = MagicMock()  # steady-state fallback -- must NOT be used here
    default_client = MagicMock()
    fast_client = MagicMock()
    fast_client.chat.return_value = {"message": {"content": "ok"}}
    motor._ollama_chat_client = default_client
    motor._ollama_chat_clients = {180.0: default_client, 45.0: fast_client}

    result = motor._ollama_chat(model="qwopus", messages=[], chat_timeout=45.0)

    assert result == {"message": {"content": "ok"}}
    fast_client.chat.assert_called_once()
    default_client.chat.assert_not_called()
    motor.ollama.chat.assert_not_called()


def test_ollama_chat_falls_back_to_default_client_for_steady_state_timeout():
    """The steady-state 180s path is unchanged: the 180s timeout resolves to
    the same default client as before this fix."""
    motor = llm_engine.MotorVocalIA(queue.Queue(), lambda event: None)
    default_client = MagicMock()
    default_client.chat.return_value = {"message": {"content": "steady"}}
    motor._ollama_chat_client = default_client

    result = motor._ollama_chat(model="llama3", messages=[], chat_timeout=180.0)

    assert result == {"message": {"content": "steady"}}
    default_client.chat.assert_called_once()


def test_ollama_chat_with_watchdog_threads_resolved_timeout_to_client_selection():
    """`_ollama_chat_with_watchdog` must thread its `timeout` into `_ollama_chat`
    as `chat_timeout`, so client selection actually receives the watchdog's
    resolved budget end-to-end (not just when `_ollama_chat` is called directly)."""
    motor = llm_engine.MotorVocalIA(queue.Queue(), lambda event: None)
    fast_client = MagicMock()
    fast_client.chat.return_value = {"message": {"content": "fast"}}
    motor._ollama_chat_client = MagicMock()
    motor._ollama_chat_clients = {45.0: fast_client}

    result = motor._ollama_chat_with_watchdog(
        timeout=45.0, model="qwopus", messages=[], keep_alive=-1, options={},
    )

    assert result == {"message": {"content": "fast"}}
    fast_client.chat.assert_called_once()


def test_transport_read_timeout_surfaces_as_watchdog_timeout_error():
    """A transport-level read timeout raised by the worker thread (the real
    exception httpx/ollama raise when the HTTP timeout expires) must surface
    as the SAME `TimeoutError('watchdog_timeout:...')` contract the pure
    wait-based watchdog timeout uses. This is the regression guard: once the
    HTTP timeout can fire at ~the same moment as the watchdog, an unconverted
    httpx exception would propagate instead and `_is_watchdog_timeout_error`
    would return False, silently killing automatic recovery."""
    motor = llm_engine.MotorVocalIA(queue.Queue(), lambda event: None)

    def blows_up(**kwargs):
        raise httpx.ReadTimeout("the read operation timed out")

    with pytest.raises(TimeoutError) as excinfo:
        motor._call_with_watchdog(blows_up, timeout=45.0, model="qwopus")

    assert str(excinfo.value).startswith("watchdog_timeout:")
    assert motor._is_watchdog_timeout_error(excinfo.value) is True


def test_non_timeout_exception_propagates_unchanged():
    """A genuine (non-timeout) transport error must NOT be converted into a
    fake watchdog timeout -- that would swallow real errors and trigger a
    bogus rollback."""
    motor = llm_engine.MotorVocalIA(queue.Queue(), lambda event: None)

    def blows_up(**kwargs):
        raise ValueError("not a timeout at all")

    with pytest.raises(ValueError, match="not a timeout at all"):
        motor._call_with_watchdog(blows_up, timeout=45.0, model="qwopus")


def test_call_with_watchdog_rejects_streaming_calls():
    """A streaming call routed through this seam is a SILENT no-op, so the seam
    must refuse it loudly instead.

    `_call_with_watchdog` measures "did `call(**kwargs)` return within budget".
    Calling a function that returns a generator returns WITHOUT executing its
    body, so `done.wait()` succeeds in microseconds and the watchdog blesses an
    un-started generator: the stall recovery validated on 2026-06-17 (qwopus
    hung, watchdog fired at 45.00s, automatic rollback) would degrade into a
    guaranteed false success, and a genuinely hung model would never be
    detected. Streaming gets its own seam that iterates on the calling thread;
    this tripwire makes the wrong route a loud failure at the first call rather
    than a watchdog that quietly stops working.
    """
    motor = llm_engine.MotorVocalIA(queue.Queue(), lambda event: None)

    def never_runs(**kwargs):  # pragma: no cover - the guard fires before this
        raise AssertionError("the streaming call must not reach the worker thread")

    with pytest.raises(ValueError, match="stream=True"):
        motor._call_with_watchdog(never_runs, timeout=45.0, model="qwopus", stream=True)


def test_call_with_watchdog_allows_explicit_stream_false():
    """Only `stream=True` is rejected. An explicit `stream=False` is the normal
    buffered call and must pass through untouched -- the tripwire must not
    become a blanket ban on the keyword."""
    motor = llm_engine.MotorVocalIA(queue.Queue(), lambda event: None)

    def buffered(**kwargs):
        return {"message": {"content": "ok"}, "stream": kwargs.get("stream")}

    response = motor._call_with_watchdog(buffered, timeout=45.0, model="llama3", stream=False)

    assert response["message"]["content"] == "ok"
    assert response["stream"] is False


def test_watchdog_still_times_out_when_worker_never_returns():
    """Existing watchdog behavior (no response within budget -> watchdog_timeout)
    must still hold after adding the transport-exception translation."""
    motor = llm_engine.MotorVocalIA(queue.Queue(), lambda event: None)
    release = threading.Event()

    def blocks_forever(**kwargs):
        release.wait(timeout=5.0)
        return {"message": {"content": "too late"}}

    try:
        with pytest.raises(TimeoutError) as excinfo:
            motor._call_with_watchdog(blocks_forever, timeout=0.1, model="qwopus")
        assert str(excinfo.value).startswith("watchdog_timeout:")
    finally:
        release.set()


def test_non_reasoning_models_use_configured_token_cap():
    motor = llm_engine.MotorVocalIA(queue.Queue(), lambda event: None)
    motor.current_model = "llama3"
    motor.use_system_role = True
    motor.ollama = MagicMock()
    motor.ollama.chat.return_value = {"message": {"content": "Respuesta segura."}}

    assert motor._generar_dialogo("hola", source="direct", commit_history=False) == "Respuesta segura."

    options = motor.ollama.chat.call_args.kwargs["options"]
    assert options["num_predict"] == llm_engine.LLM_MAX_TOKENS
    assert options["num_predict"] == 768


def test_reasoning_models_skip_fixed_token_cap():
    motor = llm_engine.MotorVocalIA(queue.Queue(), lambda event: None)
    motor.current_model = "qwen3:4b"
    motor.use_system_role = True
    motor.ollama = MagicMock()
    motor.ollama.chat.return_value = {"message": {"content": "Respuesta segura."}}

    assert motor._generar_dialogo("hola", source="direct", commit_history=False) == "Respuesta segura."

    options = motor.ollama.chat.call_args.kwargs["options"]
    assert "num_predict" not in options


def test_replace_pending_keeps_latest_item_for_same_source():
    motor = llm_engine.MotorVocalIA(queue.Queue(), lambda event: None)

    motor.enqueue("old agenda", priority=2, source="kira-agenda")
    motor.enqueue("ptt input", priority=0, source="ptt")
    motor.replace_pending("new agenda", priority=2, source="kira-agenda")

    queued = [(item[2], item[3]) for item in motor._priority_queue]
    assert ("old agenda", "kira-agenda") not in queued
    assert ("new agenda", "kira-agenda") in queued
    assert ("ptt input", "ptt") in queued


def test_drop_pending_sources_removes_agenda_without_dropping_ptt_or_chat():
    motor = llm_engine.MotorVocalIA(queue.Queue(), lambda event: None)
    motor.enqueue("agenda", priority=2, source="kira-agenda")
    motor.enqueue("closing", priority=2, source="kira-agenda-stop")
    motor.enqueue("chat", priority=1, source="chat")
    motor.enqueue("ptt", priority=0, source="ptt")
    motor.enqueue_accumulation("old agenda overflow", source="kira-agenda")
    motor.enqueue_accumulation("old chat overflow", source="chat")

    removed = motor.drop_pending_sources(("kira-agenda",))

    assert removed == 3
    assert all(not item[3].startswith("kira-agenda") for item in motor._priority_queue)
    assert {item[3] for item in motor._priority_queue} == {"ptt", "chat"}
    assert [item[1] for item in motor._accumulation_buffer] == ["old chat overflow"]


def test_pending_ptt_or_chat_can_pause_cached_agenda_prefetch():
    motor = llm_engine.MotorVocalIA(queue.Queue(), lambda event: None)
    motor.enqueue("agenda", priority=2, source="kira-agenda")

    assert motor.has_pending_priority_before(2) is False

    motor.enqueue("chat pulse", priority=1, source="chat")
    assert motor.has_pending_priority_before(2) is True

    motor.enqueue("ptt input", priority=0, source="ptt")
    assert motor.has_pending_priority_before(1) is True


def test_agenda_output_sanitizer_replaces_artificial_closings():
    motor = llm_engine.MotorVocalIA(queue.Queue(), lambda event: None)

    sanitized = motor._sanitize_agenda_output("Y eso es todo. ¡Hasta luego, próximo episodio!")

    assert "Hasta luego" not in sanitized
    assert "próximo episodio" not in sanitized
    assert "matices" in sanitized


def test_tts_sanitizer_preserves_markdown_emphasis_content():
    motor = llm_engine.MotorVocalIA(queue.Queue(), lambda event: None)

    text = "Esto es *importante*. Esto es **muy importante**. Y esto es ***crítico***."

    assert motor._sanitize_tts_text_for_playback(text) == (
        "Esto es importante. Esto es muy importante. Y esto es crítico."
    )


def test_tts_sanitizer_keeps_math_and_code_like_asterisks():
    motor = llm_engine.MotorVocalIA(queue.Queue(), lambda event: None)

    text = "Cinco por diez es 5*10=50. En código a*b queda igual. Potencia: 2 ** 8."

    assert motor._sanitize_tts_text_for_playback(text) == text


def test_tts_sanitizer_fast_path_without_asterisks_returns_same_text():
    motor = llm_engine.MotorVocalIA(queue.Queue(), lambda event: None)
    text = "Respuesta normal sin énfasis markdown."

    assert motor._sanitize_tts_text_for_playback(text) is text


def test_agenda_prefetch_generates_text_without_speaking_until_consumed():
    motor = llm_engine.MotorVocalIA(queue.Queue(), lambda event: None)
    spoken = []
    spoke = threading.Event()
    motor._generar_dialogo = MagicMock(return_value="Texto cacheado")
    motor.agenda_output_preview_validator = MagicMock(return_value=True)
    motor.agenda_output_recorder = MagicMock()

    def fake_hablar(text, source="direct"):
        spoken.append(text)
        spoke.set()

    motor._hablar = fake_hablar

    assert motor.prefetch_agenda("prompt agenda", source="kira-agenda") is True
    assert motor.wait_prefetched_agenda(timeout=1.0) is True
    motor.agenda_output_preview_validator.assert_called_once_with("Texto cacheado")
    assert spoken == []

    assert motor.play_prefetched_agenda() is True
    assert spoke.wait(1.0) is True
    assert spoken == ["Texto cacheado"]
    motor.agenda_output_recorder.assert_called_once_with("Texto cacheado")
    assert list(motor.historial)[-2:] == [
        {"role": "user", "content": "[agenda segura: prompt interno omitido]", "source": "kira-agenda", "private": False},
        {"role": "assistant", "content": "Texto cacheado", "source": "kira-agenda", "private": False},
    ]


def test_agenda_prefetch_rejects_repeated_text_before_caching():
    motor = llm_engine.MotorVocalIA(queue.Queue(), lambda event: None)
    motor._generar_dialogo = MagicMock(return_value="Texto repetido")
    motor.agenda_output_preview_validator = MagicMock(return_value=False)

    assert motor.prefetch_agenda("prompt agenda", source="kira-agenda") is True
    assert motor.wait_prefetched_agenda(timeout=1.0) is False
    assert motor.play_prefetched_agenda() is False


def test_agenda_prefetch_is_cleared_when_agenda_pending_is_replaced():
    motor = llm_engine.MotorVocalIA(queue.Queue(), lambda event: None)
    motor._prefetched_agenda = {"payload": "old", "dialogo": "viejo", "source": "kira-agenda", "priority": 2}

    motor.replace_pending("nuevo", priority=2, source="kira-agenda")

    assert motor._prefetched_agenda is None


def test_agenda_generation_uses_controller_guardrail_before_history_or_speech():
    motor = llm_engine.MotorVocalIA(queue.Queue(), lambda event: None)
    motor.current_model = "llama3"
    motor.use_system_role = True
    motor.ollama = MagicMock()
    motor.ollama.chat.return_value = {"message": {"content": "Según el resumen, el chat dice que..."}}
    motor.agenda_output_validator = MagicMock(return_value=False)

    dialogo = motor._generar_dialogo("CHAT COMPACTO FILTRADO: secreto", source="kira-agenda", commit_history=True)

    assert dialogo == ""
    motor.agenda_output_validator.assert_called_once()
    assert list(motor.historial) == []


def test_output_guard_blocked_direct_commits_user_turn_and_fallback():
    motor = llm_engine.MotorVocalIA(queue.Queue(), lambda event: None)
    motor.current_model = "llama3"
    motor.use_system_role = True
    motor.ollama = MagicMock()
    motor.ollama.chat.return_value = {
        "message": {"content": "Como modelo de lenguaje, no puedo responder eso."}
    }

    dialogo = motor._generar_dialogo("hola", source="direct", commit_history=True)

    # R9 (AI self-ID) is global: blocked even for direct source. The spoken
    # fallback line replaces dead air. D4 (memoria_quality_20260717): the blocked
    # exchange now ENTERS history as (user turn, spoken fallback) instead of
    # vanishing (F4) — but the blocked LLM output itself never reaches history.
    assert dialogo in i18n_active.LEGACY_GUARDRAIL_FALLBACK_LINES
    assert len(motor.historial) == 2
    assert motor.historial[-2]["content"] == "hola"
    assert motor.historial[-1]["content"] == dialogo
    assert "Como modelo de lenguaje" not in motor.historial[-1]["content"]


def test_output_guard_blocked_chat_commits_user_turn_and_fallback():
    motor = llm_engine.MotorVocalIA(queue.Queue(), lambda event: None)
    motor.current_model = "llama3"
    motor.use_system_role = True
    motor.ollama = MagicMock()
    motor.ollama.chat.return_value = {
        "message": {"content": "Tu audiencia está muy callada hoy."}
    }

    dialogo = motor._generar_dialogo("chat compactado", source="chat", commit_history=True)

    # R10 still applies to chat sources; the fallback line is spoken instead of
    # dead air. D4: the blocked exchange enters history (user turn + fallback);
    # the blocked LLM output ("Tu audiencia...") never does.
    assert dialogo in i18n_active.LEGACY_GUARDRAIL_FALLBACK_LINES
    assert len(motor.historial) == 2
    assert motor.historial[-2]["content"] == "chat compactado"
    assert motor.historial[-1]["content"] == dialogo
    assert "Tu audiencia" not in motor.historial[-1]["content"]


def test_ollama_chat_timeout_is_logged_and_returns_empty(caplog):
    motor = llm_engine.MotorVocalIA(queue.Queue(), lambda event: None)
    motor.current_model = "llama3"
    motor.use_system_role = True
    motor.ollama = MagicMock()
    motor.ollama.chat.side_effect = TimeoutError("chat stalled")

    with caplog.at_level(logging.WARNING, logger="OpenCohost"):
        dialogo = motor._generar_dialogo("hola", source="direct", commit_history=True)

    assert dialogo == ""
    assert motor.ollama.chat.call_count == 1
    assert motor._last_llm_failure == {
        "model": "llama3",
        "source": "direct",
        "attempt": 1,
        "reason": "TimeoutError",
        "message": "chat stalled",
    }
    assert any("ERROR Ollama chat (TimeoutError)" in item for item in list(motor.log_queue.queue))
    assert any("Ollama chat transport failure" in record.message for record in caplog.records)
    assert list(motor.historial) == []


def test_cloud_watchdog_timeout_routes_to_fallback_not_rollback():
    """Phase 4 (multi_provider_llm_20260723): a cloud watchdog timeout must
    route to `_handle_cloud_failure`, NEVER `_recover_from_stalled_inference`
    (spec B — a cloud stall must never roll back a local model). The cloud
    watchdog timeout resolves independently of the local watchdog timeouts."""
    motor = llm_engine.MotorVocalIA(queue.Queue(), lambda event: None)
    motor._provider_config = {
        "active_provider": "openai",
        "fallback_mode": "auto",
        "profiles": {"openai": {"base_url": "https://api.example.com/v1", "model": "gpt-cloud"}},
    }
    motor._recover_from_stalled_inference = MagicMock()
    motor._handle_cloud_failure = MagicMock()
    motor._ollama_chat_with_watchdog = MagicMock(
        side_effect=TimeoutError(f"watchdog_timeout:{CLOUD_CHAT_TIMEOUT:.2f}s")
    )

    result = motor._generar_dialogo("hola", source="direct", commit_history=False)

    assert result == ""
    motor._recover_from_stalled_inference.assert_not_called()
    motor._handle_cloud_failure.assert_called_once()
    assert motor._handle_cloud_failure.call_args.args == ("direct",)
    # A watchdog timeout carries no HTTP status/headers to classify from --
    # llm_engine.py's watchdog-timeout branch hardcodes CLOUD_ERROR_TRANSIENT
    # (see its own comment) and passes no retry_after_seconds at all.
    assert motor._handle_cloud_failure.call_args.kwargs == {
        "failure_class": llm_engine.cloud_llm_client.CLOUD_ERROR_TRANSIENT
    }
    assert motor._resolve_chat_watchdog_timeout("anything") == CLOUD_CHAT_TIMEOUT
    assert CLOUD_CHAT_TIMEOUT != motor._inference_watchdog_timeout
    assert CLOUD_CHAT_TIMEOUT != motor._post_switch_watchdog_timeout


def test_ollama_chat_connection_error_is_logged_and_returns_empty(caplog):
    motor = llm_engine.MotorVocalIA(queue.Queue(), lambda event: None)
    motor.current_model = "llama3"
    motor.use_system_role = True
    motor.ollama = MagicMock()
    motor.ollama.chat.side_effect = ConnectionError("ollama refused")

    with caplog.at_level(logging.WARNING, logger="OpenCohost"):
        dialogo = motor._generar_dialogo("hola", source="chat", commit_history=True)

    assert dialogo == ""
    assert motor.ollama.chat.call_count == 1
    assert motor._last_llm_failure["reason"] == "ConnectionError"
    assert motor._last_llm_failure["message"] == "ollama refused"
    assert any("ERROR Ollama chat (ConnectionError)" in item for item in list(motor.log_queue.queue))
    assert any("Ollama chat transport failure" in record.message for record in caplog.records)
    assert list(motor.historial) == []


def test_agenda_output_transformer_caps_before_history_commit():
    motor = llm_engine.MotorVocalIA(queue.Queue(), lambda event: None)
    motor.current_model = "llama3"
    motor.use_system_role = True
    motor.ollama = MagicMock()
    motor.ollama.chat.return_value = {"message": {"content": "uno dos tres cuatro"}}
    motor.agenda_output_transformer = MagicMock(return_value="uno dos")

    dialogo = motor._generar_dialogo("prompt interno", source="kira-agenda", commit_history=True)

    assert dialogo == "uno dos"
    motor.agenda_output_transformer.assert_called_once_with("uno dos tres cuatro")
    assert list(motor.historial)[-1] == {
        "role": "assistant", "content": "uno dos", "source": "kira-agenda", "private": False,
    }


def test_agenda_history_redacts_raw_compact_prompt_when_committed():
    motor = llm_engine.MotorVocalIA(queue.Queue(), lambda event: None)

    motor._commit_history("CHAT COMPACTO FILTRADO: usuario dice algo", "Salida segura", source="kira-agenda")

    history_text = "\n".join(item["content"] for item in motor.historial)
    assert "CHAT COMPACTO FILTRADO" not in history_text
    assert "usuario dice algo" not in history_text
    assert "Salida segura" in history_text


def test_commit_history_tags_both_entries_with_source():
    # Source tag (history_source_tag_20260629 Task A): both entries of a
    # committed turn carry the in-scope `source` so readers can separate
    # host (direct/ptt) from viewer (chat) without a parallel structure.
    motor = llm_engine.MotorVocalIA(queue.Queue(), lambda event: None)
    motor._commit_history("contexto host", "respuesta de kira", source="chat")
    assert list(motor.historial)[-2]["source"] == "chat"
    assert list(motor.historial)[-1]["source"] == "chat"


def test_agenda_history_redacts_editorial_context_when_committed():
    motor = llm_engine.MotorVocalIA(queue.Queue(), lambda event: None)

    motor._commit_history(
        "<editorial_context>{\"topic\":\"Monetización\",\"streamer_take\":\"pay-to-win\"}</editorial_context>",
        "Salida segura",
        source="kira-agenda",
    )

    history_text = "\n".join(item["content"] for item in motor.historial)
    assert "<editorial_context>" not in history_text
    assert "pay-to-win" not in history_text
    assert "Salida segura" in history_text


def test_ptt_priority_wins_over_chat_and_agenda():
    """PTT (priority 0) must always be processed before chat (1) and agenda (2)."""
    motor = llm_engine.MotorVocalIA(queue.Queue(), lambda event: None)

    motor.enqueue("agenda topic", priority=2, source="kira-agenda")
    motor.enqueue("chat comment", priority=1, source="chat")
    motor.enqueue("ptt input", priority=0, source="ptt")

    # Queue is sorted by priority ascending: PTT first
    items = motor._priority_queue
    assert items[0][3] == "ptt"
    assert items[0][0] == 0
    assert items[1][3] == "chat"
    assert items[1][0] == 1
    assert items[2][3] == "kira-agenda"
    assert items[2][0] == 2


def test_overflow_drops_lowest_priority_preserving_ptt():
    """When queue exceeds max, lowest priority items are dropped first."""
    motor = llm_engine.MotorVocalIA(queue.Queue(), lambda event: None)
    motor._pq_max_items = 3

    motor.enqueue("agenda 1", priority=2, source="kira-agenda")
    motor.enqueue("agenda 2", priority=2, source="kira-agenda")
    motor.enqueue("chat 1", priority=1, source="chat")
    motor.enqueue("ptt 1", priority=0, source="ptt")

    # Should have 3 items: PTT, chat, and one agenda (the newest agenda)
    sources = [item[3] for item in motor._priority_queue]
    assert "ptt" in sources
    assert "chat" in sources
    # One agenda should remain (the one that wasn't dropped)
    assert sources.count("kira-agenda") == 1
    assert len(motor._priority_queue) == 3


def test_overflow_drops_agenda_before_chat():
    """Agenda (priority 2) must be dropped before chat (priority 1)."""
    motor = llm_engine.MotorVocalIA(queue.Queue(), lambda event: None)
    motor._pq_max_items = 3

    motor.enqueue("agenda 1", priority=2, source="kira-agenda")
    motor.enqueue("chat 1", priority=1, source="chat")
    motor.enqueue("chat 2", priority=1, source="chat")
    motor.enqueue("ptt 1", priority=0, source="ptt")

    sources = [item[3] for item in motor._priority_queue]
    assert "ptt" in sources
    # Both chats should survive over agenda
    assert sources.count("chat") == 2
    assert sources.count("kira-agenda") <= 1


def test_stale_chat_expires_before_processing(monkeypatch):
    """Expired chat should be discarded, not reacted to later as accumulation."""
    # Tier split (tauri_stream_chat_20260812): stream discard window is the
    # turn_priority module setting now, not _pq_ttl_seconds.
    monkeypatch.setattr(llm_engine.turn_priority, "STREAM_TTL_SECONDS", 0.01)
    motor = llm_engine.MotorVocalIA(queue.Queue(), lambda event: None)
    motor._ejecutar_inferencia = MagicMock()

    motor.enqueue("old chat", source="chat")
    time.sleep(0.02)  # Wait for TTL to expire

    motor._processing = False
    motor._speaking = False
    motor._process_priority_queue()

    assert motor._priority_queue == []
    assert motor._accumulation_buffer == []
    motor._ejecutar_inferencia.assert_not_called()


def test_accumulation_expiry_logs_count_without_raw_payload(caplog):
    motor = llm_engine.MotorVocalIA(queue.Queue(), lambda event: None)
    motor._accum_ttl = 1.0
    raw_expired_payload = "RAW_EXPIRED_CHAT_SECRET"
    raw_fresh_payload = "RAW_FRESH_CHAT_SECRET"
    raw_source = "secret-source"

    now = time.time()
    motor._accumulation_buffer = [
        (now - 2.0, raw_expired_payload, raw_source),
        (now, raw_fresh_payload, raw_source),
    ]

    with caplog.at_level(logging.WARNING, logger="OpenCohost"):
        accumulated = motor._flush_accumulation()

    log_text = "\n".join(
        [record.message for record in caplog.records] + list(motor.log_queue.queue)
    )
    assert "descartados 1 mensajes expirados" in log_text
    assert raw_expired_payload not in log_text
    assert raw_fresh_payload not in log_text
    assert raw_source not in log_text
    assert raw_fresh_payload in accumulated
    assert motor._last_accumulation_flush_count == 1


def test_accumulation_item_overflow_logs_count_without_raw_payload(caplog):
    motor = llm_engine.MotorVocalIA(queue.Queue(), lambda event: None)
    motor._accum_max_items = 1
    raw_first_payload = "RAW_ITEM_OVERFLOW_SECRET_1"
    raw_second_payload = "RAW_ITEM_OVERFLOW_SECRET_2"
    raw_source = "secret-source"

    with caplog.at_level(logging.WARNING, logger="OpenCohost"):
        motor.enqueue_accumulation(raw_first_payload, source=raw_source)
        motor.enqueue_accumulation(raw_second_payload, source=raw_source)

    log_text = "\n".join(
        [record.message for record in caplog.records] + list(motor.log_queue.queue)
    )
    assert "descartados 1 mensajes por límite de items" in log_text
    assert raw_first_payload not in log_text
    assert raw_second_payload not in log_text
    assert raw_source not in log_text


def test_accumulation_char_overflow_logs_count_without_raw_payload(caplog):
    motor = llm_engine.MotorVocalIA(queue.Queue(), lambda event: None)
    motor._accum_max_chars = 5
    raw_payload = "RAW_CHAR_OVERFLOW_SECRET"
    raw_source = "secret-source"

    with caplog.at_level(logging.WARNING, logger="OpenCohost"):
        motor.enqueue_accumulation(raw_payload, source=raw_source)

    log_text = "\n".join(
        [record.message for record in caplog.records] + list(motor.log_queue.queue)
    )
    assert "descartados 1 mensajes por límite de caracteres" in log_text
    assert raw_payload not in log_text
    assert raw_source not in log_text


def test_ptt_items_never_expire_via_ttl():
    """PTT (priority 0) items must not be expired by TTL check."""
    motor = llm_engine.MotorVocalIA(queue.Queue(), lambda event: None)
    motor._pq_ttl_seconds = 0.01
    motor._ejecutar_inferencia = MagicMock()

    motor.enqueue("ptt important", priority=0, source="ptt")
    time.sleep(0.02)

    motor._processing = False
    motor._speaking = False
    motor._process_priority_queue()

    motor._ejecutar_inferencia.assert_called_once_with("ptt important", source="ptt", history_text=None)


def test_tts_none_text_balances_events_and_clears_speech_source():
    events = []

    def record_event(event):
        events.append((event, motor.current_speech_source))

    motor = llm_engine.MotorVocalIA(queue.Queue(), record_event)

    motor._hablar(None, source="kira-agenda")

    assert events == [
        ("speaking_start", "kira-agenda"),
        ("speaking_end", None),
    ]
    assert motor.is_speaking is False
    assert motor.current_speech_source is None


def test_heavy_tts_missing_reference_balances_events_and_clears_speech_source():
    """_hablar with no reference must fall back to ligero and complete cleanly."""
    from unittest.mock import patch, MagicMock
    import asyncio as _asyncio

    events = []

    def record_event(event):
        events.append((event, motor.current_speech_source))

    motor = llm_engine.MotorVocalIA(queue.Queue(), record_event)
    motor.motor_tts = "pesado"
    motor.voz_referencia = None
    motor.pygame = MagicMock()
    motor.pygame.mixer.music.load = MagicMock()
    motor.pygame.mixer.music.play = MagicMock()
    motor.pygame.mixer.music.get_busy = MagicMock(return_value=False)
    motor.pygame.mixer.music.unload = MagicMock()

    def fake_asyncio_run(coro, *args, **kwargs):
        try:
            coro.close()
        except Exception:
            pass

    with patch("opencohost.core.llm_engine.asyncio") as mock_asyncio:
        mock_asyncio.run.side_effect = fake_asyncio_run
        mock_asyncio.wait_for = _asyncio.wait_for
        mock_asyncio.TimeoutError = _asyncio.TimeoutError
        motor._hablar("Texto suficientemente largo para pedir audio.", source="kira-agenda")

    assert events[0] == ("speaking_start", "kira-agenda")
    assert events[-1] == ("speaking_end", None)
    assert motor.is_speaking is False
    assert motor.current_speech_source is None


def test_heavy_tts_completion_clears_speech_source_before_speaking_end(tmp_path):
    events = []

    def record_event(event):
        events.append((event, motor.current_speech_source))

    motor = llm_engine.MotorVocalIA(queue.Queue(), record_event)
    motor.motor_tts = "pesado"
    ref = tmp_path / "voice.wav"
    ref.write_bytes(b"ref")
    motor.voz_referencia = str(ref)

    class FakeMusic:
        def load(self, path):
            pass

        def play(self):
            pass

        def get_busy(self):
            return False

        def unload(self):
            pass

    motor.pygame = MagicMock()
    motor.pygame.mixer.music = FakeMusic()

    original_post = llm_engine.requests.post
    try:
        llm_engine.requests.post = MagicMock(
            return_value=MagicMock(status_code=200, content=b"wav")
        )
        motor._hablar("Texto suficientemente largo para generar audio.", source="direct")
    finally:
        llm_engine.requests.post = original_post

    assert events == [
        ("speaking_start", "direct"),
        ("speaking_end", None),
    ]
    assert motor.is_speaking is False
    assert motor.current_speech_source is None


def test_tts_playback_error_clears_speech_source_before_speaking_end(tmp_path):
    events = []

    def record_event(event):
        events.append((event, motor.current_speech_source))

    motor = llm_engine.MotorVocalIA(queue.Queue(), record_event)
    motor.motor_tts = "pesado"
    ref = tmp_path / "voice.wav"
    ref.write_bytes(b"ref")
    motor.voz_referencia = str(ref)

    class FailingMusic:
        def load(self, path):
            raise RuntimeError("audio device unavailable")

        def unload(self):
            pass

    motor.pygame = MagicMock()
    motor.pygame.mixer.music = FailingMusic()

    original_post = llm_engine.requests.post
    try:
        llm_engine.requests.post = MagicMock(
            return_value=MagicMock(status_code=200, content=b"wav")
        )
        motor._hablar("Texto suficientemente largo para generar audio.", source="direct")
    finally:
        llm_engine.requests.post = original_post

    assert events == [
        ("speaking_start", "direct"),
        ("speaking_end", None),
    ]
    assert motor.is_speaking is False
    assert motor.current_speech_source is None


def test_speaking_start_callback_failure_clears_speech_source():
    events = []

    def failing_callback(event):
        events.append((event, motor.current_speech_source))
        if event == "speaking_start":
            raise RuntimeError("ui callback failed")

    motor = llm_engine.MotorVocalIA(queue.Queue(), failing_callback)

    with pytest.raises(RuntimeError, match="ui callback failed"):
        motor._hablar("Texto suficientemente largo para iniciar habla.", source="kira-agenda")

    assert events == [("speaking_start", "kira-agenda")]
    assert motor.is_speaking is False
    assert motor.current_speech_source is None


def test_heavy_tts_continues_after_connection_error(tmp_path):
    events = []
    motor = llm_engine.MotorVocalIA(queue.Queue(), events.append)
    motor.motor_tts = "pesado"
    ref = tmp_path / "voice.wav"
    ref.write_bytes(b"ref")
    motor.voz_referencia = str(ref)

    class FakeMusic:
        def __init__(self):
            self.loaded = []

        def load(self, path):
            self.loaded.append(path)

        def play(self):
            pass

        def get_busy(self):
            return False

        def unload(self):
            pass

    music = FakeMusic()
    motor.pygame = MagicMock()
    motor.pygame.mixer.music = music

    ok_response = MagicMock(status_code=200, content=b"wav")

    original_post = llm_engine.requests.post
    calls = {"count": 0}

    def fake_post(*args, **kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            raise llm_engine.requests.exceptions.ConnectionError("temporary outage")
        return ok_response

    try:
        llm_engine.requests.post = fake_post
        motor._hablar(
            "Primera oración suficientemente larga para generar chunk. "
            "Segunda oración suficientemente larga para generar otro chunk.",
            source="direct",
        )
    finally:
        llm_engine.requests.post = original_post

    assert calls["count"] == 2
    assert len(music.loaded) == 1
    assert events[0] == "speaking_start"
    assert events[-1] == "speaking_end"


# ---------------------------------------------------------------------------
# Guardrail fallback lines (no-LLM spoken fallback on blocked output)
# ---------------------------------------------------------------------------


def test_guardrail_fallback_returns_line_for_direct():
    motor = llm_engine.MotorVocalIA(queue.Queue(), lambda event: None)
    line = motor._guardrail_fallback_line("direct")
    assert line in i18n_active.LEGACY_GUARDRAIL_FALLBACK_LINES


def test_guardrail_fallback_returns_line_for_chat_sources():
    motor = llm_engine.MotorVocalIA(queue.Queue(), lambda event: None)
    for source in ("ptt", "chat", "accumulated"):
        assert motor._guardrail_fallback_line(source) in i18n_active.LEGACY_GUARDRAIL_FALLBACK_LINES


def test_guardrail_fallback_empty_for_agenda_sources():
    """Agenda has its own rejection/recovery path — no canned line."""
    motor = llm_engine.MotorVocalIA(queue.Queue(), lambda event: None)
    assert motor._guardrail_fallback_line("kira-agenda") == ""
    assert motor._guardrail_fallback_line("kira-agenda-stop") == ""


def test_guardrail_fallback_rotates_lines():
    """Consecutive blocks never speak the same line twice in a row."""
    motor = llm_engine.MotorVocalIA(queue.Queue(), lambda event: None)
    lines = [motor._guardrail_fallback_line("direct") for _ in range(6)]
    for a, b in zip(lines, lines[1:]):
        assert a != b


def test_guardrail_fallback_lines_pass_output_guard():
    """Canned lines must never trip the guard themselves, for any source."""
    from opencohost.config.validation import output_guard

    for line in i18n_active.LEGACY_GUARDRAIL_FALLBACK_LINES:
        for source in ("direct", "chat", "kira-agenda"):
            allowed, reason = output_guard(line, source=source)
            assert allowed, f"fallback line blocked ({source}): {line!r} — {reason}"


# ---------------------------------------------------------------------------
# Missing-reference auto-fallback (fix/missing-reference-fallback)
# ---------------------------------------------------------------------------


def test_process_context_no_ref_does_not_drop_message():
    """process_context must NOT short-circuit when heavy TTS lacks a reference."""
    inference_calls = []

    motor = llm_engine.MotorVocalIA(queue.Queue(), lambda e: None)
    motor.is_ready = True
    motor.motor_tts = "pesado"
    motor.voz_referencia = None
    motor._ejecutar_inferencia = lambda payload, source, history_text=None: inference_calls.append(payload)

    motor._dispatch_command("process_context", "hello world")

    assert inference_calls == ["hello world"], (
        "Message was dropped before LLM ran — hard-block still present"
    )


def test_process_context_no_ref_does_not_log_falta_audio():
    """The old hard-block log 'Falta audio de referencia' must not fire."""
    log_messages = []

    motor = llm_engine.MotorVocalIA(queue.Queue(), lambda e: None)
    motor.is_ready = True
    motor.motor_tts = "pesado"
    motor.voz_referencia = None
    motor._log = lambda msg, **kw: log_messages.append(msg)
    motor._ejecutar_inferencia = lambda payload, source, history_text=None: None

    motor._dispatch_command("process_context", "hello world")

    assert not any("Falta audio de referencia" in m for m in log_messages), (
        "Old hard-block log message still present"
    )


def test_productor_no_ref_falls_back_to_ligero_with_missing_reference_reason(tmp_path):
    """Synthesis producer must fall back to ligero with reason=missing_reference when no reference audio."""
    from unittest.mock import patch, MagicMock

    log_messages = []

    motor = llm_engine.MotorVocalIA(queue.Queue(), lambda e: None)
    motor.is_ready = True
    motor.motor_tts = "pesado"
    motor.voz_referencia = None
    motor.health_monitor = None  # no health monitor — isolates missing_reference path

    original_log = motor._log
    motor._log = lambda msg, **kw: log_messages.append(msg)

    # Run _hablar with a valid text; patch Edge-TTS asyncio.run to succeed so
    # the producer can complete without a real network call.
    import asyncio as _asyncio

    def fake_asyncio_run(coro, *args, **kwargs):
        try:
            coro.close()
        except Exception:
            pass
        # Write a stub mp3 so the chunk path exists
        import os, uuid
        from opencohost.config.settings import TEMP_DIR
        stub = os.path.join(TEMP_DIR, f"tts_stub_{uuid.uuid4().hex[:4]}.mp3")
        open(stub, "wb").close()

    motor.pygame = MagicMock()
    motor.pygame.mixer.music.load = MagicMock()
    motor.pygame.mixer.music.play = MagicMock()
    motor.pygame.mixer.music.get_busy = MagicMock(return_value=False)
    motor.pygame.mixer.music.unload = MagicMock()

    with patch("opencohost.core.llm_engine.asyncio") as mock_asyncio:
        mock_asyncio.run.side_effect = fake_asyncio_run
        import asyncio as real_asyncio
        mock_asyncio.wait_for = real_asyncio.wait_for
        mock_asyncio.TimeoutError = real_asyncio.TimeoutError
        motor._hablar(
            "Texto suficientemente largo para producir al menos un chunk de audio.",
            source="direct",
        )

    fallback_logs = [m for m in log_messages if "missing_reference" in m]
    assert fallback_logs, (
        "Expected 'Auto-fallback to Edge-TTS: requested=pesado effective=ligero "
        "reason=missing_reference' in logs, got: " + repr(log_messages)
    )
    assert "Auto-fallback to Edge-TTS: requested=pesado effective=ligero reason=missing_reference" in fallback_logs[0]


def test_productor_with_ref_does_not_trigger_missing_reference_fallback(tmp_path):
    """When reference audio is present, missing_reference fallback must NOT appear in logs."""
    from unittest.mock import patch, MagicMock

    log_messages = []

    ref = tmp_path / "voice.wav"
    ref.write_bytes(b"ref")

    motor = llm_engine.MotorVocalIA(queue.Queue(), lambda e: None)
    motor.is_ready = True
    motor.motor_tts = "pesado"
    motor.voz_referencia = str(ref)
    motor.health_monitor = None

    motor._log = lambda msg, **kw: log_messages.append(msg)
    motor.pygame = MagicMock()
    motor.pygame.mixer.music.load = MagicMock()
    motor.pygame.mixer.music.play = MagicMock()
    motor.pygame.mixer.music.get_busy = MagicMock(return_value=False)
    motor.pygame.mixer.music.unload = MagicMock()

    # Patch requests.post so the heavy TTS path returns a valid wav
    original_post = llm_engine.requests.post
    try:
        llm_engine.requests.post = MagicMock(
            return_value=MagicMock(status_code=200, content=b"wav")
        )
        motor._hablar(
            "Texto suficientemente largo para producir al menos un chunk de audio.",
            source="direct",
        )
    finally:
        llm_engine.requests.post = original_post

    assert not any("missing_reference" in m for m in log_messages), (
        "missing_reference fallback triggered despite reference being present"
    )


def test_productor_no_ref_with_passing_health_gate_logs_single_coherent_fallback():
    """With a passing health gate and no reference audio, logs must not claim effective=pesado before falling back."""
    from unittest.mock import patch, MagicMock

    log_messages = []

    motor = llm_engine.MotorVocalIA(queue.Queue(), lambda e: None)
    motor.is_ready = True
    motor.motor_tts = "pesado"
    motor.voz_referencia = None
    hm = MagicMock()
    hm.heavy_tts_block_reason = MagicMock(return_value=None)
    motor.health_monitor = hm

    motor._log = lambda msg, **kw: log_messages.append(msg)

    import asyncio as real_asyncio

    def fake_asyncio_run(coro, *args, **kwargs):
        try:
            coro.close()
        except Exception:
            pass

    motor.pygame = MagicMock()
    motor.pygame.mixer.music.load = MagicMock()
    motor.pygame.mixer.music.play = MagicMock()
    motor.pygame.mixer.music.get_busy = MagicMock(return_value=False)
    motor.pygame.mixer.music.unload = MagicMock()

    with patch("opencohost.core.llm_engine.asyncio") as mock_asyncio:
        mock_asyncio.run.side_effect = fake_asyncio_run
        mock_asyncio.wait_for = real_asyncio.wait_for
        mock_asyncio.TimeoutError = real_asyncio.TimeoutError
        motor._hablar(
            "Texto suficientemente largo para producir al menos un chunk de audio.",
            source="direct",
        )

    assert any("reason=missing_reference" in m for m in log_messages), (
        "Expected missing_reference fallback, got: " + repr(log_messages)
    )
    assert not any("effective=pesado" in m for m in log_messages), (
        "Log must not claim effective=pesado when the engine falls back, got: "
        + repr(log_messages)
    )


# ---------------------------------------------------------------------------
# llm_output_streaming_20260813 -- the streaming transport seam and its budgets
# (design.md section 4 "Watchdog redesign", section 9 decision 2).
# ---------------------------------------------------------------------------


def test_llm_streaming_flag_defaults_off():
    """The whole track's revert lever ships OFF: nothing consumes the seam yet."""
    assert settings.LLM_STREAMING_ENABLED is False


def test_stream_idle_timeout_clears_every_measured_legitimate_load():
    """`STREAM_IDLE_TIMEOUT_SECONDS` is a VERDICT threshold, not a patience knob:
    exceeding it routes to `_recover_from_stalled_inference` and rolls the model
    back, so any value below the worst LEGITIMATE first-chunk wait converts
    healthy behavior into a false model downgrade. The 2026-08-13 measurements
    (design.md section 9 decision 2) are the floor."""
    assert settings.STREAM_IDLE_TIMEOUT_SECONDS == 40
    # Every measured legitimate load must fit, worst one (qwopus) included.
    for measured_load_seconds in (15.073, 16.12, 24.36):
        assert settings.STREAM_IDLE_TIMEOUT_SECONDS > measured_load_seconds
    # The rejected values, kept as a regression guard against re-tuning down:
    # 5s and 10s sit BELOW the measured 15.07s cold load (measured-unsafe), and
    # 30s gives only 1.23x over qwopus.
    assert settings.STREAM_IDLE_TIMEOUT_SECONDS / 24.36 > 1.6


def test_stream_idle_probe_threshold_is_below_the_verdict_threshold():
    """The probe is log-only observability; it must fire well before the verdict
    threshold or it would never produce the distribution it exists to collect."""
    assert settings.STREAM_IDLE_PROBE_SECONDS == 10
    assert settings.STREAM_IDLE_PROBE_SECONDS < settings.STREAM_IDLE_TIMEOUT_SECONDS


class _FakeClock:
    """Deterministic monotonic clock -- the seam's only time source."""

    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def _install_fake_clock(monkeypatch):
    clock = _FakeClock()
    monkeypatch.setattr(llm_engine_models, "time", SimpleNamespace(monotonic=clock.monotonic))
    return clock


class _FakeStream:
    """Stand-in for the generator `ollama.Client.chat(stream=True)` returns.

    A plain item is yielded as-is; a callable item is invoked instead and its
    return value (if any) is yielded -- that is how a test advances the fake
    clock or raises a transport error at a chosen point mid-stream.
    `closed` records the socket-level abort the seam owes on every exit path.
    """

    def __init__(self, *items):
        self._items = items
        self.closed = False

    def __iter__(self):
        for item in self._items:
            if callable(item):
                produced = item()
                if produced is not None:
                    yield produced
            else:
                yield item

    def close(self):
        self.closed = True


def _tick(clock, seconds, chunk=None):
    """A stream item that burns `seconds` of wall clock, then yields `chunk`."""

    def advance_then_yield():
        clock.advance(seconds)
        return chunk

    return advance_then_yield


def _raises(exc):
    def blow_up():
        raise exc

    return blow_up


class _FakeOllamaModule:
    """Module-shaped double -- `_create_ollama_chat_client` reads `.Client`.

    Deliberately NOT a MagicMock: an auto-mocked `self.ollama.Client()` would
    make every client-selection assertion vacuous.
    """

    def __init__(self, chat_result):
        self.built_timeouts = []
        self.chat_calls = []
        self._chat_result = chat_result

    def Client(self, **kwargs):
        self.built_timeouts.append(kwargs.get("timeout"))
        client = MagicMock()
        client.chat.side_effect = self._chat
        return client

    def _chat(self, **kwargs):
        self.chat_calls.append(kwargs)
        result = self._chat_result
        return result() if callable(result) else result


def _streaming_motor(chat_result):
    motor = llm_engine.MotorVocalIA(queue.Queue(), lambda event: None)
    module = _FakeOllamaModule(chat_result)
    motor.ollama = module
    return motor, module


def test_streaming_seam_yields_chunks_in_order_and_closes_the_stream(monkeypatch):
    """The happy path: raw chunks pass through untouched, in order, and the
    stream is closed at the end -- closing the socket is the only real
    server-side abort for Ollama and is what frees the single runner slot."""
    _install_fake_clock(monkeypatch)
    stream = _FakeStream("hola", " mundo", ".")
    motor, module = _streaming_motor(stream)

    chunks = list(motor._ollama_chat_streaming(timeout=45.0, model="llama3", messages=[]))

    assert chunks == ["hola", " mundo", "."]
    assert stream.closed is True
    assert module.chat_calls == [{"stream": True, "model": "llama3", "messages": []}]


def test_streaming_seam_translates_a_mid_stream_read_timeout_into_the_watchdog_contract(monkeypatch):
    """A per-read (idle) transport timeout must surface as the SAME
    `TimeoutError('watchdog_timeout:...')` contract `_is_watchdog_timeout_error`
    keys on -- streaming bypasses `_call_with_watchdog`'s worker, where that
    translation lives today, so an unconverted httpx exception would propagate
    and silently kill automatic recovery. The budget in the string is the IDLE
    budget, not the total: the idle threshold is what fired."""
    _install_fake_clock(monkeypatch)
    stream = _FakeStream("primera", _raises(httpx.ReadTimeout("the read operation timed out")))
    motor, _module = _streaming_motor(stream)

    generation = motor._ollama_chat_streaming(timeout=180.0, model="qwopus", messages=[])
    assert next(generation) == "primera"

    with pytest.raises(TimeoutError) as excinfo:
        next(generation)

    assert str(excinfo.value) == f"watchdog_timeout:{settings.STREAM_IDLE_TIMEOUT_SECONDS:.2f}s"
    assert motor._is_watchdog_timeout_error(excinfo.value) is True
    assert stream.closed is True


def test_streaming_seam_translates_a_first_chunk_read_timeout_before_any_chunk(monkeypatch):
    """Same contract when the transport times out before a single chunk exists
    -- the 2026-06-17 shape: a hung model emits ZERO chunks."""
    _install_fake_clock(monkeypatch)
    motor, _module = _streaming_motor(_raises(httpx.ReadTimeout("no first chunk")))

    with pytest.raises(TimeoutError) as excinfo:
        list(motor._ollama_chat_streaming(timeout=45.0, model="qwopus", messages=[]))

    assert str(excinfo.value) == f"watchdog_timeout:{settings.STREAM_IDLE_TIMEOUT_SECONDS:.2f}s"
    assert motor._is_watchdog_timeout_error(excinfo.value) is True


def test_streaming_seam_enforces_the_total_wall_clock_cap_on_a_slow_but_not_idle_stream(monkeypatch):
    """A stream that keeps delivering just fast enough to never trip the idle
    budget must still be stopped by the total cap. This is the hard ceiling on
    runaway generation, which matters precisely because `num_predict` is popped
    for reasoning-classified models like gemma4 -- an idle-only watchdog would
    let an infinite decode run forever."""
    clock = _install_fake_clock(monkeypatch)
    gap = 20.0  # below STREAM_IDLE_TIMEOUT_SECONDS: no transport timeout fires
    assert gap < settings.STREAM_IDLE_TIMEOUT_SECONDS
    stream = _FakeStream(
        _tick(clock, gap, "uno"),
        _tick(clock, gap, "dos"),
        _tick(clock, gap, "tres"),
    )
    motor, _module = _streaming_motor(stream)

    seen = []
    with pytest.raises(TimeoutError) as excinfo:
        for chunk in motor._ollama_chat_streaming(timeout=45.0, model="gemma4:e4b", messages=[]):
            seen.append(chunk)

    assert seen == ["uno", "dos"]  # 20s and 40s are inside the 45s cap; 60s is not
    assert str(excinfo.value) == "watchdog_timeout:45.00s"
    assert motor._is_watchdog_timeout_error(excinfo.value) is True
    assert stream.closed is True


def test_streaming_seam_closes_the_stream_when_the_consumer_abandons_it(monkeypatch):
    """The consumer aborting the turn early (guard trip, cancel token) must
    still free the runner. Abandonment raises GeneratorExit at the yield, so
    the close has to sit in a `finally`, not after the loop."""
    _install_fake_clock(monkeypatch)
    stream = _FakeStream("uno", "dos", "tres")
    motor, _module = _streaming_motor(stream)

    generation = motor._ollama_chat_streaming(timeout=45.0, model="llama3", messages=[])
    assert next(generation) == "uno"
    assert stream.closed is False

    generation.close()

    assert stream.closed is True


def test_streaming_seam_propagates_a_non_timeout_error_unchanged_and_still_closes(monkeypatch):
    """A genuine (non-timeout) transport error must NOT be laundered into a fake
    watchdog timeout -- that would swallow real errors and trigger a bogus
    rollback -- but the stream still has to be closed on the way out."""
    _install_fake_clock(monkeypatch)
    stream = _FakeStream("uno", _raises(ValueError("not a timeout at all")))
    motor, _module = _streaming_motor(stream)

    with pytest.raises(ValueError, match="not a timeout at all"):
        list(motor._ollama_chat_streaming(timeout=45.0, model="llama3", messages=[]))

    assert stream.closed is True


def test_streaming_seam_builds_and_memoizes_the_client_at_the_idle_budget(monkeypatch):
    """The seam rides the production-validated memoized-client machinery
    (`a8830bb`) at the IDLE budget -- that transport timeout IS the idle
    enforcement. `run()` pre-warms only the 180s and 45s clients, so the seam
    must build once and reuse, never fall through to the steady-state client
    (which would leave the idle budget unenforced at the transport)."""
    _install_fake_clock(monkeypatch)
    streams = [_FakeStream("a"), _FakeStream("b")]
    motor, module = _streaming_motor(lambda: streams.pop(0))
    steady_state_client = MagicMock()
    motor._ollama_chat_client = steady_state_client

    assert list(motor._ollama_chat_streaming(timeout=180.0, model="llama3", messages=[])) == ["a"]
    assert list(motor._ollama_chat_streaming(timeout=180.0, model="llama3", messages=[])) == ["b"]

    assert module.built_timeouts == [float(settings.STREAM_IDLE_TIMEOUT_SECONDS)]
    assert float(settings.STREAM_IDLE_TIMEOUT_SECONDS) in motor._ollama_chat_clients
    steady_state_client.chat.assert_not_called()


def test_stream_idle_probe_warns_once_on_a_slow_first_chunk(monkeypatch, caplog):
    """Log-only observability, measured AFTER the fact on the calling thread --
    no timer thread, so zero false-positive risk. It reports the first-chunk
    wait only, once per stream, and never aborts anything."""
    clock = _install_fake_clock(monkeypatch)
    slow_first = 12.0
    assert settings.STREAM_IDLE_PROBE_SECONDS < slow_first < settings.STREAM_IDLE_TIMEOUT_SECONDS
    stream = _FakeStream(
        _tick(clock, slow_first, "uno"),
        _tick(clock, 11.0, "dos"),
        _tick(clock, 11.0, "tres"),
    )
    motor, _module = _streaming_motor(stream)

    with caplog.at_level(logging.WARNING, logger="OpenCohost"):
        chunks = list(motor._ollama_chat_streaming(timeout=45.0, model="qwopus", messages=[]))

    assert chunks == ["uno", "dos", "tres"]  # log-only: nothing was aborted
    probes = [r for r in caplog.records if "[STREAM_IDLE_PROBE]" in r.getMessage()]
    assert len(probes) == 1
    assert "12.00" in probes[0].getMessage()
    assert probes[0].levelno == logging.WARNING


def test_stream_idle_probe_stays_silent_below_the_threshold(monkeypatch, caplog):
    clock = _install_fake_clock(monkeypatch)
    stream = _FakeStream(_tick(clock, 3.0, "uno"), _tick(clock, 30.0, "dos"))
    motor, _module = _streaming_motor(stream)

    with caplog.at_level(logging.DEBUG, logger="OpenCohost"):
        list(motor._ollama_chat_streaming(timeout=45.0, model="llama3", messages=[]))

    assert not [r for r in caplog.records if "[STREAM_IDLE_PROBE]" in r.getMessage()]


def test_streaming_seam_never_logs_response_text(monkeypatch, caplog):
    """Repo rule: raw dialogue never reaches the logs. The probe line is
    metadata only."""
    clock = _install_fake_clock(monkeypatch)
    secret = "cardumen-de-lubinas-42"
    stream = _FakeStream(_tick(clock, 12.0, secret), _tick(clock, 12.0, secret.upper()))
    motor, _module = _streaming_motor(stream)

    with caplog.at_level(logging.DEBUG, logger="OpenCohost"):
        list(motor._ollama_chat_streaming(timeout=45.0, model="llama3", messages=[]))

    assert [r for r in caplog.records if "[STREAM_IDLE_PROBE]" in r.getMessage()]
    for record in caplog.records:
        assert secret.lower() not in record.getMessage().lower()
    for line in list(motor.log_queue.queue):
        assert secret.lower() not in str(line).lower()
