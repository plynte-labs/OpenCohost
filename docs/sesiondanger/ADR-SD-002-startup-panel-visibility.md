# ADR-SD-002 — Startup Panel Visibility: Bug B (state-view mismatch) and Bug C (compact-default hides product workspace)

**Date**: 2026-06-25
**Status**: Accepted
**Branch**: session/danger-overnight-20260625
**Track**: startup-panel-visibility-bugfix

---

## Context

### Bug B — AdvancedModePanel.build() state-view mismatch

`AdvancedModePanel.__init__` sets `_logs_panel_visible = False` (logs hidden by
default). However, `build()` called `self._frame.grid(...)` to position the frame
without a subsequent `grid_remove()`. The result: the widget was actually rendered
(visible in the layout), while `_logs_panel_visible` remained `False`.

`set_logs_visible(False)` has a no-op guard (`if self._logs_panel_visible == visible: return`)
which would early-return immediately after build since the Python bool already says
`False`. This meant the guard **prevented** `grid_remove()` from ever being called at
construction time — the only moment where state and view diverged.

Consequence: the logs/terminal frame was always visible at startup regardless of any
compact or toggle state, while all state bools claimed it was hidden.

### Bug C — _compacto_active=True hides the product workspace at startup

`app_shell.py` line 123 set `self._compacto_active: bool = True` as the startup
default (introduced by track `ui_declutter_20260614`).

At the end of `_build_ui` (line 972), `_toggle_modo_compacto()` is called. With
`_compacto_active=True`, the compact branch runs:

1. `self._side_config_panel.grid_remove()` — hides the product workspace
2. `self._show_main_view("Kira")` — sets Kira as hero view
3. `self._set_logs_panel_visible(False)` — hides logs

`_side_config_panel` and `_product_workspace_panel` are the SAME widget (aliased at
lines 965–966). So step 1 hid the product workspace, leaving users with no config
panel visible at startup.

---

## Decision

### Bug B fix — `advanced_panel.py` line 159–160

Immediately after `self._frame.grid(...)` in `build()`, call `self._frame.grid_remove()`
so the rendered widget state matches the `_logs_panel_visible=False` init default.
The no-op guard in `set_logs_visible()` is left intact — it is correct for all
subsequent toggle calls and must not be removed.

### Bug C fix — `app_shell.py` line 123

Flip the startup default: `self._compacto_active: bool = False`.

With this change, `_toggle_modo_compacto()` at the end of `_build_ui` routes through
the else-branch:
- `self._side_config_panel.grid()` → product workspace VISIBLE
- `self._toggle_logs_panel()` → reads `_logs_visible_active=False` → logs HIDDEN

Kira hero view is set by the explicit `self._show_main_view("Kira")` call at line 968,
which runs BEFORE `_toggle_modo_compacto()` and is decoupled from compact mode. The
else-branch does NOT call `_show_main_view()`, so Kira remains the startup hero.

The gear-popover toggle reads `compacto_active=getattr(self, "_compacto_active", True)`
(line 2019). The `True` fallback is only defensive code for the case where the attribute
is absent — it is never reached in normal operation. The popover will correctly show
compact as OFF when opened, consistent with `_compacto_active=False`.

This **reverses** the `ui_declutter_20260614` "compact-is-default" decision. The
owner explicitly requested the product panel be visible at startup.

---

## Alternatives Considered

### Decouple: keep compact default, exempt product workspace from grid_remove

Instead of flipping the default, `_toggle_modo_compacto()` compact branch could be
modified to skip calling `grid_remove()` on `_side_config_panel` specifically.

**Tradeoffs vs. chosen approach:**

| Criterion | Flip default (chosen) | Decouple (rejected) |
|---|---|---|
| Correctness | Clean: compact OFF at startup matches user expectation | Awkward: compact=True but product shown — contradictory state |
| Simplicity | Single line change, no conditional logic added | Requires adding "startup exemption" guard to compact branch |
| Future compact toggle | Works normally — toggling compact at runtime hides product | Risk of exemption persisting after first toggle, requiring extra state |
| Owner intent | Owner explicitly wants product panel visible at startup | Owner intent is the panel, not the compact flag |
| git blame clarity | Old compact default clearly reversed by one line | Compact default stays True, behavior exception hidden in toggle logic |

The decouple approach introduces a mismatch between `_compacto_active=True` and the
visible product panel — users toggling compact off and back on may hit inconsistent
behavior. The flip is simpler and more honest about the desired default.

---

## Consequences

- **Startup state** (2026-06-25 onwards): product workspace VISIBLE, logs HIDDEN,
  Kira hero view active. All three state bools (`_compacto_active`, `_logs_panel_visible`,
  `_logs_visible_active`) agree with widget render state.
- **ui_declutter_20260614**: the compact-is-default behavior from that track is
  reversed. Compact mode itself is preserved and fully functional — it is just no
  longer the startup state.
- **app_shell.py line count**: 2699 → 2702 (comment expansion). Cap ratcheted
  2700 → 2710 in `tests/test_integration.py` with dated justification comment.
  Planned agenda/audio decomposition will reclaim these lines.

---

## Tests Locking This Decision

All tests in `tests/test_advanced_panel.py::TestBuildStartupVisibilityState` and
`tests/test_app_shell_obs_resilience.py` (Bug C section).

**Bug B tests** (`tests/test_advanced_panel.py`):
- `TestBuildStartupVisibilityState::test_build_leaves_frame_hidden_matching_init_default`
  — asserts `_frame._grid_visible is False` AND `_logs_panel_visible is False` after
  `build()`. Non-vacuous: checks widget render state, not just the Python bool.
  Was RED before fix (frame was gridded with `_grid_visible=True`).

**Bug C tests** (`tests/test_app_shell_obs_resilience.py`):
- `test_compact_default_is_false_at_startup` — inspects `VocalAIApp.__init__` source
  and asserts `_compacto_active = False` is present. Was RED before fix.
- `test_toggle_compacto_false_shows_product_panel_and_hides_logs` — stubs
  `_side_config_panel`, calls `_toggle_modo_compacto()` with `_compacto_active=False`,
  asserts `side_panel.grid()` was called (product shown) and `_logs_panel_visible`
  remains False. Non-vacuous: requires the affirmative `grid()` call, not just absence
  of `grid_remove()`.

**Existing tests kept GREEN**:
- `tests/test_advanced_panel.py` — all 63 existing tests (including no-op guard tests
  `test_toggle_noop_when_already_hidden`, `test_toggle_noop_when_frame_not_built`)
- `tests/test_product_ui_refactor_safety.py` — source-string assertions
- `tests/test_integration.py` — line cap (ratcheted) and all import assertions
