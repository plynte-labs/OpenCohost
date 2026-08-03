"""Deterministic end-to-end: the confirmed incident, on the NVIDIA NIM posture.

Runs in the normal suite and in CI. **This test, not the live ones, is what
proves the algorithm correct.**

REAL here: the whole post-generation pipeline — clause sanitizer, ``output_guard``,
the agenda ladder hook, ``_commit_history``, ``_emit_dialogue``, cloud posture
resolution and provider/profile selection.

SIMULATED here: exactly two things — the provider's HTTP response
(``cloud_llm_client.send_chat_completion``) and ``_hablar``. The key is a dummy
written into the per-test ``LLM_KEYS_FILE`` that ``tests/conftest.py`` already
redirects to ``tmp_path``, so the owner's real key is never read and no network
call is ever made.
"""
from __future__ import annotations

import queue
from unittest.mock import MagicMock, patch

import pytest

from opencohost.core import llm_engine as le
from opencohost.core.llm_engine import MotorVocalIA
from opencohost.stream_admin.oauth_store import OAuthStore

_SEND = "opencohost.core.providers.cloud.cloud_llm_client.send_chat_completion"

INCIDENT = "No había roadmap, no había monetización, no había roadmap, no había roadmap."
INCIDENT_REPAIRED = "No había roadmap, no había monetización."
DUMMY_KEY = "nvapi-DUMMY-TEST-KEY-NOT-A-REAL-CREDENTIAL"


def _nim_config():
    return {
        "active_provider": "nvidia_nim",
        "fallback_mode": "manual",
        "pregen_enabled": False,
        "profiles": {
            "nvidia_nim": {
                "base_url": "https://integrate.api.nvidia.com/v1",
                "model": "z-ai/glm-5.2",
            },
        },
    }


@pytest.fixture
def nim_motor(dialogue_spy=None):
    """A motor on the cloud posture with a DUMMY key in the redirected store."""
    def _build(spy=None):
        OAuthStore(le.LLM_KEYS_FILE).save("nvidia_nim", {"api_key": DUMMY_KEY})
        motor = MotorVocalIA(queue.Queue(), lambda event: None,
                             dialogue_callback=spy)
        motor.ollama = MagicMock()
        motor.pygame = MagicMock()
        motor.is_ready = True
        motor.current_model = "llama3"
        motor._reasoning_model_cache["llama3"] = False
        motor._hablar = MagicMock()
        motor._provider_config = _nim_config()
        return motor
    return _build


def _cloud_response(text):
    """The Ollama-shaped dict the cloud client adapts its response into."""
    return {"message": {"content": text, "thinking": ""}, "usage": {}}


def test_incident_repaired_end_to_end_on_cloud_posture(nim_motor):
    """The real provider response is substituted with the real defect text; the
    whole shared pipeline runs; all three destinations get the repaired string
    and none of them — nor the network — ever sees the raw duplicate."""
    emitted = MagicMock()
    motor = nim_motor(emitted)

    with patch(_SEND, return_value=_cloud_response(INCIDENT)) as send:
        motor._ejecutar_inferencia("bloque sobre el proyecto", source="kira-agenda")

    send.assert_called_once()
    kwargs = send.call_args.kwargs
    # Provider selection is real: base_url, model and key come from the posture.
    assert kwargs["base_url"] == "https://integrate.api.nvidia.com/v1"
    assert kwargs["model"] == "z-ai/glm-5.2"
    assert kwargs["api_key"] == DUMMY_KEY   # dummy, never the owner's key

    motor._hablar.assert_called_once_with(INCIDENT_REPAIRED, source="kira-agenda")
    emitted.assert_called_once_with(INCIDENT_REPAIRED, "kira-agenda")
    assert motor.historial[-1]["content"] == INCIDENT_REPAIRED

    raw_fragment = "no había roadmap, no había roadmap"
    assert raw_fragment not in motor._hablar.call_args[0][0]
    assert raw_fragment not in emitted.call_args[0][0]
    assert raw_fragment not in motor.historial[-1]["content"]


def test_identical_across_providers(nim_motor):
    """Same input, same repaired output on the local and the cloud path — the
    seam sits below every is_local branch, so this is structural."""
    cloud = nim_motor()
    with patch(_SEND, return_value=_cloud_response(INCIDENT)):
        cloud_out = cloud._generar_dialogo("tema", source="kira-agenda",
                                           commit_history=True)

    local = nim_motor()
    local._provider_config = {"active_provider": "ollama", "profiles": {}}
    local._ollama_chat = MagicMock(return_value={"message": {"content": INCIDENT}})
    local_out = local._generar_dialogo("tema", source="kira-agenda",
                                       commit_history=True)

    assert cloud_out == local_out == INCIDENT_REPAIRED


def test_cloud_severe_degeneration_hands_empty_to_the_ladder(nim_motor):
    """Tier 2 on the cloud path: one generation, no regeneration from this seam,
    and the ladder gets its "" through the same validator hook."""
    validator = MagicMock(return_value=True)
    motor = nim_motor()
    motor.agenda_output_validator = validator
    severe = "El stream se cayó" + ", el stream se cayó" * 5 + "."

    with patch(_SEND, return_value=_cloud_response(severe)) as send:
        motor._ejecutar_inferencia("tema", source="kira-agenda")

    assert send.call_count == 1
    motor._hablar.assert_not_called()
    validator.assert_called_once_with("")
