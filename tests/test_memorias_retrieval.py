"""Tests for slice 5 (retrieval+injection) — kira_memory_persistence_20260701.

Pins R9 (retrieval, pinning, budget, direct-path-only) per design v2.1 §7 and
owner decision F6 (pinned policy A: max 2 oldest-pinned injected, 220-char
clip at injection time only, automatic top-k always retains a ~260-char
floor of the 700-char budget).

MEMORIAS_ENABLED defaults True as of slice 8. Tests that exercise the
engine wiring MUST monkeypatch `llm_engine.MEMORIAS_ENABLED` (module-level
name lookup — same precedent as tests/test_memorias_capture_engine.py) AND
redirect `llm_engine.MEMORIAS_DB` to a tmp path (see the `_enable_memorias`
helper) — NEVER let the real store be instantiated against USER_DATA_DIR.
Pure store-level tests (retrieval scoring, budget assembly, clipping) do
not need the flag at all — they call opencohost.core.memoria_store
functions directly.
"""

from __future__ import annotations

import logging
import queue
import sqlite3
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

import opencohost.core.llm_engine as llm_engine
from opencohost.config.settings import (
    MEMORIAS_MAX_INJECT_CHARS,
    MEMORIAS_PINNED_CLIP_CHARS,
    MEMORIAS_PROFILE_CAP,
)
from opencohost.core.memoria_store import (
    MemoriaStore,
    build_injection_lines,
    build_title,
    pinned_injection_counter,
    select_top_k,
)


@pytest.fixture(autouse=True)
def _pin_es_locale():
    """Pin the active locale to es (matches tests/test_pipeline_memory.py) so
    the memorias block delimiters render deterministically."""
    from opencohost.i18n import active
    from opencohost.i18n.startup import resolve_active_bundle

    active.set_active_bundle(resolve_active_bundle(locale="es"))
    yield
    active.reset_active_bundle()


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
    motor._warmed_model = motor.current_model
    motor._loaded_model = motor.current_model
    return motor, log_q, ui_events


def _capture_messages(motor, contexto, source):
    """Run _generar_dialogo and capture the messages list passed to _ollama_chat_with_watchdog."""
    captured = []

    def fake_watchdog(**kwargs):
        captured.extend(kwargs.get("messages", []))
        return {"message": {"content": "fake Kira reply"}}

    with patch.object(motor, "_ollama_chat_with_watchdog", side_effect=fake_watchdog):
        motor._generar_dialogo(contexto, source=source, commit_history=False)
    return captured


def _enable_memorias(monkeypatch, motor, tmp_path, profile_id="profile-1"):
    """Flip MEMORIAS_ENABLED on and point the engine's lazy store at tmp_path."""
    monkeypatch.setattr(llm_engine, "MEMORIAS_ENABLED", True)
    monkeypatch.setattr(llm_engine, "MEMORIAS_DB", str(tmp_path / "memorias.db"))
    motor._current_profile_id = profile_id
    return motor


_BASE_TS = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _seed_rows(db_path, rows: list[dict]) -> None:
    """Bulk-insert rows directly (bypassing upsert_draft) for deterministic
    control over content length / flags / timestamps. Ensures the schema
    exists first by constructing a MemoriaStore against db_path."""
    MemoriaStore(db_path)
    with sqlite3.connect(str(db_path)) as conn:
        conn.executemany(
            "INSERT INTO memorias (id, profile_id, stable_key, revision, title, "
            "content, status, pinned, private, inactive, created_at, updated_at) "
            "VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    row["id"], row["profile_id"], f"{row['profile_id']}|{row['id']}",
                    row.get("title", "titulo"), row.get("content", "contenido"),
                    row.get("status", "draft"),
                    1 if row.get("pinned") else 0,
                    1 if row.get("private") else 0,
                    1 if row.get("inactive") else 0,
                    row.get("created_at", _BASE_TS).isoformat(),
                    row.get("updated_at", row.get("created_at", _BASE_TS)).isoformat(),
                )
                for row in rows
            ],
        )


def _seed_row(db_path, profile_id, row_id, **kwargs) -> None:
    _seed_rows(db_path, [{"id": row_id, "profile_id": profile_id, **kwargs}])


# ---------------------------------------------------------------------------
# 5.1-5.2, 5.11 — candidate set eligibility (store-level)
# ---------------------------------------------------------------------------

def test_deactivated_memories_excluded_from_candidate_set(tmp_path):
    db_path = tmp_path / "memorias.db"
    _seed_row(db_path, "profile-1", "row-active", inactive=False)
    _seed_row(db_path, "profile-1", "row-inactive", inactive=True)

    store = MemoriaStore(db_path)
    ids = {row["id"] for row in store.list_injection_candidates("profile-1")}

    assert ids == {"row-active"}


def test_private_memories_excluded_from_candidate_set(tmp_path):
    db_path = tmp_path / "memorias.db"
    _seed_row(db_path, "profile-1", "row-public", private=False)
    _seed_row(db_path, "profile-1", "row-private", private=True)

    store = MemoriaStore(db_path)
    ids = {row["id"] for row in store.list_injection_candidates("profile-1")}

    assert ids == {"row-public"}


def test_pinned_always_included_in_candidate_set_before_cap_applied(tmp_path):
    db_path = tmp_path / "memorias.db"
    # One OLD pinned row that would fall outside a pure-recency cap.
    _seed_row(db_path, "profile-1", "old-pinned", pinned=True, created_at=_BASE_TS)
    # More recent unpinned drafts than the cap, so a naive recency LIMIT
    # would push the old pinned row out if pinned rows weren't sorted first.
    _seed_rows(db_path, [
        {
            "id": f"draft-{i}",
            "profile_id": "profile-1",
            "pinned": False,
            "created_at": _BASE_TS + timedelta(minutes=i + 1),
        }
        for i in range(MEMORIAS_PROFILE_CAP + 5)
    ])

    store = MemoriaStore(db_path)
    ids = {row["id"] for row in store.list_injection_candidates("profile-1")}

    assert "old-pinned" in ids


# ---------------------------------------------------------------------------
# 5.10 — RC-7 false-positive resistance (store-level, select_top_k)
# ---------------------------------------------------------------------------

def test_domain_noise_pairs_never_false_match_calibrated_against_stopword_list(tmp_path):
    db_path = tmp_path / "memorias.db"
    # Title built the real way (RC-7): domain stopwords excluded, so it only
    # ever carries significant tokens.
    title = build_title(
        "kira obs tema perfil usuario recuerda dice streamer chat musica synthwave calma"
    )
    _seed_row(db_path, "profile-1", "row-1", content="detalle musical", title=title)

    store = MemoriaStore(db_path)
    rows = store.list_injection_candidates("profile-1")

    # A query that ONLY shares domain-noise tokens with the row (no real
    # topic overlap) must never match — RC-7 structurally removes this
    # false-positive class because the title never contains those tokens.
    noise_query = "kira obs tema perfil usuario recuerda dice streamer chat juego shooter"
    assert select_top_k(noise_query, rows) == []

    # Sanity: a genuinely on-topic query DOES match (title-only scoring works).
    real_query = "quiero hablar de musica hoy"
    assert len(select_top_k(real_query, rows)) == 1


# ---------------------------------------------------------------------------
# 5.3, 5.6, 5.7, 5.8, 5.9 — budget assembly / pinned policy A (store-level)
# ---------------------------------------------------------------------------

def test_injected_block_never_exceeds_700_char_budget(tmp_path):
    db_path = tmp_path / "memorias.db"
    _seed_rows(db_path, [
        {
            "id": f"row-{i}",
            "profile_id": "profile-1",
            "content": "palabra " * 200,  # ~1600 chars, way over budget
            "title": f"musica synthwave tema{i}",
        }
        for i in range(5)
    ])

    store = MemoriaStore(db_path)
    rows = store.list_injection_candidates("profile-1")
    lines = build_injection_lines(rows, "quiero hablar de musica ahora")

    assert len("\n".join(lines)) <= MEMORIAS_MAX_INJECT_CHARS


def test_ten_long_pinned_rows_only_2_oldest_pinned_injected_topk_retains_260_floor(tmp_path):
    db_path = tmp_path / "memorias.db"
    _seed_rows(db_path, [
        {
            "id": f"pinned-{i}",
            "profile_id": "profile-1",
            "content": f"marker{i:02d} " + ("relleno " * 40),  # ~330 chars, > 220 clip
            "title": f"musica synthwave pin{i}",
            "pinned": True,
            "created_at": _BASE_TS + timedelta(minutes=i),
        }
        for i in range(10)
    ])
    _seed_row(
        db_path, "profile-1", "auto-1",
        content="dato relevante sobre musica synthwave para el top-k automatico",
        title="musica synthwave dato",
        pinned=False,
        created_at=_BASE_TS + timedelta(hours=1),
    )

    store = MemoriaStore(db_path)
    rows = store.list_injection_candidates("profile-1")
    lines = build_injection_lines(rows, "hablemos de musica synthwave")
    block = "\n".join(lines)

    # Only the 2 OLDEST pinned rows are represented.
    assert "marker00" in block
    assert "marker01" in block
    for i in range(2, 10):
        assert f"marker{i:02d}" not in block

    # Automatic top-k still gets its floor even with 10 long pinned rows.
    assert "dato relevante sobre musica synthwave" in block
    assert len(block) <= MEMORIAS_MAX_INJECT_CHARS


def test_pinned_rows_clipped_to_220_chars_at_injection_time(tmp_path):
    db_path = tmp_path / "memorias.db"
    long_content = "palabra " * 60  # 480 chars
    _seed_row(db_path, "profile-1", "pinned-1", content=long_content, title="musica synthwave", pinned=True)

    store = MemoriaStore(db_path)
    rows = store.list_injection_candidates("profile-1")
    lines = build_injection_lines(rows, "hablemos de musica")

    assert lines
    assert len(lines[0]) <= MEMORIAS_PINNED_CLIP_CHARS
    assert lines[0] != long_content


def test_clipping_never_mutates_stored_row_content_or_title(tmp_path):
    db_path = tmp_path / "memorias.db"
    long_content = "palabra " * 60
    _seed_row(db_path, "profile-1", "pinned-1", content=long_content, title="musica synthwave", pinned=True)

    store = MemoriaStore(db_path)
    rows = store.list_injection_candidates("profile-1")
    build_injection_lines(rows, "hablemos de musica")

    refreshed = store.get("pinned-1")
    assert refreshed["content"] == long_content
    assert refreshed["title"] == "musica synthwave"


@pytest.mark.parametrize("total_pinned,expected_injected", [(0, 0), (1, 1), (2, 2), (5, 2), (10, 2)])
def test_window_shows_honest_pin_injection_counter_format(tmp_path, total_pinned, expected_injected):
    db_path = tmp_path / "memorias.db"
    _seed_rows(db_path, [
        {"id": f"pin-{i}", "profile_id": "profile-1", "pinned": True, "title": f"tema{i}"}
        for i in range(total_pinned)
    ] + [
        {"id": f"draft-{i}", "profile_id": "profile-1", "pinned": False, "title": f"otro{i}"}
        for i in range(2)
    ])

    store = MemoriaStore(db_path)
    rows = store.list_injection_candidates("profile-1")
    total, injected = pinned_injection_counter(rows)

    assert total == total_pinned
    assert injected == expected_injected


# ---------------------------------------------------------------------------
# 5.4 — direct-path-only wiring (engine-level)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("source", ["kira-agenda", "chat", "ptt"])
def test_no_injection_outside_direct_path(monkeypatch, tmp_path, source):
    motor, _, _ = _make_motor()
    _enable_memorias(monkeypatch, motor, tmp_path)
    _seed_row(
        tmp_path / "memorias.db", "profile-1", "row-1",
        content="musica synthwave", title="musica synthwave",
    )

    messages = _capture_messages(motor, "hablemos de musica synthwave", source=source)
    all_content = " ".join(m.get("content", "") for m in messages)

    assert "memorias_guardadas" not in all_content


def test_injection_present_on_direct_path_when_enabled(monkeypatch, tmp_path):
    """Positive counterpart to 5.4 — proves the negative assertion isn't
    vacuously true because the block never renders at all."""
    motor, _, _ = _make_motor()
    _enable_memorias(monkeypatch, motor, tmp_path)
    _seed_row(
        tmp_path / "memorias.db", "profile-1", "row-1",
        content="dato guardado sobre musica synthwave", title="musica synthwave",
    )

    messages = _capture_messages(motor, "hablemos de musica synthwave", source="direct")
    all_content = " ".join(m.get("content", "") for m in messages)

    assert "memorias_guardadas" in all_content
    assert "dato guardado sobre musica synthwave" in all_content


# ---------------------------------------------------------------------------
# 5.5 — combined injection stays within the existing ctx-budget ceiling
# ---------------------------------------------------------------------------

def test_combined_injection_stays_within_existing_ctx_budget_ceiling(monkeypatch, tmp_path):
    monkeypatch.setattr(llm_engine, "output_guard", lambda dialogo, source="chat": (True, ""))
    motor, _, _ = _make_motor()
    _enable_memorias(monkeypatch, motor, tmp_path)

    # Digest near its own ~600-char cap.
    for i in range(20):
        motor._memory_digest.append(f"contexto: pregunta {i:02d} → Kira: respuesta {i:02d} de relleno")

    # Memorias rows near the 700-char injection budget.
    _seed_rows(tmp_path / "memorias.db", [
        {
            "id": f"row-{i}",
            "profile_id": "profile-1",
            "content": "palabra " * 40,
            "title": f"musica synthwave extra{i}",
        }
        for i in range(5)
    ])

    # Editorial block.
    motor.direct_editorial_context_provider = lambda contexto: "editorial: dato relevante de la carta."

    # Force ctx-budget eviction with a small model ctx + a big historial —
    # same pattern as test_context_overflow_guardrail.py's end-to-end test.
    motor._model_ctx_limit[motor.current_model] = 4096
    big = "x" * 4000
    for _ in range(4):
        motor.historial.append({"role": "user", "content": big, "source": "direct"})
        motor.historial.append({"role": "assistant", "content": big, "source": "direct"})

    messages = _capture_messages(motor, "quiero hablar de musica ahora", source="direct")

    # No separate/new budget mechanism: the SAME apply_char_budget eviction
    # still runs against the combined (memorias + digest + editorial +
    # contexto) final turn — oversized history pairs get evicted, and the
    # always-protected final turn (which must carry all three blocks)
    # survives regardless.
    assert len(messages) < 9  # eviction ran (started at 4 pairs + 1 final = 9)
    final_content = messages[-1]["content"]
    assert "memorias_guardadas" in final_content
    assert "[hace" in final_content
    assert "editorial: dato relevante" in final_content
    # Sanity ceiling: combined stacking (memorias ~700 + digest ~600 +
    # editorial + contexto) does not blow up unbounded.
    assert len(final_content) < 3000


# ---------------------------------------------------------------------------
# Judge-round slice-5 hardening (B-SF1, B-SF2) — test-only additions
# ---------------------------------------------------------------------------

def test_private_and_inactive_content_never_reaches_injected_block_end_to_end(monkeypatch, tmp_path):
    """B-SF1 (judge-round slice 5) — locks the TRANSITIVE property.

    Tests 5.1/5.2 assert private/inactive exclusion at the CANDIDATE-LIST
    level only. build_injection_lines intentionally does NOT re-filter
    private/inactive (see its docstring) — MemoriaStore.list_injection_candidates'
    SQL WHERE clause is the ONLY guard. This drives the real engine path
    (_build_memorias_injection_block on a MotorVocalIA with a real
    MemoriaStore) and asserts the actual injected TEXT never carries
    private/inactive content, while eligible content is present. Private and
    inactive rows are pinned so they'd be unconditionally included by
    build_injection_lines if the SQL filter ever failed (pinned rows bypass
    topic matching), making the guard-only-at-SQL-level property observable.
    """
    motor, _, _ = _make_motor()
    _enable_memorias(monkeypatch, motor, tmp_path, profile_id="profile-1")
    db_path = tmp_path / "memorias.db"

    _seed_rows(db_path, [
        {
            "id": "row-private", "profile_id": "profile-1",
            "content": "PRIVATE_CONTENT_SENTINEL_AAA111",
            "title": "musica synthwave privado",
            "pinned": True, "private": True,
            "created_at": _BASE_TS,
        },
        {
            "id": "row-inactive", "profile_id": "profile-1",
            "content": "INACTIVE_CONTENT_SENTINEL_BBB222",
            "title": "musica synthwave inactivo",
            "pinned": True, "inactive": True,
            "created_at": _BASE_TS + timedelta(minutes=1),
        },
        {
            "id": "row-eligible", "profile_id": "profile-1",
            "content": "ELIGIBLE_CONTENT_SENTINEL_CCC333",
            "title": "musica synthwave dato",
            "pinned": False,
            "created_at": _BASE_TS + timedelta(minutes=2),
        },
    ])

    block = motor._build_memorias_injection_block("profile-1", "hablemos de musica synthwave")

    assert "PRIVATE_CONTENT_SENTINEL_AAA111" not in block
    assert "INACTIVE_CONTENT_SENTINEL_BBB222" not in block
    assert "ELIGIBLE_CONTENT_SENTINEL_CCC333" in block


def test_retrieval_fail_open_secret_sentinel_absent_from_logs(monkeypatch, tmp_path, caplog):
    """B-SF2 (judge-round slice 5) — RC-8 sentinel parity.

    Slices 2/4 have obligatory SECRET_SHOULD_NOT_LOG sentinel tests for their
    fail-open log paths; slice 5's retrieval fail-open log
    (_build_memorias_injection_block's `except Exception`) had none. Locks
    that the fail-open log carries only type(exc).__name__ + profile_id,
    never a leaked exception-message content, mirroring
    test_switch_flush_secret_sentinel_absent_from_logs_on_failure_path in
    tests/test_memorias_flush_switch.py.
    """
    motor, _, _ = _make_motor()
    _enable_memorias(monkeypatch, motor, tmp_path, profile_id="profile-1")

    sentinel = "SECRET_SHOULD_NOT_LOG"

    def failing_list(self, *args, **kwargs):
        raise RuntimeError(f"retrieval exploded on {sentinel}")

    monkeypatch.setattr(MemoriaStore, "list_injection_candidates", failing_list)

    caplog.set_level(logging.WARNING)
    block = motor._build_memorias_injection_block("profile-1", "hablemos de musica synthwave")

    assert block == ""
    assert sentinel not in caplog.text
