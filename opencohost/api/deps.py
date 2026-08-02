"""Shared state-access layer for API routers (refactor_core_api_20260802, B4).

The FastAPI app is built once by ``opencohost.api.main.create_app()`` and
stored as the module-level ``opencohost.api.main.app``. The existing test
suite monkeypatches attributes DIRECTLY on that module object (``main_mod`` /
``api_main_mod`` in test files, plus an autouse fixture in
``tests/conftest.py`` for ``LLM_KEYS_FILE``) -- never on a router module.

A router that bound one of those names via a top-level ``from
opencohost.api.main import X`` would capture the ORIGINAL object at router
IMPORT time; a later ``monkeypatch.setattr(main, "X", fake)`` would then have
no effect on the router's own copy (plain Python names are independent
bindings, not aliases back to the module). Every accessor below instead does
a LATE import of ``opencohost.api.main`` INSIDE the function body, so it
always reads whatever ``main.X`` currently is -- monkeypatched or not. This
also sidesteps the import cycle: routers may import `deps` at module load
time (this module never imports `main` at ITS module level), while `main`
only imports the routers package lazily, inside `create_app()`.

Per-request engine state (motor / EngineHost / Dispatcher / PttController)
needs NO accessor here: main.py's handlers already take ``request: Request``
and read ``request.app.state.host`` / ``.dispatcher`` / ``.ptt_controller``
directly -- set once per app instance by ``create_app()``'s ``lifespan()``.
That is request-scoped state, not a module global to shadow, and it moves to
the routers completely unchanged.

Only add an accessor here once grep confirms a test patches the name on
``opencohost.api.main`` for a route being moved -- never speculatively.
"""

from __future__ import annotations


def llm_keys_file() -> str:
    """Current ``LLM_KEYS_FILE`` path.

    Patched directly on ``main`` by test_llm_provider_config.py,
    test_llm_provider_probe.py, AND tests/conftest.py's autouse
    ``_isolate_llm_provider_and_keys_files`` (repo-wide key-file isolation --
    every test that ever builds an app goes through this fixture).
    """
    from opencohost.api import main as _main

    return _main.LLM_KEYS_FILE


def discover_ollama_models(*args, **kwargs):
    """Live Ollama discovery, resolved through the CURRENT ``main`` binding.

    tests/test_api_reads.py replaces ``main._discover_ollama_models`` wholesale
    in some cases and only ``main.ollama`` (the client module it calls
    internally) in others -- both work here because the call always lands on
    ``main``'s own module dict at call time, exactly like it did before the
    move.
    """
    from opencohost.api import main as _main

    return _main._discover_ollama_models(*args, **kwargs)


def load_provider_config():
    """GET /api/models routes through here because
    test_models_cloud_active_* (tests/test_api_reads.py) replaces the whole
    function on ``main``. GET/PUT /api/llm/provider (routers/llm_provider.py)
    and POST /api/llm/provider/probe's callers ALSO go through this accessor
    now -- the suite's established idiom patches ``main.load_provider_config``
    directly in several places, not just ``opencohost.config.llm_provider.
    LLM_PROVIDER_CONFIG_FILE``, so a plain top-level import in the router
    would silently bypass those patches (same reasoning as every other
    accessor in this module).
    """
    from opencohost.api import main as _main

    return _main.load_provider_config()


def obs_client_cls():
    """The ``OBSClient`` class, resolved at call time.

    tests/test_api_obs.py replaces ``main.OBSClient`` with fakes across most
    of its POST /api/obs/test cases.
    """
    from opencohost.api import main as _main

    return _main.OBSClient


def load_piper_voice(*args, **kwargs):
    """test_tts_config_shape_from_accessors replaces ``main.load_piper_voice``
    wholesale for GET /api/tts/config."""
    from opencohost.api import main as _main

    return _main.load_piper_voice(*args, **kwargs)


def load_tts_local_only():
    """test_tts_config_shape_from_accessors (tests/test_api_reads.py) replaces
    ``main.load_tts_local_only`` wholesale for GET /api/tts/config, alongside
    ``load_piper_voice``/``load_tts_speed``/``EXPERIMENTAL_HEAVY_TTS_ENABLED``
    in the same test."""
    from opencohost.api import main as _main

    return _main.load_tts_local_only()


def load_tts_speed():
    """test_tts_config_shape_from_accessors (tests/test_api_reads.py) replaces
    ``main.load_tts_speed`` wholesale for GET /api/tts/config, alongside
    ``load_piper_voice``/``load_tts_local_only``/``EXPERIMENTAL_HEAVY_TTS_ENABLED``
    in the same test."""
    from opencohost.api import main as _main

    return _main.load_tts_speed()


def experimental_heavy_tts_enabled() -> bool:
    """test_tts_config_shape_from_accessors (tests/test_api_reads.py) replaces
    ``main.EXPERIMENTAL_HEAVY_TTS_ENABLED`` wholesale for GET /api/tts/config,
    alongside ``load_piper_voice``/``load_tts_local_only``/``load_tts_speed``
    in the same test."""
    from opencohost.api import main as _main

    return _main.EXPERIMENTAL_HEAVY_TTS_ENABLED


def cargar_perfiles():
    """Current profiles dict. Patched directly on ``main`` by
    tests/test_api_phase1.py's perfiles + POST /api/perfiles/switch tests
    (``monkeypatch.setattr(main_mod, "cargar_perfiles", ...)``) -- a plain
    top-level import in routers/perfiles.py would bind the pre-patch
    function object. ``main.py``'s own ``_legacy_profile_key`` (memoria
    family, not moved) also calls ``cargar_perfiles()`` directly, so the
    import stays on ``main`` regardless of this accessor.
    """
    from opencohost.api import main as _main

    return _main.cargar_perfiles()


def save_personalization(data: dict) -> bool:
    """test_api_personalization.py replaces ``main.save_personalization``
    wholesale (``lambda data: False``) to exercise PUT /api/personalization's
    503 write-failure path."""
    from opencohost.api import main as _main

    return _main.save_personalization(data)


def clear_personalization() -> bool:
    """test_api_personalization.py replaces ``main.clear_personalization``
    wholesale (``lambda: False``) to exercise DELETE /api/personalization's
    503 write-failure path."""
    from opencohost.api import main as _main

    return _main.clear_personalization()


def music_import_max_bytes() -> int:
    """POST /api/music/import size cap. test_api_music_library_mutations.py
    patches ``main._MUSIC_IMPORT_MAX_BYTES`` down to 4 bytes so a 12-byte wav
    fixture deterministically exceeds it -- a plain top-level import in
    routers/music.py would freeze the pre-patch 200MB value at router import
    time."""
    from opencohost.api import main as _main

    return _main._MUSIC_IMPORT_MAX_BYTES


def editorial_cards_db() -> str:
    """Current editorial-cards sqlite path. Patched directly on ``main`` by
    tests/test_agent_cards_api.py and tests/test_api_agent_gateway.py
    (autouse fixtures, every /api/agent/cards|notices test) to isolate each
    test's db in a tmp file. tests/test_api_reads.py also patches this name
    for GET /api/memoria/stats -- that endpoint stays inline in main.py and
    reads the module global directly (no accessor needed there); this
    accessor exists only for routers/agent.py's own reads.
    """
    from opencohost.api import main as _main

    return _main.EDITORIAL_CARDS_DB
