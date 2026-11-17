"""Reading ONE link the owner typed (runtime-fixes-20261110 T2, B).

Callers decide WHEN a link may be read (desktop-origin turns only, switches
on); this module only finds the link, fetches it through the unchanged
``SafeFetcher`` (https only, public addresses, no extra redirects, size, time
and content-type limits) and turns the page into a small, delimited,
sanitized text block.

  * Only the text of the CURRENT owner message is searched: callers pass that
    text and nothing else. The pasted-image description (vision model output)
    is cut out first, so a URL it contains is never read.
  * The first URL wins; the count of the others is reported so the prompt can
    say they were not read.
  * The page text is untrusted: control and format characters are dropped,
    anything that looks like one of our block markers is neutralised, and it
    is capped to the same order of size as the search block.
  * Nothing here logs; the engine logs host, counts and an enum code only.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Optional

from opencohost.core.web import extract
from opencohost.core.web.fetch import WebFetchError, domain_of
from opencohost.core.web.prompt import _label

# Same order of size as the search block (prompt.WEB_TEXT_MAX_CHARS = 1500).
LINK_TEXT_MAX_CHARS = 1200
# The whole read (all redirect hops) is bounded by this deadline.
LINK_BUDGET_S = 6.0

OUTCOME_OK = "ok"
OUTCOME_BLOCKED = "blocked"
OUTCOME_TIMEOUT = "timeout"
OUTCOME_NOT_HTML = "not_html"
OUTCOME_TOO_LARGE = "too_large"
OUTCOME_EMPTY = "empty"
OUTCOME_FAILED = "failed"

_URL = re.compile(r"https?://[^\s<>\"'`\[\]{}|\\^]+", re.IGNORECASE)
_TRAILING = ".,;:!?)]}'\"»”’"
# The pasted-image block is vision-model output, not the owner's words.
_PASTED_IMAGE = re.compile(
    r"\[(?:PASTED IMAGE|IMAGEN PEGADA)\b[^\]]*\].*?\[/(?:PASTED IMAGE|IMAGEN PEGADA)\]",
    re.IGNORECASE | re.DOTALL,
)
# Our own block markers (and the search block's) inside page text.
_MARKERS = re.compile(r"(?i)(?:end\s+)?(?:link\s+page|web\s+results)")


@dataclass(frozen=True)
class FoundLink:
    url: str
    extra: int = 0


@dataclass(frozen=True)
class LinkResult:
    outcome: str
    host: str = ""
    text: str = ""
    ms: int = 0

    @property
    def chars(self) -> int:
        return len(self.text)


def _owner_text(text) -> str:
    if not isinstance(text, str) or not text:
        return ""
    try:
        from opencohost.i18n import active as i18n_active

        block = i18n_active.pasted_image_block()
        head, _, tail = block.partition("{description}")
        if head.strip() and tail.strip():
            text = re.sub(re.escape(head) + r".*?" + re.escape(tail), " ", text, flags=re.DOTALL)
    except Exception:
        pass
    return _PASTED_IMAGE.sub(" ", text)


def find_link(text) -> Optional[FoundLink]:
    """The first http(s) URL of ``text`` and how many others follow, or None."""
    urls = []
    for match in _URL.finditer(_owner_text(text)):
        url = match.group(0).rstrip(_TRAILING)
        if url:
            urls.append(url)
    if not urls:
        return None
    return FoundLink(urls[0], len(urls) - 1)


def strip_links(text) -> str:
    """``text`` without any URL (what a search query is built from)."""
    if not isinstance(text, str):
        return ""
    return _URL.sub(" ", text)


def has_link(text) -> bool:
    return find_link(text) is not None


def _readable(html: str) -> str:
    paragraphs = extract.html_to_paragraphs(html)
    body = " ".join(p for p in paragraphs if p)
    body = extract.clean_text(_MARKERS.sub(" ", extract.clean_text(body)))
    if len(body) <= LINK_TEXT_MAX_CHARS:
        return body
    cut = body[:LINK_TEXT_MAX_CHARS]
    cut = cut.rsplit(" ", 1)[0] if " " in cut else cut
    return cut.rstrip(" ,;:")


def read_page(url: str, fetcher, *, deadline: Optional[float] = None, clock=time.monotonic) -> LinkResult:
    """One attempt, no retry. Never raises."""
    started = clock()
    host = domain_of(url)

    def done(outcome: str, text: str = "") -> LinkResult:
        return LinkResult(outcome, host=host, text=text, ms=max(0, int((clock() - started) * 1000)))

    try:
        page = fetcher.fetch(url, deadline=deadline)
    except WebFetchError as exc:
        code = exc.code
        if code == "timeout":
            return done(OUTCOME_TIMEOUT)
        if code == "not_html":
            return done(OUTCOME_NOT_HTML)
        if code == "too_large":
            return done(OUTCOME_TOO_LARGE)
        if code == "blocked":
            return done(OUTCOME_BLOCKED)
        return done(OUTCOME_FAILED)
    except Exception:
        return done(OUTCOME_FAILED)
    try:
        text = _readable(page.html)
    except Exception:
        return done(OUTCOME_FAILED)
    if not text:
        return done(OUTCOME_EMPTY)
    return done(OUTCOME_OK, text)


def build_block(host: str, text: str, extra: int = 0) -> str:
    """The delimited, labelled block for a page's text. The header (locale
    string) says the text is quoted material, never instructions."""
    from opencohost.i18n import active as i18n_active

    clean = extract.clean_text(_MARKERS.sub(" ", extract.clean_text(text)))
    block = i18n_active.link_page_block().format(site=_label(host), text=clean)
    if extra > 0:
        block += "\n" + i18n_active.link_extra_note()
    return block
