"""self-knowledge K1: gate intents, providers, audience filter (pure, no I/O)."""
from datetime import datetime

import pytest

from opencohost.core.self_knowledge import (
    OWNER,
    VIEWER,
    SelfFact,
    SelfKnowledgeContext,
    SelfKnowledgeGate,
    filter_facts,
)
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
    derive_user_commands,
)

GATE = SelfKnowledgeGate()


@pytest.mark.parametrize(
    "text,topic",
    [
        ("¿Qué hora es?", "time"),
        ("what time is it", "time"),
        ("qué día es hoy", "time"),
        ("¿qué modelo eres?", "model"),
        ("Which model are you running?", "model"),
        ("cuánta VRAM tienes libre", "gpu"),
        ("what GPU do you run on", "gpu"),
        ("¿quién eres?", "identity"),
        ("what is OpenCohost", "identity"),
        ("¿qué puedes hacer?", "capabilities"),
        ("what can you do", "capabilities"),
        ("¿qué comandos tienes?", "commands"),
        ("list your commands", "commands"),
        ("¿quién te creó?", "creator"),
        ("who made you", "creator"),
        ("where is the repository", "creator"),
        ("¿cómo me llamo?", "interlocutor"),
        ("do you know who i am", "interlocutor"),
    ],
)
def test_gate_detects_each_intent(text, topic):
    assert topic in GATE.detect(text)


@pytest.mark.parametrize(
    "text",
    [
        "hola kira, ¿cómo estás?",
        "cuéntame un chiste",
        "a la hora de la cena hablamos",
        "this game has a great model of progression",
        "",
    ],
)
def test_gate_ignores_unrelated_text(text):
    assert GATE.detect(text) == ()


def test_gate_returns_stable_ordered_topics_and_never_raises_on_bad_input():
    assert GATE.detect("qué hora es y quién te creó") == ("time", "creator")
    assert GATE.detect(None) == ()  # type: ignore[arg-type]


def test_filter_owner_keeps_everything_viewer_keeps_viewer_only():
    facts = [SelfFact("model", "m", OWNER), SelfFact("identity", "i", VIEWER)]
    kept, withheld = filter_facts(facts, OWNER)
    assert kept == facts and withheld == 0
    kept, withheld = filter_facts(facts, VIEWER)
    assert [f.topic for f in kept] == ["identity"] and withheld == 1


def _ctx(audience=OWNER, model="qwopus:9b", lang="en"):
    return SelfKnowledgeContext(audience=audience, source="direct", model=model, lang=lang)


def test_clock_provider_formats_injected_time():
    p = ClockProvider(now=lambda: datetime(2026, 10, 4, 14, 32))
    [fact] = p.facts(_ctx())
    assert "2026-10-04" in fact.text and "14:32" in fact.text and "Sunday" in fact.text
    [fact_es] = p.facts(_ctx(lang="es"))
    assert "domingo" in fact_es.text
    assert fact.audience == VIEWER


def test_model_provider_owner_only_and_needs_a_name():
    p = ModelProvider()
    [fact] = p.facts(_ctx())
    assert "qwopus:9b" in fact.text and fact.audience == OWNER
    [nameless] = p.facts(_ctx(model=""))  # no model name: only the how-to-change/install guidance
    assert "qwopus" not in nameless.text and nameless.audience == OWNER


def test_gpu_provider_reports_free_and_total():
    info = GpuInfo(name="RTX 5060", total_mb=8192, free_mb=3072)
    [fact] = GpuProvider(reader=lambda: info).facts(_ctx())
    assert "RTX 5060" in fact.text and "3.0" in fact.text and "8.0" in fact.text
    assert fact.audience == OWNER


def test_gpu_provider_failure_or_absence_yields_no_fact():
    def boom():
        raise RuntimeError("pynvml missing")

    assert GpuProvider(reader=boom).facts(_ctx()) == []
    assert GpuProvider(reader=lambda: None).facts(_ctx()) == []


def test_identity_and_creator_use_the_card_and_are_viewer_visible():
    card = IdentityCard(creator="acme", repository="https://example.test/r", description="A co-host.")
    [ident] = IdentityProvider(card_reader=lambda: card).facts(_ctx())
    assert "OpenCohost" in ident.text and "A co-host." in ident.text and ident.audience == VIEWER
    facts = CreatorProvider(card_reader=lambda: card).facts(_ctx())
    joined = " ".join(f.text for f in facts)
    assert "acme" in joined and "example.test/r" in joined and "https://" not in joined
    assert all(f.audience == VIEWER for f in facts)


def test_capabilities_viewer_fact_is_generic_owner_fact_has_state():
    p = CapabilitiesProvider(vision_enabled=lambda: True, tts_label=lambda: "edge")
    facts = p.facts(_ctx())
    audiences = {f.audience for f in facts}
    assert audiences == {VIEWER, OWNER}
    owner_text = " ".join(f.text for f in facts if f.audience == OWNER)
    assert "edge" in owner_text and "enabled" in owner_text
    viewer_text = " ".join(f.text for f in facts if f.audience == VIEWER)
    assert "edge" not in viewer_text


def test_capabilities_reader_failure_degrades_to_generic_only():
    def boom():
        raise RuntimeError("x")

    facts = CapabilitiesProvider(vision_enabled=boom, tts_label=boom).facts(_ctx())
    assert facts and all(f.audience == VIEWER for f in facts)


def test_commands_are_derived_from_vision_gate_constants():
    cmds = derive_user_commands()
    assert cmds[0] == "/mira"
    assert any(c.startswith("/mira") and "clip" in c and "texto" in c for c in cmds)
    [fact] = CommandsProvider(commands=lambda: cmds).facts(_ctx())
    assert "/mira" in fact.text and fact.audience == OWNER
    assert CommandsProvider(commands=lambda: ()).facts(_ctx()) == []


def test_interlocutor_owner_uses_nickname_or_neutral_viewer_gets_none():
    [named] = InterlocutorProvider(owner_name=lambda: "Tavo").facts(_ctx())
    assert "Tavo" in named.text and named.audience == OWNER
    [neutral] = InterlocutorProvider(owner_name=lambda: "").facts(_ctx())
    assert "owner" in neutral.text and "Tavo" not in neutral.text
    assert InterlocutorProvider(owner_name=lambda: "Tavo").facts(_ctx(audience=VIEWER)) == []


# --- K8: persistent memory in the capabilities fact -------------------------

def _caps(memory, lang="en", **kw):
    p = CapabilitiesProvider(
        vision_enabled=lambda: True, tts_label=lambda: "edge", memory_enabled=memory, **kw
    )
    return p.facts(SelfKnowledgeContext(audience="owner", source="direct", lang=lang))


def test_capabilities_state_persistent_memory_when_enabled():
    text = " ".join(f.text for f in _caps(lambda: True))
    assert "remember" in text and "across sessions" in text
    es = " ".join(f.text for f in _caps(lambda: True, lang="es"))
    assert "recuerdas" in es and "entre sesiones" in es


def test_capabilities_state_memory_off_when_disabled_and_never_claim_it():
    text = " ".join(f.text for f in _caps(lambda: False))
    assert "across sessions" not in text and "do not remember" in text


def test_capabilities_memory_fact_is_owner_only_and_reader_failure_is_skipped():
    facts = _caps(lambda: True)
    mem = [f for f in facts if "across sessions" in f.text]
    assert mem and all(f.audience == OWNER for f in mem)

    def boom():
        raise RuntimeError("x")

    assert not [f for f in _caps(boom) if "session" in f.text]


def test_capabilities_without_memory_reader_has_no_memory_claim():
    p = CapabilitiesProvider(vision_enabled=lambda: True, tts_label=lambda: "edge")
    assert not [f for f in p.facts(_ctx()) if "session" in f.text]


def test_read_memory_enabled_follows_live_settings(monkeypatch):
    from opencohost.config import settings
    from opencohost.core.self_knowledge import wiring

    monkeypatch.setattr(settings, "MEMORY_FACTS_ENABLED", False)
    monkeypatch.setattr(settings, "MEMORY_EPISODES_ENABLED", False)
    assert wiring.read_memory_enabled() is False
    monkeypatch.setattr(settings, "MEMORY_EPISODES_ENABLED", True)
    assert wiring.read_memory_enabled() is True


@pytest.mark.parametrize("lang", ["es", "en"])
@pytest.mark.parametrize("state", [True, False])
def test_memory_wording_does_not_trip_guardrail_r9(lang, state):
    from opencohost.config.validation import output_guard

    for f in _caps(lambda: state, lang=lang):
        allowed, reason = output_guard(f.text, "direct")
        assert allowed, reason


@pytest.mark.parametrize(
    "text",
    [
        "qué datos puedes ver",
        "¿Qué información puedes ver?",
        "que informacion tienes",
        "qué info tienes",
        "que datos o para que sirves",
        "what data can you see",
        "what info do you have",
        "what information can you access",
    ],
)
def test_data_info_questions_gate_to_capabilities(text):
    assert "capabilities" in GATE.detect(text)


@pytest.mark.parametrize(
    "text", ["mira mi pantalla", "puedes ver esto", "qué hora es", "cuéntame un chiste", "look at this"]
)
def test_look_and_unrelated_text_do_not_gate_to_capabilities(text):
    assert "capabilities" not in GATE.detect(text)


@pytest.mark.parametrize(
    "text",
    [
        "JAJAJA te inventaste el modelo",
        "te inventaste el modelo",
        "¿cuál es tu modelo?",
        "dime el modelo base",
        "¿qué IA eres?",
        "qué motor usas",
        "que cerebro corres",
        "con qué IA corres",
        "what model are you running",
        "what is your base model",
        "you made up the model",
        "what AI do you run on",
        "which language model is this",
    ],
)
def test_broadened_model_intent(text):
    assert "model" in GATE.detect(text)


@pytest.mark.parametrize(
    "text",
    [
        "me gusta tu modelo vrm nuevo",
        "cambia tu modelo 3d por otro",
        "el modelo de negocio del canal",
        "that car model looks great",
        "qué ia más rara hay en este juego",
        "el motor del auto suena raro",
    ],
)
def test_broadened_model_intent_has_no_obvious_false_positives(text):
    assert "model" not in GATE.detect(text)
