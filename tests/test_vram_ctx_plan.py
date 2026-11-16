"""VRAM-aware context plan (feature vram-aware-context-budget, T2).

Covers:
1. ``clamp_ctx_for_vram`` — the pure VRAM clamp in ``context_budget``.
2. ``_resolve_effective_ctx_limit`` under ``context.policy == "requested"``:
   requested ctx replaces the 4096 cold-start default, requested wins over a
   resident allocation that differs, stale residency is ignored, spill steps
   down once and sticks, and the ``ctx_plan`` line is logged only on change.
3. Fail-open: a broken config read keeps the pre-existing behavior.
4. T1 (model_context_auto_config_20260928): Auto (min(native, 16384),
   VRAM-clamped through the SAME plan machinery) applies regardless of
   ``context.policy`` for an unconfigured model -- "runtime_truth" no longer
   means "never request anything". See section 4 below.
5. T1b (model_context_auto_config_20260928, owner-approved 2026-09-28, see
   the ADR-056 Amendment): Auto UPGRADES a model already resident at a
   SMALLER ctx than Auto's own target, through the identical VRAM-clamp/
   ratchet plan -- resident still wins unconditionally when it is >= Auto's
   target, or when native ctx is unknown/invalid.

A config-read failure (section 3) still falls back to the old flat
CTX_FALLBACK_DEFAULT (4096) as the last-resort safe default -- see that
test's docstring for why Auto is not attempted there. Small-native and
already-covered decoupling scenarios where native <= AUTO_CONTEXT_TOKENS are
untouched (Auto and the pre-T1 default agree there) and stay in
``test_llm_budget_engine.py`` / ``test_context_decoupling.py``.
"""

from __future__ import annotations

import logging
import time
from types import SimpleNamespace

import pytest

from opencohost.core.context.context_budget import clamp_ctx_for_vram
from opencohost.core.engine import llm_engine_models
from opencohost.core.engine.llm_engine_models import ModelManagementMixin
from opencohost.core.observability.health_monitor import OllamaResidencySnapshot

GIB = 1024 ** 3


# ── 1. Pure clamp ─────────────────────────────────────────────────────────────


def test_clamp_unknown_vram_does_not_clamp():
    assert clamp_ctx_for_vram(16384, native=131072) == (16384, "vram_unknown")


def test_clamp_caps_to_native():
    granted, reason = clamp_ctx_for_vram(16384, native=8192)
    assert granted == 8192
    assert reason == "native_cap"


def test_clamp_spill_halves_once():
    assert clamp_ctx_for_vram(16384, native=131072, residency_ratio=0.90) == (8192, "spill_step_down")


def test_clamp_spill_never_goes_below_floor():
    assert clamp_ctx_for_vram(6144, native=131072, residency_ratio=0.5) == (4096, "spill_step_down")
    assert clamp_ctx_for_vram(4096, native=131072, residency_ratio=0.5) == (4096, "spill_at_floor")


def test_clamp_full_residency_keeps_request():
    assert clamp_ctx_for_vram(16384, native=131072, residency_ratio=0.99) == (16384, "resident_fits")


def test_clamp_low_free_vram_halves():
    # 8 GiB card: headroom = max(1 GiB, 15% of 8 GiB = 1.2 GiB) = 1.2 GiB
    granted, reason = clamp_ctx_for_vram(
        16384, native=131072, free_bytes=1 * GIB, total_bytes=8 * GIB,
    )
    assert (granted, reason) == (8192, "vram_headroom_step_down")


def test_clamp_headroom_uses_one_gib_minimum():
    # 4 GiB card: 15% = 0.6 GiB, so the 1 GiB minimum governs.
    granted, _ = clamp_ctx_for_vram(16384, native=131072, free_bytes=int(0.9 * GIB), total_bytes=4 * GIB)
    assert granted == 8192
    granted, reason = clamp_ctx_for_vram(16384, native=131072, free_bytes=int(1.1 * GIB), total_bytes=4 * GIB)
    assert (granted, reason) == (16384, "vram_ok")


def test_clamp_zero_total_is_unknown_not_low():
    # NVML unavailable reports free=0/total=0 — no evidence, never a clamp.
    assert clamp_ctx_for_vram(16384, native=131072, free_bytes=0, total_bytes=0) == (16384, "vram_unknown")


def test_clamp_request_below_floor_is_respected():
    assert clamp_ctx_for_vram(2048, native=131072, residency_ratio=0.5) == (2048, "spill_at_floor")


# ── 2. Resolver, policy "requested" ───────────────────────────────────────────


class _Engine(ModelManagementMixin):
    def __init__(self, monitor=None):
        self.health_monitor = monitor


def _requested(monkeypatch, tokens):
    calls = []

    def fake(model=None, config_file=None, config_dict=None):
        calls.append(model)
        return {"policy": "requested", "requested_tokens": tokens}

    monkeypatch.setattr(llm_engine_models, "get_model_context_settings", fake)
    return calls


def _snap(model="gemma4:e4b", ctx=16384, ratio=1.0, age=1.0):
    size = 8_000_000_000
    return OllamaResidencySnapshot(
        model=model,
        digest="sha256:x",
        size_bytes=size,
        size_vram_bytes=int(size * ratio),
        context_length=ctx,
        observed_at=time.time() - age,
    )


def _monitor(snap=None, free_mb=0.0, total_mb=0.0):
    return SimpleNamespace(
        residency_snapshot=snap,
        vram_guard=SimpleNamespace(free_mb=free_mb, total_mb=total_mb),
    )


def test_requested_replaces_cold_start_default(monkeypatch):
    calls = _requested(monkeypatch, 16384)
    engine = _Engine()
    assert engine._resolve_effective_ctx_limit("gemma4:e4b", native_ctx=131072) == 16384
    assert calls == ["gemma4:e4b"]


def test_requested_never_exceeds_native(monkeypatch):
    _requested(monkeypatch, 16384)
    assert _Engine()._resolve_effective_ctx_limit("small", native_ctx=8192) == 8192


def test_requested_wins_over_different_resident_ctx(monkeypatch):
    _requested(monkeypatch, 16384)
    engine = _Engine(_monitor(_snap(ctx=4096)))
    assert engine._resolve_effective_ctx_limit("gemma4:e4b", native_ctx=131072) == 16384


def test_resident_at_requested_ctx_keeps_it(monkeypatch):
    _requested(monkeypatch, 16384)
    engine = _Engine(_monitor(_snap(ctx=16384, ratio=1.0)))
    assert engine._resolve_effective_ctx_limit("gemma4:e4b", native_ctx=131072) == 16384


def test_spill_steps_down_once_and_sticks(monkeypatch):
    _requested(monkeypatch, 16384)
    monitor = _monitor(_snap(ctx=16384, ratio=0.80))
    engine = _Engine(monitor)
    assert engine._resolve_effective_ctx_limit("gemma4:e4b", native_ctx=131072) == 8192
    # Same spilling snapshot seen again before the reload: no second halving.
    assert engine._resolve_effective_ctx_limit("gemma4:e4b", native_ctx=131072) == 8192
    # Reloaded at 8192 and fits: stays at 8192 (no bounce back to 16384).
    monitor.residency_snapshot = _snap(ctx=8192, ratio=1.0)
    assert engine._resolve_effective_ctx_limit("gemma4:e4b", native_ctx=131072) == 8192
    # Reloaded at 8192 and still spills: one more step down.
    monitor.residency_snapshot = _snap(ctx=8192, ratio=0.9)
    assert engine._resolve_effective_ctx_limit("gemma4:e4b", native_ctx=131072) == 4096


def test_step_down_resets_when_config_target_changes(monkeypatch):
    _requested(monkeypatch, 16384)
    engine = _Engine(_monitor(_snap(ctx=16384, ratio=0.80)))
    assert engine._resolve_effective_ctx_limit("gemma4:e4b", native_ctx=131072) == 8192
    _requested(monkeypatch, 12288)
    engine.health_monitor = _monitor(None)
    assert engine._resolve_effective_ctx_limit("gemma4:e4b", native_ctx=131072) == 12288


def test_stale_spill_snapshot_is_ignored(monkeypatch):
    _requested(monkeypatch, 16384)
    engine = _Engine(_monitor(_snap(ctx=16384, ratio=0.5, age=120.0)))
    assert engine._resolve_effective_ctx_limit("gemma4:e4b", native_ctx=131072) == 16384


def test_low_free_vram_with_nothing_resident_halves_without_sticking(monkeypatch):
    _requested(monkeypatch, 16384)
    monitor = _monitor(None, free_mb=512.0, total_mb=8192.0)
    engine = _Engine(monitor)
    assert engine._resolve_effective_ctx_limit("gemma4:e4b", native_ctx=131072) == 8192
    # Headroom recovered: the pre-load clamp is advisory, not a ratchet.
    monitor.vram_guard = SimpleNamespace(free_mb=6000.0, total_mb=8192.0)
    assert engine._resolve_effective_ctx_limit("gemma4:e4b", native_ctx=131072) == 16384


def test_free_vram_ignored_while_another_model_is_resident(monkeypatch):
    # NVML free is consumed by the OTHER model, which Ollama evicts on load
    # (OLLAMA_MAX_LOADED_MODELS=1): it is not evidence about our headroom.
    _requested(monkeypatch, 16384)
    engine = _Engine(_monitor(_snap(model="qwen3:8b", ctx=4096), free_mb=100.0, total_mb=8192.0))
    assert engine._resolve_effective_ctx_limit("gemma4:e4b", native_ctx=131072) == 16384


def test_cold_start_unknown_vram_does_not_clamp_or_crash(monkeypatch):
    _requested(monkeypatch, 16384)
    engine = _Engine(_monitor(None, free_mb=0.0, total_mb=0.0))
    assert engine._resolve_effective_ctx_limit("gemma4:e4b", native_ctx=131072) == 16384


def test_ctx_plan_logged_only_on_change(monkeypatch, caplog):
    _requested(monkeypatch, 16384)
    engine = _Engine()
    with caplog.at_level(logging.INFO, logger="OpenCohost"):
        for _ in range(3):
            engine._resolve_effective_ctx_limit("gemma4:e4b", native_ctx=131072)
    lines = [r.getMessage() for r in caplog.records if "ctx_plan:" in r.getMessage()]
    assert lines == ["ctx_plan: model=gemma4:e4b requested=16384 granted=16384 reason=vram_unknown"]


# ── 3. Fail-open / unset config ───────────────────────────────────────────────


def test_config_read_failure_falls_back_to_runtime_truth(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("corrupt")

    monkeypatch.setattr(llm_engine_models, "get_model_context_settings", boom)
    assert _Engine()._resolve_effective_ctx_limit("gemma4:e4b", native_ctx=131072) == 4096


def test_runtime_truth_policy_keeps_resident_ctx_when_larger_than_auto(monkeypatch):
    """Resident truth (step 1, ADR-056 P0) still wins over Auto when it is
    ALREADY >= Auto's own VRAM-bounded target, regardless of
    ``context.policy`` -- unchanged by T1b (see the ADR-056 Amendment,
    2026-09-28). This is what keeps ``test_runtime_residency_probe_takes_
    precedence_when_resident`` (test_context_decoupling.py) true even when
    the discovered ``native_ctx`` disagrees with what is actually resident:
    there, native=4096 -> Auto target = min(4096, 16384) = 4096, and the
    resident 16384 is larger, so resident still wins.
    """
    monkeypatch.setattr(
        llm_engine_models,
        "get_model_context_settings",
        lambda *a, **k: {"policy": "runtime_truth", "requested_tokens": None},
    )
    # native=4096 -> Auto target = min(4096, 16384) = 4096; resident (16384)
    # is larger, so it wins unconditionally.
    engine = _Engine(_monitor(_snap(ctx=16384)))
    assert engine._resolve_effective_ctx_limit("gemma4:e4b", native_ctx=4096) == 16384


def test_runtime_truth_policy_upgrades_resident_ctx_smaller_than_auto(monkeypatch):
    """T1b (model_context_auto_config_20260928, owner-approved 2026-09-28):
    a model resident at a SMALLER ctx than Auto's own VRAM-bounded target
    (e.g. Ollama's ``keep_alive`` kept an earlier cold-start default
    resident) is no longer stuck there forever -- Auto now behaves like an
    explicit target and requests the upgrade, through the SAME VRAM-clamp/
    ratchet machinery (``_apply_vram_ctx_plan``) an explicit override uses.
    See the ADR-056 Amendment (2026-09-28). This is the literal defect T1b
    fixes: Qwen3.8-9B (native 262144) resident at 4096 from an earlier
    keep_alive load never got upgraded to Auto before this fix -- every turn
    re-requested the resident 4096 and it stuck.
    """
    monkeypatch.setattr(
        llm_engine_models,
        "get_model_context_settings",
        lambda *a, **k: {"policy": "runtime_truth", "requested_tokens": None},
    )
    # native=131072 (known) -> Auto target = min(131072, 16384) = 16384;
    # resident (4096) is smaller, so it is upgraded through the VRAM-clamp
    # plan (no residency/NVML evidence at the target ceiling here ->
    # "vram_unknown", nothing shrinks it).
    engine = _Engine(_monitor(_snap(ctx=4096)))
    assert engine._resolve_effective_ctx_limit("gemma4:e4b", native_ctx=131072) == 16384


def test_unknown_native_ctx_keeps_resident_even_when_smaller_than_auto_cap(monkeypatch):
    """T1b: when native ctx itself is unknown/invalid, never guess an
    upgrade target from an unreliable ceiling -- resident truth wins
    unconditionally, exactly as before T1b.
    """
    monkeypatch.setattr(
        llm_engine_models,
        "get_model_context_settings",
        lambda *a, **k: {"policy": "runtime_truth", "requested_tokens": None},
    )
    engine = _Engine(_monitor(_snap(ctx=4096)))
    assert engine._resolve_effective_ctx_limit("gemma4:e4b", native_ctx=None) == 4096
    assert engine._resolve_effective_ctx_limit("gemma4:e4b", native_ctx=0) == 4096
    assert engine._resolve_effective_ctx_limit("gemma4:e4b", native_ctx="invalid") == 4096


def test_resident_upgrade_never_bounces_above_existing_ratchet(monkeypatch):
    """T1b: an upgrade attempt (resident < Auto) must never bounce back
    ABOVE a ceiling a PRIOR spill already ratcheted down for this model, and
    must settle (no further reload requested) once resident reaches that
    ratcheted ceiling -- the exact ceiling-preservation state
    ``_apply_vram_ctx_plan`` already gives an explicit override (see
    ``test_spill_steps_down_once_and_sticks`` above), reused identically
    here for Auto's own upgrade path.
    """
    monkeypatch.setattr(
        llm_engine_models,
        "get_model_context_settings",
        lambda *a, **k: {"policy": "runtime_truth", "requested_tokens": None},
    )
    engine = _Engine(_monitor(_snap(ctx=4096)))
    # A prior spill (e.g. observed during an earlier cold-start Auto
    # attempt) already ratcheted this model's ceiling down to 8192; Auto's
    # raw target (16384) is unchanged.
    engine._ctx_plan_state = {"gemma4:e4b": {"target": 16384, "ceiling": 8192, "logged": None}}

    # Resident (4096) is smaller than the RATCHETED ceiling (8192): the
    # upgrade is bounded by 8192, never bouncing back up to the raw 16384
    # target.
    assert engine._resolve_effective_ctx_limit("gemma4:e4b", native_ctx=131072) == 8192

    # Resident catches up to the ratcheted ceiling and fits: settle, no
    # further reload requested (no bounce, no loop).
    engine.health_monitor = _monitor(_snap(ctx=8192, ratio=1.0))
    assert engine._resolve_effective_ctx_limit("gemma4:e4b", native_ctx=131072) == 8192


def test_unconfigured_model_cold_start_gets_auto_regardless_of_policy(monkeypatch):
    """T1 (model_context_auto_config_20260928): at a true cold start (nothing
    resident), Auto = min(native, AUTO_CONTEXT_TOKENS) applies REGARDLESS of
    ``context.policy`` -- "runtime_truth" no longer means "request nothing"
    for an unconfigured model; it only means "no EXPLICIT override is
    honored". Auto runs through the SAME VRAM-clamp plan machinery as an
    explicit "requested" override (here: no residency, no NVML data ->
    "vram_unknown", nothing shrinks it).
    """
    monkeypatch.setattr(
        llm_engine_models,
        "get_model_context_settings",
        lambda *a, **k: {"policy": "runtime_truth", "requested_tokens": None},
    )
    engine = _Engine()
    assert engine._resolve_effective_ctx_limit("gemma4:e4b", native_ctx=131072) == 16384


def test_unconfigured_small_native_model_keeps_native_under_auto_cap(monkeypatch):
    """Acceptance: unconfigured small-native models keep min(native, 16384)."""
    monkeypatch.setattr(
        llm_engine_models,
        "get_model_context_settings",
        lambda *a, **k: {"policy": "runtime_truth", "requested_tokens": None},
    )
    engine = _Engine()
    assert engine._resolve_effective_ctx_limit("llama3", native_ctx=8192) == 8192


def test_unconfigured_model_auto_low_vram_headroom_halves_without_sticking(monkeypatch):
    """Auto goes through the SAME NVML advisory step-down as an explicit
    override (mirrors ``test_low_free_vram_with_nothing_resident_halves_
    without_sticking`` above, for an unconfigured model). Nothing is
    resident in either call, so step 1 never intercepts and Auto's plan
    (step 2) runs both times.
    """
    monkeypatch.setattr(
        llm_engine_models,
        "get_model_context_settings",
        lambda *a, **k: {"policy": "runtime_truth", "requested_tokens": None},
    )
    monitor = _monitor(None, free_mb=512.0, total_mb=8192.0)
    engine = _Engine(monitor)
    assert engine._resolve_effective_ctx_limit("gemma4:e4b", native_ctx=131072) == 8192
    # Headroom recovered: the pre-load clamp is advisory, not a ratchet.
    monitor.vram_guard = SimpleNamespace(free_mb=6000.0, total_mb=8192.0)
    assert engine._resolve_effective_ctx_limit("gemma4:e4b", native_ctx=131072) == 16384
