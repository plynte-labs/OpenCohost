import os
import yaml
import time
from collections import defaultdict, deque
from typing import Optional, Callable

from .session_history import SessionHistory
from .message_filter import MessageFilter
from .chat_source import YouTubeChatSource, TwitchChatSource
from .vibe_thermometer import VibeThermometer
from .activity_trigger import ActivityTrigger
from .intent_aggregator import IntentAggregator
from .chat_input_contract import (
    INPUT_CONTRACT_SHADOW_MODE,
    ChatContextPacketBuilder,
    ChatEventDetector,
)

class Aggregator:
    def __init__(self, config_path: str = "config/smart_aggregator.yaml", llm_interface: Optional[Callable] = None):
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if not os.path.isabs(config_path):
            config_path = os.path.join(base_dir, config_path)
        
        with open(config_path, "r", encoding="utf-8") as f:
            self.config = yaml.safe_load(f)
        
        hist_cfg = self.config.get("history", {})
        db_path = hist_cfg.get("db_path", "data/smart_aggregator/sessions.db")
        jl_path = hist_cfg.get("jsonl_path", "data/smart_aggregator/chat_log.jsonl")
        retention = hist_cfg.get("retention_hours", 168)
        
        if not os.path.isabs(db_path):
            db_path = os.path.join(base_dir, db_path)
        if not os.path.isabs(jl_path):
            jl_path = os.path.join(base_dir, jl_path)
        
        self.history = SessionHistory(db_path, jl_path, retention)
        try:
            self.history.cleanup_old_sessions()
        except Exception:
            pass
        self.msg_filter = MessageFilter(self.config.get("filter", {}))
        self.thermometer = VibeThermometer(
            self.config.get("vibe", {}),
            llm_interface=llm_interface,
            is_busy_callback=self._check_busy
        )
        self.activity = ActivityTrigger(
            self.config.get("activity", {}),
            callbacks={"on_trigger": self._on_activity_trigger}
        )
        self.intent_aggregator = IntentAggregator(self.config.get("intent", {}))
        self._source = None
        self._source_factory = {
            "youtube": YouTubeChatSource,
            "twitch": TwitchChatSource,
        }

        self.on_filtered_message: Optional[Callable] = None
        self.on_vibe_update: Optional[Callable] = None
        self.on_activity_trigger: Optional[Callable] = None
        self.on_aggregated_context: Optional[Callable] = None
        self.on_live_safety_log: Optional[Callable[[str], None]] = None
        self.on_source_error: Optional[Callable] = None
        self.on_source_connect: Optional[Callable] = None
        self.on_source_disconnect: Optional[Callable] = None
        
        self._session_id: Optional[int] = None
        self._busy_callback: Optional[Callable[[], bool]] = None
        self._load_spam_config()
        self._load_live_safety_config()
    
    def set_busy_callback(self, callback: Callable[[], bool]):
        self._busy_callback = callback
        self.thermometer._is_busy_callback = callback

    @property
    def source(self):
        """Return the current chat source (backward-compat property)."""
        return self._source

    def set_llm_interface(self, llm_interface: Optional[Callable]):
        self.thermometer.set_llm_interface(llm_interface)

    def set_spam_limits(self, max_messages_per_user: Optional[int] = None, user_window_seconds: Optional[float] = None):
        if max_messages_per_user is not None:
            self._spam_max_messages = max(1, int(max_messages_per_user))
        if user_window_seconds is not None:
            self._spam_user_window = max(1.0, float(user_window_seconds))

    def set_activity_limits(self, threshold_per_second: Optional[float] = None, cooldown_seconds: Optional[float] = None, reset: bool = False):
        if threshold_per_second is not None:
            self.activity.threshold_per_second = max(0.01, float(threshold_per_second))
        if cooldown_seconds is not None:
            self.activity.cooldown_seconds = max(0.0, float(cooldown_seconds))
        if reset:
            self.activity.reset()
    
    def _check_busy(self) -> bool:
        if self._busy_callback:
            try:
                return self._busy_callback()
            except Exception:
                return False
        return False
    
    def connect(self, source_id: str, platform: str = "youtube"):
        if self._source is not None:
            self._source.disconnect()

        source_cls = self._source_factory.get(platform)
        if source_cls is None:
            raise ValueError(f"Plataforma no soportada: {platform}")

        source_config = (
            self.config.get("source", {})
            if platform == "youtube"
            else self.config.get("twitch", {})
        )

        self._source = source_cls(
            source_config,
            callbacks={
                "on_message": self.process_message,
                "on_error": self._on_source_error,
                "on_connect": self._on_source_connect,
                "on_disconnect": self._on_source_disconnect,
            },
        )
        self._source.connect(source_id)

        if self._session_id is None:
            self._session_id = self.history.start_session(platform, source_id)

    def start_session(self, platform: str = "youtube", channel: str = "headless") -> int:
        if self._session_id is None:
            self._session_id = self.history.start_session(platform, channel)
        return self._session_id

    def end_session(self):
        if self._session_id is not None:
            self.history.end_session(self._session_id)
            self._session_id = None
            try:
                self.history.cleanup_old_sessions()
            except Exception:
                pass
    
    def disconnect(self):
        if self._source is not None:
            self._source.disconnect()
        self.end_session()
        try:
            self.history.cleanup_old_sessions()
        except Exception:
            pass
    
    def process_message(self, message: dict):
        now = self._message_timestamp(message)
        self._note_seen_message(now)
        filtered = self.msg_filter.filter(message)
        accepted = False
        if filtered is not None:
            # Check quality score threshold
            quality = filtered.get("quality", 1.0)
            min_quality = getattr(self.msg_filter, "min_quality_score", 0.0)
            if quality < min_quality:
                accepted = False
            else:
                filtered = self._apply_spam_filter(filtered)
                accepted = filtered is not None
        
        if accepted and self.on_filtered_message:
            try:
                self.on_filtered_message(filtered)
            except Exception:
                pass
        
        vibe = None
        vibe_temp = 50.0
        if accepted:
            current_rate = max(self.activity.get_current_rate(), self._raw_seen_rate())
            if self._should_sample_for_context(current_rate):
                self.intent_aggregator.add_message(filtered)

            if self._should_consider_vibe(current_rate):
                self.thermometer.add_message(filtered)
                vibe = self.thermometer.compute_vibe()
            vibe_temp = vibe.get("temperature", 50.0) if vibe else 50.0
            
            if vibe is not None:
                self._record_vibe_result(vibe)
                if self.on_vibe_update:
                    try:
                        self.on_vibe_update(vibe)
                    except Exception:
                        pass
                self.thermometer.reset()
            self.activity.on_message(filtered)
        
    def _on_activity_trigger(self, data: dict):
        if self.on_activity_trigger:
            try:
                self.on_activity_trigger(data)
            except Exception:
                pass

        if self._session_id is not None:
            try:
                context = self.intent_aggregator.recent_messages(max_messages=20)
                intent_summary = self.intent_aggregator.summarize()
                prompt = intent_summary.get("prompt", "")
                if prompt and prompt != "No hay un tema dominante claro en el chat filtrado.":
                    self.history.add_context_snapshot(
                        self._session_id,
                        prompt,
                        message_count=int(intent_summary.get("total_messages", 0)),
                        vibe=data.get("temperature"),
                        metadata={"trigger": data, "top_intents": intent_summary.get("top_intents", [])},
                    )
                if self.on_aggregated_context:
                    self.on_aggregated_context({
                        "trigger": data,
                        "context": context,
                        "intent_summary": intent_summary,
                    })

                # ── Shadow mode: Input Contract logging ─────────────────
                if INPUT_CONTRACT_SHADOW_MODE:
                    try:
                        builder = ChatContextPacketBuilder()
                        packet = builder.build(context)
                        old = intent_summary.get("prompt", "")
                        self._log_input_contract_shadow(old, packet)
                    except Exception:
                        pass

            except Exception:
                pass

    def _load_live_safety_config(self):
        cfg = self.config.get("live_safety", {})
        self._live_safety_enabled = bool(cfg.get("enabled", True))
        self._live_safety_threshold = max(0.01, float(cfg.get("high_traffic_threshold_per_second", 10.0)))
        self._live_safety_sample_every = max(1, int(cfg.get("high_traffic_sample_every", 10)))
        self._live_safety_empty_limit = max(1, int(cfg.get("empty_vibe_backoff_after", 2)))
        self._live_safety_backoff_seconds = max(0.0, float(cfg.get("empty_vibe_backoff_seconds", 60.0)))
        self._live_safety_log_interval = max(1.0, float(cfg.get("state_log_interval_seconds", 30.0)))
        self._live_seen_window_seconds = max(1.0, float(cfg.get("seen_window_seconds", self.activity.window_seconds)))
        self._live_seen_timestamps = deque()
        self._live_sample_counter = 0
        self._live_high_traffic = False
        self._live_empty_vibe_count = 0
        self._live_vibe_backoff_until = 0.0
        self._live_last_state_log = 0.0

    def _message_timestamp(self, message: dict) -> float:
        try:
            return float(message.get("timestamp", time.time()))
        except (TypeError, ValueError):
            return time.time()

    def _note_seen_message(self, timestamp: float) -> None:
        if not self._live_safety_enabled:
            return
        self._live_seen_timestamps.append(timestamp)
        cutoff = timestamp - self._live_seen_window_seconds
        while self._live_seen_timestamps and self._live_seen_timestamps[0] < cutoff:
            self._live_seen_timestamps.popleft()

    def _raw_seen_rate(self) -> float:
        if not self._live_safety_enabled or not self._live_seen_timestamps:
            return 0.0
        return len(self._live_seen_timestamps) / self._live_seen_window_seconds

    def _should_sample_for_context(self, current_rate: float) -> bool:
        if not self._live_safety_enabled:
            return True
        high = max(current_rate, self._raw_seen_rate()) >= self._live_safety_threshold
        if high != self._live_high_traffic:
            self._live_high_traffic = high
            state = "ON" if high else "OFF"
            accepted_rate = self.activity.get_current_rate()
            self._log_live_safety(
                f"[SmartAggregator] Live-safe high traffic {state}: compactando chat "
                f"(crudo={max(current_rate, self._raw_seen_rate()):.1f}, aceptado={accepted_rate:.1f}, "
                f"umbral_act={self.activity.threshold_per_second:.1f} msg/s).",
                force=True,
            )
        if not high:
            return True
        self._live_sample_counter += 1
        return self._live_sample_counter % self._live_safety_sample_every == 0

    def _should_consider_vibe(self, current_rate: float) -> bool:
        if not self._live_safety_enabled:
            return True
        now = time.time()
        if now < self._live_vibe_backoff_until:
            self._log_live_safety(f"[SmartAggregator] Vibe en backoff por respuestas vacías ({int(self._live_vibe_backoff_until - now)}s restantes).")
            return False
        if max(current_rate, self._raw_seen_rate()) >= self._live_safety_threshold:
            return False
        if self._source is not None and not self._source.is_connected():
            return False
        return True

    def _record_vibe_result(self, vibe: dict) -> None:
        note = vibe.get("note")
        if note in {"fallback_due_to_empty_llm_response", "fallback_due_to_parse_error", "fallback_due_to_llm_error"}:
            self._live_empty_vibe_count += 1
            if self._live_empty_vibe_count >= self._live_safety_empty_limit:
                self._live_vibe_backoff_until = time.time() + self._live_safety_backoff_seconds
                self._live_empty_vibe_count = 0
                self._log_live_safety(f"[SmartAggregator] Vibe en cooldown: {self._live_safety_empty_limit} respuestas vacías/no interpretables; pausa {int(self._live_safety_backoff_seconds)}s.", force=True)
        else:
            self._live_empty_vibe_count = 0

    def _log_live_safety(self, message: str, *, force: bool = False) -> None:
        now = time.time()
        if not force and (now - self._live_last_state_log) < self._live_safety_log_interval:
            return
        self._live_last_state_log = now
        if self.on_live_safety_log:
            try:
                self.on_live_safety_log(message)
            except Exception:
                pass

    def _log_input_contract_shadow(self, old_compact: str, packet: "ChatContextPacket") -> None:
        """Shadow log: compare old compact_chat vs new ChatContextPacket."""
        import json
        try:
            packet_dict = packet.to_dict()
            # Compact the packet for readable single-line logging
            packet_json = json.dumps(packet_dict, ensure_ascii=False, default=str)
            msg = (
                f"[InputContract Shadow] "
                f"old_compact={old_compact[:120]!r} | "
                f"packet={packet_json}"
            )
            if self.on_live_safety_log:
                self.on_live_safety_log(msg)
        except Exception:
            pass

    def _load_spam_config(self):
        spam_cfg = self.config.get("spam", {})
        self._spam_enabled = spam_cfg.get("enabled", True)
        self._spam_user_window = float(spam_cfg.get("user_window_seconds", 30))
        self._spam_max_messages = int(spam_cfg.get("max_messages_per_user", 10))
        self._spam_duplicate_window = float(spam_cfg.get("duplicate_window_seconds", 20))
        self._user_message_times = defaultdict(deque)
        self._user_last_text = {}

    def _apply_spam_filter(self, message: dict) -> Optional[dict]:
        if not self._spam_enabled:
            return message

        user = message.get("user", "").lower()
        text = message.get("text", "")
        timestamp = message.get("timestamp", time.time())
        try:
            timestamp = float(timestamp)
        except (TypeError, ValueError):
            timestamp = time.time()

        normalized_text = " ".join(text.lower().split())
        last_text, last_ts = self._user_last_text.get(user, (None, 0.0))
        if normalized_text and normalized_text == last_text and (timestamp - last_ts) <= self._spam_duplicate_window:
            return None
        self._user_last_text[user] = (normalized_text, timestamp)

        times = self._user_message_times[user]
        cutoff = timestamp - self._spam_user_window
        while times and times[0] < cutoff:
            times.popleft()
        if len(times) >= self._spam_max_messages:
            return None
        times.append(timestamp)

        return message
    
    def _on_source_error(self, error: str):
        if self.on_source_error:
            try:
                self.on_source_error(error)
            except Exception:
                pass
    
    def _on_source_connect(self, info: dict):
        if self.on_source_connect:
            try:
                self.on_source_connect(info)
            except Exception:
                pass
    
    def _on_source_disconnect(self):
        self.end_session()
        if self.on_source_disconnect:
            try:
                self.on_source_disconnect()
            except Exception:
                pass
