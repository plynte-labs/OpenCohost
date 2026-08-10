# ADR-044: Retiring the position-aware cut for the speech router

**Date**: 2026-08-10
**Status**: Accepted
**Branch**: `refactor/llm-engine-split-20260802`
**Track**: `interruptible_speech_architecture_20260804` (`step5-6-plan.md`, router steps 5-6)
**Author**: Claude Code, from `step5-6-plan.md` batch C
**Scope**: deletion of dead policy code + doc record. Source change: `opencohost/core/llm_engine.py`,
`opencohost/config/settings.py`, `opencohost/api/engine_host.py`, `opencohost/api/main.py`,
`opencohost/api/ptt_session.py`, `opencohost/core/speech/router.py` (comment only).

**Supersedes**: ADR-037 §2.1 (D1 — the position-aware cut policy) and ADR-038 §2.1 (the cut
seam implementation). ADR-037 §2.2/§2.3 and ADR-038 §2.2-§2.4 (D2 frozen-stash return, D3
connector floor/upgrade, the driver's return path) **remain in force** — this ADR does not
touch them.

---

## Context

Router steps 2-4 (commits `684843d`/`ecfeced`/`d8c30b3`/`6338b8a`) replaced the engine's
legacy blocking playback with a single speech-router thread that pauses on PTT press and
resumes losslessly (D3-uniform preemption: any priority-0 `submit()` preempts the active job
and pushes it on a stack, no fragment discarded). That router now does, generally and
losslessly, what ADR-037's D1 position-aware cut existed to do narrowly and lossily for one
source (agenda speech interrupted by PTT): stop the audio and come back to it later.

Runtime evidence that the router carries this load in practice: session 2 (2026-08-09)
recorded 12 cut/push/resume triples, all balanced, `[SPEECH_LOST] = 0`, including a real
depth-2 LIFO unwind (`runtime-validation-protocol.md`, Session 2 block).

D1's own seam — `MotorVocalIA.ptt_interrupt_if_agenda_speaking()`, the `CUT_ZONE_EARLY` /
`CUT_ZONE_LATE` / `CUT_THRESHOLD_SECONDS` constants, the `_ptt_position_cut_enabled` host
flag, and the `on_flush_precheck` wiring that fired it — had been **dead on every production
surface since router step 3 shipped**, verified by direct reachability analysis per surface
(`step5-6-plan.md` §1): CTK never sets the host flag and has no PTT flush path outside
`opencohost/api/`; the armed API host wires `on_flush_precheck` to `None` unconditionally
(`main.py`'s `stack_armed` gate). The only surface where the seam could still fire was the
kill-switch-off revert target (`_speech_router_enabled = False`).

---

## Decision

Delete the D1 seam outright: `ptt_interrupt_if_agenda_speaking()`, the three zone constants,
`_ptt_position_cut_enabled`, and the `on_flush_precheck` hook end-to-end (`ptt_session.py`
constructor param + `_dispatch` wiring, `main.py`'s `stack_armed` computation and dict entry,
`engine_host.py`'s flag write). `speech_pause_would_fire` — the step-0 telemetry probe that
had exactly one production caller, the now-deleted `on_flush_precheck` wiring — is deleted
with it (router step 6); the `[SPEECH_PAUSE]` log marker it owned disappears and is not
depended on by `runtime-validation-protocol.md`'s greps.

Interruption is now uniform under D3: any priority-0 `submit()` preempts whatever is playing,
losslessly, regardless of source or playback position. There is no second, narrower cut
mechanism running beside the router.

The frozen-stash / detour / connector-return machinery (D2/D3, ADR-037 §2.2-2.3, ADR-038
§2.2-2.4) is unaffected — it keeps its non-deleted implementation and gains a narrower single
producer (see Consequences (b)).

---

## Consequences

**(a) The kill-switch revert target loses mid-turn agenda interruption.**

Before this change, flipping `_speech_interrupt_enabled = False` or `_speech_router_enabled =
False` in source and restarting re-armed the D1 zone cut (`main.py`'s `stack_armed` went
`False` → the hook got wired → `_ptt_position_cut_enabled` was already `True` on the API
host) — `_ptt_controller_hooks()` runs exactly once, inside `lifespan()` at process startup, so
this was never a live runtime toggle. After this change it does not: a PTT arriving during an
agenda turn is answered at the turn boundary — the pre-2026-07-22 behavior, before WU5 shipped.

This is accepted, not merely tolerated, for the reasons already on record
(`step5-6-plan.md` §6.2), restated here verbatim because they are the decision-record
consequence this ADR exists to capture:

1. It is what the design already decided (`speech-router-design.md` §0 row 8, §5.4, §8 step
   5), before this plan existed.
2. What is lost is a *lossy* heuristic. The zone cut deferred below 25% and above 75%
   progress, and inside the mid band it still deferred whenever the remaining estimate was
   under 20s or no next-turn draft existed. It also cut with a bare `interrupt_speaking()` —
   **fragment loss**, which is the exact defect this whole track exists to remove.
3. The revert target is a safety net for "the router is misbehaving on stream," not a product
   mode. Losing an interruption *nicety* there is strictly better than keeping a second, lossy
   cut seam alive next to the router.
4. Resurrection cost: a single `git revert` of the step-5 commit restores the constants, the
   method, the wiring, and the flag together. **Steps 5 and 6 ship in ONE commit for exactly
   this reason** — atomic revert, nothing half-restored.

**(b) The frozen stash keeps one producer, and its meaning narrows.**

Before this change the stash had two producers: the deleted seam (G5 freeze-or-defer) and the
`pregenerate()` slot handover (a `direct`-sourced turn displacing a cached agenda draft,
`llm_engine.py:2323-2332` at plan time). The first was already unreachable in production
(above), so this is the deletion of a dead producer, not a live rewire — the surviving half is
untouched by a single production line. The stash's meaning narrows from *"the beat we lost
when a PTT cut the agenda"* to *"the next-turn draft a direct turn displaced from the pregen
slot"* — comments across `llm_engine.py` were reworded to say so (§4.1/§4.2 of the plan).

**(c) `speech_pause_would_fire` is deleted with it.**

The step-0 telemetry probe's only gate — whether `on_flush_precheck` was wired — closed the
day router step 3 shipped, making the probe dead code from that point forward. Its deletion
here is bookkeeping, not a new decision.

**Fixed verification gap.** Batch A's mandated per-test differential check (flip the
`speech_remaining_estimate` stub to `0.0`, the rewired test must fail) did not discriminate for
5 of the connector-upgrade tests reused across this deletion — not a pre-existing methodology
gap, but the rewire itself WORSENING discrimination versus base: pre-deletion, the same `0.0`
flip drove the old `ptt_interrupt_if_agenda_speaking` cut path to `defer` and each probe went
RED; post-deletion, `_freeze_stash(motor)` (this batch's replacement helper) freezes
unconditionally, so the flip no longer reaches any of the 5 tests' own named gate at all.
Fixed (Judgment Day round 2) by converting each of the 5 into a refusal→release pair inside the
same test: after the existing negative assertions (the named gate refuses at the shipped
`25.0` stub), the test's own named blocking condition is removed and the same entrypoint is
re-driven, asserting the upgrade now proceeds — proving the named gate, not an earlier one in
the chain, was the decider. Full detail, gate-by-gate and test-by-test, is recorded in
`speech-router-design.md` §14 rather than duplicated here.

---

## Reversal

`git revert` of the step-5 commit. Nothing in steps 5-6 is stateful, persisted, or
migration-shaped: no schema, no on-disk format, no settings the operator may have tuned
survives with a changed meaning — the three deleted constants are removed outright, not
repurposed.

---

## Related documents

- `conductor/tracks/interruptible_speech_architecture_20260804/step5-6-plan.md` — the
  execution plan this ADR closes out (§1 CTK reachability proof, §6.2 revert-target decision
  record verbatim, §7 batch C).
- `conductor/tracks/interruptible_speech_architecture_20260804/speech-router-design.md` — §14
  STEP-5/6 LEDGER, the deletion inventory and test bill.
- `docs/adr/ADR-037-agenda-interruption-policy-position-aware-ptt.md` — §2.1 superseded here;
  §2.2/§2.3 remain in force.
- `docs/adr/ADR-038-agenda-interruption-implementation-frozen-stash-return.md` — §2.1
  superseded here; §2.2-§2.4 remain in force.

## Update log

- **2026-08-10**: Created. Records the D1 seam deletion, the kill-switch revert-target
  consequence, the frozen-stash producer narrowing, and the R3-gate verification limitation.
