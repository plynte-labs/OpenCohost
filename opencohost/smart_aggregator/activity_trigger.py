import math
import threading
import time
from collections import deque
from typing import Optional, Dict, Any, Callable

# Arithmetic edges of the adaptive count-gate (adaptive_chat_activation
# design.md Q3/Q8) -- CODE constants, not config: a floor/ceiling the YAML
# could move is not a floor/ceiling, it's just another tunable.
# FLOOR: below one accepted message per window there is nothing to fire on
# -- equal to the Small Stream switch's own value (StreamPanel.tsx
# SMALL_STREAM_ON), a state the product already ships and the owner has
# already accepted.
_ADAPTIVE_FLOOR_COUNT = 1
# CEILING: reproduces the hand-tuned YAML default (threshold_per_second:
# 1.0). Above it the cooldown (untouched by adaptive) is the binding cadence
# limiter, so a higher threshold only subtracts responsiveness during the
# one moment -- a raid -- the audience most expects a reaction.
_ADAPTIVE_CEILING_COUNT = 5

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

        # Cumulative-since-reset diagnostics, read by the [CHAT_INGEST] rollup.
        # Without them the rate gate is invisible: the `rate_below_threshold`
        # branch in on_message routes to _emit_decision, whose callback is None
        # unless injected, so a channel that never reaches threshold_per_second
        # produces ZERO log output while looking perfectly connected.
        # peak_rate (not the instantaneous rate) is what answers the operator's
        # real question -- "did this channel EVER come close to waking Kira" --
        # because get_current_rate() sampled at rollup time reads whatever the
        # last 5 seconds happened to hold, which is usually nothing.
        self.peak_rate = 0.0
        self.trigger_count = 0

        # Opt-in adaptive activation sampler (adaptive_chat_activation,
        # 2026-08-14 incident design) -- OFF by default. config["adaptive"]
        # only exists once the owner opts in via YAML `enabled: true`, or the
        # Tauri switch flips it live through Aggregator.set_adaptive_activation.
        adaptive_cfg = config.get("adaptive", {}) or {}
        self.adaptive_enabled = bool(adaptive_cfg.get("enabled", False))
        self.adaptive_interval_seconds = float(adaptive_cfg.get("interval_seconds", 120.0))
        self.adaptive_burst_factor = float(adaptive_cfg.get("burst_factor", 2.0))
        # Sampler bookkeeping -- see _maybe_resample. None means "no interval
        # in progress yet" (cold start / just (re)enabled / just reconnected).
        self._adaptive_accepted_since_sample = 0
        self._adaptive_last_sample_ts: Optional[float] = None
        # Cross-thread race guard (judge-confirmed 2026-08-14, Finding 3):
        # on_message() (chat reader thread) can already be past the
        # `if self.adaptive_enabled` gate below and mid-retune while
        # set_adaptive(False) runs on a FastAPI worker thread. Both the flag
        # flip in set_adaptive() and the recheck-before-write in
        # _maybe_resample() take this same lock, so a retune that is still
        # "in flight" when adaptive gets disabled always loses: it either
        # completes and writes BEFORE the disable is observed (fine -- the
        # operator's own subsequent manual write still lands last), or it
        # observes the disable and discards its own result. Uncontended in
        # the overwhelmingly common case (adaptive off, or no PUT racing a
        # retune), so this costs nothing on the hot path.
        self._adaptive_write_lock = threading.Lock()

    def on_message(self, message: dict):
        now = message.get("timestamp", time.time())
        try:
            now = float(now)
        except (TypeError, ValueError):
            now = time.time()

        # Adaptive sampler (design.md Q1/Q7): count THIS accepted message
        # toward the running interval, then retune threshold_per_second --
        # BEFORE the gate below reads it. That ordering is load-bearing: a
        # message arriving after a long silent gap corrects its own
        # threshold before the gate decides whether to fire on it (the
        # dead-hour-then-message case).
        if self.adaptive_enabled:
            self._adaptive_accepted_since_sample += 1
            self._maybe_resample(now)

        self._timestamps.append(now)
        self._prune(now)
        
        current_rate = self.get_current_rate()
        if current_rate > self.peak_rate:
            self.peak_rate = current_rate
        if current_rate >= self.threshold_per_second:
            if self._last_trigger_time is None or (now - self._last_trigger_time) >= self.cooldown_seconds:
                self._last_trigger_time = now
                self.trigger_count += 1
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
        # Only reached from set_activity_limits(reset=True), i.e. the operator
        # just changed the threshold. The counters describe how the OLD one
        # behaved, so carrying them forward would report the previous setting's
        # verdict as the new one's. Earlier rollups keep the history in the log.
        self.peak_rate = 0.0
        self.trigger_count = 0
        self.reset_adaptive_sampler()

    def reset_adaptive_sampler(self) -> None:
        """Restarts the adaptive sampler's clock without touching
        threshold_per_second or adaptive_enabled -- called from reset() above
        and from Aggregator.connect() (design.md Q7: a new source's rate says
        nothing about the old one; the current threshold is still the best
        available prior, so the first interval on the new source corrects it
        instead of the reset wiping it)."""
        self._adaptive_accepted_since_sample = 0
        self._adaptive_last_sample_ts = None

    def set_adaptive(self, enabled: bool) -> None:
        """design.md Q4/Q5: turning ON starts a fresh cold start -- stale
        accepted-count history from a previous enable window must never
        masquerade as "now". Turning OFF just flips the flag; on_message's
        own `if self.adaptive_enabled` guard freezes the sampler in place,
        and threshold_per_second is DELIBERATELY left at whatever adaptive
        last chose (Q5: restoring the pre-enable value would resurrect a
        week-old manual setting as a surprise)."""
        enabled = bool(enabled)
        if enabled and not self.adaptive_enabled:
            self.reset_adaptive_sampler()
        # Same lock _maybe_resample's recheck-before-write takes (Finding 3)
        # -- makes "disable" and "an in-flight retune's write" mutually
        # exclusive instead of merely racing.
        with self._adaptive_write_lock:
            self.adaptive_enabled = enabled

    def _maybe_resample(self, now: float) -> None:
        """Message-driven retune (design.md Q1/Q7) -- no timer thread: the
        trigger only ever runs from on_message, so a retune computed while no
        message arrives could change no observable behavior anyway."""
        if self._adaptive_last_sample_ts is None:
            # Cold start (Q4): hold whatever threshold is already in force;
            # the clock starts now, on the first message seen since enable.
            self._adaptive_last_sample_ts = now
            return
        elapsed = now - self._adaptive_last_sample_ts
        if elapsed < self.adaptive_interval_seconds:
            return

        accepted = self._adaptive_accepted_since_sample
        # max(..., 1.0): a backwards/out-of-order timestamp must never divide
        # by ~zero and explode the mean into a false ceiling read.
        mean_rate = accepted / max(elapsed, 1.0)
        raw_target = math.ceil(self.adaptive_burst_factor * mean_rate * self.window_seconds)
        if raw_target < _ADAPTIVE_FLOOR_COUNT:
            target_count, verdict = _ADAPTIVE_FLOOR_COUNT, "floored"
        elif raw_target > _ADAPTIVE_CEILING_COUNT:
            target_count, verdict = _ADAPTIVE_CEILING_COUNT, "ceilinged"
        else:
            target_count, verdict = raw_target, "applied"

        current_count = round(self.threshold_per_second * self.window_seconds)
        old_threshold = self.threshold_per_second
        if target_count == current_count:
            # design.md Q6: "the adaptive mode decided not to retune" must be
            # as visible as a retune -- logged below either way.
            verdict = "unchanged"
            new_threshold = old_threshold
        else:
            window = float(self.window_seconds)
            # Same non-positive-window guard get_current_rate() already uses
            # above -- a YAML `activity.window_seconds: 0` (or negative) made
            # this divide-by-zero on the chat reader thread (judge-confirmed
            # 2026-08-14, Finding 2). "Per-second" is undefined at window<=0,
            # so collapse to the raw target count instead, matching
            # get_current_rate()'s own "return the raw count" fallback.
            new_threshold = float(target_count) if window <= 0 else target_count / window
            # Finding 3: re-check adaptive_enabled under the SAME lock
            # set_adaptive() flips it under. Without this, a retune already
            # past on_message's `if self.adaptive_enabled` gate can still
            # land its write after a concurrent PUT /limits has disabled
            # adaptive and applied the operator's manual value -- the
            # adaptive value would silently win, and _stream_state would
            # keep reporting a threshold the operator never set.
            with self._adaptive_write_lock:
                if self.adaptive_enabled:
                    self.threshold_per_second = new_threshold
                else:
                    new_threshold = old_threshold  # discard the stale retune

        self._emit_adaptive_retune(
            verdict=verdict, sampled_rate=mean_rate, accepted=accepted,
            elapsed=elapsed, target_count=target_count,
            old_threshold=old_threshold, new_threshold=new_threshold,
        )

        self._adaptive_accepted_since_sample = 0
        self._adaptive_last_sample_ts = now

    def _emit_adaptive_retune(self, *, verdict: str, sampled_rate: float, accepted: int,
                              elapsed: float, target_count: int, old_threshold: float,
                              new_threshold: float) -> None:
        """Optional telemetry hook -- metadata only (rate/count numbers,
        never chat text or usernames). No-op unless ``on_adaptive_retune``
        was injected, same seam as ``_emit_decision`` above."""
        cb = self.callbacks.get("on_adaptive_retune")
        if cb is None:
            return
        try:
            cb({
                "verdict": verdict,
                "sampled_rate": sampled_rate,
                "accepted": accepted,
                "elapsed": elapsed,
                "target_count": target_count,
                "old_threshold": old_threshold,
                "new_threshold": new_threshold,
            })
        except Exception:
            pass

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
