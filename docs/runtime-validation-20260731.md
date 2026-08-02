# Runtime validation — 2026-07-31 batch (post-validation findings loop)

What shipped this loop that **tests cannot prove**. Automated coverage already passed — and was
recounted by the orchestrator, not just reported by agents: **5072 Python tests (0 failed, 14
skipped)** and **1031 front tests (0 failed)**, up from 4907 / ~1010 before the loop with zero
regressions. That proves the code does what it was written to do — not that it sounds right, looks
right in the status bar, or behaves on a real busy machine.

The work: 11 units in 4 batches from `conductor/tracks/runtime_findings_batch_20260731/plan.md`
(F1–F15). Two units were adversarially judged (2.2 by one opus judge → 7 findings fixed; 3.1 by one
opus judge → 5 findings fixed, including one BLOCKER). Nothing is committed — the whole loop is
uncommitted work in the dirty tree, by design.

**Surfaces in scope: agenda, PTT, direct chat — in that order.** Viewer/Twitch chat is out of
scope; do not test it here.

**Injection first.** Every check below is triggerable on demand; the real event (a genuine NIM 429,
a spontaneous CJK glyph) is extra confirmation, never the requirement.

---

## 0. Precondition — same as the 2026-07-30 checklist §0 [BLOCKING]

Launch with `pnpm tauri:debug` from `OpenCohost_UI` (NOT `pnpm tauri dev`), with **no backend
already listening on 8765/8770** — otherwise `OPENCOHOST_DEBUG=1` never reaches the backend,
`[CLAUSE_SANITIZER]` lines never appear, and the ADR-039 gate stays unreadable for an eighth week.
Full trap description: `docs/runtime-validation-20260730.md` §0. Confirm `managed: true` in
`backend_info`, and note the exact log filename before testing anything.

> **The ADR-039 clause-sanitizer gate is still the open release gate.** This loop did not close it;
> only your agenda session with DEBUG on can. Report `[CLAUSE_SANITIZER]` verdict counts and
> `[TURN_LATENCY]` medians split by verdict, as before.

---

## 1. Cloud error classes — injected 401, 429+Retry-After, 429 bare (units 1.1, 2.1, 2.2)

**Why it needs you:** the classifier, the in-turn retry, and the background prober are all
table-tested against fakes; only a live app proves the banner, the feed narration, and the
fallback→probe→restore arc render where you actually look.

**1a. `bad_key` (trivially deterministic):** in the provider card, set a deliberately wrong API
key, keep cloud active, and type a direct question.
Expect: NO retry (`clase=bad_key` in the log, no `intento 2/2`), the feed banner **"Motor: revisá
la API key de cloud"** exactly once (not once per turn), fallback to local with the status bar
showing `fallback` and **no** probe countdown (`next_cloud_probe_in_seconds` stays null — a bad key
never auto-returns).

**1b. `rate_limited` and the auto-return (mock server):** save and run this, then point the
provider card's `base_url` at `http://127.0.0.1:9876/v1` (any model id, any key):

```python
# mock_cloud_429.py — first 2 requests: 429 + Retry-After; then valid completions.
# For the BARE-429 variant, comment out the Retry-After line.
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
N = {"n": 0}
class H(BaseHTTPRequestHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        N["n"] += 1
        if N["n"] <= 2:
            self.send_response(429)
            self.send_header("Retry-After", "20")   # comment out for ambiguous_429
            self.end_headers()
        else:
            body = json.dumps({"choices": [{"message": {"role": "assistant",
                "content": "ok"}}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    def log_message(self, *a): pass
HTTPServer(("127.0.0.1", 9876), H).serve_forever()
```

With `Retry-After: 20` (> the 10 s in-turn cap): no in-turn retry, fallback engages, and the feed
narrates the arc: **"Motor: cloud caído, usando Ollama local"** → **"Motor: reintento de cloud
programado"** (the status bar shows the countdown) → after the probe succeeds against the
now-healthy mock, **"Motor: cloud restaurado"**. If you drop Retry-After to `2`, you should instead
see a real in-turn retry (`intento 2/2` finally exists) that succeeds without any fallback.
With the bare-429 variant: `clase=ambiguous_429`, NO probe scheduled, no countdown — manual re-arm
only (toggle the provider) and the UI never claims which 429 cause it was.
**[OUTDATED 2026-08-01: superseded by §8 — bare 429 now auto-arms conservative probes (flag
default ON) and the mock lives at `tools/mock_cloud_429.py` with `--bare` / `--fail forever`
modes.]**

**1c. Stale-draft safety at the transitions (2.2 precondition):** during the 1b agenda run, listen
across both transitions (cloud→local and local→cloud). No block should repeat oddly or sound like a
draft from "the other side" of the switch.

**Report back:** each `clase=` line; whether `rate_limited` retried and `bad_key` did not; the three
narration events as INFO in the feed; the status-bar provider line during fallback (it must say
local — the 2026-07-30 run showed the cloud model for two hours); and
`grep -c MODEL_MISMATCH_WARNING <log>` on a cloud run — **must be 0** (was 29).

---

## 2. TTS non-Latin filter — injected phrase (unit 1.2)

Type this as a direct message: `Prueba 承認 y مرحبا y también Ω con ± 2 × 3`
Expect: the **screen shows every glyph untouched**; the **voice** reads only
"Prueba y y también con más menos dos por tres" — no spoken description of any glyph, no
placeholder, no dangling "y y" weirdness beyond what the sentence itself has. Log:
`[TTS_SANITIZE] non_latin_stripped chars=N categories=[...]` (counts only, never the text).

**Report back:** what the screen shows vs what you heard, verbatim-ish.

---

## 3. Resource rail vs `ollama ps` (units 2.3, 2.4)

Mid-session on local, run `ollama ps` in a shell, then open the status popover.
Expect new rows under **"Modelo (estimado por Ollama)"**: `VRAM usada`, `Modelo residente (est.)`,
`Modelo en VRAM (est.)`, `Spill a RAM (est.)`, `CPU/GPU`; and a **"Contexto (KV)"** section with
`Contexto usado` ("NN % de 4096") and `Pares evictados`. The 13 GB mystery from 2026-07-30 should
now be a visible number instead of a surprise.

**Report back:** `SIZE`/`PROCESSOR` from the CLI vs the popover's estimates, and whether the
context ratio roughly matches the `ctx_utilization` log line for the same turn.

---

## 4. Agenda attempts + session mode (units 3.1, 2.5)

Run one agenda with the count set (the selector now says **"Intentos por tema"**; note rejections
now also count, which the new caption explains). On a healthy model, N configured ≈ N audible
blocks — **the 2× halving is gone** (20 configured used to speak ~10). Sessions now cost ~2× the
generations they did before; that is the honest price of D1.
Expect at the end: the feed still says "■ agenda finalizada" (correct, per your ruling), while the
status shows **"Post-agenda"** as long as Kira is still speaking/answering, then **"Inactiva"**
only when truly quiet. The closing line must not consume an attempt.

**Report back:** configured vs audible blocks; whether the closing consumed one; the mode
transitions you saw and whether "Inactiva" ever appeared while she was still audibly speaking
(it must not).

---

## 5. PTT over a mid-band agenda turn (F8 — WU5, never exercised in any real run)

Hold PTT while Kira is mid-agenda-block. Expect: the TTS cuts, your dictation is processed, and
the frozen agenda draft returns after the detour. This code is complete and tested but had **zero**
runtime events in the only long run we have — this is its first real proof.

**Report back:** whether the cut happened, whether the agenda resumed with the held draft, and any
`frozen`/`stash` log lines.

---

## 6. Direct questions during agenda (units 4.1, 4.2 — the 20-minute defect)

Type direct questions while an agenda runs. Expect: an immediate receipt — **"Kira te escuchó —
responderá después del bloque actual"** — and the answer after the current block (bound: remaining
speech + one generation; documented as `DIRECT_ANSWER_MAX_WAIT_SECONDS = 300`). The landed reply
shows **"esperó N s en cola"** and the answering provider ("· Ollama local"); if the provider
changed while queued you'll see "(cambió de proveedor en cola)".
On 2026-07-30 these questions waited 13.8–29.1 **minutes** while the metric claimed ~15 s. Both
halves are now fixed: the wait is measured (`queue_wait_ms=` in `[TURN_LATENCY]`) and bounded (a
queued direct jumps ahead of the next pregenerated agenda block).

**Report back:** wall-clock wait vs the shown queue wait for 2–3 questions, and whether any
`[DIRECT_WAIT_EXCEEDED]` line fired.

---

## 7. Optional — a real cloud run until a genuine 429

Extra confirmation only. If NIM rate-limits you for real: report the `clase=` line and whether the
arc from §1b reproduced in the wild.

---

## 8. Manual re-arm + auto-return (NEW — cloud_rearm_20260801 loop, shipped 2026-08-01)

Reuse §1b's `mock_cloud_429.py` in BARE-429 mode (comment out the Retry-After line).

1. **Force the fallback**: bare 429 → `clase=ambiguous_429`, `cloud_fallback_engaged`. The status
   rail now shows a red cloud chip ("Cloud: acción requerida"). Note: with the auto-return flag on
   (default, `CLOUD_AUTO_RETURN_AMBIGUOUS_429_ENABLED`), the engine ALSO quietly schedules
   conservative probes (first at ~120 s) — the red chip deliberately shows no countdown for this
   class (see the post-loop questions doc if that reads wrong to you).
2. **Manual path**: open the chip popover, click "Probar ahora" (or
   `curl -X POST .../api/llm/provider/probe`) → `armed:true`; feed shows "Reintento de cloud
   forzado" → "Motor: reintento de cloud programado". Make the mock healthy → "Motor: cloud
   restaurado", chip disappears, next turn is cloud. If the mock still serves 429s, the failed
   manual probe hands off to the class cadence (next probe ~120 s) — it must NOT re-probe in a
   tight loop (this exact hammer bug was found by the stage judge and fixed; watch the feed
   spacing to confirm).
3. **No-click path** (flag on): do nothing — auto probe at ~120 s, then 240/480/900/900/900. With
   the mock left serving 429s, after 6 attempts the feed shows "Motor: reintento de cloud
   abandonado — probá manualmente" (`cloud_probe_gave_up`, no detail payload) and the manual
   button still works afterwards.
4. **bad_key variant** (§1a mock): red chip, no countdown ever. "Probar ahora" fires exactly ONE
   probe — if it fails, `cloud_probe_gave_up` immediately (one-shot by design: waiting cannot fix
   a bad credential). Fixing the key via the provider editor remains the real path.
5. **Error feedback**: with the backend stopped, "Probar ahora" must show the red inline alert
   ("No se pudo forzar el reintento") — never a silent re-enable.

---

## Grep cheat-sheet for the report

```powershell
Get-Content <log> | Select-String 'clase=|cloud_fallback_engaged|cloud_probe_scheduled|cloud_restored|DIRECT_WAIT_EXCEEDED|TTS_SANITIZE|CLAUSE_SANITIZER|MODEL_MISMATCH_WARNING'
Get-Content <log> | Select-String 'TURN_LATENCY'   # now carries queue_wait_ms= and request_to_tts_total_ms=
Get-Content <log> | Select-String 'MODEL_TRACE'    # now carries provider= transport= fallback_active=
```

Nothing is committed without your explicit request. The tree holds the whole loop uncommitted.

---

# Results — first pass (owner run 2026-07-31, forensics 2026-08-01)

Owner ran `pnpm tauri:debug` on 2026-07-31 evening. Primary evidence:
`logs/opencohost_20260731_171728.log` — 17:17→20:29 (3h12m), agenda on `gemma4:e4b`, ended
abruptly by a power outage mid-playback (no shutdown lines; 173/174 TTS blocks completed).
All counts below were recounted by the orchestrator after the forensic subagent reported them.

| § | Verdict | Evidence |
|---|---|---|
| 0 | ✅ | Debug logging reached the backend: 426 `[DEBUG]` lines in the log. |
| 1a bad_key | ⬜ pending | Never injected. |
| 1b 429+Retry-After | ⬜ pending | Never injected — `intento 2/2` count is 0; the auto-return prober arc (`cloud_probe_scheduled` → `cloud_restored`) has zero runtime evidence. |
| 1 (wild) | ✅ | REAL bare NIM 429 at 17:44:00 → `clase=ambiguous_429`, no in-turn retry, fallback to local (`reason=ambiguous_429`), no probe — exactly per contract. MODEL_TRACE split: 25 cloud lines before, 192 local after; zero cloud after 17:43:02. The in-flight turn's 9 TTS fragments kept playing. `MODEL_MISMATCH_WARNING` = **0** (was 29 on 2026-07-30). |
| 1c stale drafts | ✅ (weak) | One transition only (cloud→local); no repeated/odd blocks around it; local→cloud never happened. |
| 2 TTS filter | ✅ log-side | Fired on a REAL spontaneous glyph: `[TTS_SANITIZE] non_latin_stripped chars=2 categories=cjk` at 17:39:27. Screen-vs-voice comparison: owner did not check — pending. |
| 3 resource rail | ◐ | Log side alive: 192 `ctx_utilization` lines, ratio 0.651–0.725, `effective_ctx=4096` vs `native_ctx=131072`, 7–8 evicted pairs on every turn, 0 `ctx_pressure_high`. Popover-vs-`ollama ps` comparison pending. |
| 4 attempts + mode | ◐ | 174 audible blocks over 3h; two agenda closings (`source=kira-agenda-stop` at 19:57:30 and 20:19:38 — owner restarted the agenda). The log carries no per-topic config lines, so configured-vs-audible and the closing-consumes-no-attempt check need the owner's count. Session-mode chip transitions: not checked visually — pending. |
| 5 PTT | ⬜ pending | **Zero** PTT/frozen/stash events. Still the only shipped path with no runtime evidence, ever. |
| 6 direct | ✅ (n=1) | One direct at 18:18:10: `queue_wait_ms=63030`, generation 14.25 s, total 77.28 s — vs 13.8–29.1 **minutes** on 2026-07-30. 0 `DIRECT_WAIT_EXCEEDED`. More samples wanted next run. |
| 7 real 429 | ✅ | Satisfied by the wild event above. |
| ADR-039 | ⚖️ OPEN (owner ruling) | Sanitizer armed all session, 0 non-clean verdicts across ~174 blocks — with real repetition pressure present (38 `prefetch rechazado`, 4 `salida rechazada`, 5 guardrail blocks, all handled by the other guards). This disproves "the sanitizer harms healthy sessions"; the repair path remains lab-only. Owner's redefined closure path: opt-in session transcripts + external semantic evaluation (research, out of track). |

Visually confirmed by the owner: the status rail switched to `gemma4` during the fallback — the
2026-07-30 "cloud model shown for two hours" bug is dead. Every other visual check remains pending.

**Next run only needs:** §1a, §1b (mock server above), §5 PTT, 2–3 more §6 directs, the
pending visual checks (§2 screen-vs-voice, §3 popover, §4 counts + mode chip), and the NEW §8
(manual re-arm + auto-return, shipped 2026-08-01).

**New investigation items logged 2026-08-01:** (a) manual cloud re-arm endpoint reusing the 2.2
prober with conservative auto for `ambiguous_429` — **IMPLEMENTED 2026-08-01** in the
`cloud_rearm_20260801` loop (validate via §8); (b) opt-in session transcript capture (needs an
explicit carve-out of the never-log-raw-dialogue rule) — still research, out of track; (c)
guardrail rejection pairs for semantic evaluation — still research, out of track. Details:
`conductor/tracks.md` (Runtime Findings Batch entry) and engram `runtime/validation-run-20260731`.

---

# Results — second pass (owner run 2026-08-01, forensics same day)

Owner ran a 4h33m real agenda session (16:17→20:50, `logs/opencohost_20260801_161725.log`,
3275 lines) the same day the cloud_rearm loop shipped. §8 was exercised against **four REAL
bare NIM 429s** — no mock needed for the happy paths. Forensics: two sonnet log agents, every
published number recounted by the orchestrator, cross-validated at second resolution against
`logs/api_audit.jsonl.1/.2` (the four manual probe POSTs) and the owner's front feed
transcript (the event-store rendering — see the observability note below).

| § | Verdict | Evidence |
|---|---|---|
| 0 | ✅ | 373 `[DEBUG]` lines reached the file (managed launch worked again). |
| 8.1 force fallback | ✅ (wild ×4) | Four bare NIM 429s → `clase=ambiguous_429` every time (16:49:27, 17:44:13, 18:52:09, 20:01:47), fallback engaged, red chip rendered (owner screenshot). Only HTTP 429 in the whole log — never 401/5xx. Zero key/prompt leakage in cloud lines. |
| 8.2 manual path | ✅ | Four `POST /api/llm/provider/probe` in the API audit: 16:56:02, 17:15:49, 19:18:48, 19:32:31 — all 200 in 15–31 ms. Failed manual probe hands off to the class cadence: click 17:15:49 → reseed 120 s → probe 17:17:49 → `Cloud restored` 17:17:52 (120 s + 3 s, second-exact). Immediate-success arc too: click 19:32:31 → restored 19:32:34 (3 s). **No machine-gun anywhere** — minimum observed spacing 120 s (the stage-judge hammer bug stayed dead in production). |
| 8.3 no-click auto | ◐ | Two pure-auto arcs: 17:44:13→18:28:15 and 20:01:47→20:45:49, both **2642 s vs 2640 s theoretical** for restore on attempt 5 (120+240+480+900+900, delta 2 s). Cadence proven. **Give-up after 6 never reached** (max was attempt 5, twice) — needs the mock left failing. |
| 8.4 bad_key one-shot | ⬜ pending | Never exercised — needs the §1a mock. |
| 8.5 error alert | ⬜ pending | Backend never stopped mid-run — needs a deliberate stop + click. |
| 1b prober arc | ✅ (via §8) | The 2026-07-31 gap "auto-return prober arc has zero runtime evidence" is closed: `cloud_probe_scheduled` → `cloud_restored` observed 4× (feed + downtime math). The injected-Retry-After variant (`rate_limited` chip with countdown) is still pending — NIM's 429s always come bare. |
| 4 attempts | ◐ | 11 topic closings (`kira-agenda-stop`), one every ~22 min; 245 completed TTS pipelines; 194 prefetch cache-hit turns. Configured-vs-audible count still owner-side. |
| 6 direct | ✅ (n=11 total) | 10 more directs: generation median 12.5 s, max 20.7 s; `queue_wait_ms` median 16.5 s, max 55.2 s. 0 `DIRECT_WAIT_EXCEEDED`. Long queue waits are turn-in-flight + TTS, not the 2026-07-30 defect. |
| ADR-039 | ⚖️ OPEN — now n=2 sessions | **Second consecutive clean session**: 0 `[CLAUSE_SANITIZER]` lines and 0 tier-2 rejects across ~275 eligible agenda generations (7/31: ~174). Upstream guards did the catching: 23 prefetch rejections (17 skeleton_repetition, 2 internal-leak, 4 repeats_recent_line), 5 salida rechazada, 4 non-negotiable blocks (3 retried OK; the 4th at 20:48:08 cut by session end). Owner ruling on closure still open (see post-loop questions). |

Session health (context for future baselines): TURN_LATENCY n=47 — agenda median 17.3 s split
**11.5 s cloud-era vs 18.5 s local-era** (+62 %; all 39–43 s outliers in fallback windows —
the auto-return's measured value). `ctx_budget_gate`: 165 evictions, 1279 pairs,
`effective_ctx=4096` (native 131072), peak ratio 0.729. "Qwen (voz) unavailable" in the UI is
the expected post-extirpation state (Edge→Piper active). Memoria: 10 saves in the feed
(event-store only); 1 promotion sweep at startup (kept=0). Session end: `soft_stop` +
`emergency_stop` leave NO text-log lines — footprint only ("Cola: descartados 1 pendientes",
last TTS batch cut 12/14 at 20:50:56). No crash, no traceback at the end.

**Observability note (new follow-up candidate):** the probe lifecycle
(`cloud_probe_scheduled` / `cloud_probe_gave_up` / manual trigger) and the agenda stop path
emit ONLY `ui_callback` events into the engine-host event store — the text log never sees
them. This forensic pass needed the API audit + the owner's feed to reconstruct the arcs.
Candidate fix: numeric-only `self._log` lines on probe arm/give-up and agenda stop (no
dialogue, no payloads — same privacy gate as everything else).

**Next run only needs:** §8 give-up + §8.4 bad_key + §8.5 error alert (all three need the
mock), §1a, §1b injected Retry-After (countdown chip), §5 PTT, and the pending visual
checks. §6 is satisfied (n=11, both providers exercised — feed stamps show directs on
`nvidia_nim` and on local).
