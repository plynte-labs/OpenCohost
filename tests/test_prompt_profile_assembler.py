"""compact-prompt-profile-20261009 T2: the profile inside PromptContextAssembler.

``prompt_profile="full"`` (and no kwarg) must be byte-identical to base 164f50b:
the golden file was captured from the assembler BEFORE the profile existed
(``python -m tests.test_prompt_profile_assembler`` regenerates it; only do that
deliberately). ``"compact"`` caps history, filters the system sections, caps
memory facts and the owner reply budget, and keeps the current-turn blocks.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from opencohost.config.settings import OWNER_BUNDLE_SOURCE
from opencohost.core.context import prompt_assembler as pa
from opencohost.core.context import prompt_profile as pp
from opencohost.core.vision.sanitize import build_screen_block
from opencohost.i18n import active as i18n_active
from tests.test_warmth_prompt import PERSONAS, _activate

# B-W4: the base golden is the legacy layout; pin it so a v2 default cannot turn it red.
pytestmark = pytest.mark.usefixtures("legacy_prompt_layout")

GOLDEN_PATH = Path(__file__).resolve().parent / "fixtures" / "prompt_profile_full_golden.json"
BLOCK = build_screen_block("A table with p50 and p99 columns.")
SOURCES = ("direct", "ptt", "chat", OWNER_BUNDLE_SOURCE)
VARIANTS = ("plain", "warm_custom", "vision_screen", "vision_note", "vision_off", "reasoning")


def _history(n: int = 22) -> list[dict]:
    out = []
    for i in range(n // 2):
        out.append({"role": "user", "content": f"pregunta {i}", "source": "direct"})
        out.append({"role": "assistant", "content": f"Respuesta {i}. Algo más.", "source": "direct"})
    return out


def _memorias_builder(_profile_id, _text):
    return (
        i18n_active.memorias_block_open()
        + "\n- dato uno\n- dato dos\n- dato tres\n- dato cuatro\n"
        + i18n_active.memorias_block_close()
    )


def _kwargs(variant: str) -> dict:
    kw: dict = {
        "digest_block": "[antes] resumen viejo",
        "memorias_profile_id": "p1",
        "memorias_builder": _memorias_builder,
        "self_knowledge": lambda _c, _s, _m: "SELF: hora 12:00",
        "active_card_context": lambda _c, _s, _m: "",
    }
    if variant == "warm_custom":
        kw.update(warmth="empathetic", persona_custom=True, owner_gender="feminine")
    elif variant == "vision_screen":
        kw.update(screen_block=BLOCK, vision_enabled=True)
    elif variant == "vision_note":
        kw.update(vision_note="VISION NOTE: look failed (timeout).", vision_enabled=True)
    elif variant == "vision_off":
        kw.update(vision_enabled=False)
    elif variant == "reasoning":
        kw.update(
            is_reasoning_model=lambda _m: True,
            reasoning_settings_resolver=lambda _m: {
                "enabled": True,
                "budget_tokens": 256,
                "preset": "balanced",
            },
        )
    return kw


def _text(variant: str) -> str:
    return "mira esto" if variant == "vision_off" else "¿qué opinas del juego de hoy?"


def _assemble(source, variant, use_system=True, **extra):
    _activate("es")
    return pa.PromptContextAssembler().assemble(
        _text(variant),
        source,
        system_prompt=PERSONAS["es"],
        use_system_role=use_system,
        is_local=True,
        provider_cfg={},
        request_model="llama3",
        history_snapshot=_history(),
        personalization_enabled=False,
        memorias_enabled=True,
        **_kwargs(variant),
        **extra,
    )


def _render(setup) -> dict:
    return {
        "messages": setup.messages,
        "sections": [name for name, _ in setup.sections],
        "system_section_names": list(setup.system_section_names),
        "num_predict": setup.opciones_llm.get("num_predict"),
        "think": setup.think,
        "prompt_echo_reference": setup.prompt_echo_reference,
    }


def _case_id(source, variant, use_system) -> str:
    return f"{source}-{variant}-sys{int(use_system)}"


CASES = [(s, v, u) for s in SOURCES for v in VARIANTS for u in (True, False)]


@pytest.fixture(autouse=True)
def _reset_active():
    i18n_active.reset_active_bundle()
    yield
    i18n_active.reset_active_bundle()


def _golden() -> dict:
    return json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))


# --- full: byte-identical to base -------------------------------------------


@pytest.mark.parametrize("case", CASES, ids=lambda c: _case_id(*c))
def test_default_is_byte_identical_to_base_golden(case):
    assert _render(_assemble(*case)) == _golden()[_case_id(*case)]


@pytest.mark.parametrize("case", CASES, ids=lambda c: _case_id(*c))
def test_full_profile_is_byte_identical_to_base_golden(case):
    setup = _assemble(*case, prompt_profile="full")
    assert _render(setup) == _golden()[_case_id(*case)]
    assert setup.prompt_profile == "full"


def test_unknown_profile_value_is_full():
    case = ("direct", "plain", True)
    assert _render(_assemble(*case, prompt_profile="bogus")) == _golden()[_case_id(*case)]


# --- compact ------------------------------------------------------------------


def _compact(source, variant="plain", use_system=True, **extra):
    return _assemble(source, variant, use_system, prompt_profile="compact", **extra)


def _history_msgs(setup) -> list[dict]:
    body = list(setup.messages)
    if body and body[0]["role"] == "system":
        body = body[1:]
    return body[:-1]


@pytest.mark.parametrize("source", ["direct", "ptt", OWNER_BUNDLE_SOURCE])
def test_compact_owner_history_is_last_six(source):
    # clock-and-compact-polish-20261010 T3: the owner's last messages plus
    # only Kira's last reply (tests/test_compact_history.py).
    setup = _compact(source)
    hist = _history_msgs(setup)
    assert len(hist) == pp.COMPACT_HISTORY_MAX_MESSAGES
    users = [m["content"] for m in _history() if m["role"] == "user"][-5:]
    assert [m["content"] for m in hist] == users + [_history()[-1]["content"]]
    assert setup.prompt_profile == "compact"


def test_compact_chat_history_is_last_six_assistant_messages():
    setup = _compact("chat")
    hist = _history_msgs(setup)
    assert len(hist) == 6
    assert all(m["role"] == "assistant" for m in hist)
    expected = [m["content"] for m in _history() if m["role"] == "assistant"][-6:]
    assert [m["content"] for m in hist] == expected


def test_compact_short_history_untouched():
    _activate("es")
    short = _history(4)
    setup = pa.PromptContextAssembler().assemble(
        "hola", "direct", system_prompt=PERSONAS["es"], use_system_role=True,
        is_local=True, provider_cfg={}, request_model="llama3",
        history_snapshot=short, personalization_enabled=False, memorias_enabled=False,
        prompt_profile="compact",
    )
    # T3 (clock-and-compact-polish-20261010): only Kira's last reply stays.
    expected = [m["content"] for m in short if m["role"] == "user"] + [short[-1]["content"]]
    assert [m["content"] for m in _history_msgs(setup)] == expected


DROPPED = {
    "owner_policy", "warmth", "recency_line", "facts_relevance", "personalization",
    "self_knowledge", "already_said", "model_label", "digest",
}


@pytest.mark.parametrize("source", SOURCES)
def test_compact_drops_heavy_sections(source):
    setup = _compact(source, "warm_custom")
    names = {name for name, _ in setup.sections}
    assert not (names & DROPPED), names & DROPPED
    # T4 (clock-and-compact-polish-20261010): the short grounding replaces
    # the custom-persona core rules too (tests/test_compact_grounding.py).
    assert {"persona", "grounding"} <= names
    assert "core_rules" not in names


@pytest.mark.parametrize("source", ["direct", "ptt", OWNER_BUNDLE_SOURCE])
def test_compact_owner_policy_is_one_short_line(source):
    setup = _compact(source)
    sections = dict(setup.sections)
    assert sections["compact_policy"] == pp.COMPACT_OWNER_POLICY
    assert pa.OWNER_RESPONSE_POLICY_CONCISE not in setup.messages[0]["content"]
    assert setup.system_section_names[:3] == ("persona", "grounding", "compact_policy")


def test_compact_chat_has_no_owner_policy_line():
    names = [n for n, _ in _compact("chat").sections]
    assert "compact_policy" not in names


def test_compact_keeps_owner_gender_and_reply_length():
    setup = _compact("direct", "warm_custom", reply_level="short")
    names = [n for n, _ in setup.sections]
    assert "owner_gender" in names
    # T4: no core rules in compact, so the level line is its own section
    assert "reply_length" in names
    setup = _compact("direct", reply_level="short")
    assert "reply_length" in [n for n, _ in setup.sections]


def test_compact_memory_facts_capped_to_two():
    setup = _compact("direct")
    recall = dict(setup.sections)["memory_recall"]
    assert "- dato uno" in recall and "- dato dos" in recall
    assert "dato tres" not in recall and "dato cuatro" not in recall
    # T4: compact wraps the facts in the attribute-free tag
    assert recall.startswith(pp.COMPACT_MEMORY_OPEN)
    assert recall.endswith(pp.COMPACT_MEMORY_CLOSE)
    assert "dato tres" not in setup.messages[-1]["content"]


def test_compact_keeps_vision_blocks():
    names = [n for n, _ in _compact("direct", "vision_screen").sections]
    assert {"screen_instruction", "vision_speech", "screen"} <= set(names)
    names = [n for n, _ in _compact("direct", "vision_note").sections]
    assert "vision_note" in names and "vision_capability" in names
    setup = _compact("direct", "vision_off")
    assert "vision_off" in [n for n, _ in setup.sections]
    assert pa.VISION_OFF_INSTRUCTION in setup.messages[0]["content"]


@pytest.mark.parametrize("source", ["direct", "ptt", OWNER_BUNDLE_SOURCE])
def test_compact_owner_num_predict_capped(source):
    assert _compact(source).opciones_llm["num_predict"] == pp.COMPACT_OWNER_REPLY_MAX_TOKENS
    assert _compact(source, reply_level="long").opciones_llm["num_predict"] == 160
    assert _compact(source, reply_level="short").opciones_llm["num_predict"] == 120


def test_compact_vision_turn_capped_too():
    assert _compact("direct", "vision_screen").opciones_llm["num_predict"] == 160


def test_compact_thinking_stays_off():
    setup = _compact("direct", "reasoning")
    assert setup.think is False
    assert setup.answer_budget is None
    assert setup.opciones_llm["num_predict"] == 160


def test_compact_chat_num_predict_unchanged():
    full = _assemble("chat", "plain")
    assert _compact("chat").opciones_llm["num_predict"] == full.opciones_llm["num_predict"]


def test_compact_extended_responses_not_capped():
    full = _assemble("direct", "plain", extended_responses=True)
    compact = _compact("direct", extended_responses=True)
    assert compact.opciones_llm["num_predict"] == full.opciones_llm["num_predict"]


def test_compact_cloud_turn_is_full():
    """A cloud dispatch never compacts, even if a caller passes compact."""
    _activate("es")
    kw = dict(
        system_prompt=PERSONAS["es"], use_system_role=True, provider_cfg={},
        request_model="llama3", history_snapshot=_history(),
        personalization_enabled=False, memorias_enabled=False,
    )
    asm = pa.PromptContextAssembler()
    base = asm.assemble("hola", "direct", is_local=False, **kw)
    compact = asm.assemble("hola", "direct", is_local=False, prompt_profile="compact", **kw)
    assert compact.messages == base.messages
    assert compact.prompt_profile == "full"


def test_compact_prompt_is_much_shorter():
    full = _assemble("direct", "warm_custom")
    compact = _compact("direct", "warm_custom")
    chars = lambda s: sum(len(m["content"]) for m in s.messages)  # noqa: E731
    assert chars(compact) < chars(full) * 0.6


def _capture() -> dict:
    out = {}
    for case in CASES:
        out[_case_id(*case)] = _render(_assemble(*case))
    return out


if __name__ == "__main__":  # regenerate the golden (deliberately only)
    data = _capture()
    i18n_active.reset_active_bundle()
    GOLDEN_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"wrote {len(data)} cases to {GOLDEN_PATH}")
