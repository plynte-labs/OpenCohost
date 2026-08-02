"""Unit 2.2 (runtime_findings_batch_20260731) — cloud fallback carries a
reason, and cloud comes back automatically once a background probe succeeds.

Builds on 1.1 (`classify_cloud_error` taxonomy) and 2.1 (in-turn `rate_limited`
retry): the SAME `_handle_cloud_failure` fallback machine now records WHY it
engaged (`_cloud_fallback_reason`), schedules a class-specific background
return probe (or deliberately does NOT, for `ambiguous_429`/`bad_key`), and
reverses itself -- invalidating speculative drafts, bumping `provider_epoch`,
clearing the flag -- only after a successful probe.

F12's precondition ("no draft generated under one provider may be played
under another, in either direction") is asserted directly: both the
cloud->local (fallback engaging) and local->cloud (auto-return) transitions
must clear `_prefetched_agenda`/`_frozen_stash`, exactly like
`tests/test_engine_prefetch_invalidation.py` asserts for model/profile
switches.

No real thread ever waits more than a fraction of a second in this file:
`_run_cloud_prober` is driven directly (not via `_start_cloud_prober`) for
every test that isn't specifically about the real background thread, and the
two thread-integration tests monkeypatch the llm_engine-local backoff
constant down to a few seconds and poll with `_wait_until` instead of
sleeping for it.
"""

import logging
import queue
import re
import threading
import time
from unittest.mock import MagicMock, patch

from opencohost.api import engine_host
from opencohost.config import settings
from opencohost.core import cloud_llm_client
from opencohost.core.llm_engine import MotorVocalIA

_SEND = "opencohost.core.cloud_llm_client.send_chat_completion"


def _wait_until(predicate, *, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def _cloud_config(fallback_mode="auto"):
    return {
        "active_provider": "openai",
        "fallback_mode": fallback_mode,
        "pregen_enabled": False,
        "profiles": {
            "openai": {"base_url": "https://api.example.com/v1", "model": "gpt-cloud"},
        },
    }


def _make_motor(tmp_path, *, provider_config=None):
    """Mirrors test_llm_fallback.py's `_make_motor` -- ollama/pygame mocked,
    LAST_MODEL_FILE isolated -- plus `_prepare_model`/`_hablar` stubbed so the
    warm+speak background worker (already pinned by test_llm_fallback.py)
    never touches real state here."""
    import os

    original_last_model = settings.LAST_MODEL_FILE
    settings.LAST_MODEL_FILE = os.path.join(str(tmp_path), "last_model.json")
    ui_events = []
    try:
        motor = MotorVocalIA(queue.Queue(), ui_events.append)
    finally:
        settings.LAST_MODEL_FILE = original_last_model
    motor.ollama = MagicMock()
    motor.pygame = MagicMock()
    motor.is_ready = True
    motor._loaded_model = motor.current_model
    motor._provider_config = provider_config if provider_config is not None else _cloud_config()
    motor._prepare_model = MagicMock(return_value=True)
    motor._hablar = MagicMock()
    return motor, ui_events


# ---------------------------------------------------------------------------
# 0. Whitelist / schema wiring
# ---------------------------------------------------------------------------

def test_new_events_are_whitelisted_with_the_right_detail_schema():
    assert "cloud_fallback_engaged" in engine_host._MOTOR_EVENT_WHITELIST
    assert "cloud_probe_scheduled" in engine_host._MOTOR_EVENT_WHITELIST
    assert "cloud_restored" in engine_host._MOTOR_EVENT_WHITELIST
    assert engine_host._MOTOR_EVENT_DETAIL_FIELDS["cloud_probe_scheduled"] == ("seconds",)


def test_status_response_exposes_the_new_fields():
    from opencohost.api.models import StatusResponse

    fields = StatusResponse.model_fields
    assert "fallback_reason" in fields
    assert "next_cloud_probe_in_seconds" in fields
    assert fields["fallback_reason"].default is None
    assert fields["next_cloud_probe_in_seconds"].default is None


# ---------------------------------------------------------------------------
# 1. Reason recording
# ---------------------------------------------------------------------------

class TestFallbackReason:
    def test_records_failure_class_as_reason(self, tmp_path):
        motor, ui_events = _make_motor(tmp_path)

        motor._handle_cloud_failure(
            "direct", failure_class=cloud_llm_client.CLOUD_ERROR_RATE_LIMITED, retry_after_seconds=20
        )

        assert motor._cloud_fallback_reason == "rate_limited"
        assert "cloud_fallback_engaged" in ui_events
        motor._stop_cloud_prober()

    def test_provider_runtime_state_carries_reason_and_epoch(self, tmp_path):
        motor, _ = _make_motor(tmp_path)
        before_epoch = motor.provider_epoch

        motor._handle_cloud_failure("direct", failure_class=cloud_llm_client.CLOUD_ERROR_BAD_KEY)
        state = motor.provider_runtime_state()

        assert state["fallback_reason"] == "bad_key"
        assert state["provider_epoch"] == before_epoch + 1
        assert state["next_cloud_probe_in_seconds"] is None  # bad_key: no probe

    def test_manual_mode_records_no_reason_and_starts_no_probe(self, tmp_path):
        motor, ui_events = _make_motor(tmp_path, provider_config=_cloud_config(fallback_mode="manual"))

        motor._handle_cloud_failure("direct", failure_class=cloud_llm_client.CLOUD_ERROR_TRANSIENT)

        assert motor._cloud_fallback_reason is None
        assert motor._cloud_prober_thread is None
        assert "cloud_fallback_engaged" not in ui_events


# ---------------------------------------------------------------------------
# 2. Per-class probe scheduling
# ---------------------------------------------------------------------------

class TestProbeScheduling:
    def test_rate_limited_honours_retry_after_within_bounds(self, tmp_path):
        motor, _ = _make_motor(tmp_path)
        wait = motor._initial_cloud_probe_wait(cloud_llm_client.CLOUD_ERROR_RATE_LIMITED, 45)
        assert wait == 45.0

    def test_rate_limited_floors_a_tiny_retry_after(self, tmp_path):
        motor, _ = _make_motor(tmp_path)
        wait = motor._initial_cloud_probe_wait(cloud_llm_client.CLOUD_ERROR_RATE_LIMITED, 1)
        assert wait == settings.CLOUD_AUTO_RETURN_RATE_LIMIT_FLOOR_SECONDS

    def test_rate_limited_caps_a_runaway_retry_after(self, tmp_path):
        motor, _ = _make_motor(tmp_path)
        wait = motor._initial_cloud_probe_wait(cloud_llm_client.CLOUD_ERROR_RATE_LIMITED, 999999)
        assert wait == settings.CLOUD_AUTO_RETURN_RATE_LIMIT_CAP_SECONDS

    def test_rate_limited_defaults_to_floor_without_a_header(self, tmp_path):
        motor, _ = _make_motor(tmp_path)
        wait = motor._initial_cloud_probe_wait(cloud_llm_client.CLOUD_ERROR_RATE_LIMITED, None)
        assert wait == settings.CLOUD_AUTO_RETURN_RATE_LIMIT_FLOOR_SECONDS

    def test_transient_starts_at_base(self, tmp_path):
        motor, _ = _make_motor(tmp_path)
        wait = motor._initial_cloud_probe_wait(cloud_llm_client.CLOUD_ERROR_TRANSIENT, None)
        assert wait == settings.CLOUD_AUTO_RETURN_TRANSIENT_BASE_SECONDS

    def test_transient_backoff_doubles(self, tmp_path):
        motor, _ = _make_motor(tmp_path)
        base = settings.CLOUD_AUTO_RETURN_TRANSIENT_BASE_SECONDS
        doubled = motor._next_cloud_probe_wait(cloud_llm_client.CLOUD_ERROR_TRANSIENT, base)
        assert doubled == base * 2

    def test_transient_backoff_caps(self, tmp_path):
        motor, _ = _make_motor(tmp_path)
        cap = settings.CLOUD_AUTO_RETURN_TRANSIENT_CAP_SECONDS
        doubled = motor._next_cloud_probe_wait(cloud_llm_client.CLOUD_ERROR_TRANSIENT, cap)
        assert doubled == cap  # never exceeds the cap

    def test_rate_limited_backoff_caps_independently(self, tmp_path):
        motor, _ = _make_motor(tmp_path)
        cap = settings.CLOUD_AUTO_RETURN_RATE_LIMIT_CAP_SECONDS
        doubled = motor._next_cloud_probe_wait(cloud_llm_client.CLOUD_ERROR_RATE_LIMITED, cap)
        assert doubled == cap

    def test_ambiguous_429_schedules_a_conservative_probe(self, tmp_path):
        """WU3 (cloud_rearm_20260801, owner decision D-A) supersedes the
        pre-WU3 contract this test used to assert (ambiguous_429 never
        auto-probes): it now DOES get an automatic probe loop by default,
        with its own conservative backoff. See
        TestAmbiguous429ConservativeAuto for the flag-off/give-up/backoff
        coverage; bad_key right below is still genuinely non-probeable in
        every variant."""
        motor, _ = _make_motor(tmp_path)
        blocked = threading.Event()

        def fake_probe(_cfg):
            blocked.wait(timeout=2.0)
            return True

        motor._cloud_probe_once = MagicMock(side_effect=fake_probe)

        motor._handle_cloud_failure("direct", failure_class=cloud_llm_client.CLOUD_ERROR_AMBIGUOUS_429)

        assert _wait_until(lambda: motor._cloud_prober_thread is not None)
        assert motor._cloud_probe_next_at is not None
        assert motor._cloud_fallback_reason == "ambiguous_429"
        blocked.set()
        motor._stop_cloud_prober()

    def test_bad_key_schedules_no_probe(self, tmp_path):
        motor, _ = _make_motor(tmp_path)

        motor._handle_cloud_failure("direct", failure_class=cloud_llm_client.CLOUD_ERROR_BAD_KEY)
        time.sleep(0.05)

        assert motor._cloud_prober_thread is None
        assert motor._cloud_probe_next_at is None


# ---------------------------------------------------------------------------
# 3. Stale-draft safety, both directions (F12 precondition)
# ---------------------------------------------------------------------------

class TestStaleDraftSafety:
    def test_fallback_engagement_invalidates_existing_drafts(self, tmp_path):
        """cloud -> local: a draft made under cloud must not survive into
        local playback once the fallback engages."""
        motor, _ = _make_motor(tmp_path)
        motor._prefetched_agenda = {"dialogo": "cloud draft"}
        motor._frozen_stash = {"dialogo": "cloud stash draft"}
        epoch_before = motor._prefetch_epoch

        motor._handle_cloud_failure("kira-agenda", failure_class=cloud_llm_client.CLOUD_ERROR_TRANSIENT)

        assert motor._prefetched_agenda is None
        assert motor._frozen_stash is None
        assert motor._prefetch_epoch == epoch_before + 1
        motor._stop_cloud_prober()

    def test_auto_return_invalidates_existing_drafts(self, tmp_path):
        """local -> cloud: a draft made during the local fallback must not
        survive the return to cloud."""
        motor, _ = _make_motor(tmp_path)
        motor._cloud_fallback_active = True
        motor._prefetched_agenda = {"dialogo": "local draft"}
        motor._frozen_stash = {"dialogo": "local stash draft"}
        epoch_before = motor._prefetch_epoch

        motor._on_cloud_probe_success(motor._cloud_prober_generation)

        assert motor._prefetched_agenda is None
        assert motor._frozen_stash is None
        assert motor._prefetch_epoch == epoch_before + 1

    def test_manual_provider_put_invalidates_existing_drafts(self, tmp_path):
        """F12's originally-reported gap: `set_provider_config` itself must
        invalidate on ANY provider transition, not only via the auto-return
        path this unit adds."""
        motor, _ = _make_motor(tmp_path)
        motor._prefetched_agenda = {"dialogo": "stale draft"}
        motor._frozen_stash = {"dialogo": "stale stash"}
        new_cfg = _cloud_config()
        new_cfg["active_provider"] = "local"

        motor.set_provider_config(new_cfg)

        assert motor._prefetched_agenda is None
        assert motor._frozen_stash is None

    def test_same_provider_put_does_not_invalidate(self, tmp_path):
        """An unrelated PUT that keeps the same active_provider must not
        touch drafts -- mirrors the existing flag-reset contract."""
        motor, _ = _make_motor(tmp_path)
        motor._prefetched_agenda = {"dialogo": "still fresh"}
        new_cfg = _cloud_config()
        new_cfg["pregen_enabled"] = True  # unrelated field flipped

        motor.set_provider_config(new_cfg)

        assert motor._prefetched_agenda == {"dialogo": "still fresh"}


# ---------------------------------------------------------------------------
# 4. The RETURN sequence, in order
# ---------------------------------------------------------------------------

class TestProbeSuccessOrder:
    def test_invalidate_runs_before_the_flag_clears(self, tmp_path):
        motor, ui_events = _make_motor(tmp_path)
        motor._cloud_fallback_active = True
        motor._cloud_fallback_reason = "transient"
        motor._cloud_probe_next_at = time.monotonic() + 30
        seen_flag_at_invalidate = {}

        def _rec_pregen():
            seen_flag_at_invalidate["pregen"] = motor._cloud_fallback_active

        def _rec_stash():
            seen_flag_at_invalidate["stash"] = motor._cloud_fallback_active

        motor._invalidate_pregen_epoch = MagicMock(side_effect=_rec_pregen)
        motor._invalidate_frozen_stash = MagicMock(side_effect=_rec_stash)
        epoch_before = motor.provider_epoch

        motor._on_cloud_probe_success(motor._cloud_prober_generation)

        # (a) invalidation saw the OLD (still-active) flag -- proves it ran
        # before (c) the flag clear below.
        assert seen_flag_at_invalidate == {"pregen": True, "stash": True}
        # (b) epoch bumped, (c) flag+reason+schedule cleared under the lock.
        assert motor.provider_epoch == epoch_before + 1
        assert motor._cloud_fallback_active is False
        assert motor._cloud_fallback_reason is None
        assert motor._cloud_probe_next_at is None
        # (d) narration.
        assert "cloud_restored" in ui_events


# ---------------------------------------------------------------------------
# 5. The probe loop itself (driven directly -- no real waiting)
# ---------------------------------------------------------------------------

class TestProbeLoop:
    def test_probe_success_triggers_the_return_sequence(self, tmp_path):
        motor, ui_events = _make_motor(tmp_path)
        motor._cloud_fallback_active = True
        motor._cloud_probe_once = MagicMock(return_value=True)
        stop_event = threading.Event()

        motor._run_cloud_prober(
            stop_event, cloud_llm_client.CLOUD_ERROR_TRANSIENT, 0, motor._cloud_prober_generation
        )

        assert motor._cloud_fallback_active is False
        assert "cloud_restored" in ui_events

    def test_probe_failure_reschedules_using_backoff_and_stays_local(self, tmp_path):
        motor, ui_events = _make_motor(tmp_path)
        motor._cloud_fallback_active = True
        stop_event = threading.Event()
        calls = {"n": 0}

        def fake_probe(_cfg):
            calls["n"] += 1
            if calls["n"] >= 2:
                stop_event.set()  # stop deterministically after 2 failures
            return False

        motor._cloud_probe_once = MagicMock(side_effect=fake_probe)
        # Tiny, not the real per-class backoff value -- the loop's NEXT
        # `stop_event.wait(timeout=...)` actually blocks for this long in
        # real wall-clock time (the event is only set from INSIDE the 2nd
        # probe call, i.e. AFTER that wait returns), so a large mocked value
        # here would make this test really sleep for it.
        motor._next_cloud_probe_wait = MagicMock(return_value=0.01)

        motor._run_cloud_prober(
            stop_event, cloud_llm_client.CLOUD_ERROR_TRANSIENT, 0.01, motor._cloud_prober_generation
        )

        assert calls["n"] == 2  # the first failure's reschedule let it run again
        motor._next_cloud_probe_wait.assert_called_once_with(
            cloud_llm_client.CLOUD_ERROR_TRANSIENT, 0.01
        )
        assert motor._cloud_fallback_active is True  # still local -- never returned
        assert ui_events.count("cloud_probe_scheduled") == 1
        assert motor._cloud_probe_next_at is not None

    def test_stop_event_aborts_before_any_probe(self, tmp_path):
        motor, _ = _make_motor(tmp_path)
        motor._cloud_probe_once = MagicMock()
        stop_event = threading.Event()
        stop_event.set()

        motor._run_cloud_prober(
            stop_event, cloud_llm_client.CLOUD_ERROR_TRANSIENT, 0, motor._cloud_prober_generation
        )

        motor._cloud_probe_once.assert_not_called()

    def test_stale_probe_result_is_dropped_after_a_stop(self, tmp_path):
        """A probe already in flight when `_stop_cloud_prober` fires must not
        apply its result -- the stop check right after the network call
        catches it even on success."""
        motor, ui_events = _make_motor(tmp_path)
        motor._cloud_fallback_active = True
        stop_event = threading.Event()

        def fake_probe(_cfg):
            stop_event.set()  # simulate a manual PUT landing mid-probe
            return True

        motor._cloud_probe_once = MagicMock(side_effect=fake_probe)

        motor._run_cloud_prober(
            stop_event, cloud_llm_client.CLOUD_ERROR_TRANSIENT, 0, motor._cloud_prober_generation
        )

        assert motor._cloud_fallback_active is True  # untouched -- result dropped
        assert "cloud_restored" not in ui_events


# ---------------------------------------------------------------------------
# 6. Probe request privacy (no history, no persona)
# ---------------------------------------------------------------------------

class TestProbePrivacy:
    def test_probe_sends_only_the_static_message(self, tmp_path):
        motor, _ = _make_motor(tmp_path)
        motor.historial.append({"role": "user", "content": "algo que Kira NUNCA debe repetir"})
        captured = {}

        def fake_send(**kwargs):
            captured.update(kwargs)
            return {"choices": [{"message": {"content": "pong"}}]}

        def fake_adapt(*_a, **_k):
            return {"message": {"content": "pong", "thinking": ""}, "usage": {}}

        with patch(_SEND, side_effect=lambda **kw: {"message": {"content": "pong", "thinking": ""}}) as mock_send:
            ok = motor._cloud_probe_once(motor._provider_config)
            captured.update(mock_send.call_args.kwargs)

        assert ok is True
        assert captured["messages"] == [{"role": "user", "content": "ping"}]
        assert "algo que Kira" not in str(captured["messages"])
        assert motor.system_prompt not in str(captured["messages"])
        assert captured["options"] == {"num_predict": 1}
        assert captured["timeout"] == settings.CLOUD_CHAT_TIMEOUT

    def test_probe_failure_is_any_exception(self, tmp_path):
        motor, _ = _make_motor(tmp_path)
        with patch(_SEND, side_effect=cloud_llm_client.CloudLLMResponseError("HTTP 500")):
            ok = motor._cloud_probe_once(motor._provider_config)
        assert ok is False


# ---------------------------------------------------------------------------
# 7. Provider PUT cancels a running prober; thread hygiene
# ---------------------------------------------------------------------------

class TestProberCancellationAndHygiene:
    def test_provider_put_cancels_a_running_prober_promptly(self, tmp_path, monkeypatch):
        # Long enough that the prober is still sleeping when cancelled --
        # proves cancellation INTERRUPTS the wait, not merely a flag the loop
        # happens to poll on its own schedule.
        monkeypatch.setattr("opencohost.core.llm_engine.CLOUD_AUTO_RETURN_TRANSIENT_BASE_SECONDS", 5)
        motor, _ = _make_motor(tmp_path)

        motor._handle_cloud_failure("direct", failure_class=cloud_llm_client.CLOUD_ERROR_TRANSIENT)
        assert _wait_until(
            lambda: motor._cloud_prober_thread is not None and motor._cloud_prober_thread.is_alive()
        )
        thread = motor._cloud_prober_thread

        new_cfg = _cloud_config()
        new_cfg["active_provider"] = "local"
        started = time.monotonic()
        motor.set_provider_config(new_cfg)
        elapsed = time.monotonic() - started

        assert elapsed < 2.0  # cancelled promptly, not after the 5s wait
        assert motor._cloud_prober_stop is None
        assert motor._cloud_fallback_reason is None
        assert motor._cloud_probe_next_at is None
        thread.join(timeout=2.0)
        assert thread.is_alive() is False

    def test_prober_thread_is_daemon_and_stops_on_stop_event(self, tmp_path, monkeypatch):
        monkeypatch.setattr("opencohost.core.llm_engine.CLOUD_AUTO_RETURN_TRANSIENT_BASE_SECONDS", 5)
        motor, _ = _make_motor(tmp_path)

        motor._handle_cloud_failure("direct", failure_class=cloud_llm_client.CLOUD_ERROR_TRANSIENT)
        assert _wait_until(lambda: motor._cloud_prober_thread is not None)
        thread = motor._cloud_prober_thread
        assert thread.daemon is True

        motor._stop_cloud_prober()
        thread.join(timeout=2.0)

        assert thread.is_alive() is False

    def test_set_provider_config_invalidates_before_the_prober_join(self, tmp_path, monkeypatch):
        """Finding 3 (order): invalidation of speculative drafts must run
        BEFORE the up-to-2s prober join, mirroring the invalidate-then-stop
        order the other two transitions (`_handle_cloud_failure`,
        `_on_cloud_probe_success`) already use."""
        monkeypatch.setattr("opencohost.core.llm_engine.CLOUD_AUTO_RETURN_TRANSIENT_BASE_SECONDS", 5)
        motor, _ = _make_motor(tmp_path)
        motor._handle_cloud_failure("direct", failure_class=cloud_llm_client.CLOUD_ERROR_TRANSIENT)
        assert _wait_until(
            lambda: motor._cloud_prober_thread is not None and motor._cloud_prober_thread.is_alive()
        )
        call_order = []
        real_invalidate_pregen = motor._invalidate_pregen_epoch
        real_invalidate_stash = motor._invalidate_frozen_stash
        real_stop_prober = motor._stop_cloud_prober

        def _rec_pregen():
            call_order.append("invalidate_pregen")
            real_invalidate_pregen()

        def _rec_stash():
            call_order.append("invalidate_stash")
            real_invalidate_stash()

        def _rec_stop():
            call_order.append("stop_prober")
            real_stop_prober()

        motor._invalidate_pregen_epoch = _rec_pregen
        motor._invalidate_frozen_stash = _rec_stash
        motor._stop_cloud_prober = _rec_stop

        new_cfg = _cloud_config()
        new_cfg["active_provider"] = "local"
        motor.set_provider_config(new_cfg)

        assert call_order.index("invalidate_pregen") < call_order.index("stop_prober")
        assert call_order.index("invalidate_stash") < call_order.index("stop_prober")


# ---------------------------------------------------------------------------
# 8. Second-failure reconciliation (findings 2/4) -- a NEW failure must never
#    be silently swallowed by whatever prober is already running.
# ---------------------------------------------------------------------------

class TestSecondFailureReconciliation:
    def test_bad_key_second_failure_stops_a_live_transient_prober(self, tmp_path, monkeypatch):
        """Finding 2: a non-probeable second failure must stop the live
        prober outright -- it must never survive to auto-return under a
        reason (`bad_key`) the policy table forbids."""
        monkeypatch.setattr("opencohost.core.llm_engine.CLOUD_AUTO_RETURN_TRANSIENT_BASE_SECONDS", 5)
        motor, _ = _make_motor(tmp_path)

        motor._handle_cloud_failure("direct", failure_class=cloud_llm_client.CLOUD_ERROR_TRANSIENT)
        assert _wait_until(
            lambda: motor._cloud_prober_thread is not None and motor._cloud_prober_thread.is_alive()
        )
        old_thread = motor._cloud_prober_thread
        old_generation = motor._cloud_prober_generation

        motor._handle_cloud_failure("direct", failure_class=cloud_llm_client.CLOUD_ERROR_BAD_KEY)

        assert motor._cloud_prober_thread is None
        assert motor._cloud_probe_next_at is None
        old_thread.join(timeout=2.0)
        assert old_thread.is_alive() is False

        # Even if the stale prober's success somehow lands late, the
        # generation guard must discard it -- no auto-return under bad_key.
        motor._on_cloud_probe_success(old_generation)
        assert motor._cloud_fallback_active is True
        assert motor._cloud_fallback_reason == "bad_key"

    def test_ambiguous_429_second_failure_replaces_transient_schedule(self, tmp_path, monkeypatch):
        """WU3 (cloud_rearm_20260801) supersedes the pre-WU3 contract this
        test used to assert (ambiguous_429 stops the live prober outright,
        nothing left running): ambiguous_429 is now ALSO probeable by
        default, so a second failure of this class REPLACES the live
        transient prober with its own conservative schedule -- same shape
        as `test_rate_limited_second_failure_replaces_transient_schedule`
        right below. See
        `test_ambiguous_429_second_failure_stops_outright_when_flag_off`
        for the preserved pre-WU3 contract (finding 2) when the flag is off."""
        monkeypatch.setattr("opencohost.core.llm_engine.CLOUD_AUTO_RETURN_TRANSIENT_BASE_SECONDS", 5)
        motor, _ = _make_motor(tmp_path)

        motor._handle_cloud_failure("direct", failure_class=cloud_llm_client.CLOUD_ERROR_TRANSIENT)
        assert _wait_until(
            lambda: motor._cloud_prober_thread is not None and motor._cloud_prober_thread.is_alive()
        )
        old_thread = motor._cloud_prober_thread

        motor._handle_cloud_failure("direct", failure_class=cloud_llm_client.CLOUD_ERROR_AMBIGUOUS_429)

        assert motor._cloud_prober_thread is not None
        assert motor._cloud_prober_thread is not old_thread
        assert motor._cloud_fallback_reason == "ambiguous_429"
        remaining = motor._cloud_probe_next_at - time.monotonic()
        assert 0 < remaining <= settings.CLOUD_AUTO_RETURN_AMBIGUOUS_429_BASE_SECONDS

        old_thread.join(timeout=2.0)
        motor._stop_cloud_prober()

    def test_ambiguous_429_second_failure_stops_outright_when_flag_off(self, tmp_path, monkeypatch):
        """Finding 2's original contract (pre-WU3), preserved when the
        conservative auto-return flag is off: ambiguous_429 stays a
        genuinely NON-PROBEABLE class in that mode, so it must stop the
        live prober outright -- it must never survive to auto-return under
        a reason the policy table forbids."""
        monkeypatch.setattr("opencohost.core.llm_engine.CLOUD_AUTO_RETURN_TRANSIENT_BASE_SECONDS", 5)
        monkeypatch.setattr(
            "opencohost.core.llm_engine.CLOUD_AUTO_RETURN_AMBIGUOUS_429_ENABLED", False
        )
        motor, _ = _make_motor(tmp_path)

        motor._handle_cloud_failure("direct", failure_class=cloud_llm_client.CLOUD_ERROR_TRANSIENT)
        assert _wait_until(
            lambda: motor._cloud_prober_thread is not None and motor._cloud_prober_thread.is_alive()
        )
        old_thread = motor._cloud_prober_thread
        old_generation = motor._cloud_prober_generation

        motor._handle_cloud_failure("direct", failure_class=cloud_llm_client.CLOUD_ERROR_AMBIGUOUS_429)

        assert motor._cloud_prober_thread is None
        assert motor._cloud_probe_next_at is None
        old_thread.join(timeout=2.0)
        assert old_thread.is_alive() is False

        motor._on_cloud_probe_success(old_generation)
        assert motor._cloud_fallback_active is True
        assert motor._cloud_fallback_reason == "ambiguous_429"

    def test_rate_limited_second_failure_replaces_transient_schedule(self, tmp_path, monkeypatch):
        """Finding 4: a second PROBEABLE failure's own class/schedule must
        win -- a `rate_limited` Retry-After must not be discarded in favour
        of the stale `transient` 30s cadence."""
        monkeypatch.setattr("opencohost.core.llm_engine.CLOUD_AUTO_RETURN_TRANSIENT_BASE_SECONDS", 5)
        motor, _ = _make_motor(tmp_path)

        motor._handle_cloud_failure("direct", failure_class=cloud_llm_client.CLOUD_ERROR_TRANSIENT)
        assert _wait_until(
            lambda: motor._cloud_prober_thread is not None and motor._cloud_prober_thread.is_alive()
        )
        old_thread = motor._cloud_prober_thread

        motor._handle_cloud_failure(
            "direct", failure_class=cloud_llm_client.CLOUD_ERROR_RATE_LIMITED, retry_after_seconds=20
        )

        assert motor._cloud_prober_thread is not None
        assert motor._cloud_prober_thread is not old_thread  # replaced, not the stale transient one
        assert motor._cloud_fallback_reason == "rate_limited"
        remaining = motor._cloud_probe_next_at - time.monotonic()
        assert 0 < remaining <= 20  # the new class's Retry-After, not the old 5s cadence

        old_thread.join(timeout=2.0)
        motor._stop_cloud_prober()

    def test_stale_generation_reschedule_is_discarded_without_a_stop_signal(self, tmp_path):
        """Finding 1B: simulates a prober superseded purely by a concurrent
        generation bump (its OWN stop_event never gets touched) -- exactly
        the handle-race the judge described. The reschedule write on a
        failed probe must still be discarded."""
        motor, ui_events = _make_motor(tmp_path)
        motor._cloud_fallback_active = True
        stop_event = threading.Event()  # deliberately never set
        my_generation = motor._cloud_prober_generation

        def fake_probe(_cfg):
            # Simulate a concurrent _start_cloud_prober call landing exactly
            # here -- it bumps the counter without ever touching THIS
            # prober's own stop_event (finding 1B's exact gap).
            motor._cloud_prober_generation += 1
            return False

        motor._cloud_probe_once = MagicMock(side_effect=fake_probe)
        motor._next_cloud_probe_wait = MagicMock(return_value=0.01)

        motor._run_cloud_prober(stop_event, cloud_llm_client.CLOUD_ERROR_TRANSIENT, 0, my_generation)

        assert motor._cloud_probe_next_at is None  # never resurrected
        assert ui_events.count("cloud_probe_scheduled") == 0
        assert stop_event.is_set() is False  # proves this isn't just the stop-event check


# ---------------------------------------------------------------------------
# 9. PUT concurrent with a prober blocked mid network-call (finding 1, race A)
# ---------------------------------------------------------------------------

class TestPutConcurrentWithBlockedProber:
    def test_put_concurrent_with_blocked_probe_does_not_starve_a_new_prober(self, tmp_path, monkeypatch):
        """The join inside `_stop_cloud_prober` cannot interrupt a prober
        blocked INSIDE its HTTP call -- it times out. A fresh cloud failure
        arriving right after must still be able to start a NEW prober (no
        `already_running` starvation), and the old, now-zombie prober's
        belated result must never mutate state again."""
        monkeypatch.setattr("opencohost.core.llm_engine.CLOUD_AUTO_RETURN_TRANSIENT_BASE_SECONDS", 0.01)
        monkeypatch.setattr("opencohost.core.llm_engine.CLOUD_PROBER_JOIN_TIMEOUT_SECONDS", 0.05)
        motor, _ = _make_motor(tmp_path)
        blocked = threading.Event()
        release = threading.Event()

        def fake_probe(_cfg):
            blocked.set()
            release.wait(timeout=2.0)  # simulates a slow HTTP call the stop event can't interrupt
            return True

        motor._cloud_probe_once = MagicMock(side_effect=fake_probe)

        motor._handle_cloud_failure("direct", failure_class=cloud_llm_client.CLOUD_ERROR_TRANSIENT)
        assert _wait_until(lambda: blocked.is_set())
        old_thread = motor._cloud_prober_thread

        new_cfg = _cloud_config()
        new_cfg["active_provider"] = "local"
        motor.set_provider_config(new_cfg)  # join times out -- the prober is still blocked

        assert motor._cloud_prober_thread is None
        assert motor._cloud_probe_next_at is None

        # A fresh failure must NOT be starved behind the still-running zombie
        # (the flag/reason are independent of the persisted active_provider
        # field, so re-engaging does not require another PUT first).
        motor._handle_cloud_failure("direct", failure_class=cloud_llm_client.CLOUD_ERROR_TRANSIENT)
        assert _wait_until(
            lambda: motor._cloud_prober_thread is not None and motor._cloud_prober_thread is not old_thread
        )
        new_thread = motor._cloud_prober_thread
        # Stop the new prober too (deterministically, via the same bounded
        # join) so its OWN mocked-successful probe cannot race this test's
        # remaining assertions -- it will also time out and become a zombie,
        # exactly like the old one.
        motor._stop_cloud_prober()

        release.set()  # let both blocked probes finally return
        old_thread.join(timeout=2.0)
        new_thread.join(timeout=2.0)
        assert old_thread.is_alive() is False
        assert new_thread.is_alive() is False
        # Both zombies' belated "success" must be no-ops -- the reason set by
        # the second (legitimate) failure survives untouched.
        assert motor._cloud_fallback_reason == "transient"


# ---------------------------------------------------------------------------
# 10. Local warm-failure un-fallback (finding 5) -- also a provider transition
# ---------------------------------------------------------------------------

class TestWarmFailureUnFallbackInvalidation:
    def test_local_warm_failure_invalidates_drafts_and_bumps_epoch(self, tmp_path):
        motor, ui_events = _make_motor(tmp_path)
        motor._prepare_model = MagicMock(return_value=False)  # un-mock the happy path
        motor._prefetched_agenda = {"dialogo": "local draft made during the warm window"}
        motor._frozen_stash = {"dialogo": "local stash draft"}
        epoch_before = motor.provider_epoch
        prefetch_epoch_before = motor._prefetch_epoch

        motor._handle_cloud_failure("direct", failure_class=cloud_llm_client.CLOUD_ERROR_TRANSIENT)

        assert _wait_until(lambda: "cloud_llm_error" in ui_events)
        assert motor._prefetched_agenda is None
        assert motor._frozen_stash is None
        # +2: the initial cloud->local engage invalidates once, the
        # un-fallback (this fix) invalidates again -- both are real
        # transitions.
        assert motor._prefetch_epoch == prefetch_epoch_before + 2
        assert motor.provider_epoch == epoch_before + 2
        assert motor._cloud_fallback_active is False
        assert motor._cloud_fallback_reason is None
        motor._stop_cloud_prober()


# ---------------------------------------------------------------------------
# 11. Manual probe trigger (WU1, cloud_rearm_20260801) -- an on-demand probe
#     that side-steps `_initial_cloud_probe_wait`'s per-class table so
#     ambiguous_429/bad_key (manual-re-arm-only classes) can also be probed
#     on demand, e.g. via POST /api/llm/provider/probe (WU2).
# ---------------------------------------------------------------------------

class TestManualProbeTrigger:
    def test_noop_when_not_in_fallback(self, tmp_path):
        motor, _ = _make_motor(tmp_path)

        result = motor.trigger_cloud_probe_now()

        assert result == {"armed": False, "reason": "not_in_fallback"}
        assert motor._cloud_prober_thread is None

    def test_noop_when_no_cloud_profile(self, tmp_path):
        motor, _ = _make_motor(
            tmp_path, provider_config={"active_provider": "openai", "profiles": {}}
        )
        motor._cloud_fallback_active = True
        motor._cloud_fallback_reason = cloud_llm_client.CLOUD_ERROR_TRANSIENT

        result = motor.trigger_cloud_probe_now()

        assert result == {"armed": False, "reason": "no_cloud_profile"}
        assert motor._cloud_prober_thread is None

    def test_arms_immediately_for_ambiguous_429(self, tmp_path):
        """ambiguous_429 never gets an AUTOMATIC probe loop, but the manual
        trigger must still arm it -- and schedule it for essentially now,
        not some per-class wait."""
        motor, _ = _make_motor(tmp_path)
        motor._cloud_fallback_active = True
        motor._cloud_fallback_reason = cloud_llm_client.CLOUD_ERROR_AMBIGUOUS_429
        blocked = threading.Event()

        def fake_probe(_cfg):
            blocked.wait(timeout=2.0)
            return True

        motor._cloud_probe_once = MagicMock(side_effect=fake_probe)

        result = motor.trigger_cloud_probe_now()

        assert result == {"armed": True, "reason": None}
        assert _wait_until(lambda: motor._cloud_prober_thread is not None)
        assert motor._cloud_probe_next_at is not None
        assert motor._cloud_probe_next_at <= time.monotonic() + 0.5
        blocked.set()
        motor._stop_cloud_prober()

    def test_arms_for_bad_key(self, tmp_path):
        """bad_key never gets an AUTOMATIC probe loop either, but the manual
        trigger must still arm it and run the real success sequence."""
        motor, ui_events = _make_motor(tmp_path)
        motor._cloud_fallback_active = True
        motor._cloud_fallback_reason = cloud_llm_client.CLOUD_ERROR_BAD_KEY
        motor._cloud_probe_once = MagicMock(return_value=True)

        result = motor.trigger_cloud_probe_now()

        assert result == {"armed": True, "reason": None}
        assert _wait_until(lambda: "cloud_restored" in ui_events)
        assert motor._cloud_fallback_active is False

    def test_collapses_scheduled_probe_to_now(self, tmp_path):
        motor, _ = _make_motor(tmp_path)
        motor._handle_cloud_failure(
            "direct", failure_class=cloud_llm_client.CLOUD_ERROR_RATE_LIMITED, retry_after_seconds=900
        )
        assert _wait_until(lambda: motor._cloud_prober_thread is not None)
        old_thread = motor._cloud_prober_thread
        far_future = motor._cloud_probe_next_at
        blocked = threading.Event()

        def fake_probe(_cfg):
            blocked.wait(timeout=2.0)
            return True

        motor._cloud_probe_once = MagicMock(side_effect=fake_probe)

        result = motor.trigger_cloud_probe_now()

        assert result == {"armed": True, "reason": None}
        assert motor._cloud_prober_thread is not old_thread
        assert motor._cloud_probe_next_at is not None
        assert motor._cloud_probe_next_at < far_future
        assert motor._cloud_probe_next_at <= time.monotonic() + 0.5
        blocked.set()
        old_thread.join(timeout=2.0)
        motor._stop_cloud_prober()

    def test_reuses_real_success_sequence(self, tmp_path):
        motor, ui_events = _make_motor(tmp_path)
        motor._cloud_fallback_active = True
        motor._cloud_fallback_reason = cloud_llm_client.CLOUD_ERROR_AMBIGUOUS_429
        motor._cloud_probe_once = MagicMock(return_value=True)
        epoch_before = motor.provider_epoch

        result = motor.trigger_cloud_probe_now()

        assert result == {"armed": True, "reason": None}
        assert _wait_until(lambda: "cloud_restored" in ui_events)
        assert motor._cloud_fallback_active is False
        assert motor._cloud_fallback_reason is None
        assert motor.provider_epoch == epoch_before + 1

    def test_idempotent_double_call_leaves_one_live_thread(self, tmp_path, monkeypatch):
        monkeypatch.setattr("opencohost.core.llm_engine.CLOUD_PROBER_JOIN_TIMEOUT_SECONDS", 0.05)
        motor, _ = _make_motor(tmp_path)
        motor._cloud_fallback_active = True
        motor._cloud_fallback_reason = cloud_llm_client.CLOUD_ERROR_AMBIGUOUS_429
        blocked = threading.Event()

        def fake_probe(_cfg):
            blocked.wait(timeout=2.0)
            return True

        motor._cloud_probe_once = MagicMock(side_effect=fake_probe)

        motor.trigger_cloud_probe_now()
        assert _wait_until(lambda: motor._cloud_prober_thread is not None)
        first_thread = motor._cloud_prober_thread

        motor.trigger_cloud_probe_now()
        assert _wait_until(
            lambda: motor._cloud_prober_thread is not None
            and motor._cloud_prober_thread is not first_thread
        )
        second_thread = motor._cloud_prober_thread

        blocked.set()
        first_thread.join(timeout=2.0)
        second_thread.join(timeout=2.0)
        assert first_thread.is_alive() is False
        assert second_thread.is_alive() is False

    def test_bumps_generation_guard(self, tmp_path):
        motor, _ = _make_motor(tmp_path)
        motor._cloud_fallback_active = True
        motor._cloud_fallback_reason = cloud_llm_client.CLOUD_ERROR_TRANSIENT
        motor._cloud_probe_once = MagicMock(return_value=True)
        generation_before = motor._cloud_prober_generation

        motor.trigger_cloud_probe_now()

        assert motor._cloud_prober_generation > generation_before

    def test_never_touches_command_queue(self, tmp_path):
        motor, _ = _make_motor(tmp_path)
        motor._cloud_fallback_active = True
        motor._cloud_fallback_reason = cloud_llm_client.CLOUD_ERROR_TRANSIENT
        motor._cloud_probe_once = MagicMock(return_value=True)

        motor.trigger_cloud_probe_now()

        assert motor.command_queue.qsize() == 0


# ---------------------------------------------------------------------------
# 12. ambiguous_429 conservative auto-return (WU3, cloud_rearm_20260801,
#     owner decision D-A) -- a bare NIM 429 now ALSO arms the prober
#     automatically, with its own dedicated backoff and a give-up bound
#     (unlike rate_limited/transient, which retry forever).
# ---------------------------------------------------------------------------

class TestAmbiguous429ConservativeAuto:
    def test_no_probe_when_flag_off(self, tmp_path, monkeypatch):
        """Regression: byte-identical to pre-WU3 behavior when the flag is off."""
        monkeypatch.setattr(
            "opencohost.core.llm_engine.CLOUD_AUTO_RETURN_AMBIGUOUS_429_ENABLED", False
        )
        motor, _ = _make_motor(tmp_path)

        motor._handle_cloud_failure("direct", failure_class=cloud_llm_client.CLOUD_ERROR_AMBIGUOUS_429)
        time.sleep(0.05)

        assert motor._cloud_prober_thread is None
        assert motor._cloud_probe_next_at is None
        assert motor._cloud_fallback_reason == "ambiguous_429"

    def test_schedules_when_flag_on_base_120(self, tmp_path):
        motor, _ = _make_motor(tmp_path)
        wait = motor._initial_cloud_probe_wait(cloud_llm_client.CLOUD_ERROR_AMBIGUOUS_429, None)
        assert wait == settings.CLOUD_AUTO_RETURN_AMBIGUOUS_429_BASE_SECONDS

        blocked = threading.Event()

        def fake_probe(_cfg):
            blocked.wait(timeout=2.0)
            return True

        motor._cloud_probe_once = MagicMock(side_effect=fake_probe)

        motor._handle_cloud_failure("direct", failure_class=cloud_llm_client.CLOUD_ERROR_AMBIGUOUS_429)

        assert _wait_until(lambda: motor._cloud_prober_thread is not None)
        remaining = motor._cloud_probe_next_at - time.monotonic()
        assert 0 < remaining <= settings.CLOUD_AUTO_RETURN_AMBIGUOUS_429_BASE_SECONDS
        blocked.set()
        motor._stop_cloud_prober()

    def test_backoff_doubles_and_caps_independently(self, tmp_path):
        motor, _ = _make_motor(tmp_path)
        cap = settings.CLOUD_AUTO_RETURN_AMBIGUOUS_429_CAP_SECONDS
        doubled = motor._next_cloud_probe_wait(cloud_llm_client.CLOUD_ERROR_AMBIGUOUS_429, cap)
        assert doubled == cap  # ambiguous_429's own cap, never exceeded

        base = settings.CLOUD_AUTO_RETURN_AMBIGUOUS_429_BASE_SECONDS
        assert motor._next_cloud_probe_wait(cloud_llm_client.CLOUD_ERROR_AMBIGUOUS_429, base) == base * 2

        # rate_limited/transient constants and dispatch stay untouched.
        rl_cap = settings.CLOUD_AUTO_RETURN_RATE_LIMIT_CAP_SECONDS
        assert motor._next_cloud_probe_wait(cloud_llm_client.CLOUD_ERROR_RATE_LIMITED, rl_cap) == rl_cap
        t_cap = settings.CLOUD_AUTO_RETURN_TRANSIENT_CAP_SECONDS
        assert motor._next_cloud_probe_wait(cloud_llm_client.CLOUD_ERROR_TRANSIENT, t_cap) == t_cap

    def test_gives_up_after_max_attempts(self, tmp_path):
        motor, ui_events = _make_motor(tmp_path)
        motor._cloud_fallback_active = True
        motor._cloud_fallback_reason = cloud_llm_client.CLOUD_ERROR_AMBIGUOUS_429
        motor._cloud_probe_once = MagicMock(return_value=False)
        motor._next_cloud_probe_wait = MagicMock(return_value=0.001)
        stop_event = threading.Event()
        attempts = settings.CLOUD_AUTO_RETURN_AMBIGUOUS_429_MAX_ATTEMPTS

        motor._run_cloud_prober(
            stop_event,
            cloud_llm_client.CLOUD_ERROR_AMBIGUOUS_429,
            0.001,
            motor._cloud_prober_generation,
            attempts_left=attempts,
        )

        assert motor._cloud_probe_once.call_count == attempts
        assert "cloud_probe_gave_up" in ui_events
        assert motor._cloud_probe_next_at is None
        assert motor._cloud_fallback_active is True  # still local -- never returned
        assert motor._cloud_fallback_reason == "ambiguous_429"

        # The manual trigger (WU1) must still work after give-up.
        motor._cloud_probe_once = MagicMock(return_value=True)
        result = motor.trigger_cloud_probe_now()
        assert result == {"armed": True, "reason": None}
        assert _wait_until(lambda: "cloud_restored" in ui_events)

    def test_rate_limited_and_transient_keep_unbounded_retry(self, tmp_path):
        motor, ui_events = _make_motor(tmp_path)
        motor._cloud_fallback_active = True
        calls = {"n": 0}
        stop_event = threading.Event()

        def fake_probe(_cfg):
            calls["n"] += 1
            if calls["n"] >= 10:
                stop_event.set()  # stop deterministically -- proves it never gives up on its own
            return False

        motor._cloud_probe_once = MagicMock(side_effect=fake_probe)
        motor._next_cloud_probe_wait = MagicMock(return_value=0.001)

        motor._run_cloud_prober(
            stop_event, cloud_llm_client.CLOUD_ERROR_TRANSIENT, 0.001, motor._cloud_prober_generation
        )

        assert calls["n"] == 10  # ran well past ambiguous_429's max-attempts count
        assert "cloud_probe_gave_up" not in ui_events

    def test_gave_up_event_whitelisted_no_detail(self):
        assert "cloud_probe_gave_up" in engine_host._MOTOR_EVENT_WHITELIST
        assert "cloud_probe_gave_up" not in engine_host._MOTOR_EVENT_DETAIL_FIELDS


# ---------------------------------------------------------------------------
# 13. Manual probe failure policy (post-WU3 fix, cloud_rearm_20260801) --
#     `trigger_cloud_probe_now` arms with wait=0.0. On a FAILED probe,
#     `_next_cloud_probe_wait`'s `min(previous_wait * 2, cap)` is a fixed
#     point at 0 -- doubling zero stays zero forever, so a failing manual
#     probe would otherwise re-probe immediately in a tight loop (unbounded
#     classes) or burn through an attempts budget with zero spacing
#     (ambiguous_429 flag-on). Fix: a class WITHOUT an automatic policy
#     (bad_key always; ambiguous_429 flag-off) gets a ONE-SHOT manual
#     attempts budget (`_arm_cloud_prober`); a class WITH one seeds its
#     first reschedule from `_initial_cloud_probe_wait` instead of doubling
#     zero (`_run_cloud_prober`).
# ---------------------------------------------------------------------------

class TestManualProbeFailurePolicy:
    def test_manual_bad_key_failure_is_one_shot(self, tmp_path):
        motor, ui_events = _make_motor(tmp_path)
        motor._cloud_fallback_active = True
        motor._cloud_fallback_reason = cloud_llm_client.CLOUD_ERROR_BAD_KEY
        motor._cloud_probe_once = MagicMock(return_value=False)

        result = motor.trigger_cloud_probe_now()

        assert result == {"armed": True, "reason": None}
        assert _wait_until(lambda: "cloud_probe_gave_up" in ui_events)
        assert motor._cloud_probe_once.call_count == 1  # no re-probe loop
        assert motor._cloud_probe_next_at is None
        # Exactly one "scheduled" -- the initial manual arm itself
        # (_arm_cloud_prober always emits one) -- and never a second one
        # from a reschedule.
        assert ui_events.count("cloud_probe_scheduled") == 1
        assert motor._cloud_fallback_active is True
        assert motor._cloud_fallback_reason == "bad_key"

    def test_manual_ambiguous_429_failure_is_one_shot_when_flag_off(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "opencohost.core.llm_engine.CLOUD_AUTO_RETURN_AMBIGUOUS_429_ENABLED", False
        )
        motor, ui_events = _make_motor(tmp_path)
        motor._cloud_fallback_active = True
        motor._cloud_fallback_reason = cloud_llm_client.CLOUD_ERROR_AMBIGUOUS_429
        motor._cloud_probe_once = MagicMock(return_value=False)

        result = motor.trigger_cloud_probe_now()

        assert result == {"armed": True, "reason": None}
        assert _wait_until(lambda: "cloud_probe_gave_up" in ui_events)
        assert motor._cloud_probe_once.call_count == 1
        assert motor._cloud_probe_next_at is None
        # Exactly one "scheduled" -- the initial manual arm -- never a
        # second one from a reschedule.
        assert ui_events.count("cloud_probe_scheduled") == 1

    def test_manual_ambiguous_429_failure_seeds_from_class_policy_and_still_gives_up(self, tmp_path, monkeypatch):
        """Two phases: (1) the first reschedule after a failing manual probe
        must seed at the class's own base wait (120), not double a zero
        fixed point; (2) starting from a manual wait=0 must not change the
        total give-up budget -- still exactly MAX_ATTEMPTS probes."""
        motor, _ = _make_motor(tmp_path)
        motor._cloud_fallback_active = True
        stop_event = threading.Event()
        captured = {}

        def fake_notify(wait):
            captured["wait"] = wait
            stop_event.set()  # stop right after this reschedule -- no real sleep

        motor._cloud_probe_once = MagicMock(return_value=False)
        motor._notify_cloud_probe_scheduled = MagicMock(side_effect=fake_notify)

        motor._run_cloud_prober(
            stop_event,
            cloud_llm_client.CLOUD_ERROR_AMBIGUOUS_429,
            0.0,  # manual trigger's immediate wait
            motor._cloud_prober_generation,
            attempts_left=settings.CLOUD_AUTO_RETURN_AMBIGUOUS_429_MAX_ATTEMPTS,
        )

        assert captured["wait"] == settings.CLOUD_AUTO_RETURN_AMBIGUOUS_429_BASE_SECONDS

        monkeypatch.setattr(
            "opencohost.core.llm_engine.CLOUD_AUTO_RETURN_AMBIGUOUS_429_BASE_SECONDS", 0.001
        )
        motor2, ui_events2 = _make_motor(tmp_path)
        motor2._cloud_fallback_active = True
        motor2._cloud_fallback_reason = cloud_llm_client.CLOUD_ERROR_AMBIGUOUS_429
        motor2._cloud_probe_once = MagicMock(return_value=False)
        motor2._next_cloud_probe_wait = MagicMock(return_value=0.001)
        stop_event2 = threading.Event()
        attempts = settings.CLOUD_AUTO_RETURN_AMBIGUOUS_429_MAX_ATTEMPTS

        motor2._run_cloud_prober(
            stop_event2,
            cloud_llm_client.CLOUD_ERROR_AMBIGUOUS_429,
            0.0,
            motor2._cloud_prober_generation,
            attempts_left=attempts,
        )

        assert motor2._cloud_probe_once.call_count == attempts
        assert "cloud_probe_gave_up" in ui_events2
        assert motor2._cloud_probe_next_at is None

    def test_manual_transient_failure_seeds_from_class_policy_not_zero(self, tmp_path):
        motor, _ = _make_motor(tmp_path)
        motor._cloud_fallback_active = True
        stop_event = threading.Event()
        captured = {}

        def fake_notify(wait):
            captured["wait"] = wait
            stop_event.set()

        motor._cloud_probe_once = MagicMock(return_value=False)
        motor._notify_cloud_probe_scheduled = MagicMock(side_effect=fake_notify)

        motor._run_cloud_prober(
            stop_event, cloud_llm_client.CLOUD_ERROR_TRANSIENT, 0.0, motor._cloud_prober_generation
        )

        assert captured["wait"] == settings.CLOUD_AUTO_RETURN_TRANSIENT_BASE_SECONDS
        # attempts_left defaults to None (unbounded) -- never exhausted.

    def test_manual_rate_limited_failure_seeds_from_class_policy_not_zero(self, tmp_path):
        motor, _ = _make_motor(tmp_path)
        motor._cloud_fallback_active = True
        stop_event = threading.Event()
        captured = {}

        def fake_notify(wait):
            captured["wait"] = wait
            stop_event.set()

        motor._cloud_probe_once = MagicMock(return_value=False)
        motor._notify_cloud_probe_scheduled = MagicMock(side_effect=fake_notify)

        motor._run_cloud_prober(
            stop_event, cloud_llm_client.CLOUD_ERROR_RATE_LIMITED, 0.0, motor._cloud_prober_generation
        )

        assert captured["wait"] == settings.CLOUD_AUTO_RETURN_RATE_LIMIT_FLOOR_SECONDS
        # attempts_left defaults to None (unbounded) -- never exhausted.


# ---------------------------------------------------------------------------
# 14. Numeric-only text-log observability (runtime_findings forensics,
#     2026-08-01): the prober lifecycle previously emitted ONLY `ui_callback`
#     events -- the rotating text log (the "OpenCohost" logger, written via
#     `self._log`) never saw an arm or a give-up. Reconstructing a probe arc
#     required cross-referencing the API audit and the front feed. Privacy
#     gate (project law): class/enum/number fields ONLY -- never dialogue,
#     prompts, or keys.
# ---------------------------------------------------------------------------

class TestProberTextLogObservability:
    def test_auto_arm_logs_class_wait_and_attempts(self, tmp_path, caplog):
        motor, _ = _make_motor(tmp_path)

        with caplog.at_level(logging.INFO, logger="OpenCohost"):
            motor._arm_cloud_prober(cloud_llm_client.CLOUD_ERROR_AMBIGUOUS_429, 120.0)
        motor._stop_cloud_prober()

        arm_lines = [m for m in caplog.messages if "Cloud probe armed" in m]
        assert len(arm_lines) == 1
        line = arm_lines[0]
        assert "clase=ambiguous_429" in line
        assert "wait=120s" in line
        # ambiguous_429's automatic arm gets a bounded attempts budget (WU3).
        assert f"attempts_left={settings.CLOUD_AUTO_RETURN_AMBIGUOUS_429_MAX_ATTEMPTS}" in line

    def test_manual_trigger_arm_logs_wait_zero(self, tmp_path, caplog):
        motor, _ = _make_motor(tmp_path)
        motor._cloud_fallback_active = True
        motor._cloud_fallback_reason = cloud_llm_client.CLOUD_ERROR_BAD_KEY
        blocked = threading.Event()

        def fake_probe(_cfg):
            blocked.wait(timeout=2.0)
            return True

        motor._cloud_probe_once = MagicMock(side_effect=fake_probe)

        with caplog.at_level(logging.INFO, logger="OpenCohost"):
            motor.trigger_cloud_probe_now()
        blocked.set()
        motor._stop_cloud_prober()

        arm_lines = [m for m in caplog.messages if "Cloud probe armed" in m]
        assert len(arm_lines) == 1
        assert "wait=0s" in arm_lines[0]
        assert "clase=bad_key" in arm_lines[0]

    def test_give_up_logs_one_warning_line_with_class(self, tmp_path, caplog):
        motor, ui_events = _make_motor(tmp_path)
        motor._cloud_fallback_active = True
        motor._cloud_fallback_reason = cloud_llm_client.CLOUD_ERROR_AMBIGUOUS_429
        motor._cloud_probe_once = MagicMock(return_value=False)
        motor._next_cloud_probe_wait = MagicMock(return_value=0.001)
        stop_event = threading.Event()
        attempts = settings.CLOUD_AUTO_RETURN_AMBIGUOUS_429_MAX_ATTEMPTS

        with caplog.at_level(logging.WARNING, logger="OpenCohost"):
            motor._run_cloud_prober(
                stop_event,
                cloud_llm_client.CLOUD_ERROR_AMBIGUOUS_429,
                0.001,
                motor._cloud_prober_generation,
                attempts_left=attempts,
            )

        assert "cloud_probe_gave_up" in ui_events
        give_up_lines = [m for m in caplog.messages if "Cloud probe gave up" in m]
        assert give_up_lines == ["Motor: Cloud probe gave up: clase=ambiguous_429"]

    def test_log_lines_never_contain_dialogue_or_key_payload(self, tmp_path, caplog):
        """Negative assertion (privacy gate): every new line must match a
        strict class/number-only pattern -- proves no dialogue, prompt, or
        key text can ever ride along, even by future accident."""
        arm_pattern = re.compile(
            r"^Motor: Cloud probe armed: clase=[a-z_0-9]+ wait=\d+s "
            r"attempts_left=(unbounded|\d+)$"
        )
        give_up_pattern = re.compile(r"^Motor: Cloud probe gave up: clase=[a-z_0-9]+$")

        motor, _ = _make_motor(tmp_path)
        with caplog.at_level(logging.INFO, logger="OpenCohost"):
            motor._arm_cloud_prober(cloud_llm_client.CLOUD_ERROR_TRANSIENT, 30.0)
        motor._stop_cloud_prober()

        motor2, ui_events2 = _make_motor(tmp_path)
        motor2._cloud_fallback_active = True
        motor2._cloud_probe_once = MagicMock(return_value=False)
        motor2._next_cloud_probe_wait = MagicMock(return_value=0.001)
        stop_event = threading.Event()
        attempts = settings.CLOUD_AUTO_RETURN_AMBIGUOUS_429_MAX_ATTEMPTS
        with caplog.at_level(logging.WARNING, logger="OpenCohost"):
            motor2._run_cloud_prober(
                stop_event,
                cloud_llm_client.CLOUD_ERROR_AMBIGUOUS_429,
                0.001,
                motor2._cloud_prober_generation,
                attempts_left=attempts,
            )

        relevant = [m for m in caplog.messages if m.startswith("Motor: Cloud probe")]
        assert len(relevant) == 2  # sanity: both the arm and the give-up were captured
        for line in relevant:
            assert arm_pattern.match(line) or give_up_pattern.match(line)
