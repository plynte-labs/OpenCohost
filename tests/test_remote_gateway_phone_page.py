"""T4 of remote-voice-gateway: the static phone page served by the gateway.

The browser logic (Web Speech, speechSynthesis, polling) cannot run in this suite;
these tests pin the serving contract, the security headers and static properties
of the shipped files (no external URLs, only same-origin gateway paths, no token
in any URL).
"""

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.test_api_phase1 import FakeHost

STATIC = Path(__file__).resolve().parent.parent / "opencohost" / "gateway" / "static"

EXPECTED_HEADERS = {
    "content-security-policy": (
        "default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "connect-src 'self' blob:; img-src 'self' data: blob:; base-uri 'none'; form-action 'none'; "
        "frame-ancestors 'none'"
    ),
    "referrer-policy": "no-referrer",
    "x-content-type-options": "nosniff",
    "cache-control": "no-store",
}


@pytest.fixture
def client():
    from opencohost.gateway.app import create_gateway_app

    return TestClient(create_gateway_app(FakeHost(), None, None))


def _js() -> str:
    return (STATIC / "phone.js").read_text(encoding="utf-8")


def _html() -> str:
    return (STATIC / "phone.html").read_text(encoding="utf-8")


def test_root_serves_html_with_security_headers_and_no_token(client):
    r = client.get("/")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    for name, value in EXPECTED_HEADERS.items():
        assert r.headers[name] == value
    assert "phone.js" in r.text


def test_phone_js_served_with_js_type_and_headers_and_no_token(client):
    r = client.get("/phone.js")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/javascript")
    for name, value in EXPECTED_HEADERS.items():
        assert r.headers[name] == value
    assert r.text == _js()


def test_html_references_phone_js_without_inline_script_or_external_urls():
    html = _html()
    scripts = re.findall(r"<script\b([^>]*)>(.*?)</script>", html, flags=re.S | re.I)
    assert scripts, "phone.html must load phone.js"
    for attrs, body in scripts:
        assert "/phone.js" in attrs or "/avatar.js" in attrs
        assert body.strip() == ""
    assert any("phone.js" in attrs for attrs, _ in scripts)
    assert not re.search(r"https?://", html, flags=re.I)
    assert not re.search(r"\son[a-z]+\s*=", html, flags=re.I)


def test_js_has_no_external_urls():
    assert not re.search(r"https?://|//cdn\.|wss?://", _js(), flags=re.I)


def test_js_calls_only_gateway_paths():
    js = _js()
    paths = set(re.findall(r"""fetch\(\s*['"`]([^'"`?]*)""", js))
    assert paths == {"/pair", "/turn", "/reply"}
    assert "XMLHttpRequest" not in js and "sendBeacon" not in js


def test_js_uses_bearer_authorization():
    js = _js()
    assert "Authorization" in js and "Bearer" in js


def test_js_never_puts_the_token_in_a_url():
    js = _js()
    # No query/URL construction mentioning the token, and no token in location writes.
    assert not re.search(r"""['"`][^'"`]*\?[^'"`]*token""", js, flags=re.I)
    assert not re.search(r"\$\{[^}]*token[^}]*\}", js, flags=re.I)
    assert not re.search(r"""['"`]/(?:pair|turn|reply)[^'"`]*['"`]\s*\+\s*\w*token""", js, flags=re.I)
    assert "location.href" not in js and "location.assign" not in js


def test_static_files_ship_with_the_package():
    pyproject = (STATIC.parents[2] / "pyproject.toml").read_text(encoding="utf-8")
    # Hatch wheel includes every non-ignored file under the listed package directories.
    assert 'packages = ["opencohost"]' in pyproject
    assert (STATIC / "phone.html").is_file() and (STATIC / "phone.js").is_file()


# ---- avatar integration (remote-vrm-avatar T3) --------------------------------


def test_html_loads_avatar_bundle_as_module_with_canvas_and_toggle():
    html = _html()
    tags = [m for m in re.findall(r"<script[^>]*>", html, flags=re.I) if "/avatar.js" in m]
    assert len(tags) == 1 and 'type="module"' in tags[0]
    assert 'id="kira-avatar"' in html and "<canvas" in html
    assert 'id="kira-avatar-loading"' in html
    assert 'id="avatarToggle"' in html


def test_phone_js_dispatches_the_avatar_event_contract_without_new_fetches():
    js = _js()
    for name in ("kira:auth", "kira:speech", "kira:state"):
        assert name in js
    for phase in ("'start'", "'boundary'", "'end'"):
        assert phase in js
    assert "onboundary" in js
    assert "opencohost_avatar_hidden" in js  # toggle persistence key


def test_avatar_bundle_is_real_and_has_no_network_or_worker_targets(client):
    r = client.get("/avatar.js")
    assert r.status_code == 200
    text = r.text
    assert len(text) > 100_000, "avatar.js is still the placeholder; run pnpm build:phone-avatar"
    assert "new Worker" not in text and "importScripts" not in text
    assert "eval(" not in text and "new Function" not in text
    assert not re.search(r"""(?:fetch|import|XMLHttpRequest|open)\(\s*[`'"]https?:""", text)
    assert "sourceMappingURL" not in text
    assert "/avatar/model" in text


def test_licence_notices_ship_next_to_the_bundle():
    notice = (STATIC / "avatar.LICENSES.txt").read_text(encoding="utf-8")
    for name in ("three", "@pixiv/three-vrm", "@pixiv/three-vrm-animation", "MIT"):
        assert name in notice


# ---- call-style layout (remote-vrm-avatar T7) -----------------------------------


def test_html_has_every_id_phone_js_and_avatar_need():
    html = _html()
    for ident in ("talk", "textInput", "sendBtn", "stop", "avatarToggle", "unlink", "status",
                  "kira-avatar", "pairing", "codeInput", "pairBtn", "heard", "reply"):
        assert f'id="{ident}"' in html, ident


def test_html_fills_the_viewport_without_scrolling():
    html = _html()
    assert "100dvh" in html
    assert "overflow: hidden" in html
    assert "safe-area-inset" in html


def test_html_has_no_hardcoded_idle_status_text():
    html = _html()
    assert "Listo" not in html and ">Ready<" not in html


def test_phone_js_hides_status_when_idle_and_toggles_stop():
    js = _js()
    assert "dataset.state" in js or "classList" in js
    assert "aria-label" in js
