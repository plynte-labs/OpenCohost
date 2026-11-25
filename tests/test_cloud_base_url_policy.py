"""API key egress hardening (2026-11-25): https-or-private base URLs, host-bound
keys, client-side guard. HTTP to providers is always mocked."""

import pytest
import requests
from fastapi.testclient import TestClient

from tests.test_api_phase1 import FakeHost

_ORIGINS = ["http://localhost:5173"]
_KEY = "sk-SENTINEL-egress-123456"


@pytest.fixture(autouse=True)
def _reset_host_active():
    import opencohost.api.main as main_mod

    main_mod._host_active = False
    yield
    main_mod._host_active = False


@pytest.fixture(autouse=True)
def _isolated_files(tmp_path, monkeypatch):
    import opencohost.api.main as main_mod
    import opencohost.config.llm_provider as llm_provider_mod

    monkeypatch.setattr(
        llm_provider_mod, "LLM_PROVIDER_CONFIG_FILE", str(tmp_path / "llm_provider.json")
    )
    monkeypatch.setattr(main_mod, "LLM_KEYS_FILE", str(tmp_path / "llm_keys.json"))


def _client():
    import opencohost.api.main as main_mod

    return TestClient(main_mod.create_app(host_factory=FakeHost, cors_origins=_ORIGINS))


# -- helper ------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://api.openai.com/v1",
        "https://anything.example/v1",
        "http://localhost:11434/v1",
        "http://127.0.0.1:1234/v1",
        "http://127.5.5.5/v1",
        "http://[::1]:8000/v1",
        "http://192.168.1.20:8080/v1",
        "http://10.0.0.5/v1",
        "http://172.16.0.9/v1",
        "http://169.254.1.1/v1",
        "http://mybox.local:8000/v1",
    ],
)
def test_allowed_urls(url):
    from opencohost.security.cloud_url import is_allowed_cloud_base_url

    assert is_allowed_cloud_base_url(url) is True


@pytest.mark.parametrize(
    "url",
    [
        "http://api.openai.com/v1",
        "http://8.8.8.8/v1",
        "http://172.32.0.1/v1",
        "http://evil.example.local.attacker.com/v1",
        "https://user:pw@api.openai.com/v1",
        "https://user@api.openai.com/v1",
        "ftp://api.openai.com/v1",
        "https:///v1",
        "https://",
        "",
        "api.openai.com/v1",
        "javascript:alert(1)",
    ],
)
def test_rejected_urls(url):
    from opencohost.security.cloud_url import is_allowed_cloud_base_url

    assert is_allowed_cloud_base_url(url) is False


def test_non_string_rejected():
    from opencohost.security.cloud_url import is_allowed_cloud_base_url

    assert is_allowed_cloud_base_url(None) is False  # type: ignore[arg-type]


# -- PUT: scheme policy --------------------------------------------------------


def test_put_http_public_host_rejected():
    with _client() as c:
        r = c.put(
            "/api/llm/provider",
            json={"profile_id": "p1", "base_url": "http://api.example.com/v1"},
        )
    assert r.status_code == 422 and r.json() == {"detail": "insecure_base_url"}


def test_put_http_local_and_private_allowed():
    with _client() as c:
        for i, u in enumerate(["http://localhost:11434/v1", "http://192.168.1.4:9/v1"]):
            r = c.put("/api/llm/provider", json={"profile_id": f"p{i}", "base_url": u})
            assert r.status_code == 200, u


def test_put_userinfo_rejected():
    with _client() as c:
        r = c.put(
            "/api/llm/provider",
            json={"profile_id": "p1", "base_url": "https://u:p@api.openai.com/v1"},
        )
    assert r.status_code == 422 and r.json()["detail"] == "insecure_base_url"


def test_active_profile_with_disallowed_stored_url_reports_code():
    from opencohost.config.llm_provider import save_provider_config

    save_provider_config(
        {
            "active_provider": "old",
            "profiles": {"old": {"base_url": "http://api.example.com/v1", "model": "m"}},
        }
    )
    with _client() as c:
        # Reading a legacy disallowed URL must not crash.
        assert c.get("/api/llm/provider").status_code == 200
        r = c.put("/api/llm/provider", json={"pregen_enabled": True})
    assert r.status_code == 422 and r.json() == {"detail": "insecure_base_url"}


# -- PUT: host-bound key -------------------------------------------------------


def _seed(c, url="https://api.a.example/v1", key=_KEY, pid="p1"):
    r = c.put(
        "/api/llm/provider",
        json={"profile_id": pid, "base_url": url, "model": "m", "api_key": key},
    )
    assert r.status_code == 200
    return r


def test_host_change_without_key_rejected():
    with _client() as c:
        _seed(c)
        r = c.put(
            "/api/llm/provider",
            json={"profile_id": "p1", "base_url": "https://api.b.example/v1"},
        )
        assert r.status_code == 422 and r.json() == {"detail": "key_reentry_required"}
        prof = c.get("/api/llm/provider").json()["profiles"]["p1"]
        assert prof["base_url"] == "https://api.a.example/v1"


def test_host_change_with_new_key_ok():
    with _client() as c:
        _seed(c)
        r = c.put(
            "/api/llm/provider",
            json={"profile_id": "p1", "base_url": "https://api.b.example/v1", "api_key": "k2"},
        )
        assert r.status_code == 200
        assert r.json()["profiles"]["p1"]["api_key_set"] is True


def test_same_host_path_change_without_key_ok():
    with _client() as c:
        _seed(c)
        r = c.put(
            "/api/llm/provider",
            json={"profile_id": "p1", "base_url": "https://API.a.example:443/v2"},
        )
        assert r.status_code == 200


def test_port_change_counts_as_host_change():
    with _client() as c:
        _seed(c)
        r = c.put(
            "/api/llm/provider",
            json={"profile_id": "p1", "base_url": "https://api.a.example:8443/v1"},
        )
        assert r.status_code == 422 and r.json()["detail"] == "key_reentry_required"


def test_host_change_without_stored_key_ok():
    with _client() as c:
        c.put("/api/llm/provider", json={"profile_id": "p1", "base_url": "https://a.example/v1"})
        r = c.put("/api/llm/provider", json={"profile_id": "p1", "base_url": "https://b.example/v1"})
        assert r.status_code == 200


def test_preset_default_url_equals_explicit_url_not_a_change():
    with _client() as c:
        c.put(
            "/api/llm/provider",
            json={"profile_id": "p1", "preset": "openai", "api_key": _KEY, "model": "m"},
        )
        r = c.put(
            "/api/llm/provider",
            json={"profile_id": "p1", "base_url": "https://api.openai.com/v1/"},
        )
        assert r.status_code == 200


# -- /test: stored key bound to host --------------------------------------------


class _Resp:
    status_code = 200
    headers: dict = {}
    text = ""

    def json(self):
        return {"data": [{"id": "a"}]}


def _patch_get(monkeypatch):
    calls = []

    def fake_get(url, headers=None, timeout=None, **kw):
        calls.append((url, dict(headers or {})))
        return _Resp()

    import opencohost.api.routers.llm_provider as mod

    monkeypatch.setattr(mod.requests, "get", fake_get)
    return calls


def test_test_http_public_host_422(monkeypatch):
    calls = _patch_get(monkeypatch)
    with _client() as c:
        r = c.post(
            "/api/llm/provider/test",
            json={"base_url": "http://api.example.com/v1", "api_key": _KEY},
        )
    assert r.status_code == 422 and r.json() == {"detail": "insecure_base_url"}
    assert calls == []


def test_test_stored_key_not_sent_to_foreign_host(monkeypatch):
    calls = _patch_get(monkeypatch)
    with _client() as c:
        _seed(c)
        r = c.post(
            "/api/llm/provider/test",
            json={"profile_id": "p1", "base_url": "https://attacker.example/v1"},
        )
    assert calls == []
    assert r.json()["ok"] is False and r.json()["error_code"] == "key_reentry_required"
    assert _KEY not in r.text


def test_test_stored_key_used_for_same_host(monkeypatch):
    calls = _patch_get(monkeypatch)
    with _client() as c:
        _seed(c)
        r = c.post("/api/llm/provider/test", json={"profile_id": "p1"})
    assert r.json()["ok"] is True
    assert calls[0][1]["Authorization"] == f"Bearer {_KEY}"


def test_test_explicit_key_allowed_for_foreign_host(monkeypatch):
    calls = _patch_get(monkeypatch)
    with _client() as c:
        _seed(c)
        r = c.post(
            "/api/llm/provider/test",
            json={"profile_id": "p1", "base_url": "https://other.example/v1", "api_key": "new"},
        )
    assert r.json()["ok"] is True
    assert calls[0][1]["Authorization"] == "Bearer new"


# -- client guard -----------------------------------------------------------------


def test_client_refuses_insecure_url_send(monkeypatch):
    from opencohost.core.providers.cloud import cloud_llm_client as cl

    posted = []
    monkeypatch.setattr(cl.requests, "post", lambda *a, **k: posted.append(a) or None)
    with pytest.raises(requests.exceptions.RequestException) as ei:
        cl.send_chat_completion("http://api.example.com/v1?x=1", _KEY, "m", [], {}, 5)
    assert posted == []
    assert _KEY not in str(ei.value) and "example.com" not in str(ei.value)


def test_client_refuses_insecure_url_stream(monkeypatch):
    from opencohost.core.providers.cloud import cloud_llm_client as cl

    posted = []
    monkeypatch.setattr(cl.requests, "post", lambda *a, **k: posted.append(a) or None)
    with pytest.raises(requests.exceptions.RequestException):
        list(cl.stream_chat_completion("http://api.example.com/v1", _KEY, "m", [], {}, 5))
    assert posted == []
