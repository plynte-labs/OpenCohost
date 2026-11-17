"""Pytest runs must never write session logs into the repository ``logs/`` folder.

The session log handler is opened when ``opencohost.config.logger`` is first
imported, so the redirection has to be in place before that import. The
``tests/conftest.py`` module sets ``OPENCOHOST_LOG_DIR`` to a per-run temporary
directory at load time; these tests prove the handler actually picked it up.
"""

import os
from pathlib import Path

from opencohost.config import logger as logger_mod

REPO_LOGS_DIR = (Path(__file__).resolve().parent.parent / "logs").resolve()


def _is_within(path: str, root: Path) -> bool:
    try:
        Path(path).resolve().relative_to(root)
    except ValueError:
        return False
    return True


def test_session_log_handler_is_outside_repository_logs():
    handler = logger_mod.file_handler

    assert not _is_within(handler.baseFilename, REPO_LOGS_DIR), handler.baseFilename


def test_session_log_handler_lives_in_the_pytest_override_directory():
    override = os.environ.get("OPENCOHOST_LOG_DIR", "")

    assert override, "tests/conftest.py must set OPENCOHOST_LOG_DIR before the logger import"
    handler_dir = Path(logger_mod.file_handler.baseFilename).resolve().parent
    assert handler_dir == Path(override).resolve()


def test_log_dir_defaults_to_user_data_logs_without_override():
    from opencohost.config.settings import _resolve_log_dir

    assert _resolve_log_dir("", "/data/root") == os.path.join("/data/root", "logs")
    assert _resolve_log_dir("   ", "/data/root") == os.path.join("/data/root", "logs")


def test_log_dir_override_is_used_and_trimmed():
    from opencohost.config.settings import _resolve_log_dir

    assert _resolve_log_dir("  /scratch/logs  ", "/data/root") == "/scratch/logs"
