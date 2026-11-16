"""P5 (kira-personal-tone-20261007): optional owner grammatical gender.

Empty (the default) leaves every prompt byte-identical to the cold golden.
When set, one localized line on OWNER turns tells Kira how to address the
person talking to her (no slashes like "o/a").
"""
from __future__ import annotations

import json
from typing import get_args

import pytest

from opencohost.config import owner_gender
from tests.test_warmth_prompt import (  # noqa: F401  (autouse fixture re-exported)
    CASES,
    _activate,
    _assemble,
    _case_id,
    _golden,
    _render,
    _reset_active,
)

# B-W4: this module pins the legacy prompt layout (system-message placement, one-pair
# history deque, legacy goldens). The v2 placement is tested in tests/test_prompt_layout_v2.py.
pytestmark = pytest.mark.usefixtures("legacy_prompt_layout")

OWNER_CASES = [c for c in CASES if c[1] == "direct"]
VIEWER_CASES = [c for c in CASES if c[1] == "chat"]


def test_values_and_normalizer():
    assert owner_gender.OWNER_GENDERS == ("", "masculine", "feminine", "neutral")
    for good in ("masculine", "feminine", "neutral"):
        assert owner_gender.normalize_owner_gender(good) == good
    assert owner_gender.normalize_owner_gender(" Feminine ") == "feminine"
    for bad in (None, "", "x", 3, "o/a"):
        assert owner_gender.normalize_owner_gender(bad) == ""


def test_api_literal_matches_config():
    from opencohost.api.models import OwnerGender

    assert get_args(OwnerGender) == owner_gender.OWNER_GENDERS


@pytest.mark.parametrize("value", ["", None, "bogus", 7])
@pytest.mark.parametrize("case", CASES, ids=_case_id)
def test_empty_or_unknown_is_byte_identical_to_cold_golden(case, value):
    assert _render(_assemble(case, owner_gender=value)) == _golden()[_case_id(case)]


@pytest.mark.parametrize("value", ["masculine", "feminine", "neutral"])
@pytest.mark.parametrize("case", OWNER_CASES, ids=_case_id)
def test_set_gender_adds_exactly_its_line_on_owner_turns(case, value):
    from opencohost.i18n import active as i18n_active

    setup = _assemble(case, owner_gender=value)
    line = i18n_active.owner_gender_line(value)
    assert line
    flat = json.dumps(setup.messages, ensure_ascii=False)
    assert flat.count(line) == 1
    golden = _golden()[_case_id(case)]["messages"]
    assert json.dumps(golden, ensure_ascii=False) != flat


@pytest.mark.parametrize("case", VIEWER_CASES, ids=_case_id)
def test_viewer_turns_never_carry_the_line(case):
    assert _render(_assemble(case, owner_gender="feminine")) == _golden()[_case_id(case)]


def test_spanish_lines_are_pinned():
    from opencohost.i18n import active as i18n_active

    _activate("es")
    assert i18n_active.owner_gender_line("masculine") == (
        "Usa el género gramatical masculino al referirte a quien te habla "
        "(participios y adjetivos); no lo uses como apodo."
    )
    assert i18n_active.owner_gender_line("feminine") == (
        "Usa el género gramatical femenino al referirte a quien te habla "
        "(participios y adjetivos); no lo uses como apodo."
    )
    assert i18n_active.owner_gender_line("neutral") == (
        "Evita las formas con género al dirigirte a quien te habla: usa construcciones "
        "neutras y no asumas si es hombre o mujer."
    )
    assert i18n_active.owner_gender_line("") == ""


@pytest.mark.parametrize("locale", ["es", "en"])
@pytest.mark.parametrize("value", ["masculine", "feminine", "neutral"])
def test_lines_never_use_slash_forms_or_voseo(locale, value):
    from opencohost.i18n import active as i18n_active

    _activate(locale)
    line = i18n_active.owner_gender_line(value)
    assert line and "o/a" not in line and "/a" not in line
    assert "vos " not in line.lower()


def test_slot_is_no_fallback_and_in_both_manifests():
    from pathlib import Path

    import yaml

    from opencohost.i18n.contract import NO_FALLBACK_SLOTS

    assert "llm.owner_gender_lines" in NO_FALLBACK_SLOTS
    root = Path(owner_gender.__file__).resolve().parents[1] / "locales"
    for code in ("es", "en"):
        data = yaml.safe_load((root / code / "manifest.yaml").read_text(encoding="utf-8"))
        assert set(data["llm"]["owner_gender_lines"]) == {"masculine", "feminine", "neutral"}


def test_engine_set_profile_arms_owner_gender():
    import queue
    from unittest.mock import MagicMock

    from opencohost.core import llm_engine

    m = llm_engine.MotorVocalIA(queue.Queue(), lambda e: None)
    m.ollama = MagicMock()
    assert getattr(m, "_owner_gender", "") == ""
    m._dispatch_command("set_profile", {"prompt": "p", "owner_gender": "feminine", "_profile_name": "x"})
    assert m._owner_gender == "feminine"
    m._dispatch_command("set_profile", {"prompt": "p", "_profile_name": "x"})
    assert m._owner_gender == ""


@pytest.mark.parametrize("locale", ["es", "en"])
@pytest.mark.parametrize("value", ["masculine", "feminine", "neutral"])
def test_lines_carry_no_quoted_example_words(locale, value):
    """Session 21:07: the model copied the quoted example «concentrado» as a
    vocative at sentence ends. The lines are plain instructions, no quoted words."""
    from opencohost.i18n import active as i18n_active

    _activate(locale)
    line = i18n_active.owner_gender_line(value)
    assert line
    for ch in "«»\"'“”‘’":
        assert ch not in line, (locale, value, ch)
    assert "concentrad" not in line.lower() and "cansad" not in line.lower()
