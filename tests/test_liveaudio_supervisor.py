"""Tests for the local LiveAudio service client (liveaudio-service-client track).

A real ``liveaudio-service`` process or socket is NEVER launched: subprocess
and the WS handshake are faked throughout. Covers command resolution, spawn
argv (``shell=False`` + ``--parent-pid``), JSONL parsing, single-flight
ensure, hello-validated ``base..base+9`` scan, remote-URI no-spawn,
runtime-only repoint (no persistence), idempotent shutdown, and the API
retryable ``stt_loading`` path.
"""

import io
import json
import os
import threading

import pytest
from fastapi.testclient import TestClient

import opencohost.api.ptt_session as ptt_session_mod
from opencohost.config import settings
from opencohost.stt import discovery
from opencohost.stt.discovery import (
    base_port_of,
    ensure_local_service,
    find_liveaudio_port,
    is_loopback_host,
    is_loopback_uri,
    validate_hello,
    with_port,
)
from opencohost.stt.supervisor import (
    DEFAULT_PORT_TIMEOUT,
    DEFAULT_READY_TIMEOUT,
    EXPLICIT_ENV_VAR,
    LiveAudioSupervisor,
    parse_service_line,
    resolve_installed_service_exe,
    resolve_liveaudio_command,
)
from tests.test_api_phase1 import FakeHost


# ──────────────────────────────────────────────────────────────────────────
# Fakes
# ──────────────────────────────────────────────────────────────────────────


class FakePopen:
    """Minimal Popen double: records argv/kwargs, serves canned stdout lines."""

    instances = []
    exit_code = None  # when set, poll() reports a dead process with this code

    def __init__(self, argv, **kwargs):
        self.argv = argv
        self.kwargs = kwargs
        self.stdout = io.StringIO("".join(FakePopen.script))
        self.terminated = 0
        self.killed = 0
        self.wait_calls = 0
        FakePopen.instances.append(self)

    def poll(self):
        return FakePopen.exit_code  # None = alive, int = exited

    def terminate(self):
        self.terminated += 1

    def kill(self):
        self.killed += 1

    def wait(self, timeout=None):
        self.wait_calls += 1
        return 0


FakePopen.script = []


@pytest.fixture(autouse=True)
def _reset_host_active():
    import opencohost.api.main as main_mod

    main_mod._host_active = False
    FakePopen.instances.clear()
    FakePopen.script = []
    FakePopen.exit_code = None
    yield
    main_mod._host_active = False


def _event_line(**fields):
    base = {
        "schema": "liveaudio.service.event",
        "version": 1,
        "service_pid": 4242,
        "parent_pid": os.getpid(),
    }
    base.update(fields)
    return json.dumps(base) + "\n"


def _supervisor_with_lines(*lines, argv=("liveaudio-service",), **kwargs):
    FakePopen.script = list(lines)
    return LiveAudioSupervisor(
        command=list(argv),
        parent_pid=os.getpid(),
        popen_factory=FakePopen,
        **kwargs,
    )


class _FakeSupervisor:
    """Wiring double for ensure/API tests (no threads, no I/O)."""

    def __init__(self, *, started=True, port=None, loading=False):
        self.ensure_lock = threading.Lock()
        self._started = started
        self._port = port
        self._loading = loading
        self.ensure_calls = 0
        self.shutdown_calls = 0

    def ensure_started(self):
        self.ensure_calls += 1
        return self._started

    def wait_for_port(self, timeout=None):
        return self._port

    def wait_for_ready(self, timeout=None):
        return not self._loading

    def is_loading(self):
        return self._loading

    def status(self):
        return {
            "spawned": self._started,
            "running": self._started,
            "port": self._port,
            "base_port": 8765,
            "service_state": "running",
            "asr_state": "loading" if self._loading else "ready",
            "fatal": None,
        }

    def shutdown(self):
        self.shutdown_calls += 1


class _FakeController:
    def __init__(self, uri="ws://127.0.0.1:8765"):
        self._uri = uri
        self.repoints = []

    def set_ws_uri(self, uri):
        self.repoints.append(uri)
        self._uri = uri


# ──────────────────────────────────────────────────────────────────────────
# Command resolution
# ──────────────────────────────────────────────────────────────────────────


def test_resolve_explicit_setting_wins_over_env_and_path(monkeypatch):
    monkeypatch.setenv(EXPLICIT_ENV_VAR, "env-cmd --x 1")
    argv = resolve_liveaudio_command("explicit-cmd --y 2")
    assert argv[0] == "explicit-cmd"
    assert "--parent-pid" not in argv  # appended at spawn, not resolved


def test_resolve_env_used_for_test_dev_only(monkeypatch):
    monkeypatch.setenv(EXPLICIT_ENV_VAR, "dev-service --flag")
    argv = resolve_liveaudio_command()
    assert argv and argv[0] == "dev-service"


def test_resolve_path_lookup_and_missing(monkeypatch):
    import shutil

    monkeypatch.delenv(EXPLICIT_ENV_VAR, raising=False)
    monkeypatch.setattr(shutil, "which", lambda name: "C:" + os.sep + "svc" + os.sep + "liveaudio-service.exe")  # path-ok: fake PATH entry
    assert resolve_liveaudio_command() == ["C:" + os.sep + "svc" + os.sep + "liveaudio-service.exe"]  # path-ok: fake PATH entry
    monkeypatch.setattr(shutil, "which", lambda name: None)
    assert resolve_liveaudio_command() is None


# ──────────────────────────────────────────────────────────────────────────
# Spawn argv + JSONL parsing
# ──────────────────────────────────────────────────────────────────────────


def test_spawn_uses_parent_pid_no_shell_pipe():
    sup = _supervisor_with_lines()
    assert sup.ensure_started() is True
    proc = FakePopen.instances[0]
    assert proc.argv[0] == "liveaudio-service"
    assert "--parent-pid" in proc.argv
    assert proc.argv[proc.argv.index("--parent-pid") + 1] == str(os.getpid())
    assert proc.kwargs["shell"] is False
    assert proc.kwargs["stderr"] is not None
    sup.shutdown()


def test_spawn_without_binary_returns_false_and_spawns_nothing():
    sup = LiveAudioSupervisor(command=None, popen_factory=FakePopen)
    # Force "not installed" regardless of the dev machine PATH.
    sup._argv = None
    assert sup.ensure_started() is False
    assert FakePopen.instances == []


def test_parse_service_line_accepts_both_ws_port_shapes():
    supervisor_shape = _event_line(type="ws_port", base_port=8765, effective_port=8771)
    child_shape = _event_line(type="ws_port", port=8771, base=8765)
    assert parse_service_line(supervisor_shape)["effective_port"] == 8771
    assert parse_service_line(child_shape)["port"] == 8771


def test_parse_service_line_rejects_noise_and_scrubs_sensitive_keys():
    assert parse_service_line("") is None
    assert parse_service_line("not json {{") is None
    assert parse_service_line("[1, 2]") is None
    assert parse_service_line(json.dumps({"no": "type"})) is None
    leaked = _event_line(type="service_state", state="running", text="secret words",
                         transcript="secret words", path="C:" + os.sep + "private" + os.sep + "x")  # path-ok: scrub fixture
    parsed = parse_service_line(leaked)
    assert parsed["state"] == "running"
    assert "text" not in parsed and "transcript" not in parsed and "path" not in parsed


def test_ws_port_event_sets_effective_port_and_unblocks_waiter():
    sup = _supervisor_with_lines(_event_line(type="ws_port", port=8771, base=8765))
    assert sup.ensure_started() is True
    assert sup.wait_for_port(timeout=5.0) == 8771
    assert sup.effective_port() == 8771
    sup.shutdown()


def test_fatal_event_unblocks_waiter_with_none_and_surfaces_code():
    sup = _supervisor_with_lines(_event_line(type="fatal", code="port-range-exhausted"))
    assert sup.ensure_started() is True
    assert sup.wait_for_port(timeout=5.0) is None
    assert sup.status()["fatal"] == "port-range-exhausted"
    sup.shutdown()


def test_single_flight_concurrent_ensure_spawns_once():
    sup = _supervisor_with_lines()
    results = []
    threads = [threading.Thread(target=lambda: results.append(sup.ensure_started()))
               for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert results == [True] * 8
    assert len(FakePopen.instances) == 1
    sup.shutdown()


def test_shutdown_idempotent_and_blocks_late_start():
    sup = _supervisor_with_lines()
    assert sup.ensure_started() is True
    proc = FakePopen.instances[0]
    sup.shutdown()
    sup.shutdown()
    sup.shutdown()
    assert proc.terminated == 1
    assert sup.ensure_started() is False


# ──────────────────────────────────────────────────────────────────────────
# Loopback gate + hello-validated scan
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("uri", [
    "ws://127.0.0.1:8765",
    "ws://localhost:8771",
    "ws://[::1]:8765",
])
def test_loopback_uris_are_spawn_eligible(uri):
    assert is_loopback_uri(uri) is True


@pytest.mark.parametrize("uri", [
    "ws://192.168.1.50:8765",
    "ws://capture-pc.lan:8765",
    "wss://example.com:8765",
    "http://127.0.0.1:8765",
    "ws://",
    "",
    None,
    123,
])
def test_remote_and_invalid_uris_never_spawn(uri):
    assert is_loopback_uri(uri) is False


def test_validate_hello_requires_app_proto_and_echoed_port():
    good = {"type": "hello", "app": "liveaudio", "proto": 1, "port": 8771}
    assert validate_hello(good, 8771) is True
    assert validate_hello(dict(good, port="8771"), 8771) is True
    assert validate_hello(dict(good, port=8765), 8771) is False
    assert validate_hello(dict(good, app="whisperlive"), 8771) is False
    assert validate_hello(dict(good, proto=2), 8771) is False
    assert validate_hello(dict(good, type="status"), 8771) is False
    assert validate_hello({"text": "hola"}, 8771) is False
    assert validate_hello(None, 8771) is False


def test_scan_only_accepts_validated_hello_inside_base_plus_9():
    seen = []

    def _handshake(uri, timeout):
        seen.append(uri)
        port = int(uri.rsplit(":", 1)[1])
        if port == 8768:
            return {"type": "hello", "app": "liveaudio", "proto": 1, "port": 8768}
        if port == 8766:
            return {"type": "hello", "app": "other", "proto": 1, "port": 8766}
        if port == 8767:
            return {"type": "hello", "app": "liveaudio", "proto": 1, "port": 9999}
        return None

    assert find_liveaudio_port(8765, handshake=_handshake) == 8768
    # Stops at the first validated hello: 8766 (wrong app) and 8767 (echoed
    # port mismatch) are rejected, 8769+ are never dialed.
    assert seen == [f"ws://127.0.0.1:{p}" for p in (8765, 8766, 8767, 8768)]


def test_scan_returns_none_when_nothing_validates():
    calls = []
    port = find_liveaudio_port(8765, handshake=lambda uri, t: calls.append(uri) or None)
    assert port is None
    assert len(calls) == 10  # base..base+9, never base+10
    assert not any(u.endswith(":8775") for u in calls)


def test_with_port_preserves_scheme_host_and_base_port_of():
    assert with_port("ws://127.0.0.1:8765", 8771) == "ws://127.0.0.1:8771"
    assert with_port("wss://capture-pc.lan:8765/sub", 8771).startswith("wss://capture-pc.lan:8771")
    assert with_port("ws://[::1]:8765", 8771) == "ws://[::1]:8771"
    assert base_port_of("ws://127.0.0.1:8771") == 8771
    assert base_port_of("garbage") == 8765


# ──────────────────────────────────────────────────────────────────────────
# Ensure orchestration
# ──────────────────────────────────────────────────────────────────────────


def test_remote_uri_never_spawns_and_never_probes():
    sup = _FakeSupervisor()
    probes = []
    outcome = ensure_local_service(
        configured_uri="ws://192.168.1.50:8765",
        controller=_FakeController(),
        supervisor=sup,
        probe=lambda uri: probes.append(uri) or (True, "connected"),
    )
    assert sup.ensure_calls == 0
    assert probes == []
    assert outcome.available is False and outcome.spawned is False


def test_configured_uri_already_up_means_no_spawn():
    sup = _FakeSupervisor()
    outcome = ensure_local_service(
        configured_uri="ws://127.0.0.1:8765",
        controller=_FakeController(),
        supervisor=sup,
        probe=lambda uri: (True, "connected"),
    )
    assert outcome.available is True and outcome.spawned is False
    assert sup.ensure_calls == 0


def test_spawned_port_repoints_runtime_without_persisting(monkeypatch):
    saved = []
    monkeypatch.setattr(settings, "save_ptt_ws_uri",
                        lambda uri, config_file=None: saved.append(uri))
    sup = _FakeSupervisor(started=True, port=8771)
    controller = _FakeController()

    def _probe(uri):
        return (True, "connected") if uri.endswith(":8771") else (False, "unreachable")

    def _boom_scan(base):
        raise AssertionError("scan must not run when stdout ws_port is authoritative")

    outcome = ensure_local_service(
        configured_uri="ws://127.0.0.1:8765",
        controller=controller,
        supervisor=sup,
        probe=_probe,
        scan=_boom_scan,
    )
    assert outcome.available is True and outcome.spawned is True
    assert outcome.uri == "ws://127.0.0.1:8771"
    assert controller.repoints == ["ws://127.0.0.1:8771"]
    assert saved == []  # ephemeral fallback port is runtime-only, never persisted


def test_scan_fallback_covers_manual_liveaudio_on_drifted_port():
    sup = _FakeSupervisor(started=False, port=None)
    controller = _FakeController()
    outcome = ensure_local_service(
        configured_uri="ws://127.0.0.1:8765",
        controller=controller,
        supervisor=sup,
        probe=lambda uri: (True, "connected") if uri.endswith(":8769") else (False, "unreachable"),
        scan=lambda base: 8769,
    )
    assert outcome.available is True and outcome.spawned is False
    assert outcome.uri == "ws://127.0.0.1:8769"


def test_nothing_found_reports_unreachable_not_loading():
    sup = _FakeSupervisor(started=False, port=None)
    outcome = ensure_local_service(
        configured_uri="ws://127.0.0.1:8765",
        controller=_FakeController(),
        supervisor=sup,
        probe=lambda uri: (False, "unreachable"),
        scan=lambda base: None,
    )
    assert outcome.available is False
    assert outcome.loading is False
    assert outcome.detail == "stt_unreachable"


def test_alive_but_loading_reports_retryable_detail():
    sup = _FakeSupervisor(started=True, port=8771, loading=True)
    controller = _FakeController()
    outcome = ensure_local_service(
        configured_uri="ws://127.0.0.1:8765",
        controller=controller,
        supervisor=sup,
        probe=lambda uri: (True, "connected") if uri.endswith(":8771") else (False, "unreachable"),
    )
    assert outcome.available is True and outcome.loading is True


def test_ensure_never_raises_on_broken_seams():
    class _Broken:
        ensure_lock = threading.Lock()

        def ensure_started(self):
            raise RuntimeError("boom")

        def wait_for_port(self, timeout=None):
            raise RuntimeError("boom")

        def is_loading(self):
            raise RuntimeError("boom")

    outcome = ensure_local_service(
        configured_uri="ws://127.0.0.1:8765",
        controller=_FakeController(),
        supervisor=_Broken(),
        probe=lambda uri: (_ for _ in ()).throw(OSError("down")),
        scan=lambda base: (_ for _ in ()).throw(OSError("down")),
    )
    assert outcome.available is False


# ──────────────────────────────────────────────────────────────────────────
# API wiring (fake supervisor injected through the lifespan factory seam)
# ──────────────────────────────────────────────────────────────────────────


def _app_with_supervisor(sup):
    import opencohost.api.main as main_mod

    return main_mod.create_app(
        host_factory=FakeHost,
        cors_origins=["http://localhost:5173"],
        liveaudio_supervisor_factory=lambda: sup,
    )


class _RefusingConnect:
    async def __aenter__(self):
        raise ConnectionRefusedError("liveaudio down")

    async def __aexit__(self, *exc):
        return False


def _patch_connect_for_ports(monkeypatch, live_ports):
    def _connect(uri, **kwargs):
        try:
            port = int(str(uri).rsplit(":", 1)[1].split("/")[0])
        except (ValueError, IndexError):
            port = -1
        if port in live_ports:
            class _Ok:
                async def __aenter__(self):
                    return self

                async def __aexit__(self, *exc):
                    return False

            return _Ok()
        return _RefusingConnect()

    monkeypatch.setattr(ptt_session_mod.websockets, "connect", _connect)


def test_api_start_returns_retryable_stt_loading_while_model_loads(monkeypatch):
    from opencohost.api.ptt_session import PttController

    _patch_connect_for_ports(monkeypatch, live_ports=set())
    sup = _FakeSupervisor(started=True, port=8771, loading=True)
    app = _app_with_supervisor(sup)
    client = TestClient(app)
    client.__enter__()
    try:
        app.state.ptt_controller = PttController(
            "ws://127.0.0.1:8765",
            app.state.dispatcher,
            app.state.host.event_log,
            ws_open_timeout=0.2,
        )
        resp = client.post("/api/ptt/start", json={})
        assert resp.status_code == 503
        assert resp.json() == {"detail": "stt_loading"}
        # Slot untouched: still honestly idle, never fake-listening.
        assert client.get("/api/ptt/state").json()["state"] == "idle"
    finally:
        client.__exit__(None, None, None)


def test_api_test_auto_discovers_spawned_port_without_persisting(monkeypatch):
    from opencohost.api.ptt_session import PttController

    _patch_connect_for_ports(monkeypatch, live_ports={8771})
    sup = _FakeSupervisor(started=True, port=8771)
    app = _app_with_supervisor(sup)
    client = TestClient(app)
    client.__enter__()
    try:
        controller = PttController(
            "ws://127.0.0.1:8765",
            app.state.dispatcher,
            app.state.host.event_log,
        )
        app.state.ptt_controller = controller
        resp = client.post("/api/ptt/test", json={})
        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "detail": "connected"}
        # Runtime repointed at the effective port...
        assert controller.state()["stt_ws_url"] == "ws://127.0.0.1:8771"
        # ...but never persisted: a restart still loads the configured default.
        assert settings.load_ptt_ws_uri() == settings.WS_URI
    finally:
        client.__exit__(None, None, None)


def test_api_state_reports_supervisor_summary_passively(monkeypatch):
    from opencohost.api.ptt_session import PttController

    sup = _FakeSupervisor(started=True, port=8771)
    app = _app_with_supervisor(sup)
    client = TestClient(app)
    client.__enter__()
    try:
        app.state.ptt_controller = PttController(
            "ws://127.0.0.1:8765",
            app.state.dispatcher,
            app.state.host.event_log,
        )
        body = client.get("/api/ptt/state").json()
        assert body["stt_service"]["mode"] == "auto"
        assert body["stt_service"]["port"] == 8771
        assert sup.ensure_calls == 0  # state never triggers ensure/spawn
    finally:
        client.__exit__(None, None, None)


def test_api_state_reports_remote_mode_for_second_pc(monkeypatch):
    from opencohost.api.ptt_session import PttController

    sup = _FakeSupervisor()
    app = _app_with_supervisor(sup)
    client = TestClient(app)
    client.__enter__()
    try:
        app.state.ptt_controller = PttController(
            "ws://192.168.1.50:8765",
            app.state.dispatcher,
            app.state.host.event_log,
        )
        body = client.get("/api/ptt/state").json()
        assert body["stt_service"]["mode"] == "remote"
        assert sup.ensure_calls == 0
    finally:
        client.__exit__(None, None, None)


# ──────────────────────────────────────────────────────────────────────────
# (1) Installer-metadata discovery (official launcher contract, read-only)
# ──────────────────────────────────────────────────────────────────────────


def _make_installed_root(root, platform):
    """Create a fake official install layout under *root*; return exe path."""
    import os

    if platform == "win32":
        exe = os.path.join(root, "app", ".venv", "Scripts", "liveaudio-service.exe")
    else:
        exe = os.path.join(root, "app", ".venv", "bin", "liveaudio-service")
    os.makedirs(os.path.dirname(exe), exist_ok=True)
    with open(exe, "w", encoding="utf-8") as fh:
        fh.write("fake")
    return exe


def _bare_env():
    return {"PATH": os.environ.get("PATH", ""), "SystemRoot": os.environ.get("SystemRoot", "")}


def test_metadata_env_override_resolves_sibling_exe(tmp_path, monkeypatch):
    import shutil

    exe = _make_installed_root(str(tmp_path / "root"), "win32")
    monkeypatch.setattr(shutil, "which", lambda name: None)
    env = _bare_env()
    env["LIVEAUDIO_INSTALL_ROOT"] = str(tmp_path / "root")
    assert resolve_installed_service_exe(environ=env, platform="win32") == exe
    assert resolve_liveaudio_command(environ=env, platform="win32") == [exe]


def test_metadata_location_file_resolves_recorded_root(tmp_path, monkeypatch):
    import json
    import shutil

    exe = _make_installed_root(str(tmp_path / "root"), "win32")
    appdata = tmp_path / "appdata"
    (appdata / "LiveAudio").mkdir(parents=True)
    (appdata / "LiveAudio" / "install_location.json").write_text(
        json.dumps({"install_root": str(tmp_path / "root")}), encoding="utf-8")
    monkeypatch.setattr(shutil, "which", lambda name: None)
    env = _bare_env()
    env["APPDATA"] = str(appdata)
    assert resolve_installed_service_exe(environ=env, platform="win32") == exe


def test_metadata_location_file_posix_xdg(tmp_path, monkeypatch):
    import json
    import shutil

    exe = _make_installed_root(str(tmp_path / "root"), "linux")
    xdg = tmp_path / "xdg"
    (xdg / "liveaudio").mkdir(parents=True)
    (xdg / "liveaudio" / "install_location.json").write_text(
        json.dumps({"install_root": str(tmp_path / "root")}), encoding="utf-8")
    monkeypatch.setattr(shutil, "which", lambda name: None)
    env = _bare_env()
    env["XDG_CONFIG_HOME"] = str(xdg)
    assert resolve_installed_service_exe(environ=env, platform="linux") == exe


def test_metadata_corrupt_file_degrades_to_none(tmp_path, monkeypatch):
    import shutil

    appdata = tmp_path / "appdata"
    (appdata / "LiveAudio").mkdir(parents=True)
    (appdata / "LiveAudio" / "install_location.json").write_text("{ broken", encoding="utf-8")
    monkeypatch.setattr(shutil, "which", lambda name: None)
    env = _bare_env()
    env["APPDATA"] = str(appdata)
    env["LOCALAPPDATA"] = str(tmp_path / "nolocal")
    assert resolve_installed_service_exe(environ=env, platform="win32") is None


def test_metadata_default_root_requires_installed_marker(tmp_path, monkeypatch):
    import json
    import shutil

    local = tmp_path / "local"
    exe = _make_installed_root(str(local / "LiveAudio"), "win32")
    monkeypatch.setattr(shutil, "which", lambda name: None)
    env = _bare_env()
    env["LOCALAPPDATA"] = str(local)
    # Exe present but no installed.json marker → not a completed install.
    assert resolve_installed_service_exe(environ=env, platform="win32") is None
    (local / "LiveAudio" / "installed.json").write_text(json.dumps({"v": 1}), encoding="utf-8")
    assert resolve_installed_service_exe(environ=env, platform="win32") == exe


def test_resolution_order_path_beats_metadata(tmp_path, monkeypatch):
    import shutil

    _make_installed_root(str(tmp_path / "root"), "win32")
    monkeypatch.setattr(shutil, "which", lambda name: "liveaudio-service")
    env = _bare_env()
    env["LIVEAUDIO_INSTALL_ROOT"] = str(tmp_path / "root")
    assert resolve_liveaudio_command(environ=env, platform="win32") == ["liveaudio-service"]


# ──────────────────────────────────────────────────────────────────────────
# (2) Scan clamps to 65535
# ──────────────────────────────────────────────────────────────────────────


def test_scan_clamps_range_at_65535():
    seen = []
    port = find_liveaudio_port(65530, handshake=lambda uri, t: seen.append(uri) or None)
    assert port is None
    assert seen == [f"ws://127.0.0.1:{p}" for p in range(65530, 65536)]


@pytest.mark.parametrize("base", [0, -1, 65536, 99999, "x", None])
def test_scan_rejects_out_of_range_base_without_dialing(base):
    calls = []
    assert find_liveaudio_port(base, handshake=lambda uri, t: calls.append(uri)) is None
    assert calls == []


# ──────────────────────────────────────────────────────────────────────────
# (3) wss:// loopback is configured/manual — never auto-spawned
# ──────────────────────────────────────────────────────────────────────────


def test_wss_loopback_is_not_spawn_eligible_but_is_loopback_host():
    assert is_loopback_uri("wss://127.0.0.1:8765") is False
    assert is_loopback_host("wss://127.0.0.1:8765") is True
    assert is_loopback_uri("ws://127.0.0.1:8765") is True


def test_wss_loopback_ensure_never_spawns_nor_probes():
    sup = _FakeSupervisor()
    probes = []
    outcome = ensure_local_service(
        configured_uri="wss://127.0.0.1:8765",
        controller=_FakeController(),
        supervisor=sup,
        probe=lambda uri: probes.append(uri) or (True, "connected"),
    )
    assert sup.ensure_calls == 0
    assert probes == []
    assert outcome.available is False and outcome.uri == "wss://127.0.0.1:8765"


def test_api_state_reports_local_mode_for_wss_loopback():
    from opencohost.api.ptt_session import PttController

    sup = _FakeSupervisor()
    app = _app_with_supervisor(sup)
    client = TestClient(app)
    client.__enter__()
    try:
        app.state.ptt_controller = PttController(
            "wss://127.0.0.1:8765",
            app.state.dispatcher,
            app.state.host.event_log,
        )
        body = client.get("/api/ptt/state").json()
        assert body["stt_service"]["mode"] == "local"
        assert sup.ensure_calls == 0
    finally:
        client.__exit__(None, None, None)


# ──────────────────────────────────────────────────────────────────────────
# (4) Persistent module-level single-flight lock
# ──────────────────────────────────────────────────────────────────────────


def test_concurrent_ensure_with_lockless_supervisor_spawns_once():
    import time

    class _LocklessRacy:
        """No ensure_lock attr (legacy double) with a widened spawn race."""

        def __init__(self):
            self.spawns = 0
            self.started = False

        def ensure_started(self):
            if not self.started:
                time.sleep(0.05)
                self.spawns += 1
                self.started = True
            return True

        def wait_for_port(self, timeout=None):
            return 8771

        def is_loading(self):
            return False

    sup = _LocklessRacy()

    def _probe(uri):
        return (True, "connected") if uri.endswith(":8771") else (False, "unreachable")

    outcomes = []
    threads = [
        threading.Thread(
            target=lambda: outcomes.append(
                ensure_local_service(
                    configured_uri="ws://127.0.0.1:8765",
                    controller=_FakeController(),
                    supervisor=sup,
                    probe=_probe,
                    scan=lambda base: (_ for _ in ()).throw(AssertionError("no scan needed")),
                )
            )
        )
        for _ in range(6)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)
    assert len(outcomes) == 6
    assert all(o.available for o in outcomes)
    assert sup.spawns == 1  # module-level lock serialized the whole cycle


# ──────────────────────────────────────────────────────────────────────────
# (5) HTTP worst case: loading answers stt_loading with no second long wait
# ──────────────────────────────────────────────────────────────────────────


def test_http_time_budgets_are_bounded_and_documented():
    assert DEFAULT_PORT_TIMEOUT == 8.0
    assert DEFAULT_READY_TIMEOUT == 5.0


def test_wait_for_port_gives_up_on_its_bound():
    import time

    sup = _supervisor_with_lines(port_timeout=0.05)
    assert sup.ensure_started() is True
    started = time.monotonic()
    assert sup.wait_for_port() is None
    assert time.monotonic() - started < 5.0
    sup.shutdown()


class _FlippingSupervisor(_FakeSupervisor):
    """Not loading at the pre-gate, loading once ensure runs (state flip)."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.ready_waits = 0

    def ensure_started(self):
        self.ensure_calls += 1
        self._loading = True  # model load observed only after spawn/scan
        return True

    def wait_for_ready(self, timeout=None):
        self.ready_waits += 1
        return False


def test_api_start_loading_after_ensure_returns_fast_without_retry(monkeypatch):
    from opencohost.api.ptt_session import PttController

    # :8765 refused (start fails once), :8771 answers (ensure discovers it)
    # while the supervisor flips to loading mid-cycle.
    _patch_connect_for_ports(monkeypatch, live_ports={8771})
    sup = _FlippingSupervisor(started=True, port=8771, loading=False)
    app = _app_with_supervisor(sup)
    client = TestClient(app)
    client.__enter__()
    try:
        starts = []
        controller = PttController(
            "ws://127.0.0.1:8765",
            app.state.dispatcher,
            app.state.host.event_log,
            ws_open_timeout=0.2,
        )
        real_start = controller.start

        def _counting_start():
            starts.append(1)
            return real_start()

        controller.start = _counting_start
        app.state.ptt_controller = controller
        resp = client.post("/api/ptt/start", json={})
        assert resp.status_code == 503
        assert resp.json() == {"detail": "stt_loading"}
        assert len(starts) == 1  # failed attempt only — no second long attempt
        assert sup.ready_waits == 0  # known-not-ready: no second bounded wait
        assert controller.state()["stt_ws_url"] == "ws://127.0.0.1:8771"
    finally:
        client.__exit__(None, None, None)


# ──────────────────────────────────────────────────────────────────────────
# (6) Sanitized diagnostics: last_warning + returncode in status
# ──────────────────────────────────────────────────────────────────────────


def test_warning_code_and_returncode_surface_in_status():
    import time

    FakePopen.exit_code = 1
    sup = _supervisor_with_lines(
        _event_line(type="warning", code="child-restart-scheduled"),
        _event_line(type="ws_port", port=8771, base=8765),
    )
    assert sup.ensure_started() is True
    assert sup.wait_for_port(timeout=5.0) == 8771
    deadline = time.monotonic() + 5.0
    while sup.status()["returncode"] is None and time.monotonic() < deadline:
        time.sleep(0.02)
    status = sup.status()
    assert status["last_warning"] == "child-restart-scheduled"
    assert status["returncode"] == 1
    assert status["fatal"] is None
    sup.shutdown()


def test_api_state_carries_diagnostic_fields():
    from opencohost.api.ptt_session import PttController

    sup = _FakeSupervisor(started=True, port=8771)
    app = _app_with_supervisor(sup)
    client = TestClient(app)
    client.__enter__()
    try:
        app.state.ptt_controller = PttController(
            "ws://127.0.0.1:8765",
            app.state.dispatcher,
            app.state.host.event_log,
        )
        svc = client.get("/api/ptt/state").json()["stt_service"]
        assert svc["schema"] == "opencohost.stt.status"
        assert svc["version"] == 1
        assert svc["mode"] == "auto"
        assert svc["last_warning"] is None and svc["returncode"] is None
    finally:
        client.__exit__(None, None, None)
