"""Tests for the i18n-core startup resolver (T0d).

Resilience: resolve the active bundle, degrade a failed/unknown locale to the
es base (loudly), never boot without a locale, and never let a community mod
shadow an official code.
"""
from __future__ import annotations

from opencohost.i18n.contract import (
    TIER_COMMUNITY,
    TIER_OFFICIAL,
    LocaleBundle,
    resolve,
)
from opencohost.i18n.startup import merge_bundles, resolve_active_bundle


class _RecLogger:
    """Captures (level, message) so tests can assert on what was logged."""

    def __init__(self):
        self.records: list[tuple[str, str]] = []

    def _log(self, level, msg, *args):
        self.records.append((level, (msg % args) if args else msg))

    def debug(self, m, *a):
        self._log("debug", m, *a)

    def info(self, m, *a):
        self._log("info", m, *a)

    def warning(self, m, *a):
        self._log("warning", m, *a)

    def error(self, m, *a):
        self._log("error", m, *a)

    def critical(self, m, *a):
        self._log("critical", m, *a)

    def levels(self):
        return [lvl for lvl, _ in self.records]


def _b(code, tier=TIER_OFFICIAL, slots=None):
    data = {"meta": {"code": code, "tier": tier, "fallback": []}}
    if slots:
        data.update(slots)
    return LocaleBundle(code=code, tier=tier, data=data)


def test_merge_official_wins_over_community_shadow():
    # A malicious community 'es' must NOT override the official Spanish guardrails.
    official = {"es": _b("es", slots={"guardrails": {"banned_phrases": ["real"]}})}
    community = {
        "es": _b("es", TIER_COMMUNITY, slots={"guardrails": {"banned_phrases": ["EVIL"]}})
    }
    merged = merge_bundles(official, community)
    assert merged["es"].tier == TIER_OFFICIAL
    assert resolve(merged["es"], "guardrails.banned_phrases") == ["real"]


def test_resolves_requested_locale_when_present():
    reg = {
        "es": _b("es", slots={"tts": {"edge_voice": "es-MX-DaliaNeural"}}),
        "en": _b("en", slots={"tts": {"edge_voice": "en-US-AriaNeural"}}),
    }
    b = resolve_active_bundle(locale="en", registry=reg, logger=_RecLogger())
    assert b.code == "en"
    assert resolve(b, "tts.edge_voice") == "en-US-AriaNeural"


def test_normalizes_requested_locale():
    reg = {"en": _b("en", slots={"tts": {"edge_voice": "en-US-AriaNeural"}})}
    b = resolve_active_bundle(locale="EN_us", registry=reg, logger=_RecLogger())
    assert b.code == "en"


def test_unknown_locale_degrades_to_es_loudly():
    reg = {"es": _b("es", slots={"tts": {"edge_voice": "es-MX-DaliaNeural"}})}
    log = _RecLogger()
    b = resolve_active_bundle(locale="fr", registry=reg, logger=log)
    assert b.code == "es"
    assert "warning" in log.levels()


def test_empty_registry_uses_inline_minimum():
    log = _RecLogger()
    b = resolve_active_bundle(locale="en", registry={}, logger=log)
    assert b.code == "es"
    assert resolve(b, "tts.edge_voice") == "es-MX-DaliaNeural"
    assert "critical" in log.levels()
