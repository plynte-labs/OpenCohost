"""Engine-wiring tests for the personalization injection gate
(kira_personalization_onboarding_20260705, design §2, §9).

Mirrors tests/test_memorias_retrieval.py's `_make_motor` / `_capture_messages`
helpers. Locks:
- block present and FIRST (before memorias_block) for source in {direct, ptt}
- block absent for chat / accumulated / kira-agenda*
- PERSONALIZATION_ENABLED=False or an empty store -> byte-identical prompt
- build_injection_block raising -> fail-open, turn still succeeds
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
    """Pin the active locale to es (matches tests/test_memorias_retrieval.py)
    so the delimiter block and user-message label render deterministically."""
    from opencohost.i18n.startup import resolve_active_bundle

    i18n_active.set_active_bundle(resolve_active_bundle(locale="es"))
    yield
    i18n_active.reset_active_bundle()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_motor():
    """Construct a MotorVocalIA with all heavy I/O mocked out."""
    log_q = queue.Queue()
    ui_events = []

    def ui_cb(event):
        ui_events.append(event)

    motor = llm_engine.MotorVocalIA(log_q, ui_cb)
    motor.ollama = MagicMock()
    motor.pygame = MagicMock()
    motor.is_ready = True
    motor._warmed_model = motor.current_model
    motor._loaded_model = motor.current_model
    return motor, log_q, ui_events


def _capture_messages(motor, contexto, source):
    """Run _generar_dialogo and capture the messages list passed downstream."""
    captured = []

    def fake_watchdog(**kwargs):
        captured.extend(kwargs.get("messages", []))
        return {"message": {"content": "fake Kira reply"}}

    with patch.object(motor, "_ollama_chat_with_watchdog", side_effect=fake_watchdog):
        motor._generar_dialogo(contexto, source=source, commit_history=False)
    return captured


def _enable_personalization(monkeypatch, tmp_path, **fields):
    """Point the store at a tmp file, enable the settings kill-switch, and
    persist a store with the given field overrides."""
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


# ---------------------------------------------------------------------------
# Present + FIRST for direct/ptt
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("source", ["direct", "ptt"])
def test_injection_present_for_direct_and_ptt(monkeypatch, tmp_path, source):
    motor, _, _ = _make_motor()
    _enable_personalization(monkeypatch, tmp_path, nickname="Ash")

    messages = _capture_messages(motor, "hablemos de musica", source=source)
    final_content = messages[-1]["content"]

    assert "perfil_streamer" in final_content
    assert "nickname: Ash" in final_content


def test_injection_first_before_memorias_block(monkeypatch, tmp_path):
    motor, _, _ = _make_motor()
    _enable_personalization(monkeypatch, tmp_path, nickname="Ash")
    monkeypatch.setattr(llm_engine, "MEMORIAS_ENABLED", True)
    motor._current_profile_id = "profile-1"
    monkeypatch.setattr(
        motor,
        "_build_memorias_injection_block",
        lambda profile_id, contexto: "<memorias_guardadas>fake memoria</memorias_guardadas>",
    )

    messages = _capture_messages(motor, "hablemos de musica synthwave", source="direct")
    final_content = messages[-1]["content"]

    assert "perfil_streamer" in final_content
    assert "memorias_guardadas" in final_content
    assert final_content.index("perfil_streamer") < final_content.index("memorias_guardadas")


# ---------------------------------------------------------------------------
# Absent for non-qualifying sources
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "source", ["chat", "accumulated", "kira-agenda", "kira-agenda-prefetch"]
)
def test_injection_absent_for_non_qualifying_sources(monkeypatch, tmp_path, source):
    motor, _, _ = _make_motor()
    _enable_personalization(monkeypatch, tmp_path, nickname="Ash")

    messages = _capture_messages(motor, "hablemos de musica", source=source)
    all_content = " ".join(m.get("content", "") for m in messages)

    assert "perfil_streamer" not in all_content


# ---------------------------------------------------------------------------
# Disabled / empty -> byte-identical prompt
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("source", ["direct", "ptt"])
def test_settings_kill_switch_off_produces_byte_identical_prompt(monkeypatch, tmp_path, source):
    motor, _, _ = _make_motor()
    file_path = tmp_path / "personalization.json"
    monkeypatch.setattr(personalization, "PERSONALIZATION_FILE", str(file_path))
    personalization.save_personalization({"enabled": True, "nickname": "Ash"})
    monkeypatch.setattr(llm_engine, "PERSONALIZATION_ENABLED", False)

    messages = _capture_messages(motor, "hablemos de musica", source=source)

    expected = (
        f"{motor.system_prompt}\n\n[{i18n_active.user_message_label()}]: hablemos de musica"
    )
    assert messages[-1]["content"] == expected


@pytest.mark.parametrize("source", ["direct", "ptt"])
def test_empty_store_produces_byte_identical_prompt(monkeypatch, tmp_path, source):
    motor, _, _ = _make_motor()
    file_path = tmp_path / "personalization.json"
    monkeypatch.setattr(personalization, "PERSONALIZATION_FILE", str(file_path))
    monkeypatch.setattr(llm_engine, "PERSONALIZATION_ENABLED", True)
    # File absent -> defaults (enabled=True, all fields empty) -> block == "".

    messages = _capture_messages(motor, "hablemos de musica", source=source)

    expected = (
        f"{motor.system_prompt}\n\n[{i18n_active.user_message_label()}]: hablemos de musica"
    )
    assert messages[-1]["content"] == expected


# ---------------------------------------------------------------------------
# Fail-open
# ---------------------------------------------------------------------------


def test_build_injection_block_raising_fails_open_turn_still_succeeds(monkeypatch, tmp_path):
    motor, _, _ = _make_motor()
    monkeypatch.setattr(llm_engine, "PERSONALIZATION_ENABLED", True)

    def _boom(sanitize_fn):
        raise RuntimeError("boom")

    monkeypatch.setattr(personalization, "build_injection_block", _boom)

    messages = _capture_messages(motor, "hablemos de musica", source="direct")

    assert messages
    assert "perfil_streamer" not in messages[-1]["content"]
