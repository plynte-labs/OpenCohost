"""Tests for slice 3 (capture-engine) — kira_memory_persistence_20260701.

Pins the eviction-capture hook wired into MotorVocalIA._commit_history:
gate chain (R1 provenance, R2 privacy switch, R3 distillation minimum, R4
session-scoped switch), thread/lock invariants (R5 wiring), and the RC-10
accepted agenda-append loss.

MEMORIAS_ENABLED stays False in settings; every test that needs capture
enabled monkeypatches `llm_engine.MEMORIAS_ENABLED` directly (module-level
name lookup, matching the SCOUT_ENABLED precedent in test_topic_scout.py) —
patching opencohost.config.settings would NOT affect the name already bound
into llm_engine's module namespace at import time.
"""

import queue
import threading
from collections import deque
from unittest.mock import MagicMock

import pytest

import opencohost.core.llm_engine as llm_engine
from opencohost.config.settings import HISTORY_MAX_TURNS
from opencohost.core.memoria_store import MemoriaStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_motor():
    """Construct a MotorVocalIA with all heavy I/O mocked out.

    Mirrors tests/test_pipeline_memory.py's _make_motor().
    """
    log_q = queue.Queue()
    ui_events = []

    def ui_cb(event):
        ui_events.append(event)

    motor = llm_engine.MotorVocalIA(log_q, ui_cb)
    motor.ollama = MagicMock()
    motor.pygame = MagicMock()
    motor.is_ready = True
    return motor, log_q, ui_events


def _fill_history_to_max(motor, count=None):
    """Fill historial to full capacity with innocuous, untagged filler pairs.

    Mirrors tests/test_pipeline_memory.py's _fill_history_to_max(). These
    filler entries deliberately have no 'private' key — only historial[0]/[1]
    (overwritten per-test below) matter for the eviction-capture gate chain.
    """
    n = count if count is not None else HISTORY_MAX_TURNS
    for i in range(n):
        motor.historial.append({"role": "user", "content": f"user msg {i}", "source": "direct"})
        motor.historial.append(
            {"role": "assistant", "content": f"assistant reply {i}. This is a sentence.", "source": "direct"}
        )


def _enable_memorias(monkeypatch, motor, tmp_path, profile_id="profile-1"):
    """Flip MEMORIAS_ENABLED on and point the engine's lazy store at tmp_path."""
    monkeypatch.setattr(llm_engine, "MEMORIAS_ENABLED", True)
    monkeypatch.setattr(llm_engine, "MEMORIAS_DB", str(tmp_path / "memorias.db"))
    motor._current_profile_id = profile_id
    return motor


def _store(tmp_path) -> MemoriaStore:
    return MemoriaStore(tmp_path / "memorias.db")


# Vocab calibrated against the same domain-stopword list as slice 2
# (_MEMORIA_DOMAIN_STOPWORDS): "streamer"/"kira"/"dice" are filtered out,
# leaving >=3 significant tokens for eligible-pair tests.
_ELIGIBLE_USER = "streamer prefiere musica synthwave calma"
_ELIGIBLE_ASST = "Kira dice que es genial. Fin."


# ---------------------------------------------------------------------------
# R1 — provenance gate (3.1-3.3)
# ---------------------------------------------------------------------------

def test_eviction_capture_requires_direct_or_ptt_source_fail_closed(monkeypatch, tmp_path):
    """Missing/unrecognized source values on the evicted entry fail closed."""
    motor, _, _ = _make_motor()
    _enable_memorias(monkeypatch, motor, tmp_path)

    _fill_history_to_max(motor)
    motor.historial[0] = {"role": "user", "content": _ELIGIBLE_USER, "private": False}  # no 'source' key
    motor.historial[1] = {"role": "assistant", "content": _ELIGIBLE_ASST, "private": False}
    motor._commit_history("siguiente pregunta", "siguiente respuesta.", source="direct")

    assert _store(tmp_path).list_for_profile("profile-1") == []


@pytest.mark.parametrize("bad_source", ["chat", "accumulated", "unknown"])
def test_eviction_capture_skips_non_capturable_sources_chat_accumulated_unknown(monkeypatch, tmp_path, bad_source):
    motor, _, _ = _make_motor()
    _enable_memorias(monkeypatch, motor, tmp_path)

    _fill_history_to_max(motor)
    motor.historial[0] = {"role": "user", "content": _ELIGIBLE_USER, "source": bad_source, "private": False}
    motor.historial[1] = {"role": "assistant", "content": _ELIGIBLE_ASST, "source": bad_source, "private": False}
    motor._commit_history("siguiente pregunta", "siguiente respuesta.", source="direct")

    assert _store(tmp_path).list_for_profile("profile-1") == []


def test_eviction_capture_skips_agenda_sentinel_entries(monkeypatch, tmp_path):
    """Agenda-origin pairs (user slot = sentinel) never leak into memorias,
    even when source is tagged 'direct' — content-prefix belt check."""
    motor, _, _ = _make_motor()
    _enable_memorias(monkeypatch, motor, tmp_path)

    _fill_history_to_max(motor)
    motor.historial[0] = {
        "role": "user",
        "content": "[agenda segura: prompt interno omitido]",
        "source": "direct",
        "private": False,
    }
    motor.historial[1] = {
        "role": "assistant",
        "content": "TOP SECRET AGENDA REPLY that must never leak.",
        "source": "direct",
        "private": False,
    }
    motor._commit_history("innocent question", "innocent reply.", source="direct")

    assert _store(tmp_path).list_for_profile("profile-1") == []


# ---------------------------------------------------------------------------
# R4 — session-scoped capture switch (3.4, 3.7)
# ---------------------------------------------------------------------------

def test_eviction_capture_skips_when_memorias_disabled_flag_false(monkeypatch, tmp_path):
    """MEMORIAS_ENABLED defaulted False through slice 7; slice 8 flips the
    production default to True, so this OFF-behavior test now patches it
    explicitly instead of relying on the old default."""
    monkeypatch.setattr(llm_engine, "MEMORIAS_ENABLED", False)
    motor, _, _ = _make_motor()
    motor._current_profile_id = "profile-1"
    # Deliberately NOT calling _enable_memorias — MEMORIAS_ENABLED forced False above.

    _fill_history_to_max(motor)
    motor.historial[0] = {"role": "user", "content": _ELIGIBLE_USER, "source": "direct", "private": False}
    motor.historial[1] = {"role": "assistant", "content": _ELIGIBLE_ASST, "source": "direct", "private": False}
    motor._commit_history("siguiente pregunta", "siguiente respuesta.", source="direct")

    assert motor._memoria_store is None  # never instantiated — zero cost on the hot path
    assert _store(tmp_path).list_for_profile("profile-1") == []


def test_privacy_switch_is_session_scoped_single_global_toggle():
    """set_memorias_private is a single global flag — never per-profile."""
    motor, _, _ = _make_motor()
    assert motor._memorias_private is False  # default: capturing

    motor.set_memorias_private(True)
    motor._current_profile_id = "profile-A"
    motor._commit_history("hola", "hola de vuelta.", source="direct")
    assert motor.historial[-2]["private"] is True
    assert motor.historial[-1]["private"] is True

    # Switching the active profile does not reset or scope the toggle.
    motor._current_profile_id = "profile-B"
    motor._commit_history("hola de nuevo", "hola otra vez.", source="direct")
    assert motor.historial[-2]["private"] is True
    assert motor.historial[-1]["private"] is True


def test_memorias_private_property_reflects_current_switch_state():
    """Slice 7 UI read: the public memorias_private property mirrors
    _memorias_private without needing UI code to touch a private attr."""
    motor, _, _ = _make_motor()
    assert motor.memorias_private is False

    motor.set_memorias_private(True)
    assert motor.memorias_private is True

    motor.set_memorias_private(False)
    assert motor.memorias_private is False


# ---------------------------------------------------------------------------
# R2 — privacy switch, forward-only (3.5, 3.6)
# ---------------------------------------------------------------------------

def test_eviction_capture_skips_entry_tagged_private_at_append_time(monkeypatch, tmp_path):
    motor, _, _ = _make_motor()
    _enable_memorias(monkeypatch, motor, tmp_path)

    _fill_history_to_max(motor)
    motor.historial[0] = {"role": "user", "content": _ELIGIBLE_USER, "source": "direct", "private": True}
    motor.historial[1] = {"role": "assistant", "content": _ELIGIBLE_ASST, "source": "direct", "private": True}
    motor._commit_history("siguiente pregunta", "siguiente respuesta.", source="direct")

    assert _store(tmp_path).list_for_profile("profile-1") == []


def test_reenabling_capture_mid_session_never_retro_captures_private_window(monkeypatch, tmp_path):
    """A pair appended while paused stays uncapturable even after the switch
    is later flipped back to capturing (state-at-event, forward-only)."""
    motor, _, _ = _make_motor()
    _enable_memorias(monkeypatch, motor, tmp_path)

    motor.set_memorias_private(True)  # pause
    for i in range(HISTORY_MAX_TURNS):
        motor._commit_history(
            f"{_ELIGIBLE_USER} {i}", f"{_ELIGIBLE_ASST} {i}", source="direct",
        )
    motor.set_memorias_private(False)  # resume

    # Evicts the first (paused, private=True) pair above — capture must still skip it.
    motor._commit_history("nuevo tema deporte futbol pelota", "respuesta sobre deporte. Fin.", source="direct")

    assert _store(tmp_path).list_for_profile("profile-1") == []


def test_concurrent_privacy_flip_mid_append_tags_both_pair_entries_consistently(monkeypatch):
    """B-S1: set_memorias_private does not hold _history_lock, so a concurrent
    flip landing BETWEEN the user and assistant historial.append() calls must
    not split a pair's tag. The flag is snapshotted ONCE per turn and used for
    both appends — regardless of when the flip lands."""
    motor, _, _ = _make_motor()
    assert motor._memorias_private is False

    # Base collections.deque forbids per-instance attribute assignment, so a
    # thin subclass is swapped in to spy on append() calls (empty at this
    # point — nothing to migrate).
    class _SpyDeque(deque):
        pass

    spy_historial = _SpyDeque(maxlen=motor.historial.maxlen)
    calls = {"n": 0}

    def flipping_append(entry):
        calls["n"] += 1
        if calls["n"] == 1:
            # Simulate a concurrent set_memorias_private(True) call (e.g. from
            # the Tk UI thread) landing right after the user entry is built
            # but before the assistant entry is built.
            motor.set_memorias_private(True)
        return deque.append(spy_historial, entry)

    monkeypatch.setattr(spy_historial, "append", flipping_append)
    motor.historial = spy_historial

    motor._commit_history("hola", "hola de vuelta.", source="direct")

    assert calls["n"] == 2
    assert motor.historial[-2]["private"] == motor.historial[-1]["private"]


# ---------------------------------------------------------------------------
# R3 — auto-draft distillation (3.8, 3.9)
# ---------------------------------------------------------------------------

def test_pair_with_fewer_than_3_significant_tokens_produces_zero_rows(monkeypatch, tmp_path):
    """"kira obs streamer" (0 sig. tokens) + "dice musica" (1 sig. token) -> 1 < 3."""
    motor, _, _ = _make_motor()
    _enable_memorias(monkeypatch, motor, tmp_path)

    _fill_history_to_max(motor)
    motor.historial[0] = {"role": "user", "content": "kira obs streamer", "source": "direct", "private": False}
    motor.historial[1] = {"role": "assistant", "content": "dice musica", "source": "direct", "private": False}
    motor._commit_history("siguiente pregunta", "siguiente respuesta.", source="direct")

    assert _store(tmp_path).list_for_profile("profile-1") == []


def test_eligible_pair_meeting_token_minimum_creates_draft_with_nonempty_title_content(monkeypatch, tmp_path):
    motor, _, _ = _make_motor()
    _enable_memorias(monkeypatch, motor, tmp_path)

    _fill_history_to_max(motor)
    motor.historial[0] = {"role": "user", "content": _ELIGIBLE_USER, "source": "direct", "private": False}
    motor.historial[1] = {"role": "assistant", "content": _ELIGIBLE_ASST, "source": "direct", "private": False}
    motor._commit_history("siguiente pregunta", "siguiente respuesta.", source="direct")

    rows = _store(tmp_path).list_for_profile("profile-1")
    assert len(rows) == 1
    row = rows[0]
    assert row["profile_id"] == "profile-1"
    assert row["title"] != ""
    assert row["content"] != ""
    assert "contexto:" in row["content"]
    assert "→ Kira:" in row["content"]
    assert len(row["content"]) <= 300


# ---------------------------------------------------------------------------
# R5 wiring — build-under-lock, upsert-after-release (3.10); thread invariant (3.11)
# ---------------------------------------------------------------------------

def test_draft_built_under_history_lock_upsert_called_after_lock_release(monkeypatch, tmp_path):
    motor, _, _ = _make_motor()
    _enable_memorias(monkeypatch, motor, tmp_path)

    build_lock_state = {}
    capture_lock_state = {}

    original_build = motor._build_memoria_draft

    def spy_build(*args, **kwargs):
        build_lock_state["locked"] = motor._history_lock.locked()
        return original_build(*args, **kwargs)

    original_capture = motor._capture_memoria

    def spy_capture(*args, **kwargs):
        capture_lock_state["locked"] = motor._history_lock.locked()
        return original_capture(*args, **kwargs)

    monkeypatch.setattr(motor, "_build_memoria_draft", spy_build)
    monkeypatch.setattr(motor, "_capture_memoria", spy_capture)

    _fill_history_to_max(motor)
    motor.historial[0] = {"role": "user", "content": _ELIGIBLE_USER, "source": "direct", "private": False}
    motor.historial[1] = {"role": "assistant", "content": _ELIGIBLE_ASST, "source": "direct", "private": False}
    motor._commit_history("siguiente pregunta", "siguiente respuesta.", source="direct")

    assert build_lock_state["locked"] is True
    assert capture_lock_state["locked"] is False


def test_capture_never_runs_on_tk_main_thread_and_never_with_history_lock_held(monkeypatch, tmp_path):
    """Residual-risk pin: steady-state upsert_draft only ever runs on
    _commit_history's real callers (engine worker loop / agenda speaker
    daemon), never the Tk main thread, and never with _history_lock held."""
    motor, _, _ = _make_motor()
    _enable_memorias(monkeypatch, motor, tmp_path)

    recorded = {}
    original_upsert = MemoriaStore.upsert_draft

    def spy_upsert(self, *args, **kwargs):
        recorded["thread"] = threading.current_thread()
        recorded["lock_held"] = motor._history_lock.locked()
        return original_upsert(self, *args, **kwargs)

    monkeypatch.setattr(MemoriaStore, "upsert_draft", spy_upsert)

    _fill_history_to_max(motor)
    motor.historial[0] = {"role": "user", "content": _ELIGIBLE_USER, "source": "direct", "private": False}
    motor.historial[1] = {"role": "assistant", "content": _ELIGIBLE_ASST, "source": "direct", "private": False}

    worker = threading.Thread(
        target=motor._commit_history,
        args=("siguiente pregunta", "siguiente respuesta."),
        kwargs={"source": "direct"},
    )
    worker.start()
    worker.join(timeout=5)

    assert "thread" in recorded, "capture never ran"
    assert recorded["thread"] is worker
    assert recorded["thread"] is not threading.main_thread()
    assert recorded["lock_held"] is False


# ---------------------------------------------------------------------------
# RC-10 — agenda-append accepted loss (3.12)
# ---------------------------------------------------------------------------

def test_agenda_append_eviction_loss_is_currently_accepted_behavior(monkeypatch, tmp_path):
    # Si este test falla porque ahora sí captura correctamente, revisar el diseño antes de restaurarlo.
    motor, _, _ = _make_motor()
    _enable_memorias(monkeypatch, motor, tmp_path)

    _fill_history_to_max(motor)
    motor.historial[0] = {"role": "user", "content": _ELIGIBLE_USER, "source": "direct", "private": False}
    motor.historial[1] = {"role": "assistant", "content": _ELIGIBLE_ASST, "source": "direct", "private": False}

    # An agenda-sourced commit skips the ENTIRE eviction-capture block (guarded
    # by `not source.startswith("kira-agenda")` on the INCOMING source, at the
    # top of _commit_history's critical section), so the otherwise-eligible
    # evicted pair above is silently dropped — accepted v1 loss (RC-10),
    # inherited unmodified from the pre-existing T1 digest eviction gate.
    motor._commit_history("agenda prompt", "respuesta de agenda.", source="kira-agenda")

    assert _store(tmp_path).list_for_profile("profile-1") == []


# ---------------------------------------------------------------------------
# RC-1 integration — no false merge across distinct evicted pairs (3.13)
# ---------------------------------------------------------------------------

def test_two_evicted_pairs_different_intent_shared_vocab_never_merge_into_one_row(monkeypatch, tmp_path):
    motor, _, _ = _make_motor()
    _enable_memorias(monkeypatch, motor, tmp_path)

    _fill_history_to_max(motor)
    motor.historial[0] = {
        "role": "user", "content": "streamer prefiere musica synthwave calma",
        "source": "direct", "private": False,
    }
    motor.historial[1] = {
        "role": "assistant", "content": "Kira dice que es un tema tranquilo. Fin.",
        "source": "direct", "private": False,
    }
    motor._commit_history("siguiente pregunta uno", "siguiente respuesta uno.", source="direct")

    motor.historial[0] = {
        "role": "user", "content": "streamer prefiere juego shooter rapido",
        "source": "direct", "private": False,
    }
    motor.historial[1] = {
        "role": "assistant", "content": "Kira dice que es un juego intenso. Fin.",
        "source": "direct", "private": False,
    }
    motor._commit_history("siguiente pregunta dos", "siguiente respuesta dos.", source="direct")

    rows = _store(tmp_path).list_for_profile("profile-1")
    assert len(rows) == 2
    keys = {row["stable_key"] for row in rows}
    assert len(keys) == 2
