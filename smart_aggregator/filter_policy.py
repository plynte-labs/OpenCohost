from types import MappingProxyType
from typing import Optional

_PRESETS = {
    "balanced": {
        "min_words": 3,
        "min_char_length": 10,
        "min_quality_score": 0.5,
        "discard_emojis_only": True,
        "discard_links": True,
        "discard_mentions": True,
        "discard_repetitive_chars": True,
        "discard_repeated_words": True,
        "discard_gibberish": True,
        "discard_ascii_art": True,
    },
    "twitch_relaxed": {
        "min_words": 1,
        "min_char_length": 1,
        "min_quality_score": 0.0,
        "discard_emojis_only": True,
        "discard_links": True,
        "discard_mentions": True,
        "discard_repetitive_chars": True,
        "discard_repeated_words": True,
        "discard_gibberish": True,
        "discard_ascii_art": True,
    },
    "strict": {
        "min_words": 4,
        "min_char_length": 15,
        "min_quality_score": 0.6,
        "discard_emojis_only": True,
        "discard_links": True,
        "discard_mentions": True,
        "discard_repetitive_chars": True,
        "discard_repeated_words": True,
        "discard_gibberish": True,
        "discard_ascii_art": True,
    },
}

PRESETS = MappingProxyType(_PRESETS)

BUILTIN_PRESETS = frozenset(PRESETS.keys())


def get_preset(preset_name: str) -> Optional[dict]:
    preset = PRESETS.get(preset_name)
    return dict(preset) if preset is not None else None


def list_presets() -> frozenset:
    return BUILTIN_PRESETS
