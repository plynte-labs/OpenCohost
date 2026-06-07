"""Integration and stress tests for Ollama Offline UI/UX Guardrails.

Validates the spec of track 'ollama_offline_ux_guardrails_20260601':
1. UI controls (combo, buttons, tiers) are disabled when Ollama is offline.
2. Silent deferred model switches are rejected immediately instead of queuing.
3. Model warming triggers clear, non-blocking visual feedback and guards.
4. Auto-enabling of UI controls when Ollama becomes ready.

Edge cases for production robustness:
- Rapid model switch during warming (VRAM OOM guard)
- User interaction during 'checking' state (startup race)
- PTT while Ollama is offline (late feedback prevention)
- State flapping (health monitor rapid transitions)
- Ollama dies mid-inference (graceful degradation)
- Tier switch while motor is busy (queue collision)
"""

from __future__ import annotations

import queue
import time
from unittest.mock import MagicMock, patch

import pytest

from ui.state import UIState
from ui.protocols import CallbackDispatcher
from ui.model_panel import ModelPanel
from core.llm_engine import MotorVocalIA


# ---------------------------------------------------------------------------
# Shared fixtures (reuse patterns from test_model_panel.py)
# ---------------------------------------------------------------------------


@pytest.fixture()
def ui_state():
    s = UIState()
    yield s
    s.shutdown(timeout=2.0)


@pytest.fixture()
def dispatcher():
    return CallbackDispatcher(source="ModelPanel")


@pytest.fixture()
def mock_ctk():
    """Mock customtkinter module with realistic widget behavior.

    Mirrors test_model_panel.py mock fixtures for consistency.
    """
    mock_module = MagicMock()

    class MockLabel:
        def __init__(self, master=None, **kwargs):
            self.master = master
            self.kwargs = kwargs
            self.text = kwargs.get("text", "")
            self.fg_color = kwargs.get("fg_color", "")
            self.text_color = kwargs.get("text_color", "")
            self.state = "normal"

        def configure(self, **kwargs):
            for key, value in kwargs.items():
                setattr(self, key, value)
                self.kwargs[key] = value

        def pack(self, **kwargs):
            pass

        def pack_forget(self):
            pass

    class MockButton:
        def __init__(self, master=None, **kwargs):
            self.master = master
            self.kwargs = kwargs
            self.text = kwargs.get("text", "")
            self.state = kwargs.get("state", "normal")
            self.fg_color = kwargs.get("fg_color", "")
            self.hover_color = kwargs.get("hover_color", "")
            self.command = kwargs.get("command", None)

        def configure(self, **kwargs):
            for key, value in kwargs.items():
                setattr(self, key, value)
                self.kwargs[key] = value

        def pack(self, **kwargs):
            pass

        def pack_forget(self):
            pass

    class MockOptionMenu:
        def __init__(self, master=None, **kwargs):
            self.master = master
            self.kwargs = kwargs
            self.values = kwargs.get("values", [])
            self._value = self.values[0] if self.values else ""
            self.command = kwargs.get("command", None)
            self.state = kwargs.get("state", "normal")

        def set(self, value):
            self._value = value

        def get(self):
            return self._value

        def configure(self, **kwargs):
            for key, value in kwargs.items():
                setattr(self, key, value)
                self.kwargs[key] = value

        def pack(self, **kwargs):
            pass

        def pack_forget(self):
            pass

    class MockProgressBar:
        def __init__(self, master=None, **kwargs):
            self.master = master
            self._value = 0.0
            self._packed = True

        def set(self, value):
            self._value = value

        def get(self):
            return self._value

        def pack(self, **kwargs):
            self._packed = True

        def pack_forget(self):
            self._packed = False

    class MockFont:
        def __init__(self, **kwargs):
            pass

    mock_module.CTkLabel = MockLabel
    mock_module.CTkButton = MockButton
    mock_module.CTkOptionMenu = MockOptionMenu
    mock_module.CTkProgressBar = MockProgressBar
    mock_module.CTkFont = MockFont
    return mock_module


@pytest.fixture()
def on_log():
    return MagicMock()


@pytest.fixture()
def built_panel(mock_ctk, ui_state, dispatcher, on_log):
    """ModelPanel built with mocked ctk — default ollama_state is 'checking'."""
    from config.settings import DEFAULT_MODEL

    with patch("ui.model_panel.resolve_startup_model", return_value=(DEFAULT_MODEL, "default")):
        with patch.dict("sys.modules", {"customtkinter": mock_ctk}):
            panel = ModelPanel(
                parent_frame=MagicMock(),
                ui_state=ui_state,
                dispatcher=dispatcher,
                on_log=on_log,
            )
            with patch("ui.model_panel.ctk", mock_ctk):
                panel.build()
    yield panel
    panel.cleanup()


def _make_motor() -> tuple[MotorVocalIA, MagicMock]:
    """Create a MotorVocalIA with mocked dependencies (does NOT start thread)."""
    log_q = queue.Queue()
    ui_cb = MagicMock()
    motor = MotorVocalIA(log_queue=log_q, ui_callback=ui_cb)
    return motor, ui_cb


# ═══════════════════════════════════════════════════════════════════════════
# SPEC TESTS — these enforce the new guardrails behavior
# ═══════════════════════════════════════════════════════════════════════════


class TestOllamaOfflineUXGuardrailsSpec:
    """Spec tests for track ollama_offline_ux_guardrails_20260601.

    These are RED until the production code is modified to implement
    the guardrails. Each test documents the DESIRED behavior.
    """

    # --- Spec 1: UI controls disabled when Ollama not ready ---------------

    @pytest.mark.parametrize("ollama_state", [
        "service_stopped",
        "app_missing",
        "package_missing",
        "checking",
    ])
    def test_ui_controls_disabled_when_ollama_not_ready(
        self, mock_ctk, ui_state, dispatcher, on_log, ollama_state,
    ):
        """Model combo and button must be disabled for ALL non-ready Ollama states."""
        from config.settings import DEFAULT_MODEL

        ui_state.ollama_state = ollama_state

        with patch("ui.model_panel.resolve_startup_model", return_value=(DEFAULT_MODEL, "default")):
            with patch.dict("sys.modules", {"customtkinter": mock_ctk}):
                panel = ModelPanel(
                    parent_frame=MagicMock(),
                    ui_state=ui_state,
                    dispatcher=dispatcher,
                    on_log=on_log,
                )
                with patch("ui.model_panel.ctk", mock_ctk):
                    panel.build()

        assert panel.combo_modelos.state == "disabled", (
            f"combo_modelos should be disabled when ollama_state='{ollama_state}'"
        )
        assert panel.btn_download.state == "disabled" or ollama_state in ("service_stopped", "app_missing", "package_missing"), (
            f"btn_download should be disabled or show action when ollama_state='{ollama_state}'"
        )
        panel.cleanup()

    def test_tier_buttons_disabled_when_ollama_offline(self, built_panel, ui_state):
        """Tier buttons must be disabled when Ollama is not ready."""
        ui_state.ollama_state = "service_stopped"
        built_panel._update_button_for_ollama_state()

        for tier, button in built_panel._tier_buttons.items():
            assert button.state == "disabled", (
                f"Tier button '{tier}' should be disabled when Ollama is offline"
            )

    # --- Spec 2: No silent deferred model switch --------------------------

    def test_no_silent_deferred_model_switch_offline(self):
        """When is_ready=False, switch_model must NOT queue _pending_model_switch.

        This test exercises the REAL _dispatch_command handler path.
        """
        motor, ui_cb = _make_motor()
        motor.is_ready = False

        # Verify clean initial state
        assert motor._pending_model_switch is None

        # Exercise the real dispatch handler
        motor._dispatch_command("switch_model", "gemma4:e4b")

        # SPEC: _pending_model_switch must remain None when offline
        assert motor._pending_model_switch is None, (
            "switch_model must NOT queue a pending switch when Ollama is offline. "
            f"Got _pending_model_switch={motor._pending_model_switch!r}"
        )
        # SPEC: ui_callback must receive model_switch_failed
        ui_cb.assert_called_with("model_switch_failed")

    # --- Spec 3: Auto-enable when Ollama becomes ready --------------------

    def test_ui_controls_auto_enabled_when_ollama_becomes_ready(
        self, mock_ctk, ui_state, dispatcher, on_log,
    ):
        """UI must auto-enable when ollama_state transitions to 'ready'."""
        from config.settings import DEFAULT_MODEL

        ui_state.ollama_state = "service_stopped"

        with patch("ui.model_panel.resolve_startup_model", return_value=(DEFAULT_MODEL, "default")):
            with patch.dict("sys.modules", {"customtkinter": mock_ctk}):
                panel = ModelPanel(
                    parent_frame=MagicMock(),
                    ui_state=ui_state,
                    dispatcher=dispatcher,
                    on_log=on_log,
                )
                with patch("ui.model_panel.ctk", mock_ctk):
                    panel.build()

        # Pre-condition: should be disabled (part of Spec 1)
        assert panel.combo_modelos.state == "disabled", (
            "Pre-condition: combo must start disabled when offline"
        )

        # Transition to ready via observer
        ui_state.ollama_state = "ready"
        # Give observer dispatch thread time to propagate
        time.sleep(0.15)

        assert panel.combo_modelos.state == "normal", (
            "combo_modelos must auto-enable when Ollama becomes ready"
        )
        panel.cleanup()

    # --- Spec 4: Model warming guards + visual feedback -------------------

    def test_model_warming_guards_and_visual_feedback(self, built_panel, ui_state):
        """During model loading, combo must disable and label must show progress."""
        # Set Ollama ready first so combo is enabled
        ui_state.ollama_state = "ready"
        built_panel._on_state_change("ollama_state", "ready")

        # Trigger model warming state
        built_panel._on_state_change("model_status", "loading")

        assert built_panel.combo_modelos.state == "disabled", (
            "combo_modelos must be disabled during model warming"
        )
        label_text = built_panel.lbl_modelo_info.text
        assert any(word in label_text for word in ("Cargando", "Preparando", "Loading")), (
            f"Model info label must show loading feedback, got: {label_text!r}"
        )


# ═══════════════════════════════════════════════════════════════════════════
# EDGE CASE TESTS — production robustness scenarios
# ═══════════════════════════════════════════════════════════════════════════


class TestEdgeCaseRapidModelSwitch:
    """EC-2: User clicks model A then immediately model B during warm-up."""

    def test_rapid_switch_only_final_model_applies(self):
        """When two switches are queued rapidly, only the last model should apply.

        Prevents two concurrent _prepare_model calls from competing for VRAM.
        """
        motor, ui_cb = _make_motor()
        motor.is_ready = True
        motor._apply_model_switch = MagicMock()

        # Dispatch two switches rapidly via the real handler
        motor._dispatch_command("switch_model", "model_a")
        motor._dispatch_command("switch_model", "model_b")

        # After processing both, only model_b should be the target
        assert motor._desired_model == "model_b"


class TestEdgeCaseCheckingStateSelection:
    """EC-6: User selects a model while Ollama state is 'checking' (startup)."""

    def test_model_selection_during_checking_does_not_dispatch(
        self, built_panel, ui_state, dispatcher,
    ):
        """Model combo selection during 'checking' state should not dispatch switch.

        Currently, _on_model_changed dispatches on_switch_model even when
        ollama_state is not 'ready'. This test enforces the guard.
        """
        ui_state.ollama_state = "checking"

        received = []
        dispatcher.subscribe("on_switch_model", lambda t: received.append(t))

        from config.settings import MODELS_CATALOG

        tag = list(MODELS_CATALOG.keys())[0]
        display = MODELS_CATALOG[tag]["display"]

        with patch.object(built_panel, "update_model_info"):
            built_panel._on_model_changed(display)

        # SPEC: no switch should dispatch when Ollama is checking
        # Currently FAILS because _on_model_changed dispatches for any non-ready state
        assert len(received) == 0, (
            f"on_switch_model should NOT be dispatched when ollama_state='checking', "
            f"but got {received}"
        )


class TestEdgeCasePTTWhileOffline:
    """EC-9: User uses Push-to-Talk while Ollama is offline."""

    def test_ptt_offline_gives_immediate_feedback(self):
        """process_context with is_ready=False must notify immediately.

        In a live-streaming app, the user shouldn't speak for 10 seconds
        only to learn Ollama was offline after releasing PTT.
        """
        motor, ui_cb = _make_motor()
        motor.is_ready = False

        # Exercise the real dispatch handler
        motor._dispatch_command("process_context", "Hey chat, what do you think about this?")

        ui_cb.assert_called_with("ollama_unavailable")

    def test_ptt_offline_does_not_queue_in_priority(self):
        """When offline, PTT text must not be enqueued to priority queue."""
        motor, ui_cb = _make_motor()
        motor.is_ready = False

        initial_queue_len = len(motor._priority_queue)

        # Exercise the real dispatch handler
        motor._dispatch_command("process_context", "test ptt payload")

        assert len(motor._priority_queue) == initial_queue_len, (
            "PTT text must not be enqueued when Ollama is offline"
        )


class TestEdgeCaseStateFlapping:
    """EC-5: Ollama state flaps rapidly (health monitor noise)."""

    def test_rapid_state_changes_dont_cause_errors(self, built_panel, ui_state):
        """Rapid ollama_state transitions must not crash the observer dispatch."""
        states = ["checking", "ready", "service_stopped", "ready", "checking", "ready"]

        for state in states:
            ui_state.ollama_state = state

        # Give observer thread time to process all transitions
        time.sleep(0.2)

        # Panel should be in a consistent final state
        # (ready → button should not be in a broken state)
        final_state = ui_state.ollama_state
        assert final_state == "ready"
        # No assertion on specific button text — just verify no crash

    def test_rapid_state_changes_settle_to_final_state(self, built_panel, ui_state):
        """After rapid flapping, button must reflect the final stable state."""
        with patch.object(built_panel, "_modelo_instalado", return_value=True):
            ui_state.ollama_state = "service_stopped"
            ui_state.ollama_state = "ready"
            ui_state.ollama_state = "service_stopped"
            ui_state.ollama_state = "ready"
            time.sleep(0.2)

        # Final state is 'ready' → button should NOT show "Iniciar Ollama"
        assert built_panel.btn_download.text != "Iniciar Ollama", (
            "Button must reflect final state 'ready', not intermediate 'service_stopped'"
        )


class TestEdgeCaseOllamaDiesMidInference:
    """EC-1: Ollama process crashes during active inference."""

    def test_is_ready_false_after_ollama_crash(self):
        """If ollama.list() fails, is_ready must become False."""
        motor, ui_cb = _make_motor()
        motor.is_ready = True

        # Mock ollama module on the motor instance
        mock_ollama = MagicMock()
        mock_ollama.list.side_effect = ConnectionError("Connection refused")
        motor.ollama = mock_ollama

        result = motor._check_ollama_service()

        assert result is False
        assert motor.is_ready is False

    def test_check_ollama_service_notifies_ui(self):
        """When Ollama dies, ui_callback must receive 'ollama_unavailable'."""
        motor, ui_cb = _make_motor()
        motor.is_ready = True

        mock_ollama = MagicMock()
        mock_ollama.list.side_effect = ConnectionError("Connection refused")
        motor.ollama = mock_ollama

        motor._check_ollama_service(notify_unavailable=True)

        ui_cb.assert_called_with("ollama_unavailable")


class TestEdgeCaseTierSwitchWhileBusy:
    """EC-7: User clicks tier buttons while motor is processing."""

    def test_tier_buttons_disabled_during_processing(self, built_panel, ui_state):
        """Tier buttons should not be clickable when Ollama is not ready.

        Even if the button handler checks ollama_state, the UI should
        prevent interaction during non-ready states.
        """
        ui_state.ollama_state = "service_stopped"
        built_panel._update_button_for_ollama_state()

        for tier, button in built_panel._tier_buttons.items():
            assert button.state == "disabled", (
                f"Tier '{tier}' button must be disabled when Ollama is offline"
            )

    def test_tier_switch_rejected_when_ollama_not_ready(self, built_panel, ui_state, on_log):
        """Manual tier switch must be rejected with log when Ollama is offline."""
        ui_state.ollama_state = "service_stopped"

        # Simulate clicking a tier button
        built_panel._on_llm_tier_selected("fast")

        on_log.assert_any_call(
            "[Sistema] Ollama no esta listo para cambiar tier LLM."
        )
