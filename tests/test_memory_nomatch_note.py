"""T4 (memory-grounding-and-recall-budget-20261016): explicit no-match note.

On an explicit TOPICAL recall question ("do you remember the Tesla?") where no
eligible fact and no v5 episode matches, the volatile memory block carries one
code-built "no stored memory about <anchor>" line plus a one-line grounding
rule, so Kira does not invent continuity. Matching memory keeps the normal
block and callbacks. General recall questions ("what do you remember about
me?") and ordinary turns never get the note (owner decision M5).
"""
from __future__ import annotations

import logging

import pytest

from opencohost.config import settings
from opencohost.core.memory import memoria_store as ms
from opencohost.core.memory.memoria_store import _significant_tokens
from opencohost.core.memory_v5_shadow.subsystem import MemorySubsystem
from opencohost.i18n import active as i18n_active
from tests.test_memory_recall_merge import (
    FACT_LEXICAL,
    FACT_PINNED,
    FakeCoordinator,
    QUERY,
    _block_lines,
    _episode,
    _make_motor,
    _prompt,
    _recall_records,
)

TOPICAL = "recuerdas zqtesla zqmodelo del auto rojo"
GENERAL = "¿qué recuerdas de mí?"
UNRELATED = {"id": "u1", "content": "zqotro el streamer juega ajedrez los martes"}


@pytest.fixture(autouse=True)
def _pin_es_locale():
    from opencohost.i18n.startup import resolve_active_bundle

    i18n_active.set_active_bundle(resolve_active_bundle(locale="es"))
    yield
    i18n_active.reset_active_bundle()



# ---- predicates ------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "recuerdas el tesla de la otra vez",
    "te acuerdas del tesla model 3",
    "¿te acuerdas del Tesla que hablamos?",
    "el tesla del que hablamos la sesión pasada",
    "do you remember the tesla we talked about",
    "what did we say about the tesla last time",
])
def test_topical_recall_questions_are_detected(text):
    assert ms.is_recall_question(text)
    assert "tesla" in ms.recall_topic_anchors(text)
    assert ms.is_topical_recall_question(text)


@pytest.mark.parametrize("text", [
    "recuerda que mañana tengo médico",
    "mi tesla es rojo",
    "hola que tal",
    "remember that my tesla is red",
])
def test_non_questions_are_never_recall_questions(text):
    assert not ms.is_topical_recall_question(text)


@pytest.mark.parametrize("text", [
    "¿qué recuerdas de mí?",
    "de qué hablamos hoy",
    "what do you remember about me",
])
def test_general_recall_questions_have_no_topical_anchor(text):
    assert ms.is_recall_question(text)
    assert ms.recall_topic_anchors(text) == ()
    assert not ms.is_topical_recall_question(text)


# ---- engine ----------------------------------------------------------------

def test_topical_miss_emits_no_memory_note(monkeypatch, tmp_path):
    motor = _make_motor(monkeypatch, tmp_path, [UNRELATED])

    lines = _block_lines(_prompt(motor, TOPICAL))

    assert len(lines) == 2
    assert "zqtesla" in lines[0]
    assert UNRELATED["content"] not in lines
    assert i18n_active.memorias_no_match_rule() in lines


def test_topical_miss_with_no_rows_still_emits_the_note(monkeypatch, tmp_path):
    motor = _make_motor(monkeypatch, tmp_path, [])
    lines = _block_lines(_prompt(motor, TOPICAL))
    assert len(lines) == 2 and "zqtesla" in lines[0]


def test_no_match_note_is_english_in_english_locale(monkeypatch, tmp_path):
    from opencohost.i18n.startup import resolve_active_bundle

    i18n_active.set_active_bundle(resolve_active_bundle(locale="en"))
    motor = _make_motor(monkeypatch, tmp_path, [])
    lines = _block_lines(_prompt(motor, "do you remember zqtesla"))
    assert lines and "zqtesla" in lines[0] and lines[0].lower().startswith("no stored memory")


def test_callback_kept_when_memory_matches(monkeypatch, tmp_path):
    rows = [{"id": "lex", "content": FACT_LEXICAL, "signature": " ".join(_significant_tokens(QUERY))}]
    motor = _make_motor(monkeypatch, tmp_path, rows)

    lines = _block_lines(_prompt(motor, "recuerdas " + QUERY))

    assert lines == [FACT_LEXICAL]


def test_callback_kept_when_an_episode_matches(monkeypatch, tmp_path):
    motor = _make_motor(monkeypatch, tmp_path, [])
    motor._memory = MemorySubsystem(mode="ACTIVE", coordinator=FakeCoordinator([_episode("ep1")]))

    lines = _block_lines(_prompt(motor, TOPICAL))

    assert len(lines) == 1 and "zqepuser" in lines[0]


def test_ordinary_unmatched_turn_gets_no_note(monkeypatch, tmp_path):
    motor = _make_motor(monkeypatch, tmp_path, [UNRELATED])
    assert _block_lines(_prompt(motor, "zqtesla es un auto genial")) == []


def test_general_recall_question_gets_no_note(monkeypatch, tmp_path):
    motor = _make_motor(monkeypatch, tmp_path, [])
    assert _block_lines(_prompt(motor, GENERAL)) == []


def test_layers_off_means_no_note(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "MEMORY_FACTS_ENABLED", False)
    motor = _make_motor(monkeypatch, tmp_path, [UNRELATED])
    assert _block_lines(_prompt(motor, TOPICAL)) == []


def test_telemetry_marks_the_note_without_content(monkeypatch, tmp_path, caplog):
    motor = _make_motor(monkeypatch, tmp_path, [UNRELATED])
    with caplog.at_level(logging.INFO, logger="OpenCohost"):
        _prompt(motor, TOPICAL)
    (rec,) = _recall_records(caplog)
    msg = rec.getMessage()
    assert msg.endswith(" note=no_match") and "zqtesla" not in msg


def test_telemetry_has_no_note_field_otherwise(monkeypatch, tmp_path, caplog):
    motor = _make_motor(monkeypatch, tmp_path, [UNRELATED])
    with caplog.at_level(logging.INFO, logger="OpenCohost"):
        _prompt(motor, "zqtesla es un auto genial")
    (rec,) = _recall_records(caplog)
    assert "note=" not in rec.getMessage()


# ---- judge round 1: when the note must NOT fire, and its wording -----------

def _insert_draft(db, row_id, content, signature):
    import sqlite3
    from datetime import datetime, timezone
    ts = datetime(2026, 1, 1, tzinfo=timezone.utc).isoformat()
    with sqlite3.connect(str(db)) as conn:
        conn.execute(
            "INSERT INTO memorias (id, profile_id, stable_key, revision, title, "
            "content, status, pinned, private, inactive, created_at, updated_at, "
            "signature, judged_at) VALUES (?, 'profile-1', ?, 1, 't', ?, 'draft', 0, 0, 0, "
            "?, ?, ?, '')",
            (row_id, "profile-1|" + row_id, content, ts, ts, signature),
        )


def test_note_skipped_when_a_draft_mentions_the_anchor(monkeypatch, tmp_path):
    motor = _make_motor(monkeypatch, tmp_path, [UNRELATED])
    _insert_draft(tmp_path / "memorias.db", "d1",
                  "hablamos del zqtesla rojo con kira", "zqtesla rojo hablamos")
    assert _block_lines(_prompt(motor, "te acuerdas del zqtesla")) == []


def test_note_skipped_when_the_content_tokens_mention_the_anchor(monkeypatch, tmp_path):
    motor = _make_motor(monkeypatch, tmp_path, [UNRELATED])
    _insert_draft(tmp_path / "memorias.db", "d2",
                  "el owner compro un zqtesla nuevo", "")
    assert _block_lines(_prompt(motor, "te acuerdas del zqtesla")) == []


def test_note_skipped_when_session_topics_are_present(monkeypatch, tmp_path):
    motor = _make_motor(monkeypatch, tmp_path, [UNRELATED])
    monkeypatch.setattr(
        motor, "_session_topics_block",
        lambda *a, **k: "<session_topics>zqtesla antes en la sesion</session_topics>",
    )
    assert _block_lines(_prompt(motor, TOPICAL)) == []


def test_builder_flag_for_session_topics_withholds_the_note(monkeypatch, tmp_path):
    motor = _make_motor(monkeypatch, tmp_path, [UNRELATED])
    builder, stats = motor._memorias_builder_with_recall(None)
    stats["session_topics"] = True
    assert builder("profile-1", TOPICAL) == ""
    assert "note" not in stats


def test_note_skipped_when_the_strict_path_withheld_a_match(monkeypatch, tmp_path):
    rows = [{"id": "lex", "content": FACT_LEXICAL,
             "signature": " ".join(_significant_tokens(QUERY))}]
    motor = _make_motor(monkeypatch, tmp_path, rows)
    # The anchor lookup is neutralised so only the weak-drop rule can suppress.
    monkeypatch.setattr(type(motor), "_anchor_in_memory", lambda self, p, a: False)
    block = motor._build_memorias_injection_block(
        "profile-1", "recuerdas " + QUERY, lexical_min_shared=99)
    assert i18n_active.memorias_no_match_rule() not in block


def test_pinned_rows_do_not_hide_the_note(monkeypatch, tmp_path):
    motor = _make_motor(monkeypatch, tmp_path, [{"id": "pin", "content": FACT_PINNED, "pinned": True}])
    lines = _block_lines(_prompt(motor, TOPICAL))
    assert lines[0] == FACT_PINNED
    assert "zqtesla" in lines[1]
    assert lines[2] == i18n_active.memorias_no_match_rule()
    assert len(lines) == 3


@pytest.mark.parametrize("locale,start", [
    ("es", "Ninguna memoria guardada coincide con"),
    ("en", "No stored memory matches"),
])
def test_note_wording_is_scoped_to_stored_memories(locale, start):
    from opencohost.i18n.startup import resolve_active_bundle

    i18n_active.set_active_bundle(resolve_active_bundle(locale=locale))
    line, rule = i18n_active.memorias_no_match_line(), i18n_active.memorias_no_match_rule()
    assert line.startswith(start) and "{topic}" in line
    for banned in ("Solo conoces", "You only know", "this block", "este bloque"):
        assert banned not in rule
    assert ("conversaci" in rule) or ("conversation" in rule)
