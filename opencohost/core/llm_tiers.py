"""Manual LLM tier configuration and active-tier state."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


LLM_TIERS = ("quality", "balanced", "fast")
LLM_TIER_LABELS = {
    "quality": "Quality",
    "balanced": "Balanced",
    "fast": "Fast",
}


@dataclass(frozen=True)
class LLMTierConfig:
    """Model slots for manual Quality/Balanced/Fast routing."""

    quality: Optional[str]
    balanced: Optional[str]
    fast: Optional[str]

    def model_for(self, tier: str) -> Optional[str]:
        """Return the configured model tag for *tier*, or None if unavailable."""
        if tier not in LLM_TIERS:
            return None
        model = getattr(self, tier)
        if not model or not str(model).strip():
            return None
        return str(model).strip()

    def is_available(self, tier: str) -> bool:
        """Return whether *tier* has a configured model slot."""
        return self.model_for(tier) is not None

    def first_available_tier(self) -> Optional[str]:
        """Return the first configured tier using Quality, Balanced, Fast order."""
        for tier in LLM_TIERS:
            if self.is_available(tier):
                return tier
        return None

    def as_dict(self) -> dict[str, Optional[str]]:
        """Return a serializable tier-to-model mapping."""
        return {tier: self.model_for(tier) for tier in LLM_TIERS}


@dataclass
class LLMTierState:
    """Active manual LLM tier and resolved model."""

    config: LLMTierConfig
    active_tier: str = "balanced"

    def __post_init__(self) -> None:
        if not self.config.is_available(self.active_tier):
            fallback = self.config.first_available_tier()
            if fallback is not None:
                self.active_tier = fallback

    @property
    def active_model(self) -> Optional[str]:
        """Return the active tier's configured model tag."""
        return self.config.model_for(self.active_tier)

    def switch_to(self, tier: str) -> Optional[str]:
        """Set active tier and return its model, or None when unavailable."""
        model = self.config.model_for(tier)
        if model is None:
            return None
        self.active_tier = tier
        return model
