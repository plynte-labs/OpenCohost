# Track: Product UI Refactor — Kira-Centered Layout + Avatar/OBS Module

## Problem Statement

VoiceAI already has powerful features, but the interface still feels like a technical control room rather than a concrete product centered on Kira. The current UI exposes many controls, logs, and Stream Admin operations at similar visual weight, which makes the experience feel loaded and makes it hard to tell what the user should do first.

The next refactor must preserve behavior while reorganizing the product around the core mental model:

> The streamer operates Kira, a co-host agent, while advanced configuration stays organized and secondary.

## Product Direction

Use a stable two-zone layout:

```text
┌────────────────────────────┬──────────────────────────────────────┐
│ Kira / VoiceAI             │ Classified panels                     │
│ - avatar placeholder       │ - Agent / Brain                       │
│ - live state               │ - Voice / Input                       │
│ - current action           │ - Stream                              │
│ - last response            │ - Avatar / OBS                        │
│ - quick actions            │ - System                              │
│                            │ - Logs                                │
└────────────────────────────┴──────────────────────────────────────┘
```

## Scope

- Inventory all current UI controls before moving them.
- Reclassify existing controls into product categories.
- Preserve all current behavior, callbacks, config, and tests.
- Keep Stream Admin available, but stop making it compete with the primary Kira experience.
- Add an Avatar/OBS configuration module as a real product area, not loose buttons.
- Support future avatar modes without coupling them to Tkinter internals:
  - none / placeholder
  - 2D image
  - Live2D or 2D adapter later
  - 3D adapter later
  - OBS overlay/browser source later
- Hide advanced logs by default while keeping them accessible.

## Out of Scope

- Rewriting the app in React/Tauri.
- Removing existing features.
- Changing LLM, TTS, Stream Admin, Smart Aggregator, or storage backend behavior.
- Implementing a full 3D engine in this track.
- Changing OAuth or YouTube API semantics.

## New Product Categories

| Category | Purpose | Current controls that likely belong here |
|---|---|---|
| Kira / VoiceAI left panel | Product identity and live operational state | Kira response, state pills, last action, quick voice/chat controls, avatar placeholder |
| Agent / Brain | What Kira is and remembers | model selection, Ollama status, profile/persona, memory clear/status, conversation context |
| Voice / Input | How the streamer talks to Kira | TTS mode, reference voice, record voice, load WAV, microphone device, LiveAudio, PTT |
| Stream | What Kira sees from the live stream | YouTube chat, Smart Aggregator, Stream Admin, OAuth, metadata, moderation, stream actions |
| Avatar / OBS | How Kira appears on stream | avatar mode, asset path, current visual state, OBS overlay config |
| System | Local infrastructure | storage/cache paths, service status, diagnostics |
| Logs | Advanced/debug info | general log, Kira actions, YT chat log, Stream Admin log |

## Functional Requirements

1. **Inventory first**
   - Every current UI control must be listed with file/class, behavior, callback/state, proposed category, and preserve/remove decision.

2. **Behavior-preserving extraction**
   - The first implementation phase must move or wrap layout only after tests prove existing callbacks still fire.

3. **Kira left panel**
   - Must show Kira/VoiceAI identity, current state, last response, chat/voice readiness, and an avatar placeholder.
   - Must not expose deep configuration as the dominant visual element.

4. **Stream Admin placement**
   - Full Stream Admin moves under the `Stream` product category.
   - The left Kira panel may show only a small stream summary: chat connected, current topic, pending action.

5. **Agent / Brain panel**
   - Must group model, profile/persona, memory, and context controls.
   - Model selection alone is not enough; model + profile + memory together define Kira's brain.

6. **Voice / Input panel**
   - Must group TTS, voice reference recording/loading, microphone, LiveAudio, and PTT.

7. **Avatar / OBS panel**
   - Must introduce a simple image-state avatar MVP before any Live2D/VRM/OBS WebSocket work.
   - The user must be able to choose/change images for avatar states from the UI; do not hardcode local asset paths such as `Downloads`.
   - Must support state images for idle, listening, thinking, speaking, optional alternate speaking, sleeping, angry, and error/fallback.
   - Avatar runtime should expose a small API independent of the UI layer, e.g. `set_state()`, `get_state()`, `set_speech_text()`, and `subscribe()`.
   - OBS output beyond manual capture/placeholder remains future work unless implemented as a small optional overlay window.

8. **No phantom options**
   - Disabled/future controls must clearly say `Próximamente` or be hidden behind a feature flag.
   - A visible option must either work or clearly communicate why it is unavailable.

## Non-Functional Requirements

- Preserve existing manual workflows.
- Keep UI responsive; no blocking calls in visual updates.
- Use progressive disclosure: primary state first, advanced configuration second, logs last.
- Maintain tests for extracted panels and callback wiring.
- Avoid circular dependencies between UI panels and runtime modules.

## Acceptance Criteria

- [ ] `inventory.md` lists all current UI controls and maps them to the new categories.
- [ ] No existing control is removed without an explicit preserve/remove decision.
- [ ] Kira left panel exists and can show placeholder avatar, state, latest response, and quick actions.
- [ ] Right-side product panels contain Agent/Brain, Voice/Input, Stream, Avatar/OBS, System, and Logs.
- [ ] Stream Admin remains fully functional inside the Stream category.
- [ ] Model, profile, and memory controls are grouped under Agent/Brain.
- [ ] Voice recording/loading, TTS, LiveAudio, mic, and PTT are grouped under Voice/Input.
- [ ] Avatar/OBS module has config structure and UI placeholder without breaking current app startup.
- [ ] Avatar/OBS MVP lets the user assign state images through the UI and persists that mapping.
- [ ] Advanced logs are hidden by default and still accessible.
- [ ] Existing relevant tests pass.

## Risks

| Risk | Impact | Mitigation |
|---|---|---|
| Breaking callbacks during layout movement | High | Inventory callbacks first; add tests before moving controls |
| Phantom avatar controls | High | Mark future adapters clearly; only expose working placeholder/config now |
| Stream Admin regression | High | Keep StreamAdminUI module intact initially; move container only |
| UI state desync | Medium | Route state through `UIState` and existing panel APIs |
| Refactor too large | Medium | Implement by product category, one work unit at a time |

## Review Strategy

This track should be implemented in reviewable slices. The first slice is documentation/inventory only. Later slices should move layout without behavior change, then introduce Avatar/OBS configuration separately.
