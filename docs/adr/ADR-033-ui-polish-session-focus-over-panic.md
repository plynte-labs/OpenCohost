# ADR-033 — UI Polish Session: "Focus Over Panic" (2026-07-15)

**Date:** 2026-07-15
**Status:** Accepted (implemented, uncommitted → committed this session; runtime validation pending, see debts)
**Driver:** Owner-delegated autonomous UI refresh of OpenCohost_UI (Tauri/React), followed by three owner-feedback adjustment rounds in the same day.

## Context

The Tauri cockpit accumulated UI debt: a status bar that read as a wall of
errors ("Health error", "voz en silencio"), a chat that looked mocked (canned
first message, repeated 🎫 muted-notice lines, pseudo-icon avatar), raw
un-styled scrollbars, heterogeneous animation timings, a buried save button in
the profile form, and no way to see LLM markdown, edit profiles from the list,
or drive agenda/music/session actions without leaving the chat. Design spec:
`OpenCohost_UI/docs/design/ui-refresh-20260715.md`; per-phase evidence log:
`OpenCohost_UI/docs/design/ui-refresh-20260715-progress.md`.

## Decisions

1. **State taxonomy — "color is a verb, not a mood."** Five states
   (ok/info/attention/action/neutral); only `action` may fill a chip surface.
   The status rail collapsed 6 chips into 4 instruments (Motor rollup absorbs
   Health; Kira merges Voz+Inactivo) with why-popovers explaining every state
   in plain language. Decorative tone elements with no semantics (leading ring,
   account avatar with no accounts) were deleted.
2. **Motion tokens as the single timing vocabulary.** `--dur-fast/base/slow` +
   `--ease-out/--ease-io` in `tokens.css`, mapped to Tailwind utilities; a
   project-wide sweep removed every raw `duration-N`/`ease-in-out`; one global
   `prefers-reduced-motion` kill switch neutralizes all motion.
3. **Alerts stay colorless by default, style is operator-switchable.** One DOM
   shape (`ui/Alert.tsx`), three CSS-only variants (sereno/marcado/contorno)
   keyed off `data-alert-style` on `<html>` (zustand + localStorage, same
   pattern as theme/density). `marcado` uses a left tone bar plus a fainter
   bottom line (35% mix) so it does not read as a button.
4. **Chat modernization without theme betrayal.** Empty-state invitation
   instead of a mocked first turn; muted-viewers notice anchored ONCE above the
   composer; real `KiraFace` avatar; full-width turns; system events as
   full-width `role="status"` alerts following the chosen alert style;
   composer mic with hold-fill feedback (8s asymptotic fill / 250ms drain);
   near-bottom smooth auto-scroll with a "Ver lo más reciente" pill when the
   operator scrolled up.
5. **LLM markdown is rendered, never trusted.** `react-markdown@10` +
   `remark-gfm`, Kira turns only, no `rehype-raw`, images disallowed
   (ADR-032). Code/tables scroll inside their block, never the page.
6. **Command palette as a reusable step framework.** `/`-or-`!` prefix opens a
   palette; commands are declarative step lists (Text/Select/Segmented/Tags/
   Switch steps, conditional `when?` gates) driven by one Stepper engine, ending
   in a Summary proposal with Editar/Descartar. Seven commands shipped as
   design-only mockups (zero network, test-enforced): /agenda, /perfil,
   /temas, /vivo, /acciones, /sesion (one-column), /musica. State registry:
   `conductor/tracks/ui_followups_20260715/proposal.md`.
7. **Persistence for spatial memory.** `useCollapsible(defaultOpen, persistKey?)`
   → `localStorage oc-collapse-<key>`; applied across Stream/Agenda; Controles
   (previously 8 inline cards in `MainStage`) became `ControlsPanel.tsx` with 4
   persisted collapsible groups. StreamPanel's duplicate local Collapsible was
   replaced by the shared component.
8. **Profiles become tangible.** Hover-intent (700ms) info card with the real
   stored prompt (single cached fetch per profile, `staleTime: Infinity` — the
   owner's no-repeated-DB-hits constraint), fade in/out on motion tokens; a
   per-row pencil opens the existing `ProfileEditor` in edit mode (real PUT —
   the editor supported edit mode but was mounted create-only).
9. **Backend honesty over cosmetic green.** The Motor chip's red state now
   names its culprit (empty "()" bug fixed); VRAM shows "no disponible" instead
   of a false "0 MB" (pynvml was missing — `nvidia-ml-py` installed, promotion
   to base deps proposed); a never-started Qwen counts as `unavailable`
   (green with Piper/Edge) while a spawned-and-died Qwen stays red — the health
   gate was adjusted with owner approval, TDD-first.

## Process lessons (paid for in blood)

- **A reviewer on a dirty tree must know the baseline.** A scope-discipline
  reviewer flagged pre-existing owner work (welcome carousel, credit link,
  machine-local backend config) as out-of-scope P0s and a fixer reverted/
  deleted it — including 4 untracked PNGs destroyed with `rm -f`. Everything
  was reconstructed from the reviewer/fixer transcripts and the owner's Codex
  image batch. Standing rule now in memory: capture `git status` before phase
  1; files dirty at baseline are OWNER work — report, never touch; fixers never
  `rm` untracked files.
- **Parallel writers need explicit file ownership** and a single orchestrator-run
  `pnpm build` at the end (the openapi prebuild regenerates a shared file).

## Consequences

66 test files / 626 tests green at close (day started at 533). The cockpit
reads calm-by-default, state changes are traceable to causes, and the chat is
the operational center (commands, markdown, mic). Wiring the mocked commands
1-by-1 and the auth enforcement decision are the natural next tracks.
