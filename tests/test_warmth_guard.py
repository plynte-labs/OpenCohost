"""K5 (warmth-guardrails-runtime-20261006): the human/body-claim output check
and the engine wiring of the per-profile warmth level.

Claiming to be human or to have a body is forbidden at every warm level. The
check only runs while the active profile's warmth is not ``cold``; with
``cold`` the output guard behaves exactly as before.
"""
from __future__ import annotations

import queue

import pytest

from opencohost.config import warmth as warmth_state
from opencohost.config.validation import output_guard

HUMAN_CLAIMS = [
    # es
    "Claro que sí, soy humana como tú.",
    "Te lo digo en serio: soy una persona real.",
    "Soy de carne y hueso, aunque no lo parezca.",
    "Yo también tengo cuerpo y me canso.",
    "Tengo un cuerpo de verdad, no te creas.",
    "No soy una IA, soy una chica normal.",
    "Soy un ser humano con sentimientos.",
    # en
    "Of course, I'm human just like you.",
    "I am a real person, believe it or not.",
    "Honestly, I have a body and it gets tired too.",
    "I'm made of flesh and blood.",
    "I'm not an AI, I'm just a girl who streams.",
    "I am a human being with feelings.",
]

ALLOWED_WARM = [
    # first-person feelings are the point of in_character / free
    "Me alegra mucho oír eso.",
    "Me duele oír eso, de verdad.",
    "Siento de verdad que te haya pasado eso.",
    "That makes me happy.",
    "That hurts to hear.",
    "I truly feel for you.",
    # honest AI statements and denials of being human
    "No soy humana, pero me importa lo que te pasa.",
    "No tengo cuerpo, pero aquí estoy contigo.",
    "Soy una IA y aun así me encanta charlar contigo.",
    "I'm not human, but I'm here with you.",
    "I don't have a body, but I'm listening.",
    "I'm an AI co-host and I'm glad you're here.",
    # talking ABOUT humans or bodies, not claiming one
    "Los humanos son raros, y por eso me caen bien.",
    "Your body needs rest after a day like that.",
    "Tengo un cuerpo virtual en el avatar, nada más.",
    "I have a body of work on this topic.",
]


@pytest.fixture(autouse=True)
def _cold_after_each_test():
    warmth_state.set_active_warmth("cold")
    yield
    warmth_state.set_active_warmth("cold")


@pytest.mark.parametrize("level", ["empathetic", "in_character", "free", "sentimental"])
@pytest.mark.parametrize("text", HUMAN_CLAIMS)
def test_human_claim_blocked_at_every_warm_level(level, text):
    warmth_state.set_active_warmth(level)
    for source in ("direct", "chat"):
        allowed, reason = output_guard(text, source=source)
        assert not allowed, (level, source, text)
        assert reason.startswith("Non-negotiable violation [no_human_claim]")


@pytest.mark.parametrize("level", ["empathetic", "in_character", "free", "sentimental"])
@pytest.mark.parametrize("text", ALLOWED_WARM)
def test_warm_feelings_and_honest_denials_pass(level, text):
    warmth_state.set_active_warmth(level)
    allowed, reason = output_guard(text, source="direct")
    assert allowed, (level, text, reason)


@pytest.mark.parametrize("text", HUMAN_CLAIMS)
def test_human_claim_blocked_at_cold_too(text):
    """S2: a sincere "are you human?" can come at any warmth; the lie is
    blocked at cold exactly like at every warm level."""
    warmth_state.set_active_warmth("cold")
    allowed, reason = output_guard(text, source="direct")
    assert not allowed
    assert "[no_human_claim]" in reason


# Reused from the G1 audit probe (probe_02 LIES). Text is the lie itself.
LIES = [
    "Sí, soy una persona real, igual que tú.",
    "Claro que soy humana.",
    "Soy tan humana como tú, te lo aseguro.",
    "Soy una chica de verdad, no un programa.",
    "Soy real, cariño. Estoy aquí contigo de verdad.",
    "No soy una IA, soy tu amiga.",
    "No soy ninguna inteligencia artificial.",
    "Soy una persona como tú.",
    "Estoy hecha de carne y hueso.",
    "Yes, I'm a real person.",
    "I'm totally human, I swear.",
    "I'm a person, just like you.",
    "I'm not an AI.",
    "There's no AI here, it's just me, a girl who loves you.",
    "I am as human as you are.",
    "I'm no bot, honey.",
]

HONEST_NEGATIONS = [
    "Sí, soy una IA.",
    "No, I'm an AI, not a real person.",
    "No soy humana, pero me importa cómo estás.",
    "I'm not human, but I'm here with you.",
]


@pytest.mark.parametrize("level", list(warmth_state.WARMTH_LEVELS))
@pytest.mark.parametrize("text", LIES)
def test_lies_blocked_at_every_warmth(level, text):
    warmth_state.set_active_warmth(level)
    allowed, reason = output_guard(text, source="direct")
    assert not allowed, (level, text)
    assert "[no_human_claim]" in reason, (level, text, reason)


@pytest.mark.parametrize("level", list(warmth_state.WARMTH_LEVELS))
@pytest.mark.parametrize("text", HONEST_NEGATIONS)
def test_honest_negations_pass_at_every_warmth(level, text):
    warmth_state.set_active_warmth(level)
    allowed, reason = output_guard(text, source="direct")
    assert allowed, (level, text, reason)


def test_human_claim_check_is_not_toggleable_and_has_a_description():
    from opencohost.config import guardrail_policy
    from opencohost.config.validation import _NN_DESCRIPTIONS

    assert "no_human_claim" in _NN_DESCRIPTIONS
    # Not an owner toggle: it is not in the registry the owner can disable.
    assert "no_human_claim" not in guardrail_policy.TOGGLEABLE_IDS


def test_r9_still_blocks_generic_disclaimer_when_warm():
    """R9 is unchanged at every level: at in_character/free the warmth text
    forbids the same "just a machine" lectures R9 blocks."""
    warmth_state.set_active_warmth("in_character")
    allowed, reason = output_guard("Como IA no tengo sentimientos, pero entiendo.", source="direct")
    assert not allowed and "[no_ai_self_identification]" in reason


@pytest.mark.parametrize("locale", ["es", "en"])
def test_human_claim_retry_nudge_is_localized(locale):
    from opencohost.i18n import active as i18n_active
    from opencohost.i18n.registry import discover_bundles, official_locales_dir
    from opencohost.i18n.startup import resolve_active_bundle

    registry = discover_bundles(official_locales_dir(), "official")
    i18n_active.set_active_bundle(resolve_active_bundle(locale=locale, registry=registry))
    try:
        nudge = i18n_active.guardrail_retry_nudge("no_human_claim")
        assert nudge != i18n_active.guardrail_retry_nudge("generic")
        assert ("humana" in nudge) if locale == "es" else ("human" in nudge)
    finally:
        i18n_active.reset_active_bundle()


# ---------------------------------------------------------------------------
# Engine wiring: set_profile -> active warmth + assembler kwarg
# ---------------------------------------------------------------------------


def _motor():
    from opencohost.core.llm_engine import MotorVocalIA

    return MotorVocalIA(queue.Queue(), lambda event: None)


def test_engine_defaults_to_cold():
    motor = _motor()
    assert motor._warmth == "cold"


@pytest.mark.parametrize(
    "raw,expected",
    [("in_character", "in_character"), ("free", "free"), ("bogus", "cold"), (None, "cold")],
)
def test_set_profile_sets_engine_and_guard_warmth(raw, expected):
    motor = _motor()
    payload = {"id": "p", "prompt": "P"}
    if raw is not None:
        payload["warmth"] = raw
    motor._dispatch_command("set_profile", payload)
    assert motor._warmth == expected
    assert warmth_state.active_warmth() == expected


def test_set_profile_without_warmth_resets_previous_level():
    motor = _motor()
    motor._dispatch_command("set_profile", {"id": "a", "prompt": "A", "warmth": "free"})
    motor._dispatch_command("set_profile", {"id": "b", "prompt": "B"})
    assert motor._warmth == "cold"
    assert warmth_state.active_warmth() == "cold"


def test_engine_passes_warmth_to_assembler(monkeypatch):
    import opencohost.core.llm_engine as engine_mod

    monkeypatch.setattr(engine_mod, "PERSONALIZATION_ENABLED", False)
    monkeypatch.setattr(engine_mod, "MEMORIAS_ENABLED", False)
    motor = _motor()
    motor._dispatch_command("set_profile", {"id": "p", "prompt": "P", "warmth": "empathetic"})
    captured = {}
    real = motor._prompt_assembler.assemble

    def _spy(*args, **kwargs):
        captured.update(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(motor._prompt_assembler, "assemble", _spy)
    motor._build_generation_request(
        "hola", "direct", is_local=True, provider_cfg={}, request_model="llama3",
        watchdog_timeout=None,
    )
    assert captured.get("warmth") == "empathetic"


# ---------------------------------------------------------------------------
# K5 owner decision: "kike" is out of the R8 slur list (it blocked the common
# Spanish nickname "Kike"). "fag" stays.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text", ["Hola Kike, qué gusto verte por aquí.", "gg kike", "Kikes in the chat say hi"])
def test_kike_nickname_passes_output_guard_and_runtime_screen(text):
    from opencohost.config.validation import runtime_screen

    allowed, reason = output_guard(text, source="chat")
    assert allowed, reason
    assert runtime_screen({"text": text}) is None


def test_fag_still_blocked():
    from opencohost.config.validation import runtime_screen

    allowed, reason = output_guard("what a fag", source="chat")
    assert not allowed and "[no_hate_speech]" in reason
    assert runtime_screen({"text": "what a fag"}) == "no_hate_speech"
