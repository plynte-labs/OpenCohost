# Runtime validation — 2026-07-30 batch

What shipped this session that **tests cannot prove**. Automated coverage already passed: 4907
Python tests (0 failed, 14 skipped) after the clause-sanitizer residual loop and the `ollama.show`
watchdog fix. That proves the code does what it was written to do — not that it sounds right, and
not that the once-per-model probe behaves on a real, possibly-busy Ollama daemon.

**Surfaces in scope: agenda, PTT, direct chat — in that order.** Viewer/Twitch chat is out of
scope: it only works in the CustomTkinter shell, is not migrated to Tauri, and belongs to another
track. Do not test it here.

Session range for what you are testing:

```powershell
git log --oneline 8cd0a88..HEAD
```

---

## 0. Precondition — without this, the whole gate is invisible [BLOCKING]

`[CLAUSE_SANITIZER]` lines log at **DEBUG**. The logger only emits DEBUG when
`OPENCOHOST_DEBUG=1` is set **before the process starts** — it is read once at import time, so
setting it after launch does nothing.

1. In the **same shell** you will launch OpenCohost from:
   ```powershell
   $env:OPENCOHOST_DEBUG = "1"
   ```
2. Launch OpenCohost as you normally do.
3. Note the exact log filename it created — you will grep it directly instead of a wildcard, so
   you are never reading a stale file from an earlier run:
   ```powershell
   Get-ChildItem E:\VoiceAI\logs\opencohost_*.log | Sort-Object LastWriteTime -Descending | Select-Object -First 1
   ```

If you skip this, `[TURN_LATENCY]` will still appear (it logs at INFO) but `[CLAUSE_SANITIZER]`
will not — and that will look exactly like "the sanitizer never fired," which is the wrong
conclusion. Confirm before reading anything else into a quiet log.

---

## A. Clause sanitizer — agenda only, the confirmed incident's fix

**Why it needs you:** the tier is a deterministic text transform; nothing in its test suite can
tell you whether a `repaired` turn still sounds like Kira, or whether a `rejected` turn produces a
gap the audience notices. Only your ear answers that.

1. Start (or resume) an **agenda** segment and let it run at least 10–15 minutes — the confirmed
   incident took ~16 minutes to surface, and the sanitizer only has something to do when a
   candidate actually loops a clause.
2. While it runs, in a second shell, tail the sanitizer + latency lines for the agenda sources
   (PowerShell):
   ```powershell
   Get-Content <log-file> -Wait | Select-String 'CLAUSE_SANITIZER|TURN_LATENCY'
   ```
   Git Bash equivalent:
   ```bash
   tail -f <log-file> | grep -E 'CLAUSE_SANITIZER|TURN_LATENCY'
   ```
3. **Listen normally.** Do not read the log while listening — check it afterward. If a sentence
   sounds off (feels clipped, drops a callback, or a rhetorical repeat vanishes), note the
   **approximate timestamp** so you can match it to a log line later. Do not write down what she
   said — only that something sounded wrong and when.

**What a wrong removal sounds like**, so you know it when you hear it:
- A sentence that had a deliberate bookend — *"A, B, A."* — loses the second `A`. That is
  intentional rhetoric, not a loop, and D3 in ADR-039 exists specifically to protect it when the
  two occurrences are not adjacent.
- An echoed question loses its echo — e.g. she restates something then asks it back
  (`"…, ¿de verdad?"`) and the question mark version gets eaten, leaving only the flat statement.

If either happens, that is a real defect in the tier, not a threshold-tuning question — say so
explicitly when you report back.

---

## B. PTT and direct — must show ZERO sanitizer lines

**Why it needs you:** this is the cheapest possible check that the arming gate actually works.
The sanitizer is shipped **disarmed** for `ptt` and `direct` — any `[CLAUSE_SANITIZER]` line
tagged with either source is a scoping bug, not a tuning question.

1. Do a few push-to-talk turns (F-key hold) and a few typed/direct turns, ideally each with a
   sentence long enough that it *could* repeat a clause if the tier were active (e.g. list three
   things, say one of them twice).
2. Check the log:
   ```powershell
   Select-String -Path <log-file> -Pattern 'CLAUSE_SANITIZER' | Select-String 'source=ptt|source=direct'
   ```
   ```bash
   grep 'CLAUSE_SANITIZER' <log-file> | grep -E 'source=(ptt|direct)'
   ```
3. **Expect:** no output at all from that command. Any match is a scoping bug — report it, do not
   try to characterize it further.

---

## C. Dead air on a tier-2 reject

**Status: this is the one ADR-039 explicitly leaves UNRESOLVED.** A rejection empties the pregen
slot and reverts that agenda turn boundary from the measured 0.34–0.43 s gap back toward the
16.3–18.5 s regime that existed before pregen overlap shipped.

1. During the same agenda run, watch for a `verdict=rejected` line (see the grep in section D
   below — it isolates rejects specifically).
2. If one fires, note **how long the silence felt**, in seconds, before she continued. A rough
   estimate is enough — you are not timing it with a stopwatch, just flagging "that felt like a
   beat" vs. "that felt like several seconds of nothing."
3. If no reject fires during your session, that's a valid result too — write "none observed," not
   a guess.

---

## D. The four numbers that actually close ADR-039

**A "sounded fine" report does not close this gate.** These four numbers are what tells the
tuning pass whether the thresholds are right, and they only exist if you captured the log with
DEBUG on (section 0).

1. **Verdict counts, split by stage.** `stage=generate` is a spoken foreground turn;
   `stage=pregen_draft` is a speculative draft that may never be spoken — do not conflate them.
   ```powershell
   Select-String -Path <log-file> -Pattern 'CLAUSE_SANITIZER' | Group-Object { ($_ -split 'stage=|\s')[1] + '|' + ($_.Line -split 'verdict=')[1].Split()[0] } | Select-Object Name, Count
   ```
   If that one-liner is fiddly, the simple version is enough — just count manually:
   ```powershell
   Select-String -Path <log-file> -Pattern 'stage=generate.*verdict=clean'
   Select-String -Path <log-file> -Pattern 'stage=generate.*verdict=repaired'
   Select-String -Path <log-file> -Pattern 'stage=generate.*verdict=rejected'
   Select-String -Path <log-file> -Pattern 'stage=pregen_draft.*verdict=repaired'
   Select-String -Path <log-file> -Pattern 'stage=pregen_draft.*verdict=rejected'
   ```
   (swap `Select-String -Path <log-file> -Pattern` for `grep` in Git Bash)

2. **`[TURN_LATENCY]` medians, split by what the sanitizer did to that turn.** The comparison that
   matters: a turn the tier left `clean` vs. one it `repaired` vs. one it `rejected`.
   ```powershell
   Select-String -Path <log-file> -Pattern 'TURN_LATENCY'
   ```
   `[TURN_LATENCY]` and `[CLAUSE_SANITIZER]` do not share a turn ID today, so pairing them means
   matching by **timestamp proximity** — the two lines for the same turn land within a few log
   lines of each other. Eyeballing 3–5 examples per verdict bucket is enough; you do not need to
   pair every single turn.

3. **Whether any `repaired` turn removed something it should not have.** Covered in section A —
   telemetry cannot answer this one, only your ear can.

4. **Dead air on a reject, in seconds.** Covered in section C.

---

## E. Local-model turn — exercises the bounded `ollama.show` probe

**Why it needs you:** this only fires on the **local** (Ollama) path — a cloud provider never
calls it. Nothing clears the per-model classification cache, so the probe runs at most twice per
model tag per process, then never again for that tag.

1. Make sure the active provider is **local** (Ollama), not cloud.
2. Select a model you have **not** used yet this session and let it answer one turn.
3. **Note whether that first turn feels slower than the next one on the same model.** A small
   delay on the first turn (the probe is bounded at `OLLAMA_REQUEST_TIMEOUT` = 5 s, so worst case
   ~10 s if it has to check twice) is **expected**, not a bug. If Ollama is idle and responsive
   the delay should be barely noticeable.
4. Ask a second question on the **same** model. It should feel normal-speed — the classification
   is now cached.

---

## F. Model switch mid-session

1. While still running, switch to a **different** model from the model panel.
2. Ask a question on the new model, then switch back to the first one.
3. **Expect:** nothing wedges, no stuck "thinking" state, no crash. Behavior should match section
   E on the new model (first-turn probe, then cached) since nothing clears the cache — a model
   switch does not reset it, so a model you already used once this session should feel fast again
   immediately, not re-probe.

---

## If this breaks, stop and report

Do not keep testing past any of these — report immediately with the log excerpt (metadata lines
only, never raw dialogue):

- OpenCohost crashes, freezes, or the engine thread dies (check for a traceback in the log or a
  console window that closed unexpectedly).
- A `[CLAUSE_SANITIZER]` line appears with `source=ptt` or `source=direct` (section B) — the
  arming gate is broken.
- A turn hangs noticeably longer than ~10 seconds before Kira responds on a **local** model,
  even on a second turn with the same model (the probe should be cached by then).
- Any reject-driven silence feels longer than a few seconds, repeatedly — that would mean the
  dead-air cost is worse than ADR-039 documented, not just present.

---

## Privacy reminder

Only report the log lines shown above (counts, verdicts, timings) — they are metadata only.
Never paste raw dialogue, viewer chat, prompts, API keys, or full model responses into a report or
this file. If a step needs a subjective call about content (sections A.3, C, D.3), you keep the
content — report only the judgement.

---

## RESULTS

Fill in and paste back.

**Session log file:** `opencohost_________________.log`
**Commit range tested:** `8cd0a88..____________`

### A/B — Clause sanitizer verdict counts

| stage | clean | repaired | rejected |
|---|---|---|---|
| generate | | | |
| pregen_draft | | | |

**PTT/direct sanitizer lines found (should be 0):** ____

### C — Dead-air on reject

Reject observed? Y/N: ____
If yes, perceived gap (seconds): ____

### D2 — `[TURN_LATENCY]` medians (ms), by verdict

| verdict | approx. median request_to_tts_ms | sample size |
|---|---|---|
| clean | | |
| repaired | | |
| rejected | | |

### A.3 / D3 — Any wrong removal heard?

Y/N: ____
If yes, describe only the *shape* of the error (bookend lost / echo lost / other), not the
content: ____

### E — Local model probe

First-turn delay noticeable? Y/N: ____
Approx. delay if yes (seconds): ____

### F — Model switch

Any wedge/stall/crash? Y/N: ____
Second use of an already-probed model felt fast again? Y/N: ____

### Anything in the "stop and report" list triggered?

Y/N: ____
If yes, which item and the log excerpt (metadata only): ____

### Overall

Does this close ADR-039 for runtime? Y/N: ____
Anything else you noticed: ____
