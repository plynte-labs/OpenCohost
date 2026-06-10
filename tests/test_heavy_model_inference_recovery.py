"""Regression specs for stuck heavy-model inference recovery.

These tests intentionally describe the desired recovery contract before the
implementation exists. They are marked xfail so the repo can keep moving while
the bug track is documented and prioritized.
"""

from __future__ import annotations

import json
import os
import queue
import threading
import time
from unittest.mock import MagicMock

import pytest

from config.settings import DEFAULT_MODEL


def _make_motor(*, last_model_data=None, tmp_dir=None):
    """Create a MotorVocalIA with mocked dependencies."""
    import config.settings as settings

    original_last_model_file = settings.LAST_MODEL_FILE
    if tmp_dir is not None:
        settings.LAST_MODEL_FILE = os.path.join(tmp_dir, "last_model.json")

    if last_model_data is not None and tmp_dir is not None:
        with open(settings.LAST_MODEL_FILE, "w", encoding="utf-8") as handle:
            json.dump(last_model_data, handle)

    log_queue = queue.Queue()
    ui_events = []

    def ui_callback(event):
        ui_events.append(event)

    try:
        from core.llm_engine import MotorVocalIA

        motor = MotorVocalIA(log_queue, ui_callback)
    finally:
        if tmp_dir is not None:
            settings.LAST_MODEL_FILE = original_last_model_file

    motor.ollama = MagicMock()
    motor.pygame = MagicMock()
    motor.is_ready = True
    return motor, log_queue, ui_events


def _wait_until(predicate, *, timeout=1.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_hung_first_inference_clears_processing_state_after_timeout(tmp_path):
    motor, _, ui_events = _make_motor(tmp_dir=str(tmp_path))
    motor.current_model = "qwopus"
    motor._desired_model = "qwopus"
    motor._loaded_model = "qwopus"
    motor._warmed_model = "qwopus"
    motor._awaiting_first_success_after_switch = True
    motor._post_switch_watchdog_timeout = 0.1

    release = threading.Event()

    def fake_ollama_chat(**kwargs):
        release.wait(timeout=5.0)
        return {"message": {"content": "Respuesta tardia"}}

    motor._ollama_chat = MagicMock(side_effect=fake_ollama_chat)

    worker = threading.Thread(
        target=lambda: motor._dispatch_command("process_context", "hola"),
        daemon=True,
    )
    worker.start()

    try:
        assert _wait_until(lambda: motor.is_processing, timeout=0.3)
        assert "processing" in ui_events
        assert _wait_until(lambda: not motor.is_processing, timeout=0.5)
        assert "llm_timeout_recovered" in ui_events
        assert motor._last_llm_failure["reason"] == "watchdog_timeout"
    finally:
        release.set()
        worker.join(timeout=1.0)


def test_hung_inference_allows_switch_back_to_last_known_good_model(tmp_path):
    motor, _, ui_events = _make_motor(tmp_dir=str(tmp_path))
    motor.current_model = "qwopus"
    motor._desired_model = "qwopus"
    motor._loaded_model = "qwopus"
    motor._warmed_model = "qwopus"
    motor._last_known_good_model = DEFAULT_MODEL
    motor._awaiting_first_success_after_switch = True
    motor._post_switch_watchdog_timeout = 0.1
    motor._hablar = MagicMock()

    release = threading.Event()
    call_count = {"value": 0}

    def fake_ollama_chat(**kwargs):
        call_count["value"] += 1
        if call_count["value"] == 1:
            release.wait(timeout=5.0)
        return {"message": {"content": "Respuesta tardia"}}

    motor._ollama_chat = MagicMock(side_effect=fake_ollama_chat)
    motor.ollama.generate = MagicMock()

    worker = threading.Thread(
        target=lambda: motor._dispatch_command("process_context", "hola"),
        daemon=True,
    )
    worker.start()

    try:
        assert _wait_until(lambda: motor.is_processing, timeout=0.3)
        motor._dispatch_command("switch_model", DEFAULT_MODEL)
        assert _wait_until(lambda: motor.current_model == DEFAULT_MODEL, timeout=0.6)
        assert "model_switch_applied" in ui_events
    finally:
        release.set()
        worker.join(timeout=1.0)


def test_follow_up_input_does_not_remain_trapped_forever_after_hung_request(tmp_path):
    motor, _, _ = _make_motor(tmp_dir=str(tmp_path))
    motor.current_model = "qwopus"
    motor._desired_model = "qwopus"
    motor._loaded_model = "qwopus"
    motor._warmed_model = "qwopus"
    motor._last_known_good_model = DEFAULT_MODEL
    motor._awaiting_first_success_after_switch = True
    motor._post_switch_watchdog_timeout = 0.1
    motor._hablar = MagicMock()

    release = threading.Event()
    call_count = {"value": 0}

    def fake_ollama_chat(**kwargs):
        call_count["value"] += 1
        if call_count["value"] == 1:
            release.wait(timeout=5.0)
        return {"message": {"content": "Respuesta tardia"}}

    motor._ollama_chat = MagicMock(side_effect=fake_ollama_chat)

    worker = threading.Thread(
        target=lambda: motor._dispatch_command("process_context", "primer mensaje"),
        daemon=True,
    )
    worker.start()

    try:
        assert _wait_until(lambda: motor.is_processing, timeout=0.3)
        motor._dispatch_command("process_context", "segundo mensaje")
        assert _wait_until(lambda: call_count["value"] >= 2, timeout=1.0)
        assert _wait_until(lambda: len(motor._priority_queue) == 0, timeout=1.5)
        assert _wait_until(lambda: not motor.is_processing, timeout=2.0)
    finally:
        release.set()
        worker.join(timeout=1.0)


def test_hung_first_inference_rolls_back_to_last_known_good_model(tmp_path):
    motor, _, ui_events = _make_motor(tmp_dir=str(tmp_path))
    motor.current_model = "qwopus"
    motor._desired_model = "qwopus"
    motor._loaded_model = "qwopus"
    motor._warmed_model = "qwopus"
    motor._last_known_good_model = DEFAULT_MODEL
    motor._awaiting_first_success_after_switch = True
    motor._post_switch_watchdog_timeout = 0.1

    release = threading.Event()

    def fake_ollama_chat(**kwargs):
        release.wait(timeout=5.0)
        return {"message": {"content": "Respuesta tardia"}}

    motor._ollama_chat = MagicMock(side_effect=fake_ollama_chat)
    motor.ollama.generate = MagicMock()

    worker = threading.Thread(
        target=lambda: motor._dispatch_command("process_context", "hola"),
        daemon=True,
    )
    worker.start()

    try:
        assert _wait_until(lambda: motor.current_model == DEFAULT_MODEL, timeout=0.6)
        assert "model_switch_applied" in ui_events
        assert motor._desired_model == DEFAULT_MODEL
    finally:
        release.set()
        worker.join(timeout=1.0)


def test_successful_first_response_marks_new_model_as_known_good(tmp_path):
    motor, _, _ = _make_motor(tmp_dir=str(tmp_path))
    motor.current_model = "gemma4:12b"
    motor._desired_model = "gemma4:12b"
    motor._loaded_model = "gemma4:12b"
    motor._warmed_model = "gemma4:12b"
    motor._last_known_good_model = DEFAULT_MODEL
    motor._awaiting_first_success_after_switch = True
    motor._post_switch_watchdog_timeout = 0.2
    motor.use_system_role = True
    motor._ollama_chat = MagicMock(return_value={"message": {"content": "Respuesta sana"}})

    dialogo = motor._generar_dialogo("hola", source="direct", commit_history=False)

    assert dialogo == "Respuesta sana"
    assert motor._last_known_good_model == "gemma4:12b"
    assert motor._awaiting_first_success_after_switch is False
