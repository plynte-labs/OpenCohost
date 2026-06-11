"""Unit tests for the rule-based TopicSuggester module."""

import pytest

from opencohost.smart_aggregator.topic_suggester import (
    TopicSuggestion,
    compute_confidence,
    generate_suggestions,
    is_duplicate,
)


# ── helpers ──────────────────────────────────────────────────────────────────

def _make_topic(title: str, status: str = "completed") -> object:
    """Minimal AgendaTopic-alike for tests that only need .title and .status."""
    class FakeTopic:
        def __init__(self, t, s):
            self.title = t
            self.status = s

    return FakeTopic(title, status)


def _rich_intent_summary() -> dict:
    return {
        "total_messages": 30,
        "top_intents": [
            {
                "intent": "game_suggestion",
                "label": "sugerencias de juegos",
                "count": 8,
                "entities": ["mods", "minecraft", "shaders"],
            },
            {
                "intent": "trade_request",
                "label": "tradeos",
                "count": 4,
                "entities": ["garama", "og"],
            },
            {
                "intent": "greeting_request",
                "label": "saludos",
                "count": 12,
                "entities": [],
            },
        ],
    }


def _thin_intent_summary() -> dict:
    return {
        "total_messages": 3,
        "top_intents": [
            {"intent": "greeting_request", "label": "saludos", "count": 2, "entities": []},
        ],
    }


# ── compute_confidence ───────────────────────────────────────────────────────

def test_compute_confidence_high():
    assert compute_confidence(8, 72) == "HIGH"


def test_compute_confidence_medium():
    assert compute_confidence(4, 60) == "MEDIUM"


def test_compute_confidence_low():
    assert compute_confidence(1, 20) == "LOW"


def test_compute_confidence_zero_vibe():
    assert compute_confidence(10, 0) == "LOW"


def test_compute_confidence_zero_entities():
    assert compute_confidence(0, 80) == "LOW"


def test_compute_confidence_negative_inputs_safe():
    """Negative inputs should not crash — clamped internally."""
    assert compute_confidence(-1, -1) == "LOW"


# ── is_duplicate ─────────────────────────────────────────────────────────────

def test_is_duplicate_exact_match():
    assert is_duplicate("Mods en Minecraft", {"mods en minecraft"}) is True


def test_is_duplicate_near_match():
    """SequenceMatcher should catch high-similarity variants."""
    existing = {"mods para minecraft"}
    assert is_duplicate("Mods en Minecraft", existing) is True


def test_is_duplicate_distinct_title():
    existing = {"java vs bedrock"}
    assert is_duplicate("Shaders vs texturas vanilla", existing) is False


def test_is_duplicate_empty_candidate():
    assert is_duplicate("", {"algo"}) is False


def test_is_duplicate_empty_existing():
    assert is_duplicate("Mods en Minecraft", set()) is False


def test_is_duplicate_below_threshold():
    """Titles with low similarity should pass."""
    existing = {"comida peruana en stream"}
    assert is_duplicate("Shaders y rendimiento en Roblox", existing) is False


# ── generate_suggestions — rich context ──────────────────────────────────────

def test_rich_context_produces_suggestions():
    suggestions = generate_suggestions(
        intent_summary=_rich_intent_summary(),
        snapshots=[],
        vibe_temperature=72,
        existing_topics=[],
    )

    assert len(suggestions) >= 1
    assert all("title" in s and "angle" in s and "confidence" in s and "source" in s for s in suggestions)


def test_rich_context_includes_high_confidence():
    suggestions = generate_suggestions(
        intent_summary=_rich_intent_summary(),
        snapshots=[],
        vibe_temperature=72,
        existing_topics=[],
    )

    confidences = {s["confidence"] for s in suggestions}
    assert "HIGH" in confidences or "MEDIUM" in confidences


def test_suggestions_capped_at_three():
    suggestions = generate_suggestions(
        intent_summary=_rich_intent_summary(),
        snapshots=[],
        vibe_temperature=80,
        existing_topics=[],
    )

    assert len(suggestions) <= 3


def test_suggestions_sorted_by_confidence_desc():
    order = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
    suggestions = generate_suggestions(
        intent_summary=_rich_intent_summary(),
        snapshots=[],
        vibe_temperature=80,
        existing_topics=[],
    )

    for i in range(len(suggestions) - 1):
        assert order[suggestions[i]["confidence"]] <= order[suggestions[i + 1]["confidence"]]


def test_source_is_entity_for_intent_driven():
    suggestions = generate_suggestions(
        intent_summary=_rich_intent_summary(),
        snapshots=[],
        vibe_temperature=80,
        existing_topics=[],
    )

    for s in suggestions:
        if s["source"].startswith("entity:"):
            assert len(s["source"]) > len("entity:")


# ── generate_suggestions — thin context ──────────────────────────────────────

def test_thin_context_low_confidence():
    suggestions = generate_suggestions(
        intent_summary=_thin_intent_summary(),
        snapshots=[],
        vibe_temperature=20,
        existing_topics=[],
    )

    # No entities with entities list → may produce transition or empty
    # When no intents have entities and vibe is low, should be sparse
    assert len(suggestions) <= 1
    if suggestions:
        assert suggestions[0]["confidence"] in ("LOW", "MEDIUM")


def test_empty_intents_returns_empty():
    empty_summary = {"total_messages": 0, "top_intents": []}
    suggestions = generate_suggestions(
        intent_summary=empty_summary,
        snapshots=[],
        vibe_temperature=0,
        existing_topics=[],
    )
    assert suggestions == []


def test_none_intent_summary_returns_empty_at_zero_vibe():
    suggestions = generate_suggestions(
        intent_summary=None,
        snapshots=[],
        vibe_temperature=0,
        existing_topics=[],
    )
    assert suggestions == []


# ── generate_suggestions — dedup ─────────────────────────────────────────────

def test_duplicate_title_discarded():
    existing = [_make_topic("Mods en Minecraft: ¿moda o está para quedarse?", "completed")]
    suggestions = generate_suggestions(
        intent_summary=_rich_intent_summary(),
        snapshots=[],
        vibe_temperature=72,
        existing_topics=existing,
    )

    titles_lower = {s["title"].lower() for s in suggestions}
    assert "mods en minecraft: ¿moda o está para quedarse?" not in titles_lower


def test_near_duplicate_discarded():
    """A title similar to an existing topic via SequenceMatcher is deduped."""
    # Use a title very close to what the template would produce for "mods"
    existing = [_make_topic("mods: moda pasajera o acá para quedarse", "queued")]
    suggestions = generate_suggestions(
        intent_summary=_rich_intent_summary(),
        snapshots=[],
        vibe_temperature=72,
        existing_topics=existing,
    )

    titles_lower = {s["title"].lower() for s in suggestions}
    # The entity "mods" template might produce this exact title → deduped
    assert "mods: ¿moda pasajera o acá para quedarse?" not in titles_lower


# ── generate_suggestions — transition source ─────────────────────────────────

def test_transition_from_last_completed():
    last = _make_topic("Java vs Bedrock: ¿cuál es mejor?", "completed")
    suggestions = generate_suggestions(
        intent_summary={"total_messages": 0, "top_intents": []},
        snapshots=[],
        vibe_temperature=45,
        existing_topics=[last],
    )

    # When no intents but vibe > 0 and there's a completed topic, transition is produced
    if suggestions:
        assert suggestions[0]["source"] == "transition"
        assert suggestions[0]["confidence"] in ("MEDIUM", "LOW")


# ── generate_suggestions — vibe-only source ──────────────────────────────────

def test_vibe_high_produces_vibe_suggestion():
    suggestions = generate_suggestions(
        intent_summary={"total_messages": 0, "top_intents": []},
        snapshots=[],
        vibe_temperature=85,
        existing_topics=[],
    )

    assert len(suggestions) >= 1
    assert suggestions[0]["source"] == "vibe"


# ── generate_suggestions — deterministic ─────────────────────────────────────

def test_same_input_produces_same_output():
    summary = _rich_intent_summary()
    existing = [_make_topic("tema viejo", "completed")]

    result_a = generate_suggestions(summary, [], 72, existing)
    result_b = generate_suggestions(summary, [], 72, existing)

    assert result_a == result_b


# ── TopicSuggestion dataclass ────────────────────────────────────────────────

def test_topic_suggestion_dataclass():
    ts = TopicSuggestion(
        title="Shaders vs texturas vanilla",
        angle="Comparar pros y contras",
        confidence="HIGH",
        source="entity:shaders",
    )
    assert ts.title == "Shaders vs texturas vanilla"
    assert ts.angle == "Comparar pros y contras"
    assert ts.confidence == "HIGH"
    assert ts.source == "entity:shaders"
