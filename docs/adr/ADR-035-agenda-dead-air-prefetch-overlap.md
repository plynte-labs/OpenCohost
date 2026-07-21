# ADR-035: Agenda Dead Air — Prefetch Overlap Wiring (API Host, Fase 1)

**Status**: Accepted (implemented, runtime-validated)
**Date**: 2026-07-21
**Track**: [`conductor/tracks/agenda_no_dead_air_20260719/`](../../conductor/tracks/agenda_no_dead_air_20260719/) (`proposal.md`, `design.md`)
**Runtime evidence**: `logs/opencohost_20260721_012124.log` (dead air confirmed, pre-fix), `logs/opencohost_20260721_131040.log` (fix validated, post-fix)
**Tests**: 43 focused + 108 regression, 309 blast-radius — green. Adversarial review: 2 blind Opus judges, zero blockers/majors.
**Related**: ADR-003 (same agenda state machine — TOPIC_CLOSING), ADR-036 (Fase 2, builds on this)

---

## 1. Context

Kira's agenda mode runs autonomous monologue turns end-to-end without a human
driving the conversation. In the CTK desktop shell this has never had a dead-air
problem: `app_shell.py`'s Tk `after`-loop calls into the motor mid-speech to
generate turn N+1 while turn N is still playing on TTS, so generation and audio
overlap. The headless API host — the Tauri/API backend that also drives Kira —
implements the same agenda state machine (`KiraAgendaController`) but never wired
CTK's prefetch primitives to it.

The owner reported dead air between agenda turns in the API host: multi-second
silence, GPU idle, while the audience is presumably still listening to (or has
finished) the prior turn.

### 1.1 Runtime confirmation

`logs/opencohost_20260721_012124.log` (gemma4:e4b, ~70s turns) shows every
turn boundary strictly sequential:

| Speech ends (TTS done) | Next gen starts | Gen done (TTS resumes) | Dead air |
|---|---|---|---|
| 01:35:24 | 01:35:24 | 01:35:42 | 18.5s |
| 01:36:52 | 01:36:52 | 01:37:09 | 17.1s |
| 01:38:18 | 01:38:18 | 01:38:37 | 18.4s (`kira-agenda-stop`) |
| 01:39:43 | 01:39:43 | 01:40:00 | 17.1s |
| 01:41:14 | 01:41:14 | 01:41:30 | 16.3s |

`Pipeline TTS completado` → same-second `Cola prioritaria: procesando
[kira-agenda]` → 16-19s LLM generation → next playback, on every boundary
including stop turns. Zero prefetch lines in the log. Speech windows run
58-73s, so a 16-19s generation fits roughly 4x inside one — the overlap
opportunity was there and unused.

### 1.2 Root cause

`AgendaDriver` (`opencohost/api/agenda_driver.py`) ticks on a ~4.5s cadence;
`next_action()` returns `none()` while the motor is speaking
(`kira_agenda_controller.py:809-810`), so nothing runs until `speaking_end`
triggers a `nudge()` — at which point generation for N+1 starts *after* audio
has already stopped. Compounding this, `enqueue_agenda_action` unconditionally
called `motor.clear_prefetched_agenda()` (former `agenda_driver.py:56-61`) with
a comment stating the headless host never prefetches. The preview validator
prefetch needs was already wired headless (`engine_host.py:422`, guardrail
path) — nothing called it.

CTK's working mechanism (all primitives host-agnostic, confirmed by read-only
exploration):

- `MotorVocalIA.prefetch_agenda()` (`llm_engine.py:840`) generates N+1 on a
  background thread with `commit_history=False`, stashes the result in
  `_prefetched_agenda`, guarded by `_prefetch_lock` + `_prefetch_epoch`
  invalidation.
- `play_prefetched_agenda()` (`llm_engine.py:906`) speaks the cached dialogue
  with no second LLM call, committing history only at playback.
- `clear_prefetched_agenda()` (`llm_engine.py:900`) bumps the epoch so an
  in-flight stale generation can't land after a clear.
- CTK's controller-side counterparts: `prefetch_action_after_current_speech()`
  (SPEAKING-state-only preview, no state mutation) and
  `start_prefetched_action()` (staleness re-validation at consume time).

The API host had every primitive available and simply never called them.

### 1.3 CTK-era gotchas the design had to respect

Prior incidents in the same state machine, tracked as Engram observations:

- **#468** — TOPIC_CLOSING deadlock: when a `kira-agenda-stop` prefetch is
  consumed via `start_prefetched_action`, the `speaking_start` handler must
  already recognize `TOPIC_CLOSING`, or the controller never advances (this is
  the exact class of bug fixed in ADR-003, in a different code path — the fix
  there was `app_shell.py`'s `_on_motor_speaking_start`; the equivalent guard
  in the API host's `route_motor_event_to_agenda` already included
  `TOPIC_CLOSING`, so Fase 1 only needed a regression test locking it in).
- **#338** — PTT/typed input can be delayed behind prefetched agenda turns
  (priority inversion): prefetch must yield to interactive traffic, both at
  start and at consume time.
- **#344** — speaking-event lifecycle was source-blind, and a stale prefetched
  draft could be retained after higher-priority work paused the agenda.

---

## 2. Decision

Mirror CTK's proven prefetch loop headlessly by making `AgendaDriver` the
owner of prefetch orchestration, driven by the same motor events CTK uses
(`speaking_start` / `speaking_end`), not by TTS progress polling.

### 2.1 Trigger — `speaking_start`

`route_motor_event_to_agenda` (`agenda_driver.py:96-134`) gained an optional
`on_agenda_speaking_start` callback, fired only after `mark_generation_accepted()`
actually advances the controller to `SPEAKING` — i.e. only for controller-generated
agenda turns (the #344 source gate). `engine_host.py`'s
`_handle_agenda_motor_event` (`:507-534`) wires this to
`driver.maybe_start_prefetch(agenda, self.motor)`.

`AgendaDriver.maybe_start_prefetch` (`agenda_driver.py:255-284`) — caller holds
`agenda_lock` — guards:

1. `motor.current_speech_source` must start with `kira-agenda` (a human/other
   path owns the mic — #344).
2. `not motor.has_pending_priority_before(2)` — interactive work must not
   already be queued ahead of agenda priority (#338).
3. No existing stash.

On pass: `agenda.prefetch_action_after_current_speech()` previews the next
action, then `motor.prefetch_agenda(action.prompt, priority=..., source=...)`
is called. `prefetch_agenda` (`llm_engine.py:840-876`) only spawns a daemon
worker thread and returns immediately — the whole trigger runs under
`agenda_lock`, but generation itself never does. The driver stashes
`PrefetchState(action, topic_id)` (`agenda_driver.py:44-55`), pinning the draft
to the topic active when it was spawned so a topic change before consume
invalidates it.

### 2.2 Consume — `speaking_end`, before `next_action`

`AgendaDriver._tick_locked` (`agenda_driver.py:217-252`) calls
`_maybe_consume_prefetch` **before** `next_action()` is ever reached. This
ordering is structural, not a convention to remember: a valid
same-continuation draft is always spoken before an enqueue could clobber it,
because the enqueue path is physically later in the same tick body.

`_maybe_consume_prefetch` (`agenda_driver.py:286-343`) guard chain, each
failure (except "not ready") clearing the stash and falling through to a
plain nudge:

1. Topic still active and matches the stashed `topic_id`.
2. `agenda.state == WAITING_SIGNAL` (not yet at the post-speech boundary —
   falls through silently, no clear, so a mid-speech cadence tick can't drop
   an in-progress draft).
3. `not motor.has_pending_priority_before(priority)` — interactive first
   (#338).
4. No non-agenda audio work in flight (mirrors
   `kira_agenda_has_non_agenda_audio_work`, #344).
5. `motor.wait_prefetched_agenda(timeout=0)` — non-blocking. If not ready and
   `motor.prefetch_pending()` is true, set a 0.25s re-tick
   (`_next_wait = PREFETCH_RETICK_SECONDS`) and return "handled" without
   clearing — this is the headless analog of CTK's 50ms `after` poll. If not
   ready and not pending, the worker finished with nothing (error/guardrail
   reject) — clear and fall back.
6. `agenda.start_prefetched_action(stash.action)` — staleness re-validation;
   false clears and falls back.
7. `motor.play_prefetched_agenda()` — speaks the cached text with no second
   LLM call. If this returns false (engine cache raced empty between adoption
   and play — a latent CTK race, not new), the controller state is restored
   to `WAITING_SIGNAL` rather than left stuck in `GENERATING`, and a short
   re-tick is requested.

### 2.3 New engine primitive — `prefetch_pending()`

`llm_engine.py:884-893` adds a read-only tri-state helper: true only while a
prefetch worker is alive and no draft is cached yet. This distinguishes "draft
is late, keep waiting" from "worker is done and produced nothing" — the
distinction Fase 1's consume guard #5 depends on to choose between a 0.25s
re-tick and an immediate fallback.

### 2.4 Conditional clear on enqueue

The previously-unconditional `motor.clear_prefetched_agenda()` inside
`enqueue_agenda_action` (`agenda_driver.py:58-93`) is kept for genuine new
enqueues — a fresh generation must supersede any draft — but is now paired
with `driver._drop_prefetch_stash()` in the same call, and is reachable only
when §2.2's consume-before-`next_action` ordering already found nothing valid
to consume. The "never clobber a valid draft" property is therefore
structural (call ordering), not a flag checked at the clear site.

### 2.5 Staleness on model/persona switch

`switch_llm_tier` and `set_profile` handlers (`llm_engine.py:700`, `:740`)
each gained a `self.clear_prefetched_agenda()` call at the top — a draft
generated under the old model or persona must never be spoken under the new
one (#344-class staleness). This runs on the engine thread without
`agenda_lock`; see §4 for the residual ordering gap this leaves open.

### 2.6 Lock and priority discipline

- `agenda_lock` is held for every controller touch; the only entry points
  into the new driver methods are `_tick_locked` and
  `_handle_agenda_motor_event`, both already locked.
- `agenda_lock` is never held across LLM generation: `prefetch_agenda` spawns
  a daemon worker and returns; `play_prefetched_agenda` spawns a speaker
  thread likewise.
- Fixed lock order `agenda_lock` → motor locks is preserved
  (`agenda_driver.py:16-19`); the prefetch worker only ever takes
  `_prefetch_lock`, never `agenda_lock`.
- Prefetch generation does not enter the priority queue — it is a direct
  background call. The eventual turn's queue priority stays 2 (agenda).
- Stop turns (`kira-agenda-stop`) are prefetched, matching CTK parity — the
  #468 guard (`TOPIC_CLOSING` in the `speaking_start` state set,
  `agenda_driver.py:121-129`) already covers the closing turn's own
  `speaking_start`, so the stop-turn cycle completes; a regression test locks
  this in (`test_stop_turn_prefetch_full_cycle_no_topic_closing_deadlock`).

---

## 3. Runtime-validated results

`logs/opencohost_20260721_131040.log`, owner live session, post-fix:

- **4/4 consume boundaries at 0.34-0.43s dead air**, versus ~36s average
  fallback cost — roughly a 90x reduction, ~150s of dead air removed over a
  12-minute session.
- **One guardrail rejection** (`contains_internal_leak`) on a prefetch worker
  → clean fallback to plain generation for that boundary (today's cost, one
  turn). This also revealed a gap: the rejected turn forfeited its remaining
  45s of speech window with no retry — tracked as a Fase 2 solidification
  item (retry-once), not fixed here.
- **Post-interactive agenda resume pays full generation cost (~39s) by
  design** — the #344 source gate means an interactive turn's speech window
  is not agenda speech, so no agenda draft was ever spawned for it.
- **Decode throughput halved (16 → 34 ms/tok)** as the prompt grew from
  ~1,500 to ~2,900 tokens over the session — a margin-shrinking trend, not
  regressed by this change but worth tracking: the overlap only pays off
  while generation time stays under the speech window.

Multi-agent validation (Opus log validator, Sonnet CTK verifier, Sonnet system
critic) confirmed CTK does **not** background-generate interactive turns:
both shells share the identical engine path for typed/PTT input, and both
wait for TTS end before generating the next turn on that path. Prefetch is
agenda-only by the hard source gate in §2.1; `HANDLE_STREAMER`/`ptt_text` topic
resume is test-only, never reached by production callers. This sets the
baseline Fase 2's interactive-pregeneration decision (ADR-036) works against.

---

## 4. Accepted residual risk

**Concurrent `_hablar` at the turn boundary (low-probability, CTK-parity, not
introduced by this change).** `play_prefetched_agenda`'s speaker thread
(`llm_engine.py:906-926`) calls `_hablar` (`llm_engine.py:3120`) independently
of the engine worker's own call path. `_hablar` has no `_speaking`
re-entrancy guard. At the exact turn boundary, the worker's
`_process_priority_queue` may pop a just-arrived interactive item
(`llm_engine.py:1110`) and start speaking it before `_processing = True` is
set (`:1118`) — there is a microsecond window in which the driver's consume
guards (`has_pending_priority_before`, non-agenda-audio check) read "clear"
on both sides, allowing brief audio overlap and a #338-class inversion. The
same window is reachable via the empty-queue accumulation-buffer flush path
(`llm_engine.py:1094-1108`), which the guards do not inspect.

This is a pre-existing CTK hazard (CTK runs the identical
`play_prefetched_agenda` speaker-thread pattern in production) that Fase 1's
API-host wiring triggers more often, because the API host now exercises the
prefetch-consume path on every agenda boundary instead of never. Judge A
(adversarial review, 2026-07-21) confirmed: impact is short audio overlap
only, no data loss or deadlock; requires interactive input to arrive after
`speaking_start` and the driver to win the boundary race.

A real fix requires either a speaking mutex or routing the prefetched draft
through the same dispatch path as every other turn — deliberately deferred to
the Fase 2 speech-serialization design (ADR-036), which removes the parallel
speaker thread entirely rather than guarding it.

A second, narrower gap: the `switch_llm_tier`/`set_profile` clears (§2.5) run
on the engine thread without `agenda_lock`. They serialize with
`play_prefetched_agenda` on `_prefetch_lock`, but ordering between "switch
lands" and "consume reads `wait_prefetched_agenda(0)`" is not guaranteed, so a
switch landing in the microsecond window between those two events can still
speak one old-persona/old-tier line. This self-corrects on the next turn; Judge
A flagged it and it is accepted as best-effort, not closed, in the design
(`design.md` §10).

---

## 5. What was not changed

- **Controller** (`kira_agenda_controller.py`): no changes. `prefetch_action_after_current_speech`,
  `start_prefetched_action`, `mark_generation_accepted`/`mark_speech_complete`
  already supported this flow.
- **Guardrails**: not touched. Preview validation was already wired headless
  (`engine_host.py:422`); Fase 1 added no new validation code.
- **`play_prefetched_agenda`**: not touched — kept as-is, including the
  speaker-thread pattern flagged as a residual risk above.

---

## 6. Consequences

- Agenda dead air in the API host drops from 16-19s (worst case, full
  generation exposed) to sub-second at 4/4 measured boundaries — matching
  CTK's overlap behavior.
- No new failure mode is worse than pre-fix behavior: every guard-chain
  failure in §2.2 falls back to the plain `next_action()` nudge, i.e.
  today's cost, for that one turn only.
- The accepted concurrency residual (§4) is now exercised on every boundary
  instead of rarely, raising its practical (if still low) probability — the
  reason it is named explicitly here rather than left implicit.
- Fase 2 (ADR-036) is scoped specifically to close this residual by deleting
  the parallel speaker thread, not by adding a lock around it.
