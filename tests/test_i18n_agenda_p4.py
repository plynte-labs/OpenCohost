"""Tests for topic suggester + Scout prompt + agenda UI error labels (P4,
kira_bilingual_e2e).

This unit migrates three UI/filler-domain slots and wires their consumers:

  * ``nlp.topic_templates.*`` -- topic_suggester.py's ``_TEMPLATES_BY_CATEGORY``
  * ``llm.scout_prompt`` -- llm_engine.py's ``SCOUT_PROMPT_TEMPLATE``
  * ``ui.agenda_errors.*`` -- kira_agenda_controller.py's remaining
    ``ErrorCode.human()`` labels (``guardrails_missing`` already landed in P1)

Proves, per the established T3/P1a/P3a pattern:

  * es byte-identity: every migrated slot reproduces the CURRENT hard-coded
    literal/class-constant byte-for-byte.
  * en per-path: newly-authored English values, no Spanish markers, every
    slot formats without ``KeyError``.
  * fallback contract (all three domains are fallback-allowed): every
    accessor returns its LEGACY es default when the active locale ships no
    slot, or the slot is malformed/empty.
  * wiring: the real consumers (topic_suggester.generate_suggestions,
    llm_engine.MotorVocalIA.scout_digest, ErrorCode.human()) actually read
    from the active bundle -- not just dead accessors.
  * negative test: this UI/filler-domain work does NOT touch the P1
    guardrails-domain fail-closed contract.
"""
from __future__ import annotations

import pytest

from opencohost.i18n import active
from opencohost.i18n.contract import TIER_OFFICIAL, LocaleBundle
from opencohost.i18n.registry import build_chain, discover_bundles, official_locales_dir
from opencohost.i18n.startup import resolve_active_bundle
from opencohost.smart_aggregator import topic_suggester
from opencohost.smart_aggregator.kira_agenda_controller import ErrorCode

# --- byte-identity anchors (bound to the live source being migrated) --------

# The old _TEMPLATES_BY_CATEGORY dict, topic_suggester.py:33-61 (pre-P4).
LEGACY_TOPIC_TEMPLATES = {
    "game": [
        ("{entity}: ¿moda pasajera o acá para quedarse?", "Comparar con títulos similares y leer reacciones del chat."),
        ("El dilema de {entity}", "¿Lo vale o está sobrevalorado? Abrir debate sin tomar partido absoluto."),
        ("{entity} y la comunidad", "Qué dice el chat, qué cambió en los últimos meses."),
        ("¿{entity} sigue vivo?", "Revisar estado actual, updates recientes, interés del público."),
    ],
    "trade": [
        ("{entity} en el mercado actual", "Valores, tendencias y si conviene tradear ahora o esperar."),
        ("Tradeos: el fenómeno {entity}", "Contar anécdotas, leer opiniones del chat, dar perspectiva."),
    ],
    "collab": [
        ("Colaboraciones con {entity}", "¿Qué podría salir bien? ¿Qué podría salir… interesante?"),
        ("{entity} en el radar", "Por qué aparece tanto en el chat y qué dice eso de la comunidad."),
    ],
    "generic": [
        ("{entity}: ¿qué opina el chat?", "Leer ambiente, dar una opinión con matiz y abrir a respuestas."),
        ("El fenómeno {entity}", "Contexto, por qué importa ahora y cómo llegamos acá."),
        ("Hablemos de {entity}", "Ángulo fresco, sin repetir lo obvio ni sonar a Wikipedia."),
    ],
    "vibe_high": [
        ("El chat está que arde: ¿qué está pasando?", "Leer la temperatura, nombrear lo que se siente sin exagerar."),
        ("Momento de alto voltaje", "Comentar el pico de energía con humor seco, sin apagarlo."),
    ],
    "transition": [
        ("¿Y ahora qué?", "Transición natural desde el último tema, abriendo ventana a lo que sigue."),
        ("Cerramos ese capítulo… ¿qué sigue?", "Gancho para el próximo tema sin forzar estructura."),
    ],
}

# The old SCOUT_PROMPT_TEMPLATE constant, llm_engine.py:106-121 (pre-P4).
LEGACY_SCOUT_PROMPT_TEMPLATE = """Sos un asistente que sugiere próximos temas para un stream en vivo.
A partir de estas notas de la conversación reciente, proponé 2 o 3 temas
NUEVOS y ADYACENTES para seguir la charla: no repitas lo ya dicho, pensá
hacia dónde podría derivar de forma natural lo que se vino hablando.

Notas:
{digest_block}

Reglas de salida:
- Solo títulos, uno por línea.
- Máximo 6 palabras por título.
- Sin numeración, sin viñetas, sin comillas, sin explicaciones.
- Nada de código ni símbolos.

Ejemplo: si se habló de modelos de lenguaje (LLMs), un tema adyacente válido
sería "regulación de LLMs" o "alucinaciones de IA"."""

# The old ErrorCode.human() _MAP, kira_agenda_controller.py:53-64 (pre-P4).
LEGACY_AGENDA_ERRORS = {
    "guardrail_looping": "Respuesta repetitiva",
    "guardrail_leak": "Frase interna filtrada",
    "guardrail_similar": "Respuesta muy parecida a la anterior",
    "guardrail_empty": "El modelo no generó respuesta",
    "llm_timeout": "El modelo tardó demasiado",
    "ollama_down": "Ollama no está respondiendo",
    "guardrail_breaks_character": "Kira rompió el contrato de personaje",
    "guardrails_missing": "Sin guardarraíles de personaje para el idioma activo",
}


@pytest.fixture
def official():
    """The real on-disk official registry (es + en)."""
    return discover_bundles(official_locales_dir(), "official")


@pytest.fixture(autouse=True)
def _reset_active():
    active.reset_active_bundle()
    yield
    active.reset_active_bundle()


def _activate(code, official):
    active.set_active_bundle(resolve_active_bundle(locale=code, registry=official))


def _bare_bundle():
    return LocaleBundle(code="xx", tier=TIER_OFFICIAL, data={"meta": {"code": "xx"}})


# --- es byte-identity ---------------------------------------------------------


def test_es_topic_templates_are_byte_identical(official):
    _activate("es", official)
    assert active.topic_templates() == LEGACY_TOPIC_TEMPLATES


def test_es_scout_prompt_is_byte_identical(official):
    _activate("es", official)
    assert active.scout_prompt() == LEGACY_SCOUT_PROMPT_TEMPLATE


def test_es_agenda_errors_are_byte_identical(official):
    _activate("es", official)
    assert active.agenda_errors() == LEGACY_AGENDA_ERRORS


def test_es_error_code_human_matches_legacy_map(official):
    _activate("es", official)
    for code in ErrorCode:
        if code is ErrorCode.NONE:
            assert code.human() == ""
            continue
        assert code.human() == LEGACY_AGENDA_ERRORS[code.name.lower()], code


# --- en per-path ----------------------------------------------------------


def test_en_topic_templates_have_same_categories_and_entity_placeholder(official):
    _activate("es", official)
    es_templates = active.topic_templates()
    _activate("en", official)
    en_templates = active.topic_templates()
    assert set(en_templates) == set(es_templates)
    for category, entries in en_templates.items():
        assert len(entries) == len(es_templates[category])
        for title, angle in entries:
            assert "{entity}" in title or category in ("vibe_high", "transition")
            assert isinstance(angle, str) and angle


def test_en_topic_templates_format_without_keyerror(official):
    _activate("en", official)
    for entries in active.topic_templates().values():
        for title, angle in entries:
            title.format(entity="Minecraft")
            angle.format(entity="Minecraft")


def test_en_topic_templates_are_not_the_es_defaults(official):
    _activate("en", official)
    assert active.topic_templates() != LEGACY_TOPIC_TEMPLATES


def test_en_scout_prompt_formats_without_keyerror_and_has_no_spanish_markers(official):
    _activate("en", official)
    template = active.scout_prompt()
    assert "¿" not in template and "¡" not in template
    assert template != LEGACY_SCOUT_PROMPT_TEMPLATE
    template.format(digest_block="- some note")


def test_en_agenda_errors_have_same_keys_and_differ_from_es(official):
    _activate("es", official)
    es_errors = active.agenda_errors()
    _activate("en", official)
    en_errors = active.agenda_errors()
    assert set(en_errors) == set(es_errors)
    for key in es_errors:
        assert en_errors[key] != es_errors[key]


def test_en_error_code_human_differs_from_es(official):
    _activate("en", official)
    assert ErrorCode.LLM_TIMEOUT.human() == "The model took too long"
    assert ErrorCode.NONE.human() == ""


# --- fallback contract (llm/nlp/ui domains, all fallback-allowed) ----------


def test_all_p4_accessors_return_legacy_when_slot_missing():
    active.set_active_bundle(_bare_bundle())
    assert active.topic_templates() == LEGACY_TOPIC_TEMPLATES
    assert active.scout_prompt() == LEGACY_SCOUT_PROMPT_TEMPLATE
    assert active.agenda_errors() == LEGACY_AGENDA_ERRORS


def test_topic_templates_returns_legacy_on_wrong_type():
    bundle = LocaleBundle(
        code="xx", tier=TIER_OFFICIAL,
        data={"meta": {"code": "xx"}, "nlp": {"topic_templates": "not a dict"}},
    )
    active.set_active_bundle(bundle)
    assert active.topic_templates() == LEGACY_TOPIC_TEMPLATES


def test_agenda_errors_returns_legacy_on_empty_dict():
    bundle = LocaleBundle(
        code="xx", tier=TIER_OFFICIAL,
        data={"meta": {"code": "xx"}, "ui": {"agenda_errors": {}}},
    )
    active.set_active_bundle(bundle)
    assert active.agenda_errors() == LEGACY_AGENDA_ERRORS


# --- wiring: real consumers actually read the active bundle ----------------


def test_topic_suggester_generate_suggestions_uses_active_locale_bundle():
    """A custom bundle's 'vibe_high' template must reach generate_suggestions'
    output -- proving the accessor is actually consumed, not dead code."""
    bundle = LocaleBundle(
        code="xx", tier=TIER_OFFICIAL,
        data={
            "meta": {"code": "xx"},
            "nlp": {
                "topic_templates": {
                    "game": [{"title": "g", "angle": "ga"}],
                    "trade": [{"title": "t", "angle": "ta"}],
                    "collab": [{"title": "c", "angle": "ca"}],
                    "generic": [{"title": "gen", "angle": "gena"}],
                    "vibe_high": [{"title": "CUSTOM VIBE MARKER", "angle": "custom angle"}],
                    "transition": [{"title": "trans", "angle": "transa"}],
                },
            },
        },
    )
    active.set_active_bundle(bundle)
    suggestions = topic_suggester.generate_suggestions(
        intent_summary={"total_messages": 0, "top_intents": []},
        snapshots=[],
        vibe_temperature=90,
        existing_topics=[],
    )
    assert len(suggestions) == 1
    assert suggestions[0]["title"] == "CUSTOM VIBE MARKER"
    assert suggestions[0]["angle"] == "custom angle"


def test_scout_prompt_wiring_uses_active_locale_bundle(monkeypatch, official):
    """The real scout_digest() call site must build its prompt from
    i18n_active.scout_prompt(), not a dead module constant."""
    from tests.test_topic_scout import _make_scout_motor, _recording_chat_factory

    _activate("en", official)
    calls, chat = _recording_chat_factory()
    motor, _ = _make_scout_motor(monkeypatch, chat_impl=chat)

    motor.scout_digest()

    assert calls, "scout chat was never invoked"
    prompt = calls[0]["messages"][0]["content"]
    assert "Sos un asistente" not in prompt
    assert "You are an assistant that suggests upcoming topics" in prompt


# --- negative test: P1 guardrails-domain accessors untouched by P4 ----------


def test_p4_slots_do_not_satisfy_or_touch_the_guardrails_fail_closed_gate():
    """A bundle carrying every P4 UI/filler slot but NO guardrails.agenda must
    still fail closed -- proves this unit's nlp/llm/ui additions are fully
    independent of the P1 security domain (design D2/D4)."""
    bundle = LocaleBundle(
        code="xx", tier=TIER_OFFICIAL,
        data={
            "meta": {"code": "xx"},
            "nlp": {"topic_templates": {"vibe_high": [{"title": "x", "angle": "y"}]}},
            "llm": {"scout_prompt": "custom scout {digest_block}"},
            "ui": {"agenda_errors": {"llm_timeout": "custom timeout label"}},
        },
    )
    active.set_active_bundle(bundle)
    assert active.agenda_guardrails() is None
    assert active.agenda_banned_closures() is None
    # Confirm the P4 slots on the SAME bundle DID resolve (sanity: this is a
    # scoping proof, not a bundle-loading failure).
    assert active.scout_prompt() == "custom scout {digest_block}"
    assert active.agenda_errors()["llm_timeout"] == "custom timeout label"


# --- manifest structure (both locales load cleanly through the real chain) --


def test_official_bundles_chain_and_expose_p4_slots():
    official_bundles = discover_bundles(official_locales_dir(), "official")
    for code in ("es", "en"):
        bundle = build_chain(code, official_bundles)
        assert set(bundle.data["nlp"]["topic_templates"]) == {
            "game", "trade", "collab", "generic", "vibe_high", "transition",
        }
        assert "scout_prompt" in bundle.data["llm"]
        assert set(bundle.data["ui"]["agenda_errors"]) == set(LEGACY_AGENDA_ERRORS)
