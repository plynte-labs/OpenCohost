"""TurnStamp — frozen carrier for a turn's submit-time provenance.

Phase C1 (refactor_core_api_20260802, proposal.md "Phase C1 -- TurnStamp
dataclass"): replaces the hand-duplicated 2/3-way conditional-kwargs
forwarding idiom that threaded `submitted_at`/`submitted_under_provider`
across llm_engine.py's private seams (`_consume_command` ->
`_dispatch_command` -> `_ejecutar_inferencia`; `enqueue` keeps its two
separate kwargs as public API and the worker rebuilds the stamp at pop
time). One optional keyword-threaded object replaces the 2^n branch
nesting.

The "no stamp" state is `stamp=None` -- never `TurnStamp(submitted_at=None)`.
`submitted_at` is required precisely because a stamp only ever exists once a
turn was actually submitted at a known monotonic time; an internally
generated turn (agenda, accumulation flush) never has one, hence `None`.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class TurnStamp:
    """Submit-time provenance for one turn.

    submitted_at: monotonic stamp of the original front-end submission (the
        API dispatch entry, `opencohost/api/dispatch.py`) -- lets
        `_ejecutar_inferencia` compute the honest queue_wait_ms at pop time.
    submitted_under_provider: the provider posture at submit time, so a
        fallback/return that happens while the item sits queued can be
        disclosed on the reply instead of silently answering under a
        different provider. None for a turn that was never tagged.
    """

    submitted_at: float
    submitted_under_provider: Optional[str] = None
