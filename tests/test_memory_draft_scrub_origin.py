"""T6(a) (ADR-062): drafts stop carrying the user's words past judgment.

Four contracts:
1. A judge-REJECTED draft is scrubbed: title, content and signature blanked,
   stable_key replaced by a content-free hash. Row identity, status, revision,
   judged_at and inactive survive, so the row is never re-judged by itself.
   A retryable failure (unresolved decision) never scrubs.
2. Dedup still works after the scrub: re-capturing the same exchange reopens
   the SAME row (same id, revision + 1) exactly as before the scrub.
3. Unjudged drafts never enter the prompt injection candidates (lexical,
   hybrid and meta-recall all read list_injection_candidates). Rows with blank
   content never do either.
4. The judge emits ``origin`` in {self_stated, shared_content}, persisted in the
   new schema-v5 ``origin`` column. A missing or invalid origin on a keep
   defaults to shared_content (counted as ``origin_defaulted``). A
   shared_content row is rendered with a fixed tag at injection time, so it is
   never presented as a fact about the operator.
"""

from __future__ import annotations

import json
import queue
import sqlite3
from unittest.mock import MagicMock

import pytest

from opencohost.core.memory.memoria_store import (
    MemoriaStore,
    build_injection_lines,
    build_recency_lines,
    derive_stable_key,
)

_PAIR = "contexto: me encanta jugar Hollow Knight los domingos → Kira: buena eleccion"


def _row(db_path, row_id):
    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute("SELECT * FROM memorias WHERE id = ?", (row_id,)).fetchone()


def _seed_draft(store, profile_id="p", text=_PAIR):
    key = derive_stable_key(profile_id, text)
    assert key is not None
    row_id = store.upsert_draft(profile_id, key, "hollow knight domingos", text,
                                signature="hollow knight domingos jugar")
    assert isinstance(row_id, str)
    return row_id, key


def _batch(store, profile_id="p"):
    return store.list_unjudged_drafts(profile_id, limit=8, now_s=100, raising=True)


def _reject(store, profile_id="p"):
    return store.apply_promotion_batch(
        profile_id, _batch(store, profile_id),
        decisions=[(1, None, False, "trivial")], unresolved={}, now_s=100,
    )


# ---------------------------------------------------------------------------
# 1. Scrub on terminal rejection
# ---------------------------------------------------------------------------

def test_rejected_draft_is_scrubbed_but_keeps_identity_and_verdict(tmp_path):
    db = tmp_path / "memorias.db"
    store = MemoriaStore(db)
    row_id, key = _seed_draft(store)
    before = _row(db, row_id)

    result = _reject(store)

    after = _row(db, row_id)
    assert result.rejected == 1
    assert after["title"] == ""
    assert after["content"] == ""
    assert after["signature"] == ""
    assert after["stable_key"] != key
    # No significant token of the exchange survives in the key.
    for token in ("hollow", "knight", "domingos", "jugar"):
        assert token not in after["stable_key"]
    assert after["stable_key"].startswith("p|scrubbed:")
    # Identity + verdict metadata preserved.
    assert after["id"] == row_id
    assert after["profile_id"] == "p"
    assert after["status"] == "draft"
    assert after["revision"] == before["revision"]
    assert after["created_at"] == before["created_at"]
    assert after["judged_at"] != ""
    assert after["inactive"] == 1


def test_scrubbed_row_is_not_rejudged(tmp_path):
    store = MemoriaStore(tmp_path / "memorias.db")
    _seed_draft(store)
    _reject(store)
    assert _batch(store) == []


def test_retryable_failure_never_scrubs(tmp_path):
    db = tmp_path / "memorias.db"
    store = MemoriaStore(db)
    row_id, key = _seed_draft(store)

    store.apply_promotion_batch(
        "p", _batch(store), decisions=[], unresolved={1: "missing_decision"}, now_s=100,
    )

    row = _row(db, row_id)
    assert row["content"] == _PAIR
    assert row["stable_key"] == key
    assert row["title"] != ""


def test_keep_path_is_not_scrubbed(tmp_path):
    db = tmp_path / "memorias.db"
    store = MemoriaStore(db)
    row_id, key = _seed_draft(store)
    judged = "El operador juega Hollow Knight los domingos."

    store.apply_promotion_batch(
        "p", _batch(store), decisions=[(1, judged, False, "")], unresolved={}, now_s=100,
    )

    row = _row(db, row_id)
    assert row["content"] == judged
    assert row["stable_key"] == key
    assert row["status"] == "promoted"


# ---------------------------------------------------------------------------
# 2. Dedup survives the scrub
# ---------------------------------------------------------------------------

def test_recapture_of_same_exchange_reopens_the_scrubbed_row(tmp_path):
    db = tmp_path / "memorias.db"
    store = MemoriaStore(db)
    row_id, key = _seed_draft(store)
    _reject(store)

    again = store.upsert_draft("p", key, "hollow knight domingos", _PAIR)

    assert again == row_id
    row = _row(db, row_id)
    assert row["stable_key"] == key
    assert row["content"] == _PAIR
    assert row["revision"] == 2
    assert row["judged_at"] == ""
    assert row["inactive"] == 0
    with sqlite3.connect(str(db)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM memorias").fetchone()[0] == 1


def test_supersede_reaches_a_scrubbed_draft(tmp_path):
    db = tmp_path / "memorias.db"
    store = MemoriaStore(db)
    row_id, key = _seed_draft(store)
    _reject(store)

    result = store.supersede_draft("p", key, "hollow knight sabados",
                                   "contexto: quise decir los sabados → Kira: anotado")

    assert result == (row_id, 2)
    assert _row(db, row_id)["stable_key"] == key


def test_restore_never_collides_with_a_live_plain_key(tmp_path):
    """A plain-key row that already exists wins; the scrubbed row is left alone."""
    db = tmp_path / "memorias.db"
    store = MemoriaStore(db)
    row_id, key = _seed_draft(store)
    _reject(store)
    scrubbed_key = _row(db, row_id)["stable_key"]
    with sqlite3.connect(str(db)) as conn:
        conn.execute(
            "INSERT INTO memorias (id, profile_id, stable_key, revision, title, content, "
            "status, pinned, private, inactive, created_at, updated_at) "
            "VALUES ('other', 'p', ?, 1, 't', 'c', 'curated', 0, 0, 0, 'x', 'x')",
            (key,),
        )

    assert store.upsert_draft("p", key, "hollow knight domingos", _PAIR) is None
    assert _row(db, row_id)["stable_key"] == scrubbed_key


# ---------------------------------------------------------------------------
# 3. Unjudged drafts never reach injection
# ---------------------------------------------------------------------------

def _insert(db, row_id, *, status, content="dato alpha beta gamma", judged_at="",
            inactive=0, origin=None, stable_key=None, signature="alpha beta gamma"):
    with sqlite3.connect(str(db)) as conn:
        conn.execute(
            "INSERT INTO memorias (id, profile_id, stable_key, revision, title, content, "
            "status, pinned, private, inactive, created_at, updated_at, signature, "
            "judged_at, origin) VALUES (?, 'p', ?, 1, 'alpha beta gamma', ?, ?, 0, 0, ?, "
            "'2026-01-01', '2026-01-01', ?, ?, ?)",
            (row_id, stable_key or f"p|{row_id}", content, status, inactive, signature,
             judged_at, origin),
        )


def test_injection_candidates_exclude_unjudged_drafts(tmp_path):
    db = tmp_path / "memorias.db"
    store = MemoriaStore(db)
    _insert(db, "draft", status="draft")
    for status in ("promoted", "curated", "imported", "summary"):
        _insert(db, status, status=status)

    ids = {row["id"] for row in store.list_injection_candidates("p")}

    assert ids == {"promoted", "curated", "imported", "summary"}


def test_uncertain_keep_draft_stays_injectable(tmp_path):
    """An uncertain keep is status='draft' but judged and already rewritten by
    the judge (third person, no raw words) - not an unjudged draft."""
    db = tmp_path / "memorias.db"
    store = MemoriaStore(db)
    _insert(db, "uncertain", status="draft", judged_at="2026-01-02")

    assert [row["id"] for row in store.list_injection_candidates("p")] == ["uncertain"]


def test_blank_content_rows_are_never_injection_candidates(tmp_path):
    """A scrubbed reject the operator un-hides must not inject an empty line."""
    db = tmp_path / "memorias.db"
    store = MemoriaStore(db)
    _insert(db, "blank", status="draft", content="", judged_at="2026-01-02", signature="")

    assert store.list_injection_candidates("p") == []


def _make_motor(monkeypatch, tmp_path):
    from opencohost.core import llm_engine

    motor = llm_engine.MotorVocalIA(queue.Queue(), lambda event: None)
    motor.ollama = MagicMock()
    motor.pygame = MagicMock()
    motor.is_ready = True
    monkeypatch.setattr(llm_engine, "MEMORIAS_ENABLED", True)
    monkeypatch.setattr(llm_engine, "MEMORIAS_DB", str(tmp_path / "memorias.db"))
    motor._current_profile_id = "p"
    return motor


@pytest.mark.parametrize("query", ["hablemos de alpha beta gamma", "¿Qué recordás de mí?"])
def test_engine_block_ignores_unjudged_drafts_lexical_and_meta(monkeypatch, tmp_path, query):
    motor = _make_motor(monkeypatch, tmp_path)
    db = tmp_path / "memorias.db"
    MemoriaStore(db)
    _insert(db, "draft", status="draft", content="contexto: alpha beta gamma crudo")

    assert motor._build_memorias_injection_block("p", query) == ""


# ---------------------------------------------------------------------------
# 4. origin: schema v5, judge contract, persistence, rendering
# ---------------------------------------------------------------------------

def test_schema_v5_adds_nullable_origin_column(tmp_path):
    db = tmp_path / "memorias.db"
    MemoriaStore(db)
    with sqlite3.connect(str(db)) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 8
        cols = {r[1]: r for r in conn.execute("PRAGMA table_info(memorias)")}
    assert "origin" in cols
    assert cols["origin"][3] == 0  # nullable


def test_v4_db_migrates_to_v5_idempotently_with_null_origin(tmp_path):
    db = tmp_path / "memorias.db"
    store = MemoriaStore(db)
    row_id, _ = _seed_draft(store)
    with sqlite3.connect(str(db)) as conn:
        conn.execute("ALTER TABLE memorias DROP COLUMN origin")
        conn.execute("PRAGMA user_version = 4")

    MemoriaStore(db)
    MemoriaStore(db)

    with sqlite3.connect(str(db)) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 8
    assert _row(db, row_id)["origin"] is None


def test_v5_migration_tolerates_preadded_origin_column(tmp_path):
    db = tmp_path / "memorias.db"
    MemoriaStore(db)
    with sqlite3.connect(str(db)) as conn:
        conn.execute("PRAGMA user_version = 4")
    MemoriaStore(db)
    with sqlite3.connect(str(db)) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 8


def test_origin_column_rejects_unknown_values(tmp_path):
    db = tmp_path / "memorias.db"
    MemoriaStore(db)
    with pytest.raises(sqlite3.IntegrityError):
        _insert(db, "bad", status="promoted", origin="gossip")


def test_memory_decision_accepts_origin_and_tolerates_missing_or_invalid():
    from opencohost.core.memory.models import MemoryDecision

    ok = MemoryDecision.model_validate(
        {"i": 1, "keep": True, "text": "x", "origin": "shared_content"})
    assert ok.origin == "shared_content"
    assert MemoryDecision.model_validate({"i": 1, "keep": True, "text": "x"}).origin is None
    bad = MemoryDecision.model_validate(
        {"i": 1, "keep": True, "text": "x", "origin": "gossip"})
    assert bad.origin is None
    assert bad.keep is True


def test_judge_schema_requires_origin_for_constrained_decoding():
    from opencohost.core.memory.models import MemoryJudgeResult

    schema = MemoryJudgeResult.model_json_schema()
    decision = schema["$defs"]["MemoryDecision"]
    assert "origin" in decision["required"]


def test_judge_prompt_explains_origin():
    from opencohost.core.memory.promotion_parser import _PROMOTION_JUDGE_PROMPT

    assert '"origin"' in _PROMOTION_JUDGE_PROMPT
    assert "self_stated" in _PROMOTION_JUDGE_PROMPT
    assert "shared_content" in _PROMOTION_JUDGE_PROMPT


def test_diagnostics_carry_origins_without_changing_decision_tuples():
    from opencohost.core.memory.promotion_parser import _parse_promotion_diagnostics

    text = json.dumps({"decisions": [
        {"i": 1, "keep": True, "text": "a", "origin": "self_stated"},
        {"i": 2, "keep": True, "text": "b", "origin": "gossip"},
        {"i": 3, "keep": False, "reason": "vague"},
    ]})
    diag = _parse_promotion_diagnostics(text, 3)

    assert diag.decisions == [(1, "a", False, ""), (2, "b", False, ""), (3, None, False, "vague")]
    assert diag.origins == {1: "self_stated", 2: None}


def test_keep_persists_origin_and_missing_origin_defaults_to_shared(tmp_path):
    db = tmp_path / "memorias.db"
    store = MemoriaStore(db)
    ids = []
    for text in (
        "contexto: mi perro se llama Toby y es un labrador → Kira: lindo",
        "contexto: en el chat dicen que el parche sale el viernes → Kira: veremos",
        "contexto: vi un video que dice que Silksong sale pronto → Kira: ojala",
    ):
        ids.append(_seed_draft(store, text=text)[0])
    batch = _batch(store)
    order = {row["id"]: i for i, row in enumerate(batch, start=1)}

    result = store.apply_promotion_batch(
        "p", batch,
        decisions=[(order[ids[0]], "El perro del operador se llama Toby.", False, ""),
                   (order[ids[1]], "Segun el chat, el parche sale el viernes.", False, ""),
                   (order[ids[2]], "Un video dice que Silksong sale pronto.", False, "")],
        unresolved={}, now_s=100,
        origins={order[ids[0]]: "self_stated", order[ids[1]]: "shared_content"},
    )

    assert _row(db, ids[0])["origin"] == "self_stated"
    assert _row(db, ids[1])["origin"] == "shared_content"
    assert _row(db, ids[2])["origin"] == "shared_content"
    assert result.reasons["origin_defaulted"] == 1


def test_apply_without_origins_kwarg_defaults_keeps_to_shared(tmp_path):
    db = tmp_path / "memorias.db"
    store = MemoriaStore(db)
    row_id, _ = _seed_draft(store)

    store.apply_promotion_batch(
        "p", _batch(store), decisions=[(1, "El operador juega los domingos.", False, "")],
        unresolved={}, now_s=100,
    )

    assert _row(db, row_id)["origin"] == "shared_content"


def _fake_row(row_id, content, origin, **extra):
    base = {
        "id": row_id, "content": content, "title": "alpha beta gamma",
        "signature": "alpha beta gamma", "pinned": 0, "created_at": "2026-01-01",
        "updated_at": "2026-01-01", "stable_key": f"p|{row_id}", "status": "promoted",
        "judged_at": "2026-01-02", "origin": origin,
    }
    base.update(extra)
    return base


def test_shared_content_rows_are_tagged_in_lexical_and_recency_lines():
    rows = [
        _fake_row("a", "Segun el chat, el parche sale el viernes.", "shared_content"),
        _fake_row("b", "El operador juega Hollow Knight.", "self_stated"),
        _fake_row("c", "Hecho heredado sin origen.", None),
    ]

    lexical = build_injection_lines(rows, "alpha beta gamma")
    recency = build_recency_lines(rows)

    for lines in (lexical, recency):
        assert "[shared content] Segun el chat, el parche sale el viernes." in lines
        assert "El operador juega Hollow Knight." in lines
        assert "Hecho heredado sin origen." in lines


def test_pinned_shared_content_row_is_tagged_too():
    rows = [_fake_row("a", "Un video dice que Silksong sale pronto.", "shared_content", pinned=1)]
    assert build_injection_lines(rows, "nada")[0].startswith("[shared content] ")


def test_rows_without_origin_key_render_untagged():
    row = _fake_row("a", "Hecho de un fixture sin columna origin.", None)
    del row["origin"]
    assert build_injection_lines([row], "alpha beta gamma") == [row["content"]]


def test_sweep_end_to_end_persists_origin_and_scrubs_reject(monkeypatch, tmp_path):
    """Real promote_pending_drafts through the injectable judge seam."""
    from tests.test_memoria_promotion import _Recorder, _decisions, _make_motor as _promo_motor

    motor = _promo_motor(monkeypatch, tmp_path)
    db = tmp_path / "memorias.db"
    store = MemoriaStore(db)
    kept_id = store.upsert_draft("profile-1", "profile-1|k1", "t1",
                                 "contexto: mi gato se llama Pixel → Kira: lindo nombre")
    rej_id = store.upsert_draft("profile-1", "profile-1|k2", "t2",
                                "contexto: que calor hace hoy aca → Kira: tremendo")
    batch_ids = [r["id"] for r in store.list_unjudged_drafts("profile-1", limit=8, now_s=100)]
    decisions = {
        batch_ids.index(kept_id) + 1: {"keep": True, "text": "El gato del operador se llama Pixel.",
                                       "origin": "self_stated"},
        batch_ids.index(rej_id) + 1: {"keep": False, "reason": "transient",
                                      "origin": "self_stated"},
    }
    stub = _Recorder(_decisions(*({"i": i, **d} for i, d in sorted(decisions.items()))))

    counts = motor.promote_pending_drafts(chat_callable=stub)

    assert counts["kept"] == 1 and counts["rejected"] == 1
    assert _row(db, kept_id)["origin"] == "self_stated"
    rejected = _row(db, rej_id)
    assert rejected["content"] == "" and rejected["title"] == ""
    assert "origin" in stub.prompt


# ---------------------------------------------------------------------------
# API: a scrubbed row still lists and loads (blank title/content)
# ---------------------------------------------------------------------------

def test_api_list_and_row_tolerate_a_scrubbed_row(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    import opencohost.api.main as main_mod
    from tests.test_api_phase1 import FakeHost

    db = tmp_path / "memorias.db"
    store = MemoriaStore(db)
    row_id, _ = _seed_draft(store)
    _reject(store)
    monkeypatch.setattr(main_mod, "MEMORIAS_ENABLED", True)
    monkeypatch.setattr(main_mod, "MEMORIAS_DB", str(db))
    monkeypatch.setattr(main_mod, "_host_active", False)
    monkeypatch.setattr(main_mod, "_memoria_store", None)
    app = main_mod.create_app(host_factory=FakeHost, cors_origins=["http://localhost:5173"])

    with TestClient(app) as client:
        listing = client.get("/api/memoria/list", params={"profile_id": "p"})
        row = client.get(f"/api/memoria/row/{row_id}", params={"profile_id": "p"})

    assert listing.status_code == 200
    items = listing.json()["items"]
    assert [(i["id"], i["title"], i["inactive"], i["draft"]) for i in items] == [
        (row_id, "", True, True)
    ]
    assert row.status_code == 200
    assert row.json()["content"] == ""
    assert row.json()["title"] == ""
