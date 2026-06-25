"""Privacy contract: the Input Contract shadow log is GONE.

`Aggregator._log_input_contract_shadow` used to write `old_compact` (chat-derived
intent text) plus the ChatContextPacket into the live-safety log, which is persisted
to `acciones.jsonl`. Per the project rule "never expose raw chat in ... persistence"
the diagnostic shadow shipped OFF by default and has now been REMOVED entirely
(chat-activation telemetry track): the should_call decision is captured metadata-only
via FilterTelemetry, never via `packet.to_dict()`.

The `INPUT_CONTRACT_SHADOW_MODE` flag itself stays (still False) — other input-contract
work references it — but no code path logs chat-derived text any more.
"""
from __future__ import annotations


def test_input_contract_shadow_mode_off_by_default():
    """The shadow flag defaults to False so chat-derived text is not persisted."""
    import opencohost.smart_aggregator.chat_input_contract as ic

    assert ic.INPUT_CONTRACT_SHADOW_MODE is False


def test_shadow_log_leak_method_is_removed():
    """The privacy-unsafe shadow log method was DELETED so the author/text leak path
    cannot be re-introduced silently. The replacement records should_call telemetry
    as metadata only."""
    from opencohost.smart_aggregator.aggregator import Aggregator

    assert not hasattr(Aggregator, "_log_input_contract_shadow")
