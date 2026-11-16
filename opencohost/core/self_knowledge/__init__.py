"""Self-knowledge retrieval: real facts about the app and the moment, fetched
only for the turn that asks and filtered by audience."""
from opencohost.core.self_knowledge.facts import (
    ALL_TOPICS,
    OWNER,
    VIEWER,
    VIEWER_TOPICS,
    SelfFact,
    SelfKnowledgeContext,
)
from opencohost.core.self_knowledge.filter import filter_facts
from opencohost.core.self_knowledge.gate import SelfKnowledgeGate

__all__ = [
    "ALL_TOPICS",
    "OWNER",
    "VIEWER",
    "VIEWER_TOPICS",
    "SelfFact",
    "SelfKnowledgeContext",
    "SelfKnowledgeGate",
    "filter_facts",
]
