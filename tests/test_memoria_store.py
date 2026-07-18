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

MemoriaStore itself never reads MEMORIAS_ENABLED — that gate lives in
llm_engine's capture hook (wired in as of slice 8, where the flag defaults
True). These tests instantiate MemoriaStore directly against a tmp_path db
and MUST keep doing so — NEVER against the real USER_DATA_DIR/memorias.db.
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
    _SESSION_SUMMARY_MARKER,
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

def test_memorias_db_created_at_user_data_dir_with_user_version_2(tmp_path) -> None:
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
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
        cols = {row[1] for row in conn.execute("PRAGMA table_info(memorias)").fetchall()}
    assert cols == {
        "id", "profile_id", "stable_key", "revision", "title", "content",
        "status", "pinned", "private", "inactive", "created_at", "updated_at",
        "signature",
    }


# ---------------------------------------------------------------------------
# Schema migration v1 -> v2 (memoria_rag_followups_20260716, candidate 2)
# ---------------------------------------------------------------------------

_V1_DDL = """
CREATE TABLE IF NOT EXISTS memorias (
    id TEXT PRIMARY KEY,
    profile_id TEXT NOT NULL,
    stable_key TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft',
    pinned INTEGER NOT NULL DEFAULT 0,
    private INTEGER NOT NULL DEFAULT 0,
    inactive INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(profile_id, stable_key)
)
"""


def _create_v1_db(db_path, rows=()) -> None:
    """Build a pre-migration (user_version=1, no signature column) db by hand,
    byte-equivalent to what _init_db produced before candidate 2."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(_V1_DDL)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_memorias_profile_id ON memorias(profile_id)")
        conn.execute("PRAGMA user_version = 1")
        for row in rows:
            conn.execute(
                "INSERT INTO memorias (id, profile_id, stable_key, revision, title, "
                "content, status, pinned, private, inactive, created_at, updated_at) "
                "VALUES (?, ?, ?, 1, ?, ?, 'draft', 0, 0, 0, ?, ?)",
                (
                    row["id"], row["profile_id"], row["stable_key"],
                    row["title"], row["content"],
                    "2026-07-15T00:00:00+00:00", "2026-07-15T00:00:00+00:00",
                ),
            )


def test_legacy_v1_db_migrates_signature_column_and_backfills(tmp_path) -> None:
    from opencohost.core.memoria_store import build_signature

    db_path = tmp_path / "memorias.db"
    # Legacy 2026-07-15 rows are NAME-keyed (stable_key prefix is the profile
    # NAME, not the UUID) — the migration must backfill signature from the
    # existing title+content and must NEVER touch stable_key.
    legacy_key = "Guh|calma-musica-synthwave"
    _create_v1_db(db_path, rows=[{
        "id": "mem_legacy_1", "profile_id": "profile-1", "stable_key": legacy_key,
        "title": "musica synthwave calma",
        "content": "contexto: streamer prefiere musica synthwave calma nocturna",
    }])

    MemoriaStore(db_path)

    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
        cols = {row[1] for row in conn.execute("PRAGMA table_info(memorias)").fetchall()}
        assert "signature" in cols
        row = conn.execute("SELECT * FROM memorias WHERE id = 'mem_legacy_1'").fetchone()

    expected = build_signature("musica synthwave calma contexto: streamer prefiere musica synthwave calma nocturna")
    assert expected != ""
    assert row["signature"] == expected
    assert row["stable_key"] == legacy_key  # backfill never rewrites keys


def test_migration_is_idempotent_on_second_construction(tmp_path) -> None:
    db_path = tmp_path / "memorias.db"
    _create_v1_db(db_path, rows=[{
        "id": "mem_legacy_1", "profile_id": "profile-1", "stable_key": "Guh|calma-musica-synthwave",
        "title": "musica synthwave calma",
        "content": "contexto: streamer prefiere musica synthwave calma nocturna",
    }])

    MemoriaStore(db_path)  # migrates 1 -> 2
    # Plant a marker to prove the gated ALTER+backfill block does NOT rerun.
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute("UPDATE memorias SET signature = 'marker-must-survive' WHERE id = 'mem_legacy_1'")

    MemoriaStore(db_path)  # must not raise (no duplicate-column error)

    with sqlite3.connect(str(db_path)) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
        sig = conn.execute("SELECT signature FROM memorias WHERE id = 'mem_legacy_1'").fetchone()[0]
    assert sig == "marker-must-survive"  # backfill did not rerun


def test_interrupted_migration_column_present_version_stale_self_heals(tmp_path) -> None:
    """4R blocker fix (memoria_rag_followups_20260716 correction round):
    sqlite3's legacy transaction control commits the ALTER TABLE (DDL)
    immediately — a kill after ALTER but before the version bump leaves the
    column present with user_version still < 2. Construction must survive
    (no duplicate-column crash), backfill, and bump the version."""
    from opencohost.core.memoria_store import build_signature

    db_path = tmp_path / "memorias.db"
    _create_v1_db(db_path, rows=[{
        "id": "mem_legacy_1", "profile_id": "profile-1", "stable_key": "Guh|calma-musica-synthwave",
        "title": "musica synthwave calma",
        "content": "contexto: streamer prefiere musica synthwave calma nocturna",
    }])
    # Simulate the interrupted state: ALTER committed, version bump lost.
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute("ALTER TABLE memorias ADD COLUMN signature TEXT NOT NULL DEFAULT ''")

    MemoriaStore(db_path)  # must NOT raise duplicate-column

    with sqlite3.connect(str(db_path)) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
        sig = conn.execute("SELECT signature FROM memorias WHERE id = 'mem_legacy_1'").fetchone()[0]
    expected = build_signature("musica synthwave calma contexto: streamer prefiere musica synthwave calma nocturna")
    assert sig == expected  # backfill still ran on the resumed migration


def test_interrupted_mid_backfill_resume_fills_only_empty_signatures(tmp_path) -> None:
    """Resume path: column present, user_version < 2, SOME rows already
    backfilled. Construction must fill only the empty ones (never rewrite an
    already-set signature) and bump the version."""
    from opencohost.core.memoria_store import build_signature

    db_path = tmp_path / "memorias.db"
    _create_v1_db(db_path, rows=[
        {
            "id": "mem_done", "profile_id": "profile-1", "stable_key": "Guh|calma-musica-synthwave",
            "title": "musica synthwave calma",
            "content": "contexto: streamer prefiere musica synthwave calma nocturna",
        },
        {
            "id": "mem_pending", "profile_id": "profile-1", "stable_key": "Guh|juegos-estrategia-turnos",
            "title": "juegos estrategia turnos",
            "content": "contexto: streamer prefiere juegos de estrategia por turnos",
        },
    ])
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute("ALTER TABLE memorias ADD COLUMN signature TEXT NOT NULL DEFAULT ''")
        conn.execute("UPDATE memorias SET signature = 'already-backfilled-marker' WHERE id = 'mem_done'")

    MemoriaStore(db_path)

    with sqlite3.connect(str(db_path)) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
        done = conn.execute("SELECT signature FROM memorias WHERE id = 'mem_done'").fetchone()[0]
        pending = conn.execute("SELECT signature FROM memorias WHERE id = 'mem_pending'").fetchone()[0]
    assert done == "already-backfilled-marker"  # resume never rewrites filled rows
    assert pending == build_signature(
        "juegos estrategia turnos contexto: streamer prefiere juegos de estrategia por turnos"
    )


def test_fresh_db_lands_directly_at_user_version_2_with_signature_column(tmp_path) -> None:
    db_path = tmp_path / "fresh" / "memorias.db"

    MemoriaStore(db_path)

    with sqlite3.connect(str(db_path)) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
        cols = {row[1] for row in conn.execute("PRAGMA table_info(memorias)").fetchall()}
    assert "signature" in cols  # no user ever sees a version-1 schema


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


def test_upsert_draft_return_created_reports_insert_vs_update(tmp_path) -> None:
    """E1 (memoria_quality_20260717): with return_created=True, upsert_draft
    reports whether the write was a FRESH insert (revision==1 -> created True)
    or a refresh of an existing draft (created False). The default return
    contract (a bare id string) is unchanged -- only the opt-in kwarg widens
    the return so the engine can fire the memoria.captured notice on a genuine
    new memoria only (not on every idempotent re-upsert)."""
    store = MemoriaStore(tmp_path / "memorias.db")
    key = "profile-1|alpha-beta-gamma"

    row_id, created = store.upsert_draft(
        "profile-1", key, "titulo alpha", "contenido alpha beta gamma", return_created=True
    )
    assert created is True
    assert row_id is not None

    refreshed_id, created_again = store.upsert_draft(
        "profile-1", key, "titulo nuevo", "contenido nuevo aqui", return_created=True
    )
    assert created_again is False
    assert refreshed_id == row_id  # same row, revision bumped

    # Curated-immune / no-write path must report (None, False) -- callers can
    # always unpack the pair, never crash on a no-op write.
    store.update_row(row_id, title="titulo curado")
    immune_id, immune_created = store.upsert_draft(
        "profile-1", key, "titulo pisado", "contenido pisado aqui", return_created=True
    )
    assert immune_id is None
    assert immune_created is False

    # Backward compatibility: the default call (no kwarg) still returns a bare
    # id string -- the ~50 existing callers stay untouched.
    other = store.upsert_draft("profile-1", "profile-1|delta-epsilon-zeta", "t", "c uno dos tres")
    assert isinstance(other, str)


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


def test_curated_row_upsert_immune_when_signature_kwarg_passed(tmp_path) -> None:
    """Candidate 2 (memoria_rag_followups_20260716): threading the new
    signature kwarg through upsert_draft must not weaken curated immunity."""
    store = MemoriaStore(tmp_path / "memorias.db")
    key = "profile-1|alpha-beta-gamma"
    row_id = store.upsert_draft(
        "profile-1", key, "titulo original", "contenido original aqui", signature="alpha beta gamma"
    )
    store.update_row(row_id, title="titulo curado", content="contenido curado aqui")

    result = store.upsert_draft(
        "profile-1", key, "titulo pisado", "contenido pisado aqui", signature="delta epsilon zeta"
    )
    assert result is None  # curated-immune: no write happened

    row = store.get(row_id)
    assert row["title"] == "titulo curado"
    assert row["content"] == "contenido curado aqui"
    assert row["signature"] == "alpha beta gamma"  # signature immune too
    assert row["status"] == "curated"


def test_upsert_draft_stores_and_refreshes_signature_for_draft_rows(tmp_path) -> None:
    store = MemoriaStore(tmp_path / "memorias.db")
    key = "profile-1|alpha-beta-gamma"
    row_id = store.upsert_draft(
        "profile-1", key, "titulo alpha", "contenido alpha beta gamma", signature="alpha beta gamma"
    )
    assert store.get(row_id)["signature"] == "alpha beta gamma"

    # A draft refresh on the same stable_key updates the signature in lockstep
    # (ON CONFLICT ... DO UPDATE SET signature = excluded.signature).
    store.upsert_draft(
        "profile-1", key, "titulo nuevo", "contenido nuevo aqui", signature="delta epsilon zeta"
    )
    row = store.get(row_id)
    assert row["revision"] == 2
    assert row["signature"] == "delta epsilon zeta"


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
        "kira obs tema perfil usuario recuerda dice streamer chat "
        "acaba decir dijo ptt contexto musica synthwave calma",
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
    # A2 (memoria_quality_20260717) lockstep extension: PTT history-wrapper +
    # legacy ledger-label boilerplate joins the closed domain-stopword set.
    assert "acaba" not in key
    assert "decir" not in key
    assert "dijo" not in key
    assert "ptt" not in key
    assert "contexto" not in key
    assert "musica" in key
    assert "synthwave" in key
    assert "calma" in key


def test_domain_stopwords_extended_acaba_decir_dijo_ptt_contexto() -> None:
    """A2 (memoria_quality_20260717): the honest PTT history_text wrapper
    ("El streamer dijo (PTT): ...") and the legacy ledger label ("contexto:")
    still leak boilerplate tokens into memoria derivation, so
    acaba/decir/dijo/ptt/contexto join the domain-stopword set. RC-1/RC-7
    lockstep: these tokens never reach stable_key/title, and — retroactively —
    can never be the shared token that scores a select_top_k match."""
    from opencohost.core.memoria_store import _significant_tokens, select_top_k

    # 1) The five new tokens are stripped from the significant-token set.
    assert _significant_tokens("acaba decir dijo ptt contexto") == []

    # 2) A PTT-flavored pair keeps only the REAL words in key + title.
    text = "el streamer acaba de decir dijo ptt contexto musica synthwave calma"
    key = derive_stable_key("profile-1", text)
    assert key is not None
    for boiler in ("acaba", "decir", "dijo", "ptt", "contexto"):
        assert boiler not in key
    assert build_title(text) == "musica synthwave calma"

    # 3) Retroactive inertness: a legacy signature polluted with "contexto"/PTT
    # boilerplate (stored BEFORE this extension) can no longer be matched by a
    # query whose ONLY overlap is that boilerplate — the topic side is
    # stopword-filtered, so the shared-token count drops below the >=2 floor.
    legacy_polluted_signature = "contexto dijo ptt musica synthwave calma"
    rows = [{"signature": legacy_polluted_signature, "title": "musica synthwave calma"}]
    assert select_top_k("contexto dijo ptt acaba decir", rows) == []
    # A genuinely on-topic query still matches (>=2 real shared tokens).
    assert len(select_top_k("hablemos de musica synthwave", rows)) == 1


def test_significant_token_count_public_helper() -> None:
    """C1 (memoria_quality_20260717): a public token-count wrapper the engine
    reuses for content shaping (user-side >=2 capture gate, Kira-side first
    >=3-token sentence). Counts DISTINCT significant tokens (domain + generic
    stopwords filtered), mirroring _significant_tokens exactly."""
    from opencohost.core.memoria_store import _significant_tokens, significant_token_count

    assert significant_token_count("kira obs streamer") == 0          # all domain stopwords
    assert significant_token_count("streamer prefiere musica") == 2   # streamer filtered
    assert significant_token_count("") == 0
    assert significant_token_count("hola") == 1
    # Parity with the private helper it wraps, for any text.
    for text in ("musica synthwave calma nocturna", "el streamer dijo ptt contexto"):
        assert significant_token_count(text) == len(_significant_tokens(text))


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
# count_all_pinned (slice 7, F6b) — the honest N half of the management UI's
# "Fijadas: N" counter; must be unfiltered by private/inactive and scoped
# per-profile, unlike list_injection_candidates.
# ---------------------------------------------------------------------------

def test_count_all_pinned_includes_private_and_inactive_rows(tmp_path) -> None:
    store = MemoriaStore(tmp_path / "memorias.db")
    a = store.upsert_draft("profile-1", "profile-1|alpha-beta-gamma", "t1", "c1 c2 c3")
    b = store.upsert_draft("profile-1", "profile-1|delta-epsilon-zeta", "t2", "c4 c5 c6")
    c = store.upsert_draft("profile-1", "profile-1|eta-theta-iota", "t3", "c7 c8 c9")
    store.set_flags(a, pinned=True)
    store.set_flags(b, pinned=True, private=True)
    store.set_flags(c, pinned=True, inactive=True)

    assert store.count_all_pinned("profile-1") == 3


def test_count_all_pinned_excludes_unpinned_rows(tmp_path) -> None:
    store = MemoriaStore(tmp_path / "memorias.db")
    a = store.upsert_draft("profile-1", "profile-1|alpha-beta-gamma", "t1", "c1 c2 c3")
    store.upsert_draft("profile-1", "profile-1|delta-epsilon-zeta", "t2", "c4 c5 c6")
    store.set_flags(a, pinned=True)

    assert store.count_all_pinned("profile-1") == 1


def test_count_all_pinned_scoped_per_profile(tmp_path) -> None:
    store = MemoriaStore(tmp_path / "memorias.db")
    a = store.upsert_draft("profile-1", "profile-1|alpha-beta-gamma", "t1", "c1 c2 c3")
    b = store.upsert_draft("profile-2", "profile-2|delta-epsilon-zeta", "t2", "c4 c5 c6")
    store.set_flags(a, pinned=True)
    store.set_flags(b, pinned=True)

    assert store.count_all_pinned("profile-1") == 1
    assert store.count_all_pinned("profile-2") == 1


def test_count_all_pinned_zero_when_nothing_pinned(tmp_path) -> None:
    store = MemoriaStore(tmp_path / "memorias.db")
    store.upsert_draft("profile-1", "profile-1|alpha-beta-gamma", "t1", "c1 c2 c3")

    assert store.count_all_pinned("profile-1") == 0


# ---------------------------------------------------------------------------
# count_all (SF-1, slice 7 judge round) — the honest, uncapped purge-confirm
# count; unlike list_for_profile, must never cap at MEMORIAS_PROFILE_CAP
# since purge_profile deletes ALL rows regardless of the display cap.
# ---------------------------------------------------------------------------

def test_count_all_returns_true_total_including_rows_beyond_profile_cap(tmp_path) -> None:
    from opencohost.config.settings import MEMORIAS_PROFILE_CAP

    db_path = tmp_path / "memorias.db"
    store = MemoriaStore(db_path)
    _seed_raw_drafts(db_path, "profile-1", MEMORIAS_PROFILE_CAP + 5)

    assert store.count_all("profile-1") == MEMORIAS_PROFILE_CAP + 5
    assert len(store.list_for_profile("profile-1")) == MEMORIAS_PROFILE_CAP


def test_count_all_fails_open_to_negative_sentinel_on_locked_db(tmp_path) -> None:
    """-1 (never a valid count) signals a genuine read failure — distinct
    from a real 0, so a destructive-action caller never shows a
    misleadingly low or zero count on failure."""
    db_path = tmp_path / "memorias.db"
    store = MemoriaStore(db_path)
    store.upsert_draft("profile-1", "profile-1|alpha-beta-gamma", "titulo", "contenido alpha beta")

    blocker = sqlite3.connect(str(db_path))
    blocker.execute("BEGIN EXCLUSIVE")
    try:
        assert store.count_all("profile-1") == -1
    finally:
        blocker.rollback()
        blocker.close()

    assert store.count_all("profile-1") == 1


# ---------------------------------------------------------------------------
# delete_row idempotent semantics (2.22-2.24, A-N2/B-NOTE-1)
# ---------------------------------------------------------------------------

def test_delete_row_removes_existing_row_and_returns_true(tmp_path) -> None:
    store = MemoriaStore(tmp_path / "memorias.db")
    row_id = store.upsert_draft("profile-1", "profile-1|alpha-beta-gamma", "titulo", "contenido alpha beta")

    ok = store.delete_row(row_id)

    assert ok is True
    assert store.get(row_id) is None


def test_delete_row_on_nonexistent_id_is_idempotent_and_returns_true(tmp_path) -> None:
    """A-N2/B-NOTE-1: a row that is already absent satisfies delete_row's
    postcondition (row not in store) — this must return True, not the
    False that would falsely surface MEMORIAS_WRITE_FAILED_TEXT to the
    operator for a row that was never there or already gone."""
    store = MemoriaStore(tmp_path / "memorias.db")

    ok = store.delete_row("mem_does_not_exist")

    assert ok is True
    assert store.get("mem_does_not_exist") is None


def test_delete_row_under_lock_contention_returns_false(tmp_path) -> None:
    """A genuine write error (lock contention) is the ONLY case that must
    surface as False — distinct from the not-found case above."""
    db_path = tmp_path / "memorias.db"
    store = MemoriaStore(db_path)
    row_id = store.upsert_draft("profile-1", "profile-1|alpha-beta-gamma", "titulo", "contenido alpha beta")

    blocker = sqlite3.connect(str(db_path))
    blocker.execute("BEGIN EXCLUSIVE")
    try:
        ok = store.delete_row(row_id)
    finally:
        blocker.rollback()
        blocker.close()

    assert ok is False
    assert store.get(row_id) is not None  # delete did NOT take effect


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


# ---------------------------------------------------------------------------
# W2a summary tier — status='summary' provenance (memoria_recall_20260718)
# ---------------------------------------------------------------------------

def test_insert_summary_writes_status_summary_not_curated(tmp_path) -> None:
    """R1/R2 provenance: machine-authored session summaries carry
    status='summary', distinct from operator-touched status='curated' (F5)."""
    store = MemoriaStore(tmp_path / "memorias.db")
    store.insert_summary("p", "title a b", "content c d durable")

    rows = store.list_for_profile("p")
    assert len(rows) == 1
    assert rows[0]["status"] == "summary"
    assert _SESSION_SUMMARY_MARKER in rows[0]["stable_key"]


def test_summary_row_is_upsert_immune_via_status_guard(tmp_path) -> None:
    """A draft upsert colliding on a summary row's stable_key must be a no-op:
    upsert_draft's ON CONFLICT ... WHERE status='draft' excludes the summary
    row (status='summary'), so its content can never be overwritten. This pins
    the invariant independently of the stable_key's timestamp uniqueness."""
    store = MemoriaStore(tmp_path / "memorias.db")
    store.insert_summary("p", "durable title", "durable summary content")
    summary_key = store.list_for_profile("p")[0]["stable_key"]

    # Upsert a draft on the EXACT summary key — the status guard must reject it.
    result = store.upsert_draft("p", summary_key, "attacker title", "attacker content overwrite")

    assert result is None  # conflict resolved to a no-op, no write
    rows = store.list_for_profile("p")
    assert len(rows) == 1
    assert rows[0]["status"] == "summary"
    assert rows[0]["content"] == "durable summary content"  # untouched


def test_summary_row_immune_to_draft_growth_prune_status(monkeypatch, tmp_path) -> None:
    """_prune_profile targets status='draft' only; a status='summary' row is
    never a prune candidate however far the draft pool overflows the cap."""
    from opencohost.core import memoria_store as store_mod
    monkeypatch.setattr(store_mod, "MEMORIAS_PROFILE_CAP", 3)
    store = MemoriaStore(tmp_path / "memorias.db")

    store.insert_summary("p", "durable summary", "content durable summary tier")
    for i in range(8):  # far past the (patched) draft cap of 3
        store.upsert_draft("p", f"p|draft-{i}", f"title {i} alpha", f"content {i} beta")

    summaries = [
        r for r in store.list_for_profile("p", limit=10_000)
        if r["status"] == "summary"
    ]
    assert len(summaries) == 1


def test_summary_tier_write_failures_always_warn(monkeypatch, tmp_path, caplog) -> None:
    """R4: losing a durable summary is the high-value signal — insert_summary
    and _prune_summaries must log at WARNING even when a prior draft-tier
    failure already tripped the shared warn-once flag (which would otherwise
    demote repeats to debug)."""
    store = MemoriaStore(tmp_path / "memorias.db")

    def boom(timeout):
        raise sqlite3.OperationalError("locked")

    monkeypatch.setattr(store, "_connect", boom)
    caplog.set_level(logging.WARNING)

    store._write_failure_warned = True  # a draft-tier episode already warned
    assert store.insert_summary("p", "title a b", "content c d") is None
    store._write_failure_warned = True
    store._prune_summaries("p")

    warns = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("summary write failed" in r.getMessage() for r in warns)
    assert any("summary-cap prune failed" in r.getMessage() for r in warns)
