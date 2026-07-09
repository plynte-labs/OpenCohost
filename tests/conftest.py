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


@pytest.fixture(autouse=True)
def _isolate_piper_voice_file(tmp_path, monkeypatch):
    """Keep the persisted Piper-voice choice out of the developer's real
    config/ directory.

    MotorVocalIA.__init__ calls settings.load_piper_voice() unconditionally at
    construction (same repo-root-config/ hazard as api_tokens/personalization
    above) -- any real .../config/piper_voice.json on the developer's machine
    would otherwise leak into tests that pin a locale and construct a real
    MotorVocalIA (e.g. the P5 PIPER_VOICE_LOCALE_MISMATCH coherence checks).
    load_piper_voice() reads settings.PIPER_VOICE_FILE lazily at call time, so
    monkeypatching the module attribute is sufficient (same as API_TOKENS_FILE).
    """
    from opencohost.config import settings as settings_mod

    monkeypatch.setattr(
        settings_mod, "PIPER_VOICE_FILE", str(tmp_path / "piper_voice.json")
    )


@pytest.fixture(autouse=True)
def _isolate_personalization_file(tmp_path, monkeypatch):
    """Keep the personalization store out of the developer's real config/
    directory.

    PERSONALIZATION_ENABLED defaults to True and PERSONALIZATION_FILE
    resolves to ``USER_DATA_DIR/config/personalization.json``, which in dev
    mode is the repo root (same hazard as ``_isolate_api_tokens_file``
    above). Unlike ``settings.API_TOKENS_FILE`` (read lazily by auth.py),
    ``opencohost/core/personalization.py`` binds ``PERSONALIZATION_FILE`` via
    a from-import at module load time, so the module attribute itself must
    be patched — patching ``settings.PERSONALIZATION_FILE`` would have no
    effect.
    """
    import opencohost.core.personalization as personalization_mod

    monkeypatch.setattr(
        personalization_mod,
        "PERSONALIZATION_FILE",
        str(tmp_path / "personalization.json"),
    )


@pytest.fixture(autouse=True)
def _isolate_last_profile_and_model_files(tmp_path, monkeypatch):
    """Keep last_profile.json / last_model.json out of the developer's real
    config/ directory.

    ``save_last_profile``/``load_last_profile`` (settings.py) read
    ``settings.LAST_PROFILE_FILE`` at call time -- any test that hits
    /api/perfiles/switch (main.py calls save_last_profile on a successful
    switch) would otherwise clobber the real on-disk last-profile state (same
    repo-root-config/ hazard as API_TOKENS_FILE/PIPER_VOICE_FILE above).
    ``save_last_model``/``resolve_startup_model`` share the identical pattern
    via ``settings.LAST_MODEL_FILE``, isolated here too for the same reason.
    """
    from opencohost.config import settings as settings_mod

    monkeypatch.setattr(
        settings_mod, "LAST_PROFILE_FILE", str(tmp_path / "last_profile.json")
    )
    monkeypatch.setattr(
        settings_mod, "LAST_MODEL_FILE", str(tmp_path / "last_model.json")
    )


@pytest.fixture(autouse=True)
def _isolate_api_log_dir(tmp_path, monkeypatch):
    """Keep API-tree logging out of the developer's real ``logs/`` directory.

    ``create_app()``'s lifespan now calls ``setup_api_logging()`` and
    registers ``audit_middleware`` (api_observability_20260708 WU-C/WU-B),
    and ``EngineHost.__init__`` registers ``log_motor_accion`` into
    ``_motor_event_handlers`` (WU-A) -- all three resolve their target file
    from ``settings.LOG_DIR`` / ``settings.ACCIONES_LOG_FILE`` at call time
    (same lazy-read pattern as ``API_TOKENS_FILE`` etc. above), so any test
    that drives a real ``TestClient`` or a real ``EngineHost._dispatch_motor_event``
    without this isolation writes into the developer's real
    ``E:/VoiceAI/logs/`` (opencohost_api.log, api_audit.jsonl,
    acciones.jsonl) -- same repo-root hazard as the fixtures above.
    """
    from opencohost.config import settings as settings_mod

    monkeypatch.setattr(settings_mod, "LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setattr(
        settings_mod, "ACCIONES_LOG_FILE", str(tmp_path / "logs" / "acciones.jsonl")
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
