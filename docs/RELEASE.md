# Release Process

This guide matches `.github/workflows/release.yml` and `.github/workflows/ci.yml`.
A release starts with a version tag. CI runs the tests, the workflow builds the
Windows installer and the engine payload, and it creates a **draft** GitHub
release. A maintainer checks the draft and publishes it by hand.

## Prerequisites

- Push access to the repository, to push the tag and to publish the draft.
- `master` is green. `ci.yml` runs on every push to `master`, and the release
  workflow calls it as its test gate.
- For a local build: Windows x64, Node 22, pnpm 11.5.2, Rust stable, uv 0.11.6,
  and the pinned Microsoft x64 VC runtime (section 5).

## 1. Set the version

The version appears in four files. Each one must match.

| File | Field | `0.5.0` | `0.5.0a1` (alpha) |
|---|---|---|---|
| `opencohost/__init__.py` | `__version__` (PEP 440) | `0.5.0` | `0.5.0a1` |
| `OpenCohost_UI/package.json` | `version` (semver) | `0.5.0` | `0.5.0-alpha.1` |
| `OpenCohost_UI/src-tauri/tauri.conf.json` | `version` (semver) | `0.5.0` | `0.5.0-alpha.1` |
| `OpenCohost_UI/src-tauri/Cargo.toml` | `version` (semver) | `0.5.0` | `0.5.0-alpha.1` |

Then:

1. Commit the UI version bump inside `OpenCohost_UI`, then run `cargo check` in
   `OpenCohost_UI/src-tauri` so `Cargo.lock` picks up the new version.
2. In the main repository, commit the Python version together with the updated
   `OpenCohost_UI` submodule pointer. CI checks out submodules at that pointer,
   so it must reference the commit that carries the new version.

Commit message: `chore(release): set version X.Y.Z`.

`tests/test_release_ci_contract.py` checks that `package.json` and
`tauri.conf.json` match `__version__`. The workflow maps the `a`, `b` and `rc`
suffixes to `-alpha.N`, `-beta.N` and `-rc.N`.

## 2. Tag and push

```bash
git tag -a v0.5.0 -m "OpenCohost v0.5.0"
git push origin v0.5.0
```

The tag uses the semver form: `v0.5.0`, or `v0.5.0-alpha.1` for `0.5.0a1`. The
first build job rejects any tag that does not normalize to `__version__`.

## 3. What the workflow does

1. **Tests.** The `test` job calls `ci.yml`. It runs `pytest tests/` on Windows
   and the `ui` job (`tsc --noEmit` and `vitest run`). Nothing below starts until
   both pass.
2. **Engine payload** (Ubuntu). Verifies the tag against `opencohost/__init__.py`,
   builds `engine-<version>.zip` with `packaging/build_engine_payload.py`, and
   uploads it.
3. **Installer** (Windows). Installs standalone Python 3.12.4 with uv and the
   locked dependencies, then runs `packaging/stage_installer_resources.py`. That
   script fills `OpenCohost_UI/src-tauri/resources/` with the engine, the
   standalone Python, the MSVC runtime, uv, the Piper and MiniLM assets, the VRM
   avatars, and a bootstrap manifest that records the engine zip's SHA256 and
   URL. The job then runs `pnpm build` and `pnpm exec tauri build`, and copies
   the NSIS installer to `OpenCohost-<version>-x64-setup.exe`.
4. **Draft release** (Ubuntu). Writes `SHA256SUMS.txt`, then runs
   `gh release create v<version> --draft`. The release is marked as a pre-release
   only when the version carries an `-alpha`, `-beta` or `-rc` suffix.

## 4. Review and publish the draft

1. Open the draft on the Releases page. Confirm three assets:
   `OpenCohost-<version>-x64-setup.exe`, `engine-<version>.zip` and
   `SHA256SUMS.txt`.
2. Write the notes from `docs/release-notes-template.md`. Resolve its TODO items
   and delete its maintainer block.
3. Check a download against `SHA256SUMS.txt`:

   ```powershell
   Get-FileHash -Algorithm SHA256 .\OpenCohost-0.5.0-x64-setup.exe
   ```

4. Click **Publish release**.

The installer bundles its engine, runtime and voices; first run copies the bundled
copy and downloads the engine from this release only as a fallback. Publish the
draft before announcing so that fallback URL works.

Do not replace an asset on a published release. The installer expects the
engine zip's SHA256, size and URL recorded at build time, so a swapped engine
asset no longer matches. If a published build is wrong, ship a new version.

## 5. Local build

Use this to produce an installer on a Windows machine. Keep the output local.
Do not upload it.

`scripts/build-release-local.ps1` runs every step below, including the
`SHA256SUMS.txt` step, and stops at the first failure. It reads the version from
`opencohost/__init__.py` and keeps the MSVC and Laya inputs in
`build_artifacts/release-inputs/`. Options: `-ReusePython`, `-Offline`,
`-NoLaya`, `-DryRun` (preflight and plan only).

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build-release-local.ps1 -DryRun
powershell -ExecutionPolicy Bypass -File scripts\build-release-local.ps1
```

The manual steps follow.

Staging moves the current `OpenCohost_UI/src-tauri/resources/` aside, so copy
any inputs you still need out of it before you stage. The staging script takes
two inputs from outside `resources/`:

- `--msvc-runtime-dir`: a folder holding exactly the ten pinned x64 MSVC runtime
  DLLs listed in `MSVC_CRT_FILES` in `packaging/stage_installer_resources.py`.
  Required for a complete installer.
- `--laya-onnx-dir`: a verified Laya export (`manifest.json` plus the five files
  in `LAYA_FILES`). Optional: Laya ships only when you pass it.

Run the commands below from the repository root in PowerShell.

```powershell
# Copy the inputs out of resources/ before staging moves it. Use a new folder.
$Inputs = "$env:TEMP\opencohost-release-inputs"
New-Item -ItemType Directory -Path $Inputs | Out-Null
Copy-Item -Recurse OpenCohost_UI\src-tauri\resources\runtime\msvc-crt "$Inputs\msvc-crt"
Copy-Item -Recurse OpenCohost_UI\src-tauri\resources\models\laya\multilingual "$Inputs\laya"

# 1. Engine payload (same command as the build-engine-payload job)
New-Item -ItemType Directory -Force build_artifacts\payloads | Out-Null
uv run python packaging\build_engine_payload.py . build_artifacts\payloads\engine-0.5.0.zip

# 2. Standalone Python (same steps as the installer job; delete an old
#    packaging\standalone_python first)
uv python install 3.12.4 --install-dir packaging\embedded_runtime\python
Copy-Item -Recurse packaging\embedded_runtime\python\cpython-3.12.4-windows-x86_64-none packaging\standalone_python
Remove-Item -ErrorAction SilentlyContinue packaging\standalone_python\Lib\EXTERNALLY-MANAGED
uv pip install --break-system-packages ".[local-tts,cloud-tts]" --python packaging\standalone_python\python.exe

# 3. Stage the installer resources
uv run python packaging\stage_installer_resources.py --version 0.5.0 --engine-payload build_artifacts\payloads\engine-0.5.0.zip --msvc-runtime-dir "$Inputs\msvc-crt" --laya-onnx-dir "$Inputs\laya"

# 4. Move older installers out of the bundle folder, then build
New-Item -ItemType Directory -Force build_artifacts\previous-installers | Out-Null
Move-Item -ErrorAction SilentlyContinue OpenCohost_UI\src-tauri\target\release\bundle\nsis\*.exe build_artifacts\previous-installers\
Push-Location OpenCohost_UI
pnpm install --frozen-lockfile
pnpm build
pnpm exec tauri build
Pop-Location

# 5. Collect one installer and the engine zip
New-Item -ItemType Directory -Force release_assets | Out-Null
Copy-Item OpenCohost_UI\src-tauri\target\release\bundle\nsis\*.exe release_assets\OpenCohost-0.5.0-x64-setup.exe
Copy-Item build_artifacts\payloads\engine-0.5.0.zip release_assets\
```

Then, in Git Bash, from inside `release_assets`, run
`sha256sum * > SHA256SUMS.txt`. This is the same command the publish job uses.

Notes:

- Staging downloads uv, Piper and MiniLM assets that are not already cached. Add
  `--offline` to use only cached files.
- Staging rewrites the tracked `OpenCohost_UI/src-tauri/resources/bootstrap-manifest.json`.
  Do not commit that file. Check with `git -C OpenCohost_UI status`.

## 6. Re-running a failed release

If the run fails before the draft exists, nothing is published. Fix the cause,
commit, and re-tag.

If a draft exists, delete it on the Releases page first, so the publish step
does not collide with it.

Then delete the tag, fix the cause, and re-tag:

```bash
git tag -d v0.5.0
git push origin :refs/tags/v0.5.0
```

A manual run (`workflow_dispatch`) skips the tag check, because the check runs
only for tag refs. Its draft still takes the version from
`opencohost/__init__.py`. Use a tag push for releases.

## 7. Known gaps

These are open as of this guide. Fix them before the first real release.

- **The release job has never run with the new CRT and Laya steps.** It now discovers the CRT in the runner's Visual Studio tree (`--msvc-crt-policy signed`) and exports Laya from the pinned checkpoint revision, but this has only been checked locally (unit tests and YAML parsing). Run `workflow_dispatch` once and read the staged `msvc-crt-manifest.json` before relying on it. If the runner's CRT is older than 14.50.35719.0 the stage step fails by design.
- **The UI job is part of the gate.** It runs the full `vitest run`, so a flaky
  test blocks the release.

### Decisions

- **Code signing (2026-11-12): v0.5.0 ships unsigned.** The launch is
  community-first and unmonetized, and no signing certificate is available.
  `SHA256SUMS.txt` is published with every release so users can verify the
  download, and the release notes carry a Windows SmartScreen note. Revisit for
  a later release with either Azure Trusted Signing (tauri
  `bundle.windows.signCommand`) or an OV certificate
  (`bundle.windows.certificateThumbprint`).
- **MSVC runtime source and verification (2026-11-12).** CI takes the ten x64 CRT DLLs unmodified from the runner's Visual Studio
  `VC\Redist\MSVC\<ver>\x64\Microsoft.VC14*.CRT` folder. `--msvc-crt-policy signed` requires valid Microsoft Authenticode signatures,
  file version >= 14.50.35719.0 and no debug DLLs, and writes the exact hashes into `msvc-crt-manifest.json`. Local runs keep the default
  `exact` policy (pinned hashes).
- **Laya ships (2026-11-12).** The `multilingual` checkpoint is Apache-2.0 and its base model MIT, both compatible with a GPL-3.0 combined
  work. CI exports ONNX from checkpoint revision `7b928d82...` with the upstream exporter at `1adc59f7` and caches it by revision.
- **Licence notes (2026-11-12).** THIRD-PARTY-NOTICES.md records Laya, mmBERT-base, the Piper voice (Apache-2.0 per the voice card; no
  recording licence stated) and the VC++ runtime. The release notes template now offers Corresponding Source under GPL-3.0 section 6(d)
  next to the download. Not legal advice; a lawyer should review before publishing.
