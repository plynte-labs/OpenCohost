"""runtime-fixes-20261110 T1 (A): a reverted streamed attempt recovers.

Live regression: the first streamed sentence tripped a guard before any audio
(`state.job is None` -> "revert"), the loop kept consuming to the token cap
(`done_reason=length`), `trim_to_last_sentence` erased the unterminated text, and
the empty reply skipped the guard retry and the fallback line, so the turn was
dropped and nothing was spoken.

Required: a reverted attempt always goes through the buffered recovery (one
retry with the rule's nudge, then the rule's fallback line). Never an empty reply
or `turn_dropped`.
"""

import logging
import queue
from unittest.mock import MagicMock

import pytest

from opencohost.core import llm_engine
from opencohost.core.engine.generation_orchestrator import StreamAttemptState
from opencohost.i18n import active as i18n_active


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


class FakeRouter:
    def __init__(self):
        self.jobs = []
        self.appends = []

    def submit_streaming(self, source, priority, **kwargs):
        job = FakeJob(len(self.jobs) + 1)
        self.jobs.append(job)
        return job

    def append_chunks(self, job, chunks):
        self.appends.append((job, list(chunks)))
        return True

    def seal(self, job):
        pass


def _arm_stream(motor, chunks):
    record = {"closed": False, "yielded": 0}

    def _streaming(*, timeout, **kwargs):
        def _gen():
            try:
                for item in chunks:
                    record["yielded"] += 1
                    yield item
            finally:
                record["closed"] = True

        return _gen()

    motor._ollama_chat_streaming = _streaming
    return record


@pytest.fixture
def motor(monkeypatch):
    events = []
    m = llm_engine.MotorVocalIA(queue.Queue(), events.append)
    m.current_model = "llama3"
    m.use_system_role = True
    m.ollama = MagicMock()
    m._speech_router_enabled = True
    monkeypatch.setattr(llm_engine, "LLM_STREAMING_ENABLED", True)
    router = FakeRouter()
    m._ensure_router = lambda: router
    m._speech_router = router
    m._fake_router = router
    m._events = events
    m._emitted = []
    m._emit_dialogue = lambda text, source, **kw: m._emitted.append(text)
    m._speak_or_submit = MagicMock()
    return m


def _retry_returns(motor, content):
    motor._ollama_chat_with_watchdog = MagicMock(
        return_value={"message": {"content": content}}
    )
    return motor._ollama_chat_with_watchdog


def _length_cut(chunks):
    """A stream that ends at the token cap: the last chunk says done=length."""
    return list(chunks) + [FakeChunk("", done=True, done_reason="length")]


# The incident shape: a numbered structure dump. The splitter closes
# "```text\n1." as the first sentence (it trips the fence rule), the stream ends
# with done=length, and trim_to_last_sentence erases the whole text.
CODE_NO_TERMINATOR = [
    FakeChunk("```text\n1. Chasis\n2. Motor\n"),
    FakeChunk("3. Ruedas\n4. Frenos\n"),
    FakeChunk("5. Suspension"),
]

# Another existing rule that reverts: R9 AI self-identification, first sentence.
AI_SELF_ID_NO_TERMINATOR = [
    FakeChunk("Como IA, no puedo opinar. "),
    FakeChunk("y luego sigo sin ningun punto final en el resto del texto"),
    FakeChunk(" que se corta por el limite de tokens"),
]


@pytest.mark.parametrize(
    "chunks, rule",
    [(CODE_NO_TERMINATOR, "no_code_output"), (AI_SELF_ID_NO_TERMINATOR, "no_ai_self_identification")],
)
def test_reverted_length_cut_attempt_retries_then_speaks(motor, chunks, rule):
    _arm_stream(motor, _length_cut(chunks))
    retry = _retry_returns(motor, "Un coche tiene chasis, motor y ruedas.")

    dialogo = motor._generar_dialogo("dime la estructura de un coche", source="direct", commit_history=True)

    assert dialogo == "Un coche tiene chasis, motor y ruedas."
    retry.assert_called_once()
    nudged = retry.call_args.kwargs["messages"][-1]["content"]
    assert i18n_active.guardrail_retry_nudge(rule) in nudged
    assert motor._fake_router.jobs == []


@pytest.mark.parametrize(
    "chunks, rule",
    [(CODE_NO_TERMINATOR, "no_code_output"), (AI_SELF_ID_NO_TERMINATOR, "no_ai_self_identification")],
)
def test_reverted_attempt_whose_retry_is_blocked_speaks_the_fallback_line(motor, chunks, rule):
    _arm_stream(motor, _length_cut(chunks))
    blocked_again = "Como IA, no puedo opinar." if rule == "no_ai_self_identification" else "```\nx = 1\n```"
    _retry_returns(motor, blocked_again)

    dialogo = motor._generar_dialogo("dime la estructura de un coche", source="direct", commit_history=True)

    assert dialogo
    reason = f"Non-negotiable violation [{rule}]: x"
    allowed_lines = set(i18n_active.guardrail_fallback_lines())
    assert dialogo in allowed_lines
    assert dialogo == motor._guardrail_fallback_line("direct", reason) or dialogo in allowed_lines


@pytest.mark.parametrize("chunks", [CODE_NO_TERMINATOR, AI_SELF_ID_NO_TERMINATOR])
def test_reverted_attempt_with_an_empty_retry_speaks_the_fallback_line(motor, chunks):
    _arm_stream(motor, _length_cut(chunks))
    _retry_returns(motor, "")

    dialogo = motor._generar_dialogo("dime la estructura de un coche", source="direct", commit_history=True)

    assert dialogo in set(i18n_active.guardrail_fallback_lines())


@pytest.mark.parametrize("chunks", [CODE_NO_TERMINATOR, AI_SELF_ID_NO_TERMINATOR])
def test_reverted_turn_is_spoken_and_never_dropped(motor, chunks):
    _arm_stream(motor, _length_cut(chunks))
    _retry_returns(motor, "Un coche tiene chasis, motor y ruedas.")

    motor._ejecutar_inferencia("dime la estructura de un coche", source="direct")

    assert "turn_dropped" not in motor._events
    assert motor._emitted == ["Un coche tiene chasis, motor y ruedas."]
    motor._speak_or_submit.assert_called_once()


@pytest.mark.parametrize("chunks", [CODE_NO_TERMINATOR, AI_SELF_ID_NO_TERMINATOR])
def test_reverted_turn_with_blocked_retry_is_spoken_not_dropped(motor, chunks):
    _arm_stream(motor, _length_cut(chunks))
    _retry_returns(motor, "")

    motor._ejecutar_inferencia("dime la estructura de un coche", source="direct")

    assert "turn_dropped" not in motor._events
    assert len(motor._emitted) == 1 and motor._emitted[0] in set(i18n_active.guardrail_fallback_lines())


def test_revert_never_depends_on_the_text_surviving_the_length_trim(motor):
    """Even when the text carries no sentence terminator at all, the carried rule
    id drives the recovery (the trim would have returned an empty string)."""
    from opencohost.core.speech.sentence_splitter import trim_to_last_sentence

    text = "".join(c.message.content for c in CODE_NO_TERMINATOR)
    assert trim_to_last_sentence(text) == ""
    _arm_stream(motor, _length_cut(CODE_NO_TERMINATOR))
    retry = _retry_returns(motor, "Respuesta hablada.")

    assert motor._generar_dialogo("x", source="direct", commit_history=True) == "Respuesta hablada."
    retry.assert_called_once()


# ------------------------------------------------------- abort after revert


def test_code_revert_aborts_the_stream_instead_of_consuming_to_the_cap(motor):
    record = _arm_stream(motor, _length_cut(CODE_NO_TERMINATOR))
    _retry_returns(motor, "Respuesta hablada.")

    motor._generar_dialogo("x", source="direct", commit_history=True)

    assert record["yielded"] == 1  # stopped at the tripping chunk
    assert record["closed"] is True  # contextlib.closing closed the generator (socket) on abort


def test_revert_of_a_rule_that_reasoning_could_trip_still_consumes_the_stream(motor):
    """A local model may open with reasoning that ends in an orphan close tag; the
    THINK_RESCUE window needs the stream, so only no_code_output aborts early."""
    record = _arm_stream(motor, _length_cut(AI_SELF_ID_NO_TERMINATOR))
    _retry_returns(motor, "Respuesta hablada.")

    motor._generar_dialogo("x", source="direct", commit_history=True)

    assert record["yielded"] == len(AI_SELF_ID_NO_TERMINATOR) + 1
    assert record["closed"] is True


# --------------------------------------------------------------- logging


@pytest.mark.parametrize(
    "chunks, rule",
    [(CODE_NO_TERMINATOR, "no_code_output"), (AI_SELF_ID_NO_TERMINATOR, "no_ai_self_identification")],
)
def test_revert_is_logged_with_the_rule_id_and_no_text(motor, caplog, chunks, rule):
    _arm_stream(motor, _length_cut(chunks))
    _retry_returns(motor, "Respuesta hablada.")

    with caplog.at_level(logging.INFO):
        motor._generar_dialogo("x", source="direct", commit_history=True)

    lines = [r.getMessage() for r in caplog.records if "[STREAM_REVERT]" in r.getMessage()]
    assert len(lines) == 1
    assert f"rule={rule}" in lines[0]
    assert "source=direct" in lines[0]
    assert "chasis" not in lines[0] and "Como IA" not in lines[0]


def test_fence_branch_logs_the_revert_too(motor, caplog):
    state = StreamAttemptState(source="direct")
    from types import SimpleNamespace

    with caplog.at_level(logging.INFO):
        action = motor._generation_orchestrator.handle_stream_sentence(
            "```python", state, SimpleNamespace(web_searched=False)
        )

    assert action == "revert"
    assert state.reverted_rule == "no_code_output"
    assert any("[STREAM_REVERT]" in r.getMessage() and "rule=no_code_output" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------- unaffected behaviours


def test_a_clean_length_cut_stream_is_still_trimmed_to_whole_sentences(motor):
    _arm_stream(
        motor,
        _length_cut([FakeChunk("Primera frase buena. "), FakeChunk("Segunda frase cortada a med")]),
    )
    motor._retry_after_guard_block = MagicMock()

    dialogo = motor._generar_dialogo("x", source="direct", commit_history=True)

    assert dialogo == "Primera frase buena."
    motor._retry_after_guard_block.assert_not_called()
