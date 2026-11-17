# ADR-059: Episodic Recall Merged Into the Memorias Block

**Date:** 2026-09-23
**Status:** Accepted
**Supersedes:** ADR-034, for the prompt-assembly path only (what gets injected and how it is
budgeted). Lexical `select_top_k` over memorias rows stays as it is. Semantic ranking over
fact rows is T3 of the same track.
**Track:** `odd/tasks/memory-semantic-recall.md` (T2)
**Driver:** The owner asked for semantic retrieval on 2026-09-23.

## Context

Kira has two memory layers:

- **Facts:** lexical memorias rows, selected by `select_top_k` or `build_recency_lines` under
  `MEMORIAS_MAX_INJECT_CHARS=700`.
- **Episodes:** Memory v5 episodic recall, MiniLM in a worker process, reached through
  `MemorySubsystem.recall_decision`.

The engine arbitrated between them all-or-nothing. A confident v5 `EPISODIC_TOPIC` or
`SESSION_RECALL` hit suppressed the lexical block and injected the full `<episodic_memory>`
block instead. That block has a ~290-char preamble plus raw exchanges, a 1200-token cap, and a
~417-char median message. The design had three defects:

1. **Facts were lost on a hit.** The lexical block was dropped whenever an episode won.
2. **The budget was blown on a hit.** One exchange alone usually exceeds 700 chars, and small
   models get confused by too much memory.
3. **Nothing was observable.** In the owner's session (`logs/opencohost_20260923_114206.log`),
   both turns went lexical and no log line said why.

A read-only replay of 286 real user queries against the owner's trial DB (leave-own-episode-out,
warm worker) measured the following:

- **Latency:** `embed_query` p50=78ms, p95=94ms. The whole recall took p50=172ms, p95=250ms,
  max=344ms.
- **NONE intent:** 98 of 108 ordinary-chat queries were rejected with `REJECTED_LOW_SEMANTIC`
  (IMPLICIT/NONE cutoff: cosine ≥ 0.78).
- **Latent bug:** 11 of 286 raised `AttributeError` in the coordinator's single-exchange trim
  path. It rebuilt a `ConversationalExchange` with a field that does not exist, so recall
  failed open.

## Decision

1. **One block, one budget.** Recalled episodes are merged into the memorias block. Fact lines
   come first, unchanged. At most `MEMORY_RECALL_MAX_EPISODES=2` compact episode lines follow:
   `[past chat YYYY-MM-DD] <user words> → Kira: <reply>`, at most 220 chars each (the pinned
   clip). The whole block stays within `MEMORIAS_MAX_INJECT_CHARS`. Episodes are trimmed, or
   skipped below 80 chars. Facts are never cut to make room. Pinning is unchanged. The
   standalone `<episodic_memory>` block is no longer injected on this path.
2. **Latency budget.** `MEMORY_RECALL_BUDGET_MS` defaults to 400 and can be overridden with
   `OPENCOHOST_MEMORY_RECALL_BUDGET_MS`. The number comes from the replay above: 150ms would
   have discarded most recalls, and 400ms is under 4% of a warm 10-20s TTFA. The budget caps
   the worker IPC wait through the existing `SemanticWorkerService` timeout, so no new threads
   are added. A recall whose total wall time still exceeds the budget is discarded. *(Superseded 2026-10-20, see the note at the end.)*
3. **Fail to lexical-only, byte-identical.** The following cases leave the prompt exactly as the
   lexical-only path builds it, because the builder is called with its legacy signature:
   - worker timeout;
   - an overrun budget;
   - an exception;
   - no hit;
   - v5 OFF, SHADOW, or no subsystem.
4. **Telemetry.** Each attempt logs one INFO line on the `OpenCohost` logger, with numbers and
   codes only:
   `memory_recall: lexical=N episodes=N decision=<REASON> scope=<SCOPE> top_score=<cos|none> ms=N fallback=<none|timeout|error|disabled|no_hit|no_room>`.
   `top_score` is the best query-to-exchange cosine, so the 0.78 cutoff can be judged against
   real data. `no_room` means a hit that did not fit after the facts. Exception logging in
   `recall_decision` now records the type only.
5. **Bug fix.** The coordinator's trim path uses `dataclasses.replace`.

## Consequences

- An episode hit adds to the facts and never replaces them. Memory stays within 700 chars.
- `MEMORIAS_ENABLED=False` now also hides episodes, because both layers share one block.
  Per-layer toggles are T4.
- The confidence thresholds are unchanged. The H4 receipt has `theta_low == theta_high`
  (0.27648), so offline lexical corroboration is unreachable. Recalibration is a follow-up, to
  be informed by `top_score` telemetry.
- Two arbitration tests that encoded suppression were rewritten to the merge contract
  (`tests/test_memory_v5_shadow/test_lifecycle_recall_correction.py`, `test_episodic_recall_seam.py`
  seam 19). The new contract is pinned by `tests/test_memory_recall_merge.py`.

## Update 2026-10-20 (memory-write-quality T3)

The budget caps the embed IPC wait only (`WORKER_UNAVAILABLE` maps to `fallback=timeout`). It is no
longer a post-hoc discard: a recall that finished and hit is used regardless of its wall time,
because the latency is already paid and a live session threw away an accepted recall this way
(11 of 15 turns were `fallback=timeout`). The `memory_recall:` line now ends its base fields with
`over_budget=0|1` (1 when `ms` exceeds the budget), telemetry only. The earlier text above is kept
as the dated record.
