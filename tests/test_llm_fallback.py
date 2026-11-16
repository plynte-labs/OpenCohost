"""Cloud failure fallback state machine — multi_provider_llm_20260723 Phase 4.

Pins spec C / design 'Cloud Failure Flow': a cloud watchdog-timeout failure is
routed to ``_handle_cloud_failure(source)`` instead of the local model-rollback
recovery (`_recover_from_stalled_inference`, which spec B forbids for cloud).

``fallback_mode=auto`` (default) sets the runtime-only ``_cloud_fallback_active``
flag, warms the last-known-good LOCAL model, speaks the canned
``llm.provider_fallback_notice`` i18n line through the existing ``_hablar`` TTS
path, and degrades every SUBSEQUENT generation to local — without ever
mutating the persisted ``active_provider`` selector (design 'Fallback switch
semantics'). ``fallback_mode=manual`` only surfaces the error
(``ui_callback("cloud_llm_error")``) and keeps routing to cloud.

Warm+speak run on a background daemon thread (mirrors
``play_prefetched_agenda``'s ``speaker()`` closure), so both are polled for
with ``_wait_until`` rather than asserted synchronously.
"""

import logging
import queue
import time
from unittest.mock import MagicMock, patch

from opencohost.config.settings import CLOUD_CHAT_TIMEOUT
from opencohost.i18n import active as i18n_active

_SEND = "opencohost.core.providers.cloud.cloud_llm_client.send_chat_completion"


def _make_motor(tmp_path, *, provider_config=None):
    """Construct a MotorVocalIA with ollama/pygame mocked and last_model isolated."""
    import opencohost.config.settings as settings
    import os

    original_last_model = settings.LAST_MODEL_FILE
    settings.LAST_MODEL_FILE = os.path.join(str(tmp_path), "last_model.json")

    log_queue = queue.Queue()
    ui_events = []

    try:
        from opencohost.core.llm_engine import MotorVocalIA
        motor = MotorVocalIA(log_queue, ui_events.append)
    finally:
        settings.LAST_MODEL_FILE = original_last_model

    motor.ollama = MagicMock()
    motor.ollama.chat = MagicMock(return_value={"message": {"content": "local", "thinking": ""}})
    motor.pygame = MagicMock()
    motor.is_ready = True
    motor._loaded_model = motor.current_model
    if provider_config is not None:
        motor._provider_config = provider_config
    return motor, log_queue, ui_events


def _cloud_config(fallback_mode="auto"):
    return {
        "active_provider": "openai",
        "fallback_mode": fallback_mode,
        "pregen_enabled": False,
        "profiles": {
            "openai": {"base_url": "https://api.example.com/v1", "model": "gpt-cloud"},
        },
    }


def _wait_until(predicate, *, timeout=1.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def _time_out_cloud_chat(motor):
    """Force the CLOUD chat attempt to fail as a watchdog timeout.

    F1 (interruptible_speech_architecture_20260804, runtime-findings
    2026-08-07) follows every cloud failure with a one-shot LOCAL retry at
    `_generar_dialogo`'s funnel when auto-fallback engages. This helper's own
    invariant is about the CLOUD attempt only, so the retry is left to fail
    fast with a plain (non-timeout, non-transport) error -- it must NOT
    itself look like a local watchdog timeout, which would correctly (but
    differently) route to `_recover_from_stalled_inference`, a separate
    invariant this helper does not pin. Every existing caller keeps seeing
    `result == ""`.
    """
    def side_effect(*, is_local, **_kwargs):
        if not is_local:
            raise TimeoutError(f"watchdog_timeout:{CLOUD_CHAT_TIMEOUT:.2f}s")
        raise RuntimeError("local retry not exercised by this helper")

    motor._ollama_chat_with_watchdog = MagicMock(side_effect=side_effect)


def _transport_fail_cloud_chat(motor, exc=None):
    """Force the cloud chat attempt to fail as a TRANSPORT error (surviving
    retry) — e.g. a 401 bad key / DNS / 5xx surfaced as a CloudLLMResponseError
    (a ``requests.RequestException`` subclass), NOT a watchdog timeout."""
    from opencohost.core.providers.cloud import cloud_llm_client

    err = exc if exc is not None else cloud_llm_client.CloudLLMResponseError("HTTP 401 bad key")
    motor._ollama_chat_with_watchdog = MagicMock(side_effect=err)


# ---------------------------------------------------------------------------
# 1. Auto mode
# ---------------------------------------------------------------------------

class TestCloudFailureAutoMode:
    def test_sets_flag_warms_local_and_speaks_notice(self, tmp_path):
        motor, _, _ = _make_motor(tmp_path, provider_config=_cloud_config())
        motor._prepare_model = MagicMock(return_value=True)
        motor._hablar = MagicMock()
        motor._recover_from_stalled_inference = MagicMock()
        _time_out_cloud_chat(motor)

        result = motor._generar_dialogo("hola", source="direct", commit_history=False)

        assert result == ""
        assert motor._cloud_fallback_active is True
        motor._recover_from_stalled_inference.assert_not_called()
        assert _wait_until(lambda: motor._prepare_model.called)
        assert _wait_until(lambda: motor._hablar.called)
        motor._hablar.assert_called_once_with(
            i18n_active.provider_fallback_notice(), source="direct"
        )

    def test_persisted_selector_untouched(self, tmp_path):
        motor, _, _ = _make_motor(tmp_path, provider_config=_cloud_config())
        motor._prepare_model = MagicMock(return_value=True)
        motor._hablar = MagicMock()
        _time_out_cloud_chat(motor)

        motor._generar_dialogo("hola", source="direct", commit_history=False)

        assert motor._provider_config["active_provider"] == "openai"

    def test_warms_last_known_good_local_model(self, tmp_path):
        motor, _, _ = _make_motor(tmp_path, provider_config=_cloud_config())
        motor._last_known_good_model = "qwen3:1.7b"
        motor._prepare_model = MagicMock(return_value=True)
        motor._hablar = MagicMock()
        _time_out_cloud_chat(motor)

        motor._generar_dialogo("hola", source="direct", commit_history=False)

        assert _wait_until(lambda: motor._prepare_model.called)
        motor._prepare_model.assert_called_once_with("qwen3:1.7b")

    def test_next_generation_runs_local(self, tmp_path):
        motor, _, _ = _make_motor(tmp_path, provider_config=_cloud_config())
        motor._prepare_model = MagicMock(return_value=True)
        motor._hablar = MagicMock()
        _time_out_cloud_chat(motor)

        motor._generar_dialogo("hola", source="direct", commit_history=False)
        assert motor._cloud_fallback_active is True

        # Restore the real watchdog-wrapped dispatch for the NEXT call.
        del motor._ollama_chat_with_watchdog
        with patch(_SEND) as mock_send:
            result = motor._generar_dialogo("otra vez", source="direct", commit_history=False)

        motor.ollama.chat.assert_called_once()
        mock_send.assert_not_called()
        assert result == "local"

    def test_notice_spoken_with_matching_source_tag(self, tmp_path):
        motor, _, _ = _make_motor(tmp_path, provider_config=_cloud_config())
        motor._prepare_model = MagicMock(return_value=True)
        motor._hablar = MagicMock()
        _time_out_cloud_chat(motor)

        motor._generar_dialogo("hola", source="chat", commit_history=False)

        assert _wait_until(lambda: motor._hablar.called)
        assert motor._hablar.call_args.kwargs.get("source") == "chat"

    def test_no_crash_when_notice_slot_missing(self, tmp_path, monkeypatch):
        """The i18n slot fails closed (SlotNotFound); the engine must not crash."""
        motor, _, _ = _make_motor(tmp_path, provider_config=_cloud_config())
        motor._prepare_model = MagicMock(return_value=True)
        monkeypatch.setattr(
            i18n_active, "provider_fallback_notice",
            MagicMock(side_effect=Exception("SlotNotFound")),
        )
        _time_out_cloud_chat(motor)

        result = motor._generar_dialogo("hola", source="direct", commit_history=False)

        assert result == ""
        assert _wait_until(lambda: motor._prepare_model.called)


# ---------------------------------------------------------------------------
# 2. Manual mode
# ---------------------------------------------------------------------------

class TestCloudFailureManualMode:
    def test_surfaces_error_and_stays_cloud(self, tmp_path):
        motor, _, ui_events = _make_motor(tmp_path, provider_config=_cloud_config(fallback_mode="manual"))
        motor._prepare_model = MagicMock(return_value=True)
        motor._hablar = MagicMock()
        _time_out_cloud_chat(motor)

        result = motor._generar_dialogo("hola", source="direct", commit_history=False)

        assert result == ""
        assert motor._cloud_fallback_active is False
        assert "cloud_llm_error" in ui_events
        assert motor._is_local is False
        # give any (unexpected) background worker a chance, then assert absence
        time.sleep(0.05)
        motor._prepare_model.assert_not_called()
        motor._hablar.assert_not_called()


# ---------------------------------------------------------------------------
# 3. Flag reset semantics
# ---------------------------------------------------------------------------

class TestCloudFallbackFlagReset:
    def test_flag_stays_on_same_provider_put(self, tmp_path):
        """F5: an unrelated PUT that does NOT change active_provider (e.g. a
        pregen toggle) must NOT clear the fallback flag — clearing it would
        silently un-fallback into a still-dead cloud."""
        motor, _, _ = _make_motor(tmp_path, provider_config=_cloud_config())
        motor._cloud_fallback_active = True

        new_cfg = _cloud_config()  # same active_provider="openai"
        new_cfg["pregen_enabled"] = True  # unrelated field flipped
        motor.set_provider_config(new_cfg)

        assert motor._cloud_fallback_active is True

    def test_flag_clears_on_active_provider_change(self, tmp_path):
        """F5: an explicit active_provider change is the operator's intent to
        retry a (possibly different) provider — clear the flag then."""
        motor, _, _ = _make_motor(tmp_path, provider_config=_cloud_config())
        motor._cloud_fallback_active = True

        new_cfg = _cloud_config()
        new_cfg["active_provider"] = "local"
        motor.set_provider_config(new_cfg)

        assert motor._cloud_fallback_active is False

    def test_flag_defaults_false_on_a_fresh_engine(self, tmp_path):
        motor, _, _ = _make_motor(tmp_path)
        assert motor._cloud_fallback_active is False


# ---------------------------------------------------------------------------
# 5. Cloud TRANSPORT failure engages the same fallback (F1)
# ---------------------------------------------------------------------------

class TestCloudTransportFailure:
    def test_auto_transport_error_engages_fallback(self, tmp_path):
        """A cloud transport error surviving retry (401/DNS/5xx) must engage
        the SAME auto-fallback as a cloud timeout (spec C: fallback on cloud
        timeout OR a non-2xx/connection error)."""
        motor, _, _ = _make_motor(tmp_path, provider_config=_cloud_config())
        motor._prepare_model = MagicMock(return_value=True)
        motor._hablar = MagicMock()
        motor._recover_from_stalled_inference = MagicMock()
        _transport_fail_cloud_chat(motor)

        result = motor._generar_dialogo("hola", source="direct", commit_history=False)

        assert result == ""
        assert motor._cloud_fallback_active is True
        motor._recover_from_stalled_inference.assert_not_called()
        assert _wait_until(lambda: motor._prepare_model.called)

    def test_manual_transport_error_fires_callback_stays_cloud(self, tmp_path):
        motor, _, ui_events = _make_motor(
            tmp_path, provider_config=_cloud_config(fallback_mode="manual")
        )
        motor._prepare_model = MagicMock(return_value=True)
        motor._hablar = MagicMock()
        _transport_fail_cloud_chat(motor)

        result = motor._generar_dialogo("hola", source="direct", commit_history=False)

        assert result == ""
        assert motor._cloud_fallback_active is False
        assert "cloud_llm_error" in ui_events
        assert motor._is_local is False

    def test_local_transport_error_does_not_engage_fallback(self, tmp_path):
        """Spec B: a LOCAL transport error must NEVER route to cloud fallback —
        the local path stays byte-identical."""
        import requests

        motor, _, _ = _make_motor(
            tmp_path, provider_config={"active_provider": "local", "profiles": {}}
        )
        motor._handle_cloud_failure = MagicMock()
        motor._ollama_chat_with_watchdog = MagicMock(
            side_effect=requests.exceptions.ConnectionError("ollama down")
        )

        result = motor._generar_dialogo("hola", source="direct", commit_history=False)

        assert result == ""
        assert motor._cloud_fallback_active is False
        motor._handle_cloud_failure.assert_not_called()


# ---------------------------------------------------------------------------
# 6. Warm-up result gates the false-success notice (F3)
# ---------------------------------------------------------------------------

class TestCloudFallbackWarmResult:
    def test_warm_failure_clears_flag_and_surfaces_error(self, tmp_path):
        """A cloud user without a working local Ollama: the warm-up returns
        False, so the flag set optimistically must be CLEARED, the false
        'switching to local' notice must NOT be spoken, and the operator must
        be told neither backend works (cloud_llm_error)."""
        motor, _, ui_events = _make_motor(tmp_path, provider_config=_cloud_config())
        motor._prepare_model = MagicMock(return_value=False)  # no local backend
        motor._hablar = MagicMock()
        _time_out_cloud_chat(motor)

        result = motor._generar_dialogo("hola", source="direct", commit_history=False)

        assert result == ""
        assert _wait_until(lambda: motor._cloud_fallback_active is False)
        assert _wait_until(lambda: "cloud_llm_error" in ui_events)
        time.sleep(0.05)  # give any (unexpected) speak a chance, then assert absence
        motor._hablar.assert_not_called()

    def test_warm_success_keeps_flag_and_speaks(self, tmp_path):
        motor, _, ui_events = _make_motor(tmp_path, provider_config=_cloud_config())
        motor._prepare_model = MagicMock(return_value=True)
        motor._hablar = MagicMock()
        _time_out_cloud_chat(motor)

        motor._generar_dialogo("hola", source="direct", commit_history=False)

        assert _wait_until(lambda: motor._hablar.called)
        assert motor._cloud_fallback_active is True
        assert "cloud_llm_error" not in ui_events


# ---------------------------------------------------------------------------
# 4. Cloud timeout independence
# ---------------------------------------------------------------------------

class TestCloudTimeoutIndependence:
    def test_cloud_timeout_constant_differs_from_local_watchdogs(self, tmp_path):
        motor, _, _ = _make_motor(tmp_path, provider_config=_cloud_config())
        assert motor._resolve_chat_watchdog_timeout("anything") == CLOUD_CHAT_TIMEOUT
        assert CLOUD_CHAT_TIMEOUT != motor._inference_watchdog_timeout
        assert CLOUD_CHAT_TIMEOUT != motor._post_switch_watchdog_timeout


# ---------------------------------------------------------------------------
# 7. Cloud final-empty-response surfacing (2026-07-24 cloud incident item 4)
# ---------------------------------------------------------------------------

def _empty_response():
    return {"message": {"content": "", "thinking": ""}}


class TestCloudFinalEmptyResponse:
    """A cloud turn whose FINAL empty return (both attempts empty, no
    thinking -- so no Layer-2 self-heal retry either) used to be silent dead
    air: no callback, no fallback. It now fires ui_callback("cloud_llm_error")
    and routes to _handle_cloud_failure(source), same contract as transport
    failures (F1) -- but ONLY on the final exhausted-retries return, never on
    an intermediate empty attempt that later recovers."""

    def test_final_empty_fires_callback_and_handles_failure(self, tmp_path):
        motor, _, ui_events = _make_motor(tmp_path, provider_config=_cloud_config())
        motor._handle_cloud_failure = MagicMock()
        motor._ollama_chat_with_watchdog = MagicMock(return_value=_empty_response())

        result = motor._generar_dialogo("hola", source="direct", commit_history=False)

        assert result == ""
        assert "cloud_llm_error" in ui_events
        motor._handle_cloud_failure.assert_called_once()
        assert motor._handle_cloud_failure.call_args.args == ("direct",)

    def test_empty_then_recovered_does_not_fire(self, tmp_path):
        motor, _, ui_events = _make_motor(tmp_path, provider_config=_cloud_config())
        motor._handle_cloud_failure = MagicMock()
        motor._ollama_chat_with_watchdog = MagicMock(
            side_effect=[_empty_response(), {"message": {"content": "hola cloud", "thinking": ""}}]
        )

        result = motor._generar_dialogo("hola", source="direct", commit_history=False)

        assert result == "hola cloud"
        assert "cloud_llm_error" not in ui_events
        motor._handle_cloud_failure.assert_not_called()

    def test_local_final_empty_unchanged(self, tmp_path):
        motor, _, ui_events = _make_motor(
            tmp_path, provider_config={"active_provider": "local", "profiles": {}}
        )
        motor._handle_cloud_failure = MagicMock()
        motor._ollama_chat_with_watchdog = MagicMock(return_value=_empty_response())

        result = motor._generar_dialogo("hola", source="direct", commit_history=False)

        assert result == ""
        assert "cloud_llm_error" not in ui_events
        motor._handle_cloud_failure.assert_not_called()


# ---------------------------------------------------------------------------
# 8. Guardrail retry-with-nudge (owner decision "afinar + reintento",
#    guardrail_tuning_20260724): ONE extra generation, same prompt + a
#    corrective system nudge, after an output_guard block -- before the
#    canned fallback line. NEVER consumes the max_intentos transport-retry
#    budget above (a separate, single `_ollama_chat_with_watchdog` call).
# ---------------------------------------------------------------------------

def _content_response(text):
    return {"message": {"content": text, "thinking": ""}}


_LOCAL_CONFIG = {"active_provider": "local", "profiles": {}}
_BLOCKED_TEXT = "Sin duda vas a ganar el premio."
_CLEAN_TEXT = "Gracias por la pregunta, vamos a verlo con calma."


class TestGuardrailRetryWithNudge:
    def test_no_retry_when_first_response_passes(self, tmp_path):
        motor, _, _ = _make_motor(tmp_path, provider_config=_LOCAL_CONFIG)
        motor._ollama_chat_with_watchdog = MagicMock(
            return_value=_content_response(_CLEAN_TEXT)
        )

        result = motor._generar_dialogo("hola", source="direct", commit_history=False)

        assert result == _CLEAN_TEXT
        assert motor._ollama_chat_with_watchdog.call_count == 1

    def test_local_block_then_clean_retry_is_spoken(self, tmp_path):
        motor, _, _ = _make_motor(tmp_path, provider_config=_LOCAL_CONFIG)
        motor._ollama_chat_with_watchdog = MagicMock(
            side_effect=[
                _content_response(_BLOCKED_TEXT),
                _content_response(_CLEAN_TEXT),
            ]
        )

        result = motor._generar_dialogo("hola", source="direct", commit_history=False)

        assert result == _CLEAN_TEXT
        assert motor._ollama_chat_with_watchdog.call_count == 2

    def test_local_block_then_blocked_retry_returns_canned_line(self, tmp_path):
        motor, _, _ = _make_motor(tmp_path, provider_config=_LOCAL_CONFIG)
        motor._ollama_chat_with_watchdog = MagicMock(
            side_effect=[
                _content_response(_BLOCKED_TEXT),
                _content_response(_BLOCKED_TEXT),
            ]
        )

        result = motor._generar_dialogo("hola", source="direct", commit_history=False)

        assert result in i18n_active.guardrail_fallback_lines()
        assert motor._ollama_chat_with_watchdog.call_count == 2

    def test_cloud_block_then_clean_retry_is_spoken(self, tmp_path):
        motor, _, _ = _make_motor(tmp_path, provider_config=_cloud_config())
        motor._ollama_chat_with_watchdog = MagicMock(
            side_effect=[
                _content_response(_BLOCKED_TEXT),
                _content_response(_CLEAN_TEXT),
            ]
        )

        result = motor._generar_dialogo("hola", source="direct", commit_history=False)

        assert result == _CLEAN_TEXT
        assert motor._ollama_chat_with_watchdog.call_count == 2

    def test_cloud_block_then_blocked_retry_returns_canned_line(self, tmp_path):
        motor, _, _ = _make_motor(tmp_path, provider_config=_cloud_config())
        motor._ollama_chat_with_watchdog = MagicMock(
            side_effect=[
                _content_response(_BLOCKED_TEXT),
                _content_response(_BLOCKED_TEXT),
            ]
        )

        result = motor._generar_dialogo("hola", source="direct", commit_history=False)

        assert result in i18n_active.guardrail_fallback_lines()
        assert motor._ollama_chat_with_watchdog.call_count == 2

    def test_retry_transport_error_falls_back_to_canned_line(self, tmp_path):
        """The retry attempt itself failing (transport error/timeout) must
        fall through to the existing canned-line fallback, never crash."""
        motor, _, _ = _make_motor(tmp_path, provider_config=_LOCAL_CONFIG)
        motor._ollama_chat_with_watchdog = MagicMock(
            side_effect=[
                _content_response(_BLOCKED_TEXT),
                TimeoutError("watchdog_timeout:1.00s"),
            ]
        )

        result = motor._generar_dialogo("hola", source="direct", commit_history=False)

        assert result in i18n_active.guardrail_fallback_lines()
        assert motor._ollama_chat_with_watchdog.call_count == 2

    def test_retry_uses_separate_call_never_touching_max_intentos_budget(self, tmp_path):
        """Exactly 2 total generation calls for a block+retry-blocked turn --
        proves the retry is a SEPARATE single call, not a 3rd max_intentos
        transport-retry iteration (max_intentos caps at 2 on its own)."""
        motor, _, _ = _make_motor(tmp_path, provider_config=_LOCAL_CONFIG)
        motor._ollama_chat_with_watchdog = MagicMock(
            side_effect=[
                _content_response(_BLOCKED_TEXT),
                _content_response(_BLOCKED_TEXT),
            ]
        )

        motor._generar_dialogo("hola", source="direct", commit_history=False)

        assert motor._ollama_chat_with_watchdog.call_count == 2


# ---------------------------------------------------------------------------
# 9. Guardrail vs. TTS-sanitizer ordering leak (P0). output_guard runs on the
#    RAW LLM text, but _sanitize_tts_text_for_playback strips markdown
#    emphasis (`*x*`) LATER, only inside _hablar_impl. R9/R4's patterns join
#    tokens with `\s+`, and `*` is not whitespace, so a markdown-wrapped
#    violation ('Como *IA*, no puedo opinar.') passes the raw-text guard and
#    is then spoken clean on air once the sanitizer strips the asterisks.
#    Fix: guard BOTH the raw text and the TTS-sanitized copy, at the initial
#    check and at the retry re-check a few lines below it.
# ---------------------------------------------------------------------------

_MARKDOWN_R9_TEXT = "Como *IA*, no puedo opinar."
_MARKDOWN_R4_TEXT = "Listo, *ya lo hice* sin problema."
_MARKDOWN_CLEAN_TEXT = "Eso estuvo *buenisimo*, che."


class TestGuardrailTtsSanitizedLeak:
    def test_markdown_wrapped_self_id_blocked_then_clean_retry_spoken(self, tmp_path):
        """R9 regression case (broadcast on production at 2026-08-13 16:10:13
        for a raw-text hit; this is the markdown-wrapped bypass of that same
        rule). Pre-fix, output_guard(raw) allows this text outright, so the
        retry never fires and the unsanitized markdown text is returned."""
        motor, _, _ = _make_motor(tmp_path, provider_config=_LOCAL_CONFIG)
        motor._ollama_chat_with_watchdog = MagicMock(
            side_effect=[
                _content_response(_MARKDOWN_R9_TEXT),
                _content_response(_CLEAN_TEXT),
            ]
        )

        result = motor._generar_dialogo("hola", source="direct", commit_history=False)

        assert result == _CLEAN_TEXT
        assert motor._ollama_chat_with_watchdog.call_count == 2

    def test_markdown_wrapped_confirmation_blocked_then_clean_retry_spoken(self, tmp_path):
        """R4 case: Kira claiming she did something she did not, wrapped in
        emphasis markers."""
        motor, _, _ = _make_motor(tmp_path, provider_config=_LOCAL_CONFIG)
        motor._ollama_chat_with_watchdog = MagicMock(
            side_effect=[
                _content_response(_MARKDOWN_R4_TEXT),
                _content_response(_CLEAN_TEXT),
            ]
        )

        result = motor._generar_dialogo("hola", source="direct", commit_history=False)

        assert result == _CLEAN_TEXT
        assert motor._ollama_chat_with_watchdog.call_count == 2

    def test_harmless_markdown_is_not_a_false_positive(self, tmp_path):
        """Guarding the sanitized copy must not become a markdown ban --
        emphasis around otherwise-clean content stays allowed, no retry."""
        motor, _, _ = _make_motor(tmp_path, provider_config=_LOCAL_CONFIG)
        motor._ollama_chat_with_watchdog = MagicMock(
            return_value=_content_response(_MARKDOWN_CLEAN_TEXT)
        )

        result = motor._generar_dialogo("hola", source="direct", commit_history=False)

        assert result == _MARKDOWN_CLEAN_TEXT
        assert motor._ollama_chat_with_watchdog.call_count == 1

    def test_raw_violation_no_markdown_still_blocks_unchanged(self, tmp_path):
        """No behavior change on the pre-existing raw-text-only path (same
        fixture as TestGuardrailRetryWithNudge.
        test_local_block_then_blocked_retry_returns_canned_line)."""
        motor, _, _ = _make_motor(tmp_path, provider_config=_LOCAL_CONFIG)
        motor._ollama_chat_with_watchdog = MagicMock(
            side_effect=[
                _content_response(_BLOCKED_TEXT),
                _content_response(_BLOCKED_TEXT),
            ]
        )

        result = motor._generar_dialogo("hola", source="direct", commit_history=False)

        assert result in i18n_active.guardrail_fallback_lines()
        assert motor._ollama_chat_with_watchdog.call_count == 2

    def test_retry_recheck_also_catches_markdown_wrapped_violation(self, tmp_path):
        """The retry re-check (a SEPARATE output_guard call a few lines below
        the initial one) must apply the same raw+sanitized check -- a retry
        that comes back markdown-wrapped must not slip through the hole the
        initial check just closed."""
        motor, _, _ = _make_motor(tmp_path, provider_config=_LOCAL_CONFIG)
        motor._ollama_chat_with_watchdog = MagicMock(
            side_effect=[
                _content_response(_BLOCKED_TEXT),      # raw-blocked, triggers retry
                _content_response(_MARKDOWN_R9_TEXT),  # retry: markdown-wrapped R9
            ]
        )

        result = motor._generar_dialogo("hola", source="direct", commit_history=False)

        assert result in i18n_active.guardrail_fallback_lines()
        assert motor._ollama_chat_with_watchdog.call_count == 2

    def test_blocked_markdown_turn_never_logs_dialogue_text(self, tmp_path, caplog):
        """Metadata-only logging (owner privacy rule, mirrors CLAUSE_SANITIZER
        and test_direct_bounded_wait.py:143-168): the guard call site's own
        log line (`_log`, the "OpenCohost" logger) must never put the raw or
        sanitized dialogue text in any log record. Scoped to "OpenCohost" --
        `output_guard`'s own `log_non_negotiable_block` preview logging (a
        separate, pre-existing, deliberate feature on the
        "opencohost.config.validation" logger, guardrail_tuning_20260724
        item 0) is untouched by this fix and out of scope."""
        motor, _, _ = _make_motor(tmp_path, provider_config=_LOCAL_CONFIG)
        motor._ollama_chat_with_watchdog = MagicMock(
            side_effect=[
                _content_response(_MARKDOWN_R9_TEXT),
                _content_response(_MARKDOWN_R9_TEXT),
            ]
        )

        with caplog.at_level(logging.WARNING, logger="OpenCohost"):
            result = motor._generar_dialogo("hola", source="direct", commit_history=False)

        assert result in i18n_active.guardrail_fallback_lines()
        for record in caplog.records:
            if record.name != "OpenCohost":
                continue
            message = record.getMessage()
            assert _MARKDOWN_R9_TEXT not in message
            assert "Como IA, no puedo opinar" not in message

    def test_sanitized_only_block_reason_is_distinguishable_in_log(self, tmp_path, caplog):
        """A block that only the sanitized pass caught must be legible as
        such in the log line -- otherwise the next person debugging this
        sees a rule fire on text that 'isn't in the response' (raw passes)."""
        motor, _, _ = _make_motor(tmp_path, provider_config=_LOCAL_CONFIG)
        motor._ollama_chat_with_watchdog = MagicMock(
            side_effect=[
                _content_response(_MARKDOWN_R9_TEXT),
                _content_response(_CLEAN_TEXT),
            ]
        )

        with caplog.at_level(logging.WARNING, logger="OpenCohost"):
            motor._generar_dialogo("hola", source="direct", commit_history=False)

        blocked_lines = [
            r.getMessage() for r in caplog.records
            if "Salida bloqueada por guardrail" in r.getMessage()
        ]
        assert len(blocked_lines) == 1
        assert "no_ai_self_identification" in blocked_lines[0]
        # Distinguishable from a raw-text block, which carries no such marker.
        assert "tts-sanitized" in blocked_lines[0]


# ---------------------------------------------------------------------------
# 10. Guardrail retry message shape (qwen-runtime-fixes-20260928 T2). Qwen3.5's
#    chat template raises HTTP 400 "System message must be at the beginning"
#    on a trailing system message; other families silently drop it. The nudge
#    must land on the LAST user message's content instead, on a COPIED
#    list/dict (never mutating the caller's `messages`), and it must be
#    chosen per blocked rule rather than always the promise-specific text.
# ---------------------------------------------------------------------------

_NEVER_PROMISE_REASON = (
    "Non-negotiable violation [never_promise]: response contains a promise "
    "or guarantee"
)
_NO_AI_SELF_ID_REASON = (
    "Non-negotiable violation [no_ai_self_identification]: response "
    "contains AI self-identification"
)
_NO_DOXXING_REASON = (
    "Non-negotiable violation [no_doxxing]: response contains personal data"
)


class TestGuardrailRetryMessageShape:
    def test_retry_has_no_trailing_system_message(self, tmp_path):
        motor, _, _ = _make_motor(tmp_path, provider_config=_LOCAL_CONFIG)
        captured = {}

        def _fake_watchdog(**kwargs):
            captured["messages"] = kwargs["messages"]
            return _content_response(_CLEAN_TEXT)

        motor._ollama_chat_with_watchdog = _fake_watchdog

        original_messages = [
            {"role": "system", "content": "persona"},
            {"role": "user", "content": "hola"},
        ]
        result = motor._retry_after_guard_block(
            messages=original_messages,
            opciones_llm={},
            request_model="qwen35",
            chat_timeout=5.0,
            provider_cfg=_LOCAL_CONFIG,
            is_local=True,
            guard_reason=_NEVER_PROMISE_REASON,
        )

        assert result == _CLEAN_TEXT
        retry_messages = captured["messages"]
        system_indices = [i for i, m in enumerate(retry_messages) if m.get("role") == "system"]
        # Only allowed to be at index 0 (or absent) -- never trailing.
        assert system_indices in ([], [0])
        assert retry_messages[-1]["role"] == "user"
        assert retry_messages[-1]["content"] != "hola"
        assert "hola" in retry_messages[-1]["content"]

    def test_retry_never_introduces_system_message_when_none_present(self, tmp_path):
        """`use_system_role=False` path: no system message at all in the
        original list -- the nudge must not introduce one."""
        motor, _, _ = _make_motor(tmp_path, provider_config=_LOCAL_CONFIG)
        captured = {}

        def _fake_watchdog(**kwargs):
            captured["messages"] = kwargs["messages"]
            return _content_response(_CLEAN_TEXT)

        motor._ollama_chat_with_watchdog = _fake_watchdog

        original_messages = [{"role": "user", "content": "hola, contame algo"}]
        motor._retry_after_guard_block(
            messages=original_messages,
            opciones_llm={},
            request_model="qwen35",
            chat_timeout=5.0,
            provider_cfg=_LOCAL_CONFIG,
            is_local=True,
            guard_reason=_NEVER_PROMISE_REASON,
        )

        retry_messages = captured["messages"]
        assert all(m.get("role") != "system" for m in retry_messages)

    def test_retry_never_mutates_caller_messages(self, tmp_path):
        motor, _, _ = _make_motor(tmp_path, provider_config=_LOCAL_CONFIG)
        motor._ollama_chat_with_watchdog = MagicMock(return_value=_content_response(_CLEAN_TEXT))

        original_messages = [
            {"role": "system", "content": "persona"},
            {"role": "user", "content": "hola"},
        ]
        snapshot = [dict(m) for m in original_messages]

        motor._retry_after_guard_block(
            messages=original_messages,
            opciones_llm={},
            request_model="qwen35",
            chat_timeout=5.0,
            provider_cfg=_LOCAL_CONFIG,
            is_local=True,
            guard_reason=_NEVER_PROMISE_REASON,
        )

        assert original_messages == snapshot


class TestGuardrailRetryPerRuleNudge:
    def _retry_and_capture_last_user_content(self, tmp_path, *, guard_reason):
        motor, _, _ = _make_motor(tmp_path, provider_config=_LOCAL_CONFIG)
        captured = {}

        def _fake_watchdog(**kwargs):
            captured["messages"] = kwargs["messages"]
            return _content_response(_CLEAN_TEXT)

        motor._ollama_chat_with_watchdog = _fake_watchdog

        kwargs = dict(
            messages=[{"role": "user", "content": "hola"}],
            opciones_llm={},
            request_model="m",
            chat_timeout=5.0,
            provider_cfg=_LOCAL_CONFIG,
            is_local=True,
        )
        if guard_reason is not None:
            kwargs["guard_reason"] = guard_reason
        motor._retry_after_guard_block(**kwargs)
        return captured["messages"][-1]["content"]

    def test_promise_rule_keeps_existing_promise_nudge_text(self, tmp_path):
        content = self._retry_and_capture_last_user_content(
            tmp_path, guard_reason=_NEVER_PROMISE_REASON,
        )
        assert "promet" in content and "garantiz" in content

    def test_no_ai_self_identification_rule_uses_dedicated_nudge(self, tmp_path):
        content = self._retry_and_capture_last_user_content(
            tmp_path, guard_reason=_NO_AI_SELF_ID_REASON,
        )
        assert "promet" not in content
        assert "disclaimer" in content.lower() or "modelo de lenguaje" in content.lower()

    def test_unknown_rule_uses_generic_fallback_nudge(self, tmp_path):
        content = self._retry_and_capture_last_user_content(
            tmp_path, guard_reason=_NO_DOXXING_REASON,
        )
        assert "promet" not in content
        assert "disclaimer" not in content.lower()
        assert "regla" in content.lower() or "no negociable" in content.lower()

    def test_missing_guard_reason_uses_generic_fallback(self, tmp_path):
        content = self._retry_and_capture_last_user_content(tmp_path, guard_reason=None)
        assert "promet" not in content

    # K4 (G6): the nudges come from the active locale manifest.
    def _content_under_locale(self, tmp_path, locale, guard_reason):
        from opencohost.i18n.registry import discover_bundles, official_locales_dir
        from opencohost.i18n.startup import resolve_active_bundle

        official = discover_bundles(official_locales_dir(), "official")
        i18n_active.set_active_bundle(resolve_active_bundle(locale=locale, registry=official))
        try:
            return self._retry_and_capture_last_user_content(
                tmp_path, guard_reason=guard_reason,
            )
        finally:
            i18n_active.reset_active_bundle()

    def test_en_locale_retry_uses_english_nudge_and_no_spanish(self, tmp_path):
        for guard_reason in (
            _NEVER_PROMISE_REASON, _NO_AI_SELF_ID_REASON, _NO_DOXXING_REASON, None,
        ):
            content = self._content_under_locale(tmp_path, "en", guard_reason)
            assert "Internal note" in content, guard_reason
            for spanish in ("Nota interna", "respuesta", "Respond", "mantené", "mantén", " tono"):
                assert spanish not in content, (guard_reason, spanish)

    def test_en_locale_nudges_are_rule_specific(self, tmp_path):
        promise = self._content_under_locale(tmp_path, "en", _NEVER_PROMISE_REASON)
        self_id = self._content_under_locale(tmp_path, "en", _NO_AI_SELF_ID_REASON)
        generic = self._content_under_locale(tmp_path, "en", _NO_DOXXING_REASON)
        assert "promise" in promise.lower()
        assert "disclaimer" in self_id.lower()
        assert "promise" not in generic.lower() and "disclaimer" not in generic.lower()

    def test_es_locale_retry_nudge_uses_tu_not_voseo(self, tmp_path):
        for guard_reason in (_NEVER_PROMISE_REASON, _NO_AI_SELF_ID_REASON, _NO_DOXXING_REASON):
            content = self._content_under_locale(tmp_path, "es", guard_reason)
            assert "Nota interna" in content, guard_reason
            for voseo in ("Respondé", "mantené", "podés", "corrés"):
                assert voseo not in content, (guard_reason, voseo)


class TestGuardrailRetryReasonThreadedFromCaller:
    def test_generar_dialogo_passes_guard_reason_to_retry(self, tmp_path):
        motor, _, _ = _make_motor(tmp_path, provider_config=_LOCAL_CONFIG)
        motor._ollama_chat_with_watchdog = MagicMock(
            side_effect=[
                _content_response(_BLOCKED_TEXT),
                _content_response(_CLEAN_TEXT),
            ]
        )
        motor._retry_after_guard_block = MagicMock(wraps=motor._retry_after_guard_block)

        motor._generar_dialogo("hola", source="direct", commit_history=False)

        _, kwargs = motor._retry_after_guard_block.call_args
        assert "guard_reason" in kwargs
        assert "never_promise" in kwargs["guard_reason"]


class TestGuardrailRetryCarriesTurnThink:
    """K2 (warmth-guardrails-runtime-20261006): the retry must request the same
    think setting as the main attempt, or gemma4 thinks by default on retry."""

    def test_retry_call_carries_the_turn_think_false(self, tmp_path):
        motor, _, _ = _make_motor(tmp_path, provider_config=_LOCAL_CONFIG)
        motor._ollama_chat_with_watchdog = MagicMock(
            side_effect=[
                _content_response(_BLOCKED_TEXT),
                _content_response(_CLEAN_TEXT),
            ]
        )

        motor._generar_dialogo("hola", source="direct", commit_history=False)

        first, retry = motor._ollama_chat_with_watchdog.call_args_list
        assert first.kwargs["options"].get("think") is False
        assert retry.kwargs["options"].get("think") is False

    def test_retry_does_not_mutate_caller_options(self, tmp_path):
        motor, _, _ = _make_motor(tmp_path, provider_config=_LOCAL_CONFIG)
        motor._ollama_chat_with_watchdog = MagicMock(return_value=_content_response(_CLEAN_TEXT))
        opts = {"num_predict": 50}

        motor._retry_after_guard_block(
            messages=[{"role": "user", "content": "hola"}],
            opciones_llm=opts,
            request_model="m",
            chat_timeout=5.0,
            provider_cfg=_LOCAL_CONFIG,
            is_local=True,
            think=False,
        )

        assert opts == {"num_predict": 50}
        assert motor._ollama_chat_with_watchdog.call_args.kwargs["options"]["think"] is False
