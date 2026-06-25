# ADR-012: Editorial Card Matcher Recall, Single-Use Lifecycle, and Input-Sanitizer Precision

**Date**: 2026-06-21
**Status**: Proposed — decisions made, not yet implemented
**Branch**: `feat/akira-voseo-fix-and-cohost-adr`
**Author**: Claude Code orchestrator + owner ideation (open-question round)
**Scope (future implementation)**: `opencohost/core/editorial_matching.py` (scoring), `opencohost/smart_aggregator/kira_agenda_controller.py` (`CODE_PATTERNS` / `sanitize_topic_text`). Documented-only (no change): `opencohost/core/editorial_agenda_bridge.py`, `opencohost/core/editorial_cards.py` (card lifecycle). New tracks: `editorial_matcher_recall`, `input_sanitizer_gaming_words`, parked `sessions_recurrent_themes`. **Sibling of [ADR-011](./ADR-011-cohost-repetition-regenerate-on-duplicate.md).**

---

## Context

The same 2026-06-21 cohost stress test that produced ADR-011 (20 editorial cards, 10 agenda topics, randomized chat + 10 viewer requests, gemma4:e2b) surfaced three findings unrelated to repetition. ADR-011 deferred them here. They were then shaped with the owner via an open-question round; this ADR records the resulting decisions.

Stress-test facts relevant here:
- **Match precision was perfect**: 0/18 false card matches. The `≥0.8` `select_card` gate plus short-title/ambiguity guards never injected a wrong card.
- **Recall had holes**: a plural viewer query *"gaming chairs"* scored 0.40 and missed the armed *"gaming chair"* card (no stemming).
- **The input sanitizer rejected a legitimate topic title**: *"RTX 5070 price drop"* (had to be reworded to *"price cut"*).

---

## Decisions

### D1 — Matcher recall: stemming/lemmatization as a SCORE BOOST, never an exact-match override

Add inflection normalization so plural/inflected forms still match (e.g. *"chairs" → "chair"*), but apply it **only as a contribution to the match score**, not as a hard exact-match. Exact triggers keep ruling; stemming just lifts borderline inflected forms toward the gate.

**Why**: the matcher's high-precision bias is **correct for a co-host** — a *wrong* card makes Kira confidently assert irrelevant facts, whereas a *missed* card just makes her improvise (which she does acceptably; 83% card-use when matched). So the fix must improve recall **without** loosening precision. A soft score boost does that; a hard match would risk false positives.

**Deferred to design**: the exact boost weight, capped so a stemmed near-match can never push an *unrelated* card over the `0.8` gate. (Owner: "evitar falsos positivos.")

### D2 — Single-use lifecycle: KEEP as-is (it is intentional, and recurrence is already handled)

The stress test's "single-use exhaustion" is **not a flaw** — it is deliberate, and the design already handles recurrence on a second path. Verified in code:

- **CHAT/direct path is NON-consuming**: `resolve_direct_context` (editorial_agenda_bridge.py) explicitly *"does NOT activate, mark used, or change card status — the card stays ARMED."* A recurring chat question **re-fires the same card** every time.
- **AGENDA path is single-use per topic**: `auto_attach → link_card_to_topic → activate_for_topic` (ACTIVE), then `mark_used_after_successful_generation` (USED). One curated take per topic = deliberate **freshness / anti-repetition** (directly aligned with ADR-011).
- **Cards are never deleted**: USED status is preserved with `use_count` / `last_used_at`; `editorial_cli` has a `rearm` command (USED/EXPIRED → ARMED) and refuses to disable a USED card ("history is preserved").

**Decision**: keep it. Owner-confirmed.

### D3 — Input sanitizer: keep the SQL-injection/code intent, switch to CONTEXT-AWARE detection

`KiraAgendaController.sanitize_topic_text` rejects topic titles/angles/constraints (and profile styles, bulk imports, and **TopicSuggester output from viewer chat**) that "look like code or markup," matched against `CODE_PATTERNS` (kira_agenda_controller.py:305). The intent — block SQL injection / code / markup from reaching the prompt — is correct and stays. But the **bare-keyword pattern is over-broad**:

```
function | class | import | from | select | insert | update | delete | drop | script | console.log
```

- **`from`** is a top-10 English word — *"Clips from the tournament"* would be rejected.
- **`update`** (*"game update"*), **`select`**, **`drop`** (*"price drop"*, *"frame drop"*, *"drop rate"*), **`class`** (*"S-class character"*) are common gaming/streaming words.

A bare word is not an injection. The **structural patterns do the real anti-injection work**: triple-backtick fences, HTML tags (`<\/?[a-z][^>]*>`), brace-soup (`[{};]{3,}`), and arrows (`=>`). Those stay.

**Decision (owner)**: flag a code keyword **only with adjacent code syntax** (e.g. `DROP TABLE`, `from x import`), never as a standalone word. This removes the false positives while keeping injection protection real — important because the same gate sanitizes **viewer-sourced** TopicSuggester input.

### D4 — Sessions / Recurrent-Themes layer: PARKED (separate future feature, low certainty)

The one genuine cross-path gap — once the AGENDA consumes a card (ACTIVE/USED), it leaves `list_armed()` and becomes invisible to a later CHAT question on the same topic — is **not a matcher fix**. It points to a distinct concern: **session-level memory** of which themes/cards have been covered, and how recurrence is coordinated across the agenda and chat paths. Owner: *"posiblemente un sistema aparte como `sessions` o `recurrent_themes` — no estoy seguro."* Parked until the concept firms up; explicitly out of scope for the matcher work.

---

## Edge Cases Considered

- **Stemming over-matching**: aggressive lemmatization could merge unrelated words or push an unrelated card over the gate → the boost is capped and never forces a match (D1). This is the deferred tuning.
- **Sanitizer context detection**: must still catch real multi-token injection (`'; DROP TABLE users; --`, `from os import system`) — context-aware ≠ permissive. Viewer chat (TopicSuggester) is the higher-risk source and stays protected.
- **Single-use vs ADR-011**: making cards reusable for recall would *reintroduce* repetition (Kira repeating the same curated take), fighting ADR-011. This is why D2 keeps single-use and routes recurrence to the parked D4 layer instead.

---

## Investigation Notes

- Card lifecycle verified directly in `editorial_agenda_bridge.py` (non-consuming `resolve_direct_context` vs consuming `auto_attach`/`mark_used_after_successful_generation`) and `editorial_cli.py` (`rearm`, USED-history-preserved).
- `CODE_PATTERNS` read at `kira_agenda_controller.py:305`; sanitizer entry points: `sanitize_topic_text` (titles/angles/constraints/profile styles) and `BULK_CODE_PATTERNS` (bulk import, `cohost_agenda_panel.py:24`).
- Matcher precision/recall numbers come from the stress-test grounding analysis (0/18 false matches; the 0.40 plural miss).

---

## Consequences

- **Positive**: two safe, contained improvements (soft-boost stemming; context-aware sanitizer) that raise recall and remove false positives **without** weakening the matcher's precision or the injection guard. The single-use design is confirmed correct and left untouched. The harder recurrence question is correctly isolated as its own (parked) concern rather than bolted onto the matcher.
- **Low blast radius**: D1 and D3 are localized scoring/pattern changes; D2 is a no-op (documentation of intended behavior); D4 is not built.
- **Deferred / open**: the stemming boost weight (D1 tuning) and the entire Sessions/Recurrent-Themes layer (D4).
- **Operating mode**: all items are PROPOSAL/parked — no implementation authorized; consistent with "validation, less expansion."

---

## Related ADRs

- **[ADR-011](./ADR-011-cohost-repetition-regenerate-on-duplicate.md)** — cohost repetition handling. The single-use freshness rationale (D2) and the model-vs-card root-cause finding live there; this ADR covers the sibling matcher/sanitizer/sessions findings it deferred.
