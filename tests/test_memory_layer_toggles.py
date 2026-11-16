"""T4 memory-semantic-recall: user controls per memory layer.

Three runtime flags under the MEMORIAS_ENABLED master switch:
- MEMORY_FACTS_ENABLED: fact-notebook rows in the memorias block;
- MEMORY_EPISODES_ENABLED: v5 episodic recall lines (ADR-059);
- MEMORY_SEMANTIC_ENABLED: every MiniLM use on the turn path. Off means no
  query embedding, lexical `select_top_k` facts (byte-identical to pre-T3)
  and NO episodes, because v5 episodic recall is embedding-only.

An off switch never spends a worker call for the layer it disables, keeps the
prompt byte-identical to the matching "layer absent" prompt, and reports
`disabled` in the content-free `memory_recall:` line.

Persistence: `config/memory_layers.json` (atomic write, fail-open to all
layers on), applied to the settings module by EngineHost.start().
"""

from __future__ import annotations

import json
import logging
from queue import Queue
from unittest.mock import MagicMock

import pytest

from opencohost.config import settings
from opencohost.core.memory_v5_shadow.subsystem import MemorySubsystem
from tests import test_memory_fact_semantic_rank as rank
from tests import test_memory_recall_merge as merge

QUERY = rank.QUERY_PET
FACT_PET = rank.FACT_PET
FACT_CAR = rank.FACT_CAR
ROWS = [{"id": "pet", "content": FACT_PET}, {"id": "car", "content": FACT_CAR}]


@pytest.fixture(autouse=True)
def _pin_es_locale():
    from opencohost.i18n import active as i18n_active
    from opencohost.i18n.startup import resolve_active_bundle

    i18n_active.set_active_bundle(resolve_active_bundle(locale="es"))
    yield
    i18n_active.reset_active_bundle()


def _layers(monkeypatch, facts=True, episodes=True, semantic=True):
    monkeypatch.setattr(settings, "MEMORY_FACTS_ENABLED", facts)
    monkeypatch.setattr(settings, "MEMORY_EPISODES_ENABLED", episodes)
    monkeypatch.setattr(settings, "MEMORY_SEMANTIC_ENABLED", semantic)


def _full_motor(monkeypatch, tmp_path, rows=ROWS, episodes=None, reconcile=True):
    """Motor with a fact index (fake worker) and an episodic coordinator that
    always returns *episodes*. Returns (motor, worker, coordinator, scheduled)."""
    motor, db = rank._make_motor(monkeypatch, tmp_path, rows)
    scheduled: list = []
    index, _cache, worker = rank._make_index(tmp_path, scheduled=scheduled)
    if reconcile:
        index.reconcile(rank.PROFILE, rank._rows_from(db))
    worker.batches.clear()
    eps = [merge._episode("ep1")] if episodes is None else list(episodes)
    coord = merge.FakeCoordinator(eps)
    motor._memory = MemorySubsystem(mode="ACTIVE", coordinator=coord, fact_index=index)
    return motor, worker, coord, scheduled


def _episodes_only_motor(monkeypatch, tmp_path, episodes):
    """Reference for 'no fact layer': empty store, same episodes, no fact index."""
    motor, _db = rank._make_motor(monkeypatch, tmp_path, [])
    motor._memory = MemorySubsystem(mode="ACTIVE", coordinator=merge.FakeCoordinator(list(episodes)))
    return motor


def _one_recall_line(caplog):
    (rec,) = rank._recall_records(caplog)
    msg = rec.getMessage()
    assert rank._TELEMETRY_RE.match(msg), msg
    assert "zq" not in msg  # content-free
    return msg


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

def test_layer_defaults_are_all_on():
    assert settings.MEMORY_FACTS_ENABLED is True
    assert settings.MEMORY_EPISODES_ENABLED is True
    assert settings.MEMORY_SEMANTIC_ENABLED is True


def test_all_layers_on_keeps_the_merged_semantic_prompt(monkeypatch, tmp_path, caplog):
    _layers(monkeypatch)
    motor, worker, coord, _ = _full_motor(monkeypatch, tmp_path)

    with caplog.at_level(logging.INFO, logger="OpenCohost"):
        lines = rank._block_lines(rank._prompt(motor, QUERY))

    assert lines[0] == FACT_PET
    assert len(lines) == 2 and "zqepuser" in lines[1]
    assert len(coord.embed_timeouts) == 1 and len(worker.queries) == 1
    msg = _one_recall_line(caplog)
    assert "semantic=1" in msg and "episodes=1" in msg and "fact_fallback=none" in msg


# ---------------------------------------------------------------------------
# Facts off
# ---------------------------------------------------------------------------

def test_facts_off_injects_only_episodes_and_never_embeds_for_facts(monkeypatch, tmp_path, caplog):
    _layers(monkeypatch, facts=False)
    reference = rank._prompt(
        _episodes_only_motor(monkeypatch, tmp_path / "ref", [merge._episode("ep1")]), QUERY)
    motor, worker, coord, scheduled = _full_motor(monkeypatch, tmp_path, reconcile=False)

    caplog.clear()
    with caplog.at_level(logging.INFO, logger="OpenCohost"):
        prompt = rank._prompt(motor, QUERY)

    assert prompt == reference
    assert FACT_PET not in prompt and FACT_CAR not in prompt
    assert worker.queries == [] and worker.batches == [] and scheduled == []
    assert len(coord.embed_timeouts) == 1  # episodes still recalled
    msg = _one_recall_line(caplog)
    assert "lexical=0" in msg and "semantic=0" in msg and "episodes=1" in msg
    assert "fact_fallback=disabled" in msg and "fallback=none" in msg


# ---------------------------------------------------------------------------
# Episodes off
# ---------------------------------------------------------------------------

def test_episodes_off_spends_no_recall_call_and_keeps_semantic_facts(monkeypatch, tmp_path, caplog):
    _layers(monkeypatch, episodes=False)
    ref_motor, *_ = _full_motor(monkeypatch, tmp_path / "ref", episodes=[])
    reference = rank._prompt(ref_motor, QUERY)
    motor, worker, coord, _ = _full_motor(monkeypatch, tmp_path)

    caplog.clear()
    with caplog.at_level(logging.INFO, logger="OpenCohost"):
        prompt = rank._prompt(motor, QUERY)

    assert prompt == reference
    assert rank._block_lines(prompt) == [FACT_PET]
    assert coord.embed_timeouts == []  # no recall call at all
    assert len(worker.queries) == 1  # fact query embedding still runs
    msg = _one_recall_line(caplog)
    assert "episodes=0" in msg and "decision=DISABLED" in msg and "fallback=disabled" in msg
    assert "semantic=1" in msg and "fact_fallback=none" in msg


# ---------------------------------------------------------------------------
# Semantic off
# ---------------------------------------------------------------------------

def test_semantic_off_is_byte_identical_lexical_with_no_worker_use(monkeypatch, tmp_path, caplog):
    _layers(monkeypatch, semantic=False)
    baseline = rank._baseline(monkeypatch, tmp_path, ROWS, query=QUERY)  # _memory=None, pre-T3
    motor, worker, coord, scheduled = _full_motor(monkeypatch, tmp_path, reconcile=False)

    caplog.clear()
    with caplog.at_level(logging.INFO, logger="OpenCohost"):
        prompt = rank._prompt(motor, QUERY)

    assert prompt == baseline
    assert FACT_PET not in prompt  # the paraphrase only semantic ranking finds
    assert coord.embed_timeouts == []
    assert worker.queries == [] and worker.batches == []
    assert scheduled == []  # not even a background fact reconcile
    msg = _one_recall_line(caplog)
    assert "semantic=0" in msg and "episodes=0" in msg
    assert "fallback=disabled" in msg and "fact_fallback=disabled" in msg


def test_semantic_off_wins_over_episodes_on(monkeypatch, tmp_path):
    _layers(monkeypatch, episodes=True, semantic=False)
    motor, _worker, coord, _ = _full_motor(monkeypatch, tmp_path)

    lines = rank._block_lines(rank._prompt(motor, QUERY))

    assert coord.embed_timeouts == []
    assert not any("zqepuser" in line for line in lines)


# ---------------------------------------------------------------------------
# Facts and episodes off
# ---------------------------------------------------------------------------

def test_facts_and_episodes_off_inject_no_block_and_call_nothing(monkeypatch, tmp_path, caplog):
    _layers(monkeypatch, facts=False, episodes=False)
    empty_motor, _ = rank._make_motor(monkeypatch, tmp_path / "ref", [])
    reference = rank._prompt(empty_motor, QUERY)
    motor, worker, coord, scheduled = _full_motor(monkeypatch, tmp_path)

    caplog.clear()
    with caplog.at_level(logging.INFO, logger="OpenCohost"):
        prompt = rank._prompt(motor, QUERY)

    assert prompt == reference
    assert rank._block_lines(prompt) == []
    assert coord.embed_timeouts == [] and worker.queries == [] and scheduled == []
    msg = _one_recall_line(caplog)
    assert "lexical=0" in msg and "fallback=disabled" in msg and "fact_fallback=disabled" in msg


def test_flags_are_read_live_between_turns(monkeypatch, tmp_path):
    _layers(monkeypatch)
    motor, *_ = _full_motor(monkeypatch, tmp_path)
    assert FACT_PET in rank._prompt(motor, QUERY)

    monkeypatch.setattr(settings, "MEMORY_FACTS_ENABLED", False)
    assert FACT_PET not in rank._prompt(motor, QUERY)


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

_ALL_ON = {"facts": True, "episodes": True, "semantic": True}


def test_load_without_file_fails_open_to_all_layers_on(tmp_path):
    path = tmp_path / "config" / "memory_layers.json"
    assert settings.load_memory_layers(str(path)) == _ALL_ON


def test_save_then_load_round_trips(tmp_path):
    path = tmp_path / "config" / "memory_layers.json"
    layers = {"facts": False, "episodes": True, "semantic": False}

    assert settings.save_memory_layers(layers, str(path)) is True

    assert settings.load_memory_layers(str(path)) == layers
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk == layers


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("{not json", _ALL_ON),
        ("[1, 2]", _ALL_ON),
        (json.dumps({"facts": False}), {**_ALL_ON, "facts": False}),
        (json.dumps({"facts": "no", "episodes": 0, "semantic": False}),
         {**_ALL_ON, "semantic": False}),
    ],
)
def test_load_is_fail_open_per_key(tmp_path, raw, expected):
    path = tmp_path / "memory_layers.json"
    path.write_text(raw, encoding="utf-8")
    assert settings.load_memory_layers(str(path)) == expected


def test_save_reports_failure_instead_of_swallowing_it(tmp_path):
    target = tmp_path / "is_a_dir"
    target.mkdir()
    assert settings.save_memory_layers(_ALL_ON, str(target)) is False


def test_apply_sets_the_runtime_flags(monkeypatch):
    _layers(monkeypatch)
    settings.apply_memory_layers({"facts": False, "episodes": True, "semantic": False})
    assert settings.MEMORY_FACTS_ENABLED is False
    assert settings.MEMORY_EPISODES_ENABLED is True
    assert settings.MEMORY_SEMANTIC_ENABLED is False
    assert settings.current_memory_layers() == {"facts": False, "episodes": True, "semantic": False}


def test_engine_host_start_applies_persisted_layers(tmp_path, monkeypatch):
    import opencohost.api.engine_host as engine_host_mod

    path = tmp_path / "memory_layers.json"
    path.write_text(json.dumps({"episodes": False}), encoding="utf-8")
    monkeypatch.setattr(settings, "MEMORY_LAYERS_FILE", str(path))
    _layers(monkeypatch)

    motor = MagicMock()
    motor.command_queue = Queue()
    motor.current_model = None
    motor.is_processing = False
    motor.is_speaking = False
    monkeypatch.setattr(engine_host_mod, "MotorVocalIA", lambda *a, **kw: motor)
    monkeypatch.setattr(engine_host_mod, "HealthMonitor", lambda: MagicMock())
    monkeypatch.setattr(engine_host_mod, "ollama", MagicMock())
    monkeypatch.setattr(engine_host_mod, "EDITORIAL_CARDS_DB", str(tmp_path / "cards.db"))

    host = engine_host_mod.EngineHost(lock_path=str(tmp_path / "engine.lock"))
    try:
        host.start()
        assert settings.MEMORY_EPISODES_ENABLED is False
        assert settings.MEMORY_FACTS_ENABLED is True
        assert settings.MEMORY_SEMANTIC_ENABLED is True
    finally:
        host.stop()
