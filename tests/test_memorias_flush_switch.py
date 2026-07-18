"""Tests for slice 4 (flush+switch) — kira_memory_persistence_20260701.

Pins F4 bounded flush on clean close (R14) and the RC-2 profile-switch
critical section: snapshot -> build drafts (OLD profile id) -> swap id ->
clear historial+digest, all under ONE `_history_lock` acquisition, with the
resulting disk upserts dispatched to a worker thread AFTER the lock releases
(RC-3 — switch-flush must never block the Tk thread).

MEMORIAS_ENABLED defaults True as of slice 8. Tests that exercise
capture/flush/injection MUST monkeypatch `llm_engine.MEMORIAS_ENABLED`
True AND redirect `llm_engine.MEMORIAS_DB` to a tmp path (see the
`_enable_memorias` helper, mirroring slice 3's convention in
test_memorias_capture_engine.py) — NEVER let the real store be
instantiated against USER_DATA_DIR.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from unittest.mock import MagicMock

import pytest

import opencohost.core.llm_engine as llm_engine
from opencohost.core.memoria_store import MemoriaStore, derive_stable_key


# ---------------------------------------------------------------------------
# Helpers (mirrors tests/test_memorias_capture_engine.py)
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


def _run_set_profile_and_wait_for_flush(motor, monkeypatch, payload, timeout=5):
    """Dispatch set_profile and block until its fire-and-forget switch-flush
    worker thread (if any was spawned) finishes, for deterministic asserts."""
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


# Vocab calibrated against the same domain-stopword list as slice 2/3
# (_MEMORIA_DOMAIN_STOPWORDS): "streamer"/"kira"/"dice" are filtered out,
# leaving >=3 significant tokens for eligible-pair tests.
_ELIGIBLE_USER = "streamer prefiere musica synthwave calma"
_ELIGIBLE_ASST = "Kira dice que es genial. Fin."
_ELIGIBLE_USER_2 = "streamer prefiere juego shooter rapido"
_ELIGIBLE_ASST_2 = "Kira dice que es un juego intenso. Fin."


# ---------------------------------------------------------------------------
# R14 — close-flush (4.1-4.5)
# ---------------------------------------------------------------------------

def test_clean_close_flushes_eligible_pairs_still_in_live_window(monkeypatch, tmp_path):
    motor, _, _ = _make_motor()
    _enable_memorias(monkeypatch, motor, tmp_path)

    motor.historial.append({"role": "user", "content": _ELIGIBLE_USER, "source": "direct", "private": False})
    motor.historial.append({"role": "assistant", "content": _ELIGIBLE_ASST, "source": "direct", "private": False})

    motor.flush_memorias()

    rows = _store(tmp_path).list_for_profile("profile-1")
    assert len(rows) == 1
    assert rows[0]["content"] != ""
    assert rows[0]["title"] != ""


def test_close_flush_uses_same_gate_chain_as_eviction_capture(monkeypatch, tmp_path):
    """A pair whose source is outside the digest allowlist is skipped by
    flush the exact same way it would be skipped by eviction capture."""
    motor, _, _ = _make_motor()
    _enable_memorias(monkeypatch, motor, tmp_path)

    motor.historial.append({"role": "user", "content": _ELIGIBLE_USER, "source": "chat", "private": False})
    motor.historial.append({"role": "assistant", "content": _ELIGIBLE_ASST, "source": "chat", "private": False})

    motor.flush_memorias()

    assert _store(tmp_path).list_for_profile("profile-1") == []


def test_close_flush_budget_expiry_fails_open_never_blocks_app_close(monkeypatch, tmp_path):
    motor, _, _ = _make_motor()
    _enable_memorias(monkeypatch, motor, tmp_path)

    motor.historial.append({"role": "user", "content": _ELIGIBLE_USER, "source": "direct", "private": False})
    motor.historial.append({"role": "assistant", "content": _ELIGIBLE_ASST, "source": "direct", "private": False})
    motor.historial.append({"role": "user", "content": _ELIGIBLE_USER_2, "source": "direct", "private": False})
    motor.historial.append({"role": "assistant", "content": _ELIGIBLE_ASST_2, "source": "direct", "private": False})

    # First monotonic() call computes the deadline (t=0 -> deadline=2.0);
    # second call (pre-first-upsert check) is still within budget (t=0);
    # third call (pre-second-upsert check) is past the budget (t=10).
    call_times = iter([0.0, 0.0, 10.0])
    monkeypatch.setattr(llm_engine.time, "monotonic", lambda: next(call_times, 10.0))

    motor.flush_memorias(budget_seconds=2.0)  # must not raise / must not hang

    rows = _store(tmp_path).list_for_profile("profile-1")
    assert len(rows) == 1  # only the first draft got flushed before budget expired


def test_paused_tagged_entries_excluded_from_flush(monkeypatch, tmp_path):
    motor, _, _ = _make_motor()
    _enable_memorias(monkeypatch, motor, tmp_path)

    motor.historial.append({"role": "user", "content": _ELIGIBLE_USER, "source": "direct", "private": True})
    motor.historial.append({"role": "assistant", "content": _ELIGIBLE_ASST, "source": "direct", "private": True})

    motor.flush_memorias()

    assert _store(tmp_path).list_for_profile("profile-1") == []


def test_viewer_chat_entries_excluded_from_flush(monkeypatch, tmp_path):
    motor, _, _ = _make_motor()
    _enable_memorias(monkeypatch, motor, tmp_path)

    motor.historial.append({"role": "user", "content": _ELIGIBLE_USER, "source": "accumulated", "private": False})
    motor.historial.append({"role": "assistant", "content": _ELIGIBLE_ASST, "source": "accumulated", "private": False})

    motor.flush_memorias()

    assert _store(tmp_path).list_for_profile("profile-1") == []


def test_flush_mixed_window_persists_only_eligible_direct_pair_per_pair_selectivity(monkeypatch, tmp_path):
    """B-SF1 (judge-round slice 4) — 4.4/4.5 above use HOMOGENEOUS windows
    (all-excluded), which only proves 'excluded-only window -> 0 rows', not
    per-pair selectivity inside _collect_flush_drafts. This builds a MIXED
    live window (accumulated, direct, private) and asserts flush persists
    EXACTLY the one eligible direct pair, locking per-pair gating against a
    future 'gate once for the whole window' regression."""
    motor, _, _ = _make_motor()
    _enable_memorias(monkeypatch, motor, tmp_path)

    # accumulated-source pair -- excluded (not in _DIGEST_CAPTURE_SOURCES)
    motor.historial.append({"role": "user", "content": _ELIGIBLE_USER_2, "source": "accumulated", "private": False})
    motor.historial.append({"role": "assistant", "content": _ELIGIBLE_ASST_2, "source": "accumulated", "private": False})
    # direct-source pair -- eligible, the only one expected to survive
    motor.historial.append({"role": "user", "content": _ELIGIBLE_USER, "source": "direct", "private": False})
    motor.historial.append({"role": "assistant", "content": _ELIGIBLE_ASST, "source": "direct", "private": False})
    # private-tagged pair -- excluded
    motor.historial.append(
        {"role": "user", "content": "musica synthwave calma privado", "source": "direct", "private": True}
    )
    motor.historial.append(
        {"role": "assistant", "content": "Kira dice que es genial. Fin.", "source": "direct", "private": True}
    )

    motor.flush_memorias()

    rows = _store(tmp_path).list_for_profile("profile-1")
    assert len(rows) == 1
    expected_key = derive_stable_key("profile-1", f"{_ELIGIBLE_USER} {_ELIGIBLE_ASST}")
    assert rows[0]["stable_key"] == expected_key


# ---------------------------------------------------------------------------
# R14/RC-2 — profile-switch atomic critical section (4.6-4.8)
# ---------------------------------------------------------------------------

def test_profile_switch_snapshot_swap_clear_execute_as_one_atomic_critical_section(monkeypatch, tmp_path):
    motor, _, _ = _make_motor()
    _enable_memorias(monkeypatch, motor, tmp_path, profile_id="profile-old")

    motor.historial.append({"role": "user", "content": _ELIGIBLE_USER, "source": "direct", "private": False})
    motor.historial.append({"role": "assistant", "content": _ELIGIBLE_ASST, "source": "direct", "private": False})

    observed = {}
    original_collect = motor._collect_flush_drafts

    def spy_collect():
        observed["locked"] = motor._history_lock.locked()
        observed["profile_id_at_snapshot"] = motor._current_profile_id
        observed["historial_len_at_snapshot"] = len(motor.historial)
        return original_collect()

    monkeypatch.setattr(motor, "_collect_flush_drafts", spy_collect)
    monkeypatch.setattr(motor, "_dispatch_switch_flush", lambda *a, **k: None)

    motor._dispatch_command("set_profile", {
        "prompt": "New system prompt", "use_system": False,
        "_profile_name": "new-profile", "id": "profile-new",
    })

    assert observed["locked"] is True
    assert observed["profile_id_at_snapshot"] == "profile-old"
    assert observed["historial_len_at_snapshot"] == 2
    assert motor._current_profile_id == "profile-new"
    assert len(motor.historial) == 0
    assert motor._memory_digest.lines == []


def test_profile_switch_snapshots_and_clears_session_titles_under_same_lock(monkeypatch, tmp_path):
    """R3/W2a: the departing session's titles must be snapshotted AND cleared
    inside the SAME _history_lock acquisition as the drafts/historial snapshot,
    never in a second lock window a concurrent _capture_memoria append could
    slip into. Proven by observing, at the drafts-snapshot point, that the lock
    is held and the titles are still present — then that they are empty after
    the section and the exact snapshot was handed to the switch-flush."""
    motor, _, _ = _make_motor()
    _enable_memorias(monkeypatch, motor, tmp_path, profile_id="profile-old")

    motor._session_memoria_titles.extend(["title one a", "title two b"])
    motor.historial.append({"role": "user", "content": _ELIGIBLE_USER, "source": "direct", "private": False})
    motor.historial.append({"role": "assistant", "content": _ELIGIBLE_ASST, "source": "direct", "private": False})

    observed = {}
    original_collect = motor._collect_flush_drafts

    def spy_collect():
        observed["locked"] = motor._history_lock.locked()
        observed["titles_at_snapshot"] = list(motor._session_memoria_titles)
        return original_collect()

    monkeypatch.setattr(motor, "_collect_flush_drafts", spy_collect)

    dispatched = {}

    def spy_dispatch(drafts, summary_profile_id=None, summary_titles=None):
        dispatched["titles"] = summary_titles
        dispatched["profile_id"] = summary_profile_id

    monkeypatch.setattr(motor, "_dispatch_switch_flush", spy_dispatch)

    motor._dispatch_command("set_profile", {
        "prompt": "p", "use_system": False, "_profile_name": "new-profile", "id": "profile-new",
    })

    # Same critical section: lock held AND titles still present at the drafts snapshot.
    assert observed["locked"] is True
    assert observed["titles_at_snapshot"] == ["title one a", "title two b"]
    # Cleared inside that section -> the new profile's session starts empty.
    assert motor._session_memoria_titles == []
    # The departing snapshot (not the live list) rode to the switch-flush.
    assert dispatched["titles"] == ["title one a", "title two b"]
    assert dispatched["profile_id"] == "profile-old"


def test_profile_switch_disk_upserts_run_on_worker_thread_after_lock_release_never_block_ui(monkeypatch, tmp_path):
    motor, _, _ = _make_motor()
    _enable_memorias(monkeypatch, motor, tmp_path, profile_id="profile-old")

    motor.historial.append({"role": "user", "content": _ELIGIBLE_USER, "source": "direct", "private": False})
    motor.historial.append({"role": "assistant", "content": _ELIGIBLE_ASST, "source": "direct", "private": False})

    recorded = {}
    original_upsert = MemoriaStore.upsert_draft

    def spy_upsert(self, *args, **kwargs):
        recorded["thread"] = threading.current_thread()
        recorded["lock_held"] = motor._history_lock.locked()
        return original_upsert(self, *args, **kwargs)

    monkeypatch.setattr(MemoriaStore, "upsert_draft", spy_upsert)

    created = _run_set_profile_and_wait_for_flush(motor, monkeypatch, {
        "prompt": "p", "use_system": False, "_profile_name": "new-profile", "id": "profile-new",
    })

    assert len(created) == 1
    assert "thread" in recorded
    assert recorded["thread"] is created[0]
    assert recorded["thread"] is not threading.main_thread()
    assert recorded["lock_held"] is False

    rows = _store(tmp_path).list_for_profile("profile-old")
    assert len(rows) == 1


def test_concurrent_commit_during_profile_switch_never_misattributes_or_loses_turn(monkeypatch, tmp_path):
    """RC-2 closure proof: a concurrent _commit_history landing WHILE the
    set_profile critical section is executing must block on _history_lock
    (mutual exclusion), never interleave — so its turn is deterministically
    appended AFTER the swap+clear, tagged under the NEW profile, never
    silently dropped and never folded into the departing profile's snapshot."""
    motor, _, _ = _make_motor()
    _enable_memorias(monkeypatch, motor, tmp_path, profile_id="profile-old")

    motor.historial.append({"role": "user", "content": _ELIGIBLE_USER, "source": "direct", "private": False})
    motor.historial.append({"role": "assistant", "content": _ELIGIBLE_ASST, "source": "direct", "private": False})

    concurrent_started = threading.Event()
    concurrent_done = threading.Event()
    observed = {}

    original_collect = motor._collect_flush_drafts

    def spy_collect():
        result = original_collect()

        def concurrent_commit():
            concurrent_started.set()
            motor._commit_history("nueva pregunta concurrente", "nueva respuesta concurrente.", source="direct")
            concurrent_done.set()

        t = threading.Thread(target=concurrent_commit)
        t.start()
        concurrent_started.wait(timeout=2)
        time.sleep(0.05)  # let the concurrent thread reach and block on _history_lock
        observed["concurrent_done_before_release"] = concurrent_done.is_set()
        observed["thread"] = t
        return result

    monkeypatch.setattr(motor, "_collect_flush_drafts", spy_collect)

    motor._dispatch_command("set_profile", {
        "prompt": "p", "use_system": False, "_profile_name": "new-profile", "id": "profile-new",
    })

    observed["thread"].join(timeout=5)

    # Mutual exclusion proof: the concurrent commit could not complete while
    # the critical section was still mid-flight (lock held).
    assert observed["concurrent_done_before_release"] is False
    assert concurrent_done.is_set() is True

    # The concurrent turn lands in the post-switch (cleared) historial —
    # never lost, never attributed to the departing profile's snapshot.
    assert motor._current_profile_id == "profile-new"
    assert len(motor.historial) == 2
    assert motor.historial[0]["content"] == "nueva pregunta concurrente"


# ---------------------------------------------------------------------------
# RC-3/RC-8 — switch-flush fail-open + log hygiene (4.9-4.10)
# ---------------------------------------------------------------------------

def test_switch_flush_partial_failure_logs_count_only_never_content(monkeypatch, tmp_path, caplog):
    motor, _, _ = _make_motor()
    _enable_memorias(monkeypatch, motor, tmp_path, profile_id="profile-old")

    motor.historial.append({"role": "user", "content": _ELIGIBLE_USER, "source": "direct", "private": False})
    motor.historial.append({"role": "assistant", "content": _ELIGIBLE_ASST, "source": "direct", "private": False})
    motor.historial.append({"role": "user", "content": _ELIGIBLE_USER_2, "source": "direct", "private": False})
    motor.historial.append({"role": "assistant", "content": _ELIGIBLE_ASST_2, "source": "direct", "private": False})

    def failing_upsert(self, *args, **kwargs):
        raise RuntimeError("disk exploded")

    monkeypatch.setattr(MemoriaStore, "upsert_draft", failing_upsert)

    caplog.set_level(logging.WARNING)
    _run_set_profile_and_wait_for_flush(motor, monkeypatch, {
        "prompt": "p", "use_system": False, "_profile_name": "new-profile", "id": "profile-new",
    })

    assert "switch-flush" in caplog.text
    assert "2" in caplog.text  # failure COUNT only
    assert _ELIGIBLE_USER not in caplog.text
    assert _ELIGIBLE_ASST not in caplog.text


def test_switch_flush_secret_sentinel_absent_from_logs_on_failure_path(monkeypatch, tmp_path, caplog):
    motor, _, _ = _make_motor()
    _enable_memorias(monkeypatch, motor, tmp_path, profile_id="profile-old")

    sentinel = "SECRET_SHOULD_NOT_LOG"
    motor.historial.append(
        {"role": "user", "content": f"{sentinel} synthwave musica calma", "source": "direct", "private": False}
    )
    motor.historial.append({"role": "assistant", "content": _ELIGIBLE_ASST, "source": "direct", "private": False})

    def failing_upsert(self, *args, **kwargs):
        raise RuntimeError(f"disk exploded on {sentinel}")  # simulate a leaky exception message

    monkeypatch.setattr(MemoriaStore, "upsert_draft", failing_upsert)

    caplog.set_level(logging.WARNING)
    _run_set_profile_and_wait_for_flush(motor, monkeypatch, {
        "prompt": "p", "use_system": False, "_profile_name": "new-profile", "id": "profile-new",
    })

    assert sentinel not in caplog.text


def test_close_flush_secret_sentinel_absent_from_logs_on_failure_path(monkeypatch, tmp_path, caplog):
    """B-N1 (judge-round slice 4) — parity with
    test_switch_flush_secret_sentinel_absent_from_logs_on_failure_path above,
    for the CLOSE-flush path (flush_memorias). The upsert-failure log there
    must carry only type(exc).__name__ + profile_id, never the exception
    message/content, even when the exception message itself leaks a secret."""
    motor, _, _ = _make_motor()
    _enable_memorias(monkeypatch, motor, tmp_path)

    sentinel = "SECRET_SHOULD_NOT_LOG"
    motor.historial.append(
        {"role": "user", "content": f"{sentinel} synthwave musica calma", "source": "direct", "private": False}
    )
    motor.historial.append({"role": "assistant", "content": _ELIGIBLE_ASST, "source": "direct", "private": False})

    def failing_upsert(self, *args, **kwargs):
        raise RuntimeError(f"disk exploded on {sentinel}")  # simulate a leaky exception message

    monkeypatch.setattr(MemoriaStore, "upsert_draft", failing_upsert)

    caplog.set_level(logging.WARNING)
    motor.flush_memorias()

    assert sentinel not in caplog.text


# ---------------------------------------------------------------------------
# R14 — documented crash residual bound (4.11)
# ---------------------------------------------------------------------------

def test_hard_crash_loses_at_most_current_live_window(monkeypatch, tmp_path):
    """Documented accepted bound (design v2.1 SS6, residual risk #2): a hard
    crash/kill with no clean close and no profile switch loses at most the
    pairs still in the live, un-evicted window. Already-evicted pairs were
    captured write-through at eviction time and survive. This test PINS the
    accepted bound — it does not add periodic checkpointing (non-goal)."""
    motor, _, _ = _make_motor()
    _enable_memorias(monkeypatch, motor, tmp_path)

    _fill_history_to_max(motor)
    motor.historial[0] = {"role": "user", "content": _ELIGIBLE_USER, "source": "direct", "private": False}
    motor.historial[1] = {"role": "assistant", "content": _ELIGIBLE_ASST, "source": "direct", "private": False}
    # Evicts the pair above; write-through captured immediately at eviction.
    # B1 (memoria_quality_20260717): non-capturable trigger ("hola"/"chau.") so
    # capture-at-commit adds no unrelated row — only the evicted pair survives.
    motor._commit_history("hola", "chau.", source="direct")

    # A final pair enters the live window and is never evicted; no flush
    # (close/switch) is ever triggered -- simulates a hard kill.
    motor.historial.append(
        {"role": "user", "content": _ELIGIBLE_USER_2, "source": "direct", "private": False}
    )
    motor.historial.append(
        {"role": "assistant", "content": _ELIGIBLE_ASST_2, "source": "direct", "private": False}
    )

    rows = _store(tmp_path).list_for_profile("profile-1")
    assert len(rows) == 1  # only the evicted/write-through-captured pair survived

    still_live_key = derive_stable_key("profile-1", f"{_ELIGIBLE_USER_2} {_ELIGIBLE_ASST_2}")
    stored_keys = {row["stable_key"] for row in rows}
    assert still_live_key not in stored_keys


# ---------------------------------------------------------------------------
# B2a — EngineHost.stop() flushes memorias before the stop sentinel
# ---------------------------------------------------------------------------

def test_engine_host_stop_flushes_memorias_before_stop_sentinel(tmp_path):
    """B2a (memoria_quality_20260717): EngineHost.stop() must flush the live
    memorias window BEFORE dropping the engine-thread stop sentinel, so a clean
    API shutdown persists un-evicted pairs. Fail-open — a flush error never
    blocks the sentinel (belt-and-braces with B1 capture-at-commit)."""
    from opencohost.api import engine_host as engine_host_mod

    host = engine_host_mod.EngineHost(lock_path=str(tmp_path / "engine.lock"))
    calls: list[str] = []
    motor = MagicMock()
    motor.current_model = None  # skip the ollama unload generate() step in stop()
    motor.flush_memorias.side_effect = lambda *a, **k: calls.append("flush")
    motor.command_queue.put.side_effect = lambda *a, **k: calls.append("sentinel")
    host.motor = motor

    host.stop()

    assert "flush" in calls, "stop() never flushed memorias"
    assert "sentinel" in calls
    assert calls.index("flush") < calls.index("sentinel")


def _fill_history_to_max(motor, count=None):
    """Mirrors tests/test_memorias_capture_engine.py's _fill_history_to_max()."""
    from opencohost.config.settings import HISTORY_MAX_TURNS

    n = count if count is not None else HISTORY_MAX_TURNS
    for i in range(n):
        motor.historial.append({"role": "user", "content": f"user msg {i}", "source": "direct"})
        motor.historial.append(
            {"role": "assistant", "content": f"assistant reply {i}. This is a sentence.", "source": "direct"}
        )
