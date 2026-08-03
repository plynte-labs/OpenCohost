"""Single source of truth for the Qwen TTS lifecycle marker vocabulary.

Tests assert against these constants instead of ad-hoc strings, so the failure-class /
response / badge sets stay aligned and the legacy log literals cannot silently drift.
Stdlib-only and import-light (no torch / qwen_tts), so it is collectable in the CI interpreter.

Contract reference: conductor/tracks/qwen_tts_lifecycle_hardening_20260613/spec.md (F, SV-A, SV-B).
"""
from __future__ import annotations

# ── Failure classes (contract F1) ──
# Every outcome of the start / attach / poll layers resolves to exactly one of these.
FAILURE_CLASSES = (
    "success",
    "startup_or_model_load_failure",
    "transient_health_blip",
    "zombie_running_but_unhealthy",
    "xtts_python_unset",
    "manual_server_already_running",
    "port_5000_busy",
)

# ── Engine badge statuses (contract SV-A) ──
# Single source of truth; ui.state.VALID_ENGINE_STATUSES imports this in Phase 1 so the
# two can never diverge.
ENGINE_STATUSES = frozenset({
    "qwen_active",
    "edge_fallback",
    "qwen_starting",
    "not_configured",
    "piper_local",
    "unknown",
})

# ── Class -> owner-approved response (contract F2) ──
# Consumed by the failure handler (Phase 7). NO flat integer-retry loop: the response is
# driven entirely by the failure class.
RESPONSE_BY_CLASS = {
    "success": {"retries": 0, "restart": False, "action": "use_qwen"},
    "startup_or_model_load_failure": {"retries": 0, "restart": False, "action": "edge_fallback_notice"},
    "transient_health_blip": {"retries": 1, "restart": False, "action": "backoff_retry"},
    "zombie_running_but_unhealthy": {"retries": 0, "restart": True, "action": "restart_once"},
    "xtts_python_unset": {"retries": 0, "restart": False, "action": "config_message"},
    "manual_server_already_running": {"retries": 0, "restart": False, "action": "attach_only"},
    "port_5000_busy": {"retries": 0, "restart": False, "action": "warn"},
}

# ── Class -> engine badge status (contract SV-A) ──
# Every value MUST be a member of ENGINE_STATUSES (asserted by the contract test).
BADGE_BY_CLASS = {
    "success": "qwen_active",
    "startup_or_model_load_failure": "edge_fallback",
    "transient_health_blip": "edge_fallback",
    "zombie_running_but_unhealthy": "edge_fallback",
    "xtts_python_unset": "not_configured",
    "manual_server_already_running": "qwen_active",
    "port_5000_busy": "edge_fallback",
}

# ── Marker prefixes (consumed in later phases) ──
ACCEPT_TTS_ENGINE_PREFIX = "[ACCEPT] tts_engine"
QWEN_LIFECYCLE_PREFIX = "[QWEN-LIFECYCLE]"
QWEN_SELFCHECK_PREFIX = "[QWEN-SELFCHECK]"
QWEN_JOB_PREFIX = "[QWEN-JOB]"
QWEN_ORPHAN_PREFIX = "[QWEN-ORPHAN]"
QWEN_WATCHDOG_PREFIX = "[QWEN-WATCHDOG]"

# ── Pinned legacy log literals ──
# Must stay byte-identical to the lines emitted at llm_engine.py:1560-1561 / 1584 so the
# existing assertions in tests/test_llm_engine_timeouts.py stay green when the new [ACCEPT]
# markers are added alongside them.
ACCEPT_FALLBACK_MISSING_REFERENCE = (
    "Auto-fallback to Edge-TTS: requested=pesado effective=ligero reason=missing_reference"
)
ACCEPT_TTS_EFECTIVO_PESADO = "TTS efectivo: requested=pesado effective=pesado"
