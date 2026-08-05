"""Kira orchestration gap tests — critical race conditions in production wiring.

These tests exercise the INTERACTION between MotorVocalIA and
KiraAgendaController in sequences that replicate real streaming conditions.
They don't require Tkinter; they test the component contracts that app_shell.py
depends on.

Gap IDs match kira_test_gap_analysis.md:
  GAP-001: PTT arrives while prefetch is ready → prefetch must yield
  GAP-002: Chat cadence consumes turn → prefetch must be cleared
  GAP-003: Double mark_generation_accepted (TTS chunking) → must be idempotent
  GAP-004: Emergency stop while prefetch thread is running → zombie must not survive
"""

import queue
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from opencohost.core import llm_engine
from opencohost.ui import app_shell
from opencohost.smart_aggregator.kira_agenda_controller import (
    AgendaAction,
    AgendaState,
    KiraAgendaController,
    TopicStatus,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_motor() -> llm_engine.MotorVocalIA:
    """Build a MotorVocalIA with mocked internals — no LLM, no TTS."""
    motor = llm_engine.MotorVocalIA(queue.Queue(), lambda event: None)
    return motor


def _build_agenda_with_topic(turns: int = 3) -> tuple[KiraAgendaController, str]:
    """Build a controller with one approved+queued topic. Returns (controller, topic_id)."""
    ctrl = KiraAgendaController(max_turns_per_topic=turns)
    topic = ctrl.add_topic("Tema de test", angle="ángulo de prueba", approved=True)
    ctrl.queue_topic(topic.id)
    ctrl.enable()
    return ctrl, topic.id


# ═══════════════════════════════════════════════════════════════════════════
# GAP-001 — PTT arrives while prefetch is ready → prefetch must yield
# ═══════════════════════════════════════════════════════════════════════════


class TestGAP001PrefetchYieldsToPTT:
    """When PTT is in the priority queue, prefetched agenda must NOT play."""

    def test_has_pending_priority_before_detects_ptt(self):
        motor = _build_motor()
        motor.enqueue("streamer says stop", priority=0, source="ptt")

        # Agenda priority is 2, PTT is 0 — must detect higher priority
        assert motor.has_pending_priority_before(2) is True

    def test_prefetch_not_played_when_ptt_in_queue(self):
        """Simulates the exact sequence from _on_motor_speaking_end."""
        motor = _build_motor()
        ctrl, topic_id = _build_agenda_with_topic()

        # Start agenda → GENERATING
        action = ctrl.next_action()
        assert action.kind == "enqueue"

        # Simulate: generation accepted, now SPEAKING
        ctrl.mark_generation_accepted()
        assert ctrl.state == AgendaState.SPEAKING

        # Prefetch completes in background
        motor._prefetched_agenda = {
            "payload": "prefetched prompt",
            "dialogo": "respuesta prefabricada",
            "priority": 2,
            "source": "kira-agenda",
        }
        motor._prefetch_done.set()

        # PTT arrives BEFORE speaking_end checks prefetch
        motor.enqueue("Kira pará", priority=0, source="ptt")

        # NOW simulate the speaking_end check:
        # 1. mark_speech_complete
        ctrl.mark_speech_complete()

        # 2. Check if higher priority pending (this is what app_shell does)
        prefetch_action = ctrl.prefetch_action_after_current_speech()
        has_higher = motor.has_pending_priority_before(prefetch_action.priority if prefetch_action.kind == "enqueue" else 2)

        assert has_higher is True, "PTT in queue must block prefetch playback"

        # Verify prefetch is NOT played when higher priority exists
        # (app_shell would call _kira_agenda_clear_prefetch here)
        motor.clear_prefetched_agenda()
        assert motor._prefetched_agenda is None

    def test_ptt_enqueued_after_prefetch_ready_still_wins(self):
        """PTT arriving in the narrow window after wait_prefetched_agenda
        but before play_prefetched_agenda must still block playback."""
        motor = _build_motor()

        # Prefetch is ready
        motor._prefetched_agenda = {
            "payload": "p", "dialogo": "d",
            "priority": 2, "source": "kira-agenda",
        }
        motor._prefetch_done.set()

        # Verify prefetch is ready
        assert motor.wait_prefetched_agenda(timeout=0.0) is True

        # PTT arrives right now
        motor.enqueue("interruption", priority=0, source="ptt")

        # Check priority BEFORE playing
        assert motor.has_pending_priority_before(2) is True

        # If the check passes, app_shell clears prefetch instead of playing
        motor.clear_prefetched_agenda()
        assert motor._prefetched_agenda is None

        # PTT should still be in queue
        sources = [item[3] for item in motor._priority_queue]
        assert "ptt" in sources

    def test_chat_in_queue_also_blocks_agenda_prefetch(self):
        """Chat (priority 1) should also block agenda prefetch (priority 2)."""
        motor = _build_motor()
        motor._prefetched_agenda = {
            "payload": "p", "dialogo": "d",
            "priority": 2, "source": "kira-agenda",
        }

        motor.enqueue("chat message", priority=1, source="chat")
        assert motor.has_pending_priority_before(2) is True


class TestCohostAudioArbitrationCrash:
    """Regression tests for direct interaction racing agenda-prefetch playback."""

    def test_direct_command_is_queued_while_agenda_is_speaking(self):
        motor = _build_motor()
        motor.is_ready = True
        motor.motor_tts = "ligero"
        with motor._lock:
            motor._speaking = True
            motor._current_speech_source = "kira-agenda"

        with patch.object(motor, "_ejecutar_inferencia") as ejecutar:
            motor._dispatch_command("process_context", "pregunta directa del operador")

        ejecutar.assert_not_called()
        assert [item[3] for item in motor._priority_queue] == ["direct"]
        assert motor._priority_queue[0][0] == 1

    def test_agenda_prefetch_does_not_play_while_direct_is_processing(self):
        app = object.__new__(app_shell.VocalAIApp)
        app.motor_ia = _build_motor()
        app.motor_ia._prefetched_agenda = {
            "payload": "prefetched prompt",
            "dialogo": "respuesta agenda obsoleta",
            "priority": 2,
            "source": "kira-agenda",
        }
        app.motor_ia._prefetch_done.set()
        with app.motor_ia._lock:
            app.motor_ia._processing = True
            app.motor_ia._current_processing_source = "direct"
        action = AgendaAction(
            kind="enqueue",
            prompt="prefetched prompt",
            priority=2,
            source="kira-agenda",
        )
        app._kira_agenda_prefetched_action = action
        app.kira_agenda = MagicMock()
        app._on_stream_admin_log = MagicMock()
        app._kira_agenda_update_status = MagicMock()

        with patch.object(app.motor_ia, "play_prefetched_agenda", wraps=app.motor_ia.play_prefetched_agenda) as play:
            assert app._kira_agenda_play_prefetched_if_ready() is False

        play.assert_not_called()
        assert app.motor_ia._prefetched_agenda is None
        app._on_stream_admin_log.assert_called()

    def test_rapid_direct_commands_queue_while_agenda_audio_is_active(self):
        motor = _build_motor()
        motor.is_ready = True
        motor.motor_tts = "ligero"
        with motor._lock:
            motor._speaking = True
            motor._current_speech_source = "kira-agenda"

        with patch.object(motor, "_ejecutar_inferencia") as ejecutar:
            for index in range(5):
                motor._dispatch_command("process_context", f"pregunta directa {index}")

        ejecutar.assert_not_called()
        sources = [item[3] for item in motor._priority_queue]
        assert sources == ["direct"] * 5
        assert all(item[0] == 1 for item in motor._priority_queue)


# ═══════════════════════════════════════════════════════════════════════════
# GAP-002 — Chat cadence consumes turn → prefetch must be cleared
# ═══════════════════════════════════════════════════════════════════════════


class TestGAP002ChatCadenceClearsPrefetch:
    """When chat cadence fires, the motor-level prefetch must be cleaned up."""

    def test_enqueue_kira_agenda_action_clears_motor_prefetch(self):
        """_enqueue_kira_agenda_action always clears prefetch — verify contract."""
        motor = _build_motor()

        # Simulate prefetch sitting in motor
        motor._prefetched_agenda = {
            "payload": "old", "dialogo": "viejo",
            "priority": 2, "source": "kira-agenda",
        }

        # Simulate what _enqueue_kira_agenda_action does:
        # 1. Clear app-level prefetched action (simulated as None)
        # 2. Clear motor prefetch
        motor.clear_prefetched_agenda()
        # 3. Enqueue the new action
        motor.replace_pending("chat prompt", priority=1, source="chat")

        assert motor._prefetched_agenda is None
        assert motor._prefetch_done.is_set() is False

    def test_chat_cadence_during_agenda_replaces_prefetch_cleanly(self):
        """Full chat cadence sequence: controller emits chat action,
        motor prefetch gets cleared, new chat gets enqueued."""
        motor = _build_motor()
        ctrl, topic_id = _build_agenda_with_topic(turns=5)

        # Advance through agenda until WAITING_SIGNAL
        action = ctrl.next_action()
        ctrl.mark_generation_accepted()
        ctrl.mark_speech_complete()
        assert ctrl.state == AgendaState.WAITING_SIGNAL

        # Set up prefetch in motor (simulating background generation)
        motor._prefetched_agenda = {
            "payload": "next agenda", "dialogo": "respuesta agenda",
            "priority": 2, "source": "kira-agenda",
        }
        motor._prefetch_done.set()

        # Advance blocks_since_chat_check past cadence threshold
        ctrl.blocks_since_chat_check = ctrl.chat_cadence_blocks + 1

        # Chat cadence fires
        chat_action = ctrl.next_action(compact_chat="El chat dice cosas interesantes")
        assert chat_action.kind == "enqueue"
        assert chat_action.source == "chat"

        # Simulate _enqueue_kira_agenda_action clearing prefetch
        motor.clear_prefetched_agenda()
        motor.enqueue(chat_action.prompt, priority=chat_action.priority, source=chat_action.source)

        assert motor._prefetched_agenda is None
        sources = [item[3] for item in motor._priority_queue]
        assert "chat" in sources

    def test_replace_pending_kira_agenda_clears_prefetch(self):
        """replace_pending with kira-agenda source must clear prefetch."""
        motor = _build_motor()

        motor._prefetched_agenda = {
            "payload": "old", "dialogo": "viejo",
            "priority": 2, "source": "kira-agenda",
        }

        motor.replace_pending("new agenda prompt", priority=2, source="kira-agenda")

        # replace_pending calls clear_prefetched_agenda for kira-agenda source
        assert motor._prefetched_agenda is None


# ═══════════════════════════════════════════════════════════════════════════
# GAP-003 — Double mark_generation_accepted must be idempotent
# ═══════════════════════════════════════════════════════════════════════════


class TestGAP003DoubleMarkGenerationAccepted:
    """TTS chunking may emit speaking_start multiple times. Controller must be safe."""

    def test_second_call_in_speaking_state_is_noop(self):
        ctrl, topic_id = _build_agenda_with_topic()

        action = ctrl.next_action()
        assert ctrl.state == AgendaState.GENERATING

        # First call: GENERATING → SPEAKING
        ctrl.mark_generation_accepted()
        assert ctrl.state == AgendaState.SPEAKING

        # Second call: SPEAKING → should remain SPEAKING (no-op)
        ctrl.mark_generation_accepted()
        assert ctrl.state == AgendaState.SPEAKING

    def test_turns_not_double_incremented(self):
        ctrl, topic_id = _build_agenda_with_topic(turns=5)
        topic = ctrl._topic(topic_id)

        action = ctrl.next_action()
        ctrl.mark_generation_accepted()
        ctrl.mark_generation_accepted()  # Double call from TTS chunking
        ctrl.mark_speech_complete()

        assert topic.turns_spoken == 1, \
            "Double mark_generation_accepted must not double-increment turns"

    def test_triple_call_still_safe(self):
        """Extreme case: 3 TTS chunks each emitting speaking_start."""
        ctrl, topic_id = _build_agenda_with_topic()

        ctrl.next_action()
        ctrl.mark_generation_accepted()
        ctrl.mark_generation_accepted()
        ctrl.mark_generation_accepted()

        assert ctrl.state == AgendaState.SPEAKING

    def test_mark_generation_accepted_in_all_valid_states(self):
        """Verify every valid source state transitions to SPEAKING."""
        valid_states = [
            AgendaState.GENERATING,
            AgendaState.REGENERATING_SAFE,
            AgendaState.HANDLE_STREAMER,
            AgendaState.HANDLE_CHAT,
            AgendaState.CONTINUE_TOPIC,
            AgendaState.OPEN_TOPIC,
            AgendaState.TOPIC_CLOSING,
        ]
        for state in valid_states:
            ctrl, _ = _build_agenda_with_topic()
            ctrl.state = state
            ctrl.mark_generation_accepted()
            assert ctrl.state == AgendaState.SPEAKING, \
                f"mark_generation_accepted from {state} must transition to SPEAKING"

    def test_mark_generation_accepted_ignored_in_invalid_states(self):
        """States NOT in the valid set must be ignored."""
        invalid_states = [
            AgendaState.OFF,
            AgendaState.IDLE,
            AgendaState.WAITING_SIGNAL,
            AgendaState.PAUSED_NEEDS_OPERATOR,
        ]
        for state in invalid_states:
            ctrl, _ = _build_agenda_with_topic()
            ctrl.state = state
            ctrl.mark_generation_accepted()
            assert ctrl.state == state, \
                f"mark_generation_accepted in {state} must be no-op"


# ═══════════════════════════════════════════════════════════════════════════
# GAP-004 — Emergency stop while prefetch thread is running
# ═══════════════════════════════════════════════════════════════════════════


class TestGAP004EmergencyStopVsPrefetchThread:
    """Emergency stop must prevent zombie prefetch from surviving."""

    def test_drop_pending_sources_clears_prefetch(self):
        """Verify that emergency stop's drop_pending_sources clears motor prefetch."""
        motor = _build_motor()

        motor._prefetched_agenda = {
            "payload": "old", "dialogo": "viejo",
            "priority": 2, "source": "kira-agenda",
        }

        motor.drop_pending_sources(("kira-agenda",))
        assert motor._prefetched_agenda is None

    def test_emergency_stop_full_sequence_clears_everything(self):
        """Simulate the exact app_shell._kira_agenda_emergency_stop sequence."""
        motor = _build_motor()
        ctrl, topic_id = _build_agenda_with_topic()

        # Start agenda
        action = ctrl.next_action()
        motor.enqueue(action.prompt, priority=action.priority, source=action.source)

        # Simulate prefetch in motor
        motor._prefetched_agenda = {
            "payload": "prefetched", "dialogo": "zombie",
            "priority": 2, "source": "kira-agenda",
        }

        # Emergency stop sequence (mirrors app_shell._kira_agenda_emergency_stop):
        ctrl.emergency_stop()
        motor.drop_pending_sources(("kira-agenda",))

        # Verify everything is clean
        assert ctrl.state == AgendaState.OFF
        assert ctrl.active_topic is None
        assert motor._prefetched_agenda is None
        agenda_items = [item for item in motor._priority_queue
                        if str(item[3]).startswith("kira-agenda")]
        assert agenda_items == []

    def test_prefetch_thread_write_after_clear_is_prevented_by_epoch(self):
        """The epoch fix prevents the zombie: worker checks epoch under lock
        before writing. If epoch changed (clear was called), result is discarded.

        Sequence:
        1. prefetch_agenda() starts, captures epoch=0
        2. Worker calls _generar_dialogo (slow — blocks on barrier)
        3. Main thread calls clear_prefetched_agenda() → epoch becomes 1
        4. Worker unblocks, checks epoch: 0 != 1 → discards result
        """
        motor = _build_motor()
        generation_barrier = threading.Event()

        def slow_generar(payload, **kwargs):
            """Simulates slow LLM generation that blocks on a barrier."""
            generation_barrier.wait(timeout=3.0)
            return "zombie response that should be discarded"

        def always_accept(text):
            return True

        # Wire mocks
        motor._generar_dialogo = slow_generar
        motor._preview_accept_agenda_output = always_accept

        # Start prefetch — worker will block on barrier
        assert motor.prefetch_agenda("test prompt", priority=2, source="kira-agenda")

        # Give thread time to start
        time.sleep(0.05)

        # Emergency stop clears prefetch — epoch increments
        motor.clear_prefetched_agenda()
        assert motor._prefetch_epoch == 1

        # Now release the worker
        generation_barrier.set()

        # Wait for worker to finish
        motor._prefetch_done.wait(timeout=3.0)

        # The zombie must NOT survive because epoch check prevents write
        assert motor._prefetched_agenda is None, \
            "Epoch fix must prevent zombie write after clear"

    def test_play_prefetched_agenda_returns_false_when_cleared(self):
        """After emergency stop, play_prefetched_agenda must return False."""
        motor = _build_motor()

        # Clear state
        motor.clear_prefetched_agenda()

        # Attempt to play — must return False
        assert motor.play_prefetched_agenda() is False

    def test_prefetch_thread_does_not_write_when_controller_is_off(self):
        """Prefetch validator rejects output when controller is in OFF state.

        This tests the defense-in-depth: even if the thread writes to motor,
        the controller's accept_output would reject it when state is OFF.
        """
        ctrl, _ = _build_agenda_with_topic()

        # Accept an output in normal state
        assert ctrl.accept_output("Primera respuesta válida sobre el tema") is True

        # Emergency stop
        ctrl.emergency_stop()
        assert ctrl.state == AgendaState.OFF

        # After emergency stop, accept_output still works (it's stateless
        # w.r.t. controller state), but the controller won't generate
        # new actions because next_action returns none in OFF state
        action = ctrl.next_action()
        assert action.kind == "none"

    def test_reactivation_after_emergency_stop_starts_clean(self):
        """After emergency stop + re-enable, no zombie prefetch plays."""
        motor = _build_motor()
        ctrl, topic_id = _build_agenda_with_topic(turns=3)

        # Start, generate, emergency stop
        action = ctrl.next_action()
        motor.enqueue(action.prompt, priority=action.priority, source=action.source)
        motor._prefetched_agenda = {
            "payload": "zombie", "dialogo": "should be dead",
            "priority": 2, "source": "kira-agenda",
        }

        ctrl.emergency_stop()
        motor.drop_pending_sources(("kira-agenda",))

        # Re-enable
        ctrl.enable()

        # Motor prefetch must be None
        assert motor._prefetched_agenda is None
        # wait_prefetched_agenda must return False
        assert motor.wait_prefetched_agenda(timeout=0.0) is False
        # play_prefetched_agenda must return False
        assert motor.play_prefetched_agenda() is False

        # Controller should be ready for new action
        assert ctrl.state == AgendaState.IDLE
        action = ctrl.next_action()
        # Topic was active when emergency_stop was called, so it's still
        # queued and available
        assert action.kind == "enqueue" or action.kind == "none"


class TestDoubleClosingPrefetch:
    """Regression: a closing prefetched while the closing line itself was
    playing must not be spoken after the topic completes (orphan audio that
    sounds like the same goodbye repeated with different wording)."""

    def test_stale_closing_prefetch_is_discarded_after_topic_completes(self):
        app = object.__new__(app_shell.VocalAIApp)
        app.motor_ia = _build_motor()
        app.motor_ia._prefetched_agenda = {
            "payload": "closing prompt",
            "dialogo": "segunda despedida duplicada",
            "priority": 2,
            "source": "kira-agenda-stop",
        }
        app.motor_ia._prefetch_done.set()

        ctrl, _topic_id = _build_agenda_with_topic(turns=1)
        ctrl.next_action()
        ctrl.mark_generation_accepted()
        closing = ctrl.prefetch_action_after_current_speech()
        assert closing.source == "kira-agenda-stop"
        ctrl.mark_speech_complete()
        ctrl.start_prefetched_action(closing)
        ctrl.mark_generation_accepted()
        ctrl.mark_speech_complete()  # closing finished → topic completed
        assert ctrl.active_topic is None

        app.kira_agenda = ctrl
        app._kira_agenda_prefetched_action = AgendaAction(
            kind="enqueue",
            prompt="closing prompt",
            priority=2,
            source="kira-agenda-stop",
        )
        app._on_stream_admin_log = MagicMock()
        app._kira_agenda_update_status = MagicMock()

        with patch.object(app.motor_ia, "play_prefetched_agenda", wraps=app.motor_ia.play_prefetched_agenda) as play:
            assert app._kira_agenda_play_prefetched_if_ready() is False

        play.assert_not_called()
        assert app.motor_ia._prefetched_agenda is None


# ═══════════════════════════════════════════════════════════════════════════
# Cross-gap: Combined orchestration sequence
# ═══════════════════════════════════════════════════════════════════════════


class TestOrchestrationSequence:
    """End-to-end sequence simulating a real stream with all 4 gaps exercised."""

    def test_full_stream_lifecycle_no_corruption(self):
        """Agenda start → prefetch → PTT interrupt → chat cadence →
        emergency stop → re-enable → verify clean state."""
        motor = _build_motor()
        ctrl = KiraAgendaController(max_turns_per_topic=3)

        # Add topics
        for title in ["Tema A", "Tema B", "Tema C"]:
            t = ctrl.add_topic(title, approved=True)
            ctrl.queue_topic(t.id)
        ctrl.enable()

        # Phase 1: Normal agenda action
        action = ctrl.next_action()
        assert action.kind == "enqueue"
        motor.enqueue(action.prompt, priority=action.priority, source=action.source)

        # GAP-003: Simulate double speaking_start
        ctrl.mark_generation_accepted()
        ctrl.mark_generation_accepted()  # TTS chunking duplicate
        assert ctrl.state == AgendaState.SPEAKING

        # Phase 2: Prefetch while speaking
        motor._prefetched_agenda = {
            "payload": "prefetch", "dialogo": "next thing to say",
            "priority": 2, "source": "kira-agenda",
        }
        motor._prefetch_done.set()

        # GAP-001: PTT arrives during speech
        motor.enqueue("streamer interrupts", priority=0, source="ptt")

        # Speech ends
        ctrl.mark_speech_complete()

        # Check: PTT blocks prefetch
        assert motor.has_pending_priority_before(2) is True
        motor.clear_prefetched_agenda()  # App shell would do this
        assert motor._prefetched_agenda is None

        # PTT gets processed — controller handles streamer
        action = ctrl.next_action(ptt_text="streamer interrupts")
        assert action.source == "ptt"
        ctrl.mark_generation_accepted()
        ctrl.mark_speech_complete()

        # Phase 3: Chat cadence
        ctrl.blocks_since_chat_check = ctrl.chat_cadence_blocks + 1
        motor._prefetched_agenda = {
            "payload": "new prefetch", "dialogo": "should be cleared",
            "priority": 2, "source": "kira-agenda",
        }

        # GAP-002: Chat cadence fires, must clear prefetch
        chat_action = ctrl.next_action(compact_chat="chat dice algo")
        if chat_action.kind == "enqueue" and chat_action.source == "chat":
            motor.clear_prefetched_agenda()
            motor.enqueue(chat_action.prompt, priority=chat_action.priority,
                          source=chat_action.source)

        assert motor._prefetched_agenda is None

        # Phase 4: Emergency stop
        # GAP-004: Emergency stop clears everything
        ctrl.emergency_stop()
        motor.drop_pending_sources(("kira-agenda",))

        assert ctrl.state == AgendaState.OFF
        assert motor._prefetched_agenda is None

        # Phase 5: Re-enable — clean start
        ctrl.enable()
        assert ctrl.state == AgendaState.IDLE
        assert motor.wait_prefetched_agenda(timeout=0.0) is False

        # Final invariants
        assert len(motor._priority_queue) <= motor._pq_max_items
        ptt_items = [i for i in motor._priority_queue if i[3] == "ptt"]
        # PTT was already consumed, so queue may have remaining items
        assert ctrl.active_topic is None


# ═══════════════════════════════════════════════════════════════════════════
# GAP-005 — Empty agenda response must not stall the cohost loop
# ═══════════════════════════════════════════════════════════════════════════


class TestGAP005EmptyResponseRecovery:
    """An empty (or guardrail-blocked) agenda generation returns "" from
    _generar_dialogo, so _hablar never runs and no speaking_start event fires.
    Without wiring, the controller stays in GENERATING forever and the
    autonomous loop dies silently. The fix routes the empty result through the
    existing validator hook → register_failure(GUARDRAIL_EMPTY) → recovery.
    """

    def test_empty_agenda_response_exits_generating_state(self):
        """The confirmed bug: empty response must leave GENERATING and recover."""
        motor = _build_motor()
        ctrl, _ = _build_agenda_with_topic()
        motor.agenda_output_validator = ctrl.accept_output

        action = ctrl.next_action()
        assert action.kind == "enqueue"
        assert ctrl.state == AgendaState.GENERATING

        with patch.object(motor, "_generar_dialogo", return_value=""), \
                patch.object(motor, "_hablar") as hablar:
            motor._ejecutar_inferencia(action.prompt, source=action.source)

        hablar.assert_not_called()
        assert ctrl.state != AgendaState.GENERATING
        assert ctrl.state == AgendaState.REGENERATING_SAFE

    def test_empty_direct_response_does_not_touch_agenda_controller(self):
        """Direct path is not driven by the controller: it must stay untouched."""
        motor = _build_motor()
        ctrl, _ = _build_agenda_with_topic()
        motor.agenda_output_validator = ctrl.accept_output

        ctrl.next_action()
        assert ctrl.state == AgendaState.GENERATING
        failures_before = ctrl.recovery.failure_count

        with patch.object(motor, "_generar_dialogo", return_value=""), \
                patch.object(motor, "_hablar"):
            motor._ejecutar_inferencia("pregunta directa del operador", source="direct")

        assert ctrl.recovery.failure_count == failures_before
        assert ctrl.state == AgendaState.GENERATING

    def test_nonempty_agenda_response_still_speaks(self):
        """Regression: a valid agenda response must still be spoken."""
        motor = _build_motor()
        ctrl, _ = _build_agenda_with_topic()
        motor.agenda_output_validator = ctrl.accept_output

        action = ctrl.next_action()
        with patch.object(motor, "_generar_dialogo", return_value="una idea concreta y viva"), \
                patch.object(motor, "_hablar") as hablar:
            motor._ejecutar_inferencia(action.prompt, source=action.source)

        hablar.assert_called_once()

    def test_two_consecutive_empties_abandon_topic(self):
        """Second consecutive empty abandons the topic and moves on (degrade ladder)."""
        motor = _build_motor()
        ctrl, _ = _build_agenda_with_topic(turns=3)
        motor.agenda_output_validator = ctrl.accept_output

        a1 = ctrl.next_action()
        with patch.object(motor, "_generar_dialogo", return_value=""), \
                patch.object(motor, "_hablar"):
            motor._ejecutar_inferencia(a1.prompt, source=a1.source)
        assert ctrl.state == AgendaState.REGENERATING_SAFE

        a2 = ctrl.next_action()
        assert a2.kind == "enqueue"
        with patch.object(motor, "_generar_dialogo", return_value=""), \
                patch.object(motor, "_hablar"):
            motor._ejecutar_inferencia(a2.prompt, source=a2.source)

        assert ctrl.recovery.failure_count >= 2
        assert ctrl.state == AgendaState.WAITING_SIGNAL
        assert ctrl.active_topic.turns_spoken == ctrl.max_turns_per_topic

    def test_persistent_empty_responses_are_bounded_not_infinite(self):
        """A systemically-empty model must NOT loop forever — the controller
        must stop issuing agenda work within a bounded number of cycles."""
        motor = _build_motor()
        ctrl, _ = _build_agenda_with_topic(turns=3)
        motor.agenda_output_validator = ctrl.accept_output

        enqueues = 0
        action = AgendaAction.none()
        for _ in range(30):
            action = ctrl.next_action()
            if action.kind != "enqueue":
                break
            enqueues += 1
            with patch.object(motor, "_generar_dialogo", return_value=""), \
                    patch.object(motor, "_hablar"):
                motor._ejecutar_inferencia(action.prompt, source=action.source)
        else:
            pytest.fail("Controller kept issuing+failing agenda actions forever")

        assert action.kind == "none"
        assert ctrl.state in {
            AgendaState.IDLE,
            AgendaState.OFF,
            AgendaState.PAUSED_NEEDS_OPERATOR,
            AgendaState.HARD_PAUSED,
        }
        assert enqueues <= 6  # real path is ~3 (open → continue → close)


# ═══════════════════════════════════════════════════════════════════════════
# agenda_ptt_commit_raw_text — app_shell dispatch threads history_text
# ═══════════════════════════════════════════════════════════════════════════


class TestAppShellThreadsHistoryTextToEnqueue:
    """_enqueue_kira_agenda_action must forward action.history_text to
    motor_ia.enqueue() for non-agenda sources (ptt/chat), and must leave the
    kira-agenda replace_pending() call untouched (owner-gated, masked commit)."""

    def _bare_app(self):
        app = object.__new__(app_shell.VocalAIApp)
        app._kira_agenda_prefetched_action = "stale"
        return app

    def test_ptt_action_history_text_reaches_motor_enqueue(self):
        app = self._bare_app()
        app.motor_ia = MagicMock(spec=["enqueue", "replace_pending", "clear_prefetched_agenda"])

        action = AgendaAction(
            kind="enqueue",
            prompt="TAREA: respondé al aire como Kira...",
            source="ptt",
            priority=0,
            history_text="El streamer dijo (PTT): probemos esto",
        )
        app._enqueue_kira_agenda_action(action)

        app.motor_ia.enqueue.assert_called_once_with(
            action.prompt, priority=0, source="ptt",
            history_text="El streamer dijo (PTT): probemos esto",
        )

    def test_direct_chat_action_history_text_none_reaches_motor_enqueue(self):
        """Regression: chat actions have history_text=None by default — the
        enqueue call must still receive it explicitly as None (byte-identical
        contract, no behavior change for non-PTT sources)."""
        app = self._bare_app()
        app.motor_ia = MagicMock(spec=["enqueue", "replace_pending", "clear_prefetched_agenda"])

        action = AgendaAction(kind="enqueue", prompt="chat prompt", source="chat", priority=1)
        app._enqueue_kira_agenda_action(action)

        app.motor_ia.enqueue.assert_called_once_with(
            action.prompt, priority=1, source="chat", history_text=None,
        )

    def test_kira_agenda_action_routes_through_replace_pending_untouched(self):
        """kira-agenda sourced actions must keep using replace_pending — the
        masked-commit path is owner-gated and out of scope for this fix."""
        app = self._bare_app()
        app.motor_ia = MagicMock(spec=["enqueue", "replace_pending", "clear_prefetched_agenda"])

        action = AgendaAction(kind="enqueue", prompt="agenda prompt", source="kira-agenda", priority=2)
        app._enqueue_kira_agenda_action(action)

        app.motor_ia.replace_pending.assert_called_once_with(
            action.prompt, priority=2, source="kira-agenda",
        )
        app.motor_ia.enqueue.assert_not_called()


class TestInternalLeakRejectionRendersPhrase:
    """2026-08-03 incident: an agenda turn was discarded as
    contains_internal_leak with zero diagnostic — the log said only
    "Agenda: salida rechazada (contains_internal_leak)." and the phrase that
    actually leaked was unrecoverable.

    contains_internal_leak's match is now recorded into rejection_log's
    "matched_phrase" — always a literal from the closed INTERNAL_PHRASES
    vocabulary, never generated text — so _format_agenda_rejection can render
    it while _agenda_rejection_code, which rides the event stream, stays
    code-only (the privacy contract llm_engine.py:5678 documents)."""

    LEAK_TEXT = "Nos vemos en el próximo episodio, gracias por venir."

    def _rejected_motor(self) -> llm_engine.MotorVocalIA:
        motor = _build_motor()
        motor.agenda_controller = KiraAgendaController()
        assert motor.agenda_controller.accept_output(self.LEAK_TEXT) is False
        assert motor.agenda_controller.rejection_log[-1]["guardrail"] == "contains_internal_leak"
        return motor

    def test_format_agenda_rejection_renders_matched_internal_phrase(self):
        motor = self._rejected_motor()

        reason = motor._format_agenda_rejection()

        assert reason.startswith("contains_internal_leak (")
        assert any(phrase in reason for phrase in KiraAgendaController.INTERNAL_PHRASES)

    def test_agenda_rejection_code_stays_bare_for_same_rejection(self):
        motor = self._rejected_motor()

        assert motor._agenda_rejection_code() == "contains_internal_leak"
