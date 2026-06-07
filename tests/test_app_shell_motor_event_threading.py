import threading

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
