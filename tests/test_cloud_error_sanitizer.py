"""Shared provider-error sanitizer + message extraction (setup-wizard-simple S1)."""

import pytest

from opencohost.core.providers.cloud import cloud_llm_client as c
from opencohost.core.providers.cloud.cloud_llm_client import CloudLLMResponseError

KEY = "AIzaSyDUMMYKEYDUMMYKEYDUMMYKEY123456789"


def test_strips_the_known_key_anywhere():
    out = c.sanitize_provider_text(f"bad key {KEY} given", api_key=KEY)
    assert KEY not in out and "bad key" in out


@pytest.mark.parametrize(
    "raw",
    [
        "Authorization: Bearer abc.def-ghi_123456",
        "see https://x.test/v1?key=SECRETVALUE123&alt=sse now",
        "token AIzaSyABCDEFGHIJKLMNOPQRSTUVWXYZ0123456 used",
        "token sk-proj-abcdefghijklmnopqrstuvwxyz used",
        "token AQ.Ab8RN6abcdefghijklmnopqrstuvwxyz used",
        "api_key=hunter2hunter2 failed",
    ],
)
def test_strips_key_like_strings(raw):
    out = c.sanitize_provider_text(raw)
    for secret in ("abc.def-ghi_123456", "SECRETVALUE123", "AIzaSyABCDEF", "sk-proj-abc", "AQ.Ab8RN6", "hunter2"):
        assert secret not in out


def test_collapses_whitespace_and_truncates():
    out = c.sanitize_provider_text("a \n\n  b\t" + "x" * 500, limit=200)
    assert len(out) <= 200 and "\n" not in out and out.startswith("a b ")


def test_empty_and_none():
    assert c.sanitize_provider_text(None) == ""
    assert c.sanitize_provider_text("   ") == ""


def _err(body, status=400):
    return CloudLLMResponseError(f"cloud chat HTTP {status}", status_code=status, body_excerpt=body)


def test_message_from_openai_shaped_json():
    exc = _err('{"error": {"code": 400, "message": "Unknown field chat_template_kwargs", "status": "INVALID_ARGUMENT"}}')
    msg = c.extract_provider_message(exc)
    assert "Unknown field chat_template_kwargs" in msg and "INVALID_ARGUMENT" in msg


def test_message_from_list_wrapped_json():
    exc = _err('[{"error": {"code": 400, "message": "API key not valid", "status": "INVALID_ARGUMENT"}}]')
    assert "API key not valid" in c.extract_provider_message(exc)


def test_message_from_top_level_message_or_detail():
    assert "nope" in c.extract_provider_message(_err('{"message": "nope"}'))
    assert "slow down" in c.extract_provider_message(_err('{"detail": "slow down"}', 429))


def test_message_from_truncated_json_uses_regex():
    exc = _err('{"error": {"code": 400, "message": "Budget exceeded for project", "details": [{"@type": "x", "fie')
    assert "Budget exceeded for project" in c.extract_provider_message(exc)


def test_message_falls_back_to_raw_excerpt_and_sanitizes():
    exc = _err(f"<html>Bad request key={KEY}</html>")
    msg = c.extract_provider_message(exc, api_key=KEY)
    assert "Bad request" in msg and KEY not in msg


def test_message_none_without_body():
    assert c.extract_provider_message(CloudLLMResponseError("x", status_code=400)) is None
    assert c.extract_provider_message(ValueError("x")) is None


def test_message_is_bounded():
    exc = _err('{"error": {"message": "' + "m" * 900 + '"}}')
    assert len(c.extract_provider_message(exc)) <= 200


# --- S8 review fixes --------------------------------------------------------


@pytest.mark.parametrize(
    "raw,secret",
    [
        ('{"api_key": "hunter2hunter2"}', "hunter2"),
        ("x-api-key: hunter2hunter2", "hunter2"),
        ("api-key: hunter2hunter2", "hunter2"),
        ("'apiKey' = 'hunter2hunter2'", "hunter2"),
        ("Authorization: Basic dXNlcjpwYXNz", "dXNlcjpwYXNz"),
        ("blob QWxhZGRpbjpvcGVuIHNlc2FtZQ+/abcdefghijklmn== end", "QWxhZGRpbjpvcGVu"),
        ("truncated AIzaSyAB", "AIzaSyAB"),
        ("truncated sk-abc1", "sk-abc1"),
    ],
)
def test_strips_more_key_shapes(raw, secret):
    out = c.sanitize_provider_text(raw)
    assert secret not in out


def test_json_body_without_message_never_falls_back_to_raw():
    body = '{"detail": [{"type": "missing", "loc": ["body"], "input": {"messages": [{"content": "hola Kira"}]}}]}'
    msg = c.extract_provider_message(_err(body, 422))
    assert msg is None or "hola Kira" not in msg


def test_json_body_echoing_messages_never_falls_back_to_raw():
    body = '{"messages": [{"role": "user", "content": "hola Kira"}], "oops": true}'
    assert c.extract_provider_message(_err(body)) is None


def test_truncated_json_with_input_fields_never_falls_back_to_raw():
    body = '{"detail": [{"input": {"messages": [{"content": "hola Kira secreto de la charla'
    assert c.extract_provider_message(_err(body, 422)) is None


def test_validation_detail_list_keeps_only_msg():
    body = '{"detail": [{"msg": "Field required", "input": {"content": "hola Kira"}}]}'
    msg = c.extract_provider_message(_err(body, 422))
    assert "Field required" in msg and "hola Kira" not in msg


def test_message_drops_quoted_input_echo():
    body = '{"error": {"message": "Invalid request, input_value=\'hola Kira charla\' was rejected"}}'
    msg = c.extract_provider_message(_err(body))
    assert "Invalid request" in msg and "hola Kira" not in msg


def test_extract_error_status():
    exc = _err('{"error": {"code": 400, "message": "x", "status": "INVALID_ARGUMENT"}}')
    assert c.extract_error_status(exc) == "INVALID_ARGUMENT"
    assert c.extract_error_status(_err("not json")) is None
