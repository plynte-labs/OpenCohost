"""B1/B2 (vision-settings-redesign-20261125): image-rejection and quota classes.

B1: a cloud 400/415/422 whose sanitized provider message says images are not
supported is its own class (`image_unsupported`). A turn that carried images
retries ONCE on the same cloud model without them (fixed unseen note), never
enters cloud->local fallback, and emits `cloud_image_rejected`.
B2: HTTP 402 (or a 429/403 whose message says quota/credits/billing) is
`quota_exhausted`: no probe loop, `cloud_quota_exhausted` event, spoken line in
manual mode.
"""

import json
from unittest.mock import MagicMock

import pytest

from opencohost.core.providers.cloud import cloud_llm_client as client
from opencohost.core.providers.cloud.cloud_llm_client import CloudLLMResponseError
from opencohost.core.vision import direct_cloud as dc
from opencohost.core.vision import turn_step
from tests.test_vision_direct_cloud import (  # noqa: F401  (autouse fixture re-exported)
    SECRET_PIXELS,
    _all_text,
    _engage_fallback,
    _FakeStep,
    _manual_cfg,
    _motor,
    _vision_flags,
)
from types import SimpleNamespace


def _err(status, message, headers=None):
    body = json.dumps({"error": {"message": message}})
    return CloudLLMResponseError(
        f"cloud chat HTTP {status}", status_code=status, headers=headers or {}, body_excerpt=body
    )


# ----------------------------------------------------------------- classifier


@pytest.mark.parametrize("status", [400, 415, 422])
@pytest.mark.parametrize(
    "message",
    [
        "This model does not support image input",
        "image_url is not supported for this model",
        "Model is not multimodal",
        "Invalid content type: vision inputs are not supported",
    ],
)
def test_image_rejection_statuses_and_messages(status, message):
    assert client.classify_cloud_error(_err(status, message)) == client.CLOUD_ERROR_IMAGE_UNSUPPORTED


@pytest.mark.parametrize(
    "message",
    ["Invalid temperature", "max_tokens too large", "Bad request", "invalid image format is fine? no: unrelated"],
)
def test_a_400_without_an_image_message_stays_transient(message):
    assert client.classify_cloud_error(_err(400, message)) == client.CLOUD_ERROR_TRANSIENT


def test_image_words_on_other_statuses_do_not_classify():
    assert client.classify_cloud_error(_err(500, "image input not supported")) == client.CLOUD_ERROR_TRANSIENT


def test_message_helpers_are_conservative():
    assert client.message_indicates_image_rejection("does not support images")
    assert not client.message_indicates_image_rejection("")
    assert not client.message_indicates_image_rejection(None)
    assert not client.message_indicates_image_rejection("the model is overloaded")
    assert client.message_indicates_quota_exhausted("You exceeded your current quota")
    assert client.message_indicates_quota_exhausted("insufficient_quota")
    assert client.message_indicates_quota_exhausted("Out of credits, check billing")
    assert not client.message_indicates_quota_exhausted("Too many requests")


def test_402_is_quota_exhausted_even_without_a_body():
    assert (
        client.classify_cloud_error(CloudLLMResponseError("x", status_code=402))
        == client.CLOUD_ERROR_QUOTA_EXHAUSTED
    )


@pytest.mark.parametrize("status", [429, 403])
def test_quota_message_on_429_or_403_is_quota_exhausted(status):
    exc = _err(status, "You exceeded your current quota, please check your plan and billing details")
    assert client.classify_cloud_error(exc) == client.CLOUD_ERROR_QUOTA_EXHAUSTED


def test_plain_403_and_429_keep_their_classes():
    assert client.classify_cloud_error(_err(403, "forbidden")) == client.CLOUD_ERROR_BAD_KEY
    assert client.classify_cloud_error(CloudLLMResponseError("x", status_code=429)) == (
        client.CLOUD_ERROR_AMBIGUOUS_429
    )


def test_429_with_retry_timing_stays_rate_limited_despite_a_quota_word():
    exc = _err(429, "Quota exceeded for metric per minute", headers={"retry-after": "7"})
    assert client.classify_cloud_error(exc) == client.CLOUD_ERROR_RATE_LIMITED


def test_quota_class_never_gets_a_probe_loop():
    motor = _motor()
    assert motor._initial_cloud_probe_wait(client.CLOUD_ERROR_QUOTA_EXHAUSTED, None) is None
    assert motor._initial_cloud_probe_wait(client.CLOUD_ERROR_IMAGE_UNSUPPORTED, None) is None


# --------------------------------------------------------------- engine (B1)


def _events(motor):
    seen = []
    motor.ui_callback = lambda *args, **kw: seen.append(args[0] if args else None)
    return seen


def _script(motor, calls, outcomes):
    """Chat double: *outcomes* is consumed per call; an Exception is raised."""
    queue_ = list(outcomes)

    def fake(*, is_local, messages, **kwargs):
        calls.append({"is_local": is_local, "messages": messages})
        item = queue_.pop(0) if queue_ else ""
        if isinstance(item, Exception):
            raise item
        return {"message": {"content": item, "thinking": ""}}

    motor._ollama_chat_with_watchdog = MagicMock(side_effect=fake)


def _with_look(motor):
    motor._vision = SimpleNamespace(step=_FakeStep(images=((SECRET_PIXELS, "image/jpeg"),)))


IMG_ERR = "This model does not support image input"


def test_image_rejection_retries_same_turn_text_only_on_the_same_cloud_model():
    motor = _motor()
    events = _events(motor)
    _engage_fallback(motor)
    calls = []
    _script(motor, calls, [_err(400, IMG_ERR), "I could not see it."])
    _with_look(motor)

    out = motor._generar_dialogo("what is on my screen?", source="direct", commit_history=True)

    assert out == "I could not see it."
    assert [c["is_local"] for c in calls] == [False, False]
    assert any(isinstance(m["content"], list) for m in calls[0]["messages"])
    assert all(isinstance(m["content"], str) for m in calls[1]["messages"])
    assert "SECRETPIXELS" not in str(calls[1]["messages"])
    assert dc.IMAGE_UNSEEN_NOTE in _all_text(calls[1])
    assert motor._handle_cloud_failure.call_count == 0
    assert motor._cloud_fallback_active is False
    assert "cloud_image_rejected" in events
    assert "cloud_fallback_engaged" not in events


def test_image_rejection_retries_only_once():
    motor = _motor()
    events = _events(motor)
    _engage_fallback(motor)
    calls = []
    _script(motor, calls, [_err(400, IMG_ERR), _err(400, IMG_ERR)])
    _with_look(motor)

    out = motor._generar_dialogo("what is on my screen?", source="direct", commit_history=True)

    assert str(out) == ""
    # one text-only retry on the cloud model, never a second image retry; the
    # second 400 carried no images, so it takes today's transient path
    assert [c["is_local"] for c in calls[:2]] == [False, False]
    assert events.count("cloud_image_rejected") == 1
    assert motor._handle_cloud_failure.call_count == 1
    assert motor._handle_cloud_failure.call_args.kwargs["failure_class"] == client.CLOUD_ERROR_TRANSIENT


def test_a_400_without_images_keeps_todays_behavior():
    motor = _motor()
    events = _events(motor)
    _engage_fallback(motor)
    calls = []
    _script(motor, calls, [_err(400, IMG_ERR), "ok"])
    motor._vision = SimpleNamespace(step=_FakeStep(images=(), block=""))

    motor._generar_dialogo("tell me a joke", source="direct", commit_history=True)

    assert motor._handle_cloud_failure.call_count == 1
    assert motor._handle_cloud_failure.call_args.kwargs["failure_class"] == client.CLOUD_ERROR_TRANSIENT
    assert "cloud_image_rejected" not in events


def test_image_rejection_never_starts_the_return_probe():
    motor = _motor()
    _events(motor)
    motor._start_cloud_prober = MagicMock()
    calls = []
    _script(motor, calls, [_err(422, IMG_ERR), "no image."])
    _with_look(motor)
    motor._generar_dialogo("what is on my screen?", source="direct", commit_history=True)
    assert motor._start_cloud_prober.call_count == 0


# --------------------------------------------------------------- engine (B2)


def test_quota_in_auto_mode_goes_through_the_existing_fallback_with_its_class():
    motor = _motor()
    events = _events(motor)
    _engage_fallback(motor)
    calls = []
    _script(motor, calls, [_err(402, "Payment required")])
    motor._vision = SimpleNamespace(step=_FakeStep(images=(), block=""))

    motor._generar_dialogo("tell me a joke", source="direct", commit_history=True)

    assert motor._handle_cloud_failure.call_args.kwargs["failure_class"] == client.CLOUD_ERROR_QUOTA_EXHAUSTED
    assert events.count("cloud_quota_exhausted") == 1


def test_quota_event_is_latched_per_failure_event():
    motor = _motor(_manual_cfg())
    events = _events(motor)
    calls = []
    _script(motor, calls, [_err(402, "Payment required"), _err(402, "Payment required")])
    motor._vision = SimpleNamespace(step=_FakeStep(images=(), block=""))
    motor._generar_dialogo("tell me a joke", source="direct", commit_history=True)
    motor._generar_dialogo("tell me another", source="direct", commit_history=True)
    assert events.count("cloud_quota_exhausted") == 1


def test_manual_quota_with_a_look_reuses_the_phase_a_line():
    motor = _motor(_manual_cfg())
    _events(motor)
    calls = []
    _script(motor, calls, [_err(402, "Payment required")])
    _with_look(motor)
    out = motor._generar_dialogo("what is on my screen?", source="direct", commit_history=True)
    assert str(out) == turn_step.failure_line("cloud_unavailable", "en")


def test_manual_quota_without_a_look_speaks_the_short_line_in_the_owner_language():
    motor = _motor(_manual_cfg())
    _events(motor)
    calls = []
    _script(motor, calls, [_err(402, "Payment required")])
    motor._vision = SimpleNamespace(step=_FakeStep(images=(), block=""))
    out = motor._generar_dialogo("cuéntame un chiste, por favor", source="direct", commit_history=True)
    assert str(out) == "Me quedé sin créditos en el proveedor de la nube."
    assert getattr(out, "speak_source", "")


def test_manual_quota_line_in_english():
    motor = _motor(_manual_cfg())
    _events(motor)
    _script(motor, [], [_err(429, "You exceeded your current quota, check billing")])
    motor._vision = SimpleNamespace(step=_FakeStep(images=(), block=""))
    out = motor._generar_dialogo("what is the weather like, can you check this", source="direct", commit_history=True)
    assert str(out) == turn_step.failure_line("cloud_quota_exhausted", "en")
    assert "credit" in str(out).lower()


def test_manual_non_quota_failure_without_a_look_stays_silent():
    motor = _motor(_manual_cfg())
    _events(motor)
    _script(motor, [], [ConnectionError("refused")])
    motor._vision = SimpleNamespace(step=_FakeStep(images=(), block=""))
    out = motor._generar_dialogo("tell me a joke", source="direct", commit_history=True)
    assert str(out) == ""
