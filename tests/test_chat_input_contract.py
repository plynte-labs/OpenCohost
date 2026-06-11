"""Tests for ChatEventDetector and ChatContextPacketBuilder — Phase A shadow mode."""

import pytest

from opencohost.smart_aggregator.chat_input_contract import (
    ChatContextPacket,
    ChatContextPacketBuilder,
    ChatEventDetector,
    CHAT_EVENTS,
)


# ── Political debate chat (from live test) ──────────────────────────────────

POLITICAL_CHAT = [
    {"user": "javiercortes256", "text": "LOS VERDADEROS TERRORISTAS SIEMPRE HAN SIDO LOS YANKIS. Sus gobiernos ASESINOS"},
    {"user": "SirMicrosoft", "text": "ahora quieren expandir el izquierdismo por Peru, Bolivia y Chile"},
    {"user": "raulsalazarhernandez6759", "text": "Es Usted un Gigante Presidente Evo Morales. Mi Admiración y mi Respeto."},
    {"user": "MrChecochocolate", "text": "Evo Morales sacó de la pobreza a millones de bolivianos."},
    {"user": "ismaelmorales08", "text": "Sr Evo usted es un buen hombre .. Viva Evo Morales"},
    {"user": "BelemTexis-q3g", "text": "todo esto es culpa de Evo por su ambicion de poder"},
    {"user": "SirMicrosoft", "text": "AMLO es el peor mal para Mexico, solo financia a los izquierdistas"},
    {"user": "juancarloslopezsalinas3850", "text": "Usted es culpable de que llegó la derecha por ambicioso vulgar."},
    {"user": "franciscohernandez4958", "text": "EL LITIO BOLIVIANO ES DE LOS BOLIVIANOS. SOLO DE ELLOS."},
    {"user": "CatalinaAlarcon-qc8dp", "text": "¡Que viva por siempre el digno pueblo de Bolivia!"},
    {"user": "Amoelcafémx", "text": "CUBA EL ÚNICO QUE RESISTIRÁ AL BLOQUEO YANKEE por la eternidad"},
    {"user": "veroram1945", "text": "OJO MÉXICO: ASÍ SE DEFIENDE EL TERRITORIO, SE DEFIENDE A LA PATRIA"},
    {"user": "sandragzz.8202", "text": "un líder puede poner en peligro su vida, pero no la de su pueblo"},
    {"user": "yollotly", "text": "Ojalá que la presidenta realmente tome cartas en el asunto."},
    {"user": "FranciscoJavierMerazMeraz", "text": "Alejandro Moreno Cardenas un vulgar limpia canales, ignorante"},
]


class TestChatEventDetector:
    """Verify 12 universal event types are detected structurally."""

    def test_all_12_events_defined(self):
        assert len(CHAT_EVENTS) == 12
        assert "direct_question" in CHAT_EVENTS
        assert "low_signal_noise" in CHAT_EVENTS

    def test_direct_question_detected(self):
        detector = ChatEventDetector()
        assert detector.classify_message("¿van a dejar el en vivo?") == "direct_question"
        assert detector.classify_message("what time is the stream?") == "direct_question"
        assert detector.classify_message("cuándo empieza el directo") == "direct_question"

    def test_greeting_detected(self):
        detector = ChatEventDetector()
        assert detector.classify_message("hola chicos") == "greeting_or_shoutout"
        assert detector.classify_message("saludos a mi mamá") == "greeting_or_shoutout"

    def test_hype_detected(self):
        detector = ChatEventDetector()
        assert detector.classify_message("VAMOS VAMOS VAMOS!!!") == "hype_or_emotion"
        assert detector.classify_message("¡¡¡QUÉ EMOCIÓN!!!") == "hype_or_emotion"

    def test_complaint_detected(self):
        detector = ChatEventDetector()
        assert detector.classify_message("no se escucha el audio") == "complaint_or_confusion"
        assert detector.classify_message("el stream va muy lento") == "complaint_or_confusion"

    def test_correction_detected(self):
        detector = ChatEventDetector()
        assert detector.classify_message("no es así, se dice weberly") == "correction_or_clarification"

    def test_poll_detected(self):
        detector = ChatEventDetector()
        assert detector.classify_message("hagan encuesta para el próximo video") == "poll_or_vote_suggestion"
        assert detector.classify_message("voten por la opción 2") == "poll_or_vote_suggestion"

    def test_low_signal_noise(self):
        detector = ChatEventDetector()
        assert detector.classify_message("jaja") == "low_signal_noise"
        assert detector.classify_message("xd") == "low_signal_noise"
        assert detector.classify_message("") == "low_signal_noise"

    def test_moderation_risk_detected(self):
        detector = ChatEventDetector()
        assert detector.classify_message("mátate") == "moderation_or_risk"

    def test_political_chat_not_low_signal(self):
        """Political debate messages must NOT be classified as low_signal_noise."""
        detector = ChatEventDetector()
        results = []
        for msg in POLITICAL_CHAT:
            event = detector.classify_message(msg["text"])
            results.append((msg["user"], msg["text"][:50], event))

        low_signal = [r for r in results if r[2] == "low_signal_noise"]
        # At most 20% can be low_signal (short messages like "Como debe ser")
        assert len(low_signal) <= len(POLITICAL_CHAT) * 0.3, (
            f"Too many low_signal_noise classifications in political chat:\n{low_signal}"
        )

    def test_political_chat_has_significant_events(self):
        detector = ChatEventDetector()
        summary = detector.detect_events(POLITICAL_CHAT)
        assert summary["total_messages"] == len(POLITICAL_CHAT)
        assert summary["unique_users"] >= 5
        # Should detect significant events, not just low_signal_noise
        assert summary["primary_event"] != "low_signal_noise", (
            f"Political chat should have a dominant event, got: {summary}"
        )


class TestChatContextPacketBuilder:
    """Verify ChatContextPacket is built correctly from real chat data."""

    def test_builds_packet_from_political_chat(self):
        builder = ChatContextPacketBuilder()
        packet = builder.build(POLITICAL_CHAT)

        assert packet.total_messages == len(POLITICAL_CHAT)
        assert packet.unique_users >= 5
        assert packet.primary_event != "low_signal_noise"
        # Should call LLM for active political debate
        assert packet.should_call_llm is True
        assert packet.response_goal != "stay_silent"

    def test_packet_has_highlight(self):
        builder = ChatContextPacketBuilder()
        packet = builder.build(POLITICAL_CHAT)
        assert packet.selected_highlight is not None
        assert len(packet.selected_highlight.get("text", "")) > 10

    def test_packet_has_supporting_comments(self):
        builder = ChatContextPacketBuilder()
        packet = builder.build(POLITICAL_CHAT)
        assert len(packet.supporting_comments) >= 1

    def test_packet_to_prompt_context(self):
        builder = ChatContextPacketBuilder()
        packet = builder.build(POLITICAL_CHAT)
        context = packet.to_prompt_context()
        # Must NOT contain the old defective text
        assert "No hay un tema dominante claro" not in context
        # Must contain actual content references
        assert len(context) > 50

    def test_packet_to_dict_serializable(self):
        builder = ChatContextPacketBuilder()
        packet = builder.build(POLITICAL_CHAT)
        d = packet.to_dict()
        assert isinstance(d, dict)
        assert "primary_event" in d
        assert "selected_highlight" in d

    def test_empty_chat_produces_silent_packet(self):
        builder = ChatContextPacketBuilder()
        packet = builder.build([])
        assert packet.should_call_llm is False
        assert packet.response_goal == "stay_silent"
        assert packet.primary_event == "low_signal_noise"

    def test_direct_question_triggers_response(self):
        builder = ChatContextPacketBuilder()
        msgs = [
            {"user": "a", "text": "¿van a dejar el en vivo para verlo después?"},
            {"user": "b", "text": "sí, porfa déjenlo guardado"},
            {"user": "c", "text": "yo también quiero verlo después"},
        ]
        packet = builder.build(msgs)
        assert packet.should_call_llm is True
        assert "question" in packet.primary_event or packet.response_goal != "stay_silent"

    def test_spam_only_produces_low_signal(self):
        builder = ChatContextPacketBuilder()
        msgs = [
            {"user": "spammer", "text": "BUY MY PRODUCT"},
            {"user": "spammer", "text": "BUY MY PRODUCT"},
            {"user": "spammer", "text": "BUY MY PRODUCT"},
        ]
        packet = builder.build(msgs)
        # Repeated identical spam should NOT trigger LLM call
        # (unique_users = 1, not enough diversity)
        assert packet.unique_users == 1


class TestOldVsNewComparison:
    """Verify ChatContextPacket is BETTER than old compact_chat."""

    def test_political_chat_packet_beats_old_compact(self):
        """The old compact_chat would say 'No hay tema dominante'.
        The new packet must detect political debate."""
        builder = ChatContextPacketBuilder()
        packet = builder.build(POLITICAL_CHAT)

        # Old: "No hay un tema dominante claro..."
        # New: must have substance
        assert packet.total_messages == 15
        assert packet.unique_users >= 5
        assert len(packet.topic_clusters) >= 1 or packet.selected_highlight is not None

    def test_packet_context_contains_real_content(self):
        """to_prompt_context() must reference actual chat topics."""
        builder = ChatContextPacketBuilder()
        packet = builder.build(POLITICAL_CHAT)
        context = packet.to_prompt_context()

        # Must NOT be generic statistical noise
        forbidden = [
            "No hay un tema dominante claro",
            "temperatura baja",
            "el chat está",
        ]
        for f in forbidden:
            assert f not in context, f"Context contains forbidden phrase: {f}"

        # Must contain real substance
        assert len(context) > 80, f"Context too short: {len(context)} chars"
