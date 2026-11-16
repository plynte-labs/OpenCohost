# Turn state (`opencohost/core/engine/turn_state.py`)

One explicit `TurnState` per **foreground** turn (feature `turn-state-machine-20261112`).
Every transition logs one `[TURN_STATE]` line, so a turn can be traced from the log alone.

## Binding

The open turn is bound to the thread that opened it (`open_turn` / `close_turn`, a
`threading.local`, like `turn_timing.turn_clock()`). Background pregen and agenda generations
(`_generar_dialogo(commit_history=False)`) run the same call sites on daemon threads; they see
no bound turn, so their hooks are no-ops and never touch the foreground turn.

Turns are opened and closed by the two owners of `_processing`, inside their existing
`try/finally` and before `_complete_processing_cycle`: `MotorVocalIA._dispatch_command`
(`process_context`) and `MotorVocalIA._process_priority_queue` (queued item and accumulation
flush). `close_turn()` closes a turn still open as `failed`, so every turn gets exactly one
terminal line, even on an exception.

## Log lines

```
[TURN_STATE] tid=t<8 hex> source=<code> <from>-><to> event=<event>[ ms=<int>][ attempt=<int>]
[TURN_STATE] illegal tid=t<8 hex> source=<code> phase=<phase> event=<event>     (WARNING)
```

The first line of a turn is `start->assembling event=begin`. An illegal pair logs a warning and
keeps the phase; it never raises. Any event after a terminal phase is ignored (DEBUG line).
The `tid` field (`t` + 8 hex) is deliberately not the shape of prompt_trace's per-call
`turn=` id (bare hex, matched by the diagnostics MCP `_TURN_RE`).

## State x event table

Blank cell = illegal. `complete`, `drop` and `fail` are legal from every active phase.

| phase \ event | prompt_built | first_audio | guard_revert | guard_block   | guard_retry | stream_truncate | stall      | rollback   | cloud_retry | served_pregen | refuse  | expire  |
|---------------|--------------|-------------|--------------|---------------|-------------|-----------------|------------|------------|-------------|---------------|---------|---------|
| assembling    | generating   |             |              |               |             |                 |            |            |             | completed     | refused | expired |
| generating    |              | streaming   | reverted     | guard_blocked |             |                 | recovering |            | assembling  |               |         |         |
| streaming     |              |             |              |               |             | guard_blocked   | recovering |            |             |               |         |         |
| reverted      |              |             |              | guard_blocked |             |                 | recovering |            | assembling  |               |         |         |
| guard_blocked |              |             |              |               | generating  |                 |            |            | assembling  |               |         |         |
| recovering    |              |             |              |               |             |                 |            | recovering | assembling  |               |         |         |

Terminal phases: `completed`, `dropped`, `failed`, `refused`, `expired`. Every foreground turn
ends in exactly one of them.

`completed` means the reply was **handed to speech**. Playback continues on the speech router
thread; speech-side lines are `[TURN_SPEECH]` (below).

## Where each event is emitted

All hooks are one-liners: `turn_state.emit(event, ...)`, a no-op without a bound turn, swallowing
any tracker error.

| Event | File : symbol |
|---|---|
| begin | `core/llm_engine.py : MotorVocalIA._dispatch_command` (`process_context`) and `_process_priority_queue` (`turn_state.open_turn`) |
| refuse | `core/llm_engine.py : MotorVocalIA._dispatch_command`, the `ollama_unavailable` refusal (one-shot turn, `_log_turn_outcome`) |
| expire | `core/llm_engine.py : MotorVocalIA._process_priority_queue`, each TTL-expired item (one-shot turn) |
| served_pregen | `core/llm_engine.py : MotorVocalIA._process_priority_queue`, after `_speak_pregenerated` |
| drop (cached) | `core/llm_engine.py : MotorVocalIA._process_priority_queue`, cached `reasoning_budget_exhausted` |
| prompt_built (ms) | `core/llm_engine.py : MotorVocalIA._generar_dialogo`, after `_build_generation_request` |
| cloud_retry | `core/llm_engine.py : MotorVocalIA._generar_dialogo`, the cloud to local retry |
| fail | `core/llm_engine.py : MotorVocalIA._generar_dialogo` catch-all (it returns an empty result, which would otherwise read as dropped), and `close_turn()` for a turn left open |
| first_audio (ms) | `core/engine/generation_orchestrator.py : GenerationOrchestrator._stream_sentence_action` (`[STREAM_TTFA]`) |
| guard_revert | `core/engine/generation_orchestrator.py : GenerationOrchestrator.handle_stream_sentence`, first `revert` action (`[STREAM_REVERT]` and the other pre-audio reverts) |
| stream_truncate | `core/engine/generation_orchestrator.py : GenerationOrchestrator.apply_stream_guard_verdict`, trip path |
| stall (attempt), rollback | `core/engine/generation_orchestrator.py : GenerationOrchestrator.execute_generation_attempt`, watchdog branch (`rollback` only when the model changed) |
| guard_block, guard_retry | `core/llm_engine.py : MotorVocalIA._finalize_generation`, buffered guard block, its retry, and a blocked retry |
| complete / drop (ms) | `core/llm_engine.py : MotorVocalIA._ejecutar_inferencia` tail (`[TURN_LATENCY]` reply branch / dropped branch) |

Hooks inside `_generar_dialogo` / `_finalize_generation` are also gated on `commit_history`.
Not instrumented: the echo-guard retry (`_apply_echo_guard`). The speech side is `[TURN_SPEECH]` (below), not part of the phase machine.

## Speech outcomes: `[TURN_SPEECH]`

The phase machine stops at `completed` (handed to speech). It is deliberately NOT extended to the
router thread. Instead the speech outcome is tied to the turn by a tid stamp:

- `SpeechJob.turn_uid` (immutable, default None) is set by `SpeechRouter.submit` /
  `submit_streaming` from `turn_state.current_tid()` on the submitting thread. It is NOT
  `SpeechJob.turn_id` (the vision-read / feed token). Agenda, background pregen and notices from
  other threads have no bound turn, so `turn_uid` is None and they log nothing.
- The router logs from its existing post-lock log sites; never under `_sched_lock`, never taking
  `MotorVocalIA._lock`.

```
[TURN_SPEECH] tid=t<8 hex> job=<id|-> state=<state> reason=<code>
```

| state | reason | site |
|---|---|---|
| started | activated | `_run_job`, first activation only |
| resumed | resume | `_pick`, a job popped off the stack |
| suspended | ptt, preempt, user_pause | `_run_job` pending pause, `_reconcile`, `_starved_wait` |
| finished | complete | `_finish` (FINISHED) |
| discarded | interrupted, cancelled, stopped, discard_stale, discard_residue, unwind | `_finish`, `sweep_sources` (cancelled), `_sweep_non_agenda_stack` (discard_residue), `discard_job` (discard_stale), `_release` (unwind) |
| lost | error | `_finish` DISCARDED with reason error (pairs with `[SPEECH_LOST]`) |

Router off (`_speak_or_submit` blocking inline `_hablar`): the engine thread logs
`started`/`finished` (or `lost error` if `_hablar` raises, then re-raises) with `job=-`.

`reason` is always a fixed code; no spoken or prompt text. Logging is exception-safe and a no-op
without a `turn_uid`.

## Turn event (JSONL)

`close_turn()` writes exactly one JSON line per **foreground** turn to
`<LOG_DIR>/turn_events.jsonl` (`opencohost/core/engine/turn_events.py`), a dedicated
`RotatingFileHandler` (5 MB x 3 backups, `propagate=False`, never into the main log). Pregen and
agenda background generations have no open turn and write nothing; refused and expired one-shot
turns write an event. Writing is exception-safe and swallowed on error.

The line is built ONLY from a fixed allowlist (`turn_events.ALLOWLIST`): unknown keys and values of
the wrong shape are dropped. Strings are short codes (`^[a-z0-9_.:\-]{1,64}$`), the tid
(`^t[0-9a-f]{8}$`), a model id (`^[A-Za-z0-9_.:/\-]{1,100}$`) or an exception class name.

| key | type | meaning |
|---|---|---|
| `v` | int | schema version (1) |
| `ts` | int | turn open time, epoch ms |
| `tid` | str | turn id, joins every per-turn log line (`[TURN_STATE]`, `[TURN_SPEECH]`, `[TURN_LATENCY]`, `[PROMPT_TRACE]`) |
| `source` | code | turn source (`direct`, `ptt`, ...) |
| `model` | str | model id of the last prompt built (cloud profile model for cloud turns) |
| `outcome` | code | terminal phase: completed, dropped, failed, refused, expired |
| `latency_outcome` | code | `[TURN_LATENCY]` outcome: ok, retried, fallback, dropped |
| `path` | code | `stream` or `buffered` |
| `phases` | [code] | phase chain from `assembling` to the terminal phase (max 64) |
| `events` | {code: int} | applied-event counts |
| `guards` | [code] | guard verdicts in order: guard_revert, guard_block, guard_retry, stream_truncate |
| `attempts` | int | generation attempts (prompt_built + guard_retry, or the highest stall attempt) |
| `cloud_retry` | bool | the turn was retried locally after a cloud failure |
| `timings` | {key: int} | whole ms; keys engine_ms, queue_wait_ms, total_ms, recall_ms, web_ms, prompt_build_ms, llm_ms, ttfa_ms |
| `reasons` | [{reason, exc}] | failure reason codes with the exception class name (max 8) |
| `speech` | code | speech state known at close (router-off inline path only); absent otherwise, since router-thread playback continues after close and is logged as `[TURN_SPEECH]` |

`tid=` is also appended to `[TURN_LATENCY]` and `[PROMPT_TRACE]` while a turn is open (prompt_trace
keeps its own per-call `turn=` id unchanged).

### Failure reasons

`reasons[].reason` is a member of `turn_events.REASON_CODES` (anything else is dropped); `exc` is
`type(e).__name__`, never the message. Recorded with `turn_state.fail_reason(code, exc)` (no-op without
an open turn).

| reason | File : symbol |
|---|---|
| `engine_error` | `core/llm_engine.py : _generar_dialogo` catch-all |
| `watchdog_timeout` | `core/engine/generation_orchestrator.py : execute_generation_attempt`, watchdog branch |
| `ollama_transport_error` | same, local transport failure |
| `cloud_bad_key`, `cloud_rate_limited`, `cloud_transient`, `cloud_error` | same, cloud transport failure by class |
| `stream_error` | `generation_orchestrator.py : run_streaming_attempt` (BaseException) |
| `stream_seal_failed` | `generation_orchestrator.py : stream_partial_exit`, seal raised |
| `guard_retry_failed` | `core/llm_engine.py : _retry_after_guard_block` |
| `agenda_transform_failed` | `core/llm_engine.py : _finalize_generation`, agenda transformer on the guard retry |
| `dialogue_callback_failed` | `core/llm_engine.py : _emit_dialogue` |
| `tts_submit_failed` | `core/engine/llm_engine_speech.py : _speak_or_submit`, router-off inline `_hablar` raised |
| `turn_aborted`, `turn_unclosed` | `turn_state.close_turn`: an exception escaped the turn / the turn was left open |

A failed stream usually records two reasons (`stream_error` then the handler that caught it).

## Reading a turn by tid (diagnostics MCP)

The `opencohost-diagnostics` server (`opencohost/mcp_local/diagnostics_tools.py`) reads these records
read-only and bounded:

- `get_turn("t0a1b2c3d")`: a tid (`^t[0-9a-f]{8}$`) returns `{source_of_data, tid, event?, lines}`.
  `event` is the allowlisted JSONL event (every line goes through `turn_events.sanitize`) found in
  `turn_events.jsonl` or a rotated backup (`.1` to `.3`, last 6 MB of each). `lines` are the
  `[TURN_STATE]`, `[TURN_SPEECH]`, `[TURN_LATENCY]` and `[PROMPT_TRACE]` lines carrying `tid=<tid>` from
  the newest session log that has any (5 newest logs searched, max 60 lines), each as
  `{time, tag, fields, transition?}`; only code-like `key=value` tokens survive, never free text.
  Unknown tid: `{"error": "turn_not_found"}`. The legacy 8-hex prompt-trace id behaves as before.
- `list_turns(kind="events", limit=50)`: recent turn events newest first, each
  `{tid, ts, source, outcome, model, total_ms, reasons}` (`reasons` = reason codes only), plus
  `total` and `truncated`. Without `kind`, `list_turns` is unchanged.

Malformed JSON lines and missing files are skipped silently.

## Privacy

A `[TURN_STATE]` line carries codes and integers only: turn id, a sanitized source code, phases,
event, and optional `ms` / `attempt`. Never prompt, chat or reply text. Unknown keyword fields and
non-integer values passed to `apply()` are dropped.
