"""Safety checks for the Kira-centered product UI refactor.

These tests intentionally verify the current UI contract before visual layout
movement starts.  They are mostly source-level checks because the next phase is
about moving containers without changing behavior.
"""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP_SHELL = ROOT / "ui" / "app_shell.py"
INVENTORY = ROOT / "conductor" / "tracks" / "product_ui_kira_avatar_refactor_20260513" / "inventory.md"


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_inventory_covers_current_ui_modules_and_product_categories() -> None:
    """The refactor inventory must be the safety map for every major UI module."""
    inventory = read_text(INVENTORY)

    for module in (
        "ui/app_shell.py",
        "ui/voice_control.py",
        "ui/model_panel.py",
        "ui/profile_panel.py",
        "ui/smart_aggregator_ui.py",
        "ui/stream_admin_ui.py",
        "ui/advanced_panel.py",
    ):
        assert module in inventory

    for category in (
        "Kira left panel",
        "Agent / Brain",
        "Voice / Input",
        "Stream",
        "Avatar / OBS",
        "System",
        "Logs",
    ):
        assert category in inventory


def test_inventory_documents_duplicate_and_phantom_controls() -> None:
    """Known confusing controls must stay visible in the plan until resolved."""
    inventory = read_text(INVENTORY)

    for finding in (
        "Hablar",
        "Conectar LiveAudio",
        "Recording code",
        "Registrar logs en avanzado",
        "RF3 chat vs StreamAdmin authenticated chat",
        "Twitch button",
        "Storage UI",
    ):
        assert finding in inventory


def test_app_shell_still_composes_all_existing_panels() -> None:
    """Before redesign, AppShell must still instantiate every existing panel."""
    source = read_text(APP_SHELL)

    for panel_name in (
        "VoiceControlPanel",
        "ModelPanel",
        "ProfilePanel",
        "SmartAggregatorUI",
        "StreamAdminUI",
        "AdvancedModePanel",
        "StatusBar",
    ):
        assert panel_name in source


def test_agent_brain_callbacks_remain_wired() -> None:
    """Model, profile, and memory controls are the Agent/Brain contract."""
    source = read_text(APP_SHELL)

    assert "ModelPanel(" in source
    assert "ProfilePanel(" in source
    assert "self._model_dispatcher" in source
    assert "self._profile_dispatcher" in source
    assert "command=self._limpiar_historial" in source
    assert "clear_history" in source


def test_voice_input_callbacks_remain_wired() -> None:
    """Voice, recording, LiveAudio, and PTT controls must survive layout moves."""
    source = read_text(APP_SHELL)

    for callback in (
        "command=self._iniciar_grabacion",
        "command=self._cargar_voz",
        "command=self._toggle_websocket",
        "command=self._al_cambiar_motor_tts",
        "command=self._al_toggle_ptt",
        "command=self._mapear_hotkey",
    ):
        assert callback in source


def test_stream_and_logs_callbacks_remain_wired() -> None:
    """StreamAdmin, RF3 chat, and advanced logs stay reachable before redesign."""
    source = read_text(APP_SHELL)

    assert "self.smart_agg_ui.toggle_connection()" in source
    assert "StreamAdminUI(" in source
    assert "self._wire_stream_admin_callbacks()" in source
    assert "command=self._toggle_logs_panel" in source
    assert "AdvancedModePanel(" in source
