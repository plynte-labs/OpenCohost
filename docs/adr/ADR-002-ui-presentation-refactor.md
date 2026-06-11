# ADR-002: UI Presentation Refactor & Collapsible Card Pattern

**Date**: 2026-05-17  
**Status**: Accepted  
**Branch**: `feature/ui-roast-refactor`

## Context

The OpenCohost UI had accumulated visual debt: overloaded panels, invisible tab navigation, hardcoded colors without a system, and wide scroll-heavy layouts. The user identified 9 UX pain points during a roast session. The underlying architecture (UIState, CallbackDispatcher, modular panels) was solid — the issues were purely presentation-layer.

## Decision

### 1. Custom Product Tabs (replace `CTkTabview`)

`CTkTabview` with `unselected_color="#151d26"` on `#0f151c` background was imperceptible. Replaced with 5 full-width `CTkButton` tabs:

- Active: `fg_color="#2f5f8f"`, `text_color="#ffffff"`
- Inactive: `fg_color="#151d26"`, `text_color="#6b7b8d"`
- Equal column weights (`uniform="product_tab"`)
- Content frames toggled via `_switch_product_tab()` with `grid()`/`grid_remove()`

### 2. Collapsible Card Pattern (gold standard)

Established in Stream Acciones and propagated to all panels:

```python
# Toggle button
btn = CTkButton(card, text="▼ Section", anchor="w",
    fg_color="transparent", text_color="#d8e2ef", hover_color="#1d2a38",
    command=lambda: toggle(content, btn))

# Content frame
content = CTkFrame(card, fg_color="#101923", corner_radius=14)
content.grid_remove()  # collapsed by default
```

Applied to: Stream Acciones (3 sections), Ayuda (5 help cards), Co-host (3 sections), Avatar/OBS (main + sub-toggles).

### 3. Extracted Primary Button

`btn_primary_voice` ("Hablar") extracted from `VoiceControlPanel` to Kira panel level. `VoiceControlPanel.__init__` accepts optional `external_primary_button` parameter. When external button is provided, voice actions area is compact (no empty frame).

### 4. Security Hardening (post-audit)

- Thread safety: `threading.Lock` on `_chat_users`, `_seen_chat_ids`, `_chat_connected`, `_ptt_buffer`
- Observer leak: store `subscribe()` ID, `unsubscribe()` in `on_closing()`
- `_chat_users` capped at 1000 entries (insertion-order eviction)
- Speech truncated to 30 chars in logs (PII protection)
- Action log rotation: last 5000 lines

## Consequences

### Positive
- Product tabs are clearly visible and fill full width
- Collapsible pattern reduces visual noise — users expand only what they need
- Voice panel no longer has empty wasted space
- Thread-safety bugs fixed (real race conditions confirmed in code)
- All 6 modified files compile clean

### Negative
- `app_shell.py` grew from ~2470 to ~2650 lines (collapsible toggles + custom tabs)
- Two design-vs-task mismatches remain: response textbox at 130px (design said 200px), voice panel not in collapsible wrapper (design-only, not tasked)

### Neutral
- Collapsible pattern creates consistency across all panels but adds maintenance burden if pattern changes
- Custom tabs lose `CTkTabview` built-in keyboard navigation (not used in this app)

## Affected Files

| File | Lines changed | Summary |
|------|--------------|---------|
| `ui/app_shell.py` | +183/-30 | Custom tabs, Ayuda cards, Hablar button, observer leak fix |
| `ui/voice_control.py` | +27/-8 | External button support, PTT lock, speech truncation |
| `ui/avatar_panel.py` | +176/-13 | Mode spinner, collapsible main + sub-toggles |
| `ui/cohost_agenda_panel.py` | +178/-47 | 3 collapsible sections (full-width) |
| `ui/stream_admin_ui.py` | +208/-20 | Client ID toggle, OAuth/Metadata collapsible, Acciones collapsible, thread safety |
| `ui/advanced_panel.py` | +15/-2 | Action log rotation |

**Total**: ~+687 / −120 lines across 6 files

## Related
- ADR-001: Co-host state machine resilience
- SDD artifacts: `sdd/ui-roast-refactor/*` in Engram
- Audit report: Engram `audit/ui-security-perf-2026-05-17`
