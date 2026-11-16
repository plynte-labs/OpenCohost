"""HTTPS page fetch behind an SSRF guard (web-fetch-laya-20261009).

  * ``https`` only, default port only, no credentials in the URL.
  * The host is resolved here and EVERY address must be public (no private,
    loopback, link-local, shared, multicast, reserved or unspecified ones,
    IPv4-mapped IPv6 included). The request then goes to that exact address,
    with the ``Host`` header and TLS SNI set to the name, so a second DNS
    answer cannot swap in another address (DNS rebinding) and the
    certificate is still checked against the name.
  * Redirects are followed by hand, at most ``MAX_REDIRECTS``, and every hop
    goes through the same checks.
  * ``timeout_s`` per request, an optional overall ``deadline``, a body cap
    of ``MAX_BYTES`` (checked on Content-Length and while streaming) and
    ``text/html`` (or XHTML) only.
  * A fixed User-Agent and no cookies.

Failures raise ``WebFetchError`` with a short code: ``blocked``, ``dns``,
``redirects``, ``too_large``, ``not_html``, ``http_status``, ``tls``,
``timeout`` or ``error``. Its ``reason`` is the closed log value for the
code (``http_<status>``, ``dns``, ``blocked``, ``tls`` or ``other``).
Nothing here logs the URL.
"""
from __future__ import annotations

import ipaddress
import socket
import ssl
import time
from dataclasses import dataclass
from typing import Callable, Iterable, Optional
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

USER_AGENT = "Mozilla/5.0 (compatible; OpenCohost/1.0; local co-host web lookup)"
TIMEOUT_S = 5.0
MAX_BYTES = 1_000_000
MAX_REDIRECTS = 3
_HTML_TYPES = ("text/html", "application/xhtml+xml")
_REDIRECTS = (301, 302, 303, 307, 308)


_PLAIN_REASONS = frozenset({"dns", "blocked", "tls"})


def default_accept_language(lang: Optional[str] = None) -> str:
    """Resolve the Accept-Language header for the active locale."""
    code = lang
    if not code:
        try:
            from opencohost.i18n import active as i18n_active

            bundle = i18n_active.get_active_bundle()
            code = bundle.code if bundle else "es"
        except Exception:
            code = "es"
    subtag = str(code or "es").split("-", 1)[0].strip().lower()
    return "en-US,en;q=0.9" if subtag == "en" else "es,en;q=0.8"


class WebFetchError(Exception):
    def __init__(self, code: str, status: Optional[int] = None) -> None:
        super().__init__(code)
        self.code = code
        self.status = status

    @property
    def reason(self) -> str:
        """Closed log value: ``http_<status>``, ``dns``, ``blocked``, ``tls``
        or ``other``."""
        if self.code == "http_status" and isinstance(self.status, int) and 100 <= self.status <= 599:
            return f"http_{self.status}"
        return self.code if self.code in _PLAIN_REASONS else "other"


def _is_tls_failure(exc: BaseException) -> bool:
    seen = 0
    while exc is not None and seen < 5:
        if isinstance(exc, ssl.SSLError):
            return True
        text = str(exc).upper()
        if "SSL" in text or "CERTIFICATE" in text:
            return True
        exc = exc.__cause__ or exc.__context__
        seen += 1
    return False


@dataclass(frozen=True)
class FetchedPage:
    domain: str
    html: str


def domain_of(url) -> str:
    """The lowercase host of ``url`` (no scheme, port, path or query), or ""."""
    try:
        return (urlsplit(str(url)).hostname or "").lower()
    except ValueError:
        return ""


def default_resolver(host: str) -> list[str]:
    infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    return [info[4][0] for info in infos]


def _public(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip.split("%", 1)[0])
    except ValueError:
        return False
    mapped = getattr(addr, "ipv4_mapped", None)
    if mapped is not None:
        addr = mapped
    return addr.is_global and not (
        addr.is_multicast or addr.is_reserved or addr.is_loopback or addr.is_link_local
        or addr.is_private or addr.is_unspecified
    )


class SafeFetcher:
    """Fetches one https page at a time; see the module docstring."""

    def __init__(
        self,
        *,
        transport: Optional[httpx.BaseTransport] = None,
        resolver: Callable[[str], Iterable[str]] = default_resolver,
        timeout_s: float = TIMEOUT_S,
        max_bytes: int = MAX_BYTES,
        max_redirects: int = MAX_REDIRECTS,
        clock: Callable[[], float] = time.monotonic,
        accept_language: Optional[str] = None,
    ) -> None:
        self._transport = transport
        self._resolver = resolver
        self.timeout_s = timeout_s
        self.max_bytes = max_bytes
        self.max_redirects = max_redirects
        self._clock = clock
        self._accept_language = accept_language

    def _check(self, url: str) -> tuple[str, str]:
        """``(host, ip)`` for an allowed URL; raises ``blocked``/``dns``."""
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
        try:
            ipaddress.ip_address(host)
            ips = [host]
        except ValueError:
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

    def fetch(self, url: str, *, deadline: Optional[float] = None) -> FetchedPage:
        current = url
        for _hop in range(self.max_redirects + 1):
            host, ip = self._check(current)
            timeout = self._remaining(deadline)
            parts = urlsplit(current)
            ip_host = f"[{ip}]" if ":" in ip else ip
            pinned = urlunsplit(("https", ip_host, parts.path or "/", parts.query, ""))
            try:
                with httpx.Client(
                    transport=self._transport, timeout=timeout, follow_redirects=False, trust_env=False
                ) as client:
                    request = client.build_request(
                        "GET",
                        pinned,
                        headers={
                            "Host": host,
                            "User-Agent": USER_AGENT,
                            "Accept": "text/html,application/xhtml+xml",
                            "Accept-Language": self._accept_language or default_accept_language(),
                        },
                        extensions={"sni_hostname": host},
                    )
                    response = client.send(request, stream=True)
                    try:
                        if response.status_code in _REDIRECTS:
                            location = response.headers.get("location")
                            if not location:
                                raise WebFetchError("http_status")
                            current = urljoin(current, location)
                            continue
                        return FetchedPage(host, self._read(response, deadline))
                    finally:
                        response.close()
            except WebFetchError:
                raise
            except httpx.TimeoutException:
                raise WebFetchError("timeout") from None
            except Exception as exc:
                raise WebFetchError("tls" if _is_tls_failure(exc) else "error") from None
        raise WebFetchError("redirects")

    def _read(self, response: httpx.Response, deadline: Optional[float]) -> str:
        if response.status_code != 200:
            raise WebFetchError("http_status", status=response.status_code)
        ctype = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if ctype not in _HTML_TYPES:
            raise WebFetchError("not_html")
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
            return bytes(body).decode(charset, errors="replace")
        except LookupError:
            return bytes(body).decode("utf-8", errors="replace")
