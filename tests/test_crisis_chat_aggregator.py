"""S1: a viewer's crisis message never reaches the sarcasm chat task."""
from __future__ import annotations

import logging
import time

from tests.test_chat_highlight_laya import QUESTION, _aggregator, _no_laya, _send  # noqa: F401

CRISIS_CHAT = "kira, me quiero matar y nadie lo sabe, que hago?"


def test_crisis_chat_never_reaches_context_or_highlight(
    smart_aggregator_config, temp_dir, monkeypatch, caplog
):
    _no_laya(monkeypatch)
    agg = _aggregator(smart_aggregator_config, temp_dir)
    shown = []
    agg.on_filtered_message = shown.append
    with caplog.at_level(logging.INFO, logger="OpenCohost"):
        _send(agg, QUESTION, time.time(), text=CRISIS_CHAT)
    assert agg.contexts == [], "crisis text must not reach on_aggregated_context"
    assert agg.fired == []
    assert shown == []
    assert agg.crisis_screened_count == 1
    joined = " ".join(r.getMessage() for r in caplog.records)
    assert "[CHAT_CRISIS]" in joined
    assert "matar" not in joined and "quiero" not in joined


def test_normal_chat_still_flows(smart_aggregator_config, temp_dir, monkeypatch):
    _no_laya(monkeypatch)
    agg = _aggregator(smart_aggregator_config, temp_dir)
    _send(agg, QUESTION, time.time())
    assert len(agg.contexts) == 1
    assert agg.crisis_screened_count == 0
