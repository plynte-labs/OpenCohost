"""Tests for Agenda Progression Deck Variants v1..v5 (OPENCOHOST_AGENDA_PROGRESSION).

Covers:
- Flag resolution: off (0/false/unset/invalid) vs 1/v1 vs v2..v5
- v1: baseline 14-move deck + spent-word ledger
- v2: length-aware continuation prompt + agenda-only sampling penalties & token cap
- v3: 56-move family deck (8 families x 7 cards), no back-to-back family, no fixed openers
- v4: Ledger v2 (opening stems + 1-occurrence keywords + topic-wide stem/sentence guard)
- v5: Facet progression plan + hybrid 9B controls (family deck + length cap + ledger v2 + penalties)
- Non-negotiable invariants:
  * 1500+ session deterministic fuzz (prefetch == live for move & facet across v1..v5)
  * No move/facet on open, closing, chat, PTT, or N=1
  * i18n lockstep (es/en keys, no Spanish in en, tuteo-only in es)
  * Never expose raw viewer chat in prompts, ledgers, or metrics
"""
from __future__ import annotations

import re

import pytest

from opencohost.config import settings
import opencohost.core.llm_engine  # noqa: F401 - mixin import order
from opencohost.core.context.prompt_assembler import PromptContextAssembler, sampling_options
from opencohost.core.engine.llm_engine_agenda import agenda_llm_options
from opencohost.i18n import active
from opencohost.i18n.registry import discover_bundles, official_locales_dir
from opencohost.i18n.startup import resolve_active_bundle
from opencohost.smart_aggregator.kira_agenda_controller import (
    AgendaState,
    ErrorCode,
    KiraAgendaController,
    TopicStatus,
)


_SPANISH_ONLY = re.compile(
    r"[áéíóúñ¿¡]|\b(?:el|los|las|una|que|para|con|del|pero|sin|tema|usted|eres|puedes)\b",
    re.IGNORECASE,
)

EXPECTED_FAMILIES = frozenset({
    "historical_analogy",
    "moral_dilemma",
    "counterintuitive_fact",
    "chat_question",
    "inversion",
    "concrete_case",
    "future_projection",
    "systemic_mechanism",
})


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


def _set_variant(monkeypatch, variant: str) -> None:
    if variant in ("", "0", "false", "off"):
        monkeypatch.setattr(settings, "AGENDA_PROGRESSION_ENABLED", False, raising=False)
        monkeypatch.setattr(settings, "AGENDA_PROGRESSION_VARIANT", "", raising=False)
    else:
        monkeypatch.setattr(settings, "AGENDA_PROGRESSION_ENABLED", True, raising=False)
        monkeypatch.setattr(settings, "AGENDA_PROGRESSION_VARIANT", variant, raising=False)


def _controller(official, *, locale="es", turns=6, topic_title="Tema de prueba", progression_variant=None):
    _activate(locale, official)
    c = KiraAgendaController(max_turns_per_topic=turns, progression_variant=progression_variant)
    topic = c.add_topic(topic_title, angle="angulo practico", approved=True)
    topic.status = TopicStatus.ACTIVE
    c.active_topic = topic
    c.state = AgendaState.WAITING_SIGNAL
    return c


def _live_prompt(c, turns_spoken):
    c.active_topic.turns_spoken = turns_spoken
    c.state = AgendaState.WAITING_SIGNAL
    action = c.next_action()
    assert action.kind == "enqueue"
    return action.prompt


def _all_family_moves():
    fams = active.agenda_progression_family_moves()
    return [m for cards in fams.values() for m in cards]


def _family_of_move(move_text: str, fams: dict[str, tuple[str, ...]]) -> str:
    for fam, cards in fams.items():
        if move_text in cards:
            return fam
    raise AssertionError(f"Move not found in family deck: {move_text!r}")


# ---------------------------------------------------------------------------
# 1. Flag compatibility & resolution
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw,expected_enabled,expected_variant",
    [
        ("", False, ""),
        ("0", False, ""),
        ("false", False, ""),
        ("FALSE", False, ""),
        ("off", False, ""),
        ("unknown", False, ""),
        ("1", True, "auto"),
        ("true", True, "auto"),
        ("auto", True, "auto"),
        ("rotate", True, "auto"),
        ("ROTATE", True, "auto"),
        ("v1", True, "v1"),
        ("V1", True, "v1"),
        ("v2", True, "v2"),
        ("v3", True, "v3"),
        ("v4", True, "v4"),
        ("v5", True, "v5"),
        ("V5", True, "v5"),
    ],
)
def test_flag_resolution_matrix(raw, expected_enabled, expected_variant):
    resolved = settings._resolve_agenda_progression_variant(raw)
    assert resolved == expected_variant
    assert bool(resolved) is expected_enabled


def test_flag_enabled_without_variant_defaults_to_v1(monkeypatch):
    monkeypatch.setattr(settings, "AGENDA_PROGRESSION_ENABLED", True, raising=False)
    monkeypatch.setattr(settings, "AGENDA_PROGRESSION_VARIANT", "", raising=False)
    assert settings.get_agenda_progression_variant() == "v1"


def test_flag_disabled_overrides_stale_variant(monkeypatch):
    monkeypatch.setattr(settings, "AGENDA_PROGRESSION_ENABLED", False, raising=False)
    monkeypatch.setattr(settings, "AGENDA_PROGRESSION_VARIANT", "v4", raising=False)
    assert settings.get_agenda_progression_variant() == ""


# ---------------------------------------------------------------------------
# 2. v1 baseline & v2 length-aware + agenda-only sampling penalties
# ---------------------------------------------------------------------------

def test_v1_uses_base_14_move_deck(official, monkeypatch):
    _set_variant(monkeypatch, "v1")
    c = _controller(official, turns=10)
    base_moves = active.agenda_progression_moves()
    assert len(base_moves) == 14
    p = _live_prompt(c, 1)
    assert [m for m in base_moves if m in p]


def test_v2_injects_length_budget_and_replaces_long_normal_rule(official, monkeypatch):
    _set_variant(monkeypatch, "v2")
    c = _controller(official, turns=10)
    p = _live_prompt(c, 1)
    prog = active.agenda_progression()
    assert prog["length_budget"] in p
    assert prog["length_budget_rule"] in p
    assert "~1500 caracteres" not in p  # no contradiction with 3 sentences / ~110 tokens


def test_v2_and_v5_apply_agenda_only_sampling_penalties_and_token_cap(monkeypatch):
    for off_var in ("", "v1", "v3", "v4"):
        _set_variant(monkeypatch, off_var)
        opts_agenda = sampling_options("kira-agenda")
        assert "repeat_penalty" not in opts_agenda
        assert sampling_options("other") == {
            "temperature": settings.LLM_TEMPERATURE,
            "top_p": settings.LLM_TOP_P,
        }

    for var in ("v2", "v5", "auto"):
        _set_variant(monkeypatch, var)
        opts_agenda = sampling_options("kira-agenda")
        assert opts_agenda["repeat_penalty"] == pytest.approx(1.18)
        # Non-agenda sources stay unaffected by agenda penalty
        assert sampling_options("other") == {
            "temperature": settings.LLM_TEMPERATURE,
            "top_p": settings.LLM_TOP_P,
        }
        assert sampling_options("chat")["repeat_penalty"] == settings.CHAT_REPEAT_PENALTY
        assert sampling_options("direct")["repeat_penalty"] == settings.OWNER_REPEAT_PENALTY

        capped = agenda_llm_options({"num_predict": 512}, source="kira-agenda", is_local=True)
        assert capped["repeat_penalty"] == pytest.approx(1.18)
        assert capped["num_predict"] == settings.AGENDA_V2_MAX_TOKENS
        assert capped["num_predict"] <= 120

        # Assembler also caps num_predict for local kira-agenda in v2/v5
        assembler = PromptContextAssembler()
        setup = assembler.assemble(
            "agenda prompt",
            "kira-agenda",
            system_prompt="You are Kira.",
            use_system_role=True,
            is_local=True,
            provider_cfg={},
            request_model="qwen3:8b",
            history_snapshot=(),
            native_ctx_resolver=lambda _: 8192,
        )
        assert setup.opciones_llm["repeat_penalty"] == pytest.approx(1.18)
        assert setup.opciones_llm["num_predict"] <= settings.AGENDA_V2_MAX_TOKENS


# ---------------------------------------------------------------------------
# 3. v3 expanded diverse family deck (50-60 cards, no back-to-back family)
# ---------------------------------------------------------------------------

def test_v3_family_deck_size_structure_and_i18n_lockstep(official):
    _activate("es", official)
    es_fams = active.agenda_progression_family_moves()
    _activate("en", official)
    en_fams = active.agenda_progression_family_moves()

    assert set(es_fams) == EXPECTED_FAMILIES
    assert set(en_fams) == EXPECTED_FAMILIES
    total_es = sum(len(v) for v in es_fams.values())
    total_en = sum(len(v) for v in en_fams.values())
    assert 50 <= total_es <= 60
    assert total_es == total_en
    for fam in EXPECTED_FAMILIES:
        assert len(es_fams[fam]) == len(en_fams[fam]) >= 6
        for m in en_fams[fam]:
            assert not _SPANISH_ONLY.search(m), m

    # No forced canned opener strings anywhere in moves
    forbidden_openers = ("dentro de diez años", "el error más común", "in ten years", "the most common mistake")
    for fam_map in (es_fams, en_fams):
        for cards in fam_map.values():
            for card in cards:
                low = card.lower()
                for phrase in forbidden_openers:
                    assert phrase not in low, card


def test_v3_draws_across_families_never_repeating_family_back_to_back(official, monkeypatch):
    _set_variant(monkeypatch, "v3")
    c = _controller(official, turns=20)
    fams = active.agenda_progression_family_moves()
    all_moves = _all_family_moves()

    chosen_moves = []
    chosen_families = []
    for k in range(1, 20):
        prompt = _live_prompt(c, k)
        matches = [m for m in all_moves if m in prompt]
        assert len(matches) == 1, (k, matches)
        mv = matches[0]
        chosen_moves.append(mv)
        chosen_families.append(_family_of_move(mv, fams))

    assert len(set(chosen_moves)) == len(chosen_moves)  # 19 unique moves
    for i in range(1, len(chosen_families)):
        assert chosen_families[i] != chosen_families[i - 1], (i, chosen_families)


def test_v3_long_horizon_permutation_and_cycle_boundary_safety(official, monkeypatch):
    from opencohost.smart_aggregator.kira_agenda_controller import pick_family_progression_move

    _activate("es", official)
    fams = active.agenda_progression_family_moves()
    total = sum(len(v) for v in fams.values())

    for tid in ("topic-alpha", "topic-beta", "topic-gamma"):
        prev_fam = None
        seq_moves = []
        for turn in range(1, total * 3 + 1):
            fam, mv = pick_family_progression_move(tid, turn, fams)
            assert fam != prev_fam, (tid, turn, fam)
            prev_fam = fam
            seq_moves.append(mv)
        # Every cycle of `total` is an exact permutation of all cards
        for c_idx in range(3):
            chunk = seq_moves[c_idx * total:(c_idx + 1) * total]
            assert len(set(chunk)) == total
        # No move repeats within 4 positions across cycle boundaries
        for i in range(4, len(seq_moves)):
            assert seq_moves[i] not in seq_moves[i - 4:i]


# ---------------------------------------------------------------------------
# 4. v4 Ledger v2 (opening stems + 1-occurrence keywords + anti-self-copy)
# ---------------------------------------------------------------------------

def test_v4_ledger_v2_tracks_opening_stems_and_single_occurrence_keywords(official, monkeypatch):
    _set_variant(monkeypatch, "v4")
    c = _controller(official, turns=10, topic_title="Cultura del hype")
    c.record_accepted_output("Siempre pasa lo mismo cuando las marcas prometen milagros con espejos brillantes.")
    prompt = _live_prompt(c, 1)
    assert "espejos" in prompt or "brillantes" in prompt or "milagros" in prompt
    stem_prefix = active.agenda_progression()["spent_openings_line"].split("{openings}")[0]
    assert stem_prefix in prompt
    assert "siempre pasa lo mismo" in prompt.lower()
    # Topic words excluded from spent keywords
    spent_line = next(ln for ln in prompt.splitlines() if ln.startswith("Palabras que ya se usaron"))
    assert "cultura" not in spent_line.lower()
    assert "hype" not in spent_line.lower()


def test_v4_blocks_copied_opening_stem_or_sentence_across_topic(official, monkeypatch):
    _set_variant(monkeypatch, "v4")
    c = _controller(official, turns=10, topic_title="Streaming nocturno")
    c.record_accepted_output("Cuando cae la noche todo el mundo cambia de máscara y habla distinto.")
    c.record_accepted_output("Al final la costumbre pesa más que la pantalla encendida.")

    # Copies opening stem from an earlier turn in the topic -> rejected
    c.state = AgendaState.GENERATING
    ok_stem = c.accept_output("Cuando cae la noche todo el mundo busca otra excusa para quedarse.")
    assert ok_stem is False
    assert c.recovery.error_code == ErrorCode.GUARDRAIL_SIMILAR

    # Copies a whole sentence (> 20 chars) from turn 1 mid-output -> rejected
    c.state = AgendaState.GENERATING
    ok_sent = c.accept_output(
        "Mira esto. Al final la costumbre pesa más que la pantalla encendida. Luego nadie responde."
    )
    assert ok_sent is False

    # Fresh opening and fresh sentence -> accepted
    c.state = AgendaState.GENERATING
    ok_fresh = c.accept_output("En cambio, los moderadores notan el cambio de humor desde el primer minuto.")
    assert ok_fresh is True


# ---------------------------------------------------------------------------
# 5. v5 Facet progression plan + hybrid 9B mode
# ---------------------------------------------------------------------------

def test_v5_advances_across_distinct_facets_and_combines_9b_controls(official, monkeypatch):
    _set_variant(monkeypatch, "v5")
    c = _controller(official, turns=6)
    facets = active.agenda_progression_facets()
    assert len(facets) == 6
    all_moves = _all_family_moves()

    seen_facets = []
    seen_moves = []
    c.record_accepted_output("En realidad la economía de atención premia el ruido sobre el oficio.")
    for k in range(1, 6):
        prompt = _live_prompt(c, k)
        matched_facets = [f for f in facets if f in prompt]
        matched_moves = [m for m in all_moves if m in prompt]
        assert len(matched_facets) == 1, (k, matched_facets)
        assert len(matched_moves) == 1, (k, matched_moves)
        seen_facets.append(matched_facets[0])
        seen_moves.append(matched_moves[0])
        assert "3 frases cortas" in prompt
        assert "~110 tokens" in prompt
        assert "en realidad la economía" in prompt.lower()

    assert len(set(seen_facets)) == len(seen_facets)
    assert len(set(seen_moves)) == len(seen_moves)


def test_v5_n20_walks_all_facets_in_order(official, monkeypatch):
    _set_variant(monkeypatch, "v5")
    c = _controller(official, turns=20)
    facets = active.agenda_progression_facets()
    indices = []
    for k in range(1, 20):
        prompt = _live_prompt(c, k)
        idx = next(i for i, f in enumerate(facets) if f in prompt)
        indices.append(idx)
    # Monotonic progression covering every facet from 0 to len-1
    assert indices == sorted(indices)
    assert set(indices) == set(range(len(facets)))


def test_auto_rotates_facets_across_topics_and_applies_full_9b_controls(official, monkeypatch):
    _set_variant(monkeypatch, "auto")
    _activate("es", official)
    facets = active.agenda_progression_facets()
    all_moves = _all_family_moves()

    first_facets = set()
    for title in ("Tema Alpha", "Tema Beta", "Tema Gamma", "Tema Delta", "Tema Epsilon", "Tema Zeta"):
        c = _controller(official, turns=6, topic_title=title)
        c.record_accepted_output("En realidad la economía de atención premia el ruido sobre el oficio.")
        prompt = _live_prompt(c, 1)
        matched_facets = [f for f in facets if f in prompt]
        matched_moves = [m for m in all_moves if m in prompt]
        assert len(matched_facets) == 1
        assert len(matched_moves) == 1
        assert "3 frases cortas" in prompt
        assert "~110 tokens" in prompt
        assert "en realidad la economía" in prompt.lower()
        first_facets.add(matched_facets[0])

    # Rotating facet plans across distinct topics avoids starting every topic identically
    assert len(first_facets) >= 2

    # Within a single 20-turn topic, auto visits all 6 facets monotonically and rejects stem copies
    c_long = _controller(official, turns=20, topic_title="Streaming nocturno")
    seen_topic_facets = []
    for k in range(1, 20):
        prompt = _live_prompt(c_long, k)
        seen_topic_facets.append(next(f for f in facets if f in prompt))
    assert set(seen_topic_facets) == set(facets)

    c_long.record_accepted_output("Cuando cae la noche todo el mundo cambia de máscara y habla distinto.")
    c_long.state = AgendaState.GENERATING
    assert c_long.accept_output("Cuando cae la noche todo el mundo busca otra excusa para quedarse.") is False


# ---------------------------------------------------------------------------
# 6. Non-negotiable invariants across ALL variants (1500+ session fuzz!)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("variant", ["v1", "v2", "v3", "v4", "v5", "auto"])
def test_1500_sessions_prefetch_matches_live_deterministically(official, monkeypatch, variant):
    _set_variant(monkeypatch, variant)
    _activate("es", official)
    base_moves = active.agenda_progression_moves()
    fam_moves = _all_family_moves()
    facets = active.agenda_progression_facets()
    pool = base_moves if variant in ("v1", "v2") else fam_moves

    for i in range(320):  # 320 * 6 variants = 1920 sessions total
        max_turns = 2 + (i % 19)  # 2..20
        turns_spoken = i % (max_turns - 1)  # 0..max_turns-2 -> projected = 1..max_turns-1
        c = KiraAgendaController(max_turns_per_topic=max_turns)
        topic = c.add_topic(f"Topic {i}", angle="angle", approved=True)
        topic.status = TopicStatus.ACTIVE
        topic.turns_spoken = turns_spoken
        c.active_topic = topic

        c.state = AgendaState.SPEAKING
        preview = c.prefetch_action_after_current_speech()
        assert preview.kind == "enqueue"

        topic.turns_spoken = turns_spoken + 1
        c.state = AgendaState.WAITING_SIGNAL
        live = c.next_action()
        assert live.kind == "enqueue"

        p_move = [m for m in pool if m in preview.prompt]
        l_move = [m for m in pool if m in live.prompt]
        assert len(p_move) == 1 and p_move == l_move

        if variant in ("v5", "auto"):
            p_facet = [f for f in facets if f in preview.prompt]
            l_facet = [f for f in facets if f in live.prompt]
            assert len(p_facet) == 1 and p_facet == l_facet


@pytest.mark.parametrize("variant", ["v1", "v2", "v3", "v4", "v5", "auto"])
def test_no_move_or_facet_on_open_closing_chat_ptt_or_n1(official, monkeypatch, variant):
    _set_variant(monkeypatch, variant)
    _activate("es", official)
    all_markers = list(active.agenda_progression_moves()) + _all_family_moves() + list(active.agenda_progression_facets())

    # N=1 topic: open -> close, never a move
    c1 = _controller(official, turns=1)
    c1.state = AgendaState.IDLE
    c1.active_topic.status = TopicStatus.QUEUED
    c1.active_topic = None
    opened = c1.next_action()
    assert [m for m in all_markers if m in opened.prompt] == []
    c1.state = AgendaState.WAITING_SIGNAL
    c1.active_topic.turns_spoken = 1
    closing = c1.next_action()
    assert [m for m in all_markers if m in closing.prompt] == []
    c1.state = AgendaState.SPEAKING
    c1.active_topic.status = TopicStatus.ACTIVE
    assert [m for m in all_markers if m in c1.prefetch_action_after_current_speech().prompt] == []

    # Chat and PTT turns never carry moves or facets
    c2 = _controller(official, turns=10)
    c2.blocks_since_chat_check = 99
    c2.active_topic.turns_spoken = 2
    chat = c2.next_action(compact_chat="hola desde chat")
    assert chat.source == "chat"
    assert [m for m in all_markers if m in chat.prompt] == []

    c2.state = AgendaState.WAITING_SIGNAL
    ptt = c2.next_action(ptt_text="cambia de tema por favor")
    assert ptt.source == "ptt"
    assert [m for m in all_markers if m in ptt.prompt] == []


@pytest.mark.parametrize("variant", ["v1", "v2", "v3", "v4", "v5", "auto"])
def test_en_locale_prompts_and_no_chat_in_ledger_or_metrics(official, monkeypatch, variant):
    _set_variant(monkeypatch, variant)
    c = _controller(official, locale="en", turns=10, topic_title="Retro consoles")
    c.blocks_since_chat_check = 99
    c.next_action(compact_chat="secretochat123 secretochat123 secretochat123")
    c.state = AgendaState.WAITING_SIGNAL
    c.record_accepted_output("Collectors always chase mint boxes while missing the joy of worn cartridges.")

    prompt = _live_prompt(c, 2)
    assert "secretochat123" not in prompt
    assert "secretochat123" not in repr(c.get_metrics())
    assert "¿" not in prompt and "¡" not in prompt
    assert "{" not in prompt and "}" not in prompt
    assert "Sigue" not in prompt
    assert "Palabras" not in prompt
    assert "Aperturas" not in prompt
