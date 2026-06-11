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
    assert "Turnos globales" in text
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
    assert "Los turnos son globales 1-20" in text
    assert "metadatos legacy no autoritativos" in text


def test_session_control_buttons_are_outside_topic_form_and_stateful() -> None:
    text = source()

    assert "Control de sesión" in text
    assert "btn_agenda_enable" in text
    assert "btn_agenda_soft_stop" in text
    assert "btn_agenda_emergency" in text
    assert "_update_session_buttons" in text
    assert "state=\"disabled\"" in text
