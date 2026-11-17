"""Composition of one turn's self-knowledge block.

The service is the single entry point the prompt assembler calls. It reads the
owner toggles live, decides the audience from the turn source, runs the gate,
fetches only the gated topics from the providers, filters by audience and logs
one counters-only line. It never logs or stores the utterance.
"""
from __future__ import annotations

import logging
from typing import Callable, Iterable, Optional

from opencohost.core.self_knowledge.facts import (
    OWNER,
    TOPIC_CAPABILITIES,
    TOPIC_CREATOR,
    TOPIC_IDENTITY,
    TOPIC_MODEL,
    TOPIC_TIME,
    VIEWER,
    VIEWER_TOPICS,
    SelfFact,
    SelfKnowledgeContext,
)
from opencohost.core.self_knowledge.filter import filter_facts
from opencohost.core.self_knowledge.gate import SelfKnowledgeGate
from opencohost.core.self_knowledge.providers import SelfKnowledgeProvider
from opencohost.core.self_knowledge.texts import t

logger = logging.getLogger("OpenCohost")

_IDENTITY_TOPICS = frozenset({TOPIC_MODEL, TOPIC_CREATOR, TOPIC_IDENTITY})
DEFAULT_OWNER_SOURCES = frozenset({"direct", "ptt"})
DEFAULT_VIEWER_SOURCES = frozenset({"chat"})


class SelfKnowledgeService:
    def __init__(
        self,
        *,
        providers: Iterable[SelfKnowledgeProvider],
        settings_reader: Callable[[], object],
        lang_reader: Callable[[], str],
        gate: Optional[SelfKnowledgeGate] = None,
        owner_sources: frozenset = DEFAULT_OWNER_SOURCES,
        viewer_sources: frozenset = DEFAULT_VIEWER_SOURCES,
    ) -> None:
        self._providers = {p.topic: p for p in providers}
        self._settings_reader = settings_reader
        self._lang_reader = lang_reader
        self._gate = gate or SelfKnowledgeGate()
        self._owner_sources = owner_sources
        self._viewer_sources = viewer_sources

    def __call__(self, text: str, source: str, request_model: str = "") -> str:
        """The block to append to the system prompt, or "" (never raises)."""
        try:
            return self._build(text, source, request_model)
        except Exception as e:
            logger.warning("[SELF_KNOWLEDGE] build failed: %s", type(e).__name__)
            return ""

    def _lang(self) -> str:
        try:
            return str(self._lang_reader() or "es")
        except Exception:
            return "es"

    def _facts_for(self, topic: str, ctx: SelfKnowledgeContext) -> list[SelfFact]:
        provider = self._providers.get(topic)
        if provider is None:
            return []
        try:
            return list(provider.facts(ctx))
        except Exception as e:
            logger.warning("[SELF_KNOWLEDGE] provider %s failed: %s", topic, type(e).__name__)
            return []

    def _memory_facts(self, ctx: SelfKnowledgeContext) -> list[SelfFact]:
        txt = t(ctx.lang)
        memory_lines = {txt["cap_memory_on"], txt["cap_memory_off"]}
        return [f for f in self._facts_for(TOPIC_CAPABILITIES, ctx) if f.text in memory_lines]

    def knowledge(self, text: str, source: str, request_model: str = "") -> str:
        """ADR-064: the gated topic facts and the gender rule, WITHOUT any date
        or time (the single ``now`` line owns those). "" when nothing applies;
        never raises."""
        try:
            return self._build(text, source, request_model, ambient_time=False)
        except Exception as e:
            logger.warning("[SELF_KNOWLEDGE] build failed: %s", type(e).__name__)
            return ""

    def now_line(self, source: str, request_model: str = "") -> str:
        """ADR-064: the current local date and time for an owner turn, or "".

        Follows the same toggles and audience rule as the ambient line that
        :meth:`__call__` prepends, so the prompt carries the time once."""
        try:
            settings = self._settings_reader()
            if not bool(getattr(settings, "enabled", False)) or source not in self._owner_sources:
                return ""
            ctx = SelfKnowledgeContext(
                audience=OWNER, source=source, model=request_model or "", lang=self._lang()
            )
            return "\n".join(f.text for f in self._facts_for(TOPIC_TIME, ctx))
        except Exception as e:
            logger.warning("[SELF_KNOWLEDGE] now failed: %s", type(e).__name__)
            return ""

    def _build(
        self, text: str, source: str, request_model: str, ambient_time: bool = True
    ) -> str:
        try:
            settings = self._settings_reader()
            enabled = bool(getattr(settings, "enabled", False))
            viewers = bool(getattr(settings, "viewers", False))
        except Exception:
            return ""
        if not enabled:
            return ""
        if source in self._owner_sources:
            audience = OWNER
        elif source in self._viewer_sources and viewers:
            audience = VIEWER
        else:
            return ""

        ctx = SelfKnowledgeContext(
            audience=audience, source=source, model=request_model or "", lang=self._lang()
        )
        topics = self._gate.detect(text)
        skipped = 0
        fetch = topics
        if audience == VIEWER:
            fetch = tuple(x for x in topics if x in VIEWER_TOPICS)
            skipped = len(topics) - len(fetch)

        if audience == OWNER and (TOPIC_CREATOR in fetch or TOPIC_IDENTITY in fetch) and TOPIC_MODEL not in fetch:
            # Weak models invent a base model when only creator/identity facts
            # are present, so the owner also gets the real model fact there.
            fetch = (*fetch, TOPIC_MODEL)
        if audience == OWNER and TOPIC_MODEL in fetch and TOPIC_IDENTITY not in fetch:
            # And the reverse: told only the model tag, a weak model claims to
            # BE that model, so a model turn also carries who Kira is.
            fetch = (*fetch, TOPIC_IDENTITY)

        facts: list[SelfFact] = []
        for topic in fetch:
            facts.extend(self._facts_for(topic, ctx))
        if audience == OWNER and TOPIC_CAPABILITIES not in fetch and any(x in _IDENTITY_TOPICS for x in fetch):
            # Identity answers pad with limitations ("no access to past
            # conversations"), so the owner also gets the real memory state.
            facts.extend(self._memory_facts(ctx))
        kept, filtered = filter_facts(facts, audience)
        if not ambient_time:
            # The time topic is answered from the single ``now`` line.
            kept = [f for f in kept if f.topic != TOPIC_TIME]
        if topics:
            logger.info(
                "[SELF_KNOWLEDGE] topics=%s audience=%s facts=%d withheld=%d",
                ",".join(topics),
                audience,
                len(kept),
                skipped + filtered,
            )

        # Ambient lines ride on every turn that passed the audience check: the
        # fixed gender rule, plus the current date/time for the owner only.
        # The gender rule is a grammar instruction, not a fact: it goes before
        # the facts header so the model never recites it as a datum.
        gender = t(ctx.lang)["gender"]
        ambient: list[str] = []
        if ambient_time and audience == OWNER and TOPIC_TIME not in topics:
            ambient.extend(f.text for f in self._facts_for(TOPIC_TIME, ctx))
        if not kept:
            return "\n".join([gender, *ambient])
        lines = [*ambient, *(f.text for f in kept)]
        if any(f.topic in _IDENTITY_TOPICS for f in kept):
            lines.append(t(ctx.lang)["no_invention"])
        unique = list(dict.fromkeys(lines))
        return "\n".join([gender, t(ctx.lang)["header"], *unique])
