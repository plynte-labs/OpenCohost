# Specification: Modular VRM 3D Avatar & LipSync Integration

- **Target Systems**: OpenCohost Backend (FastAPI/Python) & OpenCohost UI (Tauri / React / Three.js)
- **Status**: Ready for Codex Implementation
- **Architecture Pattern**: Decoupled WebGL Overlay + Silent WebAudio LipSync Analyser

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
  - `GET /api/avatar/vrm/audio/last`: Returns the binary bytes of the most recently synthesized speech chunk from `TEMP_DIR` for spectral analysis.
- **Engine Seam**: In `opencohost/core/engine/llm_engine_speech.py`, when a chunk is enqueued for playback, record the path of the active chunk in an atomic variable/property `last_speech_chunk_path` accessible by the API host.

### WU2: Frontend VRM Domain Architecture & Three.js Loader
**Target Directory**: `OpenCohost_UI/src/features/avatar-vrm/`
- **Dependencies**: `three`, `@types/three`, `@pixiv/three-vrm`.
- **Modules**:
  - `domain/vrmLoader.ts`:
    - Wraps `GLTFLoader` with `VRMLoaderPlugin`.
    - Handles VRM 0.x and VRM 1.0 schema differences.
    - Normalizes blendshape targets:
      - VRM 0.x: `vrm.blendShapeProxy.setValue(VRMSchema.BlendShapePresetName.A, weight)`
      - VRM 1.0: `vrm.expressionManager.setValue('aa', weight)`
    - Centers model geometry and positions camera at portrait framing (bust/headshot).
  - `domain/vrmAnimator.ts`:
    - **Delta-Time Clamp**: Limit delta time on every tick (`dt = Math.min(clock.getDelta(), 0.05)`) to prevent SpringBone physics explosion when frame drops occur in OBS.
    - **Procedural Breathing**: Subtle harmonic oscillation (sine wave) applied to spine/chest bone rotation.
    - **Procedural Blinking**: Timer-driven blink (`blink` expression ramps 0 -> 1 -> 0 over 150ms every 3 to 6 seconds).

### WU3: Silent Analyser LipSync Pipeline
**Target File**: `OpenCohost_UI/src/features/avatar-vrm/domain/vrmLipSync.ts`
- **Audio Decoding**:
  - When `useAvatarLiveState` signals `speaking === true`, fetch `/api/avatar/vrm/audio/last`.
  - Instantiate singleton `AudioContext`.
  - Decode binary array buffer via `audioContext.decodeAudioData(bytes)`.
- **Silent Node Graph**:
  ```
  AudioBufferSourceNode ──> AnalyserNode ──> GainNode (gain=0.0) ──> AudioContext.destination
  ```
- **Formant & RMS Extraction**:
  - `analyser.getByteFrequencyData(dataArray)`: Compute average RMS power.
  - Apply noise floor threshold (ignore background noise < 0.05).
  - Map energy to mouth blendshape (`aa` / `oh`) using exponential smoothing:
    $$\text{mouthOpening} = \text{lerp}(\text{current}, \text{targetEnergy}, 0.3)$$
- **Cleanup**: Disconnect nodes and reset mouth expression to 0.0 immediately upon `speaking === false`.

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
- **Route**: Expose `/overlay/vrm` in the UI router so OBS Browser Source can point to `http://localhost:5173/overlay/vrm` (or production port).

---

## 4. Failure Mode Checklist for Codex

| Failure Mode | Root Cause | Mandatory Guardrail in Code |
| :--- | :--- | :--- |
| **Double Audio / Echo** | Browser source plays audio while Python plays through `pygame.mixer` | Hardcode `gainNode.gain.value = 0.0` in `vrmLipSync.ts`. Audio is processed, never emitted to stream from browser. |
| **SpringBone Explosion** | Framerate dips in OBS cause large `dt` spikes in physics simulation | Enforce `const dt = Math.min(clock.getDelta(), 0.05);` before calling `vrm.update(dt)`. |
| **Black Screen of Death** | GPU overload by game triggers WebGL context loss | Implement explicit `webglcontextlost` and `webglcontextrestored` lifecycle listeners. |
| **Mouth Stuck Open** | Interrupted turn stops audio fetch without zeroing blendshapes | Enforce `vrm.expressionManager.setValue('aa', 0)` on `speaking_end` or audio buffer end. |
| **VRM Version Mismatch** | Models from VRoid or Booth using either 0.x or 1.0 format | Use `@pixiv/three-vrm` 2.x which auto-detects version and exposes unified expression interfaces. |

---

## 5. Verification & Acceptance Criteria

1. **Standalone Load**: Loading `/overlay/vrm` displays the active 3D model with transparent background, steady 60 FPS, subtle chest breathing, and natural blinking.
2. **LipSync Accuracy**: When Kira speaks a TTS turn, mouth opens in real-time correlation with voice syllables; zero audible echo from the browser source.
3. **Speech Interruption**: Pressing PTT or invoking emergency stop immediately cuts mouth movement back to rest position.
4. **Stress Resistance**: Simulating a 5 FPS drop or tab defocus does not invert or tangle model hair/clothing physics.
