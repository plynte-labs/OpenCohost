# Windows Installer and Clean-VM Recovery

**Date**: 2026-10-24  
**Status**: Accepted decisions, documented from the completed recovery track  
**Engram mirror**: Pending; the host session-resolution hook blocked the write  
**Feature document**: `odd/tasks/cloud-configuration-startup-recovery-20261023.md`  
**Related**: `packaging/stage_installer_resources.py`, `OpenCohost_UI/src-tauri/custom-installer.nsi`

This ADR records the installer and startup-recovery decisions that produced the supplied Windows candidate. It is a recovery record, not a claim of broad release readiness.

## Context

A clean Windows installation exposed several independent failures: setup could be blocked when the API was healthy but the inference thread was unavailable; first-run storage could land under the install root; shutdown could lose the operator token context; native Python dependencies were not reliably app-local; and NSIS could not map a large solid-compression block. Upgrade preservation also had to retain existing and conflicting user state without destructive cleanup.

## Decisions

| Area | Decision | Why |
|---|---|---|
| Python runtime | Bundle CPython 3.12.4 for the primary application runtime; do not require system Python. Keep manifest-provisioned Python/venv launch paths supported. | A clean-machine install must not depend on a separately installed interpreter. |
| Control plane and inference | Treat healthy API/control-plane availability separately from inference-thread readiness. The setup UI may reach provider/API-key configuration when the engine is unavailable. A cloud API key does not require external Ollama. | Users need a way to configure cloud access even if local inference is not ready. This does not claim that configuration revives a failed engine. |
| User data and legacy state | Keep the configured external data root authoritative. Preserve legacy files at both the runtime root and nested `runtime/opencohost` root; adopt only into an empty destination group. Existing destination groups and divergent conflicts win and remain untouched. SQLite groups are handled as files, without parsing or purging databases. | Upgrade recovery must not destroy active or ambiguous user state. The report's destructive profile/database cleanup suggestion was rejected. |
| Interrupted state adoption | For an owned, interrupted adoption, use a snapshot-local pending ownership record bound to the source, destination, group, and hashes; publish sidecars first and the SQLite main file last. Retry only an exact owned publication; fail closed on divergence. | A partial multi-file SQLite group must be recoverable without mistaking unrelated destination data for our own work. |
| Shutdown authentication | Retain the launched `BackendConfig` context through IPC and shutdown so the operator token remains available for the authenticated shutdown request. Do not weaken the bearer-auth requirement. | Fix context loss rather than making shutdown unauthenticated. |
| Native runtime | Stage one coherent, pinned, signed Microsoft x64 CRT family (10 DLLs) app-locally under the bundled runtime and provisioned executable roots used by Piper, MiniLM, multiprocessing spawn, and isolated Laya. The staging input is the installed VC Redist tree, not a target-machine prerequisite. | Native extensions need the matching runtime beside the Python launch roots; relying on a developer machine's system directories is not portable. A `sitecustomize`/`PYTHONPATH`-only solution is insufficient for isolated `python -I` Laya workers. |
| NSIS compression | Keep zlib, but use non-solid compression (`SetCompressor "zlib"`). | NSIS 3.11 failed mapping the approximately 2.17 GB solid block because of its signed 31-bit mapping limit. Non-solid compression completes without omitting resources; the tradeoff is a potentially larger installer. |
| Staging and provenance | Rebuild the engine payload from current source for offline staging; do not pass a stale `--engine-payload`. Generate and verify the complete resource inventory, compare every staged file's size and SHA-256, and preserve prior resource and installer candidates before replacement. | The installer must correspond to current source and remain auditable/rollbackable. |

## Rejected alternatives and boundaries

- Do not delete profiles, databases, or other state to make an upgrade appear clean; preserve originals and conflicting versions.
- Do not treat a DLL loaded from `System32` as proof that a target machine's missing-runtime/uninstall history caused the startup failure. App-local module-path evidence proves the packaged process loaded its local CRT; it does not establish that broader causal claim.
- Do not rely on `sitecustomize` or `PYTHONPATH` to supply native modules to the isolated `-I` worker.
- Do not silently switch inference providers, relax backend authentication, or claim that setup access restarts a dead inference thread.
- This record does not cover the deferred T7/T8/T9 work, certify every Windows filesystem or power-loss scenario, or establish general production readiness.

## Reproduction

Run staging from `E:\VoiceAI` with the repository's existing `.venv`. The CRT and Laya paths below are recorded build-host candidate inputs; neither is a target-machine runtime requirement. The Laya directory was verified to exist in the current checkout.

```powershell
Set-Location E:\VoiceAI
$env:CARGO_NET_OFFLINE = 'true'
.venv\Scripts\python.exe packaging\stage_installer_resources.py `
  --version 0.5.0-alpha.1 `
  --offline `
  --msvc-runtime-dir 'C:\Program Files\Microsoft Visual Studio\18\Community\VC\Redist\MSVC\14.50.35710\x64\Microsoft.VC145.CRT' `
  --laya-onnx-dir 'E:\VoiceAI\OpenCohost_UI\src-tauri\resources\models\laya\multilingual'
```

Do not add `--engine-payload`; the default builds the engine archive from current source.

Then build NSIS offline from the UI checkout:

```powershell
Set-Location E:\VoiceAI\OpenCohost_UI
$env:CARGO_NET_OFFLINE = 'true'
pnpm tauri build --bundles nsis
```

Before rerunning this workflow, preserve any prior installer and staged resources. After staging, verify the inventory and every file hash; after building, inspect generated NSIS for non-solid zlib and exact resource coverage, then hash the final installer and its preserved copy.

## Evidence and limitations

- Delivered candidate: `E:\VoiceAI\build_artifacts\deliverables\OpenCohost_0.5.0-alpha.1_x64-setup-runtime-recovery-2bbfb2e6.exe` — 1,406,390,108 bytes; SHA-256 `2bbfb2e6244f916bf6075c10e59a9fdb23f598ec103162c31ffd98609a95c8e0`.
- Owner-reported clean-VM validation: NVIDIA API-key setup succeeded and cloud chat answered through `nvidia_nim` without external Ollama. This is the owner's report, not an agent-executed VM test or real cloud API invocation.
- Local packaged-runtime smoke: bundled CPython 3.12.4 imported ONNX Runtime and Piper; multiprocessing spawn imports passed; the parent and spawned process resolved four CRT modules under the bundled runtime. An isolated `python -I -B laya_worker.py` loaded/warmed the packaged ONNX model and reported ready. The fallback-venv isolated Laya worker also warmed, but its remote CRT-module enumeration was inconclusive.
- Final recorded checks: Python installer tests 32/32; combined root Python spot-check 208/208; Rust suite 177/177; focused UI/startup tests 76/76; TypeScript check exit 0. The unchanged whole-Rust formatting check still reports failures in `shutdown.rs`; a known unrelated LogsPanel NBSP assertion also remains. See the feature document for exact scope and chronology.
- No clean-VM reproduction was independently performed in this work session. No separate FAT/ReFS, power-loss, or hostile-ancestor-race proof exists. Optional fallback Laya readiness is not a stronger DLL-path proof. These limits should remain visible when sharing the candidate with testers.
