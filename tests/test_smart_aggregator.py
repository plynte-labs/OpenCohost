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
import json
import sqlite3
import time
from copy import deepcopy
from unittest.mock import MagicMock, patch

import pytest
import yaml

from opencohost.smart_aggregator.session_history import SessionHistory
from opencohost.smart_aggregator.message_filter import MessageFilter
from opencohost.smart_aggregator.chat_source import YouTubeChatSource, TwitchChatSource
from opencohost.smart_aggregator.vibe_thermometer import VibeThermometer
from opencohost.smart_aggregator.activity_trigger import ActivityTrigger
from opencohost.smart_aggregator.aggregator import Aggregator
from opencohost.smart_aggregator.intent_aggregator import IntentAggregator, IntentClassifier
from opencohost.smart_aggregator.filter_policy import get_preset, list_presets, PRESETS
from opencohost.smart_aggregator.diagnostics import FilterDiagnostics

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

    def test_long_keyboard_smash_discarded(self, smart_aggregator_config):
        """TC3.1.5d: Long random-looking tokens should be discarded."""
        cfg = smart_aggregator_config["filter"]
        msg_filter = MessageFilter(cfg)
        result = msg_filter.filter({
            "user": "a",
            "text": "bjbd x kxxbfjjbeldbdhdicvyckveyfkcjuhdkudnejxvdxvdjdhdjxbyeb uy febeurjwuthfbehuxjbdlbdbshkxo holaaaaaaaaa",
            "timestamp": time.time(),
        })
        assert result is None

    def test_repeated_pvp_spam_discarded(self, smart_aggregator_config):
        """TC3.1.5e: Repeated syllable spam should be discarded."""
        cfg = smart_aggregator_config["filter"]
        msg_filter = MessageFilter(cfg)
        result = msg_filter.filter({
            "user": "a",
            "text": "pvppvpvpvpvppvpvpvpvpvpvpvppvpvpvpvpvpvp soy buenisimo abran porfa pvp papapapapapappapapapap",
            "timestamp": time.time(),
        })
        assert result is None

    def test_short_low_context_message_has_low_quality(self, smart_aggregator_config):
        """TC3.1.5f: Short low-context messages are scored below aggregator threshold."""
        cfg = smart_aggregator_config["filter"]
        msg_filter = MessageFilter(cfg)
        result = msg_filter.filter({"user": "a", "text": "hola me saludas bro", "timestamp": time.time()})
        assert result is not None
        assert result["quality"] < cfg["min_quality_score"]

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


# --- TC3.3b — Intent Aggregation ---

class TestIntentAggregator:
    """TC3.3b: Rule-based intent classification and summary."""

    def test_classifies_common_chat_intents(self):
        classifier = IntentClassifier({})
        assert classifier.classify("me saludas hoy es mi cumpleaños") == "greeting_request"
        assert classifier.classify("me puedo unir al server privado") == "join_request"
        assert classifier.classify("me mandas soli en roblox") == "roblox_friend"
        assert classifier.classify("pa cuando video con alguien famoso") == "video_collab"
        assert classifier.classify("que tradeas por un garama de 561M") == "trade_request"

    def test_creator_names_do_not_drive_intent(self):
        classifier = IntentClassifier({})
        assert classifier.classify("Abraham") == "other"
        assert classifier.classify("Fede") == "other"
        assert classifier.classify("Bros") == "other"
        assert classifier.classify("Fernanfloo") == "other"

    def test_extracts_runtime_entities_from_generic_patterns(self):
        classifier = IntentClassifier({})
        collab = classifier.classify_message("pa cuando video con los bros y Fede")
        suggestion = classifier.classify_message("deberías jugar Coraline")
        trade = classifier.classify_message("que tradeas por un garama de 561M")

        assert collab["intent"] == "video_collab"
        assert collab["entity"] == "los bros y fede"
        assert suggestion["intent"] == "game_suggestion"
        assert suggestion["entity"] == "coraline"
        assert trade["intent"] == "trade_request"
        assert "garama" in trade["entity"]

    def test_summarizes_top_intents_with_examples(self):
        agg = IntentAggregator({"top_intents": 3, "min_count": 2, "window_seconds": 60})
        base = time.time()
        messages = [
            "me saludas soy tu fan",
            "me saludas hoy es mi cumpleaños",
            "puedo jugar contigo porfa",
            "me puedo unir al server privado",
            "pa cuando video con los bros",
            "cuando regresan los bros",
        ]
        for i, text in enumerate(messages):
            agg.add_message({"user": f"u{i}", "text": text, "timestamp": base + i})

        summary = agg.summarize(now=base + 10)
        intents = [item["intent"] for item in summary["top_intents"]]
        assert "greeting_request" in intents
        assert "join_request" in intents
        assert "video_collab" in intents
        video_cluster = next(item for item in summary["top_intents"] if item["intent"] == "video_collab")
        assert video_cluster["entities"]
        assert "CONTEXTO PRIVADO" in summary["prompt"]
        assert "Ej:" not in summary["prompt"]
        assert "mensajes" not in summary["prompt"].lower()

    def test_prompt_does_not_leak_internal_summary_metadata(self):
        agg = IntentAggregator({"top_intents": 3, "min_count": 2, "window_seconds": 60})
        base = time.time()
        for i, text in enumerate([
            "me saludas hoy es mi cumpleaños soy tu fan",
            "me saludas soy tu fan desde pequeño por favor",
            "puedo jugar contigo porfa en el servidor privado",
            "me puedo unir al server privado para jugar contigo",
        ]):
            agg.add_message({"user": f"u{i}", "text": text, "timestamp": base + i})

        prompt = agg.summarize(now=base + 10)["prompt"]

        assert "CONTEXTO PRIVADO" in prompt
        assert "Ej:" not in prompt
        assert "Temas/personas" not in prompt
        assert "u0" not in prompt
        assert "mensajes" not in prompt.lower()

    def test_other_bucket_is_not_sent_as_dominant_theme_by_default(self):
        agg = IntentAggregator({"top_intents": 3, "min_count": 2, "window_seconds": 60})
        base = time.time()
        for i, text in enumerate([
            "comentario largo random sobre vitaly y cosas sin pregunta clara",
            "otra frase extensa mencionando vital pero sin una intención útil",
            "me saludas hoy es mi cumpleaños soy tu fan",
            "me saludas soy tu fan desde pequeño por favor",
        ]):
            agg.add_message({"user": f"u{i}", "text": text, "timestamp": base + i})

        intents = [item["intent"] for item in agg.summarize(now=base + 10)["top_intents"]]

        assert "greeting_request" in intents
        assert "other" not in intents

    def test_marks_duplicate_intent_messages(self):
        agg = IntentAggregator({"min_count": 1, "duplicate_window_seconds": 45.0})
        base = time.time()
        text = "ADMIN ABUSE ZOO CON AMIGOS RIVALS CAPITULO 2"
        agg.add_message({"user": "a", "text": text, "timestamp": base})
        agg.add_message({"user": "b", "text": text, "timestamp": base + 1})

        summary = agg.summarize(now=base + 2)
        top = summary["top_intents"][0]
        assert top["intent"] == "copypasta"
        assert top["duplicates"] == 1


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

    def test_raw_message_persistence_api_is_removed(self, temp_dir):
        """TC3.4.2: Raw chat persistence must be impossible from SessionHistory."""
        db_path = os.path.join(temp_dir, "sessions.db")
        jl_path = os.path.join(temp_dir, "chat_log.jsonl")
        history = SessionHistory(db_path, jl_path, retention_hours=1)
        sid = history.start_session("youtube", "test_channel")
        assert not hasattr(history, "add_message")
        assert history.get_session_context(sid, max_messages=25) == []

    def test_snapshots_do_not_create_raw_jsonl(self, temp_dir):
        """TC3.4.3: Compact snapshots must not create raw JSONL chat logs."""
        db_path = os.path.join(temp_dir, "sessions.db")
        jl_path = os.path.join(temp_dir, "chat_log.jsonl")
        history = SessionHistory(db_path, jl_path, retention_hours=1)
        sid = history.start_session("youtube", "test_channel")
        history.add_context_snapshot(sid, "contexto compacto", message_count=20)
        assert not os.path.exists(jl_path)

    def test_compact_context_limited_by_max_items(self, temp_dir):
        """TC3.4.4: Compact context must be limited to max_items."""
        db_path = os.path.join(temp_dir, "sessions.db")
        jl_path = os.path.join(temp_dir, "chat_log.jsonl")
        history = SessionHistory(db_path, jl_path, retention_hours=1)
        sid = history.start_session("youtube", "test_channel")
        for i in range(20):
            history.add_context_snapshot(sid, f"contexto compacto {i}", message_count=i)
        context = history.get_recent_context_snapshots(sid, max_items=10)
        assert len(context) == 10

    def test_compact_context_snapshots_saved_and_retrieved(self, temp_dir):
        """TC3.4.4b: Compact Kira contexts are persisted separately from raw chat."""
        db_path = os.path.join(temp_dir, "sessions.db")
        jl_path = os.path.join(temp_dir, "chat_log.jsonl")
        history = SessionHistory(db_path, jl_path, retention_hours=1)
        sid = history.start_session("youtube", "test_channel")

        history.add_context_snapshot(
            sid,
            "CONTEXTO PRIVADO DEL CHAT PARA KIRA: tema dominante de prueba",
            message_count=42,
            vibe=66.0,
            metadata={"top_intents": [{"intent": "game_question"}]},
        )

        snapshots = history.get_recent_context_snapshots(sid, max_items=5)
        assert len(snapshots) == 1
        assert snapshots[0]["message_count"] == 42
        assert snapshots[0]["metadata"]["top_intents"][0]["intent"] == "game_question"

    def test_cleanup_removes_old_sessions(self, temp_dir):
        """TC3.4.5: Cleanup must remove old sessions, snapshots, and legacy JSONL."""
        db_path = os.path.join(temp_dir, "sessions.db")
        jl_path = os.path.join(temp_dir, "chat_log.jsonl")
        history = SessionHistory(db_path, jl_path, retention_hours=1)
        sid = history.start_session("youtube", "test_channel")
        history.add_context_snapshot(sid, "contexto compacto", message_count=20)
        with open(jl_path, "w", encoding="utf-8") as f:
            f.write(json.dumps({"session_id": sid, "text": "legacy raw"}, ensure_ascii=False) + "\n")

        conn = sqlite3.connect(db_path)
        conn.execute("UPDATE sessions SET start_time = ? WHERE id = ?", (time.time() - 7200, sid))
        conn.commit()
        conn.close()
        history.cleanup_old_sessions()
        context_after = history.get_session_context(sid, max_messages=100)
        assert len(context_after) == 0
        snapshots_after = history.get_recent_context_snapshots(sid, max_items=100)
        assert len(snapshots_after) == 0
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
        cfg = deepcopy(smart_aggregator_config)
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
            agg.process_message({"user": f"spike{i}", "text": "Mensaje valido de pico para probar actividad intensa del chat", "timestamp": spike_base + (i * 0.01)})

        assert len(filtered_msgs) > 0

        vibe = agg.thermometer.compute_vibe(force=True)
        if vibe:
            vibes.append(vibe)
        assert len(vibes) > 0

        assert len(triggers) > 0

    def test_aggregated_context_includes_intent_summary(self, smart_aggregator_config, mock_llm, temp_dir):
        """TC3.6.3b: Activity context should include ranked intent summary."""
        cfg = deepcopy(smart_aggregator_config)
        config_path = os.path.join(temp_dir, "smart_aggregator.yaml")
        cfg["history"]["db_path"] = os.path.join(temp_dir, "sessions.db")
        cfg["history"]["jsonl_path"] = os.path.join(temp_dir, "chat_log.jsonl")
        cfg["activity"]["threshold_per_second"] = 1.0
        cfg["activity"]["cooldown_seconds"] = 0.0
        cfg["intent"]["min_count"] = 2
        with open(config_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(cfg, f, allow_unicode=True)

        agg = Aggregator(config_path=config_path, llm_interface=mock_llm)
        contexts = []
        agg.on_aggregated_context = lambda data: contexts.append(data)
        agg.start_session("youtube", "headless_test")

        base = time.time()
        for i, text in enumerate([
            "me saludas hoy es mi cumpleaños soy tu fan",
            "me saludas soy tu fan desde pequeño por favor",
            "me puedo unir al server privado para jugar contigo",
            "puedo jugar contigo porfa en el servidor privado",
            "pa cuando video con los bros y con Fede",
            "cuando regresan los bros para grabar otro video juntos",
        ]):
            agg.process_message({"user": f"u{i}", "text": text, "timestamp": base + (i * 0.01)})

        assert contexts
        summary = contexts[-1]["intent_summary"]
        assert summary["top_intents"]
        assert "CONTEXTO PRIVADO" in summary["prompt"]

    def test_high_traffic_samples_context_and_skips_vibe_llm(self, smart_aggregator_config, temp_dir):
        """Massive streams should compact/sample instead of calling vibe LLM per spike."""
        cfg = deepcopy(smart_aggregator_config)
        config_path = os.path.join(temp_dir, "smart_aggregator.yaml")
        cfg["history"]["db_path"] = os.path.join(temp_dir, "sessions.db")
        cfg["history"]["jsonl_path"] = os.path.join(temp_dir, "chat_log.jsonl")
        cfg["vibe"]["window_seconds"] = 60
        cfg["activity"]["threshold_per_second"] = 10.0
        cfg["live_safety"] = {
            "enabled": True,
            "high_traffic_threshold_per_second": 10.0,
            "high_traffic_sample_every": 10,
            "state_log_interval_seconds": 0.0,
        }
        with open(config_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(cfg, f, allow_unicode=True)

        llm_calls = []
        agg = Aggregator(config_path=config_path, llm_interface=lambda prompt: llm_calls.append(prompt) or {"emotions": {"neutral": 1.0}, "temperature": 50})
        logs = []
        agg.on_live_safety_log = logs.append
        base = time.time()

        for i in range(70):
            agg.process_message({"user": f"u{i}", "text": "mensaje valido con suficiente contexto para directo masivo", "timestamp": base + (i * 0.01)})

        assert llm_calls == []
        assert agg.intent_aggregator.summarize(now=base + 1)["total_messages"] < 70
        assert any("high traffic ON" in line for line in logs)

    def test_empty_vibe_responses_enter_backoff(self, smart_aggregator_config, temp_dir):
        cfg = deepcopy(smart_aggregator_config)
        config_path = os.path.join(temp_dir, "smart_aggregator.yaml")
        cfg["history"]["db_path"] = os.path.join(temp_dir, "sessions.db")
        cfg["history"]["jsonl_path"] = os.path.join(temp_dir, "chat_log.jsonl")
        cfg["vibe"]["window_seconds"] = 0
        cfg["activity"]["threshold_per_second"] = 99.0
        cfg["live_safety"] = {
            "enabled": True,
            "high_traffic_threshold_per_second": 99.0,
            "empty_vibe_backoff_after": 2,
            "empty_vibe_backoff_seconds": 120.0,
            "state_log_interval_seconds": 0.0,
        }
        with open(config_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(cfg, f, allow_unicode=True)

        calls = []
        agg = Aggregator(config_path=config_path, llm_interface=lambda prompt: calls.append(prompt) or "")
        logs = []
        agg.on_live_safety_log = logs.append
        base = time.time()

        for i in range(3):
            agg.process_message({"user": f"u{i}", "text": "mensaje valido para probar backoff de respuestas vacias", "timestamp": base})

        assert len(calls) == 2
        assert agg._live_vibe_backoff_until > time.time()
        assert any("Vibe en cooldown" in line for line in logs)

    def test_session_persistence(self, smart_aggregator_config, mock_llm, temp_dir):
        """TC3.6.4: Aggregator persists compact Kira contexts, not raw chat by default."""
        cfg = deepcopy(smart_aggregator_config)
        config_path = os.path.join(temp_dir, "smart_aggregator.yaml")
        cfg["history"]["db_path"] = os.path.join(temp_dir, "sessions.db")
        cfg["history"]["jsonl_path"] = os.path.join(temp_dir, "chat_log.jsonl")
        with open(config_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(cfg, f, allow_unicode=True)

        agg = Aggregator(config_path=config_path, llm_interface=mock_llm)
        sid = agg.start_session("youtube", "headless_test")
        base = time.time()
        for i in range(60):
            agg.process_message({
                "user": f"spike{i}",
                "text": "me saludas hoy es mi cumpleaños soy tu fan de Kira",
                "timestamp": base + (i * 0.01),
            })

        raw_context = agg.history.get_session_context(sid, max_messages=300)
        compact_context = agg.history.get_recent_context_snapshots(sid, max_items=10)
        assert raw_context == []
        assert len(compact_context) > 0
        agg.disconnect()

    def test_raw_message_persistence_config_is_ignored(self, smart_aggregator_config, mock_llm, temp_dir):
        """TC3.6.4b: Raw chat remains impossible even if legacy config flags exist."""
        cfg = deepcopy(smart_aggregator_config)
        config_path = os.path.join(temp_dir, "smart_aggregator.yaml")
        cfg["history"]["db_path"] = os.path.join(temp_dir, "sessions.db")
        cfg["history"]["jsonl_path"] = os.path.join(temp_dir, "chat_log.jsonl")
        cfg["history"]["persist_raw_messages"] = True
        cfg["history"]["persist_rejected_messages"] = False
        with open(config_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(cfg, f, allow_unicode=True)

        agg = Aggregator(config_path=config_path, llm_interface=mock_llm)
        sid = agg.start_session("youtube", "headless_test")
        agg.process_message({"user": "spam", "text": "😂", "timestamp": time.time()})
        agg.process_message({"user": "ok", "text": "me saludas hoy es mi cumpleaños soy tu fan", "timestamp": time.time() + 1})

        context = agg.history.get_session_context(sid, max_messages=10)
        assert context == []
        assert not os.path.exists(cfg["history"]["jsonl_path"])

    def test_optional_callbacks_no_failure(self, smart_aggregator_config, mock_llm, temp_dir):
        """TC3.6.5: Optional callbacks should not cause failures."""
        cfg = deepcopy(smart_aggregator_config)
        config_path = os.path.join(temp_dir, "smart_aggregator.yaml")
        cfg["history"]["db_path"] = os.path.join(temp_dir, "sessions.db")
        cfg["history"]["jsonl_path"] = os.path.join(temp_dir, "chat_log.jsonl")
        with open(config_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(cfg, f, allow_unicode=True)

        agg2 = Aggregator(config_path=config_path, llm_interface=mock_llm)
        for msg in MOCK_MESSAGES_20[:5]:
            agg2.process_message(msg)

    # --- Aggregator Factory Tests (T-14, REQ-17..20) ---

    def test_factory_creates_youtube_source(self, smart_aggregator_config, mock_llm, temp_dir):
        """REQ-17/18: connect() with default platform creates YouTubeChatSource."""
        cfg = deepcopy(smart_aggregator_config)
        config_path = os.path.join(temp_dir, "smart_aggregator.yaml")
        cfg["history"]["db_path"] = os.path.join(temp_dir, "sessions.db")
        cfg["history"]["jsonl_path"] = os.path.join(temp_dir, "chat_log.jsonl")
        with open(config_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(cfg, f, allow_unicode=True)

        agg = Aggregator(config_path=config_path, llm_interface=mock_llm)

        with patch.object(YouTubeChatSource, "connect") as mock_connect:
            agg.connect("test123")
            assert isinstance(agg.source, YouTubeChatSource)
            mock_connect.assert_called_once_with("test123")

    def test_factory_creates_twitch_source(self, smart_aggregator_config, mock_llm, temp_dir):
        """REQ-17/18: connect() with platform='twitch' creates TwitchChatSource."""
        cfg = deepcopy(smart_aggregator_config)
        config_path = os.path.join(temp_dir, "smart_aggregator.yaml")
        cfg["history"]["db_path"] = os.path.join(temp_dir, "sessions.db")
        cfg["history"]["jsonl_path"] = os.path.join(temp_dir, "chat_log.jsonl")
        with open(config_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(cfg, f, allow_unicode=True)

        agg = Aggregator(config_path=config_path, llm_interface=mock_llm)

        with patch.object(TwitchChatSource, "connect") as mock_connect:
            agg.connect("testchannel", platform="twitch")
            assert isinstance(agg.source, TwitchChatSource)
            mock_connect.assert_called_once_with("testchannel")

    def test_factory_rejects_unknown_platform(self, smart_aggregator_config, mock_llm, temp_dir):
        """REQ-18: connect() with unknown platform raises ValueError."""
        cfg = deepcopy(smart_aggregator_config)
        config_path = os.path.join(temp_dir, "smart_aggregator.yaml")
        cfg["history"]["db_path"] = os.path.join(temp_dir, "sessions.db")
        cfg["history"]["jsonl_path"] = os.path.join(temp_dir, "chat_log.jsonl")
        with open(config_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(cfg, f, allow_unicode=True)

        agg = Aggregator(config_path=config_path, llm_interface=mock_llm)
        with pytest.raises(ValueError, match="Plataforma no soportada"):
            agg.connect("test", platform="kick")

    def test_should_consider_vibe_without_video_id(self, smart_aggregator_config, temp_dir):
        """REQ-20: _should_consider_vibe works without _video_id attribute."""
        cfg = deepcopy(smart_aggregator_config)
        config_path = os.path.join(temp_dir, "smart_aggregator.yaml")
        cfg["history"]["db_path"] = os.path.join(temp_dir, "sessions.db")
        cfg["history"]["jsonl_path"] = os.path.join(temp_dir, "chat_log.jsonl")
        with open(config_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(cfg, f, allow_unicode=True)

        agg = Aggregator(config_path=config_path)

        # No source connected — should consider vibe
        assert agg._should_consider_vibe(1.0) is True

        # Source connected — should consider vibe
        mock_source = MagicMock()
        mock_source.is_connected.return_value = True
        agg._source = mock_source
        assert agg._should_consider_vibe(1.0) is True

        # Source disconnected — should NOT consider vibe
        mock_source.is_connected.return_value = False
        assert agg._should_consider_vibe(1.0) is False

    def test_youtube_backward_compat_still_works(self, smart_aggregator_config, mock_llm, temp_dir):
        """Backward compat: connect() without platform still works for YouTube."""
        cfg = deepcopy(smart_aggregator_config)
        config_path = os.path.join(temp_dir, "smart_aggregator.yaml")
        cfg["history"]["db_path"] = os.path.join(temp_dir, "sessions.db")
        cfg["history"]["jsonl_path"] = os.path.join(temp_dir, "chat_log.jsonl")
        with open(config_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(cfg, f, allow_unicode=True)

        agg = Aggregator(config_path=config_path, llm_interface=mock_llm)

        with patch.object(YouTubeChatSource, "connect") as mock_connect:
            agg.connect("dQw4w9WgXcQ")
            assert isinstance(agg.source, YouTubeChatSource)
            mock_connect.assert_called_once_with("dQw4w9WgXcQ")

    def test_disconnect_handles_no_source(self, smart_aggregator_config, temp_dir):
        """REQ-17: disconnect() does not crash when no source exists."""
        cfg = deepcopy(smart_aggregator_config)
        config_path = os.path.join(temp_dir, "smart_aggregator.yaml")
        cfg["history"]["db_path"] = os.path.join(temp_dir, "sessions.db")
        cfg["history"]["jsonl_path"] = os.path.join(temp_dir, "chat_log.jsonl")
        with open(config_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(cfg, f, allow_unicode=True)

        agg = Aggregator(config_path=config_path)
        # No source — disconnect should not crash
        agg.disconnect()

    def test_source_property_returns_none_initially(self, smart_aggregator_config, temp_dir):
        """source property returns None when no source is connected."""
        cfg = deepcopy(smart_aggregator_config)
        config_path = os.path.join(temp_dir, "smart_aggregator.yaml")
        cfg["history"]["db_path"] = os.path.join(temp_dir, "sessions.db")
        cfg["history"]["jsonl_path"] = os.path.join(temp_dir, "chat_log.jsonl")
        with open(config_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(cfg, f, allow_unicode=True)

        agg = Aggregator(config_path=config_path)
        assert agg.source is None


# --- TC3.7 — YouTube API (placeholder) ---

class TestYouTubeAPI:
    """TC3.7: YouTube API placeholder test."""

    def test_api_key_not_configured(self, smart_aggregator_config):
        """TC3.7: YouTube API key placeholder check."""
        cfg = smart_aggregator_config.get("youtube_api", {})
        assert cfg.get("api_key") == "${YOUTUBE_API_KEY}"


# --- TC4.1 — Filter Policy ---

class TestFilterPolicy:
    """TC4.1: Filter policy presets."""

    def test_balanced_preset_matches_existing_defaults(self, smart_aggregator_config):
        preset = get_preset("balanced")
        assert preset is not None
        assert preset["min_words"] == 3
        assert preset["min_char_length"] == 10
        assert preset["min_quality_score"] == 0.5
        assert preset["discard_gibberish"] is True
        assert preset["discard_ascii_art"] is True
        assert preset["discard_repetitive_chars"] is True
        assert preset["discard_repeated_words"] is True

    def test_twitch_relaxed_relaxes_only_size_and_quality(self):
        preset = get_preset("twitch_relaxed")
        assert preset is not None
        assert preset["min_words"] == 1
        assert preset["min_char_length"] == 1
        assert preset["min_quality_score"] == 0.0
        assert preset["discard_gibberish"] is True
        assert preset["discard_ascii_art"] is True
        assert preset["discard_repetitive_chars"] is True
        assert preset["discard_repeated_words"] is True
        assert preset["discard_links"] is True
        assert preset["discard_mentions"] is True

    def test_strict_preset_is_stricter_than_balanced(self):
        preset = get_preset("strict")
        assert preset is not None
        assert preset["min_words"] >= 3
        assert preset["min_char_length"] >= 10
        assert preset["min_quality_score"] >= 0.5
        assert preset["discard_gibberish"] is True
        assert preset["discard_ascii_art"] is True

    def test_unknown_preset_returns_none(self):
        assert get_preset("nonexistent") is None

    def test_list_presets_includes_all_three(self):
        names = list_presets()
        assert "balanced" in names
        assert "twitch_relaxed" in names
        assert "strict" in names

    def test_presets_are_readonly(self):
        with pytest.raises(TypeError):
            PRESETS["balanced"] = {}

    def test_all_critical_filters_enabled_in_all_presets(self):
        for name in ("balanced", "twitch_relaxed", "strict"):
            preset = get_preset(name)
            assert preset["discard_gibberish"], f"{name} must enable discard_gibberish"
            assert preset["discard_ascii_art"], f"{name} must enable discard_ascii_art"
            assert preset["discard_repetitive_chars"], f"{name} must enable discard_repetitive_chars"
            assert preset["discard_repeated_words"], f"{name} must enable discard_repeated_words"


# --- TC4.2 — Filter Diagnostics ---

class TestFilterDiagnostics:
    """TC4.2: FilterDiagnostics counters."""

    def test_initial_counts_are_zero(self):
        diag = FilterDiagnostics()
        d = diag.get_diagnostics()
        assert d["seen"] == 0
        assert d["accepted"] == 0
        assert d["rejected"] == 0
        assert d["by_reason"] == {}

    def test_seen_increments(self):
        diag = FilterDiagnostics()
        diag.record_seen()
        diag.record_seen()
        assert diag.get_diagnostics()["seen"] == 2

    def test_accepted_increments(self):
        diag = FilterDiagnostics()
        diag.record_accepted()
        diag.record_accepted()
        assert diag.get_diagnostics()["accepted"] == 2

    def test_rejected_increments_with_reason(self):
        diag = FilterDiagnostics()
        diag.record_rejected("empty")
        diag.record_rejected("empty")
        diag.record_rejected("link")
        d = diag.get_diagnostics()
        assert d["rejected"] == 3
        assert d["by_reason"]["empty"] == 2
        assert d["by_reason"]["link"] == 1

    def test_reset_clears_all(self):
        diag = FilterDiagnostics()
        diag.record_seen()
        diag.record_accepted()
        diag.record_rejected("spam")
        diag.reset_diagnostics()
        d = diag.get_diagnostics()
        assert d["seen"] == 0
        assert d["accepted"] == 0
        assert d["rejected"] == 0
        assert d["by_reason"] == {}

    def test_diagnostics_is_safe_copy(self):
        diag = FilterDiagnostics()
        diag.record_rejected("link")
        d = diag.get_diagnostics()
        d["by_reason"]["link"] = 999
        assert diag.get_diagnostics()["by_reason"]["link"] == 1

    def test_no_raw_messages_in_diagnostics(self):
        diag = FilterDiagnostics()
        diag.record_seen()
        diag.record_rejected("empty")
        d = diag.get_diagnostics()
        assert "messages" not in d
        assert "raw" not in d
        assert "text" not in d
        assert isinstance(d["by_reason"], dict)
        for key in d["by_reason"]:
            assert isinstance(key, str)
            assert isinstance(d["by_reason"][key], int)


# --- TC4.3 — MessageFilter Rejection Reason ---

class TestMessageFilterRejectionReason:
    """TC4.3: explain_filter and last_rejection_reason."""

    def test_rejection_reason_empty(self, smart_aggregator_config):
        cfg = smart_aggregator_config["filter"]
        mf = MessageFilter(cfg)
        result = mf.filter({"user": "a", "text": "", "timestamp": time.time()})
        assert result is None
        assert mf.last_rejection_reason == "empty"

    def test_rejection_reason_too_short_words(self, smart_aggregator_config):
        cfg = smart_aggregator_config["filter"]
        mf = MessageFilter(cfg)
        result = mf.filter({"user": "a", "text": "hello world", "timestamp": time.time()})
        assert result is None
        assert mf.last_rejection_reason == "too_short_words"

    def test_rejection_reason_link(self, smart_aggregator_config):
        cfg = smart_aggregator_config["filter"]
        mf = MessageFilter(cfg)
        msg = {"user": "a", "text": "mira https://twitch.tv/test", "timestamp": time.time()}
        result = mf.filter(msg)
        assert result is None
        assert mf.last_rejection_reason == "link"

    def test_rejection_reason_mention(self, smart_aggregator_config):
        cfg = smart_aggregator_config["filter"]
        mf = MessageFilter(cfg)
        result = mf.filter({"user": "a", "text": "@Kira hola", "timestamp": time.time()})
        assert result is None
        assert mf.last_rejection_reason == "mention"

    def test_rejection_reason_emoji_only(self, smart_aggregator_config):
        cfg = smart_aggregator_config["filter"].copy()
        cfg["min_words"] = 1
        cfg["min_char_length"] = 1
        mf = MessageFilter(cfg)
        result = mf.filter({"user": "a", "text": "🔥🔥", "timestamp": time.time()})
        assert result is None
        assert mf.last_rejection_reason == "emoji_only"

    def test_rejection_reason_gibberish(self, smart_aggregator_config):
        cfg = smart_aggregator_config["filter"].copy()
        cfg["min_words"] = 1
        mf = MessageFilter(cfg)
        result = mf.filter({
            "user": "a",
            "text": "bjbd kxxbfjjbeldbdhdicvyckveyfkcjuhdkudnejxvdxvdjdhdjxbyeb",
            "timestamp": time.time(),
        })
        assert result is None
        assert mf.last_rejection_reason == "gibberish"

    def test_explain_filter_returns_none_for_accepted(self, smart_aggregator_config):
        cfg = smart_aggregator_config["filter"]
        mf = MessageFilter(cfg)
        msg = {"user": "a", "text": "Kira que juego estas jugando?", "timestamp": time.time()}
        reason = mf.explain_filter(msg)
        assert reason is None

    def test_explain_filter_returns_reason_for_rejected(self, smart_aggregator_config):
        cfg = smart_aggregator_config["filter"]
        mf = MessageFilter(cfg)
        reason = mf.explain_filter({"user": "a", "text": "hello world", "timestamp": time.time()})
        assert reason == "too_short_words"

    def test_filter_contract_unchanged(self, smart_aggregator_config):
        cfg = smart_aggregator_config["filter"]
        mf = MessageFilter(cfg)
        result = mf.filter({"user": "a", "text": "Kira que juego estas jugando?", "timestamp": time.time()})
        assert result is not None
        assert "user" in result
        assert "text" in result
        assert "timestamp" in result
        assert "quality" in result
        assert "rejection_reason" not in result


# --- TC4.4 — Aggregator Filter Policy + Diagnostics ---

class TestAggregatorFilterPolicy:
    """TC4.4: Aggregator set_filter_policy, get_diagnostics, reset_diagnostics."""

    def test_set_filter_policy_applies_preset(self, smart_aggregator_config, temp_dir):
        cfg = deepcopy(smart_aggregator_config)
        config_path = os.path.join(temp_dir, "smart_aggregator.yaml")
        cfg["history"]["db_path"] = os.path.join(temp_dir, "sessions.db")
        cfg["history"]["jsonl_path"] = os.path.join(temp_dir, "chat_log.jsonl")
        with open(config_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(cfg, f, allow_unicode=True)

        agg = Aggregator(config_path=config_path)
        agg.set_filter_policy("twitch_relaxed")
        assert agg.get_filter_policy() == "twitch_relaxed"
        assert agg.msg_filter.min_words == 1
        assert agg.msg_filter.min_char_length == 1
        assert agg.msg_filter.min_quality_score == 0.0
        assert agg.msg_filter.discard_gibberish is True

    def test_set_filter_policy_unknown_raises(self, smart_aggregator_config, temp_dir):
        cfg = deepcopy(smart_aggregator_config)
        config_path = os.path.join(temp_dir, "smart_aggregator.yaml")
        cfg["history"]["db_path"] = os.path.join(temp_dir, "sessions.db")
        cfg["history"]["jsonl_path"] = os.path.join(temp_dir, "chat_log.jsonl")
        with open(config_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(cfg, f, allow_unicode=True)

        agg = Aggregator(config_path=config_path)
        with pytest.raises(ValueError, match="Unknown filter preset"):
            agg.set_filter_policy("invalid_preset")

    def test_relaxed_passes_short_twitch_messages(self, smart_aggregator_config, temp_dir):
        cfg = deepcopy(smart_aggregator_config)
        config_path = os.path.join(temp_dir, "smart_aggregator.yaml")
        cfg["history"]["db_path"] = os.path.join(temp_dir, "sessions.db")
        cfg["history"]["jsonl_path"] = os.path.join(temp_dir, "chat_log.jsonl")
        with open(config_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(cfg, f, allow_unicode=True)

        agg = Aggregator(config_path=config_path)
        agg.set_filter_policy("twitch_relaxed")

        accepted = []
        agg.on_filtered_message = accepted.append

        short_msgs = [
            {"user": "u1", "text": "L", "timestamp": time.time()},
            {"user": "u2", "text": "F", "timestamp": time.time() + 1},
            {"user": "u3", "text": "hola", "timestamp": time.time() + 2},
            {"user": "u4", "text": "gg", "timestamp": time.time() + 3},
            {"user": "u5", "text": "Kira juegas?", "timestamp": time.time() + 4},
            {"user": "u6", "text": "buena jugada bro", "timestamp": time.time() + 5},
        ]
        for msg in short_msgs:
            agg.process_message(msg)

        assert len(accepted) >= 4

    def test_balanced_preserves_current_defaults(self, smart_aggregator_config, temp_dir):
        cfg = deepcopy(smart_aggregator_config)
        config_path = os.path.join(temp_dir, "smart_aggregator.yaml")
        cfg["history"]["db_path"] = os.path.join(temp_dir, "sessions.db")
        cfg["history"]["jsonl_path"] = os.path.join(temp_dir, "chat_log.jsonl")
        with open(config_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(cfg, f, allow_unicode=True)

        agg = Aggregator(config_path=config_path)
        assert agg.msg_filter.min_words == 3
        assert agg.msg_filter.min_char_length == 10
        assert agg.msg_filter.min_quality_score == 0.5

        agg.set_filter_policy("balanced")
        assert agg.msg_filter.min_words == 3
        assert agg.msg_filter.min_char_length == 10
        assert agg.msg_filter.min_quality_score == 0.5

    def test_strict_preset_rejects_balanced_messages(self, smart_aggregator_config, temp_dir):
        cfg = deepcopy(smart_aggregator_config)
        config_path = os.path.join(temp_dir, "smart_aggregator.yaml")
        cfg["history"]["db_path"] = os.path.join(temp_dir, "sessions.db")
        cfg["history"]["jsonl_path"] = os.path.join(temp_dir, "chat_log.jsonl")
        with open(config_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(cfg, f, allow_unicode=True)

        agg = Aggregator(config_path=config_path)
        agg.set_filter_policy("strict")

        accepted = []
        agg.on_filtered_message = accepted.append

        msg = {"user": "a", "text": "ok buena jugada", "timestamp": time.time()}
        agg.process_message(msg)

        assert len(accepted) == 0

    def test_diagnostics_counters_increment(self, smart_aggregator_config, temp_dir):
        cfg = deepcopy(smart_aggregator_config)
        config_path = os.path.join(temp_dir, "smart_aggregator.yaml")
        cfg["history"]["db_path"] = os.path.join(temp_dir, "sessions.db")
        cfg["history"]["jsonl_path"] = os.path.join(temp_dir, "chat_log.jsonl")
        with open(config_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(cfg, f, allow_unicode=True)

        agg = Aggregator(config_path=config_path)
        agg.set_filter_policy("twitch_relaxed")

        agg.process_message({"user": "u1", "text": "🔥🔥🔥", "timestamp": time.time()})
        agg.process_message({"user": "u2", "text": "hola", "timestamp": time.time() + 1})
        agg.process_message({"user": "u3", "text": "Kira que juegas hoy amiga?", "timestamp": time.time() + 2})

        d = agg.get_diagnostics()
        assert d["seen"] == 3
        assert d["accepted"] >= 1
        assert d["rejected"] >= 1
        assert len(d["by_reason"]) >= 1

    def test_reset_diagnostics_clears_all(self, smart_aggregator_config, temp_dir):
        cfg = deepcopy(smart_aggregator_config)
        config_path = os.path.join(temp_dir, "smart_aggregator.yaml")
        cfg["history"]["db_path"] = os.path.join(temp_dir, "sessions.db")
        cfg["history"]["jsonl_path"] = os.path.join(temp_dir, "chat_log.jsonl")
        with open(config_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(cfg, f, allow_unicode=True)

        agg = Aggregator(config_path=config_path)
        agg.process_message({"user": "u1", "text": "hola Kira que tal todo bien?", "timestamp": time.time()})

        agg.reset_diagnostics()
        d = agg.get_diagnostics()
        assert d["seen"] == 0
        assert d["accepted"] == 0
        assert d["rejected"] == 0

    def test_get_filter_policy_default(self, smart_aggregator_config, temp_dir):
        cfg = deepcopy(smart_aggregator_config)
        config_path = os.path.join(temp_dir, "smart_aggregator.yaml")
        cfg["history"]["db_path"] = os.path.join(temp_dir, "sessions.db")
        cfg["history"]["jsonl_path"] = os.path.join(temp_dir, "chat_log.jsonl")
        with open(config_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(cfg, f, allow_unicode=True)

        agg = Aggregator(config_path=config_path)
        assert agg.get_filter_policy() == "balanced"

    def test_strict_preset_available_for_cohost(self, smart_aggregator_config, temp_dir):
        cfg = deepcopy(smart_aggregator_config)
        config_path = os.path.join(temp_dir, "smart_aggregator.yaml")
        cfg["history"]["db_path"] = os.path.join(temp_dir, "sessions.db")
        cfg["history"]["jsonl_path"] = os.path.join(temp_dir, "chat_log.jsonl")
        with open(config_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(cfg, f, allow_unicode=True)

        agg = Aggregator(config_path=config_path)
        agg.set_filter_policy("strict")
        assert agg.get_filter_policy() == "strict"
        assert agg.msg_filter.min_words == 4
        assert agg.msg_filter.min_char_length == 15
        assert agg.msg_filter.min_quality_score == 0.6
