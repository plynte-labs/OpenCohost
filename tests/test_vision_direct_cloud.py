"""P2 (cloud-providers-vision-20261111): direct vision through a cloud chat model.

When the active provider is cloud, the chosen model sees images and the owner
opted in, screen looks and pasted images ride in the SAME chat call as OpenAI
``image_url`` parts. Everything else keeps the two-call local path. All HTTP is
mocked; no real provider, no real screen, no GPU.
"""
from __future__ import annotations

import base64
import logging
import queue
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from opencohost.api import chat_image
from opencohost.config import settings
from opencohost.config import vision_settings as vs
from opencohost.core import llm_engine
from opencohost.core.providers.cloud import cloud_llm_client
from opencohost.core.vision import direct_cloud as dc
from opencohost.core.vision.capture import CapturedFrame
from opencohost.core.vision.gate import VisionDecision
from opencohost.core.vision.sanitize import SCREEN_BLOCK_CLOSE, SCREEN_BLOCK_OPEN
from opencohost.core.vision.turn_step import VisionTurnResult, VisionTurnStep

# The legacy layout puts the screen instruction in the user turn (asserted below);
# the v2 layout keeps a stable rule and a marker (tests/test_prompt_layout_v2.py).
pytestmark = pytest.mark.usefixtures("legacy_prompt_layout")

SECRET_PIXELS = b"\xff\xd8\xff\xe0SECRETPIXELS-" * 8
SECRET_B64 = base64.b64encode(SECRET_PIXELS).decode("ascii")
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"SECRETPNG-" * 40
JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"SECRETJPEG-" * 40
WEBP_BYTES = b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"SECRETWEBP-" * 40
_ORIGINS = ["http://localhost:5173"]

GEMINI_CFG = {
    "active_provider": "gem",
    "fallback_mode": "auto",
    "profiles": {
        "gem": {
            "preset": "gemini",
            "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
            "model": "gemini-3.5-flash-lite",
        }
    },
}


def _cfg(preset="gemini", model="gemini-3.5-flash-lite", **extra):
    return {
        "active_provider": "p1",
        "profiles": {"p1": {"preset": preset, "base_url": "https://x/v1", "model": model, **extra}},
    }


# ---------------------------------------------------------------- eligibility


@pytest.mark.parametrize(
    "cloud,vision_model,opted,enabled,direct,reason",
    [
        (True, True, True, True, True, "ok"),
        (False, True, True, True, False, "local_provider"),
        (True, False, True, True, False, "model_no_vision"),
        (True, True, False, True, False, "not_opted_in"),
        (True, True, True, False, False, "vision_off"),
        (False, False, False, False, False, "local_provider"),
    ],
)
def test_route_decision_matrix(cloud, vision_model, opted, enabled, direct, reason):
    decision = dc.decide_route(
        provider_is_cloud=cloud, model_vision=vision_model, opted_in=opted, vision_enabled=enabled
    )
    assert decision.direct is direct
    assert decision.reason == reason


def test_route_log_line_is_fixed_literal(caplog):
    caplog.set_level(logging.INFO, logger="OpenCohost")
    dc.log_route(dc.decide_route(
        provider_is_cloud=True, model_vision=True, opted_in=True, vision_enabled=True))
    dc.log_route(dc.decide_route(
        provider_is_cloud=True, model_vision=True, opted_in=False, vision_enabled=True))
    assert "[VISION] route=direct reason=ok" in caplog.text
    assert "[VISION] route=describe reason=not_opted_in" in caplog.text


def test_preset_model_flag_decides_vision():
    assert dc.profile_model_vision(_cfg("gemini", "gemini-3.5-flash-lite")) is True
    assert dc.profile_model_vision(_cfg("deepseek", "deepseek-v4-pro")) is False
    assert dc.profile_model_vision(_cfg("deepseek", "deepseek-flash")) is True
    assert dc.profile_model_vision(_cfg("nvidia_nim", "nvidia/nemotron-3.5-super-vl-120b-a12b")) is True
    assert dc.profile_model_vision(_cfg("nvidia_nim", "meta/llama-3.1-70b-instruct")) is False


def test_custom_profile_uses_the_model_vision_checkbox():
    assert dc.profile_model_vision(_cfg(None, "my-model")) is False
    assert dc.profile_model_vision(_cfg(None, "my-model", model_vision=True)) is True
    assert dc.profile_model_vision(_cfg(None, "my-model", model_vision="yes")) is False
    # a preset provider with a model outside the curated list (OpenRouter's live list)
    assert dc.profile_model_vision(_cfg("openrouter", "some/vl-model")) is False
    assert dc.profile_model_vision(_cfg("openrouter", "some/vl-model", model_vision=True)) is True


def test_local_or_missing_profile_never_sees_images():
    assert dc.profile_model_vision({"active_provider": "local", "profiles": {}}) is False
    assert dc.profile_model_vision({}) is False
    assert dc.profile_model_vision(None) is False


# ------------------------------------------------------------------- settings


def test_opt_in_defaults_to_false_and_is_in_the_vision_settings():
    assert settings._VISION_DEFAULTS["VISION_CLOUD_DIRECT_ALLOWED"] is False
    assert vs.VisionSettings(True, False, 0.0, True, 11435, "m", 15.0).cloud_direct_allowed is False
    assert "cloud_direct_allowed" in vs._ATTRS


def test_opt_in_accepts_only_a_strict_bool_and_persists(tmp_path):
    from dataclasses import replace

    base = replace(vs.current(), cloud_direct_allowed=False)
    assert vs.with_updates(base, {"cloud_direct_allowed": True}).cloud_direct_allowed is True
    for bad in ("yes", 1, None, "true"):
        assert vs.with_updates(base, {"cloud_direct_allowed": bad}).cloud_direct_allowed is False
    path = str(tmp_path / "vision.json")
    on = vs.with_updates(base, {"cloud_direct_allowed": True})
    assert vs.save(on, path) is True
    assert vs.load(path, base=base).cloud_direct_allowed is True


@pytest.fixture
def _restore_vision(monkeypatch):
    for attr in vs._ATTRS.values():
        monkeypatch.setattr(settings, attr, getattr(settings, attr))


def test_vision_route_reads_and_writes_the_opt_in_and_rejects_non_bool(_restore_vision):
    from tests.test_api_vision import _VisionHost, _client, _operator_headers

    settings.VISION_CLOUD_DIRECT_ALLOWED = False
    app, client = _client(_VisionHost)
    try:
        assert client.get("/api/vision").json()["cloud_direct_allowed"] is False
        ok = client.put(
            "/api/vision", json={"cloud_direct_allowed": True}, headers=_operator_headers()
        )
        assert ok.status_code == 200
        assert ok.json()["cloud_direct_allowed"] is True
        bad = client.put(
            "/api/vision", json={"cloud_direct_allowed": "yes"}, headers=_operator_headers()
        )
        assert bad.status_code == 422
        assert bad.json()["errors"] == [{"field": "cloud_direct_allowed", "code": "invalid_value"}]
    finally:
        client.__exit__(None, None, None)


# ------------------------------------------------------------ provider profile


from tests.test_provider_test_route import (  # noqa: E402,F401  (autouse fixtures)
    _client as _provider_client,
    _isolated_files,
    _reset_host_active,
)


def test_provider_route_persists_model_vision_and_never_echoes_a_key():
    with _provider_client() as client:
        put = client.put(
            "/api/llm/provider",
            json={
                "profile_id": "mine",
                "base_url": "https://llm.example/v1",
                "model": "my-vl",
                "model_vision": True,
            },
        )
        assert put.status_code == 200
        assert put.json()["profiles"]["mine"]["model_vision"] is True
        assert client.get("/api/llm/provider").json()["profiles"]["mine"]["model_vision"] is True
        off = client.put(
            "/api/llm/provider", json={"profile_id": "mine", "model_vision": False}
        )
        assert off.json()["profiles"]["mine"]["model_vision"] is False


def test_provider_route_rejects_a_non_bool_model_vision():
    with _provider_client() as client:
        bad = client.put(
            "/api/llm/provider",
            json={"profile_id": "mine", "base_url": "https://llm.example/v1", "model_vision": "yes"},
        )
        assert bad.status_code == 422


def test_profiles_without_the_flag_report_false():
    with _provider_client() as client:
        client.put(
            "/api/llm/provider",
            json={"profile_id": "plain", "base_url": "https://llm.example/v1", "model": "m"},
        )
        assert client.get("/api/llm/provider").json()["profiles"]["plain"]["model_vision"] is False


# --------------------------------------------------------------- message parts


def test_attach_images_turns_only_the_last_user_message_into_parts():
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "old"},
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": "look"},
    ]
    out = dc.attach_images(messages, [(SECRET_PIXELS, "image/jpeg"), (PNG_BYTES, "image/png")])
    assert out[:3] == messages[:3]
    parts = out[3]["content"]
    assert parts[0] == {"type": "text", "text": "look"}
    assert parts[1] == {
        "type": "image_url",
        "image_url": {"url": "data:image/jpeg;base64," + SECRET_B64},
    }
    assert parts[2]["image_url"]["url"].startswith("data:image/png;base64,")
    # the input list is not mutated and stays text only
    assert messages[3]["content"] == "look"


def test_attach_images_without_images_returns_the_same_messages():
    messages = [{"role": "user", "content": "hi"}]
    assert dc.attach_images(messages, []) is messages


def test_the_cloud_request_body_carries_the_parts_and_a_local_body_does_not(monkeypatch):
    captured = {}

    class _Resp:
        status_code = 200
        headers = {}

        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": "ok"}}], "usage": {}}

    def fake_post(url, json=None, headers=None, timeout=None, **kw):
        captured["body"] = json
        return _Resp()

    monkeypatch.setattr(cloud_llm_client.requests, "post", fake_post)
    messages = dc.attach_images([{"role": "user", "content": "look"}], [(SECRET_PIXELS, "image/jpeg")])
    cloud_llm_client.send_chat_completion(
        "https://x/v1", "k", "m", messages, {}, 5.0, reasoning_style="none"
    )
    content = captured["body"]["messages"][0]["content"]
    assert isinstance(content, list) and content[1]["type"] == "image_url"
    plain = [{"role": "user", "content": "look"}]
    cloud_llm_client.send_chat_completion(
        "https://x/v1", "k", "m", plain, {}, 5.0, reasoning_style="none"
    )
    assert captured["body"]["messages"][0]["content"] == "look"


# ------------------------------------------------------------------ turn step


class _Settings:
    describe_timeout_s = 8.0
    frames = 1
    frame_interval_s = 0.0
    show_thumbnails = False


class _Gate:
    def __init__(self, decision):
        self.decision = decision

    def decide(self, text, source):
        return self.decision


class _Capture:
    def __init__(self, calls):
        self.calls = calls

    def capture(self, **kwargs):
        self.calls.append("capture")
        return CapturedFrame(
            jpeg=SECRET_PIXELS, width=1280, height=720, source_width=1920,
            source_height=1080, is_black=False, grab_ms=1.0, encode_ms=1.0,
        )


class _Describer:
    def __init__(self, calls):
        self.calls = calls

    def describe(self, *args, **kwargs):
        self.calls.append("describe")
        return "A description."


def _step(calls, *, direct=None, ready=None):
    return VisionTurnStep(
        _Gate(VisionDecision(True, ("explicit_look",), 0.0)),
        _Capture(calls),
        _Describer(calls),
        speak_bridge=lambda line: calls.append("bridge"),
        settings=_Settings(),
        ready_check=ready,
        clock=lambda: 0.0,
        sleep=lambda s: None,
        choice=lambda seq: seq[0],
        **({"direct_check": direct} if direct is not None else {}),
    )


def test_direct_screen_look_captures_but_never_describes():
    calls = []
    result = _step(calls, direct=lambda: True).run("what is on my screen?", "direct")
    assert "describe" not in calls and "capture" in calls
    assert result.outcome == "ok"
    assert result.images == ((SECRET_PIXELS, "image/jpeg"),)
    assert result.screen_block.startswith(SCREEN_BLOCK_OPEN)
    assert result.screen_block.endswith(SCREEN_BLOCK_CLOSE)
    assert dc.DIRECT_SCREEN_BODY in result.screen_block
    assert result.description == ""


def test_direct_screen_look_does_not_wait_for_the_local_vision_daemon():
    calls = []
    result = _step(calls, direct=lambda: True, ready=lambda: "model_missing").run("look", "direct")
    assert result.outcome == "ok" and result.images


def test_ineligible_look_keeps_the_describe_path():
    calls = []
    result = _step(calls, direct=lambda: False).run("what is on my screen?", "direct")
    assert "describe" in calls
    assert result.images == ()
    assert "A description." in result.screen_block


def test_step_without_a_direct_check_is_byte_identical_to_before():
    calls = []
    result = _step(calls).run("look", "direct")
    assert "describe" in calls and result.images == ()


def test_a_failing_direct_check_falls_back_to_the_describe_path():
    calls = []

    def boom():
        raise RuntimeError("config unreadable")

    result = _step(calls, direct=boom).run("look", "direct")
    assert "describe" in calls and result.images == ()


def test_direct_look_never_logs_image_bytes(caplog):
    caplog.set_level(logging.DEBUG)
    _step([], direct=lambda: True).run("look", "direct")
    assert "SECRETPIXELS" not in caplog.text and SECRET_B64 not in caplog.text


# ------------------------------------------------------------- engine plumbing


class _FakeStep:
    def __init__(self, images=(), block=None):
        self.images = images
        self.block = block
        self.calls = 0

    def run(self, text, source, *, cancel=None):
        self.calls += 1
        block = self.block if self.block is not None else dc.build_direct_screen_block()
        return VisionTurnResult(block, ("explicit_look",), "ok", images=self.images)


@pytest.fixture(autouse=True)
def _vision_flags(monkeypatch):
    monkeypatch.setattr(settings, "VISION_ENABLED", True)
    monkeypatch.setattr(settings, "VISION_CLOUD_DIRECT_ALLOWED", True)


def _motor(cfg=None):
    motor = llm_engine.MotorVocalIA(queue.Queue(), lambda event: None)
    motor._provider_config = dict(cfg if cfg is not None else GEMINI_CFG)
    motor.current_model = "llama3"
    motor._last_known_good_model = "llama3"
    motor.use_system_role = True
    motor._reasoning_model_cache["llama3"] = False
    motor._model_ctx_limit = {"llama3": 8192}
    motor._cloud_fallback_active = False
    return motor


def _record_chat(motor, calls, reply="Looks like a spreadsheet."):
    def fake(*, is_local, messages, **kwargs):
        calls.append({"is_local": is_local, "messages": messages})
        return {"message": {"content": reply, "thinking": ""}}

    motor._ollama_chat_with_watchdog = MagicMock(side_effect=fake)


def _last_user(call):
    return [m for m in call["messages"] if m["role"] == "user"][-1]


def _all_text(call):
    out = []
    for m in call["messages"]:
        c = m["content"]
        out.append(c if isinstance(c, str) else " ".join(p.get("text", "") for p in c))
    return "\n".join(out)


def _engage_fallback(motor):
    def stub(source, **kwargs):
        motor._cloud_fallback_active = True
        motor._cloud_fallback_reason = cloud_llm_client.CLOUD_ERROR_TRANSIENT

    motor._handle_cloud_failure = MagicMock(side_effect=stub)


def test_engine_eligibility_reads_provider_model_opt_in_and_vision(monkeypatch):
    motor = _motor()
    assert motor._vision_direct_eligible() is True
    monkeypatch.setattr(settings, "VISION_CLOUD_DIRECT_ALLOWED", False)
    assert motor._vision_direct_eligible() is False
    monkeypatch.setattr(settings, "VISION_CLOUD_DIRECT_ALLOWED", True)
    monkeypatch.setattr(settings, "VISION_ENABLED", False)
    assert motor._vision_direct_eligible() is False
    monkeypatch.setattr(settings, "VISION_ENABLED", True)
    motor._cloud_fallback_active = True
    assert motor._vision_direct_eligible() is False
    motor._cloud_fallback_active = False
    motor._provider_config = {"active_provider": "local", "profiles": {}}
    assert motor._vision_direct_eligible() is False
    motor._provider_config = _cfg("deepseek", "deepseek-v4-pro")
    assert motor._vision_direct_eligible() is False


def test_engine_logs_the_route_as_a_fixed_literal(caplog, monkeypatch):
    caplog.set_level(logging.INFO, logger="OpenCohost")
    motor = _motor()
    motor._vision_direct_eligible()
    monkeypatch.setattr(settings, "VISION_CLOUD_DIRECT_ALLOWED", False)
    motor._vision_direct_eligible()
    assert "[VISION] route=direct reason=ok" in caplog.text
    assert "[VISION] route=describe reason=not_opted_in" in caplog.text


def test_screen_look_reaches_the_cloud_call_as_an_image_part_in_the_same_call():
    motor = _motor()
    calls = []
    _record_chat(motor, calls)
    motor._vision = SimpleNamespace(step=_FakeStep(images=((SECRET_PIXELS, "image/jpeg"),)))

    out = motor._generar_dialogo("what is on my screen?", source="direct", commit_history=True)

    assert out == "Looks like a spreadsheet."
    assert len(calls) == 1 and calls[0]["is_local"] is False
    user = _last_user(calls[0])
    content = user["content"]
    assert isinstance(content, list)
    assert [p["type"] for p in content] == ["text", "image_url"]
    assert content[1]["image_url"]["url"] == "data:image/jpeg;base64," + SECRET_B64
    assert dc.DIRECT_SCREEN_BODY in content[0]["text"]
    assert all(isinstance(m["content"], str) for m in calls[0]["messages"] if m is not user)


def test_screen_instruction_names_the_attached_image_as_the_screen():
    motor = _motor()
    calls = []
    _record_chat(motor, calls)
    motor._vision = SimpleNamespace(step=_FakeStep(images=((SECRET_PIXELS, "image/jpeg"),)))
    motor._generar_dialogo("what is on my screen?", source="direct", commit_history=True)
    text = _all_text(calls[0])
    assert dc.SCREEN_DIRECT_INSTRUCTION in text
    from opencohost.core.context.prompt_assembler import SCREEN_CONTEXT_INSTRUCTION

    assert SCREEN_CONTEXT_INSTRUCTION not in text


def test_describe_path_instruction_is_unchanged_when_no_image_is_attached():
    motor = _motor()
    calls = []
    _record_chat(motor, calls)
    block = f"{SCREEN_BLOCK_OPEN}\nA chart.\n{SCREEN_BLOCK_CLOSE}"
    motor._vision = SimpleNamespace(step=_FakeStep(images=(), block=block))
    motor._generar_dialogo("what is on my screen?", source="direct", commit_history=True)
    from opencohost.core.context.prompt_assembler import SCREEN_CONTEXT_INSTRUCTION

    assert SCREEN_CONTEXT_INSTRUCTION in _all_text(calls[0])
    assert dc.SCREEN_DIRECT_INSTRUCTION not in _all_text(calls[0])
    assert isinstance(_last_user(calls[0])["content"], str)


def test_history_stays_text_only_after_a_direct_look():
    motor = _motor()
    calls = []
    _record_chat(motor, calls)
    motor._vision = SimpleNamespace(step=_FakeStep(images=((SECRET_PIXELS, "image/jpeg"),)))
    motor._generar_dialogo("what is on my screen?", source="direct", commit_history=True)
    assert motor.historial
    for message in motor.historial:
        assert isinstance(message["content"], str)
        assert "image_url" not in str(message) and SECRET_B64 not in str(message)
        assert "SECRETPIXELS" not in str(message)


def test_a_local_provider_request_stays_strings_even_if_a_look_carried_images():
    motor = _motor({"active_provider": "local", "profiles": {}})
    calls = []
    _record_chat(motor, calls)
    motor._vision = SimpleNamespace(step=_FakeStep(images=((SECRET_PIXELS, "image/jpeg"),)))
    motor._generar_dialogo("what is on my screen?", source="direct", commit_history=True)
    assert calls[0]["is_local"] is True
    assert all(isinstance(m["content"], str) for m in calls[0]["messages"])
    assert "SECRETPIXELS" not in str(calls[0]["messages"])


def test_a_cloud_failure_retries_locally_without_images_and_says_so():
    motor = _motor()
    calls = []
    _engage_fallback(motor)

    def fake(*, is_local, messages, **kwargs):
        calls.append({"is_local": is_local, "messages": messages})
        if not is_local:
            raise TimeoutError("watchdog_timeout:75.00s")
        return {"message": {"content": "I could not see it.", "thinking": ""}}

    motor._ollama_chat_with_watchdog = MagicMock(side_effect=fake)
    motor._vision = SimpleNamespace(step=_FakeStep(images=((SECRET_PIXELS, "image/jpeg"),)))

    out = motor._generar_dialogo("what is on my screen?", source="direct", commit_history=True)

    assert out == "I could not see it."
    assert [c["is_local"] for c in calls] == [False, True]
    assert isinstance(_last_user(calls[0])["content"], list)
    local_text = _all_text(calls[1])
    assert all(isinstance(m["content"], str) for m in calls[1]["messages"])
    assert "SECRETPIXELS" not in str(calls[1]["messages"])
    assert dc.DIRECT_SCREEN_BODY not in local_text
    assert dc.IMAGE_UNSEEN_NOTE in local_text


def test_pasted_images_up_to_three_ride_the_same_call_and_history_keeps_the_note():
    motor = _motor()
    calls = []
    _record_chat(motor, calls)
    text = chat_image.compose_direct_turn_text("what are these?", 3)
    motor.stash_turn_images(
        text, [(PNG_BYTES, "image/png"), (JPEG_BYTES, "image/jpeg"), (WEBP_BYTES, "image/webp")]
    )

    motor._generar_dialogo(text, source="direct", commit_history=True)

    content = _last_user(calls[0])["content"]
    assert [p["type"] for p in content] == ["text", "image_url", "image_url", "image_url"]
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert content[3]["image_url"]["url"].startswith("data:image/webp;base64,")
    for message in motor.historial:
        assert isinstance(message["content"], str)
        assert "SECRET" not in str(message) and "base64" not in str(message)
    assert dc.DIRECT_PASTED_NOTE in str(motor.historial)
    # the stash is consumed by the committed turn
    assert motor.peek_turn_images(text) == []


def test_pasted_images_fall_back_without_images_and_with_a_fixed_note():
    motor = _motor()
    calls = []
    _engage_fallback(motor)

    def fake(*, is_local, messages, **kwargs):
        calls.append({"is_local": is_local, "messages": messages})
        if not is_local:
            raise ConnectionError("refused")
        return {"message": {"content": "no image.", "thinking": ""}}

    motor._ollama_chat_with_watchdog = MagicMock(side_effect=fake)
    text = chat_image.compose_direct_turn_text("what is this?", 1)
    motor.stash_turn_images(text, [(PNG_BYTES, "image/png")])

    motor._generar_dialogo(text, source="direct", commit_history=True)

    assert [c["is_local"] for c in calls] == [False, True]
    assert all(isinstance(m["content"], str) for m in calls[1]["messages"])
    assert "SECRETPNG" not in str(calls[1]["messages"])
    assert dc.IMAGE_UNSEEN_NOTE in _all_text(calls[1])


def test_stashed_images_never_attach_to_a_local_provider_or_other_sources():
    motor = _motor({"active_provider": "local", "profiles": {}})
    calls = []
    _record_chat(motor, calls)
    text = chat_image.compose_direct_turn_text("x", 1)
    motor.stash_turn_images(text, [(PNG_BYTES, "image/png")])
    motor._generar_dialogo(text, source="direct", commit_history=True)
    assert all(isinstance(m["content"], str) for m in calls[0]["messages"])

    cloud = _motor()
    calls2 = []
    _record_chat(cloud, calls2)
    cloud.stash_turn_images(text, [(PNG_BYTES, "image/png")])
    cloud._generar_dialogo(text, source="chat", commit_history=True)
    assert all(isinstance(m["content"], str) for m in calls2[0]["messages"])


def test_no_image_bytes_reach_the_logs_during_a_direct_turn(caplog):
    caplog.set_level(logging.DEBUG)
    motor = _motor()
    calls = []
    _record_chat(motor, calls)
    motor._vision = SimpleNamespace(step=_FakeStep(images=((SECRET_PIXELS, "image/jpeg"),)))
    motor._generar_dialogo("what is on my screen?", source="direct", commit_history=True)
    assert "SECRETPIXELS" not in caplog.text
    assert SECRET_B64 not in caplog.text
    assert "what is on my screen" not in caplog.text


def test_stash_is_bounded_and_expires(monkeypatch):
    motor = _motor()
    clock = [1000.0]
    monkeypatch.setattr(dc, "_now", lambda: clock[0])
    motor.stash_turn_images("t1", [(PNG_BYTES, "image/png")])
    assert len(motor.peek_turn_images("t1")) == 1
    clock[0] += dc.STASH_TTL_S + 1
    assert motor.peek_turn_images("t1") == []
    for i in range(dc.STASH_MAX_ENTRIES + 3):
        motor.stash_turn_images(f"t{i}", [(PNG_BYTES, "image/png")])
    assert len(motor._turn_images) <= dc.STASH_MAX_ENTRIES


# ------------------------------------------------------------------- chat route


def _direct_host_factory(eligible):
    from tests.test_api_chat_image import _ImageHost

    class Host(_ImageHost):
        def __init__(self):
            super().__init__()
            self.stashed = []

        def vision_direct_eligible(self):
            return eligible

        def vision_stash_images(self, text, images):
            self.stashed.append((text, list(images)))

    return Host


def _app(eligible):
    import opencohost.api.main as main_mod

    return main_mod.create_app(host_factory=_direct_host_factory(eligible), cors_origins=_ORIGINS)


def _img(data, mime):
    return {"data": base64.b64encode(data).decode("ascii"), "mime_type": mime}


def test_chat_route_sends_pasted_images_directly_when_eligible(caplog):
    caplog.set_level(logging.DEBUG)
    app = _app(True)
    with TestClient(app) as client:
        resp = client.post(
            "/api/chat/turn",
            json={
                "text": "what are these?",
                "images": [
                    _img(PNG_BYTES, "image/png"),
                    _img(JPEG_BYTES, "image/jpeg"),
                    _img(WEBP_BYTES, "image/webp"),
                ],
            },
        )
        assert resp.status_code == 200
        host = app.state.host
        assert host.describe_calls == []  # no local describe call
        text, images = host.stashed[0]
        assert [m for _, m in images] == ["image/png", "image/jpeg", "image/webp"]
        assert [b for b, _ in images] == [PNG_BYTES, JPEG_BYTES, WEBP_BYTES]
        queued = host.motor.command_queue.get_nowait()
        assert queued[1] == text
        assert text.startswith("what are these?")
        assert dc.DIRECT_PASTED_NOTE in text
        assert "SECRET" not in text and "base64" not in text
    assert "SECRET" not in caplog.text


def test_chat_route_image_only_direct_turn_has_nonempty_text():
    app = _app(True)
    with TestClient(app) as client:
        resp = client.post("/api/chat/turn", json={"image": _img(PNG_BYTES, "image/png")})
        assert resp.status_code == 200
        text = app.state.host.stashed[0][0]
        assert text.strip() and dc.DIRECT_PASTED_NOTE in text


def test_chat_route_keeps_the_describe_path_when_not_eligible():
    app = _app(False)
    with TestClient(app) as client:
        resp = client.post("/api/chat/turn", json={"text": "hi", "image": _img(PNG_BYTES, "image/png")})
        assert resp.status_code == 200
        host = app.state.host
        assert len(host.describe_calls) == 1 and host.stashed == []


def test_chat_route_without_a_direct_hook_keeps_the_describe_path():
    import opencohost.api.main as main_mod
    from tests.test_api_chat_image import _ImageHost

    app = main_mod.create_app(host_factory=_ImageHost, cors_origins=_ORIGINS)
    with TestClient(app) as client:
        resp = client.post("/api/chat/turn", json={"text": "hi", "image": _img(PNG_BYTES, "image/png")})
        assert resp.status_code == 200
        assert len(app.state.host.describe_calls) == 1


def test_pasted_direct_text_is_stripped_like_a_described_image_for_memory():
    from opencohost.core.memory.pasted_image import strip_pasted_image_block

    text = chat_image.compose_direct_turn_text("my question", 2)
    assert strip_pasted_image_block(text) == "my question"


def test_streaming_cloud_attempt_also_carries_the_image_parts(monkeypatch):
    from tests.test_cloud_streaming import FakeRouter, _cloud_chunk

    motor = _motor()
    monkeypatch.setattr(llm_engine, "LLM_STREAMING_ENABLED", True)
    monkeypatch.setattr(llm_engine, "CLOUD_STREAMING_ENABLED", True)
    motor._speech_router_enabled = True
    router = FakeRouter()
    motor._ensure_router = lambda: router
    motor._speech_router = router
    seen = {}

    def fake_stream(*, is_local, provider_cfg=None, timeout, model, messages, options):
        seen["messages"] = messages
        seen["is_local"] = is_local
        yield _cloud_chunk("Es una hoja de calculo. ")
        yield _cloud_chunk(done=True)

    motor._llm_chat_streaming = fake_stream
    motor._vision = SimpleNamespace(step=_FakeStep(images=((SECRET_PIXELS, "image/jpeg"),)))

    motor._generar_dialogo("what is on my screen?", source="direct", commit_history=True)

    assert seen["is_local"] is False
    content = _last_user({"messages": seen["messages"]})["content"]
    assert [p["type"] for p in content] == ["text", "image_url"]
