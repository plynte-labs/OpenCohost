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

# Force Memory v5 to OFF by default across the entire test suite unless a test
# explicitly opts in. Avoids spawning unisolated child worker processes that
# allocate ~800 MB each and exhaust system RAM.
os.environ.setdefault("OPENCOHOST_MEMORY_V5_MODE", "OFF")

# Session logs must never land in the repository logs/ folder. The logger opens
# its file handler at import time, so the override is set here, when conftest
# loads, before any test module imports opencohost. A per-run temporary folder
# keeps test traffic out of the real session logs that diagnostics read as latest.
os.environ["OPENCOHOST_LOG_DIR"] = tempfile.mkdtemp(prefix="opencohost-pytest-logs-")


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


@pytest.fixture(autouse=True)
def _isolate_i18n_locale_file(tmp_path_factory, monkeypatch):
    """Keep the suite independent of the repo's mutable ``config/locale.json``.

    ``i18n.state`` resolves its default persisted-locale file under the real
    user-data dir (the repo's ``config/`` in a dev checkout), which holds
    whatever locale the developer last picked. Tests that go through the
    default path (``get_locale()`` / ``set_locale(code)``) then pass or fail
    depending on machine state. Redirect it to a per-test empty tmp file so the
    default locale applies and writes never touch the repo. Tests that pass an
    explicit ``locale_file`` or patch ``get_locale`` are unaffected.
    """
    from pathlib import Path

    from opencohost.i18n import state as _i18n_state

    locale_file = Path(tmp_path_factory.mktemp("i18n_locale")) / "config" / "locale.json"
    monkeypatch.setattr(_i18n_state, "_default_locale_file", lambda: locale_file)


@pytest.fixture(autouse=True)
def _isolate_vision_settings_file(tmp_path_factory, monkeypatch):
    """Keep the suite independent of the developer's ``config/vision_settings.json``.

    ``EngineHost.start()`` loads that file and applies it to the process-wide
    ``settings.VISION_*`` attributes. Any test that starts a host without its own
    path therefore read the machine's real file (capture_delay_s=0.5, a lighter
    model) and left it applied, so later tests that pin the code defaults
    (``test_vision_settings``, ``test_engine_host_vision``) failed depending on
    order and on the machine. Point the file at an empty per-test path, and
    restore the runtime attributes afterwards. Tests that set their own
    ``VISION_SETTINGS_FILE`` are unaffected (their monkeypatch runs later).
    """
    from opencohost.config import settings as _settings
    from opencohost.config import vision_settings as _vision_settings

    saved = _vision_settings.current()
    path = tmp_path_factory.mktemp("vision_settings") / "vision_settings.json"
    monkeypatch.setattr(_settings, "VISION_SETTINGS_FILE", str(path), raising=False)
    yield
    _vision_settings.apply(saved)


@pytest.fixture(autouse=True)
def _isolate_session_topics_view_file(tmp_path_factory, monkeypatch):
    """session-topic-folder-20261016 (F4): the engine now deletes the session
    topics view at start-up, so a test that builds a MotorVocalIA must never
    point at the developer's real ``data/session_topics/session.md``."""
    from opencohost.config import settings as _settings

    path = tmp_path_factory.mktemp("session_topics_view") / "session.md"
    monkeypatch.setattr(_settings, "SESSION_TOPICS_VIEW_FILE", str(path), raising=False)


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
def _isolate_agenda_settings_file(tmp_path, monkeypatch):
    """Keep the agenda progression toggle off the developer's real
    ``config/agenda_settings.json`` and pin the process globals to the shipped
    default (ON, 'auto') so no test depends on a stored operator choice.
    Tests that need the legacy prompt pin ``AGENDA_PROGRESSION_ENABLED`` False.
    """
    from opencohost.config import settings as settings_mod

    monkeypatch.setattr(
        settings_mod, "AGENDA_SETTINGS_FILE", str(tmp_path / "agenda_settings.json")
    )
    variant = settings_mod._resolve_agenda_progression_variant()
    monkeypatch.setattr(settings_mod, "AGENDA_PROGRESSION_VARIANT", variant)
    monkeypatch.setattr(settings_mod, "AGENDA_PROGRESSION_ENABLED", bool(variant))


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
def _pin_api_auth_off_for_legacy_tests(monkeypatch):
    """Operator-token auth is ENFORCED by default (owner decision 2026-11-08).

    Most API tests exercise route behavior, not auth, and were written for the
    old default. Pin the flag OFF per test so they keep testing their route.
    Auth-specific tests set ``API_AUTH_ENFORCED`` explicitly (later monkeypatch
    wins), and tests/test_api_auth.py::TestDefaultEnforcement covers the real
    default resolution.
    """
    from opencohost.config import settings as settings_mod

    monkeypatch.setattr(settings_mod, "API_AUTH_ENFORCED", False)


@pytest.fixture(autouse=True)
def _allow_testclient_host(monkeypatch):
    """The main API refuses non-loopback Host headers (release-050 D1).

    starlette's TestClient sends ``testserver`` as Host, so the test suite adds
    that one name to the allowlist. The production value stays loopback-only;
    tests/test_host_guard.py restores it for every check of the real rule.
    """
    from opencohost.config import settings as settings_mod

    monkeypatch.setattr(
        settings_mod,
        "API_ALLOWED_HOST_NAMES",
        settings_mod.LOOPBACK_API_HOST_NAMES | {"testserver"},
    )


@pytest.fixture(autouse=True)
def _isolate_remote_devices_file(tmp_path, monkeypatch):
    """Keep paired remote devices out of the developer's real config/ directory.

    remote-voice-gateway-20261020 T2: ``DeviceRegistry`` resolves
    ``settings.REMOTE_DEVICES_FILE`` when it is constructed (in the lifespan,
    or directly in tests), so monkeypatching the module attribute is
    sufficient (same repo-root hazard as API_TOKENS_FILE above).
    """
    from opencohost.config import settings as settings_mod

    monkeypatch.setattr(
        settings_mod, "REMOTE_DEVICES_FILE", str(tmp_path / "remote_devices.json")
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
def _isolate_memorias_db(tmp_path, monkeypatch):
    """Keep every test out of the developer's real data/memorias/memorias.db.

    Root cause of the `test-profile` (156) / `p1` (5) / `bench` (2) drafts
    found in the real store (T6, memory-semantic-recall): tests that build a
    real MotorVocalIA, set ``_current_profile_id`` and commit a capturable pair
    (e.g. test_context_budget_blind_window.py's ``motor_instance``) never
    redirected the path, so capture-at-commit upserted into USER_DATA_DIR.
    Both bound copies are pinned (the engine reads ``llm_engine.MEMORIAS_DB``
    lazily, the API ``main.MEMORIAS_DB``); ``settings.MEMORIAS_DB`` is left
    alone because test_memoria_store asserts its production default. A test
    that sets its own path still wins (its monkeypatch runs after this one).
    """
    import opencohost.api.main as main_mod
    import opencohost.core.llm_engine as llm_engine_mod

    db = str(tmp_path / "isolated-memorias" / "memorias.db")
    monkeypatch.setattr(llm_engine_mod, "MEMORIAS_DB", db)
    monkeypatch.setattr(main_mod, "MEMORIAS_DB", db)


@pytest.fixture(autouse=True)
def _isolate_memory_layers(tmp_path, monkeypatch):
    """Keep the per-layer memory controls (T4) out of the developer's real
    config/ and stop them leaking between tests: EngineHost.start() applies
    MEMORY_LAYERS_FILE to the settings module, and POST /api/memoria/settings
    mutates it. Pin the file to tmp and re-set the flags so monkeypatch
    restores them after every test."""
    import opencohost.config.settings as settings_mod

    monkeypatch.setattr(settings_mod, "MEMORY_LAYERS_FILE", str(tmp_path / "memory_layers.json"))
    for attr in ("MEMORY_FACTS_ENABLED", "MEMORY_EPISODES_ENABLED", "MEMORY_SEMANTIC_ENABLED"):
        monkeypatch.setattr(settings_mod, attr, True)


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
def _isolate_self_knowledge_settings(tmp_path, monkeypatch):
    """Keep the owner's real ``self_knowledge.json`` out of every test.

    ``SELF_KNOWLEDGE_FILE`` is bound via a from-import, so the module attribute
    is patched (same reason as the pronunciation fixture); the live-read cache
    is cleared on both sides.
    """
    import opencohost.config.self_knowledge_settings as sks_mod

    monkeypatch.setattr(sks_mod, "SELF_KNOWLEDGE_FILE", str(tmp_path / "self_knowledge.json"))
    sks_mod.reset_cache()
    yield
    sks_mod.reset_cache()


@pytest.fixture(autouse=True)
def _isolate_pronunciation_dictionary(tmp_path, monkeypatch):
    """Keep the owner's real ``pronunciation.json`` out of every test and start
    each one with an EMPTY in-memory dictionary.

    The module binds ``PRONUNCIATION_FILE`` via a from-import, so the module
    attribute itself is patched (same reason as the personalization fixture).
    API startup (`lifespan`) calls ``load_pronunciation()``; with the patched
    path that reads a non-existent tmp file, i.e. stays empty.
    """
    import opencohost.core.speech.pronunciation as pronunciation_mod

    monkeypatch.setattr(
        pronunciation_mod, "PRONUNCIATION_FILE", str(tmp_path / "pronunciation.json")
    )
    pronunciation_mod.reset_pronunciation()
    yield
    pronunciation_mod.reset_pronunciation()


@pytest.fixture(autouse=True)
def _isolate_cohost_profiles_and_locale_migration_marker(tmp_path, monkeypatch):
    """Keep cohost_profiles and the .en_locale_migrated.json marker out of
    the developer's real repo root and config/ directory.
    """
    import opencohost.config.locale_upgrade as locale_upgrade_mod
    import opencohost.core.profiles.cohost_profiles as cohost_profiles_mod

    monkeypatch.setattr(
        cohost_profiles_mod,
        "COHOST_PROFILES_FILE",
        str(tmp_path / "cohost_profiles.json"),
    )
    monkeypatch.setattr(
        locale_upgrade_mod,
        "EN_LOCALE_MIGRATED_FILE",
        str(tmp_path / ".en_locale_migrated.json"),
    )


@pytest.fixture(autouse=True)
def _isolate_guardrail_policy(tmp_path, monkeypatch):
    """Keep the owner's real ``guardrails.json`` out of every test and start
    each one with the default policy (every rule enabled).

    ``GUARDRAILS_FILE`` is bound via a from-import, so the module attribute is
    patched (same reason as the pronunciation fixture).
    """
    import opencohost.config.guardrail_policy as guardrail_mod

    monkeypatch.setattr(guardrail_mod, "GUARDRAILS_FILE", str(tmp_path / "guardrails.json"))
    guardrail_mod.reset_guardrails()
    yield
    guardrail_mod.reset_guardrails()


@pytest.fixture(autouse=True)
def _isolate_reply_length(tmp_path, monkeypatch):
    """Keep the owner's real ``reply_length.json`` out of every test and start
    each one at the default mode (auto). ``REPLY_LENGTH_FILE`` is bound via a
    from-import, so the module attribute is patched."""
    import opencohost.config.reply_length as reply_length_mod

    monkeypatch.setattr(
        reply_length_mod, "REPLY_LENGTH_FILE", str(tmp_path / "reply_length.json")
    )
    reply_length_mod.reset_reply_length()
    yield
    reply_length_mod.reset_reply_length()


@pytest.fixture(autouse=True)
def _isolate_web_fetch(tmp_path, monkeypatch):
    """web-fetch-laya-20261009: the owner's ``web_fetch.json`` and the
    developer's ``OPENCOHOST_WEB_FETCH`` never reach a test; each test starts
    with web lookups off."""
    import opencohost.config.settings as settings_mod
    import opencohost.config.web_fetch as web_fetch_mod

    monkeypatch.delenv("OPENCOHOST_WEB_FETCH", raising=False)
    monkeypatch.setattr(settings_mod, "WEB_FETCH_DEFAULT", False)
    monkeypatch.setattr(web_fetch_mod, "WEB_FETCH_FILE", str(tmp_path / "web_fetch.json"))
    web_fetch_mod.reset_web_fetch()
    yield
    web_fetch_mod.reset_web_fetch()


@pytest.fixture(autouse=True)
def _isolate_update_check(tmp_path, monkeypatch):
    """update-notification-20261111: the owner's ``update_check.json`` never
    reaches a test; each test starts with the check on and an empty cache."""
    import opencohost.config.update_setting as update_setting_mod
    import opencohost.core.update_check as update_check_mod

    monkeypatch.setattr(update_setting_mod, "UPDATE_CHECK_FILE", str(tmp_path / "update_check.json"))
    update_setting_mod.reset_update_check()
    update_check_mod.reset_cache()
    yield
    update_setting_mod.reset_update_check()
    update_check_mod.reset_cache()


_DECISION_ENV = (
    "OPENCOHOST_LAYA_PYTHON",
    "OPENCOHOST_LAYA_CHECKPOINT",
    "OPENCOHOST_LAYA_ENGINE",
    "OPENCOHOST_LAYA_ONNX_DIR",
    "OPENCOHOST_DECISION_BUDGET_MS",
    *(
        f"OPENCOHOST_DECISION_{d}{suffix}"
        for d in ("VISION", "MEMORY", "REPLY_LENGTH", "RESPOND", "WEB", "CLOCK")
        for suffix in ("", "_THRESHOLD")
    ),
)


@pytest.fixture(autouse=True)
def _isolate_decisions(tmp_path, monkeypatch):
    """Keep the owner's real ``decisions.json``, the developer's
    ``OPENCOHOST_DECISION_*``/``OPENCOHOST_LAYA_*`` env and any Laya worker out
    of every test (laya-decision-layer-20261009). Each test starts at the code
    defaults with no decision backend, so the bus is unavailable and every
    turn takes the rule path unless a test installs a fake backend."""
    from opencohost.config import decisions as decisions_mod
    from opencohost.config import settings as settings_mod
    from opencohost.core.decision import runtime as decision_runtime

    for name in _DECISION_ENV:
        monkeypatch.delenv(name, raising=False)
    defaults = settings_mod._resolve_decision_defaults()
    monkeypatch.setattr(settings_mod, "DECISION_DEFAULTS", defaults)
    monkeypatch.setattr(settings_mod, "DECISION_BUDGET_MS", defaults["budget_ms"])
    monkeypatch.setattr(settings_mod, "LAYA_PYTHON", "")
    monkeypatch.setattr(settings_mod, "LAYA_CHECKPOINT", "")
    # Never the real exported model: the default ONNX engine looks in an empty
    # per-test dir, so the worker stays unavailable unless a test says otherwise.
    monkeypatch.setenv("OPENCOHOST_LAYA_ONNX_DIR", str(tmp_path / "no-laya-onnx"))
    monkeypatch.setattr(settings_mod, "LAYA_ENGINE", "onnx")
    monkeypatch.setattr(settings_mod, "LAYA_ONNX_DIR", str(tmp_path / "no-laya-onnx"))
    monkeypatch.setattr(decisions_mod, "DECISIONS_FILE", str(tmp_path / "decisions.json"))
    decisions_mod.reset_decisions()
    decision_runtime._reset_for_tests()
    yield
    decision_runtime._reset_for_tests()
    decisions_mod.reset_decisions()


@pytest.fixture(autouse=True)
def _isolate_model_context_policy(monkeypatch):
    """Keep the developer's real ``config/model_parameters.json`` context policy
    out of the ctx resolver.

    ``_resolve_effective_ctx_limit`` reads ``get_model_context_settings`` on every
    call; an owner running ``context.policy = "requested"`` locally would
    otherwise flip the 4096 cold-start assertions across the suite. Tests that
    exercise the requested path monkeypatch the same seam themselves.
    """
    from opencohost.core.engine import llm_engine_models

    monkeypatch.setattr(
        llm_engine_models,
        "get_model_context_settings",
        lambda *a, **k: {"policy": "runtime_truth", "requested_tokens": None},
    )


@pytest.fixture(autouse=True)
def _isolate_model_downloads(tmp_path, monkeypatch):
    """Keep the model-download job record and the real Ollama pull out of tests.

    ``create_app()``'s lifespan builds a ``ModelDownloadManager`` that reads
    ``manager.MODEL_DOWNLOADS_FILE`` (USER_DATA_DIR/config in dev mode, i.e.
    the repo root) and, on a download request, streams ``/api/pull`` from the
    owner's real Ollama. Both are resolved at call time, so patching the
    module attributes redirects the record to a per-test temp path and turns
    any default-built pull into an immediate, non-retryable failure.
    """
    from opencohost.core.model_downloads import manager as model_downloads_mod

    def _blocked_pull(tag):
        raise RuntimeError("real Ollama pull is blocked in tests")

    monkeypatch.setattr(
        model_downloads_mod, "MODEL_DOWNLOADS_FILE", str(tmp_path / "model_downloads.json")
    )
    monkeypatch.setattr(model_downloads_mod, "default_pull_stream", _blocked_pull)


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
    import keyring
    from opencohost.security.keyring_store import MemoryKeyring, _STORE_INSTANCES, _STORE_LOCK

    keys_file = str(tmp_path / "llm_keys.json")
    monkeypatch.setattr(
        llm_provider_mod, "LLM_PROVIDER_CONFIG_FILE", str(tmp_path / "llm_provider.json")
    )
    monkeypatch.setattr(api_main_mod, "LLM_KEYS_FILE", keys_file)
    monkeypatch.setattr(llm_engine_mod, "LLM_KEYS_FILE", keys_file)

    test_keyring = MemoryKeyring()
    old_keyring = keyring.get_keyring()
    keyring.set_keyring(test_keyring)
    with _STORE_LOCK:
        _STORE_INSTANCES.clear()

    yield

    with _STORE_LOCK:
        _STORE_INSTANCES.clear()
    keyring.set_keyring(old_keyring)


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
def _never_launch_real_liveaudio_desktop(monkeypatch):
    """Keep the default ``LiveAudioSupervisor`` from finding the developer's
    real LiveAudio desktop install.

    The supervisor resolves ``liveaudio-cli.exe`` from the install root
    (registry ``InstallLocation``) and launches it detached when a loopback
    PTT probe finds no service.
    Any test that builds ``create_app()`` without a supervisor factory would
    otherwise open the real app on this machine. Tests that exercise desktop
    resolution inject ``registry_reader``/``environ`` explicitly.
    """
    from opencohost.stt import supervisor as supervisor_mod

    monkeypatch.setattr(supervisor_mod, "_read_registry_install_location", lambda: None)
    monkeypatch.delenv("LIVEAUDIO_INSTALL_ROOT", raising=False)


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
    without this isolation writes into the checkout's own ``logs/``
    (opencohost_api.log, api_audit.jsonl, acciones.jsonl) -- outside a frozen
    build ``USER_DATA_DIR`` IS the repo root, so that is a live developer log
    directory, not a scratch one. Same repo-root hazard as the fixtures above.
    """
    from opencohost.config import settings as settings_mod

    log_dir = tmp_path / "logs"
    os.makedirs(log_dir, exist_ok=True)
    monkeypatch.setattr(settings_mod, "LOG_DIR", str(log_dir))
    monkeypatch.setattr(
        settings_mod, "ACCIONES_LOG_FILE", str(log_dir / "acciones.jsonl")
    )


@pytest.fixture(autouse=True)
def _isolate_memory_v5_mode(monkeypatch):
    """Keep Memory v5 subsystem OFF by default across all tests.

    When MEMORY_V5_MODE is ACTIVE or SHADOW, MotorVocalIA initializes
    MemorySubsystem, which spawns SemanticWorkerService in an uncollected
    child process holding ~800 MB for ONNX + tokenizers. Over hundreds of tests,
    uncollected child processes exhaust all system RAM and cause system OOM crashes.
    Tests specifically asserting Memory v5 functionality monkeypatch this per-test.
    """
    from opencohost.config import settings as settings_mod

    monkeypatch.setenv("OPENCOHOST_MEMORY_V5_MODE", "OFF")
    monkeypatch.setattr(settings_mod, "MEMORY_V5_MODE", "OFF")


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


@pytest.fixture(autouse=True)
def _isolate_web_search_ledger_and_keys(tmp_path, monkeypatch):
    """No test may read a real search API key or touch the dev usage ledger.

    The owner's shell may export OPENCOHOST_TAVILY_API_KEY / _BRAVE_API_KEY, and
    ``settings`` resolves them at import time; the default tracker would then
    write to config/web_search_usage.sqlite3.
    """
    from opencohost.config import settings as _settings
    from opencohost.core.web import search_usage as _usage

    monkeypatch.delenv("OPENCOHOST_TAVILY_API_KEY", raising=False)
    monkeypatch.delenv("OPENCOHOST_BRAVE_API_KEY", raising=False)
    monkeypatch.setattr(_settings, "TAVILY_API_KEY", "", raising=False)
    monkeypatch.setattr(_settings, "TAVILY_API_KEY_ENV_PRESENT", False)
    # Search tests must never inspect the owner's OS credentials. Secure-store
    # tests replace this boundary with an explicitly isolated fake backend.
    from opencohost.security import search_keys
    from opencohost.security.keyring_store import KeyStoreError

    def unavailable_store(**_kwargs):
        raise KeyStoreError("tavily_key_store_unavailable")

    monkeypatch.setattr(search_keys, "get_keyring_store", unavailable_store)
    monkeypatch.setattr(_settings, "BRAVE_API_KEY", "", raising=False)
    monkeypatch.setattr(
        _settings, "WEB_SEARCH_USAGE_DB", str(tmp_path / "web_search_usage.sqlite3"), raising=False
    )
    monkeypatch.setattr(
        _settings, "WEB_SEARCH_USAGE_FILE", str(tmp_path / "web_search_usage.json"), raising=False
    )
    monkeypatch.setattr(_usage, "_DEFAULT_TRACKER", None, raising=False)


@pytest.fixture
def legacy_prompt_layout(monkeypatch):
    """Pin ``PROMPT_LAYOUT`` to ``legacy`` for a test that asserts legacy-only behavior.

    ``settings.PROMPT_LAYOUT`` is resolved once at import (``OPENCOHOST_PROMPT_LAYOUT``),
    and both the assembler and ``MotorVocalIA._history_maxlen`` read it at call time.
    A test that pins the legacy system-message placement, the one-pair history deque
    (``turns * 2``) or a legacy golden must request this fixture, so the suite stays
    green when the default flips to ``v2`` (kv-cache-prompt-layout, Judgment Day B-W4).
    Request it before the engine/deque is built. It is not autouse on purpose: the
    layout-neutral and v2 tests must keep running under whichever layout is set.
    """
    from opencohost.config import settings as _settings

    monkeypatch.setattr(_settings, "PROMPT_LAYOUT", "legacy", raising=False)


@pytest.fixture(autouse=True)
def _isolate_usage_ledger(tmp_path, monkeypatch):
    """Keep the usage ledger out of the repo: point it at a per-test temp DB.

    opencohost/core/usage/ledger.py reads settings.USAGE_LEDGER_DB lazily.
    """
    from opencohost.config import settings as settings_mod

    monkeypatch.setattr(
        settings_mod, "USAGE_LEDGER_DB", str(tmp_path / "usage" / "usage_ledger.db")
    )
    monkeypatch.setattr(settings_mod, "USAGE_LEDGER_ENABLED", True, raising=False)
