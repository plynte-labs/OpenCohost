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
- batch 0 — session docs setup.

## Progress
- [ ] FR1
- [ ] Bug A (music threads)
- [ ] Bug B (terminal slider)
- [ ] Bug C (product panel)
- [ ] Investigation D (UI/stack ADR)
