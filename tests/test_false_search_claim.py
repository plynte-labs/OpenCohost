"""False web-search claim guard (web-trigger-memory-relevance-20261014 T2).

Live log 2026-10-14 21:45:42: no web search ran (``[WEB] decision=skip``) and
the prompt said "Do not say that you searched", yet the reply was "Busqué en
internet '...' ... Los resultados no mostraron ...". A reply that claims a
search or cites search results is a violation on a turn where no search ran;
it takes the existing guard path (one retry with a nudge, then the neutral
line). When a search did run, the same phrases are allowed.
"""

import logging
import queue
from unittest.mock import MagicMock

import pytest

from opencohost.core import llm_engine
from opencohost.core.context import search_claim
from opencohost.core.engine.llm_engine_web import WebLook
from opencohost.core.web import prompt as web_prompt
from tests.test_local_prompt_leak_defence import (
    PERSONA,
    FakeChunk,
    FakeRouter,
    _arm_stream,
)

CLAIM = "Busqué en internet 'agentes de IA recomendados'. Los resultados no mostraron nada claro."


@pytest.fixture
def motor(monkeypatch):
    m = llm_engine.MotorVocalIA(queue.Queue(), lambda event: None)
    m.current_model = "llama3"
    m.use_system_role = True
    m.system_prompt = PERSONA
    m.ollama = MagicMock()
    m._speech_router_enabled = True
    monkeypatch.setattr(llm_engine, "LLM_STREAMING_ENABLED", False)
    router = FakeRouter()
    m._ensure_router = lambda: router
    m._speech_router = router
    m._fake_router = router
    return m


# --------------------------------------------------------------- detector


@pytest.mark.parametrize(
    "text",
    [
        "Busqué en internet 'agentes de IA recomendados'.",
        "Busque en la web y no hay mucho.",
        "Según los resultados, el modelo X es el mejor.",
        "Según mis resultados no hay nada.",
        "Los resultados que encontré apuntan a tres opciones.",
        "Los resultados no mostraron nada claro.",
        "Los resultados muestran que sube.",
        "Encontré en internet que sale en octubre.",
        "He buscado en google y no sale.",
        "I searched the web and found nothing.",
        "I searched online for it.",
        "According to the search results, it launches in May.",
        "The search results show three options.",
        "I looked it up online and it is real.",
    ],
)
def test_claim_detector_flags_search_claims(text):
    assert search_claim.claims_web_search(text)


@pytest.mark.parametrize(
    "text",
    [
        "No busqué en internet esta vez, te respondo de memoria.",
        "No he buscado en internet, dime busca en internet y lo hago.",
        "¿Quieres que busque en internet?",
        "Puedo buscar en la web si me lo pides.",
        "Los resultados del partido fueron una vergüenza.",
        "Busca la llave en el cofre.",
        "I did not search the web this time.",
        "I can search online if you ask me to.",
        "",
    ],
)
def test_claim_detector_allows_honest_and_unrelated_text(text):
    assert not search_claim.claims_web_search(text)


def test_nudge_exists_in_both_locales():
    from opencohost.i18n import active as i18n_active

    nudge = i18n_active.guardrail_retry_nudge(search_claim.SEARCH_CLAIM_RULE_ID)
    generic = i18n_active.guardrail_retry_nudge("generic")
    assert nudge and nudge != generic
    import yaml
    from pathlib import Path

    root = Path(llm_engine.__file__).resolve().parents[1] / "locales"
    for lang in ("es", "en"):
        data = yaml.safe_load((root / lang / "manifest.yaml").read_text(encoding="utf-8"))
        assert search_claim.SEARCH_CLAIM_RULE_ID in data["llm"]["guardrail_retry_nudges"]


# ---------------------------------------------------------------- buffered


def _buffered(motor, content):
    motor._ollama_chat_with_watchdog = MagicMock(return_value={"message": {"content": content}})
    motor._guardrail_fallback_line = MagicMock(return_value="linea neutral")


def test_buffered_claim_without_search_retries_once_with_the_nudge(motor, caplog):
    _buffered(motor, CLAIM)
    motor._retry_after_guard_block = MagicMock(return_value="No busqué nada, te lo digo de memoria.")

    with caplog.at_level(logging.INFO):
        dialogo = motor._generar_dialogo("hola", source="direct", commit_history=True)

    assert dialogo == "No busqué nada, te lo digo de memoria."
    motor._retry_after_guard_block.assert_called_once()
    assert search_claim.SEARCH_CLAIM_RULE_ID in motor._retry_after_guard_block.call_args.kwargs["guard_reason"]
    assert any("[SEARCH_CLAIM]" in r.getMessage() for r in caplog.records)
    for record in caplog.records:
        assert "agentes de IA" not in record.getMessage()


def test_buffered_claim_repeated_on_retry_takes_the_neutral_fallback(motor):
    _buffered(motor, CLAIM)
    motor._retry_after_guard_block = MagicMock(return_value=CLAIM)

    assert motor._generar_dialogo("hola", source="direct", commit_history=True) == "linea neutral"


def test_buffered_claim_is_allowed_when_a_search_ran(motor):
    _buffered(motor, CLAIM)
    motor._run_web_step = lambda *a, **k: WebLook(block="WEB RESULTS: ejemplo")
    motor._retry_after_guard_block = MagicMock(return_value="no deberia llamarse")

    assert motor._generar_dialogo("hola", source="direct", commit_history=True) == CLAIM
    motor._retry_after_guard_block.assert_not_called()


def test_buffered_claim_is_allowed_after_a_failed_search(motor):
    """A search that ran and failed (note, not the not-searched note) may be reported."""
    _buffered(motor, CLAIM)
    motor._run_web_step = lambda *a, **k: WebLook(note=web_prompt.WEB_EMPTY_NOTE)
    motor._retry_after_guard_block = MagicMock(return_value="no deberia llamarse")

    assert motor._generar_dialogo("hola", source="direct", commit_history=True) == CLAIM


def test_buffered_claim_is_blocked_when_the_not_searched_note_was_used(motor):
    _buffered(motor, CLAIM)
    motor._run_web_step = lambda *a, **k: WebLook(note=web_prompt.WEB_NOT_SEARCHED_NOTE)
    motor._retry_after_guard_block = MagicMock(return_value="De memoria.")

    assert motor._generar_dialogo("hola", source="direct", commit_history=True) == "De memoria."


def test_buffered_honest_reply_is_untouched(motor):
    reply = "No busqué en internet esta vez. ¿Quieres que busque en internet?"
    _buffered(motor, reply)
    motor._retry_after_guard_block = MagicMock(return_value="no deberia llamarse")

    assert motor._generar_dialogo("hola", source="direct", commit_history=True) == reply


# ---------------------------------------------------------------- streamed


def test_streamed_claim_before_first_submit_reverts_to_the_buffered_guard(motor, monkeypatch):
    monkeypatch.setattr(llm_engine, "LLM_STREAMING_ENABLED", True)
    _arm_stream(motor, [FakeChunk(CLAIM + " "), FakeChunk("Otra frase."), FakeChunk("", done=True)])
    motor._retry_after_guard_block = MagicMock(return_value="")
    motor._guardrail_fallback_line = MagicMock(return_value="linea neutral")

    dialogo = motor._generar_dialogo("hola", source="direct", commit_history=True)

    assert motor._fake_router.jobs == []
    assert dialogo == "linea neutral"


def test_streamed_claim_after_a_submit_truncates_the_spoken_prefix(motor, monkeypatch, caplog):
    monkeypatch.setattr(llm_engine, "LLM_STREAMING_ENABLED", True)
    _arm_stream(
        motor,
        [
            FakeChunk("Primera frase limpia. "),
            FakeChunk("Busqué en internet y no hay nada. "),
            FakeChunk("Tercera frase."),
            FakeChunk("", done=True),
        ],
    )

    with caplog.at_level(logging.INFO):
        dialogo = motor._generar_dialogo("hola", source="direct", commit_history=True)

    spoken = [c for _, chunks in motor._fake_router.appends for c in chunks]
    assert spoken == ["Primera frase limpia."]
    assert dialogo == "Primera frase limpia."
    assert any("[SEARCH_CLAIM]" in r.getMessage() for r in caplog.records)


def test_streamed_claim_is_spoken_when_a_search_ran(motor, monkeypatch):
    monkeypatch.setattr(llm_engine, "LLM_STREAMING_ENABLED", True)
    motor._run_web_step = lambda *a, **k: WebLook(block="WEB RESULTS: ejemplo")
    _arm_stream(
        motor,
        [FakeChunk("Primera frase limpia. "), FakeChunk("Busqué en internet y hay algo. "), FakeChunk("", done=True)],
    )

    motor._generar_dialogo("hola", source="direct", commit_history=True)

    spoken = [c for _, chunks in motor._fake_router.appends for c in chunks]
    assert spoken == ["Primera frase limpia.", "Busqué en internet y hay algo."]


# ---------------------------------------------------------------- cannot-search guard (O3)

CANNOT = "No puedo buscar eso ahora mismo, no tengo acceso a internet."


@pytest.mark.parametrize(
    "text",
    [
        "No puedo buscar eso ahora mismo.",
        "Lo siento, no puedo buscar en internet.",
        "No tengo acceso a internet en este momento.",
        "No puedo acceder a internet ahora.",
        "I cannot search the web right now.",
        "I can't search online.",
        "I don't have access to the internet.",
    ],
)
def test_cannot_search_detector_flags_the_false_inability(text):
    assert search_claim.claims_cannot_search(text)


@pytest.mark.parametrize(
    "text",
    [
        "Se me agotó el tiempo buscando, inténtalo otra vez.",
        "No pude buscarlo esta vez, pídemelo de nuevo.",
        "Puedo buscar en internet, díselo otra vez.",
        "No puedo buscar la llave si no me dices dónde.",
        "The search timed out, ask me again.",
        "",
    ],
)
def test_cannot_search_detector_allows_honest_timeout_talk(text):
    assert not search_claim.claims_cannot_search(text)


def test_cannot_search_nudge_exists_in_both_locales():
    import yaml
    from pathlib import Path

    from opencohost.i18n import active as i18n_active

    assert i18n_active.guardrail_retry_nudge(search_claim.CANNOT_SEARCH_RULE_ID) != i18n_active.guardrail_retry_nudge("generic")
    root = Path(llm_engine.__file__).resolve().parents[1] / "locales"
    for lang in ("es", "en"):
        data = yaml.safe_load((root / lang / "manifest.yaml").read_text(encoding="utf-8"))
        assert search_claim.CANNOT_SEARCH_RULE_ID in data["llm"]["guardrail_retry_nudges"]


def test_cannot_search_after_a_timeout_is_retried_once_with_the_nudge(motor, caplog):
    _buffered(motor, CANNOT)
    motor._run_web_step = lambda *a, **k: WebLook(note=web_prompt.WEB_TIMEOUT_NOTE)
    motor._retry_after_guard_block = MagicMock(return_value="Se me agotó el tiempo, pídemelo otra vez.")

    with caplog.at_level(logging.INFO):
        dialogo = motor._generar_dialogo("hola", source="direct", commit_history=True)

    assert dialogo == "Se me agotó el tiempo, pídemelo otra vez."
    motor._retry_after_guard_block.assert_called_once()
    assert search_claim.CANNOT_SEARCH_RULE_ID in motor._retry_after_guard_block.call_args.kwargs["guard_reason"]
    assert any("[CANNOT_SEARCH_CLAIM]" in r.getMessage() for r in caplog.records)
    assert all("ahora mismo" not in r.getMessage() for r in caplog.records)


def test_cannot_search_repeated_on_retry_takes_the_neutral_fallback(motor):
    _buffered(motor, CANNOT)
    motor._run_web_step = lambda *a, **k: WebLook(note=web_prompt.WEB_TIMEOUT_NOTE)
    motor._retry_after_guard_block = MagicMock(return_value=CANNOT)

    assert motor._generar_dialogo("hola", source="direct", commit_history=True) == "linea neutral"


def test_honest_timeout_reply_is_not_flagged(motor):
    reply = "Se me agotó el tiempo buscando, inténtalo otra vez."
    _buffered(motor, reply)
    motor._run_web_step = lambda *a, **k: WebLook(note=web_prompt.WEB_TIMEOUT_NOTE)
    motor._retry_after_guard_block = MagicMock(return_value="no deberia llamarse")

    assert motor._generar_dialogo("hola", source="direct", commit_history=True) == reply
    motor._retry_after_guard_block.assert_not_called()


def test_cannot_search_is_not_inspected_when_the_web_switch_is_off(motor):
    _buffered(motor, CANNOT)  # default _run_web_step: switch off -> NO_WEB
    motor._retry_after_guard_block = MagicMock(return_value="no deberia llamarse")

    assert motor._generar_dialogo("hola", source="direct", commit_history=True) == CANNOT
    motor._retry_after_guard_block.assert_not_called()


def test_streamed_cannot_search_after_a_submit_truncates_the_spoken_prefix(motor, monkeypatch):
    monkeypatch.setattr(llm_engine, "LLM_STREAMING_ENABLED", True)
    motor._run_web_step = lambda *a, **k: WebLook(note=web_prompt.WEB_FAILED_NOTE)
    _arm_stream(
        motor,
        [FakeChunk("Primera frase limpia. "), FakeChunk("No puedo buscar eso ahora mismo. "), FakeChunk("", done=True)],
    )

    dialogo = motor._generar_dialogo("hola", source="direct", commit_history=True)

    spoken = [c for _, chunks in motor._fake_router.appends for c in chunks]
    assert spoken == ["Primera frase limpia."]
    assert dialogo == "Primera frase limpia."
