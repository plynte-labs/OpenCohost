# OpenCohost Windows v1 Installer Plan

## 1. Outcome and current-state gap

### Outcome

Ship one per-user Tauri NSIS installer for Windows. NSIS installs only the Tauri application in a user-selected application directory. On first launch, Tauri asks for a separate data root, records the runtime handoff, and provisions a private, versioned Python backend with a pinned private `uv.exe`. Optional Piper support is installed by the same runtime component manager, not by NSIS.

The installed product must launch from a runtime manifest. Users must never install Python or Conda, edit `backend.config.json`, modify `PATH`, or accept OpenCohost-managed Ollama/model storage.

### Verified current state

- `OpenCohost_UI/src-tauri/tauri.conf.json` builds the Tauri NSIS shell but bundles only `backend.config.default.json` as `backend.config.json`.
- `OpenCohost_UI/src-tauri/src/backend.rs` launches `python -m uvicorn` from that configuration. It has development/source-tree fallback logic, but no installed-runtime discovery or provisioning lifecycle.
- The observed installed shell points at `E:\Miniconda\envs\flux_env\python.exe` and `E:\VoiceAI`; it is therefore not a distributable standalone product.
- `packaging/launcher.py` and `packaging/launcher.spec` implement an older `uv`/PyInstaller bootstrap flow, then launch the frozen CustomTkinter entry point. That flow is legacy and is not the v1 composition root.
- `.github/workflows/release.yml` publishes the legacy launcher/source package, does not compose the Tauri product, and does not explicitly check out the Tauri submodule.
- `pyproject.toml` mixes core backend, legacy UI, API, and optional TTS dependencies. The old launcher does not install from `uv.lock`.
- Piper voices are discovered from project-relative/cache-relative paths. There is no release-owned voice license/hash manifest or component lifecycle.

The primary gap is architectural, not an NSIS switch: the product lacks a stable installed-runtime contract between NSIS, Tauri, the Python engine, and optional components.

## 2. Scope and non-goals

### Windows v1 scope

- One Tauri NSIS installer, per-user by default.
- User-selectable application directory in NSIS and independent data/runtime root in Tauri first-run.
- Writable local fixed drives, including a data root on another local disk.
- Online first-run provisioning of a private Python runtime and locked core engine.
- Optional Piper component, installable at setup time or later from Settings.
- EN/ES voice payloads only after an exact license and hash manifest is approved.
- Repair, engine update with last-known-good rollback, and selective uninstall cleanup.
- Offline backend startup after successful provisioning; local offline speech only when Piper and its approved voices are installed.

### Non-goals

- The frozen `opencohost/ui/` CustomTkinter application and `packaging/launcher.py` as release entry points.
- Shared/system Python, Conda, Python registration, or `PATH` modification.
- Qwen3-TTS, `qwen_tts`, Torch/Transformers heavy TTS, Hugging Face TTS caches, or their model payloads.
- Installing, upgrading, configuring, or relocating Ollama or its models. Ollama remains an external prerequisite with user-owned storage.
- Bundling LLM model files. Qwen Ollama LLM tags remain valid user configuration and must not be rejected by a broad `qwen` string check.
- Network shares, UNC paths, mapped network drives, or removable media as the application or data root.
- Long network downloads inside NSIS.

## 3. Target architecture and path layout

### Ownership boundaries

1. **NSIS owns application installation:** install/update the Tauri shell and immutable bootstrap resources, validate only the application path, and invoke the application. It does not select the data root or install runtime components.
2. **The Tauri runtime manager owns first-run and provisioning:** select/validate the data root, write the minimal HKCU handoff, download and verify engine/component payloads, run pinned `uv`, expose progress/retry/cancel to the UI, and update runtime state atomically.
3. **The runtime manifest owns launch:** `backend.rs` reads one authoritative manifest and launches the active engine. It does not search source trees or require editable configuration in installed mode.
4. **The Python engine owns application data semantics:** it receives explicit data/config/cache paths from Tauri. It must not infer writable storage from `sys.frozen` or the package install directory.
5. **The component manager owns Piper:** the same transactional service supports initial install, later install/removal, repair, and updates.

### Concrete layout

Default locations are shown. NSIS selects only `<AppDir>`; Tauri first-run selects `<DataRoot>` and may place it on another writable local fixed drive.

```text
<AppDir> = %LOCALAPPDATA%\Programs\OpenCohost
  OpenCohost.exe
  uninstall.exe
  resources\tools\uv\<uv-version>\uv.exe
  resources\bootstrap\...                 # small, immutable bootstrap metadata

<DataRoot> = %LOCALAPPDATA%\OpenCohost\Data
  state\runtime-manifest.json              # authoritative state
  state\runtime-manifest.previous.json     # recovery copy
  state\operation.lock
  downloads\                               # resumable temporary downloads
  staging\                                 # never launched
  python\                                  # UV_PYTHON_INSTALL_DIR
  cache\uv\                                # UV_CACHE_DIR; same filesystem as env
  engine\releases\<engine-version>\
    project\                               # immutable extracted engine payload
    generations\<generation-id>\venv\     # UV_PROJECT_ENVIRONMENT
  components\piper\<component-version>\
    voices\<voice-id>\model.onnx
    voices\<voice-id>\model.onnx.json
    licenses\...
  user\config\
  user\logs\
  user\state\
```

For every `uv` operation, set these explicitly and do not inherit global equivalents:

```text
UV_PYTHON_INSTALL_DIR=<DataRoot>\python
UV_PROJECT_ENVIRONMENT=<DataRoot>\engine\releases\<engine-version>\generations\<generation-id>\venv
UV_CACHE_DIR=<DataRoot>\cache\uv
```

The environment and uv cache stay on the same filesystem so uv can use its normal linking behavior reliably. NSIS and the provisioner must reject any selected root whose Windows drive type is not `DRIVE_FIXED`, then perform an actual write/rename/delete probe before acceptance. Spaces and Unicode are supported; no path is passed through shell string concatenation.

## 4. Installer-to-app handoff and runtime manifest

### Minimal Tauri-owned HKCU handoff

Use `HKCU\Software\OpenCohost\Runtime`:

| Value | Type | Purpose |
| --- | --- | --- |
| `DataRoot` | `REG_SZ` | Pointer from the installed shell to the selected data root. |
| `InstallId` | `REG_SZ` | Stable per-install identifier used for ownership and diagnostics. |
| `RuntimeManagerVersion` | `REG_SZ` | Tauri runtime-manager version that last wrote the handoff. |

If this handoff does not exist, the release shell stays alive in an unconfigured state and renders the first-run experience. Tauri validates the chosen root, generates `InstallId`, writes the handoff, and creates the initial manifest. Piper and other component choices live only in the manifest, so an application update cannot silently reinstall a component the user removed. The fixed manifest location is `<DataRoot>\state\runtime-manifest.json`; no second registry path is needed.

### Runtime manifest contract

Use a versioned JSON schema. Required logical fields:

```json
{
  "schema_version": 1,
  "install_id": "uuid",
  "product_version": "x.y.z",
  "data_root": "absolute local fixed-drive path",
  "revision": 1,
  "state": "unprovisioned|provisioning|ready|repairing|updating|failed",
  "operation": {
    "id": "uuid-or-null",
    "kind": "first_install|repair|update|component_change|null",
    "phase": "download|verify|stage|sync|health_check|activate|null",
    "last_error_code": "stable-code-or-null",
    "retryable": false
  },
  "engine": {
    "active_version": "x.y.z-or-null",
    "previous_version": "x.y.z-or-null",
    "pending_version": "x.y.z-or-null",
    "active_generation": "id-or-null",
    "previous_generation": "id-or-null",
    "project_dir": "absolute path-or-null",
    "python_executable": "absolute path-or-null",
    "app_module": "opencohost.api.main:app",
    "preferred_port": 8765,
    "fallback_port": 8770,
    "lock_sha256": "hex-or-null",
    "payload_sha256": "hex-or-null"
  },
  "tooling": {
    "uv_version": "pinned-version",
    "python_version": "pinned-version"
  },
  "components": {
    "piper": {
      "requested": false,
      "state": "absent|installing|installed|removing|failed",
      "package_version": "version-or-null",
      "voices_manifest_sha256": "hex-or-null",
      "voices": []
    }
  }
}
```

Each installed voice record contains `id`, `language`, model/config relative paths, individual SHA-256 values, upstream source, license identifier, and bundled notice path. Absolute paths are derived beneath the declared component root and must pass containment checks.

### Manifest lifecycle and launch rules

- Validate schema, install identity, fixed-drive roots, path containment, file hashes, and executable existence before launch.
- Hold a single-writer operation lock for provision/update/component changes.
- Write a new manifest to a sibling temporary file, flush it, then atomically replace the current file; preserve the last readable manifest as the recovery copy.
- Never point `active_version` or `active_generation` at staging. Activate only after locked sync and a backend health check succeed.
- `backend.rs` launches only `engine.python_executable -m uvicorn engine.app_module` with the declared working directory and ports. It passes explicit OpenCohost data/config/component paths in the child environment.
- Development builds may retain an explicit development-only configuration path. Installed builds fail with an actionable repair state instead of walking upward to find a source checkout.
- Manifests contain no credentials and do not claim ownership of external Ollama paths or models.

## 5. Provisioning and component state machine

All downloads use HTTPS, a release manifest pinned by version, content hashes, bounded retries with backoff, resumable temporary files where supported, progress events, and cancellation. Cancellation is honored before activation; cleanup returns to the previous stable state.

| Operation | Transaction |
| --- | --- |
| **First-run configuration** | The shell starts without a backend, asks for a writable local fixed-drive data root, performs drive/write/rename/containment checks, writes the HKCU handoff, records the initial component choices, and only then starts provisioning. Cancel leaves the shell unconfigured and retryable. |
| **First install** | `unprovisioned -> provisioning(download -> verify -> stage -> uv sync -> health_check -> activate) -> ready`. Create the first engine generation and run `uv sync --locked --no-editable` for the core dependency group. If canceled or failed, remove/quarantine staging and remain `unprovisioned`; the shell offers Retry and Diagnostics. |
| **Install Piper later** | From `ready`, set Piper to `installing`, download the separately versioned package/voice manifest, verify licenses and hashes, build a successor engine generation with the Piper extra, stage voice assets, health-check Piper discovery, then atomically switch `active_generation` and set `installed`. Failure deletes/quarantines only the staged generation and leaves the core generation active. |
| **Remove Piper** | Set `removing`, stop new TTS work, build and health-check a successor core-only generation, then atomically switch `active_generation` and set Piper `absent`. Remove the retired Piper generation and managed voice assets only after acceptance. Preserve user-owned exports; remove only manifest-owned paths. |
| **Repair** | Re-verify the active engine, Python, lock, component hashes, and manifest-owned files. Redownload or resync only corrupt/missing owned artifacts. Repair never deletes `user\` data and never installs an unrequested component. |
| **Update** | Download and build `<new-version>` under staging/a new release directory. Run locked sync and health checks without changing `active_version`; then atomically switch active and retain the old release as `previous_version`. Clean older releases only after the new version has completed an acceptance launch. |
| **Failed engine update** | Keep the old engine active if failure occurs before activation. If post-activation acceptance fails, atomically restore `previous_version`, mark the new release failed, and retain diagnostics. Never perform dependency rollback in place. |
| **Uninstall** | Remove the application directory and HKCU application registration. Prompt separately to keep/delete: runtime/Python, managed voices, uv cache/downloads, logs, and config/user state. Default to preserving config/user state; delete only selected manifest-owned categories. Report files that could not be removed. |

Only one operation may run at a time. Stale-lock recovery requires process-liveness and operation-id checks; elapsed time alone must not break a live operation.

## 6. Ordered implementation phases

Strict TDD applies to every behavior-changing phase: add a small, reproducibly failing test first, capture why it fails, implement the minimum behavior, then refactor while the focused suite stays green. Do not substitute a build that merely succeeds for behavioral verification.

### Phase 0 — Approve release contracts (non-behavioral gate)

- [ ] Pin Windows architecture support, `uv.exe`, Python, engine, and payload versions in one release manifest.
- [ ] Approve the Piper GPL distribution decision and its notices/source obligations.
- [ ] Decide whether OBS control is core, optional, replaced, or excluded; `obsws-python` is GPL-3.0-only and cannot enter a public payload accidentally.
- [ ] Approve the exact EN/ES voice manifest: source URLs, model cards, license texts, model/config hashes, size, and listening-quality sign-off.
- [ ] Establish provenance and redistribution permission for Kira artwork before any public signed release.
- [ ] Decide whether `cloud-tts`/Edge-TTS is part of the default core runtime so a Piper-free first install still has a voice path.
- [ ] Approve code-signing identity and timestamp service.
- [ ] Document the engine payload format and HTTPS release location.

Focused verification: schema validation of release metadata, checksum recomputation, license/notices completeness, and cross-file version parity. No runtime behavior changes occur in this phase.

### Phase 1 — Establish the locked core engine boundary

- [ ] **RED:** add dependency-boundary tests proving a core Tauri backend install includes the API/runtime dependencies and excludes legacy CTk UI, Piper, `torch`, `transformers`, and `qwen_tts`.
- [ ] Split `pyproject.toml` groups/extras so core, Piper, integrations, development, and frozen legacy UI are explicit and non-overlapping.
- [ ] Ensure all production Python entry points needed by Tauri, including any required PTT bridge, live inside the installable package rather than only at repository root.
- [ ] Define the exact `uv sync --locked --no-editable` invocation for core and regenerate/validate the lock intentionally.
- [ ] Build an immutable engine payload containing the project metadata, lock, package sources, and payload manifest.

Focused verification: Python import/entry-point tests; locked-sync test in an empty isolated directory; installed-distribution metadata inspection; forbidden-package scan; backend startup/health check with external services stubbed.

### Phase 2 — Add manifest-driven paths and launch

- [ ] **RED:** add Rust tests for missing, malformed, stale, escaped-path, and valid runtime manifests, plus Python tests proving writable storage uses explicit data-root inputs.
- [ ] Add the versioned manifest model, atomic read/write/recovery helpers, and fixed-drive/path-containment validation.
- [ ] Replace installed-mode `backend.config.json` discovery in `backend.rs` with HKCU `DataRoot` plus manifest discovery.
- [ ] Pass explicit data/config/log/component paths to Python and remove installed-mode dependence on source/package directories.
- [ ] Keep development configuration behind an explicit development-only branch.

Focused verification: Rust manifest/path unit tests; Unicode/space path fixtures; atomic replacement and recovery tests; backend process argument/environment tests; Python storage-resolution tests.

### Phase 3 — Implement private core provisioning

- [ ] **RED:** add provisioner tests using a fake HTTP source and fake `uv.exe` for progress, retry, cancel, hash mismatch, locked-sync failure, health-check failure, and successful activation.
- [ ] Bundle the pinned private `uv.exe`; verify its hash before execution.
- [ ] Implement release-manifest download, staged extraction, private Python installation, and the three required uv environment variables.
- [ ] Run `uv sync --locked --no-editable` without shell interpolation or global Python/PATH mutation.
- [ ] Implement operation locking, structured progress/errors, cancellation cleanup, and atomic activation.
- [ ] Add the Tauri-owned first-run contract: choose/validate `DataRoot`, create `InstallId`, write HKCU, and persist initial component choices.
- [ ] Connect first-run Tauri UI states: Choose storage, Choose voice components, Provision, Progress, Cancel, Retry, Repair, and Diagnostics.

Focused verification: deterministic state-machine unit tests; mocked process integration; local HTTP fault injection; no-system-Python clean-environment test; core health check; process cleanup after cancel/failure.

### Phase 4 — Implement the optional Piper component

- [ ] **RED:** add tests proving unselected installs contain neither `piper-tts` nor voices, and that install/remove/repair transitions preserve a healthy core engine.
- [ ] Add Piper as a separately synced dependency group with a pinned package version and stage component changes in successor engine generations rather than mutating the active environment.
- [ ] Implement approved voice-manifest download, dual-file model/config verification, license notice placement, and component-owned path tracking.
- [ ] Add Settings actions for Install, Remove, Retry, and Repair using the same component manager as first run.
- [ ] Make voice discovery read the component manifest rather than project-relative paths.

Focused verification: dependency metadata scan; voice hash/license tests; install/remove idempotency; interrupted component operation recovery; EN/ES synthesis smoke tests; core launch after removal.

### Phase 5 — Implement update, rollback, and repair

- [ ] **RED:** add tests proving a failed staged update cannot replace the active engine and a failed post-activation acceptance restores the previous engine.
- [ ] Implement side-by-side engine releases, pending/active/previous pointers, acceptance launch, and bounded retention.
- [ ] Implement repair from authoritative release/component manifests without touching user-owned data.
- [ ] Define compatibility/migration hooks for manifest schema and user config, with backups before irreversible migrations.

Focused verification: failure injection at every transaction boundary; previous-version launch; corrupt/missing artifact repair; schema migration; user-data byte-for-byte preservation checks.

### Phase 6 — Compose NSIS installation and uninstall

- [ ] **RED:** add installer-harness assertions for default/custom application paths, silent upgrade behavior, preservation of the Tauri-owned HKCU handoff, and every uninstall retention choice.
- [ ] Configure Tauri NSIS for per-user default and a selectable application path only.
- [ ] Do not write runtime/component choices or generate an editable `backend.config.json` from NSIS.
- [ ] Ensure application updates preserve the Tauri-selected data root and current component choices.
- [ ] Add selective cleanup UI and manifest-owned deletion safeguards to uninstall.

Focused verification: unattended installer harness where supported; registry assertions; spaces/Unicode and custom-drive cases; upgrade-in-place; uninstall cleanup matrix; no HKLM/PATH/Python registration side effects.

### Phase 7 — Rebuild release CI and prove clean-machine behavior

- [ ] **RED:** add release-policy checks that fail for missing submodule content, version drift, unsigned status ambiguity, missing notices/SBOM, or forbidden Python distributions/artifacts.
- [ ] Replace the legacy launcher release composition with Tauri NSIS plus versioned online engine/component payloads.
- [ ] Check out submodules recursively and assert `OpenCohost_UI` is at the parent repository's recorded gitlink.
- [ ] Produce checksums, SBOMs, notices/licenses, and signature status for all published payloads.
- [ ] Run the clean-VM acceptance matrix and retain logs/screenshots/manifests as release evidence.

Focused verification: CI policy/unit tests; reproducible artifact inventory; checksum and signature verification; SBOM/license validation; clean Windows VM scenarios below.

## 7. Reviewable work units and likely commit boundaries

No commits are created by this plan. During implementation, keep tests and documentation in the same work unit as the behavior they prove. Split a unit before it exceeds a reviewable diff; do not separate a test from its implementation merely to reduce line count.

| Work unit | Likely conventional commit | Complete when |
| --- | --- | --- |
| WU1: dependency boundary and packaged entry points | `refactor(packaging): isolate the core backend dependency set` | Locked core sync succeeds and forbidden packages/legacy UI are absent. |
| WU2: runtime manifest and explicit storage paths | `feat(runtime): launch the backend from a versioned manifest` | Installed-mode launch and data paths are manifest-driven under path tests. |
| WU3: core provisioner | `feat(provisioning): install the private locked backend runtime` | First provision supports progress, retry, cancel, cleanup, and health-gated activation. |
| WU4: optional Piper lifecycle | `feat(components): manage optional Piper voices` | Initial/later install, remove, and repair are transactional and licensed payloads are verified. |
| WU5: side-by-side update and repair | `feat(updates): add engine rollback and repair` | Failed updates preserve or restore the last-known-good engine. |
| WU6: minimal NSIS and selective uninstall | `feat(installer): package the per-user Tauri application` | Application-path selection, Tauri-owned handoff preservation, upgrades, and cleanup choices pass the harness. |
| WU7: release pipeline and acceptance evidence | `ci(release): publish the verified Tauri Windows distribution` | CI publishes the full evidence set and the clean-VM matrix passes. |

If review size requires chained PRs, use the same boundaries in order: runtime contract (WU1-2), provisioning/components (WU3-5), then installer/release (WU6-7).

## 8. Release CI artifact contract

The Windows release job must:

- Check out the parent repository with submodules recursively and verify the `OpenCohost_UI` gitlink.
- Build the Tauri NSIS installer from `OpenCohost_UI`, not the legacy PyInstaller launcher.
- Publish `OpenCohost-<version>-x64-setup.exe` for the v1 baseline, plus the versioned core engine payload and, only after approval, the Piper/voice payload.
- Publish SHA-256 checksums for NSIS, pinned `uv.exe`, engine payload, component payload, and individual voice model/config files.
- Publish an SPDX or CycloneDX SBOM covering Rust, Node, Python, bundled tools, and component payloads.
- Publish a notices/license bundle containing OpenCohost, bundled tools, Python dependencies, Piper when present, and every shipped voice/model-card obligation.
- Publish machine-readable signature status. A public release must verify Authenticode signatures and timestamping; CI must never label an unsigned artifact as signed.
- Assert version parity across the release manifest, Tauri product version, Rust/Node package metadata, Python engine version, payload filenames, and Git tag.
- Scan the unpacked installer, engine environment metadata, and component payloads for forbidden TTS artifacts. Match Python distributions/modules such as `torch`, `transformers`, and `qwen_tts`; do not reject permitted Ollama configuration strings such as `qwen3:1.7b`.

## 9. Clean-VM acceptance matrix

Run on supported clean Windows x64 VMs with no developer checkout. Preserve installer logs, provisioner logs, final manifest, installed-file inventory, registry export, and signature/checksum results.

| Scenario | Acceptance evidence |
| --- | --- |
| Default per-user install | Installs without elevation, provisions core, launches backend/UI, and writes only intended HKCU entries. |
| Custom application path and `D:` data root | NSIS persists the application path; Tauri first-run persists `D:` as `DataRoot`. Runtime, Python, env, uv cache, logs, and components remain under `D:` while Tauri remains under the chosen app path. |
| Spaces and Unicode paths | Install, provisioning, update, backend launch, Piper synthesis, repair, and uninstall succeed without quoting/path corruption. |
| No Python or Conda installed | Core provisions and starts; no system Python lookup, registration, or PATH change occurs. |
| Piper unselected | Core succeeds; `piper-tts`, voices, ONNX runtime payloads specific to Piper, and Piper notices are absent; Settings offers Install. |
| Piper selected | Approved EN/ES voices install with matching hashes/licenses; synthesis works; Settings offers Remove/Repair. |
| Install Piper later / remove it | Both operations are idempotent, progress/cancel correctly, and preserve core/user data. |
| Ollama missing | Installation and backend startup succeed; LLM-dependent UI reports the external prerequisite actionably without installing anything. |
| Ollama present with user-owned models | OpenCohost connects without changing Ollama installation, configuration, model path, or stored models; Qwen Ollama tags remain usable. |
| Offline after successful provisioning | Core launches without network. With Piper installed, approved EN/ES synthesis works offline. Without Piper, local speech is correctly unavailable rather than silently downloaded. |
| Repair | Deliberately removed/corrupt owned files are restored; unrequested Piper and user-owned files are untouched. |
| Successful update | New engine activates after health checks; component choices and all user data persist; previous release is retained until acceptance. |
| Failed update / rollback | Failure before activation leaves old engine active; post-activation acceptance failure restores it; diagnostics identify the failed release. |
| Uninstall retention choices | Each runtime/voices/cache/logs/config keep-delete combination affects only the selected manifest-owned category; kept data supports reinstall recovery where specified. |
| Forbidden-artifact denial | Unpacked installer, runtime distribution metadata, caches, and component payloads contain no `torch`, `transformers`, `qwen_tts`, Qwen3-TTS model files, or Hugging Face TTS cache. The check explicitly allows Qwen Ollama LLM tags/configuration. |

## 10. Release decision gates and recommended defaults

### Piper GPL boundary

**Gate:** legal acceptance of distributing/provisioning the selected GPL `piper-tts` version and satisfying source, notice, and corresponding-license obligations across updates.

**Recommended default:** ship the core installer independently and keep the Piper component unavailable in public builds until this gate is recorded as accepted. If accepted, pin the exact Piper version and treat its package, notices, source-offer/source location, and hashes as one inseparable component payload. Do not rely on process separation to pretend the GPL question does not exist.

### Voice licenses and hashes

**Gate:** approve an exact immutable manifest for every model and JSON configuration, including source URL, model card, dataset/model license, attribution text, SHA-256, and redistribution approval.

**Recommended default:** start evaluation with one EN candidate such as `en_US-kristin-medium` and one ES candidate such as `es_MX-claude-high`, then publish only after legal and quality acceptance. Do not select `en_US-lessac-high` by default because its upstream dataset terms are unsuitable for an assumed redistributable default. A manifest without explicit approval is denied, not best-effort.

### OBS integration license

**Gate:** decide the distribution posture for OBS control before adding the `integrations` extra to any public engine payload. The currently locked `obsws-python` package is GPL-3.0-only and imported in-process.

**Recommended default:** keep `integrations` out of the core payload until OpenCohost either accepts the applicable GPL obligations or replaces the client with a distribution-compatible implementation. Optional installation is still distribution and does not erase the license question.

### Kira artwork provenance

**Gate:** record the source, author, permission, and redistribution terms for every Kira asset included by Tauri or the engine.

**Recommended default:** block public signed artifacts that contain unverified artwork. Internal test builds may use placeholders clearly excluded from release evidence.

### Default voice path

**Gate:** decide whether Edge-TTS is part of the core runtime or an explicit first-run component. A Piper-free install must not silently become voice-less.

**Recommended default:** include the lightweight `cloud-tts` extra in the default online profile after service/reliability terms are accepted; keep Piper separately optional and offline-capable.

### Code signing

**Gate:** availability of an organization-controlled Authenticode certificate, protected signing workflow, and trusted timestamp verification.

**Recommended default:** require valid, timestamped Authenticode for public v1 NSIS and any executable payload. If signing is not ready, label artifacts as internal/pre-release with machine-readable `unsigned` status; do not publish them as production-ready.

## 11. Historical plan relationship

For this Windows v1 initiative, this plan supersedes the obsolete CustomTkinter composition and Ollama-install assumptions in `conductor/tracks/packaging_deploy_20260510/plan.md`. That file remains a historical record and must not be edited as part of this initiative.

## 12. Definition of Done and first implementation slice

### Definition of Done

- A signed per-user Tauri NSIS installs the application on a clean Windows VM without taking ownership of runtime/component configuration.
- Tauri first-run selects a writable local fixed-drive data root and provisions/health-checks a private locked core engine without system Python/Conda/PATH changes or manual configuration.
- Piper is truly optional and can be installed, removed, and repaired transactionally with approved EN/ES license/hash evidence.
- Updates preserve user data and component choices and recover the last-known-good engine after failure.
- Uninstall honors explicit retention choices.
- CI publishes verified checksums, SBOM, notices/licenses, signature status, version-parity evidence, and a clean-VM acceptance record.
- Forbidden Qwen3-TTS/Torch/Transformers artifacts are absent while external Ollama and Qwen LLM tags remain supported.

### Recommended first slice

Implement **WU1 only**: isolate the locked core backend dependency set, package every Tauri-required Python entry point, and prove an empty private environment can run `uv sync --locked --no-editable`, start the API, and exclude legacy UI, Piper, Torch, Transformers, and `qwen_tts`. This removes the highest-risk packaging ambiguity before writing NSIS or network-provisioning behavior.
