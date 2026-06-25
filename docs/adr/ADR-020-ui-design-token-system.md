# ADR-020: UI Design-Token System — Phase 1 Foundation

**Date**: 2026-06-25
**Status**: Accepted
**Branch**: `feat/ui-design-system-20260625`
**Author**: Claude Code (sdd-apply executor, Phase 1)
**Scope**: `opencohost/ui/theme.py` (new) · `opencohost/ui/music_panel.py` (pilot migration) · `tests/test_theme_tokens.py` · `tests/test_music_panel_no_raw_hex.py`

---

## Context

ADR-SD-003 (design audit) identified **382 raw hex literals across 16 UI files** with no shared
design system. Concrete problems:

- Three different "danger" reds: `#cc3333` (status_bar), `#8f2f2f` (music_panel, delete
  buttons), and the Tk named color `"darkred"` (voice_control, smart_aggregator).
- Five background tints that should be the same surface level: `#0f151c`, `#10161d`, `#101923`,
  `#151d26`, `#162232`.
- Two "success" greens (`#22cc66`, `#44cc66`) and two separate success-dim backgrounds
  (`#1f5a3a`).
- Tailwind-palette interlopers in `avatar_panel.py` (`#4ade80` / `#f87171`) matching nothing else.
- Eleven distinct font sizes (9–24) with no named scale.
- Ad-hoc `padx`/`pady` literals (`2`, `4`, `8`, `10`, `12`, `14`, `(0, 8)`, `(4, 12)`, …)
  with no shared step table.

**This ADR documents Phase 1 only**: the token module and one pilot panel migration as proof.
Visual refinement (palette tuning, spacing tightening, typography polish) is a **separate later
phase** requiring explicit owner review.

---

## Decision

### 1. Token module: `opencohost/ui/theme.py`

A pure, dependency-light module exposing named constants (CSS/Tailwind-variable style). All raw
color/size literals belong in this file only; call sites reference tokens by name.

#### Color taxonomy

| Token | Value | Replaces / Notes |
|-------|-------|------------------|
| `BG` | `#0f151c` | Root canvas / scroll-frame bg (was 5 similar tints) |
| `SURFACE` | `#151d26` | Cards, panel frames |
| `SURFACE_ALT` | `#162232` | Hero / elevated cards |
| `SURFACE_INSET` | `#1b2633` | Pill idle background |
| `SURFACE_WARM` | `#7d5a2a` | De-emphasis action button (e.g. Fade out) |
| `BORDER` | `#2a3a50` | Subtle dividers |
| `TEXT` | `#d8e2ef` | Primary text |
| `TEXT_MUTED` | `#a9bdd3` | Secondary / caption text |
| `PRIMARY` | `#2f5f8f` | CTA / import button (unchanged) |
| `PRIMARY_HOVER` | `#3a72a8` | Hover variant |
| `DANGER` | `#cc3333` | **Unified "danger red"** — was 3 different values |
| `DANGER_HOVER` | `#e04444` | Hover variant |
| `DANGER_DIM` | `#8f2f2f` | Dark pill bg for destructive-action buttons |
| `SUCCESS` | `#22cc66` | Ready / success label |
| `SUCCESS_DIM` | `#1f5a3a` | Success pill background |
| `WARNING` | `#cc8800` | Amber — alert / loading |
| `INFO` | `#1f3f6f` | Blue — generating / info state (pill bg) |
| `INFO_BRIGHT` | `#4488ff` | Bright blue — pipeline label text |
| `NEUTRAL` | `#555555` | Inactive buttons, tertiary controls |
| `NEUTRAL_DIM` | `#444444` | Subtle quiet background |

**Consolidation decisions:**
- `#cc3333`, `#8f2f2f`, `"darkred"` → `DANGER` (bright label) + `DANGER_DIM` (pill bg).
  Two tokens because they serve different widget roles: label text vs. pill background. A single
  `DANGER` value would make pill backgrounds unreadably dark or labels unreadably dim.
- `#22cc66` / `#44cc66` → `SUCCESS` (both were used as "ready" label; `#44cc66` is kept as
  a pipeline-display value in status_bar color dicts — those dicts will be migrated in a
  future panel pass).
- `#4ade80` / `#f87171` (Tailwind interlopers, avatar_panel) → fold into `SUCCESS` / `DANGER`
  on migration (Phase 2+).
- Five background tints → `BG` / `SURFACE` / `SURFACE_ALT` (three semantic levels).

#### Spacing scale

| Token | px | Maps from |
|-------|----|-----------|
| `SPACE_XS` | 2 | `pady=2`, minimal gaps |
| `SPACE_SM` | 4 | `padx=4`, `pady=4` |
| `SPACE_MD` | 8 | `padx=(0, 8)`, `pady=8` |
| `SPACE_LG` | 12 | `padx=12`, `pady=(12, 8)` |
| `SPACE_XL` | 16 | `padx=14/16` (rounded to 16) |
| `SPACE_2XL` | 24 | Hero sections, breathing room |

#### Typography scale

| Token | pt | Maps from |
|-------|-----|-----------|
| `CAPTION` | 10 | Labels at 9/10 |
| `BODY` | 12 | Default text at 11/12 |
| `LABEL` | 13 | Pill text, compact labels |
| `TITLE` | 14 | Sub-panel titles at 14/15 |
| `HEADING` | 16 | Section headings |
| `HERO` | 24 | Panel hero title (was 21/22/24 — settled at 24) |

`FONT_FAMILY = "Roboto"` (CTk falls back to system sans-serif if not installed).

#### Radii

| Token | px | Usage |
|-------|-----|-------|
| `RADIUS_SM` | 8 | Compact pills |
| `RADIUS_MD` | 12 | Standard pill / card |
| `RADIUS_LG` | 18 | Hero card / scroll-frame |

#### `font()` helper

```python
theme.font("BODY")              # → CTkFont(size=12, weight="normal")
theme.font("HEADING", weight="bold")  # → CTkFont(size=16, weight="bold")
```

Lazy import of `customtkinter` keeps the module importable in headless test environments.
Raises `KeyError` on unknown step names (fail-fast, not silent fallback).

### 2. Pilot migration: `opencohost/ui/music_panel.py`

`music_panel.py` was chosen as the pilot because:
- Self-contained (no UIState observer, no thread-safety concerns).
- 16 raw hex literals across 93 lines — a representative cross-section.
- No existing tests (clean slate for the contract test).
- Covers all four token categories: colors, spacing, typography, radii.

All 16 raw hex literals replaced with theme token references. Effective values are identical
(look-preserving). No layout logic changed.

### 3. Phased migration plan

| Phase | Scope | Notes |
|-------|-------|-------|
| **1 (this ADR)** | Token module + music_panel pilot | Foundation proof |
| **2** | status_bar, voice_control, model_panel | High-traffic panels |
| **3** | avatar_panel, cohost_agenda_panel, gear_popover | Complex panels |
| **4** | app_shell, advanced_panel, profile_panel, remaining panels | Shell wiring |
| **5 (separate)** | Visual refinement — palette tuning, spacing tightening | Owner review required |

**Visual refinement is explicitly deferred.** Phase 1–4 are mechanical substitutions
(same effective values via tokens). Phase 5 is the design pass where the owner reviews
and approves changes to the actual palette/sizes — it requires UX judgment, not just code.

---

## Consequences

### Positive
- One source of truth for all visual values.
- Future UI changes (palette tuning, dark/light mode) require editing one file.
- Non-vacuous contract tests catch any accidental hex re-introduction in migrated panels.
- Zero runtime risk: the token module has no dependencies and no side effects.

### Negative / Risks
- Panel-by-panel migration is incremental — the codebase will be in a mixed state (some
  panels migrated, others not) until Phase 4 completes.
- `font()` helper requires a Tk display at call time (headless test environments must mock
  `customtkinter`). The existing test pattern (fixture-level mock) handles this correctly.
- `SURFACE_WARM` (`#7d5a2a`) has no other semantic peer in the palette — it is the "Fade out"
  button color. If the palette is later unified, this token should be reviewed.

### Not in scope
- Changing the application theme mode (`ctk.set_appearance_mode("dark")` unchanged).
- Migrating status_bar's internal palette dicts (those are tightly coupled to existing tests
  asserting specific hex values — migrate in Phase 2 with coordinated test updates).
- Any visual change visible to the end user.

---

## Implementation Evidence

- `opencohost/ui/theme.py` — token module (new file)
- `opencohost/ui/music_panel.py` — pilot migration (16 raw hex → token references)
- `tests/test_theme_tokens.py` — 22 token taxonomy tests (all GREEN)
- `tests/test_music_panel_no_raw_hex.py` — 3 contract tests (all GREEN)
- Full suite: **2817 passed, 2 skipped, 0 failed**

TDD cycle: RED (22 errors + 2 failures before implementation) → GREEN (25 passed after).
Non-vacuity: failing `test_no_raw_hex_color_literals` printed 16 offending literals with line
numbers before the migration was applied.
