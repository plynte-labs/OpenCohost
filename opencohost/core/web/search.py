"""DuckDuckGo search results (web-fetch-laya-20261009).

Neither endpoint needs a key. Only the query leaves the machine.

  * ``html``: ``html.duckduckgo.com/html/``, results are ``a.result__a``
    links with ``.result__snippet`` elements.
  * ``lite``: ``lite.duckduckgo.com/lite/``, the fallback
    (web-lookup-polish-20261010 T3). Results are table rows: an
    ``a.result-link`` in one row and a ``td.result-snippet`` in the next.

Result links may come wrapped as ``//duckduckgo.com/l/?uddg=<url>``; the
target is decoded, ads (``duckduckgo.com/y.js``) and DuckDuckGo's own links
are dropped, and only ``https`` results are kept, because the fetcher only
fetches ``https``.

``page_reason`` tells an anti-bot page (``anti_bot``) and a page that is not
a results page at all (``parse``) apart from a real, possibly empty, list of
results (``""``).
"""
from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import parse_qs, quote_plus, urlsplit

from opencohost.core.web.extract import clean_text
from opencohost.core.web.fetch import domain_of

SEARCH_ENDPOINT = "https://html.duckduckgo.com/html/?q="
LITE_ENDPOINT = "https://lite.duckduckgo.com/lite/?q="
ENGINE_HTML = "html"
ENGINE_LITE = "lite"
MAX_RESULTS = 5
TITLE_MAX_CHARS = 200
SNIPPET_MAX_CHARS = 400

REASON_ANTI_BOT = "anti_bot"
REASON_PARSE = "parse"
_ANTI_BOT_MARKERS = ("anomaly-modal", "anomaly.js", "challenge-form", "bots use duckduckgo too")
_NO_RESULTS_MARKERS = ("no-results", "no results", "sin resultados", "no se han encontrado resultados")
_RESULT_MARKERS = {ENGINE_HTML: ("result__a",), ENGINE_LITE: ("result-link",)}
_CLASSES = {ENGINE_HTML: ("result__a", "result__snippet"), ENGINE_LITE: ("result-link", "result-snippet")}


@dataclass(frozen=True)
class SearchResult:
    title: str
    url: str
    snippet: str

    @property
    def domain(self) -> str:
        return domain_of(self.url)


def search_url(query: str) -> str:
    return SEARCH_ENDPOINT + quote_plus(query or "")


def lite_search_url(query: str) -> str:
    return LITE_ENDPOINT + quote_plus(query or "")


def _target(href: str) -> str:
    """The real result URL, or "" for ads, DuckDuckGo links and non-https."""
    if not href:
        return ""
    url = "https:" + href if href.startswith("//") else href
    try:
        parts = urlsplit(url)
    except ValueError:
        return ""
    host = (parts.hostname or "").lower()
    if host == "duckduckgo.com" or host.endswith(".duckduckgo.com"):
        if parts.path == "/l/":
            wrapped = parse_qs(parts.query).get("uddg")
            return _target(wrapped[0]) if wrapped else ""
        return ""
    if parts.scheme != "https" or not host:
        return ""
    return url


def _classes(attrs) -> set[str]:
    for name, value in attrs:
        if name == "class" and value:
            return set(value.split())
    return set()


class _ResultParser(HTMLParser):
    def __init__(self, link_class: str, snippet_class: str) -> None:
        super().__init__(convert_charrefs=True)
        self.link_class = link_class
        self.snippet_class = snippet_class
        self.results: list[dict] = []
        self.field = None  # "title" | "snippet"
        self.depth = 0
        self.tag = None

    def handle_starttag(self, tag, attrs):
        if self.field is not None:
            if tag == self.tag:
                self.depth += 1
            return
        classes = _classes(attrs)
        if tag == "a" and self.link_class in classes:
            href = dict(attrs).get("href") or ""
            self.results.append({"url": _target(href), "title": [], "snippet": []})
            self.field, self.tag, self.depth = "title", tag, 0
        elif self.snippet_class in classes and self.results:
            self.field, self.tag, self.depth = "snippet", tag, 0

    def handle_endtag(self, tag):
        if self.field is None or tag != self.tag:
            return
        if self.depth:
            self.depth -= 1
            return
        self.field = None
        self.tag = None

    def handle_data(self, data):
        if self.field is not None and self.results:
            self.results[-1][self.field].append(data)


def _parse(html, limit: int, engine: str) -> list[SearchResult]:
    if not isinstance(html, str) or not html:
        return []
    parser = _ResultParser(*_CLASSES[engine])
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        pass
    out: list[SearchResult] = []
    seen: set[str] = set()
    for raw in parser.results:
        url = raw["url"]
        if not url or url in seen:
            continue
        seen.add(url)
        title = clean_text("".join(raw["title"]))[:TITLE_MAX_CHARS]
        snippet = clean_text("".join(raw["snippet"]))[:SNIPPET_MAX_CHARS]
        out.append(SearchResult(title, url, snippet))
        if len(out) >= limit:
            break
    return out


def parse_results(html, limit: int = MAX_RESULTS) -> list[SearchResult]:
    """Organic https results of the html endpoint in page order,
    deduplicated, at most ``limit``."""
    return _parse(html, limit, ENGINE_HTML)


def parse_lite_results(html, limit: int = MAX_RESULTS) -> list[SearchResult]:
    """Same as ``parse_results`` for the lite endpoint's table rows."""
    return _parse(html, limit, ENGINE_LITE)


def page_reason(html, engine: str) -> str:
    """``anti_bot`` for DuckDuckGo's bot challenge, ``parse`` for a page with
    neither result markup nor a no-results notice, else ``""``."""
    low = html.lower() if isinstance(html, str) else ""
    if any(marker in low for marker in _ANTI_BOT_MARKERS):
        return REASON_ANTI_BOT
    if any(marker in low for marker in _RESULT_MARKERS.get(engine, ())):
        return ""
    if any(marker in low for marker in _NO_RESULTS_MARKERS):
        return ""
    return REASON_PARSE
