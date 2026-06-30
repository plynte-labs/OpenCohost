"""Smoke test for the real-env infra: proves the gate skips by default.

With OPENCOHOST_REALENV_TESTS unset this is SKIPPED at setup (body never runs,
no Ollama call, no model load). With the flag set it runs and only checks that
the shared helpers import — still no model load.
"""
import pytest

pytestmark = pytest.mark.realenv


def test_realenv_gate_lets_through_and_helpers_import():
    from tests.realenv import _helpers
    assert callable(_helpers.require_ollama)
    assert callable(_helpers.require_model)
    assert callable(_helpers.run_bounded)
