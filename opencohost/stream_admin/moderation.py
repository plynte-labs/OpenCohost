import time


class ModerationEngine:
    def __init__(self, config: dict):
        self.config = config or {}
        self.enabled = self.config.get("enabled", False)
        self.mode = self.config.get("mode", "alerts_only")
        self.cooldown_seconds = float(self.config.get("cooldown_seconds", 120))
        self.negative_threshold = float(self.config.get("negative_temperature_threshold", 35))
        self.chat_rate_threshold = float(self.config.get("chat_rate_threshold", 2.0))
        self.high_risk_requires_confirmation = self.config.get("high_risk_actions_require_confirmation", True)
        self._last_action_ts = 0.0

    def evaluate(self, vibe: dict = None, activity: dict = None) -> dict:
        if not self.enabled:
            return {"action": "none", "reason": "moderation_disabled"}

        vibe = vibe or {}
        activity = activity or {}
        temp = float(vibe.get("temperature", 50.0) or 50.0)
        emotions = vibe.get("emotions", {}) or {}
        anger = float(emotions.get("anger", 0.0) or 0.0)
        sadness = float(emotions.get("sadness", 0.0) or 0.0)
        rate = float(activity.get("rate", 0.0) or 0.0)
        negative = temp <= self.negative_threshold or anger >= 0.55 or sadness >= 0.65
        high_velocity = rate >= self.chat_rate_threshold

        if not negative:
            return {"action": "none", "reason": "no_negative_spike"}

        proposal = {
            "action": "alert",
            "reason": f"vibe negativo detectado (temp={temp:.0f}, anger={anger:.2f}, sadness={sadness:.2f}, rate={rate:.2f})",
            "requires_confirmation": False,
            "mode": self.mode,
        }

        if self.mode == "alerts_only":
            return proposal

        if not self._cooldown_ready():
            proposal["action"] = "cooldown_skip"
            proposal["requires_confirmation"] = False
            return proposal

        proposal.update({
            "action": "slow_mode",
            "delay_seconds": int(self.config.get("slow_mode", {}).get("default_seconds", 10)),
            "duration_minutes": int(self.config.get("slow_mode", {}).get("duration_minutes", 5)),
            "requires_confirmation": self.mode != "automatic",
        })
        if high_velocity:
            proposal["reason"] += "; velocidad alta"
        return proposal

    def high_risk_action(self, action: str, user: str, reason: str, duration_seconds: int = 300) -> dict:
        return {
            "action": action,
            "user": user,
            "reason": reason,
            "duration_seconds": duration_seconds,
            "requires_confirmation": self.high_risk_requires_confirmation,
            "risk": "high",
        }

    def mark_action(self):
        self._last_action_ts = time.time()

    def _cooldown_ready(self) -> bool:
        return (time.time() - self._last_action_ts) >= self.cooldown_seconds
