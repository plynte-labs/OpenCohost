""" opencohost/core/engine/llm_engine_scout.py
Scout and memory-promotion methods of MotorVocalIA.
Mixin extracted from llm_engine.py.
All runtime state stays on MotorVocalIA.
"""
import hashlib
import math
import re
import sqlite3
import time
from collections import Counter
from collections.abc import Sequence
from pathlib import Path

from opencohost.config import settings as _settings


# F1 (memory-paraphrase-contradiction-20260928): bounded timeout for the ONE
# embed_batch IPC round trip that embeds this sweep's up-to-_PROMOTION_DRAFT_BATCH
# (4) keeper texts. fact_index.py's own reconcile batching estimates ~90ms/row
# at 256-token padding once the MiniLM worker is warm (its _BATCH_SIZE=8
# docstring), and embed_query's measured p50 is 78ms for a SINGLE text
# (ADR-060) -- so 4 short keeper texts land well under 500ms in the warm case.
# 2s gives roughly 4x headroom for a cold or momentarily-contended worker
# (the reconcile background job and a turn's query embedding share the same
# lock) while still keeping this SYNCHRONOUS engine-thread sweep bounded: a
# promotion sweep that stalls the chat thread for several seconds is worse
# than one duplicate row surviving on the lexical fallback for one sweep.
_DEDUP_EMBED_BATCH_TIMEOUT_S = 2.0


def _cosine_similarity(vec_a, vec_b):
    """Pure-Python cosine of two equal-length vectors, or None.

    No numpy dependency at this layer (the engine only ever holds plain
    lists handed back over the semantic-worker IPC boundary). None on a
    length mismatch, a missing vector, or a (near-)zero norm -- never
    raises, so a malformed vector degrades the ONE pair it belongs to,
    never the whole dedup pass.
    """
    if not vec_a or not vec_b or len(vec_a) != len(vec_b):
        return None
    dot = sum(x * y for x, y in zip(vec_a, vec_b))
    norm_a = math.sqrt(sum(x * x for x in vec_a))
    norm_b = math.sqrt(sum(y * y for y in vec_b))
    if norm_a <= 1e-9 or norm_b <= 1e-9:
        return None
    return dot / (norm_a * norm_b)


def _cached_vector_if_fresh(rec, row, fact_index):
    """The cached fact vector *rec* for durable *row*, or None when it is
    missing or stale (revision / content hash / model id+version mismatch --
    FactSemanticIndex's own freshness contract)."""
    from opencohost.core.memory_v5_shadow.fact_index import content_fingerprint

    if (
        rec is None
        or rec.revision != int(row["revision"])
        or rec.content_hash != content_fingerprint(row["content"])
        or rec.model_id != fact_index.model_id
        or rec.model_version != fact_index.model_version
    ):
        return None
    return rec.vector


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

    def _usage_record(self, response, *, kind: str, model, is_local: bool, started: float) -> None:
        """Count one internal LLM call in the usage ledger (numbers only; never raises)."""
        try:
            from opencohost.core.usage import ledger

            model = ledger.call_model_name(
                model, is_local=is_local, provider_cfg=getattr(self, "_provider_config", None)
            )
            ledger.record_response(
                response, model=model, is_local=is_local, kind=kind,
                profile_id=getattr(self, "_current_profile_id", None) or "",
                wall_ms=int((time.monotonic() - started) * 1000),
            )
        except Exception:
            pass

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
            _usage_t0 = time.monotonic()
            response = self._ollama_chat_with_watchdog(
                timeout=_eng.LLM_SCOUT_TIMEOUT + 2,
                chat_callable=self._ollama_scout_chat,
                model=self._loaded_model,
                messages=[{"role": "user", "content": prompt}],
                options=self._with_num_ctx(
                    {
                        "num_predict": _eng.LLM_SCOUT_NUM_PREDICT,
                        "temperature": _eng.LLM_SCOUT_TEMPERATURE,
                    },
                    # Same num_ctx as chat: a default-ctx scout reloads the model.
                    self._resolve_local_num_ctx(self._loaded_model),
                ),
                keep_alive=_eng.LLM_KEEP_ALIVE,
            )
            self._usage_record(response, kind="topic_scout", model=self._loaded_model,
                               is_local=bool(self._is_local), started=_usage_t0)
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
        clock = getattr(self, "_promotion_wall_clock", None) or time.time
        return max(0, int(clock()))

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
        if status_code == 429:
            return "model_rate_limited"
        if status_code in {401, 403}:
            return "model_auth_error"
        if status_code in {404, 410}:
            return "model_unavailable"
        if isinstance(status_code, int) and status_code >= 500:
            return "model_server_error"
        transport_module = type(exc).__module__.split(".", 1)[0]
        if (
            isinstance(exc, (ConnectionError, OSError))
            or transport_module in {"httpcore", "httpx", "requests", "urllib3"}
            or hasattr(exc, "body_excerpt")
        ):
            return "model_transport"
        return "unexpected_precommit"

    def _cloud_promotion_enabled(self) -> bool:
        """Explicit opt-in gate for running promotion via the active cloud model."""
        from opencohost.config.llm_provider import is_cloud_promotion_enabled

        return is_cloud_promotion_enabled(getattr(self, "_provider_config", None))

    def _cloud_promotion_model(self) -> str:
        """Active cloud profile's model name when available, else empty string."""
        cfg = getattr(self, "_provider_config", None) or {}
        profile = self._cfg_active_profile(cfg) or {}
        return str(profile.get("model") or "").strip()

    def _use_cloud_promotion(self) -> bool:
        """True only when cloud is active, opt-in is enabled, and no local model is loaded."""
        return (
            not self._is_local
            and self._cloud_promotion_enabled()
            and not self._loaded_model
        )

    def _judge_model(self) -> str:
        """Return only the model this motor already loaded locally.

        When operating in local mode or with cloud promotion opt-in off,
        returns ``self._loaded_model or ""`` so unloaded motors gate with
        ``model_not_loaded`` without probing Ollama or touching cloud APIs.
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
        self, batch: list, *, chat_callable=None, related=None,
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
        existing_block = ""
        if related is not None and related.existing_map:
            # F2: the related EXISTING facts ride in the SAME judge call.
            draft_block = related.draft_block
            existing_block = _eng._PROMOTION_EXISTING_BLOCK.replace(
                "{existing_facts}", "\0X\0",
            )
        # str.replace, not str.format: the prompt's JSON example is full of
        # literal braces and doubling every one of them is a corruption trap.
        # Sentinels keep memory text that happens to contain a placeholder
        # from being substituted a second time.
        prompt = (
            _eng._PROMOTION_JUDGE_PROMPT
            .replace("{existing_block}", "\0E\0")
            .replace("{draft_block}", "\0D\0")
            .replace("\0E\0", existing_block)
            .replace("\0D\0", draft_block)
        )
        if existing_block:
            prompt = prompt.replace("\0X\0", related.existing_block)
        use_cloud = self._use_cloud_promotion()
        options = {
            "temperature": 0,
            "num_predict": _eng._PROMOTION_NUM_PREDICT,
        }
        messages = [{"role": "user", "content": prompt}]
        from opencohost.core.memory.models import MemoryJudgeResult
        json_schema = MemoryJudgeResult.model_json_schema()

        if use_cloud:
            judge_model = self._cloud_promotion_model()
            budget = float(_eng.CLOUD_CHAT_TIMEOUT)
            think = False
            cfg = self._provider_config
            if chat_callable is None:
                from opencohost.core.engine.llm_inference_service import (
                    get_inference_service,
                )

                inference_svc = get_inference_service(self)
                call = lambda **kw: inference_svc.cloud_chat(
                    provider_cfg=cfg, timeout=budget, **kw
                )
            else:
                call = chat_callable
        else:
            budget = self._judge_timeout_seconds()
            judge_model = self._judge_model()
            # Resolved for the judge model itself (it may differ from chat's).
            self._with_num_ctx(options, self._resolve_local_num_ctx(judge_model))
            normalized_model = judge_model.lower().replace("_", "-")
            think = "low" if "gpt-oss" in normalized_model else False
            if chat_callable is None:
                # Rebuilt per sweep because the budget is adaptive. The
                # judge transport (`_ollama_judge_chat`) is pinned local
                # regardless of the active provider, so it always needs this
                # client.
                self._ollama_judge_client = self._create_ollama_scout_client(
                    self.ollama, timeout=budget,
                )
            call = chat_callable or self._ollama_judge_chat

        _usage_t0 = time.monotonic()
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
        self._usage_record(response, kind="memoria_judge", model=judge_model,
                           is_local=not use_cloud, started=_usage_t0)
        text = self._scout_extract_text(response)
        return _eng._parse_promotion_diagnostics(text, len(batch))

    def _promotion_lifecycle_gate(self) -> str:
        """Return a normal lifecycle gate without probing infrastructure."""
        if not _eng.MEMORIAS_ENABLED:
            return "memorias_disabled"
        if self._use_cloud_promotion():
            if not self._cloud_promotion_model():
                return "model_not_loaded"
        elif not self._judge_model():
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

    def _build_dedup_semantic_fn(self, profile_id: str, decisions):
        """F1 (memory-paraphrase-contradiction-20260928): a precomputed-score
        closure for THIS sweep, or None to leave dedup on the lexical
        fallback.

        Design choice (precomputed-scores over a widened constructor hook,
        per the F1 task): the store's ``semantic_similarity_fn`` constructor
        parameter is a PERSISTENT instance attribute on a long-lived
        ``MemoriaStore``, but a sweep's embeddings are only valid for THIS
        batch, THIS profile, right now -- storing them there would either go
        stale the moment the sweep moves to another profile/batch, or need
        the store to know when to invalidate them, which drags worker/IPC
        awareness into a module the docstring is explicit must stay free of
        it. ``apply_promotion_batch``'s new ``semantic_similarity_fn``
        keyword is a per-call override instead: this method builds one fresh
        closure, the caller passes it in, and it is thrown away after.

        Only the up to ``_PROMOTION_DRAFT_BATCH`` (4) KEEP judged_texts in
        *decisions* are embedded (ONE ``embed_batch`` call, bounded by
        ``_DEDUP_EMBED_BATCH_TIMEOUT_S``) -- existing durable rows are never
        embedded here; they reuse the SAME cached ``fact_embeddings`` vectors
        (and the SAME revision/content-hash/model freshness contract) the
        retrieval hot path already validates in ``FactSemanticIndex``, via
        ``MemoriaStore.list_durable_rows_for_dedup``. A row with no cached or
        stale vector is simply absent from the returned closure's map, so
        THAT one pair alone falls back to lexical.

        Fully fail-open (F1 constraint 4): any settings gate, missing
        ``_memory``/``fact_index``, dead/absent worker, an embedding timeout,
        or a malformed batch result all return None here, never raise --
        ``_is_near_duplicate`` already treats a None fn as "use the lexical
        path", so an unavailable embedder never blocks promotion. Never logs
        memory text (RC-8); the one log line here is a reason CODE only.
        """
        try:
            if not (_settings.MEMORY_SEMANTIC_ENABLED and _settings.MEMORY_FACTS_ENABLED):
                return None

            # Lazy (OFF-zero contract, memoria injection's own precedent):
            # importing the v5 package must cost nothing when v5 is OFF.
            from opencohost.core.memory_v5_shadow.fact_index import FactSemanticIndex

            mem = getattr(self, "_memory", None)
            fact_index = getattr(mem, "fact_index", None) if mem is not None else None
            if not isinstance(fact_index, FactSemanticIndex):
                return None

            keeper_texts: list[str] = []
            seen: set[str] = set()
            for _, judged_text, uncertain, _reason in decisions:
                if judged_text is None or uncertain or judged_text in seen:
                    continue
                seen.add(judged_text)
                keeper_texts.append(judged_text)
            if not keeper_texts:
                return None

            vectors = fact_index.worker.embed_batch(
                keeper_texts, timeout_s=_DEDUP_EMBED_BATCH_TIMEOUT_S,
            )
            if not vectors or len(vectors) != len(keeper_texts):
                _eng.logger.info(
                    "memoria dedup semantic embedding unavailable (fail-open): "
                    "reason=embed_batch_empty_or_mismatched"
                )
                return None

            vector_by_text: dict[str, list] = dict(zip(keeper_texts, vectors))

            durable_rows = self._get_memoria_store().list_durable_rows_for_dedup(
                profile_id,
            )
            if durable_rows:
                cached = {
                    rec.fact_id: rec
                    for rec in fact_index.cache_store.get_fact_embeddings_by_profile(
                        profile_id,
                    )
                }
                for row in durable_rows:
                    content = row["content"]
                    if content in vector_by_text:
                        continue  # a fresh keeper vector always wins
                    vector = _cached_vector_if_fresh(
                        cached.get(row["id"]), row, fact_index,
                    )
                    if vector is None:
                        continue  # missing/stale cache entry -> lexical for this row
                    vector_by_text[content] = vector

            def _similarity(text_a: str, text_b: str):
                return _cosine_similarity(
                    vector_by_text.get(text_a), vector_by_text.get(text_b),
                )

            return _similarity
        except Exception as exc:
            _eng.logger.info(
                "memoria dedup semantic embedding unavailable (fail-open): "
                "reason=exception exc=%s",
                type(exc).__name__,
            )
            return None

    def _retrieve_related_facts(self, profile_id: str, store, batch: list) -> dict:
        """F2: ``{draft index (1-based): [ExistingFact, ...]}`` -- up to 3
        existing durable facts of THIS profile related to each draft, for the
        judge's EXISTING FACTS section. Runs BEFORE the judge call.

        Durable = promoted/imported/curated, active, non-private, non-blank
        (``MemoriaStore.list_supersede_candidates``). Related = MiniLM cosine
        in [RELATED_BAND_LOW, RELATED_BAND_HIGH) using ONE ``embed_batch`` of
        the draft texts plus the cached ``fact_embeddings`` vectors (same
        freshness contract as F1); a row with no fresh vector -- or every row
        when the worker is absent/dead/timed out -- uses the conservative
        lexical fallback in ``promotion_related.rank_related``. Fully
        fail-open: any error returns {} (the sweep then runs exactly as
        before F2). Counts-only logging (RC-8).
        """
        try:
            from opencohost.core.memory.promotion_related import (
                ExistingFact,
                rank_related,
            )

            rows = store.list_supersede_candidates(profile_id)
            if not rows:
                return {}
            facts = [
                ExistingFact(
                    id=r["id"], profile_id=r["profile_id"], status=r["status"],
                    revision=int(r["revision"]), content=r["content"],
                )
                for r in rows
            ]
            draft_vectors = None
            fact_vectors: dict = {}
            fact_index = None
            if _settings.MEMORY_SEMANTIC_ENABLED and _settings.MEMORY_FACTS_ENABLED:
                try:
                    from opencohost.core.memory_v5_shadow.fact_index import (
                        FactSemanticIndex,
                    )

                    mem = getattr(self, "_memory", None)
                    candidate = getattr(mem, "fact_index", None) if mem is not None else None
                    if isinstance(candidate, FactSemanticIndex):
                        fact_index = candidate
                except Exception:
                    fact_index = None
            if fact_index is not None:
                try:
                    texts = [row["content"] for row in batch]
                    vectors = fact_index.worker.embed_batch(
                        texts, timeout_s=_DEDUP_EMBED_BATCH_TIMEOUT_S,
                    )
                    if vectors and len(vectors) == len(texts):
                        draft_vectors = vectors
                        cached = {
                            rec.fact_id: rec
                            for rec in fact_index.cache_store.get_fact_embeddings_by_profile(
                                profile_id,
                            )
                        }
                        for row in rows:
                            vec = _cached_vector_if_fresh(
                                cached.get(row["id"]), row, fact_index,
                            )
                            if vec is not None:
                                fact_vectors[row["id"]] = vec
                except Exception as exc:
                    draft_vectors = None
                    _eng.logger.info(
                        "memoria related-fact embedding unavailable (fail-open): "
                        "reason=exception exc=%s", type(exc).__name__,
                    )
            related: dict = {}
            semantic_drafts = 0
            for index, row in enumerate(batch, start=1):
                scores = None
                if draft_vectors is not None:
                    scores = {}
                    for fid, vec in fact_vectors.items():
                        score = _cosine_similarity(draft_vectors[index - 1], vec)
                        if score is not None:
                            scores[fid] = score
                    semantic_drafts += 1
                picked = rank_related(row["content"], facts, scores)
                if picked:
                    related[index] = picked
            _eng.logger.info(
                "memoria related facts: drafts=%d with_related=%d semantic=%d "
                "candidates=%d", len(batch), len(related), semantic_drafts, len(facts),
            )
            return related
        except Exception as exc:
            _eng.logger.info(
                "memoria related-fact retrieval failed (fail-open): %s",
                type(exc).__name__,
            )
            return {}

    def _schedule_fact_reconcile_after_promotion(self, profile_id: str) -> None:
        """F1 -- after a sweep KEEPS at least one row, schedule (never block
        on) a background re-embed so the newly-promoted row(s) get a fact
        vector for both future dedup checks and retrieval ranking (ADR-060).

        Mirrors ``_fact_semantic_scores``'s own ``schedule_reconcile`` call:
        same lazy v5 import, same "full eligible row set, not just this
        batch" contract (``FactSemanticIndex.reconcile`` treats any id absent
        from the passed rows as GONE and deletes its cached vector, so
        passing only the just-promoted rows would wrongly evict every OTHER
        row's cache). Fail-open and silent -- a reconcile is a background
        nicety, never a sweep-blocking step.
        """
        try:
            from opencohost.core.memory_v5_shadow.fact_index import FactSemanticIndex

            mem = getattr(self, "_memory", None)
            fact_index = getattr(mem, "fact_index", None) if mem is not None else None
            if not isinstance(fact_index, FactSemanticIndex):
                return
            rows = self._get_memoria_store().list_injection_candidates(profile_id)
            fact_index.schedule_reconcile(profile_id, rows)
        except Exception as exc:
            _eng.logger.info(
                "memoria post-promotion reconcile schedule failed (fail-open): %s",
                type(exc).__name__,
            )

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
                return counts

            if not self._use_cloud_promotion():
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
                    return counts
                # Includes the cooling/deferred no-call case. A stale, already
                # due profile failure no longer has work to protect, while the
                # draft's own deadline remains authoritative in memorias.db.
                reset_ok = backoff_store.reset(profile_id, now_s=now_s)
                remaining = counts["unjudged_remaining"]
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
                return counts

            # The model call is intentionally outside every SQLite transaction.
            # F2: related EXISTING facts (fail-open; {} means "as before").
            related_ctx = None
            related_by_draft = self._retrieve_related_facts(profile_id, store, batch)
            if related_by_draft:
                from opencohost.core.memory.promotion_related import (
                    EXISTING_BUDGET_CHARS,
                    build_related_context,
                )

                related_ctx = build_related_context(
                    batch,
                    related_by_draft,
                    budget_chars=_eng._PROMOTION_DRAFT_CHARS + EXISTING_BUDGET_CHARS,
                    existing_budget_chars=EXISTING_BUDGET_CHARS,
                )
            try:
                if related_ctx is not None and related_ctx.existing_map:
                    diagnostics = self._run_promotion_judge(
                        batch, chat_callable=chat_callable, related=related_ctx,
                    )
                else:
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
                return counts

            if not diagnostics.top_level_valid:
                self._record_promotion_profile_failure(
                    profile_id, "protocol_malformed", now_s,
                )
                return counts

            counts["decided"] = len(diagnostics.decisions)
            # F1: built AFTER the judge call (only CONFIRMED keeper texts are
            # worth a worker round trip) and BEFORE the write transaction
            # opens (the closure itself does read-only I/O only).
            dedup_semantic_fn = self._build_dedup_semantic_fn(
                profile_id, diagnostics.decisions,
            )
            # F2: deterministic validator between the judge's action/target and
            # the store. Degrades to "new" on ANY failed check; counts only.
            applied_decisions = diagnostics.decisions
            updates: dict = {}
            if related_ctx is not None and related_ctx.existing_map:
                from opencohost.core.memory.promotion_related import (
                    resolve_judge_actions,
                )

                resolved = resolve_judge_actions(
                    diagnostics.decisions,
                    diagnostics.actions,
                    diagnostics.origins,
                    related_ctx.existing_map,
                    profile_id,
                )
                applied_decisions, updates = resolved.decisions, resolved.updates
                _eng.logger.info(
                    "memoria judge actions: new=%d duplicate=%d update=%d "
                    "degraded=%d reasons=%s",
                    resolved.stats["new"], resolved.stats["duplicate"],
                    resolved.stats["update"], resolved.stats["degraded"],
                    {
                        k[len("degraded_"):]: v
                        for k, v in resolved.stats.items()
                        if k.startswith("degraded_")
                    },
                )
            try:
                committed = store.apply_promotion_batch(
                    profile_id,
                    batch,
                    decisions=applied_decisions,
                    unresolved=diagnostics.unresolved,
                    now_s=now_s,
                    origins=diagnostics.origins,
                    semantic_similarity_fn=dedup_semantic_fn,
                    updates=updates,
                )
            except sqlite3.Error:
                self._record_promotion_profile_failure(
                    profile_id, "sqlite_write", now_s,
                )
                return counts
            except Exception:
                self._record_promotion_profile_failure(
                    profile_id, "unexpected_precommit", now_s,
                )
                return counts

            counts["kept"] = committed.kept
            counts["rejected"] = committed.rejected
            counts["stale"] = committed.stale
            charged_count = committed.charged
            deferred_count = committed.deferred
            reasons.update(committed.reasons)

            # F1: at least one new durable row exists now -- schedule (never
            # block on) a background re-embed so it gets a fact vector.
            if committed.kept:
                self._schedule_fact_reconcile_after_promotion(profile_id)

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
