"""Laya question schemas, one per decision (laya-decision-layer-20261009).

Each decision is a ``choice`` question in Laya's format: ``instructions`` plus
``criteria`` mapping each option to a neutral description. The state Laya
reads is the clean user text; the questions stay in English (Laya routes
Spanish input to its multilingual checkpoint on its own).

Two-option choices on purpose: the model card notes that a no/unknown option
gets pulled toward its own label text, so every decision offers exactly the
options its consumer can act on, with symmetric descriptions.

A future ``tool`` decision is one more entry here plus a consumer.
"""
from __future__ import annotations

from types import MappingProxyType

from opencohost.config.settings import OWNER_BUNDLE_SOURCE
from opencohost.core.decision.types import CLOCK, MEMORY, REPLY_LENGTH, RESPOND, VISION, WEB

_OWNER = frozenset({"direct", "ptt", OWNER_BUNDLE_SOURCE})

QUESTIONS = MappingProxyType({
    VISION: {
        "type": "choice",
        "instructions": (
            "A streamer is talking to their AI co-host. Decide whether the co-host "
            "needs to look at the streamer's screen to answer this message well."
        ),
        "criteria": {
            "look": (
                "The message is about something visible on the screen right now, such as "
                "the game, an image, a video, a menu or on-screen text."
            ),
            "no_look": (
                "The message can be answered well without seeing the screen."
            ),
        },
    },
    MEMORY: {
        "type": "choice",
        "instructions": (
            "A streamer is talking to their AI co-host. Decide whether answering this "
            "message well needs the co-host to recall earlier conversations or facts "
            "about the streamer."
        ),
        "criteria": {
            "recall": (
                "The message refers to the past, to the streamer's life, preferences or "
                "plans, or to something the co-host may already know about them."
            ),
            "skip": (
                "The message can be answered well from the current conversation alone."
            ),
        },
    },
    REPLY_LENGTH: {
        "type": "choice",
        "instructions": (
            "A streamer is talking to their AI co-host. Decide how long the co-host's "
            "spoken reply to this message should be."
        ),
        "criteria": {
            "short": "One short sentence is enough, such as for a greeting, a reaction or a quick acknowledgement.",
            "medium": "Two or three sentences, for a normal conversational reply.",
            "long": "A longer reply, for an explicit request to explain, list, compare or go into detail.",
        },
    },
    RESPOND: {
        "type": "choice",
        "instructions": (
            "A viewer wrote this message in a live stream chat. Decide whether the "
            "stream's AI co-host should answer it out loud."
        ),
        "criteria": {
            "respond": "The message invites an answer, such as a question, a greeting to the co-host or a comment worth reacting to.",
            "ignore": "The message does not need an answer from the co-host.",
        },
    },
    # web-fetch-laya-20261009: asked only while the owner's web lookup switch is on.
    WEB: {
        "type": "choice",
        "instructions": (
            "A streamer is talking to their AI co-host. Decide whether the co-host "
            "needs to search the internet for fresh information to answer this message well."
        ),
        "criteria": {
            "search": (
                "The message asks for news, recent events or current facts, such as releases, "
                "patches, scores, prices or anything that must be looked up online."
            ),
            "no_search": "The message can be answered well without searching the internet.",
        },
    },
    # clock-and-compact-polish-20261010: asked only when a loose time/date word
    # is present (core/context/clock.py ``may_ask_time``).
    CLOCK: {
        "type": "choice",
        "instructions": (
            "A streamer is talking to their AI co-host. Decide whether the message "
            "asks for the current time, day or date."
        ),
        "criteria": {
            "time": "The message asks what time it is now, what day or date it is today, or the current year.",
            "no_time": "The message does not ask for the current time, day or date.",
        },
    },
})

VALUES = MappingProxyType({d: tuple(q["criteria"]) for d, q in QUESTIONS.items()})

SOURCES = MappingProxyType({
    VISION: frozenset({"direct", "ptt"}),
    MEMORY: _OWNER,
    REPLY_LENGTH: _OWNER,
    RESPOND: frozenset({"chat"}),
    WEB: frozenset({"direct", "ptt"}),
    CLOCK: frozenset({"direct", "ptt"}),
})
