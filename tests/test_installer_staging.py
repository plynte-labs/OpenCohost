"""Installer staging uses tiny fixtures, never owner models or Python installs."""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import zipfile

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("installer_staging", ROOT / "packaging/stage_installer_resources.py")
stage = importlib.util.module_from_spec(spec)
spec.loader.exec_module(stage)

MODEL_FILES = ("laya.onnx", "laya.onnx.data", "tokenizer.json", "tokenizer_config.json", "rl_agent_config.json")
CRT_FILES = (
    "concrt140.dll", "msvcp140.dll", "msvcp140_1.dll", "msvcp140_2.dll",
    "msvcp140_atomic_wait.dll", "msvcp140_codecvt_ids.dll", "vccorlib140.dll",
    "vcruntime140.dll", "vcruntime140_1.dll", "vcruntime140_threads.dll",
)


def crt_fixture(path, monkeypatch):
    path.mkdir(parents=True)
    hashes = {}
    for name in CRT_FILES:
        contents = f"trusted-x64-{name}".encode()
        (path / name).write_bytes(contents)
        hashes[name] = hashlib.sha256(contents).hexdigest()
    monkeypatch.setattr(stage, "MSVC_CRT_FILES", hashes)
    (path / "msvc-crt-manifest.json").write_text(json.dumps({
        "schema": 1, "publisher": "Microsoft Corporation", "architecture": "x64",
        "version": stage.MSVC_CRT_VERSION,
        "files": {name: {"size": (path / name).stat().st_size, "sha256": digest}
                  for name, digest in hashes.items()},
    }), encoding="utf-8")
    return path


def model_fixture(path):
    path.mkdir(parents=True)
    hashes = {}
    for name in MODEL_FILES:
        content = name.encode()
        (path / name).write_bytes(content)
        hashes[name] = hashlib.sha256(content).hexdigest()
    (path / "manifest.json").write_text(json.dumps({"format": 1, "files": hashes}), encoding="utf-8")
    return path


@pytest.mark.parametrize("damage", ["missing", "tampered", "extra_claim", "escape"])
def test_laya_staging_rejects_missing_or_tampered_member(tmp_path, damage):
    source = model_fixture(tmp_path / "model")
    if damage == "missing":
        (source / "laya.onnx.data").unlink()
    elif damage == "tampered":
        (source / "tokenizer.json").write_bytes(b"tampered")
    else:
        manifest = json.loads((source / "manifest.json").read_text())
        manifest["files"]["owner.db" if damage == "extra_claim" else "../owner.db"] = "0" * 64
        (source / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError):
        stage.stage_laya_assets(source, tmp_path / "resources")
    assert not (tmp_path / "resources/models/laya/multilingual").exists()


def test_laya_staging_copies_exact_allowlist(tmp_path):
    source = model_fixture(tmp_path / "model")
    (source / "owner.db").write_bytes(b"private")
    inventory = stage.stage_laya_assets(source, tmp_path / "resources")
    target = tmp_path / "resources/models/laya/multilingual"
    assert sorted(p.name for p in target.iterdir()) == sorted((*MODEL_FILES, "manifest.json"))
    assert inventory["laya.onnx"]["sha256"] == hashlib.sha256(b"laya.onnx").hexdigest()


def staging_fixture(tmp_path, monkeypatch):
    resources = tmp_path / "ui/resources"
    resources.mkdir(parents=True)
    (resources / "bootstrap-manifest.json").write_text(json.dumps({"engine": {}, "product_version": "old"}))
    (resources / "runtime/opencohost").mkdir(parents=True)
    (resources / "runtime/opencohost/removed.py").write_text("stale = True")
    (resources / "vrm").mkdir()
    (resources / "vrm/official.vrm").write_bytes(b"official")
    (resources / "unexpected.db").write_bytes(b"private")
    monkeypatch.setattr(stage, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(stage, "TAURI_RESOURCES", resources)
    monkeypatch.setattr(stage, "BOOTSTRAP_MANIFEST", resources / "bootstrap-manifest.json")
    for name in ("uv-0.11.6.zip", "piper/es_MX-claude-high.onnx", "piper/es_MX-claude-high.onnx.json",
                 "piper/en_US-kristin-medium.onnx", "piper/en_US-kristin-medium.onnx.json",
                 "modelos_f5/minilm_l12_onnx/model.onnx", "modelos_f5/minilm_l12_onnx/tokenizer.json", "modelos_f5/minilm_l12_onnx/config.json"):
        target = resources / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"cached")
    payload = tmp_path / "engine.zip"
    with zipfile.ZipFile(payload, "w") as archive:
        files = {"opencohost/__init__.py": b"__version__ = 'test'", "opencohost/current.py": b"current = True"}
        for name, content in files.items():
            archive.writestr(name, content)
        archive.writestr("payload-manifest.json", json.dumps({"schema": 1, "format": "zip", "files": [
            {"path": name, "size": len(content), "sha256": hashlib.sha256(content).hexdigest()}
            for name, content in files.items()]}))
    return resources, payload, model_fixture(tmp_path / "model")


def test_staging_replaces_inventory_without_old_modules(tmp_path, monkeypatch):
    resources, payload, model = staging_fixture(tmp_path, monkeypatch)
    stage.stage_resources("test", payload, skip_python=True, laya_onnx_dir=model, offline=True)
    assert (resources / "runtime/opencohost/current.py").is_file()
    assert not (resources / "runtime/opencohost/removed.py").exists()
    assert not (resources / "unexpected.db").exists()
    assert (resources / "vrm/official.vrm").read_bytes() == b"official"
    backups = list((tmp_path / "build_artifacts/staging-backups").iterdir())
    assert len(backups) == 1
    assert (backups[0] / "runtime/opencohost/removed.py").is_file()
    assert not any("staging-backups" in str(p) for p in resources.rglob("*"))


def test_staging_ships_both_piper_voices_with_per_voice_urls(tmp_path, monkeypatch):
    resources, payload, model = staging_fixture(tmp_path, monkeypatch)
    urls = {}
    real = stage._download_or_copy

    def spy(local, destination, url, *, offline=False):
        urls[destination.name] = url
        return real(local, destination, url, offline=offline)

    monkeypatch.setattr(stage, "_download_or_copy", spy)
    stage.stage_resources("test", payload, skip_python=True, laya_onnx_dir=model, offline=True)
    inventory = json.loads((resources / "staging-inventory.json").read_text())["files"]
    for name in ("es_MX-claude-high", "en_US-kristin-medium"):
        for suffix in (".onnx", ".onnx.json"):
            assert (resources / "piper" / f"{name}{suffix}").is_file()
            assert f"piper/{name}{suffix}" in inventory
    base = "https://huggingface.co/rhasspy/piper-voices/resolve/main"
    assert urls["es_MX-claude-high.onnx"] == f"{base}/es/es_MX/claude/high/es_MX-claude-high.onnx"
    assert urls["en_US-kristin-medium.onnx.json"] == f"{base}/en/en_US/kristin/medium/en_US-kristin-medium.onnx.json"


def test_every_configured_piper_voice_is_in_the_staged_set():
    """Contract: a voice the app can select must be a voice the installer ships."""
    from opencohost.config.settings import PIPER_VOICES, default_piper_voice_for_locale
    from opencohost.i18n.startup import load_registry

    staged = {stem + ".onnx" for stem, _ in stage.PIPER_VOICES}
    remote_dirs = dict(stage.PIPER_VOICES)
    for key, voice in PIPER_VOICES.items():
        assert voice["file"] in staged, f"PIPER_VOICES[{key!r}] ({voice['file']}) is not staged by the installer"
        assert remote_dirs[voice["file"][: -len(".onnx")]]
    for locale in {*load_registry().keys(), "es", "en"}:
        default = PIPER_VOICES[default_piper_voice_for_locale(locale)]["file"]
        assert default in staged, f"default voice for locale {locale!r} ({default}) is not staged"


def test_legacy_cli_stages_without_model_flag(tmp_path, monkeypatch):
    import sys

    resources, payload, _ = staging_fixture(tmp_path, monkeypatch)
    msvc_runtime = crt_fixture(tmp_path / "crt-source", monkeypatch)
    # Exercise the real CLI and staging, replacing only the heavy interpreter step.
    def tiny_python(target, *, offline=False, msvc_runtime_dir=None):
        target.mkdir(parents=True)
        (target / "python.exe").write_bytes(b"fixture")
        stage.copy_msvc_crt_to_python(msvc_runtime_dir, target)
    monkeypatch.setattr(stage, "_stage_standalone_python", tiny_python)
    monkeypatch.setattr(sys, "argv", ["stage_installer_resources.py", "--version", "test", "--engine-payload", str(payload),
                                       "--msvc-runtime-dir", str(msvc_runtime)])
    assert stage.main() == 0
    assert (resources / "runtime/opencohost/current.py").is_file()
    assert not (resources / "models/laya/multilingual").exists()
    assert not (resources / "runtime/opencohost/removed.py").exists()


@pytest.mark.parametrize("damage", ["missing", "tampered", "invalid_manifest"])
def test_explicit_cli_model_failure_preserves_previous_stage(tmp_path, monkeypatch, damage):
    import sys

    resources, payload, model = staging_fixture(tmp_path, monkeypatch)
    if damage == "missing":
        model = tmp_path / "absent-export"
    elif damage == "tampered":
        (model / "laya.onnx").write_bytes(b"tampered")
    else:
        (model / "manifest.json").write_text("not-json")
    monkeypatch.setattr(sys, "argv", ["stage_installer_resources.py", "--version", "test",
                                     "--engine-payload", str(payload), "--laya-onnx-dir", str(model), "--offline"])
    with pytest.raises(ValueError):
        stage.main()
    assert (resources / "runtime/opencohost/removed.py").is_file()


def test_failed_staging_preserves_previous_usable_inventory(tmp_path, monkeypatch):
    resources, payload, model = staging_fixture(tmp_path, monkeypatch)
    (model / "laya.onnx.data").unlink()
    with pytest.raises(ValueError):
        stage.stage_resources("test", payload, skip_python=True, laya_onnx_dir=model, offline=True)
    assert (resources / "runtime/opencohost/removed.py").is_file()
    assert (resources / "unexpected.db").read_bytes() == b"private"


def test_offline_resource_cache_miss_never_downloads(tmp_path, monkeypatch):
    def no_network(*args, **kwargs):
        pytest.fail("offline staging attempted network access")
    monkeypatch.setattr(stage.urllib.request, "urlopen", no_network)
    with pytest.raises(FileNotFoundError, match="offline"):
        stage._download_or_copy(tmp_path / "missing", tmp_path / "out", "https://invalid", offline=True)


def test_python_dependencies_not_accepted_by_fastapi_smoke_alone(tmp_path, monkeypatch):
    python = tmp_path / "python"
    python.mkdir()
    (python / "python.exe").write_bytes(b"not executed")
    monkeypatch.setattr(stage, "REPO_ROOT", tmp_path)
    # An already functional interpreter must not bypass a missing locked inventory.
    monkeypatch.setattr(stage.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="ok"))
    with pytest.raises((FileNotFoundError, ValueError)):
        stage._stage_standalone_python(python, offline=True)


def test_python_sync_records_and_checks_exact_locked_inventory(tmp_path, monkeypatch):
    monkeypatch.setattr(stage, "REPO_ROOT", tmp_path)
    (tmp_path / "uv.lock").write_text("version = 1")
    (tmp_path / "pyproject.toml").write_text("[project]\nname='opencohost'")
    msvc_runtime = crt_fixture(tmp_path / "crt-source", monkeypatch)
    source = tmp_path / "packaging/standalone_python"
    source.mkdir(parents=True)
    (source / "python.exe").write_bytes(b"fake interpreter")
    old = source / "Lib/site-packages/torch"
    old.mkdir(parents=True)
    (old / "owner-training.py").write_text("private")
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        if "export" in command or "compile" in command:
            Path(command[command.index("--output-file") + 1]).write_text("fastapi==1.0 \\\n    --hash=sha256:" + "0" * 64 + "\n")
        if "list" in command:
            return SimpleNamespace(returncode=0, stdout='[{"name":"fastapi","version":"1.0"}]')
        return SimpleNamespace(returncode=0, stdout="")

    monkeypatch.setattr(stage.subprocess, "run", run)
    target = tmp_path / "work/runtime/python"
    stage._stage_standalone_python(target, offline=True, msvc_runtime_dir=msvc_runtime)
    assert not (target / "Lib/site-packages/torch").exists()
    assert json.loads((target.parent / "python-packages.json").read_text()) == [{"name": "fastapi", "version": "1.0"}]
    export = next(command for command in calls if "export" in command)
    assert "--locked" in export and "--no-emit-project" in export and "--offline" in export
    assert [export[i + 1] for i, value in enumerate(export) if value == "--extra"] == ["local-tts", "cloud-tts", "integrations", "vision"]
    sync = next(command for command in calls if "sync" in command)
    assert "--require-hashes" in sync and "--offline" in sync
    assert all((target / name).is_file() for name in CRT_FILES)


def test_staging_rejects_zip_escape_and_preserves_old_stage(tmp_path, monkeypatch):
    resources, payload, model = staging_fixture(tmp_path, monkeypatch)
    with zipfile.ZipFile(payload, "a") as archive:
        archive.writestr("../escaped.py", "bad")
    with pytest.raises(ValueError, match="payload"):
        stage.stage_resources("test", payload, skip_python=True, laya_onnx_dir=model, offline=True)
    assert (resources / "runtime/opencohost/removed.py").is_file()
    assert not (resources.parent / "escaped.py").exists()


def test_payload_hash_mismatch_prevents_promotion(tmp_path, monkeypatch):
    resources, payload, model = staging_fixture(tmp_path, monkeypatch)
    with zipfile.ZipFile(payload, "a") as archive:
        archive.writestr("opencohost/undeclared.py", "unexpected")
    with pytest.raises(ValueError, match="payload"):
        stage.stage_resources("test", payload, skip_python=True, laya_onnx_dir=model, offline=True)
    assert (resources / "runtime/opencohost/removed.py").is_file()


def test_msvc_crt_staging_records_provenance_and_coherent_hashes(tmp_path, monkeypatch):
    source = crt_fixture(tmp_path / "crt-source", monkeypatch)
    target = tmp_path / "resources/runtime/msvc-crt"

    manifest = stage.stage_msvc_crt(source, target)

    assert sorted(path.name for path in target.glob("*.dll")) == sorted(CRT_FILES)
    assert manifest["publisher"] == "Microsoft Corporation"
    assert manifest["architecture"] == "x64"
    assert manifest["version"] == "14.50.35719.0"
    assert set(manifest["files"]) == set(CRT_FILES)
    assert all(manifest["files"][name]["sha256"] == stage._compute_sha256(target / name) for name in CRT_FILES)


@pytest.mark.parametrize("damage", ["missing", "mixed_architecture", "extra_runtime"])
def test_msvc_crt_staging_rejects_incomplete_or_mixed_family(tmp_path, monkeypatch, damage):
    source = crt_fixture(tmp_path / "crt-source", monkeypatch)
    if damage == "missing":
        (source / "msvcp140_2.dll").unlink()
    elif damage == "mixed_architecture":
        (source / "vcruntime140.dll").write_bytes(b"x86 runtime")
    else:
        (source / "unrecognized.dll").write_bytes(b"extra")

    with pytest.raises(ValueError):
        stage.stage_msvc_crt(source, tmp_path / "resources/runtime/msvc-crt")


def test_python_root_replaces_only_the_known_older_vcruntime_pair(tmp_path, monkeypatch):
    source = crt_fixture(tmp_path / "crt-source", monkeypatch)
    target = tmp_path / "python"
    target.mkdir()
    old_pair = {"vcruntime140.dll": b"old-runtime", "vcruntime140_1.dll": b"old-runtime-1"}
    old_hashes = {name: hashlib.sha256(content).hexdigest() for name, content in old_pair.items()}
    monkeypatch.setattr(stage, "KNOWN_PYTHON_CRT_SHA256", old_hashes)
    for name, content in old_pair.items():
        (target / name).write_bytes(content)

    stage.copy_msvc_crt_to_python(source, target)

    assert all(stage._compute_sha256(target / name) == stage.MSVC_CRT_FILES[name] for name in old_pair)


def test_known_previous_python_crt_hashes_match_checked_in_cpython_distribution():
    python_source = ROOT / "packaging/standalone_python"
    assert set(stage.KNOWN_PYTHON_CRT_SHA256) == {"vcruntime140.dll", "vcruntime140_1.dll"}
    if not python_source.is_dir():
        pytest.skip("standalone_python not yet staged")
    for name, expected in stage.KNOWN_PYTHON_CRT_SHA256.items():
        assert stage._compute_sha256(python_source / name) == expected


def test_python_root_refuses_unknown_native_runtime_collision(tmp_path, monkeypatch):
    source = crt_fixture(tmp_path / "crt-source", monkeypatch)
    target = tmp_path / "python"
    target.mkdir()
    (target / "msvcp140.dll").write_bytes(b"user-owned")

    with pytest.raises(ValueError, match="refusing to overwrite"):
        stage.copy_msvc_crt_to_python(source, target)

    assert (target / "msvcp140.dll").read_bytes() == b"user-owned"


def test_msvc_crt_is_mandatory_for_complete_staging_and_copied_beside_python(tmp_path, monkeypatch):
    resources, payload, model = staging_fixture(tmp_path, monkeypatch)
    source = crt_fixture(tmp_path / "crt-source", monkeypatch)

    def tiny_python(target, *, offline=False, msvc_runtime_dir=None):
        target.mkdir(parents=True)
        (target / "python.exe").write_bytes(b"fixture")
        stage.copy_msvc_crt_to_python(msvc_runtime_dir, target)

    monkeypatch.setattr(stage, "_stage_standalone_python", tiny_python)
    with pytest.raises(ValueError, match="MSVC"):
        stage.stage_resources("test", payload, laya_onnx_dir=model, offline=True)

    stage.stage_resources("test", payload, laya_onnx_dir=model, offline=True, msvc_runtime_dir=source)
    python_root = resources / "runtime/python"
    assert all((python_root / name).is_file() for name in CRT_FILES)
    assert (resources / "runtime/msvc-crt/msvc-crt-manifest.json").is_file()
    inventory = json.loads((resources / "staging-inventory.json").read_text())
    assert {f"runtime/msvc-crt/{name}" for name in (*CRT_FILES, "msvc-crt-manifest.json")} <= set(inventory["files"])
    assert {f"runtime/python/{name}" for name in CRT_FILES} <= set(inventory["files"])
    assert (resources / "vrm/official.vrm").read_bytes() == b"official"


def test_laya_changed_after_verification_cannot_be_promoted(tmp_path, monkeypatch):
    source = model_fixture(tmp_path / "model")
    copy = stage.shutil.copy2
    def racing_copy(src, dest, *args, **kwargs):
        if Path(src).name == "laya.onnx":
            Path(src).write_bytes(b"changed after verification")
        return copy(src, dest, *args, **kwargs)
    monkeypatch.setattr(stage.shutil, "copy2", racing_copy)
    with pytest.raises(ValueError, match="changed"):
        stage.stage_laya_assets(source, tmp_path / "resources")
