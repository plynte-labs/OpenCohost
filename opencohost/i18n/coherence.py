"""i18n-core — T4 coherence gate: warn-only, the profile always wins.

Kira has three language-bearing pieces that should agree for a coherent result:
the active locale (what the app says it speaks), the TTS voice (how she sounds),
and the persona/prompt (how she thinks). The first two come from the same locale
bundle and are coherent by construction; the persona can diverge because a
selected profile may override the bundle persona with its own prompt.

This gate compares them and emits NON-BLOCKING warnings on a mismatch. It NEVER
overrides anything — the profile (the user's customization) always wins; a
mismatch is announced, never corrected (decision #2223). T4 is therefore not a
ship-blocker.

Detection is deterministic (Option A): the gate does not infer a profile's
language from its prompt text — inferring profile language belongs to the
separate profile-locale-autodetect track. Today the gate flags that a custom
profile persona is outside locale governance. Once a profile declares an explicit
``locale`` (Option B / the autodetect track), the SAME gate compares languages
precisely and the generic notice falls silent — Option B is then pure data, no
new gate code. See conductor/tracks/profile_locale_autodetect_20260618/proposal.md.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from opencohost.i18n.contract import LocaleBundle, resolve

# Stable warning codes (for tests today, a UI badge later — same deferral as the
# community-mod badge). The message text may change; the code is the contract.
BUNDLE_VOICE_MISMATCH = "bundle_voice_mismatch"
PROFILE_LOCALE_MISMATCH = "profile_locale_mismatch"
PROFILE_PERSONA_UNGOVERNED = "profile_persona_ungoverned"
PIPER_VOICE_LOCALE_MISMATCH = "piper_voice_locale_mismatch"


@dataclass(frozen=True)
class CoherenceWarning:
    """One non-blocking coherence finding. ``code`` is stable; ``message`` is human text."""

    code: str
    message: str


def primary_subtag(tag: str | None) -> str:
    """Primary language subtag of a BCP 47 tag, lowercased (``en-US`` -> ``en``)."""
    if not tag:
        return ""
    return tag.split("-", 1)[0].strip().lower()


def voice_language(bundle: LocaleBundle) -> str:
    """Primary language subtag of the bundle's Edge-TTS voice (``""`` if none)."""
    return primary_subtag(resolve(bundle, "tts.edge_voice", default=""))


def check_coherence(
    bundle: LocaleBundle,
    *,
    profile_name: str | None = None,
    profile_prompt_active: bool = False,
    profile_locale: str | None = None,
    piper_voice_lang: str | None = None,
) -> list[CoherenceWarning]:
    """Return coherence warnings for the active bundle + profile. Never raises, never mutates.

    Args:
        bundle: the active locale bundle.
        profile_name: name of the selected profile (for the message only).
        profile_prompt_active: True when a profile supplies a custom persona that
            overrides the bundle persona.
        profile_locale: the profile's declared locale, if any (Option B). When
            present, the gate compares languages precisely and the generic
            "ungoverned" notice is suppressed.
        piper_voice_lang: the ``lang`` of the currently selected offline Piper
            voice, if known (e.g. ``PIPER_VOICES[key]["lang"]``). ``None``
            (the default) skips this check silently — callers that have no
            Piper voice info yet never produce a false mismatch.
    """
    warnings: list[CoherenceWarning] = []
    locale_lang = primary_subtag(bundle.code)
    label = profile_name or "active profile"

    # Check 1 — bundle self-coherence: declared locale vs TTS voice language.
    # Normally silent (both from the same bundle); fires on a malformed or
    # community bundle, or a degrade that produced an incoherent pairing.
    voice_lang = voice_language(bundle)
    if voice_lang and locale_lang and voice_lang != locale_lang:
        warnings.append(CoherenceWarning(
            BUNDLE_VOICE_MISMATCH,
            f"Active locale '{bundle.code}' but the TTS voice speaks '{voice_lang}' — "
            f"voice and locale disagree; check the locale bundle.",
        ))

    # Check 2 — persona vs active locale.
    if profile_locale is not None:
        # Option B (precise): the profile declares its language.
        profile_lang = primary_subtag(profile_locale)
        if profile_lang and locale_lang and profile_lang != locale_lang:
            warnings.append(CoherenceWarning(
                PROFILE_LOCALE_MISMATCH,
                f"Profile '{label}' is '{profile_lang}' but the active locale is "
                f"'{locale_lang}'. The profile wins — persona stays '{profile_lang}', "
                f"voice stays '{locale_lang}'. Switch the locale to '{profile_lang}' "
                f"for a coherent voice + persona.",
            ))
    elif profile_prompt_active:
        # Option A (today): a custom persona with no declared language. The locale
        # system cannot govern it; advise the user to keep it aligned.
        warnings.append(CoherenceWarning(
            PROFILE_PERSONA_UNGOVERNED,
            f"Profile '{label}' supplies a custom persona; the locale system "
            f"('{locale_lang}') does not govern its language. Ensure the profile "
            f"text matches '{locale_lang}' for a coherent voice + persona.",
        ))

    # Check 3 — offline Piper voice vs active locale (honest-degrade warning).
    # Silent when no voice lang is supplied or it matches the active locale.
    if piper_voice_lang:
        voice_primary = primary_subtag(piper_voice_lang)
        if voice_primary and locale_lang and voice_primary != locale_lang:
            warnings.append(CoherenceWarning(
                PIPER_VOICE_LOCALE_MISMATCH,
                f"Active locale '{bundle.code}' but the selected offline Piper "
                f"voice speaks '{voice_primary}' — install/select a "
                f"'{locale_lang}' Piper voice for a coherent offline fallback.",
            ))

    return warnings


def log_coherence(
    bundle: LocaleBundle,
    *,
    profile_name: str | None = None,
    profile_prompt_active: bool = False,
    profile_locale: str | None = None,
    piper_voice_lang: str | None = None,
    logger: logging.Logger | None = None,
) -> list[CoherenceWarning]:
    """Run :func:`check_coherence` and log each finding at WARNING. Returns the findings."""
    log = logger or logging.getLogger("OpenCohost")
    warnings = check_coherence(
        bundle,
        profile_name=profile_name,
        profile_prompt_active=profile_prompt_active,
        profile_locale=profile_locale,
        piper_voice_lang=piper_voice_lang,
    )
    for w in warnings:
        log.warning("i18n coherence [%s]: %s", w.code, w.message)
    return warnings
