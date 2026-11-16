# ADR-068: Kira's VRM Avatar on the Phone Page

**Date**: 2026-10-20
**Status**: Accepted 2026-10-20. Implementation: T1 `3a2a80b` (gateway routes), T2 UI submodule `9fb482a` (bundle, committed as a generated file in `e0e9be1`), T3 `e0e9be1` (phone page). T5 (owner phone check) is pending.
**Feature document**: `odd/tasks/remote-vrm-avatar-20261020.md`
**Related**: `docs/adr/ADR-067-remote-voice-gateway.md` (the phone page and gateway this extends), `docs/guides/remote-phone-access.md` (owner checklist)

---

## Context

The phone page served by the remote voice gateway (ADR-067) is voice-only. The owner wants the same visual Kira as the desktop companion when talking from the phone.

The desktop companion already renders the VRM with portable three.js and three-vrm code in `OpenCohost_UI/src/features/avatar-vrm/domain/`. The phone page does not run the React app, and ADR-067 forbids directory mounts, so the avatar needs its own small bundle and explicit routes.

## Decision

### Bundle

- A React-free browser bundle, `opencohost/gateway/static/avatar.js`, is built from `OpenCohost_UI/src/phone-avatar/` with `pnpm build:phone-avatar` (run in `OpenCohost_UI/`, config `vite.phone-avatar.config.ts`). The output is one ES module; `emptyOutDir` is off so the other static files stay.
- It reuses the desktop domain modules `vrmLoader`, `vrmAnimator` and `frameBudget` from `src/features/avatar-vrm/domain/`, plus a small render loop (`stage.ts`), a controller, the event contract and a procedural mouth.
- Phone caps: pixel ratio 1, 30 fps. Rendering pauses while the page is hidden or the avatar is toggled off.
- `avatar.js` is open like `phone.js` because it is code only. The model is protected and the clips are a fixed-name allowlist (see Routes).

### DOM event contract

`phone.js` never loads the avatar. It announces state on `document` as `CustomEvent`s, and `avatar.js` listens for them:

| Event | Detail | Meaning |
|---|---|---|
| `kira:auth` | `{token: string \| null}` | paired (token) or unpaired / auth lost (null) |
| `kira:speech` | `{phase: 'start' \| 'boundary' \| 'end'}` | the phone voice starts, reaches a word boundary, or ends |
| `kira:state` | `{state: 'idle' \| 'thinking' \| 'listening'}` | phone turn state |

Initial auth is dispatched on DOMContentLoaded, so the deferred module is already listening.

### Lip-sync

The phone has no TTS audio stream: ADR-067 uses the phone's own `speechSynthesis` voices, and the PC TTS is not piped to the phone. The avatar therefore cannot read an audio level. The mouth is procedural: a syllable wave runs while the voice speaks, `boundary` events add extra opening when the browser fires them, and `end` closes the mouth.

### Routes

Added to the ADR-067 allowlist:

| Route | Auth | Purpose |
|---|---|---|
| `GET /avatar.js` | none | the avatar bundle (code only); 404 if the file is missing |
| `GET /animations/{clip}.vrma` | none | idle and gesture clips. Fixed allowlist of nine clip names; any other name is 404. Read from `settings.VRMA_DIR` (env `OPENCOHOST_VRMA_DIR`, default `OpenCohost_UI/public/animations`). `Cache-Control: private, max-age=86400` |
| `GET /avatar/model` | bearer device token | the VRM model: always the single file `settings.PHONE_VRM_PATH` (default `OpenCohost_UI/src-tauri/resources/vrm/Opencohost Kira v3.vrm`, env `OPENCOHOST_PHONE_VRM`), read in place. 404 when it is missing or not a `.vrm` regular file; never falls back to `VRM_MODEL_DIR`. ETag, and 304 on a matching `If-None-Match`. `Cache-Control: private, max-age=0, must-revalidate`. No query parameter selects a file |

- The clip route is open, not protected, so the animator's existing paths work unchanged (T1 decision). The request name is checked against the allowlist before any path is built, so it never reaches the filesystem as a name.
- `/avatar/model` is registered on the `protected` router: a missing or wrong token gets 401.
- Every other path still returns 404.

### CSP change

`img-src` gains `blob:` for glTF textures. The header is now `img-src 'self' data: blob:`. Nothing else in the policy changes.

`connect-src` becomes `'self' blob:`. three's `GLTFLoader` decodes embedded textures with `ImageBitmapLoader` on Chrome, which calls `fetch(blobUrl)`; `connect-src 'self'` blocked it and the avatar rendered white (found on a real Android phone). Nothing else in the policy changes.

`/`, `/phone.js`, `/avatar.js` and the clip responses carry the page header set (CSP, `Referrer-Policy`, `nosniff`). The clips override only the cache header. The model response is not a document and carries only ETag and Cache-Control.

## Consequences and risks

- **Which model is served.** Owner decision 2026-10-20: the phone always shows Kira v3 (`Opencohost Kira v3.vrm`, 20,738,200 bytes, about 20.7 MB). The gateway reads it in place from `settings.PHONE_VRM_PATH` (override with `OPENCOHOST_PHONE_VRM`); the file is never copied or modified. The desktop companion's own model selection is unchanged.
- **First load is large.** The first load downloads the whole model, about 20 MB for the Kira v3 file. The phone fetch uses `cache: 'no-cache'`, so the browser sends `If-None-Match` on every load and reuses its stored copy when the server answers 304. Later loads therefore skip the body download but still make one round trip. The stored copy can be evicted by browser storage pressure. This is not yet checked in a real browser (T5).
- **Bundle size and generated file.** `avatar.js` is 973,104 bytes (about 221 KB gzipped) and is committed as a generated file. After changing `src/phone-avatar/` or the domain modules it imports, rebuild with `pnpm build:phone-avatar` in `OpenCohost_UI/` and commit the regenerated file with the change.
- **WebGL and iOS memory.** Rendering needs WebGL and a parse of a ~20 MB glTF. Memory limits on iOS Safari or low-end Android can fail the load or lose the GL context. A failed load hides the canvas (`unavailable`) and the voice flow keeps working.
- **`onboundary` is unreliable on iOS.** iOS Safari often does not fire word-boundary events. The mouth then runs on the syllable wave alone, which is less precise.
- **Exposure.** The model is served only on the protected route and leaves the PC only through the Access-protected tunnel. `avatar.js` and the clips are open, so they depend on Cloudflare Access in front of the hostname, as the rest of the gateway does (ADR-067, outer layer).
- **Licences.** Third-party notices are in `opencohost/gateway/static/avatar.LICENSES.txt` (three.js and @pixiv/three-vrm, both MIT; not served) and `OpenCohost_UI/public/animations/THIRD_PARTY_LICENSES.md`.
- **Not automatically browser-tested.** Unit tests cover the event contract, the controller, the model fetch and the mouth module. WebGL rendering, parsing of the real model, `speechSynthesis` boundary events and the iOS unlock are covered only by the manual check (T5).

### Follow-ups

- A phone-optimised, lighter VRM, to cut the download and the GPU cost.
- Better visemes than the syllable wave, if a browser audio path becomes available.
