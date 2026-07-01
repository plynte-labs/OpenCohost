"""TDD tests for the Sistema rollup pill — Part B of ui_declutter_20260614.

RED phase: these tests should FAIL before implementation, then GREEN after.

Covers:
- All 5 severity levels: CRIT, WARN, INFO, QUIET, OK
- Priority ordering (CRIT beats WARN, WARN beats INFO, etc.)
- Each dimension driving the correct severity
- Steady-state OK
- update_* methods write _sistema_state even when individual pill widget is None
- Rollup still fires when lbl_model_status_pill is None (backward compat)
- Engine badge visibility gating (owner decision: qwen_starting → visible/INFO)
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from opencohost.ui.state import UIState
from opencohost.ui.status_bar import StatusBar


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def ui_state():
    s = UIState()
    yield s
    s.shutdown(timeout=2.0)


@pytest.fixture()
def mock_ctk():
    mock = MagicMock()

    class MockLabel:
        def __init__(self, *a, **kw):
            self.text = kw.get("text", "")
            self.fg_color = kw.get("fg_color", "")
            self.text_color = kw.get("text_color", "")
            self.corner_radius = kw.get("corner_radius", 0)
            self.font = kw.get("font", None)
            self._kwargs = kw

        def configure(self, **kw):
            for k, v in kw.items():
                setattr(self, k, v)

        def pack(self, **kw):
            pass

        def grid_remove(self):
            pass

        def grid(self, **kw):
            pass

    class MockFont:
        def __init__(self, **kw):
            pass

    mock.CTkLabel = MockLabel
    mock.CTkFont = MockFont
    return mock


@pytest.fixture()
def status_bar(mock_ctk, ui_state):
    """StatusBar with mocked CTk — pills created."""
    mock_parent = MagicMock()
    with patch("opencohost.ui.status_bar.ctk", mock_ctk):
        bar = StatusBar(parent_frame=mock_parent, ui_state=ui_state)
        bar.create_status_pills()
    yield bar
    bar.cleanup()


# ---------------------------------------------------------------------------
# 1. _sistema_state dict exists after create_status_pills
# ---------------------------------------------------------------------------


class TestSistemaStateInit:
    def test_sistema_state_dict_exists(self, status_bar):
        """StatusBar must have a _sistema_state dict after create_status_pills."""
        assert hasattr(status_bar, "_sistema_state"), (
            "StatusBar must expose _sistema_state dict"
        )
        assert isinstance(status_bar._sistema_state, dict)

    def test_sistema_state_has_required_dimensions(self, status_bar):
        """_sistema_state must have model, mic, tts, health keys."""
        state = status_bar._sistema_state
        for key in ("model", "mic", "tts", "health"):
            assert key in state, f"_sistema_state must contain dimension '{key}'"

    def test_lbl_sistema_pill_exists(self, status_bar):
        """StatusBar must expose lbl_sistema_pill after create_status_pills."""
        assert hasattr(status_bar, "lbl_sistema_pill"), (
            "StatusBar must expose lbl_sistema_pill widget"
        )
        assert status_bar.lbl_sistema_pill is not None


# ---------------------------------------------------------------------------
# 2. Rollup severity levels
# ---------------------------------------------------------------------------


class TestRollupSeverityLevels:
    """Test each severity level from the priority table."""

    def _set_dimensions(self, status_bar, **kwargs):
        """Directly set _sistema_state dimensions and trigger recompute."""
        status_bar._sistema_state.update(kwargs)
        status_bar._recompute_rollup()

    def test_crit_on_model_error(self, status_bar):
        """model=error → CRIT → 'Sistema: error' (red)."""
        self._set_dimensions(status_bar,
            model="error", mic="idle", tts="idle", health="green")
        assert status_bar.lbl_sistema_pill.text == "Sistema: error · modelo"
        assert status_bar.lbl_sistema_pill.fg_color == "#cc3333"

    def test_crit_on_health_red(self, status_bar):
        """health=red → CRIT → 'Sistema: error' (red)."""
        self._set_dimensions(status_bar,
            model="ready", mic="idle", tts="idle", health="red")
        assert status_bar.lbl_sistema_pill.text == "Sistema: error · salud"
        assert status_bar.lbl_sistema_pill.fg_color == "#cc3333"

    def test_crit_on_tts_error(self, status_bar):
        """tts=error → CRIT → 'Sistema: error' (red)."""
        self._set_dimensions(status_bar,
            model="ready", mic="idle", tts="error", health="green")
        assert status_bar.lbl_sistema_pill.text == "Sistema: error · TTS"
        assert status_bar.lbl_sistema_pill.fg_color == "#cc3333"

    def test_warn_on_model_loading(self, status_bar):
        """model=loading → WARN → 'Sistema: alerta' (amber)."""
        self._set_dimensions(status_bar,
            model="loading", mic="idle", tts="idle", health="green")
        assert status_bar.lbl_sistema_pill.text == "Sistema: alerta · modelo"
        assert status_bar.lbl_sistema_pill.fg_color == "#cc8800"

    def test_warn_on_health_yellow(self, status_bar):
        """health=yellow → WARN → 'Sistema: alerta' (amber)."""
        self._set_dimensions(status_bar,
            model="ready", mic="idle", tts="idle", health="yellow")
        assert status_bar.lbl_sistema_pill.text == "Sistema: alerta · salud"
        assert status_bar.lbl_sistema_pill.fg_color == "#cc8800"

    def test_warn_on_mic_disconnected(self, status_bar):
        """mic=disconnected → WARN → 'Sistema: alerta' (amber)."""
        self._set_dimensions(status_bar,
            model="ready", mic="disconnected", tts="idle", health="green")
        assert status_bar.lbl_sistema_pill.text == "Sistema: alerta · mic"
        assert status_bar.lbl_sistema_pill.fg_color == "#cc8800"

    def test_info_on_tts_generating(self, status_bar):
        """tts=generating (all else OK) → INFO → 'Sistema: activo' (blue)."""
        self._set_dimensions(status_bar,
            model="ready", mic="idle", tts="generating", health="green")
        assert status_bar.lbl_sistema_pill.text == "Sistema: activo"
        assert status_bar.lbl_sistema_pill.fg_color == "#1f3f6f"

    def test_info_on_tts_paused(self, status_bar):
        """tts=paused (all else OK) → INFO → 'Sistema: activo' (blue)."""
        self._set_dimensions(status_bar,
            model="ready", mic="idle", tts="paused", health="green")
        assert status_bar.lbl_sistema_pill.text == "Sistema: activo"
        assert status_bar.lbl_sistema_pill.fg_color == "#1f3f6f"

    def test_info_on_tts_speaking(self, status_bar):
        """tts=speaking (Kira actively speaking, all else OK) → INFO → 'Sistema: activo' (blue).

        FIX B: tts=speaking is an active operational state, not OK.
        """
        self._set_dimensions(status_bar,
            model="ready", mic="idle", tts="speaking", health="green")
        assert status_bar.lbl_sistema_pill.text == "Sistema: activo"
        assert status_bar.lbl_sistema_pill.fg_color == "#1f3f6f"

    def test_info_on_mic_recording(self, status_bar):
        """mic=recording (all else OK) → INFO → 'Sistema: activo' (blue)."""
        self._set_dimensions(status_bar,
            model="ready", mic="recording", tts="idle", health="green")
        assert status_bar.lbl_sistema_pill.text == "Sistema: activo"
        assert status_bar.lbl_sistema_pill.fg_color == "#1f3f6f"

    def test_info_on_mic_listening(self, status_bar):
        """mic=listening (PTT actively listening, all else OK) → INFO → 'Sistema: activo' (blue).

        FIX B: mic=listening is an active operational state, not OK.
        """
        self._set_dimensions(status_bar,
            model="ready", mic="listening", tts="idle", health="green")
        assert status_bar.lbl_sistema_pill.text == "Sistema: activo"
        assert status_bar.lbl_sistema_pill.fg_color == "#1f3f6f"

    def test_tts_speaking_downgrades_to_ok_on_idle(self, status_bar):
        """tts=speaking → INFO; then tts=idle (all else OK) → OK (recovery)."""
        self._set_dimensions(status_bar,
            model="ready", mic="idle", tts="speaking", health="green")
        assert status_bar.lbl_sistema_pill.text == "Sistema: activo"

        self._set_dimensions(status_bar,
            model="ready", mic="idle", tts="idle", health="green")
        assert status_bar.lbl_sistema_pill.text == "Sistema: OK"

    def test_mic_listening_downgrades_to_ok_on_idle(self, status_bar):
        """mic=listening → INFO; then mic=idle (all else OK) → OK (recovery)."""
        self._set_dimensions(status_bar,
            model="ready", mic="listening", tts="idle", health="green")
        assert status_bar.lbl_sistema_pill.text == "Sistema: activo"

        self._set_dimensions(status_bar,
            model="ready", mic="idle", tts="idle", health="green")
        assert status_bar.lbl_sistema_pill.text == "Sistema: OK"

    def test_quiet_on_health_unknown(self, status_bar):
        """health=unknown (no other degradation) → QUIET → 'Sistema: ...' (grey)."""
        self._set_dimensions(status_bar,
            model="ready", mic="idle", tts="idle", health="unknown")
        assert status_bar.lbl_sistema_pill.text == "Sistema: ..."
        assert status_bar.lbl_sistema_pill.fg_color == "#444444"

    def test_ok_on_all_nominal(self, status_bar):
        """All nominal → OK → 'Sistema: OK' (dim dark)."""
        self._set_dimensions(status_bar,
            model="ready", mic="idle", tts="idle", health="green")
        assert status_bar.lbl_sistema_pill.text == "Sistema: OK"
        assert status_bar.lbl_sistema_pill.fg_color == "#1b2633"

    def test_crit_names_all_failing_dimensions(self, status_bar):
        """Multiple CRIT triggers → Sistema names each degraded dimension in order."""
        self._set_dimensions(status_bar,
            model="error", mic="idle", tts="error", health="red")
        assert status_bar.lbl_sistema_pill.text == "Sistema: error · modelo, salud, TTS"
        assert status_bar.lbl_sistema_pill.fg_color == "#cc3333"


# ---------------------------------------------------------------------------
# 3. Priority ordering
# ---------------------------------------------------------------------------


class TestRollupPriority:
    def _set_dimensions(self, status_bar, **kwargs):
        status_bar._sistema_state.update(kwargs)
        status_bar._recompute_rollup()

    def test_crit_beats_warn(self, status_bar):
        """CRIT (model=error) beats WARN (mic=disconnected)."""
        self._set_dimensions(status_bar,
            model="error", mic="disconnected", tts="idle", health="green")
        assert "error" in status_bar.lbl_sistema_pill.text

    def test_crit_beats_info(self, status_bar):
        """CRIT (tts=error) beats INFO (mic=recording)."""
        self._set_dimensions(status_bar,
            model="ready", mic="recording", tts="error", health="green")
        assert "error" in status_bar.lbl_sistema_pill.text

    def test_warn_beats_info(self, status_bar):
        """WARN (health=yellow) beats INFO (tts=generating)."""
        self._set_dimensions(status_bar,
            model="ready", mic="idle", tts="generating", health="yellow")
        assert "alerta" in status_bar.lbl_sistema_pill.text

    def test_warn_beats_quiet(self, status_bar):
        """WARN (model=loading) beats QUIET (health=unknown)."""
        self._set_dimensions(status_bar,
            model="loading", mic="idle", tts="idle", health="unknown")
        assert "alerta" in status_bar.lbl_sistema_pill.text

    def test_info_beats_quiet(self, status_bar):
        """INFO (tts=generating) beats QUIET (health=unknown)."""
        self._set_dimensions(status_bar,
            model="ready", mic="idle", tts="generating", health="unknown")
        assert "activo" in status_bar.lbl_sistema_pill.text


# ---------------------------------------------------------------------------
# 4. update_* methods write _sistema_state
# ---------------------------------------------------------------------------


class TestUpdateMethodsWriteSistemaState:
    def test_update_model_status_writes_sistema_state(self, status_bar):
        """update_model_status('error') must update _sistema_state['model']."""
        status_bar.update_model_status("error")
        assert status_bar._sistema_state["model"] == "error"

    def test_update_mic_status_writes_sistema_state(self, status_bar):
        """update_mic_status('disconnected') must update _sistema_state['mic']."""
        status_bar.update_mic_status("disconnected")
        assert status_bar._sistema_state["mic"] == "disconnected"

    def test_update_tts_status_writes_sistema_state(self, status_bar):
        """update_tts_status('error') must update _sistema_state['tts']."""
        status_bar.update_tts_status("error")
        assert status_bar._sistema_state["tts"] == "error"

    def test_update_health_status_writes_sistema_state(self, status_bar):
        """update_health_status('red') must update _sistema_state['health']."""
        status_bar.update_health_status("red")
        assert status_bar._sistema_state["health"] == "red"

    def test_update_model_status_triggers_rollup_recompute(self, status_bar):
        """update_model_status('error') must cause CRIT in the rollup pill."""
        # Start OK
        status_bar._sistema_state.update(
            model="ready", mic="idle", tts="idle", health="green"
        )
        status_bar._recompute_rollup()
        assert status_bar.lbl_sistema_pill.text == "Sistema: OK"

        # Error should trigger CRIT
        status_bar.update_model_status("error")
        assert status_bar.lbl_sistema_pill.text == "Sistema: error · modelo"

    def test_update_health_status_triggers_rollup_recompute(self, status_bar):
        """update_health_status('red') must cause CRIT in the rollup pill."""
        status_bar._sistema_state.update(
            model="ready", mic="idle", tts="idle", health="green"
        )
        status_bar._recompute_rollup()
        assert status_bar.lbl_sistema_pill.text == "Sistema: OK"

        status_bar.update_health_status("red")
        assert status_bar.lbl_sistema_pill.text == "Sistema: error · salud"


# ---------------------------------------------------------------------------
# 5. Backward compat — rollup fires even when individual pill is None
# ---------------------------------------------------------------------------


class TestRollupWithNilIndividualPill:
    def test_rollup_fires_when_model_pill_is_none(self, status_bar):
        """update_model_status('error') must update rollup even if lbl_model_status_pill is None."""
        status_bar.lbl_model_status_pill = None
        status_bar._sistema_state.update(
            model="ready", mic="idle", tts="idle", health="green"
        )
        status_bar._recompute_rollup()

        # Now trigger model error with individual pill gone
        status_bar.update_model_status("error")
        assert status_bar._sistema_state["model"] == "error"
        assert status_bar.lbl_sistema_pill.text == "Sistema: error · modelo"

    def test_rollup_fires_when_health_pill_is_none(self, status_bar):
        """update_health_status('red') must update rollup even if lbl_health_status_pill is None."""
        status_bar.lbl_health_status_pill = None
        status_bar._sistema_state.update(
            model="ready", mic="idle", tts="idle", health="green"
        )
        status_bar._recompute_rollup()

        status_bar.update_health_status("red")
        assert status_bar._sistema_state["health"] == "red"
        assert status_bar.lbl_sistema_pill.text == "Sistema: error · salud"

    def test_rollup_fires_when_tts_pill_is_none(self, status_bar):
        """update_tts_status('error') must update rollup even if lbl_tts_status_pill is None."""
        status_bar.lbl_tts_status_pill = None
        status_bar._sistema_state.update(
            model="ready", mic="idle", tts="idle", health="green"
        )
        status_bar._recompute_rollup()

        status_bar.update_tts_status("error")
        assert status_bar._sistema_state["tts"] == "error"
        assert status_bar.lbl_sistema_pill.text == "Sistema: error · TTS"

    def test_rollup_does_not_raise_when_sistema_pill_is_none(self, status_bar):
        """_recompute_rollup must not raise if lbl_sistema_pill is None."""
        status_bar.lbl_sistema_pill = None
        status_bar._sistema_state.update(
            model="error", mic="idle", tts="idle", health="green"
        )
        # Should not raise
        status_bar._recompute_rollup()


# ---------------------------------------------------------------------------
# 6. Engine badge visibility — owner decision
# ---------------------------------------------------------------------------


class TestEngineBadgeVisibility:
    """The Voz badge is visible only on fallback; dim on normal statuses."""

    def test_engine_badge_dim_on_qwen_active(self, status_bar):
        """qwen_active → badge uses dim color (near-invisible)."""
        status_bar.update_engine_status("qwen_active", "")
        # Design Decision D: qwen_active → dim
        assert status_bar.lbl_engine_status_pill.fg_color == "#1b2633"

    def test_engine_badge_dim_on_piper_local(self, status_bar):
        """piper_local → badge uses dim color."""
        status_bar.update_engine_status("piper_local", "")
        assert status_bar.lbl_engine_status_pill.fg_color == "#1b2633"

    def test_engine_badge_visible_on_edge_fallback(self, status_bar):
        """edge_fallback → badge uses visible amber color."""
        status_bar.update_engine_status("edge_fallback", "")
        # Should be amber/visible, not dim
        assert status_bar.lbl_engine_status_pill.fg_color != "#1b2633"

    def test_engine_badge_visible_on_not_configured(self, status_bar):
        """not_configured → badge uses visible color (not dim)."""
        status_bar.update_engine_status("not_configured", "")
        assert status_bar.lbl_engine_status_pill.fg_color != "#1b2633"

    def test_engine_badge_info_visible_on_qwen_starting(self, status_bar):
        """qwen_starting → OWNER DECISION: badge is INFO-visible (amber/blue).

        Rationale: Edge speaks during Qwen warmup; operator should understand
        the transient voice change. Badge must NOT be dim (#1b2633).
        """
        status_bar.update_engine_status("qwen_starting", "")
        # Owner decision: qwen_starting is visible (INFO-level), NOT dim
        assert status_bar.lbl_engine_status_pill.fg_color != "#1b2633", (
            "qwen_starting must show a visible (amber/blue) badge per owner decision"
        )
