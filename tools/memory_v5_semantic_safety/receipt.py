"""
Benchmark Receipt Publisher & Formatter (BenchmarkReceiptV1 JSON, Markdown & Diagnostics).

Module Responsibilities:
1. Canonical BenchmarkReceiptV1 serialization and deserialization.
2. Exhaustive closed schema and state nullability validation (exact required keys, no extras, no missing).
3. Frozen model ID and metadata enforcement.
4. Candidate identity cross-consistency and exact candidate_id formula recomputation.
5. Finite numeric value enforcement (rejection of NaN / Infinity).
6. Deterministic Markdown report rendering from the receipt model.
7. Metadata-only diagnostics.json builder (zero PII, zero raw payloads).
8. Single-root atomic benchmark run publisher with atomic replacement.
"""

import json
import math
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from tools.memory_v5_semantic_safety.candidates import compute_candidate_id
from tools.memory_v5_semantic_safety.metrics import CRITICAL_REASONS_ORDER
from tools.memory_v5_semantic_safety.models import (
    BenchmarkReceiptV1,
    FrozenDict,
)
from tools.memory_v5_semantic_safety.routing import (
    CLOSED_GLOBAL_INVALID_REASONS,
    TERMINAL_ROUTES,
)

RECEIPT_SCHEMA_VERSION = "memory-semantic-safety-refinement-receipt-v1"
DIAGNOSTICS_SCHEMA_VERSION = "memory-semantic-safety-refinement-diagnostics-v1"
FROZEN_MODEL_ID = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
FROZEN_LOCK_IDENTITY = "cpython-3.10.20-Windows-AMD64"
FROZEN_ADR_REFERENCE = "ADR-053"
FROZEN_SEED = 42

HEX_64_REGEX = re.compile(r"^[0-9a-f]{64}$")

RECEIPT_TOP_LEVEL_FIELDS: Set[str] = {
    "schema_version",
    "run_id",
    "state",
    "global_invalid_reason",
    "locked_fixture_hash",
    "calibration_fixture_hash",
    "model_id",
    "model_artifact_hash",
    "shared_config_hash",
    "seed",
    "lock_identity",
    "adr_reference",
    "candidates",
    "metrics",
    "winner",
    "terminal_route",
}

CANDIDATE_FIELDS: Set[str] = {
    "variant_key",
    "execution_status",
    "execution_reason",
    "quality_status",
    "critical_reasons",
    "calibration",
}

CALIBRATION_FIELDS: Set[str] = {
    "candidate_id",
    "mechanism_key",
    "selected_params",
    "calibration_fixture_hash",
    "model_artifact_hash",
    "shared_config_hash",
    "frozen_at",
    "frozen_sequence",
    "cal_syn_para_mrr3_mean",
    "cal_hard_recall_at_1",
    "cal_no_memory_abstention_accuracy",
    "cal_no_memory_false_injection_count",
    "cal_profile_privacy_leakage_count",
}

CANDIDATE_METRICS_FIELDS: Set[str] = {
    "hard_correct_at_1_count",
    "hard_correct_at_3_count",
    "hard_recall_at_1",
    "hard_recall_at_3",
    "hard_mrr_at_3",
    "syn_correct_at_1_count",
    "syn_correct_at_3_count",
    "syn_recall_at_1",
    "syn_recall_at_3",
    "syn_mrr_at_3",
    "para_correct_at_1_count",
    "para_correct_at_3_count",
    "para_recall_at_1",
    "para_recall_at_3",
    "para_mrr_at_3",
    "semantic_quality_score",
    "no_memory_abstention_count",
    "no_memory_false_injection_count",
    "no_memory_abstention_accuracy",
    "near_wrong_correct_at_1_count",
    "near_wrong_precision_at_1",
    "near_wrong_target_mrr_at_3",
    "profile_isolation_pass_count",
    "profile_privacy_leakage_count",
    "profile_private_leakage_count",
    "profile_inactive_leakage_count",
    "profile_cross_profile_leakage_count",
    "stale_correct_count",
    "stale_correct_rate",
    "retention_vs_b",
    "gain_over_a",
}

BASELINE_METRICS_FIELDS: Set[str] = {
    "hard_recall_at_1",
    "semantic_quality_score",
    "no_memory_false_injection_count",
    "near_wrong_correct_at_1_count",
    "profile_privacy_leakage_count",
}

CLOSED_CANDIDATE_EXECUTION_REASONS: Set[str] = {
    "DEPENDENCY_UNAVAILABLE",
    "MODEL_UNAVAILABLE",
    "MODEL_HASH_MISMATCH",
    "CONFIG_INVALID",
    "CALIBRATION_LEAK",
    "CALIBRATION_FAILED",
    "EXECUTION_FAILED",
    "METRICS_INCOMPLETE",
}

CLOSED_CRITICAL_REASONS: Set[str] = set(CRITICAL_REASONS_ORDER)


def _validate_hex_hash(val: Any, field_name: str) -> None:
    if not isinstance(val, str) or not HEX_64_REGEX.match(val):
        raise ValueError(f"Field '{field_name}' must be a 64-character lowercase hex SHA-256 hash, got '{val}'")


def _validate_exact_dict_keys(d: Dict[str, Any], expected_keys: Set[str], context_name: str) -> None:
    actual_keys = set(d.keys())
    if actual_keys != expected_keys:
        missing = expected_keys - actual_keys
        extra = actual_keys - expected_keys
        msg_parts = []
        if missing:
            msg_parts.append(f"missing required fields: {sorted(missing)}")
        if extra:
            msg_parts.append(f"unknown extra fields: {sorted(extra)}")
        raise ValueError(f"Schema violation in {context_name}: {', '.join(msg_parts)}")


def _assert_all_numbers_finite(obj: Any, path: str = "root") -> None:
    """Recursively ensure that no float is NaN, +Inf, or -Inf."""
    if isinstance(obj, float):
        if not math.isfinite(obj):
            raise ValueError(f"Non-finite numeric value {obj} at {path}")
    elif isinstance(obj, (dict, FrozenDict)):
        for k, v in obj.items():
            _assert_all_numbers_finite(v, f"{path}.{k}")
    elif isinstance(obj, (list, tuple)):
        for idx, v in enumerate(obj):
            _assert_all_numbers_finite(v, f"{path}[{idx}]")


def validate_receipt_schema(receipt: BenchmarkReceiptV1) -> None:
    """
    Perform exhaustive closed schema validation on BenchmarkReceiptV1:
    - Enforce constant schema identifiers (schema_version, model_id, lock_identity, adr_reference, seed)
    - Enforce non-empty run_id
    - Enforce 64-char lowercase hex hash format
    - Enforce strict nullability matrix for VALID vs GLOBAL_INVALID states
    - Enforce exact required schema keys across candidates, calibration, candidate metrics, and baseline metrics
    - Enforce candidate identity cross-consistency (key == variant_key == mechanism_key)
    - Enforce recomputed candidate_id == calibration.candidate_id
    - Enforce nested candidate hash consistency against top-level receipt hashes
    - Enforce candidate critical reasons canonical ordering, deduplication, and quality status invariants
    - Enforce all numeric values are finite real numbers
    """
    if receipt.schema_version != RECEIPT_SCHEMA_VERSION:
        raise ValueError(f"Invalid schema_version '{receipt.schema_version}', expected '{RECEIPT_SCHEMA_VERSION}'")

    if not isinstance(receipt.run_id, str) or not receipt.run_id.strip():
        raise ValueError("Field 'run_id' must be a non-empty string")

    if receipt.model_id != FROZEN_MODEL_ID:
        raise ValueError(f"Invalid model_id '{receipt.model_id}', expected exact frozen model '{FROZEN_MODEL_ID}'")

    if receipt.seed != FROZEN_SEED:
        raise ValueError(f"Invalid seed {receipt.seed}, expected {FROZEN_SEED}")

    if receipt.lock_identity != FROZEN_LOCK_IDENTITY:
        raise ValueError(f"Invalid lock_identity '{receipt.lock_identity}', expected '{FROZEN_LOCK_IDENTITY}'")

    if receipt.adr_reference != FROZEN_ADR_REFERENCE:
        raise ValueError(f"Invalid adr_reference '{receipt.adr_reference}', expected '{FROZEN_ADR_REFERENCE}'")

    _validate_hex_hash(receipt.locked_fixture_hash, "locked_fixture_hash")
    _validate_hex_hash(receipt.calibration_fixture_hash, "calibration_fixture_hash")
    _validate_hex_hash(receipt.model_artifact_hash, "model_artifact_hash")
    _validate_hex_hash(receipt.shared_config_hash, "shared_config_hash")

    if receipt.terminal_route not in TERMINAL_ROUTES:
        raise ValueError(f"Invalid terminal_route '{receipt.terminal_route}' not in {TERMINAL_ROUTES}")

    # Nullability & State Matrix
    if receipt.state == "GLOBAL_INVALID":
        if receipt.global_invalid_reason is None:
            raise ValueError("GLOBAL_INVALID state must have non-null global_invalid_reason")
        if receipt.global_invalid_reason not in CLOSED_GLOBAL_INVALID_REASONS:
            raise ValueError(f"global_invalid_reason '{receipt.global_invalid_reason}' not in closed enumeration")
        if receipt.candidates is not None:
            raise ValueError("GLOBAL_INVALID state must have candidates=None")
        if receipt.metrics is not None:
            raise ValueError("GLOBAL_INVALID state must have metrics=None")
        if receipt.winner is not None:
            raise ValueError("GLOBAL_INVALID state must have winner=None")
        if receipt.terminal_route != "GLOBAL_INVALID":
            raise ValueError(f"GLOBAL_INVALID state must have terminal_route='GLOBAL_INVALID', got '{receipt.terminal_route}'")

    elif receipt.state == "VALID":
        if receipt.global_invalid_reason is not None:
            raise ValueError("VALID state must have global_invalid_reason=None")
        if receipt.candidates is None or not isinstance(receipt.candidates, (dict, FrozenDict)):
            raise ValueError("VALID state must have non-null candidates dictionary")
        if receipt.metrics is None or not isinstance(receipt.metrics, (dict, FrozenDict)):
            raise ValueError("VALID state must have non-null metrics dictionary")
        if receipt.terminal_route == "GLOBAL_INVALID":
            raise ValueError("VALID state cannot have terminal_route='GLOBAL_INVALID'")

        # Require baseline_a and baseline_b in metrics for VALID receipt
        if "baseline_a" not in receipt.metrics:
            raise ValueError("VALID receipt metrics must contain 'baseline_a'")
        if "baseline_b" not in receipt.metrics:
            raise ValueError("VALID receipt metrics must contain 'baseline_b'")

        # Validate baselines exact fields
        for b_key in ("baseline_a", "baseline_b"):
            b_metrics = receipt.metrics[b_key]
            if not isinstance(b_metrics, (dict, FrozenDict)):
                raise ValueError(f"Baseline metrics '{b_key}' must be a dictionary")
            _validate_exact_dict_keys(b_metrics, BASELINE_METRICS_FIELDS, f"metrics for '{b_key}'")

        # Closed metrics keys: candidate keys + baseline_a + baseline_b
        expected_metrics_keys = set(receipt.candidates.keys()) | {"baseline_a", "baseline_b"}
        actual_metrics_keys = set(receipt.metrics.keys())
        if actual_metrics_keys != expected_metrics_keys:
            extra = actual_metrics_keys - expected_metrics_keys
            missing = expected_metrics_keys - actual_metrics_keys
            raise ValueError(f"Receipt metrics key mismatch: extra={extra}, missing={missing}")

        # Validate candidates
        for c_key, c_data in receipt.candidates.items():
            if not isinstance(c_data, (dict, FrozenDict)):
                raise ValueError(f"Candidate '{c_key}' must be a dictionary")

            _validate_exact_dict_keys(c_data, CANDIDATE_FIELDS, f"candidate '{c_key}'")

            var_key = c_data.get("variant_key")
            if var_key != c_key:
                raise ValueError(f"Candidate key mismatch: dict key '{c_key}' != variant_key '{var_key}'")

            exec_status = c_data.get("execution_status")
            exec_reason = c_data.get("execution_reason")
            qual_status = c_data.get("quality_status")
            crit_reasons = c_data.get("critical_reasons")
            cal_data = c_data.get("calibration")

            if exec_status == "NON_EVALUABLE":
                if exec_reason is None:
                    raise ValueError(f"NON_EVALUABLE candidate '{c_key}' must have non-null execution_reason")
                if exec_reason not in CLOSED_CANDIDATE_EXECUTION_REASONS:
                    raise ValueError(f"Candidate '{c_key}' has unknown execution_reason='{exec_reason}'")
                if qual_status != "NOT_APPLICABLE":
                    raise ValueError(f"NON_EVALUABLE candidate '{c_key}' must have quality_status='NOT_APPLICABLE'")
                if crit_reasons not in ([], (), None):
                    raise ValueError(f"NON_EVALUABLE candidate '{c_key}' must have empty critical_reasons")
                if cal_data is not None:
                    raise ValueError(f"NON_EVALUABLE candidate '{c_key}' must have calibration=None")
                if receipt.metrics.get(c_key) is not None:
                    raise ValueError(f"NON_EVALUABLE candidate '{c_key}' must have metrics=None")

            elif exec_status == "EVALUABLE":
                if exec_reason is not None:
                    raise ValueError(f"EVALUABLE candidate '{c_key}' must have execution_reason=None")
                if qual_status not in ("QUALIFIED", "CRITICAL_REGRESSION"):
                    raise ValueError(f"EVALUABLE candidate '{c_key}' has invalid quality_status='{qual_status}'")

                if qual_status == "QUALIFIED":
                    if crit_reasons not in ([], (), None):
                        raise ValueError(f"QUALIFIED candidate '{c_key}' must have empty critical_reasons, got {crit_reasons}")
                elif qual_status == "CRITICAL_REGRESSION":
                    if not crit_reasons:
                        raise ValueError(f"CRITICAL_REGRESSION candidate '{c_key}' must have non-empty critical_reasons")
                    # Check deduplication and canonical ordering
                    crit_list = list(crit_reasons)
                    if len(crit_list) != len(set(crit_list)):
                        raise ValueError(f"Candidate '{c_key}' has duplicate critical_reasons: {crit_list}")
                    expected_order = [r for r in CRITICAL_REASONS_ORDER if r in set(crit_list)]
                    if crit_list != expected_order:
                        raise ValueError(
                            f"Candidate '{c_key}' critical_reasons not in canonical order: {crit_list} != {expected_order}"
                        )

                if cal_data is None or not isinstance(cal_data, (dict, FrozenDict)):
                    raise ValueError(f"EVALUABLE candidate '{c_key}' must have non-null dictionary calibration")

                _validate_exact_dict_keys(cal_data, CALIBRATION_FIELDS, f"calibration record for '{c_key}'")

                # Candidate identity cross-consistency check
                mech_key = cal_data.get("mechanism_key")
                if mech_key != c_key:
                    raise ValueError(f"Mechanism key mismatch for '{c_key}': '{mech_key}' != '{c_key}'")

                # Recompute exact candidate ID
                sel_params = cal_data.get("selected_params", {})
                recomputed_id = compute_candidate_id(
                    mechanism_key=mech_key,
                    params=dict(sel_params),
                    model_hash=receipt.model_artifact_hash,
                    config_hash=receipt.shared_config_hash,
                )
                if cal_data.get("candidate_id") != recomputed_id:
                    raise ValueError(
                        f"Candidate ID mismatch for '{c_key}': recorded '{cal_data.get('candidate_id')}' != recomputed '{recomputed_id}'"
                    )

                # Nested hash consistency check
                nested_cal_hash = cal_data.get("calibration_fixture_hash")
                nested_mod_hash = cal_data.get("model_artifact_hash")
                nested_cfg_hash = cal_data.get("shared_config_hash")

                if nested_cal_hash != receipt.calibration_fixture_hash:
                    raise ValueError(
                        f"Nested calibration_fixture_hash mismatch for candidate '{c_key}': "
                        f"'{nested_cal_hash}' != '{receipt.calibration_fixture_hash}'"
                    )
                if nested_mod_hash != receipt.model_artifact_hash:
                    raise ValueError(
                        f"Nested model_artifact_hash mismatch for candidate '{c_key}': "
                        f"'{nested_mod_hash}' != '{receipt.model_artifact_hash}'"
                    )
                if nested_cfg_hash != receipt.shared_config_hash:
                    raise ValueError(
                        f"Nested shared_config_hash mismatch for candidate '{c_key}': "
                        f"'{nested_cfg_hash}' != '{receipt.shared_config_hash}'"
                    )

                c_metrics = receipt.metrics.get(c_key)
                if c_metrics is None or not isinstance(c_metrics, (dict, FrozenDict)):
                    raise ValueError(f"EVALUABLE candidate '{c_key}' must have non-null metrics dictionary")
                _validate_exact_dict_keys(c_metrics, CANDIDATE_METRICS_FIELDS, f"metrics for '{c_key}'")
            else:
                raise ValueError(f"Invalid execution_status '{exec_status}' for candidate '{c_key}'")

        if receipt.terminal_route == "SAFETY_REFINEMENT_PROMISING" and receipt.winner is None:
            raise ValueError("terminal_route 'SAFETY_REFINEMENT_PROMISING' requires a non-null winner")

        if receipt.winner is not None:
            if receipt.winner not in receipt.candidates:
                raise ValueError(f"Winner '{receipt.winner}' not in receipt candidates")
            winner_cand = receipt.candidates[receipt.winner]
            if winner_cand.get("quality_status") != "QUALIFIED":
                raise ValueError(f"Winner '{receipt.winner}' must have quality_status='QUALIFIED'")

        # Ensure all numeric leaves are finite (no NaN or Inf)
        _assert_all_numbers_finite(receipt.candidates, "receipt.candidates")
        _assert_all_numbers_finite(receipt.metrics, "receipt.metrics")
    else:
        raise ValueError(f"Invalid receipt state '{receipt.state}'")


def receipt_to_dict(receipt: BenchmarkReceiptV1) -> Dict[str, Any]:
    """Convert BenchmarkReceiptV1 to a canonical, JSON-serializable dictionary."""
    validate_receipt_schema(receipt)

    def _convert(val: Any) -> Any:
        if isinstance(val, (dict, FrozenDict)):
            return {k: _convert(v) for k, v in val.items()}
        if isinstance(val, (list, tuple)):
            return [_convert(v) for v in val]
        return val

    return {
        "schema_version": receipt.schema_version,
        "run_id": receipt.run_id,
        "state": receipt.state,
        "global_invalid_reason": receipt.global_invalid_reason,
        "locked_fixture_hash": receipt.locked_fixture_hash,
        "calibration_fixture_hash": receipt.calibration_fixture_hash,
        "model_id": receipt.model_id,
        "model_artifact_hash": receipt.model_artifact_hash,
        "shared_config_hash": receipt.shared_config_hash,
        "seed": receipt.seed,
        "lock_identity": receipt.lock_identity,
        "adr_reference": receipt.adr_reference,
        "candidates": _convert(receipt.candidates) if receipt.candidates is not None else None,
        "metrics": _convert(receipt.metrics) if receipt.metrics is not None else None,
        "winner": receipt.winner,
        "terminal_route": receipt.terminal_route,
    }


def receipt_from_dict(data: Dict[str, Any]) -> BenchmarkReceiptV1:
    """Reconstruct BenchmarkReceiptV1 from a dictionary with closed schema validation."""
    _validate_exact_dict_keys(data, RECEIPT_TOP_LEVEL_FIELDS, "top-level receipt")

    receipt = BenchmarkReceiptV1(
        schema_version=data.get("schema_version", ""),
        run_id=data.get("run_id", ""),
        state=data.get("state", ""),
        global_invalid_reason=data.get("global_invalid_reason"),
        locked_fixture_hash=data.get("locked_fixture_hash", ""),
        calibration_fixture_hash=data.get("calibration_fixture_hash", ""),
        model_id=data.get("model_id", ""),
        model_artifact_hash=data.get("model_artifact_hash", ""),
        shared_config_hash=data.get("shared_config_hash", ""),
        seed=data.get("seed", 0),
        lock_identity=data.get("lock_identity", ""),
        adr_reference=data.get("adr_reference", ""),
        candidates=data.get("candidates"),
        metrics=data.get("metrics"),
        winner=data.get("winner"),
        terminal_route=data.get("terminal_route", ""),
    )
    validate_receipt_schema(receipt)
    return receipt


def build_diagnostics_object(receipt: BenchmarkReceiptV1) -> Dict[str, Any]:
    """
    Build metadata-only diagnostics dictionary from BenchmarkReceiptV1.
    Strictly contains NO raw payloads, NO query text, and NO PII.
    """
    validate_receipt_schema(receipt)

    diag: Dict[str, Any] = {
        "schema_version": DIAGNOSTICS_SCHEMA_VERSION,
        "run_id": receipt.run_id,
        "state": receipt.state,
        "terminal_route": receipt.terminal_route,
        "winner": receipt.winner,
        "global_invalid_reason": receipt.global_invalid_reason,
        "authority_hashes": {
            "locked_fixture_hash": receipt.locked_fixture_hash,
            "calibration_fixture_hash": receipt.calibration_fixture_hash,
            "model_artifact_hash": receipt.model_artifact_hash,
            "shared_config_hash": receipt.shared_config_hash,
        },
        "model_id": receipt.model_id,
        "seed": receipt.seed,
        "lock_identity": receipt.lock_identity,
        "adr_reference": receipt.adr_reference,
    }

    if receipt.candidates is not None and receipt.metrics is not None:
        cand_summary: Dict[str, Any] = {}
        for c_key, c_data in receipt.candidates.items():
            cand_summary[c_key] = {
                "variant_key": c_data.get("variant_key"),
                "execution_status": c_data.get("execution_status"),
                "execution_reason": c_data.get("execution_reason"),
                "quality_status": c_data.get("quality_status"),
                "critical_reasons": list(c_data.get("critical_reasons", ())),
                "candidate_id": c_data["calibration"].get("candidate_id") if c_data.get("calibration") else None,
                "selected_params": dict(c_data["calibration"].get("selected_params", {})) if c_data.get("calibration") else None,
            }
        diag["candidates_summary"] = cand_summary
        diag["metrics_summary"] = dict(receipt.metrics)
    else:
        diag["candidates_summary"] = None
        diag["metrics_summary"] = None

    return diag


def render_markdown_report(receipt: BenchmarkReceiptV1) -> str:
    """Render authoritative Markdown report from BenchmarkReceiptV1."""
    validate_receipt_schema(receipt)

    lines: List[str] = [
        f"# Benchmark Report: {receipt.run_id}",
        "",
        "## Executive Summary",
        f"- **Terminal Route**: `{receipt.terminal_route}`",
        f"- **Selected Winner**: `{receipt.winner}`",
        f"- **Benchmark State**: `{receipt.state}`",
        f"- **Model ID**: `{receipt.model_id}`",
        f"- **Seed**: `{receipt.seed}`",
        f"- **ADR Reference**: `{receipt.adr_reference}`",
        f"- **Runtime Identity**: `{receipt.lock_identity}`",
        "",
        "## Hashes & Authority",
        f"- **Locked Fixture**: `{receipt.locked_fixture_hash}`",
        f"- **Calibration Fixture**: `{receipt.calibration_fixture_hash}`",
        f"- **Model Artifact**: `{receipt.model_artifact_hash}`",
        f"- **Shared Config**: `{receipt.shared_config_hash}`",
        "",
    ]

    if receipt.state == "GLOBAL_INVALID":
        lines.extend([
            "## Global Invalidation",
            f"- **Reason**: `{receipt.global_invalid_reason}`",
            "",
        ])
        return "\n".join(lines) + "\n"

    lines.extend([
        "## Candidate Results Summary",
        "| Candidate | Status | Quality | Hard Recall@1 | Syn MRR@3 | Para MRR@3 | Semantic Score | Retention vs B | Gain vs A |",
        "| :--- | :--- | :--- | :---: | :---: | :---: | :---: | :---: | :---: |",
    ])

    if receipt.candidates and receipt.metrics:
        for c_key, c_data in receipt.candidates.items():
            m_data = receipt.metrics.get(c_key, {})
            status = c_data.get("execution_status", "N/A")
            qual = c_data.get("quality_status", "N/A")
            if m_data:
                hr1_val = m_data.get("hard_recall_at_1")
                smrr_val = m_data.get("syn_mrr_at_3")
                pmrr_val = m_data.get("para_mrr_at_3")
                score_val = m_data.get("semantic_quality_score")
                ret_val = m_data.get("retention_vs_b")
                gain_val = m_data.get("gain_over_a")

                hr1 = f"{hr1_val:.4f}" if hr1_val is not None else "N/A"
                smrr = f"{smrr_val:.4f}" if smrr_val is not None else "N/A"
                pmrr = f"{pmrr_val:.4f}" if pmrr_val is not None else "N/A"
                score = f"{score_val:.4f}" if score_val is not None else "N/A"
                ret = f"{ret_val:.4f}" if ret_val is not None else "N/A"
                gain = f"{gain_val:.4f}" if gain_val is not None else "N/A"
            else:
                hr1, smrr, pmrr, score, ret, gain = "N/A", "N/A", "N/A", "N/A", "N/A", "N/A"

            lines.append(f"| `{c_key}` | {status} | {qual} | {hr1} | {smrr} | {pmrr} | {score} | {ret} | {gain} |")

    lines.extend([
        "",
        "## Architectural Recommendation",
        f"Based on the evaluated evidence, the lifecycle concluded with **`{receipt.terminal_route}`**.",
        "",
    ])

    return "\n".join(lines) + "\n"


def _atomic_write_file(path: Path, content: str) -> None:
    """Write content to a file atomically via a temporary file in the same directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_fd, temp_path = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp_atomic_")
    try:
        with open(temp_fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(temp_path, path)
    except Exception:
        try:
            os.close(temp_fd)
        except OSError:
            pass
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass
        raise


def publish_benchmark_run(
    receipt: BenchmarkReceiptV1,
    output_dir: Optional[Path] = None,
) -> Tuple[Path, Path, Path, Optional[Path]]:
    """
    Atomically publish benchmark artifacts under single-root directory:
    - docs/memory_v5/generations/<run_id>/receipt.json
    - docs/memory_v5/generations/<run_id>/report.md
    - docs/memory_v5/generations/<run_id>/diagnostics.json
    - docs/memory_v5/current-generation.json (updated only on VALID state)
    Returns: (receipt_path, report_path, diagnostics_path, current_generation_path)
    """
    validate_receipt_schema(receipt)

    base_dir = output_dir if output_dir is not None else (Path(__file__).resolve().parents[2] / "docs" / "memory_v5")
    run_dir = base_dir / "generations" / receipt.run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    receipt_path = run_dir / "receipt.json"
    report_path = run_dir / "report.md"
    diagnostics_path = run_dir / "diagnostics.json"
    current_path = base_dir / "current-generation.json"

    # Write receipt.json atomically
    receipt_dict = receipt_to_dict(receipt)
    _atomic_write_file(receipt_path, json.dumps(receipt_dict, indent=2, ensure_ascii=False, allow_nan=False))

    # Write report.md atomically
    report_content = render_markdown_report(receipt)
    _atomic_write_file(report_path, report_content)

    # Write diagnostics.json atomically
    diagnostics_dict = build_diagnostics_object(receipt)
    _atomic_write_file(diagnostics_path, json.dumps(diagnostics_dict, indent=2, ensure_ascii=False, allow_nan=False))

    # Write current-generation.json atomically only on VALID runs
    current_ret: Optional[Path] = None
    if receipt.state == "VALID":
        current_data = {
            "latest_valid_run_id": receipt.run_id,
            "terminal_route": receipt.terminal_route,
            "winner": receipt.winner,
            "state": receipt.state,
        }
        _atomic_write_file(current_path, json.dumps(current_data, indent=2, ensure_ascii=False, allow_nan=False))
        current_ret = current_path

    return receipt_path, report_path, diagnostics_path, current_ret


def build_benchmark_receipt(
    run_id: str,
    state: str,
    terminal_route: str,
    winner: Optional[str],
    global_invalid_reason: Optional[str] = None,
    candidates: Optional[Dict[str, Any]] = None,
    metrics: Optional[Dict[str, Any]] = None,
    locked_fixture_hash: str = "",
    calibration_fixture_hash: str = "",
    model_artifact_hash: str = "",
    shared_config_hash: str = "",
    model_id: str = FROZEN_MODEL_ID,
) -> BenchmarkReceiptV1:
    """Helper factory for constructing BenchmarkReceiptV1 with closed defaults."""
    receipt = BenchmarkReceiptV1(
        schema_version=RECEIPT_SCHEMA_VERSION,
        run_id=run_id,
        state=state,
        global_invalid_reason=global_invalid_reason,
        locked_fixture_hash=locked_fixture_hash,
        calibration_fixture_hash=calibration_fixture_hash,
        model_id=model_id,
        model_artifact_hash=model_artifact_hash,
        shared_config_hash=shared_config_hash,
        seed=FROZEN_SEED,
        lock_identity=FROZEN_LOCK_IDENTITY,
        adr_reference=FROZEN_ADR_REFERENCE,
        candidates=candidates,
        metrics=metrics,
        winner=winner,
        terminal_route=terminal_route,
    )
    validate_receipt_schema(receipt)
    return receipt

