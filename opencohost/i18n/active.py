"""Process-wide active locale bundle (load-once, with an injection seam).

The active bundle is resolved once per process at first access (or set
explicitly at startup) and held immutable, so engines read locale resources
lock-free and without re-hitting disk on every TTS chunk. Tests inject via
:func:`set_active_bundle` / :func:`reset_active_bundle`.

Every typed accessor (e.g. :func:`edge_voice`) carries a LEGACY default that
matches pre-i18n behavior exactly, so a total i18n failure can never change what
the engines did before this track.
"""
from __future__ import annotations

import threading

from opencohost.i18n.contract import LocaleBundle, resolve
from opencohost.i18n.startup import resolve_active_bundle

# Last-resort defaults — must match the values hard-coded in the engines before
# the i18n track, so the literal→accessor substitution is provably a no-op.
LEGACY_EDGE_VOICE = "es-MX-DaliaNeural"

_lock = threading.Lock()
_active: LocaleBundle | None = None


def get_active_bundle() -> LocaleBundle:
    """The active bundle for this process, resolved once and cached."""
    global _active
    if _active is None:
        with _lock:
            if _active is None:
                _active = resolve_active_bundle()
    return _active


def set_active_bundle(bundle: LocaleBundle) -> None:
    """Set the active bundle explicitly (app startup) or inject it (tests)."""
    global _active
    with _lock:
        _active = bundle


def reset_active_bundle() -> None:
    """Clear the cache so the next access re-resolves (tests)."""
    global _active
    with _lock:
        _active = None


def edge_voice() -> str:
    """Edge-TTS voice for the active locale (legacy es default on any failure)."""
    try:
        return resolve(get_active_bundle(), "tts.edge_voice", default=LEGACY_EDGE_VOICE)
    except Exception:
        return LEGACY_EDGE_VOICE
