"""voice-catalog-selection-20261118 T3: engine Piper switch, per-locale startup, locale change.

No real model is loaded: ``motor._piper`` is a fake and catalog voices are synthetic files.
"""
from __future__ import annotations

import json
import queue
from unittest.mock import MagicMock

import pytest

from opencohost.config import settings
from opencohost.core.speech import piper_catalog


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolated voice folder (saved as the catalog folder) and per-locale voice file."""
    monkeypatch.setattr(settings, "PIPER_VOICE_DIR_FILE", str(tmp_path / "cfg" / "piper_voice_dir.json"))
    monkeypatch.delenv("OPENCOHOST_PIPER_VOICE_DIR", raising=False)
    folder = tmp_path / "voices"
    folder.mkdir()
    piper_catalog.save_voice_dir(folder)
    vfile = tmp_path / "piper_voice.json"
    monkeypatch.setattr(settings, "PIPER_VOICE_FILE", str(vfile))

    def add(name, lang="es_ES", speakers=1):
        (folder / name).write_bytes(b"x")
        (folder / (name + ".json")).write_text(
            json.dumps({"language": {"code": lang}, "num_speakers": speakers, "audio": {"sample_rate": 22050}})
        )

    class Env:
        pass

    e = Env()
    e.folder, e.vfile, e.add = folder, vfile, add
    e.saved = lambda locale: settings.load_piper_voice_for_locale(locale, config_file=str(vfile))
    return e


def _fake_piper(reload_ok=True, last_error=None):
    piper = MagicMock()
    piper.reload.return_value = reload_ok
    piper.last_error = last_error
    piper.speaker_id = None
    return piper


def _motor(locale="es"):
    from opencohost.core.llm_engine import MotorVocalIA
    from opencohost.i18n import active
    from opencohost.i18n.startup import resolve_active_bundle

    active.set_active_bundle(resolve_active_bundle(locale=locale))
    events = []
    motor = MotorVocalIA(queue.Queue(), events.append)
    motor.ollama = MagicMock()
    motor.pygame = MagicMock()
    motor.is_ready = True
    motor._events = events
    return motor


@pytest.fixture(autouse=True)
def _reset_locale():
    yield
    from opencohost.i18n import active

    active.reset_active_bundle()


# ---------------------------------------------------------------------------
# set_piper_voice
# ---------------------------------------------------------------------------


def test_catalog_id_with_speaker_switches_persists_and_signals(env):
    env.add("multi.onnx", speakers=4)
    motor = _motor("es")
    motor._piper = _fake_piper()
    motor._dispatch_command("set_piper_voice", {"value": "multi.onnx", "speaker_id": 2})

    path, = motor._piper.reload.call_args.args
    assert path.endswith("multi.onnx") and str(env.folder.resolve()) in path
    assert motor._piper.reload.call_args.kwargs == {"speaker_id": 2}
    assert motor._piper_voice_key == "multi.onnx"
    assert env.saved("es") == {"voice": "multi.onnx", "speaker_id": 2}
    assert "piper_voice_switch_applied" in motor._events
    assert "piper_voice_switch_failed" not in motor._events
    assert motor._last_piper_switch_failure is None


def test_legacy_registry_key_still_works(env):
    motor = _motor("es")
    motor._piper = _fake_piper()
    motor._dispatch_command("set_piper_voice", "english")
    assert motor._piper.reload.call_args.args[0] == settings.piper_voice_path("english")
    assert motor._piper.reload.call_args.kwargs == {"speaker_id": None}
    assert motor._piper_voice_key == "english"
    assert env.saved("es") == {"voice": "english", "speaker_id": None}


def test_switch_writes_only_the_active_locale_slot(env):
    env.add("a.onnx", lang="en_US")
    settings.save_piper_voice_for_locale("es", "neutral", None)
    motor = _motor("en")
    motor._piper = _fake_piper()
    motor._dispatch_command("set_piper_voice", "a.onnx")
    assert env.saved("en")["voice"] == "a.onnx"
    assert env.saved("es") == {"voice": "neutral", "speaker_id": None}


@pytest.mark.parametrize(
    "value, reason",
    [("../evil.onnx", "invalid_voice_id"), ("C:\\x.onnx", "invalid_voice_id"), ("ghost.onnx", "voice_not_found"), ("notes.txt", "invalid_voice_id")],
)
def test_unresolvable_voice_keeps_everything_and_signals_reason(env, value, reason):
    settings.save_piper_voice_for_locale("es", "neutral", 1)
    motor = _motor("es")
    motor._piper = _fake_piper()
    before = motor._piper_voice_key
    motor._dispatch_command("set_piper_voice", value)
    motor._piper.reload.assert_not_called()
    assert motor._piper_voice_key == before
    assert env.saved("es") == {"voice": "neutral", "speaker_id": 1}
    assert "piper_voice_switch_failed" in motor._events
    assert "piper_voice_switch_applied" not in motor._events
    assert motor._last_piper_switch_failure == {"requested": value, "reason": reason}


@pytest.mark.parametrize(
    "reload_ok, error, expected",
    [(False, "speaker_out_of_range", "speaker_out_of_range"), (False, "load_failed", "load_failed"), (False, None, "load_failed")],
)
def test_reload_failure_keeps_voice_does_not_persist(env, reload_ok, error, expected):
    env.add("v.onnx")
    motor = _motor("es")
    motor._piper = _fake_piper(reload_ok=reload_ok, last_error=error)
    before = motor._piper_voice_key
    motor._dispatch_command("set_piper_voice", {"value": "v.onnx", "speaker_id": 9})
    assert motor._piper_voice_key == before
    assert env.saved("es") is None
    assert not env.vfile.exists()
    assert motor._events.count("piper_voice_switch_failed") == 1
    assert motor._last_piper_switch_failure["reason"] == expected


def test_reload_exception_is_a_load_failure(env):
    env.add("v.onnx")
    motor = _motor("es")
    motor._piper = _fake_piper()
    motor._piper.reload.side_effect = RuntimeError("onnx exploded")
    motor._dispatch_command("set_piper_voice", "v.onnx")
    assert motor._last_piper_switch_failure["reason"] == "load_failed"
    assert env.saved("es") is None


@pytest.mark.parametrize("speaker", ["2", -1, True, 1.5])
def test_invalid_speaker_value_is_rejected_before_reload(env, speaker):
    env.add("v.onnx")
    motor = _motor("es")
    motor._piper = _fake_piper()
    motor._dispatch_command("set_piper_voice", {"value": "v.onnx", "speaker_id": speaker})
    motor._piper.reload.assert_not_called()
    assert motor._last_piper_switch_failure["reason"] == "invalid_speaker_id"


def test_success_clears_previous_failure(env):
    env.add("v.onnx")
    motor = _motor("es")
    motor._piper = _fake_piper()
    motor._dispatch_command("set_piper_voice", "ghost.onnx")
    assert motor._last_piper_switch_failure is not None
    motor._dispatch_command("set_piper_voice", "v.onnx")
    assert motor._last_piper_switch_failure is None


def test_voice_language_mismatch_uses_catalog_language(env):
    env.add("v.onnx", lang="en_US")
    motor = _motor("es")
    motor._piper = _fake_piper()
    motor._dispatch_command("set_piper_voice", "v.onnx")
    assert motor._active_piper_voice_lang() == "en"


# ---------------------------------------------------------------------------
# startup
# ---------------------------------------------------------------------------


def test_startup_uses_saved_voice_of_the_active_locale(env):
    env.add("mine.onnx", lang="en_US", speakers=3)
    settings.save_piper_voice_for_locale("en", "mine.onnx", 2)
    motor = _motor("en")
    assert motor._piper_voice_key == "mine.onnx"
    assert motor._piper.speaker_id == 2
    assert motor._piper._model_path.endswith("mine.onnx")


def test_startup_other_locale_slot_is_ignored_and_never_overwritten(env):
    settings.save_piper_voice_for_locale("es", "neutral", None)
    before = env.vfile.read_text(encoding="utf-8")
    motor = _motor("en")
    assert motor._piper_voice_key == "english"  # locale default
    assert env.vfile.read_text(encoding="utf-8") == before


def test_startup_legacy_file_other_language_is_not_rewritten(env):
    env.vfile.write_text(json.dumps({"piper_voice": "neutral"}), encoding="utf-8")
    before = env.vfile.read_text(encoding="utf-8")
    motor = _motor("en")
    assert motor._piper_voice_key == "english"
    assert env.vfile.read_text(encoding="utf-8") == before


def test_startup_saved_voice_missing_on_disk_uses_default_without_overwrite(env):
    settings.save_piper_voice_for_locale("en", "gone.onnx", 1)
    before = env.vfile.read_text(encoding="utf-8")
    motor = _motor("en")
    assert motor._piper_voice_key == "english"
    assert motor._piper._speaker_id is None
    assert env.vfile.read_text(encoding="utf-8") == before


def test_startup_load_failure_falls_back_to_default_without_overwrite(env):
    env.add("bad.onnx", lang="en_US")
    settings.save_piper_voice_for_locale("en", "bad.onnx", None)
    before = env.vfile.read_text(encoding="utf-8")
    motor = _motor("en")
    assert motor._piper_voice_key == "bad.onnx"
    calls = []
    motor._piper = MagicMock()
    motor._piper.load.return_value = False
    motor._piper.reload.side_effect = lambda path, speaker_id=None: calls.append(path) or True
    motor._piper_startup_fallback()
    assert calls == [settings.piper_voice_path("english")]
    assert motor._piper_voice_key == "english"
    assert env.vfile.read_text(encoding="utf-8") == before


# ---------------------------------------------------------------------------
# locale change
# ---------------------------------------------------------------------------


def test_locale_change_keeps_saved_voice_of_that_locale(env, tmp_path, monkeypatch):
    from opencohost.i18n import state

    _isolate_profiles(tmp_path, monkeypatch)
    settings.save_piper_voice_for_locale("en", "mine.onnx", 3)
    state.set_locale("en", tmp_path / "locale.json")
    assert env.saved("en") == {"voice": "mine.onnx", "speaker_id": 3}


def test_locale_change_fills_a_missing_slot_with_the_default(env, tmp_path, monkeypatch):
    from opencohost.i18n import state

    _isolate_profiles(tmp_path, monkeypatch)
    settings.save_piper_voice_for_locale("es", "mine.onnx", 3)
    state.set_locale("en", tmp_path / "locale.json")
    assert env.saved("en") == {"voice": "english", "speaker_id": None}
    assert env.saved("es") == {"voice": "mine.onnx", "speaker_id": 3}


def _isolate_profiles(tmp_path, monkeypatch):
    import opencohost.core.profiles.profiles as prof_mod

    monkeypatch.setattr(prof_mod, "PROFILES_FILE", str(tmp_path / "perfiles.json"))
    monkeypatch.setattr(settings, "LAST_PROFILE_FILE", str(tmp_path / "last_profile.json"))


# ---------------------------------------------------------------------------
# API surface
# ---------------------------------------------------------------------------


def test_new_switch_events_are_whitelisted_for_the_event_stream():
    from opencohost.api import engine_host

    assert {"piper_voice_switch_applied", "piper_voice_switch_failed"} <= engine_host._MOTOR_EVENT_WHITELIST


@pytest.mark.parametrize(
    "wire, engine_payload",
    [
        ({"value": "es_kira"}, "es_kira"),
        ({"value": "v.onnx", "speaker_id": 2}, {"value": "v.onnx", "speaker_id": 2}),
        ({"value": "v.onnx", "speaker_id": None}, "v.onnx"),
    ],
)
def test_command_payload_translation(wire, engine_payload):
    from opencohost.api.routers import chat

    assert chat._engine_command_payload("set_piper_voice", wire) == engine_payload


@pytest.mark.parametrize("wire", [{"value": "v.onnx", "speaker_id": "2"}, {"value": "v.onnx", "speaker_id": -1}, {"value": "v.onnx", "speaker_id": True}])
def test_command_rejects_bad_speaker_at_the_boundary(wire):
    from opencohost.api.routers import chat

    payload = chat._engine_command_payload("set_piper_voice", wire)
    assert chat._validate_command_value("set_piper_voice", payload) is not None


def test_tts_config_reports_real_engine_voice_and_speaker(env, monkeypatch):
    from fastapi.testclient import TestClient

    import opencohost.api.main as main_mod
    from tests.test_api_phase1 import FakeHost

    monkeypatch.setattr(main_mod, "_host_active", False)
    app = main_mod.create_app(host_factory=FakeHost, cors_origins=["http://localhost:5173"])
    with TestClient(app) as client:
        motor = app.state.host.motor
        motor._piper_voice_key = "multi.onnx"
        motor._piper = MagicMock()
        motor._piper.speaker_id = 3
        motor._piper.is_available.return_value = True
        motor._last_piper_switch_failure = {"requested": "x.onnx", "reason": "voice_not_found"}
        body = client.get("/api/tts/config").json()
    main_mod._host_active = False
    assert body["piper_voice_id"] == "multi.onnx"
    assert body["piper_speaker_id"] == 3
    assert body["piper_switch_failure_reason"] == "voice_not_found"


# ---------------------------------------------------------------------------
# T3a follow-ups
# ---------------------------------------------------------------------------


def test_speaker_id_above_cap_is_rejected_by_engine(env):
    env.add("multi.onnx", speakers=4)
    motor = _motor("es")
    motor._piper = _fake_piper()
    motor._dispatch_command("set_piper_voice", {"value": "multi.onnx", "speaker_id": 4097})
    motor._piper.reload.assert_not_called()
    assert motor._last_piper_switch_failure["reason"] == "invalid_speaker_id"
    assert "piper_voice_switch_failed" in motor._events


def test_speaker_id_above_cap_is_rejected_by_router_validation():
    from opencohost.api.routers.chat import _validate_command_value

    assert _validate_command_value("set_piper_voice", {"value": "a.onnx", "speaker_id": 4096}) is None
    assert _validate_command_value("set_piper_voice", {"value": "a.onnx", "speaker_id": 4097}) is not None


def test_startup_fallback_failure_stops_reporting_the_unloaded_saved_voice(env):
    env.add("bad.onnx", lang="en_US")
    settings.save_piper_voice_for_locale("en", "bad.onnx", 2)
    motor = _motor("en")
    assert motor._piper_voice_key == "bad.onnx"
    motor._piper = _fake_piper(reload_ok=False)
    motor._piper.speaker_id = 2
    motor._piper_startup_fallback()
    assert motor._piper_voice_key == "english"  # the locale default, not the unloaded saved voice
    assert motor._piper_catalog_lang is None
    motor._piper.set_speaker_id.assert_called_with(None)
    assert env.saved("en") == {"voice": "bad.onnx", "speaker_id": 2}  # disk untouched
