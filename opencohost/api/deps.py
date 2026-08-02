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
    function on ``main``. GET/PUT /api/llm/provider do NOT need this
    accessor: those tests only patch
    ``opencohost.config.llm_provider.LLM_PROVIDER_CONFIG_FILE``, so the
    router there imports ``load_provider_config`` straight from its home
    module with no seam required.
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
    from opencohost.api import main as _main

    return _main.load_tts_local_only()


def load_tts_speed():
    from opencohost.api import main as _main

    return _main.load_tts_speed()


def experimental_heavy_tts_enabled() -> bool:
    from opencohost.api import main as _main

    return _main.EXPERIMENTAL_HEAVY_TTS_ENABLED
