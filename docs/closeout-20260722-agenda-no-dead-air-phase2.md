# Phase 2 Closure — Agenda "No Dead Air" (`agenda_no_dead_air_20260719`)

Report for the owner: what shipped, why, where, what's left to validate, and
what we learned. Policy decisions are in ADR-036/ADR-037; the WU5
implementation is in ADR-038. This document is the phase-level summary that
ties the five work units together.

## Context

Agenda mode had a dead-air problem: every turn boundary paused for 16-19s of
LLM generation (decode 14.5-17.2s) with the GPU otherwise idle, and a typed
question arriving mid-speech didn't generate in the background at all —
observed silence ran 31.7s to ~90s, worse than the agenda case it was
compared against. Fase 1 (commit `748f235`, ADR-035) closed the agenda-to-agenda
half of this by prefetching the next turn on a background thread while the
current turn's TTS played. Runtime-validated: 4/4 consume boundaries landed
at 0.34-0.43s dead air versus a 36s-average fallback — roughly a 90x
reduction, about 150s of dead air removed in a 12-minute run.

Fase 1's own validation surfaced the next problem: a µs-scale race where the
prefetch speaker thread and the engine worker's own pop-and-speak path could
both reach `_hablar` at a turn boundary, plus the still-unsolved interactive
dead-air case. Fase 2 (this phase, five work units) rebuilt speech dispatch
around a single pregenerated-turn cache consulted at queue pop, extended
pregeneration to interactive replies, and — once the owner resolved three
gating product decisions — implemented PTT interruption of agenda speech with
a connector-based return.

## The five work units

**WU1 — `26c06f5`** (RED race test). Pinned the `_hablar` re-entrancy window
with a real `MotorVocalIA`, fake Ollama, no-op TTS, and an injectable hook at
the pop → `_processing=True` boundary — deterministic, no sleeps. The first
version was itself judged: Judge A (Fable) found a self-contradictory pass
condition and that the test pinned the CTK-legacy entry point WU2 was meant
to keep, not delete; Judge B (Opus) found the same legacy-path issue plus an
xfail that would swallow harness breakage. Root cause was a latent
contradiction in the design draft itself (§1 said the legacy path was
deleted, §2.1 v2 said it wasn't) — fixed in `design-fase2.md` [v3]. The fixed
version drives the real `AgendaDriver` consume path, distinguishes harness
failure from legitimate red, and self-verifies its hook window.

**WU2 — `88377c3`** (speech serialization through the worker). Replaced the
agenda-only prefetch cache and its parallel speaker thread with a
source-agnostic pregen slot consulted at queue pop
(`_take_pregen_if_match` / `_speak_pregenerated`), making two threads calling
`_hablar` structurally impossible in the API host rather than merely
unlikely. Judge A (Fable) found a critical: consume-at-event made a latent
unbounded worker recursion deterministic (`_complete_processing_cycle`
recursing into `_process_priority_queue`, guaranteed a queued item before
every `finally`, adding two stack frames per turn — a `RecursionError` after
roughly 480 chained turns, i.e. 3-6 hours of continuous agenda, meaning
permanent silence). Fixed with an iterative drain that keeps the stack flat
across 50 chained turns. A second fix made the clear logic match-aware
(`_clear_prefetch_unless_matches`), closing a self-nuke trap where consuming
a draft via `replace_pending` could wipe the very entry it had just adopted.

**WU3 — `319d2fd`** (pregeneration of queued interactive replies). A typed or
PTT reply now generates during the ongoing TTS instead of waiting for speech
end, via a generalized `pregenerate()`, a narrow `_llm_generating` flag, a
head-only trigger in `enqueue()`, and priority-based slot eviction. Round-1
judges (both, independently) found a blocker: pregeneration dropped
`history_text`, so a reply served from cache committed the raw prompt
template — not the rendered dialogue — to `historial`/`memoria`, reintroducing
a regression the project had already fixed once. Also found: in-flight
eviction could spawn zombie Ollama workers holding a poisoned shared `Event`,
and the same-item fallback path could race its own pregeneration worker
instead of waiting on it. Fixes: `history_text` carried end-to-end; eviction
restricted to cached-only entries (in-flight generations are never evicted);
the same-item path now always waits on `_prefetch_done` rather than racing a
fresh foreground generation.

**WU4 — `7bd4811`** (boundary telemetry, guardrail surfacing, retry-once,
pairing guards). Added one greppable `Pregen boundary:` INFO line per turn
boundary (`draft=used|late|rejected|none|evicted`), a guardrail-rejection
event surfaced to the UI Logs tab (rejection code only, never dialogue text),
an adaptive retry-once on guardrail rejection gated on estimated remaining
speech time, and a stash-pairing structural guard. The judged fix round
corrected `speech_remaining_estimate()` to track a real `first_play`
timestamp — without it, a slow-to-synthesize first TTS fragment inflated the
estimate and could trigger a retry the remaining speech couldn't actually
cover — and corrected the pregen wait bound from an overstated 2x-the-watchdog
figure to the real 1x ceiling.

**WU5 — `a25d780`** (PTT interruption + frozen-stash return via connector).
Implements ADR-037's three policy decisions: PTT-only position-aware cuts
(early/late zones defer, mid-zone cuts only past a remaining-speech
threshold and only if a draft was actually frozen), return-by-default at
clean boundaries with deterministic skip conditions, and a two-layer
connector (an always-available parameterized template pool plus an
opportunistic, latency-safe generated upgrade). Full detail — including what
two rounds of blind judging changed — is in ADR-038.

## Judging method

Per work unit: implement → independent verify → two blind judges (Fable +
Opus, no cross-visibility) → synthesized fix round addressing every
confirmed finding → re-judge when blockers or criticals remain open. WU5
needed two full rounds: round 1 caught a blocker (the return path could fire
before the interruption's own answer had played) plus three criticals; round
2, run after the round-1 fixes, caught three more criticals that only became
visible once the first set was closed.

## Key discoveries

| Discovery | Work unit | What made it dangerous |
|---|---|---|
| Deterministic recursion bomb | WU2 | Consume-at-event added 2 stack frames per chained turn; `RecursionError` only after ~480 turns (3-6h) — silent until a long unattended stream hit the wall |
| `history_text` dropped by pregeneration | WU3 | Cache-served replies committed the raw prompt template to `historial`/`memoria` instead of rendered dialogue — a memoria-quality regression the project had already fixed once |
| Zombie in-flight eviction | WU3 | Evicting an in-flight (not just cached) pregen left an orphaned Ollama worker holding a poisoned shared `Event` |
| Estimate inflation from synthesis warm-up | WU4 | `speech_remaining_estimate()` without a real `first_play` mark let a slow first TTS fragment trigger a retry the remaining speech couldn't cover |
| Return-before-answer | WU5 (round 1, blocker) | The PTT answer rides the command queue, invisible to the driver's tick-based return gates — the stashed topic could resume before the interruption was ever answered |
| Unbounded hold | WU5 (round 2, critical) | Three independent paths (queue `is_ready` drop, TTL sweep, post-cut dispatch exception) could lose the interruption answer without incrementing the detour counter, holding the agenda silent forever with no backstop |
| One-directional occupancy / double-release | WU5 (rounds 1-2) | The connector upgrade's `_llm_generating` claim could be released by more than one code path, letting an unrelated generation start while believing the engine was free |
| Auto-resume starvation | WU5 (round 2, critical) | The frozen-stash return check ran unconditionally and could race or block the existing `PAUSED_NEEDS_OPERATOR` auto-recovery path |

## Accepted residuals and follow-ups

- The frozen-stash hold releases on **any** interactive turn, not
  specifically the one answering the interruption — a chat item queued
  before the cut can release the return early. Ordering glitch only; the
  interruption answer still speaks, no data is lost.
- `_note_detour_turn` is not called on the `is_ready`-drop and
  accumulated-flush branches, so those paths don't increment the detour
  counter directly. Covered by the 90s hold-timeout backstop rather than
  fixed structurally.
- The `main.py` `on_flush_precheck` wiring test replicates the guarded-`getattr`
  idiom rather than exercising `create_app` end-to-end. Follow-up, not a
  blocker.
- Carried from WU4: the unlocked `rejection_log[-1]` diagnostic read in
  `_agenda_rejection_code()` (mislabel-risk only, closed vocabulary keeps
  privacy intact) and the pre-existing `_format_agenda_rejection` 50-char
  snippet reaching the app log — both flagged for a future thread-safety /
  privacy sweep, neither fixed in this phase.

## Verification gates used

- Race test run 3x for determinism on every work unit that touched the
  serialization path.
- Focused + regression battery per work unit (165-705 tests depending on the
  unit's blast radius); WU5's gate was 314 focused + regression tests green.
- Running the full project suite (thousands of tests) was not attempted for
  each work unit — it exceeds the harness's per-command time budget, so
  verification relied on the focused/regression/blast subsets scoped to each
  change's actual call graph instead.

## What remains for the owner

Runtime validation of Fase 2 has not happened yet — everything above is unit
and integration-tested, not confirmed against a live session. Specifically:

1. Typed interactive replies generating and playing correctly during ongoing
   agenda TTS (no dead air at the boundary).
2. PTT zone cuts — early/late defer, mid-zone cut-or-defer by the
   remaining-speech margin — behaving as designed against real speech timing.
3. The connector return: floor template plays correctly, topic title
   substitution reads naturally, and (if timing allows) the generated
   upgrade doesn't add latency.
4. Guardrail-rejection visibility in the UI Logs tab.
5. The `Pregen boundary:` telemetry lines in a real log, confirming the
   `used/late/rejected/none/evicted` classification matches what actually
   happened in a session.
