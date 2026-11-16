"""Block 4 BE (agenda-deck-variants-and-safety #68): the TTS engine choice
(``motor_tts``) survives a restart, and a failing Piper voice reload never
kills the command loop or persists a voice that did not load.

Heavy TTS ("pesado") is an experimental opt-in (OPENCOHOST_EXPERIMENTAL_TTS=1):
when it is disabled the engine always resolves to "ligero", whatever is on disk.
"""
from __future__ import annotations

import json
import queue
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from opencohost.config import settings


def _make_motor():
    from opencohost.core.llm_engine import MotorVocalIA

    motor = MotorVocalIA(queue.Queue(), lambda event: None)
    motor.ollama = MagicMock()
    motor.pygame = MagicMock()
    motor.is_ready = True
    return motor


@pytest.fixture
def motor_tts_file(tmp_path, monkeypatch):
    path = tmp_path / "config" / "motor_tts.json"
    monkeypatch.setattr(settings, "MOTOR_TTS_FILE", str(path))
    return path


# ---------------------------------------------------------------------------
# settings.load_motor_tts / save_motor_tts
# ---------------------------------------------------------------------------

def test_missing_or_corrupt_file_resolves_to_ligero(tmp_path):
    assert settings.load_motor_tts(config_file=str(tmp_path / "missing.json"), heavy_enabled=True) == "ligero"
    bad = tmp_path / "bad.json"
    bad.write_text("not json", encoding="utf-8")
    assert settings.load_motor_tts(config_file=str(bad), heavy_enabled=True) == "ligero"


def test_pesado_round_trips_only_while_heavy_is_enabled(tmp_path):
    path = str(tmp_path / "motor_tts.json")
    settings.save_motor_tts("pesado", config_file=path)

    assert settings.load_motor_tts(config_file=path, heavy_enabled=True) == "pesado"
    assert settings.load_motor_tts(config_file=path, heavy_enabled=False) == "ligero"


def test_unknown_values_are_never_persisted_or_loaded(tmp_path):
    path = tmp_path / "motor_tts.json"
    settings.save_motor_tts("turbo", config_file=str(path))
    assert not path.exists()

    path.write_text(json.dumps({"motor_tts": "turbo"}), encoding="utf-8")
    assert settings.load_motor_tts(config_file=str(path), heavy_enabled=True) == "ligero"


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

def test_engine_loads_the_persisted_choice_at_startup(motor_tts_file, monkeypatch):
    settings.save_motor_tts("pesado", config_file=str(motor_tts_file))

    monkeypatch.setattr(settings, "EXPERIMENTAL_HEAVY_TTS_ENABLED", True)
    assert _make_motor().motor_tts == "pesado"

    monkeypatch.setattr(settings, "EXPERIMENTAL_HEAVY_TTS_ENABLED", False)
    assert _make_motor().motor_tts == "ligero"


def test_set_motor_tts_persists_valid_choices(motor_tts_file, monkeypatch):
    monkeypatch.setattr(settings, "EXPERIMENTAL_HEAVY_TTS_ENABLED", True)
    motor = _make_motor()

    motor._dispatch_command("set_motor_tts", "pesado")
    assert motor.motor_tts == "pesado"
    assert settings.load_motor_tts(config_file=str(motor_tts_file), heavy_enabled=True) == "pesado"

    motor._dispatch_command("set_motor_tts", "ligero")
    assert motor.motor_tts == "ligero"
    assert settings.load_motor_tts(config_file=str(motor_tts_file), heavy_enabled=True) == "ligero"


@pytest.mark.parametrize("payload, heavy", [("pesado", False), ("turbo", True), (None, True)])
def test_set_motor_tts_rejects_unavailable_or_invalid_choices(motor_tts_file, monkeypatch, payload, heavy):
    monkeypatch.setattr(settings, "EXPERIMENTAL_HEAVY_TTS_ENABLED", heavy)
    motor = _make_motor()

    motor._dispatch_command("set_motor_tts", payload)

    assert motor.motor_tts == "ligero"
    assert not motor_tts_file.exists()


def test_piper_reload_exception_keeps_the_loop_alive_and_persists_nothing():
    motor = _make_motor()
    motor._piper = MagicMock()
    motor._piper.reload.side_effect = RuntimeError("onnx load failed")
    before = motor._piper_voice_key

    with patch("opencohost.core.llm_engine.save_piper_voice") as mock_save:
        motor._dispatch_command("set_piper_voice", "english")

    mock_save.assert_not_called()
    assert motor._piper_voice_key == before


# ---------------------------------------------------------------------------
# GET /api/tts/config
# ---------------------------------------------------------------------------

def test_tts_config_never_reports_pesado_while_heavy_is_disabled(monkeypatch):
    import opencohost.api.main as main_mod
    from opencohost.api.routers import i18n_tts as i18n_tts_mod
    from tests.test_api_phase1 import FakeHost

    monkeypatch.setattr(main_mod, "_host_active", False)
    monkeypatch.setattr(main_mod, "load_piper_voice", lambda **kwargs: "es_kira")
    monkeypatch.setattr(main_mod, "load_tts_local_only", lambda: False)
    monkeypatch.setattr(main_mod, "load_tts_speed", lambda: 1.0)
    monkeypatch.setattr(main_mod, "EXPERIMENTAL_HEAVY_TTS_ENABLED", False)
    monkeypatch.setattr(i18n_tts_mod, "_piper_unavailable_reason", lambda voice_key: None)

    app = main_mod.create_app(host_factory=FakeHost, cors_origins=["http://localhost:5173"])
    with TestClient(app) as client:
        app.state.host.motor.motor_tts = "pesado"
        body = client.get("/api/tts/config").json()

    assert body["heavy_available"] is False
    assert body["engine"] == "ligero"
    main_mod._host_active = False
