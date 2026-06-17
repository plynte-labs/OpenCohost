"""Branding contract for the runtime logger: OpenCohost, not VoiceAI.

Guards the rebrand of the shared logger (name, log-file prefix, debug env var)
and the security-critical SensitiveDataFilter attachment that avatar/OBS modules
inherit by resolving the same configured logger.
"""
from __future__ import annotations

import logging
import os

import pytest

from opencohost.config import logger as logger_module
from opencohost.config.logger import SensitiveDataFilter, get_logger


def test_logger_is_named_opencohost():
    assert get_logger().name == "OpenCohost"
    assert logging.getLogger("OpenCohost") is get_logger()


def test_logger_keeps_sensitive_data_filter():
    assert any(isinstance(f, SensitiveDataFilter) for f in get_logger().filters)


def test_log_file_uses_opencohost_prefix():
    file_handlers = [h for h in get_logger().handlers if isinstance(h, logging.FileHandler)]
    assert file_handlers, "expected a FileHandler on the configured logger"
    names = [os.path.basename(h.baseFilename) for h in file_handlers]
    assert any(n.startswith("opencohost_") for n in names), names
    assert not any(n.startswith("voiceai_") for n in names), names


@pytest.mark.parametrize(
    "env,expected",
    [
        ({"OPENCOHOST_DEBUG": "1"}, True),
        ({"OPENCOHOST_DEBUG": "0"}, False),
        ({"VOICEAI_DEBUG": "1"}, False),  # legacy var intentionally removed
        ({}, False),
    ],
)
def test_debug_env_var(monkeypatch, env, expected):
    monkeypatch.delenv("OPENCOHOST_DEBUG", raising=False)
    monkeypatch.delenv("VOICEAI_DEBUG", raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    assert logger_module._debug_enabled() is expected


def test_avatar_and_obs_resolve_the_configured_logger():
    # The SensitiveDataFilter lives on the named instance; these modules must
    # resolve the SAME configured logger or they lose token redaction.
    from opencohost.avatar import obs_client

    assert obs_client.logger is get_logger()
    assert obs_client.logger.name == "OpenCohost"
