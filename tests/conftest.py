"""Shared pytest fixtures for VoiceAI tests."""

import faulthandler
import logging
import os
import sys
import tempfile
from unittest.mock import MagicMock, patch

import pytest

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)


# Per-test wall-clock ceiling, in seconds. Deliberately generous: this is a
# runaway backstop, not a performance budget. Override with
# OPENCOHOST_TEST_TIMEOUT=0 to disable (e.g. when attaching a debugger).
HUNG_TEST_TIMEOUT_S = float(os.environ.get("OPENCOHOST_TEST_TIMEOUT", "90"))


@pytest.fixture(autouse=True)
def _kill_hung_test():
    """Hard ceiling on a single test, so a runaway can never take the machine.

    OWNER RULE 2026-07-24: "la suite de test no hay que fundir la pc."

    ``pytest.ini``'s own ``faulthandler_timeout`` is NOT a safety net — it only
    PRINTS the tracebacks and lets the test keep running. That was proven the
    hard way: test_memorias_profile_uuid.py's launch-coverage test spun in an
    unbounded CustomTkinter loop and reached 16 GB RSS (99% of 31 GB) with
    ``faulthandler_timeout=25`` active the whole time.

    ``dump_traceback_later(..., exit=True)`` is the version that actually
    stops it: it dumps every thread's stack (so you still learn WHICH test and
    WHERE) and then hard-exits the process. Losing the rest of the run is the
    correct trade against losing the machine — a hang is a bug either way.

    Stdlib only; deliberately not pytest-timeout, which would add a dependency
    to do what faulthandler already does.
    """
    if HUNG_TEST_TIMEOUT_S <= 0:
        yield
        return
    faulthandler.dump_traceback_later(HUNG_TEST_TIMEOUT_S, exit=True)
    try:
        yield
    finally:
        faulthandler.cancel_dump_traceback_later()


@pytest.fixture(autouse=True, scope="session")
def _neutralize_ctk_appearance_tracker():
    """Stop CustomTkinter from walking a mock widget's parent chain forever.

    Every real CTk widget registers with ``AppearanceModeTracker.add()``, which
    resolves the Tk root like this::

        while isinstance(current_widget, tkinter.Tk) is False:
            current_widget = current_widget.master

    Hand it a ``MagicMock`` parent — what every headless UI test does — and the
    loop never terminates: a MagicMock is never a ``tkinter.Tk``, and each
    ``.master`` access CREATES a fresh child mock that the parent then caches
    (``m.master is m.master`` -> True). It is a while loop, not recursion, so
    there is no RecursionError to rescue it; RSS just climbs until the machine
    dies.

    Light/dark appearance tracking has no meaning in a headless suite, so we
    no-op the registration for the whole session. This kills the entire CLASS
    of hang rather than one test: any future panel nested inside another
    (exactly how this surfaced — personalization_panel mounted from
    profile_panel.build) is covered without anyone remembering to widen a
    patch. Best-effort by design: if customtkinter is absent or its internals
    move, tests must still run.
    """
    try:
        from customtkinter.windows.widgets.appearance_mode import (
            appearance_mode_tracker as _tracker_mod,
        )
    except Exception:
        yield
        return

    tracker = _tracker_mod.AppearanceModeTracker
    with (
        patch.object(tracker, "add", classmethod(lambda cls, *a, **k: None)),
        patch.object(tracker, "remove", classmethod(lambda cls, *a, **k: None)),
    ):
        yield


@pytest.fixture(autouse=True)
def _restore_api_logger_levels():
    """setup_api_logging() pins process-wide levels on the opencohost.api tree
    (INFO, by design for production). Without restoring ``.level`` between
    tests, any test that touches create_app() pins the level for the rest of
    the pytest process and caplog.at_level() expectations in unrelated files
    become collection-order-dependent. Handlers are already restored by the
    local fixtures in test_api_observability.py; levels are restored here for
    the whole suite.
    """
    names = ("opencohost.api", "opencohost.api.audit")
    saved = [(logging.getLogger(name), logging.getLogger(name).level) for name in names]
    yield
    for logger, level in saved:
        logger.setLevel(level)


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
    import opencohost.core.profiles.personalization as personalization_mod

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
def _isolate_llm_provider_and_keys_files(tmp_path, monkeypatch):
    """Keep the LLM provider config / API keys out of the developer's real
    config/ directory.

    ``config/llm_provider.json`` and ``config/llm_keys.json`` are real,
    live files on this machine (the owner's actual cloud posture + a real
    API key -- multi_provider_llm_20260723 incident, 2026-07-24).
    ``MotorVocalIA.__init__`` calls ``load_provider_config()`` unconditionally
    at construction, and ``opencohost/config/llm_provider.py`` binds
    ``LLM_PROVIDER_CONFIG_FILE`` via a from-import at module load time (same
    pattern as ``PERSONALIZATION_FILE`` above), so the module attribute
    itself must be patched -- patching ``settings.LLM_PROVIDER_CONFIG_FILE``
    would have no effect. ``LLM_KEYS_FILE`` is imported by name into both
    ``opencohost/api/main.py`` and ``opencohost/core/llm_engine.py`` and must
    be patched at each site for the same reason. Absent files resolve to the
    fail-open local-only default (spec B) -- no directory needs creating,
    and any test that sets its own tmp path afterwards (e.g.
    test_llm_provider_config.py, test_llm_is_local_gating.py's explicit
    ``provider_config=`` override) simply overwrites this one, since both
    point at nonexistent per-test paths either way.
    """
    import opencohost.api.main as api_main_mod
    import opencohost.config.llm_provider as llm_provider_mod
    import opencohost.core.llm_engine as llm_engine_mod

    keys_file = str(tmp_path / "llm_keys.json")
    monkeypatch.setattr(
        llm_provider_mod, "LLM_PROVIDER_CONFIG_FILE", str(tmp_path / "llm_provider.json")
    )
    monkeypatch.setattr(api_main_mod, "LLM_KEYS_FILE", keys_file)
    monkeypatch.setattr(llm_engine_mod, "LLM_KEYS_FILE", keys_file)


@pytest.fixture(autouse=True)
def _isolate_ptt_config_file(tmp_path, monkeypatch):
    """Keep ``config/ptt_settings.json`` out of the developer's real config/
    directory.

    That file is SHARED state: the legacy CTK ``PTTManager`` owns the
    ``hotkey`` key and PUT /api/ptt/config owns ``stt_ws_uri``
    (liveaudio_ws_uri_config_20260724). ``load_ptt_ws_uri``/``save_ptt_ws_uri``
    resolve ``settings.PTT_CONFIG_FILE`` at call time (same lazy-read pattern
    as ``LAST_PROFILE_FILE`` above), so patching the module attribute is
    enough to keep any test that hits the PUT from rewriting the operator's
    real hotkey + LiveAudio URL. An absent file resolves to the ``WS_URI``
    default, so nothing needs creating here.
    """
    from opencohost.config import settings as settings_mod

    monkeypatch.setattr(
        settings_mod, "PTT_CONFIG_FILE", str(tmp_path / "ptt_settings.json")
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

    log_dir = tmp_path / "logs"
    os.makedirs(log_dir, exist_ok=True)
    monkeypatch.setattr(settings_mod, "LOG_DIR", str(log_dir))
    monkeypatch.setattr(
        settings_mod, "ACCIONES_LOG_FILE", str(log_dir / "acciones.jsonl")
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
