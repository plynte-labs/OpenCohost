"""K5 (warmth-guardrails-runtime-20261006): personalization defaults.

* A nickname that sanitizes to nothing ("123", "!!!") no longer becomes the
  hard-coded "Usuario": the self-knowledge owner fact falls back to its
  neutral, localized wording (no name), and the prompt label falls back to
  the active locale's user label.
* The identity line reads naturally ("co-host en streams, parte del proyecto
  OpenCohost") instead of "la co-host de OpenCohost"; the facts stay intact.
"""
from __future__ import annotations

import pytest

from opencohost.core.context.injection_markers import sanitize_user_label
from opencohost.core.self_knowledge import wiring
from opencohost.core.self_knowledge.texts import PRODUCT_NAME, t
from opencohost.i18n import active as i18n_active
from opencohost.i18n.registry import discover_bundles, official_locales_dir
from opencohost.i18n.startup import resolve_active_bundle


@pytest.fixture
def locale():
    registry = discover_bundles(official_locales_dir(), "official")

    def _set(code):
        i18n_active.set_active_bundle(resolve_active_bundle(locale=code, registry=registry))

    yield _set
    i18n_active.reset_active_bundle()


@pytest.mark.parametrize("nickname", ["123", "!!!", "[]:", "  42  "])
def test_owner_nickname_that_sanitizes_to_empty_reads_as_no_name(monkeypatch, nickname):
    from opencohost.core.profiles import personalization

    monkeypatch.setattr(
        personalization, "load_personalization", lambda: {"enabled": True, "nickname": nickname}
    )
    assert wiring.read_owner_nickname() == ""


def test_owner_nickname_with_letters_is_kept(monkeypatch):
    from opencohost.core.profiles import personalization

    monkeypatch.setattr(
        personalization, "load_personalization", lambda: {"enabled": True, "nickname": "Tavo_99"}
    )
    assert wiring.read_owner_nickname() == "Tavo"


@pytest.mark.parametrize("code,expected", [("es", "Usuario"), ("en", "User")])
def test_label_last_resort_is_the_locale_user_label(locale, code, expected):
    locale(code)
    assert sanitize_user_label("123") == expected
    assert sanitize_user_label("[]:", fallback="") == expected
    assert sanitize_user_label(None, fallback="   ") == expected


@pytest.mark.parametrize("lang", ["es", "en"])
def test_identity_line_wording(lang):
    text = t(lang)["identity"].format(name=PRODUCT_NAME, description="D.")
    if lang == "es":
        assert text.startswith("Eres Kira, co-host en streams, parte del proyecto OpenCohost.")
        assert "co-host de OpenCohost" not in text
    else:
        assert text.startswith("You are Kira, a streaming co-host and part of the OpenCohost project.")
        assert "co-host of OpenCohost" not in text
    assert text.endswith("D.")
