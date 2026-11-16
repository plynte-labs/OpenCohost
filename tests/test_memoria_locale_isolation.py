"""Memoria locale isolation (Block 2, agenda-deck-variants-and-safety #68).

Contract:
- every memoria write stores the PRIMARY language subtag (``en-US``/``en_US``/
  ``EN`` -> ``en``); empty / missing / malformed -> ``unknown``;
- ``list_injection_candidates(lang=...)`` matches the primary subtag, legacy
  regional rows (``en-US`` / ``en_US``) and ``unknown`` rows, never another
  language;
- the engine resolves the turn language once (profile locale first, then the
  active bundle) and uses it for injection, capture, close-flush, switch-flush,
  session summaries and owner reflection; a profile switch tags the DEPARTING
  profile's flush with the departing locale;
- locale-filtered injection never evicts the other locale's cached fact vectors
  (``FactSemanticIndex.reconcile`` drops every row id missing from its input).

Temp DBs only; synthetic text only.
"""
from __future__ import annotations

import queue
import sqlite3
import threading
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

import opencohost.core.llm_engine as llm_engine
from opencohost.core.context.prompt_assembler import PromptContextAssembler
from opencohost.core.memory.memoria_store import MemoriaStore
from opencohost.i18n import active as i18n_active

PROFILE = "profile-1"
_TS = datetime(2026, 1, 1, tzinfo=timezone.utc).isoformat()
_ELIGIBLE_USER = "streamer prefiere musica synthwave calma"
_ELIGIBLE_ASST = "Kira dice que es genial. Fin."


@pytest.fixture(autouse=True)
def _pin_es_bundle():
    """The active bundle stays Spanish so every English expectation below can
    only come from the profile locale, never from the bundle."""
    from opencohost.i18n.startup import resolve_active_bundle

    i18n_active.set_active_bundle(resolve_active_bundle(locale="es"))
    yield
    i18n_active.reset_active_bundle()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _seed(db_path, rows):
    MemoriaStore(db_path)
    with sqlite3.connect(str(db_path)) as conn:
        conn.executemany(
            "INSERT INTO memorias (id, profile_id, stable_key, revision, title, content, status, "
            "pinned, private, inactive, created_at, updated_at, signature, lang) "
            "VALUES (?, ?, ?, 1, 'titulo', ?, ?, ?, 0, 0, ?, ?, '', ?)",
            [
                (r["id"], PROFILE, f"{PROFILE}|{r['id']}", r.get("content", f"contenido {r['id']}"),
                 r.get("status", "promoted"), 1 if r.get("pinned") else 0, _TS, _TS, r["lang"])
                for r in rows
            ],
        )


def _make_motor(monkeypatch, tmp_path, *, profile_id=PROFILE, locale=None):
    monkeypatch.setattr(llm_engine, "MEMORIAS_ENABLED", True)
    monkeypatch.setattr(llm_engine, "MEMORIAS_DB", str(tmp_path / "memorias.db"))
    motor = llm_engine.MotorVocalIA(queue.Queue(), lambda e: None)
    motor.ollama = MagicMock()
    motor.pygame = MagicMock()
    motor.is_ready = True
    motor._current_profile_id = profile_id
    motor._current_profile_locale = locale
    motor._memory = None
    return motor


def _langs(tmp_path, profile_id=PROFILE, status=None):
    conn = sqlite3.connect(str(tmp_path / "memorias.db"))
    try:
        query = "SELECT lang FROM memorias WHERE profile_id = ?"
        params = [profile_id]
        if status is not None:
            query += " AND status = ?"
            params.append(status)
        return [row[0] for row in conn.execute(query, params).fetchall()]
    finally:
        conn.close()


def _prompt_text(motor, query="hablemos de musica synthwave"):
    setup = motor._build_generation_request(
        query, source="direct", is_local=True, provider_cfg={},
        request_model="m", watchdog_timeout=10.0,
    )
    return "\n".join(str(m.get("content", "")) for m in setup.messages)


# ---------------------------------------------------------------------------
# Store: write normalization
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw, expected",
    [("en-US", "en"), ("en_US", "en"), ("EN", "en"), ("es-MX", "es"), ("es", "es"),
     ("unknown", "unknown"), ("", "unknown"), (None, "unknown"), ("x", "unknown")],
)
def test_every_write_path_stores_the_primary_subtag(tmp_path, raw, expected):
    store = MemoriaStore(tmp_path / "memorias.db")

    draft_id = store.upsert_draft(PROFILE, f"{PROFILE}|draft", "t", "draft body", lang=raw)
    curated_id = store.insert_curated(PROFILE, "t", "curated body", lang=raw)
    summary_id = store.insert_summary(PROFILE, "t", "summary body", lang=raw)
    store.insert_imported(PROFILE, "t", "imported body unique words here", lang=raw)

    assert store.get(draft_id)["lang"] == expected
    assert store.get(curated_id)["lang"] == expected
    assert store.get(summary_id)["lang"] == expected
    assert _langs(tmp_path, status="imported") == [expected]


# ---------------------------------------------------------------------------
# Store: read matching
# ---------------------------------------------------------------------------

def _seed_mixed(db_path):
    _seed(db_path, [
        {"id": "en", "lang": "en"},
        {"id": "en-us", "lang": "en-US"},
        {"id": "en_us", "lang": "en_US"},
        {"id": "eng", "lang": "eng"},
        {"id": "es", "lang": "es"},
        {"id": "es-mx", "lang": "es-MX"},
        {"id": "unk", "lang": "unknown"},
    ])


@pytest.mark.parametrize("query_lang", ["en", "en-US", "en_US", "EN"])
def test_english_turn_reads_primary_legacy_regional_and_unknown_rows_only(tmp_path, query_lang):
    db = tmp_path / "memorias.db"
    _seed_mixed(db)

    ids = {r["id"] for r in MemoriaStore(db).list_injection_candidates(PROFILE, lang=query_lang)}

    assert ids == {"en", "en-us", "en_us", "unk"}


def test_spanish_turn_never_reads_english_rows(tmp_path):
    db = tmp_path / "memorias.db"
    _seed_mixed(db)

    ids = {r["id"] for r in MemoriaStore(db).list_injection_candidates(PROFILE, lang="es-MX")}

    assert ids == {"es", "es-mx", "unk"}


def test_no_lang_filter_keeps_every_eligible_row(tmp_path):
    db = tmp_path / "memorias.db"
    _seed_mixed(db)

    ids = {r["id"] for r in MemoriaStore(db).list_injection_candidates(PROFILE)}

    assert ids == {"en", "en-us", "en_us", "eng", "es", "es-mx", "unk"}


# ---------------------------------------------------------------------------
# Assembler: the builder receives the turn language when it accepts it
# ---------------------------------------------------------------------------

def _assemble(builder, lang):
    return PromptContextAssembler().assemble(
        "hablemos de musica synthwave", "ptt",
        system_prompt="Eres Kira.", use_system_role=True, is_local=True,
        provider_cfg={}, request_model="llama3", history_snapshot=[],
        memorias_profile_id="p1", memorias_builder=builder, memorias_enabled=True,
        lang=lang,
    )


def test_assembler_passes_lang_to_a_builder_that_accepts_it():
    seen = []

    def builder(profile_id, query, lang=None):
        seen.append(lang)
        return ""

    _assemble(builder, "en-US")

    assert seen == ["en"]


def test_assembler_keeps_supporting_two_argument_builders():
    seen = []
    _assemble(lambda pid, ctx: seen.append((pid, ctx)) or "", "en")

    assert seen == [("p1", "hablemos de musica synthwave")]


# ---------------------------------------------------------------------------
# Engine: injection
# ---------------------------------------------------------------------------

def test_english_profile_turn_injects_only_english_and_unknown_facts(monkeypatch, tmp_path):
    motor = _make_motor(monkeypatch, tmp_path, locale="en")
    _seed(tmp_path / "memorias.db", [
        {"id": "es", "lang": "es", "pinned": True, "content": "ZQSPANISHFACT el perro se llama firulais"},
        {"id": "en", "lang": "en-US", "pinned": True, "content": "ZQENGLISHFACT the dog is called rex"},
    ])

    text = _prompt_text(motor)

    assert "ZQENGLISHFACT" in text
    assert "ZQSPANISHFACT" not in text


def test_untagged_profile_falls_back_to_the_active_bundle_language(monkeypatch, tmp_path):
    motor = _make_motor(monkeypatch, tmp_path, locale=None)
    _seed(tmp_path / "memorias.db", [
        {"id": "es", "lang": "es", "pinned": True, "content": "ZQSPANISHFACT el perro se llama firulais"},
        {"id": "en", "lang": "en", "pinned": True, "content": "ZQENGLISHFACT the dog is called rex"},
    ])

    text = _prompt_text(motor)

    assert "ZQSPANISHFACT" in text
    assert "ZQENGLISHFACT" not in text


# ---------------------------------------------------------------------------
# Engine: writes carry the turn language
# ---------------------------------------------------------------------------

def test_capture_at_commit_tags_the_profile_locale(monkeypatch, tmp_path):
    motor = _make_motor(monkeypatch, tmp_path, locale="en")

    motor._commit_history(_ELIGIBLE_USER, _ELIGIBLE_ASST, source="direct")

    assert _langs(tmp_path) == ["en"]


def test_close_flush_tags_drafts_and_summary_with_the_profile_locale(monkeypatch, tmp_path):
    motor = _make_motor(monkeypatch, tmp_path, locale="en")
    motor.historial.append({"role": "user", "content": _ELIGIBLE_USER, "source": "direct", "private": False})
    motor.historial.append({"role": "assistant", "content": _ELIGIBLE_ASST, "source": "direct", "private": False})
    motor._session_memoria_titles = [
        f"zqtitle{i} synthwave calma" for i in range(llm_engine.MEMORIAS_SUMMARY_MIN_TITLES)
    ]

    motor.flush_memorias()

    assert _langs(tmp_path, status="draft") == ["en"]
    assert _langs(tmp_path, status="summary") == ["en"]


def test_profile_switch_flush_uses_the_departing_locale(monkeypatch, tmp_path):
    motor = _make_motor(monkeypatch, tmp_path, profile_id="profile-old", locale="es")
    motor.historial.append({"role": "user", "content": _ELIGIBLE_USER, "source": "direct", "private": False})
    motor.historial.append({"role": "assistant", "content": _ELIGIBLE_ASST, "source": "direct", "private": False})
    motor._session_memoria_titles = [
        f"zqtitle{i} synthwave calma" for i in range(llm_engine.MEMORIAS_SUMMARY_MIN_TITLES)
    ]

    created = []
    original_thread_cls = threading.Thread

    class _CapturingThread(original_thread_cls):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            created.append(self)

    monkeypatch.setattr(llm_engine.threading, "Thread", _CapturingThread)
    motor._dispatch_command("set_profile", {
        "prompt": "You are Kira.", "use_system": True, "_profile_name": "Kira",
        "id": "profile-new", "locale": "en",
    })
    for t in created:
        t.join(timeout=5)

    assert motor._current_profile_locale == "en"
    assert _langs(tmp_path, "profile-old", status="draft") == ["es"]
    assert _langs(tmp_path, "profile-old", status="summary") == ["es"]


def test_owner_reflection_fact_is_tagged_with_the_profile_locale(monkeypatch, tmp_path):
    from tests.owner_reflection_support import fact_json, feed, install_stream, make_motor, run_job

    motor = make_motor(monkeypatch, tmp_path)
    motor._current_profile_locale = "en"
    feed(motor, 8)
    install_stream(monkeypatch, motor, fact_json(("What is the owner's dog called?", "Rex.", (1,))))

    run_job(motor)

    assert _langs(tmp_path, status="draft") == ["en"]


# ---------------------------------------------------------------------------
# Engine: locale filtering never evicts the other locale's fact vectors
# ---------------------------------------------------------------------------

def test_english_turn_keeps_cached_spanish_fact_vectors(monkeypatch, tmp_path):
    from opencohost.core.memory_v5_shadow.subsystem import MemorySubsystem
    from tests.test_memory_fact_semantic_rank import FakeCoordinator, _make_index

    motor = _make_motor(monkeypatch, tmp_path, locale="en")
    db = tmp_path / "memorias.db"
    _seed(db, [
        {"id": "es", "lang": "es", "content": "zqcarfact el streamer maneja un auto rojo"},
        {"id": "en", "lang": "en", "content": "zqpetfact the streamer has a dog named rex"},
    ])
    scheduled = []
    index, cache, _worker = _make_index(tmp_path, scheduled=scheduled)
    index.reconcile(PROFILE, MemoriaStore(db).list_injection_candidates(PROFILE))
    motor._memory = MemorySubsystem(mode="ACTIVE", coordinator=FakeCoordinator(), fact_index=index)

    _prompt_text(motor, "zqquery what is my pet called")
    for fn in scheduled:
        fn()

    cached = {rec.fact_id for rec in cache.get_fact_embeddings_by_profile(PROFILE)}
    assert cached == {"es", "en"}
