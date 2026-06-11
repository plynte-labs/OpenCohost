"""Shared pytest fixtures for VoiceAI tests."""

import os
import sys
import tempfile
from unittest.mock import MagicMock, patch

import pytest

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)


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
