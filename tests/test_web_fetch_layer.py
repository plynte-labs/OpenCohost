"""web-fetch-laya-20261009 T1: search parsing, safe fetch and HTML chunks.

No test here touches the network: every request goes through an
``httpx.MockTransport`` and every host through a fake resolver.
"""
from __future__ import annotations

import httpx
import pytest

from opencohost.core.web import extract, fetch, search
from opencohost.core.web.fetch import SafeFetcher, WebFetchError

PUBLIC_IP = "93.184.216.34"
HTML = "text/html; charset=utf-8"


def _resolver(table=None, default=PUBLIC_IP):
    table = dict(table or {})
    calls = []

    def resolve(host):
        calls.append(host)
        value = table.get(host, default)
        return list(value) if isinstance(value, (list, tuple)) else [value]

    resolve.calls = calls
    return resolve


def _fetcher(handler, resolver=None, **kwargs):
    return SafeFetcher(transport=httpx.MockTransport(handler), resolver=resolver or _resolver(), **kwargs)


def _page(body="<p>hola</p>", ctype=HTML, status=200, headers=None):
    h = {"content-type": ctype, **(headers or {})}
    return httpx.Response(status, headers=h, content=body.encode("utf-8") if isinstance(body, str) else body)


# --- search -----------------------------------------------------------------

DDG = """
<html><body>
<div class="result results_links result--ad">
  <h2 class="result__title"><a class="result__a" href="https://duckduckgo.com/y.js?ad_domain=ads.com&u3=x">Buy now</a></h2>
  <a class="result__snippet" href="#">An ad.</a>
</div>
<div class="result results_links">
  <h2 class="result__title"><a rel="nofollow" class="result__a"
     href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fnews.example.com%2Felden%3Fid%3D7&amp;rut=abc">Elden <b>Ring</b> news</a></h2>
  <a class="result__snippet" href="//duckduckgo.com/l/?uddg=x">The new <b>DLC</b> ships in &amp; around June.</a>
</div>
<div class="result results_links">
  <h2 class="result__title"><a class="result__a" href="http://insecure.example.org/page">Plain http</a></h2>
  <a class="result__snippet" href="#">Not https.</a>
</div>
<div class="result results_links">
  <h2 class="result__title"><a class="result__a" href="https://wiki.example.net/Elden_Ring">Elden Ring - Wiki</a></h2>
  <div class="result__snippet">An action RPG.</div>
</div>
</body></html>
"""


def test_search_url_quotes_the_query_only():
    url = search.search_url("noticias de elden ring & más")
    assert url.startswith("https://html.duckduckgo.com/html/?q=")
    assert "noticias+de+elden+ring+%26+m%C3%A1s" in url


def test_parse_results_decodes_uddg_skips_ads_and_non_https():
    results = search.parse_results(DDG)
    assert [r.url for r in results] == [
        "https://news.example.com/elden?id=7",
        "https://wiki.example.net/Elden_Ring",
    ]
    assert results[0].title == "Elden Ring news"
    assert results[0].snippet == "The new DLC ships in & around June."
    assert results[0].domain == "news.example.com"
    assert results[1].snippet == "An action RPG."


def test_parse_results_caps_and_dedupes():
    block = '<div class="result"><a class="result__a" href="https://a.example.com/{i}">T{i}</a></div>'
    html = "".join(block.format(i=i) for i in range(20)) + block.format(i=0)
    results = search.parse_results(html, limit=5)
    assert len(results) == 5
    assert len({r.url for r in results}) == 5


def test_parse_results_garbage_is_empty():
    assert search.parse_results("") == []
    assert search.parse_results("<html><p>captcha</p>") == []


# --- safe fetch ---------------------------------------------------------------


def test_fetch_pins_the_resolved_ip_with_host_header_and_sni():
    seen = []

    def handler(request):
        seen.append(request)
        return _page("<p>ok</p>")

    page = _fetcher(handler).fetch("https://example.com/a?b=1")
    assert page.html == "<p>ok</p>"
    assert page.domain == "example.com"
    req = seen[0]
    assert req.url.host == PUBLIC_IP
    assert req.url.path == "/a" and req.url.query == b"b=1"
    assert req.headers["host"] == "example.com"
    assert req.extensions["sni_hostname"] == "example.com"
    assert req.headers["user-agent"] == fetch.USER_AGENT


@pytest.mark.parametrize("url", ["http://example.com/", "ftp://example.com/", "file:///etc/passwd", "example.com"])
def test_fetch_rejects_non_https(url):
    with pytest.raises(WebFetchError) as err:
        _fetcher(lambda r: _page()).fetch(url)
    assert err.value.code == "blocked"


@pytest.mark.parametrize("url", ["https://user:pw@example.com/", "https://example.com:8443/"])
def test_fetch_rejects_credentials_and_non_default_ports(url):
    with pytest.raises(WebFetchError) as err:
        _fetcher(lambda r: _page()).fetch(url)
    assert err.value.code == "blocked"


@pytest.mark.parametrize(
    "ip",
    [
        "127.0.0.1", "10.0.0.5", "172.16.3.4", "192.168.1.10", "169.254.169.254", "0.0.0.0",
        "100.64.0.1", "224.0.0.1", "240.0.0.1", "255.255.255.255", "::1", "fe80::1", "fc00::1",
        "::ffff:127.0.0.1", "::",
    ],
)
def test_fetch_rejects_non_public_ips(ip):
    hit = []
    with pytest.raises(WebFetchError) as err:
        _fetcher(lambda r: hit.append(r) or _page(), _resolver(default=ip)).fetch("https://evil.example/")
    assert err.value.code == "blocked"
    assert hit == []


def test_fetch_rejects_when_any_resolved_ip_is_private():
    resolver = _resolver(default=[PUBLIC_IP, "127.0.0.1"])
    with pytest.raises(WebFetchError) as err:
        _fetcher(lambda r: _page(), resolver).fetch("https://mixed.example/")
    assert err.value.code == "blocked"


@pytest.mark.parametrize("host", ["127.0.0.1", "[::1]", "localhost"])
def test_fetch_rejects_literal_and_local_hosts(host):
    resolver = _resolver({"localhost": "127.0.0.1"})
    with pytest.raises(WebFetchError) as err:
        _fetcher(lambda r: _page(), resolver).fetch(f"https://{host}/")
    assert err.value.code == "blocked"


def test_fetch_dns_failure_is_error():
    def resolve(host):
        raise OSError("nxdomain")

    with pytest.raises(WebFetchError) as err:
        SafeFetcher(transport=httpx.MockTransport(lambda r: _page()), resolver=resolve).fetch("https://x.example/")
    assert err.value.code == "dns"


def test_fetch_follows_redirects_and_rechecks_each_hop():
    resolver = _resolver({"internal.example": "10.0.0.1"})
    hops = []

    def handler(request):
        hops.append(request.headers["host"])
        if request.headers["host"] == "a.example":
            return httpx.Response(302, headers={"location": "https://b.example/next"})
        if request.headers["host"] == "b.example":
            return httpx.Response(301, headers={"location": "https://internal.example/admin"})
        return _page()

    with pytest.raises(WebFetchError) as err:
        _fetcher(handler, resolver).fetch("https://a.example/")
    assert err.value.code == "blocked"
    assert hops == ["a.example", "b.example"]


def test_fetch_redirect_to_http_is_blocked():
    def handler(request):
        return httpx.Response(302, headers={"location": "http://a.example/plain"})

    with pytest.raises(WebFetchError) as err:
        _fetcher(handler).fetch("https://a.example/")
    assert err.value.code == "blocked"


def test_fetch_relative_redirect_is_followed():
    def handler(request):
        if request.url.path == "/old":
            return httpx.Response(302, headers={"location": "/new"})
        return _page("<p>new</p>")

    page = _fetcher(handler).fetch("https://a.example/old")
    assert page.html == "<p>new</p>" and page.domain == "a.example"


def test_fetch_redirect_cap():
    count = []

    def handler(request):
        count.append(1)
        return httpx.Response(302, headers={"location": f"https://a.example/{len(count)}"})

    with pytest.raises(WebFetchError) as err:
        _fetcher(handler).fetch("https://a.example/0")
    assert err.value.code == "redirects"
    assert len(count) == fetch.MAX_REDIRECTS + 1


def test_fetch_size_cap():
    body = b"<p>" + b"a" * (fetch.MAX_BYTES + 10) + b"</p>"
    with pytest.raises(WebFetchError) as err:
        _fetcher(lambda r: _page(body)).fetch("https://a.example/")
    assert err.value.code == "too_large"


def test_fetch_size_cap_by_content_length_header():
    with pytest.raises(WebFetchError) as err:
        _fetcher(lambda r: _page("<p>x</p>", headers={"content-length": str(fetch.MAX_BYTES + 1)})).fetch(
            "https://a.example/"
        )
    assert err.value.code == "too_large"


@pytest.mark.parametrize("ctype", ["application/json", "application/pdf", "image/png", ""])
def test_fetch_rejects_non_html(ctype):
    with pytest.raises(WebFetchError) as err:
        _fetcher(lambda r: _page("{}", ctype=ctype)).fetch("https://a.example/")
    assert err.value.code == "not_html"


def test_fetch_accepts_xhtml_and_decodes_charset():
    body = "<p>canción</p>".encode("latin-1")
    page = _fetcher(lambda r: _page(body, ctype="application/xhtml+xml; charset=ISO-8859-1")).fetch(
        "https://a.example/"
    )
    assert page.html == "<p>canción</p>"


def test_fetch_http_error_status():
    with pytest.raises(WebFetchError) as err:
        _fetcher(lambda r: _page(status=404)).fetch("https://a.example/")
    assert err.value.code == "http_status"


def test_fetch_timeout():
    def handler(request):
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(WebFetchError) as err:
        _fetcher(handler).fetch("https://a.example/")
    assert err.value.code == "timeout"


def test_fetch_past_deadline_is_timeout_without_request():
    hit = []
    clock = lambda: 100.0  # noqa: E731
    f = _fetcher(lambda r: hit.append(r) or _page(), clock=clock)
    with pytest.raises(WebFetchError) as err:
        f.fetch("https://a.example/", deadline=99.0)
    assert err.value.code == "timeout"
    assert hit == []


def test_fetch_transport_error():
    def handler(request):
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(WebFetchError) as err:
        _fetcher(handler).fetch("https://a.example/")
    assert err.value.code == "error"


# --- reasons and the lite engine (web-lookup-polish-20261010 T3) -----------------------


def test_http_error_carries_the_status_as_reason():
    with pytest.raises(WebFetchError) as err:
        _fetcher(lambda r: _page(status=503)).fetch("https://a.example/")
    assert err.value.code == "http_status"
    assert err.value.status == 503
    assert err.value.reason == "http_503"


def test_tls_failure_is_its_own_reason():
    def handler(request):
        raise httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed", request=request)

    with pytest.raises(WebFetchError) as err:
        _fetcher(handler).fetch("https://a.example/")
    assert err.value.code == "tls"
    assert err.value.reason == "tls"


@pytest.mark.parametrize(
    "code,reason",
    [("dns", "dns"), ("blocked", "blocked"), ("error", "other"), ("too_large", "other"),
     ("not_html", "other"), ("redirects", "other"), ("http_status", "other")],
)
def test_reasons_are_a_closed_set(code, reason):
    assert WebFetchError(code).reason == reason


def test_lite_search_url_quotes_the_query_only():
    url = search.lite_search_url("cuándo sale silksong")
    assert url == "https://lite.duckduckgo.com/lite/?q=cu%C3%A1ndo+sale+silksong"


LITE = """<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.0 Transitional//EN"
  "http://www.w3.org/TR/xhtml1/DTD/xhtml1-transitional.dtd">
<html><head><meta http-equiv="content-type" content="text/html; charset=UTF-8">
<title>silksong at DuckDuckGo</title></head>
<body>
<form action="/lite/" method="post"><input class='query' type="text" size="40" name="q" value="silksong">
<input class='submit' type="submit" value="Search"></form>
<table border="0">
  <tr class="result-sponsored">
    <td valign="top">1.&nbsp;</td>
    <td><a rel="nofollow" href="https://duckduckgo.com/y.js?ad_domain=shop.example&amp;ad_provider=bingv7aa" class='result-link'>Buy Silksong now</a></td>
  </tr>
  <tr class="result-sponsored"><td>&nbsp;&nbsp;&nbsp;</td><td class='result-snippet'>Sponsored offer.</td></tr>
</table>
<table border="0">
  <tr>
    <td valign="top">1.&nbsp;</td>
    <td>
      <a rel="nofollow" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.3djuegos.com%2Fsilksong%3Fp%3D2&amp;rut=abc123" class='result-link'>Hollow Knight: <b>Silksong</b> fecha</a>
    </td>
  </tr>
  <tr>
    <td>&nbsp;&nbsp;&nbsp;</td>
    <td class='result-snippet'>
      <b>Silksong</b> sale el 4 de septiembre de 2025 &amp; llega a Switch.
    </td>
  </tr>
  <tr>
    <td>&nbsp;&nbsp;&nbsp;</td>
    <td><span class='link-text'>www.3djuegos.com/silksong</span>&nbsp;&nbsp;&nbsp;<span class='timestamp'>2025-08-21</span></td>
  </tr>
  <tr><td>&nbsp;</td><td>&nbsp;</td></tr>
  <tr>
    <td valign="top">2.&nbsp;</td>
    <td><a rel="nofollow" href="http://insecure.example.org/x" class='result-link'>Plain http</a></td>
  </tr>
  <tr><td>&nbsp;&nbsp;&nbsp;</td><td class='result-snippet'>Not https.</td></tr>
  <tr>
    <td valign="top">3.&nbsp;</td>
    <td><a rel="nofollow" href="https://www.nintenderos.com/silksong/" class="result-link extra">Silksong en Switch</a></td>
  </tr>
  <tr>
    <td valign="top">4.&nbsp;</td>
    <td><a rel="nofollow" href="https://wiki.example.net/Silksong" class='result-link'>Silksong wiki</a></td>
  </tr>
  <tr><td>&nbsp;&nbsp;&nbsp;</td><td class='result-snippet'>A <i>metroidvania</i>.</td></tr>
</table>
</body></html>
"""


def test_parse_lite_results_reads_result_rows():
    results = search.parse_lite_results(LITE)
    assert [r.url for r in results] == [
        "https://www.3djuegos.com/silksong?p=2",
        "https://www.nintenderos.com/silksong/",
        "https://wiki.example.net/Silksong",
    ]
    assert results[0].title == "Hollow Knight: Silksong fecha"
    assert results[0].snippet == "Silksong sale el 4 de septiembre de 2025 & llega a Switch."
    assert results[1].snippet == ""
    assert results[2].snippet == "A metroidvania."


def test_parse_lite_results_caps_and_tolerates_garbage():
    row = "<tr><td><a class='result-link' href='https://a.example.com/{i}'>T{i}</a></td></tr>"
    assert len(search.parse_lite_results("<table>" + "".join(row.format(i=i) for i in range(9)), limit=5)) == 5
    assert search.parse_lite_results("") == []
    assert search.parse_lite_results("<td class='result-snippet'>orphan</td><a class='result-link'") == []


ANOMALY = """<html><body><div class="anomaly-modal__title">Unfortunately, bots use DuckDuckGo too.</div>
<form id="challenge-form" action="//duckduckgo.com/anomaly.js?sv=html&amp;cc=sre" method="POST"></form>
</body></html>"""


@pytest.mark.parametrize(
    "html,engine,reason",
    [
        (DDG, "html", ""),
        (LITE, "lite", ""),
        (ANOMALY, "html", "anti_bot"),
        (ANOMALY, "lite", "anti_bot"),
        ('<html><div class="no-results">No results.</div></html>', "html", ""),
        ("<html><table><tr><td>No results.</td></tr></table></html>", "lite", ""),
        ("<html><p>something else entirely</p></html>", "html", "parse"),
        ("", "lite", "parse"),
    ],
)
def test_page_reason_spots_anti_bot_and_unknown_pages(html, engine, reason):
    assert search.page_reason(html, engine) == reason


def test_domain_of_strips_everything_but_the_host():
    assert fetch.domain_of("https://News.Example.com:443/a/b?q=secret#x") == "news.example.com"
    assert fetch.domain_of("not a url") == ""


# --- extract ------------------------------------------------------------------

PAGE = """
<html><head><title>T</title><script>var x = "ignore me";</script><style>p{}</style></head>
<body>
<header>Site header menu</header>
<nav><a>Home</a><a>News</a></nav>
<div class="cookie">We use cookies.</div>
<p>First paragraph has quite a few words in it so that it counts as real content for the reader.</p>
<p>Second   paragraph\u200b with &amp; entities and a zero\u202e width char and more words here please.</p>
<aside>Related links aside</aside>
<footer>Copyright footer</footer>
</body></html>
"""


def test_html_to_paragraphs_drops_boilerplate_tags_and_control_chars():
    paragraphs = extract.html_to_paragraphs(PAGE)
    text = " ".join(paragraphs)
    for junk in ("ignore me", "Site header", "Home", "Related links", "Copyright"):
        assert junk not in text
    assert "First paragraph has quite" in text
    assert "Second paragraph with & entities and a zero width char" in text
    assert "\u200b" not in text and "\u202e" not in text


def test_main_or_article_content_wins_when_present():
    html = "<body><div>Outer junk text that is long enough to be a paragraph of words.</div>" \
           "<article><p>Article body is the real content and it is long enough here.</p></article></body>"
    assert extract.html_to_paragraphs(html) == ["Article body is the real content and it is long enough here."]


def test_chunks_respect_word_cap_and_minimum():
    words = " ".join(f"w{i}" for i in range(300))
    html = f"<p>tiny</p><p>{words}</p><p>short tail words here</p>"
    chunks = extract.html_to_chunks(html, max_words=120, min_words=15)
    assert all(len(c.split()) <= 120 for c in chunks)
    assert all(len(c.split()) >= 15 for c in chunks)
    assert chunks[0].split()[:2] == ["tiny", "w0"]


def test_chunks_cap_count():
    html = "".join(f"<p>{' '.join(['palabra'] * 40)} {i}</p>" for i in range(50))
    assert len(extract.html_to_chunks(html, max_chunks=6)) == 6


def test_clean_text_removes_controls_and_collapses_space():
    assert extract.clean_text("a\x00b\x1b[31m c\t\n d\u2066e") == "ab[31m c de"


def test_extract_never_raises_on_garbage():
    assert extract.html_to_chunks("<<<>>></p></div><script>") == []
    assert extract.html_to_chunks(None) == []


def test_fetcher_accept_language_follows_active_locale():
    from types import SimpleNamespace
    from opencohost.i18n import active as i18n_active

    seen_headers = []

    def handler(request):
        seen_headers.append(request.headers.get("accept-language"))
        return _page("<p>hello</p>")

    try:
        i18n_active.set_active_bundle(SimpleNamespace(code="en", data={}, fallback=None))
        _fetcher(handler).fetch("https://example.com/")
        i18n_active.set_active_bundle(SimpleNamespace(code="es", data={}, fallback=None))
        _fetcher(handler).fetch("https://example.com/")
    finally:
        i18n_active.reset_active_bundle()

    assert len(seen_headers) == 2
    assert seen_headers[0].startswith("en")
    assert seen_headers[1] == "es,en;q=0.8"

