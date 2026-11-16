"""T6 (memory-grounding-and-recall-budget-20261016): meta-recall summary gate.

Owner decision M1: the summary/meta-recall branch is OFF by default until
summaries are real (today they are keyword bags). The topical gate is
implemented for when the flag is turned on: a topical meta question ("the Tesla
we discussed last session") never uses the summary branch, and a general meta
question uses it only when the v5 decision does not contradict it.
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from opencohost.config import settings
from opencohost.i18n import active as i18n_active
from tests.test_memoria_recall_routing import (
    _BASE_TS,
    _enable_memorias,
    _make_motor,
    _pin_es_locale,  # noqa: F401  (autouse fixture)
    _seed,
    _summary_key,
)

SUMMARY = "busca web que; puedes buscar google; tesla model tres"
GENERAL = "¿qué recuerdas de mí?"
TOPICAL_META = "el tesla del que hablamos la sesión pasada"


def _rig(monkeypatch, tmp_path):
    motor, _, _ = _make_motor()
    _enable_memorias(monkeypatch, motor, tmp_path, profile_id="p")
    _seed(
        tmp_path / "memorias.db", "p", "sum", content=SUMMARY,
        status="summary", stable_key=_summary_key("p", _BASE_TS + timedelta(hours=1)),
        updated_at=_BASE_TS + timedelta(hours=1),
    )
    return motor


def test_summary_branch_flag_is_off_by_default():
    assert settings.MEMORY_SUMMARY_RECALL_ENABLED is False


def test_summary_branch_off_by_default(monkeypatch, tmp_path):
    motor = _rig(monkeypatch, tmp_path)
    block = motor._build_memorias_injection_block("p", GENERAL)
    assert SUMMARY not in block


def test_topical_meta_query_skips_summary_branch(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "MEMORY_SUMMARY_RECALL_ENABLED", True)
    motor = _rig(monkeypatch, tmp_path)

    stats: dict = {}
    block = motor._build_memorias_injection_block("p", TOPICAL_META, recall_stats=stats)

    assert SUMMARY not in block
    # A topical miss is answered with the no-match note instead (T4).
    assert i18n_active.memorias_no_match_rule() in block
    assert stats.get("note") == "no_match"


def test_general_meta_query_uses_summaries_when_the_flag_is_on(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "MEMORY_SUMMARY_RECALL_ENABLED", True)
    motor = _rig(monkeypatch, tmp_path)

    block = motor._build_memorias_injection_block("p", GENERAL)

    assert SUMMARY in block


@pytest.mark.parametrize("decision,expected", [
    ("REJECTED_LEXICAL_UNSUBSTANTIATED", False),
    ("NO_CANDIDATES", False),
    ("ACCEPTED_EXPLICIT_CORROBORATED", True),
    ("DISABLED", True),   # v5 did not run: nothing contradicts the summaries
])
def test_flag_on_general_meta_query_follows_the_v5_decision(monkeypatch, tmp_path, decision, expected):
    monkeypatch.setattr(settings, "MEMORY_SUMMARY_RECALL_ENABLED", True)
    motor = _rig(monkeypatch, tmp_path)

    block = motor._build_memorias_injection_block(
        "p", GENERAL, recall_stats={"decision": decision}
    )

    assert (SUMMARY in block) is expected


def test_off_flag_general_meta_query_falls_through_to_the_normal_path(monkeypatch, tmp_path):
    """Regular facts are still injected by the lexical path; only summaries go."""
    motor = _rig(monkeypatch, tmp_path)
    _seed(tmp_path / "memorias.db", "p", "reg", content="el streamer usa un telescopio dobsoniano",
          title="telescopio dobsoniano", signature="telescopio dobsoniano", pinned=True)

    block = motor._build_memorias_injection_block("p", GENERAL)

    assert "telescopio dobsoniano" in block and SUMMARY not in block
