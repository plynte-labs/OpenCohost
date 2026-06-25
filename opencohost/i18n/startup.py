"""i18n-core — startup resolver: pick the active bundle resiliently (T0d).

Degrade rule: a CRITICAL failure drops the WHOLE active locale to the base
(``es``), never limps slot-by-slot — so we never ship English-text + Spanish-voice.
The app never boots without a locale: requested → es → inline minimum.

Anti-shadowing: official bundles ALWAYS win on a code collision, so a community
mod named ``es`` can never override the official Spanish guardrails.

The resolved bundle is immutable (frozen) and loaded once at startup, so every
engine reads it lock-free from any thread. There is no runtime switch.
"""
from __future__ import annotations

import logging

from opencohost.i18n import state, tags
from opencohost.i18n.contract import TIER_OFFICIAL, LocaleBundle
from opencohost.i18n.registry import (
    build_chain,
    community_locales_dir,
    discover_bundles,
    official_locales_dir,
    validate_bundle,
)

BASE_LOCALE = "es"


def _minimal_base() -> LocaleBundle:
    """Last-resort inline bundle if even the shipped es bundle fails to load."""
    return LocaleBundle(
        code="es",
        tier=TIER_OFFICIAL,
        data={
            "meta": {"code": "es", "display": "Español", "tier": "official"},
            "tts": {"edge_voice": "es-MX-DaliaNeural", "qwen_language": "Spanish"},
        },
    )


def merge_bundles(
    official: dict[str, LocaleBundle], community: dict[str, LocaleBundle]
) -> dict[str, LocaleBundle]:
    """Merge registries. OFFICIAL ALWAYS WINS on a code collision (anti-shadowing)."""
    merged = dict(community)   # community first...
    merged.update(official)    # ...official overwrites — a mod cannot shadow es/en.
    return merged


def load_registry(official_dir=None, community_dir=None) -> dict[str, LocaleBundle]:
    """Discover and merge official + community bundles into one registry."""
    official = discover_bundles(official_dir or official_locales_dir(), "official")
    community = discover_bundles(community_dir or community_locales_dir(), "community")
    return merge_bundles(official, community)


def _try_load(registry, code, log) -> LocaleBundle | None:
    try:
        bundle = build_chain(code, registry)
    except Exception as exc:  # malformed chain, cycle, load failure
        log.error("i18n: failed to load locale '%s' (%s)", code, exc)
        return None
    result = validate_bundle(bundle)
    if result.ok:
        return bundle
    log.error("i18n: locale '%s' invalid: %s", code, result.errors)
    return None


def resolve_active_bundle(*, locale=None, registry=None, logger=None) -> LocaleBundle:
    """Resolve the active bundle for this session, degrading safely on failure.

    ``locale``/``registry`` are injectable for tests; otherwise read from
    persisted state and the real locale directories.
    """
    log = logger or logging.getLogger("OpenCohost")
    reg = registry if registry is not None else load_registry()
    requested = locale if locale is not None else state.get_locale()

    code = tags.match(requested, list(reg.keys())) if reg else None
    if code:
        bundle = _try_load(reg, code, log)
        if bundle is not None:
            return bundle
        log.warning("i18n: locale %r unusable; falling back to '%s'", requested, BASE_LOCALE)
    else:
        log.warning("i18n: locale %r not available; falling back to '%s'", requested, BASE_LOCALE)

    base_code = tags.match(BASE_LOCALE, list(reg.keys())) if reg else None
    if base_code:
        bundle = _try_load(reg, base_code, log)
        if bundle is not None:
            return bundle

    log.critical("i18n: base locale unavailable; using inline minimum")
    return _minimal_base()
