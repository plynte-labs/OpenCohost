# OpenCohost Agent Handoff

This is the first file an AI agent should read when starting work in this repo.

## Current operating mode

**Less expansion, more controlled validation.**

Do not start new feature work by default. The near-term goal is release readiness:
validate the existing prototypes, reconcile documentation with reality, and reduce
runtime uncertainty before packaging or broad product polish.

## Start-of-session checklist

1. Read `AGENTS.md` for repo rules.
2. Recover prior session context from your memory tooling if available.
3. Read `sdd/session-status-2026-06-04.md` for the latest SDD checkpoint.
4. Check `git status --short` before editing; do not overwrite user changes.
5. If the request touches SDD/Conductor work, inspect the relevant track/spec before coding.


## TESTING DEBT STATUS

**PTT + LiveAudio voice-death — CLOSED by owner 2026-07-16.** The TTS playback-recovery
fix (llm_engine.py `_audio_reinit_needed` + `mark_audio_suspect()`, ptt_session.py
`on_audio_suspect` hook, wired in api/main.py; unit-tested green) was validated in real
runtime: a 53-minute 11-turn F10 session with a mid-session profile switch — mixer
re-init engaged on all 11 turns, zero voice incidents, zero non-200 responses (full log
analysis 2026-07-16). Owner closed the debt the same day.

Deferred, explicitly NOT relevant yet per owner (2026-07-16): heavy-model recovery gate
(`heavy_model_inference_recovery` — the session ran a light model, so that gate remains
unexercised) and Gate 3 (kill-mid-hold watchdog proof). Revisit before release, not now.

## LATEST SNAPSHOT — 2026-07-29 (clause_sanitizer V1 IMPLEMENTED, suites green, UNCOMMITTED)

**Owner approved the plan on 2026-07-29 and it is now implemented. Nothing committed.**

### What landed
| File | State |
|---|---|
| `opencohost/core/repetition_guard.py` | `sanitize_clause_repetition()` + `ClauseSanitizeResult` + 6 `SANITIZE_*` constants. `detect_repetition` untouched |
| `opencohost/config/settings.py` | `CLAUSE_SANITIZER_SOURCES` (agenda only) + `_parse_clause_sanitizer_sources()` + unknown-token WARNING |
| `opencohost/core/llm_engine.py` | seam before `output_guard`; repair-only re-pass at the pregen connector junction; `_log_clause_sanitizer()`; `[TURN_LATENCY]` span |
| `docs/adr/ADR-011-…md` | Addendum 2026-07-29 — intra-speech tier below D1; bounded-retry question ANSWERED (zero new budget) |
| `pytest.ini` | new `live_cloud` marker |
| tests | `test_clause_sanitizer.py` (33) · `_seam.py` (26) · `_e2e.py` (3) · `tests/live_cloud/` (3, gated/skipped) |

### FULL SUITE, 2026-07-29: **4899 passed · 1 failed · 14 skipped** (4914 collected, 3m54s)

The single failure is **`test_profile_tone.py::TestProfileExistence::test_legacy_profiles_preserved`,
and it is PRE-EXISTING** — proven by stashing the three production files and reproducing it
identically.

**Two claims in the first version of this diagnosis were wrong. Corrected 2026-07-29 after
verifying against source and history:**

- It does **NOT** fail on CI. `test_legacy_profiles_preserved` carries a `skipif` on
  `os.path.isfile(PERFILES_FILE)`, so on a clean checkout it **skips**. It fails only on a
  machine that has a `perfiles.json` — i.e. only the owner's.
- The names are **not** phantoms. `git show 924ea5a^:perfiles.json` lists `Hagg` and
  `Akira (Uncensored)` as real, tracked profiles. Commit `924ea5a` untracked the file as
  runtime user state; the owner's copy has since diverged — `Akira (Uncensored)` appears to
  have been **renamed to `Akira (Unchained)`** and `Hagg` removed, with `Chat` and
  `Default EN` added. The test is a stale snapshot of one machine, not a broken contract.
- The earlier recommendation — "switch the test to read `default_profiles.json` only" —
  **would also fail**. That file has no `Gemma` and no `Vacio`, both of which the assertion
  requires. Do not apply it.

Real state: asserted list is `["Akira", "Akira (Learn)", "Gemma", "Hagg", "Vacio",
"Akira (Uncensored)"]`; the owner's file is missing exactly `Hagg` and `Akira (Uncensored)`.
The test reads the owner's **gitignored** user state (`perfiles.json`) as its primary source,
so it can never be a stable contract test whatever list it asserts. Needs one owner decision;
see the pending-decisions file.

**Correction to an earlier note in this snapshot:**
`test_interactive_pregen.py::test_llm_generating_flag_brackets_the_ollama_call` **fails in
isolation but PASSES in a full run** — it is *order-dependent*, not simply stale. `_bare_motor()`
never sets `is_ready`/`pygame` and another test leaves the state behind. Fix is to make the helper
self-sufficient like `_chat_motor()` in `tests/test_dialogue_callback.py:26-34`.

### Documentation written this session — read these before re-deciding anything
| Artifact | What it holds |
|---|---|
| `docs/adr/ADR-039-intra-speech-clause-repetition-sanitizer.md` | The tier: 10 decisions **with their justifications**, two verified mermaid pipeline traces, the audit results, the consequences including the pregen cost, and the runtime validation gate |
| `docs/adr/ADR-040-deferral-policy-clause-sanitizer-residuals.md` | **Why each residual became a proposal instead of code** — an eight-category deferral taxonomy, each with its own re-entry rule. Read this before reopening any deferred item |
| `conductor/pending-decisions-20260729.md` | **Consolidated INDEX of every open decision** (~26 items, grouped by what kind of answer they need). Starts with three record defects, including one real contradiction about the heavy-model gate |
| `conductor/tracks/residual_cleanup_20260729/proposal.md` | The safe cleanup loop **with a paste-ready prompt** and a binding live-validation safety gate |
| `conductor/tracks/log_privacy_hygiene_20260729/proposal.md` | Log leak inventory. **Nothing ships logs off the machine — NOT FOUND** |
| `conductor/tracks/sanitizer_language_scope_20260729/proposal.md` | Multilingual audit + 6 property invariants replacing per-language fixtures |

Also fixed: **ADR-011's status line** read "Proposed — not yet implemented" for five weeks while
its own update log recorded the ladder shipped and validated. Corrected.

### NEXT ITERATION — the order
1. **Owner: run a real agenda session.** This is the gate. Report `[CLAUSE_SANITIZER]` verdict
   counts, `[TURN_LATENCY]` medians split by verdict, and whether any `repaired` turn removed
   something it should not have. Nothing else closes ADR-039 for runtime.
2. **In parallel, the residual loop** — R1→R4 from `residual_cleanup_20260729`, one unit per cycle,
   design/fable → apply/sonnet → verify/sonnet → trace/sonnet. The safety gate is binding: **no
   unit may modify a code path that executes during a live agenda session.**
3. Answer §0 of the pending-decisions file (three cheap record fixes, one real contradiction).
4. Then, and only then, threshold tuning — on the telemetry from step 1, never on intuition.

### Still open on this unit
- Threshold tuning needs `[CLAUSE_SANITIZER]` telemetry from a **real agenda session** (owner runtime).
- Live NIM tests are written but never run: `OPENCOHOST_LIVE_CLOUD_TESTS=1 pytest tests/live_cloud -s`.
- Dead-air risk stays **NOT RESOLVED** until `[TURN_LATENCY]` produces real NIM numbers.
- Two adjacent defects found by the pipeline trace, **registered not fixed**: the chat-repetition
  discard (`llm_engine.py:3516-3524`) speaks a fallback line and updates last-reply but **never
  calls `_commit_history`** — the sibling guardrail path four lines earlier does, and its comment
  names that as the D4 `memoria_quality_20260717` fix for exactly this bug class. And a pregenerated
  draft never passes `_accept_agenda_output` (the gate at `:3526` requires `commit_history`); its
  only validation is the *different* `_preview_accept_agenda_output` (`:1700`).

---

**Original plan section, kept as the scope contract:**

### Read these two, in order, before touching anything
1. `temp/plan_v1_corrected.md` — the approved plan. Authoritative.
2. `docs/deferred-20260729-clause-sanitizer-scope.md` — what is out of scope and why, with citations.
   Several rows are **BLOCKED-BY-EVIDENCE**; do not pick them up.

Superseded, keep only as history: `temp/revised_plan.md`, `temp/evidence_package.md`.
Engram: `architecture/clause-sanitizer-v1-scope` (the decision), `architecture/clause-sanitizer-evidence-package`
(the audit), `architecture/clause-sanitizer-intra-speech` (the original design).

### The defect
A real ~16-min agenda session emitted, inside ONE sentence:
`"No había roadmap, no había monetización, no había roadmap, no había roadmap."`
A comma-delimited clause repeated 3×, one repetition non-contiguous. Every existing guard is
structurally blind to it: `has_looping_lines` splits on `[.!?¡¿]+` and filters ≤24 chars;
`detect_repetition` compares whole candidates across turns; `trim_trailing_repeated_sentences`
compares only against `recent_texts`; TTS comma-chunking runs after all guards.

### V1 scope (owner-decided 2026-07-29 — do not widen)
- Implemented **once**, in the shared backend. `opencohost/ui/` is read-only legacy reference.
- Armed by default **ONLY** for `kira-agenda` + `kira-agenda-stop`.
- Disarmed for `chat`, `direct`, `ptt`, `accumulated` — config preserved so they can be armed later
  on real evidence. **The earlier "all sources" justification was a fallacy** (absence of a guard
  ≠ a defect) and there is a real false positive on operator-facing sources: an instructed
  repetition (`"repetí tres veces …"`) would be silently collapsed.
- **No** history comparison. **No** intent detection — no regex, no phrase list, no LLM call.
- Exact normalized clause equality **within a single sentence** only.
- **Tier 2 (reject → regenerate) is agenda-only BY CONSTRUCTION, not by config**: it needs an owner
  for the regeneration and only agenda has one (the ADR-011 ladder via `_accept_agenda_output("")`,
  `llm_engine.py:5240`). Elsewhere the sanitizer is repair-only even when armed; the verdict is still
  logged so evidence accrues.

### Where the code goes
| File | Change | ~Lines |
|---|---|---|
| `opencohost/core/repetition_guard.py` | `_SENT_SPLIT_RE`, `_CLAUSE_SPLIT_RE`, 6 `SANITIZE_*` constants, frozen `ClauseSanitizeResult`, pure `sanitize_clause_repetition()` reusing existing `_norm` (`:73-76`) + `_tokens`/`_WORD_RE` (`:79-80`, `:46`). `detect_repetition` untouched | +65 |
| `opencohost/config/settings.py` | `_parse_clause_sanitizer_sources()` + `CLAUSE_SANITIZER_SOURCES` defaulting to the two agenda sources + unknown-token WARNING | +12 |
| `opencohost/core/llm_engine.py` | ~10-line seam block **immediately before `:3399`** (the `output_guard` call); repair-only re-pass after `:1979`; `_log_clause_sanitizer()`; 2 instrumentation lines | +22 |
| tests | `tests/test_clause_sanitizer.py`, `_seam.py`, `_e2e.py`, `tests/live_cloud/` (gated) | ~430 |
| `docs/adr/ADR-011-…md` | addendum: intra-speech tier below D1, agenda-only V1 scoping, retry-count question answered | +18 |

**Untouched:** `kira_agenda_controller.py`, all of `opencohost/ui/`, `_retry_after_guard_block`,
`_accept_agenda_output`, `_ejecutar_inferencia`, `output_guard` internals, `detect_repetition`.
Production ≈ 99 lines. Zero new modules.

### The seam (verbatim shape)
```python
if source in CLAUSE_SANITIZER_SOURCES:
    san = sanitize_clause_repetition(dialogo)
    if san.verdict != "clean":
        self._log_clause_sanitizer(san, source)
    if san.verdict == "rejected" and source.startswith("kira-agenda"):
        if commit_history:
            self._invalidate_pregen_epoch()
        return ""
    dialogo = san.text
```

### Thresholds (all `SANITIZE_*` in repetition_guard.py)
`FRAGMENTS=5` (always) · `DISTINCT=2` (keys with ≥2 drops) · `REMOVED_PCT=0.30` (gated:
`drops>=3` AND `original_len>=300`) · `MIN_REMAINING_CHARS=100` (gated: `original_len>=300`) ·
`SHORT_EXPR_MAX_CHARS=12` (eligibility) · `LENGTH_AXES_FLOOR=300`.

```
rejected = drops>=5 or distinct>=2
         or (drops>=3 and original_len>=300 and removed_pct>=0.30)
         or (original_len>=300 and remaining_len<100)
```
The gates exist because an earlier draft's own thresholds **rejected its own acceptance case**;
three independent review lenses proved it. The incident above must classify **`repaired`** →
`"No había roadmap, no había monetización."` (40 chars from 76; drops=2, distinct=1,
max_occurrences=3). Assert that arithmetic in a test.

### The three real destinations (there is NO transcript, and NO STT in this repo)
`_commit_history` `:3494` (history/context — `self.historial` deque `:567`, in-memory) ·
`_emit_dialogue` `:5219` (last-reply sink → `ChatReplySink` `api/engine_host.py:150-176` →
`GET /api/chat/last-reply`; **no-op in the CTk path**, `dialogue_callback` defaults to `None` `:426`) ·
`_hablar` `:5221` (TTS boundary — a thin `_hablar_lock` wrapper at `:5280-5298`, so it IS the
str-receiving seam; mock it like `tests/test_dialogue_callback.py:40`, **no real audio**).
All three read the SAME `dialogo` local — verified in the pregen path too (`:1983`/`:1999`/`:2017`).

### Two paths, not one — the requested single flow does not exist
Foreground has **no** connector concatenation. The pregen path is the only one with a connector,
and there concatenation (`_speak_pregenerated:1979`) happens **AFTER** the guardrails, with history
at `:1983`, last-reply `:1999`, TTS `:2017` — and guardrails do **not** re-run after concatenating
(`:1994-1997`, pre-existing). Hence the repair-only re-pass after `:1979`: no regeneration
machinery exists there, so a reject could only stall playback.

### Execution order
1. Sanitizer + config + telemetry + seam + repair-only re-pass, with the plan §8 tests.
2. Run, with `E:/Miniconda/envs/flux_env/python.exe` and
   `-q -p no:cacheprovider --basetemp=E:/VoiceAI/temp/pytest-piper-clean`:
   the new files → then `test_repetition_guard.py test_chat_repetition_guard.py
   test_kira_agenda_controller.py test_cohost_repetition_ladder.py test_kira_orchestration_gaps.py
   test_validation.py test_dialogue_callback.py test_pregen_pop_cache.py test_interactive_pregen.py`
   → then the CLAUDE.md focused suite (`test_llm_tiers.py test_model_panel.py
   test_heavy_model_inference_recovery.py`).
3. Report, including telemetry samples from a real agenda session for the tuning pass.
4. Interruption state machine design (4 sources). 5. Stream/Tauri migration.

### Traps verified this session — do not re-litigate
- **`max_intentos = 2` is NOT a transport retry.** Both exception branches `return ""` on the FIRST
  failure (`:3160-3183` watchdog, `:3184-3234` transport); neither has `continue`. Two comments
  claim otherwise (`:3405`, and `:3211` logs an attempt that never happens).
- **`output_guard` has ZERO repetition rules** — all 9 (`validation.py:456-577`) are content-safety;
  `STREAMER_SOURCES` gates only R10/R11. 68 tests in `test_validation.py`, none about repetition.
- **Do NOT add `record_success()` at `kira_agenda_controller.py:966`.** It makes `failure_count`
  oscillate 0→1→2→0 in CLOSING so the force-complete at `:958` (`>=3`) is unreachable and the topic
  never closes — the exact "20+ LLM calls in a row" cascade `:954-957` records as fixed. Also breaks
  the pinned assertion at `tests/test_kira_orchestration_gaps.py:742`. See deferred doc §1.1.
- **NIM has ZERO latency measurement.** `settings.py:105-108` calls `CLOUD_CHAT_TIMEOUT=90` a
  placeholder. Never transfer the Ollama RTX-3060 figures (`ADR-013:59-67`) to NIM.
- **Front provider config is live:** `active_provider=nvidia_nim`, `base_url=https://integrate.api.nvidia.com/v1`,
  model `z-ai/glm-5.2`, key present in `config/llm_keys.json` (field `api_key`, read via
  `OAuthStore(LLM_KEYS_FILE)`, `llm_engine.py:3669-3674`). `tests/conftest.py:238-268` is autouse and
  redirects that path to `tmp_path` for every test — a live test must opt out. **Never log the value.**

### Adjacent defects registered, NOT to be fixed here
`tests/test_interactive_pregen.py::test_llm_generating_flag_brackets_the_ollama_call` fails on
pristine code: `_bare_motor()` never sets `is_ready`/`pygame`, so `_generar_dialogo` bails before
the stubbed `_ollama_chat_with_watchdog` ever runs. Stale harness, not a production defect — but it
means the `llm_generating` bracket is currently UNVERIFIED. ·
**Raw-text log inventory (audited 2026-07-29, corrected).** Off-machine shipping: **NOT FOUND** —
`ui/crash_reporting.py` lists log *filenames* only, never contents; the only `requests.post` targets
are YouTube chat, the cloud LLM provider, and localhost TTS. Every leak below is **local disk only**.
Ranked by the project rule ("never expose raw *chat*"), which targets VIEWER text, not Kira's own:
1. **`validation.py:397,405,414,423`** — `preview=text[:100]` of VIEWER chat at WARNING, ungated, in
   `runtime_check()`. **Dead code today** (only its own test calls it) — a landmine, not a live leak.
   One call site away from violating the rule. Fix: `chars=len(text)`. THE one worth doing.
2. **`ui/voice_control.py:216`** — 30 raw chars of PTT-transcribed OPERATOR speech at INFO, ungated
   (`:239` logs it untruncated at DEBUG). Real, live. In `opencohost/ui/` → out of bounds for the
   sanitizer unit; own micro-unit.
3. `validation.py:465-575` (9 sites, `preview=response[:120]`) and `llm_engine.py:3542`
   (`dialogo[:200]`, gated on `OPENCOHOST_DEBUG=1`) are **Kira's OWN output** — the category the rule
   explicitly permits (R8 forbids raw viewer chat). Consistency cleanup, NOT a privacy violation.
4. Retention hygiene: `config/logger.py` uses a plain `FileHandler` — the engine log never rotates
   and is never pruned (`temp_file_cleanup.py` only cleans TTS audio). `validation.log` and
   `api.log` DO rotate (5MB×3). Same for the `server_qwen_*`/`ollama_startup_*` subprocess logs.
`SensitiveDataFilter` (`logger.py:21-41`) redacts credentials/tokens/chat-ids — **never dialogue**.
Memoria and `_log_clause_sanitizer` logging is metadata-only by explicit design; the rest of the
codebase is disciplined. ·
`voice_control.py:244-250` mistags legacy PTT as `direct` ·
`llm_engine.py:1338` hardcodes `priority=1` · `pygame.mixer.init()` at `:1217` has no headless
fallback and its `except` kills the whole engine thread · `_regen_retry_count` is dead telemetry ·
ADR-011's status line contradicts its own update log. All detailed in the deferred doc §6.

## LATEST SNAPSHOT — 2026-07-23/24 (multi-provider cloud LLM + model-switch continuity SHIPPED; ALL UNCOMMITTED)

**Read conductor/tracks.md entries for the four *_2026072{3,4} tracks — they carry the full detail.**
Engram trail: sdd/{model_switch_memory_continuity,multi_provider_llm}_20260723/*, sdd/multi_provider_ui_20260724/apply-progress, sdd/kira_topic_suggestions_20260724/{proposal,design}, runtime/cloud-dead-turns-20260724 (incident+decisions), sdd/design-plan-20260723/* (orchestrator checkpoints).

**SHIPPED (uncommitted: Python repo ~30 files on codex/ui-ux-audit-proposal-20260709; OpenCohost_UI ~10 files same branch):**
1. **model_switch_memory_continuity** — seamless historial across model switch (both clear sites deleted), meta-recall regex two-pattern shape. Verify PASS; OWNER RUNTIME-VALIDATED LIVE 2026-07-24 (continuity + recall proven; 1B incoherence = documented D5 degrade, not a bug).
2. **multi_provider_llm (5 phases, dual-judged per batch, verify PASS 719/0)** — per-provider profiles + per-profile keys (OAuthStore atomic, Windows ACL DELETE-right bug FIXED: (R,W)→(R,W,D)+self-heal), is_local posture-snapshot seam, pregen gate over ALL speculative gen (chat/agenda/connector/scout), fallback machine (auto: warm+spoken notice — PROVEN LIVE; manual: silent+feed per owner), CLOUD_MAX_TOKENS=16384, empty-200→fallback, provider-aware health + /api/models, delete_profile semantics (switch+delete one PUT), i18n slot llm.provider_fallback_notice es/en.
3. **ProviderCard (Tauri, 942/942)** — full provider config UI: profiles/presets/custom ids, write-only key (scrubbed from MutationCache), draft-vs-activation honest 422s, Eliminar/Duplicar, design-system Alerts, cloud_llm_error feed label, panel opens-when-profiles.
4. **Guardrail R3 split + retry** (owner: "afinar+reintento") — discourse markers need same-sentence 2nd-person outcome; one retry-with-nudge before canned line; validation logger FIXED (logs/opencohost_validation.log). Negation handling = follow-up.
5. **Test hygiene**: conftest autouse isolation for LLM_PROVIDER_CONFIG_FILE/LLM_KEYS_FILE (tests were making REAL NVIDIA calls with the owner's key); stale __new__ helpers reconciled. .gitignore `config/` ANCHORED to `/config/` (was swallowing opencohost/config/llm_provider.py — a commit would have shipped broken).

**OWNER MUST: RESTART THE BACKEND** (running process predates the key-store fix; first save self-heals the file ACL). Then: provider stress-testing (add/duplicate/delete providers, keys from the card, kill provider → feed error / spoken fallback).

**PENDING OWNER DECISIONS (the ledger):** commit/PR shaping for everything above (chain strategy; multi_provider phase 1 = 586 lines needs size:exception or split); memoria track GO (direction DECIDED: model-invoked tool-calling commands, NOT regex; UX DECIDED: direct narration mini-show; hybrid fallback for small local models); kira_topic_suggestions tasks GO (proposal+design+gate DONE: Scout→inbox→inline chat-feed card Aceptar/Descartar; 4 conservative defaults await confirmation); gate GET /api/llm/provider?; always-enforced auth for the provider PUT?; cloud-reachability health probe?; R3 negation; other handler-less opencohost.* loggers; task-adaptive cloud token cap; ObsCard password MutationCache retention (same pattern as the fixed key one); cloud model= never logged.

**PARKED (intact):** i18n_completion_20260723 + launch_readiness_20260723 proposals (owner cut scope to the two built tracks).



**liveaudio_ptt_tauri_20260710 — DONE, VERIFY PASS-WITH-WARNINGS (0 critical).** Real hold-to-talk in the
Tauri app. Backend 8e08151 + ce3f304: NEW opencohost/api/ptt_session.py — headless PttSession, a RECV-ONLY
WhisperLive WS consumer (design-phase finding: CTK's voice_control.py never streams mic audio; WhisperLive
captures the mic itself and pushes transcript JSON — zero audio bytes cross Python/Rust/webview) + single-slot
PttController flushing through the same process_context dispatch path as /api/chat/turn. Routes:
POST /api/ptt/{start,keepalive,stop} + GET /api/ptt/state. The keepalive-starvation watchdog is the HTTP
guillotine: a dead client's session is auto-stopped AND its buffer still delivered. Privacy proven by a
sentinel test — transcript text never appears in any response, event, or log (source "ptt" events carry
detail=None, counts only). Client OpenCohost_UI 53dbe25 + 26077d5: usePttHold hook + real PTTCard (pointer
capture, in-app Space/Enter gated on activeElement, blur safety, honest states — 503 renders "STT
(WhisperLive) no disponible", never fake-listening). Global OS shortcut DEFERRED (ptt_global_shortcut
follow-up); LiveVoice continuous mode = phase-2, NOT built (separation rule holds; CTK untouched, 160 voice/ptt
tests green). Suites: backend FULL 3945 passed / 0 failed; UI 498/498 + tsc clean. Residual lows documented in
tracks.md (DispatchResult ignored on flush, grace re-arm slot pinning, close-callback identity guard,
cosmetic flush-poll cache).

**OWNER RUNTIME GATE (before push, noted in commit bodies):** (1) real WhisperLive up -> POST /api/ptt/start
returns 200 listening, not 503; (2) hold-to-talk in Tauri -> dictation reaches Kira as exactly ONE wrapped
process_context turn, no transcript in any /api/ptt/* response or /api/events entry; (3) kill the app
mid-hold -> watchdog auto-stops, still flushes the buffer, frees the slot for a fresh start.

**RUNTIME VALIDATION UPDATE (2026-07-10, owner live session):** PTT gates 1+2 PASS — real LiveAudio
connect (200 listening) and dictation delivered to Kira as one turn, her reply rendered in the feed.
The first 503 was a PORT COLLISION, not CORS: the API launched first and squatted 8765 (LiveAudio's WS
port), LiveAudio then died on bind (Errno 10048). Resolution = START ORDER (LiveAudio first, then the
app; run-api.bat falls back to 8770 as designed). Owner declined moving either default port. Gate 3
still owed: kill-app-mid-hold watchdog proof. NEW TOOL: ptt_f10_bridge.py (repo root) — global F10 hold
via GetAsyncKeyState against /api/ptt/*, works while gaming; webview key events are focus-scoped so the
in-app button can never do this (native fix = the deferred ptt_global_shortcut plugin). NEW FOLLOW-UP
TRACK documented in tracks.md: ptt_transcript_echo — render the operator's own dictation via a DIRECT
webview->LiveAudio WS (display-only, same pattern as the OBS browser source); the API privacy contract
(transcript never in /api/ptt/* responses) stays intact by design.

**Release state:** all Python work linear on codex/ui-ux-audit-proposal-20260709 (both repos on it); owner
decides branch naming/merge, then pushes BOTH repos. tracks.md now committed (owner authorized "commit a
todo" on close day, including their Codex audit-entry revision). NEVER committed regardless: repo-root
config/ (owner tokens), OpenCohost_UI/.atl/, src-tauri/backend.config.json (machine-local), .pnpm-store/.
Still owed by owner from earlier snapshots: watchdog kill-mid-hold proof, Tauri drain re-validation,
locale=en session, auth enforcement flip, heavy-model stall test, reject test proposal ti_6edb78... from
the UI. Open product question: agenda turn semantics (turn_batch_size=2 vs UI slider labeling).

## LATEST SNAPSHOT — 2026-07-09 night (owner runtime validation + command-starvation fix track)

Owner runtime-tested the NEW Tauri build: event feed A+B live (mutation toasts + motor ring events),
personalization proven in-conversation, music rotation fixed, agenda advances + pauses cleanly, gateway
curl 14/14 PASS (trust tier holds: proposals land as `proposed`, operator-only routes 403, idempotent
dedupe, zero token leakage in audit; leftover test proposal ti_6edb78... for owner to reject in the UI).
Privacy audit of the live session: PASS on all three sinks.

Their testing exposed 3 REAL core bugs -> **command_starvation_fix_20260709 DONE** (see tracks.md entry;
commits bedff32, c945fee, dad8330, 889a5b4 + UI 73b1487). Root cause: single engine thread never drains
command_queue during recursive agenda cycles. Full Python suite now 3916 passed / 0 failed; UI 479/479.
OWNER RE-VALIDATION OWED (runtime, after restarting the API): mid-agenda speed change applies at the next
turn boundary; NO straggler turn after emergency stop; Edge-TTS speed audibly changes.

Also explored (engram sdd/liveaudio-tauri-parity/explore): Tauri PTTCard is a UI stub (no mic/hotkey/net);
CTK LiveVoice = WS client to external WhisperLive; guillotine = physical-key reconcile (ptt_manager.py:419).
3 scoped options for parity, favorite = sidecar WS bridge (reuses ~90% CTK logic, no SSE dependency).
Open product question from owner testing: agenda turn semantics — turn_batch_size=2 means the UI "turns"
slider (max 8) counts HALF-blocks; 50 configured would sound like 25 blocks. Needs a product decision
(relabel vs true per-turn counting) — small fix once decided.

## LATEST SNAPSHOT — 2026-07-09 PM (FULL SUITE GREEN: threading track closed + Phase 7 extraction landed)

**Milestone: first all-green full suite** — 3899 passed / 11 skipped / **0 failed** (split only for the
pre-existing test_memorias_profile_uuid hang). The app_shell line-budget known-red is ERASED.

**ui_thread_hardening_agenda_audio_20260624 — DONE.** Discovery: all 4 FRs were already committed on
2026-06-25 (FR4 da4f1f3, FR3 eb8849c, FR2 d0bf19e, FR1 5f83724) while tracks.md sat stale at PLANNED.
This session's judge pass found + fixed 1 real medium gap (31458c5: boundary-deferred audio decode was
still on the Tk main thread — now routed via _dispatch_audio_play). FR1's push validation still shares
the heavy-model runtime gate with heavy_model_inference_recovery.

**app_shell Phase 7 — DONE (VERIFY PASS, judges Opus x2 zero findings).** bdc3a79 (23 characterization
pins first) + 20e0ab3 (20-function agenda/audio cluster -> opencohost/ui/agenda_audio_controller.py,
796 lines, function-module + thin delegates, obs_lifecycle pattern; shared state via injected
getter/setters). app_shell.py: 3016 -> 2655 lines; cap ratcheted 3000 -> 2660 (never-raise).

**Test-infra green-up (same session):** 7779283 (conftest LOG_DIR makedirs — isolated test_integration
runs fixed) + OpenCohost_UI e7c509d (AgendaPanel/AppLayout ToastProvider wrap + stale contentinfo assert
removed — UI suite 479/479 green).

**BRANCH NOTE (decision owed before push):** mid-session the checkout moved to
codex/ui-ux-audit-proposal-20260709 (created by the owner's Codex UI/UX-audit session). It linearly
contains ALL session commits; maintenance/big-file-audit-small-fixes-20260629 sits behind at 7779283.
The Codex session also left an UNCOMMITTED tracks.md revision of its own track entry — do not sweep it
into unrelated commits. Owner decides branch naming/merge before any push.

## LATEST SNAPSHOT — 2026-07-09 (API observability landed + music empty-mood fixed)

Two runtime findings from the Tauri+API session are now closed, both committed to master:

**API Observability** (commit **2485ac1**, backend-only, VERIFY PASS-WITH-WARNINGS) — answers the owner's
"do we keep API run logs for telemetry like CTK?". Three gaps closed: **C** the API's own `opencohost.api.*`
logger now persists to a rotating file WITH the `SensitiveDataFilter` (was dying unredacted on stderr);
**B** a per-request audit JSONL with a CLOSED metadata-only whitelist (method, path, role, status,
duration_ms, idempotency-key — NEVER bodies, NEVER the Authorization token, NEVER query content; judges'
primary lens was privacy and the masked-secret held); **A** motor lifecycle events under the headless API
host now mirror into CTK's `acciones.jsonl` for parity. Pure add-on, sinks fail-open + rotation-bounded, no
endpoint behavior changed. FOLLOW-UP (one line, not a blocker): tests/conftest.py::_isolate_api_log_dir
monkeypatches settings.LOG_DIR without os.makedirs → ISOLATED runs of test_integration.py cascade
FileNotFoundError (config/logger.py:39 builds a module FileHandler at import, no makedirs). Full suite
unaffected. Engram: sdd/api-observability-20260708/*.

**Music empty-mood** (backend **5a937c9** + UI **5422b3c**) — a mood with no own tracks always replayed the
same song (endpoint returned only suggested_track_id); backend now returns the normal->any fallback pool + a
flag and the Tauri MusicPanel rotates over it.

**Event Engine A** (OpenCohost_UI commit **3f9d8b6**, VERIFY PASS-WITH-WARNINGS) — the last approved item, now
shipped. A single client-side emission chokepoint (src/lib/appEvents.ts: whitelisted source.action->label +
sanitizeDetail, a global TanStack MutationCache subscriber reading each mutation's meta.event) fires a
transient toast (reusing the existing ToastProvider) AND persists the same event into a bounded 200-event
zustand ring buffer, interleaved into ConversationPanel as alert dividers. No jank (external store + sliced
selector), zero new deps, metadata-only labels (Opus judge found no privacy leak; Fable judge's 1 medium OBS
mislabel FIXED). Item A tests 40/40. GOTCHA: 36 PRE-EXISTING red tests in OpenCohost_UI (AgendaPanel/AppLayout
render AgendaPanel without a ToastProvider wrapper — from prior commit da108a0, NOT this change) block a green
FULL vitest suite; small follow-up = wrap those test renders in ToastProvider. Engram: event-engine-a-20260709.

**Event Log B** (backend E:/VoiceAI commit **adfb3ea**, client OpenCohost_UI commit **e7a3877**, VERIFY
PASS-WITH-WARNINGS) — owner chose "implementar B ahora". Closes A's blind spot (engine-initiated events).
Backend: EventLogSink ring (deque(200)+lock+seq+boot) hooked into the SAME EngineHost._motor_event_handlers
as the observability track; GET /api/events?since=seq (read-tier) returns {events, cursor, boot}, since=0
backfills, boot signals restart. Client: hand-typed src/api/events.ts polls 1500ms and feeds new seqs through
the EXISTING emitAppEvent chokepoint (srv-<seq>, toast:false — engine events persist but don't toast).
PRIVACY = closed two-gate whitelist (server frozenset of 19 status enums, detail always null; client subset
of 14) — both Opus judges found no leak. useAgendaEvents NOT refactored (non-goal). Backend 3869 passed / 1
known-red (app_shell). NOTE: the Opus design agent implemented the backend during design (blueprint is
"as-built"). KNOWN LOW (owner to decide): cold-start backfill stamps replayed events with Date.now() so old
engine events can read as "now"; whitelist hand-synced in 3 places. Engram: event-log-b-20260709.

MODEL NOTE (2026-07-09): Fable briefly returned null on DESIGN agents (item B's first run) — that run was
redone on Opus. Later the same day Fable recovered and the OWNER DIRECTED: use Fable for ALL heavy phases
again (designs, judges, verify, hard applies); Sonnet stays for mechanical work (small fixes, commits).

C (SSE push) remains DESIGN-ONLY, deferred until a real-time consumer exists. Still owed by owner: locale=en
runtime session, personalization form use, gateway curl, auth enforcement flip, push to origin (both repos).

## LATEST SNAPSHOT — 2026-07-08 PM (runtime finding fixed: agenda None-loop)

Owner runtime-tested Tauri+API and hit a real bug: agenda looped ~20 turns on ONE topic, never
advanced through the 5 configured, "agenda finalizada" fired yet generation continued, only
emergency-stop worked. Diagnosed (3-agent read-only workflow, engram runtime/agenda-infinite-single-topic-20260708)
and FIXED (commit **d383c35**, VERIFY PASS, 5 RED-first regression tests + 143 agenda tests green).
Root cause = the "None-loop": a rejected agenda turn double-counted as two register_failure calls
(llm_engine.py:1573 + trailing :2461); coinciding with a CLOSING force-complete, the second failure
ran with active_topic already None → terminal REGENERATING_SAFE → never re-reached IDLE (the only
caller of _select_next_topic) → topics never advanced, soft_stop couldn't stop it. Also fixed BUG3:
repetition_guard was gated to source=="chat" so it never ran on kira-agenda turns (why the paraphrased
duplicates survived) — now covers kira-agenda. Shared CTK+API code, so the CTK app benefits too.
**Answer to "do guardrails run in the API?"**: the character-contract + coarse-similarity validator DID
run headless (engine_host.py:310, and ADR-011's detect-but-emit-stale bug is fixed — rejects now
suppress the turn); only the skeleton/synonym repetition guard was dead on agenda (now fixed).
**DEFERRED (owner liked the loop AS a feature)**: opt-in single-topic "deep-dive" mode (active_topic
stays NON-None so stops still work) + Tauri agenda events/logging (Tier-1 turns_spoken + state badges,
Tier-2 sanitized events array). Both scoped in the diagnosis; neither built.
Note: full `pytest tests` in ONE invocation deadlocks on a PRE-EXISTING headless hang
(test_memorias_profile_uuid.py::...seeds_ids_for_all_legacy_profiles — Tk mainloop); run split or add
pytest-timeout. The app_shell <3000 line guard is still owner-accepted RED (3015).

## LATEST SNAPSHOT — 2026-07-08 (ALL 3 designed tracks IMPLEMENTED — gateway + personalization + bilingual)

Owner-authorized autonomous implementation session (Fable 5 orchestrator; appliers Sonnet/Opus, judges
1-Fable+2-Opus per owner policy, later all-Opus). SDD tasks→apply→judges→fix→commit→verify per track.
**NOT pushed** (standing). All work on `maintenance/big-file-audit-small-fixes-20260629` + the separate
OpenCohost_UI repo (branch feat/react-ui-s4-experience-selects).

### What landed (chronological, both repos)
1. Baseline: `2ca76b3` (July-5 backend parity committed) + `e977808` (planning docs).
2. **agent_context_gateway** — `a2eed21` warn-only bearer auth (3-rule middleware, OPENCOHOST_API_AUTH
   default OFF → Tauri unaffected); `71c103d` /api/agent/topics (human-gated inbox, D1) + status +
   60/min rate limiter; `9b4a846` cards origin+DRAFT-demotion + AgentNoticeStore; `1701c8c`
   docs/AGENT_GATEWAY.md. UI repo: `da108a0` (owner WIP rescue) + `a291aa9` (api_token command +
   getAuthHeaders). **VERIFY PASS — 3640 passed, 0 critical/0 warnings.**
3. **kira_personalization_onboarding** — `1a3354f` (personalization.json store, <perfil_streamer>
   block FIRST in direct/ptt with 900-char budget + byte-identity-when-disabled, /api/personalization,
   CTK panel) + `e099184` (panel mounted from ProfilePanel.build() — app_shell back to 2998; fixed a
   real fixture hang). UI repo: `ee2bab7` PersonalizationCard. Interview flow deferred (design Phase 4).
4. **kira_bilingual_e2e** — `f8a1d68` (9 units: per-locale FAIL-CLOSED agenda validator +
   GUARDRAILS_MISSING, en guardrails complete, bundle-driven agenda/chat/topic prompts, es
   byte-identity, Piper en voice + honest degrade, qwen_language() into server_qwen.py, profile
   `locale` field, GET/PUT /api/i18n) + `255588c` (Idioma control extracted to
   opencohost/ui/locale_control.py). UI repo: `acaec5d` SettingsPopover locale card. **Verify: all 9
   functional hard gates PASS; suite 3868 passed + 1 KNOWN-RED (below).**

### ⚠️ KNOWN-RED TEST (owner decision 2026-07-08 — do not "fix" without reading this)
`tests/test_integration.py::TestAppShellStructure::test_app_shell_line_count_under_1500` fails:
app_shell.py = 3015, gate <3000. After honest extraction (locale_control.py), the residual +15 lines
are the bilingual fail-closed gate wiring + Piper locale coupling. Owner chose to LEAVE IT RED as
visible debt until `app_shell_agenda_audio_decomposition_20260624` Phase 7 lands. Full-suite runs
report 1 failed BY DESIGN — check any new failure is not hiding behind it.

### OWNER-OWED (new items from this session)
1. Runtime validation of the 3 tracks (Tauri + CTK): locale=en real session incl. agenda speech;
   personalization form → Kira uses it in direct/PTT; gateway curl test with the token from
   `config/api_tokens.json` (see docs/AGENT_GATEWAY.md).
2. Governance: shipped cohost STYLE profiles are es-authored operator data that override en defaults
   ungoverned (no locale field/coherence warning for cohost styles) — product decision needed.
3. Pre-existing red/flaky surfaced: test_memorias_profile_uuid launch test HANGS headless (deselected
   during verify; add pytest-timeout or a guard); OpenCohost_UI has 36 AgendaPanel + 2 AppLayout vitest
   failures (pre-existing ToastProvider wiring gap) — cleanup candidate.
4. docs/api-reference.md lacks the 6 /api/agent/* routes (gateway verify suggestion).
5. Auth enforcement flip (OPENCOHOST_API_AUTH=1) whenever ready — everything ships warn-only.
6. Push both repos to origin (standing decision, still owner-gated).

Engram trail: sdd/{agent-context-gateway,kira-personalization-onboarding,kira-bilingual-e2e}-20260705/*
(tasks, apply-progress, verify-report, orchestrator-checkpoint each) + session summaries.

## LATEST SNAPSHOT — 2026-07-05 (Fable 5 planning session: 3 tracks designed + suggestions backlog — NO code changes)

Owner-requested planning-only session (likely Fable 5's last). Audit + SDD explore→propose→design for
three new tracks, all registered in `conductor/tracks.md` (top of file), NONE implemented:

1. **`kira_bilingual_e2e_20260705`** — EN/ES across agenda + viewer chat + PTT + TTS + profiles.
   Consolidates/absorbs: english_compatibility_i18n T5, i18n_engine_locale_residue (premise-corrected:
   fallback lines NO LONGER commit to historial — FIX-B2), profile `locale` field. KEY AUDIT FACTS:
   only llm_engine.py + server_qwen.py consume i18n_active; kira_agenda_controller has ZERO i18n
   coupling; Character Contract Validator is Spanish-only (guardrail OFF in en — ship-blocker; design
   makes it per-locale FAIL-CLOSED); Piper has no en voice; server_qwen.py:196 hardcodes
   language="Spanish" despite an existing (dead) manifest slot. 7 phases, ~2,480 lines.
2. **`kira_personalization_onboarding_20260705`** — ChatGPT-style "about you" (nickname/occupation/
   interests/custom instructions), global JSON store + new <perfil_streamer> injection block +
   /api/personalization + CTK/Tauri UI; Kira-led interview deferred to Phase 4. Note: ptt gains an
   injection block it never had (deliberate, test-covered). 3 phases.
3. **`agent_context_gateway_20260705`** — safe external-agent ingestion (Claude Code/Codex/Gemini CLI/
   OpenClaw/Hermes). AUDIT FINDING: API has NO auth (loopback-only) and POST /api/chat/turn gives any
   local process the STREAMER trust tier (memorias capture included); POST /api/agenda/topic
   auto-approves (bypasses the CLI inbox's human gate). Design: agents propose→humans approve, two
   bearer tokens (operator/agent) via token FILE for the Tauri spawn, cards upsert forced to DRAFT
   (closes an ARMED-preserving upsert hole at editorial_cards.py:188), agents get NO chat/turn in v1.
   TWO OWNER DECISIONS block apply (agenda/topic stays operator-only; enforcement-flip timing).

Plus `conductor/fable_suggestions_20260705.md` — 6 prioritized backlog candidates (SSE /api/events,
OBS captions, `opencohost doctor`, post-stream recap, persona packs, chat language bridge).
Engram: sdd/kira-bilingual-e2e-20260705/*, sdd/kira-personalization-onboarding-20260705/*,
sdd/agent-context-gateway-20260705/*, sdd/agent-gateway-personalization-20260705/explore.
Operating mode UNCHANGED: runtime-validation gates still come first; these tracks are post-validation
work requiring explicit owner approval before any sdd-tasks/apply.

## LATEST SNAPSHOT — 2026-07-03 (React/Tauri migration: backend DONE+MERGED, frontend design-system built, UI PIVOT to a "music-player" concept — awaiting owner)

Big session: the React/Tauri UI migration off CustomTkinter. **The Tk app is 100% untouched** (verified: `git diff d85dcdb..HEAD` on maintenance touches ONLY `opencohost/api/**` + tests + pyproject + README + .gitignore — ZERO edits to ui/core/__main__/config). Tk remains the real, working app.

### Backend — FastAPI layer, DONE + MERGED to maintenance (@ `67a0c9c`)
Standalone `opencohost/api/` (owns its OWN MotorVocalIA, never imports ui). Full SDD cycle (proposal→spec→design v2.1 judged→tasks). PR1 (pydantic models + command Dispatcher) + PR2 (engine_host + main app/lifespan/CORS, size:exception) each dual-Opus judged + mutation-verified fixes, + a `GET /api/perfiles` addendum. Endpoints: `GET /api/status`, `GET /api/perfiles` (names only), `POST /api/perfiles/switch` (Idempotency-Key). **accepted≠applied** contract. **LIVE runtime-validated** vs real MotorVocalIA + Ollama (qwen3:1.7b): status/switch-applied(~2s)/idempotent-replay/404/health/clean-shutdown all PASSED (engram #2811). 3202 tests, 0 regressions. NOT pushed. Optional `api` extra (Tk install never pulls fastapi/uvicorn).
- **PHASE-2 PRIVACY BLOCKER (engram #2809):** `llm_engine.py:1531` `logger.info(...dialogo[:200])` persists Kira's dialogue to the on-disk file logger, BYPASSING the API's `_Drain` (the drain only covers the `log_queue`/UI path). INERT in Phase 1 (the API engine wires no generating feeder → never reaches :1531). Any future generating endpoint MUST neutralize :1531 first. (Pre-existing in the Tk app too.)

### Frontend — OpenCohost_UI is its OWN git repo (git-init'd this session, gitignored in the Python repo)
React 18 + Vite 5 + TS + Tauri v2 + Tailwind + **TanStack Query (pinned, owner chose despite the May-2026 npm supply-chain incident)** + Zustand + a **3-theme token design system (cockpit/aurora/studio) with a live switcher**. Slices on stacked branches, NONE merged to the frontend `master`:
- Slice A (`feat/react-ui-s1-data-layer` @ `8e61b95`) — typed API client + hooks, judged + fixed (idempotency-key re-switch HIGH fixed, obs #2826).
- Slice B1 (`feat/react-ui-s2-design-system` @ `47b66de`) — tokens/3-themes/switcher/primitives + StatusRail + ProfileSwitcher wired, judged + 4 fixes (tailwind-merge→2.6.0 focus-ring HIGH, FOUC, studio AA contrast, useTheme shared store). Follow-up #2828 (studio badge-tint <AA).
- Slice B2 (`feat/react-ui-s3-panels`) — Modelo/tiers, Voz-TTS, PTT, Memoria panels (designed, local state — no backend endpoints exist for them). Judged clean (one MEDIUM: ModelCard tier radiogroup a11y — fix pending).
- Slice B3 (`feat/react-ui-s4-experience-selects`, PARTIAL — stopped on the pivot) — committed: `Select` primitive, `ExperiencePanel` (avatar-states/transcript/composer), PTTCard affordance fix. Tauri icons committed (`ed06c42`).
- `safe.directory` exception added for OpenCohost_UI (its files are owned by Windows user `CodexSandboxOffline` = owner's OTHER agent that scaffolded it). Supply-chain: exact pins, pnpm 11 script-block, `pnpm audit` (4 pre-existing vite-5 findings, dev-only). Avatar art copied to `public/avatar/` (gitignored — art never committed).

### ⭐ OPEN DECISION — UI PIVOT to a "music-player" concept (AWAITING OWNER)
Owner reconceived the whole UI as YT-Music/Spotify with Kira's essence: **Kira avatar = album cover**, **the conversation = the "up next" queue**, **sidebar = section nav + profiles-as-playlists**, **bottom bar = "now playing Kira"** (Hablar=play, TTS progress=seek, PTT, LiveVoice). **Memories NOT music-styled.** Palette: CTk-legacy dark blue-black + **Kira spectrum** (cyan→violet→pink, her hair) — distinctive, not Spotify-green/YT-red. Mockup published (artifact `39bf1d45-91ac-4a7d-a532-f306bd96d2bc`, scratchpad `kira-player-concept.html`). **Next: owner reacts/adjusts, then reshape the shell** — the design system + B1/B2/B3 components (ExperiencePanel→album-art, transcript→queue, Select, control cards, avatar-states) are REUSABLE; only the shell/layout changes.

### Model directive (this migration)
Owner: "solo agentes Opus, Sonnet" (no Fable). Backend design used Fable 5; everything else Opus/Sonnet; apply = Sonnet 5.

### Non-migration owner-owed (still open)
Push maintenance to origin (never pushed); VocalAIApp flag-ON smoke test (#2789 init-order gap); the mutation-testing "don't revert" message was investigated → **benign harness file-tracking artifact, not injection** (engram #2812).

## LATEST SNAPSHOT — 2026-07-02 (kira_memory_persistence — 8/8 SLICES COMPLETE + live-validated + MERGED to maintenance)

Persistent per-profile Kira memory ("opencohost_memorias") shipped end-to-end on a local
feature-branch-chain, then **fast-forwarded into `maintenance/big-file-audit-small-fixes-20260629`
(now at `ab51f17`)** on owner approval 2026-07-02. **19 commits** (8 slices × feat+judge-fix + the
runtime init-order fix `c865f59` + handoff). **`MEMORIAS_ENABLED = True`** (settings.py:271).
Still NOT pushed to origin (maintenance itself has never been pushed — a separate owner decision).
**Live runtime gate PASSED**: real session captured 2 host-only memorias, persisted across app close
via the F4 flush (proven by identical close-timestamps), zero viewer chat, zero crashes (engram #2789).
**FULL SUITE 3154 passed, 11 skipped, 0 failed — RELEASE GATE GREEN with the flag ON**, empirically
verified no real memorias.db/notice written under USER_DATA_DIR. Every slice = feat + judge-fix,
each dual-Opus approved; owner made all product decisions (Q1-Q6, F1-F6, F3b, F6b — engram #2770).

### What shipped (all 16 spec requirements landed)
- Per-profile SQLite store (`memorias.db`): host-distilled EXTRACTS from evicted direct/ptt pairs,
  provenance-gated behind the T1 `_DIGEST_CAPTURE_SOURCES` fail-closed gate — viewer chat NEVER persists.
- Write-through on eviction + bounded flush on clean close + atomic profile-switch flush (RC-2 window closed).
- Session-scoped capture switch («Memorias: ON» / «sin guardar», DISK-ONLY, forward-only, no retro-capture).
- Lexical top-k retrieval (match_score reuse) + max-2 pinned injection (F6) on the DIRECT path only;
  private/inactive never injected; 700-char budget.
- Management UI in "Memoria de Kira": edit/pin/private/inactive/delete, unified freeze rule (F5),
  honest «Fijadas: N · se inyectan M» counter (F6b: N=all-pinned, M=injectable).
- Active-profile + explicit-profile-delete purge (honest uncapped count, id-keyed).
- Stable profile UUID in perfiles.json (rename-safe, atomic write). F1 passive disclosure banner
  + honest PRIVACY.md/TRUST_MODEL.md/ADR-030 reconciliation.
- Engram trail: sdd/kira-memory-persistence-20260701/{explore,proposal,spec,design,tasks,apply-progress,
  owner-decisions,researcher-review,judge-round-slice1..8}. Full commit map in apply-progress #2780.

### Accepted residuals (documented, NOT bugs)
Hard-crash loses live window ≤10 pairs; clear/model-switch/download don't flush; plaintext-on-disk
(v1 non-goal — no encryption/retention, matches sessions.db/cards.db); agenda-append eviction loss
(pinned test); bag-of-words retrieval ceiling (no embeddings; pinning is the override). Deferred
sub-tracks: memorias_fuzzy_upsert, automated PII redactor, historial_privacy_lanes_ui.

### OWNER-OWED (open gates)
1. **MERGE/LAND — DONE 2026-07-02** (ff to maintenance @ ab51f17). Remaining: push to origin is still
   OPEN (maintenance has never been pushed — a broader unrelated decision), and a regression-test gap:
   NO smoke test covers VocalAIApp construction with the flag ON (the init-order crash c865f59 was
   invisible to the suite because no test instantiates the app — see engram #2789).
2. **RUNTIME VALIDATION — CORE PASSED live 2026-07-02** (persistence + host-only privacy + F4 flush).
   Optional UI-surface follow-up (owner not yet done): reopen app → confirm the 2 memorias render in the
   "Memoria de Kira" window; exercise the capture switch, per-profile purge, «Fijadas» counter, F1 banner.
3. **NEW DIRECTION (owner, 2026-07-02): UI stack migration** off CustomTkinter → React+Tailwind in Tauri v2,
   Python core exposed via a local FastAPI sidecar. Owner builds the React/Tauri app; binding model
   "one or the other" (React REPLACES Tk at cutover — never two frontends on one live engine; Tk stays
   as fallback). **BOTH tracks FULLY PLANNED + PAUSED before implementation** (owner cut at the planning
   checkpoint 2026-07-02 — no backend code, no opencohost/api/ created). NOTE: the owner ALREADY has an
   existing React/Tauri frontend at `E:\VoiceAI\OpenCohost_UI` (untracked, built 06-30, actively edited
   07-02 incl. `src/lib/pythonEngineBridge.ts` + maquetación previews) — the frontend plan below is
   greenfield and MUST RECONCILE with this existing scaffold, not overwrite it (engram #2803):
   - **Backend `kira_fastapi_api_layer_20260702`** — proposal #2793(v2) · spec #2796 · design #2798(**v2.1,
     dual-Opus judged** — judge round #2802) · tasks NOT yet run. Phase 1 = standalone `opencohost/api/`
     FastAPI process owning its OWN MotorVocalIA, 2 endpoints (`GET /api/status`, `POST /api/perfiles/switch`),
     concurrency/idempotency contract, `active_profile` in status (zero core edits — reads existing
     `_current_profile_name`), CORS. Judges caught + folded: a PRIVACY regression (log_sink was persisting
     Kira's dialogue → now a no-op drain), `is_`-prefixed field names, engine-stop via `command_queue.put(None)`,
     bounded idempotency cache, cross-process lockfile, loopback bind. Est ~615-680 lines → **2-PR split**
     (PR1 dispatch+models ~250; PR2 main+engine_host ~400 → size:exception or 3-way). NEVER imports ui/ or
     touches core (verified).
   - **Frontend `opencohost_react_ui_20260702`** — proposal #2794 · spec #2797 · design #2799 · tasks #2801.
     Vite+React+TS+Tauri v2+Tailwind, feature-based, zustand (UI) + TanStack Query (server state), NO Prisma,
     OpenAPI-generated types = anti-drift lock. FE Phase 1 = status+profiles pages vs the 2 backend endpoints.
     **MUST RECONCILE with the owner's EXISTING E:\VoiceAI\OpenCohost_UI scaffold** (#2803) — read their
     pythonEngineBridge.ts/demoState.ts/App.tsx first, align our API-client + state design with it, keep
     their maquetación previews, do NOT greenfield-overwrite; owner OK required before editing their WIP.
     Also decide: nested at E:\VoiceAI\OpenCohost_UI (gitignore it — separate Rust/node build) vs sibling.
     Open: R6 spec/design conflict (types committed vs git-ignored — reconcile to owner's "on-demand not
     committed" default before verify).
   - **Cross-track reconciliation** #2800 (active_profile + CORS) folded into backend. **Model directive**
     #2795: backend design=Fable, everything else (explore/spec/frontend-design/judges/tasks)=Opus, apply=Sonnet 5.
   - **TO RESUME**: backend `sdd-tasks` (Opus) → apply both (Sonnet, 2-PR backend + 1 FE slice), each impl
     slice gets a per-PR judgment day. Backend judged-clean design is ready; frontend fully task-planned.
   Also noted (not fixed): avatar async-flash UI bug (engram #2790) — subsumed by the migration.

---

## LATEST SNAPSHOT — 2026-07-01 (privacy fixes + inspector windows + agenda-ptt honest commit + external-LLM API research — COMMITTED)

Branch `maintenance/big-file-audit-small-fixes-20260629`. THREE judge-approved tracks, stacked
(T1 → T2 → T3). **FULL SUITE 100% GREEN: 2995 passed, 11 skipped, 0 failed.**
Owner approved the commit split 2026-07-01 — landed as three commits (hunk-level split of the
shared files llm_engine.py / app_shell.py / test_pipeline_memory.py):
1. `61b7f26` `fix(privacy): gate digest eviction by source + redact/purge session_history persistence` (T1)
2. `085c613` `feat(ui): read-only inspector windows for editorial cards and Kira memory` (T2)
3. `831dbb1` `fix(engine): honest history_text seam for agenda-ptt commits + retarget stale OBS test patch` (T3)

### (a) T1 — privacy_prereq_fixes_20260701
- D1: MemoryDigest eviction capture now allowlists evicted-pair sources `{direct, ptt}`
  (`_DIGEST_CAPTURE_SOURCES`, llm_engine.py) — viewer `chat` AND `accumulated` (which bundles verbatim
  chat via `_flush_accumulation`) no longer enter the digest. Fail-closed on missing/unknown source.
- D2: session_history.py redacts its ENTIRE persisted payload (summary strips "Referencias detectadas";
  metadata top_intents allowlisted to `{intent,label,count,duplicates}` — `examples` AND `entities`
  dropped; trigger allowlisted to scalars) + one-time `user_version`-guarded purge (DELETE
  context_snapshots, DROP legacy `messages` table, unlink chat_log.jsonl; locked jsonl → retry next start).
- Dual-Opus Judgment Day APPROVED (Judge A zero defects; Judge B should-fixes applied + verified).

### (b) T2 — cards_memory_readonly_panels_20260701
- Two read-only inspector windows from Configuración → M/Perfil: "Tarjetas editoriales (N)"
  (opencohost/ui/inspector_cards.py) and "Memoria de Kira (N turnos)" (inspector_memory.py — honest
  lifetime badges «Solo en RAM» / «En disco · persiste entre sesiones», agenda provenance note).
- New read-only engine accessor `memory_inspector_snapshot()` (llm_engine.py): content only for
  user+direct and assistant+direct|ptt; digest stats-only; snapshot-then-release under _history_lock.
- app_shell.py +59 lines (launcher mini-frame + 2 openers); line-count guard raised 2710→2990 with
  documented debt (tests/test_integration.py:247) — guard is GREEN again.
- Judgment Day: Judge B (Opus) — privacy core SOUND, zero leak paths; 3 product must-fixes + 2
  recommended ALL applied (agenda refresh idempotent, absolute "Actualizado HH:MM:SS" stamp,
  winfo_exists guards on marshaled renders, "[turno oculto — N caracteres]" rows, memory stamp).
  Judge A (Fable 5) was owner-stopped mid-run — optional re-run on the final diff.
- Judge panel policy (owner decision): Judge A = Fable 5, Judge B = Opus 4.8 going forward.

### (c) T3 — agenda_ptt_commit_raw_text (+ OBS red-test fix)
- AgendaAction.history_text seam threaded end-to-end (controller → app_shell dispatch → enqueue →
  5-tuple queue → _commit_history): agenda-ptt turns commit "El streamer dijo (PTT): {ptt_text}"
  instead of the full "TAREA: …" template. Byte-identical for every other caller (default None).
- Mixed Judgment Day (Judge A = Fable 5, Judge B = Opus 4.8): **BOTH APPROVED, zero must-fixes**.
- KEY HONEST FRAMING (Judge B): the fixed seam is production-DORMANT — no live caller passes ptt_text
  to next_action(); the real PTT path (voice_control.py) was ALREADY honest. This fixed a LATENT
  defect in an unwired seam (future-proofing), NOT a live production leak.
- Companion fix: tests/test_app_shell_obs_resilience.py now patches the REAL sleep site
  (obs_lifecycle.time.sleep; app_shell.time was a stale target from the Phase-6 decomposition) —
  the last red test is gone. `import time` in app_shell is genuinely dead; NOT restored.
- Judge residuals (pre-existing, NOT regressions): (1) `_chat_action` still commits template
  boilerplate for agenda HANDLE_CHAT turns — registered as `agenda_chat_action_raw_text` proposal;
  (2) enqueue overflow-drop discards history_text when re-routing to accumulation
  (llm_engine.py:516-519, LOW); (3) replace_pending doesn't forward history_text (safe today).

### (d) External-LLM API engine research (exploration only, NO code) — engram sdd/external-llm-api-engine/explore
- ALL four targets speak OpenAI chat-completions (Ollama /v1, Groq, Gemini compat endpoints verified
  2026-07-01) → ONE client seam (base_url + api_key + model) can cover Ollama+OpenAI+Gemini+Groq.
- Cost model (2h stream ≈ 518K in / 90K out; 12 streams/mo): Groq Llama-8B ~$0.39/mo · Groq 70B
  ~$4.52 · Gemini Flash-Lite ~$3.15 · Gemini 3.5 Flash ~$19 · GPT-5.4-mini ~$9.51 · GPT-5.4 ~$31.80.
- KEY INSIGHT: the ADR-029 front-eviction bug defeats prompt caching on EVERY provider (exact-prefix
  match from position 0) → full-price billing; the ADR-029 fix becomes a COST prerequisite for any
  cloud engine work.
- Hard gates: Gemini free tier trains on API data (hard exclude); Groq free tier can't survive one
  stream (paid tier from day one). PRIVACY: agenda prompts carry wrapped viewer chat → cloud transit
  = new trust boundary → ADR + opt-in gate + TRUST_MODEL/PRIVACY updates mandatory before any code.

### (e) State / next (owner gates when back)
- FULL suite: **2995 passed, 11 skipped, 0 failed** — first fully green suite.
- Commit decision DONE (3 commits above, 2026-07-01). Runtime validation: open both inspector windows live (no viewer
  text anywhere, badges, counts, clipboard, refresh idempotency, close-mid-refresh). T3 has nothing
  to validate live today (dormant seam).
- Decide next: inspector v1.1 (ptt/digest content display — technically unlocked by T3, owner-gated);
  external-LLM API proposal (owner questions in the engram artifact); Track 4 agent-contract ADR
  (still blocked on real OpenClaw/Hermes contracts + trigger-speech stance).
- Registered proposal-only backlog: `historial_privacy_lanes_ui`, `output_guardrails_prompt_extraction`,
  `agenda_chat_action_raw_text` (new, from T3 judges).
- Engram: sdd/privacy-prereq-fixes-20260701/*, sdd/cards-memory-readonly-panels-20260701/*,
  sdd/agenda-ptt-commit-raw-text/*, sdd/agenda-chat-action-raw-text/proposal,
  sdd/external-llm-api-engine/explore + session summaries.

---

## LATEST SNAPSHOT — 2026-06-29/30 (big-file audit + Ollama hardening + Topic Scout DARK + ctx-discovery prod fix)

Branch `maintenance/big-file-audit-small-fixes-20260629` — **49 commits ahead of `master`, NOT merged.**
Tree: all session work committed; only owner-local files dirty (see DO-NOT-TOUCH below).

### (a) Operating mode
**Less expansion, release readiness.** Product is believed SOLID to release soon. Do NOT start new
feature tracks. The remaining work is runtime validation of what already shipped (gated-off or
unexercised), then merge. Release verdict on this branch: **SOLID-WITH-CAVEATS** — mergeable for the
owner's primary config (gemma4), but one real regression-vector ships (the `fast` tier num_ctx change,
item #4 below) and must be validated or capped before relying on a non-gemma cohost model.

### (b) #1 NEXT STEP — UNMISSABLE, DO THIS FIRST
**Topic Scout is implemented but DARK. Flip it on and validate its adjacency on a real model.**
- Set `SCOUT_ENABLED = True` (`opencohost/config/settings.py:68`).
- Run the gated real-env test (needs a live Ollama with `gemma4:e2b` pulled):
  ```powershell
  $env:OPENCOHOST_REALENV_TESTS = "1"; python -m pytest tests/realenv/test_topic_scout_realenv.py -q
  ```
- Goal: confirm the idle-LLM topic suggestions are TOPICALLY ADJACENT (not random) on a real model.
  The Scout is hard-gated at `llm_engine.py:1579` and the app_shell wiring (`app_shell.py:1112`) is
  defensively wrapped, so flipping the flag is safe — it cannot affect the hot path if the validation
  goes sideways. Flip back to `False` if adjacency is poor; do NOT merge Scout ON until T9 passes.

### (c) OWNER-OWED next-attack map
Each item: WHAT / WHERE / HOW to attack.

1. **Topic Scout T9 realenv (THE #1 above).** WHAT: validate Scout adjacency on `gemma4:e2b`.
   WHERE: `tests/realenv/test_topic_scout_realenv.py`, flag `settings.py:68`. HOW: see (b).
2. **Flash-attention real exercise.** WHAT: FA config (ADR-022/023) is a cold-start-only
   `setdefault` — inert in this test run, never proven live. WHERE: Ollama startup path.
   HOW: set `OLLAMA_FLASH_ATTENTION=1` as a SYSTEM env var, then COLD-START the Ollama daemon
   (a warm daemon ignores it), and confirm the model loads with FA active.
3. **7 SDD proposals A–G** (includes the 2 latent bugs below). WHAT: explore+design landed,
   none implemented. WHERE: `conductor/tracks/big_file_decomposition_20260629.md`. HOW: pick one,
   fix-pass coordinate/contract drift if any, then strict-TDD implement on owner approval.
   - **Latent bug A — `self.after` cross-thread race.** `app_shell.py:1664/1670/1682/1691/1693`
     use raw `self.after`, bypassing the `_safe_after` thread-guard (`app_shell.py:2231`), on a path
     fed by the chat-aggregator DAEMON thread. Severity for release: **MEDIUM-LOW** — only reachable
     with a live RF3 chat connected, intermittent, GIL usually masks it, no observed crash. NOT a
     blocker. Fix = route those 5 calls through `_safe_after`.
   - **Latent bug B — shared retry budget → silent empty return.** `llm_engine.py:1212-1234`,
     `max_intentos=2`: overflow-trim (attempt 0) + reasoning-cap-drop (attempt 1) can exit with
     `raw_content=""`. Severity: **MEDIUM-LOW** — needs a double self-heal on one turn (rare); the
     agenda `register_failure` degrade ladder catches the empty (one muted, recoverable turn). NOT a
     blocker. Fix = give the two self-heals independent retry budget.
4. **A4 per-tier num_ctx caps — ELEVATED from "nice-to-have" to RELEASE-RELEVANT.** WHAT: the
   ctx-discovery fix (`d3334dc`) uncapped non-gemma tiers. `_model_ctx` feeds BOTH the char-budget
   AND `opciones_llm['num_ctx']` (`llm_engine.py:1190`), so a non-gemma model now requests its full
   native ctx as the Ollama KV allocation. **The shipping `fast` tier is `qwen3:1.7b` (native ctx
   40960)** → selecting it allocates ~40960-token KV (~10x the prior ~4096) on the 12GB box AND
   disables char-budget eviction (budget ≫ the 10-turn window ⇒ full re-prefill every turn ⇒ worse
   TTFT). balanced/default=llama3 (8192, ~2x, low risk); quality=gemma4:e4b is popped → safe; gemma
   primary path is unaffected (why the branch is releasable). WHERE: `llm_engine.py:1171-1194`.
   HOW: before relying on the `fast` tier OR recommending any high-ctx non-gemma model (qwen3 / large
   llama) as cohost, EITHER land A4 OR runtime-validate `qwen3:1.7b` for VRAM/TTFT. A4 must clamp
   BOTH `num_ctx` AND the overflow-budget ctx together — capping one re-opens overflow. Consider
   clamping `num_ctx` independently of the discovery value.
5. **ctx_utilization telemetry doc/code drift.** WHAT: the `ctx_utilization` log line
   (`llm_engine.py:1323`) and its doc description have drifted. WHERE: ADR-029 + emit site. HOW:
   reconcile the documented fields against the actual emitted record; `prompt_eval_count` is present
   in every Ollama response and now used here — confirm the doc matches.
6. **Prompt-efficiency Lever 2 (compact verbose replies).** WHAT: measure-first instrumentation
   shipped (`c428574`); the actual compaction lever is pending DATA + owner sign-off. WHERE: ADR-029,
   instrumentation in `llm_engine.py`. HOW: collect real ctx_utilization samples first, then propose
   Lever 2. NOTE: couples to #4 — on high-ctx non-gemma models char-budget eviction is disabled, so
   the full 10-turn window re-prefills every turn; measure these two together.
7. **Merge the branch.** WHAT: merge `maintenance/big-file-audit-small-fixes-20260629` → `master`.
   WHERE: this branch. HOW: ONLY after the runtime validations above (at minimum #1 Scout T9 and the
   #4 fast-tier decision). Do not commit/merge without explicit owner ask.

### (d) Branch state
- `maintenance/big-file-audit-small-fixes-20260629` — **49 commits ahead of `master`, NOT merged.**
- The 49-commit span is almost entirely observability + dead-code removal + gated-off features.
  Topic Scout is genuinely DARK (`SCOUT_ENABLED=False`), the 3 deferred core fixes landed via the
  snapshot-10 design→2-judge→apply gate, source-tag is stripped before `ollama.chat` (rebuild, not
  mutate). The only behavioral change that actually ships is the num_ctx side-effect in #4.
- **DO NOT TOUCH (owner-local, intentionally never committed):** `assets/avatar/kira/*.png`
  (owner's local avatar edits, modified) and `config/` (untracked runtime config). Leave both dirty.

### (e) Reading index
- **ADR-030** (`docs/adr/ADR-030-session-decision-journal.md`) — this session's decision journal;
  read it first for the full narrative behind the OWED map.
- **ADR-025** real-env ctx-discovery bug (model_info vs modelinfo → ctx was always 4096; the
  production fix in `d3334dc`). **ADR-026** real-env test harness. **ADR-027** adversarial
  multi-agent gates (design→2 judges→apply). **ADR-028** Kira memory + topic architecture (Scout).
  **ADR-029** prompt-efficiency / KV-cache (couples to #4 and #6). **ADR-022/023** Ollama backend
  choice + 12GB config hardening (flash-attention, item #2). **ADR-024** editorial cards as
  primitive RAG (deferred).

### (f) Runtime validation — 2026-06-30 (live gemma4 session, profile Akira, ~2h)
A real 2h+ session (logs 12:09–14:33) with model switching qwen3:1.7b → gemma4:e2b → gemma4:e4b.
Updates to the OWED map above:
- **#5 telemetry drift — CONFIRMED LIVE + ROOT-CAUSED.** The `ctx_utilization` log prints `num_ctx=131072`
  for gemma, which LOOKS alarming but is MISLABELED. Verified: `llm_engine.py:1193-1194` POPS `num_ctx`
  for gemma (never sent to Ollama), and the log field at `:1313` is `_ctx_for_obs = self._model_ctx_limit`
  = the DISCOVERED NATIVE ctx, NOT the effective num_ctx. So **gemma does NOT allocate a 131072 KV cache —
  primary path confirmed safe live.** Fix = rename the log field (`native_ctx=` / add `effective_num_ctx=`)
  so it stops implying gemma runs at 131072. `ratio` = prompt-vs-native headroom, not KV utilization.
- **#6 prompt-efficiency — REAL DATA captured.** TTFT is DECODE-dominated, not prefill: prefill_ms 421–1640
  (cheap, grows slowly), decode_ms 6587–17490; responses verbose (eval_count 700–1156 → 10–27 TTS fragments,
  full TTS 54–127s). ⇒ **Lever 2 (compact replies) is the right lever; Lever 1 (prefix-stability) is LOW
  priority for gemma** (prefill already cheap). measure-first instrumentation (c428574) proven working live.
- **source tag — VALIDATED LIVE.** `source=direct` / `kira-agenda` / `kira-agenda-stop` all emitted correctly
  end-to-end (the substrate the host-only Scout consumes is confirmed clean).
- **heavy_model_inference_recovery — happy-path only.** Clean switching + memory release
  ("Liberando memoria del modelo: qwen3:1.7b") + warm-prep proven; but the WATCHDOG TIMEOUT + ROLLBACK
  recovery was NOT exercised this run (no stall). Still relies on the prior qwopus/gemma:26b validation.
- **#1 Topic Scout — STILL DARK / not exercised** (no scout lines in the log). Unchanged, still owed.
- **#4 fast-tier qwen3 num_ctx=40960 — STILL UNTESTED.** qwen3:1.7b was the initial model but the session
  switched to gemma4 BEFORE any qwen3 inference, so no `ctx_utilization` line for qwen3 was captured. The
  regression is neither confirmed nor cleared by this run.
- **#2 Flash attention — not provable from this log.**
Per-track annotations written to: prompt_efficiency_kvcache, ADR-029, heavy_model_inference_recovery,
dynamic_model_management, history_source_tag, topic_scout_llm.

### Stale-doc note (internal, harmless — reconcile if snapshots are kept as the record)
`09_final_report.md` points 11/15 still say the 3 core fixes are "DIFERIDOS, pendientes de OK del
owner" — they were APPLIED in snapshot 10. Audit drift only; no owner-facing impact.

---

## LATEST SNAPSHOT — 2026-06-23 (cohost backlog: 2 fixes shipped + 7 designs staged + Opus audit)

Branch `feat/akira-voseo-fix-and-cohost-adr` (NOT merged to master). Tree clean — all work committed.

### Shipped this session (committed, strict-TDD)
- **Raw-chat prompt-injection fix** (`aa88a56`) — `kira_agenda_controller._build_prompt` wraps viewer
  chat in read-only data delimiters + collapses `=` runs so the markers can't be forged. 8 tests +
  blind-judge validated (all-resist).
- **Decision 6 interim** (`3577a60`) — flipped `INPUT_CONTRACT_SHADOW_MODE=False`
  (`chat_input_contract.py:18`): the shadow path was persisting chat-derived `old_compact`+packet to
  `acciones.jsonl` (local gitignored log; not a git leak, a runtime privacy-rule violation). Full
  redact/allowlist work STAGED as a track.
- **Music-Mode Ducking** (`f242dd4`) — `_is_ducked` flag fixes the track-change duck regression
  (`audio_bed.py:228`) + `AudioBedPolicy.__post_init__` clamping + `load/save_music_volumes`. 11 tests.
  REMAINING (owner): app_shell 1-line wiring to load persisted volumes + runtime ear-check.
- **Reasoning-Token-Budget / think=False** (`18995c4`) — capabilities-augment
  (`_check_capabilities_reasoning` via `ollama.show`, defensive — augments, never depends) + per-model
  cache + self-heal (empty content + thinking → uncapped retry). 12 tests. REMAINING (owner): validate
  vs a real `gemma4:12b` (RUN C).

### Public-launch readiness (ADR-016, committed) — repo NOT made public (OWNER-RUN)
- ADR-016 (`4af9caf`) MIT + fresh-history export. README Lite-aligned (`83e60eb`): honest TTS framing,
  dropped RF4/heavy-TTS, fixed install path. 9 trust/onboarding docs (`24863f3`):
  CONTRIBUTING/SECURITY/CODE_OF_CONDUCT/SUPPORT/PRIVACY/TRUST_MODEL/QUICKSTART + 2 issue templates.
  Repo-hygiene (`eec1606`): `.agents/` → `.gitignore` + git-safety-check `.engram//Documents/` blocks.
- Export EXCLUSION MANIFEST decided + recorded in
  `conductor/tracks/opencohost_repo_export_20260610/plan.md` (Phase 1): keep only conductor/
  product.md+tech-stack.md+code_styleguides; EXCLUDE `avatar.yaml` (orphan), `CLAUDE.md`/`AGENTS.md`/
  `AGENT_HANDOFF.md`, `.agents/`, `.opencode/*`; strip pyproject `heavy-tts` extra at export.
  CODE_OF_CONDUCT contact = `gitrafuh@gmail.com`.
- **The export itself (create `plynte-labs/opencohost`, fresh-init, flip public) is OWNER-RUN — NOT started.**

### Design pipeline — 7 tracks have explore/design artifacts (gitignored `conductor/tracks/<slug>/`), Opus-audited
Full audit: `conductor/tracks/design_audit_20260623.md` + engram #2438. All 7 landed MINOR-GAPS
(right architecture, precision defects). Cross-cutting theme: file:line / API / fixture DRIFT vs live source.
- **Implement-ready NOW (2):** Profile-Language Auto-Detect; Engine Locale Residue.
- **Need a fix-pass first (5)** — one concrete blocker each:
  - Repo Hygiene (H3): T3 asserts a constant it removes → RED forever; missed 3rd dup `CODE_PATTERNS` at `kira_agenda_controller.py:302`.
  - App Startup Clarity: wrong API name (`_on_ui_state_change` → real `_on_state_change`); 3-way `lbl_status` write race in warming.
  - Status Bars Stale: Fix A snippet crashes (`_safe_after` arg order inverted); delayed-vs-immediate reset contradiction.
  - Context-Overflow Guardrail (NEW, 2h+ live resilience): response-access contract contradicts code+tests (getattr vs `respuesta.get('message')`, tests mock plain dicts → Layers 3/4 never fire); T1-T6 ready; T7 gated on a live Ollama-overflow runtime question. KEY FINDING: `prompt_eval_count` is in every response but unused.
  - Latency Tracing: 2 HIGH seam mislocations (DISPATCH misses motor-busy path; `finish()` wrong location); stale line numbers — reference impl on `feat/latency-tracing` likely already resolves these.

### NEXT SESSION ENTRY POINT
1. Recover via `mem_context` (engram #2438 audit, #2431 designs, #2425 impls, #2415 ADR-016 arc). Read `conductor/tracks/design_audit_20260623.md`.
2. **Fix-pass the 5 not-ready designs** (correct the coordinate/contract drift per the Opus audit — doc edits to the gitignored `design.md` files).
3. Then **implement strict-TDD** (like Music/Reasoning): the 2 ready + the fixed ones, one batch at a time, on owner approval.
4. Owner runtime validations still owed: RUN B (latency tracer live) + RUN C (heavy-model) from prior sessions; Music duck mid-utterance + app_shell volume wiring; Reasoning vs `gemma4:12b`; Context-Overflow "does Ollama truncate silently or return empty?".

### Infra notes (this session)
- **Fable 5 is UNAVAILABLE** — substituted sonnet per the no-access rule (the audit used Opus, the session model).
- Workflow sub-agent spawning hit transient API 500s twice; **INLINE implementation (main loop) was the reliable recovery.** After a partial-edit 500, revert to tag `backup/pre-explore-design-20260623` + remove untracked partial test files so the strict-TDD RED phase is genuine.

---

## LATEST SNAPSHOT — UI polish + audio-teardown MERGED to master (2026-06-15)

**`feat/ui-polish-freeze-declutter-20260614` MERGED to `master` via PR #46 (commit
`5505629`). CI green. The branch itself is kept (not deleted).**

What landed on master in this branch:
- **Main-thread freeze elimination** (Phase 5 of `ui_rendering_optimization`) — ADR-008.
- **UI declutter** — RF4 stream-admin panels hidden behind `STREAM_ADMIN_ENABLED=False`,
  the `Sistema` rollup pill, de-bloated state bar — ADR-009.
- **Toplevel focus fix** — `opencohost/ui/window_utils.py` (`show_toplevel` / `raise_window`)
  defers the raise past CTkToplevel's `after(5, deiconify)`; adopted by `gear_popover.py`.
- **In-flight WIP baseline** (commit `6d77b56`): Qwen TTS lifecycle Phase 1 visible engine
  badge (`qwen_markers.py`, `state.engine_status`, startup self-check), model-panel
  installed-model discovery, and the `server_qwen.py` APP_ID heavy-TTS fallback fix.
- **PR #45 (audio-teardown) is MERGED** (was on master at `9fdc1a5`; merged into this branch).
  Two conflicts resolved: union in `_kira_agenda_emergency_stop` (prefetch-retry cancel +
  motor-interrupt + audio-bed hard-stop, single guarded `drop_pending_sources`); app_shell
  line-budget guard raised to `<3270` (real merged file = 3256 lines).

CI bugfix (commit `81be9cb`, merged): the full-suite CI caught 5 failures a local SUBSET run
missed — 4× RecursionError (read `_prefetch_retry_id` via `self.__dict__.get(...)`, not
`getattr`, because tests build the app with `object.__new__` bypassing `__init__`) + 1
hardcoded test path made portable. LESSON: run the FULL `tests/` suite before any PR.

Cleanup done: both leftover `.claude/worktrees/` removed; `fix/audio-teardown-stop`
local+remote branch deleted. The `viewer_queue_backpressure_20260613` board edit is preserved
at `conductor/tracks/viewer_queue_backpressure_20260613/board_entry.patch` (proposal still
awaiting owner approval).

**Owner runtime gates still OPEN:** (1) gear popover raises on top on Windows + first-boot
freeze gone + `Sistema` pill reflects state; (2) carried from PR#45 — music stops *deferred*
on soft-stop, *immediate* on emergency-stop. The Qwen lifecycle Phase 1 badge shipped, but
the full self-managing lifecycle (eager start / keep-warm / VRAM-gated stop) is NOT built —
track `qwen_tts_lifecycle_hardening_20260613` stays open.

---

## SNAPSHOT — UI Declutter shipped (2026-06-14, now merged via PR #46)

**Track: ui_declutter_20260614 / feat/ui-polish-freeze-declutter-20260614 — MERGED to master (PR #46, 2026-06-15)**

### Active flags

**STREAM_ADMIN_ENABLED = False (2026-06-14):** RF4 metadata/moderation/Kira-chat panels
are HIDDEN for the OpenCohost launch UI. Moderation is delegated to Nightbot. The panels
were HIDDEN (not deleted) to reduce operator confusion and declutter the launch-ready UI.
RF3 Chat Live (`_build_chat_live_tab`) is KEPT active and unaffected.
Code is CONSERVED — do not delete without the dep-check + owner approval in
`stream_admin_legacy_removal_20260614` (NO-PRIORITY track).
See `docs/HANDOFF_RF4.md` for the RF4 legacy freeze note and rationale.

### What shipped in this track

- **Part A (flag gate):** `STREAM_ADMIN_ENABLED = False` in `settings.py` hides
  `_build_metadata_tab`, `_build_moderation_tab`, `_build_chat_tab` in
  `stream_admin_ui.py`. `_build_chat_live_tab` (RF3) is NOT gated.
- **Part B (Sistema rollup):** New `lbl_sistema_pill` in `StatusBar` aggregates
  model + mic + TTS + health into one always-visible indicator (5 severity levels:
  CRIT/WARN/INFO/QUIET/OK in red/amber/blue/grey/dim). Engine badge visibility
  gated: dim on steady-state, amber on qwen_starting (owner decision: Edge speaks
  during warmup, operator must see the transient change).
- **Part C (cleanup pointer):** `stream_admin_legacy_removal_20260614` track stub
  created (NO-PRIORITY, NOT STARTED) for eventual RF4 code deletion after dep audit
  and owner authorization.
- **Tests:** `tests/test_ui_declutter_flag_gate.py` (18 tests) and
  `tests/test_sistema_rollup.py` (32 tests) — all green.

---

## LATEST SNAPSHOT — Qwen TTS lifecycle build in progress (2026-06-14)

Active build: `qwen_tts_lifecycle_hardening_20260613` — making heavy TTS self-manage and
**self-verify** (the fix for the owner's "I can't tell if it actually works" uncertainty).
Strict TDD, **NOTHING COMMITTED** (working tree only).

- Artifacts: `conductor/tracks/qwen_tts_lifecycle_hardening_20260613/` — `spec.md`, `tasks.md`,
  `investigation.md` (Appendix A = how QwenProcessManager works), and **`progress.md`** with the
  exact resume point + green test baseline. Engram topic `architecture/qwen-tts-lifecycle`.
- DONE (green, fresh-reviewed): Phase 0 foundations (T0.1–T0.4) + Phase 1 **T1.1+T1.2** (the
  visible "Voz: …" engine badge — validated `UIState.engine_status` field + status-bar pill that
  shows the EFFECTIVE engine).
- **RESUME NEXT SESSION AT: Phase 1 T1.3 + T1.4** — persist the effective engine + emit
  `[ACCEPT] tts_engine …` markers (`llm_engine.py` after L1584, legacy lines byte-identical) and
  wire `engine:*` events to the badge (`llm_engine` + `app_shell._handle_motor_event`). Full
  steps in `progress.md`.
- Earlier this session (also uncommitted): the APP_ID heavy-TTS silent-fallback bug fixed
  (`server_qwen.py:102` `voiceai-` → `opencohost-qwen-tts`); tracks board reconciled (2 closed,
  1 reclassified); YouTube chat compliance RESCOPED (read-only own-key, no OAuth) + RF4/stream_admin
  marked LEGACY; `.gitignore` leak (`config/tts_speed.json`) plugged.

---

## LATEST SNAPSHOT — Benchmark triage + audio PR (2026-06-13 PM)

First live production benchmark (24k-viewer stream) was run and triaged by a
28-agent fan-out. Snapshot for cross-session verification:

- **PR #45 `fix/audio-teardown-stop` — CI GREEN, MERGEABLE, NOT yet merged.**
  Fixes the O8 audio-teardown cluster (music kept playing minutes after a
  co-host segment; emergency stop did not interrupt speech). 4 bugs + a
  DEFERRED soft-stop. Strict TDD (25 tests), passed Judgment Day 3 rounds
  (both judges APPROVED). Owner decision: soft_stop = deferred (music stops
  after Kira's closing speech, minutes-late OK); emergency_stop = immediate.
  - **PENDING (owner): runtime ear-validation** — confirm music stops deferred
    on soft-stop and immediate on emergency-stop. Then merge (or merge then
    validate — owner's call).
  - CI note: the only CI failure was the app_shell.py line-count guard
    (test_integration.py); resolved by raising 3100→3160 with documented debt.
    app_shell decomposition stays owned by ui_rendering_optimization_20260609.

- **Benchmark triage report:** `conductor/benchmark_20260613_triage.md` (23
  findings). Two operator perceptions were REFUTED with evidence: the "5-min
  downtime" was ~2m3s (~78% inevitable model load; fix = keep rollback model
  warm); the "20-min stream hijack" was operator absence, not a hang.
  The real "hijack feel" driver is a CONFIRMED bug: runaway generation
  (gemma4:e4b pops num_predict → 97s monologues) → routed to
  kira_history_summarization_20260611.

- **New track proposal (UNCOMMITTED, awaiting approval):**
  `conductor/tracks/viewer_queue_backpressure_20260613/proposal.md` — bounded
  viewer-query queue + sectioned-accumulation config. Blocked behind two
  prerequisites in kira_history_summarization_20260611 (runaway-generation cap
  + operator priority=0 lane). NOT a crash fix.

- **Uncommitted on master:** proposal.md + tracks.md entry + this snapshot.
  Worktree with the PR branch: `.claude/worktrees/agent-a7c551c89720a2e7f`.
  Engram: #1891 (verified triage), #1892 (backpressure idea), #1899 (audio fix).

- **Still owed (other sessions):** pipeline memory L1 DEBUG re-run for technical
  proof of `<memoria_de_fondo>` injection (log showed digest:0 = INFO-level
  artifact, not a failure); UI-jank fixes under ui_rendering_optimization.


## Product direction update ? 2026-06-05

- The next product/release direction is **OpenCohost**.
- User reports `OpenCohost.com` is purchased for 3 years.
- User reports the target repo is secured at `plynte-labs/opencohost`.
- Treat this as launch-readiness preparation first, not a blind rebrand.
- Current Conductor track: `opencohost_launch_readiness_20260605`.

## Migration and validation update — 2026-06-10

- `public_repo_migration_20260610` is implemented and verified: a 4-PR
  feature-branch chain (PRs #12, #13, #15, #16) on tracker
  `feat/public-repo-migration` — MERGED to `master` on 2026-06-11 (see the
  validation update section below).
  - Preventive pre-commit guard active: detect-secrets (pinned) + drive-letter
    path hook (`tools/check_abs_paths.py`); full-tree run exits 0.
  - Identity renamed to OpenCohost / plynte-labs; curated default profiles ship
    in `opencohost/config/default_profiles.json` with first-run seeding.
  - Sensitive/user-state files untracked: `perfiles.json`,
    `config/music_library.json`, `opencode.json`, `.engram/`.
- Tracks closed: `local_light_tts_piper_20260610` (Piper offline fallback
  implemented; offline gate PASSED 2026-06-11, branch content already in
  `master` via the migration chain — fully closed) and
  `portable_tts_runtime_path_20260610` (delivered by migration PR2).
- New P0 tracks: `runtime_validation_gates_20260610` (Gate 3 partial pass,
  Gate 4 preliminary pass on chain tip; Gates 1-2 owner-pending — see the
  track's `validation_log.md`) and `opencohost_repo_export_20260610`
  (fresh-history export runbook; blocked on chain merge).
- Owner items open: OBS WebSocket password rotation, `detect-secrets audit
  .secrets.baseline`, `Documents/` public-curation decision. (Gates 1, 2 and 4
  passed as of 2026-06-11 — see the validation update below.)

## Validation update — 2026-06-11

- Migration chain MERGED to `master` (PRs #16-#20; `origin/master` @ ec0a95c).
- **Gate 1 (heavy model inference recovery): PASS** — real watchdog timeout on
  `gemma:26b` (45s window), automatic rollback to `gemma4:e4b`, stalled model
  unloaded, no zombie process. `heavy_model_inference_recovery_20260609` is
  closed (`[x]` in `conductor/tracks.md`).
- **Gate 2 (Piper offline fallback): PASS** — offline trigger and three full
  Piper pipelines proven live (morning session), online positive half proven in
  a later session (Edge-TTS speaks while connected, fallback does not engage).
  Gate text amended: Edge-TTS resumes on next app start (sticky-per-session
  fallback is the accepted design). Fix delivered under this gate's scope:
  missing-reference hard blocks removed and routed through the auto-fallback
  gate (`reason=missing_reference`) — PR #22, TDD + fresh review, 129 passed.
- **Gate 3: PARTIAL PASS** — Evidence C postponed by owner. Startup decision
  (2026-06-11): attach-only with clear guidance for the OpenCohost launch;
  demand-driven managed auto-start deferred to the post-launch
  `qwen-tts-lifecycle-hardening` proposal (12GB VRAM contention between the
  LLM and heavy TTS needs real lifecycle design, not a patch).
- **Gate 4 (runtime smoke harness): PASS** — re-stamped on `master` @ ec0a95c
  (deterministic mode, exit 0, all five invariants true).
- Future proposal candidate recorded: request replay after watchdog recovery
  (`conductor/recovery_request_replay_idea.md`) — user requests are currently
  dropped when the inference watchdog fires.
- Evidence details: `conductor/tracks/runtime_validation_gates_20260610/validation_log.md`.

## Editorial cards CLI — 2026-06-11 (uncommitted on master)

Outcome of the Kira memory/context audit (engram topic
`architecture/kira-memory-subsystem`): the user chose to wire the existing
editorial cards backend through a CLI usable by humans AND external agents,
so Kira can receive curated internet/stream-topic context.

- Shipped (strict TDD, 22 tests green, NOT committed yet):
  `opencohost/editorial_cli.py` (`python -m opencohost.editorial_cli`),
  `EditorialCardStore.list_all()`, `tests/test_editorial_cli.py`.
- Agent/operator contract: `docs/EDITORIAL_CARDS_CLI.md` (execution model,
  idempotency, exit codes, retry policy).
- Conversational memory itself is unchanged (20-message deque, no
  summarization) — deferred by design to pending track
  `kira_history_summarization_20260611` in `conductor/tracks.md`.
- Runtime validation pending: create + arm a card via CLI, then confirm the
  agenda bridge injects `<editorial_context>` into a real Kira prompt.

## Topic inbox — 2026-06-12

Backlog #2 delivered (branch `feat/topic-inbox`): external agents propose
stream topics; the operator approves in the app UI.

- Core: `opencohost/core/topic_inbox.py` — same DB as editorial cards;
  validation at write AND read time (rows failing read-time validation are
  never approvable, even via direct SQLite writes — edge case 5); caps
  (title 120 / angle 600 / 8 tags / 30 pending); slug dedupe; fail-open.
- CLI: `python -m opencohost.editorial_cli topic propose/list/discard`
  (+`--from-json`). `topic approve` is always refused — human-only gate.
  Contract: `docs/EDITORIAL_CARDS_CLI.md`.
- UI: `opencohost/ui/topic_inbox_bridge.py` polls every 7s fail-open;
  proposals render in the suggestions panel tagged 🤖 with the angle
  visible; approve creates an APPROVED+QUEUED agenda topic.
- Known sharp edge: the code/HTML rejection patterns (mirrored from
  BULK_CODE_PATTERNS by design) reject titles containing common words like
  "from"/"update"/"delete". Same behavior as agenda topics today; refine
  only with a product decision.
- Runtime validation pending (owner): propose via CLI while the app runs →
  suggestion appears within ~7s → approve → topic enters the agenda queue.

## Agenda persistence — 2026-06-12

Backlog #3 delivered (branch `feat/agenda-persistence`): queued/approved
agenda topics and session settings survive app restarts.

- `opencohost/core/agenda_persistence.py` — agenda_topics/agenda_settings/
  agenda_meta tables in cards.db; write-through on real change (fingerprint
  gate); bounded Tk-thread timeouts; fail-open with one-time operator
  warning; restore via filtered SELECT (cap 50) through the controller
  sanitizer.
- Deliberate non-features (owner decisions): runtime counters never persist
  (ACTIVE re-hosts fresh as queued); the agenda switch always starts OFF
  after a restart (broadcast safety); COMPLETED/SKIPPED/DRAFTED are not
  persisted. The recurring-topic library is a future proposal track.
- Inbox approve now persists the topic BEFORE claiming the ti_ row
  (crash leaves a visible duplicate, never a silent loss).
- **Runtime gate PASSED (owner, 2026-06-12)**: Task-Manager kill →
  relaunch → queue intact and ordered, settings survive enable, agenda
  OFF. The gate caught and we fixed two real defects: an init-order
  launch crash (PR #40) and an invisible restore — the panel never
  refreshed after load (PR #41; restore is now logged to
  acciones.jsonl as "Restaurados N tema(s)"). Track CLOSED
  (PRs #39 + #40 + #41 merged).

## Audit — 2026-06-12

Dual-reviewer audit run on 2026-06-12. Key findings:

**(a) Node-24 verified done.** All five GitHub Actions were checked at their
pinned tags and confirmed node24 (see Packaging section below for details).
No action required before the 2026-06-16 deadline.

**(b) Four tracks audited — all remain OPEN.** Earlier session notes created
an impression that some were closeable; that was premature. Corrected status:
- `runtime_smoke_harness_20260606` — track folder is ABSENT from
  `conductor/tracks/`; cannot verify closure from local files. Stays [~].
- `ui_rendering_optimization_20260609` — Phases 1–3 done in progress.md,
  but Phase 4 (DPI/canvas snapping, FR4) has no progress entry — unstarted,
  not formally N/A. Stays [~].
- `runtime_validation_gates_20260610` — Gate 3 is PARTIAL (Evidence C
  PENDING, owner-postponed but not closed). Stays [~].
- `dynamic_model_management_20260608` — Phase 3 tasks all [ ]; "deferred"
  was an over-read — only the download-resilience sub-slice is deferred,
  not Phase 3 core. Stays [~].

**(c) Repo hygiene / audit track proposal created** at
`conductor/tracks/repo_hygiene_audit_20260612/proposal.md`. Collects
low/medium code-hygiene findings from this audit (gitignore gap, dead code,
filter pattern duplication, stale path refs). PROPOSAL ONLY — not yet
scheduled or implemented.

**(d) Two findings are pipeline-memory prerequisites** (R3: commit-before-TTS
history pollution; R4: injection-laundering in the future digest) and one is
a `dynamic_model_management_20260608` test gap (R2: watchdog pending-switch
sub-branch untested at `opencohost/core/llm_engine.py:~1191-1198`). All
three are documented in the proposal and routed to their respective tracks.

## Pipeline memory L1 — 2026-06-13 (implemented, PR open, runtime gate pending)

Kira's intra-session memory digest (backlog #5 / `kira_history_summarization`
track, L1 slice). Branch `feat/pipeline-memory`, NOT yet merged.

- Owner-approved design after a deep walkthrough: D1 deterministic truncation
  ledger, D2 survival matrix (survives watchdog/model-switch, dies on
  profile-change/clear, RAM-only), D3 direct-path-only injection wrapped in a
  read-only `<memoria_de_fondo>` block, capped ~600 chars, re-sanitized.
- E3 defense-in-depth: Spanish injection-marker floor + structural isolation
  (the primary, language-agnostic gate) + a SYSTEM_PROMPT read-only rule.
- Strict TDD (50 focused tests); passed a 3-round dual adversarial review
  (Judgment Day APPROVED, 0 critical / 0 real warnings). The review caught and
  fixed 3 real bugs the broad suite missed (inverted `[hace N]` counter,
  agenda-reply digest leak on cross-source eviction, a `_commit_history` race).
- Future layers L2 (session snapshots) and L3 (long-term retrieval) are an
  aim-high RFC at `conductor/tracks/kira_memory_architecture_rfc_20260612/`.
- Non-blocking follow-up tickets (wrapper-breakout scrub, clear()-under-lock,
  chat→direct bleed verification) at
  `conductor/tracks/pipeline_memory_followups_20260612/`.
- **Runtime validation pending (owner):** converse, force a model switch /
  watchdog recovery, and confirm Kira keeps the thread via the digest.

## Packaging update — 2026-06-11 (track paused, mark of record)

The packaging & distribution track executed Phases 2–4 end-to-end in one day
(PRs #26, #28, #29, #30 — all merged to `master`):

- **Phase 2 — package restructure (DONE)**: all app code lives in the
  installable `opencohost/` package; hatchling build, `gui-scripts` entry
  point (`python -m opencohost` / `opencohost`), `uv.lock`, version 0.1.0.
  Full suite green for the first time ever: 1850 → 1926 tests. Root-caused
  the owner's two "PC crashes" to a test-suite OOM (CustomTkinter widgets
  walking MagicMock parents — fixed).
- **Phase 3 — launcher (DONE, code-complete)**: `packaging/launcher.py`
  (stdlib-only uv bootstrapper, splash with progress, Ollama preflight,
  `--update`/`--debug`/`--headless`/`--self-test`), PyInstaller spec,
  82 unit tests. Critical fixes already in: install from unpacked source
  (named spec would have hit PyPI), window-title prefix match, PEP 440
  pre-releases, zip-slip guard, bundled CA certificates (see below).
- **Phase 4 — CI + release (DONE, proven)**: `ci.yml` runs the full suite on
  every PR (windows-latest, ~4.5 min). `release.yml` on `v*` tags builds
  `opencohost-src-<ver>.zip` + `OpenCohost-Setup-<ver>.exe` + `SHA256SUMS.txt`
  into a DRAFT release — ran end-to-end successfully for `v0.1.0`.
  Owner runbook: `docs/RELEASE.md`.
- **Phase 5 — clean-machine validation (PAUSED, NOT passed)**: first VM run
  failed at download with `SSL: CERTIFICATE_VERIFY_FAILED` — clean Windows
  lacks GitHub's root CAs (lazy AuthRoot population; frozen Python never
  triggers it). Fix merged (PR #30: cacert.pem bundled into the exe) but
  **no exe containing the fix has been built yet** — the `v0.1.0` exe
  predates it. The launcher is NOT yet validated on a clean machine.

To resume Phase 5 later (in order):
1. ~~Bump `__version__` to `0.1.1`, tag `v0.1.1`, push tag → new draft exe.~~
   DONE 2026-06-11: `v0.1.1` draft built green (first exe with the CA fix);
   Node-24 action bumps landed (PR #31 + pinned `setup-uv@v8.2.0`).
2. Repo visibility decision: the launcher downloads anonymously, so the
   release must live on a PUBLIC repo (make this repo public, or export to
   `plynte-labs/opencohost`). Private repo → 404 after the SSL fix.
3. Publish the draft (draft assets are not anonymously downloadable).
4. VM: delete `%LOCALAPPDATA%\OpenCohost`, run the new exe; on failure read
   `bootstrap.log` there. Known-good shortcut while testing: opening
   github.com in Edge inside the VM populates the OS cert store.

Node-24 action bumps: DONE and verified (PR #31 + follow-up pin commit
2026-06-11). All five actions confirmed node24:
`actions/checkout@v6`, `actions/setup-python@v6`,
`actions/upload-artifact@v7`, `actions/download-artifact@v8`,
`astral-sh/setup-uv@v8.2.0`.
The `windows-latest` → `windows-2025-vs2026` redirect (2026-06-15) is
automatic GitHub infra — NON-BLOCKING, no file change required. One
validation CI run on or after that date is advisable but optional.

## Current project truth

- OpenCohost has functional prototypes for local AI voice, TTS, SmartAggregator, stream
  workflows, and health monitoring.
- The project has grown enough that blind expansion is risky.
- Branding: VoiceAI→OpenCohost rebrand complete in docs + runtime (logger name,
  log-file prefix `opencohost_*.log`, env vars `OPENCOHOST_DEBUG`/`_CRASH_LOG`/
  `_FATAL_LOG`, identifiers, operator log strings); merged on
  `audit/comprehensive-review`. Deferred by design: the `VocalAI` class
  identifiers (`MotorVocalIA`, `VocalAIApp`) — see docs/architecture.md.
- Active local implementation checkpoint: `dynamic_model_management_20260608`
  is in progress under a **thin client over Ollama** boundary.
  - completed locally: Phase 1 (runtime validity + persistence) and
    Phase 2 (installed-model discovery merge in `ModelPanel`)
  - validated locally: focused model-management suite reached `154 passed`
  - deferred intentionally: download/retry/watchdog orchestration
- Closed bug-recovery checkpoint: `heavy_model_inference_recovery_20260609`
  is implemented AND runtime-validated (Gate 1 PASS, 2026-06-11).
  - completed: watchdog around first real chat after switch, stuck-processing recovery,
    pending-switch escape path, and rollback to last known good model
  - validated: focused recovery/model-management suite `159 passed` + real
    watchdog/rollback event against `gemma:26b` (logs/voiceai_20260611_084746.log)
  - RE-VALIDATED 2026-06-17 (release gate #1 confirmed): inference watchdog timeout
    (45s) + automatic rollback against real stalling model `qwopus`; app kept
    processing the queue without restart (logs/opencohost_20260617_175453.log)
- `health-monitor-auto-fallback` has been reconciled:
  - keep: HealthMonitor core, health pill, Vibe gate, heavy-TTS fallback gate
  - adjust only if needed: thresholds, docs wording, manual-vs-auto fallback policy
  - do not claim complete: Qwen demand-driven auto-start and idle shutdown
- Future focused track, only if runtime validation proves it matters:
  `qwen-tts-lifecycle-hardening`.

## User-owned validation before more expansion

The user still needs to validate real runtime behavior:

- heavy vs light TTS
- visible fallback behavior
- manually started Qwen server
- Ollama online/offline behavior
- health pill state changes

Treat these as release-readiness gates.

## Deferred for now

Do not pick these up unless the user explicitly re-prioritizes them:

- `knowledge_card_mvp`
- packaging Phase 5 resumption (see "Packaging update — 2026-06-11"; Phases
  2–4 are DONE and the pipeline is operational — only the clean-machine
  validation and the repo-visibility decision remain)
- broad hardening and failure testing
- first-run readiness wizard
- large Product UI implementation

Product UI can be reviewed later against real states, but do not implement it yet
without a fresh user decision.

## Safety rules that matter most

- Do not commit unless the user explicitly asks.
- Do not revert or overwrite existing user changes.
- Do not remove feature gates, filters, or validation without verifying current behavior first.
- Never expose raw chat to LLM prompts, diagnostics, logs, or persistence.
- Keep LiveVoice continuous and PTT separate unless the user explicitly asks to touch them.
- Pre-commit safety hooks are expected to block private/runtime artifacts and obvious secrets.
  Do not bypass them unless the user explicitly approves after manual review.

## Known useful test commands

Targeted health validation (activate your project Python environment first):

```powershell
python -m pytest tests/test_health_monitor.py tests/test_health_integration.py tests/test_app_shell_obs_resilience.py -q
```

## Important local artifact notes

- `sdd/` and `openspec/` are ignored by `.gitignore`; they are local artifact-store notes.
- The honest current claim is: prioritized/relevant SDD was reviewed and reconciled.
  Do not claim that every SDD track is fully complete.
