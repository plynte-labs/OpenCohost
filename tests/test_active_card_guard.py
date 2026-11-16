"""Active cards: forbidden-claim output guard."""

from __future__ import annotations

import logging

import pytest

from opencohost.config import active_card_guard
from opencohost.config.validation import output_guard
from opencohost.core.context.generation_evaluator import guard_rule_id, output_guard_with_tts_check
from opencohost.core.editorial.editorial_cards import EditorialCard, EditorialCardStore
from opencohost.core.editorial.active_cards import ActiveCardService, sync_guard_from_store


@pytest.fixture(autouse=True)
def _clean_guard():
    active_card_guard.clear()
    yield
    active_card_guard.clear()


def _store(tmp_path, forbidden=("cura la ansiedad", "Garantía de por vida")) -> tuple[EditorialCardStore, EditorialCard]:
    store = EditorialCardStore(tmp_path / "cards.db")
    card = store.upsert(
        EditorialCard(
            topic="Acme Mic",
            summary="USB microphone.",
            streamer_take="I like it.",
            forbidden_claims=list(forbidden),
        )
    )
    return store, card


SAMPLES = [
    "Hola, que tal el stream de hoy.",
    "Este microfono cura la ansiedad, te lo juro.",
    "Tiene garantia de por vida.",
    "Estoy segura de que va a pasar.",
]


def test_inactive_guard_is_unchanged() -> None:
    baseline = [output_guard(s, source="direct") for s in SAMPLES]
    active_card_guard.set_cards([("ec_x", ["cura la ansiedad"])])
    active_card_guard.clear()
    assert [output_guard(s, source="direct") for s in SAMPLES] == baseline
    assert baseline[1][0] is True  # not blocked by anything else, so the active card is what changes it


def test_forbidden_phrase_blocks_case_and_accent_insensitive() -> None:
    active_card_guard.set_cards([("ec_x", ["cura la ansiedad", "Garantía de por vida"])])
    for text in (
        "Este microfono CURA la ansiedad, te lo juro.",
        "Tiene garantia de por vida.",
        "Tiene GARANTÍA   de por, vida!",
    ):
        allowed, reason = output_guard(text, source="chat")
        assert allowed is False, text
        assert guard_rule_id(reason) == "active_card_forbidden_claim"
    assert output_guard("Hola, que tal el stream de hoy.", source="chat")[0] is True


def test_phrase_matches_whole_words_only() -> None:
    active_card_guard.set_cards([("ec_x", ["cura"])])
    assert output_guard("Es una curacion rapida.", source="chat")[0] is True
    assert output_guard("Eso cura todo.", source="chat")[0] is False


def test_blank_phrases_never_block_everything() -> None:
    active_card_guard.set_cards([("ec_x", ["", "  ", "!!!"])])
    assert output_guard("Hola, que tal.", source="chat")[0] is True


@pytest.mark.parametrize("source", ["direct", "ptt", "chat", "kira-agenda"])
def test_applies_on_every_source(source) -> None:
    active_card_guard.set_cards([("ec_x", ["cura la ansiedad"])])
    assert output_guard("cura la ansiedad", source=source)[0] is False


def test_streamed_sentence_and_buffered_paths_both_block() -> None:
    active_card_guard.set_cards([("ec_x", ["cura la ansiedad"])])
    # Streaming guards each sentence, buffered guards the whole reply; both go
    # through output_guard_with_tts_check (raw and tts-sanitized text).
    assert output_guard_with_tts_check("Dicen que *cura la ansiedad*.", source="direct")[0] is False
    assert output_guard_with_tts_check("Hola. Dicen que cura la ansiedad. Chau.", source="direct")[0] is False
    assert output_guard_with_tts_check("Hola. Chau.", source="direct")[0] is True


def test_block_log_has_card_id_only(caplog) -> None:
    active_card_guard.set_cards([("ec_card1", ["cura la ansiedad"])])
    with caplog.at_level(logging.INFO, logger="OpenCohost"):
        output_guard("el viewer privado dijo que cura la ansiedad", source="chat")
    lines = [r.getMessage() for r in caplog.records if "[ACTIVE_CARD]" in r.getMessage()]
    assert lines and all(l == "[ACTIVE_CARD] blocked rule=forbidden_claim card=ec_card1" for l in lines)
    assert "viewer" not in caplog.text and "ansiedad" not in caplog.text


def test_engine_wiring_uses_the_same_guard_function() -> None:
    import opencohost.core.llm_engine as eng
    from opencohost.config import validation

    assert eng.output_guard is validation.output_guard


def test_service_turn_syncs_guard_with_active_state(tmp_path) -> None:
    store, card = _store(tmp_path)
    svc = ActiveCardService(store, lang_reader=lambda: "es")
    svc("hola", "direct", "m")
    assert output_guard("cura la ansiedad", source="chat")[0] is True  # not active
    store.activate(card.id)
    svc("hola", "direct", "m")
    assert output_guard("cura la ansiedad", source="chat")[0] is False
    store.deactivate(card.id)
    svc("hola", "direct", "m")
    assert output_guard("cura la ansiedad", source="chat")[0] is True


def test_sync_guard_from_store_is_immediate_and_fail_keeps_last_state(tmp_path) -> None:
    store, card = _store(tmp_path)
    store.activate(card.id)
    sync_guard_from_store(store)
    assert active_card_guard.active_card_ids() == (card.id,)
    store.deactivate(card.id)
    sync_guard_from_store(store)
    assert active_card_guard.active_card_ids() == ()

    store.activate(card.id)
    sync_guard_from_store(store)

    class Boom:
        def list_active(self):
            raise RuntimeError("db gone")

    sync_guard_from_store(Boom())
    assert active_card_guard.active_card_ids() == (card.id,)


def test_retry_nudge_for_active_card_rule_is_the_generic_one() -> None:
    from opencohost.core.llm_engine import _guardrail_retry_nudge_for_reason
    from opencohost.i18n import active as i18n_active

    active_card_guard.set_cards([("ec_x", ["cura la ansiedad"])])
    _, reason = output_guard("cura la ansiedad", source="chat")
    # K4: nudges are locale data now; the active locale's generic entry.
    assert _guardrail_retry_nudge_for_reason(reason) == i18n_active.guardrail_retry_nudge("generic")


# --- every active card is enforced, whether or not it was injected (R4) --------


def _two_cards(tmp_path):
    store = EditorialCardStore(tmp_path / "cards.db")
    a = store.upsert(
        EditorialCard(
            topic="Acme Mic", summary="USB microphone.", streamer_take="I like it.",
            triggers=["microfono"], forbidden_claims=["cura la ansiedad"],
        )
    )
    b = store.upsert(
        EditorialCard(
            topic="Hades II", summary="A game.", streamer_take="Great.",
            triggers=["hades"], forbidden_claims=["gratis para siempre"],
        )
    )
    assert store.activate(a.id) and store.activate(b.id)
    return store, a, b


def test_forbidden_phrases_of_every_active_card_hold_on_a_turn_that_injects_none(tmp_path) -> None:
    store, a, b = _two_cards(tmp_path)
    svc = ActiveCardService(store, lang_reader=lambda: "es")
    assert svc("háblame de animales", "chat", "m") == ""  # nothing injected
    assert output_guard("el micro cura la ansiedad", source="chat")[0] is False
    assert output_guard("hades es gratis para siempre", source="chat")[0] is False
    assert output_guard("hola que tal", source="chat")[0] is True


def test_enforcement_does_not_depend_on_top_k_or_on_which_card_was_injected(tmp_path, monkeypatch) -> None:
    from opencohost.config import settings

    monkeypatch.setattr(settings, "ACTIVE_CARD_TOP_K", 1)
    store, a, b = _two_cards(tmp_path)
    svc = ActiveCardService(store, lang_reader=lambda: "es")
    assert "Acme Mic" in svc("que microfono usas", "chat", "m")  # only card A injected
    assert output_guard("gratis para siempre", source="chat")[0] is False  # B still enforced


def test_block_log_names_the_card_that_owns_the_phrase(tmp_path, caplog) -> None:
    store, a, b = _two_cards(tmp_path)
    ActiveCardService(store, lang_reader=lambda: "es")
    with caplog.at_level(logging.INFO, logger="OpenCohost"):
        output_guard("es gratis para siempre", source="chat")
        output_guard("cura la ansiedad", source="chat")
    lines = [r.getMessage() for r in caplog.records if "[ACTIVE_CARD] blocked" in r.getMessage()]
    assert lines == [
        f"[ACTIVE_CARD] blocked rule=forbidden_claim card={b.id}",
        f"[ACTIVE_CARD] blocked rule=forbidden_claim card={a.id}",
    ]


def test_enforcement_survives_a_retrieval_failure(tmp_path, monkeypatch) -> None:
    import opencohost.core.memory.memoria_store as memoria_store

    store, a, b = _two_cards(tmp_path)
    svc = ActiveCardService(store, lang_reader=lambda: "es")
    active_card_guard.clear()  # as if the guard had been dropped between turns

    def boom(text):
        raise RuntimeError("cleaning failed")

    monkeypatch.setattr(memoria_store, "strip_history_wrapper", boom)
    assert svc("que microfono usas", "chat", "m") == ""  # fail-open prompt side
    assert output_guard("cura la ansiedad", source="chat")[0] is False  # guard side still synced


def test_an_expired_card_drops_out_while_the_other_keeps_being_enforced(tmp_path) -> None:
    from datetime import datetime, timedelta, timezone
    from dataclasses import replace

    store, a, b = _two_cards(tmp_path)
    svc = ActiveCardService(store, lang_reader=lambda: "es")
    store.upsert(replace(store.get(a.id), expires_at=datetime.now(timezone.utc) - timedelta(minutes=1)))
    svc("hola", "chat", "m")
    assert output_guard("cura la ansiedad", source="chat")[0] is True
    assert output_guard("gratis para siempre", source="chat")[0] is False


def test_cards_without_forbidden_phrases_enforce_nothing_and_do_not_disturb_others(tmp_path) -> None:
    store, a, b = _two_cards(tmp_path)
    plain = store.upsert(EditorialCard(topic="Plain", summary="S", streamer_take="T"))
    store.activate(plain.id)
    sync_guard_from_store(store)
    assert set(active_card_guard.active_card_ids()) == {a.id, b.id}
    assert output_guard("hola que tal", source="chat")[0] is True


def test_no_active_card_leaves_the_guard_exactly_as_before(tmp_path) -> None:
    baseline = [output_guard(s, source="direct") for s in SAMPLES]
    store, a, b = _two_cards(tmp_path)
    svc = ActiveCardService(store, lang_reader=lambda: "es")
    store.deactivate(a.id)
    store.deactivate(b.id)
    svc("hola", "direct", "m")
    assert active_card_guard.active_card_ids() == ()
    assert [output_guard(s, source="direct") for s in SAMPLES] == baseline


def test_engine_prompt_assembly_arms_the_guard_even_on_an_off_topic_turn(monkeypatch, tmp_path) -> None:
    from tests.test_memory_fact_semantic_rank import FACT_PET, _prompt, _semantic_motor

    motor, *_ = _semantic_motor(monkeypatch, tmp_path / "m", [{"id": "pet", "content": FACT_PET}])
    store, a, b = _two_cards(tmp_path)
    active_card_guard.clear()
    motor.active_card_service = ActiveCardService(store, lang_reader=lambda: "es")
    active_card_guard.clear()
    prompt = _prompt(motor, "zqunrelated charla sobre otra cosa")
    assert "<active_card>" not in prompt
    assert output_guard("cura la ansiedad", source="direct")[0] is False
