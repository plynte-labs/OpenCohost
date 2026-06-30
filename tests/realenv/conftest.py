"""Gate for real-env tests: opt-in, never part of the default fast suite."""
from __future__ import annotations
import os
import pytest

REALENV_ENV_FLAG = "OPENCOHOST_REALENV_TESTS"


@pytest.fixture(autouse=True)
def _realenv_gate():
    if os.environ.get(REALENV_ENV_FLAG) != "1":
        pytest.skip(f"real-env test; set {REALENV_ENV_FLAG}=1 to run")
    yield
