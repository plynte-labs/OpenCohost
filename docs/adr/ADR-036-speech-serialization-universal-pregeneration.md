# ADR-036: Speech Serialization via Universal Pregeneration (API Host, Fase 2)

**Status**: Designed — v2 (v1 verdict NEEDS-REWORK; all blocker/major findings amended in v2, see design-fase2.md §7; implementation not yet applied)
**Date**: 2026-07-21
**Track**: [`conductor/tracks/agenda_no_dead_air_20260719/`](../../conductor/tracks/agenda_no_dead_air_20260719/) (`design-fase2.md`, v2)
**Builds on**: ADR-035 (Fase 1, implemented and runtime-validated: 4/4 consume boundaries at
0.34-0.43s vs 36s fallback)
**Related**: ADR-003 (agenda state machine — TOPIC_CLOSING precedent), ADR-027 (the
design → adversarial-review → apply gate this track followed)

---

## 1. Context

ADR-035 (Fase 1) closed agenda-to-agenda dead air by prefetching the next turn
on a background thread and speaking it from a separate speaker thread
(`play_prefetched_agenda`, `llm_engine.py:906-926`). That thread runs
independently of the engine worker's own dispatch loop
(`_process_priority_queue`), and ADR-035 §4 named the resulting hazard
explicitly: at the exact turn boundary, both the prefetch speaker thread and
the worker's own pop-and-speak path can reach `_hablar` (`llm_engine.py:3120`,
no re-entrancy guard) inside a microsecond window where the driver's consume
guards read "clear" on both sides.

Two more problems sit next to that one, surfaced by the owner's post-Fase-1
runtime session (log `20260721_131040`) and by a Sonnet system-critique pass:

- **Interactive dead air is worse than agenda dead air.** A typed question
  arriving mid-agenda-speech does not generate in the background at all — the
  worker starts generating only at speech end (log: 31.7s floor, up to ~90s
  observed), because the interactive path has no pregeneration trigger. This
  is confirmed identical in CTK: CTK does not background-generate interactive
  turns either; prefetch was always agenda-only by a hard source gate.
- **The mutex problem and the pregeneration problem are the same shape.**
  Fixing "two threads can call `_hablar`" and fixing "the typed question
  should generate while TTS plays" both reduce to the same question: what
  gets spoken next, and does it need to generate first. Building them as two
  separate mechanisms (a lock plus a second cache) would duplicate the
  invalidation, staleness, and priority logic Fase 1 already built once for
  agenda.

The core insight this design starts from: these are **one mechanism**, not
two.

---

## 2. Decision

Replace the agenda-only `_prefetched_agenda` cache and its parallel speaker
thread with a single **pregenerated-turn cache consulted at queue pop**,
inside the API host's engine worker. Every spoken turn — agenda or
interactive — flows through the one priority queue and the one engine worker.
When the worker pops an item whose `(payload, source)` matches a valid cache
entry, it skips `_generar_dialogo` and goes straight to deferred-commit +
emit + `_hablar`. The cache is filled in the background while TTS plays, by
either the agenda driver (Fase 1's trigger, re-routed) or a new interactive
trigger.

By construction, this makes two threads calling `_hablar` **impossible**, not
merely improbable: there is exactly one call site for a spoken turn — the
worker's pop-time dispatch — after the parallel speaker thread is deleted.
`#338` priority ordering also becomes structural rather than duplicated: one
queue, one order, so the separate `has_pending_priority_before` consume guard
that Fase 1 needed on the driver side disappears with the parallel path it
was guarding against.

### 2.1 The cache

`_prefetched_agenda` (dict, agenda-only) becomes `_pregen` (dict, source-agnostic):
same `_prefetch_lock`, same `_prefetch_epoch` bump-on-clear invalidation,
wider key — `{payload, source, dialogo, priority}`. One slot.

*(ponytail: multi-slot only if a real need appears — under the GPU-free rule
in §2.3, at most one background generation runs at a time anyway.)*

API generalizes the Fase 1 primitives:

- `pregenerate(payload, priority, source) -> bool` — `prefetch_agenda`
  generalized: same worker-thread + `commit_history=False` + epoch check; the
  `source.startswith("kira-agenda")` gate is **removed**. Agenda sources still
  run `_preview_accept_agenda_output` (unchanged guardrail); non-agenda
  sources run no preview gate, matching their existing foreground path, which
  has none either.
- `pregen_pending()` / `clear_pregen()` — same semantics as `prefetch_pending`
  / `clear_prefetched_agenda`. Every existing invalidation call site (profile
  switch, tier switch, `drop_pending_sources`, same-source `replace_pending`)
  keeps working unmodified.
- `play_prefetched_agenda` is **not deleted**. CTK production still consumes
  through it (`agenda_audio_controller.py:385`, wired from the Tk
  after-loop). It becomes the CTK legacy consume path, untouched — the API
  host simply stops calling it. This is stated honestly as a scope boundary,
  not a closed gap: CTK keeps its historical two-mics race after this design
  ships. The §2.5 belt lock serializes it there as defense-in-depth (in CTK,
  lock contention there is the lock *working*, logged at info; in API-host
  tests, any contention on that lock is treated as a bypass regression and
  fails the suite).

### 2.2 Pop-time cache hit

`_process_priority_queue`, after pop (`llm_engine.py:1110-1125`):

```python
item = pop()
priority, ts, payload, source, *rest = item
cached = self._take_pregen_if_match(payload, source)   # under _prefetch_lock
if cached:
    self._speak_pregenerated(cached)     # deferred commit + emit + _hablar — worker thread
else:
    self._ejecutar_inferencia(payload, source=source, history_text=...)
```

`_speak_pregenerated` is `play_prefetched_agenda`'s speaker body
(`_commit_history` → `_record_accepted_agenda_output` if agenda source → log
→ `_emit_dialogue` → `_hablar`) moved onto the worker thread, minus the
thread — the same sequence, one fewer call site.

### 2.3 Fill triggers

**Agenda** (Fase 1 flow, re-routed): the `speaking_start` spawn trigger is
unchanged (`maybe_start_prefetch` → `pregenerate(...)`). What changes is
consume: the driver no longer calls `play_prefetched_agenda`. On a ready
draft, `_maybe_consume_prefetch` calls `start_prefetched_action` (controller
adoption, unchanged) then `enqueue_agenda_action` (normal `replace_pending`)
— the worker pops it like any other item and hits the cache.
`enqueue_agenda_action`'s existing draft-clear must not clear the cache for
the same `(payload, source)` it is about to enqueue — only on mismatch
(supersede).

**Interactive (new):** triggered inside `enqueue()` itself, on the caller's
thread (never the busy worker): after appending, if the GPU-free predicate
holds (§2.3.1) and no equal-or-higher-priority pregen is running or cached,
`pregenerate(head.payload, head.priority, head.source)` runs for the queue
**head only**. Excluded for `source="accumulated"` (the payload doesn't exist
until flush) and for enqueues that are actually a `replace_pending` of an
already-cached source (supersede handles those).

#### 2.3.1 GPU-free rule — v1's predicate was unsatisfiable

v1 of this design gated the interactive trigger on `_speaking and not
_processing`. Adversarial review caught that `_processing` brackets the
*whole* turn — generation **and** TTS playback (set at
`_process_priority_queue:1118`, cleared in `_complete_processing_cycle` only
after `_hablar` returns) — so `_speaking ⟹ _processing` always, and the
predicate was always false. No "LLM idle while still speaking" flag existed
anywhere in the codebase to build on instead.

**v2** introduces a narrow new flag, `_llm_generating`, set/cleared in a
try/finally bracket around the actual Ollama call inside `_generar_dialogo` —
both the foreground path and pregeneration set it. The predicate becomes:
`self._speaking and not self._llm_generating and no pregen worker alive`.
`_processing`'s existing semantics are left untouched, since other code
depends on them; this is declared as new, small engine code (a two-line
bracket plus a property), not existing wiring being reused.

### 2.4 Slot priority eviction

Adversarial review's other MAJOR finding on v1: the single pregen slot, as
originally specified, structurally starved interactive pregeneration whenever
an agenda draft already occupied it (agenda fills the slot on every turn;
interactive would never get a turn).

**v2**: the slot becomes priority-aware. `pregenerate(p, prio, src)` no
longer refuses outright when busy — if the current occupant (cached or
in-flight) has a *lower* priority (higher number) than the incoming request,
it is epoch-invalidated and the new request takes the slot; an
equal-or-higher-priority occupant wins and the new request is simply dropped
(that item pays foreground cost later, same as today). Rationale: a queued
PTT/chat item is already ahead of the next agenda turn in the queue, so once
it plays, history has moved and the agenda draft would be stale regardless —
evicting it costs nothing extra and enforces `#338` structurally rather than
by convention. The evicted agenda boundary falls back to plain generation
(today's Fase-1-absent behavior), recorded by the §3 telemetry as
`draft=evicted`.

### 2.5 Consume-at-event + optional belt lock

**Consume-at-event** closes the boundary race with the accumulation-buffer
flush directly (rather than relying on the guard-chain timing Fase 1 used):
consumption for agenda drafts moves from the driver's own tick thread to
`engine_host._handle_agenda_motor_event`'s `speaking_end` handling, which
already runs on the worker thread (inside `_hablar`'s tail) under
`agenda_lock`. A new `on_agenda_speaking_end` hook calls
`driver.maybe_consume_prefetch(agenda, motor)` synchronously there. By the
time the worker reaches `_process_priority_queue` again, the item is already
enqueued — no race window remains, and the boundary stays at Fase 1's ~0.4s.
The driver-tick consume path stays as the fallback for the late-draft 0.25s
re-tick, unchanged.

**Belt (optional, WU2b):** a `threading.Lock` acquired at `_hablar` entry,
released in `finally`. Zero-cost defense-in-depth for any future caller that
bypasses the queue; both historical call sites collapse to one (the worker)
after WU2, so the lock should never contend in the API host — contention
there is asserted/logged as a bypass regression, not tolerated as normal.

---

## 3. Alternatives considered

**`_hablar`-entry lock only (no cache unification).** Would have closed the
concurrency hazard from ADR-035 §4 with a much smaller diff — a single
`threading.Lock`. Rejected as the *sole* fix because it does nothing for the
interactive dead-air problem (§1), which the owner confirmed is the larger
pain point (31.7s-90s of observed silence versus Fase 1's already-solved
agenda case), and because it leaves two independent call paths to `_hablar`
racing for the lock instead of removing the race. Kept as the belt-lock
addition in §2.5, not as the primary mechanism.

**Dedicated speaker thread, generalized (build a second thread-based
consumer for interactive turns too).** Would have reused Fase 1's
`play_prefetched_agenda` pattern for interactive replies as well. Rejected:
this doubles the exact hazard ADR-035 §4 flagged instead of closing it — two
independent threads (agenda speaker, interactive speaker) both calling
`_hablar`, now racing each other *and* the worker. The single-dispatch-path
design (§2.2) was chosen specifically because it makes the race impossible by
construction rather than merely rarer.

**Two separate mechanisms (agenda prefetch as-is, plus a new independent
interactive pregen cache).** The most literal reading of "add interactive
pregeneration" as its own feature. Rejected because it duplicates
invalidation, epoch-staleness, and priority-yield logic that Fase 1 already
built once for agenda, doubling the surface area for the same class of bug
(stale draft spoken, priority inversion) without doubling the payoff — one
unified cache with a wider key (`source`-agnostic) covers both cases with one
staleness model instead of two.

---

## 4. Adversarial review record (process note)

This design went through the house design → adversarial-review → amend gate
(the same method documented as a general practice in ADR-027) before any
implementation started. v1 was reviewed by a Sonnet design-review pass and
returned a **NEEDS-REWORK** verdict — two blockers and two majors, all caught
on paper, none in a diff:

| Finding | Severity | Disposition in v2 |
|---|---|---|
| `play_prefetched_agenda` is CTK production; deleting it breaks CTK | BLOCKER | Kept as CTK's legacy consume path (§2.1); API-host-scoped invariant instead of a global one |
| GPU-free predicate `_speaking and not _processing` is unsatisfiable (`_speaking ⟹ _processing` always) | BLOCKER | New narrow `_llm_generating` flag around the Ollama call only (§2.3.1) |
| Single pregen slot structurally starves interactive pregen behind agenda | MAJOR | Priority-aware slot eviction, stale-anyway rationale (§2.4) |
| AC3.3/AC3.5 hooks (`_commit_history` epoch bump, head-tracking in `enqueue`) assumed to exist but don't | MAJOR | Declared as new engine code in WU3's diff, not assumed wiring |
| 4b guardrail-visibility event schema didn't match the real `EventOut` model | MINOR | Corrected to the real schema (`action="guardrail:<code>"`, `detail=None`) |
| Flat 2s wait-tax on cache-miss-but-pending | MINOR | Skip the wait entirely when the estimated remaining pregen time already exceeds the bound |

The reviewer confirmed as sound and unchanged from v1: lock ordering (no
inversion), the §2.2 payload-identity match logic, and the feasibility of the
WU1 red-race-test approach.

Two blockers means two implementation dead-ends were avoided before a single
line of code was written: shipping v1 as designed would have broken CTK
production on first contact and shipped an interactive-pregeneration trigger
that could structurally never fire. Both were catchable only by reading the
design against the real call sites (`agenda_audio_controller.py:385`,
`_processing`'s actual set/clear locations) — exactly the pattern ADR-027
describes as the reason this gate exists on core changes.

---

## 5. Work units and sequencing

Strict TDD, RED before fix, ordered:

1. **WU1** — RED race test: real `MotorVocalIA`, fake Ollama, no-op TTS,
   injectable hook at the pop → `_processing=True` boundary
   (`llm_engine.py:1110`/`:1118`) to pin the interleaving deterministically
   (no sleeps). Must fail or flake on Fase 1 code.
2. **WU2 (+2b)** — pop-time cache + delete the speaker thread (§2.1, §2.2,
   §2.5); driver re-route; `play_prefetched_agenda` deleted from the API
   host's call graph (kept for CTK). Belt lock added.
3. **WU3** — interactive pregeneration: trigger (§2.3), `_llm_generating`
   flag, `_commit_history` epoch hook, head-tracking in `enqueue()`. Rated
   effort **L** — the v2 amendments alone added three declared new engine
   seams.
4. **WU4** — solidification: boundary telemetry, guardrail-rejection
   visibility in the frontend (`EventOut(source="kira-agenda",
   action="guardrail:<code>", detail=None)` — codes only, dialogue text never
   crosses this boundary), retry-once on rejection with a remaining-speech
   check, stash-pairing structural guard. 4a/4b/4c can land parallel to WU3
   (different files).
5. **WU5** — interruption + connector phrase. **Gated** on three open owner
   decisions (§6). Not started.

---

## 6. Open decisions (gating WU5)

1. **Cut policy** — does any typed/PTT input cut agenda speech, or PTT only?
2. **Return policy** — always return to the interrupted topic after
   answering, or evaluate whether to (e.g. skip return if the interruption
   changed subject, or the topic was near completion)?
3. **Connector source** — a fixed phrase pool per locale, or a cheap
   one-line LLM generation (adds a generation call)?

The seams for WU5 are named but not built: `interrupt_speaking()`
(`llm_engine.py:468`) remains the single cut primitive; an interrupted draft
would be stashed (epoch-frozen) rather than cleared, which under the unified
cache model is just "leave the cache entry, requeue the interactive item at
its own priority."

---

## 7. Restrictions (hard invariants, all work units)

- Single Ollama worker: never two generations running at once (GPU-free rule
  + WU3 AC3.2).
- `agenda_lock` → motor-locks order; neither is ever held across generation
  or playback.
- After WU2, the engine worker is the only `_hablar` caller **in the API
  host** — this by-construction guarantee is explicitly API-host-scoped.
  CTK's legacy speaker thread remains and is only serialized by the belt
  lock, not eliminated, until CTK itself is ported or retired.
- `commit_history` exactly once per spoken turn, at playback time, in spoken
  order.
- Raw chat content is never persisted or logged — WU4's guardrail-visibility
  events carry rejection codes only, never dialogue text.
- Every failure path (cache miss, stale, late) falls back to today's plain
  generation; telemetry records which branch fired.
- No `kira_agenda_controller.py` changes in WU1-4. WU5 may need `ptt_text`
  plumbing into production `next_action` calls (test-only today) — reviewed
  separately when WU5 is scoped.

---

## 8. Consequences

- Deletes rather than guards the ADR-035 §4 residual: two-mics-at-once
  becomes structurally impossible in the API host instead of merely
  low-probability.
- Interactive typed/PTT replies gain the same background-overlap treatment
  agenda turns got in Fase 1 — expected to cut the observed 31.7s-90s
  interactive silence toward the same sub-second range, pending WU3
  validation.
- CTK is explicitly left with its pre-existing race, unresolved by this
  design — a known, stated scope boundary, not an oversight. Porting or
  retiring CTK's speaker-thread path is out of scope here.
- Effort is heavier than Fase 1: WU1 M, WU2 M, WU3 **L** (three new declared
  engine seams), WU4 S+S/M+S/M+S, WU5 L (post owner-decision). Sequence:
  WU1 → WU2(+2b) → WU3 → WU4 (parallel-safe with WU3) → WU5.
- The two-blocker, two-major review outcome on v1 is itself evidence for
  keeping the design → adversarial-review → apply gate (ADR-027) mandatory
  on this class of core change, rather than treating it as ceremony to skip
  under time pressure.
