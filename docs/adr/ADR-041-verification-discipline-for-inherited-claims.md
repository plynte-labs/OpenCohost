# ADR-041: Verification Discipline for Inherited Claims

**Date**: 2026-07-30
**Status**: Accepted
**Branch**: `codex/ui-ux-audit-proposal-20260709`
**Author**: Claude Code, self-review of the 2026-07-29/30 session
**Scope**: process only — no source. Governs how future sessions treat status markers,
counts, coverage claims, and proposal premises **inherited** from documents, prior sessions,
or an earlier draft of the current session, before they are used to gate a decision.

---

## Context

Over one session, **nine** load-bearing claims turned out false. Most were mine. Each had
already been recorded as fact somewhere — a tracked doc, a status marker, a proposal, a
prior diagnosis — before anyone checked it against source, a log, or a measurement. None of
the nine were exotic: a stale gate, a wrong grep count, a `try/except` mistaken for a
liveness check, two wrong diagnoses of one flaky test, a seam shared by two call classes
with different budgets, a skip mistaken for silence, a severity call blind to surface scope,
three proposals with an unverified premise, and an estimate wrong by enough to signal the
investigation itself was wrong.

The common shape: a claim was **inherited** — read from a document, remembered from an
earlier pass, or produced by a quick regex — and then **acted on** before it was checked
against the thing it claimed to describe. This ADR is not about any one of those bugs. It is
about the discipline that would have caught all nine with the same question, asked earlier.

---

## Decision

Adopt nine verification rules, each carrying the instance that produced it. A rule without
its scar is not useful, so none are stated without one.

### R1 — An unexercised-gate claim must cite a log or be treated as stale

`conductor/tracks.md:748` marked the heavy-model recovery gate `[x]`. `CLAUDE.md` and a
2026-07-16 `AGENT_HANDOFF.md` entry said it still needed runtime validation. I ranked the
July sources above the June ones **because they were newer and more direct** — recency as a
proxy for correctness. Wrong proxy: the June entries cited a filename, the July ones cited
nothing. `logs/opencohost_20260617_175453.log` exists and shows `qwopus` hanging, the
watchdog firing at 45.00s, and automatic rollback — the gate had been runtime-validated and
closed for six weeks while `CLAUDE.md` kept blocking every new track on it. **A status
marker with no log or measurement behind it is a stale claim wearing a recent timestamp,
not evidence.**

### R2 — A count that justifies a decision must count the thing being decided

I claimed "37 test sites across 9 files monkeypatch `ollama.show`" and used it as the reason
to reject switching to a timeout-bearing `Client`. Real: **19 patch sites in 2 files**. My
regex was an alternation whose second branch matched the string anywhere — comments,
docstrings, assert messages — and 4 of the 9 files were `tests/realenv/`, which patch
nothing. The wrong number reached three tracked documents as fact before anyone ran it
against the actual call sites.

### R3 — `try/except` proves error handling, never liveness

`_check_capabilities_reasoning` wrapped `ollama.show` in `try/except Exception: return
False` and read as covered. It was not: `ollama-python` builds its default client with
`timeout=None` (its own docstring says so; in httpx that means wait forever), and a blocked
socket raises nothing — there is no exception to catch. A guard that only reacts to raised
errors is silent on a hang, which is the failure mode that matters on a network call.

### R4 — Distinguish order-dependence from timing by measuring, not by re-running

`test_llm_generating_flag_brackets_the_ollama_call` failed in isolation and passed in a full
run. I diagnosed it twice and was wrong both times — first "stale, always fails", then
"order-dependent, another test leaks `is_ready`/`pygame`". Neither survived a check:
`is_ready` is per-instance and `self.pygame` only exists after `run()`, which no test
starts. The real cause was a live unbounded RPC inside the test's 2-second budget — it
passes in isolation three times running on an idle daemon, which is exactly why the second
wrong diagnosis looked confirmed by re-running it. Re-running a flaky test changes its
inputs (daemon load, scheduling) without telling you which input changed; only a timing
measurement separates "depends on order" from "depends on how long the machine took today."

### R5 — Two call classes with different budgets must not share one seam

Routing the metadata probe through `_ollama_chat_with_watchdog` broke three self-heal tests,
and they were right to break: the chat transport gets swapped by the cloud fallback and by
tests, and the probe has neither property. Forced the extraction of `_call_with_watchdog`.
The extraction was forced by a failing test, not chosen for taste — which is the only good
reason to add an abstraction (see ADR-039 D2 on the same principle from the other side).

### R6 — An opt-in test needs something to give it a reason to run

`tests/realenv/test_realenv_reasoning_budget.py` — the only test exercising `MotorVocalIA`
against a real `ollama.show` — had been red since 2026-07-24, and no green suite number
could reveal it, because `tests/realenv/` is gated behind `OPENCOHOST_REALENV_TESTS=1` and a
default run **skips** rather than fails. Worse, it failed looking like a real negative:
`_new_motor` bypassed `__init__`, `_fetch_show` gates on the `_is_local` property reading
`_provider_config`, the missing attribute raised `AttributeError`, and `except Exception:
return False` turned it into "the model lacks the capability." The test landed `a5606de`
(06-29), the gate landed `82eafa5` (07-24), 25 days apart, and a skip was mistaken for a
pass for all of them.

### R7 — Severity does not override surface priority when the surface does not run

Unit R1 (the risk-lens review, distinct from this rule's own number) removed a genuine
viewer-chat privacy leak from `runtime_check()` — dead code, on a surface the owner then
declared out of scope: viewer/Twitch chat works only in CTK today and is unmigrated in
Tauri, another track's job. A real defect, correctly described, fixed on the wrong surface
because severity was checked before scope was.

### R8 — A proposal is a hypothesis with a citation, not a fact

In a 4-unit cleanup loop where the design phase was explicitly instructed to re-verify its
own premises, **three of four units had something wrong in their own proposal** — including
one whose literal instruction (`chars=len(text)` as a new kwarg) would have been a
`TypeError` against the live function's actual signature. Instruction to re-verify was not
itself sufficient; the re-verify step had to run before the diff, not alongside it.

### R9 — A wrong estimate is evidence the investigation was wrong, not a reason to push on

Counting the real seam is what caught R2 mid-fix: the moment the recount landed at 19/2
instead of 37/9, the estimate for the surrounding unit stopped matching its scope, and that
mismatch was the signal to stop and re-derive the blast radius before continuing, not to
finish on the original number. (Same tell recorded independently in ADR-040 §1: a unit
exceeding roughly 2× its estimate ends the loop rather than getting finished.)

---

## The cost test — when this discipline pays and when it does not

Verification is not free, and this ADR is not a license to re-derive everything. It pays on
**claims that gate a decision**: a status marker that blocks or unblocks a track, a count
that picks between two approaches, a premise a diff is built on. It does not pay on claims
that change nothing regardless of their truth value.

One line, applied before spending time on a check:

> **If this claim were false, would I do something different? If no, do not spend on it.**

All nine rules above passed that test before the check was made — each was about to gate a
real decision (reject a design, close a gate, ship a diff, blame a test) and each check was
cheap relative to the decision: a log grep, a corrected regex, a five-minute trace, a timing
run, a failing test already in hand. The test is a floor, not a ritual — it does not mean
re-verifying every stable fact every session.

---

## Consequences

**Positive.** Each rule above is falsifiable against a file, a log, or a test run named in
this ADR — a future agent can re-check the instance, not just trust the rule. The cost test
keeps the discipline from generalizing into stalling on claims nobody is about to act on.

**Negative — stated plainly.** This discipline costs real time: R1 needed a log grep, R2
needed a corrected regex and a recount across two files, R6 needed tracing two gate
mechanisms 25 days apart. Applied indiscriminately it would slow every session down for
claims nobody was going to act on differently either way — which is exactly why the cost
test above is part of the decision, not an afterthought. There is also no automated
enforcement here: nothing stops a future session from re-inheriting a claim without
checking it, the same way this one did nine times. The mitigation is the same as ADR-040's
for its own taxonomy — the rule is only worth its weight if it is actually applied at the
moment a claim is about to gate something, not read once and set aside.

---

## Related ADRs

- **[ADR-011](./ADR-011-cohost-repetition-regenerate-on-duplicate.md)** — the repetition
  ladder whose gate status R1's canonical case concerns.
- **[ADR-014](./ADR-014-model-qualification-and-minibenchmark.md)** — its 2026-07-30
  addendum documents the unbounded `ollama.show` probe that is R3's technical detail.
- **[ADR-039](./ADR-039-intra-speech-clause-repetition-sanitizer.md)** — D2 and D9 record the
  same "evidence forces the abstraction, not taste" reasoning as R5, from the implementation
  side.
- **[ADR-040](./ADR-040-deferral-policy-clause-sanitizer-residuals.md)** — §1's "a unit
  exceeding ~2× its estimate ends the loop" is the same signal as R9, recorded independently.

## Update log

- **2026-07-30**: Created. Nine verification lessons recorded from the 2026-07-29/30
  session, each with its source instance.
