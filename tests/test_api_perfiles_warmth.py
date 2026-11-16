"""K5 (warmth-guardrails-runtime-20261006): `warmth` on /api/perfiles.

Contract for the UI: field `warmth`, one of "cold" | "empathetic" |
"in_character" | "free". GET/POST/PUT responses always carry it; a stored
profile without the field (older files) or with an unknown value reads as
"cold". POST defaults to "cold"; PUT leaves it unchanged when omitted. An
unknown value on a write is rejected with 422.
"""
import json

import pytest
from fastapi.testclient import TestClient

from tests.test_api_phase1 import FakeHost

_ORIGINS = ["http://localhost:5173"]


@pytest.fixture(autouse=True)
def _reset_host_active():
    import opencohost.api.main as main_mod

    main_mod._host_active = False
    yield
    main_mod._host_active = False


@pytest.fixture
def profiles_file(tmp_path, monkeypatch):
    import opencohost.core.profiles.profiles as profiles_mod

    path = tmp_path / "perfiles.json"
    path.write_text(
        json.dumps(
            {
                "old": {"id": "id-old", "prompt": "Old prompt", "use_system": False},
                "warm": {"id": "id-warm", "prompt": "Warm", "use_system": True, "warmth": "free"},
                "junk": {"id": "id-junk", "prompt": "J", "use_system": True, "warmth": "tibia"},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(profiles_mod, "PROFILES_FILE", str(path))
    return path


def _client():
    import opencohost.api.main as main_mod

    return TestClient(main_mod.create_app(host_factory=FakeHost, cors_origins=_ORIGINS))


def _stored(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_get_profile_without_field_reads_cold(profiles_file):
    with _client() as client:
        assert client.get("/api/perfiles/old").json()["warmth"] == "cold"


def test_get_profile_returns_stored_warmth(profiles_file):
    with _client() as client:
        assert client.get("/api/perfiles/warm").json()["warmth"] == "free"


def test_get_profile_with_unknown_stored_value_reads_cold(profiles_file):
    with _client() as client:
        assert client.get("/api/perfiles/junk").json()["warmth"] == "cold"


def test_create_defaults_to_cold_and_persists(profiles_file):
    with _client() as client:
        resp = client.post("/api/perfiles", json={"name": "nuevo", "prompt": "P"})
    assert resp.status_code == 200
    assert resp.json()["warmth"] == "cold"
    assert _stored(profiles_file)["nuevo"]["warmth"] == "cold"


@pytest.mark.parametrize("level", ["cold", "empathetic", "in_character", "free", "sentimental"])
def test_create_with_each_level(profiles_file, level):
    with _client() as client:
        resp = client.post("/api/perfiles", json={"name": f"p-{level}", "prompt": "P", "warmth": level})
    assert resp.status_code == 200
    assert resp.json()["warmth"] == level
    assert _stored(profiles_file)[f"p-{level}"]["warmth"] == level


def test_create_rejects_unknown_level(profiles_file):
    with _client() as client:
        resp = client.post("/api/perfiles", json={"name": "x", "prompt": "P", "warmth": "hot"})
    assert resp.status_code == 422
    assert "x" not in _stored(profiles_file)


def test_update_sets_warmth(profiles_file):
    with _client() as client:
        resp = client.put("/api/perfiles/old", json={"warmth": "in_character"})
    assert resp.status_code == 200
    assert resp.json()["warmth"] == "in_character"
    assert _stored(profiles_file)["old"]["warmth"] == "in_character"


def test_update_without_warmth_keeps_it(profiles_file):
    with _client() as client:
        resp = client.put("/api/perfiles/warm", json={"prompt": "Changed"})
    assert resp.json()["warmth"] == "free"
    assert _stored(profiles_file)["warm"]["warmth"] == "free"


def test_update_rejects_unknown_level(profiles_file):
    with _client() as client:
        resp = client.put("/api/perfiles/warm", json={"warmth": "lukewarm"})
    assert resp.status_code == 422
    assert _stored(profiles_file)["warm"]["warmth"] == "free"


def test_api_literal_matches_warmth_levels():
    from typing import get_args

    from opencohost.api.models import WarmthLevel
    from opencohost.config.warmth import WARMTH_LEVELS

    assert get_args(WarmthLevel) == WARMTH_LEVELS


# --- P5 (kira-personal-tone-20261007): owner_gender round trip -------------


def test_owner_gender_defaults_empty_and_round_trips(profiles_file):
    with _client() as client:
        assert client.get("/api/perfiles/old").json()["owner_gender"] == ""
        created = client.post(
            "/api/perfiles", json={"name": "og", "prompt": "P", "owner_gender": "feminine"}
        )
        assert created.status_code == 200
        assert created.json()["owner_gender"] == "feminine"
        assert _stored(profiles_file)["og"]["owner_gender"] == "feminine"
        assert client.get("/api/perfiles/og").json()["owner_gender"] == "feminine"
        updated = client.put("/api/perfiles/og", json={"owner_gender": "neutral"})
        assert updated.json()["owner_gender"] == "neutral"
        kept = client.put("/api/perfiles/og", json={"prompt": "Q"})
        assert kept.json()["owner_gender"] == "neutral"
        cleared = client.put("/api/perfiles/og", json={"owner_gender": ""})
        assert cleared.json()["owner_gender"] == ""
        assert _stored(profiles_file)["og"]["owner_gender"] == ""


def test_owner_gender_rejects_unknown_value(profiles_file):
    with _client() as client:
        assert client.put("/api/perfiles/old", json={"owner_gender": "x"}).status_code == 422
        assert (
            client.post("/api/perfiles", json={"name": "z", "prompt": "P", "owner_gender": "o/a"}).status_code
            == 422
        )


def test_owner_gender_unknown_stored_value_reads_empty(profiles_file):
    data = _stored(profiles_file)
    data["old"]["owner_gender"] = "weird"
    profiles_file.write_text(json.dumps(data), encoding="utf-8")
    with _client() as client:
        assert client.get("/api/perfiles/old").json()["owner_gender"] == ""
