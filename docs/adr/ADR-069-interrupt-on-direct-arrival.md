# ADR-069: Interrupt on direct-message arrival (opt-in)

**Date**: 2026-11-21
**Status**: Accepted 2026-11-21 (owner decision). Runtime validation on the Tauri surface is still UNVERIFIED.
**Feature document**: `odd/tasks/voice-controls-refresh-20261120.md` (T8)
**Related**: ADR-044 (interruptible speech, D3-uniform), `direct_turn_preemption_20260803`, `docs/adr/` dated records are not rewritten.

**Language**: English.

---

## Context

A typed (direct) message that arrives while Kira is speaking a non-owner turn (agenda block, stream chat) is non-preemptive today:

- `api/direct_turn.py` accepts it and moves it into the priority queue, but nothing is cut.
- The dispatch pop gate (`_process_priority_queue`) admits only priority 0 (PTT) while speech is busy, so the direct turn waits for the speech boundary.
- The cut happens only when the reply is submitted to the speech router at the owner band (ADR-044 D3-uniform).

PTT behaves differently: `hold_and_pause` cuts at the press. Owners asked for the same immediacy for typed messages, but not everyone wants it, and it trades the pregeneration cover (the reply generating while the agenda still talks) for a faster reaction.

## Decision

Add an opt-in switch, `interrupt_on_direct` (Controls, Voice: "Interrupt when a direct message arrives"), **default off**.

When ON, the router and interrupt flags are armed (`EngineHost` only), and the active speech job is NOT owner speech:

1. `api/direct_turn.py` calls `motor.cut_for_direct_arrival()` after accepting the turn. The decision stays in the motor and router; the HTTP layer is only the arrival point.
2. `SpeechRouter.cut_for_direct_arrival()` requests the cut through the existing pause path, with the same semantics as an owner submit: reason `discard` when `discard_speech_residue` is on and the victim is not agenda, else `preempt` (suspended, resumed later). Protected-job shields are honored.
3. The router sets an **arrival hold** that blocks `_pick()` like a PTT hold. Without it, a suspended victim would resume at once, because nothing else is queued yet.
4. While the hold is set, the pop gate also admits the direct tier (priority 1), so the turn starts generating immediately.
5. The hold is keyed to the direct turn that caused it (its `submitted_at` stamp, including bundle members). It ends when THAT turn's reply is submitted (including `submit_streaming`), when that turn ends without speech, when its queued item expires, when the switch is turned off, on a full stop (`discard_all`), or at a bounded deadline (`ARRIVAL_HOLD_MAX_SECONDS`, 180 s), so a lost release can never mean permanent silence.

6. The cut happens only when the direct turn can start generating at once: Kira is speaking and no other LLM generation is in flight (`_processing` or `_llm_generating`). Otherwise nothing new happens and the reply cuts at submit as before, so the switch never creates a silence longer than it saves. The hook runs before the arrival drain so the turn's own pregen cannot veto the cut.

When OFF, behavior is exactly the previous one; the new code paths are not entered.

Setting, command and persistence mirror `discard_speech_residue`: `set_interrupt_on_direct` (bool), stored in `tts_settings.json`, drain-safe and not speech-deferred so it applies mid-speech, exposed as `interrupt_on_direct` on `GET /api/tts/config`.

## Consequences

- A short silence gap while the direct reply generates (the cost of cutting early).
- Owner speech (direct, PTT, owner bundle) is never cut by a new direct message.
- An in-flight LLM generation is never aborted; only speech is cut and the pop is unblocked.
- The router and interrupt kill switches still dominate: if either is off, the switch is a no-op (logged once at debug).
- A PTT turn or an older direct turn finishing never releases a newer direct turn's hold.
- With the switch OFF, none of the hold code runs (no router lock, no wake) on the pop or turn-end paths.
- Trade-off (L2, documented): a cloud generation slower than the 180 s hold deadline lets the cut speech resume before the reply arrives.
- Logs carry source tags and reason codes only (`[SPEECH_STACK] arrival_cut ...`), never chat content.
- Pinned test `test_nothing_here_ever_interrupts_the_agenda_speech` stays valid because it runs with the switch OFF.
- **Runtime validation must happen on the Tauri surface** (`EngineHost` arms the flags) and has not been done: this is UNVERIFIED until the owner or a validation run exercises it live.
