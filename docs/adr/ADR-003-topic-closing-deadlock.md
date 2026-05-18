# ADR-003: TOPIC_CLOSING State Machine Deadlock

**Status**: Accepted  
**Date**: 2026-05-18  
**Branch**: `audit/cohost-guardrails-causal`  
**Test sessions**: 3 sessions — llama3 8B (2 sessions, 67% rejection rate), Gemma4 e2b (1 session, 0% rejections)  
**Tests**: 72/72 passing, zero regressions

---

## 1. Context

After a causal audit of Kira cohost guardrail rejections (Phase 0), the system was
tested with two different local models:

| Model | Rejection rate | Prefetch success | Sessions |
|-------|---------------|-----------------|----------|
| llama3 8B | 67% | 33% | 2 |
| Gemma4 e2b | 0% | 100% | 1 |

llama3 8B failed primarily due to guardrail rejections (`GUARDRAIL_SIMILAR`,
`GUARDRAIL_LOOPING`). The RecoveryPolicy degradation ladder was bypassed at 2 failures
(line 731 of `kira_agenda_controller.py`), and the app eventually shut down after
3 consecutive failures.

Gemma4 e2b showed the **opposite failure mode**: zero guardrail rejections, all
prefetches accepted and used, but the controller got **permanently stuck in
`TOPIC_CLOSING` state** after exhausting a topic. The queued topic ("mods") was
never selected. 65 seconds of silence followed before manual app shutdown.

This revealed that the state machine was not robust against **either extreme**:
too many rejections (llama3) or too few (Gemma4).

---

## 2. Root Cause Analysis

### 2.1 The deadlock path

When a prefetched `kira-agenda-stop` response is played, the controller enters
`TOPIC_CLOSING` state (controller.py:676). The expected flow is:

```
TOPIC_CLOSING → mark_generation_accepted() → SPEAKING → TTS plays →
mark_speech_complete() → COMPLETED → IDLE → SELECT_TOPIC (next topic)
```

The controller **already supports this** — `mark_generation_accepted` (line 692)
includes `TOPIC_CLOSING` in its accepted states:

```python
# controller.py:684-694
def mark_generation_accepted(self) -> None:
    if self.state in {
        AgendaState.GENERATING,
        AgendaState.REGENERATING_SAFE,
        AgendaState.HANDLE_STREAMER,
        AgendaState.HANDLE_CHAT,
        AgendaState.CONTINUE_TOPIC,
        AgendaState.OPEN_TOPIC,
        AgendaState.TOPIC_CLOSING,  # ← already supported
    }:
        self.state = AgendaState.SPEAKING
```

### 2.2 The break

The break was in `app_shell.py:2217`, `_on_motor_speaking_start`. This method
checks the controller state to decide if the current speech was initiated by the
agenda state machine:

```python
# app_shell.py:2215-2218 (BEFORE FIX)
controller_generated = (
    hasattr(self, "kira_agenda")
    and self.kira_agenda.state in {AgendaState.SPEAKING, AgendaState.GENERATING}
    #                               ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
    #                               TOPIC_CLOSING is MISSING from this set
)
```

When the state was `TOPIC_CLOSING`, `controller_generated` evaluated to `False`,
causing a cascade of failures:

| Step | What should happen | What actually happened |
|------|-------------------|----------------------|
| 1 | `controller_generated = True` | `False` — TOPIC_CLOSING not recognized |
| 2 | `mark_generation_accepted()` → SPEAKING | **Never called** → state stays TOPIC_CLOSING |
| 3 | Generate prefetch for next turn | **Never called** |
| 4 | `mark_speech_complete()` → COMPLETED → IDLE | **Never called** |
| 5 | `next_action()` produces next action | Returns `none()` — TOPIC_CLOSING has no branch |
| 6 | Tick loop proceeds | Spins forever doing nothing |

### 2.3 Why this was never caught

- **Unit tests**: The controller's `mark_generation_accepted` correctly handles
  `TOPIC_CLOSING`. The bug was in the **integration** between `app_shell` and
  the controller.
- **llama3 sessions**: Guardrails rejected turns before the system could reach
  the prefetch-stop path. The bypass at line 731 force-completed topics at 2
  failures, skipping `TOPIC_CLOSING` entirely.
- **Gemma4 session**: With zero rejections, the prefetch-stop path was exercised
  for the first time, exposing the missing state.

---

## 3. Fix

**File**: `ui/app_shell.py`, line 2217  
**Change**: Add `AgendaState.TOPIC_CLOSING` to the recognized states set

```diff
  controller_generated = (
      hasattr(self, "kira_agenda")
-     and self.kira_agenda.state in {AgendaState.SPEAKING, AgendaState.GENERATING}
+     and self.kira_agenda.state in {AgendaState.SPEAKING, AgendaState.GENERATING, AgendaState.TOPIC_CLOSING}
  )
```

### 3.1 Why this single change is sufficient

With `TOPIC_CLOSING` recognized at speech start:

1. `controller_generated = True` → the speech is correctly attributed to the agenda
2. `mark_generation_accepted()` runs → state transitions to `SPEAKING` (controller already supports this)
3. `_kira_agenda_prefetch_while_speaking()` runs → prefetch generated for next transition
4. When TTS ends, `_on_motor_speaking_end` sees `SPEAKING` → `agenda_speech = True`
5. `mark_speech_complete()` runs → detects `status == CLOSING` → `COMPLETED` → `active_topic = None` → `state = IDLE`
6. Next tick in `IDLE` → `_select_next_topic()` → selects the next queued topic

### 3.2 What was NOT changed

- **Controller**: No changes needed. `mark_generation_accepted` already accepts
  `TOPIC_CLOSING`, and `mark_speech_complete` handles the `CLOSING → COMPLETED`
  transition correctly.
- **Guardrails**: Not touched. All guardrail thresholds remain unchanged.
- **RecoveryPolicy**: Not touched. The degradation ladder bypass (line 731)
  remains; it only activates on guardrail failures, not on successful turns.
- **Prefetch mechanism**: Not touched. It works correctly with Gemma4 and is
  correctly cleared when not applicable.

### 3.3 Safety analysis

| Concern | Assessment |
|---------|-----------|
| Could this cause double-counting of turns? | No — `mark_speech_complete` is only called once per speech event |
| Could this incorrectly attribute non-agenda speech? | No — `TOPIC_CLOSING` is only set by the controller for agenda-initiated stop actions |
| Could this activate for chat/PTT speech? | No — chat/PTT use different states (`HANDLE_CHAT`, `HANDLE_STREAMER`) |
| Could `TOPIC_CLOSING` leak into wrong code paths? | No — by the time `_on_motor_speaking_end` runs, the state is already `SPEAKING` |

---

## 4. Related Issues

- **ADR-001**: RecoveryPolicy and 8 state machine bugs fixed in `fix/kira-cohost-chat-silence`
- **RecoveryPolicy bypass** (line 731): `turns_spoken = max_turns` at 2 failures
  prevents the degradation ladder from activating. Documented but not fixed in
  this ADR — it is a separate concern from the TOPIC_CLOSING deadlock.
- **Prefetch prompt identity**: With llama3, prefetch generates near-identical
  text to the main turn (same prompt → same output → guardrail rejection).
  Mitigated by model choice (Gemma4 generates varied prefetches) but the
  structural issue remains for low-diversity models.

---

## 5. Lessons Learned

1. **State machine integration points need explicit state coverage.**
   When `app_shell` checks `controller.state`, it must cover ALL states that
   can appear at that integration point, not just the "happy path" states.

2. **Different models expose different failure modes.**
   llama3 exposed guardrail calibration issues. Gemma4 exposed a state
   machine deadlock. Both were latent in the same codebase.

3. **Prefetch accounting is invisible to the state machine.**
   Prefetched turns bypass `next_action()` entirely. They don't increment
   `turns_spoken` through the normal pipeline. Future work should ensure
   prefetch usage is tracked equivalently to main turns.

4. **65 seconds of silent failure = missing observability.**
   The deadlock was only discovered because a human was watching. The system
   should self-detect when `next_action()` returns `none()` for N consecutive
   ticks and log a warning or attempt recovery.

---

## 6. Commit

```
fix(cohost): add TOPIC_CLOSING to _on_motor_speaking_start state check

When a prefetched kira-agenda-stop response is played, the controller
enters TOPIC_CLOSING state. _on_motor_speaking_start only recognized
{SPEAKING, GENERATING}, causing mark_generation_accepted() to never
be called. The controller remained stuck in TOPIC_CLOSING forever,
never transitioning to the next queued topic.

Adding TOPIC_CLOSING to the recognized set allows the normal
speech→complete→idle→select_topic pipeline to proceed.

Root cause: app_shell.py:2217, one missing enum member.
```
