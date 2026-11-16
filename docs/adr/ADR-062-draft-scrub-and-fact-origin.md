# ADR-062: Drafts Stop Carrying the User's Words, and Kept Facts Record Their Origin

**Date:** 2026-09-23
**Status:** Accepted
**Track:** `odd/tasks/memory-semantic-recall.md` (T6 a)
**Related:** ADR-061 (correction supersede), ADR-060 (fact notebook ranking)

## Context

Every capturable turn is stored as a memoria draft that holds the raw exchange
(`contexto: <user> → Kira: <reply>`, up to 300 chars). The promotion judge is a
local LLM that runs off the turn path. It rewrites each draft it keeps into one
third-person sentence of at most 220 chars, and it hides each draft it rejects
(`inactive=1`).

A read-only count on 2026-09-23 found three gaps:

- 200 judge-rejected drafts still held the raw exchange. They were hidden, but
  the words stayed in the store.
- Unjudged drafts were injection candidates. The lexical and meta-recall paths
  could put the user's raw words back into the prompt. That was 279 active
  drafts, 116 of them outside the test-pollution profiles.
- A kept fact had no record of whose fact it was. "The chat says the patch
  ships Friday" could come back as a fact about the operator.

The owner chose option 1: scrub on judgment, exclude unjudged drafts from
injection, and have the judge emit `origin`.

## Decision

1. **A terminal reject scrubs the row.** It happens in the same guarded UPDATE
   that stamps `judged_at` and sets `inactive=1` in
   `MemoriaStore.apply_promotion_batch`:
   - `title`, `content` and `signature` become `''`. The signature is the 12
     significant tokens of the pair.
   - `stable_key` becomes `scrubbed_stable_key(stable_key)`, which is
     `"{profile_id}|scrubbed:" + sha256(stable_key)[:32]`. A plain draft key is
     6 sorted significant tokens of the exchange, so keeping it would keep
     words.
   - `id`, `profile_id`, `status` (`draft`), `revision`, `created_at`,
     `updated_at`, `judged_at` and `inactive` stay as they are. So the row is
     never picked up again by `list_unjudged_drafts`, and prune order is
     unchanged.

   Unresolved decisions (`missing_decision`, `invalid_decision`,
   `duplicate_index`) are retryable. They go through the backoff table and are
   never scrubbed. The legacy `mark_judged(inactive=True)` has no production
   caller and is left unchanged.

2. **Dedup still works.** Before this change, re-capturing a rejected exchange
   reopened the same row for a re-judge (ADR-061 and D3b semantics). That still
   holds. `upsert_draft` and `supersede_draft` first call
   `_restore_scrubbed_key` in the same transaction. It gives the scrubbed row
   back its plain key, so the ON CONFLICT or UPDATE lands on the same id with
   `revision + 1`. The restore is skipped when a live row already owns the plain
   key, which keeps UNIQUE intact. The hash is pseudonymization, not
   encryption: anyone holding the store could confirm a guessed key. The goal
   is to stop keeping the words in clear, and the hash has to be reproducible
   for dedup to work.

3. **Unjudged drafts are never injected.** `list_injection_candidates` now
   also requires `content != ''` and `NOT (status = 'draft' AND judged_at = '')`.
   The lexical, hybrid (ADR-060) and meta-recall (recency) paths all read that
   list, so one filter covers all three. The fact index already skipped
   drafts. The v5 episode line is a separate path and is out of scope here.

   A judged draft is still injectable. That is an *uncertain keep*: the judge
   kept it and rewrote it, but was not sure of a proper noun. Its text is the
   judge's third-person sentence, not raw words. This follows the owner's
   stated target, "exclude unjudged drafts". Making it strictly
   promoted/curated/imported/summary is a one-clause change if the owner wants
   it.

   The panel and API list still show every row, drafts included. Only
   injection changed. A scrubbed row lists with an empty title and loads with
   empty content, which the owner accepted. The UI renders the empty string
   without error, so it was not changed.

4. **The judge emits `origin`.** The value is `self_stated` or
   `shared_content`, and it is stored in a new nullable `origin` column
   (schema v5).
   - **Migration v4 → v5.** An idempotent `ALTER TABLE memorias ADD COLUMN
     origin TEXT CHECK(origin IS NULL OR origin IN (...))` that tolerates a
     duplicate column, then `user_version = 5`. Existing rows get NULL, which
     means "origin not recorded". There is no backfill.
   - **Contract.** `MemoryDecision.origin` is in the prompt, the JSON example
     and the field contract. The JSON schema passed to Ollama `format=` lists
     it as required, so constrained decoding always emits it. The parser still
     tolerates a missing or invalid value: it maps to `None` and never drops
     the decision. `PromotionParseDiagnostics.origins` carries it as a side
     channel, so the existing `(i, text, uncertain, reason)` tuple contract is
     unchanged.
   - **Fail-safe default: `shared_content`.** A keep with a missing or invalid
     origin is stored as `shared_content` and counted as `origin_defaulted` in
     the sweep's reason counts. The two possible errors are not equal.
     Tagging an operator fact as shared content costs a little
     personalization. Presenting someone else's words as a fact about the
     operator is the harm this task exists to prevent.
   - **Handling `shared_content`.** The judge is told to phrase the sentence
     about the source ("A video the operator shared says ..."). Injection also
     adds a deterministic `[shared content] ` prefix to the line (pinned,
     top-k and recency alike), so the fact cannot be read as being about the
     operator even when the phrasing slips. The tag is English and
     prompt-internal, like the `[past chat ...]` episode tag of ADR-059. NULL
     (legacy) rows render untagged, as before. We chose not to reject shared
     content outright, because relayed information can still be worth
     remembering.

## Consequences

- Existing rows are not migrated by default. A one-off, dry-run-first script
  (session scratchpad `scrub_rejected_drafts.py`; `--apply` writes a sqlite
  backup and then scrubs in one transaction) found 200 rejected, unscrubbed
  rows across three profiles on 2026-09-23, with 0 scrubbed-key collisions.
  It has not been applied.
- A rejected draft's text can no longer be inspected in the panel. The owner
  accepted this.
- Right after upgrade, drafts captured tonight are not recalled until the
  sweep promotes them. Promotion runs at the next launch, so within a session
  only durable and judged rows are recalled.
- Tests whose contract this supersedes were updated in place, with a comment
  naming this ADR. The retrieval and routing fixtures now seed `promoted` rows
  by default. The "drafts are not excluded" and "deferred draft is an
  injection candidate" assertions now expect exclusion. The reject test now
  expects a scrub instead of "never rewritten". Schema assertions now expect
  v5 and the `origin` column.
- Open: the v5 episode injection line still carries raw words (follow-up).
  Promoted-fact supersede still needs a judge-emitted `supersedes`
  (ADR-061 follow-up).

## Amendment 2026-09-28 — judge noise rules and pre-promotion dedup

Feature: `odd/tasks/memory-noise-amplification-20260928.md`.

Problem: every draft carries the first contentful sentence of Kira's reply. The judge prompt did not reject facts about the assistant or general knowledge. Its rewrite dropped the `Kira:` label. Dedup was only the exact `stable_key`. Together these let the assistant's self-statements, aphorisms and reworded repeats become durable `promoted` rows. On 2026-09-28, 8 of the 43 retrieval-eligible rows were Kira self-statements.

Decisions:
- **Judge rules.** The prompt now rejects items whose subject is the assistant (identity, role, creator, opinions, recommendations), items supported only by the assistant's line, and general knowledge, definitions and aphorisms. New reject reasons: `assistant_sourced` and `general_knowledge`.
- **Deterministic guard.** A post-parse guard (`_ASSISTANT_SUBJECT_PATTERNS`, `promotion_parser.py`) downgrades a keeper whose grammatical subject is Kira or the assistant. It is defense in depth, because small local judges follow prompt rules loosely. An operator fact with Kira as the object is still kept. An operator opinion about Kira is downgraded by design, as low value.
- **Pre-promotion dedup.** `apply_promotion_batch` checks each keeper against the profile's durable rows (`promoted`, `imported`, `curated`, `summary`) and against earlier keepers in the same sweep. A hit goes through the ordinary scrub/reject path with reason `duplicate`.
  - Semantic path: the optional `semantic_similarity_fn` hook, threshold 0.90. It is not wired yet, because MiniLM runs only inside the async semantic worker.
  - Lexical fallback: conservative containment. A candidate is a duplicate only when its significant tokens are a subset of an existing row's tokens, and neither text carries a negation marker. A Jaccard ratio was rejected because it swallowed updates ("24 años" versus "25 años") and negations. With supersede still deferred, that would freeze the stale fact forever.

Consequences:
- Reworded paraphrases that add or change tokens are still promoted until a synchronous semantic scorer is wired. Duplicates are preferred over lost facts.
- Existing noisy rows are not touched. Cleaning them is a separate owner decision.
- Still open: promoted-fact supersede (contradictions), which the owner deferred on 2026-09-28.

## Amendment 2026-09-29 — MiniLM paraphrase dedup and promoted-fact supersede

Feature: `odd/tasks/memory-paraphrase-contradiction-20260928.md`. This closes the "promoted-fact supersede" follow-up of ADR-061 and ADR-062.

Decisions:
- **No second generative model.** With `OLLAMA_MAX_LOADED_MODELS=1`, a dedicated small judge would evict the chat model. MiniLM-L12 embeddings find candidates, and the already-loaded model decides in the SAME judge call.
- **Calibrated auto-dedup.** A synthetic set of 64 pairs, run through the real MiniLM model (`scripts/calibrate_memoria_dedup_threshold.py`), gave these cosine ranges:

  | Class | Cosine range |
  |---|---|
  | paraphrase | 0.85–0.99 |
  | update | 0.33–0.955 |
  | negation | 0.43–0.86 |
  | unrelated | 0.10–0.48 |

  Embeddings cannot separate a paraphrase from a changed number ("24 vs 25 años" scores 0.955). The auto-dedup threshold is therefore 0.97, which keeps every update. A candidate is a duplicate when its semantic score reaches that threshold OR the lexical containment check matches.
- **Judge actions.** Existing durable facts in the related band [0.40, 0.97), up to 2 per draft, are shown as `EXISTING E1..En`. The judge returns `new`, `duplicate`, or `update` plus a target. If it is unsure, it returns `new`.
- **Deterministic validator** (`promotion_related.resolve_judge_actions`). The action degrades to `new` when:
  - the label is unknown;
  - the target is in another profile, inactive, or has a non-durable status;
  - the candidate is uncertain or not `self_stated`;
  - it is an `update` of a `curated` row.

  A `duplicate` of a curated row is allowed, because it only rejects the draft. The store re-checks the target inside the transaction.
- **Reversible supersede.** Schema v6 adds `superseded_by`. An update inserts the new promoted row and marks the old one `inactive=1` with `superseded_by` set to the new row. Nothing is deleted. `POST /api/memoria/flags` with `inactive=false` restores the old row and clears the link, and the UI shows a "replaced by a newer memory" badge.

Consequences:
- A sweep makes two `embed_batch` calls: one for the drafts, before the judge, and one for the keepers, after it. Both fail open to the lexical path.
- Draft relatedness is embedded on raw exchange text, so the related band is noisy; the judge and the validator absorb that.
- Judge accuracy for `duplicate` and `update` on the locally used models (Qwen3.8, gemma4 e4b and e2b) is not measured yet. Measure it in a live session by grepping the promotion action counts. A dedicated small judge is reconsidered only if that measurement shows poor accuracy.
