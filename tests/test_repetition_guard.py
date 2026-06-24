"""Unit specs for the chat-reactive repetition guard (pure module).

RED before ``opencohost/core/repetition_guard.py`` exists (collection import error).
The guard catches the two repetition modes seen in the real RF3 run:
verbatim duplicates and synonym-swap TEMPLATE repetition that a content-token
overlap detector (the agenda guard) provably misses.
"""
from __future__ import annotations

import re

from opencohost.core.repetition_guard import detect_repetition, DEFAULT_CONFIG


def _content_token_jaccard(a: str, b: str) -> float:
    """Mirror of the agenda guard's signal: Jaccard over 4+char content tokens."""
    ta = {t for t in re.findall(r"\w+", a.lower()) if len(t) >= 4}
    tb = {t for t in re.findall(r"\w+", b.lower()) if len(t) >= 4}
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def test_exact_duplicate_is_caught():
    prior = "La sorpresa siempre es un buen regalo para el chat."
    assert detect_repetition(prior, [prior]).reason == "exact_dup"


def test_scaffold_synonym_swap_is_caught():
    prior = "Cada partida es un abismo sin fin."
    cand = "Cada derrota es un abismo sin salida."
    assert detect_repetition(cand, [prior]).reason == "scaffold_repeat"


def test_structural_detector_catches_what_token_overlap_misses():
    # The agenda guard scores by content-token overlap (>=0.78 gate). The swapped
    # nouns are exactly what it measures, so it provably misses this pair — while
    # the structural skeleton detector catches it.
    prior = "Cada partida es un abismo sin fin."
    cand = "Cada derrota es un abismo sin salida."
    assert _content_token_jaccard(cand, prior) < 0.78
    assert detect_repetition(cand, [prior]).is_repetitive is True


def test_real_log_template_family_is_caught():
    # The dominant failure from the real run: "La verdad es que X es un abismo Y".
    prior = "La verdad es que la confusion es un abismo sin fin."
    cand = "La verdad es que la ironia es un abismo sin salida."
    assert detect_repetition(cand, [prior]).reason == "scaffold_repeat"


def test_normalization_noise_is_caught():
    prior = "la sorpresa siempre es un buen regalo para el chat"
    cand = "  La  SORPRESA siempre es un buen REGALO para el chat  "
    assert detect_repetition(cand, [prior]).reason == "exact_dup"


def test_near_exact_suffix_noise_is_caught():
    prior = (
        "El nivel de juego de este equipo durante toda esta larga "
        "partida fue realmente impresionante"
    )
    cand = prior + " posta"
    assert detect_repetition(cand, [prior]).is_repetitive is True


def test_varied_lines_do_not_trigger():
    prior = "Uy, casi se cae la torre, que jugada arriesgada."
    cand = "Bueno, parece que el rival no va a rendirse facil hoy."
    assert detect_repetition(cand, [prior]).is_repetitive is False


def test_short_interjection_below_min_chars_is_ignored():
    assert detect_repetition("Jajaja", ["Jajaja"]).is_repetitive is False


def test_first_occurrence_is_never_repetitive():
    assert detect_repetition("Cada partida es un abismo sin fin total.", []).is_repetitive is False


def test_low_slot_shared_frame_is_not_a_false_positive():
    # Both collapse to the generic skeleton "eso es una #" (1 content slot).
    # The min_slots>=2 rail must prevent flagging genuinely different lines that
    # only share a short function-word frame.
    prior = "Eso es una verdadera locura total."
    cand = "Eso es una completa ridiculez absurda."
    assert detect_repetition(cand, [prior]).is_repetitive is False
