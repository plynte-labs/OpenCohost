"""T8 (memory-grounding-and-recall-budget-20261016): ADR-065 distillation input rule.

Distillation takes the owner's text only, and recall exchanges (explicit
questions about earlier conversation) are excluded from its input, so a
confabulated Kira answer can never feed a distilled draft. The explicit
"remember that ..." request stays eligible: it states a fact.
All text is synthetic.
"""
from __future__ import annotations

import pytest

from opencohost.core.context.owner_reflection import (
    ReflectionRing,
    build_distillation_prompt,
    owner_exchange_from_turn,
)
from tests.owner_reflection_support import (
    fact_json,
    install_stream,
    make_motor,
    run_job,
)

RECALL_Q = "te acuerdas del zqtesla que comentamos la sesión pasada"
KIRA_INVENTED = "KIRAINVENTEDSENTINEL claro, un zqtesla rojo con ocho mil kilómetros"
FACT = "my dog is called Rex and he loves tomatoes"


def _ring(*pairs):
    ring = ReflectionRing(session_token="abcdef01")
    for user, kira in pairs:
        ring.offer("direct", False, user, kira)
    return ring


def test_recall_exchange_excluded():
    ring = _ring((FACT, "nice"), (RECALL_Q, KIRA_INVENTED), ("I stream on tuesdays and fridays", "ok"))

    batch, evicted = ring.pending_batch()

    assert [e.user_text for e in batch] == [FACT, "I stream on tuesdays and fridays"]
    assert evicted == 0
    prompt = build_distillation_prompt(batch)
    assert "zqtesla" not in prompt and "KIRAINVENTEDSENTINEL" not in prompt


def test_prompt_never_carries_kira_text_even_for_recall_exchanges():
    ring = _ring((RECALL_Q, KIRA_INVENTED), (FACT, "KIRAREPLYSENTINEL"))
    prompt = build_distillation_prompt(ring.pending_batch()[0])
    assert "KIRAINVENTEDSENTINEL" not in prompt and "KIRAREPLYSENTINEL" not in prompt


def test_recall_only_ring_is_never_due_and_has_nothing_pending():
    ring = _ring((RECALL_Q, KIRA_INVENTED), ("¿qué recuerdas de mí?", "mucho"))
    assert ring.pending_count() == 0
    assert ring.owner_turns_since_reflection == 0
    assert not ring.due(1)
    assert ring.pending_batch() == ((), 0)


def test_trailing_recall_exchange_does_not_hold_the_counter_up():
    ring = _ring((FACT, "nice"), (RECALL_Q, KIRA_INVENTED))
    batch, _ = ring.pending_batch()
    assert len(batch) == 1

    ring.mark_distilled(batch[-1].seq, len(batch))

    assert ring.pending_count() == 0
    assert ring.owner_turns_since_reflection == 0


def test_recall_exchange_between_facts_is_not_counted_as_evicted():
    ring = _ring((RECALL_Q, KIRA_INVENTED), (FACT, "nice"))
    batch, evicted = ring.pending_batch()
    assert [e.user_text for e in batch] == [FACT]
    assert evicted == 0


@pytest.mark.parametrize("text", [
    "quiero que recuerdes que mi cumpleaños es el 3 de mayo",
    "recuerda que mañana tengo médico",
    "remember that my dog is called Rex",
])
def test_explicit_remember_request_stays_eligible(text):
    ex = owner_exchange_from_turn("direct", False, 1, text, "ok")
    assert ex is not None and ex.recall is False


def test_recall_question_is_flagged_on_the_exchange():
    assert owner_exchange_from_turn("direct", False, 1, RECALL_Q, "x").recall is True
    assert owner_exchange_from_turn("direct", False, 1, FACT, "x").recall is False


def test_engine_run_distills_only_non_recall_exchanges(monkeypatch, tmp_path):
    motor = make_motor(monkeypatch, tmp_path)
    ring = motor._owner_reflection_ring()
    for i in range(8):
        ring.offer("direct", False, f"owner statement number {i} about gardening tomatoes", f"kira {i}")
    ring.offer("ptt", False, RECALL_Q, KIRA_INVENTED)
    spy = install_stream(
        monkeypatch, motor,
        fact_json(("What does the owner grow?", "The owner grows tomatoes.", (1,))),
    )

    run_job(motor)

    assert spy.calls, "the job should have run"
    sent = repr(spy.calls[0])
    assert "zqtesla" not in sent and "KIRAINVENTEDSENTINEL" not in sent
