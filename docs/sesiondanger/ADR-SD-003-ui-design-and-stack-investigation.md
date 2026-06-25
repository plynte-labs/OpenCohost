# ADR-SD-003: UI Design Audit ("menos es más") and Stack Assessment — CustomTkinter vs Migration

**Date**: 2026-06-25
**Status**: Proposed — owner decision
**Branch**: `session/danger-overnight-20260625` (Investigation D — investigation only, changes no code)
**Author**: Claude Code (autonomous overnight batch)
**Scope**: Read-only audit of `opencohost/ui/*` + entry-point theme. Proposes; migrates nothing.

---

## Context

OpenCohost is a local-first AI streaming co-host ("Kira") for streamers. The desktop UI is
Python + CustomTkinter (`opencohost/ui/`), and it is **thread-safety-critical**: a
`_safe_after` / `_process_ui_tasks` / `_on_motor_event` triad (`app_shell.py:2071-2120`)
marshals every Tk mutation back to the main thread, because the LLM/TTS engine runs on a
separate daemon thread and CustomTkinter (Tcl/Tk) is not thread-safe.

The owner's design philosophy is **"menos es más"** (less is more): the product must look
**modern and professional**, and the owner is questioning whether CustomTkinter is enough.
`conductor/product.md` already encodes this — "UI centrada en Kira", "Config secundaria",
"Modo avanzado opcional: logs y debug ocultos por defecto". `conductor/tech-stack.md` already
lists, as a stated intent, **"Tauri + React + Tailwind — migración futura planificada"** — so
the stack question is not new; this ADR is the place to actually weigh it.

This investigation has two parts, both grounded in the real files.

---

## Part 1 — UI Design Audit (within CustomTkinter, low-risk)

### What exists today (the good)

The UI is **not** a beginner mess. It already shows real design maturity:

- A clean composition shell: `app_shell.py` is a thin wiring layer that delegates to panel
  modules (`VoiceControlPanel`, `ModelPanel`, `MusicPanel`, `AvatarPanel`, `CoHostAgendaPanel`,
  `StreamAdminUI`, `SmartAggregatorUI`, `AdvancedModePanel`, `StatusBar`).
- A **product workspace** layout: Kira stays persistent on the left; configuration and stream
  ops live in a right-side tabbed workspace (`app_shell.py:441-583`) with five product tabs
  (Configuración / Stream / Co-host / Música / Avatar-OBS) and nested sub-tabs.
- **Compact mode is the startup default** (`_compacto_active = True`, `app_shell.py:123`,
  applied at `:968-972`), hiding the side config panel — a real "menos es más" move already shipped.
- A prior declutter pass (ADR-009) cut the status bar from **13 always-visible elements to ~4**
  via a single `Sistema` rollup pill and moved switches/brand into a gear (⚙) popover
  (`gear_popover.py`). Logs are hidden by default.
- A documented collapsible-card pattern (ADR-002) and a custom full-width tab pattern that
  replaced the imperceptible `CTkTabview`.

So the problem is **not** "the UI is ugly". The problem is **inconsistency and density debt**:
the polish was applied panel-by-panel, reactively (ADR-002 was literally a "roast session"
follow-up), without a shared design system. That is exactly what reads as "almost
professional but not quite".

### Concrete "less-is-more" gaps (grounded)

**G1 — No design system; the palette is hand-mixed per widget.**
There is **no central theme/colors/spacing module** in `opencohost/ui/`. The only global theme
config is two lines at the entry point: `ctk.set_appearance_mode("dark")` +
`ctk.set_default_color_theme("blue")` (`opencohost/__main__.py:19-20`). Everything else is
**hardcoded hex literals inline** — a grep finds **382 `#rrggbb` literals across 16 UI files**.
The same semantic color is spelled differently in different places: "success green" appears as
`#22cc66` (`status_bar.py:23`), `#1f5a3a` (`status_bar.py:68`, `model_panel`), and `#44cc66`
(`status_bar.py:91`); the panel-background tint is `#0f151c` / `#10161d` / `#101923` / `#111820`
/ `#151d26` / `#162232` depending on which file you're in (e.g. `music_panel.py:35,39,52,75`).
`StatusBar` does the *right* thing locally (named palette dicts at `status_bar.py:22-84`), which
proves the team knows the pattern — it just isn't shared. There are even **three different
"danger" reds** (`#cc3333`, `#8f2f2f`, and the Tk named color `"darkred"` used in
`voice_control.py` and `smart_aggregator_ui.py`) and a **Tailwind-palette interloper**
(`#4ade80` / `#f87171` in `avatar_panel.py:474-475`) that matches nothing else. **This is the
single biggest "professional polish" lever and the lowest-risk one.**

**G2 — No typography scale.**
Font sizes are chosen ad-hoc per label. Across the UI there are **11 distinct sizes**
(9, 10, 11, 12, 13, 14, 15, 16, 21, 22, 24). Two incompatible systems coexist: product panels
(`music_panel`, `cohost_agenda_panel`) use 24/16/14 for internal hierarchy, while the config
sidebar (`model_panel`, `profile_panel`) uses 13/12/10, and the shell uses 22/21/16/14/13/12/11/10.
`avatar_panel.py` alone uses **nine distinct size/weight combinations** (9–14) — the worst single
panel. There is no `ctk.CTkFont` factory or size token set, so panels that should look like
siblings don't.

**G3 — Spacing is ad-hoc.**
`padx`/`pady` values are literals scattered everywhere (`(12, 8)`, `(0, 8)`, `(14, 2)`,
`(4, 12)`, `(8, 0)`, `10`, `4`, `2`…). There is no 4/8/12/16 spacing scale, so vertical rhythm
drifts between panels. This is why the app can feel "busy" even when each individual panel is fine.

**G4 — Densest panels still over-expose controls.**
Even after the declutter, several surfaces remain heavy:
- The **status bar** still renders **8 always-visible elements** (1 label + 7 pills:
  Sistema/Modelo/Mic/TTS/Chat/Health/Voz, `status_bar.py:154-215`). The `Sistema` rollup (ADR-009)
  was added *to replace* the detailed pills, but the 6 detailed pills were kept, so the rollup is
  now redundant with what it summarizes. Long pill text ("Voz: clonación no configurada") risks
  truncation on narrow windows.
- **Redundant pipeline state in two places**: the `voice_control.py:266-293` 4-pill strip
  (Voz/TTS/Memoria/Chat) duplicates 4 of the status-bar pills via a *separate* update path
  (`update_tts_label` vs `update_tts_status`) — wasted vertical space and a divergence risk.
- The **Audio/TTS config sub-tab** (`app_shell.py:708-786`) stacks, in one scroll column: device
  combo, Grabar, Cargar WAV, LiveAudio connect, two TTS switches (Ligero/Pesado + Solo-local),
  a speed selector, Limpiar Memoria, the PTT block (switch + hotkey map + status) — ~10 distinct
  controls, several with multi-line Spanish helper paragraphs inline.
- The **Co-host agenda panel**: when the topic form is expanded, ~15 interactive elements are
  visible at once (7 form fields + 4 session dropdowns + 4 action buttons,
  `cohost_agenda_panel.py:86-349`), plus a ~250-char guardrails paragraph (`:340`) inlined as UI
  copy. The richest control set in the app — collapsible, but a wall on first expand.
- **`music_panel.py:65-73`**: up to 8 mood buttons, all identical `#555555`, plus a destructive
  "Limpiar faltantes" sharing a row with operational "Fade out", distinguished only by color.
- **`avatar_panel.py:270-325`**: 8 identical-looking buttons (4× "Elegir imagen" + 4× "Probar"),
  none conditionally disabled, in a vertical list.

**G5 — Weak first-run hierarchy / phantom controls.**
There are real "phantom"/stub controls preserved for backward-compat (`_EntryStub` /
`_ButtonStub`, `app_shell.py:84-101`, and `entry_youtube_*` stubs at `:790-795`). They are
invisible no-ops, so not user-facing clutter — but they signal that the YouTube/Stream-Admin
surface (RF4) is half-retired (hidden behind `STREAM_ADMIN_ENABLED=False`, ADR-009). A
first-time streamer opening the "Stream" tab still sees an Emisión/Acciones structure whose
moderation half is intentionally gone. The information scent there is misleading.

**G6 — Mixed-language UI and emoji as iconography.**
All UI copy is Spanish (consistent with the owner), but buttons mix emoji-as-icon
(`🎤 Grabar`, `📂 Cargar WAV`, `🗑️ Limpiar Memoria`, `🎛️ TTS`) with plain-text buttons
(`Hablar`, `Enviar a IA`). Emoji glyphs render inconsistently across Windows font fallbacks and
read as informal — at odds with "professional". Pick one icon strategy.

**G7 — No real iconography, no empty/loading states beyond text.**
Feedback is text-and-color-pill based. There is no icon set, no skeleton/loading affordance
beyond a progress bar and color changes. This is a ceiling of the toolkit as much as a design
gap (see Part 2).

### Prioritized, low-risk design improvements (all within CustomTkinter)

Ordered by value/effort. None requires a rewrite; all are reversible.

| # | Improvement | Why it moves "professional/menos-es-más" | Effort |
|---|---|---|---|
| **D1** | **Extract a `ui/theme.py`**: named color tokens (bg.base, bg.raised, accent, success, warn, crit, text.primary/secondary), a typography scale (`heading_lg/md/sm`, `body`, `caption` via `CTkFont` factories), and a 4/8/12/16 spacing scale. Migrate panels to it incrementally. | Kills G1/G2/G3 at the root. One source of truth makes every future screen automatically consistent — the difference between "templated default" and "designed". | **M** (token module is S; full migration is the long tail — do it panel-by-panel) |
| **D2** | **Unify the accent + neutral palette** to one blue and one set of grays (the `set_default_color_theme("blue")` accent should match the inline `#2f5f8f`). Replace the 6 background tints with 2–3 tokens (base / raised / inset). | Removes the subtle "every panel is a slightly different shade" effect that reads as unpolished. | **S–M** (mechanical once D1 exists) |
| **D3** | **Tighten the Audio/TTS sub-tab (G4)**: collapse the helper paragraphs into one info affordance per group, group the two TTS switches into a single "Voz" card, demote Limpiar-Memoria to a less prominent slot. | The most control-dense everyday surface becomes scannable. | **S** |
| **D3b** | **De-duplicate pipeline status (G4)**: keep the `Sistema` rollup as the single top-of-window indicator and demote/remove the 6 detailed status-bar pills + the redundant `voice_control` 4-pill strip (or make one the source of truth). | Pure "menos es más" — removes the busiest, most redundant region; kills the two-update-path divergence risk. | **S–M** |
| **D4** | **One icon strategy (G6)**: either drop emoji from buttons for clean text+accent, or adopt a single consistent glyph font. Standardize on text-first for the launch. | Consistency = professional; removes Windows emoji-fallback rendering risk. | **S** |
| **D5** | **First-run hierarchy pass (G5)**: when `STREAM_ADMIN_ENABLED=False`, label/empty-state the Stream tab honestly ("Moderación vía Nightbot") instead of showing a half-empty Acciones structure. Add a one-line "what is this panel" header to each product tab (the Ayuda copy already exists at `app_shell.py:811-817` — surface it contextually). | Fixes streamer first-run comprehension — the screens that hurt onboarding today. | **S–M** |
| **D6** | **Typography pass on the Kira hero** — settle one heading size for panel titles (currently 22/24/16 compete). Make "Hablar" the unambiguous single primary action (it already is at `:487-497` — keep it the only `#1f7a5a` green button app-wide). | Strong, single visual hierarchy = the core of "menos es más". | **S** |
| **D7** | **HiDPI sanity check**: set explicit `ctk.set_widget_scaling()` / honor OS DPI, and verify on a 4K stream-PC. Today scaling is left fully default (no scaling calls anywhere). | Streamers run high-DPI/large monitors; blurry or mis-scaled UI reads as amateur. | **S** to test; **M** if fixes needed |

**Total for a credible "launch-polish" pass: D1–D6 ≈ M (a few focused days), low risk** — it is
additive (a token module) plus mechanical substitution, fully inside the toolkit, behind the
existing test suite. It does **not** touch the thread-safety triad or any core logic.

---

## Part 2 — Stack Assessment: is CustomTkinter enough, or migrate?

### CustomTkinter's real ceiling (honest)

What it does well here: it is pure-Python, in-process, trivially shares objects with the core
(the `UIState` observer, `app_shell.py:142-146`, is a clean framework-agnostic seam), packages
into the existing PyInstaller pipeline, and the team is already productive in it (9+ UI ADRs,
working panels). For a single-window operator tool, that is a lot of value.

Where it hits a ceiling for a *professional streaming product*:

1. **Single-threaded Tk event loop is the architectural tax.** ADR-008 documents it bluntly:
   "CustomTkinter runs one event loop; any synchronous network/disk call on it freezes every
   panel." The team has spent **ADR-002, ADR-007, ADR-008, ADR-009 and an entire
   `ui_rendering_optimization_20260609` track** fighting freezes, off-thread marshaling, and
   StatusBar thread-safety. This is hard-won and *mostly solved* — but it is a permanent tax the
   toolkit imposes, not a one-time bug.
2. **Theming/skinning depth is shallow.** CustomTkinter theming is a JSON color file + per-widget
   args. No CSS-class cascade, no design-token system, no component variants. Achieving a truly
   bespoke look means hand-tuning hundreds of widget args (exactly the G1 problem).
3. **Animation/transitions are minimal.** No real transition/easing primitives; "animation" is
   manual `after()` loops (the RMS bar in `voice_control.py`). Smooth micro-interactions that
   read as "modern" are painful.
4. **Custom widgets are limited.** Anything beyond the CTk widget set means raw Tk `Canvas`
   drawing. Rich avatar compositing, video preview, animated VU meters, or a polished agenda
   timeline would be a slog.
5. **Rendering performance** is adequate for this UI but Tk is not GPU-accelerated; heavy
   redraw/scroll on large monitors can stutter (part of why the rendering track exists).
6. **Accessibility** is effectively nil (no semantic roles, screen-reader story is weak).
7. **Packaging size** is already fine (PyInstaller, established).
8. **Dev velocity** is *good today* precisely because the team knows it and the seam is clean.

Net: CustomTkinter is **enough to launch** and is **not** the thing blocking "professional" —
the missing design system (Part 1) is. But its ceiling is real if the product later wants
bespoke skinning, animation, and rich custom widgets.

### Decision options

#### Option A — Stay on CustomTkinter + invest in a design system (Part 1, D1–D7)

- **Buys**: 80% of the perceived "professional" jump (consistent palette/typography/spacing,
  cleaner hierarchy) for a fraction of the cost. Zero risk to the core.
- **Cost**: **S–M** (the D1–D6 pass).
- **Risk**: **Very low.** Additive, reversible, behind existing tests, no process boundary.
- **Loses**: nothing.
- **Verdict**: the obvious **now** move regardless of any later migration.

#### Option B — Rust TUI (terminal UI)

- **Buys**: fast, low-resource, "hacker-cool".
- **Reality check (be skeptical)**: a **terminal UI cannot host an avatar image, OBS preview, a
  music/mood board, or a streamer-facing visual product**. The whole point of OpenCohost is a
  *visual* co-host with an avatar (`AvatarPanel`, the left-panel avatar preview at
  `app_shell.py:469-485`) and OBS integration. A TUI throws away the product's face.
- **Cost / Risk**: **XL / very high** — full rewrite *and* a category mismatch.
- **Verdict**: **No.** Wrong tool for an avatar/video streaming product. Reject outright.

#### Option C — TypeScript + Tauri for the UI layer only; keep the Python core as a backend

This is the option `tech-stack.md` already names as "migración futura planificada".

- **The seam**: today UI↔core communicate **in-process** via `UIState` (thread-safe observer),
  `CallbackDispatcher`, command queues (`motor_ia.command_queue.put(...)`), and the
  `_on_motor_event(status)` callback. Under Tauri, the Python core becomes a local
  sidecar/server (websocket or local HTTP + a push channel) and the React UI is the client.
  `UIState` becomes the serialized state pushed over the socket; `command_queue.put` becomes a
  request message; motor events become server-pushed events.
- **What breaks (the hard part)**: the entire `_safe_after`/`_process_ui_tasks` **Tk-thread
  marshaling becomes a process/serialization boundary**. The thing the team spent four ADRs
  hardening is *replaced*, not reused — you trade "marshal to Tk main thread" for "serialize,
  send over IPC, deserialize, render in JS". That removes the freeze class (the JS UI can't be
  blocked by Python disk I/O) **but** introduces a new class: IPC liveness, reconnection,
  backpressure, partial-state sync, and startup ordering between two processes.
- **Buys**: a genuinely modern, fully skinnable UI (Tailwind/CSS, real components, animation,
  web-grade accessibility), and arguably faster UI dev velocity *for people who know web*.
- **Cost**: **XL.** Every panel (~10 panels, ~12.7k lines of UI) reimplemented in React + a new
  IPC protocol + a new packaging story (Tauri bundles a Rust shell + Node toolchain alongside
  the Python sidecar — heavier, more moving parts than today's single PyInstaller exe).
- **Risk to the working core**: **Medium** *if* the seam is respected. The LLM engine, TTS
  pipeline, SmartAggregator, health monitor, agenda — none of that needs to change; only the
  *presentation* and the *transport*. But the transport rewrite is exactly where subtle
  regressions (latency, dropped events, double-fire) would hide, and the current test suite
  asserts against the in-process seam, so a large swath of UI tests would need rewriting.
- **Verdict**: **Real and coherent, but not now.** Justified only *after* launch, *if* the
  product roadmap demands bespoke skinning/animation that A cannot deliver. Doing it pre-launch
  would pause the release to re-solve already-solved problems.

#### Option D — PySide/PyQt (Qt) for richer native widgets, staying in Python

- **Buys**: a far richer, genuinely native-feeling widget set, real styling (QSS ≈ CSS),
  animation framework, better HiDPI, accessibility — **without** leaving Python or introducing a
  process boundary. Qt has a proper main-thread/signal-slot model (`QThread`, signals) that maps
  cleanly onto the existing thread architecture, so the marshaling concept *transfers* rather
  than being discarded.
- **Cost**: **L** (smaller than C — same language, same in-process model, but every widget
  reimplemented and the `after()`-based marshaling rewritten to signals/slots).
- **Risk**: **Medium**, but lower than C: no IPC, core untouched, Python-native, the team's
  threading mental model survives. Licensing: PySide6 (LGPL) is fine for an MIT product.
- **Verdict**: **The most underrated option.** If CustomTkinter's *widget/animation* ceiling
  (not its theming) becomes the real blocker post-launch, Qt is a smaller, safer jump than a
  web rewrite and keeps the entire Python asset in one process. Worth naming as the leading
  "if we must migrate" path, ahead of Tauri, unless web-grade theming specifically is the goal.

### What we'd lose in any rewrite (the asset to protect)

The hard-won, battle-tested part of OpenCohost is **not** the UI — it's the Python core: the LLM
engine with tier-switching and the heavy-model inference watchdog/rollback (runtime-validated,
Gate 1 PASS), the chunked TTS pipeline with Edge/Piper/Qwen fallback gates, the SmartAggregator,
the health monitor, and the **thread-safety architecture** (`UIState`, `_safe_after`, the motor
event seam). Options C and B put the *transport* of that architecture at risk; Option D and A
leave the core entirely untouched. Any migration must treat "do not regress the core or its
runtime-validated recovery behavior" as the top constraint.

---

## Recommendation

**Do Option A now; defer C/D as an explicit post-launch decision; reject B.** The owner's
"menos es más" and "professional" goals are blocked far more by the **absence of a design
system** (one palette, one type scale, one spacing scale, one icon strategy — Part 1, D1–D6)
than by CustomTkinter's framework ceiling. That pass is **S–M effort, very low risk, reversible,
and touches no core logic** — it will deliver most of the perceived professionalism jump before
launch. CustomTkinter is *enough to launch*. A UI-layer migration (Tauri, or the underrated Qt
path) is a real future option the project already anticipated, but it is **XL/L effort, pre-launch
risk to a runtime-validated core, and re-solves the already-solved thread-safety problem on a new
boundary** — it is not justified now.

### Suggested sequencing

1. **Pre-launch (now)**: A → ship `ui/theme.py` (tokens + type + spacing), unify palette,
   tighten the Audio/TTS + Co-host density, settle one icon strategy, one heading scale, and a
   HiDPI check. Migrate panels to the tokens incrementally behind the existing tests.
2. **Launch** on CustomTkinter with the polished design system.
3. **Post-launch, gated on real user signal**: if and only if bespoke skinning/animation/custom
   widgets become a roadmap requirement that A demonstrably cannot satisfy, run a focused
   spike comparing **Option D (Qt, in-process, Python-native — smaller jump)** vs **Option C
   (Tauri, web UI over an IPC seam — richest theming, heaviest cost)**. Decide then, with the
   core frozen behind a stable contract (`UIState` is already most of that contract).
4. **Never** Option B.
