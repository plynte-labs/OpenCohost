"""Tests for opencohost.avatar.obs_runtime.ObsRuntime (FIX-B).

ObsRuntime is the UI-free OBS push chain for the headless API engine host.
It mirrors the CTK chain (opencohost/ui/obs_lifecycle.py +
opencohost/ui/motor_event_handlers.py) WITHOUT importing opencohost.ui — the
engine host must never pull in the desktop UI (hard contract, see
opencohost/api/main.py module docstring).

Isolation: AVATAR_CONFIG_FILE is patched to a tmp avatar.yaml (fixture pattern
mirrors tests/test_api_obs.py:32-38) and a fake OBSClient is injected so no
real OBS websocket is ever opened. The state-drain worker is flushed
deterministically via ObsRuntime.flush().
"""

from __future__ import annotations

import threading
from pathlib import Path
from queue import Queue
from unittest.mock import MagicMock

import pytest

from opencohost.avatar import avatar_config as ac
from opencohost.avatar.avatar_state import AvatarState


# ──────────────────────────────────────────────────────────────────────────
# Fakes / helpers
# ──────────────────────────────────────────────────────────────────────────


class _FakeOBSClient:
    """Fake OBSClient — never opens a real OBS websocket.

    Faithful to opencohost.avatar.obs_client.OBSClient: connect() flips
    is_connected, subscribe_bridge registers on_state_change on the bridge,
    and on_state_change only records while connected.
    """

    def __init__(self, config, assets_folder, state_images=None, on_log=None, **kwargs):
        self.config = config
        self.assets_folder = assets_folder
        self.state_images = state_images or {}
        self._connected = False
        self.subscribed_bridge = None
        self.received_states: list = []
        self.disconnected = False
        self.connect_calls = 0
        self.fail_until = 0  # number of leading connect() attempts that fail
        self._sub_id = None

    def connect(self, log_failures=True):
        self.connect_calls += 1
        if self.connect_calls <= self.fail_until:
            return False
        self._connected = True
        return True

    @property
    def is_connected(self):
        return self._connected

    def subscribe_bridge(self, bridge):
        self.subscribed_bridge = bridge
        self._sub_id = bridge.subscribe(self.on_state_change)

    def on_state_change(self, state):
        if not self._connected:
            return
        self.received_states.append(state)

    def disconnect(self):
        self.disconnected = True
        self._connected = False


def _recording_factory():
    """Return (cls, created_list) — cls records every constructed instance."""
    created: list = []

    class _C(_FakeOBSClient):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            created.append(self)

    return _C, created


def _connect_recording_factory():
    """Return (cls, created_list) — like _recording_factory, but every instance
    also flags ``ever_connected`` the first time connect() succeeds, so a test
    can assert every client that ever opened a socket was either the final
    active one or was disconnected (FIX-B / F2 orphan check)."""
    created: list = []

    class _C(_FakeOBSClient):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.ever_connected = False
            created.append(self)

        def connect(self, log_failures=True):
            ok = super().connect(log_failures=log_failures)
            if ok:
                self.ever_connected = True
            return ok

    return _C, created


@pytest.fixture
def avatar_yaml(tmp_path, monkeypatch):
    """Patch AVATAR_CONFIG_FILE to a tmp yaml; no real user config is touched."""
    path = tmp_path / "avatar.yaml"
    monkeypatch.setattr(ac, "AVATAR_CONFIG_FILE", path)
    return path


def _write_cfg(path, *, enabled, host="localhost", port=4455, state_images=None):
    cfg = ac.AvatarConfig(
        enabled=True,
        mode="image_states",
        state_images=state_images or {},
        obs=ac.OBSConfig(enabled=enabled, host=host, port=port),
    )
    ac.save_avatar_config(cfg, config_file=path)


def _make_runtime(client_cls):
    from opencohost.avatar.obs_runtime import ObsRuntime

    # A small retry_delay keeps any (unexpected) failing loop cheap.
    return ObsRuntime(obs_client_cls=client_cls, retry_delay=0.05)


def _start_connected(avatar_yaml, *, host="localhost", port=4455, state_images=None):
    """Write an enabled config, start the runtime, and wait for the retry loop
    to connect + subscribe. Returns (runtime, client, created)."""
    _write_cfg(avatar_yaml, enabled=True, host=host, port=port, state_images=state_images)
    cls, created = _recording_factory()
    runtime = _make_runtime(cls)
    assert runtime.start_from_config() is True
    runtime._retry_thread.join(timeout=2.0)
    return runtime, created[0], created


# ──────────────────────────────────────────────────────────────────────────
# start_from_config
# ──────────────────────────────────────────────────────────────────────────


def test_disabled_config_builds_no_client(avatar_yaml):
    _write_cfg(avatar_yaml, enabled=False)
    cls, created = _recording_factory()
    runtime = _make_runtime(cls)

    assert runtime.start_from_config() is False
    assert created == []
    assert runtime._obs_client is None


def test_enabled_config_builds_client_and_retry_loop_subscribes(avatar_yaml):
    runtime, client, created = _start_connected(avatar_yaml, host="10.0.0.7", port=4460)
    try:
        assert len(created) == 1
        assert client.config.host == "10.0.0.7"
        assert client.config.port == 4460
        assert client.connect_calls >= 1
        # Retry loop subscribed the runtime's own bridge and pushed the
        # current (initial IDLE) state on first successful connect.
        assert client.subscribed_bridge is runtime.bridge
        assert AvatarState.IDLE in client.received_states
    finally:
        runtime.stop()


# ──────────────────────────────────────────────────────────────────────────
# handle_motor_event -> bridge -> OBSClient (mirrors motor_event_handlers)
# ──────────────────────────────────────────────────────────────────────────


def test_handle_speaking_start_pushes_speaking(avatar_yaml):
    runtime, client, _ = _start_connected(avatar_yaml)
    try:
        runtime.handle_motor_event("speaking_start")
        runtime.flush()
        assert AvatarState.SPEAKING in client.received_states
    finally:
        runtime.stop()


def test_handle_speaking_end_pushes_idle(avatar_yaml):
    runtime, client, _ = _start_connected(avatar_yaml)
    try:
        runtime.handle_motor_event("speaking_start")
        runtime.handle_motor_event("speaking_end")
        runtime.flush()
        assert client.received_states[-1] == AvatarState.IDLE
    finally:
        runtime.stop()


def test_handle_processing_pushes_thinking(avatar_yaml):
    runtime, client, _ = _start_connected(avatar_yaml)
    try:
        runtime.handle_motor_event("processing")
        runtime.flush()
        assert AvatarState.THINKING in client.received_states
    finally:
        runtime.stop()


def test_handle_ready_pushes_idle(avatar_yaml):
    runtime, client, _ = _start_connected(avatar_yaml)
    try:
        # Move off IDLE first so the transition is observable.
        runtime.handle_motor_event("processing")
        runtime.handle_motor_event("ready")
        runtime.flush()
        assert client.received_states[-1] == AvatarState.IDLE
    finally:
        runtime.stop()


def test_unknown_event_is_ignored(avatar_yaml):
    runtime, client, _ = _start_connected(avatar_yaml)
    try:
        before = list(client.received_states)
        runtime.handle_motor_event("model_warming")  # not mapped
        runtime.handle_motor_event(None)  # must not raise
        runtime.flush()
        assert client.received_states == before
    finally:
        runtime.stop()


def test_speaking_start_alternates_speaking_and_alt(avatar_yaml):
    """Parity with motor_event_handlers.start_speaking_alt_timer: speaking_start
    yields the visible mouth-flap (SPEAKING then SPEAKING_ALT)."""
    runtime, client, _ = _start_connected(avatar_yaml)
    try:
        runtime.handle_motor_event("speaking_start")
        runtime.flush()
        assert AvatarState.SPEAKING in client.received_states
        assert AvatarState.SPEAKING_ALT in client.received_states
    finally:
        runtime.stop()


# ──────────────────────────────────────────────────────────────────────────
# stop / apply_config
# ──────────────────────────────────────────────────────────────────────────


def test_stop_cancels_and_disconnects(avatar_yaml):
    runtime, client, _ = _start_connected(avatar_yaml)
    cancel_event = runtime._cancel_event
    assert cancel_event is not None

    runtime.stop()

    assert client.disconnected is True
    assert runtime._obs_client is None
    assert runtime._cancel_event is None
    assert cancel_event.is_set() is True


def test_apply_config_rebuilds_with_new_settings(avatar_yaml, tmp_path):
    runtime, first, created = _start_connected(avatar_yaml, host="first-host", port=4455)
    try:
        assert first.config.host == "first-host"

        # Rewrite the config with a new host/port AND a new state_images map,
        # then apply — a live OBSClient snapshots state_images at construction
        # (obs_client.py), so a card change requires a fresh client, not a reconnect.
        new_images = {"idle": tmp_path / "idle.png"}
        _write_cfg(avatar_yaml, enabled=True, host="second-host", port=4466, state_images=new_images)

        assert runtime.apply_config() is True
        runtime._retry_thread.join(timeout=2.0)

        assert len(created) == 2
        second = created[1]
        assert second is not first
        assert second.config.host == "second-host"
        assert second.config.port == 4466
        assert "idle" in second.state_images
        # Old client was disconnected as part of the rebuild.
        assert first.disconnected is True
    finally:
        runtime.stop()


# ──────────────────────────────────────────────────────────────────────────
# F1: speaking_alt Timer cancel race (in-flight tick must not re-arm the flap)
# ──────────────────────────────────────────────────────────────────────────


def test_stopped_flap_tick_does_not_rearm_or_enqueue(avatar_yaml):
    """F1: a SPEAKING/SPEAKING_ALT tick that already fired before
    _stop_speaking_alt cancelled its Timer must NOT re-arm a replacement Timer
    nor enqueue another SPEAKING once the flap has been stopped.

    Without the epoch guard the in-flight tick overwrites _speaking_alt_timer
    with a live Timer that _stop_speaking_alt never saw, orphaning a flap chain
    that keeps pushing SPEAKING states while Kira is silent. Here we drive the
    race deterministically: arm the flap, capture the epoch an in-flight tick
    carries, stop the flap (the engine-thread teardown path), then replay the
    stale tick by hand and assert it is inert.
    """
    runtime, client, _ = _start_connected(avatar_yaml)
    try:
        runtime._start_speaking_alt()
        captured_epoch = runtime._speaking_alt_epoch
        assert runtime._speaking_alt_timer is not None

        # Engine thread stops the flap (speaking_end / stop() / apply_config).
        runtime._stop_speaking_alt()
        assert runtime._speaking_alt_timer is None

        runtime.flush()
        states_after_stop = len(client.received_states)

        # A tick that fired JUST before the cancel now runs with its stale epoch.
        runtime._tick_speaking_alt(captured_epoch)

        # It armed no replacement Timer and enqueued no further state.
        assert runtime._speaking_alt_timer is None
        runtime.flush()
        assert len(client.received_states) == states_after_stop
    finally:
        runtime.stop()


# ──────────────────────────────────────────────────────────────────────────
# F2: concurrent apply_config must never orphan a connected client
# ──────────────────────────────────────────────────────────────────────────


def test_concurrent_apply_config_disconnects_every_losing_client(avatar_yaml):
    """F2: PUT /api/obs/config and PUT /api/avatar/config both call apply_config
    on FastAPI's threadpool. Without serialization two interleaved
    stop()+start_from_config() calls can each read _obs_client as None and
    orphan the loser's connected client (never disconnected). The _apply_lock
    serializes the whole stop()+start, and start_from_config defensively
    disconnects any pre-existing client, so every client that ever connected is
    either the final active one or was disconnected.
    """
    _write_cfg(avatar_yaml, enabled=True)
    cls, created = _connect_recording_factory()
    runtime = _make_runtime(cls)
    try:
        # Prime one live client + retry loop so both apply_config calls have a
        # predecessor that must be torn down.
        assert runtime.start_from_config() is True
        runtime._retry_thread.join(timeout=2.0)

        barrier = threading.Barrier(2)

        def _apply():
            barrier.wait()  # release both threads into apply_config together
            runtime.apply_config()

        threads = [threading.Thread(target=_apply, name=f"apply-{i}") for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)

        # Drain the surviving retry loop so the final client connects+subscribes.
        final_retry = runtime._retry_thread
        if final_retry is not None:
            final_retry.join(timeout=2.0)

        final = runtime._obs_client
        assert final is not None
        connected = [c for c in created if c.ever_connected]
        assert connected, "expected at least one client to open a socket"
        for c in connected:
            assert c is final or c.disconnected is True, (
                "a connected client was orphaned (neither active nor disconnected)"
            )
    finally:
        runtime.stop()


# ──────────────────────────────────────────────────────────────────────────
# EngineHost wiring (ui_callback router + obs_runtime lifecycle)
# ──────────────────────────────────────────────────────────────────────────


def _make_engine_host(tmp_path, monkeypatch, obs_runtime_cls):
    """Build an EngineHost with every heavy dependency stubbed. Returns
    (host, captured) where captured['ui_callback'] is the callback the host
    passed to MotorVocalIA."""
    import opencohost.api.engine_host as eh

    captured: dict = {}

    def _fake_motor(log_queue, ui_callback=None, dialogue_callback=None):
        captured["ui_callback"] = ui_callback
        m = MagicMock()
        m.command_queue = Queue()
        m.current_model = None
        return m

    monkeypatch.setattr(eh, "MotorVocalIA", _fake_motor)
    monkeypatch.setattr(eh, "HealthMonitor", lambda: MagicMock())
    monkeypatch.setattr(eh, "ObsRuntime", obs_runtime_cls)
    monkeypatch.setattr(eh, "Aggregator", MagicMock())
    monkeypatch.setattr(eh, "KiraAgendaController", MagicMock())
    monkeypatch.setattr(eh, "AgendaPersistence", MagicMock())
    monkeypatch.setattr(eh, "MusicLibrary", MagicMock())
    monkeypatch.setattr(eh, "ollama", MagicMock())

    host = eh.EngineHost(lock_path=str(tmp_path / "eh.lock"))
    host._wake_ollama_eager = lambda: None  # no daemon wake thread in tests
    return host, captured


def test_engine_host_forwards_motor_event_to_obs_runtime(tmp_path, monkeypatch):
    events: list = []

    class _FakeRuntime:
        def __init__(self, *a, **k):
            pass

        def start_from_config(self):
            return True

        def handle_motor_event(self, event):
            events.append(event)

        def stop(self):
            pass

    host, captured = _make_engine_host(tmp_path, monkeypatch, _FakeRuntime)
    host.start()
    try:
        assert callable(captured["ui_callback"])
        captured["ui_callback"]("speaking_start")
        assert events == ["speaking_start"]
    finally:
        host.stop()


def test_engine_host_stop_stops_obs_runtime(tmp_path, monkeypatch):
    stopped: list = []

    class _FakeRuntime:
        def __init__(self, *a, **k):
            pass

        def start_from_config(self):
            return True

        def handle_motor_event(self, event):
            pass

        def stop(self):
            stopped.append(True)

    host, _ = _make_engine_host(tmp_path, monkeypatch, _FakeRuntime)
    host.start()
    host.stop()
    assert stopped == [True]


def test_engine_host_obs_runtime_construction_failure_is_resilient(tmp_path, monkeypatch):
    class _BoomRuntime:
        def __init__(self, *a, **k):
            raise RuntimeError("obs boom")

    host, captured = _make_engine_host(tmp_path, monkeypatch, _BoomRuntime)
    host.start()  # must NOT raise despite ObsRuntime construction failure
    try:
        assert host.obs_runtime is None
        # The ui_callback router must still be safe to invoke with no runtime.
        captured["ui_callback"]("speaking_start")  # must not raise
    finally:
        host.stop()  # must not raise with obs_runtime None
