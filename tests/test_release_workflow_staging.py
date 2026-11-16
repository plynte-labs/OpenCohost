"""release.yml stages the signed MSVC CRT and a CI-exported Laya model."""
from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def _steps():
    data = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8"))
    return data["jobs"]["build-tauri-installer"]["steps"]


def test_release_workflow_passes_the_crt_and_laya_flags_to_staging():
    stage_step = next(s for s in _steps() if s.get("name") == "Stage bundled installer resources")
    run = stage_step["run"]
    assert "--msvc-runtime-dir" in run and "--msvc-crt-policy signed" in run
    assert "--laya-onnx-dir" in run


def test_release_workflow_discovers_the_crt_and_exports_laya_before_staging():
    names = [s.get("name") for s in _steps()]
    crt = names.index("Locate the Visual Studio MSVC runtime")
    laya = names.index("Export the Laya ONNX model")
    staging = names.index("Stage bundled installer resources")
    assert crt < staging and laya < staging
    laya_step = _steps()[laya]
    data = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8"))
    assert data["env"]["LAYA_REVISION"] == "7b928d828b7b0e022f929d9bd2e44165aa270148"
    assert "LAYA_REVISION" in laya_step["run"] and "export_laya_onnx.py" in laya_step["run"]
    cache = next(s for s in _steps() if str(s.get("uses", "")).startswith("actions/cache"))
    assert "laya" in cache["with"]["key"].lower() and cache["with"]["path"]
