"""voice-catalog-selection-20261118 T4: per-locale Edge voice override, validation, engine switch.

No network: ``edge_tts`` and the voice catalog cache are faked.
"""
from __future__ import annotations

import json
import queue
import threading
import time
import types
from unittest.mock import MagicMock

import pytest

from opencohost.config import settings
from opencohost.core import llm_engine
from opencohost.core.speech import edge_voices


@pytest.fixture
def efile(tmp_path, monkeypatch):
    path = tmp_path / "edge_voice.json"
    monkeypatch.setattr(settings, "EDGE_VOICE_FILE", str(path))
    monkeypatch.setattr(settings, "PIPER_VOICE_FILE", str(tmp_path / "piper_voice.json"))
    # A fresh catalog with no cache and no network: peek() is None unless a test seeds it.
    monkeypatch.setattr(edge_voices, "DEFAULT_CATALOG", edge_voices.EdgeVoiceCatalog(fetch=lambda: []))
    return path


@pytest.fixture(autouse=True)
def _reset_locale():
    yield
    from opencohost.i18n import active

    active.reset_active_bundle()


def _motor(locale="es"):
    from opencohost.i18n import active
    from opencohost.i18n.startup import resolve_active_bundle

    active.set_active_bundle(resolve_active_bundle(locale=locale))
    events = []
    motor = llm_engine.MotorVocalIA(queue.Queue(), events.append)
    motor.ollama = MagicMock()
    motor.pygame = MagicMock()
    motor.pygame.mixer.music.get_busy.return_value = False
    motor.is_ready = True
    motor._piper = MagicMock()
    motor._events = events
    return motor


def _seed_catalog(ids):
    raw = [{"ShortName": i, "Locale": "-".join(i.split("-")[:2]), "FriendlyName": i} for i in ids]
    edge_voices.DEFAULT_CATALOG = edge_voices.EdgeVoiceCatalog(fetch=lambda: raw)
    edge_voices.DEFAULT_CATALOG.get(None)  # fills the cache


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "voice",
    ["es-MX-DaliaNeural", "en-US-AriaNeural", "zh-CN-liaoning-XiaobeiNeural", "fil-PH-AngeloNeural",
     "en-US-EmmaMultilingualNeural", "cy-GB-NiaNeural"],
)
def test_real_short_names_are_valid(voice):
    assert settings.is_valid_edge_voice(voice) is True


@pytest.mark.parametrize(
    "voice",
    ["", " ", "es-MX-Dalia", "../evil", "es-MX-DaliaNeural; rm", "es-MX-Dalia Neural", "ES-mx-DaliaNeural",
     "es-MX-DaliaNeural\n", "x" * 200 + "Neural", "es_MX_DaliaNeural", None, 5, ["es-MX-DaliaNeural"]],
)
def test_malformed_short_names_are_invalid(voice):
    assert settings.is_valid_edge_voice(voice) is False


# ---------------------------------------------------------------------------
# persistence
# ---------------------------------------------------------------------------


def test_round_trip_and_per_locale_isolation(efile):
    assert settings.load_edge_voice_for_locale("es") is None
    assert settings.save_edge_voice_for_locale("es", "es-MX-JorgeNeural") is True
    assert settings.save_edge_voice_for_locale("en-US", "en-US-GuyNeural") is True
    assert settings.load_edge_voice_for_locale("es") == "es-MX-JorgeNeural"
    assert settings.load_edge_voice_for_locale("EN") == "en-US-GuyNeural"
    assert set(json.loads(efile.read_text(encoding="utf-8"))["by_locale"]) == {"es", "en"}


def test_save_rejects_invalid_and_keeps_file(efile):
    settings.save_edge_voice_for_locale("es", "es-MX-JorgeNeural")
    before = efile.read_text(encoding="utf-8")
    assert settings.save_edge_voice_for_locale("es", "bogus") is False
    assert settings.save_edge_voice_for_locale("", "es-MX-JorgeNeural") is False
    assert efile.read_text(encoding="utf-8") == before


@pytest.mark.parametrize("content", ["not json", "[]", '{"by_locale": []}', '{"by_locale": {"es": 5}}',
                                     '{"by_locale": {"es": "bogus"}}'])
def test_corrupt_or_invalid_file_means_no_override(efile, content):
    efile.write_text(content, encoding="utf-8")
    assert settings.load_edge_voice_for_locale("es") is None


def test_concurrent_saves_never_lose_an_update(efile, monkeypatch):
    real_read = settings._read_edge_voices_by_locale

    def slow_read(path):
        data = real_read(path)
        time.sleep(0.05)
        return data

    monkeypatch.setattr(settings, "_read_edge_voices_by_locale", slow_read)
    threads = [
        threading.Thread(target=settings.save_edge_voice_for_locale, args=(loc, f"{loc}-XX-AnaNeural"))
        for loc in ("es", "en", "fr", "de")
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert set(json.loads(efile.read_text(encoding="utf-8"))["by_locale"]) == {"es", "en", "fr", "de"}


# ---------------------------------------------------------------------------
# catalog peek (no network on the command path)
# ---------------------------------------------------------------------------


def test_peek_never_fetches():
    calls = []
    cat = edge_voices.EdgeVoiceCatalog(fetch=lambda: calls.append(1) or [])
    assert cat.peek() is None
    assert calls == []


def test_peek_does_not_wait_for_an_in_flight_fetch():
    import threading
    import time

    raw = [{"ShortName": "es-MX-DaliaNeural", "Locale": "es-MX"}]
    started = threading.Event()
    release = threading.Event()
    state = {"n": 0}

    def fetch():
        state["n"] += 1
        if state["n"] == 1:
            return raw
        started.set()
        release.wait(5)
        return raw

    cat = edge_voices.EdgeVoiceCatalog(fetch=fetch, ttl_s=0)
    cat.get(None)  # first fetch fills the cache; ttl 0 makes the next get refetch
    worker = threading.Thread(target=cat.get, args=(None,))
    worker.start()
    try:
        assert started.wait(5)
        t0 = time.monotonic()
        result = cat.peek()
        elapsed = time.monotonic() - t0
    finally:
        release.set()
        worker.join(5)
    assert result == {"es-MX-DaliaNeural"}
    assert elapsed < 0.5


def test_peek_returns_cached_ids_even_when_stale():
    raw = [{"ShortName": "es-MX-DaliaNeural", "Locale": "es-MX"}]
    cat = edge_voices.EdgeVoiceCatalog(fetch=lambda: raw, ttl_s=0)
    cat.get(None)
    assert cat.peek() == {"es-MX-DaliaNeural"}


# ---------------------------------------------------------------------------
# engine
# ---------------------------------------------------------------------------


def test_no_override_uses_bundle_voice_and_writes_nothing_at_startup(efile):
    motor = _motor("es")
    assert motor.effective_edge_voice() == "es-MX-DaliaNeural"
    assert not efile.exists()


def test_startup_reads_only_the_active_locale_override(efile):
    settings.save_edge_voice_for_locale("es", "es-MX-JorgeNeural")
    settings.save_edge_voice_for_locale("en", "en-US-GuyNeural")
    assert _motor("es").effective_edge_voice() == "es-MX-JorgeNeural"
    assert _motor("en").effective_edge_voice() == "en-US-GuyNeural"


def test_switch_applies_persists_and_signals(efile):
    motor = _motor("es")
    motor._dispatch_command("set_edge_voice", "es-MX-JorgeNeural")
    assert motor.effective_edge_voice() == "es-MX-JorgeNeural"
    assert settings.load_edge_voice_for_locale("es") == "es-MX-JorgeNeural"
    assert "edge_voice_switch_applied" in motor._events
    assert "edge_voice_switch_failed" not in motor._events
    assert motor._last_edge_switch_failure is None


def test_switch_writes_only_the_active_locale(efile):
    settings.save_edge_voice_for_locale("en", "en-US-GuyNeural")
    motor = _motor("es")
    motor._dispatch_command("set_edge_voice", "es-MX-JorgeNeural")
    assert settings.load_edge_voice_for_locale("en") == "en-US-GuyNeural"


@pytest.mark.parametrize("value", ["", "bogus", "../x", None, 7, "es-MX-Dalia"])
def test_invalid_voice_keeps_everything_and_signals(efile, value):
    settings.save_edge_voice_for_locale("es", "es-MX-JorgeNeural")
    motor = _motor("es")
    before = efile.read_text(encoding="utf-8")
    motor._dispatch_command("set_edge_voice", value)
    assert motor.effective_edge_voice() == "es-MX-JorgeNeural"
    assert efile.read_text(encoding="utf-8") == before
    assert "edge_voice_switch_failed" in motor._events
    assert motor._last_edge_switch_failure["reason"] == "invalid_edge_voice"


def test_voice_missing_from_a_real_catalog_cache_is_rejected(efile):
    _seed_catalog(["es-MX-DaliaNeural", "es-MX-JorgeNeural"])
    motor = _motor("es")
    motor._dispatch_command("set_edge_voice", "es-MX-GhostNeural")
    assert motor._last_edge_switch_failure["reason"] == "edge_voice_unknown"
    assert motor.effective_edge_voice() == "es-MX-DaliaNeural"
    assert not efile.exists()


def test_voice_present_in_catalog_cache_is_accepted(efile):
    _seed_catalog(["es-MX-DaliaNeural", "es-MX-JorgeNeural"])
    motor = _motor("es")
    motor._dispatch_command("set_edge_voice", "es-MX-JorgeNeural")
    assert motor.effective_edge_voice() == "es-MX-JorgeNeural"


def test_no_cache_accepts_a_well_formed_voice_without_network(efile):
    motor = _motor("es")
    motor._dispatch_command("set_edge_voice", "es-MX-JorgeNeural")  # a catalog fetch would return []
    assert motor.effective_edge_voice() == "es-MX-JorgeNeural"


def test_voice_of_another_language_is_allowed(efile):
    motor = _motor("es")
    motor._dispatch_command("set_edge_voice", "en-US-GuyNeural")
    assert motor.effective_edge_voice() == "en-US-GuyNeural"
    assert "edge_voice_switch_applied" in motor._events


def test_failed_save_keeps_old_voice_and_signals(efile, monkeypatch):
    motor = _motor("es")
    monkeypatch.setattr(llm_engine, "save_edge_voice_for_locale", lambda *a, **k: False)
    motor._dispatch_command("set_edge_voice", "es-MX-JorgeNeural")
    assert motor.effective_edge_voice() == "es-MX-DaliaNeural"
    assert motor._last_edge_switch_failure["reason"] == "config_write_failed"
    assert "edge_voice_switch_failed" in motor._events


def test_success_clears_previous_failure(efile):
    motor = _motor("es")
    motor._dispatch_command("set_edge_voice", "bogus")
    motor._dispatch_command("set_edge_voice", "es-MX-JorgeNeural")
    assert motor._last_edge_switch_failure is None


def test_speech_path_uses_override_for_the_next_sentence(efile, monkeypatch):
    motor = _motor("es")
    motor.tts_local_only = False
    motor._edge_tts_offline = False
    motor.motor_tts = "ligero"
    monkeypatch.setattr(llm_engine, "save_tts_speed", lambda *_a, **_k: None)
    voices = []

    class FakeCommunicate:
        def __init__(self, text, voice, **kw):
            voices.append(voice)

        async def save(self, path):
            open(path, "wb").close()

    monkeypatch.setattr(llm_engine, "edge_tts", types.SimpleNamespace(Communicate=FakeCommunicate))
    motor._hablar("primera frase de prueba corta", source="direct")
    motor._dispatch_command("set_edge_voice", "es-MX-JorgeNeural")
    motor._hablar("segunda frase de prueba corta", source="direct")
    assert voices == ["es-MX-DaliaNeural", "es-MX-JorgeNeural"]


# ---------------------------------------------------------------------------
# API surface
# ---------------------------------------------------------------------------


def test_events_are_whitelisted():
    from opencohost.api import engine_host

    assert {"edge_voice_switch_applied", "edge_voice_switch_failed"} <= engine_host._MOTOR_EVENT_WHITELIST


def test_command_is_whitelisted_and_validated():
    from opencohost.api.routers import chat

    assert "set_edge_voice" in chat._COMMAND_WHITELIST
    assert chat._engine_command_payload("set_edge_voice", {"value": "es-MX-JorgeNeural"}) == "es-MX-JorgeNeural"
    assert chat._validate_command_value("set_edge_voice", "es-MX-JorgeNeural") is None
    assert chat._validate_command_value("set_edge_voice", None) is not None
    assert chat._validate_command_value("set_edge_voice", 5) is not None
    assert chat._validate_command_value("set_edge_voice", "x" * 500) is not None


def test_command_is_drain_safe_and_deferred_during_speech():
    assert "set_edge_voice" in llm_engine._DRAIN_SAFE_COMMANDS
    assert "set_edge_voice" in llm_engine._SPEECH_DEFER_COMMANDS


def _config(monkeypatch, motor_setup):
    from fastapi.testclient import TestClient

    import opencohost.api.main as main_mod
    from tests.test_api_phase1 import FakeHost

    monkeypatch.setattr(main_mod, "_host_active", False)
    app = main_mod.create_app(host_factory=FakeHost, cors_origins=["http://localhost:5173"])
    with TestClient(app) as client:
        motor_setup(app.state.host.motor)
        body = client.get("/api/tts/config").json()
    main_mod._host_active = False
    return body


def test_tts_config_reports_effective_voice_and_failure(efile, monkeypatch):
    def setup(motor):
        motor.effective_edge_voice = lambda: "es-MX-JorgeNeural"
        motor._last_edge_switch_failure = {"requested": "x", "reason": "edge_voice_unknown"}

    body = _config(monkeypatch, setup)
    assert body["edge_voice"] == "es-MX-JorgeNeural"
    assert body["edge_voice_failure_reason"] == "edge_voice_unknown"


def test_tts_config_falls_back_to_bundle_voice_for_hosts_without_engine_state(efile, monkeypatch):
    body = _config(monkeypatch, lambda motor: None)
    assert isinstance(body["edge_voice"], str) and body["edge_voice"].endswith("Neural")
    assert body["edge_voice_failure_reason"] is None
