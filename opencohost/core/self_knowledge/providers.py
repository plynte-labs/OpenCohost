"""Fact providers. Each depends only on injected readers, so tests use fakes
and a failing reader (pynvml absent, ...) yields no fact instead of an error."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Optional, Protocol, Sequence, runtime_checkable

from opencohost.core.self_knowledge.facts import (
    OWNER,
    TOPIC_CAPABILITIES,
    TOPIC_COMMANDS,
    TOPIC_CREATOR,
    TOPIC_GPU,
    TOPIC_IDENTITY,
    TOPIC_INTERLOCUTOR,
    TOPIC_MODEL,
    TOPIC_RUNTIME,
    TOPIC_TIME,
    VIEWER,
    SelfFact,
    SelfKnowledgeContext,
)
from opencohost.core.self_knowledge.texts import (
    DEFAULT_CREATOR,
    DEFAULT_REPOSITORY,
    PRODUCT_NAME,
    t,
)
from opencohost.core.vision.gate import LOOK_MODE_ALIASES


@runtime_checkable
class SelfKnowledgeProvider(Protocol):
    """Port: the facts of one topic for one turn."""

    topic: str

    def facts(self, ctx: SelfKnowledgeContext) -> list[SelfFact]: ...


@dataclass(frozen=True)
class IdentityCard:
    """Owner-editable identity fields; empty values fall back to the defaults."""

    creator: str = ""
    repository: str = ""
    description: str = ""


@dataclass(frozen=True)
class GpuInfo:
    name: str
    total_mb: float
    free_mb: float


def derive_user_commands() -> tuple[str, ...]:
    """User-facing slash commands, derived from code constants.

    Evidence: the only typed slash command in the backend is ``/mira`` plus the
    mode words ``core.vision.gate`` accepts after it. The engine verbs
    (``POST /api/commands``) are internal API calls, not something the owner
    types, so they are not listed.
    """
    return ("/mira", "/mira [" + "|".join(LOOK_MODE_ALIASES) + "]")


def _speakable_url(url: str) -> str:
    """The repository without scheme or ``www.``: Kira's replies are voiced, and
    the locked ``no_suspicious_links`` output guard blocks ``https://`` and
    ``www.`` links, so a fact carrying one would get her answer discarded."""
    out = url.split("://", 1)[-1]
    return out[4:] if out.lower().startswith("www.") else out


class ClockProvider:
    topic = TOPIC_TIME

    def __init__(self, now: Callable[[], datetime] = datetime.now) -> None:
        self._now = now

    def facts(self, ctx: SelfKnowledgeContext) -> list[SelfFact]:
        txt = t(ctx.lang)
        now = self._now()
        text = txt["time"].format(
            weekday=txt["weekdays"][now.weekday()],
            date=now.strftime("%Y-%m-%d"),
            time=now.strftime("%H:%M"),
        )
        return [SelfFact(self.topic, text, VIEWER)]


class ModelProvider:
    topic = TOPIC_MODEL

    def facts(self, ctx: SelfKnowledgeContext) -> list[SelfFact]:
        txt = t(ctx.lang)
        model = (ctx.model or "").strip()
        parts = [txt["model"].format(model=model)] if model else []
        parts += [txt["model_change"], txt["install"]]
        return [SelfFact(self.topic, " ".join(parts), OWNER)]


class RuntimeProvider:
    """Where inference runs, from the live provider config. Owner only: the
    cloud variant names the provider (never a key)."""

    topic = TOPIC_RUNTIME

    def __init__(self, provider_reader: Callable[[], str]) -> None:
        self._provider_reader = provider_reader

    def facts(self, ctx: SelfKnowledgeContext) -> list[SelfFact]:
        try:
            raw = str(self._provider_reader() or "local")
        except Exception:
            return []
        txt = t(ctx.lang)
        provider = re.sub(r"[^A-Za-z0-9._-]", "", raw)[:40]
        if not provider or provider.lower() == "local":
            return [SelfFact(self.topic, txt["runtime_local"], OWNER)]
        return [SelfFact(self.topic, txt["runtime_cloud"].format(provider=provider), OWNER)]


class GpuProvider:
    topic = TOPIC_GPU

    def __init__(self, reader: Callable[[], Optional[GpuInfo]]) -> None:
        self._reader = reader

    def facts(self, ctx: SelfKnowledgeContext) -> list[SelfFact]:
        try:
            info = self._reader()
        except Exception:
            return []
        if info is None:
            return []
        text = t(ctx.lang)["gpu"].format(
            name=info.name,
            free=f"{info.free_mb / 1024:.1f}",
            total=f"{info.total_mb / 1024:.1f}",
        )
        return [SelfFact(self.topic, text, OWNER)]


class IdentityProvider:
    topic = TOPIC_IDENTITY

    def __init__(self, card_reader: Callable[[], IdentityCard]) -> None:
        self._card_reader = card_reader

    def facts(self, ctx: SelfKnowledgeContext) -> list[SelfFact]:
        txt = t(ctx.lang)
        card = self._card_reader()
        description = (card.description or "").strip() or txt["description"]
        text = txt["identity"].format(name=PRODUCT_NAME, description=description)
        return [SelfFact(self.topic, f"{text} {txt['identity_body']}", VIEWER)]


class CreatorProvider:
    topic = TOPIC_CREATOR

    def __init__(self, card_reader: Callable[[], IdentityCard]) -> None:
        self._card_reader = card_reader

    def facts(self, ctx: SelfKnowledgeContext) -> list[SelfFact]:
        txt = t(ctx.lang)
        card = self._card_reader()
        creator = (card.creator or "").strip() or DEFAULT_CREATOR
        url = (card.repository or "").strip() or DEFAULT_REPOSITORY
        return [
            SelfFact(self.topic, txt["creator"].format(name=PRODUCT_NAME, creator=creator), VIEWER),
            SelfFact(self.topic, txt["repository"].format(url=_speakable_url(url)), VIEWER),
        ]


class CapabilitiesProvider:
    topic = TOPIC_CAPABILITIES

    def __init__(
        self,
        vision_enabled: Callable[[], bool],
        tts_label: Callable[[], str],
        memory_enabled: Optional[Callable[[], bool]] = None,
    ) -> None:
        self._vision_enabled = vision_enabled
        self._tts_label = tts_label
        self._memory_enabled = memory_enabled

    def facts(self, ctx: SelfKnowledgeContext) -> list[SelfFact]:
        txt = t(ctx.lang)
        out = [SelfFact(self.topic, txt["cap_generic"], VIEWER)]
        try:
            vision = bool(self._vision_enabled())
            out.append(
                SelfFact(self.topic, txt["cap_vision_on" if vision else "cap_vision_off"], OWNER)
            )
        except Exception:
            pass
        try:
            tts = str(self._tts_label() or "").strip()
            if tts:
                out.append(SelfFact(self.topic, txt["cap_voice"].format(tts=tts), OWNER))
        except Exception:
            pass
        if self._memory_enabled is not None:
            try:
                memory = bool(self._memory_enabled())
                key = "cap_memory_on" if memory else "cap_memory_off"
                out.append(SelfFact(self.topic, txt[key], OWNER))
            except Exception:
                pass
        return out


class CommandsProvider:
    topic = TOPIC_COMMANDS

    def __init__(self, commands: Callable[[], Sequence[str]] = derive_user_commands) -> None:
        self._commands = commands

    def facts(self, ctx: SelfKnowledgeContext) -> list[SelfFact]:
        try:
            cmds = [c for c in self._commands() if c]
        except Exception:
            return []
        if not cmds:
            return []
        text = t(ctx.lang)["commands"].format(commands=", ".join(cmds))
        return [SelfFact(self.topic, text, OWNER)]


class InterlocutorProvider:
    """Owner turns only: no viewer display name reaches the assembler today, so
    a viewer turn gets no interlocutor fact rather than raw chat identity."""

    topic = TOPIC_INTERLOCUTOR

    def __init__(self, owner_name: Callable[[], str]) -> None:
        self._owner_name = owner_name

    def facts(self, ctx: SelfKnowledgeContext) -> list[SelfFact]:
        if ctx.audience != OWNER:
            return []
        txt = t(ctx.lang)
        try:
            name = str(self._owner_name() or "").strip()
        except Exception:
            name = ""
        text = txt["owner_named"].format(name=name) if name else txt["owner_neutral"]
        return [SelfFact(self.topic, text, OWNER)]
