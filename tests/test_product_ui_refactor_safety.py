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
        "CoHostAgendaPanel",
        "MusicPanel",
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
    assert '("config", "Configuración")' in source
    assert '("stream", "Stream")' in source
    assert '("cohost", "Co-host")' in source
    assert '("music", "Música")' in source
    assert "self._product_workspace_panel" in source
    assert "self._product_tab_data" in source


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


def test_kira_agenda_mode_wiring_stays_out_of_llm_engine() -> None:
    """Agenda orchestration belongs in AppShell/controller, not core LLM engine."""
    source = read_text(APP_SHELL)

    assert "KiraAgendaController" in source
    assert "CoHostAgendaPanel" in source
    assert "load_cohost_profiles" in source
    assert "save_cohost_profiles" in source
    assert "on_remove_topic" in source
    assert "on_move_topic" in source
    assert "set_agenda_add_topic_callback" in source
    assert "set_agenda_enable_callback" in source
    assert "set_agenda_soft_stop_callback" in source
    assert "set_agenda_emergency_stop_callback" in source
    assert "self.smart_agg.on_aggregated_context = self._on_smart_aggregated_context" in source
    assert "drop_pending_sources((\"kira-agenda\",))" in source
    assert "_kira_agenda_has_higher_priority_pending" in source
    assert "Prefetch pausado: hay PTT/chat pendiente" in source
    assert "_kira_agenda_pending_compact_chat" in source


def test_music_mood_tab_is_wired_next_to_avatar_obs() -> None:
    """Music is a separate production module, not part of LLM/agenda logic."""
    source = read_text(APP_SHELL)

    assert "MusicLibrary" in source
    assert "AudioBedEngine" in source
    assert "MusicPanel" in source
    assert '("music", "Música")' in source
    assert '("avatar", "Avatar / OBS")' in source
    assert "self.music_panel = MusicPanel(" in source
    assert "on_delete_track=lambda track_id: self._music_delete_track(track_id)" in source
    assert "def _music_delete_track" in source
    assert "messagebox.askyesno" in source
    assert "self.audio_bed.duck()" in source
    assert "self.audio_bed.unduck()" in source


def test_music_panel_exposes_list_and_confirmed_delete_source_wiring() -> None:
    """Music panel must list imported tracks and expose a delete control through AppShell confirmation."""
    music_panel = read_text(ROOT / "ui" / "music_panel.py")
    app_shell = read_text(APP_SHELL)

    assert "combo_delete_track" in music_panel
    assert "Eliminar track" in music_panel
    assert "_dispatch_delete_track" in music_panel
    assert "_on_delete_track(track.id)" in music_panel
    assert "No se borran archivos fuente externos" in app_shell


def test_agenda_lifecycle_is_source_gated_and_prefetch_invalidates_on_interrupt() -> None:
    """Agenda state must only advance for agenda-origin speech, never PTT/chat/direct."""
    source = read_text(APP_SHELL)

    assert "def _is_kira_agenda_speech_source" in source
    assert "current_speech_source" in source
    assert "startswith(\"kira-agenda\")" in source
    assert "mark_generation_accepted()" in source
    # Post-fix: checks use controller state (SPEAKING/GENERATING), not motor source
    assert "AgendaState.SPEAKING, AgendaState.GENERATING" in source
    assert "agenda_speech = " in source
    assert "self._kira_agenda_clear_prefetch()" in source


def test_destructive_music_and_agenda_cleanup_require_confirmation() -> None:
    source = read_text(APP_SHELL)

    assert "Limpiar faltantes" in source
    assert "messagebox.askyesno" in source
    assert "Eliminar tema de agenda" in source
    assert "¿Eliminar de la cola" in source


def test_audio_bed_state_mutation_is_locked() -> None:
    source = read_text(ROOT / "core" / "audio_bed.py")

    assert "threading.RLock()" in source
    assert "with self._lock:" in source


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
    assert "height=140" in source


def test_kira_response_panel_is_compact_and_scrollable() -> None:
    """Kira response should not consume the left panel height."""
    source = read_text(APP_SHELL)

    assert "compact scrollable panel" in source
    assert "CTkTextbox" in source
    assert "height=130" in source
    assert "wrap=\"word\"" in source
    assert "font=ctk.CTkFont(size=14)" in source
