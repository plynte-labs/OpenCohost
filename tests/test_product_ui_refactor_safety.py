"""Safety checks for the Kira-centered product UI refactor.

These tests intentionally verify the current UI contract before visual layout
movement starts.  They are mostly source-level checks because the next phase is
about moving containers without changing behavior.
"""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP_SHELL = ROOT / "ui" / "app_shell.py"
AVATAR_PANEL = ROOT / "ui" / "avatar_panel.py"
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


def test_phase2_product_shell_uses_persistent_kira_and_workspace() -> None:
    """Phase 2 should introduce left Kira + right product workspace containers."""
    source = read_text(APP_SHELL)

    assert "Product shell: Kira stays visible on the left" in source
    assert "Product workspace: current configuration plus full Stream Admin" in source
    assert "Paneles de producto" in source
    assert "product_tabs.add(\"Configuración\")" in source
    assert "product_tabs.add(\"Stream\")" in source
    assert "self._product_workspace_panel" in source
    assert "self._product_tabs" in source


def test_stream_admin_container_moved_without_rewriting_internals() -> None:
    """StreamAdminUI should live in the Stream workspace with internals intact."""
    source = read_text(APP_SHELL)

    assert "Stream Admin panel — internals preserved" in source
    assert "ctk.CTkFrame(tab_product_stream" in source
    assert "self.stream_admin_ui = StreamAdminUI(" in source
    assert "self.stream_admin_ui.build(stream_admin_panel)" in source


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

    assert 'config_tabs.add("PTT")' not in source
    assert "ctk.CTkFrame(tab_cfg_model_profile" in source

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


def test_avatar_panel_is_gridded_into_its_parent() -> None:
    """AvatarPanel must attach its root frame, otherwise the tab renders empty."""
    source = read_text(AVATAR_PANEL)

    assert "self._frame.grid(row=0, column=0" in source
    assert "CTkScrollableFrame" in source
    assert "Elegir imagen" in source
    assert "Probar" in source


def test_kira_avatar_preview_does_not_fail_empty_silently() -> None:
    """Main Kira preview should show a visible fallback instead of blanking out."""
    source = read_text(APP_SHELL)

    assert "Error al cargar avatar" in source
    assert "Sin imagen para:" in source
    assert "No se pudo cargar preview" in source


def test_kira_avatar_preview_is_the_visual_hero() -> None:
    """Main Kira preview should be larger than the compact response card."""
    source = read_text(APP_SHELL)

    assert "minsize=460" in source
    assert "img.thumbnail((220, 220)" in source
    assert "height=220" in source


def test_kira_response_panel_is_compact_and_scrollable() -> None:
    """Kira response should not consume the left panel height."""
    source = read_text(APP_SHELL)

    assert "compact scrollable panel" in source
    assert "CTkTextbox" in source
    assert "height=130" in source
    assert "wrap=\"word\"" in source
    assert "font=ctk.CTkFont(size=14)" in source
