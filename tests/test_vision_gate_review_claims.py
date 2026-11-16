"""vision-and-prompt-leaks-20261018 T2: external review claims V1-V11.

A test that passes on the unchanged gate refutes the claim (or pins a documented
policy); a failing one is a confirmed bug.
"""
from __future__ import annotations

import pytest

from opencohost.core.vision.gate import (
    CompositeGate,
    RuleVisionGate,
    VisionDecision,
    is_requested_look,
    look_mode,
    look_request_reasons,
)


def _decide(text, source="direct"):
    return RuleVisionGate().decide(text, source)


# ----------------------------------------------------------------- T2: V1-V11

def test_v1_refuted_comma_after_mira_is_suppressed():
    assert _decide("Kira, mira, te cuento algo").needs_vision is False
    assert _decide("Mira, te cuento algo").needs_vision is False


def test_v2_policy_screen_content_fallback_is_documented_behavior():
    # look_request_reasons documents "an unsuppressed rule gate hit, or a
    # screen/image/clip reference". Only the vision-off notice reads it.
    assert look_request_reasons("Ayer subí un video al canal", "direct") == "screen_ref"


class _Fixed:
    def __init__(self, needs, mode):
        self._d = VisionDecision(needs, ("fixed",), 0.0, mode)

    def decide(self, text, source):
        return self._d


def test_v3_composite_keeps_the_secondary_mode():
    d = CompositeGate(_Fixed(False, "default"), _Fixed(True, "clip")).decide("x", "direct")
    assert d.needs_vision is True
    assert d.mode == "clip"


def test_v3_composite_primary_mode_still_wins_when_primary_fires():
    assert CompositeGate(_Fixed(True, "text"), _Fixed(True, "clip")).decide("x", "direct").mode == "text"


def test_v5_policy_pinned_bare_video_fires():
    # Pinned by test_vision_gate.py (live-session-fixes L5): "te gustó el video".
    assert _decide("Tengo que editar el video").needs_vision is True


def test_v6_policy_pinned_state_noun_is_optional():
    # "cuántas pociones me quedan" is a pinned heldout look; pociones is not a state noun.
    assert _decide("¿cuántas pociones me quedan?").needs_vision is True
    assert _decide("¿Cuántos cafés tengo?").needs_vision is True


def test_v7_policy_pinned_generic_object_nouns_fire():
    assert _decide("what is that thing in the corner?").needs_vision is True
    assert _decide("Esa cosa no tiene sentido").needs_vision is True


def test_v8_policy_bare_says_is_text_mode_on_a_look_turn():
    assert look_mode("my friend says it's funny") == "text"


def test_v9_policy_meta_words_suppress_real_requests():
    # Documented: suppressors win because precision matters more than recall.
    assert _decide("Mira esto que me pasó en el directo").needs_vision is False
    assert _decide("Lee lo que dice el cartel, chat").needs_vision is False


@pytest.mark.parametrize("text", ["ves ese enemigo", "estás viendo la pantalla"])
def test_v10_refuted_for_these_examples_they_already_fire(text):
    assert _decide(text).needs_vision is True


@pytest.mark.parametrize("text", ["ves esto", "observa"])
def test_v10_policy_remaining_examples_are_recall_additions(text):
    assert _decide(text).needs_vision is False


def test_v11_read_request_counts_as_a_requested_look():
    assert is_requested_look(("read_request",), "léeme eso") is True


def test_v11_policy_ui_ref_alone_is_not_a_request():
    assert is_requested_look(("ui_ref",), "el minimapa") is False
