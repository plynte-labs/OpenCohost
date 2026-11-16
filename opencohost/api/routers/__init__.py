"""Router registry mounted by ``opencohost.api.main.create_app()``.

``ALL_ROUTERS`` order mirrors proposal.md's migration order (small ->
large): events -> status -> llm_provider -> i18n_tts -> avatar -> obs ->
personalization -> perfiles -> music -> agent -> memoria -> ptt -> agenda ->
chat -> speech -> stream -> vision. Every parameterized (``{...}``) path across all routers
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
- ``routers/active_cards.py``: ``GET /api/editorial/cards/{card_id}``,
  ``PUT .../{card_id}/fields``, ``POST .../{card_id}/activate|deactivate|disable``,
  ``DELETE .../{card_id}`` (the literal ``/active`` is registered first)
- ``routers/memoria.py`` (B6): ``GET /api/memoria/row/{row_id}``
- ``routers/model_downloads.py``: ``POST /api/models/downloads/{job_id}/pause|resume|cancel``,
  ``GET /api/models/hf/{owner}/{repo}/quants``
- ``routers/remote.py``: ``DELETE /api/remote/devices/{device_id}``

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
    active_cards,
    agenda,
    agent,
    avatar,
    avatar_vrm,
    chat,
    decisions,
    events,
    guardrails,
    i18n_tts,
    llm_provider,
    memoria,
    model_downloads,
    music,
    obs,
    perfiles,
    personalization,
    ptt,
    remote,
    reply_length,
    self_knowledge,
    setup,
    speech,
    status,
    stream,
    system,
    update,
    usage,
    vision,
    web_fetch,
)

ALL_ROUTERS = (
    events.router,
    status.router,
    llm_provider.router,
    model_downloads.router,
    i18n_tts.router,
    avatar.router,
    avatar_vrm.router,
    obs.router,
    personalization.router,
    perfiles.router,
    music.router,
    agent.router,
    memoria.router,
    ptt.router,
    agenda.router,
    chat.router,
    speech.router,
    stream.router,
    system.router,
    vision.router,
    guardrails.router,
    self_knowledge.router,
    active_cards.router,
    setup.router,
    reply_length.router,
    decisions.router,
    web_fetch.router,
    remote.router,
    usage.router,
    update.router,
)
