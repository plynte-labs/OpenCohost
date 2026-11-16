"""update-notification-20261111 N1: the once-per-launch release check, its
toggle and GET /api/update + PUT /api/update/config.

Nothing here reaches the network: httpx is driven through MockTransport.
``update_check.json`` is isolated per test by the autouse fixture in conftest.
"""
from __future__ import annotations

import json
import os

import httpx
import pytest
from fastapi.testclient import TestClient

import opencohost
from opencohost.config import settings, update_setting
from opencohost.core import update_check
from tests.test_api_phase1 import FakeHost

_ORIGINS = ["http://localhost:5173"]
_BASE = "https://github.com/plynte-labs/opencohost/releases/tag/"


def _rel(tag, draft=False, prerelease=True, url=None):
    return {"tag_name": tag, "draft": draft, "prerelease": prerelease,
            "html_url": url if url is not None else _BASE + tag}


def _install(monkeypatch, handler):
    calls = []

    def wrapped(request):
        calls.append(request)
        return handler(request)

    monkeypatch.setattr(
        update_check, "_make_client",
        lambda: httpx.Client(transport=httpx.MockTransport(wrapped), timeout=5.0),
    )
    return calls


def _json_handler(payload):
    return lambda request: httpx.Response(200, json=payload)


def _app():
    import opencohost.api.main as main_mod

    return main_mod.create_app(host_factory=FakeHost, cors_origins=_ORIGINS)


@pytest.fixture(autouse=True)
def _reset_host_active():
    import opencohost.api.main as main_mod

    main_mod._host_active = False
    yield
    main_mod._host_active = False


# --- version parsing -------------------------------------------------------------


@pytest.mark.parametrize("a,b", [("0.5.0a1", "v0.5.0-alpha.1"), ("0.5.0a1", "0.5.0-alpha.1"),
                                 ("0.5.0b2", "v0.5.0-beta.2"), ("0.5.0rc1", "v0.5.0-rc.1"),
                                 ("1.0.0", "v1.0.0")])
def test_forms_normalize_to_the_same_version(a, b):
    assert update_check.parse_version(a) == update_check.parse_version(b)
    assert update_check.parse_version(a) is not None


def test_version_ordering():
    p = update_check.parse_version
    assert p("v0.5.0-alpha.2") > p("0.5.0a1")
    assert p("0.5.0") > p("0.5.0rc1") > p("0.5.0b3") > p("0.5.0a9")
    assert p("0.10.0") > p("0.9.9")


@pytest.mark.parametrize("raw", ["", "latest", "v1", "1.2", "x.y.z", None, 5])
def test_unparseable_versions_are_none(raw):
    assert update_check.parse_version(raw) is None


# --- check -----------------------------------------------------------------------


def test_newer_prerelease_is_found(monkeypatch):
    _install(monkeypatch, _json_handler([_rel("v0.5.1-alpha.1"), _rel("v0.5.1-alpha.2")]))
    got = update_check.check()
    assert got == {"current": opencohost.__version__, "latest": "0.5.1-alpha.2",
                   "newer": True, "url": _BASE + "v0.5.1-alpha.2"}


def test_same_version_is_not_newer(monkeypatch):
    _install(monkeypatch, _json_handler([_rel("v0.5.0")]))
    got = update_check.check()
    assert got["newer"] is False and got["latest"] == "0.5.0"


def test_only_older_is_not_newer(monkeypatch):
    _install(monkeypatch, _json_handler([_rel("v0.4.0"), _rel("v0.3.0-alpha.4")]))
    got = update_check.check()
    assert got["newer"] is False and got["url"] is None


def test_drafts_are_ignored(monkeypatch):
    _install(monkeypatch, _json_handler([_rel("v0.9.0", draft=True), _rel("v0.5.0-alpha.1")]))
    got = update_check.check()
    assert got["newer"] is False


def test_unparseable_tags_are_skipped(monkeypatch):
    _install(monkeypatch, _json_handler([_rel("nightly"), _rel("v0.6.0", prerelease=False)]))
    assert update_check.check()["latest"] == "0.6.0"


def test_malformed_json_fails_silent(monkeypatch):
    _install(monkeypatch, lambda r: httpx.Response(200, content=b"<html>not json"))
    got = update_check.check()
    assert got["newer"] is False and got["url"] is None and got["latest"] is None


def test_non_list_json_and_http_error_fail_silent(monkeypatch):
    _install(monkeypatch, _json_handler({"message": "rate limited"}))
    assert update_check.check()["newer"] is False
    update_check.reset_cache()
    _install(monkeypatch, lambda r: httpx.Response(403, json=[_rel("v9.9.9")]))
    assert update_check.check()["newer"] is False


def test_timeout_fails_silent(monkeypatch):
    def boom(request):
        raise httpx.ReadTimeout("slow", request=request)

    _install(monkeypatch, boom)
    got = update_check.check()
    assert got["newer"] is False and got["current"] == opencohost.__version__


@pytest.mark.parametrize("url", ["https://evil.example/plynte-labs/opencohost/", "http://github.com/plynte-labs/opencohost/x",
                                 "https://github.com/other/opencohost/releases/tag/v9.0.0", "javascript:alert(1)", None])
def test_foreign_url_is_rejected(monkeypatch, url):
    rel = _rel("v9.0.0")
    rel["html_url"] = url
    _install(monkeypatch, _json_handler([rel]))
    got = update_check.check()
    assert got["newer"] is False and got["url"] is None


def test_request_carries_only_a_generic_user_agent(monkeypatch):
    calls = _install(monkeypatch, _json_handler([]))
    update_check.check()
    req = calls[0]
    assert str(req.url) == "https://api.github.com/repos/plynte-labs/opencohost/releases?per_page=10"
    assert req.method == "GET"
    assert req.headers["user-agent"] == f"OpenCohost/{opencohost.__version__}"
    assert req.headers["accept"] == "application/vnd.github+json"
    assert not {"authorization", "cookie"} & set(req.headers)
    assert req.content == b""


def test_disabled_makes_no_call(monkeypatch):
    calls = _install(monkeypatch, _json_handler([_rel("v9.9.9")]))
    update_setting.set_enabled(False)
    got = update_check.check()
    assert calls == [] and got["newer"] is False


def test_result_is_cached_for_the_launch(monkeypatch):
    calls = _install(monkeypatch, _json_handler([_rel("v0.5.0-alpha.2")]))
    first = update_check.check()
    second = update_check.check()
    assert first == second and len(calls) == 1


def test_log_line_never_carries_the_body(monkeypatch, caplog):
    _install(monkeypatch, _json_handler([_rel("v0.5.1-alpha.2")]))
    with caplog.at_level("INFO"):
        update_check.check()
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "[UPDATE_CHECK] outcome=ok newer=1" in text
    assert "alpha.2" not in text and "github.com" not in text


# --- toggle store ----------------------------------------------------------------


def test_default_is_on_and_file_lives_in_config_dir():
    assert update_setting.is_enabled() is True
    assert settings.UPDATE_CHECK_FILE == os.path.join(settings.CONFIG_DIR, "update_check.json")


def test_save_load_roundtrip_and_validation():
    assert update_setting.save_update_check(False) is False
    update_setting.reset_update_check()
    assert update_setting.is_enabled() is True
    assert update_setting.load_update_check() is False
    with pytest.raises(ValueError):
        update_setting.save_update_check("no")


def test_corrupt_file_falls_back_to_default():
    with open(update_setting.UPDATE_CHECK_FILE, "w", encoding="utf-8") as f:
        f.write("{nope")
    assert update_setting.load_update_check() is True
    with open(update_setting.UPDATE_CHECK_FILE, "w", encoding="utf-8") as f:
        json.dump({"enabled": "x"}, f)
    assert update_setting.load_update_check() is True


# --- routes ----------------------------------------------------------------------


def test_get_update_shape(monkeypatch):
    _install(monkeypatch, _json_handler([_rel("v0.5.1-alpha.2")]))
    with TestClient(_app()) as client:
        resp = client.get("/api/update")
    assert resp.status_code == 200
    assert resp.json() == {"enabled": True, "current": opencohost.__version__,
                           "latest": "0.5.1-alpha.2", "newer": True, "url": _BASE + "v0.5.1-alpha.2"}


def test_get_update_when_disabled_does_not_call(monkeypatch):
    calls = _install(monkeypatch, _json_handler([_rel("v9.9.9")]))
    update_setting.save_update_check(False)  # persisted: startup reloads it from disk
    with TestClient(_app()) as client:
        body = client.get("/api/update").json()
    assert calls == [] and body["enabled"] is False and body["newer"] is False


def test_put_config_roundtrip_persists_and_startup_loads_it():
    with TestClient(_app()) as client:
        resp = client.put("/api/update/config", json={"enabled": False})
        assert resp.status_code == 200 and resp.json() == {"enabled": False}
        assert client.get("/api/update").json()["enabled"] is False
    update_setting.reset_update_check()
    with TestClient(_app()) as client:  # lifespan loads the file
        assert client.get("/api/update").json()["enabled"] is False


@pytest.mark.parametrize("body", [{}, {"enabled": "yes"}, {"enabled": 1}, {"enabled": None}, {"enabled": True, "x": 1}])
def test_put_config_invalid_is_422(body):
    with TestClient(_app()) as client:
        resp = client.put("/api/update/config", json=body)
    assert resp.status_code == 422
    assert resp.json() == {"detail": "invalid_update_setting"}
    assert update_setting.is_enabled() is True


def test_put_config_write_failure_is_503(monkeypatch):
    with TestClient(_app()) as client:
        monkeypatch.setattr(update_setting, "UPDATE_CHECK_FILE", os.path.join(os.devnull, "x", "u.json"))
        resp = client.put("/api/update/config", json={"enabled": False})
    assert resp.status_code == 503
    assert resp.json() == {"detail": "update_setting_write_failed"}
    assert update_setting.is_enabled() is True


@pytest.mark.parametrize("url", ["https://github.com/plynte-labs/OpenCohost/releases/tag/v9.0.0",
                                 "https://GitHub.com/Plynte-Labs/OPENCOHOST/releases/tag/v9.0.0"])
def test_canonical_repo_casing_is_accepted(monkeypatch, url):
    # GitHub returns html_url with the repo's canonical casing; owner/repo names are case-insensitive.
    rel = _rel("v9.0.0")
    rel["html_url"] = url
    _install(monkeypatch, _json_handler([rel]))
    got = update_check.check()
    assert got["newer"] is True and got["url"] == url


def test_lookalike_repo_is_still_rejected(monkeypatch):
    rel = _rel("v9.0.0")
    rel["html_url"] = "https://github.com/plynte-labs/OpenCohost-evil/releases/tag/v9.0.0"
    _install(monkeypatch, _json_handler([rel]))
    assert update_check.check()["newer"] is False
