"""Unit tests for opencohost.core.context.ctx_telemetry.CtxTelemetryRing (Phase C3,
refactor_core_api_20260802 batch B3). Pins the ring + snapshot contract that
used to live inline in MotorVocalIA as `_ctx_telemetry_ring` +
`ctx_telemetry_snapshot()`. tests/test_ctx_telemetry_ring.py stays the
motor-integrated contract; this file is the leaf-class unit harness.
"""

from opencohost.core.context.ctx_telemetry import CtxTelemetryRing


def test_empty_ring_snapshot():
    ring = CtxTelemetryRing(maxlen=3)
    assert ring.snapshot() == {"latest": None, "ring": []}


def test_append_then_snapshot_latest_is_last_appended():
    ring = CtxTelemetryRing(maxlen=3)
    ring.append({"source": "chat", "n": 1})
    ring.append({"source": "chat", "n": 2})
    assert ring.snapshot()["latest"] == {"source": "chat", "n": 2}


def test_ring_is_oldest_first():
    ring = CtxTelemetryRing(maxlen=5)
    for i in range(3):
        ring.append({"n": i})
    assert [e["n"] for e in ring.snapshot()["ring"]] == [0, 1, 2]


def test_maxlen_eviction_drops_oldest():
    ring = CtxTelemetryRing(maxlen=3)
    for i in range(5):
        ring.append({"n": i})
    snap = ring.snapshot()
    assert [e["n"] for e in snap["ring"]] == [2, 3, 4]
    assert len(snap["ring"]) == 3


def test_sources_filtered_latest_restricts_to_matching_sources():
    ring = CtxTelemetryRing(maxlen=5)
    ring.append({"source": "kira-agenda", "n": 1})
    ring.append({"source": "direct", "n": 2})
    ring.append({"source": "kira-agenda", "n": 3})
    snap = ring.snapshot(sources=("direct",))
    assert snap["latest"] == {"source": "direct", "n": 2}
    # `ring` stays the full unfiltered history regardless of the filter.
    assert len(snap["ring"]) == 3


def test_sources_filter_with_no_match_yields_none_latest():
    ring = CtxTelemetryRing(maxlen=5)
    ring.append({"source": "kira-agenda", "n": 1})
    snap = ring.snapshot(sources=("direct",))
    assert snap["latest"] is None
    assert len(snap["ring"]) == 1


def test_motor_delegate_degrades_to_empty_snapshot_without_the_ring():
    # Pins the delegate's guard branch in MotorVocalIA.ctx_telemetry_snapshot:
    # a motor built via __new__ (the pattern several suites use) has no
    # _ctx_telemetry_ring attribute and must degrade to an empty snapshot,
    # exactly as the pre-C3 inline `getattr(...) or ()` did.
    from opencohost.core.llm_engine import MotorVocalIA

    motor = MotorVocalIA.__new__(MotorVocalIA)
    assert motor.ctx_telemetry_snapshot() == {"latest": None, "ring": []}
