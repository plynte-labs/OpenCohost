"""Tests for tools/check_abs_paths.py — stdlib-only absolute-path hook.

TDD RED phase: tests written before implementation exists.
Covers: drive-letter detection, pragma escape, file-type filtering, clean pass.
"""
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parent.parent / "tools" / "check_abs_paths.py"
PYTHON = sys.executable


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
    f = _write(tmp_path, "bad.py", r"""
        path = r"E:\real\path"
    """)
    result = _run([f])
    assert result.returncode == 1
    assert "bad.py" in result.stdout


def test_forward_slash_drive_letter_detected(tmp_path):
    """A forward-slash drive-letter path in a .py file must cause exit 1."""
    f = _write(tmp_path, "bad.py", """
        path = "C:/some/path"
    """)
    result = _run([f])
    assert result.returncode == 1
    assert "bad.py" in result.stdout


def test_drive_letter_in_markdown_detected(tmp_path):
    """A drive-letter path in a .md file must cause exit 1."""
    f = _write(tmp_path, "doc.md", """
        See `E:/voiceAi/README.md` for details.
    """)
    result = _run([f])
    assert result.returncode == 1
    assert "doc.md" in result.stdout


def test_drive_letter_in_yaml_detected(tmp_path):
    """A drive-letter path in a .yaml file must cause exit 1."""
    f = _write(tmp_path, "config.yaml", """
        base_dir: "C:\\\\Users\\\\foo"
    """)
    result = _run([f])
    assert result.returncode == 1


def test_drive_letter_in_toml_detected(tmp_path):
    """A drive-letter path in a .toml file must cause exit 1."""
    f = _write(tmp_path, "config.toml", """
        [paths]
        root = "D:/project"
    """)
    result = _run([f])
    assert result.returncode == 1


def test_drive_letter_in_json_detected(tmp_path):
    """A drive-letter path in a .json file must cause exit 1."""
    f = _write(tmp_path, "settings.json", '{"path": "E:/voiceAi"}')
    result = _run([f])
    assert result.returncode == 1


# ---------------------------------------------------------------------------
# Pragma escape
# ---------------------------------------------------------------------------


def test_pragma_escapes_violation(tmp_path):
    """A line containing 'path-ok' pragma must be skipped even if it has a drive-letter."""
    f = _write(tmp_path, "ok.py", r"""
        # Example path for docs: E:\real\path  # path-ok
    """)
    result = _run([f])
    assert result.returncode == 0, result.stdout


def test_pragma_only_on_same_line(tmp_path):
    """The pragma on one line must NOT suppress a violation on a different line."""
    f = _write(tmp_path, "mixed.py", r"""
        # path-ok (this line is safe)
        bad = r"E:\real\path"
    """)
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
    bad = _write(tmp_path, "bad.py", 'path = "C:/bad"')
    result = _run([clean, bad])
    assert result.returncode == 1
    assert "bad.py" in result.stdout
    assert "clean.py" not in result.stdout


def test_violation_output_format(tmp_path):
    """Output must include filename and line number on violation."""
    f = _write(tmp_path, "bad.py", 'x = "E:/real"\n')
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
    f = _write(tmp_path, "stage.py", r'exe = r"E:\real\path"')
    result = _run([f])
    assert result.returncode == 1


def test_subprocess_pragma_allowed(tmp_path):
    """Pragma-escaped line allowed — matches spec scenario."""
    f = _write(tmp_path, "stage.py", r'# Example: E:\docs\example  # path-ok')
    result = _run([f])
    assert result.returncode == 0
