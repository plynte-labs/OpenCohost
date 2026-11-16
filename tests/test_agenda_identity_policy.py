"""The R9 owner toggle also gates the agenda character contract's
``assistant_identity`` class (owner decision 2026-10-01). Every other
character-contract class stays enforced regardless of the policy."""

from __future__ import annotations

from opencohost.config import guardrail_policy
from opencohost.smart_aggregator.kira_agenda_controller import KiraAgendaController

_IDENTITY_LINE = "Bueno, soy una IA y aun asi este tema me parece fascinante."
_OFFER_LINE = "Estoy lista para continuar con el siguiente tema."


def test_identity_line_breaks_character_while_r9_is_enabled():
    result = KiraAgendaController.breaks_character(_IDENTITY_LINE)
    assert result is not None
    assert result.subtype == "assistant_identity"


def test_identity_line_passes_when_r9_is_disabled():
    assert guardrail_policy.set_disabled(["no_ai_self_identification"]) == []
    assert KiraAgendaController.breaks_character(_IDENTITY_LINE) is None


def test_other_character_classes_stay_enforced_when_r9_is_disabled():
    assert guardrail_policy.set_disabled(["no_ai_self_identification"]) == []
    result = KiraAgendaController.breaks_character(_OFFER_LINE)
    assert result is not None
    assert result.subtype == "assistant_offer"
