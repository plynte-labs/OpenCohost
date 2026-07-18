"""Tests for POST /api/memoria/import + the GET /api/memoria/list `imported`
badge field (memoria_import_20260718, WU3).

Strict-TDD RED-first coverage for the import route. Posture mirrors the sibling
memoria routes (test_api_memoria_mutations.py shape): tmp sqlite +
monkeypatched MEMORIAS_DB/MEMORIAS_ENABLED, and the lazy `_memoria_store`
singleton reset before/after each test so a stale tmp path never leaks.

PII: every fixture here is SYNTHETIC (fake names/facts only). The owner's real
export is never read by tests — it is uploaded through the running UI.

Four-state accounting under test (insert_imported -> Literal): a genuine INSERT
is `imported`, an ON-CONFLICT collision is `skipped_duplicates`, a too-short
claim is `skipped_too_short`, and a fail-open store error is `failed` — never
conflated with a duplicate (R4).
"""

import sqlite3

import pytest
from fastapi.testclient import TestClient

from tests.test_api_phase1 import FakeHost

_DEFAULT_TEST_ORIGINS = ["http://localhost:5173"]


@pytest.fixture(autouse=True)
def _reset_host_active():
    import opencohost.api.main as main_mod

    main_mod._host_active = False
    yield
    main_mod._host_active = False


@pytest.fixture(autouse=True)
def _reset_memoria_store():
    import opencohost.api.main as main_mod

    main_mod._memoria_store = None
    yield
    main_mod._memoria_store = None


def _app():
    import opencohost.api.main as main_mod

    return main_mod.create_app(host_factory=FakeHost, cors_origins=_DEFAULT_TEST_ORIGINS)


def _enable(monkeypatch, db_path, enabled=True):
    import opencohost.api.main as main_mod

    monkeypatch.setattr(main_mod, "MEMORIAS_ENABLED", enabled)
    monkeypatch.setattr(main_mod, "MEMORIAS_DB", str(db_path))
    # Keep the migrate-on-read list path out of these tests' way.
    monkeypatch.setattr(main_mod, "cargar_perfiles", lambda: {})


# Synthetic Gemini-shaped export: three distinct, capturable claims (one carries
# a folded Fechas date), a Prueba line that must NOT become its own item, and a
# trailing source hint that must be dropped.
_GEMINI_THREE = (
    "## 1. Datos\n"
    "* Ana Test colecciona teclados mecanicos vintage raros.\n"
    "* Prueba: Lo dijo al aire. Fechas: [2024-01-02]\n"
    "* Bruno Ejemplo entrena escalada deportiva todos los martes.\n"
    "\n"
    "## 2. Gustos\n"
    "* Prefiere documentales historicos sobre arquitectura romana antigua.\n"
    "Importado de: Gemini.\n"
)


def _count_imported(db_path, profile_id):
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM memorias WHERE profile_id = ? AND status = 'imported'",
            (profile_id,),
        ).fetchone()[0]
    finally:
        conn.close()


def _seed_imported(db_path, profile_id, n):
    """Insert *n* distinct synthetic status='imported' rows so count_imported
    returns a precise 'existing' baseline for the D6 cap tests."""
    from opencohost.core.memoria_store import MemoriaStore

    MemoriaStore(db_path)  # real schema + signature migration
    conn = sqlite3.connect(db_path)
    try:
        for i in range(n):
            conn.execute(
                "INSERT INTO memorias (id, profile_id, stable_key, revision, title, "
                "content, status, pinned, private, inactive, created_at, updated_at, "
                "signature) VALUES (?, ?, ?, 1, 'seed', 'seed content', 'imported', "
                "0, 0, 0, '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00', '')",
                (f"seed_{i}", profile_id, f"{profile_id}|import:seed-{i}"),
            )
        conn.commit()
    finally:
        conn.close()


# ── 3.1 happy path ──────────────────────────────────────────────────────────


def test_import_happy_path_counts_and_no_content_echo(tmp_path, monkeypatch):
    db_path = tmp_path / "memorias.db"
    _enable(monkeypatch, db_path)

    with TestClient(_app()) as client:
        resp = client.post(
            "/api/memoria/import",
            json={"profile_id": "p1", "source_label": "Gemini", "content": _GEMINI_THREE},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body == {
        "ok": True,
        "imported": 3,
        "skipped_duplicates": 0,
        "skipped_too_short": 0,
        "skipped_cap": 0,
        "failed": 0,
    }
    # R8: the response never echoes any claim/title/content text.
    raw = resp.text.lower()
    assert "teclados" not in raw
    assert "escalada" not in raw
    assert "documentales" not in raw
    # Rows actually landed as status='imported'.
    assert _count_imported(db_path, "p1") == 3


def test_reimport_same_file_is_all_duplicates(tmp_path, monkeypatch):
    db_path = tmp_path / "memorias.db"
    _enable(monkeypatch, db_path)

    payload = {"profile_id": "p1", "source_label": "Gemini", "content": _GEMINI_THREE}
    with TestClient(_app()) as client:
        first = client.post("/api/memoria/import", json=payload).json()
        second = client.post("/api/memoria/import", json=payload).json()

    assert first["imported"] == 3
    # Re-import: every claim collides -> skipped_duplicates, nothing overwritten,
    # NOT counted as failed.
    assert second == {
        "ok": True,
        "imported": 0,
        "skipped_duplicates": 3,
        "skipped_too_short": 0,
        "skipped_cap": 0,
        "failed": 0,
    }
    assert _count_imported(db_path, "p1") == 3


def test_import_too_short_claim_goes_to_skipped_too_short(tmp_path, monkeypatch):
    db_path = tmp_path / "memorias.db"
    _enable(monkeypatch, db_path)

    content = (
        "## 1. Datos\n"
        "* Ok.\n"  # one significant token -> too short, never stored
        "* Ana Test colecciona teclados mecanicos vintage raros.\n"
    )
    with TestClient(_app()) as client:
        body = client.post(
            "/api/memoria/import",
            json={"profile_id": "p1", "source_label": "Gemini", "content": content},
        ).json()

    assert body["imported"] == 1
    assert body["skipped_too_short"] == 1
    assert body["skipped_duplicates"] == 0
    assert body["failed"] == 0


# ── 3.2 422 branches ────────────────────────────────────────────────────────


def test_import_empty_content_is_422(tmp_path, monkeypatch):
    _enable(monkeypatch, tmp_path / "memorias.db")
    with TestClient(_app()) as client:
        resp = client.post(
            "/api/memoria/import",
            json={"profile_id": "p1", "source_label": "Gemini", "content": "   \n  "},
        )
    assert resp.status_code == 422


def test_import_blank_profile_id_is_422_not_500(tmp_path, monkeypatch):
    """Finding 1 (R3): an empty/whitespace profile_id passes Pydantic and would
    make insert_imported raise -> a bare 500. It must be a boundary 422."""
    _enable(monkeypatch, tmp_path / "memorias.db")
    with TestClient(_app()) as client:
        for pid in ("", "   "):
            resp = client.post(
                "/api/memoria/import",
                json={"profile_id": pid, "source_label": "Gemini", "content": _GEMINI_THREE},
            )
            assert resp.status_code == 422, pid
            assert resp.json()["detail"] == "invalid profile_id"


def test_import_source_label_control_char_sanitized(tmp_path, monkeypatch):
    """Finding 2 (R1): a control char embedded in source_label survives the
    whitespace-collapse and would raise ValueError inside sqlite3 -> 500. It must
    be stripped so a clean "[Gemini]" title prefix lands, request 200."""
    db_path = tmp_path / "memorias.db"
    _enable(monkeypatch, db_path)
    content = "## 1. Datos\n* Ana Test colecciona teclados mecanicos vintage raros.\n"
    with TestClient(_app()) as client:
        resp = client.post(
            "/api/memoria/import",
            json={"profile_id": "p1", "source_label": "\x00Gemini", "content": content},
        )
    assert resp.status_code == 200
    assert resp.json()["imported"] == 1
    conn = sqlite3.connect(db_path)
    try:
        title = conn.execute(
            "SELECT title FROM memorias WHERE profile_id='p1' AND status='imported'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert title.startswith("[Gemini] ")
    assert "\x00" not in title


def test_import_oversize_body_is_422(tmp_path, monkeypatch):
    _enable(monkeypatch, tmp_path / "memorias.db")
    huge = "palabra larga sintetica " * 4000  # ~96 KB, > 64 KB cap
    with TestClient(_app()) as client:
        resp = client.post(
            "/api/memoria/import",
            json={"profile_id": "p1", "source_label": "Gemini", "content": huge},
        )
    assert resp.status_code == 422


def test_import_too_many_items_is_422_with_split_hint(tmp_path, monkeypatch):
    _enable(monkeypatch, tmp_path / "memorias.db")
    # 101 generic bullets, each small -> under the byte cap but over the 100-item
    # cap. Honest refusal, no silent truncation.
    content = "\n".join(f"* dato sintetico numero {i} alpha beta" for i in range(101))
    with TestClient(_app()) as client:
        resp = client.post(
            "/api/memoria/import",
            json={"profile_id": "p1", "source_label": "Gemini", "content": content},
        )
    assert resp.status_code == 422
    assert "too many items" in resp.json()["detail"].lower()


def test_import_source_label_over_40_chars_is_422(tmp_path, monkeypatch):
    _enable(monkeypatch, tmp_path / "memorias.db")
    with TestClient(_app()) as client:
        resp = client.post(
            "/api/memoria/import",
            json={
                "profile_id": "p1",
                "source_label": "x" * 41,
                "content": _GEMINI_THREE,
            },
        )
    assert resp.status_code == 422


def test_import_cap_full_profile_is_422(tmp_path, monkeypatch):
    """Pre-flight reject ONLY when the profile is already AT the cap (D6)."""
    import opencohost.api.main as main_mod

    db_path = tmp_path / "memorias.db"
    _seed_imported(db_path, "p1", 2)  # existing == cap below
    _enable(monkeypatch, db_path)
    monkeypatch.setattr(main_mod, "MEMORIAS_IMPORT_CAP", 2)
    with TestClient(_app()) as client:
        resp = client.post(
            "/api/memoria/import",
            json={"profile_id": "p1", "source_label": "Gemini", "content": _GEMINI_THREE},
        )
    assert resp.status_code == 422
    # Nothing new written — the reject is pre-flight.
    assert _count_imported(db_path, "p1") == 2


def test_reimport_near_cap_all_duplicates_no_false_reject(tmp_path, monkeypatch):
    """D6 (finding 3.i): a full-duplicate re-import near the cap lands 0 rows and
    is NOT falsely rejected — every claim is a duplicate, skipped_cap stays 0."""
    import opencohost.api.main as main_mod

    db_path = tmp_path / "memorias.db"
    _enable(monkeypatch, db_path)
    # cap=4, first import lands 3 -> existing=3 is "near cap" but < cap.
    monkeypatch.setattr(main_mod, "MEMORIAS_IMPORT_CAP", 4)
    payload = {"profile_id": "p1", "source_label": "Gemini", "content": _GEMINI_THREE}
    with TestClient(_app()) as client:
        first = client.post("/api/memoria/import", json=payload).json()
        second = client.post("/api/memoria/import", json=payload)

    assert first["imported"] == 3
    assert second.status_code == 200
    assert second.json() == {
        "ok": True,
        "imported": 0,
        "skipped_duplicates": 3,
        "skipped_too_short": 0,
        "skipped_cap": 0,
        "failed": 0,
    }
    assert _count_imported(db_path, "p1") == 3


def test_import_cap_headroom_enforced_in_loop(tmp_path, monkeypatch):
    """D6 (finding 3.ii): existing == cap-1 + 3 fresh items -> imported=1, the
    two past the headroom land in skipped_cap, request still 200."""
    import opencohost.api.main as main_mod

    db_path = tmp_path / "memorias.db"
    _seed_imported(db_path, "p1", 1)  # existing == cap-1 below
    _enable(monkeypatch, db_path)
    monkeypatch.setattr(main_mod, "MEMORIAS_IMPORT_CAP", 2)
    with TestClient(_app()) as client:
        resp = client.post(
            "/api/memoria/import",
            json={"profile_id": "p1", "source_label": "Gemini", "content": _GEMINI_THREE},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["imported"] == 1
    assert body["skipped_cap"] == 2
    assert body["skipped_duplicates"] == 0
    assert body["skipped_too_short"] == 0
    assert body["failed"] == 0
    assert body["ok"] is True
    assert _count_imported(db_path, "p1") == 2  # seeded 1 + 1 fresh


# ── 3.3 503 store-down + disabled no-op ─────────────────────────────────────


def test_import_store_unavailable_is_503(tmp_path, monkeypatch):
    import opencohost.api.main as main_mod

    _enable(monkeypatch, tmp_path / "memorias.db")
    monkeypatch.setattr(
        main_mod,
        "_get_memoria_store",
        lambda: (_ for _ in ()).throw(sqlite3.OperationalError("database is locked")),
    )
    with TestClient(_app()) as client:
        resp = client.post(
            "/api/memoria/import",
            json={"profile_id": "p1", "source_label": "Gemini", "content": _GEMINI_THREE},
        )
    assert resp.status_code == 503
    assert resp.json() == {"detail": "memoria_unavailable"}


def test_import_aborts_after_three_consecutive_errors(tmp_path, monkeypatch):
    """Finding 4 (R4): under a sustained store error the loop must NOT grind all
    items — it early-aborts after 3 consecutive 'error' outcomes, counting every
    remaining item into `failed` (ok=False)."""
    from opencohost.core.memoria_store import MemoriaStore

    db_path = tmp_path / "memorias.db"
    _enable(monkeypatch, db_path)
    calls = {"n": 0}

    def _always_error(self, *args, **kwargs):
        calls["n"] += 1
        return "error"

    monkeypatch.setattr(MemoriaStore, "insert_imported", _always_error)
    content = "\n".join(
        f"* dato sintetico numero {i} alpha beta gamma" for i in range(10)
    )
    with TestClient(_app()) as client:
        resp = client.post(
            "/api/memoria/import",
            json={"profile_id": "p1", "source_label": "Gemini", "content": content},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert calls["n"] == 3  # only three inserts attempted, then abort
    assert body["failed"] == 10  # 3 attempted + 7 unattempted, all counted
    assert body["ok"] is False


def test_import_disabled_is_benign_noop(tmp_path, monkeypatch):
    _enable(monkeypatch, tmp_path / "memorias.db", enabled=False)
    with TestClient(_app()) as client:
        resp = client.post(
            "/api/memoria/import",
            json={"profile_id": "p1", "source_label": "Gemini", "content": _GEMINI_THREE},
        )
    assert resp.status_code == 200
    assert resp.json() == {
        "ok": False,
        "imported": 0,
        "skipped_duplicates": 0,
        "skipped_too_short": 0,
        "skipped_cap": 0,
        "failed": 0,
    }


# ── 3.4 GET /api/memoria/list carries the `imported` badge field ────────────


def _seed_list_db(db_path):
    """Real schema + one 'imported' row and one 'draft' row for the badge test."""
    from opencohost.core.memoria_store import MemoriaStore

    MemoriaStore(db_path)  # real schema/index/PRAGMA
    conn = sqlite3.connect(db_path)

    def _ins(row_id, stable_key, title, status):
        conn.execute(
            "INSERT INTO memorias (id, profile_id, stable_key, revision, title, content, "
            "status, pinned, private, inactive, created_at, updated_at) VALUES "
            "(?, 'p1', ?, 1, ?, 'content', ?, 0, 0, 0, "
            "'2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')",
            (row_id, stable_key, title, status),
        )

    _ins("mem_imported", "p1|import:a-b-c-d-e-f", "an imported row", "imported")
    _ins("mem_draft", "p1|g-h-i", "a draft row", "draft")
    conn.commit()
    conn.close()


def test_list_exposes_imported_flag_per_row(tmp_path, monkeypatch):
    db_path = tmp_path / "memorias.db"
    _seed_list_db(db_path)
    _enable(monkeypatch, db_path)

    with TestClient(_app()) as client:
        resp = client.get("/api/memoria/list", params={"profile_id": "p1"})

    assert resp.status_code == 200
    by_id = {item["id"]: item for item in resp.json()["items"]}
    assert by_id["mem_imported"]["imported"] is True
    assert by_id["mem_draft"]["imported"] is False
