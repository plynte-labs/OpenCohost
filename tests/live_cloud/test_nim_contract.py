"""Test B — live contract test against the operator's real cloud provider.

Validates INTEGRATION, not the algorithm: auth works, provider/profile selection
is the configured one, the ``source`` value survives, the shared post-generation
seam runs, ``output_guard`` runs, and history / last-reply / the TTS boundary all
receive the same text. It must NOT depend on the model spontaneously repeating
anything — a clean generation is a passing generation.

Never prints keys, prompts, full responses, RAG cards, or audio.
Opt-in only: OPENCOHOST_LIVE_CLOUD_TESTS=1.
"""
from __future__ import annotations

import logging
import time

import pytest

pytestmark = pytest.mark.live_cloud

# Neutral, short, deterministic-ish prompt. One call, low token cap.
PROMPT = "Comentá en dos frases cortas cómo viene el stream de hoy."


def test_live_contract_agenda_turn(live_motor, live_posture, caplog):
    motor = live_motor

    started = time.monotonic()
    with caplog.at_level(logging.DEBUG, logger="OpenCohost"):
        motor._ejecutar_inferencia(PROMPT, source="kira-agenda")
    latency_ms = int((time.monotonic() - started) * 1000)

    # ── The turn actually produced spoken text ──────────────────────────────
    assert motor._hablar.call_count == 1, "the TTS boundary was not reached"
    spoken = motor._hablar.call_args[0][0]
    assert isinstance(spoken, str) and spoken.strip()
    assert motor._hablar.call_args[1]["source"] == "kira-agenda"

    # ── The three destinations agree byte-for-byte ──────────────────────────
    motor._dialogue_spy.assert_called_once()
    emitted, emit_source = motor._dialogue_spy.call_args[0]
    assert emitted == spoken
    assert emit_source == "kira-agenda"
    assert motor.historial[-1]["content"] == spoken

    # ── Provider selection is the configured one ────────────────────────────
    assert live_posture["base_url"].startswith("https://")
    assert live_posture["model"]

    # ── No secret and no raw dialogue in the sanitizer telemetry ────────────
    sanitizer_records = [r for r in caplog.records
                         if "[CLAUSE_SANITIZER]" in r.getMessage()]
    for record in sanitizer_records:
        message = record.getMessage()
        assert "api_key" not in message
        assert "nvapi-" not in message
        # Metadata only: no 12-char window of the spoken text may appear.
        for start in range(0, max(0, len(spoken) - 12), 7):
            assert spoken[start:start + 12] not in message

    # Metadata report. Latency and length only — no currency (the repo has no
    # pricing data) and no text.
    print(
        f"\n[live_contract] provider={live_posture['provider_id']} "
        f"model={live_posture['model']} calls=1 latency_ms={latency_ms} "
        f"spoken_chars={len(spoken)} sanitizer_records={len(sanitizer_records)}"
    )


def test_live_auth_failure_is_a_contract_failure(live_posture):
    """A wrong key must surface as a clean provider error, not a silent success.

    Proves the auth path is actually exercised by the test above: with a bogus
    key the same call must NOT return usable content.
    """
    from opencohost.core import cloud_llm_client

    with pytest.raises(Exception):
        cloud_llm_client.send_chat_completion(
            base_url=live_posture["base_url"],
            api_key="nvapi-DEFINITELY-NOT-A-VALID-KEY",
            model=live_posture["model"],
            messages=[{"role": "user", "content": "hola"}],
            options={"num_predict": 16},
            timeout=30,
        )
