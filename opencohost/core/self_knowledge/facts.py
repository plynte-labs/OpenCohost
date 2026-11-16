"""Value types and constants shared by the self-knowledge modules."""
from __future__ import annotations

from dataclasses import dataclass

OWNER = "owner"
VIEWER = "viewer"

TOPIC_TIME = "time"
TOPIC_MODEL = "model"
TOPIC_GPU = "gpu"
TOPIC_RUNTIME = "runtime"
TOPIC_IDENTITY = "identity"
TOPIC_CAPABILITIES = "capabilities"
TOPIC_COMMANDS = "commands"
TOPIC_CREATOR = "creator"
TOPIC_INTERLOCUTOR = "interlocutor"

# Display/evaluation order of the topics.
ALL_TOPICS = (
    TOPIC_TIME,
    TOPIC_MODEL,
    TOPIC_GPU,
    TOPIC_RUNTIME,
    TOPIC_IDENTITY,
    TOPIC_CAPABILITIES,
    TOPIC_COMMANDS,
    TOPIC_CREATOR,
    TOPIC_INTERLOCUTOR,
)

# Topics a viewer turn may even look up. Model, GPU/VRAM, commands and the
# owner interlocutor are never fetched for viewers (T3 and settings privacy).
VIEWER_TOPICS = frozenset({TOPIC_TIME, TOPIC_IDENTITY, TOPIC_CAPABILITIES, TOPIC_CREATOR})


@dataclass(frozen=True)
class SelfFact:
    """One verified statement. ``audience`` is the narrowest audience allowed
    to see it: ``viewer`` facts are also visible to the owner."""

    topic: str
    text: str
    audience: str


@dataclass(frozen=True)
class SelfKnowledgeContext:
    """What a provider may depend on for one turn (never the user's text)."""

    audience: str
    source: str
    model: str = ""
    lang: str = "es"
