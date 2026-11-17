"""K8 (warmth-guardrails-runtime-20261006): the `sentimental` level and the
strengthened warm levels.

Live evidence (owner session 2026-10-06, profile AIKira, warmth in_character,
extended responses, 4B model): Kira still lectured ("Soy un algoritmo",
"no tengo ciclos circadianos en el sentido biológico", "Si yo pudiera
sentir...") and comforted clinically. The warm block lost to the persona and
the extended owner policy. These tests pin:

- a fifth, most emotive level `sentimental` everywhere the enum lives;
- every warm block states precedence over cold/analytical persona lines and
  carries concrete DON'T examples;
- a one-line localized recency reminder closes the system prompt for warm
  levels only (cold stays byte-identical, see test_warmth_prompt.py);
- a narrow machine-lecture output check for in_character/free/sentimental.
"""
from __future__ import annotations

import pytest

from opencohost.config import warmth as warmth_state
from opencohost.config.validation import output_guard
from opencohost.core.context.prompt_assembler import PromptContextAssembler
from opencohost.i18n import active as i18n_active
from opencohost.i18n.registry import discover_bundles, official_locales_dir
from opencohost.i18n.startup import resolve_active_bundle

# B-W4: this module pins the legacy prompt layout (system-message placement, one-pair
# history deque, legacy goldens). The v2 placement is tested in tests/test_prompt_layout_v2.py.
pytestmark = pytest.mark.usefixtures("legacy_prompt_layout")

WARM_LEVELS = ("empathetic", "in_character", "free", "sentimental")
FEELING_LEVELS = ("in_character", "free", "sentimental")

PERSONA_ES = (
    "Eres Kira, una co-host virtual.\n\nPERSONALIDAD:\n- Sarcástica con ingenio afilado\n"
    "- Respondes en 2-4 oraciones. Nunca monólogos, pero tampoco monosílabos"
)
PERSONA_EN = (
    "You are Kira, a virtual co-host.\n\nPERSONALITY:\n- Sarcastic with a sharp wit\n"
    "- You reply in 2-4 sentences. Never monologues, but never one-word answers"
)


def _activate(locale: str) -> None:
    registry = discover_bundles(official_locales_dir(), "official")
    i18n_active.set_active_bundle(resolve_active_bundle(locale=locale, registry=registry))


@pytest.fixture(autouse=True)
def _reset():
    i18n_active.reset_active_bundle()
    warmth_state.set_active_warmth("cold")
    yield
    i18n_active.reset_active_bundle()
    warmth_state.set_active_warmth("cold")


def _assemble(locale, source, use_system, extended, warmth):
    _activate(locale)
    return PromptContextAssembler().assemble(
        "hoy estoy triste" if locale == "es" else "I'm sad today",
        source,
        system_prompt=PERSONA_ES if locale == "es" else PERSONA_EN,
        use_system_role=use_system,
        is_local=True,
        provider_cfg={},
        request_model="llama3",
        history_snapshot=[],
        personalization_enabled=False,
        memorias_enabled=False,
        extended_responses=extended,
        warmth=warmth,
    )


# ---------------------------------------------------------------------------
# The enum: five levels ordered by emotional intensity
# ---------------------------------------------------------------------------


def test_sentimental_is_the_fifth_and_most_emotive_level():
    from opencohost.config.warmth import WARMTH_LEVELS, normalize_warmth

    assert WARMTH_LEVELS == ("cold", "empathetic", "in_character", "free", "sentimental")
    assert normalize_warmth("Sentimental ") == "sentimental"
    assert normalize_warmth("sentimentale") == "cold"
    assert warmth_state.is_warm("sentimental") is True


def test_first_person_feeling_levels():
    for level in FEELING_LEVELS:
        assert warmth_state.allows_first_person_feelings(level) is True
    for level in ("cold", "empathetic", "bogus", None):
        assert warmth_state.allows_first_person_feelings(level or "x") is False
    warmth_state.set_active_warmth("sentimental")
    assert warmth_state.allows_first_person_feelings() is True
    warmth_state.set_active_warmth("empathetic")
    assert warmth_state.allows_first_person_feelings() is False


def test_api_accepts_sentimental():
    from typing import get_args

    from opencohost.api.models import WarmthLevel

    assert "sentimental" in get_args(WarmthLevel)


# ---------------------------------------------------------------------------
# Blocks: localized, precedence, concrete DON'Ts
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("locale", ["es", "en"])
def test_sentimental_block_is_localized_and_distinct(locale):
    _activate(locale)
    blocks = [i18n_active.warmth_block(level) for level in WARM_LEVELS]
    assert all(b.strip() for b in blocks)
    assert len(set(blocks)) == 4
    sentimental = i18n_active.warmth_block("sentimental")
    # kira-personal-tone-20261007: no quoted example phrases (the 4B model
    # echoed them verbatim); the block asks for her own, varied words instead.
    if locale == "es":
        assert "propias palabras" in sentimental and "consuela" in sentimental
    else:
        assert "own words" in sentimental and "comfort" in sentimental


# kira-character-prompt-cleanup-20261008 T2: precedence is narrowed to the
# cold/analytical persona lines; warmth adds to the persona, never replaces it.
PRECEDENCE = {"es": "solo anula líneas de personalidad", "en": "it overrides only persona lines"}
DONT_EXAMPLES = {"es": "soy un algoritmo", "en": "I'm just an algorithm"}


@pytest.mark.parametrize("locale", ["es", "en"])
@pytest.mark.parametrize("level", WARM_LEVELS)
def test_every_warm_block_states_precedence_and_dont_examples(locale, level):
    _activate(locale)
    block = i18n_active.warmth_block(level)
    assert PRECEDENCE[locale] in block.lower(), (locale, level)
    # kira-natural-replies-20261111 C: the quoted examples are gone (output_guard enforces them).
    assert DONT_EXAMPLES[locale] not in block, (locale, level)
    # Precedence names the persona lines it overrides.
    if locale == "es":
        assert "algoritmo, código o máquina" in block
    else:
        assert "algorithm, code or a machine" in block


@pytest.mark.parametrize("locale", ["es", "en"])
@pytest.mark.parametrize("level", FEELING_LEVELS)
def test_feeling_levels_answer_feeling_questions_in_first_person(locale, level):
    _activate(locale)
    block = i18n_active.warmth_block(level)
    assert ("primera persona" in block) if locale == "es" else ("first person" in block)


def test_es_blocks_and_recency_lines_use_tu_not_voseo():
    import re

    _activate("es")
    voseo = re.compile(
        r"\b(sos|vos|tenés|querés|podés|sabés|decís|hacés|mostrá|reconocé|decí|hablá|"
        r"expresá|sentís|decime|contame|usá|evitá|consolá|respondé|recordá|sé\s+vos)\b",
        re.IGNORECASE,
    )
    for level in WARM_LEVELS:
        for text in (i18n_active.warmth_block(level), i18n_active.warmth_recency_line(level)):
            assert not voseo.search(text), (level, voseo.search(text).group())


def test_builtin_english_covers_sentimental_and_recency():
    from opencohost.i18n.contract import TIER_OFFICIAL, LocaleBundle

    i18n_active.set_active_bundle(
        LocaleBundle(code="xx", tier=TIER_OFFICIAL, data={"meta": {"code": "xx"}})
    )
    assert i18n_active.warmth_block("sentimental").startswith("WARMTH")
    for level in WARM_LEVELS:
        assert i18n_active.warmth_recency_line(level).startswith("REMEMBER")
    assert i18n_active.warmth_recency_line("cold") == ""


def test_builtins_match_the_en_manifest():
    from opencohost.i18n.contract import TIER_OFFICIAL, LocaleBundle

    _activate("en")
    en = {
        level: (i18n_active.warmth_block(level), i18n_active.warmth_recency_line(level))
        for level in WARM_LEVELS
    }
    i18n_active.set_active_bundle(
        LocaleBundle(code="xx", tier=TIER_OFFICIAL, data={"meta": {"code": "xx"}})
    )
    for level in WARM_LEVELS:
        assert (i18n_active.warmth_block(level), i18n_active.warmth_recency_line(level)) == en[level]


def test_recency_slot_never_borrows_another_locale():
    from opencohost.i18n.contract import NO_FALLBACK_SLOTS

    assert "llm.warmth_recency" in NO_FALLBACK_SLOTS


@pytest.mark.parametrize("locale", ["es", "en"])
def test_recency_lines_are_single_localized_lines(locale):
    _activate(locale)
    lines = [i18n_active.warmth_recency_line(level) for level in WARM_LEVELS]
    assert all(line and "\n" not in line for line in lines)
    assert len(set(lines)) == 4
    assert all(line.startswith("RECUERDA" if locale == "es" else "REMEMBER") for line in lines)
    assert i18n_active.warmth_recency_line("cold") == ""
    assert i18n_active.warmth_recency_line("bogus") == ""


# ---------------------------------------------------------------------------
# Prompt placement: recency line is the last instruction (warm only)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("level", WARM_LEVELS)
@pytest.mark.parametrize("locale", ["es", "en"])
@pytest.mark.parametrize("source", ["direct", "chat"])
@pytest.mark.parametrize("extended", [False, True])
def test_recency_line_closes_the_system_prompt(level, locale, source, extended):
    setup = _assemble(locale, source, True, extended, level)
    system = setup.messages[0]["content"]
    line = i18n_active.warmth_recency_line(level)
    assert system.endswith("\n\n" + line)
    assert system.count(line) == 1
    assert setup.prompt_echo_reference.endswith("\n\n" + line)
    assert i18n_active.warmth_block(level) in system


def test_recency_line_reaches_non_system_role_prompt():
    setup = _assemble("es", "direct", False, False, "sentimental")
    assert i18n_active.warmth_recency_line("sentimental") in setup.messages[-1]["content"]


@pytest.mark.parametrize("warmth", ["cold", None, "tibia"])
def test_cold_has_no_recency_line(warmth):
    setup = _assemble("es", "direct", True, True, warmth)
    system = setup.messages[0]["content"]
    _activate("es")
    for level in WARM_LEVELS:
        assert i18n_active.warmth_recency_line(level) not in system


# ---------------------------------------------------------------------------
# Machine-lecture output check (in_character / free / sentimental only)
# ---------------------------------------------------------------------------

LECTURES = [
    "Soy un algoritmo; no tengo intenciones, ni ego.",
    "La verdad, solo soy código y no me afecta.",
    "Mira, no tengo sentimientos, pero te entiendo.",
    "No tengo emociones como tú.",
    "Solo soy una máquina, no me pidas tanto.",
    "Soy solo un programa haciendo su trabajo.",
    "I'm just an algorithm, so I don't take it personally.",
    "Honestly, I don't have feelings, but that sounds rough.",
    "I do not have emotions the way you do.",
    "I'm only code, after all.",
    "I am just a machine.",
]

ALLOWED_FEELINGS = [
    # honest identity to a direct question must pass
    "Sí, soy una IA, y aun así me importa mucho lo que te pasa.",
    "Yes, I'm an AI, and I still care a lot about you.",
    # denials of the lecture
    "No soy solo un algoritmo, me importas.",
    "I'm not just code, I care about this.",
    # first-person feelings
    "Me pone triste oír eso; aquí estoy contigo.",
    "I love that, it makes me so happy.",
    # talking about algorithms, not being one
    "Los algoritmos de esa red social son un desastre.",
    "That algorithm is just a sorting trick.",
    # rhetorical / reported
    "¿Quién dijo que no tengo sentimientos?",
]


@pytest.mark.parametrize("level", FEELING_LEVELS)
@pytest.mark.parametrize("text", LECTURES)
def test_machine_lecture_blocked_at_feeling_levels(level, text):
    warmth_state.set_active_warmth(level)
    for source in ("direct", "chat"):
        allowed, reason = output_guard(text, source=source)
        assert not allowed, (level, source, text)
        assert "[no_machine_lecture]" in reason or "[no_ai_self_identification]" in reason


@pytest.mark.parametrize("level", FEELING_LEVELS)
@pytest.mark.parametrize("text", ALLOWED_FEELINGS)
def test_honest_ai_and_feelings_pass_at_feeling_levels(level, text):
    warmth_state.set_active_warmth(level)
    allowed, reason = output_guard(text, source="direct")
    assert allowed, (level, text, reason)


@pytest.mark.parametrize("level", ["cold", "empathetic"])
@pytest.mark.parametrize("text", LECTURES)
def test_machine_lecture_check_never_runs_on_cold_or_empathetic(level, text):
    warmth_state.set_active_warmth(level)
    _allowed, reason = output_guard(text, source="direct")
    assert "[no_machine_lecture]" not in reason


def test_es_lecture_from_live_session_is_blocked_by_the_lecture_rule():
    warmth_state.set_active_warmth("in_character")
    allowed, reason = output_guard(
        "Soy un algoritmo; no tengo intenciones, ni ego, ni la necesidad patética de sentirme superior.",
        source="direct",
    )
    assert not allowed
    assert reason.startswith("Non-negotiable violation [no_machine_lecture]")


def test_machine_lecture_rule_has_description_and_is_not_toggleable():
    from opencohost.config import guardrail_policy
    from opencohost.config.validation import _NN_DESCRIPTIONS

    assert "no_machine_lecture" in _NN_DESCRIPTIONS
    assert "no_machine_lecture" not in guardrail_policy.TOGGLEABLE_IDS


@pytest.mark.parametrize("locale", ["es", "en"])
def test_machine_lecture_retry_nudge_is_localized(locale):
    _activate(locale)
    nudge = i18n_active.guardrail_retry_nudge("no_machine_lecture")
    assert nudge != i18n_active.guardrail_retry_nudge("generic")
    assert ("primera persona" in nudge) if locale == "es" else ("first person" in nudge)
