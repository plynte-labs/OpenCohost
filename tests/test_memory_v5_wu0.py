"""Tests for Memory v5 WU0 Contract and Evidence Baseline."""

from __future__ import annotations

import json
from pathlib import Path
import pytest

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "memory_v5_expected_cases.json"

REQUIRED_CLASSES = frozenset({
    "lexical",
    "paraphrase",
    "temporal",
    "mixed",
    "stale_contradiction",
    "private",
    "pinned_curated",
    "profile_isolation",
    "no_memory",
})

REQUIRED_CASE_FIELDS = frozenset({
    "id",
    "class",
    "profile",
    "candidates",
    "query",
    "expected_ids",
    "expected_abstain",
})

REQUIRED_CANDIDATE_FIELDS = frozenset({
    "id",
    "profile_id",
    "title",
    "content",
    "signature",
    "pinned",
    "status",
    "is_private",
    "inactive",
    "created_at",
})

VALID_ROUTES = frozenset({
    "no change",
    "shadow evidence journal experiment",
    "episode/unit-quality experiment",
    "semantic retrieval benchmark",
})

ALLOWED_RECEIPT_KEYS = frozenset({
    "schema",
    "mode",
    "timestamp",
    "total_cases",
    "cases_evaluated",
    "outcomes",
    "class_breakdown",
    "latency_stats_ms",
    "peak_memory_bytes",
    "route",
    "route_reason",
    "gates",
    "uncertainty_notes",
    "sample_limits",
})


def test_fixture_file_exists_and_loads():
    """1.1/1.2: Fixture file must exist and be valid JSON."""
    assert FIXTURE_PATH.is_file(), f"Fixture file not found: {FIXTURE_PATH}"
    with open(FIXTURE_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    assert isinstance(data, dict)
    assert "schema" in data
    assert data["schema"] == "memory-v5-expected-cases/v1"
    assert "cases" in data
    assert isinstance(data["cases"], list)
    assert len(data["cases"]) >= 9


def test_fixture_covers_all_required_classes():
    """1.2: Every required query/evaluation class must be covered."""
    with open(FIXTURE_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    cases = data["cases"]
    classes_present = {c.get("class") for c in cases}
    missing = REQUIRED_CLASSES - classes_present
    assert not missing, f"Missing required fixture classes: {missing}"


def test_fixture_schema_and_types():
    """1.2: Validate schema integrity of each fixture case and candidate."""
    with open(FIXTURE_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    case_ids = set()
    for case in data["cases"]:
        assert isinstance(case, dict)
        missing_case_fields = REQUIRED_CASE_FIELDS - set(case.keys())
        assert not missing_case_fields, f"Case missing fields: {missing_case_fields}"
        assert case["id"] not in case_ids, f"Duplicate case id: {case['id']}"
        case_ids.add(case["id"])
        assert case["class"] in REQUIRED_CLASSES
        assert isinstance(case["profile"], str)
        assert isinstance(case["candidates"], list)
        assert isinstance(case["query"], str)
        assert isinstance(case["expected_ids"], list)
        assert isinstance(case["expected_abstain"], bool)

        for cand in case["candidates"]:
            assert isinstance(cand, dict)
            missing_cand_fields = REQUIRED_CANDIDATE_FIELDS - set(cand.keys())
            assert not missing_cand_fields, f"Candidate missing fields: {missing_cand_fields}"
            assert isinstance(cand["id"], int)
            assert isinstance(cand["profile_id"], str)
            assert isinstance(cand["title"], str)
            assert isinstance(cand["content"], str)
            assert isinstance(cand["signature"], str)
            assert isinstance(cand["pinned"], bool)
            assert isinstance(cand["status"], str)
            assert isinstance(cand["is_private"], bool)
            assert isinstance(cand["inactive"], bool)
            assert isinstance(cand["created_at"], str)


def test_evaluator_module_and_synthetic_evaluation():
    """2.1/2.2: tools.memory_v5_wu0 must exist and perform synthetic evaluation."""
    from tools.memory_v5_wu0 import run_synthetic_evaluation, evaluate_cases

    with open(FIXTURE_PATH, "r", encoding="utf-8") as f:
        fixture_data = json.load(f)

    receipt = run_synthetic_evaluation(fixture_data, iterations=5)
    assert isinstance(receipt, dict)
    assert set(receipt.keys()).issubset(ALLOWED_RECEIPT_KEYS)
    assert receipt["total_cases"] == len(fixture_data["cases"])
    assert receipt["cases_evaluated"] == len(fixture_data["cases"])
    assert "pass" in receipt["outcomes"]
    assert "latency_stats_ms" in receipt
    assert "p50" in receipt["latency_stats_ms"]
    assert receipt["route"] in VALID_ROUTES


def test_evaluator_metadata_only_privacy_allowlist():
    """2.1/2.2: Outputs must not contain raw private content, queries, or PII."""
    from tools.memory_v5_wu0 import run_synthetic_evaluation

    with open(FIXTURE_PATH, "r", encoding="utf-8") as f:
        fixture_data = json.load(f)

    receipt = run_synthetic_evaluation(fixture_data, iterations=2)
    receipt_json = json.dumps(receipt)

    # Must not contain private text or candidate content snippets
    forbidden_tokens = ["Dato confidencial", "clave wifi", "RTX 5090", "Shure SM7B"]
    for token in forbidden_tokens:
        assert token not in receipt_json, f"Leaked content token in receipt: {token}"


def test_evaluator_sqlite_readonly_mode(tmp_path):
    """2.1/2.2: SQLite connection must be strictly read-only (mode=ro)."""
    import sqlite3
    from tools.memory_v5_wu0 import open_readonly_db

    db_file = tmp_path / "test_memorias.db"
    conn_create = sqlite3.connect(str(db_file))
    conn_create.execute("CREATE TABLE memorias (id INTEGER PRIMARY KEY, profile_id TEXT, title TEXT);")
    conn_create.execute("INSERT INTO memorias VALUES (1, 'p1', 'title1');")
    conn_create.commit()
    conn_create.close()

    ro_conn = open_readonly_db(str(db_file))
    cursor = ro_conn.cursor()
    cursor.execute("SELECT id, profile_id, title FROM memorias;")
    rows = cursor.fetchall()
    assert len(rows) == 1

    # Any write attempt must fail
    with pytest.raises(sqlite3.OperationalError):
        ro_conn.execute("INSERT INTO memorias VALUES (2, 'p1', 'title2');")

    ro_conn.close()


def test_evaluator_routing_decision_rules():
    """2.1/2.3: Route selection must deterministically pick exactly one valid route."""
    from tools.memory_v5_wu0 import determine_terminal_route

    # Corrupt / insufficient data -> "no change"
    assert determine_terminal_route(cases_evaluated=0, outcomes={}, gates={}) == (
        "no change",
        "Insufficient or unavailable evaluation evidence forces no change fail-closed.",
    )

    # Paraphrase bottleneck with unproven privacy persistence -> semantic retrieval benchmark
    synthetic_outcomes = {
        "pass": 7,
        "miss": 1,  # paraphrase miss
        "false_injection": 0,
        "isolation_failure": 0,
        "unavailable": 0,
    }
    class_breakdown = {
        "paraphrase": {"pass": 0, "miss": 1},
        "lexical": {"pass": 1, "miss": 0},
    }
    route, reason = determine_terminal_route(
        cases_evaluated=8,
        outcomes=synthetic_outcomes,
        class_breakdown=class_breakdown,
        gates={"consent": "unmet", "retention": "unmet"},
    )
    assert route in VALID_ROUTES
    assert route == "semantic retrieval benchmark"


def test_production_code_does_not_import_tool():
    """2.2: Production opencohost code MUST NOT import tools.memory_v5_wu0."""
    import sys
    from pathlib import Path

    opencohost_dir = Path(__file__).parent.parent / "opencohost"
    for py_file in opencohost_dir.rglob("*.py"):
        content = py_file.read_text(encoding="utf-8")
        assert "memory_v5_wu0" not in content, f"Production file {py_file} imports memory_v5_wu0"
