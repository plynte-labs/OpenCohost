"""Tests for the Tier-B direct-read GET endpoints (B3): /api/models,
/api/tts/config, /api/memoria/stats.

All three are sync-def direct reads — no command_queue, no engine mutation,
no new host plumbing (design v2.1 Tier B). R8-CRITICAL: /api/memoria/stats
must never expose memoria/card text — counts only, via the
`memory_inspector_snapshot` provenance gate (reused verbatim, never
re-derived — see opencohost/core/llm_engine.py).
"""

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


def _app():
    import opencohost.api.main as main_mod

    return main_mod.create_app(host_factory=FakeHost, cors_origins=_DEFAULT_TEST_ORIGINS)


# ──────────────────────────────────────────────────────────────────────────
# GET /api/models
# ──────────────────────────────────────────────────────────────────────────


def test_models_shape_and_real_catalog():
    from opencohost.config.settings import MODELS_CATALOG

    app = _app()
    with TestClient(app) as client:
        resp = client.get("/api/models")
        assert resp.status_code == 200
        body = resp.json()
        assert set(body.keys()) == {"catalog", "discovered", "current_model", "tiers", "active_tier"}
        # real curated catalog, not a hand-rolled stub
        assert set(body["catalog"].keys()) == set(MODELS_CATALOG.keys())
        assert body["current_model"] == "qwen3:8b"
        assert body["active_tier"] == "balanced"
        assert isinstance(body["tiers"], dict) and body["tiers"]
        assert isinstance(body["discovered"], list)


def test_models_discovery_timeout_degrades_to_catalog_only(monkeypatch):
    import opencohost.api.main as main_mod

    def _raise_timeout(*args, **kwargs):
        raise TimeoutError("ollama discovery timed out")

    monkeypatch.setattr(main_mod, "_discover_ollama_models", _raise_timeout)

    app = _app()
    with TestClient(app) as client:
        resp = client.get("/api/models")
        assert resp.status_code == 200
        body = resp.json()
        assert body["discovered"] == []
        assert body["catalog"]  # catalog still present


def test_models_discovery_error_degrades_never_500(monkeypatch):
    import opencohost.api.main as main_mod

    class _BrokenOllama:
        def Client(self, *a, **kw):
            raise ConnectionError("ollama unreachable")

    monkeypatch.setattr(main_mod, "ollama", _BrokenOllama())

    app = _app()
    with TestClient(app) as client:
        resp = client.get("/api/models")
        assert resp.status_code == 200
        body = resp.json()
        assert body["discovered"] == []


# ──────────────────────────────────────────────────────────────────────────
# GET /api/tts/config
# ──────────────────────────────────────────────────────────────────────────


def test_tts_config_shape_from_accessors(monkeypatch):
    import opencohost.api.main as main_mod

    monkeypatch.setattr(main_mod, "load_piper_voice", lambda **kwargs: "es_kira")
    monkeypatch.setattr(main_mod, "load_tts_local_only", lambda: True)
    monkeypatch.setattr(main_mod, "load_tts_speed", lambda: 1.25)
    monkeypatch.setattr(main_mod, "EXPERIMENTAL_HEAVY_TTS_ENABLED", True)

    app = _app()
    with TestClient(app) as client:
        app.state.host.motor.motor_tts = "pesado"
        resp = client.get("/api/tts/config")
        assert resp.status_code == 200
        body = resp.json()
        assert body == {
            "piper_voice": "es_kira",
            "local_only": True,
            "speed": 1.25,
            "engine": "pesado",
            "heavy_available": True,
        }


def test_tts_config_does_not_touch_command_queue():
    app = _app()
    with TestClient(app) as client:
        client.get("/api/tts/config")
        assert app.state.host.motor.command_queue.qsize() == 0
        assert app.state.dispatcher.state_version == 0


# ──────────────────────────────────────────────────────────────────────────
# GET /api/memoria/stats (R8-CRITICAL)
# ──────────────────────────────────────────────────────────────────────────

_FORBIDDEN_SUBSTRINGS = ("content", "transcript", "dialogue", "persona", "prompt", "title")


def test_memoria_stats_shape_counts_only():
    app = _app()
    with TestClient(app) as client:
        resp = client.get("/api/memoria/stats")
        assert resp.status_code == 200
        body = resp.json()
        assert set(body.keys()) == {
            "session_turns",
            "digest_entries",
            "saved_memorias",
            "pinned",
            "saved_memorias_total",
            "pinned_total",
            "editorial_cards_by_status",
        }
        for key in (
            "session_turns",
            "digest_entries",
            "saved_memorias",
            "pinned",
            "saved_memorias_total",
            "pinned_total",
        ):
            assert isinstance(body[key], int)


def test_memoria_stats_never_leaks_text_fields():
    """R8: strong negative assertion — no text/persona/transcript anywhere
    in the response, including keys and string values."""
    app = _app()
    with TestClient(app) as client:
        app.state.host.motor._memory_snapshot = {
            "entries": [
                {"turn_index": 0, "role": "user", "source": "direct", "content_chars": 12, "content": "secret text"},
                {"turn_index": 1, "role": "assistant", "source": "ptt", "content_chars": 20, "content": "Kira's own words"},
            ],
            "source_breakdown": {"direct": 1, "ptt": 1},
            "digest": {"line_count": 3, "total_chars": 90, "max_chars": 4000},
        }
        resp = client.get("/api/memoria/stats")
        assert resp.status_code == 200
        raw = resp.text.lower()
        assert "secret text" not in raw
        assert "kira's own words" not in raw
        for forbidden in _FORBIDDEN_SUBSTRINGS:
            assert forbidden not in raw
        assert resp.json()["session_turns"] == 2
        assert resp.json()["digest_entries"] == 3


def test_memoria_stats_reuses_memory_inspector_snapshot_gate():
    """Must call the engine's memory_inspector_snapshot() (the ONLY
    provenance gate) rather than re-deriving _DIGEST_CAPTURE_SOURCES
    filtering in the API layer."""
    app = _app()
    calls = {"n": 0}
    with TestClient(app) as client:
        original = app.state.host.motor.memory_inspector_snapshot

        def _spy():
            calls["n"] += 1
            return original()

        app.state.host.motor.memory_inspector_snapshot = _spy
        client.get("/api/memoria/stats")
        assert calls["n"] == 1


def test_memoria_stats_does_not_touch_command_queue():
    app = _app()
    with TestClient(app) as client:
        client.get("/api/memoria/stats")
        assert app.state.host.motor.command_queue.qsize() == 0
        assert app.state.dispatcher.state_version == 0


def test_memoria_stats_memorias_enabled_true_counts_real_rows(tmp_path, monkeypatch):
    import sqlite3

    import opencohost.api.main as main_mod

    db_path = tmp_path / "memorias.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE memorias (id TEXT PRIMARY KEY, pinned INTEGER NOT NULL DEFAULT 0)"
    )
    conn.execute("INSERT INTO memorias (id, pinned) VALUES ('a', 1)")
    conn.execute("INSERT INTO memorias (id, pinned) VALUES ('b', 0)")
    conn.commit()
    conn.close()

    monkeypatch.setattr(main_mod, "MEMORIAS_ENABLED", True)
    monkeypatch.setattr(main_mod, "MEMORIAS_DB", str(db_path))

    app = _app()
    with TestClient(app) as client:
        resp = client.get("/api/memoria/stats")
        body = resp.json()
        assert body["saved_memorias"] == 2
        assert body["pinned"] == 1
        # No profile_id -> per-profile figures coincide with the global totals.
        assert body["saved_memorias_total"] == 2
        assert body["pinned_total"] == 1


def test_memoria_stats_profile_id_splits_per_profile_vs_global(tmp_path, monkeypatch):
    """FIX-A: with ?profile_id=, saved_memorias/pinned filter to that profile
    (parity with MemoriaStore.count_all/count_all_pinned) while
    saved_memorias_total/pinned_total stay global."""
    import sqlite3

    import opencohost.api.main as main_mod

    db_path = tmp_path / "memorias.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE memorias (id TEXT PRIMARY KEY, profile_id TEXT NOT NULL, "
        "pinned INTEGER NOT NULL DEFAULT 0)"
    )
    # Profile "akira": 3 rows, 2 pinned. Profile "bravo": 1 row, 0 pinned.
    conn.execute("INSERT INTO memorias (id, profile_id, pinned) VALUES ('a1', 'akira', 1)")
    conn.execute("INSERT INTO memorias (id, profile_id, pinned) VALUES ('a2', 'akira', 1)")
    conn.execute("INSERT INTO memorias (id, profile_id, pinned) VALUES ('a3', 'akira', 0)")
    conn.execute("INSERT INTO memorias (id, profile_id, pinned) VALUES ('b1', 'bravo', 0)")
    conn.commit()
    conn.close()

    monkeypatch.setattr(main_mod, "MEMORIAS_ENABLED", True)
    monkeypatch.setattr(main_mod, "MEMORIAS_DB", str(db_path))

    app = _app()
    with TestClient(app) as client:
        resp = client.get("/api/memoria/stats", params={"profile_id": "akira"})
        assert resp.status_code == 200
        body = resp.json()
        # Per-profile figures for "akira".
        assert body["saved_memorias"] == 3
        assert body["pinned"] == 2
        # Global totals across every profile.
        assert body["saved_memorias_total"] == 4
        assert body["pinned_total"] == 2

        # A profile with a single, unpinned row.
        resp_b = client.get("/api/memoria/stats", params={"profile_id": "bravo"})
        body_b = resp_b.json()
        assert body_b["saved_memorias"] == 1
        assert body_b["pinned"] == 0
        assert body_b["saved_memorias_total"] == 4
        assert body_b["pinned_total"] == 2

        # An unknown profile -> zero per-profile, totals unchanged.
        resp_x = client.get("/api/memoria/stats", params={"profile_id": "ghost"})
        body_x = resp_x.json()
        assert body_x["saved_memorias"] == 0
        assert body_x["pinned"] == 0
        assert body_x["saved_memorias_total"] == 4


def test_memoria_stats_memorias_disabled_returns_zeros_gracefully(tmp_path, monkeypatch):
    import sqlite3

    import opencohost.api.main as main_mod

    db_path = tmp_path / "memorias.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE memorias (id TEXT PRIMARY KEY, pinned INTEGER NOT NULL DEFAULT 0)"
    )
    conn.execute("INSERT INTO memorias (id, pinned) VALUES ('a', 1)")
    conn.commit()
    conn.close()

    monkeypatch.setattr(main_mod, "MEMORIAS_ENABLED", False)
    monkeypatch.setattr(main_mod, "MEMORIAS_DB", str(db_path))

    app = _app()
    with TestClient(app) as client:
        resp = client.get("/api/memoria/stats")
        assert resp.status_code == 200
        body = resp.json()
        assert body["saved_memorias"] == 0
        assert body["pinned"] == 0


def test_memoria_stats_editorial_cards_by_status(tmp_path, monkeypatch):
    import sqlite3

    import opencohost.api.main as main_mod

    db_path = tmp_path / "cards.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE editorial_cards (id TEXT PRIMARY KEY, status TEXT NOT NULL)")
    conn.execute("INSERT INTO editorial_cards (id, status) VALUES ('c1', 'armed')")
    conn.execute("INSERT INTO editorial_cards (id, status) VALUES ('c2', 'armed')")
    conn.execute("INSERT INTO editorial_cards (id, status) VALUES ('c3', 'used')")
    conn.commit()
    conn.close()

    monkeypatch.setattr(main_mod, "EDITORIAL_CARDS_DB", str(db_path))

    app = _app()
    with TestClient(app) as client:
        resp = client.get("/api/memoria/stats")
        body = resp.json()
        assert body["editorial_cards_by_status"] == {"armed": 2, "used": 1}


def test_memoria_stats_missing_db_files_return_zero_not_500(tmp_path, monkeypatch):
    import opencohost.api.main as main_mod

    monkeypatch.setattr(main_mod, "MEMORIAS_ENABLED", True)
    monkeypatch.setattr(main_mod, "MEMORIAS_DB", str(tmp_path / "nope.db"))
    monkeypatch.setattr(main_mod, "EDITORIAL_CARDS_DB", str(tmp_path / "also-nope.db"))

    app = _app()
    with TestClient(app) as client:
        resp = client.get("/api/memoria/stats")
        assert resp.status_code == 200
        body = resp.json()
        assert body["saved_memorias"] == 0
        assert body["pinned"] == 0
        assert body["editorial_cards_by_status"] == {}
