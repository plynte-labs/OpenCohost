"""runtime-fixes-20261110 T1 (B, C, D, E): honesty rules and prompt wording.

B  no_memory_denial: Kira never says she has no persistent memory.
C  no_false_capability: Kira never claims she can generate/edit images, video or
   audio, or send files she created (nothing in this product does that).
D  the core-rules prompt sentence also forbids OFFERING code/JSON/templates.
E  the saved-memories block header carries a grounding sentence about invented
   shared history.

Both new rules have the no_code_output governance: listed in the guardrail
REGISTRY as locked/safety, not a preset-level NON_NEGOTIABLE_IDS member,
enforced unconditionally by output_guard.
"""

from pathlib import Path

import pytest
import yaml

from opencohost.config import guardrail_policy as gp
from opencohost.config.validation import NON_NEGOTIABLE_IDS, output_guard
from opencohost.core import llm_engine
from opencohost.i18n import active as i18n_active

MEMORY_RULE = "no_memory_denial"
CAPABILITY_RULE = "no_false_capability"
NEW_RULES = (MEMORY_RULE, CAPABILITY_RULE)
LOCALES = Path(llm_engine.__file__).resolve().parents[1] / "locales"


def _manifest(lang):
    return yaml.safe_load((LOCALES / lang / "manifest.yaml").read_text(encoding="utf-8"))


def _blocked_by(text, rule, source="direct"):
    allowed, reason = output_guard(text, source=source)
    return (not allowed) and f"[{rule}]" in reason


# ----------------------------------------------------------- B: memory denial

MEMORY_DENIAL = [
    "No puedo guardar información entre conversaciones.",
    "No puedo recordar nada entre conversaciones, lo siento.",
    "No puedo guardar ni recordar información de una conversación a otra.",
    "No tengo memoria persistente, así que cada vez empiezo desde cero.",
    "Ah, no tengo una memoria persistente.",
    "No tengo memoria a largo plazo.",
    "Cada conversación es nueva para mí.",
    "Cada chat es completamente nuevo para mí.",
    "Cada conversación empieza de cero para mí.",
    "No recuerdo nada de conversaciones anteriores porque no puedo guardar datos.",
    "No tengo memoria entre conversaciones.",
    "I can't remember between conversations.",
    "I cannot retain information between sessions.",
    "I have no persistent memory.",
    "I don't have persistent memory, sorry.",
    "I don't have any long-term memory.",
    "Each conversation starts fresh for me.",
    "Every conversation is brand new to me.",
    "I don't remember anything from previous conversations.",
    "I have no memory of past conversations.",
]

MEMORY_HONEST = [
    "No tengo nada guardado sobre eso.",
    "No recuerdo que hayamos hablado de eso.",
    "Eso no lo tengo guardado, cuéntamelo de nuevo.",
    "I don't have anything saved about that.",
    "I don't remember us talking about that.",
    "I have nothing saved on that topic.",
    "Tu memoria es mejor que la mía para los cumpleaños.",
    "Tienes buena memoria para los nombres.",
    "Mi computadora se quedó sin memoria RAM.",
    "No tengo memoria RAM suficiente para esto, creo.",
    "This drive has no persistent memory, it loses everything on reboot.",
    "Every conversation with a new viewer is different, that is what keeps it fun.",
    "Recuerdo lo que me contaste ayer sobre tu gato.",
    "I remember you told me about your cat.",
    "No puedo guardar tu contraseña por seguridad.",
]


@pytest.mark.parametrize("text", MEMORY_DENIAL)
def test_memory_denial_is_blocked(text):
    assert _blocked_by(text, MEMORY_RULE), text


@pytest.mark.parametrize("text", MEMORY_HONEST)
def test_honest_memory_statements_are_not_blocked(text):
    allowed, reason = output_guard(text, source="direct")
    assert allowed, (text, reason)


def test_memory_denial_is_blocked_on_every_source():
    for source in ("direct", "ptt", "chat", "kira-agenda"):
        assert _blocked_by(MEMORY_DENIAL[0], MEMORY_RULE, source=source), source


# ------------------------------------------------------ C: false capability

CAPABILITY_CLAIM = [
    "Claro, puedo generar una imagen de eso.",
    "Puedo crear una imagen para ti.",
    "Puedo dibujar un logo si quieres.",
    "Te genero una imagen en un momento.",
    "Te dibujo un meme ahora mismo.",
    "Aquí tienes la imagen que hice.",
    "Aquí tienes la imagen.",
    "Voy a generar una foto de tu gato.",
    "Te mando el archivo en un segundo.",
    "Te paso el enlace que preparé.",
    "Puedo editar tu foto, pásamela.",
    "Puedo hacer un video de eso.",
    "I can generate an image for you.",
    "Sure, I can draw a picture of that.",
    "I can create a logo for your stream.",
    "Let me draw that.",
    "Let me generate that for you.",
    "Here is the image I made.",
    "I'll send you the file in a second.",
    "I can edit your photo if you send it.",
    "I'll generate a video for you.",
]

CAPABILITY_HONEST = [
    "En la imagen veo un gato sobre un sofá.",
    "En la foto que me mandaste se ve una playa.",
    "Midjourney genera imágenes a partir de texto.",
    "Los generadores de imágenes están de moda.",
    "Te pinto el panorama: está complicado.",
    "Imagínate la escena, es una locura.",
    "No puedo generar imágenes, pero te lo describo con palabras.",
    "No puedo crear ni editar imágenes.",
    "I can't generate images, but I can describe it in words.",
    "I cannot draw pictures or edit photos.",
    "Midjourney can generate images from a text prompt.",
    "Let me paint you a picture of how this ends.",
    "I can see in the picture that the sofa is blue.",
    "Describe la imagen que te pasé.",
    "Puedo describir lo que ves en la pantalla.",
    "Puedo contarte cómo se dibuja un gato paso a paso con palabras.",
]


@pytest.mark.parametrize("text", CAPABILITY_CLAIM)
def test_false_capability_claim_is_blocked(text):
    assert _blocked_by(text, CAPABILITY_RULE), text


@pytest.mark.parametrize("text", CAPABILITY_HONEST)
def test_honest_capability_statements_are_not_blocked(text):
    allowed, reason = output_guard(text, source="direct")
    assert allowed, (text, reason)


# ------------------------------------------------ nothing generates images

def test_no_image_generation_feature_is_wired_to_kira():
    """The rule's premise: grep proves the product has no image generator."""
    root = Path(llm_engine.__file__).resolve().parents[1]
    needles = ("diffusers", "stable_diffusion", "comfyui", "txt2img", "text2image", "images/generations")
    offenders = []
    for path in root.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore").lower()
        for needle in needles:
            if needle in text:
                offenders.append((path.name, needle))
    assert offenders == []


# ------------------------------------------------------------- governance

@pytest.mark.parametrize("rule", NEW_RULES)
def test_new_rules_are_locked_registry_rows_not_non_negotiables(rule):
    assert gp.REGISTRY[rule] == {"tier": "locked", "category": "safety"}
    assert rule not in NON_NEGOTIABLE_IDS
    assert rule not in gp.TOGGLEABLE_IDS
    by_id = {r["id"]: r for r in gp.list_rules()}
    assert by_id[rule] == {"id": rule, "tier": "locked", "category": "safety", "enabled": True}


@pytest.mark.parametrize("rule", NEW_RULES)
def test_new_rules_can_never_be_disabled(rule):
    assert gp.set_disabled([rule]) == [{"id": rule, "code": "locked"}]
    assert gp.is_rule_enabled(rule)


@pytest.mark.parametrize("rule", NEW_RULES)
def test_new_rules_are_described_for_the_block_log(rule):
    from opencohost.config.validation import _NN_DESCRIPTIONS

    assert rule in _NN_DESCRIPTIONS


@pytest.mark.parametrize(
    "rule, text",
    [(MEMORY_RULE, MEMORY_DENIAL[0]), (CAPABILITY_RULE, CAPABILITY_CLAIM[0])],
)
def test_enforcement_ignores_the_registry(monkeypatch, rule, text):
    monkeypatch.setattr(gp, "is_rule_enabled", lambda rule_id: False)
    monkeypatch.setattr(gp, "REGISTRY", {})
    assert _blocked_by(text, rule)


@pytest.mark.parametrize("rule, text", [(MEMORY_RULE, MEMORY_DENIAL[0]), (CAPABILITY_RULE, CAPABILITY_CLAIM[0])])
def test_block_log_carries_no_text(rule, text, caplog):
    import logging

    with caplog.at_level(logging.WARNING):
        output_guard(text, source="direct")
    lines = [r.getMessage() for r in caplog.records if rule in r.getMessage()]
    assert lines
    assert text.split()[1] not in "".join(lines).replace(rule, "")


# ----------------------------------------------------------------- nudges

@pytest.mark.parametrize("lang", ["es", "en"])
@pytest.mark.parametrize("rule", NEW_RULES + ("no_code_output",))
def test_nudge_exists_dedicated_and_trips_no_guard(lang, rule):
    nudges = _manifest(lang)["llm"]["guardrail_retry_nudges"]
    assert nudges[rule].strip()
    assert nudges[rule] != nudges["generic"]
    allowed, reason = output_guard(nudges[rule], source="direct")
    assert allowed, reason


def test_nudges_differ_between_locales():
    es = _manifest("es")["llm"]["guardrail_retry_nudges"]
    en = _manifest("en")["llm"]["guardrail_retry_nudges"]
    for rule in NEW_RULES:
        assert es[rule] != en[rule]


@pytest.mark.parametrize("lang", ["es", "en"])
def test_code_nudge_answers_the_question_instead_of_explaining_what_you_would_do(lang):
    nudge = _manifest(lang)["llm"]["guardrail_retry_nudges"]["no_code_output"].lower()
    # The old wording invited a generic-assistant answer.
    assert "what you would do instead" not in nudge
    assert "qué harías en su lugar" not in nudge
    # New wording: in character, use what the prompt already shows, short.
    if lang == "en":
        assert "in character" in nudge and "memories" in nudge
    else:
        assert "personaje" in nudge and "recuerdos" in nudge
    assert len(nudge) < 520


@pytest.mark.parametrize("lang", ["es", "en"])
def test_memory_nudge_points_at_the_memories_in_the_prompt(lang):
    nudge = _manifest(lang)["llm"]["guardrail_retry_nudges"][MEMORY_RULE].lower()
    if lang == "en":
        assert "do have" in nudge and "nothing saved" in nudge
    else:
        assert "sí tienes" in nudge and "nada guardado" in nudge


@pytest.mark.parametrize("lang", ["es", "en"])
def test_capability_nudge_says_what_she_cannot_do(lang):
    nudge = _manifest(lang)["llm"]["guardrail_retry_nudges"][CAPABILITY_RULE].lower()
    if lang == "en":
        assert "cannot generate" in nudge and "in words" in nudge
    else:
        assert "no puedes generar" in nudge and "palabras" in nudge


def test_nudge_accessor_picks_the_new_rules():
    for rule in NEW_RULES:
        reason = f"Non-negotiable violation [{rule}]: x"
        assert llm_engine._guardrail_retry_nudge_for_reason(reason) == i18n_active.guardrail_retry_nudge(rule)
        assert i18n_active.guardrail_retry_nudge(rule) != i18n_active.guardrail_retry_nudge("generic")


def test_new_rules_reuse_the_generic_fallback_lines():
    motor = llm_engine.MotorVocalIA.__new__(llm_engine.MotorVocalIA)
    lines = i18n_active.guardrail_fallback_lines()
    generic = lines[:4]
    for rule in NEW_RULES:
        said = motor._guardrail_fallback_line("direct", f"Non-negotiable violation [{rule}]: x")
        assert said in generic


# --------------------------------------------------------- D: prompt wording

@pytest.mark.parametrize("lang", ["es", "en"])
def test_core_rules_body_forbids_offering_code(lang):
    body = _manifest(lang)["llm"]["core_rules"]["body"] if "core_rules" in _manifest(lang)["llm"] else None
    assert body is not None
    text = body.lower()
    if lang == "en":
        assert "or offer to write" in text
    else:
        assert "ni te ofrezcas a escribir" in text


def test_builtin_core_rules_copy_matches_the_en_manifest_body():
    builtin = i18n_active._CORE_RULES_BUILTIN["body"]
    assert "or offer to write" in builtin
    assert builtin == _manifest("en")["llm"]["core_rules"]["body"]


# ---------------------------------------------------- E: recall grounding

@pytest.mark.parametrize("lang", ["es", "en"])
def test_memory_block_header_grounds_shared_history(lang):
    tag = _manifest(lang)["llm"]["memorias_block_open"].lower()
    if lang == "en":
        assert "only say you talked" in tag and "do not have that saved" in tag
    else:
        assert "solo di que hablaron" in tag and "no lo tienes guardado" in tag
    assert tag.count('"') == 2  # one well-formed note attribute


def test_legacy_memory_block_open_matches_the_es_manifest():
    assert i18n_active.LEGACY_MEMORIAS_BLOCK_OPEN == _manifest("es")["llm"]["memorias_block_open"]
