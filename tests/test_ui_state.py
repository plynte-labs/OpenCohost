"""Comprehensive tests for ui.state.UIState.

Covers:
- Basic get/set operations
- Thread safety (concurrent reads/writes)
- Observer pattern (subscribe, unsubscribe, notify)
- Batch update (single notification pass)
- State validation (invalid values raise ValueError)
- Concurrent observer notifications
- wait_for synchronization
- shutdown lifecycle
"""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock

import pytest

from opencohost.ui.state import (
    VALID_CHAT_STATUSES,
    VALID_MIC_STATUSES,
    VALID_MODEL_STATUSES,
    VALID_OLLAMA_STATES,
    VALID_PIPELINE_STATES,
    VALID_TTS_STATUSES,
    UIState,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def state():
    """Fresh UIState instance, shut down after each test."""
    s = UIState()
    yield s
    s.shutdown(timeout=2.0)


# ---------------------------------------------------------------------------
# 1. Basic get/set operations
# ---------------------------------------------------------------------------

class TestBasicGetSet:
    def test_initial_defaults(self, state):
        assert state.model_status == "loading"
        assert state.mic_status == "disconnected"
        assert state.tts_status == "idle"
        assert state.chat_status == "disconnected"
        assert state.pipeline_state == "idle"
        assert state.ollama_state == "checking"
        assert state.ws_connected is False
        assert state.ws_should_reconnect is False
        assert state.smart_agg_connected is False
        assert state.smart_agg_connecting is False
        assert state.stream_admin_chat_connected is False
        assert state.current_model == ""
        assert state.current_profile == ""
        assert state.ptt_active is False
        assert state.advanced_mode is False

    def test_set_and_get_bool(self, state):
        state.ws_connected = True
        assert state.ws_connected is True

    def test_set_and_get_str(self, state):
        state.pipeline_state = "listening"
        assert state.pipeline_state == "listening"

    def test_generic_get(self, state):
        assert state.get("ws_connected") is False
        assert state.get("nonexistent", "fallback") == "fallback"

    def test_generic_set(self, state):
        state.set("custom_key", 42)
        assert state.get("custom_key") == 42

    def test_snapshot_returns_copy(self, state):
        state.ws_connected = True
        snap = state.snapshot()
        assert snap["ws_connected"] is True
        # Mutating the snapshot must not affect the real state
        snap["ws_connected"] = False
        assert state.ws_connected is True

    def test_snapshot_is_independent(self, state):
        snap1 = state.snapshot()
        state.ws_connected = True
        snap2 = state.snapshot()
        assert snap1["ws_connected"] is False
        assert snap2["ws_connected"] is True


# ---------------------------------------------------------------------------
# 2. Thread safety
# ---------------------------------------------------------------------------

class TestThreadSafety:
    def test_concurrent_writes_no_crash(self, state):
        """Multiple threads writing simultaneously must not raise."""
        errors: list[Exception] = []

        def writer(thread_id):
            try:
                for i in range(100):
                    state.ws_connected = (i % 2 == 0)
                    state.pipeline_state = "idle" if i % 2 == 0 else "listening"
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=writer, args=(t,)) for t in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert not errors, f"Errors during concurrent writes: {errors}"

    def test_concurrent_reads_consistent(self, state):
        """Readers must always see a valid value, never a partial write."""
        state.pipeline_state = "idle"
        seen_values: set[str] = set()
        stop_event = threading.Event()

        def writer():
            cycle = list(VALID_PIPELINE_STATES)
            i = 0
            while not stop_event.is_set():
                state.pipeline_state = cycle[i % len(cycle)]
                i += 1

        def reader():
            while not stop_event.is_set():
                val = state.pipeline_state
                assert val in VALID_PIPELINE_STATES
                seen_values.add(val)

        w = threading.Thread(target=writer)
        readers = [threading.Thread(target=reader) for _ in range(4)]
        w.start()
        for r in readers:
            r.start()
        time.sleep(0.5)
        stop_event.set()
        w.join(timeout=5)
        for r in readers:
            r.join(timeout=5)

        assert len(seen_values) > 1, "Reader should have seen multiple states"

    def test_concurrent_mixed_reads_writes(self, state):
        """Interleaved reads and writes across many threads."""
        errors: list[Exception] = []

        def worker(thread_id):
            try:
                for i in range(50):
                    if thread_id % 2 == 0:
                        state.ws_connected = True
                        state.smart_agg_connected = True
                    else:
                        _ = state.ws_connected
                        _ = state.smart_agg_connected
                        _ = state.snapshot()
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert not errors


# ---------------------------------------------------------------------------
# 3. Observer notifications
# ---------------------------------------------------------------------------

class TestObserverPattern:
    def test_subscribe_returns_id(self, state):
        cb = MagicMock()
        sid = state.subscribe(cb)
        assert isinstance(sid, int)

    def test_observer_notified_on_set(self, state):
        calls: list[tuple[str, Any]] = []
        state.subscribe(lambda k, v: calls.append((k, v)))
        state.ws_connected = True
        time.sleep(0.1)  # allow dispatch thread to process
        assert ("ws_connected", True) in calls

    def test_observer_notified_on_property_setter(self, state):
        calls: list[tuple[str, Any]] = []
        state.subscribe(lambda k, v: calls.append((k, v)))
        state.pipeline_state = "processing"
        time.sleep(0.1)
        assert ("pipeline_state", "processing") in calls

    def test_unsubscribe_stops_notifications(self, state):
        calls: list[tuple[str, Any]] = []
        sid = state.subscribe(lambda k, v: calls.append((k, v)))
        state.ws_connected = True
        time.sleep(0.1)
        assert len(calls) >= 1

        state.unsubscribe(sid)
        calls.clear()
        state.ws_connected = False
        time.sleep(0.1)
        assert len(calls) == 0

    def test_multiple_observers(self, state):
        calls_a: list[tuple[str, Any]] = []
        calls_b: list[tuple[str, Any]] = []
        state.subscribe(lambda k, v: calls_a.append((k, v)))
        state.subscribe(lambda k, v: calls_b.append((k, v)))

        state.tts_status = "generating"
        time.sleep(0.1)

        assert ("tts_status", "generating") in calls_a
        assert ("tts_status", "generating") in calls_b

    def test_unsubscribe_unknown_id_no_error(self, state):
        state.unsubscribe(99999)  # should not raise


# ---------------------------------------------------------------------------
# 4. Batch update
# ---------------------------------------------------------------------------

class TestBatchUpdate:
    def test_update_multiple_fields(self, state):
        state.update(
            ws_connected=True,
            pipeline_state="listening",
            mic_status="listening",
        )
        assert state.ws_connected is True
        assert state.pipeline_state == "listening"
        assert state.mic_status == "listening"

    def test_update_notifies_all_changed_keys(self, state):
        calls: list[tuple[str, Any]] = []
        state.subscribe(lambda k, v: calls.append((k, v)))

        state.update(ws_connected=True, pipeline_state="speaking")
        time.sleep(0.15)

        keys = [k for k, _ in calls]
        assert "ws_connected" in keys
        assert "pipeline_state" in keys

    def test_update_is_atomic(self, state):
        """A snapshot taken during update should see all or none of the changes."""
        snapshots: list[dict] = []
        stop = threading.Event()

        def updater():
            for i in range(50):
                state.update(
                    ws_connected=True,
                    pipeline_state="listening",
                    mic_status="listening",
                )
                state.update(
                    ws_connected=False,
                    pipeline_state="idle",
                    mic_status="idle",
                )

        def snapper():
            while not stop.is_set():
                snapshots.append(state.snapshot())

        u = threading.Thread(target=updater)
        s = threading.Thread(target=snapper)
        u.start()
        s.start()
        time.sleep(0.3)
        stop.set()
        u.join(timeout=5)
        s.join(timeout=5)

        # Every snapshot must have internally consistent values
        for snap in snapshots:
            if snap["ws_connected"]:
                assert snap["pipeline_state"] == "listening"
                assert snap["mic_status"] == "listening"
            else:
                assert snap["pipeline_state"] == "idle"
                assert snap["mic_status"] == "idle"


# ---------------------------------------------------------------------------
# 5. State validation
# ---------------------------------------------------------------------------

class TestValidation:
    @pytest.mark.parametrize("value", sorted(VALID_MODEL_STATUSES))
    def test_valid_model_status(self, state, value):
        state.model_status = value
        assert state.model_status == value

    def test_invalid_model_status_raises(self, state):
        with pytest.raises(ValueError, match="Invalid model_status"):
            state.model_status = "invalid_value"

    @pytest.mark.parametrize("value", sorted(VALID_MIC_STATUSES))
    def test_valid_mic_status(self, state, value):
        state.mic_status = value
        assert state.mic_status == value

    def test_invalid_mic_status_raises(self, state):
        with pytest.raises(ValueError, match="Invalid mic_status"):
            state.mic_status = "broken"

    @pytest.mark.parametrize("value", sorted(VALID_TTS_STATUSES))
    def test_valid_tts_status(self, state, value):
        state.tts_status = value
        assert state.tts_status == value

    def test_invalid_tts_status_raises(self, state):
        with pytest.raises(ValueError, match="Invalid tts_status"):
            state.tts_status = "exploding"

    @pytest.mark.parametrize("value", sorted(VALID_CHAT_STATUSES))
    def test_valid_chat_status(self, state, value):
        state.chat_status = value
        assert state.chat_status == value

    def test_invalid_chat_status_raises(self, state):
        with pytest.raises(ValueError, match="Invalid chat_status"):
            state.chat_status = "telepathic"

    @pytest.mark.parametrize("value", sorted(VALID_PIPELINE_STATES))
    def test_valid_pipeline_state(self, state, value):
        state.pipeline_state = value
        assert state.pipeline_state == value

    def test_invalid_pipeline_state_raises(self, state):
        with pytest.raises(ValueError, match="Invalid pipeline_state"):
            state.pipeline_state = "dancing"

    @pytest.mark.parametrize("value", sorted(VALID_OLLAMA_STATES))
    def test_valid_ollama_state(self, state, value):
        state.ollama_state = value
        assert state.ollama_state == value

    def test_invalid_ollama_state_raises(self, state):
        with pytest.raises(ValueError, match="Invalid ollama_state"):
            state.ollama_state = "confused"

    def test_bool_fields_accept_any_value(self, state):
        """Bool fields should not validate – they accept any truthy value."""
        state.ws_connected = True
        assert state.ws_connected is True
        state.ws_connected = False
        assert state.ws_connected is False


# ---------------------------------------------------------------------------
# 6. Concurrent observer notifications
# ---------------------------------------------------------------------------

class TestConcurrentObservers:
    def test_rapid_updates_dont_lose_notifications(self, state):
        """Fire many updates quickly; all keys should appear in notifications."""
        received: list[str] = []
        state.subscribe(lambda k, v: received.append(k))

        for i in range(30):
            state.pipeline_state = "idle" if i % 2 == 0 else "listening"

        time.sleep(0.3)
        assert len(received) == 30

    def test_observer_error_does_not_crash_dispatch(self, state):
        """An observer that raises must not prevent other observers from running."""
        good_calls: list[tuple[str, Any]] = []

        def bad_observer(k, v):
            raise RuntimeError("boom")

        def good_observer(k, v):
            good_calls.append((k, v))

        state.subscribe(bad_observer)
        state.subscribe(good_observer)

        state.ws_connected = True
        time.sleep(0.15)

        assert ("ws_connected", True) in good_calls

    def test_observer_does_not_block_setter(self, state):
        """A slow observer must not block the setter thread."""
        barrier = threading.Barrier(2, timeout=5)

        def slow_observer(k, v):
            barrier.wait()  # will block until main thread also waits
            time.sleep(0.5)

        state.subscribe(slow_observer)

        setter_done = threading.Event()

        def setter():
            state.ws_connected = True
            setter_done.set()

        t = threading.Thread(target=setter)
        t.start()
        # Setter should complete quickly even though observer is slow
        setter_done.wait(timeout=1.0)
        assert setter_done.is_set()
        t.join(timeout=5)


# ---------------------------------------------------------------------------
# 7. wait_for synchronization
# ---------------------------------------------------------------------------

class TestWaitFor:
    def test_wait_for_met(self, state):
        def delayed_set():
            time.sleep(0.1)
            state.pipeline_state = "speaking"

        threading.Thread(target=delayed_set, daemon=True).start()
        result = state.wait_for("pipeline_state", "speaking", timeout=5.0)
        assert result is True

    def test_wait_for_timeout(self, state):
        result = state.wait_for("pipeline_state", "speaking", timeout=0.1)
        assert result is False

    def test_wait_for_already_true(self, state):
        state.ws_connected = True
        time.sleep(0.1)
        result = state.wait_for("ws_connected", True, timeout=1.0)
        assert result is True


# ---------------------------------------------------------------------------
# 8. Shutdown lifecycle
# ---------------------------------------------------------------------------

class TestShutdown:
    def test_shutdown_stops_dispatch(self, state):
        calls: list[tuple[str, Any]] = []
        state.subscribe(lambda k, v: calls.append((k, v)))
        state.shutdown(timeout=2.0)

        state.ws_connected = True
        time.sleep(0.15)
        # After shutdown, dispatch thread is stopped so no callbacks
        assert len(calls) == 0

    def test_shutdown_is_idempotent(self, state):
        state.shutdown(timeout=1.0)
        state.shutdown(timeout=1.0)  # should not raise
