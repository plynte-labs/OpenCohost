"""Phase 0 - T0.3: marker vocabulary contract.

Pins the single source of truth in opencohost/core/qwen_markers.py so the failure-class /
response / badge sets stay aligned and the legacy log literals cannot silently drift.
"""
from __future__ import annotations

import pytest


def test_class_sets_are_aligned():
    from opencohost.core.qwen_markers import (
        BADGE_BY_CLASS,
        FAILURE_CLASSES,
        RESPONSE_BY_CLASS,
    )
    assert set(FAILURE_CLASSES) == set(RESPONSE_BY_CLASS) == set(BADGE_BY_CLASS)


def test_engine_statuses_exact():
    from opencohost.core.qwen_markers import ENGINE_STATUSES
    assert ENGINE_STATUSES == frozenset({
        "qwen_active", "edge_fallback", "qwen_starting",
        "not_configured", "piper_local", "unknown",
    })


def test_every_badge_is_a_valid_engine_status():
    from opencohost.core.qwen_markers import BADGE_BY_CLASS, ENGINE_STATUSES
    for cls, badge in BADGE_BY_CLASS.items():
        assert badge in ENGINE_STATUSES, f"{cls} -> {badge!r} is not a valid engine status"


def test_legacy_log_literals_pinned():
    # Must stay byte-identical to llm_engine.py:1560-1561 / 1584 so
    # tests/test_llm_engine_timeouts.py's assertions stay green.
    from opencohost.core.qwen_markers import (
        ACCEPT_FALLBACK_MISSING_REFERENCE,
        ACCEPT_TTS_EFECTIVO_PESADO,
    )
    assert ACCEPT_TTS_EFECTIVO_PESADO == "TTS efectivo: requested=pesado effective=pesado"
    assert ACCEPT_FALLBACK_MISSING_REFERENCE == (
        "Auto-fallback to Edge-TTS: requested=pesado effective=ligero reason=missing_reference"
    )


@pytest.mark.parametrize("cls", [
    "success", "startup_or_model_load_failure", "transient_health_blip",
    "zombie_running_but_unhealthy", "xtts_python_unset",
    "manual_server_already_running", "port_5000_busy",
])
def test_every_failure_class_has_response_and_badge(cls):
    from opencohost.core.qwen_markers import (
        BADGE_BY_CLASS,
        FAILURE_CLASSES,
        RESPONSE_BY_CLASS,
    )
    assert cls in FAILURE_CLASSES
    assert cls in RESPONSE_BY_CLASS
    assert cls in BADGE_BY_CLASS
