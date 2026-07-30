# ADR-040: Why the Clause-Sanitizer Residuals Became Proposals — a Deferral Taxonomy

**Date**: 2026-07-29
**Status**: Accepted
**Branch**: `codex/ui-ux-audit-proposal-20260709` (uncommitted)
**Author**: Claude Code, following owner instruction that new tracks and concerns route to SDD
proposal rather than to code
**Scope**: planning artifacts only — no source. Governs
`conductor/tracks/log_privacy_hygiene_20260729/`,
`conductor/tracks/sanitizer_language_scope_20260729/`,
`conductor/tracks/residual_cleanup_20260729/`,
`docs/deferred-20260729-clause-sanitizer-scope.md`, and
`conductor/pending-decisions-20260729.md`.

---

## Context

Implementing the ADR-039 tier, plus three parallel audits over the same code, surfaced far more
findings than one unit could absorb: adjacent defects, latent landmines, measurement gaps, stale
tests, and two genuine design questions. Exactly **one** was fixed inside the unit — a race in
the latency instrument, because it was a defect in code written that same session. Everything else
was deferred.

**Deferral is itself a decision, and an unrecorded one rots.** An open proposal with no criterion
attached is a decision the owner must re-read and re-decide every session; that cost compounds
until the proposal is either done or quietly forgotten. The operating mode for this repo is
explicitly *"less expansion, more controlled validation"* and *"do not start new feature tracks
without explicit user approval"* — which makes an accumulating pile of undifferentiated
"laters" the specific failure mode to avoid.

This ADR records **why each class of finding was deferred and what would bring it back**, so the
next session re-decides nothing that was already decided.

---

## Decision

Every deferred item is filed under exactly one of eight reasons, and each reason carries its own
re-entry rule. A finding that fits none of them is not deferrable — it must be done, refused, or
escalated to the owner as a pending decision.

### 1. Deferred because doing it would be a regression

The item looks like a fix and is not. **Re-entry requires a state table**, not an estimate.

*Canonical case:* adding `record_success()` at `kira_agenda_controller.py:966`. It reads as a
one-line fix; traced against source it makes `failure_count` oscillate 0→1→2→0 in CLOSING, so the
force-complete at `:958` (`>= 3`) becomes unreachable and the topic never closes — reproducing the
exact "20+ LLM calls in a row" cascade the `:954-957` comment records as *fixed*. It also breaks a
pinned assertion at `tests/test_kira_orchestration_gaps.py:742`.

**The tell was that it looked too small for what it touched.** That is now a stop condition in the
residual loop: a unit exceeding ~2× its estimate ends the loop rather than getting finished,
because a wrong estimate is evidence the investigation was wrong.

### 2. Deferred because no evidence justifies it

The code lacks a defense on some path, and that is *all* that is known.

**Absence of a guard describes the code; it does not demonstrate a defect.** Re-entry requires a
**logged incident on that specific path** — not a plausible story about one.

*Canonical case:* arming the sanitizer for `chat`/`direct`/`ptt`. I argued the opposite and the
owner corrected me; ADR-039 D5 records the reversal and the concrete false positive that only
exists on the widened paths. The config knob and the per-source telemetry stay precisely so the
evidence can accrue on its own.

### 3. Refused, not deferred — a distinct category that must not decay into "later"

Some items are not waiting for anything. Filing them as deferred invites a future session to
"finally get to them".

*Canonical cases:* intent detection to recognize a requested repetition (regex, phrase list, or an
extra LLM call), and semantic/embedding-based repetition detection in this tier. ADR-039 D2 gives
the reasoning: it would trade a **property** for an unmeasurable accuracy number, and make a
guardrail that decides what the audience hears depend on the operator's GPU.

**Re-entry is not "more evidence" — it is a different design.** If non-exact intra-sentence
repetition ever appears in a real incident, the named first move is extending
`detect_repetition`'s existing threshold-free skeleton comparison
(`repetition_guard.py:98-106`), not adding a model.

### 4. Deferred because the referent does not exist

The item presupposes something the codebase does not contain. **Re-entry requires building the
referent first**, which is a different track.

*Canonical cases:* `system_command` as a dialogue source (zero grep matches; it is a `tipo`
dimension in `_dispatch_command`, and only the `process_context` branch reaches
`_ejecutar_inferencia`). And every STT / transcript-generation concern: OpenCohost performs **no**
speech recognition — it consumes already-transcribed text from an external WhisperLive-style
server, and `api/ptt_session.py`'s own docstring states *"zero audio bytes cross Python."* The
word "transcript" had no referent and was deleted from the plan rather than implemented against.

### 5. Out by explicit owner instruction

Not a judgment call. `opencohost/ui/` (CustomTkinter) is read-only legacy reference for backend
units; the Stream/Tauri connections migration follows this work by the owner's stated order; the
interruption state machine waits on distinguishing four sources first.

**Re-entry is the owner saying so.** No amount of evidence promotes these.

### 6. Deferred because it is out of bounds for the unit that found it

The finding is real and live, but fixing it from inside an unrelated unit would smuggle scope.

*Canonical case:* `ui/voice_control.py:216` logging 30 raw chars of operator PTT speech at INFO,
ungated (`:239`, untruncated at DEBUG). A genuine violation of the raw-text rule — and in `ui/`.
**Re-entry: the next time `opencohost/ui/` is legitimately open.** Filed, not lost.

### 7. Deferred pending measurement

The right answer depends on a number nobody has. **Re-entry is the number.**

*Canonical cases:* threshold tuning (needs `[CLAUSE_SANITIZER]` records from a real agenda
session — no threshold value is itself a measurement); the `gen_seq` full telemetry correlation
(~15 lines, deferred until the current `stage` field proves insufficient); and the
direct/PTT regeneration-vs-fallback policy, which cannot be decided because **NVIDIA NIM has zero
latency measurement of any kind** and Ollama RTX-3060 figures must not be transferred to it.

### 8. Document, do not fix

A real gap whose available fixes are worse than the gap. **Re-entry requires a fix that does not
widen scope**, plus a pinned test proving the current behaviour was intentional.

*Canonical case:* the abbreviation false sentence split (ADR-039 D10). Every candidate heuristic
would also merge legitimate short sentences and push the tier into cross-sentence comparison — the
scope D2 excluded. The gap is a **false negative**, never corruption, which is the tolerable
direction.

---

## Consequences

**Positive.** The three proposals filed on 2026-07-29 each carry a recommendation rather than a
menu, and each item carries its re-entry rule, so the next session's decision surface is small.
`docs/deferred-20260729-clause-sanitizer-scope.md` marks the strongest rows
**BLOCKED-BY-EVIDENCE** — a reader can tell at a glance which items are cheap-but-waiting versus
which are forbidden.

**A cost.** Eight categories is more taxonomy than a small repo usually needs, and it is only
worth its weight if the categories are actually used to *close* things. The
`residual_cleanup_20260729` loop is the counterweight: it exists specifically to drain categories
6, 7 and 8 of their cheap members rather than let the register grow.

**A risk this ADR does not remove.** Categories 2 and 7 both mean "waiting for evidence", and the
evidence in both cases comes from the owner running a real session. If live validation keeps
slipping, both categories silently become category 5 by attrition. The honest mitigation is that
runtime validation is already the repo's stated release gate, not a nice-to-have.

---

## Open questions

- Whether `docs/deferred-*.md` and `conductor/pending-decisions-*.md` should merge. They serve
  different readers today — the deferred doc is per-unit engineering scope, the pending-decisions
  file is owner-facing — but two ledgers can disagree, and this repo has already produced one
  self-contradicting document (ADR-011's status line versus its own update log).
- Whether a deferred item should carry an expiry. A category-7 item with no measurement after N
  sessions is arguably evidence that the measurement is not going to happen.

---

## Related ADRs

- **[ADR-039](./ADR-039-intra-speech-clause-repetition-sanitizer.md)** — the unit whose residuals
  this ADR governs; the referenced decisions D2, D5 and D10 live there.
- **[ADR-011](./ADR-011-cohost-repetition-regenerate-on-duplicate.md)** — the ladder whose
  `record_success()` trap is category 1's canonical case.
- **[ADR-030](./ADR-030-session-decision-journal.md)** — the existing session-journal practice
  this taxonomy sits alongside.

## Update log

- **2026-07-29**: Created alongside the three proposals it governs.
