# ADR-064: KV-Cache Stable-Prefix Prompt Layout

**Date**: 2026-10-16  
**Status**: Accepted 2026-10-16 by owner, with the three recommended answers (design for T3 of `kv-cache-prompt-layout-20261015`; implementation pending T4 RED tests)  
**Supersedes**: nothing. It refines the block order produced by `PromptContextAssembler.assemble` as described in the feature document.  
**Scope**: `opencohost/core/context/prompt_assembler.py`, `opencohost/core/context/prompt_profile.py`, `opencohost/core/context/context_budget.py` (eviction), `opencohost/core/engine/llm_engine_memorias.py` (history commit), `opencohost/core/engine/prompt_trace.py`, `opencohost/mcp_local/prompt_debug.py`  
**Feature document**: `odd/tasks/kv-cache-prompt-layout-20261015.md`

---

## Context

The baseline (T1, session `opencohost_20261015_225126`, 10 consecutive pairs, Tauri surface) shows that KV reuse is far below target:

- Per-turn `prompt_eval` is 1717-3413 tokens. Prefill is 1167-2238 ms, against a 2.42 s median first-audio.
- Median system-prompt common prefix is 85.6%, but `append_only=false` in all 10 pairs.
- The first differing section is almost always a section tagged *static* in `mcp_local/prompt_debug.py`: `self_knowledge` (5 of 10), `core_rules` (2), `web_instruction` (1), `vision_off` (1). The one dynamic breaker is `already_said` (1).

Two mechanics shape the design.

1. **Ordering is the whole game.** The request is `[system, *history, final user]` (`prompt_assembler.py:550-551`). The system message is the only block that precedes history. Any byte change in it invalidates the cache from that byte onward, which is the entire history. Context parts (`episodic_memory`, `memory_recall`, `digest`, `web`, `screen`) are already appended to the final user message (`prompt_assembler.py:665-701`). They therefore do not break the prefix. They only cost their own tokens.
2. **The model reuses KV only at checkpoints.** The runner is llama-server inside Ollama. It restores a context checkpoint, not an arbitrary prefix, and `LLAMA_ARG_CHECKPOINT_MIN_SPACING_NT=512` sets the checkpoint spacing. Reuse is therefore quantized to checkpoint positions, and any prefix change moves the restore point back.

### Verified claims for this ADR

- The per-turn volatile sections inside the system message are `self_knowledge` (its `ClockProvider` HH:MM and date plus gated topic facts, `core/self_knowledge/service.py:84-150`), `core_rules` (length line depends on the per-turn reply level, `i18n/active.py:818-830`), `reply_length`, the conditional `web_*`, `vision_*`, `no_look` and `vision_off`, `clock`, `active_cards`, `already_said` and `recency_line`. This matches the T2 mapping.
- `self_knowledge` writes the current time twice on owner turns: once as the ambient date/time line, and once in `clock` (`prompt_assembler.py:490`). Both carry HH:MM.
- The stored history is not the sent history. The user turn sent in request N is `context_block + "\n\n" + [user] text` (`prompt_assembler.py:690-696`). The history commit stores the sanitized raw text without the context block (`core/engine/llm_engine_memorias.py:231`). In request N+1 the same turn therefore appears in a different byte form, and the prefix diverges at that message.
- History is evicted from the head by `context_budget.apply_char_budget_pure` (`context_budget.py:223`), called on every turn at `prompt_assembler.py:746`. The deque `maxlen` also caps history at `_max_turns * 2`.
- **`cache_reused` in `[PROMPT_TRACE]` is an estimate, not a measurement.** `prompt_trace.py:126` computes `chars * best_ratio - prompt_eval`, where the ratio is a per-model tokens-per-character best. It is not evidence of reuse.
- **The real reuse number is in the runner log.** `mcp_local/runner_log.py` says that for hybrid/SWA models Ollama's `prompt_eval_count` is the total prompt size, and that the real reuse is visible only in the llama-server runner output (`reused_tokens`, `evaluated_tokens`, `restored context checkpoint ... n_past`). `cache_report` reads this.
- `LLM_KEEP_ALIVE` is `21m` (`config/settings.py:45`).

### Verification item (closed 2026-10-16)

The 0-100 `cache_reused` figure was the `prompt_trace.py` estimate. The real numbers come from the runner log (`mcp_local/runner_log.py`) for session `opencohost_20261015_225126`:

- `reused_tokens`: 525, 0, 973 (six times), 525, 525.
- `prompt_tokens`: 1717-3413.
- Reuse: 31, 0, 37, 44, 42, 36, 32, 30, 16 and 15%, median ~31%. This matches the brief.

Reuse plateaus at checkpoints inside the system prompt, and history is never reused. That confirms the premise of this ADR.

## Decision

### 1. Target layout

The request becomes:

```
[ system ]   byte-stable for the session; changes only on an explicit settings/model/persona change
[ history ]  append-only user/assistant pairs, byte-identical to what was sent (see section 2)
[ final user ] = context preamble (delimited, volatile) + "[user]: " + raw user text
```

**System message (per-session content only).** Allowed: `persona`, `grounding`, `core_rules` in its *stable* form, `owner_policy`, `owner_gender`, `model_label`, `personalization`, `warmth`, `vision_speech`, `vision_rules`, and the stable rule text for reply length and vision-off (section 4). Each block is included only when its per-session condition holds. A toggle change may change the system message once, and it then becomes stable again. The system message must never contain a time, a date, a count, a per-turn level, cards, memory, web or screen content.

**Final user message (per-turn content), in this order, most stable first:**

| Order | Block | Source today | Why this position |
|---|---|---|---|
| 1 | `knowledge` (topic facts, gated per turn, no time) | `self_knowledge` facts | Changes only when the topic gate matches |
| 2 | `active_cards` | `active_cards` | Changes on card edits |
| 3 | `episodic_memory`, `memory_recall`, `digest` | existing context parts | Changes on retrieval |
| 4 | `web` (`web_instruction` or `web_note` plus results) | `web_*` | Changes on lookups |
| 5 | `screen` (`screen_instruction`, `vision_read_note`, `vision_note`, `no_look`, `vision_off`, `vision_capability`) | `vision_*`, `no_look`, `vision_off` | Present only on screen-related turns |
| 6 | `turn_directive` (reply length level, `already_said`, `facts_relevance`) | `reply_length`, `already_said`, `facts_relevance` | Per turn; closest to the question |
| 7 | `now` (date and HH:MM, once) | `clock` plus the ambient date line | Changes every minute; last |
| 8 | `recency_line` | `recency_line` | Closest to the generation point |

The preamble is wrapped in a fixed delimiter, for example `<turn_context>...</turn_context>`, so the model can tell it from the user's words. The delimiter is code-built and localized through `i18n_active`, as the existing `memory_block_open` / `memory_block_close` already are.

Time is deduplicated: `now` is the only place that carries the date and HH:MM. `self_knowledge` stops emitting its ambient time line and keeps only the gender rule and topic facts.

### 2. History byte-equality

**Decision: do not persist the context preamble. Accept one divergence at the previous user turn, and state the reuse bound it causes.**

Options considered:

- **A. Persist the exact sent user message (preamble plus text) in history.** This is rejected. The preamble contains memory recall, web results and screen descriptions. Persisting them grows history and copies non-chat data into long-lived storage. It also breaks the existing rule that history holds sanitized user text. It is only partly rejected for viewer chat: the preamble is not raw chat, but it is data that must not persist. This matches the rule "never expose raw chat in LLM prompts, logs, or persistence": no viewer chat enters persisted history in any form.
- **B. Send past user turns in their stored (raw) form, and keep the preamble only in the final message.** This is the chosen option. Request N+1 then matches request N except at the user turn N, whose sent form differs from its stored form.
- **C. Move the preamble after the final user text in a separate message.** Rejected. It adds a message per turn that is not in history and does not change the divergence point.

**Reuse bound under option B.** Let `P` be the prompt tokens of request N+1, `C` the context preamble plus new user text of request N+1 (never reusable), `T` the tokens of the previous pair that differ (user N, sent with its preamble, plus assistant N), and `S` the checkpoint slack (up to 512 tokens, `LLAMA_ARG_CHECKPOINT_MIN_SPACING_NT`). The reusable part is roughly `P - C - T - S`. With `P` around 2500 (the T1 median), a preamble and new text of about 400 tokens, and a previous pair of about 250, the expected reuse is `(2500 - 400 - 250 - 256) / 2500`, about 64%, with the checkpoint slack at its mean. That is below 70%. **So the 70% target is not reachable by layout alone at this prompt size, and the target must be re-based once the true baseline is known.** The result depends on the size of the preamble, which is the main lever (section 3 and risk 2).

### 3. Eviction policy

Head eviction changes message 0 (or the first kept message) on every turn that evicts. The prefix then restarts from the new head, so reuse for that turn is near zero. With a rolling window that evicts one pair per turn once the budget is full, this happens on every turn after the budget is reached.

**Decision: evict in chunks.** When the history exceeds the budget, drop `K` oldest pairs at once, down to a low-water mark (for example 75% of the budget), so the head changes once every `K` turns.

Trade-off, with a 2500-token prompt and about 250 tokens per pair:

| Chunk `K` | Turns with a full-prefix miss | Average history kept | Memory depth lost per eviction |
|---|---|---|---|
| 1 (current) | every turn after the budget is full | at budget | 1 pair |
| 4 | 1 turn in 4 | about 0.75 of budget | 4 pairs |
| 8 | 1 turn in 8 | about 0.6 of budget | 8 pairs |

Summary boundaries are the better long-term answer but are out of scope here. They rewrite history, which the append-only rule forbids except at a boundary. A later ADR can define that.

The chunk size is an owner question (see below). The default is `K=4` and low-water 75%.

### 4. Instruction-strength risk

Rule: **behavioral rules stay in the system role, stable. Per-turn values move to the final message as a short, fixed-form directive.**

| Instruction | Class | Placement |
|---|---|---|
| Persona, `owner_policy`, `owner_gender`, `model_label`, `personalization`, `warmth` | Behavioral | System, stable |
| Core rules, including "the reply length follows the level in the turn directive" | Behavioral | System, stable text |
| Reply length **level** (short, medium, long) | Per-turn fact | `turn_directive`, a fixed line such as `Reply length this turn: short.` |
| Vision-off rule ("when asked to look and vision is off, say so and point to the button") | Behavioral | System stays as the stable rule; `vision_off` marker on the turn in the directive |
| `already_said` (last 2 openings) | Per-turn fact | `turn_directive`, fixed header, closest to the question |
| `facts_relevance` | Behavioral, but gated per turn | Stays as the stable rule in system; the per-turn gate is one line in the directive |
| Date and time | Fact | `now`, last |
| Topic facts (self-knowledge) | Fact | `knowledge`, in the preamble |
| Guardrail / per-sentence streaming guard | Code, not prompt | Unchanged. It runs in code on the speech path, not from the prompt. Unverified in this pass; T4 must confirm no prompt text is its input. |

Risk: moving the reply level and the vision-off trigger out of the system role may weaken compliance. Mitigations:

- The directive sits in the final user message, closest to generation. Recency is more likely to help compliance than hurt it, but this must be measured.
- Keep the stable rule text in the system role, so the model always has the rule. Only the trigger or value moves.
- T6 (persona and guardrail regression) must run before merge, with the reply-length and vision-off cases as explicit fixtures.
- If a regression appears, move the affected rule back to system as stable text, and keep the value in the directive.

**Compact profile (2B and below, `prompt_profile.py:25`).** Compact turns skip `self_knowledge` and `already_said`, use `COMPACT_OWNER_POLICY` in system, and truncate history at the last model switch. **Decision: compact turns keep the legacy layout in this ADR's first implementation.** The 9B production model is the target. The compact layout is a separate decision, and it must be re-evaluated when a compact model is used in production. The trace reports the layout per turn, so it is measurable.

### 5. Time granularity

- `now` (the final message) carries the date and HH:MM once.
- `self_knowledge` drops its ambient HH:MM and date. Its topic facts for the time topic are rendered as `knowledge` only when the time topic was asked, and then they carry no HH:MM of their own. The `clock` line from `i18n` is the single time source.
- Result: the time appears once per turn, in the last block, which is the only block that changes every minute.

### 6. Logs

- **`[PROMPT_TRACE]`** keeps its existing `key=value` fields and their order. New fields are appended only, so any parser that matches by key keeps working. New fields: `layout=v2|legacy`, `stable_prefix=<hash of the system content>`, `history_pairs=<n>`, `evicted_pairs=<n>`. The `cache_reused` field keeps its current meaning and is documented as an estimate.
- **`[PROMPT_PROFILE]`** (`llm_engine.py:2258-2298`, per T2) keeps its fields. The layout name is added as a field.
- **Section reclassification in `mcp_local/prompt_debug.py`.** `CONTEXT_SECTIONS` already exists (`prompt_debug.py:24-40`). The new `turn_directive`, `now` and `knowledge` sections are added to `CONTEXT_SECTIONS` and `DYNAMIC_SECTIONS`. `self_knowledge` moves from `STATIC_SECTIONS` to the preamble. The static set shrinks to the system-stable blocks. `tests/test_mcp_local_prompt_debug.py:109` pins every section name, so it changes in the same commit as the reclassification.
- **Measuring true reuse.** Use the runner log, not the trace. `reused_tokens / (reused_tokens + evaluated_tokens)` per task, from `runner_log.py`, is the acceptance number. `cache_report` already reports it. The trace estimate may be shown but must not be used as acceptance evidence. T7 reads `cache_report` on the Tauri surface.

### 7. Rollout

- **Kill switch: yes.** `OPENCOHOST_PROMPT_LAYOUT=legacy` restores the current order byte-identically. It is read once in `config/settings.py` next to the other env flags, like `OPENCOHOST_LLM_STREAMING`. The default is `v2` once T7 passes.
- **Small commits**, each with its tests:
  1. Trace and reclassification only: add `layout=legacy` and `stable_prefix`, reclassify sections in `prompt_debug.py`. No behavior change.
  2. Dedupe time: `self_knowledge` drops its ambient HH:MM, `now` becomes the single time source. Output changes, layout does not.
  3. Move per-turn values out of system: `turn_directive`, `knowledge`, `screen`, `web`. Behind the flag.
  4. Preamble wrapper and final-message order (section 1). Behind the flag.
  5. Chunked eviction (section 3). Behind the flag, with its own test.
  6. Flip the default to `v2` after T6 and T7.

### 8. Test plan (feeds T4, RED first)

1. **Stable prefix.** Two consecutive turns with unchanged per-session state produce a byte-identical system message. Asserted on the full string, not only the trace hash.
2. **Volatile blocks only in the final user message.** Time, date, `active_cards`, `already_said`, `web`, `screen`, reply-level directive: none appear in the system message. Each appears exactly once in the final user message, in the documented order.
3. **History append-only without eviction.** Over N turns below budget, the history prefix of request N+1 equals request N's messages, except the previous user message, which equals its stored form. The test asserts the exact divergence index, not "some divergence".
4. **Chunked eviction.** Above budget, the head changes only on eviction turns. Between evictions, the message-0 bytes are identical.
5. **Trace and profile parse.** A `[PROMPT_TRACE]` line produced by the new code parses by the same key regex as the legacy line, and every legacy key is still present with the same type. `[PROMPT_PROFILE]` likewise.
6. **Legacy kill switch.** With `OPENCOHOST_PROMPT_LAYOUT=legacy`, the request is byte-identical to the pre-change request for a fixed input.
7. **No raw persistence.** The persisted history never contains the preamble text.
8. **Persona and guardrail regression (T6).** Existing persona and guardrail suites, plus fixtures for the reply-length and vision-off rules.

## Owner questions

1. **Eviction chunk.** Accept `K=4` with a 75% low-water mark, which means losing about 4 pairs of conversational memory at each eviction, in exchange for one full-prefix miss in four turns instead of every turn? **Recommendation: yes.** It is the only lever that changes the per-turn miss rate, and the memory loss is small compared with the latency.
2. **Target.** Accept re-basing the 70% reuse target on the runner-log measurement once T1 is re-read, instead of holding it fixed? The estimate in section 2 is about 64% at a 2500-token prompt. **Recommendation: yes, and set the acceptance as "at least 70% on the owner's typical prompt, and no TTFA regression", reported with the prompt size.** Layout alone may not clear 70% if the preamble is large.
3. **Compact profile.** Keep compact turns on the legacy layout for this change? **Recommendation: yes.** Compact models are not the production target, and changing their layout adds risk without a measured need.

## Consequences

- Positive: the system message becomes byte-stable, which removes the largest measured prefix breakers. Time appears once. Eviction becomes rare.
- Negative: the reuse ceiling is set by the preamble size and checkpoint slack, not by the layout. Eviction trades some memory depth. Some instruction placements change and need the T6 regression fixtures.
- Neutral: no change to the streaming default, TTS, or model choice (out of scope).

## Could not verify in this pass

- The T1 `cache_reused` figure: whether it came from the estimate or the runner log. This is the first open item.
- `[PROMPT_PROFILE]` line location and fields (`llm_engine.py:2258-2298`) and the test pin at `tests/test_mcp_local_prompt_debug.py:109`. These come from T2 and were not re-read here.
- The per-sentence streaming guard's inputs. It is assumed to be code-side. T4 must confirm it.
- Whether llama-server's checkpoint restore behaves identically when the system message changes at a session boundary (toggle change). Expected to invalidate once, not verified.

## Amendment (Judgment Day round 1, 2026-10-18)

- **Vision reply style is look-scoped (A-C1).** Under v2 the system message carries `VISION_SPEECH_INSTRUCTION_V2` whenever vision is enabled (stable, F2), but the rule applies only when the turn context contains `Screen look this turn.` A non-look turn keeps the `REPLY LENGTH` behavior; a look turn keeps the legacy 2-4 sentence style. Legacy output is unchanged.
- **One system message for every owner source (J1).** The stable v2 rules (`web_rule`, `vision_speech`, `vision_rules`, `screen_rule`, `vision_off`) are gated on `OWNER_POLICY_SOURCES` (`direct`, `ptt`, `owner-bundle`), the same set as `owner_policy` and `reply_length`. Chat and agenda turns do not get them. The per-turn screen and web injection keeps its own gate.
- **Echo guard covers the v2 vocabulary (J2, refined in round 2).** `prompt_echo._INTERNAL_MARKERS` gains `reply length this turn`. The turn-context vocabulary is matched on the raw reply text (`_RAW_MARKER_RE`: the `turn_context` token, its tags, and `turn context` joined only by spaces or tabs), so "Your turn. Context…" is not a leak. Under v2 the echo reference also carries the turn-context, web, screen and vision-style rules. It does not carry the vision-off rule, which scripts what Kira says when vision is off, so a compliant reply would read as an echo. The legacy reference is unchanged.
- **Debug classification (J3).** `session_topics` is a dynamic context section in `mcp_local/prompt_debug.py`, and its pin test reads multi-line appends.
- **Flip prerequisite (B-W4).** Tests that assert legacy-only behavior request the `legacy_prompt_layout` fixture (`tests/conftest.py`), so changing `PROMPT_LAYOUT_DEFAULT` does not turn them red. The default has not been flipped.
