"""Generation orchestrator extracted from MotorVocalIA (WU3).

Orchestrates multi-attempt generation loops, cloud transport failure classification,
rate-limiting backoff, context-overflow reactive trimming, governed reasoning recovery with bounded escalation,
and sentence-level streaming generation with guardrail enforcement.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
import logging
import sys
import time
from typing import Any, Optional, Tuple

from opencohost.config.settings import (
    CLOUD_RATE_LIMIT_RETRY_DEFAULT_SECONDS,
    CLOUD_RATE_LIMIT_RETRY_MAX_SECONDS,
    CTX_OVERFLOW_SIGNAL_RATIO,
    LLM_KEEP_ALIVE,
    LLM_STREAMING_ENABLED,
    OWNER_BUNDLE_SOURCE,
)
from opencohost.config.validation import log_non_negotiable_block, output_guard
from opencohost.core.context import context_budget
from opencohost.core.context.generation_evaluator import (
    guard_rule_id as _guard_rule_id_pure,
    output_guard_with_tts_check as _output_guard_with_tts_check_pure,
)
from opencohost.core.context.prompt_assembler import GenerationSetup
from opencohost.core.providers.cloud import cloud_llm_client
from opencohost.core.speech.router import priority_for_source
from opencohost.core.speech.sentence_splitter import SentenceSplitter

logger = logging.getLogger("OpenCohost")


def _engine_attr(name: str, default: Any) -> Any:
    mod = sys.modules.get("opencohost.core.llm_engine")
    if mod is not None and hasattr(mod, name):
        return getattr(mod, name)
    return default


def _output_guard_with_tts_check(text: str, source: str) -> tuple[bool, str]:
    guard_fn = _engine_attr("output_guard", output_guard)
    return _output_guard_with_tts_check_pure(text, source=source, output_guard_fn=guard_fn)


def _guard_rule_id(reason: str) -> str:
    return _guard_rule_id_pure(reason)


@dataclass
class StreamAttemptState:
    """Per-attempt state of the streaming loop (llm_output_streaming_20260813
    design §3/§5/§7). Carries everything the divergence points downstream need:
    the lazily-created job (None until the first clean sentence closes — which
    is what keeps every pre-commit failure path byte-identical to the buffered
    path), the appended-sentence prefix (what Kira is owed to have said on
    air), the sentence counter, and the two failure signals (job cancel and
    post-submit guard trip).
    """
    source: str
    contexto: Any = None
    history_text: Optional[str] = None
    job: Any = None
    router: Any = None
    sentence_index: int = 0
    appended_sentences: list[str] = field(default_factory=list)
    consume_only: bool = False
    # Post-submit guard trip (§5 "after audio is out"):
    # (rule_id, tripping sentence index, spoken_upto).
    trip: Optional[tuple] = None
    # Cancel token / append_chunks refusal (§6): the turn is dead.
    abort_reason: Optional[str] = None
    # True once the partial-turn exit (or the finalize divergence) ran, so the
    # orphan belt in _generar_dialogo's catch-all never double-commits.
    handled: bool = False

    def spoken_prefix(self) -> str:
        return " ".join(self.appended_sentences).strip()


_StreamAttemptState = StreamAttemptState


@dataclass
class GenerationAttemptOutcome:
    """_cloud_attempt_loop's result. `early_return` mirrors the original
    method's own control flow: every early `return` inside the retry loop
    (watchdog timeout, transport failure exhausting the retry budget)
    returned exactly `""`, so `is not None` on this field is the one check
    the orchestrator needs to reproduce that identically -- it is NOT a
    truthiness check, since `""` itself is a valid (falsy) early-return
    value that must still short-circuit finalize.

    `stream` is None for every buffered attempt; a stream-eligible attempt
    carries its `_StreamAttemptState` so `_finalize_generation` can gate the
    §5/§7 divergence points on `stream.job` (llm_output_streaming_20260813).
    """
    raw_content: str = ""
    respuesta: object = None
    early_return: Optional[str] = None
    stream: Optional[StreamAttemptState] = None


_GenerationAttemptOutcome = GenerationAttemptOutcome


class GenerationOrchestrator:
    """Orchestrates generation attempts, retry policy, and streaming execution."""

    def __init__(self, host: Any) -> None:
        self._host = host

    def execute_generation_attempt(
        self,
        setup: GenerationSetup,
        *,
        source: str,
        commit_history: bool,
        is_local: bool,
        provider_cfg: dict,
        request_model: str,
        watchdog_timeout: Optional[float],
        contexto=None,
        history_text: Optional[str] = None,
    ) -> GenerationAttemptOutcome:
        """Phase 2 of _generar_dialogo (refactor_core_api_20260802 B7): the
        max_intentos retry loop, including the cloud transport-failure
        classification + rate-limited Retry-After retry branch, moved as ONE
        piece with its comments intact. Every early `return ""` here becomes
        `early_return=""` on the outcome -- the orchestrator propagates it
        identically via `if outcome.early_return is not None: return ...`.
        """
        messages = setup.messages
        opciones_llm = setup.opciones_llm
        chat_timeout = setup.chat_timeout
        max_intentos = setup.max_intentos
        _effective_ctx = setup.effective_ctx
        raw_content = ""
        respuesta = None
        stream_state: Optional[StreamAttemptState] = None

        stream_eligible = (
            is_local
            and source in ("direct", "ptt", OWNER_BUNDLE_SOURCE)
            and commit_history
            and getattr(self._host, "_speech_router_enabled", False)
            and _engine_attr("LLM_STREAMING_ENABLED", LLM_STREAMING_ENABLED)
            and getattr(setup, "tts_eligible", True)
        )

        # ADR-056: BUDGET_INFEASIBLE controlled abort. Never issue Ollama requests with num_predict=0.
        budget_res = getattr(setup, "budget_resolution", None)
        verdict_val = getattr(getattr(budget_res, "verdict", None), "value", str(getattr(budget_res, "verdict", "")))
        if verdict_val == "budget_infeasible" or opciones_llm.get("num_predict") == 0:
            self._host._log(
                f"Gobernanza de contexto: BUDGET_INFEASIBLE ({request_model}, ctx={_effective_ctx}); "
                "abortando generación de forma controlada sin invocar a Ollama.",
                level="warning",
            )
            if commit_history:
                self._host._invalidate_pregen_epoch()
            return GenerationAttemptOutcome(early_return="")

        for intento in range(max_intentos):
            with self._host._lock:
                self._host._llm_generating = True
            try:
                if stream_eligible:
                    respuesta, stream_state = self.run_streaming_attempt(
                        setup,
                        source=source,
                        request_model=request_model,
                        contexto=contexto,
                        history_text=history_text,
                    )
                    if stream_state.abort_reason is not None:
                        if commit_history:
                            self._host._invalidate_pregen_epoch()
                        return GenerationAttemptOutcome(early_return="")
                else:
                    watchdog_opts = dict(opciones_llm)
                    if getattr(setup, "think", None) is not None:
                        watchdog_opts["think"] = setup.think
                    respuesta = self._host._ollama_chat_with_watchdog(
                        timeout=chat_timeout,
                        model=request_model,
                        messages=messages,
                        keep_alive=LLM_KEEP_ALIVE,
                        options=watchdog_opts,
                        provider_cfg=provider_cfg,
                        is_local=is_local,
                    )
            except Exception as e:
                if self._host._is_watchdog_timeout_error(e):
                    if watchdog_timeout is None:
                        if is_local:
                            self._host._recover_from_stalled_inference(
                                request_model=request_model,
                                source=source,
                                timeout=chat_timeout,
                            )
                        else:
                            self._host._handle_cloud_failure(
                                source, failure_class=cloud_llm_client.CLOUD_ERROR_TRANSIENT
                            )
                        if commit_history:
                            self._host._invalidate_pregen_epoch()
                    return GenerationAttemptOutcome(early_return="")
                if not self._host._is_ollama_transport_error(e):
                    raise

                _cloud_class = None
                _cloud_attr = ""
                if is_local:
                    self._host._last_llm_failure = {
                        "model": self._host.current_model,
                        "source": source,
                        "attempt": intento + 1,
                        "reason": type(e).__name__,
                        "message": str(e),
                    }
                else:
                    _fail_profile = self._host._cfg_active_profile(provider_cfg) or {}
                    _cloud_class = cloud_llm_client.classify_cloud_error(e)
                    self._host._last_cloud_failure_class = _cloud_class
                    self._host._last_llm_failure = {
                        "model": _fail_profile.get("model") or self._host.current_model,
                        "provider": provider_cfg.get("active_provider"),
                        "source": source,
                        "attempt": intento + 1,
                        "reason": type(e).__name__,
                        "message": str(e),
                        "clase": _cloud_class,
                    }
                    _cloud_attr = " provider={} cloud_model={} error_code={}".format(
                        provider_cfg.get("active_provider") or "unknown",
                        _fail_profile.get("model") or "unknown",
                        cloud_llm_client.extract_error_code(e) or "n/a",
                    )
                _clase_suffix = f" clase={_cloud_class}" if _cloud_class else ""
                self._host._log(
                    f"ERROR Ollama chat ({type(e).__name__}) intento {intento+1}/{max_intentos}{_clase_suffix}: {e}",
                    level="error",
                )
                logger.warning(
                    "Ollama chat transport failure: model=%s source=%s attempt=%s/%s clase=%s%s",
                    request_model,
                    source,
                    intento + 1,
                    max_intentos,
                    _cloud_class or "n/a",
                    _cloud_attr,
                    exc_info=True,
                )

                if (
                    not is_local
                    and _cloud_class == cloud_llm_client.CLOUD_ERROR_RATE_LIMITED
                    and intento < max_intentos - 1
                ):
                    _retry_after = cloud_llm_client.parse_retry_after_seconds(
                        getattr(e, "headers", None) or {}
                    )
                    _wait_seconds = (
                        _retry_after if _retry_after is not None
                        else CLOUD_RATE_LIMIT_RETRY_DEFAULT_SECONDS
                    )
                    if _wait_seconds <= CLOUD_RATE_LIMIT_RETRY_MAX_SECONDS:
                        self._host._log(
                            f"rate_limited: retrying in {_wait_seconds}s "
                            f"(intento {intento+1}/{max_intentos}).",
                            level="warning",
                        )
                        time.sleep(_wait_seconds)
                        continue
                    self._host._log(
                        f"rate_limited: Retry-After={_wait_seconds}s exceeds "
                        f"{CLOUD_RATE_LIMIT_RETRY_MAX_SECONDS}s bound; not retrying in-turn.",
                        level="warning",
                    )

                if _cloud_class == cloud_llm_client.CLOUD_ERROR_BAD_KEY and not getattr(self._host, "_cloud_bad_key_notified", False):
                    self._host._cloud_bad_key_notified = True
                    self._host.ui_callback("cloud_bad_key")

                if not is_local:
                    _probe_retry_after = (
                        cloud_llm_client.parse_retry_after_seconds(getattr(e, "headers", None) or {})
                        if _cloud_class == cloud_llm_client.CLOUD_ERROR_RATE_LIMITED
                        else None
                    )
                    self._host._handle_cloud_failure(
                        source,
                        failure_class=_cloud_class or cloud_llm_client.CLOUD_ERROR_TRANSIENT,
                        retry_after_seconds=_probe_retry_after,
                    )
                if commit_history:
                    self._host._invalidate_pregen_epoch()
                return GenerationAttemptOutcome(early_return="")
            finally:
                with self._host._lock:
                    self._host._llm_generating = False

            msg_obj = respuesta.get('message', {}) if hasattr(respuesta, 'get') else getattr(respuesta, 'message', {})
            if hasattr(msg_obj, 'get'):
                raw_content = msg_obj.get('content', '')
                thinking = msg_obj.get('thinking', '')
            else:
                raw_content = getattr(msg_obj, 'content', '')
                thinking = getattr(msg_obj, 'thinking', '')

            if not is_local and hasattr(respuesta, 'get'):
                _usage = respuesta.get('usage')
                if _usage:
                    logger.info("cloud_llm_usage: %s source=%s", _usage, source)

            if thinking:
                logger.debug(f"Pensamiento interno detectado ({len(thinking)} chars)")

            if isinstance(respuesta, dict):
                _pec = respuesta.get("prompt_eval_count", 0)
            else:
                _pec = getattr(respuesta, "prompt_eval_count", None)
                if _pec is None and hasattr(respuesta, "get"):
                    _pec = respuesta.get("prompt_eval_count", 0)
            _pec = _pec or 0
            _ctx_limit_now = _effective_ctx
            cb = _engine_attr("context_budget", context_budget)
            if is_local and intento == 0 and cb.is_overflow_signal(
                raw_content, _pec, _ctx_limit_now, CTX_OVERFLOW_SIGNAL_RATIO
            ):
                _dropped = cb.trim_messages_reactive(messages, n_pairs=3)
                self._host._log(
                    f"ctx_overflow_reactive: prompt_eval_count={_pec} >= "
                    f"{_ctx_limit_now}*{CTX_OVERFLOW_SIGNAL_RATIO:.2f}; dropped "
                    f"{_dropped} pair(s) from in-flight messages, retrying.",
                    level="warning",
                )
                continue

            if not raw_content.strip() and thinking and 'num_predict' in opciones_llm:
                # ADR-056: Governed reasoning recovery. Never drop num_predict or retry uncapped.
                if is_local and hasattr(self._host, "_reasoning_model_cache"):
                    self._host._reasoning_model_cache[request_model] = True

                current_budget = int(opciones_llm["num_predict"])
                is_custom_user_budget = getattr(setup, "preset", "balanced") == "custom"
                remaining_ctx = getattr(getattr(setup, "budget_resolution", None), "remaining_context", None)
                if remaining_ctx is None or remaining_ctx <= 0:
                    remaining_ctx = max(0, int(_effective_ctx) - int(_pec or 0) - 128)

                if is_custom_user_budget:
                    self._host._log(
                        f"Gobernanza de razonamiento: {request_model} agotó presupuesto personalizado "
                        f"({current_budget} tokens) en pensamiento interno; respetando límite de usuario "
                        f"(REASONING_BUDGET_EXHAUSTED).",
                        level="warning",
                    )
                    break

                task_cap = 4096 if getattr(setup, "intent", "chat") == "drafting" else (2048 if getattr(setup, "intent", "chat") == "reasoning" else 1024)
                escalated_budget = min(current_budget * 2, task_cap, remaining_ctx)

                if escalated_budget > current_budget:
                    opciones_llm["num_predict"] = escalated_budget
                    self._host._log(
                        f"Gobernanza de razonamiento: {request_model} agotó presupuesto ({current_budget} tokens) "
                        f"en pensamiento interno; escalando a {escalated_budget} tokens gobernados "
                        f"(intento {intento+1}/{max_intentos}).",
                        level="warning",
                    )
                    continue
                else:
                    self._host._log(
                        f"Gobernanza de razonamiento: {request_model} agotó presupuesto ({current_budget} tokens) "
                        f"y no es posible autorizar más capacidad acotada (REASONING_BUDGET_EXHAUSTED).",
                        level="warning",
                    )
                    break

            if raw_content.strip():
                break

            self._host._log(f"⚠️ Intento {intento+1}: {request_model} devolvió respuesta vacía. Reintentando...", level="warning")
            time.sleep(0.5)

        return GenerationAttemptOutcome(
            raw_content=raw_content, respuesta=respuesta, stream=stream_state
        )

    execute_attempt_loop = execute_generation_attempt

    def run_streaming_attempt(
        self,
        setup: GenerationSetup,
        *,
        source: str,
        request_model: str,
        contexto,
        history_text: Optional[str],
    ) -> Tuple[Any, StreamAttemptState]:
        """One stream-eligible generation attempt (llm_output_streaming_20260813 §3)."""
        state = StreamAttemptState(
            source=source, contexto=contexto, history_text=history_text
        )
        self._host._live_stream_state = state

        # ADR-056: BUDGET_INFEASIBLE controlled abort in streaming path
        budget_res = getattr(setup, "budget_resolution", None)
        verdict_val = getattr(getattr(budget_res, "verdict", None), "value", str(getattr(budget_res, "verdict", "")))
        if verdict_val == "budget_infeasible" or setup.opciones_llm.get("num_predict") == 0:
            self._host._log(
                f"Gobernanza de contexto: BUDGET_INFEASIBLE en streaming ({request_model}, ctx={setup.effective_ctx}); "
                "abortando streaming de forma controlada sin invocar a Ollama.",
                level="warning",
            )
            state.abort_reason = "budget_infeasible"
            return None, state

        splitter = SentenceSplitter()
        accumulated = ""
        accumulated_thinking = ""
        final_chunk = None
        stream_opts = dict(setup.opciones_llm)
        if getattr(setup, "think", None) is not None:
            stream_opts["think"] = setup.think
        stream = self._host._ollama_chat_streaming(
            timeout=setup.chat_timeout,
            model=request_model,
            messages=setup.messages,
            keep_alive=LLM_KEEP_ALIVE,
            options=stream_opts,
        )
        try:
            with contextlib.closing(stream):
                stopped = False
                for chunk in stream:
                    final_chunk = chunk
                    msg = getattr(chunk, "message", None)
                    delta = (getattr(msg, "content", "") or "") if msg is not None else ""
                    accumulated += delta
                    accumulated_thinking += (
                        (getattr(msg, "thinking", "") or "") if msg is not None else ""
                    )
                    if state.consume_only:
                        continue
                    for sentence in splitter.feed(delta):
                        action = self.handle_stream_sentence(sentence, state, setup)
                        if action == "revert":
                            state.consume_only = True
                            break
                        if action != "continue":
                            stopped = True
                            break
                    if stopped:
                        break
                if not stopped and not state.consume_only:
                    for sentence in splitter.flush():
                        action = self.handle_stream_sentence(sentence, state, setup)
                        if action != "continue":
                            break
        except BaseException as exc:
            if state.job is not None and not state.handled:
                self.stream_partial_exit(state, reason=type(exc).__name__)
            raise
        if state.abort_reason is not None and state.job is not None and not state.handled:
            self.stream_partial_exit(state, reason=state.abort_reason)
        if state.job is not None:
            logger.info(
                "[STREAM_DONE] source=%s elapsed_ms=%d chars=%d words=%d sentences=%d",
                source,
                max(0, int((time.time() - setup.start_llm) * 1000)),
                len(state.spoken_prefix()),
                len(state.spoken_prefix().split()),
                len(state.appended_sentences),
            )
        respuesta = self.rebuild_stream_response(
            final_chunk, accumulated, accumulated_thinking
        )
        return respuesta, state

    def handle_stream_sentence(
        self, sentence: str, state: StreamAttemptState, setup: GenerationSetup
    ) -> str:
        """One closed sentence through the §3 loop."""
        source = state.source
        if self._host._speech_cancelled(source):
            state.abort_reason = "speech_cancelled"
            return "abort"
        state.sentence_index += 1
        pieces = [p for p in self._host._sanitize_for_tts(sentence) if p.strip()]
        sanitized = " ".join(p.strip() for p in pieces)
        if not sanitized:
            return "continue"

        allowed, reason = _output_guard_with_tts_check(sentence, source=source)
        if not allowed:
            if state.job is None:
                return "revert"
            state.trip = (
                _guard_rule_id(reason),
                state.sentence_index,
                len(state.appended_sentences),
            )
            return "truncate"
        fragments = self._host._fragment_for_tts(pieces)
        if not fragments:
            return "continue"
        if state.job is None:
            state.router = self._host._ensure_router()
            state.job = state.router.submit_streaming(
                source, priority_for_source(source)
            )
            if not state.router.append_chunks(state.job, fragments):
                state.abort_reason = "append_refused"
                return "abort"
            state.appended_sentences.append(sanitized)
            logger.info(
                "[STREAM_TTFA] source=%s first_audio_submit_ms=%d "
                "first_sentence_words=%d first_sentence_chars=%d fragments=%d",
                source,
                max(0, int((time.time() - setup.start_llm) * 1000)),
                len(sanitized.split()),
                len(sanitized),
                len(fragments),
            )
            return "continue"
        if not state.router.append_chunks(state.job, fragments):
            state.abort_reason = "append_refused"
            return "abort"
        state.appended_sentences.append(sanitized)
        return "continue"

    def stream_partial_exit(self, state: StreamAttemptState, *, reason: str) -> None:
        """§7 partial-turn exit for a streamed turn that dies AFTER its first submit."""
        try:
            state.router.seal(state.job)
        except Exception:
            logger.exception("stream partial exit: seal failed")
        prefix = state.spoken_prefix()
        if prefix:
            self._host._commit_history(
                state.contexto, prefix, source=state.source,
                history_text=state.history_text,
            )
            self._host._streamed_turn_prefix = prefix
        logger.warning(
            "[STREAM_PARTIAL_EXIT] source=%s reason=%s spoken_upto=%d "
            "sentences_closed=%d committed=%s",
            state.source, reason, len(state.appended_sentences),
            state.sentence_index, bool(prefix),
        )
        state.handled = True
        self._host._live_stream_state = None

    @staticmethod
    def rebuild_stream_response(final_chunk, accumulated: str, accumulated_thinking: str):
        """§3 'at stream end': reconstruct response dict/object."""
        if final_chunk is None:
            return {"message": {"content": accumulated, "thinking": accumulated_thinking}}
        if isinstance(final_chunk, dict):
            msg = final_chunk.setdefault("message", {})
        else:
            msg = getattr(final_chunk, "message", None)
        if isinstance(msg, dict):
            msg["content"] = accumulated
            msg["thinking"] = accumulated_thinking
        elif msg is not None:
            msg.content = accumulated
            msg.thinking = accumulated_thinking
        else:
            try:
                final_chunk.message = {"content": accumulated, "thinking": accumulated_thinking}
            except Exception:
                pass
        return final_chunk

    def apply_stream_guard_verdict(
        self, state: StreamAttemptState, dialogo: str, source: str
    ) -> str:
        """The §5/§7 finalize divergence for a turn whose audio is already on air."""
        trip = state.trip
        if trip is None:
            allowed, reason = _output_guard_with_tts_check(dialogo, source=source)
            if allowed:
                state.router.seal(state.job)
                state.handled = True
                self._host._live_stream_state = None
                self._host._streamed_turn_job = state.job
                return dialogo
            trip = (_guard_rule_id(reason), -1, len(state.appended_sentences))
        rule_id, sentence_index, spoken_upto = trip
        log_non_negotiable_block(
            rule_id,
            "stream_truncation",
            preview=f"sentence_index={sentence_index} spoken_upto={spoken_upto}",
        )
        state.router.seal(state.job)
        state.handled = True
        self._host._live_stream_state = None
        self._host._streamed_turn_job = state.job
        return state.spoken_prefix()
