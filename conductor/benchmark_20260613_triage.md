# OpenCohost Production Benchmark Triage — Live 24k-Viewer Run (2026-06-13)

## Executive Summary

A live ~24k-viewer benchmark (12:35–13:27) was investigated by four auditors and every bug-category finding was adversarially verified. The engine is stable under production load (operator morale positive, O9) and the model-tier design behaves exactly as intended (O2/O5/O6 are validation WINS, not bugs).

The triage surfaces **two genuinely confirmed P1 audio-teardown bugs** that jointly explain the operator's most jarring symptom (O8 — music kept playing for minutes after the co-host ended), plus a third confirmed P2 TTS-drain bug. These match the prior engram antecedents #352/#751 (the stale-prefetch clearing exists but was built for sporadic interruptions, not sustained backlog). UI jank (O4) is real: two confirmed P1 thread-blocking sources. The runaway-generation bug (gemma4:e4b producing 21-fragment/97s monologues) is confirmed and is the upstream mechanical driver of the queue pressure.

**Two high-profile findings were REFUTED** and must NOT be presented as real bugs:
- The "20m42s engine deadlock / stream hijacking" (originally P0) — the engine reset `_processing=False` after the empty responses and served the next request in 8.42s at 13:26:49 with no recovery sequence. The gap is **operator absence**, not a hang.
- O7 as a standalone "queue backpressure" bug — the TTL drops were inside the 117s heavy-model recovery window, the items waited <2min (not 10-20min), and the 24k figure appears in no log line. The real driver is the already-tracked heavy-model dead time. The operator's **design intent** (sectioned accumulation) survives as the strongest new-track candidate.

**Note on L1 pipeline-memory digest:** validated only BEHAVIORALLY. The log shows `digest:0` because the injection path logs at DEBUG while this session ran at INFO — not because injection failed. A DEBUG-level re-run is required for technical proof before merging the PR.

---

## Findings by Category

### BUG (confirmed — survived adversarial verification)

| Title | Sev | Evidence | Verification | Track |
|---|---|---|---|---|
| Agenda teardown never calls `audio_bed.stop()` — music outlives mode up to 10 min | P1 | app_shell.py:1166-1186 vs only stop() at :979; grep confirms | confirmed 0.97 | ui_rendering_optimization |
| `on_boundary()` resets idle-drain timer every Kira response (undrainable) | P1 | audio_bed.py:67-73/107-110; `_idle_loop_count` is dead code, live cause is `_last_interaction` | confirmed 0.82 | ui_rendering_optimization |
| `_can_transition_now()` True on None-track → auto-restarts music (#751) | P2 | audio_bed.py:153-157; second vector via unread `_idle_stopped` | confirmed 0.97 | ui_rendering_optimization |
| TTS consumer loop drains buffer; `mixer.music.stop()` never called | P2 | llm_engine.py:1741/1759/1762; grep: zero music.stop() | confirmed 0.93 | ui_rendering_optimization |
| Runaway generation: e4b bypasses num_predict → 21-frag/97s monologues | P1 | log 388-391; llm_engine.py:1031-1033/1273 | confirmed 0.95 | kira_history_summarization |
| `wait_prefetched_agenda(timeout=0.35)` blocks Tk thread 350ms/boundary | P1 | app_shell.py:1382 → llm_engine.py:423 Event.wait | confirmed 0.97 | ui_rendering_optimization |
| Startup-recall sqlite `load_into()` sync on Tk thread before mainloop | P1 | app_shell.py:211; agenda_persistence.py:43 | confirmed 0.90 | ui_rendering_optimization |
| `_on_motor_speaking_end` heavy chain runs sync on Tk thread | P2 | app_shell.py:2448-2478 (deferred entry, inline body) | confirmed 0.82 | ui_rendering_optimization |
| Operator direct messages priority=1 (not highest), droppable under load | P1 | llm_engine.py:268; corrected: harm is pipeline-serialize + queue-drop, not agenda ordering | confirmed 0.72 | kira_history_summarization |
| Empty-response exhaustion returns '' with no watchdog/rollback | P2 | llm_engine.py:1041-1096 vs timeout path 1051-1057 | confirmed 0.72 | heavy_model_inference_recovery |

### EXPECTED (validation wins / documented behavior — not bugs)

| Title | Sev | Evidence | Track |
|---|---|---|---|
| O2: gemma:26b collapse + 45s watchdog rollback (operator-confirmed design) | P3 | log 12:43:26 structured WARNING, clean rollback | runtime_validation_gates |
| O5: qwen3:1.7b repetition loop (small models explode) | P3 | log 12:46:55-12:47:48 identical templates | opencohost_launch_readiness |
| O6: gemma4:e4b confirmed ideal target model | P3 | 50+ coherent turns, 1.3-14.4s latency | opencohost_launch_readiness |
| O9: 24k-viewer session — core pipeline stable | P3 | operator O9; no fatal.log events | runtime_validation_gates |
| O3 window quantified: ~2m3s, ~78% inherent, avoidable = cold rollback reload | P2 | log 25.79s load + 45s watchdog + 44s e4b cold reload | heavy_model_inference_recovery |
| Qwen3-TTS qwen_unknown fallback (documented, already PASS) | P3 | health_monitor.py:639-642; validation_log:165 | opencohost_launch_readiness |

### REFUTED (do NOT present as real bugs)

| Title | Sev | Why refuted | Verification |
|---|---|---|---|
| 20m42s "engine deadlock / stream hijacking" | P3 | Engine reset _processing=False, served next request in 8.42s; gap = operator absence | refuted (0.82-0.88, 4 verifiers) |
| O7 standalone queue-backpressure bug | P3 | TTL drops inside 117s heavy-model recovery; <2min waits; no 24k in log | refuted 0.88 |
| O8 prefetch-drain (3-4 item backlog) | P3 | Prefetch buffer is depth-1 epoch-guarded; cited timestamp misread (line 313 = 12:55:06, not 13:13) | refuted 0.90 |
| Qwen TTS as needs-investigation | P3 | Already documented + marked PASS; latency misattributed | refuted 0.92 |

### NEEDS-INVESTIGATION (low priority / observability)

| Title | Sev | Note | Track |
|---|---|---|---|
| Engine-thread heartbeat (disambiguate idle vs hung) | P3 | Diagnostic only — refuted deadlock | opencohost_launch_readiness |
| Agenda state-machine resume-after-stop (drop_pending_sources) | P3 | Real residual from refuted O8 prefetch theory | ui_rendering_optimization |
| Empty-response at 13:05:00 as cluster leading indicator | P2 | num_ctx already bypassed for gemma; no trim to add | heavy_model_inference_recovery |
| Phase 4 DPI/canvas snapping unstarted (no canvas calls exist) | P3 | Future/additive work, not a patch | ui_rendering_optimization |
| L1 digest DEBUG re-run for technical proof | P2 | digest:0 is a DEBUG-vs-INFO artifact, not a failure | pipeline_memory_followups |

### CONFIG-IDEA / NEW-TRACK

| Title | Sev | Note | Track |
|---|---|---|---|
| Bounded-queue sectioned-accumulation config toggle (operator intent) | P2 | immediate vs sectioned mode; do NOT touch PTT-accumulate; needs user approval | viewer_queue_backpressure (NEW) |

---

## Recommended Next Actions (priority-ordered)

1. **[P1] Fix the audio teardown pair (O8 root cause).** In `ui_rendering_optimization_20260609`: (a) call `audio_bed.stop()` from `_kira_agenda_emergency_stop()` and a deferred graceful stop from `_kira_agenda_soft_stop()`; (b) decouple `_mark_interaction()` from `on_boundary()` so the idle-drain can reach its threshold under sustained load. These two together close the "music kept playing for minutes" symptom directly. Also harden `_can_transition_now()` to return False on a None track.

2. **[P1] Fix the TTS interrupt path (P2 but same teardown area).** Add `_speaking` guard in the consumer loop, call `pygame.mixer.music.stop()` before `unload()`, and have `emergency_stop` set `motor_ia._speaking=False`. There is currently NO `music.stop()` anywhere in the codebase.

3. **[P1] Eliminate the two confirmed UI freezes (O4).** Change `wait_prefetched_agenda` to a non-blocking `timeout=0` poll with `_safe_after` retry; move the startup-recall `load_into()` to a daemon thread posting back via `after(0, ...)`. These remove the 350ms-per-boundary block and the up-to-500ms startup freeze.

4. **[P1] Cap runaway generation + fix operator priority.** In `kira_history_summarization_20260611`: add `max_response_tokens`/`max_fragment_count` for chat/direct (the e4b num_predict bypass is the upstream cause of the queue pressure that O7 misdiagnosed), and elevate operator/PTT direct messages to `priority=0` (one-integer change). Do this BEFORE any queue-backpressure work.

5. **[P1] Heavy-model recovery hardening (release gate).** In `heavy_model_inference_recovery_20260609`: add a VRAM-fit pre-check before dispatching to a just-loaded heavy model (block/warn when predicted VRAM > total*0.75), add an empty-response watchdog that triggers the same rollback as timeout, keep the rollback model warm to avoid the cold reload, and split the misleading prep metric into eviction_wait + actual_load. Then run the user-mandated real heavy/stalling-model runtime validation.

6. **[P2] Re-run the L1 digest session at DEBUG level** to capture the injection line and confirm `digest>0` technically before merging `feat/pipeline-memory`. Until then L1 is behaviorally-validated only.

7. **[P2] (Requires user approval — respects "less expansion" mandate) Propose `viewer_queue_backpressure_20260613`** implementing the operator's sectioned-accumulation config toggle. Gated behind the priority=0 fix. This is the only justified NEW track; the overflow symptom itself was refuted as a standalone bug.

8. **[P3] Low-urgency observability/cleanup:** add a 30s engine-thread heartbeat (to disambiguate future idle-vs-hung gaps), investigate the agenda resume-after-stop ordering, and defer Phase 4 DPI snapping.

### Validation wins to record (no code change)

- Close Gate 1 in `runtime_validation_gates_20260610/validation_log.md` (O2 watchdog rollback confirmed under production load).
- Record gemma4:e4b as the OpenCohost 0.1 production baseline (O6).
- Add a "Gate 5: Production Load Stress" entry for the 24k session (O9).
- Mark qwen3:1.7b test-only / not live-eligible in the model registry (O5).