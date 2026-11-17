"""pregen-retrigger: the interactive pregen trigger is re-evaluated at the
moments the old trigger sites never covered.

Measured gap (2026-08..10 logs): a typed/PTT turn that arrives while a
foreground generation runs is refused by the GPU-free gates, and nothing
re-evaluated it once that generation ended, so Kira spoke for 20s+ with an idle
GPU and then generated serially (`draft=none`). Armed, `_processing` clears in
`_complete_processing_cycle` (the turn "ends" when `submit` returns, before the
playback does); the drain that follows refuses a non-priority-0 head until the
boundary, and that refusal is where the trigger is re-evaluated.
"""

import logging
import queue
import threading
import time
from unittest.mock import MagicMock

from opencohost.core.llm_engine import MotorVocalIA


def _bare_motor() -> MotorVocalIA:
    motor = MotorVocalIA(queue.Queue(), lambda event: None)
    motor.current_model = "llama3"
    motor._reasoning_model_cache["llama3"] = False
    motor._model_ctx_limit = {"llama3": 8192}
    return motor


def _item(payload, source="direct", *, priority=1, age=0.0):
    return (priority, time.time() - age, payload, source, None, None, None)


def _seed_queue(motor, items):
    with motor._pq_lock:
        motor._priority_queue = list(items)


def _armed_foreground_ended(motor):
    """Armed host, a foreground turn just finished generating and Kira speaks."""
    motor._speech_interrupt_enabled = True
    motor._speech_router_enabled = True
    motor._speaking = True
    with motor._lock:
        motor._processing = True


# ── P1 ──────────────────────────────────────────────────────────────────────


def test_p1_head_refused_during_foreground_turn_is_pregenerated_when_the_turn_ends():
    motor = _bare_motor()
    motor.pregenerate = MagicMock(return_value=True)
    _armed_foreground_ended(motor)
    _seed_queue(motor, [_item("hola kira")])

    # While the armed turn is processing, the trigger refuses (the gate under test).
    motor._maybe_trigger_interactive_pregen(motor._head_snapshot_locked())
    motor.pregenerate.assert_not_called()

    motor._complete_processing_cycle()

    motor.pregenerate.assert_called_once()
    assert motor.pregenerate.call_args.args[0] == "hola kira"
    assert motor.pregenerate.call_args.args[2] == "direct"


def test_p1_does_not_fire_when_not_speaking():
    motor = _bare_motor()
    motor.pregenerate = MagicMock(return_value=True)
    _armed_foreground_ended(motor)
    motor._speaking = False
    _seed_queue(motor, [_item("hola kira")])

    motor._complete_processing_cycle()

    motor.pregenerate.assert_not_called()


def test_p1_respects_single_runner_gate_when_a_generation_is_in_flight():
    motor = _bare_motor()
    motor.pregenerate = MagicMock(return_value=True)
    _armed_foreground_ended(motor)
    with motor._lock:
        motor._llm_generating = True
    _seed_queue(motor, [_item("hola kira")])

    motor._complete_processing_cycle()

    motor.pregenerate.assert_not_called()


def test_p1_respects_bundling_when_two_owner_questions_are_pending():
    motor = _bare_motor()
    motor.pregenerate = MagicMock(return_value=True)
    _armed_foreground_ended(motor)
    _seed_queue(motor, [_item("Q1", age=2.0), _item("Q2", age=1.0)])

    motor._complete_processing_cycle()

    motor.pregenerate.assert_not_called()


def test_p1_is_idempotent_one_generation_across_repeated_cycle_ends():
    motor = _bare_motor()
    motor._commit_history = lambda *a, **kw: None
    gen_calls = []
    release = threading.Event()

    def _gen(contexto, source="direct", commit_history=True, history_text=None, log_prefix="LLM", **_kwargs):
        gen_calls.append(source)
        release.wait(2.0)
        return "Respuesta."

    motor._generar_dialogo = _gen
    _armed_foreground_ended(motor)
    _seed_queue(motor, [_item("hola kira")])

    motor._complete_processing_cycle()
    with motor._lock:
        motor._processing = True
    motor._complete_processing_cycle()  # worker still in flight
    release.set()
    deadline = time.time() + 2.0
    while time.time() < deadline and motor._prefetched_agenda is None:
        time.sleep(0.005)
    with motor._lock:
        motor._processing = True
    motor._complete_processing_cycle()  # draft already stored

    assert gen_calls == ["direct"], "the head was generated more than once"


# ── P2 ──────────────────────────────────────────────────────────────────────


def test_p2_requeued_bundle_follower_is_re_evaluated_after_the_failed_turn():
    motor = _bare_motor()
    motor.pregenerate = MagicMock(return_value=True)
    motor._generar_dialogo = lambda *a, **kw: ""
    motor._hablar = MagicMock()
    _armed_foreground_ended(motor)
    follower = _item("pregunta escrita", age=5.0)

    motor._ejecutar_inferencia(
        "1. pregunta hablada\n2. pregunta escrita",
        source="owner-bundle",
        bundle_followers=[follower],
    )
    assert motor._priority_queue[0] is follower  # requeued by BUNDLE_FAILED
    motor._complete_processing_cycle()

    motor.pregenerate.assert_called_once()
    assert motor.pregenerate.call_args.args[0] == "pregunta escrita"


# ── P3 ──────────────────────────────────────────────────────────────────────


def _cloud_motor(monkeypatch):
    # `_is_local` is a read-only property derived from the provider config.
    monkeypatch.setattr(MotorVocalIA, "_is_local", property(lambda self: False))
    motor = _bare_motor()
    motor._provider_config = {"pregen_enabled": False}
    return motor


def _refusals(caplog):
    return [r for r in caplog.records if r.getMessage().startswith("Pregen refused")]


def test_p3_cloud_gate_refusal_logs_reason_once_per_head_without_payload(caplog, monkeypatch):
    caplog.set_level(logging.INFO, logger="OpenCohost")
    motor = _cloud_motor(monkeypatch)

    assert motor.pregenerate("texto secreto del owner", 1, "direct") is False
    assert motor.pregenerate("texto secreto del owner", 1, "direct") is False

    records = _refusals(caplog)
    assert len(records) == 1, "must log at most once per head"
    assert records[0].levelno == logging.INFO
    assert records[0].getMessage() == "Pregen refused: reason=cloud_pregen_disabled source=direct"
    assert "secreto" not in caplog.text


def test_p3_a_new_head_logs_its_own_refusal(caplog, monkeypatch):
    caplog.set_level(logging.INFO, logger="OpenCohost")
    motor = _cloud_motor(monkeypatch)

    motor.pregenerate("primera", 1, "direct")
    motor.pregenerate("segunda", 1, "ptt")

    assert [r.getMessage() for r in _refusals(caplog)] == [
        "Pregen refused: reason=cloud_pregen_disabled source=direct",
        "Pregen refused: reason=cloud_pregen_disabled source=ptt",
    ]


def test_p3_local_provider_and_enabled_cloud_do_not_log_a_refusal(caplog, monkeypatch):
    caplog.set_level(logging.INFO, logger="OpenCohost")
    local = _bare_motor()
    local._generar_dialogo = lambda *a, **kw: ""
    local.pregenerate("hola", 1, "direct")
    cloud_on = _cloud_motor(monkeypatch)
    cloud_on._provider_config = {"pregen_enabled": True}
    cloud_on._generar_dialogo = lambda *a, **kw: ""
    cloud_on.pregenerate("hola", 1, "direct")

    assert _refusals(caplog) == []


# ── P4 ──────────────────────────────────────────────────────────────────────


def test_p4_agenda_connector_cloud_gate_refusal_logs_reason_once_per_epoch(caplog, monkeypatch):
    caplog.set_level(logging.INFO, logger="OpenCohost")
    motor = _cloud_motor(monkeypatch)
    motor._prefetch_epoch = 7

    motor._maybe_generate_connector_upgrade()
    motor._maybe_generate_connector_upgrade()

    records = _refusals(caplog)
    assert len(records) == 1, "must log at most once per head"
    assert records[0].levelno == logging.INFO
    assert records[0].getMessage() == (
        "Pregen refused: reason=cloud_pregen_disabled source=kira-agenda-connector"
    )

    motor._prefetch_epoch = 8  # a new head logs its own refusal
    motor._maybe_generate_connector_upgrade()
    assert len(_refusals(caplog)) == 2


def test_p4_local_motor_logs_no_refusal(caplog):
    caplog.set_level(logging.INFO, logger="OpenCohost")
    motor = _bare_motor()
    motor.speech_remaining_estimate = lambda: None
    motor._maybe_generate_connector_upgrade()
    assert _refusals(caplog) == []
