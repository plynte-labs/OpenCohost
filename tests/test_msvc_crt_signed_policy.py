"""The `signed` MSVC CRT policy verifies signature and version, then records exact hashes."""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("installer_staging_signed", ROOT / "packaging/stage_installer_resources.py")
stage = importlib.util.module_from_spec(spec)
spec.loader.exec_module(stage)

NAMES = tuple(stage.MSVC_CRT_FILES)
GOOD = {"status": "Valid", "subject": "CN=Microsoft Corporation, O=Microsoft Corporation, C=US", "version": "14.50.35800.0"}


def make_source(path: Path) -> Path:
    path.mkdir(parents=True)
    for name in NAMES:
        (path / name).write_bytes(f"runner-{name}".encode())
    return path


def inspector(overrides=None):
    overrides = overrides or {}
    return lambda p: {**GOOD, **overrides.get(p.name.lower(), {})}


def test_exact_policy_is_still_the_default(tmp_path):
    source = make_source(tmp_path / "src")
    with pytest.raises(ValueError):  # runner bytes do not match the pinned hashes
        stage.stage_msvc_crt(source, tmp_path / "dst")


def test_signed_policy_stages_and_records_hash_version_and_signer(tmp_path):
    source = make_source(tmp_path / "src")
    manifest = stage.stage_msvc_crt(source, tmp_path / "dst", policy="signed", inspector=inspector())
    assert manifest["policy"] == "signed"
    assert set(manifest["files"]) == set(NAMES)
    for name in NAMES:
        record = manifest["files"][name]
        assert record["sha256"] == hashlib.sha256(f"runner-{name}".encode()).hexdigest()
        assert record["version"] == "14.50.35800.0"
        assert "Microsoft" in record["signer"]
    on_disk = json.loads((tmp_path / "dst" / "msvc-crt-manifest.json").read_text(encoding="utf-8"))
    assert on_disk["files"] == manifest["files"]


@pytest.mark.parametrize("override, message", [
    ({"status": "NotSigned"}, "signature"),
    ({"status": "HashMismatch"}, "signature"),
    ({"subject": "CN=Someone Else"}, "signer"),
    ({"version": "14.50.35718.9"}, "version"),
    ({"version": "14.44.0.0"}, "version"),
    ({"version": ""}, "version"),
])
def test_signed_policy_rejects_bad_member(tmp_path, override, message):
    source = make_source(tmp_path / "src")
    with pytest.raises(ValueError, match=message):
        stage.stage_msvc_crt(source, tmp_path / "dst", policy="signed",
                             inspector=inspector({"vcruntime140.dll": override}))
    assert not (tmp_path / "dst").exists()


def test_signed_policy_rejects_missing_extra_and_debug(tmp_path):
    source = make_source(tmp_path / "missing")
    (source / "msvcp140_2.dll").unlink()
    with pytest.raises(ValueError):
        stage.stage_msvc_crt(source, tmp_path / "d1", policy="signed", inspector=inspector())
    source = make_source(tmp_path / "debug")
    (source / "msvcp140d.dll").write_bytes(b"debug")
    with pytest.raises(ValueError, match="debug"):
        stage.stage_msvc_crt(source, tmp_path / "d2", policy="signed", inspector=inspector())


def test_unknown_policy_is_rejected(tmp_path):
    source = make_source(tmp_path / "src")
    with pytest.raises(ValueError, match="policy"):
        stage.stage_msvc_crt(source, tmp_path / "dst", policy="whatever")


def test_signed_staged_family_is_accepted_by_the_python_copy_step(tmp_path):
    source = make_source(tmp_path / "src")
    stage.stage_msvc_crt(source, tmp_path / "dst", policy="signed", inspector=inspector())
    python = tmp_path / "python"
    python.mkdir()
    stage.copy_msvc_crt_to_python(tmp_path / "dst", python)
    assert all((python / name).is_file() for name in NAMES)


def test_signed_manifest_with_tampered_file_is_refused_by_the_python_copy_step(tmp_path):
    source = make_source(tmp_path / "src")
    stage.stage_msvc_crt(source, tmp_path / "dst", policy="signed", inspector=inspector())
    (tmp_path / "dst" / "vccorlib140.dll").write_bytes(b"tampered")
    python = tmp_path / "python"
    python.mkdir()
    with pytest.raises(ValueError):
        stage.copy_msvc_crt_to_python(tmp_path / "dst", python)


def test_cli_exposes_the_policy_flag():
    text = (ROOT / "packaging/stage_installer_resources.py").read_text(encoding="utf-8")
    assert "--msvc-crt-policy" in text and '"exact"' in text and '"signed"' in text
