"""Router registry mounted by ``opencohost.api.main.create_app()``.

``ALL_ROUTERS`` order mirrors proposal.md's migration order (small ->
large): events -> status -> llm_provider -> i18n_tts -> avatar -> obs ->
personalization -> perfiles -> music -> agent. `perfiles.py` and `music.py`
each carry parameterized (``{...}``) paths, but neither collides with any
other route in the app on segment-count + literal-prefix (documented per
family in each router module's own docstring) -- order here stays cosmetic,
not load-bearing (verified during the B4 and B5 moves; see the batch
handoff notes).

Imported LAZILY by ``create_app()`` (never at ``main.py`` module-load time)
to preserve `main.py`'s own import order. Since refactor_core_api_20260802
B5 Part B, no router here imports ``opencohost.api.main`` at module level
(that cycle -- importing a router before ``main`` has ever run raised
ImportError -- is what ``tests/test_routers_import_order.py`` pins closed):
every router instead imports shared, never-monkeypatched objects from
``opencohost.api.shared``, and any monkeypatched name through
``opencohost.api.deps``'s late-import accessors.
"""

from opencohost.api.routers import (
    agent,
    avatar,
    events,
    i18n_tts,
    llm_provider,
    music,
    obs,
    perfiles,
    personalization,
    status,
)

ALL_ROUTERS = (
    events.router,
    status.router,
    llm_provider.router,
    i18n_tts.router,
    avatar.router,
    obs.router,
    personalization.router,
    perfiles.router,
    music.router,
    agent.router,
)
