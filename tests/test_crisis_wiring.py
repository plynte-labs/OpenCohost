"""S1: engine wiring. A crisis message from the owner marks the whole
generation; a blocked or empty reply is replaced by the crisis reply."""
from __future__ import annotations

import queue

import pytest

from opencohost.core import llm_engine as engine_mod
from opencohost.core.llm_engine import MotorVocalIA
from opencohost.core.safety.turn import is_crisis_turn
from opencohost.i18n import active as i18n_active
from opencohost.i18n.registry import discover_bundles, official_locales_dir
from opencohost.i18n.startup import resolve_active_bundle

CRISIS_TEXT = "ya no quiero seguir viviendo"
NORMAL_TEXT = "que opinas del juego de anoche"


@pytest.fixture(autouse=True)
def _es_bundle():
    registry = discover_bundles(official_locales_dir(), "official")
    i18n_active.set_active_bundle(resolve_active_bundle(locale="es", registry=registry))
    yield
    i18n_active.reset_active_bundle()


def _motor() -> MotorVocalIA:
    motor = MotorVocalIA(queue.Queue(), lambda event: None)
    motor.current_model = "llama3"
    motor._reasoning_model_cache["llama3"] = False
    motor._model_ctx_limit = {"llama3": 8192}
    motor._resolve_chat_watchdog_timeout = lambda *a, **kw: 30.0
    return motor


@pytest.mark.parametrize("source", ["direct", "ptt", "owner-bundle"])
def test_owner_crisis_text_marks_the_generation(source):
    motor = _motor()
    seen = {}

    def core(contexto, source="direct", **kw):
        seen["flag"] = is_crisis_turn()
        return ""

    motor._generar_dialogo_core = core
    motor._generar_dialogo(CRISIS_TEXT, source=source, commit_history=True)
    assert seen["flag"] is True
    assert is_crisis_turn() is False, "flag must not leak past the turn"


@pytest.mark.parametrize("source", ["direct", "ptt", "owner-bundle"])
def test_normal_owner_text_is_not_marked(source):
    motor = _motor()
    seen = {}

    def core(contexto, source="direct", **kw):
        seen["flag"] = is_crisis_turn()
        return ""

    motor._generar_dialogo_core = core
    motor._generar_dialogo(NORMAL_TEXT, source=source, commit_history=True)
    assert seen["flag"] is False


@pytest.mark.parametrize("source", ["chat", "kira-agenda", "kira-agenda-x"])
def test_non_owner_sources_never_mark_a_crisis_turn(source):
    motor = _motor()
    seen = {}

    def core(contexto, source="direct", **kw):
        seen["flag"] = is_crisis_turn()
        return ""

    motor._generar_dialogo_core = core
    motor._generar_dialogo(CRISIS_TEXT, source=source, commit_history=True)
    assert seen["flag"] is False


def _setup(motor, source):
    return motor._build_generation_request(
        CRISIS_TEXT, source, is_local=True, provider_cfg={},
        request_model="llama3", watchdog_timeout=None,
    )


def _finalize(motor, raw, source="direct"):
    setup = _setup(motor, source)
    outcome = engine_mod._GenerationAttemptOutcome(raw_content=raw, respuesta=None)
    return motor._finalize_generation(
        setup, outcome, CRISIS_TEXT, source=source, commit_history=False,
        log_prefix="LLM", history_text=None, is_local=True, provider_cfg={},
        request_model="llama3",
    )


def test_blocked_reply_on_a_crisis_turn_speaks_the_crisis_reply():
    from opencohost.core.safety.turn import crisis_turn

    motor = _motor()
    motor._retry_after_guard_block = lambda **kw: ""
    with crisis_turn():
        # hate speech stays locked on a crisis turn, so this is still blocked
        result = _finalize(motor, "Eres un pendejo, haz lo que quieras.")
    assert str(result) == i18n_active.crisis_reply()


def test_empty_reply_on_a_crisis_turn_speaks_the_crisis_reply():
    from opencohost.core.safety.turn import crisis_turn

    motor = _motor()
    with crisis_turn():
        result = _finalize(motor, "")
    assert str(result) == i18n_active.crisis_reply()


def test_empty_reply_on_a_normal_turn_stays_empty():
    motor = _motor()
    assert str(_finalize(motor, "")) == ""


# ---------------------------------------------------------------- streaming
from unittest.mock import MagicMock  # noqa: E402

from tests.test_llm_streaming_loop import FakeChunk, _arm_stream  # noqa: E402
from tests.test_llm_streaming_loop import motor as stream_motor  # noqa: E402,F401


def test_crisis_turn_with_a_blocked_later_sentence_ends_with_the_crisis_reply(stream_motor):
    """A sentence trips a still-locked rule after audio is on air: the turn is
    not truncated into silence, the crisis reply is appended."""
    motor = stream_motor
    _arm_stream(
        motor,
        [
            FakeChunk("Estoy aquí contigo. "),
            FakeChunk("Eres un pendejo y lo sabes. "),
            FakeChunk("Tercera frase nunca vista."),
            FakeChunk("", done=True),
        ],
    )
    motor._retry_after_guard_block = MagicMock()
    dialogo = motor._generar_dialogo(CRISIS_TEXT, source="direct", commit_history=True)

    reply = i18n_active.crisis_reply()
    assert str(dialogo).startswith("Estoy aquí contigo.")
    assert str(dialogo).endswith(reply)
    assert "pendejo" not in str(dialogo)
    router = motor._fake_router
    spoken = " ".join(" ".join(chunks) for _, chunks in router.appends)
    assert "pendejo" not in spoken
    assert reply.split(".")[0] in spoken
    assert len(router.sealed) == 1


def test_normal_turn_with_a_blocked_later_sentence_is_still_truncated(stream_motor):
    motor = stream_motor
    _arm_stream(
        motor,
        [
            FakeChunk("Primera frase limpia. "),
            FakeChunk("Eres un pendejo y lo sabes. "),
            FakeChunk("", done=True),
        ],
    )
    motor._retry_after_guard_block = MagicMock()
    dialogo = motor._generar_dialogo(NORMAL_TEXT, source="direct", commit_history=True)
    assert str(dialogo) == "Primera frase limpia."
