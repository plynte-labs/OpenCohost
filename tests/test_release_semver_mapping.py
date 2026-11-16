"""The release workflow maps PEP 440 and semver prereleases in both directions."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
import yaml

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "release.yml"


def _load_mapping_functions() -> dict:
    data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    for job in data["jobs"].values():
        for step in job.get("steps", []):
            run = step.get("run", "")
            if "def normalize_pep440" in run:
                marker = "<<'EOF'\n"
                body = run.split(marker, 1)[1].rsplit("\nEOF", 1)[0]
                source = textwrap.dedent(body)
                # Only the function definitions; the rest reads files and argv.
                head = source.split("tag = sys.argv", 1)[0]
                namespace: dict = {}
                exec(compile(head, "release.yml", "exec"), namespace)
                return namespace
    raise AssertionError("normalize_pep440 step not found in release.yml")


CASES = [
    ("0.5.0", "0.5.0"),
    ("0.5.0a1", "0.5.0-alpha.1"),
    ("0.5.0b2", "0.5.0-beta.2"),
    ("0.5.0rc3", "0.5.0-rc.3"),
    ("1.12.30rc10", "1.12.30-rc.10"),
]


@pytest.mark.parametrize("pep440, semver", CASES)
def test_pep440_to_semver(pep440, semver):
    assert _load_mapping_functions()["pep440_to_semver"](pep440) == semver


@pytest.mark.parametrize("pep440, semver", CASES)
def test_normalize_pep440(pep440, semver):
    assert _load_mapping_functions()["normalize_pep440"](semver) == pep440


@pytest.mark.parametrize("value", ["0.5.0-dev.1", "0.5.0-beta", "garbage"])
def test_unknown_forms_pass_through(value):
    fns = _load_mapping_functions()
    assert fns["normalize_pep440"](value) == value
    assert fns["pep440_to_semver"](value) == value
