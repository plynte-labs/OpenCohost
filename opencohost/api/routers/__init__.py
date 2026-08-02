"""Router registry mounted by ``opencohost.api.main.create_app()``.

``ALL_ROUTERS`` order mirrors proposal.md's migration order (small ->
large): events -> status -> llm_provider -> i18n_tts -> avatar -> obs ->
personalization -> perfiles -> music -> agent -> memoria -> ptt -> agenda ->
chat -> stream. Every parameterized (``{...}``) path across all 15 routers
is indexed below. One pair shares segment-count + literal-prefix --
``/api/perfiles/{name}`` vs ``POST /api/perfiles/switch`` -- and is
disambiguated by HTTP method alone (see perfiles.py's CAUTION block); every
other template collides with nothing (documented per family in each router
module's own docstring) -- order here stays cosmetic, not load-bearing
(verified during the B4, B5, and B6 moves; see the batch handoff notes):

- ``routers/perfiles.py``: ``GET/PUT/DELETE /api/perfiles/{name}``
- ``routers/music.py``: ``DELETE /api/music/track/{track_id}``,
  ``GET /api/music/track/{track_id}/audio``
- ``routers/agent.py``: ``POST /api/agent/cards/{card_id}/arm``,
  ``POST /api/agent/notices/{notice_id}/dismiss``
- ``routers/memoria.py`` (B6): ``GET /api/memoria/row/{row_id}``

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
    agenda,
    agent,
    avatar,
    chat,
    events,
    i18n_tts,
    llm_provider,
    memoria,
    music,
    obs,
    perfiles,
    personalization,
    ptt,
    status,
    stream,
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
    memoria.router,
    ptt.router,
    agenda.router,
    chat.router,
    stream.router,
)
