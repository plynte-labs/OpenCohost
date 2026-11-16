"""Keyed web search API clients for fallback (web-search-api-fallback-20261010 T1).

Supports Tavily and Brave Search APIs behind an SSRF guard:
  * Fixed host allowlist: api.tavily.com, api.search.brave.com
  * HTTPS only, port 443 only, no credentials in URL
  * Public-IP check and DNS pinning reused from SafeFetcher
  * No redirects, trust_env=False, 256 KB body cap
  * Returns SearchResult tuples (max 5, https only, deduplicated, capped text)
  * Closed reasons: auth, quota, rate_limit, http_<status>, parse, timeout,
    dns, blocked, tls, other.
"""
from __future__ import annotations

import json as _json
import logging
import socket
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Optional
from urllib.parse import quote_plus, urlsplit, urlunsplit

import httpx

from opencohost.core.web.extract import clean_text
from opencohost.core.web.fetch import (
    WebFetchError,
    _is_tls_failure,
    _public,
    default_resolver,
    domain_of,
)
from opencohost.core.web.search import (
    MAX_RESULTS,
    SNIPPET_MAX_CHARS,
    TITLE_MAX_CHARS,
    SearchResult,
)

TAVILY_HOST = "api.tavily.com"
BRAVE_HOST = "api.search.brave.com"
ALLOWED_HOSTS = frozenset({TAVILY_HOST, BRAVE_HOST})

TAVILY_ENDPOINT = "https://api.tavily.com/search"
BRAVE_BASE_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"

MAX_BYTES = 256_000
TIMEOUT_S = 5.0

REASON_AUTH = "auth"
REASON_QUOTA = "quota"
REASON_RATE_LIMIT = "rate_limit"
REASON_PARSE = "parse"
REASON_TIMEOUT = "timeout"
REASON_DNS = "dns"
REASON_BLOCKED = "blocked"
REASON_TLS = "tls"
REASON_OTHER = "other"


@dataclass(frozen=True)
class ApiResponse:
    status_code: int
    data: Any
    text: str
    headers: httpx.Headers


def _is_https_url(url: Any) -> bool:
    if not isinstance(url, str) or not url:
        return False
    try:
        parts = urlsplit(url)
        return parts.scheme == "https" and bool((parts.hostname or "").strip())
    except ValueError:
        return False


_SILENCED_LOGGERS = ("httpx", "httpcore")


class _TransportPrivacyFilter(logging.Filter):
    """Filter to suppress DEBUG/INFO records from transport loggers to prevent query string leakage."""

    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno >= logging.WARNING


_PRIVACY_FILTER = _TransportPrivacyFilter()


def _silence_transport_loggers() -> None:
    for name in _SILENCED_LOGGERS:
        log = logging.getLogger(name)
        log.setLevel(logging.WARNING)
        if _PRIVACY_FILTER not in log.filters:
            log.addFilter(_PRIVACY_FILTER)


_silence_transport_loggers()


class ApiSearchClient:
    """Safe HTTPS client restricted to allowed search API hosts."""

    def __init__(
        self,
        *,
        transport: Optional[httpx.BaseTransport] = None,
        resolver: Callable[[str], Iterable[str]] = default_resolver,
        timeout_s: float = TIMEOUT_S,
        max_bytes: int = MAX_BYTES,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        _silence_transport_loggers()
        self._transport = transport
        self._resolver = resolver
        self.timeout_s = timeout_s
        self.max_bytes = max_bytes
        self._clock = clock

    def _check(self, url: str) -> tuple[str, str]:
        """Validate URL against allowlist, scheme, port, and SSRF public IP check."""
        try:
            parts = urlsplit(url)
            port = parts.port
        except ValueError:
            raise WebFetchError("blocked") from None

        host = (parts.hostname or "").lower()
        if parts.scheme != "https" or not host or parts.username or parts.password:
            raise WebFetchError("blocked")
        if port not in (None, 443):
            raise WebFetchError("blocked")
        if host not in ALLOWED_HOSTS:
            raise WebFetchError("blocked")

        try:
            ips = [str(ip) for ip in self._resolver(host)]
        except Exception:
            raise WebFetchError("dns") from None

        if not ips:
            raise WebFetchError("dns")
        if not all(_public(ip) for ip in ips):
            raise WebFetchError("blocked")
        return host, ips[0]

    def _remaining(self, deadline: Optional[float]) -> float:
        if deadline is None:
            return self.timeout_s
        left = deadline - self._clock()
        if left <= 0:
            raise WebFetchError("timeout")
        return min(self.timeout_s, left)

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Optional[dict[str, str]] = None,
        json: Any = None,
        deadline: Optional[float] = None,
    ) -> ApiResponse:
        _silence_transport_loggers()
        host, ip = self._check(url)
        timeout = self._remaining(deadline)
        parts = urlsplit(url)
        ip_host = f"[{ip}]" if ":" in ip else ip
        pinned = urlunsplit(("https", ip_host, parts.path or "/", parts.query, ""))

        req_headers = {"Host": host, "Accept": "application/json"}
        if headers:
            req_headers.update(headers)

        try:
            with httpx.Client(
                transport=self._transport,
                timeout=timeout,
                follow_redirects=False,
                trust_env=False,
            ) as client:
                request = client.build_request(
                    method,
                    pinned,
                    headers=req_headers,
                    json=json,
                    extensions={"sni_hostname": host},
                )
                response = client.send(request, stream=True)
                try:
                    try:
                        declared = int(response.headers.get("content-length", "0") or 0)
                    except ValueError:
                        declared = 0
                    if declared > self.max_bytes:
                        raise WebFetchError("too_large")

                    body = bytearray()
                    for block in response.iter_bytes():
                        body.extend(block)
                        if len(body) > self.max_bytes:
                            raise WebFetchError("too_large")
                        if deadline is not None and self._clock() > deadline:
                            raise WebFetchError("timeout")

                    charset = response.charset_encoding or "utf-8"
                    try:
                        text = bytes(body).decode(charset, errors="replace")
                    except LookupError:
                        text = bytes(body).decode("utf-8", errors="replace")

                    ctype = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                    if response.status_code == 200:
                        if ctype != "application/json" and not ctype.endswith("+json"):
                            raise WebFetchError("parse")
                        try:
                            data = _json.loads(text)
                        except Exception:
                            raise WebFetchError("parse")
                    else:
                        try:
                            data = _json.loads(text)
                        except Exception:
                            data = None

                    return ApiResponse(response.status_code, data, text, response.headers)
                finally:
                    response.close()
        except WebFetchError:
            raise
        except httpx.TimeoutException:
            raise WebFetchError("timeout") from None
        except Exception as exc:
            raise WebFetchError("tls" if _is_tls_failure(exc) else "other") from None

    def get(self, url: str, *, headers: Optional[dict[str, str]] = None, deadline: Optional[float] = None) -> ApiResponse:
        return self.request("GET", url, headers=headers, deadline=deadline)

    def post(self, url: str, *, headers: Optional[dict[str, str]] = None, json: Any = None, deadline: Optional[float] = None) -> ApiResponse:
        return self.request("POST", url, headers=headers, json=json, deadline=deadline)


def _parse_tavily_results(raw_items: list[dict], limit: int = MAX_RESULTS) -> list[SearchResult]:
    out: list[SearchResult] = []
    seen: set[str] = set()
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        url = item.get("url", "")
        if not _is_https_url(url) or url in seen:
            continue
        seen.add(url)
        title = clean_text(str(item.get("title") or ""))[:TITLE_MAX_CHARS]
        snippet = clean_text(str(item.get("content") or ""))[:SNIPPET_MAX_CHARS]
        out.append(SearchResult(title=title, url=url, snippet=snippet))
        if len(out) >= limit:
            break
    return out


def _parse_brave_results(raw_items: list[dict], limit: int = MAX_RESULTS) -> list[SearchResult]:
    out: list[SearchResult] = []
    seen: set[str] = set()
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        url = item.get("url", "")
        if not _is_https_url(url) or url in seen:
            continue
        seen.add(url)
        title = clean_text(str(item.get("title") or ""))[:TITLE_MAX_CHARS]
        snippet = clean_text(str(item.get("description") or item.get("snippet") or ""))[:SNIPPET_MAX_CHARS]
        out.append(SearchResult(title=title, url=url, snippet=snippet))
        if len(out) >= limit:
            break
    return out


def tavily_search(
    query: str,
    key: str,
    deadline: Optional[float] = None,
    *,
    client: Optional[ApiSearchClient] = None,
    transport: Optional[httpx.BaseTransport] = None,
    resolver: Optional[Callable[[str], Iterable[str]]] = None,
    clock: Optional[Callable[[], float]] = None,
) -> tuple[list[SearchResult], str]:
    """Execute a Tavily search query. Returns (results, reason)."""
    if not key:
        return [], REASON_AUTH
    if not query or not query.strip():
        return [], ""

    if client is None:
        if transport is not None or resolver is not None or clock is not None:
            client = ApiSearchClient(
                transport=transport,
                resolver=resolver or default_resolver,
                clock=clock or time.monotonic,
            )
        else:
            client = ApiSearchClient()

    payload = {"query": query, "max_results": MAX_RESULTS, "search_depth": "basic"}
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}

    try:
        resp = client.request("POST", TAVILY_ENDPOINT, headers=headers, json=payload, deadline=deadline)
    except WebFetchError as exc:
        if exc.code == "timeout":
            return [], REASON_TIMEOUT
        if exc.code == "dns":
            return [], REASON_DNS
        if exc.code == "blocked":
            return [], REASON_BLOCKED
        if exc.code == "tls":
            return [], REASON_TLS
        if exc.code in ("too_large", "parse"):
            return [], REASON_PARSE
        return [], REASON_OTHER
    except Exception:
        return [], REASON_OTHER

    if resp.status_code == 200:
        if not isinstance(resp.data, dict):
            return [], REASON_PARSE
        raw_results = resp.data.get("results")
        if raw_results is None:
            return [], ""
        if not isinstance(raw_results, list):
            return [], REASON_PARSE
        return _parse_tavily_results(raw_results), ""

    if resp.status_code in (401, 403):
        return [], REASON_AUTH
    if resp.status_code == 429:
        return [], REASON_RATE_LIMIT
    if resp.status_code == 432:
        return [], REASON_QUOTA
    if 100 <= resp.status_code <= 599:
        return [], f"http_{resp.status_code}"
    return [], REASON_PARSE


def brave_search(
    query: str,
    key: str,
    deadline: Optional[float] = None,
    *,
    client: Optional[ApiSearchClient] = None,
    transport: Optional[httpx.BaseTransport] = None,
    resolver: Optional[Callable[[str], Iterable[str]]] = None,
    clock: Optional[Callable[[], float]] = None,
) -> tuple[list[SearchResult], str]:
    """Execute a Brave Web Search query. Returns (results, reason)."""
    if not key:
        return [], REASON_AUTH
    if not query or not query.strip():
        return [], ""

    if client is None:
        if transport is not None or resolver is not None or clock is not None:
            client = ApiSearchClient(
                transport=transport,
                resolver=resolver or default_resolver,
                clock=clock or time.monotonic,
            )
        else:
            client = ApiSearchClient()

    url = f"{BRAVE_BASE_ENDPOINT}?q={quote_plus(query)}&count=5"
    headers = {"X-Subscription-Token": key, "Accept": "application/json"}

    try:
        resp = client.request("GET", url, headers=headers, deadline=deadline)
    except WebFetchError as exc:
        if exc.code == "timeout":
            return [], REASON_TIMEOUT
        if exc.code == "dns":
            return [], REASON_DNS
        if exc.code == "blocked":
            return [], REASON_BLOCKED
        if exc.code == "tls":
            return [], REASON_TLS
        if exc.code in ("too_large", "parse"):
            return [], REASON_PARSE
        return [], REASON_OTHER
    except Exception:
        return [], REASON_OTHER

    if resp.status_code == 200:
        if not isinstance(resp.data, dict):
            return [], REASON_PARSE
        web = resp.data.get("web")
        if web is None:
            return [], ""
        if not isinstance(web, dict):
            return [], REASON_PARSE
        raw_results = web.get("results")
        if raw_results is None:
            return [], ""
        if not isinstance(raw_results, list):
            return [], REASON_PARSE
        return _parse_brave_results(raw_results), ""

    if resp.status_code in (401, 403):
        return [], REASON_AUTH
    if resp.status_code == 429:
        return [], REASON_RATE_LIMIT
    if resp.status_code == 402:
        return [], REASON_QUOTA
    if 100 <= resp.status_code <= 599:
        return [], f"http_{resp.status_code}"
    return [], REASON_PARSE
