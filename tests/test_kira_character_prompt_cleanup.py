"""kira-character-prompt-cleanup-20261008 (T1-T3).

Prompt-trace evidence (2026-10-08): Kira disclosed her creator verbatim from the
persona, assumed a live stream ("tu stream corre") while the owner was not
streaming, and replied as neutral analysis because the warmth block claimed
priority over the whole persona and the owner policy ranked answering above
banter. These tests pin the cleanup through the real assembly path.
"""
from __future__ import annotations

import pytest

from opencohost.core.context import prompt_assembler as pa
from opencohost.i18n import active as i18n_active
from tests.test_warmth_prompt import (  # noqa: F401  (autouse fixture re-exported)
    _activate,
    _assemble,
    _reset_active,
)

LOCALES = ("es", "en")
WARM_LEVELS = ("empathetic", "in_character", "free", "sentimental")


def _custom_system(locale: str, warmth: str = "cold", extended: bool = False) -> str:
    setup = _assemble((locale, "direct", True, extended), persona_custom=True, warmth=warmth)
    return setup.messages[0]["content"]


# ---------------------------------------------------------------------------
# T1: core rules
# ---------------------------------------------------------------------------

NO_INTERNALS = {
    "es": ("no expliques cómo funcionas por dentro", "prompts"),
    "en": ("never explain how you work inside", "prompts"),
}
CREATOR_ONLY_IF_ASKED = {
    "es": "creador u origen solo si te lo preguntan directamente",
    "en": "creator or origin only when asked directly",
}
NO_STREAM_ASSUMPTION = {
    "es": ("no supongas un stream, público o chat", "transmisión activa"),
    "en": ("do not assume a stream, audience or chat", "stream is live"),
}
DIRECT_ADDRESS = {
    "es": ('"el host"', '"el operador"', "de tú"),
    "en": ('"the host"', '"the operator"', "directly"),
}


@pytest.mark.parametrize("locale", LOCALES)
def test_assembled_custom_prompt_carries_no_internals_and_creator_rules(locale):
    low = _custom_system(locale).lower()
    for needle in NO_INTERNALS[locale]:
        assert needle in low, (locale, needle)
    assert CREATOR_ONLY_IF_ASKED[locale] in low, locale


@pytest.mark.parametrize("locale", LOCALES)
def test_core_rules_do_not_presuppose_a_live_stream(locale):
    _activate(locale)
    low = i18n_active.core_rules(False).lower()
    assert "streamer" not in low, locale
    assert "la plataforma que construyó" not in low
    assert "the platform your streamer built" not in low
    for needle in NO_STREAM_ASSUMPTION[locale]:
        assert needle in low, (locale, needle)


@pytest.mark.parametrize("locale", LOCALES)
def test_core_rules_address_the_person_directly(locale):
    _activate(locale)
    low = i18n_active.core_rules(False).lower()
    for needle in DIRECT_ADDRESS[locale]:
        assert needle in low, (locale, needle)


def test_builtin_core_rules_track_the_en_manifest(monkeypatch):
    _activate("en")
    en = (i18n_active.core_rules(False), i18n_active.core_rules(True))
    from opencohost.i18n.contract import SlotNotFound

    def boom(*a, **k):
        raise SlotNotFound("llm.core_rules")

    monkeypatch.setattr(i18n_active, "resolve", boom)
    assert (i18n_active.core_rules(False), i18n_active.core_rules(True)) == en


# ---------------------------------------------------------------------------
# T2: warmth adds to the persona instead of replacing it
# ---------------------------------------------------------------------------

ADDS = {
    "es": ("se suma a tu personalidad sarcástica", "nunca la reemplaza"),
    "en": ("adds to your sarcastic persona", "never replaces it"),
}
NARROW_OVERRIDE = {
    "es": "solo anula líneas de personalidad que pidan frialdad",
    "en": "overrides only persona lines that ask for coldness",
}


@pytest.mark.parametrize("locale", LOCALES)
@pytest.mark.parametrize("level", WARM_LEVELS)
def test_warm_block_adds_to_persona_and_overrides_only_cold_lines(locale, level):
    system = _custom_system(locale, warmth=level)
    block = i18n_active.warmth_block(level)
    assert block in system
    low = block.lower()
    assert "prioridad" not in low and "priority" not in low, (locale, level)
    assert "este tono manda sobre cualquier" not in low
    assert "this tone overrides any" not in low
    for needle in ADDS[locale]:
        assert needle in low, (locale, level, needle)
    assert NARROW_OVERRIDE[locale] in low, (locale, level)


# ---------------------------------------------------------------------------
# T3: owner policy keeps answer-first, delivered in the persona's voice
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("extended", (False, True))
def test_owner_policy_is_answer_first_in_the_persona_voice(extended):
    policy = pa.OWNER_RESPONSE_POLICY_EXTENDED if extended else pa.OWNER_RESPONSE_POLICY_CONCISE
    system = _custom_system("es", extended=extended)
    assert policy in system
    assert "prioritize answering the current request over maintaining banter" not in policy
    assert "answer the current request first" in policy
    assert "humor carry the delivery" in policy
    assert "useful does not mean neutral or bland" in policy
    # A useful answer still wins over engagement.
    assert "never let humor, novelty, or conversational engagement replace a useful answer" in policy.lower()


def test_owner_policy_leaves_length_wording_alone():
    assert "Treat brevity as a default, not a restriction against requested detail." in (
        pa.OWNER_RESPONSE_POLICY_CONCISE
    )
    assert "without brevity constraints" in pa.OWNER_RESPONSE_POLICY_EXTENDED
