"""Rule-based topic suggestion generator for Kira Co-host Agenda.

Pure data-transform module — zero LLM, zero I/O, zero coupling to agenda state.
Consumes intent summaries, snapshots, and vibe temperature; produces 1–3
DRAFTED-ready suggestion dicts ranked by confidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Optional

from opencohost.i18n import active as i18n_active

# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------


@dataclass
class TopicSuggestion:
    """One suggested agenda topic, ready for controller ingestion."""

    title: str
    angle: str
    confidence: str  # "HIGH" | "MEDIUM" | "LOW"
    source: str  # "entity:<name>" | "vibe" | "transition"


# ---------------------------------------------------------------------------
# Templates by entity category
# ---------------------------------------------------------------------------
#
# Migrated to `nlp.topic_templates.*` (P4, kira_bilingual_e2e) --
# i18n_active.topic_templates() returns the es legacy dict (module-level
# constants above the accessor in opencohost/i18n/active.py) on any i18n
# failure, so this module keeps zero hard dependency on i18n loading
# succeeding. Category keys are engine identifiers (locale-neutral); the
# {entity} placeholder is preserved.


def _pick_template(entity: str, intent: str | None = None) -> tuple[str, str]:
    """Choose a title+angle template appropriate for the entity and intent."""
    entity_lower = entity.lower() if entity else ""
    templates_by_category = i18n_active.topic_templates()
    templates: list[tuple[str, str]]

    if intent in ("game_suggestion", "game_question"):
        templates = templates_by_category["game"]
    elif intent in ("trade_request",):
        templates = templates_by_category["trade"]
    elif intent in ("video_collab",):
        templates = templates_by_category["collab"]
    elif _looks_like_game(entity_lower):
        templates = templates_by_category["game"]
    else:
        templates = templates_by_category["generic"]

    # Deterministic pick: use entity hash so same entity → same template
    idx = hash(entity) % len(templates)
    return templates[idx]


def _looks_like_game(entity: str) -> bool:
    """Heuristic: common game-related tokens in entity name."""
    game_tokens = {"roblox", "minecraft", "fortnite", "among", "valorant", "league", "fifa", "cod", "gta", "server", "mod", "mods", "shader", "texture", "skin", "mapa", "mundo"}
    return any(token in entity for token in game_tokens)


# ---------------------------------------------------------------------------
# Confidence scoring
# ---------------------------------------------------------------------------

_CONFIDENCE_ORDER: dict[str, int] = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}


def compute_confidence(entity_count: int, vibe_temperature: float) -> str:
    """Map entity frequency × vibe temperature to a confidence tier.

    Returns "HIGH", "MEDIUM", or "LOW".
    """
    if entity_count < 0:
        entity_count = 0
    if vibe_temperature < 0:
        vibe_temperature = 0

    raw = (entity_count / max(1, 8)) * (vibe_temperature / max(1, 50))
    if raw >= 0.8:
        return "HIGH"
    if raw >= 0.4:
        return "MEDIUM"
    return "LOW"


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------

_SIMILARITY_THRESHOLD = 0.75


def is_duplicate(candidate_title: str, existing_titles_lower: set[str]) -> bool:
    """True if any existing title is too similar to the candidate."""
    if not candidate_title or not existing_titles_lower:
        return False
    candidate_lower = candidate_title.lower().strip()
    for existing in existing_titles_lower:
        if SequenceMatcher(None, candidate_lower, existing).ratio() > _SIMILARITY_THRESHOLD:
            return True
    return False


# ---------------------------------------------------------------------------
# Main generation function
# ---------------------------------------------------------------------------


def generate_suggestions(
    intent_summary: dict | None,
    snapshots: list[dict] | None,
    vibe_temperature: float,
    existing_topics: list | None,
    last_outputs: list[str] | None = None,
) -> list[dict]:
    """Generate 0–3 DRAFTED-ready topic suggestions from aggregated context.

    Zero LLM — pure rule-based extraction from intents and snapshots.

    Args:
        intent_summary: IntentAggregator.summarize() output dict.
        snapshots: SessionHistory.get_recent_context_snapshots() result.
        vibe_temperature: VibeThermometer.compute_vibe()["temperature"] (0–100).
        existing_topics: All existing AgendaTopic objects (any status).
        last_outputs: Controller last_outputs (anti-loop buffer, unused currently).

    Returns:
        List of suggestion dicts with keys: title, angle, confidence, source.
    """
    suggestions: list[dict] = []
    existing_titles_lower: set[str] = set()

    # Collect existing titles for dedup (any status)
    if existing_topics:
        for topic in existing_topics:
            title = getattr(topic, "title", "")
            if title and title.strip():
                existing_titles_lower.add(title.strip().lower())

    # Source A: intent entities → topic ideas
    if intent_summary:
        top_intents: list[dict] = intent_summary.get("top_intents", [])
        for intent_item in top_intents:
            intent = intent_item.get("intent", "")
            entities: list[str] = intent_item.get("entities", [])
            count: int = intent_item.get("count", 0)
            for entity in entities:
                entity = (entity or "").strip()
                if not entity or len(entity) < 2:
                    continue
                confidence = compute_confidence(count, vibe_temperature)
                title_tmpl, angle_tmpl = _pick_template(entity, intent)
                title = title_tmpl.replace("{entity}", entity)
                angle = angle_tmpl.replace("{entity}", entity)
                if not is_duplicate(title, existing_titles_lower):
                    suggestions.append({
                        "title": title,
                        "angle": angle,
                        "confidence": confidence,
                        "source": f"entity:{entity}",
                    })
                    existing_titles_lower.add(title.lower())

    # Source B: vibe-only when chat is hot but no entity-driven suggestions
    if not suggestions and vibe_temperature >= 70:
        for tmpl in i18n_active.topic_templates()["vibe_high"]:
            title = tmpl[0]
            angle = tmpl[1]
            if not is_duplicate(title, existing_titles_lower):
                suggestions.append({
                    "title": title,
                    "angle": angle,
                    "confidence": "MEDIUM",
                    "source": "vibe",
                })
                existing_titles_lower.add(title)
                break  # one vibe suggestion is enough

    # Source C: transition from last completed topic (only when there IS one)
    if not suggestions:
        last_completed = _find_last_completed(existing_topics)
        if last_completed is not None:
            title_tmpl, angle_tmpl = i18n_active.topic_templates()["transition"][0]
            title = title_tmpl
            angle = angle_tmpl
            if not is_duplicate(title, existing_titles_lower):
                transition_confidence = "MEDIUM" if vibe_temperature >= 40 else "LOW"
                suggestions.append({
                    "title": title,
                    "angle": angle,
                    "confidence": transition_confidence,
                    "source": "transition",
                })

    # Sort by confidence DESC, cap at 3
    suggestions.sort(key=lambda s: _CONFIDENCE_ORDER.get(s["confidence"], 3))
    return suggestions[:3]


def _find_last_completed(topics: list | None) -> Optional:
    """Find the most recently COMPLETED topic from the topics list."""
    if not topics:
        return None
    for topic in reversed(list(topics)):
        if getattr(topic, "status", None) and str(getattr(topic, "status", "")) == "completed":
            return topic
    return None
