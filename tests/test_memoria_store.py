"""Tests for opencohost.core.memoria_store — MemoriaStore.

Design contract (engram sdd/kira-memory-persistence-20260701/design v2.1,
section 1-3): own unshared SQLite file (memorias.db, PRAGMA user_version=1),
single guarded upsert (INSERT .. ON CONFLICT(profile_id, stable_key) DO UPDATE ..
WHERE status='draft'), unified curation rule F5 (edit/pin/private all
promote to curated in the same statement; inactive is the sole pure
visibility flag), stable_key/title derivation via domain-stopword filtering
on top of editorial_matching.normalize_tokens (RC-1/RC-7), bounded
read/write timeouts with fail-open behavior (agenda_persistence.py
precedent), per-profile growth cap (prune oldest unpinned drafts beyond
MEMORIAS_PROFILE_CAP after each successful INSERT), and log hygiene (RC-8:
failure logs/exception messages never carry row title or content).

Slice 2 scope only: this module is not yet wired into the engine or UI
(MEMORIAS_ENABLED stays False) — it is exercised only by these unit tests.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

import pytest

from opencohost.core.memoria_store import (
    READ_TIMEOUT_SECONDS,
    WRITE_TIMEOUT_SECONDS,
    MemoriaStore,
    MemoriaValidationError,
    build_title,
    derive_stable_key,
    is_capturable,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _seed_raw_drafts(db_path, profile_id, count, *, pinned_ids=frozenset(), curated_ids=frozenset()):
    """Insert *count* rows directly (bypassing upsert_draft) with strictly
    increasing updated_at timestamps, so recency ordering is deterministic.
    Used only to make growth-cap tests fast — production code always goes
    through upsert_draft.
    """
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    ids = [f"seed-{profile_id}-{i}" for i in range(count)]
    with sqlite3.connect(str(db_path)) as conn:
        for i, row_id in enumerate(ids):
            ts = (base + timedelta(seconds=i)).isoformat()
            status = "curated" if row_id in curated_ids else "draft"
            pinned = 1 if row_id in pinned_ids else 0
            conn.execute(
                "INSERT INTO memorias (id, profile_id, stable_key, revision, title, "
                "content, status, pinned, private, inactive, created_at, updated_at) "
                "VALUES (?, ?, ?, 1, ?, ?, ?, ?, 0, 0, ?, ?)",
                (row_id, profile_id, f"{profile_id}|seed-{i}", f"titulo {i}", f"contenido {i}", status, pinned, ts, ts),
            )
    return ids


# ---------------------------------------------------------------------------
# Schema (2.1)
# ---------------------------------------------------------------------------

def test_memorias_db_created_at_user_data_dir_with_user_version_1(tmp_path) -> None:
    from opencohost.config.settings import MEMORIAS_DB
    from opencohost.config.storage import USER_DATA_DIR

    assert MEMORIAS_DB.startswith(str(USER_DATA_DIR))
    assert "memorias.db" in MEMORIAS_DB

    # Schema/PRAGMA verification happens against an isolated tmp path —
    # never touch the real USER_DATA_DIR during tests.
    db_path = tmp_path / "memorias" / "memorias.db"
    MemoriaStore(db_path)
    assert db_path.exists()
    with sqlite3.connect(str(db_path)) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
        cols = {row[1] for row in conn.execute("PRAGMA table_info(memorias)").fetchall()}
    assert cols == {
        "id", "profile_id", "stable_key", "revision", "title", "content",
        "status", "pinned", "private", "inactive", "created_at", "updated_at",
    }


# ---------------------------------------------------------------------------
# Guarded upsert + status transitions (2.2-2.8, F5)
# ---------------------------------------------------------------------------

def test_upsert_insert_creates_draft_row_revision_1(tmp_path) -> None:
    store = MemoriaStore(tmp_path / "memorias.db")
    row_id = store.upsert_draft(
        "profile-1", "profile-1|alpha-beta-gamma", "titulo alpha beta", "contenido alpha beta gamma"
    )
    assert row_id is not None
    row = store.get(row_id)
    assert row["revision"] == 1
    assert row["status"] == "draft"
    assert row["title"] == "titulo alpha beta"
    assert row["content"] == "contenido alpha beta gamma"
    assert row["profile_id"] == "profile-1"
    assert row["pinned"] == 0
    assert row["private"] == 0
    assert row["inactive"] == 0


def test_upsert_same_stable_key_bumps_revision_replaces_draft_content(tmp_path) -> None:
    store = MemoriaStore(tmp_path / "memorias.db")
    key = "profile-1|alpha-beta-gamma"
    first_id = store.upsert_draft("profile-1", key, "titulo viejo", "contenido viejo aqui")
    second_id = store.upsert_draft("profile-1", key, "titulo nuevo", "contenido nuevo aqui")

    assert second_id == first_id
    row = store.get(first_id)
    assert row["revision"] == 2
    assert row["title"] == "titulo nuevo"
    assert row["content"] == "contenido nuevo aqui"
    assert row["status"] == "draft"


def test_curated_row_is_upsert_immune(tmp_path) -> None:
    store = MemoriaStore(tmp_path / "memorias.db")
    key = "profile-1|alpha-beta-gamma"
    row_id = store.upsert_draft("profile-1", key, "titulo original", "contenido original aqui")
    store.update_row(row_id, title="titulo curado", content="contenido curado aqui")

    result = store.upsert_draft("profile-1", key, "titulo pisado", "contenido pisado aqui")
    assert result is None  # curated-immune: no write happened

    row = store.get(row_id)
    assert row["title"] == "titulo curado"
    assert row["content"] == "contenido curado aqui"
    assert row["status"] == "curated"
    assert row["revision"] == 1  # untouched by the immune write attempt


def test_concurrent_upserts_against_curated_row_never_mutate_content(tmp_path) -> None:
    """RC-1/A-MF3: the WHERE-clause immunity must hold under real concurrency,
    not just sequential ordering."""
    store = MemoriaStore(tmp_path / "memorias.db")
    key = "profile-1|alpha-beta-gamma"
    row_id = store.upsert_draft("profile-1", key, "titulo original", "contenido original aqui")
    store.update_row(row_id, title="titulo curado", content="contenido curado aqui")

    def attempt(i: int) -> None:
        store.upsert_draft("profile-1", key, f"titulo intento {i}", f"contenido intento numero {i}")

    threads = [threading.Thread(target=attempt, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    row = store.get(row_id)
    assert row["title"] == "titulo curado"
    assert row["content"] == "contenido curado aqui"
    assert row["status"] == "curated"


def test_edit_promotes_status_to_curated_in_same_statement(tmp_path) -> None:
    store = MemoriaStore(tmp_path / "memorias.db")
    row_id = store.upsert_draft("profile-1", "profile-1|alpha-beta-gamma", "titulo alpha", "contenido alpha beta gamma")
    assert store.get(row_id)["status"] == "draft"

    ok = store.update_row(row_id, title="titulo editado")
    assert ok is True

    row = store.get(row_id)
    assert row["status"] == "curated"
    assert row["title"] == "titulo editado"


def test_pin_promotes_status_to_curated_in_same_statement(tmp_path) -> None:
    store = MemoriaStore(tmp_path / "memorias.db")
    row_id = store.upsert_draft("profile-1", "profile-1|alpha-beta-gamma", "titulo alpha", "contenido alpha beta gamma")
    assert store.get(row_id)["status"] == "draft"

    ok = store.set_flags(row_id, pinned=True)
    assert ok is True

    row = store.get(row_id)
    assert row["pinned"] == 1
    assert row["status"] == "curated"


def test_mark_private_promotes_status_to_curated_in_same_statement(tmp_path) -> None:
    store = MemoriaStore(tmp_path / "memorias.db")
    row_id = store.upsert_draft("profile-1", "profile-1|alpha-beta-gamma", "titulo alpha", "contenido alpha beta gamma")
    assert store.get(row_id)["status"] == "draft"

    ok = store.set_flags(row_id, private=True)
    assert ok is True

    row = store.get(row_id)
    assert row["private"] == 1
    assert row["status"] == "curated"


def test_deactivation_alone_does_not_curate_row_remains_refreshable(tmp_path) -> None:
    store = MemoriaStore(tmp_path / "memorias.db")
    key = "profile-1|alpha-beta-gamma"
    row_id = store.upsert_draft("profile-1", key, "titulo alpha", "contenido alpha beta gamma")

    ok = store.set_flags(row_id, inactive=True)
    assert ok is True
    row = store.get(row_id)
    assert row["inactive"] == 1
    assert row["status"] == "draft"  # still refreshable — not a content judgment

    refreshed_id = store.upsert_draft("profile-1", key, "titulo nuevo", "contenido nuevo aqui")
    assert refreshed_id == row_id
    row = store.get(row_id)
    assert row["revision"] == 2
    assert row["title"] == "titulo nuevo"
    assert row["inactive"] == 1  # flag survives the refresh untouched


def test_unpin_or_unmark_private_never_demotes_curated_row_to_draft(tmp_path) -> None:
    store = MemoriaStore(tmp_path / "memorias.db")
    row_id = store.upsert_draft("profile-1", "profile-1|alpha-beta-gamma", "titulo alpha", "contenido alpha beta gamma")
    store.set_flags(row_id, pinned=True)
    assert store.get(row_id)["status"] == "curated"

    store.set_flags(row_id, pinned=False)
    row = store.get(row_id)
    assert row["pinned"] == 0
    assert row["status"] == "curated"  # one-way — never auto-reverts


# ---------------------------------------------------------------------------
# stable_key / title derivation (2.9-2.12, RC-1/RC-7)
# ---------------------------------------------------------------------------

def test_distinct_intent_pairs_shared_generic_vocab_produce_distinct_stable_keys() -> None:
    key_a = derive_stable_key("profile-1", "streamer prefiere musica synthwave calma")
    key_b = derive_stable_key("profile-1", "streamer prefiere juego shooter rapido")

    assert key_a is not None and key_b is not None
    assert key_a != key_b


def test_stable_key_uses_domain_stopwords_on_top_of_normalize_tokens() -> None:
    key = derive_stable_key(
        "profile-1",
        "kira obs tema perfil usuario recuerda dice streamer chat musica synthwave calma",
    )
    assert key is not None
    assert "kira" not in key
    assert "obs" not in key
    assert "tema" not in key
    assert "perfil" not in key
    assert "usuario" not in key
    assert "recuerda" not in key
    assert "dice" not in key
    assert "streamer" not in key
    assert "chat" not in key
    assert "musica" in key
    assert "synthwave" in key
    assert "calma" in key


def test_derive_stable_key_returns_none_below_significant_token_minimum() -> None:
    # Only "musica" survives domain-stopword filtering -> 1 significant token.
    assert derive_stable_key("profile-1", "kira obs streamer musica") is None
    assert is_capturable("kira obs streamer musica") is False

    # 4 significant tokens -> meets the >=3 minimum.
    assert derive_stable_key("profile-1", "musica synthwave calma nocturna") is not None
    assert is_capturable("musica synthwave calma nocturna") is True


def test_all_generic_vocabulary_text_produces_no_stable_key() -> None:
    text = "el streamer dice que kira recuerda el tema del perfil del usuario en el chat"
    assert derive_stable_key("profile-1", text) is None


def test_stable_key_revision_count_metric_logs_id_only_never_content(tmp_path, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    store = MemoriaStore(tmp_path / "memorias.db")
    key = "profile-1|alpha-beta-gamma"
    store.upsert_draft("profile-1", key, "titulo secreto alpha", "contenido secreto beta gamma")

    caplog.clear()
    row_id = store.upsert_draft("profile-1", key, "titulo nuevo delta", "contenido nuevo epsilon zeta")

    assert row_id is not None
    assert key in caplog.text
    assert "2" in caplog.text  # revision bumped to 2
    assert "titulo nuevo delta" not in caplog.text
    assert "contenido nuevo epsilon zeta" not in caplog.text


def test_significant_token_title_excludes_domain_stopwords() -> None:
    title = build_title("kira obs streamer musica synthwave calma nocturna")
    assert "kira" not in title
    assert "obs" not in title
    assert "streamer" not in title
    assert title == "musica synthwave calma"  # first 3 significant, appearance order


# ---------------------------------------------------------------------------
# Bounded timeouts + fail-open (2.13-2.16, R5/RC-8)
# ---------------------------------------------------------------------------

def test_write_through_off_tk_thread_bounded_by_read_write_timeouts(tmp_path) -> None:
    import time

    db_path = tmp_path / "memorias.db"
    store = MemoriaStore(db_path)

    blocker = sqlite3.connect(str(db_path))
    blocker.execute("BEGIN EXCLUSIVE")
    try:
        start = time.monotonic()
        result = store.upsert_draft(
            "profile-1", "profile-1|alpha-beta-gamma", "titulo alpha beta", "contenido alpha beta gamma"
        )
        elapsed = time.monotonic() - start
    finally:
        blocker.rollback()
        blocker.close()

    assert result is None  # fail-open: no exception escapes the caller
    # Windows sqlite retry backoff can overshoot the requested timeout a bit;
    # this just proves the wait is BOUNDED, not indefinite.
    assert elapsed < WRITE_TIMEOUT_SECONDS + 2.0


def test_store_failure_does_not_hold_history_lock_during_io(tmp_path) -> None:
    """Store-level analog of R5's 'never hold _history_lock during I/O': a
    failed write must not leave any internal lock held — a normal write
    immediately after a failed one must succeed without extra delay, proving
    the store holds no state across the failed attempt that could serialize
    or block a caller (e.g. the engine's _history_lock in later slices)."""
    db_path = tmp_path / "memorias.db"
    store = MemoriaStore(db_path)

    blocker = sqlite3.connect(str(db_path))
    blocker.execute("BEGIN EXCLUSIVE")
    result_during = store.upsert_draft(
        "profile-1", "profile-1|alpha-beta-gamma", "titulo alpha beta", "contenido alpha beta gamma"
    )
    blocker.rollback()
    blocker.close()
    assert result_during is None

    result_after = store.upsert_draft(
        "profile-1", "profile-1|delta-epsilon-zeta", "titulo delta epsilon", "contenido delta epsilon zeta"
    )
    assert result_after is not None


def test_failure_logs_never_leak_draft_title_or_content(tmp_path, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    db_path = tmp_path / "memorias.db"
    store = MemoriaStore(db_path)
    marker = "MI_TITULO_SECRETO_UNICO_XYZ"

    blocker = sqlite3.connect(str(db_path))
    blocker.execute("BEGIN EXCLUSIVE")
    result = store.upsert_draft(
        "profile-1", "profile-1|alpha-beta-gamma", f"{marker} titulo", f"{marker} contenido aqui"
    )
    blocker.rollback()
    blocker.close()

    assert result is None
    assert marker not in caplog.text


def test_secret_sentinel_absent_from_logs_across_sqlite_and_validation_error_paths(tmp_path, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    db_path = tmp_path / "memorias.db"
    store = MemoriaStore(db_path)
    sentinel = "SECRET_SHOULD_NOT_LOG"

    # Validation-error path: empty stable_key with sentinel-laden title/content.
    with pytest.raises(MemoriaValidationError) as exc_info:
        store.upsert_draft("profile-1", "", f"{sentinel} titulo", f"{sentinel} contenido")
    assert sentinel not in str(exc_info.value)
    assert sentinel not in caplog.text

    caplog.clear()

    # sqlite-error path: valid inputs, but the db is locked by another connection.
    blocker = sqlite3.connect(str(db_path))
    blocker.execute("BEGIN EXCLUSIVE")
    result = store.upsert_draft(
        "profile-1",
        "profile-1|alpha-beta-gamma",
        f"{sentinel} titulo alpha beta",
        f"{sentinel} contenido alpha beta gamma",
    )
    blocker.rollback()
    blocker.close()

    assert result is None
    assert sentinel not in caplog.text


def test_read_operations_fail_open_on_locked_db(tmp_path) -> None:
    db_path = tmp_path / "memorias.db"
    store = MemoriaStore(db_path)
    row_id = store.upsert_draft("profile-1", "profile-1|alpha-beta-gamma", "titulo alpha", "contenido alpha beta gamma")

    blocker = sqlite3.connect(str(db_path))
    blocker.execute("BEGIN EXCLUSIVE")
    try:
        assert store.get(row_id) is None
        assert store.list_for_profile("profile-1") == []
        assert store.purge_profile("profile-1") == 0
    finally:
        blocker.rollback()
        blocker.close()

    assert store.get(row_id) is not None


def test_lock_contended_update_row_returns_false_not_true_or_raising(tmp_path) -> None:
    """A-SF1: a lock-lost curation must be observable — update_row returns
    False (never raises, never silently succeeds) so callers that require
    the freeze/edit to have taken effect (management UI, slice 6/7) can
    surface the failure. A False here means the row is NOT curated and
    remains auto-capture-eligible."""
    db_path = tmp_path / "memorias.db"
    store = MemoriaStore(db_path)
    row_id = store.upsert_draft(
        "profile-1", "profile-1|alpha-beta-gamma", "titulo alpha", "contenido alpha beta gamma"
    )

    blocker = sqlite3.connect(str(db_path))
    blocker.execute("BEGIN EXCLUSIVE")
    try:
        ok = store.update_row(row_id, title="titulo editado")
    finally:
        blocker.rollback()
        blocker.close()

    assert ok is False
    row = store.get(row_id)
    assert row["status"] == "draft"  # curation did NOT take effect
    assert row["title"] == "titulo alpha"


def test_lock_contended_set_flags_returns_false_not_true_or_raising(tmp_path) -> None:
    """A-SF1: same contract as update_row — a lock-lost pin/private toggle
    must return False, not True and not raise, so the row is known to remain
    uncurated/auto-capture-eligible."""
    db_path = tmp_path / "memorias.db"
    store = MemoriaStore(db_path)
    row_id = store.upsert_draft(
        "profile-1", "profile-1|alpha-beta-gamma", "titulo alpha", "contenido alpha beta gamma"
    )

    blocker = sqlite3.connect(str(db_path))
    blocker.execute("BEGIN EXCLUSIVE")
    try:
        ok = store.set_flags(row_id, pinned=True)
    finally:
        blocker.rollback()
        blocker.close()

    assert ok is False
    row = store.get(row_id)
    assert row["status"] == "draft"  # curation did NOT take effect
    assert row["pinned"] == 0


# ---------------------------------------------------------------------------
# Profile isolation + purge (2.17-2.19, R7/R8)
# ---------------------------------------------------------------------------

def test_profile_isolation_query_never_returns_other_profile_rows(tmp_path) -> None:
    store = MemoriaStore(tmp_path / "memorias.db")
    store.upsert_draft("profile-1", "profile-1|alpha-beta-gamma", "titulo uno", "contenido uno dos tres")
    store.upsert_draft("profile-2", "profile-2|delta-epsilon-zeta", "titulo dos", "contenido cuatro cinco seis")

    rows_p1 = store.list_for_profile("profile-1")
    rows_p2 = store.list_for_profile("profile-2")

    assert len(rows_p1) == 1 and len(rows_p2) == 1
    assert {r["profile_id"] for r in rows_p1} == {"profile-1"}
    assert {r["profile_id"] for r in rows_p2} == {"profile-2"}


def test_colliding_stable_key_across_profiles_never_cross_profile_overwrites(tmp_path) -> None:
    """B-S1: derive_stable_key always prefixes with {profile_id}| today, so
    this is defense-in-depth for R7 — if a future derivation bug ever emits
    a colliding un-prefixed key for two different profiles, the store's
    (profile_id, stable_key) composite uniqueness must produce two distinct
    rows, never let profile B's write silently overwrite profile A's row
    content while keeping profile A's profile_id."""
    store = MemoriaStore(tmp_path / "memorias.db")
    colliding_key = "collision-without-profile-prefix"

    id_a = store.upsert_draft("profile-a", colliding_key, "titulo a", "contenido original de a")
    id_b = store.upsert_draft("profile-b", colliding_key, "titulo b", "contenido original de b")

    assert id_a is not None
    assert id_b is not None
    assert id_a != id_b  # distinct rows for distinct profiles, never merged

    row_a = store.get(id_a)
    row_b = store.get(id_b)
    assert row_a["profile_id"] == "profile-a"
    assert row_a["content"] == "contenido original de a"  # untouched by b's write
    assert row_b["profile_id"] == "profile-b"
    assert row_b["content"] == "contenido original de b"


def test_purge_profile_deletes_all_rows_scoped_to_active_profile_only(tmp_path) -> None:
    store = MemoriaStore(tmp_path / "memorias.db")
    store.upsert_draft("profile-1", "profile-1|alpha-beta-gamma", "t1", "c1 c2 c3")
    store.upsert_draft("profile-1", "profile-1|delta-epsilon-zeta", "t2", "c4 c5 c6")

    deleted = store.purge_profile("profile-1")

    assert deleted == 2
    assert store.list_for_profile("profile-1") == []


def test_purge_profile_leaves_other_profiles_untouched(tmp_path) -> None:
    store = MemoriaStore(tmp_path / "memorias.db")
    store.upsert_draft("profile-1", "profile-1|alpha-beta-gamma", "t1", "c1 c2 c3")
    store.upsert_draft("profile-2", "profile-2|delta-epsilon-zeta", "t2", "c4 c5 c6")

    store.purge_profile("profile-1")

    assert store.list_for_profile("profile-1") == []
    remaining = store.list_for_profile("profile-2")
    assert len(remaining) == 1
    assert remaining[0]["profile_id"] == "profile-2"


# ---------------------------------------------------------------------------
# Growth cap (2.20-2.21, R16)
# ---------------------------------------------------------------------------

def test_growth_cap_prunes_oldest_unpinned_drafts_beyond_200_per_profile(tmp_path) -> None:
    from opencohost.config.settings import MEMORIAS_PROFILE_CAP

    db_path = tmp_path / "memorias.db"
    store = MemoriaStore(db_path)
    seeded_ids = _seed_raw_drafts(db_path, "profile-1", MEMORIAS_PROFILE_CAP + 5)

    new_id = store.upsert_draft(
        "profile-1", "profile-1|fresh-unique-key", "titulo fresco", "contenido fresco nuevo aqui"
    )

    rows = store.list_for_profile("profile-1", limit=10_000)
    kept_ids = {r["id"] for r in rows}

    assert len(rows) == MEMORIAS_PROFILE_CAP
    assert new_id in kept_ids
    assert seeded_ids[0] not in kept_ids  # oldest seeded row pruned
    assert seeded_ids[-1] in kept_ids  # most recently seeded row survives


def test_growth_cap_never_prunes_curated_or_pinned_rows(tmp_path) -> None:
    from opencohost.config.settings import MEMORIAS_PROFILE_CAP

    db_path = tmp_path / "memorias.db"
    store = MemoriaStore(db_path)
    seeded_ids = _seed_raw_drafts(
        db_path,
        "profile-1",
        MEMORIAS_PROFILE_CAP + 5,
        pinned_ids={"seed-profile-1-0", "seed-profile-1-1"},
        curated_ids={"seed-profile-1-2"},
    )

    store.upsert_draft(
        "profile-1", "profile-1|fresh-unique-key-2", "titulo fresco dos", "contenido fresco nuevo dos tres"
    )

    rows = store.list_for_profile("profile-1", limit=10_000)
    kept_ids = {r["id"] for r in rows}

    assert seeded_ids[0] in kept_ids  # pinned, never pruned
    assert seeded_ids[1] in kept_ids  # pinned, never pruned
    assert seeded_ids[2] in kept_ids  # curated, never pruned
