"""reply-length-selector-20261009 T1: owner reply-length store and API.

``reply_length.json`` is redirected per test by the autouse
``_isolate_reply_length`` fixture in tests/conftest.py.
"""

import json
import os

import pytest
from fastapi.testclient import TestClient

from opencohost.config import reply_length as rl
from opencohost.config import settings
from tests.test_api_phase1 import FakeHost

_ORIGINS = ["http://localhost:5173"]


@pytest.fixture(autouse=True)
def _reset_host_active():
    import opencohost.api.main as main_mod

    main_mod._host_active = False
    yield
    main_mod._host_active = False


def _app():
    import opencohost.api.main as main_mod

    return main_mod.create_app(host_factory=FakeHost, cors_origins=_ORIGINS)


# --- store -------------------------------------------------------------------


def test_setting_points_into_config_dir():
    assert settings.REPLY_LENGTH_FILE == os.path.join(settings.CONFIG_DIR, "reply_length.json")


def test_modes_and_default():
    assert rl.MODES == ("auto", "short", "medium", "long", "very_long")
    assert rl.DEFAULT_MODE == "auto"
    assert rl.get_reply_length_mode() == "auto"


def test_missing_file_loads_auto():
    assert rl.load_reply_length() == "auto"
    assert rl.get_reply_length_mode() == "auto"


@pytest.mark.parametrize(
    "content",
    ["{not json", json.dumps({"mode": "huge"}), json.dumps(["short"]), json.dumps({"mode": 3})],
)
def test_corrupt_or_invalid_file_loads_auto(content):
    with open(rl.REPLY_LENGTH_FILE, "w", encoding="utf-8") as f:
        f.write(content)
    rl.set_reply_length_mode("long")
    assert rl.load_reply_length() == "auto"
    assert rl.get_reply_length_mode() == "auto"


def test_save_writes_atomically_and_installs():
    assert rl.save_reply_length("short") == "short"
    assert rl.get_reply_length_mode() == "short"
    with open(rl.REPLY_LENGTH_FILE, encoding="utf-8") as f:
        assert json.load(f) == {"mode": "short"}
    leftovers = [n for n in os.listdir(os.path.dirname(rl.REPLY_LENGTH_FILE)) if n.endswith(".tmp")]
    assert leftovers == []


def test_save_invalid_raises_and_keeps_previous():
    rl.save_reply_length("long")
    with pytest.raises(ValueError):
        rl.save_reply_length("enormous")
    assert rl.get_reply_length_mode() == "long"


def test_save_write_failure_returns_none_and_keeps_previous(monkeypatch):
    rl.save_reply_length("long")

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(rl.os, "replace", boom)
    assert rl.save_reply_length("short") is None
    assert rl.get_reply_length_mode() == "long"


def test_load_roundtrip():
    rl.save_reply_length("very_long")
    rl.reset_reply_length()
    assert rl.get_reply_length_mode() == "auto"
    assert rl.load_reply_length() == "very_long"
    assert rl.get_reply_length_mode() == "very_long"


def test_getter_never_reads_disk(monkeypatch):
    rl.save_reply_length("short")

    def no_open(*a, **k):
        raise AssertionError("disk read on the turn path")

    monkeypatch.setattr("builtins.open", no_open)
    assert rl.get_reply_length_mode() == "short"


# --- API ---------------------------------------------------------------------


def test_get_default_auto():
    with TestClient(_app()) as client:
        resp = client.get("/api/reply-length")
    assert resp.status_code == 200
    assert resp.json() == {"mode": "auto"}


def test_put_roundtrip_and_live_apply():
    with TestClient(_app()) as client:
        resp = client.put("/api/reply-length", json={"mode": "long"})
        assert resp.status_code == 200
        assert resp.json() == {"mode": "long"}
        assert client.get("/api/reply-length").json() == {"mode": "long"}
    assert rl.get_reply_length_mode() == "long"


def test_put_persists_and_startup_loads_it():
    with TestClient(_app()) as client:
        client.put("/api/reply-length", json={"mode": "short"})
    rl.reset_reply_length()
    with TestClient(_app()) as client:  # lifespan calls load_reply_length()
        assert client.get("/api/reply-length").json() == {"mode": "short"}


@pytest.mark.parametrize("body", [{"mode": "huge"}, {"mode": 3}, {"nope": 1}, {"mode": None}])
def test_put_422_invalid(body):
    with TestClient(_app()) as client:
        resp = client.put("/api/reply-length", json=body)
    assert resp.status_code == 422
    assert resp.json() == {"detail": "invalid_reply_length"}
    assert rl.get_reply_length_mode() == "auto"


def test_put_503_on_write_failure(monkeypatch):
    def boom(*a, **k):
        raise OSError("read-only")

    with TestClient(_app()) as client:
        monkeypatch.setattr(rl.os, "replace", boom)
        resp = client.put("/api/reply-length", json={"mode": "short"})
    assert resp.status_code == 503
    assert resp.json() == {"detail": "reply_length_write_failed"}
    assert rl.get_reply_length_mode() == "auto"


def test_openapi_exposes_mode_literal():
    with TestClient(_app()) as client:
        schema = client.get("/openapi.json").json()
    comps = schema["components"]["schemas"]
    assert comps["ReplyLengthResponse"]["properties"]["mode"]["enum"] == list(rl.MODES)
    put = schema["paths"]["/api/reply-length"]["put"]
    body_schema = put["requestBody"]["content"]["application/json"]["schema"]
    assert body_schema == {"$ref": "#/components/schemas/ReplyLengthRequest"}
    assert comps["ReplyLengthRequest"]["properties"]["mode"]["enum"] == list(rl.MODES)


def test_api_literal_matches_store_modes():
    import typing

    from opencohost.api.models import ReplyLengthMode

    assert typing.get_args(ReplyLengthMode) == rl.MODES
