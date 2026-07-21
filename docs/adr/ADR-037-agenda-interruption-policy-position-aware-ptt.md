# ADR-037: Agenda Interruption Policy — Position-Aware PTT Cuts (API Host, Fase 2 WU5)

**Status**: Accepted (policy decided; implementation pending — WU5 of ADR-036's plan)
**Date**: 2026-07-21
**Track**: [`conductor/tracks/agenda_no_dead_air_20260719/`](../../conductor/tracks/agenda_no_dead_air_20260719/) (`design-fase2.md` §WU5 v3, `tracks.md`)
**Builds on**: ADR-036 (Fase 2 design — pregenerated-turn cache, priority queue as the
single ordering authority; WU5 was gated on the three product decisions this ADR
resolves)
**Related**: ADR-035 (Fase 1, prefetch overlap wiring — the origin of the pregeneration
machinery D1/D3 reuse)

---

## 1. Context

ADR-036 designed WU1-WU4 of Fase 2 (pop-time pregenerated-turn cache, single
`_hablar` dispatch path, interactive pregeneration) and scoped WU5 —
interruption of an agenda monologue plus the "connector" phrase that resumes
it — as **gated**, pending three open product questions (ADR-036 §6):

1. **Cut policy** — does any typed/PTT input cut agenda speech, or PTT only?
2. **Return policy** — always return to the interrupted topic, or evaluate
   whether to?
3. **Connector source** — fixed phrase pool, or a cheap one-line LLM
   generation?

ADR-036 left WU5's seams named but unbuilt: `interrupt_speaking()`
(`llm_engine.py:468`) as the single cut primitive, and the stash mechanic
under the unified cache model as "leave the cache entry, requeue the
interactive item at its own priority" (ADR-036 §6) — refined in
`design-fase2.md` WU5 [v3] to "epoch-freeze the cache entry, requeue the
agenda item after the interactive item(s)" — ordinary queue operations, not
new machinery.

The owner resolved all three questions on 2026-07-21 (`conductor/tracks.md`,
"Owner decisions 2026-07-21 (WU5 UNBLOCKED)"), and `design-fase2.md`'s WU5
section was rewritten to v3 to record the resolved spec plus AC5.1-5.6. This
ADR is that policy record — WU5 itself has not been implemented yet.

---

## 2. Decision

### 2.1 D1 — Cut policy: PTT-only, position-aware

Typed chat **never** cuts agenda speech. Under WU3's interactive
pregeneration, a typed question's answer generates during the remaining
speech and plays at the turn boundary — deferral stopped costing dead air, so
there is no reason to interrupt for it.

PTT (push-to-talk) during agenda speech is evaluated against speech progress
(`played fragments / total fragments`, the same TTS-pipeline seam as WU4's
`speech_remaining_estimate()`), split into three zones with no LLM in the
loop — a deterministic rule every time:

- **Early zone (< ~25% progress)**: do not cut. Defer; the PTT answer holds
  priority 0 and pregenerates meanwhile, so it is the first thing spoken at
  the boundary.
- **Mid zone (~25-75%)**: the motor decides by margin —
  `remaining_speech_estimate > CUT_THRESHOLD_SECONDS` (default 20s) cuts via
  `interrupt_speaking()`; otherwise defers, since the turn ends soon anyway.
- **Late zone (> ~75%)**: do not cut — the turn is about to end regardless;
  answer at the boundary.

Zone boundaries and the threshold are `settings.py` constants, owner-tunable.
A deferred PTT item never loses its priority-0 slot.

### 2.2 D2 — Return policy: return-by-default with narrow deterministic skips

After an interruption response plays, Kira returns to the stashed agenda turn
(connector + pregenerated draft) **unless** a deterministic skip condition
holds — again, no LLM evaluation:

- the interruption changed or closed the topic (stashed `topic_id` no longer
  active, status `CLOSING`, or session stopped/emergency-stopped);
- the stashed draft was epoch-invalidated (profile or model switch, explicit
  clear);
- the interactive exchange chained beyond `RETURN_MAX_DETOUR_TURNS` (default
  2) interactive turns — past that point a real conversation started, and
  forcing a return would be robotic.

On skip, normal `next_action` flow decides what plays next. The stash
mechanic reuses ADR-036's unified cache exactly as scoped: epoch-freeze the
cache entry, requeue the agenda item after the interactive item(s) — no new
storage, just queue ordering.

### 2.3 D3 — Connector: parameterized pool floor + opportunistic generated upgrade

Two layers, structured so the upgrade can never add latency:

- **Floor (always available, zero cost)**: a per-locale pool of ~8 connector
  templates parameterized with the live topic title (e.g. "volviendo a
  {tema}…"), rotated without immediate repetition. Requires a new i18n
  manifest slot — `design-fase2.md` confirms no such slot exists today.
- **Upgrade (opportunistic)**: while the interruption answer's TTS plays
  (GPU free), a one-line contextual connector referencing the interruption
  generates through the same WU3 pregeneration slot, at **lowest** priority
  so it never evicts a real agenda or interactive pregen. At return time, a
  non-blocking `timeout=0` check decides: ready and guardrail-clean → use it;
  late, rejected, or evicted → the pool floor plays instead.

Connector text prepends to the stashed dialogo at pop time
(`_speak_pregenerated`).

---

## 3. The unifying pattern

All three decisions reduce to the same shape: **a deterministic floor that is
always available, plus an opportunistic quality upgrade that cannot add
latency by construction.** D1's margin rule is a deterministic floor decision
(cut or defer, no waiting on a model). D2's default-return-with-skips is a
deterministic floor outcome with narrow, cheaply-checked exceptions. D3 is
the pattern applied literally — a zero-cost pool floor plus a pregenerated
upgrade that only wins if it is already done by the time it is needed. None
of the three ever puts a generation call, or any nondeterministic wait, on
the interruption's critical path.

This is possible because one piece of Fase 2 machinery serves triple duty:
the single pregeneration slot (ADR-036 §2.1) now holds, at different moments,
the agenda draft, the interactive reply, or the connector upgrade — with the
same priority-eviction rule (ADR-036 §2.4) arbitrating which one wins when
more than one wants the slot at once. WU5 adds no new cache, no new
invalidation model, and no new priority scheme; it is ordinary queue
operations and one more consumer of an existing slot.

---

## 4. Alternatives considered

**Any-input cuts (typed chat can interrupt too).** Rejected: typed input is
comparatively high-volume, low-urgency chat noise next to a deliberate PTT
press — allowing it to cut would make agenda monologues constantly
interruptible, defeating the point of an autonomous agenda mode. WU3
pregeneration already removes the cost of deferring typed input, so there is
no latency argument left for cutting on it.

**LLM-evaluated return decision.** Rejected: evaluating "should Kira return
to the topic" with a model call adds both a generation and nondeterminism to
a path that Fase 1 and Fase 2 spent their whole design keeping
latency-critical and deterministic. A handful of cheap, checkable conditions
(topic state, epoch, detour count) covers the cases that matter without a
model in the loop.

**Mini-generation-only connector (no pool floor).** Rejected: a connector
generated only on demand adds 2-4s to every return, reintroducing exactly the
kind of boundary latency Fase 1/Fase 2 eliminated elsewhere. The pool floor
guarantees a zero-cost connector always exists; the generated upgrade is
strictly additive quality, never a dependency.

---

## 5. Acceptance criteria (WU5, `design-fase2.md`)

- **AC5.1** Typed input never calls `interrupt_speaking()` (regression test).
- **AC5.2** PTT in early/late zones defers (no cut) and its answer plays
  first at the boundary; PTT in the mid zone cuts iff the remaining estimate
  exceeds the threshold (tested on both sides of the margin with fake TTS
  progress).
- **AC5.3** After a cut plus response, the stashed turn resumes with a
  connector exactly once, and history commits in spoken order (interruption
  answer before the resumed turn).
- **AC5.4** Each skip condition (topic gone/CLOSING/stopped, epoch
  invalidated, detour beyond max) suppresses the return and falls through to
  `next_action` — one test per condition.
- **AC5.5** The pool floor always plays when the upgrade is late or
  rejected; the upgrade never delays the return (`timeout=0` check); no
  immediate pool repetition.
- **AC5.6** The connector upgrade generation never evicts a pending
  interactive or agenda pregen (lowest slot priority).

---

## 6. Consequences

- WU5 is unblocked. Implementation follows ADR-036's sequence (WU1 →
  WU2(+2b) → WU3 → WU4 → WU5), i.e. after the pregenerated-turn cache and
  single-dispatch-path work already scoped in ADR-036 lands.
- Two new owner-tunable `settings.py` constants are implied by the policy:
  `CUT_THRESHOLD_SECONDS` (default 20s) and `RETURN_MAX_DETOUR_TURNS`
  (default 2). Neither exists yet — WU5 introduces them.
- A new i18n manifest slot is needed for the per-locale connector template
  pool (`{tema}`-parameterized); no equivalent slot exists today.
- No LLM sits on any latency-critical decision in the interruption path (cut,
  return, connector fallback); the only generation involved (the connector
  upgrade) is structurally incapable of adding latency.
- `kira_agenda_controller.py` may still need `ptt_text` plumbing into
  production `next_action` calls (currently test-only) — ADR-036 §7 already
  flagged this as its own review at WU5 time, unchanged by this ADR.
- No code changes ship with this ADR; it records policy only, per the WU5
  gate ADR-036 left open.
