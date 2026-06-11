"""Dedicated UI panel for Kira Co-host Agenda Mode."""

from __future__ import annotations

import re
import tkinter.messagebox as messagebox
from typing import Any, Callable, Optional

import customtkinter as ctk


class CoHostAgendaPanel:
    """Operator-facing controls for the Co-host Agenda product tab."""

    PRIORITIES = ["Alta", "Normal", "Baja"]
    RESPONSE_LENGTHS = ["Corta", "Normal", "Expandida"]
    RHYTHMS = ["Calmo", "Normal", "Dinámico"]
    SAFETY_MODES = ["Live_safe", "Monologue", "Test"]
    TURN_LIMITS = [str(value) for value in range(1, 21)]
    DEFAULT_TURN_LIMIT = "3"
    MAX_TAGS = 12
    BULK_MAX_TOPICS = 20
    BULK_MAX_CHARS = 12000
    BULK_CODE_PATTERNS = (
        r"```",
        r"<\/?[a-z][^>]*>",
        r"\b(function|class|import|from|select|insert|update|delete|drop|script|console\.log)\b",
        r"=>",
    )

    def __init__(
        self,
        *,
        on_add_topic: Optional[Callable[[str, str, list[str], str, str, int], None]] = None,
        on_remove_topic: Optional[Callable[[int], None]] = None,
        on_move_topic: Optional[Callable[[int, int], None]] = None,
        on_select_profile: Optional[Callable[[str], None]] = None,
        on_save_profile: Optional[Callable[[str, str, str, str], None]] = None,
        on_session_settings: Optional[Callable[[int, str, str, str], None]] = None,
        on_enable: Optional[Callable[[], None]] = None,
        on_soft_stop: Optional[Callable[[], None]] = None,
        on_emergency_stop: Optional[Callable[[], None]] = None,
        on_approve_suggestion: Optional[Callable[[str], None]] = None,
        on_reject_suggestion: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._on_add_topic = on_add_topic or (lambda title, angle, constraints, priority, length, turns: None)
        self._on_session_settings = on_session_settings or (lambda turns, rhythm, length, safety_mode: None)
        self._on_remove_topic = on_remove_topic or (lambda index: None)
        self._on_move_topic = on_move_topic or (lambda index, direction: None)
        self._on_select_profile = on_select_profile or (lambda name: None)
        self._on_save_profile = on_save_profile or (lambda name, style, priority, length: None)
        self._on_enable = on_enable or (lambda: None)
        self._on_soft_stop = on_soft_stop or (lambda: None)
        self._on_emergency_stop = on_emergency_stop or (lambda: None)
        self._on_approve_suggestion = on_approve_suggestion or (lambda topic_id: None)
        self._on_reject_suggestion = on_reject_suggestion or (lambda topic_id: None)
        self._widgets: dict[str, Any] = {}
        self._constraint_tags: list[str] = []
        self._profiles: dict[str, dict[str, Any]] = {}
        self._suggestion_widgets: list[dict[str, Any]] = []
        self._profile_expanded = False

    def build(self, parent: Any) -> Any:
        parent.grid_columnconfigure(0, weight=1)
        parent.grid_rowconfigure(0, weight=1)

        root = ctk.CTkScrollableFrame(parent, fg_color="#0f151c", corner_radius=18)
        root.grid(row=0, column=0, sticky="nsew", padx=0, pady=0)
        root.grid_columnconfigure(0, weight=1)
        root.grid_columnconfigure(1, weight=1)

        hero = ctk.CTkFrame(root, fg_color="#162232", corner_radius=18)
        hero.grid(row=0, column=0, columnspan=2, sticky="ew", padx=12, pady=(12, 8))
        hero.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(hero, text="Kira Co-host", font=ctk.CTkFont(size=24, weight="bold"), anchor="w").grid(row=0, column=0, sticky="ew", padx=16, pady=(14, 2))
        ctk.CTkLabel(
            hero,
            text="Agenda aprobada, turnos seguros y control humano. El espectador no ve la estructura: Kira debe sonar natural.",
            text_color="#a9bdd3",
            anchor="w",
            justify="left",
            wraplength=760,
        ).grid(row=1, column=0, sticky="ew", padx=16, pady=(0, 14))

        card_profile = ctk.CTkFrame(root, fg_color="#151d26", corner_radius=16)
        card_profile.grid(row=1, column=0, columnspan=2, sticky="ew", padx=12, pady=8)
        card_profile.grid_columnconfigure(0, weight=1)

        btn_toggle_profile = ctk.CTkButton(
            card_profile, text="▶ Perfil Co-host",
            anchor="w",
            fg_color="transparent", text_color="#d8e2ef",
            hover_color="#1d2a38",
            font=ctk.CTkFont(size=16, weight="bold"),
        )
        btn_toggle_profile.grid(row=0, column=0, sticky="ew", padx=14, pady=(12, 4))

        profile_content = ctk.CTkFrame(card_profile, fg_color="#101923", corner_radius=14)
        profile_content.grid(row=1, column=0, sticky="ew", padx=10, pady=(0, 10))
        profile_content.grid_remove()
        profile_content.grid_columnconfigure(0, weight=1)
        profile_content.grid_columnconfigure(1, weight=1)

        btn_toggle_profile.configure(
            command=lambda: self._toggle_profile_section(profile_content, btn_toggle_profile)
        )

        combo_profile = ctk.CTkOptionMenu(profile_content, values=["Natural"], command=self._dispatch_select_profile)
        combo_profile.set("Natural")
        combo_profile.grid(row=0, column=0, sticky="ew", padx=(14, 6), pady=4)
        self._widgets["combo_profile"] = combo_profile
        entry_profile_name = ctk.CTkEntry(profile_content, placeholder_text="Nombre del perfil Co-host")
        entry_profile_name.grid(row=0, column=1, sticky="ew", padx=(6, 14), pady=4)
        self._widgets["entry_profile_name"] = entry_profile_name
        style_text = ctk.CTkTextbox(profile_content, height=88, fg_color="#0f151c", text_color="#d8e2ef", wrap="word")
        style_text.grid(row=1, column=0, columnspan=2, sticky="ew", padx=14, pady=4)
        style_text.insert("end", "Soná como co-host natural de stream: cercana, con humor seco, sin anunciar estructura ni despedirte entre ideas.")
        self._widgets["text_profile_style"] = style_text
        ctk.CTkButton(profile_content, text="Guardar perfil Co-host", command=self._dispatch_save_profile, fg_color="#555555").grid(row=2, column=0, columnspan=2, sticky="ew", padx=14, pady=(4, 8))

        session = ctk.CTkFrame(profile_content, fg_color="#101923", corner_radius=12)
        session.grid(row=3, column=0, columnspan=2, sticky="ew", padx=14, pady=(0, 12))
        for col in range(4):
            session.grid_columnconfigure(col, weight=1)
        ctk.CTkLabel(session, text="Configuración de sesión", font=ctk.CTkFont(size=14, weight="bold"), anchor="w").grid(row=0, column=0, columnspan=4, sticky="ew", padx=12, pady=(10, 2))
        ctk.CTkLabel(session, text="Turnos globales", text_color="#a9bdd3", anchor="w").grid(row=1, column=0, sticky="ew", padx=12, pady=(2, 2))
        ctk.CTkLabel(session, text="Ritmo global", text_color="#a9bdd3", anchor="w").grid(row=1, column=1, sticky="ew", padx=12, pady=(2, 2))
        ctk.CTkLabel(session, text="Longitud global", text_color="#a9bdd3", anchor="w").grid(row=1, column=2, sticky="ew", padx=12, pady=(2, 2))
        ctk.CTkLabel(session, text="Modo vivo", text_color="#a9bdd3", anchor="w").grid(row=1, column=3, sticky="ew", padx=12, pady=(2, 2))
        turns = ctk.CTkOptionMenu(session, values=self.TURN_LIMITS)
        turns.set(self.DEFAULT_TURN_LIMIT)
        turns.grid(row=2, column=0, sticky="ew", padx=12, pady=(2, 10))
        self._widgets["combo_turns"] = turns

        rhythm = ctk.CTkOptionMenu(session, values=self.RHYTHMS)
        rhythm.set("Normal")
        rhythm.grid(row=2, column=1, sticky="ew", padx=12, pady=(2, 10))
        self._widgets["combo_rhythm"] = rhythm

        length = ctk.CTkOptionMenu(session, values=self.RESPONSE_LENGTHS)
        length.set("Normal")
        length.grid(row=2, column=2, sticky="ew", padx=12, pady=(2, 10))
        self._widgets["combo_length"] = length

        safety_mode = ctk.CTkOptionMenu(session, values=self.SAFETY_MODES)
        safety_mode.set("Live_safe")
        safety_mode.grid(row=2, column=3, sticky="ew", padx=12, pady=(2, 10))
        self._widgets["combo_safety_mode"] = safety_mode

        current = ctk.CTkFrame(root, fg_color="#151d26", corner_radius=16)
        current.grid(row=2, column=0, sticky="nsew", padx=(12, 6), pady=8)
        current.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(current, text="Ahora", font=ctk.CTkFont(size=16, weight="bold"), anchor="w").grid(row=0, column=0, sticky="ew", padx=14, pady=(12, 4))
        lbl_current = ctk.CTkLabel(current, text="Sin tema activo", text_color="#d8e2ef", anchor="w", justify="left", wraplength=360)
        lbl_current.grid(row=1, column=0, sticky="ew", padx=14, pady=(0, 8))
        self._widgets["lbl_current_topic"] = lbl_current
        lbl_state = ctk.CTkLabel(current, text="Estado: OFF · fallos: 0", text_color="#8fa3b8", anchor="w", justify="left", wraplength=360)
        lbl_state.grid(row=2, column=0, sticky="ew", padx=14, pady=(0, 12))
        self._widgets["lbl_state"] = lbl_state

        queue = ctk.CTkFrame(root, fg_color="#151d26", corner_radius=16)
        queue.grid(row=2, column=1, sticky="nsew", padx=(6, 12), pady=8)
        queue.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(queue, text="Cola de temas", font=ctk.CTkFont(size=16, weight="bold"), anchor="w").grid(row=0, column=0, sticky="ew", padx=14, pady=(12, 4))
        queue_text = ctk.CTkTextbox(queue, height=120, fg_color="#0f151c", text_color="#d8e2ef", wrap="word")
        queue_text.grid(row=1, column=0, sticky="ew", padx=14, pady=(0, 12))
        queue_text.insert("end", "No hay temas en cola todavía.\n")
        queue_text.configure(state="disabled")
        self._widgets["text_queue"] = queue_text

        queue_controls = ctk.CTkFrame(queue, fg_color="transparent")
        queue_controls.grid(row=2, column=0, sticky="ew", padx=14, pady=(0, 12))
        for col in range(4):
            queue_controls.grid_columnconfigure(col, weight=1)
        queue_index = ctk.CTkEntry(queue_controls, placeholder_text="Nº", width=48)
        queue_index.grid(row=0, column=0, padx=4, pady=4, sticky="ew")
        self._widgets["entry_queue_index"] = queue_index
        ctk.CTkButton(queue_controls, text="Subir", command=lambda: self._dispatch_move_topic(-1), fg_color="#555555").grid(row=0, column=1, padx=4, pady=4, sticky="ew")
        ctk.CTkButton(queue_controls, text="Bajar", command=lambda: self._dispatch_move_topic(1), fg_color="#555555").grid(row=0, column=2, padx=4, pady=4, sticky="ew")
        ctk.CTkButton(queue_controls, text="Eliminar", command=self._dispatch_remove_topic, fg_color="#8f2f2f").grid(row=0, column=3, padx=4, pady=4, sticky="ew")

        # Sugerencias de Kira — auto-generated topic suggestions
        suggestions_frame = ctk.CTkFrame(queue, fg_color="#101923", corner_radius=12)
        suggestions_frame.grid(row=3, column=0, sticky="ew", padx=14, pady=(4, 12))
        suggestions_frame.grid_columnconfigure(0, weight=1)
        self._widgets["suggestions_frame"] = suggestions_frame
        suggestions_header = ctk.CTkLabel(
            suggestions_frame,
            text="Sugerencias de Kira",
            font=ctk.CTkFont(size=14, weight="bold"),
            anchor="w",
        )
        suggestions_header.grid(row=0, column=0, sticky="ew", padx=12, pady=(10, 4))
        self._widgets["suggestions_header"] = suggestions_header
        suggestions_container = ctk.CTkFrame(suggestions_frame, fg_color="transparent")
        suggestions_container.grid(row=1, column=0, sticky="ew", padx=4, pady=(0, 6))
        suggestions_container.grid_columnconfigure(0, weight=1)
        self._widgets["suggestions_container"] = suggestions_container
        # Initially hidden until suggestions arrive
        suggestions_frame.grid_remove()

        session_controls = ctk.CTkFrame(root, fg_color="#151d26", corner_radius=16)
        session_controls.grid(row=3, column=0, columnspan=2, sticky="ew", padx=12, pady=8)
        for col in range(3):
            session_controls.grid_columnconfigure(col, weight=1)
        ctk.CTkLabel(session_controls, text="Control de sesión", font=ctk.CTkFont(size=16, weight="bold"), anchor="w").grid(row=0, column=0, columnspan=3, sticky="ew", padx=14, pady=(12, 4))
        btn_enable = ctk.CTkButton(session_controls, text="Activar", command=self._dispatch_enable, fg_color="#2f7d50", state="disabled")
        btn_enable.grid(row=1, column=0, padx=(14, 4), pady=(4, 14), sticky="ew")
        self._widgets["btn_agenda_enable"] = btn_enable
        btn_soft = ctk.CTkButton(session_controls, text="Stop suave", command=self._on_soft_stop, fg_color="#7d5a2a", state="disabled")
        btn_soft.grid(row=1, column=1, padx=4, pady=(4, 14), sticky="ew")
        self._widgets["btn_agenda_soft_stop"] = btn_soft
        btn_emergency = ctk.CTkButton(session_controls, text="Emergencia", command=self._on_emergency_stop, fg_color="#8f2f2f", state="disabled")
        btn_emergency.grid(row=1, column=2, padx=(4, 14), pady=(4, 14), sticky="ew")
        self._widgets["btn_agenda_emergency"] = btn_emergency
        lbl_activation_hint = ctk.CTkLabel(session_controls, text="", text_color="#8fa3b8", anchor="w", wraplength=760)
        lbl_activation_hint.grid(row=2, column=0, columnspan=3, sticky="ew", padx=14, pady=(0, 12))
        self._widgets["lbl_activation_hint"] = lbl_activation_hint

        # Collapsible: Nuevo tema aprobado
        card_nuevo = ctk.CTkFrame(root, fg_color="#151d26", corner_radius=16)
        card_nuevo.grid(row=4, column=0, columnspan=2, sticky="ew", padx=12, pady=8)
        card_nuevo.grid_columnconfigure(0, weight=1)

        self._topic_form_expanded = False
        btn_toggle_nuevo = ctk.CTkButton(
            card_nuevo, text="▶ Nuevo tema aprobado",
            anchor="w",
            fg_color="transparent", text_color="#d8e2ef",
            hover_color="#1d2a38",
            font=ctk.CTkFont(size=16, weight="bold"),
        )
        btn_toggle_nuevo.grid(row=0, column=0, sticky="ew", padx=14, pady=(12, 4))

        # Content frame for nuevo tema
        content_nuevo = ctk.CTkFrame(card_nuevo, fg_color="#101923", corner_radius=14)
        content_nuevo.grid(row=1, column=0, sticky="ew", padx=14, pady=(0, 14))
        content_nuevo.grid_remove()
        content_nuevo.grid_columnconfigure(0, weight=2)
        content_nuevo.grid_columnconfigure(1, weight=1)
        content_nuevo.grid_columnconfigure(2, weight=1)
        content_nuevo.grid_columnconfigure(3, weight=1)

        # Wire toggle
        btn_toggle_nuevo.configure(
            command=lambda: self._toggle_topic_form(content_nuevo, btn_toggle_nuevo)
        )

        entry_title = ctk.CTkEntry(content_nuevo, placeholder_text="Tema claro, máximo 90 caracteres")
        entry_title.grid(row=0, column=0, sticky="ew", padx=(14, 6), pady=4)
        self._widgets["entry_title"] = entry_title

        ctk.CTkLabel(content_nuevo, text="Prioridad en cola", text_color="#a9bdd3", anchor="w").grid(row=0, column=1, sticky="ew", padx=6, pady=(0, 0))

        priority = ctk.CTkOptionMenu(content_nuevo, values=self.PRIORITIES)
        priority.set("Normal")
        priority.grid(row=1, column=1, columnspan=2, sticky="ew", padx=6, pady=4)
        self._widgets["combo_priority"] = priority

        entry_angle = ctk.CTkEntry(content_nuevo, placeholder_text="Ángulo: cómo querés que Kira lo trate")
        entry_angle.grid(row=2, column=0, columnspan=4, sticky="ew", padx=14, pady=4)
        self._widgets["entry_angle"] = entry_angle

        tag_row = ctk.CTkFrame(content_nuevo, fg_color="transparent")
        tag_row.grid(row=3, column=0, columnspan=4, sticky="ew", padx=14, pady=4)
        tag_row.grid_columnconfigure(0, weight=1)
        entry_constraint = ctk.CTkEntry(tag_row, placeholder_text="Pegar hashtags/tags: #sinDespedidas, humor seco; no spoilers")
        entry_constraint.grid(row=0, column=0, sticky="ew", padx=(0, 6), pady=0)
        self._widgets["entry_constraint_tag"] = entry_constraint
        ctk.CTkButton(tag_row, text="Agregar tag", command=self._dispatch_add_constraint_tag, width=110, fg_color="#555555").grid(row=0, column=1, sticky="ew")
        tags = ctk.CTkLabel(content_nuevo, text="Tags: —", text_color="#8fa3b8", anchor="w", justify="left", wraplength=760)
        tags.grid(row=4, column=0, columnspan=4, sticky="ew", padx=14, pady=(0, 4))
        self._widgets["lbl_constraint_tags"] = tags

        actions = ctk.CTkFrame(content_nuevo, fg_color="transparent")
        actions.grid(row=5, column=0, columnspan=4, sticky="ew", padx=14, pady=(6, 14))
        for col in range(4):
            actions.grid_columnconfigure(col, weight=1)
        ctk.CTkButton(actions, text="Agregar a cola", command=self._dispatch_add_topic, fg_color="#2f5f8f").grid(row=0, column=0, padx=4, pady=4, sticky="ew")

        # Collapsible: Importar temas en lote
        card_bulk = ctk.CTkFrame(root, fg_color="#151d26", corner_radius=16)
        card_bulk.grid(row=5, column=0, columnspan=2, sticky="ew", padx=12, pady=8)
        card_bulk.grid_columnconfigure(0, weight=1)

        self._bulk_expanded = False
        btn_toggle_bulk = ctk.CTkButton(
            card_bulk, text="▶ Importar temas en lote",
            anchor="w",
            fg_color="transparent", text_color="#d8e2ef",
            hover_color="#1d2a38",
            font=ctk.CTkFont(size=16, weight="bold"),
        )
        btn_toggle_bulk.grid(row=0, column=0, sticky="ew", padx=14, pady=(12, 4))

        # Content frame for bulk import
        content_bulk = ctk.CTkFrame(card_bulk, fg_color="#101923", corner_radius=14)
        content_bulk.grid(row=1, column=0, sticky="ew", padx=14, pady=(0, 14))
        content_bulk.grid_remove()
        content_bulk.grid_columnconfigure(0, weight=1)

        btn_toggle_bulk.configure(
            command=lambda: self._toggle_bulk_section(content_bulk, btn_toggle_bulk)
        )

        bulk_text = ctk.CTkTextbox(content_bulk, height=120, fg_color="#0f151c", text_color="#d8e2ef", wrap="word")
        bulk_text.grid(row=0, column=0, sticky="ew", padx=12, pady=4)
        bulk_text.insert("end", "1. Sociedad Latam\nTema: La nostalgia noventera en internet\nÁngulo: Por qué volvemos a símbolos viejos cuando el presente pesa.\nPrioridad: alta\nRitmo: normal\nTags: #Latam #Nostalgia #Internet\n\n2. Gaming\nTema: Mods como cultura popular\nÁngulo: Comunidades chicas que terminan definiendo gustos enormes.\nLongitud: expandida\nPrioridad: baja\nTags: #Gaming #Mods")
        self._widgets["text_bulk_topics"] = bulk_text
        ctk.CTkButton(content_bulk, text="Importar temas", command=self._dispatch_bulk_import, fg_color="#2f5f8f").grid(row=1, column=0, sticky="ew", padx=12, pady=(4, 4))
        bulk_status = ctk.CTkLabel(content_bulk, text="Estructura por bloque: Tema, Ángulo, Prioridad alta|normal|baja, Tags. Ritmo/Longitud en importación se aceptan como metadatos legacy no autoritativos; mandan los controles globales. Los turnos son globales 1-20 desde el selector. Se ignora HTML/código y texto excesivo.", text_color="#8fa3b8", anchor="w", justify="left", wraplength=760)
        bulk_status.grid(row=2, column=0, sticky="ew", padx=12, pady=(0, 10))
        self._widgets["lbl_bulk_status"] = bulk_status

        guardrails = ctk.CTkLabel(
            root,
            text="Guardrails: se rechazan textos larguísimos, código, HTML, emojis/símbolos raros y demasiadas restricciones. Modos: Live_safe ≈25-40s/cap 1100; Monologue permite monólogos largos interruptibles/cap 3000; Test permite 60-90s/cap 6000. Longitud global: Corta ≈450 chars; Normal ≈1500 chars mini monólogo; Expandida = monólogo largo de prueba con cap 6000. Turnos globales 1-20.",
            text_color="#8fa3b8",
            anchor="w",
            justify="left",
            wraplength=760,
        )
        guardrails.grid(row=6, column=0, columnspan=2, sticky="ew", padx=12, pady=(0, 12))
        return root

    def _toggle_topic_form(self, content: Any, button: Any) -> None:
        self._topic_form_expanded = not self._topic_form_expanded
        if self._topic_form_expanded:
            content.grid()
            button.configure(text="▼ Nuevo tema aprobado")
        else:
            content.grid_remove()
            button.configure(text="▶ Nuevo tema aprobado")

    def _toggle_bulk_section(self, content: Any, button: Any) -> None:
        self._bulk_expanded = not self._bulk_expanded
        if self._bulk_expanded:
            content.grid()
            button.configure(text="▼ Importar temas en lote")
        else:
            content.grid_remove()
            button.configure(text="▶ Importar temas en lote")

    def _toggle_profile_section(self, content: Any, button: Any) -> None:
        self._profile_expanded = not self._profile_expanded
        if self._profile_expanded:
            content.grid()
            button.configure(text="▼ Perfil Co-host")
        else:
            content.grid_remove()
            button.configure(text="▶ Perfil Co-host")

    def _dispatch_add_constraint_tag(self) -> None:
        raw = self._get("entry_constraint_tag")
        for tag in self.parse_constraint_tags(raw):
            if tag not in self._constraint_tags and len(self._constraint_tags) < self.MAX_TAGS:
                self._constraint_tags.append(tag)
        entry = self._widgets.get("entry_constraint_tag")
        if entry is not None:
            entry.delete(0, "end")
        self._render_constraint_tags()

    def _render_constraint_tags(self) -> None:
        label = self._widgets.get("lbl_constraint_tags")
        if label is not None:
            label.configure(text="Tags: " + (", ".join(f"#{tag}" for tag in self._constraint_tags) if self._constraint_tags else "—"))

    @classmethod
    def normalize_constraint_tag(cls, value: str) -> str:
        tag = " ".join((value or "").strip().lstrip("#").split())
        tag = tag.strip(" ,;#")
        return tag[:120]

    @classmethod
    def parse_constraint_tags(cls, value: str) -> list[str]:
        parts = re.split(r"[\s,;]+|(?=#)", value or "")
        tags: list[str] = []
        for part in parts:
            tag = cls.normalize_constraint_tag(part)
            if tag and tag not in tags:
                tags.append(tag)
        return tags[: cls.MAX_TAGS]

    def _dispatch_add_topic(self) -> None:
        turns, rhythm, length, safety_mode = self._session_settings()
        self._on_session_settings(turns, rhythm, length, safety_mode)
        title = self._get("entry_title")
        angle = self._get("entry_angle")
        extra = self._get("entry_constraint_tag")
        constraints = list(self._constraint_tags)
        for tag in self.parse_constraint_tags(extra):
            if tag not in constraints and len(constraints) < self.MAX_TAGS:
                constraints.append(tag)
        priority = self._get("combo_priority").lower()
        self._on_add_topic(title, angle, constraints, priority, length, turns)
        self._clear_topic_form()

    def _session_settings(self) -> tuple[int, str, str, str]:
        turns = self.clamp_turn_limit(self._get("combo_turns"))
        rhythm = self.normalize_rhythm(self._get("combo_rhythm"))
        length = self.normalize_response_length(self._get("combo_length"))
        safety_mode = self.normalize_safety_mode(self._get("combo_safety_mode"))
        return turns, rhythm, length, safety_mode

    def _dispatch_enable(self) -> None:
        turns, rhythm, length, safety_mode = self._session_settings()
        self._on_session_settings(turns, rhythm, length, safety_mode)
        self._on_enable()

    def _clear_topic_form(self) -> None:
        for name in ("entry_title", "entry_angle", "entry_constraint_tag"):
            widget = self._widgets.get(name)
            if widget is not None:
                widget.delete(0, "end")
        self._constraint_tags.clear()
        self._render_constraint_tags()

    def _dispatch_bulk_import(self) -> None:
        turns, rhythm, length, safety_mode = self._session_settings()
        self._on_session_settings(turns, rhythm, length, safety_mode)
        widget = self._widgets.get("text_bulk_topics")
        raw = widget.get("1.0", "end") if widget is not None else ""
        priority = self._get("combo_priority").lower()
        topics, ignored = self.parse_bulk_topics(raw, default_priority=priority, default_length=length)
        for topic in topics:
            self._on_add_topic(topic["title"], topic.get("angle", ""), topic.get("tags", []), topic.get("priority", priority), length, turns)
        if widget is not None and topics:
            widget.delete("1.0", "end")
        self._clear_topic_form()
        status = self._widgets.get("lbl_bulk_status")
        if status is not None:
            msg = f"Importados {len(topics)} temas."
            if ignored:
                msg += f" Ignorados {ignored} por límite o formato inseguro."
            status.configure(text=msg)

    @classmethod
    def parse_bulk_topics(cls, raw: str, *, default_priority: str = "normal", default_length: str = "normal") -> tuple[list[dict[str, Any]], int]:
        text = cls._normalize_bulk_text(raw)
        if not text or cls._looks_like_code(text):
            return [], 1 if text else 0
        topics: list[dict[str, Any]] = []
        ignored = 0
        current: dict[str, Any] = {}
        pending_number_title = ""

        def flush() -> None:
            nonlocal current, ignored, pending_number_title
            title = (current.get("title") or pending_number_title or "").strip()
            if title:
                if len(topics) < cls.BULK_MAX_TOPICS:
                    topics.append({
                        "title": title,
                        "angle": (current.get("angle") or "").strip(),
                        "tags": cls.parse_constraint_tags(" ".join(current.get("tags", []))),
                        "priority": cls.normalize_priority(current.get("priority") or default_priority),
                        "response_length": cls.normalize_response_length(default_length),
                    })
                else:
                    ignored += 1
            elif current:
                ignored += 1
            current = {}
            pending_number_title = ""

        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            numbered = re.match(r"^\s*\d+[.)-]\s*(.+)$", line)
            if numbered and not any(line.lower().startswith(prefix) for prefix in ("tema:", "ángulo:", "angulo:", "tags:", "prioridad:", "ritmo:", "longitud:")):
                flush()
                pending_number_title = numbered.group(1).strip()
                continue
            field = re.match(r"^(tema|ángulo|angulo|tags?|prioridad|ritmo|longitud)\s*:\s*(.*)$", line, flags=re.IGNORECASE)
            if field:
                name = field.group(1).lower()
                value = field.group(2).strip()
                if name == "tema":
                    if current:
                        flush()
                    current["title"] = value or pending_number_title
                    pending_number_title = ""
                elif name in {"ángulo", "angulo"}:
                    current["angle"] = ((current.get("angle") or "") + " " + value).strip()
                elif name in {"tag", "tags"}:
                    current.setdefault("tags", []).append(value)
                elif name == "prioridad":
                    current["priority"] = value
                elif name in {"ritmo", "longitud"}:
                    current.setdefault("legacy_pacing", []).append(value)
                continue
            if current.get("angle"):
                current["angle"] = f"{current['angle']} {line}".strip()
            elif current.get("title"):
                current["angle"] = line
            elif pending_number_title:
                current["title"] = pending_number_title
                current["angle"] = line
                pending_number_title = ""
        flush()
        return topics, ignored

    @classmethod
    def _normalize_bulk_text(cls, raw: str) -> str:
        text = (raw or "")[: cls.BULK_MAX_CHARS]
        lines = [" ".join(line.strip().split()) for line in text.replace("\r", "\n").split("\n")]
        return "\n".join(line for line in lines if line)

    @classmethod
    def _looks_like_code(cls, text: str) -> bool:
        return any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in cls.BULK_CODE_PATTERNS)

    @staticmethod
    def normalize_priority(priority: object) -> str:
        normalized = str(priority or "normal").strip().lower()
        return normalized if normalized in {"alta", "normal", "baja"} else "normal"

    @staticmethod
    def normalize_response_length(response_length: object) -> str:
        normalized = str(response_length or "normal").strip().lower()
        aliases = {"extensa": "expandida", "extendida": "expandida", "largo": "expandida", "larga": "expandida"}
        normalized = aliases.get(normalized, normalized)
        return normalized if normalized in {"corta", "normal", "expandida"} else "normal"

    @staticmethod
    def normalize_rhythm(rhythm: object) -> str:
        normalized = str(rhythm or "normal").strip().lower()
        if normalized == "dinámico":
            return "dinamico"
        return normalized if normalized in {"calmo", "normal", "dinamico"} else "normal"

    @staticmethod
    def normalize_safety_mode(safety_mode: object) -> str:
        normalized = str(safety_mode or "live_safe").strip().lower().replace("-", "_")
        aliases = {"live": "live_safe", "seguro": "live_safe", "monologo": "monologue", "monólogo": "monologue", "prueba": "test"}
        normalized = aliases.get(normalized, normalized)
        return normalized if normalized in {"live_safe", "monologue", "test"} else "live_safe"

    @staticmethod
    def clamp_turn_limit(value: object) -> int:
        try:
            turns = int(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            turns = int(CoHostAgendaPanel.DEFAULT_TURN_LIMIT)
        return max(1, min(20, turns))

    def _dispatch_select_profile(self, name: str) -> None:
        self.apply_profile_selection(name)
        self._on_select_profile(name)

    def _dispatch_save_profile(self) -> None:
        name = self._get("entry_profile_name") or self._get("combo_profile") or "Natural"
        style_widget = self._widgets.get("text_profile_style")
        style = style_widget.get("1.0", "end").strip() if style_widget is not None else ""
        priority = self._get("combo_priority").lower()
        length = self._get("combo_length").lower()
        self._on_save_profile(name, style, priority, length)

    def _dispatch_remove_topic(self) -> None:
        index = self._queue_index()
        if index is not None:
            self._on_remove_topic(index)
        else:
            messagebox.showwarning("Índice inválido", "Ingresá un número de tema válido (la cola es 1-indexada).")

    def _dispatch_move_topic(self, direction: int) -> None:
        index = self._queue_index()
        if index is not None:
            self._on_move_topic(index, direction)
        else:
            messagebox.showwarning("Índice inválido", "Ingresá un número de tema válido (la cola es 1-indexada).")

    def _queue_index(self) -> int | None:
        raw = self._get("entry_queue_index")
        if not raw:
            return None
        try:
            value = int(raw)
        except ValueError:
            return None
        return value if value >= 1 else None

    def _get(self, name: str) -> str:
        widget = self._widgets.get(name)
        return widget.get().strip() if widget is not None else ""

    def set_profiles(self, profiles: dict[str, dict[str, Any]], selected: str = "Natural") -> None:
        self._profiles = profiles
        combo = self._widgets.get("combo_profile")
        names = list(profiles.keys()) or ["Natural"]
        if combo is not None:
            combo.configure(values=names)
            combo.set(selected if selected in names else names[0])
        self.apply_profile_selection(selected if selected in names else names[0])

    def apply_profile_selection(self, name: str) -> None:
        profile = self._profiles.get(name)
        if not profile:
            return
        style = self._widgets.get("text_profile_style")
        if style is not None:
            style.delete("1.0", "end")
            style.insert("end", profile.get("style", ""))
        priority = self._widgets.get("combo_priority")
        if priority is not None:
            priority.set(str(profile.get("default_priority", "normal")).capitalize())
        length = self._widgets.get("combo_length")
        if length is not None:
            length.set(str(profile.get("default_response_length", "normal")).capitalize())

    def update_status(self, *, state: str, current_topic: str, queue_lines: list[str], failures: int, error_code: str = "", error_reasons: list[str] | None = None) -> None:
        current = self._widgets.get("lbl_current_topic")
        if current is not None:
            current.configure(text=current_topic or "Sin tema activo")
        state_label = self._widgets.get("lbl_state")
        if state_label is not None:
            base = f"Estado: {state} · fallos: {failures}"
            if error_code:
                base += f" · ⚠ {error_code}"
            state_label.configure(text=base)
        queue = self._widgets.get("text_queue")
        if queue is not None:
            queue.configure(state="normal")
            queue.delete("1.0", "end")
            queue.insert("end", "\n".join(queue_lines) if queue_lines else "No hay temas en cola todavía.\n")
            queue.configure(state="disabled")
        self._update_session_buttons(state=state, has_queue=bool(queue_lines), has_current=bool(current_topic and current_topic != "Sin tema activo"))

    # ------------------------------------------------------------------
    # Sugerencias de Kira — auto-generated topic suggestions
    # ------------------------------------------------------------------

    _CONFIDENCE_COLORS: dict[str, str] = {
        "HIGH": "#2f7d50",
        "MEDIUM": "#7d5a2a",
        "LOW": "#555555",
    }
    _CONFIDENCE_LABELS: dict[str, str] = {
        "HIGH": "ALTA",
        "MEDIUM": "MEDIA",
        "LOW": "BAJA",
    }

    def update_suggestions(self, suggestions: list[dict]) -> None:
        """Render the 'Sugerencias de Kira' section with confidence badges."""
        frame = self._widgets.get("suggestions_frame")
        container = self._widgets.get("suggestions_container")
        if frame is None or container is None:
            return

        # Clear existing suggestion widgets
        for child in container.winfo_children():
            child.destroy()
        self._suggestion_widgets.clear()

        if not suggestions:
            frame.grid_remove()
            return

        frame.grid()
        # Sort by confidence: HIGH first
        conf_order = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
        sorted_suggestions = sorted(suggestions, key=lambda s: conf_order.get(s.get("confidence", "LOW"), 3))
        for row_idx, suggestion in enumerate(sorted_suggestions):
            self._render_suggestion(container, suggestion, row_idx)

    def _render_suggestion(self, parent: Any, suggestion: dict, row: int) -> None:
        """Render a single suggestion row: title, angle, confidence badge, buttons."""
        title = suggestion.get("title", "")
        angle = suggestion.get("angle", "")
        confidence = suggestion.get("confidence", "LOW")
        source = suggestion.get("source", "")
        topic_id = suggestion.get("topic_id", "")

        frame = ctk.CTkFrame(parent, fg_color="#0f151c", corner_radius=10)
        frame.grid(row=row, column=0, sticky="ew", padx=4, pady=2)
        frame.grid_columnconfigure(0, weight=1)

        # Row 0: confidence badge + source label
        badge_row = ctk.CTkFrame(frame, fg_color="transparent")
        badge_row.grid(row=0, column=0, sticky="ew", padx=8, pady=(6, 0))
        badge_color = self._CONFIDENCE_COLORS.get(confidence, "#555555")
        badge_text = self._CONFIDENCE_LABELS.get(confidence, "BAJA")
        badge = ctk.CTkLabel(
            badge_row,
            text=f"● {badge_text}",
            text_color=badge_color,
            font=ctk.CTkFont(size=11, weight="bold"),
            anchor="w",
        )
        badge.grid(row=0, column=0, sticky="w")
        if source:
            source_label = ctk.CTkLabel(
                badge_row,
                text=source,
                text_color="#8fa3b8",
                font=ctk.CTkFont(size=11),
                anchor="e",
            )
            source_label.grid(row=0, column=1, sticky="e")

        # Row 1: title (bold)
        title_label = ctk.CTkLabel(
            frame,
            text=title[:90],
            text_color="#d8e2ef",
            font=ctk.CTkFont(size=13, weight="bold"),
            anchor="w",
            justify="left",
        )
        title_label.grid(row=1, column=0, sticky="ew", padx=8, pady=(2, 0))

        # Row 2: angle preview (truncated 60 chars)
        angle_preview = angle[:60] + ("…" if len(angle) > 60 else "")
        angle_label = ctk.CTkLabel(
            frame,
            text=angle_preview or "Sin ángulo",
            text_color="#8fa3b8",
            font=ctk.CTkFont(size=11),
            anchor="w",
            justify="left",
            wraplength=340,
        )
        angle_label.grid(row=2, column=0, sticky="ew", padx=8, pady=(0, 4))

        # Row 3: approve + reject buttons
        button_row = ctk.CTkFrame(frame, fg_color="transparent")
        button_row.grid(row=3, column=0, sticky="ew", padx=8, pady=(0, 8))
        button_row.grid_columnconfigure(0, weight=1)
        button_row.grid_columnconfigure(1, weight=1)
        tid = topic_id  # capture for lambda
        ctk.CTkButton(
            button_row,
            text="Aprobar",
            command=lambda t=tid: self._on_approve_suggestion(t),
            fg_color="#2f7d50",
            height=28,
            font=ctk.CTkFont(size=12),
        ).grid(row=0, column=0, padx=(0, 4), sticky="ew")
        ctk.CTkButton(
            button_row,
            text="Rechazar",
            command=lambda t=tid: self._on_reject_suggestion(t),
            fg_color="#8f2f2f",
            height=28,
            font=ctk.CTkFont(size=12),
        ).grid(row=0, column=1, padx=(4, 0), sticky="ew")

        self._suggestion_widgets.append({"frame": frame, "topic_id": topic_id})

    def _update_session_buttons(self, *, state: str, has_queue: bool, has_current: bool) -> None:
        active_states = {"IDLE", "SELECT_TOPIC", "OPEN_TOPIC", "GENERATING", "REGENERATING_SAFE", "SPEAKING", "WAITING_SIGNAL", "HANDLE_STREAMER", "HANDLE_CHAT", "CONTINUE_TOPIC", "TOPIC_CLOSING"}
        enable_state = "normal" if has_queue and state == "OFF" and not has_current else "disabled"
        soft_state = "normal" if has_current or state in active_states else "disabled"
        emergency_state = "normal" if has_current or state in active_states else "disabled"
        mapping = {
            "btn_agenda_enable": enable_state,
            "btn_agenda_soft_stop": soft_state,
            "btn_agenda_emergency": emergency_state,
        }
        for name, desired in mapping.items():
            widget = self._widgets.get(name)
            if widget is not None:
                widget.configure(state=desired)
        # BUG-010: show WHY Activar is disabled
        hint = self._widgets.get("lbl_activation_hint")
        if hint is not None:
            if enable_state == "disabled":
                reasons = []
                if not has_queue:
                    reasons.append("la cola de temas está vacía")
                if state != "OFF":
                    reasons.append(f"la agenda está en estado {state}")
                if has_current:
                    reasons.append("ya hay un tema activo")
                hint.configure(text="Activar deshabilitado: " + "; ".join(reasons) + "." if reasons else "")
            else:
                hint.configure(text="")
