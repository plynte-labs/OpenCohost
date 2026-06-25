"""Privacy contract: the Input Contract shadow log must be OFF by default.

`Aggregator._log_input_contract_shadow` writes `old_compact` (chat-derived intent
text) plus the ChatContextPacket into the live-safety log, which is persisted to
`acciones.jsonl` (advanced_panel._guardar_accion). Per the project rule
"never expose raw chat in ... persistence", this diagnostic shadow must not run
unless a developer explicitly opts in by flipping the flag.

Track: chat-entity persistence hygiene (Decision 6). The full redact/allowlist
work must be resolved before re-enabling shadow mode or flipping
USE_INPUT_CONTRACT_PROMPT to True.
"""

from __future__ import annotations


def test_input_contract_shadow_mode_off_by_default():
    """The shadow flag defaults to False so chat-derived text is not persisted."""
    import opencohost.smart_aggregator.chat_input_contract as ic

    assert ic.INPUT_CONTRACT_SHADOW_MODE is False


def test_shadow_log_would_carry_chat_derived_old_compact():
    """Characterization (why default-off matters): when invoked, the shadow log
    message embeds the chat-derived `old_compact`. This is exactly what would
    reach the persisted action log if the flag were enabled."""
    from opencohost.smart_aggregator.aggregator import Aggregator
    from opencohost.smart_aggregator.chat_input_contract import (
        ChatContextPacketBuilder,
    )

    agg = Aggregator.__new__(Aggregator)  # bypass __init__ (reads config files)
    captured: list[str] = []
    agg.on_live_safety_log = captured.append

    packet = ChatContextPacketBuilder().build([])
    agg._log_input_contract_shadow("viewer_nick_random por mods", packet)

    assert captured, "shadow log emitted nothing"
    assert "viewer_nick_random" in captured[0]
