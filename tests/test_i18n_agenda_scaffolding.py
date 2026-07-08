"""Tests for the agenda prompt scaffolding bundle slots (P3a, kira_bilingual_e2e).

This unit is DATA + ACCESSOR plumbing only — zero controller wiring. That lands
in P3b (same file as P1b's gate, so it must land after it). This file proves:

  * es byte-identity: every migrated slot (task_template, repair_prefix,
    anti_repetition_template, defaults, instructions, rules.length,
    rules.rhythm, rules.safety) reproduces the CURRENT
    ``kira_agenda_controller.py`` literal/class-constant byte-for-byte (the T3
    characterization pattern).
  * The golden characterization test (design risk #3 mitigation): filling
    ``task_template`` with a real call's computed parts reproduces the
    controller's actual (pre-refactor) ``_build_prompt()`` output byte-for-
    byte, in TWO shapes (an active topic + no active topic / all-defaults),
    so P3b's re-assembly has a pinned oracle to refactor against.
  * en per-path: newly-authored English values, structurally symmetric to es
    (same placeholder names, same dict keys), no ``¿``/``¡`` markers, and
    every slot formats without ``KeyError``.
  * fallback contract (`llm` domain, fallback-allowed): every accessor returns
    its LEGACY es default when the active locale ships no
    ``llm.agenda.*`` slot, or the slot is malformed/empty.

All assertions inject the locale explicitly; none read the persisted locale.
"""
from __future__ import annotations

import string

import pytest

from opencohost.i18n import active
from opencohost.i18n.contract import TIER_OFFICIAL, LocaleBundle
from opencohost.i18n.registry import build_chain, discover_bundles, official_locales_dir
from opencohost.i18n.startup import resolve_active_bundle
from opencohost.smart_aggregator.kira_agenda_controller import KiraAgendaController

# --- byte-identity anchors (bound to the live source being migrated) --------

# _build_prompt's repair-retry prefix, kira_agenda_controller.py ~:1317-1323.
LEGACY_REPAIR_PREFIX = (
    "REESCRIBE: hablá como Kira, la co-host. "
    "No menciones contexto, sesión, reflexión, procesamiento. "
    "No preguntes qué hacer ni ofrezcas ayuda. "
    "No digas que sos una IA. "
    "Solo hablá natural como co-host de stream.\n\n"
)

# _build_prompt's forced anti-repetition block, ~:1328-1331.
LEGACY_ANTI_REPETITION_TEMPLATE = (
    "ANTI-REPETICIÓN FORZADA: no repitas estas frases/aperturas exactas ni algo muy similar, "
    "decí algo distinto: {phrases}\n\n"
)

# _build_prompt's filler defaults for an absent active topic, ~:1333-1340.
# ptt_block/editorial_block added in P3b (kira_bilingual_e2e): filler for an
# empty ptt_text / editorial_context argument -- a gap the original P3a table
# missed (same "filler for missing input" concept as the other four keys).
# style/style_fallback added later in kira_bilingual_e2e (judge-panel fix):
# __init__'s default profile style and _build_prompt's empty-style fallback
# were still hardcoded Spanish literals leaking into en prompts under a
# default/no-op profile -- same gap-closing pattern as ptt_block/editorial_block.
LEGACY_DEFAULTS = {
    "title": "sin tema activo",
    "angle": "mantenerlo concreto, entretenido y seguro",
    "constraints": "- 1-2 frases cortas.\n- Una idea por turno.",
    "last_lines": "- nada todavía",
    "ptt_block": "- sin PTT",
    "editorial_block": "- sin cue card editorial activo",
    "style": (
        "Soná como co-host natural de stream: cercana, con humor seco, "
        "sin anunciar estructura ni despedirte entre ideas."
    ),
    "style_fallback": "Soná natural, como co-host de stream.",
}

# Topic-beat instruction literals. chat/ptt/closing come from _chat_action,
# _streamer_action, _closing_action respectively; topic_open/topic_continue
# from the two _topic_action(...) callers; preview_continue from the
# _preview_topic_action(...) continue-branch caller. The prefetch-stop
# _preview_topic_action(...) call reuses the SAME literal as `closing`
# (verified identical pre-migration text — one slot, two call sites).
LEGACY_INSTRUCTIONS = {
    "chat": "Integrá el contexto compacto si suma, sin decir que viene del chat. Si no suma, seguí natural con la idea actual.",
    "ptt": "Respondé o ajustá el rumbo según esta indicación del streamer. No inventes que dijo otra cosa.",
    "closing": "Hacé una transición natural a otra idea sin despedirte, sin decir 'tema', 'episodio', 'eso es todo' ni anunciar estructura.",
    "topic_open": "Entrá al tema como comentario orgánico de stream. No anuncies que estás abriendo un tema.",
    "topic_continue": "Seguí desarrollando la postura como si fuera una conversación viva. No digas que vas al siguiente tema ni que estás cerrando.",
    "preview_continue": "Seguí desarrollando la postura como bloque fluido de stream. Sumá un ángulo nuevo, no repitas ni cierres en círculo.",
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


def _placeholders(template: str) -> set[str]:
    return {name for _, name, _, _ in string.Formatter().parse(template) if name}


# --- es byte-identity ---------------------------------------------------------


def test_es_repair_prefix_is_byte_identical(official):
    _activate("es", official)
    assert active.agenda_repair_prefix() == LEGACY_REPAIR_PREFIX


def test_es_anti_repetition_template_is_byte_identical(official):
    _activate("es", official)
    assert active.agenda_anti_repetition_template() == LEGACY_ANTI_REPETITION_TEMPLATE


def test_es_defaults_are_byte_identical(official):
    _activate("es", official)
    assert active.agenda_defaults() == LEGACY_DEFAULTS


def test_es_instructions_are_byte_identical(official):
    _activate("es", official)
    assert active.agenda_instructions() == LEGACY_INSTRUCTIONS


def test_es_rules_length_is_byte_identical_to_class_constant(official):
    _activate("es", official)
    assert active.agenda_rules_length() == KiraAgendaController.RESPONSE_LENGTH_RULES


def test_es_rules_rhythm_is_byte_identical_to_class_constant(official):
    _activate("es", official)
    rhythm = active.agenda_rules_rhythm()
    # The class constant has a duplicate 'dinámico'/'dinamico' key (same
    # value); the manifest collapses it to one 'dinamico' entry.
    assert rhythm["calmo"] == KiraAgendaController.RHYTHM_RULES["calmo"]
    assert rhythm["normal"] == KiraAgendaController.RHYTHM_RULES["normal"]
    assert rhythm["dinamico"] == KiraAgendaController.RHYTHM_RULES["dinamico"]
    assert rhythm["dinamico"] == KiraAgendaController.RHYTHM_RULES["dinámico"]
    assert set(rhythm) == {"calmo", "normal", "dinamico"}


def test_es_rules_safety_rule_text_is_byte_identical_to_class_constant(official):
    _activate("es", official)
    safety = active.agenda_rules_safety()
    for mode in ("live_safe", "monologue", "test"):
        assert safety[mode]["rule"] == KiraAgendaController.LIVE_SAFETY_MODE_RULES[mode]["rule"]


# --- golden characterization: task_template.format(**parts) == real output --


def _build_parts(controller, *, instruction, compact_chat="", ptt_text="", editorial_context=""):
    """Compute the exact keyword args _build_prompt's internals derive, so a
    manifest-driven .format() call can be compared against the real method's
    pre-refactor output. Mirrors kira_agenda_controller.py's _build_prompt
    body line-for-line (P3a is read-only against that method: no wiring)."""
    topic = controller.active_topic
    defaults = LEGACY_DEFAULTS
    title = topic.title if topic else defaults["title"]
    angle = topic.angle if topic and topic.angle else defaults["angle"]
    constraints = "\n".join(f"- {c}" for c in (topic.constraints if topic else [])) or defaults["constraints"]
    response_rule = controller.RESPONSE_LENGTH_RULES.get(controller.response_length, controller.RESPONSE_LENGTH_RULES["normal"])
    rhythm_rule = controller.RHYTHM_RULES.get(controller.rhythm, controller.RHYTHM_RULES["normal"])
    safety_rule = controller.LIVE_SAFETY_MODE_RULES.get(controller.safety_mode, controller.LIVE_SAFETY_MODE_RULES["live_safe"])
    block_size = controller._pending_turns_spoken if topic else 1
    last_lines = "\n".join(f"- {line}" for line in controller.last_outputs[-3:]) or defaults["last_lines"]
    style = controller.profile.get("style") or "Soná natural, como co-host de stream."
    interruption_rule = (
        "si entra PTT/chat, no continúes este bloque largo en el próximo turno."
        if safety_rule["interruptible"]
        else "modo de prueba; aun así respetá stop/emergencia del operador."
    )
    return dict(
        short_target=controller.SHORT_RESPONSE_TARGET_CHARS,
        normal_target=controller.NORMAL_RESPONSE_TARGET_CHARS,
        expanded_cap=controller.EXPANDED_RESPONSE_HARD_CAP_CHARS,
        instruction=instruction,
        block_size=block_size,
        style=style,
        title=title,
        angle=angle,
        rhythm_rule=rhythm_rule,
        response_rule=response_rule,
        safety_rule=safety_rule["rule"],
        interruption_rule=interruption_rule,
        constraints=constraints,
        ptt_block=ptt_text or "- sin PTT",
        chat_block=controller._wrap_untrusted_chat(compact_chat),
        editorial_block=editorial_context or "- sin cue card editorial activo",
        last_lines=last_lines,
    )


def test_task_template_reproduces_real_build_prompt_with_active_topic(official):
    _activate("es", official)
    controller = KiraAgendaController(response_length="normal", rhythm="calmo", safety_mode="monologue")
    topic = controller.add_topic(
        "Un tema de prueba", angle="un angulo de prueba",
        constraints=["constraint uno", "constraint dos"], approved=True,
    )
    controller.active_topic = topic
    controller.last_outputs = ["primera linea previa", "segunda linea previa"]
    controller._pending_turns_spoken = 3
    controller.profile = {"style": "Estilo de prueba"}

    actual = controller._build_prompt(
        instruction="Instruccion de prueba",
        compact_chat="mensaje de chat de prueba",
        ptt_text="",
        editorial_context="",
    )
    parts = _build_parts(controller, instruction="Instruccion de prueba", compact_chat="mensaje de chat de prueba")
    expected = active.agenda_task_template().format(**parts)
    assert expected == actual


def test_task_template_reproduces_real_build_prompt_with_all_defaults(official):
    _activate("es", official)
    controller = KiraAgendaController(response_length="expandida", rhythm="dinamico", safety_mode="test")
    controller.active_topic = None
    controller.last_outputs = []
    controller._pending_turns_spoken = 1
    controller.profile = {"style": ""}

    actual = controller._build_prompt(
        instruction="Otra instruccion",
        compact_chat="",
        ptt_text="dijo algo por PTT",
        editorial_context="tarjeta editorial activa",
    )
    parts = _build_parts(
        controller, instruction="Otra instruccion",
        compact_chat="", ptt_text="dijo algo por PTT", editorial_context="tarjeta editorial activa",
    )
    expected = active.agenda_task_template().format(**parts)
    assert expected == actual


# --- en per-path --------------------------------------------------------------


def test_en_task_template_has_same_placeholders_as_es(official):
    _activate("es", official)
    es_template = active.agenda_task_template()
    _activate("en", official)
    en_template = active.agenda_task_template()
    assert _placeholders(es_template) == _placeholders(en_template)


def test_en_task_template_has_no_spanish_markers(official):
    _activate("en", official)
    template = active.agenda_task_template()
    assert "¿" not in template and "¡" not in template
    assert template != active.LEGACY_AGENDA_TASK_TEMPLATE


def test_en_task_template_formats_without_keyerror(official):
    _activate("en", official)
    template = active.agenda_task_template()
    parts = {name: "x" for name in _placeholders(template)}
    template.format(**parts)  # raises on any KeyError/mismatch


def test_en_repair_prefix_and_anti_repetition_are_english(official):
    _activate("en", official)
    assert active.agenda_repair_prefix() != LEGACY_REPAIR_PREFIX
    assert "{phrases}" in active.agenda_anti_repetition_template()
    assert active.agenda_anti_repetition_template() != LEGACY_ANTI_REPETITION_TEMPLATE


def test_en_defaults_instructions_and_rules_have_same_keys_as_es(official):
    _activate("es", official)
    es_defaults, es_instructions = active.agenda_defaults(), active.agenda_instructions()
    es_length, es_rhythm, es_safety = (
        active.agenda_rules_length(), active.agenda_rules_rhythm(), active.agenda_rules_safety(),
    )
    _activate("en", official)
    en_defaults, en_instructions = active.agenda_defaults(), active.agenda_instructions()
    en_length, en_rhythm, en_safety = (
        active.agenda_rules_length(), active.agenda_rules_rhythm(), active.agenda_rules_safety(),
    )
    assert set(en_defaults) == set(es_defaults)
    assert set(en_instructions) == set(es_instructions)
    assert set(en_length) == set(es_length)
    assert set(en_rhythm) == set(es_rhythm)
    assert set(en_safety) == set(es_safety)
    for mode in es_safety:
        assert "rule" in en_safety[mode]
        assert en_safety[mode]["rule"] != es_safety[mode]["rule"]


# --- fallback contract (llm domain, fallback-allowed) -------------------------


def test_all_accessors_return_legacy_when_slot_missing():
    active.set_active_bundle(_bare_bundle())
    assert active.agenda_task_template() == active.LEGACY_AGENDA_TASK_TEMPLATE
    assert active.agenda_repair_prefix() == active.LEGACY_AGENDA_REPAIR_PREFIX
    assert active.agenda_anti_repetition_template() == active.LEGACY_AGENDA_ANTI_REPETITION_TEMPLATE
    assert active.agenda_defaults() == active.LEGACY_AGENDA_DEFAULTS
    assert active.agenda_instructions() == active.LEGACY_AGENDA_INSTRUCTIONS
    assert active.agenda_rules_length() == active.LEGACY_AGENDA_RULES_LENGTH
    assert active.agenda_rules_rhythm() == active.LEGACY_AGENDA_RULES_RHYTHM
    assert active.agenda_rules_safety() == active.LEGACY_AGENDA_RULES_SAFETY


def test_dict_accessor_returns_legacy_on_wrong_type():
    bundle = LocaleBundle(
        code="xx", tier=TIER_OFFICIAL,
        data={"meta": {"code": "xx"}, "llm": {"agenda": {"instructions": "not a dict"}}},
    )
    active.set_active_bundle(bundle)
    assert active.agenda_instructions() == active.LEGACY_AGENDA_INSTRUCTIONS


def test_dict_accessor_returns_legacy_on_empty_dict():
    bundle = LocaleBundle(
        code="xx", tier=TIER_OFFICIAL,
        data={"meta": {"code": "xx"}, "llm": {"agenda": {"defaults": {}}}},
    )
    active.set_active_bundle(bundle)
    assert active.agenda_defaults() == active.LEGACY_AGENDA_DEFAULTS


# --- manifest structure (both locales load cleanly through the real chain) --


def test_official_bundles_chain_and_expose_agenda_scaffolding():
    official_bundles = discover_bundles(official_locales_dir(), "official")
    for code in ("es", "en"):
        bundle = build_chain(code, official_bundles)
        agenda = bundle.data["llm"]["agenda"]
        assert set(agenda) == {
            "task_template", "repair_prefix", "anti_repetition_template",
            "defaults", "instructions", "rules",
        }
        assert set(agenda["rules"]) == {"length", "rhythm", "safety"}
