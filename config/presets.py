"""Creator Configuration Contract — zero-config defaults and named presets.

Every preset is a callable that returns a complete, valid ``CreatorConfig``.
``default_config()`` is the zero-config safe fallback; the 4 named presets
are opinionated bundles for common stream types.
"""

from __future__ import annotations

from config.schema import (
    ActionPolicy,
    AudienceScale,
    AwayBehavior,
    ChatEvent,
    CreatorConfig,
    CreatorPolicy,
    CreatorTone,
    EventAction,
    InterventionType,
    ModePolicy,
    NonNegotiableRule,
    ScalePolicy,
    UserNamePolicy,
)


# ══════════════════════════════════════════════════════════════════════════════
# 10 Non-Negotiable Rules (shared by all configs)
# ══════════════════════════════════════════════════════════════════════════════

_NON_NEGOTIABLES: tuple[NonNegotiableRule, ...] = (
    NonNegotiableRule(
        id="no_doxxing",
        description="No doxxing in voice output",
    ),
    NonNegotiableRule(
        id="no_suspicious_links",
        description="No suspicious links in voice output",
    ),
    NonNegotiableRule(
        id="never_promise",
        description="Kira never promises things for streamer",
    ),
    NonNegotiableRule(
        id="never_invent_confirmations",
        description="Kira never invents confirmations",
    ),
    NonNegotiableRule(
        id="never_moderate_automatically",
        description="Kira never moderates automatically (flag only)",
    ),
    NonNegotiableRule(
        id="no_personal_viewer_data",
        description="No storing personal viewer data by default",
    ),
    NonNegotiableRule(
        id="no_raw_spam_to_llm",
        description="No raw spam sent to LLM (always filtered first)",
    ),
    NonNegotiableRule(
        id="no_hate_speech",
        description="No hate speech or slurs",
    ),
    NonNegotiableRule(
        id="no_ai_self_identification",
        description="No self-identification as AI/LLM/chatbot",
    ),
    NonNegotiableRule(
        id="no_meta_commentary",
        description="No meta-commentary about audience engagement",
    ),
    NonNegotiableRule(
        id="no_negative_engagement",
        description="No negative commentary about stream energy, emotion, boredom, or silence",
    ),
)


# ══════════════════════════════════════════════════════════════════════════════
# Default per-event action table (zero-config)
# ══════════════════════════════════════════════════════════════════════════════

def _default_action_policy() -> ActionPolicy:
    """Zero-config defaults:
    - questions → panel
    - greetings → ignore
    - tech alerts → panel + voice
    - spam → ignore
    - moderation → panel only, NEVER voice
    """
    return ActionPolicy(
        direct_question=EventAction(
            voice_allowed=False, surface_to_ui=True, ignore=False, priority="high"
        ),
        viewer_request=EventAction(
            voice_allowed=False, surface_to_ui=True, ignore=False, priority="medium"
        ),
        poll_or_vote_suggestion=EventAction(
            voice_allowed=False, surface_to_ui=True, ignore=False, priority="low"
        ),
        greeting_or_shoutout=EventAction(
            voice_allowed=False, surface_to_ui=True, ignore=True, priority="low"
        ),
        correction_or_clarification=EventAction(
            voice_allowed=True, surface_to_ui=True, ignore=False, priority="high"
        ),
        repeated_topic=EventAction(
            voice_allowed=False, surface_to_ui=True, ignore=False, priority="medium"
        ),
        joke_or_meme=EventAction(
            voice_allowed=False, surface_to_ui=True, ignore=True, priority="low"
        ),
        hype_or_emotion=EventAction(
            voice_allowed=True, surface_to_ui=True, ignore=False, priority="medium"
        ),
        complaint_or_confusion=EventAction(
            voice_allowed=False, surface_to_ui=True, ignore=False, priority="high"
        ),
        moderation_or_risk=EventAction(
            voice_allowed=False, surface_to_ui=True, ignore=False, priority="high"
        ),
        factual_update=EventAction(
            voice_allowed=True, surface_to_ui=True, ignore=False, priority="medium"
        ),
        # low_signal_noise uses frozen default: voice=False, ui=False, ignore=True
    )


# ══════════════════════════════════════════════════════════════════════════════
# default_config — zero-config safe fallback
# ══════════════════════════════════════════════════════════════════════════════


def default_config() -> CreatorConfig:
    """Return a zero-config CreatorConfig with balanced, safe defaults.

    Defaults:
      - questions → panel
      - greetings → ignore (<30 viewers)
      - tech_alerts → panel + voice
      - usernames → no
      - interruption → medium (0.5)
      - All 10 non-negotiables active
    """
    return CreatorConfig(
        creator=CreatorPolicy(
            tone=CreatorTone.BALANCED.value,
            formality=0.5,
            humor_level=0.5,
            caution_level=0.5,
            max_response_length=120,
            intervention_type=InterventionType.MODERATE.value,
            use_usernames=False,
            factuality_strictness=0.5,
        ),
        mode=ModePolicy(
            preset_name="default",
            interruption_threshold=0.5,
            response_frequency="medium",
        ),
        action=_default_action_policy(),
        scale=ScalePolicy(
            audience_scale=AudienceScale.SMALL.value,
            max_messages=50,
            dedup_window=30,
        ),
        non_negotiables=_NON_NEGOTIABLES,
    )


# ══════════════════════════════════════════════════════════════════════════════
# 4 Named Presets
# ══════════════════════════════════════════════════════════════════════════════


def preset_comunidad() -> CreatorConfig:
    """Warm, casual community-building preset.

    - tone=casual, moderate humor, low factuality
    - greetings → voice (scale-gated < 30 viewers)
    - questions → voice + panel
    """
    return CreatorConfig(
        creator=CreatorPolicy(
            tone=CreatorTone.CASUAL.value,
            formality=0.3,
            humor_level=0.5,
            caution_level=0.5,
            max_response_length=120,
            intervention_type=InterventionType.HIGH.value,
            use_usernames=True,
            factuality_strictness=0.3,
        ),
        mode=ModePolicy(
            preset_name="comunidad",
            interruption_threshold=0.6,
            response_frequency="high",
        ),
        action=ActionPolicy(
            direct_question=EventAction(
                voice_allowed=True, surface_to_ui=True, ignore=False, priority="high"
            ),
            viewer_request=EventAction(
                voice_allowed=True, surface_to_ui=True, ignore=False, priority="medium"
            ),
            poll_or_vote_suggestion=EventAction(
                voice_allowed=True, surface_to_ui=True, ignore=False, priority="low"
            ),
            greeting_or_shoutout=EventAction(
                voice_allowed=True, surface_to_ui=True, ignore=False, priority="medium"
            ),
            correction_or_clarification=EventAction(
                voice_allowed=True, surface_to_ui=True, ignore=False, priority="high"
            ),
            repeated_topic=EventAction(
                voice_allowed=False, surface_to_ui=True, ignore=False, priority="medium"
            ),
            joke_or_meme=EventAction(
                voice_allowed=False, surface_to_ui=True, ignore=False, priority="low"
            ),
            hype_or_emotion=EventAction(
                voice_allowed=True, surface_to_ui=True, ignore=False, priority="medium"
            ),
            complaint_or_confusion=EventAction(
                voice_allowed=False, surface_to_ui=True, ignore=False, priority="high"
            ),
            moderation_or_risk=EventAction(
                voice_allowed=False, surface_to_ui=True, ignore=False, priority="high"
            ),
            factual_update=EventAction(
                voice_allowed=False, surface_to_ui=True, ignore=False, priority="medium"
            ),
        ),
        scale=ScalePolicy(
            audience_scale=AudienceScale.SMALL.value,
            max_messages=60,
            dedup_window=30,
        ),
        non_negotiables=_NON_NEGOTIABLES,
    )


def preset_show() -> CreatorConfig:
    """High-energy entertainment/show preset.

    - tone=energetic, high humor, low caution/factuality
    - jokes → voice, hype → voice
    """
    return CreatorConfig(
        creator=CreatorPolicy(
            tone=CreatorTone.ENERGETIC.value,
            formality=0.2,
            humor_level=0.9,
            caution_level=0.3,
            max_response_length=80,
            intervention_type=InterventionType.HIGH.value,
            use_usernames=True,
            factuality_strictness=0.2,
        ),
        mode=ModePolicy(
            preset_name="show",
            interruption_threshold=0.3,
            response_frequency="high",
        ),
        action=ActionPolicy(
            direct_question=EventAction(
                voice_allowed=True, surface_to_ui=True, ignore=False, priority="high"
            ),
            viewer_request=EventAction(
                voice_allowed=True, surface_to_ui=True, ignore=False, priority="medium"
            ),
            poll_or_vote_suggestion=EventAction(
                voice_allowed=True, surface_to_ui=True, ignore=False, priority="medium"
            ),
            greeting_or_shoutout=EventAction(
                voice_allowed=True, surface_to_ui=True, ignore=False, priority="medium"
            ),
            correction_or_clarification=EventAction(
                voice_allowed=False, surface_to_ui=True, ignore=False, priority="medium"
            ),
            repeated_topic=EventAction(
                voice_allowed=True, surface_to_ui=True, ignore=False, priority="medium"
            ),
            joke_or_meme=EventAction(
                voice_allowed=True, surface_to_ui=True, ignore=False, priority="high"
            ),
            hype_or_emotion=EventAction(
                voice_allowed=True, surface_to_ui=True, ignore=False, priority="high"
            ),
            complaint_or_confusion=EventAction(
                voice_allowed=False, surface_to_ui=True, ignore=False, priority="medium"
            ),
            moderation_or_risk=EventAction(
                voice_allowed=False, surface_to_ui=True, ignore=False, priority="high"
            ),
            factual_update=EventAction(
                voice_allowed=False, surface_to_ui=True, ignore=False, priority="low"
            ),
        ),
        scale=ScalePolicy(
            audience_scale=AudienceScale.MEDIUM.value,
            max_messages=100,
            dedup_window=15,
        ),
        non_negotiables=_NON_NEGOTIABLES,
    )


def preset_tecnico() -> CreatorConfig:
    """Technical/educational preset.

    - tone=balanced, low humor, high caution/factuality
    - corrections → voice, no greetings, usernames=false
    """
    return CreatorConfig(
        creator=CreatorPolicy(
            tone=CreatorTone.BALANCED.value,
            formality=0.7,
            humor_level=0.3,
            caution_level=0.7,
            max_response_length=200,
            intervention_type=InterventionType.MODERATE.value,
            use_usernames=False,
            factuality_strictness=0.95,
        ),
        mode=ModePolicy(
            preset_name="tecnico",
            interruption_threshold=0.4,
            response_frequency="medium",
        ),
        action=ActionPolicy(
            direct_question=EventAction(
                voice_allowed=True, surface_to_ui=True, ignore=False, priority="high"
            ),
            viewer_request=EventAction(
                voice_allowed=False, surface_to_ui=True, ignore=False, priority="medium"
            ),
            poll_or_vote_suggestion=EventAction(
                voice_allowed=False, surface_to_ui=True, ignore=False, priority="low"
            ),
            greeting_or_shoutout=EventAction(
                voice_allowed=False, surface_to_ui=True, ignore=True, priority="low"
            ),
            correction_or_clarification=EventAction(
                voice_allowed=True, surface_to_ui=True, ignore=False, priority="high"
            ),
            repeated_topic=EventAction(
                voice_allowed=False, surface_to_ui=True, ignore=False, priority="medium"
            ),
            joke_or_meme=EventAction(
                voice_allowed=False, surface_to_ui=True, ignore=True, priority="low"
            ),
            hype_or_emotion=EventAction(
                voice_allowed=False, surface_to_ui=True, ignore=True, priority="low"
            ),
            complaint_or_confusion=EventAction(
                voice_allowed=False, surface_to_ui=True, ignore=False, priority="high"
            ),
            moderation_or_risk=EventAction(
                voice_allowed=False, surface_to_ui=True, ignore=False, priority="high"
            ),
            factual_update=EventAction(
                voice_allowed=True, surface_to_ui=True, ignore=False, priority="high"
            ),
        ),
        scale=ScalePolicy(
            audience_scale=AudienceScale.SMALL.value,
            max_messages=40,
            dedup_window=60,
        ),
        non_negotiables=_NON_NEGOTIABLES,
    )


def preset_calmado() -> CreatorConfig:
    """Relaxed, low-intervention preset.

    - tone=casual, moderate humor, moderate caution
    - only high-confidence (>0.7) questions → voice
    - no jokes/hype via voice
    """
    return CreatorConfig(
        creator=CreatorPolicy(
            tone=CreatorTone.CASUAL.value,
            formality=0.3,
            humor_level=0.3,
            caution_level=0.5,
            max_response_length=120,
            intervention_type=InterventionType.LOW.value,
            use_usernames=True,
            factuality_strictness=0.5,
        ),
        mode=ModePolicy(
            preset_name="calmado",
            interruption_threshold=0.8,
            response_frequency="low",
        ),
        action=ActionPolicy(
            direct_question=EventAction(
                voice_allowed=True, surface_to_ui=True, ignore=False, priority="high"
            ),
            viewer_request=EventAction(
                voice_allowed=False, surface_to_ui=True, ignore=False, priority="medium"
            ),
            poll_or_vote_suggestion=EventAction(
                voice_allowed=False, surface_to_ui=True, ignore=False, priority="low"
            ),
            greeting_or_shoutout=EventAction(
                voice_allowed=False, surface_to_ui=True, ignore=False, priority="low"
            ),
            correction_or_clarification=EventAction(
                voice_allowed=False, surface_to_ui=True, ignore=False, priority="medium"
            ),
            repeated_topic=EventAction(
                voice_allowed=False, surface_to_ui=True, ignore=False, priority="medium"
            ),
            joke_or_meme=EventAction(
                voice_allowed=False, surface_to_ui=True, ignore=False, priority="low"
            ),
            hype_or_emotion=EventAction(
                voice_allowed=False, surface_to_ui=True, ignore=False, priority="low"
            ),
            complaint_or_confusion=EventAction(
                voice_allowed=False, surface_to_ui=True, ignore=False, priority="high"
            ),
            moderation_or_risk=EventAction(
                voice_allowed=False, surface_to_ui=True, ignore=False, priority="high"
            ),
            factual_update=EventAction(
                voice_allowed=False, surface_to_ui=True, ignore=False, priority="medium"
            ),
        ),
        scale=ScalePolicy(
            audience_scale=AudienceScale.SMALL.value,
            max_messages=30,
            dedup_window=60,
        ),
        non_negotiables=_NON_NEGOTIABLES,
    )


# ══════════════════════════════════════════════════════════════════════════════
# Preset registry (name → factory function)
# ══════════════════════════════════════════════════════════════════════════════

_PRESET_REGISTRY: dict[str, callable] = {
    "default": default_config,
    "comunidad": preset_comunidad,
    "show": preset_show,
    "tecnico": preset_tecnico,
    "calmado": preset_calmado,
}


def load_preset(name: str) -> CreatorConfig:
    """Load a named preset. Unknown names fall back to ``default_config()``."""
    factory = _PRESET_REGISTRY.get(name)
    if factory is None:
        factory = default_config
    return factory()


def duplicate_preset(name: str, new_name: str) -> CreatorConfig:
    """Create a copy of a preset with a new name. Values are identical."""
    config = load_preset(name)
    return CreatorConfig(
        creator=CreatorPolicy(**config.creator.to_dict()),
        mode=ModePolicy(
            preset_name=new_name,
            interruption_threshold=config.mode.interruption_threshold,
            response_frequency=config.mode.response_frequency,
        ),
        action=config.action,  # frozen, safe to share
        scale=ScalePolicy(**config.scale.to_dict()),
        non_negotiables=config.non_negotiables,
    )
