# ADR-019: Thread-Safe Telemetry Collector — Lock-After-Fast-Exit and the Judgment-Day Findings

**Date**: 2026-06-23
**Status**: Implemented (committed) — awaiting owner RF3 runtime validation
**Branch**: `feat/akira-voseo-fix-and-cohost-adr`
**Author**: Claude Code orchestrator + dual blind adversarial review (Judgment Day, 2 sonnet judges)
**Scope**: `opencohost/smart_aggregator/filter_telemetry.py`, `opencohost/smart_aggregator/aggregator.py`, `opencohost/core/llm_engine.py`, `opencohost/ui/app_shell.py`, `opencohost/config/smart_aggregator.yaml` — concurrency hardening of the chat-activation telemetry collector. Part of the measure-first chat-activation diagnostics work (ADR-017 umbrella, ADR-018 seams).

---

## Context

The chat-activation telemetry collector (`FilterTelemetry`) was built measure-first and **off-by-default** to answer a single product question — "why is Kira under-fed at high traffic" — *without* a second instrumentation pass and *without* ever logging raw chat text or usernames. The safety invariant is baked into the record type: `FilterDecision` (`filter_telemetry.py:99-112`) carries `msg_len: int`, closed-enum `msg_category`/`reason_code`/`stage`, and no text/user/message field. It physically cannot carry chat content; out-of-enum reasons are coerced to `UNKNOWN` (`filter_telemetry.py:118-122`). Commit `a8b8067` also DELETED the privacy-unsafe `_log_input_contract_shadow` author+text log and replaced it with the metadata-only `SHOULD_CALL` record path (`aggregator.py:368-385`).

The original collector was deliberately **lock-free**. That was correct under its stated contract: it was called *only on the single chat-source daemon thread*. With one writer, the GIL serializes each record and the unguarded `self._total += 1` / `self._counts[k] = ...+1` read-modify-writes are safe enough for a measurement tool.

ADR-018 broke that assumption. To capture the two starvation signals the in-aggregator seams cannot see — chat items expiring in the motor's priority queue (TTL), and the gap since Kira **actually spoke** — it added cross-module seams that fire from a **second thread**: the MOTOR worker thread. `on_chat_item_expired` is recorded from `_process_priority_queue` (`llm_engine.py:636-641`) and `on_chat_turn_spoken`/`mark_spoken` from `_ejecutar_inferencia` (`llm_engine.py:1563-1567`). Both of those run on the motor worker. Now two concurrent writers — chat-source thread and motor thread — mutate the same counters, and a thread switch landing mid read-modify-write silently loses increments. A measurement tool that under-counts the very expiries it exists to measure is worse than no tool.

This ADR records the concurrency decision that resolves that, and the blind adversarial review (Judgment Day) that hardened it well past the first fix.

---

## Findings & Code Audit

### #A — A second writer thread invalidated the lock-free contract

* **Location**: writer seams at `llm_engine.py:636-641` (TTL expiry) and `llm_engine.py:1563-1567` (spoken clock); collector mutation at `filter_telemetry.py:197-226`.
* **The Issue**: the in-aggregator seams (`aggregator.py:_emit_decision`, `_on_activity_decision`, `_on_activity_trigger`) record from the chat-source thread, while the cross-module seams record from the motor worker thread. The counter mutations in `record()` are not atomic: `self._total += 1` and `self._counts[key] = self._counts.get(key, 0) + 1` are each multiple bytecodes. With two writers and frequent switches, increments are lost.
* **The Decision**: add a `threading.Lock` (`filter_telemetry.py:152`) and guard every mutation under it — but only *after* the disabled fast-exit, so production pays nothing (see #B).

### #B — Lock-after-fast-exit: production stays byte-identical

* **Location**: `filter_telemetry.py:193-197` (`record`), `filter_telemetry.py:173-176` (`mark_spoken`).
* **The Code**:
  ```python
  def record(self, *, stage, ...):
      if not self.enabled:
          return  # hot-path fast exit — no record constructed

      ts = self._clock()
      with self._lock:
          rec = FilterDecision(...)
  ```
* **The Decision**: the `if not self.enabled: return` check runs **before** the lock is ever touched. In production (diagnostics OFF) the method returns on a single bool check — no lock acquire, no allocation — so behavior is byte-identical to the lock-free version. Only an *enabled* diagnostic run pays one cheap, uncontended acquire per record; contention only appears on the rare cross-thread TTL/spoken events. The module docstring now documents this ordering explicitly (`filter_telemetry.py:13-19`).

### #C — Counter loss is real and reproducible

* **Location**: `tests/test_filter_telemetry.py:175-215` (`test_concurrent_record_and_mark_spoken_is_lossless`).
* **The Issue**: the test drives `record()` + `mark_spoken()` from 8 threads × 3 000 iterations (24 000 increments) behind a `threading.Barrier`, and forces frequent switches via `sys.setswitchinterval(1e-6)` (`test_filter_telemetry.py:201`). Without the lock the run lost on the order of ~4 200 of 24 000 increments; with the lock the assertions `roll["total"] == 24000`, `roll["rejected"] == 24000`, and `roll["counts"]["queue_ttl:ttl_expired"] == 24000` hold exactly (`test_filter_telemetry.py:213-215`).

---

## Decisions & Alternatives Considered

### #A: How to make the collector thread-safe

| Option | Summary | Verdict |
|---|---|---|
| **1 — lock-after-fast-exit (chosen)** | `if not self.enabled: return` first, then `with self._lock:` around the mutation | **Selected** — production (disabled) stays lock-free and byte-identical; only enabled runs pay one uncontended acquire per record |
| 2 — always lock | take the lock unconditionally at method entry | Rejected — violates the "zero overhead when disabled" contract; every production hot-path record would pay a lock it never needs |
| 3 — atomic-free / per-thread accumulators | shard counts per thread, merge on read | Rejected — over-engineered for a 2-writer measurement tool; complicates `rollup()` and the gap-deque ordering for no measurable gain |
| 4 — leave lock-free, accept loss | document "approximate counts under load" | Rejected — the tool exists to count starvation events accurately; an under-counting starvation meter is misleading |

**Rationale**: the only writers are the chat-source thread and the motor worker thread; the only reader is the operator (UI thread) calling `get_diagnostics()`. A single `threading.Lock` covers all three. The fast-exit-then-lock ordering keeps the disabled path free, which is the path that ships.

### #B: Where to fire the spoken clock — normal path vs `finally` (resolved dissent)

See the Adversarial Review section, finding 6 — this fork was raised by a judge and resolved *against* the change, by design.

---

## Edge Cases Considered

- **Disabled (production default)**: both `record()` (`filter_telemetry.py:193-194`) and `mark_spoken()` (`filter_telemetry.py:173-174`) fast-exit before the lock. Lock-free, allocation-free, byte-identical to pre-ADR behavior. The motor seams stay `None` (`llm_engine.py:199-200`) because `attach_motor_telemetry_seams` returns early unless diagnostics are enabled (`aggregator.py:206-209`), so the motor's TTL/spoken guards are never even entered in production.
- **Single-writer clocks vs cross-thread reader**: the activation clock advances only on the chat-source thread (`record()` at the `SHOULD_CALL` accept branch, `filter_telemetry.py:213-215`); the spoken clock advances only on the motor thread (`mark_spoken`). Each clock has a single writer, but the operator's `rollup()`/`recent()` read both — which is why the *reader* must also take the lock (finding 1).
- **`list(deque)` GIL-atomicity**: `recent()` snapshots via `list(self._ring)`, which is a single C-level call and rarely tore in practice. The reproducible torn snapshot was `rollup()`'s multi-counter read (`accepted + rejected != total`), not `recent()` — but both now copy under the lock for correctness.
- **No reverse lock ordering / no deadlock**: the only nested acquisition is `_pq_lock` then (after the split) the collector `_lock`, and the split removed even that. Nothing acquires the collector `_lock` and then `_pq_lock`, so no cycle exists.
- **Failing callback never disturbs the queue or speech**: every seam call is wrapped (`llm_engine.py:638-641`, `llm_engine.py:1564-1567`); a telemetry exception is swallowed and the queue/speech path continues.

---

## Adversarial Review — What It Caught (value of the process)

Two blind sonnet judges ran the Judgment Day dual review (Fable was unavailable, so both judges were substituted with sonnet per the model-fallback rule). **Both returned `REQUEST_CHANGES`.** The findings below are the heart of this ADR: the first lock-after-fast-exit pass was correct for *writers* but the judges found it incomplete for readers, lock-order, the disabled contract, and test coverage.

1. **Reader-side torn snapshot (BOTH judges, HIGH)** — `rollup()` and `recent()` read shared state (`_ring`, `_counts`, `_by_stage`, `_total`/`_accepted`/`_rejected`, the gap deques) **without** the lock while a writer mutated them. The operator calling `get_diagnostics()` mid-run (`aggregator.py:128-132`) could observe a torn snapshot where `accepted + rejected != total`, or hit "dictionary/deque changed size during iteration".
   **Fix**: `rollup()` now snapshots everything **under the lock**, then computes percentiles **outside** it to keep the hold short (`filter_telemetry.py:241-260`); `recent()` copies the ring under the lock (`filter_telemetry.py:232-233`).
   **Proven by** a NEW reader-race test, `test_rollup_snapshot_is_consistent_during_concurrent_writes` (`tests/test_filter_telemetry.py:218-268`): 4 writers vs 1 reader under `setswitchinterval(1e-6)`, asserting `roll["accepted"] + roll["rejected"] == roll["total"]` always holds. Without the fix it observed torn reads (e.g. `25 + 23 != 49`).

2. **Lock held across a callout (Judge A, HIGH)** — the TTL callback was originally fired *inside* `with self._pq_lock`, and inside it took the collector lock. That held the queue lock across an external callout and coupled the lock order `_pq_lock → _telemetry._lock`, blocking enqueue paths during the callout.
   **Fix**: a two-block split. Expired chat infos are *collected* inside `_pq_lock` (`llm_engine.py:617-631`), then the callback is fired **OUTSIDE** it (`llm_engine.py:636-641`). This is safe because `_process_priority_queue` runs **only** on the motor worker thread — its callers are `run()` (`llm_engine.py:252`) and the `_complete_processing_cycle` recursion (`llm_engine.py:678-679`, itself called from `run()`'s dispatch path). The other `_pq_lock` holders — `enqueue`/`replace_pending` (`llm_engine.py:383`, `llm_engine.py:400`) and the selection blocks (`llm_engine.py:452`, `llm_engine.py:492`) — only append/pop under the lock from other threads; none re-enter `_process_priority_queue`, so firing the callback after releasing the lock cannot race the queue mutation it just performed.

3. **`mark_spoken()` acquired the lock when disabled (BOTH judges, MEDIUM)** — the first pass guarded `record()` with the fast-exit but `mark_spoken()` still took the lock unconditionally, contradicting the "zero overhead when disabled" contract.
   **Fix**: added `if not self.enabled: return` at the top of `mark_spoken()` (`filter_telemetry.py:173-174`), before any clock read or lock acquire.

4. **Concurrency test never exercised concurrent READERS (BOTH judges, test quality)** — the original loss test (`test_filter_telemetry.py:175-215`) drove only concurrent *writers*. The highest-risk race — a reader tearing against writers — had no coverage, giving false confidence.
   **Fix**: the reader-race test from finding 1 (`test_filter_telemetry.py:218-268`) closes that gap.

5. **Stale docstring (NIT)** — the class docstring still claimed "Lock-free". Corrected: `FilterTelemetry` now documents "Lock-free on the disabled hot path; lock-guarded for every mutating/reading method when enabled" (`filter_telemetry.py:140-142`).

6. **Spoken clock in `finally` — proposed and rejected by design (Judge B, MEDIUM)** — Judge B argued `on_chat_turn_spoken` should fire in a `finally` so a TTS failure still advances the spoken clock. The team **DISAGREED** and recorded the rationale in code (`llm_engine.py:1560-1562`): if `_hablar` raises, **Kira did NOT speak**, so a *growing* `secs_since_last_spoken` is the correct signal — it is precisely what surfaces silent TTS failures to the operator. Firing on failure would corrupt the cadence metric by pretending speech happened. The seam is therefore deliberately placed after `_hablar` returns, **not** in a `finally`. This is a reasoned dissent resolved by design, recorded here because an ADR should preserve disagreements that were closed by decision rather than by code change.

---

## Implementation Notes

- **Files touched**:
  - `filter_telemetry.py` — added `threading.Lock` (`:152`), fast-exit-then-lock in `record()`/`mark_spoken()`, under-lock snapshotting in `rollup()`/`recent()`, corrected docstrings.
  - `aggregator.py` — metadata-only seams (`on_chat_item_expired`, `on_chat_turn_spoken`, `attach_motor_telemetry_seams`) and the `SHOULD_CALL` record path replacing the deleted shadow log.
  - `llm_engine.py` — the two-block TTL split (`:616-641`) and the chat-turn spoken seam (`:1563-1567`); optional callbacks default `None` (`:199-200`).
  - `app_shell.py` — one composition-root line wiring the seams, no-op unless enabled (`:2183`).
  - `smart_aggregator.yaml` — `chat_activation_diagnostics` block (`:55-57`, `enabled: false`) and `live_safety` sampling (`:66-74`).
- **Commits in scope**: `a8b8067` (in-aggregator seams + deletion of the `_log_input_contract_shadow` author+text leak) and `0edbdd7` (cross-module seams + the thread-safety hardening from the judge review).
- **Related prior context (do not re-document)**: `ed54f53` added the chat repetition guard (Kira mode-collapse), covered by ADR-011/ADR-015.
- **TDD notes**: the two concurrency tests are behavioral and self-falsifying — each forces `setswitchinterval(1e-6)` so the *absence* of the guard reliably fails them, not just passes by luck. Test files: `tests/test_filter_telemetry.py`, `tests/test_chat_activation_telemetry.py`, `tests/test_chat_activation_telemetry_cross_module.py`.
- **Test status**: full suite **2609 passed, 2 skipped** after `0edbdd7`.

---

## Consequences

- **Positive**: the collector is now correct under its real two-writer-plus-reader topology. Counts no longer tear or under-count; the operator can call `get_diagnostics()` mid-run without risking a torn snapshot or an iteration error. The disabled production path remains lock-free and byte-identical. The metadata-only privacy invariant is preserved end-to-end (the author+text shadow log is gone).
- **Deferred**: nothing structural. The lock is intentionally a single coarse mutex — adequate for a 2-writer measurement tool; per-thread sharding stays unimplemented until a real contention profile demands it.
- **Runtime validation pending (owner)**: RF3 — enable `chat_activation_diagnostics` against real high-traffic chat and confirm the TTL/spoken seams populate `activation_telemetry` with sane gaps, and that the spoken-gap signal grows (not resets) across an induced TTS failure, validating finding 6's design choice.

---

**Related ADRs**: ADR-017 (umbrella — measure-first chat-activation telemetry, the in-aggregator seams, and the privacy decisions). ADR-018 (cross-module seam mechanism — optional motor callbacks, chat-only gating, lock-ordering split). Prior context: `ed54f53` chat repetition guard (ADR-011 / ADR-015) — referenced, not re-documented here.
