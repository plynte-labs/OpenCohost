"""Tests for dedicated search API worker process and IPC handshake (Phase 3, A5/A6).

Covers:
- Handshake sequence: PREPARE -> READY -> SEND -> RESULT.
- PREPARE failure (DNS failure, SSRF blocked host, unknown provider, empty key):
  returns error without HTTP, allowing reservation release with zero consumed.
- Bounded frames: request > 8 KiB or response > 64 KiB fail closed.
- Concurrency: single active slot; concurrent requests return api_reason="busy".
- Absolute deadline enforcement: slow worker / hanging I/O is terminated, reaped,
  generation incremented, old generation frames rejected.
- Crash resilience: unexpected EOF / child exit at PREPARE vs after DISPATCHED.
- Clean shutdown: runtime.close() terminates child process without leaking subprocesses.
- Global singleton management: get_runtime() / close_runtime().
"""
from __future__ import annotations

import io
import json
import os
import sys
import threading
import time
from typing import Optional

import pytest

from opencohost.core.web.search import SearchResult
from opencohost.core.web.search_api import (
    REASON_AUTH,
    REASON_BLOCKED,
    REASON_DNS,
    REASON_PARSE,
    REASON_TIMEOUT,
)
from opencohost.core.web.search_api_runtime import (
    MAX_REQUEST_FRAME_BYTES,
    MAX_RESPONSE_FRAME_BYTES,
    REASON_BUSY,
    REASON_WORKER_UNAVAILABLE,
    FrameOverflowError,
    SearchApiRuntime,
    close_runtime,
    get_runtime,
)
from opencohost.core.web.search_api_worker import (
    handle_prepare,
    handle_send,
    read_frame,
    write_frame,
)

SAMPLE_TAVILY_RESPONSE = {
    "results": [
        {
            "title": "Tavily Result 1",
            "url": "https://example.com/t1",
            "content": "Snippet from Tavily",
        }
    ]
}

SAMPLE_BRAVE_RESPONSE = {
    "web": {
        "results": [
            {
                "title": "Brave Result 1",
                "url": "https://example.com/b1",
                "description": "Snippet from Brave",
            }
        ]
    }
}


@pytest.fixture(autouse=True)
def cleanup_runtime_fixture():
    """Ensure global runtime singleton is closed after every test."""
    yield
    close_runtime()


def _make_test_env(**overrides: str) -> dict[str, str]:
    """Create isolated environment for test worker child processes."""
    env = dict(os.environ)
    # Default: mock DNS to public IP and mock response to Tavily sample
    env["OPENCOHOST_WORKER_MOCK_DNS"] = "93.184.216.34"
    env["OPENCOHOST_WORKER_MOCK_HTTP"] = json.dumps(SAMPLE_TAVILY_RESPONSE)
    for k, v in overrides.items():
        if v is None:
            env.pop(k, None)
        else:
            env[k] = v
    return env


# ==============================================================================
# 1. Handshake sequence: PREPARE -> READY -> SEND -> RESULT
# ==============================================================================


def test_handshake_sequence_tavily_success():
    """Full handshake sequence succeeds for Tavily."""
    env = _make_test_env(OPENCOHOST_WORKER_MOCK_HTTP=json.dumps(SAMPLE_TAVILY_RESPONSE))
    rt = SearchApiRuntime(env=env)
    try:
        deadline = time.monotonic() + 5.0
        ok, reason = rt.prepare("tavily", "apple earnings", "valid_key", deadline=deadline)
        assert ok is True
        assert reason == ""

        results, reason = rt.dispatch("tavily", "apple earnings", "valid_key", deadline=deadline)
        assert reason == ""
        assert len(results) == 1
        assert isinstance(results[0], SearchResult)
        assert results[0].title == "Tavily Result 1"
        assert results[0].url == "https://example.com/t1"
        assert results[0].snippet == "Snippet from Tavily"
    finally:
        rt.close()


def test_handshake_sequence_brave_success():
    """Full handshake sequence succeeds for Brave."""
    env = _make_test_env(OPENCOHOST_WORKER_MOCK_HTTP=json.dumps(SAMPLE_BRAVE_RESPONSE))
    rt = SearchApiRuntime(env=env)
    try:
        deadline = time.monotonic() + 5.0
        ok, reason = rt.prepare("brave", "python asyncio", "valid_key", deadline=deadline)
        assert ok is True
        assert reason == ""

        results, reason = rt.dispatch("brave", "python asyncio", "valid_key", deadline=deadline)
        assert reason == ""
        assert len(results) == 1
        assert isinstance(results[0], SearchResult)
        assert results[0].title == "Brave Result 1"
        assert results[0].url == "https://example.com/b1"
        assert results[0].snippet == "Snippet from Brave"
    finally:
        rt.close()


# ==============================================================================
# 2. PREPARE failure: returns error without HTTP, zero consumed
# ==============================================================================


def test_prepare_failure_dns_zero_http():
    """DNS failure at PREPARE returns reason='dns' without making HTTP requests."""
    env = _make_test_env(
        OPENCOHOST_WORKER_MOCK_DNS="gaierror",
        OPENCOHOST_WORKER_FAIL_ON_HTTP="1",  # Child process crashes if HTTP attempted
    )
    rt = SearchApiRuntime(env=env)
    try:
        ok, reason = rt.prepare("tavily", "query", "key", deadline=time.monotonic() + 5.0)
        assert ok is False
        assert reason == REASON_DNS
    finally:
        rt.close()


def test_prepare_failure_ssrf_blocked_zero_http():
    """SSRF private IP at PREPARE returns reason='blocked' without HTTP."""
    env = _make_test_env(
        OPENCOHOST_WORKER_MOCK_DNS="127.0.0.1",
        OPENCOHOST_WORKER_FAIL_ON_HTTP="1",
    )
    rt = SearchApiRuntime(env=env)
    try:
        ok, reason = rt.prepare("tavily", "query", "key", deadline=time.monotonic() + 5.0)
        assert ok is False
        assert reason == REASON_BLOCKED
    finally:
        rt.close()


def test_prepare_failure_unknown_provider_zero_http():
    """Unknown provider at PREPARE fails closed with reason='parse' without HTTP."""
    env = _make_test_env(OPENCOHOST_WORKER_FAIL_ON_HTTP="1")
    rt = SearchApiRuntime(env=env)
    try:
        ok, reason = rt.prepare("unknown_engine", "query", "key", deadline=time.monotonic() + 5.0)
        assert ok is False
        assert reason == REASON_PARSE
    finally:
        rt.close()


def test_prepare_failure_empty_key_zero_http():
    """Empty API key at PREPARE returns reason='auth' without HTTP."""
    env = _make_test_env(OPENCOHOST_WORKER_FAIL_ON_HTTP="1")
    rt = SearchApiRuntime(env=env)
    try:
        ok, reason = rt.prepare("tavily", "query", "", deadline=time.monotonic() + 5.0)
        assert ok is False
        assert reason == REASON_AUTH
    finally:
        rt.close()


# ==============================================================================
# 3. Bounded frames: > 8 KiB request or > 64 KiB response fail closed
# ==============================================================================


def test_bounded_frames_request_too_large():
    """Request frame exceeding 8 KiB fails closed before transmission."""
    env = _make_test_env()
    rt = SearchApiRuntime(env=env)
    try:
        oversized_query = "x" * 9000  # Frame will exceed 8192 bytes
        ok, reason = rt.prepare("tavily", oversized_query, "key", deadline=time.monotonic() + 5.0)
        assert ok is False
        assert reason == REASON_PARSE
    finally:
        rt.close()


def test_bounded_frames_response_too_large():
    """Response frame exceeding 64 KiB fails closed, terminates child and reaps."""
    env = _make_test_env(OPENCOHOST_WORKER_OVERFLOW_RESPONSE="1")
    rt = SearchApiRuntime(env=env)
    try:
        initial_gen = rt.generation
        ok, reason = rt.prepare("tavily", "query", "key", deadline=time.monotonic() + 5.0)
        assert ok is False
        assert reason == REASON_PARSE
        # Process generation should have incremented due to termination of violating child
        assert rt.generation > initial_gen
    finally:
        rt.close()


# ==============================================================================
# 4. Concurrency: single active slot; concurrent requests return "busy"
# ==============================================================================


def test_concurrency_single_active_slot_busy():
    """While a slot is active, concurrent requests return api_reason='busy'."""
    env = _make_test_env()
    rt = SearchApiRuntime(env=env)
    try:
        deadline = time.monotonic() + 5.0
        # Request A prepares and holds the slot
        ok, reason = rt.prepare("tavily", "query a", "key", deadline=deadline, request_id="req-a")
        assert ok is True
        assert reason == ""

        # Request B attempts prepare -> busy!
        ok_b, reason_b = rt.prepare("tavily", "query b", "key", deadline=deadline, request_id="req-b")
        assert ok_b is False
        assert reason_b == REASON_BUSY

        # Request B attempts dispatch -> busy!
        res_b, reason_b = rt.dispatch("tavily", "query b", "key", deadline=deadline, request_id="req-b")
        assert res_b == []
        assert reason_b == REASON_BUSY

        # Request A completes dispatch -> releases slot
        res_a, reason_a = rt.dispatch("tavily", "query a", "key", deadline=deadline, request_id="req-a")
        assert reason_a == ""

        # Now Request C can prepare successfully
        ok_c, reason_c = rt.prepare("tavily", "query c", "key", deadline=deadline, request_id="req-c")
        assert ok_c is True
        assert reason_c == ""
        rt.release_slot("req-c")
    finally:
        rt.close()


def test_concurrency_threaded_race():
    """Multiple concurrent threads racing to prepare get exactly one winner; others busy."""
    env = _make_test_env()
    rt = SearchApiRuntime(env=env)
    results = []

    def worker(tid: int):
        ok, reason = rt.prepare("tavily", f"query {tid}", "key", deadline=time.monotonic() + 5.0, request_id=f"req-{tid}")
        results.append((ok, reason))

    try:
        threads = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        successes = [r for r in results if r[0] is True]
        busies = [r for r in results if r[1] == REASON_BUSY]
        assert len(successes) == 1
        assert len(busies) == 4
    finally:
        rt.close()


# ==============================================================================
# 5. Absolute deadline enforcement: slow worker terminated, reaped, generation incremented
# ==============================================================================


def test_deadline_enforcement_slow_prepare_terminated_and_reaped():
    """Hanging worker during PREPARE is terminated past deadline, reaped, and generation increments."""
    env = _make_test_env(OPENCOHOST_WORKER_HANG_PREPARE="5.0")
    rt = SearchApiRuntime(env=env)
    try:
        gen_before = rt.generation
        start = time.monotonic()
        # Set short deadline: 150 ms
        deadline = start + 0.15
        ok, reason = rt.prepare("tavily", "query", "key", deadline=deadline)
        elapsed = time.monotonic() - start

        assert ok is False
        assert reason == REASON_TIMEOUT
        # Tolerance: should return cleanly near deadline, well under 1.5s
        assert elapsed < 1.5
        # Child process generation must be incremented
        assert rt.generation > gen_before
    finally:
        rt.close()


def test_deadline_enforcement_slow_send_terminated_and_reaped():
    """Hanging worker during SEND is terminated past deadline, reaped, and generation increments."""
    env = _make_test_env(OPENCOHOST_WORKER_HANG_SEND="5.0")
    rt = SearchApiRuntime(env=env)
    try:
        # Prepare passes normally
        ok, reason = rt.prepare("tavily", "query", "key", deadline=time.monotonic() + 5.0)
        assert ok is True

        gen_before = rt.generation
        start = time.monotonic()
        deadline = start + 0.15
        results, reason = rt.dispatch("tavily", "query", "key", deadline=deadline)
        elapsed = time.monotonic() - start

        assert results == []
        assert reason == REASON_TIMEOUT
        assert elapsed < 1.5
        assert rt.generation > gen_before
    finally:
        rt.close()


def test_old_generation_frames_rejected():
    """Worker frames stamped with a prior generation ID are rejected."""
    env = _make_test_env(OPENCOHOST_WORKER_SEND_OLD_GENERATION="1")
    rt = SearchApiRuntime(env=env)
    try:
        ok, reason = rt.prepare("tavily", "query", "key", deadline=time.monotonic() + 5.0)
        assert ok is False
        assert reason == REASON_PARSE
    finally:
        rt.close()


# ==============================================================================
# 6. Crash resilience: worker crash / unexpected EOF at PREPARE vs after DISPATCHED
# ==============================================================================


def test_crash_resilience_worker_crash_at_prepare():
    """Worker crash at PREPARE is detected, cleaned up, and returns worker_unavailable."""
    env = _make_test_env(OPENCOHOST_WORKER_CRASH_PREPARE="1")
    rt = SearchApiRuntime(env=env)
    try:
        gen_before = rt.generation
        ok, reason = rt.prepare("tavily", "query", "key", deadline=time.monotonic() + 5.0)
        assert ok is False
        assert reason == REASON_WORKER_UNAVAILABLE
        assert rt.generation > gen_before
    finally:
        rt.close()


def test_crash_resilience_worker_crash_after_dispatched():
    """Worker crash after DISPATCHED (during SEND) is detected, cleaned up, and returns worker_unavailable."""
    env = _make_test_env(OPENCOHOST_WORKER_CRASH_SEND="1")
    rt = SearchApiRuntime(env=env)
    try:
        ok, reason = rt.prepare("tavily", "query", "key", deadline=time.monotonic() + 5.0)
        assert ok is True

        gen_before = rt.generation
        results, reason = rt.dispatch("tavily", "query", "key", deadline=time.monotonic() + 5.0)
        assert results == []
        assert reason == REASON_WORKER_UNAVAILABLE
        assert rt.generation > gen_before
    finally:
        rt.close()


# ==============================================================================
# 7. Clean shutdown and reusable worker
# ==============================================================================


def test_clean_shutdown_terminates_child_process():
    """runtime.close() terminates child process without leaking subprocesses."""
    env = _make_test_env()
    rt = SearchApiRuntime(env=env)
    ok, _ = rt.prepare("tavily", "q", "key", deadline=time.monotonic() + 5.0)
    assert ok is True
    assert rt.is_running is True

    proc = rt._child
    rt.close()
    assert rt.is_running is False
    # Child process has been waited/reaped
    assert proc.poll() is not None


def test_reusable_worker_across_multiple_requests():
    """Same child process and generation handle multiple sequential requests without restarting."""
    env = _make_test_env()
    rt = SearchApiRuntime(env=env)
    try:
        # Request 1
        ok, _ = rt.prepare("tavily", "q1", "key", deadline=time.monotonic() + 5.0)
        assert ok is True
        pid1 = rt.child_pid
        gen1 = rt.generation
        res1, _ = rt.dispatch("tavily", "q1", "key", deadline=time.monotonic() + 5.0)
        assert len(res1) == 1

        # Request 2
        ok, _ = rt.prepare("tavily", "q2", "key", deadline=time.monotonic() + 5.0)
        assert ok is True
        pid2 = rt.child_pid
        gen2 = rt.generation
        res2, _ = rt.dispatch("tavily", "q2", "key", deadline=time.monotonic() + 5.0)
        assert len(res2) == 1

        # PIDs and generations must match (same reusable worker)
        assert pid1 == pid2
        assert gen1 == gen2
    finally:
        rt.close()


def test_release_slot_allows_subsequent_prepare():
    """Explicitly releasing a slot after prepare frees it for subsequent callers."""
    env = _make_test_env()
    rt = SearchApiRuntime(env=env)
    try:
        ok, _ = rt.prepare("tavily", "q", "key", deadline=time.monotonic() + 5.0, request_id="r1")
        assert ok is True
        rt.release_slot("r1")

        # Can now prepare a new request without 'busy'
        ok2, _ = rt.prepare("tavily", "q2", "key", deadline=time.monotonic() + 5.0, request_id="r2")
        assert ok2 is True
        rt.release_slot("r2")
    finally:
        rt.close()


def test_global_runtime_singleton():
    """get_runtime() and close_runtime() manage a shared singleton instance."""
    rt1 = get_runtime()
    rt2 = get_runtime()
    assert rt1 is rt2
    close_runtime()
    assert rt1.is_running is False

    rt3 = get_runtime()
    assert rt3 is not rt1
    close_runtime()


# ==============================================================================
# 8. Worker framing unit tests
# ==============================================================================


def test_worker_framing_bounded_request_overflow():
    """Reading a request frame exceeding 8 KiB raises FrameOverflowError."""
    buf = io.BytesIO(b"x" * (MAX_REQUEST_FRAME_BYTES + 10) + b"\n")
    with pytest.raises(FrameOverflowError):
        read_frame(buf, MAX_REQUEST_FRAME_BYTES)


def test_worker_framing_bounded_response_overflow():
    """Writing a response frame exceeding 64 KiB raises FrameOverflowError."""
    buf = io.BytesIO()
    large_frame = {"cmd": "RESULT", "data": "x" * (MAX_RESPONSE_FRAME_BYTES + 100)}
    with pytest.raises(FrameOverflowError):
        write_frame(buf, large_frame, MAX_RESPONSE_FRAME_BYTES)


def test_worker_framing_roundtrip():
    """Valid frames serialize and deserialize correctly."""
    buf = io.BytesIO()
    frame = {"cmd": "READY", "request_id": "test-123", "generation": 1, "ok": True, "reason": ""}
    write_frame(buf, frame, MAX_REQUEST_FRAME_BYTES)
    buf.seek(0)
    parsed = read_frame(buf, MAX_REQUEST_FRAME_BYTES)
    assert parsed == frame


# ==============================================================================
# F6: no new child until previous exit confirmed; caller wait bounded
# ==============================================================================


class _StuckProc:
    pid = 4242
    stdin = None
    stdout = None

    def __init__(self):
        self.wait_timeouts = []
        self.killed = False

    def poll(self):
        return None

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        import subprocess as sp
        self.wait_timeouts.append(timeout)
        raise sp.TimeoutExpired("cmd", timeout)


def test_f6_unconfirmed_exit_blocks_new_child_launch(monkeypatch):
    import subprocess as sp

    rt = SearchApiRuntime(env=_make_test_env())
    stuck = _StuckProc()
    rt._child = stuck
    rt._terminate_and_reap()
    assert stuck.killed is True

    launched = []
    monkeypatch.setattr(sp, "Popen", lambda *a, **k: launched.append(1))
    ok, reason = rt.prepare("tavily", "q", "key", deadline=time.monotonic() + 5.0)
    assert ok is False
    assert reason == REASON_WORKER_UNAVAILABLE
    assert launched == []


def test_f6_reap_wait_is_bounded_by_deadline():
    rt = SearchApiRuntime(env=_make_test_env())
    stuck = _StuckProc()
    rt._child = stuck
    start = time.monotonic()
    rt._terminate_and_reap(deadline=start + 0.2)
    assert stuck.wait_timeouts and stuck.wait_timeouts[0] <= 0.25


# --- judge round 1 (F8): a retry after a SEND kill must not fail as worker_unavailable ---


class _SlowToDie:
    """A killed child whose exit is only confirmed after a short wait."""

    def __init__(self):
        self.waited = False

    def poll(self):
        return 0 if self.waited else None

    def wait(self, timeout=None):
        self.waited = True
        return 0


def test_ensure_child_waits_for_a_killed_worker_before_refusing(monkeypatch):
    from opencohost.core.web import search_api_runtime as sar

    rt = sar.SearchApiRuntime()
    rt._unreaped = _SlowToDie()
    launched = []
    monkeypatch.setattr(sar.subprocess, "Popen", lambda *a, **k: launched.append(1) or (_ for _ in ()).throw(RuntimeError("stop")))
    with pytest.raises(RuntimeError, match="stop"):  # got past the unreaped check and tried to launch
        rt._ensure_child()
    assert rt._unreaped is None and launched == [1]
