# ADR-021: UI Design System Architecture (3-Layer Atomic Design)

- Status: Accepted
- Date: 2026-06-25
- Track: `ui-design-system-20260625` (Phase 2)
- Supersedes/extends: [ADR-020 — UI Design Token System](./ADR-020-ui-design-token-system.md)

## Context

ADR-020 established **Layer 1** (tokens) in `opencohost/ui/theme.py` and migrated
`music_panel.py` as the pilot. That removed raw hex from one panel, but panels
still construct widgets by hand, repeating the same
`fg_color=… corner_radius=… font=…` recipes everywhere. A neutral button
(`#555555` / `#666666`) appears verbatim in `model_panel.py` AND
`profile_panel.py`; section headers repeat `CTkFont(size=13, weight="bold")`
across nearly every panel. Tokens alone do not stop that duplication — they only
name the values.

We need the middle layer of an atomic-design system: reusable, named component
factories that compose tokens into consistent widgets, analogous to CSS
component classes / Tailwind component layers.

## Decision

Adopt a **three-layer** UI design system:

```
Layer 1  theme.py    Tokens         colors / spacing / typography / radii
Layer 2  styles.py   Components     factory fns: card, *_button, *_label  (NEW)
Layer 3  panels      Consumers      use styles + theme; zero raw hex / sizes
```

### File structure

| File | Layer | Responsibility |
|------|-------|----------------|
| `opencohost/ui/theme.py` | 1 | Single source of truth for raw values + `font()` |
| `opencohost/ui/styles.py` | 2 | Pre-styled widget factories composing tokens |
| `opencohost/ui/<panel>.py` | 3 | Consume `styles.*` and `theme.*` only |

### `styles.py` API

Every factory takes the **`ctk` module as its first argument**, supplied by the
calling panel. Each factory sets DEFAULTS from tokens; caller `**kw` always wins
(defaults merged first, caller kwargs merged last). Factories contain **no
business logic** — pure presentation.

| Factory | Returns | Token defaults |
|---------|---------|----------------|
| `card(ctk, parent, **kw)` | `CTkFrame` | `fg_color=SURFACE`, `corner_radius=RADIUS_MD` |
| `primary_button(ctk, parent, text, command, **kw)` | `CTkButton` | `fg_color=PRIMARY`, `hover_color=PRIMARY_HOVER` |
| `danger_button(ctk, parent, text, command, **kw)` | `CTkButton` | `fg_color=DANGER_DIM`, `hover_color=DANGER` (destructive pill) |
| `neutral_button(ctk, parent, text, command, **kw)` | `CTkButton` | `fg_color=NEUTRAL`, `hover_color=NEUTRAL_HOVER` |
| `select_button(ctk, parent, text, command, *, active=False, **kw)` | `CTkButton` | `fg_color=SELECT_ACTIVE`/`SELECT_IDLE`, `hover_color=SELECT_HOVER` (segmented control) |
| `heading(ctk, parent, text, *, step="LABEL", **kw)` | `CTkLabel` | bold font at `step`, `anchor="w"` |
| `body_label(ctk, parent, text, *, step="BODY", **kw)` | `CTkLabel` | `text_color=TEXT`, font at `step`, `anchor="w"` |
| `muted_label(ctk, parent, text, *, step="BODY", **kw)` | `CTkLabel` | `text_color=TEXT_MUTED`, font at `step`, `anchor="w"` |

The factory set was derived from the **real** repeated patterns in the migrated
panels (`music_panel.py`, `model_panel.py`) — no speculative factories were
added. `select_button` exists specifically for the manual LLM-tier segmented
control in `model_panel.py`.

### Why `ctk` is injected (not imported in `styles.py`)

`theme.font()` lazily imports customtkinter for headless safety. For Layer 2 we
go one step further: the panel passes its own `ctk` binding. Two reasons:

1. **Headless-safe** — no module-level Tk import in `styles.py`.
2. **Test-transparent** — panel tests already patch their own module's `ctk`
   (e.g. `patch("opencohost.ui.model_panel.ctk", mock_ctk)`). Because the panel
   forwards that same object into `styles.*`, the factories build the mock
   widget with **no new patch target** and no real-Tk construction under test.
   All 83 pre-existing `model_panel` behavior tests stayed green unmodified.

### New tokens added (Layer 1)

To migrate `model_panel.py` without raw hex, `theme.py` gained:

- `NEUTRAL_HOVER = "#666666"` — hover for `NEUTRAL` buttons.
- `SELECT_ACTIVE = "#1f4f7a"`, `SELECT_IDLE = "#2b3440"`, `SELECT_HOVER = "#286391"`
  — the segmented "tab group" palette for the manual tier selector.

These are exact 1:1 captures of the previous literals (no visual change).

## Migration plan (panel-by-panel, look-preserving)

Migrate one panel per phase. Each migration is **behavior- and look-preserving**:
every effective value (color, size, padding) must map to a token of the SAME
value. A source-level no-raw-hex contract test guards each migrated panel.

| Phase | Panel(s) | Status |
|-------|----------|--------|
| 1 | `music_panel.py` (pilot) | Done (ADR-020) |
| 2 | `model_panel.py` | **This ADR** |
| 3+ | `profile_panel.py`, `voice_control.py`, `avatar_panel.py`, … | Pending |

`status_bar.py` is explicitly **deferred**: its color dicts are asserted by
literal value in `test_status_bar.py`, so migration there needs coordinated test
updates and is out of scope for look-preserving phases.

### Consolidation caveat (carried from ADR-020)

When a panel uses a tint that a token consolidated to a **non-equal** value, that
is a real (small) visual shift and MUST be flagged, not silently applied. In
Phase 2, `model_panel.py` had two muted-text greys that are not equal to the
consolidated `TEXT_MUTED` (`#a9bdd3`):

- `#aaaaaa` (neutral grey, model-info label) → `TEXT_MUTED`
- `#8fa3b8` (cool blue-grey, tier-info label) → `TEXT_MUTED`

Both now render as `#a9bdd3` (slightly lighter / cooler). This is an intentional
consolidation consistent with Phase-1's "unify muted secondary text" decision.
Additionally, the model-info label font moved `11pt → BODY (12pt)`, matching the
ADR-020 typography map (`11/12 → BODY`). These are the only deviations from
pixel-identical; everything else is exact.

## Phase 3 is a separate, owner-reviewed phase

This ADR covers **architecture and look-preserving migration only**. Introducing
a new professional palette (the "menos es más" visual refinement) is a SEPARATE
phase that changes token VALUES and therefore changes appearance. It must be
reviewed and approved by the owner before any values move. Layers 1–2 are
designed so that refinement becomes a token-value edit, not a panel rewrite.

## Consequences

- Panels shrink and read declaratively; widget recipes live in one place.
- A future palette change is a `theme.py` edit; `styles.py` and panels follow
  automatically.
- Slight ergonomic cost: factories take `ctk` as the first arg. Justified by
  headless safety and zero-churn testability.
- Each migrated panel carries a source-level no-raw-hex contract test, so a
  regression (someone hard-coding a hex) fails CI even if the UI looks right.

## References

- ADR-020 — UI Design Token System (Layer 1, pilot)
- `opencohost/ui/theme.py`, `opencohost/ui/styles.py`, `opencohost/ui/model_panel.py`
- `tests/test_styles.py`, `tests/test_model_panel_no_raw_hex.py`
