"""S3 (release-050-stabilization): pasted-image text never becomes memory.

OCR'd text (e.g. viewer chat inside a screenshot) arrives inside the owner's
turn as a delimited block. It must be stripped before the memoria draft, the
digest ledger and the v5 evidence snapshot. Synthetic data only.
"""
from __future__ import annotations

import queue
import sqlite3
from unittest.mock import MagicMock

import pytest

import opencohost.core.llm_engine as llm_engine
from opencohost.api import chat_image
from opencohost.core.memory.pasted_image import (
    has_pasted_image_block,
    strip_pasted_image_block,
)
from opencohost.core.memory_v5_shadow.subsystem import MemorySubsystem
from tests.test_memory_v5_write_path import FakeWorker, temp_v5  # noqa: F401 (fixture)

OCR = "viewer_zed says the secret launch code is 4417-OCR"
OWNER = "mi gato Pixel odia la aspiradora cuando suena de noche"
pytestmark = pytest.mark.usefixtures("legacy_prompt_layout")


def _turn(text, ocr=OCR):
    return chat_image.compose_turn_text(text, ocr)


# --- pure stripping ---------------------------------------------------------


def test_compose_then_strip_roundtrips_to_the_owner_words():
    assert strip_pasted_image_block(_turn(OWNER)) == OWNER


def test_image_only_turn_strips_to_empty():
    assert strip_pasted_image_block(_turn("")) == ""


def test_text_without_a_block_is_unchanged():
    assert strip_pasted_image_block(OWNER) == OWNER
    assert not has_pasted_image_block(OWNER)


def test_unterminated_block_cut_by_history_clipping_is_still_stripped():
    cut = _turn(OWNER)[: len(OWNER) + 40]
    assert strip_pasted_image_block(cut) == OWNER


def test_spanish_block_is_stripped_too():
    es = f"{OWNER}\n\n[IMAGEN PEGADA - el host adjuntó una imagen]\n{OCR}\n[/IMAGEN PEGADA]"
    assert strip_pasted_image_block(es) == OWNER


# --- memoria draft + digest -------------------------------------------------


def _motor(monkeypatch, tmp_path):
    motor = llm_engine.MotorVocalIA(queue.Queue(), lambda e: None)
    motor.ollama = MagicMock()
    motor.pygame = MagicMock()
    motor.is_ready = True
    monkeypatch.setattr(llm_engine, "MEMORIAS_ENABLED", True)
    monkeypatch.setattr(llm_engine, "MEMORIAS_DB", str(tmp_path / "memorias.db"))
    motor._current_profile_id = "profile-1"
    return motor


def _memoria_rows(tmp_path):
    db = tmp_path / "memorias.db"
    if not db.exists():
        return []
    con = sqlite3.connect(str(db))
    try:
        return con.execute("SELECT title, content FROM memorias").fetchall()
    finally:
        con.close()


def test_draft_excludes_the_ocr_text(monkeypatch, tmp_path):
    motor = _motor(monkeypatch, tmp_path)
    motor._commit_history(_turn(OWNER), "que bien, lo recuerdo", source="direct")
    rows = _memoria_rows(tmp_path)
    assert rows, "the owner's own words should still make a draft"
    blob = " ".join(f"{t} {c}" for t, c in rows)
    assert "4417" not in blob and "viewer_zed" not in blob and "PASTED" not in blob
    assert "Pixel" in blob


def test_image_only_turn_produces_no_draft(monkeypatch, tmp_path):
    motor = _motor(monkeypatch, tmp_path)
    motor._commit_history(_turn(""), "veo una captura de chat", source="direct")
    assert _memoria_rows(tmp_path) == []


def test_digest_ledger_excludes_the_ocr_text(monkeypatch, tmp_path):
    motor = _motor(monkeypatch, tmp_path)
    for i in range(llm_engine.HISTORY_MAX_TURNS + 1):
        motor._commit_history(_turn(OWNER + f" {i}"), f"respuesta {i}", source="direct")
    lines = " ".join(motor._memory_digest.lines)
    assert "4417" not in lines and "viewer_zed" not in lines


# --- v5 evidence journal ----------------------------------------------------


def _journal(shadow):
    con = sqlite3.connect(str(shadow))
    try:
        return con.execute("SELECT role, content FROM evidence_journal ORDER BY stream_sequence").fetchall()
    finally:
        con.close()


def test_v5_snapshot_excludes_the_ocr_text(monkeypatch, tmp_path, temp_v5):
    shadow, _ = temp_v5
    motor = _motor(monkeypatch, tmp_path)
    motor._memory = MemorySubsystem.from_settings(mode="ACTIVE")
    try:
        motor._current_profile_id = "prof-img"
        motor._commit_history(_turn(OWNER), "anotado", source="direct")
        motor._memory.runtime._queue.join()
        rows = _journal(shadow)
        user_rows = [c for r, c in rows if r == "user"]
        assert user_rows == [OWNER]
        assert all("4417" not in c and "viewer_zed" not in c for _, c in rows)
    finally:
        motor._memory.shutdown(timeout_s=2.0)


def test_v5_image_only_turn_writes_nothing(monkeypatch, tmp_path, temp_v5):
    shadow, _ = temp_v5
    motor = _motor(monkeypatch, tmp_path)
    motor._memory = MemorySubsystem.from_settings(mode="ACTIVE")
    try:
        motor._current_profile_id = "prof-img"
        motor._commit_history(_turn(""), "veo algo", source="direct")
        motor._memory.runtime._queue.join()
        assert _journal(shadow) == []
    finally:
        motor._memory.shutdown(timeout_s=2.0)


# --- several images (runtime-fixes-20261110 T7) -------------------------------

OCR_2 = "second_viewer says the backup code is 9921-OCR"
OCR_3 = "third_viewer posted wallet 7733-OCR"


def _three_image_turn(text):
    return chat_image.compose_turn_text_multi(text, [OCR, OCR_2, OCR_3])


def test_three_blocks_strip_to_the_owner_words():
    assert strip_pasted_image_block(_three_image_turn(OWNER)) == OWNER
    assert strip_pasted_image_block(_three_image_turn("")) == ""


def test_three_blocks_cut_by_history_clipping_are_still_stripped():
    cut = _three_image_turn(OWNER)[:300]
    assert strip_pasted_image_block(cut) == OWNER


def test_draft_excludes_the_ocr_text_of_every_image(monkeypatch, tmp_path):
    motor = _motor(monkeypatch, tmp_path)
    motor._commit_history(_three_image_turn(OWNER), "que bien, lo recuerdo", source="direct")
    blob = " ".join(f"{t} {c}" for t, c in _memoria_rows(tmp_path))
    assert "Pixel" in blob
    for secret in ("4417", "9921", "7733", "viewer_zed", "second_viewer", "third_viewer", "PASTED", "IMAGEN"):
        assert secret not in blob


def test_image_only_multi_turn_produces_no_draft(monkeypatch, tmp_path):
    motor = _motor(monkeypatch, tmp_path)
    motor._commit_history(_three_image_turn(""), "veo capturas de chat", source="direct")
    assert _memoria_rows(tmp_path) == []


def test_v5_snapshot_excludes_the_ocr_text_of_every_image(monkeypatch, tmp_path, temp_v5):
    shadow, _ = temp_v5
    motor = _motor(monkeypatch, tmp_path)
    motor._memory = MemorySubsystem.from_settings(mode="ACTIVE")
    try:
        motor._current_profile_id = "prof-img"
        motor._commit_history(_three_image_turn(OWNER), "anotado", source="direct")
        motor._memory.runtime._queue.join()
        rows = _journal(shadow)
        assert [c for r, c in rows if r == "user"] == [OWNER]
        assert all(s not in c for _, c in rows for s in ("4417", "9921", "7733"))
    finally:
        motor._memory.shutdown(timeout_s=2.0)
