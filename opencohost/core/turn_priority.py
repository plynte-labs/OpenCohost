"""Dispatch-queue tier model (tauri_stream_chat_20260812 §3.2 phase 1).

Owner-approved 2026-08-12:

    0  PTT / voice
    1  direct                 <- ALWAYS above stream
    2  stream  <->  agenda    <- relative order is a STREAMER SETTING
    3

Why the split exists: `_priority_queue`'s original model (0=PTT, 1=chat,
2=agenda) was designed when "chat" meant ONE interlocutor at human pace.
Stream (viewer chat) put a firehose in tier 1, so a typed owner question
(`direct`) queued FIFO behind viewer reactions (measured 2026-08-12, 42-turn
Twitch session). `direct` now permanently outranks stream; whether the
co-host serves the audience (stream first, the default) or the planned
topics (agenda first) is the streamer's call, not a constant.

These are PROCESS-GLOBAL runtime settings, live-adjustable through
`PUT /api/stream/chat-live/limits` before AND during a connection. Same
mechanism as `chat_input_contract.USE_INPUT_CONTRACT_PROMPT`: the router
rebinds the module attribute (atomic under the GIL); every reader goes
through the module attribute or the functions below — never a from-import
of the value, which would freeze the boot value. Not persisted across
restarts, matching the sibling limits (threshold/cooldown/spam).

This module must stay dependency-free (stdlib only): it is read from
`opencohost.core` (the engine's TTL sweep and enqueue default),
`opencohost.smart_aggregator` (the agenda controller's mint sites) and
`opencohost.api` (the limits endpoint) — any import here risks a cycle.

NOTE: distinct from `speech.router.priority_for_source`, which orders
PLAYBACK bands (owner/chat/agenda) and is deliberately untouched: two
generated replies rarely coexist in the router, and the waiting/TTL problem
this model solves lives in the dispatch queue, not the mixer.
"""
from __future__ import annotations

PRIORITY_PTT = 0
PRIORITY_DIRECT = 1

# The streamer setting: True (default) keeps today's effective order —
# stream reactions outrank agenda blocks — so existing users see no change.
STREAM_OVER_AGENDA: bool = True

# The configurable discard window for stream items, replacing the engine's
# hardcoded 30s `_pq_ttl_seconds` for `source="chat"`. Raised per the owner's
# decision: 30s barely covered one agenda sentence, let alone a monologue.
STREAM_TTL_SECONDS: float = 120.0

# PUT validation bounds. The lower bound exists because a ~0s window expires
# every stream item before the engine can pop it — the same mute-co-host
# failure THE TRAP floor guards against, reachable from the other direction.
STREAM_TTL_MIN_SECONDS: float = 10.0
STREAM_TTL_MAX_SECONDS: float = 3600.0

# THE TRAP guard (see effective_stream_ttl): with "agenda first" selected a
# stream item legitimately waits out whole agenda blocks (71s observed in
# logs/opencohost_20260812_194539.log; generate-and-speak spans 15-100s), and
# often more than one before a boundary finds no queued agenda item. 300s
# covers several consecutive blocks with margin. NOT configurable on purpose:
# a floor the UI can lower is not a floor.
AGENDA_FIRST_STREAM_TTL_FLOOR_SECONDS: float = 300.0


def stream_priority() -> int:
    """Dispatch tier for viewer/stream items (source="chat")."""
    return 2 if STREAM_OVER_AGENDA else 3


def agenda_priority() -> int:
    """Dispatch tier for agenda items (source="kira-agenda*")."""
    return 3 if STREAM_OVER_AGENDA else 2


def effective_stream_ttl() -> float:
    """The TTL the engine's sweep actually applies to stream items.

    THE TRAP this exists to prevent: the order setting and the TTL setting
    interact. "Agenda first" makes stream items wait far longer, and a short
    TTL then expires every one of them SILENTLY — the streamer configured a
    co-host that can never answer its audience and gets no error anywhere.
    Flooring the effective window when agenda wins makes that combination
    unreachable through the UI while still honoring a configured window
    LONGER than the floor. (A full TTL exemption was rejected: the owner
    chose a raised-but-real discard window, and a reaction to 10-minute-old
    chat is its own failure mode.)
    """
    if STREAM_OVER_AGENDA:
        return STREAM_TTL_SECONDS
    return max(STREAM_TTL_SECONDS, AGENDA_FIRST_STREAM_TTL_FLOOR_SECONDS)


def dispatch_priority_for_source(source: str) -> int:
    """Resolve the dispatch-queue tier for a source tag.

    Unknown sources fail closed into the stream tier (least trusted band
    that still gets served): a new tag must never outrank an owner question
    by accident. Callers that need a different band pass `priority`
    explicitly — `enqueue()` always honors an explicit value.
    """
    if source == "ptt":
        return PRIORITY_PTT
    if source == "direct":
        return PRIORITY_DIRECT
    if source.startswith("kira-agenda"):
        return agenda_priority()
    return stream_priority()
