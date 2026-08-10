# ADR-038: Agenda Interruption Implemented — Bounded Frozen-Stash Return with Connector Floor

**Status**: Accepted
**Superseded in part**: §2.1 (position-cut zone implementation) retired by ADR-044 (2026-08-10); the frozen-stash return (D2) and connector floor (D3) remain in force
**Date**: 2026-07-22
**Track**: [`conductor/tracks/agenda_no_dead_air_20260719/`](../../conductor/tracks/agenda_no_dead_air_20260719/) (`design-fase2.md` §WU5 [v3], `tracks.md`)
**Builds on**: ADR-037 (policy decision: D1 position-aware PTT-only cut, D2
return-by-default, D3 connector floor + opportunistic upgrade); ADR-036
(pregenerated-turn cache and single-dispatch-path machinery WU5 reuses)
**Related**: ADR-035 (Fase 1 prefetch overlap — origin of the pregeneration
machinery), ADR-027 (design → adversarial-review → apply gate this WU followed)

---

## 1. Context

ADR-037 resolved the three product questions gating WU5 (cut policy, return
policy, connector source) but shipped no code — it is a policy record. This
ADR documents the WU5 **implementation** (commit `a25d780`) of that policy:
the cut seam, the frozen stash, the connector floor/upgrade, and the driver's
return path, plus what two rounds of blind dual judging (Fable + Opus)
changed between the first implementation pass and what actually shipped.

---

## 2. Decision

Implement ADR-037's D1/D2/D3 exactly as scoped, reusing ADR-036's pregen slot
and priority-eviction rule as the substrate — no new cache, no new priority
scheme.

### 2.1 Cut seam — `ptt_interrupt_if_agenda_speaking()`

`opencohost/core/llm_engine.py:618` (`MotorVocalIA.ptt_interrupt_if_agenda_speaking`,
~:611 in the surrounding zone-classification block). PTT-only: gated by a new
host flag, `_ptt_position_cut_enabled`, `False` by default and set `True`
only in `EngineHost` (`opencohost/api/engine_host.py:431`) — CTK never sets
it, so the seam is inert there. Fired synchronously from
`ptt_session.PttController.on_flush_precheck` on the WS flush thread, before
the turn is dispatched — the queued command path can't reach it, since the
engine thread is already inside `_hablar` by the time a command pops. The
precheck call is fail-open (`main.py`'s `_dispatch`: any exception from the
precheck is caught and logged, never blocks the turn). Wired in
`opencohost/api/main.py:815-819` via a guarded `getattr` on the bound method
(`getattr(getattr(host, "motor", None), "ptt_interrupt_if_agenda_speaking",
None)`), so a host without a motor, or a motor without the method, degrades
to no precheck rather than raising at wiring time.

Zone classification reads `_speech_progress` (fragments played / total,
maintained live by `_hablar_impl`'s consumer loop):

- **Early** (`< CUT_ZONE_EARLY`, 0.25): defer.
- **Late** (`> CUT_ZONE_LATE`, 0.75): defer.
- **Mid** (0.25-0.75): cuts only if both hold — `speech_remaining_estimate()`
  exceeds `CUT_THRESHOLD_SECONDS` (20.0s) **and** `_freeze_agenda_stash()`
  actually froze a draft. A freeze failure (no draft to freeze) defers
  instead of cutting into dead air — this was a round-1 judge critical
  against the first implementation pass, which cut unconditionally on the
  margin check alone.

### 2.2 Frozen stash

A separate one-entry `_frozen_stash` dict, distinct from the WU2/WU3 pregen
slot — freezing moves the next-turn agenda draft **out of** the prefetch slot
rather than copying it, so the freed slot is immediately available to the
interruption answer's own pregeneration. Carries a monotonic freeze
timestamp (`_frozen_stash_at`, `time.monotonic()`) used by the hold-timeout
gate (§2.4). `restore_frozen_stash()` bumps `_prefetch_epoch` inside the same
locked block that reads and clears the stash, so any pregen worker still
holding a stale epoch check sees the invalidation atomically. `detour_started()`
(`_detour_turns > 0`) is the seam the driver polls to know the interruption
answer's own turn has begun.

### 2.3 Connector — floor and upgrade

**Floor**: `opencohost/i18n/active.py:connector_templates()` reads a new
manifest slot, `llm.connector_templates` — 8 es-AR voseo templates
(`opencohost/locales/es/manifest.yaml`) and 8 EN equivalents
(`opencohost/locales/en/manifest.yaml`), each parameterized with `{tema}`.
`LEGACY_CONNECTOR_TEMPLATES` in `active.py` is the fallback tuple on any
manifest failure. The missing EN slot was a round-1 judge critical — the
first pass shipped only the es-AR pool, which would have silently regressed
English sessions to a raw stash resume with no connector.

**Upgrade**: an opportunistic one-line contextual connector, generated while
the interruption answer's own TTS plays. Spawn-gated on
`speech_remaining_estimate() > CONNECTOR_UPGRADE_MIN_REMAINING_SECONDS`
(12.0s) so the worker never starts when there isn't enough playback runway to
absorb it. Generation is bounded by `CONNECTOR_UPGRADE_TIMEOUT_SECONDS`
(10.0s) via a watchdog-timeout override on `_generar_dialogo` that suppresses
the engine's normal stall-recovery path for this call — a stalling model
abandons the upgrade instead of retrying into the real turn's time budget.
The worker claims `_llm_generating` atomically under the engine lock, with an
ownership-token release: only the code path that actually invoked
`_generar_dialogo` for this claim clears the flag in its `finally`. Both
judges independently found a double-release in the first pass (the connector
worker's own `finally` and `_generar_dialogo`'s internal `finally` could both
clear the same flag, letting a second, unrelated generation start believing
the engine was free while the first was still finishing) — round-2 critical,
fixed by the ownership check. The upgrade's write back into the stash is
identity-checked (`self._frozen_stash is stash`) so a stash that was
discarded or replaced while the upgrade was generating can't have its
connector field written into a stash object no longer in use.

NOTE: in production, `speech_remaining_estimate()` is typically `None` at
the interruption answer's own `speaking_start` (progress tracking hasn't
initialized yet at that instant), so the upgrade worker's spawn gate rarely
fires and the floor plays in practice. This is accepted as-is: the template
floor is a complete, correctly-parameterized connector on its own, and the
upgrade is strictly additive quality per ADR-037 §3, never a dependency.

### 2.4 Driver return — `_maybe_return_frozen_stash`

`opencohost/api/agenda_driver.py:446`. Return-by-default at clean tick
boundaries, gated by deterministic checks only:

- **Deterministic skips**: topic gone, `TOPIC_CLOSING`, agenda inert, or
  detour count beyond `RETURN_MAX_DETOUR_TURNS` (2).
- **Yields to `PAUSED_NEEDS_OPERATOR`**: the return check does not fire while
  the agenda is paused for operator auto-recovery, so the existing
  auto-resume path (mirrored from `app_shell.py:1544-1563`) stays reachable
  instead of the frozen-stash return racing or blocking it — a round-2
  critical: the first pass's return check ran unconditionally and could
  starve auto-resume.
- **HOLD until `detour_started()`**: the return can never fire before the
  interruption answer's own turn has begun. This was the round-1 **blocker**:
  the PTT answer is dispatched through the command queue, which is invisible
  to the driver's tick-based return gates, so the first implementation could
  return to the stashed topic before the interruption answer had even
  played.
- **Bounded by `FROZEN_STASH_MAX_HOLD_SECONDS`** (90.0s) from the freeze
  timestamp: on expiry the stash is discarded with code-only telemetry
  (`Frozen stash: hold_timeout`, no dialogue text). Round-2 critical: three
  independent paths could lose the interruption answer without ever
  incrementing the detour counter — the queue's `is_ready` drop, a TTL sweep
  of the direct requeue, and a dispatch exception raised after the cut had
  already frozen the stash. Without the 90s backstop, any of the three would
  hold the agenda silent forever.
- **Adopt-before-restore ordering** with a fresh-turn fallback: if the
  restore is invalidated mid-flight (profile/tier switch, explicit clear),
  the driver logs `Frozen stash: restore_lost_fallback` and falls through to
  a normal fresh turn rather than adopting a stale draft.

---

## 3. Judging record

Two full rounds of blind dual judging (Fable + Opus), independent read, no
cross-visibility between judges:

- **Round 1**: blocker — return-before-answer (§2.4 HOLD gate did not exist
  yet). Criticals — freeze-failure cut (§2.1), concurrent connector
  generation / double-release (§2.3), missing EN connector templates (§2.3).
- **Round 2** (after round-1 fixes): criticals — unbounded hold (§2.4 backstop
  did not exist yet), auto-resume starvation (§2.4 `PAUSED_NEEDS_OPERATOR`
  yield), one-directional occupancy (the `_llm_generating` ownership-token
  release, §2.3).

All findings closed and pinned by tests. Gate: race test 3x green, 314
focused + regression tests green.

---

## 4. Accepted residuals

Documented explicitly rather than silently left implicit:

1. **Hold releases on any interactive turn.** `detour_started()` reads
   `_detour_turns > 0`, which increments on any interactive turn, not
   specifically the one answering the PTT interruption. A chat item queued
   before the cut could release the return early. This is an ordering
   glitch only — the interruption answer still speaks (it is not lost, only
   possibly not the very next thing spoken), and no data or dialogue is
   lost.
2. **`_note_detour_turn` omission on two branches.** The is_ready-drop and
   accumulated-flush branches do not call `_note_detour_turn`, so a detour
   started on those paths would not increment the counter that governs
   `RETURN_MAX_DETOUR_TURNS`. Covered by the `FROZEN_STASH_MAX_HOLD_SECONDS`
   deadline backstop (§2.4) rather than fixed structurally — the same
   backstop that closes the round-2 unbounded-hold finding also bounds this
   gap.
3. **`main.py` wire test replicates the getattr idiom.** The test coverage
   for the `on_flush_precheck` wiring re-implements the guarded-`getattr`
   pattern rather than exercising `create_app` end-to-end. Accepted as a
   unit-level check on the idiom; an integration-level check through
   `create_app` is a follow-up, not a blocker.

---

## 5. Consequences

- Typed chat input **never** cuts agenda speech — the cut primitive is
  reachable only from the PTT flush precheck, and the host flag that enables
  it is set only in `EngineHost`.
- Single-Ollama occupancy is preserved end-to-end: the connector upgrade's
  atomic claim + ownership-token release closes the one path (found
  independently by both judges) where two generations could believe they
  each owned the engine.
- The privacy invariant holds: all telemetry in the interruption/return path
  is code-only (`Frozen stash: hold_timeout`, `Frozen stash:
  restore_lost_fallback`) — no dialogue text crosses these log lines.
- Fase 2 of the `agenda_no_dead_air_20260719` track is implementation-complete
  (WU1-WU5, all landed). Owner runtime validation of the interruption path
  (PTT zone cuts, connector return, typed-input non-interruption) is the
  remaining gate before this track closes — see
  `docs/agenda-no-dead-air-phase2-closure.md`.
