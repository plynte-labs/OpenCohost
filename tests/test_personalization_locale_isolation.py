"""Tests for locale-isolated user personalization (es vs en).

Verifies:
- `es` persists to `personalization.json` (preserving legacy path/data)
- `en` persists to `personalization_en.json`
- Per-locale mtime cache isolation in `personalization.py`
- `build_injection_block(..., locale=...)` injects only the requested locale's record
- `GET/PUT/DELETE /api/personalization?locale=...` and `PUT` body `locale` isolate `es` and `en`
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

import opencohost.core.profiles.personalization as personalization
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


def _sanitize_passthrough(text: str) -> str:
    return text


def test_store_isolates_es_and_en_files_and_caches(tmp_path, monkeypatch):
    base_file = tmp_path / "personalization.json"
    en_file = tmp_path / "personalization_en.json"
    monkeypatch.setattr(personalization, "PERSONALIZATION_FILE", str(base_file))

    assert personalization.save_personalization(
        {
            "enabled": True,
            "nickname": "FranES",
            "occupation": "Streamer",
            "interests": "Videojuegos",
            "custom_instructions": "Habla siempre en español neutro",
        },
        locale="es",
    )

    assert base_file.exists()
    assert not en_file.exists()

    # Switching to en starts fresh with empty defaults even though es is populated.
    en_loaded = personalization.load_personalization(locale="en")
    assert en_loaded["enabled"] is True
    assert en_loaded["nickname"] == ""
    assert en_loaded["occupation"] == ""
    assert en_loaded["interests"] == ""
    assert en_loaded["custom_instructions"] == ""
    assert en_loaded["updated_at"] is None
    assert not en_file.exists()

    # Saving en writes personalization_en.json without altering es.
    assert personalization.save_personalization(
        {
            "enabled": True,
            "nickname": "FranEN",
            "occupation": "Developer",
            "interests": "Chess",
            "custom_instructions": "Reply in concise English",
        },
        locale="en",
    )
    assert en_file.exists()

    es_loaded = personalization.load_personalization(locale="es")
    en_loaded_after = personalization.load_personalization(locale="en")
    assert es_loaded["nickname"] == "FranES"
    assert es_loaded["custom_instructions"] == "Habla siempre en español neutro"
    assert en_loaded_after["nickname"] == "FranEN"
    assert en_loaded_after["custom_instructions"] == "Reply in concise English"

    # Clearing en leaves es intact.
    assert personalization.clear_personalization(locale="en") is True
    assert personalization.load_personalization(locale="en")["nickname"] == ""
    assert personalization.load_personalization(locale="es")["nickname"] == "FranES"


def test_build_injection_block_respects_locale_and_falls_back_to_active_bundle(tmp_path, monkeypatch):
    base_file = tmp_path / "personalization.json"
    monkeypatch.setattr(personalization, "PERSONALIZATION_FILE", str(base_file))

    personalization.save_personalization(
        {"enabled": True, "nickname": "Carlos", "custom_instructions": "Habla en español"},
        locale="es",
    )

    # Explicit en is empty even when es has data -> returns "".
    assert personalization.build_injection_block(_sanitize_passthrough, locale="en") == ""

    personalization.save_personalization(
        {"enabled": True, "nickname": "Charlie", "custom_instructions": "Speak English"},
        locale="en",
    )

    en_block = personalization.build_injection_block(_sanitize_passthrough, locale="en-US")
    es_block = personalization.build_injection_block(_sanitize_passthrough, locale="es-MX")
    assert "Charlie" in en_block and "Carlos" not in en_block
    assert "Carlos" in es_block and "Charlie" not in es_block

    # When locale=None, follows active bundle locale.
    i18n_active.set_active_bundle(resolve_active_bundle(locale="en"))
    active_en_block = personalization.build_injection_block(_sanitize_passthrough)
    assert "Charlie" in active_en_block and "Carlos" not in active_en_block
    assert active_en_block.startswith("<user_profile ")


def test_api_personalization_isolates_es_and_en(tmp_path):
    app = _app()
    with TestClient(app) as client:
        # Save in es
        r_es = client.put(
            "/api/personalization?locale=es",
            json={"nickname": "TavoES", "custom_instructions": "En español"},
        )
        assert r_es.status_code == 200
        assert r_es.json()["nickname"] == "TavoES"

        # GET en is still empty defaults
        r_en_get = client.get("/api/personalization?locale=en")
        assert r_en_get.status_code == 200
        assert r_en_get.json()["nickname"] == ""
        assert r_en_get.json()["custom_instructions"] == ""

        # Save in en via body `locale`
        r_en_put = client.put(
            "/api/personalization",
            json={"locale": "en", "nickname": "TavoEN", "custom_instructions": "In English"},
        )
        assert r_en_put.status_code == 200
        assert r_en_put.json()["nickname"] == "TavoEN"

        # Both coexist independently
        assert client.get("/api/personalization?locale=es").json()["nickname"] == "TavoES"
        assert client.get("/api/personalization?locale=en").json()["nickname"] == "TavoEN"

        # Delete en leaves es intact
        r_del = client.delete("/api/personalization?locale=en")
        assert r_del.status_code == 200
        assert client.get("/api/personalization?locale=en").json()["nickname"] == ""
        assert client.get("/api/personalization?locale=es").json()["nickname"] == "TavoES"

    assert (tmp_path / "personalization.json").exists()
    assert json.loads((tmp_path / "personalization.json").read_text(encoding="utf-8"))["nickname"] == "TavoES"
    assert (tmp_path / "personalization_en.json").exists()
    assert json.loads((tmp_path / "personalization_en.json").read_text(encoding="utf-8"))["nickname"] == ""
