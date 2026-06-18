"""Tests for BCP 47 locale tag normalization (the world standard)."""
from __future__ import annotations

from opencohost.i18n.tags import match, normalize, primary


def test_normalize_case():
    assert normalize("EN") == "en"


def test_normalize_separator_and_region_case():
    assert normalize("en_US") == "en-US"


def test_normalize_trim_and_region():
    assert normalize("  es-mx ") == "es-MX"


def test_normalize_empty_and_none():
    assert normalize("") == ""
    assert normalize(None) == ""


def test_primary_subtag():
    assert primary("en-US") == "en"


def test_match_exact_case_insensitive():
    assert match("EN", ["en", "es"]) == "en"


def test_match_falls_to_primary_subtag():
    assert match("en-US", ["en"]) == "en"


def test_match_prefers_exact_over_primary():
    assert match("en-US", ["en", "en-US"]) == "en-US"


def test_match_unknown_returns_none():
    assert match("fr", ["en", "es"]) is None
