"""Strip a pasted-image description block from owner turn text.

The chat route folds the vision model's description (which can contain OCR'd
text, e.g. viewer chat visible in a screenshot) into the owner's turn as a
delimited block (``chat_image.compose_turn_text``). The model needs it for the
current turn, but it must never become memory: not a memoria draft, not the
v5 evidence journal, not a digest line. Callers strip it before any capture.

Pure, no I/O.
"""

from __future__ import annotations

import re
from typing import Iterable

# Both shipped locale delimiters (en + the legacy es literal), so a block
# composed under one locale is still stripped after a locale switch.
# The block may be CUT before its closing tag (history clips turns to 800
# chars), so an unterminated block runs to the end of the text.
_GENERIC_RE = re.compile(
    r"\[(?:PASTED IMAGE|IMAGEN PEGADA)\b.*?"
    r"(?:\[/(?:PASTED IMAGE|IMAGEN PEGADA)\]|\Z)",
    re.IGNORECASE | re.DOTALL,
)


def _template_patterns() -> Iterable["re.Pattern[str]"]:
    """Patterns derived from the ACTIVE locale's block template, if any."""
    try:
        from opencohost.i18n import active as i18n_active

        template = i18n_active.pasted_image_block()
    except Exception:
        return ()
    head, sep, tail = template.partition("{description}")
    if not sep:
        return ()
    head, tail = head.strip(), tail.strip()
    if not head:
        return ()
    closing = re.escape(tail) + "|" if tail else ""
    return (re.compile(re.escape(head) + r".*?(?:" + closing + r"\Z)", re.DOTALL),)


def has_pasted_image_block(text: str) -> bool:
    if not text:
        return False
    if _GENERIC_RE.search(text):
        return True
    return any(p.search(text) for p in _template_patterns())


def strip_pasted_image_block(text: str) -> str:
    """The owner's own words with every pasted-image block removed.

    Returns ``""`` for an image-only turn. Text without a block is returned
    unchanged (same object).
    """
    if not text or not has_pasted_image_block(text):
        return text
    out = _GENERIC_RE.sub("", text)
    for pattern in _template_patterns():
        out = pattern.sub("", out)
    return out.strip()
