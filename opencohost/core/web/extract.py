"""HTML to short text chunks with the stdlib parser (web-fetch-laya-20261009).

Drops script, style, nav, header, footer, aside and similar boilerplate, and
splits the rest into paragraphs at block tags. When the page has a ``<main>``
or ``<article>``, only the text inside it is kept. Paragraphs are packed into
chunks of at most ``max_words`` words, small enough that Laya reads a chunk
with the owner's question inside its 512-token window.

Everything here is untrusted web text: ``clean_text`` strips control and
format characters (zero-width, bidi overrides) and collapses whitespace.
Never raises on bad input.
"""
from __future__ import annotations

import re
import unicodedata
from html.parser import HTMLParser
from typing import Optional

CHUNK_MAX_WORDS = 120
CHUNK_MIN_WORDS = 15
MAX_CHUNKS = 3

_SKIP_TAGS = frozenset({
    "script", "style", "nav", "header", "footer", "aside", "noscript", "svg", "form",
    "template", "head", "iframe", "select", "button", "canvas", "object",
})
_MAIN_TAGS = frozenset({"main", "article"})
_BLOCK_TAGS = frozenset({
    "p", "div", "li", "ul", "ol", "br", "tr", "td", "th", "table", "section", "article", "main",
    "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre", "dd", "dt", "dl", "figcaption", "hr",
})
_SPACE = re.compile(r"\s+")


def clean_text(text) -> str:
    """Drop control (Cc) and format (Cf) characters, collapse whitespace."""
    if not isinstance(text, str):
        return ""
    out = []
    for ch in text:
        if ch in "\t\n\r\f\v":
            out.append(" ")
            continue
        if unicodedata.category(ch) in ("Cc", "Cf"):
            continue
        out.append(ch)
    return _SPACE.sub(" ", "".join(out)).strip()


class _TextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.skip = 0
        self.main = 0
        self.buf: list[str] = []
        self.paragraphs: list[tuple[str, bool]] = []

    def _flush(self) -> None:
        text = clean_text("".join(self.buf))
        self.buf = []
        if text:
            self.paragraphs.append((text, self.main > 0))

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_TAGS:
            self.skip += 1
            return
        if tag in _BLOCK_TAGS:
            self._flush()
        if tag in _MAIN_TAGS:
            self.main += 1

    def handle_startendtag(self, tag, attrs):
        if tag in _BLOCK_TAGS:
            self._flush()

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS:
            self.skip = max(0, self.skip - 1)
            return
        if tag in _BLOCK_TAGS:
            self._flush()
        if tag in _MAIN_TAGS:
            self.main = max(0, self.main - 1)

    def handle_data(self, data):
        if not self.skip:
            self.buf.append(data)


def html_to_paragraphs(html) -> list[str]:
    """Clean paragraphs of the page's readable text, in page order."""
    if not isinstance(html, str) or not html:
        return []
    parser = _TextParser()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        pass
    parser._flush()
    paragraphs = parser.paragraphs
    if any(in_main for _, in_main in paragraphs):
        return [text for text, in_main in paragraphs if in_main]
    return [text for text, _ in paragraphs]


def html_to_chunks(
    html,
    *,
    max_words: int = CHUNK_MAX_WORDS,
    min_words: int = CHUNK_MIN_WORDS,
    max_chunks: Optional[int] = MAX_CHUNKS,
) -> list[str]:
    """Paragraphs packed in order into chunks of ``min_words..max_words``
    words; a trailing chunk shorter than ``min_words`` is dropped."""
    chunks: list[str] = []
    current: list[str] = []
    for paragraph in html_to_paragraphs(html):
        for word in paragraph.split():
            current.append(word)
            if len(current) >= max_words:
                chunks.append(" ".join(current))
                current = []
                if max_chunks is not None and len(chunks) >= max_chunks:
                    return chunks
    if len(current) >= min_words:
        chunks.append(" ".join(current))
    return [c for c in chunks if len(c.split()) >= min_words][: max_chunks or None]
