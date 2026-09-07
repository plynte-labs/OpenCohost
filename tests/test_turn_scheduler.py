"""Fast, dependency-free contracts for the extracted ``TurnScheduler``."""

from __future__ import annotations

import itertools
import sys
import threading

from opencohost.core import turn_priority
from opencohost.core.turn_scheduler import TurnScheduler


def _payloads(items) -> list[str]:
    return [item[2] for item in items]


def _item(
    priority: int,
    timestamp: float,
    payload: str,
    source: str,
    history_text: str | None = None,
):
    return (priority, timestamp, payload, source, history_text, None, None)


def test_enqueue_resolves_live_priorities_and_snapshots_each_item(monkeypatch):
    ticks = itertools.count(1.0)
    scheduler = TurnScheduler(max_items=10, clock=lambda: next(ticks))
    monkeypatch.setattr(turn_priority, "STREAM_OVER_AGENDA", True)

    scheduler.enqueue("ptt", source="ptt")
    scheduler.enqueue("direct", source="direct")
    scheduler.enqueue("stream-before", source="chat")
    scheduler.enqueue("agenda-before", source="kira-agenda")

    monkeypatch.setattr(turn_priority, "STREAM_OVER_AGENDA", False)
    scheduler.enqueue("stream-after", source="chat")
    scheduler.enqueue("agenda-after", source="kira-agenda")

    priorities = {item[2]: item[0] for item in scheduler.snapshot()}
    assert priorities == {
        "ptt": 0,
        "direct": 1,
        "stream-before": 2,
        "agenda-before": 3,
        "stream-after": 3,
        "agenda-after": 2,
    }
    assert scheduler.peek()[2] == "ptt"
    assert scheduler.head_snapshot() == (0, "ptt", "ptt", None)


def test_capacity_drops_lowest_non_owner_and_yields_for_owner_only_queue(monkeypatch):
    monkeypatch.setattr(turn_priority, "STREAM_OVER_AGENDA", True)
    scheduler = TurnScheduler(max_items=2, clock=lambda: 1.0)
    dropped = []

    for payload, source in (
        ("agenda", "kira-agenda"),
        ("stream", "chat"),
        ("direct", "direct"),
        ("ptt", "ptt"),
    ):
        dropped.extend(scheduler.enqueue(payload, source=source).dropped)

    assert _payloads(scheduler.snapshot()) == ["ptt", "direct"]
    assert _payloads(dropped) == ["agenda", "stream"]

    owner_only = TurnScheduler(max_items=1, clock=lambda: 1.0)
    assert owner_only.enqueue("direct", source="direct").dropped == ()
    assert owner_only.enqueue("ptt", source="ptt").dropped == ()
    assert _payloads(owner_only.snapshot()) == ["ptt", "direct"]


def test_ttl_expiry_distinguishes_direct_stream_ptt_and_agenda():
    ticks = iter((994.0, 992.0, 1.0, 1.0))
    scheduler = TurnScheduler(
        max_items=10,
        direct_ttl_seconds=5.0,
        stream_ttl_resolver=lambda: 7.0,
        clock=lambda: next(ticks),
    )
    scheduler.enqueue("stale-direct", priority=1, source="direct")
    scheduler.enqueue("stale-stream", priority=2, source="chat")
    scheduler.enqueue("old-agenda", priority=3, source="kira-agenda-session")
    scheduler.enqueue("old-ptt", priority=0, source="ptt")

    expired = scheduler.expire(now=1000.0)

    assert _payloads(entry.item for entry in expired) == [
        "stale-direct",
        "stale-stream",
    ]
    assert [entry.ttl_seconds for entry in expired] == [5.0, 7.0]
    assert _payloads(scheduler.snapshot()) == ["old-ptt", "old-agenda"]


def test_agenda_first_stream_floor_is_exact_and_eventually_expires(monkeypatch):
    monkeypatch.setattr(turn_priority, "STREAM_OVER_AGENDA", False)
    monkeypatch.setattr(turn_priority, "STREAM_TTL_SECONDS", 0.01)
    scheduler = TurnScheduler(clock=lambda: 0.0)
    scheduler.enqueue("waiting-stream", source="chat")

    assert scheduler.expire(now=299.0) == ()
    expired = scheduler.expire(now=301.0)

    assert len(expired) == 1
    assert expired[0].item[2] == "waiting-stream"
    assert expired[0].ttl_seconds == 300.0


def test_dequeue_preserves_fifo_and_clear_returns_removed_items():
    ticks = itertools.count(1.0)
    scheduler = TurnScheduler(max_items=10, clock=lambda: next(ticks))
    scheduler.enqueue("first", priority=1, source="direct")
    scheduler.enqueue("second", priority=1, source="direct")
    scheduler.enqueue("third", priority=2, source="chat")

    assert scheduler.dequeue()[2] == "first"
    assert scheduler.dequeue()[2] == "second"
    assert _payloads(scheduler.clear()) == ["third"]
    assert scheduler.dequeue() is None


def test_source_operations_partition_exact_replace_from_prefix_clear():
    ticks = itertools.count(1.0)
    scheduler = TurnScheduler(max_items=10, clock=lambda: next(ticks))
    scheduler.enqueue("agenda-1", priority=2, source="kira-agenda")
    scheduler.enqueue("agenda-2", priority=2, source="kira-agenda-next")
    scheduler.enqueue("chat", priority=3, source="chat")

    exact = scheduler.drop_source("kira-agenda")
    assert _payloads(exact) == ["agenda-1"]
    prefixed = scheduler.drop_sources(("kira-agenda",))
    assert _payloads(prefixed) == ["agenda-2"]
    assert _payloads(scheduler.snapshot()) == ["chat"]


def test_owner_bundle_takes_only_a_bounded_contiguous_prefix():
    ticks = itertools.count(1.0)
    scheduler = TurnScheduler(max_items=10, clock=lambda: next(ticks))
    scheduler.enqueue("Q1", priority=1, source="direct")
    scheduler.enqueue("Q2", priority=1, source="ptt")
    scheduler.enqueue("viewer", priority=1, source="chat")
    scheduler.enqueue("Q3", priority=1, source="direct")
    head = scheduler.dequeue()

    taken = scheduler.take_owner_prefix(head)

    assert _payloads(taken) == ["Q2"]
    assert _payloads(scheduler.snapshot()) == ["viewer", "Q3"]


def test_owner_bundle_character_limit_defers_whole_questions():
    ticks = itertools.count(1.0)
    scheduler = TurnScheduler(max_items=10, clock=lambda: next(ticks))
    scheduler.enqueue("a" * 900, priority=1, source="direct")
    scheduler.enqueue("b" * 900, priority=1, source="direct")
    scheduler.enqueue("c" * 900, priority=1, source="direct")
    head = scheduler.dequeue()

    taken = scheduler.take_owner_prefix(head)

    assert _payloads(taken) == ["b" * 900]
    assert _payloads(scheduler.snapshot()) == ["c" * 900]


def test_failed_bundle_requeues_original_tuples_in_scheduler_order():
    scheduler = TurnScheduler(max_items=10)
    direct = _item(1, 10.0, "direct", "direct", "direct-history")
    ptt = _item(0, 30.0, "ptt", "ptt", "ptt-history")
    latecomer = _item(1, 20.0, "late", "direct")
    scheduler.requeue((latecomer,))

    scheduler.requeue((direct, ptt))

    queued = scheduler.snapshot()
    assert queued == (ptt, direct, latecomer)
    assert queued[0] is ptt and queued[1] is direct and queued[2] is latecomer


def test_empty_callback_runs_under_scheduler_lock_but_blocked_pop_skips_it():
    scheduler = TurnScheduler()
    callback_lock_states = []

    def flush():
        acquired = scheduler.lock.acquire(blocking=False)
        callback_lock_states.append(acquired)
        if acquired:
            scheduler.lock.release()
        return "accumulated"

    result = scheduler.dequeue_or_else(on_empty=flush)

    assert result.empty and not result.blocked and result.empty_value == "accumulated"
    assert callback_lock_states == [False]

    blocked = scheduler.dequeue_or_else(on_empty=flush, required_priority=0)
    assert blocked.blocked and not blocked.empty and blocked.item is None
    assert callback_lock_states == [False]


def test_concurrent_enqueue_and_dequeue_preserve_exactly_once_invariant():
    scheduler = TurnScheduler(max_items=10_000)
    producer_count = 4
    items_per_producer = 100
    producer_barrier = threading.Barrier(producer_count)
    failures = []
    previous_interval = sys.getswitchinterval()
    sys.setswitchinterval(1e-5)

    def produce(worker: int) -> None:
        try:
            producer_barrier.wait(timeout=15.0)
            for index in range(items_per_producer):
                scheduler.enqueue(f"{worker}:{index}", priority=1, source="direct")
        except BaseException as exc:  # surfaced on the parent thread below
            failures.append(exc)

    producers = [
        threading.Thread(target=produce, args=(worker,), daemon=True)
        for worker in range(producer_count)
    ]
    try:
        for thread in producers:
            thread.start()
        for thread in producers:
            thread.join(10.0)
    finally:
        sys.setswitchinterval(previous_interval)

    assert not failures
    assert all(not thread.is_alive() for thread in producers)
    assert len(scheduler.snapshot()) == producer_count * items_per_producer

    seen = []
    while (item := scheduler.dequeue()) is not None:
        seen.append(item[2])

    assert len(seen) == producer_count * items_per_producer
    assert len(set(seen)) == len(seen)
    assert scheduler.snapshot() == ()
