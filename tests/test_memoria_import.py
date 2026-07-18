"""Tests for opencohost.core.memoria_import — pure external-AI export parser.

Design contract (engram sdd/memoria_import_20260718/design, "Parser rules"):
a pure, no-I/O module that turns an external-AI export (Gemini-shaped or
generic markdown/text) into a flat list of ParsedItem(content, section). Format
is auto-detected: `^##\\s*\\d+\\.` anywhere → Gemini rules, else generic. The
module NEVER rejects or truncates — the 100-item / 64 KB caps and the
is_capturable / cap gates live in the WU3 route; this module only parses.

PII policy (CRITICAL, binding): every fixture here is SYNTHETIC — fake names
("Ana Test") and fake facts. The owner's REAL Gemini export is orchestrator-only
and must never be read, quoted, or committed by an apply agent.
"""

from __future__ import annotations

from opencohost.config.settings import MEMORIAS_IMPORT_ITEM_CHARS
from opencohost.core.memoria_import import (
    ParsedItem,
    parse_import,
    parse_source_hint,
)


# ---------------------------------------------------------------------------
# Synthetic fixtures (fake names/facts only — never the owner's real export)
# ---------------------------------------------------------------------------

_GEMINI_EXPORT = """\
## 1. Intereses
* A Ana Test le gusta el jazz y la fotografia analogica.
* Prueba: Lo comento en varias charlas del stream. Fechas: [2024-01-02]
* Ana colecciona vinilos de synthwave editados en tirada limitada.
* Prueba: Mostro la coleccion al aire. Fechas: [2024-03-15, 2024-04-01]

## 2. Demografia
* Ana Test vive en la ciudad ficticia de Testville.
* Prueba: Referencia geografica reiterada. Fechas: [2023-11-20]

Importado de: Gemini.
"""


_GENERIC_EXPORT = """\
# Gustos
- Le gusta el cafe fuerte por la manana.
- Prefiere te verde por la tarde.

Trabaja como disenadora grafica freelance.
Vive con dos gatos llamados Pixel y Vector.

# Rutina
1. Corre cinco kilometros cada dia antes del stream.
2. Lee un rato antes de dormir siempre.
"""


# ---------------------------------------------------------------------------
# 1.1 Gemini mode — claim + Prueba folding, section tracking, source hint
# ---------------------------------------------------------------------------

def test_gemini_claims_fold_prueba_to_dates_and_track_sections() -> None:
    items = parse_import(_GEMINI_EXPORT)

    # Three claims — the three `* <claim>` bullets; the `* Prueba:` bullets and
    # the trailing "Importado de:" line are NEVER items.
    assert len(items) == 3
    assert all(isinstance(it, ParsedItem) for it in items)

    # Claim 1: Prueba prose dropped, only the Fechas date folded in.
    assert items[0].section == "Intereses"
    assert items[0].content == (
        "A Ana Test le gusta el jazz y la fotografia analogica. (fechas: 2024-01-02)"
    )
    assert "Prueba" not in items[0].content
    assert "charlas" not in items[0].content  # evidence prose gone

    # Claim 2: multiple dates preserved verbatim inside the fold.
    assert items[1].section == "Intereses"
    assert items[1].content.endswith("(fechas: 2024-03-15, 2024-04-01)")

    # Claim 3: section reset by the second "## N." header.
    assert items[2].section == "Demografia"
    assert items[2].content.startswith("Ana Test vive en la ciudad ficticia de Testville.")

    # "Importado de: Gemini." never becomes an item.
    assert not any("Importado de" in it.content for it in items)


def test_gemini_source_hint_parsed_alongside_items() -> None:
    # The parsed source hint is returned alongside the items (the route's
    # explicit source_label always wins — this is informational only).
    assert parse_source_hint(_GEMINI_EXPORT) == "Gemini"
    # No trailing hint line → empty string, never an exception.
    assert parse_source_hint("* solo un dato suelto sin fuente declarada") == ""


def test_gemini_multi_bracket_fechas_preserves_all_dates() -> None:
    # The owner's real export writes multiple dates as SEPARATE bracket groups
    # ("[d1] y [d2]"), not one comma-joined list. The parser must keep ALL of
    # them (D4: dates are cheap temporal grounding) — the old "stop at the first
    # ']'" regex kept only the first date and silently dropped the rest.
    export = (
        "## 1. Historia\n"
        "* Ana Test se mudo de ciudad dos veces distintas.\n"
        "* Prueba: Lo conto en dos streams. Fechas: [2026-02-01] y [2026-06-01]\n"
    )
    items = parse_import(export)
    assert len(items) == 1
    assert items[0].content.endswith("(fechas: 2026-02-01, 2026-06-01)")


# ---------------------------------------------------------------------------
# 1.2 Generic mode — bullets/numbered = item each, paragraph = one item,
#     headers set section and are never items
# ---------------------------------------------------------------------------

def test_generic_mode_bullets_paragraphs_and_headers() -> None:
    items = parse_import(_GENERIC_EXPORT)

    # 2 Gustos bullets + 1 paragraph item + 2 Rutina numbered = 5 items.
    assert len(items) == 5

    assert items[0].content == "Le gusta el cafe fuerte por la manana."
    assert items[0].section == "Gustos"
    assert items[1].content == "Prefiere te verde por la tarde."

    # A no-bullet block collapses to ONE paragraph item (both lines joined),
    # inheriting the current section.
    assert items[2].content == (
        "Trabaja como disenadora grafica freelance. "
        "Vive con dos gatos llamados Pixel y Vector."
    )
    assert items[2].section == "Gustos"

    # Numbered bullets under the next header become items, section reset.
    assert items[3].content == "Corre cinco kilometros cada dia antes del stream."
    assert items[3].section == "Rutina"
    assert items[4].section == "Rutina"

    # No `#`-header ever leaks into an item.
    assert not any(it.content.startswith("#") for it in items)
    assert not any(it.content in {"Gustos", "Rutina"} for it in items)


# ---------------------------------------------------------------------------
# 1.3 Clip at word boundary; never truncates the item list; empty → []
# ---------------------------------------------------------------------------

def test_per_item_clip_is_word_boundary_bounded() -> None:
    # A single long generic paragraph (~480 chars) clips to <=300 chars at a
    # word boundary — never mid-word, never a partial token.
    para = "palabra " * 60
    items = parse_import(para)

    assert len(items) == 1
    assert len(items[0].content) <= MEMORIAS_IMPORT_ITEM_CHARS
    assert items[0].content.split() and all(tok == "palabra" for tok in items[0].content.split())


def test_parser_never_truncates_over_100_items() -> None:
    # The module returns EVERY parsed item — the 100-item honest-refusal cap is
    # the route's job (WU3). Silent truncation here would hide data loss.
    text = "\n".join(f"* dato numero {i} sobre el perfil sintetico" for i in range(150))
    items = parse_import(text)
    assert len(items) == 150


def test_empty_and_whitespace_input_yield_no_items() -> None:
    assert parse_import("") == []
    assert parse_import("   \n\n  \t \n ") == []
    assert parse_source_hint("") == ""


def test_parser_strips_control_bytes_from_claims() -> None:
    # Untrusted import text may carry NUL/control bytes. Python's sqlite3 raises
    # ValueError (NOT sqlite3.Error) on an embedded NUL, which would escape
    # insert_imported's fail-open sqlite3.Error catch. The parser strips C0
    # control chars (except \t/\n, handled by whitespace collapse) so no control
    # byte ever leaves the parser toward the store.
    items = parse_import("* le gusta el\x00 jazz\x07 y la\x1f fotografia analogica\n")
    assert len(items) == 1
    assert "\x00" not in items[0].content
    assert not any(ord(ch) < 0x20 for ch in items[0].content)  # \t/\n already collapsed
    assert "le gusta el jazz y la fotografia analogica" in items[0].content
