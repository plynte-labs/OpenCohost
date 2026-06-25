"""BCP 47 locale tag normalization (the world standard).

OpenCohost keys bundles by primary language subtag (``es``, ``en``); region
subtags (``en-US``) are accepted, canonicalized, and matched down to the base
language when no region-specific bundle exists.
"""
from __future__ import annotations

from typing import Iterable


def normalize(raw: str | None) -> str:
    """Canonical BCP 47 form: language lowercase, region UPPER, script Title, ``-`` sep.

    ``'en_US'`` → ``'en-US'``, ``'EN'`` → ``'en'``, ``'  es-mx '`` → ``'es-MX'``.
    Empty / ``None`` → ``''``.
    """
    if not raw:
        return ""
    parts = [p for p in str(raw).strip().replace("_", "-").split("-") if p]
    if not parts:
        return ""
    out = [parts[0].lower()]
    for p in parts[1:]:
        if len(p) == 2 and p.isalpha():       # region: ISO 3166-1 alpha-2
            out.append(p.upper())
        elif len(p) == 4 and p.isalpha():     # script: ISO 15924
            out.append(p.title())
        else:
            out.append(p.lower())
    return "-".join(out)


def primary(tag: str | None) -> str:
    """Primary language subtag: ``'en-US'`` → ``'en'``."""
    n = normalize(tag)
    return n.split("-", 1)[0] if n else ""


def match(tag: str | None, available: Iterable[str]) -> str | None:
    """Match ``tag`` to an available bundle code: exact (canonical) first, else primary.

    Returns the matching code from ``available`` (as listed there), or ``None``.
    """
    n = normalize(tag)
    if not n:
        return None
    avail_norm = {normalize(c): c for c in available}
    if n in avail_norm:
        return avail_norm[n]
    p = primary(n)
    if p in avail_norm:
        return avail_norm[p]
    return None
