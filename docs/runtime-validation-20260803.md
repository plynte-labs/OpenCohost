# Runtime validation — direct-turn preemption Steps 1+2

**Owner-executed. No mocks, no agent can do this one.**
Written 2026-08-03, for the next live session.

What this run decides: whether a typed chat message now gets its answer
**generated while Kira is still speaking**, instead of after she finishes.
Steps 1+2 do **not** interrupt anything — that is Step 3, not built yet. If you
hear Kira get cut off during this run, something is wrong; report it.

Baseline to beat, measured 2026-08-03 (`logs/opencohost_20260803_135755.log`):

| | occurrence 1 | occurrence 2 |
|---|---|---|
| `queue_wait_ms` | 53 734 | 50 390 |
| `request_to_tts_ms` | 27 547 | 15 094 |
| **total** | **81 281** | **65 484** |
| pregen | `draft=none` | `draft=none` |

---

## Before you press start

1. **Set the debug flag BEFORE launching the app.**
   ```powershell
   $env:OPENCOHOST_DEBUG="1"
   ```
   The log level is fixed at process start — setting it afterwards does nothing.
   You get a second gate for free this run: `[CLAUSE_SANITIZER]` now logs `clean`
   verdicts too, so this same session can close the ADR-039 gate. Without the
   flag you get zero sanitizer lines and it reads exactly like "the tier never
   fired".

2. **Launch with `pnpm tauri:debug`, not `pnpm tauri dev`.**
   If a backend is already listening on 8765/8770, Tauri **reuses it** and the
   env var is silently irrelevant. Confirm `managed: true` in `backend_info` if
   in doubt.

3. **Provider: either works, but the same one compares cleaner.** The number
   this run is about (`queue_wait_ms`, `request_to_tts_ms`) is about the queue,
   not the provider. The baseline above was on `nvidia_nim` / `z-ai/glm-5.2`;
   keeping it makes the comparison direct. Local changes the absolute times but
   not the shape of the result.

---

## During the session

Run an agenda with several topics, as usual. Then, **at least three times**:

1. Wait until Kira is clearly **mid-block and speaking**.
2. Send a direct chat message.
3. Note the clock time you pressed enter.
4. Note the clock time she starts speaking **your** answer.

Vary *when* you send it — the result is expected to differ:

| Send it… | Why it matters |
|---|---|
| Right after she starts a long block | The worst case. This is what produced 81 s |
| Around the middle | The common case |
| When she is nearly done | Should be roughly unchanged — there was never time to pregenerate |

One sample is what we have now, and it is not enough to see a distribution.

---

## The one line that decides it

Afterwards, in the new log under `logs/`:

```powershell
Select-String -Path logs\opencohost_2026*.log -Pattern "Pregen boundary.*source=direct"
```

**Success looks like:**
```
Pregen boundary: draft=used source=direct gap_ms=31 gen_ms=14070 speech_ms=...
```

**Failure looks like** (this is the 2026-08-03 baseline):
```
Pregen boundary: draft=none source=direct gap_ms=-1 gen_ms=-1 speech_ms=...
```

`draft=used source=direct` is the whole finding. It means your answer was
generated under cover of the agenda's speech and was waiting when the block
ended. `draft=none` means Steps 1+2 did not engage and the rest of the numbers
do not matter.

### Then the timings

```powershell
Select-String -Path logs\opencohost_2026*.log -Pattern "TURN_LATENCY.*source=direct"
```

- `request_to_tts_ms` — **this is the number that should collapse.** It was
  27 547 and 15 094. On a pregen hit it should be small, because nothing is
  generated at that point; the draft is just popped.
- `queue_wait_ms` — **expected to stay large.** We did not remove the wait, we
  removed the *generation* from inside it. If this also dropped, say so; it
  would mean something else changed.
- `request_to_tts_total_ms` — the end-to-end you actually felt. 81 281 → target
  around 55 000.

### And the displacement line

```powershell
Select-String -Path logs\opencohost_2026*.log -Pattern "draft=frozen|draft=evicted"
```

`draft=frozen` means an agenda draft your message displaced was preserved and
the beat resumed with a connector, instead of being thrown away. `draft=evicted`
on a `direct` turn would be wrong — report it.

---

## Report these five things

1. The `Pregen boundary … source=direct` lines, verbatim.
2. `request_to_tts_ms`, `queue_wait_ms` and the total, per direct turn.
3. Your own wall-clock enter→first-word, per turn. **The log and your stopwatch
   disagreeing is itself a finding** — it would mean the telemetry is measuring
   something other than what you experience.
4. Whether Kira ever got cut off. She should not have. Steps 1+2 do not interrupt.
5. `[CLAUSE_SANITIZER]` verdict counts — `clean` / `repaired` / `rejected`. This
   closes ADR-039 and costs you nothing extra:
   ```powershell
   Select-String -Path logs\opencohost_2026*.log -Pattern "CLAUSE_SANITIZER" |
     ForEach-Object { ($_ -split "verdict=")[1].Split(" ")[0] } |
     Group-Object | Select-Object Name, Count
   ```

---

## Stop and report immediately if

- **A direct message is never answered at all.** Steps 1+2 changed how long a
  queued direct turn is allowed to live. If one vanishes after being accepted,
  that is the highest-severity outcome of this change and the run should stop.
- Kira interrupts herself. Nothing in Steps 1+2 should cut speech.
- The agenda stalls after you send a chat message.
