# Pending Owner Decisions — consolidated index (2026-07-29)

> **This file is an INDEX, not a fourth copy.** Each row points at where the detail already
> lives. Nothing here restates a decision that is recorded elsewhere — that is how two
> ledgers end up disagreeing, which has already happened in this repo (see §0).
>
> Referenced by `docs/adr/ADR-040-*.md` and
> `conductor/tracks/residual_cleanup_20260729/proposal.md`. Built from a full audit of
> `AGENT_HANDOFF.md`, `conductor/tracks.md`, `conductor/product.md`, all 40 ADRs, and the
> recent track proposals.

---

## §0 — Three record defects to settle first

These are not feature decisions. They are places where the repo's own record contradicts
itself, which makes every downstream decision unreliable.

### 0.1 — The heavy-model recovery gate — **RESOLVED 2026-07-29: it is CLOSED**

**The `[x]` at `conductor/tracks.md:748` is CORRECT. `CLAUDE.md` and the 2026-07-16 handoff entry
were STALE and are now fixed.**

I left this open earlier because I only had inference. There is evidence.
`logs/opencohost_20260617_175453.log` exists on disk (20,100 bytes) and contains, at 18:05:24:

```
[ERROR]   Motor: Timeout de inferencia con qwopus tras 45.00s. Iniciando recuperación...
[WARNING] Inference watchdog timeout: model=qwopus source=direct timeout=45.00s
[WARNING] Motor: Recuperación: rollback automático de qwopus a gemma4:e2b.
```

Heavy model `qwopus` warmed 23.5 s, the first real inference hung, the watchdog fired at 45.00 s,
automatic rollback to last-known-good `gemma4:e2b`, and queue processing continued **without a
restart**. AC1/AC2/AC3/AC5 satisfied. Corroborates engram #2214.

**Why I got this wrong the first time, worth keeping:** I ranked the July sources above the June
ones purely because they were newer and more direct. The June entries cited a filename; the July
entries cited nothing. **Recency does not beat a log file.** An unexercised-gate claim must cite
a log or be treated as stale.

**Consequence:** roadmap #1 as written is satisfied, so the documentation fix — not a runtime
validation — was the only thing blocking new tracks. The remaining open runtime gate is the
clause sanitizer.

<details><summary>Original row, kept for the record</summary>

#### The heavy-model recovery gate: is it closed or open? (superseded)

The record disagrees with itself, and this one matters because **it is roadmap item #1 and it
blocks starting any new track.**

| Says CLOSED | Says OPEN |
|---|---|
| `conductor/tracks.md:748` — marked `[x]` | `AGENT_HANDOFF.md:31-33` (2026-07-16) — "explicitly NOT relevant yet per owner… remains **unexercised**" |
| `AGENT_HANDOFF.md:989-992` (2026-06-11) — "Gate 1: PASS — real watchdog timeout on `gemma:26b`" | `conductor/tracks/lote1_open_questions_20260725/proposal.md:189-190` — "still never exercised against a real heavy/stalling model" |
| `AGENT_HANDOFF.md:1198-1206` (2026-06-17) — "RE-VALIDATED… against real stalling model `qwopus`" | **`CLAUDE.md`** (governs every session) — "implemented, needs real runtime validation"; roadmap #1: "This is a release gate" |

The newer sources and the live `CLAUDE.md` outrank the June entries by date and directness, so
the working assumption is **OPEN**. But this needs your word, not my inference.

**I did not flip the `[x]` at `tracks.md:748` myself** — that is your status marker and the
newer evidence could equally mean the gate was re-opened for a different reason. One sentence
from you closes this.

</details>

### 0.2 — `conductor/tracks/` is gitignored (`.gitignore:78`)

Every track proposal — including the three written today — is **invisible to `git grep`,
ripgrep, and any git-aware audit tool**. Only `Read` finds them. Consequence: the planning
surface a future agent will actually discover is `conductor/tracks.md` + `AGENT_HANDOFF.md` +
`docs/adr/`, and anything living only in a track folder is effectively unfindable.

**Decision needed:** keep them local-only (and accept that they must be indexed in
`tracks.md` to be discoverable), or start tracking them. I have indexed today's three in
`tracks.md` as a stopgap either way.

### 0.3 — `ADR-010` does not exist

The sequence jumps 009 → 011 with no record of why. Harmless, but a reader will wonder.
**Decision:** leave the gap with a note, or renumber (do not renumber — cross-references
exist).

*Fixed today, no decision needed:* ADR-011's status line read "Proposed — not yet
implemented" for five weeks while its own update log recorded the ladder as shipped and
validated. Corrected.

---

## §1 — Blocks the clause_sanitizer release (ADR-039)

| # | Decision | Detail lives at | My recommendation |
|---|---|---|---|
| 1.1 | **Run a real agenda session and report the telemetry.** Nothing else closes this. | `ADR-039` § Validation gate | Report `[CLAUSE_SANITIZER]` verdict counts, `[TURN_LATENCY]` medians split by verdict, and whether any `repaired` turn removed something it should not have |
| 1.2 | Threshold tuning — every value is reasoned, **none is a measurement** | `ADR-039` § Open questions; `deferred-20260729:152-156` | Wait for 1.1. Do not tune on intuition |
| 1.3 | Dead-air risk on the tier-2 reject path — **declared NOT RESOLVED** | `ADR-039` § Consequences | Wait for 1.1. A reject empties the pregen slot, reverting a boundary from 0.34-0.43 s toward 16.3-18.5 s |
| 1.4 | direct/PTT reject policy if those sources are ever armed: one regeneration vs immediate fallback | `deferred-20260729:131-150` | **Policy B (immediate fallback)** — the operator is present and can re-ask; NIM latency is unmeasured and retry success is negatively correlated |

## §2 — Ready to act, small, low risk

| # | Decision | Detail lives at | My recommendation |
|---|---|---|---|
| 2.1 | Run the residual cleanup loop (R1–R4) while you validate live | `tracks/residual_cleanup_20260729/proposal.md` | **Yes.** Four units, three touch only tests/docs, all behind a live-validation safety gate |
| 2.2 | Viewer-chat preview in `runtime_check()` → metadata | `tracks/log_privacy_hygiene_20260729/` U1 | **Do it now, precisely because it is dead code.** Cheaper than after it goes live |
| 2.3 | Pin the abbreviation + CJK no-ops as intentional; document what the tier promises | `tracks/sanitizer_language_scope_20260729/` U1+U3 | Yes — converts latent limitations into visible ones |
| ~~2.4~~ | **ANSWERED 2026-07-29 — test deleted (`48207ac`).** Owner confirmed the renames were deliberate, so the assertion was claiming the user had not used a supported feature. Suite now fully green. Original row kept below for the record | — | — |
| ~~2.4~~ | **Is `test_legacy_profiles_preserved` worth keeping at all?** It asserts six profile names; your `perfiles.json` is missing exactly `Hagg` and `Akira (Uncensored)`. Both were **real tracked profiles** until `924ea5a` untracked `perfiles.json` as user state — history at `924ea5a^` proves it. Your copy has since diverged: `Akira (Uncensored)` looks **renamed to `Akira (Unchained)`**, `Hagg` removed, `Chat` + `Default EN` added | `tracks/residual_cleanup_20260729/` R5 | **Delete the test.** It is a snapshot of one machine's mutable, gitignored state, so no list makes it stable — and the test above it already covers the shipped set. Confirm you renamed/removed those two yourself and it is not the seeder eating profiles |

## §3 — Waiting on you, no work possible until answered

| # | Decision | Detail lives at |
|---|---|---|
| 3.1 | **Push / branch / commit shaping** for everything uncommitted on `codex/ui-ux-audit-proposal-20260709`. Restated in essentially every snapshot from 07-02 to today, never closed. Includes: chain strategy, and whether multi_provider phase 1 (586 lines) needs `size:exception` or a split | `AGENT_HANDOFF.md:221,260-266,313,429,488,496` |
| 3.2 | Auth enforcement flip (`OPENCOHOST_API_AUTH=1`). Recorded **four** times, all in agreement, never assigned | `AGENT_HANDOFF.md:221,428,456`; `ADR-032:62-67,76` |
| 3.3 | memoria track GO (direction and UX already decided — this is authorization to run tasks/apply) | `AGENT_HANDOFF.md:221` |
| 3.4 | kira_topic_suggestions GO — 4 conservative defaults await confirmation | `AGENT_HANDOFF.md:221`; `tracks/kira_topic_suggestions_20260724/` |
| 3.5 | Lote-1: 25 staged questions, groups A–D (provenance labeling, feed timestamps, memoria filler-list policy) | `tracks/lote1_open_questions_20260725/proposal.md`; indexed `tracks.md:1598-1613` |
| 3.6 | Gate 3 — kill-app-mid-hold PTT watchdog proof. Repeated 3× in the handoff, never closed | `AGENT_HANDOFF.md:33,252,264-265` |
| 3.7 | multi_provider follow-ups, bundled: gate `GET /api/llm/provider`? cloud-reachability health probe? task-adaptive cloud token cap? never log cloud `model=`? | `AGENT_HANDOFF.md:221` |
| 3.8 | ObsCard password retained in TanStack MutationCache — same pattern as the already-fixed provider-key leak | `AGENT_HANDOFF.md:221`; `tracks.md:12-14` |
| 3.9 | R3 guardrail negation handling | `AGENT_HANDOFF.md:216,221` |
| 3.10 | chat_commands_wiring sign-offs: R19 `estandar`→monologue label, R33 "Media" label, StreamPanel Conectar/Desconectar toggle, input-contract preset mapping (open since 07-18) | `tracks.md:1387-1415` |
| 3.11 | ADR-011 leftovers: "acknowledge vs cover" wording for the self-catch line; prompt-uniformity vs mandate-a-larger-model | `ADR-011:101,103` |
| 3.12 | Nickname-retrieval instruction wording — no sign-off yet | `ADR-031:40` |
| 3.13 | Retention policy for logs (how long, purge on launch?). The *mechanical* fix is a handler swap; the policy is yours | `tracks/log_privacy_hygiene_20260729/` U3 |

## §4 — Deliberately parked (decisions already made — do NOT re-open)

- `i18n_completion_20260723` and `launch_readiness_20260723` — **parked**, you cut scope to the
  two built tracks (`AGENT_HANDOFF.md:223`). A parked item with a recorded reason is a
  decision, not an open question.
- Packaging (`ADR-004`) — deferred by `CLAUDE.md`, gated behind runtime validation.
- `opencohost/ui/` (CustomTkinter), Stream/Tauri connections migration, the interruption state
  machine — out by your instruction; see `ADR-040` category 5.
- Intent detection and semantic repetition detection in the sanitizer — **refused, not
  deferred**; `ADR-040` category 3. Re-entry is a different design, not more evidence.
- `ui/voice_control.py:216` PTT speech logging — real and live, but in `ui/`; waits for the
  next time that directory is legitimately open (`ADR-040` category 6).

## §5 — Possibly stale, needs a one-word verdict

Each of these was raised once and never mentioned again. Say "dead" and I delete the row.

| # | Item | Last seen | Why I think it is stale |
|---|---|---|---|
| 5.1 | UI pivot to a "music-player" concept (album cover / now-playing bar), marked AWAITING OWNER | `AGENT_HANDOFF.md:481-482` | Every later UI description (ProviderCard, ModelCard, ConversationPanel, StreamPanel) uses conventional panel language. Looks superseded in practice, but nothing says so on the record |
| 5.2 | Agenda turn semantics — `turn_batch_size=2` vs the UI "turns" slider counting half-blocks | `AGENT_HANDOFF.md:266,285-287` (07-09/10) | The UI has been substantially rebuilt since |
| 5.3 | Cohost STYLE profiles are es-authored with no locale-coherence check | `AGENT_HANDOFF.md:422-423` (07-08) | Predates the whole bilingual/i18n effort; not re-raised |

## §5b — Owner priority ruling 2026-07-29: viewer chat is out of scope

> Only **agenda, PTT and direct** get effort — what the owner running OpenCohost exercises.
> Viewer/Twitch chat works only in CTK, is not properly migrated to Tauri, and is another track's
> job. Testing or hardening it today is wasted effort.

**This closes several rows by making them out of scope rather than deferred.** Anything in this
file whose only surface is viewer-chat ingest is now filed against the unmigrated-chat track, not
pending. It also means unit R1 of the residual loop targeted the wrong surface — recorded in
`AGENT_HANDOFF.md`, not reverted.

**Severity does not override surface priority when the surface does not run.**

## §6 — Found during the residual-cleanup loop (2026-07-29), recorded not fixed

Both were surfaced by R4 and both were left alone deliberately: the loop forbids widening, and
the second one is production code on the live agenda path, which the loop's safety gate freezes
while you validate.

| # | Finding | Evidence | Why it was not fixed |
|---|---|---|---|
| ~~6.1~~ | **FIXED — owner authorized 2026-07-30.** `_fetch_show` is now bounded by the same watchdog mechanism the chat call uses, at the metadata budget (`OLLAMA_REQUEST_TIMEOUT`, 5 s) rather than the 180 s generation budget | The fix could not be "pass a timeout to the client" as I first told the owner: `ollama.show(model)` takes **no timeout argument**, and moving to a `Client` would have moved the seam that **19 patch sites across 2 files** monkeypatch. Reused the engine's own generic watchdog instead — `ollama.show` stays the call target, so all 19 patches still work | Worst case is now **2× the timeout** (~10 s), because `_check_capabilities_reasoning` calls `_discover_model_ctx` and then `_fetch_show`, and a failure is not cached. Bounded and once-per-model, so not worth restructuring — noted in a `ponytail:` comment |
| ~~6.2~~ | **FIXED.** All three `_bare_motor` helpers are now genuinely hermetic — **0 live RPCs**, measured with the same spy that found them | The seed needed **two** caches, not one: `_generar_dialogo` reaches `_fetch_show` through `_discover_model_ctx` (context limit) *and* `_resolve_reasoning_classification` (capabilities). Seeding only the second left 4 RPCs alive in `test_pregen_pop_cache.py` | — |

## §7 — From the 2026-07-30 opus audit of the watchdog fix

The reviewer's mechanism claim was right and their severity was wrong: the abandoned probe thread
is real but bounded at **2 per model tag per process, 0 per additional turn** (measured; full
figures in the ADR-014 addendum). Semaphore, extra negative cache, dedicated metadata client and
a transport-timeout migration were all **rejected** — each bounds something the existing
memoisation already bounds.

Two things it surfaced that are recorded rather than fixed:

| # | Finding | Why not fixed |
|---|---|---|
| 7.1 | **`scout_digest` calls `_check_capabilities_reasoning` DIRECTLY**, bypassing the `_resolve_reasoning_classification` memo — so it would re-probe on every dispatch. **That is exactly the unbounded accumulation the reviewer described**, and it is inert only because `SCOUT_ENABLED = False` with no production writer. **If that flag is ever turned on, the reviewer wins retroactively.** A one-line swap to `_resolve_reasoning_classification` inoculates it | It changes scout gating semantics, and it is production code on the turn path during the clause-sanitizer validation freeze. **Do this before ever enabling the scout** |
| 7.2 | `tests/realenv/test_topic_scout_realenv.py::test_scout_digest_real_generation_returns_adjacent_titles` fails — "real scout produced no adjacent titles" | **PRE-EXISTING**, proven by running it against the pre-fix engine at `1a1c3f2`, where it fails identically. Unrelated to this work, and the scout is disabled |

**A process lesson worth more than either finding:** `tests/realenv/` is opt-in behind
`OPENCOHOST_REALENV_TESTS=1`, so a default run **skips** it rather than failing. The capability-probe
integration test had been red since 2026-07-24 and no green suite number would ever have revealed
it. **A test that only runs behind an env var needs something to give it a reason to run** — right
now nothing does, and 2 of 9 realenv tests are red.

---

## How to use this file

Answer §0 first — those are cheap and everything else reads more clearly once the record stops
contradicting itself. Then §1 (it is the only thing gating a release). §2 can proceed in
parallel with your live validation. §3 is the standing backlog; §5 costs you three words.

When an item is decided, record the decision **where the detail lives** and strike the row
here. Do not record the decision here — this file is an index and must stay one.
