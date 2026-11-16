"""Spoken notice when the inference watchdog recovers a stalled owner turn (V2)."""

from __future__ import annotations

import types
from unittest.mock import MagicMock

import pytest

from opencohost.config.settings import OWNER_BUNDLE_SOURCE
from opencohost.core.engine import llm_engine_models as models_mod
from tests.test_heavy_model_inference_recovery import _make_motor

ES_LINE = "Se me trabó la respuesta, pregúntamelo otra vez."
EN_LINE = "My answer got stuck, ask me again."


def _patch_locale(monkeypatch, code=None, fail=False):
    from opencohost.i18n import active as i18n_active

    def fake():
        if fail:
            raise RuntimeError("boom")
        return types.SimpleNamespace(code=code)

    monkeypatch.setattr(i18n_active, "get_active_bundle", fake)


@pytest.fixture
def armed(tmp_path, monkeypatch):
    """Motor with an armed (mocked) router, rollback stubbed, es locale."""
    motor, _, ui_events = _make_motor(tmp_dir=str(tmp_path))
    # Patched only after construction: the motor reads the real bundle at init.
    _patch_locale(monkeypatch, "es")
    motor._speech_router_enabled = True
    router = MagicMock()
    motor._ensure_router = MagicMock(return_value=router)
    motor._rollback_to_last_known_good_model = MagicMock(return_value=True)
    motor._pending_model_switch = None
    return motor, router, ui_events


def _recover(motor, source="direct"):
    motor._recover_from_stalled_inference(request_model="qwopus", source=source, timeout=45.0)


@pytest.mark.parametrize("source", ["direct", "ptt", OWNER_BUNDLE_SOURCE])
def test_owner_source_timeout_queues_exactly_one_spoken_line(armed, source):
    motor, router, ui_events = armed
    _recover(motor, source)
    assert router.submit.call_count == 1
    text, job_source, priority = router.submit.call_args.args[:3]
    from opencohost.core.speech.router import PRIORITY_OWNER

    assert text == ES_LINE
    assert priority == PRIORITY_OWNER
    assert job_source.startswith(source)
    assert "llm_timeout_recovered" in ui_events
    motor._rollback_to_last_known_good_model.assert_called_once_with("qwopus")


@pytest.mark.parametrize("source", ["chat", "viewer", "agenda", "", "vision-bridge"])
def test_non_owner_sources_speak_nothing(armed, source):
    motor, router, ui_events = armed
    _recover(motor, source)
    router.submit.assert_not_called()
    assert "llm_timeout_recovered" in ui_events


def test_router_disabled_speaks_nothing(armed):
    motor, router, ui_events = armed
    motor._speech_router_enabled = False
    _recover(motor)
    router.submit.assert_not_called()
    motor._ensure_router.assert_not_called()
    assert "llm_timeout_recovered" in ui_events


@pytest.mark.parametrize(
    "code, expected",
    [("es", ES_LINE), ("es-MX", ES_LINE), ("en", EN_LINE), ("en-US", EN_LINE),
     ("fr", EN_LINE), ("pt-BR", EN_LINE), (None, EN_LINE)],
)
def test_line_is_localized_with_english_fallback(armed, monkeypatch, code, expected):
    motor, router, _ = armed
    _patch_locale(monkeypatch, code)
    _recover(motor)
    assert router.submit.call_args.args[0] == expected


def test_locale_accessor_failure_falls_back_to_english(armed, monkeypatch):
    motor, router, _ = armed
    _patch_locale(monkeypatch, fail=True)
    _recover(motor)
    assert router.submit.call_args.args[0] == EN_LINE


def test_submit_failure_does_not_break_recovery(armed):
    motor, router, ui_events = armed
    router.submit.side_effect = RuntimeError("router down")
    _recover(motor)
    assert "llm_timeout_recovered" in ui_events
    motor._rollback_to_last_known_good_model.assert_called_once_with("qwopus")


def test_sanitizer_failure_does_not_break_recovery(armed):
    motor, router, ui_events = armed
    motor._sanitize_for_tts = MagicMock(side_effect=RuntimeError("boom"))
    _recover(motor)
    assert "llm_timeout_recovered" in ui_events
    router.submit.assert_not_called()


def test_output_guard_block_drops_the_line(armed, monkeypatch):
    motor, router, ui_events = armed
    from opencohost.core.engine import generation_orchestrator as go

    monkeypatch.setattr(go, "_output_guard_with_tts_check", lambda *a, **k: (False, "blocked"))
    _recover(motor)
    router.submit.assert_not_called()
    assert "llm_timeout_recovered" in ui_events


def test_speaking_happens_after_the_rollback_is_attempted(armed):
    motor, router, _ = armed
    order = []
    motor._rollback_to_last_known_good_model.side_effect = lambda m: order.append("rollback")
    router.submit.side_effect = lambda *a, **k: order.append("speak")
    _recover(motor)
    assert order == ["rollback", "speak"]


def test_notice_never_calls_the_llm(armed):
    motor, router, _ = armed
    motor.ollama.chat.reset_mock()
    _recover(motor)
    motor.ollama.chat.assert_not_called()


def test_notice_constants_are_exposed():
    assert models_mod.TIMEOUT_NOTICE_LINES["es"] == ES_LINE
    assert models_mod.TIMEOUT_NOTICE_LINES["en"] == EN_LINE
