"""Tests for web search API fallback (T1) and sensitive data redaction (T5).
Tasks T1 and T5 of the web-search API fallback work (2026-10-10).

Tests use httpx.MockTransport and mock resolvers only: NEVER hit the live network.
"""
from __future__ import annotations

import json
import logging
import ssl
from typing import Optional

import httpx
import pytest

from opencohost.config.logger import SensitiveDataFilter, log_formatter
from opencohost.core.web.fetch import WebFetchError
from opencohost.core.web.search import MAX_RESULTS, SearchResult
from opencohost.core.web.search_api import (
    ALLOWED_HOSTS,
    REASON_AUTH,
    REASON_QUOTA,
    REASON_RATE_LIMIT,
    ApiSearchClient,
    brave_search,
    tavily_search,
)

PUBLIC_IP = "93.184.216.34"
JSON_CTYPE = "application/json"


def _fake_resolver(table: Optional[dict[str, str | list[str]]] = None, default: str = PUBLIC_IP):
    table = dict(table or {})

    def resolve(host: str):
        val = table.get(host, default)
        return list(val) if isinstance(val, (list, tuple)) else [val]

    return resolve


def _json_response(data: dict | list | str, status: int = 200, ctype: str = JSON_CTYPE, headers: Optional[dict] = None) -> httpx.Response:
    content = json.dumps(data).encode("utf-8") if isinstance(data, (dict, list)) else data.encode("utf-8")
    h = {"content-type": ctype, **(headers or {})}
    return httpx.Response(status, headers=h, content=content)


# ==============================================================================
# Task T5: SensitiveDataFilter redaction tests
# ==============================================================================


def test_sensitive_data_filter_redacts_x_subscription_token_header():
    f = SensitiveDataFilter()
    record = logging.LogRecord("test", logging.INFO, "", 0, "Header X-Subscription-Token: bsv-secret-key-123", (), None)
    f.filter(record)
    assert "bsv-secret-key-123" not in record.msg
    assert "X-Subscription-Token=<redacted>" in record.msg


def test_sensitive_data_filter_redacts_x_subscription_token_lowercase_and_dict():
    f = SensitiveDataFilter()
    record = logging.LogRecord("test", logging.INFO, "", 0, "{'x-subscription-token': 'bsv-secret-456'}", (), None)
    f.filter(record)
    assert "bsv-secret-456" not in record.msg
    assert "x-subscription-token=<redacted>" in record.msg


def test_sensitive_data_filter_redacts_bare_tvly_key():
    f = SensitiveDataFilter()
    record = logging.LogRecord("test", logging.INFO, "", 0, "Query failed with key tvly-abcdef1234567890", (), None)
    f.filter(record)
    assert "tvly-abcdef1234567890" not in record.msg
    assert "<redacted>" in record.msg


def test_sensitive_data_filter_preserves_existing_patterns():
    f = SensitiveDataFilter()
    record = logging.LogRecord(
        "test", logging.INFO, "", 0,
        "Bearer ya29.secret_token and api_key=secret_api_val and X-Subscription-Token: brave_key",
        (), None,
    )
    f.filter(record)
    assert "secret_token" not in record.msg
    assert "secret_api_val" not in record.msg
    assert "brave_key" not in record.msg


# ==============================================================================
# Task T1: ApiSearchClient tests
# ==============================================================================


def test_api_search_client_allowed_hosts():
    assert ALLOWED_HOSTS == frozenset({"api.tavily.com", "api.search.brave.com"})


@pytest.mark.parametrize("url", [
    "https://example.com/search",
    "https://google.com/search",
    "https://tavily.com/search",
    "https://search.brave.com/search",
])
def test_api_search_client_host_outside_allowlist_refused(url):
    client = ApiSearchClient(
        transport=httpx.MockTransport(lambda r: _json_response({})),
        resolver=_fake_resolver(),
    )
    with pytest.raises(WebFetchError) as err:
        client.request("GET", url)
    assert err.value.code == "blocked"


@pytest.mark.parametrize("url", [
    "http://api.tavily.com/search",
    "ftp://api.tavily.com/search",
    "https://api.tavily.com:8443/search",
    "https://user:pass@api.tavily.com/search",
])
def test_api_search_client_non_https_and_non_default_port_refused(url):
    client = ApiSearchClient(
        transport=httpx.MockTransport(lambda r: _json_response({})),
        resolver=_fake_resolver(),
    )
    with pytest.raises(WebFetchError) as err:
        client.request("GET", url)
    assert err.value.code == "blocked"


@pytest.mark.parametrize("ip", [
    "127.0.0.1", "10.0.0.5", "192.168.1.1", "169.254.169.254", "::1", "fc00::1",
])
def test_api_search_client_rejects_non_public_ips(ip):
    client = ApiSearchClient(
        transport=httpx.MockTransport(lambda r: _json_response({})),
        resolver=_fake_resolver(default=ip),
    )
    with pytest.raises(WebFetchError) as err:
        client.request("GET", "https://api.tavily.com/search")
    assert err.value.code == "blocked"


def test_api_search_client_dns_failure():
    def failing_resolver(host):
        raise OSError("DNS lookup failed")

    client = ApiSearchClient(
        transport=httpx.MockTransport(lambda r: _json_response({})),
        resolver=failing_resolver,
    )
    with pytest.raises(WebFetchError) as err:
        client.request("GET", "https://api.tavily.com/search")
    assert err.value.code == "dns"


def test_api_search_client_pins_ip_and_sets_host_and_sni():
    seen_requests = []

    def handler(request: httpx.Request):
        seen_requests.append(request)
        return _json_response({"ok": True})

    client = ApiSearchClient(
        transport=httpx.MockTransport(handler),
        resolver=_fake_resolver(),
    )
    resp = client.request("POST", "https://api.tavily.com/search", json={"q": "test"})
    assert resp.status_code == 200
    assert resp.data == {"ok": True}
    assert len(seen_requests) == 1
    req = seen_requests[0]
    assert req.url.host == PUBLIC_IP
    assert req.headers["host"] == "api.tavily.com"
    assert req.extensions["sni_hostname"] == "api.tavily.com"


def test_api_search_client_body_cap_content_length():
    client = ApiSearchClient(
        transport=httpx.MockTransport(lambda r: _json_response("x" * 10, headers={"content-length": "300000"})),
        resolver=_fake_resolver(),
    )
    with pytest.raises(WebFetchError) as err:
        client.request("POST", "https://api.tavily.com/search")
    assert err.value.code == "too_large"


def test_api_search_client_body_cap_streaming():
    large_chunk = b"x" * 100_000

    def handler(request):
        def stream():
            for _ in range(4):
                yield large_chunk
        return httpx.Response(200, headers={"content-type": JSON_CTYPE}, content=stream())

    client = ApiSearchClient(
        transport=httpx.MockTransport(handler),
        resolver=_fake_resolver(),
    )
    with pytest.raises(WebFetchError) as err:
        client.request("POST", "https://api.tavily.com/search")
    assert err.value.code == "too_large"


def test_api_search_client_non_json_on_200_raises_parse():
    client = ApiSearchClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, headers={"content-type": "text/html"}, text="<html/>")),
        resolver=_fake_resolver(),
    )
    with pytest.raises(WebFetchError) as err:
        client.request("POST", "https://api.tavily.com/search")
    assert err.value.code == "parse"


def test_api_search_client_malformed_json_on_200_raises_parse():
    client = ApiSearchClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, headers={"content-type": JSON_CTYPE}, text="{not json")),
        resolver=_fake_resolver(),
    )
    with pytest.raises(WebFetchError) as err:
        client.request("POST", "https://api.tavily.com/search")
    assert err.value.code == "parse"


def test_api_search_client_timeout():
    def handler(request):
        raise httpx.ReadTimeout("timed out")

    client = ApiSearchClient(
        transport=httpx.MockTransport(handler),
        resolver=_fake_resolver(),
    )
    with pytest.raises(WebFetchError) as err:
        client.request("POST", "https://api.tavily.com/search")
    assert err.value.code == "timeout"


def test_api_search_client_tls_error():
    def handler(request):
        raise ssl.SSLError("certificate verify failed")

    client = ApiSearchClient(
        transport=httpx.MockTransport(handler),
        resolver=_fake_resolver(),
    )
    with pytest.raises(WebFetchError) as err:
        client.request("POST", "https://api.tavily.com/search")
    assert err.value.code == "tls"


# ==============================================================================
# Task T1: tavily_search tests
# ==============================================================================


def test_tavily_search_happy_path():
    captured_request = []

    def handler(request: httpx.Request):
        captured_request.append(request)
        return _json_response({
            "results": [
                {
                    "title": "Minecraft Wiki",
                    "url": "https://minecraft.wiki/w/Crafting",
                    "content": "Crafting is how most items are made.",
                },
                {
                    "title": "Minecraft Official",
                    "url": "https://minecraft.net/guide",
                    "content": "Official survival guide.",
                },
            ]
        })

    client = ApiSearchClient(transport=httpx.MockTransport(handler), resolver=_fake_resolver())
    results, reason = tavily_search("minecraft crafting", key="tvly-mock-key-123", client=client)

    assert reason == ""
    assert len(results) == 2
    assert isinstance(results[0], SearchResult)
    assert results[0].title == "Minecraft Wiki"
    assert results[0].url == "https://minecraft.wiki/w/Crafting"
    assert results[0].snippet == "Crafting is how most items are made."
    assert results[0].domain == "minecraft.wiki"

    assert len(captured_request) == 1
    req = captured_request[0]
    assert req.method == "POST"
    assert req.headers["authorization"] == "Bearer tvly-mock-key-123"
    body = json.loads(req.content.decode("utf-8"))
    assert body == {"query": "minecraft crafting", "max_results": 5, "search_depth": "basic"}


def test_tavily_search_drops_non_https_and_deduplicates():
    def handler(request):
        return _json_response({
            "results": [
                {"title": "Valid 1", "url": "https://a.com/1", "content": "S1"},
                {"title": "Insecure", "url": "http://insecure.com/2", "content": "S2"},
                {"title": "Valid 1 Duplicate", "url": "https://a.com/1", "content": "Duplicate"},
                {"title": "Ftp", "url": "ftp://ftp.com/3", "content": "S3"},
                {"title": "Valid 2", "url": "https://b.com/2", "content": "S4"},
            ]
        })

    client = ApiSearchClient(transport=httpx.MockTransport(handler), resolver=_fake_resolver())
    results, reason = tavily_search("query", key="tvly-k", client=client)
    assert reason == ""
    assert [r.url for r in results] == ["https://a.com/1", "https://b.com/2"]


def test_tavily_search_caps_at_max_results():
    items = [{"title": f"T{i}", "url": f"https://example.com/{i}", "content": f"C{i}"} for i in range(10)]
    client = ApiSearchClient(
        transport=httpx.MockTransport(lambda r: _json_response({"results": items})),
        resolver=_fake_resolver(),
    )
    results, reason = tavily_search("query", key="tvly-k", client=client)
    assert reason == ""
    assert len(results) == MAX_RESULTS


def test_tavily_search_truncates_title_and_snippet():
    long_title = "A" * 300
    long_snippet = "B" * 600
    client = ApiSearchClient(
        transport=httpx.MockTransport(lambda r: _json_response({
            "results": [{"title": long_title, "url": "https://example.com/1", "content": long_snippet}]
        })),
        resolver=_fake_resolver(),
    )
    results, reason = tavily_search("query", key="tvly-k", client=client)
    assert reason == ""
    assert len(results[0].title) == 200
    assert len(results[0].snippet) == 400


@pytest.mark.parametrize("status", [401, 403])
def test_tavily_search_closed_reason_auth(status):
    client = ApiSearchClient(
        transport=httpx.MockTransport(lambda r: _json_response({"detail": "unauthorized"}, status=status)),
        resolver=_fake_resolver(),
    )
    results, reason = tavily_search("query", key="tvly-bad", client=client)
    assert results == []
    assert reason == "auth"


def test_tavily_search_closed_reason_quota_432():
    client = ApiSearchClient(
        transport=httpx.MockTransport(lambda r: _json_response({"detail": "monthly request limit reached"}, status=432)),
        resolver=_fake_resolver(),
    )
    results, reason = tavily_search("query", key="tvly-k", client=client)
    assert results == []
    assert reason == "quota"


def test_tavily_search_closed_reason_rate_limit():
    client = ApiSearchClient(
        transport=httpx.MockTransport(lambda r: _json_response({"detail": "rate limit"}, status=429)),
        resolver=_fake_resolver(),
    )
    results, reason = tavily_search("query", key="tvly-k", client=client)
    assert results == []
    assert reason == "rate_limit"


def test_tavily_search_closed_reason_http_500():
    client = ApiSearchClient(
        transport=httpx.MockTransport(lambda r: _json_response({"error": "server error"}, status=500)),
        resolver=_fake_resolver(),
    )
    results, reason = tavily_search("query", key="tvly-k", client=client)
    assert results == []
    assert reason == "http_500"


def test_tavily_search_closed_reason_parse_invalid_json_or_shape():
    client = ApiSearchClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, headers={"content-type": JSON_CTYPE}, text="{bad json")),
        resolver=_fake_resolver(),
    )
    results, reason = tavily_search("query", key="tvly-k", client=client)
    assert results == []
    assert reason == "parse"


def test_tavily_search_closed_reason_timeout():
    def handler(request):
        raise httpx.ReadTimeout("deadline expired")

    client = ApiSearchClient(transport=httpx.MockTransport(handler), resolver=_fake_resolver())
    results, reason = tavily_search("query", key="tvly-k", client=client)
    assert results == []
    assert reason == "timeout"


def test_tavily_search_closed_reason_dns():
    def bad_resolver(host):
        raise OSError("resolution failure")

    client = ApiSearchClient(transport=httpx.MockTransport(lambda r: _json_response({})), resolver=bad_resolver)
    results, reason = tavily_search("query", key="tvly-k", client=client)
    assert results == []
    assert reason == "dns"


def test_tavily_search_closed_reason_tls():
    def handler(request):
        raise ssl.SSLError("tls handshake failed")

    client = ApiSearchClient(transport=httpx.MockTransport(handler), resolver=_fake_resolver())
    results, reason = tavily_search("query", key="tvly-k", client=client)
    assert results == []
    assert reason == "tls"


def test_tavily_search_closed_reason_blocked():
    client = ApiSearchClient(
        transport=httpx.MockTransport(lambda r: _json_response({})),
        resolver=_fake_resolver(default="127.0.0.1"),
    )
    results, reason = tavily_search("query", key="tvly-k", client=client)
    assert results == []
    assert reason == "blocked"


# ==============================================================================
# Task T1: brave_search tests
# ==============================================================================


def test_brave_search_happy_path():
    captured_request = []

    def handler(request: httpx.Request):
        captured_request.append(request)
        return _json_response({
            "web": {
                "results": [
                    {
                        "title": "Brave Search - Minecraft",
                        "url": "https://example.com/minecraft",
                        "description": "Information about Minecraft game.",
                    },
                    {
                        "title": "Redstone Guide",
                        "url": "https://example.com/redstone",
                        "description": "How to wire redstone.",
                    },
                ]
            }
        })

    client = ApiSearchClient(transport=httpx.MockTransport(handler), resolver=_fake_resolver())
    results, reason = brave_search("minecraft redstone", key="bsv-test-key", client=client)

    assert reason == ""
    assert len(results) == 2
    assert isinstance(results[0], SearchResult)
    assert results[0].title == "Brave Search - Minecraft"
    assert results[0].url == "https://example.com/minecraft"
    assert results[0].snippet == "Information about Minecraft game."
    assert results[0].domain == "example.com"

    assert len(captured_request) == 1
    req = captured_request[0]
    assert req.method == "GET"
    assert req.headers["x-subscription-token"] == "bsv-test-key"
    assert req.headers["accept"] == "application/json"
    assert "q=minecraft+redstone" in str(req.url)
    assert "count=5" in str(req.url)


def test_brave_search_drops_non_https_and_deduplicates():
    def handler(request):
        return _json_response({
            "web": {
                "results": [
                    {"title": "Valid 1", "url": "https://a.com/1", "description": "D1"},
                    {"title": "Insecure", "url": "http://insecure.com/2", "description": "D2"},
                    {"title": "Valid 1 Dup", "url": "https://a.com/1", "description": "Dup"},
                    {"title": "Valid 2", "url": "https://b.com/2", "description": "D3"},
                ]
            }
        })

    client = ApiSearchClient(transport=httpx.MockTransport(handler), resolver=_fake_resolver())
    results, reason = brave_search("query", key="bsv-key", client=client)
    assert reason == ""
    assert [r.url for r in results] == ["https://a.com/1", "https://b.com/2"]


def test_brave_search_caps_at_max_results():
    items = [{"title": f"T{i}", "url": f"https://example.com/{i}", "description": f"D{i}"} for i in range(10)]
    client = ApiSearchClient(
        transport=httpx.MockTransport(lambda r: _json_response({"web": {"results": items}})),
        resolver=_fake_resolver(),
    )
    results, reason = brave_search("query", key="bsv-key", client=client)
    assert reason == ""
    assert len(results) == MAX_RESULTS


def test_brave_search_truncates_title_and_snippet():
    long_title = "A" * 300
    long_snippet = "B" * 600
    client = ApiSearchClient(
        transport=httpx.MockTransport(lambda r: _json_response({
            "web": {"results": [{"title": long_title, "url": "https://example.com/1", "description": long_snippet}]}
        })),
        resolver=_fake_resolver(),
    )
    results, reason = brave_search("query", key="bsv-key", client=client)
    assert reason == ""
    assert len(results[0].title) == 200
    assert len(results[0].snippet) == 400


@pytest.mark.parametrize("status", [401, 403])
def test_brave_search_closed_reason_auth(status):
    client = ApiSearchClient(
        transport=httpx.MockTransport(lambda r: _json_response({"message": "Invalid token"}, status=status)),
        resolver=_fake_resolver(),
    )
    results, reason = brave_search("query", key="bsv-bad", client=client)
    assert results == []
    assert reason == "auth"


def test_brave_search_closed_reason_quota_402():
    client = ApiSearchClient(
        transport=httpx.MockTransport(lambda r: _json_response({"message": "Payment required"}, status=402)),
        resolver=_fake_resolver(),
    )
    results, reason = brave_search("query", key="bsv-k", client=client)
    assert results == []
    assert reason == "quota"


def test_brave_search_closed_reason_rate_limit():
    client = ApiSearchClient(
        transport=httpx.MockTransport(lambda r: _json_response({"message": "Rate limited"}, status=429)),
        resolver=_fake_resolver(),
    )
    results, reason = brave_search("query", key="bsv-k", client=client)
    assert results == []
    assert reason == "rate_limit"


def test_brave_search_closed_reason_http_500():
    client = ApiSearchClient(
        transport=httpx.MockTransport(lambda r: _json_response({"error": "internal error"}, status=500)),
        resolver=_fake_resolver(),
    )
    results, reason = brave_search("query", key="bsv-k", client=client)
    assert results == []
    assert reason == "http_500"


def test_brave_search_closed_reason_parse_invalid_json():
    client = ApiSearchClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, headers={"content-type": JSON_CTYPE}, text="{bad json")),
        resolver=_fake_resolver(),
    )
    results, reason = brave_search("query", key="bsv-k", client=client)
    assert results == []
    assert reason == "parse"


def test_brave_search_closed_reason_timeout():
    def handler(request):
        raise httpx.ReadTimeout("timeout")

    client = ApiSearchClient(transport=httpx.MockTransport(handler), resolver=_fake_resolver())
    results, reason = brave_search("query", key="bsv-k", client=client)
    assert results == []
    assert reason == "timeout"


def test_brave_search_closed_reason_dns():
    def bad_resolver(host):
        raise OSError("resolution failure")

    client = ApiSearchClient(transport=httpx.MockTransport(lambda r: _json_response({})), resolver=bad_resolver)
    results, reason = brave_search("query", key="bsv-k", client=client)
    assert results == []
    assert reason == "dns"


def test_brave_search_closed_reason_tls():
    def handler(request):
        raise ssl.SSLError("tls handshake failed")

    client = ApiSearchClient(transport=httpx.MockTransport(handler), resolver=_fake_resolver())
    results, reason = brave_search("query", key="bsv-k", client=client)
    assert results == []
    assert reason == "tls"


def test_brave_search_closed_reason_blocked():
    client = ApiSearchClient(
        transport=httpx.MockTransport(lambda r: _json_response({})),
        resolver=_fake_resolver(default="127.0.0.1"),
    )
    results, reason = brave_search("query", key="bsv-k", client=client)
    assert results == []
    assert reason == "blocked"


def test_no_secret_keys_in_results_or_filter():
    f = SensitiveDataFilter()
    tavily_key = "tvly-sample_secret_key_999"
    brave_key = "bsv-token-val-888"

    rec1 = logging.LogRecord("test", logging.INFO, "", 0, f"Using key {tavily_key} for search", (), None)
    f.filter(rec1)
    assert tavily_key not in rec1.msg

    rec2 = logging.LogRecord("test", logging.INFO, "", 0, f"X-Subscription-Token: {brave_key}", (), None)
    f.filter(rec2)
    assert brave_key not in rec2.msg


# ==============================================================================
# Phase 2: Classification Precedence (A4) and Log Privacy (A7)
# ==============================================================================


@pytest.mark.parametrize("status,body,expected_reason", [
    (429, {"error": "Rate limit exceeded"}, REASON_RATE_LIMIT),
    (429, {"detail": "Rate limit exceeded, please slow down"}, REASON_RATE_LIMIT),
    (401, {"detail": "Usage limit exceeded or unauthorized"}, REASON_AUTH),
    (403, {"detail": "Usage limit exceeded or forbidden"}, REASON_AUTH),
    (432, {"detail": "monthly request limit reached"}, REASON_QUOTA),
])
def test_tavily_search_classification_precedence_a4(status, body, expected_reason):
    client = ApiSearchClient(
        transport=httpx.MockTransport(lambda r: _json_response(body, status=status)),
        resolver=_fake_resolver(),
    )
    results, reason = tavily_search("query", key="tvly-mock-key", client=client)
    assert results == []
    assert reason == expected_reason


@pytest.mark.parametrize("status,body,expected_reason", [
    (429, {"error": "Rate limit exceeded"}, REASON_RATE_LIMIT),
    (429, {"message": "Rate limit exceeded, try again later"}, REASON_RATE_LIMIT),
    (401, {"message": "Monthly quota exhausted or invalid token"}, REASON_AUTH),
    (403, {"message": "Monthly quota exhausted or forbidden"}, REASON_AUTH),
    (402, {"message": "Payment required"}, REASON_QUOTA),
])
def test_brave_search_classification_precedence_a4(status, body, expected_reason):
    client = ApiSearchClient(
        transport=httpx.MockTransport(lambda r: _json_response(body, status=status)),
        resolver=_fake_resolver(),
    )
    results, reason = brave_search("query", key="bsv-mock-key", client=client)
    assert results == []
    assert reason == expected_reason


def test_tavily_search_200_snippets_with_limit_or_quota_words_succeed():
    data = {
        "results": [
            {
                "title": "Understanding the Credit Limit and Daily Quota",
                "url": "https://example.com/credit-limit",
                "content": "A guide on how usage limit and payment required work when limit reached.",
            },
            {
                "title": "Rate Limit Exceeded: Common API Patterns",
                "url": "https://example.com/rate-limit-guide",
                "content": "When you encounter rate limit exceeded or credit errors in web services.",
            },
        ]
    }
    client = ApiSearchClient(
        transport=httpx.MockTransport(lambda r: _json_response(data, status=200)),
        resolver=_fake_resolver(),
    )
    results, reason = tavily_search("what is credit limit", key="tvly-mock-key", client=client)
    assert reason == ""
    assert len(results) == 2
    assert results[0].title == "Understanding the Credit Limit and Daily Quota"
    assert "usage limit" in results[0].snippet


def test_brave_search_200_snippets_with_limit_or_quota_words_succeed():
    data = {
        "web": {
            "results": [
                {
                    "title": "Subscription Quota & Usage Limit Guide",
                    "url": "https://example.com/quota-guide",
                    "description": "Explaining monthly quota, rate limit exceeded, and credit thresholds.",
                }
            ]
        }
    }
    client = ApiSearchClient(
        transport=httpx.MockTransport(lambda r: _json_response(data, status=200)),
        resolver=_fake_resolver(),
    )
    results, reason = brave_search("subscription quota guide", key="bsv-mock-key", client=client)
    assert reason == ""
    assert len(results) == 1
    assert results[0].title == "Subscription Quota & Usage Limit Guide"
    assert "monthly quota" in results[0].snippet


def test_api_search_client_suppresses_httpx_and_httpcore_logs():
    records = []

    class CapturingHandler(logging.Handler):
        def emit(self, record):
            records.append(record)

    h = CapturingHandler()
    h.setLevel(logging.DEBUG)

    httpx_logger = logging.getLogger("httpx")
    httpcore_logger = logging.getLogger("httpcore")
    httpx_logger.addHandler(h)
    httpcore_logger.addHandler(h)

    httpx_logger.setLevel(logging.DEBUG)
    httpcore_logger.setLevel(logging.DEBUG)

    try:
        client = ApiSearchClient(
            transport=httpx.MockTransport(lambda r: _json_response({"web": {"results": []}})),
            resolver=_fake_resolver(),
        )
        client.get("https://api.search.brave.com/res/v1/web/search?q=very_secret_query_12345&count=5")

        logged_messages = [r.getMessage() for r in records]
        assert not any("very_secret_query_12345" in m for m in logged_messages)
        assert not any("?q=" in m for m in logged_messages)
        assert httpx_logger.level >= logging.WARNING
        assert httpcore_logger.level >= logging.WARNING
    finally:
        httpx_logger.removeHandler(h)
        httpcore_logger.removeHandler(h)


def test_sensitive_data_filter_sanitizes_exc_text():
    f = SensitiveDataFilter()
    rec = logging.LogRecord("test", logging.ERROR, "", 0, "An error occurred", (), None)
    rec.exc_text = (
        "Traceback (most recent call last):\n"
        "  File 'search.py', line 10, in execute\n"
        "    auth_header = 'Bearer tvly-super_secret_tavily_key'\n"
        "    token_header = 'X-Subscription-Token: bsv-secret_brave_key'\n"
        "ValueError: Request failed with key tvly-raw_key_in_msg\n"
    )
    f.filter(rec)
    assert rec.exc_text is not None
    assert "tvly-super_secret_tavily_key" not in rec.exc_text
    assert "bsv-secret_brave_key" not in rec.exc_text
    assert "tvly-raw_key_in_msg" not in rec.exc_text
    assert "<redacted>" in rec.exc_text


def test_sensitive_data_filter_sanitizes_exc_info_and_traceback():
    import sys

    f = SensitiveDataFilter()
    try:
        raise ValueError("Failed connecting to API with tvly-leak-key-1234 and X-Subscription-Token: bsv-token-5678")
    except Exception:
        exc_info = sys.exc_info()

    rec = logging.LogRecord("OpenCohost", logging.ERROR, "", 0, "Error during search", (), exc_info)
    f.filter(rec)

    formatted = log_formatter.format(rec)
    assert "tvly-leak-key-1234" not in formatted
    assert "bsv-token-5678" not in formatted
    assert "<redacted>" in formatted
    if rec.exc_text:
        assert "tvly-leak-key-1234" not in rec.exc_text
        assert "bsv-token-5678" not in rec.exc_text


def test_sensitive_data_filter_sanitizes_formatted_args():
    f = SensitiveDataFilter()
    rec = logging.LogRecord(
        "test", logging.INFO, "", 0,
        "Connecting with Tavily key %s and Brave %s",
        ("tvly-arg-secret-111", "X-Subscription-Token: bsv-arg-secret-222"),
        None,
    )
    f.filter(rec)
    assert "tvly-arg-secret-111" not in rec.msg
    assert "bsv-arg-secret-222" not in rec.msg
    assert "<redacted>" in rec.msg
    assert rec.args == ()



# ==============================================================================
# F7: closed status-code classification only, no free-text heuristic
# ==============================================================================


@pytest.mark.parametrize("body", [
    {"detail": "Usage limit exceeded"},
    {"detail": "You have exceeded your credit balance"},
    {"message": "Subscription quota reached"},
])
def test_f7_text_never_classifies_4xx_as_quota(body):
    for search_fn, key in ((tavily_search, "tvly-k"), (brave_search, "bsv-k")):
        client = ApiSearchClient(
            transport=httpx.MockTransport(lambda r: _json_response(body, status=400)),
            resolver=_fake_resolver(),
        )
        results, reason = search_fn("query", key=key, client=client)
        assert results == []
        assert reason == "http_400"


# ==============================================================================
# F9: redaction never mutates live exception objects; logs carry no abs paths
# ==============================================================================


def test_f9_filter_does_not_mutate_live_exception_args():
    import logging as _logging
    import sys as _sys

    secret = "api_key=tvly-live-secret-123"
    exc = RuntimeError(secret)
    try:
        raise exc
    except RuntimeError:
        record = _logging.LogRecord("t", _logging.ERROR, __file__, 1, "boom", (), _sys.exc_info())
    SensitiveDataFilter().filter(record)
    assert exc.args == (secret,)
    assert "tvly-live-secret-123" not in log_formatter.format(record)


def test_f9_ledger_failure_logs_basename_only(tmp_path, caplog):
    import logging as _logging
    from opencohost.core.web.search_usage import SearchUsageTracker

    blocker = tmp_path / "file"
    blocker.write_text("x")
    db = str(blocker / "private_dir" / "u.sqlite3")
    with caplog.at_level(_logging.WARNING):
        SearchUsageTracker(db_path=db).reserve("tavily", cap=5)
    assert caplog.records
    assert str(tmp_path) not in caplog.text
