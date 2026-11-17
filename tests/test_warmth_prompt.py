"""K5 (warmth-guardrails-runtime-20261006): per-profile warmth in the prompt.

`cold` (the default) must leave every assembled prompt byte-identical to the
pre-warmth output. The golden file was captured from the assembler BEFORE the
warmth parameter existed (`python -m tests.test_warmth_prompt` regenerates it;
only do that deliberately). The other levels inject one localized block right
after the owner policy and add it to the prompt-echo reference.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from opencohost.core.context.prompt_assembler import PromptContextAssembler
from opencohost.i18n import active as i18n_active
from opencohost.i18n.registry import discover_bundles, official_locales_dir
from opencohost.i18n.startup import resolve_active_bundle

# B-W4: this module pins the legacy prompt layout (system-message placement, one-pair
# history deque, legacy goldens). The v2 placement is tested in tests/test_prompt_layout_v2.py.
pytestmark = pytest.mark.usefixtures("legacy_prompt_layout")

GOLDEN_PATH = Path(__file__).resolve().parent / "fixtures" / "warmth_cold_prompt_golden.json"

PERSONAS = {
    "es": (
        "Eres Kira, una co-host virtual.\n\nPERSONALIDAD:\n- Sarcástica con ingenio afilado\n"
        "- Respondes en 2-4 oraciones. Nunca monólogos, pero tampoco monosílabos\n"
        "- NO uses emojis ni asteriscos para acciones"
    ),
    "en": (
        "You are Kira, a virtual co-host.\n\nPERSONALITY:\n- Sarcastic with a sharp wit\n"
        "- You reply in 2-4 sentences. Never monologues, but never one-word answers\n"
        "- Do NOT use emojis or asterisks for actions"
    ),
}
USER_TEXT = {"es": "hoy fue un día pesado, la verdad", "en": "today was a rough day, honestly"}

# (locale, source, use_system_role, extended_responses)
CASES = [
    (locale, source, use_system, extended)
    for locale in ("es", "en")
    for source in ("direct", "chat")
    for use_system in (True, False)
    for extended in (False, True)
]


def _case_id(case) -> str:
    locale, source, use_system, extended = case
    return f"{locale}-{source}-sys{int(use_system)}-ext{int(extended)}"


def _activate(locale: str) -> None:
    registry = discover_bundles(official_locales_dir(), "official")
    i18n_active.set_active_bundle(resolve_active_bundle(locale=locale, registry=registry))


def _assemble(case, **extra):
    locale, source, use_system, extended = case
    _activate(locale)
    history = [
        {"role": "user", "content": USER_TEXT[locale], "source": source},
        {"role": "assistant", "content": "Uf. Cuéntame más." if locale == "es" else "Oof. Tell me more.", "source": source},
    ]
    return PromptContextAssembler().assemble(
        USER_TEXT[locale],
        source,
        system_prompt=PERSONAS[locale],
        use_system_role=use_system,
        is_local=True,
        provider_cfg={},
        request_model="llama3",
        history_snapshot=history,
        personalization_enabled=False,
        memorias_enabled=False,
        extended_responses=extended,
        **extra,
    )


def _render(setup) -> dict:
    return {"messages": setup.messages, "prompt_echo_reference": setup.prompt_echo_reference}


@pytest.fixture(autouse=True)
def _reset_active():
    i18n_active.reset_active_bundle()
    yield
    i18n_active.reset_active_bundle()


def _golden() -> dict:
    return json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))


@pytest.mark.parametrize("case", CASES, ids=_case_id)
def test_default_prompt_is_byte_identical_to_pre_warmth_golden(case):
    assert _render(_assemble(case)) == _golden()[_case_id(case)]


# ---------------------------------------------------------------------------
# cold is the default and the fallback for anything unknown
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("warmth", ["cold", None, "", "COLD", "tibia", 3])
@pytest.mark.parametrize("case", [CASES[0], CASES[-1], CASES[5], CASES[10]], ids=_case_id)
def test_cold_or_unknown_warmth_is_byte_identical_to_golden(case, warmth):
    assert _render(_assemble(case, warmth=warmth)) == _golden()[_case_id(case)]


def test_normalize_warmth_defaults_to_cold():
    from opencohost.config.warmth import DEFAULT_WARMTH, WARMTH_LEVELS, normalize_warmth

    assert DEFAULT_WARMTH == "cold"
    # K8 added "sentimental" (see test_warmth_sentimental.py).
    assert WARMTH_LEVELS == ("cold", "empathetic", "in_character", "free", "sentimental")
    for level in WARMTH_LEVELS:
        assert normalize_warmth(level) == level
    assert normalize_warmth(" In_Character ") == "in_character"
    for junk in (None, "", "warm", 1, True, ["free"], {"free": 1}):
        assert normalize_warmth(junk) == "cold"


# ---------------------------------------------------------------------------
# empathetic / in_character / free / sentimental inject one localized block
# ---------------------------------------------------------------------------

WARM_LEVELS = ("empathetic", "in_character", "free", "sentimental")


@pytest.mark.parametrize("locale", ["es", "en"])
def test_warmth_blocks_are_localized_and_distinct(locale):
    _activate(locale)
    blocks = [i18n_active.warmth_block(level) for level in WARM_LEVELS]
    assert all(b.strip() for b in blocks)
    assert len(set(blocks)) == len(WARM_LEVELS)
    assert i18n_active.warmth_block("cold") == ""
    assert i18n_active.warmth_block("bogus") == ""
    other = "en" if locale == "es" else "es"
    _activate(other)
    assert [i18n_active.warmth_block(level) for level in WARM_LEVELS] != blocks


def test_warmth_blocks_es_use_neutral_tu_not_voseo():
    import re

    _activate("es")
    voseo = re.compile(
        r"\b(sos|vos|tenés|querés|podés|sabés|decís|hacés|mostrá|reconocé|decí|hablá|"
        r"expresá|sentís|decime|contame|usá|evitá)\b",
        re.IGNORECASE,
    )
    for level in WARM_LEVELS:
        text = i18n_active.warmth_block(level)
        assert not voseo.search(text), (level, voseo.search(text).group())


def test_warmth_block_accessor_degrades_to_english_without_slot():
    from opencohost.i18n.contract import TIER_OFFICIAL, LocaleBundle

    i18n_active.set_active_bundle(
        LocaleBundle(code="xx", tier=TIER_OFFICIAL, data={"meta": {"code": "xx"}})
    )
    for level in WARM_LEVELS:
        assert i18n_active.warmth_block(level).startswith("WARMTH")
    assert i18n_active.warmth_block("cold") == ""


def test_warmth_slot_never_borrows_another_locale():
    from opencohost.i18n.contract import NO_FALLBACK_SLOTS

    assert "llm.warmth_blocks" in NO_FALLBACK_SLOTS


@pytest.mark.parametrize("level", WARM_LEVELS)
@pytest.mark.parametrize("case", [c for c in CASES if c[2]], ids=_case_id)
def test_warm_level_injects_block_right_after_owner_policy(case, level):
    locale, source, _use_system, extended = case
    setup = _assemble(case, warmth=level)
    block = i18n_active.warmth_block(level)
    system = setup.messages[0]["content"]
    golden_system = _golden()[_case_id(case)]["messages"][0]["content"]
    assert system.count(block) == 1
    if source == "direct":
        from opencohost.core.context import prompt_assembler as pa

        policy = pa.OWNER_RESPONSE_POLICY_EXTENDED if extended else pa.OWNER_RESPONSE_POLICY_CONCISE
        assert policy + "\n\n" + block + "\n\n" in system
    # Removing the block and the K8 closing recency line gives back the cold
    # prompt exactly.
    recency = i18n_active.warmth_recency_line(level)
    assert system.endswith("\n\n" + recency)
    stripped = system[: -len("\n\n" + recency)].replace("\n\n" + block, "", 1)
    # kira-personal-tone-20261007 P2: warm owner turns also carry the
    # profile/memory relevance rule.
    if source == "direct":
        stripped = stripped.replace("\n\n" + i18n_active.facts_relevance_rule(), "", 1)
    assert stripped == golden_system
    assert block in setup.prompt_echo_reference


@pytest.mark.parametrize("level", WARM_LEVELS)
def test_warm_level_reaches_non_system_role_prompt(level):
    case = ("es", "direct", False, False)
    setup = _assemble(case, warmth=level)
    assert i18n_active.warmth_block(level) in setup.messages[-1]["content"]


def test_warm_level_does_not_shorten_extended_responses():
    """No automatic support mode: extended_responses keeps its policy."""
    from opencohost.core.context import prompt_assembler as pa

    setup = _assemble(("es", "direct", True, True), warmth="in_character")
    assert pa.OWNER_RESPONSE_POLICY_EXTENDED in setup.messages[0]["content"]


if __name__ == "__main__":  # pragma: no cover - golden regeneration
    out = {}
    for case in CASES:
        out[_case_id(case)] = _render(_assemble(case))
    GOLDEN_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"wrote {len(out)} cases to {GOLDEN_PATH}")
