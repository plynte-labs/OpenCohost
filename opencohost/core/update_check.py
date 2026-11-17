"""Once-per-launch "is there a newer release" check (update-notification-20261111).

Notify-and-link only: one unauthenticated ``GET`` to the GitHub releases list,
no download, no install. The request carries nothing but a generic
``User-Agent: OpenCohost/<version>``. Every failure is silent: the log line is a
fixed literal and never includes the response body. The result is cached for
the process lifetime, and a disabled setting makes no network call at all.
"""

from __future__ import annotations

import re
import threading
from typing import Optional

import httpx

import opencohost
from opencohost.config import update_setting
from opencohost.config.logger import get_logger

logger = get_logger()

RELEASES_URL = "https://api.github.com/repos/plynte-labs/opencohost/releases?per_page=10"
# Lowercase on purpose: GitHub owner/repo names are case-insensitive and html_url uses the
# canonical casing, so the prefix is compared case-insensitively.
_URL_PREFIX = "https://github.com/plynte-labs/opencohost/"
_TIMEOUT_S = 5.0

_VERSION_RE = re.compile(
    r"^v?(\d+)\.(\d+)\.(\d+)(?:[-.]?(a|alpha|b|beta|rc)[-.]?(\d+))?$", re.IGNORECASE
)
_PRE_RANK = {"a": 0, "alpha": 0, "b": 1, "beta": 1, "rc": 2}
_FINAL_RANK = 3

_lock = threading.Lock()
_cache: Optional[dict] = None


def parse_version(raw) -> Optional[tuple]:
    """Comparable key for ``0.5.0a1`` / ``v0.5.0-alpha.1`` style versions, else None."""
    if not isinstance(raw, str):
        return None
    m = _VERSION_RE.match(raw.strip())
    if not m:
        return None
    major, minor, patch, pre, num = m.groups()
    if pre is None:
        return (int(major), int(minor), int(patch), _FINAL_RANK, 0)
    return (int(major), int(minor), int(patch), _PRE_RANK[pre.lower()], int(num or 0))


def _make_client() -> httpx.Client:
    return httpx.Client(timeout=_TIMEOUT_S)


def _headers() -> dict:
    return {
        "Accept": "application/vnd.github+json",
        "User-Agent": f"OpenCohost/{opencohost.__version__}",
    }


def _empty() -> dict:
    return {"current": opencohost.__version__, "latest": None, "newer": False, "url": None}


def _pick(releases) -> dict:
    """Newest non-draft release by version; ``newer``/``url`` only when it beats the running one."""
    result = _empty()
    current = parse_version(opencohost.__version__)
    best_key = None
    best = None
    for rel in releases if isinstance(releases, list) else []:
        if not isinstance(rel, dict) or rel.get("draft") is True:
            continue
        tag = rel.get("tag_name")
        key = parse_version(tag)
        if key is None:
            continue
        if best_key is None or key > best_key:
            best_key, best = key, rel
    if best is None:
        return result
    result["latest"] = str(best["tag_name"]).lstrip("vV")
    url = best.get("html_url")
    if current is not None and best_key > current and isinstance(url, str) and url[: len(_URL_PREFIX)].lower() == _URL_PREFIX:
        result["newer"] = True
        result["url"] = url
    return result


def _fetch() -> dict:
    with _make_client() as client:
        resp = client.get(RELEASES_URL, headers=_headers())
        resp.raise_for_status()
        return _pick(resp.json())


def check() -> dict:
    """``{current, latest, newer, url}``; first call may do the request, later ones use the cache."""
    global _cache
    with _lock:
        if _cache is not None:
            return dict(_cache)
        if not update_setting.is_enabled():
            logger.info("[UPDATE_CHECK] outcome=disabled newer=0")
            return _empty()
        try:
            result = _fetch()
            outcome = "ok"
        except Exception:
            result = _empty()
            outcome = "error"
        logger.info("[UPDATE_CHECK] outcome=%s newer=%d", outcome, 1 if result["newer"] else 0)
        _cache = result
        return dict(result)


def reset_cache() -> None:
    global _cache
    with _lock:
        _cache = None
