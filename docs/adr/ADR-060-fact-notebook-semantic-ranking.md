# ADR-060: Semantic Ranking Over the Fact Notebook

**Date:** 2026-09-23
**Status:** Accepted
**Supersedes:** ADR-034's lexical-only retrieval rule for AUTOMATIC top-k over memorias rows.
ADR-059 said `select_top_k` "stays as it is". It still does whenever semantic ranking is not
available. Pinned rows, the meta-recall recency path and the injection budget do not change.
**Track:** `odd/tasks/memory-semantic-recall.md` (T3)

## Context

The owner asked for semantic retrieval. Before this change, fact rows were chosen by lexical
IDF overlap only (`select_top_k`, at least 2 shared significant tokens). So a paraphrase
("how is your pet called" against "the streamer has a dog named X") found nothing. Memory v5
already runs MiniLM-L12 ONNX in an isolated worker process, and it already has a derived
semantic cache DB.

## Decision

1. **What gets embedded.** Only rows whose status is `promoted`, `curated`, `imported` or
   `summary` and that have `private=0` and `inactive=0` (`fact_index.is_semantic_eligible`).
   Drafts are never embedded: they are unjudged and hold the user's raw words.
2. **Where the vectors live.** In a new `fact_embeddings` table in the existing v5 semantic
   cache DB. Each row is keyed by the fact id and stores `revision`, a sha256 prefix of the
   content, and the model id/version. No text is stored. Profile purge and forget-all drop the
   table's rows along with the episode vectors.
3. **When a vector is valid.** Only while id, revision, content hash and model all still match
   the live row. `update_row` (operator edits, promotion) rewrites content without bumping
   `revision`, so the content hash is what catches those edits. A stale vector is never scored.
4. **Off the hot path.** A turn never embeds a row. `FactSemanticIndex.view()` scores the
   current candidates using valid cached vectors only. When anything is missing, stale or
   orphaned (a deleted, private or inactive row), it schedules a single-flight background
   `reconcile()`. That pass deletes orphaned and stale vectors, then embeds the missing ones
   through the shared worker in batches of 8. Because the cache persists across restarts, only
   changed rows are ever re-embedded. The turn embeds only the query, and it gets whatever is
   left of `MEMORY_RECALL_BUDGET_MS` (400ms) after episodic recall. `SemanticWorkerService`
   now serializes calls: a caller that cannot get the worker within its own timeout fails open.
   Without this, a turn and a background batch sharing the response queue would discard each
   other's replies.
5. **Hybrid rank** (`select_top_k_hybrid`). A non-pinned row qualifies when its query cosine
   is at least `MEMORY_FACT_SEMANTIC_MIN_SCORE` (0.55), or when it passes the lexical gate.
   Its score is `max(cosine, 0.55 if it passes the lexical gate) + 0.10 × lexical/best_lexical`.
   A lexical-only row (for example a draft) therefore ranks like a threshold-level semantic
   hit, and lexical overlap breaks near ties. k=3 and the 700-char budget are unchanged.
6. **Fallback.** With no query vector (`timeout`, `error`, `cold`, `disabled`) or no valid
   vector for any candidate (`cold`), `build_injection_lines` calls `select_top_k` exactly as
   before, so the prompt is byte-identical.
7. **Telemetry.** The ADR-059 line becomes
   `memory_recall: lexical=N semantic=N episodes=N ... fallback=... fact_fallback=<none|disabled|cold|timeout|error|skipped>`.
   `lexical` counts all fact lines, and `semantic` counts the ones the semantic score selected.
   A background pass logs `memory_facts: reconcile profile_id= indexed= dropped= kept= failed= ms=`.
   The line carries numbers, ids and codes only.

## Threshold evidence

This is a read-only replay on 2026-09-23 of 286 real user queries from the owner's trial v5
journal against the owner's eligible fact rows (98, 8 and 25 rows across three profiles).
It printed scores only.

- Query-to-fact cosine over all pairs (background): p50 0.191, p90 0.360, p99 0.524.
- Top-1 cosine per query: p10 0.344, p50 0.481, p90 0.613, p99 0.789.
- Cosine of the facts that lexical `select_top_k` picked: p10 0.171, p50 0.363, p90 0.612.
  Lexical hits are often incidental token overlap, so this is not a clean positive set.
- Queries where lexical found nothing: 154. Their top-1 cosine was p50 0.457 and p90 0.564.
  At the 0.55 floor, 7 of them get a semantic-only fact (3 at 0.60, 15 at 0.50).
- Cost: `embed_query` p50 78ms. A cold batch embed is about 90ms per row (98 rows took 8.9s),
  which is why it runs in the background in batches of 8.

0.55 sits just above the background p99. Precision of those semantic-only hits has not been
measured, because the replay reads no text. T5 (runtime paraphrase test) is the check.

The episodic NONE/IMPLICIT cutoff of 0.78 (`retrieval.py`) is unchanged. On the same replay,
NONE top cosines were p10 0.491, p50 0.614 and p90 0.694 (1 of 101 reached 0.78). EXPLICIT
top cosines were p10 0.553, p50 0.662 and p90 0.748. The two distributions overlap heavily,
so lowering the NONE cutoff would add opportunistic recalls without evidence that they are
relevant. Revisit this only with labelled pairs.

## Consequences

- A paraphrased question can surface a durable fact that lexical overlap misses, inside the
  same budget.
- A newly promoted or edited fact becomes semantically searchable one turn after it is first
  seen, once the background reconcile has run. Until then it is still reachable lexically.
- The first reconcile of a large notebook keeps the worker busy for several seconds. Turns in
  that window log `fact_fallback=timeout` (or `cold`) and stay lexical.
- A profile that is never queried again keeps its orphaned vectors until
  `purge_profile_cache`. Those vectors hold no text.
