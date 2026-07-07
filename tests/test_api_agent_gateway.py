"""Strict-TDD tests for the agent gateway surface (Phase 2 of track
agent_context_gateway_20260705, design.md 'Endpoint contracts (/api/agent/*)'
and 'Rate limit').

Owner decision D1 binds this suite: agents NEVER reach the agenda queue.
POST /api/agent/topics maps 1:1 to ``TopicInboxStore.propose`` — proposals
land in the human-gated topic inbox and the operator approves in the app UI.
POST /api/agenda/topic (auto-approve + queue) stays operator-token-only.

Covers:
- POST /api/agent/topics: propose creates an inbox row with source = agent
  name; repeat propose slug-dedupes (deduped=true, same row updated — the
  store's dedupe-upsert IS the idempotency, no Idempotency-Key); store
  validation errors surface as 422 with the store's own message; the 31st
  pending proposal (PENDING_CAP) is 429.
- GET /api/agent/status: read-only counts {topics_pending, cards,
  notices_undismissed} with any valid token; notices_undismissed is pinned
  to 0 until Phase 3 lands the notice store (key must already be present).
- auth.RateLimiter: fixed 60-second window, 60 mutating requests per token
  (stdlib Lock + dict, mirrors dispatch.py's Dispatcher); the 61st mutating
  call in one window is 429 and a window reset re-allows; GET is never
  counted.
- D1 literal assertions: the agent surface exposes EXACTLY topics + status,
  and a proposal never appears in the agenda queue.

tests/conftest.py redirects ``settings.API_TOKENS_FILE`` to a per-test temp
path (autouse); this module does the same for main.py's EDITORIAL_CARDS_DB
so topic-inbox writes never touch a real cards.db.
"""

import sqlite3
from contextlib import closing

import pytest
from fastapi.testclient import TestClient

import opencohost.api.auth as auth
import opencohost.api.main as main_mod
from opencohost.core.topic_inbox import PENDING_CAP, SOURCE_MAX, TopicInboxStore
from opencohost.smart_aggregator.kira_agenda_controller import KiraAgendaController
from tests.test_api_phase1 import FakeHost

_DEFAULT_TEST_ORIGINS = ["http://localhost:5173"]


class _FakeClock:
    """Injectable clock for RateLimiter's ``time_fn`` seam."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


@pytest.fixture(autouse=True)
def _reset_host_active():
    main_mod._host_active = False
    yield
    main_mod._host_active = False


@pytest.fixture(autouse=True)
def _isolate_cards_db(tmp_path, monkeypatch):
    """Redirect the handlers' inbox/cards DB to a per-test temp file.

    main.py imports EDITORIAL_CARDS_DB by name, so the module attribute is
    the value every handler resolves at call time (same pattern as
    tests/test_api_reads.py).
    """
    monkeypatch.setattr(main_mod, "EDITORIAL_CARDS_DB", str(tmp_path / "cards.db"))


@pytest.fixture(autouse=True)
def _fresh_rate_limiter(monkeypatch):
    """Per-test limiter so one test's window never bleeds into the next."""
    monkeypatch.setattr(auth, "_rate_limiter", auth.RateLimiter())


@pytest.fixture
def agent_headers():
    auth.ensure_tokens()
    return {"Authorization": f"Bearer {auth.load_tokens()['agent']}"}


@pytest.fixture
def operator_headers():
    auth.ensure_tokens()
    return {"Authorization": f"Bearer {auth.load_tokens()['operator']}"}


def _app(host_factory=FakeHost):
    return main_mod.create_app(
        host_factory=host_factory, cors_origins=_DEFAULT_TEST_ORIGINS
    )


def _store() -> TopicInboxStore:
    return TopicInboxStore(main_mod.EDITORIAL_CARDS_DB)


# ──────────────────────────────────────────────────────────────────────────
# POST /api/agent/topics — 1:1 mapping onto TopicInboxStore.propose
# ──────────────────────────────────────────────────────────────────────────


class TestAgentTopicPropose:
    def test_propose_creates_inbox_row_with_agent_source(self, agent_headers):
        app = _app()
        with TestClient(app) as client:
            resp = client.post(
                "/api/agent/topics",
                json={
                    "agent": "claude-code",
                    "title": "Quantum computing hits the desktop",
                    "angle": "what changes for hobbyists",
                    "tags": ["tech"],
                },
                headers=agent_headers,
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"].startswith("ti_")
        assert body["status"] == "proposed"
        assert body["deduped"] is False
        pending = _store().list_pending()
        assert len(pending["valid"]) == 1
        row = pending["valid"][0]
        assert row["source"] == "claude-code"
        assert row["title"] == "Quantum computing hits the desktop"
        assert row["angle"] == "what changes for hobbyists"
        assert row["tags"] == ["tech"]

    def test_angle_and_tags_are_optional(self, agent_headers):
        app = _app()
        with TestClient(app) as client:
            resp = client.post(
                "/api/agent/topics",
                json={"agent": "claude-code", "title": "Only a title here"},
                headers=agent_headers,
            )
        assert resp.status_code == 200
        row = _store().list_pending()["valid"][0]
        assert row["angle"] == ""
        assert row["tags"] == []

    def test_repeat_propose_dedupes_and_updates_same_row(self, agent_headers):
        app = _app()
        with TestClient(app) as client:
            first = client.post(
                "/api/agent/topics",
                json={
                    "agent": "claude-code",
                    "title": "Same topic twice",
                    "angle": "first angle",
                },
                headers=agent_headers,
            )
            second = client.post(
                "/api/agent/topics",
                json={
                    "agent": "claude-code",
                    "title": "Same topic twice",
                    "angle": "second angle",
                },
                headers=agent_headers,
            )
        assert first.json()["deduped"] is False
        assert second.status_code == 200
        assert second.json()["deduped"] is True
        assert second.json()["id"] == first.json()["id"]
        pending = _store().list_pending()["valid"]
        assert len(pending) == 1
        assert pending[0]["angle"] == "second angle"

    def test_31st_pending_proposal_is_429(self, agent_headers):
        store = _store()
        for i in range(PENDING_CAP):
            store.propose(f"Prefill topic number {i}", "", [], source="prefill")
        app = _app()
        with TestClient(app) as client:
            resp = client.post(
                "/api/agent/topics",
                json={"agent": "claude-code", "title": "The one over the cap"},
                headers=agent_headers,
            )
        assert resp.status_code == 429
        assert "full" in resp.json()["detail"]

    def test_agent_name_over_80_chars_is_422_with_store_message(self, agent_headers):
        app = _app()
        with TestClient(app) as client:
            resp = client.post(
                "/api/agent/topics",
                json={"agent": "a" * (SOURCE_MAX + 1), "title": "A valid title"},
                headers=agent_headers,
            )
        assert resp.status_code == 422
        assert resp.json()["detail"] == f"source exceeds {SOURCE_MAX} characters"

    def test_code_in_title_is_422_with_store_message(self, agent_headers):
        app = _app()
        with TestClient(app) as client:
            resp = client.post(
                "/api/agent/topics",
                json={"agent": "claude-code", "title": "look <script>alert(1)</script>"},
                headers=agent_headers,
            )
        assert resp.status_code == 422
        assert resp.json()["detail"] == "title contains code or HTML"

    def test_missing_title_is_422(self, agent_headers):
        app = _app()
        with TestClient(app) as client:
            resp = client.post(
                "/api/agent/topics",
                json={"agent": "claude-code"},
                headers=agent_headers,
            )
        assert resp.status_code == 422


# ──────────────────────────────────────────────────────────────────────────
# GET /api/agent/status — read-only counts with any valid token
# ──────────────────────────────────────────────────────────────────────────


class TestAgentStatus:
    def test_status_counts_pending_topics_and_cards(self, agent_headers):
        store = _store()
        store.propose("First pending topic", "", [], source="a1")
        store.propose("Second pending topic", "", [], source="a1")
        # Minimal editorial_cards table: the count helper only reads `status`.
        with closing(sqlite3.connect(main_mod.EDITORIAL_CARDS_DB)) as conn, conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS editorial_cards (id TEXT PRIMARY KEY, status TEXT)"
            )
            conn.executemany(
                "INSERT INTO editorial_cards (id, status) VALUES (?, ?)",
                [("ec_1", "draft"), ("ec_2", "armed"), ("ec_3", "armed")],
            )
        app = _app()
        with TestClient(app) as client:
            resp = client.get("/api/agent/status", headers=agent_headers)
        assert resp.status_code == 200
        assert resp.json() == {
            "topics_pending": 2,
            "cards": {"draft": 1, "armed": 2},
            "notices_undismissed": 0,
        }

    def test_status_fails_open_to_zeros_when_db_missing(self, agent_headers):
        app = _app()
        with TestClient(app) as client:
            resp = client.get("/api/agent/status", headers=agent_headers)
        assert resp.status_code == 200
        assert resp.json() == {
            "topics_pending": 0,
            "cards": {},
            "notices_undismissed": 0,
        }

    def test_status_is_readable_with_operator_token_too(self, operator_headers):
        app = _app()
        with TestClient(app) as client:
            resp = client.get("/api/agent/status", headers=operator_headers)
        assert resp.status_code == 200


# ──────────────────────────────────────────────────────────────────────────
# RateLimiter — fixed 60s window, 60 mutating requests per token
# ──────────────────────────────────────────────────────────────────────────


class TestRateLimiterUnit:
    def test_blocks_after_limit_within_window(self):
        clock = _FakeClock()
        limiter = auth.RateLimiter(limit=3, window_seconds=60.0, time_fn=clock)
        assert [limiter.allow("tok") for _ in range(3)] == [True, True, True]
        assert limiter.allow("tok") is False

    def test_window_reset_reallows(self):
        clock = _FakeClock()
        limiter = auth.RateLimiter(limit=2, window_seconds=60.0, time_fn=clock)
        assert limiter.allow("tok") is True
        assert limiter.allow("tok") is True
        assert limiter.allow("tok") is False
        clock.now += 60.0
        assert limiter.allow("tok") is True

    def test_tokens_have_independent_windows(self):
        clock = _FakeClock()
        limiter = auth.RateLimiter(limit=1, window_seconds=60.0, time_fn=clock)
        assert limiter.allow("token-a") is True
        assert limiter.allow("token-a") is False
        assert limiter.allow("token-b") is True


class TestRateLimitOnAgentSurface:
    def test_61st_mutating_call_is_429_then_window_reset_reallows(
        self, agent_headers, monkeypatch
    ):
        clock = _FakeClock()
        monkeypatch.setattr(auth, "_rate_limiter", auth.RateLimiter(time_fn=clock))
        app = _app()
        payload = {"agent": "claude-code", "title": "Rate limit probe topic"}
        with TestClient(app) as client:
            for _ in range(60):
                ok = client.post(
                    "/api/agent/topics", json=payload, headers=agent_headers
                )
                assert ok.status_code == 200
            blocked = client.post(
                "/api/agent/topics", json=payload, headers=agent_headers
            )
            assert blocked.status_code == 429
            clock.now += 60.0
            reallowed = client.post(
                "/api/agent/topics", json=payload, headers=agent_headers
            )
            assert reallowed.status_code == 200

    def test_get_status_is_never_rate_limited(self, agent_headers, monkeypatch):
        monkeypatch.setattr(auth, "_rate_limiter", auth.RateLimiter(limit=0))
        app = _app()
        with TestClient(app) as client:
            # Mutating call is blocked by the zero-budget limiter BEFORE the
            # handler runs (no inbox row appears)...
            blocked = client.post(
                "/api/agent/topics",
                json={"agent": "claude-code", "title": "Blocked by zero budget"},
                headers=agent_headers,
            )
            assert blocked.status_code == 429
            assert _store().list_pending()["valid"] == []
            # ...but the read surface is never counted.
            resp = client.get("/api/agent/status", headers=agent_headers)
            assert resp.status_code == 200


# ──────────────────────────────────────────────────────────────────────────
# Owner decision D1 — the agent surface has NO path into the agenda queue
# ──────────────────────────────────────────────────────────────────────────


class TestD1AgendaIsolation:
    def test_agent_surface_exposes_exactly_topics_and_status(self):
        """D1 (literal, structural): the agent surface is EXACTLY the
        human-gated topic inbox plus the read-only status probe. If this set
        ever grows an agenda route, that is a trust-model regression."""
        app = _app()
        agent_paths = {
            getattr(route, "path", "")
            for route in app.routes
            if getattr(route, "path", "").startswith("/api/agent/")
        }
        assert agent_paths == {"/api/agent/topics", "/api/agent/status"}

    def test_agent_topic_proposal_never_reaches_agenda_queue(self, agent_headers):
        """D1 (literal, behavioral): a successful agent proposal lands in the
        inbox and the live agenda controller stays untouched."""

        def factory():
            host = FakeHost()
            host.agenda = KiraAgendaController()
            return host

        app = _app(factory)
        with TestClient(app) as client:
            resp = client.post(
                "/api/agent/topics",
                json={"agent": "claude-code", "title": "Inbox only never agenda"},
                headers=agent_headers,
            )
            assert resp.status_code == 200
            agenda = client.get("/api/agenda").json()
        assert agenda["queued_topics"] == []
        assert agenda["drafted_topics"] == []
        assert agenda["active_topic"] is None
        assert agenda["metrics"]["topics_queued"] == 0
        assert len(_store().list_pending()["valid"]) == 1
