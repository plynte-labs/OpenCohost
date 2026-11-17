"""Opt-in topic progression (OPENCOHOST_AGENDA_PROGRESSION): move deck + spent ledger.

Flag off must leave the prompt byte-identical (pinned by
tests/test_i18n_agenda_prompt_golden.py); these tests cover the flag-on path.
"""
from __future__ import annotations

import pytest

from opencohost.config import settings
from opencohost.i18n import active
from opencohost.i18n.registry import discover_bundles, official_locales_dir
from opencohost.i18n.startup import resolve_active_bundle
from opencohost.smart_aggregator.kira_agenda_controller import (
    AgendaState,
    KiraAgendaController,
    TopicStatus,
)

import re

_SPANISH_ONLY = re.compile(
    r"[áéíóúñ¿¡]|\b(?:el|los|las|una|que|para|con|del|pero|sin|tema|usted|eres|puedes)\b",
    re.IGNORECASE,
)


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


@pytest.fixture(autouse=True)
def _legacy_prompt_by_default(monkeypatch):
    """Progression is ON by default (owner decision 2026-11-16); these tests
    characterize the legacy prompt unless they opt in through `flag_on`."""
    monkeypatch.setattr(settings, "AGENDA_PROGRESSION_ENABLED", False, raising=False)


@pytest.fixture
def flag_on(monkeypatch):
    # Enabled with a blank variant resolves to the v1 baseline these tests pin.
    monkeypatch.setattr(settings, "AGENDA_PROGRESSION_VARIANT", "", raising=False)
    monkeypatch.setattr(settings, "AGENDA_PROGRESSION_ENABLED", True, raising=False)


def _controller(official, locale="es", turns=5, topic_title="Tema de prueba"):
    _activate(locale, official)
    c = KiraAgendaController(max_turns_per_topic=turns)
    topic = c.add_topic(topic_title, angle="angulo", approved=True)
    topic.status = TopicStatus.ACTIVE
    c.active_topic = topic
    c.state = AgendaState.WAITING_SIGNAL
    return c


def _live_prompt(c, turns_spoken):
    """Live continuation prompt for a given number of already-debited turns."""
    c.active_topic.turns_spoken = turns_spoken
    c.state = AgendaState.WAITING_SIGNAL
    action = c.next_action()
    assert action.kind == "enqueue"
    return action.prompt


def _moves_in(prompt, locale_moves):
    return [m for m in locale_moves if m in prompt]


# 1 -------------------------------------------------------------------------

def test_n5_continuation_moves_are_pairwise_distinct(official, flag_on):
    c = _controller(official, turns=5)
    moves = active.agenda_progression_moves()
    seen = []
    for k in range(1, 5):  # turn 0 is topic_open; 1..4 are continuations
        found = _moves_in(_live_prompt(c, k), moves)
        assert len(found) == 1, (k, found)
        seen.append(found[0])
    assert len(set(seen)) == len(seen)


def test_n20_no_repeat_before_exhaustion_and_not_within_last_four(official, flag_on):
    c = _controller(official, turns=20)
    moves = active.agenda_progression_moves()
    pool = len(moves)
    seq = []
    for k in range(1, 20):
        found = _moves_in(_live_prompt(c, k), moves)
        assert len(found) == 1
        seq.append(found[0])
    first_cycle = seq[:pool]
    assert len(set(first_cycle)) == len(first_cycle)
    for i in range(pool, len(seq)):
        assert seq[i] not in seq[max(0, i - 4):i]


# 2 -------------------------------------------------------------------------

def test_prefetch_preview_matches_live_prompt_and_does_not_advance(official, flag_on):
    c = _controller(official, turns=20)
    moves = active.agenda_progression_moves()
    for k in (1, 2, 5, 9, 15):
        # While turn k-1 is on air the preview targets the next turn, index k.
        c.active_topic.turns_spoken = k - 1
        c.state = AgendaState.SPEAKING
        snapshot = (c.active_topic.turns_spoken, c.state, c._pending_turns_spoken)
        preview = c.prefetch_action_after_current_speech()
        again = c.prefetch_action_after_current_speech()
        assert (c.active_topic.turns_spoken, c.state, c._pending_turns_spoken) == snapshot
        assert preview.prompt == again.prompt
        live = _live_prompt(c, k)
        p_moves, l_moves = _moves_in(preview.prompt, moves), _moves_in(live, moves)
        assert len(p_moves) == 1 and p_moves == l_moves


# 3 -------------------------------------------------------------------------

def test_spent_ledger_replaces_raw_outputs_and_lists_recurring_words(official, flag_on):
    c = _controller(official, turns=20)
    long_a = ("la melancolía industrial " * 30) + "final de la primera salida"
    out_b = "otra salida con melancolía distinta " + ("relleno " * 40) + "terminó aquí mismo"
    c.record_accepted_output(long_a)
    c.record_accepted_output(out_b)
    prompt = _live_prompt(c, 2)
    assert long_a.lower() not in prompt
    assert long_a.lower()[:250] not in prompt
    assert "terminó aquí mismo" in prompt  # the tail of the previous output
    assert "melancolía" in prompt  # used in 2 accepted outputs
    assert "distinta" not in prompt.split("ÚLTIMAS LÍNEAS")[-1]  # used once only


def test_spent_list_is_capped(official, flag_on):
    c = _controller(official, turns=20)
    words = [f"palabra{chr(97 + i)}{chr(97 + i)}" for i in range(26)]
    c.record_accepted_output(" ".join(words))
    c.record_accepted_output(" ".join(words))
    prompt = _live_prompt(c, 2)
    prefix = active.agenda_progression()["spent_line"].split("{words}")[0]
    line = next(l for l in prompt.splitlines() if l.startswith(prefix))
    listed = line[len(prefix):].split(", ")
    assert 0 < len(listed) <= 20


# 4 -------------------------------------------------------------------------

def test_ledger_resets_on_topic_change(official, flag_on):
    c = _controller(official, turns=20)
    c.record_accepted_output("quimera silenciosa quimera")
    c.record_accepted_output("otra quimera silenciosa")
    assert "quimera" in _live_prompt(c, 2)
    other = c.add_topic("Otro tema distinto", angle="b", approved=True)
    other.status = TopicStatus.ACTIVE
    c.active_topic = other
    prompt = _live_prompt(c, 1)
    assert "quimera" not in prompt


def test_ledger_never_ingests_chat_or_ptt_and_is_not_exposed(official, flag_on):
    c = _controller(official, turns=20)
    c.state = AgendaState.WAITING_SIGNAL
    c.blocks_since_chat_check = 99
    chat = c.next_action(compact_chat="zanahoria zanahoria zanahoria")
    assert chat.source == "chat"
    c.state = AgendaState.WAITING_SIGNAL
    c.next_action(ptt_text="berenjena berenjena berenjena")
    c.state = AgendaState.WAITING_SIGNAL
    prompt = _live_prompt(c, 2)
    assert "zanahoria zanahoria" not in prompt.split("ÚLTIMAS LÍNEAS")[-1]
    assert "berenjena" not in prompt.split("ÚLTIMAS LÍNEAS")[-1]
    c.record_accepted_output("quimera silenciosa quimera")
    c.record_accepted_output("otra quimera silenciosa")
    assert "quimera" not in repr(c.get_metrics())


def test_ledger_not_serialized_in_topics(official, flag_on):
    c = _controller(official, turns=20)
    c.record_accepted_output("quimera silenciosa quimera")
    c.record_accepted_output("otra quimera silenciosa")
    assert "quimera" not in repr(c.active_topic.__dict__)


# 5 -------------------------------------------------------------------------

def test_flag_off_prompt_has_no_move_and_keeps_raw_last_lines(official, monkeypatch):
    monkeypatch.setattr(settings, "AGENDA_PROGRESSION_ENABLED", False, raising=False)
    c = _controller(official, turns=20)
    c.record_accepted_output("primera salida completa")
    c.record_accepted_output("segunda salida completa")
    prompt = _live_prompt(c, 2)
    assert _moves_in(prompt, active.agenda_progression_moves()) == []
    assert "- primera salida completa\n- segunda salida completa" in prompt
    assert active.agenda_instructions()["topic_continue"] in prompt


def test_flag_default_is_on(monkeypatch):
    monkeypatch.delenv("OPENCOHOST_AGENDA_PROGRESSION", raising=False)
    assert settings._resolve_agenda_progression_variant() == "auto"
    assert settings._resolve_agenda_progression() is True


# 6 -------------------------------------------------------------------------

def test_open_closing_and_chat_turns_carry_no_move(official, flag_on):
    moves = active.agenda_progression_moves()
    c = _controller(official, turns=5)
    c.state = AgendaState.IDLE
    c.active_topic.status = TopicStatus.QUEUED
    c.active_topic = None
    opened = c.next_action()
    assert opened.kind == "enqueue"
    assert _moves_in(opened.prompt, moves) == []
    c.state = AgendaState.WAITING_SIGNAL
    c.active_topic.turns_spoken = 5
    closing = c.next_action()
    assert _moves_in(closing.prompt, moves) == []
    c2 = _controller(official, turns=20)
    c2.blocks_since_chat_check = 99
    c2.active_topic.turns_spoken = 3
    chat = c2.next_action(compact_chat="hola gente")
    assert chat.source == "chat"
    assert _moves_in(chat.prompt, moves) == []


def test_n1_topic_has_no_continuation_move(official, flag_on):
    moves = active.agenda_progression_moves()
    c = _controller(official, turns=1)
    c.state = AgendaState.IDLE
    c.active_topic.status = TopicStatus.QUEUED
    c.active_topic = None
    opened = c.next_action()
    assert _moves_in(opened.prompt, moves) == []
    c.state = AgendaState.WAITING_SIGNAL
    c.active_topic.turns_spoken = 1
    assert _moves_in(c.next_action().prompt, moves) == []
    c.state = AgendaState.SPEAKING
    c.active_topic.status = TopicStatus.ACTIVE
    assert _moves_in(c.prefetch_action_after_current_speech().prompt, moves) == []


def test_move_pools_are_clean_per_locale(official):
    _activate("en", official)
    en = active.agenda_progression_moves()
    assert 12 <= len(en) <= 16
    for m in en:
        assert not _SPANISH_ONLY.search(m), m
    _activate("es", official)
    es = active.agenda_progression_moves()
    assert 12 <= len(es) <= 16
    assert len(es) == len(en)
    from tests.test_i18n_neutral_spanish import _voseo_in
    for m in es:
        assert not _voseo_in(m), m


def test_en_locale_prompt_carries_english_move_and_wording(official, flag_on):
    c = _controller(official, locale="en", turns=20)
    moves = active.agenda_progression_moves()
    prompt = _live_prompt(c, 3)
    assert len(_moves_in(prompt, moves)) == 1
    assert "Sigue" not in prompt


# N2-S2 fixes -----------------------------------------------------------------

def test_spent_list_excludes_topic_title_and_angle_words(official, flag_on):
    c = _controller(official, turns=20, topic_title="Minecraft en la industria gaming")
    c.record_accepted_output("minecraft industria gaming espejo cambia todo")
    c.record_accepted_output("el espejo de minecraft en la industria gaming")
    prompt = _live_prompt(c, 2)
    spent_line = next(ln for ln in prompt.splitlines() if ln.startswith("Palabras que ya se usaron"))
    for word in ("minecraft", "industria", "gaming"):
        assert word not in spent_line.lower()
    assert "espejo" in spent_line


def test_spent_words_ignores_english_contraction_fragments():
    from opencohost.smart_aggregator.kira_agenda_controller import spent_words

    outs = [
        "It doesn't matter, people wouldn't notice and they haven't tried. Couldn't be simpler, shouldn't be harder.",
        "She doesn't think so; you wouldn't either, we haven't seen it. Couldn't, shouldn't, really.",
    ]
    words = spent_words(outs)
    for frag in ("doesn", "wouldn", "haven", "couldn", "shouldn"):
        assert frag not in words


def test_spent_words_drops_extended_function_words():
    from opencohost.smart_aggregator.kira_agenda_controller import spent_words

    assert spent_words(["además mientras tienes espejo", "además mientras tienes espejo"]) == ["espejo"]


def test_spent_words_exclude_parameter():
    from opencohost.smart_aggregator.kira_agenda_controller import spent_words

    outs = ["espejo muralla", "espejo muralla"]
    assert spent_words(outs, exclude={"muralla"}) == ["espejo"]


@pytest.mark.parametrize("size", range(2, 8))
def test_small_decks_are_permutations_without_close_reuse(size):
    from opencohost.smart_aggregator.kira_agenda_controller import _deck_order

    window = min(4, size // 2)
    for t in range(50):
        tid = f"topic-{t}"
        seq = []
        for cycle in range(5):
            order = _deck_order(tid, cycle, size)
            assert sorted(order) == list(range(size))
            seq.extend(order)
        last = {}
        for pos, move in enumerate(seq):
            if move in last:
                assert pos - last[move] > window
            last[move] = pos


@pytest.mark.parametrize("template", ["Sigue con {x}: {move}", "Sigue { con {move}"])
def test_malformed_continue_template_falls_back_to_legacy(official, flag_on, monkeypatch, template):
    c = _controller(official, turns=20)
    legacy = active.agenda_instructions()["topic_continue"]
    prog = dict(active.agenda_progression())
    prog["continue"] = template
    monkeypatch.setattr(active, "agenda_progression", lambda: prog)
    prompt = _live_prompt(c, 2)
    assert legacy in prompt
