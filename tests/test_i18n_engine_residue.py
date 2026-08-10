"""Direct-path residue migration (P2, kira_bilingual_e2e_20260705).

Absorbs i18n_engine_locale_residue_20260618/design.md section 4's test plan
(minus the R5-premised assertions — R5 turned out FALSE against current code:
the guardrail/repetition fallback returns in _generar_dialogo happen BEFORE
_commit_history, so there was never a memory-hardening coordination hazard to
test for). Adds coverage for the THREE wrapper slots (ptt_wrapper,
live_voice_wrapper, ptt_history_wrapper) that the residue design didn't know
about — those were added by the kira_bilingual_e2e design (section 2.2).
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from unittest.mock import MagicMock

import pytest

from opencohost.core import llm_engine
from opencohost.i18n import active
from opencohost.i18n.contract import TIER_OFFICIAL, LocaleBundle, resolve
from opencohost.i18n.registry import build_chain, discover_bundles, official_locales_dir
from opencohost.i18n.startup import resolve_active_bundle
from opencohost.smart_aggregator.kira_agenda_controller import KiraAgendaController


@pytest.fixture
def official():
    return discover_bundles(official_locales_dir(), "official")


@pytest.fixture(autouse=True)
def _reset_active():
    active.reset_active_bundle()
    yield
    active.reset_active_bundle()


def _activate(code, official):
    active.set_active_bundle(resolve_active_bundle(locale=code, registry=official))


def _bare_bundle(code: str = "xx") -> LocaleBundle:
    return LocaleBundle(code=code, tier=TIER_OFFICIAL, data={"meta": {"code": code}})


def _make_motor() -> llm_engine.MotorVocalIA:
    return llm_engine.MotorVocalIA(queue.Queue(), lambda event: None)


# ---------------------------------------------------------------------------
# 1. guardrail_fallback_lines()
# ---------------------------------------------------------------------------


def test_guardrail_fallback_lines_es_matches_legacy(official):
    _activate("es", official)
    assert active.guardrail_fallback_lines() == active.LEGACY_GUARDRAIL_FALLBACK_LINES


def test_guardrail_fallback_lines_en_returns_english(official):
    _activate("en", official)
    for line in active.guardrail_fallback_lines():
        assert "¿" not in line and "¡" not in line
        assert line not in active.LEGACY_GUARDRAIL_FALLBACK_LINES


def test_guardrail_fallback_lines_missing_slot_returns_legacy():
    active.set_active_bundle(_bare_bundle())
    assert active.guardrail_fallback_lines() == active.LEGACY_GUARDRAIL_FALLBACK_LINES


def test_guardrail_fallback_lines_wrong_type_returns_legacy():
    bundle = LocaleBundle(
        code="xx", tier=TIER_OFFICIAL,
        data={"meta": {"code": "xx"}, "llm": {"guardrail_fallback_lines": "not a list"}},
    )
    active.set_active_bundle(bundle)
    assert active.guardrail_fallback_lines() == active.LEGACY_GUARDRAIL_FALLBACK_LINES


def test_guardrail_fallback_lines_empty_list_returns_legacy():
    bundle = LocaleBundle(
        code="xx", tier=TIER_OFFICIAL,
        data={"meta": {"code": "xx"}, "llm": {"guardrail_fallback_lines": []}},
    )
    active.set_active_bundle(bundle)
    assert active.guardrail_fallback_lines() == active.LEGACY_GUARDRAIL_FALLBACK_LINES


def test_guardrail_fallback_lines_returns_tuple():
    bundle = LocaleBundle(
        code="xx", tier=TIER_OFFICIAL,
        data={"meta": {"code": "xx"}, "llm": {"guardrail_fallback_lines": ["a", "b", "c", "d"]}},
    )
    active.set_active_bundle(bundle)
    assert isinstance(active.guardrail_fallback_lines(), tuple)


# ---------------------------------------------------------------------------
# 2. accumulation_ptt() / accumulation_chat()
# ---------------------------------------------------------------------------


def test_accumulation_ptt_es_matches_legacy(official):
    _activate("es", official)
    assert active.accumulation_ptt() == active.LEGACY_ACCUMULATION_PTT


def test_accumulation_chat_es_matches_legacy(official):
    _activate("es", official)
    assert active.accumulation_chat() == active.LEGACY_ACCUMULATION_CHAT


def test_accumulation_ptt_en_is_english(official):
    _activate("en", official)
    value = active.accumulation_ptt()
    assert "{messages}" in value
    assert "streamer dijo" not in value and "acumulado" not in value


def test_accumulation_chat_en_is_english(official):
    _activate("en", official)
    value = active.accumulation_chat()
    assert "{messages}" in value
    assert "procesabas" not in value and "mensajes del chat" not in value


def test_accumulation_ptt_missing_slot_returns_legacy():
    active.set_active_bundle(_bare_bundle())
    assert active.accumulation_ptt() == active.LEGACY_ACCUMULATION_PTT


def test_accumulation_ptt_template_formats_correctly(official):
    _activate("es", official)
    assert active.accumulation_ptt().format(messages="a; b") == "El streamer dijo (acumulado): a; b"


def test_accumulation_chat_template_formats_correctly(official):
    _activate("es", official)
    assert (
        active.accumulation_chat().format(messages="x | y")
        == "Mientras procesabas, llegaron estos mensajes del chat: x | y"
    )


# ---------------------------------------------------------------------------
# 3. ledger_context_label() / ledger_kira_label()
# ---------------------------------------------------------------------------


def test_ledger_context_label_es_matches_legacy(official):
    _activate("es", official)
    assert active.ledger_context_label() == "contexto"


def test_ledger_kira_label_es_matches_legacy(official):
    _activate("es", official)
    assert active.ledger_kira_label() == "→ Kira"


def test_ledger_context_label_en_is_english(official):
    _activate("en", official)
    assert active.ledger_context_label() == "context"


def test_ledger_kira_label_en_unchanged(official):
    _activate("en", official)
    assert active.ledger_kira_label() == "→ Kira"


def test_ledger_context_label_missing_slot_returns_legacy():
    active.set_active_bundle(_bare_bundle())
    assert active.ledger_context_label() == "contexto"


# ---------------------------------------------------------------------------
# 4. agenda_sanitizer_fallback()
# ---------------------------------------------------------------------------


def test_agenda_sanitizer_fallback_es_matches_legacy(official):
    _activate("es", official)
    assert active.agenda_sanitizer_fallback() == active.LEGACY_AGENDA_SANITIZER_FALLBACK


def test_agenda_sanitizer_fallback_en_is_english(official):
    _activate("en", official)
    value = active.agenda_sanitizer_fallback()
    assert not any(ch in value for ch in "¿¡óáéú")
    assert "quedo" not in value and "matices" not in value


def test_agenda_sanitizer_fallback_missing_slot_returns_legacy():
    active.set_active_bundle(_bare_bundle())
    assert active.agenda_sanitizer_fallback() == active.LEGACY_AGENDA_SANITIZER_FALLBACK


# ---------------------------------------------------------------------------
# 4b. connector_templates() (WU5 D3 — F4 EN parity)
# ---------------------------------------------------------------------------


def test_connector_templates_es_matches_legacy(official):
    _activate("es", official)
    assert active.connector_templates() == active.LEGACY_CONNECTOR_TEMPLATES


def test_connector_templates_en_is_english(official):
    _activate("en", official)
    templates = active.connector_templates()
    # F4: the EN locale must ship its OWN slot, not fall back to the es legacy
    # tuple (which would leak Spanish into English-mode output).
    assert templates != active.LEGACY_CONNECTOR_TEMPLATES, "EN must not fall back to the es pool"
    for t in templates:
        assert not any(ch in t for ch in "¿¡óáéúñ"), f"connector template not English: {t}"
        assert t not in active.LEGACY_CONNECTOR_TEMPLATES
        assert "{tema}" in t, "every EN template keeps the {tema} placeholder"


def test_connector_templates_en_matches_es_length(official):
    # Both official locales ship the same number of floor templates.
    _activate("es", official)
    es_len = len(active.connector_templates())
    _activate("en", official)
    assert len(active.connector_templates()) == es_len, "es/en connector pools must be parallel"


def test_connector_templates_missing_slot_returns_legacy():
    active.set_active_bundle(_bare_bundle())
    assert active.connector_templates() == active.LEGACY_CONNECTOR_TEMPLATES


def test_connector_templates_returns_tuple(official):
    _activate("en", official)
    assert isinstance(active.connector_templates(), tuple)


def test_connector_templates_es_no_rioplatense_markers(official):
    """Owner decision (neutral_spanish_20260809): Kira must stop speaking
    Rioplatense/Argentine voseo. The es connector FLOOR pool (agenda-return
    transitions) must carry neutral Spanish, no voseo/rioplatense markers."""
    import re

    _activate("es", official)
    markers = re.compile(r"\bChe\b|\bDale\b|\bposta\b|\bvos\b|\btenés\b|\bmirá\b", re.IGNORECASE)
    for t in active.connector_templates():
        assert not markers.search(t), f"connector template carries a rioplatense marker: {t!r}"


# ---------------------------------------------------------------------------
# 4c. grounding_rules() / editorial_card_instruction()
#     (grounding_authority_temporal_humility)
#
# grounding_rules carries a LEGACY es default like every other llm.* slot:
# a total i18n failure must degrade to the Spanish rule, never to silence.
# editorial_card_instruction deliberately has NO legacy here — its fallback
# lives next to the renderer (EditorialCard.DEFAULT_PROMPT_INSTRUCTION), so
# the accessor returns "" and the caller keeps the card-local default.
# ---------------------------------------------------------------------------


def test_grounding_rules_es_matches_legacy(official):
    _activate("es", official)
    assert active.grounding_rules() == active.LEGACY_GROUNDING_RULES


def test_grounding_rules_en_is_english(official):
    _activate("en", official)
    rules = active.grounding_rules()
    assert rules != active.LEGACY_GROUNDING_RULES, "EN must not fall back to the es rules"
    assert not any(ch in rules for ch in "¿¡óáéúíñ")
    assert "cutoff" in rules


def test_grounding_rules_missing_slot_returns_legacy():
    # Fail-soft: a community bundle without the slot still gets the es rule
    # rather than losing the grounding guard entirely.
    active.set_active_bundle(_bare_bundle())
    assert active.grounding_rules() == active.LEGACY_GROUNDING_RULES


def test_editorial_card_instruction_es_matches_card_default(official):
    # Anti-drift: the es slot is the canonical text the renderer falls back to
    # (same lockstep trick test_i18n_persona uses for settings.SYSTEM_PROMPT).
    from opencohost.core.editorial.editorial_cards import EditorialCard

    _activate("es", official)
    assert active.editorial_card_instruction() == EditorialCard.DEFAULT_PROMPT_INSTRUCTION


def test_editorial_card_instruction_en_is_english(official):
    _activate("en", official)
    instruction = active.editorial_card_instruction()
    assert instruction
    assert not any(ch in instruction for ch in "¿¡óáéúíñ")


def test_editorial_card_instruction_missing_slot_returns_empty(official):
    # "" is the documented signal for "no locale text — keep the card default".
    active.set_active_bundle(_bare_bundle())
    assert active.editorial_card_instruction() == ""


# ---------------------------------------------------------------------------
# 5. ptt_wrapper() / live_voice_wrapper() / ptt_history_wrapper()
# ---------------------------------------------------------------------------


def test_ptt_wrapper_es_matches_legacy(official):
    _activate("es", official)
    assert active.ptt_wrapper() == active.LEGACY_PTT_WRAPPER
    assert active.ptt_wrapper().format(text="hola") == "El streamer acaba de decir (PTT): hola"


def test_ptt_wrapper_en_formats_without_keyerror(official):
    _activate("en", official)
    value = active.ptt_wrapper()
    assert "dijo" not in value
    assert value.format(text="hi") == "The streamer just said (PTT): hi"


def test_live_voice_wrapper_es_matches_legacy(official):
    _activate("es", official)
    assert active.live_voice_wrapper() == active.LEGACY_LIVE_VOICE_WRAPPER
    assert active.live_voice_wrapper().format(text="hola") == "El streamer acaba de decir: hola"


def test_live_voice_wrapper_differs_from_ptt_wrapper(official):
    """Pins the one-slot-vs-two decision: LiveVoice and PTT stay separate."""
    _activate("es", official)
    assert active.live_voice_wrapper() != active.ptt_wrapper()


def test_live_voice_wrapper_en_formats_without_keyerror(official):
    _activate("en", official)
    value = active.live_voice_wrapper()
    assert value.format(text="hi") == "The streamer just said: hi"


def test_ptt_history_wrapper_es_matches_legacy(official):
    _activate("es", official)
    assert active.ptt_history_wrapper() == active.LEGACY_PTT_HISTORY_WRAPPER
    assert active.ptt_history_wrapper().format(text="hola") == "El streamer dijo (PTT): hola"


def test_ptt_history_wrapper_differs_from_ptt_wrapper(official):
    """Pins the byte-identity deviation: controller keeps its own 'dijo' wording."""
    _activate("es", official)
    assert active.ptt_history_wrapper() != active.ptt_wrapper()


def test_ptt_history_wrapper_en_formats_without_keyerror(official):
    _activate("en", official)
    value = active.ptt_history_wrapper()
    assert value.format(text="hi") == "The streamer said (PTT): hi"


def test_typed_history_wrapper_es_matches_legacy(official):
    _activate("es", official)
    assert active.typed_history_wrapper() == active.LEGACY_TYPED_HISTORY_WRAPPER
    assert active.typed_history_wrapper().format(text="hola") == "El streamer escribió: hola"


def test_typed_history_wrapper_differs_from_ptt_history_wrapper(official):
    """B1 (turn provenance): a typed turn must not claim it came through PTT."""
    _activate("es", official)
    assert active.typed_history_wrapper() != active.ptt_history_wrapper()


def test_typed_history_wrapper_en_formats_without_keyerror(official):
    _activate("en", official)
    value = active.typed_history_wrapper()
    assert "escribió" not in value
    assert value.format(text="hi") == "The streamer typed: hi"


def test_ptt_wrapper_missing_slot_returns_legacy():
    active.set_active_bundle(_bare_bundle())
    assert active.ptt_wrapper() == active.LEGACY_PTT_WRAPPER


def test_live_voice_wrapper_missing_slot_returns_legacy():
    active.set_active_bundle(_bare_bundle())
    assert active.live_voice_wrapper() == active.LEGACY_LIVE_VOICE_WRAPPER


def test_ptt_history_wrapper_missing_slot_returns_legacy():
    active.set_active_bundle(_bare_bundle())
    assert active.ptt_history_wrapper() == active.LEGACY_PTT_HISTORY_WRAPPER


def test_typed_history_wrapper_missing_slot_returns_legacy():
    active.set_active_bundle(_bare_bundle())
    assert active.typed_history_wrapper() == active.LEGACY_TYPED_HISTORY_WRAPPER


# ---------------------------------------------------------------------------
# 6. Engine integration — _guardrail_fallback_line()
# ---------------------------------------------------------------------------


def test_engine_guardrail_fallback_returns_es_line_for_es_locale(official):
    _activate("es", official)
    motor = _make_motor()
    assert motor._guardrail_fallback_line("direct") in active.LEGACY_GUARDRAIL_FALLBACK_LINES


def test_engine_guardrail_fallback_returns_en_line_for_en_locale(official):
    _activate("en", official)
    motor = _make_motor()
    line = motor._guardrail_fallback_line("ptt")
    assert line and line not in active.LEGACY_GUARDRAIL_FALLBACK_LINES


def test_engine_guardrail_fallback_rotates_through_first_four_generic_lines(official):
    """D4 (memoria_quality_20260717): generic blocks round-robin over the FIRST
    FOUR lines only — the memory-flavored 5th line is reserved for R9 self-ID
    blocks and never appears in generic rotation."""
    _activate("es", official)
    motor = _make_motor()
    generic = active.LEGACY_GUARDRAIL_FALLBACK_LINES[:4]
    memory_line = active.LEGACY_GUARDRAIL_FALLBACK_LINES[4]
    results = [motor._guardrail_fallback_line("direct") for _ in range(len(generic) + 1)]
    assert set(results[:4]) == set(generic)
    assert memory_line not in results        # never emitted by generic rotation
    assert results[4] == results[0]          # wraps at 4, not 5


def test_engine_guardrail_fallback_memory_line_only_on_self_id_reason(official):
    """The memory-flavored line is returned ONLY when the block reason is the R9
    AI-self-ID rule; a non-self-ID reason still round-robins the generic four."""
    _activate("es", official)
    motor = _make_motor()
    r9_reason = "Non-negotiable violation [no_ai_self_identification]: contains AI self-ID"
    assert (
        motor._guardrail_fallback_line("direct", r9_reason)
        == active.LEGACY_GUARDRAIL_FALLBACK_LINES[4]
    )
    other = motor._guardrail_fallback_line(
        "direct", "Non-negotiable violation [never_promise]: contains a promise"
    )
    assert other in active.LEGACY_GUARDRAIL_FALLBACK_LINES[:4]


def test_engine_guardrail_fallback_en_lines_pass_output_guard(official):
    from opencohost.config.validation import output_guard

    _activate("en", official)
    # Iterate the FULL en bundle directly (not via the rotating selector, which
    # after D4 never emits the 5th memory-flavored line for a generic reason) so
    # every line — including that 5th line — is verified guard-safe.
    lines = active.guardrail_fallback_lines()
    assert len(lines) == 5  # 4 generic + 1 memory-flavored (D4)
    for line in lines:
        allowed, reason = output_guard(line, source="direct")
        assert allowed, f"en fallback line blocked: {line!r} — {reason}"


def test_engine_guardrail_fallback_agenda_source_returns_empty(official):
    _activate("en", official)
    motor = _make_motor()
    assert motor._guardrail_fallback_line("kira-agenda-main") == ""


def test_engine_guardrail_fallback_returns_empty_for_agenda_prefix_variants(official):
    _activate("es", official)
    motor = _make_motor()
    assert motor._guardrail_fallback_line("kira-agenda-secondary") == ""
    assert motor._guardrail_fallback_line("kira-agenda") == ""


# ---------------------------------------------------------------------------
# 7. Engine integration — _flush_accumulation()
# ---------------------------------------------------------------------------


def test_flush_accumulation_ptt_es_uses_es_template(official):
    _activate("es", official)
    motor = _make_motor()
    motor._accumulation_buffer.append((time.time(), "hello streamer", "ptt"))
    result = motor._flush_accumulation()
    assert result.startswith("El streamer dijo (acumulado):")


def test_flush_accumulation_chat_es_uses_es_template(official):
    _activate("es", official)
    motor = _make_motor()
    motor._accumulation_buffer.append((time.time(), "nice", "chat"))
    result = motor._flush_accumulation()
    assert result.startswith("Mientras procesabas")


def test_flush_accumulation_ptt_en_uses_en_template(official):
    _activate("en", official)
    motor = _make_motor()
    motor._accumulation_buffer.append((time.time(), "hello streamer", "ptt"))
    result = motor._flush_accumulation()
    assert "dijo" not in result
    assert "hello streamer" in result


def test_flush_accumulation_chat_en_uses_en_template(official):
    _activate("en", official)
    motor = _make_motor()
    motor._accumulation_buffer.append((time.time(), "nice", "chat"))
    result = motor._flush_accumulation()
    assert "procesabas" not in result
    assert "nice" in result


def test_flush_accumulation_multi_entry_separator_correctness(official):
    _activate("es", official)
    motor = _make_motor()
    now = time.time()
    motor._accumulation_buffer.extend([
        (now, "msg1", "ptt"),
        (now, "msg2", "ptt"),
        (now, "chat1", "chat"),
        (now, "chat2", "chat"),
    ])
    result = motor._flush_accumulation()
    assert "msg1; msg2" in result
    assert "chat1 | chat2" in result


def test_flush_accumulation_other_source_is_language_neutral(official):
    _activate("en", official)
    motor = _make_motor()
    motor._accumulation_buffer.append((time.time(), "payload", "accumulated"))
    result = motor._flush_accumulation()
    assert result.startswith("[accumulated]")


# ---------------------------------------------------------------------------
# 8. Engine integration — _build_ledger_line()
# ---------------------------------------------------------------------------


def test_build_ledger_line_es_matches_legacy_format(official):
    _activate("es", official)
    result = llm_engine.MotorVocalIA._build_ledger_line("hola", "así es")
    assert result == "contexto: hola → Kira: así es"


def test_build_ledger_line_en_uses_english_labels(official):
    _activate("en", official)
    result = llm_engine.MotorVocalIA._build_ledger_line("hola", "así es")
    assert result.startswith("context:")
    assert "contexto" not in result


def test_build_ledger_line_missing_slot_falls_back_to_es_labels():
    active.set_active_bundle(_bare_bundle())
    result = llm_engine.MotorVocalIA._build_ledger_line("hola", "así es")
    assert result.startswith("contexto:")


# ---------------------------------------------------------------------------
# 9. Engine integration — _sanitize_agenda_output()
# ---------------------------------------------------------------------------


def test_sanitize_agenda_output_fallback_es_matches_legacy(official):
    _activate("es", official)
    motor = _make_motor()
    result = motor._sanitize_agenda_output("hasta luego, nos vemos")
    assert result == active.LEGACY_AGENDA_SANITIZER_FALLBACK


def test_sanitize_agenda_output_fallback_en_is_english(official):
    _activate("en", official)
    motor = _make_motor()
    result = motor._sanitize_agenda_output("hasta luego, nos vemos")
    assert result and result != active.LEGACY_AGENDA_SANITIZER_FALLBACK


def test_sanitize_agenda_output_clean_text_passes_through(official):
    _activate("es", official)
    motor = _make_motor()
    assert motor._sanitize_agenda_output("un texto cualquiera sin cierres raros") == (
        "un texto cualquiera sin cierres raros"
    )


def test_sanitize_agenda_output_empty_input_returns_empty(official):
    _activate("es", official)
    motor = _make_motor()
    assert motor._sanitize_agenda_output("") == ""


# ---------------------------------------------------------------------------
# 10. Engine integration — voice_control.py wrapper call sites
# ---------------------------------------------------------------------------


def test_voice_control_ptt_flush_busy_uses_ptt_wrapper(official):
    from opencohost.ui import voice_control

    _activate("en", official)
    panel = object.__new__(voice_control.VoiceControlPanel)
    panel._ptt_lock = threading.Lock()
    panel._ptt_buffer = "hello there"
    panel._logger = logging.getLogger("test")
    panel._motor_ia = MagicMock()
    panel._motor_ia.is_speaking = True
    panel._motor_ia.is_processing = False
    panel._on_log = lambda *a, **k: None

    panel._flush_ptt_buffer()

    panel._motor_ia.enqueue.assert_called_once()
    sent_text = panel._motor_ia.enqueue.call_args.args[0]
    assert sent_text == active.ptt_wrapper().format(text="hello there")


def test_voice_control_ptt_flush_idle_uses_ptt_wrapper(official):
    from opencohost.ui import voice_control

    _activate("en", official)
    panel = object.__new__(voice_control.VoiceControlPanel)
    panel._ptt_lock = threading.Lock()
    panel._ptt_buffer = "hello there team"
    panel._logger = logging.getLogger("test")
    panel._motor_ia = MagicMock()
    panel._motor_ia.is_speaking = False
    panel._motor_ia.is_processing = False
    panel._motor_ia.command_queue = queue.Queue()
    panel._on_log = lambda *a, **k: None

    panel._flush_ptt_buffer()

    # WU1 (memoria_quality_20260717) made the idle PTT flush enqueue a 3-tuple
    # (command, payload, history_text); tolerant unpack mirrors run()'s consumer.
    _cmd, payload, *_rest = panel._motor_ia.command_queue.get_nowait()
    assert payload == active.ptt_wrapper().format(text="hello there team")


# ---------------------------------------------------------------------------
# 11. Engine integration — kira_agenda_controller.py history_text
# ---------------------------------------------------------------------------


def test_controller_ptt_history_text_uses_own_wrapper_es(official):
    _activate("es", official)
    controller = KiraAgendaController()
    topic = controller.add_topic("Tema de fondo", approved=True)
    controller.queue_topic(topic.id)
    controller.enable()
    controller.next_action()
    controller.mark_generation_accepted()
    controller.mark_speech_complete()

    action = controller.next_action(ptt_text="probemos esto")

    assert action.history_text == "El streamer dijo (PTT): probemos esto"


def test_controller_ptt_history_text_uses_own_wrapper_en(official):
    _activate("en", official)
    controller = KiraAgendaController()
    topic = controller.add_topic("background topic", approved=True)
    controller.queue_topic(topic.id)
    controller.enable()
    controller.next_action()
    controller.mark_generation_accepted()
    controller.mark_speech_complete()

    action = controller.next_action(ptt_text="let's try this")

    assert action.history_text == active.ptt_history_wrapper().format(text="let's try this")
    assert "dijo" not in action.history_text


# ---------------------------------------------------------------------------
# 12. Manifest schema tests
# ---------------------------------------------------------------------------


def test_es_manifest_has_guardrail_fallback_lines():
    es = build_chain("es", discover_bundles(official_locales_dir(), "official"))
    lines = resolve(es, "llm.guardrail_fallback_lines")
    assert isinstance(lines, list) and len(lines) == 5  # +1 memory-flavored line (D4)


def test_es_manifest_guardrail_fallback_lines_are_spanish():
    import yaml

    es = build_chain("es", discover_bundles(official_locales_dir(), "official"))
    lines = resolve(es, "llm.guardrail_fallback_lines")
    assert any(any(ch in line for ch in "¿¡") for line in lines)
    # YAML round-trip: accented/¿¡ chars must survive re-serialization on Windows.
    dumped = yaml.dump(lines, allow_unicode=True)
    reparsed = yaml.safe_load(dumped)
    assert reparsed == lines


def test_en_manifest_has_guardrail_fallback_lines():
    en = build_chain("en", discover_bundles(official_locales_dir(), "official"))
    lines = resolve(en, "llm.guardrail_fallback_lines")
    assert isinstance(lines, list) and len(lines) == 5  # +1 memory-flavored line (D4)


def test_en_manifest_guardrail_fallback_lines_are_english():
    en = build_chain("en", discover_bundles(official_locales_dir(), "official"))
    lines = resolve(en, "llm.guardrail_fallback_lines")
    assert all("¿" not in line and "¡" not in line for line in lines)


def test_es_manifest_has_accumulation_slots():
    es = build_chain("es", discover_bundles(official_locales_dir(), "official"))
    assert "{messages}" in resolve(es, "llm.scaffolding.accumulation_ptt")
    assert "{messages}" in resolve(es, "llm.scaffolding.accumulation_chat")


def test_en_manifest_has_accumulation_slots():
    en = build_chain("en", discover_bundles(official_locales_dir(), "official"))
    assert "{messages}" in resolve(en, "llm.scaffolding.accumulation_ptt")
    assert "{messages}" in resolve(en, "llm.scaffolding.accumulation_chat")


def test_es_manifest_has_ledger_labels():
    es = build_chain("es", discover_bundles(official_locales_dir(), "official"))
    assert resolve(es, "llm.scaffolding.ledger_context_label") == "contexto"
    assert resolve(es, "llm.scaffolding.ledger_kira_label") == "→ Kira"


def test_en_manifest_has_ledger_labels():
    en = build_chain("en", discover_bundles(official_locales_dir(), "official"))
    assert resolve(en, "llm.scaffolding.ledger_context_label") == "context"


def test_es_manifest_has_agenda_sanitizer_fallback():
    es = build_chain("es", discover_bundles(official_locales_dir(), "official"))
    assert resolve(es, "llm.agenda_sanitizer_fallback")


def test_en_manifest_has_agenda_sanitizer_fallback():
    es = build_chain("es", discover_bundles(official_locales_dir(), "official"))
    en = build_chain("en", discover_bundles(official_locales_dir(), "official"))
    assert resolve(en, "llm.agenda_sanitizer_fallback")
    assert resolve(en, "llm.agenda_sanitizer_fallback") != resolve(es, "llm.agenda_sanitizer_fallback")


def test_es_manifest_has_wrapper_slots():
    es = build_chain("es", discover_bundles(official_locales_dir(), "official"))
    assert "{text}" in resolve(es, "llm.scaffolding.ptt_wrapper")
    assert "{text}" in resolve(es, "llm.scaffolding.live_voice_wrapper")
    assert "{text}" in resolve(es, "llm.scaffolding.ptt_history_wrapper")
    assert "{text}" in resolve(es, "llm.scaffolding.typed_history_wrapper")


def test_en_manifest_has_wrapper_slots():
    en = build_chain("en", discover_bundles(official_locales_dir(), "official"))
    assert "{text}" in resolve(en, "llm.scaffolding.ptt_wrapper")
    assert "{text}" in resolve(en, "llm.scaffolding.live_voice_wrapper")
    assert "{text}" in resolve(en, "llm.scaffolding.ptt_history_wrapper")
    assert "{text}" in resolve(en, "llm.scaffolding.typed_history_wrapper")
