"""Tests for locale-isolated TTS pronunciation dictionaries (es vs en).

Verifies:
- `es` persists to `pronunciation.json` (preserving legacy path/data)
- `en` persists to `pronunciation_en.json`
- In-memory state, `apply_pronunciation`, and `tts_sanitizer` isolate `es` and `en`
- `load_pronunciation()` at startup loads both `es` and `en` dictionaries
- `GET/PUT /api/tts/pronunciation?locale=...` isolate `es` and `en`
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from opencohost.core.speech import pronunciation as pr
from opencohost.core.speech.tts_sanitizer import (
    _sanitize_tts_text_for_playback,
    _spoken_text_for_fragment,
)
from opencohost.i18n import active as i18n_active
from opencohost.i18n.startup import resolve_active_bundle
from tests.test_api_phase1 import FakeHost


@pytest.fixture(autouse=True)
def _pin_es_bundle():
    i18n_active.set_active_bundle(resolve_active_bundle(locale="es"))
    yield
    i18n_active.reset_active_bundle()


def _app():
    import opencohost.api.main as main_mod

    return main_mod.create_app(host_factory=FakeHost, cors_origins=["http://localhost:5173"])


def test_store_and_runtime_isolate_es_and_en(tmp_path):
    es_file = tmp_path / "pronunciation.json"
    en_file = tmp_path / "pronunciation_en.json"

    assert pr.save_pronunciation([{"match": "OBS", "replace": "o be ese"}], locale="es") == []
    assert es_file.exists()
    assert not en_file.exists()

    # English remains empty and unaffected
    assert pr.get_entries(locale="en") == []
    assert pr.apply_pronunciation("open OBS", locale="en") == "open OBS"
    assert pr.apply_pronunciation("abre OBS", locale="es") == "abre o be ese"

    assert pr.save_pronunciation([{"match": "Kira", "replace": "Keera"}], locale="en") == []
    assert en_file.exists()

    assert pr.get_entries(locale="es") == [{"match": "OBS", "replace": "o be ese"}]
    assert pr.get_entries(locale="en") == [{"match": "Kira", "replace": "Keera"}]
    assert pr.apply_pronunciation("OBS Kira", locale="es") == "o be ese Kira"
    assert pr.apply_pronunciation("OBS Kira", locale="en") == "OBS Keera"

    # Sanitizer passes lang through to pronunciation
    assert _spoken_text_for_fragment("OBS Kira", lang="es") == "o be ese Kira"
    assert _spoken_text_for_fragment("OBS Kira", lang="en") == "OBS Keera"
    assert _sanitize_tts_text_for_playback("OBS Kira", lang="en-US") == "OBS Keera"

    # Resetting en leaves es intact
    pr.reset_pronunciation(locale="en")
    assert pr.get_entries(locale="en") == []
    assert pr.get_entries(locale="es") == [{"match": "OBS", "replace": "o be ese"}]


def test_startup_load_populates_both_es_and_en_and_api_isolates(tmp_path):
    (tmp_path / "pronunciation.json").write_text(
        json.dumps({"version": 1, "entries": [{"match": "GG", "replace": "ge ge"}]}),
        encoding="utf-8",
    )
    (tmp_path / "pronunciation_en.json").write_text(
        json.dumps({"version": 1, "entries": [{"match": "API", "replace": "A P I"}]}),
        encoding="utf-8",
    )

    with TestClient(_app()) as client:
        es_resp = client.get("/api/tts/pronunciation?locale=es")
        en_resp = client.get("/api/tts/pronunciation?locale=en")
        assert es_resp.status_code == 200
        assert en_resp.status_code == 200
        assert es_resp.json()["entries"] == [{"match": "GG", "replace": "ge ge"}]
        assert en_resp.json()["entries"] == [{"match": "API", "replace": "A P I"}]

        # Updating en does not touch es
        put_en = client.put(
            "/api/tts/pronunciation?locale=en",
            json={"entries": [{"match": "TTS", "replace": "tee tee ess"}]},
        )
        assert put_en.status_code == 200
        assert client.get("/api/tts/pronunciation?locale=es").json()["entries"] == [
            {"match": "GG", "replace": "ge ge"}
        ]
        assert client.get("/api/tts/pronunciation?locale=en").json()["entries"] == [
            {"match": "TTS", "replace": "tee tee ess"}
        ]

    # Active bundle switches to en -> default GET & playback follow en
    i18n_active.set_active_bundle(resolve_active_bundle(locale="en"))
    with TestClient(_app()) as client:
        assert client.get("/api/tts/pronunciation").json()["entries"] == [
            {"match": "TTS", "replace": "tee tee ess"}
        ]
        assert _sanitize_tts_text_for_playback("GG TTS") == "GG tee tee ess"


def test_apply_pronunciation_survives_active_locale_accessor_failure(monkeypatch):
    """A failing active-locale lookup must not break TTS sanitization.

    Falls back to the legacy ``es`` table (HEAD behavior: one locale-independent
    dictionary that persisted to ``pronunciation.json``).
    """
    pr.reset_pronunciation()
    assert pr.set_entries([{"match": "GPU", "replace": "ge pe u"}], locale="es") == []

    def _boom():
        raise RuntimeError("locale accessor down")

    monkeypatch.setattr(i18n_active, "get_active_bundle", _boom)
    assert pr.apply_pronunciation("la GPU") == "la ge pe u"
    pr.reset_pronunciation()
