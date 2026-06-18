"""i18n-core — the locale contract (the seam every engine reads from).

A *bundle* is one locale's resource pack: a ``code``, a ``tier``, and a nested
``data`` dict. The *resolver* (:func:`resolve`) looks up a dotted slot key
against a bundle, walking the bundle's fallback chain — EXCEPT for security
slots, which never fall back. A guardrail authored for one language does not
protect another (a Spanish banned-phrase list catches nothing in English), so a
missing security slot means "OFF", not "borrow the fallback locale's patterns".

Adding a language = adding a bundle (data), never engine code.
See conductor/tracks/english_compatibility_i18n_20260617/proposal.md.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# --- Tiers -----------------------------------------------------------------
# official  : guardrails authored + reviewed by the project (es, en).
# community : unofficial drop-in mod — no guarantee, at the user's own risk.
TIER_OFFICIAL = "official"
TIER_COMMUNITY = "community"
TIERS = (TIER_OFFICIAL, TIER_COMMUNITY)

# Manifest schema version. Bump when the slot layout changes; old community mods
# can then be warned/adapted instead of silently misread.
SCHEMA_VERSION = 1

# --- Slot domains (the closed set) -----------------------------------------
# Bundles fill these; engines read these. Keys are dotted, e.g.
# "tts.edge_voice", "llm.scaffolding.user_message", "guardrails.banned_phrases".
SLOT_DOMAINS = ("meta", "tts", "llm", "guardrails", "nlp", "ui")

# Security slots never fall back across locales. A missing one means OFF (and
# must gate the official tier), never "silently borrow the fallback locale's".
NO_FALLBACK_DOMAINS = ("guardrails",)


class SlotNotFound(KeyError):
    """A slot is absent from a bundle and no fallback resolves it."""


_UNSET = object()


@dataclass(frozen=True)
class LocaleBundle:
    """One locale's resource pack.

    ``data`` is a nested dict keyed by slot domain, e.g.::

        {"meta": {"code": "es"}, "tts": {"edge_voice": "es-MX-DaliaNeural"}}

    ``fallback`` is the next bundle in the chain (or ``None``). Non-security
    slots resolve through it; security slots do not.
    """

    code: str
    tier: str
    data: dict[str, Any]
    fallback: "LocaleBundle | None" = None


def _domain_of(slot: str) -> str:
    return slot.split(".", 1)[0]


def _lookup(data: dict[str, Any], slot: str) -> Any:
    """Nested lookup of a dotted slot key in one bundle's data.

    Raises :class:`KeyError` if any segment is missing.
    """
    node: Any = data
    for part in slot.split("."):
        if not isinstance(node, dict) or part not in node:
            raise KeyError(slot)
        node = node[part]
    return node


def resolve(bundle: LocaleBundle, slot: str, default: Any = _UNSET) -> Any:
    """Resolve a dotted ``slot`` against ``bundle``, then its fallback chain.

    Security slots (see :data:`NO_FALLBACK_DOMAINS`) are looked up only in
    ``bundle`` itself — they never borrow another locale's patterns.

    Returns ``default`` if provided and nothing resolves; otherwise raises
    :class:`SlotNotFound`.
    """
    allow_fallback = _domain_of(slot) not in NO_FALLBACK_DOMAINS
    current: LocaleBundle | None = bundle
    while current is not None:
        try:
            return _lookup(current.data, slot)
        except KeyError:
            if not allow_fallback:
                break
            current = current.fallback
    if default is not _UNSET:
        return default
    raise SlotNotFound(slot)
