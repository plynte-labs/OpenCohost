"""Tests for ui.protocols — Protocol classes and CallbackDispatcher."""

import logging
from typing import Protocol, runtime_checkable
from unittest.mock import MagicMock, call

import pytest

from ui.protocols import (
    CallbackDispatcher,
    MotorEventCallback,
    ProfileSaveCallback,
    SmartAggregatorCallbacks,
    StreamAdminCallbacks,
    create_dispatcher_for,
)


# ──────────────────────────────────────────────
# Protocol compliance tests
# ──────────────────────────────────────────────


class TestMotorEventCallback:
    def test_protocol_is_runtime_checkable(self):
        assert hasattr(MotorEventCallback, "_is_runtime_protocol")

    def test_compliant_callable_satisfies_protocol(self):
        def handler(status: str) -> None:
            pass

        assert isinstance(handler, MotorEventCallback)

    def test_lambda_satisfies_protocol(self):
        handler = lambda status: None
        assert isinstance(handler, MotorEventCallback)

    def test_method_satisfies_protocol(self):
        class UI:
            def on_motor_event(self, status: str) -> None:
                pass

        ui = UI()
        assert isinstance(ui.on_motor_event, MotorEventCallback)


class TestSmartAggregatorCallbacks:
    def test_protocol_is_runtime_checkable(self):
        assert hasattr(SmartAggregatorCallbacks, "_is_runtime_protocol")

    def test_compliant_class_satisfies_protocol(self):
        class CompliantHandler:
            def on_filtered_message(self, message: dict) -> None: ...
            def on_vibe_update(self, vibe: dict) -> None: ...
            def on_activity_trigger(self, data: dict) -> None: ...
            def on_aggregated_context(self, data: dict) -> None: ...
            def on_source_error(self, error: str) -> None: ...
            def on_source_connect(self, info: dict) -> None: ...
            def on_source_disconnect(self) -> None: ...

        assert isinstance(CompliantHandler(), SmartAggregatorCallbacks)

    def test_non_compliant_class_fails_protocol(self):
        class IncompleteHandler:
            def on_filtered_message(self, message: dict) -> None: ...

        assert not isinstance(IncompleteHandler(), SmartAggregatorCallbacks)


class TestStreamAdminCallbacks:
    def test_protocol_is_runtime_checkable(self):
        assert hasattr(StreamAdminCallbacks, "_is_runtime_protocol")

    def test_compliant_class_satisfies_protocol(self):
        class CompliantHandler:
            def on_log(self, msg: str) -> None: ...
            def on_state(self, state: dict) -> None: ...
            def on_metadata(self, metadata: dict) -> None: ...
            def on_pending_action(self, pending: dict) -> None: ...
            def on_analytics(self, snapshot: dict) -> None: ...

        assert isinstance(CompliantHandler(), StreamAdminCallbacks)


class TestProfileSaveCallback:
    def test_protocol_is_runtime_checkable(self):
        assert hasattr(ProfileSaveCallback, "_is_runtime_protocol")

    def test_compliant_callable_satisfies_protocol(self):
        def handler(nuevos_perfiles: dict) -> None:
            pass

        assert isinstance(handler, ProfileSaveCallback)


# ──────────────────────────────────────────────
# CallbackDispatcher tests
# ──────────────────────────────────────────────


class TestDispatcherBasic:
    def test_empty_dispatch_does_not_raise(self):
        disp = CallbackDispatcher("test")
        disp.dispatch("nonexistent_event")

    def test_subscribe_and_dispatch(self):
        disp = CallbackDispatcher("test")
        results = []
        disp.subscribe("evt", lambda x: results.append(x))
        disp.dispatch("evt", 42)
        assert results == [42]

    def test_dispatch_with_kwargs(self):
        disp = CallbackDispatcher("test")
        results = []
        disp.subscribe("evt", lambda **kw: results.append(kw))
        disp.dispatch("evt", a=1, b=2)
        assert results == [{"a": 1, "b": 2}]

    def test_has_subscribers(self):
        disp = CallbackDispatcher("test")
        assert not disp.has_subscribers("evt")
        disp.subscribe("evt", lambda: None)
        assert disp.has_subscribers("evt")

    def test_subscriber_count(self):
        disp = CallbackDispatcher("test")
        assert disp.subscriber_count("evt") == 0
        disp.subscribe("evt", lambda: None)
        disp.subscribe("evt", lambda: None)
        assert disp.subscriber_count("evt") == 2

    def test_clear(self):
        disp = CallbackDispatcher("test")
        disp.subscribe("a", lambda: None)
        disp.subscribe("b", lambda: None)
        disp.clear()
        assert disp.subscriber_count("a") == 0
        assert disp.subscriber_count("b") == 0


class TestDispatcherErrorHandling:
    def test_failing_callback_does_not_break_others(self, caplog):
        disp = CallbackDispatcher("test")
        results = []

        def good_cb(x):
            results.append(x)

        def bad_cb(x):
            raise RuntimeError("boom")

        disp.subscribe("evt", good_cb)
        disp.subscribe("evt", bad_cb)
        disp.subscribe("evt", good_cb)

        with caplog.at_level(logging.ERROR):
            disp.dispatch("evt", 99)

        assert results == [99, 99]
        assert any("boom" in r.message for r in caplog.records)

    def test_error_is_logged_with_exc_info(self, caplog):
        disp = CallbackDispatcher("my_module")

        def bad_cb():
            raise ValueError("test error")

        disp.subscribe("evt", bad_cb)

        with caplog.at_level(logging.ERROR):
            disp.dispatch("evt")

        matching = [
            r for r in caplog.records
            if r.levelno == logging.ERROR
            and "my_module" in r.message
            and "test error" in r.message
        ]
        assert len(matching) == 1

    def test_multiple_failures_all_logged(self, caplog):
        disp = CallbackDispatcher("test")
        disp.subscribe("evt", lambda: (_ for _ in ()).throw(ValueError("err1")))
        disp.subscribe("evt", lambda: (_ for _ in ()).throw(ValueError("err2")))

        with caplog.at_level(logging.ERROR):
            disp.dispatch("evt")

        messages = [r.message for r in caplog.records]
        assert any("err1" in m for m in messages)
        assert any("err2" in m for m in messages)


class TestDispatcherMultipleSubscribers:
    def test_all_subscribers_called(self):
        disp = CallbackDispatcher("test")
        results = []
        disp.subscribe("evt", lambda: results.append(1))
        disp.subscribe("evt", lambda: results.append(2))
        disp.subscribe("evt", lambda: results.append(3))
        disp.dispatch("evt")
        assert results == [1, 2, 3]

    def test_subscribers_called_in_order(self):
        disp = CallbackDispatcher("test")
        order = []
        for i in range(5):
            disp.subscribe("evt", lambda i=i: order.append(i))
        disp.dispatch("evt")
        assert order == [0, 1, 2, 3, 4]

    def test_dispatch_copies_subscriber_list(self):
        """Dispatch should iterate a snapshot so that a callback can
        subscribe/unsubscribe mid-dispatch without RuntimeError."""
        disp = CallbackDispatcher("test")
        results = []

        def subscriber_a():
            results.append("a")

        def subscriber_b():
            results.append("b")
            disp.subscribe("evt", lambda: results.append("c"))

        disp.subscribe("evt", subscriber_a)
        disp.subscribe("evt", subscriber_b)
        disp.dispatch("evt")

        assert results == ["a", "b"]


class TestDispatcherUnsubscribe:
    def test_unsubscribe_removes_callback(self):
        disp = CallbackDispatcher("test")
        results = []
        cb = lambda: results.append(1)
        disp.subscribe("evt", cb)
        disp.dispatch("evt")
        assert results == [1]

        assert disp.unsubscribe("evt", cb) is True
        disp.dispatch("evt")
        assert results == [1]

    def test_unsubscribe_unknown_callback_returns_false(self):
        disp = CallbackDispatcher("test")
        assert disp.unsubscribe("evt", lambda: None) is False

    def test_unsubscribe_all(self):
        disp = CallbackDispatcher("test")
        disp.subscribe("evt", lambda: None)
        disp.subscribe("evt", lambda: None)
        count = disp.unsubscribe_all("evt")
        assert count == 2
        assert disp.subscriber_count("evt") == 0

    def test_unsubscribe_nonexistent_event(self):
        disp = CallbackDispatcher("test")
        count = disp.unsubscribe_all("nonexistent")
        assert count == 0


class TestDispatcherSignatureValidation:
    def test_valid_callback_passes_validation(self):
        disp = CallbackDispatcher("test")
        disp.set_protocol(SmartAggregatorCallbacks)
        disp.subscribe("on_filtered_message", lambda msg: None)

    def test_too_few_args_raises_type_error(self):
        disp = CallbackDispatcher("test")
        disp.set_protocol(SmartAggregatorCallbacks)

        with pytest.raises(TypeError, match="expects 0 args but protocol requires 1"):
            disp.subscribe("on_filtered_message", lambda: None)

    def test_correct_args_pass_validation(self):
        disp = CallbackDispatcher("test")
        disp.set_protocol(StreamAdminCallbacks)
        disp.subscribe("on_log", lambda msg: None)
        disp.subscribe("on_state", lambda state: None)
        disp.subscribe("on_metadata", lambda metadata: None)

    def test_no_protocol_skips_validation(self):
        disp = CallbackDispatcher("test")
        disp.subscribe("anything", lambda: None)

    def test_unknown_event_skips_validation(self):
        disp = CallbackDispatcher("test")
        disp.set_protocol(SmartAggregatorCallbacks)
        disp.subscribe("unknown_event", lambda: None)


class TestFactory:
    def test_create_dispatcher_for_without_protocol(self):
        disp = create_dispatcher_for("my_source")
        assert disp._source == "my_source"
        assert disp._protocol is None

    def test_create_dispatcher_for_with_protocol(self):
        disp = create_dispatcher_for("agg", SmartAggregatorCallbacks)
        assert disp._source == "agg"
        assert disp._protocol is SmartAggregatorCallbacks


class TestDispatcherIntegration:
    def test_full_workflow_with_error_isolation(self, caplog):
        """Simulate the Smart Aggregator pattern: multiple callbacks,
        one fails, others still fire, error is logged."""
        disp = CallbackDispatcher("SmartAggregator")
        disp.set_protocol(SmartAggregatorCallbacks)

        filtered_messages = []
        vibe_updates = []
        activity_triggers = []

        disp.subscribe("on_filtered_message", lambda m: filtered_messages.append(m))
        disp.subscribe("on_vibe_update", lambda v: vibe_updates.append(v))
        disp.subscribe(
            "on_activity_trigger",
            lambda d: (_ for _ in ()).throw(RuntimeError("trigger crash")),
        )
        disp.subscribe("on_activity_trigger", lambda d: activity_triggers.append(d))

        with caplog.at_level(logging.ERROR):
            disp.dispatch("on_filtered_message", {"user": "test", "text": "hello"})
            disp.dispatch("on_vibe_update", {"temperature": 72.0})
            disp.dispatch("on_activity_trigger", {"type": "spike"})

        assert filtered_messages == [{"user": "test", "text": "hello"}]
        assert vibe_updates == [{"temperature": 72.0}]
        assert activity_triggers == [{"type": "spike"}]
        assert any("trigger crash" in r.message for r in caplog.records)
