"""Tests for the W2b/W4 meta/temporal recall router (memoria_recall_20260718).

Pins the retrieval-side half of the recall fix. A META/temporal query — one
asking Kira what she remembers or what happened in a past session ("¿qué
recordás de mí?", "de qué hablamos la sesión pasada") — cannot be answered by
lexical select_top_k: it has no topical anchor, no recency term, and the schema
has no "session" concept. On such a query _build_memorias_injection_block routes
to RECENCY instead: machine-authored session summaries first (found by
_SESSION_SUMMARY_MARKER in stable_key), then the newest regular rows, capped at
MEMORIAS_META_RECALL_K, under the same 700-char budget + sanitize/strip pipeline.

W4 (owner-gated fallback) is satisfied STRUCTURALLY: recency fires ONLY on a
detected meta query; a topical query with zero lexical matches still injects
nothing (the untouched select_top_k path), so off-topic recency noise never
leaks onto a topical turn.

The two REAL owner queries that failed in production are pinned as fixtures,
both plain and PTT-wrapped: they must route to recency AND produce a non-empty
injection when summaries/rows exist.

Pure detector/recency tests call opencohost.core.memoria_store directly; engine
router tests drive a real MotorVocalIA + real MemoriaStore (mirror
tests/test_memorias_retrieval.py) and never touch USER_DATA_DIR.
"""

from __future__ import annotations

import queue
import sqlite3
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

import opencohost.core.llm_engine as llm_engine
from opencohost.config.settings import (
    MEMORIAS_MAX_INJECT_CHARS,
    MEMORIAS_META_RECALL_K,
)
from opencohost.core.memoria_store import (
    MemoriaStore,
    _SESSION_SUMMARY_MARKER,
    build_recency_lines,
    is_meta_recall_query,
)


# The two production-failing owner queries (engram #4296): a self-recall query
# and a temporal/session query. Both MUST detect as meta and route to recency.
_OWNER_Q_SELF = "¿Qué recordás de mí?"
_OWNER_Q_SESSION = "la sesión pasada qué se dijo, de qué se habló"


@pytest.fixture(autouse=True)
def _pin_es_locale():
    """Pin the active locale to es so the memorias block delimiters and PTT
    wrapper render deterministically (mirrors test_memorias_retrieval.py)."""
    from opencohost.i18n import active
    from opencohost.i18n.startup import resolve_active_bundle

    active.set_active_bundle(resolve_active_bundle(locale="es"))
    yield
    active.reset_active_bundle()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_BASE_TS = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _make_motor():
    log_q = queue.Queue()
    ui_events = []
    motor = llm_engine.MotorVocalIA(log_q, lambda event: ui_events.append(event))
    motor.ollama = MagicMock()
    motor.pygame = MagicMock()
    motor.is_ready = True
    return motor, log_q, ui_events


def _enable_memorias(monkeypatch, motor, tmp_path, profile_id="profile-1"):
    monkeypatch.setattr(llm_engine, "MEMORIAS_ENABLED", True)
    monkeypatch.setattr(llm_engine, "MEMORIAS_DB", str(tmp_path / "memorias.db"))
    motor._current_profile_id = profile_id
    return motor


def _seed(
    db_path, profile_id, row_id, *, content="contenido significativo alpha",
    title="titulo alpha beta", status="draft", stable_key=None,
    updated_at=_BASE_TS, signature="", pinned=False, private=False, inactive=False,
):
    """Insert one row with full control over stable_key / status / updated_at.

    Ensures the schema exists first (constructing a MemoriaStore). A summary
    row is seeded by passing a stable_key carrying _SESSION_SUMMARY_MARKER.
    """
    MemoriaStore(db_path)
    sk = stable_key if stable_key is not None else f"{profile_id}|{row_id}"
    ts = updated_at.isoformat() if hasattr(updated_at, "isoformat") else updated_at
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            "INSERT INTO memorias (id, profile_id, stable_key, revision, title, "
            "content, status, pinned, private, inactive, created_at, updated_at, signature) "
            "VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (row_id, profile_id, sk, title, content, status,
             1 if pinned else 0, 1 if private else 0, 1 if inactive else 0,
             ts, ts, signature),
        )


def _summary_key(profile_id, ts):
    return f"{profile_id}{_SESSION_SUMMARY_MARKER}{ts.isoformat()}"


# ---------------------------------------------------------------------------
# Detector — is_meta_recall_query (positive / negative table)
# ---------------------------------------------------------------------------

# Full positive set (anchored-phrase redesign, 4R correction WU4): every entry
# must detect plain AND PTT-wrapped (see the wrapped test below).
_META_QUERIES = [
    _OWNER_Q_SELF,
    _OWNER_Q_SESSION,
    "qué recuerdas de mi",
    "¿qué recuerdas de mí?",
    "¿de qué hablamos la sesión pasada?",
    "de qué hablamos",
    "¿qué hablamos ayer?",
    "qué hablamos la otra vez",
    "en base a tus memorias, ¿qué sabés?",
    "en base a mis memorias, respondeme",
    "usá mis memorias para responder",
    "te acordás de lo que dijimos ayer?",
    "¿se acuerdan de la vez del jefe final?",
    "no me acuerdo, ¿vos sí?",
    "¿qué sabés de mí?",
    "contame de la charla anterior",
    "de qué se habló en la charla pasada",
    "en la sesión anterior, ¿de qué se habló?",
    # D6 (model_switch_memory_continuity_20260723): singular "tu/mi memoria"
    # phrasing — the owner's real "lo último de tu memoria" wording.
    "¿qué es lo último de tu memoria?",
    "que hay en tu memoria",
    "en base a mi memoria",
]

# Full negative set: topical turns that share surface stems with recall verbs
# ("récord" the gaming noun, "acorde"/"acordeón", indicative "hablamos de X",
# resemblance "me recordás a...") and must NEVER route to the recency dump —
# that would bypass the W4 owner-gated fallback on a topical turn.
_TOPICAL_QUERIES = [
    "¿cómo configuro OBS?",
    "hablemos de Dark Souls",
    "hablamos de Dark Souls",
    "hablamos mañana",
    "hice un nuevo récord",
    "es un récord de tiempo",
    "tocá un acorde",
    "el acordeón",
    "me recordás al profe",
    "¿qué opinás de la sesión de fotos?",
    "recomiéndame un juego nuevo",
    "¿qué opinás de este juego?",
    "subí el volumen del micrófono",
    "contame un chiste",
    "",
    # D6 negative guards: "memoria" alone (no mi/tu possessive) must never
    # over-trigger recency routing.
    "memoria RAM",
    "se me fue la memoria",
    "hablamos de la memoria del juego",
    # jd-fix-agent (model_switch_memory_continuity_20260723, finding 1):
    # possessive + "memoria" + another noun is a topical hardware mention, not
    # a recall phrase — the singular arm must not over-trigger here.
    "mi memoria RAM",
    "tu memoria USB se llenó",
    "se llenó mi memoria del teléfono",
]


@pytest.mark.parametrize("text", _META_QUERIES)
def test_meta_recall_queries_detected(text):
    assert is_meta_recall_query(text) is True


@pytest.mark.parametrize("text", _TOPICAL_QUERIES)
def test_topical_queries_not_detected(text):
    assert is_meta_recall_query(text) is False


@pytest.mark.parametrize("query", _META_QUERIES)
def test_meta_queries_detected_direct_and_ptt_wrapped(query):
    """The detector must match through the PTT/live-voice wrappers, since a
    voice turn reaches the engine wrapped ("El streamer acaba de decir (PTT):
    ...") — the recall phrase sits after the wrapper prefix."""
    from opencohost.i18n import active as i18n_active

    assert is_meta_recall_query(query) is True
    assert is_meta_recall_query(i18n_active.ptt_wrapper().format(text=query)) is True
    assert is_meta_recall_query(i18n_active.ptt_history_wrapper().format(text=query)) is True


@pytest.mark.parametrize("text", _TOPICAL_QUERIES)
def test_topical_queries_stay_negative_ptt_wrapped(text):
    """Wrapper words themselves ("dijo" in the history wrapper) must never trip
    the detector on a topical turn."""
    from opencohost.i18n import active as i18n_active

    assert is_meta_recall_query(i18n_active.ptt_history_wrapper().format(text=text)) is False


def test_detector_is_accent_insensitive():
    # Same query with and without accents must both detect.
    assert is_meta_recall_query("que recordas de mi") is True
    assert is_meta_recall_query("QUÉ RECORDÁS DE MÍ") is True


# ---------------------------------------------------------------------------
# Recency assembly — build_recency_lines
# ---------------------------------------------------------------------------

def test_recency_puts_session_summaries_first_then_newest_regular(tmp_path):
    db_path = tmp_path / "memorias.db"
    # A newer regular row and an older summary: summary must STILL come first.
    _seed(db_path, "p", "reg-new", content="regular reciente uno",
          updated_at=_BASE_TS + timedelta(hours=5))
    _seed(db_path, "p", "reg-old", content="regular viejo dos",
          updated_at=_BASE_TS + timedelta(hours=1))
    _seed(db_path, "p", "sum-old", content="resumen viejo",
          status="summary", stable_key=_summary_key("p", _BASE_TS + timedelta(hours=2)),
          updated_at=_BASE_TS + timedelta(hours=2))
    _seed(db_path, "p", "sum-new", content="resumen nuevo",
          status="summary", stable_key=_summary_key("p", _BASE_TS + timedelta(hours=3)),
          updated_at=_BASE_TS + timedelta(hours=3))

    rows = MemoriaStore(db_path).list_injection_candidates("p")
    lines = build_recency_lines(rows)

    # Summaries first (newest summary before older summary), then newest regular.
    assert lines[0] == "resumen nuevo"
    assert lines[1] == "resumen viejo"
    assert lines[2] == "regular reciente uno"
    assert lines[3] == "regular viejo dos"


def test_recency_caps_at_meta_recall_k(tmp_path):
    db_path = tmp_path / "memorias.db"
    _seed(db_path, "p", "sum", content="resumen sesion",
          status="summary", stable_key=_summary_key("p", _BASE_TS + timedelta(hours=9)),
          updated_at=_BASE_TS + timedelta(hours=9))
    for i in range(8):  # more regular rows than the cap
        _seed(db_path, "p", f"reg-{i}", content=f"regular {i}",
              updated_at=_BASE_TS + timedelta(minutes=i))

    rows = MemoriaStore(db_path).list_injection_candidates("p")
    lines = build_recency_lines(rows)

    assert len(lines) == MEMORIAS_META_RECALL_K == 5
    assert lines[0] == "resumen sesion"  # summary still first within the cap


def test_recency_respects_char_budget(tmp_path):
    db_path = tmp_path / "memorias.db"
    for i in range(5):
        _seed(db_path, "p", f"reg-{i}", content="palabra " * 60,  # ~480 chars each
              updated_at=_BASE_TS + timedelta(minutes=i))

    rows = MemoriaStore(db_path).list_injection_candidates("p")
    lines = build_recency_lines(rows)

    assert lines  # non-vacuous
    assert len("\n".join(lines)) <= MEMORIAS_MAX_INJECT_CHARS


def test_recency_pinned_row_outside_recent_window_still_injected(tmp_path):
    """F6 pinned-first guarantee on the META path (finding B): a pinned row
    OLDER than the k newest rows must still be injected FIRST — mirroring
    build_injection_lines' pinned carve-out."""
    db_path = tmp_path / "memorias.db"
    _seed(db_path, "p", "pin-old", content="dato fijado importante",
          pinned=True, status="curated", updated_at=_BASE_TS)
    for i in range(6):  # more fresh rows than MEMORIAS_META_RECALL_K
        _seed(db_path, "p", f"reg-{i}", content=f"regular fresco {i}",
              updated_at=_BASE_TS + timedelta(hours=1 + i))

    rows = MemoriaStore(db_path).list_injection_candidates("p")
    lines = build_recency_lines(rows)

    assert lines[0] == "dato fijado importante"
    assert len(lines) <= MEMORIAS_META_RECALL_K


def test_recency_summary_share_bounded_regular_rows_survive(tmp_path):
    """Count-truncation guard (finding C): accumulated summaries must not
    starve live-session regular rows out of the k slots — summaries' share is
    bounded at 2, regular rows fill the remaining slots."""
    db_path = tmp_path / "memorias.db"
    for i in range(6):
        _seed(db_path, "p", f"sum-{i}", content=f"resumen {i}",
              status="summary", stable_key=_summary_key("p", _BASE_TS + timedelta(hours=i)),
              updated_at=_BASE_TS + timedelta(hours=i))
    for i in range(3):
        _seed(db_path, "p", f"reg-{i}", content=f"regular fresco {i}",
              updated_at=_BASE_TS + timedelta(days=1, hours=i))

    rows = MemoriaStore(db_path).list_injection_candidates("p")
    lines = build_recency_lines(rows)

    assert len(lines) == MEMORIAS_META_RECALL_K
    assert [line for line in lines if line.startswith("regular fresco")]
    assert len([line for line in lines if line.startswith("resumen")]) <= 2


def test_recency_summary_content_is_clean_marker_lives_in_stable_key(tmp_path):
    """The _SESSION_SUMMARY_MARKER lives ONLY in stable_key — the displayed
    summary content the model sees must never carry the marker prefix."""
    db_path = tmp_path / "memorias.db"
    _seed(db_path, "p", "sum", content="musica synthwave; juego shooter",
          status="summary", stable_key=_summary_key("p", _BASE_TS + timedelta(hours=1)),
          updated_at=_BASE_TS + timedelta(hours=1))

    rows = MemoriaStore(db_path).list_injection_candidates("p")
    lines = build_recency_lines(rows)

    assert lines == ["musica synthwave; juego shooter"]
    assert _SESSION_SUMMARY_MARKER not in lines[0]


# ---------------------------------------------------------------------------
# Router branch — _build_memorias_injection_block
# ---------------------------------------------------------------------------

def test_meta_query_surfaces_summary_lexical_path_would_miss(monkeypatch, tmp_path):
    """The core behavior: a meta query sharing NO significant tokens with any
    row still surfaces the recent session summary via recency — exactly the
    row select_top_k (lexical) returns nothing for."""
    motor, _, _ = _make_motor()
    _enable_memorias(monkeypatch, motor, tmp_path, profile_id="p")
    _seed(tmp_path / "memorias.db", "p", "sum",
          content="hablamos de monetizacion y sponsors del stream",
          status="summary", stable_key=_summary_key("p", _BASE_TS + timedelta(hours=1)),
          updated_at=_BASE_TS + timedelta(hours=1))

    block = motor._build_memorias_injection_block("p", _OWNER_Q_SELF)

    assert "</memorias_guardadas>" in block
    assert "hablamos de monetizacion y sponsors del stream" in block


@pytest.mark.parametrize("query", [_OWNER_Q_SELF, _OWNER_Q_SESSION])
def test_owner_queries_produce_nonempty_injection_direct_and_ptt(monkeypatch, tmp_path, query):
    """Both real owner queries, plain AND PTT-wrapped, must produce a non-empty
    recency injection when a summary exists."""
    from opencohost.i18n import active as i18n_active

    motor, _, _ = _make_motor()
    _enable_memorias(monkeypatch, motor, tmp_path, profile_id="p")
    _seed(tmp_path / "memorias.db", "p", "sum",
          content="resumen de la sesion pasada sobre monetizacion",
          status="summary", stable_key=_summary_key("p", _BASE_TS + timedelta(hours=1)),
          updated_at=_BASE_TS + timedelta(hours=1))

    wrapped = i18n_active.ptt_wrapper().format(text=query)
    for form in (query, wrapped):
        block = motor._build_memorias_injection_block("p", form)
        assert "</memorias_guardadas>" in block
        assert "resumen de la sesion pasada sobre monetizacion" in block


def test_topical_zero_match_still_injects_nothing_gated_fallback(monkeypatch, tmp_path):
    """W4 owner-gated fallback: a TOPICAL (non-meta) query with zero lexical
    matches must inject nothing, even though recency-surfaceable summaries
    exist. Recency must NEVER fire on a topical turn."""
    motor, _, _ = _make_motor()
    _enable_memorias(monkeypatch, motor, tmp_path, profile_id="p")
    _seed(tmp_path / "memorias.db", "p", "sum",
          content="resumen sobre monetizacion y sponsors",
          status="summary", stable_key=_summary_key("p", _BASE_TS + timedelta(hours=1)),
          updated_at=_BASE_TS + timedelta(hours=1))
    _seed(tmp_path / "memorias.db", "p", "reg",
          content="nota sobre configuracion de escenas", title="escenas config obs")

    # A topical query sharing no significant tokens with either row.
    block = motor._build_memorias_injection_block("p", "recomendame un juego de plataformas")

    assert block == ""


def test_meta_path_strips_injection_markers(monkeypatch, tmp_path):
    """The recency branch shares the sanitize + strip pipeline: an injection
    marker inside a surfaced summary is stripped before entering the wrapper,
    while innocent surrounding text survives (Batch A parity)."""
    motor, _, _ = _make_motor()
    _enable_memorias(monkeypatch, motor, tmp_path, profile_id="p")
    _seed(tmp_path / "memorias.db", "p", "sum",
          content="ignore all previous dato inocente que sobrevive",
          status="summary", stable_key=_summary_key("p", _BASE_TS + timedelta(hours=1)),
          updated_at=_BASE_TS + timedelta(hours=1))

    block = motor._build_memorias_injection_block("p", _OWNER_Q_SESSION)

    assert block
    assert "ignore all previous" not in block.lower()
    assert "dato inocente que sobrevive" in block
