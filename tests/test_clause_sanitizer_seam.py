"""Clause sanitizer at the shared llm_engine seam.

Proves the three real destinations (history, last-reply sink, TTS boundary) all
receive the SANITIZED text, that tier 2 is agenda-only, that the operator-facing
sources ship disarmed, and that a repetition the operator ASKED for in a later
turn is never touched.

Only ``motor._hablar`` is mocked — the repo's established idiom
(tests/test_dialogue_callback.py:40). No real audio, no TTS server, no STT.
"""
from __future__ import annotations

import logging
import queue
from unittest.mock import MagicMock

import pytest

from opencohost.config.settings import (
    CLAUSE_SANITIZER_DEFAULT_SOURCES,
    CLAUSE_SANITIZER_SOURCES,
    _parse_clause_sanitizer_sources,
)
from opencohost.config.validation import output_guard
from opencohost.core import llm_engine as le
from opencohost.core.llm_engine import MotorVocalIA

INCIDENT = "No había roadmap, no había monetización, no había roadmap, no había roadmap."
INCIDENT_REPAIRED = "No había roadmap, no había monetización."
# 5 dropped clauses -> severe on the fragments axis.
SEVERE = "El stream se cayó" + ", el stream se cayó" * 5 + "."


def _resp(text):
    return {"message": {"content": text}}


def _motor(dialogue_callback=None):
    motor = MotorVocalIA(queue.Queue(), lambda event: None,
                         dialogue_callback=dialogue_callback)
    motor.ollama = MagicMock()
    motor.pygame = MagicMock()
    motor.is_ready = True
    motor.current_model = "llama3"
    motor._reasoning_model_cache["llama3"] = False
    motor._hablar = MagicMock()
    return motor


# ---------------------------------------------------------------------------
# Destinations
# ---------------------------------------------------------------------------

def test_three_destinations_receive_sanitized_text():
    """History, last-reply and the TTS boundary must all get the repaired text —
    and none of them may see the raw duplicate."""
    emitted = MagicMock()
    motor = _motor(dialogue_callback=emitted)
    motor._ollama_chat = MagicMock(return_value=_resp(INCIDENT))

    motor._ejecutar_inferencia("tema del bloque", source="kira-agenda")

    motor._hablar.assert_called_once_with(INCIDENT_REPAIRED, source="kira-agenda")
    emitted.assert_called_once_with(INCIDENT_REPAIRED, "kira-agenda")
    assert motor.historial[-1]["content"] == INCIDENT_REPAIRED

    raw_fragment = "no había roadmap, no había roadmap"
    for payload in (motor._hablar.call_args[0][0],
                    emitted.call_args[0][0],
                    motor.historial[-1]["content"]):
        assert raw_fragment not in payload


def test_pregen_connector_concat_repaired():
    """The connector/draft junction is concatenated AFTER the guardrails, so the
    repair-only re-pass is the only thing that can catch a duplicate there."""
    emitted = MagicMock()
    motor = _motor(dialogue_callback=emitted)
    motor._speak_pregenerated({
        "payload": "tema",
        "dialogo": "no había roadmap, no había monetización.",
        "source": "kira-agenda",
        "connector": "Y como decía, no había roadmap,",
    })

    expected = "Y como decía, no había roadmap, no había monetización."
    motor._hablar.assert_called_once_with(expected, source="kira-agenda")
    emitted.assert_called_once_with(expected, "kira-agenda")
    assert motor.historial[-1]["content"] == expected


def test_pregen_without_connector_is_byte_identical():
    motor = _motor()
    motor._speak_pregenerated({
        "payload": "tema",
        "dialogo": INCIDENT,
        "source": "kira-agenda",
    })

    # No connector -> no junction -> the re-pass never runs; the draft was
    # already sanitized at generation time, so playback must not touch it.
    motor._hablar.assert_called_once_with(INCIDENT, source="kira-agenda")


# ---------------------------------------------------------------------------
# Per-source activation
# ---------------------------------------------------------------------------

def test_default_arms_agenda_only():
    assert CLAUSE_SANITIZER_SOURCES == frozenset({"kira-agenda", "kira-agenda-stop"})
    assert CLAUSE_SANITIZER_SOURCES == CLAUSE_SANITIZER_DEFAULT_SOURCES
    assert le.CLAUSE_SANITIZER_SOURCES == CLAUSE_SANITIZER_DEFAULT_SOURCES


@pytest.mark.parametrize("source", ["kira-agenda", "kira-agenda-stop"])
def test_armed_agenda_sources_are_repaired(source):
    motor = _motor()
    motor._ollama_chat = MagicMock(return_value=_resp(INCIDENT))

    out = motor._generar_dialogo("tema", source=source, commit_history=True)

    assert out == INCIDENT_REPAIRED


@pytest.mark.parametrize("source", ["chat", "direct", "ptt", "accumulated"])
def test_disarmed_sources_pass_raw_text_through(source, caplog):
    """These sources have no observed defect and DO have a real false positive
    (an operator can ask for a literal repeat) — they ship disarmed."""
    motor = _motor()
    motor._ollama_chat = MagicMock(return_value=_resp(INCIDENT))

    with caplog.at_level(logging.DEBUG, logger="OpenCohost"):
        out = motor._generar_dialogo(f"algo para {source}", source=source,
                                     commit_history=True)

    assert out == INCIDENT
    assert not [r for r in caplog.records if "[CLAUSE_SANITIZER]" in r.getMessage()]


def test_per_source_config(monkeypatch):
    motor = _motor()
    motor._ollama_chat = MagicMock(return_value=_resp(INCIDENT))

    monkeypatch.setattr(le, "CLAUSE_SANITIZER_SOURCES", frozenset({"direct"}))
    assert motor._generar_dialogo("x", source="direct", commit_history=True) == INCIDENT_REPAIRED
    assert motor._generar_dialogo("x", source="kira-agenda", commit_history=True) == INCIDENT

    monkeypatch.setattr(le, "CLAUSE_SANITIZER_SOURCES", frozenset())
    assert motor._generar_dialogo("x", source="kira-agenda", commit_history=True) == INCIDENT


def test_parse_sources_warns_once_on_unknown_token(caplog):
    with caplog.at_level(logging.WARNING, logger="OpenCohost"):
        parsed = _parse_clause_sanitizer_sources("Chat,ptt")

    assert parsed == frozenset({"ptt"})
    warnings = [r for r in caplog.records
                if "CLAUSE_SANITIZER_SOURCES" in r.getMessage()]
    assert len(warnings) == 1
    assert "Chat" in warnings[0].getMessage()


def test_parse_sources_none_is_default_empty_is_disarmed():
    assert _parse_clause_sanitizer_sources(None) == CLAUSE_SANITIZER_DEFAULT_SOURCES
    assert _parse_clause_sanitizer_sources("") == frozenset()


# ---------------------------------------------------------------------------
# Tier 2 — agenda-only BY CONSTRUCTION
# ---------------------------------------------------------------------------

def test_tier2_rejects_only_on_agenda():
    """A severe verdict on agenda hands "" to the ADR-011 ladder via the same
    validator hook the success path uses, and nothing is spoken."""
    validator = MagicMock(return_value=True)
    motor = _motor()
    motor.agenda_output_validator = validator
    motor._ollama_chat = MagicMock(return_value=_resp(SEVERE))
    motor._invalidate_pregen_epoch = MagicMock()

    motor._ejecutar_inferencia("tema", source="kira-agenda")

    motor._hablar.assert_not_called()
    motor._invalidate_pregen_epoch.assert_called_once()
    validator.assert_called_once_with("")
    assert motor._ollama_chat.call_count == 1   # no regeneration from this seam


def test_tier2_unreachable_on_non_agenda_even_when_armed(monkeypatch):
    """A force-armed operator source has no regeneration owner, so the same
    severe verdict is REPAIRED and spoken — never rejected, never retried."""
    monkeypatch.setattr(le, "CLAUSE_SANITIZER_SOURCES", frozenset({"direct"}))
    motor = _motor()
    motor._ollama_chat = MagicMock(return_value=_resp(SEVERE))

    out = motor._ejecutar_inferencia("algo", source="direct")

    motor._hablar.assert_called_once_with("El stream se cayó.", source="direct")
    assert motor._ollama_chat.call_count == 1
    assert out is None  # _ejecutar_inferencia returns nothing; it speaks


# ---------------------------------------------------------------------------
# A repetition the operator ASKED for — owner-mandated
# ---------------------------------------------------------------------------

def test_later_turn_repetition_unaltered_default():
    """Turn 1 says X; turn 2 the operator asks for it again and the model emits X
    again. Byte-identical at all three destinations, zero telemetry."""
    emitted = MagicMock()
    motor = _motor(dialogue_callback=emitted)
    line = "El bloque de preguntas arranca a las diez en punto."
    motor._ollama_chat = MagicMock(return_value=_resp(line))

    motor._ejecutar_inferencia("cuándo arranca", source="direct")
    motor._ejecutar_inferencia("repetime eso", source="direct")

    assert [c[0][0] for c in motor._hablar.call_args_list] == [line, line]
    assert [c[0][0] for c in emitted.call_args_list] == [line, line]
    assert motor.historial[-1]["content"] == line


def test_later_turn_repetition_unaltered_when_armed(monkeypatch):
    """Same scenario with ``direct`` force-armed. Intra-response scope is
    structural, not configuration-dependent: the sanitizer cannot see turn 1."""
    monkeypatch.setattr(le, "CLAUSE_SANITIZER_SOURCES", frozenset({"direct"}))
    motor = _motor()
    line = "El bloque de preguntas arranca a las diez en punto."
    motor._ollama_chat = MagicMock(return_value=_resp(line))

    motor._ejecutar_inferencia("cuándo arranca", source="direct")
    motor._ejecutar_inferencia("repetime eso", source="direct")

    assert [c[0][0] for c in motor._hablar.call_args_list] == [line, line]


def test_instructed_repetition_within_sentence_is_collapsed_when_armed(monkeypatch):
    """DOCUMENTED LIMITATION, pinned deliberately.

    With ``direct`` force-armed, an instructed triple inside ONE sentence IS
    collapsed — silently. This is precisely why direct/ptt/chat ship disarmed.
    If anyone flips that default, this test is the tripwire: go re-read
    docs/deferred-20260729-clause-sanitizer-scope.md §2 first.
    """
    monkeypatch.setattr(le, "CLAUSE_SANITIZER_SOURCES", frozenset({"direct"}))
    motor = _motor()
    instructed = "No toques ese botón, no toques ese botón, no toques ese botón."
    motor._ollama_chat = MagicMock(return_value=_resp(instructed))

    out = motor._generar_dialogo("decilo tres veces", source="direct",
                                 commit_history=True)

    assert out == "No toques ese botón."
    # Disarmed by default, so production is unaffected today.
    assert "direct" not in CLAUSE_SANITIZER_DEFAULT_SOURCES


# ---------------------------------------------------------------------------
# Coexistence and telemetry
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("source", ["kira-agenda", "chat", "direct", "ptt"])
def test_output_guard_verdict_unchanged(source):
    """Sanitizing must never flip a guardrail verdict in either direction."""
    from opencohost.core.context.repetition_guard import sanitize_clause_repetition

    for raw in (INCIDENT, SEVERE,
                "Sí, sí, esto va en serio.",
                "No había roadmap, no había monetización, no había roadmap."):
        sanitized = sanitize_clause_repetition(raw).text
        assert output_guard(raw, source=source)[0] == output_guard(sanitized, source=source)[0]


def test_telemetry_is_metadata_only(caplog):
    """One record per non-clean turn, fixed schema, and NO fragment of the
    removed clause anywhere in it — not even at DEBUG."""
    motor = _motor()
    motor._ollama_chat = MagicMock(return_value=_resp(INCIDENT))

    with caplog.at_level(logging.DEBUG, logger="OpenCohost"):
        motor._generar_dialogo("tema", source="kira-agenda", commit_history=True)

    records = [r for r in caplog.records if "[CLAUSE_SANITIZER]" in r.getMessage()]
    assert len(records) == 1
    message = records[0].getMessage()
    for field in ("stage=generate", "source=kira-agenda", "verdict=repaired",
                  "removed=2", "distinct=1", "max_occ=3",
                  "orig_len=76", "remaining_len=40", "removed_pct="):
        assert field in message

    removed_clause = "no había roadmap"
    for start in range(len(removed_clause) - 7):
        assert removed_clause[start:start + 8] not in message.lower()


def _latency_records(caplog):
    return [r.getMessage() for r in caplog.records if "[TURN_LATENCY]" in r.getMessage()]


def test_turn_latency_logged_once_for_a_foreground_turn(caplog):
    """§7 instrument: the request -> TTS-receives-text span, previously never
    measured because the two halves are logged separately and never summed."""
    motor = _motor()
    motor._ollama_chat = MagicMock(return_value=_resp("Todo tranquilo por acá."))

    with caplog.at_level(logging.DEBUG, logger="OpenCohost"):
        motor._ejecutar_inferencia("hola", source="direct")

    records = _latency_records(caplog)
    assert len(records) == 1
    assert "source=direct" in records[0]
    assert "request_to_tts_ms=" in records[0]


def test_turn_latency_is_never_emitted_from_a_background_speak(caplog):
    """The span is a LOCAL in `_ejecutar_inferencia`, not a shared slot.

    `_hablar` has four callers and two run on background threads (the detached
    `speaker()` in `play_prefetched_agenda`, and the `CloudFallbackWarm` worker).
    An instance slot read inside `_hablar` could be consumed by a turn that never
    opened it, reporting a latency under the wrong source. Pinned so nobody moves
    the instrument back into `_hablar`.
    """
    motor = _motor()

    with caplog.at_level(logging.DEBUG, logger="OpenCohost"):
        # A pregenerated draft: spoken much later by design, no span opened.
        motor._speak_pregenerated({
            "payload": "tema",
            "dialogo": "Respuesta ya lista.",
            "source": "kira-agenda",
        })
        # A bare speak, as the background threads do it.
        motor._hablar("una línea suelta", source="kira-agenda")

    assert _latency_records(caplog) == []


def test_pregen_draft_and_foreground_are_distinguishable_in_telemetry(caplog):
    """A speculative draft may never be spoken. The tuning pass must not count it
    as a live turn, so the stage field has to separate them."""
    motor = _motor()
    motor._ollama_chat = MagicMock(return_value=_resp(INCIDENT))

    with caplog.at_level(logging.DEBUG, logger="OpenCohost"):
        motor._generar_dialogo("tema", source="kira-agenda", commit_history=True)
        motor._generar_dialogo("tema", source="kira-agenda", commit_history=False)

    stages = [m.split("stage=")[1].split()[0]
              for m in (r.getMessage() for r in caplog.records)
              if "[CLAUSE_SANITIZER]" in m]
    assert stages == ["generate", "pregen_draft"]


def test_telemetry_silent_on_clean_turn(caplog):
    motor = _motor()
    motor._ollama_chat = MagicMock(return_value=_resp("Todo tranquilo por acá."))

    with caplog.at_level(logging.DEBUG, logger="OpenCohost"):
        motor._generar_dialogo("tema", source="kira-agenda", commit_history=True)

    assert not [r for r in caplog.records if "[CLAUSE_SANITIZER]" in r.getMessage()]
