import threading
import queue

from ui import app_shell


def test_motor_event_from_worker_thread_is_scheduled_before_handling():
    app = object.__new__(app_shell.VocalAIApp)
    calls = []

    app._safe_after = lambda func: calls.append(func)
    app._on_motor_processing = lambda: calls.append("handled")

    worker = threading.Thread(target=lambda: app._on_motor_event("processing"))
    worker.start()
    worker.join(timeout=2)

    assert calls and calls[0] != "handled"
    calls[0]()
    assert calls == [calls[0], "handled"]


def test_motor_event_from_main_thread_is_handled_immediately():
    app = object.__new__(app_shell.VocalAIApp)
    calls = []

    app._safe_after = lambda func: calls.append(func)
    app._on_motor_processing = lambda: calls.append("handled")

    app._on_motor_event("processing")

    assert calls == ["handled"]


def test_safe_after_from_worker_queues_until_main_thread_pump_runs():
    app = object.__new__(app_shell.VocalAIApp)
    app._ui_task_queue = queue.Queue()
    app._closing = False
    after_calls = []
    executed = []

    app.after = lambda delay_ms, func: after_calls.append((delay_ms, func))

    worker = threading.Thread(
        target=lambda: app._safe_after(lambda: executed.append("ran"))
    )
    worker.start()
    worker.join(timeout=2)

    assert after_calls == []
    assert executed == []

    app._process_ui_tasks()

    assert after_calls[0][0] == 0
    after_calls[0][1]()
    assert executed == ["ran"]
    assert after_calls[-1][0] == 50


def test_safe_after_from_worker_preserves_delay_until_main_thread_pump_runs():
    app = object.__new__(app_shell.VocalAIApp)
    app._ui_task_queue = queue.Queue()
    app._closing = False
    after_calls = []

    app.after = lambda delay_ms, func: after_calls.append((delay_ms, func))

    worker = threading.Thread(
        target=lambda: app._safe_after(lambda: None, delay_ms=700)
    )
    worker.start()
    worker.join(timeout=2)

    assert after_calls == []

    app._process_ui_tasks()

    assert after_calls[0][0] == 700
