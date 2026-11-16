"""live-session-fixes L3/L4: model identity, local/cloud fact, gender as grammar."""
from types import SimpleNamespace

import pytest

from opencohost.config.validation import output_guard
from opencohost.core.self_knowledge.facts import TOPIC_CREATOR, TOPIC_MODEL
from opencohost.core.self_knowledge.gate import SelfKnowledgeGate
from opencohost.core.self_knowledge.providers import (
    CapabilitiesProvider,
    ClockProvider,
    CreatorProvider,
    IdentityCard,
    IdentityProvider,
    ModelProvider,
    RuntimeProvider,
)
from opencohost.core.self_knowledge.facts import SelfKnowledgeContext
from opencohost.core.self_knowledge.service import SelfKnowledgeService
from opencohost.core.self_knowledge.texts import t

CARD = IdentityCard(creator="acme", repository="https://example.test/r", description="A co-host.")
GATE = SelfKnowledgeGate()


def _service(provider="local", lang="es", viewers=False):
    settings = SimpleNamespace(enabled=True, viewers=viewers)
    return SelfKnowledgeService(
        providers=[
            ClockProvider(),
            ModelProvider(),
            IdentityProvider(card_reader=lambda: CARD),
            CapabilitiesProvider(vision_enabled=lambda: True, tts_label=lambda: "edge"),
            CreatorProvider(card_reader=lambda: CARD),
            RuntimeProvider(provider_reader=lambda: provider),
        ],
        settings_reader=lambda: settings,
        lang_reader=lambda: lang,
    )


@pytest.mark.parametrize("text", [
    "¿por quién fuiste creada?",
    "¿quién te entrenó?",
    "¿qué empresa te hizo?",
    "que compañía te desarrolló",
    "quien creo este proyecto",
    "were you created by google?",
    "which company made you",
    "who created this project",
])
def test_gate_widens_creator_phrasings(text):
    assert TOPIC_CREATOR in GATE.detect(text)


@pytest.mark.parametrize("text", [
    "eres local o cloud?", "funcionas en la nube", "¿dónde estás corriendo?",
    "desde donde interactuas", "dónde corres", "are you running in the cloud",
    "are you local or cloud", "where are you running",
])
def test_gate_detects_runtime_topic(text):
    assert "runtime" in GATE.detect(text)


@pytest.mark.parametrize("text", ["eres gemma?", "are you qwen", "eres llama o qué"])
def test_gate_eres_model_name_is_model_topic(text):
    assert TOPIC_MODEL in GATE.detect(text)


def test_gate_does_not_fire_on_plain_talk():
    assert GATE.detect("guardemos el clip y sigamos jugando") == ()


@pytest.mark.parametrize("lang", ["es", "en"])
def test_model_fact_says_you_are_kira_not_the_model(lang):
    [fact] = ModelProvider().facts(SelfKnowledgeContext("owner", "direct", model="gemma4:e2b", lang=lang))
    low = fact.text.lower()
    assert "gemma4:e2b" in fact.text
    assert ("eres kira" if lang == "es" else "you are kira") in low
    assert ("no eres ese modelo" if lang == "es" else "not that model") in low
    for name in ("direct", "ptt"):
        allowed, reason = output_guard(fact.text, name)
        assert allowed, reason


def test_model_turn_also_gets_identity_fact():
    block = _service(lang="en")("what model are you", "direct", request_model="gemma4:e2b")
    assert "You are Kira, a streaming co-host and part of the OpenCohost project" in block
    assert block.count("gemma4:e2b") == 1


def test_model_turn_for_viewers_stays_empty_of_owner_lines():
    block = _service(lang="en", viewers=True)("what model are you", "chat", request_model="gemma4:e2b")
    assert "gemma4" not in block and "Kira" not in block.replace(t("en")["gender"], "")


def test_local_runtime_line_when_local():
    block = _service("local", "es")("eres local o cloud?", "direct", request_model="m")
    assert "en local" in block and "no en la nube" in block


def test_cloud_runtime_line_when_cloud_never_claims_local_and_hides_keys():
    block = _service("openai", "en")("are you local or cloud", "direct", request_model="m")
    assert "openai" in block
    assert "not in the cloud" not in block
    assert "key" in block.lower()  # the "never reveal keys" instruction
    allowed, reason = output_guard(block, "direct")
    assert allowed, reason


def test_cloud_provider_name_is_sanitized():
    block = _service("op en\nai<script>")("where are you running", "direct", request_model="m")
    assert "<script>" not in block and "\nai" not in block


def test_viewer_never_gets_runtime_line():
    block = _service("local", "es", viewers=True)("eres local o cloud?", "chat", request_model="m")
    assert "en local" not in block.replace(t("es")["cap_generic"], "")


def test_runtime_reader_failure_yields_no_fact():
    def boom():
        raise RuntimeError("x")

    assert RuntimeProvider(provider_reader=boom).facts(SelfKnowledgeContext("owner", "direct")) == []


# --- L4 ---------------------------------------------------------------------

@pytest.mark.parametrize("lang,header_key,woman", [("es", "header", "es una mujer"), ("en", "header", "is a woman")])
def test_gender_is_a_grammar_instruction_before_the_facts_header(lang, header_key, woman):
    block = _service(lang=lang)("quién eres" if lang == "es" else "who are you", "direct", request_model="m")
    header = t(lang)[header_key]
    pos = block.index(header)
    before, after = block[:pos], block[pos:]
    assert t(lang)["gender"] in before
    assert woman not in after.lower()
    assert t(lang)["gender"] not in after


@pytest.mark.parametrize("lang", ["es", "en"])
def test_identity_fact_no_longer_repeats_gender(lang):
    [fact] = IdentityProvider(card_reader=lambda: CARD).facts(SelfKnowledgeContext("owner", "direct", lang=lang))
    low = fact.text.lower()
    assert "femenin" not in low and "feminine" not in low


def test_gender_line_is_not_a_factual_claim_and_keeps_interlocutor_clause():
    es, en = t("es")["gender"].lower(), t("en")["gender"].lower()
    assert "es una mujer" not in es and "is a woman" not in en
    assert "salvo que te" in es and "unless" in en
    assert "no asumas el género" in es and "never assume the gender" in en
