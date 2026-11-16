"""Code-built source attribution for web turns (runtime-fixes-20261110 T5).

The sources of a reply are attached by code, never by the model: the kept
search results whose text went into the prompt (or the one link that was read)
become ``(host, url)`` pairs that ride the in-memory reply path to the desktop
UI. They are never persisted (memory drafts, episodic store, usage ledger,
exports) and never logged beyond a count and the bare hosts. The remote
gateway never receives them.

This module also removes a trailing "Fuente: ..." / "Source: ..." line the
model may still write on a web turn (it can be invented): the app shows the
real sources itself.
"""
from __future__ import annotations

import re
from typing import Iterable, Optional
from urllib.parse import urlsplit

# At most this many sources ride one reply.
MAX_SOURCES = 3
_MAX_URL_CHARS = 2048
_UNSAFE_URL_CHARS = re.compile(r"[\s\x00-\x1f\x7f]")


def bare_host(value) -> str:
    """The bare host of a host or URL: lower-case, no scheme, path, port,
    credentials or leading ``www.``. "" when there is none."""
    text = str(value or "").strip()
    if not text:
        return ""
    if "://" not in text:
        text = "//" + text
    try:
        host = urlsplit(text).hostname or ""
    except ValueError:
        return ""
    host = host.lower().rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    return host


def make_source(url) -> Optional[tuple]:
    """``(host, url)`` for an ``https://`` URL, else None. A URL with
    credentials, whitespace or control characters is refused."""
    if not isinstance(url, str):
        return None
    text = url.strip()
    if not text or len(text) > _MAX_URL_CHARS or _UNSAFE_URL_CHARS.search(text):
        return None
    try:
        parts = urlsplit(text)
        parts.port  # noqa: B018 - raises ValueError on a bad port
    except ValueError:
        return None
    if parts.scheme != "https" or not parts.hostname:
        return None
    if parts.username is not None or parts.password is not None:
        return None
    host = bare_host(text)
    if not host:
        return None
    return host, text


def collect(urls: Iterable, limit: int = MAX_SOURCES) -> tuple:
    """The first ``limit`` valid sources of ``urls``, in order, one per host."""
    out: list = []
    seen: set = set()
    for url in urls:
        source = make_source(url)
        if source is None or source[0] in seen:
            continue
        seen.add(source[0])
        out.append(source)
        if len(out) >= limit:
            break
    return tuple(out)


def merge(*groups, limit: int = MAX_SOURCES) -> tuple:
    """Several source groups in order, de-duplicated by host, capped."""
    out: list = []
    seen: set = set()
    for group in groups:
        for host, url in group or ():
            if host in seen:
                continue
            seen.add(host)
            out.append((host, url))
            if len(out) >= limit:
                return tuple(out)
    return tuple(out)


def as_dicts(sources) -> list:
    """The wire shape: ``[{"host": ..., "url": ...}]``."""
    return [{"host": host, "url": url} for host, url in (sources or ())]


def hosts_for_log(sources) -> str:
    """Bare hosts for a log line, "-" when there are none. Never a URL."""
    hosts = [re.sub(r"[^a-z0-9.\-]", "", host)[:80] for host, _ in (sources or ())]
    return ",".join(h for h in hosts if h) or "-"


# --- the model's own source line ------------------------------------------------

# "Fuente:", "Fuentes:", "Source:", "Sources:" at the start, optionally behind
# brackets, bullets or markdown emphasis ("**Fuente:** x", "[Fuente: x]").
_LABEL = re.compile(
    r"^[\s\[\(\*_>\-–—•#]*(?:fuentes?|sources?)[\s\]\)\*_]*:",
    re.IGNORECASE,
)
_SENTENCE_START = re.compile(r"(?:^|(?<=[.!?…])\s+)(?=[\[\(\*_]*(?:fuentes?|sources?)[\s\]\)\*_]*:)", re.IGNORECASE)
_SENTENCE_BREAK = re.compile(r"[.!?…][\]\)\*_\"'”]*\s+\S")
_LIST_ITEM = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+\S")


def is_source_line(text) -> bool:
    """True when ``text`` (a line or a sentence) is only a source attribution:
    it begins with a ``Fuente:`` / ``Fuentes:`` / ``Source:`` / ``Sources:``
    label. A sentence that merely contains the word ("la fuente de energia es")
    has no label colon and never matches."""
    return isinstance(text, str) and bool(_LABEL.match(text))


def carries_web_context(setup) -> bool:
    """True when this turn's prompt held web context (search results, a search
    failure note or a read link). Strict ``is True``: only a real flag counts."""
    return getattr(setup, "web_searched", False) is True or getattr(setup, "link_read", False) is True


def strip_trailing_source(text):
    """``text`` without a trailing source attribution (a final standalone
    line, its list items, or a final sentence beginning with the label).

    Only the end of the reply is touched. When stripping would leave nothing,
    the text is returned unchanged."""
    if not isinstance(text, str) or not text.strip():
        return text
    current = text
    for _ in range(3):  # "Fuente: a. Fuente: b." removes both, never more
        stripped = _strip_once(current)
        if stripped == current:
            break
        current = stripped
    return current


def _strip_once(text: str) -> str:
    lines = text.rstrip().split("\n")
    # A label line (and the list items under it) at the end.
    idx = len(lines) - 1
    while idx > 0 and (not lines[idx].strip() or _LIST_ITEM.match(lines[idx])):
        idx -= 1
    if is_source_line(lines[idx]):
        kept = "\n".join(lines[:idx]).rstrip()
        return kept if kept else text
    # A final sentence of the last line beginning with the label.
    last = lines[-1]
    starts = [m.start() for m in _SENTENCE_START.finditer(last)]
    if not starts:
        return text
    cut = starts[-1]
    tail = last[cut:].lstrip()
    if not is_source_line(tail) or _SENTENCE_BREAK.search(tail):
        return text
    kept = "\n".join([*lines[:-1], last[:cut]]).rstrip()
    return kept if kept else text
