"""Durable profile-level backoff contracts for memoria promotion."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from opencohost.core.memory.promotion_backoff_store import (
    PROFILE_BACKOFF_DELAYS_S,
    PromotionBackoffStore,
)


def test_exact_sequence_clamps_and_later_failures_refresh_six_hours(
    tmp_path,
) -> None:
    store = PromotionBackoffStore(tmp_path / "promotion_backoff.db")
    expected = (60, 300, 1800, 7200, 21600)

    assert PROFILE_BACKOFF_DELAYS_S == expected
    for attempt, delay in enumerate(expected, start=1):
        now_s = 1_000 * attempt
        state = store.record_failure("profile-a", "model_transport", now_s)
        assert state.failure_count == attempt
        assert state.last_failure_at_s == now_s
        assert state.next_attempt_at_s == now_s + delay
        assert state.persisted is True
        assert store.is_active("profile-a", now_s + delay - 1) is True
        assert store.is_active("profile-a", now_s + delay) is False

    refreshed = store.record_failure(
        "profile-a", "model_server_error", 10_000,
    )
    assert refreshed.failure_count == 5
    assert refreshed.last_failure_at_s == 10_000
    assert refreshed.next_attempt_at_s == 31_600


def test_state_survives_restart_and_active_checks_never_increment(
    tmp_path,
) -> None:
    db_path = tmp_path / "promotion_backoff.db"
    first = PromotionBackoffStore(db_path)
    expected = first.record_failure("profile-a", "sqlite_read", 500)

    restarted = PromotionBackoffStore(db_path)
    for now_s in (500, 520, 559):
        assert restarted.is_active("profile-a", now_s) is True

    assert restarted.get_state("profile-a") == expected
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
        count = conn.execute(
            "SELECT failure_count FROM promotion_backoff "
            "WHERE profile_id = 'profile-a'"
        ).fetchone()[0]
    assert count == 1


def test_profiles_are_isolated_and_reset_deletes_only_one_profile(
    tmp_path,
) -> None:
    store = PromotionBackoffStore(tmp_path / "promotion_backoff.db")
    store.record_failure("profile-a", "sqlite_write", 100)
    state_b = store.record_failure("profile-b", "judge_watchdog", 200)

    assert store.reset("profile-a") is True
    assert store.get_state("profile-a") is None
    assert store.get_state("profile-b") == state_b
    assert store.is_active("profile-b", 259) is True


def test_backward_wall_clock_makes_profile_due_instead_of_extending_wait(
    tmp_path,
) -> None:
    store = PromotionBackoffStore(tmp_path / "promotion_backoff.db")
    state = store.record_failure("profile-a", "model_transport", 1_000)

    assert state.next_attempt_at_s == 1_060
    assert store.is_active("profile-a", 999) is False
    assert store.is_active("profile-a", 1_000) is True


def test_sidecar_write_failure_uses_conservative_in_process_fallback(
    monkeypatch, tmp_path,
) -> None:
    store = PromotionBackoffStore(tmp_path / "promotion_backoff.db")

    def locked(*_args, **_kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(store, "_connect", locked)
    state = store.record_failure("profile-a", "sqlite_write", 100)

    assert state.persisted is False
    assert state.failure_count == 5
    assert state.next_attempt_at_s == 100 + 21_600
    assert store.is_active("profile-a", 101) is True
    assert store.get_state("profile-a") == state


def test_failed_reset_keeps_a_conservative_backoff_active(
    monkeypatch, tmp_path,
) -> None:
    store = PromotionBackoffStore(tmp_path / "promotion_backoff.db")
    store.record_failure("profile-a", "sqlite_read", 100)

    def locked(*_args, **_kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(store, "_connect", locked)

    assert store.reset("profile-a", now_s=120) is False
    assert store.is_active("profile-a", 121) is True
    assert store.get_state("profile-a").persisted is False


def test_failed_reset_refreshes_an_expired_fallback(
    monkeypatch, tmp_path,
) -> None:
    store = PromotionBackoffStore(tmp_path / "promotion_backoff.db")

    def locked(*_args, **_kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(store, "_connect", locked)
    expired = store.record_failure("profile-a", "sqlite_write", 100)
    now_s = expired.next_attempt_at_s

    assert store.is_active("profile-a", now_s) is False
    assert store.reset("profile-a", now_s=now_s) is False

    refreshed = store.get_state("profile-a")
    assert refreshed is not None
    assert refreshed.last_failure_at_s == now_s
    assert refreshed.next_attempt_at_s == now_s + 21_600
    assert refreshed.persisted is False
    assert store.is_active("profile-a", now_s) is True


def test_sidecar_directory_failure_still_returns_nonpersistent_fallback(
    monkeypatch, tmp_path,
) -> None:
    db_path = tmp_path / "missing" / "promotion_backoff.db"
    real_mkdir = Path.mkdir

    def fail_target(self, *args, **kwargs):
        if self == db_path.parent:
            raise OSError("directory unavailable")
        return real_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", fail_target)

    store = PromotionBackoffStore(db_path)
    state = store.record_failure("profile-a", "sqlite_write", 100)

    assert state.persisted is False
    assert state.failure_count == 5
    assert state.next_attempt_at_s == 21_700
