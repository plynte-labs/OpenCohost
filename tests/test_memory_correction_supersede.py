"""T6(b) memory-semantic-recall: a correction replaces the wrong draft.

Before this, every turn produced its own draft, so "my birthday is May 3"
followed by "I got it wrong, it is May 5" left TWO drafts: the wrong one stayed
injectable and promotable. Now an explicit correction turn supersedes the draft
captured from the IMMEDIATELY preceding pair of the same live window: same row,
revision bump, content replaced, re-judged at the next sweep (ADR-061).

Scope is deliberately narrow: only an unjudged-or-judge-hidden DRAFT is ever
rewritten. A promoted/curated/imported/summary row is never touched here; the
correction then falls back to an ordinary new draft.
"""

import logging
import queue
import sqlite3
from unittest.mock import MagicMock

import pytest

import opencohost.core.llm_engine as llm_engine
from opencohost.core.memory.memoria_store import (
    MemoriaStore,
    derive_stable_key,
    is_correction_turn,
)

PROFILE = "profile-corr"
FIRST_USER = "mi cumpleaños es el tres de mayo anotalo para la agenda"
FIRST_ASST = "Anotado, tu cumpleaños es el tres de mayo. Lo guardo."
FIX_USER = "me equivoqué, mi cumpleaños es el cinco de mayo no el tres"
FIX_ASST = "Corregido, tu cumpleaños es el cinco de mayo. Listo."
PLAIN_USER = "hoy voy a jugar hollow knight toda la tarde en stream"
PLAIN_ASST = "Hollow Knight es una gran eleccion para la tarde. Suerte."


# ---------------------------------------------------------------------------
# Detection (pure)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "me equivoqué, mi cumpleaños es el cinco de mayo",
    "Me confundí: el juego era Hollow Knight",
    "perdón, quise decir el cinco de mayo",
    "corrijo: el sponsor es otra marca",
    "corrección: son tres gatos, no dos",
    "no, en realidad es el cinco de mayo",
    "perdón, es el cinco de mayo",
    "El streamer dijo (PTT): me equivoqué, es el cinco",
    "I meant Hollow Knight, not Silksong",
    "correction: my birthday is May 5",
    "sorry, I was wrong, it is May 5",
    "I misspoke, the sponsor is another brand",
    "actually, it's May 5",
])
def test_correction_cues_are_detected(text):
    assert is_correction_turn(text) is True


@pytest.mark.parametrize("text", [
    "",
    "mi cumpleaños es el tres de mayo",
    "no me gusta el café",
    "no, gracias",
    "en realidad virtual jugué Beat Saber",
    "en realidad es bastante bueno el juego",
    "el corrector ortográfico está roto",
    "quiero decirte algo",
    "perdón por la demora",
    "actually I love this game",
    "that was a correct answer",
])
def test_ordinary_turns_are_not_corrections(text):
    assert is_correction_turn(text) is False


# ---------------------------------------------------------------------------
# Store: supersede_draft
# ---------------------------------------------------------------------------

def _row(db, row_id):
    with sqlite3.connect(str(db)) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute("SELECT * FROM memorias WHERE id = ?", (row_id,)).fetchone()


def _seed_draft(tmp_path):
    db = tmp_path / "memorias.db"
    store = MemoriaStore(db)
    key = derive_stable_key(PROFILE, f"{FIRST_USER} {FIRST_ASST}")
    row_id = store.upsert_draft(PROFILE, key, "cumpleaños tres mayo", "contexto: wrong", signature="s")
    return db, store, key, row_id


def test_supersede_rewrites_the_draft_in_place_and_bumps_revision(tmp_path):
    db, store, key, row_id = _seed_draft(tmp_path)

    result = store.supersede_draft(PROFILE, key, "cumpleaños cinco mayo", "contexto: right", signature="s2")

    assert result == (row_id, 2)
    row = _row(db, row_id)
    assert row["revision"] == 2
    assert row["content"] == "contexto: right"
    assert row["title"] == "cumpleaños cinco mayo"
    assert row["signature"] == "s2"
    assert row["stable_key"] == key
    assert row["status"] == "draft"
    with sqlite3.connect(str(db)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM memorias").fetchone()[0] == 1


def test_supersede_rejudges_a_judge_hidden_draft(tmp_path):
    db, store, key, row_id = _seed_draft(tmp_path)
    with sqlite3.connect(str(db)) as conn:
        conn.execute("UPDATE memorias SET judged_at = 'x', inactive = 1 WHERE id = ?", (row_id,))

    assert store.supersede_draft(PROFILE, key, "t", "contexto: right") == (row_id, 2)

    row = _row(db, row_id)
    assert row["judged_at"] == ""
    assert row["inactive"] == 0


def test_supersede_keeps_an_operator_muted_draft_hidden(tmp_path):
    db, store, key, row_id = _seed_draft(tmp_path)
    with sqlite3.connect(str(db)) as conn:
        conn.execute("UPDATE memorias SET inactive = 1 WHERE id = ?", (row_id,))

    assert store.supersede_draft(PROFILE, key, "t", "contexto: right") == (row_id, 2)

    assert _row(db, row_id)["inactive"] == 1


@pytest.mark.parametrize("status", ["promoted", "curated", "imported", "summary"])
def test_supersede_never_touches_a_durable_row(tmp_path, status):
    db, store, key, row_id = _seed_draft(tmp_path)
    with sqlite3.connect(str(db)) as conn:
        conn.execute("UPDATE memorias SET status = ? WHERE id = ?", (status, row_id))

    assert store.supersede_draft(PROFILE, key, "t", "contexto: right") is None

    row = _row(db, row_id)
    assert row["content"] == "contexto: wrong"
    assert row["revision"] == 1


def test_supersede_ignores_private_rows_and_other_profiles(tmp_path):
    db, store, key, row_id = _seed_draft(tmp_path)
    assert store.supersede_draft("other-profile", key, "t", "contexto: right") is None
    with sqlite3.connect(str(db)) as conn:
        conn.execute("UPDATE memorias SET private = 1 WHERE id = ?", (row_id,))
    assert store.supersede_draft(PROFILE, key, "t", "contexto: right") is None
    assert _row(db, row_id)["content"] == "contexto: wrong"


def test_supersede_clears_the_stale_retry_verdict(tmp_path):
    db, store, key, row_id = _seed_draft(tmp_path)
    with sqlite3.connect(str(db)) as conn:
        conn.execute(
            "INSERT INTO memoria_promotion_attempts (memoria_id, draft_revision, attempt_count, "
            "last_attempt_at_s, next_attempt_at_s, last_failure_code, promotion_state) "
            "VALUES (?, 1, 1, 0, 300, 'timeout', 'pending')",
            (row_id,),
        )

    store.supersede_draft(PROFILE, key, "t", "contexto: right")

    with sqlite3.connect(str(db)) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM memoria_promotion_attempts WHERE memoria_id = ?", (row_id,)
        ).fetchone()[0] == 0


# ---------------------------------------------------------------------------
# Engine: _commit_history routes a correction to supersede
# ---------------------------------------------------------------------------

def _motor(monkeypatch, tmp_path):
    monkeypatch.setattr(llm_engine, "MEMORIAS_ENABLED", True)
    monkeypatch.setattr(llm_engine, "MEMORIAS_DB", str(tmp_path / "memorias.db"))
    motor = llm_engine.MotorVocalIA(queue.Queue(), lambda e: None)
    motor.ollama = MagicMock()
    motor.pygame = MagicMock()
    motor._current_profile_id = PROFILE
    return motor


def _rows(tmp_path):
    return MemoriaStore(tmp_path / "memorias.db").list_for_profile(PROFILE)


def test_correction_turn_replaces_the_previous_draft(monkeypatch, tmp_path):
    motor = _motor(monkeypatch, tmp_path)
    motor._commit_history(FIRST_USER, FIRST_ASST, source="direct")
    [first] = _rows(tmp_path)

    motor._commit_history(FIX_USER, FIX_ASST, source="direct")

    rows = _rows(tmp_path)
    assert len(rows) == 1
    [row] = rows
    assert row["id"] == first["id"]
    assert row["revision"] == first["revision"] + 1
    assert "cinco" in row["content"]
    assert "tres de mayo anotalo" not in row["content"]
    assert row["content"] == motor._build_memoria_content(FIX_USER, "")  # owner-only-drafts
    assert motor.historial[-2].get("mem_captured") is True


def test_non_correction_turn_still_adds_its_own_draft(monkeypatch, tmp_path):
    motor = _motor(monkeypatch, tmp_path)
    motor._commit_history(FIRST_USER, FIRST_ASST, source="direct")
    motor._commit_history(PLAIN_USER, PLAIN_ASST, source="direct")

    assert len(_rows(tmp_path)) == 2


def test_correction_without_a_previous_captured_pair_is_an_ordinary_draft(monkeypatch, tmp_path):
    motor = _motor(monkeypatch, tmp_path)
    motor._commit_history(FIX_USER, FIX_ASST, source="direct")

    rows = _rows(tmp_path)
    assert len(rows) == 1 and rows[0]["revision"] == 1


def test_correction_after_a_private_pair_does_not_supersede(monkeypatch, tmp_path):
    motor = _motor(monkeypatch, tmp_path)
    motor.set_memorias_private(True)
    motor._commit_history(FIRST_USER, FIRST_ASST, source="direct")
    motor.set_memorias_private(False)

    motor._commit_history(FIX_USER, FIX_ASST, source="direct")

    rows = _rows(tmp_path)
    assert len(rows) == 1 and rows[0]["revision"] == 1


def test_correction_falls_back_to_new_draft_when_target_was_promoted(monkeypatch, tmp_path):
    motor = _motor(monkeypatch, tmp_path)
    motor._commit_history(FIRST_USER, FIRST_ASST, source="direct")
    [first] = _rows(tmp_path)
    with sqlite3.connect(str(tmp_path / "memorias.db")) as conn:
        conn.execute("UPDATE memorias SET status = 'promoted' WHERE id = ?", (first["id"],))

    motor._commit_history(FIX_USER, FIX_ASST, source="direct")

    rows = {r["id"]: r for r in _rows(tmp_path)}
    assert len(rows) == 2
    assert rows[first["id"]]["content"] == first["content"]
    assert rows[first["id"]]["revision"] == first["revision"]


def test_correction_replaces_the_session_summary_title(monkeypatch, tmp_path):
    motor = _motor(monkeypatch, tmp_path)
    motor._commit_history(FIRST_USER, FIRST_ASST, source="direct")
    wrong_title = list(motor._session_memoria_titles)

    motor._commit_history(FIX_USER, FIX_ASST, source="direct")

    assert len(wrong_title) == 1
    assert motor._session_memoria_titles == [_rows(tmp_path)[0]["title"]]


def test_supersede_log_carries_ids_only(monkeypatch, tmp_path, caplog):
    motor = _motor(monkeypatch, tmp_path)
    motor._commit_history(FIRST_USER, FIRST_ASST, source="direct")
    [first] = _rows(tmp_path)

    with caplog.at_level(logging.INFO, logger="OpenCohost"):
        motor._commit_history(FIX_USER, FIX_ASST, source="direct")

    lines = [r.getMessage() for r in caplog.records if "memoria correction" in r.getMessage()]
    assert lines == [f"memoria correction superseded id={first['id']} revision={first['revision'] + 1}"]
    text = "\n".join(r.getMessage() for r in caplog.records)
    for needle in ("cumpleaños", "cinco", "mayo", first["stable_key"]):
        assert needle not in text


# ---------------------------------------------------------------------------
# Privacy: capture-failure log must not carry the stable_key (content tokens)
# ---------------------------------------------------------------------------

def test_capture_failure_log_has_no_stable_key(monkeypatch, tmp_path, caplog):
    motor = _motor(monkeypatch, tmp_path)
    broken = MagicMock()
    broken.upsert_draft.side_effect = RuntimeError("locked")
    motor._memoria_store = broken
    key = derive_stable_key(PROFILE, f"{FIRST_USER} {FIRST_ASST}")

    with caplog.at_level(logging.WARNING, logger="OpenCohost"):
        assert motor._capture_memoria(PROFILE, key, "t", "c") is False

    messages = [r.getMessage() for r in caplog.records if "memoria capture failed" in r.getMessage()]
    assert len(messages) == 1
    assert key not in messages[0]
    assert "RuntimeError" in messages[0]
