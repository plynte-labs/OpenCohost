"""runtime-fixes-20261110 T2 (A): the origin of a typed turn.

``desktop`` is set only by ``POST /api/chat/turn``; ``remote`` only by the
gateway's ``POST /turn``. It rides next to ``source="direct"`` (never replaces
it), is never read from the request, and a missing or unknown value is not
desktop.
"""
from __future__ import annotations

import queue
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from opencohost.api.dispatch import Dispatcher
from opencohost.core import turn_origin
from opencohost.core.llm_engine import MotorVocalIA
from opencohost.core.scheduling.turn_stamp import TurnStamp
from tests.test_api_phase1 import FakeHost

_ORIGINS = ["http://localhost:5173"]


# --- the value ------------------------------------------------------------------------


def test_only_the_exact_desktop_value_is_desktop():
    assert turn_origin.is_desktop("desktop") is True
    for value in (None, "", "remote", "Desktop", " desktop", "DESKTOP", "phone", 1, True, ["desktop"]):
        assert turn_origin.is_desktop(value) is False


def test_normalize_is_closed():
    assert turn_origin.normalize("desktop") == turn_origin.DESKTOP
    assert turn_origin.normalize("remote") == turn_origin.REMOTE
    assert turn_origin.normalize("anything else") is None
    assert turn_origin.normalize(None) is None


# --- dispatcher tuple -----------------------------------------------------------------


def test_dispatch_without_origin_keeps_every_tuple_shape():
    q = queue.Queue()
    d = Dispatcher(q)
    d.dispatch("process_context", "hi", None, history_text="h", source="direct", submitted_at=1.0,
               submitted_under_provider="local")
    assert q.get_nowait() == ("process_context", "hi", "h", "direct", 1.0, "local")


def test_dispatch_with_origin_appends_it_last():
    q = queue.Queue()
    d = Dispatcher(q)
    d.dispatch("process_context", "hi", None, history_text="h", source="direct", submitted_at=1.0,
               submitted_under_provider="local", origin="desktop")
    assert q.get_nowait() == ("process_context", "hi", "h", "direct", 1.0, "local", "desktop")


# --- routes set it, clients cannot ----------------------------------------------------


def _app():
    import opencohost.api.main as main_mod

    main_mod._host_active = False
    return main_mod.create_app(host_factory=FakeHost, cors_origins=_ORIGINS)


def test_desktop_route_stamps_desktop_and_ignores_a_spoofed_value():
    app = _app()
    with TestClient(app) as client:
        resp = client.post(
            "/api/chat/turn",
            json={"text": "hola", "origin": "remote"},
            headers={"X-Turn-Origin": "remote", "X-Origin": "remote"},
        )
        assert resp.status_code == 200
        queued = app.state.host.motor.command_queue.get_nowait()
        assert queued[3] == "direct"
        assert queued[6] == "desktop"
    import opencohost.api.main as main_mod

    main_mod._host_active = False


def test_gateway_route_stamps_remote_whatever_the_client_claims():
    from tests.test_remote_gateway_turns import FakeDispatcher, Rig

    rig = Rig()
    _, headers = rig.pair()
    headers = {**headers, "X-Turn-Origin": "desktop", "X-Origin": "desktop"}
    resp = rig.client.post(
        "/turn?origin=desktop", json={"text": "hola", "origin": "desktop", "source": "direct"}, headers=headers
    )
    assert resp.status_code == 200
    (_, _, _, kwargs) = rig.dispatcher.calls[0]
    assert kwargs["origin"] == "remote"
    assert kwargs["source"] == "direct"


def test_submit_direct_turn_default_is_not_desktop():
    from opencohost.api.direct_turn import submit_direct_turn

    calls = []

    class D:
        def dispatch(self, *a, **kw):
            calls.append(kw)
            return SimpleNamespace(state="accepted", command_id="c")

    host = SimpleNamespace(motor=SimpleNamespace(provider_runtime_state=lambda: {"provider": "local"},
                                                 is_processing=False, is_speaking=False))
    import opencohost.core.engine.llm_readiness as rd

    orig = rd.resolve_llm_readiness
    rd.resolve_llm_readiness = lambda h: SimpleNamespace(can_chat=True, state="ready")
    try:
        submit_direct_turn(host, D(), "x")
    finally:
        rd.resolve_llm_readiness = orig
    assert turn_origin.is_desktop(calls[0].get("origin")) is False


# --- the engine carries it ------------------------------------------------------------


def _motor():
    motor = MotorVocalIA(queue.Queue(), lambda event: None)
    motor.is_ready = True
    motor._ejecutar_inferencia = MagicMock()
    return motor


def test_turn_stamp_origin_defaults_to_none():
    assert TurnStamp(submitted_at=1.0).origin is None
    assert TurnStamp(submitted_at=1.0, origin="desktop").origin == "desktop"


def test_idle_command_reaches_the_inference_with_its_origin():
    motor = _motor()
    motor._consume_command(("process_context", "hi", "h", "direct", 5.0, "local", "desktop"))
    stamp = motor._ejecutar_inferencia.call_args.kwargs["stamp"]
    assert stamp.origin == "desktop"


def test_short_command_has_no_origin():
    motor = _motor()
    motor._consume_command(("process_context", "hi", "h", "direct", 5.0, "local"))
    assert motor._ejecutar_inferencia.call_args.kwargs["stamp"].origin is None


def test_busy_enqueue_and_pop_keep_the_origin():
    motor = _motor()
    motor._processing = True
    motor._consume_command(("process_context", "hi", "h", "direct", 5.0, "local", "desktop"))
    (item,) = motor._turn_scheduler.snapshot()
    assert item[7] == "desktop"
    motor._processing = False
    motor._take_pregen_if_match = lambda *a, **k: None
    motor._wait_or_invalidate_pregen = lambda *a, **k: None
    motor._process_priority_queue()
    assert motor._ejecutar_inferencia.call_args.kwargs["stamp"].origin == "desktop"


def test_enqueue_without_origin_keeps_the_seven_element_item():
    motor = _motor()
    motor.enqueue("hi", source="direct", submitted_at=1.0, submitted_under_provider="local")
    (item,) = motor._turn_scheduler.snapshot()
    assert len(item) == 7


def test_boundary_drain_keeps_the_origin():
    motor = _motor()
    motor.command_queue.put(("process_context", "hi", "h", "direct", 5.0, "local", "desktop"))
    motor._drain_pending_direct_into_priority_queue()
    (item,) = motor._turn_scheduler.snapshot()
    assert item[7] == "desktop"


def test_inference_hands_the_stamp_origin_to_generation():
    motor = MotorVocalIA(queue.Queue(), lambda event: None)
    seen = {}

    def fake_generate(contexto, source="direct", **kw):
        seen.update(kw)
        return ""

    motor._generar_dialogo = fake_generate
    motor._ejecutar_inferencia("hi", source="direct", stamp=TurnStamp(submitted_at=1.0, origin="desktop"))
    assert seen["origin"] == "desktop"
    seen.clear()
    motor._ejecutar_inferencia("hi", source="direct")
    assert seen.get("origin") is None


def test_pregen_is_refused_for_a_turn_that_carries_a_link(monkeypatch):
    motor = MotorVocalIA(queue.Queue(), lambda event: None)
    monkeypatch.setattr(MotorVocalIA, "_is_local", True)
    assert motor.pregenerate("mira https://example.com/a por favor", priority=1, source="direct") is False
