"""Unit tests for opencohost.api.dispatch.Dispatcher.

Exercises the bounded-gate + capped idempotency-TTL cache contract against
a REAL queue.Queue() — no FastAPI, no engine. HTTP-code mapping (200/404/
409/429) lives in the PR2 endpoint handler; here we only assert dispatcher
result STATES (accepted / queue_full / conflict / replay).
"""

import builtins
import re
import time
from queue import Queue

import pytest

from opencohost.api.dispatch import Dispatcher

COMMAND_ID_RE = re.compile(r"^cmd_[0-9a-f]{32}$")


def _payload(**overrides):
    base = {"name": "streamer_mode"}
    base.update(overrides)
    return base


def test_accept_enqueues_and_bumps_version():
    q = Queue()
    d = Dispatcher(q)
    result = d.dispatch("set_profile", _payload(), key=None)
    assert result.state == "accepted"
    assert COMMAND_ID_RE.match(result.command_id)
    assert q.qsize() == 1
    assert q.get_nowait() == ("set_profile", _payload())
    assert d.state_version == 1


def test_replay_same_key_same_payload_no_reenqueue_no_bump():
    q = Queue()
    d = Dispatcher(q)
    first = d.dispatch("set_profile", _payload(), key="k1")
    assert first.state == "accepted"
    assert d.state_version == 1
    assert q.qsize() == 1

    second = d.dispatch("set_profile", _payload(), key="k1")
    assert second.state == "replay"
    assert second.command_id == first.command_id
    assert q.qsize() == 1  # no second enqueue
    assert d.state_version == 1  # no bump


def test_conflict_same_key_different_payload():
    q = Queue()
    d = Dispatcher(q)
    d.dispatch("set_profile", _payload(), key="k1")
    assert d.state_version == 1

    result = d.dispatch("set_profile", _payload(name="other_profile"), key="k1")
    assert result.state == "conflict"
    assert result.command_id is None
    assert q.qsize() == 1  # nothing new enqueued
    assert d.state_version == 1  # no bump


def test_queue_full_gate_rejects_without_enqueue_or_bump():
    q = Queue()
    for _ in range(16):
        q.put_nowait(("noop", {}))
    d = Dispatcher(q)

    result = d.dispatch("set_profile", _payload(), key=None)
    assert result.state == "queue_full"
    assert result.command_id is None
    assert q.qsize() == 16  # unchanged
    assert d.state_version == 0  # no bump


def test_no_key_means_no_dedupe():
    q = Queue()
    d = Dispatcher(q)
    first = d.dispatch("set_profile", _payload(), key=None)
    second = d.dispatch("set_profile", _payload(), key=None)
    assert first.state == "accepted"
    assert second.state == "accepted"
    assert first.command_id != second.command_id
    assert q.qsize() == 2
    assert d.state_version == 2


def test_ttl_expiry_produces_fresh_command_and_prunes():
    q = Queue()
    d = Dispatcher(q, ttl_seconds=0.05)
    first = d.dispatch("set_profile", _payload(), key="k1")
    assert first.state == "accepted"
    time.sleep(0.1)

    second = d.dispatch("set_profile", _payload(), key="k1")
    assert second.state == "accepted"
    assert second.command_id != first.command_id
    assert q.qsize() == 2  # re-enqueued
    assert d.state_version == 2
    # lazy-prune replaced the expired entry — cache holds only the fresh one
    assert len(d._cache) == 1


def test_cache_cap_evicts_oldest_fifo_and_evicted_key_replays_fresh():
    q = Queue()
    d = Dispatcher(q)
    cap = 1024
    first_command_id = None
    first_key = "key-0"
    for i in range(cap + 1):
        key = f"key-{i}"
        result = d.dispatch("set_profile", _payload(name=f"profile-{i}"), key=key)
        assert result.state == "accepted"
        if i == 0:
            first_command_id = result.command_id
        q.get_nowait()  # drain so the qsize gate never trips during this loop

    assert len(d._cache) == cap
    assert first_key not in d._cache  # oldest FIFO-evicted

    # Evicted key replays as a FRESH command (not found in cache -> new accept).
    replay_result = d.dispatch("set_profile", _payload(name="profile-0"), key=first_key)
    assert replay_result.state == "accepted"
    assert replay_result.command_id != first_command_id


def test_dispatch_does_no_io():
    q = Queue()
    d = Dispatcher(q)

    def _raise_on_open(*args, **kwargs):
        raise AssertionError("dispatch() must not perform file IO")

    original_open = builtins.open
    builtins.open = _raise_on_open
    try:
        result = d.dispatch("set_profile", _payload(), key="k1")
    finally:
        builtins.open = original_open

    assert result.state == "accepted"
