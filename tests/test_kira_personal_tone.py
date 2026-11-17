"""kira-personal-tone-20261007 P1-P3: Kira sounds personal instead of echoing
her prompt, keeps profile facts in the background, and answers vision
questions directly (knowing the avatar overlay on screen is herself).

Live evidence (owner session 2026-10-07): the 4B model copied the quoted
example phrases of the warm blocks verbatim ("me alegra muchísimo" ~8x),
narrated its own persona ("mi fachada sarcástica"), dropped profile facts
off-topic, comforted without any sadness, and opened every vision reply with
"Veo..." even when asked "pero tú eres el personaje".

Small models copy quoted text, so the only quoted phrases a warm block or
recency line may carry are the prohibited ones (the DON'T list); everything
else must describe intent.
"""
from __future__ import annotations

import re

import pytest

from opencohost.core.context import prompt_assembler as pa
from opencohost.core.context.prompt_assembler import PromptContextAssembler
from opencohost.core.vision.sanitize import build_screen_block
from opencohost.i18n import active as i18n_active
from opencohost.i18n.contract import TIER_OFFICIAL, LocaleBundle
from opencohost.i18n.registry import discover_bundles, official_locales_dir
from opencohost.i18n.startup import resolve_active_bundle

# B-W4: this module pins the legacy prompt layout (system-message placement, one-pair
# history deque, legacy goldens). The v2 placement is tested in tests/test_prompt_layout_v2.py.
pytestmark = pytest.mark.usefixtures("legacy_prompt_layout")

WARM_LEVELS = ("empathetic", "in_character", "free", "sentimental")
LOCALES = ("es", "en")

# The only phrases a warm block / recency line may quote: things Kira must NOT say.
FORBIDDEN_QUOTES = {
    "es": {
        "soy un algoritmo",
        "no tengo sentimientos",
        "no tengo ciclos circadianos",
        "en el sentido biológico",
        "si yo pudiera sentir",
        "un parche en el sistema operativo emocional",
        "solo soy una máquina",
        "mis emociones no son reales",
    },
    "en": {
        "I'm just an algorithm",
        "I don't have feelings",
        "I don't have circadian rhythms",
        "in the biological sense",
        "if I could feel",
        "a patch for your emotional operating system",
        "I'm just a machine",
        "my emotions aren't real",
    },
}

# Example phrases the live model echoed (or their en twins); never again.
ECHOED_EXAMPLES = {
    "es": ("me encanta", "me alegra", "me pone triste", "me duele", "me siento contenta"),
    "en": ("i love that", "makes me happy", "makes me sad", "hurts to hear", "i feel happy", "so happy"),
}

# Pre-change sizes: the rewrite must not grow the blocks.
OLD_BLOCK_LEN = {
    "es": {"empathetic": 769, "in_character": 893, "free": 831, "sentimental": 1045},
    "en": {"empathetic": 754, "in_character": 891, "free": 818, "sentimental": 983},
}

RESPECT_ALLOWANCE = 170

SHOW_DONT_DESCRIBE = {"es": "Muestra tu personalidad, no la describas", "en": "Show your personality, never describe it"}
OWN_WORDS = {"es": "con tus propias palabras", "en": "in your own words"}
NO_REPEAT = {"es": "no repitas", "en": "never repeat"}
SADNESS_GATE = {
    "es": "tristeza, dolor o angustia",
    "en": "sadness, pain or distress",
}


def _activate(locale: str) -> None:
    registry = discover_bundles(official_locales_dir(), "official")
    i18n_active.set_active_bundle(resolve_active_bundle(locale=locale, registry=registry))


def _builtin() -> None:
    i18n_active.set_active_bundle(
        LocaleBundle(code="xx", tier=TIER_OFFICIAL, data={"meta": {"code": "xx"}})
    )


@pytest.fixture(autouse=True)
def _reset():
    i18n_active.reset_active_bundle()
    yield
    i18n_active.reset_active_bundle()


def _quotes(text: str) -> set[str]:
    return set(re.findall(r'"([^"]+)"', text))


def _warm_texts(locale: str):
    _activate(locale)
    for level in WARM_LEVELS:
        yield level, "block", i18n_active.warmth_block(level)
        yield level, "recency", i18n_active.warmth_recency_line(level)


# ---------------------------------------------------------------------------
# P1: warm blocks + recency lines
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("locale", LOCALES)
def test_warm_texts_quote_only_forbidden_phrases(locale):
    for level, kind, text in _warm_texts(locale):
        extra = _quotes(text) - FORBIDDEN_QUOTES[locale]
        assert not extra, (locale, level, kind, extra)
        assert "«" not in text and "»" not in text, (locale, level, kind)


@pytest.mark.parametrize("locale", LOCALES)
def test_warm_texts_drop_the_echoed_example_phrases(locale):
    for level, kind, text in _warm_texts(locale):
        low = text.lower()
        for phrase in ECHOED_EXAMPLES[locale]:
            assert phrase not in low, (locale, level, kind, phrase)


@pytest.mark.parametrize("locale", LOCALES)
@pytest.mark.parametrize("level", WARM_LEVELS)
def test_warm_blocks_carry_the_personal_tone_rules(locale, level):
    _activate(locale)
    block = i18n_active.warmth_block(level)
    assert SHOW_DONT_DESCRIBE[locale] in block
    assert SADNESS_GATE[locale] in block
    # kira-natural-replies-20261111 C: the no-repeat line is gone (replayed openers are stripped).
    if level != "empathetic":  # empathetic claims no feelings of its own
        assert OWN_WORDS[locale] in block


@pytest.mark.parametrize("locale", LOCALES)
def test_warm_blocks_did_not_grow(locale):
    """The new rules replace the example phrases instead of piling on: the
    four blocks together are no longer than before, and none is longer than
    the old longest one."""
    _activate(locale)
    sizes = [len(i18n_active.warmth_block(level)) for level in WARM_LEVELS]
    # kira-identity-core-rules-20261007 C2 added one owner-respect sentence (and the
    # "biological sense" ban) per block: a fixed allowance, not a license to grow.
    allowance = RESPECT_ALLOWANCE
    assert sum(sizes) <= sum(OLD_BLOCK_LEN[locale].values()) + allowance * len(sizes)
    assert max(sizes) <= max(OLD_BLOCK_LEN[locale].values()) + allowance


@pytest.mark.parametrize("locale", LOCALES)
@pytest.mark.parametrize("level", WARM_LEVELS)
def test_warm_blocks_keep_identity_honesty(locale, level):
    _activate(locale)
    block = i18n_active.warmth_block(level)
    if locale == "es":
        assert "humana" in block and "cuerpo" in block
    else:
        assert "human" in block and "body" in block


def test_builtin_english_texts_follow_the_same_rules():
    _builtin()
    for level in WARM_LEVELS:
        for text in (i18n_active.warmth_block(level), i18n_active.warmth_recency_line(level)):
            assert not (_quotes(text) - FORBIDDEN_QUOTES["en"]), (level, text)
            for phrase in ECHOED_EXAMPLES["en"]:
                assert phrase not in text.lower(), (level, phrase)


# ---------------------------------------------------------------------------
# P2: profile / memory facts are background, warm owner turns only
# ---------------------------------------------------------------------------

FACTS_RULE_HEAD = {"es": "DATOS DE FONDO", "en": "BACKGROUND FACTS"}


def _assemble(locale, source="direct", warmth=None, screen_block="", use_system_role=True):
    _activate(locale)
    return PromptContextAssembler().assemble(
        "¿qué ves?" if locale == "es" else "what do you see?",
        source,
        system_prompt="Eres Kira." if locale == "es" else "You are Kira.",
        use_system_role=use_system_role,
        is_local=True,
        provider_cfg={},
        request_model="llama3",
        history_snapshot=[],
        personalization_enabled=False,
        memorias_enabled=False,
        warmth=warmth,
        screen_block=screen_block,
    )


@pytest.mark.parametrize("locale", LOCALES)
def test_facts_relevance_rule_is_localized(locale):
    _activate(locale)
    rule = i18n_active.facts_relevance_rule()
    assert rule.startswith(FACTS_RULE_HEAD[locale])
    assert "\n" not in rule
    _builtin()
    assert i18n_active.facts_relevance_rule().startswith("BACKGROUND FACTS")


def test_facts_relevance_slot_never_borrows_another_locale():
    from opencohost.i18n.contract import NO_FALLBACK_SLOTS

    assert "llm.facts_relevance_rule" in NO_FALLBACK_SLOTS
    assert "llm.vision_answer_rules" in NO_FALLBACK_SLOTS


@pytest.mark.parametrize("locale", LOCALES)
@pytest.mark.parametrize("level", WARM_LEVELS)
@pytest.mark.parametrize("source", ["direct", "ptt"])
def test_facts_rule_reaches_warm_owner_turns(locale, level, source):
    setup = _assemble(locale, source=source, warmth=level)
    system = setup.messages[0]["content"]
    rule = i18n_active.facts_relevance_rule()
    assert system.count(rule) == 1
    # It sits before the closing recency line, which stays last.
    assert system.endswith("\n\n" + i18n_active.warmth_recency_line(level))
    assert rule in setup.prompt_echo_reference


@pytest.mark.parametrize("locale", LOCALES)
@pytest.mark.parametrize("warmth", ["cold", None, "tibia"])
def test_facts_rule_never_reaches_cold(locale, warmth):
    system = _assemble(locale, warmth=warmth).messages[0]["content"]
    assert FACTS_RULE_HEAD[locale] not in system


@pytest.mark.parametrize("locale", LOCALES)
def test_facts_rule_skips_viewer_chat(locale):
    system = _assemble(locale, source="chat", warmth="sentimental").messages[0]["content"]
    assert FACTS_RULE_HEAD[locale] not in system


# ---------------------------------------------------------------------------
# P3: vision answers -- question first, no fixed opener, self-avatar is Kira
# ---------------------------------------------------------------------------

BLOCK = build_screen_block("Una ventana con un personaje anime de pelo rosa.")
QUESTION_FIRST = {"es": "Responde primero a lo que te preguntaron", "en": "Answer the question first"}
SELF_AVATAR = {"es": "eres tú, Kira", "en": "is you, Kira"}


@pytest.mark.parametrize("locale", LOCALES)
def test_vision_answer_rules_are_localized(locale):
    _activate(locale)
    rules = i18n_active.vision_answer_rules()
    assert QUESTION_FIRST[locale] in rules
    assert SELF_AVATAR[locale] in rules
    assert "avatar" in rules.lower()
    assert '"' not in rules and "«" not in rules  # no quoted openers to copy
    low = rules.lower()
    assert "veo" not in low and "i see" not in low
    _builtin()
    assert QUESTION_FIRST["en"] in i18n_active.vision_answer_rules()


@pytest.mark.parametrize("locale", LOCALES)
@pytest.mark.parametrize("source", ["direct", "ptt"])
def test_vision_turn_carries_answer_rules(locale, source):
    system = _assemble(locale, source=source, screen_block=BLOCK).messages[0]["content"]
    _activate(locale)
    rules = i18n_active.vision_answer_rules()
    assert system.count(rules) == 1
    assert system.index(pa.VISION_SPEECH_INSTRUCTION) < system.index(rules)


@pytest.mark.parametrize("locale", LOCALES)
def test_vision_rules_reach_non_system_role_prompt(locale):
    setup = _assemble(locale, screen_block=BLOCK, use_system_role=False)
    _activate(locale)
    assert i18n_active.vision_answer_rules() in setup.messages[-1]["content"]


@pytest.mark.parametrize("locale", LOCALES)
def test_non_vision_turn_has_no_answer_rules(locale):
    system = _assemble(locale).messages[0]["content"]
    _activate(locale)
    assert i18n_active.vision_answer_rules() not in system


@pytest.mark.parametrize("source", ["chat", "kira-agenda", "livevoice"])
def test_non_owner_sources_never_get_vision_rules(source):
    joined = "\n".join(m["content"] for m in _assemble("es", source=source, screen_block=BLOCK).messages)
    _activate("es")
    assert i18n_active.vision_answer_rules() not in joined


def test_screen_instruction_no_longer_dictates_an_opener():
    low = pa.SCREEN_CONTEXT_INSTRUCTION.lower()
    assert "veo" not in low and "i see" not in low
    assert '"' not in pa.SCREEN_CONTEXT_INSTRUCTION


@pytest.mark.parametrize("level", WARM_LEVELS)
def test_vision_turn_keeps_the_active_warmth(level):
    system = _assemble("es", warmth=level, screen_block=BLOCK).messages[0]["content"]
    _activate("es")
    assert i18n_active.warmth_block(level) in system
    assert i18n_active.vision_answer_rules() in system


def test_builtin_lines_match_the_en_manifest():
    _activate("en")
    en = (i18n_active.facts_relevance_rule(), i18n_active.vision_answer_rules())
    _builtin()
    assert (i18n_active.facts_relevance_rule(), i18n_active.vision_answer_rules()) == en


def test_es_new_lines_use_tu_not_voseo():
    _activate("es")
    voseo = re.compile(
        r"\b(sos|vos|tenés|querés|podés|sabés|decís|hacés|mostrá|respondé|describí|abrí|mencioná)\b",
        re.IGNORECASE,
    )
    for text in (i18n_active.facts_relevance_rule(), i18n_active.vision_answer_rules()):
        assert not voseo.search(text), voseo.search(text).group()
