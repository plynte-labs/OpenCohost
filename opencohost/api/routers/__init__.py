"""Router registry mounted by ``opencohost.api.main.create_app()``.

``ALL_ROUTERS`` order mirrors proposal.md's migration order (small ->
large): events -> status -> llm_provider -> i18n_tts -> avatar -> obs. None
of the moved paths are parameterized (no ``{...}`` segments), so there is no
ambiguous overlap with any other route in the app -- order here is cosmetic,
not load-bearing (verified during the B4 move; see the batch handoff notes).

Imported LAZILY by ``create_app()`` (never at ``main.py`` module-load time)
to break the import cycle: these router modules import helper functions
straight from ``opencohost.api.main`` at THEIR module-load time, which
requires ``main``'s own module namespace to already be populated.
"""

from opencohost.api.routers import avatar, events, i18n_tts, llm_provider, obs, status

ALL_ROUTERS = (
    events.router,
    status.router,
    llm_provider.router,
    i18n_tts.router,
    avatar.router,
    obs.router,
)
