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
