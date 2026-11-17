"""runtime-fixes-20261110 T3: an honest vision readiness signal and a bounded
cold-start wait for a pasted image.

Fakes only (the controller harness of ``test_vision_controller``): no Ollama,
no screen. The owner saw ``POST /api/chat/turn failed with 504`` when an image
arrived 13 s after vision was enabled, because the daemon was still starting
and the model had not loaded yet.
"""
from __future__ import annotations

import base64
import logging

import pytest
from fastapi.testclient import TestClient

from opencohost.api import chat_image
from opencohost.core.vision import controller as controller_mod
from opencohost.core.vision.describer import VisionDescribeError
from tests.test_api_chat_image import PNG_BYTES, _app, _image
from tests.test_vision_controller import ON, Harness


# --------------------------------------------------------------- status honesty


def test_ready_requires_the_model_to_be_resident():
    h = Harness()
    h.controller.apply(ON)
    assert h.controller.status()["state"] == "ready"
    assert h.controller.status()["model_loaded"] is True


def test_status_stops_saying_ready_once_the_model_is_confirmed_evicted():
    h = Harness(run_bg=False)
    h.controller.apply(ON)
    h.pending.pop()()
    daemon = h.daemons[0]
    daemon.ps_script = [False, False]  # two consecutive misses = really evicted
    h.clock.now += 60

    h.controller.status()  # first miss: debounced, still ready
    h.clock.now += 60
    status = h.controller.status()

    assert status["state"] == "warming"
    assert status["model_loaded"] is False
    assert len(h.pending) == 1  # a re-warm was kicked: no image needed to recover


def test_status_reports_starting_when_the_daemon_died_behind_our_back():
    h = Harness(run_bg=False)
    h.controller.apply(ON)
    h.pending.pop()()
    daemon = h.daemons[0]
    daemon.state = "failed"
    daemon.loaded = None  # /api/ps unreachable
    h.clock.now += 60

    status = h.controller.status()

    assert status["state"] == "starting"
    assert len(h.pending) == 1


def test_enabling_vision_warms_without_any_image():
    h = Harness(run_bg=False)
    h.controller.apply(ON)
    assert h.controller.status()["state"] == "starting"
    h.pending.pop()()
    assert h.controller.status()["state"] == "ready"
    assert h.daemons[0].warms  # the load-only warm call ran on enable


def test_failed_state_carries_an_enum_reason_never_free_text():
    h = Harness(daemon_kwargs={"state_after_start": "failed", "last_error": "port_in_use"})
    h.controller.apply(ON)
    status = h.controller.status()
    assert status["state"] == "failed"
    assert status["last_error"] == "port_in_use"


# ------------------------------------------------------------ cold-start budget


def test_cold_start_wait_budget_is_forty_seconds():
    assert controller_mod.DEFAULT_COLD_START_WAIT_S == 40.0


def test_describe_image_waits_through_a_cold_start_and_then_describes():
    h = Harness(run_bg=False)
    h.controller.apply(ON)  # state starting, worker pending

    def sleep(_seconds):
        h.sleeps.append(_seconds)
        if len(h.sleeps) == 3 and h.pending:
            h.pending.pop()()  # the daemon came up and the model loaded

    h.controller._sleep = sleep
    assert h.controller.describe_image(b"\x89PNG", "q") == "Barra de vida al 30%."
    assert len(h.sleeps) == 3


def test_describe_image_gives_up_after_the_budget_with_model_warming():
    h = Harness(run_bg=False)
    h.controller.apply(ON)  # never completes
    with pytest.raises(VisionDescribeError) as exc:
        h.controller.describe_image(b"\x89PNG", "q")
    assert exc.value.reason == "unavailable"
    assert exc.value.detail == "model_warming"
    assert sum(h.sleeps) == pytest.approx(controller_mod.DEFAULT_COLD_START_WAIT_S)


def test_describe_error_exposes_its_detail_code():
    assert VisionDescribeError("unavailable", "daemon_not_ready").detail == "daemon_not_ready"
    assert VisionDescribeError("timeout").detail == ""


# ------------------------------------------------------------ structured errors


@pytest.mark.parametrize(
    "error,status,code",
    [
        (VisionDescribeError("unavailable", "model_warming"), 503, "vision_starting"),
        (VisionDescribeError("unavailable", "daemon_not_ready"), 503, "vision_failed"),
        (VisionDescribeError("unavailable", "model_missing"), 503, "vision_failed"),
        (VisionDescribeError("unavailable", "vision_off"), 422, "vision_off"),
        (VisionDescribeError("unavailable", "vision_not_wired"), 422, "vision_off"),
        (VisionDescribeError("timeout"), 504, "vision_describe_timeout"),
        (VisionDescribeError("unavailable"), 503, "vision_describe_unavailable"),
        (VisionDescribeError("http", "status=500"), 502, "vision_describe_http"),
    ],
)
def test_describe_failures_map_to_stable_codes_and_enqueue_nothing(error, status, code):
    app = _app()
    with TestClient(app) as client:
        app.state.host.describe_error = error
        resp = client.post("/api/chat/turn", json={"text": "x", "image": _image()})
        assert resp.status_code == status
        assert resp.json()["detail"] == code
        assert app.state.host.motor.command_queue.empty()


def test_vision_off_at_the_door_keeps_its_existing_code():
    app = _app()
    with TestClient(app) as client:
        app.state.host.status = {"enabled": False}
        resp = client.post("/api/chat/turn", json={"text": "x", "image": _image()})
        assert resp.status_code == 422
        assert resp.json()["detail"] == "vision_disabled"
        assert app.state.host.motor.command_queue.empty()


def test_failure_log_carries_the_code_and_no_image_content(caplog):
    caplog.set_level(logging.INFO)
    app = _app()
    with TestClient(app) as client:
        app.state.host.describe_error = VisionDescribeError("unavailable", "model_warming")
        client.post("/api/chat/turn", json={"text": "secret words", "image": _image()})
    assert "[CHAT_IMAGE] failed code=vision_starting" in caplog.text
    assert base64.b64encode(PNG_BYTES).decode()[:40] not in caplog.text
    assert "secret words" not in caplog.text


def test_code_table_is_the_documented_one():
    assert chat_image.describe_failure_code(VisionDescribeError("unavailable", "model_warming")) == (503, "vision_starting")
