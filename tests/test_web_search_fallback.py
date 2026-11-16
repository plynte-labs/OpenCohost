"""Tests for web search API fallback (Tasks T2, T3, T4, T6 & Phase 4).

Covers:
- Settings & configuration defaults and env resolution (T2)
- Usage guard / transactional SQLite quota tracking (T3 / Phase 1)
- Engine health and circuit breakers (T4)
- Lookup fallback chain wiring with search_api_runtime and transactional ledger (T2 / Phase 4)
- A5: Pre-dispatch failure (DNS failure or SSRF blocked in PREPARE) releases SQLite reservation (consumes 0)
- A6: Competing request while runtime slot is occupied returns api_reason="busy"
- Full fallback chain with runtime and SQLite ledger (html -> lite -> tavily -> brave)
- Zero-key / unconfigured parity: when no keys are set or fallback is "off", behavior, return values and log fields stay 100% byte-identical to baseline
- llm_engine_web log formatting and backward compatibility (T2/T5)
- Kira messaging and prompt block formatting (T6)
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any, Callable, Dict, List, Optional
from unittest.mock import MagicMock

import httpx
import pytest

from opencohost.config import settings
from opencohost.core.web import engine_health, lookup, prompt, search, search_api, search_api_runtime, search_usage
from opencohost.core.web.fetch import SafeFetcher
from opencohost.core.web.lookup import (
    OUTCOME_BLOCKED,
    OUTCOME_EMPTY,
    OUTCOME_ERROR,
    OUTCOME_OK,
    OUTCOME_TIMEOUT,
    WebLookupResult,
)


@pytest.fixture(autouse=True)
def reset_engine_health():
    engine_health.reset_health()
    yield
    engine_health.reset_health()


# ─────────────────────────────────────────────────────────────
# 1. Settings & Config (T2)
# ─────────────────────────────────────────────────────────────

def test_settings_search_api_defaults(monkeypatch):
    monkeypatch.delenv("OPENCOHOST_TAVILY_API_KEY", raising=False)
    monkeypatch.delenv("OPENCOHOST_BRAVE_API_KEY", raising=False)
    monkeypatch.delenv("OPENCOHOST_SEARCH_FALLBACK", raising=False)
    monkeypatch.delenv("OPENCOHOST_TAVILY_MONTHLY_CAP", raising=False)
    monkeypatch.delenv("OPENCOHOST_BRAVE_MONTHLY_CAP", raising=False)

    assert settings._resolve_tavily_api_key() == ""
    assert settings._resolve_brave_api_key() == ""
    assert settings._resolve_search_fallback() == ("tavily", "brave")
    assert settings._resolve_tavily_monthly_cap() == 900
    assert settings._resolve_brave_monthly_cap() == 900
    assert settings.SEARCH_API_MIN_BUDGET_S == 1.5
    assert settings.DDG_COOLDOWN_S == 600.0


def test_settings_search_api_env_overrides(monkeypatch):
    monkeypatch.setenv("OPENCOHOST_TAVILY_API_KEY", "tvly-test-123")
    monkeypatch.setenv("OPENCOHOST_BRAVE_API_KEY", "brave-test-456")
    monkeypatch.setenv("OPENCOHOST_SEARCH_FALLBACK", "brave,tavily")
    monkeypatch.setenv("OPENCOHOST_TAVILY_MONTHLY_CAP", "500")
    monkeypatch.setenv("OPENCOHOST_BRAVE_MONTHLY_CAP", "300")

    assert settings._resolve_tavily_api_key() == "tvly-test-123"
    assert settings._resolve_brave_api_key() == "brave-test-456"
    assert settings._resolve_search_fallback() == ("brave", "tavily")
    assert settings._resolve_tavily_monthly_cap() == 500
    assert settings._resolve_brave_monthly_cap() == 300


def test_settings_search_fallback_off(monkeypatch):
    monkeypatch.setenv("OPENCOHOST_SEARCH_FALLBACK", "off")
    assert settings._resolve_search_fallback() == ()


# ─────────────────────────────────────────────────────────────
# 2. Usage Guard (T3 / Phase 1 Transactional Ledger)
# ─────────────────────────────────────────────────────────────

def test_search_usage_initial_and_increment(tmp_path):
    db_file = tmp_path / "web_search_usage.sqlite3"
    tracker = search_usage.SearchUsageTracker(db_path=str(db_file), current_month="2026-10")

    assert tracker.get_count("tavily") == 0
    assert tracker.get_count("brave") == 0
    assert not tracker.is_capped("tavily", cap=900)

    res = tracker.reserve("tavily", cap=900)
    assert res.ok is True
    assert tracker.get_count("tavily") == 0
    assert tracker.mark_dispatched(res.request_id) is True
    assert tracker.get_count("tavily") == 1
    assert tracker.get_count("brave") == 0

    res_b = tracker.reserve("brave", cap=900)
    assert tracker.mark_dispatched(res_b.request_id) is True
    assert tracker.get_count("brave") == 1


def test_search_usage_month_rollover(tmp_path):
    db_file = tmp_path / "web_search_usage.sqlite3"
    tracker_sept = search_usage.SearchUsageTracker(db_path=str(db_file), current_month="2026-09")
    tracker_sept._set_consumed("tavily", "2026-09", 50)
    tracker_sept._set_consumed("brave", "2026-09", 20)

    tracker_oct = search_usage.SearchUsageTracker(db_path=str(db_file), current_month="2026-10")
    assert tracker_oct.get_count("tavily") == 0
    assert tracker_oct.get_count("brave") == 0

    res = tracker_oct.reserve("tavily", cap=900)
    tracker_oct.mark_dispatched(res.request_id)
    assert tracker_oct.get_count("tavily") == 1
    assert tracker_oct.get_consumed_for_period("tavily", "2026-09") == 50


def test_search_usage_cap_reached(tmp_path):
    db_file = tmp_path / "web_search_usage.sqlite3"
    tracker = search_usage.SearchUsageTracker(db_path=str(db_file), current_month="2026-10")
    tracker._set_consumed("tavily", "2026-10", 900)
    tracker._set_consumed("brave", "2026-10", 899)

    assert tracker.is_capped("tavily", cap=900)
    assert not tracker.is_capped("brave", cap=900)

    res_tavily = tracker.reserve("tavily", cap=900)
    assert res_tavily.ok is False
    assert res_tavily.reason == search_usage.REASON_LOCAL_CAP

    res_brave = tracker.reserve("brave", cap=900)
    assert res_brave.ok is True
    tracker.mark_dispatched(res_brave.request_id)
    assert tracker.is_capped("brave", cap=900)


def test_search_usage_corrupt_file_handling(tmp_path, caplog):
    db_file = tmp_path / "web_search_usage.sqlite3"
    legacy_file = tmp_path / "web_search_usage.json"
    legacy_file.write_text("{corrupt-json", encoding="utf-8")

    caplog.set_level(logging.WARNING, logger="OpenCohost")
    tracker = search_usage.SearchUsageTracker(
        db_path=str(db_file),
        legacy_file_path=str(legacy_file),
        current_month="2026-10",
    )

    res = tracker.reserve("tavily")
    assert res.ok is False
    assert res.reason == search_usage.REASON_USAGE_STATE

    warnings = [
        r.getMessage()
        for r in caplog.records
        if "corrupt" in r.getMessage().lower() or "malformed" in r.getMessage().lower()
    ]
    assert len(warnings) >= 1


def test_search_usage_concurrent_increments(tmp_path):
    db_file = tmp_path / "web_search_usage.sqlite3"
    tracker = search_usage.SearchUsageTracker(db_path=str(db_file), current_month="2026-10")

    def worker():
        for _ in range(10):
            res = tracker.reserve("tavily", cap=1000)
            if res.ok:
                tracker.mark_dispatched(res.request_id)

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert tracker.get_count("tavily") == 100


# ─────────────────────────────────────────────────────────────
# 3. Health & Circuit Breakers (T4)
# ─────────────────────────────────────────────────────────────

def test_engine_health_ddg_cooldown_with_api_key():
    now = 1000.0
    health = engine_health.EngineHealth(clock=lambda: now, cooldown_s=600.0)

    assert not health.is_ddg_cooled_down(has_api_keys=True)

    health.record_ddg_attempt(engine="html", reason=search.REASON_ANTI_BOT)
    health.record_ddg_attempt(engine="lite", reason=search.REASON_ANTI_BOT)

    assert health.is_ddg_cooled_down(has_api_keys=True)
    assert not health.is_ddg_cooled_down(has_api_keys=False)

    now += 601.0
    assert not health.is_ddg_cooled_down(has_api_keys=True)


def test_engine_health_ddg_no_cooldown_if_only_one_fails():
    now = 1000.0
    health = engine_health.EngineHealth(clock=lambda: now, cooldown_s=600.0)

    health.record_ddg_attempt(engine="html", reason=search.REASON_ANTI_BOT)
    health.record_ddg_attempt(engine="lite", reason="empty")
    assert not health.is_ddg_cooled_down(has_api_keys=True)


def test_engine_health_provider_auth_breaker(caplog):
    health = engine_health.EngineHealth()
    caplog.set_level(logging.WARNING, logger="OpenCohost")

    avail, reason = health.is_provider_available("tavily")
    assert avail and reason == ""

    health.record_provider_failure("tavily", search_api.REASON_AUTH)

    avail, reason = health.is_provider_available("tavily")
    assert not avail
    assert reason == search_api.REASON_AUTH

    warnings = [r.getMessage() for r in caplog.records if "provider=tavily disabled reason=auth" in r.getMessage()]
    assert len(warnings) == 1

    health.record_provider_failure("tavily", search_api.REASON_AUTH)
    warnings2 = [r.getMessage() for r in caplog.records if "provider=tavily disabled reason=auth" in r.getMessage()]
    assert len(warnings2) == 1


def test_engine_health_provider_quota_breaker():
    current_month = "2026-10"
    health = engine_health.EngineHealth(month_resolver=lambda: current_month)

    health.record_provider_failure("brave", search_api.REASON_QUOTA)
    avail, reason = health.is_provider_available("brave")
    assert not avail and reason == search_api.REASON_QUOTA

    current_month = "2026-11"
    avail, reason = health.is_provider_available("brave")
    assert avail and reason == ""


def test_engine_health_provider_rate_limit_breaker():
    now = 100.0
    health = engine_health.EngineHealth(clock=lambda: now)

    health.record_provider_failure("tavily", search_api.REASON_RATE_LIMIT)
    avail, reason = health.is_provider_available("tavily")
    assert not avail and reason == search_api.REASON_RATE_LIMIT

    now += 59.0
    avail, _ = health.is_provider_available("tavily")
    assert not avail

    now += 2.0
    avail, reason = health.is_provider_available("tavily")
    assert avail and reason == ""


# ─────────────────────────────────────────────────────────────
# 4. Lookup Fallback Wiring & Phase 4 Runtime Handshake
# ─────────────────────────────────────────────────────────────

class MockNetwork:
    """Mock network for testing DuckDuckGo fetcher."""

    def __init__(
        self,
        *,
        ddg_html_response: Any = None,
        ddg_lite_response: Any = None,
    ):
        self.ddg_html_response = ddg_html_response
        self.ddg_lite_response = ddg_lite_response
        self.requests: List[tuple[str, str]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        host = request.headers.get("host", request.url.host)
        url_str = str(request.url)
        self.requests.append((host, url_str))

        if host == "html.duckduckgo.com":
            return self._make_response(self.ddg_html_response)
        elif host == "lite.duckduckgo.com":
            return self._make_response(self.ddg_lite_response)
        elif "example.com" in host or "example.org" in host:
            return httpx.Response(
                200,
                headers={"content-type": "text/html"},
                content=b"<html><body><p>Excerpts from page</p></body></html>",
            )
        return httpx.Response(404)

    def _make_response(self, spec: Any) -> httpx.Response:
        if isinstance(spec, httpx.Response):
            return spec
        if isinstance(spec, Exception):
            raise spec
        if isinstance(spec, int):
            return httpx.Response(spec)
        if isinstance(spec, dict):
            return httpx.Response(200, json=spec)
        if isinstance(spec, str):
            return httpx.Response(200, headers={"content-type": "text/html"}, content=spec.encode())
        return httpx.Response(200, headers={"content-type": "text/html"}, content=b"<html><body>no results</body></html>")

    def fetcher(self) -> SafeFetcher:
        return SafeFetcher(
            transport=httpx.MockTransport(self.handler),
            resolver=lambda host: ["93.184.216.34"],
        )


class MockSearchApiRuntime:
    """Mock SearchApiRuntime for deterministic Phase 4 orchestration testing."""

    def __init__(
        self,
        *,
        is_busy: bool = False,
        prepare_map: Optional[dict[str, tuple[bool, str]]] = None,
        dispatch_map: Optional[dict[str, tuple[list[search.SearchResult], str]]] = None,
    ):
        self.is_busy = is_busy
        self.prepare_map = prepare_map or {}
        self.dispatch_map = dispatch_map or {}
        self.slot_acquired_count = 0
        self.prepared_calls: list[tuple[str, str, str]] = []
        self.dispatched_calls: list[tuple[str, str, str]] = []
        self.released_slots: list[Optional[str]] = []
        self._slot_owner: Optional[str] = None

    def acquire_slot(self, deadline: Optional[float] = None, *, request_id: Optional[str] = None):
        if self.is_busy or self._slot_owner is not None:
            return search_usage.ReservationResult(False, search_usage.REASON_BUSY, request_id or "")
        req_id = request_id or "slot-req-1"
        self._slot_owner = req_id
        self.slot_acquired_count += 1
        return search_usage.ReservationResult(True, "", req_id)

    def release_slot(self, request_id: Optional[str] = None):
        self.released_slots.append(request_id)
        if request_id is None or self._slot_owner == request_id:
            self._slot_owner = None

    def prepare(
        self,
        provider: str,
        query: str,
        key: str,
        deadline: Optional[float] = None,
        *,
        request_id: Optional[str] = None,
    ):
        self.prepared_calls.append((provider, query, request_id or ""))
        if provider in self.prepare_map:
            return self.prepare_map[provider]
        return True, ""

    def dispatch(
        self,
        provider: str,
        query: str,
        key: str,
        deadline: Optional[float] = None,
        *,
        request_id: Optional[str] = None,
    ):
        self.dispatched_calls.append((provider, query, request_id or ""))
        self._slot_owner = None
        if provider in self.dispatch_map:
            return self.dispatch_map[provider]
        return [], ""


def test_lookup_happy_path_ddg_no_fallback(monkeypatch):
    monkeypatch.setattr(settings, "TAVILY_API_KEY", "tvly-key")
    monkeypatch.setattr(settings, "BRAVE_API_KEY", "brave-key")

    mock_rt = MockSearchApiRuntime()
    ddg_html = (
        '<html><body><div class="result">'
        '<a class="result__a" href="https://news.example.com/item">Item 1</a>'
        '<a class="result__snippet">Snippet 1</a>'
        '</div></body></html>'
    )
    net = MockNetwork(ddg_html_response=ddg_html)
    res = lookup.run_lookup(
        "minecraft",
        "minecraft",
        fetcher=net.fetcher(),
        backend=None,
        deadline=time.monotonic() + 5.0,
        runtime=mock_rt,
    )

    assert res.outcome == OUTCOME_OK
    assert res.engine == search.ENGINE_HTML
    assert res.api_reason == ""
    # Runtime slot was never acquired since DDG succeeded
    assert mock_rt.slot_acquired_count == 0
    assert len(mock_rt.prepared_calls) == 0


def test_lookup_ddg_bot_falls_back_to_tavily(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "TAVILY_API_KEY", "tvly-key")
    monkeypatch.setattr(settings, "BRAVE_API_KEY", "brave-key")

    engine_health.reset_health()
    tracker = search_usage.SearchUsageTracker(db_path=str(tmp_path / "usage.sqlite3"))
    monkeypatch.setattr(search_usage, "get_tracker", lambda: tracker)

    mock_rt = MockSearchApiRuntime(
        dispatch_map={
            "tavily": (
                [
                    search.SearchResult(
                        "Minecraft Wiki",
                        "https://wiki.example.org/minecraft",
                        "Minecraft crafting guide.",
                    ),
                    search.SearchResult(
                        "Minecraft News",
                        "https://news.example.com/minecraft",
                        "Minecraft patch notes.",
                    ),
                ],
                "",
            )
        }
    )

    net = MockNetwork(
        ddg_html_response=202,
        ddg_lite_response=202,
    )
    res = lookup.run_lookup(
        "minecraft",
        "minecraft",
        fetcher=net.fetcher(),
        backend=None,
        deadline=time.monotonic() + 5.0,
        runtime=mock_rt,
        usage=tracker,
    )

    assert res.outcome == OUTCOME_OK
    assert res.engine == "tavily"
    assert res.api_reason == ""
    assert tracker.get_count("tavily") == 1
    assert tracker.get_count("brave") == 0
    assert "wiki.example.org" in res.domains or "news.example.com" in res.domains


def test_lookup_tavily_fails_falls_back_to_brave(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "TAVILY_API_KEY", "tvly-key")
    monkeypatch.setattr(settings, "BRAVE_API_KEY", "brave-key")

    engine_health.reset_health()
    tracker = search_usage.SearchUsageTracker(db_path=str(tmp_path / "usage.sqlite3"))
    monkeypatch.setattr(search_usage, "get_tracker", lambda: tracker)

    mock_rt = MockSearchApiRuntime(
        dispatch_map={
            "tavily": ([], search_api.REASON_RATE_LIMIT),
            "brave": (
                [
                    search.SearchResult(
                        "Brave Wiki",
                        "https://wiki.example.org/brave",
                        "Brave search results guide.",
                    ),
                ],
                "",
            ),
        }
    )

    net = MockNetwork(
        ddg_html_response=202,
        ddg_lite_response=202,
    )
    res = lookup.run_lookup(
        "minecraft",
        "minecraft",
        fetcher=net.fetcher(),
        backend=None,
        deadline=time.monotonic() + 5.0,
        runtime=mock_rt,
        usage=tracker,
    )

    assert res.outcome == OUTCOME_OK
    assert res.engine == "brave"
    assert res.api_reason == ""
    assert tracker.get_count("tavily") == 1
    assert tracker.get_count("brave") == 1


def test_lookup_all_fail_keeps_ddg_reason_and_records_last_api_reason(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "TAVILY_API_KEY", "tvly-key")
    monkeypatch.setattr(settings, "BRAVE_API_KEY", "brave-key")

    engine_health.reset_health()
    tracker = search_usage.SearchUsageTracker(db_path=str(tmp_path / "usage.sqlite3"))
    monkeypatch.setattr(search_usage, "get_tracker", lambda: tracker)

    mock_rt = MockSearchApiRuntime(
        dispatch_map={
            "tavily": ([], search_api.REASON_AUTH),
            "brave": ([], search_api.REASON_QUOTA),
        }
    )

    net = MockNetwork(
        ddg_html_response=202,
        ddg_lite_response=202,
    )
    res = lookup.run_lookup(
        "minecraft",
        "minecraft",
        fetcher=net.fetcher(),
        backend=None,
        deadline=time.monotonic() + 5.0,
        runtime=mock_rt,
        usage=tracker,
    )

    assert res.outcome == OUTCOME_ERROR
    assert res.reason == search.REASON_ANTI_BOT
    assert res.api_reason == search_api.REASON_QUOTA
    assert res.engine == search.ENGINE_LITE


def test_lookup_ddg_cooldown_jumps_straight_to_api(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "TAVILY_API_KEY", "tvly-key")
    monkeypatch.setattr(settings, "BRAVE_API_KEY", "brave-key")

    engine_health.reset_health()
    tracker = search_usage.SearchUsageTracker(db_path=str(tmp_path / "usage.sqlite3"))
    monkeypatch.setattr(search_usage, "get_tracker", lambda: tracker)

    mock_rt = MockSearchApiRuntime(
        dispatch_map={
            "tavily": (
                [
                    search.SearchResult(
                        "Minecraft Wiki",
                        "https://wiki.example.org/minecraft",
                        "Minecraft guide.",
                    ),
                ],
                "",
            )
        }
    )

    # 1st turn: triggers cooldown
    net1 = MockNetwork(
        ddg_html_response=202,
        ddg_lite_response=202,
    )
    res1 = lookup.run_lookup(
        "minecraft",
        "minecraft",
        fetcher=net1.fetcher(),
        backend=None,
        deadline=time.monotonic() + 5.0,
        runtime=mock_rt,
        usage=tracker,
    )
    assert res1.outcome == OUTCOME_OK

    # 2nd turn: DDG is cooled down, should NOT query DDG at all
    net2 = MockNetwork(
        ddg_html_response=httpx.Response(500),
        ddg_lite_response=httpx.Response(500),
    )
    res2 = lookup.run_lookup(
        "minecraft",
        "minecraft",
        fetcher=net2.fetcher(),
        backend=None,
        deadline=time.monotonic() + 5.0,
        runtime=mock_rt,
        usage=tracker,
    )
    assert res2.outcome == OUTCOME_OK
    assert res2.engine == "tavily"

    hosts2 = [h for h, _ in net2.requests]
    assert "html.duckduckgo.com" not in hosts2
    assert "lite.duckduckgo.com" not in hosts2


def test_lookup_ssrf_blocked_does_not_fall_back(monkeypatch):
    monkeypatch.setattr(settings, "TAVILY_API_KEY", "tvly-key")

    mock_rt = MockSearchApiRuntime()
    fetcher = SafeFetcher(
        transport=httpx.MockTransport(lambda req: httpx.Response(200)),
        resolver=lambda host: ["127.0.0.1"],
    )
    res = lookup.run_lookup(
        "query",
        "query",
        fetcher=fetcher,
        backend=None,
        deadline=time.monotonic() + 5.0,
        runtime=mock_rt,
    )
    assert res.outcome == OUTCOME_BLOCKED
    assert res.reason == "blocked"
    assert res.api_reason == ""
    assert mock_rt.slot_acquired_count == 0


def test_lookup_timeout_budget_boundary(monkeypatch):
    monkeypatch.setattr(settings, "TAVILY_API_KEY", "tvly-key")

    clock_val = 100.0
    mock_rt = MockSearchApiRuntime()

    class TimingFetcher:
        def fetch(self, url, deadline=None):
            nonlocal clock_val
            clock_val = 103.6
            from opencohost.core.web.fetch import WebFetchError
            raise WebFetchError("timeout")

    res = lookup.run_lookup(
        "query",
        "query",
        fetcher=TimingFetcher(),
        backend=None,
        deadline=104.0,
        clock=lambda: clock_val,
        runtime=mock_rt,
    )
    assert res.outcome == OUTCOME_TIMEOUT
    assert res.api_reason == ""
    assert mock_rt.slot_acquired_count == 0


def test_lookup_no_key_configured_behaves_identically(monkeypatch):
    monkeypatch.setattr(settings, "TAVILY_API_KEY", "")
    monkeypatch.setattr(settings, "BRAVE_API_KEY", "")

    mock_rt = MockSearchApiRuntime()
    net = MockNetwork(
        ddg_html_response=202,
        ddg_lite_response=202,
    )
    res = lookup.run_lookup(
        "query",
        "query",
        fetcher=net.fetcher(),
        backend=None,
        deadline=time.monotonic() + 5.0,
        runtime=mock_rt,
    )
    assert res.outcome == OUTCOME_ERROR
    assert res.reason == search.REASON_ANTI_BOT
    assert res.api_reason == ""
    assert res.engine == search.ENGINE_LITE
    assert mock_rt.slot_acquired_count == 0


# ─────────────────────────────────────────────────────────────
# 4.1 Phase 4 Specific Acceptance Tests (A5, A6, Full Chain, Parity)
# ─────────────────────────────────────────────────────────────

def test_lookup_a5_prepare_dns_failure_releases_reservation_and_consumes_zero(monkeypatch, tmp_path):
    """A5: Pre-dispatch DNS failure in PREPARE releases SQLite reservation, consuming ZERO calls."""
    monkeypatch.setattr(settings, "TAVILY_API_KEY", "tvly-key")
    monkeypatch.setattr(settings, "BRAVE_API_KEY", "brave-key")

    engine_health.reset_health()
    tracker = search_usage.SearchUsageTracker(db_path=str(tmp_path / "usage.sqlite3"))
    monkeypatch.setattr(search_usage, "get_tracker", lambda: tracker)

    mock_rt = MockSearchApiRuntime(
        prepare_map={
            "tavily": (False, search_api.REASON_DNS),
            "brave": (True, ""),
        },
        dispatch_map={
            "brave": (
                [
                    search.SearchResult(
                        "Brave Result",
                        "https://example.com/item",
                        "Snippet from Brave",
                    ),
                ],
                "",
            ),
        },
    )

    net = MockNetwork(ddg_html_response=202, ddg_lite_response=202)
    res = lookup.run_lookup(
        "query",
        "query",
        fetcher=net.fetcher(),
        backend=None,
        deadline=time.monotonic() + 5.0,
        runtime=mock_rt,
        usage=tracker,
    )

    assert res.outcome == OUTCOME_OK
    assert res.engine == "brave"
    # Tavily PREPARE failed: reservation RELEASED, consumed is ZERO!
    assert tracker.get_count("tavily") == 0
    # Brave succeeded: consumed is 1
    assert tracker.get_count("brave") == 1
    # Check that Tavily was prepared but never dispatched; Brave was prepared and dispatched
    assert any(c[0] == "tavily" for c in mock_rt.prepared_calls)
    assert not any(c[0] == "tavily" for c in mock_rt.dispatched_calls)
    assert any(c[0] == "brave" for c in mock_rt.dispatched_calls)


def test_lookup_a5_prepare_ssrf_blocked_releases_reservation_and_consumes_zero(monkeypatch, tmp_path):
    """A5: Pre-dispatch SSRF private IP blocked in PREPARE releases reservation, consuming ZERO calls."""
    monkeypatch.setattr(settings, "TAVILY_API_KEY", "tvly-key")
    monkeypatch.setattr(settings, "BRAVE_API_KEY", "brave-key")

    engine_health.reset_health()
    tracker = search_usage.SearchUsageTracker(db_path=str(tmp_path / "usage.sqlite3"))
    monkeypatch.setattr(search_usage, "get_tracker", lambda: tracker)

    mock_rt = MockSearchApiRuntime(
        prepare_map={
            "tavily": (False, search_api.REASON_BLOCKED),
            "brave": (True, ""),
        },
        dispatch_map={
            "brave": (
                [
                    search.SearchResult(
                        "Brave Result",
                        "https://example.com/b",
                        "Snippet from Brave",
                    ),
                ],
                "",
            ),
        },
    )

    net = MockNetwork(ddg_html_response=202, ddg_lite_response=202)
    res = lookup.run_lookup(
        "query",
        "query",
        fetcher=net.fetcher(),
        backend=None,
        deadline=time.monotonic() + 5.0,
        runtime=mock_rt,
        usage=tracker,
    )

    assert res.outcome == OUTCOME_OK
    assert res.engine == "brave"
    assert tracker.get_count("tavily") == 0
    assert tracker.get_count("brave") == 1


def test_lookup_a6_competing_request_returns_busy(monkeypatch, tmp_path):
    """A6: Competing request while runtime slot is occupied returns api_reason='busy'."""
    monkeypatch.setattr(settings, "TAVILY_API_KEY", "tvly-key")
    monkeypatch.setattr(settings, "BRAVE_API_KEY", "brave-key")

    engine_health.reset_health()
    tracker = search_usage.SearchUsageTracker(db_path=str(tmp_path / "usage.sqlite3"))
    monkeypatch.setattr(search_usage, "get_tracker", lambda: tracker)

    # Runtime is currently occupied / busy
    mock_rt = MockSearchApiRuntime(is_busy=True)

    net = MockNetwork(ddg_html_response=202, ddg_lite_response=202)
    res = lookup.run_lookup(
        "query",
        "query",
        fetcher=net.fetcher(),
        backend=None,
        deadline=time.monotonic() + 5.0,
        runtime=mock_rt,
        usage=tracker,
    )

    assert res.outcome == OUTCOME_ERROR
    assert res.reason == search.REASON_ANTI_BOT
    assert res.api_reason == "busy"
    assert res.engine == search.ENGINE_LITE
    # No quota consumed for any provider
    assert tracker.get_count("tavily") == 0
    assert tracker.get_count("brave") == 0


def test_lookup_full_fallback_chain_html_lite_tavily_brave(monkeypatch, tmp_path):
    """Full fallback chain: html -> lite -> tavily (fails 429) -> brave (succeeds)."""
    monkeypatch.setattr(settings, "TAVILY_API_KEY", "tvly-key")
    monkeypatch.setattr(settings, "BRAVE_API_KEY", "brave-key")

    engine_health.reset_health()
    tracker = search_usage.SearchUsageTracker(db_path=str(tmp_path / "usage.sqlite3"))
    monkeypatch.setattr(search_usage, "get_tracker", lambda: tracker)

    mock_rt = MockSearchApiRuntime(
        prepare_map={
            "tavily": (True, ""),
            "brave": (True, ""),
        },
        dispatch_map={
            "tavily": ([], search_api.REASON_RATE_LIMIT),
            "brave": (
                [
                    search.SearchResult(
                        "Brave Title",
                        "https://example.com/article",
                        "Brave Article Snippet",
                    ),
                ],
                "",
            ),
        },
    )

    net = MockNetwork(ddg_html_response=202, ddg_lite_response=202)
    res = lookup.run_lookup(
        "query",
        "query",
        fetcher=net.fetcher(),
        backend=None,
        deadline=time.monotonic() + 5.0,
        runtime=mock_rt,
        usage=tracker,
    )

    assert res.outcome == OUTCOME_OK
    assert res.engine == "brave"
    assert res.api_reason == ""
    assert res.results == 1
    assert "example.com" in res.domains

    # Tavily dispatched: 1 consumed call, rate_limit recorded in health
    assert tracker.get_count("tavily") == 1
    avail_tavily, reason_tavily = engine_health.get_health().is_provider_available("tavily")
    assert not avail_tavily
    assert reason_tavily == search_api.REASON_RATE_LIMIT

    # Brave dispatched: 1 consumed call
    assert tracker.get_count("brave") == 1


def test_lookup_zero_key_unconfigured_parity(monkeypatch, tmp_path, caplog):
    """When no API keys are configured, return values and log fields stay 100% byte-identical to baseline."""
    monkeypatch.setattr(settings, "TAVILY_API_KEY", "")
    monkeypatch.setattr(settings, "BRAVE_API_KEY", "")

    from opencohost.core.engine.llm_engine_web import _log

    caplog.set_level(logging.INFO, logger="OpenCohost")
    tracker = search_usage.SearchUsageTracker(db_path=str(tmp_path / "usage.sqlite3"))
    mock_rt = MockSearchApiRuntime()

    # 1. Successful DDG lookup
    ddg_html = (
        '<html><body><div class="result">'
        '<a class="result__a" href="https://example.com/one">One</a>'
        '<a class="result__snippet">Snippet</a>'
        '</div></body></html>'
    )
    net_ok = MockNetwork(ddg_html_response=ddg_html)
    res_ok = lookup.run_lookup(
        "question",
        "query",
        fetcher=net_ok.fetcher(),
        backend=None,
        deadline=time.monotonic() + 5.0,
        runtime=mock_rt,
        usage=tracker,
    )
    assert res_ok.outcome == OUTCOME_OK
    assert res_ok.engine == "html"
    assert res_ok.api_reason == ""
    assert mock_rt.slot_acquired_count == 0

    # Log line check
    _log("search", res_ok, subject="direct")
    log_line_ok = caplog.records[-1].getMessage()
    assert "api_reason" not in log_line_ok
    assert "outcome=ok" in log_line_ok
    assert "engine=html" in log_line_ok

    # 2. Blocked DDG lookup
    net_err = MockNetwork(ddg_html_response=202, ddg_lite_response=202)
    res_err = lookup.run_lookup(
        "question",
        "query",
        fetcher=net_err.fetcher(),
        backend=None,
        deadline=time.monotonic() + 5.0,
        runtime=mock_rt,
        usage=tracker,
    )
    assert res_err.outcome == OUTCOME_ERROR
    assert res_err.reason == search.REASON_ANTI_BOT
    assert res_err.engine == search.ENGINE_LITE
    assert res_err.api_reason == ""
    assert mock_rt.slot_acquired_count == 0

    _log("search", res_err, subject="direct")
    log_line_err = caplog.records[-1].getMessage()
    assert "api_reason" not in log_line_err
    assert "outcome=error" in log_line_err
    assert "reason=anti_bot" in log_line_err
    assert "engine=lite" in log_line_err


def test_lookup_search_fallback_off_parity(monkeypatch, tmp_path):
    """When OPENCOHOST_SEARCH_FALLBACK='off', fallback is disabled even if keys are set."""
    monkeypatch.setattr(settings, "TAVILY_API_KEY", "tvly-key")
    monkeypatch.setattr(settings, "BRAVE_API_KEY", "brave-key")
    monkeypatch.setattr(settings, "SEARCH_FALLBACK", ())

    tracker = search_usage.SearchUsageTracker(db_path=str(tmp_path / "usage.sqlite3"))
    mock_rt = MockSearchApiRuntime()

    net = MockNetwork(ddg_html_response=202, ddg_lite_response=202)
    res = lookup.run_lookup(
        "query",
        "query",
        fetcher=net.fetcher(),
        backend=None,
        deadline=time.monotonic() + 5.0,
        runtime=mock_rt,
        usage=tracker,
    )
    assert res.outcome == OUTCOME_ERROR
    assert res.reason == search.REASON_ANTI_BOT
    assert res.engine == search.ENGINE_LITE
    assert res.api_reason == ""
    assert mock_rt.slot_acquired_count == 0
    assert tracker.get_count("tavily") == 0


def test_lookup_integration_with_actual_runtime_worker_subprocess(monkeypatch, tmp_path):
    """End-to-end integration: run_lookup orchestrates with the actual search_api_runtime worker subprocess."""
    monkeypatch.setattr(settings, "TAVILY_API_KEY", "tvly-subprocess-key")
    monkeypatch.setattr(settings, "BRAVE_API_KEY", "brave-subprocess-key")

    engine_health.reset_health()
    tracker = search_usage.SearchUsageTracker(db_path=str(tmp_path / "usage.sqlite3"))
    monkeypatch.setattr(search_usage, "get_tracker", lambda: tracker)

    sample_tavily_resp = {
        "results": [
            {
                "title": "Subprocess Tavily Item",
                "url": "https://wiki.example.org/sub",
                "content": "Snippet from real child process worker",
            }
        ]
    }

    env = dict(os.environ)
    env["OPENCOHOST_WORKER_MOCK_DNS"] = "93.184.216.34"
    env["OPENCOHOST_WORKER_MOCK_HTTP"] = json.dumps(sample_tavily_resp)

    rt = search_api_runtime.SearchApiRuntime(env=env)
    try:
        net = MockNetwork(ddg_html_response=202, ddg_lite_response=202)
        res = lookup.run_lookup(
            "minecraft query",
            "minecraft query",
            fetcher=net.fetcher(),
            backend=None,
            deadline=time.monotonic() + 5.0,
            runtime=rt,
            usage=tracker,
        )

        assert res.outcome == OUTCOME_OK
        assert res.engine == "tavily"
        assert res.api_reason == ""
        assert tracker.get_count("tavily") == 1
        assert "wiki.example.org" in res.domains
    finally:
        rt.close()


# ─────────────────────────────────────────────────────────────
# 5. llm_engine_web log line format (T2/T5)
# ─────────────────────────────────────────────────────────────

def test_llm_engine_web_log_line_with_and_without_api_reason(caplog):
    from opencohost.core.engine.llm_engine_web import _log

    caplog.set_level(logging.INFO, logger="OpenCohost")

    # Without api_reason (empty string)
    res_no_api = WebLookupResult(
        outcome="ok",
        results=3,
        kept_links=2,
        chunks=2,
        kept_chunks=2,
        fetch_ms=100,
        filter_ms=20,
        reason="",
        engine="html",
        domains=("example.com",),
        api_reason="",
    )
    _log("search", res_no_api, subject="direct")
    line1 = caplog.records[-1].getMessage()
    assert "api_reason" not in line1
    assert line1 == (
        "[WEB] decision=search results=3 kept_links=2 chunks=2 kept_chunks=2 fetch_ms=100 filter_ms=20 "
        "outcome=ok reason=- subject=direct engine=html domains=example.com"
    )

    # With api_reason
    res_with_api = WebLookupResult(
        outcome="error",
        results=0,
        kept_links=0,
        chunks=0,
        kept_chunks=0,
        fetch_ms=200,
        filter_ms=0,
        reason="anti_bot",
        engine="lite",
        domains=(),
        api_reason="quota",
    )
    _log("search", res_with_api, subject="direct")
    line2 = caplog.records[-1].getMessage()
    assert "api_reason=quota" in line2


# ─────────────────────────────────────────────────────────────
# 6. Kira Messaging / Prompt Block (T6)
# ─────────────────────────────────────────────────────────────

def test_api_search_results_generate_valid_prompt_block():
    results = [
        search.SearchResult(
            title="Minecraft Crafting",
            url="https://minecraft.wiki/w/Crafting",
            snippet="Crafting recipes for tools.",
        ),
        search.SearchResult(
            title="Minecraft News",
            url="https://news.mojang.com/article",
            snippet="Update 1.21 released.",
        ),
    ]
    chunks = [
        ("minecraft.wiki", "Crafting table requires four wooden planks."),
    ]

    block = prompt.build_block(chunks, results)
    assert prompt.WEB_BLOCK_HEADER in block
    assert prompt.WEB_BLOCK_FOOTER in block
    assert "[minecraft]" in block
    assert "[mojang]" in block
    # Ensure provider name (tavily/brave) is NOT the label
    assert "[tavily]" not in block
    assert "[brave]" not in block


# ─────────────────────────────────────────────────────────────
# 5. Independent-review corrections F1-F5, F10
# ─────────────────────────────────────────────────────────────

_DDG_BOT = dict(ddg_html_response=202, ddg_lite_response=202)


def _fallback_lookup(monkeypatch, tmp_path, rt, tracker=None, deadline=None, clock=None):
    monkeypatch.setattr(settings, "TAVILY_API_KEY", "tvly-key")
    monkeypatch.setattr(settings, "BRAVE_API_KEY", "")
    tracker = tracker or search_usage.SearchUsageTracker(db_path=str(tmp_path / "u.sqlite3"))
    net = MockNetwork(**_DDG_BOT)
    kwargs = {}
    if clock is not None:
        kwargs["clock"] = clock
    res = lookup.run_lookup(
        "q", "q", fetcher=net.fetcher(), backend=None,
        deadline=deadline if deadline is not None else time.monotonic() + 5.0,
        runtime=rt, usage=tracker, **kwargs,
    )
    return res, tracker


def test_f1_empty_setting_disables_provider_even_with_env_key(monkeypatch):
    monkeypatch.setenv("OPENCOHOST_TAVILY_API_KEY", "tvly-env-should-be-ignored")
    monkeypatch.setenv("OPENCOHOST_BRAVE_API_KEY", "brave-env-should-be-ignored")
    monkeypatch.setattr(settings, "TAVILY_API_KEY", "")
    monkeypatch.setattr(settings, "BRAVE_API_KEY", "")
    assert lookup._get_api_key("tavily") == ""
    assert lookup._get_api_key("brave") == ""
    assert lookup._has_configured_api_keys() is False


def test_f2_failed_durable_mark_never_sends(monkeypatch, tmp_path):
    class FailingMark(search_usage.SearchUsageTracker):
        def mark_dispatched(self, request_id, **kw):
            return False

    tracker = FailingMark(db_path=str(tmp_path / "u.sqlite3"))
    rt = MockSearchApiRuntime()
    res, _ = _fallback_lookup(monkeypatch, tmp_path, rt, tracker)
    assert rt.dispatched_calls == []
    assert res.api_reason == search_usage.REASON_USAGE_STATE
    assert tracker.get_count("tavily") == 0
    # Reservation was released: the cap slot is free again.
    assert tracker.reserve("tavily", cap=1).ok is True
    assert rt._slot_owner is None


def test_f3_deadline_rechecked_before_mark_consumes_no_quota(monkeypatch, tmp_path):
    now = {"t": 100.0}

    class SlowPrepare(MockSearchApiRuntime):
        def prepare(self, *a, **kw):
            now["t"] = 109.5  # leaves < min budget (1.5s) before the 110.0 deadline
            return super().prepare(*a, **kw)

    rt = SlowPrepare()
    tracker = search_usage.SearchUsageTracker(db_path=str(tmp_path / "u.sqlite3"))
    res, _ = _fallback_lookup(
        monkeypatch, tmp_path, rt, tracker, deadline=110.0, clock=lambda: now["t"]
    )
    assert rt.dispatched_calls == []
    assert tracker.get_count("tavily") == 0
    assert res.api_reason == search_api.REASON_TIMEOUT
    assert tracker.reserve("tavily", cap=1).ok is True


def test_f4_runtime_class_owns_atomic_slot_acquire(monkeypatch):
    cls = search_api_runtime.SearchApiRuntime
    assert "acquire_slot" in vars(cls)
    assert cls.prepare.__module__ == search_api_runtime.__name__
    rt = cls()
    seen = []

    def fake_ensure():
        seen.append(rt._slot_owner)
        raise RuntimeError("stop before spawning a child")

    monkeypatch.setattr(rt, "_ensure_child", fake_ensure)
    assert rt.acquire_slot(None, request_id="a").ok is True
    assert rt.acquire_slot(None, request_id="b").ok is False
    assert rt.prepare("tavily", "q", "k", None, request_id="b") == (False, search_usage.REASON_BUSY)
    ok, reason = rt.prepare("tavily", "q", "k", None, request_id="a")
    assert ok is False
    # The slot was never free between acquire and prepare.
    assert seen == ["a"]


def test_f5_reserve_exception_releases_runtime_slot(monkeypatch, tmp_path):
    class Exploding(search_usage.SearchUsageTracker):
        def reserve(self, *a, **kw):
            raise OSError("disk gone")

    rt = MockSearchApiRuntime()
    tracker = Exploding(db_path=str(tmp_path / "u.sqlite3"))
    res, _ = _fallback_lookup(monkeypatch, tmp_path, rt, tracker)
    assert rt._slot_owner is None
    assert res.outcome == OUTCOME_ERROR  # honest failure, never raises


def test_f5_prepare_exception_releases_slot_and_reservation(monkeypatch, tmp_path):
    class Exploding(MockSearchApiRuntime):
        def prepare(self, *a, **kw):
            raise RuntimeError("boom")

    rt = Exploding()
    tracker = search_usage.SearchUsageTracker(db_path=str(tmp_path / "u.sqlite3"))
    _fallback_lookup(monkeypatch, tmp_path, rt, tracker)
    assert rt._slot_owner is None
    assert tracker.reserve("tavily", cap=1).ok is True


def test_f5_connect_oserror_returns_usage_state(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    tracker = search_usage.SearchUsageTracker(db_path=str(blocker / "sub" / "u.sqlite3"))
    res = tracker.reserve("tavily", cap=5)
    assert res.ok is False and res.reason == search_usage.REASON_USAGE_STATE
    assert tracker.mark_dispatched("x") is False
    assert tracker.release("x") is False


# ─────────────────────────────────────────────────────────────
# web-query-followup-and-retry T4: one sequential retry inside the turn budget
# ─────────────────────────────────────────────────────────────

_TAV_OK = [
    search.SearchResult("Wiki", "https://wiki.example.org/x", "Snippet about the thing."),
    search.SearchResult("News", "https://news.example.com/x", "Another snippet."),
]


class _SeqRuntime(MockSearchApiRuntime):
    """Dispatch answers come from a per-provider script of ``(results, reason, seconds)``;
    each dispatch advances the shared fake clock by ``seconds``."""

    def __init__(self, clock_box, script):
        super().__init__()
        self.clock_box = clock_box
        self.script = {k: list(v) for k, v in script.items()}
        self.prepare_deadlines: list[float] = []
        self.attempt_deadlines: list[float] = []  # SEND deadlines

    def prepare(self, provider, query, key, deadline=None, *, request_id=None):
        self.prepare_deadlines.append(deadline)
        return super().prepare(provider, query, key, deadline, request_id=request_id)

    def dispatch(self, provider, query, key, deadline=None, *, request_id=None):
        self.dispatched_calls.append((provider, query, request_id or ""))
        self.attempt_deadlines.append(deadline)
        self._slot_owner = None
        results, reason, seconds = self.script[provider].pop(0)
        self.clock_box["now"] += seconds
        return results, reason


def _retry_lookup(monkeypatch, tmp_path, script, *, keys=("tavily",), deadline=8.2, cap_used=None):
    monkeypatch.setattr(settings, "TAVILY_API_KEY", "tvly-key" if "tavily" in keys else "")
    monkeypatch.setattr(settings, "BRAVE_API_KEY", "brave-key" if "brave" in keys else "")
    clock_box = {"now": 0.0}
    tracker = search_usage.SearchUsageTracker(db_path=str(tmp_path / "u.sqlite3"))
    if cap_used:
        tracker._set_consumed("tavily", search_usage._current_month_str(), cap_used)
    health = engine_health.EngineHealth()
    monkeypatch.setattr(health, "is_ddg_cooled_down", lambda **kw: True)
    runtime = _SeqRuntime(clock_box, script)
    res = lookup.run_lookup(
        "q", "minecraft crafting",
        fetcher=None, backend=None, deadline=deadline,
        clock=lambda: clock_box["now"], health=health, usage=tracker, runtime=runtime,
    )
    return res, runtime, tracker


def test_attempt_cap_and_budget_defaults():
    assert settings.SEARCH_API_ATTEMPT_S == 3.5
    assert settings.WEB_FETCH_BUDGET_S == 8.5


def test_single_provider_timeout_is_retried_once_and_can_succeed(monkeypatch, tmp_path):
    res, rt, tracker = _retry_lookup(
        monkeypatch, tmp_path,
        {"tavily": [([], search_api.REASON_TIMEOUT, 3.5), (_TAV_OK, "", 2.0)]},
    )
    assert res.outcome == OUTCOME_OK and res.engine == "tavily"
    assert len(rt.dispatched_calls) == 2 and rt.slot_acquired_count == 2
    assert tracker.get_count("tavily") == 2
    assert rt.dispatched_calls[0][1] == rt.dispatched_calls[1][1] == "minecraft crafting"
    assert rt.attempt_deadlines == [3.5, 7.0]  # SEND: min(deadline, send start + 3.5)
    assert rt.prepare_deadlines == [8.2, 8.2]  # PREPARE (spawn, DNS) gets the overall deadline


def test_single_provider_double_timeout_stops_after_two_dispatches(monkeypatch, tmp_path):
    res, rt, _ = _retry_lookup(
        monkeypatch, tmp_path,
        {"tavily": [([], search_api.REASON_TIMEOUT, 3.5), ([], search_api.REASON_TIMEOUT, 3.5)]},
    )
    assert res.outcome != OUTCOME_OK and res.api_reason == search_api.REASON_TIMEOUT
    assert len(rt.dispatched_calls) == 2


@pytest.mark.parametrize(
    "reason",
    [search_api.REASON_AUTH, search_api.REASON_QUOTA, search_api.REASON_RATE_LIMIT, search_api.REASON_PARSE],
)
def test_permanent_reasons_are_never_retried(monkeypatch, tmp_path, reason):
    res, rt, _ = _retry_lookup(monkeypatch, tmp_path, {"tavily": [([], reason, 0.4)]})
    assert len(rt.dispatched_calls) == 1
    assert res.api_reason == reason


def test_transient_other_reason_is_retried(monkeypatch, tmp_path):
    res, rt, _ = _retry_lookup(
        monkeypatch, tmp_path,
        {"tavily": [([], search_api.REASON_OTHER, 0.5), (_TAV_OK, "", 1.0)]},
    )
    assert res.outcome == OUTCOME_OK and len(rt.dispatched_calls) == 2


def test_two_providers_do_not_retry_the_first(monkeypatch, tmp_path):
    res, rt, _ = _retry_lookup(
        monkeypatch, tmp_path,
        {"tavily": [([], search_api.REASON_TIMEOUT, 3.5)], "brave": [(_TAV_OK, "", 1.0)]},
        keys=("tavily", "brave"),
    )
    assert res.outcome == OUTCOME_OK and res.engine == "brave"
    assert [c[0] for c in rt.dispatched_calls] == ["tavily", "brave"]


def test_no_retry_when_less_than_the_minimum_budget_is_left(monkeypatch, tmp_path):
    res, rt, _ = _retry_lookup(
        monkeypatch, tmp_path,
        {"tavily": [([], search_api.REASON_TIMEOUT, 3.5)]},
        deadline=4.5,
    )
    assert len(rt.dispatched_calls) == 1
    assert res.api_reason == search_api.REASON_TIMEOUT


def test_local_cap_is_not_retried(monkeypatch, tmp_path):
    res, rt, _ = _retry_lookup(monkeypatch, tmp_path, {"tavily": []}, cap_used=900)
    assert rt.dispatched_calls == [] and rt.prepared_calls == []
    assert res.api_reason == search_usage.REASON_LOCAL_CAP


def test_keyless_config_never_touches_the_runtime(monkeypatch, tmp_path):
    res, rt, _ = _retry_lookup(monkeypatch, tmp_path, {}, keys=())
    assert rt.slot_acquired_count == 0 and res.api_reason == ""


def test_phase_box_names_the_stage_reached(monkeypatch, tmp_path):
    box: list = []
    monkeypatch.setattr(settings, "TAVILY_API_KEY", "tvly-key")
    clock_box = {"now": 0.0}
    health = engine_health.EngineHealth()
    monkeypatch.setattr(health, "is_ddg_cooled_down", lambda **kw: True)
    rt = _SeqRuntime(clock_box, {"tavily": [(_TAV_OK, "", 0.5)]})
    tracker = search_usage.SearchUsageTracker(db_path=str(tmp_path / "u.sqlite3"))

    seen: list = []

    class Boom:
        def fetch(self, url, deadline=None):
            seen.append(list(box))  # the stage while a page is being fetched
            raise RuntimeError("stop here")

    lookup.run_lookup(
        "q", "minecraft", fetcher=Boom(), backend=None, deadline=8.0,
        clock=lambda: clock_box["now"], health=health, usage=tracker, runtime=rt, phase=box,
    )
    assert seen and all(s_ == ["fetch_pages"] for s_ in seen)
    assert box == ["rank_chunks"]  # the lookup ended after ranking the snippets


@pytest.mark.parametrize("reason", ["http_500", "http_502", "http_503", "http_504"])
def test_http_5xx_reasons_are_retried(monkeypatch, tmp_path, reason):
    res, rt, _ = _retry_lookup(
        monkeypatch, tmp_path, {"tavily": [([], reason, 0.4), (_TAV_OK, "", 1.0)]}
    )
    assert res.outcome == OUTCOME_OK and len(rt.dispatched_calls) == 2


@pytest.mark.parametrize("reason", ["http_400", "http_404", "http_429"])
def test_http_4xx_reasons_are_not_retried(monkeypatch, tmp_path, reason):
    _, rt, _ = _retry_lookup(monkeypatch, tmp_path, {"tavily": [([], reason, 0.4)]})
    assert len(rt.dispatched_calls) == 1
