"""Tests for the opt-in `interrupt_on_direct` preference (ADR-069).

Owner decision 2026-11-21: with the switch ON, a typed (direct) message that
arrives while Kira speaks a NON-owner turn cuts that speech at arrival, like
PTT does at press, and the direct turn may pop and generate right away. OFF
(default) is today's behavior (ADR-044 D3-uniform: the cut happens when the
reply is submitted).

Covers:
1. Config load/save (settings + deps/main re-exports)
2. TTSConfigResponse wire model & GET /api/tts/config
3. POST /api/commands validation & dispatch
4. MotorVocalIA command handling + `cut_for_direct_arrival` gating
5. SpeechRouter arrival cut + arrival hold
6. Pop gate: a direct item pops while speech is busy only under the hold
"""
from __future__ import annotations

import json
import queue
import threading
import time
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from opencohost.api.models import TTSConfigResponse
from opencohost.core.speech.router import (
    PRIORITY_AGENDA,
    PRIORITY_CHAT,
    PRIORITY_OWNER,
    SpeechJob,
    SpeechJobState,
    SpeechRouter,
)
from tests.test_api_phase1 import FakeHost


# ── 1. Config ─────────────────────────────────────────────────────────────


def test_load_interrupt_on_direct_defaults_false_and_corrupt(tmp_path):
    from opencohost.config.settings import load_interrupt_on_direct

    assert load_interrupt_on_direct(str(tmp_path / "missing.json")) is False

    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("not json!@#", encoding="utf-8")
    assert load_interrupt_on_direct(str(corrupt)) is False

    non_dict = tmp_path / "non_dict.json"
    non_dict.write_text(json.dumps(["x"]), encoding="utf-8")
    assert load_interrupt_on_direct(str(non_dict)) is False

    explicit_true = tmp_path / "true.json"
    explicit_true.write_text(json.dumps({"interrupt_on_direct": True}), encoding="utf-8")
    assert load_interrupt_on_direct(str(explicit_true)) is True


def test_save_and_load_interrupt_on_direct_preserves_keys(tmp_path):
    from opencohost.config.settings import (
        load_interrupt_on_direct,
        save_interrupt_on_direct,
    )

    cfg = tmp_path / "tts_settings.json"
    cfg.write_text(json.dumps({"existing_key": "v", "discard_speech_residue": False}), encoding="utf-8")

    save_interrupt_on_direct(True, config_file=str(cfg))
    assert load_interrupt_on_direct(str(cfg)) is True
    data = json.loads(cfg.read_text(encoding="utf-8"))
    assert data["existing_key"] == "v"
    assert data["discard_speech_residue"] is False
    assert data["interrupt_on_direct"] is True

    save_interrupt_on_direct(False, config_file=str(cfg))
    assert load_interrupt_on_direct(str(cfg)) is False


def test_deps_and_main_re_export_interrupt_on_direct():
    from opencohost.api import deps
    from opencohost.api import main as main_mod

    for mod in (deps, main_mod):
        assert hasattr(mod, "load_interrupt_on_direct")
        assert hasattr(mod, "save_interrupt_on_direct")


# ── 2. Wire model / GET ───────────────────────────────────────────────────


def test_tts_config_response_has_field_default_false():
    kwargs = dict(piper_voice="es_MX", local_only=False, speed=1.0, engine="ligero", heavy_available=False)
    assert TTSConfigResponse(**kwargs).interrupt_on_direct is False
    assert TTSConfigResponse(**kwargs, interrupt_on_direct=True).interrupt_on_direct is True


def test_get_tts_config_endpoint_surfaces_flag(monkeypatch):
    import opencohost.api.main as main_mod

    flag = [True]
    monkeypatch.setattr(main_mod, "load_interrupt_on_direct", lambda: flag[0])
    app = main_mod.create_app(host_factory=FakeHost)
    with TestClient(app) as client:
        assert client.get("/api/tts/config").json()["interrupt_on_direct"] is True
        flag[0] = False
        assert client.get("/api/tts/config").json()["interrupt_on_direct"] is False


# ── 3. POST /api/commands ─────────────────────────────────────────────────


def test_api_commands_set_interrupt_on_direct_validation():
    import opencohost.api.main as main_mod

    app = main_mod.create_app(host_factory=FakeHost)
    with TestClient(app) as client:
        for bad in ["true", 1, 0, None, [], {}]:
            resp = client.post(
                "/api/commands",
                json={"command": "set_interrupt_on_direct", "payload": {"value": bad}},
            )
            assert resp.status_code == 422
            assert resp.json()["detail"] == "set_interrupt_on_direct requires a boolean value"
        resp = client.post(
            "/api/commands",
            json={"command": "set_interrupt_on_direct", "payload": {"value": True}},
        )
        assert resp.status_code == 200
        assert resp.json()["accepted"] is True


# ── 4. Motor ──────────────────────────────────────────────────────────────


def test_motor_dispatches_set_interrupt_on_direct(monkeypatch, tmp_path):
    from opencohost.core.llm_engine import MotorVocalIA
    import opencohost.config.settings as settings_mod

    cfg = str(tmp_path / "tts_settings.json")
    monkeypatch.setattr(settings_mod, "TTS_SETTINGS_FILE", cfg)

    motor = MotorVocalIA.__new__(MotorVocalIA)
    motor._lock = threading.Lock()
    motor._speaking = False
    motor._speech_router = None
    motor._deferred_control_commands = []
    motor._interrupt_on_direct = False

    motor._dispatch_command("set_interrupt_on_direct", True)
    assert motor._interrupt_on_direct is True
    assert settings_mod.load_interrupt_on_direct(cfg) is True
    motor._dispatch_command("set_interrupt_on_direct", False)
    assert motor._interrupt_on_direct is False
    assert settings_mod.load_interrupt_on_direct(cfg) is False


def test_command_is_drain_safe_and_not_speech_deferred():
    from opencohost.core import llm_engine

    assert "set_interrupt_on_direct" in llm_engine._DRAIN_SAFE_COMMANDS
    assert "set_interrupt_on_direct" not in llm_engine._SPEECH_DEFER_COMMANDS


# ── 5. Router ─────────────────────────────────────────────────────────────


class _FakeMotor:
    def __init__(self):
        self._lock = threading.Lock()
        self._speech_router_enabled = True
        self._speech_interrupt_enabled = True
        self._discard_speech_residue = False
        self.interrupted = 0

    def interrupt_speaking(self) -> None:
        self.interrupted += 1

    def _speech_cancelled(self, source: str) -> bool:
        return False

    def _speech_boundary_end(self) -> None:
        pass

    def ui_callback(self, status: str) -> None:
        pass


def _active(router, source, priority, **kw):
    job = SpeechJob(
        job_id=99, text="x", source=source, priority=priority,
        chunks=["a", "b"], state=SpeechJobState.ACTIVE, **kw,
    )
    router._active = job
    return job


def test_router_arrival_cut_preempts_agenda_and_holds_pick():
    motor = _FakeMotor()
    router = SpeechRouter(motor)
    victim = _active(router, "kira-agenda", PRIORITY_AGENDA)

    assert router.cut_for_direct_arrival() is True
    assert victim.pause_requested == "preempt"
    assert motor.interrupted == 1
    assert router.arrival_hold_active() is True

    # The victim unwinds onto the stack; the hold must keep it from resuming
    # while the direct reply is still generating.
    outcome = MagicMock(chunks=["a", "b"], spoken=[0], skipped=[], cursor=1, interrupted=True, error=False)
    state, _ = router._reconcile(victim, outcome=outcome, exc=None)
    assert state == SpeechJobState.SUSPENDED
    assert router._pick() is None


def test_router_owner_reply_submit_releases_hold_and_plays_first():
    motor = _FakeMotor()
    router = SpeechRouter(motor)
    victim = _active(router, "kira-agenda", PRIORITY_AGENDA)
    router.cut_for_direct_arrival(token="t1")
    victim.state = SpeechJobState.SUSPENDED
    router._active = None
    router._stack.append(victim)
    assert router._pick() is None

    motor._current_turn_tokens = {"t1"}  # the reply is produced by the causing turn
    reply = router.submit("reply", source="direct", priority=PRIORITY_OWNER)
    assert router.arrival_hold_active() is False
    assert router._pick() is reply


def test_router_arrival_cut_discards_non_agenda_victim_when_discard_on():
    motor = _FakeMotor()
    motor._discard_speech_residue = True
    router = SpeechRouter(motor)
    victim = _active(router, "chat", PRIORITY_CHAT)
    assert router.cut_for_direct_arrival() is True
    assert victim.pause_requested == "discard"


def test_router_arrival_cut_never_cuts_owner_speech():
    for src in ("direct", "ptt"):
        motor = _FakeMotor()
        router = SpeechRouter(motor)
        victim = _active(router, src, PRIORITY_OWNER)
        assert router.cut_for_direct_arrival() is False
        assert victim.pause_requested is None
        assert motor.interrupted == 0
        assert router.arrival_hold_active() is False


def test_router_arrival_cut_noop_when_nothing_active_or_unarmed():
    motor = _FakeMotor()
    router = SpeechRouter(motor)
    assert router.cut_for_direct_arrival() is False  # nothing active
    assert router.arrival_hold_active() is False

    victim = _active(router, "kira-agenda", PRIORITY_AGENDA)
    motor._speech_interrupt_enabled = False
    assert router.cut_for_direct_arrival() is False
    assert victim.pause_requested is None
    assert motor.interrupted == 0


def test_router_arrival_cut_respects_protected_shield():
    motor = _FakeMotor()  # discard off -> protected job shields every owner submit
    router = SpeechRouter(motor)
    victim = _active(router, "vision-read", PRIORITY_CHAT, protected=True)
    assert router.cut_for_direct_arrival() is False
    assert victim.pause_requested is None


def test_router_arrival_hold_expires_by_deadline():
    motor = _FakeMotor()
    router = SpeechRouter(motor)
    victim = _active(router, "kira-agenda", PRIORITY_AGENDA)
    assert router.cut_for_direct_arrival(max_hold_s=0.05) is True
    victim.state = SpeechJobState.SUSPENDED
    router._active = None
    router._stack.append(victim)
    assert router._pick() is None
    time.sleep(0.08)
    assert router.arrival_hold_active() is False
    assert router._pick() is victim


def test_router_release_arrival_hold_is_idempotent():
    motor = _FakeMotor()
    router = SpeechRouter(motor)
    _active(router, "kira-agenda", PRIORITY_AGENDA)
    router.cut_for_direct_arrival()
    router.release_arrival_hold("test")
    router.release_arrival_hold("test")
    assert router.arrival_hold_active() is False


# ── 4b/6. Motor gating + pop gate ─────────────────────────────────────────


def _bare_motor():
    from opencohost.core.llm_engine import MotorVocalIA

    motor = MotorVocalIA(queue.Queue(), lambda event: None)
    motor.current_model = "llama3"
    motor._simulated_readiness = True
    motor._reasoning_model_cache["llama3"] = False
    motor._model_ctx_limit = {"llama3": 8192}
    # Hermetic: enqueue may start an interactive pregen worker; it must never
    # reach a live Ollama or TTS (same convention as test_direct_turn_preemption).
    motor._generar_dialogo = lambda *args, **kwargs: "RESPUESTA"
    motor._hablar = MagicMock()
    # These tests drive the pop gate by hand; no background drafting.
    motor._maybe_trigger_interactive_pregen = lambda *args, **kwargs: None
    return motor


def _armed_motor(*, on: bool, armed: bool = True):
    motor = _bare_motor()
    motor._speech_router_enabled = armed
    motor._speech_interrupt_enabled = armed
    motor._interrupt_on_direct = on
    router = SpeechRouter(motor)  # never started: state is driven by hand
    motor._speech_router = router
    return motor, router


def test_motor_cut_for_direct_arrival_gates_on_switch_and_flags():
    # ON + armed + agenda speech -> cut
    motor, router = _armed_motor(on=True)
    victim = _active(router, "kira-agenda", PRIORITY_AGENDA)
    assert motor.cut_for_direct_arrival() is True
    assert victim.pause_requested == "preempt"

    # OFF -> today's behavior, no cut
    motor, router = _armed_motor(on=False)
    victim = _active(router, "kira-agenda", PRIORITY_AGENDA)
    assert motor.cut_for_direct_arrival() is False
    assert victim.pause_requested is None

    # ON but flags not armed (non-EngineHost) -> no-op
    motor, router = _armed_motor(on=True, armed=False)
    victim = _active(router, "kira-agenda", PRIORITY_AGENDA)
    assert motor.cut_for_direct_arrival() is False
    assert victim.pause_requested is None


def test_motor_cut_for_direct_arrival_owner_speech_is_not_cut():
    motor, router = _armed_motor(on=True)
    victim = _active(router, "direct", PRIORITY_OWNER)
    assert motor.cut_for_direct_arrival() is False
    assert victim.pause_requested is None


def test_direct_pops_while_speech_busy_only_under_arrival_hold():
    ran = []

    def _run(motor):
        motor._ejecutar_inferencia = lambda payload, source="", **kw: ran.append((payload, source))

    # ON + agenda speech: cut at arrival, then the direct item pops.
    motor, router = _armed_motor(on=True)
    _run(motor)
    victim = _active(router, "kira-agenda", PRIORITY_AGENDA)
    victim.state = SpeechJobState.SUSPENDED  # unwound after the cut
    motor.enqueue("pregunta", priority=1, source="direct")
    assert motor.cut_for_direct_arrival() is True
    router._active = None
    router._stack.append(victim)  # speech still "busy": victim on the stack
    assert motor._speech_active is True
    motor._process_priority_queue()
    assert ran == [("pregunta", "direct")]

    # OFF: speech busy -> direct waits (today's rule).
    ran.clear()
    motor, router = _armed_motor(on=False)
    _run(motor)
    victim = _active(router, "kira-agenda", PRIORITY_AGENDA)
    motor.enqueue("pregunta", priority=1, source="direct")
    assert motor.cut_for_direct_arrival() is False
    motor._process_priority_queue()
    assert ran == []

    # ON but owner speech active: no cut, so no widened pop.
    motor, router = _armed_motor(on=True)
    _run(motor)
    _active(router, "direct", PRIORITY_OWNER)
    motor.enqueue("pregunta", priority=1, source="direct")
    assert motor.cut_for_direct_arrival() is False
    motor._process_priority_queue()
    assert ran == []


def test_direct_turn_cuts_before_drain_with_the_dispatch_token():
    """The hook passes the turn's own submitted_at stamp as the hold token and
    cuts BEFORE the arrival drain (so the turn's own pregen cannot start first
    and then block the cut)."""
    from types import SimpleNamespace

    import opencohost.core.engine.llm_readiness as rd
    from opencohost.api.direct_turn import submit_direct_turn

    order, dispatch_kwargs, cut_args = [], [], []

    class D:
        def dispatch(self, *a, **kw):
            dispatch_kwargs.append(kw)
            return SimpleNamespace(state="accepted", command_id="c")

    motor = SimpleNamespace(
        provider_runtime_state=lambda: {"provider": "local"},
        is_processing=False,
        is_speaking=True,
        cut_for_direct_arrival=lambda token=None: (order.append("cut"), cut_args.append(token)),
        _drain_pending_direct_into_priority_queue=lambda: order.append("drain"),
    )
    orig = rd.resolve_llm_readiness
    rd.resolve_llm_readiness = lambda h: SimpleNamespace(can_chat=True, state="ready")
    try:
        submit_direct_turn(SimpleNamespace(motor=motor), D(), "x")
    finally:
        rd.resolve_llm_readiness = orig
    assert order == ["cut", "drain"]
    assert cut_args == [dispatch_kwargs[0]["submitted_at"]]


def test_replayed_direct_turn_never_cuts_or_holds():
    """Judgment Day 2026-11-23 CRITICAL: an idempotent replay enqueues nothing,
    so no turn carries its fresh stamp. Cutting on a replay would set a hold no
    turn can release, muting every job for ARRIVAL_HOLD_MAX_SECONDS. A replay
    must stay a pure receipt: no cut. The drain is harmless and may still run."""
    from types import SimpleNamespace

    import opencohost.core.engine.llm_readiness as rd
    from opencohost.api.direct_turn import submit_direct_turn

    cut_calls = []

    class D:
        def dispatch(self, *a, **kw):
            return SimpleNamespace(state="replay", command_id="c")

    motor = SimpleNamespace(
        provider_runtime_state=lambda: {"provider": "local"},
        is_processing=False,
        is_speaking=True,
        cut_for_direct_arrival=lambda token=None: cut_calls.append(token),
        _drain_pending_direct_into_priority_queue=lambda: None,
    )
    orig = rd.resolve_llm_readiness
    rd.resolve_llm_readiness = lambda h: SimpleNamespace(can_chat=True, state="ready")
    try:
        outcome = submit_direct_turn(SimpleNamespace(motor=motor), D(), "x")
    finally:
        rd.resolve_llm_readiness = orig
    assert cut_calls == []
    assert outcome.kind == "accepted"


# ── Correction round: M1 / M2 / M3 / L1 ───────────────────────────────────


def test_router_release_does_not_wake_when_nothing_was_held():  # M1
    router = SpeechRouter(_FakeMotor())
    router._wake.clear()
    router.release_arrival_hold("test")
    assert router._wake.is_set() is False


def test_off_path_never_touches_the_arrival_hold():  # M1
    motor, router = _armed_motor(on=False)
    spy = MagicMock()
    motor._speech_router = spy
    motor._current_processing_source = "direct"
    motor._complete_processing_cycle(process_queue=False)
    spy.has_work.return_value = True
    motor._speaking = True  # speech busy -> the pop path asks about the hold
    motor.enqueue("pregunta", priority=1, source="direct")
    motor._process_priority_queue()
    spy.release_arrival_hold.assert_not_called()
    spy.arrival_hold_active.assert_not_called()


def test_ptt_turn_end_and_ptt_submit_do_not_release_a_direct_hold():  # M2
    motor, router = _armed_motor(on=True)
    victim = _active(router, "kira-agenda", PRIORITY_AGENDA)
    assert motor.cut_for_direct_arrival(token="t1") is True
    # A PTT turn (its own token) finishes during the hold.
    motor._current_turn_tokens = {"p1"}
    motor._current_processing_source = "ptt"
    motor._complete_processing_cycle(process_queue=False)
    assert router.arrival_hold_active() is True
    # A PTT reply submit does not clear it either.
    router.submit("ptt reply", source="ptt", priority=PRIORITY_OWNER)
    assert router.arrival_hold_active() is True
    assert victim.pause_requested == "preempt"


def test_older_direct_reply_does_not_release_a_newer_directs_hold():  # M2
    motor, router = _armed_motor(on=True)
    _active(router, "kira-agenda", PRIORITY_AGENDA)
    assert motor.cut_for_direct_arrival(token="t2") is True
    motor._current_turn_tokens = {"t1"}  # older direct turn replies
    router.submit("old reply", source="direct", priority=PRIORITY_OWNER)
    assert router.arrival_hold_active() is True
    motor._current_turn_tokens = {"t2"}
    router.submit("new reply", source="direct", priority=PRIORITY_OWNER)
    assert router.arrival_hold_active() is False


def test_streaming_reply_of_the_owning_turn_releases_the_hold():
    motor, router = _armed_motor(on=True)
    _active(router, "kira-agenda", PRIORITY_AGENDA)
    motor.cut_for_direct_arrival(token="t1")
    motor._current_turn_tokens = {"t1"}
    router.submit_streaming(source="direct", priority=PRIORITY_OWNER)
    assert router.arrival_hold_active() is False


def test_errored_or_empty_direct_turn_end_clears_its_hold():
    motor, router = _armed_motor(on=True)
    _active(router, "kira-agenda", PRIORITY_AGENDA)
    motor.cut_for_direct_arrival(token="t1")
    motor._current_turn_tokens = {"t1"}
    motor._current_processing_source = "direct"  # turn ends with no speech submitted
    motor._complete_processing_cycle(process_queue=False)
    assert router.arrival_hold_active() is False


def test_no_cut_while_another_generation_is_in_flight():  # M3
    for attr in ("_processing", "_llm_generating"):
        motor, router = _armed_motor(on=True)
        victim = _active(router, "kira-agenda", PRIORITY_AGENDA)
        setattr(motor, attr, True)
        assert motor.cut_for_direct_arrival(token="t1") is False
        assert victim.pause_requested is None
        assert router.arrival_hold_active() is False


def test_expired_direct_item_releases_its_hold():  # L1
    from types import SimpleNamespace

    motor, router = _armed_motor(on=True)
    _active(router, "kira-agenda", PRIORITY_AGENDA)
    motor.cut_for_direct_arrival(token="t1")
    item = (1, time.monotonic(), "pregunta", "direct", "hist", "t1", None, None)
    expired = SimpleNamespace(item=item, ttl_seconds=1.0, age_seconds=2.0)
    from opencohost.core import llm_engine

    sched = llm_engine._get_turn_scheduler(motor)
    sched.expire = lambda *a, **k: (expired,)
    motor._process_priority_queue()
    assert router.arrival_hold_active() is False


def test_switching_the_setting_off_releases_a_live_hold(monkeypatch, tmp_path):
    import opencohost.config.settings as settings_mod

    monkeypatch.setattr(settings_mod, "TTS_SETTINGS_FILE", str(tmp_path / "tts.json"))
    motor, router = _armed_motor(on=True)
    _active(router, "kira-agenda", PRIORITY_AGENDA)
    motor.cut_for_direct_arrival(token="t1")
    motor._dispatch_command("set_interrupt_on_direct", False)
    assert router.arrival_hold_active() is False


# ── Judgment Day 2026-11-23: S1 / S2 / S3 verification ─────────────────────


def test_bridge_line_does_not_release_the_hold_and_still_plays():  # S1
    """A web/vision/timeout bridge is owner-band speech submitted MID-turn by
    the owning direct turn. It must be audible, but it must not end the hold:
    the suspended victim would resume before the real reply and be cut again
    (ADR-069 decision 3 keeps the hold until the reply is submitted)."""
    motor, router = _armed_motor(on=True)
    victim = _active(router, "kira-agenda", PRIORITY_AGENDA)
    assert motor.cut_for_direct_arrival(token="t1") is True
    victim.state = SpeechJobState.SUSPENDED
    router._active = None
    router._stack.append(victim)
    motor._current_turn_tokens = frozenset({"t1"})

    router.start = lambda: None  # hand-driven: no router thread
    motor._web_session_lang = lambda: "en"
    motor._web_speak_bridge("what is the weather", "direct")

    assert router.arrival_hold_active() is True
    bridge = router._pick()
    assert bridge is not None and bridge.priority == PRIORITY_OWNER
    router._active = None
    assert router._pick() is None  # victim stays held until the real reply
    reply = router.submit("reply", source="direct", priority=PRIORITY_OWNER)
    assert router.arrival_hold_active() is False
    assert router._pick() is reply


def test_command_queue_direct_turn_owns_its_hold_and_releases_it():  # S2
    """A direct turn consumed through command_queue (idle path) must carry its
    own stamp as the turn token, so its reply submit releases the hold."""
    from opencohost.core.llm_engine import TurnStamp

    motor, router = _armed_motor(on=True)
    motor._discard_speech_residue = True
    _active(router, "chat", PRIORITY_CHAT)
    assert motor.cut_for_direct_arrival(token=42.0) is True
    router._active = None  # discarded victim: speech no longer active
    assert motor._speech_active is False
    motor.is_ready = True

    def _infer(payload, source="", **kw):
        router.submit("reply", source=source, priority=PRIORITY_OWNER)

    motor._ejecutar_inferencia = _infer
    motor._dispatch_command(
        "process_context", "pregunta", source="direct", stamp=TurnStamp(submitted_at=42.0)
    )
    assert router.arrival_hold_active() is False


def test_not_ready_refuse_releases_the_turns_hold():  # S2 (REFUSE path)
    from opencohost.core.llm_engine import TurnStamp

    motor, router = _armed_motor(on=True)
    motor._discard_speech_residue = True
    _active(router, "chat", PRIORITY_CHAT)
    assert motor.cut_for_direct_arrival(token=42.0) is True
    router._active = None
    motor._simulated_readiness = False
    motor.is_ready = False
    motor._recover_local_readiness = lambda: None
    motor._dispatch_command(
        "process_context", "pregunta", source="direct", stamp=TurnStamp(submitted_at=42.0)
    )
    assert router.arrival_hold_active() is False
