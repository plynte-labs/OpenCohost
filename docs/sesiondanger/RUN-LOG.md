# Session: Danger Overnight — 2026-06-25

Autonomous batch on branch `session/danger-overnight-20260625` (off master `04893d7`).
Owner asleep. All work on ONE branch; batch commits = rollback points; NOT merged to master
(owner reviews on wake). Every task: phases + two blind opus judges; tests distrusted (verified
against real object surfaces, not just mocks). Doubts / new major bugs / design decisions are
captured in `FOLLOWUPS.md` and engram — never block.

(Renamed from SESSION-LOG.md — `.gitignore` rule `session-*.md` was eating that filename.)

## Scope (strict — owner-specified, do not wander)
1. **FR1** — `motor_ia.interrupt_speaking()` (Demeter HIGH fix, ADR-AUD-005) — same phased+judge discipline.
2. **Bug A — music "Probar"**: clicking the test/play button repeatedly spawns multiple music
   threads/tracks; must stay exactly ONE.
3. **Bug B — startup terminal**: app launches with the log/terminal slider showing "collapsed"
   but the panel renders open (inconsistent state). Make state and view agree.
4. **Bug C — product panel**: launches collapsed; must show expanded so the user can see the
   available configurations.
5. **Investigation D (INVESTIGATION ONLY, no migration)**: UI design audit ("less is more" —
   modern/professional), and whether CustomTkinter is insufficient vs alternatives (Rust TUI,
   TypeScript+Tauri for the UI layer only). Produce an ADR; change nothing.

## Ground rules
- One branch, batch commits, no master merge.
- Phases + 2 blind judges per task; apply only confirmed fixes; full suite before each commit.
- Distrust tests: judges scrutinize for tautology/over-mock; verify against real attribute surfaces.
- No `git checkout/revert/stash/reset` inside sub-agents (prior incident).
- Document WHY in `docs/sesiondanger/ADR-SD-*.md`.

## Rollback points (batch commits)
- batch 0 — session docs setup (`RUN-LOG`, `FOLLOWUPS`).
- batch 1 — **FR1** `interrupt_speaking()` Demeter fix (`5f83724`). Dual opus judges SAFE. Suite 2783.
- (ADR-SD-003 commit — UI design/stack investigation.)
- batch 2 — **Bug A** music-preview single-flight (`120883b`). Judges: code correct; tests rewritten non-vacuous. Suite 2788.
- batch 3 — **Bug B+C** startup panel visibility (`da8b637`). Judges: code correct; Bug C runtime test added. Suite 2792.

## Progress — ALL SCOPED WORK DONE
- [x] FR1 — `5f83724` (batch 1). Demeter HIGH fix.
- [x] Bug A (music threads) — `120883b` (batch 2). Single-flight; FR3-regression fixed. ADR-SD-001.
- [x] Bug B (terminal slider) — `da8b637` (batch 3). grid_remove in build().
- [x] Bug C (product panel) — `da8b637` (batch 3). Compact-default flipped; ADR-SD-002 (reverses ui_declutter — owner confirm).
- [x] Investigation D — ADR-SD-003 (recommend: stay on CustomTkinter + design system; never Rust TUI; Tauri/Qt post-launch only).

## Final state
- Branch `session/danger-overnight-20260625`, NOT merged to master. Owner reviews.
- Full suite green at every batch (final 2792 passed, 2 skipped).
- `opencohost/ui/stream_admin_ui.py` — owner's uncommitted NOTE_DEVELOPER note, intentionally left untouched.
- Open follow-ups in `FOLLOWUPS.md`: B1 (aggregator locking), residual `_last_switch_failure` Demeter reach, `_make_shell_stub` spec hygiene, stale `_compacto_active` getattr fallback default.
- Owner decisions pending: ADR-SD-002 (compact-default reversal) and ADR-SD-003 (UI/stack direction).
