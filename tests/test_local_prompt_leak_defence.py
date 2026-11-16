"""Prompt-leak defence on the LOCAL Ollama path (live-session-fixes-20261005 L1).

Live log 2026-10-05: a community Qwen fine-tune put the whole persona system
prompt into `content` followed by an orphan `</think>` (13:37:53), and another
reply was `<th>` x32 `</th> </think>`, spoken as "th" 32 times (13:40:08).

Covers: the think-tag split on local stream deltas and local buffered content,
the prompt-echo guard (stream revert / truncate, buffered block), the
sanitizer's table-tag and degenerate-sentence rules, and the display text the
UI receives for an in-flight streamed turn.
"""

import logging
import queue
from unittest.mock import MagicMock

import pytest

from opencohost.core import llm_engine
from opencohost.core.speech.tts_sanitizer import (
    _sanitize_tts_text_for_display,
    _sanitize_tts_text_for_playback,
)


PERSONA = (
    "PERSONALIDAD:\n"
    "- Sarcástica con ingenio afilado y humor negro sutil que nunca se disculpa\n"
    "- Nunca saludes ni te despidas\n\n"
    "ESTILO:\n"
    "- Hablas SIEMPRE en español neutro latinoamericano, con tuteo y frases cortas.\n"
    "- Respondes en 2-4 oraciones. Nunca monólogos, pero tampoco monosílabos.\n"
    "- Comentas el juego del streamer con observaciones agudas y nunca repites chistes."
)

# A 60+ char sentence copied from the persona above.
ECHO_SENTENCE = (
    "Comentas el juego del streamer con observaciones agudas y nunca repites chistes."
)


class FakeMessage:
    def __init__(self, content="", thinking=None):
        self.content = content
        self.thinking = thinking


class FakeChunk:
    def __init__(self, content="", done=False, **stats):
        self.message = FakeMessage(content)
        self.done = done
        for key, value in stats.items():
            setattr(self, key, value)

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
        self.sealed = []

    def submit_streaming(self, source, priority):
        job = FakeJob(len(self.jobs) + 1)
        self.jobs.append((job, source, priority))
        return job

    def append_chunks(self, job, chunks):
        self.appends.append((job, list(chunks)))
        return True

    def seal(self, job):
        job.sealed = True
        self.sealed.append(job)


def _arm_stream(motor, chunks):
    record = {"yielded": 0}

    def _streaming(*, timeout, **kwargs):
        def _gen():
            for item in chunks:
                record["yielded"] += 1
                yield item

        return _gen()

    motor._ollama_chat_streaming = _streaming
    return record


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


def PromptEchoDetector(reference):
    from opencohost.core.context.prompt_echo import PromptEchoDetector as _Detector

    return _Detector(reference)


def _chunked(text, size=17):
    return [FakeChunk(text[i:i + size]) for i in range(0, len(text), size)]


# ------------------------------------------------------------ think split


def test_local_stream_of_persona_then_orphan_close_never_speaks(motor):
    """13:37:53: the persona prompt, then an orphan `</think>`. The text
    before the orphan close is reasoning: no job, no append_chunks."""
    chunks = _chunked(PERSONA + "\n</think>") + [FakeChunk("", done=True)]
    _arm_stream(motor, chunks)

    dialogo = motor._generar_dialogo("hola", source="direct", commit_history=True)

    assert motor._fake_router.jobs == []
    assert motor._fake_router.appends == []
    assert "Sarcástica" not in dialogo
    assert "PERSONALIDAD" not in dialogo


def test_local_stream_routes_think_block_out_of_spoken_text(motor):
    _arm_stream(
        motor,
        [
            FakeChunk("<think>Debo contestar breve.</think>"),
            FakeChunk("Primera frase limpia. "),
            FakeChunk("Segunda frase tranquila."),
            FakeChunk("", done=True),
        ],
    )

    dialogo = motor._generar_dialogo("hola", source="direct", commit_history=True)

    assert dialogo == "Primera frase limpia. Segunda frase tranquila."
    spoken = [c for _, chunks in motor._fake_router.appends for c in chunks]
    assert spoken == ["Primera frase limpia.", "Segunda frase tranquila."]


def test_local_buffered_orphan_close_keeps_only_the_dialogue(motor, monkeypatch):
    monkeypatch.setattr(llm_engine, "LLM_STREAMING_ENABLED", False)
    motor._ollama_chat_with_watchdog = MagicMock(
        return_value={
            "message": {"content": "El usuario saluda, contesto corto…</think> Hola."}
        }
    )

    dialogo = motor._generar_dialogo("hola", source="direct", commit_history=True)

    assert dialogo == "Hola."


def test_local_buffered_closed_think_block_is_stripped(motor, monkeypatch):
    monkeypatch.setattr(llm_engine, "LLM_STREAMING_ENABLED", False)
    motor._ollama_chat_with_watchdog = MagicMock(
        return_value={"message": {"content": "<think>plan</think>Respuesta clara."}}
    )

    assert motor._generar_dialogo("hola", source="direct", commit_history=True) == (
        "Respuesta clara."
    )


# ------------------------------------------------------------- echo guard


def test_echo_detector_flags_a_copied_system_sentence():
    detector = PromptEchoDetector(PERSONA)
    assert len(ECHO_SENTENCE) >= 60
    assert detector.is_echo(ECHO_SENTENCE)
    # Case, accents and punctuation do not hide the echo.
    assert detector.is_echo(ECHO_SENTENCE.upper().replace(",", ""))


def test_echo_detector_flags_a_multi_line_persona_dump():
    detector = PromptEchoDetector(PERSONA)
    assert detector.is_echo(PERSONA[:200])


def test_echo_detector_ignores_normal_replies_and_short_rule_quotes():
    detector = PromptEchoDetector(PERSONA)
    for reply in (
        "Ese salto fue horrible, pero al menos el jefe se rió de ti un rato.",
        "Nunca saludes ni te despidas.",  # one short rule, quoted
        "Sí.",
        "Hablas en español, claro, como siempre que juegas de noche.",
    ):
        assert not detector.text_has_echo(reply), reply


def test_echo_detector_with_empty_reference_never_flags():
    assert not PromptEchoDetector("").text_has_echo(ECHO_SENTENCE)


def test_buffered_echo_is_blocked_and_never_logs_the_prompt(motor, monkeypatch, caplog):
    monkeypatch.setattr(llm_engine, "LLM_STREAMING_ENABLED", False)
    motor._ollama_chat_with_watchdog = MagicMock(
        return_value={"message": {"content": f"Claro. {ECHO_SENTENCE}"}}
    )
    motor._guardrail_fallback_line = MagicMock(return_value="linea neutral")

    with caplog.at_level(logging.INFO):
        dialogo = motor._generar_dialogo("hola", source="direct", commit_history=True)

    assert dialogo == "linea neutral"
    assert any("[PROMPT_ECHO]" in r.getMessage() for r in caplog.records)
    for record in caplog.records:
        assert "observaciones agudas" not in record.getMessage()


def test_buffered_normal_reply_is_not_blocked(motor, monkeypatch):
    monkeypatch.setattr(llm_engine, "LLM_STREAMING_ENABLED", False)
    reply = "Ese salto fue horrible, pero al menos el jefe se rió de ti un rato."
    motor._ollama_chat_with_watchdog = MagicMock(
        return_value={"message": {"content": reply}}
    )

    assert motor._generar_dialogo("hola", source="direct", commit_history=True) == reply


def test_streamed_echo_before_first_submit_reverts(motor):
    _arm_stream(
        motor,
        [FakeChunk(ECHO_SENTENCE + " "), FakeChunk("Otra frase."), FakeChunk("", done=True)],
    )
    motor._retry_after_guard_block = MagicMock(return_value="")
    motor._guardrail_fallback_line = MagicMock(return_value="linea neutral")

    dialogo = motor._generar_dialogo("hola", source="direct", commit_history=True)

    assert motor._fake_router.jobs == []
    assert dialogo == "linea neutral"


def test_streamed_echo_after_a_submit_truncates(motor, caplog):
    _arm_stream(
        motor,
        [
            FakeChunk("Primera frase limpia. "),
            FakeChunk(ECHO_SENTENCE + " "),
            FakeChunk("Tercera frase."),
            FakeChunk("", done=True),
        ],
    )

    with caplog.at_level(logging.INFO):
        dialogo = motor._generar_dialogo("hola", source="direct", commit_history=True)

    spoken = [c for _, chunks in motor._fake_router.appends for c in chunks]
    assert spoken == ["Primera frase limpia."]
    assert dialogo == "Primera frase limpia."
    assert any("[PROMPT_ECHO]" in r.getMessage() for r in caplog.records)


# -------------------------------------------------------------- sanitizer


def test_table_tags_then_orphan_think_close_sanitize_to_nothing():
    assert _sanitize_tts_text_for_playback("<th>\n" * 32 + "</th>\n</think>") == ""


def test_table_tags_are_dropped_not_spoken_as_words():
    text = "<table><tr><th>Nombre</th><td>Kira</td></tr></table>"
    assert _sanitize_tts_text_for_playback(text) == "Nombre Kira"
    assert _sanitize_tts_text_for_playback("<th>\n" * 32) == ""


def test_placeholder_brackets_still_keep_their_word():
    assert _sanitize_tts_text_for_playback("Hola <usuario>, bienvenido.") == (
        "Hola usuario, bienvenido."
    )


def test_degenerate_repeated_token_sentence_is_rejected():
    assert _sanitize_tts_text_for_playback("th th th th th th th th.") == ""
    assert _sanitize_tts_text_for_playback("Hola. th th th th th th. Adiós.") == (
        "Hola. Adiós."
    )


def test_short_or_varied_repetition_is_kept():
    assert _sanitize_tts_text_for_playback("no no no no.") == "no no no no."
    varied = "Sí, sí, claro que sí, lo sé bien."
    assert _sanitize_tts_text_for_playback(varied) == varied


def test_orphan_reasoning_close_drops_the_text_before_it():
    assert _sanitize_tts_text_for_display("plan secreto</think> Hola.") == "Hola."


# ---------------------------------------------------------------- display


def test_stream_display_text_carries_no_control_tags(motor):
    shown = []

    def _emit(state):
        shown.append(state.display_prefix())

    motor._emit_stream_chunk = _emit
    _arm_stream(
        motor,
        [
            FakeChunk("Hola <analysis>secreto</analysis>amigo, todo bien. "),
            FakeChunk("Otra **frase** clara."),
            FakeChunk("", done=True),
        ],
    )

    motor._generar_dialogo("hola", source="direct", commit_history=True)

    assert shown, "the stream emitted display text"
    for text in shown:
        assert "<" not in text and "secreto" not in text
    assert shown[-1] == "Hola amigo, todo bien. Otra **frase** clara."


def test_local_stream_drops_unclosed_reasoning_held_before_an_orphan_close(motor):
    """Reasoning with no closed sentence is still in the sentence splitter when
    the orphan `</think>` arrives: it is dropped, only the answer is spoken."""
    _arm_stream(
        motor,
        [
            FakeChunk("el usuario saluda y yo"),
            FakeChunk(" respondo breve"),
            FakeChunk("</think>"),
            FakeChunk("Hola, ¿qué tal? "),
            FakeChunk("", done=True),
        ],
    )

    dialogo = motor._generar_dialogo("hola", source="direct", commit_history=True)

    assert dialogo == "Hola, ¿qué tal?"
    spoken = [c for _, chunks in motor._fake_router.appends for c in chunks]
    assert spoken == ["Hola, ¿qué tal?"]


def test_shared_think_splitter_reports_an_orphan_close():
    from opencohost.core.providers.think_tags import ThinkTagStreamSplitter

    splitter = ThinkTagStreamSplitter(pre_first_tag_holdback=0)
    content, thinking = splitter.feed("razono aqui</think>Hola")
    assert splitter.orphan_close_seen is True
    assert thinking == "razono aqui"
    assert content + splitter.flush()[0] == "Hola"
