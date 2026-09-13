"""Tests for bounded reasoning recovery and visible terminal failures.

Verifies:
1. Attempt 1 thinking-only -> attempt 2 succeeds with escalated budget.
2. Attempt 2 thinking-only -> no phantom escalation, no 3rd call, failure_reason="reasoning_budget_exhausted".
3. Custom user budget -> no escalation, immediate exhaustion.
4. Context/task ceiling prevents escalation -> no escalation, immediate exhaustion.
5. Terminal exhaustion propagates without generic outer replay in _generar_dialogo.
6. Exactly one failure notification (reasoning_budget_exhausted), no assistant history, no memory write, no TTS.
7. Following queued owner request proceeds and completes.
8. Partial streamed prefix preserved without replay or false exhaustion claim.
9. Non-reasoning successful response unchanged.
10. Cancellation, transport failure, and excluded sources (chat/agenda) unchanged.
11. Pregen terminal exhaustion prevents live double-generation at queue pop boundary.
"""

import threading
import time
from typing import Any, Optional
from unittest.mock import MagicMock

import pytest

from opencohost.api.engine_host import _MOTOR_EVENT_WHITELIST
from opencohost.core.context.prompt_assembler import GenerationSetup
from opencohost.core.engine.generation_orchestrator import (
    GenerationAttemptOutcome,
    GenerationOrchestrator,
    GenerationResult,
    StreamAttemptState,
)
from opencohost.core.llm_engine import MotorVocalIA, OWNER_BUNDLE_SOURCE


class _FakeResp:
    def __init__(self, content: str = "", thinking: str = "", prompt_eval_count: int = 100):
        self._message = {"content": content, "thinking": thinking}
        self.prompt_eval_count = prompt_eval_count

    def get(self, key: str, default=None):
        if key == "message":
            return self._message
        if key == "prompt_eval_count":
            return self.prompt_eval_count
        return default


class _FakeHost:
    def __init__(self, model: str = "gemma4:e4b"):
        self.current_model = model
        self._lock = threading.Lock()
        self._llm_generating = False
        self._logs = []
        self._reasoning_model_cache = {}
        self._speech_router_enabled = False

    def _log(self, msg: str, level: str = "info"):
        self._logs.append((level, msg))

    def _is_watchdog_timeout_error(self, e):
        return False

    def _is_ollama_transport_error(self, e):
        return False

    def _sanitize_for_tts(self, s):
        return [s]

    def _fragment_for_tts(self, p):
        return p

    def _speech_cancelled(self, s):
        return False


def _make_setup(
    num_predict: int = 272,
    max_intentos: int = 2,
    preset: str = "balanced",
    intent: str = "chat",
    effective_ctx: int = 4096,
) -> GenerationSetup:
    return GenerationSetup(
        messages=[{"role": "user", "content": "hello"}],
        opciones_llm={"num_predict": num_predict},
        chat_timeout=30.0,
        max_intentos=max_intentos,
        start_llm=time.time(),
        native_ctx=131072,
        effective_ctx=effective_ctx,
        ctx_evicted=0,
        editorial_block="",
        history_snapshot=[],
        preset=preset,
        intent=intent,
    )


def _make_motor_instance(model: str = "gemma4:e4b") -> MotorVocalIA:
    m = MotorVocalIA.__new__(MotorVocalIA)
    m._provider_config = {"active_provider": "local", "profiles": {}}
    m._cloud_fallback_active = False
    m._cloud_fallback_reason = None
    m._last_known_good_model = None
    m._reasoning_model_cache = {model: True}
    m._model_ctx_limit = {model: 4096}
    m.current_model = model
    m._desired_model = model
    m._loaded_model = model
    m._current_profile_name = "default"
    m._current_profile_id = "default"
    m.use_system_role = True
    m.system_prompt = "sys"
    from opencohost.core.turn_scheduler import TurnScheduler
    m._turn_scheduler = TurnScheduler()
    m._history_lock = threading.RLock()
    m._lock = threading.Lock()
    m._prefetch_lock = threading.Lock()
    m._prefetch_done = threading.Event()
    m._prefetched_agenda = None
    m._pregen_inflight = None
    m._prefetch_epoch = 0
    m._prefetch_thread = None
    m.historial = []
    m._last_llm_failure = None
    m._logs = []
    m._log = lambda msg, level="info": m._logs.append((level, msg))
    m._mark_model_generation_success = lambda *a, **k: None
    m._resolve_chat_watchdog_timeout = lambda *a, **k: 30.0
    m._resolve_reasoning_settings = lambda model: {"enabled": True, "budget_tokens": 272}
    m._cfg_is_local = lambda *a, **k: True
    m.ui_events = []
    m.ui_callback = lambda ev, *a, **k: m.ui_events.append(ev)
    m.spoken_dialogues = []
    m._speak_or_submit = lambda d, source="direct": m.spoken_dialogues.append((d, source))
    m.emitted_dialogues = []
    m._emit_dialogue = lambda d, source, **k: m.emitted_dialogues.append((d, source))
    m._speaking = False
    m._speech_router = None
    m._speech_router_enabled = False
    m._test_pop_boundary_hook = None
    m._streamed_turn_job = None
    m._streamed_turn_prefix = None
    m._inference_watchdog_timeout = 5.0
    m._speech_ms_for_boundary = lambda: 0
    m._last_speaking_end_monotonic = None
    m._requeue_owner_bundle_followers = MagicMock()
    m._accept_agenda_output = MagicMock(return_value=True)
    m._record_accepted_agenda_output = MagicMock()
    m.log_queue = MagicMock()
    m._note_detour_turn = MagicMock()
    m._memory_digest = MagicMock(build_block=lambda *a, **k: "")
    return m


# ──────────────────────────────────────────────────────────────────────────
# Test 1: Attempt 1 thinking-only -> attempt 2 succeeds with escalated budget
# ──────────────────────────────────────────────────────────────────────────
def test_reasoning_recovery_first_attempt_escalates_and_second_succeeds():
    host = _FakeHost()
    orchestrator = GenerationOrchestrator(host)
    setup = _make_setup(num_predict=272, max_intentos=2)

    responses = [
        _FakeResp(content="", thinking="thinking deep..."),
        _FakeResp(content="Here is the final answer.", thinking="done thinking"),
    ]
    captured_options = []

    def mock_chat(*, options, **kwargs):
        captured_options.append(dict(options))
        return responses.pop(0)

    host._ollama_chat_with_watchdog = mock_chat

    outcome = orchestrator.execute_generation_attempt(
        setup,
        source="direct",
        commit_history=True,
        is_local=True,
        provider_cfg={},
        request_model="gemma4:e4b",
        watchdog_timeout=30.0,
    )

    assert len(captured_options) == 2
    assert captured_options[0]["num_predict"] == 272
    assert captured_options[1]["num_predict"] == 544
    assert outcome.raw_content == "Here is the final answer."
    assert outcome.failure_reason is None


# ──────────────────────────────────────────────────────────────────────────
# Test 2: Attempt 2 thinking-only -> no phantom escalation, no 3rd call
# ──────────────────────────────────────────────────────────────────────────
def test_reasoning_recovery_second_attempt_exhausted_no_phantom_escalation():
    host = _FakeHost()
    orchestrator = GenerationOrchestrator(host)
    setup = _make_setup(num_predict=272, max_intentos=2)

    responses = [
        _FakeResp(content="", thinking="thinking attempt 1..."),
        _FakeResp(content="", thinking="thinking attempt 2..."),
    ]
    captured_options = []

    def mock_chat(*, options, **kwargs):
        captured_options.append(dict(options))
        return responses.pop(0)

    host._ollama_chat_with_watchdog = mock_chat

    outcome = orchestrator.execute_generation_attempt(
        setup,
        source="direct",
        commit_history=True,
        is_local=True,
        provider_cfg={},
        request_model="gemma4:e4b",
        watchdog_timeout=30.0,
    )

    assert len(captured_options) == 2
    assert captured_options[0]["num_predict"] == 272
    assert captured_options[1]["num_predict"] == 544
    # Crucial: options must NOT be mutated to 1024 phantom budget on final attempt!
    assert setup.opciones_llm["num_predict"] == 544
    # Must NOT log phantom escalation (intento 2/2)
    phantom_logs = [msg for lvl, msg in host._logs if "escalando a 1024" in msg]
    assert len(phantom_logs) == 0, f"Phantom escalation logged: {phantom_logs}"
    # Must log REASONING_BUDGET_EXHAUSTED
    exhausted_logs = [msg for lvl, msg in host._logs if "REASONING_BUDGET_EXHAUSTED" in msg]
    assert len(exhausted_logs) >= 1
    assert outcome.raw_content == ""
    assert outcome.failure_reason == "reasoning_budget_exhausted"


# ──────────────────────────────────────────────────────────────────────────
# Test 3: Custom user budget -> no escalation, immediate exhaustion
# ──────────────────────────────────────────────────────────────────────────
def test_reasoning_recovery_custom_user_budget_exhaustion():
    host = _FakeHost()
    orchestrator = GenerationOrchestrator(host)
    setup = _make_setup(num_predict=500, max_intentos=2, preset="custom")

    responses = [_FakeResp(content="", thinking="thinking with custom budget...")]
    captured_options = []

    def mock_chat(*, options, **kwargs):
        captured_options.append(dict(options))
        return responses.pop(0)

    host._ollama_chat_with_watchdog = mock_chat

    outcome = orchestrator.execute_generation_attempt(
        setup,
        source="direct",
        commit_history=True,
        is_local=True,
        provider_cfg={},
        request_model="gemma4:e4b",
        watchdog_timeout=30.0,
    )

    assert len(captured_options) == 1
    assert setup.opciones_llm["num_predict"] == 500
    assert outcome.raw_content == ""
    assert outcome.failure_reason == "reasoning_budget_exhausted"


# ──────────────────────────────────────────────────────────────────────────
# Test 4: Context/task ceiling prevents escalation
# ──────────────────────────────────────────────────────────────────────────
def test_reasoning_recovery_ceiling_prevents_escalation():
    host = _FakeHost()
    orchestrator = GenerationOrchestrator(host)
    # Chat intent task_cap is 1024. If current is 1024, min(1024*2, 1024) = 1024 <= 1024
    setup = _make_setup(num_predict=1024, max_intentos=2, intent="chat")

    responses = [_FakeResp(content="", thinking="already at cap...")]
    captured_options = []

    def mock_chat(*, options, **kwargs):
        captured_options.append(dict(options))
        return responses.pop(0)

    host._ollama_chat_with_watchdog = mock_chat

    outcome = orchestrator.execute_generation_attempt(
        setup,
        source="direct",
        commit_history=True,
        is_local=True,
        provider_cfg={},
        request_model="gemma4:e4b",
        watchdog_timeout=30.0,
    )

    assert len(captured_options) == 1
    assert setup.opciones_llm["num_predict"] == 1024
    assert outcome.raw_content == ""
    assert outcome.failure_reason == "reasoning_budget_exhausted"


# ──────────────────────────────────────────────────────────────────────────
# Test 5: Terminal exhaustion propagates without generic outer replay in _generar_dialogo
# ──────────────────────────────────────────────────────────────────────────
def test_reasoning_terminal_exhaustion_propagates_without_outer_retry(monkeypatch):
    import opencohost.core.llm_engine as le
    monkeypatch.setattr(le, "output_guard", lambda dialogo, source="direct": (True, ""))

    m = _make_motor_instance("gemma4:e4b")
    call_count = 0

    def fake_chat(*, options, **kwargs):
        nonlocal call_count
        call_count += 1
        return _FakeResp(content="", thinking="exhausted reasoning")

    m._ollama_chat_with_watchdog = fake_chat

    result = m._generar_dialogo("hola", source="direct", commit_history=True)

    assert result == ""
    assert getattr(result, "failure_reason", None) == "reasoning_budget_exhausted"
    # Early return short-circuited _finalize_generation -> no history committed
    assert len(m.historial) == 0
    # max_intentos is 2, so exactly 2 calls occurred; no outer loop restarted it
    assert call_count == 2


# ──────────────────────────────────────────────────────────────────────────
# Test 6: Exactly one failure notification, no assistant history, no TTS
# ──────────────────────────────────────────────────────────────────────────
def test_owner_turn_lifecycle_terminal_exhaustion_emits_ui_notification_only(monkeypatch):
    m = _make_motor_instance("gemma4:e4b")
    m._generar_dialogo = lambda *a, **k: GenerationResult("", failure_reason="reasoning_budget_exhausted")

    m._ejecutar_inferencia("¿Por qué el cielo es azul?", source="direct")

    assert m.ui_events == ["reasoning_budget_exhausted"]
    assert len(m.spoken_dialogues) == 0
    assert len(m.emitted_dialogues) == 0
    assert len(m.historial) == 0


# ──────────────────────────────────────────────────────────────────────────
# Test 7: Following queued owner request proceeds and completes
# ──────────────────────────────────────────────────────────────────────────
def test_following_queued_owner_request_proceeds_after_exhaustion(monkeypatch):
    m = _make_motor_instance("gemma4:e4b")

    call_seq = [
        # First turn fails with reasoning exhaustion
        ("", "reasoning_budget_exhausted"),
        # Second turn succeeds
        ("El cielo es azul por la dispersión de Rayleigh.", None),
    ]

    def fake_generar(contexto, source="direct", **kwargs):
        dialogo, reason = call_seq.pop(0)
        return GenerationResult(dialogo, failure_reason=reason)

    m._generar_dialogo = fake_generar

    # Turn 1
    m._ejecutar_inferencia("Pregunta 1", source="direct")
    assert m.ui_events == ["reasoning_budget_exhausted"]
    assert len(m.spoken_dialogues) == 0

    # Turn 2
    m._ejecutar_inferencia("Pregunta 2", source="direct")
    assert len(m.spoken_dialogues) == 1
    assert m.spoken_dialogues[0][0] == "El cielo es azul por la dispersión de Rayleigh."
    assert m.emitted_dialogues[-1][0] == "El cielo es azul por la dispersión de Rayleigh."


# ──────────────────────────────────────────────────────────────────────────
# Test 8: Partial streamed prefix preserved without false exhaustion claim
# ──────────────────────────────────────────────────────────────────────────
def test_partial_streamed_prefix_preserved_without_exhaustion_claim():
    m = _make_motor_instance("gemma4:e4b")
    def fake_generar(*a, **k):
        m._streamed_turn_prefix = "Entiendo lo que dices sobre los embeddings."
        return GenerationResult("", failure_reason="reasoning_budget_exhausted")

    m._generar_dialogo = fake_generar

    m._ejecutar_inferencia("Explícame embeddings", source="direct")

    # Spoken prefix was already emitted to audience during streaming, so dialogue is emitted
    assert len(m.emitted_dialogues) == 1
    assert m.emitted_dialogues[0][0] == "Entiendo lo que dices sobre los embeddings."
    # reasoning_budget_exhausted and turn_dropped must NOT fire when prefix was spoken
    assert "reasoning_budget_exhausted" not in m.ui_events
    assert "turn_dropped" not in m.ui_events


# ──────────────────────────────────────────────────────────────────────────
# Test 9: Non-reasoning successful response unchanged
# ──────────────────────────────────────────────────────────────────────────
def test_non_reasoning_successful_response_unaffected():
    m = _make_motor_instance("llama3")
    m._generar_dialogo = lambda *a, **k: GenerationResult("Todo bien por aquí.")

    m._ejecutar_inferencia("¿Cómo estás?", source="direct")

    assert len(m.spoken_dialogues) == 1
    assert m.spoken_dialogues[0][0] == "Todo bien por aquí."
    assert len(m.emitted_dialogues) == 1
    assert "reasoning_budget_exhausted" not in m.ui_events


# ──────────────────────────────────────────────────────────────────────────
# Test 10: Cancellation, transport failure, and excluded sources unchanged
# ──────────────────────────────────────────────────────────────────────────
def test_excluded_sources_and_transports_unaffected():
    # Chat source
    m_chat = _make_motor_instance("gemma4:e4b")
    m_chat._generar_dialogo = lambda *a, **k: GenerationResult("", failure_reason="reasoning_budget_exhausted")
    m_chat._ejecutar_inferencia("mensaje de chat", source="chat")
    assert "reasoning_budget_exhausted" not in m_chat.ui_events

    # Agenda source
    m_agenda = _make_motor_instance("gemma4:e4b")
    m_agenda._generar_dialogo = lambda *a, **k: GenerationResult("", failure_reason="reasoning_budget_exhausted")
    m_agenda._ejecutar_inferencia("tema agenda", source="kira-agenda:topic1")
    assert "reasoning_budget_exhausted" not in m_agenda.ui_events
    m_agenda._accept_agenda_output.assert_called_once_with("")

    # Turn dropped for generic non-reasoning empty owner turn
    m_dropped = _make_motor_instance("gemma4:e4b")
    m_dropped._generar_dialogo = lambda *a, **k: GenerationResult("")
    m_dropped._ejecutar_inferencia("pregunta", source="direct")
    assert m_dropped.ui_events == ["turn_dropped"]


# ──────────────────────────────────────────────────────────────────────────
# Test 11: Event whitelist includes reasoning_budget_exhausted
# ──────────────────────────────────────────────────────────────────────────
def test_motor_event_whitelist_contains_reasoning_budget_exhausted():
    assert "reasoning_budget_exhausted" in _MOTOR_EVENT_WHITELIST


# ──────────────────────────────────────────────────────────────────────────
# Test 12: Pregen terminal exhaustion prevents live double-generation
# ──────────────────────────────────────────────────────────────────────────
def test_pregen_exhaustion_prevents_live_double_generation():
    m = _make_motor_instance("gemma4:e4b")
    m._flush_accumulation = lambda: None
    m._complete_processing_cycle = lambda process_queue=False: None
    m._speech_interrupt_enabled = False
    m._processing = False

    payload = "¿Qué es backpropagation?"
    source = "direct"

    # Pre-populate pregen cached failure tombstone
    cached_failure = {
        "payload": payload,
        "dialogo": "",
        "priority": 1,
        "source": source,
        "history_text": None,
        "gen_ms": 13000,
        "failure_reason": "reasoning_budget_exhausted",
    }
    m._prefetched_agenda = cached_failure
    m._ejecutar_inferencia = MagicMock()

    # Enqueue item in real turn scheduler
    m._turn_scheduler.enqueue(payload, source=source, priority=1)

    # Execute actual queue processing loop
    m._process_priority_queue()

    # Verify: UI notified, live foreground inference never invoked
    assert m.ui_events == ["processing", "reasoning_budget_exhausted"]
    m._ejecutar_inferencia.assert_not_called()


# ──────────────────────────────────────────────────────────────────────────
# Test 13: EngineHost records reasoning_budget_exhausted for Tauri UI
# ──────────────────────────────────────────────────────────────────────────
def test_engine_host_records_reasoning_budget_exhausted_event_for_tauri():
    from types import SimpleNamespace
    from opencohost.api.engine_host import EngineHost, EventLogSink

    stub = SimpleNamespace(event_log=EventLogSink())
    EngineHost._record_motor_event(stub, "reasoning_budget_exhausted")

    events = stub.event_log.since(0)["events"]
    assert len(events) == 1
    assert events[0]["source"] == "motor"
    assert events[0]["action"] == "reasoning_budget_exhausted"
    assert events[0]["detail"] is None


# ──────────────────────────────────────────────────────────────────────────
# Test 14: Strict concurrency test: failure_reason isolated per invocation
# ──────────────────────────────────────────────────────────────────────────
def test_concurrent_generation_failure_reason_isolated_per_invocation():
    import sys
    from opencohost.core.engine.generation_orchestrator import GenerationAttemptOutcome

    m = _make_motor_instance("gemma4:e4b")

    barrier = threading.Barrier(2)
    results = {}

    def fake_cloud_attempt_loop(setup, source, **kwargs):
        barrier.wait()
        if source == "thread_failure":
            time.sleep(0.001)
            return GenerationAttemptOutcome(
                raw_content="",
                failure_reason="reasoning_budget_exhausted",
            )
        else:
            time.sleep(0.001)
            return GenerationAttemptOutcome(
                raw_content="Respuesta concurrente exitosa",
                failure_reason=None,
            )

    m._cloud_attempt_loop = fake_cloud_attempt_loop
    m._finalize_generation = lambda setup, outcome, *a, **k: outcome.raw_content

    old_interval = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)

    def run_t1():
        results["t1"] = m._generar_dialogo("pregunta 1", source="thread_failure")

    def run_t2():
        results["t2"] = m._generar_dialogo("pregunta 2", source="thread_success")

    try:
        t1 = threading.Thread(target=run_t1)
        t2 = threading.Thread(target=run_t2)
        t1.start()
        t2.start()
        t1.join()
        t2.join()
    finally:
        sys.setswitchinterval(old_interval)

    # Invariant: Each thread receives its OWN result and failure_reason without shared state leak
    res1 = results["t1"]
    res2 = results["t2"]

    assert res1 == ""
    assert getattr(res1, "failure_reason", None) == "reasoning_budget_exhausted"

    assert res2 == "Respuesta concurrente exitosa"
    assert getattr(res2, "failure_reason", None) is None


