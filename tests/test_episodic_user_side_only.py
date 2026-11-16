"""S2 (release-050-stabilization): episodic recall renders the user side only.

Kira's own text is generated, not observed (fabrications included). Re-injecting
it as a "past chat" memory would let an invented claim return as fact.
Synthetic data only.
"""
from __future__ import annotations

import re
from types import SimpleNamespace

from opencohost.core.memory import episodic_merge
from opencohost.core.memory_v5_shadow.episodic_recall import (
    RetrievedEpisodeContext,
    _format_episodic_block,
)

FABRICATED = "I personally flew the Zephyr ZX-9 rocket to Mars yesterday"
USER = "my cat Pixel hates the vacuum cleaner when it runs at night"


def _exchange(user=USER, asst=FABRICATED):
    return SimpleNamespace(
        user_text=user, assistant_text=asst, user_event_id="u1", assistant_event_id="a1"
    )


def _episode(exchanges=None):
    return SimpleNamespace(
        started_at="2026-11-01T10:00:00Z",
        exchanges=exchanges or [_exchange()],
        best_exchange_key="u1:a1",
    )


def test_line_has_user_words_and_no_assistant_text():
    line = episodic_merge.format_episode_line(_episode(), 300)
    assert re.match(r"\[past chat 2026-11-0[12], [^\]]+\] ", line)  # local date + age (T4)
    assert "Pixel" in line
    assert "Zephyr" not in line and "Mars" not in line
    assert "Kira" not in line and "→" not in line


def test_fabricated_assistant_text_is_never_injected_into_the_lines():
    lines = ["a fact line"]
    n = episodic_merge.append_episode_lines(lines, [_episode()], max_chars=900, max_episodes=2)
    assert n == 1
    assert all("Zephyr" not in ln and "Mars" not in ln for ln in lines)


def test_empty_user_side_yields_no_line_even_with_assistant_text():
    ep = _episode([_exchange(user="   ", asst=FABRICATED)])
    assert episodic_merge.format_episode_line(ep, 300) == ""


def test_user_side_gets_the_whole_room_clipped_to_the_limit():
    long_user = "word " * 200
    ep = _episode([_exchange(user=long_user)])
    line = episodic_merge.format_episode_line(ep, 120)
    assert 0 < len(line) <= 120


def test_standalone_episodic_block_also_omits_assistant_text():
    ctx = RetrievedEpisodeContext(
        episode_id="ep123456789",
        session_id="sess123456789",
        started_at="2026-11-01T10:00:00Z",
        composite_score=0.9,
        exchanges=[_exchange()],
    )
    block = _format_episodic_block([ctx])
    assert "Pixel" in block
    assert "Zephyr" not in block and "Kira:" not in block
