"""Gate for live cloud tests: opt-in, real key, real network. Never in CI.

These tests exist to validate INTEGRATION, operational behavior and latency
against a real provider. They are NOT what proves the sanitizer algorithm
correct — ``tests/test_clause_sanitizer_e2e.py`` does that, deterministically,
in the mandatory suite.

Two hard rules enforced here:
  * The gate skips unless ``OPENCOHOST_LIVE_CLOUD_TESTS=1`` (mirrors
    tests/realenv/conftest.py).
  * Nothing in this package may print or assert on API keys, prompts, full
    model responses, RAG cards, or audio. Metadata only.
"""
from __future__ import annotations

import os
import queue
from unittest.mock import MagicMock

import pytest

LIVE_CLOUD_ENV_FLAG = "OPENCOHOST_LIVE_CLOUD_TESTS"
# Hard budget for the whole package, so an accidental run cannot spend freely.
MAX_CALLS_PER_TEST = 3
MAX_OUTPUT_TOKENS = 300
REQUEST_TIMEOUT_SECONDS = 60


@pytest.fixture(autouse=True)
def _live_cloud_gate():
    if os.environ.get(LIVE_CLOUD_ENV_FLAG) != "1":
        pytest.skip(f"live cloud test; set {LIVE_CLOUD_ENV_FLAG}=1 to run")
    yield


@pytest.fixture(autouse=True)
def _isolate_llm_provider_and_keys_files():
    """Deliberate override of the repo-wide autouse redirect.

    ``tests/conftest.py`` points ``LLM_KEYS_FILE`` and
    ``LLM_PROVIDER_CONFIG_FILE`` at ``tmp_path`` so no test can reach the
    owner's real cloud posture. These tests must reach it — that is the point —
    so the override is a no-op. Reading the real posture is allowed; printing
    any part of it is not.
    """
    yield


@pytest.fixture
def live_posture():
    """The operator's real provider posture, as the app itself loads it."""
    from opencohost.config.llm_provider import load_provider_config
    from opencohost.config.settings import LLM_KEYS_FILE
    from opencohost.stream_admin.oauth_store import OAuthStore

    config = load_provider_config()
    provider_id = config.get("active_provider")
    if not provider_id or provider_id == "ollama":
        pytest.skip("no cloud provider is active in the operator's posture")

    profile = (config.get("profiles") or {}).get(provider_id) or {}
    token = OAuthStore(LLM_KEYS_FILE).load(provider_id) or {}
    if not token.get("api_key"):
        pytest.skip(f"no api_key stored for provider {provider_id}")

    # Presence only. The value is never returned, logged, or asserted on.
    return {
        "provider_id": provider_id,
        "base_url": str(profile.get("base_url") or ""),
        "model": str(profile.get("model") or ""),
        "config": config,
    }


@pytest.fixture
def live_motor(live_posture, monkeypatch):
    """A real motor on the real posture, with audio mocked and hard caps set."""
    from opencohost.core import llm_engine as le
    from opencohost.core.llm_engine import MotorVocalIA

    monkeypatch.setattr(le, "CLOUD_MAX_TOKENS", MAX_OUTPUT_TOKENS)
    monkeypatch.setattr(le, "CLOUD_CHAT_TIMEOUT", REQUEST_TIMEOUT_SECONDS)

    spy = MagicMock()
    motor = MotorVocalIA(queue.Queue(), lambda event: None, dialogue_callback=spy)
    motor.pygame = MagicMock()
    motor.is_ready = True
    motor._hablar = MagicMock()          # no audio, no TTS server
    motor._provider_config = live_posture["config"]
    motor._dialogue_spy = spy
    return motor
