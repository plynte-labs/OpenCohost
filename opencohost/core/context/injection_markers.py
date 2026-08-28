"""
opencohost/core/context/injection_markers.py

Prompt injection markers and string neutralization utilities.
Extracted from MotorVocalIA.
"""

from __future__ import annotations

# Max words allowed in a scout title (preamble filter rejects longer lines).
SCOUT_TITLE_MAX_WORDS = 6

# Prompt-injection marker floor (modest, not exhaustive — keyword lists don't
# scale). Shared by _sanitize_history_context (neutralizes via truncation) and
# the scout scrub (removes the phrase outright from the compact render).
INJECTION_MARKERS = (
    # English markers
    "ignore all previous",
    "you are now",
    "new system prompt",
    "pretend you are",
    "forget everything",
    "disregard previous",
    "do not follow",
    "your new role is",
    "you must now",
    "act as if",
    "from now on you are",
    # Spanish markers
    "olvida todo",
    "olvidá todo",
    "ignora todo",
    "ignorá todo",
    "ignora las instrucciones",
    "ignorá las instrucciones",
    "ahora eres",
    "ahora sos",
    "nuevo system prompt",
    "nuevo prompt de sistema",
    "haz de cuenta",
    "hacé de cuenta",
    "tu nuevo rol es",
    "no sigas",
    "no obedezcas",
    "actúa como",
    "actua como",
    "de ahora en adelante eres",
    "de ahora en más sos",
)


def _strip_injection_markers(text: str) -> str:
    """Remove INJECTION_MARKERS phrases outright, collapse whitespace.

    Shared by the scout scrub (_scout_scrub_text) and the memorias injection
    path (_build_memorias_injection_block): both surfaces re-render stored
    text into the prompt, so a marker phrase must be stripped, not merely
    truncated around (which _sanitize_history_context alone would do).
    """
    lowered = text.lower()
    for marker in INJECTION_MARKERS:
        idx = lowered.find(marker)
        while idx != -1:
            text = text[:idx] + text[idx + len(marker):]
            lowered = text.lower()
            idx = lowered.find(marker)
    return " ".join(text.split())
