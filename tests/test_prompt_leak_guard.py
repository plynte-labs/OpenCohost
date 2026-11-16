"""vision-and-prompt-leaks-20261018 T3: internal-marker and third-person owner leaks.

Live 2026-10-17 23:21:11 "el dueño me pidió directamente..." and 23:22:21 "me pediste
explícitamente la web search this turn". The existing prompt-echo guard misses both
(P2: a short marker inside a sentence never reaches the 40-char / 60% thresholds).
The leak check rides the same guard path: stream revert/truncate, buffered retry
with a nudge, then the neutral line.
"""

import logging
import queue
from unittest.mock import MagicMock

import pytest

from opencohost.core import llm_engine
from opencohost.core.context import prompt_echo
from opencohost.core.context.prompt_assembler import SCREEN_LOOK_MARKER, VISION_OFF_MARKER
from opencohost.core.web.prompt import WEB_RESULTS_MARKER

PERSONA = "Eres Kira, co-host de streams. Respondes en 2-4 oraciones con humor seco."
LEAK_REPLY = "El dueño me pidió directamente que te lo cuente."
MARKER_REPLY = "Me pediste explícitamente la web search this turn."


# ------------------------------------------------------------------ pure module

@pytest.mark.parametrize("marker", [WEB_RESULTS_MARKER, SCREEN_LOOK_MARKER, VISION_OFF_MARKER])
@pytest.mark.parametrize("source", ["direct", "ptt", "chat"])
def test_internal_markers_are_leaks_on_every_source(marker, source):
    assert prompt_echo.leak_kind(f"Claro, {marker.lower()} y ya.", source) == "marker"


@pytest.mark.parametrize("source", ["direct", "ptt", "chat"])
@pytest.mark.parametrize("text", [
    "Segun el reply length this turn: long, te respondo largo.",
    "Reply length this turn: very long.",
    "Me dice mi turn context que hable mas.",
    "Dentro del <turn_context> hay una nota.",
    "El bloque turn_context dice que busque.",
])
def test_v2_per_turn_vocabulary_is_a_marker_leak(text, source):
    assert prompt_echo.leak_kind(text, source) == "marker"


@pytest.mark.parametrize("text", [
    "El contexto de este turno no me deja hablar, jaja.",
    "Hoy el contexto importa mucho en este tema.",
    "Es tu turno, cuentame el contexto del juego.",
    "Podemos cambiar el largo de la respuesta si quieres.",
    "In this context, your turn is next and the reply is short.",
    "Return context to the player before the turn ends.",
    "El largo de esta respuesta es corto este turno.",
])
def test_v2_markers_do_not_fire_on_normal_cohost_speech(text):
    assert prompt_echo.leak_kind(text, "direct") is None


@pytest.mark.parametrize("source", ["direct", "ptt", "chat"])
@pytest.mark.parametrize("text", [
    "Your turn. Context matters here.",
    "Every turn, context changes.",
    "Okay, turn - context is everything in this game.",
    "It is your turn! Context is key.",
])
def test_r2_1_turn_and_context_across_punctuation_is_not_a_leak(text, source):
    assert prompt_echo.leak_kind(text, source) is None


@pytest.mark.parametrize("source", ["direct", "ptt", "chat"])
@pytest.mark.parametrize("text", [
    "Dice <turn_context> y nada mas.",
    "Cierra con </turn_context> al final.",
    "El bloque TURN_CONTEXT dice que busque.",
    "TURN CONTEXT: A user message may begin with a block.",
    "Segun mi turn context deberia hablar mas.",
    "Segun mi TURN   CONTEXT deberia hablar mas.",
])
def test_r2_1_real_turn_context_vocabulary_is_still_a_marker_leak(text, source):
    assert prompt_echo.leak_kind(text, source) == "marker"


def test_r2_1_the_rule_heading_as_the_prompt_spells_it_is_a_leak():
    from opencohost.core.context.prompt_assembler import turn_context_rule_v2

    rule = turn_context_rule_v2()
    assert rule.startswith("TURN CONTEXT:")
    for source in ("direct", "ptt", "chat"):
        assert prompt_echo.leak_kind(rule, source) == "marker"
        assert prompt_echo.leak_kind(rule.split(":", 1)[0] + " dice algo", source) == "marker"


def test_live_marker_sentence_is_a_leak_p2():
    assert prompt_echo.leak_kind(MARKER_REPLY, "direct") == "marker"


@pytest.mark.parametrize("text", [
    "El dueño me pidió directamente que lo haga.",
    "the owner asked me to do it",
    "Según el dueño, no hay problema.",
    "Le dije al dueño que sí.",
])
@pytest.mark.parametrize("source", ["direct", "ptt"])
def test_third_person_owner_is_a_leak_on_owner_sources(text, source):
    assert prompt_echo.leak_kind(text, source) == "owner_third_person"


@pytest.mark.parametrize("text", [
    "El dueño de la tienda cerró temprano.",
    "Habla con el dueño del restaurante.",
    "El dueño de esta cuenta eres tú.",
    "the owner of the shop was rude",
])
def test_owner_of_something_passes(text):
    assert prompt_echo.leak_kind(text, "direct") is None


@pytest.mark.parametrize("source", ["chat", "kira-agenda", "agenda", "system", ""])
def test_third_person_owner_check_is_owner_sources_only(source):
    assert prompt_echo.leak_kind(LEAK_REPLY, source) is None


@pytest.mark.parametrize("text", [
    "Ese salto fue horrible, pero el jefe se rió de ti.",
    "Busqué algo en el mapa del juego.",
    "",
])
def test_normal_replies_are_clean(text):
    assert prompt_echo.leak_kind(text, "direct") is None


def test_leak_check_never_raises_on_odd_input():
    assert prompt_echo.leak_kind(None, "direct") is None
    assert prompt_echo.leak_kind(123, "direct") is None


def test_reason_names_the_rule_for_the_nudge_path():
    assert f"[{prompt_echo.PROMPT_LEAK_RULE_ID}]" in prompt_echo.PROMPT_LEAK_REASON
    assert prompt_echo.PROMPT_LEAK_REASON.startswith("Non-negotiable violation [")


def test_leak_log_carries_codes_and_counts_only(caplog):
    with caplog.at_level(logging.WARNING, logger="OpenCohost"):
        prompt_echo.log_prompt_leak(stage="buffered", source="direct", kind="marker", chars=12)
    line = caplog.records[-1].getMessage()
    assert "[PROMPT_LEAK]" in line and "kind=marker" in line and "chars=12" in line


# ------------------------------------------------------------------ P2 claim

def test_p2_confirmed_ngram_detector_misses_the_embedded_marker():
    detector = prompt_echo.PromptEchoDetector(f"{PERSONA}\n\n{WEB_RESULTS_MARKER}")
    assert detector.is_echo(MARKER_REPLY) is False


@pytest.mark.usefixtures("legacy_prompt_layout")
def test_p1_confirmed_echo_reference_omits_the_turn_rules_but_goldens_pin_it():
    # Legacy layout only (B-W4 pin): its reference omits the web/vision/turn-context
    # rules, because extending it would change prompt_echo_reference in the pinned
    # golden fixtures. Under v2 (J2) the reference does carry them; see
    # tests/test_prompt_layout_v2.py::test_j2_v2_echo_reference_carries_the_stable_rules_and_legacy_does_not.
    from opencohost.core.context.prompt_assembler import PromptContextAssembler
    from opencohost.core.web.prompt import WEB_RESULTS_RULE_V2

    setup = PromptContextAssembler().assemble(
        "hola", "direct", system_prompt=PERSONA, use_system_role=True, is_local=True,
        provider_cfg={}, request_model="llama3", history_snapshot=[],
        personalization_enabled=False, memorias_enabled=False,
    )
    assert WEB_RESULTS_RULE_V2 not in setup.prompt_echo_reference


# ----------------------------------------------------------------- engine wiring

class FakeMessage:
    def __init__(self, content=""):
        self.content = content
        self.thinking = None


class FakeChunk:
    def __init__(self, content="", done=False):
        self.message = FakeMessage(content)
        self.done = done

    def get(self, key, default=None):
        return getattr(self, key, default)


class FakeJob:
    def __init__(self, job_id):
        self.job_id = job_id
        self.sealed = False


class FakeRouter:
    def __init__(self):
        self.jobs = []
        self.appends = []

    def submit_streaming(self, source, priority):
        job = FakeJob(len(self.jobs) + 1)
        self.jobs.append((job, source, priority))
        return job

    def append_chunks(self, job, chunks):
        self.appends.append((job, list(chunks)))
        return True

    def seal(self, job):
        job.sealed = True


def _arm_stream(motor, chunks):
    def _streaming(*, timeout, **kwargs):
        def _gen():
            for item in chunks:
                yield item

        return _gen()

    motor._ollama_chat_streaming = _streaming


@pytest.fixture
def motor(monkeypatch):
    m = llm_engine.MotorVocalIA(queue.Queue(), lambda event: None)
    m.current_model = "llama3"
    m.use_system_role = True
    m.system_prompt = PERSONA
    m.ollama = MagicMock()
    m._speech_router_enabled = True
    monkeypatch.setattr(llm_engine, "LLM_STREAMING_ENABLED", True)
    router = FakeRouter()
    m._ensure_router = lambda: router
    m._speech_router = router
    m._fake_router = router
    return m


def _buffered(motor, monkeypatch, replies):
    monkeypatch.setattr(llm_engine, "LLM_STREAMING_ENABLED", False)
    motor._ollama_chat_with_watchdog = MagicMock(
        side_effect=[{"message": {"content": r}} for r in replies]
    )
    motor._guardrail_fallback_line = MagicMock(return_value="linea neutral")


@pytest.mark.parametrize("leak", [LEAK_REPLY, MARKER_REPLY])
def test_buffered_leak_retries_with_a_nudge_and_accepts_a_clean_retry(motor, monkeypatch, caplog, leak):
    _buffered(motor, monkeypatch, [leak])
    motor._retry_after_guard_block = MagicMock(return_value="Te lo cuento yo misma, sin rodeos.")

    with caplog.at_level(logging.INFO):
        dialogo = motor._generar_dialogo("hola", source="direct", commit_history=True)

    assert dialogo == "Te lo cuento yo misma, sin rodeos."
    reason = motor._retry_after_guard_block.call_args.kwargs["guard_reason"]
    assert f"[{prompt_echo.PROMPT_LEAK_RULE_ID}]" in reason
    assert any("[PROMPT_LEAK]" in r.getMessage() for r in caplog.records)
    for record in caplog.records:
        assert "explícitamente" not in record.getMessage() and "me pidió" not in record.getMessage()


def test_buffered_leak_that_leaks_again_falls_to_the_neutral_line(motor, monkeypatch):
    _buffered(motor, monkeypatch, [LEAK_REPLY])
    motor._retry_after_guard_block = MagicMock(return_value=LEAK_REPLY)

    assert motor._generar_dialogo("hola", source="direct", commit_history=True) == "linea neutral"


def test_buffered_leak_on_chat_is_untouched(motor, monkeypatch):
    _buffered(motor, monkeypatch, [LEAK_REPLY])

    assert motor._generar_dialogo("hola", source="chat", commit_history=True) == LEAK_REPLY


def test_buffered_owner_of_a_shop_passes(motor, monkeypatch):
    reply = "El dueño de la tienda cerró temprano, qué pena."
    _buffered(motor, monkeypatch, [reply])

    assert motor._generar_dialogo("hola", source="direct", commit_history=True) == reply


def test_streamed_leak_before_first_submit_reverts_to_the_neutral_line(motor):
    _arm_stream(motor, [FakeChunk(LEAK_REPLY + " "), FakeChunk("Otra frase."), FakeChunk("", done=True)])
    motor._retry_after_guard_block = MagicMock(return_value="")
    motor._guardrail_fallback_line = MagicMock(return_value="linea neutral")

    dialogo = motor._generar_dialogo("hola", source="direct", commit_history=True)

    assert motor._fake_router.jobs == []
    assert dialogo == "linea neutral"


def test_streamed_leak_after_a_submit_truncates(motor, caplog):
    _arm_stream(
        motor,
        [FakeChunk("Primera frase limpia. "), FakeChunk(MARKER_REPLY + " "),
         FakeChunk("Tercera frase."), FakeChunk("", done=True)],
    )

    with caplog.at_level(logging.INFO):
        dialogo = motor._generar_dialogo("hola", source="direct", commit_history=True)

    spoken = [c for _, chunks in motor._fake_router.appends for c in chunks]
    assert spoken == ["Primera frase limpia."]
    assert dialogo == "Primera frase limpia."
    assert any("[PROMPT_LEAK]" in r.getMessage() for r in caplog.records)


def test_streamed_clean_owner_reply_is_spoken(motor):
    _arm_stream(motor, [FakeChunk("El dueño de la tienda cerró. "), FakeChunk("", done=True)])

    dialogo = motor._generar_dialogo("hola", source="direct", commit_history=True)

    assert dialogo == "El dueño de la tienda cerró."
