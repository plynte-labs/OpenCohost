"""T1 (memory-write-quality-20261020): owner-reflection grounding.

Live evidence 2026-10-20: the reflection job wrote "The streamer lives in Wautla,
Morelos." from a single owner exchange that was a web request. A question or request
says nothing about the owner's own life, so:

* an exchange whose every sentence is a question or a request is kept in the ring but
  never distilled (like a recall exchange);
* the prompt forbids turning one into a fact, or inferring residence, job or ownership;
* a parsed fact whose cited turns are all question/request-shaped is dropped and
  counted as ``ungrounded``.

Draft capture is untouched (owner decision 2026-08-14). Text is synthetic except the
Wautla exchange, which is the fixture under test.
"""
from __future__ import annotations

import logging

import pytest

from opencohost.config import settings
from opencohost.core.context.owner_reflection import (
    FactCandidate,
    ReflectionRing,
    build_distillation_prompt,
    drop_ungrounded,
    is_question_or_request,
    owner_exchange_from_turn,
)
from tests.owner_reflection_support import (
    PROFILE,
    fact_json,
    install_stream,
    make_motor,
    owner_reflection_lines,
    run_job,
    spy_capture,
)

WAUTLA = "Puedes buscarme en la web. ¿Qué noticias hay últimamente en Wautla? Morelos. ¡Wautla!"
WAUTLA_FACT = ("Where does the streamer live?", "The streamer lives in Wautla, Morelos.")
FASTIDIA = "me fastidia todo lo administrativo y estudiar"
FASTIDIA_FACT = ("What does the owner dislike?", "The owner dislikes paperwork and studying.")
MIXED = "Vivo en Cuernavaca. ¿Qué noticias hay?"
BIRTHDAY = "quiero que recuerdes que mi cumpleaños es el 3 de mayo"


# ---------------------------------------------------------------------------
# the detector
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("text", [
    WAUTLA,
    "¿Dónde queda Wautla?",
    "Kira, ¿qué noticias hay?",
    "Puedes buscarme en la web",
    "Búscame el clima de mañana.",
    "Googlea quién ganó ayer",
    "Dime qué hora es.",
    "Explícame cómo funciona OBS.",
    "Muéstrame el chat.",
    "¿Podrías buscar el precio del jitomate? Dime cuánto cuesta.",
    "What's the news in Paris?",
    "Can you look up the weather in Tokyo?",
    "Search the web for tomato prices.",
    "Look up the score. Tell me who won!",
    "Could you google the release date?",
])
def test_questions_and_requests_are_detected(text):
    assert is_question_or_request(text) is True


@pytest.mark.parametrize("text", [
    FASTIDIA,
    MIXED,
    "Soy programador.",
    "My dog is called Rex.",
    "I work at Google.",
    "Trabajo en Google desde 2020.",
    "No puedes imaginar lo cansado que estoy.",
    "Remember that my dog is called Rex.",
    "",
    "   ",
])
def test_declaratives_and_mixed_exchanges_are_not_detected(text):
    assert is_question_or_request(text) is False


def test_remember_request_carries_a_fact_and_is_not_a_request():
    """A request to remember states the fact it asks to keep."""
    assert is_question_or_request(BIRTHDAY) is False
    ex = owner_exchange_from_turn("direct", False, 1, BIRTHDAY, "ok")
    assert ex.recall is False and ex.inquiry is False


# ---------------------------------------------------------------------------
# batch input
# ---------------------------------------------------------------------------
def _ring(*texts):
    ring = ReflectionRing(session_token="abcdef01")
    for text in texts:
        ring.offer("direct", False, text, "kira reply")
    return ring


def test_wautla_exchange_is_kept_in_the_ring_but_never_distilled():
    ex = owner_exchange_from_turn("direct", False, 1, WAUTLA, "kira reply")
    assert ex.inquiry is True and ex.recall is False

    ring = _ring(WAUTLA)

    assert len(ring.snapshot()) == 1
    assert ring.pending_batch() == ((), 0)
    assert ring.pending_count() == 0
    assert ring.owner_turns_since_reflection == 0


def test_inquiry_between_statements_is_excluded_not_evicted():
    ring = _ring(WAUTLA, FASTIDIA, MIXED, "Dime qué hora es.")

    batch, evicted = ring.pending_batch()

    assert [e.user_text for e in batch] == [FASTIDIA, MIXED]
    assert evicted == 0
    assert "Wautla" not in build_distillation_prompt(batch)

    ring.mark_distilled(batch[-1].seq)
    assert ring.pending_count() == 0 and ring.owner_turns_since_reflection == 0


# ---------------------------------------------------------------------------
# prompt
# ---------------------------------------------------------------------------
def test_prompt_forbids_facts_from_questions_requests_and_inference():
    prompt = build_distillation_prompt(_ring(FASTIDIA).pending_batch()[0])
    assert "Never turn a question or a request" in prompt
    assert "search the web" in prompt
    assert "says nothing about the owner's own life" in prompt
    assert "Never infer where the owner lives or works, or what they own" in prompt
    # existing rules and schema unchanged
    assert "Skip questions, jokes, requests and chit-chat." in prompt
    assert '{"facts":[{"q":"<short question the fact answers>"' in prompt


# ---------------------------------------------------------------------------
# grounding gate (pure)
# ---------------------------------------------------------------------------
def _batch(*texts):
    return tuple(owner_exchange_from_turn("direct", False, i, t, "k") for i, t in enumerate(texts, 1))


def test_gate_drops_a_fact_citing_only_inquiries_and_keeps_the_rest():
    batch = _batch(WAUTLA, FASTIDIA, "Dime qué hora es.")
    cands = [
        FactCandidate(question=WAUTLA_FACT[0], answer=WAUTLA_FACT[1], turns=(1,)),
        FactCandidate(question=WAUTLA_FACT[0], answer=WAUTLA_FACT[1], turns=(1, 3)),
        FactCandidate(question=FASTIDIA_FACT[0], answer=FASTIDIA_FACT[1], turns=(2,)),
        FactCandidate(question=FASTIDIA_FACT[0], answer=FASTIDIA_FACT[1], turns=(1, 2)),
    ]

    kept, dropped = drop_ungrounded(cands, batch)

    assert dropped == 2
    assert kept == [cands[2], cands[3]]


def test_gate_keeps_a_fact_citing_a_mixed_exchange():
    cand = FactCandidate(question="Where does the owner live?", answer="The owner lives in Cuernavaca.", turns=(1,))
    kept, dropped = drop_ungrounded([cand], _batch(MIXED))
    assert kept == [cand] and dropped == 0


# ---------------------------------------------------------------------------
# engine: no written fact from the Wautla batch, declaratives still written
# ---------------------------------------------------------------------------
def test_engine_wautla_batch_is_never_distilled(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "OWNER_REFLECTION_EVERY_N", 1)
    motor = make_motor(monkeypatch, tmp_path)
    motor._owner_reflection_ring().offer("direct", False, WAUTLA, "kira reply")
    spy = install_stream(monkeypatch, motor, fact_json((*WAUTLA_FACT, (1,))))
    calls = spy_capture(monkeypatch, motor)

    run_job(motor)

    assert spy.calls == []
    assert calls == []


def test_engine_gate_drops_a_fact_citing_the_wautla_turn(monkeypatch, tmp_path, caplog):
    """Even if the exchange reached the batch, a fact citing only it is not written."""
    motor = make_motor(monkeypatch, tmp_path)
    motor._owner_reflection_ring()
    install_stream(monkeypatch, motor, fact_json((*WAUTLA_FACT, (1,))))
    calls = spy_capture(monkeypatch, motor)
    token = motor._owner_reflection_control.begin()
    caplog.set_level(logging.INFO)

    motor._owner_reflection_job(token, _batch(WAUTLA), 0, 1, "m", PROFILE)

    assert calls == []
    lines = owner_reflection_lines(caplog)
    assert len(lines) == 1, lines
    assert "outcome=ok" in lines[0]
    assert "distilled=1" in lines[0] and "ungrounded=1" in lines[0] and "written=0" in lines[0]
    assert "Wautla" not in caplog.text


def test_engine_declarative_still_distils_and_is_written(monkeypatch, tmp_path, caplog):
    monkeypatch.setattr(settings, "OWNER_REFLECTION_EVERY_N", 1)
    motor = make_motor(monkeypatch, tmp_path)
    motor._owner_reflection_ring().offer("direct", False, FASTIDIA, "kira reply")
    spy = install_stream(monkeypatch, motor, fact_json((*FASTIDIA_FACT, (1,))))
    calls = spy_capture(monkeypatch, motor)
    caplog.set_level(logging.INFO)

    run_job(motor)

    assert spy.calls, "the job should have run"
    assert len(calls) == 1 and calls[0][0][3] == FASTIDIA_FACT[1]
    lines = owner_reflection_lines(caplog)
    assert len(lines) == 1, lines
    assert "ungrounded=0" in lines[0] and "written=1" in lines[0]


def test_engine_mixed_exchange_stays_eligible(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "OWNER_REFLECTION_EVERY_N", 1)
    motor = make_motor(monkeypatch, tmp_path)
    motor._owner_reflection_ring().offer("direct", False, MIXED, "kira reply")
    spy = install_stream(
        monkeypatch, motor,
        fact_json(("Where does the owner live?", "The owner lives in Cuernavaca.", (1,))),
    )
    calls = spy_capture(monkeypatch, motor)

    run_job(motor)

    assert spy.calls and "Cuernavaca" in repr(spy.calls[0])
    assert len(calls) == 1
