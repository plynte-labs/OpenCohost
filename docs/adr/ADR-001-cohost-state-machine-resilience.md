# ADR-001: Co-host State Machine Resilience — Post-Mortem

**Status**: Accepted  
**Date**: 2026-05-16  
**Branch**: `fix/kira-cohost-chat-silence` (12 commits)  
**Test sessions**: 3 real-stream stress tests (2M-viewer YT chat, llama3 + gemma4)  
**Tests**: 256 passing (+20 integration tests added for uncovered scenarios)

---

## 1. Context

After deploying the `kira-auto-topic-suggestion` SDD feature, real-stream testing
revealed multiple state machine failures that unit tests never caught:

1. Kira froze in `SPEAKING` state after chat spikes between agenda turns.
2. Chat responses were silently dropped when the controller entered `PAUSED`.
3. A single guardrail rejection left Kira permanently muted (`REGENERATING_SAFE` deadlock).
4. Guardrail rejections during topic closing cascaded into 20+ LLM calls in a loop.
5. Chat stopped working when all topics were exhausted (required manual "Emergencia").
6. OBS joyita rendered as a single line (font size 72) and had no content filter.

The 315+ existing tests were all unit-level. None simulated real-stream scenarios:
chat spikes between turns, guardrail rejection recovery, topic exhaustion, or
the interaction between the motor thread and controller state machine.

---

## 2. Root Cause Analysis

### 2.1 Architectural anti-pattern: motor source string as state proxy

**The bug**: `_is_kira_agenda_speech_source()` checked `motor.current_speech_source.startswith("kira-agenda")`.
The controller can emit actions with source=`"chat"` (HANDLE_CHAT) or `"ptt"` (HANDLE_STREAMER).
These don't start with `"kira-agenda"`, so `mark_generation_accepted()` and
`mark_speech_complete()` were never called. The controller stayed in `SPEAKING` forever.

**The fix** (668abe4): Use the controller's OWN state as the signal:
```python
# Before (fragile)
agenda_speech = self._is_kira_agenda_speech_source()
# After (authoritative)
agenda_speech = self.kira_agenda.state in {AgendaState.SPEAKING, AgendaState.GENERATING}
```

**Principle**: Never use an external system's metadata as a proxy for internal state.
The source string is set by the *caller* (AppShell → motor.enqueue). The controller's
state is set by the *controller itself*. Trust the controller.

**This anti-pattern caused bugs #1, #2, and contributed to #3.**

### 2.2 State machine gap: REGENERATING_SAFE unhandled

**The bug** (768f6c5): `next_action()` had no handler for `REGENERATING_SAFE`.
When a guardrail rejected output, `register_failure()` set state to `REGENERATING_SAFE`.
The next tick called `next_action()` which fell through all guards and returned `none()`.
The tick kept firing every 4.5s but never recovered.

**Why it wasn't caught earlier**: With the old `max_failures=3`, three consecutive
rejections → `PAUSED` → tick stopped → operator saw the warning. With
`RecoveryPolicy`'s `max_failures=5`, a single rejection could deadlock.

**The fix**: Transition `REGENERATING_SAFE` → `WAITING_SIGNAL` at the top of `next_action()`.

### 2.3 Infinite retry: closing cascade

**The bug** (9ca8414): When a topic was complete (`turns_spoken >= max_turns`), the tick
generated `kira-agenda-stop` (closing line). If guardrails rejected it:
1. `register_failure()` → state = `REGENERATING_SAFE`
2. Tick → `WAITING_SIGNAL` → `_topic_complete()` = True → `_closing_action()` again
3. Goto 1

Each iteration was a full LLM call. Observed: 20+ consecutive calls in 1.5 minutes.

**The fix**: After 3 consecutive closing rejections, force-complete the topic
without speaking a closing line. Reset recovery counter for the next topic.

### 2.4 Passive state: IDLE+empty blocks chat

**The bug** (bb44452, e6279d8): When all topics completed, the controller stayed in `IDLE`
with no active topic and no queued topics. `next_action()` returned `none()` for all inputs,
including `compact_chat`. The standalone RF3 reaction path was never reached because
`state != OFF` routed chat through the controller.

**The fix**: When `state == IDLE` and `active_topic is None` and `not queued_topics()`,
auto-transition to `OFF`. Tick stops, UI updates, chat flows through RF3.

---

## 3. Design Decisions

### 3.1 RecoveryPolicy (91c88cc)

Standalone class injected into `KiraAgendaController` via composition. Not inheritance.

```
KiraAgendaController          RecoveryPolicy
┌──────────────────┐          ┌──────────────────────────┐
│ register_failure │──calls──▶│ record_failure(error)     │
│ can_auto_resume  │◀──asks───│ should_auto_retry()       │
│                  │          │ is_hard_paused()          │
│                  │          │ degraded_length()         │
└──────────────────┘          └──────────────────────────┘
```

**Degradation ladder**:
```
failures 1-2 → REGENERATING_SAFE (silent retry)
failure  3   → degrade response_length (expandida→normal→corta)
failures 4-5 → REGENERATING_SAFE, then PAUSED_NEEDS_OPERATOR
after PAUSED → auto-retries at 60s, 120s, 240s intervals
exhausted    → HARD_PAUSED (operator must intervene)
```

**Why not inline**: RecoveryPolicy has its own tests (70 lines). The controller
only calls `record_failure()` and `can_auto_resume()`. If the recovery strategy
changes (e.g., configurable via YAML), only this class is touched.

### 3.2 Error codes (91c88cc)

`ErrorCode` enum with machine-readable codes and Spanish human descriptions:

| Code | Human |
|------|-------|
| `ERR_GUARDRAIL_LOOPING` | "Respuesta repetitiva" |
| `ERR_GUARDRAIL_LEAK` | "Frase interna filtrada" |
| `ERR_GUARDRAIL_SIMILAR` | "Respuesta muy parecida a la anterior" |
| `ERR_GUARDRAIL_EMPTY` | "El modelo no generó respuesta" |

Surfaced in Stream Admin panel and cohost panel without popups (streamer-friendly).

### 3.3 Joyita content safety (622a52f)

`_is_joyita_unsafe()` rejects: URLs, @mentions, payment/advertising keywords.
Applied at scoring level (`_select_highlight` returns -1) and OBS gateway (score ≥100).

`_format_joyita_for_obs()` word-wraps at 38 chars into 2-3 lines with ellipsis truncation.

### 3.4 Crash resilience (825dc1b)

`sys.excepthook` + `threading.excepthook` + `Tk.report_callback_exception` →
all write to `logs/crash.log` with timestamp, thread name, and full traceback.
Previously silent crashes (app disappears with no log) now leave a diagnostic artifact.

---

## 4. Test Coverage Gaps Closed

| Scenario | Tests added | File |
|----------|-------------|------|
| Chat spike between agenda turns | 4 | test_kira_agenda_controller.py |
| PAUSED state chat routing | 3 | test_kira_agenda_controller.py |
| REGENERATING_SAFE recovery | 1 | test_kira_agenda_controller.py |
| RecoveryPolicy degradation + pause + auto-retry | 3 | test_kira_agenda_controller.py |
| Joyita content safety (URLs, @, ads) | 9 | test_smart_aggregator_ui.py |
| Joyita OBS formatting (wrap, cap) | 3 | test_smart_aggregator_ui.py |
| Joyita score threshold | 3 | test_smart_aggregator_ui.py |

Before: 315 tests, all unit-level. After: 256 tests, +20 integration.
(Reduction from 315→256 is because some edge_tts-dependent tests were excluded.)

---

## 5. Lessons Learned

1. **Integration tests must simulate real-stream timing**. Chat spikes between
   turns, guardrail rejection during speech, and topic exhaustion are NOT edge
   cases — they're the normal operating conditions of a live stream.

2. **Anti-pattern: motor source string as controller state proxy**. The motor's
   `current_speech_source` reflects what the *caller* passed. The controller's
   `state` reflects what the *controller* decided. Trust the controller.

3. **State machine gaps are silent killers**. Every state in the enum MUST have
   a handler in `next_action()`. If not, the tick fires forever but does nothing.

4. **Retry loops need hard caps**. The closing cascade burned 20+ LLM calls
   before the cap. Always add `MAX_ATTEMPTS` to any retry logic that involves
   external API calls.

5. **Paused ≠ broken**. When the controller pauses, the product should still work.
   Chat must fall through to standalone RF3. Joyita must clear. UI must show WHY.

6. **RecoveryPolicy as a separate class was the right call**. It's 70 lines,
   fully testable, and the controller only calls 3 methods. If we need to make
   recovery configurable, we touch one file.

---

## 6. Future Work

- [ ] Make RecoveryPolicy thresholds configurable via YAML
- [ ] Add diagnostic log panel showing last 10 state transitions
- [ ] Pending chat merge (accumulate instead of overwrite)
- [ ] 7-hour long-stream endurance test
