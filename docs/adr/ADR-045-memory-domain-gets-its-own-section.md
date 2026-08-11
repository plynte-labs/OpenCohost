# ADR-045: The memory domain gets its own section, and the chat column is never gated

**Date**: 2026-08-11
**Status**: Accepted
**Branch**: `codex/ui-ux-audit-proposal-20260709` (OpenCohost_UI repo — separate from this one)
**Track**: UI/UX audit follow-up, owner-driven; no SDD track opened
**Author**: Claude Code
**Scope**: Tauri UI only. `OpenCohost_UI` commits `48016d3`, `ab96bae`, `02e4e56`. No backend
change, no mutation semantics touched, no request body or query key altered.

**Relates to**: ADR-002 (UI presentation refactor) — same instinct, different UI generation.
This one is about the React/Tauri shell, not the CustomTkinter one.

---

## Context

Three cards — `MemoryCard`, `PersonalizationCard`, `EditorialCardsCard` — lived inside a single
collapsible group in Controles. `MemoryCard` alone is 763 lines, 27 hook call sites, 99 i18n keys
and a 958-line test file. It was also the only card in the app that boxed its own list
(`max-h-96`) rather than trusting the panel scroll every sibling relies on. With ~118 saved
memories the operator's complaint was direct: reading them from inside that accordion is
unusable.

A prior design brief (`docs/MEMORY_SURFACE_HANDOFF.md`, UI repo) recommended a `Dialog` over a
nav section, on the grounds that a section gets ~727px while a dialog gets ~1350px. That brief
was written by the same agent that had, one section earlier in the same document, established
that Controles cards already render at **630–850px**.

## Decision

### 1. The three cards move to a top-level `memoria` section

`src/features/memoria/`, reached from a nav entry between Música and Controles. The Controles
group is deleted; Controles drops from eight cards to six.

### 2. The section-vs-dialog recommendation is overruled

The owner chose the section. The space argument that favoured the dialog was self-refuting: if
cards already render at 630–850px, width was never the constraint. The constraint was one card
carrying too much responsibility inside a container that gave it no room to breathe. A domain
with its own nav entry is the honest structure; a modal is a place you visit and must close.

### 3. `ConversationPanel` is mounted for **every** section. Unconditionally.

A first implementation collapsed the 465px queue column to 0px and hid the chat while Memoria was
active, buying back the width the space argument had asked for. This made Memoria the only
section in the app without the chat. It was reverted.

This is now a standing rule with a comment in `AppLayout.tsx` and a regression test in
`AppLayout.test.tsx`. Two distinct reasons hold it up:

- **Consistency.** The operator is streaming. The queue column is their live view of what Kira is
  doing. Losing it in exactly one section is a defect, not a layout trade.
- **State.** `ConversationPanel` owns the session transcript, the composer draft, the active tab
  and `seenLogId` as local `useState`. Unmounting it on a section change destroys the operator's
  conversation. This is the same trap already documented in that file for `MusicPanel` and the
  shared `<audio>` element, which is why `PlaybackProvider` was hoisted above the section switch.

Note that `display: none` avoids the state loss but not the consistency problem. Both are
forbidden.

### 4. One pane at a time, via the existing `Segmented` primitive

Stacking the three cards in page flow meant scrolling past ~118 memory rows to reach
Personalization — the original complaint made longer rather than fixed. A `Segmented` control now
mounts exactly one pane; the other two are **absent from the DOM**, not hidden, so the operator
does not pay 118 rows' worth of hooks while editing something else. Selection persists under
`oc-memoria-pane`.

`Segmented` was chosen over a vertical sidebar because with the chat column restored the section
is ~695px, and a 180px rail would starve the memory editor's textareas. No new primitive was
built; `Segmented` already serves 11 call sites for short mutually-exclusive enums.

### 5. `Dialog` portals to `document.body`

`Card` sets `backdropFilter: var(--surface-blur)`, which on the aurora theme is a real blur. Per
CSS Filter Effects that makes the element a containing block for `position: fixed` descendants,
so `Dialog`'s overlay positioned against the enclosing Card rather than the viewport — and every
mount is inside one. Same fix `Select` took earlier in this batch.

## Consequences

**Accepted.**

- i18n keys keep their historical `controles.*` prefixes even though the components left
  Controles. Renaming ~120 keys across two compile-checked bundles buys nothing and risks a silent
  miss. The panel documents why.
- `localStorage["oc-collapse-controles-memoria-personalizacion"]` is orphaned. No migration; it is
  a collapse toggle.
- "Memoria" now reads three times on that screen (nav item, segment label, card heading). Left
  alone deliberately — renaming a card title is a product call.

**Deferred.**

The master-detail rework of the memories list is still the right idea and still unbuilt. Each row
owns three `useState`, its own query and three mutations; the win is *deleting* that machinery,
not adding surface. The Segmented split means the cost is only paid while on that pane, which is
why it stopped being urgent. Brief retained in `docs/MEMORY_SURFACE_HANDOFF.md` §4–§5, including
the R8 privacy constraint that makes selection-as-activation mandatory rather than optional.

## Verification

`tsc --noEmit` exit 0. Suite 84 files / 1076 tests → **85 / 1081**, green throughout; never red at
any commit.

Two guards were confirmed load-bearing by deliberately breaking the code and observing the
failure, then restoring:

- Swapping the queue wrapper to a conditional render turns the composer-draft survival test red.
- Rendering all three Memoria panes and hiding two with a `hidden` class turns **all three**
  `MemoriaPanel` tests red. This is why they assert `not.toBeInTheDocument()` and not
  `not.toBeVisible()` — the latter would pass under exactly the design the tests exist to forbid.

**Not verified, and no test can verify it.** jsdom has no layout engine, so nothing here proves
the `Dialog` now centres on aurora, that the 0px→465px column behaves under the existing
transition, or that ~118 rows read sanely under the panel scroll. `docs/OPEN_WORK.md` §1 lists
what a human still has to look at in a real window. A green suite after a geometry change means
"nothing else broke", never "the fix works".

## What this ADR does not claim

That the memory surface is finished. It is relocated and no longer boxed. The per-row cost is
unchanged, and the list is still a flat render of every memory the profile owns.
