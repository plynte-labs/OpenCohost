"""P3 producer — opt-in dialogue_callback on MotorVocalIA (S1).

Kira's OWN generated reply text is allowed to flow through this sink (R8
only forbids raw VIEWER chat). Two emission sites must route through it:
  - the main-turn reply path (_generar_dialogo, source="chat" etc.)
  - the agenda-prefetch playback path (play_prefetched_agenda)
A raising callback must never break the turn. Default None = zero behavior
change for the CTk app.
"""
from __future__ import annotations

import queue
import threading
import time
from unittest.mock import MagicMock

from opencohost.core import llm_engine as le
from opencohost.core.llm_engine import MotorVocalIA
from opencohost.i18n import active as i18n_active


def _resp(text):
    return {"message": {"content": text}}


def _chat_motor(dialogue_callback=None):
    log_q = queue.Queue()
    motor = MotorVocalIA(log_q, lambda event: None, dialogue_callback=dialogue_callback)
    motor.ollama = MagicMock()
    motor.pygame = MagicMock()
    motor.is_ready = True
    motor.current_model = "llama3"
    motor._reasoning_model_cache["llama3"] = False
    return motor


def test_main_turn_invokes_dialogue_callback():
    spy = MagicMock()
    motor = _chat_motor(dialogue_callback=spy)
    motor._hablar = MagicMock()
    motor._ollama_chat = MagicMock(return_value=_resp("Che, que buena jugada."))

    motor._ejecutar_inferencia("hola", source="chat")

    # Emission happens at the speak site, exactly once, carrying the TRUE
    # source. The uniform "kira" attribution is derived at the sink that
    # publishes it (ChatReplySink.record), not destroyed here.
    spy.assert_called_once_with("Che, que buena jugada.", "chat")


def test_spoken_guardrail_fallback_updates_last_reply(monkeypatch):
    """FIX-B2: a guardrail-blocked fallback that Kira still SPEAKS must update
    the sink to the fallback text — not leave it on the previous turn."""
    spy = MagicMock()
    motor = _chat_motor(dialogue_callback=spy)
    motor._hablar = MagicMock()
    motor._ollama_chat = MagicMock(return_value=_resp("texto bloqueado"))
    # Force the guardrail to block so _generar_dialogo returns EARLY with a
    # canned fallback line (never reaching the old commit_history emit site).
    monkeypatch.setattr(le, "output_guard", lambda text, source="chat": (False, "blocked"))

    motor._ejecutar_inferencia("hola", source="chat")

    fallback = i18n_active.LEGACY_GUARDRAIL_FALLBACK_LINES[0]
    # Kira audibly spoke the fallback...
    motor._hablar.assert_called_once_with(fallback, source="chat")
    # ...and last-reply now reflects THAT fallback, emitted exactly once.
    spy.assert_called_once_with(fallback, "chat")


def test_guardrail_blocked_turn_never_commits_fallback_to_history(monkeypatch):
    """Session 21:07 (F2): the canned fallback is spoken and shown but is NOT
    conversation. Stored as an assistant turn it was replayed into later prompts
    (and the already-said block), so the model parroted it. The blocked exchange
    stays out of history entirely (pairs only, never an orphan user turn)."""
    motor = _chat_motor()
    motor._hablar = MagicMock()
    motor._ollama_chat = MagicMock(return_value=_resp("respuesta bloqueada por el guardrail"))
    monkeypatch.setattr(le, "output_guard", lambda text, source="chat": (False, "blocked"))

    assert len(motor.historial) == 0
    motor._ejecutar_inferencia("pregunta honesta del usuario", source="chat")

    assert len(motor.historial) == 0
    motor._hablar.assert_called_once()


def test_spoken_normal_turn_emits_exactly_once():
    """A normal spoken turn must emit through the sink exactly once (no
    double-emit now that the emission point moved to the speak site)."""
    spy = MagicMock()
    motor = _chat_motor(dialogue_callback=spy)
    motor._hablar = MagicMock()
    motor._ollama_chat = MagicMock(return_value=_resp("Todo firme por acá."))

    motor._ejecutar_inferencia("hola", source="chat")

    spy.assert_called_once_with("Todo firme por acá.", "chat")


def test_agenda_prefetch_invokes_dialogue_callback():
    spy = MagicMock()
    motor = _chat_motor(dialogue_callback=spy)
    motor._hablar = MagicMock()
    motor._prefetched_agenda = {
        "payload": "prefetched prompt",
        "dialogo": "respuesta prefabricada",
        "priority": 2,
        "source": "kira-agenda",
    }
    motor._prefetch_done.set()

    assert motor.play_prefetched_agenda() is True
    # speaker() runs on a background thread — give it a moment to finish.
    deadline = time.time() + 2
    while not spy.called and time.time() < deadline:
        time.sleep(0.01)

    spy.assert_called_once_with("respuesta prefabricada", "kira-agenda")


def test_raising_callback_does_not_break_main_turn():
    spy = MagicMock(side_effect=RuntimeError("boom"))
    motor = _chat_motor(dialogue_callback=spy)
    motor._hablar = MagicMock()
    motor._ollama_chat = MagicMock(return_value=_resp("Sigue firme el equipo."))

    motor._ejecutar_inferencia("hola", source="chat")

    assert spy.called
    # A raising callback must not prevent Kira from actually speaking.
    motor._hablar.assert_called_once_with("Sigue firme el equipo.", source="chat")


def test_raising_callback_does_not_break_agenda_playback():
    spy = MagicMock(side_effect=RuntimeError("boom"))
    motor = _chat_motor(dialogue_callback=spy)
    motor._hablar = MagicMock()
    motor._prefetched_agenda = {
        "payload": "p",
        "dialogo": "d",
        "priority": 2,
        "source": "kira-agenda",
    }
    motor._prefetch_done.set()

    assert motor.play_prefetched_agenda() is True
    deadline = time.time() + 2
    while not spy.called and time.time() < deadline:
        time.sleep(0.01)

    assert spy.called
    # _hablar must still have been reached despite the callback raising.
    deadline = time.time() + 2
    while not motor._hablar.called and time.time() < deadline:
        time.sleep(0.01)
    motor._hablar.assert_called_once_with("d", source="kira-agenda")


def test_default_none_never_calls_anything():
    motor = _chat_motor(dialogue_callback=None)
    motor._ollama_chat = MagicMock(return_value=_resp("Todo tranqui por acá."))

    out = motor._generar_dialogo("hola", source="chat", commit_history=True)

    assert out == "Todo tranqui por acá."
    assert motor.dialogue_callback is None


def test_commit_history_refuses_every_locale_fallback_line():
    """F2: whatever path hands a canned fallback line to _commit_history, it is
    not stored (active bundle plus the legacy es tuple)."""
    from opencohost.i18n import active as i18n_active

    motor = _chat_motor()
    lines = set(i18n_active.LEGACY_GUARDRAIL_FALLBACK_LINES) | set(
        i18n_active.guardrail_fallback_lines()
    )
    for line in lines:
        motor._commit_history("hola", line, source="direct")
    assert len(motor.historial) == 0
    motor._commit_history("hola", "Una respuesta real.", source="direct")
    assert [m["role"] for m in motor.historial] == ["user", "assistant"]


def test_fallback_lines_are_neutral_spanish_with_accents():
    from opencohost.i18n import active as i18n_active

    lines = i18n_active.LEGACY_GUARDRAIL_FALLBACK_LINES
    assert "Soy una IA, no una persona; dicho eso, aquí estoy contigo." in lines
    joined = " ".join(lines).lower()
    for bad in ("preguntame", "repetilo", "podés", "decime", "mirá", "contame"):
        assert bad not in joined
