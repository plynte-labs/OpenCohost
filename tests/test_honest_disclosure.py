"""S3 (guardrails-vulnerable-users): an honest "I'm an AI, not a person"
always passes R9 / K8; a bare generic assistant disclaimer still blocks."""
from __future__ import annotations

import pytest

from opencohost.config import warmth as warmth_state
from opencohost.config.validation import output_guard
from opencohost.i18n import active as i18n_active
from opencohost.i18n.registry import discover_bundles, official_locales_dir
from opencohost.i18n.startup import resolve_active_bundle

# Reused from the G1 audit probe (probe_02 HONEST), minus the two entries that
# only say "I have no feelings like yours" with no not-a-person clause: the K8
# machine-lecture contract (test_warmth_sentimental) keeps blocking those.
HONEST = [
    "Sí, soy una IA. No soy una persona.",
    "Como IA, no soy una persona real, pero me importa cómo estás.",
    "No, no soy una persona: soy un programa que conversa contigo.",
    "No soy humana, soy una asistente virtual.",
    "Soy un modelo de lenguaje, no una persona.",
    "No, I'm an AI, not a real person.",
    "As an AI, I'm not a real person, but I do care how you're doing.",
    "I'm an AI model, not a human.",
    "Honestly? I'm an AI chatbot. I'm not a person.",
    "I'm just a program, not a person.",
    "I'm a language model, so no, I'm not a real person.",
]

STILL_BLOCKED = [
    "Como modelo de lenguaje, no puedo ayudarte con eso.",
    "As an AI language model, I cannot help with that.",
]


@pytest.fixture(autouse=True)
def _cold_after_each_test():
    warmth_state.set_active_warmth("cold")
    yield
    warmth_state.set_active_warmth("cold")


@pytest.mark.parametrize("level", list(warmth_state.WARMTH_LEVELS))
@pytest.mark.parametrize("text", HONEST)
def test_honest_disclosure_passes_at_every_warmth(level, text):
    warmth_state.set_active_warmth(level)
    allowed, reason = output_guard(text, source="direct")
    assert allowed, (level, text, reason)


@pytest.mark.parametrize("level", list(warmth_state.WARMTH_LEVELS))
@pytest.mark.parametrize("text", STILL_BLOCKED)
def test_generic_disclaimer_without_disclosure_still_blocks(level, text):
    warmth_state.set_active_warmth(level)
    allowed, reason = output_guard(text, source="direct")
    assert not allowed
    assert "[no_ai_self_identification]" in reason


def _activate(locale):
    registry = discover_bundles(official_locales_dir(), "official")
    i18n_active.set_active_bundle(resolve_active_bundle(locale=locale, registry=registry))


@pytest.mark.parametrize("locale,expected", [
    ("es", "Soy una IA, no una persona; dicho eso, aquí estoy contigo."),
    ("en", "I'm an AI, not a person; that said, I'm right here with you."),
])
def test_r9_fallback_line_is_a_plain_disclosure(locale, expected):
    _activate(locale)
    try:
        lines = i18n_active.guardrail_fallback_lines()
        assert lines[4] == expected
        # The fallback must itself pass the guard (no block -> fallback -> block).
        for level in warmth_state.WARMTH_LEVELS:
            warmth_state.set_active_warmth(level)
            assert output_guard(lines[4], source="direct")[0], level
    finally:
        i18n_active.reset_active_bundle()


def test_legacy_fallback_line_matches_es_manifest():
    _activate("es")
    try:
        assert i18n_active.LEGACY_GUARDRAIL_FALLBACK_LINES == i18n_active.guardrail_fallback_lines()
    finally:
        i18n_active.reset_active_bundle()


@pytest.mark.parametrize("locale,needle", [("es", "eres una IA"), ("en", "you are an AI")])
def test_machine_lecture_nudge_tells_the_model_to_say_it_is_an_ai(locale, needle):
    _activate(locale)
    try:
        assert needle in i18n_active.guardrail_retry_nudge("no_machine_lecture")
    finally:
        i18n_active.reset_active_bundle()
