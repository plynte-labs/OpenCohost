"""self-knowledge K2: service composition, audience rules, log line, R9 wording."""
import logging
from datetime import datetime
from types import SimpleNamespace

import pytest

from opencohost.config.validation import output_guard
from opencohost.core.self_knowledge.providers import (
    CapabilitiesProvider,
    ClockProvider,
    CommandsProvider,
    CreatorProvider,
    GpuInfo,
    GpuProvider,
    IdentityCard,
    IdentityProvider,
    InterlocutorProvider,
    ModelProvider,
)
from opencohost.core.self_knowledge.facts import SelfKnowledgeContext
from opencohost.core.self_knowledge.service import SelfKnowledgeService
from opencohost.core.self_knowledge.texts import t

NOW = datetime(2026, 10, 4, 14, 32)
GENDER_EN = t("en")["gender"]
GENDER_ES = t("es")["gender"]
CARD = IdentityCard(creator="acme", repository="https://example.test/r", description="A co-host.")


def _service(enabled=True, viewers=False, gpu=None, lang="en", memory=None):
    settings = SimpleNamespace(enabled=enabled, viewers=viewers)
    providers = [
        ClockProvider(now=lambda: NOW),
        ModelProvider(),
        GpuProvider(reader=gpu or (lambda: GpuInfo("RTX 5060", 8192, 3072))),
        IdentityProvider(card_reader=lambda: CARD),
        CapabilitiesProvider(
            vision_enabled=lambda: True,
            tts_label=lambda: "edge",
            memory_enabled=None if memory is None else (lambda: memory),
        ),
        CommandsProvider(commands=lambda: ("/mira",)),
        CreatorProvider(card_reader=lambda: CARD),
        InterlocutorProvider(owner_name=lambda: "Tavo"),
    ]
    return SelfKnowledgeService(
        providers=providers,
        settings_reader=lambda: settings,
        lang_reader=lambda: lang,
    )


def _records(caplog):
    return [r.getMessage() for r in caplog.records if "[SELF_KNOWLEDGE]" in r.getMessage()]


def test_owner_turn_without_intent_gets_only_the_time_line_and_no_log(caplog):
    caplog.set_level(logging.INFO)
    block = _service()("cuéntame un chiste", "direct", request_model="m")
    assert "2026-10-04" in block and "14:32" in block
    assert "SELF-KNOWLEDGE" not in block and "RTX" not in block
    assert _records(caplog) == []


def test_owner_gated_turn_injects_facts_and_logs_one_line_without_query(caplog):
    caplog.set_level(logging.INFO)
    block = _service()("what model are you and how much vram is free", "ptt", request_model="qwopus:9b")
    assert "qwopus:9b" in block and "RTX 5060" in block and "3.0 GB" in block
    assert "14:32" in block  # time line rides along on every owner turn
    lines = _records(caplog)
    assert lines == ["[SELF_KNOWLEDGE] topics=model,gpu audience=owner facts=3 withheld=0"]
    assert "vram" not in lines[0].lower() and "what model" not in lines[0]


def test_time_question_does_not_duplicate_the_time_line():
    block = _service()("what time is it", "direct", request_model="m")
    assert block.count("14:32") == 1


def test_master_off_returns_empty_for_every_source(caplog):
    caplog.set_level(logging.INFO)
    svc = _service(enabled=False, viewers=True)
    for source in ("direct", "ptt", "chat"):
        assert svc("what time is it", source, request_model="m") == ""
    assert _records(caplog) == []


def test_viewer_toggle_off_returns_empty_and_touches_nothing(caplog):
    caplog.set_level(logging.INFO)
    assert _service(viewers=False)("who made you", "chat", request_model="m") == ""
    assert _records(caplog) == []


def test_viewer_toggle_on_gets_only_viewer_facts_never_model_gpu_commands(caplog):
    caplog.set_level(logging.INFO)
    svc = _service(viewers=True)
    block = svc("who made you, what model are you, vram, commands", "chat", request_model="qwopus:9b")
    assert "acme" in block and "example.test/r" in block and "https://" not in block
    for secret in ("qwopus", "RTX", "VRAM", "/mira", "Tavo", "edge"):
        assert secret not in block
    assert _records(caplog) == [
        "[SELF_KNOWLEDGE] topics=model,gpu,commands,creator audience=viewer facts=2 withheld=3"
    ]


def test_viewer_capabilities_are_generic_only():
    block = _service(viewers=True)("what can you do", "chat", request_model="m")
    assert "locally" in block and "edge" not in block and "Vision is" not in block


def test_viewer_asking_only_owner_topics_gets_no_owner_facts_but_is_logged(caplog):
    caplog.set_level(logging.INFO)
    block = _service(viewers=True)("what model are you", "chat", request_model="m")
    assert block == GENDER_EN  # only the always-on gender line, no owner fact
    assert _records(caplog) == ["[SELF_KNOWLEDGE] topics=model audience=viewer facts=0 withheld=1"]


def test_unknown_sources_are_ignored():
    svc = _service(viewers=True)
    assert svc("what time is it", "kira-agenda", request_model="m") == ""


def test_provider_failure_does_not_break_the_turn():
    def boom():
        raise RuntimeError("pynvml missing")

    block = _service(gpu=boom)("how much vram", "direct", request_model="m")
    assert "14:32" in block and "RTX" not in block


def test_settings_reader_failure_means_off():
    def boom():
        raise OSError("disk")

    svc = SelfKnowledgeService(providers=[], settings_reader=boom, lang_reader=lambda: "en")
    assert svc("what time is it", "direct", request_model="m") == ""


@pytest.mark.parametrize("lang", ["es", "en"])
@pytest.mark.parametrize("source", ["direct", "chat"])
def test_block_wording_does_not_trip_guardrail_r9(lang, source):
    svc = _service(viewers=True, lang=lang)
    block = svc(
        "who are you, what can you do, who made you, what time is it, what model, commands",
        source,
        request_model="qwopus:9b",
    )
    assert block
    for source_name in ("direct", "chat"):
        allowed, reason = output_guard(block, source_name)
        assert allowed, reason


def test_language_follows_the_reader():
    block = _service(lang="es")("¿qué hora es?", "direct", request_model="m")
    assert "domingo" in block


def test_default_wiring_reads_live_settings_and_degrades_without_gpu(tmp_path, monkeypatch):
    import json

    from opencohost.config import self_knowledge_settings as sks
    from opencohost.core.self_knowledge import wiring

    path = str(tmp_path / "sk.json")
    monkeypatch.setattr(sks, "SELF_KNOWLEDGE_FILE", path)
    sks.reset_cache()
    svc = wiring.build_default_service(
        vision_enabled=lambda: False,
        tts_label=lambda: "piper",
        gpu_reader=lambda: None,
        now=lambda: NOW,
    )
    assert "14:32" in svc("hola", "direct", "m")
    assert "VRAM" not in svc("cuánta vram", "direct", "m")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"enabled": False}, f)
    assert svc("qué hora es", "direct", "m") == ""


def test_read_gpu_info_returns_none_when_pynvml_fails(monkeypatch):
    import sys

    from opencohost.core.self_knowledge import wiring

    monkeypatch.setitem(sys.modules, "pynvml", None)  # import raises ImportError
    assert wiring.read_gpu_info() is None


# --- K7: grammatical gender -------------------------------------------------

def test_gender_line_is_feminine_and_in_character():
    es = GENDER_ES.lower()
    assert "femenino" in es and "misma" in es and "lista" in es and "segura" in es
    assert "feminine" in GENDER_EN.lower()
    for text in (GENDER_ES, GENDER_EN):
        for name in ("direct", "chat"):
            allowed, reason = output_guard(text, name)
            assert allowed, reason


def test_gender_line_scopes_to_kira_and_never_assumes_the_interlocutors_gender():
    # follow-reading-pronunciation R2: Kira addressed the owner in the
    # feminine; the line must say it is about Kira only.
    es = GENDER_ES.lower()
    assert "solo a ti" in es
    assert "no asumas el género" in es and "neutr" in es
    en = GENDER_EN.lower()
    assert "only to yourself" in en
    assert "never assume the gender" in en and "neutral" in en
    for text in (GENDER_ES, GENDER_EN):
        for name in ("direct", "chat"):
            allowed, reason = output_guard(text, name)
            assert allowed, reason


@pytest.mark.parametrize("lang,line", [("es", GENDER_ES), ("en", GENDER_EN)])
def test_owner_turn_without_intent_carries_gender_and_time(lang, line):
    block = _service(lang=lang)("cuéntame un chiste", "direct", request_model="m")
    assert line in block and "14:32" in block
    assert "SELF-KNOWLEDGE" not in block and "AUTOCONOCIMIENTO" not in block


def test_owner_gated_turn_carries_gender_once():
    block = _service()("what model are you", "ptt", request_model="qwopus:9b")
    assert block.count(GENDER_EN) == 1 and "qwopus:9b" in block


def test_viewer_turn_carries_gender_when_toggle_on_even_without_intent():
    block = _service(viewers=True)("hello there", "chat", request_model="m")
    assert GENDER_EN in block
    assert "14:32" not in block  # viewers never get the owner ambient time line


def test_viewer_toggle_off_has_no_gender_line():
    assert _service(viewers=False)("hello there", "chat", request_model="m") == ""


def test_master_off_has_no_gender_line():
    svc = _service(enabled=False, viewers=True)
    for source in ("direct", "ptt", "chat"):
        assert svc("hello", source, request_model="m") == ""


def test_identity_fact_mentions_feminine_reference():
    # live-session-fixes L4: the gender rule lives only in the grammar line,
    # outside the facts list; the identity fact no longer repeats it.
    [fact] = IdentityProvider(card_reader=lambda: CARD).facts(SelfKnowledgeContext("owner", "direct", lang="es"))
    assert "femenino" not in fact.text.lower()
    [fact_en] = IdentityProvider(card_reader=lambda: CARD).facts(SelfKnowledgeContext("owner", "direct", lang="en"))
    assert "feminine" not in fact_en.text.lower()


ANTI_ES = t("es")["no_invention"]
ANTI_EN = t("en")["no_invention"]


@pytest.mark.parametrize("text", ["quién es tu creador", "¿quién eres?", "te inventaste el modelo", "who made you"])
def test_owner_creator_identity_model_turns_carry_model_fact_and_anti_invention(text):
    block = _service(lang="es")(text, "direct", request_model="qwen3.8:9b")
    assert "qwen3.8:9b" in block
    assert ANTI_ES in block


def test_anti_invention_line_is_localized_and_not_a_fact():
    block = _service(lang="en")("who made you", "direct", request_model="m1")
    assert ANTI_EN in block and "m1" in block
    assert ANTI_ES not in block


def test_anti_invention_absent_without_model_creator_identity_topics():
    block = _service()("what time is it", "direct", request_model="m1")
    assert ANTI_EN not in block
    assert ANTI_EN not in _service()("cuéntame un chiste", "direct", request_model="m1")


def test_viewer_never_gets_model_fact_even_with_creator_or_identity():
    svc = _service(viewers=True, lang="en")
    for text in ("who made you", "who are you", "what model are you"):
        block = svc(text, "chat", request_model="secret-model:7b")
        assert "secret-model" not in block, text
        assert "language model that powers you" not in block, text


def test_master_off_is_byte_identical_empty():
    assert _service(enabled=False)("who made you", "direct", request_model="m") == ""


def test_owner_model_topic_not_duplicated():
    block = _service(lang="en")("what model are you", "direct", request_model="m1")
    assert block.count("language model that powers you under the hood is m1;") == 1


@pytest.mark.parametrize("lang", ["es", "en"])
def test_anti_invention_wording_does_not_trip_guardrail_r9(lang):
    for name in ("direct", "ptt"):
        allowed, reason = output_guard(t(lang)["no_invention"], name)
        assert allowed, reason


@pytest.mark.parametrize("text", ["what model are you", "who created you", "who are you"])
@pytest.mark.parametrize("memory,key", [(True, "cap_memory_on"), (False, "cap_memory_off")])
def test_owner_identity_turns_carry_the_memory_fact_only(text, memory, key):
    # Live 2026-10-05: asked for her model, Kira claimed she had no access to
    # past conversations while memory was on; identity answers pad with
    # limitations, so the real memory state rides along for the owner.
    block = _service(memory=memory)(text, "direct", request_model="m")
    assert t("en")[key] in block
    assert t("en")["cap_vision_on"] not in block
    assert t("en")["cap_generic"] not in block


def test_viewer_identity_turn_never_gets_the_memory_fact():
    block = _service(viewers=True, memory=True)("who created you", "chat", request_model="m")
    assert t("en")["cap_memory_on"] not in block


def test_owner_identity_turn_without_memory_reader_adds_nothing():
    block = _service()("what model are you", "direct", request_model="m")
    assert t("en")["cap_memory_on"] not in block and t("en")["cap_memory_off"] not in block


@pytest.mark.parametrize("lang,phrase", [("en", "do not list things you cannot do"), ("es", "no enumeres lo que no puedes hacer")])
def test_anti_invention_line_discourages_unasked_limitations(lang, phrase):
    assert phrase in t(lang)["no_invention"]


def test_en_gender_rule_enforces_first_person_and_avoids_third_person_pronouns():
    import re

    en = t("en")["gender"]
    low = en.lower()
    assert "first person" in low
    assert "third person" in low
    assert '"i"' in low or "'i'" in low or " i " in low
    assert not re.search(r"\b(?:she|her|kira)\b", low)
    for name in ("direct", "chat"):
        allowed, reason = output_guard(en, name)
        assert allowed, reason

