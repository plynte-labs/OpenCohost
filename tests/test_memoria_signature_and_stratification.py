"""Tests for memoria_store.py: signature recomputation on update_row and candidate stratification."""

import sqlite3
import pytest
from opencohost.core.memory.memoria_store import MemoriaStore, build_signature


@pytest.fixture
def store(tmp_path):
    db_path = str(tmp_path / "memorias.sqlite")
    return MemoriaStore(db_path)


def test_update_row_recomputes_signature_when_signature_is_none(store):
    # Insert a draft row
    initial_sig = build_signature("Acoustic guitar User plays acoustic guitar")
    row_id = store.upsert_draft("profile-1", "guitar", "Acoustic guitar", "User plays acoustic guitar", signature=initial_sig)
    
    with sqlite3.connect(store.db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT signature, status FROM memorias WHERE id = ?", (row_id,)).fetchone()
        assert row["signature"] == initial_sig

    # Operator edits title and content via update_row without passing signature
    store.update_row(row_id, title="Electric Bass", content="User switched to electric bass guitar")
    expected_new_sig = build_signature("Electric Bass User switched to electric bass guitar")

    with sqlite3.connect(store.db_path) as conn:
        conn.row_factory = sqlite3.Row
        updated = conn.execute("SELECT title, content, signature, status FROM memorias WHERE id = ?", (row_id,)).fetchone()
        assert updated["title"] == "Electric Bass"
        assert updated["content"] == "User switched to electric bass guitar"
        assert updated["status"] == "curated"
        assert updated["signature"] == expected_new_sig
        assert updated["signature"] != initial_sig


def test_update_row_preserves_explicit_signature(store):
    row_id = store.upsert_draft("profile-1", "piano", "Piano practice", "User plays piano")
    
    # Passing an explicit signature must be respected
    store.update_row(row_id, title="Grand Piano", content="Classical piano player", signature="custom piano signature")

    with sqlite3.connect(store.db_path) as conn:
        conn.row_factory = sqlite3.Row
        updated = conn.execute("SELECT signature FROM memorias WHERE id = ?", (row_id,)).fetchone()
        assert updated["signature"] == "custom piano signature"


def test_update_row_recomputes_signature_when_only_one_field_changes(store):
    row_id = store.upsert_draft("profile-1", "cat", "Pixel the Cat", "Likes tuna and naps")
    
    # Only update title
    store.update_row(row_id, title="Pixel the Feline")
    expected_sig = build_signature("Pixel the Feline Likes tuna and naps")

    with sqlite3.connect(store.db_path) as conn:
        conn.row_factory = sqlite3.Row
        updated = conn.execute("SELECT title, content, signature FROM memorias WHERE id = ?", (row_id,)).fetchone()
        assert updated["title"] == "Pixel the Feline"
        assert updated["content"] == "Likes tuna and naps"
        assert updated["signature"] == expected_sig


def test_list_injection_candidates_stratifies_pinned_and_durable_before_drafts(store):
    profile_id = "profile-strat"

    # 1. Old pinned draft (pinned=1, updated_at='2020-01-01')
    r_pinned = store.upsert_draft(profile_id, "k_pinned", "Pinned Row", "Must come first")
    # 2. Older curated row (status='curated', updated_at='2021-01-01')
    r_curated = store.upsert_draft(profile_id, "k_curated", "Curated Row", "Curated fact")
    # 3. Older promoted row (status='promoted', updated_at='2021-02-01')
    r_promoted = store.upsert_draft(profile_id, "k_promoted", "Promoted Row", "Promoted fact")
    # 4. Older imported row (status='imported', updated_at='2021-03-01')
    r_imported = store.upsert_draft(profile_id, "k_imported", "Imported Row", "Imported fact")
    # 5. Very recent unjudged draft (status='draft', updated_at='2026-09-19')
    r_recent_draft = store.upsert_draft(profile_id, "k_recent_draft", "Recent Draft", "Just auto-captured")

    # Manually backdate and configure statuses in database
    with sqlite3.connect(store.db_path) as conn:
        conn.row_factory = sqlite3.Row
        # ADR-062: a real pin curates the row (F5); an unjudged pinned draft
        # cannot exist through the store and would no longer be injectable.
        conn.execute("UPDATE memorias SET pinned = 1, status = 'curated', updated_at = '2020-01-01 00:00:00' WHERE id = ?", (r_pinned,))
        conn.execute("UPDATE memorias SET status = 'curated', updated_at = '2021-01-01 00:00:00' WHERE id = ?", (r_curated,))
        conn.execute("UPDATE memorias SET status = 'promoted', updated_at = '2021-02-01 00:00:00' WHERE id = ?", (r_promoted,))
        conn.execute("UPDATE memorias SET status = 'imported', updated_at = '2021-03-01 00:00:00' WHERE id = ?", (r_imported,))
        # ADR-062: only a JUDGED draft (an uncertain keep) is still injectable;
        # an unjudged one is excluded (asserted below).
        conn.execute("UPDATE memorias SET status = 'draft', judged_at = '2026-09-19', updated_at = '2026-09-19 00:00:00' WHERE id = ?", (r_recent_draft,))
    r_unjudged = store.upsert_draft(profile_id, "k_unjudged", "Unjudged Draft", "Raw exchange")

    candidates = store.list_injection_candidates(profile_id)
    candidate_ids = [c["id"] for c in candidates]

    # Verification:
    # 1. Pinned must be #1 regardless of date
    assert candidate_ids[0] == r_pinned
    # 2. Durable rows (imported, promoted, curated) must come before the recent draft
    # Even though recent_draft is from 2026 and durable rows are from 2021!
    durable_ids = {r_imported, r_promoted, r_curated}
    assert set(candidate_ids[1:4]) == durable_ids
    # 3. Recent draft must come after all durable rows
    assert candidate_ids[4] == r_recent_draft
    # 4. ADR-062: an unjudged draft never becomes a candidate
    assert r_unjudged not in candidate_ids
