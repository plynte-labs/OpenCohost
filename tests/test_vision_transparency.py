"""Vision transparency (V1): the look's description and the SENT frames are kept
in memory next to each other, served to the owner only, and never logged or
written to disk."""
from __future__ import annotations

import builtins
import io
import logging
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

import opencohost.api.auth as auth
import opencohost.api.main as main_mod
from opencohost.core.vision.frames import THUMBNAIL_RING_SIZE, ThumbnailRing
from tests.test_api_phase1 import FakeHost
from tests.test_vision_controller import ON, Harness

DESCRIPTION = "Barra de vida al 30%."


def _look(h, text="mira"):
    h.controller.apply(replace(ON, frames=1))
    h.controller.arm_look()
    h.controller.step.run(text, "direct")
    return h.controller.status()["last_result"]["id"]


# ---------------------------------------------------------------- controller


def test_description_is_served_for_the_looks_id_only():
    h = Harness()
    look_id = _look(h)

    assert h.controller.description(look_id) == DESCRIPTION
    assert h.controller.description("unknown") is None


def test_description_is_not_in_status_last_result():
    h = Harness()
    _look(h)

    status = h.controller.status()

    assert DESCRIPTION not in repr(status)
    assert "description" not in status["last_result"]


def test_description_is_not_in_the_result_event():
    h = Harness()
    _look(h)

    assert DESCRIPTION not in repr(h.results)


def test_description_expires_with_the_frames_when_thumbnails_turn_off():
    h = Harness()
    look_id = _look(h)

    h.controller.apply(replace(ON, frames=1, show_thumbnails=False))

    assert h.controller.description(look_id) is None
    h.controller.apply(replace(ON, frames=1, show_thumbnails=True))
    assert h.controller.description(look_id) is None  # cleared, not hidden


def test_description_is_not_kept_when_thumbnails_are_off():
    h = Harness()
    h.controller.apply(replace(ON, frames=1, show_thumbnails=False))
    h.controller.arm_look()
    h.controller.step.run("mira", "direct")

    assert h.controller.description(h.controller.status()["last_result"]["id"]) is None


@pytest.mark.parametrize("teardown", ["disable", "shutdown"])
def test_description_expires_on_disable_and_shutdown(teardown):
    h = Harness()
    look_id = _look(h)

    if teardown == "disable":
        h.controller.apply(replace(ON, enabled=False))
    else:
        h.controller.shutdown()

    assert h.controller.description(look_id) is None


def test_description_is_evicted_with_its_frames_from_the_ring():
    ring = ThumbnailRing()
    ring.put("first", [b"a"])
    ring.set_description("first", "secret one")
    for n in range(THUMBNAIL_RING_SIZE):
        ring.put(f"look-{n}", [b"x"])

    assert ring.get("first") is None
    assert ring.get_description("first") is None


def test_ring_description_needs_live_frames_and_repr_hides_it():
    ring = ThumbnailRing()
    ring.set_description("ghost", "never stored")
    ring.put("real", [b"a"])
    ring.set_description("real", "TOPSECRET")

    assert ring.get_description("ghost") is None
    assert "TOPSECRET" not in repr(ring)


def test_description_never_reaches_logs(caplog):
    caplog.set_level(logging.DEBUG)
    h = Harness()
    look_id = _look(h)
    h.controller.description(look_id)
    h.controller.apply(replace(ON, enabled=False))

    assert DESCRIPTION not in caplog.text


def test_description_is_never_written_to_disk(monkeypatch):
    writes = []
    real_open = builtins.open

    def spy(file, mode="r", *args, **kwargs):
        if any(flag in str(mode) for flag in "wax+"):
            writes.append(str(file))
        return real_open(file, mode, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", spy)
    h = Harness()
    _look(h)

    assert writes == []


# --------------------------------------------------- sent frames, not previews


def test_frames_served_are_the_bytes_sent_to_the_model():
    h = Harness()
    look_id = _look(h)

    served = h.controller.thumbnails(look_id)

    # The fake capture's jpeg is b"\xff\xd8x" and its 320 px preview b"\xff\xd8thumb".
    assert served == ["data:image/jpeg;base64,/9h4"]


# ---------------------------------------------------------------------- API


class _Host(FakeHost):
    def __init__(self):
        super().__init__()
        self.thumbs = {"abc": ["data:image/jpeg;base64,AAAA"]}
        self.descriptions = {"abc": "A game with a health bar."}

    def vision_thumbnails(self, look_id):
        return self.thumbs.get(look_id)

    def vision_description(self, look_id):
        return self.descriptions.get(look_id)


@pytest.fixture(autouse=True)
def _reset():
    main_mod._host_active = False
    yield
    main_mod._host_active = False


@pytest.fixture
def operator_headers():
    auth.ensure_tokens()
    return {"Authorization": f"Bearer {auth.load_tokens()['operator']}"}


@pytest.fixture
def agent_headers():
    auth.ensure_tokens()
    return {"Authorization": f"Bearer {auth.load_tokens()['agent']}"}


def _client(factory=_Host):
    app = main_mod.create_app(host_factory=factory, cors_origins=["http://localhost:5173"])
    return TestClient(app)


def test_route_serves_the_description_to_the_operator(operator_headers):
    with _client() as client:
        body = client.get("/api/vision/looks/abc/thumbnails", headers=operator_headers).json()

    assert body["thumbnails"] == ["data:image/jpeg;base64,AAAA"]
    assert body["description"] == "A game with a health bar."


@pytest.mark.parametrize("who", ["none", "garbage", "agent"])
def test_route_is_403_without_the_operator_token(who, agent_headers):
    headers = {
        "none": {},
        "garbage": {"Authorization": "Bearer nope"},
        "agent": agent_headers,
    }[who]
    with _client() as client:
        resp = client.get("/api/vision/looks/abc/thumbnails", headers=headers)

    assert resp.status_code == 403
    assert resp.json() == {"detail": "operator token required"}
    assert "AAAA" not in resp.text and "health bar" not in resp.text


def test_route_is_403_before_it_reveals_whether_the_look_exists():
    with _client() as client:
        assert client.get("/api/vision/looks/nope/thumbnails").status_code == 403


def test_route_is_503_when_the_token_file_is_corrupt(operator_headers):
    from pathlib import Path

    from opencohost.config import settings

    Path(settings.API_TOKENS_FILE).write_text("{ not json", encoding="utf-8")
    with _client() as client:
        resp = client.get("/api/vision/looks/abc/thumbnails", headers=operator_headers)

    assert resp.status_code == 503
    assert resp.json() == {"detail": "auth_unavailable"}
    assert "AAAA" not in resp.text


def test_route_404_for_an_expired_look_even_if_a_description_lingers(operator_headers):
    def factory():
        host = _Host()
        host.thumbs = {}
        return host

    with _client(factory) as client:
        resp = client.get("/api/vision/looks/abc/thumbnails", headers=operator_headers)

    assert resp.status_code == 404
    assert "health bar" not in resp.text


def test_route_without_description_support_still_serves_frames(operator_headers):
    class Old(_Host):
        vision_description = None

    with _client(Old) as client:
        body = client.get("/api/vision/looks/abc/thumbnails", headers=operator_headers).json()

    assert body["thumbnails"] == ["data:image/jpeg;base64,AAAA"]
    assert "description" not in body


def test_status_route_never_carries_the_description(operator_headers):
    class H(_Host):
        def vision_status(self):
            return {"enabled": True, "state": "ready", "last_result": {"id": "abc"}}

    with _client(H) as client:
        text = client.get("/api/vision", headers=operator_headers).text

    assert "health bar" not in text
