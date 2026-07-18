"""Strict-TDD tests for the agent gateway surface (Phases 2-3 of track
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
  notices_undismissed} with any valid token; notices_undismissed is wired
  to the agent_notices store (Phase 3).
- POST /api/agent/cards: maps to EditorialCardStore.upsert with the
  dataclass's own validation; writes origin=agent (new provenance column,
  idempotent PRAGMA-guarded migration); demotion rule — a new card lands
  DRAFT, an existing ARMED/ACTIVE card is set back to DRAFT after upsert
  with demoted=true (closes the silent-rewrite hole; CLI unchanged).
- POST /api/agent/notice (agent surface) + GET /api/agent/notices and
  POST /api/agent/notices/{id}/dismiss (operator surface — agent token
  gets 403): caps/dedupe live in AgentNoticeStore.
- ADR-2 literal assertion: llm_engine._DIGEST_CAPTURE_SOURCES stays
  frozenset({"direct", "ptt"}) — an agent source must NEVER be added.
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
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

import opencohost.api.auth as auth
import opencohost.api.main as main_mod
from opencohost.core import llm_engine
from opencohost.core.agent_notices import UNDISMISSED_CAP, AgentNoticeStore
from opencohost.core.editorial_cards import (
    EditorialCard,
    EditorialCardStatus,
    EditorialCardStore,
)
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


def _cards_store() -> EditorialCardStore:
    return EditorialCardStore(main_mod.EDITORIAL_CARDS_DB)


def _notices_store() -> AgentNoticeStore:
    return AgentNoticeStore(main_mod.EDITORIAL_CARDS_DB)


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

    def test_blank_agent_name_is_422(self, agent_headers):
        """design line 13: agent name REQUIRED in every agent write — a blank
        name would store source='' and forge operator provenance."""
        app = _app()
        with TestClient(app) as client:
            for blank in ("", "   "):
                resp = client.post(
                    "/api/agent/topics",
                    json={"agent": blank, "title": "A valid title"},
                    headers=agent_headers,
                )
                assert resp.status_code == 422
        assert _store().list_pending()["valid"] == []

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

    def test_status_wires_real_undismissed_notice_count(self, agent_headers):
        """Phase 3: notices_undismissed is no longer pinned to 0 — it counts
        the agent_notices rows still in 'proposed'."""
        store = _notices_store()
        store.propose("First undismissed notice", source="bot")
        store.propose("Second undismissed notice", source="bot")
        dismissed = store.propose("Dismissed notice", source="bot")
        store.dismiss(dismissed["id"])
        app = _app()
        with TestClient(app) as client:
            resp = client.get("/api/agent/status", headers=agent_headers)
        assert resp.status_code == 200
        assert resp.json()["notices_undismissed"] == 2


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
    def test_agent_surface_exposes_exactly_the_designed_routes(self):
        """D1 (literal, structural): the agent surface is EXACTLY the
        human-gated topic inbox, the card/notice proposal routes, the
        operator notice surface, and the read-only status probe. If this
        set ever grows an agenda route, that is a trust-model regression."""
        app = _app()
        agent_paths = {
            getattr(route, "path", "")
            for route in app.routes
            if getattr(route, "path", "").startswith("/api/agent/")
        }
        assert agent_paths == {
            "/api/agent/topics",
            "/api/agent/status",
            "/api/agent/cards",
            "/api/agent/cards/{card_id}/arm",
            "/api/agent/notice",
            "/api/agent/notices",
            "/api/agent/notices/{notice_id}/dismiss",
        }

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


# ──────────────────────────────────────────────────────────────────────────
# POST /api/agent/cards — EditorialCardStore.upsert + origin + demotion rule
# ──────────────────────────────────────────────────────────────────────────


_CARD_PAYLOAD = {
    "agent": "claude-code",
    "topic": "Quantum computing on the desktop",
    "summary": "Consumer quantum dev kits are shipping this year.",
    "streamer_take": "Cool for research, useless for gaming for now.",
}


class TestAgentCardUpsert:
    def test_new_card_lands_draft_with_agent_origin(self, agent_headers):
        app = _app()
        with TestClient(app) as client:
            resp = client.post(
                "/api/agent/cards", json=_CARD_PAYLOAD, headers=agent_headers
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"].startswith("ec_")
        assert body["status"] == "draft"
        assert body["demoted"] is False
        card = _cards_store().find_by_topic_slug(body["topic_slug"])
        assert card is not None
        assert card.status is EditorialCardStatus.DRAFT
        assert card.origin == "claude-code"

    def test_upsert_over_armed_card_demotes_to_draft(self, agent_headers):
        store = _cards_store()
        seeded = store.upsert(
            EditorialCard(
                topic=_CARD_PAYLOAD["topic"],
                summary="Original operator summary.",
                streamer_take="Original take.",
            )
        )
        assert store.arm(seeded.id) is True
        app = _app()
        with TestClient(app) as client:
            resp = client.post(
                "/api/agent/cards", json=_CARD_PAYLOAD, headers=agent_headers
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == seeded.id
        assert body["demoted"] is True
        assert body["status"] == "draft"
        card = store.get(seeded.id)
        assert card.status is EditorialCardStatus.DRAFT
        assert card.summary == _CARD_PAYLOAD["summary"]
        assert card.origin == "claude-code"

    def test_upsert_over_draft_card_is_not_demoted(self, agent_headers):
        store = _cards_store()
        seeded = store.upsert(
            EditorialCard(
                topic=_CARD_PAYLOAD["topic"],
                summary="Original operator summary.",
                streamer_take="Original take.",
            )
        )
        app = _app()
        with TestClient(app) as client:
            resp = client.post(
                "/api/agent/cards", json=_CARD_PAYLOAD, headers=agent_headers
            )
        assert resp.status_code == 200
        assert resp.json()["id"] == seeded.id
        assert resp.json()["demoted"] is False
        assert resp.json()["status"] == "draft"

    def test_card_validation_error_is_422_with_dataclass_message(self, agent_headers):
        payload = dict(_CARD_PAYLOAD, summary="S" * 1201)
        app = _app()
        with TestClient(app) as client:
            resp = client.post("/api/agent/cards", json=payload, headers=agent_headers)
        assert resp.status_code == 422
        assert resp.json()["detail"] == "summary exceeds 1200 characters"

    def test_agent_name_over_80_chars_is_422(self, agent_headers):
        payload = dict(_CARD_PAYLOAD, agent="a" * 81)
        app = _app()
        with TestClient(app) as client:
            resp = client.post("/api/agent/cards", json=payload, headers=agent_headers)
        assert resp.status_code == 422
        assert resp.json()["detail"] == "origin exceeds 80 characters"

    def test_bad_expires_at_is_422(self, agent_headers):
        payload = dict(_CARD_PAYLOAD, expires_at="not-a-timestamp")
        app = _app()
        with TestClient(app) as client:
            resp = client.post("/api/agent/cards", json=payload, headers=agent_headers)
        assert resp.status_code == 422

    def test_z_suffixed_expires_at_is_accepted(self, agent_headers):
        """design line 93: expires_at is iso8601 | null. Z-UTC is the
        canonical machine-emitted form (Date.toISOString(), RFC3339) and the
        runtime is Python 3.10, where fromisoformat rejects the Z suffix
        unless the handler normalizes it."""
        payload = dict(_CARD_PAYLOAD, expires_at="2026-07-08T00:00:00Z")
        app = _app()
        with TestClient(app) as client:
            resp = client.post("/api/agent/cards", json=payload, headers=agent_headers)
        assert resp.status_code == 200
        card = _cards_store().find_by_topic_slug(resp.json()["topic_slug"])
        assert card.expires_at == datetime(2026, 7, 8, tzinfo=timezone.utc)

    def test_blank_agent_name_is_422_never_operator_origin(self, agent_headers):
        """design line 13: agent name REQUIRED in every agent write. Blank
        would store origin='' — which editorial_cards defines as OPERATOR
        provenance — so it must be rejected, never coerced."""
        app = _app()
        with TestClient(app) as client:
            for blank in ("", "   "):
                payload = dict(_CARD_PAYLOAD, agent=blank)
                resp = client.post(
                    "/api/agent/cards", json=payload, headers=agent_headers
                )
                assert resp.status_code == 422
        with closing(sqlite3.connect(main_mod.EDITORIAL_CARDS_DB)) as conn:
            tables = conn.execute(
                "SELECT name FROM sqlite_master WHERE name='editorial_cards'"
            ).fetchall()
        assert tables == []  # 422 fired before any store write

    def test_missing_required_field_is_422(self, agent_headers):
        payload = {k: v for k, v in _CARD_PAYLOAD.items() if k != "summary"}
        app = _app()
        with TestClient(app) as client:
            resp = client.post("/api/agent/cards", json=payload, headers=agent_headers)
        assert resp.status_code == 422


class TestEditorialCardOriginColumn:
    _LEGACY_SCHEMA = """
        CREATE TABLE editorial_cards (
            id TEXT PRIMARY KEY,
            topic_slug TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL,
            topic TEXT NOT NULL,
            summary TEXT NOT NULL,
            streamer_take TEXT NOT NULL,
            counterpoints_json TEXT NOT NULL,
            discussion_hooks_json TEXT NOT NULL,
            triggers_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            expires_at TEXT,
            last_used_at TEXT,
            use_count INTEGER NOT NULL DEFAULT 0
        )
    """

    def test_legacy_db_gains_origin_column_idempotently(self, tmp_path):
        """A cards.db created before this track has no origin column; store
        construction must migrate it exactly once (PRAGMA-guarded ALTER)."""
        db = tmp_path / "legacy.db"
        with closing(sqlite3.connect(db)) as conn, conn:
            conn.execute(self._LEGACY_SCHEMA)
        store = EditorialCardStore(db)
        EditorialCardStore(db)  # second init must be a no-op, not an error
        with closing(sqlite3.connect(db)) as conn:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(editorial_cards)")}
        assert "origin" in columns
        card = store.upsert(
            EditorialCard(topic="Legacy topic", summary="s", streamer_take="t")
        )
        assert card.origin == ""

    def test_cli_style_upsert_leaves_origin_empty(self, tmp_path):
        """CLI/UI writes never set origin — '' means operator (design)."""
        store = EditorialCardStore(tmp_path / "cards.db")
        card = store.upsert(
            EditorialCard(topic="Operator topic", summary="s", streamer_take="t")
        )
        assert store.get(card.id).origin == ""


# ──────────────────────────────────────────────────────────────────────────
# POST /api/agent/notice + operator notice surface
# ──────────────────────────────────────────────────────────────────────────


class TestAgentNoticeEndpoint:
    def test_post_notice_creates_row(self, agent_headers):
        app = _app()
        with TestClient(app) as client:
            resp = client.post(
                "/api/agent/notice",
                json={"agent": "claude-code", "text": "Digest ready for review"},
                headers=agent_headers,
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"].startswith("an_")
        assert body["deduped"] is False
        pending = _notices_store().list_pending()["valid"]
        assert len(pending) == 1
        assert pending[0]["text"] == "Digest ready for review"
        assert pending[0]["source"] == "claude-code"

    def test_duplicate_source_text_dedupes_to_existing_id(self, agent_headers):
        app = _app()
        payload = {"agent": "claude-code", "text": "Same notice twice"}
        with TestClient(app) as client:
            first = client.post("/api/agent/notice", json=payload, headers=agent_headers)
            second = client.post("/api/agent/notice", json=payload, headers=agent_headers)
        assert first.json()["deduped"] is False
        assert second.status_code == 200
        assert second.json()["deduped"] is True
        assert second.json()["id"] == first.json()["id"]
        assert len(_notices_store().list_pending()["valid"]) == 1

    def test_21st_undismissed_notice_is_429(self, agent_headers):
        store = _notices_store()
        for i in range(UNDISMISSED_CAP):
            store.propose(f"Prefill notice number {i:03d}", source="prefill")
        app = _app()
        with TestClient(app) as client:
            resp = client.post(
                "/api/agent/notice",
                json={"agent": "claude-code", "text": "The one over the cap"},
                headers=agent_headers,
            )
        assert resp.status_code == 429
        assert "full" in resp.json()["detail"]

    def test_duplicate_dedupes_even_when_board_is_full(self, agent_headers):
        store = _notices_store()
        first = store.propose("Prefill notice number 000", source="claude-code")
        for i in range(1, UNDISMISSED_CAP):
            store.propose(f"Prefill notice number {i:03d}", source="claude-code")
        app = _app()
        with TestClient(app) as client:
            resp = client.post(
                "/api/agent/notice",
                json={"agent": "claude-code", "text": "Prefill notice number 000"},
                headers=agent_headers,
            )
        assert resp.status_code == 200
        assert resp.json() == {"id": first["id"], "deduped": True}

    def test_blank_agent_name_is_422(self, agent_headers):
        """design line 13: agent name REQUIRED in every agent write — same
        rule as topics/cards, shared at the request models."""
        app = _app()
        with TestClient(app) as client:
            for blank in ("", "   "):
                resp = client.post(
                    "/api/agent/notice",
                    json={"agent": blank, "text": "Valid notice text"},
                    headers=agent_headers,
                )
                assert resp.status_code == 422
        assert _notices_store().list_pending()["valid"] == []

    def test_notice_validation_error_is_422_with_store_message(self, agent_headers):
        app = _app()
        with TestClient(app) as client:
            resp = client.post(
                "/api/agent/notice",
                json={"agent": "claude-code", "text": "T" * 281},
                headers=agent_headers,
            )
        assert resp.status_code == 422
        assert resp.json()["detail"] == "text exceeds 280 characters"


class TestOperatorNoticeSurface:
    def test_get_notices_lists_pending_with_operator_token(self, operator_headers):
        store = _notices_store()
        row = store.propose("Visible to the operator", source="bot")
        dismissed = store.propose("Already handled", source="bot")
        store.dismiss(dismissed["id"])
        app = _app()
        with TestClient(app) as client:
            resp = client.get("/api/agent/notices", headers=operator_headers)
        assert resp.status_code == 200
        notices = resp.json()["notices"]
        assert [n["id"] for n in notices] == [row["id"]]
        assert notices[0]["text"] == "Visible to the operator"
        assert notices[0]["source"] == "bot"

    def test_get_notices_with_agent_token_is_403(self, agent_headers):
        app = _app()
        with TestClient(app) as client:
            resp = client.get("/api/agent/notices", headers=agent_headers)
        assert resp.status_code == 403

    def test_dismiss_with_operator_token(self, operator_headers):
        row = _notices_store().propose("Dismiss me", source="bot")
        app = _app()
        with TestClient(app) as client:
            resp = client.post(
                f"/api/agent/notices/{row['id']}/dismiss", headers=operator_headers
            )
        assert resp.status_code == 200
        assert resp.json() == {"dismissed": True}
        assert _notices_store().list_pending()["valid"] == []

    def test_dismiss_with_agent_token_is_403(self, agent_headers):
        """The agent token can CREATE notices but never dismiss them — a
        self-dismissing agent would hide its own messages from the human."""
        row = _notices_store().propose("Agent may not dismiss", source="bot")
        app = _app()
        with TestClient(app) as client:
            resp = client.post(
                f"/api/agent/notices/{row['id']}/dismiss", headers=agent_headers
            )
        assert resp.status_code == 403
        assert len(_notices_store().list_pending()["valid"]) == 1

    def test_dismiss_unknown_id_is_404(self, operator_headers):
        _notices_store().propose("Table exists", source="bot")
        app = _app()
        with TestClient(app) as client:
            resp = client.post(
                "/api/agent/notices/an_unknown/dismiss", headers=operator_headers
            )
        assert resp.status_code == 404


# ──────────────────────────────────────────────────────────────────────────
# ADR-2 — memoria isolation is inherited, never rebuilt
# ──────────────────────────────────────────────────────────────────────────


class TestAdr2MemoriaIsolation:
    def test_digest_capture_sources_is_the_literal_frozen_allowlist(self):
        """ADR-2 (literal): the fail-closed capture allowlist must stay
        EXACTLY {"direct", "ptt"}. Adding any agent source here would let
        agent text reach memorias.db — a trust-model regression this track
        explicitly forbids. Do NOT update this assertion to make a diff
        pass; remove the new source instead."""
        assert llm_engine._DIGEST_CAPTURE_SOURCES == frozenset({"direct", "ptt"})
