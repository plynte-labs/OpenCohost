"""VisionController: lazy build, daemon lifecycle, status, arm, runtime apply
(vision-on-demand V1.5). Fakes only: no Ollama, no screen."""
from __future__ import annotations

import logging
from dataclasses import replace
from types import SimpleNamespace

import pytest

from opencohost.config.vision_settings import VisionSettings
from opencohost.core.vision.capture import CapturedFrame
from opencohost.core.vision.controller import VisionController
from opencohost.core.vision.describer import VisionDescribeError
from opencohost.core.vision.gate import RuleVisionGate

OFF = VisionSettings(
    enabled=False, look_on_ptt=False, capture_delay_s=3.0, rule_gate_enabled=False,
    port=11435, model="qwen3-vl:4b-instruct", describe_timeout_s=8.0,
)
ON = replace(OFF, enabled=True, capture_delay_s=0.0)  # no real sleep in looks


class FakeDaemon:
    def __init__(self, port, *, state_after_start="running", installed=True, warm_ok=True,
                 last_error=None, on_warm=None, ops=None):
        self.port = port
        self.state = "stopped"
        self.state_after_start = state_after_start
        self.installed = installed
        self.warm_ok = warm_ok
        self.last_error = last_error
        self.on_warm = on_warm
        self.ensure_calls = 0
        self.stop_calls = 0
        self.model_queries = []
        self.warms = []
        self.unloads = []
        self.ps_calls = 0
        self.ps_script = []
        self.loaded = []
        self.ops = ops if ops is not None else []

    def ensure_running(self):
        self.ensure_calls += 1
        self.state = self.state_after_start
        return self.status()

    def status(self):
        return SimpleNamespace(state=self.state, owned=True, pid=1, port=self.port,
                               last_error=self.last_error)

    def stop(self):
        self.stop_calls += 1
        self.ops.append("stop")
        self.state = "stopped"

    def warm(self, model, *, keep_alive, timeout_s=None):
        self.warms.append((model, keep_alive))
        self.ops.append("warm")
        if self.on_warm is not None:
            self.on_warm()
        if self.warm_ok:
            self.loaded = [model]
        return self.warm_ok

    def unload(self, model):
        self.unloads.append(model)
        self.ops.append("unload")
        self.loaded = []
        return True

    def model_loaded(self, model):
        self.ps_calls += 1
        if self.ps_script:  # V1.11: scripted /api/ps answers are consumed first
            return self.ps_script.pop(0)
        if self.loaded is None:
            return None
        return model in self.loaded

    def model_installed(self, model):
        self.model_queries.append(model)
        return self.installed

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self.port}"


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class Harness:
    def __init__(self, *, capture_ok=True, daemon_kwargs=None, run_bg=True, describe_error=None):
        self.daemons = []
        self.describers = []
        self.pending = []
        self.looking_events = []
        self.bridges = []
        self.results = []
        self.describing = []
        self.clock = Clock()
        self.describe_error = describe_error
        self.capture_ok = capture_ok
        self.daemon_kwargs = daemon_kwargs or {}
        self.run_bg = run_bg

        def daemon_factory(values):
            d = FakeDaemon(values.port, **self.daemon_kwargs)
            self.daemons.append(d)
            return d

        def describe(jpeg, question, *, timeout_s, cancel=None):
            if self.describe_error is not None:
                raise self.describe_error
            return "Barra de vida al 30%."

        def describer_factory(values, base_url):
            desc = SimpleNamespace(model=values.model, base_url=base_url, describe=describe)
            self.describers.append(desc)
            return desc

        frame = CapturedFrame(jpeg=b"\xff\xd8x", width=1280, height=720, source_width=1920,
                              source_height=1080, is_black=False, grab_ms=1.0, encode_ms=1.0,
                              thumbnail=b"\xff\xd8thumb")
        self.sleeps = []
        self.foreground = SimpleNamespace(is_self_foreground=lambda: False)

        def capture_factory():
            return SimpleNamespace(capture=lambda: frame) if self.capture_ok else None

        def spawn(fn):
            if self.run_bg:
                fn()
            else:
                self.pending.append(fn)

        self.controller = VisionController(
            daemon_factory=daemon_factory,
            describer_factory=describer_factory,
            capture_factory=capture_factory,
            speak_bridge=self.bridges.append,
            on_looking=self.looking_events.append,
            on_result=self.results.append,
            spawn=spawn,
            clock=self.clock,
            wall_clock=lambda: 1700000000.0,
            ps_ttl_s=5.0,
            sleep=self.sleeps.append,  # V1.11: the burst never really sleeps in tests
            foreground_probe=self.foreground,
            on_describing=lambda: self.describing.append(True),
        )


def test_disabled_builds_nothing():
    h = Harness()
    h.controller.apply(OFF)

    assert h.controller.step is None
    assert h.daemons == [] and h.describers == []
    status = h.controller.status()
    assert status["enabled"] is False
    assert status["daemon_state"] == "stopped"
    assert status["armed"] is False
    assert status["looking"] is False


def test_enable_builds_step_and_starts_daemon_in_background():
    h = Harness(run_bg=False)
    h.controller.apply(ON)

    assert h.controller.step is not None
    assert len(h.daemons) == 1
    assert h.daemons[0].ensure_calls == 0  # not on the caller's thread
    assert len(h.pending) == 1
    h.pending.pop()()
    assert h.daemons[0].ensure_calls == 1
    assert h.daemons[0].model_queries == ["qwen3-vl:4b-instruct"]
    assert h.describers[0].base_url == "http://127.0.0.1:11435"


def test_status_after_start():
    h = Harness()
    h.controller.apply(ON)

    status = h.controller.status()
    assert status == {
        "enabled": True,
        "state": "ready",
        "last_error": None,
        "daemon_state": "running",
        "model_installed": True,
        "model_loaded": True,
        "capture_available": True,
        "armed": False,
        "looking": False,
        "model": "qwen3-vl:4b-instruct",
        "port": 11435,
        "last_result": None,
    }


def test_turn_while_daemon_starting_waits_boundedly_then_fails_open_as_warming():
    # V1.23: a starting daemon reads as "warming"; the step waits (bounded) first.
    h = Harness(run_bg=False)
    h.controller.apply(ON)

    h.controller.arm_look()
    result = h.controller.step.run("mira esto", "direct")

    assert result.outcome == "model_warming"
    assert result.screen_block == ""
    assert h.bridges == []  # never says "let me look" when it cannot
    assert 0 < sum(h.sleeps) <= 10.0 + 1e-9


def test_failed_daemon_is_rekicked_in_background_on_a_turn():
    h = Harness(run_bg=False, daemon_kwargs={"state_after_start": "failed"})
    h.controller.apply(ON)
    h.pending.pop()()
    assert h.daemons[0].state == "failed"

    h.controller.arm_look()
    result = h.controller.step.run("mira", "direct")

    assert result.outcome == "daemon_not_ready"
    assert len(h.pending) == 1  # a non-blocking restart was scheduled


def test_missing_model_fails_open_with_model_missing():
    h = Harness(daemon_kwargs={"installed": False})
    h.controller.apply(ON)
    h.controller.arm_look()

    result = h.controller.step.run("mira", "direct")

    assert result.outcome == "model_missing"
    assert h.controller.status()["model_installed"] is False


def test_external_daemon_counts_as_ready():
    h = Harness(daemon_kwargs={"state_after_start": "external"})
    h.controller.apply(ON)
    h.controller.arm_look()

    result = h.controller.step.run("mira", "ptt")

    assert result.outcome != "daemon_not_ready"


def test_capture_unavailable_keeps_gate_but_fails_open():
    h = Harness(capture_ok=False)
    h.controller.apply(ON)
    assert h.controller.status()["capture_available"] is False
    h.controller.arm_look()

    result = h.controller.step.run("mira", "direct")

    assert result.outcome == "capture_unavailable"


def test_arm_look_only_when_enabled():
    h = Harness()
    h.controller.apply(OFF)
    assert h.controller.arm_look() is False

    h.controller.apply(ON)
    assert h.controller.arm_look() is True
    assert h.controller.status()["armed"] is True


def test_runtime_apply_updates_gate_without_rebuilding_or_losing_arm():
    h = Harness()
    h.controller.apply(ON)
    step = h.controller.step
    h.controller.arm_look()

    h.controller.apply(replace(ON, look_on_ptt=True, capture_delay_s=1.0, describe_timeout_s=3.0,
                               rule_gate_enabled=True))

    assert h.controller.step is step
    assert len(h.daemons) == 1
    gate = step.gate
    assert gate.primary.look_on_ptt is True
    assert gate.primary.capture_delay_s == 1.0
    assert isinstance(gate.secondary, RuleVisionGate)
    assert gate.secondary.capture_delay_s == 1.0
    assert h.controller.status()["armed"] is True
    assert step._settings.describe_timeout_s == 3.0

    h.controller.apply(replace(ON, rule_gate_enabled=False))
    assert step.gate.secondary is None


def test_port_or_model_change_rebuilds_daemon_and_describer():
    h = Harness()
    h.controller.apply(ON)

    h.controller.apply(replace(ON, port=11440))

    assert len(h.daemons) == 2
    assert h.daemons[0].stop_calls == 1
    assert h.describers[-1].base_url == "http://127.0.0.1:11440"


def test_disable_stops_daemon_and_drops_step():
    h = Harness()
    h.controller.apply(ON)

    h.controller.apply(OFF)

    assert h.controller.step is None
    assert h.daemons[0].stop_calls == 1
    assert h.controller.status()["enabled"] is False


def test_shutdown_stops_owned_daemon_and_is_idempotent():
    h = Harness()
    h.controller.apply(ON)

    h.controller.shutdown()
    h.controller.shutdown()

    assert h.daemons[0].stop_calls == 1
    assert h.controller.step is None


def test_looking_events_are_forwarded():
    h = Harness()
    h.controller.apply(ON)
    h.controller.arm_look()

    h.controller.step.run("mira", "direct")

    assert h.looking_events[:1] == [True]
    assert h.looking_events[-1] is False


# -- V1.8: pre-warm, state machine, per-turn result ------------------------------


def test_disabled_state_is_off_with_no_result():
    h = Harness()
    h.controller.apply(OFF)

    status = h.controller.status()
    assert status["state"] == "off"
    assert status["last_error"] is None
    assert status["model_loaded"] is None
    assert status["last_result"] is None


def test_enable_is_starting_until_the_background_thread_runs():
    h = Harness(run_bg=False)
    h.controller.apply(ON)

    assert h.controller.status()["state"] == "starting"
    assert h.daemons[0].warms == []  # never warmed on the caller's thread


def test_background_start_warms_the_model_once_and_keeps_it_resident():
    h = Harness()
    h.controller.apply(ON)

    assert h.daemons[0].warms == [("qwen3-vl:4b-instruct", -1)]
    assert h.controller.status()["state"] == "ready"


def test_state_transitions_are_logged_once_each_without_content(caplog):
    caplog.set_level(logging.INFO, logger="OpenCohost")
    h = Harness()
    h.controller.apply(ON)

    lines = [r.getMessage() for r in caplog.records if "[VISION] state=" in r.getMessage()]
    assert any("state=off->starting" in line for line in lines)
    assert any("state=starting->warming" in line for line in lines)
    ready = [line for line in lines if "state=warming->ready" in line]
    assert len(ready) == 1 and "model_load_ms=" in ready[0]


def test_look_while_warming_fails_open_before_the_bridge():
    box = {}
    h = Harness()

    def _turn_during_warm():
        box["state"] = h.controller.status()["state"]
        h.controller.arm_look()
        box["result"] = h.controller.step.run("mira esto", "direct")

    h.daemon_kwargs = {"on_warm": _turn_during_warm}
    h.controller.apply(ON)

    assert box["state"] == "warming"
    assert box["result"].outcome == "model_warming"
    assert box["result"].note == ""
    assert h.bridges == []


def test_missing_model_is_failed_with_model_missing_and_never_warmed():
    h = Harness(daemon_kwargs={"installed": False})
    h.controller.apply(ON)

    status = h.controller.status()
    assert status["state"] == "failed"
    assert status["last_error"] == "model_missing"
    assert h.daemons[0].warms == []


def test_daemon_failure_is_failed_with_the_daemon_reason():
    h = Harness(daemon_kwargs={"state_after_start": "failed", "last_error": "port_in_use"})
    h.controller.apply(ON)

    status = h.controller.status()
    assert status["state"] == "failed"
    assert status["last_error"] == "port_in_use"
    h.controller.arm_look()
    assert h.controller.step.run("mira", "direct").outcome == "daemon_not_ready"


def test_warm_failure_is_failed_and_a_look_retries_in_background():
    h = Harness(run_bg=False, daemon_kwargs={"warm_ok": False})
    h.controller.apply(ON)
    h.pending.pop()()
    assert h.controller.status()["state"] == "failed"
    assert h.controller.status()["last_error"] == "warm_failed"

    h.controller.arm_look()
    result = h.controller.step.run("mira", "direct")

    assert result.outcome == "daemon_not_ready"
    assert h.bridges == []
    assert len(h.pending) == 1


def test_evicted_model_fails_open_as_warming_and_rewarms_in_background():
    h = Harness(run_bg=False)
    h.controller.apply(ON)
    h.pending.pop()()
    daemon = h.daemons[0]
    daemon.loaded = []  # someone evicted it
    h.clock.now += 60

    h.controller.arm_look()
    result = h.controller.step.run("mira", "direct")

    assert result.outcome == "model_warming"
    assert h.bridges == []
    assert h.controller.status()["state"] == "warming"
    h.pending.pop()()
    assert len(daemon.warms) == 2
    assert h.controller.status()["state"] == "ready"


def test_unreachable_daemon_on_a_turn_is_not_ready_and_restarts():
    h = Harness(run_bg=False)
    h.controller.apply(ON)
    h.pending.pop()()
    h.daemons[0].loaded = None  # /api/ps unreachable
    h.clock.now += 60

    h.controller.arm_look()
    result = h.controller.step.run("mira", "direct")

    assert result.outcome == "daemon_not_ready"
    assert h.controller.status()["state"] == "starting"
    assert len(h.pending) == 1


def test_model_loaded_probe_is_cached_between_turns():
    h = Harness()
    h.controller.apply(ON)
    daemon = h.daemons[0]
    base = daemon.ps_calls

    for _ in range(3):
        h.controller.arm_look()
        h.controller.step.run("mira", "direct")
        h.controller.status()

    assert daemon.ps_calls - base <= 1
    h.clock.now += 10
    h.controller.status()
    assert daemon.ps_calls - base <= 2


def test_disable_unloads_the_model_before_stopping_the_daemon():
    h = Harness()
    h.controller.apply(ON)
    daemon = h.daemons[0]

    h.controller.apply(OFF)

    assert daemon.unloads == ["qwen3-vl:4b-instruct"]
    assert daemon.ops[-2:] == ["unload", "stop"]
    assert h.controller.status()["state"] == "off"


def test_shutdown_unloads_then_stops():
    h = Harness()
    h.controller.apply(ON)

    h.controller.shutdown()

    assert h.daemons[0].ops[-2:] == ["unload", "stop"]


def test_model_change_unloads_the_old_model_and_warms_the_new_one():
    h = Harness()
    h.controller.apply(ON)

    h.controller.apply(replace(ON, model="moondream"))

    assert h.daemons[0].unloads == ["qwen3-vl:4b-instruct"]
    assert h.daemons[1].warms == [("moondream", -1)]
    assert h.controller.status()["state"] == "ready"


def test_a_warm_that_finishes_after_disable_is_unloaded_again():
    h = Harness()
    h.daemon_kwargs = {"on_warm": lambda: h.controller.apply(OFF)}

    h.controller.apply(ON)

    assert h.daemons[0].ops[-1] == "unload"
    assert h.controller.status()["state"] == "off"


def test_stale_start_thread_does_nothing_after_disable():
    h = Harness(run_bg=False)
    h.controller.apply(ON)
    h.controller.apply(OFF)

    h.pending.pop()()

    assert h.daemons[0].warms == []
    assert h.daemons[0].ensure_calls == 0
    assert h.controller.status()["state"] == "off"


def test_last_result_and_event_after_a_successful_look():
    h = Harness()
    h.controller.apply(ON)
    assert h.controller.status()["last_result"] is None

    h.controller.arm_look()
    h.controller.step.run("mira", "direct")

    last = h.controller.status()["last_result"]
    assert last["outcome"] == "ok"
    assert isinstance(last["capture_ms"], int) and isinstance(last["describe_ms"], int)
    assert last["at"] == 1700000000.0
    assert set(last) == {
        "outcome", "capture_ms", "describe_ms", "at",
        "id", "frames_captured", "frames_sent", "self_capture_skipped", "mode",
    }
    assert last["mode"] == "normal"
    assert [r["outcome"] for r in h.results] == ["ok"]
    assert set(h.results[0]) == {"outcome", "id", "describe_ms", "frames_sent", "mode"}
    assert h.results[0]["id"] == last["id"] and h.results[0]["mode"] == "normal"


def test_last_result_for_a_not_ready_look_has_no_timings():
    h = Harness(daemon_kwargs={"installed": False})
    h.controller.apply(ON)
    h.controller.arm_look()

    h.controller.step.run("mira", "direct")

    last = h.controller.status()["last_result"]
    assert isinstance(last.pop("id"), str)
    assert last == {"outcome": "model_missing", "capture_ms": None, "describe_ms": None,
                    "at": 1700000000.0, "frames_captured": 0, "frames_sent": 0,
                    "self_capture_skipped": 0, "mode": "normal"}
    assert [r["outcome"] for r in h.results] == ["model_missing"]


def test_describe_timeout_result_is_recorded_and_emitted():
    h = Harness(describe_error=VisionDescribeError("timeout"))
    h.controller.apply(ON)
    h.controller.arm_look()

    result = h.controller.step.run("mira", "direct")

    assert result.outcome == "describe_timeout"
    assert "describe_timeout" in result.note
    assert h.controller.status()["last_result"]["outcome"] == "describe_timeout"
    assert [r["outcome"] for r in h.results] == ["describe_timeout"]


def test_no_look_turn_emits_nothing():
    h = Harness()
    h.controller.apply(ON)

    h.controller.step.run("hola", "direct")

    assert h.results == []
    assert h.controller.status()["last_result"] is None


def test_result_callback_failure_is_contained():
    h = Harness()

    def _boom(_outcome):
        raise RuntimeError("x")

    h.controller._on_result = _boom
    h.controller.apply(ON)
    h.controller.arm_look()

    assert h.controller.step.run("mira", "direct").outcome == "ok"
    assert h.controller.status()["last_result"]["outcome"] == "ok"


# ------------------------------------------------- V1.11 burst + thumbnails


def test_a_look_takes_the_configured_burst_without_real_sleeps():
    h = Harness()
    h.controller.apply(replace(ON, frames=3, frame_interval_s=1.5))
    h.controller.arm_look()

    result = h.controller.step.run("mira", "direct")

    assert result.outcome == "ok"
    assert h.sleeps == [1.5, 1.5]
    last = h.controller.status()["last_result"]
    assert (last["frames_captured"], last["frames_sent"], last["self_capture_skipped"]) == (3, 3, 0)


def test_frames_are_served_by_look_id_as_data_urls_of_the_sent_bytes():
    h = Harness()
    h.controller.apply(replace(ON, frames=1))
    h.controller.arm_look()
    h.controller.step.run("mira", "direct")
    look_id = h.controller.status()["last_result"]["id"]

    thumbs = h.controller.thumbnails(look_id)

    assert thumbs == ["data:image/jpeg;base64,/9h4"]  # the sent b"\xff\xd8x", not the preview
    assert h.controller.thumbnails("unknown") is None


def test_thumbnails_are_not_kept_when_show_thumbnails_is_off():
    h = Harness()
    h.controller.apply(replace(ON, frames=1, show_thumbnails=False))
    h.controller.arm_look()
    h.controller.step.run("mira", "direct")

    assert h.controller.thumbnails(h.controller.status()["last_result"]["id"]) is None


def test_turning_show_thumbnails_off_clears_the_ring():
    h = Harness()
    h.controller.apply(replace(ON, frames=1))
    h.controller.arm_look()
    h.controller.step.run("mira", "direct")
    look_id = h.controller.status()["last_result"]["id"]

    h.controller.apply(replace(ON, frames=1, show_thumbnails=False))
    assert h.controller.thumbnails(look_id) is None
    h.controller.apply(replace(ON, frames=1, show_thumbnails=True))
    assert h.controller.thumbnails(look_id) is None  # cleared, not hidden


@pytest.mark.parametrize("teardown", ["disable", "shutdown"])
def test_disable_and_shutdown_clear_the_thumbnails(teardown):
    h = Harness()
    h.controller.apply(replace(ON, frames=1))
    h.controller.arm_look()
    h.controller.step.run("mira", "direct")
    look_id = h.controller.status()["last_result"]["id"]

    if teardown == "disable":
        h.controller.apply(OFF)
        h.controller.apply(replace(ON, frames=1))
    else:
        h.controller.shutdown()

    assert h.controller.thumbnails(look_id) is None


def test_self_capture_look_is_recorded_with_the_skip_count():
    h = Harness()
    h.foreground.is_self_foreground = lambda: True
    h.controller.apply(ON)
    h.controller.arm_look()

    result = h.controller.step.run("mira", "direct")

    assert result.outcome == "self_capture"
    assert "self_capture" in result.note
    last = h.controller.status()["last_result"]
    assert (last["outcome"], last["frames_captured"], last["self_capture_skipped"]) == ("self_capture", 0, 3)
    assert [r["outcome"] for r in h.results] == ["self_capture"]


# ------------------------------------------------ V1.11 spurious re-warm fix


def test_a_single_ps_miss_confirmed_loaded_does_not_rewarm():
    h = Harness(run_bg=False)
    h.controller.apply(ON)
    h.pending.pop()()
    daemon = h.daemons[0]
    daemon.ps_script = [False]  # one transient miss, then loaded again
    h.clock.now += 60

    h.controller.arm_look()
    result = h.controller.step.run("mira", "direct")

    assert result.outcome == "ok"
    assert h.controller.status()["state"] == "ready"
    assert len(daemon.warms) == 1 and h.pending == []


def test_a_single_ps_miss_on_a_status_poll_is_not_reported_as_unloaded():
    h = Harness(run_bg=False)
    h.controller.apply(ON)
    h.pending.pop()()
    h.daemons[0].ps_script = [False]
    h.clock.now += 60

    assert h.controller.status()["model_loaded"] is True
    assert h.controller.status()["state"] == "ready"


def test_two_consecutive_misses_rewarm():
    h = Harness(run_bg=False)
    h.controller.apply(ON)
    h.pending.pop()()
    daemon = h.daemons[0]
    daemon.ps_script = [False, False]
    h.clock.now += 60

    h.controller.arm_look()
    result = h.controller.step.run("mira", "direct")

    assert result.outcome == "model_warming"
    assert h.controller.status()["state"] == "warming"


def test_a_hit_between_misses_resets_the_count():
    h = Harness(run_bg=False)
    h.controller.apply(ON)
    h.pending.pop()()
    daemon = h.daemons[0]

    for answer in (False, True, False, True):
        daemon.ps_script = [answer]
        h.clock.now += 60
        h.controller.status()

    assert h.controller.status()["state"] == "ready"
    assert len(daemon.warms) == 1


# -- V1.13: describing phase ------------------------------------------------------


def test_describing_phase_is_forwarded_once_per_described_look():
    h = Harness()
    h.controller.apply(ON)
    h.controller.arm_look()

    h.controller.step.run("mira", "direct")

    assert h.describing == [True]


def test_describing_phase_is_silent_when_the_look_never_describes():
    h = Harness(capture_ok=False)
    h.controller.apply(ON)
    h.controller.arm_look()

    h.controller.step.run("mira", "direct")

    assert h.describing == []


# -------------------------------------------- V1.24: enabled flag for the prompt


def test_enabled_property_follows_the_state_machine():
    h = Harness(run_bg=False)
    assert h.controller.enabled is False

    h.controller.apply(ON)
    assert h.controller.enabled is True  # starting counts: vision is on

    h.controller.apply(OFF)
    assert h.controller.enabled is False


def test_clip_look_reports_clip_mode_in_last_result_and_event():
    h = Harness()
    h.controller.apply(ON)
    h.controller.arm_look()

    h.controller.step.run("que opinas del clip", "direct")

    assert h.controller.status()["last_result"]["mode"] == "clip"
    assert h.results[-1]["mode"] == "clip"


def test_describe_image_reuses_the_configured_describer():
    h = Harness()
    h.controller.apply(ON)

    assert h.controller.describe_image(b"\x89PNG-bytes", "what is this?") == "Barra de vida al 30%."


def test_describe_image_is_unavailable_while_vision_is_off():
    h = Harness()
    h.controller.apply(OFF)

    with pytest.raises(VisionDescribeError) as exc:
        h.controller.describe_image(b"x", "q")
    assert exc.value.reason == "unavailable"


def test_describe_image_propagates_describer_errors():
    h = Harness(describe_error=VisionDescribeError("timeout"))
    h.controller.apply(ON)

    with pytest.raises(VisionDescribeError) as exc:
        h.controller.describe_image(b"x", "q")
    assert exc.value.reason == "timeout"
