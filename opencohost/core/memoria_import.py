"""Pure parser for external-AI memory exports (memoria_import_20260718).

Turns an external-AI export — the owner's Gemini "lo que sé de ti" dump, or a
generic markdown/plain-text list — into a flat list of ``ParsedItem``
(``content`` + ``section`` context). Pure: no I/O, no store access, no config
mutation, never raises for shape. The WU3 route owns every rejection (64 KB /
100-item honest-refusal 422s) and every store gate (``is_capturable`` /
``derive_import_key`` / cap); this module ONLY parses and clips.

Design decisions this file implements:
  - D4: keep the claim + its ``Fechas:`` dates, DROP the ``Prueba:`` evidence
    prose (dates are cheap temporal grounding; prose is bytes, not recall).
  - Per-item clip to ``MEMORIAS_IMPORT_ITEM_CHARS`` at a word boundary (one
    imported row is already ~half the 700-char inject budget).
  - No silent truncation of the item LIST — the route refuses oversize inputs
    honestly (422); truncating here would hide data loss.

Format detection: ``^##\\s*\\d+\\.`` anywhere in the text selects Gemini rules
(the export's "## N. Sección" section headers); otherwise generic rules.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from opencohost.config.settings import MEMORIAS_IMPORT_ITEM_CHARS


@dataclass(frozen=True)
class ParsedItem:
    """One parsed claim ready for the route's gates.

    ``content`` is the claim text (with any folded ``(fechas: …)`` suffix),
    clipped to ``MEMORIAS_IMPORT_ITEM_CHARS`` at a word boundary. ``section`` is
    the current header text ("" when none) — signature context only, never
    stored as its own field.
    """

    content: str
    section: str


# Gemini "## N. Sección" section header.
_GEMINI_HEADER = re.compile(r"##\s*\d+\.\s*(.*)")
# A leading bullet marker ("* ", "- ", "N. ") + the rest of the line.
_BULLET = re.compile(r"[*\-]\s+(.*)|\d+\.\s+(.*)")
# A "Prueba:" bullet — metadata for the PREVIOUS Gemini item, never its own item.
_PRUEBA = re.compile(r"[*\-]\s*Prueba:\s*(.*)", re.IGNORECASE)
# Dates on a Prueba line, after a "Fechas:" anchor. The owner's real export
# writes multiple dates as SEPARATE bracket groups ("[2026-02-01] y
# [2026-06-01]"); the single-bracket comma variant ("[2024-03-15, 2024-04-01]")
# is one group. _fold_dates collects ALL groups after the anchor and joins them,
# so no date is dropped (D4) — a bare "[^\]\n]*" match stops at the FIRST ']'
# and silently loses every date after the first bracket.
_FECHAS_ANCHOR = re.compile(r"Fechas:", re.IGNORECASE)
_FECHAS_GROUP = re.compile(r"\[([^\]\n]+)\]")
# C0 control bytes EXCEPT \t/\n (whitespace collapse already handles those).
# Stripped from every parsed claim before it leaves the parser: Python sqlite3
# raises ValueError (NOT sqlite3.Error) on an embedded NUL, which would escape
# the store's fail-open sqlite3.Error catch — so the NUL must never get there.
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f]")
# Trailing "Importado de: X" source hint (informational — route's label wins).
_SOURCE_HINT = re.compile(r"Importado de:\s*(\w+)", re.IGNORECASE)
# Gemini-format sniff: a "## N." numbered section header anywhere.
_IS_GEMINI = re.compile(r"^##\s*\d+\.", re.MULTILINE)


def _fold_dates(prueba_text: str) -> str:
    """All bracketed date groups after a "Fechas:" anchor, joined with ", ".

    Multiple dates written as separate bracket groups ("[2026-02-01] y
    [2026-06-01]") and the single-bracket comma variant ("[2024-03-15,
    2024-04-01]") both collapse to a flat comma-joined string (D4: keep every
    date, drop the evidence prose). "" when the line has no "Fechas:" anchor or
    no bracketed dates after it.
    """
    anchor = _FECHAS_ANCHOR.search(prueba_text)
    if anchor is None:
        return ""
    groups = (g.strip() for g in _FECHAS_GROUP.findall(prueba_text[anchor.end():]))
    return ", ".join(g for g in groups if g)


def _clip_item(text: str, limit: int = MEMORIAS_IMPORT_ITEM_CHARS) -> str:
    """Clip *text* to at most *limit* chars, never cutting a word in half.

    Strips C0 control bytes (except \\t/\\n, handled by the whitespace collapse)
    from the untrusted import text first — see _CONTROL_CHARS.
    """
    text = _CONTROL_CHARS.sub("", text)
    text = " ".join(text.split())  # collapse internal whitespace/newlines
    if len(text) <= limit:
        return text
    cut = text[:limit]
    space = cut.rfind(" ")
    if space > 0:
        cut = cut[:space]
    return cut.rstrip()


def _bullet_claim(line: str) -> str | None:
    """The claim text of a bullet/numbered line, or None when *line* is not one."""
    match = _BULLET.match(line)
    if match is None:
        return None
    return (match.group(1) if match.group(1) is not None else match.group(2)).strip()


def parse_source_hint(text: str) -> str:
    """The declared source from a trailing "Importado de: X" line, else "".

    Returned alongside the parsed items so the caller CAN see the export's own
    claim of origin — but the route's explicit ``source_label`` always wins, so
    this is informational only. Pure; never raises.
    """
    match = _SOURCE_HINT.search(text or "")
    return match.group(1) if match else ""


def _parse_gemini(text: str) -> list[ParsedItem]:
    """Gemini export rules — see module docstring D4."""
    items: list[list[str]] = []  # [content, section] pairs (mutable while folding)
    section = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        header = _GEMINI_HEADER.match(line)
        if header is not None:
            section = header.group(1).strip()
            continue
        prueba = _PRUEBA.match(line)
        if prueba is not None:
            if items:  # fold dates into the previous item; drop the prose
                dates = _fold_dates(prueba.group(1))
                if dates:
                    items[-1][0] = f"{items[-1][0]} (fechas: {dates})"
            continue
        if _SOURCE_HINT.match(line):
            continue  # source hint line — parsed separately, never an item
        claim = _bullet_claim(line)
        if claim:
            items.append([claim, section])
    return [ParsedItem(_clip_item(content), section) for content, section in items]


def _parse_generic(text: str) -> list[ParsedItem]:
    """Generic markdown/text rules — blank-line blocks; bullets/numbered are one
    item each; a bullet-free block collapses to one paragraph item; ``#`` headers
    set the section and are never items."""
    items: list[ParsedItem] = []
    section = ""
    for block in re.split(r"\n\s*\n", text):
        lines = [ln for ln in block.splitlines() if ln.strip()]
        if not lines:
            continue
        has_bullet = any(_bullet_claim(ln.strip()) is not None for ln in lines)
        paragraph: list[str] = []
        for raw in lines:
            line = raw.strip()
            if line.startswith("#"):
                section = line.lstrip("#").strip()
                continue
            claim = _bullet_claim(line)
            if claim is not None:
                if claim:
                    items.append(ParsedItem(_clip_item(claim), section))
                continue
            if has_bullet:
                # Stray non-bullet line inside a bulleted block: keep it as its
                # own item rather than silently dropping content.
                items.append(ParsedItem(_clip_item(line), section))
            else:
                paragraph.append(line)
        if paragraph:
            items.append(ParsedItem(_clip_item(" ".join(paragraph)), section))
    return items


def parse_import(text: str) -> list[ParsedItem]:
    """Parse *text* into ordered ParsedItems. Empty/whitespace → []. Never
    raises, never truncates the list (the route owns the 100-item / 64 KB caps).
    """
    if not text or not text.strip():
        return []
    if _IS_GEMINI.search(text):
        return _parse_gemini(text)
    return _parse_generic(text)


if __name__ == "__main__":  # ponytail: one runnable check, no framework
    gemini = (
        "## 1. Intereses\n"
        "* Le gusta el jazz.\n"
        "* Prueba: Lo dijo al aire. Fechas: [2024-01-02]\n"
        "Importado de: Gemini.\n"
    )
    got = parse_import(gemini)
    assert len(got) == 1, got
    assert got[0].content == "Le gusta el jazz. (fechas: 2024-01-02)", got[0].content
    assert got[0].section == "Intereses", got[0].section
    assert parse_source_hint(gemini) == "Gemini"
    # Owner's real multi-bracket shape: every date kept, NUL stripped.
    multi = (
        "## 1. Historia\n"
        "* dato\x00 real del perfil\n"
        "* Prueba: p. Fechas: [2026-02-01] y [2026-06-01]\n"
    )
    got_multi = parse_import(multi)
    assert got_multi[0].content == "dato real del perfil (fechas: 2026-02-01, 2026-06-01)", got_multi[0].content
    assert parse_import("a\nb\n\nc") == [ParsedItem("a b", ""), ParsedItem("c", "")]
    assert parse_import("  ") == []
    print("memoria_import self-check OK")
