"""Tests for GET /api/stream/chat-live/messages?since=cursor (RF3 -- B4,
conductor/tracks/tauri_stream_chat_20260812/proposal.md 5).

Scope: the endpoint (opencohost/api/routers/stream.py) and its ChatFeedSink
(opencohost/api/engine_host.py) exercised together through a real
TestClient/HTTP round trip. Pure ChatFeedSink unit behaviour (seq
monotonicity, since() filtering, ring cap, restart boot change, record()
field whitelist) already lives in tests/test_chat_feed_sink.py -- this file
does not repeat it. What this file adds:

- concurrency through the real endpoint (many writer threads + reader
  threads polling HTTP, not the sink directly)
- cursor monotonicity and the 50-message page cap AT THE HTTP LAYER
- eviction observed through the endpoint (a stale cursor gets a visible
  seq gap, never a silent hole)
- boot-change detection through the endpoint
- auth (operator token required, matching test_api_agent_gateway.py's
  agent_headers/operator_headers style)
- the full chain end-to-end: Aggregator.process_message -> runtime_screen
  (design.md 3.1/8) -> chat_feed.record -> GET .../messages, proving a
  message the safety floor blocks never reaches HTTP
- no raw chat text ever lands in logging output (root-logger caplog)
"""

import logging
import os
import threading
import time
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from fastapi.testclient import TestClient

import opencohost.api.auth as auth
import opencohost.api.engine_host as engine_host_mod
import opencohost.api.main as main_mod
from opencohost.smart_aggregator.aggregator import Aggregator
from opencohost.smart_aggregator.chat_source import TwitchChatSource
from tests.test_api_phase1 import FakeHost

_DEFAULT_TEST_ORIGINS = ["http://localhost:5173"]
_URL = "/api/stream/chat-live/messages"


@pytest.fixture(autouse=True)
def _reset_host_active():
    main_mod._host_active = False
    yield
    main_mod._host_active = False


@pytest.fixture(autouse=True)
def _reset_block_throttle():
    """validation.py's per-rule block counters/throttle are module-global --
    reset before each test, same as TestInputSafetyFloor in
    tests/test_smart_aggregator.py."""
    from opencohost.config import validation

    validation._reset_block_throttle()
    yield


@pytest.fixture
def operator_headers():
    auth.ensure_tokens()
    return {"Authorization": f"Bearer {auth.load_tokens()['operator']}"}


@pytest.fixture
def agent_headers():
    auth.ensure_tokens()
    return {"Authorization": f"Bearer {auth.load_tokens()['agent']}"}


def _app(host_factory):
    return main_mod.create_app(host_factory=host_factory, cors_origins=_DEFAULT_TEST_ORIGINS)


def _host_with_chat_feed(sink):
    def factory():
        host = FakeHost()
        host.chat_feed = sink
        return host

    return factory


def _real_aggregator(smart_aggregator_config, temp_dir) -> Aggregator:
    """Mirrors TestInputSafetyFloor's setup in tests/test_smart_aggregator.py
    -- a real Aggregator, real config, real MessageFilter/spam/runtime_screen
    chain, writing history to a throwaway temp dir."""
    cfg = deepcopy(smart_aggregator_config)
    config_path = os.path.join(temp_dir, "smart_aggregator.yaml")
    cfg["history"]["db_path"] = os.path.join(temp_dir, "sessions.db")
    cfg["history"]["jsonl_path"] = os.path.join(temp_dir, "chat_log.jsonl")
    with open(config_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(cfg, f, allow_unicode=True)
    return Aggregator(config_path=config_path)


# ──────────────────────────────────────────────────────────────────────────
# AUTH -- operator token required, unconditionally (R8-CRITICAL)
# ──────────────────────────────────────────────────────────────────────────


def test_messages_no_token_is_403():
    app = _app(FakeHost)
    with TestClient(app) as client:
        resp = client.get(_URL)
    assert resp.status_code == 403
    assert resp.json()["detail"] == "operator token required"


def test_messages_garbage_token_is_403():
    app = _app(FakeHost)
    with TestClient(app) as client:
        resp = client.get(_URL, headers={"Authorization": "Bearer not-a-real-token"})
    assert resp.status_code == 403


def test_messages_agent_token_is_403(agent_headers):
    """The agent token is valid but the wrong role -- this GET is the one
    surface where even a lesser-privileged real token must not pass."""
    app = _app(FakeHost)
    with TestClient(app) as client:
        resp = client.get(_URL, headers=agent_headers)
    assert resp.status_code == 403


def test_messages_corrupt_token_file_is_503_not_500(operator_headers):
    """An unreadable/corrupt token file is a broken server, not a bad
    credential. `_operator_role` deliberately lets TokenFileError propagate
    (unlike agent.py's fail-closed-to-None, which is safe only because
    auth_middleware already gated the request); the endpoint catches it and
    returns the same 503 `auth_unavailable` auth_middleware returns.

    Two things are asserted at once: it is not a 500 traceback, and it is not
    a 403 that would send the operator hunting for a token that is fine.
    Fail-closed either way -- buffered chat is never served."""
    from opencohost.config import settings

    sink = engine_host_mod.ChatFeedSink()
    sink.record({"user": "viewer", "text": "hola", "timestamp": 1.0})
    app = _app(_host_with_chat_feed(sink))

    # operator_headers already minted a valid file; corrupt it in place. The
    # lifespan's ensure_tokens() is a no-op once the path exists, so the token
    # in the header stays the "right" one -- only the file is broken.
    Path(settings.API_TOKENS_FILE).write_text("{ not json at all", encoding="utf-8")

    with TestClient(app) as client:
        resp = client.get(_URL, params={"since": 0}, headers=operator_headers)

    assert resp.status_code == 503
    body = resp.json()
    assert body == {"detail": "auth_unavailable"}
    assert "messages" not in body


def test_messages_operator_token_is_200(operator_headers):
    app = _app(FakeHost)
    with TestClient(app) as client:
        resp = client.get(_URL, headers=operator_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert set(body.keys()) == {"messages", "cursor", "boot", "session", "more_pending"}
    assert body["messages"] == []
    assert body["cursor"] == 0
    assert body["session"] == 0
    assert body["more_pending"] is False


# ──────────────────────────────────────────────────────────────────────────
# THE 50-CAP -- 120 pending drains as 50 + 50 + 20, in order
# ──────────────────────────────────────────────────────────────────────────


def test_120_pending_drains_as_50_then_50_then_20_in_order(operator_headers):
    sink = engine_host_mod.ChatFeedSink(maxlen=1000)
    for i in range(120):
        sink.record({"user": f"viewer{i}", "text": f"msg{i}", "timestamp": float(i)})
    app = _app(_host_with_chat_feed(sink))

    with TestClient(app) as client:
        page1 = client.get(_URL, params={"since": 0}, headers=operator_headers).json()
        assert [m["seq"] for m in page1["messages"]] == list(range(1, 51))
        assert page1["more_pending"] is True
        assert page1["cursor"] == 50  # advances only to the last message ACTUALLY returned

        page2 = client.get(_URL, params={"since": page1["cursor"]}, headers=operator_headers).json()
        assert [m["seq"] for m in page2["messages"]] == list(range(51, 101))
        assert page2["more_pending"] is True
        assert page2["cursor"] == 100

        page3 = client.get(_URL, params={"since": page2["cursor"]}, headers=operator_headers).json()
        assert [m["seq"] for m in page3["messages"]] == list(range(101, 121))
        assert page3["more_pending"] is False
        assert page3["cursor"] == 120  # uncapped page -> mirrors the sink's own cursor

        # caught up: empty, cursor unchanged, never regresses
        page4 = client.get(_URL, params={"since": page3["cursor"]}, headers=operator_headers).json()
        assert page4["messages"] == []
        assert page4["cursor"] == 120


# ──────────────────────────────────────────────────────────────────────────
# CURSOR MONOTONICITY -- polling with the returned cursor never re-delivers
# and never skips, across many pages
# ──────────────────────────────────────────────────────────────────────────


def test_cursor_never_regresses_and_full_drain_has_no_gap_no_duplicate(operator_headers):
    sink = engine_host_mod.ChatFeedSink(maxlen=1000)
    total = 235
    for i in range(total):
        sink.record({"user": "viewer", "text": f"m{i}", "timestamp": float(i)})
    app = _app(_host_with_chat_feed(sink))

    with TestClient(app) as client:
        seen_seqs = []
        seen_cursors = []
        since = 0
        while True:
            body = client.get(_URL, params={"since": since}, headers=operator_headers).json()
            seen_cursors.append(body["cursor"])
            seen_seqs.extend(m["seq"] for m in body["messages"])
            since = body["cursor"]
            if not body["more_pending"] and not body["messages"]:
                break

        assert seen_seqs == list(range(1, total + 1))  # strictly in order, no gap, no dup
        assert seen_cursors == sorted(seen_cursors)  # cursor never regresses across polls


# ──────────────────────────────────────────────────────────────────────────
# EVICTION -- past maxlen the oldest are dropped; a stale cursor surfaces a
# visible seq gap (the client's own "I fell behind" signal) instead of a
# page that silently starts at since+1 as if nothing were missing
# ──────────────────────────────────────────────────────────────────────────


def test_eviction_stale_cursor_gets_visible_seq_gap_not_a_silent_hole(operator_headers):
    sink = engine_host_mod.ChatFeedSink(maxlen=200)  # production default
    for i in range(250):
        sink.record({"user": "viewer", "text": f"m{i}", "timestamp": float(i)})
    app = _app(_host_with_chat_feed(sink))

    with TestClient(app) as client:
        # A client that last saw seq=10 (long evicted -- only seq 51..250 remain,
        # 250-200=50 evicted) must NOT be handed a page that starts at seq=11
        # (its own since+1) as if the 40 messages in between still existed.
        body = client.get(_URL, params={"since": 10}, headers=operator_headers).json()
        first_seq = body["messages"][0]["seq"]
        assert first_seq == 51  # oldest surviving message, not since+1
        assert first_seq > 10 + 1  # the gap itself IS the "you fell behind" signal

        # A totally fresh client (since=0) gets exactly what's left -- no crash,
        # no exception, just the ring's current contents.
        fresh = client.get(_URL, params={"since": 0}, headers=operator_headers).json()
        assert fresh["messages"][0]["seq"] == 51


# ──────────────────────────────────────────────────────────────────────────
# BOOT CHANGE -- a fresh sink (process restart) produces a different boot
# stamp and a reset cursor, observable through the endpoint
# ──────────────────────────────────────────────────────────────────────────


def test_boot_change_is_visible_through_the_endpoint(operator_headers, monkeypatch):
    monkeypatch.setattr(engine_host_mod.time, "time", lambda: 1000.0)
    sink1 = engine_host_mod.ChatFeedSink()
    sink1.record({"user": "viewer", "text": "hola", "timestamp": 1.0})
    app = _app(_host_with_chat_feed(sink1))

    with TestClient(app) as client:
        before = client.get(_URL, params={"since": 0}, headers=operator_headers).json()
        assert before["boot"] == 1000.0
        assert before["cursor"] == 1

    monkeypatch.setattr(engine_host_mod.time, "time", lambda: 2000.0)
    sink2 = engine_host_mod.ChatFeedSink()  # simulates a fresh process boot
    app2 = _app(_host_with_chat_feed(sink2))

    with TestClient(app2) as client:
        after = client.get(_URL, params={"since": before["cursor"]}, headers=operator_headers).json()
        assert after["boot"] == 2000.0
        assert after["boot"] != before["boot"]  # restart is detectable
        assert after["cursor"] == 0  # seq reset -- client must resync, not skip


# ──────────────────────────────────────────────────────────────────────────
# SOURCE CHANGE -- connecting to a second channel bumps `session` and drops
# the first channel's chat, without ever rewinding the cursor
# ──────────────────────────────────────────────────────────────────────────


def test_session_change_is_visible_through_the_endpoint(operator_headers):
    sink = engine_host_mod.ChatFeedSink(maxlen=1000)
    for i in range(120):
        sink.record({"user": "canalA_viewer", "text": f"a{i}", "timestamp": float(i)})
    app = _app(_host_with_chat_feed(sink))

    with TestClient(app) as client:
        before = client.get(_URL, params={"since": 0}, headers=operator_headers).json()
        assert before["session"] == 0
        assert before["cursor"] == 50  # capped page, client is mid-backlog

        sink.new_session()  # operator disconnects #canalA and connects #canalB
        sink.record({"user": "canalB_viewer", "text": "b0", "timestamp": 900.0})

        # The client polls on with its STALE cursor from the old session.
        after = client.get(
            _URL, params={"since": before["cursor"]}, headers=operator_headers
        ).json()
        assert after["session"] == 1  # the "clear your list" signal
        assert after["session"] != before["session"]
        assert after["boot"] == before["boot"]  # same process -- only the source changed
        assert [m["text"] for m in after["messages"]] == ["b0"]  # nothing from #canalA
        assert after["cursor"] == 121  # seq kept climbing; b0 was NOT skipped


def test_real_aggregator_channel_switch_never_mixes_two_channels(
    smart_aggregator_config, temp_dir, operator_headers
):
    """End-to-end through a real Aggregator with the exact production wiring
    (engine_host.py): disconnect leaves the scrollback intact, the NEXT
    connect is what wipes it and bumps `session`.

    The FIRST connect bumps too (0 -> 1) -- it is a source change like any
    other, and a client that sees it just clears an already-empty list."""
    agg = _real_aggregator(smart_aggregator_config, temp_dir)
    host = FakeHost()
    host.aggregator = agg
    agg.on_filtered_message = host.chat_feed.record
    agg.on_source_changed = host.chat_feed.new_session
    app = _app(lambda: host)

    canal_a = "que buen stream hoy amigos, se ve genial todo"
    canal_b = "buenas noches gente, primera vez en este canal"

    with patch.object(TwitchChatSource, "connect"):
        agg.connect("canalA", platform="twitch")
        agg.process_message({"user": "viewerA", "text": canal_a, "timestamp": time.time()})

        # Disconnect alone: the operator can still scroll back over #canalA.
        agg.disconnect()
        with TestClient(app) as client:
            after_disconnect = client.get(
                _URL, params={"since": 0}, headers=operator_headers
            ).json()
        assert [m["text"] for m in after_disconnect["messages"]] == [canal_a]
        assert after_disconnect["session"] == 1  # unchanged by the disconnect

        # The next connect is what wipes it.
        agg.connect("canalB", platform="twitch")
        agg.process_message({"user": "viewerB", "text": canal_b, "timestamp": time.time()})

    with TestClient(app) as client:
        body = client.get(
            _URL, params={"since": after_disconnect["cursor"]}, headers=operator_headers
        ).json()

    assert body["session"] == 2
    texts = [m["text"] for m in body["messages"]]
    assert canal_b in texts
    assert canal_a not in texts


# ──────────────────────────────────────────────────────────────────────────
# CONCURRENCY -- real writer threads racing real reader threads through the
# actual HTTP endpoint (not the sink directly): no lost messages, no
# corrupted responses, no deadlock
# ──────────────────────────────────────────────────────────────────────────


def test_concurrent_writers_and_http_readers_no_lost_messages_no_deadlock(operator_headers):
    # Large maxlen so this test isolates the concurrency question from ring
    # eviction (already covered separately above).
    sink = engine_host_mod.ChatFeedSink(maxlen=5000)
    app = _app(_host_with_chat_feed(sink))

    num_writers = 8
    records_per_writer = 40
    total = num_writers * records_per_writer
    num_readers = 4
    reader_iteration_cap = 400  # safety valve, not expected to be hit

    errors = []
    reader_stop = threading.Event()

    def _writer(idx):
        for i in range(records_per_writer):
            sink.record({"user": f"w{idx}", "text": f"msg-{idx}-{i}", "timestamp": time.time()})

    def _reader(client):
        cursor = 0
        iterations = 0
        try:
            while not reader_stop.is_set() and iterations < reader_iteration_cap:
                resp = client.get(_URL, params={"since": cursor}, headers=operator_headers)
                if resp.status_code != 200:
                    errors.append(f"unexpected status {resp.status_code}")
                    return
                body = resp.json()
                if set(body.keys()) != {"messages", "cursor", "boot", "session", "more_pending"}:
                    errors.append(f"malformed body {body!r}")
                    return
                if body["cursor"] < cursor:
                    errors.append(f"cursor regressed: {cursor} -> {body['cursor']}")
                    return
                cursor = body["cursor"]
                iterations += 1
        except Exception as exc:  # any exception here = corruption/deadlock signal
            errors.append(repr(exc))

    with TestClient(app) as client:
        readers = [threading.Thread(target=_reader, args=(client,)) for _ in range(num_readers)]
        for t in readers:
            t.start()

        writers = [threading.Thread(target=_writer, args=(i,)) for i in range(num_writers)]
        for t in writers:
            t.start()
        for t in writers:
            t.join(timeout=30)
            assert not t.is_alive(), "writer thread hung -- deadlock"

        reader_stop.set()
        for t in readers:
            t.join(timeout=30)
            assert not t.is_alive(), "reader thread hung -- deadlock"

        assert errors == []

        # Definitive final drain proves no write was lost regardless of how
        # the reader/writer race interleaved above.
        seen = []
        since = 0
        while True:
            body = client.get(_URL, params={"since": since}, headers=operator_headers).json()
            seen.extend(m["seq"] for m in body["messages"])
            since = body["cursor"]
            if not body["more_pending"] and not body["messages"]:
                break

        assert sorted(seen) == list(range(1, total + 1))
        assert len(seen) == len(set(seen))  # no duplicates handed out


# ──────────────────────────────────────────────────────────────────────────
# NOTHING UNFILTERED -- end-to-end through Aggregator.process_message, not
# the sink directly. Proves the whole chain (MessageFilter -> spam gate ->
# runtime_screen input safety floor) screens before HTTP, not just that
# ChatFeedSink is innocent of anything fed to it directly.
# ──────────────────────────────────────────────────────────────────────────


class TestNothingUnfilteredReachesHttp:

    @pytest.mark.parametrize("blocked_text", [
        "Che mi numero personal para contactarme es 555-123-4567 tranquilo",  # no_doxxing
        "Amigo la verdad es que eres un pendejo tremendo hoy",  # no_hate_speech
        "Recuerda que mi direccion IP privada es 192.168.1.100 para el juego",  # no_personal_viewer_data
    ])
    def test_blocked_message_never_reaches_the_endpoint(
        self, blocked_text, smart_aggregator_config, temp_dir, operator_headers
    ):
        agg = _real_aggregator(smart_aggregator_config, temp_dir)
        host = FakeHost()
        host.aggregator = agg
        # Exact production wiring (engine_host.py:824) -- direct assignment,
        # no lambda/wrapper indirection.
        agg.on_filtered_message = host.chat_feed.record
        app = _app(lambda: host)

        agg.process_message({"user": "viewer1", "text": blocked_text, "timestamp": time.time()})

        with TestClient(app) as client:
            resp = client.get(_URL, params={"since": 0}, headers=operator_headers)
        body = resp.json()
        assert body["messages"] == []

    def test_clean_message_reaches_the_endpoint_blocked_one_does_not(
        self, smart_aggregator_config, temp_dir, operator_headers
    ):
        agg = _real_aggregator(smart_aggregator_config, temp_dir)
        host = FakeHost()
        host.aggregator = agg
        agg.on_filtered_message = host.chat_feed.record
        app = _app(lambda: host)

        blocked_text = "Che mi numero personal para contactarme es 555-123-4567 tranquilo"
        clean_text = "que buen stream hoy amigos, se ve genial todo"
        agg.process_message({"user": "viewer1", "text": blocked_text, "timestamp": time.time()})
        agg.process_message({"user": "viewer2", "text": clean_text, "timestamp": time.time()})

        with TestClient(app) as client:
            resp = client.get(_URL, params={"since": 0}, headers=operator_headers)
        body = resp.json()
        texts = [m["text"] for m in body["messages"]]
        assert clean_text in texts
        assert blocked_text not in texts
        assert len(body["messages"]) == 1


# ──────────────────────────────────────────────────────────────────────────
# NO LEAK -- raw chat text must never appear in logging output, root logger,
# across the blocked/clean chain above plus a concurrent hammer
# ──────────────────────────────────────────────────────────────────────────


def test_no_raw_chat_text_in_any_log_output(
    smart_aggregator_config, temp_dir, operator_headers, caplog
):
    caplog.set_level(logging.DEBUG)  # root logger, every logger propagates here

    agg = _real_aggregator(smart_aggregator_config, temp_dir)
    host = FakeHost()
    host.aggregator = agg
    agg.on_filtered_message = host.chat_feed.record
    app = _app(lambda: host)

    blocked_sentinel = "SENTINEL-BLOCKED-555-987-6543-do-not-leak"
    blocked_text = f"che mi numero personal es {blocked_sentinel} tranquilo"
    clean_sentinel = "SENTINEL-CLEAN-do-not-leak-either"
    clean_text = f"que buen stream {clean_sentinel} hoy amigos genial"
    author_sentinel = "sentinel_author_do_not_leak"

    agg.process_message({"user": author_sentinel, "text": blocked_text, "timestamp": time.time()})
    agg.process_message({"user": author_sentinel, "text": clean_text, "timestamp": time.time()})

    with TestClient(app) as client:
        for _ in range(5):
            client.get(_URL, params={"since": 0}, headers=operator_headers)
        # unauthenticated attempts must not leak the header/token either
        client.get(_URL, headers={"Authorization": "Bearer some-fake-token-leak-check"})

    log_text = caplog.text
    assert blocked_sentinel not in log_text
    assert clean_sentinel not in log_text
    assert author_sentinel not in log_text
    assert blocked_text not in log_text
    assert clean_text not in log_text
