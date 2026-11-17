"""laya-decision-layer-20261009 T1: per-decision mode/threshold settings.

Env (``OPENCOHOST_DECISION_<ID>``/``_THRESHOLD``) gives the defaults; the
persisted owner store (``decisions.json``) wins per field. ``DECISIONS_FILE`` is
redirected per test by the autouse ``_isolate_decisions`` fixture.
"""
from __future__ import annotations

import json
import os

import pytest

from opencohost.config import decisions as dc
from opencohost.config import settings


def _defaults(monkeypatch, **env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return settings._resolve_decision_defaults()


def test_file_points_into_config_dir():
    assert settings.DECISIONS_FILE == os.path.join(settings.CONFIG_DIR, "decisions.json")


def test_code_defaults_are_on_except_respond_and_vision(monkeypatch):
    for d in ("VISION", "MEMORY", "REPLY_LENGTH", "RESPOND", "WEB", "CLOCK"):
        monkeypatch.delenv(f"OPENCOHOST_DECISION_{d}", raising=False)
        monkeypatch.delenv(f"OPENCOHOST_DECISION_{d}_THRESHOLD", raising=False)
    monkeypatch.delenv("OPENCOHOST_DECISION_BUDGET_MS", raising=False)
    out = settings._resolve_decision_defaults()
    # installed-app-parity-20261014 T1: everything defaults to ``on`` except
    # ``respond`` (shadow-only) and ``vision`` (stays shadow).
    assert out["decisions"] == {
        **{d: {"mode": "on", "threshold": 0.9} for d in ("memory", "reply_length", "web", "clock")},
        **{d: {"mode": "shadow", "threshold": 0.9} for d in ("vision", "respond")},
    }
    assert out["budget_ms"] == 600


def test_env_overrides_and_invalid_values_fall_back(monkeypatch):
    out = _defaults(
        monkeypatch,
        OPENCOHOST_DECISION_VISION="on",
        OPENCOHOST_DECISION_VISION_THRESHOLD="0.95",
        OPENCOHOST_DECISION_MEMORY="banana",
        OPENCOHOST_DECISION_MEMORY_THRESHOLD="2",
        OPENCOHOST_DECISION_REPLY_LENGTH="OFF",
        OPENCOHOST_DECISION_RESPOND="on",
        OPENCOHOST_DECISION_BUDGET_MS="99999",
    )
    d = out["decisions"]
    assert d["vision"] == {"mode": "on", "threshold": 0.95}
    assert d["memory"] == {"mode": "on", "threshold": 0.9}  # malformed keeps the code default
    assert d["reply_length"]["mode"] == "off"
    assert d["respond"]["mode"] == "shadow"  # respond is shadow-only
    assert out["budget_ms"] == settings.DECISION_BUDGET_MAX_MS


def test_env_can_turn_a_default_on_decision_back_to_shadow_or_off(monkeypatch):
    out = _defaults(monkeypatch, OPENCOHOST_DECISION_WEB="shadow", OPENCOHOST_DECISION_CLOCK="off")
    assert out["decisions"]["web"]["mode"] == "shadow"
    assert out["decisions"]["clock"]["mode"] == "off"


def test_budget_lower_bound_and_garbage(monkeypatch):
    assert _defaults(monkeypatch, OPENCOHOST_DECISION_BUDGET_MS="1")["budget_ms"] == settings.DECISION_BUDGET_MIN_MS
    assert _defaults(monkeypatch, OPENCOHOST_DECISION_BUDGET_MS="x")["budget_ms"] == 600


def test_laya_env(monkeypatch):
    monkeypatch.setenv("OPENCOHOST_LAYA_PYTHON", "  C:/envs/laya/python.exe ")
    monkeypatch.setenv("OPENCOHOST_LAYA_CHECKPOINT", "multilingual")
    out = settings._resolve_decision_defaults()
    assert out["laya_python"] == "C:/envs/laya/python.exe"
    assert out["laya_checkpoint"] == "multilingual"


@pytest.mark.parametrize("raw,engine", [("", "onnx"), ("torch", "torch"), (" TORCH ", "torch"), ("nope", "onnx")])
def test_laya_engine_env(monkeypatch, raw, engine):
    monkeypatch.setenv("OPENCOHOST_LAYA_ENGINE", raw)
    monkeypatch.setenv("OPENCOHOST_LAYA_ONNX_DIR", "  D:/laya/onnx ")
    out = settings._resolve_decision_defaults()
    assert out["laya_engine"] == engine
    assert out["laya_onnx_dir"] == "D:/laya/onnx"


# --- store -------------------------------------------------------------------


def test_store_starts_at_defaults():
    snap = dc.get_decision_settings()
    assert set(snap) == {"vision", "memory", "reply_length", "respond", "web", "clock"}
    assert snap["vision"] == {"mode": "shadow", "threshold": 0.9}
    assert dc.get_mode("vision") == "shadow"
    assert dc.get_threshold("vision") == 0.9


def test_snapshot_is_a_copy():
    snap = dc.get_decision_settings()
    snap["vision"]["mode"] = "on"
    assert dc.get_mode("vision") == "shadow"


def test_missing_file_loads_defaults():
    assert dc.load_decisions() == dc.get_decision_settings()
    assert dc.get_mode("memory") == "on"


def test_file_wins_per_field_and_invalid_entries_fall_back():
    with open(dc.DECISIONS_FILE, "w", encoding="utf-8") as f:
        json.dump(
            {"decisions": {
                "vision": {"mode": "on"},
                "memory": {"mode": "x", "threshold": 0.85},
                "respond": {"mode": "on", "threshold": 0.4},
                "bogus": {"mode": "on"},
            }},
            f,
        )
    dc.load_decisions()
    assert dc.get_decision_settings()["vision"] == {"mode": "on", "threshold": 0.9}
    assert dc.get_decision_settings()["memory"] == {"mode": "on", "threshold": 0.85}
    assert dc.get_decision_settings()["respond"] == {"mode": "shadow", "threshold": 0.9}


@pytest.mark.parametrize("content", ["{not json", "[]", json.dumps({"decisions": 3})])
def test_corrupt_file_loads_defaults(content):
    dc.set_decision("vision", mode="on")
    with open(dc.DECISIONS_FILE, "w", encoding="utf-8") as f:
        f.write(content)
    dc.load_decisions()
    assert dc.get_mode("vision") == "shadow"


def test_save_partial_update_writes_full_state_atomically():
    out = dc.save_decisions({"vision": {"mode": "on"}, "memory": {"threshold": 0.85}})
    assert out["vision"] == {"mode": "on", "threshold": 0.9}
    assert out["memory"] == {"mode": "on", "threshold": 0.85}
    with open(dc.DECISIONS_FILE, encoding="utf-8") as f:
        assert json.load(f) == {"decisions": out}
    leftovers = [n for n in os.listdir(os.path.dirname(dc.DECISIONS_FILE)) if n.endswith(".tmp")]
    assert leftovers == []
    dc.reset_decisions()
    assert dc.get_mode("vision") == "shadow"
    dc.load_decisions()
    assert dc.get_mode("vision") == "on"


@pytest.mark.parametrize(
    "update",
    [
        {"bogus": {"mode": "on"}},
        {"vision": {"mode": "maybe"}},
        {"respond": {"mode": "on"}},
        {"vision": {"threshold": 0.3}},
        {"vision": {"threshold": 1.0}},
        {"vision": {"threshold": True}},
        {"vision": {"threshold": "0.9"}},
        {"vision": "on"},
        {"vision": {"extra": 1}},
    ],
)
def test_save_invalid_raises_and_keeps_state(update):
    dc.set_decision("memory", mode="on")
    with pytest.raises(ValueError):
        dc.save_decisions(update)
    assert dc.get_mode("memory") == "on"
    assert dc.get_mode("vision") == "shadow"
    assert not os.path.exists(dc.DECISIONS_FILE)


def test_save_write_failure_returns_none_and_keeps_state(monkeypatch):
    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(dc.os, "replace", boom)
    assert dc.save_decisions({"vision": {"mode": "on"}}) is None
    assert dc.get_mode("vision") == "shadow"


def test_getters_never_read_disk(monkeypatch):
    dc.save_decisions({"vision": {"mode": "off"}})

    def no_open(*a, **k):
        raise AssertionError("disk read on the turn path")

    monkeypatch.setattr("builtins.open", no_open)
    assert dc.get_mode("vision") == "off"
    assert dc.get_decision_settings()["memory"]["mode"] == "on"
