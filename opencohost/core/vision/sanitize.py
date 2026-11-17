"""Neutralize vision descriptions before they enter the prompt.

A vision description is untrusted data (the block frames it as Kira's own sight): the vision model reads on-screen text
(chat windows, web pages, game HUDs) and can echo it verbatim. Anything that
looks like a role switch, a chat-template token, an instruction heading, an
@mention or the screen-block delimiter itself is stripped, then the text is
capped and wrapped in a fixed delimited block.

The pipeline runs to a fixed point so ``sanitize_description`` is idempotent:
removing one token can never splice together a new one that survives.
"""

from __future__ import annotations

import re
import unicodedata

from opencohost.core.context.injection_markers import _strip_injection_markers

# V1.22: first-person sight. The block is still sanitized; the prompt assembler's
# SCREEN_CONTEXT_INSTRUCTION tells Kira that on-screen text is content, never orders.
SCREEN_BLOCK_OPEN = "[SCREEN - what you see on the owner's screen right now, with your own eyes]"
SCREEN_BLOCK_CLOSE = "[/SCREEN]"

# V1.21: room for the transcribed on-screen text (was 300).
DEFAULT_MAX_CHARS = 1200
_ELLIPSIS = "..."
_MAX_PASSES = 8

# C0/C1 controls except whitespace (collapsed later), plus zero-width and
# bidi-control characters that can hide a marker from substring matching.
_INVISIBLE_RE = re.compile(
    "[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f-\u009f"
    "­​-‏‪-‮⁠-⁤﻿]"
)
_TEMPLATE_TOKEN_RE = re.compile(r"<\|[^<>]*?\|>")
_TEMPLATE_RESIDUE_RE = re.compile(r"<\||\|>")
_DELIMITER_RE = re.compile(r"\[\s*/?\s*screen\b[^\]]*\]?", re.IGNORECASE)
_HEADING_RE = re.compile(r"^\s*#{1,6}\s*(.*)$")
_INSTRUCTION_HEADING_RE = re.compile(
    r"\b(instructions?|instrucci[oó]n(es)?|rules?|reglas?|system|sistema|prompt|"
    r"directives?|directivas?|commands?|comandos?|role|rol)\b",
    re.IGNORECASE,
)
_ROLE_PREFIX_RE = re.compile(
    r"(?<!\w)(system|assistant|user|developer|human|tool|sistema|asistente|usuario)\s*:",
    re.IGNORECASE,
)
_MENTION_RE = re.compile(r"(?<!\w)@\w[\w.-]*")


def _drop_headings(text: str) -> str:
    lines = []
    for line in text.splitlines():
        match = _HEADING_RE.match(line)
        if match is None:
            lines.append(line)
            continue
        heading = match.group(1)
        if _INSTRUCTION_HEADING_RE.search(heading):
            continue
        lines.append(heading)
    return "\n".join(lines)


def _single_pass(text: str) -> str:
    text = _INVISIBLE_RE.sub("", text)
    text = _TEMPLATE_TOKEN_RE.sub(" ", text)
    text = _TEMPLATE_RESIDUE_RE.sub(" ", text)
    text = _DELIMITER_RE.sub(" ", text)
    # Any bracket left over could start a forged block header; keep the
    # content readable but structurally inert.
    text = text.replace("[", "(").replace("]", ")")
    text = _drop_headings(text)
    text = _ROLE_PREFIX_RE.sub(" ", text)
    text = _MENTION_RE.sub(" ", text)
    return _strip_injection_markers(text)


def _cap(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    budget = max(max_chars - len(_ELLIPSIS), 0)
    head = text[: budget + 1]
    cut = head.rfind(" ")
    head = head[:cut] if cut > 0 else text[:budget]
    return head.rstrip(" ,;:.-") + _ELLIPSIS


def sanitize_description(text: str, max_chars: int = DEFAULT_MAX_CHARS) -> str:
    """Return *text* neutralized for prompt use, capped at *max_chars*."""
    if not text:
        return ""
    # NFKC folds fullwidth look-alikes (e.g. U+FF3B for "[") onto ASCII so
    # they cannot dodge the delimiter and template patterns. Accented letters
    # survive (they compose, not decompose).
    current = unicodedata.normalize("NFKC", text)
    for _ in range(_MAX_PASSES):
        cleaned = _single_pass(current)
        if cleaned == current:
            break
        current = cleaned
    return _cap(current, max_chars)


def build_screen_block(description: str, max_chars: int = DEFAULT_MAX_CHARS) -> str:
    """Wrap a description in the fixed screen block; empty in, empty out.

    The body is re-sanitized here (idempotent) so a caller can never hand an
    unsanitized string that closes the block early.
    """
    body = sanitize_description(description, max_chars)
    if not body:
        return ""
    return f"{SCREEN_BLOCK_OPEN}\n{body}\n{SCREEN_BLOCK_CLOSE}"
