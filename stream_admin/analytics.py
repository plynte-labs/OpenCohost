import time
from collections import deque


class AnalyticsTracker:
    def __init__(self, config: dict):
        self.config = config or {}
        self.enabled = self.config.get("enabled", True)
        self.update_interval = float(self.config.get("update_interval_seconds", 60))
        self._last_injection = 0.0
        self.chat_rates = deque(maxlen=60)
        self.vibes = deque(maxlen=20)
        self.events = deque(maxlen=50)
        self.started_at = time.time()
        self.viewers = None

    def ingest_rf3_event(self, event_type: str, payload: dict):
        now = time.time()
        if event_type == "vibe":
            self.vibes.append({"ts": now, "data": payload})
        elif event_type == "activity":
            rate = float(payload.get("rate", 0.0) or 0.0)
            self.chat_rates.append({"ts": now, "rate": rate})
        else:
            self.events.append({"ts": now, "type": event_type, "data": payload})

    def set_viewers(self, viewers):
        try:
            self.viewers = int(viewers)
        except (TypeError, ValueError):
            self.viewers = None

    def snapshot(self) -> dict:
        last_vibe = self.vibes[-1]["data"] if self.vibes else {}
        emotions = last_vibe.get("emotions", {}) if isinstance(last_vibe, dict) else {}
        dominant = max(emotions, key=emotions.get) if emotions else "neutral"
        avg_rate = 0.0
        if self.chat_rates:
            avg_rate = sum(item["rate"] for item in self.chat_rates) / len(self.chat_rates)
        return {
            "viewers": self.viewers,
            "uptime_seconds": int(time.time() - self.started_at),
            "chat_rate_avg": avg_rate,
            "last_vibe_temperature": last_vibe.get("temperature", 0.0) if isinstance(last_vibe, dict) else 0.0,
            "last_vibe_dominant": dominant,
            "events_count": len(self.events),
        }

    def should_inject_context(self) -> bool:
        if not self.enabled or not self.config.get("inject_context", True):
            return False
        now = time.time()
        if now - self._last_injection >= self.update_interval:
            self._last_injection = now
            return True
        return False

    def context_text(self) -> str:
        snap = self.snapshot()
        viewers = "no disponible" if snap["viewers"] is None else str(snap["viewers"])
        uptime_min = snap["uptime_seconds"] // 60
        return (
            f"Analiticas stream: viewers={viewers}, uptime={uptime_min} min, "
            f"chat_rate_avg={snap['chat_rate_avg']:.2f} msg/s, "
            f"vibe={snap['last_vibe_dominant']} {snap['last_vibe_temperature']:.0f}/100."
        )
