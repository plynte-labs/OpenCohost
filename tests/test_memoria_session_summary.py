"""Tests for W2a session-summary memorias (memoria_recall_20260718, WU3).

Pins the mechanical (NO LLM) durable session-summary tier. At clean close
(flush_memorias) and profile switch (_dispatch_switch_flush) Kira writes ONE
summary memoria synthesized from that session's captured memoria titles
(self._session_memoria_titles, tracked in _capture_memoria on success). The
summary is:
  - status='summary' => prune-immune to the draft growth cap (_prune_profile's
    draft-only WHERE clause) AND distinct from operator-curated rows (F5),
  - marked with a "|session_summary:" stable_key so the W2b meta-router can
    find it and _prune_summaries can cap the tier independently,
  - silent (emits no memoria_captured notice) and never itself re-summarized,
  - skipped when the session captured fewer than MEMORIAS_SUMMARY_MIN_TITLES.

MEMORIAS_ENABLED defaults True; tests redirect llm_engine.MEMORIAS_DB to a tmp
path and never touch USER_DATA_DIR (mirrors test_memorias_flush_switch.py).
"""

from __future__ import annotations

import queue
import threading
from unittest.mock import MagicMock

import opencohost.core.llm_engine as llm_engine
from opencohost.core import memoria_store
from opencohost.core.memoria_store import MemoriaStore, _SESSION_SUMMARY_MARKER
from opencohost.config.settings import (
    MEMORIAS_SUMMARY_CAP,
    MEMORIAS_SUMMARY_MIN_TITLES,
)


# ---------------------------------------------------------------------------
# Helpers (mirror tests/test_memorias_flush_switch.py)
# ---------------------------------------------------------------------------

def _make_motor():
    log_q = queue.Queue()
    ui_events = []

    def ui_cb(event):
        ui_events.append(event)

    motor = llm_engine.MotorVocalIA(log_q, ui_cb)
    motor.ollama = MagicMock()
    motor.pygame = MagicMock()
    motor.is_ready = True
    return motor, log_q, ui_events


def _enable_memorias(monkeypatch, motor, tmp_path, profile_id="profile-1"):
    monkeypatch.setattr(llm_engine, "MEMORIAS_ENABLED", True)
    monkeypatch.setattr(llm_engine, "MEMORIAS_DB", str(tmp_path / "memorias.db"))
    motor._current_profile_id = profile_id
    return motor


def _store(tmp_path) -> MemoriaStore:
    return MemoriaStore(tmp_path / "memorias.db")


def _summaries(tmp_path, profile_id):
    rows = _store(tmp_path).list_for_profile(profile_id, limit=10000)
    return [r for r in rows if _SESSION_SUMMARY_MARKER in r["stable_key"]]


def _capture(motor, user, asst):
    """Drive one real commit-time capture (populates _session_memoria_titles)."""
    draft = motor._build_memoria_draft(user, asst, source="direct", private=False)
    assert draft is not None, "test vocab must be capturable"
    assert motor._capture_memoria(*draft) is True


def _run_set_profile_and_wait_for_flush(motor, monkeypatch, payload, timeout=5):
    created: list[threading.Thread] = []
    original_thread_cls = threading.Thread

    class _CapturingThread(original_thread_cls):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            created.append(self)

    monkeypatch.setattr(llm_engine.threading, "Thread", _CapturingThread)
    motor._dispatch_command("set_profile", payload)
    for t in created:
        t.join(timeout=timeout)
    return created


_USER_1 = "musica synthwave calma"
_ASST_1 = "Kira dice que es genial. Fin."
_USER_2 = "juego shooter rapido"
_ASST_2 = "Kira dice que es intenso. Fin."


# ---------------------------------------------------------------------------
# Store layer — insert_summary / _prune_summaries
# ---------------------------------------------------------------------------

def test_insert_summary_writes_one_curated_marked_row(tmp_path):
    store = MemoriaStore(tmp_path / "memorias.db")
    row_id = store.insert_summary("profile-1", "musica juego", "musica synthwave; juego shooter")

    assert row_id is not None
    rows = store.list_for_profile("profile-1")
    assert len(rows) == 1
    assert rows[0]["status"] == "summary"
    assert _SESSION_SUMMARY_MARKER in rows[0]["stable_key"]
    assert rows[0]["content"] == "musica synthwave; juego shooter"


def test_summary_row_immune_to_draft_growth_prune(monkeypatch, tmp_path):
    """Curated summaries are excluded from _prune_profile — flooding drafts
    past the profile cap never deletes the durable summary row."""
    monkeypatch.setattr(memoria_store, "MEMORIAS_PROFILE_CAP", 3)
    store = MemoriaStore(tmp_path / "memorias.db")

    store.insert_summary("p", "durable summary", "content durable summary tier")
    for i in range(8):  # far past the (patched) draft cap of 3
        store.upsert_draft("p", f"p|draft-{i}", f"title {i} alpha", f"content {i} beta")

    summaries = [
        r for r in store.list_for_profile("p", limit=10000)
        if _SESSION_SUMMARY_MARKER in r["stable_key"]
    ]
    assert len(summaries) == 1


def test_summary_cap_prunes_oldest_keeps_newest(monkeypatch, tmp_path):
    monkeypatch.setattr(memoria_store, "MEMORIAS_SUMMARY_CAP", 3)
    ts = iter([f"2026-07-17T00:00:{i:02d}+00:00" for i in range(5)])
    monkeypatch.setattr(memoria_store, "_now_text", lambda: next(ts))
    store = MemoriaStore(tmp_path / "memorias.db")

    ids = [store.insert_summary("p", f"title {i} x y", f"content {i} durable summary") for i in range(5)]

    summaries = [
        r for r in store.list_for_profile("p", limit=10000)
        if _SESSION_SUMMARY_MARKER in r["stable_key"]
    ]
    assert len(summaries) == 3
    assert {r["id"] for r in summaries} == set(ids[-3:])  # newest 3 kept


def test_insert_summary_rejects_missing_inputs(tmp_path):
    store = MemoriaStore(tmp_path / "memorias.db")
    import pytest
    with pytest.raises(memoria_store.MemoriaValidationError):
        store.insert_summary("p", "", "content only")
    with pytest.raises(memoria_store.MemoriaValidationError):
        store.insert_summary("", "title only", "content")


# ---------------------------------------------------------------------------
# Engine layer — close flush + profile switch triggers
# ---------------------------------------------------------------------------

def test_close_flush_writes_one_curated_session_summary(monkeypatch, tmp_path):
    motor, _, _ = _make_motor()
    _enable_memorias(monkeypatch, motor, tmp_path)

    _capture(motor, _USER_1, _ASST_1)
    _capture(motor, _USER_2, _ASST_2)

    motor.flush_memorias()

    summaries = _summaries(tmp_path, "profile-1")
    assert len(summaries) == 1
    assert summaries[0]["status"] == "summary"
    # mechanical synthesis from the two captured titles
    assert "musica" in summaries[0]["content"]
    assert "juego" in summaries[0]["content"]


def test_switch_writes_summary_for_departing_profile_and_clears_titles(monkeypatch, tmp_path):
    motor, _, _ = _make_motor()
    _enable_memorias(monkeypatch, motor, tmp_path, profile_id="profile-old")

    _capture(motor, _USER_1, _ASST_1)
    _capture(motor, _USER_2, _ASST_2)

    _run_set_profile_and_wait_for_flush(motor, monkeypatch, {
        "prompt": "p", "use_system": False, "_profile_name": "new", "id": "profile-new",
    })

    assert len(_summaries(tmp_path, "profile-old")) == 1
    assert _summaries(tmp_path, "profile-new") == []
    assert motor._session_memoria_titles == []  # new session starts empty


def test_fewer_than_min_titles_writes_no_summary(monkeypatch, tmp_path):
    motor, _, _ = _make_motor()
    _enable_memorias(monkeypatch, motor, tmp_path)

    assert MEMORIAS_SUMMARY_MIN_TITLES == 2
    _capture(motor, _USER_1, _ASST_1)  # only ONE

    motor.flush_memorias()

    assert _summaries(tmp_path, "profile-1") == []


def test_summary_write_is_mechanical_no_model_call(monkeypatch, tmp_path):
    motor, _, _ = _make_motor()
    _enable_memorias(monkeypatch, motor, tmp_path)

    _capture(motor, _USER_1, _ASST_1)
    _capture(motor, _USER_2, _ASST_2)

    motor.flush_memorias()

    motor.ollama.chat.assert_not_called()  # no LLM in the teardown path
    assert len(_summaries(tmp_path, "profile-1")) == 1


def test_summary_emits_no_memoria_captured_notice(monkeypatch, tmp_path):
    motor, _, ui_events = _make_motor()
    _enable_memorias(monkeypatch, motor, tmp_path)

    _capture(motor, _USER_1, _ASST_1)
    _capture(motor, _USER_2, _ASST_2)
    ui_events.clear()  # drop the two real capture notices

    motor.flush_memorias()

    assert "memoria_captured" not in ui_events


# ---------------------------------------------------------------------------
# Re-summarization guard + session-titles cap
# ---------------------------------------------------------------------------

def test_writing_summary_does_not_feed_session_titles(monkeypatch, tmp_path):
    """A summary row is written via insert_summary, never through
    _capture_memoria — so it never lands back in _session_memoria_titles and
    can never be an input to the next summary."""
    motor, _, _ = _make_motor()
    _enable_memorias(monkeypatch, motor, tmp_path)

    _capture(motor, _USER_1, _ASST_1)
    _capture(motor, _USER_2, _ASST_2)
    assert len(motor._session_memoria_titles) == 2

    motor._write_session_summary("profile-1", list(motor._session_memoria_titles))

    assert len(motor._session_memoria_titles) == 2  # summary title NOT re-appended


def test_summary_never_re_summarized_across_switch_then_close(monkeypatch, tmp_path):
    motor, _, _ = _make_motor()
    _enable_memorias(monkeypatch, motor, tmp_path, profile_id="profile-old")

    _capture(motor, _USER_1, _ASST_1)
    _capture(motor, _USER_2, _ASST_2)

    # switch -> summary for profile-old, titles cleared
    _run_set_profile_and_wait_for_flush(motor, monkeypatch, {
        "prompt": "p", "use_system": False, "_profile_name": "new", "id": "profile-new",
    })
    assert len(_summaries(tmp_path, "profile-old")) == 1
    assert motor._session_memoria_titles == []

    # close the (empty) new session — no captures, so no second summary anywhere
    motor.flush_memorias()
    assert _summaries(tmp_path, "profile-new") == []
    assert len(_summaries(tmp_path, "profile-old")) == 1


def test_session_titles_capped_at_40(monkeypatch, tmp_path):
    motor, _, _ = _make_motor()
    _enable_memorias(monkeypatch, motor, tmp_path)

    for i in range(45):
        assert motor._capture_memoria(
            "profile-1", f"profile-1|k{i}", f"title-{i} a b", "content c d"
        ) is True

    assert len(motor._session_memoria_titles) == 40
    assert motor._session_memoria_titles[-1] == "title-44 a b"  # newest kept
    assert "title-0 a b" not in motor._session_memoria_titles   # oldest dropped


# ---------------------------------------------------------------------------
# R3 — cross-profile title contamination guard
# ---------------------------------------------------------------------------

def test_capture_title_not_leaked_across_profile_switch(monkeypatch, tmp_path):
    """R3: a profile switch landing between _capture_memoria's UNLOCKED upsert
    I/O and its locked title append must NOT leak the departing profile's title
    into the NEW profile's freshly-cleared session titles. The late title is a
    bounded, correctly-attributed loss for the departing summary — never
    contamination of the arriving one."""
    motor, _, _ = _make_motor()
    _enable_memorias(monkeypatch, motor, tmp_path, profile_id="profile-old")

    original_upsert = MemoriaStore.upsert_draft

    def switching_upsert(self, *args, **kwargs):
        # Simulate a concurrent set_profile completing its atomic
        # snapshot+clear+swap WHILE this capture's I/O is in flight, i.e. after
        # the (old-profile) draft was built but before the locked title append.
        result = original_upsert(self, *args, **kwargs)
        motor._session_memoria_titles.clear()
        motor._current_profile_id = "profile-new"
        return result

    monkeypatch.setattr(MemoriaStore, "upsert_draft", switching_upsert)

    ok = motor._capture_memoria("profile-old", "profile-old|k1", "old title a b", "content c d")

    assert ok is True
    assert "old title a b" not in motor._session_memoria_titles
    assert motor._session_memoria_titles == []  # new profile's session stays clean


# ---------------------------------------------------------------------------
# R4 — close-flush summary respects the wall-clock close budget
# ---------------------------------------------------------------------------

def test_close_flush_skips_summary_when_budget_exhausted(monkeypatch, tmp_path):
    """R4: the session-summary write is ONE more bounded disk write — it must be
    skipped (never blocks close) when no write's worth of the flush budget
    remains, even with >= MEMORIAS_SUMMARY_MIN_TITLES captured titles."""
    motor, _, _ = _make_motor()
    _enable_memorias(monkeypatch, motor, tmp_path)

    _capture(motor, _USER_1, _ASST_1)
    _capture(motor, _USER_2, _ASST_2)
    assert len(motor._session_memoria_titles) == MEMORIAS_SUMMARY_MIN_TITLES  # no drafts pending

    # call 1 -> deadline = 0 + budget; call 2 (summary budget check) sees t=10,
    # so no write's worth of budget remains and the summary is skipped.
    call_times = iter([0.0, 10.0])
    monkeypatch.setattr(llm_engine.time, "monotonic", lambda: next(call_times, 10.0))

    motor.flush_memorias(budget_seconds=2.0)  # must not raise / must not hang

    assert _summaries(tmp_path, "profile-1") == []
