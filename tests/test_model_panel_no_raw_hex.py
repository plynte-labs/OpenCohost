"""Contract test: model_panel.py must not contain raw hex literals and must
use the Layer 2 ``styles`` factory (design-system Phase 2).

Mirrors tests/test_music_panel_no_raw_hex.py. Reads the SOURCE FILE directly
(not the runtime object) so a hard-coded ``#RRGGBB`` is caught at CI time even
when the rendered UI looks fine.

Non-vacuity: on failure the test prints every offending literal with its line
number, so the reviewer sees exactly what leaked through.
"""

from __future__ import annotations

import pathlib
import re

# Path to the migrated panel under test (absolute, Windows-safe)
_PANEL_PATH = (
    pathlib.Path(__file__).parent.parent / "opencohost" / "ui" / "model_panel.py"
)

# Hex color literal pattern — matches #RGB and #RRGGBB
_HEX_RE = re.compile(r"#[0-9a-fA-F]{3}(?:[0-9a-fA-F]{3})?")


class TestModelPanelNoRawHex:
    def test_panel_file_exists(self):
        assert _PANEL_PATH.exists(), f"Panel not found at: {_PANEL_PATH}"

    def test_panel_imports_theme(self):
        source = _PANEL_PATH.read_text(encoding="utf-8")
        assert (
            "from opencohost.ui import theme" in source
            or "from opencohost.ui.theme" in source
            or "from opencohost.ui import styles, theme" in source
            or "from opencohost.ui import theme, styles" in source
        ), "model_panel.py must import theme tokens."

    def test_panel_imports_styles(self):
        """Migrated panel must consume the Layer 2 component-styles factory."""
        source = _PANEL_PATH.read_text(encoding="utf-8")
        assert "styles" in source and "from opencohost.ui import" in source, (
            "model_panel.py must import the styles module (Layer 2). "
            "The whole point of Phase 2 is panels stop repeating widget "
            "construction and delegate to styles.* factories."
        )
        assert "styles." in source, (
            "model_panel.py imports styles but never calls a styles.* factory."
        )

    def test_no_raw_hex_color_literals(self):
        source = _PANEL_PATH.read_text(encoding="utf-8")
        lines = source.splitlines()
        offenses: list[str] = []
        for lineno, line in enumerate(lines, start=1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue  # comment lines may document old values
            for match in _HEX_RE.finditer(line):
                offenses.append(
                    f"  Line {lineno}: {line.strip()!r}  → match: {match.group()!r}"
                )

        assert not offenses, (
            f"model_panel.py contains {len(offenses)} raw hex color literal(s) "
            f"(Phase 2 contract VIOLATED — migrate these to theme tokens / styles):\n"
            + "\n".join(offenses)
        )
