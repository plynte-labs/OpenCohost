"""Strict WU7-Core release workflow and version parity contract tests."""

from __future__ import annotations

import json
from pathlib import Path
import re
try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 (flux_env): tomllib arrived in 3.11
    import tomli as tomllib
import yaml
import pytest

ROOT = Path(__file__).resolve().parents[1]
RELEASE_YML = ROOT / ".github" / "workflows" / "release.yml"
PYPROJECT = ROOT / "pyproject.toml"
INIT_PY = ROOT / "opencohost" / "__init__.py"
TAURI_CONF = ROOT / "OpenCohost_UI" / "src-tauri" / "tauri.conf.json"
PACKAGE_JSON = ROOT / "OpenCohost_UI" / "package.json"
CI_YML = ROOT / ".github" / "workflows" / "ci.yml"


def _load_workflow(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _triggers(workflow: dict) -> dict:
    # PyYAML reads an unquoted `on:` key as the boolean True.
    return workflow.get("on", workflow.get(True, {}))


def _needs(job: dict) -> set:
    needs = job.get("needs", [])
    return {needs} if isinstance(needs, str) else set(needs)


def _step_with_action(job: dict, action_prefix: str) -> dict:
    return next(step for step in job["steps"] if str(step.get("uses", "")).startswith(action_prefix))


def _run_text(job: dict) -> str:
    return "\n".join(str(step["run"]) for step in job["steps"] if "run" in step)


def _get_pyproject_version() -> str:
    text = INIT_PY.read_text(encoding="utf-8")
    for line in text.splitlines():
        if line.startswith("__version__"):
            return line.split("=")[1].strip().strip('"').strip("'")
    raise ValueError("Could not find __version__ in opencohost/__init__.py")


def _normalize_to_semver(pep440: str) -> str:
    """Convert PEP 440 prerelease (e.g. '0.2.0a1') to semver (e.g. '0.2.0-alpha.1')."""
    m = re.match(r'^(\d+\.\d+\.\d+)(?:a(\d+))?$', pep440)
    if m and m.group(2):
        return f"{m.group(1)}-alpha.{m.group(2)}"
    return pep440


def test_version_parity_across_repository_components():
    py_version = _get_pyproject_version()
    semver_version = _normalize_to_semver(py_version)
    
    # Tauri conf version
    tauri_conf = json.loads(TAURI_CONF.read_text(encoding="utf-8"))
    assert tauri_conf["version"] == semver_version, (
        f"Tauri config version ({tauri_conf['version']}) does not match python version ({py_version} -> {semver_version})"
    )
    
    # UI package.json version
    package_json = json.loads(PACKAGE_JSON.read_text(encoding="utf-8"))
    assert package_json["version"] == semver_version, (
        f"UI package.json version ({package_json['version']}) does not match python version ({py_version} -> {semver_version})"
    )


def test_release_workflow_uses_tauri_nsis_and_submodules():
    assert RELEASE_YML.is_file(), "release.yml workflow must exist"
    content = RELEASE_YML.read_text(encoding="utf-8")
    data = yaml.safe_load(content)
    
    # Assert workflow trigger
    on_triggers = data.get("on", {})
    assert "push" in on_triggers or "workflow_dispatch" in on_triggers
    
    # Assert no obsolete PyInstaller or legacy launcher
    assert "pyinstaller" not in content.lower(), "release.yml must not use legacy PyInstaller"
    assert "launcher.spec" not in content.lower(), "release.yml must not reference launcher.spec"
    assert "miniconda" not in content.lower(), "release.yml must not reference miniconda"
    
    # Assert submodules checkout is recursive
    assert "submodules: recursive" in content or "submodules: 'recursive'" in content, (
        "release.yml checkout steps must specify submodules: recursive"
    )
    
    # Assert Tauri build and engine payload build
    assert "build_engine_payload.py" in content, "release.yml must build engine payload using build_engine_payload.py"
    assert "tauri build" in content or "tauri" in content, "release.yml must build the Tauri frontend and installer"
    assert "sha256" in content.lower(), "release.yml must compute SHA256 checksums for published artifacts"


def test_release_builds_wait_for_the_test_workflow():
    jobs = _load_workflow(RELEASE_YML)["jobs"]
    assert jobs["test"]["uses"] == "./.github/workflows/ci.yml", "release must call the CI workflow as its test gate"
    assert "test" in _needs(jobs["build-engine-payload"]), "the engine payload build must wait for the tests"
    assert "build-tauri-installer" in _needs(jobs["publish"]) and "build-engine-payload" in _needs(jobs["publish"])


def test_ci_workflow_is_reusable_and_runs_the_ui_gate():
    workflow = _load_workflow(CI_YML)
    assert "workflow_call" in _triggers(workflow), "ci.yml must be callable from release.yml"
    assert "pytest tests/" in _run_text(workflow["jobs"]["test"]), "the Python suite must stay in ci.yml"

    ui = workflow["jobs"]["ui"]
    assert ui["runs-on"] == "windows-latest"
    assert _step_with_action(ui, "actions/checkout@")["with"]["submodules"] == "recursive"
    assert str(_step_with_action(ui, "actions/setup-node@")["with"]["node-version"]) == "22"
    assert str(_step_with_action(ui, "pnpm/action-setup@")["with"]["version"]) == "11.5.2"
    run = _run_text(ui)
    assert "pnpm install --frozen-lockfile" in run
    # src/api/types.gen.ts is gitignored and generated: a clean checkout must generate it before tsc or vitest.
    assert "gen:api:offline" in run
    assert run.index("gen:api:offline") < run.index("pnpm exec tsc --noEmit -p .") < run.index("pnpm vitest run")
    for step in ui["steps"]:
        if "run" in step and "pnpm" in str(step["run"]):
            assert step.get("working-directory") == "OpenCohost_UI", step["run"]


def test_publish_stays_a_draft_and_prerelease_only_for_suffixed_versions():
    publish = _load_workflow(RELEASE_YML)["jobs"]["publish"]
    run = _run_text(publish)
    assert "--draft" in run, "the owner publishes manually: the release stays a draft"
    prerelease_lines = [line.strip() for line in run.splitlines() if "--prerelease" in line]
    assert prerelease_lines == ["PRERELEASE_ARGS=(--prerelease)"], "--prerelease must only come from the suffix guard"
    assert "*-alpha*|*-beta*|*-rc*)" in run, "only -alpha, -beta and -rc suffixes count as pre-releases"
    assert '"${PRERELEASE_ARGS[@]}"' in run
    assert 'TAG="v${VERSION}"' in run, "the tag must come from the version, never from a branch name"
