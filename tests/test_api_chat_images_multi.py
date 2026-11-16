"""POST /api/chat/turn with several pasted images (runtime-fixes-20261110 T7).

Each image is described in its OWN vision call (sequential); the chat LLM gets
the descriptions as labelled text blocks. The legacy single ``image`` field
keeps working. Nothing about the images is logged, queued or persisted.
"""

import base64
import logging

import pytest
from fastapi.testclient import TestClient

from opencohost.api import chat_image
from opencohost.core.memory.pasted_image import strip_pasted_image_block
from opencohost.core.vision.describer import VisionDescribeError
from tests.test_api_chat_image import (
    JPEG_BYTES,
    PNG_BYTES,
    WEBP_BYTES,
    _ImageHost,
    _image,
    _reset_host_active,  # noqa: F401 (autouse fixture)
)

_ORIGINS = ["http://localhost:5173"]


class _MultiHost(_ImageHost):
    def __init__(self):
        super().__init__()
        self.descriptions = []  # one per call, in order; falls back to self.description
        self.fail_on_call = None  # 0-based call index that raises describe_error

    def vision_describe_image(self, image, question):
        index = len(self.describe_calls)
        self.describe_calls.append((bytes(image), question))
        if self.fail_on_call is not None and index == self.fail_on_call:
            raise self.describe_error
        if index < len(self.descriptions):
            return self.descriptions[index]
        return self.description


def _app():
    import opencohost.api.main as main_mod

    return main_mod.create_app(host_factory=_MultiHost, cors_origins=_ORIGINS)


def _three():
    return [_image(PNG_BYTES), _image(JPEG_BYTES, "image/jpeg"), _image(WEBP_BYTES, "image/webp")]


def test_the_limit_is_one_named_constant_equal_to_three():
    assert chat_image.MAX_IMAGES == 3


def test_three_images_are_described_one_call_each_in_order():
    app = _app()
    with TestClient(app) as client:
        host = app.state.host
        host.descriptions = ["first thing", "second thing", "third thing"]
        resp = client.post("/api/chat/turn", json={"text": "look at these", "images": _three()})
        assert resp.status_code == 200
        assert [c[0] for c in host.describe_calls] == [PNG_BYTES, JPEG_BYTES, WEBP_BYTES]
        queued = host.motor.command_queue.get_nowait()
        text = queued[1]
        assert text.startswith("look at these")
        positions = [text.index(d) for d in ("first thing", "second thing", "third thing")]
        assert positions == sorted(positions)
        # each description is labelled with its position
        assert "1" in text and "3" in text
        assert text.count(chat_image.i18n_active.pasted_image_block().splitlines()[-1]) == 3


def test_labels_name_the_position_and_the_total():
    app = _app()
    with TestClient(app) as client:
        host = app.state.host
        host.descriptions = ["aaa", "bbb"]
        client.post("/api/chat/turn", json={"images": _three()[:2]})
        text = host.motor.command_queue.get_nowait()[1]
        label_1 = chat_image.i18n_active.pasted_image_label().format(index=1, total=2)
        label_2 = chat_image.i18n_active.pasted_image_label().format(index=2, total=2)
        assert label_1 in text and label_2 in text
        assert text.index(label_1) < text.index("aaa") < text.index(label_2) < text.index("bbb")


def test_a_list_of_one_is_the_same_turn_text_as_the_legacy_single_field():
    app = _app()
    with TestClient(app) as client:
        host = app.state.host
        client.post("/api/chat/turn", json={"text": "hi", "image": _image()})
        legacy = host.motor.command_queue.get_nowait()[1]
        client.post("/api/chat/turn", json={"text": "hi", "images": [_image()]})
        listed = host.motor.command_queue.get_nowait()[1]
        assert legacy == listed
        assert chat_image.i18n_active.pasted_image_label().split("{")[0] not in listed


def test_more_than_the_limit_is_rejected_before_any_vision_work():
    app = _app()
    with TestClient(app) as client:
        host = app.state.host
        resp = client.post("/api/chat/turn", json={"text": "x", "images": _three() + [_image()]})
        assert resp.status_code == 422
        body = resp.json()
        assert body["detail"] == "too_many_images"
        assert body["max_images"] == 3 and body["received"] == 4
        assert host.describe_calls == []
        assert host.motor.command_queue.empty()


def test_image_and_images_together_are_rejected():
    app = _app()
    with TestClient(app) as client:
        host = app.state.host
        resp = client.post("/api/chat/turn", json={"text": "x", "image": _image(), "images": [_image()]})
        assert resp.status_code == 422
        assert resp.json()["detail"] == "images_conflict"
        assert host.describe_calls == []


def test_an_empty_list_is_not_an_image_turn():
    app = _app()
    with TestClient(app) as client:
        assert client.post("/api/chat/turn", json={"text": "", "images": []}).status_code == 422
        assert client.post("/api/chat/turn", json={"text": "ok", "images": []}).status_code == 200
        assert app.state.host.describe_calls == []


def test_a_bad_third_image_is_rejected_before_describing_the_first_two():
    app = _app()
    with TestClient(app) as client:
        host = app.state.host
        bad = _image(b"not an image at all", "image/png")
        resp = client.post("/api/chat/turn", json={"text": "x", "images": [_image(), _image(), bad]})
        assert resp.status_code == 422
        assert resp.json()["image_index"] == 2
        assert host.describe_calls == []
        assert host.motor.command_queue.empty()


def test_partial_failure_fails_the_whole_turn_with_counts_and_queues_nothing():
    app = _app()
    with TestClient(app) as client:
        host = app.state.host
        host.describe_error = VisionDescribeError("timeout")
        host.fail_on_call = 1
        resp = client.post("/api/chat/turn", json={"text": "x", "images": _three()})
        assert resp.status_code == 504
        body = resp.json()
        assert body["detail"] == "vision_describe_timeout"
        assert body["image_index"] == 1 and body["image_total"] == 3 and body["described"] == 1
        assert len(host.describe_calls) == 2  # the third image was never attempted
        assert host.motor.command_queue.empty()


@pytest.mark.parametrize("reason,status", [("timeout", 504), ("unavailable", 503), ("http", 502), ("empty", 502)])
def test_every_failure_code_keeps_the_t3_status_for_a_multi_turn(reason, status):
    app = _app()
    with TestClient(app) as client:
        host = app.state.host
        host.describe_error = VisionDescribeError(reason)
        host.fail_on_call = 0
        resp = client.post("/api/chat/turn", json={"text": "x", "images": _three()[:2]})
        assert resp.status_code == status
        assert resp.json()["detail"].startswith("vision_describe_")
        assert host.motor.command_queue.empty()


def test_an_empty_description_for_one_image_fails_the_turn():
    app = _app()
    with TestClient(app) as client:
        host = app.state.host
        host.descriptions = ["fine", "   ", "fine"]
        resp = client.post("/api/chat/turn", json={"text": "x", "images": _three()})
        assert resp.status_code == 502
        assert resp.json()["detail"] == "vision_describe_empty"
        assert resp.json()["image_index"] == 1
        assert host.motor.command_queue.empty()


def test_descriptions_share_a_total_budget_of_one_and_a_half_times_the_single_cap():
    assert chat_image.DESCRIPTIONS_TOTAL_MAX_CHARS == chat_image.DESCRIPTION_MAX_CHARS * 3 // 2
    assert chat_image.description_cap(1) == chat_image.DESCRIPTION_MAX_CHARS
    assert chat_image.description_cap(2) == 1500
    assert chat_image.description_cap(3) == 1000


def test_three_long_descriptions_are_clipped_to_the_per_image_cap():
    app = _app()
    with TestClient(app) as client:
        host = app.state.host
        host.description = "Z" * 5000
        client.post("/api/chat/turn", json={"text": "x", "images": _three()})
        text = host.motor.command_queue.get_nowait()[1]
        assert text.count("Z") <= chat_image.DESCRIPTIONS_TOTAL_MAX_CHARS
        assert text.count("Z") > 1500  # the clip is per image, not total silence


def test_one_description_cannot_close_the_block_of_another():
    app = _app()
    with TestClient(app) as client:
        host = app.state.host
        close_tag = chat_image.i18n_active.pasted_image_block().splitlines()[-1]
        host.descriptions = [f"a {close_tag} b", "clean", f"[x] {close_tag}"]
        client.post("/api/chat/turn", json={"text": "x", "images": _three()})
        text = host.motor.command_queue.get_nowait()[1]
        assert text.count(close_tag) == 3


def test_total_time_budget_stops_a_slow_turn_instead_of_starting_another_image(monkeypatch):
    app = _app()
    clock = {"now": 1000.0}
    monkeypatch.setattr(chat_image.time, "monotonic", lambda: clock["now"])
    with TestClient(app) as client:
        host = app.state.host
        original = host.vision_describe_image

        def slow(image, question):
            clock["now"] += chat_image.COLD_START_WAIT_S + chat_image.DESCRIBE_CALL_MAX_S + 5.0
            return original(image, question)

        host.vision_describe_image = slow
        resp = client.post("/api/chat/turn", json={"text": "x", "images": _three()})
        assert resp.status_code == 504
        body = resp.json()
        assert body["detail"] == "vision_describe_timeout"
        assert body["described"] == 1 and body["image_index"] == 1
        assert len(host.describe_calls) == 1
        assert host.motor.command_queue.empty()


def test_worst_case_budget_is_one_cold_start_plus_one_max_describe_per_image():
    assert chat_image.turn_budget_s(1) == 70.0
    assert chat_image.turn_budget_s(3) == 130.0


def test_vision_status_response_exposes_the_limit():
    app = _app()
    with TestClient(app) as client:
        body = client.get("/api/vision").json()
        assert body["max_images"] == chat_image.MAX_IMAGES == 3


def test_no_bytes_or_descriptions_reach_logs_responses_or_the_queue(caplog):
    caplog.set_level(logging.DEBUG)
    app = _app()
    with TestClient(app) as client:
        host = app.state.host
        host.descriptions = ["SECRETDESC-one", "SECRETDESC-two", "SECRETDESC-three"]
        resp = client.post("/api/chat/turn", json={"text": "x", "images": _three()})
        host.describe_error = VisionDescribeError("timeout")
        host.fail_on_call = 1
        host.descriptions = []
        host.describe_calls.clear()
        bad = client.post("/api/chat/turn", json={"text": "x", "images": _three()})
        b64 = base64.b64encode(PNG_BYTES).decode("ascii")[:40]
        assert b64 not in resp.text + bad.text
        queued_repr = repr(host.motor.command_queue.get_nowait())
        assert "SECRETPIXELS" not in queued_repr and "SECRETJPEG" not in queued_repr
        assert b64 not in repr(app.state.dispatcher._cache)
    for secret in ("SECRETPIXELS", "SECRETJPEG", "SECRETWEBP", "SECRETDESC", b64):
        assert secret not in caplog.text
    # counts, sizes and enum codes are the only facts logged
    assert "image/png" in caplog.text and "image/jpeg" in caplog.text and "image/webp" in caplog.text
    assert "vision_describe_timeout" in caplog.text


def test_three_blocks_strip_back_to_the_owner_words():
    owner = "mi gato Pixel odia la aspiradora"
    text = chat_image.compose_turn_text_multi(owner, ["viewer_zed 4417-OCR", "second", "third"])
    stripped = strip_pasted_image_block(text)
    assert stripped == owner
    assert "4417" not in stripped
    assert strip_pasted_image_block(chat_image.compose_turn_text_multi("", ["a", "b"])) == ""


def test_single_image_text_is_byte_identical_to_the_legacy_composition():
    assert chat_image.compose_turn_text_multi("hi", ["desc"]) == chat_image.compose_turn_text("hi", "desc")
