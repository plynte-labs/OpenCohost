"""Optional grammatical gender used to address the owner (kira-personal-tone-
20261007 P5). ``""`` (the default) means "unknown": no prompt line at all, so
the prompt is byte-identical to the pre-feature output.
"""

from __future__ import annotations

OWNER_GENDERS: tuple[str, ...] = ("", "masculine", "feminine", "neutral")
DEFAULT_OWNER_GENDER = ""


def normalize_owner_gender(value: object) -> str:
    """The value named by ``value``, or ``""`` for anything unknown."""
    if not isinstance(value, str):
        return DEFAULT_OWNER_GENDER
    v = value.strip().lower()
    return v if v in OWNER_GENDERS else DEFAULT_OWNER_GENDER
