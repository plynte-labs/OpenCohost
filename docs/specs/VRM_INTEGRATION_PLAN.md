# Specification: Modular VRM 3D Avatar & LipSync Integration

## Implemented quick path (2026-09-13)

1. Put self-contained `.vrm` files in `~/Downloads/modelos vrm`, or set
   `OPENCOHOST_VRM_MODEL_DIR` before launching the backend.
2. Run the existing backend and serve the UI. Point OBS Browser Source to
   `/overlay/vrm` on the UI server; optionally select `?model=filename.vrm`.
3. Open OBS **Interact**, confirm the loopback backend origin (default
   `http://127.0.0.1:8765`), select the model, and enter the operator token from
   your existing local operator setup. Click **Enable silent analysis**.
   The password is held only in runtime memory and sent only as an Authorization
   header to a validated loopback origin. Never put a token in a URL.
4. Controls disappear after enabling analysis. Reload/visibility suspension
   requires Interact setup again. Missing files, auth errors and WebGL failures
   reopen visible diagnostics; do not capture setup controls in the final scene.

### Boundaries and verification

- `/overlay/vrm` is isolated before normal shell/CSS imports. Normal routes keep
  the original shell bootstrap. The prepaint guard only suppresses the opaque
  splash on this exact route and preserves existing theme selection.
- Browser audio always passes through a hardcoded zero-gain node. Python remains
  the audible source. This is amplitude-driven lip sync, not phoneme alignment.
- Interruption latency is polling (100 ms) plus transport/browser scheduling,
  **not zero latency**. A 1.5 s watchdog clears the mouth if a request hangs.
  Suspended/hidden browser sources stop analysis rather than replay stale audio.
- Audio copies over 8 MiB or unreadable chunks degrade to idle animation without
  preventing Python speech. Completed chunk bytes are cleared, not archived.
- VRM external resource URLs are rejected; use self-contained GLB/VRM files.
  WebGL context loss cancels rendering and invalidates pending loads; restoration
  rebuilds the renderer and reloads the model. Late loads are disposed.
- Automated tests exercise fake WebAudio, consumer playback, clocks and context
  lifecycle. Actual VRM 0.x/1.0 model rendering, OBS transparency, gesture policy,
  hair physics and audible echo must still be checked on the target machine.
- No normal dev/prebuild hooks are needed for checks: use `pnpm exec vitest run
  src/features/avatar-vrm`, `pnpm exec tsc --noEmit`, and `pnpm exec vite build`.

The WU descriptions below retain the original implementation boundaries, with
the producer-path, legacy expression API and spectral-RMS mistakes corrected.

- **Target Systems**: OpenCohost Backend (FastAPI/Python) & OpenCohost UI (Tauri / React / Three.js)
- **Status**: WU1–WU4 implemented, verified with dual blind adversarial review (Judgment Day: APPROVED), automated suites (179 passed, 1 skipped). Live OBS runtime validation is deferred by user decision.
- **Architecture Pattern**: Decoupled WebGL Overlay + Silent WebAudio LipSync Analyser (100 ms nominal polling interval)

---

## 1. Problem Statement & Architectural Context

OpenCohost currently supports 2D static avatar states (`IDLE`, `THINKING`, `SPEAKING`, `SPEAKING_ALT`) pushed to OBS via `obs-websocket` image source swapping. While stable, this lacks 3D presence, real-time mouth movement (LipSync), and procedural life.

### Design Principles (CONCEPTS > CODE)
1. **Zero 3D Bloat in Python**: The Python backend must never import OpenGL, Pygame 3D, or heavy graphics libraries. Rendering belongs exclusively to the GPU-accelerated browser engine (OBS Browser Source / Webview).
2. **Audio Pipeline Non-Interference**: Python's `pygame.mixer` continues to own local desktop playback, ducking, interruptions (`interrupt_speaking`), and speech queues. The web overlay must never duplicate or take over playback (avoids double-audio echo).
3. **Silent Analyser Pattern**: The browser decodes audio bytes via WebAudio `AudioContext` and feeds an `AnalyserNode` solely to compute real-time spectral/RMS amplitude for blendshapes, keeping `gainNode.gain.value = 0`.
4. **Strict Domain Isolation**: The new 3D system lives in an isolated feature domain (`src/features/avatar-vrm/`). Existing 2D avatar stores and image endpoints remain completely untouched as fallbacks.

---

## 2. Component Topology

```
┌─────────────────────────────────────────────────────────────┐
│                       PYTHON BACKEND                        │
│                                                             │
│  [Speech Synthesis] ──> (Temp WAV/MP3) ──> [pygame.mixer]   │
│           │                                       │         │
│           ▼                                   Local Audio   │
│  [avatar_vrm router]                                        │
│     ├── GET /api/avatar/vrm/model (Binary VRM stream)       │
│     └── GET /api/avatar/vrm/audio/last (Latest speech bytes)│
└──────────────────────────┬──────────────────────────────────┘
                           │ HTTP / Localhost
                           ▼
┌─────────────────────────────────────────────────────────────┐
│                 OBS BROWSER SOURCE / TAURI                  │
│               (Route: /overlay/vrm - Transparent)           │
│                                                             │
│  ┌───────────────┐     ┌──────────────────────────────────┐ │
│  │ WebAudio      │     │ Three.js + @pixiv/three-vrm       │ │
│  │ AnalyserNode  │────>│   - VrmLoader (0.x & 1.0 support)│ │
│  │ (Gain = 0.0)  │RMS  │   - VrmLipSync (aa/oh blendshape)│ │
│  └───────────────┘     │   - VrmAnimator (dt-clamped loop)│ │
│                        └──────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────┘
```

---

## 3. Work Breakdown Structure (Codex Implementation Units)

### WU1: Backend VRM File Serving & Audio Byte Seam
**Target Directory**: `opencohost/api/routers/avatar_vrm.py`
- **Config Storage**: Default model directory path configured in settings or storage (e.g. `C:\Users\tavo_\Downloads\modelos vrm`).
- **Endpoints**:
  - `GET /api/avatar/vrm/list`: Scans configured directory and returns list of `.vrm` files with basic metadata (`filename`, `size_mb`, `modified_at`).
  - `GET /api/avatar/vrm/model?name=<filename>`: Streams the `.vrm` file with MIME `model/gltf-binary` or `application/octet-stream`. Includes ETag / Cache-Control headers.
  - `GET /api/avatar/vrm/audio/last`: Returns a bounded immutable copy of the current playback chunk, pinned with `?sequence=N`. Requires an operator bearer token, never an agent token. Inactive audio returns 404; replaced sequence returns 409; responses are no-store.
- **Engine Seam**: In `opencohost/core/engine/llm_engine_speech.py`, prepare at most 8 MiB before playback, publish an immutable snapshot immediately after `mixer.music.play()`, and clear bytes at chunk completion. The single serialized consumer owns publication. `GET /api/avatar/vrm/audio/state` returns `{sequence, active, elapsed_ms}` without text or paths. Cancellation also gates `active` on the existing speaking flag. Producer enqueue is NOT playback: synthesis can run several chunks ahead and cleanup deletes paths.

### WU2: Frontend VRM Domain Architecture & Three.js Loader
**Target Directory**: `OpenCohost_UI/src/features/avatar-vrm/`
- **Dependencies**: `three`, `@types/three`, `@pixiv/three-vrm`.
- **Modules**:
  - `domain/vrmLoader.ts`:
    - Wraps `GLTFLoader` with `VRMLoaderPlugin`.
    - Handles VRM 0.x and VRM 1.0 schema differences.
    - Normalizes blendshape targets:
      - VRM 0.x: modern `VRMLoaderPlugin` converts expressions to unified `expressionManager`; call `VRMUtils.rotateVRM0` for orientation.
      - VRM 1.0: `vrm.expressionManager.setValue('aa', weight)`
    - Centers model geometry and positions camera at portrait framing (bust/headshot).
  - `domain/vrmAnimator.ts`:
    - **Delta-Time Clamp**: Limit delta time on every tick (`dt = Math.min(clock.getDelta(), 0.05)`) to prevent SpringBone physics explosion when frame drops occur in OBS.
    - **Procedural Breathing**: Subtle harmonic oscillation (sine wave) applied to spine/chest bone rotation.
    - **Procedural Blinking**: Timer-driven blink (`blink` expression ramps 0 -> 1 -> 0 over 150ms every 3 to 6 seconds).

### WU3: Silent Analyser LipSync Pipeline
**Target File**: `OpenCohost_UI/src/features/avatar-vrm/domain/vrmLipSync.ts`
- **Audio Decoding**:
  - Poll authoritative `/audio/state` every 100 ms (serialized requests), independently of the coarse shell speaking store. Fetch each new sequence once, then recheck state after decoding and offset playback by current `elapsed_ms`.
  - Instantiate one disposable `AudioContext` per enabled overlay session, resumed from an explicit user gesture.
  - Decode binary array buffer via `audioContext.decodeAudioData(bytes)`.
- **Silent Node Graph**:
  ```
  AudioBufferSourceNode ──> AnalyserNode ──> GainNode (gain=0.0) ──> AudioContext.destination
  ```
- **Formant & RMS Extraction**:
  - `analyser.getFloatTimeDomainData(samples)`: compute `sqrt(mean(sample²))` time-domain RMS. Frequency-bin averages are not RMS.
  - Apply noise floor threshold (ignore background noise < 0.05).
  - Map energy to mouth blendshape (`aa` / `oh`) using exponential smoothing:
    $$\text{mouthOpening} = \text{lerp}(\text{current}, \text{targetEnergy}, 0.3)$$
- **Cleanup**: Disconnect nodes and reset mouth expression to 0.0 when authoritative chunk state becomes inactive, the buffer ends, or the session is disposed. Interruption detection is subject to polling and browser scheduling latency.

### WU4: Canvas Component & Transparent OBS Route
**Target Files**:
- `OpenCohost_UI/src/features/avatar-vrm/components/VrmStage.tsx`
- `OpenCohost_UI/src/features/avatar-vrm/routes/VrmOverlayPage.tsx`
- **Canvas Properties**:
  - WebGLRenderer with `alpha: true, antialias: true, premultipliedAlpha: false`.
  - Clear color `0x000000, 0.0` (pure transparent background for OBS overlay).
- **WebGL Context Loss Handler**:
  - Attach `webglcontextlost` event listener (`event.preventDefault()`).
  - Attach `webglcontextrestored` event listener to re-initialize scene and reload active VRM without crashing the browser view.
- **Route**: Expose `/overlay/vrm` through the isolated UI entry so OBS Browser Source can point to `http://127.0.0.1:1420/overlay/vrm` while the UI HTTP server is running. Packaged Tauri assets alone do not provide that HTTP server.

---

## 4. Failure Mode Checklist for Codex

| Failure Mode | Root Cause | Mandatory Guardrail in Code |
| :--- | :--- | :--- |
| **Double Audio / Echo** | Browser source plays audio while Python plays through `pygame.mixer` | Hardcode `gainNode.gain.value = 0.0` in `vrmLipSync.ts`. Audio is processed, never emitted to stream from browser. |
| **SpringBone Explosion** | Framerate dips in OBS cause large `dt` spikes in physics simulation | Enforce `const dt = Math.min(clock.getDelta(), 0.05);` before calling `vrm.update(dt)`. |
| **Black Screen of Death** | GPU overload by game triggers WebGL context loss | Implement explicit `webglcontextlost` and `webglcontextrestored` lifecycle listeners. |
| **Mouth Stuck Open** | Interrupted turn stops audio fetch without zeroing blendshapes | Enforce `vrm.expressionManager.setValue('aa', 0)` on `speaking_end` or audio buffer end. |
| **VRM Version Mismatch** | Models from VRoid or Booth using either 0.x or 1.0 format | Use exact `@pixiv/three-vrm` 3.5.5 with Three.js 0.186.0 and matching types; unified expression interfaces cover both versions. |
| **Transient 404/409 Disruption** | Chunk finishes playing or replaced mid-fetch | Return `null` from `vrmApi.audio`, defer `lastSequence` update until byte retrieval, and retry on next tick without calling `onError`. |
| **Corrupt Audio Chunk Crash** | Malformed audio bytes throw in `decodeAudioData` | Wrap `decodeAudioData` in try/catch and degrade to idle animation without terminating overlay session. |
| **Polling Runaway on Fatal Auth Error** | 401 / bad token keeps scheduling fetch every 100ms | Enforce `this.stopped = true` before calling `onError`, halting polling loop immediately. |
| **Premature Sequence Consumption** | Post-decode abort causes skipped chunk | Defer `this.lastSequence = state.sequence` until `source.start()` begins analysis or chunk is explicitly expired. |
| **Async Decode Error Cross-Generation Leak** | Stale decode failure from superseded generation clears active source of new session | Guard `catch` block with `if (!this.stopped && generation === this.generation)`. |


---

## 5. Verification & Acceptance Criteria

1. **Standalone Load**: Loading `/overlay/vrm` displays the active 3D model with transparent background, steady 60 FPS, subtle chest breathing, and natural blinking.
2. **LipSync Accuracy**: When Kira speaks a TTS turn, mouth opens in real-time correlation with voice syllables; zero audible echo from the browser source.
3. **Speech Interruption**: Pressing PTT or invoking emergency stop immediately cuts mouth movement back to rest position.
4. **Stress Resistance**: Simulating a 5 FPS drop or tab defocus does not invert or tangle model hair/clothing physics.
