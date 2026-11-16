"""vision-and-prompt-leaks-20261018 T1: the live CV phrasing triggers a look."""
from __future__ import annotations

import pytest

from opencohost.core.vision.gate import (
    RuleVisionGate,
    references_screen_content,
)

LIVE_CV = "Acabo de hacer mi CV y puedes verlo, o sea... ¡Hidlésolo! ¿Qué opina sobre él?"


def _decide(text, source="direct"):
    return RuleVisionGate().decide(text, source)


# ----------------------------------------------------------------- T1: live case

def test_live_cv_phrasing_triggers_a_look():
    d = _decide(LIVE_CV)
    assert d.needs_vision is True
    assert "look_verb" in d.reason_codes


@pytest.mark.parametrize("text", ["¿puedes verla?", "oye puedes verlos un segundo", "puedes verlas?", "léelo por favor"])
def test_clitic_see_and_read_forms_trigger(text):
    assert _decide(text).needs_vision is True


@pytest.mark.parametrize("text", ["Acabo de hacer mi CV", "mi currículum quedó listo", "revisa el documento", "my curriculum vitae"])
def test_document_cue_is_screen_content(text):
    assert references_screen_content(text) is True


@pytest.mark.parametrize("text", ["¿puedes ver lo que te digo?", "cuéntame un chiste"])
def test_document_cue_does_not_overreach(text):
    assert references_screen_content(text) is False


def test_cv_mention_alone_does_not_trigger_a_look():
    assert _decide("Mi CV tiene dos páginas").needs_vision is False


def test_chat_never_triggers_the_clitic_form():
    assert _decide(LIVE_CV, "chat").needs_vision is False
