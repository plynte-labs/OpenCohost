"""Unit 2.1 (runtime_findings_batch_20260731) — make the 429 actually retry.

Builds on unit 1.1's taxonomy (`classify_cloud_error` / `parse_retry_after_seconds`):

- `rate_limited` now spends one attempt of the existing `max_intentos` budget
  (`continue` in `_generar_dialogo`'s retry loop) instead of exiting
  immediately, honouring a bounded `Retry-After` -- above
  `CLOUD_RATE_LIMIT_RETRY_MAX_SECONDS` it behaves exactly as before this unit
  (no in-turn retry, `return ""` -> fallback path).
- `bad_key` never retries and surfaces a "check your key" banner
  (`cloud_bad_key`, engine_host's motor-event whitelist) exactly ONCE per
  failure event -- a latch that resets when `_last_cloud_failure_class`
  clears on the next successful generation.
- `ambiguous_429` / `transient` are untouched (no retry, same as today).
"""

import os
import queue
import sys
from unittest.mock import patch

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from opencohost.core.cloud_llm_client import CloudLLMResponseError
from opencohost.api import engine_host

_SEND = "opencohost.core.cloud_llm_client.send_chat_completion"


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
    from unittest.mock import MagicMock

    original_last_model = settings.LAST_MODEL_FILE
    settings.LAST_MODEL_FILE = os.path.join(str(tmp_path), "last_model.json")
    ui_events = []
    try:
        motor = MotorVocalIA(queue.Queue(), ui_events.append)
    finally:
        settings.LAST_MODEL_FILE = original_last_model
    motor.ollama = MagicMock()
    motor.ollama.chat = MagicMock(return_value={"message": {"content": "local", "thinking": ""}})
    motor.pygame = MagicMock()
    motor.is_ready = True
    motor._loaded_model = motor.current_model
    motor._provider_config = _cloud_config()
    return motor, ui_events


def _success_response(content="hi"):
    return {"message": {"content": content, "thinking": ""}, "usage": {}}


def test_rate_limited_retries_once_and_succeeds(tmp_path, monkeypatch):
    sleeps = []
    monkeypatch.setattr("opencohost.core.llm_engine.time.sleep", lambda s: sleeps.append(s))
    motor, _ = _make_motor(tmp_path)
    handle_calls = []
    monkeypatch.setattr(
        motor, "_handle_cloud_failure", lambda source, **_kwargs: handle_calls.append(source)
    )
    exc = CloudLLMResponseError("HTTP 429", status_code=429, headers={"retry-after": "2"})

    with patch(_SEND, side_effect=[exc, _success_response()]) as mock_send:
        result = motor._generar_dialogo("hola", source="direct", commit_history=False)

    assert mock_send.call_count == 2
    assert result == "hi"
    assert sleeps == [2]
    assert handle_calls == []  # no fallback triggered -- the retry itself succeeded
    assert motor._last_cloud_failure_class is None  # cleared on success


def test_rate_limited_default_wait_used_when_header_unparseable(tmp_path, monkeypatch):
    """x-ratelimit-reset counts as retry-timing info (classification), but
    parse_retry_after_seconds only reads `retry-after` -- this is the one
    reachable path to the CLOUD_RATE_LIMIT_RETRY_DEFAULT_SECONDS fallback."""
    sleeps = []
    monkeypatch.setattr("opencohost.core.llm_engine.time.sleep", lambda s: sleeps.append(s))
    motor, _ = _make_motor(tmp_path)
    exc = CloudLLMResponseError("HTTP 429", status_code=429, headers={"x-ratelimit-reset": "1700000000"})

    with patch(_SEND, side_effect=[exc, _success_response()]) as mock_send:
        result = motor._generar_dialogo("hola", source="direct", commit_history=False)

    assert mock_send.call_count == 2
    assert result == "hi"
    assert sleeps == [2]  # CLOUD_RATE_LIMIT_RETRY_DEFAULT_SECONDS


def test_rate_limited_negative_retry_after_uses_default_wait(tmp_path, monkeypatch):
    """A malformed negative Retry-After (e.g. "-1" from a broken proxy) must
    behave like an unparseable header: default wait, retry -- never a negative
    sleep. time.sleep(-1) raises ValueError, which the outer generic handler
    swallows into a silent empty turn with no fallback engagement."""
    sleeps = []
    monkeypatch.setattr("opencohost.core.llm_engine.time.sleep", lambda s: sleeps.append(s))
    motor, _ = _make_motor(tmp_path)
    exc = CloudLLMResponseError("HTTP 429", status_code=429, headers={"retry-after": "-1"})

    with patch(_SEND, side_effect=[exc, _success_response()]) as mock_send:
        result = motor._generar_dialogo("hola", source="direct", commit_history=False)

    assert mock_send.call_count == 2
    assert result == "hi"
    assert sleeps == [2]  # CLOUD_RATE_LIMIT_RETRY_DEFAULT_SECONDS, never -1


def test_rate_limited_budget_exhausted_falls_back_like_today(tmp_path, monkeypatch):
    sleeps = []
    monkeypatch.setattr("opencohost.core.llm_engine.time.sleep", lambda s: sleeps.append(s))
    motor, _ = _make_motor(tmp_path)
    handle_calls = []
    monkeypatch.setattr(
        motor, "_handle_cloud_failure", lambda source, **_kwargs: handle_calls.append(source)
    )
    exc = CloudLLMResponseError("HTTP 429", status_code=429, headers={"retry-after": "2"})

    with patch(_SEND, side_effect=[exc, exc]) as mock_send:
        result = motor._generar_dialogo("hola", source="direct", commit_history=False)

    assert mock_send.call_count == 2
    assert result == ""
    assert sleeps == [2]  # the one retry still waited before its (failed) 2nd attempt
    assert handle_calls == ["direct"]
    assert motor._last_cloud_failure_class == "rate_limited"


def test_rate_limited_retry_after_exceeds_max_no_retry(tmp_path, monkeypatch):
    sleeps = []
    monkeypatch.setattr("opencohost.core.llm_engine.time.sleep", lambda s: sleeps.append(s))
    motor, _ = _make_motor(tmp_path)
    handle_calls = []
    monkeypatch.setattr(
        motor, "_handle_cloud_failure", lambda source, **_kwargs: handle_calls.append(source)
    )
    exc = CloudLLMResponseError("HTTP 429", status_code=429, headers={"retry-after": "3600"})

    with patch(_SEND, side_effect=[exc]) as mock_send:
        result = motor._generar_dialogo("hola", source="direct", commit_history=False)

    assert mock_send.call_count == 1  # no in-turn retry
    assert result == ""
    assert sleeps == []  # never slept for a wait it wasn't going to honour
    assert handle_calls == ["direct"]
    assert motor._last_cloud_failure_class == "rate_limited"


def test_ambiguous_429_never_retries(tmp_path, monkeypatch):
    sleeps = []
    monkeypatch.setattr("opencohost.core.llm_engine.time.sleep", lambda s: sleeps.append(s))
    motor, _ = _make_motor(tmp_path)
    handle_calls = []
    monkeypatch.setattr(
        motor, "_handle_cloud_failure", lambda source, **_kwargs: handle_calls.append(source)
    )
    exc = CloudLLMResponseError("HTTP 429", status_code=429)  # bare -- no timing headers

    with patch(_SEND, side_effect=[exc]) as mock_send:
        result = motor._generar_dialogo("hola", source="direct", commit_history=False)

    assert mock_send.call_count == 1
    assert result == ""
    assert sleeps == []
    assert handle_calls == ["direct"]
    assert motor._last_cloud_failure_class == "ambiguous_429"


def test_bad_key_never_retries_and_banner_latched_once_across_two_turns(tmp_path, monkeypatch):
    motor, ui_events = _make_motor(tmp_path)
    exc = CloudLLMResponseError("HTTP 401", status_code=401)

    with patch(_SEND, side_effect=exc):
        result_1 = motor._generar_dialogo("hola", source="direct", commit_history=False)
    assert result_1 == ""
    assert motor._last_cloud_failure_class == "bad_key"
    assert ui_events.count("cloud_bad_key") == 1

    # _handle_cloud_failure optimistically flips _cloud_fallback_active on the
    # first failure (its own state machine, out of scope for this unit) --
    # reset it so the second call re-hits the cloud branch this test is
    # isolating, exactly like test_cloud_error_taxonomy.py's own pattern.
    motor._cloud_fallback_active = False

    with patch(_SEND, side_effect=exc):
        result_2 = motor._generar_dialogo("hola de nuevo", source="direct", commit_history=False)
    assert result_2 == ""
    assert ui_events.count("cloud_bad_key") == 1  # latched -- not emitted again

    assert "cloud_bad_key" in engine_host._MOTOR_EVENT_WHITELIST
