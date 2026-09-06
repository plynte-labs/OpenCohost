from __future__ import annotations

from datetime import datetime, timezone
import pytest

from opencohost.core.memory_v5_shadow.query_analyzer import (
    EpisodicQueryAnalyzer,
    RecallIntent,
    TemporalConstraintType,
)


def test_query_analyzer_explicit_recall_spanish():
    analyzer = EpisodicQueryAnalyzer()
    now = datetime(2026, 9, 2, 12, 0, 0, tzinfo=timezone.utc)

    # Question with explicit recall keyword
    q = "¿Te acuerdas qué problema tenía con esos audífonos?"
    res = analyzer.analyze(q, profile_id="prof_1", reference_time=now)

    assert res.recall_intent == RecallIntent.EXPLICIT
    assert "audifonos" in res.lexical_anchors or "audífonos" in res.lexical_anchors
    assert res.temporal_constraint is None
    assert res.profile_id == "prof_1"


def test_query_analyzer_temporal_last_week():
    analyzer = EpisodicQueryAnalyzer()
    # Wednesday 2026-09-02
    now = datetime(2026, 9, 2, 12, 0, 0, tzinfo=timezone.utc)

    q = "¿Qué habíamos decidido la semana pasada sobre Memory v5?"
    res = analyzer.analyze(q, profile_id="prof_1", reference_time=now)

    assert res.recall_intent == RecallIntent.EXPLICIT
    assert res.temporal_constraint is not None
    assert res.temporal_constraint.constraint_type == TemporalConstraintType.LAST_WEEK

    # A date from last week: 2026-08-27 (Thursday of prior week) -> should match
    assert res.temporal_constraint.matches("2026-08-27T15:00:00Z", reference_time=now) is True
    # A date from 3 months ago: 2026-05-10 -> should NOT match
    assert res.temporal_constraint.matches("2026-05-10T15:00:00Z", reference_time=now) is False
    # A date from today: 2026-09-02 -> should NOT match
    assert res.temporal_constraint.matches("2026-09-02T10:00:00Z", reference_time=now) is False


def test_query_analyzer_temporal_yesterday_and_days_ago():
    analyzer = EpisodicQueryAnalyzer()
    now = datetime(2026, 9, 2, 12, 0, 0, tzinfo=timezone.utc)

    # Ayer
    q_ayer = "Lo que vimos ayer de la arquitectura"
    res_ayer = analyzer.analyze(q_ayer, profile_id="prof_1", reference_time=now)
    assert res_ayer.temporal_constraint is not None
    assert res_ayer.temporal_constraint.constraint_type == TemporalConstraintType.YESTERDAY
    assert res_ayer.temporal_constraint.matches("2026-09-01T18:00:00Z", reference_time=now) is True
    assert res_ayer.temporal_constraint.matches("2026-08-30T18:00:00Z", reference_time=now) is False

    # Hace 2 semanas
    q_2w = "¿De qué hablamos hace dos semanas sobre el coche?"
    res_2w = analyzer.analyze(q_2w, profile_id="prof_1", reference_time=now)
    assert res_2w.temporal_constraint is not None
    assert res_2w.temporal_constraint.constraint_type == TemporalConstraintType.N_WEEKS_AGO
    assert res_2w.temporal_constraint.n_value == 2
    # 14 days before Sep 2 is Aug 19
    assert res_2w.temporal_constraint.matches("2026-08-19T14:00:00Z", reference_time=now) is True
    assert res_2w.temporal_constraint.matches("2026-09-01T14:00:00Z", reference_time=now) is False


def test_query_analyzer_implicit_recall():
    analyzer = EpisodicQueryAnalyzer()
    now = datetime(2026, 9, 2, 12, 0, 0, tzinfo=timezone.utc)

    q = "Volví a probar los audífonos y siguen con el mismo defecto."
    res = analyzer.analyze(q, profile_id="prof_1", reference_time=now)

    assert res.recall_intent == RecallIntent.IMPLICIT
    assert len(res.lexical_anchors) > 0


def test_query_analyzer_no_recall():
    analyzer = EpisodicQueryAnalyzer()
    now = datetime(2026, 9, 2, 12, 0, 0, tzinfo=timezone.utc)

    q = "Cuéntame un chiste de programadores."
    res = analyzer.analyze(q, profile_id="prof_1", reference_time=now)

    assert res.recall_intent == RecallIntent.NONE
    assert res.temporal_constraint is None
