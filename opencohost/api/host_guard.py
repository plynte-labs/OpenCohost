"""Host-header guard for the main API app (release-050 D1, DNS rebinding).

A page on an attacker's domain can resolve that domain to 127.0.0.1 and send
same-origin requests to the local API. The browser then sends the attacker's
name in ``Host``, and CORS does not stop it because the request is same-origin
from the page's point of view. Refusing any Host that is not a loopback name
closes that path.

Applied to the main API app only (opencohost/api/main.py::create_app). The
remote voice gateway is a separate ASGI app on its own loopback port and does
not install this middleware, so its Cloudflare Tunnel hostname keeps working.
"""

import logging
import re

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from opencohost.config import settings

logger = logging.getLogger("opencohost.api.host_guard")

_PORT_RE = re.compile(r"[0-9]{1,5}")
_REFUSED_BODY = {"error": "invalid_host"}


def is_loopback_host(value, allowed) -> bool:
    """True when a Host header names an allowed loopback host, with an optional port.

    ``allowed`` holds lowercase names without ports (``127.0.0.1``, ``localhost``,
    ``[::1]``). Matching is case-insensitive. Anything else (other names, a
    non-numeric or out-of-range port, a trailing dot, junk after an IPv6 bracket)
    is refused.
    """
    if not value:
        return False
    host = value.lower()
    if host.startswith("["):
        end = host.find("]")
        if end == -1:
            return False
        name, rest = host[: end + 1], host[end + 1 :]
    else:
        name, sep, port = host.partition(":")
        rest = f":{port}" if sep else ""
    if name not in allowed:
        return False
    if not rest:
        return True
    if not rest.startswith(":"):
        return False
    port = rest[1:]
    return _PORT_RE.fullmatch(port) is not None and 1 <= int(port) <= 65535


class HostGuardMiddleware:
    """Pure ASGI middleware: refuses HTTP with a fixed 400 and WebSocket with a 1008 close.

    The allowlist is read from ``settings.API_ALLOWED_HOST_NAMES`` on each
    request, so the test suite can override it. Requests with zero or several
    Host headers are refused as well. Lifespan and other scopes pass through.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        hosts = [value for name, value in scope.get("headers", []) if name == b"host"]
        allowed = settings.API_ALLOWED_HOST_NAMES
        if len(hosts) == 1 and is_loopback_host(hosts[0].decode("latin-1"), allowed):
            await self.app(scope, receive, send)
            return
        logger.warning("Refused a request whose Host header is not a loopback name")
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        response = JSONResponse(_REFUSED_BODY, status_code=400)
        await response(scope, receive, send)
