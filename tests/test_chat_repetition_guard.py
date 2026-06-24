"""Integration specs for the chat-reactive anti-repetition fixes in the engine.

FIX 1 (sampling brake) and FIX 2 (reactive structural guard) apply ONLY to
chat-reactive generations, currently represented by ``source == "chat"``. That
intentionally covers RF3 viewer chat, agenda HANDLE_CHAT, and default-enqueue
chat turns. It must NOT affect direct, ptt, accumulated, or kira-agenda paths.
A separate ``event_taxonomy`` track will disambiguate the producers later.

RED today:
  - B-Contract: opciones_llm carries no repeat_penalty -> FAILS until FIX 1.
  - B-Repro: the verbatim/synonym-swap duplicate leaks out -> FAILS until FIX 2.
"""
from __future__ import annotations

import inspect
import queue
from unittest.mock import MagicMock

from opencohost.core.llm_engine import MotorVocalIA, GUARDRAIL_FALLBACK_LINES


def _chat_motor():
    """A MotorVocalIA wired for deterministic chat generation (no real Ollama)."""
    log_q = queue.Queue()
    motor = MotorVocalIA(log_q, lambda event: None)
    motor.ollama = MagicMock()
    motor.pygame = MagicMock()
    motor.is_ready = True
    motor.current_model = "llama3"
    # Short-circuit the reasoning resolver so it never hits the real ollama.show
    # RPC on a box with a live daemon (which would flip num_predict).
    motor._reasoning_model_cache["llama3"] = False
    return motor


def _resp(text):
    return {"message": {"content": text}}


# ── B-Contract — FIX 1 (RED until the sampling brake is wired) ────────────────

def test_chat_passes_repeat_penalty():
    motor = _chat_motor()
    motor._ollama_chat = MagicMock(return_value=_resp("El juego se esta poniendo bueno, gente."))
    motor._generar_dialogo("hola", source="chat", commit_history=False)

    assert motor._ollama_chat.called
    opts = motor._ollama_chat.call_args.kwargs["options"]
    assert "repeat_penalty" in opts and opts["repeat_penalty"] >= 1.1
    # The brake ADDS keys, never rewrites the existing sampling config.
    assert opts["temperature"] == 0.8 and opts["top_p"] == 0.9 and opts["num_predict"] == 768
    # And the reasoning branch must not have been taken for llama3.
    assert motor._reasoning_model_cache["llama3"] is False


# ── B-Isolation — the penalties never leak to other paths (CRITICAL) ─────────

def test_chat_penalties_do_not_apply_to_other_sources():
    # If this fails, do NOT merge: the fix is bleeding past chat-reactive scope.
    for src in ("direct", "ptt", "accumulated", "kira-agenda", "kira-agenda-stop"):
        motor = _chat_motor()
        motor._ollama_chat = MagicMock(return_value=_resp("Una linea neutra cualquiera para el test."))
        motor._generar_dialogo("hola", source=src, commit_history=False)

        assert motor._ollama_chat.called, src
        opts = motor._ollama_chat.call_args.kwargs["options"]
        assert "repeat_penalty" not in opts, src
        assert "presence_penalty" not in opts, src
        assert "frequency_penalty" not in opts, src


def test_default_enqueue_is_chat_documented():
    # Freeze today's behavior: enqueue without an explicit source falls into
    # "chat". This is the footgun the event_taxonomy track will disambiguate —
    # the test exists so a future change to the default is a conscious decision.
    motor = _chat_motor()
    motor.enqueue("hola sin source explicito")
    queued = list(motor._priority_queue)
    assert queued and queued[-1][3] == "chat"
    assert inspect.signature(motor.enqueue).parameters["source"].default == "chat"


# ── B-Repro — FIX 2 (RED until the reactive guard is wired) ──────────────────

def test_verbatim_dup_suppressed():
    motor = _chat_motor()
    line = "La sorpresa siempre es un buen regalo para todo el chat."
    motor._ollama_chat = MagicMock(return_value=_resp(line))

    out1 = motor._generar_dialogo("tema uno", source="chat", commit_history=True)
    assert out1 == line  # turn 1 passes the output guard intact
    out2 = motor._generar_dialogo("tema dos", source="chat", commit_history=True)
    assert out2 != line  # the stale duplicate is never re-emitted


def test_verbatim_dup_uses_neutral_fallback():
    # D3 = fallback-first. Isolated from the core invariant above so that a future
    # switch to "regenerate" only rewrites this test.
    motor = _chat_motor()
    line = "La sorpresa siempre es un buen regalo para todo el chat."
    motor._ollama_chat = MagicMock(return_value=_resp(line))

    motor._generar_dialogo("tema uno", source="chat", commit_history=True)
    out2 = motor._generar_dialogo("tema dos", source="chat", commit_history=True)
    assert out2 in set(GUARDRAIL_FALLBACK_LINES)


def test_synonym_swap_template_suppressed():
    motor = _chat_motor()
    l1 = "Cada partida es un abismo sin fin para el equipo."
    l2 = "Cada derrota es un abismo sin salida para el equipo."
    responses = iter([_resp(l1), _resp(l2)])
    motor._ollama_chat = MagicMock(side_effect=lambda **kw: next(responses))

    out1 = motor._generar_dialogo("tema uno", source="chat", commit_history=True)
    assert out1 == l1
    out2 = motor._generar_dialogo("tema dos", source="chat", commit_history=True)
    assert out2 != l2  # the synonym-swap template is suppressed


def test_genuinely_new_line_is_not_over_suppressed():
    motor = _chat_motor()
    l1 = "Cada partida es un abismo sin fin para el equipo."
    l2 = "Bueno, parece que el rival viene fuerte esta ronda, ojo."
    responses = iter([_resp(l1), _resp(l2)])
    motor._ollama_chat = MagicMock(side_effect=lambda **kw: next(responses))

    motor._generar_dialogo("tema uno", source="chat", commit_history=True)
    out2 = motor._generar_dialogo("tema dos", source="chat", commit_history=True)
    assert out2 == l2  # a genuinely different line must pass through
