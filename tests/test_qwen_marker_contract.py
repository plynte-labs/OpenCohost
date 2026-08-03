"""Phase 0 - T0.3: marker vocabulary contract.

Pins the live engine-status vocabulary in opencohost/core/qwen_markers.py so it
cannot silently drift.
"""
from __future__ import annotations


def test_engine_statuses_exact():
    from opencohost.core.speech.qwen_markers import ENGINE_STATUSES
    assert ENGINE_STATUSES == frozenset({
        "qwen_active", "edge_fallback", "qwen_starting",
        "not_configured", "piper_local", "unknown",
    })
