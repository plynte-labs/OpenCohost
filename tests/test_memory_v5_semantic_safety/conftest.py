"""Gate for semantic safety benchmark tests: opt-in, requires Python 3.10 and local model weights."""
from __future__ import annotations
import os
import pytest

SEMANTIC_SAFETY_ENV_FLAG = "OPENCOHOST_SEMANTIC_SAFETY_TESTS"


def pytest_ignore_collect(collection_path, config):
    """Ignore collection unless explicitly enabled via OPENCOHOST_SEMANTIC_SAFETY_TESTS=1."""
    if os.environ.get(SEMANTIC_SAFETY_ENV_FLAG) != "1":
        return True
    return False
