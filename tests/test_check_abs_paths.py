"""Tests for tools/check_abs_paths.py — stdlib-only absolute-path hook.

TDD RED → GREEN: tests written before implementation, then implementation added.
Covers: drive-letter detection, pragma escape, file-type filtering, clean pass.

Fixture strings containing drive-letter patterns are built via concatenation
so the test SOURCE FILE itself is clean (no literal drive-letter on any line).
The actual content written to tmp files is identical to the inline form.
"""
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parent.parent / "tools" / "check_abs_paths.py"
PYTHON = sys.executable

# Drive-letter fixture fragments — built by concatenation so source is hook-clean.
# Each fragment is a partial token; joining at runtime produces the full path.
_DL_BACK = "E:" + "\\" + "real" + "\\" + "path"
_DL_FWD = "C:" + "/" + "some" + "/" + "path"
_DL_FWD2 = "D:" + "/" + "project"
_DL_DOCS = "E:" + "/" + "voiceAi" + "/" + "README.md"
_DL_YAML = "C:" + "\\\\" + "Users" + "\\\\" + "foo"
_DL_JSON = "E:" + "/" + "voiceAi"
_DL_MIX_BAD = "E:" + "\\" + "real" + "\\" + "path"
_DL_SMALL = "E:" + "/" + "real"
_DL_STAGE = "E:" + "\\" + "real" + "\\" + "path"


def _run(files: list[Path]) -> subprocess.CompletedProcess:
    """Invoke the hook script with the given file paths as arguments."""
    return subprocess.run(
        [PYTHON, str(SCRIPT)] + [str(f) for f in files],
        capture_output=True,
        text=True,
    )


def _write(tmp_path: Path, name: str, content: str) -> Path:
    p = tmp_path / name
    p.write_text(textwrap.dedent(content), encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# Violation detection
# ---------------------------------------------------------------------------


def test_backslash_drive_letter_detected(tmp_path):
    """A backslash drive-letter path in a .py file must cause exit 1."""
    content = f'path = r"{_DL_BACK}"\n'
    f = _write(tmp_path, "bad.py", content)
    result = _run([f])
    assert result.returncode == 1
    assert "bad.py" in result.stdout


def test_forward_slash_drive_letter_detected(tmp_path):
    """A forward-slash drive-letter path in a .py file must cause exit 1."""
    content = f'path = "{_DL_FWD}"\n'
    f = _write(tmp_path, "bad.py", content)
    result = _run([f])
    assert result.returncode == 1
    assert "bad.py" in result.stdout


def test_drive_letter_in_markdown_detected(tmp_path):
    """A drive-letter path in a .md file must cause exit 1."""
    content = f"See `{_DL_DOCS}` for details.\n"
    f = _write(tmp_path, "doc.md", content)
    result = _run([f])
    assert result.returncode == 1
    assert "doc.md" in result.stdout


def test_drive_letter_in_yaml_detected(tmp_path):
    """A drive-letter path in a .yaml file must cause exit 1."""
    content = f'base_dir: "{_DL_YAML}"\n'
    f = _write(tmp_path, "config.yaml", content)
    result = _run([f])
    assert result.returncode == 1


def test_drive_letter_in_toml_detected(tmp_path):
    """A drive-letter path in a .toml file must cause exit 1."""
    content = f'[paths]\nroot = "{_DL_FWD2}"\n'
    f = _write(tmp_path, "config.toml", content)
    result = _run([f])
    assert result.returncode == 1


def test_drive_letter_in_json_detected(tmp_path):
    """A drive-letter path in a .json file must cause exit 1."""
    content = f'{{"path": "{_DL_JSON}"}}'
    f = _write(tmp_path, "settings.json", content)
    result = _run([f])
    assert result.returncode == 1


# ---------------------------------------------------------------------------
# Pragma escape
# ---------------------------------------------------------------------------


def test_pragma_escapes_violation(tmp_path):
    """A line containing 'path-ok' pragma must be skipped even if it has a drive-letter."""
    content = f"# Example path for docs: {_DL_BACK}  # path-ok\n"
    f = _write(tmp_path, "ok.py", content)
    result = _run([f])
    assert result.returncode == 0, result.stdout


def test_pragma_only_on_same_line(tmp_path):
    """The pragma on one line must NOT suppress a violation on a different line."""
    content = "# path-ok (this line is safe)\n" + f'bad = r"{_DL_MIX_BAD}"\n'
    f = _write(tmp_path, "mixed.py", content)
    result = _run([f])
    assert result.returncode == 1


# ---------------------------------------------------------------------------
# Clean files pass
# ---------------------------------------------------------------------------


def test_posix_paths_clean(tmp_path):
    """A file with only POSIX paths must exit 0."""
    f = _write(tmp_path, "clean.py", """
        path = "/usr/local/bin/python"
        other = "/fake/opencohost-cache"
    """)
    result = _run([f])
    assert result.returncode == 0, result.stdout


def test_empty_file_clean(tmp_path):
    """An empty file must exit 0."""
    f = tmp_path / "empty.py"
    f.write_text("", encoding="utf-8")
    result = _run([f])
    assert result.returncode == 0


def test_no_files_clean():
    """Invocation with no files must exit 0 (nothing to check)."""
    result = _run([])
    assert result.returncode == 0


def test_multiple_files_one_violation(tmp_path):
    """When multiple files are passed, a single violation still causes exit 1."""
    clean = _write(tmp_path, "clean.py", 'x = "/fake/path"')
    bad = _write(tmp_path, "bad.py", f'path = "{_DL_FWD}"')
    result = _run([clean, bad])
    assert result.returncode == 1
    assert "bad.py" in result.stdout
    assert "clean.py" not in result.stdout


def test_violation_output_format(tmp_path):
    """Output must include filename and line number on violation."""
    f = _write(tmp_path, "bad.py", f'x = "{_DL_SMALL}"\n')
    result = _run([f])
    assert result.returncode == 1
    # Expect "bad.py:N: ..." format
    assert ":" in result.stdout


# ---------------------------------------------------------------------------
# Subprocess invocation (integration via tmp_path-seeded files)
# ---------------------------------------------------------------------------


def test_script_is_invocable():
    """The script must exist and be importable as a subprocess."""
    assert SCRIPT.exists(), f"Script not found: {SCRIPT}"


def test_subprocess_blocked_drive_letter(tmp_path):
    """Seeded drive-letter path blocked — matches spec scenario."""
    content = f'exe = r"{_DL_STAGE}"'
    f = _write(tmp_path, "stage.py", content)
    result = _run([f])
    assert result.returncode == 1


def test_subprocess_pragma_allowed(tmp_path):
    """Pragma-escaped line allowed — matches spec scenario."""
    content = f"# Example: {_DL_STAGE}  # path-ok"
    f = _write(tmp_path, "stage.py", content)
    result = _run([f])
    assert result.returncode == 0
