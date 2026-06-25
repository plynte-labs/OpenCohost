"""Contract test: music_panel.py must not contain raw hex color literals.

This test reads the SOURCE FILE directly (not the runtime object) and asserts
that no `#RRGGBB` or `#RGB` literal appears outside the token import. This is
the Phase 1 pilot contract for the design-token migration.

Non-vacuity guarantee: the test prints all raw-hex matches when it fails so
the reviewer can see exactly what leaked through.

Why this approach: source-level regex is intentionally strict — if a developer
accidentally hard-codes a hex value, this test catches it at CI time even if
the runtime behavior is visually correct.
"""

from __future__ import annotations

import pathlib
import re

import pytest

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Path to the pilot panel under test (absolute, Windows-safe)
_PANEL_PATH = (
    pathlib.Path(__file__).parent.parent / "opencohost" / "ui" / "music_panel.py"
)

# Hex color literal pattern — matches #RGB and #RRGGBB
_HEX_RE = re.compile(r"#[0-9a-fA-F]{3}(?:[0-9a-fA-F]{3})?")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestMusicPanelNoRawHex:
    def test_pilot_panel_file_exists(self):
        """Sanity: the pilot file exists so subsequent tests are meaningful."""
        assert _PANEL_PATH.exists(), f"Pilot panel not found at: {_PANEL_PATH}"

    def test_pilot_imports_theme(self):
        """music_panel.py must import from opencohost.ui.theme."""
        source = _PANEL_PATH.read_text(encoding="utf-8")
        assert "from opencohost.ui import theme" in source or "from opencohost.ui.theme" in source, (
            "music_panel.py does not import theme. "
            "All color values must reference theme tokens, not raw literals."
        )

    def test_no_raw_hex_color_literals(self):
        """music_panel.py must contain zero raw hex color literals.

        If this test fails it prints all offending literals with line numbers
        so the reviewer knows exactly what needs fixing (non-vacuous proof).
        """
        source = _PANEL_PATH.read_text(encoding="utf-8")
        lines = source.splitlines()
        offenses: list[str] = []
        for lineno, line in enumerate(lines, start=1):
            # Skip comment lines — they may document old values for reference
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            for match in _HEX_RE.finditer(line):
                offenses.append(
                    f"  Line {lineno}: {line.strip()!r}  → match: {match.group()!r}"
                )

        assert not offenses, (
            f"music_panel.py contains {len(offenses)} raw hex color literal(s) "
            f"(pilot contract VIOLATED — migrate these to theme tokens):\n"
            + "\n".join(offenses)
        )
