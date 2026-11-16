"""T2 memory-semantic-recall: episodic recall merged with lexical memorias.

Contract (ADR-059, supersedes ADR-034 for the prompt-assembly path):
- lexical fact lines come first, then at most MEMORY_RECALL_MAX_EPISODES (2)
  compact episode lines, all inside MEMORIAS_MAX_INJECT_CHARS (700);
- episodes are trimmed or skipped to fit, facts are never cut for them;
- recall has a latency budget (MEMORY_RECALL_BUDGET_MS): over budget, a worker
  timeout or an exception falls back to the lexical-only prompt, byte-identical;
- v5 OFF / no subsystem keeps today's lexical-only prompt byte-identical;
- one `memory_recall:` INFO line per attempt on the OpenCohost logger, numbers
  and codes only.

Fixture text uses distinctive `zq*` tokens so the log-content assertions are
meaningful. Temp DBs only; the coordinator/worker are fakes.
"""

from __future__ import annotations

import logging
import queue
import re
import sqlite3
import time
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

import opencohost.core.llm_engine as llm_engine
from opencohost.config import settings
from opencohost.core.memory.memoria_store import MemoriaStore, _significant_tokens
from opencohost.core.memory_v5_shadow.episodic_recall import (
    EpisodicRecallCoordinator,
    EpisodicRecallPacket,
    RecallMode,
    RetrievedEpisodeContext,
)
from opencohost.core.memory_v5_shadow.query_analyzer import RecallScope
from opencohost.core.memory_v5_shadow.retrieval import CandidateExchange
from opencohost.core.memory_v5_shadow.semantic import ConversationalExchange
from opencohost.core.memory_v5_shadow.subsystem import MemorySubsystem
from opencohost.i18n import active as i18n_active

PROFILE = "profile-1"
QUERY = "zqorbit zqnebula zqquasar telescopio"
FACT_PINNED = "zqpinfact el streamer usa un telescopio dobsoniano de ocho pulgadas"
FACT_LEXICAL = "zqlexfact zqorbit zqnebula zqquasar el streamer fotografia nebulosas"
EP_USER = "zqepuser me compre un filtro de hidrogeno alfa para el telescopio"
EP_ASST = "zqepasst un filtro h-alfa te va a mostrar las protuberancias solares"

_TELEMETRY_RE = re.compile(
    r"^memory_recall: lexical=\d+ semantic=\d+ episodes=\d+ decision=[A-Z_]+ scope=[A-Z_]+ "
    r"top_score=(?:\d+\.\d{3}|none) ms=\d+ "
    r"fallback=(?:none|timeout|error|disabled|no_hit|no_room) "
    r"fact_fallback=(?:none|disabled|cold|timeout|error|skipped) over_budget=[01]$"
)


@pytest.fixture(autouse=True)
def _pin_es_locale():
    from opencohost.i18n.startup import resolve_active_bundle

    i18n_active.set_active_bundle(resolve_active_bundle(locale="es"))
    yield
    i18n_active.reset_active_bundle()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _seed(db_path, rows):
    MemoriaStore(db_path)
    ts = datetime(2026, 1, 1, tzinfo=timezone.utc).isoformat()
    with sqlite3.connect(str(db_path)) as conn:
        conn.executemany(
            "INSERT INTO memorias (id, profile_id, stable_key, revision, title, "
            "content, status, pinned, private, inactive, created_at, updated_at, signature) "
            "VALUES (?, ?, ?, 1, ?, ?, 'curated', ?, 0, 0, ?, ?, ?)",
            [
                (r["id"], PROFILE, f"{PROFILE}|{r['id']}", "titulo", r["content"],
                 1 if r.get("pinned") else 0, ts, ts, r.get("signature", ""))
                for r in rows
            ],
        )


def _lexical_signature() -> str:
    return " ".join(_significant_tokens(QUERY))


def _make_motor(monkeypatch, tmp_path, rows):
    monkeypatch.setattr(llm_engine, "MEMORIAS_ENABLED", True)
    db = tmp_path / "memorias.db"
    monkeypatch.setattr(llm_engine, "MEMORIAS_DB", str(db))
    _seed(db, rows)
    motor = llm_engine.MotorVocalIA(queue.Queue(), lambda e: None)
    motor.ollama = MagicMock()
    motor.pygame = MagicMock()
    motor._current_profile_id = PROFILE
    motor._memory = None
    return motor


def _episode(eid, user=EP_USER, asst=EP_ASST, started="2026-09-02T10:00:00Z"):
    ex = ConversationalExchange(
        exchange_ordinal=1, session_id="s1",
        user_event_id=f"{eid}u", assistant_event_id=f"{eid}a",
        user_text=user, assistant_text=asst,
        combined_text=f"{user}\n{asst}", occurred_at=started,
    )
    return RetrievedEpisodeContext(
        episode_id=eid, session_id="s1", started_at=started,
        composite_score=0.9, exchanges=[ex],
    )


class FakeCoordinator:
    def __init__(self, episodes=(), reason="ACCEPTED_EXPLICIT_CORROBORATED",
                 scope=RecallScope.EPISODIC_TOPIC, delay_s=0.0, raises=None,
                 top_score=0.912):
        self.episodes = list(episodes)
        self.reason = reason
        self.scope = scope
        self.delay_s = delay_s
        self.raises = raises
        self.top_score = top_score
        self.embed_timeouts = []

    def process_query(self, query_text, profile_id, reference_time=None,
                      embed_timeout_s=None):
        self.embed_timeouts.append(embed_timeout_s)
        if self.delay_s:
            time.sleep(self.delay_s)
        if self.raises is not None:
            raise self.raises
        block = "<episodic_memory>zqfullblock</episodic_memory>" if self.episodes else ""
        return EpisodicRecallPacket(
            query_hash="h", profile_id=profile_id, mode=RecallMode.ACTIVE,
            retrieved_episodes=self.episodes, formatted_block=block,
            token_estimate=len(block) // 4, reason_code=self.reason,
            scope=self.scope, candidate_count=len(self.episodes),
            top_score=self.top_score,
        )


def _prompt(motor, query=QUERY):
    setup = motor._build_generation_request(
        query, source="direct", is_local=True, provider_cfg={},
        request_model="m", watchdog_timeout=10.0,
    )
    return setup.messages[-1]["content"]


def _block_lines(prompt):
    open_tag = i18n_active.memorias_block_open()
    close_tag = i18n_active.memorias_block_close()
    if open_tag not in prompt:
        return []
    inner = prompt.split(open_tag + "\n", 1)[1].split("\n" + close_tag, 1)[0]
    return inner.split("\n")


def _recall_records(caplog):
    return [
        r for r in caplog.records
        if r.name == "OpenCohost" and r.getMessage().startswith("memory_recall:")
    ]


def _lexical_only_prompt(monkeypatch, tmp_path, rows):
    motor = _make_motor(monkeypatch, tmp_path / "base", rows)
    return _prompt(motor)


# ---------------------------------------------------------------------------
# Merge order, cap and budget
# ---------------------------------------------------------------------------

def test_merge_puts_facts_first_then_episodes(monkeypatch, tmp_path):
    rows = [{"id": "pin", "content": FACT_PINNED, "pinned": True}]
    motor = _make_motor(monkeypatch, tmp_path, rows)
    motor._memory = MemorySubsystem(
        mode="ACTIVE", coordinator=FakeCoordinator([_episode("ep1"), _episode("ep2")]))

    lines = _block_lines(_prompt(motor))

    assert lines[0] == FACT_PINNED
    assert len(lines) == 3
    # S2: user side only; the assistant text is never re-injected.
    assert all("zqepuser" in line and "zqepasst" not in line for line in lines[1:])
    # The full coordinator block is no longer injected on its own.
    assert "zqfullblock" not in _prompt(motor)


def test_at_most_two_episodes(monkeypatch, tmp_path):
    motor = _make_motor(monkeypatch, tmp_path, [{"id": "pin", "content": FACT_PINNED, "pinned": True}])
    motor._memory = MemorySubsystem(
        mode="ACTIVE",
        coordinator=FakeCoordinator([_episode("ep1"), _episode("ep2"), _episode("ep3")]))

    lines = _block_lines(_prompt(motor))

    assert sum(1 for line in lines if "zqepuser" in line) == 2


def test_episodes_only_when_no_fact_matches(monkeypatch, tmp_path):
    motor = _make_motor(monkeypatch, tmp_path, [{"id": "other", "content": "zqunrelated"}])
    motor._memory = MemorySubsystem(mode="ACTIVE", coordinator=FakeCoordinator([_episode("ep1")]))

    lines = _block_lines(_prompt(motor))

    assert len(lines) == 1 and "zqepuser" in lines[0]


@pytest.mark.parametrize("fact_len", [60, 300, 500, 650, 699])
def test_budget_never_exceeded(monkeypatch, tmp_path, fact_len):
    long_user = "zqepuser " + "palabra " * 200
    long_asst = "zqepasst " + "respuesta " * 200
    rows = [{"id": "lex", "content": ("zqlexfact " * 100)[:fact_len],
             "signature": _lexical_signature()}]
    motor = _make_motor(monkeypatch, tmp_path, rows)
    motor._memory = MemorySubsystem(
        mode="ACTIVE",
        coordinator=FakeCoordinator([_episode("e1", long_user, long_asst),
                                     _episode("e2", long_user, long_asst)]))

    lines = _block_lines(_prompt(motor))

    assert len("\n".join(lines)) <= settings.MEMORIAS_MAX_INJECT_CHARS


def test_episodes_dropped_before_facts(monkeypatch, tmp_path):
    rows = [
        {"id": "pin1", "content": "zqpinone " + "a" * 400, "pinned": True},
        {"id": "pin2", "content": "zqpintwo " + "b" * 400, "pinned": True},
        {"id": "lex", "content": FACT_LEXICAL + " " + "c" * 400,
         "signature": _lexical_signature()},
    ]
    baseline = _block_lines(_lexical_only_prompt(monkeypatch, tmp_path, rows))
    motor = _make_motor(monkeypatch, tmp_path, rows)
    motor._memory = MemorySubsystem(mode="ACTIVE", coordinator=FakeCoordinator([_episode("ep1")]))

    lines = _block_lines(_prompt(motor))

    assert len(baseline) == 3
    assert lines == baseline  # facts fill the budget: no episode, no fact cut


def test_episode_trimmed_into_remaining_budget(monkeypatch, tmp_path):
    rows = [
        {"id": "pin1", "content": "zqpinone " + "a" * 400, "pinned": True},
        {"id": "pin2", "content": "zqpintwo " + "b" * 400, "pinned": True},
    ]
    baseline = _block_lines(_lexical_only_prompt(monkeypatch, tmp_path, rows))
    motor = _make_motor(monkeypatch, tmp_path, rows)
    long_asst = "zqepasst " + "respuesta " * 100
    motor._memory = MemorySubsystem(
        mode="ACTIVE", coordinator=FakeCoordinator([_episode("ep1", EP_USER, long_asst)]))

    lines = _block_lines(_prompt(motor))

    assert lines[:2] == baseline
    assert len(lines) == 3 and "zqepuser" in lines[2] and "zqepasst" not in lines[2]
    assert len("\n".join(lines)) <= settings.MEMORIAS_MAX_INJECT_CHARS


# ---------------------------------------------------------------------------
# Fallbacks: byte-identical lexical-only prompt
# ---------------------------------------------------------------------------

_ROWS = [
    {"id": "pin", "content": FACT_PINNED, "pinned": True},
    {"id": "lex", "content": FACT_LEXICAL, "signature": _lexical_signature()},
]


def test_slow_hit_over_budget_is_still_injected(monkeypatch, tmp_path, caplog):
    # memory-write-quality T3: the budget caps the IPC wait, it is not a post-hoc discard.
    baseline = _lexical_only_prompt(monkeypatch, tmp_path, _ROWS)
    monkeypatch.setattr(settings, "MEMORY_RECALL_BUDGET_MS", 50)
    motor = _make_motor(monkeypatch, tmp_path, _ROWS)
    coord = FakeCoordinator([_episode("ep1")], delay_s=0.15)
    motor._memory = MemorySubsystem(mode="ACTIVE", coordinator=coord)

    caplog.clear()  # drop the baseline run's records
    with caplog.at_level(logging.INFO, logger="OpenCohost"):
        prompt = _prompt(motor)

    assert prompt != baseline
    assert coord.embed_timeouts == [pytest.approx(0.05)]
    (rec,) = _recall_records(caplog)
    assert "fallback=none" in rec.getMessage()
    assert "episodes=1" in rec.getMessage()
    assert "over_budget=1" in rec.getMessage()


def test_worker_timeout_reason_maps_to_timeout(monkeypatch, tmp_path, caplog):
    baseline = _lexical_only_prompt(monkeypatch, tmp_path, _ROWS)
    motor = _make_motor(monkeypatch, tmp_path, _ROWS)
    motor._memory = MemorySubsystem(
        mode="ACTIVE",
        coordinator=FakeCoordinator([], reason="WORKER_UNAVAILABLE", top_score=None))

    caplog.clear()  # drop the baseline run's records
    with caplog.at_level(logging.INFO, logger="OpenCohost"):
        prompt = _prompt(motor)

    assert prompt == baseline
    (rec,) = _recall_records(caplog)
    assert "fallback=timeout" in rec.getMessage()
    assert "top_score=none" in rec.getMessage()


def test_coordinator_exception_falls_back(monkeypatch, tmp_path, caplog):
    baseline = _lexical_only_prompt(monkeypatch, tmp_path, _ROWS)
    motor = _make_motor(monkeypatch, tmp_path, _ROWS)
    motor._memory = MemorySubsystem(
        mode="ACTIVE", coordinator=FakeCoordinator(raises=RuntimeError("zqboom")))

    caplog.clear()  # drop the baseline run's records
    with caplog.at_level(logging.INFO, logger="OpenCohost"):
        prompt = _prompt(motor)

    assert prompt == baseline
    (rec,) = _recall_records(caplog)
    assert "fallback=error" in rec.getMessage()


def test_subsystem_exception_falls_back(monkeypatch, tmp_path, caplog):
    class Boom:
        def recall_decision(self, *a, **k):
            raise RuntimeError("zqboom")

    baseline = _lexical_only_prompt(monkeypatch, tmp_path, _ROWS)
    motor = _make_motor(monkeypatch, tmp_path, _ROWS)
    motor._memory = Boom()

    caplog.clear()  # drop the baseline run's records
    with caplog.at_level(logging.INFO, logger="OpenCohost"):
        prompt = _prompt(motor)

    assert prompt == baseline
    (rec,) = _recall_records(caplog)
    assert "fallback=error" in rec.getMessage()


def test_no_hit_keeps_lexical_only(monkeypatch, tmp_path, caplog):
    baseline = _lexical_only_prompt(monkeypatch, tmp_path, _ROWS)
    motor = _make_motor(monkeypatch, tmp_path, _ROWS)
    motor._memory = MemorySubsystem(
        mode="ACTIVE",
        coordinator=FakeCoordinator([], reason="REJECTED_LOW_SEMANTIC", top_score=0.612))

    caplog.clear()  # drop the baseline run's records
    with caplog.at_level(logging.INFO, logger="OpenCohost"):
        prompt = _prompt(motor)

    assert prompt == baseline
    (rec,) = _recall_records(caplog)
    msg = rec.getMessage()
    assert "fallback=no_hit" in msg
    assert "decision=REJECTED_LOW_SEMANTIC" in msg
    assert "top_score=0.612" in msg


def test_v5_off_subsystem_is_byte_identical(monkeypatch, tmp_path, caplog):
    baseline = _lexical_only_prompt(monkeypatch, tmp_path, _ROWS)
    motor = _make_motor(monkeypatch, tmp_path, _ROWS)
    motor._memory = MemorySubsystem(mode="OFF")

    caplog.clear()  # drop the baseline run's records
    with caplog.at_level(logging.INFO, logger="OpenCohost"):
        prompt = _prompt(motor)

    assert prompt == baseline
    (rec,) = _recall_records(caplog)
    assert "fallback=disabled" in rec.getMessage()


def test_no_subsystem_calls_builder_with_legacy_signature(monkeypatch, tmp_path, caplog):
    motor = _make_motor(monkeypatch, tmp_path, _ROWS)
    real = motor._build_memorias_injection_block
    calls = []

    def spy(*args, **kwargs):
        calls.append((args, kwargs))
        return real(*args, **kwargs)

    monkeypatch.setattr(motor, "_build_memorias_injection_block", spy)
    caplog.clear()  # drop the baseline run's records
    with caplog.at_level(logging.INFO, logger="OpenCohost"):
        prompt = _prompt(motor)

    # Legacy positional shape (profile_id, contexto) is preserved; the only
    # extra argument allowed is the locale-isolation ``lang`` kwarg.
    assert calls and all(
        set(kwargs) <= {"lang"} and len(args) == 2 for args, kwargs in calls
    )
    assert FACT_PINNED in prompt
    (rec,) = _recall_records(caplog)
    assert "fallback=disabled" in rec.getMessage()
    assert "lexical=2" in rec.getMessage()


# ---------------------------------------------------------------------------
# Telemetry: one line per attempt, numbers and codes only
# ---------------------------------------------------------------------------

def test_telemetry_one_line_per_attempt_and_no_content(monkeypatch, tmp_path, caplog):
    motor = _make_motor(monkeypatch, tmp_path, _ROWS)
    motor._memory = MemorySubsystem(mode="ACTIVE", coordinator=FakeCoordinator([_episode("ep1")]))

    with caplog.at_level(logging.DEBUG):
        _prompt(motor)
        _prompt(motor)

    recs = _recall_records(caplog)
    assert len(recs) == 2
    for rec in recs:
        msg = rec.getMessage()
        assert rec.levelno == logging.INFO
        assert _TELEMETRY_RE.match(msg), msg
        assert "lexical=2" in msg and "episodes=1" in msg and "fallback=none" in msg
        assert "top_score=0.912" in msg
        assert "zq" not in msg
    for rec in caplog.records:
        text = rec.getMessage()
        for needle in ("zqpinfact", "zqlexfact", "zqepuser", "zqepasst", "zqfullblock"):
            assert needle not in text, (rec.name, needle)


# ---------------------------------------------------------------------------
# T3 (web-trigger-memory-relevance): weak lexical-only gate + correction turns
# ---------------------------------------------------------------------------

# Two shared significant tokens with QUERY_WEAK: enough for the legacy lexical
# gate (>= 2), NOT enough once the semantic side was rejected or timed out.
QUERY_WEAK = "zqorbit zqnebula pregunta"
FACT_WEAK = "zqweakfact zqorbit zqnebula el streamer habla de otra cosa"
_WEAK_ROWS = [
    {"id": "weak", "content": FACT_WEAK, "signature": "zqorbit zqnebula zqother"},
]
_WEAK_PINNED_ROWS = _WEAK_ROWS + [
    {"id": "pin", "content": FACT_PINNED, "pinned": True},
]


def _rejected_motor(monkeypatch, tmp_path, rows):
    motor = _make_motor(monkeypatch, tmp_path, rows)
    motor._memory = MemorySubsystem(
        mode="ACTIVE",
        coordinator=FakeCoordinator([], reason="REJECTED_LOW_SEMANTIC", top_score=0.46),
    )
    return motor


def test_weak_lexical_hit_not_injected_when_semantic_rejected(monkeypatch, tmp_path, caplog):
    motor = _rejected_motor(monkeypatch, tmp_path, _WEAK_ROWS)
    with caplog.at_level(logging.INFO, logger="OpenCohost"):
        prompt = _prompt(motor, QUERY_WEAK)
    assert "zqweakfact" not in prompt
    assert _block_lines(prompt) == []
    (rec,) = _recall_records(caplog)
    msg = rec.getMessage()
    assert "lexical=0" in msg
    assert "inject=skipped reason=lexical_only_weak" in msg
    assert "zq" not in msg


def test_weak_lexical_hit_not_injected_when_semantic_timed_out(monkeypatch, tmp_path, caplog):
    monkeypatch.setattr(settings, "MEMORY_RECALL_BUDGET_MS", 50)
    motor = _make_motor(monkeypatch, tmp_path, _WEAK_ROWS)
    motor._memory = MemorySubsystem(
        mode="ACTIVE",
        coordinator=FakeCoordinator([], reason="WORKER_UNAVAILABLE", top_score=None))
    with caplog.at_level(logging.INFO, logger="OpenCohost"):
        prompt = _prompt(motor, QUERY_WEAK)
    assert "zqweakfact" not in prompt
    (rec,) = _recall_records(caplog)
    assert "fallback=timeout" in rec.getMessage()
    assert "inject=skipped reason=lexical_only_weak" in rec.getMessage()


def test_strong_lexical_hit_still_injected_when_semantic_rejected(monkeypatch, tmp_path):
    # FACT_LEXICAL shares 4 significant tokens with QUERY (>= 3): strong.
    motor = _rejected_motor(monkeypatch, tmp_path, _ROWS)
    lines = _block_lines(_prompt(motor))
    assert any("zqlexfact" in line for line in lines)


def test_pinned_rows_survive_the_weak_lexical_gate(monkeypatch, tmp_path):
    motor = _rejected_motor(monkeypatch, tmp_path, _WEAK_PINNED_ROWS)
    lines = _block_lines(_prompt(motor, QUERY_WEAK))
    assert any("zqpinfact" in line for line in lines)
    assert not any("zqweakfact" in line for line in lines)


def test_weak_lexical_hit_still_injected_when_v5_off(monkeypatch, tmp_path, caplog):
    # No recall subsystem: nothing was rejected, the legacy gate (>= 2) stands.
    motor = _make_motor(monkeypatch, tmp_path, _WEAK_ROWS)
    lines = _block_lines(_prompt(motor, QUERY_WEAK))
    assert any("zqweakfact" in line for line in lines)


def test_weak_lexical_hit_still_injected_when_semantic_accepted(monkeypatch, tmp_path):
    motor = _make_motor(monkeypatch, tmp_path, _WEAK_ROWS)
    motor._memory = MemorySubsystem(
        mode="ACTIVE", coordinator=FakeCoordinator([_episode("ep1")]))
    lines = _block_lines(_prompt(motor, QUERY_WEAK))
    assert any("zqweakfact" in line for line in lines)


def test_correction_turn_skips_recall_and_injection(monkeypatch, tmp_path, caplog):
    motor = _make_motor(monkeypatch, tmp_path, _WEAK_PINNED_ROWS)
    coord = FakeCoordinator([_episode("ep1")])
    motor._memory = MemorySubsystem(mode="ACTIVE", coordinator=coord)
    with caplog.at_level(logging.INFO, logger="OpenCohost"):
        prompt = _prompt(motor, "no, no tiene sentido lo que dijiste")
    assert _block_lines(prompt) == []
    assert "zqpinfact" not in prompt and "zqepuser" not in prompt
    assert coord.embed_timeouts == []  # no worker call was spent
    (rec,) = _recall_records(caplog)
    msg = rec.getMessage()
    assert "inject=skipped reason=correction_turn" in msg
    assert "lexical=0" in msg and "episodes=0" in msg
    assert "sentido" not in msg


def test_long_topical_sentence_with_correction_words_still_recalls(monkeypatch, tmp_path):
    motor = _make_motor(monkeypatch, tmp_path, _ROWS)
    motor._memory = MemorySubsystem(
        mode="ACTIVE", coordinator=FakeCoordinator([_episode("ep1")]))
    query = "no tiene sentido " + QUERY + " ayer en el stream con mi hermano"
    lines = _block_lines(_prompt(motor, query))
    assert any("zqlexfact" in line for line in lines)


# ---------------------------------------------------------------------------
# Coordinator: single oversize exchange trim (latent AttributeError)
# ---------------------------------------------------------------------------

def test_coordinator_trims_single_oversize_exchange_without_raising():
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE evidence_journal (event_id TEXT, profile_id TEXT, run_id TEXT,
            stream_sequence INTEGER, occurred_at TEXT, role TEXT, content TEXT);
        CREATE TABLE episodes (episode_id TEXT, session_id TEXT, started_at TEXT);
        CREATE TABLE episode_membership (episode_id TEXT, event_id TEXT, sequence_index INTEGER);
        """
    )
    conn.execute("INSERT INTO episodes VALUES ('ep1', 's1', '2026-09-02T10:00:00Z')")
    conn.execute("INSERT INTO evidence_journal VALUES ('u1','p1','r1',1,'2026-09-02T10:00:00Z',"
                 "'user','te acuerdas de los audifonos sonos')")
    conn.execute("INSERT INTO evidence_journal VALUES ('a1','p1','r1',2,'2026-09-02T10:00:01Z',"
                 "'assistant',?)", ("sonos " * 1000,))
    conn.execute("INSERT INTO episode_membership VALUES ('ep1','u1',0)")
    conn.execute("INSERT INTO episode_membership VALUES ('ep1','a1',1)")

    vec = [1.0] + [0.0] * 383

    class Worker:
        def embed_query(self, text, timeout_s=0.5):
            return vec

    class Cache:
        def get_exchange_embeddings_by_profile(self, profile_id):
            return []

        def get_episode_embeddings_by_profile(self, profile_id):
            return []

    class Retriever:
        def get_candidates(self, query, query_vector, reference_time=None):
            return [CandidateExchange("u1:a1", "p1", "s1", "ep1", "2026-09-02T10:00:00Z",
                                      vec, "audifonos sonos")]

    coord = EpisodicRecallCoordinator(conn, Cache(), Worker(), mode=RecallMode.ACTIVE,
                                      max_token_budget=300)
    coord.retriever = Retriever()

    packet = coord.process_query("¿te acuerdas de los audifonos sonos?", profile_id="p1")

    assert packet is not None
    assert len(packet.retrieved_episodes) == 1
    assert packet.token_estimate <= 300
    assert packet.top_score == pytest.approx(1.0, abs=1e-3)
