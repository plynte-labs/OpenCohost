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

from opencohost.api.dispatch import Dispatcher, _DEFAULT_TTL_SECONDS

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


def test_conflict_same_key_same_payload_different_command():
    # Cross-verb false-replay guard: once a second verb exists, the same
    # Idempotency-Key + a coincidentally-identical payload under a DIFFERENT
    # command must NOT replay the original command_id — it must conflict.
    q = Queue()
    d = Dispatcher(q)
    first = d.dispatch("set_profile", _payload(), key="k1")
    assert first.state == "accepted"

    result = d.dispatch("some_other_command", _payload(), key="k1")
    assert result.state == "conflict"
    assert result.command_id is None
    assert q.qsize() == 1  # no false-replay enqueue
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


def test_default_ttl_seconds_pinned():
    # Pins the intended default so a regression (e.g. accidental unit change)
    # fails the suite instead of silently shipping. WU4 raised this 120 -> 600
    # so a legitimate retry of a stalled >120s chat turn replays instead of
    # double-firing Kira (OLLAMA_CHAT_TIMEOUT=180s + watchdog + operator retry).
    assert _DEFAULT_TTL_SECONDS == 600.0


def test_replay_survives_beyond_old_120s_ttl(monkeypatch):
    # A stalled heavy chat turn can take >120s; the operator's retry must replay
    # the cached command_id, not enqueue a second one. Under the old 120s TTL the
    # entry would have expired and this second dispatch would be a fresh accept.
    q = Queue()
    clock = {"t": 1000.0}
    monkeypatch.setattr("opencohost.api.dispatch.time.time", lambda: clock["t"])
    d = Dispatcher(q)  # default TTL (600s)
    first = d.dispatch("set_profile", _payload(), key="k1")
    assert first.state == "accepted"

    clock["t"] = 1000.0 + 200.0  # past the old 120s window, inside the new 600s one
    second = d.dispatch("set_profile", _payload(), key="k1")
    assert second.state == "replay"
    assert second.command_id == first.command_id
    assert q.qsize() == 1  # no double-fire
    assert d.state_version == 1


def test_dispatch_optional_history_text_produces_3tuple_hash_ignores_it():
    # A1 (memoria_quality_20260717): dispatch() gains an OPTIONAL history_text.
    # Given (F10 PTT path) -> the queue item is a 3-tuple (command, payload,
    # history_text); omitted -> it stays a 2-tuple so every existing caller is
    # unchanged. history_text NEVER enters the idempotency hash/cache key — a
    # replay is decided on (command, payload) alone.
    q = Queue()
    d = Dispatcher(q)

    # 3-tuple when history_text is given.
    r1 = d.dispatch(
        "process_context",
        "El streamer acaba de decir (PTT): hola",
        history_text="El streamer dijo (PTT): hola",
    )
    assert r1.state == "accepted"
    assert q.get_nowait() == (
        "process_context",
        "El streamer acaba de decir (PTT): hola",
        "El streamer dijo (PTT): hola",
    )

    # 2-tuple when history_text is omitted (every other caller).
    r2 = d.dispatch("set_profile", _payload())
    assert r2.state == "accepted"
    assert q.get_nowait() == ("set_profile", _payload())

    # Idempotency hash ignores history_text: same key+command+payload but a
    # DIFFERENT history_text still replays (no conflict, no re-enqueue).
    first = d.dispatch("process_context", "hola", key="k1", history_text="hist A")
    assert first.state == "accepted"
    assert q.get_nowait() == ("process_context", "hola", "hist A")
    replay = d.dispatch("process_context", "hola", key="k1", history_text="hist B DIFFERENT")
    assert replay.state == "replay"
    assert replay.command_id == first.command_id
    assert q.qsize() == 0  # no second enqueue — history_text is not part of identity


def test_dispatch_optional_source_rides_as_4th_element_and_is_not_identity():
    # B2 (turn provenance): dispatch() gains an OPTIONAL source that rides as a
    # 4th tuple element, exactly the way history_text rides as the 3rd — the
    # engine hardcoded source="direct" for every dispatched turn, so a PTT turn
    # logged/telemetered as "direct". Omitted -> the tuple shape is unchanged for
    # every existing caller. Like history_text it is NEVER part of the
    # idempotency hash/cache key (identity is (command, payload) only).
    q = Queue()
    d = Dispatcher(q)

    r1 = d.dispatch(
        "process_context",
        "El streamer acaba de decir (PTT): hola",
        history_text="El streamer dijo (PTT): hola",
        source="ptt",
    )
    assert r1.state == "accepted"
    assert q.get_nowait() == (
        "process_context",
        "El streamer acaba de decir (PTT): hola",
        "El streamer dijo (PTT): hola",
        "ptt",
    )

    # source without history_text still lands in slot 4 (history_text None) —
    # the engine's tolerant unpack reads position, not presence.
    r2 = d.dispatch("process_context", "hola", source="ptt")
    assert r2.state == "accepted"
    assert q.get_nowait() == ("process_context", "hola", None, "ptt")

    # Omitted -> 2-tuple, byte-identical to every pre-existing caller.
    r3 = d.dispatch("set_profile", _payload())
    assert r3.state == "accepted"
    assert q.get_nowait() == ("set_profile", _payload())

    # Identity ignores source: same key+command+payload with a DIFFERENT source
    # still replays (no conflict, no re-enqueue).
    first = d.dispatch("process_context", "hola", key="k1", source="ptt")
    assert first.state == "accepted"
    assert q.get_nowait() == ("process_context", "hola", None, "ptt")
    replay = d.dispatch("process_context", "hola", key="k1", source="direct")
    assert replay.state == "replay"
    assert replay.command_id == first.command_id
    assert q.qsize() == 0


def test_hash_payload_is_key_order_independent():
    # Pins sort_keys=True in _hash_payload — must fail if removed, since a
    # dict re-serialized in a different key order would otherwise hash
    # differently for what is semantically the same payload.
    assert Dispatcher._hash_payload({"a": 1, "b": 2}) == Dispatcher._hash_payload({"b": 2, "a": 1})


def test_rejected_dispatch_with_key_does_not_cache_queue_full():
    q = Queue()
    for _ in range(16):
        q.put_nowait(("noop", {}))
    d = Dispatcher(q)

    result = d.dispatch("set_profile", _payload(), key="k1")
    assert result.state == "queue_full"
    assert result.command_id is None
    assert "k1" not in d._cache

    # Drain the queue and retry with the same key: must be a fresh accept,
    # not a phantom replay of a command_id that was never actually enqueued.
    for _ in range(16):
        q.get_nowait()
    retry = d.dispatch("set_profile", _payload(), key="k1")
    assert retry.state == "accepted"
    assert q.qsize() == 1


def test_rejected_dispatch_with_key_does_not_cache_conflict():
    q = Queue()
    d = Dispatcher(q)
    first = d.dispatch("set_profile", _payload(), key="k1")
    assert first.state == "accepted"

    conflict = d.dispatch("set_profile", _payload(name="other_profile"), key="k1")
    assert conflict.state == "conflict"
    assert conflict.command_id is None

    # The rejected (conflicting) request must not have overwritten the cache
    # entry — the original command_id must still replay for the original payload.
    replay = d.dispatch("set_profile", _payload(), key="k1")
    assert replay.state == "replay"
    assert replay.command_id == first.command_id
