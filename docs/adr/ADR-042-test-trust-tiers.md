# ADR-042: Test Trust Tiers — what a green suite actually proves

**Date**: 2026-07-30
**Status**: Accepted
**Branch**: `codex/ui-ux-audit-proposal-20260709`
**Author**: Claude Code, three parallel audits (API / LLM engine / agenda) plus independent verification
**Scope**: process + measurement. No source changed. Governs how a suite result is read before it
is used to gate a merge, a release, or a decision to skip running tests at all.

---

## Context

The owner asked a question worth taking literally: *which tests are real and which are mocked, and
why should I not trust all of them?* The motivating suspicion was that a large green number is
partly theatre — that some tests are "hardcoded" and pass regardless of production behavior.

The suspicion had a real precedent, from this same repo, eight days old at the time of writing:
`tests/realenv/test_realenv_reasoning_budget.py` — the only test exercising `MotorVocalIA` against
a real `ollama.show` — was **red for 25 days** underneath a green suite number, because
`tests/realenv/` is opt-in behind `OPENCOHOST_REALENV_TESTS=1` and a default run *skips* rather
than fails. It also failed **looking like a real negative**: a missing attribute raised
`AttributeError`, and `except Exception: return False` rendered it as "the model lacks the
capability." Fixed in `e461503`.

So the question is not paranoid. But the audit's answer turned out to be different from the
question's premise, and the difference is the point of this ADR.

---

## Decision

### 1. Four tiers, and the reframe

Classify every test into exactly one tier. Two tiers are healthy, two are not.

| Tier | Definition | What green means |
|---|---|---|
| **T1 — Real contract** | Real production path **and** real external dependency (real file I/O, real subprocess, real service). | The integration actually works. |
| **T2 — Real logic, stubbed boundary** | The unit under test runs for real; only the external boundary is faked. | The logic is right. **Says nothing about the boundary.** |
| **T3 — Self-mocked** | The test substitutes the behavior it claims to assert, or asserts a value it injected. | Nothing. Green regardless of production code. |
| **T4 — Skip-gated** | Opt-in behind an env var. A default run reports *skipped*, not *failed*. | Nothing — **it cannot fail the suite**, so it can rot silently. |

**The reframe, which is the main finding: T2 is not a defect.** Nearly every good unit test mocks
something. A suite where most tests are T2 is a *healthy* suite, not a compromised one. The premise
"many tests are hardcoded, so the number is inflated" did not survive measurement. What is true is
narrower and more actionable: T2 green tells you nothing about the boundary, and the boundary is
where this project's real risk lives.

### 2. Measured results

Three surfaces audited independently, each recounted per ADR-041 R2 (a prior count in this repo
reached three tracked documents as "37 across 9 files" when the truth was 19 in 2, because a regex
alternation matched comments and assert strings).

| Surface | Tests | T1 | T2 | T3 | T4 |
|---|---:|---:|---:|---:|---:|
| API (`tests/test_api_*.py`, excl. agenda) | 483 | 2 | 454 | **0** | 0 |
| LLM engine (35 files) | 605 | 13 | 586 | **0** | 6 |
| Agenda (11 files) | 304 | 23 | 281 | **0** | 0 |

The API figure of 483 was verified independently by a direct run, not taken from the audit.

**The three counts are not the same unit, and that matters.** API and engine count *collected
items* (parametrize cases expanded); agenda counts *test functions* — its collect-only reports 348
against 304 functions. Do not add the three columns and present the total as one measurement
without saying which unit it is in. The tier proportions are unaffected either way.

**T3 = 0 on all three surfaces.** The thing the owner was worried about does not exist here. The
audits specifically hunted the pattern most likely to hide it — a test that seeds a cache or sets a
fake's return value and then asserts that same value back — and every instance resolved to
asserting a *different, independently derived* value alongside the seed, or asserting that the seed
was **not** clobbered.

### 3. The real risk is T4, and it is small and concentrated

Every test in this repo that touches a real external dependency:

| Gate | Env var | Tests | Touches |
|---|---|---:|---|
| `tests/realenv/` | `OPENCOHOST_REALENV_TESTS=1` | 7 | a real Ollama process |
| `tests/live_cloud/` | `OPENCOHOST_LIVE_CLOUD_TESTS=1` | 3 | a real cloud provider |

**Ten tests.** All skipped by default, all invisible in the summary line — a skip and a
not-applicable look identical. Against a full suite of ~4900, the entire real-dependency contract
is **0.2%**, and it only runs when a human remembers an env var.

Every other skip in the repo is a **platform** gate (`os.name != "nt"`), which on the owner's
Windows machine actually runs. The silent-rot class is exactly these two directories and nothing
else.

### 4. A green run is not a stable fact — the API scope is nondeterministic

Measured, not inferred. Same commit, same command, three runs of the 30-file API scope:

| Run | Result |
|---|---|
| 1 | 17 failed, 466 passed |
| 2 | 483 passed |
| 3 (independent, mine) | 483 passed |

Bisecting the failing files in isolation: 99/99 passed. The failures cluster on the tests doing
real file, thread and lock work (i18n locale state, `MemoriaStore` sqlite construction, an
observability lock-hold test, `perfiles_crud` locale persistence) — the same property that makes
them legitimately *more* real is what makes them load-sensitive when 483 tests share one process.

**Consequence: a single red run of the full API scope is not evidence of a regression** when the
failures land in that cluster. Rerun before believing it. Symmetrically, this is why ADR-041 R4
insists on measuring rather than re-running to diagnose flakiness — re-running changes the inputs
without telling you which input changed.

### 5. The agenda's specific hole, stated precisely

The owner is about to run **10 topics × 20 turns**. What is and is not pinned:

- The **clamp** to `MAX_TURNS_PER_TOPIC = 20` is pinned —
  `tests/test_kira_agenda_controller.py:229` asserts 99 clamps to 20. It does not *run* 20 turns.
- `turn_batch_size = 2`, the production default, **is** exercised —
  `tests/test_kira_agenda_controller.py:244`, but at trivial scale: one topic, three turns.
  (An earlier draft said it was never exercised. False, caught by recounting before publication —
  the claim was about to justify a conclusion, which is exactly ADR-041 R2's trigger.)
- The largest multi-topic test drives **7 topics** and is well built — it tallies each topic's
  turns individually (`tests/test_kira_chaos_stream.py:311-333`). But its helper's default is
  `_build_controller_with_7_topics(self, turns=2, batch=1)` (`:238`) — **batch 1, not the
  production 2**.
- The highest per-topic turn count any test drives is **10**, on a **single** topic, at batch 1
  (`tests/test_kira_chaos_stream.py:412`).

So every piece is covered in isolation, and **no test combines multi-topic scale with the
production batch size**. That combination is what decides how many generations a 20-turn topic
actually needs, via `min(turn_batch_size, max_turns_per_topic - projected_turns)` at
`kira_agenda_controller.py:872`. The owner's exact configuration — 10 topics × 20 turns at
batch 2 — has zero coverage as a whole, while its parts are well tested. That is a much narrower
statement than "the agenda is untested", which with 304 tests it plainly is not.

**Host terminal state, both sides pinned but never against each other.** The driver's silent exit
is pinned at `tests/test_agenda_driver.py:329-340`; the CTK path's five-effect cleanup at
`tests/test_agenda_audio_shell_characterization.py:79-113`. Each test asserts exactly what its own
path does, which is correct. But **no host-parity test exists**: a change that broke the two paths'
agreement would pass both files.

---

## The operating rule this produces

> **Run the suite when the boundary you changed is covered by it. Otherwise you are re-proving
> logic that did not change.**

| You changed | Run | Why |
|---|---|---|
| Endpoint / handler / pydantic shape | that file, then the API scope | fast, and T2 covers it well |
| `llm_engine.py` logic, tiers, sanitizer, watchdog | the engine subset | 586 T2 tests genuinely pin this |
| `_fetch_show`, `_discover_model_ctx`, `_is_local`, capability probing | **`OPENCOHOST_REALENV_TESTS=1` + a live Ollama** | the only thing that catches a real-contract break; this is the 25-day bug's exact class |
| Cloud dispatch / fallback | `OPENCOHOST_LIVE_CLOUD_TESTS=1` | costs real tokens; still the only real proof |
| Agenda state machine / turn arithmetic | the agenda scope | 304 tests, strong on single-topic logic |
| Nothing — "just checking" | **nothing** | a green run of unchanged code buys no information |

And the standing warning: **a green default run has never executed a single line against a real
Ollama.** That is not a flaw to fix by deleting the gates — real-dependency tests genuinely cannot
run in every context. It is a fact to hold when deciding whether green means shippable.

---

## Consequences

**Positive.** The owner's actual question is answered with numbers rather than reassurance, and the
answer is mostly good news: no self-mocked tests exist on the three surfaces that matter. The
suite's real weakness is now named and quantified (10 tests, 0.2%, two directories) instead of
diffuse suspicion about mocking. The flakiness finding prevents a false regression panic.

**Negative — stated plainly.** T3 = 0 is a *sampled* conclusion on two of three surfaces: the API
audit read all 456 functions, the agenda audit read its scope, but the engine audit fully read 23
of 35 files and grep-sampled the remaining 12. A T3 could hide in the unread 12. The tier
assignments are also judgment calls at the T1/T2 margin — one surface's counts moved by 5 tests on
a second pass. Neither changes any decision in the table above, but neither is a mechanical fact.
There is no automated enforcement: nothing stops a new test from being written as T3, or a new
env-gated directory from being added and forgotten, which is precisely how the 25-day bug happened.

---

## Related ADRs

- **[ADR-041](./ADR-041-verification-discipline-for-inherited-claims.md)** — R6 (an opt-in test
  needs a reason to run) is the rule this ADR quantifies; R2 (recount what you decide on) caught
  the false batch-size claim in §5; R4 (measure, don't re-run) governs §4.

## Update log

- **2026-07-30**: Created. Three-surface audit, tier taxonomy, and the run-when rule.
