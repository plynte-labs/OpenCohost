"""Contract tests: status_bar.py must reference design tokens, not raw hex.

Design-system migration (ui-design-system-20260625). Mirrors
tests/test_model_panel_no_raw_hex.py but adds VALUE-level pinning: every
palette-dict value must equal the corresponding ``theme.*`` token, and every
flagged consolidation shift must resolve to its intended token.

Non-vacuity:
- The source-scan test fails (printing line numbers) if ANY raw 6-digit hex
  literal survives in status_bar.py.
- The value-pinning tests fail if a palette entry points at a stale hex instead
  of the token (proves the module references tokens, not copies).
- The consolidation-shift tests fail if a flagged shift did not land on the
  intended token (proves each visual shift is intentional and located).

These tests import the REAL palette dicts and drive the REAL pure methods
(``_get_status_color``, ``_engine_status_text``) headless — no Tk display, no
over-mocking.
"""

from __future__ import annotations

import pathlib
import re

from opencohost.ui import theme
from opencohost.ui import status_bar as sb


# ---------------------------------------------------------------------------
# Source-scan guard — strongest non-vacuity proof
# ---------------------------------------------------------------------------

_PANEL_PATH = (
    pathlib.Path(__file__).parent.parent / "opencohost" / "ui" / "status_bar.py"
)

# Hex color literal pattern — matches #RGB and #RRGGBB
_HEX_RE = re.compile(r"#[0-9a-fA-F]{3}(?:[0-9a-fA-F]{3})?")


class TestStatusBarNoRawHex:
    def test_panel_file_exists(self):
        assert _PANEL_PATH.exists(), f"Panel not found at: {_PANEL_PATH}"

    def test_panel_imports_theme(self):
        source = _PANEL_PATH.read_text(encoding="utf-8")
        assert (
            "from opencohost.ui import theme" in source
            or "from opencohost.ui.theme" in source
        ), "status_bar.py must import theme tokens."

    def test_no_raw_hex_color_literals(self):
        """No raw #RRGGBB may survive in non-comment source lines.

        Comment lines (starting with ``#``) are exempt because they document
        the OLD pre-migration value next to each consolidation shift.
        """
        source = _PANEL_PATH.read_text(encoding="utf-8")
        lines = source.splitlines()
        offenses: list[str] = []
        for lineno, line in enumerate(lines, start=1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue  # comment lines may document old values
            # Strip any trailing inline comment so a documented "→ #oldhex"
            # note does not count as a live literal.
            code_part = line.split("#", 1)[0] if "  #" in line else line
            # Re-include genuine hex literals only from the code portion: we
            # detect inline comments by the "  #" (two-space) convention used
            # throughout this file.
            if "  #" in line:
                code_part = line[: line.index("  #")]
            for match in _HEX_RE.finditer(code_part):
                offenses.append(
                    f"  Line {lineno}: {line.strip()!r}  -> match: {match.group()!r}"
                )

        assert not offenses, (
            f"status_bar.py contains {len(offenses)} raw hex color literal(s) "
            f"(design-system contract VIOLATED — migrate to theme tokens):\n"
            + "\n".join(offenses)
        )


# ---------------------------------------------------------------------------
# Value pinning — EXACT matches (no visual change)
# Each palette value must BE the token value, proving a token reference.
# ---------------------------------------------------------------------------


class TestPaletteValuesAreTokens:
    def test_model_status_colors_pinned(self):
        assert sb._MODEL_STATUS_COLORS == {
            "loading": theme.WARNING,
            "ready": theme.SUCCESS,
            "error": theme.DANGER,
            "offline": theme.NEUTRAL_HOVER,
        }

    def test_mic_status_colors_pinned(self):
        assert sb._MIC_STATUS_COLORS == {
            "disconnected": theme.MIC_OFFLINE_BG,
            "idle": theme.SURFACE_INSET,
            "listening": theme.SUCCESS_DIM,
            "recording": theme.WARNING,
        }

    def test_tts_status_colors_pinned(self):
        assert sb._TTS_STATUS_COLORS == {
            "idle": theme.SURFACE_INSET,
            "generating": theme.INFO,
            "speaking": theme.INFO,        # consolidation: #1f526f -> INFO
            "paused": theme.WARNING,       # consolidation: #ffaa00 -> WARNING
            "error": theme.DANGER,
        }

    def test_chat_status_colors_pinned(self):
        assert sb._CHAT_STATUS_COLORS == {
            "disconnected": theme.SURFACE_INSET,
            "connecting": theme.WARNING,
            "connected": theme.SUCCESS_DIM,
            "error": theme.DANGER,
        }

    def test_health_status_colors_pinned(self):
        assert sb._HEALTH_STATUS_COLORS == {
            "unknown": theme.NEUTRAL_HOVER,
            "green": theme.SUCCESS,
            "yellow": theme.WARNING,
            "red": theme.DANGER,
        }

    def test_engine_status_colors_pinned(self):
        assert sb._ENGINE_STATUS_COLORS == {
            "qwen_active": theme.SUCCESS_DIM,
            "edge_fallback": theme.WARNING,
            "qwen_starting": theme.INFO,   # DEAD value (overridden at runtime); tokenized faithfully
            "not_configured": theme.NEUTRAL,
            "piper_local": theme.SUCCESS_DIM,
            "unknown": theme.NEUTRAL_HOVER,
        }

    def test_rollup_config_pinned(self):
        assert sb._ROLLUP_CONFIG == {
            "OK": ("Sistema: OK", theme.SURFACE_INSET),
            "QUIET": ("Sistema: ...", theme.NEUTRAL_DIM),
            "INFO": ("Sistema: activo", theme.INFO),
            "WARN": ("Sistema: alerta", theme.WARNING),
            "CRIT": ("Sistema: error", theme.DANGER),
        }


# ---------------------------------------------------------------------------
# Consolidation shifts — each flagged shift must land on its intended token.
# These FAIL if a shift was applied to the wrong token (intent enforcement).
# ---------------------------------------------------------------------------


class TestPipelineConsolidationShifts:
    def test_pipeline_display_pinned(self):
        assert sb._PIPELINE_DISPLAY == {
            "idle": ("Modelo listo", theme.SUCCESS),            # #44cc66 -> SUCCESS
            "listening": ("Micrófono escuchando", theme.SUCCESS),  # #44ff44 -> SUCCESS
            "processing": ("Modelo procesando", theme.WARNING),    # #ffaa00 -> WARNING
            "speaking": ("TTS renderizando voz", theme.INFO_BRIGHT),  # #4488ff exact
            "playing": ("TTS hablando", theme.INFO_BRIGHT),        # #44ccff -> INFO_BRIGHT
            "downloading": ("Modelo cargando", theme.WARNING),     # #ff8800 -> WARNING
            "init": ("Modelo cargando", theme.NEUTRAL_HOVER),      # #888888 -> NEUTRAL_HOVER
            "error": ("Modelo error", theme.DANGER),               # #ff5555 -> DANGER
        }


# ---------------------------------------------------------------------------
# Real-surface drive — pure methods return token values, not stale hex.
# ---------------------------------------------------------------------------


class TestRealSurfaceColors:
    def _bare(self):
        """A StatusBar instance with no Tk widgets — pure methods only."""
        bar = sb.StatusBar.__new__(sb.StatusBar)
        return bar

    def test_get_status_color_uses_tokens(self):
        bar = self._bare()
        assert bar._get_status_color("model_status", "ready") == theme.SUCCESS
        assert bar._get_status_color("mic_status", "disconnected") == theme.MIC_OFFLINE_BG
        assert bar._get_status_color("tts_status", "speaking") == theme.INFO
        assert bar._get_status_color("chat_status", "connected") == theme.SUCCESS_DIM

    def test_get_status_color_default_is_surface_inset(self):
        bar = self._bare()
        assert bar._get_status_color("nonexistent", "x") == theme.SURFACE_INSET

    def test_engine_status_text_unchanged(self):
        bar = self._bare()
        # behavior must be identical post-migration (text is unaffected by tokens)
        assert bar._engine_status_text("qwen_active") == "Voz: Qwen clonada"
        assert bar._engine_status_text("edge_fallback") == "Voz: Edge respaldo"
        assert bar._engine_status_text("unknown") == "Voz: iniciando…"


# ---------------------------------------------------------------------------
# New token — MIC_OFFLINE_BG must exist and stay distinct from DANGER_DIM.
# ---------------------------------------------------------------------------


class TestNewMicOfflineToken:
    def test_mic_offline_bg_present(self):
        assert hasattr(theme, "MIC_OFFLINE_BG"), "theme.MIC_OFFLINE_BG token missing"

    def test_mic_offline_bg_is_hex(self):
        assert re.match(r"^#[0-9a-fA-F]{6}$", theme.MIC_OFFLINE_BG)

    def test_mic_offline_distinct_from_danger_dim(self):
        # Distinct semantic role: dim warning-red mic pill vs destructive-action pill bg.
        assert theme.MIC_OFFLINE_BG != theme.DANGER_DIM
