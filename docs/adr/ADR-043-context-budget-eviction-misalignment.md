# ADR-043: Context-budget eviction misalignment — two clocks, one blind window

**Date**: 2026-08-10
**Status**: Accepted
**Branch**: `refactor/llm-engine-split-20260802`
**Author**: Claude Code, from `proposal-context-budget.md` (F6 finding) and owner decisions
recorded in `open-decisions-20260810.md`
**Scope**: process + design decision. No source changed by this ADR. Documents why the
proposed stopgap was rejected, what structural fix was accepted instead (and deferred), and
why the companion cap-raise proposal was also deferred.

---

## Context

This section assumes no prior knowledge of the engine internals. Read it in order.

### What Kira sees when she answers

Every time the owner talks to Kira, the engine (`opencohost/core/llm_engine.py`) builds a
list of messages to send to the model: a system prompt, then the recent conversation
history, then the new turn. That history does not grow forever — two separate mechanisms
control how much of it Kira actually gets to see. This ADR is about those two mechanisms
disagreeing with each other, and about a bug that can hide the disagreement.

### Mechanism 1 — the historial deque (counts turns)

`self.historial` is a `deque` (a fixed-capacity list) holding the live back-and-forth: one
entry for what the owner said, one for what Kira replied, per turn. It is declared at
`llm_engine.py:752`:

```python
self.historial = deque(maxlen=HISTORY_MAX_TURNS * 2)
```

`HISTORY_MAX_TURNS = 10` (`opencohost/config/settings.py:62`), so the deque holds at most 10
pairs (20 entries). When an 11th pair arrives, Python's `deque` silently drops the oldest
pair to make room — that is what `maxlen` means.

Before that drop happens, `_commit_history` (`llm_engine.py:6621`) notices it coming and
does something important: it writes a **digest line** — a short summary sentence — into
`self._memory_digest`, a separate, small, permanent-for-the-session record of "what Kira
used to know but no longer holds verbatim." That write happens at `llm_engine.py:6686-6690`,
inside the `with self._history_lock:` block that starts at `:6661`. So: a pair leaving the
deque **always** leaves a trace behind, in the digest. This mechanism fires on **turn count**
— the 11th pair, whenever it arrives, however big or small the pairs were.

### Mechanism 2 — the byte gate (counts bytes)

Every single turn, before the message list is sent to the model, the engine takes a
throwaway copy of the current history (a Python list, not the deque itself) and checks its
total size in characters. If that snapshot is too big for the model's context window, it
trims the oldest pairs off the front of the copy — but only off the copy. This happens at
`llm_engine.py:4392-4397`:

```python
messages, _ctx_evicted = context_budget.apply_char_budget(
    messages,
    ctx_limit=_effective_ctx,
    max_output_tokens=LLM_MAX_TOKENS,
    safety_factor=CHAR_BUDGET_SAFETY_FACTOR,
)
```

`apply_char_budget` (`opencohost/core/context/context_budget.py:95-138`) returns the trimmed
list and a count of how many pairs it dropped — nothing else. Read its own return contract:
"Returns `(messages, n_pairs_evicted)`." The content it removed is gone the moment the
function returns; nothing captures it, nothing summarizes it. If it evicted anything, the
engine only logs a warning (`llm_engine.py:4398-4403`, the `ctx_budget_gate: evicted N
pair(s)` line) — a log line, not a digest line the model can ever see again.

Crucially: this snapshot is built under `self._history_lock` at `llm_engine.py:4282-4283`,
but the lock is **released** before the trimming call at `:4392` runs. The trimming itself
never touches `self.historial` or `self._memory_digest` — it only shrinks the local copy that
is about to become the actual prompt.

This mechanism fires on **byte size** — however many characters the current snapshot adds up
to, regardless of how many pairs that is.

### The two clocks, side by side

```
Mechanism 1 (deque, turn-count):        fires at pair #11, #21, #31, ...  (fixed cadence)
                                          ALWAYS writes a digest line.

Mechanism 2 (byte gate, byte-count):    fires whenever char_budget is exceeded — could be
                                          pair #3 (verbose turns) or never (terse turns).
                                          NEVER writes a digest line.
```

Both mechanisms look at "the same" conversation, but they are not synchronized and do not
talk to each other. The byte gate can trim a pair out of what Kira is about to see, in a
session that never lives long enough (in turn-count) for the deque to reach its 10-pair
cap. That pair is gone from the prompt. It is still physically sitting in
`self.historial` — the deque has room — so mechanism 1 has not fired, and no digest line was
ever written for it either.

### The blind window, drawn out

```
Turn:        1    2    3    4    5    6    ...   N (still < 10 pairs total)
Deque:      [pair still in historial the whole time — never evicted, no digest]
Byte gate:                 ^ trims this pair from the PROMPT at turn 4
                            (Kira stops seeing it from here on)

Result: from turn 4 onward, Kira has no memory of that exchange in the live prompt,
        AND no digest line exists to remind her either. Total blackout for that content.
```

This is the "blind window": a stretch of session time in which real conversation content
has left what the model can see, with no trace left behind anywhere. It closes only once
the deque itself eventually reaches 10 pairs and evicts that same content for real — which
in a long, verbose session can be much later, or may not happen before the session ends.

**Evidence.** Session 1 (`logs/opencohost_20260807_144251.log`, referenced as F6 in
`runtime-findings-20260807.md`): the byte gate started evicting at `15:04:09`, growing to 8
evictions by `15:24:36`; the digest's first non-empty block did not appear until `15:26:55`
— roughly a 24-minute window where the engine had been silently discarding prompt content
while writing nothing to the digest. Session 2 (2026-08-09 log): the byte gate fired again,
evicting 13x more than in session 1. Both sessions were long study sessions — the owner's
main real-world use case — where turns accumulate for tens of minutes at a stretch.

---

## Decision

### 1. Reject the stopgap: lowering `HISTORY_MAX_TURNS`

The cheapest fix on paper is to shrink the deque — set `HISTORY_MAX_TURNS` to something
smaller than 10, so the deque reaches its cap (and writes a digest line) sooner, narrowing
the window before the byte gate can win the race.

**Rejected as the fix, kept off entirely — owner ruling 2026-08-10: SKIPPED.**

The reason is structural, not a matter of picking the wrong number. `HISTORY_MAX_TURNS`
counts **pairs**; the byte gate counts **bytes**. A "turn" has no fixed size — the comment
already on that constant (`settings.py:62`, *"Reducido a 10 turnos... para no desbordar el
contexto de 4096"*) shows it was already tuned once against a specific context cap, for
turns of a specific typical size. Session 1 ran reasoning-mode with an uncapped output limit
(`num_predict` popped for reasoning models, `llm_engine.py:4423-4425`), so single pairs ran
to thousands of characters — the byte gate hit its ceiling in far fewer than 10 pairs no
matter what the count-based knob was set to.

Turning the knob down narrows the window (a 3-pair cap trims sooner than a 10-pair cap,
statistically), but it can never close it: one sufficiently verbose exchange still lets the
byte gate fire before the deque count catches up, at any `HISTORY_MAX_TURNS` value. Two
mechanisms measuring different units cannot be made to agree by retuning only one of them.
That is also exactly why this same constant already needed retuning once before — it is not
a stable invariant, it is a heuristic that drifts as usage patterns change.

### 2. Adopt the structural fix: the byte gate writes its own digest line

**Owner ruling 2026-08-10: scheduled AFTER router steps 5-6. Not yet implemented.**

The fix that actually closes the window: when the byte gate trims a pair out of the
per-turn prompt snapshot, it writes the same kind of digest line for that pair, at that
exact moment — the same ledger-line mechanism `_commit_history` already uses for the deque
eviction path (`_build_ledger_line`, called at `llm_engine.py:6686`). By construction, once
this lands, nothing can leave what Kira sees without leaving a trace: either the deque
evicted it (already traced today) or the byte gate evicted it (would now also be traced).

This is honestly a focused day of work, not a one-line patch, for four reasons:

- **Contract change.** `apply_char_budget` today returns only a count
  (`context_budget.py:95-138`, `-> tuple[list[dict], int]`). It has to start returning *what*
  it evicted, not just how much — a real signature change to a module whose own design goal
  is staying pure and free of engine state.
- **Allowlist.** Not every evicted pair is safe to digest. `_commit_history`'s own eviction
  path already gates on `_DIGEST_CAPTURE_SOURCES` (`llm_engine.py:125`, currently
  `{"direct", "ptt", OWNER_BUNDLE_SOURCE}`) and skips agenda-sentinel pairs
  (`llm_engine.py:6675`, the `evicted_is_agenda` check). The byte gate's new write path has
  to honor the same allowlist, or agenda/chat scaffolding could leak into the digest as if it
  were a real exchange.
- **Locking.** `self._memory_digest.append` today only ever runs under `self._history_lock`,
  inside `_commit_history`. The byte gate runs on a snapshot taken *outside* that lock — the
  lock's scope for the snapshot ends at `llm_engine.py:4283`; the gate itself runs later, at
  `:4392`. Making the gate write to the digest means either bringing that write back under
  the lock or introducing new synchronization, because the agenda speaker daemon can be
  calling `_commit_history` concurrently.
- **Dedup.** A pair trimmed out of *this turn's* prompt snapshot by the byte gate is still
  physically present in the live `self.historial` deque — it has not actually left storage,
  only the prompt. It will still hit the deque's own eviction path later, for real, and
  `_commit_history` will try to digest it again unless something remembers "this one was
  already written." Without a dedup guard, the same exchange gets two digest lines.

None of these four is hard in isolation; getting all four right under concurrent access
(the agenda speaker daemon writing history on its own thread while a turn is generating) is
what makes this a dedicated pass rather than a quick patch, and why it is scheduled to land
in its own window rather than sharing one with the router steps 5-6 churn already planned for
`llm_engine.py`.

### 3. Defer the cap raise (Lever B) — and the `num_ctx` pop investigation

A separate, related proposal (Lever B) asked whether `LLM_TIER_EFFECTIVE_CTX_CAPS`
(`settings.py:90-94`, currently `quality: 4096, balanced: 8192, fast: 6144`) should simply be
raised, so the byte gate's budget grows and it stops evicting so eagerly in the first place.

**Owner ruling 2026-08-10: DEFERRED until the investigation below has an answer, and until
latency work is prioritized ahead of it.**

**The trap.** Raising the cap alone, without touching anything else, is not safe for one
specific model family. `llm_engine.py:4409-4418`:

```python
opciones_llm = {
    'temperature': LLM_TEMPERATURE,
    'top_p': LLM_TOP_P,
    'num_predict': LLM_MAX_TOKENS if is_local else CLOUD_MAX_TOKENS,
    'num_ctx': _effective_ctx,
}

if is_local and "gemma" in request_model.lower():
    opciones_llm.pop('num_ctx', None)
    opciones_llm['temperature'] = 0.7
```

For any local model whose name contains `"gemma"`, `num_ctx` is built and then immediately
removed before the request reaches Ollama. Ollama then uses its own internal default window
for that model — not `_effective_ctx`, not whatever the tier cap says. The engine's overflow
detector (`context_budget.is_overflow_signal`, wired at `llm_engine.py:4653-4661`) compares
the model's real response against `_effective_ctx` to decide whether the window overflowed —
but for gemma, `_effective_ctx` was never the number actually governing the model. Raising
the cap raises the threshold that detector waits for, at the same time as it does nothing to
what Ollama is actually enforcing for gemma — the one safety net that would catch a real
overflow becomes *less* likely to trip exactly when the real risk goes up. That is the whole
reason the cap raise cannot be decided independently of the pop.

**Investigation (read-only, this ADR's own contribution): why does the pop exist?**

Findings, from `git log` and the repo's own docs — no source was changed to reach these:

- The pop is not documented anywhere near the code itself. There is no comment on the
  `if is_local and "gemma"` block at `llm_engine.py:4416-4418`, and no test that explains
  the reasoning — `tests/test_context_overflow_guardrail.py:392-394`
  (`test_gemma_still_pops_num_ctx`) only pins the *behavior*, not the *why*.
- `git log -S` on the pop's exact code (`opciones_llm.pop('num_ctx'`) shows it changed
  exactly once in this repo's entire history: it was already present, unconditionally, in
  the very first commit (`9eb7ae4`, 2026-05-04, "Initial commit: VoiceAI v1.0"). Every later
  touch to that region (`18995c4`, `82eafa5`, `355ed49`) is a refactor around it, never a
  change to the pop's own condition or reasoning.
- The reasoning does exist — just in a different file. `docs/TROUBLESHOOTING.md`, entry
  **TSH-010: "Gemma no soporta num_ctx"** (added 2026-05-12, `1c7e16e`, eight days after the
  initial commit — a docs-only addition; the code itself did not change in that commit):

  > **Síntoma:** `Error Ollama: invalid option: num_ctx`
  > **Causa raíz:** El modelo `gemma` de Google no acepta el parámetro `num_ctx` en las
  > opciones de generación de Ollama.
  > **Fix:** Detectar "gemma" en el nombre del modelo y remover `num_ctx` de `opciones_llm`
  > antes de llamar a `ollama.chat()`.

  In English: sending `num_ctx` to a gemma model produced a **hard error from Ollama** —
  `invalid option: num_ctx` — not a silent VRAM problem and not a soft degradation. The pop
  is documented, in this repo, as a compatibility workaround for a request Ollama outright
  rejected for this model family.
- This is a different explanation from the one the source proposal considered plausible. A
  separate, later track (`context_overflow_guardrail_20260623/explore.md:175-181`, written
  2026-06-23 — six weeks after TSH-010) frames raising `num_ctx` in general as a VRAM/KV-cache
  cost tradeoff ("every 4096-token increase... costs ~0.5–1 GB VRAM"). That framing is about
  `num_ctx` generally, across all local models — it never mentions gemma's specific
  incompatibility, and nothing in that track cross-references TSH-010. The VRAM-guard
  reading and the invalid-option reading were never reconciled against each other in this
  repo; they simply live in two documents that do not cite one another.

**What the evidence supports, plainly:** the pop's original, documented reason is a
compatibility workaround for a hard Ollama error on gemma models — not a VRAM guard. A VRAM
concern about `num_ctx` in general is separately and legitimately documented elsewhere, but
for a different reason and not specific to gemma.

**What remains unknown:** TSH-010 carries no Ollama version number and no specific gemma tag.
Ollama and the gemma model family have both shipped changes since 2026-05-04; whether
`invalid option: num_ctx` still reproduces against the versions currently in use is
unverified — this investigation was read-only by design (git history, code, tests) and ran no
live Ollama call. Confirming or retiring the pop requires exactly the kind of live check this
investigation deliberately did not do.

---

## Consequences

**Positive.** The blind window now has a named cause (two independent eviction clocks: bytes
vs. turn-count) instead of being read as one vague "Kira forgets sometimes" complaint. The
rejected stopgap has a stated reason for rejection that will not need re-litigating the next
time someone proposes "just lower `HISTORY_MAX_TURNS` again." The `num_ctx` pop's origin is
now traced to a specific, cited troubleshooting entry instead of standing as an unexplained
four-line special case that the next reader has to either trust blindly or worry is load-bearing
for something undocumented.

**Negative — stated plainly.** The blind window is not fixed by this ADR; it is only
diagnosed and scheduled. Until the structural fix (byte gate writes digest lines) lands after
router steps 5-6, every long session can still produce the same class of silent forgetting
described in §"The blind window, drawn out" above — this is an accepted, time-boxed gap, not
a closed one. The cap raise stays blocked behind an open question (does `invalid option:
num_ctx` still reproduce today?) that this ADR could not close by design, since answering it
needs a live Ollama call against a current gemma tag, which was explicitly out of scope for
the read-only investigation. Until that live check happens, Lever B cannot be decided at all
— not "decided to defer" in the sense of a considered tradeoff, but genuinely undecidable on
today's evidence.

---

## Related documents

- `conductor/tracks/interruptible_speech_architecture_20260804/proposal-context-budget.md` —
  the source proposal (Lever A and Lever B), F5/F6 findings, and the original measurement
  protocol for a future cap decision.
- `conductor/tracks/interruptible_speech_architecture_20260804/open-decisions-20260810.md` —
  Decision 2 (Lever A, recommendation "(3): stopgap now is NOT taken, real fix scheduled
  after router 5-6" — this ADR records the owner choosing the real-fix-only path directly,
  without the stopgap) and Decision 3 (Lever B: defer).
- `docs/TROUBLESHOOTING.md` — TSH-010, the original documented reason for the gemma
  `num_ctx` pop.
- `conductor/tracks/context_overflow_guardrail_20260623/explore.md` — the VRAM/KV-cache
  framing for `num_ctx` in general (Option F), written independently of TSH-010.

## Update log

- **2026-08-10**: Created. Documents the two-mechanism eviction misalignment, the rejected
  stopgap, the scheduled structural fix, and the read-only `num_ctx` pop investigation.
