<#
.SYNOPSIS
    Build the OpenCohost Windows installer locally (docs/RELEASE.md section 5).

.DESCRIPTION
    Runs the same steps as the release workflow, on this machine:
      1. engine payload zip
      2. standalone Python 3.12.4 with the locked dependencies
      3. staging of OpenCohost_UI/src-tauri/resources/
      4. pnpm build + tauri build (NSIS)
      5. release_assets/ with the installer, the engine zip and SHA256SUMS.txt

    Local only: it never tags, pushes or uploads anything.

    The MSVC runtime DLLs and the Laya export are kept in
    build_artifacts/release-inputs/ because staging moves resources/ aside.
    They are refreshed from resources/ when present there.

.PARAMETER Version
    Optional. Must match opencohost.__version__ when given; the script stops otherwise.

.PARAMETER ReusePython
    Keep an existing packaging/standalone_python instead of rebuilding it.

.PARAMETER Offline
    Pass --offline to staging (use only cached uv, Piper and MiniLM downloads).

.PARAMETER NoLaya
    Build without the Laya model.

.PARAMETER DryRun
    Run the preflight checks and print the plan without building anything.

.EXAMPLE
    .\scripts\build-release-local.ps1
.EXAMPLE
    .\scripts\build-release-local.ps1 -Version 0.5.0 -ReusePython -Offline
#>
[CmdletBinding()]
param(
    [string]$Version,
    [switch]$ReusePython,
    [switch]$Offline,
    [switch]$NoLaya,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$UiDir = Join-Path $Root "OpenCohost_UI"
$ResourcesDir = Join-Path $UiDir "src-tauri\resources"
$NsisDir = Join-Path $UiDir "src-tauri\target\release\bundle\nsis"
$InputsDir = Join-Path $Root "build_artifacts\release-inputs"
$PayloadDir = Join-Path $Root "build_artifacts\payloads"
$AssetsDir = Join-Path $Root "release_assets"
$PythonDir = Join-Path $Root "packaging\standalone_python"
$EmbeddedDir = Join-Path $Root "packaging\embedded_runtime\python"

function Write-Step([string]$Message) {
    Write-Host ""
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Write-Warn([string]$Message) {
    Write-Host "WARNING: $Message" -ForegroundColor Yellow
}

function Stop-Build([string]$Message) {
    Write-Host ""
    Write-Host "BUILD FAILED: $Message" -ForegroundColor Red
    exit 1
}

# Native commands do not throw on a non-zero exit code in PowerShell 5.1.
function Invoke-Native([string]$Label, [scriptblock]$Command) {
    & $Command
    if ($LASTEXITCODE -ne 0) {
        Stop-Build "$Label exited with code $LASTEXITCODE"
    }
}

# Same mapping as .github/workflows/release.yml: 0.5.0a1 -> 0.5.0-alpha.1
function ConvertTo-SemVer([string]$Pep440) {
    $tags = @{ "a" = "alpha"; "b" = "beta"; "rc" = "rc" }
    $m = [regex]::Match($Pep440, '^(\d+\.\d+\.\d+)(a|b|rc)(\d+)$')
    if ($m.Success) {
        return "$($m.Groups[1].Value)-$($tags[$m.Groups[2].Value]).$($m.Groups[3].Value)"
    }
    return $Pep440
}

function ConvertTo-Pep440([string]$SemVer) {
    $tags = @{ "alpha" = "a"; "beta" = "b"; "rc" = "rc" }
    $m = [regex]::Match($SemVer, '^(\d+\.\d+\.\d+)-(alpha|beta|rc)\.(\d+)$')
    if ($m.Success) {
        return "$($m.Groups[1].Value)$($tags[$m.Groups[2].Value])$($m.Groups[3].Value)"
    }
    return $SemVer
}

# ---------------------------------------------------------------- preflight
Write-Step "Preflight"

foreach ($tool in @("uv", "pnpm", "git")) {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) {
        Stop-Build "'$tool' is not on PATH"
    }
}

$initText = Get-Content -Raw (Join-Path $Root "opencohost\__init__.py")
$vm = [regex]::Match($initText, '__version__\s*=\s*["'']([^"'']+)["'']')
if (-not $vm.Success) { Stop-Build "could not read __version__ from opencohost/__init__.py" }
$PkgVersion = $vm.Groups[1].Value
$SemVer = ConvertTo-SemVer $PkgVersion

if ($Version) {
    $requested = ConvertTo-Pep440 ($Version.TrimStart("v"))
    if ($requested -ne $PkgVersion) {
        Stop-Build "-Version $Version does not match opencohost.__version__ == $PkgVersion"
    }
}
Write-Host "Version: $PkgVersion (semver $SemVer)"

$uiStatus = git -C $UiDir status --porcelain
if ($uiStatus) {
    Write-Warn "OpenCohost_UI has uncommitted changes; they WILL be packaged as they are."
}
$rootStatus = git -C $Root status --porcelain --ignore-submodules=all
if ($rootStatus) {
    Write-Warn "the main repository has uncommitted changes; the engine payload packages the working tree."
}

$listening = Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |
    Where-Object { $_.LocalPort -in 8765, 8770 }
if ($listening) {
    Write-Warn "a process is listening on port 8765/8770. Stop it before you TEST the installed app."
}

# MSVC runtime and Laya inputs: refresh the cache from resources/ when possible.
$CrtSource = Join-Path $ResourcesDir "runtime\msvc-crt"
$CrtInput = Join-Path $InputsDir "msvc-crt"
$LayaSource = Join-Path $ResourcesDir "models\laya\multilingual"
$LayaInput = Join-Path $InputsDir "laya"

$crtDlls = @()
if (Test-Path $CrtSource) { $crtDlls = @(Get-ChildItem $CrtSource -Filter *.dll) }
if ($crtDlls.Count -eq 0 -and -not (Test-Path $CrtInput)) {
    Stop-Build "no MSVC runtime DLLs in $CrtSource or $CrtInput (see docs/RELEASE.md section 5)"
}
$useLaya = -not $NoLaya
if ($useLaya -and -not (Test-Path (Join-Path $LayaSource "manifest.json")) -and -not (Test-Path (Join-Path $LayaInput "manifest.json"))) {
    Stop-Build "no Laya export in $LayaSource or $LayaInput; pass -NoLaya to build without it"
}

Write-Host "Plan:"
Write-Host "  1. engine payload  -> build_artifacts\payloads\engine-$SemVer.zip"
if ($ReusePython -and (Test-Path (Join-Path $PythonDir "python.exe"))) {
    Write-Host "  2. standalone Python: reuse packaging\standalone_python"
} else {
    Write-Host "  2. standalone Python: rebuild packaging\standalone_python"
}
Write-Host "  3. stage resources (offline: $([bool]$Offline), laya: $useLaya)"
Write-Host "  4. pnpm build + tauri build"
Write-Host "  5. release_assets\OpenCohost-$SemVer-x64-setup.exe, engine zip, SHA256SUMS.txt"

if ($DryRun) {
    Write-Host ""
    Write-Host "Dry run: nothing was built." -ForegroundColor Green
    exit 0
}

$started = Get-Date

# ---------------------------------------------------------------- inputs
Write-Step "Caching MSVC runtime and Laya inputs in build_artifacts\release-inputs"
New-Item -ItemType Directory -Force $InputsDir | Out-Null
if ($crtDlls.Count -gt 0) {
    if (Test-Path $CrtInput) { Remove-Item -Recurse -Force $CrtInput }
    New-Item -ItemType Directory -Force $CrtInput | Out-Null
    # DLLs only: staging expects exactly the pinned DLL set.
    $crtDlls | Copy-Item -Destination $CrtInput
}
if ($useLaya -and (Test-Path (Join-Path $LayaSource "manifest.json"))) {
    if (Test-Path $LayaInput) { Remove-Item -Recurse -Force $LayaInput }
    Copy-Item -Recurse $LayaSource $LayaInput
}

# ---------------------------------------------------------------- 1. payload
Write-Step "1/5 Engine payload"
New-Item -ItemType Directory -Force $PayloadDir | Out-Null
$Payload = Join-Path $PayloadDir "engine-$SemVer.zip"
Push-Location $Root
try {
    Invoke-Native "build_engine_payload" { uv run python packaging\build_engine_payload.py . $Payload }
} finally { Pop-Location }

# ---------------------------------------------------------------- 2. python
Write-Step "2/5 Standalone Python"
if ($ReusePython -and (Test-Path (Join-Path $PythonDir "python.exe"))) {
    Write-Host "Reusing $PythonDir"
} else {
    if (Test-Path $PythonDir) { Remove-Item -Recurse -Force $PythonDir }
    Push-Location $Root
    try {
        Invoke-Native "uv python install" { uv python install 3.12.4 --install-dir $EmbeddedDir }
        Copy-Item -Recurse (Join-Path $EmbeddedDir "cpython-3.12.4-windows-x86_64-none") $PythonDir
        Remove-Item -ErrorAction SilentlyContinue (Join-Path $PythonDir "Lib\EXTERNALLY-MANAGED")
        Invoke-Native "uv pip install" { uv pip install --break-system-packages ".[local-tts,cloud-tts]" --python (Join-Path $PythonDir "python.exe") }
    } finally { Pop-Location }
}

# ---------------------------------------------------------------- 3. stage
Write-Step "3/5 Staging installer resources"
$stageArgs = @("--version", $SemVer, "--engine-payload", $Payload, "--msvc-runtime-dir", $CrtInput)
if ($useLaya) { $stageArgs += @("--laya-onnx-dir", $LayaInput) }
if ($Offline) { $stageArgs += "--offline" }
Push-Location $Root
try {
    Invoke-Native "stage_installer_resources" { uv run python packaging\stage_installer_resources.py @stageArgs }
} finally { Pop-Location }

# ---------------------------------------------------------------- 4. build
Write-Step "4/5 UI and Tauri NSIS build"
if (Test-Path $NsisDir) {
    $previous = Join-Path $Root "build_artifacts\previous-installers"
    New-Item -ItemType Directory -Force $previous | Out-Null
    Get-ChildItem $NsisDir -Filter *.exe | Move-Item -Destination $previous -Force
}
Push-Location $UiDir
try {
    Invoke-Native "pnpm install" { pnpm install --frozen-lockfile }
    Invoke-Native "pnpm build" { pnpm build }
    Invoke-Native "tauri build" { pnpm exec tauri build }
} finally { Pop-Location }

# ---------------------------------------------------------------- 5. collect
Write-Step "5/5 Collecting release assets"
$installers = @(Get-ChildItem $NsisDir -Filter *.exe -ErrorAction SilentlyContinue)
if ($installers.Count -ne 1) {
    Stop-Build "expected one installer in $NsisDir, found $($installers.Count)"
}
if (Test-Path $AssetsDir) { Remove-Item -Recurse -Force $AssetsDir }
New-Item -ItemType Directory -Force $AssetsDir | Out-Null
$Setup = Join-Path $AssetsDir "OpenCohost-$SemVer-x64-setup.exe"
Copy-Item $installers[0].FullName $Setup
Copy-Item $Payload $AssetsDir

# Same format as `sha256sum *`: "<lowercase hash>  <name>", LF endings.
$lines = Get-ChildItem $AssetsDir -File | Sort-Object Name | ForEach-Object {
    "{0}  {1}" -f (Get-FileHash -Algorithm SHA256 $_.FullName).Hash.ToLower(), $_.Name
}
[System.IO.File]::WriteAllText((Join-Path $AssetsDir "SHA256SUMS.txt"), (($lines -join "`n") + "`n"), (New-Object System.Text.UTF8Encoding $false))

$elapsed = (Get-Date) - $started
Write-Host ""
Write-Host ("BUILD OK in {0:hh\:mm\:ss}" -f $elapsed) -ForegroundColor Green
Get-ChildItem $AssetsDir | ForEach-Object { Write-Host ("  {0,-40} {1,10:N1} MB" -f $_.Name, ($_.Length / 1MB)) }
Write-Host ""
Write-Host "Reminder: staging rewrote OpenCohost_UI/src-tauri/resources/bootstrap-manifest.json. Do not commit it."
Write-Host "Nothing was tagged, pushed or uploaded."
