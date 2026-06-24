import time
from collections import deque
from typing import Optional, Dict, Any, Callable

class ActivityTrigger:
    def __init__(self, config: dict, callbacks: dict):
        self.config = config
        self.callbacks = callbacks
        self.window_seconds = config.get("window_seconds", 5)
        self.threshold_per_second = config.get("threshold_per_second", 10.0)
        self.cooldown_seconds = config.get("cooldown_seconds", 10.0)
        
        actions_cfg = config.get("actions", {})
        self.auto_reply_cfg = actions_cfg.get("auto_reply", {"enabled": False, "message": ""})
        self.behavior_change_cfg = actions_cfg.get("behavior_change", {"enabled": False, "parameter": "", "value": 1.0})
        
        self._timestamps: deque = deque()
        self._last_trigger_time: Optional[float] = None
        self._lock = False
    
    def on_message(self, message: dict):
        now = message.get("timestamp", time.time())
        try:
            now = float(now)
        except (TypeError, ValueError):
            now = time.time()

        self._timestamps.append(now)
        self._prune(now)
        
        current_rate = self.get_current_rate()
        if current_rate >= self.threshold_per_second:
            if self._last_trigger_time is None or (now - self._last_trigger_time) >= self.cooldown_seconds:
                self._last_trigger_time = now
                self._trigger(current_rate)
                self._emit_decision("trigger_fired", current_rate)
            else:
                # Rate is high enough but we're inside the cooldown window — a
                # currently-invisible suppression. Surfaced for telemetry only.
                self._emit_decision("cooldown_suppressed", current_rate)
        else:
            self._emit_decision("rate_below_threshold", current_rate)

    def _emit_decision(self, reason: str, rate: float) -> None:
        """Optional telemetry hook. No-op (identical behavior) unless an
        ``on_decision`` callback was injected."""
        cb = self.callbacks.get("on_decision")
        if cb is None:
            return
        try:
            cb({
                "reason": reason,
                "rate": rate,
                "threshold": self.threshold_per_second,
                "cooldown_seconds": self.cooldown_seconds,
            })
        except Exception:
            pass

    def get_current_rate(self) -> float:
        if not self._timestamps:
            return 0.0
        window = float(self.window_seconds)
        if window <= 0:
            return float(len(self._timestamps))
        return len(self._timestamps) / window
    
    def reset(self):
        self._timestamps.clear()
        self._last_trigger_time = None
    
    def _trigger(self, rate: float):
        payload = {
            "rate": rate,
            "threshold": self.threshold_per_second,
            "window_seconds": self.window_seconds,
            "actions": {}
        }
        
        if self.auto_reply_cfg.get("enabled"):
            payload["actions"]["auto_reply"] = self.auto_reply_cfg.get("message", "")
        
        if self.behavior_change_cfg.get("enabled"):
            payload["actions"]["behavior_change"] = {
                "parameter": self.behavior_change_cfg.get("parameter", ""),
                "value": self.behavior_change_cfg.get("value", 1.0)
            }
        
        if self.callbacks.get("on_trigger"):
            try:
                self.callbacks["on_trigger"](payload)
            except Exception:
                pass

    def _prune(self, now: float):
        cutoff = now - self.window_seconds
        while self._timestamps and self._timestamps[0] < cutoff:
            self._timestamps.popleft()
