"""Owner guardrail policy registry (guardrail-toggles).

A single table maps every non-negotiable rule id to a ``tier`` and a
``category``. ``locked`` rules (platform safety) can never be disabled;
``toggleable`` rules (persona/style) can be switched off by the owner. New
rules, toggleable or locked, are added by adding a row, not by rewiring the
guard.

Design constraints:
  * The disabled set is an immutable ``frozenset`` swapped atomically under a
    lock: the output guard runs from several threads and only ever reads one
    reference.
  * A locked or unknown id can never enter the disabled set, whatever the file
    says.
  * The module default is "everything enabled" in memory. Nothing reads disk
    until ``load_guardrails()`` (API startup), which keeps unrelated tests
    hermetic.
  * Load fails CLOSED: a missing/corrupt/invalid file leaves every rule on.
  * Logs carry rule ids and counts only, never reply text.

This module must not import ``opencohost.config.validation`` (the guard
imports this module); a test pins that the registry covers every id in
``validation.NON_NEGOTIABLE_IDS`` (the registry may also list always-enforced
rules that are not preset-level non-negotiables, e.g. ``no_code_output``,
``no_memory_denial``, ``no_false_capability``).
"""

from __future__ import annotations

import json
import os
import tempfile
import threading

from opencohost.config.logger import get_logger
from opencohost.config.settings import GUARDRAILS_FILE

logger = get_logger()

# rule id -> {tier, category}. Order is the display order of GET /api/guardrails.
REGISTRY: dict[str, dict[str, str]] = {
    "no_doxxing": {"tier": "locked", "category": "safety"},
    "no_suspicious_links": {"tier": "locked", "category": "safety"},
    "never_promise": {"tier": "locked", "category": "safety"},
    "never_invent_confirmations": {"tier": "locked", "category": "safety"},
    "never_moderate_automatically": {"tier": "locked", "category": "safety"},
    "no_personal_viewer_data": {"tier": "locked", "category": "safety"},
    "no_raw_spam_to_llm": {"tier": "locked", "category": "safety"},
    "no_hate_speech": {"tier": "locked", "category": "safety"},
    # Listed so the owner UI shows it. Enforcement in output_guard is
    # unconditional (never a registry lookup); it is NOT a preset-level
    # NON_NEGOTIABLE_IDS member, so saved configs and presets are unaffected.
    "no_code_output": {"tier": "locked", "category": "safety"},
    # runtime-fixes-20261110 T1: same governance as no_code_output.
    "no_memory_denial": {"tier": "locked", "category": "safety"},
    "no_false_capability": {"tier": "locked", "category": "safety"},
    # S4 (guardrails-vulnerable-users): same governance as no_code_output
    # (always enforced, locked, no owner toggle, not a NON_NEGOTIABLE_IDS member).
    "no_isolation_pressure": {"tier": "locked", "category": "safety"},
    "no_ai_self_identification": {"tier": "toggleable", "category": "persona"},
    "no_meta_commentary": {"tier": "toggleable", "category": "style"},
    "no_negative_engagement": {"tier": "toggleable", "category": "style"},
}

TOGGLEABLE_IDS: frozenset[str] = frozenset(
    rule_id for rule_id, spec in REGISTRY.items() if spec["tier"] == "toggleable"
)

_lock = threading.Lock()
_disabled: frozenset[str] = frozenset()


def validate_disabled(raw) -> tuple[frozenset[str], list[dict]]:
    """Validate a raw disabled-ids list. Returns ``(clean_set, errors)``.

    Errors are ``{"id": str, "code": "unknown" | "locked"}``; a payload that is
    not a list yields ``[{"id": "", "code": "invalid"}]``.
    """
    if not isinstance(raw, list):
        return frozenset(), [{"id": "", "code": "invalid"}]
    errors: list[dict] = []
    clean: set[str] = set()
    for item in raw:
        spec = REGISTRY.get(item) if isinstance(item, str) else None
        if spec is None:
            errors.append({"id": item if isinstance(item, str) else str(item), "code": "unknown"})
        elif spec["tier"] != "toggleable":
            errors.append({"id": item, "code": "locked"})
        else:
            clean.add(item)
    return frozenset(clean), errors


def _install(disabled: frozenset[str]) -> None:
    global _disabled
    with _lock:
        _disabled = disabled


def set_disabled(raw) -> list[dict]:
    """Validate and install in memory only. Returns errors ([] on success); on
    errors the previous policy stays active."""
    clean, errors = validate_disabled(raw)
    if errors:
        return errors
    _install(clean)
    return []


def reset_guardrails() -> None:
    """Back to the default policy (everything enabled). Does not touch disk."""
    _install(frozenset())


def get_disabled() -> frozenset[str]:
    return _disabled


def is_rule_enabled(rule_id: str) -> bool:
    """False only for a toggleable rule the owner disabled. Locked and unknown
    ids are always enforced."""
    return rule_id not in _disabled


def list_rules() -> list[dict]:
    disabled = _disabled
    return [
        {
            "id": rule_id,
            "tier": spec["tier"],
            "category": spec["category"],
            "enabled": rule_id not in disabled,
        }
        for rule_id, spec in REGISTRY.items()
    ]


def _log_policy(event: str, disabled: frozenset[str]) -> None:
    logger.info("[GUARDRAILS] policy disabled=%s (%s)", sorted(disabled), event)


def load_guardrails() -> int:
    """Load the policy from disk into memory, failing closed (all enabled) on a
    missing/corrupt/invalid file. Locked and unknown ids in the file are
    ignored. Returns the number of disabled rules."""
    path = GUARDRAILS_FILE
    disabled: frozenset[str] = frozenset()
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if not isinstance(raw, dict) or not isinstance(raw.get("disabled"), list):
                raise ValueError("not a policy object")
            ids = [i for i in raw["disabled"] if isinstance(i, str)]
            disabled = frozenset(i for i in ids if i in TOGGLEABLE_IDS)
        except Exception as e:
            logger.warning("Could not load guardrail policy: %s", type(e).__name__)
            disabled = frozenset()
    _install(disabled)
    _log_policy("loaded", disabled)
    return len(disabled)


def save_guardrails(raw):
    """Validate, write atomically (temp + os.replace) and install.

    Returns ``[]`` on success, the error list on validation failure, or
    ``None`` when the write failed (callers map that to 503). The in-memory
    policy only changes on success.
    """
    clean, errors = validate_disabled(raw)
    if errors:
        return errors
    path = GUARDRAILS_FILE
    target_dir = os.path.dirname(path) or "."
    tmp_path = None
    try:
        os.makedirs(target_dir, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=target_dir, prefix=".guardrails_", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"disabled": sorted(clean)}, f, indent=2)
        os.replace(tmp_path, path)
    except Exception as e:
        logger.warning("Failed to save guardrail policy: %s", type(e).__name__)
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        return None
    _install(clean)
    _log_policy("saved", clean)
    return []
