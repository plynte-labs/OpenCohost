import logging
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
from .filter_policy import get_preset, list_presets
from .diagnostics import FilterDiagnostics
from .filter_telemetry import FilterTelemetry, FilterStage, ReasonCode, MsgCategory
from .chat_input_contract import (
    ChatContextPacketBuilder,
    ChatEventDetector,
)
from opencohost.config.validation import runtime_screen

logger = logging.getLogger("OpenCohost")

# [CHAT_CONN]/[CHAT_INGEST] rollup cadence -- ponytail: fixed cadence, no
# config knob until a real session shows a fixed cadence isn't enough.
# Whichever threshold hits first triggers the rollup.
_CHAT_INGEST_ROLLUP_EVERY_N = 50
_CHAT_INGEST_ROLLUP_EVERY_SECONDS = 30.0


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
            callbacks={
                "on_trigger": self._on_activity_trigger,
                "on_decision": self._on_activity_decision,
            }
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
        # Fired by connect() once the old source is down and the new one is
        # built, immediately before the new one starts delivering. Declared
        # with on_filtered_message and the other source callbacks above;
        # consumers use it to drop per-source state (see
        # ChatFeedSink.new_session). The call site's position is load-bearing
        # -- see the comment there before moving it.
        self.on_source_changed: Optional[Callable] = None
        
        self._session_id: Optional[int] = None
        self._busy_callback: Optional[Callable[[], bool]] = None
        self._diagnostics = FilterDiagnostics()
        _diag_cfg = self.config.get("chat_activation_diagnostics", {}) or {}
        self._telemetry = FilterTelemetry(
            enabled=bool(_diag_cfg.get("enabled", False)),
            ring=int(_diag_cfg.get("ring_size", 512)),
        )
        self._filter_policy_name: str = "balanced"

        # [CHAT_CONN]/[CHAT_INGEST] observability state -- metadata only. See
        # connect()/_on_source_connect/_on_source_error/_on_source_disconnect
        # and _log_chat_ingest_received/_log_chat_ingest_rollup below.
        self._chat_ingest_platform: Optional[str] = None
        self._chat_ingest_source_id: Optional[str] = None
        self._chat_ingest_connect_seq = 0
        self._chat_ingest_last_error: Optional[str] = None
        self._chat_ingest_msg_count = 0
        self._chat_ingest_last_rollup_ts = time.monotonic()

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

    def set_filter_policy(self, preset_name: str) -> None:
        preset = self._get_filter_preset(preset_name)
        if preset is None:
            raise ValueError(f"Unknown filter preset: {preset_name}. Available: {sorted(list_presets())}")
        self._filter_policy_name = preset_name
        _apply_preset_to_filter(self.msg_filter, preset)

    def _get_filter_preset(self, preset_name: str) -> Optional[dict]:
        configured = self.config.get("filter_presets", {})
        if isinstance(configured, dict) and isinstance(configured.get(preset_name), dict):
            return dict(configured[preset_name])
        return get_preset(preset_name)

    def get_filter_policy(self) -> str:
        return self._filter_policy_name

    def get_diagnostics(self) -> dict:
        diag = self._diagnostics.get_diagnostics()
        if self._telemetry.enabled:
            diag = {**diag, "activation_telemetry": self._telemetry.rollup()}
        return diag

    def reset_diagnostics(self) -> None:
        self._diagnostics.reset_diagnostics()

    def _emit_decision(self, *, stage: str, accepted: bool, reason: str,
                       score=None, threshold=None, text: str = "", rate=None,
                       msg_category=None, batch_count=None) -> None:
        """Record a filter decision as METADATA ONLY. Fast-exits when disabled, so
        behavior is identical and the hot path pays only a bool check. ``text`` is
        used solely for its length — it is never stored."""
        if not self._telemetry.enabled:
            return
        if rate is None:
            rate = max(self.activity.get_current_rate(), self._raw_seen_rate())
        self._telemetry.record(
            stage=stage, accepted=accepted, reason=reason, score=score, threshold=threshold,
            msg_len=len(text) if text else 0, chat_rate=float(rate),
            msg_category=msg_category, batch_count=batch_count,
        )

    def _on_activity_decision(self, data: dict) -> None:
        """Activity-trigger decision callback (fired/cooldown-suppressed/rate-below)."""
        if not self._telemetry.enabled:
            return
        reason = data.get("reason", ReasonCode.UNKNOWN.value)
        self._telemetry.record(
            stage=FilterStage.ACTIVITY_TRIGGER.value,
            accepted=(reason == ReasonCode.TRIGGER_FIRED.value),
            reason=reason, score=data.get("rate"), threshold=data.get("threshold"),
            msg_len=0, msg_category=MsgCategory.NA.value,
            chat_rate=float(data.get("rate", 0.0)),
        )

    @property
    def diagnostics_enabled(self) -> bool:
        """True when chat-activation telemetry is collecting. app_shell reads this to
        decide whether to wire the motor's cross-module seams — so in production
        (False) the callbacks are never even attached."""
        return bool(self._telemetry.enabled)

    def on_chat_item_expired(self, info: dict) -> None:
        """Cross-module seam (called from the MOTOR worker thread): a ``source ==
        "chat"`` item expired in the motor's priority queue (TTL). RECORD-ONLY — it
        never changes item lifetime. Fast-exits when disabled.

        ``chat_rate`` is intentionally 0.0: reading the live rate here would touch the
        activity window from the wrong thread, and the TTL signal is age-vs-ttl, not
        rate. Age is carried in ``score`` and the TTL limit in ``threshold``."""
        if not self._telemetry.enabled:
            return
        ttl = info.get("ttl_sec")
        self._telemetry.record(
            stage=FilterStage.QUEUE_TTL.value,
            accepted=False,
            reason=ReasonCode.TTL_EXPIRED.value,
            score=float(info.get("age_sec", 0.0)),
            threshold=float(ttl) if ttl is not None else None,
            msg_len=0,
            chat_rate=0.0,
            msg_category=MsgCategory.NA.value,
        )

    def on_chat_turn_spoken(self) -> None:
        """Cross-module seam (called from the MOTOR worker thread): Kira finished
        speaking a chat turn. Advances the spoken clock. Fast-exits when disabled."""
        if not self._telemetry.enabled:
            return
        self._telemetry.mark_spoken()

    def attach_motor_telemetry_seams(self, motor) -> None:
        """Introduce the motor's cross-module telemetry seams to this collector, but
        ONLY when diagnostics are enabled — production leaves the motor's callbacks
        None so its behavior is byte-identical. Called once by the composition root."""
        if not self.diagnostics_enabled:
            return
        motor.on_chat_item_expired = self.on_chat_item_expired
        motor.on_chat_turn_spoken = self.on_chat_turn_spoken

    def _check_busy(self) -> bool:
        if self._busy_callback:
            try:
                return self._busy_callback()
            except Exception:
                return False
        return False
    
    def connect(self, source_id: str, platform: str = "twitch"):
        logger.info("[CHAT_CONN] connect attempt platform=%s source=%s", platform, source_id)

        if self._source is not None:
            # Fires _on_source_disconnect for the OLD source below, BEFORE
            # self._chat_ingest_platform/source_id are overwritten with the
            # new identity -- so that disconnect line reports the channel
            # that actually went down, not the one we're about to join.
            self._source.disconnect()

        source_cls = self._source_factory.get(platform)
        if source_cls is None:
            raise ValueError(f"Plataforma no soportada: {platform}")

        source_config = (
            self.config.get("source", {})
            if platform == "youtube"
            else self.config.get("twitch", {})
        )

        self._chat_ingest_platform = platform
        self._chat_ingest_source_id = source_id
        self._chat_ingest_connect_seq = 0
        self._chat_ingest_last_error = None

        self._source = source_cls(
            source_config,
            callbacks={
                "on_message": self.process_message,
                "on_error": self._on_source_error,
                "on_connect": self._on_source_connect,
                "on_disconnect": self._on_source_disconnect,
            },
        )

        # Every connect starts a NEW chat session -- including a reconnect to
        # the same channel, since the teardown above is unconditional and
        # nothing after this point is guaranteed contiguous with what came
        # before. Guarded exactly like on_filtered_message in process_message:
        # a listener that raises must never break a connect.
        #
        # THIS POSITION IS LOAD-BEARING. Do not move it. It is the only point
        # that satisfies all four constraints at once:
        #   1. AFTER the old source's disconnect() -- the old reader thread has
        #      been told to stop and joined, so an old-channel message can no
        #      longer travel process_message -> on_filtered_message -> the
        #      consumer and get stamped into the NEW session. That silent
        #      channel-mix is exactly what this signal exists to prevent, so
        #      firing at the top of connect() (where it used to be) defeats it.
        #   2. AFTER the platform lookup -- an unsupported platform raises
        #      before this line, so a typo'd URL (422) leaves the consumer's
        #      buffer intact instead of destroying the operator's scrollback.
        #   3. AFTER source construction -- same reason, for a connector that
        #      is unavailable at build time (503).
        #   4. BEFORE self._source.connect() -- that call starts the reader
        #      thread, so anything later could wipe the new session's own first
        #      messages.
        #
        # TWO RESIDUALS, both deliberately left alone:
        #   a) disconnect() joins its reader thread with timeout=2.0
        #      (chat_source.py) and can return with the thread still alive, so
        #      a straggler from the old channel can still land in the new
        #      session. That window is narrow and only opens on an
        #      already-degraded source; a generation counter to close it is
        #      more machinery than the failure is worth.
        #   b) constraint 3 covers failures raised by __init__ only. Today
        #      YouTubeChatSource does its pytchat-missing RuntimeError and its
        #      empty-source_id ValueError inside connect(), not __init__, so
        #      those two still wipe the buffer before failing. Fixing that
        #      means moving the checks up into __init__ (chat_source.py), not
        #      moving this call -- there is nowhere left to move it to that
        #      does not reopen constraint 4.
        #
        # No un-bumping on failure: the session counter is the client's "clear
        # your list" signal, and rewinding it would make a poll that already
        # observed the new value silently stop clearing.
        if self.on_source_changed:
            try:
                self.on_source_changed()
            except Exception:
                pass

        self._source.connect(source_id)

        if self._session_id is None:
            self._session_id = self.history.start_session(platform, source_id)

    def start_session(self, platform: str = "twitch", channel: str = "headless") -> int:
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
    
    def _log_chat_ingest_received(self) -> None:
        """DEBUG per-message receipt marker -- platform/source identity only,
        never text or user. Per-message INFO would spam a busy channel;
        _log_chat_ingest_rollup below is the INFO-level signal instead."""
        self._chat_ingest_msg_count += 1
        logger.debug(
            "[CHAT_INGEST] received platform=%s source=%s",
            self._chat_ingest_platform, self._chat_ingest_source_id,
        )

    def _log_chat_ingest_rollup(self) -> None:
        """Periodic INFO rollup: arrived vs survived the filter/spam/safety
        chain, plus the top rejection reasons -- the datum that settles
        "is the filter eating everything or is the socket dropping".
        Surfaces FilterDiagnostics' own cumulative counters (diagnostics.py)
        -- no separate counter is kept here. Gated to
        _CHAT_INGEST_ROLLUP_EVERY_N messages or _CHAT_INGEST_ROLLUP_EVERY_SECONDS,
        whichever comes first, so a busy channel doesn't spam the log.

        The activation half (peak_rate/threshold/fired) answers the question
        surviving the filter does NOT answer: whether Kira ever woke up.
        Session 2026-08-14 10:43-10:54 had survived=65 and fired=0, and the
        log said nothing at all, because ActivityTrigger's below-threshold
        branch only feeds telemetry that is off by default. peak_rate against
        threshold also separates the two ways a live channel stays silent:
        peak below threshold is the rate gate, peak at-or-above it with
        fired=0 is the cooldown or a swallowed callback.

        The activation half trails the filter half by one message -- this is
        called before process_message feeds the trigger. At the real cadence
        (50 messages or 30s) that is invisible, and it is not worth reordering
        a live path on which the trigger enqueues a turn."""
        now = time.monotonic()
        due_by_count = self._chat_ingest_msg_count % _CHAT_INGEST_ROLLUP_EVERY_N == 0
        due_by_time = (now - self._chat_ingest_last_rollup_ts) >= _CHAT_INGEST_ROLLUP_EVERY_SECONDS
        if not (due_by_count or due_by_time):
            return
        self._chat_ingest_last_rollup_ts = now
        diag = self._diagnostics.get_diagnostics()
        top_reasons = sorted(diag["by_reason"].items(), key=lambda kv: kv[1], reverse=True)[:3]
        reasons_str = ",".join(f"{r}:{n}" for r, n in top_reasons) if top_reasons else "none"
        logger.info(
            "[CHAT_INGEST] rollup platform=%s source=%s arrived=%d survived=%d "
            "rejected=%d top_reasons=%s peak_rate=%.2f threshold=%.2f fired=%d",
            self._chat_ingest_platform, self._chat_ingest_source_id,
            diag["seen"], diag["accepted"], diag["rejected"], reasons_str,
            self.activity.peak_rate, self.activity.threshold_per_second,
            self.activity.trigger_count,
        )

    def process_message(self, message: dict):
        now = self._message_timestamp(message)
        self._note_seen_message(now)
        self._diagnostics.record_seen()
        self._log_chat_ingest_received()
        filtered = self.msg_filter.filter(message)
        accepted = False
        if filtered is not None:
            # Check quality score threshold
            quality = filtered.get("quality", 1.0)
            min_quality = getattr(self.msg_filter, "min_quality_score", 0.0)
            if quality < min_quality:
                accepted = False
                self._diagnostics.record_rejected("quality_too_low")
                self._emit_decision(stage=FilterStage.QUALITY_GATE.value, accepted=False,
                                    reason=ReasonCode.QUALITY_TOO_LOW.value, score=quality,
                                    threshold=min_quality, text=filtered.get("text", ""))
            else:
                whitelisted = "quality" not in filtered  # whitelist bypass returns no quality key
                filtered = self._apply_spam_filter(filtered)
                accepted = filtered is not None
                if accepted:
                    self._diagnostics.record_accepted()
                    self._emit_decision(
                        stage=FilterStage.QUALITY_GATE.value, accepted=True,
                        reason=ReasonCode.WHITELISTED.value if whitelisted else ReasonCode.ACCEPTED.value,
                        score=filtered.get("quality"), threshold=min_quality,
                        text=filtered.get("text", ""))
                else:
                    self._diagnostics.record_rejected("spam")
                    self._emit_decision(stage=FilterStage.QUALITY_GATE.value, accepted=False,
                                        reason=ReasonCode.SPAM.value, text=message.get("text", ""))
        else:
            reason = self.msg_filter.last_rejection_reason or "unknown"
            self._diagnostics.record_rejected(reason)
            self._emit_decision(stage=FilterStage.MESSAGE_FILTER.value, accepted=False,
                                reason=reason, text=message.get("text", ""))

        # Input safety floor (design.md 3.1/8): the non-negotiable screen runs
        # once, here, after MessageFilter/spam accepted the message and before
        # it reaches ANY consumer -- display callback, context buffer, vibe,
        # activity. A blocked message is dropped outright, not routed through
        # _emit_decision: no FilterStage/ReasonCode exists for a content-safety
        # block, and inventing one is unrequested scope for this stage.
        # runtime_screen() already counts + throttle-logs the block by rule id
        # (validation.py) -- never the text.
        blocked_rule = runtime_screen(filtered) if accepted else None
        if blocked_rule is not None:
            accepted = False
            filtered = None
            # record_accepted() already fired above; reclassify so the counters
            # stay consistent instead of reporting a blocked raid as accepted.
            self._diagnostics.reclassify_accepted_as_rejected(f"safety_floor:{blocked_rule}")

        # Diagnostics are fully settled for this message at this point
        # (including a possible safety-floor reclassify above) -- the right
        # moment to check whether a periodic [CHAT_INGEST] rollup is due.
        self._log_chat_ingest_rollup()

        if accepted and self.on_filtered_message:
            try:
                self.on_filtered_message(filtered)
            except Exception:
                pass
        
        vibe = None
        vibe_temp = 50.0
        if accepted:
            current_rate = max(self.activity.get_current_rate(), self._raw_seen_rate())
            sampled = self._should_sample_for_context(current_rate)
            self._emit_decision(
                stage=FilterStage.CONTEXT_SAMPLING.value, accepted=sampled,
                reason=ReasonCode.SAMPLED_IN.value if sampled else ReasonCode.SAMPLED_OUT.value,
                score=current_rate, threshold=float(self._live_safety_sample_every),
                text=filtered.get("text", ""), rate=current_rate)
            if sampled:
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
                    # D2 — defense in depth: minimize metadata here too, independent
                    # of SessionHistory's own sanitizer. Drops trigger.actions
                    # (operator config, not PII, but not needed downstream) and
                    # top_intents[].examples/entities (verbatim viewer text).
                    minimized_trigger = {
                        k: v for k, v in data.items()
                        if k in {"rate", "threshold", "window_seconds"}
                    }
                    minimized_top_intents = [
                        {
                            k: v for k, v in item.items()
                            if k in {"intent", "label", "count", "duplicates"}
                        }
                        for item in intent_summary.get("top_intents", [])
                    ]
                    self.history.add_context_snapshot(
                        self._session_id,
                        prompt,
                        message_count=int(intent_summary.get("total_messages", 0)),
                        # vibe=data.get("temperature") is a pre-existing dead field:
                        # ActivityTrigger's payload never sets "temperature", so this
                        # is always None. Left as-is (out of scope for this fix).
                        vibe=data.get("temperature"),
                        metadata={"trigger": minimized_trigger, "top_intents": minimized_top_intents},
                    )
                if self.on_aggregated_context:
                    self.on_aggregated_context({
                        "trigger": data,
                        "context": context,
                        "intent_summary": intent_summary,
                    })

                # ── should_call telemetry (metadata-only; replaces the removed
                #    privacy-unsafe shadow log). Reads ONLY packet metadata —
                #    never to_dict()/to_prompt_context() (those carry author+text).
                if self._telemetry.enabled:
                    try:
                        packet = ChatContextPacketBuilder().build(context)
                        self._telemetry.record(
                            stage=FilterStage.SHOULD_CALL.value,
                            accepted=packet.should_call_llm,
                            reason=(ReasonCode.SHOULD_CALL_TRUE.value if packet.should_call_llm
                                    else ReasonCode.SHOULD_CALL_FALSE.value),
                            score=packet.confidence, threshold=None, msg_len=0,
                            msg_category=MsgCategory.NA.value,
                            chat_rate=float(data.get("rate", 0.0)),
                            batch_count=packet.total_messages,
                        )
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
        # `error` is str(Exception) from a socket/pytchat failure -- a
        # connection error message, never chat content. Kept as the pending
        # disconnect reason (below) so the operator can see WHY the socket
        # dropped, not just that it did.
        self._chat_ingest_last_error = error
        logger.warning(
            "[CHAT_CONN] error platform=%s source=%s msg=%s",
            self._chat_ingest_platform, self._chat_ingest_source_id, error,
        )
        if self.on_source_error:
            try:
                self.on_source_error(error)
            except Exception:
                pass

    def _on_source_connect(self, info: dict):
        self._chat_ingest_connect_seq += 1
        if self._chat_ingest_connect_seq == 1:
            logger.info(
                "[CHAT_CONN] connected platform=%s source=%s",
                info.get("platform"), info.get("source_id"),
            )
        else:
            # chat_source.py's _run() retries in place after on_error, WITHOUT
            # an intervening on_disconnect -- so a 2nd+ on_connect in the same
            # source lifetime IS the reconnect outcome.
            logger.info(
                "[CHAT_CONN] reconnected platform=%s source=%s attempt=%d",
                info.get("platform"), info.get("source_id"), self._chat_ingest_connect_seq,
            )
        self._chat_ingest_last_error = None
        if self.on_source_connect:
            try:
                self.on_source_connect(info)
            except Exception:
                pass

    def _on_source_disconnect(self):
        # "if available" per the observability spec: chat_source.py's
        # on_disconnect carries no reason, so the best available signal is
        # whatever on_error last reported for this source lifetime.
        reason = self._chat_ingest_last_error or "clean"
        logger.info(
            "[CHAT_CONN] disconnected platform=%s source=%s reason=%s",
            self._chat_ingest_platform, self._chat_ingest_source_id, reason,
        )
        self.end_session()
        if self.on_source_disconnect:
            try:
                self.on_source_disconnect()
            except Exception:
                pass


def _apply_preset_to_filter(msg_filter: MessageFilter, preset: dict) -> None:
    attr_map = {
        "min_words": "min_words",
        "min_char_length": "min_char_length",
        "min_quality_score": "min_quality_score",
        "discard_emojis_only": "discard_emojis_only",
        "discard_links": "discard_links",
        "discard_mentions": "discard_mentions",
        "discard_repetitive_chars": "discard_repetitive_chars",
        "discard_repeated_words": "discard_repeated_words",
        "discard_gibberish": "discard_gibberish",
        "discard_ascii_art": "discard_ascii_art",
    }
    for preset_key, attr_name in attr_map.items():
        if preset_key in preset:
            setattr(msg_filter, attr_name, preset[preset_key])
