"""chat-highlight-laya-scoring-20261111: highlight floor, Laya scoring,
low-traffic trigger and counts-only telemetry.

Viewer chat never leaves the machine and never reaches a log: the telemetry
test pins that the new [CHAT_HIGHLIGHT] line carries numbers only.
"""
import logging
import os
import time
from copy import deepcopy

import pytest
import yaml

from opencohost.core.decision import runtime as decision_runtime
from opencohost.smart_aggregator.chat_reaction import ChatReactionCore
from opencohost.smart_aggregator.highlight_scoring import HighlightConfig, new_counters

SECRET = "zanahoria-secreta-del-espectador"

NEUTRAL = {"user": "a", "text": "esto es una frase normal sin nada especial"}  # score 3
HUMOR = {"user": "b", "text": "jajaja que bueno estuvo ese momento"}  # 15 + 3
QUESTION = {"user": "c", "text": "¿alguien sabe cuanto cuesta esto ahora?"}  # 100 + 3


def _msg(base, **extra):
    return {**base, **extra}


def _pick(context, cfg=None, counters=None):
    return ChatReactionCore._select_highlight(context, cfg, counters)


# --------------------------------------------------------------------- L1
def test_floor_rejects_all_negative_window():
    context = [
        {"user": "a", "text": "corto"},  # too short -> -1
        {"user": "b", "text": "mira https://example.com/algo muy largo ya"},  # link -> -1
    ]
    counters = new_counters()
    assert _pick(context, None, counters) == ""
    assert counters["floor_rejected"] == 1


def test_floor_rejects_plain_neutral_window():
    counters = new_counters()
    assert _pick([NEUTRAL], None, counters) == ""
    assert counters["floor_rejected"] == 1


def test_floor_default_sits_one_above_a_plain_neutral_message():
    assert HighlightConfig().highlight_min_score == 4


def test_signal_above_the_floor_is_still_picked():
    assert _pick([NEUTRAL, HUMOR]) == "b: jajaja que bueno estuvo ese momento"


def test_floor_is_configurable():
    cfg = HighlightConfig(highlight_min_score=0)
    assert _pick([NEUTRAL], cfg).startswith("a:")


# --------------------------------------------------------------------- L2
def test_laya_respond_bonus_changes_the_pick():
    assert _pick([NEUTRAL, HUMOR]).startswith("b:")
    boosted = _msg(NEUTRAL, laya=("respond", 0.9))
    counters = new_counters()
    assert _pick([boosted, HUMOR], None, counters).startswith("a:")
    assert counters["laya_used"] == 1
    assert counters["laya_missing"] == 1


def test_laya_respond_below_min_confidence_gives_no_bonus():
    weak = _msg(NEUTRAL, laya=("respond", 0.5))
    assert _pick([weak, HUMOR]).startswith("b:")


def test_laya_spam_drop_removes_a_strong_heuristic_candidate():
    spam = _msg(QUESTION, laya=("ignore", 0.95))
    counters = new_counters()
    assert _pick([spam, HUMOR], None, counters).startswith("b:")
    assert counters["laya_filtered"] == 1


def test_laya_ignore_below_spam_confidence_keeps_the_candidate():
    unsure = _msg(QUESTION, laya=("ignore", 0.80))
    assert _pick([unsure, HUMOR]).startswith("c:")


def test_laya_unavailable_is_identical_to_the_heuristic():
    context = [NEUTRAL, HUMOR, QUESTION]
    counters = new_counters()
    assert _pick(context, None, counters) == "c: ¿alguien sabe cuanto cuesta esto ahora?"
    assert counters["laya_used"] == 0
    assert counters["laya_missing"] == 3
    assert counters["laya_filtered"] == 0


def test_laya_cannot_resurrect_an_unsafe_candidate():
    link = {"user": "x", "text": "visita https://example.com/mi-canal ahora", "laya": ("respond", 0.99)}
    assert _pick([link]) == ""


# ------------------------------------------------------- aggregator fixtures
def _aggregator(smart_aggregator_config, temp_dir, **highlight):
    from opencohost.smart_aggregator.aggregator import Aggregator

    cfg = deepcopy(smart_aggregator_config)
    cfg["history"]["db_path"] = os.path.join(temp_dir, "sessions.db")
    cfg["history"]["jsonl_path"] = os.path.join(temp_dir, "chat_log.jsonl")
    cfg["activity"]["threshold_per_second"] = 1000.0
    cfg["activity"]["cooldown_seconds"] = 45.0
    cfg["chat_highlight"] = {**(cfg.get("chat_highlight") or {}), **highlight}
    path = os.path.join(temp_dir, "smart_aggregator.yaml")
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(cfg, f, allow_unicode=True)
    agg = Aggregator(config_path=path)
    agg.start_session("youtube", "headless_test")
    agg.fired = []
    agg.contexts = []
    agg.on_activity_trigger = agg.fired.append
    agg.on_aggregated_context = agg.contexts.append
    return agg


def _no_laya(monkeypatch):
    monkeypatch.setattr(decision_runtime, "shadow_respond", lambda text, rule, on_result=None: False)


def _laya(monkeypatch, label, conf):
    def fake(text, rule, on_result=None):
        if on_result is not None:
            on_result(label, conf)
        return True

    monkeypatch.setattr(decision_runtime, "shadow_respond", fake)


def _send(agg, base, ts, text=None):
    agg.process_message({"user": base["user"], "text": text or base["text"], "timestamp": ts})


# --------------------------------------------------------------------- L3
def test_low_traffic_single_strong_message_fires_once(smart_aggregator_config, temp_dir, monkeypatch):
    _no_laya(monkeypatch)
    agg = _aggregator(smart_aggregator_config, temp_dir)
    t0 = time.time()
    _send(agg, QUESTION, t0)
    assert len(agg.fired) == 1
    assert agg.fired[0].get("low_traffic") is True
    assert len(agg.contexts) == 1
    assert agg._highlight_counters["low_traffic_fired"] == 1


def test_low_traffic_ignores_a_weak_message(smart_aggregator_config, temp_dir, monkeypatch):
    _no_laya(monkeypatch)
    agg = _aggregator(smart_aggregator_config, temp_dir)
    _send(agg, NEUTRAL, time.time())
    assert agg.fired == []


def test_low_traffic_respects_its_own_cooldown(smart_aggregator_config, temp_dir, monkeypatch):
    _no_laya(monkeypatch)
    agg = _aggregator(smart_aggregator_config, temp_dir)
    t0 = time.time()
    _send(agg, QUESTION, t0)
    _send(agg, QUESTION, t0 + 60, text="¿y esta otra pregunta distinta funciona bien?")
    assert len(agg.fired) == 1
    _send(agg, QUESTION, t0 + 100, text="¿tercera pregunta ya fuera del cooldown de 90s?")
    assert len(agg.fired) == 2


def test_low_traffic_never_beats_the_activity_cooldown(smart_aggregator_config, temp_dir, monkeypatch):
    _no_laya(monkeypatch)
    agg = _aggregator(smart_aggregator_config, temp_dir, low_traffic_cooldown_s=1)
    t0 = time.time()
    agg.activity._last_trigger_time = t0 - 10  # a normal trigger fired 10 s ago, cooldown 45 s
    _send(agg, QUESTION, t0)
    assert agg.fired == []
    _send(agg, QUESTION, t0 + 40, text="¿pregunta despues del cooldown normal si dispara?")
    assert len(agg.fired) == 1


def test_low_traffic_disabled_keeps_todays_behavior(smart_aggregator_config, temp_dir, monkeypatch):
    _no_laya(monkeypatch)
    agg = _aggregator(smart_aggregator_config, temp_dir, low_traffic_enabled=False)
    _send(agg, QUESTION, time.time())
    assert agg.fired == []
    assert agg._highlight_counters["low_traffic_fired"] == 0


def test_low_traffic_does_not_fire_above_the_rate_threshold(smart_aggregator_config, temp_dir, monkeypatch):
    _no_laya(monkeypatch)
    agg = _aggregator(smart_aggregator_config, temp_dir)
    agg.activity.threshold_per_second = 0.01  # any message reaches the normal gate
    _send(agg, QUESTION, time.time())
    assert len(agg.fired) == 1
    assert not agg.fired[0].get("low_traffic")
    assert agg._highlight_counters["low_traffic_fired"] == 0


def test_low_traffic_laya_respond_lifts_a_neutral_message(smart_aggregator_config, temp_dir, monkeypatch):
    _laya(monkeypatch, "respond", 0.9)
    agg = _aggregator(smart_aggregator_config, temp_dir)
    _send(agg, NEUTRAL, time.time())
    assert len(agg.fired) == 1
    context = agg.contexts[0]["context"]
    assert context[-1].get("laya") == ("respond", 0.9)


def test_low_traffic_laya_spam_blocks_a_strong_message(smart_aggregator_config, temp_dir, monkeypatch):
    _laya(monkeypatch, "ignore", 0.95)
    agg = _aggregator(smart_aggregator_config, temp_dir)
    _send(agg, QUESTION, time.time())
    assert agg.fired == []
    assert agg._highlight_counters["laya_filtered"] == 1


def test_laya_result_is_cached_per_message_and_bounded(smart_aggregator_config, temp_dir, monkeypatch):
    _laya(monkeypatch, "respond", 0.9)
    agg = _aggregator(smart_aggregator_config, temp_dir, low_traffic_enabled=False)
    t0 = time.time()
    for i in range(300):
        # distinct user per message, spaced past the duplicate window
        _send(agg, {"user": f"viewer{i}", "text": NEUTRAL["text"]}, t0 + 30 * i)
    assert len(agg._laya_cache) == 256  # bounded: the oldest entries were evicted


# --------------------------------------------------------------------- L4
def test_telemetry_line_carries_counts_only(smart_aggregator_config, temp_dir, monkeypatch, caplog):
    _no_laya(monkeypatch)
    caplog.set_level(logging.INFO)
    agg = _aggregator(smart_aggregator_config, temp_dir)
    _send(agg, QUESTION, time.time(), text=f"¿{SECRET} cuanto cuesta esto ahora?")
    agg._chat_ingest_last_rollup_ts = -1e9
    agg._log_chat_ingest_rollup()
    lines = [r.getMessage() for r in caplog.records if "[CHAT_HIGHLIGHT]" in r.getMessage()]
    assert len(lines) == 1
    for key in ("laya_used=", "laya_missing=", "laya_filtered=", "floor_rejected=", "low_traffic_fired="):
        assert key in lines[0]
    assert "low_traffic_fired=1" in lines[0]
    assert "zanahoria" not in caplog.text
