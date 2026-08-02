"""Source-level safety tests for the dedicated Co-host Agenda panel."""

from pathlib import Path

from opencohost.smart_aggregator.kira_agenda_controller import KiraAgendaController, TopicStatus
from opencohost.ui.cohost_agenda_panel import CoHostAgendaPanel


ROOT = Path(__file__).resolve().parents[1]
PANEL = ROOT / "opencohost" / "ui" / "cohost_agenda_panel.py"


def source() -> str:
    return PANEL.read_text(encoding="utf-8")


def test_cohost_panel_exposes_operator_state_and_queue() -> None:
    text = source()

    assert "Kira Co-host" in text
    assert "lbl_current_topic" in text
    assert "text_queue" in text
    assert "entry_queue_index" in text
    assert "Eliminar" in text
    assert "Subir" in text
    assert "Bajar" in text
    assert "update_status" in text


def test_cohost_panel_exposes_priority_and_global_session_controls() -> None:
    text = source()

    assert "PRIORITIES" in text
    assert "RESPONSE_LENGTHS" in text
    assert "RHYTHMS" in text
    assert "combo_priority" in text
    assert "combo_length" in text
    assert "combo_rhythm" in text
    assert "combo_turns" in text
    assert "combo_safety_mode" in text
    assert "Configuración de sesión" in text
    assert "Intentos globales" in text
    assert "Ritmo global" in text
    assert "Longitud global" in text
    assert "Modo vivo" in text
    assert "Ritmo / longitud" not in text


def test_cohost_panel_exposes_editable_guarded_profiles() -> None:
    text = source()

    assert "Perfil Co-host" in text
    assert "combo_profile" in text
    assert "text_profile_style" in text
    assert "Guardar perfil Co-host" in text
    assert "set_profiles" in text


def test_cohost_panel_warns_about_untrusted_input() -> None:
    text = source()

    assert "código" in text
    assert "emojis" in text
    assert "larguísimos" in text
    assert "Guardrails" in text


def test_cohost_panel_uses_constraint_tags_instead_of_semicolon_field() -> None:
    text = source()

    assert "Agregar tag" in text
    assert "lbl_constraint_tags" in text
    assert "entry_constraints" not in text
    assert "split(\";\")" not in text


def test_cohost_panel_explains_length_modes_as_product_semantics() -> None:
    text = source()

    assert "Corta ≈450 chars" in text
    assert "Normal ≈1500 chars" in text
    assert "cap 6000" in text
    assert "Live_safe ≈25-40s" in text
    assert "Monologue permite monólogos largos interruptibles" in text


def test_bulk_parser_handles_numbered_ai_outline_and_queues_topics() -> None:
    pasted = """
    1. Sociedad Latam
    Tema: La nostalgia noventera en internet
    Ángulo: Explorar por qué tanta gente vuelve a símbolos viejos cuando el presente se siente caro, rápido y medio hostil.
    Tags: #Latam #Nostalgia #Internet

    2. Gaming
    Tema: Mods como cultura popular
    Ángulo: Comparar mods con escenas musicales: comunidades chicas que terminan definiendo gustos enormes.
    Tags: #Gaming, #Mods; #Cultura
    """
    parsed, ignored = CoHostAgendaPanel.parse_bulk_topics(pasted)
    controller = KiraAgendaController()

    for topic in parsed:
        created = controller.add_topic(topic["title"], topic["angle"], topic["tags"], approved=True)
        controller.queue_topic(created.id)

    assert ignored == 0
    assert [topic.title for topic in controller.queued_topics()] == [
        "La nostalgia noventera en internet",
        "Mods como cultura popular",
    ]
    assert controller.queued_topics()[0].constraints == ["Latam", "Nostalgia", "Internet"]
    assert all(topic.status == TopicStatus.QUEUED for topic in controller.queued_topics())


def test_bulk_parser_accepts_legacy_rhythm_and_length_but_globals_drive_prompt() -> None:
    pasted = """
    Tema: La IA como herramienta social
    Ángulo: Diferenciar herramienta de reemplazo mágico.
    Prioridad: alta
    Ritmo: extendida
    Tags: #IA #Sociedad

    Tema: Cultura de mods
    Ángulo: Comunidades chicas con impacto grande.
    Longitud: corta
    """

    parsed, ignored = CoHostAgendaPanel.parse_bulk_topics(
        pasted,
        default_priority="baja",
        default_length="normal",
    )

    assert ignored == 0
    assert parsed[0]["priority"] == "alta"
    assert parsed[0]["response_length"] == "normal"
    assert "max_turns" not in parsed[0]
    assert parsed[1]["priority"] == "baja"
    assert parsed[1]["response_length"] == "normal"
    assert "max_turns" not in parsed[1]

    controller = KiraAgendaController(response_length="expandida")
    topic = controller.add_topic(parsed[0]["title"], parsed[0]["angle"], parsed[0]["tags"], approved=True, response_length=parsed[0]["response_length"])
    controller.queue_topic(topic.id)
    controller.enable()

    assert "monólogo largo expandido" in controller.next_action().prompt


def test_bulk_parser_handles_repeated_tema_angle_tags_blocks_and_caps_import() -> None:
    pasted = "\n".join(f"Tema: Tema {idx}\nÁngulo: Ángulo {idx}\nTags: #Uno #Dos" for idx in range(25))

    parsed, ignored = CoHostAgendaPanel.parse_bulk_topics(pasted)

    assert len(parsed) == CoHostAgendaPanel.BULK_MAX_TOPICS
    assert ignored == 5


def test_tag_normalization_prevents_double_hash_and_duplicates() -> None:
    tags = CoHostAgendaPanel.parse_constraint_tags("#Latam ##Latam, #Gaming;#Gaming #NoSpoilers")

    assert tags == ["Latam", "Gaming", "NoSpoilers"]
    assert all(not tag.startswith("#") for tag in tags)


def test_bulk_parser_rejects_obvious_code_or_html() -> None:
    parsed, ignored = CoHostAgendaPanel.parse_bulk_topics("Tema: <script>alert(1)</script>\nÁngulo: hack")

    assert parsed == []
    assert ignored == 1


def test_cohost_panel_exposes_bulk_import_controls() -> None:
    text = source()

    assert "Importar temas en lote" in text
    assert "text_bulk_topics" in text
    assert "Importar temas" in text
    assert "parse_bulk_topics" in text
    assert "Prioridad: alta" in text
    assert "Ritmo: normal" in text
    assert "Longitud: expandida" in text
    assert "Los intentos son globales 1-20" in text
    assert "metadatos legacy no autoritativos" in text


def test_session_control_buttons_are_outside_topic_form_and_stateful() -> None:
    text = source()

    assert "Control de sesión" in text
    assert "btn_agenda_enable" in text
    assert "btn_agenda_soft_stop" in text
    assert "btn_agenda_emergency" in text
    assert "_update_session_buttons" in text
    assert "state=\"disabled\"" in text


# ---------------------------------------------------------------------------
# Phase 3 — Incremental batch rendering for update_suggestions (ADR-006 P3)
# ---------------------------------------------------------------------------
# These tests use a recording scheduler to drive batch callbacks manually,
# mirroring the TestKiraDebounce pattern from test_advanced_panel.py.
# Widgets are replaced with lightweight fakes so ctk is never imported at
# module level (avoids the OOM crash documented in the Phase 2 bugfix).
# ---------------------------------------------------------------------------


def _make_suggestion(idx: int, confidence: str = "HIGH") -> dict:
    """Build a minimal suggestion dict for testing."""
    return {
        "title": f"Suggestion {idx}",
        "angle": f"Angle {idx}",
        "confidence": confidence,
        "source": "test",
        "topic_id": f"topic-{idx}",
    }


class _FakeContainer:
    """Minimal widget stand-in for the suggestions container."""

    def __init__(self) -> None:
        self._children: list["_FakeFrame"] = []

    def winfo_children(self) -> list:
        return list(self._children)

    def _register_child(self, child: "_FakeFrame") -> None:
        self._children.append(child)


class _FakeFrame:
    """Minimal stand-in for a ctk.CTkFrame suggestion row."""

    def __init__(self, parent: _FakeContainer, row: int) -> None:
        self.parent = parent
        self.row = row
        self._destroyed = False
        parent._register_child(self)

    def destroy(self) -> None:
        self._destroyed = True
        self.parent._children = [c for c in self.parent._children if c is not self]


class _FakeSuggestionsFrame:
    """Stand-in for the top-level suggestions_frame."""

    def __init__(self) -> None:
        self._visible = True

    def grid(self, **_kw) -> None:
        self._visible = True

    def grid_remove(self) -> None:
        self._visible = False


def _make_panel_with_batch_scheduler(**extra_kwargs):
    """Return a (panel, scheduled_callbacks) pair.

    The panel is constructed with a recording scheduler that appends
    (delay_ms, fn) tuples to *scheduled_callbacks* instead of using
    real timers.  The _render_suggestion method is monkey-patched to
    record rendered rows without importing customtkinter.
    """
    scheduled_callbacks: list[tuple[int, object]] = []

    def _recording_scheduler(delay_ms: int, fn) -> None:
        scheduled_callbacks.append((delay_ms, fn))

    panel = CoHostAgendaPanel(
        schedule_ui_update_after=_recording_scheduler,
        **extra_kwargs,
    )

    container = _FakeContainer()
    frame = _FakeSuggestionsFrame()
    panel._widgets["suggestions_frame"] = frame
    panel._widgets["suggestions_container"] = container

    rendered_rows: list[tuple[int, dict]] = []

    def _fake_render(parent, suggestion, row):
        # Create a fake frame so winfo_children stays consistent
        _FakeFrame(parent, row)
        rendered_rows.append((row, suggestion))

    panel._render_suggestion = _fake_render  # type: ignore[method-assign]
    panel._rendered_rows = rendered_rows  # expose for assertions
    panel._fake_container = container

    return panel, scheduled_callbacks


class _FakeOptionMenu:
    """Minimal stand-in for a ctk.CTkOptionMenu."""

    def __init__(self, value: str = "") -> None:
        self._value = value

    def set(self, value: str) -> None:
        self._value = value

    def get(self) -> str:
        return self._value


class TestApplySessionSettings:
    """Restored controller settings must reach the session widgets.

    Regression for the restart-restore clobber: _dispatch_enable /
    _dispatch_add_topic / _dispatch_bulk_import push the WIDGET values via
    on_session_settings before acting, so widgets left at hardcoded defaults
    silently overwrite whatever persistence restored into the controller.
    """

    def _panel_with_fake_combos(self) -> CoHostAgendaPanel:
        panel = CoHostAgendaPanel()
        for name in ("combo_turns", "combo_rhythm", "combo_length", "combo_safety_mode"):
            panel._widgets[name] = _FakeOptionMenu()
        return panel

    def test_apply_session_settings_reflects_restored_values(self) -> None:
        panel = self._panel_with_fake_combos()

        panel.apply_session_settings(4, "calmo", "corta", "monologue")

        assert panel._widgets["combo_turns"].get() == "4"
        assert panel._widgets["combo_rhythm"].get() == "Calmo"
        assert panel._widgets["combo_length"].get() == "Corta"
        assert panel._widgets["combo_safety_mode"].get() == "Monologue"
        # Closing the loop: dispatch paths now push the restored values
        assert panel._session_settings() == (4, "calmo", "corta", "monologue")

    def test_apply_session_settings_handles_accented_rhythm(self) -> None:
        panel = self._panel_with_fake_combos()

        panel.apply_session_settings(3, "dinamico", "normal", "live_safe")

        assert panel._widgets["combo_rhythm"].get() == "Dinámico"

    def test_apply_session_settings_falls_back_on_unknown_values(self) -> None:
        panel = self._panel_with_fake_combos()

        panel.apply_session_settings("???", "marciano", "gigante", "yolo")

        assert panel._widgets["combo_rhythm"].get() == "Normal"
        assert panel._widgets["combo_length"].get() == "Normal"
        assert panel._widgets["combo_safety_mode"].get() == "Live_safe"

    def test_apply_session_settings_tolerates_missing_widgets(self) -> None:
        panel = CoHostAgendaPanel()  # no widgets built at all
        panel.apply_session_settings(3, "normal", "normal", "live_safe")  # must not raise


class TestBatchSuggestions:
    """Incremental batch rendering for update_suggestions (ADR-006 Phase 3)."""

    # (a) Large list: first chunk sync, rest scheduled, all rendered in order

    def test_large_list_first_chunk_sync_rest_scheduled_all_rendered(self) -> None:
        """50 items: first 20 rendered synchronously, remainder via scheduled callbacks."""
        panel, sched = _make_panel_with_batch_scheduler()
        suggestions = [_make_suggestion(i) for i in range(50)]

        panel.update_suggestions(suggestions)

        # First 20 rendered immediately (synchronous)
        assert len(panel._rendered_rows) == 20

        # Remaining 30 need at least one scheduled callback
        assert len(sched) >= 1

        # Fire all scheduled callbacks until none remain
        while sched:
            _delay, fn = sched.pop(0)
            fn()

        # All 50 rendered, in row order
        assert len(panel._rendered_rows) == 50
        row_indices = [r for r, _ in panel._rendered_rows]
        assert row_indices == list(range(50))

    # (b) Re-entry mid-batch: second call cancels first; only second list rendered

    def test_reentry_mid_batch_cancels_previous_renders_second_list(self) -> None:
        """A second update_suggestions call while the first batch is pending must
        supersede it — the final rendered set is exactly the second list."""
        panel, sched = _make_panel_with_batch_scheduler()

        first = [_make_suggestion(i) for i in range(50)]
        second = [_make_suggestion(100 + i) for i in range(5)]

        # Start first batch
        panel.update_suggestions(first)
        assert len(panel._rendered_rows) == 20  # first chunk rendered sync
        assert len(sched) >= 1  # continuation scheduled

        # Snapshot the render log: everything appended after this index
        # belongs to the second call (and any stale callbacks fired later).
        before_second = len(panel._rendered_rows)

        # Issue second update before the continuation fires
        panel.update_suggestions(second)

        second_ids = [s["topic_id"] for s in second]
        rendered_after_second = [
            s.get("topic_id") for _, s in panel._rendered_rows[before_second:]
        ]
        # Second list is small (5 <= BATCH_SIZE) → rendered fully and in order
        assert rendered_after_second == second_ids

        # Fire whatever is still scheduled — stale first-list callbacks must
        # be no-ops and append nothing
        while sched:
            _delay, fn = sched.pop(0)
            fn()

        rendered_final = [
            s.get("topic_id") for _, s in panel._rendered_rows[before_second:]
        ]
        assert rendered_final == second_ids, (
            f"Stale first-batch items found: {[t for t in rendered_final if t not in second_ids]}"
        )

    # (c) Small list (<= BATCH_SIZE): fully synchronous, zero scheduled callbacks

    def test_small_list_renders_synchronously_no_scheduler_calls(self) -> None:
        """A list of <= BATCH_SIZE items must render fully synchronously."""
        panel, sched = _make_panel_with_batch_scheduler()
        suggestions = [_make_suggestion(i) for i in range(20)]

        panel.update_suggestions(suggestions)

        assert len(panel._rendered_rows) == 20
        assert len(sched) == 0, "Small list must not schedule any deferred callbacks"

    # (d) Teardown safety: scheduled callback after cleanup is a no-op

    def test_scheduled_callback_after_cleanup_is_noop(self) -> None:
        """A pending batch callback that fires after cleanup() must not raise."""
        panel, sched = _make_panel_with_batch_scheduler()
        suggestions = [_make_suggestion(i) for i in range(50)]

        panel.update_suggestions(suggestions)
        assert len(sched) >= 1

        # Simulate teardown before the scheduled callback fires
        panel.cleanup()

        # Fire the stale callback — must be a silent no-op
        for _delay, fn in sched:
            fn()  # must not raise

    # (e) Yield delay constant is 10 ms

    def test_yield_delay_constant_is_10ms(self) -> None:
        """BATCH_YIELD_MS must equal 10 (ADR-006 Phase 3)."""
        assert CoHostAgendaPanel.BATCH_YIELD_MS == 10

    # (f) BATCH_SIZE constant is 20

    def test_batch_size_constant_is_20(self) -> None:
        """BATCH_SIZE must equal 20 (ADR-006 Phase 3)."""
        assert CoHostAgendaPanel.BATCH_SIZE == 20
