"""Vision model inventory: installed tags + vision capability (vision-on-demand V1.17).

A fake HTTP transport stands in for Ollama: no network, no daemon.
"""
from __future__ import annotations

import json

from opencohost.core.vision.catalog import SUGGESTED_VISION_MODELS
from opencohost.core.vision.model_inventory import (
    VisionModelInventory,
    main_ollama_base_url,
    vision_by_name,
)

VISION_URL = "http://127.0.0.1:11435"
MAIN_URL = "http://127.0.0.1:11434"


class FakeHttp:
    """Routes ``(method, url)`` to a canned answer; an exception instance is raised."""

    def __init__(self, routes=None, show=None):
        self.routes = dict(routes or {})
        self.show = dict(show or {})  # (base_url, tag) -> answer
        self.calls = []

    @staticmethod
    def _answer(answer):
        if isinstance(answer, BaseException):
            raise answer
        status, payload = answer
        return status, payload if isinstance(payload, str) else json.dumps(payload)

    def get(self, url, timeout):
        self.calls.append(("GET", url, None, timeout))
        if url not in self.routes:
            raise ConnectionRefusedError(url)
        return self._answer(self.routes[url])

    def post_json(self, url, payload, timeout):
        self.calls.append(("POST", url, payload, timeout))
        base = url.rsplit("/api/", 1)[0]
        key = (base, payload.get("model"))
        if key not in self.show:
            raise ConnectionRefusedError(url)
        return self._answer(self.show[key])

    def show_calls(self):
        return [c for c in self.calls if c[0] == "POST"]


def _tags(*entries):
    return (200, {"models": list(entries)})


def _show(*capabilities):
    return (200, {"capabilities": list(capabilities)})


class FakeClock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


def test_lists_installed_tags_from_the_vision_daemon_with_capability_and_size():
    http = FakeHttp(
        routes={
            f"{VISION_URL}/api/tags": _tags(
                {"name": "qwen3-vl:4b-instruct", "model": "qwen3-vl:4b-instruct", "size": 3300000000},
                {"name": "llama3:latest", "model": "llama3:latest", "size": 4700000000},
            )
        },
        show={
            (VISION_URL, "qwen3-vl:4b-instruct"): _show("completion", "vision"),
            (VISION_URL, "llama3"): _show("completion"),
        },
    )
    inventory = VisionModelInventory(http=http)

    snapshot = inventory.snapshot([VISION_URL, MAIN_URL])

    assert snapshot.reachable is True
    assert snapshot.installed == [
        {"tag": "llama3", "vision": False, "size_bytes": 4700000000},
        {"tag": "qwen3-vl:4b-instruct", "vision": True, "size_bytes": 3300000000},
    ]
    assert not any(MAIN_URL in c[1] for c in http.calls)


def test_falls_back_to_the_main_daemon_when_the_vision_daemon_is_down():
    http = FakeHttp(
        routes={f"{MAIN_URL}/api/tags": _tags({"name": "moondream:latest", "size": 1})},
        show={(MAIN_URL, "moondream"): _show("completion", "vision")},
    )

    snapshot = VisionModelInventory(http=http).snapshot([VISION_URL, MAIN_URL])

    assert snapshot.reachable is True
    assert snapshot.installed == [{"tag": "moondream", "vision": True, "size_bytes": 1}]


def test_non_200_or_malformed_tags_fall_through_to_the_next_daemon():
    http = FakeHttp(
        routes={
            f"{VISION_URL}/api/tags": (200, "not json"),
            f"{MAIN_URL}/api/tags": _tags({"name": "llava:7b"}),
        },
        show={(MAIN_URL, "llava:7b"): _show("vision")},
    )

    snapshot = VisionModelInventory(http=http).snapshot([VISION_URL, MAIN_URL])

    assert snapshot.installed == [{"tag": "llava:7b", "vision": True, "size_bytes": None}]


def test_unreachable_everywhere_fails_open_to_empty_and_unreachable():
    snapshot = VisionModelInventory(http=FakeHttp()).snapshot([VISION_URL, MAIN_URL])

    assert snapshot.reachable is False
    assert snapshot.installed == []


def test_tags_requests_are_bounded_by_the_timeout():
    http = FakeHttp()

    VisionModelInventory(http=http, timeout_s=2.5).snapshot([VISION_URL, MAIN_URL])

    assert [c[3] for c in http.calls] == [2.5, 2.5]


def test_duplicate_and_blank_tags_are_canonicalized_once():
    http = FakeHttp(
        routes={
            f"{VISION_URL}/api/tags": _tags(
                {"name": "llama3:latest"}, {"model": "llama3"}, {"name": "  "}, "junk"
            )
        },
        show={(VISION_URL, "llama3"): _show("completion")},
    )

    snapshot = VisionModelInventory(http=http).snapshot([VISION_URL])

    assert [m["tag"] for m in snapshot.installed] == ["llama3"]


def test_capability_is_cached_per_tag_and_digest():
    http = FakeHttp(
        routes={f"{VISION_URL}/api/tags": _tags({"name": "qwen3-vl:2b-instruct", "digest": "d1"})},
        show={(VISION_URL, "qwen3-vl:2b-instruct"): _show("vision")},
    )
    inventory = VisionModelInventory(http=http)

    inventory.snapshot([VISION_URL])
    inventory.snapshot([VISION_URL])
    assert len(http.show_calls()) == 1

    http.routes[f"{VISION_URL}/api/tags"] = _tags({"name": "qwen3-vl:2b-instruct", "digest": "d2"})
    inventory.snapshot([VISION_URL])
    assert len(http.show_calls()) == 2


def test_show_failure_falls_back_to_the_name_heuristic_and_is_not_cached():
    http = FakeHttp(
        routes={
            f"{VISION_URL}/api/tags": _tags({"name": "minicpm-v:8b"}, {"name": "mistral:7b"})
        },
        show={
            (VISION_URL, "minicpm-v:8b"): (500, "boom"),
            (VISION_URL, "mistral:7b"): (404, "nope"),
        },
    )
    inventory = VisionModelInventory(http=http)

    snapshot = inventory.snapshot([VISION_URL])

    assert {m["tag"]: m["vision"] for m in snapshot.installed} == {
        "minicpm-v:8b": True,
        "mistral:7b": None,
    }
    inventory.snapshot([VISION_URL])
    assert len(http.show_calls()) == 4  # unknowns are asked again next time


def test_show_without_capabilities_uses_the_heuristic():
    http = FakeHttp(
        routes={f"{VISION_URL}/api/tags": _tags({"name": "bakllava:7b"})},
        show={(VISION_URL, "bakllava:7b"): (200, {"details": {}})},
    )

    snapshot = VisionModelInventory(http=http).snapshot([VISION_URL])

    assert snapshot.installed[0]["vision"] is True


def test_gemma_reported_as_vision_keeps_the_reported_value():
    http = FakeHttp(
        routes={f"{VISION_URL}/api/tags": _tags({"name": "gemma4:e4b"})},
        show={(VISION_URL, "gemma4:e4b"): _show("completion", "vision")},
    )

    snapshot = VisionModelInventory(http=http).snapshot([VISION_URL])

    assert snapshot.installed[0]["vision"] is True


def test_a_connection_failure_on_show_stops_probing_the_rest():
    http = FakeHttp(
        routes={
            f"{VISION_URL}/api/tags": _tags({"name": "a-vl:1b"}, {"name": "b:1b"}, {"name": "c:1b"})
        },
        show={},  # every show raises ConnectionRefusedError
    )

    snapshot = VisionModelInventory(http=http).snapshot([VISION_URL])

    assert len(http.show_calls()) == 1
    assert [m["vision"] for m in snapshot.installed] == [True, None, None]


def test_probing_stops_when_the_global_budget_is_spent():
    clock = FakeClock()

    class SlowHttp(FakeHttp):
        def post_json(self, url, payload, timeout):
            clock.now += 1.5
            return super().post_json(url, payload, timeout)

    http = SlowHttp(
        routes={
            f"{VISION_URL}/api/tags": _tags(
                {"name": "a:1b"}, {"name": "b:1b"}, {"name": "c:1b"}, {"name": "d-vl:1b"}
            )
        },
        show={(VISION_URL, t): _show("completion") for t in ("a:1b", "b:1b", "c:1b", "d-vl:1b")},
    )
    inventory = VisionModelInventory(http=http, probe_budget_s=3.0, monotonic=clock)

    snapshot = inventory.snapshot([VISION_URL])

    assert len(http.show_calls()) == 2
    assert [m["vision"] for m in snapshot.installed] == [False, False, None, True]
    # every show timeout fits inside what is left of the budget
    assert all(c[3] <= 3.0 for c in http.show_calls())
    # the next call finishes the job from the cache
    inventory.snapshot([VISION_URL])
    assert len(http.show_calls()) == 4


def test_name_heuristic():
    for tag in (
        "qwen3-vl:4b-instruct",
        "llava:7b",
        "minicpm-v:8b",
        "llama3.2-vision:11b",
        "moondream",
        "bakllava",
        "qwen2.5vl:3b",
        "granite3.2-vision:2b",
        "Qwen2.5-VL:7B",
    ):
        assert vision_by_name(tag) is True, tag
    for tag in ("gemma4:e4b", "gemma3:4b", "llama3", "qwen3:8b", ""):
        assert vision_by_name(tag) is None, tag


def test_main_ollama_base_url_follows_ollama_host():
    assert main_ollama_base_url({}) == "http://127.0.0.1:11434"
    assert main_ollama_base_url({"OLLAMA_HOST": "127.0.0.1:11500"}) == "http://127.0.0.1:11500"
    assert main_ollama_base_url({"OLLAMA_HOST": "0.0.0.0"}) == "http://127.0.0.1:11434"
    assert main_ollama_base_url({"OLLAMA_HOST": "http://localhost:9999/"}) == "http://localhost:9999"
    assert main_ollama_base_url({"OLLAMA_HOST": "  "}) == "http://127.0.0.1:11434"


def test_suggested_catalog():
    assert [m["tag"] for m in SUGGESTED_VISION_MODELS] == [
        "qwen3-vl:4b-instruct",
        "qwen3-vl:2b-instruct",
    ]
    by_tag = {m["tag"]: m for m in SUGGESTED_VISION_MODELS}
    assert by_tag["qwen3-vl:4b-instruct"]["display"] == "Qwen3-VL 4B Instruct"
    assert by_tag["qwen3-vl:4b-instruct"]["size_gb"] == 3.3
    assert by_tag["qwen3-vl:2b-instruct"]["display"] == "Qwen3-VL 2B Instruct"
    assert by_tag["qwen3-vl:2b-instruct"]["size_gb"] == 1.9
    for model in SUGGESTED_VISION_MODELS:
        assert set(model) == {"tag", "display", "size_gb", "desc"}
        assert model["desc"]
