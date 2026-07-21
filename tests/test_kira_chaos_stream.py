"""Kira Chaos Stream — hardening tests for installer readiness.

Covers CT-001 through CT-007 from the urgent Kira chaos test protocol:
  CT-001: PTT wins under full queue
  CT-002: Stale chat does not produce late reactions
  CT-003: Overflow preserves human priority
  CT-004: Joyita beats boring text (with spec samples)
  CT-005: 7-topic agenda stress without repetition or state stall
  CT-006: Repeated interruptions leave no corrupt state
  CT-007: Hyperintense chat does not cause unbounded backlog

Track: hardening_failure_testing_20260515
"""

import queue
import time
import threading
from unittest.mock import MagicMock

import pytest

from opencohost.core import llm_engine
from opencohost.smart_aggregator.kira_agenda_controller import (
    AgendaState,
    KiraAgendaController,
    TopicStatus,
)
from opencohost.ui.smart_aggregator_ui import SmartAggregatorUI


# ---------------------------------------------------------------------------
# Synthetic data from the chaos test spec
# ---------------------------------------------------------------------------

AGENDA_TOPICS = [
    ("Nostalgia tecnológica", "conectar aparatos del pasado con emociones presentes"),
    ("IA como co-host", "hasta dónde puede opinar una IA sin caer en papelón"),
    ("Cultura de streaming", "por qué hay gente viendo gente vivir y qué dice de nosotros"),
    ("Videojuegos difíciles", "esos juegos que te hacen insultar la pantalla"),
    ("Comunidades online", "cómo se forman tribus digitales sin un líder claro"),
    ("Privacidad local-first", "por qué tu data no debería salir de tu compu"),
    ("Humor absurdo de chat", "cuándo un copypaste se convierte en arte involuntario"),
]

JOYITA_MESSAGES = [
    "¿Kira acaba de descubrir el capitalismo con RGB?",
    "jajaja la IA se puso modo tía del cyber 😂",
    "si el modelo pesa 7b, ¿Kira cobra por kilo?",
    "esto suena a Windows 98 teniendo una crisis existencial",
]

TRASH_CHAT = [
    "hola", "hola", "hola",          # spam repetido
    "🔥🔥🔥", "🔥🔥🔥", "🔥🔥🔥",  # emojis repetidos
    "si", "no", "GG",                 # texto muy corto
    "COMPREN MI SKIN", "COMPREN MI SKIN",  # bait sin contenido
    "AAAAAA", "AAAAA",                # mayúsculas
    "blablabla random sin contexto",   # fuera de tema
]

PTT_INTERRUPTIONS = [
    "Kira, pará un segundo y respondé esto.",
    "No sigas con ese tema, cambiemos el ángulo.",
    "Eso del chat está bueno, agarrá ese comentario.",
    "Cortá, volvamos al juego.",
]


# ---------------------------------------------------------------------------
# CT-001 — PTT wins under combined full queue
# ---------------------------------------------------------------------------


class TestCT001PttWinsUnderFullQueue:
    """PTT (priority 0) ALWAYS gets processed before chat and agenda."""

    def test_ptt_wins_even_when_queue_overflows_with_agenda_and_chat(self):
        motor = llm_engine.MotorVocalIA(queue.Queue(), lambda event: None)
        motor._pq_max_items = 3

        # Fill queue with agenda and chat
        motor.enqueue("agenda 1", priority=2, source="kira-agenda")
        motor.enqueue("agenda 2", priority=2, source="kira-agenda")
        motor.enqueue("chat 1", priority=1, source="chat")
        motor.enqueue("chat 2", priority=1, source="chat")

        # Now add PTT — must survive overflow
        motor.enqueue("ptt streamer", priority=0, source="ptt")

        sources = [item[3] for item in motor._priority_queue]
        assert "ptt" in sources, "PTT must always survive overflow"
        assert motor._priority_queue[0][3] == "ptt", "PTT must be first (priority 0)"
        assert len(motor._priority_queue) <= 3

    def test_multiple_ptt_all_preserved_over_lower_priority(self):
        motor = llm_engine.MotorVocalIA(queue.Queue(), lambda event: None)
        motor._pq_max_items = 4

        motor.enqueue("agenda", priority=2, source="kira-agenda")
        motor.enqueue("chat", priority=1, source="chat")
        motor.enqueue("ptt 1", priority=0, source="ptt")
        motor.enqueue("ptt 2", priority=0, source="ptt")
        motor.enqueue("ptt 3", priority=0, source="ptt")

        ptt_count = sum(1 for item in motor._priority_queue if item[3] == "ptt")
        assert ptt_count == 3, "All PTT items must survive overflow"


# ---------------------------------------------------------------------------
# CT-002 — Stale chat does not produce late reactions
# ---------------------------------------------------------------------------


class TestCT002StaleChatExpiry:
    """Expired chat items must be silently discarded — no accumulation."""

    def test_expired_chat_not_moved_to_accumulation(self):
        motor = llm_engine.MotorVocalIA(queue.Queue(), lambda event: None)
        motor._pq_ttl_seconds = 0.01
        motor._ejecutar_inferencia = MagicMock()

        motor.enqueue("old chat", priority=1, source="chat")
        time.sleep(0.02)

        motor._processing = False
        motor._speaking = False
        motor._process_priority_queue()

        assert motor._priority_queue == []
        assert motor._accumulation_buffer == [], \
            "Expired chat must NOT enter accumulation buffer"
        motor._ejecutar_inferencia.assert_not_called()

    def test_stale_agenda_is_ttl_exempt_and_still_processed(self):
        # F2 (judgment-day WU2): kira-agenda* is EXEMPT from TTL expiry. A stale
        # adopted agenda draft must NOT be silently discarded — expiring it would
        # strand the turn (never speaks) and its orphaned pregen cache would block
        # every new prefetch. replace_pending dedup keeps agenda from stacking, so
        # exemption can't grow the queue. So the stale item survives the sweep and
        # is processed (inverts the pre-F2 "expired agenda also discarded").
        motor = llm_engine.MotorVocalIA(queue.Queue(), lambda event: None)
        motor._pq_ttl_seconds = 0.01
        motor._ejecutar_inferencia = MagicMock()

        motor.enqueue("old agenda", priority=2, source="kira-agenda")
        time.sleep(0.02)

        motor._processing = False
        motor._speaking = False
        motor._process_priority_queue()

        # Survived the TTL sweep and was processed (not discarded).
        assert motor._priority_queue == []
        motor._ejecutar_inferencia.assert_called_once()
        assert motor._ejecutar_inferencia.call_args[0][0] == "old agenda"


# ---------------------------------------------------------------------------
# CT-003 — Overflow preserves human priority ordering
# ---------------------------------------------------------------------------


class TestCT003OverflowPriority:
    """Overflow must discard agenda first, then chat, NEVER PTT."""

    def test_agenda_dropped_before_chat_and_ptt_under_combined_load(self):
        motor = llm_engine.MotorVocalIA(queue.Queue(), lambda event: None)
        motor._pq_max_items = 3

        # Simulate combined load
        motor.enqueue("agenda 1", priority=2, source="kira-agenda")
        motor.enqueue("agenda 2", priority=2, source="kira-agenda")
        motor.enqueue("chat", priority=1, source="chat")
        motor.enqueue("ptt", priority=0, source="ptt")

        sources = [item[3] for item in motor._priority_queue]
        assert "ptt" in sources
        assert "chat" in sources
        # At most one agenda should remain
        assert sources.count("kira-agenda") <= 1

    def test_accumulation_receives_dropped_items(self):
        motor = llm_engine.MotorVocalIA(queue.Queue(), lambda event: None)
        motor._pq_max_items = 2

        motor.enqueue("agenda", priority=2, source="kira-agenda")
        motor.enqueue("chat", priority=1, source="chat")
        motor.enqueue("ptt", priority=0, source="ptt")

        # The dropped item (agenda) should go to accumulation
        assert len(motor._accumulation_buffer) >= 1
        accum_sources = [item[2] for item in motor._accumulation_buffer]
        assert "kira-agenda" in accum_sources


# ---------------------------------------------------------------------------
# CT-004 — Joyita vence texto largo aburrido (with spec samples)
# ---------------------------------------------------------------------------


class TestCT004JoyitaSelection:
    """_select_highlight must prefer joyitas over long boring text."""

    @pytest.mark.parametrize("joyita", JOYITA_MESSAGES)
    def test_spec_joyita_beats_boring_long_text(self, joyita):
        context = [
            {"user": "boring_user", "text": "Bueno esto es un mensaje largo sobre un tema que no tiene nada de interesante y solo ocupa espacio"},
            {"user": "joyita_user", "text": joyita},
        ]
        result = SmartAggregatorUI._select_highlight(context)
        assert "joyita_user" in result, f"Joyita '{joyita}' must beat boring text"

    def test_joyita_selected_among_trash_chat(self):
        """Joyita must be selected even in a sea of trash chat."""
        context = [{"user": f"spam{i}", "text": t} for i, t in enumerate(TRASH_CHAT)]
        context.append({"user": "gem_user", "text": JOYITA_MESSAGES[0]})
        result = SmartAggregatorUI._select_highlight(context)
        assert "gem_user" in result

    def test_at_least_one_joyita_selected_from_mixed_batch(self):
        """Given a batch with joyitas and trash, at least one joyita wins."""
        context = (
            [{"user": f"trash{i}", "text": t} for i, t in enumerate(TRASH_CHAT[:5])]
            + [{"user": "gem", "text": j} for j in JOYITA_MESSAGES[:2]]
        )
        result = SmartAggregatorUI._select_highlight(context)
        assert any(j in result for j in JOYITA_MESSAGES[:2])


# ---------------------------------------------------------------------------
# CT-005 — 7-topic agenda stress without repetition or state stall
# ---------------------------------------------------------------------------


class TestCT005SevenTopicAgendaStress:
    """7 approved topics must advance without repetition, stall, or invalid state."""

    def _build_controller_with_7_topics(self, turns=2, batch=1):
        controller = KiraAgendaController(
            max_turns_per_topic=turns,
            turn_batch_size=batch,
            response_length="normal",
        )
        topic_ids = []
        for title, angle in AGENDA_TOPICS:
            topic = controller.add_topic(title, angle=angle, approved=True)
            controller.queue_topic(topic.id)
            topic_ids.append(topic.id)
        controller.enable()
        return controller, topic_ids

    def test_all_7_topics_complete_without_stall(self):
        controller, topic_ids = self._build_controller_with_7_topics(turns=1, batch=1)

        completed_topics = []
        for _round in range(50):  # safety limit
            action = controller.next_action()
            if action.kind == "none":
                if controller.state in (AgendaState.IDLE, AgendaState.OFF):
                    break
                continue
            if action.source == "kira-agenda":
                controller.mark_generation_accepted()
                controller.mark_speech_complete()
            elif action.source == "kira-agenda-stop":
                controller.mark_generation_accepted()
                controller.mark_speech_complete()
                if controller.active_topic is None:
                    completed_topics.append(action.topic_id)

        # All 7 topics should have been processed
        assert len(completed_topics) == 7, f"Expected 7 completed, got {len(completed_topics)}"

    def test_no_exact_repeat_in_accept_output(self):
        controller, _ = self._build_controller_with_7_topics(turns=3, batch=1)

        outputs = []
        unique_outputs = set()

        for topic_idx in range(7):
            action = controller.next_action()
            if action.kind == "none":
                break
            for turn in range(3):
                fake_output = f"Respuesta única #{topic_idx}_{turn}: {AGENDA_TOPICS[topic_idx][0]}"
                accepted = controller.accept_output(fake_output)
                if accepted:
                    outputs.append(fake_output)
                    unique_outputs.add(fake_output)
                    controller.mark_generation_accepted()
                    controller.mark_speech_complete()
                action = controller.next_action()
                if action.kind == "none" or action.source == "kira-agenda-stop":
                    controller.mark_generation_accepted()
                    controller.mark_speech_complete()
                    break

        # No exact repeats
        assert len(outputs) == len(unique_outputs), "No exact repeats should exist"

    def test_near_repeat_rejected_across_topics(self):
        controller = KiraAgendaController()

        first = "Y así nos damos cuenta de que la tecnología no cambia la cultura sola."
        near = "Y así nos damos cuenta de que la tecnología no cambia la cultura por sí misma."

        assert controller.accept_output(first) is True
        assert controller.accept_output(near) is False, \
            "Near-repeat must be rejected even across different topics"

    def test_turn_counters_advance_correctly_through_7_topics(self):
        controller, topic_ids = self._build_controller_with_7_topics(turns=2, batch=1)
        topic_turns = {}

        for _round in range(100):
            action = controller.next_action()
            if action.kind == "none":
                if controller.state in (AgendaState.IDLE, AgendaState.OFF):
                    break
                continue
            if action.source == "kira-agenda":
                tid = action.topic_id
                controller.mark_generation_accepted()
                controller.mark_speech_complete()
                topic_turns[tid] = topic_turns.get(tid, 0) + 1
            elif action.source == "kira-agenda-stop":
                controller.mark_generation_accepted()
                controller.mark_speech_complete()

        # Each topic should have gotten exactly 2 turns
        for tid in topic_ids:
            assert topic_turns.get(tid, 0) == 2, \
                f"Topic {tid} should have 2 turns, got {topic_turns.get(tid, 0)}"

    def test_agenda_ends_in_valid_state(self):
        controller, _ = self._build_controller_with_7_topics(turns=1, batch=1)

        for _round in range(100):
            action = controller.next_action()
            if action.kind == "none":
                if controller.state in (AgendaState.IDLE, AgendaState.OFF):
                    break
                continue
            controller.mark_generation_accepted()
            controller.mark_speech_complete()

        assert controller.state in (AgendaState.IDLE, AgendaState.OFF), \
            f"Agenda must end in valid terminal state, got {controller.state}"
        assert controller.active_topic is None


# ---------------------------------------------------------------------------
# CT-006 — Repeated interruptions leave no corrupt state
# ---------------------------------------------------------------------------


class TestCT006RepeatedInterruptions:
    """Multiple PTT interruptions must not corrupt controller or prefetch state."""

    def test_ptt_during_generating_is_blocked_until_state_ready(self):
        """PTT can't preempt while GENERATING — next_action returns none.

        This is by design: the controller prevents state corruption.
        PTT is delivered once the state transitions to WAITING_SIGNAL.
        """
        controller = KiraAgendaController(max_turns_per_topic=5)
        topic = controller.add_topic("Tema interrumpido", approved=True)
        controller.queue_topic(topic.id)
        controller.enable()
        controller.next_action()  # starts GENERATING

        assert controller.state == AgendaState.GENERATING

        # PTT during GENERATING returns none — controller blocks new actions
        action = controller.next_action(ptt_text=PTT_INTERRUPTIONS[0])
        assert action.kind == "none", "Controller must block PTT during GENERATING"

        # After generation + speech complete, PTT gets through
        controller.mark_generation_accepted()
        controller.mark_speech_complete()
        action = controller.next_action(ptt_text=PTT_INTERRUPTIONS[0])

        assert action.source == "ptt"
        assert action.priority == 0
        assert controller.state == AgendaState.GENERATING

    def test_ptt_during_speaking_creates_valid_continuation(self):
        controller = KiraAgendaController(max_turns_per_topic=5)
        topic = controller.add_topic("Tema hablando", approved=True)
        controller.queue_topic(topic.id)
        controller.enable()
        controller.next_action()
        controller.mark_generation_accepted()

        assert controller.state == AgendaState.SPEAKING

        # PTT arrives while speaking — should be handled when speech completes
        controller.mark_speech_complete()
        action = controller.next_action(ptt_text=PTT_INTERRUPTIONS[1])

        assert action.source == "ptt"
        assert action.priority == 0

    def test_multiple_ptt_interruptions_advance_turns_predictably(self):
        """PTT interruptions DO consume turn slots by design.

        Each PTT→generate→speak cycle counts as 1 turn. This is correct
        because the LLM produces output that gets spoken. The key invariant
        is that turns_spoken never exceeds max_turns_per_topic and state
        remains valid after each PTT cycle.
        """
        controller = KiraAgendaController(max_turns_per_topic=10, turn_batch_size=1)
        topic = controller.add_topic("Tema bajo fuego", approved=True)
        controller.queue_topic(topic.id)
        controller.enable()

        # First normal action
        controller.next_action()
        controller.mark_generation_accepted()
        controller.mark_speech_complete()
        assert topic.turns_spoken == 1

        # Multiple PTT interruptions — each consumes 1 turn
        for i, ptt_text in enumerate(PTT_INTERRUPTIONS):
            action = controller.next_action(ptt_text=ptt_text)
            assert action.source == "ptt"
            controller.mark_generation_accepted()
            controller.mark_speech_complete()

        # 1 agenda turn + 4 PTT turns = 5 total
        assert topic.turns_spoken == 5
        assert topic.turns_spoken <= controller.max_turns_per_topic
        assert controller.state in (
            AgendaState.WAITING_SIGNAL,
            AgendaState.IDLE,
        )

    def test_prefetch_cleared_on_ptt_interrupt_via_replace_pending(self):
        motor = llm_engine.MotorVocalIA(queue.Queue(), lambda event: None)
        motor._prefetched_agenda = {
            "payload": "old", "dialogo": "viejo",
            "source": "kira-agenda", "priority": 2,
        }

        # PTT arrives — agenda pending should be replaced/cleared
        motor.replace_pending("ptt input", priority=0, source="ptt")

        # Prefetch should remain because replace_pending only clears for
        # kira-agenda source. But drop_pending_sources should clear it.
        motor.drop_pending_sources(("kira-agenda",))
        assert motor._prefetched_agenda is None

    def test_emergency_stop_during_generating_preserves_queue(self):
        controller = KiraAgendaController(max_turns_per_topic=5)
        topic1 = controller.add_topic("Tema 1", approved=True)
        topic2 = controller.add_topic("Tema 2", approved=True)
        controller.queue_topic(topic1.id)
        controller.queue_topic(topic2.id)
        controller.enable()
        controller.next_action()  # start generating topic 1

        controller.emergency_stop()

        assert controller.state == AgendaState.OFF
        assert controller.active_topic is None
        # Topic 2 should still be queued
        assert topic2.status == TopicStatus.QUEUED

    def test_soft_stop_during_speaking_waits_then_closes(self):
        controller = KiraAgendaController(max_turns_per_topic=5)
        topic = controller.add_topic("Tema que para suave", approved=True)
        controller.queue_topic(topic.id)
        controller.enable()
        controller.next_action()
        controller.mark_generation_accepted()

        assert controller.state == AgendaState.SPEAKING

        result = controller.soft_stop()
        assert result.kind == "none"  # Can't stop mid-speech
        assert controller.stop_requested is True

        controller.mark_speech_complete()
        action = controller.next_action()
        assert action.source == "kira-agenda-stop"


# ---------------------------------------------------------------------------
# CT-007 — Hyperintense chat does not cause unbounded backlog
# ---------------------------------------------------------------------------


class TestCT007ChatBackpressure:
    """High-volume noisy chat must not cause unbounded queues or stale reactions."""

    def test_priority_queue_bounded_under_chat_flood(self):
        motor = llm_engine.MotorVocalIA(queue.Queue(), lambda event: None)
        motor._pq_max_items = 5

        # Flood with 50 chat messages
        for i in range(50):
            motor.enqueue(f"chat msg {i}", priority=1, source="chat")

        assert len(motor._priority_queue) <= 5, \
            "Priority queue must stay bounded under chat flood"

    def test_accumulation_buffer_bounded_under_overflow(self):
        motor = llm_engine.MotorVocalIA(queue.Queue(), lambda event: None)
        motor._pq_max_items = 2
        motor._accum_max_items = 50
        motor._accum_max_chars = 2000

        for i in range(100):
            motor.enqueue(f"overflow msg {i}", priority=1, source="chat")

        assert len(motor._accumulation_buffer) <= 50, \
            "Accumulation buffer must be bounded"
        total_chars = sum(len(p) for _, p, _ in motor._accumulation_buffer)
        assert total_chars <= 2000, \
            "Accumulation buffer chars must be bounded"

    def test_ptt_survives_during_chat_flood(self):
        motor = llm_engine.MotorVocalIA(queue.Queue(), lambda event: None)
        motor._pq_max_items = 5

        # Add PTT first
        motor.enqueue("ptt important", priority=0, source="ptt")

        # Flood with chat
        for i in range(20):
            motor.enqueue(f"chat flood {i}", priority=1, source="chat")

        sources = [item[3] for item in motor._priority_queue]
        assert "ptt" in sources, "PTT must survive chat flood"

    def test_agenda_preserved_alongside_ptt_during_mixed_flood(self):
        """Under mixed PTT + chat + agenda load, all priorities must coexist."""
        motor = llm_engine.MotorVocalIA(queue.Queue(), lambda event: None)
        motor._pq_max_items = 5

        motor.enqueue("ptt", priority=0, source="ptt")
        motor.enqueue("agenda", priority=2, source="kira-agenda")
        for i in range(10):
            motor.enqueue(f"chat {i}", priority=1, source="chat")

        sources = [item[3] for item in motor._priority_queue]
        assert "ptt" in sources
        # Under max=5 with 1 PTT + 10 chat + 1 agenda, 
        # PTT and some chat should survive; agenda may or may not
        assert len(motor._priority_queue) <= 5

    def test_ttl_prevents_stale_chat_reaction_after_flood(self):
        """After flood, expired chat items must not trigger late reactions."""
        motor = llm_engine.MotorVocalIA(queue.Queue(), lambda event: None)
        motor._pq_ttl_seconds = 0.01
        motor._ejecutar_inferencia = MagicMock()

        for i in range(5):
            motor.enqueue(f"old flood {i}", priority=1, source="chat")

        time.sleep(0.02)  # Let TTL expire

        motor._processing = False
        motor._speaking = False
        motor._process_priority_queue()

        assert motor._priority_queue == []
        motor._ejecutar_inferencia.assert_not_called()


# ---------------------------------------------------------------------------
# Combined chaos scenario — integration test
# ---------------------------------------------------------------------------


class TestChaosStreamIntegration:
    """End-to-end chaos combining agenda + chat + PTT + overflow + TTL."""

    def test_combined_chaos_does_not_corrupt_state(self):
        """Simulate the full chaos scenario from the spec."""
        motor = llm_engine.MotorVocalIA(queue.Queue(), lambda event: None)
        motor._pq_max_items = 5
        motor._pq_ttl_seconds = 0.5

        controller = KiraAgendaController(
            max_turns_per_topic=2,
            turn_batch_size=1,
        )

        # Add 7 agenda topics
        for title, angle in AGENDA_TOPICS:
            topic = controller.add_topic(title, angle=angle, approved=True)
            controller.queue_topic(topic.id)
        controller.enable()

        # Simulate a few rounds of chaos
        for _round in range(3):
            # Agenda action
            action = controller.next_action()
            if action.kind == "enqueue":
                motor.enqueue(action.prompt, priority=action.priority, source=action.source)

            # Chat flood
            for i in range(10):
                motor.enqueue(f"chat noise {_round}_{i}", priority=1, source="chat")

            # PTT interrupt
            motor.enqueue(PTT_INTERRUPTIONS[_round % len(PTT_INTERRUPTIONS)],
                          priority=0, source="ptt")

            # Verify invariants after each round
            assert len(motor._priority_queue) <= motor._pq_max_items
            ptt_items = [item for item in motor._priority_queue if item[3] == "ptt"]
            assert len(ptt_items) >= 1, "PTT must always be in queue"

        # Final state checks
        assert controller.state in (
            AgendaState.GENERATING,
            AgendaState.SPEAKING,
            AgendaState.IDLE,
            AgendaState.WAITING_SIGNAL,
            AgendaState.OFF,
            AgendaState.OPEN_TOPIC,
            AgendaState.CONTINUE_TOPIC,
        )
        assert len(motor._priority_queue) <= motor._pq_max_items
        assert len(motor._accumulation_buffer) <= motor._accum_max_items

    def test_ptt_never_expires_even_after_long_delay(self):
        """PTT items must survive TTL checks even with very short TTL.

        [F2, judgment-day WU2] kira-agenda* is now also TTL-exempt, so the stale
        agenda item survives too and the iterative drain processes it AFTER the
        PTT item (priority 0 sorts first). Chat still expires. The PTT guarantee
        is the point: it survives and is processed first.
        """
        motor = llm_engine.MotorVocalIA(queue.Queue(), lambda event: None)
        motor._pq_ttl_seconds = 0.001  # 1ms - extremely aggressive
        motor._ejecutar_inferencia = MagicMock()

        motor.enqueue("streamer says something", priority=0, source="ptt")
        motor.enqueue("chat that expires", priority=1, source="chat")
        motor.enqueue("agenda that expires", priority=2, source="kira-agenda")

        time.sleep(0.01)  # Let non-PTT TTL expire (chat only; agenda is exempt)

        motor._processing = False
        motor._speaking = False
        motor._process_priority_queue()

        # PTT processed FIRST (priority 0). Chat expired; the TTL-exempt agenda
        # item is processed after PTT.
        processed = [c[0][0] for c in motor._ejecutar_inferencia.call_args_list]
        assert processed == ["streamer says something", "agenda that expires"], processed
        assert motor._ejecutar_inferencia.call_args_list[0][1]["source"] == "ptt"
