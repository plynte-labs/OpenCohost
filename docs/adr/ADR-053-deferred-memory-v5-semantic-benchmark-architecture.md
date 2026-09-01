# ADR-053 - Deferred Memory v5 Semantic Benchmark Architecture

**Date:** 2026-08-30  
**Status:** Accepted - Deferred Architecture Preserved  
**Decision scope:** Historical preservation and staged resumption only; no implementation authorization

## Decision Summary

Preserve the original broad semantic benchmark design as durable historical evidence, but do not implement it in the current quality track. Split the work into three separately gated stages:

1. **Semantic quality benchmark (current):** compare v4 IDF, MiniLM, and IDF+MiniLM RRF on locked synthetic quality cases with separate calibration.
2. **Semantic runtime qualification (conditional):** open only after the quality benchmark returns `SEMANTIC_PROMISING`.
3. **Production integration (conditional):** open only after runtime qualification passes its own acceptance gates.

Repeated fresh-context gate failures are evidence that the original work-unit boundary was oversized. They are **not** evidence that semantic retrieval, MiniLM, hybrid retrieval, or Memory v5 is invalid.

This ADR records an architecture that future teams may resume. Resumption requires a new SDD track and revalidation against the then-current code, models, dependencies, operating system, packaging toolchain, and product constraints. This ADR does not pre-authorize implementation.

## Context

Memory v5 WU0 established the current v4 contract and selected a semantic retrieval benchmark as the next evidence-gathering route. WU0 remained non-production and was committed as `2914a26`, then archived under `openspec/changes/archive/2026-08-30-memory-v5/`.

The first semantic benchmark design expanded into a broad scientific and product-qualification program. Repeated fresh-context reviews failed because one track attempted to settle five different questions at once:

1. Does semantic retrieval improve lexical retrieval quality?
2. How should dense and hybrid variants calibrate `NO_MEMORY` abstention without leaking evaluation data?
3. Which model and fusion architecture should win a model competition?
4. Can the experiment be scientifically and hermetically reproduced?
5. Can the selected architecture satisfy Windows offline packaging and production-runtime constraints?

These questions are related, but they do not share one evidence boundary or one failure meaning. Combining them made quality evidence contingent on model packaging, native artifact access, environment identity, performance scaling, and production-readiness policy. The result was a design whose enforcement surface grew faster than its ability to answer the immediate question.

## Decision Drivers

- Answer the smallest current question first: whether MiniLM semantics materially improve v4 IDF retrieval quality.
- Keep calibration disjoint from locked evaluation so `NO_MEMORY` behavior is not tuned on the decision set.
- Preserve all broad-design knowledge instead of silently deleting it during the quality-track rewrite.
- Distinguish semantic quality from Windows runtime and packaging viability.
- Avoid interpreting unavailable dependencies or packaging failures as evidence against retrieval quality.
- Preserve local-first privacy, profile isolation, abstention safety, and zero production impact.
- Require each later stage to earn its own SDD authorization and evidence.

## Preserved Original Architecture

The deferred design intended an offline benchmark over a locked 80-case synthetic decision corpus plus a disjoint 20-30-case calibration corpus and synthetic scale pools. It compared five variants on identical candidate pools and queries:

| Key | Variant | Intended role |
|---|---|---|
| A | v4 IDF lexical retrieval | Production-aligned control using the current pure helper rather than a duplicate implementation |
| B | Multilingual MiniLM dense retrieval | First semantic candidate |
| C | Multilingual E5-small dense retrieval | Second semantic candidate with explicit query/passage prefixes, mask-aware mean pooling, L2 normalization, and fixed sequence/runtime settings |
| D | v4 IDF + MiniLM RRF | Hybrid lexical-semantic candidate |
| E | v4 IDF + E5 RRF | Hybrid lexical-semantic candidate |

### Calibration and Freeze

The design separated calibration from evaluation. A disjoint calibration set would derive per-model dense thresholds and hybrid RRF abstention policy through a deterministic search. Calibration inputs and policy would be represented by `CalibrationConfigV1`; selected thresholds and ranking policy would be frozen into `FrozenVariantConfigV1` before the locked decision corpus was opened. Any post-freeze tuning against the decision corpus would invalidate the run.

The intent was to calibrate `NO_MEMORY` abstention independently for each dense model and hybrid rather than impose one universal threshold. RRF candidates were constrained to declared policies, including the baseline `k=60`, weights `1:1`, and a bounded secondary `k=20`, weights `2:1` configuration.

### Artifact, Environment, and Identity Contracts

The broad design proposed these reproducibility contracts:

- `ArtifactManifestV1`: canonical relative paths, byte sizes, and SHA-256 digests for model/tokenizer artifacts, frozen before calibration and verified through an `ArtifactResolver`.
- `CalibrationConfigV1` and `FrozenVariantConfigV1`: canonical hashed configuration before and after calibration.
- `EnvironmentManifestV1`: interpreter, Windows/architecture, resolved distributions, lockfile, and wheelhouse receipt identity.
- A hash-locked benchmark requirements file and isolated `.venv-semantic-benchmark`, installed offline from an approved wheelhouse.
- `RunFingerprintV1`: one aggregate fingerprint binding environment, calibration dataset, decision dataset, ordered variants A-E, artifact manifests, and frozen variant configurations.
- Canonical JSON serialization before hashing so identical inputs produce byte-identical identities.

The design also intended fail-closed offline execution with network denial and explicit offline environment flags. Missing, corrupt, undeclared, dimensionally incompatible, or otherwise unusable artifacts would never silently fall back to another model.

### Quality and Performance Evidence

Quality evaluation covered Recall@1/3, MRR@3, hard-identifier recall, false injection, `NO_MEMORY` precision, near-but-wrong distractors, and profile isolation. The five stale-contradiction cases were diagnostic controls rather than primary quality cases.

Performance and packaging qualification were separate measurements inside the original broad track, although they were combined into the final decision. They included import time, model load/session initialization, first inference, warm latency percentiles, candidate scoring at 50/200/1,000/5,000/10,000 candidates, RSS, working-set delta, thread/CPU bounds, disk footprint, zero-network behavior, and Windows offline wheel/model packaging. Quality metrics were not to be computed from synthetic scale pools.

### Typed Outcomes and Terminal Routing

Each attempted variant was to emit `VariantResultV1` with one of four statuses:

| Status | Meaning |
|---|---|
| `VALID` | Evidence is trustworthy; quality may pass or fail |
| `INVALID` | Scientific identity or input integrity was violated |
| `UNAVAILABLE` | The intended model/runtime could not execute honestly |
| `REJECTED` | Measured performance or packaging crossed a hard-reject boundary |

Reason codes distinguished calibration leak, incomplete config, corrupt datasets, artifact/manifest/path failures, environment mismatch, model/tokenizer load failures, offline violations, tensor/dimension mismatch, inference failure, and performance or packaging hard rejection.

`BenchmarkDecisionV1` then deterministically routed the aggregate evidence to exactly one of four terminal outcomes:

- `IMPLEMENT_HYBRID_INTEGRATION`
- `KEEP_LEXICAL`
- `REJECT_TESTED_SEMANTIC_MODELS`
- `BENCHMARK_INCONCLUSIVE`

Hybrid implementation eligibility was conjunctive, not based on aggregate recall alone: hard-ID preservation, material synonym/paraphrase gain, zero false injection, perfect `NO_MEMORY` and profile isolation behavior, no near-but-wrong regression, and all acceptance-level runtime/packaging gates had to hold.

## What Is Deferred From the Current Quality Track

The current quality track intentionally removes or defers the following broad-design elements:

| Deferred concept | Why it is outside the quality track |
|---|---|
| E5 variants C and E | MiniLM must first produce quality evidence; a second model multiplies calibration and artifact contracts before semantic value is established |
| Five-way model competition | The current question needs only v4 IDF, MiniLM, and IDF+MiniLM RRF |
| Performance scale pools and latency/RSS/CPU/thread gates | These qualify runtime fitness, not retrieval relevance |
| Windows offline packaging, wheelhouse, DLL, and disk-footprint gates | These qualify distributability and deployment, not semantic quality |
| Hermetic environment manifest and executable/distribution identity | Valuable for runtime qualification, but disproportionate to the first quality decision |
| Native secondary-file access auditing and strict artifact resolver enforcement | The gate proved difficult to specify honestly across model libraries; it is a qualification concern, not a prerequisite for measuring quality |
| Aggregate A-E `RunFingerprintV1` | The current three-variant experiment needs a narrower receipt; the aggregate five-variant identity remains preserved for a future bake-off |
| Broad `VariantResultV1` status/reason taxonomy | Runtime, dependency, artifact, and packaging failures belong to runtime qualification |
| Performance/packaging-aware four-way terminal router | The quality track instead routes to `SEMANTIC_PROMISING`, `KEEP_LEXICAL`, or `INCONCLUSIVE` |
| Production integration planning | No runtime qualification evidence exists, so integration design would be premature |

These concepts are deferred, not rejected. Their removal reduces the current SDD boundary without erasing the original intent.

## Staged Architecture

### Stage 1: Semantic Quality Benchmark

The current `memory-v5-semantic-retrieval-benchmark` track compares:

- v4 IDF lexical control;
- MiniLM dense retrieval;
- v4 IDF + MiniLM RRF.

It retains fixed synthetic fixtures, disjoint calibration and freeze, deterministic seeds, model/config hashes, a dependency lock, quality metrics, profile/privacy checks, metadata-only receipts, and zero production changes. Its positive gate is `SEMANTIC_PROMISING`; that gate means only that semantic or hybrid retrieval merits runtime qualification.

### Stage 2: Semantic Runtime Qualification

Candidate track: **`memory-v5-semantic-runtime-qualification`**.

This track may be created only after `SEMANTIC_PROMISING`. It restores runtime-relevant artifact integrity, environment identity, offline execution, Windows packaging, performance scale pools, resource gates, typed runtime outcomes, and qualification routing. Passing quality does not imply passing this stage.

An E5/model bake-off is optional and may occur only after MiniLM quality evidence. Candidate track: **`memory-v5-semantic-model-bakeoff`**. It should be opened only if the expected decision value of a second model justifies the additional artifacts, calibration, and packaging surface.

### Stage 3: Production Integration

Candidate track: **`memory-v5-semantic-production-integration`**.

This track may be created only after runtime qualification passes. It must define production ownership, model lifecycle, caching, failure/fallback behavior, observability, rollout/rollback, schema implications if any, and product-facing acceptance. Neither `SEMANTIC_PROMISING` nor this ADR authorizes production code changes.

## Resumption Gates

Future work may resume deferred architecture only when all applicable gates are met:

- Stage 1 has a completed, reviewable receipt with terminal result `SEMANTIC_PROMISING`.
- Quality evidence preserves hard identifiers, `NO_MEMORY`, near-but-wrong controls, and profile isolation while showing a material semantic gain.
- A new SDD track is created; the current quality track must not be expanded in place.
- The new track revalidates model availability, licenses, hashes, dimensions, tokenizer/preprocessing contracts, dependency versions, Python version, Windows version, and packaging toolchain.
- Runtime budgets are derived from then-current product measurements rather than copied uncritically from the 2026 design.
- Offline and privacy assumptions are tested against then-current loaders and diagnostics.
- E5 is considered only after MiniLM quality evidence and only if another bake-off could change the decision.
- Production integration begins only after runtime qualification has an explicit PASS and a separately accepted integration design.

## Restoration Checklist

Use this checklist when creating future SDD artifacts. It maps deferred knowledge to the place where it must be restored and tested.

| Deferred concept | Future artifact | Required verification |
|---|---|---|
| Windows CPU latency, scale, RSS, working set, CPU, and thread budgets | `memory-v5-semantic-runtime-qualification` spec/design | Tests for percentile math, per-scale receipts, thread caps, hard/intermediate/acceptance boundaries, and repeated-run stability |
| Offline model and tokenizer loading | Runtime qualification spec/design | Network-denial integration test; missing/corrupt artifacts must fail closed without fallback |
| Windows wheelhouse, DLLs, lock hashes, and disk footprint | Runtime qualification environment/packaging plan | Clean offline environment creation test using hash-locked transitive dependencies and package-size receipt |
| `ArtifactManifestV1` and `ArtifactResolver` | Runtime qualification interface contracts | Traversal, symlink/reparse, case collision, undeclared access, missing artifact, and hash/size mismatch tests; re-evaluate feasibility with actual loader APIs |
| `EnvironmentManifestV1` | Runtime qualification receipt schema | Interpreter/distribution/OS/architecture mismatch tests and canonical hash stability |
| Aggregate `RunFingerprintV1` | Runtime qualification or model-bake-off receipt schema | Ordered variant aggregation, unavailable-variant identity, canonical serialization, and mutation-invalidates-fingerprint tests |
| Full `VariantResultV1` taxonomy | Runtime qualification spec and result schema | Exhaustive reason-to-status mapping and unknown-reason rejection tests |
| Performance/packaging-aware four-way routing | Runtime qualification decision policy | Complete mixed-status truth table for `IMPLEMENT_HYBRID_INTEGRATION`, `KEEP_LEXICAL`, `REJECT_TESTED_SEMANTIC_MODELS`, and `BENCHMARK_INCONCLUSIVE` |
| E5 variants C/E and preprocessing | Optional `memory-v5-semantic-model-bakeoff` | Query/passage prefix, mask-aware mean pooling, L2 norm, max length, tensor shape/dimension, per-model calibration, and no-fallback tests |
| Five-variant A-E competition | Optional model-bake-off design/report | Identical pools, independent calibration, stable tie-breaks, and aggregate fingerprint tests |
| Production model lifecycle and retrieval integration | `memory-v5-semantic-production-integration` proposal/spec/design | Strict TDD for lifecycle, cache, profile isolation, abstention, fail-open/fail-closed policy, rollout flag, rollback, and regression coverage |
| Production observability | Production integration design | Metadata-only diagnostics tests; no query, memory text, profile identifier, or raw chat leakage |

## Consequences and Tradeoffs

### Positive

- The current track becomes small enough to answer one falsifiable question.
- Quality failure, runtime unavailability, and packaging rejection retain distinct meanings.
- The broad architecture remains reconstructable instead of disappearing in an SDD rewrite.
- Later tracks inherit explicit gates, candidate names, contracts, and tests.
- No production commitment is inferred from promising experimental quality.

### Negative

- Runtime viability and distributability remain unknown after the quality benchmark.
- A promising MiniLM result requires at least one additional SDD cycle before integration can be considered.
- Some reproducibility machinery will be designed later and must be revalidated rather than copied verbatim.
- Deferring E5 may postpone discovery of a better model, accepted in exchange for a smaller first decision.

## Non-Goals

- This ADR does not select MiniLM, E5, RRF, ONNX Runtime, FastEmbed, or any packaging technology for production.
- It does not approve dependencies, model downloads, schema changes, production code, prompt changes, vector databases, or rollout.
- It does not prove semantic quality, runtime performance, Windows compatibility, offline packaging, or production safety.
- It does not rewrite or supersede WU0 evidence.
- It does not authorize expansion of the active quality track to recover deferred scope.

## Privacy and Production Invariants

- Synthetic fixtures only; no real `memorias.db` text, raw conversation, PII, or raw chat may enter fixtures, reports, logs, OpenSpec, or Engram.
- Diagnostics and receipts remain metadata-only.
- Profile isolation, private/inactive filtering, and `NO_MEMORY` abstention remain mandatory safety gates.
- The current v4 production retrieval remains authoritative until a separately approved production integration track ships.
- No production runtime, schema, dependency, model/provider, prompt, or packaging behavior changes under this ADR.
- No production semantic runtime or Windows packaging test has been performed or claimed by this ADR.

## Traceability

| Evidence | Link / identifier | Relevance |
|---|---|---|
| WU0 implementation commit | `2914a26` | Contract and evidence baseline that selected semantic benchmarking |
| WU0 archive | Engram `#6220`; `openspec/changes/archive/2026-08-30-memory-v5/` | Archived authority and terminal route |
| Refined exploration | Engram `#6222` | Original corpus, variants, privacy, and performance intent |
| Broad proposal | Engram `#6223` | Five-variant benchmark scope and four-way terminal routing |
| Broad specification | Engram `#6224` | Original contractual quality, performance, packaging, and isolation gates |
| Broad design | Engram `#6225` | Preserved manifest, calibration, environment, fingerprint, result, and routing architecture |
| Original tasks | Engram `#6226` | Planned implementation and verification units |
| Planning handoff | Engram `#6227` | WU0 completion and calibration correction context |
| Corrected specification | Engram `#6228` | Disjoint calibration/freeze and corrected quality contracts |
| Gate status | Engram `#6232` | Repeated fresh-context FAIL after simplification |
| Root-cause analysis | Engram `#6234` | Executable ambiguity findings and oversized enforcement surface |
| Split decision | Engram `#6235` | Quality-only direction and conditional later stages |
| Active historical artifacts | `openspec/changes/memory-v5-semantic-retrieval-benchmark/` | Broad proposal, corrected spec, design, and tasks as they existed before rewrite |

## Final Ruling

The broad architecture is preserved as a deferred reference, not endorsed for current implementation. Repeated gate failures diagnosed an oversized work-unit boundary; they did not invalidate semantic retrieval or Memory v5. Future teams may restore any deferred concept only through a new, explicitly scoped SDD track and must revalidate every assumption against the code, models, dependencies, Windows runtime, and packaging toolchain then in force.
