"""Tests for transactional SQLite ledger and migration (Phase 1, A1/A2/A3/A5).

Covers:
- Last-slot race condition between concurrent callers/threads/processes at 899/900.
- Reservation lifecycle: reserve() -> mark_dispatched() increments consumed once.
- Reservation release: reserve() -> release() consumes zero.
- Idempotency: mark_dispatched multiple times, release after release, release after dispatch, dispatch after release.
- Clock rollback protection: fails closed with reason "clock_rollback".
- Forward period transition: preserves previous months, starts new period at 0, updates high_water_period.
- Legacy JSON migration: valid atomic migration, fresh install, malformed fails closed with "usage_state" and preserves file.
- Corrupted DB: fails closed with "usage_state" without crashing or clearing data.
- Busy wait timeout: PRAGMA busy_timeout capped at min(50ms, budget).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from typing import Optional

import pytest

from opencohost.config import settings
from opencohost.core.web import search_usage
from opencohost.core.web.search_usage import (
    REASON_CLOCK_ROLLBACK,
    REASON_LOCAL_CAP,
    REASON_USAGE_STATE,
    ReservationResult,
    SearchUsageTracker,
)


def test_constants_and_settings_integration():
    """Verify closed reasons and settings.WEB_SEARCH_USAGE_DB exist."""
    assert REASON_LOCAL_CAP == "local_cap"
    assert REASON_USAGE_STATE == "usage_state"
    assert REASON_CLOCK_ROLLBACK == "clock_rollback"
    assert hasattr(settings, "WEB_SEARCH_USAGE_DB")
    assert settings.WEB_SEARCH_USAGE_DB.endswith("web_search_usage.sqlite3")


def test_reservation_lifecycle_reserve_and_dispatch(tmp_path):
    """reserve() -> mark_dispatched() increments consumed once."""
    db_file = str(tmp_path / "usage.sqlite3")
    tracker = SearchUsageTracker(db_path=db_file, current_month="2026-10")

    assert tracker.get_count("tavily") == 0

    res = tracker.reserve("tavily", cap=900)
    assert res.ok is True
    assert res.reason == ""
    assert bool(res.request_id) is True

    # Consumed remains 0 until mark_dispatched
    assert tracker.get_count("tavily") == 0

    dispatched = tracker.mark_dispatched(res.request_id)
    assert dispatched is True
    assert tracker.get_count("tavily") == 1


def test_reservation_lifecycle_reserve_and_release(tmp_path):
    """reserve() -> release() consumes zero calls."""
    db_file = str(tmp_path / "usage.sqlite3")
    tracker = SearchUsageTracker(db_path=db_file, current_month="2026-10")

    res = tracker.reserve("tavily", cap=900)
    assert res.ok is True
    assert bool(res.request_id) is True

    released = tracker.release(res.request_id)
    assert released is True

    # Consumed count is 0
    assert tracker.get_count("tavily") == 0


def test_reservation_idempotency(tmp_path):
    """Verify dispatch idempotency, duplicate release, and invalid transitions."""
    db_file = str(tmp_path / "usage.sqlite3")
    tracker = SearchUsageTracker(db_path=db_file, current_month="2026-10")

    # 1. Dispatch idempotency: multiple calls do not increment twice
    res1 = tracker.reserve("tavily", cap=900)
    assert tracker.mark_dispatched(res1.request_id) is True
    assert tracker.get_count("tavily") == 1

    # Second mark_dispatched is a no-op / returns True without incrementing
    tracker.mark_dispatched(res1.request_id)
    assert tracker.get_count("tavily") == 1

    # Release after dispatch fails and does not alter consumed count
    assert tracker.release(res1.request_id) is False
    assert tracker.get_count("tavily") == 1

    # 2. Release idempotency and dispatch after release
    res2 = tracker.reserve("tavily", cap=900)
    assert tracker.release(res2.request_id) is True

    # Duplicate release is a no-op
    tracker.release(res2.request_id)

    # Dispatch after release fails and does not increment
    assert tracker.mark_dispatched(res2.request_id) is False
    assert tracker.get_count("tavily") == 1


def test_last_slot_race_between_threads(tmp_path):
    """At 899/900 cap, concurrent callers race for the last slot: exactly one succeeds."""
    db_file = str(tmp_path / "usage.sqlite3")
    tracker = SearchUsageTracker(db_path=db_file, current_month="2026-10")

    # Seed consumed count to 899
    tracker._set_consumed("tavily", "2026-10", 899)
    assert tracker.get_count("tavily") == 899

    barrier = threading.Barrier(2)
    results: list[ReservationResult] = []

    def caller(tracker_instance: SearchUsageTracker):
        barrier.wait()
        res = tracker_instance.reserve("tavily", cap=900)
        results.append(res)

    # Use two separate tracker instances to simulate independent callers
    t1 = threading.Thread(target=caller, args=(SearchUsageTracker(db_path=db_file, current_month="2026-10"),))
    t2 = threading.Thread(target=caller, args=(SearchUsageTracker(db_path=db_file, current_month="2026-10"),))

    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert len(results) == 2
    successes = [r for r in results if r.ok]
    failures = [r for r in results if not r.ok]

    assert len(successes) == 1
    assert len(failures) == 1
    assert failures[0].reason == REASON_LOCAL_CAP

    # Dispatch the one successful reservation
    assert tracker.mark_dispatched(successes[0].request_id) is True
    assert tracker.get_count("tavily") == 900


def test_last_slot_race_between_processes(tmp_path):
    """Two concurrent OS processes at 899/900: exactly one reservation succeeds."""
    db_file = str(tmp_path / "usage.sqlite3")
    tracker = SearchUsageTracker(db_path=db_file, current_month="2026-10")
    tracker._set_consumed("tavily", "2026-10", 899)
    assert tracker.get_count("tavily") == 899

    repo_dir = str(tmp_path)
    # Python script executed by two processes
    script = f"""
import sys
sys.path.insert(0, r"{os.path.abspath('.')}")
from opencohost.core.web.search_usage import SearchUsageTracker
import time

tracker = SearchUsageTracker(db_path=r"{db_file}", current_month="2026-10")
res = tracker.reserve("tavily", cap=900)
if res.ok:
    print("SUCCESS:" + res.request_id)
else:
    print("FAILURE:" + res.reason)
"""
    script_file = tmp_path / "race_proc.py"
    script_file.write_text(script, encoding="utf-8")

    python_exe = sys.executable

    p1 = subprocess.Popen([python_exe, str(script_file)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    p2 = subprocess.Popen([python_exe, str(script_file)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    out1, err1 = p1.communicate(timeout=10)
    out2, err2 = p2.communicate(timeout=10)

    combined = (out1 + "\n" + out2).strip().splitlines()
    success_lines = [line for line in combined if line.startswith("SUCCESS:")]
    failure_lines = [line for line in combined if line.startswith("FAILURE:")]

    assert len(success_lines) == 1, f"Expected 1 success, got output: {combined}\nerr1: {err1}\nerr2: {err2}"
    assert len(failure_lines) == 1, f"Expected 1 failure, got output: {combined}"
    assert failure_lines[0] == f"FAILURE:{REASON_LOCAL_CAP}"


def test_clock_rollback_protection(tmp_path):
    """If current period < high_water_period, reserve fails closed with reason 'clock_rollback'."""
    db_file = str(tmp_path / "usage.sqlite3")

    # In October 2026, set high-water and consume 10 calls
    tracker_oct = SearchUsageTracker(db_path=db_file, current_month="2026-10")
    res = tracker_oct.reserve("tavily", cap=900)
    assert res.ok is True
    tracker_oct.mark_dispatched(res.request_id)
    assert tracker_oct.get_count("tavily") == 1

    # Clock moves backwards to September 2026
    tracker_sep = SearchUsageTracker(db_path=db_file, current_month="2026-09")
    res_sep = tracker_sep.reserve("tavily", cap=900)
    assert res_sep.ok is False
    assert res_sep.reason == REASON_CLOCK_ROLLBACK

    # Clock returns to October 2026: October usage is preserved, not reset
    tracker_oct2 = SearchUsageTracker(db_path=db_file, current_month="2026-10")
    assert tracker_oct2.get_count("tavily") == 1
    res_oct2 = tracker_oct2.reserve("tavily", cap=900)
    assert res_oct2.ok is True


def test_forward_period_transition(tmp_path):
    """When period advances (October -> November), previous month's usage is preserved and November starts at 0."""
    db_file = str(tmp_path / "usage.sqlite3")

    tracker_oct = SearchUsageTracker(db_path=db_file, current_month="2026-10")
    for _ in range(5):
        r = tracker_oct.reserve("tavily", cap=900)
        assert r.ok is True
        tracker_oct.mark_dispatched(r.request_id)
    assert tracker_oct.get_count("tavily") == 5

    # Period advances to November
    tracker_nov = SearchUsageTracker(db_path=db_file, current_month="2026-11")
    assert tracker_nov.get_count("tavily") == 0
    assert tracker_nov.get_high_water_period() == "2026-11"

    # Previous month's usage is still recorded in DB
    assert tracker_nov.get_consumed_for_period("tavily", "2026-10") == 5

    # Rollback from November back to October now fails closed
    tracker_back = SearchUsageTracker(db_path=db_file, current_month="2026-10")
    res_back = tracker_back.reserve("tavily", cap=900)
    assert res_back.ok is False
    assert res_back.reason == REASON_CLOCK_ROLLBACK


def test_legacy_json_migration_valid(tmp_path):
    """Valid legacy JSON is migrated atomically once, flag is set, file is preserved."""
    legacy_json = tmp_path / "web_search_usage.json"
    legacy_json.write_text(json.dumps({"month": "2026-10", "tavily": 42, "brave": 17}), encoding="utf-8")
    db_file = str(tmp_path / "usage.sqlite3")

    tracker = SearchUsageTracker(db_path=db_file, legacy_file_path=str(legacy_json), current_month="2026-10")
    assert tracker.get_count("tavily") == 42
    assert tracker.get_count("brave") == 17
    assert tracker.is_legacy_migrated() is True

    # Legacy file was NOT deleted or overwritten
    assert legacy_json.exists()
    data = json.loads(legacy_json.read_text(encoding="utf-8"))
    assert data == {"month": "2026-10", "tavily": 42, "brave": 17}


def test_legacy_json_migration_fresh_install(tmp_path):
    """Missing legacy JSON on fresh install starts clean without error."""
    missing_json = tmp_path / "nonexistent.json"
    db_file = str(tmp_path / "usage.sqlite3")

    tracker = SearchUsageTracker(db_path=db_file, legacy_file_path=str(missing_json), current_month="2026-10")
    assert tracker.get_count("tavily") == 0
    assert tracker.get_count("brave") == 0
    assert tracker.is_legacy_migrated() is True


@pytest.mark.parametrize(
    "bad_content",
    [
        "{corrupt-json",
        json.dumps({"month": "2026-10", "tavily": "42", "brave": 0}),  # string value
        json.dumps({"month": "2026-10", "tavily": True, "brave": 0}),  # bool value
        json.dumps({"month": "2026-10", "tavily": -5, "brave": 0}),  # negative number
        json.dumps({"tavily": 10, "brave": 5}),  # missing month
        json.dumps({"month": "bad-month", "tavily": 10}),  # invalid month format
        json.dumps([1, 2, 3]),  # not a dict
    ],
)
def test_legacy_json_migration_malformed_fails_closed(tmp_path, bad_content):
    """Malformed/invalid JSON fails closed with reason 'usage_state' and preserves the file."""
    legacy_json = tmp_path / "web_search_usage.json"
    legacy_json.write_text(bad_content, encoding="utf-8")
    db_file = str(tmp_path / "usage.sqlite3")

    tracker = SearchUsageTracker(db_path=db_file, legacy_file_path=str(legacy_json), current_month="2026-10")

    res = tracker.reserve("tavily", cap=900)
    assert res.ok is False
    assert res.reason == REASON_USAGE_STATE

    # Invalid file is preserved exactly as written
    assert legacy_json.exists()
    assert legacy_json.read_text(encoding="utf-8") == bad_content


def test_corrupted_db_fails_closed(tmp_path):
    """sqlite3 corrupt state fails closed with reason 'usage_state' without crashing or clearing data."""
    db_file = tmp_path / "corrupt.sqlite3"
    # Write garbage bytes into sqlite file
    db_file.write_bytes(b"\x00\xff" * 500)

    tracker = SearchUsageTracker(db_path=str(db_file), current_month="2026-10")
    res = tracker.reserve("tavily", cap=900)

    assert res.ok is False
    assert res.reason == REASON_USAGE_STATE
    # Corrupted file was not deleted or cleared
    assert db_file.exists()
    assert len(db_file.read_bytes()) == 1000


def test_busy_wait_timeout_capped(tmp_path):
    """PRAGMA busy_timeout is capped at min(50ms, budget)."""
    # Verify timeout helper logic
    assert SearchUsageTracker._resolve_busy_timeout_ms(budget_ms=100.0) == 50
    assert SearchUsageTracker._resolve_busy_timeout_ms(budget_ms=30.0) == 30
    assert SearchUsageTracker._resolve_busy_timeout_ms(budget_ms=None) == 50
    assert SearchUsageTracker._resolve_busy_timeout_ms(budget_ms=-10.0) == 0


def test_locked_db_waits_and_fails_closed(tmp_path):
    """When DB is locked by another connection, reserve waits up to timeout and fails closed."""
    import sqlite3

    db_file = str(tmp_path / "locked.sqlite3")
    # Initialize DB first
    tracker_init = SearchUsageTracker(db_path=db_file, current_month="2026-10")
    tracker_init.reserve("tavily")

    # Open external connection holding exclusive lock
    conn = sqlite3.connect(db_file, isolation_level=None)
    conn.execute("BEGIN IMMEDIATE;")
    try:
        tracker = SearchUsageTracker(db_path=db_file, current_month="2026-10")
        t0 = time.time()
        res = tracker.reserve("tavily", budget_ms=20.0)
        elapsed_ms = (time.time() - t0) * 1000.0

        assert res.ok is False
        assert res.reason == REASON_USAGE_STATE
        # Waited at least part of the budget, but did not hang indefinitely
        assert elapsed_ms >= 10.0
        assert elapsed_ms < 2000.0
    finally:
        conn.close()


def test_is_capped_behavior(tmp_path):
    """Verify is_capped checks admission and fails closed on corrupted/rollback states."""
    db_file = str(tmp_path / "usage.sqlite3")
    tracker = SearchUsageTracker(db_path=db_file, current_month="2026-10")

    assert tracker.is_capped("tavily", cap=2) is False

    r1 = tracker.reserve("tavily", cap=2)
    assert r1.ok is True
    # One slot reserved: 1/2 -> not capped yet
    assert tracker.is_capped("tavily", cap=2) is False

    r2 = tracker.reserve("tavily", cap=2)
    assert r2.ok is True
    # Two slots reserved: 2/2 -> capped!
    assert tracker.is_capped("tavily", cap=2) is True

    # Releasing one slot frees capacity
    tracker.release(r1.request_id)
    assert tracker.is_capped("tavily", cap=2) is False

    # Dispatching r2 consumes 1: 1/2 -> not capped
    tracker.mark_dispatched(r2.request_id)
    assert tracker.is_capped("tavily", cap=2) is False

    # Clock rollback fails closed in is_capped
    tracker_sep = SearchUsageTracker(db_path=db_file, current_month="2026-09")
    assert tracker_sep.is_capped("tavily", cap=2) is True


def test_record_call_compat(tmp_path):
    """record_call backwards compatibility increments count."""
    db_file = str(tmp_path / "usage.sqlite3")
    tracker = SearchUsageTracker(db_path=db_file, current_month="2026-10")

    assert tracker.get_count("tavily") == 0
    c1 = tracker.record_call("tavily")
    assert c1 == 1
    assert tracker.get_count("tavily") == 1
    c2 = tracker.record_call("tavily")
    assert c2 == 2
    assert tracker.get_count("tavily") == 2


def test_multi_thread_quota_cap_enforcement(tmp_path):
    """With cap=3, 10 concurrent threads attempt reservation: exactly 3 succeed."""
    db_file = str(tmp_path / "usage.sqlite3")
    tracker = SearchUsageTracker(db_path=db_file, current_month="2026-10")

    results: list[ReservationResult] = []
    lock = threading.Lock()
    barrier = threading.Barrier(10)

    def worker():
        barrier.wait()
        res = tracker.reserve("tavily", cap=3)
        with lock:
            results.append(res)

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results) == 10
    successes = [r for r in results if r.ok]
    failures = [r for r in results if not r.ok]

    assert len(successes) == 3
    assert len(failures) == 7
    for f in failures:
        assert f.reason == REASON_LOCAL_CAP


def test_f8_first_time_legacy_future_month_fails_closed(tmp_path):
    legacy = tmp_path / "web_search_usage.json"
    legacy.write_text(json.dumps({"month": "2027-03", "tavily": 5}), encoding="utf-8")
    db_file = str(tmp_path / "usage.sqlite3")
    tracker = SearchUsageTracker(db_path=db_file, legacy_file_path=str(legacy), current_month="2026-10")
    res = tracker.reserve("tavily", cap=900)
    assert res.ok is False
    assert res.reason == REASON_CLOCK_ROLLBACK
    # Nothing was persisted: the high-water mark was not poisoned.
    ok = SearchUsageTracker(db_path=db_file, legacy_file_path=str(legacy), current_month="2027-03")
    assert ok.reserve("tavily", cap=900).ok is True
    assert ok.get_consumed_for_period("tavily", "2027-03") == 5
