"""Edge-TTS failure classification (TA1) and circuit breaker (TA3). Pure unit tests."""
from __future__ import annotations

import asyncio
import socket
import ssl

import aiohttp
import pytest

from opencohost.core.speech import speech_rate
from opencohost.core.speech.speech_rate import (
    EdgeBreaker,
    _is_connection_error,
    classify_edge_failure,
)


def _handshake(status):
    ri = aiohttp.RequestInfo(
        url=aiohttp.client.URL("wss://example.invalid/x"),
        method="GET",
        headers={},
        real_url=aiohttp.client.URL("wss://example.invalid/x"),
    )
    return aiohttp.WSServerHandshakeError(ri, (), status=status, message="x")


class TestClassify:
    @pytest.mark.parametrize("exc", [socket.gaierror("dns"), ssl.SSLError("tls")])
    def test_offline_kinds(self, exc):
        f = classify_edge_failure(exc)
        assert f.kind == "offline" and f.permanent is True
        assert f.code == "offline"

    def test_http_403_is_transient_with_status(self):
        f = classify_edge_failure(_handshake(403))
        assert (f.kind, f.status, f.permanent, f.code) == ("http", 403, False, "http_403")

    def test_timeout_transient(self):
        f = classify_edge_failure(asyncio.TimeoutError())
        assert (f.kind, f.permanent, f.code) == ("timeout", False, "timeout")

    def test_other_transient(self):
        f = classify_edge_failure(RuntimeError("boom"))
        assert (f.kind, f.permanent, f.code) == ("other", False, "error")

    def test_cause_chain_walked(self):
        try:
            try:
                raise _handshake(429)
            except Exception as inner:
                raise RuntimeError("wrapped") from inner
        except RuntimeError as outer:
            f = classify_edge_failure(outer)
        assert f.kind == "http" and f.status == 429

    def test_is_connection_error_wrapper_unchanged(self):
        assert _is_connection_error(socket.gaierror("x")) is True
        assert _is_connection_error(_handshake(403)) is False
        assert _is_connection_error(asyncio.TimeoutError()) is False


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def _breaker(clock):
    return EdgeBreaker(threshold=3, cooldown_s=120, max_cooldown_s=900, clock=clock)


class TestBreaker:
    def test_opens_after_threshold(self):
        c = Clock()
        b = _breaker(c)
        assert b.allow()
        assert b.record_failure("http_403") is None
        assert b.record_failure("http_403") is None
        assert b.record_failure("http_403") == "open"
        assert b.degraded is True
        assert b.allow() is False
        assert b.last_error == "http_403"

    def test_probe_after_cooldown_success_closes(self):
        c = Clock()
        b = _breaker(c)
        for _ in range(3):
            b.record_failure("http_403")
        c.t += 119
        assert b.allow() is False
        c.t += 2
        assert b.allow() is True  # probe
        assert b.allow() is False  # only one probe in flight
        assert b.record_success() == "closed"
        assert b.degraded is False and b.last_error is None
        assert b.allow() is True

    def test_probe_failure_doubles_cooldown_to_cap(self):
        c = Clock()
        b = _breaker(c)
        for _ in range(3):
            b.record_failure("timeout")
        expected = [240, 480, 900, 900]
        for cd in expected:
            c.t += b.cooldown_s + 1
            assert b.allow() is True
            assert b.record_failure("timeout") == "open"
            assert b.cooldown_s == cd

    def test_success_resets_consecutive_count(self):
        b = _breaker(Clock())
        b.record_failure("error")
        b.record_failure("error")
        assert b.record_success() is None
        b.record_failure("error")
        b.record_failure("error")
        assert b.degraded is False
        assert b.allow() is True

    def test_close_resets_cooldown(self):
        c = Clock()
        b = _breaker(c)
        for _ in range(3):
            b.record_failure("error")
        c.t += 121
        b.allow()
        b.record_failure("error")
        assert b.cooldown_s == 240
        c.t += 241
        b.allow()
        b.record_success()
        for _ in range(3):
            b.record_failure("error")
        assert b.cooldown_s == 120


def test_try_acquire_reports_half_open_transition_once():
    c = Clock()
    b = _breaker(c)
    assert b.try_acquire() == (True, None)
    for _ in range(3):
        b.record_failure("error")
    assert b.try_acquire() == (False, None)
    c.t += 121
    assert b.try_acquire() == (True, "half_open")
    assert b.try_acquire() == (False, None)
