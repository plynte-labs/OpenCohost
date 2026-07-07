"""Shared pytest fixtures for VoiceAI tests."""

import os
import sys
import tempfile
from unittest.mock import MagicMock, patch

import pytest

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)


@pytest.fixture(autouse=True)
def _neutralize_obs_runtime():
    """FIX-B: keep a real ``EngineHost.start()`` from opening a REAL OBS socket.

    The API-hosted engine now constructs an ``ObsRuntime`` that reads the
    machine's ``avatar.yaml`` and, when OBS is enabled, connects to a live OBS
    instance (and can push image-source updates). Any test that drives a real
    ``EngineHost.start()`` would otherwise attempt that websocket and could
    mutate a developer's running OBS scene. Replace it with an inert stub by
    default.

    This never masks the FIX-B coverage: ``ObsRuntime``'s own unit tests import
    the class directly from ``opencohost.avatar.obs_runtime`` (unaffected), and
    the EngineHost-wiring tests monkeypatch ``engine_host.ObsRuntime`` themselves
    inside the test body — that patch runs after this fixture and wins.
    """
    import opencohost.api.engine_host as engine_host_mod

    class _InertObsRuntime:
        def __init__(self, *args, **kwargs):
            pass

        def start_from_config(self):
            return False

        def apply_config(self):
            return False

        def handle_motor_event(self, event):
            pass

        def stop(self):
            pass

        @property
        def is_connected(self):
            return False

    with patch.object(engine_host_mod, "ObsRuntime", _InertObsRuntime):
        yield


@pytest.fixture(autouse=True)
def _isolate_api_tokens_file(tmp_path, monkeypatch):
    """Keep API token minting out of the developer's real config/ directory.

    create_app()'s lifespan mints ``USER_DATA_DIR/config/api_tokens.json`` at
    startup (agent_context_gateway Phase 1). In dev mode USER_DATA_DIR is the
    repo root, so any test entering a TestClient context would otherwise write
    a real token file into the repo-root config/ directory. Redirect to a
    per-test temp path; opencohost/api/auth.py reads settings.API_TOKENS_FILE
    lazily, so monkeypatching the module attribute is sufficient.
    """
    from opencohost.config import settings as settings_mod

    monkeypatch.setattr(
        settings_mod, "API_TOKENS_FILE", str(tmp_path / "api_tokens.json")
    )


@pytest.fixture(scope="session")
def root_dir():
    """Return the project root directory."""
    return ROOT_DIR


@pytest.fixture(scope="session")
def config_dir(root_dir):
    """Return the config directory path."""
    return os.path.join(root_dir, "opencohost", "config")


@pytest.fixture(scope="session")
def smart_aggregator_config(config_dir):
    """Load the smart_aggregator.yaml config."""
    import yaml

    config_path = os.path.join(config_dir, "smart_aggregator.yaml")
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


@pytest.fixture
def mock_ollama():
    """Mock Ollama API responses."""
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "model": "qwen3-tts",
        "message": {"role": "assistant", "content": "Mock response"},
        "done": True,
    }
    with patch("requests.post", return_value=mock_response) as mock_post:
        yield mock_post


@pytest.fixture
def mock_tts_server():
    """Mock TTS server (Flask on port 5000)."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.content = b"mock-audio-data"
    with patch("requests.post", return_value=mock_response) as mock_post:
        yield mock_post


@pytest.fixture
def mock_websocket():
    """Mock WebSocket connection to LiveAudio."""
    mock_ws = MagicMock()
    mock_ws.connected = True
    mock_ws.recv.return_value = '{"type": "audio", "data": "mock"}'
    with patch("websocket.create_connection", return_value=mock_ws) as mock_create:
        yield mock_create


@pytest.fixture
def temp_dir():
    """Provide a temporary directory that is cleaned up after the test."""
    with tempfile.TemporaryDirectory(prefix="voiceai_test_") as tmp:
        yield tmp


@pytest.fixture
def mock_logger():
    """Mock logger to avoid file I/O during tests."""
    mock = MagicMock()
    with patch("logging.getLogger", return_value=mock):
        yield mock


@pytest.fixture
def mock_llm():
    """Mock LLM interface for vibe thermometer testing."""
    def llm_mock(prompt):
        return {
            "emotions": {
                "excitement": 0.7,
                "neutral": 0.2,
                "sadness": 0.1,
                "anger": 0.0,
                "joy": 0.0,
                "confusion": 0.0,
            },
            "temperature": 75,
        }
    return llm_mock
