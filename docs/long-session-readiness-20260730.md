# Long-session readiness — 10 topics × 20 turns

**Date**: 2026-07-30
**Scope**: theoretical readiness review for a many-hours manual agenda validation session.
**Method**: read-only. No source file was modified. Every claim below carries a symbol name and
`file:line` that was actually opened, per `docs/adr/ADR-041-verification-discipline-for-inherited-claims.md`.
**Surfaces**: agenda, PTT, direct chat only. Viewer/Twitch chat is out of scope (owner ruling
2026-07-29); one viewer-chat-adjacent note is filed in §5 and dropped.

**Label contract, used on every row below:**

- **MEASURED** — I ran something or read a real artifact on this disk, and the number is the
  observation. The exact command is quoted.
- **THEORETICAL** — I read the code and reasoned. No run backs it. The owner asked for theory, so
  this label is expected; it is never dressed up as measurement.
- **could not determine** — stated plainly, with what would be needed.

---

## 0. Verdict

**Yes, with three cheap caveats to clear before you press start.** The state machine terminates
cleanly, and essentially every per-turn in-memory collection in this codebase is *already*
explicitly bounded — I went looking for unbounded growth and mostly found caps with comments
explaining them (full list in §2.4). The real many-hours exposure is on **disk**, not in memory,
plus one **documentation defect that would make your own gate unreadable**.

Do these three first. Total cost: about five minutes.

### C1 — Your checklist asks for a number that structurally cannot exist [RESOLVED 2026-07-30]

> **Resolved after this document was written.** Both guard sites were re-verified independently
> (`llm_engine.py:1994` and `:3426` — both inside `if san.verdict != "clean":`), and
> `docs/runtime-validation-20260730.md` was corrected: the `verdict=clean` grep is gone, replaced
> by the `[TURN_LATENCY]` denominator and the subtraction below, and the RESULTS table now carries
> an explicit warning against writing a grepped `0` into a clean column. The analysis below is kept
> because the reasoning is the lesson, not the patch. Uncommitted — awaiting owner review.


`docs/runtime-validation-20260730.md:128` tells you to run
`Select-String … -Pattern 'stage=generate.*verdict=clean'`, and the RESULTS table at
`:215-218` has a **`clean`** column.

`_log_clause_sanitizer` is only ever called behind `if san.verdict != "clean"` — at
`opencohost/core/llm_engine.py:3420` (the main seam) and `:1993` (the pregen connector re-pass).
**A clean verdict is never logged.** That column will always be empty, and
`temp/revised_plan.md:160` says so in writing: *"Emitted only when `verdict != "clean"` — clean
turns are silent"*.

This is precisely the ADR-041 R6 shape — a structural zero mistaken for silence. You would grep,
get nothing, and have no way to tell "the tier saw 90 clean turns" from "the tier never ran."

**Fix**: delete the `clean` grep and the `clean` column from the checklist, or replace the
denominator with the `[TURN_LATENCY]` line count (INFO, one per foreground spoken turn,
`llm_engine.py:5296-5299`). `clean = TURN_LATENCY_count − repaired − rejected` is derivable; a
direct count is not. **MEASURED** (the two guard sites and the doc line were read).

### C2 — Empty `E:\VoiceAI\temp` before you start  <!-- path-ok: local env example -->

There are **133 leaked TTS audio artifacts, 2,961,804 bytes**, sitting there right now.

```
$ cd /e/VoiceAI/temp && ls -la tts_chunk_* out_ligero_* out_pesado_* 2>/dev/null \
    | awk '{s+=$5; n++} END {print "leaked audio files:", n, "total bytes:", s}'
leaked audio files: 133 total bytes: 2961804
$ ls -la tts_chunk_* | awk '{print $6, $7}' | sort | uniq -c
      5 Jul 22
     22 Jul 23
    106 Jul 24
```

**MEASURED.** Clearing them first costs nothing and converts a THEORETICAL leak into a MEASURED
one **from the very session you are about to run**: count the files before, count them after,
divide by turns. That is the single highest-value instrument you can add to this session, and it
is one `ls | wc -l`. Mechanism and caveats in §2.1.

### C3 — Know the number you are actually asking for

**10 topics × 20 turns is 110 LLM generations, not 200**, at the default `turn_batch_size = 2`.
MEASURED, §1.5. This is the same "agenda turn semantics" question already open in
`AGENT_HANDOFF.md:476` (the UI "turns" slider counts half-blocks). If you wanted 200 generations,
set `turn_batch_size = 1` and expect roughly **double** the wall clock and double the GPU work —
because the per-output length cap does **not** scale with the beat count (§1.6).

---

## 1. Q1 — Expected normal behaviour at 10 × 20

### 1.1 How a turn is counted

Three symbols own the arithmetic, all in `opencohost/smart_aggregator/kira_agenda_controller.py`:

| Step | Symbol | Line | What it does |
|---|---|---|---|
| 1 | `_next_block_size` | 1467-1471 | `min(turn_batch_size, max(1, max_turns_per_topic − turns_spoken))` |
| 2 | `_topic_action` | 1291-1299 | sets `_pending_turns_spoken = _next_block_size()`, source `kira-agenda`, emits the action with `turns=` that value |
| 3 | `mark_speech_complete` | 912-936 | `turns_spoken = min(max_turns_per_topic, turns_spoken + max(1, _pending_turns_spoken))`, then resets `_pending_turns_spoken = 1` |

Limits are class constants at `:267-269` — `MIN_TURNS_PER_TOPIC = 1`,
`MAX_TURNS_PER_TOPIC = 20`, `DEFAULT_TURNS_PER_TOPIC = 3` — clamped through `clamp_turn_limit`
(`:607-613`). `turn_batch_size` defaults to `2` (`__init__`, `:436`) and clamps to 1-4
(`clamp_turn_batch_size`, `:615-621`).

So **one generation can consume more than one "turn."** With the default batch of 2, a topic
capped at 20 turns produces **10** `kira-agenda` generations, not 20.

There are two other counted sources that do **not** advance `turns_spoken` by a batch:
`_chat_action` (`:1310-1318`, `turns=1`) and `_streamer_action` (PTT, `:1320-1337`, `turns=1`).
A PTT interjection therefore burns exactly one turn of the active topic's budget.

`register_failure` has a shortcut worth knowing: at `:966-970`, two consecutive failures set
`turns_spoken = max_turns_per_topic` outright — a topic can be force-exhausted by guardrail
rejections without speaking its full budget.

### 1.2 When a topic is considered exhausted

`_topic_complete` (`:1464-1465`) is a one-liner: `turns_spoken >= max_turns_per_topic`. It is
consulted from exactly one place on the normal path — `next_action`'s `WAITING_SIGNAL` branch
(`:840-841`) — and only *after* the chat-cadence check (`:836-839`), so a due chat pulse outranks
closing a finished topic by one beat.

On exhaustion, `_closing_action` (`:1339-1347`) runs: topic status → `CLOSING`, one extra
generation with source `kira-agenda-stop` and `turns=1`. That closing line is **not** counted
against the budget, because `turns_spoken` is already clamped at the max.

### 1.3 The transition to the next topic

`mark_speech_complete` (`:924-930`) is the hinge. When the finished speech belonged to a `CLOSING`
topic it: marks it `COMPLETED`, sets `active_topic = None`, calls `_reset_ladder_state()`
(`:754-764`, clearing all four regeneration-ladder fields), and lands in `IDLE` — or `OFF` if
`stop_requested`.

The next tick hits `next_action`'s `IDLE` branch (`:822-833`): `_select_next_topic()`
(`:1460-1462`) returns `queued_topics()[0]`, where `queued_topics` (`:643-645`) sorts by
`(PRIORITY_ORDER[priority], self.topics.index(topic))` — so priority first, insertion order as
tiebreak. Status → `ACTIVE`, state → `OPEN_TOPIC`, ladder state cleared again at `:830` with an
explicit comment that a previous topic's rejected phrase must not leak into the new prompt.

### 1.4 After the LAST topic — is there a terminal state, or does it idle?

**Both, depending on who is driving.** This is the one place where the answer differs by host.

- **The controller alone idles.** `next_action` at `IDLE` with an empty queue returns
  `AgendaAction.none()` (`:824-825`) and leaves the state at `IDLE`, forever.
  **MEASURED**: 20 consecutive ticks after exhaustion returned `none()` with state still `IDLE`
  (command in §1.5).
- **The host auto-exits to `OFF`.** Two independent mirrors:
  - API / Tauri: `opencohost/api/agenda_driver.py:282-288` — `action.kind == "none"` and `IDLE`
    and no active topic and no queued topics → `agenda.state = AgendaState.OFF`.
  - CTK: `opencohost/ui/agenda_audio_controller.py:183-194` — same predicate, **plus** it logs
    `"[Kira Agenda] Sesión completada: sin temas pendientes."`, restores the chat filter, refreshes
    status, and clears the OBS overlay.

Two consequences worth naming:

1. **On Tauri/API, session completion is silent.** `agenda_driver.py:282-288` sets `OFF` with no
   log line and no event emission. Compare `agenda_audio_controller.py:190`, which logs it. If you
   run the Tauri shell, expect the agenda to simply stop producing turns with no "done" marker in
   the engine log. Plan to read the agenda state badge, not the log. **THEORETICAL** (read both
   sites; I did not run either host).
2. `agenda_driver.py:280-281`'s own comment points at `app_shell.py:1590-1601` as the mirror.
   That pointer is **stale** — `app_shell.py:1584-1605` is now Twitch/stream-admin chat wiring
   (the agenda cluster was extracted to `agenda_audio_controller.py` in the Phase 7 refactor).
   Cosmetic; noted so the next reader does not chase it. **MEASURED** (read both).

Once `OFF`, `next_action` short-circuits at `:807-808` and the driver keeps ticking harmlessly
every `DEFAULT_TICK_SECONDS = 4.5` (`agenda_driver.py:38`). It never stops its thread; it just
does nothing. That is a bounded idle, not a leak.

### 1.5 Generation count — MEASURED

I drove the real controller with a stubbed speech path. No files created; run from stdin.

```
$ E:/Miniconda/envs/flux_env/python.exe - <<'PY'  # path-ok: local env example
from opencohost.smart_aggregator.kira_agenda_controller import KiraAgendaController, AgendaState
for bs in (1,2,3,4):
    c = KiraAgendaController(max_turns_per_topic=20, turn_batch_size=bs)
    for i in range(10):
        t = c.add_topic(f"T{i}", approved=True); c.queue_topic(t.id)
    c.enable()
    gens=0; by={}
    for _ in range(5000):
        a=c.next_action()
        if a.kind=="none":
            if c.state==AgendaState.IDLE and c.active_topic is None and not c.queued_topics(): break
            continue
        gens+=1; by[a.source]=by.get(a.source,0)+1
        c.mark_generation_accepted(); c.mark_speech_complete()
    print(f"batch={bs} generations={gens} {by} state={c.state}")
    idle = [c.next_action().kind for _ in range(20)]
    print(f"   post-exhaustion ticks -> {set(idle)}, state={c.state}")
PY
batch=1 generations=210 {'kira-agenda': 200, 'kira-agenda-stop': 10} state=IDLE
   post-exhaustion ticks -> {'none'}, state=IDLE
batch=2 generations=110 {'kira-agenda': 100, 'kira-agenda-stop': 10} state=IDLE
   post-exhaustion ticks -> {'none'}, state=IDLE
batch=3 generations=80  {'kira-agenda': 70,  'kira-agenda-stop': 10} state=IDLE
   post-exhaustion ticks -> {'none'}, state=IDLE
batch=4 generations=60  {'kira-agenda': 50,  'kira-agenda-stop': 10} state=IDLE
   post-exhaustion ticks -> {'none'}, state=IDLE
```

A separate run of the same drive at `batch=2` also confirmed: **111 ticks, 10/10 topics
`completed`, `turns_spoken == [20]*10` exactly — no topic overshot or undershot**, and
`rejection_log` length 0 (no guardrail path exercised, by construction).

**What this measured, precisely:** the state machine's turn accounting, topic advancement through
the whole queue, and the terminal state. **What it did NOT measure:** the guardrail validator
(`accept_output` was never called), and the **prefetch/pregen consume path** —
`prefetch_action_after_current_speech` (`:847-877`) + `start_prefetched_action` (`:879-894`) —
which is the path the API host actually uses on *every* boundary (ADR-035 §2.1-2.2). My drive
took the plain `next_action` route. That gap is the honest argument *for* the one narrow test
discussed in §3.3, and the reason I do not claim §1.5 covers production.

### 1.6 The length cap does not scale with the beat count

`_validate_output`'s sanitizer branch reads the cap at
`kira_agenda_controller.py:1167`: `cap = int(self.LIVE_SAFETY_MODE_RULES[self.safety_mode]["cap_chars"])`,
then hard-truncates on a sentence boundary (`:1168-1174`). The expression contains **no `turns`
term** — a 1-beat block and a 4-beat block get the same character ceiling. The prompt merely
*asks* the model to cover N beats (`tests/test_kira_agenda_controller.py:251` pins the literal
`"representa 2 beat(s)"`).

Caps, from `LIVE_SAFETY_MODE_RULES` (`:277-296`), with each mode's own annotated duration band:

| `safety_mode` | `cap_chars` | annotation in source |
|---|---|---|
| `live_safe` (default, `:435`) | 1100 | "~25-40s" (`:281`) |
| `monologue` | 3000 | — |
| `test` | 6000 | "~60-90s" (`:293`) |

Note those annotations are mutually inconsistent as a rate (1100→30 s implies ~37 chars/s;
6000→75 s implies ~80 chars/s), so **I do not derive character counts from durations anywhere
below.** They are source annotations, not measurements.

Practical consequence: **`turn_batch_size` is the real session-length dial, not `max_turns_per_topic`.**
Halving the batch roughly doubles the number of capped outputs, hence roughly doubles both wall
clock and LLM calls for the same nominal "200 turns."

### 1.7 Wall-clock estimate

The only real latency numbers in this repo for this path are in
`docs/adr/ADR-035-agenda-dead-air-prefetch-overlap.md`, from two owner sessions on `gemma4:e4b`:

**MEASURED (ADR-035 §1.1, §3, log files named there):**

| Quantity | Value | Where |
|---|---|---|
| Generation per agenda turn | 16-19 s | §1.1 table |
| Speech window per turn | 58-73 s | §1.1 prose |
| Dead air, pre-prefetch | 16.3-18.5 s (5/5 boundaries) | §1.1 table |
| Dead air, post-prefetch | 0.34-0.43 s (4/4 boundaries) | §3 |
| Post-interactive agenda resume | ~39 s full generation cost, by design | §3 |
| Decode throughput | 16 → 34 ms/tok as prompt grew ~1,500 → ~2,900 tok | §3 |

**Caveat I must flag:** those 58-73 s speech windows **exceed `live_safe`'s own annotated 25-40 s
band**, so that session was not running the default safety mode. Which mode it *was* running
**could not be determined** — the ADR does not record it and I did not find it in the referenced
logs. So the table below gives two rows, and says which input each uses.

**EXTRAPOLATED** — arithmetic on the above, not observed:

| Assumption | Blocks | Per-block wall | Total |
|---|---|---|---|
| ADR-035 measured window, `batch=2` | 110 | 58-73 s + ~0.4 s | **1.8 - 2.2 h** |
| ADR-035 measured window, `batch=1` | 210 | 58-73 s + ~0.4 s | **3.4 - 4.3 h** |
| `live_safe` source annotation, `batch=2` | 110 | 25-40 s + ~0.4 s | **0.8 - 1.2 h** |
| `live_safe` source annotation, `batch=1` | 210 | 25-40 s + ~0.4 s | **1.5 - 2.3 h** |

Add for each `verdict=rejected` clause-sanitizer turn: that turn's boundary reverts from ~0.4 s
toward the 16.3-18.5 s regime, because the reject path calls `_invalidate_pregen_epoch()`
(`llm_engine.py:3441-3442`) and there is no draft left to speak. `docs/runtime-validation-20260730.md:99-112`
already flags this as ADR-039's explicitly-unresolved item. **THEORETICAL** for the count (nobody
has measured the reject rate); **MEASURED** for the two dead-air regimes it moves between.

### 1.8 The margin that actually decides whether it feels good

The overlap only pays while **generation < speech window** — ADR-035 §3's own closing bullet says
this. Two of its measured numbers put that margin under pressure in the *default* mode:

- generation 16-19 s at a fresh prompt, and decode **2.1× slower** (16 → 34 ms/tok) once the
  prompt fills;
- against a `live_safe` window annotated at 25-40 s.

At saturated decode with `batch=1` in `live_safe`, generation can plausibly approach or exceed the
window, and dead air returns on every boundary. **THEORETICAL.** The good news, and the reason
this is a step and not a drift, is in §2.5: the prompt **saturates**, it does not keep growing.

**Normal behaviour, in one paragraph:** for each of 10 topics — 10 `kira-agenda` generations at
2 beats each (default), then 1 `kira-agenda-stop` closing line, then silent advance to the next
queued topic in priority order. 110 generations total. Sub-second gaps at boundaries where the
pregen draft survived, 16-19 s where it did not. After the tenth topic closes: state `OFF`, driver
still ticking every 4.5 s doing nothing, "Sesión completada" logged on CTK and nothing logged on
Tauri.

---

## 2. Q2 — Theoretical failure modes over many hours

I hunted specifically for state that grows or is never released. The headline finding is
counter-intuitive and good news: **almost everything is already capped, deliberately, with a
comment.** §2.4 lists 15 bounded collections I checked and cleared. The genuine exposure is on
disk and in threads.

Ranked by whether it bites **within one session**.

### 2.1 Leaked TTS temp files — WOULD BITE, and already has [MEASURED]

**Symbols**: `_hablar_impl` producer thread creates chunk files at
`llm_engine.py:5562`, `:5589`, `:5604`, `:5675` (`os.path.join(TEMP_DIR, f"tts_chunk_{i}_{uuid…}")`).
`TEMP_DIR` resolves to `E:\VoiceAI\temp` (verified by importing `opencohost.config.settings`).  <!-- path-ok: local env example -->

The removal discipline is genuinely careful — three separate sites:
- normal path: `finally: os.remove(archivo_chunk)` at `:5808-5813`;
- teardown mid-turn: `:5744-5751`;
- post-interrupt drain, producer joined first to close the enqueue race: `:5820-5848`.

**The gap**: on the *playback exception* path (`:5797-5807`), `self.pygame.mixer.music.unload()`
at `:5788` is **skipped** — the exception jumps straight to `finally`. On Windows, pygame may
still hold the file handle, so `os.remove` raises `OSError`, which `:5812-5813` swallows. The file
survives. **THEORETICAL** for the mechanism (I read it; I did not reproduce it).

**The amplifier**: the sweep exists — `cleanup_opencohost_temp_artifacts`
(`opencohost/core/temp_file_cleanup.py:41-73`, patterns at `:11-16` cover `tts_chunk_*`,
`out_ligero_*`, `out_pesado_*`) — but its **only two call sites are
`opencohost/ui/app_shell.py:393` (startup, `min_age_seconds=60.0`) and `:2655` (shutdown,
`min_age_seconds=0.0`)**. I found **no call site anywhere under `opencohost/api/`**. So a
Tauri/API session never sweeps, at either end. **MEASURED** (grep over `opencohost/`).

**Growth rate per turn: could not determine.** Provenance of today's 133 files is mixed and I will
not guess: the size histogram is
`29×0 B, 12×7920, 34×12672, 2×23760, 51×28656, 1×113196, 1×115244, 1×136236, 1×153132, 1×409132`,
and chunk indices are `77× idx 0, 51× idx 1, 1 each of idx 3-7`. The tightly-repeated sizes look
fixture-shaped; the five 113-409 KB files at indices 3-7 look like real multi-chunk speech. And
`CLAUDE.local.md` points pytest's `--basetemp` at `E:/VoiceAI/temp/pytest-piper-clean`, so test  <!-- path-ok: local env example -->
runs write into the same tree. **This is exactly what C2 measures for you**: clear the directory,
run the session, count again — the number is then unambiguous.

**Value at turn 200: could not determine.** Bounded above by 1 file per TTS chunk per turn if
every playback threw, which it will not.

### 2.2 Unrotated engine log — NOT a problem at INFO; unknown at DEBUG

`opencohost/config/logger.py:43-46` builds a plain `logging.FileHandler` against
`opencohost_{strftime}.log` — no `maxBytes`, no `backupCount`, no pruning of old timestamped
files. Already filed as `conductor/tracks/runtime_findings_followup_20260730/proposal.md` U4, held
until after your validation *precisely because* a rotation boundary mid-session could split the
telemetry you are collecting. **Do not change it before this session.**

**MEASURED**, from the two real ADR-035 agenda sessions on disk:

```
$ for f in logs/opencohost_20260721_012124.log logs/opencohost_20260721_131040.log; do \
    echo "$f size=$(stat -c %s $f) lines=$(wc -l < $f)"; done
logs/opencohost_20260721_012124.log size=9404 lines=86     # 01:21:28 -> 01:50:58, 29.5 min
logs/opencohost_20260721_131040.log size=9292 lines=83     # 13:10:45 -> 13:28:57, 18.2 min
```

That is **0.32-0.51 KB/min ≈ 19-31 KB/hour**. Over eight hours: ~150-250 KB. Not a concern.

**With `OPENCOHOST_DEBUG=1`: could not determine.** No DEBUG-on real session log exists in
`logs/`. I checked five candidates; the only files carrying `[DEBUG]` lines are pytest artifacts
(`logs/opencohost_20260730_000210.log`, 2143 lines with `DEBUG=3`, whose own content names
`temp\pytest-piper-` paths). To bound it by reading instead: there are **8** `logger.debug` call
sites in `llm_engine.py` (`:1149, :1707, :1730, :3066, :3272, :4720, :5078, :5376`) and **21**
across the whole package excluding `opencohost/ui/`. Of those, `_log_clause_sanitizer` (`:5376`)
fires only on a non-clean verdict, and `:1707`/`:1730` only on pregen retry/abandon. So the DEBUG
delta is additive and small, not a multiplier. **THEORETICAL.** If you want the real number, note
your log's byte size at the start and end of the session.

### 2.3 Ollama subprocess logs — the largest unbounded pair on disk [MEASURED]

`opencohost/core/ollama_startup.py:87` — `open(self._stdout_log_path, "a", encoding="utf-8")`.
Mode `"a"`, no rotation, no truncation, no size cap. Wired from `api/engine_host.py:359-360` and
`ui/model_panel.py:754-755`.

```
$ ls -la logs/ | grep ollama_startup
-rw-r--r-- 1 tavo_ 197609 10471867 Jul 30 04:42 ollama_startup_stderr.log
-rw-r--r-- 1 tavo_ 197609 11263522 Jul 30 01:32 ollama_startup_stdout.log
```

**21.7 MB combined, already.** Growth rate is Ollama's own verbosity per model load/unload:
**could not determine** from this repo. Contrast `logs/api_audit.jsonl`, which *does* rotate —
`.1`/`.2`/`.3` all present at ~5,242,8xx B, matching the `maxBytes=5*1024*1024, backupCount=3`
in `api/observability.py`. Severity: **low but monotonic** — it never shrinks, and no code prunes
it. Not a session risk; a disk-hygiene item for the U4 follow-up.

### 2.4 In-memory collections — checked and cleared [MEASURED reads]

I list these so you do **not** spend the session worrying about them. Every one is capped in
source, and I read each cap:

| Collection | Cap | Line |
|---|---|---|
| `historial` | `deque(maxlen=HISTORY_MAX_TURNS*2)` = 20 messages | `llm_engine.py:572`; `settings.py:58` (`= 10`) |
| `_memory_digest` | `MemoryDigest` FIFO, `DEFAULT_MAX_CHARS = 600` | `memory_digest.py:34`, eviction `:44-54` |
| `_session_memoria_titles` | `_SESSION_MEMORIA_TITLES_CAP = 40`, `del [0]` on overflow | `llm_engine.py:130`, `:4929-4931` |
| `_priority_queue` | `_pq_max_items = 5`, `pop()` while over | `:637`, `:1513-1514` |
| `_accumulation_buffer` | `_accum_max_items = 50` **and** `_accum_max_chars = 2000` | `:643-644`, `:2166-2172` |
| pregen cache | **one slot**, text only, no audio | `_prefetched_agenda`, `:1747-1754` |
| `_reasoning_model_cache` | one entry per model tag | `:433`, `:4590` |
| `_model_ctx_limit` / `_ctx_show_cache` | one entry per model tag | `:4503-4515`, `:4531-4556` |
| `last_outputs` | last 5 | `kira_agenda_controller.py:1005` |
| `_rejected_phrases` | cleared on every accepted output **and** every topic transition | `:1112`, `:830`, `:754-764` |
| `RecoveryPolicy._reasons` | `MAX_HISTORY = 20` | `:92`, `:109` |
| `self.topics` | grows only via `add_topic`/`suggest_topics`; `_suggestion_session_cap = 5` and `SCOUT_ENABLED=False` | `:468`, `settings.py:69` |
| API log sink | `_Drain`, a no-op `put` | `api/engine_host.py:102-113` |
| `ChatReplySink` | `deque(maxlen=16)` | `api/engine_host.py:161` |
| `EventLogSink` | `deque(maxlen=200)` | `api/engine_host.py:202` |

Two more that matter specifically for an agenda soak:

- **The memoria draft table does not grow at all.** `_DIGEST_CAPTURE_SOURCES = frozenset({"direct","ptt"})`
  (`llm_engine.py:94`), and `_build_memoria_draft` gates on it at `:4837`. Agenda turns produce
  **zero** memoria rows. `_commit_history`'s eviction-capture branch is additionally gated
  `and not source.startswith("kira-agenda")` at `:4707`. So a pure agenda session writes no
  drafts — the memoria table is a PTT/direct concern only. **MEASURED** (read both gates).
- **Agenda SQLite persistence is fingerprint-gated and tiny.** `save_if_changed`
  (`agenda_persistence.py:79-158`) does `DELETE` + `INSERT` of the whole ≤10-row table, but only
  when the fingerprint changes (`:106-108`), and `_snapshot` (`:244-273`) **excludes**
  `turns_spoken` and keeps only `_PERSISTED_STATUSES = ("approved","queued","active")` (`:55`).
  So it writes roughly once per topic completion — ~10 writes per session, not per turn.
  Side note: its only call sites are `ui/agenda_audio_controller.py:661` and `ui/app_shell.py:358`
  — **I found none under `opencohost/api/`**, so on Tauri the queue is probably not persisted at
  all. Not a leak; flagged in §5 as an unverified gap.

Only one unbounded collection survived the sweep, and it is negligible — §2.6.

### 2.5 Prompt growth is a step, not a drift [MEASURED trend, THEORETICAL bound]

ADR-035 §3 records **MEASURED**: decode 16 → 34 ms/tok as the prompt grew ~1,500 → ~2,900 tokens
over a ~12-minute session, and calls it "a margin-shrinking trend … worth tracking."

Reading the code bounds that trend: `historial` is `deque(maxlen=HISTORY_MAX_TURNS*2)` = **20
messages = 10 pairs** (`llm_engine.py:572`, `settings.py:58`). Agenda turns *do* enter it — both
entries appended at `:4767-4771`, with the user slot masked to a fixed sentinel string
(`:4681-4682`). So the history block reaches its maximum size after ~10 committed turns and then
**stops growing**: the oldest pair is evicted for each new one.

**THEORETICAL conclusion**: the 2.1× decode slowdown is a **one-time step reached in the first
~10 turns**, not a many-hours degradation. Turn 200 should decode at the same rate as turn 15. If
you observe latency still climbing at hour three, that contradicts this reading and is worth
reporting — it would mean something outside `historial` is accumulating into the prompt.

What does *not* self-bound is the **margin** at that saturated rate: §1.8.

### 2.6 `rejection_log` — the only unbounded per-event append [THEORETICAL, negligible]

`KiraAgendaController.rejection_log: list[dict]` at `:461`, appended at `:1106`. **Never trimmed
and never cleared** — not by `emergency_stop` (`:766-773`), not by `_reset_ladder_state`
(`:754-764`), not by `enable` (`:726-743`); a grep for `rejection_log` across `opencohost/` finds
only the declaration, the one append, and three readers (`:1121`, `:1126`, `llm_engine.py:4625-4648`).

- **Growth rate**: one small dict per **rejected** turn, not per turn. 5-8 short keys.
- **Value at turn 200**: bounded by the rejection count, which is unmeasured. Even at a pessimistic
  50% rejection rate that is ~100 dicts — order of tens of KB.
- **Secondary cost**: `get_metrics()` (`:1119-1140`) rescans the whole list on every call, O(n).
  Trivial at n≈100; it would matter in a process running for days.
- **Verdict**: real, correctly identified as unbounded, and **not worth acting on for this
  session.** It only becomes interesting in a process that never restarts.

### 2.7 Threads — zero orphans on a clean run, one per timeout [THEORETICAL]

`_call_with_watchdog` (`llm_engine.py:3789-3819`) spawns a named daemon thread per call and, on
timeout, `raise TimeoutError` at `:3816` **without joining** — the worker is abandoned, alive until
the underlying blocking call returns. That is the whole point (a hang raises nothing; ADR-041 R3),
and it is the correct tradeoff. But it means **one orphan thread per timeout**, with no cap.

Per-turn thread inventory on the agenda path, all daemon, all normally self-terminating:

| Thread | Site | Per turn |
|---|---|---|
| chat watchdog worker (via `_ollama_chat_with_watchdog`) | `:3821-3824` → `:3809` | 1 |
| TTS producer (`productor`) | `:5704-5705` | 1 |
| pregen worker | `:1777-1784` | 0-1 |
| CTK prefetch speaker | `:1879` | CTK only |

**At 200 turns with zero timeouts: zero orphans.** With a stalling model, orphans accumulate at
one per timeout for as long as the stall persists. This is *not* the U1 scout case from
`runtime_findings_followup_20260730/proposal.md` — that one is genuinely unbounded per dispatch,
but inert because `SCOUT_ENABLED = False` (`settings.py:69`).

The metadata probe is bounded and memoised twice over: `_fetch_show` (`:4518-4557`) runs behind
`_ctx_show_cache`, is reached through `_discover_model_ctx` (`:4493-4516`) and
`_resolve_reasoning_classification` (`:4578-4591`), and its own `ponytail:` comment at `:4547-4549`
names the accepted 2× cost (`OLLAMA_REQUEST_TIMEOUT = 5`, `settings.py:522` → ~10 s worst case,
**once per model tag per process**).

**Cheap instrument, if you want it**: note the OpenCohost process's thread count and working set in
Task Manager at start and at end. One number each. Turns this whole row from THEORETICAL into
MEASURED for the price of two glances.

### 2.8 Audio device and mixer — bounded, and the old hazard is closed [MEASURED reads]

- **No channel accumulation.** Playback uses the single `pygame.mixer.music` channel:
  `load` → `play` → busy-wait → `unload`, per chunk (`:5768-5788`). No `Channel` objects are
  allocated per turn.
- **Re-init is flag-gated, not per turn.** `:5713-5722` consumes `_audio_reinit_needed` once and
  only then calls `mixer.quit()/init()`. Its comment explains why it is not unconditional (CTK's
  `AudioBedEngine` shares the mixer). The PTT voice-death fix behind it was owner-validated over a
  53-minute 11-turn session (`AGENT_HANDOFF.md:96-101`).
- **ADR-035 §4's audio-overlap residual is closed on the surface you will run.**
  `play_prefetched_agenda` — the parallel speaker thread named as the hazard — now has exactly one
  caller: `ui/agenda_audio_controller.py:385`. The API host goes through `_speak_pregenerated`
  (`:1963`+), whose docstring says "minus the parallel thread: the worker already owns the turn."
  And `_hablar` is now a lock wrapper (`:5384-5402`, `_hablar_lock` at `:701`) that serializes
  every caller, so even the CTK path is guarded. **MEASURED** (grep for callers + read the lock).
  Known unfixed: `pygame.mixer.init()` at `:1217` still has no headless fallback and its `except`
  kills the engine thread — already registered in `AGENT_HANDOFF.md:413`.

### 2.9 Summary table

| # | Finding | Label | Growth per turn | At turn 200 | Bites this session? | Severity |
|---|---|---|---|---|---|---|
| 2.1 | TTS temp files leak; sweep exists but only in CTK | MEASURED (133 files / 2.96 MB today) + THEORETICAL (mechanism) | could not determine | could not determine | **Yes, plausibly** | Med |
| 2.2 | Engine log unrotated | MEASURED at INFO (19-31 KB/h); DEBUG rate could not determine | ~0.3-0.5 KB/min | ~150-250 KB at 8 h | No | Low |
| 2.3 | `ollama_startup_std*.log` append-only, no rotation | MEASURED (21.7 MB today) | could not determine | could not determine | No | Low |
| 2.5 | Prompt/decode step (16→34 ms/tok) | MEASURED trend, THEORETICAL bound | saturates at 10 pairs | same as turn 15 | Only via §1.8 margin | Med |
| 2.6 | `rejection_log` unbounded | THEORETICAL | 1 small dict per **rejected** turn | tens of KB | No | Low |
| 2.7 | Watchdog orphan threads on timeout | THEORETICAL | 0 on a clean run; 1 per timeout | 0 if no stalls | Only under stalls | Med if stalling |
| 1.8 | Pregen margin inverts if generation > speech window | THEORETICAL from MEASURED inputs | n/a | n/a | **Yes, audibly** | Med |
| 1.4 | Tauri/API session completion is silent | THEORETICAL | n/a | n/a | Cosmetic | Low |
| C1 | Checklist asks for a never-logged `verdict=clean` count | MEASURED | n/a | n/a | **Yes — blocks the gate** | **High** |
| 2.4 | 15 collections + memoria drafts + SQLite: all bounded | MEASURED reads | 0 | 0 | No | — |

---

## 3. Q3 — What a unit test can and cannot validate here

### 3.1 (a) What a fast deterministic test CAN prove about a 200-turn session

All of this is drivable with a stub LLM and no clock at all, because the controller is explicitly
written for it — its class docstring says so: *"Public methods are intentionally event-oriented so
tests and UI wiring can drive the controller without a real clock or background thread"*
(`kira_agenda_controller.py:241-245`).

Provable, and **already pinned**:

| Claim | Existing test |
|---|---|
| Batch counting never exceeds the global max | `tests/test_kira_agenda_controller.py:244-261` — asserts `turns_spoken` 2 → 3 with `batch=2, max=3`, then the `kira-agenda-stop` action |
| Turn limit clamps to 20 / 1 | `:225-241` |
| Topic closes after max turns | `tests/test_agenda_driver.py:310` |
| Queue advances after a rejected closing turn | `tests/test_agenda_driver.py:586` |
| Terminal state reached on empty queue | `tests/test_agenda_driver.py:329`; CTK mirror `tests/test_agenda_audio_shell_characterization.py:79` (also asserts the "Sesión completada" log) |
| Prefetch preview does not mutate state / staleness / consume-before-enqueue / stop-turn full cycle | 17 tests, `tests/test_agenda_driver.py:657-1076` |
| Force-exhaust on repeated character breaks | `tests/test_kira_agenda_controller.py:1573` |
| Trailing-empty after force-complete keeps IDLE (the "None-loop") | `:1642` |

`tests/test_kira_agenda_controller.py` alone holds **87** test functions.

Also provable, and cheap: bounded-collection invariants — "after N appends, `len(x) <= cap`" — for
every row in §2.4. Whether they are *worth* pinning is §3.3.

### 3.2 (b) What ONLY a live run can catch

- **Whether the pregen margin holds** (§1.8). This is the single most important live question. A
  stub LLM returns instantly, so a test can *never* observe generation outrunning the speech
  window. Only real decode against a real prompt at real saturation answers it.
- **Whether a `repaired` turn still sounds like Kira** — the whole reason ADR-039's gate is a human
  gate (`docs/runtime-validation-20260730.md:43-74`).
- **How long a reject's silence actually feels** (§C of the checklist).
- **Audio device behaviour over hours** — WASAPI stream migration, device switch, mixer zombie.
  The `_audio_reinit_needed` recovery exists precisely because this class of failure is invisible
  to tests (`llm_engine.py:5798-5807`).
- **The temp-file leak rate** (§2.1) — needs a real `pygame` holding a real handle on real Windows.
- **The DEBUG log rate** (§2.2).
- **Orphan thread count under a genuinely busy daemon** (§2.7).
- **GPU/VRAM** — nothing in this repo measures it. `AGENT_HANDOFF.md:381` is explicit that NIM has
  **zero** latency measurement and that the Ollama RTX-3060 figures in `ADR-013:59-67` must never
  be transferred to it.

### 3.3 (c) Is a 200-turn simulated soak test worth writing?

**No. Do not write it.** Ponytail mode; here is the honest reasoning rather than a shrug.

**What it would assert, and why each assertion is already covered:**

1. *Every queued topic reaches `COMPLETED` exactly once.* → composition of
   `test_topic_completes_after_max_turns` + `test_auto_exit_to_off_on_empty_queue`. I also just
   drove it for real in §1.5 (10/10 completed) in about 0.3 s from stdin, with no file created.
2. *`turns_spoken` never exceeds `max_turns_per_topic` for any topic.* → this is a `min()` at
   `:918-921`, pinned by `:244-261`. A 200-iteration loop cannot falsify a `min()` that a
   3-iteration loop already exercises at the boundary.
3. *No collection exceeds its cap after 200 turns.* → each cap is a `while len(x) > N: pop()` or a
   `deque(maxlen=…)`. A soak asserting `len(historial) <= 20` after 200 turns is testing CPython's
   `deque`, not this codebase.
4. *Terminal state is `OFF`.* → `test_agenda_driver.py:329` and
   `test_agenda_audio_shell_characterization.py:79`.

**And what it would cost**: the soak would need a stub for the LLM, TTS, `pygame`, and the driver
thread. That stub set is exactly the surface where the *real* risks live (§3.2), so the test would
be maximally mocked precisely where it matters least, and would join the maintenance burden of
whichever of those four seams changes next. A green 200-turn soak would also read as reassurance
about a many-hours session, which it structurally cannot provide — and manufacturing that kind of
false confidence is what ADR-041 exists to prevent.

**The one narrow test I would defend, if you want one** — and I would still skip it:

> A ~15-line drive in the *existing* `tests/test_kira_agenda_controller.py`, 3 topics × 4 turns
> at `batch=2`, through the **prefetch path** (`prefetch_action_after_current_speech` +
> `start_prefetched_action`), asserting `[t.turns_spoken for t in topics] == [4,4,4]` and all three
> `COMPLETED`.

Its only real justification is the honest scope limit I flagged in §1.5: my measurement drove the
plain `next_action` route, and the API host uses the prefetch route on *every* boundary. But
`test_stop_turn_prefetch_full_cycle_no_topic_closing_deadlock` (`test_agenda_driver.py:870`)
already drives that cycle end to end, and 16 sibling tests cover its guard chain. So the residual
value is a **composition** check across a topic boundary, which is thin.

**No new file either way.** If it is ever written it belongs in the existing agenda test module.

**What to do instead of writing the soak — three lines of instrumentation, zero code:**

| Instrument | Cost | Converts |
|---|---|---|
| `ls E:\VoiceAI\temp | Measure-Object` before and after | 2 commands | §2.1 THEORETICAL → MEASURED, with a per-turn rate |  <!-- path-ok: local env example -->
| Engine log byte size before and after | 2 glances | §2.2 "could not determine" → MEASURED DEBUG rate |
| Thread count + working set in Task Manager, before and after | 2 glances | §2.7 THEORETICAL → MEASURED |

Those three turn the session you are already going to run into the measurement instrument, which
is strictly more information than any soak test can produce, for strictly less work. Add them to
`docs/runtime-validation-20260730.md` alongside the C1 fix.

---

## 4. What I could not determine

Stated as gaps, not filled with reasoning:

1. **The DEBUG-on log growth rate.** No `OPENCOHOST_DEBUG=1` real-session log exists in `logs/`.
   *Needed*: your session's log size at start and end. Bounded from above by the 21 `logger.debug`
   sites outside `opencohost/ui/`, but not quantified.
2. **Provenance of the 133 leaked temp files.** Mixed size/index histogram, and pytest's
   `--basetemp` writes into the same tree. *Needed*: C2's clear-then-count.
3. **Which `safety_mode` the ADR-035 session ran.** Its measured 58-73 s windows exceed
   `live_safe`'s own annotated 25-40 s band, so it was not the default. The ADR does not say.
   *Needed*: one line in the ADR, or the mode from the log.
4. **Ollama subprocess log growth rate.** Ollama's own verbosity, not this repo's.
   *Needed*: file sizes before and after a session.
5. **The guardrail rejection rate per 100 agenda turns.** Nothing in the repo records it; this is
   what drives both the reject-dead-air cost (§1.7) and `rejection_log` growth (§2.6).
   *Needed*: the session's `[CLAUSE_SANITIZER]` and validator counts.
6. **Thread count and RSS at turn 200.** *Needed*: the live session, or a heavily-stubbed soak I am
   recommending against.
7. **Whether the API/Tauri host persists the agenda queue at all.** `save_if_changed`'s only call
   sites are under `opencohost/ui/`. I did not trace whether the API host has a different
   persistence mechanism, so I am not claiming it has none. *Needed*: one grep over
   `opencohost/api/` for an alternative writer.
8. **Whether `_hablar`'s exception path actually leaves a Windows handle open.** The mechanism reads
   that way (§2.1) but I did not reproduce it. *Needed*: C2's count, or one forced playback failure.
9. **Real per-turn wall clock in `live_safe` mode.** Every figure in §1.7's `live_safe` rows comes
   from a source annotation, not a measurement, and those annotations are internally inconsistent
   as a rate. *Needed*: your `[TURN_LATENCY]` medians plus perceived speech duration.

---

## 5. Out-of-scope note, filed and dropped

Per the owner's 2026-07-29 surface-priority ruling and ADR-041 R7: `runtime_check()` in
`opencohost/config/validation.py` (viewer-chat preview logging) is unrelated to this session's
surfaces and is not assessed here. Filed against the unmigrated-chat track; no further attention.

---

## 6. Related documents

- `docs/adr/ADR-041-verification-discipline-for-inherited-claims.md` — the rules this review is
  written under.
- `docs/adr/ADR-035-agenda-dead-air-prefetch-overlap.md` — the only measured latency figures for
  this path (§1.1, §3, §4).
- `docs/adr/ADR-039-intra-speech-clause-repetition-sanitizer.md` — the tier under validation and
  its own gate.
- `docs/runtime-validation-20260730.md` — the checklist. **Fix its `clean` column before use (C1).**
- `conductor/tracks/runtime_findings_followup_20260730/proposal.md` — U1 (scout probe, blocker on
  `SCOUT_ENABLED`), U4 (log rotation, held until after this session), U5 (2× probe cost, recommended
  skip). Nothing in this document re-reports those as new.
