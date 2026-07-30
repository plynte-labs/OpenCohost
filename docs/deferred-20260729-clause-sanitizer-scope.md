# Deferred Scope Register — clause_sanitizer V1 (2026-07-29)

Companion to `temp/plan_v1_corrected.md`. Everything **deliberately left out** of the
intra-speech clause sanitizer unit, with the reason and the condition that would bring it back.

Nothing here is a TODO. Each row is a decision with evidence behind it. Rows marked
**BLOCKED-BY-EVIDENCE** must not be picked up without new runtime data.

Tags: `[CODE]` verified in source 2026-07-29 · `[MEASURED]` repo provenance · `[INFER]` reasoned ·
`[PRODUCT]` owner decision · `[GAP]` no evidence exists.

---

## 1. Deferred because implementing it would be a regression

### 1.1 `record_success()` at `kira_agenda_controller.py:966` and the CLOSING ladder

**Status: BLOCKED-BY-EVIDENCE. Own unit, own commit.**

The earlier plan proposed adding `self.recovery.record_success()` unconditionally inside the
topic-exhaust rung, to stop `failure_count` from carrying across topic transitions. Traced
against source, that line makes the counter oscillate and the topic never close: `[INFER]` from `[CODE]`

```
ACTIVE topic, 2 failures → :966 fires → WAITING_SIGNAL, failure_count → 0
tick → next_action :840 _topic_complete() True → :841 _closing_action() → CLOSING, GENERATING
closing rejected → failure_count 1 → :958 no (1<3), :966 no (1<2) → :989 REGENERATING_SAFE
tick → :813 REGENERATING_SAFE → WAITING_SIGNAL → :841 _closing_action() again
closing rejected → failure_count 2 → :966 YES → record_success → 0
tick → :841 _closing_action() again → ...
```

`failure_count` cycles 0→1→2→0 and never reaches 3, so the force-complete rung at `:958`
(`>= 3`) is **unreachable**. Each cycle is one more `kira-agenda-stop` LLM call. That is exactly
the incident the comment at `:954-957` records as fixed: *"Prevents the infinite
kira-agenda-stop retry cascade seen under heavy chat + guardrail stress (20+ LLM calls in a
row)."*

Traced through: `register_failure` `:938-989` · `next_action` `:798-845` · `_closing_action`
`:1339-1347` · `_topic_complete` `:1464-1465`.

Two further blockers:
- **Breaks a pinned assertion.** `tests/test_kira_orchestration_gaps.py:742`
  (`test_two_consecutive_empties_abandon_topic`) asserts `ctrl.recovery.failure_count >= 2` at
  exactly this branch with an ACTIVE topic. `[CODE]`
- **The repo already has the tripwire.** `test_persistent_empty_responses_are_bounded_not_infinite`
  (`:746`) exists to catch an unbounded loop here.

**Correct shape when this unit is picked up:**

```python
if self.active_topic and self.recovery.failure_count >= 2:
    self.active_topic.turns_spoken = self.max_turns_per_topic
    self._character_repair_needed = False
    self.state = AgendaState.WAITING_SIGNAL
    if self.active_topic.status != TopicStatus.CLOSING:   # keeps :958 reachable
        self.recovery.record_success()
    return
```

Traced: reset on ACTIVE exhaust → CLOSING starts at 0 → reject 1 → `REGENERATING_SAFE` →
reject 2 → `:966` fires but status is CLOSING so no reset → reject 3 → `:958` force-complete.
Bounded at 3 closing generations.

**Why the sanitizer does not need it.** The drain concern is **pre-existing** — every
`GUARDRAIL_*` rejection already has it. Tier 2 adds one more producer of the same failure type;
it does not create the behavior. `CLAUDE.md` forbids altering recovery gates without verifying
behavior first; the verification says the naive line is unsafe.

**Re-entry condition:** owner asks for it as its own unit, accepting that
`test_kira_orchestration_gaps.py:742` changes contract. Required tests are listed in
`temp/plan_v1_corrected.md` §10.

### 1.2 Force-complete comment is already inaccurate today

`[CODE]` Because `failure_count` carries over from the ACTIVE phase, `:958` fires on the **first**
closing rejection whenever the count arrived at ≥2 — not "after failing to generate a closing
line 3+ times" as `:954-957` states. Documentation drift, live behavior. Fixing the comment
belongs to unit 1.1, since the fix changes which statement is true.

---

## 2. Deferred because no evidence justifies it

### 2.1 Arming the sanitizer for `chat`, `direct`, `ptt`, `accumulated`

**Status: BLOCKED-BY-EVIDENCE.** Config exists; default is off.

The original justification was a fallacy: *"these sources have no repetition defense, therefore
the sanitizer is more needed there."* Absence of a guard describes the code; it does not
demonstrate a defect. The only confirmed intra-sentence degeneration incident is an **agenda**
response. `[GAP]` — no such incident on any other source anywhere in this repo.

And there is a concrete false positive that exists **only** on operator-facing sources: an
instructed repetition. `[INFER]` from `[CODE]`

```
operator: "repetí tres veces 'no toques ese botón'"
Kira:     "No toques ese botón, no toques ese botón, no toques ese botón."
```

Normalized key `no toques ese boton` = 19 chars, above the 12-char floor, three occurrences in
one sentence → collapsed to one copy, classified `repaired`, silently. Agenda cannot produce
this case: it is an autonomous monologue, nobody instructs it.

**Re-entry condition:** a real logged incident of intra-sentence exact clause degeneration on
that specific source. Telemetry accrues the evidence automatically — the severity verdict is
computed and logged for every armed source even where tier 2 cannot act.

### 2.2 Intent detection for "please repeat" requests

**Status: REFUSED, not deferred.** `[PRODUCT]`

No regex, no phrase list, no extra LLM call to recognize a repetition request. Spanish can
express it in unbounded ways; a matcher would be a regex test lab for an unobserved problem and
a new failure surface bought with no evidence. `tests/.../test_no_intent_detection_exists`
exists to fail if it is ever smuggled in.

### 2.3 Tier 2 (reject → regenerate) for non-agenda sources

**Status: structural, not configurable.** `[CODE]`

Tier 2 needs an owner for the regeneration. Only agenda has one — the ADR-011 ladder, reached
by returning `""` (`llm_engine.py:5240` → `register_failure`). `chat`/`direct`/`ptt` never
traverse it: the gates at `:3463` and `:3480` exclude them. So for any other source the
sanitizer is repair-only, even when armed. Arming a source does not arm tier 2 for it.

**Re-entry condition:** a decision on the direct/PTT reject policy (see 2.4), plus a real
incident on that source.

### 2.4 direct/PTT reject policy — one regeneration vs immediate fallback

**Status: deferred, analysis recorded.** Not live in V1 (sanitizer disarmed there and tier 2
agenda-only, so neither policy executes).

| | **A — one regeneration** | **B — immediate fallback** |
|---|---|---|
| Extra LLM calls | 1 (`_retry_after_guard_block` `:2833-2867`) | 0 |
| Added latency | one full non-streaming generation. **NIM: `[GAP]`**, only bound is `CLOUD_CHAT_TIMEOUT=90 s` | none |
| Reliability | success rate **unmeasured** `[GAP]` and **negatively correlated** — ADR-011 D4 puts root cause at model level, so a model that just degenerated is likelier to do it again on the same prompt | deterministic: always a spoken line, never the degenerate text |
| Cost of failure | operator waits, then still re-asks | operator re-asks once |

**Recommendation if armed: B.** `[PRODUCT]` The operator is present and re-asks in seconds, so
latency is the scarcer resource on an operator channel; spending an up-to-90 s cloud call on an
unmeasured, negatively-correlated retry is not a trade the evidence supports. Viewer-facing
`chat` needs its own decision — a viewer cannot re-ask.

**Do not transfer Ollama figures to NIM.** Every latency number in this repo is local Ollama on
an RTX 3060 (`ADR-013:59-67`). NIM has zero measurement; `settings.py:105-108` labels
`CLOUD_CHAT_TIMEOUT=90` a placeholder awaiting phase-4 runtime validation. `[GAP]`

### 2.5 Threshold tuning

One pass after real agenda telemetry accrues. All six `SANITIZE_*` values are `[INFER]` anchored
to `[MEASURED]` data with one-notch-either-side justification; none is itself a measurement.
First input to the pass: the residual false positive in 2.6.

### 2.6 Residual agenda false positive — accepted, documented

`[INFER]` A genuine Spanish rhetorical triple whose clause exceeds 12 chars **is** collapsed:
`"Que se vayan todos, que se vayan todos, que se vayan todos."` (key 18 chars). The
`SANITIZE_SHORT_EXPR_MAX_CHARS = 12` floor protects `"no, no, no"` and `"que se vaya"` (11
chars) but not this. Telemetry makes it visible. No intent heuristic is added to guess around it.

---

## 3. Deliberately not detected by the algorithm

Each is a design boundary, not an oversight. `[CODE]` for the coverage claims.

| Not detected | Why | Who covers it today |
|---|---|---|
| Cross-sentence repetition (refrains, anaphora, parallel lists) | Protected by design — within-sentence scope. `"Micro, sigue sin funcionar. Cámara, sigue sin funcionar."` must survive | `has_looping_lines` (`kira_agenda_controller.py:1204-1211`, sentences >24 chars) on agenda; nothing on chat — unchanged from today |
| Fuzzy / near-duplicate clauses (word swapped, reordered) | Owner rule: exact normalized equality only in V1 | cross-turn skeleton detector `detect_repetition` (`repetition_guard.py:109-157`) at line granularity |
| Non-adjacent exact doubles (`A, B, A.`) | Binding owner rule — kept in V1 | nothing |
| Short-expression loops (key ≤ 12 chars, e.g. `"sí,"` ×20) | Rhetorical floor; never observed; collapsing them risks Kira's register | nothing |
| Alternating loops (`A, B, A, B`) | Each key is a non-adjacent double → kept per rule | nothing |
| Newline-separated repeats without punctuation | No delimiter boundary. **Honest pre-existing gap:** the adjacent-line check at `kira_agenda_controller.py:1206-1208` is dead on agenda because `_sanitize_agenda_output` (`llm_engine.py:5262-5278`, applied `:3370`) collapses newlines first | nothing |
| Colon-delimited clauses | `:` is not a boundary; never observed; adding it risks enumerations and times (`20:30`) | nothing |
| Full-sentence duplicate at the pregen connector junction | Not clause-visible under within-sentence scope. The concatenation is not re-validated today either (`llm_engine.py:1994-1997`) | nothing — pre-existing, unobserved |

---

## 4. Out of scope by owner instruction

| Item | Note |
|---|---|
| Interruption state machine | Separate unit. No chat-interrupt implementation until `operator_voice`, `operator_text`, `viewer_chat`, `system_command` are distinguished in an explicit design. **That design starts by deciding whether `system_command` should exist** — see 5.1 |
| Stream / connections migration to Tauri | Owner's stated order, after this unit |
| General refactors | Untouched: `detect_repetition`, `output_guard` internals, `_retry_after_guard_block`, `_accept_agenda_output`, `_ejecutar_inferencia`, LiveVoice/PTT separation |
| CustomTkinter (`opencohost/ui/`) | Read-only legacy reference. This unit changes only the shared backend and its tests. Verified: `llm_engine.py` has zero `customtkinter`/`tkinter` imports, and all five touched methods are shared-backend `MotorVocalIA` members instantiated by both surfaces (`app_shell.py:282`, `api/engine_host.py:612-616`) `[CODE]` |
| STT, transcript generation, input audio | See 5.2 — none of it exists |
| Real-audio TTS tests | Assert on `_hablar` (`llm_engine.py:5280`), the str-receiving seam, with the repo's established mock (`tests/test_dialogue_callback.py:40`). No audio, no TTS server |

---

## 5. Removed from the plan because the referent does not exist

### 5.1 `system_command` is not a source

`[CODE]` Repo-wide grep: **zero matches** in any file. System commands are a different
dimension — a `tipo` in `_dispatch_command` (`llm_engine.py:1286-1470`), which enumerates
`set_voice`, `check_ollama`, `process_context`, `clear_history`, `switch_model`,
`switch_llm_tier`, `set_motor_tts`, `set_tts_local_only`, `set_tts_speed`, `set_piper_voice`,
`set_profile`, `download_model`. Only the `process_context` branch (`:1326-1352`) reaches
`_ejecutar_inferencia`; the other eleven never do. Commands carry no `source` at all.

Removed from the source matrix and from the config allowlist.

### 5.2 There is no STT and no transcript

`[CODE]` OpenCohost does not do speech recognition; it **receives already-transcribed text**.

- `api/ptt_session.py` docstring `:1-20`: recv-only WebSocket consumer of an external
  WhisperLive-style STT server — *"zero audio bytes cross Python."*
- `_ws_main:223-260` connects; `_recv_loop:262-297` only calls `ws.recv()` (`:286`);
  `_ingest:303-313` is `json.loads(msg)` then `data.get("text")`. No model call.
- Repo-wide: no `whisper`, `faster_whisper`, `vosk`, or `speech_recognition` import in
  `opencohost/`. **NOT FOUND.**
- External address: setting `stt_ws_uri` (`settings.py:824`), default `ws://127.0.0.1:8765`
  (`:276`).
- No recognized speech is written to disk. **NOT FOUND.** The only local WAV write
  (`app_shell.py:2339`) is a TTS voice-cloning reference recording.

**"Transcript" has no referent.** What exists is a **last-reply sink**:

| | |
|---|---|
| File | **none** |
| Function | `_emit_dialogue(text, source)` — `llm_engine.py:5761-5773`, only calls `self.dialogue_callback(text, source)` |
| Structure | `ChatReplySink` — `api/engine_host.py:150-176`, bounded thread-safe deque of `{text, source, turn_id, ts}`, wired `:615` |
| Consumer | `GET /api/chat/last-reply` (`api/main.py:2302`) → React poll every 1.5 s (`OpenCohost_UI/src/api/chat.ts:13-21`) |

`dialogue_callback` defaults to `None` (`llm_engine.py:426`) and only `engine_host.py:615` sets
it, so in the legacy CustomTkinter path `_emit_dialogue` is a **no-op** — the last-reply sink
exists only in the API/Tauri path.

The three real destinations are: **history/context** (`_commit_history:3494`), **last-reply
sink** (`_emit_dialogue:5219`), **TTS boundary** (`_hablar:5221`).

---

## 6. Adjacent defects found during the audit — registered, not touched

Each is its own micro-unit.

| # | Defect | Evidence | Why not here |
|---|---|---|---|
| D-1 | **Raw dialogue at WARNING.** `log_non_negotiable_block(..., preview=...)` logs `preview=%r`; every `output_guard` block site passes `response[:120]` → up to 120 chars of generated dialogue in `logs/opencohost_*.log` at the default level, no debug flag needed | `validation.py:587`, `:601-604`. `SensitiveDataFilter` (`logger.py:21-41`) redacts credentials, **not** dialogue `[CODE]` | Sits against the owner's zero-text telemetry rule but is not this unit's change. Fix ≈ 4 lines: drop the `preview` parameter, keep `rule_id`/`layer`/`description`, add `len(response)`. Test: `caplog` assertion that no substring of a sentinel phrase appears at any level |
| D-2 | **Legacy PTT mistags itself as `direct`.** `voice_control.py:244-250` puts a 3-tuple with no `source`; `_consume_command:1281-1283` defaults it to `"direct"`. The busy path in the same function (`:225-234`) correctly passes `source="ptt"` | `[CODE]` | Lives in `opencohost/ui/` — out of bounds. Relevance: arming/disarming `ptt` in the sanitizer config would not reliably cover legacy-UI PTT, which lands in the `direct` bucket |
| D-3 | **Busy re-enqueue hardcodes `priority=1`** — a PTT turn routed through `process_context` loses its expected priority 0 | `llm_engine.py:1338` `[CODE]` | Unrelated behavior |
| D-4 | **Agenda-synthesized `chat`/`ptt` turns are indistinguishable from genuine ones.** `_chat_action`/`_streamer_action` emit actions tagged `source="chat"`/`"ptt"`; all downstream logic keys off the string alone | `kira_agenda_controller.py:1310-1337` `[CODE]` | Accepted. Noted so per-source config semantics are not oversold |
| D-5 | **No headless-audio fallback.** `MotorVocalIA.run()` calls a real unstubbed `pygame.mixer.init()`; if it raises, the `except` **returns out of `run()` entirely** — the whole engine thread dies, not just speech | `llm_engine.py:1217`, `:1218-1220` `[CODE]` | Deployment-validation concern, not a sanitizer concern |
| D-6 | **`_regen_retry_count` is dead telemetry** — incremented at `:951`, reset at `:763`/`:1045`/`:1113`, and **never read or compared anywhere**. The real ladder bound is `recovery.failure_count` at `:958` (`>=3`) and `:966` (`>=2`) | `kira_agenda_controller.py` `[CODE]` | Cosmetic. Delete or wire it in unit 1.1 |
| D-7 | **`max_intentos = 2` is not a transport retry, and two comments say it is.** Both exception branches `return ""` on the first failure (`:3160-3183` watchdog, `:3184-3234` transport); neither has `continue`. The second iteration is consumed only by in-band conditions. `:3405` calls it "the max_intentos transport-retry loop"; `:3211` logs `"intento {n}/{max_intentos}"` for an attempt that will never happen | `llm_engine.py` `[CODE]` | Comment/log drift only — no behavior change. Fix alongside any future retry work |
| D-8 | **ADR-011 status-line drift** — the header says *"Proposed — investigation complete, not yet implemented"* while its own update log at `:125` records it implemented and validated 2026-06-22 | `docs/adr/ADR-011-…md` `[CODE]` | Reconcile in the ADR addendum this unit already writes |

---

## 7. Measurement gaps this unit does not close

| Gap | Consequence |
|---|---|
| **Zero NVIDIA NIM latency measurement of any kind.** `settings.py:105-108` labels `CLOUD_CHAT_TIMEOUT=90` a placeholder awaiting phase-4 runtime validation `[GAP]` | The dead-air risk cannot be quantified on the configured live provider. Every repo latency figure is local Ollama on an RTX 3060 |
| **`request → TTS receives text` is never measured.** The code logs the two halves separately (`elapsed` `:3304`, `elapsed_first` `:5653`) and never sums them `[GAP]` | Partially closed: the plan adds 2 instrumentation lines. Separable and removable |
| **Fase-2 interactive pregen never runtime-validated** (`enqueue()` → `pregenerate()` `:1570`); `docs/closeout-20260722-agenda-no-dead-air-phase2.md:149-158` lists it as owed `[GAP]` | The only mechanism that could hide direct/PTT latency is unproven |
| **Controller tick cadence unmeasured** `[GAP]` | Agenda wall-clock recovery time cannot be computed |
| **Model attribution of the original incident is unresolved.** The prior plan says `gemma4:e4b` via NIM, but `gemma4:e4b` is an Ollama **local** tag while the front is configured for `z-ai/glm-5.2` `[GAP]` | The live probe targets the configured model |

**Known mechanism finding worth carrying forward** `[INFER]` from `[CODE]`+`[MEASURED]`: agenda turn
boundaries measure 0.34–0.43 s (`ADR-035:211-213`) **only because the pregen slot was filled.**
A tier-2 rejection inside the pregen worker returns `""` and leaves the slot empty, so the
boundary reverts to foreground generation — back into the regime that measured 16.3–18.5 s
(`ADR-035:28-37`). The sanitizer's reject path removes the protection ADR-035 installed, for
that one boundary. This is the reason every threshold gate biases toward repair.

---

## 8. Live NIM tests: in the plan, out of CI

`tests/live_cloud/`, new `live_cloud` marker, autouse gate mirroring
`tests/realenv/conftest.py:9-13`: skip unless `OPENCOHOST_LIVE_CLOUD_TESTS=1`. Excluded from
the normal suite and from CI by default. `[CODE]`

Two obstacles a future run must handle:
- `tests/conftest.py:238-268` is `autouse=True` and redirects `LLM_KEYS_FILE` /
  `LLM_PROVIDER_CONFIG_FILE` to `tmp_path` for **every** test. A live test must explicitly opt
  out or read via `OAuthStore(<real path>)` before constructing the engine.
- No pricing data exists anywhere in the repo `[GAP]` — live tests report provider, model,
  latency and token counts, **never currency**.

**Correctness is proven by `tests/test_clause_sanitizer_e2e.py`, not by the live tests.** The
live tests validate integration, operational behavior, and latency — the three things a
deterministic test structurally cannot see.
