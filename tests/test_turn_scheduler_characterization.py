"""Characterize legacy turn scheduling before extracting ``TurnScheduler``.

These tests intentionally exercise ``MotorVocalIA`` methods through lightweight
harnesses. They freeze queue behavior without initializing Ollama, TTS, audio,
or the engine thread.
"""

from __future__ import annotations

import queue
import threading
import time
from types import SimpleNamespace

import pytest

from opencohost.core import llm_engine, turn_priority
from opencohost.core.llm_engine import MotorVocalIA


def _queue_harness(*, max_items: int = 20) -> SimpleNamespace:
    harness = SimpleNamespace(
        _priority_queue=[],
        _pq_lock=threading.Lock(),
        _pq_max_items=max_items,
        accumulated=[],
        logs=[],
    )
    harness.enqueue_accumulation = (
        lambda payload, source="chat": harness.accumulated.append((payload, source))
    )
    harness._head_snapshot_locked = lambda: None
    harness._maybe_trigger_interactive_pregen = lambda _head: None
    harness._log = lambda message, level="info": harness.logs.append((level, message))
    return harness


def _enqueue(harness: SimpleNamespace, payload: str, *, source: str) -> None:
    MotorVocalIA.enqueue(harness, payload, source=source)


def _item(
    payload: str,
    *,
    source: str = "direct",
    priority: int = 1,
    timestamp: float = 0.0,
    history_text: str | None = None,
    submitted_at: float | None = None,
    submitted_under_provider: str | None = None,
) -> tuple:
    return (
        priority,
        timestamp,
        payload,
        source,
        history_text,
        submitted_at,
        submitted_under_provider,
    )


def _ttl_harness(items: list[tuple]) -> SimpleNamespace:
    harness = SimpleNamespace(
        _processing=False,
        _speech_interrupt_enabled=True,
        _speech_router_enabled=True,
        _speech_active=True,
        _pq_lock=threading.Lock(),
        _priority_queue=list(items),
        _pq_ttl_seconds=30.0,
        on_chat_item_expired=None,
        cleared_prefetch=[],
        logs=[],
    )
    harness._log = lambda message, level="info": harness.logs.append((level, message))
    harness._clear_prefetch_if_matches = (
        lambda payload, source: harness.cleared_prefetch.append((payload, source))
    )
    harness._flush_accumulation = lambda: None
    return harness


@pytest.mark.parametrize(
    ("stream_over_agenda", "expected_priorities", "expected_order"),
    [
        (
            True,
            {"ptt": 0, "direct": 1, "chat": 2, "kira-agenda": 3},
            ["ptt", "direct", "chat", "kira-agenda"],
        ),
        (
            False,
            {"ptt": 0, "direct": 1, "chat": 3, "kira-agenda": 2},
            ["ptt", "direct", "kira-agenda", "chat"],
        ),
    ],
)
def test_default_enqueue_resolves_the_live_priority_contract(
    monkeypatch,
    stream_over_agenda,
    expected_priorities,
    expected_order,
):
    monkeypatch.setattr(turn_priority, "STREAM_OVER_AGENDA", stream_over_agenda)
    harness = _queue_harness()

    for source in ("kira-agenda", "chat", "direct", "ptt"):
        _enqueue(harness, source, source=source)

    priorities = {item[2]: item[0] for item in harness._priority_queue}
    assert priorities == expected_priorities
    assert [item[2] for item in harness._priority_queue] == expected_order


def test_priority_setting_is_snapshotted_per_enqueue(monkeypatch):
    harness = _queue_harness()
    monkeypatch.setattr(turn_priority, "STREAM_OVER_AGENDA", True)
    _enqueue(harness, "stream-before", source="chat")
    _enqueue(harness, "agenda-before", source="kira-agenda")

    monkeypatch.setattr(turn_priority, "STREAM_OVER_AGENDA", False)
    _enqueue(harness, "stream-after", source="chat")
    _enqueue(harness, "agenda-after", source="kira-agenda")

    priorities = {item[2]: item[0] for item in harness._priority_queue}
    assert priorities == {
        "stream-before": 2,
        "agenda-before": 3,
        "stream-after": 3,
        "agenda-after": 2,
    }


def test_direct_and_stream_ttl_expire_while_agenda_is_exempt(monkeypatch):
    monkeypatch.setattr(turn_priority, "STREAM_OVER_AGENDA", True)
    monkeypatch.setattr(turn_priority, "STREAM_TTL_SECONDS", 7.0)
    monkeypatch.setattr(llm_engine, "DIRECT_ANSWER_MAX_WAIT_SECONDS", 5.0)
    now = time.time()
    harness = _ttl_harness(
        [
            _item("stale-direct", source="direct", priority=1, timestamp=now - 6.0),
            _item("stale-stream", source="chat", priority=2, timestamp=now - 8.0),
            _item(
                "stale-agenda",
                source="kira-agenda-session",
                priority=3,
                timestamp=now - 10_000.0,
            ),
        ]
    )

    MotorVocalIA._process_priority_queue(harness)

    assert [item[2] for item in harness._priority_queue] == ["stale-agenda"]
    assert harness.cleared_prefetch == [
        ("stale-direct", "direct"),
        ("stale-stream", "chat"),
    ]
    messages = [message for _level, message in harness.logs]
    assert any("TTL 5s): direct" in message for message in messages)
    assert any("TTL 7s): chat" in message for message in messages)
    assert all("kira-agenda" not in message for message in messages)


def test_agenda_first_applies_the_stream_ttl_floor(monkeypatch):
    monkeypatch.setattr(turn_priority, "STREAM_OVER_AGENDA", False)
    monkeypatch.setattr(turn_priority, "STREAM_TTL_SECONDS", 0.01)
    harness = _ttl_harness(
        [
            _item(
                "waiting-stream",
                source="chat",
                priority=3,
                timestamp=time.time() - 1.0,
            )
        ]
    )

    MotorVocalIA._process_priority_queue(harness)

    assert [item[2] for item in harness._priority_queue] == ["waiting-stream"]
    assert harness.cleared_prefetch == []


def test_backpressure_discards_lowest_non_owner_items_first(monkeypatch):
    monkeypatch.setattr(turn_priority, "STREAM_OVER_AGENDA", True)
    harness = _queue_harness(max_items=2)

    _enqueue(harness, "agenda", source="kira-agenda")
    _enqueue(harness, "stream", source="chat")
    _enqueue(harness, "owner-direct", source="direct")
    _enqueue(harness, "owner-ptt", source="ptt")

    assert [item[2] for item in harness._priority_queue] == [
        "owner-ptt",
        "owner-direct",
    ]
    assert harness.accumulated == [
        ("agenda", "kira-agenda"),
        ("stream", "chat"),
    ]


def test_queue_cap_yields_instead_of_discarding_owner_questions():
    harness = _queue_harness(max_items=1)

    _enqueue(harness, "owner-direct", source="direct")
    _enqueue(harness, "owner-ptt", source="ptt")

    assert [item[2] for item in harness._priority_queue] == [
        "owner-ptt",
        "owner-direct",
    ]
    assert harness.accumulated == []


class _DrainLockProbe:
    def __init__(self) -> None:
        self.held = False
        self.entries = 0

    def __enter__(self):
        assert not self.held
        self.held = True
        self.entries += 1
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.held = False


def test_direct_drain_holds_one_lock_across_pop_enqueue_and_preserves_fifo():
    harness = _queue_harness()
    harness.command_queue = queue.Queue()
    probe = _DrainLockProbe()
    harness._direct_drain_lock = probe
    calls = []

    def locked_enqueue(payload, **kwargs):
        calls.append((payload, probe.held))
        MotorVocalIA.enqueue(harness, payload, **kwargs)

    harness.enqueue = locked_enqueue
    harness.command_queue.put(
        ("process_context", "first", "first-history", "direct", 10.0, "local")
    )
    harness.command_queue.put(
        ("process_context", "second", "second-history", "direct", 11.0, "cloud")
    )

    MotorVocalIA._drain_pending_direct_into_priority_queue(harness)

    assert calls == [("first", True), ("second", True)]
    assert probe.entries == 1 and not probe.held
    assert harness.command_queue.empty()
    assert [item[2] for item in harness._priority_queue] == ["first", "second"]
    assert [item[0] for item in harness._priority_queue] == [1, 1]
    assert [item[4:] for item in harness._priority_queue] == [
        ("first-history", 10.0, "local"),
        ("second-history", 11.0, "cloud"),
    ]


def test_owner_bundle_takes_only_the_contiguous_owner_prefix():
    harness = SimpleNamespace(
        _pq_lock=threading.Lock(),
        _priority_queue=[
            _item("second-owner", source="direct", priority=1, timestamp=2.0),
            _item("viewer", source="chat", priority=1, timestamp=3.0),
            _item("later-owner", source="direct", priority=1, timestamp=4.0),
        ],
    )
    head = _item("first-owner", source="ptt", priority=0, timestamp=1.0)

    taken = MotorVocalIA._take_owner_bundle_prefix(harness, head)

    assert [item[2] for item in taken] == ["second-owner"]
    assert [item[2] for item in harness._priority_queue] == [
        "viewer",
        "later-owner",
    ]


def test_owner_bundle_character_cap_defers_whole_questions():
    assert llm_engine.OWNER_BUNDLE_MAX_CHARS == 2_000
    harness = SimpleNamespace(
        _pq_lock=threading.Lock(),
        _priority_queue=[
            _item("b" * 900, timestamp=2.0),
            _item("c" * 900, timestamp=3.0),
        ],
    )
    head = _item("a" * 900, timestamp=1.0)

    taken = MotorVocalIA._take_owner_bundle_prefix(harness, head)

    assert [item[2] for item in taken] == ["b" * 900]
    assert [item[2] for item in harness._priority_queue] == ["c" * 900]


def test_failed_bundle_requeues_original_followers_in_scheduler_order():
    direct = _item(
        "direct-follower",
        source="direct",
        priority=1,
        timestamp=10.0,
        history_text="direct-history",
    )
    ptt = _item(
        "ptt-follower",
        source="ptt",
        priority=0,
        timestamp=30.0,
        history_text="ptt-history",
    )
    latecomer = _item(
        "latecomer",
        source="direct",
        priority=1,
        timestamp=20.0,
    )
    harness = SimpleNamespace(
        _pq_lock=threading.Lock(),
        _priority_queue=[latecomer],
        _streamed_turn_job=None,
        _streamed_turn_prefix=None,
        _generar_dialogo=lambda *args, **kwargs: "",
    )
    harness._requeue_owner_bundle_followers = (
        lambda followers: MotorVocalIA._requeue_owner_bundle_followers(
            harness, followers
        )
    )

    MotorVocalIA._ejecutar_inferencia(
        harness,
        "failed bundle",
        source=llm_engine.OWNER_BUNDLE_SOURCE,
        bundle_followers=[direct, ptt],
    )

    assert harness._priority_queue == [ptt, direct, latecomer]
    assert harness._priority_queue[0] is ptt
    assert harness._priority_queue[1] is direct
    assert harness._priority_queue[2] is latecomer
