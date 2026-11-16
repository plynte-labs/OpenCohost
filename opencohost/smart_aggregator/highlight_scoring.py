"""Highlight scoring for the chat reaction (chat-highlight-laya-scoring-20261111).

Pure functions, no I/O and no logging: viewer text goes in, numbers come out.
``heuristic_score`` is the scorer that used to live inside
``ChatReactionCore._select_highlight`` (same weights). On top of it:

* a floor (``highlight_min_score``): a window with nothing positive in it gets
  no highlight instead of its least-bad message;
* Laya (the local ``respond`` decision, shadow-only): a confident ``respond``
  adds a bonus, a confident ``ignore`` drops the candidate as spam. Laya's
  verdict arrives asynchronously and may be missing; missing means heuristic
  only, never an error.

Floor default: a message with no signal at all scores ``NEUTRAL_LENGTH_SCORE``
at best (only the sweet-spot length bonus), so the default floor is that plus
one: something must actually be positive (question, humor, emoji, digits,
interrogative word).
"""
from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Any, Optional

# The best a message with no signal can score: the 30..120 char length bonus.
NEUTRAL_LENGTH_SCORE = 3

LAYA_USED = "used"
LAYA_MISSING = "missing"
LAYA_FILTERED = "filtered"

_URL_PATTERNS = (
    "http://", "https://", "www.", ".com", ".net", ".org",
    ".gg/", ".tv/", ".live/", "youtu.be/", "twitch.tv/",
    "discord.gg/", "tiktok.com/",
)
_AD_PATTERNS = (
    "paga", "pagá", "pagame", "donación", "donacion", "dona",
    "subscribe", "suscribete", "suscríbete", "follow",
    "sígueme", "sigueme", "regalame", "regálame",
    "compra", "comprá", "vendo", "vendes", "barato",
    "promo", "sorteo", "giveaway", "sponsor",
    "bit.ly", "linktr.ee", "onlyfans",
)


@dataclass(frozen=True)
class HighlightConfig:
    highlight_min_score: int = NEUTRAL_LENGTH_SCORE + 1
    laya_respond_bonus: int = 20
    laya_respond_min_conf: float = 0.70
    laya_spam_min_conf: float = 0.85
    low_traffic_enabled: bool = True
    low_traffic_min_score: int = 20
    low_traffic_cooldown_s: float = 90.0

    @classmethod
    def from_dict(cls, raw: Any) -> "HighlightConfig":
        """Build from the ``chat_highlight`` YAML section; bad or missing
        values fall back to the defaults."""
        defaults = cls()
        if not isinstance(raw, dict):
            return defaults
        values = {}
        for f in fields(cls):
            if f.name not in raw:
                continue
            default = getattr(defaults, f.name)
            try:
                if isinstance(default, bool):
                    values[f.name] = bool(raw[f.name])
                else:
                    values[f.name] = type(default)(raw[f.name])
            except (TypeError, ValueError):
                continue
        return cls(**values)


def new_counters() -> dict:
    return {
        "laya_used": 0,
        "laya_missing": 0,
        "laya_filtered": 0,
        "floor_rejected": 0,
        "low_traffic_fired": 0,
    }


def is_unsafe(text: str, lowered: str) -> bool:
    """Links, @mentions and advertising are never highlighted."""
    if any(p in lowered for p in _URL_PATTERNS):
        return True
    if "@" in text:
        for w in lowered.split():
            if w.startswith("@") and len(w) > 1 and w[1].isalnum():
                return True
    return any(p in lowered for p in _AD_PATTERNS)


def heuristic_score(text: str) -> int:
    """The deterministic scorer. -1 disqualifies (length or unsafe)."""
    length = len(text)
    if length < 10 or length > 180:
        return -1
    lowered = text.lower()
    if is_unsafe(text, lowered):
        return -1

    score = 0
    if "?" in text or "¿" in text:
        score += 100
    emoji_count = sum(1 for c in text if ord(c) > 0x1F000)
    score += min(emoji_count * 10, 30)
    for marker in ("jaja", "jeje", "lol", "xd", "😂", "🤣", "😆", "jajaja"):
        if marker in lowered:
            score += 15
            break
    if any(c.isdigit() for c in text) and any(c.isalpha() for c in text):
        score += 8
    for marker in ("por qué", "cómo", "cuándo", "dónde", "quién"):
        if marker in lowered:
            score += 5
    if 30 <= length <= 120:
        score += NEUTRAL_LENGTH_SCORE
    return score


def combined_score(text: str, laya: Optional[tuple], cfg: HighlightConfig) -> tuple[int, str]:
    """Heuristic plus Laya. Returns ``(score, laya_state)``; a score of -1 is
    disqualified. A Laya verdict never rescues a disqualified candidate."""
    base = heuristic_score(text)
    label, conf = _parse_laya(laya)
    if label is None:
        return base, LAYA_MISSING
    if base < 0:
        return base, LAYA_USED
    if label == "ignore" and conf >= cfg.laya_spam_min_conf:
        return -1, LAYA_FILTERED
    if label == "respond" and conf >= cfg.laya_respond_min_conf:
        return base + cfg.laya_respond_bonus, LAYA_USED
    return base, LAYA_USED


def _parse_laya(laya: Any) -> tuple[Optional[str], float]:
    try:
        label, conf = laya
        if label in ("respond", "ignore") and not isinstance(conf, bool):
            return label, float(conf)
    except (TypeError, ValueError):
        pass
    return None, 0.0


def select_best(context: list, cfg: HighlightConfig, counters: Optional[dict] = None) -> Optional[dict]:
    """The best candidate of ``context`` at or above the floor, or None."""
    best, best_score = None, -1
    for msg in context or []:
        text = (msg.get("text") or "").strip()
        if not text:
            continue
        score, state = combined_score(msg.get("text", ""), msg.get("laya"), cfg)
        if counters is not None:
            key = {LAYA_USED: "laya_used", LAYA_MISSING: "laya_missing", LAYA_FILTERED: "laya_filtered"}[state]
            counters[key] += 1
        if best is None or score > best_score:
            best, best_score = msg, score
    if best is None or best_score < cfg.highlight_min_score:
        if counters is not None and best is not None:
            counters["floor_rejected"] += 1
        return None
    return best
