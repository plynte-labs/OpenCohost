"""vision-and-prompt-leaks-20261018 T4: Whisper silence hallucinations in owner speech.

Live 2026-10-17: "Gracias por ver este video." and "Muchas gracias a todos aquellos."
reached the owner transcript and the drafts. Only a sentence that IS a known
hallucination is dropped; a sentence that merely contains "gracias" is kept; when
nothing is left the turn is dropped.
"""

import logging
import queue

import pytest

from opencohost.api.dispatch import Dispatcher
from opencohost.api.owner_speech_dispatch import OwnerSpeechFilterDispatcher
from opencohost.i18n import active as i18n_active
from opencohost.stt.hallucination_filter import filter_owner_speech


@pytest.mark.parametrize("text", [
    "Gracias por ver este video.",
    "gracias por ver este vídeo",
    "¡Gracias por ver el video!",
    "Muchas gracias a todos aquellos.",
    "Subtítulos realizados por la comunidad de Amara.org",
    "Subtítulos por la comunidad de Amara.org",
    "Thanks for watching!",
    "Thank you for watching.",
    "Thanks for watching this video.",
])
def test_whole_sentence_hallucination_drops_the_turn(text):
    result = filter_owner_speech(text)
    assert result.dropped is True
    assert result.text == ""
    assert result.removed == 1


def test_hallucination_among_real_sentences_is_stripped():
    result = filter_owner_speech("Oye Kira, mira esto. Gracias por ver este video. ¿Qué opinas?")
    assert result.dropped is False
    assert result.text == "Oye Kira, mira esto. ¿Qué opinas?"
    assert result.removed == 1


def test_repeated_hallucination_is_fully_removed():
    result = filter_owner_speech("Gracias por ver este video. Gracias por ver este video.")
    assert result.dropped is True and result.removed == 2


@pytest.mark.parametrize("text", [
    "Gracias por ver este video conmigo, Kira, ¿qué te pareció?",
    "Muchas gracias a todos por venir al stream.",
    "Muchas gracias a todos.",
    "Gracias, Kira, me ayudaste mucho.",
    "Gracias por ver el stream de hoy.",
    "Thanks for watching my friend's stream with me.",
    "Los subtítulos están mal.",
    "Hola Kira",
])
def test_sentence_that_merely_contains_the_phrase_is_kept(text):
    result = filter_owner_speech(text)
    assert result.dropped is False
    assert result.text == text
    assert result.removed == 0


@pytest.mark.parametrize("text", ["", "   ", None])
def test_empty_input_is_not_a_hallucination_just_empty(text):
    result = filter_owner_speech(text)
    assert result.removed == 0
    assert result.text == ""


# ------------------------------------------------------------- dispatcher hook

def _dispatcher():
    q = queue.Queue()
    return OwnerSpeechFilterDispatcher(Dispatcher(q)), q


def _send(dispatcher, text, source="ptt"):
    return dispatcher.dispatch(
        "process_context",
        i18n_active.ptt_wrapper().format(text=text),
        history_text=i18n_active.ptt_history_wrapper().format(text=text),
        source=source,
        submitted_at=1.0,
    )


def test_clean_ptt_turn_passes_through_unchanged():
    dispatcher, q = _dispatcher()
    result = _send(dispatcher, "Oye Kira, ¿cómo vas?")
    assert result.state == "accepted"
    command, payload, history, source, _ = q.get_nowait()
    assert command == "process_context" and source == "ptt"
    assert payload == i18n_active.ptt_wrapper().format(text="Oye Kira, ¿cómo vas?")


def test_hallucination_only_ptt_turn_is_dropped_and_never_enqueued(caplog):
    dispatcher, q = _dispatcher()
    with caplog.at_level(logging.INFO):
        result = _send(dispatcher, "Gracias por ver este video.")
    assert result.state == "filtered"
    assert q.empty()
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "[STT_HALLUCINATION]" in logged and "action=drop" in logged
    assert "ver este video" not in logged


def test_mixed_ptt_turn_is_rewritten_with_both_wrappers():
    dispatcher, q = _dispatcher()
    _send(dispatcher, "Mira mi CV. Gracias por ver este video.")
    _command, payload, history, _source, _ts = q.get_nowait()
    assert payload == i18n_active.ptt_wrapper().format(text="Mira mi CV.")
    assert history == i18n_active.ptt_history_wrapper().format(text="Mira mi CV.")


def test_non_ptt_sources_are_never_filtered():
    dispatcher, q = _dispatcher()
    result = _send(dispatcher, "Gracias por ver este video.", source="chat")
    assert result.state == "accepted"
    assert not q.empty()


def test_missing_history_text_fails_open():
    dispatcher, q = _dispatcher()
    result = dispatcher.dispatch("process_context", "Gracias por ver este video.", source="ptt")
    assert result.state == "accepted"
    assert not q.empty()


def test_other_commands_and_attributes_delegate():
    dispatcher, q = _dispatcher()
    assert dispatcher.state_version == 0
    dispatcher.dispatch("pause", {})
    assert dispatcher.state_version == 1


def test_create_app_wires_the_filter_between_ptt_and_the_dispatcher():
    from fastapi.testclient import TestClient

    import opencohost.api.main as main_mod
    from tests.test_api_phase1 import FakeHost

    app = main_mod.create_app(host_factory=FakeHost, cors_origins=["http://localhost"])
    with TestClient(app):
        controller = app.state.ptt_controller
        assert isinstance(controller._dispatcher, OwnerSpeechFilterDispatcher)
        assert controller._dispatcher._inner is app.state.dispatcher
