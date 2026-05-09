"""Migrated tests from smart_aggregator/test_local.py.

Preserves all 7 test categories:
  TC3.1 — Message Filter
  TC3.2 — Vibe Thermometer
  TC3.3 — Activity Trigger
  TC3.4 — Session History
  TC3.5 — YouTube Chat Source
  TC3.6 — Aggregator Orchestration
  TC3.7 — YouTube API (placeholder)
"""

import os
import sqlite3
import time

import pytest
import yaml

from smart_aggregator.session_history import SessionHistory
from smart_aggregator.message_filter import MessageFilter
from smart_aggregator.chat_source import YouTubeChatSource
from smart_aggregator.vibe_thermometer import VibeThermometer
from smart_aggregator.activity_trigger import ActivityTrigger
from smart_aggregator.aggregator import Aggregator

MOCK_MESSAGES_20 = [
    {"user": f"user{i}", "text": f"Mensaje de prueba numero {i} para Kira", "timestamp": time.time() + i}
    for i in range(20)
]

MOCK_MESSAGES_200 = []
for i in range(200):
    if i % 10 == 0:
        text = "hola"
    elif i % 10 == 1:
        text = "🔥🔥🔥"
    elif i % 10 == 2:
        text = "mira esto https://youtube.com/test"
    elif i % 10 == 3:
        text = "@Kira eres genial"
    elif i % 10 == 4:
        text = "ESTO ES INCREIBLE"
    elif i % 10 == 5:
        text = "Que momento epico"
    elif i % 10 == 6:
        text = "Esto es basura"
    elif i % 10 == 7:
        text = "Kira que opinas del juego?"
    elif i % 10 == 8:
        text = "GG"
    else:
        text = f"Mensaje largo y variado numero {i} con suficientes palabras para pasar el filtro"
    MOCK_MESSAGES_200.append({"user": f"u{i}", "text": text, "timestamp": time.time() + i})

VIBE_TEST_MESSAGES = [
    {"user": "fan1", "text": "ESTO ES INCREIBLE 🔥🔥🔥", "timestamp": time.time()},
    {"user": "fan2", "text": "Que momento epico", "timestamp": time.time() + 1},
    {"user": "hater1", "text": "Esto es basura", "timestamp": time.time() + 2},
    {"user": "normal1", "text": "Kira que opinas del juego?", "timestamp": time.time() + 3},
]


# --- TC3.1 — Message Filter ---

class TestMessageFilter:
    """TC3.1: Message filtering rules."""

    def test_short_message_discarded(self, smart_aggregator_config):
        """TC3.1.1: Short messages should be discarded."""
        cfg = smart_aggregator_config["filter"]
        msg_filter = MessageFilter(cfg)
        result = msg_filter.filter({"user": "a", "text": "hola", "timestamp": time.time()})
        assert result is None

    def test_pure_emojis_discarded(self, smart_aggregator_config):
        """TC3.1.2: Pure emoji messages should be discarded."""
        cfg = smart_aggregator_config["filter"].copy()
        cfg["min_words"] = 1
        cfg["min_char_length"] = 1
        emoji_filter = MessageFilter(cfg)
        result = emoji_filter.filter({"user": "a", "text": "🔥🔥🔥", "timestamp": time.time()})
        assert result is None

    def test_links_discarded(self, smart_aggregator_config):
        """TC3.1.3: Messages with links should be discarded."""
        cfg = smart_aggregator_config["filter"]
        msg_filter = MessageFilter(cfg)
        result = msg_filter.filter({"user": "a", "text": "mira esto https://youtube.com/test", "timestamp": time.time()})
        assert result is None

    def test_mentions_discarded(self, smart_aggregator_config):
        """TC3.1.4: Messages with @mentions should be discarded."""
        cfg = smart_aggregator_config["filter"]
        msg_filter = MessageFilter(cfg)
        result = msg_filter.filter({"user": "a", "text": "@Kira eres genial", "timestamp": time.time()})
        assert result is None

    def test_mentions_with_hyphen_discarded(self, smart_aggregator_config):
        """TC3.1.4b: Mentions with hyphen should be discarded."""
        cfg = smart_aggregator_config["filter"]
        msg_filter = MessageFilter(cfg)
        result = msg_filter.filter({"user": "a", "text": "@Kira-test eres genial", "timestamp": time.time()})
        assert result is None

    def test_normal_message_passes(self, smart_aggregator_config):
        """TC3.1.5: Normal messages should pass the filter."""
        cfg = smart_aggregator_config["filter"]
        msg_filter = MessageFilter(cfg)
        result = msg_filter.filter({"user": "a", "text": "Kira que juego estas jugando?", "timestamp": time.time()})
        assert result is not None

    def test_custom_emojis_pure_discarded(self, smart_aggregator_config):
        """TC3.1.5b: Pure custom emojis should be discarded."""
        cfg = smart_aggregator_config["filter"].copy()
        cfg["min_words"] = 1
        cfg["min_char_length"] = 1
        emoji_filter = MessageFilter(cfg)
        result = emoji_filter.filter({"user": "a", "text": ":bird::bird::bird:", "timestamp": time.time()})
        assert result is None

    def test_custom_emojis_cleaned(self, smart_aggregator_config):
        """TC3.1.5c: Custom emojis should be cleaned from mixed messages."""
        cfg = smart_aggregator_config["filter"]
        msg_filter = MessageFilter(cfg)
        result = msg_filter.filter({"user": "a", "text": "stop saying YDBAF you'll get banned :bird::bird::bird:", "timestamp": time.time()})
        assert result is not None
        assert ":bird:" not in result["text"]

    def test_whitelist_bypasses_filter(self, smart_aggregator_config):
        """TC3.1.6: VIP users should bypass the filter."""
        cfg = smart_aggregator_config["filter"].copy()
        cfg["whitelist"] = {"enabled": True, "users": ["vip_user"]}
        filter_vip = MessageFilter(cfg)
        result = filter_vip.filter({"user": "vip_user", "text": "hola", "timestamp": time.time()})
        assert result is not None

    def test_batch_filter_passes_some(self, smart_aggregator_config):
        """TC3.1.7: At least some messages from a batch should pass."""
        cfg = smart_aggregator_config["filter"]
        msg_filter = MessageFilter(cfg)
        passed = [m for m in MOCK_MESSAGES_200 if msg_filter.filter(m) is not None]
        assert len(passed) > 0


# --- TC3.2 — Vibe Thermometer ---

class TestVibeThermometer:
    """TC3.2: Vibe computation from chat messages."""

    def test_empty_window_returns_zero(self, smart_aggregator_config, mock_llm):
        """TC3.2.1: Empty window must return temperature 0."""
        cfg = smart_aggregator_config["vibe"]
        thermometer = VibeThermometer(cfg, llm_interface=mock_llm)
        vibe = thermometer.compute_vibe(force=True)
        assert vibe["temperature"] == 0.0

    def test_non_empty_window_positive_temperature(self, smart_aggregator_config, mock_llm):
        """TC3.2.2: Non-empty window must have temperature > 0."""
        cfg = smart_aggregator_config["vibe"]
        thermometer = VibeThermometer(cfg, llm_interface=mock_llm)
        for msg in VIBE_TEST_MESSAGES:
            thermometer.add_message(msg)
        vibe = thermometer.compute_vibe(force=True)
        assert vibe["temperature"] > 0

    def test_excitement_detected(self, smart_aggregator_config, mock_llm):
        """TC3.2.3: Excitement emotion should be > 0.5 for hype messages."""
        cfg = smart_aggregator_config["vibe"]
        thermometer = VibeThermometer(cfg, llm_interface=mock_llm)
        for msg in VIBE_TEST_MESSAGES:
            thermometer.add_message(msg)
        vibe = thermometer.compute_vibe(force=True)
        assert vibe["emotions"]["excitement"] > 0.5


# --- TC3.3 — Activity Trigger ---

class TestActivityTrigger:
    """TC3.3: Activity rate triggering."""

    def test_low_rate_no_trigger(self, smart_aggregator_config):
        """TC3.3.1: Low message rate should not trigger."""
        cfg = smart_aggregator_config["activity"].copy()
        cfg["threshold_per_second"] = 2.0
        cfg["cooldown_seconds"] = 0.0
        triggered = []

        def on_trigger(data):
            triggered.append(data)

        activity = ActivityTrigger(cfg, callbacks={"on_trigger": on_trigger})
        base = time.time()
        for i in range(5):
            activity.on_message({"user": f"u{i}", "text": "msg", "timestamp": base + i})
        assert len(triggered) == 0

    def test_high_rate_triggers(self, smart_aggregator_config):
        """TC3.3.2: High message rate should trigger."""
        cfg = smart_aggregator_config["activity"].copy()
        cfg["threshold_per_second"] = 2.0
        cfg["cooldown_seconds"] = 0.0
        triggered = []

        def on_trigger(data):
            triggered.append(data)

        activity = ActivityTrigger(cfg, callbacks={"on_trigger": on_trigger})
        base = time.time()
        for i in range(15):
            activity.on_message({"user": f"u{i}", "text": "msg", "timestamp": base + (i * 0.2)})
        assert len(triggered) > 0

    def test_configured_actions_included(self, smart_aggregator_config):
        """TC3.3.3: Configured actions should be included in trigger payload."""
        cfg = smart_aggregator_config["activity"].copy()
        cfg["threshold_per_second"] = 2.0
        cfg["cooldown_seconds"] = 0.0
        cfg["actions"] = {
            "auto_reply": {"enabled": True, "message": "Chat en pico"},
            "behavior_change": {"enabled": True, "parameter": "excitement_multiplier", "value": 1.5},
        }
        triggered = []

        def on_trigger(data):
            triggered.append(data)

        activity = ActivityTrigger(cfg, callbacks={"on_trigger": on_trigger})
        base = time.time()
        for i in range(15):
            activity.on_message({"user": f"u{i}", "text": "msg", "timestamp": base + (i * 0.2)})
        assert triggered[-1]["actions"]["auto_reply"] == "Chat en pico"
        assert triggered[-1]["actions"]["behavior_change"]["parameter"] == "excitement_multiplier"


# --- TC3.4 — Session History ---

class TestSessionHistory:
    """TC3.4: Session persistence with SQLite + JSONL."""

    def test_session_created_with_positive_id(self, temp_dir):
        """TC3.4.1: Session ID must be > 0."""
        db_path = os.path.join(temp_dir, "sessions.db")
        jl_path = os.path.join(temp_dir, "chat_log.jsonl")
        history = SessionHistory(db_path, jl_path, retention_hours=1)
        sid = history.start_session("youtube", "test_channel")
        assert sid > 0

    def test_messages_saved_and_retrieved(self, temp_dir):
        """TC3.4.2: All messages must be retrievable."""
        db_path = os.path.join(temp_dir, "sessions.db")
        jl_path = os.path.join(temp_dir, "chat_log.jsonl")
        history = SessionHistory(db_path, jl_path, retention_hours=1)
        sid = history.start_session("youtube", "test_channel")
        for msg in MOCK_MESSAGES_20:
            history.add_message(sid, msg, passed_filter=True, vibe=50.0)
        context = history.get_session_context(sid, max_messages=25)
        assert len(context) == 20

    def test_jsonl_contains_lines(self, temp_dir):
        """TC3.4.3: JSONL file must contain correct number of lines."""
        db_path = os.path.join(temp_dir, "sessions.db")
        jl_path = os.path.join(temp_dir, "chat_log.jsonl")
        history = SessionHistory(db_path, jl_path, retention_hours=1)
        sid = history.start_session("youtube", "test_channel")
        for msg in MOCK_MESSAGES_20:
            history.add_message(sid, msg, passed_filter=True, vibe=50.0)
        assert os.path.exists(jl_path)
        with open(jl_path, "r", encoding="utf-8") as f:
            lines = [l for l in f if l.strip()]
        assert len(lines) == 20

    def test_context_limited_by_max_messages(self, temp_dir):
        """TC3.4.4: Context must be limited to max_messages."""
        db_path = os.path.join(temp_dir, "sessions.db")
        jl_path = os.path.join(temp_dir, "chat_log.jsonl")
        history = SessionHistory(db_path, jl_path, retention_hours=1)
        sid = history.start_session("youtube", "test_channel")
        for msg in MOCK_MESSAGES_20:
            history.add_message(sid, msg, passed_filter=True, vibe=50.0)
        context = history.get_session_context(sid, max_messages=10)
        assert len(context) == 10

    def test_cleanup_removes_old_sessions(self, temp_dir):
        """TC3.4.5: Cleanup must remove old sessions from SQLite and JSONL."""
        db_path = os.path.join(temp_dir, "sessions.db")
        jl_path = os.path.join(temp_dir, "chat_log.jsonl")
        history = SessionHistory(db_path, jl_path, retention_hours=1)
        sid = history.start_session("youtube", "test_channel")
        for msg in MOCK_MESSAGES_20:
            history.add_message(sid, msg, passed_filter=True, vibe=50.0)

        conn = sqlite3.connect(db_path)
        conn.execute("UPDATE sessions SET start_time = ? WHERE id = ?", (time.time() - 7200, sid))
        conn.commit()
        conn.close()
        history.cleanup_old_sessions()
        context_after = history.get_session_context(sid, max_messages=100)
        assert len(context_after) == 0
        with open(jl_path, "r", encoding="utf-8") as f:
            remaining_lines = [l for l in f if l.strip()]
        assert len(remaining_lines) == 0


# --- TC3.5 — YouTube Chat Source ---

class TestYouTubeChatSource:
    """TC3.5: YouTube chat source connectivity."""

    def test_not_connected_initially(self, smart_aggregator_config):
        """TC3.5.3: Source should not be connected initially."""
        cfg = smart_aggregator_config["source"]
        source = YouTubeChatSource(cfg, callbacks={})
        assert not source.is_connected()

    def test_connect_fails_without_video_id(self, smart_aggregator_config):
        """TC3.5.1: Connect must fail without video_id."""
        cfg = smart_aggregator_config["source"]
        source = YouTubeChatSource(cfg, callbacks={})
        with pytest.raises(ValueError):
            source.connect("")

    def test_connect_handles_invalid_video_id(self, smart_aggregator_config):
        """TC3.5.2/5: Connect with invalid video_id handled gracefully."""
        cfg = smart_aggregator_config["source"]
        source = YouTubeChatSource(cfg, callbacks={})
        try:
            source.connect("invalid_video_id_123")
            time.sleep(0.5)
        except Exception as e:
            error_str = str(e).lower()
            assert "pytchat" in error_str or "video" in error_str or "invalid" in error_str

    def test_disconnect_without_connect(self, smart_aggregator_config):
        """TC3.5.4: Disconnect without prior connect should not crash."""
        cfg = smart_aggregator_config["source"]
        source = YouTubeChatSource(cfg, callbacks={})
        source.disconnect()


# --- TC3.6 — Aggregator Orchestration ---

class TestAggregator:
    """TC3.6: Full aggregator orchestration."""

    def test_filtered_messages_and_vibe_and_triggers(self, smart_aggregator_config, mock_llm, temp_dir):
        """TC3.6.1-3: Filtered messages, vibe computation, and activity triggers."""
        cfg = smart_aggregator_config.copy()
        config_path = os.path.join(temp_dir, "smart_aggregator.yaml")
        cfg["history"]["db_path"] = os.path.join(temp_dir, "sessions.db")
        cfg["history"]["jsonl_path"] = os.path.join(temp_dir, "chat_log.jsonl")
        with open(config_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(cfg, f, allow_unicode=True)

        agg = Aggregator(config_path=config_path, llm_interface=mock_llm)
        filtered_msgs = []
        vibes = []
        triggers = []

        agg.on_filtered_message = lambda m: filtered_msgs.append(m)
        agg.on_vibe_update = lambda v: vibes.append(v)
        agg.on_activity_trigger = lambda d: triggers.append(d)
        sid = agg.start_session("youtube", "headless_test")

        for msg in MOCK_MESSAGES_200:
            agg.process_message(msg)

        spike_base = time.time()
        for i in range(60):
            agg.process_message({"user": f"spike{i}", "text": "Mensaje valido de pico para probar actividad", "timestamp": spike_base + (i * 0.01)})

        assert len(filtered_msgs) > 0

        vibe = agg.thermometer.compute_vibe(force=True)
        if vibe:
            vibes.append(vibe)
        assert len(vibes) > 0

        assert len(triggers) > 0

    def test_session_persistence(self, smart_aggregator_config, mock_llm, temp_dir):
        """TC3.6.4: Aggregator persists session messages."""
        cfg = smart_aggregator_config.copy()
        config_path = os.path.join(temp_dir, "smart_aggregator.yaml")
        cfg["history"]["db_path"] = os.path.join(temp_dir, "sessions.db")
        cfg["history"]["jsonl_path"] = os.path.join(temp_dir, "chat_log.jsonl")
        with open(config_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(cfg, f, allow_unicode=True)

        agg = Aggregator(config_path=config_path, llm_interface=mock_llm)
        sid = agg.start_session("youtube", "headless_test")
        for msg in MOCK_MESSAGES_200:
            agg.process_message(msg)

        context = agg.history.get_session_context(sid, max_messages=300)
        assert len(context) > 0
        agg.disconnect()

    def test_optional_callbacks_no_failure(self, smart_aggregator_config, mock_llm, temp_dir):
        """TC3.6.5: Optional callbacks should not cause failures."""
        cfg = smart_aggregator_config.copy()
        config_path = os.path.join(temp_dir, "smart_aggregator.yaml")
        cfg["history"]["db_path"] = os.path.join(temp_dir, "sessions.db")
        cfg["history"]["jsonl_path"] = os.path.join(temp_dir, "chat_log.jsonl")
        with open(config_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(cfg, f, allow_unicode=True)

        agg2 = Aggregator(config_path=config_path, llm_interface=mock_llm)
        for msg in MOCK_MESSAGES_20[:5]:
            agg2.process_message(msg)


# --- TC3.7 — YouTube API (placeholder) ---

class TestYouTubeAPI:
    """TC3.7: YouTube API placeholder test."""

    def test_api_key_not_configured(self, smart_aggregator_config):
        """TC3.7: YouTube API key placeholder check."""
        cfg = smart_aggregator_config.get("youtube_api", {})
        assert cfg.get("api_key") == "${YOUTUBE_API_KEY}"
