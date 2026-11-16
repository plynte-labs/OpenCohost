# Kira Co-host Agenda Mode

Kira Co-host Agenda Mode is the definitive product direction for semi-autonomous stream hosting. The streamer prepares a small queue of approved topics, and Kira develops them in short, safe turns while listening to PTT and filtered/compacted chat as higher-priority events.

## Decision

Do **not** build a fully autonomous “Kira runs the stream forever” mode. Build a deterministic **co-host with agenda** mode instead.

The human owns direction. Kira owns pacing, transitions, lightweight improvisation, and reacting to compacted context.

```text
approved topic queue
        ↓
Kira opens one topic
        ↓
short spoken turn
        ↓
wait for PTT/chat/timer
        ↓
continue, answer, close topic, or move next
```

## Why this is the product

| Risk in full autonomy | Agenda-mode answer |
|---|---|
| Kira invents direction | Streamer approves topics first |
| Infinite monologue | Short turns + cooldown + state machine |
| GPU/Ollama saturation | One active generation; no infinite pre-generation |
| Repetition | Track last Kira intents/lines and force variation |
| Context leaks | Strict prompt + output sanitizer before TTS |
| Unsafe stop timing | Explicit safe-exit states and emergency stop |

## Core concepts

### Topic

A topic is a compact plan approved by the streamer.

```json
{
  "id": "topic-uuid",
  "title": "Programación orientada a objetos para principiantes",
  "angle": "Explicar con ejemplos cotidianos, sin ponerse académica",
  "constraints": ["no leer definición de Wikipedia", "mantenerlo en 1-2 frases por turno"],
  "status": "queued"
}
```

### Turn

A turn is one short Kira output. It must be small enough to fit the live rhythm.

Rules:

- 1-2 spoken phrases.
- One idea per turn.
- No internal wording: “resumen”, “contexto privado”, “intención dominante”, “el chat dice”.
- Do not mention raw usernames unless the streamer explicitly asked for that behavior.

### Safe exit

The mode should stop only at a safe boundary unless emergency stop is used.

Safe boundaries:

- Kira is idle.
- Kira is waiting for chat/PTT.
- Kira finished a turn and has not selected the next topic.
- Kira is closing the current topic.

Unsafe boundaries:

- Kira is currently speaking.
- TTS chunks are still being produced/played.
- LLM generation is in flight.

## State machine

```text
OFF
  └─ enable → IDLE

IDLE
  ├─ topic available → SELECT_TOPIC
  └─ disable → OFF

SELECT_TOPIC
  └─ topic loaded → OPEN_TOPIC

OPEN_TOPIC
  └─ enqueue Kira turn → GENERATING

GENERATING
  ├─ output accepted → SPEAKING
  ├─ output rejected once → REGENERATING_SAFE
  ├─ too many failures → PAUSED_NEEDS_OPERATOR
  └─ emergency stop → OFF

SPEAKING
  ├─ TTS complete → WAITING_SIGNAL
  ├─ soft stop requested → TOPIC_CLOSING after speech
  └─ emergency stop → OFF

WAITING_SIGNAL
  ├─ PTT event → HANDLE_STREAMER
  ├─ fresh compact chat → HANDLE_CHAT
  ├─ timeout and topic incomplete → CONTINUE_TOPIC
  ├─ timeout and topic complete → TOPIC_CLOSING
  └─ soft stop requested → TOPIC_CLOSING

HANDLE_STREAMER
  └─ enqueue priority response or update topic memory → GENERATING/WAITING_SIGNAL

HANDLE_CHAT
  └─ enqueue chat-aware short response → GENERATING

CONTINUE_TOPIC
  └─ enqueue next planned beat → GENERATING

TOPIC_CLOSING
  ├─ closing line accepted → SPEAKING
  └─ next topic available → SELECT_TOPIC

PAUSED_NEEDS_OPERATOR
  ├─ operator resumes → IDLE
  └─ operator disables → OFF
```

## Event priorities

| Priority | Event | Behavior |
|---:|---|---|
| 0 | Emergency stop | Cancel autonomous loop and autonomous pending work immediately |
| 1 | PTT from streamer | Highest semantic priority; interrupts agenda planning |
| 2 | Kira currently speaking | Do not stack new autonomous turns unless explicitly allowed |
| 3 | Filtered compact chat spike | Can steer the current topic or trigger a short reaction |
| 4 | Topic agenda tick | Continue or close the planned topic |
| 5 | Topic suggestions | Draft only; never auto-approve |

YouTube chat write remains disabled by default. Kira speaks by TTS unless the existing Stream Admin write gates are explicitly enabled by the operator.

## Context model

Kira must never persist raw comments.

```text
PTT transcript ───────────────┐
filtered chat → intent summary ├─→ Co-host Agenda Controller → compact prompt → Motor IA
context_snapshots DB ─────────┘
```

Context sources:

| Source | Lifetime | Use |
|---|---|---|
| Current topic | Until completed/skipped | Main direction |
| PTT transcript | Immediate/high-priority | Human correction or steering |
| In-memory chat summary | Current stream window | React to live audience |
| `context_snapshots` | Persistent compact memory | Fallback, recap, future topic suggestions |
| Last Kira actions | Current mode session | Prevent repetition |

Do not store raw chat in SQLite, JSONL, or hidden debug paths.

## Queue contract

The controller must treat Motor IA as a scarce resource.

Hard rules:

- Only one autonomous generation can be active at a time.
- Never enqueue another autonomous agenda turn while Kira is speaking unless it replaces a stale pending autonomous item.
- PTT outranks agenda output.
- Chat reactions outrank topic continuation but do not outrank PTT.
- Autonomous prompts must be droppable/replacable; streamer prompts must not be dropped.

Recommended source labels:

```text
ptt              # streamer input, never drop casually
chat             # filtered/compacted chat reaction
kira-agenda      # autonomous agenda turn, droppable/replacable
kira-agenda-stop # closing turn, droppable only by emergency stop
```

## Stop behavior

### Soft stop

Use when the streamer toggles Agenda Mode off normally.

Behavior:

1. Mark `stop_requested = true`.
2. Do not select new topics.
3. If Kira is idle/waiting, close immediately.
4. If Kira is speaking, let current audio finish.
5. Optionally generate one short closing line.
6. Transition to `OFF`.

### Emergency stop

Use when the streamer needs Kira silent now.

Behavior:

1. Cancel autonomous loop timer.
2. Drop pending `kira-agenda*` queue items.
3. Stop or mute current TTS if the engine supports it; otherwise ignore completion callbacks.
4. Transition to `OFF`.
5. Log clearly in UI.

Emergency stop does not delete approved topics unless the operator chooses to clear the queue.

## Failure policy

| Failure | First response | Repeated response |
|---|---|---|
| LLM unavailable | Skip tick, show UI warning | Pause mode |
| TTS timeout | Retry once with shorter output | Pause mode |
| Output leaks internal text | Regenerate once with stricter prompt | Safe fallback line |
| Repetition detected | Regenerate with “do not repeat last actions” | Close topic or move next |
| No fresh context | Continue planned topic briefly | Wait or close topic |
| Queue pressure high | Skip agenda tick | Pause agenda until queue drains |

Backoff rule:

```text
1 failure  → retry simpler
2 failures → fallback safe line or skip turn
3 failures → PAUSED_NEEDS_OPERATOR
```

## Anti-leak and anti-hallucination guardrails

### Prompt contract

Every agenda prompt must say:

- You are Kira, a co-host, not the streamer.
- Use context privately; do not describe the context.
- Speak one short live line.
- Do not claim the streamer did/said something unless PTT provided it.
- Do not invent chat consensus.
- If context is weak, make a general transition or ask a light question.

### Output sanitizer

Before TTS, reject outputs containing internal phrases such as:

- `contexto privado`
- `resumen`
- `intención dominante`
- `mensaje destacado`
- `el chat dice`
- `parece que el chat`
- message counts or author-count claims

Rejected output may regenerate once. After that, use a safe fallback line.

## Topic lifecycle

```text
drafted → approved → queued → active → closing → completed
                         └──── skipped
```

Only `approved` topics can enter the queue.

Kira may suggest future topics, but suggestions are always drafts:

```text
context_snapshots → topic suggestions → streamer approves → queue
```

No auto-approval.

## UI requirements

Minimum UI:

- Agenda queue list.
- Add/edit/remove/reorder topic.
- Approve suggested topic.
- Toggle: `Modo Co-host con Agenda`.
- Soft stop button.
- Emergency stop button.
- Current state label: `IDLE`, `SPEAKING`, `WAITING_SIGNAL`, etc.
- Failure counter/status.

Operator-facing wording should avoid implementation jargon. Example:

```text
Kira está desarrollando: “Minecraft en la industria gaming”
Esperando reacción del chat o PTT...
```

## Topic progression (default ON, operator toggle)

Progression is **ON by default** (owner decision 2026-11-16): a fresh install
runs the `auto` mode below. The Agenda panel has a switch ("Topic progression")
that turns it off; the choice persists across restarts and applies to the running
engine without a restart. No extra LLM call is involved.

Resolution precedence (highest first):

1. `OPENCOHOST_AGENDA_PROGRESSION`, when set to a non-blank value, always wins
   (restart required). The panel switch is still saved but is shown as overridden
   by the environment (`env_override: true`) and does not change the effective mode.
2. The stored operator choice in `config/agenda_settings.json`
   (`progression_enabled`, global, not per-locale like the other agenda session
   settings; written by `PUT /api/agenda/progression`). `false` disables
   progression. A missing, corrupt or non-boolean value fails open to ON.
3. The shipped default: ON, variant `auto`.

API: `GET /api/agenda/progression` returns `{enabled, variant, env_override}`
(`variant` is the effective variant, `""` when off); `PUT` takes `{"enabled": bool}`
and returns the same shape (503 `agenda_settings_write_failed` if the file cannot
be written). Turning it off also drops any turn prefetched under the old variant.

Env values (`OPENCOHOST_AGENDA_PROGRESSION`, restart required):

- `0`, `false`, `off`, or unrecognized: progression disabled; the
  assembled prompt is byte-identical to the legacy baseline.
- `1`, `true`, `auto`, or `rotate`: enables **`auto` (dynamic rotation mode)** —
  automatically runs the full 56-card / 8-family move deck, per-topic rotated
  6-facet progression (`pick_auto_progression_facet`), Ledger v2 + anti-self-copy
  validator, continuation length budget (~3 short sentences / ~110 tokens), and
  agenda-only 9B sampling controls (`repeat_penalty=1.18`, local `num_predict<=110`).
- `v1`–`v5`: selects an explicit fixed benchmark variant below (`v1` is the
  14-move baseline).
- unset or blank: the stored operator choice / default above applies.

Every active variant preserves these invariants:

- Opening, closing, N=1 topics, chat, and PTT turns carry no move, no facet,
  and no continuation budget override.
- Move and facet selection are deterministic pure functions of
  `(topic_id, turn_index)` (and `max_turns` for `v5` facets), so `prefetch`
  and `live` always pick the exact same move and facet.
- Ledgers are built in memory only from Kira's own accepted outputs in the
  current topic, reset on topic change, and never include raw viewer chat or
  persist to logs/metrics.

### Progression variants (`v1`–`v5`)

| Variant | Hypothesis / Focus | Move Deck | Ledger | Length & Sampling Controls |
|---|---|---|---|---|
| `v1` | Baseline anti-looping via turn moves and recurring-word avoidance | 14 base moves (`llm.agenda.progression.moves`), no reuse in last `min(4, size // 2)` positions | Spent ledger v1 (~200-char tail + 5+ char words recurring 2+ times, excluding topic words) | Default length rule & sampling |
| `v2` | Long turns (~146 words) cause small local 9B models to self-copy mid-output | 14 base moves | Spent ledger v1 | Continuation turns cap to max 3 short sentences / ~110 tokens (`length_budget` + `length_budget_rule`), agenda-only `repeat_penalty=1.18` (`AGENDA_REPEAT_PENALTY`), and local `num_predict` capped at `AGENDA_V2_MAX_TOKENS` (`110`) |
| `v3` | Larger, family-structured move space prevents predictable skeletons across turns | 56 moves across 8 families × 7 cards (`historical_analogy`, `moral_dilemma`, `counterintuitive_fact`, `chat_question`, `inversion`, `concrete_case`, `future_projection`, `systemic_mechanism`); cross-family shuffle never repeats a family back-to-back and never forces fixed opening phrases | Spent ledger v1 | Default length rule & sampling |
| `v4` | Blocking single-use content keywords and prior opening stems/sentences stops subtle paraphrased self-copying | 56 family moves (`v3`) | Ledger v2: tracks 1+ occurrence content words, up to 5 compact normalized opening stems (`spent_openings_line`), and rejects candidate outputs in `_validate_output` that reuse a prior opening stem or ≥22-char sentence (`ledger_v2_self_copy`) | Default length rule & sampling |
| `v5` | Ordered topic facets + all 9B controls stop small local models from circling the same metaphor on longer topics | 56 family moves (`v3`) | Ledger v2 (`v4`) + anti-self-copy validator | Walks 6 ordered facets (`llm.agenda.progression.facets`: mechanism → human tension → honest counter-angle → social ripple → extreme implication → open synthesis) across N turns (`continue_v5` / `preview_continue_v5`) + `v2` length cap (`length_budget_rule`) & agenda-only `repeat_penalty=1.18` / `num_predict<=110` |
| `auto` (`1` / `rotate`) | Default live streaming mode combining all 9B protections while avoiding cross-topic template repetition | 56 family moves (`v3`) | Ledger v2 (`v4`) + anti-self-copy validator | Rotates the 6-facet order deterministically per `topic_id` (`pick_auto_progression_facet`) so every topic opens and progresses along a different arc, while keeping `v2` length cap & `repeat_penalty=1.18` / `num_predict<=110` (`prefetch == live`) |

### Shared mechanics

- **Move deck (`v1`/`v2` vs `v3`–`v5`/`auto`)**: `pick_progression_move` (14 moves) and
  `pick_family_progression_move` (56 moves across 8 families) are pure
  deterministic functions of `(topic_id, turn_index)`. After a cycle finishes it
  reshuffles while keeping the last `min(4, size // 2)` moves out of the opening
  boundary; `v3`–`v5` and `auto` also enforce no adjacent same-family draw across the cycle
  and across cycle boundaries.
- **Spent ledger (`v1`–`v3` vs `v4`/`v5`/`auto`)**: replaces the raw "last lines"
  block with a ~200-character tail of the previous accepted output plus spent
  content words (5+ chars, excluding stopwords and the topic's title/angle).
  `v4`, `v5`, and `auto` upgrade to Ledger v2 (`spent_words_v2` on 1+ occurrence words,
  `spent_openings`, and `repeats_topic_stem_or_sentence` guard in
  `_validate_output`).
- **Agenda-only LLM options (`v2`/`v5`/`auto`)**: `agenda_llm_options()` in
  `opencohost/core/context/prompt_assembler.py` (re-exported by
  `opencohost/core/engine/llm_engine_agenda.py`) applies `repeat_penalty=1.18`
  and caps local `num_predict` at `110` only when `source` starts with
  `"kira-agenda"` and `v2`, `v5`, or `auto` is active; `chat`, `direct`, and other
  sources are untouched.
- **Known limitation (accepted by design)**: progression turns use a fixed
  110-token answer cap with `repeat_penalty=1.18` and no extra reasoning budget,
  so thinking or heavily quantized models may return short or empty agenda
  lines. Users can turn progression off in the Agenda panel.

## Implementation boundaries

Recommended modules:

| Module | Responsibility |
|---|---|
| `smart_aggregator/session_history.py` | Compact snapshot retrieval helpers only |
| `ui/app_shell.py` | Wiring, timers, engine integration |
| `ui/stream_admin_ui.py` | Agenda controls and state display |
| `ui/smart_aggregator_ui.py` | Prompt builder/sanitizer reuse |
| New `stream_admin/kira_agenda_controller.py` or `smart_aggregator/kira_agenda_controller.py` | Deterministic state machine |

Do not put product orchestration inside `core/llm_engine.py`. The engine should stay generic: process commands, queue work, synthesize speech, report status.

## Testing checklist

- [ ] Raw chat is never persisted.
- [ ] Only approved topics can be queued.
- [ ] Agenda mode does nothing when no topic exists.
- [ ] PTT input outranks agenda continuation.
- [ ] Chat compact context can steer the active topic.
- [ ] Controller does not enqueue while Kira is speaking.
- [ ] Soft stop waits for a safe boundary.
- [ ] Emergency stop drops pending autonomous work.
- [ ] Output sanitizer rejects internal prompt leaks.
- [ ] Repetition detector prevents repeating the last Kira turn.
- [ ] Three consecutive failures pause the mode and notify UI.
- [ ] Suggested future topics require explicit approval.

## Out of scope for MVP

- Vector database.
- Raw chat persistence.
- Fully autonomous endless stream direction.
- Auto-posting to YouTube chat.
- Multi-agent planning.

## MVP implementation order

1. Data model for approved topics and controller state.
2. Deterministic controller with no LLM calls, tested by fake clock/events.
3. UI controls for queue, toggle, soft stop, emergency stop.
4. Prompt builder and sanitizer.
5. Motor IA integration with droppable `kira-agenda` source.
6. Compact snapshot fallback and topic suggestion drafts.
7. End-to-end smoke test with fake LLM/TTS.

## Final product rule

Kira is a co-host, not an unattended streamer. The system should make her feel alive while preserving human control, local-first privacy, and deterministic recovery.
