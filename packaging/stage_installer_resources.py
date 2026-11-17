"""Build a verified fresh installer inventory, preserving previous stages outside resources."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tempfile
import urllib.request
import uuid
import zipfile

REPO_ROOT = Path(__file__).resolve().parent.parent
TAURI_RESOURCES = REPO_ROOT / "OpenCohost_UI" / "src-tauri" / "resources"
BOOTSTRAP_MANIFEST = TAURI_RESOURCES / "bootstrap-manifest.json"
PIPER_BASE_URL = "https://huggingface.co/rhasspy/piper-voices/resolve/main"
# (voice stem, path inside the rhasspy/piper-voices repository)
PIPER_VOICES = (
    ("es_MX-claude-high", "es/es_MX/claude/high"),
    ("en_US-kristin-medium", "en/en_US/kristin/medium"),
)
MINILM_BASE_URL = "https://huggingface.co/Xenova/paraphrase-multilingual-MiniLM-L12-v2/resolve/main"
UV_URL = "https://github.com/astral-sh/uv/releases/download/0.11.6/uv-x86_64-pc-windows-msvc.zip"
LAYA_FILES = ("laya.onnx", "laya.onnx.data", "tokenizer.json", "tokenizer_config.json", "rl_agent_config.json")
SUPPORTED_EXTRAS = ("local-tts", "cloud-tts", "integrations", "vision")
MSVC_CRT_VERSION = "14.50.35719.0"
MSVC_CRT_POLICIES = ("exact", "signed")
MSVC_CRT_SOURCE = "Microsoft Visual Studio VC Redist MSVC 14.50.35710 x64"
KNOWN_PYTHON_CRT_SHA256 = {
    "vcruntime140.dll": "3ea53d61f2c5a96bfaa6722b271bcfca84b1f3fb1020c5d081e12762eb3797ce",
    "vcruntime140_1.dll": "03fd6ba760bb3e85f55c9ce707ae11d3a25cb1487c987c6c1f8d3262c9875acf",
}
MSVC_CRT_FILES = {
    "concrt140.dll": "b2faf3b85b23c840b654e57d5497a0ad31acd02fb01856cad4725a1715d5f78e",
    "msvcp140.dll": "def46aa6a8f72f27bafac0c43334419486a4d1dcdb6c479a8ef7034b3e1fa4cb",
    "msvcp140_1.dll": "2dd670f874562fbdca5b022df1943d70a57ba91fde559280e3a1daebe4db2380",
    "msvcp140_2.dll": "1d60da3ac2b06482912ca852fa7047436e6e474b4cfffa3bf77f4598cfbf454c",
    "msvcp140_atomic_wait.dll": "e7963645e0d1db08e300614d4c5fa7194bd8173e9ab7a5558859e6b232ed3241",
    "msvcp140_codecvt_ids.dll": "ae8d922b00cdd93e3ebecc37beb46c800f383ebdeb9f9e5b84e04a72428b6fb3",
    "vccorlib140.dll": "6b8d8a76c3e6664293407553650e60b94df9aaafc7c92057ea83032bd228e44f",
    "vcruntime140.dll": "184146852727a9db4eea06178716bec3cdbb1015c911f6b0f915b184ad7775b2",
    "vcruntime140_1.dll": "e6bfb3662ab4b1969a73441dbe35c96d51441b6bff8cf1fe7430bd5b246ca605",
    "vcruntime140_threads.dll": "a6222020b500a9a86b36e040c2dbd0e459716db1bf2810a11cd7512ea9b8d89b",
}


def _compute_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _file_record(path: Path) -> dict:
    return {"size": path.stat().st_size, "sha256": _compute_sha256(path)}


def _version_tuple(text: str) -> tuple[int, ...]:
    if not re.fullmatch(r"\d+(\.\d+){1,3}", text or ""):
        raise ValueError(f"MSVC CRT file version is missing or malformed: {text!r}")
    return tuple(int(part) for part in text.split("."))


def inspect_windows_file(path: Path) -> dict:
    """Authenticode status, signer subject and file version, read through PowerShell (Windows only)."""
    import os
    script = ("$p = $env:OPENCOHOST_INSPECT_PATH; "
              "$s = Get-AuthenticodeSignature -LiteralPath $p; "
              "$v = (Get-Item -LiteralPath $p).VersionInfo; "
              "[pscustomobject]@{status=[string]$s.Status; "
              "subject=[string]$s.SignerCertificate.Subject; "
              "version=('{0}.{1}.{2}.{3}' -f $v.FileMajorPart,$v.FileMinorPart,$v.FileBuildPart,$v.FilePrivatePart)} "
              "| ConvertTo-Json -Compress")
    env = {**os.environ, "OPENCOHOST_INSPECT_PATH": str(path)}
    result = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                            env=env, capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


def _verify_signed_member(path: Path, inspector) -> dict:
    facts = inspector(path)
    if facts.get("status") != "Valid":
        raise ValueError(f"MSVC CRT member has no valid Authenticode signature: {path.name}")
    signer = facts.get("subject") or ""
    if "Microsoft Corporation" not in signer:
        raise ValueError(f"MSVC CRT member is not signed by Microsoft (signer): {path.name}")
    version = facts.get("version") or ""
    if _version_tuple(version) < _version_tuple(MSVC_CRT_VERSION):
        raise ValueError(f"MSVC CRT member version {version} is older than {MSVC_CRT_VERSION}: {path.name}")
    return {"version": version, "signer": signer}


def stage_msvc_crt(source: Path, destination: Path, *, policy: str = "exact", inspector=None) -> dict:
    """Stage the x64 VC redist family.

    ``exact`` (default) requires the hashes pinned in MSVC_CRT_FILES. ``signed``
    (CI) requires the same ten files, each validly Microsoft-signed with a file
    version >= MSVC_CRT_VERSION and no debug DLLs, and records the exact hashes shipped.
    """
    if policy not in MSVC_CRT_POLICIES:
        raise ValueError(f"unknown MSVC CRT policy: {policy}")
    source = source.resolve()
    if destination.exists():
        raise FileExistsError("MSVC CRT destination must be fresh")
    dlls = {path.name.lower(): path for path in source.iterdir() if path.is_file() and path.suffix.lower() == ".dll"}
    if policy == "signed" and any(re.fullmatch(r"\w+140d(_\w+)?\.dll", name) for name in dlls):
        raise ValueError("MSVC CRT source contains debug runtime DLLs")
    if set(dlls) != set(MSVC_CRT_FILES):
        raise ValueError("MSVC CRT source must contain exactly the pinned x64 runtime family")
    inspector = inspector or inspect_windows_file
    verified = {}
    for name, expected_hash in MSVC_CRT_FILES.items():
        member = _contained(dlls[name], source)
        if not member.is_file():
            raise ValueError(f"MSVC CRT member is missing, mixed, or untrusted: {name}")
        record = _file_record(member)
        if policy == "exact":
            if record["sha256"] != expected_hash:
                raise ValueError(f"MSVC CRT member is missing, mixed, or untrusted: {name}")
        else:
            record.update(_verify_signed_member(member, inspector))
        verified[name] = record
    destination.mkdir(parents=True)
    for name in MSVC_CRT_FILES:
        shutil.copy2(dlls[name], destination / name)
        if _compute_sha256(destination / name) != verified[name]["sha256"]:
            raise ValueError(f"MSVC CRT changed while staging: {name}")
    manifest = {
        "schema": 1,
        "publisher": "Microsoft Corporation",
        "architecture": "x64",
        "version": MSVC_CRT_VERSION if policy == "exact" else min((r["version"] for r in verified.values()), key=_version_tuple),
        "source_distribution": MSVC_CRT_SOURCE if policy == "exact" else "Visual Studio VC Redist MSVC x64 (CI runner)",
        "files": verified,
    }
    if policy == "signed":
        manifest["policy"] = "signed"
    (destination / "msvc-crt-manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    return manifest


def copy_msvc_crt_to_python(crt_dir: Path | None, python_root: Path) -> None:
    if crt_dir is None:
        raise ValueError("a pinned Microsoft x64 MSVC CRT family is required for Python")
    crt_dir = crt_dir.resolve()
    manifest_path = crt_dir / "msvc-crt-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    signed = manifest.get("policy") == "signed"
    if (manifest.get("publisher") != "Microsoft Corporation" or manifest.get("architecture") != "x64"
            or set(manifest.get("files", {})) != set(MSVC_CRT_FILES)
            or (not signed and manifest.get("version") != MSVC_CRT_VERSION)):
        raise ValueError("MSVC CRT provenance manifest is invalid")
    if signed:
        expected = {name: manifest["files"][name].get("sha256") for name in MSVC_CRT_FILES}
        if any(_version_tuple(manifest["files"][name].get("version", "")) < _version_tuple(MSVC_CRT_VERSION)
               or "Microsoft Corporation" not in manifest["files"][name].get("signer", "") for name in MSVC_CRT_FILES):
            raise ValueError("MSVC CRT provenance manifest is invalid")
    else:
        expected = dict(MSVC_CRT_FILES)
    for name, expected_hash in expected.items():
        source = _contained(crt_dir / name, crt_dir)
        if not source.is_file() or manifest["files"][name].get("sha256") != expected_hash or _compute_sha256(source) != expected_hash:
            raise ValueError(f"staged MSVC CRT is incomplete or changed: {name}")
    for name in MSVC_CRT_FILES:
        destination = python_root / name
        if destination.exists():
            existing_hash = _compute_sha256(destination) if destination.is_file() else None
            if existing_hash != expected[name] and existing_hash != KNOWN_PYTHON_CRT_SHA256.get(name):
                raise ValueError(f"refusing to overwrite unrecognized Python runtime file: {name}")
            if existing_hash == expected[name]:
                continue
            shutil.copy2(crt_dir / name, destination)
        else:
            shutil.copy2(crt_dir / name, destination)


def _contained(path: Path, root: Path) -> Path:
    resolved = path.resolve()
    if resolved == root.resolve() or not resolved.is_relative_to(root.resolve()):
        raise ValueError(f"staging path escapes its authorized root: {path.name}")
    return resolved


def stage_laya_assets(source: Path, resources: Path) -> dict:
    """Validate the exact exported ONNX allowlist before writing any model bytes."""
    source = source.resolve()
    try:
        manifest = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
        files = manifest["files"]
        if not isinstance(files, dict) or set(files) != set(LAYA_FILES):
            raise ValueError("Laya manifest must claim exactly the five exported assets")
        expected = {name: files[name].lower() if isinstance(files[name], str) else files[name] for name in LAYA_FILES}
        expected["manifest.json"] = _compute_sha256(source / "manifest.json")
        for name in LAYA_FILES:
            member = _contained(source / name, source)
            digest = files[name]
            if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", digest):
                raise ValueError("invalid Laya sha256")
            if not member.is_file() or _compute_sha256(member) != digest.lower():
                raise ValueError(f"Laya member missing or tampered: {name}")
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as error:
        raise ValueError("Laya export is missing or has an invalid manifest") from error
    destination = resources / "models" / "laya" / "multilingual"
    destination.mkdir(parents=True, exist_ok=False)
    inventory = {}
    for name in (*LAYA_FILES, "manifest.json"):
        shutil.copy2(source / name, destination / name)
        inventory[name] = _file_record(destination / name)
        if inventory[name]["sha256"] != expected[name]:
            raise ValueError("Laya changed while staging; previous resources are preserved")
    return inventory


def _download_or_copy(local_candidate: Path, destination: Path, download_url: str, *, offline: bool = False) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if local_candidate.is_file() and local_candidate.stat().st_size:
        shutil.copy2(local_candidate, destination)
        return
    if offline:
        raise FileNotFoundError(f"offline resource cache miss: {destination.name}")
    request = urllib.request.Request(download_url, headers={"User-Agent": "OpenCohost-Packager/1.0"})
    with urllib.request.urlopen(request) as response, destination.open("wb") as output:
        shutil.copyfileobj(response, output)


def _extract_engine(payload: Path, runtime: Path) -> None:
    with zipfile.ZipFile(payload) as archive:
        members = archive.infolist()
        for member in members:
            name = member.filename
            path = PurePosixPath(name)
            if "\\" in name or ":" in name or path.is_absolute() or ".." in path.parts:
                raise ValueError("invalid engine payload path")
            _contained(runtime.joinpath(*path.parts), runtime)
            if (member.external_attr >> 16) & 0o170000 == 0o120000:
                raise ValueError("engine payload cannot contain symbolic links")
        names = [member.filename for member in members]
        try:
            manifest = json.loads(archive.read("payload-manifest.json"))
            records = manifest["files"]
            declared = {record["path"]: record for record in records}
            if (manifest.get("schema") != 1 or manifest.get("format") != "zip"
                    or len(declared) != len(records) or len(names) != len(set(names))
                    or set(names) != {*declared, "payload-manifest.json"}):
                raise ValueError("engine payload inventory mismatch")
            for name, record in declared.items():
                content = archive.read(name)
                if len(content) != record["size"] or hashlib.sha256(content).hexdigest() != record["sha256"]:
                    raise ValueError("engine payload hash mismatch")
        except (KeyError, TypeError, json.JSONDecodeError) as error:
            raise ValueError("invalid engine payload manifest") from error
        for member in members:
            if member.filename != "payload-manifest.json":
                archive.extract(member, runtime)
    if not (runtime / "opencohost" / "__init__.py").is_file():
        raise ValueError("engine payload has no opencohost package")


def stage_resources(version: str, engine_payload: Path | None = None, skip_python: bool = False,
                    *, laya_onnx_dir: Path | None = None, offline: bool = False,
                    msvc_runtime_dir: Path | None = None, msvc_crt_policy: str = "exact") -> None:
    resources = _contained(TAURI_RESOURCES, REPO_ROOT)
    work_root = _contained(REPO_ROOT / "build_artifacts" / "staging-work", REPO_ROOT)
    backup_root = _contained(REPO_ROOT / "build_artifacts" / "staging-backups", REPO_ROOT)
    for path in (work_root, backup_root):
        if path.is_relative_to(resources) or resources.is_relative_to(path):
            raise ValueError("staging work/backups must be outside bundled resources")
        path.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="resources-", dir=work_root))
    staged = work / "resources"
    staged.mkdir()
    # Legacy callers stage without Laya. Model-inclusive candidates must pass
    # an explicit export; never infer an owner's cache or ignore invalid input.
    if laya_onnx_dir is not None:
        stage_laya_assets(laya_onnx_dir, staged)
    if not skip_python:
        if msvc_runtime_dir is None:
            raise ValueError("--msvc-runtime-dir (MSVC CRT) is required for a complete Windows installer")
        crt_dest = staged / "runtime" / "msvc-crt"
        stage_msvc_crt(msvc_runtime_dir, crt_dest, policy=msvc_crt_policy)

    engine_dest = staged / f"engine-{version}.zip"
    if engine_payload is not None:
        if not engine_payload.is_file():
            raise FileNotFoundError(engine_payload)
        shutil.copy2(engine_payload, engine_dest)
    else:
        import sys
        sys.path.insert(0, str(REPO_ROOT / "packaging"))
        import build_engine_payload
        build_engine_payload.build_payload(REPO_ROOT, engine_dest)
    manifest = json.loads(BOOTSTRAP_MANIFEST.read_text(encoding="utf-8"))
    manifest["product_version"] = version
    manifest["engine"].update(version=version, sha256=_compute_sha256(engine_dest),
                              expected_size=engine_dest.stat().st_size,
                              source=f"https://github.com/plynte-labs/OpenCohost/releases/download/v{version}/engine-{version}.zip")
    (staged / "bootstrap-manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    assets = [("uv-0.11.6.zip", REPO_ROOT / "packaging/uv-0.11.6.zip", UV_URL)]
    for stem, remote_dir in PIPER_VOICES:
        for name in (f"{stem}.onnx", f"{stem}.onnx.json"):
            assets.append((f"piper/{name}", REPO_ROOT / "modelos_f5/piper" / name, f"{PIPER_BASE_URL}/{remote_dir}/{name}"))
    for name, remote in (("model.onnx", "onnx/model.onnx"), ("tokenizer.json", "tokenizer.json"), ("config.json", "config.json")):
        assets.append((f"modelos_f5/minilm_l12_onnx/{name}", REPO_ROOT / "modelos_f5/minilm_l12_onnx" / name, f"{MINILM_BASE_URL}/{remote}"))
    for relative, local, url in assets:
        cached = local if local.is_file() else resources / relative
        _download_or_copy(cached, staged / relative, url, offline=offline)
    # Keep explicitly staged avatar assets, never an old runtime or unknown blobs.
    for avatar in sorted((resources / "vrm").glob("*.vrm")):
        _contained(avatar, resources)
        (staged / "vrm").mkdir(exist_ok=True)
        shutil.copy2(avatar, staged / "vrm" / avatar.name)

    runtime = staged / "runtime"
    _extract_engine(engine_dest, runtime)
    for relative in ("logs", "data/editorial_cards", "data/memorias", "config"):
        (runtime / relative).mkdir(parents=True, exist_ok=True)
    profiles = REPO_ROOT / "opencohost/config/default_profiles.json"
    if profiles.is_file():
        shutil.copy2(profiles, runtime / "config/default_profiles.json")
    if not skip_python:
        _stage_standalone_python(runtime / "python", offline=offline, msvc_runtime_dir=runtime / "msvc-crt")
    inventory = {path.relative_to(staged).as_posix(): _file_record(path)
                 for path in sorted(staged.rglob("*")) if path.is_file()}
    (staged / "staging-inventory.json").write_text(json.dumps({"version": version, "files": inventory}, indent=2, sort_keys=True), encoding="utf-8")

    # All preparation/verification occurs before moving the previous usable stage.
    backup = _contained(backup_root / f"resources-{uuid.uuid4().hex}", backup_root)
    had_resources = resources.exists()
    if had_resources:
        resources.rename(backup)
    try:
        resources.parent.mkdir(parents=True, exist_ok=True)
        staged.rename(resources)
    except OSError:
        if had_resources and not resources.exists():
            backup.rename(resources)
        raise
    print(f"Staging complete; previous inventory preserved at {backup if had_resources else 'none'}")


def _ignore_python_junk(dirpath: str, contents: list[str]) -> set[str]:
    # A distribution provides interpreter/stdlib, not preinstalled developer packages.
    return {name for name in contents if name in {"site-packages", "Scripts", "__pycache__", ".git", ".pytest_cache", ".lock", "EXTERNALLY-MANAGED"}
            or name.endswith((".pyc", ".pyo"))}


def _normalized_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _stage_standalone_python(python_dest: Path, *, offline: bool = False,
                             msvc_runtime_dir: Path | None = None) -> None:
    for filename in ("uv.lock", "pyproject.toml"):
        if not (REPO_ROOT / filename).is_file():
            raise FileNotFoundError(f"locked dependency input missing: {filename}")
    python_dest.parent.mkdir(parents=True, exist_ok=True)
    requirements = python_dest.parent / "dependency-lock.txt"
    resolved = python_dest.parent / "dependency-resolved.txt"
    uv = shutil.which("uv")
    if not uv:
        raise FileNotFoundError("cached uv executable is required")
    flags = ["--offline"] if offline else []
    export = [uv, "export", *flags, "--locked", "--no-dev", "--no-emit-project", "--no-header", "--no-annotate", "--format", "requirements-txt"]
    for extra in SUPPORTED_EXTRAS:
        export += ["--extra", extra]
    subprocess.run([*export, "--output-file", str(requirements)], cwd=REPO_ROOT, check=True)
    sources = [REPO_ROOT / "packaging/standalone_python",
               REPO_ROOT / "packaging/embedded_runtime/python/cpython-3.12.4-windows-x86_64-none"]
    source = next((path for path in sources if (path / "python.exe").is_file()), None)
    if source is None:
        cache = _contained(REPO_ROOT / "build_artifacts/staging-work" / f"interpreter-{uuid.uuid4().hex}", REPO_ROOT)
        if cache.is_relative_to(TAURI_RESOURCES.resolve()):
            raise ValueError("interpreter retrieval cache must remain outside bundled resources")
        subprocess.run([uv, "python", "install", *flags, "3.12.4", "--install-dir", str(cache)], check=True)
        source = cache / "cpython-3.12.4-windows-x86_64-none"
    if python_dest.exists():
        raise ValueError("standalone Python must be staged into a fresh directory")
    shutil.copytree(source, python_dest, ignore=_ignore_python_junk)
    python = python_dest / "python.exe"
    if not python.is_file():
        raise FileNotFoundError("cached standalone Python is incomplete")
    copy_msvc_crt_to_python(msvc_runtime_dir, python_dest)
    # Resolve universal lock markers for this exact interpreter, then hash-sync.
    subprocess.run([uv, "pip", "compile", *flags, "--python", str(python), "--generate-hashes", "--no-header", "--no-annotate",
                    str(requirements), "--output-file", str(resolved)], cwd=REPO_ROOT, check=True)
    subprocess.run([uv, "pip", "sync", *flags, "--require-hashes", "--no-build", "--python", str(python), str(resolved)], cwd=REPO_ROOT, check=True)
    result = subprocess.run([uv, "pip", "list", *flags, "--python", str(python), "--format", "json"], capture_output=True, text=True, check=True)
    installed = sorted(({"name": _normalized_name(item["name"]), "version": item["version"]} for item in json.loads(result.stdout)), key=lambda item: item["name"])
    expected = sorted(({"name": _normalized_name(name), "version": version}
                       for name, version in re.findall(r"^([\w.-]+)==([^\s;\\]+)", resolved.read_text(encoding="utf-8"), re.MULTILINE)), key=lambda item: item["name"])
    if not expected or installed != expected or any(item["name"] in {"opencohost", "torch", "transformers", "qwen-tts"} for item in installed):
        raise ValueError("staged Python inventory differs from the resolved approved dependency lock")
    subprocess.run([uv, "pip", "check", *flags, "--python", str(python)], check=True)
    (python_dest.parent / "python-packages.json").write_text(json.dumps(installed, indent=2), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", default="0.5.0")
    parser.add_argument("--engine-payload", type=Path)
    parser.add_argument("--skip-python", action="store_true", help="Source-only diagnostic stage; not a complete installer")
    parser.add_argument("--laya-onnx-dir", type=Path, help="Verified ONNX export for model-inclusive candidates")
    parser.add_argument("--msvc-runtime-dir", type=Path, help="Pinned Microsoft x64 VC CRT directory from the installed VC Redist tree")
    parser.add_argument("--msvc-crt-policy", choices=MSVC_CRT_POLICIES, default="exact",
                        help="exact: pinned hashes (default). signed: Microsoft-signed, version >= pinned, hashes recorded (CI)")
    parser.add_argument("--offline", action="store_true")
    args = parser.parse_args()
    stage_resources(args.version, args.engine_payload, args.skip_python, laya_onnx_dir=args.laya_onnx_dir,
                    offline=args.offline, msvc_runtime_dir=args.msvc_runtime_dir, msvc_crt_policy=args.msvc_crt_policy)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
