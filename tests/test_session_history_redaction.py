"""D2 — session_history whole-payload persistence redaction + legacy purge.

Redaction is a persistence-only boundary: the LIVE prompt sent to Kira
(IntentAggregator.to_prompt) is unchanged. Only what SessionHistory writes
to sessions.db is sanitized (see privacy_prereq_fixes_20260701 design).
"""

import json
import os
import sqlite3
import time

from opencohost.smart_aggregator.session_history import SessionHistory
from opencohost.smart_aggregator.intent_aggregator import IntentAggregator


def _deep_keys(obj) -> set:
    """Collect every dict key found anywhere in a nested JSON-like structure."""
    keys: set = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            keys.add(k)
            keys |= _deep_keys(v)
    elif isinstance(obj, list):
        for item in obj:
            keys |= _deep_keys(item)
    return keys


class TestSessionHistoryMetadataRedaction:
    """add_context_snapshot must sanitize the entire persisted payload."""

    def test_persisted_metadata_drops_verbatim_examples_and_entities(self, temp_dir):
        db_path = os.path.join(temp_dir, "sessions.db")
        jl_path = os.path.join(temp_dir, "chat_log.jsonl")
        history = SessionHistory(db_path, jl_path, retention_hours=1)
        sid = history.start_session("youtube", "test_channel")

        history.add_context_snapshot(
            sid,
            "CONTEXTO PRIVADO DEL CHAT PARA KIRA: tema dominante de prueba",
            message_count=10,
            metadata={
                "top_intents": [
                    {
                        "intent": "video_collab",
                        "label": "pedidos de videos o colaboraciones",
                        "count": 5,
                        "duplicates": 1,
                        "entities": ["los bros", "Fede"],
                        "examples": [
                            {"user": "viewer1", "text": "pa cuando video con los bros"}
                        ],
                    }
                ],
            },
        )

        conn = sqlite3.connect(db_path)
        row = conn.execute("SELECT metadata_json FROM context_snapshots").fetchone()
        conn.close()
        persisted = json.loads(row[0])
        leaked = {"examples", "user", "text", "entities"} & _deep_keys(persisted)
        assert not leaked, f"Leaked keys in persisted metadata: {leaked}"
        # Safe fields must survive
        assert persisted["top_intents"][0]["intent"] == "video_collab"
        assert persisted["top_intents"][0]["count"] == 5

    def test_persisted_summary_strips_referencias_detectadas(self, temp_dir):
        db_path = os.path.join(temp_dir, "sessions.db")
        jl_path = os.path.join(temp_dir, "chat_log.jsonl")
        history = SessionHistory(db_path, jl_path, retention_hours=1)
        sid = history.start_session("youtube", "test_channel")

        summary = (
            "- Intención dominante: pedidos de videos o colaboraciones."
            " Referencias detectadas: los bros, Fede."
            " Conversación repetitiva."
        )
        history.add_context_snapshot(sid, summary, message_count=5)

        conn = sqlite3.connect(db_path)
        row = conn.execute("SELECT summary FROM context_snapshots").fetchone()
        conn.close()
        assert "Referencias detectadas" not in row[0]
        assert "los bros" not in row[0]
        # The rest of the summary must survive the redaction.
        assert "Intención dominante" in row[0]
        assert "Conversación repetitiva" in row[0]

    def test_persisted_trigger_metadata_allowlisted(self, temp_dir):
        db_path = os.path.join(temp_dir, "sessions.db")
        jl_path = os.path.join(temp_dir, "chat_log.jsonl")
        history = SessionHistory(db_path, jl_path, retention_hours=1)
        sid = history.start_session("youtube", "test_channel")

        history.add_context_snapshot(
            sid,
            "contexto compacto",
            message_count=1,
            metadata={
                "trigger": {
                    "rate": 12.5,
                    "threshold": 5.0,
                    "window_seconds": 10,
                    "actions": {"auto_reply": "hi"},
                    "unexpected_key": "leak-me-not",
                },
            },
        )

        conn = sqlite3.connect(db_path)
        row = conn.execute("SELECT metadata_json FROM context_snapshots").fetchone()
        conn.close()
        persisted = json.loads(row[0])
        assert persisted["trigger"] == {"rate": 12.5, "threshold": 5.0, "window_seconds": 10}

    def test_unknown_top_level_metadata_keys_dropped(self, temp_dir):
        db_path = os.path.join(temp_dir, "sessions.db")
        jl_path = os.path.join(temp_dir, "chat_log.jsonl")
        history = SessionHistory(db_path, jl_path, retention_hours=1)
        sid = history.start_session("youtube", "test_channel")

        history.add_context_snapshot(
            sid,
            "contexto compacto",
            message_count=1,
            metadata={"unknown_field": "should not survive"},
        )

        conn = sqlite3.connect(db_path)
        row = conn.execute("SELECT metadata_json FROM context_snapshots").fetchone()
        conn.close()
        persisted = json.loads(row[0])
        assert "unknown_field" not in persisted


class TestIntentAggregatorLivePromptUnchanged:
    """The redaction fix must be persistence-only — to_prompt is untouched."""

    def test_to_prompt_still_includes_referencias_detectadas_live(self):
        agg = IntentAggregator({"top_intents": 3, "min_count": 2, "window_seconds": 60})
        base = time.time()
        for i, text in enumerate(
            ["pa cuando video con los bros", "cuando regresan los bros"]
        ):
            agg.add_message({"user": f"u{i}", "text": text, "timestamp": base + i})

        prompt = agg.summarize(now=base + 10)["prompt"]
        assert "Referencias detectadas" in prompt


class TestSessionHistoryEndToEndRedaction:
    """Guards against IntentAggregator.to_prompt phrasing drift.

    The other redaction tests hand-mirror the "Referencias detectadas: ..."
    string, which would silently stop catching a leak if to_prompt's entity
    phrasing ever changes. This test builds a real IntentAggregator, takes
    its actual live prompt verbatim (no mocking of to_prompt), and asserts
    the extracted entity never survives persistence.
    """

    def test_live_prompt_entity_redacted_end_to_end(self, temp_dir):
        agg = IntentAggregator({"top_intents": 3, "min_count": 2, "window_seconds": 60})
        base = time.time()
        for i, text in enumerate(["juega GTA6 porfa", "deberias jugar GTA6"]):
            agg.add_message({"user": f"u{i}", "text": text, "timestamp": base + i})

        result = agg.summarize(now=base + 10)
        prompt = result["prompt"]
        entity = result["top_intents"][0]["entities"][0]
        assert "Referencias detectadas" in prompt
        assert entity in prompt

        db_path = os.path.join(temp_dir, "sessions.db")
        jl_path = os.path.join(temp_dir, "chat_log.jsonl")
        history = SessionHistory(db_path, jl_path, retention_hours=1)
        sid = history.start_session("youtube", "test_channel")

        history.add_context_snapshot(sid, prompt, message_count=2)

        conn = sqlite3.connect(db_path)
        row = conn.execute("SELECT summary FROM context_snapshots").fetchone()
        conn.close()
        persisted = row[0]

        assert entity not in persisted, f"Entity {entity!r} leaked into persisted summary"
        assert "Referencias detectadas" not in persisted
        # The intent label line must survive the redaction.
        assert "Intención dominante" in persisted


class TestSessionHistoryLegacyPurge:
    """One-time, marker-guarded purge of pre-redaction liability data."""

    def test_legacy_data_purged_exactly_once(self, temp_dir):
        db_path = os.path.join(temp_dir, "sessions.db")
        jl_path = os.path.join(temp_dir, "chat_log.jsonl")

        # Seed a legacy DB as if written by pre-redaction / pre-refactor code.
        conn = sqlite3.connect(db_path)
        conn.execute(
            """CREATE TABLE sessions (id INTEGER PRIMARY KEY, platform TEXT,
               channel TEXT, start_time REAL, end_time REAL)"""
        )
        conn.execute(
            """CREATE TABLE context_snapshots (id INTEGER PRIMARY KEY, session_id INTEGER,
               summary TEXT, timestamp REAL, message_count INTEGER,
               vibe_temperature REAL, metadata_json TEXT)"""
        )
        conn.execute(
            """CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id INTEGER, text TEXT)"""
        )
        conn.execute(
            "INSERT INTO sessions (platform, channel, start_time, end_time) VALUES (?, ?, ?, ?)",
            ("youtube", "legacy_channel", time.time(), None),
        )
        sid = conn.execute("SELECT id FROM sessions").fetchone()[0]
        conn.execute(
            "INSERT INTO context_snapshots (session_id, summary, timestamp, message_count, "
            "vibe_temperature, metadata_json) VALUES (?, ?, ?, ?, ?, ?)",
            (sid, "legacy snapshot with entities: los bros", time.time(), 3, None, "{}"),
        )
        conn.execute("INSERT INTO messages (session_id, text) VALUES (?, ?)", (sid, "raw viewer chat"))
        conn.commit()
        conn.close()
        with open(jl_path, "w", encoding="utf-8") as f:
            f.write(json.dumps({"session_id": sid, "text": "legacy raw"}) + "\n")

        # First init: purge must run exactly once.
        history = SessionHistory(db_path, jl_path, retention_hours=1)

        conn = sqlite3.connect(db_path)
        snapshots = conn.execute("SELECT COUNT(*) FROM context_snapshots").fetchone()[0]
        sessions_count = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        has_messages = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='messages'"
        ).fetchone()
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        conn.close()

        assert snapshots == 0, "Legacy context_snapshots rows must be purged"
        assert sessions_count == 1, "sessions table rows must remain intact"
        assert has_messages is None, "Legacy messages table must be dropped"
        assert not os.path.exists(jl_path), "Legacy chat_log.jsonl must be unlinked"
        assert version == 1, "PRAGMA user_version must be bumped to 1"

        # New data added post-purge must survive a second init (no-op).
        sid2 = history.start_session("youtube", "new_channel")
        history.add_context_snapshot(sid2, "fresh compact context", message_count=1)

        SessionHistory(db_path, jl_path, retention_hours=1)
        conn = sqlite3.connect(db_path)
        snapshots_after = conn.execute("SELECT COUNT(*) FROM context_snapshots").fetchone()[0]
        conn.close()
        assert snapshots_after == 1, "Second init must be a no-op — new rows must survive"

    def test_purge_handles_fresh_install_gracefully(self, temp_dir):
        """No legacy messages table, no jsonl file — must not raise."""
        db_path = os.path.join(temp_dir, "sessions.db")
        jl_path = os.path.join(temp_dir, "chat_log.jsonl")
        history = SessionHistory(db_path, jl_path, retention_hours=1)
        sid = history.start_session("youtube", "fresh_channel")
        assert sid > 0
