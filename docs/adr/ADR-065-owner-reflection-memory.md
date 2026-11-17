# ADR-065: Owner Reflection Memory (Idle Distillation of Owner Q&A Facts)

**Date**: 2026-10-16
**Status**: Accepted 2026-10-16 (owner confirmed the defaults and accepted the sweep blocking and the extra judge call, section 10). Implementation: T2 to T4 of the feature document.
**Feature document**: `odd/tasks/owner-reflection-memory-20261015.md`
**Related**: ADR-060 (fact ranking), ADR-061 (correction supersede), ADR-062 (draft scrub and origin), `odd/tasks/session-topic-folder-20261016.md` (in-session topic index, the capture pattern reused here)

---

## Context

Facts the owner states in conversation are kept only when the owner asks for an explicit save, or when a turn happens to become a draft. Neuro distills Q&A facts every N messages (`kimjammer/Neuro memory.py:63-99`). We adopt the cadence, not the privacy model: here only owner turns may be distilled.

The code shapes the design in six ways that the feature document did not know:

1. **The history window is 3 turns.** `HISTORY_MAX_TURNS = 3` (`opencohost/config/settings.py:84`), and `_max_turns` starts from it (`opencohost/core/llm_engine.py:671`). N=8 owner exchanges cannot be read from the window. Raising the window would also change the KV-cache prefix (ADR-064), so it is not an option.
2. **Viewer text is in history, but not as a user turn.** `_HISTORY_ASSISTANT_ONLY_SOURCES = frozenset({"chat"})` (`llm_engine.py:214`). Viewer chat is stored assistant-side only. The owner allowlist must therefore be applied at capture time, not inferred from the history shape.
3. **The existing judge sweep is synchronous and runs once per launch.** `promote_pending_drafts` runs on the engine thread in the idle branch of `run()` (`llm_engine.py:1012-1024`), and `_promotion_swept` stops it after one successful pass (`llm_engine.py:1019-1024`). Drafts written mid-session are not judged until the next launch, and unjudged drafts are never injected (ADR-062, `memoria_store.py:2286-2294`). The distillation design must therefore re-arm the sweep.
4. **`call_with_watchdog` abandons the request, it does not cancel it.** `llm_inference_service.py:336-375` runs the call on a daemon thread and returns on timeout while the HTTP request keeps running. That is the failure mode of `heavy_model_inference_recovery_20260609` (the runner stayed busy after a watchdog abandon, fixed by `a8830bb`). The distillation job must not use this path.
5. **Runner busy-state is already exposed.** `llm_generating` is True only while an Ollama generation is in flight, foreground or pregen (`llm_engine.py:865-866`). `is_speaking` reports speech (`llm_engine.py:850-855`). `_promotion_ptt_held` records a physical PTT hold (`llm_engine_speech.py:670-691`).
6. **Supersession is turn-bound.** `_correction_target` and `_supersede_memoria` (`llm_engine_memorias.py:775-826`) act only on a correction turn that follows a captured pair. A distilled fact is not a turn, so contradiction handling must come from the judge's `duplicate`/`update` actions over the related existing facts (`promotion_parser.py:~165-175`, F2 block in `_run_promotion_judge`, `llm_engine_scout.py:521-560`).

## Decision

### 1. Trigger: every N owner turns (N = 8, owner-confirmable)

`_commit_history` (`llm_engine_memorias.py:246`) already decides eligibility for each committed pair (source allowlist at `:316-323` and `:420-427`, private gate at `:427`). When a pair passes the owner gate, the commit hook increments `self._owner_turns_since_reflection`. When the counter reaches `OWNER_REFLECTION_EVERY_N` (default 8), the job becomes *due*. A due job runs only when the idle predicate (section 5) holds. The counter is reset only when a run completes. A cancelled run keeps the counter, so it is retried at the next idle window.

### 2. Idle definition (owner-confirmable)

Idle means all of the following hold at the moment the job starts:

| Check | Source |
|---|---|
| `MEMORIAS_ENABLED` and the reflection flag are on | settings, section 8 |
| not `is_processing` (no turn in flight) | `llm_engine.py:860-863` |
| not `llm_generating` (no Ollama generation, foreground or pregen) | `llm_engine.py:865` |
| not `is_speaking` (no TTS playing) | `llm_engine.py:850-855` |
| not `_promotion_ptt_held` (no physical PTT hold) | `llm_engine_speech.py:676-691` |
| `command_queue` empty and `_priority_queue` empty; no pending accumulation buffer | `llm_engine.py:1299`, `:1345`, `:1402`, `:1809` |
| at least 30 s since the last turn completed | new `_last_turn_done_monotonic`, set in `_complete_processing_cycle` (`llm_engine.py:1727-1729`) |
| the existing promotion lifecycle gates pass (no model switch pending, profile set, judge model resident) | `_promotion_lifecycle_gate` and `_promotion_gate` (`llm_engine_scout.py:590-618`) |

The last gate reuses the existing residency rule. The job never loads a model. If the judge model is not the resident one, the run is skipped with `reason=model_not_resident`. This keeps a model swap, and the cold load it causes on the next turn (the open 21-minute keep-alive datum in `CLAUDE.md`), off the idle path.

### 3. Eligibility filter: owner-only, enforced by structure

Eligible sources are `direct` and `ptt` only. `owner-bundle` (`settings.py:235`) is excluded by default (owner-confirmable). It is in `_DIGEST_CAPTURE_SOURCES` (`llm_engine.py:205`), but a bundle is a composed, scripted set of questions, not a turn the owner spoke freely.

The filter is structural, in a new pure module `opencohost/core/context/owner_reflection.py`:

- `OwnerExchange` is a frozen dataclass with fields `seq: int`, `user_text: str`, `kira_text: str`.
- The only constructor is `owner_exchange_from_turn(source, private, seq, user_text, kira_text) -> OwnerExchange | None`. It returns `None` unless `source in {"direct", "ptt"}` and `private is False`. It also returns `None` for the agenda sentinel (`"[agenda segura"`, `llm_engine_memorias.py:96`).
- The ring buffer (section 4) accepts only `OwnerExchange`. The commit hook calls the constructor and nothing else.
- `build_distillation_prompt(batch: Sequence[OwnerExchange]) -> str` accepts only `OwnerExchange`. There is no string or history parameter, so a viewer exchange cannot reach the prompt through this function.
- The repetition check (section 6) reads recent Kira lines in code. It never sends them to the model, so Kira replies that were shaped by viewers cannot re-enter the prompt through the check.

The test in section 9 asserts on the built prompt string, not on intent.

### 4. Input source: a bounded RAM ring, not the history window (exception to the "no new store" rule)

The ring is `collections.deque(maxlen=16)` of `OwnerExchange`, with each side capped at 300 characters (the same cap as `_MEMORIA_CONTENT_MAX_CHARS`, `llm_engine_memorias.py:1362`). It is:

- RAM only. Never persisted, never logged.
- cleared on session reset and profile switch, under `_history_lock`, the same way `_session_memoria_titles` is cleared (`llm_engine.py:1274`, `llm_engine_memorias.py:874`).
- the batch source. A run takes up to 8 exchanges with `seq > last_distilled_seq`, oldest first.

This is a deliberate, narrow exception to the feature document's "no new raw-text store". It keeps raw owner text in RAM for at most 16 exchanges, which is the same class of storage as the history deque and the session topic index. That index already keeps raw `TopicFragment.user` text in RAM (`opencohost/core/context/session_topics.py:TopicFragment`, bounded by `max_topics` × `max_fragments`). No persisted copy is added. A run that needs exchanges already evicted from the ring reports `skipped_evicted` and proceeds with what remains.

Each exchange gets an opaque owner-turn identifier, `seq`, assigned from a per-session counter that increments only for eligible exchanges. The provenance id is `"<session_token>:<seq>"`, where `session_token` is an 8-hex value generated at session start. It contains no profile, user, or host data.

### 5. Runner yielding: a dedicated worker, cancelled on arrival

The job must not run on the engine thread. The engine thread is single-threaded (`run()` reads `command_queue` only between calls, `llm_engine.py:1012-1024`), so an engine-thread call would delay every turn for the whole HTTP round trip. The job runs on one daemon worker thread named `owner-reflection`, the same pattern as the session topic executor (`llm_engine_memorias.py:539-585`, `max_workers=1`).

- **Start.** The idle predicate (section 2) is evaluated on the engine thread at the idle branch. If it holds, the job starts. The start is recorded under the lock, and a generation counter is bumped.
- **Arrival.** Every entry point that can queue an owner turn calls `note_owner_turn_arrival()`. These are the `replace_pending`, `enqueue_accumulation` and `command_queue` paths, and the PTT press in `pause_speech_for_ptt` (`llm_engine_speech.py:676`). The hook does three things, without blocking:
  1. bumps the generation, so any result still in flight is discarded;
  2. sets a `threading.Event` that the worker checks before the request, between stream chunks, and before any write;
  3. closes the in-flight HTTP response from the calling thread. This is the only way to release the runner, because the request is not a Python generator the caller can close.
- **No watchdog.** The job does not use `call_with_watchdog` (`llm_inference_service.py:336`), because it abandons the request. It uses the streaming seam (`chat_streaming`, `llm_inference_service.py:539-585`), which closes its stream in `finally`, with a dedicated client whose timeout equals the job budget (`OWNER_REFLECTION_JOB_TIMEOUT_S`, default 20 s). This follows the `a8830bb` lesson: the HTTP timeout must match the budget.
- **Bounded work.** `num_predict` is 256 (the promotion judge uses 1024, `llm_engine.py:298`). The prompt is at most 16 × 300 characters, which bounds prefill.
- **Cancelled means discarded.** A result from a cancelled generation is dropped. Nothing is written, `last_distilled_seq` does not advance, and the counter is kept.

**Gate to verify (open, owner-visible).** Cancellation frees the runner only if Ollama aborts generation when the client socket closes. `CLAUDE.md` lists this as an unverified part of the streaming gate ("the Ollama runner is freed on socket close"). Prefill is also uninterruptible in the Python process; only the socket close can stop it. T3 includes a spike that confirms the response handle can be closed from another thread. T5 measures the turn delay when a turn arrives mid-job. If T5 shows the runner stays busy after a socket close, the feature stays off for that build, and this ADR is revised.

### 6. Repetition threshold

A candidate fact is rejected before any write when either check matches:

- **Against recent facts.** Its normalized text (question plus answer) has char-4-shingle Jaccard ≥ 0.90 with any of the last 20 injection candidates (`list_injection_candidates`, `memoria_store.py:2286`), or its `derive_stable_key` collides with an existing row (`memoria_store.py:421`).
- **Against recently spoken lines.** Its answer has the same Jaccard ≥ 0.90 against Kira's last 6 spoken lines, taken from the ring. The check uses `_jaccard` and `_char_shingles` from `repetition_guard.py:94-106`, with `shingle_k = 4`. The threshold matches `near_exact_threshold` (`repetition_guard.py:65`) and is owner-confirmable.

The 0.90 threshold is chosen so that a corrected fact passes. For example, "my dog is called Rex" against "my dog is called Max" shares too few 4-shingles to reach 0.90. A corrected fact must reach the judge so that supersession (section 7) can act on it. The T2 fixture asserts both the repeated case (rejected) and the slot-change case (passes).

Rejected candidates increment `repeated`. The text is never logged.

### 7. Write path: the existing memory path, so ranking and supersession apply

A passing candidate becomes a normal memoria draft through the existing capture method:

1. The fact is built in code, not by the model. Title = question (at most 80 characters, `build_title`). Content = answer (at most `_PROMOTION_TEXT_MAX_CHARS` = 220, `llm_engine.py:299`). Signature = `build_signature` of question plus answer.
2. `stable_key = derive_stable_key(profile_id, question + " " + answer)` (`memoria_store.py:421`). A `None` result counts as `unkeyed` and skips the candidate.
3. The write is `_capture_memoria(profile_id, stable_key, title, content, signature)` (`llm_engine_memorias.py:916`). This is the same call the commit path uses, so draft scrub and origin rules (ADR-062) apply. There is no direct `MemoriaStore` write.
4. `provenance` is a new nullable JSON column on the memoria table, added the same way ADR-062 added `origin` (`memoria_store.py:745`). It is written through `upsert_draft`, which gains an optional `provenance` keyword. The model never supplies provenance. Code maps the indices in the model's reply back to `seq` values in the batch, and rejects any index outside the batch.
5. The distilled draft is unjudged. ADR-062 keeps it out of injection until the judge keeps it. The job therefore re-arms the judge sweep by setting `_promotion_swept = False` after a run writes at least one draft (`llm_engine.py:1019`). The sweep then runs at the next idle window under the same predicate.

**Supersession.** A contradicting fact is handled by the judge, not by a new mechanism. The sweep already passes related existing facts in the same judge call (F2, `_run_promotion_judge`, `llm_engine_scout.py:521-560`). When the judge answers `update` with a valid `E#` target, the existing row is rewritten in place. This is the same ADR-061 outcome (same row id, revision + 1), reached through the judge. `_correction_target` is not used, because it requires a correction turn.

**Pre-existing blocking.** The judge sweep is synchronous on the engine thread and not cancellable (section 5 does not apply to it). A turn arriving during a sweep's judge call waits for that call. This is existing behavior, and it is now more frequent because the sweep is re-armed. It is an open owner decision (section 10).

### 8. Kill switch and configuration

- Environment flag `OPENCOHOST_OWNER_REFLECTION`, default OFF. Accepted on values: `on`, `1`, `true`, `yes`. The resolver follows `_resolve_session_topics` (`settings.py:1134-1139`). Changing it needs a restart. The owner sets it in the launch bat.
- `OWNER_REFLECTION_ENABLED` is the resolved setting. It is False unless the environment flag is set, and it also requires `MEMORIAS_ENABLED`.
- `OWNER_REFLECTION_EVERY_N = 8`, `OWNER_REFLECTION_IDLE_S = 30`, `OWNER_REFLECTION_MAX_FACTS = 3` (facts per run, owner-confirmable), `OWNER_REFLECTION_REPEAT_THRESHOLD = 0.90`, `OWNER_REFLECTION_JOB_TIMEOUT_S = 20`.
- With the flag off, the commit hook does nothing beyond today's behavior. No ring, no counter, no thread.

### 9. Logging: counters only

Each run emits one line through the existing `_eng.logger`:

`[OWNER_REFLECTION] outcome=<ok|skipped|cancelled|failed> reason=<code> considered=<int> eligible=<int> distilled=<int> repeated=<int> schema_rejected=<int> unkeyed=<int> written=<int> elapsed_ms=<int>`

Reason codes are fixed strings from the idle predicate or the failure class. No question, answer, title, content, provenance value, viewer text, or raw model output appears in any log line, error message, or exception text (`RC-8` rule, as in `_capture_memoria`).

### 10. Resolved decisions and open owner decisions

**Owner-confirmable defaults (adopted):**

| Decision | Default |
|---|---|
| Default on or off | OFF, behind `OPENCOHOST_OWNER_REFLECTION` |
| N | 8 owner turns |
| Idle | section 2, including 30 s since the last turn completed |
| Eligibility | `direct` and `ptt` only; `owner-bundle` excluded |
| Input | RAM ring of 16 owner exchanges, cleared on reset and profile switch |
| Facts per run | at most 3 |
| Repetition threshold | char-4-shingle Jaccard ≥ 0.90 |
| Provenance column | new nullable JSON column, as ADR-062's `origin` |
| Judge model | the resident judge model only; no model load on the idle path |
| Sweep re-arm | after a run writes at least one draft |

**Owner decisions, recorded 2026-10-16 (resolved):**

1. **Sweep blocking: accepted.** The re-armed judge sweep stays synchronous and non-cancellable on the engine thread. T5 measures its cost with `[TURN_LATENCY]`.
2. **Extra LLM call per run: accepted.** The sweep is re-armed after a run writes a draft, which adds one judge call per run.
3. The defaults in the table above are confirmed: OFF by default behind `OPENCOHOST_OWNER_REFLECTION`, N = 8, and the 30 s idle rule.

**Gate, not a decision:** the socket-close runner release (section 5). It is verified by a T3 spike and T5. It is not an owner choice.

## Test plan for T2 (RED first)

Each test must fail for the assertion, not for an import. T2 creates `opencohost/core/context/owner_reflection.py` as a stub whose functions raise `NotImplementedError`, so the RED result is a failing assertion. Tests use the existing fakes and fixtures of the files named below.

Acceptance criteria from the feature document:

- **AC1** zero viewer-source text in the distillation input.
- **AC2** a turn arriving during distillation is served without waiting for the job.
- **AC3** distilled facts pass through ranking and supersession.
- **AC4** the repetition check blocks a repeated-fact fixture.
- **AC5** live, no speech loop (T5, not RED-testable).

### File A: `tests/test_owner_reflection_privacy.py` (next to `tests/test_session_topics_capture.py`)

| Test | Asserts | AC |
|---|---|---|
| `test_viewer_sentinel_absent_from_built_prompt` | Ring fed with direct and chat exchanges, each chat side carrying a unique sentinel string. The built prompt contains zero sentinels and contains the direct sentinels. | AC1 |
| `test_only_direct_and_ptt_construct_exchanges` | Parametrized over `direct`, `ptt`, `chat`, `owner-bundle`, `agenda`, unknown. Only `direct` and `ptt` with `private=False` return an `OwnerExchange`. | AC1 |
| `test_private_turn_never_constructs_exchange` | `private=True` or `None` returns `None` for an owner source. | AC1 |
| `test_agenda_sentinel_never_constructs_exchange` | Text starting with `[agenda segura` returns `None`. | AC1 |
| `test_commit_hook_feeds_ring_only_from_owner_gate` | Drives `_commit_history` with a viewer pair and an owner pair. Ring holds only the owner exchange. | AC1 |
| `test_provenance_is_opaque_owner_turn_ids` | Provenance values match `^[0-9a-f]{8}:\d+$`. No profile, user, or text appears. | AC1 |
| `test_logs_are_counters_only` | `caplog` over a full run contains no sentinel, no answer text, and no title. Only the `[OWNER_REFLECTION]` counter line appears. | RC-8 |

### File B: `tests/test_owner_reflection_scheduling.py` (next to `tests/test_heavy_model_inference_recovery.py`)

| Test | Asserts | AC |
|---|---|---|
| `test_flag_off_is_inert` | With the env flag unset, no ring, no counter change, no thread started. | kill switch |
| `test_flag_resolution_values` | `on`, `1`, `true`, `yes` enable. Anything else, including unset, disables. | kill switch |
| `test_job_never_starts_while_turn_pending` | With `is_processing=True`, or a non-empty `command_queue`, or `llm_generating=True`, the due job makes zero chat calls. | AC2 |
| `test_job_never_starts_during_tts_or_ptt_hold` | `is_speaking=True` or `_promotion_ptt_held=True` blocks the start. | AC2 |
| `test_idle_requires_thirty_seconds_since_last_turn` | At 29 s since `_last_turn_done_monotonic`, the job does not start. At 30 s it does. | AC2 |
| `test_turn_arrival_returns_without_waiting_for_job` | A fake chat stream blocks indefinitely. Calling `note_owner_turn_arrival()` returns in under 50 ms. | AC2 |
| `test_arrival_closes_inflight_response` | The fake response's `close` is called once on arrival, from the calling thread. | AC2 |
| `test_cancelled_result_is_discarded` | A result returned after arrival writes nothing, does not advance `last_distilled_seq`, and keeps the counter. | AC2 |
| `test_every_turn_entry_point_notifies_arrival` | Drives `replace_pending`, `enqueue_accumulation`, and `pause_speech_for_ptt`. Each calls the arrival hook. | AC2 |
| `test_job_does_not_use_watchdog` | The job's client call does not go through `call_with_watchdog`. Asserted by patching it to raise. | AC2 |
| `test_model_not_resident_skips_without_load` | With the judge model not resident, the run is skipped with `reason=model_not_resident` and no load call is made. | AC2 |

### File C: `tests/test_owner_reflection_write_path.py` (next to `tests/test_memory_correction_supersede.py`)

| Test | Asserts | AC |
|---|---|---|
| `test_distilled_fact_written_through_capture_only` | A spy on `_capture_memoria` records one call. Direct `MemoriaStore` write methods are not called. | AC3 |
| `test_distilled_draft_is_unjudged_and_not_injected` | After the write, `list_injection_candidates` excludes the row until the judge keeps it. | AC3 |
| `test_sweep_is_rearmed_after_write` | After a run writes one draft, `_promotion_swept` is False. | AC3 |
| `test_contradicting_fact_is_updated_in_place` | Old row "my dog is called Rex" is judged. The new "my dog is called Max" comes back from a fake judge with `update` and the old `E#`. The old row id keeps its id and revision + 1. No second row is created. | AC3 |
| `test_ranking_order_unchanged_after_update` | The updated row sorts by the existing `list_injection_candidates` order (pinned, durable, then rank). | AC3 |
| `test_model_index_outside_batch_is_rejected` | Provenance index 9 for an 8-exchange batch is dropped. Nothing is written for it. | AC3 |

### File D: `tests/test_owner_reflection_repetition.py` (next to `tests/test_memoria_promotion_dedup.py`)

| Test | Asserts | AC |
|---|---|---|
| `test_repeated_fact_is_rejected` | A fact identical to an existing injection candidate is rejected. `repeated=1`, no draft written. | AC4 |
| `test_fact_repeating_recent_kira_line_is_rejected` | A fact whose answer near-duplicates one of the last 6 spoken lines is rejected. | AC4 |
| `test_slot_change_passes_repetition_check` | "my dog is called Max" against "my dog is called Rex" is not rejected. | AC4 |
| `test_stable_key_collision_is_rejected` | Same `derive_stable_key` as an existing row is rejected as a repeat, not as an error. | AC4 |
| `test_threshold_is_configurable` | `OWNER_REFLECTION_REPEAT_THRESHOLD` changes the decision at the boundary. | AC4 |

## Implementation notes (T3, 2026-10-16)

Where the code differs from, or settles, a detail of the sections above:

1. **Supersession is the judge's existing `update`, not an in-place rewrite.** Section 7 and the File C test described the old row being rewritten in place (same id, revision + 1). That is ADR-061's behaviour for a *draft*. For a durable row the existing F2 path (`apply_promotion_batch` with `updates`) marks the old row `inactive` with `superseded_by` set to the new row and promotes the new draft. T3 reuses that unchanged and the test asserts it. Ranking is unchanged: the old row leaves `list_injection_candidates`, the promoted row sorts by the existing order.
2. **Title is the question, clipped to 80 characters.** Section 7 said `build_title`, which returns the first three significant tokens and would discard the question. Content is the answer (at most 220 characters). The signature is `build_signature(question + " " + answer)`.
3. **Only the owner side of an exchange is placed in the prompt.** `OwnerExchange.kira_text` stays in the ring for the repetition check and is never sent to the model, so a Kira line shaped by viewer chat cannot enter the prompt.
4. **A second cancellation path covers the dispatcher.** `Dispatcher.dispatch` puts directly on `command_queue`, which bypasses every `note_owner_turn_arrival` hook. A small watcher thread (25 ms poll) cancels the job when `command_queue`, the priority queue or `_processing` shows work. The explicit hooks in `enqueue`, `replace_pending`, `enqueue_accumulation` and `pause_speech_for_ptt` remain the fast path.
5. **Transport.** `_OllamaStreamHandle` owns an `httpx.Client` whose timeout equals the job budget and posts `/api/chat` itself. The request is sent from `iter_lines` on the worker thread, after the handle is registered, so `close()` can abort a request still blocked on prefill. The base URL comes from `main_ollama_base_url()` (`OLLAMA_HOST`). The ollama-python client was not used because its stream is a generator that another thread cannot close.
6. **Schema v7.** `provenance TEXT` (nullable JSON list of `"<8 hex>:<seq>"`). `upsert_draft(provenance=)` keeps an existing value when called without it. Existing `user_version == 6` assertions were updated to 7, as v6 did before.
7. **Flag off is inert.** No ring, counter, control object or thread is created, every hook returns at once, and `_last_turn_done_monotonic` is only stamped when the flag is on.
8. **Back-off.** A skipped or failed run waits 30 s before the next probe (one `Client.ps()` at most), and a skip is logged once per changed reason.
9. **Cursor.** A run that received a complete reply advances the cursor even when it yields no fact or an invalid reply, so a bad reply cannot loop. Only a cancelled or failed run keeps the counter.

## Implementation notes (T4, 2026-10-16)

10. **Repetition check order.** Per candidate: unkeyable (`unkeyed`), then stable-key collision against every stored key, then similarity against the 20 newest injection candidates (`title + content`), then similarity of the answer against Kira's last 6 lines. Any hit increments `repeated`. A fact written in the same run is added to the comparison sets, so duplicates inside one reply write once. The store is read once per run.
11. **Similarity.** `similarity()` normalises like the repetition guard (lower case, accents stripped, edge punctuation trimmed) and uses char-4-shingle Jaccard. Measured: one changed word in a short fact 0.838; in a fact of about 100 characters 0.895. The margin to 0.90 shrinks as facts get longer, so T5 should watch for corrections rejected as repeats.

## Implementation notes (judge round 1, 2026-10-16)

12. **Purge and delete.** `reset_session_topics()` (called by the memoria purge/delete and profile delete routes) also resets the reflection state: the in-flight job is cancelled and its result discarded, the ring and any pending re-arm are cleared.
13. **Counter.** After a completed run the counter is the number of undistilled exchanges still in the ring, not "counter minus batch". An empty batch reconciles the counter and backs off. This supersedes the "reset only when a run completes" wording of section 1.
14. **Stable key is answer-first** (`fact_key_text`). A collision with an unjudged draft of different content is a correction and the draft is revised by the ordinary upsert; a collision with a durable row, or with a draft of near-identical content, counts as `repeated`. This supersedes the key wording of section 7.
15. **Recent-facts comparison** is answer versus row `content` (promotion rewrites titles to three tokens), over the 20 newest injection candidates plus distilled drafts not yet judged (`provenance IS NOT NULL`). This supersedes section 6's `question + answer` comparison for stored facts; the spoken-line check was already answer-only.
16. **Sweep re-arm** is a request flag raised by the worker (also after a cancelled run that wrote something). The engine thread consumes it in `_owner_reflection_tick`, only when no job is active and the idle predicate holds, then clears `_promotion_swept`. The worker never writes that flag.
17. **Watcher** also yields to `llm_generating` (pregen and agenda prefetch), `is_speaking` and a held PTT. The job sets none of them.
18. **Back-off** is exponential (30 s doubling to 600 s), reset by a successful run. A repeated failure reason is logged once.

## Consequences

- No prompt, KV-cache layout, or turn-path change. The job runs off the turn path and only when idle.
- A new nullable memoria column (`provenance`), added by migration, as ADR-062 did for `origin`.
- The owner-only boundary is enforced by the constructor and the prompt-builder signature. A convention is not relied on.
- The runner-release gate (section 5) and the sweep blocking (section 10, decision 1) are the two runtime risks. T5 measures both on the Tauri surface.
- The feature document's "no new raw-text store" is relaxed to "no persisted raw-text store". The RAM ring is bounded and cleared on reset.

## References

- `kimjammer/Neuro memory.py:63-99` (cadence)
- `opencohost/config/settings.py:84` (HISTORY_MAX_TURNS), `:235` (OWNER_BUNDLE_SOURCE), `:1134-1139` (flag pattern)
- `opencohost/core/llm_engine.py:205, 214, 299, 671, 850-878, 1012-1024, 1299, 1345, 1402, 1727-1729, 1809`
- `opencohost/core/engine/llm_engine_memorias.py:69-110, 246-330, 386-486, 539-620, 775-826, 916, 1362`
- `opencohost/core/engine/llm_engine_scout.py:521-560, 590-618, 850`
- `opencohost/core/engine/llm_engine_speech.py:676-691`
- `opencohost/core/engine/llm_inference_service.py:336-375, 539-585`
- `opencohost/core/memory/memoria_store.py:399, 421, 745, 1175, 1275, 1771, 2286`
- `opencohost/core/memory/promotion_parser.py:~165-176`
- `opencohost/core/context/repetition_guard.py:61-106, 120`
- `opencohost/core/context/session_topics.py` (RAM raw-text precedent, off-thread capture)
- ADR-060, ADR-061, ADR-062, ADR-064
- Commit `a8830bb` (client timeout tied to watchdog budget)
