""" opencohost/core/engine/llm_engine_scout.py
Scout and memory-promotion methods of MotorVocalIA.
Mixin extracted from llm_engine.py.
All runtime state stays on MotorVocalIA.
"""
import hashlib
import re
import sqlite3
import time
from collections import Counter
from collections.abc import Sequence
from pathlib import Path


_PROMOTION_DIAGNOSTIC_EVENTS = frozenset({
    "eligible", "started", "completed", "deferred",
})
_PROMOTION_DIAGNOSTIC_REASONS = frozenset({
    "none", "memorias_disabled", "model_not_loaded",
    "model_switch_pending", "no_profile", "profile_backoff",
    "model_not_resident", "no_drafts", "no_due", "input_cap",
    "success", "semantic_partial", "store_read_failed", "judge_failed",
    "protocol_malformed", "store_write_failed", "unexpected_failure",
})
_PROMOTION_DIAGNOSTIC_FAILURES = frozenset({
    "none", "residency_timeout", "residency_offline",
    "residency_malformed", "sqlite_read", "judge_watchdog",
    "model_unavailable", "model_server_error", "model_transport",
    "unexpected_precommit", "protocol_malformed", "sqlite_write",
    "backoff_reset_failed",
})
_PROMOTION_DIAGNOSTIC_LOG = (
    "[MEMORY_SWEEP] operation=memory_promotion "
    "event=%s profile_hash=%s model_hash=%s drafts_count=%d "
    "input_chars=%d reasoning_mode=%s output_budget=%d duration_ms=%d "
    "decided_count=%d kept_count=%d rejected_count=%d charged_count=%d "
    "deferred_count=%d stale_count=%d remaining_count=%d reason=%s "
    "failure_class=%s"
)

# Module object, not names off it: `_eng.X` resolves at CALL time, so the suite's
# monkeypatches on llm_engine are seen. Safe at module level in THIS direction --
# llm_engine is already in sys.modules when it imports us, and nothing below
# dereferences `_eng` until a method actually runs.
#
# Never evaluate `_eng.X` at module or class-body scope (default args, decorator
# args, class attributes). Those run during llm_engine's PARTIAL import, so they
# see only the names bound above its `import ScoutPromotionMixin` line: measured
# 2026-08-11, `_eng.SYSTEM_PROMPT` (llm_engine.py:30) resolves while
# `_eng.TTS_AUDIO_QUEUE_TIMEOUT` (llm_engine.py:111) raises
# "AttributeError: partially initialized module". Which side of that line a name
# falls on is not something a mixin can track -- and the line moves -- so the rule
# is scope, not the name. `mixin_freevar_audit.py` enforces it.
from opencohost.core import llm_engine as _eng

class ScoutPromotionMixin:
     # ── Topic Scout (topic_scout_llm_20260629) ──────────────────────────────
    def _scout_render_history(self, history_snapshot: list) -> list[str]:
        """Compact, sanitized rendering of the most-recent LIVE turns.

        Inherits the direct-path gate (``_sanitize_history_context``); the scout
        needs topic words only, so usernames and injection phrases are scrubbed.
        """
        # Host-only (history_source_tag_20260629 Task C/D): filter the FULL
        # snapshot to genuine HOST turns (direct/ptt) FIRST, then take the last N,
        # so the scout sees the last N real host turns — not N mixed turns thinned
        # to however few host turns happen to survive. Untagged/viewer/agenda
        # entries (source absent or not in the set) are excluded by `.get`.

        host_only = [
            msg for msg in history_snapshot
            if isinstance(msg, dict) and msg.get("source") in {"direct", "ptt"}
        ]
        recent = host_only[-_eng.LLM_SCOUT_HISTORY_MSGS:]
        lines: list[str] = []
        for msg in recent:
            if not isinstance(msg, dict):
                continue
            content = self._scout_scrub_text(
                self._sanitize_history_context(str(msg.get("content", "")))
            )
            if not content:
                continue
            speaker = "Host" if msg.get("role") == "user" else "Kira"
            lines.append(f"{speaker}: {content}")
        return lines

    @staticmethod
    def _scout_scrub_text(text: str) -> str:
        """Strip @mentions and injection-marker phrases from a render line.

        The scout only needs topic words — never usernames or injected
        instructions — so it scrubs harder than the verbatim direct path.
        """
        text = re.sub(r"@\w+", "", text)
        return _eng._strip_injection_markers(text)
    
    
    def _scout_extract_text(self, response, *, field: str = "content") -> str:
        """Pull the assistant content out of a chat response (dict or object).

        *field* selects which message field. The memoria judge's empty-content
        self-heal needs ``thinking``, and this already handles both response
        shapes — a second extractor would only duplicate that.
        """
        if response is None:
            return ""
        msg = response["message"] if isinstance(response, dict) else getattr(response, "message", None)
        if msg is None:
            return ""
        content = msg.get(field) if isinstance(msg, dict) else getattr(msg, field, "")
        return content or ""

    def _scout_parse_titles(self, text: str, input_block: str) -> list[dict]:
        """Parse adjacent topic titles: filter preamble/echo, sanitize, dedupe, cap 3."""
        if not text:
            return []
        from opencohost.smart_aggregator.kira_agenda_controller import KiraAgendaController

        input_cf = input_block.casefold()
        results: list[dict] = []
        seen: set[str] = set()
        for raw_line in text.splitlines():
            line = raw_line.strip().strip("-•*\t\"'").strip()
            if not line:
                continue
            # Preamble filter: conversational lead-ins ("Claro, acá van:") and
            # over-long lines are not titles.
            if line.endswith(":"):
                continue
            if len(line.split()) > _eng.SCOUT_TITLE_MAX_WORDS:
                continue
            # Echo filter: a title that merely repeats a seed term is not adjacent.
            if line.casefold() in input_cf:
                continue
            # Title gate (emoji/code/length) — drop silently on rejection.
            try:
                title = KiraAgendaController.sanitize_topic_text(line, field="title")
            except ValueError:
                continue
            slug = title.casefold()
            if slug in seen:
                continue
            seen.add(slug)
            results.append({"title": title, "source": "scout", "confidence": "LOW"})
            if len(results) >= 3:
                break
        return results

    def scout_digest(self) -> list[dict]:
        """Topic Scout: idle-time adjacent-topic suggester.

        Best-effort and FULLY self-contained: every internal failure returns []
        so it can never break the rule-based ``generate_suggestions`` it runs
        alongside. Returns DRAFTED-ready dicts; NEVER speaks or persists.
        """
        try:
            if not _eng.SCOUT_ENABLED:
                return []
            # F4 Pregen Cloud Gate (multi_provider_llm_20260723): the scout is
            # speculative generation and pregen-OFF covers ALL speculative spend,
            # so skip the dispatch on cloud unless explicitly opted in. Local
            # short-circuits (byte-identical). Same idiom as the pregenerate gate.
            if not self._is_local and not self._provider_config.get("pregen_enabled", False):
                return []
            # Gate 3: no model resident, or a switch pending/in-flight -> skip
            # (never cold-load; use the RESIDENT model, not the desired one).
            if self._loaded_model is None:
                return []
            if self._pending_model_switch or self._awaiting_first_success_after_switch:
                return []
            # Gate 1: re-read idle state immediately before the call.
            if self.is_processing or self.is_speaking:
                return []
            # Gate 2: real work queued but not yet started would serialize behind
            # an in-flight scout on the single runner — abort on ANY pending item.
            if self.has_pending_priority_before(_eng.SCOUT_QUEUE_FLOOR):
                return []
            # Value gate (efficiency, not safety): a thinking-capable model burns
            # the 64-token budget on <think> and returns nothing useful.
            if self._check_capabilities_reasoning(self._loaded_model):
                return []
            with self._history_lock:
                history_snapshot = list(self.historial)
            lines = self._scout_render_history(history_snapshot)
            if len(lines) < _eng.LLM_SCOUT_MIN_DIGEST_LINES:
                return []
            input_block = "\n".join(lines)
            # Fresh-input gate: hash the LIVE snapshot; skip an identical input.
            # Cached on EVERY attempt that reaches here (even when the call returns
            # []), so a no-op scout does not re-fire until the conversation moves.
            input_hash = hashlib.sha256(input_block.encode("utf-8")).hexdigest()
            if input_hash == self._scout_last_input_hash:
                return []
            self._scout_last_input_hash = input_hash
            if self._ollama_scout_client is None:
                self._ollama_scout_client = self._create_ollama_scout_client(self.ollama)
            prompt = _eng.i18n_active.scout_prompt().format(digest_block=input_block)
            response = self._ollama_chat_with_watchdog(
                timeout=_eng.LLM_SCOUT_TIMEOUT + 2,
                chat_callable=self._ollama_scout_chat,
                model=self._loaded_model,
                messages=[{"role": "user", "content": prompt}],
                options={
                    "num_predict": _eng.LLM_SCOUT_NUM_PREDICT,
                    "temperature": _eng.LLM_SCOUT_TEMPERATURE,
                },
                keep_alive=_eng.LLM_KEEP_ALIVE,
            )
            text = self._scout_extract_text(response)
            return self._scout_parse_titles(text, input_block)
        except Exception:
            # Total internal isolation — a scout failure must never escape.
            return []

    # ── memoria draft promotion (memory_promotion_20260725) ─────────────────
    def _get_promotion_backoff_store(self):
        """Return the process singleton for the sibling control database."""
        with self._memoria_store_lock:
            if self._promotion_backoff_store is None:
                path = Path(_eng.MEMORIAS_DB).with_name(
                    "memoria_promotion_backoff.db"
                )
                self._promotion_backoff_store = _eng.PromotionBackoffStore(
                    path
                )
            return self._promotion_backoff_store

    def _promotion_now_s(self) -> int:
        """Return the injected non-negative UTC epoch second."""
        return max(0, int(self._promotion_wall_clock()))

    def _promotion_diagnostic(
        self,
        event: str,
        profile_id: str | None = None,
        model: str | None = None,
        drafts_count: int = -1,
        input_chars: int = -1,
        *,
        reasoning_mode: str = "none",
        duration_ms: int = -1,
        decided_count: int = 0,
        kept_count: int = 0,
        rejected_count: int = 0,
        charged_count: int = 0,
        deferred_count: int = 0,
        stale_count: int = 0,
        remaining_count: int = -1,
        reason: str = "none",
        failure_class: str = "none",
        dedupe: bool = False,
        gate_dedupe: bool = False,
        clear_noop: bool = False,
        start_ns: int | None = None,
        counts: dict | None = None,
    ) -> int | None:
        """Emit one bounded metadata-only lifecycle line when opted in."""
        if not _eng.MEMORY_PROMOTION_DIAGNOSTICS:
            return None
        try:
            def identity_hash(domain: str, value: str | None) -> str:
                if not isinstance(value, str) or not value:
                    return "none"
                prefix = (
                    f"opencohost.memory_promotion.{domain}.v1\0".encode()
                )
                return hashlib.sha256(
                    prefix + value.encode("utf-8")
                ).hexdigest()[:16]

            def bounded(value: int, upper: int | None = None) -> int:
                value = int(value)
                if value < -1:
                    return -1
                if upper is not None:
                    return min(value, upper)
                return value

            if event not in _PROMOTION_DIAGNOSTIC_EVENTS:
                return None
            if clear_noop:
                self._promotion_diagnostic_last_noop = None
            profile_hash = identity_hash("profile", profile_id)
            model_hash = identity_hash("model", model)
            if reasoning_mode == "request":
                normalized = (model or "").lower().replace("_", "-")
                reasoning_mode = (
                    "low" if "gpt-oss" in normalized else "disabled"
                )
            if reasoning_mode not in {"disabled", "low", "none"}:
                reasoning_mode = "none"
            if reason not in _PROMOTION_DIAGNOSTIC_REASONS:
                reason = "unexpected_failure"
            if failure_class not in _PROMOTION_DIAGNOSTIC_FAILURES:
                failure_class = "unexpected_precommit"
            if counts is not None:
                decided_count = counts["decided"]
                kept_count = counts["kept"]
                rejected_count = counts["rejected"]
                stale_count = counts["stale"]
            if start_ns is not None:
                try:
                    duration_ms = max(
                        0, (time.monotonic_ns() - start_ns) // 1_000_000,
                    )
                except Exception:
                    duration_ms = -1
            remaining_count = bounded(remaining_count)
            if gate_dedupe:
                signature = (profile_hash, reason)
                if self._promotion_diagnostic_last_gate == signature:
                    return None
                self._promotion_diagnostic_last_gate = signature
            if dedupe:
                signature = (profile_hash, reason, remaining_count)
                if self._promotion_diagnostic_last_noop == signature:
                    return None
                self._promotion_diagnostic_last_noop = signature
            _eng.logger.info(
                _PROMOTION_DIAGNOSTIC_LOG,
                event,
                profile_hash,
                model_hash,
                bounded(drafts_count, _eng._PROMOTION_DRAFT_BATCH),
                bounded(input_chars, _eng._PROMOTION_DRAFT_CHARS),
                reasoning_mode,
                _eng._PROMOTION_NUM_PREDICT,
                bounded(duration_ms),
                max(0, int(decided_count)),
                max(0, int(kept_count)),
                max(0, int(rejected_count)),
                max(0, int(charged_count)),
                max(0, int(deferred_count)),
                max(0, int(stale_count)),
                remaining_count,
                reason,
                failure_class,
            )
            if event == "started":
                try:
                    return time.monotonic_ns()
                except Exception:
                    return None
        except Exception:
            return None

    def _record_promotion_profile_failure(
        self, profile_id: str, failure_class: str, now_s: int,
    ) -> None:
        """Persist one privacy-safe infrastructure class, never draft text."""
        self._get_promotion_backoff_store().record_failure(
            profile_id, failure_class, now_s,
        )
        _eng.logger.warning(
            "memoria promotion sweep deferred: failure_class=%s",
            failure_class,
        )

    @staticmethod
    def _promotion_exception_class(exc: BaseException) -> str:
        """Map model-call exceptions to the bounded profile taxonomy."""
        if isinstance(exc, TimeoutError):
            return "judge_watchdog"
        status_code = getattr(exc, "status_code", None)
        if status_code in {404, 410}:
            return "model_unavailable"
        if isinstance(status_code, int) and status_code >= 500:
            return "model_server_error"
        transport_module = type(exc).__module__.split(".", 1)[0]
        if isinstance(exc, (ConnectionError, OSError)) or transport_module in {
            "httpcore",
            "httpx",
            "requests",
            "urllib3",
        }:
            return "model_transport"
        return "unexpected_precommit"

    def _judge_model(self) -> str:
        """Return only the model this motor already loaded locally.

        The promotion sweep sends up to 8 draft contents
        (excerpts of the owner's own conversation) to whatever model this
        resolves to. Memory content stays on the machine; the judge is the
        only place in the memoria pipeline that ever crosses the network, so
        it must never follow a cloud provider — never the active profile's
        model, never ``_cloud_chat``.

        No configured-model fallback is allowed: requesting an unloaded model
        would cold-load it solely for housekeeping. Always returns a string
        because reasoning classification lowercases the result.
        """
        return self._loaded_model or ""

    def _judge_model_residency(self) -> tuple[bool, str | None]:
        """Confirm the exact loaded model through a short local ``Client.ps``.

        This is a fail-closed pre-call check, not an atomic reservation. Ollama
        can still evict the runner between ``ps()`` and ``chat()``; Phase 2 does
        not claim to close that external race.
        """
        judge_model = self._judge_model()
        if not judge_model:
            return False, None
        try:
            client = self.ollama.Client(
                timeout=_eng._PROMOTION_RESIDENCY_TIMEOUT_SECONDS
            )
            response = client.ps()
            models = getattr(response, "models", None)
            if isinstance(models, (str, bytes)) or not isinstance(
                models, Sequence
            ):
                return False, "residency_malformed"
            resident = False
            for item in models:
                model = getattr(item, "model", None)
                if not isinstance(model, str) or not model:
                    return False, "residency_malformed"
                if model == judge_model:
                    resident = True
            return resident, None
        except TimeoutError:
            return False, "residency_timeout"
        except Exception:
            return False, "residency_offline"

    def _judge_model_is_resident(self) -> bool:
        """Compatibility projection of the richer residency result."""
        resident, _failure_class = self._judge_model_residency()
        return resident

    def _judge_timeout_seconds(self) -> float:
        """Return the judge's finite adaptive watchdog budget.

        Reuses ``_pregen_last_gen_duration`` — the SAME measurement
        ``_pregen_retry_gate_seconds`` already ships — instead of introducing a
        second latency-measurement scheme or a fixed constant (a constant would
        be a per-model assumption in disguise, contradicting the model-agnostic
        decision 2).

        The current trigger follows a successful owner response and a real-idle
        window, so ``last`` may already hold observed generation latency. When
        unavailable, ``None`` still selects the finite cold-start fallback.
        Reasoning capability does not expand the timeout because this maintenance
        request always sends an explicit thinking mode and output-token cap.

        Always the LOCAL adaptive budget now (owner decision 2026-08-08, F16):
        the judge transport (``_ollama_judge_chat``) is pinned local, so there is
        no cloud socket for ``CLOUD_CHAT_TIMEOUT`` to bound anymore — deriving
        the budget from local generation latency is correct in every case, not
        just when the active provider happens to be local too.
        """
        last = self._pregen_last_gen_duration
        base = (
            _eng.RETRY_MIN_REMAINING_SECONDS
            if last is None
            else last * _eng._JUDGE_BUDGET_FACTOR
        )
        return max(_eng._JUDGE_BUDGET_FLOOR_SECONDS, min(_eng._JUDGE_BUDGET_CEILING_SECONDS, base))

    def _run_promotion_judge(
        self, batch: list, *, chat_callable=None,
    ) -> "_eng._PromotionParseDiagnostics":
        """ONE chat completion over *batch*, parsed. Raises on transport failure.

        Maintenance is deterministic and bounded: temperature zero, 512 output
        tokens, and thinking disabled. GPT-OSS is the exception because Ollama
        ignores booleans for that family, so it receives the smallest supported
        level (``low``) while retaining the same output cap.
        """
        draft_block = "\n".join(
            f"{i}. {row['content']}" for i, row in enumerate(batch, start=1)
        )
        # str.replace, not str.format: the prompt's JSON example is full of
        # literal braces and doubling every one of them is a corruption trap.
        prompt = _eng._PROMOTION_JUDGE_PROMPT.replace("{draft_block}", draft_block)
        budget = self._judge_timeout_seconds()
        options = {
            "temperature": 0,
            "num_predict": _eng._PROMOTION_NUM_PREDICT,
        }
        judge_model = self._judge_model()
        normalized_model = judge_model.lower().replace("_", "-")
        think = "low" if "gpt-oss" in normalized_model else False
        if chat_callable is None:
            # Rebuilt per sweep because the budget is adaptive. The
            # judge transport (`_ollama_judge_chat`) is pinned local regardless
            # of the active provider, so it always needs this client.
            self._ollama_judge_client = self._create_ollama_scout_client(
                self.ollama, timeout=budget,
            )
        call = chat_callable or self._ollama_judge_chat
        messages = [{"role": "user", "content": prompt}]

        from opencohost.core.memory.models import MemoryJudgeResult
        json_schema = MemoryJudgeResult.model_json_schema()

        response = self._ollama_chat_with_watchdog(
            timeout=budget + 2,  # the socket abort must fire first (scout precedent)
            chat_callable=call,
            model=judge_model,
            messages=messages,
            format=json_schema,
            think=think,
            options=options,
            keep_alive=_eng.LLM_KEEP_ALIVE,
        )
        text = self._scout_extract_text(response)
        return _eng._parse_promotion_diagnostics(text, len(batch))

    def _promotion_lifecycle_gate(self) -> str:
        """Return a normal lifecycle gate without probing infrastructure."""
        if not _eng.MEMORIAS_ENABLED:
            return "memorias_disabled"
        if not self._judge_model():
            return "model_not_loaded"
        if (
            self._pending_model_switch
            or self._awaiting_first_success_after_switch
        ):
            return "model_switch_pending"
        if self._current_profile_id is None:
            return "no_profile"
        return ""

    def _promotion_gate(self) -> str:
        """Name of the gate blocking a sweep right now, or "" when clear.

        The judge stays local regardless of foreground provider and can run
        only on the exact non-empty `_loaded_model` confirmed by local
        ``Client.ps()``. Absence, mismatch, timeout, or malformed responses all
        stop before any judge call.
        """
        gate = self._promotion_lifecycle_gate()
        if gate:
            return gate
        if not self._judge_model_is_resident():
            return "model_not_resident"
        return ""

    def promote_pending_drafts(self, *, chat_callable=None) -> dict:
        """ONE LLM call: judge, rewrite and promote this profile's oldest
        unjudged drafts (memory_promotion_20260725).

        Per-turn capture is cheap, permissive and LLM-free; nothing ever filtered
        afterward, so the store fills with vague half-memories Kira then recites.
        This is the missing step.

        Synchronous on the engine thread and fully isolated. Infrastructure and
        malformed-protocol failures charge only the profile sidecar. A valid
        top-level response is applied atomically with attributable per-draft
        retry metadata; no failure path deletes memory content.

        *chat_callable* is the test seam, threaded straight into
        ``_ollama_chat_with_watchdog`` (the same boundary the Topic Scout tests
        fake). Returns counts ONLY — RC-8: no memory text ever reaches a log.

        ``counts["skipped"]`` names the gate that blocked a NOT-ATTEMPTED sweep
        and is "" whenever the sweep genuinely reached the store.
        """
        reasons: Counter = Counter()
        counts = {
            "considered": 0, "decided": 0, "kept": 0, "rejected": 0, "stale": 0,
            "unjudged_remaining": 0, "reasons": reasons, "skipped": "",
        }
        profile_id = None
        now_s = None
        attempt_started = False
        attempt_terminal = False
        attempt_start_ns = None
        batch = []
        draft_chars = 0
        charged_count = 0
        deferred_count = 0
        try:
            # Promotion supplies its own bounded reasoning controls, so it does
            # not inherit Topic Scout's reasoning-model skip.
            gate = self._promotion_lifecycle_gate()
            if gate:
                counts["skipped"] = gate
                # Once per CHANGED gate state, so a permanently inert sweep is
                # observable (owner decision 8) without a log line every second.
                if gate != self._promotion_last_gate:
                    self._promotion_last_gate = gate
                    # Missing/unconfirmed residency can leave the subsystem
                    # inert while drafts accumulate, so report it as a warning.
                    # Other gates are ordinary transient lifecycle phases.
                    level = (
                        _eng.logger.warning if gate.startswith("model_not_")
                        else _eng.logger.info
                    )
                    level("memoria promotion sweep gated: %s", gate)
                self._promotion_diagnostic(
                    "deferred",
                    self._current_profile_id,
                    self._loaded_model,
                    reason=gate,
                    gate_dedupe=True,
                )
                return counts
            profile_id = self._current_profile_id
            now_s = self._promotion_now_s()
            backoff_store = self._get_promotion_backoff_store()
            if backoff_store.is_active(profile_id, now_s):
                counts["skipped"] = "profile_backoff"
                if self._promotion_last_gate != "profile_backoff":
                    self._promotion_last_gate = "profile_backoff"
                    _eng.logger.info(
                        "memoria promotion sweep gated: profile_backoff"
                    )
                self._promotion_diagnostic(
                    "deferred",
                    profile_id,
                    self._loaded_model,
                    reason="profile_backoff",
                    gate_dedupe=True,
                )
                return counts

            resident, residency_failure = self._judge_model_residency()
            if not resident:
                counts["skipped"] = "model_not_resident"
                if residency_failure:
                    self._record_promotion_profile_failure(
                        profile_id, residency_failure, now_s,
                    )
                if self._promotion_last_gate != "model_not_resident":
                    self._promotion_last_gate = "model_not_resident"
                    _eng.logger.warning(
                        "memoria promotion sweep gated: model_not_resident"
                    )
                self._promotion_diagnostic(
                    "deferred",
                    profile_id,
                    self._loaded_model,
                    reason="model_not_resident",
                    failure_class=residency_failure or "none",
                    gate_dedupe=True,
                )
                return counts
            self._promotion_last_gate = ""

            try:
                store = self._get_memoria_store()
                drafts = store.list_unjudged_drafts(
                    profile_id,
                    limit=_eng._PROMOTION_DRAFT_BATCH,
                    now_s=now_s,
                    raising=True,
                )
            except sqlite3.Error:
                self._record_promotion_profile_failure(
                    profile_id, "sqlite_read", now_s,
                )
                self._promotion_diagnostic(
                    "deferred",
                    profile_id,
                    self._loaded_model,
                    reason="store_read_failed",
                    failure_class="sqlite_read",
                )
                return counts
            if not drafts:
                try:
                    counts["unjudged_remaining"] = (
                        store._count_unjudged_drafts(
                            profile_id, raising=True,
                        )
                    )
                except sqlite3.Error:
                    self._record_promotion_profile_failure(
                        profile_id, "sqlite_read", now_s,
                    )
                    self._promotion_diagnostic(
                        "deferred",
                        profile_id,
                        self._loaded_model,
                        reason="store_read_failed",
                        failure_class="sqlite_read",
                    )
                    return counts
                # Includes the cooling/deferred no-call case. A stale, already
                # due profile failure no longer has work to protect, while the
                # draft's own deadline remains authoritative in memorias.db.
                reset_ok = backoff_store.reset(profile_id, now_s=now_s)
                remaining = counts["unjudged_remaining"]
                self._promotion_diagnostic(
                    "completed",
                    profile_id,
                    self._loaded_model,
                    0,
                    0,
                    duration_ms=0,
                    remaining_count=remaining,
                    reason="no_drafts" if remaining == 0 else "no_due",
                    failure_class=(
                        "none" if reset_ok else "backoff_reset_failed"
                    ),
                    dedupe=True,
                )
                return counts  # the common no-op: zero tokens, no call at all

            # No dedup step, deliberately: the design's "drop any draft whose
            # stable_key is already durable" can never fire. UNIQUE(profile_id,
            # stable_key) is a TABLE constraint, so that state is unreachable —
            # a re-capture of an already-durable exchange resolves upsert_draft
            # to a no-op and inserts nothing. Re-judging a promoted row is
            # equally impossible (list_unjudged_drafts filters status='draft'
            # AND judged_at=''). Both would have been dead code behind a test
            # that only passes on a hand-forged database.
            for row in drafts:
                next_chars = draft_chars + len(row["content"])
                if next_chars > _eng._PROMOTION_DRAFT_CHARS:
                    break
                batch.append(row)
                draft_chars = next_chars
            counts["considered"] = len(batch)

            if not batch:
                self._promotion_diagnostic(
                    "completed",
                    profile_id,
                    self._loaded_model,
                    0,
                    0,
                    duration_ms=0,
                    remaining_count=-1,
                    reason="input_cap",
                    dedupe=True,
                )
                return counts

            # The model call is intentionally outside every SQLite transaction.
            diagnostic = (
                profile_id, self._loaded_model, len(batch), draft_chars,
            )
            self._promotion_diagnostic(
                "eligible",
                *diagnostic,
                reasoning_mode="request",
                clear_noop=True,
            )
            attempt_started = True
            attempt_start_ns = self._promotion_diagnostic(
                "started",
                *diagnostic,
                reasoning_mode="request",
            )
            try:
                diagnostics = self._run_promotion_judge(
                    batch, chat_callable=chat_callable,
                )
            except Exception as exc:
                failure_class = self._promotion_exception_class(exc)
                self._record_promotion_profile_failure(
                    profile_id,
                    failure_class,
                    now_s,
                )
                self._promotion_diagnostic(
                    "deferred",
                    *diagnostic,
                    start_ns=attempt_start_ns,
                    reasoning_mode="request",
                    reason="judge_failed",
                    failure_class=failure_class,
                )
                attempt_terminal = True
                return counts

            if not diagnostics.top_level_valid:
                self._record_promotion_profile_failure(
                    profile_id, "protocol_malformed", now_s,
                )
                self._promotion_diagnostic(
                    "deferred",
                    *diagnostic,
                    start_ns=attempt_start_ns,
                    reasoning_mode="request",
                    reason="protocol_malformed",
                    failure_class="protocol_malformed",
                )
                attempt_terminal = True
                return counts

            counts["decided"] = len(diagnostics.decisions)
            try:
                committed = store.apply_promotion_batch(
                    profile_id,
                    batch,
                    decisions=diagnostics.decisions,
                    unresolved=diagnostics.unresolved,
                    now_s=now_s,
                )
            except sqlite3.Error:
                self._record_promotion_profile_failure(
                    profile_id, "sqlite_write", now_s,
                )
                self._promotion_diagnostic(
                    "deferred",
                    *diagnostic,
                    start_ns=attempt_start_ns,
                    reasoning_mode="request",
                    counts=counts,
                    reason="store_write_failed",
                    failure_class="sqlite_write",
                )
                attempt_terminal = True
                return counts
            except Exception:
                self._record_promotion_profile_failure(
                    profile_id, "unexpected_precommit", now_s,
                )
                self._promotion_diagnostic(
                    "deferred",
                    *diagnostic,
                    start_ns=attempt_start_ns,
                    reasoning_mode="request",
                    counts=counts,
                    reason="store_write_failed",
                    failure_class="unexpected_precommit",
                )
                attempt_terminal = True
                return counts

            counts["kept"] = committed.kept
            counts["rejected"] = committed.rejected
            counts["stale"] = committed.stale
            charged_count = committed.charged
            deferred_count = committed.deferred
            reasons.update(committed.reasons)

            # A reset is truthful only after the transaction committed. If the
            # sidecar reset fails it keeps conservative state internally.
            terminal_failure = "none"
            if not backoff_store.reset(profile_id, now_s=now_s):
                reasons["backoff_reset_failed"] += 1
                terminal_failure = "backoff_reset_failed"
            remaining_count = -1
            try:
                counts["unjudged_remaining"] = (
                    store._count_unjudged_drafts(
                        profile_id, raising=True,
                    )
                )
                remaining_count = counts["unjudged_remaining"]
            except sqlite3.Error:
                self._record_promotion_profile_failure(
                    profile_id, "sqlite_read", now_s,
                )
                terminal_failure = "sqlite_read"
            self._promotion_diagnostic(
                "completed",
                *diagnostic,
                start_ns=attempt_start_ns,
                reasoning_mode="request",
                counts=counts,
                charged_count=charged_count,
                deferred_count=deferred_count,
                remaining_count=remaining_count,
                reason=(
                    "semantic_partial" if diagnostics.unresolved else "success"
                ),
                failure_class=terminal_failure,
            )
            attempt_terminal = True
            _eng.logger.info(
                "memoria promotion sweep: considered=%d decided=%d kept=%d rejected=%d "
                "stale=%d remaining=%d reasons=%s",
                counts["considered"], counts["decided"], counts["kept"],
                counts["rejected"], counts["stale"], counts["unjudged_remaining"],
                dict(reasons),
            )
            if counts["kept"]:
                # The owner-facing notice lives HERE now, not at capture
                # (moved 2026-08-14, see `_capture_memoria`): this sweep is
                # the first moment anything is KNOWN to be worth keeping.
                # Announcing at capture told the owner "Kira guardó una
                # memoria" after almost every turn for rows the judge later
                # threw away ~86% of the time.
                #
                # Reuses the existing `memoria_captured` event name so surfaces
                # that only listen for the bare status need no change.
                #
                # The COUNT rides a dedicated hook, not `ui_callback`: that one
                # lands in `EngineHost._dispatch_motor_event`, which drops extra
                # args by design (CTk's concrete callback takes exactly one
                # argument). Same shape as `on_ctx_pressure_high` and
                # `on_cloud_probe_scheduled`. Without it a sweep keeping 20
                # rendered identically to one keeping 1.
                #
                # Both are guarded: this is a fail-open path and a notice must
                # never break a sweep that already did its work.
                hook = getattr(self, "on_memoria_promoted", None)
                if hook is not None:
                    try:
                        hook({"kept": counts["kept"]})
                    except Exception:
                        _eng.logger.exception("on_memoria_promoted callback failed")
                try:
                    self.ui_callback("memoria_captured")
                except Exception:
                    pass
        except Exception as exc:
            # Last-resort pre-commit isolation. Type only; never raw draft
            # text.
            if profile_id is not None and now_s is not None:
                try:
                    self._record_promotion_profile_failure(
                        profile_id, "unexpected_precommit", now_s,
                    )
                except Exception:
                    pass
            if attempt_started and not attempt_terminal and profile_id:
                self._promotion_diagnostic(
                    "deferred",
                    *diagnostic,
                    start_ns=attempt_start_ns,
                    reasoning_mode="request",
                    counts=counts,
                    charged_count=charged_count,
                    deferred_count=deferred_count,
                    reason="unexpected_failure",
                    failure_class="unexpected_precommit",
                )
            elif not attempt_started:
                self._promotion_diagnostic(
                    "deferred",
                    profile_id,
                    self._loaded_model,
                    reason="unexpected_failure",
                    failure_class="unexpected_precommit",
                )
            _eng.logger.warning(
                "memoria promotion sweep failed (fail-open): %s",
                type(exc).__name__,
            )
        return counts

    def memory_inspector_snapshot(self) -> dict:
        """Read-only, privacy-gated snapshot of session memory for the
        "Memoria de Kira" UI inspector (cards_memory_readonly_panels_20260701).

        Snapshot-then-release (precedent: scout_digest): copies historial
        entries + digest stats to plain dicts under _history_lock, releases
        the lock, then formats. Never mutates historial or the digest.

        Content policy (fail-closed, no cross-module string heuristics):
          - user-slot entries: 'content' key present ONLY when source == "direct"
          - assistant-slot entries: 'content' key present when source is in
            _DIGEST_CAPTURE_SOURCES ({"direct", "ptt"}) — Kira's own on-air
            words are safe to show even for a ptt-sourced turn.
          - everything else (chat/accumulated/kira-agenda*/unknown/missing
            source): NO 'content' key at all.

        Returns:
            {
              "entries": [{"turn_index", "role", "source", "content_chars", ["content"]}],
              "source_breakdown": collections.Counter over entry sources,
              "digest": {"line_count", "total_chars", "max_chars"} — stats
                  only, never digest line text (compacted lines are
                  unattributable and can carry ptt-template junk pre-T1.1).
            }
        """
        with self._history_lock:
            raw_entries = list(self.historial)
            digest_lines = list(self._memory_digest.lines)
            digest_max_chars = self._memory_digest._max_chars

        entries: list[dict] = []
        for idx, raw_entry in enumerate(raw_entries):
            role = raw_entry.get("role")
            source = raw_entry.get("source")
            content = raw_entry.get("content", "") or ""
            entry = {
                "turn_index": idx,
                "role": role,
                "source": source,
                "content_chars": len(content),
            }
            show_content = (
                (role == "user" and source == "direct")
                or (role == "assistant" and source in _eng._DIGEST_CAPTURE_SOURCES)
            )
            if show_content:
                entry["content"] = content
            entries.append(entry)

        return {
            "entries": entries,
            "source_breakdown": Counter(e["source"] for e in entries),
            "digest": {
                "line_count": len(digest_lines),
                "total_chars": sum(len(line) for line in digest_lines),
                "max_chars": digest_max_chars,
            },
        }
