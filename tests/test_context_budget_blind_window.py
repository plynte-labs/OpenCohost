"""
Comprehensive test suite for Context-Budget Blind Window (BW-01 to BW-14).

Formal Invariants:
    - INV-VIS: Every committed conversational turn eligible for continuity MUST be
      represented at least once: verbatim in the active prompt messages, OR as a
      memory digest entry. It can NEVER be absent from both.
    - INV-DEDUP: A turn already digested on budget gate MUST NEVER produce a duplicate
      entry when deque reaches maxlen rotation.
    - INV-PROV: Synthetic agenda prompts and private turns remain strictly barred from digestion.
"""
import queue
import threading
import pytest
from unittest.mock import MagicMock

from opencohost.core.context import context_budget as cb
from opencohost.core.llm_engine import MotorVocalIA
from opencohost.core.engine.llm_engine_memorias import MemoriaCaptureMixin


# ─────────────────────────────────────────────────────────────────────────────
# Pure Context Budget Partitioning (BW-01, BW-03, BW-04, BW-05, BW-06, BW-12, BW-13)
# ─────────────────────────────────────────────────────────────────────────────

def test_bw_01_pure_zero_eviction():
    """BW-01: When total chars are within budget, zero pairs are evicted."""
    messages = [
        {"role": "system", "content": "You are Kira."},
        {"role": "user", "content": "Hola Kira"},
        {"role": "assistant", "content": "Hola streamer"},
        {"role": "user", "content": "Como estas?"},
    ]
    retained, evicted_pairs, n_evicted = cb.apply_char_budget_pure(
        messages,
        ctx_limit=4096,
        max_output_tokens=768,
        safety_factor=0.95,
    )
    assert n_evicted == 0
    assert len(evicted_pairs) == 0
    assert len(retained) == 4


def test_bw_03_pure_multiple_pairs_evicted_order():
    """BW-03: When multiple pairs are evicted, they are returned in strict chronological order."""
    messages = [
        {"role": "system", "content": "System prompt"},
        {"role": "user", "content": "Turn 1 User " + "A" * 1000},
        {"role": "assistant", "content": "Turn 1 Asst " + "B" * 1000},
        {"role": "user", "content": "Turn 2 User " + "C" * 1000},
        {"role": "assistant", "content": "Turn 2 Asst " + "D" * 1000},
        {"role": "user", "content": "Current turn " + "E" * 500},
    ]
    retained, evicted_pairs, n_evicted = cb.apply_char_budget_pure(
        messages,
        ctx_limit=2000,
        max_output_tokens=200,
        safety_factor=0.95,
    )
    assert n_evicted == 2
    assert len(evicted_pairs) == 2
    assert "Turn 1 User" in evicted_pairs[0][0]["content"]
    assert "Turn 1 Asst" in evicted_pairs[0][1]["content"]
    assert "Turn 2 User" in evicted_pairs[1][0]["content"]
    assert "Turn 2 Asst" in evicted_pairs[1][1]["content"]
    assert retained[0]["role"] == "system"
    assert "Current turn" in retained[-1]["content"]


def test_bw_04_pure_system_message_is_never_evicted():
    """BW-04: System message (role=system at index 0) is protected from eviction."""
    messages = [
        {"role": "system", "content": "Critical System Prompt " + "S" * 3000},
        {"role": "user", "content": "Turn 1 User " + "U" * 500},
        {"role": "assistant", "content": "Turn 1 Asst " + "A" * 500},
        {"role": "user", "content": "Current turn"},
    ]
    retained, evicted_pairs, n_evicted = cb.apply_char_budget_pure(
        messages,
        ctx_limit=2000,
        max_output_tokens=200,
        safety_factor=0.95,
    )
    assert n_evicted == 1
    assert retained[0]["role"] == "system"
    assert "Critical System Prompt" in retained[0]["content"]
    assert len(evicted_pairs) == 1
    assert "Turn 1 User" in evicted_pairs[0][0]["content"]


def test_bw_05_pure_odd_orphan_turn_eviction_safe():
    """BW-05: Odd/orphan single message in history is evicted safely without corrupting structure."""
    messages = [
        {"role": "system", "content": "System prompt"},
        {"role": "user", "content": "Orphan user turn " + "O" * 2000},
        {"role": "user", "content": "Current turn"},
    ]
    retained, evicted_pairs, n_evicted = cb.apply_char_budget_pure(
        messages,
        ctx_limit=1000,
        max_output_tokens=200,
        safety_factor=0.95,
    )
    assert n_evicted == 1
    assert len(evicted_pairs) == 1
    assert "Orphan user turn" in evicted_pairs[0][0]["content"]
    assert evicted_pairs[0][1]["content"] == ""
    assert len(retained) == 2
    assert retained[0]["role"] == "system"
    assert retained[1]["content"] == "Current turn"


def test_bw_06_pure_current_turn_protected():
    """BW-06: The final message (current turn, index -1) is never evicted even if oversized."""
    messages = [
        {"role": "system", "content": "Sys"},
        {"role": "user", "content": "Old user turn"},
        {"role": "assistant", "content": "Old asst turn"},
        {"role": "user", "content": "Huge current prompt " + "Z" * 5000},
    ]
    retained, evicted_pairs, n_evicted = cb.apply_char_budget_pure(
        messages,
        ctx_limit=1000,
        max_output_tokens=200,
        safety_factor=0.95,
    )
    assert n_evicted == 1
    assert "Huge current prompt" in retained[-1]["content"]


def test_bw_12_trim_messages_reactive_preserves_invariants():
    """BW-12: Reactive trimming (trim_messages_reactive) drops oldest pairs from evictable slice."""
    messages = [
        {"role": "system", "content": "Sys"},
        {"role": "user", "content": "Turn 1 User"},
        {"role": "assistant", "content": "Turn 1 Asst"},
        {"role": "user", "content": "Turn 2 User"},
        {"role": "assistant", "content": "Turn 2 Asst"},
        {"role": "user", "content": "Current turn"},
    ]
    dropped = cb.trim_messages_reactive(messages, n_pairs=1)
    assert dropped == 1
    assert len(messages) == 4
    assert messages[0]["role"] == "system"
    assert "Turn 2 User" in messages[1]["content"]
    assert "Current turn" in messages[-1]["content"]


def test_bw_13_use_system_role_false_evicts_index_0():
    """BW-13: When index 0 is not a system message (use_system_role=False), it is evictable."""
    messages = [
        {"role": "user", "content": "Oldest User Turn " + "X" * 2000},
        {"role": "assistant", "content": "Oldest Asst Turn " + "Y" * 2000},
        {"role": "user", "content": "Current turn prompt"},
    ]
    retained, evicted_pairs, n_evicted = cb.apply_char_budget_pure(
        messages,
        ctx_limit=1000,
        max_output_tokens=200,
        safety_factor=0.95,
    )
    assert n_evicted == 1
    assert len(evicted_pairs) == 1
    assert "Oldest User Turn" in evicted_pairs[0][0]["content"]
    assert len(retained) == 1
    assert retained[0]["content"] == "Current turn prompt"


# ─────────────────────────────────────────────────────────────────────────────
# Engine Integration & Invariant Tests (BW-02, BW-07, BW-08, BW-09, BW-10, BW-11, BW-14)
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def motor_instance():
    """Create a test MotorVocalIA instance."""
    log_q = queue.Queue()
    motor = MotorVocalIA(log_q, lambda event: None)
    motor._current_profile_id = "test-profile"
    return motor


def test_bw_02_reproduce_blind_window_and_verify_fix(motor_instance):
    """
    BW-02 (Core Defect Fix): Turn 1 is committed. Turn 2 is huge and causes char budget eviction
    BEFORE deque reaches maxlen (len(historial) = 2 < 6).
    
    Invariant (INV-VIS): Turn 1 must NOT disappear into a blind window.
    It must be present in _memory_digest before Ollama inference.
    """
    motor = motor_instance
    motor._commit_history(
        contexto="Usuario: Hola Kira, hablamos de la arquitectura modular",
        dialogo="Kira: Excelente tema, la arquitectura modular desacopla componentes",
        source="direct",
    )
    assert len(motor.historial) == 2
    assert len(motor._memory_digest.lines) == 0

    captured_messages = []
    def mock_chat_watchdog(*args, **kwargs):
        captured_messages.extend(kwargs.get("messages", []))
        return {"message": {"content": "Respuesta al turno 2"}}

    motor._ollama_chat_with_watchdog = mock_chat_watchdog
    motor._model_ctx_limit["llama3"] = 1024

    huge_user_input = "Pregunta sobre el sistema con mucho texto: " + "X" * 1500
    motor._generar_dialogo(huge_user_input, "direct")

    # Turn 1 was evicted from raw messages sent to Ollama:
    assert not any("Hola Kira, hablamos de la arquitectura modular" in m.get("content", "") for m in captured_messages)
    
    # INV-VIS: Turn 1 MUST be captured in _memory_digest!
    assert len(motor._memory_digest.lines) >= 1
    digest_text = "\n".join(motor._memory_digest.lines)
    assert "arquitectura" in digest_text.lower() or "modular" in digest_text.lower()


def test_bw_07_no_duplicate_digest_on_repeated_budget_eval(motor_instance):
    """
    BW-07: Evaluating budget multiple times on the same evicted turn does NOT
    produce duplicate entries in _memory_digest (INV-DEDUP).
    """
    motor = motor_instance
    motor._commit_history(
        contexto="Usuario: Primer tema de conversacion clave",
        dialogo="Kira: Respuesta sobre el primer tema",
        source="direct",
    )
    motor._model_ctx_limit["llama3"] = 1024
    motor._ollama_chat_with_watchdog = lambda *args, **kwargs: {"message": {"content": "Ok"}}

    motor._generar_dialogo("Largo 1 " + "Y " * 200, "direct")
    motor._generar_dialogo("Largo 2 " + "Z " * 200, "direct")

    matching_entries = [d for d in motor._memory_digest.lines if "primer tema" in d.lower()]
    assert len(matching_entries) == 1


def test_bw_08_no_duplicate_digest_on_later_deque_eviction(motor_instance):
    """
    BW-08: When a turn was proactively digested by budget eviction, later deque
    rotation at maxlen (6 items) MUST NOT write a second duplicate ledger line.
    """
    motor = motor_instance
    motor._commit_history("Usuario: Turno Alfa especial", "Kira: Respuesta Alfa", source="direct")
    motor._model_ctx_limit["llama3"] = 1024
    motor._ollama_chat_with_watchdog = lambda *args, **kwargs: {"message": {"content": "Ok"}}

    # Trigger budget eviction on Turn Alfa
    motor._generar_dialogo("Largo que expulsa " + "A " * 200, "direct")
    assert any("turno alfa especial" in d.lower() for d in motor._memory_digest.lines)

    # Fill deque to maxlen (commit Turn 2 and Turn 3)
    motor._commit_history("Usuario: Turno Beta", "Kira: Respuesta Beta", source="direct")
    motor._commit_history("Usuario: Turno Gamma", "Kira: Respuesta Gamma", source="direct")
    
    # Committing Turn Delta pushes Alfa out of the deque
    motor._commit_history("Usuario: Turno Delta", "Kira: Respuesta Delta", source="direct")

    alfa_entries = [d for d in motor._memory_digest.lines if "turno alfa especial" in d.lower()]
    assert len(alfa_entries) == 1, f"Expected 1 entry for Alfa, found {len(alfa_entries)}: {motor._memory_digest.lines}"


def test_bw_09_profile_switch_resets_sidecar_watermark(motor_instance):
    """
    BW-09: Switching profile clears historial, digest, and sidecar watermark
    so no ghost states leak across profiles.
    """
    motor = motor_instance
    motor._commit_history("Usuario: Conversacion de Perfil A", "Kira: Respuesta A", source="direct")
    motor._model_ctx_limit["llama3"] = 1024
    motor._ollama_chat_with_watchdog = lambda *args, **kwargs: {"message": {"content": "Ok"}}
    motor._generar_dialogo("Largo " + "W " * 200, "direct")

    motor._dispatch_command("set_profile", {"id": "perfil-b", "prompt": "Prompt B", "_profile_name": "Perfil B"})
    assert len(motor.historial) == 0
    assert len(motor._memory_digest.lines) == 0
    assert len(getattr(motor, "_digested_turn_keys", set())) == 0


def test_bw_10_clear_history_resets_sidecar_watermark(motor_instance):
    """
    BW-10: clear_history command clears historial, digest, and sidecar watermark.
    """
    motor = motor_instance
    motor._commit_history("Usuario: Conversacion a borrar", "Kira: Respuesta a borrar", source="direct")
    motor._model_ctx_limit["llama3"] = 1024
    motor._ollama_chat_with_watchdog = lambda *args, **kwargs: {"message": {"content": "Ok"}}
    motor._generar_dialogo("Largo " + "K " * 200, "direct")

    assert len(motor._memory_digest.lines) > 0
    motor._dispatch_command("clear_history", {})
    assert len(motor.historial) == 0
    assert len(motor._memory_digest.lines) == 0
    assert len(getattr(motor, "_digested_turn_keys", set())) == 0


def test_bw_11_provenance_isolation_agenda_and_private(motor_instance):
    """
    BW-11 (INV-PROV): Synthetic agenda prompts ([agenda segura...]) and private turns
    must NOT be written to _memory_digest during budget eviction.
    """
    motor = motor_instance
    motor._commit_history(
        contexto="[agenda segura: prompt interno]",
        dialogo="Kira: Respuesta agenda",
        source="kira-agenda",
    )
    motor._model_ctx_limit["llama3"] = 1024
    motor._ollama_chat_with_watchdog = lambda *args, **kwargs: {"message": {"content": "Ok"}}

    motor._generar_dialogo("Largo " + "M " * 200, "direct")
    assert not any("agenda segura" in d for d in motor._memory_digest.lines)


def test_bw_14_concurrency_lock_safety(motor_instance):
    """
    BW-14: Rapid concurrent turns across threads do not cause deadlocks or
    corrupt the sidecar watermark / _memory_digest.
    """
    motor = motor_instance
    motor._model_ctx_limit["llama3"] = 2048
    motor._ollama_chat_with_watchdog = lambda *args, **kwargs: {"message": {"content": "Ok"}}

    errors = []
    def worker(worker_id):
        try:
            for i in range(10):
                motor._commit_history(
                    f"Usuario {worker_id}: Mensaje {i} " + "T " * 20,
                    f"Kira: Respuesta {i} " + "R " * 20,
                    source="direct",
                )
                motor._generar_dialogo(f"Prompt {worker_id}-{i} " + "P " * 50, "direct")
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(t_id,)) for t_id in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"Concurrent workers encountered errors: {errors}"
    assert len(motor.historial) <= motor.historial.maxlen


# ─────────────────────────────────────────────────────────────────────────────
# Explicit Boundary Defense Test (user_summary 100-character bound)
# ─────────────────────────────────────────────────────────────────────────────

def test_bw_user_summary_unspaced_token_bound():
    """
    Explicit regression test for user_summary 100-character bound:
    An adversarial or synthetic prompt containing a single unspaced token of 1200 chars
    must be capped at 100 chars in _build_ledger_line so it does NOT consume the
    entire 600-char MemoryDigest buffer and prematurely purge older turns.
    """
    raw_user_turn = "Usuario: " + "X" * 1200
    raw_asst_turn = "Kira: Respuesta normal a turno largo."
    
    ledger_line = MemoriaCaptureMixin._build_ledger_line(raw_user_turn, raw_asst_turn)
    # The ledger line must be bounded and well under MemoryDigest max_chars (600)
    assert len(ledger_line) < 200
    assert "X" * 101 not in ledger_line
    assert "X" * 90 in ledger_line