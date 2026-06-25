"""Tests for motor model source-of-truth: MODEL_TRACE, persistence, retry, UI feedback.

Validates that desired/active/loaded/generation model variables are tracked
separately and that the motor properly handles pending switches, retries,
and persistence.
"""

import json
import logging
import os
import queue
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

# Ensure project root is on path
import sys

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from opencohost.config.settings import DEFAULT_MODEL


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_motor(*, last_model_data=None, tmp_dir=None):
    """Create a MotorVocalIA with mocked dependencies.

    Args:
        last_model_data: If given, write as last_model.json before init.
        tmp_dir: Temp dir for LAST_MODEL_FILE isolation. ALWAYS pass to avoid
                 reading production last_model.json.

    Returns:
        (motor, log_queue, ui_events) tuple.
    """
    import opencohost.config.settings as settings

    original_last_model_file = settings.LAST_MODEL_FILE
    if tmp_dir is not None:
        settings.LAST_MODEL_FILE = os.path.join(tmp_dir, "last_model.json")

    if last_model_data is not None and tmp_dir is not None:
        with open(settings.LAST_MODEL_FILE, "w") as f:
            json.dump(last_model_data, f)

    log_queue = queue.Queue()
    ui_events = []

    def ui_callback(event):
        ui_events.append(event)

    try:
        from opencohost.core.llm_engine import MotorVocalIA
        motor = MotorVocalIA(log_queue, ui_callback)
    finally:
        if tmp_dir is not None:
            settings.LAST_MODEL_FILE = original_last_model_file

    # Mock ollama and pygame so we don't need real services
    motor.ollama = MagicMock()
    motor.pygame = MagicMock()
    motor.is_ready = True

    return motor, log_queue, ui_events


def _drain_log(log_queue):
    """Drain all messages from the log queue."""
    messages = []
    while not log_queue.empty():
        try:
            messages.append(log_queue.get_nowait())
        except queue.Empty:
            break
    return messages


def _wait_until(predicate, *, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


# ---------------------------------------------------------------------------
# 1. Startup reads saved model
# ---------------------------------------------------------------------------

class TestStartupModelResolution:
    def test_startup_reads_saved_model(self, tmp_path):
        """last_model.json with valid model -> current_model matches, source='saved'."""
        motor, _, _ = _make_motor(
            last_model_data={"model": "gemma4:e4b", "saved_at": "2026-01-01", "source": "user_switch"},
            tmp_dir=str(tmp_path),
        )
        assert motor.current_model == "gemma4:e4b"
        assert motor._desired_model == "gemma4:e4b"
        assert motor._model_source == "saved"

    def test_startup_default_no_saved(self, tmp_path):
        """No json file -> current_model == DEFAULT_MODEL, source='default'."""
        motor, _, _ = _make_motor(tmp_dir=str(tmp_path))
        assert motor.current_model == DEFAULT_MODEL
        assert motor._model_source == "default"

    def test_startup_invalid_saved_fallback(self, tmp_path):
        """Json has model not in catalog -> fallback to DEFAULT_MODEL."""
        motor, _, _ = _make_motor(
            last_model_data={"model": "nonexistent-model-xyz"},
            tmp_dir=str(tmp_path),
        )
        assert motor.current_model == DEFAULT_MODEL
        assert motor._model_source == "invalid_saved_fallback"


# ---------------------------------------------------------------------------
# 2. Model switch behavior
# ---------------------------------------------------------------------------

class TestModelSwitch:
    def test_switch_while_ready_applies(self, tmp_path):
        """Switch command when ready -> model_switch_applied, save called."""
        import opencohost.config.settings as settings

        original = settings.LAST_MODEL_FILE
        settings.LAST_MODEL_FILE = os.path.join(str(tmp_path), "last_model.json")
        try:
            motor, log_q, ui_events = _make_motor(tmp_dir=str(tmp_path))
            motor.ollama.generate = MagicMock()

            motor._apply_model_switch("gemma4:e4b")

            assert motor.current_model == "gemma4:e4b"
            assert "model_switch_applied" in ui_events

            # Verify persistence was called
            assert os.path.exists(settings.LAST_MODEL_FILE)
            with open(settings.LAST_MODEL_FILE) as f:
                data = json.load(f)
            assert data["model"] == "gemma4:e4b"
            assert data["source"] == "user_switch"
        finally:
            settings.LAST_MODEL_FILE = original

    def test_switch_while_speaking_is_pending(self, tmp_path):
        """Switch while _speaking=True -> pending state, model NOT changed yet."""
        motor, _, ui_events = _make_motor(tmp_dir=str(tmp_path))
        original_model = motor.current_model
        motor._speaking = True

        # Exercise the real dispatch handler
        motor._dispatch_command("switch_model", "qwen3:4b")

        assert motor._pending_model_switch == "qwen3:4b"
        assert motor._desired_model == "qwen3:4b"
        assert "model_switch_pending" in ui_events
        # current_model should NOT have changed — still the original
        assert motor.current_model == original_model

    def test_pending_switch_applies_after_idle(self, tmp_path):
        """Set pending -> simulate idle -> switch applies."""
        import opencohost.config.settings as settings

        original = settings.LAST_MODEL_FILE
        settings.LAST_MODEL_FILE = os.path.join(str(tmp_path), "last_model.json")
        try:
            motor, _, ui_events = _make_motor(tmp_dir=str(tmp_path))
            motor.ollama.generate = MagicMock()

            motor._pending_model_switch = "gemma4:e4b"
            motor._pending_switch_retries = 3
            motor._pending_switch_next_at = time.monotonic() - 1.0  # already ready
            motor._processing = False
            motor._speaking = False

            motor._check_pending_model_switch()

            assert motor.current_model == "gemma4:e4b"
            assert motor._pending_model_switch is None
            assert "model_switch_applied" in ui_events
        finally:
            settings.LAST_MODEL_FILE = original


# ---------------------------------------------------------------------------
# 3. Retry exhaustion
# ---------------------------------------------------------------------------

class TestRetryExhaustion:
    def test_pending_switch_waits_for_ollama_without_rejecting_user_intent(self, tmp_path):
        """is_ready=False must not reject a model the user intentionally selected."""
        motor, _, ui_events = _make_motor(tmp_dir=str(tmp_path))
        motor.is_ready = False

        motor._pending_model_switch = "gemma4:e4b"
        motor._pending_switch_retries = 3
        motor._pending_switch_next_at = 0
        motor._processing = False
        motor._speaking = False
        motor.ollama.list = MagicMock(side_effect=RuntimeError("down"))

        # Retry 1
        motor._check_pending_model_switch()
        assert motor._pending_model_switch == "gemma4:e4b"
        assert motor._pending_switch_retries == 3

        # Retry 2
        motor._pending_switch_next_at = 0
        motor._check_pending_model_switch()
        assert motor._pending_model_switch == "gemma4:e4b"
        assert motor._pending_switch_retries == 3

        # Retry 3 — still pending; user intent must survive slow Ollama startup
        motor._pending_switch_next_at = 0
        motor._check_pending_model_switch()
        assert motor._pending_model_switch == "gemma4:e4b"
        assert "model_switch_failed" not in ui_events
        assert motor._last_switch_failure is None

    def test_model_selected_before_ollama_ready_applies_after_check_ollama(self, tmp_path):
        """Selecting Gemma while Ollama is down should become intent, not a dead rejection.

        The user should be able to choose a model first, start/check Ollama next,
        and have the selected model prepared automatically once Ollama is ready.
        """
        import opencohost.config.settings as settings

        original = settings.LAST_MODEL_FILE
        settings.LAST_MODEL_FILE = os.path.join(str(tmp_path), "last_model.json")
        try:
            motor, _, ui_events = _make_motor(tmp_dir=str(tmp_path))
            motor.is_ready = False
            motor.current_model = "qwen3:1.7b"
            motor._desired_model = "gemma4:e4b"
            motor._pending_model_switch = "gemma4:e4b"
            motor._pending_switch_retries = 3
            motor._pending_switch_next_at = 0
            motor.ollama.list = MagicMock(side_effect=[
                RuntimeError("down"),
                RuntimeError("down"),
                RuntimeError("down"),
                [],
            ])
            motor.ollama.generate = MagicMock()

            # Simulate the old failure window passing before Ollama is ready.
            for _ in range(3):
                motor._pending_switch_next_at = 0
                motor._check_pending_model_switch()
                assert motor._pending_model_switch == "gemma4:e4b"

            assert motor._check_ollama_service() is True

            assert motor.current_model == "gemma4:e4b"
            assert motor._pending_model_switch is None
            assert "model_switch_applied" in ui_events
            assert os.path.exists(settings.LAST_MODEL_FILE)
            with open(settings.LAST_MODEL_FILE) as f:
                data = json.load(f)
            assert data["model"] == "gemma4:e4b"
        finally:
            settings.LAST_MODEL_FILE = original

    def test_pending_switch_rechecks_ollama_readiness_without_user_check_spam(self, tmp_path):
        """Pending switch should recover when Ollama starts outside the motor loop."""
        import opencohost.config.settings as settings

        original = settings.LAST_MODEL_FILE
        settings.LAST_MODEL_FILE = os.path.join(str(tmp_path), "last_model.json")
        try:
            motor, log_q, ui_events = _make_motor(tmp_dir=str(tmp_path))
            motor.is_ready = False
            motor.current_model = "qwen3:1.7b"
            motor._desired_model = "gemma4:e4b"
            motor._pending_model_switch = "gemma4:e4b"
            motor._pending_switch_retries = 3
            motor._pending_switch_next_at = 0
            motor._processing = False
            motor._speaking = False
            motor.ollama.list = MagicMock(side_effect=[RuntimeError("down"), []])
            motor.ollama.generate = MagicMock()

            motor._check_pending_model_switch()
            motor._pending_switch_next_at = 0
            motor._check_pending_model_switch()

            assert motor.ollama.list.call_count == 2
            assert motor.current_model == "gemma4:e4b"
            assert motor._pending_model_switch is None
            assert "model_switch_applied" in ui_events

            pending_logs = [
                msg for msg in _drain_log(log_q)
                if "Switch a gemma4:e4b sigue pendiente: Ollama no está listo." in msg
            ]
            assert len(pending_logs) == 1
        finally:
            settings.LAST_MODEL_FILE = original

    def test_run_loop_rejects_offline_switch_immediately(self, tmp_path, monkeypatch):
        """Offline switch_model must fail immediately, NOT defer to retry queue.

        Per ADR-006: when is_ready=False, switch_model emits 'model_switch_failed'
        and does NOT set _pending_model_switch. The user must switch when Ollama
        becomes ready.
        """
        import opencohost.config.settings as settings

        original = settings.LAST_MODEL_FILE
        settings.LAST_MODEL_FILE = os.path.join(str(tmp_path), "last_model.json")
        motor = None
        try:
            motor, _, ui_events = _make_motor(tmp_dir=str(tmp_path))
            motor.current_model = "qwen3:1.7b"
            motor._desired_model = "qwen3:1.7b"

            fake_ollama = SimpleNamespace(
                list=MagicMock(side_effect=[RuntimeError("down"), []]),
                generate=MagicMock(),
                Client=MagicMock(return_value=MagicMock()),
            )
            fake_pygame = SimpleNamespace(mixer=SimpleNamespace(init=MagicMock()))
            monkeypatch.setitem(sys.modules, "ollama", fake_ollama)
            monkeypatch.setitem(sys.modules, "pygame", fake_pygame)

            motor.start()
            assert _wait_until(lambda: fake_ollama.list.call_count == 1)
            assert motor.is_ready is False

            motor.command_queue.put(("switch_model", "gemma4:e4b"))
            assert _wait_until(lambda: "model_switch_failed" in ui_events)
            assert motor.current_model == "qwen3:1.7b"
            assert motor._pending_model_switch is None

            # Ollama comes online — but the switch was already rejected
            # The user's _desired_model is recorded but not applied automatically
            assert motor._desired_model == "gemma4:e4b"
        finally:
            if motor is not None:
                motor.command_queue.put(None)
                motor.join(timeout=1.0)
            settings.LAST_MODEL_FILE = original

    def test_pending_switch_readiness_probe_does_not_emit_unavailable_spam(self, tmp_path):
        """Background pending retries should not spam UI/logs while Ollama is still down."""
        motor, log_q, ui_events = _make_motor(tmp_dir=str(tmp_path))
        motor.is_ready = False
        motor.current_model = "qwen3:1.7b"
        motor._desired_model = "gemma4:e4b"
        motor._pending_model_switch = "gemma4:e4b"
        motor._pending_switch_retries = 3
        motor._pending_switch_next_at = 0
        motor._processing = False
        motor._speaking = False
        motor.ollama.list = MagicMock(side_effect=RuntimeError("down"))

        motor._check_pending_model_switch()
        motor._pending_switch_next_at = 0
        motor._check_pending_model_switch()

        logs = _drain_log(log_q)

        assert ui_events == []
        assert not any("Ollama no esta disponible" in msg for msg in logs)
        pending_logs = [
            msg for msg in logs
            if "Switch a gemma4:e4b sigue pendiente: Ollama no está listo." in msg
        ]
        assert len(pending_logs) == 1

    def test_check_ollama_with_pending_model_does_not_warm_stale_current_model(self, tmp_path):
        """When Ollama becomes ready, prepare the user's pending model directly.

        Warming the stale current model first adds exactly the delay the user is
        trying to avoid when selecting a heavier Gemma model before Ollama is up.
        """
        import opencohost.config.settings as settings

        original = settings.LAST_MODEL_FILE
        settings.LAST_MODEL_FILE = os.path.join(str(tmp_path), "last_model.json")
        try:
            motor, _, _ = _make_motor(tmp_dir=str(tmp_path))
            motor.is_ready = False
            motor.current_model = "qwen3:1.7b"
            motor._desired_model = "gemma4:e4b"
            motor._pending_model_switch = "gemma4:e4b"
            motor.ollama.list = MagicMock(return_value=[])
            motor.ollama.generate = MagicMock()

            assert motor._check_ollama_service() is True

            warmed_models = [
                call.kwargs["model"]
                for call in motor.ollama.generate.call_args_list
                if call.kwargs.get("prompt") == "Responde solo: ok"
            ]
            assert warmed_models == ["gemma4:e4b"]
        finally:
            settings.LAST_MODEL_FILE = original

    def test_check_ollama_clears_pending_when_model_is_already_current(self, tmp_path):
        """A pending intent equal to the active model should not linger forever."""
        motor, _, ui_events = _make_motor(tmp_dir=str(tmp_path))
        motor.is_ready = False
        motor.current_model = "gemma4:e4b"
        motor._desired_model = "gemma4:e4b"
        motor._pending_model_switch = "gemma4:e4b"
        motor.ollama.list = MagicMock(return_value=[])
        motor.ollama.generate = MagicMock()

        assert motor._check_ollama_service() is True

        assert motor.current_model == "gemma4:e4b"
        assert motor._pending_model_switch is None
        assert "model_switch_failed" not in ui_events

    def test_pending_model_prepare_failure_does_not_persist_or_mark_active(self, tmp_path):
        """If pending Gemma cannot warm, keep the previous model as source of truth."""
        import opencohost.config.settings as settings

        original = settings.LAST_MODEL_FILE
        settings.LAST_MODEL_FILE = os.path.join(str(tmp_path), "last_model.json")
        try:
            motor, _, ui_events = _make_motor(tmp_dir=str(tmp_path))
            motor.is_ready = False
            motor.current_model = "qwen3:1.7b"
            motor._desired_model = "gemma4:e4b"
            motor._pending_model_switch = "gemma4:e4b"
            motor.ollama.list = MagicMock(return_value=[])

            def fake_generate(*, model, prompt, **kwargs):
                if model == "gemma4:e4b" and prompt == "Responde solo: ok":
                    raise RuntimeError("model unavailable")
                return {"response": "ok"}

            motor.ollama.generate = MagicMock(side_effect=fake_generate)

            assert motor._check_ollama_service() is True

            assert motor.current_model == "qwen3:1.7b"
            assert motor._desired_model == "qwen3:1.7b"
            assert "model_switch_failed" in ui_events
            assert not os.path.exists(settings.LAST_MODEL_FILE)
        finally:
            settings.LAST_MODEL_FILE = original


# ---------------------------------------------------------------------------
# 4. MODEL_TRACE logging
# ---------------------------------------------------------------------------

class TestModelTrace:
    """MODEL_TRACE goes to logger.info (not log_queue) when all models match.

    When there is a mismatch, it goes to log_queue via self._log(WARNING).
    Tests check captured log records via caplog.
    """

    def _setup_motor_for_generation(self, tmp_path):
        """Create motor ready for _generar_dialogo with mocked ollama.chat."""
        motor, log_q, ui_events = _make_motor(tmp_dir=str(tmp_path))
        motor.ollama.chat = MagicMock(return_value={
            "message": {"content": "Respuesta de test", "thinking": ""},
        })
        motor._loaded_model = motor.current_model
        return motor, log_q, ui_events

    def test_model_trace_standalone(self, tmp_path, caplog):
        """MODEL_TRACE appears in logger for standalone/direct source."""
        motor, log_q, _ = self._setup_motor_for_generation(tmp_path)

        with caplog.at_level(logging.INFO, logger="OpenCohost"):
            result = motor._generar_dialogo("hola", source="direct", commit_history=False)

        assert result == "Respuesta de test"
        trace_records = [r for r in caplog.records if "MODEL_TRACE" in r.message]
        assert len(trace_records) >= 1, f"No MODEL_TRACE in caplog: {[r.message for r in caplog.records]}"
        assert "source=direct" in trace_records[0].message

    def test_model_trace_agenda(self, tmp_path, caplog):
        """MODEL_TRACE with source=kira-agenda."""
        motor, log_q, _ = self._setup_motor_for_generation(tmp_path)
        motor.agenda_output_validator = None
        motor.agenda_output_preview_validator = None
        motor.agenda_output_transformer = None

        with caplog.at_level(logging.INFO, logger="OpenCohost"):
            result = motor._generar_dialogo("tema de agenda", source="kira-agenda", commit_history=False)

        assert result  # non-empty after sanitization
        trace_records = [r for r in caplog.records if "MODEL_TRACE" in r.message and "kira-agenda" in r.message]
        assert len(trace_records) >= 1, f"No agenda MODEL_TRACE: {[r.message for r in caplog.records]}"

    def test_model_trace_prefetch(self, tmp_path, caplog):
        """Prefetch uses agenda source, MODEL_TRACE should reflect it."""
        motor, log_q, _ = self._setup_motor_for_generation(tmp_path)
        motor.agenda_output_validator = None
        motor.agenda_output_preview_validator = None
        motor.agenda_output_transformer = None

        with caplog.at_level(logging.INFO, logger="OpenCohost"):
            result = motor._generar_dialogo(
                "prefetch test", source="kira-agenda", commit_history=False, log_prefix="Agenda prefetch",
            )

        assert result
        trace_records = [r for r in caplog.records if "MODEL_TRACE" in r.message]
        assert len(trace_records) >= 1

    def test_model_mismatch_warning(self, tmp_path):
        """desired != active -> MODEL_MISMATCH_WARNING in log queue."""
        motor, log_q, _ = self._setup_motor_for_generation(tmp_path)

        # Simulate: UI requested gemma4:e4b but motor still on default
        motor._desired_model = "gemma4:e4b"
        # current_model stays as default (whatever _make_motor resolved)

        motor._generar_dialogo("test mismatch", source="direct", commit_history=False)

        logs = _drain_log(log_q)
        mismatch_logs = [l for l in logs if "MODEL_MISMATCH_WARNING" in l]
        assert len(mismatch_logs) >= 1, f"No MODEL_MISMATCH_WARNING found in: {logs}"
        assert "desired=gemma4:e4b" in mismatch_logs[0]


# ---------------------------------------------------------------------------
# 5. Persistence roundtrip
# ---------------------------------------------------------------------------

class TestPersistence:
    def test_persistence_roundtrip(self, tmp_path):
        """save_last_model -> resolve_startup_model returns saved value."""
        import opencohost.config.settings as settings

        original = settings.LAST_MODEL_FILE
        settings.LAST_MODEL_FILE = os.path.join(str(tmp_path), "last_model.json")
        try:
            settings.save_last_model("gemma4:e4b", source="test")

            model, source = settings.resolve_startup_model()
            assert model == "gemma4:e4b"
            assert source == "saved"

            with open(settings.LAST_MODEL_FILE) as f:
                data = json.load(f)
            assert data["model"] == "gemma4:e4b"
            assert data["source"] == "test"
            assert "saved_at" in data
        finally:
            settings.LAST_MODEL_FILE = original

    def test_persistence_invalid_model_fallback(self, tmp_path):
        """Saved model not in catalog -> fallback."""
        import opencohost.config.settings as settings

        original = settings.LAST_MODEL_FILE
        settings.LAST_MODEL_FILE = os.path.join(str(tmp_path), "last_model.json")
        try:
            settings.save_last_model("this-does-not-exist", source="test")

            model, source = settings.resolve_startup_model()
            assert model == DEFAULT_MODEL
            assert source == "invalid_saved_fallback"
        finally:
            settings.LAST_MODEL_FILE = original


# ---------------------------------------------------------------------------
# 6. Non-blocking retry verification
# ---------------------------------------------------------------------------

class TestNonBlockingRetry:
    def test_no_sleep_in_retry(self, tmp_path):
        """Verify that _check_pending_model_switch does NOT call time.sleep."""
        motor, _, _ = _make_motor(tmp_dir=str(tmp_path))
        motor.is_ready = False
        motor._pending_model_switch = "gemma4:e4b"
        motor._pending_switch_retries = 2
        motor._pending_switch_next_at = 0

        with patch("time.sleep") as mock_sleep:
            motor._check_pending_model_switch()
            mock_sleep.assert_not_called()

    def test_uses_monotonic_for_backoff(self, tmp_path):
        """Verify that _pending_switch_next_at uses time.monotonic."""
        motor, _, _ = _make_motor(tmp_dir=str(tmp_path))
        motor.is_ready = False
        motor._pending_model_switch = "gemma4:e4b"
        motor._pending_switch_retries = 3
        motor._pending_switch_next_at = 0

        before = time.monotonic()
        motor._check_pending_model_switch()
        after = time.monotonic()

        # After a failed retry, next_at should be ~2s in the future
        assert motor._pending_switch_next_at >= before + 1.5
        assert motor._pending_switch_next_at <= after + 3.0
