"""Active cards: prompt block, naming and seam (R1, R3)."""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace

import pytest

from opencohost.config import active_card_guard
from opencohost.core.context.prompt_assembler import PromptContextAssembler
from opencohost.core.editorial.editorial_agenda_bridge import EditorialAgendaBridge
from opencohost.core.editorial.editorial_cards import EditorialCard, EditorialCardStatus, EditorialCardStore
from opencohost.core.editorial.active_cards import ActiveCardService, render_active_block

# B-W4: this module pins the legacy prompt layout (system-message placement, one-pair
# history deque, legacy goldens). The v2 placement is tested in tests/test_prompt_layout_v2.py.
pytestmark = pytest.mark.usefixtures("legacy_prompt_layout")


@pytest.fixture(autouse=True)
def _clean_guard():
    # The service keeps the process-wide forbidden-claim guard in step with
    # the active set; reset it so these tests never leak enforcement elsewhere.
    active_card_guard.clear()
    yield
    active_card_guard.clear()


def _store(tmp_path) -> EditorialCardStore:
    return EditorialCardStore(tmp_path / "cards.db")


def _sponsor(store, topic="Acme Mic", **kw):
    card = store.upsert(
        EditorialCard(
            topic=topic,
            summary="Acme Mic is a USB microphone.",
            streamer_take="I use it daily.",
                        must_say=["costs 49 USD"],
            forbidden_claims=["cures anxiety"],
            discount_code="KIRA10",
            link="https://www.acme.example/kira",
            **{"triggers": ["cuesta"], **kw},
        )
    )
    return card


def _service(store, lang="es"):
    return ActiveCardService(store, lang_reader=lambda: lang)


def _assemble(source="direct", text="cuanto cuesta?", **kw):
    setup = PromptContextAssembler().assemble(
        text,
        source,
        system_prompt="Eres Kira.",
        use_system_role=True,
        is_local=True,
        provider_cfg={},
        request_model="llama3",
        history_snapshot=[],
        **kw,
    )
    return setup.messages[0]["content"], setup


def test_inactive_service_returns_empty_and_prompt_is_byte_identical(tmp_path) -> None:
    store = _store(tmp_path)
    _sponsor(store)  # exists, but not active
    svc = _service(store)
    assert svc("cuanto cuesta?", "direct", "m") == ""
    base, base_setup = _assemble()
    got, got_setup = _assemble(active_card_context=svc)
    assert got == base
    assert got_setup.messages == base_setup.messages
    absent, _ = _assemble(active_card_context=lambda t, s, m: "")
    assert absent == base


def test_block_has_facts_instruction_and_link_without_scheme(tmp_path) -> None:
    store = _store(tmp_path)
    card = _sponsor(store)
    store.activate(card.id)
    block = _service(store)("cuanto cuesta", "direct", "m")
    assert block.startswith("<active_card>") and block.endswith("</active_card>")
    body = json.loads(block.split("\n", 1)[1].rsplit("\n", 1)[0])
    assert "product" not in body
    assert body["topic"] == "Acme Mic"
    assert body["code"] == "KIRA10"
    assert body["must_say"] == ["costs 49 USD"]
    assert body["link"] == "acme.example/kira"
    assert "https://" not in block and "www." not in block
    assert "no tengo ese dato" in body["instruction"]
    assert "patrocinador" not in block.lower() and "sponsor" not in block.lower()
    assert body["never_say"] == ["cures anxiety"]


def test_instruction_follows_prompt_language(tmp_path) -> None:
    store = _store(tmp_path)
    card = _sponsor(store)
    store.activate(card.id)
    block = _service(store, lang="en")("cuanto cuesta", "direct", "m")
    assert "do not have that data" in block
    assert "no tengo ese dato" not in block


def test_empty_optional_fields_are_omitted() -> None:
    card = EditorialCard(topic="T", summary="S", streamer_take="X")
    body = json.loads(render_active_block(card, "es").split("\n", 1)[1].rsplit("\n", 1)[0])
    assert set(body) == {"instruction", "topic", "summary", "streamer_take"}


SOURCES = ["direct", "ptt", "chat", "kira-agenda", "kira-agenda-x", "owner-bundle"]


@pytest.mark.parametrize("source", SOURCES)
def test_relevant_turn_gets_the_card_on_every_source(tmp_path, source) -> None:
    store = _store(tmp_path)
    card = _sponsor(store)
    store.activate(card.id)
    content, _ = _assemble(source=source, text="cuanto cuesta", active_card_context=_service(store))
    assert content.count("<active_card>") == 1
    assert content.endswith("</active_card>")


@pytest.mark.parametrize("source", SOURCES)
def test_off_topic_turn_gets_no_card_and_the_prompt_is_unchanged(tmp_path, source) -> None:
    store = _store(tmp_path)
    card = _sponsor(store)
    store.activate(card.id)
    base, _ = _assemble(source=source, text="háblame de animales")
    got, _ = _assemble(source=source, text="háblame de animales", active_card_context=_service(store))
    assert got == base  # the old every-turn injection is gone


def test_activation_does_not_consume_across_many_turns(tmp_path) -> None:
    store = _store(tmp_path)
    card = _sponsor(store, single_use=True)
    store.arm(card.id)
    store.activate(card.id)
    svc = _service(store)
    for _ in range(5):
        assert svc("cuanto cuesta", "direct", "m")
    after = store.get(card.id)
    assert after.use_count == 0 and after.status.value == "armed"


def test_turn_logs_one_line_with_id_and_no_text(tmp_path, caplog) -> None:
    store = _store(tmp_path)
    card = _sponsor(store)
    store.activate(card.id)
    svc = _service(store)
    with caplog.at_level(logging.INFO, logger="OpenCohost"):
        svc("super secret viewer question about cuesta", "chat", "m")
        svc("super secret viewer question", "chat", "m")
    lines = [r.getMessage() for r in caplog.records if "[ACTIVE_CARD]" in r.getMessage()]
    assert lines == [f"[ACTIVE_CARD] matched card={card.id} via=lexical score=0.90", "[ACTIVE_CARD] none"]
    assert "secret" not in caplog.text and "49 USD" not in caplog.text


def test_store_failure_is_fail_open(tmp_path) -> None:
    class Boom:
        def list_active(self):
            raise RuntimeError("db gone")

    assert ActiveCardService(Boom(), lang_reader=lambda: "es")("x", "direct", "m") == ""


def test_active_card_is_not_duplicated_by_the_normal_editorial_paths(tmp_path) -> None:
    store = _store(tmp_path)
    card = _sponsor(store, triggers=["microfono", "cuesta"])
    store.arm(card.id)
    bridge = EditorialAgendaBridge(store, controller=SimpleNamespace())
    assert bridge.resolve_direct_context("cuanto cuesta el microfono acme mic") is not None
    store.activate(card.id)
    assert bridge.resolve_direct_context("cuanto cuesta el microfono acme mic") is None
    assert bridge._pending_direct_card_id is None
    active_card = store.get(card.id)
    assert active_card.active is True
    # agenda-attached rendering also skips an active card
    store._set_status(card.id, EditorialCardStatus.ACTIVE)
    assert bridge.resolve_prompt_block(card.id) is None


def test_engine_host_arms_active_card_service_on_the_motor(tmp_path, monkeypatch) -> None:
    import opencohost.api.engine_host as engine_host_mod
    from opencohost.api.engine_host import EngineHost

    monkeypatch.setattr(engine_host_mod, "EDITORIAL_CARDS_DB", str(tmp_path / "cards.db"))
    motor = SimpleNamespace()
    host = EngineHost.__new__(EngineHost)
    host.motor = motor
    host.agenda = None
    host.agenda_lock = None
    host._wire_editorial_bridge()
    assert isinstance(motor.active_card_service, ActiveCardService)
    # The wired lang reader must work end to end (a failure is swallowed
    # fail-open, so assert on a real block).
    store = EditorialCardStore(tmp_path / "cards.db")
    card = _sponsor(store)
    store.activate(card.id)
    assert "<active_card>" in motor.active_card_service("cuanto cuesta", "direct", "m")


def test_active_card_past_its_expiry_is_no_longer_injected_or_enforced(tmp_path) -> None:
    from datetime import datetime, timedelta, timezone

    store = _store(tmp_path)
    card = _sponsor(store, expires_at=datetime.now(timezone.utc) + timedelta(hours=1))
    store.arm(card.id)
    assert store.activate(card.id)
    svc = _service(store)
    assert svc("cuanto cuesta", "direct", "m")
    assert active_card_guard.check("esto cures anxiety")

    # The validity window ends while the card is still active.
    store.upsert(replace_expiry(store.get(card.id), datetime.now(timezone.utc) - timedelta(minutes=1)))
    assert store.get(card.id).active

    assert svc("cuanto cuesta", "direct", "m") == ""
    assert not active_card_guard.check("esto cures anxiety")


def replace_expiry(card, when):
    from dataclasses import replace

    return replace(card, expires_at=when)


def test_several_active_cards_are_each_rendered_up_to_top_k(tmp_path, monkeypatch) -> None:
    from opencohost.config import settings

    monkeypatch.setattr(settings, "ACTIVE_CARD_TOP_K", 2)
    store = _store(tmp_path)
    a = _sponsor(store, "Acme Mic")
    b = _sponsor(store, "Beta Cam")
    assert store.activate(a.id) and store.activate(b.id)
    block = _service(store)("cuanto cuesta", "direct", "m")
    assert block.count("<active_card>") == 2
    assert "Acme Mic" in block and "Beta Cam" in block
