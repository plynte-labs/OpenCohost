"""T2 of remote-voice-gateway: device pairing, device-token auth, operator endpoints.

The gateway is the only part of OpenCohost the owner exposes through a tunnel, so
this suite pins the security contract: one-time codes (single use, 120 s TTL, one
live at a time, burned after repeated failures), hash-only persistence, constant-
time verification, revocation, a generic 401 on /pair, and operator-only
/api/remote/* on the main API even while API_AUTH_ENFORCED is off.

tests/conftest.py redirects settings.REMOTE_DEVICES_FILE and API_TOKENS_FILE to
per-test temp paths (autouse).
"""

import hashlib
import json
import logging
import re
import threading
import urllib.parse
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from opencohost.config import settings
from tests.test_api_phase1 import FakeHost


class FakeClock:
    def __init__(self, now: float = 1_000_000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


def _registry(clock=None, path=None):
    from opencohost.gateway.devices import DeviceRegistry

    return DeviceRegistry(path=path, clock=clock or FakeClock())


def _devices_path() -> Path:
    return Path(settings.REMOTE_DEVICES_FILE)


def _sha(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@pytest.fixture(autouse=True)
def _reset_host_active():
    import opencohost.api.main as main_mod

    main_mod._host_active = False
    yield
    main_mod._host_active = False


# --- registry: pairing codes ------------------------------------------------


def test_mint_then_exchange_succeeds():
    clock = FakeClock()
    reg = _registry(clock)
    code, expires_at = reg.mint_code()
    assert expires_at == clock.now + 120
    result = reg.exchange(code, "Pixel")
    assert result is not None
    device_id, token = result
    assert isinstance(device_id, str) and device_id
    assert isinstance(token, str) and len(token) >= 43  # token_urlsafe(32)
    assert reg.verify(token) == device_id


def test_code_has_at_least_40_bits_and_is_url_safe():
    from opencohost.gateway import devices

    assert len(set(devices.CODE_ALPHABET)) ** devices.CODE_LENGTH >= 2**40
    reg = _registry()
    codes = {reg.mint_code()[0] for _ in range(50)}
    assert len(codes) == 50
    for code in codes:
        assert len(code) == devices.CODE_LENGTH
        assert set(code) <= set(devices.CODE_ALPHABET)
        assert urllib.parse.quote(code, safe="") == code


def test_code_is_single_use():
    reg = _registry()
    code, _ = reg.mint_code()
    assert reg.exchange(code, "a") is not None
    assert reg.exchange(code, "b") is None


def test_expired_code_fails():
    clock = FakeClock()
    reg = _registry(clock)
    code, _ = reg.mint_code()
    clock.now += 121
    assert reg.exchange(code, "late") is None


def test_code_still_valid_just_before_expiry():
    clock = FakeClock()
    reg = _registry(clock)
    code, _ = reg.mint_code()
    clock.now += 119
    assert reg.exchange(code, "on time") is not None


def test_new_mint_invalidates_previous_code():
    reg = _registry()
    old, _ = reg.mint_code()
    new, _ = reg.mint_code()
    assert old != new
    assert reg.exchange(old, "old") is None
    assert reg.exchange(new, "new") is not None


def test_exchange_is_case_and_separator_tolerant():
    reg = _registry()
    code, _ = reg.mint_code()
    typed = f" {code[:5].lower()}-{code[5:].lower()} "
    assert reg.exchange(typed, "typed") is not None


def test_attempt_limit_burns_the_code():
    from opencohost.gateway import devices

    reg = _registry()
    code, _ = reg.mint_code()
    for _ in range(devices.MAX_PAIR_ATTEMPTS):
        assert reg.exchange("WRONGWRONG", "x") is None
    assert reg.exchange(code, "too late") is None
    fresh, _ = reg.mint_code()
    assert reg.exchange(fresh, "fresh") is not None


def test_attempts_below_the_limit_keep_the_code():
    from opencohost.gateway import devices

    reg = _registry()
    code, _ = reg.mint_code()
    for _ in range(devices.MAX_PAIR_ATTEMPTS - 1):
        assert reg.exchange("WRONGWRONG", "x") is None
    assert reg.exchange(code, "ok") is not None


def test_concurrent_exchange_of_one_code_yields_one_device():
    reg = _registry()
    code, _ = reg.mint_code()
    results = []
    barrier = threading.Barrier(16)

    def worker():
        barrier.wait()
        results.append(reg.exchange(code, "race"))

    threads = [threading.Thread(target=worker) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(r is not None for r in results) == 1
    assert len(reg.list_devices()) == 1


# --- registry: persistence and verification --------------------------------


def test_store_never_contains_the_raw_token():
    reg = _registry()
    code, _ = reg.mint_code()
    device_id, token = reg.exchange(code, "Pixel")
    raw = _devices_path().read_text(encoding="utf-8")
    assert token not in raw
    assert code not in raw
    data = json.loads(raw)
    [entry] = data["devices"]
    assert set(entry) == {"device_id", "name", "token_sha256", "created_at", "last_used_at", "revoked"}
    assert entry["token_sha256"] == _sha(token)
    assert entry["device_id"] == device_id
    assert entry["revoked"] is False


def test_devices_survive_a_new_registry_instance():
    reg = _registry()
    code, _ = reg.mint_code()
    device_id, token = reg.exchange(code, "Pixel")
    assert _registry().verify(token) == device_id


def test_verify_uses_compare_digest(monkeypatch):
    from opencohost.gateway import devices

    reg = _registry()
    code, _ = reg.mint_code()
    device_id, token = reg.exchange(code, "Pixel")
    calls = []
    real = devices.hmac.compare_digest

    def spy(a, b):
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(devices.hmac, "compare_digest", spy)
    assert reg.verify(token) == device_id
    assert any(_sha(token) in (a, b) or _sha(token).encode() in (a, b) for a, b in calls)


@pytest.mark.parametrize("bad", [None, "", "nope", "x" * 5000, 123])
def test_verify_rejects_unknown_or_malformed_tokens(bad):
    reg = _registry()
    code, _ = reg.mint_code()
    reg.exchange(code, "Pixel")
    assert reg.verify(bad) is None


def test_revoke_makes_verify_fail_and_persists():
    reg = _registry()
    code, _ = reg.mint_code()
    device_id, token = reg.exchange(code, "Pixel")
    assert reg.revoke(device_id) is True
    assert reg.verify(token) is None
    assert _registry().verify(token) is None
    assert json.loads(_devices_path().read_text(encoding="utf-8"))["devices"][0]["revoked"] is True


def test_revoke_unknown_device_is_false():
    assert _registry().revoke("does-not-exist") is False


@pytest.mark.parametrize(
    "content",
    [
        "{not json",
        "[]",
        json.dumps({"version": 2, "devices": []}),
        json.dumps({"version": 1, "devices": "nope"}),
        json.dumps({"version": 1, "devices": [{"device_id": "a"}]}),
        json.dumps(
            {
                "version": 1,
                "devices": [
                    {
                        "device_id": "a",
                        "name": "n",
                        "token_sha256": "not-a-hash",
                        "created_at": 1.0,
                        "last_used_at": None,
                        "revoked": False,
                    }
                ],
            }
        ),
    ],
)
def test_malformed_file_means_zero_devices_and_one_quiet_warning(content, caplog):
    path = _devices_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="opencohost.gateway.devices"):
        reg = _registry()
    assert reg.list_devices() == []
    assert reg.verify("anything") is None
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert str(path) not in message
    assert "not-a-hash" not in message and "{not json" not in message


def test_missing_file_means_zero_devices_without_warning(caplog):
    with caplog.at_level(logging.WARNING, logger="opencohost.gateway.devices"):
        reg = _registry()
    assert reg.list_devices() == []
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_last_used_writes_are_throttled(monkeypatch):
    from opencohost.gateway import devices

    clock = FakeClock()
    reg = _registry(clock)
    code, _ = reg.mint_code()
    device_id, token = reg.exchange(code, "Pixel")
    writes = []
    real = devices.atomic_write_text
    monkeypatch.setattr(devices, "atomic_write_text", lambda p, t: (writes.append(t), real(p, t)))
    for _ in range(20):
        clock.now += 1
        assert reg.verify(token) == device_id
    assert len(writes) <= 1
    before = len(writes)
    clock.now += devices.LAST_USED_WRITE_INTERVAL_S + 1
    assert reg.verify(token) == device_id
    assert len(writes) == before + 1
    entry = json.loads(_devices_path().read_text(encoding="utf-8"))["devices"][0]
    assert entry["last_used_at"] == pytest.approx(clock.now)


def test_list_devices_has_no_hash():
    reg = _registry()
    code, _ = reg.mint_code()
    device_id, token = reg.exchange(code, "Pixel")
    [item] = reg.list_devices()
    assert set(item) == {"device_id", "name", "created_at", "last_used_at", "revoked"}
    blob = json.dumps(reg.list_devices())
    assert _sha(token) not in blob
    assert token not in blob
    assert not re.search(r"[0-9a-f]{64}", blob)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("  Pixel 7  ", "Pixel 7"),
        ("Pix\x00el\n7\t", "Pixel7"),
        ("‮evil​ phone", "evil phone"),
        ("", "Phone"),
        ("   \x07 ", "Phone"),
        (None, "Phone"),
        (12345, "Phone"),
        ("a" * 500, "a" * 40),
    ],
)
def test_name_is_sanitized(raw, expected):
    reg = _registry()
    code, _ = reg.mint_code()
    reg.exchange(code, raw)
    [item] = reg.list_devices()
    assert item["name"] == expected


def test_logs_never_carry_codes_tokens_hashes_or_names(caplog):
    from opencohost.gateway import devices

    clock = FakeClock()
    with caplog.at_level(logging.DEBUG, logger="opencohost.gateway.devices"):
        reg = _registry(clock)
        code, _ = reg.mint_code()
        device_id, token = reg.exchange(code, "SecretPhoneName")
        reg.verify(token)
        clock.now += devices.LAST_USED_WRITE_INTERVAL_S + 1
        reg.verify(token)
        bad_code, _ = reg.mint_code()
        for _ in range(devices.MAX_PAIR_ATTEMPTS):
            reg.exchange("WRONGWRONG", "x")
        reg.revoke(device_id)
    text = "\n".join(r.getMessage() for r in caplog.records)
    for secret in (code, bad_code, token, _sha(token), "SecretPhoneName", "WRONGWRONG"):
        assert secret not in text
    assert device_id in text  # device ids and counts are allowed


# --- gateway routes -----------------------------------------------------------


def _gateway(registry):
    from opencohost.gateway.app import create_gateway_app

    return TestClient(create_gateway_app(FakeHost(), object(), registry))


def test_pair_route_returns_a_token_that_whoami_accepts():
    reg = _registry()
    client = _gateway(reg)
    code, _ = reg.mint_code()
    resp = client.post("/pair", json={"code": code, "name": "Pixel"})
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"device_id", "token"}
    assert "no-store" in resp.headers.get("cache-control", "")
    who = client.get("/whoami", headers={"Authorization": f"Bearer {body['token']}"})
    assert who.status_code == 200
    assert who.json() == {"device_id": body["device_id"]}


def _wrong(reg, clock):
    reg.mint_code()
    return "WRONGWRONG"


def _expired(reg, clock):
    code, _ = reg.mint_code()
    clock.now += 500
    return code


def _burned(reg, clock):
    from opencohost.gateway import devices

    code, _ = reg.mint_code()
    for _ in range(devices.MAX_PAIR_ATTEMPTS):
        reg.exchange("WRONGWRONG", "x")
    return code


def _reused(reg, clock):
    code, _ = reg.mint_code()
    reg.exchange(code, "first")
    return code


def _none_live(reg, clock):
    return "ABCDEFGHJK"


@pytest.mark.parametrize("scenario", [_wrong, _expired, _burned, _reused, _none_live])
def test_pair_failures_are_one_generic_401(scenario):
    clock = FakeClock()
    reg = _registry(clock)
    client = _gateway(reg)
    code = scenario(reg, clock)
    resp = client.post("/pair", json={"code": code, "name": "x"})
    assert resp.status_code == 401
    assert resp.json() == {"error": "pairing_failed"}


def test_pair_rejects_oversized_body():
    reg = _registry()
    code, _ = reg.mint_code()
    resp = _gateway(reg).post("/pair", json={"code": code, "name": "x" * 10_000})
    assert resp.status_code == 413
    assert reg.exchange(code, "still live") is not None


@pytest.mark.parametrize("body", [b"{bad json", b"[]", b'{"name": "x"}', b'{"code": 5}'])
def test_pair_rejects_malformed_body(body):
    reg = _registry()
    resp = _gateway(reg).post("/pair", content=body, headers={"Content-Type": "application/json"})
    assert resp.status_code in (400, 401)
    assert "detail" not in resp.json() or not isinstance(resp.json()["detail"], list)


@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": "Bearer nope"}, {"Authorization": "Basic abc"}, {"Authorization": "Bearer "}],
)
def test_whoami_requires_a_valid_device_token(headers):
    resp = _gateway(_registry()).get("/whoami", headers=headers)
    assert resp.status_code == 401
    assert resp.headers.get("www-authenticate") == "Bearer"


def test_revoked_device_gets_401():
    reg = _registry()
    client = _gateway(reg)
    code, _ = reg.mint_code()
    body = client.post("/pair", json={"code": code, "name": "Pixel"}).json()
    auth = {"Authorization": f"Bearer {body['token']}"}
    assert client.get("/whoami", headers=auth).status_code == 200
    reg.revoke(body["device_id"])
    assert client.get("/whoami", headers=auth).status_code == 401


def test_gateway_without_registry_fails_closed():
    client = _gateway(None)
    assert client.post("/pair", json={"code": "ABCDEFGHJK", "name": "x"}).status_code == 401
    assert client.get("/whoami", headers={"Authorization": "Bearer x"}).status_code == 401
    assert client.get("/status").status_code == 200


# --- main API operator endpoints --------------------------------------------


@pytest.fixture
def gateway_on(monkeypatch):
    """Gateway flag on, with a socket-free server double that keeps the app."""
    import opencohost.gateway.server as server_mod

    captured = {}

    class FakeServer:
        def __init__(self, app, port):
            captured["app"] = app

        def start(self):
            return True

        def stop(self):
            pass

    monkeypatch.setattr(server_mod, "GatewayServer", FakeServer)
    monkeypatch.setattr(settings, "REMOTE_GATEWAY_ENABLED", True)
    monkeypatch.setattr(settings, "REMOTE_GATEWAY_PORT", 8793)
    monkeypatch.setattr(settings, "API_AUTH_ENFORCED", False)
    return captured


def _main_app():
    import opencohost.api.main as main_mod

    return main_mod.create_app(host_factory=FakeHost, cors_origins=["http://localhost:5173"])


def _role_headers(role: str) -> dict:
    import opencohost.api.auth as auth

    return {"Authorization": f"Bearer {auth.load_tokens()[role]}"}


_REMOTE_CALLS = [
    ("post", "/api/remote/pair-code"),
    ("get", "/api/remote/devices"),
    ("delete", "/api/remote/devices/abc"),
]


@pytest.mark.parametrize("method,path", _REMOTE_CALLS)
def test_remote_endpoints_need_the_operator_token_even_with_enforcement_off(gateway_on, method, path):
    with TestClient(_main_app()) as client:
        assert getattr(client, method)(path).status_code == 401
        bad = {"Authorization": "Bearer not-a-token"}
        assert getattr(client, method)(path, headers=bad).status_code == 401
        assert getattr(client, method)(path, headers=_role_headers("agent")).status_code == 403


@pytest.mark.parametrize("method,path", _REMOTE_CALLS)
def test_remote_endpoints_return_503_when_the_token_file_is_unusable(gateway_on, method, path):
    with TestClient(_main_app()) as client:
        Path(settings.API_TOKENS_FILE).write_text("{broken", encoding="utf-8")
        resp = getattr(client, method)(path, headers={"Authorization": "Bearer x"})
        assert resp.status_code == 503


def test_operator_can_mint_list_and_revoke(gateway_on):
    app = _main_app()
    with TestClient(app) as client:
        op = _role_headers("operator")
        resp = client.post("/api/remote/pair-code", headers=op)
        assert resp.status_code == 200
        body = resp.json()
        assert set(body) == {"code", "expires_at", "gateway_port"}
        assert body["gateway_port"] == 8793
        assert "no-store" in resp.headers.get("cache-control", "")

        device_id, token = app.state.remote_devices.exchange(body["code"], "Pixel")
        listing = client.get("/api/remote/devices", headers=op)
        assert listing.status_code == 200
        [item] = listing.json()["devices"]
        assert item["device_id"] == device_id and item["name"] == "Pixel"
        assert _sha(token) not in listing.text and token not in listing.text

        assert client.delete(f"/api/remote/devices/{device_id}", headers=op).status_code == 200
        assert client.get("/api/remote/devices", headers=op).json()["devices"][0]["revoked"] is True
        assert app.state.remote_devices.verify(token) is None
        assert client.delete("/api/remote/devices/unknown", headers=op).status_code == 404


def test_main_api_and_gateway_share_one_registry(gateway_on):
    app = _main_app()
    with TestClient(app) as client:
        gateway_app = gateway_on["app"]
        assert gateway_app.state.devices is app.state.remote_devices
        code = client.post("/api/remote/pair-code", headers=_role_headers("operator")).json()["code"]
        paired = TestClient(gateway_app).post("/pair", json={"code": code, "name": "Pixel"})
        assert paired.status_code == 200
        listing = client.get("/api/remote/devices", headers=_role_headers("operator")).json()
        assert [d["device_id"] for d in listing["devices"]] == [paired.json()["device_id"]]


@pytest.mark.parametrize("method,path", _REMOTE_CALLS)
def test_remote_endpoints_409_when_the_gateway_is_off(monkeypatch, method, path):
    monkeypatch.setattr(settings, "REMOTE_GATEWAY_ENABLED", False)
    with TestClient(_main_app()) as client:
        resp = getattr(client, method)(path, headers=_role_headers("operator"))
        assert resp.status_code == 409
        assert resp.json() == {"error": "gateway_disabled"}
