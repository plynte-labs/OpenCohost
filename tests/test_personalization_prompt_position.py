"""Placement tests for the W1 personalization move (memoria_recall_20260718).

The <perfil_streamer> block moves from the per-turn USER prepend into the
SYSTEM position — mirroring set_profile's system_prompt persona
(llm_engine.py:715-737): one stable copy per REQUEST, never in the user turn,
never in historial/capture. `ollama.chat` is stateless, so identity must be
re-supplied every request — this is once-per-REQUEST at the system position,
NOT literally once-per-session.

Locks:
- use_system_role=True  -> block in messages[0] (system), ABSENT from the user msg
- use_system_role=False -> block in the prompt_completo prefix, BEFORE [label]:
- ABSENT from committed historial (personalization never persists)
- _PERSONALIZATION_INJECT_SOURCES gate honored (chat/accumulated -> absent)
- PERSONALIZATION_ENABLED=False -> absent
"""

from __future__ import annotations

import queue
from unittest.mock import MagicMock, patch

import pytest

import opencohost.core.llm_engine as llm_engine
from opencohost.core import personalization
from opencohost.i18n import active as i18n_active


@pytest.fixture(autouse=True)
def _pin_es_locale():
    """Pin the active locale to es so the block + user-message label render
    deterministically (matches tests/test_llm_personalization_injection.py)."""
    from opencohost.i18n.startup import resolve_active_bundle

    i18n_active.set_active_bundle(resolve_active_bundle(locale="es"))
    yield
    i18n_active.reset_active_bundle()


# ---------------------------------------------------------------------------
# Helpers (mirror tests/test_llm_personalization_injection.py)
# ---------------------------------------------------------------------------


def _make_motor():
    log_q = queue.Queue()

    def ui_cb(event):
        pass

    motor = llm_engine.MotorVocalIA(log_q, ui_cb)
    motor.ollama = MagicMock()
    motor.pygame = MagicMock()
    motor.is_ready = True
    motor._warmed_model = motor.current_model
    motor._loaded_model = motor.current_model
    return motor, log_q


def _capture_messages(motor, contexto, source, *, commit_history=False):
    captured = []

    def fake_watchdog(**kwargs):
        captured.extend(kwargs.get("messages", []))
        return {"message": {"content": "fake Kira reply"}}

    with patch.object(motor, "_ollama_chat_with_watchdog", side_effect=fake_watchdog):
        motor._generar_dialogo(contexto, source=source, commit_history=commit_history)
    return captured


def _enable_personalization(monkeypatch, tmp_path, **fields):
    file_path = tmp_path / "personalization.json"
    monkeypatch.setattr(personalization, "PERSONALIZATION_FILE", str(file_path))
    monkeypatch.setattr(llm_engine, "PERSONALIZATION_ENABLED", True)
    data = {
        "enabled": True,
        "nickname": "",
        "occupation": "",
        "interests": "",
        "custom_instructions": "",
    }
    data.update(fields)
    personalization.save_personalization(data)
    return file_path


def _label_marker():
    return f"[{i18n_active.user_message_label()}]:"


# ---------------------------------------------------------------------------
# use_system_role=True -> block joins the SYSTEM message, not the user turn
# ---------------------------------------------------------------------------


def test_block_in_system_message_when_use_system_role_true(monkeypatch, tmp_path):
    motor, _ = _make_motor()
    motor.use_system_role = True
    _enable_personalization(monkeypatch, tmp_path, nickname="Ash")

    messages = _capture_messages(motor, "hablemos de musica", source="direct")

    assert messages[0]["role"] == "system"
    assert "perfil_streamer" in messages[0]["content"]
    assert "nickname: Ash" in messages[0]["content"]
    # The block must NOT ride along in the user turn anymore.
    assert "perfil_streamer" not in messages[-1]["content"]


def test_block_appended_after_system_prompt_when_use_system_role_true(monkeypatch, tmp_path):
    motor, _ = _make_motor()
    motor.use_system_role = True
    motor.system_prompt = "PERSONA_MARKER system persona"
    _enable_personalization(monkeypatch, tmp_path, nickname="Ash")

    messages = _capture_messages(motor, "hablemos de musica", source="direct")
    system_content = messages[0]["content"]

    # system_prompt stays first, block is appended after it (design: system_prompt + block).
    assert system_content.index("PERSONA_MARKER") < system_content.index("perfil_streamer")


# ---------------------------------------------------------------------------
# use_system_role=False -> block sits in the prompt prefix, BEFORE [label]:
# ---------------------------------------------------------------------------


def test_block_before_user_label_when_use_system_role_false(monkeypatch, tmp_path):
    motor, _ = _make_motor()
    assert motor.use_system_role is False  # engine default (llm_engine.py:323)
    _enable_personalization(monkeypatch, tmp_path, nickname="Ash")

    messages = _capture_messages(motor, "hablemos de musica", source="direct")
    content = messages[-1]["content"]

    assert "perfil_streamer" in content
    # System-position injection: block precedes the user-message label.
    assert content.index("perfil_streamer") < content.index(_label_marker())


# ---------------------------------------------------------------------------
# Never persists into historial
# ---------------------------------------------------------------------------


def test_block_absent_from_committed_historial(monkeypatch, tmp_path):
    motor, _ = _make_motor()
    _enable_personalization(monkeypatch, tmp_path, nickname="Ash")

    _capture_messages(motor, "hablemos de musica", source="direct", commit_history=True)

    joined = " ".join(entry.get("content", "") for entry in motor.historial)
    assert "perfil_streamer" not in joined
    assert "nickname: Ash" not in joined


# ---------------------------------------------------------------------------
# Source gate + kill-switch
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("source", ["chat", "accumulated", "kira-agenda"])
def test_absent_for_non_qualifying_sources(monkeypatch, tmp_path, source):
    motor, _ = _make_motor()
    _enable_personalization(monkeypatch, tmp_path, nickname="Ash")

    messages = _capture_messages(motor, "hablemos de musica", source=source)
    all_content = " ".join(m.get("content", "") for m in messages)

    assert "perfil_streamer" not in all_content


def test_absent_when_kill_switch_off(monkeypatch, tmp_path):
    motor, _ = _make_motor()
    file_path = tmp_path / "personalization.json"
    monkeypatch.setattr(personalization, "PERSONALIZATION_FILE", str(file_path))
    personalization.save_personalization({"enabled": True, "nickname": "Ash"})
    monkeypatch.setattr(llm_engine, "PERSONALIZATION_ENABLED", False)

    messages = _capture_messages(motor, "hablemos de musica", source="direct")
    all_content = " ".join(m.get("content", "") for m in messages)

    assert "perfil_streamer" not in all_content
