"""C5: gate triggers and identity/capability facts for owner questions about
what Kira is, how to install her and how to change the language model."""
import pytest

from opencohost.core.self_knowledge import SelfKnowledgeGate
from opencohost.core.self_knowledge.facts import OWNER, VIEWER, SelfKnowledgeContext
from opencohost.core.self_knowledge.providers import IdentityCard, IdentityProvider, ModelProvider

GATE = SelfKnowledgeGate()


@pytest.mark.parametrize(
    "text,topic",
    [
        ("¿cómo te instalo?", "model"),
        ("how do I install you", "model"),
        ("cómo instalo opencohost", "model"),
        ("¿cómo cambio de modelo?", "model"),
        ("quiero cambiar el modelo", "model"),
        ("how do I change the model", "model"),
        ("how can i switch your model", "model"),
        ("¿qué eres tú?", "identity"),
        ("what are you exactly", "identity"),
        ("¿eres un modelo?", "identity"),
        ("¿eres un modelo?", "model"),
        ("are you a model", "identity"),
        ("¿eres una novia virtual?", "identity"),
        ("eres una companion", "identity"),
        ("are you an AI companion", "identity"),
        ("¿y OpenCohost?", "identity"),
        ("opencohost", "identity"),
    ],
)
def test_new_triggers(text, topic):
    assert topic in GATE.detect(text)


@pytest.mark.parametrize(
    "text",
    [
        "cambiar el modelo 3d del avatar",
        "cómo cambio de modelo de negocio",
        "how do I change the model vrm",
        "eres un modelo de negocio genial",
        "cómo te instalo un mod en minecraft",
        "instalo el juego esta noche",
        "mi novia virtual es de otro juego",
        "me encanta este companion de juego",
    ],
)
def test_new_triggers_have_no_obvious_false_positives(text):
    detected = GATE.detect(text)
    assert "model" not in detected and "identity" not in detected


def _ctx(lang, audience=OWNER, model="qwen3"):
    return SelfKnowledgeContext(audience=audience, source="direct", model=model, lang=lang)


def test_identity_facts_es_cover_avatar_voice_vision_ptt_swappable_model():
    facts = IdentityProvider(card_reader=lambda: IdentityCard()).facts(_ctx("es", VIEWER))
    text = " ".join(f.text for f in facts)
    for needle in ("avatar VRM", "voz", "pantalla", "pulsar para hablar (PTT)", "modelo de lenguaje intercambiable"):
        assert needle in text
    assert all(f.audience == VIEWER for f in facts)


def test_identity_facts_en_cover_avatar_voice_vision_ptt_swappable_model():
    facts = IdentityProvider(card_reader=lambda: IdentityCard()).facts(_ctx("en", VIEWER))
    text = " ".join(f.text for f in facts)
    for needle in ("VRM avatar", "voice", "screen", "push-to-talk", "swappable language model"):
        assert needle in text


@pytest.mark.parametrize("lang,needles", [
    ("es", ("Controles", "tarjeta «Modelo»", "tarjeta «Proveedor LLM»", "Ollama", "API key")),
    ("en", ("Controls", "\"Model\" card", "\"LLM provider\" card", "Ollama", "API key")),
])
def test_model_provider_owner_only_how_to_change_and_install(lang, needles):
    facts = ModelProvider().facts(_ctx(lang, model=""))
    text = " ".join(f.text for f in facts)
    assert facts and all(f.audience == OWNER for f in facts)
    for needle in needles:
        assert needle in text
    assert "voseo" not in text and "vos " not in text
