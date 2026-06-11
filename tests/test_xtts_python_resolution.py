"""Tests for XTTS_PYTHON resolution via env var, storage.yaml, and graceful degradation.

Requirement: Regression Safety (spec requirement 6).
Design: D1 — resolve_xtts_python() in config/storage.py.

Resolution order:
  1. env XTTS_PYTHON → wins if set and non-empty
  2. storage.yaml tools.xtts_python → used if not "auto" and not empty
  3. None → QwenProcessManager.start() degrades gracefully (LIFECYCLE_FAILED, no Popen)
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from opencohost.config.storage import resolve_xtts_python
from opencohost.core.health_monitor import LIFECYCLE_FAILED, QwenProcessManager


# ──────────────────────────────────────────────
# resolve_xtts_python — unit tests
# ──────────────────────────────────────────────


class TestResolveXttsPython:
    """resolve_xtts_python() resolution order: env → yaml → None."""

    def test_env_var_wins_over_yaml(self, tmp_path):
        """XTTS_PYTHON env var takes highest priority."""
        yaml_cfg = tmp_path / "storage.yaml"
        yaml_cfg.write_text(
            "storage:\n  cache_root: auto\ntools:\n  xtts_python: /yaml/python\n",
            encoding="utf-8",
        )

        with patch.dict(os.environ, {"XTTS_PYTHON": "/env/python3"}, clear=False):
            result = resolve_xtts_python(config_file=yaml_cfg)

        assert result == Path("/env/python3")

    def test_yaml_fallback_when_env_absent(self, tmp_path):
        """When XTTS_PYTHON is not set, use tools.xtts_python from storage.yaml."""
        yaml_cfg = tmp_path / "storage.yaml"
        yaml_cfg.write_text(
            "tools:\n  xtts_python: /fake/miniconda/python\n",
            encoding="utf-8",
        )

        env = {k: v for k, v in os.environ.items() if k != "XTTS_PYTHON"}
        with patch.dict(os.environ, env, clear=True):
            result = resolve_xtts_python(config_file=yaml_cfg)

        assert result == Path("/fake/miniconda/python")

    def test_returns_none_when_env_absent_and_yaml_auto(self, tmp_path):
        """'auto' in tools.xtts_python is treated as unset — returns None."""
        yaml_cfg = tmp_path / "storage.yaml"
        yaml_cfg.write_text(
            "tools:\n  xtts_python: auto\n",
            encoding="utf-8",
        )

        env = {k: v for k, v in os.environ.items() if k != "XTTS_PYTHON"}
        with patch.dict(os.environ, env, clear=True):
            result = resolve_xtts_python(config_file=yaml_cfg)

        assert result is None

    def test_returns_none_when_env_absent_and_yaml_missing_key(self, tmp_path):
        """Missing tools.xtts_python key in yaml returns None."""
        yaml_cfg = tmp_path / "storage.yaml"
        yaml_cfg.write_text(
            "storage:\n  cache_root: auto\n",
            encoding="utf-8",
        )

        env = {k: v for k, v in os.environ.items() if k != "XTTS_PYTHON"}
        with patch.dict(os.environ, env, clear=True):
            result = resolve_xtts_python(config_file=yaml_cfg)

        assert result is None

    def test_returns_none_when_env_absent_and_no_yaml(self, tmp_path):
        """Returns None when config file does not exist."""
        nonexistent = tmp_path / "no_such.yaml"

        env = {k: v for k, v in os.environ.items() if k != "XTTS_PYTHON"}
        with patch.dict(os.environ, env, clear=True):
            result = resolve_xtts_python(config_file=nonexistent)

        assert result is None

    def test_env_empty_string_treated_as_unset(self, tmp_path):
        """Empty XTTS_PYTHON env var falls through to yaml lookup."""
        yaml_cfg = tmp_path / "storage.yaml"
        yaml_cfg.write_text(
            "tools:\n  xtts_python: /fake/yaml/python\n",
            encoding="utf-8",
        )

        with patch.dict(os.environ, {"XTTS_PYTHON": ""}, clear=False):
            result = resolve_xtts_python(config_file=yaml_cfg)

        assert result == Path("/fake/yaml/python")

    def test_result_is_path_not_string(self, tmp_path):
        """resolve_xtts_python always returns Path or None, never a raw str."""
        yaml_cfg = tmp_path / "storage.yaml"
        yaml_cfg.write_text("tools:\n  xtts_python: /fake/python\n", encoding="utf-8")

        env = {k: v for k, v in os.environ.items() if k != "XTTS_PYTHON"}
        with patch.dict(os.environ, env, clear=True):
            result = resolve_xtts_python(config_file=yaml_cfg)

        assert isinstance(result, Path)


# ──────────────────────────────────────────────
# QwenProcessManager.start() — degradation when resolution returns None
# ──────────────────────────────────────────────


class TestQwenDegradationWhenNoPython:
    """start() degrades gracefully (LIFECYCLE_FAILED, return False) when
    XTTS_PYTHON resolution yields None. Popen must never be called."""

    def test_start_returns_false_when_python_unresolved(self, tmp_path):
        """start() returns False without raising when xtts_python is None."""
        yaml_cfg = tmp_path / "storage.yaml"
        yaml_cfg.write_text("tools:\n  xtts_python: auto\n", encoding="utf-8")

        env = {k: v for k, v in os.environ.items() if k != "XTTS_PYTHON"}
        with patch.dict(os.environ, env, clear=True):
            with patch("opencohost.config.storage.STORAGE_CONFIG_FILE", yaml_cfg):
                mgr = QwenProcessManager()
                with patch("opencohost.core.health_monitor.subprocess.Popen") as mock_popen:
                    result = mgr.start()

        assert result is False
        mock_popen.assert_not_called()

    def test_start_sets_lifecycle_failed_when_python_unresolved(self, tmp_path):
        """start() sets LIFECYCLE_FAILED state when xtts_python is None."""
        yaml_cfg = tmp_path / "storage.yaml"
        yaml_cfg.write_text("tools:\n  xtts_python: auto\n", encoding="utf-8")

        env = {k: v for k, v in os.environ.items() if k != "XTTS_PYTHON"}
        with patch.dict(os.environ, env, clear=True):
            with patch("opencohost.config.storage.STORAGE_CONFIG_FILE", yaml_cfg):
                mgr = QwenProcessManager()
                mgr.start()

        assert mgr._lifecycle_state == LIFECYCLE_FAILED

    def test_start_does_not_raise_when_python_unresolved(self, tmp_path):
        """start() never raises — mirrors PiperEngine.load() never-raise contract."""
        yaml_cfg = tmp_path / "storage.yaml"
        yaml_cfg.write_text("tools:\n  xtts_python: auto\n", encoding="utf-8")

        env = {k: v for k, v in os.environ.items() if k != "XTTS_PYTHON"}
        with patch.dict(os.environ, env, clear=True):
            with patch("opencohost.config.storage.STORAGE_CONFIG_FILE", yaml_cfg):
                mgr = QwenProcessManager()
                try:
                    mgr.start()
                except Exception as exc:
                    pytest.fail(f"start() raised unexpectedly: {exc}")

    def test_start_logs_actionable_warning_when_python_unresolved(self, tmp_path):
        """start() logs a warning with remediation hint when xtts_python is None."""
        yaml_cfg = tmp_path / "storage.yaml"
        yaml_cfg.write_text("tools:\n  xtts_python: auto\n", encoding="utf-8")

        env = {k: v for k, v in os.environ.items() if k != "XTTS_PYTHON"}
        with patch.dict(os.environ, env, clear=True):
            with patch("opencohost.config.storage.STORAGE_CONFIG_FILE", yaml_cfg):
                mgr = QwenProcessManager()
                with patch("opencohost.core.health_monitor.logger") as mock_logger:
                    mgr.start()

        # At least one warning should mention how to fix the problem
        all_warn_calls = " ".join(
            str(call) for call in mock_logger.warning.call_args_list
        )
        assert "XTTS_PYTHON" in all_warn_calls or "xtts_python" in all_warn_calls

    def test_start_uses_env_python_when_set(self, tmp_path):
        """When XTTS_PYTHON env is set, start() uses it and reaches Popen."""
        yaml_cfg = tmp_path / "storage.yaml"
        yaml_cfg.write_text("tools:\n  xtts_python: auto\n", encoding="utf-8")

        with patch.dict(os.environ, {"XTTS_PYTHON": "/fake/python"}, clear=False):
            with patch("opencohost.config.storage.STORAGE_CONFIG_FILE", yaml_cfg):
                mgr = QwenProcessManager()
                with patch("opencohost.core.health_monitor.LOG_DIR", str(tmp_path)):
                    with patch("opencohost.core.health_monitor.subprocess.Popen") as mock_popen:
                        with patch.object(mgr, "_check_health", return_value=True):
                            mock_popen.return_value.poll.return_value = None
                            result = mgr.start()

        assert result is True
        call_args = mock_popen.call_args
        cmd = call_args.args[0] if call_args.args else call_args[0][0]
        assert Path(cmd[0]) == Path("/fake/python")
