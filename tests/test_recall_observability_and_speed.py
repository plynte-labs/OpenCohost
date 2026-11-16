"""T4 (spoken-output-guard-and-measurement-20261109): recall observability,
fold speed-up equivalence, and the local-date episode label.

Synthetic data only. No decision (which memory is accepted or rejected) may
change; only how fast it is made, how it is logged and how the date is shown.
"""
from __future__ import annotations

import random
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from opencohost.core.memory import episodic_merge
from opencohost.core.memory_v5_shadow import episodic_recall
from opencohost.core.memory_v5_shadow.episodic_recall import (
    EpisodicRecallCoordinator,
    RecallMode,
)
from opencohost.core.memory_v5_shadow.query_analyzer import EpisodicQueryAnalyzer
from opencohost.core.memory_v5_shadow.semantic_cache import (
    EpisodeEmbeddingRecord,
    ExchangeEmbeddingRecord,
    SemanticCacheStore,
)
from opencohost.i18n import active as i18n_active
from opencohost.i18n.startup import resolve_active_bundle

DIM = 8


def _vec(*head):
    return list(head) + [0.0] * (DIM - len(head))


# ---------------------------------------------------------------------------
# A. Query analyzer: which family produced the intent (fixed codes only)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "text, intent, trigger",
    [
        ("¿Te acuerdas de mis audífonos?", "EXPLICIT", "legacy"),
        ("recordaste lo del cafe", "EXPLICIT", "recall_verb"),
        ("qué tal el clima en la ciudad", "NONE", "-"),
        ("dijimos algo del cafe", "EXPLICIT", "conversation_stem"),
        ("tengo memoria del cafe", "EXPLICIT", "memory_noun"),
    ],
)
def test_analyzer_exposes_trigger_family(text, intent, trigger):
    q = EpisodicQueryAnalyzer().analyze(text, profile_id="p")
    assert q.recall_intent.value == intent
    assert q.trigger == trigger


def test_trigger_is_always_a_short_fixed_code():
    allowed = re.compile(r"^[a-z_]+$|^-$")
    for text in ["hola", "¿Recuerdas lo de ayer?", "el dia 2 de septiembre", "what did we say"]:
        q = EpisodicQueryAnalyzer().analyze(text, profile_id="p")
        assert allowed.match(q.trigger), q.trigger


# ---------------------------------------------------------------------------
# A. Coordinator: best_reason belongs to the best-cosine candidate
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE evidence_journal (event_id TEXT PRIMARY KEY, profile_id TEXT, run_id TEXT,
  stream_sequence INTEGER, role TEXT, source TEXT, occurred_at TEXT, content TEXT,
  is_private INTEGER DEFAULT 0, created_at TEXT);
CREATE TABLE sessions (session_id TEXT PRIMARY KEY, run_id TEXT, profile_id TEXT, state TEXT,
  started_at TEXT, ended_at TEXT, opened_reason TEXT, closure_reason TEXT, event_count INTEGER DEFAULT 0);
CREATE TABLE episodes (episode_id TEXT PRIMARY KEY, profile_id TEXT, session_id TEXT, state TEXT,
  formation_policy_id TEXT, formation_policy_version TEXT, started_at TEXT, ended_at TEXT,
  opened_reason TEXT, closure_reason TEXT, event_count INTEGER DEFAULT 0);
CREATE TABLE episode_membership (episode_id TEXT, event_id TEXT, sequence_index INTEGER,
  PRIMARY KEY (episode_id, event_id));
"""


class _Worker:
    def __init__(self, vec):
        self._vec = vec

    def embed_query(self, text, timeout_s=0.5):
        return list(self._vec)


def _coordinator(tmp_path: Path, rows, query_vec):
    """rows: (exchange_key, vector, lexical_tokens) inserted in this order."""
    conn = sqlite3.connect(tmp_path / "shadow.db")
    conn.executescript(_SCHEMA)
    conn.commit()
    cache = SemanticCacheStore(tmp_path / "cache.db")
    cache.initialize()
    for key, vector, tokens in rows:
        cache.insert_exchange_embedding(
            ExchangeEmbeddingRecord(
                key, "p", "s1", "ep1", "h" + key, "m", "v1", DIM, vector,
                "2026-08-25T10:00:00Z", lexical_tokens=tokens,
            )
        )
    cache.insert_episode_embedding(
        EpisodeEmbeddingRecord("ep1", "p", "m", "v1", _vec(1.0), 0.9, 0.8, "fp", "2026-08-25T10:00:00Z")
    )
    return EpisodicRecallCoordinator(
        shadow_conn=conn, cache_store=cache, worker=_Worker(query_vec), mode=RecallMode.ACTIVE
    )


def test_rejection_reason_is_the_best_cosine_candidates_not_the_last_scored(tmp_path):
    # X (cos 0.6, no shared token) is the best candidate -> LEXICAL_UNSUBSTANTIATED.
    # Y (cos 0.1) is scored last -> LOW_SEMANTIC; the old code reported that one.
    rows = [
        ("x:x", _vec(0.6, 0.8), "zzz"),
        ("y:y", _vec(0.1, 0.995), "yyy"),
    ]
    coord = _coordinator(tmp_path, rows, _vec(1.0))
    packet = coord.process_query("¿Te acuerdas de los audífonos?", profile_id="p")
    assert packet.reason_code == "REJECTED_LEXICAL_UNSUBSTANTIATED"
    assert packet.best_reason == "REJECTED_LEXICAL_UNSUBSTANTIATED"
    assert packet.accepted_n == 0
    assert packet.intent == "EXPLICIT"
    assert packet.trigger == "legacy"
    assert packet.anchors >= 1


def test_accepted_candidates_are_counted_and_best_reason_is_the_accept_code(tmp_path):
    rows = [("x:x", _vec(1.0), "audifonos"), ("y:y", _vec(0.9, 0.43), "audifonos")]
    coord = _coordinator(tmp_path, rows, _vec(1.0))
    # No evidence events: expansion drops the stale candidate, but scoring ran.
    packet = coord.process_query("¿Te acuerdas de los audífonos?", profile_id="p")
    assert packet.accepted_n == 2
    assert packet.best_reason == "ACCEPTED_EXPLICIT_CORROBORATED"


def test_early_exit_packets_still_carry_intent_and_anchor_count(tmp_path):
    coord = _coordinator(tmp_path, [("x:x", _vec(1.0), "otro")], _vec(1.0))
    packet = coord.process_query("el clima de la ciudad es raro", profile_id="p")
    assert packet.reason_code == "NO_HISTORICAL_ANCHOR"
    assert packet.intent == "NONE"
    assert packet.accepted_n == 0
    assert packet.best_reason is None


# ---------------------------------------------------------------------------
# A. Telemetry line: appended fields, nothing removed or reordered
# ---------------------------------------------------------------------------

def _outcome(**kw):
    return episodic_merge.RecallOutcome(
        (), "REJECTED_LEXICAL_UNSUBSTANTIATED", "EPISODIC_TOPIC", 0.6, 540,
        episodic_merge.FALLBACK_NO_HIT, **kw,
    )


def test_telemetry_without_intent_is_byte_identical():
    line = episodic_merge.format_recall_telemetry(_outcome(), lexical=1, episodes=0)
    assert line.endswith("over_budget=0")
    assert "intent=" not in line


def test_telemetry_appends_new_fields_after_the_old_ones():
    old = episodic_merge.format_recall_telemetry(
        _outcome(embed_ms=7, worker_wait_ms=1, retrieval_ms=500, device="cpu"),
        lexical=1, episodes=0,
    )
    new = episodic_merge.format_recall_telemetry(
        _outcome(
            embed_ms=7, worker_wait_ms=1, retrieval_ms=500, device="cpu",
            intent="EXPLICIT", trigger="recall_verb", anchors=3, accepted_n=0,
            best_reason="REJECTED_LOW_SEMANTIC",
        ),
        lexical=1, episodes=0,
    )
    assert new.startswith(old)
    assert new.endswith(
        " intent=EXPLICIT trigger=recall_verb anchors=3 accepted_n=0 "
        "best_reason=REJECTED_LOW_SEMANTIC"
    )


def test_telemetry_fields_are_sanitised_codes():
    line = episodic_merge.format_recall_telemetry(
        _outcome(intent="exp licit; drop", trigger="Hola Mundo!", anchors=2,
                 accepted_n=1, best_reason=None),
        lexical=0, episodes=0,
    )
    tail = line.split(" intent=")[1]
    assert re.fullmatch(r"[A-Z_]+ trigger=[a-z_\-]+ anchors=\d+ accepted_n=\d+ best_reason=-", tail), tail


def test_decision_fields_flow_into_the_outcome():
    decision = SimpleNamespace(
        reason="NO_CANDIDATES", scope="EPISODIC_TOPIC", top_score=None, episodes=(),
        hit=False, intent="NONE", trigger="-", anchors=2, accepted_n=0, best_reason="-",
    )
    out = episodic_merge.resolve_recall_outcome(decision, 10, 400, 2)
    assert (out.intent, out.trigger, out.anchors, out.accepted_n, out.best_reason) == (
        "NONE", "-", 2, 0, "-",
    )


def test_diagnostics_parser_counts_decisions_by_intent():
    from opencohost.mcp_local import logparse

    lines = [
        "[10:00:03] [INFO] [OpenCohost] memory_recall: lexical=1 semantic=0 episodes=0 "
        "decision=NO_CANDIDATES scope=EPISODIC_TOPIC top_score=none ms=5 fallback=no_hit "
        "fact_fallback=none over_budget=0 intent=EXPLICIT trigger=recall_verb anchors=2 "
        "accepted_n=0 best_reason=-",
        "[10:00:04] [INFO] [OpenCohost] memory_recall: lexical=1 semantic=0 episodes=0 "
        "decision=NO_CANDIDATES scope=EPISODIC_TOPIC top_score=none ms=5 fallback=no_hit "
        "fact_fallback=none over_budget=0 intent=NONE trigger=- anchors=0 accepted_n=0 best_reason=-",
        "[10:00:05] [INFO] [OpenCohost] memory_recall: lexical=1 semantic=0 episodes=0 "
        "decision=NO_CANDIDATES scope=EPISODIC_TOPIC top_score=none ms=5 fallback=no_hit "
        "fact_fallback=none over_budget=0",
    ]
    summary = logparse.summarize(lines)
    assert summary["memory_recall"]["intent"] == {"EXPLICIT": 1, "NONE": 1}


# ---------------------------------------------------------------------------
# B. Folding: equivalent decisions, much less work
# ---------------------------------------------------------------------------

def _reference_fold(token: str) -> str:
    """The pre-change implementation, kept here as the oracle."""
    from opencohost.core.editorial.editorial_matching import _strip_accents

    return _strip_accents(token or "").lower()


def _reference_set(text: str) -> set[str]:
    out = {_reference_fold(t) for t in (text or "").split()}
    out.discard("")
    return out


_MIXED = [
    "audifonos sonos ace graves",
    "AUDÍFONOS Ñandú Café naïve",
    "hola, mundo! x-y 42 gta6",
    "日本語 привет مرحبا café Ünï",
    "",
    "   ",
    "İstanbul ǅ ß ﬁ ́ é",
]


@pytest.mark.parametrize("text", _MIXED)
def test_fold_token_set_equals_the_reference(text):
    assert episodic_recall._fold_token_set(text) == _reference_set(text)


@pytest.mark.parametrize("token", " ".join(_MIXED).split())
def test_fold_token_equals_the_reference(token):
    assert episodic_recall._fold_token(token) == _reference_fold(token)


def test_writer_tokens_are_already_folded():
    """The indexer stores `_significant_tokens(...)`: folding them is the identity."""
    from opencohost.core.memory.memoria_store import _significant_tokens

    toks = _significant_tokens("Mis AUDÍFONOS Sonos, el Ñandú y el café naïve; GTA6!")
    assert toks
    assert all(_reference_fold(t) == t for t in toks)


def test_preflight_overlap_matches_the_reference_over_a_mixed_corpus(tmp_path):
    rows = [(f"k{i}:a{i}", _vec(1.0), t) for i, t in enumerate(_MIXED)]
    coord = _coordinator(tmp_path, rows, _vec(1.0))
    ref_cached = set().union(*(_reference_set(t) for t in _MIXED))
    for anchors in (["cafe"], ["audifonos"], ["zzzz"], ["nandu"], ["ß"], ["istanbul"]):
        query = SimpleNamespace(lexical_anchors=anchors, profile_id="p")
        folded = {_reference_fold(a) for a in anchors} - {""}
        expected = bool(folded & ref_cached)
        assert coord._has_historical_anchor_overlap(query) is expected, anchors


def test_fold_benchmark_old_versus_new(capsys):
    """Report-only (no wall-clock assertion): ~1,000 exchanges, ~70,000 tokens."""
    rng = random.Random(7)
    vocab = [f"palabra{n}" for n in range(6000)]
    rows = [" ".join(rng.choice(vocab) for _ in range(70)) for _ in range(1000)]

    t0 = time.perf_counter()
    old_cached: set[str] = set()
    for row in rows:
        for tok in (row or "").split():
            folded = _reference_fold(tok)
            if folded:
                old_cached.add(folded)
    old_pre = time.perf_counter() - t0

    t0 = time.perf_counter()
    new_cached = episodic_recall._fold_token_set(" ".join(rows))
    new_pre = time.perf_counter() - t0
    assert new_cached == old_cached

    t0 = time.perf_counter()
    old_sets = [_reference_set(r) for r in rows]
    old_loop = time.perf_counter() - t0
    t0 = time.perf_counter()
    new_sets = [episodic_recall._fold_token_set(r) for r in rows]
    new_loop = time.perf_counter() - t0
    assert new_sets == old_sets

    with capsys.disabled():
        print(
            f"\n[fold-bench] preflight old={old_pre*1000:.1f}ms new={new_pre*1000:.1f}ms | "
            f"scoring loop old={old_loop*1000:.1f}ms new={new_loop*1000:.1f}ms"
        )


# ---------------------------------------------------------------------------
# C. Episode date label: local date + relative age from the locale bundle
# ---------------------------------------------------------------------------

UTC6 = timezone(timedelta(hours=-6))


@pytest.fixture(params=["es", "en"])
def locale(request):
    i18n_active.set_active_bundle(resolve_active_bundle(locale=request.param))
    yield request.param
    i18n_active.reset_active_bundle()


def _episode(started_at):
    ex = SimpleNamespace(
        user_text="my cat Pixel hates the vacuum cleaner at night",
        assistant_text="ignored", user_event_id="u1", assistant_event_id="a1",
    )
    return SimpleNamespace(started_at=started_at, exchanges=[ex], best_exchange_key="u1:a1")


def _label(started_at, now, tz=UTC6, limit=300):
    line = episodic_merge.format_episode_line(_episode(started_at), limit, now=now, tz=tz)
    return line[: line.index("]") + 1], line


def test_utc_evening_session_is_labelled_with_the_previous_local_day(locale):
    # 02:00Z on Nov 2 is 20:00 on Nov 1 at UTC-6.
    now = datetime(2026, 11, 3, 1, 0, tzinfo=timezone.utc)  # Nov 2 19:00 local
    label, line = _label("2026-11-02T02:00:00Z", now)
    assert label.startswith("[past chat 2026-11-01")
    assert "2026-11-02" not in label
    assert ("ayer" if locale == "es" else "yesterday") in label
    assert line.endswith("vacuum cleaner at night")


def test_relative_age_flips_across_the_local_midnight_not_the_utc_one(locale):
    started = "2026-11-02T02:00:00Z"  # Nov 1 local
    just_before = datetime(2026, 11, 3, 5, 59, tzinfo=timezone.utc)  # Nov 2 23:59 local
    just_after = datetime(2026, 11, 3, 6, 1, tzinfo=timezone.utc)  # Nov 3 00:01 local
    yest = "ayer" if locale == "es" else "yesterday"
    assert yest in _label(started, just_before)[0]
    days = "hace 2 días" if locale == "es" else "2 days ago"
    assert days in _label(started, just_after)[0]


def test_same_local_day_is_today(locale):
    now = datetime(2026, 11, 2, 2, 30, tzinfo=timezone.utc)  # Nov 1 20:30 local
    label, _ = _label("2026-11-02T02:00:00Z", now)
    assert ("hoy" if locale == "es" else "today") in label
    assert "2026-11-01" in label


@pytest.mark.parametrize(
    "days, es, en",
    [(5, "hace 5 días", "5 days ago"), (14, "hace 2 semanas", "2 weeks ago"),
     (70, "hace 2 meses", "2 months ago")],
)
def test_longer_ages(locale, days, es, en):
    started = datetime(2026, 9, 1, 15, 0, tzinfo=timezone.utc)
    now = started + timedelta(days=days)
    label, _ = _label(started.isoformat().replace("+00:00", "Z"), now)
    assert (es if locale == "es" else en) in label


def test_unparseable_start_falls_back_to_the_raw_date_without_an_age():
    line = episodic_merge.format_episode_line(
        _episode("not-a-date-at-all"), 300, now=datetime(2026, 11, 3, tzinfo=timezone.utc), tz=UTC6
    )
    assert line.startswith("[past chat not-a-date] ")


def test_line_stays_inside_the_limit_with_the_age_label(locale):
    now = datetime(2026, 11, 3, 1, 0, tzinfo=timezone.utc)
    ep = _episode("2026-11-02T02:00:00Z")
    ep.exchanges[0].user_text = "word " * 200
    line = episodic_merge.format_episode_line(ep, 120, now=now, tz=UTC6)
    assert 0 < len(line) <= 120


def test_locale_bundles_define_the_same_age_keys():
    for code in ("es", "en"):
        i18n_active.set_active_bundle(resolve_active_bundle(locale=code))
        try:
            labels = i18n_active.episode_age_labels()
        finally:
            i18n_active.reset_active_bundle()
        assert set(labels) == {"today", "yesterday", "days", "weeks", "months"}
        assert "{n}" in labels["days"] and "{n}" in labels["weeks"] and "{n}" in labels["months"]
