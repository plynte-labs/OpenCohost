"""PTT dictation mode (ptt-dictation-20261111).

A released PTT hold either SENDS the words as a turn (today's behavior, mode
``send``) or DICTATES them into the main window's chat composer (``always``,
or ``foreground`` while the main window reports focus). On "dictate" the
backend skips the dispatch and emits the fixed literal ``dictated``; the
webview inserts its OWN copy of the transcript. Nothing about the words ever
reaches an event, a log line or a file.

Never a real socket, never a real sleep: sessions are fakes driven by hand and
the focus clock is injected.
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from opencohost.api.ptt_session import PttController, PttSession
from opencohost.config import settings

SENTINEL = "secret banana phrase whiskey tango"


# ──────────────────────────────────────────────────────────────────────────
# Settings store
# ──────────────────────────────────────────────────────────────────────────


def test_dictation_mode_defaults_to_send_when_file_absent(tmp_path):
    assert settings.load_ptt_dictation_mode(str(tmp_path / "ptt_settings.json")) == "send"


def test_dictation_mode_round_trips_and_merges(tmp_path):
    path = str(tmp_path / "ptt_settings.json")
    settings.save_ptt_hotkey("F8", path)
    settings.save_ptt_dictation_mode("foreground", path)
    assert settings.load_ptt_dictation_mode(path) == "foreground"
    # Merge: never clobbers the keys it does not own.
    assert settings.load_ptt_hotkey(path) == "F8"
    settings.save_ptt_ws_uri("ws://127.0.0.1:9999", path)
    assert settings.load_ptt_dictation_mode(path) == "foreground"


def test_companion_dictation_defaults_false_and_round_trips_and_merges(tmp_path):
    path = str(tmp_path / "ptt_settings.json")
    assert settings.load_ptt_companion_dictation(path) is False
    settings.save_ptt_dictation_mode("always", path)
    settings.save_ptt_companion_dictation(True, path)
    assert settings.load_ptt_companion_dictation(path) is True
    assert settings.load_ptt_dictation_mode(path) == "always"
    settings.save_ptt_dictation_mode("send", path)
    assert settings.load_ptt_companion_dictation(path) is True


def test_companion_dictation_ignores_a_hand_edited_non_boolean(tmp_path):
    path = tmp_path / "ptt_settings.json"
    path.write_text('{"companion_dictation": "yes"}', encoding="utf-8")
    assert settings.load_ptt_companion_dictation(str(path)) is False


def test_save_companion_dictation_rejects_non_boolean(tmp_path):
    with pytest.raises(ValueError):
        settings.save_ptt_companion_dictation("yes", str(tmp_path / "ptt_settings.json"))


def test_dictation_mode_ignores_a_hand_edited_unknown_value(tmp_path):
    path = tmp_path / "ptt_settings.json"
    path.write_text('{"dictation_mode": "telepathy"}', encoding="utf-8")
    assert settings.load_ptt_dictation_mode(str(path)) == "send"


def test_save_dictation_mode_rejects_unknown_value(tmp_path):
    with pytest.raises(ValueError):
        settings.save_ptt_dictation_mode("telepathy", str(tmp_path / "ptt_settings.json"))


# ──────────────────────────────────────────────────────────────────────────
# Controller decision
# ──────────────────────────────────────────────────────────────────────────


class _Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


class _RecordingDispatcher:
    def __init__(self):
        self.calls = []

    def dispatch(self, command, payload, **kwargs):
        self.calls.append((command, payload, kwargs))


class _HandSession:
    """Fake session driven by hand: the test decides when flush and close
    happen, so the release-time vs flush-time ordering is explicit."""

    def __init__(self, ws_uri, on_flush=None, on_event=None, on_close=None, on_release=None, **kwargs):
        self.on_flush = on_flush
        self.on_event = on_event
        self.on_close = on_close
        self.on_release = on_release
        self.session_id = f"ptt_hand_{id(self):x}"
        self.state = "idle"
        self.buffered_chars = 0
        self.last_error = None
        self.stops = 0

    def start(self):
        self.state = "listening"

    def keepalive(self):
        pass

    def stop(self):
        self.stops += 1
        if self.state == "listening":
            self.state = "flushing"
            if self.on_release is not None:
                self.on_release()

    def finish(self, text=SENTINEL):
        """Grace expired: flush, then close (PttSession._ws_main order)."""
        outcome = self.on_flush(text)
        self.state = "idle"
        self.on_close(None)
        return outcome


def _controller(**kwargs):
    created = []

    def factory(ws_uri, **kw):
        s = _HandSession(ws_uri, **kw)
        created.append(s)
        return s

    disp = _RecordingDispatcher()
    clock = kwargs.pop("focus_clock", None) or _Clock()
    releases = []
    controller = PttController(
        "ws://test/whisperlive",
        disp,
        MagicMock(),
        session_factory=factory,
        grace=0.1,
        focus_clock=clock,
        on_release=lambda: releases.append(1),
        **kwargs,
    )
    return controller, created, disp, clock, releases


def _hold(controller, created):
    controller.start()
    return created[-1]


def test_default_mode_is_send_and_dispatches_exactly_as_before():
    controller, created, disp, _clock, _rel = _controller()
    assert controller.dictation_mode == "send"
    controller.report_focus(True)
    session = _hold(controller, created)
    controller.stop(session.session_id)
    outcome = session.finish()
    assert outcome is None  # the session then emits the unchanged "flushed"
    assert len(disp.calls) == 1
    assert disp.calls[0][2]["source"] == "ptt"


def test_always_mode_skips_the_dispatch_and_reports_dictated():
    motor = MagicMock()
    motor.is_processing = True
    controller, created, disp, _clock, releases = _controller(dictation_mode="always", motor=motor)
    session = _hold(controller, created)
    controller.stop(session.session_id)
    assert session.finish() == "dictated"
    assert disp.calls == []
    # No turn was created, so nothing may be swept or drained for one.
    motor.drain_speech_for_new_turn.assert_not_called()
    motor._drain_pending_direct_into_priority_queue.assert_not_called()
    # The ADR-048 release contract is untouched: exactly once.
    assert releases == [1]


def test_foreground_with_fresh_focus_at_release_dictates():
    controller, created, disp, clock, _rel = _controller(dictation_mode="foreground")
    session = _hold(controller, created)
    controller.report_focus(True)
    clock.now += 1.0
    controller.stop(session.session_id)
    assert session.finish() == "dictated"
    assert disp.calls == []


def test_foreground_without_any_focus_report_sends():
    controller, created, disp, _clock, _rel = _controller(dictation_mode="foreground")
    session = _hold(controller, created)
    controller.stop(session.session_id)
    assert session.finish() is None
    assert len(disp.calls) == 1


def test_foreground_with_stale_focus_report_sends():
    controller, created, disp, clock, _rel = _controller(dictation_mode="foreground")
    controller.report_focus(True)
    clock.now += settings.PTT_FOCUS_STALE_S + 0.01
    session = _hold(controller, created)
    controller.stop(session.session_id)
    assert session.finish() is None
    assert len(disp.calls) == 1


def test_foreground_with_blurred_report_sends():
    controller, created, disp, _clock, _rel = _controller(dictation_mode="foreground")
    controller.report_focus(False)
    session = _hold(controller, created)
    controller.stop(session.session_id)
    assert session.finish() is None
    assert len(disp.calls) == 1


def test_press_focused_release_after_alt_tab_sends():
    controller, created, disp, clock, _rel = _controller(dictation_mode="foreground")
    controller.report_focus(True)
    session = _hold(controller, created)
    clock.now += 0.5
    controller.report_focus(False)  # alt-tab to the game mid-hold
    controller.stop(session.session_id)
    assert session.finish() is None
    assert len(disp.calls) == 1


def test_press_in_game_release_after_alt_tab_back_dictates():
    controller, created, disp, clock, _rel = _controller(dictation_mode="foreground")
    controller.report_focus(False)
    session = _hold(controller, created)
    clock.now += 0.5
    controller.report_focus(True)
    controller.stop(session.session_id)
    assert session.finish() == "dictated"
    assert disp.calls == []


def test_focus_change_during_grace_does_not_flip_the_release_decision():
    controller, created, disp, clock, _rel = _controller(dictation_mode="foreground")
    controller.report_focus(True)
    session = _hold(controller, created)
    controller.stop(session.session_id)
    controller.report_focus(False)  # blur after release, before the flush
    assert session.finish() == "dictated"

    controller.report_focus(False)
    session2 = _hold(controller, created)
    controller.stop(session2.session_id)
    controller.report_focus(True)  # focus after release, before the flush
    assert session2.finish() is None
    assert len(disp.calls) == 1


def test_a_retried_stop_keeps_the_first_release_decision():
    controller, created, disp, _clock, _rel = _controller(dictation_mode="foreground")
    controller.report_focus(True)
    session = _hold(controller, created)
    controller.stop(session.session_id)
    controller.report_focus(False)
    controller.stop(session.session_id)  # bridge retry after a lost 200
    assert session.finish() == "dictated"


def test_mode_changed_during_the_hold_applies_at_release():
    controller, created, disp, _clock, _rel = _controller()
    session = _hold(controller, created)
    controller.set_dictation_mode("always")
    controller.stop(session.session_id)
    assert session.finish() == "dictated"


def test_mode_changed_after_release_does_not_flip_the_decision():
    controller, created, disp, _clock, _rel = _controller(dictation_mode="always")
    session = _hold(controller, created)
    controller.stop(session.session_id)
    controller.set_dictation_mode("send")
    assert session.finish() == "dictated"


def test_exit_without_explicit_stop_decides_at_flush_time():
    # Keepalive starvation / WS drop never reach controller.stop(): the
    # decision is taken when the buffer is flushed.
    controller, created, disp, _clock, _rel = _controller(dictation_mode="foreground")
    session = _hold(controller, created)
    assert session.finish() is None
    assert len(disp.calls) == 1
    controller.report_focus(True)
    session2 = _hold(controller, created)
    assert session2.finish() == "dictated"


def test_release_decisions_do_not_leak_between_presses():
    controller, created, disp, _clock, _rel = _controller(dictation_mode="foreground")
    controller.report_focus(True)
    session = _hold(controller, created)
    controller.stop(session.session_id)
    assert session.finish() == "dictated"
    controller.report_focus(False)
    session2 = _hold(controller, created)
    assert session2.finish() is None  # no stale "dictate" from press 1


def test_a_companion_hold_counts_as_background_in_foreground_mode():
    controller, created, disp, _clock, _rel = _controller(dictation_mode="foreground")
    controller.report_focus(True)  # the companion never takes focus
    session = _hold(controller, created)
    assert controller.mark_hold_background(session.session_id) is True
    controller.stop(session.session_id)
    assert session.finish() is None
    assert len(disp.calls) == 1
    # The marker belongs to that press only.
    session2 = _hold(controller, created)
    controller.stop(session2.session_id)
    assert session2.finish() == "dictated"


def test_companion_dictation_defaults_to_false():
    controller, *_ = _controller()
    assert controller.companion_dictation is False


def test_a_companion_hold_sends_in_always_mode_when_the_switch_is_off():
    controller, created, disp, _clock, _rel = _controller(dictation_mode="always")
    session = _hold(controller, created)
    controller.mark_hold_background(session.session_id)
    controller.stop(session.session_id)
    assert session.finish() is None
    assert len(disp.calls) == 1


def test_a_companion_hold_dictates_in_always_mode_when_the_switch_is_on():
    controller, created, disp, _clock, _rel = _controller(
        dictation_mode="always", companion_dictation=True
    )
    session = _hold(controller, created)
    controller.mark_hold_background(session.session_id)
    controller.stop(session.session_id)
    assert session.finish() == "dictated"
    assert disp.calls == []


@pytest.mark.parametrize("switch", [False, True])
def test_a_companion_hold_sends_in_foreground_mode_in_both_switch_states(switch):
    controller, created, disp, _clock, _rel = _controller(
        dictation_mode="foreground", companion_dictation=switch
    )
    controller.report_focus(True)
    session = _hold(controller, created)
    controller.mark_hold_background(session.session_id)
    controller.stop(session.session_id)
    assert session.finish() is None
    assert len(disp.calls) == 1


@pytest.mark.parametrize("switch", [False, True])
def test_a_non_companion_hold_ignores_the_switch(switch):
    controller, created, disp, _clock, _rel = _controller(
        dictation_mode="always", companion_dictation=switch
    )
    session = _hold(controller, created)
    controller.stop(session.session_id)
    assert session.finish() == "dictated"


@pytest.mark.parametrize("switch", [False, True])
def test_send_mode_ignores_the_switch(switch):
    controller, created, disp, _clock, _rel = _controller(
        dictation_mode="send", companion_dictation=switch
    )
    session = _hold(controller, created)
    controller.mark_hold_background(session.session_id)
    controller.stop(session.session_id)
    assert session.finish() is None


def test_switch_changed_during_the_hold_applies_at_release():
    controller, created, disp, _clock, _rel = _controller(dictation_mode="always")
    session = _hold(controller, created)
    controller.mark_hold_background(session.session_id)
    controller.set_companion_dictation(True)
    controller.stop(session.session_id)
    assert session.finish() == "dictated"


def test_switch_changed_after_release_does_not_flip_the_decision():
    controller, created, disp, _clock, _rel = _controller(
        dictation_mode="always", companion_dictation=True
    )
    session = _hold(controller, created)
    controller.mark_hold_background(session.session_id)
    controller.stop(session.session_id)
    controller.set_companion_dictation(False)
    assert session.finish() == "dictated"


def test_set_companion_dictation_rejects_non_booleans():
    controller, *_ = _controller()
    with pytest.raises(ValueError):
        controller.set_companion_dictation("yes")
    assert controller.companion_dictation is False


def test_marking_an_unknown_session_background_is_a_no_op():
    controller, created, *_ = _controller(dictation_mode="foreground")
    assert controller.mark_hold_background("ptt_nope") is False


def test_set_dictation_mode_rejects_unknown_values():
    controller, *_ = _controller()
    with pytest.raises(ValueError):
        controller.set_dictation_mode("telepathy")
    assert controller.dictation_mode == "send"


def test_dictated_outcome_logs_literals_only(caplog):
    caplog.set_level(logging.DEBUG)
    controller, created, disp, _clock, _rel = _controller(dictation_mode="always")
    session = _hold(controller, created)
    controller.stop(session.session_id)
    session.finish()
    messages = [r.getMessage() for r in caplog.records]
    assert any("[PTT] dictated" in m for m in messages)
    for m in messages:
        assert SENTINEL not in m
        assert "banana" not in m


# ──────────────────────────────────────────────────────────────────────────
# Session outcome literal
# ──────────────────────────────────────────────────────────────────────────


def _session_with(on_flush):
    events = []
    session = PttSession("ws://test/whisperlive", on_flush=on_flush, on_event=events.append)
    return session, events


def test_session_emits_dictated_when_the_flush_callback_says_so():
    session, events = _session_with(lambda text: "dictated")
    session._buffer = SENTINEL
    session._flush_buffer()
    assert events == ["dictated"]


def test_session_emits_flushed_for_none_or_any_unknown_outcome():
    for result in (None, "whatever", 3):
        session, events = _session_with(lambda text, _r=result: _r)
        session._buffer = SENTINEL
        session._flush_buffer()
        assert events == ["flushed"]


def test_session_still_emits_empty_below_min_words_in_dictation():
    session, events = _session_with(lambda text: "dictated")
    session._buffer = "hola"
    session._flush_buffer()
    assert events == ["empty"]


# ──────────────────────────────────────────────────────────────────────────
# HTTP surface
# ──────────────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_host_active():
    import opencohost.api.main as main_mod

    main_mod._host_active = False
    yield
    main_mod._host_active = False


def _client():
    import opencohost.api.main as main_mod
    from tests.test_api_phase1 import FakeHost

    app = main_mod.create_app(host_factory=FakeHost, cors_origins=["http://localhost:5173"])
    client = TestClient(app)
    client.__enter__()
    return app, client


def test_focus_report_reaches_the_controller():
    app, client = _client()
    try:
        controller, _created, _disp, clock, _rel = _controller(dictation_mode="foreground")
        app.state.ptt_controller = controller
        resp = client.post("/api/ptt/focus", json={"focused": True})
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}
        assert controller.focus_is_fresh_and_focused() is True
        client.post("/api/ptt/focus", json={"focused": False})
        assert controller.focus_is_fresh_and_focused() is False
    finally:
        client.__exit__(None, None, None)


def test_focus_report_with_a_controller_that_cannot_take_it_is_harmless():
    app, client = _client()
    try:
        app.state.ptt_controller = object()
        resp = client.post("/api/ptt/focus", json={"focused": True})
        assert resp.status_code == 200
        assert resp.json() == {"ok": False}
    finally:
        client.__exit__(None, None, None)


def test_focus_report_requires_operator_token_when_enforced(monkeypatch):
    monkeypatch.setattr(settings, "API_AUTH_ENFORCED", True)
    app, client = _client()
    try:
        resp = client.post("/api/ptt/focus", json={"focused": True})
        assert resp.status_code == 401
    finally:
        client.__exit__(None, None, None)


def test_start_with_companion_origin_marks_the_hold_background():
    app, client = _client()
    try:
        controller, created, disp, _clock, _rel = _controller(dictation_mode="foreground")
        app.state.ptt_controller = controller
        controller.report_focus(True)
        resp = client.post("/api/ptt/start", json={"origin": "companion"})
        assert resp.status_code == 200
        sid = resp.json()["session_id"]
        client.post("/api/ptt/stop", json={"session_id": sid})
        assert created[-1].finish() is None
        assert len(disp.calls) == 1

        resp = client.post("/api/ptt/start", json={})
        sid = resp.json()["session_id"]
        client.post("/api/ptt/stop", json={"session_id": sid})
        assert created[-1].finish() == "dictated"
    finally:
        client.__exit__(None, None, None)


def test_put_config_dictation_mode_persists_live_applies_and_reads_back():
    app, client = _client()
    try:
        assert client.get("/api/ptt/state").json()["dictation_mode"] == "send"
        controller = PttController("ws://test/whisperlive", app.state.dispatcher, app.state.host.event_log)
        app.state.ptt_controller = controller
        resp = client.put("/api/ptt/config", json={"dictation_mode": "foreground"})
        assert resp.status_code == 200
        assert resp.json()["dictation_mode"] == "foreground"
        assert settings.load_ptt_dictation_mode() == "foreground"
        assert controller.dictation_mode == "foreground"
        assert client.get("/api/ptt/state").json()["dictation_mode"] == "foreground"
    finally:
        client.__exit__(None, None, None)


def test_put_config_rejects_unknown_dictation_mode_with_422():
    app, client = _client()
    try:
        resp = client.put("/api/ptt/config", json={"dictation_mode": "telepathy"})
        assert resp.status_code == 422
        assert settings.load_ptt_dictation_mode() == "send"
    finally:
        client.__exit__(None, None, None)


def test_put_config_companion_dictation_persists_live_applies_and_reads_back():
    app, client = _client()
    try:
        assert client.get("/api/ptt/state").json()["companion_dictation"] is False
        controller = PttController("ws://test/whisperlive", app.state.dispatcher, app.state.host.event_log)
        app.state.ptt_controller = controller
        resp = client.put("/api/ptt/config", json={"companion_dictation": True})
        assert resp.status_code == 200
        assert resp.json()["companion_dictation"] is True
        assert settings.load_ptt_companion_dictation() is True
        assert controller.companion_dictation is True
        assert client.get("/api/ptt/state").json()["companion_dictation"] is True
        resp = client.put("/api/ptt/config", json={})
        assert resp.json()["companion_dictation"] is True
    finally:
        client.__exit__(None, None, None)


def test_put_config_rejects_non_boolean_companion_dictation_with_422():
    app, client = _client()
    try:
        resp = client.put("/api/ptt/config", json={"companion_dictation": "maybe"})
        assert resp.status_code == 422
        assert settings.load_ptt_companion_dictation() is False
    finally:
        client.__exit__(None, None, None)


def test_persisted_companion_dictation_seeds_the_controller_at_boot():
    settings.save_ptt_companion_dictation(True)
    app, client = _client()
    try:
        assert app.state.ptt_controller.companion_dictation is True
        assert client.get("/api/ptt/state").json()["companion_dictation"] is True
    finally:
        client.__exit__(None, None, None)


def test_persisted_dictation_mode_seeds_the_controller_at_boot():
    settings.save_ptt_dictation_mode("always")
    app, client = _client()
    try:
        assert app.state.ptt_controller.dictation_mode == "always"
        assert client.get("/api/ptt/state").json()["dictation_mode"] == "always"
    finally:
        client.__exit__(None, None, None)
