import os
import yaml
import time
from collections import defaultdict, deque
from typing import Optional, Callable

from .session_history import SessionHistory
from .message_filter import MessageFilter
from .chat_source import YouTubeChatSource
from .vibe_thermometer import VibeThermometer
from .activity_trigger import ActivityTrigger
from .intent_aggregator import IntentAggregator

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
        self.source = YouTubeChatSource(
            self.config.get("source", {}),
            callbacks={
                "on_message": self.process_message,
                "on_error": self._on_source_error,
                "on_connect": self._on_source_connect,
                "on_disconnect": self._on_source_disconnect
            }
        )
        
        self.on_filtered_message: Optional[Callable] = None
        self.on_vibe_update: Optional[Callable] = None
        self.on_activity_trigger: Optional[Callable] = None
        self.on_aggregated_context: Optional[Callable] = None
        self.on_source_error: Optional[Callable] = None
        self.on_source_connect: Optional[Callable] = None
        self.on_source_disconnect: Optional[Callable] = None
        
        self._session_id: Optional[int] = None
        self._busy_callback: Optional[Callable[[], bool]] = None
        self._load_spam_config()
    
    def set_busy_callback(self, callback: Callable[[], bool]):
        self._busy_callback = callback
        self.thermometer._is_busy_callback = callback

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
    
    def connect(self, video_id: str):
        self.source.connect(video_id)
        if self._session_id is None:
            self._session_id = self.history.start_session("youtube", video_id)

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
        self.source.disconnect()
        self.end_session()
        try:
            self.history.cleanup_old_sessions()
        except Exception:
            pass
    
    def process_message(self, message: dict):
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
            self.intent_aggregator.add_message(filtered)
            self.thermometer.add_message(filtered)
            vibe = self.thermometer.compute_vibe()
            vibe_temp = vibe.get("temperature", 50.0) if vibe else 50.0
            
            if vibe is not None:
                if self.on_vibe_update:
                    try:
                        self.on_vibe_update(vibe)
                    except Exception:
                        pass
                self.thermometer.reset()
        
        if accepted:
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
