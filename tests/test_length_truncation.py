"""Token-limit truncation (live-session-fixes-20261005 L2).

Live log 2026-10-05 13:57:52 and 14:07:45: replies were cut mid-sentence with
`eval_count=512`. With thinking on, the per-model reasoning budget (512) was
the cap on the WHOLE `num_predict`, so ~400 tokens of hidden thinking starved
the answer; then `done_reason` was never read and `SentenceSplitter.flush()`
spoke the unterminated tail.
"""

import logging
import queue
from unittest.mock import MagicMock

import pytest

from opencohost.core import llm_engine
from opencohost.core.context.prompt_assembler import PromptContextAssembler
from opencohost.core.engine.llm_budget_engine import (
    DEFAULT_FALLBACK_BUDGET,
    InferenceIntent,
    resolve_generation_budget,
)
from opencohost.core.speech.sentence_splitter import SentenceSplitter, trim_to_last_sentence


class FakeMessage:
    def __init__(self, content=""):
        self.content = content
        self.thinking = None


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

    def submit_streaming(self, source, priority):
        job = FakeJob(len(self.jobs) + 1)
        self.jobs.append(job)
        return job

    def append_chunks(self, job, chunks):
        self.appends.append(list(chunks))
        return True

    def seal(self, job):
        pass


@pytest.fixture
def motor(monkeypatch):
    m = llm_engine.MotorVocalIA(queue.Queue(), lambda event: None)
    m.current_model = "llama3"
    m.use_system_role = True
    m.ollama = MagicMock()
    m._speech_router_enabled = True
    monkeypatch.setattr(llm_engine, "LLM_STREAMING_ENABLED", True)
    router = FakeRouter()
    m._ensure_router = lambda: router
    m._fake_router = router
    return m


def _arm_stream(motor, chunks):
    def _streaming(*, timeout, **kwargs):
        # A real generator: the loop closes the stream with contextlib.closing.
        return (chunk for chunk in chunks)

    motor._ollama_chat_streaming = _streaming


# ------------------------------------------------------------- stream tail


def test_stream_cut_by_length_never_speaks_the_unterminated_tail(motor, caplog):
    raw_sentences = []
    motor._emit_stream_chunk = lambda state: raw_sentences.__setitem__(
        slice(None), list(state.raw_sentences)
    )
    _arm_stream(
        motor,
        [
            FakeChunk("Uno. "),
            FakeChunk("Dos que"),
            FakeChunk("", done=True, done_reason="length", eval_count=512),
        ],
    )

    with caplog.at_level(logging.INFO):
        dialogo = motor._generar_dialogo("hola", source="direct", commit_history=True)

    assert raw_sentences == ["Uno."]
    assert motor._fake_router.appends == [["Uno."]]
    assert dialogo == "Uno."
    truncated = [r.getMessage() for r in caplog.records if "[STREAM_TRUNCATED]" in r.getMessage()]
    assert len(truncated) == 1
    assert "eval_count=512" in truncated[0]
    assert "Dos que" not in truncated[0]


def test_stream_that_stops_normally_still_flushes_its_last_sentence(motor):
    _arm_stream(
        motor,
        [
            FakeChunk("Uno. "),
            FakeChunk("Dos."),
            FakeChunk("", done=True, done_reason="stop"),
        ],
    )

    assert motor._generar_dialogo("hola", source="direct", commit_history=True) == "Uno. Dos."
    assert motor._fake_router.appends == [["Uno."], ["Dos."]]


# ----------------------------------------------------------- buffered tail


def test_buffered_reply_cut_by_length_is_trimmed(motor, monkeypatch):
    monkeypatch.setattr(llm_engine, "LLM_STREAMING_ENABLED", False)
    motor._ollama_chat_with_watchdog = MagicMock(
        return_value={
            "message": {"content": "Uno. ¿Dos? Tres que"},
            "done_reason": "length",
            "eval_count": 512,
        }
    )

    assert motor._generar_dialogo("hola", source="direct", commit_history=True) == "Uno. ¿Dos?"


def test_buffered_reply_without_length_stop_is_untouched(motor, monkeypatch):
    monkeypatch.setattr(llm_engine, "LLM_STREAMING_ENABLED", False)
    motor._ollama_chat_with_watchdog = MagicMock(
        return_value={"message": {"content": "Uno. Dos que"}, "done_reason": "stop"}
    )

    assert motor._generar_dialogo("hola", source="direct", commit_history=True) == "Uno. Dos que"


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Uno. Dos que", "Uno."),
        ("Uno. Dos.", "Uno. Dos."),
        ('Dijo "hola." Y luego', 'Dijo "hola."'),
        ("La versión 2.5 es", ""),
        ("sin terminador", ""),
        ("", ""),
    ],
)
def test_trim_to_last_sentence(text, expected):
    assert trim_to_last_sentence(text) == expected


@pytest.mark.parametrize(
    "text,expected",
    [
        # K1 (warmth-guardrails-runtime-20261006): markdown scaffolding left
        # dangling after the last whole sentence of a length-stopped reply.
        ("A.\n### Fase 1\n**Mi Tarea:** Anal", "A."),
        ("A.\n### Fase 1.\n**Mi Tarea:** Anal", "A."),
        ("A.\n## ¿Qué sigue?\nPrim", "A."),
        ("A. B.\n**Mi Tarea:**\nAnal", "A. B."),
        ("A.\n1. Primero\n2. Seg", "A."),
        ("Uno.\n- Dos.\n- Tr", "Uno.\n- Dos."),
        ("Uno. **Nota:** Ana", "Uno."),
        ("Uno.\n### Fin.", "Uno."),
    ],
)
def test_trim_drops_a_dangling_heading_label_or_list_marker(text, expected):
    assert trim_to_last_sentence(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "Uno. Dos.",
        "Precio: cinco dólares.",
        "Hola.\n**Nota:** esto ya terminó.",
        "Pasos:\n1. Abre el editor.\n2. Guarda.",
        "Mira esto: está bien!",
        "La versión 2.5 salió.",
    ],
)
def test_trim_keeps_complete_endings_intact(text):
    assert trim_to_last_sentence(text) == text


def test_sentence_splitter_discard_drops_the_pending_tail():
    splitter = SentenceSplitter()
    assert splitter.feed("Uno. Dos que") == ["Uno."]
    assert splitter.discard() == "Dos que"
    assert splitter.flush() == []


# ------------------------------------------------------------------ budget


def test_reasoning_budget_adds_to_the_answer_budget_when_thinking_is_on():
    res = resolve_generation_budget(
        intent=InferenceIntent.CHAT,
        allocated_context=16384,
        prompt_tokens=1300,
        requested_budget=512,
        reasoning_enabled=True,
    )
    assert res.think is True
    assert res.effective_budget > 512
    assert res.effective_budget == DEFAULT_FALLBACK_BUDGET + 512


def test_reasoning_budget_still_bounded_by_the_context_ceiling():
    res = resolve_generation_budget(
        intent=InferenceIntent.CHAT,
        allocated_context=2048,
        prompt_tokens=1200,  # remaining = 720
        requested_budget=512,
        reasoning_enabled=True,
    )
    assert res.effective_budget == 720


def test_budget_cap_with_thinking_off_is_unchanged():
    res = resolve_generation_budget(
        intent=InferenceIntent.CHAT,
        allocated_context=16384,
        prompt_tokens=1300,
        requested_budget=512,
        reasoning_enabled=False,
    )
    assert res.effective_budget == 512


def test_assembled_num_predict_exceeds_the_reasoning_budget_when_thinking():
    setup = PromptContextAssembler().assemble(
        "hola",
        "direct",
        system_prompt="Eres Kira.",
        use_system_role=True,
        is_local=True,
        provider_cfg={},
        request_model="hf.co/unsloth/gemma-4-E4B-it-GGUF:IQ4_NL",
        history_snapshot=[],
        is_reasoning_model=lambda m: True,
        reasoning_settings_resolver=lambda m: {"enabled": True, "budget_tokens": 512},
    )
    assert setup.think is True
    assert setup.opciones_llm["num_predict"] > 512


# ------------------------------------------- owner reply cap (identity C-fix)


@pytest.mark.parametrize("source", ["direct", "ptt"])
def test_capped_owner_reply_buffered_ends_on_a_sentence_boundary(motor, monkeypatch, source):
    """A local owner reply that hits the OWNER_REPLY_MAX_TOKENS num_predict cap
    (done_reason == "length") is trimmed by the existing L2 path, so the
    returned text never ends mid-sentence."""
    monkeypatch.setattr(llm_engine, "LLM_STREAMING_ENABLED", False)
    motor._ollama_chat_with_watchdog = MagicMock(
        return_value={
            "message": {"content": "Primero, abre la app. Luego elige el modelo. Despu\u00e9s revisa la vo"},
            "done_reason": "length",
            "eval_count": 320,
        }
    )

    out = motor._generar_dialogo("hola", source=source, commit_history=True)

    assert out == "Primero, abre la app. Luego elige el modelo."
    assert out.endswith(".")


def test_capped_owner_reply_streamed_trims_spoken_and_returned_text(motor):
    _arm_stream(
        motor,
        [
            FakeChunk("Primero, abre la app. "),
            FakeChunk("Luego elige el modelo. Despu\u00e9s revisa la vo"),
            FakeChunk("", done=True, done_reason="length", eval_count=320),
        ],
    )

    out = motor._generar_dialogo("hola", source="direct", commit_history=True)

    assert out == "Primero, abre la app. Luego elige el modelo."
    spoken = [s for batch in motor._fake_router.appends for s in batch]
    assert spoken == ["Primero, abre la app.", "Luego elige el modelo."]
