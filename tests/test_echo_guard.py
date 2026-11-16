"""P4 (kira-personal-tone-20261007): echo guard -- a reply identical or
near-identical to the previous assistant turn gets ONE retry with a localized
nudge. Streamed turns cannot be retried (audio already out) and are only
logged."""

import logging
import queue
from unittest.mock import MagicMock

import pytest

from opencohost.core import llm_engine
from opencohost.core.context import echo_guard
from opencohost.i18n import active as i18n_active

LONG = (
    "Pues mira, eso que dices me parece muy curioso y tengo que pensarlo "
    "un poco mas antes de darte una opinion de verdad."
)


# ------------------------------------------------------------ pure helpers


def test_identical_long_reply_is_echo():
    echo, ratio = echo_guard.is_echo(LONG, LONG)
    assert echo is True
    assert ratio == pytest.approx(1.0)


def test_case_whitespace_punctuation_are_normalized():
    noisy = "  " + LONG.upper().replace(",", " ,,") + "!!  "
    echo, ratio = echo_guard.is_echo(noisy, LONG)
    assert echo is True
    assert ratio >= 0.9


def test_near_identical_is_echo():
    near = LONG.replace("verdad", "verdad ahora")
    echo, ratio = echo_guard.is_echo(near, LONG)
    assert echo is True
    assert 0.9 <= ratio < 1.0


def test_distinct_reply_is_not_echo():
    other = "Ayer vi un documental sobre pulpos y todavia no puedo dejar de pensar en eso."
    echo, ratio = echo_guard.is_echo(other, LONG)
    assert echo is False
    assert ratio < 0.9


def test_short_replies_are_exempt():
    echo, _ = echo_guard.is_echo("Sí, claro.", "Sí, claro.")
    assert echo is False


def test_empty_previous_is_not_echo():
    assert echo_guard.is_echo(LONG, "")[0] is False
    assert echo_guard.is_echo(LONG, None)[0] is False


def test_last_assistant_text_picks_latest_assistant():
    history = [
        {"role": "assistant", "content": "primera"},
        {"role": "user", "content": "hola"},
        {"role": "assistant", "content": "segunda"},
        {"role": "user", "content": "otra"},
    ]
    assert echo_guard.last_assistant_text(history) == "segunda"
    assert echo_guard.last_assistant_text([]) == ""
    assert echo_guard.last_assistant_text([{"role": "user", "content": "x"}]) == ""


# ------------------------------------------------------------- nudge slot


def test_no_echo_nudge_exists_in_both_locales_without_fallback_to_generic():
    import yaml
    from pathlib import Path

    root = Path(llm_engine.__file__).resolve().parents[1] / "locales"
    for code in ("es", "en"):
        data = yaml.safe_load((root / code / "manifest.yaml").read_text(encoding="utf-8"))
        nudges = data["llm"]["guardrail_retry_nudges"]
        assert nudges["no_echo"].strip()
        assert nudges["no_echo"] != nudges["generic"]


def test_engine_nudge_for_echo_reason_uses_no_echo_key():
    text = llm_engine._guardrail_retry_nudge_for_reason(
        "Non-negotiable violation [no_echo]: x"
    )
    assert text == i18n_active.guardrail_retry_nudge("no_echo")
    assert text != i18n_active.guardrail_retry_nudge("generic")


# ---------------------------------------------------- buffered integration


@pytest.fixture
def motor(monkeypatch):
    m = llm_engine.MotorVocalIA(queue.Queue(), lambda event: None)
    m.current_model = "llama3"
    m.use_system_role = True
    m.ollama = MagicMock()
    monkeypatch.setattr(llm_engine, "LLM_STREAMING_ENABLED", False)
    m.historial.append({"role": "user", "content": "hola", "source": "direct"})
    m.historial.append({"role": "assistant", "content": LONG, "source": "direct"})
    return m


def _chat(*contents):
    seq = list(contents)

    def _fn(**kwargs):
        return {"message": {"content": seq.pop(0)}}

    return MagicMock(side_effect=_fn)


FRESH = "Ayer vi un documental sobre pulpos y todavia no puedo dejar de pensar en eso."


def test_buffered_echo_retries_once_and_accepts_distinct_retry(motor, caplog):
    motor._ollama_chat_with_watchdog = _chat(LONG, FRESH)
    with caplog.at_level(logging.INFO, logger="OpenCohost"):
        out = motor._generar_dialogo("cuentame algo", source="direct", commit_history=True)
    assert out == FRESH
    assert motor._ollama_chat_with_watchdog.call_count == 2
    retry_msgs = motor._ollama_chat_with_watchdog.call_args_list[1].kwargs["messages"]
    assert i18n_active.guardrail_retry_nudge("no_echo") in retry_msgs[-1]["content"]
    logs = [r.getMessage() for r in caplog.records if "[ECHO_GUARD]" in r.getMessage()]
    assert len(logs) == 1
    assert "blocked source=direct" in logs[0] and "ratio=" in logs[0]
    assert LONG[:20] not in logs[0] and FRESH[:20] not in logs[0]


def test_buffered_echo_retry_that_echoes_again_is_accepted_no_loop(motor):
    motor._ollama_chat_with_watchdog = _chat(LONG, LONG)
    out = motor._generar_dialogo("cuentame algo", source="direct", commit_history=True)
    assert out == LONG
    assert motor._ollama_chat_with_watchdog.call_count == 2


def test_buffered_echo_empty_retry_keeps_original(motor):
    motor._ollama_chat_with_watchdog = _chat(LONG, "")
    out = motor._generar_dialogo("cuentame algo", source="direct", commit_history=True)
    assert out == LONG


def test_buffered_distinct_reply_is_not_retried(motor):
    motor._ollama_chat_with_watchdog = _chat(FRESH)
    out = motor._generar_dialogo("cuentame algo", source="direct", commit_history=True)
    assert out == FRESH
    assert motor._ollama_chat_with_watchdog.call_count == 1


def test_no_previous_assistant_turn_is_not_retried():
    m = llm_engine.MotorVocalIA(queue.Queue(), lambda event: None)
    m.current_model = "llama3"
    m.use_system_role = True
    m.ollama = MagicMock()
    m._ollama_chat_with_watchdog = _chat(LONG)
    assert m._generar_dialogo("hola", source="direct", commit_history=True) == LONG
    assert m._ollama_chat_with_watchdog.call_count == 1


def test_streamed_turn_is_logged_not_retried(motor, caplog):
    motor._retry_after_guard_block = MagicMock()
    setup = MagicMock()
    setup.history_snapshot = [{"role": "assistant", "content": LONG}]
    with caplog.at_level(logging.WARNING, logger="OpenCohost"):
        out = motor._apply_echo_guard(
            setup, LONG, source="direct", streamed=True, messages=[], opciones_llm={},
            request_model="llama3", provider_cfg={}, is_local=True,
        )
    assert out == LONG
    motor._retry_after_guard_block.assert_not_called()
    logs = [r.getMessage() for r in caplog.records if "[ECHO_GUARD]" in r.getMessage()]
    assert len(logs) == 1 and "observed" in logs[0] and LONG[:20] not in logs[0]


def test_agenda_sources_are_exempt(motor):
    motor._retry_after_guard_block = MagicMock()
    setup = MagicMock()
    setup.history_snapshot = [{"role": "assistant", "content": LONG}]
    out = motor._apply_echo_guard(
        setup, LONG, source="kira-agenda", streamed=False, messages=[], opciones_llm={},
        request_model="llama3", provider_cfg={}, is_local=True,
    )
    assert out == LONG
    motor._retry_after_guard_block.assert_not_called()
