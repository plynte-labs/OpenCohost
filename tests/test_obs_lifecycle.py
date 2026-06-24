"""Characterization tests for opencohost.ui.obs_lifecycle (Stage 1 extraction).

Written BEFORE the extraction (RED phase) per strict-TDD.
Target module: opencohost/ui/obs_lifecycle.py

Covers §5 Stage-1 TDD plan from decomposition_design.md:
  - start_from_config disabled  → stops runtime, returns False
  - start_from_config enabled   → spawns retry thread, sets cancel event
  - connect_obs_loop cancel     → breaks loop cleanly
  - widget mutation marshals via injected schedule_ui_update (NOT bare after)
  - joyita arm/clear timer
"""

from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch, call


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_obs_cfg(enabled: bool = True):
    """Return a minimal avatar_cfg-style namespace (obs sub-namespace)."""
    return SimpleNamespace(
        obs=SimpleNamespace(
            enabled=enabled,
            host="localhost",
            port=4455,
            password="",
            source_name="KiraAvatar",
            scene_name="",
        ),
        assets_folder=Path("assets/avatar"),
        state_images={},
    )


def _make_injected(
    *,
    obs_client_value=None,
    retry_thread_value=None,
    retry_cancel_value=None,
    joyita_timer_value=None,
    avatar_panel=None,
    avatar_bridge=None,
    winfo_exists_return=True,
):
    """Return a minimal dict of injected callables for obs_lifecycle functions."""
    _store = {
        "obs_client": obs_client_value,
        "retry_thread": retry_thread_value,
        "retry_cancel": retry_cancel_value,
        "joyita_timer": joyita_timer_value,
    }
    return {
        "print_log": MagicMock(),
        "schedule_ui_update": MagicMock(),
        "avatar_panel": avatar_panel or MagicMock(),
        "avatar_bridge": avatar_bridge or MagicMock(),
        "get_obs_client": lambda: _store["obs_client"],
        "set_obs_client": lambda v: _store.update({"obs_client": v}),
        "get_retry_thread": lambda: _store["retry_thread"],
        "set_retry_thread": lambda v: _store.update({"retry_thread": v}),
        "get_retry_cancel": lambda: _store["retry_cancel"],
        "set_retry_cancel": lambda v: _store.update({"retry_cancel": v}),
        "get_joyita_timer_id": lambda: _store["joyita_timer"],
        "set_joyita_timer_id": lambda v: _store.update({"joyita_timer": v}),
        "after": MagicMock(return_value="timer-id"),
        "after_cancel": MagicMock(),
        "winfo_exists": MagicMock(return_value=winfo_exists_return),
        "_store": _store,  # test-only access for assertions
    }


# ---------------------------------------------------------------------------
# Import guard: module must exist before tests pass (RED if not yet created)
# ---------------------------------------------------------------------------


def test_obs_lifecycle_module_importable():
    """The module must exist and be importable (fails RED if not yet extracted)."""
    import opencohost.ui.obs_lifecycle  # noqa: F401


# ---------------------------------------------------------------------------
# start_from_config: disabled path
# ---------------------------------------------------------------------------


class _RecordingThread:
    """Captures Thread(...).start() calls without spawning a real thread."""
    instances: list = []

    def __init__(self, target, args=(), kwargs=None, daemon=False):
        self.target = target
        self.args = args
        self.kwargs = kwargs or {}
        self.daemon = daemon
        self.started = False
        _RecordingThread.instances.append(self)

    def start(self):
        self.started = True

    def is_alive(self):
        return self.started


def test_start_from_config_disabled_calls_stop_runtime_and_returns_false():
    """When obs.enabled is False, start_from_config must stop any running client
    and return False.  It must NOT spawn a retry thread."""
    from opencohost.ui.obs_lifecycle import start_from_config

    injected = _make_injected()
    # Put a live-looking client in state so stop_runtime has something to kill
    fake_client = MagicMock()
    injected["_store"]["obs_client"] = fake_client

    _RecordingThread.instances = []
    with patch("opencohost.avatar.avatar_config.load_avatar_config", return_value=_make_obs_cfg(enabled=False)):
        with patch("opencohost.ui.obs_lifecycle.threading.Thread", _RecordingThread):
            result = start_from_config(
                print_log=injected["print_log"],
                schedule_ui_update=injected["schedule_ui_update"],
                avatar_panel=injected["avatar_panel"],
                avatar_bridge=injected["avatar_bridge"],
                get_obs_client=injected["get_obs_client"],
                set_obs_client=injected["set_obs_client"],
                get_retry_thread=injected["get_retry_thread"],
                set_retry_thread=injected["set_retry_thread"],
                get_retry_cancel=injected["get_retry_cancel"],
                set_retry_cancel=injected["set_retry_cancel"],
                winfo_exists=injected["winfo_exists"],
            )

    assert result is False, "start_from_config must return False when OBS is disabled"
    assert len(_RecordingThread.instances) == 0, "must NOT spawn a retry thread when disabled"
    # Client must be nulled out (stop_runtime ran)
    assert injected["_store"]["obs_client"] is None, "obs_client must be cleared after stop_runtime"


def test_start_from_config_disabled_notifies_avatar_panel_with_none():
    """When disabled, avatar_panel.set_obs_client(None) must be called."""
    from opencohost.ui.obs_lifecycle import start_from_config

    injected = _make_injected()
    panel = MagicMock()
    injected["avatar_panel"] = panel

    with patch("opencohost.avatar.avatar_config.load_avatar_config", return_value=_make_obs_cfg(enabled=False)):
        start_from_config(
            print_log=injected["print_log"],
            schedule_ui_update=injected["schedule_ui_update"],
            avatar_panel=panel,
            avatar_bridge=injected["avatar_bridge"],
            get_obs_client=injected["get_obs_client"],
            set_obs_client=injected["set_obs_client"],
            get_retry_thread=injected["get_retry_thread"],
            set_retry_thread=injected["set_retry_thread"],
            get_retry_cancel=injected["get_retry_cancel"],
            set_retry_cancel=injected["set_retry_cancel"],
            winfo_exists=injected["winfo_exists"],
        )

    panel.set_obs_client.assert_called_once_with(None)


# ---------------------------------------------------------------------------
# start_from_config: enabled path
# ---------------------------------------------------------------------------


def test_start_from_config_enabled_spawns_retry_thread():
    """When enabled and no live client, start_from_config must create an OBSClient,
    set a cancel Event, and start exactly ONE daemon retry thread."""
    from opencohost.ui.obs_lifecycle import start_from_config

    injected = _make_injected()
    fake_client = MagicMock()

    _RecordingThread.instances = []
    with patch("opencohost.avatar.avatar_config.load_avatar_config", return_value=_make_obs_cfg(enabled=True)):
        with patch("opencohost.ui.obs_lifecycle.OBSClient", return_value=fake_client):
            with patch("opencohost.ui.obs_lifecycle.threading.Thread", _RecordingThread):
                result = start_from_config(
                    print_log=injected["print_log"],
                    schedule_ui_update=injected["schedule_ui_update"],
                    avatar_panel=injected["avatar_panel"],
                    avatar_bridge=injected["avatar_bridge"],
                    get_obs_client=injected["get_obs_client"],
                    set_obs_client=injected["set_obs_client"],
                    get_retry_thread=injected["get_retry_thread"],
                    set_retry_thread=injected["set_retry_thread"],
                    get_retry_cancel=injected["get_retry_cancel"],
                    set_retry_cancel=injected["set_retry_cancel"],
                    winfo_exists=injected["winfo_exists"],
                )

    assert result is True, "start_from_config must return True when enabled and client was created"
    assert len(_RecordingThread.instances) == 1, "must spawn exactly one retry thread"
    assert _RecordingThread.instances[0].started is True, "retry thread must be started"
    assert _RecordingThread.instances[0].daemon is True, "retry thread must be a daemon"
    assert injected["_store"]["obs_client"] is fake_client, "obs_client must be set on state"
    assert injected["_store"]["retry_cancel"] is not None, "cancel event must be set"


def test_start_from_config_enabled_second_call_does_not_spawn_second_thread():
    """If a live thread already exists (is_alive → True), a second call must not
    spawn another thread (idempotent guard)."""
    from opencohost.ui.obs_lifecycle import start_from_config

    alive_thread = MagicMock()
    alive_thread.is_alive.return_value = True
    fake_client = MagicMock()

    injected = _make_injected(
        obs_client_value=fake_client,
        retry_thread_value=alive_thread,
    )

    _RecordingThread.instances = []
    with patch("opencohost.avatar.avatar_config.load_avatar_config", return_value=_make_obs_cfg(enabled=True)):
        with patch("opencohost.ui.obs_lifecycle.threading.Thread", _RecordingThread):
            result = start_from_config(
                print_log=injected["print_log"],
                schedule_ui_update=injected["schedule_ui_update"],
                avatar_panel=injected["avatar_panel"],
                avatar_bridge=injected["avatar_bridge"],
                get_obs_client=injected["get_obs_client"],
                set_obs_client=injected["set_obs_client"],
                get_retry_thread=injected["get_retry_thread"],
                set_retry_thread=injected["set_retry_thread"],
                get_retry_cancel=injected["get_retry_cancel"],
                set_retry_cancel=injected["set_retry_cancel"],
                winfo_exists=injected["winfo_exists"],
            )

    assert result is True
    assert len(_RecordingThread.instances) == 0, "must NOT spawn a second thread when one is already alive"


# ---------------------------------------------------------------------------
# connect_obs_loop: cancellation
# ---------------------------------------------------------------------------


def test_connect_obs_loop_cancellation_breaks_immediately():
    """When cancel_event is pre-set, connect_obs_loop must exit immediately without
    calling connect() on the client."""
    from opencohost.ui.obs_lifecycle import connect_obs_loop

    injected = _make_injected()
    fake_client = MagicMock()
    injected["_store"]["obs_client"] = fake_client

    cancel_event = threading.Event()
    cancel_event.set()  # pre-cancelled

    connect_obs_loop(
        cancel_event=cancel_event,
        obs_client=fake_client,
        get_obs_client=injected["get_obs_client"],
        print_log=injected["print_log"],
        schedule_ui_update=injected["schedule_ui_update"],
        avatar_panel=injected["avatar_panel"],
        avatar_bridge=injected["avatar_bridge"],
        winfo_exists=injected["winfo_exists"],
        retry_delay=0.0,
    )

    fake_client.connect.assert_not_called()


def test_connect_obs_loop_cancel_event_set_during_retry_breaks_loop():
    """cancel_event.wait() returning True (event set) must break the retry loop."""
    from opencohost.ui.obs_lifecycle import connect_obs_loop

    injected = _make_injected()
    fake_client = MagicMock()
    fake_client.connect.return_value = False  # always fails to connect
    injected["_store"]["obs_client"] = fake_client

    cancel_event = threading.Event()
    # Simulate: first wait call sets the event (loop should break)
    cancel_event.wait = MagicMock(return_value=True)

    connect_obs_loop(
        cancel_event=cancel_event,
        obs_client=fake_client,
        get_obs_client=injected["get_obs_client"],
        print_log=injected["print_log"],
        schedule_ui_update=injected["schedule_ui_update"],
        avatar_panel=injected["avatar_panel"],
        avatar_bridge=injected["avatar_bridge"],
        winfo_exists=injected["winfo_exists"],
        retry_delay=0.0,
    )

    # connect was called once (before the cancel was checked on wait)
    assert fake_client.connect.call_count == 1


# ---------------------------------------------------------------------------
# connect_obs_loop: widget mutation must go via schedule_ui_update
# ---------------------------------------------------------------------------


def test_connect_obs_loop_widget_mutation_via_schedule_ui_update():
    """On successful connect, set_obs_client must be scheduled via
    schedule_ui_update (== _safe_after), NEVER via a bare after call."""
    from opencohost.ui.obs_lifecycle import connect_obs_loop

    schedule_ui_update = MagicMock()
    injected = _make_injected(winfo_exists_return=True)
    injected["schedule_ui_update"] = schedule_ui_update

    fake_client = MagicMock()
    fake_client.connect.return_value = True  # connects on first try
    injected["_store"]["obs_client"] = fake_client
    bridge = MagicMock()
    bridge.get_state.return_value = "idle"

    cancel_event = threading.Event()  # not set

    connect_obs_loop(
        cancel_event=cancel_event,
        obs_client=fake_client,
        get_obs_client=injected["get_obs_client"],
        print_log=injected["print_log"],
        schedule_ui_update=schedule_ui_update,
        avatar_panel=injected["avatar_panel"],
        avatar_bridge=bridge,
        winfo_exists=injected["winfo_exists"],
        retry_delay=0.0,
    )

    # The widget mutation must be DEFERRED, not run inline on the worker thread:
    # set_obs_client must NOT have been called yet, only scheduled.
    injected["avatar_panel"].set_obs_client.assert_not_called()
    assert schedule_ui_update.called, (
        "set_obs_client must be scheduled via schedule_ui_update, not called inline or via bare after"
    )
    # Invoke the captured callable and prove it performs the real widget mutation
    # with the connected client — i.e. schedule_ui_update wraps the true work.
    scheduled = schedule_ui_update.call_args[0][0]
    scheduled()
    injected["avatar_panel"].set_obs_client.assert_called_once_with(fake_client)


def test_connect_obs_loop_no_bare_after_call():
    """connect_obs_loop must never accept a bare `after` callable.

    The strongest structural guarantee that widget mutation goes ONLY through
    schedule_ui_update is that the function signature has no `after` parameter
    at all — it physically cannot perform a bare-after widget mutation.
    """
    import inspect

    from opencohost.ui.obs_lifecycle import connect_obs_loop

    sig = inspect.signature(connect_obs_loop)
    assert "after" not in sig.parameters, (
        "connect_obs_loop must NOT accept a bare `after` parameter — "
        "widget mutation must go through schedule_ui_update only"
    )


# ---------------------------------------------------------------------------
# Joyita arm/clear timer
# ---------------------------------------------------------------------------


def test_on_joyita_to_obs_arms_timer_via_after():
    """_on_joyita_to_obs must arm a 120 000 ms timer via the injected after callable
    when the OBS client is connected and set_obs_text returns True."""
    from opencohost.ui.obs_lifecycle import on_joyita_to_obs

    obs = MagicMock()
    obs.is_connected = True
    obs.set_obs_text.return_value = True

    after = MagicMock(return_value="joyita-timer-id")
    after_cancel = MagicMock()
    _store = {"joyita_timer": None}

    on_joyita_to_obs(
        text="¡Hola!",
        get_obs_client=lambda: obs,
        get_joyita_timer_id=lambda: _store["joyita_timer"],
        set_joyita_timer_id=lambda v: _store.update({"joyita_timer": v}),
        after=after,
        after_cancel=after_cancel,
        clear_obs_joyita=MagicMock(),
    )

    after.assert_called_once()
    call_args = after.call_args
    assert call_args[0][0] == 120_000, "timer delay must be 120 000 ms"
    assert _store["joyita_timer"] == "joyita-timer-id", "timer id must be stored"


def test_on_joyita_to_obs_cancels_existing_timer_before_arming_new():
    """If a timer is already armed, it must be cancelled before a new one is set."""
    from opencohost.ui.obs_lifecycle import on_joyita_to_obs

    obs = MagicMock()
    obs.is_connected = True
    obs.set_obs_text.return_value = True

    after = MagicMock(return_value="new-timer-id")
    after_cancel = MagicMock()
    _store = {"joyita_timer": "old-timer-id"}

    on_joyita_to_obs(
        text="Texto nuevo",
        get_obs_client=lambda: obs,
        get_joyita_timer_id=lambda: _store["joyita_timer"],
        set_joyita_timer_id=lambda v: _store.update({"joyita_timer": v}),
        after=after,
        after_cancel=after_cancel,
        clear_obs_joyita=MagicMock(),
    )

    after_cancel.assert_called_once_with("old-timer-id")


def test_on_joyita_to_obs_noop_when_no_obs_client():
    """If no OBS client is present, on_joyita_to_obs must return without arming a timer."""
    from opencohost.ui.obs_lifecycle import on_joyita_to_obs

    after = MagicMock()
    _store = {"joyita_timer": None}

    on_joyita_to_obs(
        text="¡Hola!",
        get_obs_client=lambda: None,
        get_joyita_timer_id=lambda: _store["joyita_timer"],
        set_joyita_timer_id=lambda v: _store.update({"joyita_timer": v}),
        after=after,
        after_cancel=MagicMock(),
        clear_obs_joyita=MagicMock(),
    )

    after.assert_not_called()


def test_on_joyita_to_obs_noop_when_empty_text():
    """If text is empty, on_joyita_to_obs must return immediately."""
    from opencohost.ui.obs_lifecycle import on_joyita_to_obs

    after = MagicMock()

    on_joyita_to_obs(
        text="",
        get_obs_client=lambda: MagicMock(is_connected=True),
        get_joyita_timer_id=lambda: None,
        set_joyita_timer_id=MagicMock(),
        after=after,
        after_cancel=MagicMock(),
        clear_obs_joyita=MagicMock(),
    )

    after.assert_not_called()


def test_clear_obs_joyita_resets_timer_id_and_blanks_text():
    """clear_obs_joyita must set timer id to None and blank the OBS text source."""
    from opencohost.ui.obs_lifecycle import clear_obs_joyita

    obs = MagicMock()
    obs.is_connected = True

    _store = {"joyita_timer": "some-id"}

    clear_obs_joyita(
        source_name="KiraJoyita",
        get_obs_client=lambda: obs,
        set_joyita_timer_id=lambda v: _store.update({"joyita_timer": v}),
    )

    assert _store["joyita_timer"] is None, "joyita_timer_id must be cleared to None"
    obs.set_obs_text.assert_called_once_with("KiraJoyita", "")


def test_clear_obs_joyita_noop_when_no_obs_client():
    """clear_obs_joyita must not raise when obs_client is None."""
    from opencohost.ui.obs_lifecycle import clear_obs_joyita

    _store = {"joyita_timer": "some-id"}

    clear_obs_joyita(
        source_name="KiraJoyita",
        get_obs_client=lambda: None,
        set_joyita_timer_id=lambda v: _store.update({"joyita_timer": v}),
    )

    assert _store["joyita_timer"] is None


# ---------------------------------------------------------------------------
# Delegate contract: shell methods must call the module functions
# ---------------------------------------------------------------------------


def test_shell_delegates_preserve_method_names():
    """VocalAIApp must still expose all OBS method names (delegate pattern)."""
    import importlib
    import sys
    from unittest.mock import patch as _patch

    # Minimal mock set to load app_shell without Tk
    class _Dummy:
        pass

    class _DummyCtk(SimpleNamespace):
        def __getattr__(self, _):
            return _Dummy

    mods = {
        "customtkinter": _DummyCtk(CTk=_Dummy, CTkToplevel=_Dummy),
        "numpy": MagicMock(),
        "sounddevice": MagicMock(),
        "soundfile": MagicMock(),
        "pynput": MagicMock(),
        "pynput.keyboard": MagicMock(),
        "pynput.mouse": MagicMock(),
    }
    old = sys.modules.pop("opencohost.ui.app_shell", None)
    with _patch.dict(sys.modules, mods):
        shell_mod = importlib.import_module("opencohost.ui.app_shell")

    expected = [
        "_on_joyita_to_obs",
        "_clear_obs_joyita",
        "_init_obs_client",
        "_obs_connect_now",
        "_obs_start_from_config",
        "_obs_stop_runtime",
        "_connect_obs_loop",
    ]
    for name in expected:
        assert hasattr(shell_mod.VocalAIApp, name), (
            f"VocalAIApp must still have delegate method {name!r} after extraction"
        )

    # Restore
    if old is not None:
        sys.modules["opencohost.ui.app_shell"] = old
    else:
        sys.modules.pop("opencohost.ui.app_shell", None)
