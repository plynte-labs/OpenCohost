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

# Phase 4 (multi_provider_llm_20260723): a handful of individual slots outside
# a security domain must ALSO fail closed (missing translation -> SlotNotFound,
# never a silent cross-locale borrow) without making their WHOLE domain
# no-fallback. `llm` is documented (i18n/active.py) as fallback-ALLOWED for its
# other ~40 scaffolding slots — broadening NO_FALLBACK_DOMAINS to cover "llm"
# would fail those closed too for any future partial community-tier locale.
# Slot-exact governance, checked alongside the domain list in `resolve()`.
NO_FALLBACK_SLOTS = frozenset({
    "llm.provider_fallback_notice",
    # T4 (live_session_fixes_20260925): the brief transitional phrase spoken
    # before a regenerated reply when the owner setting
    # `discard_stale_speech_on_regenerated_reply` is on. Same governance as
    # its sibling above -- a new-era slot with no legacy cross-locale
    # default, so a locale that omits it must fail closed, never silently
    # borrow the other locale's line.
    "llm.provider_fallback_transition_notice",
    # S1 (guardrails-vulnerable-users): the deterministic reply spoken when the
    # owner's own words signal self-harm. A locale that omits it must not
    # borrow another locale's text; i18n/active.py::crisis_reply degrades to a
    # built-in English line (never silence) instead.
    "llm.crisis_reply",
    # K4 (warmth-guardrails-runtime-20261006): the guardrail retry nudges.
    # A locale that omits them must not borrow another locale's correction
    # (the old hard-coded es text leaked Spanish into en retries);
    # i18n/active.py::guardrail_retry_nudge degrades to a built-in English
    # note instead.
    "llm.guardrail_retry_nudges",
    # K5 (warmth-guardrails-runtime-20261006): the per-profile warmth blocks.
    # A locale that omits them must not borrow another locale's text;
    # i18n/active.py::warmth_block degrades to a built-in English block.
    "llm.warmth_blocks",
    # K8: the one-line warmth reminder that closes a warm system prompt.
    # Same governance as the blocks; i18n/active.py::warmth_recency_line
    # degrades to a built-in English line.
    "llm.warmth_recency",
    # kira-personal-tone-20261007 P2/P3: the profile/memory relevance rule
    # (warm owner turns) and the vision answer rules. Same governance;
    # i18n/active.py degrades each to a built-in English line.
    "llm.facts_relevance_rule",
    # kira-natural-replies-20261111 A2: per-message role labels (owner / chat)
    # and the one stable line mapping the owner role to the nickname. Same
    # governance; i18n/active.py degrades each to a built-in English value.
    "llm.owner_role_label",
    "llm.chat_role_label",
    "llm.owner_nickname_line",
    "llm.vision_answer_rules",
    # kira-personal-tone-20261007 P5: how to address the owner grammatically.
    # Same governance; i18n/active.py::owner_gender_line degrades to English.
    "llm.owner_gender_lines",
    # kira-identity-core-rules-20261007 C1: the core rules appended for custom
    # personas. Same governance; i18n/active.py::core_rules degrades to English.
    "llm.core_rules",
})


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
    allow_fallback = _domain_of(slot) not in NO_FALLBACK_DOMAINS and slot not in NO_FALLBACK_SLOTS
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
