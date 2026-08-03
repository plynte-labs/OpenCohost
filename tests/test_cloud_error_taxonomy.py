"""Cloud error taxonomy (runtime_findings_batch_20260731 Batch 1 unit 1.1).

F2: `cloud_llm_client.CloudLLMResponseError` used to flatten every non-2xx
response into a string-only exception, discarding the status code and never
reading `resp.headers`. This pins:

- the exception now carries `status_code` / `headers` (bounded subset) /
  `body_excerpt` (bounded, classification-only, never logged);
- `classify_cloud_error` sorts a failure into exactly four classes --
  `bad_key` / `rate_limited` / `ambiguous_429` / `transient` -- reading only
  status_code/headers, never a guessed provider-specific body shape;
- `parse_retry_after_seconds` handles delta-seconds only (ponytail: no
  HTTP-date parsing -- see its docstring);
- the classification reaches `llm_engine._generar_dialogo`'s cloud-failure
  branch (`_last_cloud_failure_class`, a `clase=` log field) without leaking
  the body excerpt or headers into any log line.

No provider-specific error-code table: only NVIDIA NIM has been exercised.
`quota_exhausted` is deliberately NOT a class (owner ruling) -- an exhausted
quota is indistinguishable from a bare rate limit today and correctly lands
in `ambiguous_429`.
"""

import logging
import os
import queue
import sys
from unittest.mock import MagicMock, patch

import pytest
import requests

from opencohost.core.providers.cloud.cloud_llm_client import (
    CLOUD_ERROR_AMBIGUOUS_429,
    CLOUD_ERROR_BAD_KEY,
    CLOUD_ERROR_RATE_LIMITED,
    CLOUD_ERROR_TRANSIENT,
    CloudLLMResponseError,
    classify_cloud_error,
    parse_retry_after_seconds,
    send_chat_completion,
)

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)



_SEND = "opencohost.core.providers.cloud.cloud_llm_client.send_chat_completion"


# ──────────────────────────────────────────────────────────────────────────
# Fakes for driving send_chat_completion through the real HTTP-error path
# ──────────────────────────────────────────────────────────────────────────


class _FakeResponse:
    def __init__(self, status_code=200, json_data=None, text="", headers=None, raise_json_error=False):
        self.status_code = status_code
        self._json_data = json_data
        self.text = text
        self.reason = text or "Error"
        self.headers = headers or {}
        self._raise_json_error = raise_json_error

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"{self.status_code} error", response=self)

    def json(self):
        if self._raise_json_error:
            raise ValueError("not valid json")
        return self._json_data


def _post_returning(response, monkeypatch):
    monkeypatch.setattr(
        "opencohost.core.providers.cloud.cloud_llm_client.requests.post",
        lambda url, json=None, headers=None, timeout=None: response,
    )


def _send():
    return send_chat_completion(
        base_url="https://api.example.com/v1",
        api_key="sk-irrelevant",
        model="gpt-cloud",
        messages=[{"role": "user", "content": "hi"}],
        options={},
        timeout=30,
    )


# ──────────────────────────────────────────────────────────────────────────
# status_code / headers / body_excerpt survive on the exception
# ──────────────────────────────────────────────────────────────────────────


def test_http_error_carries_status_code_and_bounded_headers(monkeypatch):
    _post_returning(
        _FakeResponse(
            status_code=429,
            text="rate limited",
            headers={"Retry-After": "30", "X-Request-Id": "should-not-be-kept"},
        ),
        monkeypatch,
    )
    with pytest.raises(CloudLLMResponseError) as excinfo:
        _send()
    exc = excinfo.value
    assert exc.status_code == 429
    assert exc.headers == {"retry-after": "30"}  # bounded: X-Request-Id dropped
    assert exc.body_excerpt == "rate limited"


def test_body_excerpt_is_bounded_to_500_chars(monkeypatch):
    _post_returning(_FakeResponse(status_code=500, text="x" * 2000), monkeypatch)
    with pytest.raises(CloudLLMResponseError) as excinfo:
        _send()
    assert len(excinfo.value.body_excerpt) == 500


def test_malformed_json_on_2xx_carries_status_code_200(monkeypatch):
    _post_returning(_FakeResponse(status_code=200, raise_json_error=True, text="<html>"), monkeypatch)
    with pytest.raises(CloudLLMResponseError) as excinfo:
        _send()
    assert excinfo.value.status_code == 200


# ──────────────────────────────────────────────────────────────────────────
# classify_cloud_error — table-driven over the four classes
# ──────────────────────────────────────────────────────────────────────────


def _err(status_code=None, headers=None):
    return CloudLLMResponseError("boom", status_code=status_code, headers=headers)


@pytest.mark.parametrize(
    "exc, expected",
    [
        pytest.param(_err(401), CLOUD_ERROR_BAD_KEY, id="401-bad_key"),
        pytest.param(_err(403), CLOUD_ERROR_BAD_KEY, id="403-bad_key"),
        pytest.param(
            _err(429, {"retry-after": "30"}), CLOUD_ERROR_RATE_LIMITED, id="429-retry-after-rate_limited"
        ),
        pytest.param(
            _err(429, {"x-ratelimit-reset": "1700000000"}),
            CLOUD_ERROR_RATE_LIMITED,
            id="429-ratelimit-reset-rate_limited",
        ),
        pytest.param(
            _err(429, {"x-ratelimit-remaining": "0"}),
            CLOUD_ERROR_AMBIGUOUS_429,
            id="429-remaining-only-is-not-timing-info",
        ),
        pytest.param(_err(429), CLOUD_ERROR_AMBIGUOUS_429, id="429-bare-ambiguous"),
        pytest.param(_err(500), CLOUD_ERROR_TRANSIENT, id="500-transient"),
        pytest.param(_err(200), CLOUD_ERROR_TRANSIENT, id="malformed-2xx-transient"),
        pytest.param(
            requests.exceptions.ConnectionError("refused"), CLOUD_ERROR_TRANSIENT, id="connection-error-transient"
        ),
        pytest.param(requests.exceptions.Timeout("timed out"), CLOUD_ERROR_TRANSIENT, id="timeout-transient"),
        pytest.param(TimeoutError("watchdog"), CLOUD_ERROR_TRANSIENT, id="watchdog-timeout-transient"),
    ],
)
def test_classify_cloud_error(exc, expected):
    assert classify_cloud_error(exc) == expected


# ──────────────────────────────────────────────────────────────────────────
# parse_retry_after_seconds — delta-seconds only
# ──────────────────────────────────────────────────────────────────────────


def test_parse_retry_after_seconds_delta_seconds():
    assert parse_retry_after_seconds({"retry-after": "30"}) == 30


def test_parse_retry_after_seconds_absent_header():
    assert parse_retry_after_seconds({}) is None


def test_parse_retry_after_seconds_http_date_not_implemented():
    # ponytail: HTTP-date Retry-After is valid per RFC 7231 but every provider
    # exercised so far (NVIDIA NIM) sends delta-seconds; date parsing is
    # deliberately skipped (see parse_retry_after_seconds docstring), so an
    # HTTP-date value parses to None rather than raising.
    assert parse_retry_after_seconds({"retry-after": "Wed, 21 Oct 2026 07:28:00 GMT"}) is None


def test_parse_retry_after_seconds_negative_is_malformed():
    # RFC 7231 delta-seconds is 1*DIGIT -- a negative value is malformed
    # timing info. Same treatment as the http-date case above: None, so
    # callers use their own defaults instead of handing time.sleep() a
    # negative number (which raises ValueError mid-turn).
    assert parse_retry_after_seconds({"retry-after": "-1"}) is None


# ──────────────────────────────────────────────────────────────────────────
# Wiring: llm_engine._generar_dialogo's cloud-failure branch
# ──────────────────────────────────────────────────────────────────────────


def _cloud_config():
    return {
        "active_provider": "openai",
        "fallback_mode": "auto",
        "pregen_enabled": False,
        "profiles": {"openai": {"base_url": "https://api.example.com/v1", "model": "gpt-cloud"}},
    }


def _make_motor(tmp_path):
    import opencohost.config.settings as settings
    from opencohost.core.llm_engine import MotorVocalIA

    original_last_model = settings.LAST_MODEL_FILE
    settings.LAST_MODEL_FILE = os.path.join(str(tmp_path), "last_model.json")
    try:
        motor = MotorVocalIA(queue.Queue(), lambda event: None)
    finally:
        settings.LAST_MODEL_FILE = original_last_model
    motor.ollama = MagicMock()
    motor.ollama.chat = MagicMock(return_value={"message": {"content": "local", "thinking": ""}})
    motor.pygame = MagicMock()
    motor.is_ready = True
    motor._loaded_model = motor.current_model
    motor._provider_config = _cloud_config()
    return motor


def test_cloud_429_with_retry_after_sets_rate_limited_class(tmp_path):
    motor = _make_motor(tmp_path)
    exc = CloudLLMResponseError(
        "cloud chat HTTP 429: Too Many Requests", status_code=429, headers={"retry-after": "30"}
    )
    with patch(_SEND, side_effect=exc):
        result = motor._generar_dialogo("hola", source="direct", commit_history=False)
    assert result == ""
    assert motor._last_cloud_failure_class == CLOUD_ERROR_RATE_LIMITED
    assert motor._last_llm_failure["clase"] == CLOUD_ERROR_RATE_LIMITED


def test_cloud_401_sets_bad_key_class(tmp_path):
    motor = _make_motor(tmp_path)
    exc = CloudLLMResponseError("cloud chat HTTP 401: Unauthorized", status_code=401)
    with patch(_SEND, side_effect=exc):
        result = motor._generar_dialogo("hola", source="direct", commit_history=False)
    assert result == ""
    assert motor._last_cloud_failure_class == CLOUD_ERROR_BAD_KEY


def test_cloud_bare_429_sets_ambiguous_class(tmp_path):
    motor = _make_motor(tmp_path)
    exc = CloudLLMResponseError("cloud chat HTTP 429: Too Many Requests", status_code=429)
    with patch(_SEND, side_effect=exc):
        motor._generar_dialogo("hola", source="direct", commit_history=False)
    assert motor._last_cloud_failure_class == CLOUD_ERROR_AMBIGUOUS_429


def test_local_transport_error_never_sets_cloud_failure_class(tmp_path):
    """Regression guard: a LOCAL fault must never populate the cloud-only
    field -- mirrors the existing byte-identical-local-dict guard."""
    motor = _make_motor(tmp_path)
    motor._provider_config = {}  # local
    motor.ollama.chat.side_effect = ConnectionError("ollama refused")
    result = motor._generar_dialogo("hola", source="direct", commit_history=False)
    assert result == ""
    assert motor._last_cloud_failure_class is None
    assert "clase" not in motor._last_llm_failure


def test_cloud_failure_class_cleared_on_next_success(tmp_path):
    motor = _make_motor(tmp_path)
    exc = CloudLLMResponseError("cloud chat HTTP 401: Unauthorized", status_code=401)
    with patch(_SEND, side_effect=exc):
        motor._generar_dialogo("hola", source="direct", commit_history=False)
    assert motor._last_cloud_failure_class == CLOUD_ERROR_BAD_KEY

    # _handle_cloud_failure optimistically flips _cloud_fallback_active on the
    # first failure (its own state machine, out of scope for this unit) --
    # reset it so the second call actually re-hits the cloud branch this test
    # is isolating, instead of silently taking the local path.
    motor._cloud_fallback_active = False
    with patch(_SEND, return_value={"message": {"content": "hi", "thinking": ""}, "usage": {}}):
        result = motor._generar_dialogo("hola de nuevo", source="direct", commit_history=False)
    assert result == "hi"
    assert motor._last_cloud_failure_class is None


def test_log_line_reports_class_name(tmp_path, caplog):
    motor = _make_motor(tmp_path)
    exc = CloudLLMResponseError(
        "cloud chat HTTP 429: Too Many Requests", status_code=429, headers={"retry-after": "30"}
    )
    with caplog.at_level(logging.WARNING, logger="OpenCohost"):
        with patch(_SEND, side_effect=exc):
            motor._generar_dialogo("hola", source="direct", commit_history=False)
    assert any("clase=rate_limited" in item for item in list(motor.log_queue.queue))
    assert any("clase=rate_limited" in record.message for record in caplog.records)


def test_body_excerpt_and_headers_never_appear_in_logs(tmp_path, caplog):
    """F2's whole point: status/headers/body are carried ON the exception for
    classification, never logged. `body_excerpt` in particular must never
    reach a log line."""
    sensitive_body = "SENSITIVE_QUOTA_DETAIL_DO_NOT_LOG account=acct_12345"
    motor = _make_motor(tmp_path)
    exc = CloudLLMResponseError(
        "cloud chat HTTP 429: Too Many Requests",
        status_code=429,
        headers={"retry-after": "30", "x-ratelimit-remaining": "0"},
        body_excerpt=sensitive_body,
    )
    with caplog.at_level(logging.WARNING, logger="OpenCohost"):
        with patch(_SEND, side_effect=exc):
            motor._generar_dialogo("hola", source="direct", commit_history=False)

    log_lines = list(motor.log_queue.queue)
    assert not any(sensitive_body in item for item in log_lines)
    assert not any(sensitive_body in record.message for record in caplog.records)
    # Retry-After seconds specifically is safe to have surfaced (F2 spec);
    # the raw header dict repr is not something this unit logs at all.
    assert not any("x-ratelimit-remaining" in item for item in log_lines)
