"""Shared submission of an owner-typed ``direct`` turn.

Used by ``POST /api/chat/turn`` and by the remote gateway's ``POST /turn``: one
readiness check, one dispatch, one arrival-time drain. The callers own text
validation and the shape of their own responses.

R8: nothing here logs or returns the text.
"""

import time
from dataclasses import dataclass
from typing import Any, Optional

from opencohost.api.shared import logger
from opencohost.i18n import active as i18n_active


@dataclass
class DirectTurnOutcome:
    # "accepted" | "conflict" | "queue_full" | "unavailable"
    kind: str
    result: Any = None
    # Readiness state, set only when kind == "unavailable".
    state: Optional[str] = None


def submit_direct_turn(
    host, dispatcher, text: str, key: Optional[str] = None, *, origin: Optional[str] = None
) -> DirectTurnOutcome:
    """``origin`` is passed by the calling ROUTE (``turn_origin.DESKTOP`` or
    ``REMOTE``); the default None is not desktop (fail closed)."""
    if host is not None:
        from opencohost.core.engine import llm_readiness as llm_readiness_mod

        readiness = llm_readiness_mod.resolve_llm_readiness(host)
        if not readiness.can_chat:
            return DirectTurnOutcome("unavailable", state=readiness.state)

    # B1 (turn provenance): a typed turn used to commit to historial BARE, so
    # the model could not tell WHO spoke and guessed (it referred to "the
    # viewer before" with no chat platform connected at all). history_text
    # gives it the same speaker frame PTT already had; the prompt payload
    # stays the raw text, so generation behavior is unchanged.
    # Unit 4.1 (runtime_findings_batch_20260731, F5): stamp submitted_at HERE
    # -- the earliest backend receipt of a typed direct question -- so
    # queue_wait_ms can later report the FULL wait (queue time was
    # invisible to TURN_LATENCY before this unit). source="direct" made
    # explicit alongside it (was previously left to _consume_command's
    # default).
    # Unit 4.2 (F12 closure): tag the item with the provider posture in
    # effect AT SUBMIT time (unit 1.3's provider_runtime_state -- the same
    # live truth /api/status reports), so a fallback/return that happens
    # while this item sits queued can be disclosed on the reply instead of
    # silently answering under a different provider with no note.
    arrival_stamp = time.monotonic()
    result = dispatcher.dispatch(
        "process_context",
        text,
        key,
        history_text=i18n_active.typed_history_wrapper().format(text=text),
        source="direct",
        submitted_at=arrival_stamp,
        submitted_under_provider=host.motor.provider_runtime_state()["provider"],
        origin=origin,
    )
    if result.state in ("accepted", "replay"):
        # Step 1 (direct_turn_preemption_20260803, OQ-6): while the engine is
        # busy it only reads command_queue between dispatches, so this turn
        # would sit there invisible until the next speech boundary -- and the
        # interactive-pregen trigger, which works off the priority-queue HEAD,
        # would never see it (the measured "Pregen boundary: draft=none
        # source=direct"). Move it into the priority queue NOW, on the caller's
        # thread, using the engine's own existing boundary drain: it re-checks
        # the front of command_queue under its mutex, pops only a leading
        # explicitly-tagged source="direct" process_context, and re-enqueues it
        # at priority 1 with its stamps. Nothing here interrupts anything --
        # the queue is non-preemptive; the win is that the reply can now
        # pregenerate under cover of the agenda's remaining speech.
        #
        # Exception (ADR-069, opt-in, default OFF): with the owner's
        # "interrupt when a direct message arrives" switch ON, the motor cuts
        # non-owner speech at arrival (`cut_for_direct_arrival`) and the turn
        # may pop at once. With the switch OFF the sentence above is exact.
        #
        # The engine-boundary call stays as the backstop for the cases this one
        # cannot cover (a non-direct command ahead of it in FIFO, or this
        # busy-check racing the engine going idle -- run()'s 1s idle tick picks
        # the item up either way).
        #
        # Best-effort by construction: the dispatch already succeeded and the
        # ack is the receipt, so a motor that predates this seam (getattr) or a
        # drain that raises must never turn a queued turn into a 500 the client
        # would retry. Same getattr-and-call idiom the driver uses for every
        # optional engine seam (api/agenda_driver.py).
        motor = getattr(host, "motor", None)
        if motor is not None and (motor.is_processing or motor.is_speaking):
            # ADR-069: the decision lives in the motor/router; this is only
            # the arrival point. It runs BEFORE the drain so this turn's own
            # interactive pregen cannot start first and veto the cut. The
            # hold is keyed to this turn's stamp. Best-effort like the drain.
            # Only a fresh enqueue cuts: a replay enqueued nothing, so no turn
            # carries this stamp and its hold could never be released (it would
            # mute every job until ARRIVAL_HOLD_MAX_SECONDS).
            cut = getattr(motor, "cut_for_direct_arrival", None)
            if result.state == "accepted" and callable(cut):
                try:
                    cut(token=arrival_stamp)
                except Exception:
                    logger.exception("arrival-time direct cut failed; the reply still cuts at submit")
            drain = getattr(motor, "_drain_pending_direct_into_priority_queue", None)
            if callable(drain):
                try:
                    drain()
                except Exception:
                    logger.exception("arrival-time direct drain failed; the boundary drain still covers it")
        return DirectTurnOutcome("accepted", result=result)
    if result.state == "conflict":
        return DirectTurnOutcome("conflict", result=result)
    return DirectTurnOutcome("queue_full", result=result)
