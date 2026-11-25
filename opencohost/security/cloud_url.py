"""Cloud provider base-URL policy (api_key_egress_hardening_20261125).

A cloud API key is only ever attached to an ``https://`` URL, or to an
``http://`` URL whose host is loopback / private / link-local / ``*.local``
(local OpenAI-compatible servers). One helper, used by the provider router
and by the cloud chat client as defense in depth.
"""

from __future__ import annotations

import ipaddress
from typing import Optional, Tuple
from urllib.parse import urlsplit

_DEFAULT_PORTS = {"http": 80, "https": 443}


def _parse(url: object):
    if not isinstance(url, str):
        return None
    try:
        parts = urlsplit(url.strip())
        host = parts.hostname
        port = parts.port
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not host:
        return None
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        return None
    return parts.scheme, host.lower(), port


def _is_local_host(host: str) -> bool:
    if host == "localhost" or host.endswith(".localhost") or host.endswith(".local"):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_private or ip.is_link_local


def is_allowed_cloud_base_url(url: object) -> bool:
    parsed = _parse(url)
    if parsed is None:
        return False
    scheme, host, _port = parsed
    return scheme == "https" or _is_local_host(host)


def base_url_host_key(url: object) -> Optional[Tuple[str, int]]:
    """(lowercase host, effective port) or None when the URL is unparseable."""
    parsed = _parse(url)
    if parsed is None:
        return None
    scheme, host, port = parsed
    return host, port or _DEFAULT_PORTS[scheme]
