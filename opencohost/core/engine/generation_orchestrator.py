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
    CLOUD_STREAMING_ENABLED,
    CTX_OVERFLOW_SIGNAL_RATIO,
    LLM_KEEP_ALIVE,
    LLM_STREAMING_ENABLED,
    OWNER_BUNDLE_SOURCE,
    TTS_FIRST_CHUNK_BOOST,
)
from opencohost.config.validation import (
    NO_CODE_OUTPUT_RULE_ID,
    count_code_fence_markers,
    log_non_negotiable_block,
    output_guard,
)
from opencohost.core.context import context_budget, prompt_echo, search_claim
from opencohost.core.context.generation_evaluator import (
    guard_rule_id as _guard_rule_id_pure,
    output_guard_with_tts_check as _output_guard_with_tts_check_pure,
)
from opencohost.core.context.prompt_assembler import GenerationSetup
from opencohost.core.safety.turn import is_crisis_turn
from opencohost.core.vision import direct_cloud
from opencohost.i18n import active as i18n_active
from opencohost.core.engine import prompt_trace, turn_events, turn_state, turn_timing
from opencohost.core.usage import ledger as usage_ledger
from opencohost.core.engine.turn_state import TurnEvent
from opencohost.core.providers.cloud import cloud_llm_client
from opencohost.core.providers.think_tags import ThinkTagStreamSplitter, strip_think_blocks
from opencohost.core.speech.router import priority_for_source
from opencohost.core.speech.sentence_splitter import SentenceSplitter, trim_to_last_sentence
from opencohost.core.web import sources as web_sources
from opencohost.core.speech.tts_sanitizer import _strip_control_markup_for_display

logger = logging.getLogger("OpenCohost")


def _engine_attr(name: str, default: Any) -> Any:
    mod = sys.modules.get("opencohost.core.llm_engine")
    if mod is not None and hasattr(mod, name):
        return getattr(mod, name)
    return default


def _output_guard_with_tts_check(text: str, source: str) -> tuple[bool, str]:
    guard_fn = _engine_attr("output_guard", output_guard)
    return _output_guard_with_tts_check_pure(text, source=source, output_guard_fn=guard_fn)


def _first_chunk_boost_enabled(setup: Any) -> bool:
    """tts-first-chunk-boost-20261015 W2, revised by the owner decision of
    2026-10-15 ("activate, then measure"): the boost is ON when
    TTS_FIRST_CHUNK_BOOST is on and `setup.think` is None or False, and OFF only
    when `think` is truthy (a model asked to reason).

    ACCEPTED RISK: Qwen3.8-9B always runs with think=False yet is a
    thinking-capable model. If it leaks inline reasoning in `content` despite
    think=off, a reasoning clause could be spoken as the soft first chunk before
    the orphan-`</think>` rescue (which only works while the splitter still holds
    the unspoken head) can act. Every rescue is counted by the `[THINK_RESCUE]`
    log line (`spoken_before=yes` is that leak); the target is zero across 1-2
    normal sessions."""
    return bool(TTS_FIRST_CHUNK_BOOST) and not getattr(setup, "think", None)


def _guard_rule_id(reason: str) -> str:
    return _guard_rule_id_pure(reason)


def _done_reason(response: Any) -> Optional[str]:
    """Ollama's stop reason ("stop", "length", ...) on a final chunk or a
    buffered response; None when the transport does not report one."""
    if response is None:
        return None
    if isinstance(response, dict):
        value = response.get("done_reason")
    else:
        value = getattr(response, "done_reason", None)
    return value if isinstance(value, str) else None


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
    raw_sentences: list[str] = field(default_factory=list)
    consume_only: bool = False
    # Post-submit guard trip (§5 "after audio is out"):
    # (rule_id, tripping sentence index, spoken_upto).
    trip: Optional[tuple] = None
    # Cancel token / append_chunks refusal (§6): the turn is dead.
    abort_reason: Optional[str] = None
    # Per-turn value (setup.web_searched): whether this turn's reply was built
    # on a web lookup. Read by the host's chunk sink; never shared instance state.
    web_searched: bool = False
    # True once the partial-turn exit (or the finalize divergence) ran, so the
    # orphan belt in _generar_dialogo's catch-all never double-commits.
    handled: bool = False
    # live-session-fixes-20261005 L1: this turn's prompt-echo detector, built
    # lazily from the setup on the first sentence that reaches the guard.
    echo_detector: Any = None
    # tts-first-chunk-boost-20261015: how the chunk now in handle_stream_sentence
    # was closed ("soft" punctuation or a full "sentence"); logged on the TTFA line.
    pending_boundary: str = "sentence"
    # Raw text of a spoken soft first chunk (the head of a sentence whose rest is
    # still to come). The chunk that completes that sentence is guarded as
    # `soft_head + " " + continuation` so guard rules see the whole sentence.
    soft_head: str = ""
    # no_code_output: True once an opening code fence was seen. Every later
    # sentence of the attempt is a hit, because a block's inner lines look
    # harmless sentence by sentence.
    in_fence: bool = False
    # runtime-fixes-20261110 T1: a guard reverted this attempt before any audio.
    # `reverted` is set for every revert; `reverted_rule` carries the id of an
    # output_guard / fence rule so the buffered recovery (retry + fallback line)
    # never depends on the text surviving the length trim. `abort_on_revert`
    # asks the loop to close the stream instead of consuming it to the cap.
    reverted: bool = False
    reverted_rule: Optional[str] = None
    abort_on_revert: bool = False
    # runtime-fixes-20261110 T5: sentences that are only a "Fuente: ..." attribution on
    # a web turn. Held back from speech; spoken only if more reply follows them,
    # dropped if the reply ends on them (the app shows the real sources itself).
    held_source: list[str] = field(default_factory=list)

    def spoken_prefix(self) -> str:
        return " ".join(self.appended_sentences).strip()

    def display_prefix(self) -> str:
        return " ".join(self.raw_sentences).strip() if self.raw_sentences else self.spoken_prefix()


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
    failure_reason: Optional[str] = None
    # B1: the cloud model rejected the turn's images (class image_unsupported);
    # the caller retries once text-only on the same cloud model.
    image_rejected: bool = False
    # B2: the cloud failure class of a failed cloud attempt (None otherwise).
    cloud_class: Optional[str] = None


class GenerationResult(str):
    """Invocation-local generation outcome transporting dialogue text and failure reason.

    Subclasses str so all existing string operations, comparisons, truthiness,
    and mock expectations remain 100% backward-compatible without shared instance state.

    `answered_by_provider`/`answered_by_transport` (T3, live session
    2026-09-25): the provider/transport that ACTUALLY produced `text`, as
    observed by the caller at the exact attempt that succeeded -- never a
    snapshot taken before `_generar_dialogo`'s retry loop ran. A cloud
    watchdog timeout that falls back and succeeds on the SAME call's in-turn
    local retry (`_generar_dialogo`'s `cloud_fallback_retry` branch) flips
    `is_local` mid-call; a caller-side pre-call snapshot of
    `provider_runtime_state()` cannot see that and misattributes the reply to
    the cloud provider it started under (the exact live-session bug: MODEL_
    TRACE showed provider=local transport=local fallback_active=True but the
    reply was reported under the cloud provider id). None on every path that
    returns "" (never surfaced -- `if dialogo:` gates the whole attribution
    block downstream) and on any construction that omits them, so existing
    callers/tests stay byte-identical.

    `cloud_fallback_retry` (T4, live_session_fixes_20260925): True only on
    the return that happens on/after `_generar_dialogo`'s own
    `cloud_fallback_retry_done` flip -- i.e. this exact turn actually took
    the same-call cloud->local retry branch, not merely "the effective
    transport happens to be local" (which is also true of a turn that was
    local from the start and never touched cloud). `_ejecutar_inferencia`
    reads this to decide whether the regenerated reply must first discard a
    still-pending `provider_fallback_notice` job (owner setting
    `discard_stale_speech_on_regenerated_reply`) before speaking. Defaults
    False, so any construction that omits it -- every pre-existing caller --
    stays byte-identical.
    """
    failure_reason: Optional[str] = None
    answered_by_provider: Optional[str] = None
    answered_by_transport: Optional[str] = None
    cloud_fallback_retry: bool = False
    # Per-turn value: this reply was built on a web lookup (setup.web_searched).
    web_searched: bool = False
    # runtime-fixes-20261110 T5: (host, https url) of the sources whose text was in
    # the prompt, at most 3. In memory, on the reply path only: never persisted,
    # never logged beyond a count and bare hosts, never sent to the gateway.
    web_sources: tuple = ()
    # vision-on-demand V1.15: speech tag for a canned reply (`<source>-vision-fail`);
    # None = speak under the turn source. Read by `_speak_or_submit`.
    speak_source: Optional[str] = None

    def __new__(
        cls,
        text: str = "",
        failure_reason: Optional[str] = None,
        answered_by_provider: Optional[str] = None,
        answered_by_transport: Optional[str] = None,
        cloud_fallback_retry: bool = False,
        web_searched: bool = False,
        web_sources: tuple = (),
    ):
        instance = super().__new__(cls, text or "")
        instance.failure_reason = failure_reason
        instance.answered_by_provider = answered_by_provider
        instance.answered_by_transport = answered_by_transport
        instance.cloud_fallback_retry = cloud_fallback_retry
        instance.web_searched = bool(web_searched)
        instance.web_sources = tuple(web_sources or ())
        return instance


_GenerationAttemptOutcome = GenerationAttemptOutcome


def _with_cloud_images(messages, setup, is_local):
    """*messages* with the setup's images spliced in for a cloud attempt, else
    the same list (no images, or a local attempt)."""
    images = getattr(setup, "cloud_images", ()) or ()
    if is_local or not images:
        return messages
    return direct_cloud.attach_images(messages, images)


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
        # P2: the wire messages of a CLOUD attempt carry the turn's images as
        # image parts on the last user message; ``messages`` (trace, history,
        # retries, the local path) stays text only.
        chat_messages = _with_cloud_images(messages, setup, is_local)
        opciones_llm = setup.opciones_llm
        chat_timeout = setup.chat_timeout
        max_intentos = setup.max_intentos
        _effective_ctx = setup.effective_ctx
        raw_content = ""
        respuesta = None
        stream_state: Optional[StreamAttemptState] = None
        failure_reason: Optional[str] = None
        vision_think_retry_done = False
        think_off_retry_done = False

        # live_session_fixes_20260925 T8: cloud streams too, but only behind
        # its OWN additional gate (CLOUD_STREAMING_ENABLED) -- every other
        # conjunct below still applies unchanged to a cloud attempt.
        stream_eligible = (
            (is_local or _engine_attr("CLOUD_STREAMING_ENABLED", CLOUD_STREAMING_ENABLED))
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
                        is_local=is_local,
                        provider_cfg=provider_cfg,
                    )
                    if stream_state.abort_reason is not None:
                        if commit_history:
                            self._host._invalidate_pregen_epoch()
                        return GenerationAttemptOutcome(early_return="")
                else:
                    watchdog_opts = dict(opciones_llm)
                    if getattr(setup, "think", None) is not None:
                        watchdog_opts["think"] = setup.think
                    _trace = prompt_trace.begin(
                        source=source,
                        model=usage_ledger.call_model_name(
                            request_model, is_local=is_local, provider_cfg=provider_cfg
                        ),
                        is_local=is_local,
                        messages=messages,
                        options=watchdog_opts,
                        sections=getattr(setup, "sections", ()),
                        path="buffered",
                        decisions=getattr(setup, "decisions", None),
                        profile=getattr(setup, "prompt_profile", None),
                        layout=getattr(setup, "layout", None),
                        evicted_pairs=getattr(setup, "evicted_pairs", None),
                        head_moved=getattr(setup, "head_moved", None),
                    )
                    try:
                        llm_mark = turn_timing.mark()
                        respuesta = self._host._ollama_chat_with_watchdog(
                            timeout=chat_timeout,
                            model=request_model,
                            messages=chat_messages,
                            keep_alive=LLM_KEEP_ALIVE,
                            options=watchdog_opts,
                            provider_cfg=provider_cfg,
                            is_local=is_local,
                        )
                        turn_timing.close(llm_mark, "llm_ms")
                    except BaseException as _trace_exc:
                        if _trace is not None:
                            _trace.fail(_trace_exc)
                        raise
                    if _trace is not None:
                        _trace.finish(respuesta)
            except Exception as e:
                # live_session_fixes_20260925 T8: a cloud STREAMING attempt
                # that already created a job (StreamAttemptState.job is not
                # None -- audio is already owed to the air) attaches its
                # state onto the exception before re-raising (see
                # run_streaming_attempt's except clause below) so that
                # signal survives up here even though `stream_state` (this
                # function's own local) never got assigned on a raise.
                # `_generar_dialogo`'s cloud_fallback_retry gate reads this
                # off the outcome to refuse retrying a turn that already
                # spoke (design.md §7's "no job created" gate for cloud
                # streaming).
                _attempt_stream_state = getattr(e, "_stream_attempt_state", None)
                if self._host._is_watchdog_timeout_error(e):
                    if commit_history:
                        turn_state.fail_reason("watchdog_timeout", e)
                        turn_state.emit(TurnEvent.STALL, attempt=intento + 1)
                    if watchdog_timeout is None:
                        if is_local:
                            _model_before = getattr(self._host, "current_model", None)
                            self._host._recover_from_stalled_inference(
                                request_model=request_model,
                                source=source,
                                timeout=chat_timeout,
                            )
                            if commit_history and getattr(self._host, "current_model", None) != _model_before:
                                turn_state.emit(TurnEvent.ROLLBACK)
                        else:
                            self._host._handle_cloud_failure(
                                source, failure_class=cloud_llm_client.CLOUD_ERROR_TRANSIENT
                            )
                        if commit_history:
                            self._host._invalidate_pregen_epoch()
                    return GenerationAttemptOutcome(early_return="", stream=_attempt_stream_state)
                if not self._host._is_ollama_transport_error(e):
                    raise

                _cloud_class = None
                _image_rejected = False
                _cloud_attr = ""
                if is_local and commit_history:
                    turn_state.fail_reason("ollama_transport_error", e)
                if is_local:
                    self._host._last_llm_failure = {
                        "model": self._host.current_model,
                        "source": source,
                        "attempt": intento + 1,
                        "reason": type(e).__name__,
                    }
                else:
                    _fail_profile = self._host._cfg_active_profile(provider_cfg) or {}
                    _cloud_class = cloud_llm_client.classify_cloud_error(e)
                    _image_rejected = (
                        _cloud_class == cloud_llm_client.CLOUD_ERROR_IMAGE_UNSUPPORTED
                    )
                    if _image_rejected and not (getattr(setup, "cloud_images", ()) or ()):
                        # B1: a 400 on a request that carried no images is not
                        # an image rejection; it keeps today's class.
                        _image_rejected = False
                        _cloud_class = cloud_llm_client.CLOUD_ERROR_TRANSIENT
                    self._host._last_cloud_failure_class = _cloud_class
                    if commit_history and not _image_rejected:
                        turn_state.fail_reason(
                            f"cloud_{_cloud_class}"
                            if f"cloud_{_cloud_class}" in turn_events.REASON_CODES
                            else "cloud_error",
                            e,
                        )
                    self._host._last_llm_failure = {
                        "model": _fail_profile.get("model") or self._host.current_model,
                        "provider": provider_cfg.get("active_provider"),
                        "source": source,
                        "attempt": intento + 1,
                        "reason": type(e).__name__,
                        "clase": _cloud_class,
                    }
                    _cloud_attr = " provider={} cloud_model={} error_code={}".format(
                        provider_cfg.get("active_provider") or "unknown",
                        _fail_profile.get("model") or "unknown",
                        cloud_llm_client.extract_error_code(e) or "n/a",
                    )
                    # Machine-readable reason only (HTTP status + the provider's
                    # enum status). Never the provider's free-text message: it can
                    # quote the request, and raw chat never reaches a log.
                    _http_status = getattr(e, "status_code", None)
                    if _http_status is not None:
                        _cloud_attr += " http_status={}".format(_http_status)
                    _provider_status = cloud_llm_client.extract_error_status(e)
                    if _provider_status:
                        _cloud_attr += " provider_status={}".format(_provider_status)
                _clase_suffix = f" clase={_cloud_class}" if _cloud_class else ""
                self._host._log(
                    f"ERROR Ollama chat ({type(e).__name__}) intento {intento+1}/{max_intentos}{_clase_suffix}",
                    level="error",
                )
                logger.warning(
                    "Ollama chat transport failure: model=%s source=%s attempt=%s/%s clase=%s%s site=%s",
                    request_model,
                    source,
                    intento + 1,
                    max_intentos,
                    _cloud_class or "n/a",
                    _cloud_attr,
                    turn_state.exc_site(e),
                )

                if _image_rejected:
                    # B1: the model refused the images, the cloud itself is fine.
                    # No fallback, no return probe: the caller retries this turn
                    # once on the same model without images. No text payload.
                    self._host.ui_callback("cloud_image_rejected")
                    if commit_history:
                        self._host._invalidate_pregen_epoch()
                    return GenerationAttemptOutcome(
                        early_return="",
                        stream=_attempt_stream_state,
                        image_rejected=True,
                        cloud_class=_cloud_class,
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

                if (
                    _cloud_class == cloud_llm_client.CLOUD_ERROR_QUOTA_EXHAUSTED
                    and not getattr(self._host, "_cloud_quota_notified", False)
                ):
                    self._host._cloud_quota_notified = True
                    self._host.ui_callback("cloud_quota_exhausted")

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
                return GenerationAttemptOutcome(
                    early_return="", stream=_attempt_stream_state, cloud_class=_cloud_class
                )
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
            if is_local:
                # L1 (live-session-fixes-20261005): a local fine-tune can put
                # its reasoning, or an orphan </think>, inside `content`.
                raw_content, thinking = strip_think_blocks(raw_content or "", thinking or "")

            if not is_local and hasattr(respuesta, 'get'):
                _usage = respuesta.get('usage')
                if _usage:
                    logger.info("cloud_llm_usage: %s source=%s", _usage, source)

            if thinking:
                t_len = len(thinking) if isinstance(thinking, (str, bytes, list)) else len(str(thinking))
                logger.debug(f"Pensamiento interno detectado ({t_len} chars)")

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
                if not is_local:
                    opciones_llm.pop("num_predict", None)
                    continue

                # ADR-056: Governed reasoning recovery for local inference. Never drop num_predict or retry uncapped.
                if hasattr(self._host, "_reasoning_model_cache"):
                    self._host._reasoning_model_cache[request_model] = True

                current_budget = int(opciones_llm["num_predict"])
                is_custom_user_budget = getattr(setup, "preset", "balanced") == "custom"
                remaining_ctx = getattr(getattr(setup, "budget_resolution", None), "remaining_context", None)
                if remaining_ctx is None or remaining_ctx <= 0:
                    remaining_ctx = max(0, int(_effective_ctx) - int(_pec or 0) - 128)
                elif _pec and _pec > 0:
                    remaining_ctx = min(remaining_ctx, max(0, int(_effective_ctx) - int(_pec) - 128))

                if (
                    getattr(setup, "vision_turn", False)
                    and not vision_think_retry_done
                    and intento + 1 < max_intentos
                ):
                    # K1 (warmth-guardrails-runtime-20261006): a vision turn is
                    # capped at VISION_REPLY_MAX_TOKENS and should never think.
                    # One retry with thinking off and a modest raise, even under
                    # a custom preset: the cap is the vision cap, not the user's.
                    vision_think_retry_done = True
                    retry_budget = max(
                        current_budget, min(current_budget + current_budget // 2, remaining_ctx)
                    )
                    opciones_llm["num_predict"] = retry_budget
                    setup.think = False
                    self._host._log(
                        f"Gobernanza de razonamiento: {request_model} gastó el turno de visión "
                        f"({current_budget} tokens) en pensamiento interno; reintento único sin "
                        f"pensamiento con {retry_budget} tokens (intento {intento+1}/{max_intentos}).",
                        level="warning",
                    )
                    continue

                if is_custom_user_budget:
                    self._host._log(
                        f"Gobernanza de razonamiento: {request_model} agotó presupuesto personalizado "
                        f"({current_budget} tokens) en pensamiento interno; respetando límite de usuario "
                        f"(REASONING_BUDGET_EXHAUSTED).",
                        level="warning",
                    )
                    return GenerationAttemptOutcome(
                        raw_content="",
                        early_return="",
                        respuesta=respuesta,
                        stream=stream_state,
                        failure_reason="reasoning_budget_exhausted",
                    )

                # F3 (session-2107-fixes): these models loop while thinking, so a
                # bigger budget only burns time. Instead of doubling num_predict,
                # ONE retry with thinking OFF and the answer-only budget, so the
                # owner always gets an answer (mirrors the vision K1 retry above).
                answer_budget = getattr(setup, "answer_budget", None)
                if answer_budget is None:
                    # Thinking was not requested (model only discovered to think at
                    # runtime), so the current budget is already answer-only.
                    answer_budget = current_budget
                answer_budget = max(32, min(int(answer_budget), int(remaining_ctx)))

                if (
                    not think_off_retry_done
                    and intento + 1 < max_intentos
                ):
                    think_off_retry_done = True
                    opciones_llm["num_predict"] = answer_budget
                    setup.think = False
                    self._host._log(
                        f"Gobernanza de razonamiento: {request_model} agotó presupuesto ({current_budget} tokens) "
                        f"en pensamiento interno; reintento único sin pensamiento con {answer_budget} tokens "
                        f"(intento {intento+1}/{max_intentos}).",
                        level="warning",
                    )
                    continue
                else:
                    if intento + 1 >= max_intentos:
                        self._host._log(
                            f"Gobernanza de razonamiento: {request_model} agotó presupuesto ({current_budget} tokens) "
                            f"en pensamiento interno y se alcanzó el límite de intentos ({max_intentos}/{max_intentos}) "
                            f"(REASONING_BUDGET_EXHAUSTED).",
                            level="warning",
                        )
                    else:
                        self._host._log(
                            f"Gobernanza de razonamiento: {request_model} agotó presupuesto ({current_budget} tokens) "
                            f"y no es posible autorizar más capacidad acotada (REASONING_BUDGET_EXHAUSTED).",
                            level="warning",
                        )
                    return GenerationAttemptOutcome(
                        raw_content="",
                        early_return="",
                        respuesta=respuesta,
                        stream=stream_state,
                        failure_reason="reasoning_budget_exhausted",
                    )

            if raw_content.strip():
                break

            self._host._log(f"⚠️ Intento {intento+1}: {request_model} devolvió respuesta vacía. Reintentando...", level="warning")
            time.sleep(0.5)

        # A reverted streamed attempt is judged whole by the buffered guard, so its
        # text must not be trimmed (an unterminated dump would trim to "").
        reverted_attempt = stream_state is not None and stream_state.reverted
        if raw_content and _done_reason(respuesta) == "length" and not reverted_attempt:
            raw_content = self._trim_length_truncated(
                raw_content, respuesta, source=source, streamed=stream_state is not None
            )

        return GenerationAttemptOutcome(
            raw_content=raw_content,
            respuesta=respuesta,
            stream=stream_state,
            failure_reason=failure_reason,
        )

    execute_attempt_loop = execute_generation_attempt

    @staticmethod
    def _trim_length_truncated(raw_content: str, respuesta: Any, *, source: str, streamed: bool) -> str:
        """L2 (live-session-fixes-20261005): the model hit its token limit, so
        its last sentence is cut. Keep only whole sentences -- the streamed
        path already withheld the same tail from speech. Counts only in logs."""
        kept = trim_to_last_sentence(raw_content)
        if isinstance(respuesta, dict):
            eval_count = respuesta.get("eval_count")
        else:
            eval_count = getattr(respuesta, "eval_count", None)
        logger.warning(
            "[STREAM_TRUNCATED] source=%s path=%s eval_count=%s kept_chars=%d dropped_chars=%d",
            source,
            "stream" if streamed else "buffered",
            eval_count if isinstance(eval_count, int) else "-",
            len(kept),
            max(0, len(raw_content.strip()) - len(kept)),
        )
        return kept

    def run_streaming_attempt(
        self,
        setup: GenerationSetup,
        *,
        source: str,
        request_model: str,
        contexto,
        history_text: Optional[str],
        is_local: bool = True,
        provider_cfg: Optional[dict] = None,
    ) -> Tuple[Any, StreamAttemptState]:
        """One stream-eligible generation attempt (llm_output_streaming_20260813 §3;
        cloud dispatch added live_session_fixes_20260925 T8)."""
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

        splitter = SentenceSplitter(first_chunk_boost=_first_chunk_boost_enabled(setup))
        # L1: the local transport hands `content` raw, so the think split the
        # cloud client applies runs here too. Cloud deltas are already split.
        think_splitter = ThinkTagStreamSplitter(pre_first_tag_holdback=0) if is_local else None
        orphan_close_handled = False
        accumulated = ""
        accumulated_thinking = ""
        final_chunk = None
        stream_opts = dict(setup.opciones_llm)
        if getattr(setup, "think", None) is not None:
            stream_opts["think"] = setup.think
        _trace = prompt_trace.begin(
            source=source,
            model=usage_ledger.call_model_name(
                request_model, is_local=is_local, provider_cfg=provider_cfg
            ),
            is_local=is_local,
            messages=setup.messages,
            options=stream_opts,
            sections=getattr(setup, "sections", ()),
            path="stream",
            decisions=getattr(setup, "decisions", None),
            profile=getattr(setup, "prompt_profile", None),
            layout=getattr(setup, "layout", None),
            evicted_pairs=getattr(setup, "evicted_pairs", None),
            head_moved=getattr(setup, "head_moved", None),
        )
        stream = self._host._llm_chat_streaming(
            is_local=is_local,
            provider_cfg=provider_cfg,
            timeout=setup.chat_timeout,
            model=request_model,
            messages=_with_cloud_images(setup.messages, setup, is_local),
            options=stream_opts,
        )
        try:
            with contextlib.closing(stream):
                stopped = False
                for chunk in stream:
                    final_chunk = chunk
                    # T8: a cloud SSE chunk is a plain dict (`{"message": {...}}`)
                    # -- `getattr(dict, "message", None)` never finds a KEY,
                    # only an ATTRIBUTE, so it silently returned None (every
                    # cloud delta would vanish) unless read by key first, the
                    # same dict-vs-attribute gap `rebuild_stream_response`
                    # already guards below. Ollama's ChatResponse objects keep
                    # using attribute access, unchanged.
                    msg = chunk.get("message") if isinstance(chunk, dict) else getattr(chunk, "message", None)
                    if isinstance(msg, dict):
                        delta = msg.get("content", "") or ""
                        delta_thinking = msg.get("thinking", "") or ""
                    elif msg is not None:
                        delta = getattr(msg, "content", "") or ""
                        delta_thinking = getattr(msg, "thinking", "") or ""
                    else:
                        delta = ""
                        delta_thinking = ""
                    if think_splitter is not None:
                        delta, split_thinking = think_splitter.feed(delta)
                        delta_thinking = split_thinking + delta_thinking
                        if think_splitter.orphan_close_seen and not orphan_close_handled:
                            orphan_close_handled = True
                            _think = getattr(setup, "think", None)
                            logger.info(
                                "[THINK_RESCUE] source=%s think=%s boost=%s "
                                "spoken_before=%s held_chars=%d",
                                source,
                                "-" if _think is None else ("on" if _think else "off"),
                                "on" if _first_chunk_boost_enabled(setup) else "off",
                                "yes" if state.job is not None else "no",
                                len(accumulated),
                            )
                            if state.job is None:
                                # Everything before the orphan </think> was
                                # reasoning: nothing of it was spoken yet, so
                                # drop what the sentence splitter still holds.
                                accumulated_thinking += accumulated
                                accumulated = ""
                                splitter = SentenceSplitter(first_chunk_boost=_first_chunk_boost_enabled(setup))
                                state.consume_only = False
                    accumulated += delta
                    accumulated_thinking += delta_thinking
                    if state.consume_only:
                        continue
                    for sentence, boundary in zip(
                        splitter.feed(delta), splitter.last_boundaries
                    ):
                        state.pending_boundary = boundary
                        action = self.handle_stream_sentence(sentence, state, setup)
                        if action == "revert":
                            if state.abort_on_revert:
                                # Close the stream (contextlib.closing below), the
                                # same path as every other abort: the socket close
                                # frees the Ollama runner. No dead generation.
                                stopped = True
                            else:
                                state.consume_only = True
                            break
                        if action != "continue":
                            stopped = True
                            break
                    if stopped:
                        break
                tail = ""
                if think_splitter is not None:
                    tail, tail_thinking = think_splitter.flush()
                    accumulated += tail
                    accumulated_thinking += tail_thinking
                if not stopped and not state.consume_only:
                    pending = splitter.feed(tail) if tail else []
                    pending_boundaries = list(splitter.last_boundaries) if tail else []
                    if _done_reason(final_chunk) == "length":
                        # L2: the last sentence was cut by the token limit;
                        # never speak it (logged once by the attempt loop).
                        splitter.discard()
                    else:
                        flushed = splitter.flush()
                        pending.extend(flushed)
                        pending_boundaries.extend(splitter.last_boundaries)
                    for sentence, boundary in zip(pending, pending_boundaries):
                        state.pending_boundary = boundary
                        action = self.handle_stream_sentence(sentence, state, setup)
                        if action != "continue":
                            break
        except BaseException as exc:
            turn_state.fail_reason("stream_error", exc)
            if _trace is not None:
                _trace.fail(exc, partial=accumulated, thinking=accumulated_thinking)
            if state.job is not None and not state.handled:
                self.stream_partial_exit(state, reason=type(exc).__name__)
            # T8: let execute_generation_attempt's except block see whether
            # THIS attempt already created a job (i.e., already spoke), even
            # though its own `stream_state` local never gets assigned on a
            # raise -- see the comment at that call site.
            exc._stream_attempt_state = state
            raise
        if state.abort_reason is not None and state.job is not None and not state.handled:
            self.stream_partial_exit(state, reason=state.abort_reason)
        if state.job is not None:
            stream_done_ms = max(0, int((time.time() - setup.start_llm) * 1000))
            turn_timing.add("llm_ms", stream_done_ms)
            logger.info(
                "[STREAM_DONE] source=%s elapsed_ms=%d chars=%d words=%d sentences=%d",
                source,
                stream_done_ms,
                len(state.spoken_prefix()),
                len(state.spoken_prefix().split()),
                len(state.appended_sentences),
            )
        respuesta = self.rebuild_stream_response(
            final_chunk, accumulated, accumulated_thinking
        )
        if _trace is not None:
            _trace.finish(respuesta)
        return respuesta, state

    def handle_stream_sentence(
        self, sentence: str, state: StreamAttemptState, setup: GenerationSetup
    ) -> str:
        """One closed sentence through the §3 loop (marks a reverted attempt)."""
        if web_sources.carries_web_context(setup) and state.pending_boundary == "sentence":
            if web_sources.is_source_line(sentence):
                # Not spoken yet: it is dropped if the reply ends here (the model's
                # own source line can be invented), spoken if more reply follows.
                state.held_source.append(sentence)
                return "continue"
            if state.held_source:
                held, state.held_source = state.held_source, []
                for text in held:
                    action = self._handle_one_sentence(text, state, setup)
                    if action != "continue":
                        return action
        return self._handle_one_sentence(sentence, state, setup)

    def _handle_one_sentence(
        self, sentence: str, state: StreamAttemptState, setup: GenerationSetup
    ) -> str:
        action = self._stream_sentence_action(sentence, state, setup)
        if action == "revert":
            if not state.reverted:
                turn_state.emit(TurnEvent.GUARD_REVERT)
            state.reverted = True
        return action

    def _note_guard_revert(self, state: StreamAttemptState, rule_id: str) -> None:
        """Record and log (rule id and counters only) a guard revert before any audio.

        Only no_code_output aborts the stream early: a local model may open with
        reasoning that ends in an orphan close tag (THINK_RESCUE), which needs the
        stream, and reasoning rarely contains a code fence or code-shaped lines."""
        state.reverted_rule = rule_id
        state.abort_on_revert = rule_id == NO_CODE_OUTPUT_RULE_ID
        logger.info(
            "[STREAM_REVERT] source=%s rule=%s sentence_index=%d aborted=%s",
            state.source, rule_id, state.sentence_index,
            "yes" if state.abort_on_revert else "no",
        )

    def _stream_sentence_action(
        self, sentence: str, state: StreamAttemptState, setup: GenerationSetup
    ) -> str:
        source = state.source
        if self._host._speech_cancelled(source):
            state.abort_reason = "speech_cancelled"
            return "abort"
        state.sentence_index += 1
        pieces = [p for p in self._host._sanitize_for_tts(sentence) if p.strip()]
        sanitized = " ".join(p.strip() for p in pieces)
        if not sanitized:
            # The completing chunk was empty after sanitizing: the pending soft
            # head must not leak into the next sentence's guard text.
            state.soft_head = ""
            return "continue"

        # tts-first-chunk-boost-20261015 C1: a soft first chunk ends mid-sentence.
        # The chunk that completes it is judged together with the head already
        # on air, so rules that need the whole sentence (R3 co-occurrence, R4
        # 'listo, ya lo hice', K8 tails) and sentence-initial anchors
        # ('confirmado') see the true sentence. The unfinished head itself is
        # judged without its trailing soft mark.
        guard_text = sentence
        if state.soft_head:
            guard_text = f"{state.soft_head} {sentence}"
            state.soft_head = ""
        elif state.pending_boundary == "soft":
            guard_text = sentence.rstrip(",;:— ")

        if state.in_fence or count_code_fence_markers(guard_text):
            state.in_fence = True
            if state.job is None:
                self._note_guard_revert(state, NO_CODE_OUTPUT_RULE_ID)
                return "revert"
            state.trip = (
                NO_CODE_OUTPUT_RULE_ID,
                state.sentence_index,
                len(state.appended_sentences),
            )
            return "truncate"
        allowed, reason = _output_guard_with_tts_check(guard_text, source=source)
        if not allowed:
            if state.job is None:
                self._note_guard_revert(state, _guard_rule_id(reason))
                return "revert"
            state.trip = (
                _guard_rule_id(reason),
                state.sentence_index,
                len(state.appended_sentences),
            )
            return "truncate"
        claim_rule = search_claim.violated_rule(guard_text, setup)
        if claim_rule is not None:
            search_claim.log_claim(
                claim_rule,
                stage="stream_revert" if state.job is None else "stream_truncate",
                source=source,
                chars=len(sentence),
                sentence_index=state.sentence_index,
            )
            if state.job is None:
                return "revert"
            state.trip = (
                claim_rule,
                state.sentence_index,
                len(state.appended_sentences),
            )
            return "truncate"
        if state.echo_detector is None:
            state.echo_detector = prompt_echo.detector_for(setup)
        if state.echo_detector.is_echo(guard_text):
            prompt_echo.log_prompt_echo(
                stage="stream_revert" if state.job is None else "stream_truncate",
                source=source,
                chars=len(sentence),
                sentence_index=state.sentence_index,
            )
            if state.job is None:
                return "revert"
            state.trip = (
                prompt_echo.PROMPT_ECHO_RULE_ID,
                state.sentence_index,
                len(state.appended_sentences),
            )
            return "truncate"
        leak = prompt_echo.leak_kind(guard_text, source)
        if leak is not None:
            prompt_echo.log_prompt_leak(
                stage="stream_revert" if state.job is None else "stream_truncate",
                source=source,
                kind=leak,
                chars=len(sentence),
                sentence_index=state.sentence_index,
            )
            if state.job is None:
                return "revert"
            state.trip = (
                prompt_echo.PROMPT_LEAK_RULE_ID,
                state.sentence_index,
                len(state.appended_sentences),
            )
            return "truncate"
        # L1: the UI's copy keeps markdown but never control/think markup.
        display_sentence = _strip_control_markup_for_display(sentence) or sanitized
        fragments = self._host._fragment_for_tts(pieces)
        if not fragments:
            return "continue"
        if state.job is None:
            state.router = self._host._ensure_router()
            read_turn = getattr(self._host, "_vision_read_turn_id", None)
            turn_kwargs = {} if read_turn is None else {"turn_id": read_turn}
            state.job = state.router.submit_streaming(
                source, priority_for_source(source), **turn_kwargs
            )
            if not state.router.append_chunks(state.job, fragments):
                state.abort_reason = "append_refused"
                return "abort"
            state.appended_sentences.append(sanitized)
            state.raw_sentences.append(display_sentence)
            if state.pending_boundary == "soft":
                state.soft_head = sentence
            emit_chunk = getattr(self._host, "_emit_stream_chunk", None)
            if callable(emit_chunk):
                state.web_searched = bool(getattr(setup, "web_searched", False))
                emit_chunk(state)
            first_audio_ms = max(0, int((time.time() - setup.start_llm) * 1000))
            turn_timing.add("ttfa_ms", first_audio_ms)
            logger.info(
                "[STREAM_TTFA] source=%s first_audio_submit_ms=%d "
                "first_sentence_words=%d first_sentence_chars=%d fragments=%d "
                "first_chunk_boundary=%s",
                source,
                first_audio_ms,
                len(sanitized.split()),
                len(sanitized),
                len(fragments),
                state.pending_boundary,
            )
            turn_state.emit(TurnEvent.FIRST_AUDIO, ms=first_audio_ms)
            return "continue"
        if not state.router.append_chunks(state.job, fragments):
            state.abort_reason = "append_refused"
            return "abort"
        state.appended_sentences.append(sanitized)
        state.raw_sentences.append(display_sentence)
        emit_chunk = getattr(self._host, "_emit_stream_chunk", None)
        if callable(emit_chunk):
            state.web_searched = bool(getattr(setup, "web_searched", False))
            emit_chunk(state)
        return "continue"

    def stream_partial_exit(self, state: StreamAttemptState, *, reason: str) -> None:
        """§7 partial-turn exit for a streamed turn that dies AFTER its first submit."""
        try:
            state.router.seal(state.job)
        except Exception as seal_exc:
            turn_state.fail_reason("stream_seal_failed", seal_exc)
            logger.exception("stream partial exit: seal failed")
        prefix = state.spoken_prefix()
        display_prefix = getattr(state, "display_prefix", lambda: prefix)()
        if prefix:
            self._host._commit_history(
                state.contexto, prefix, source=state.source,
                history_text=state.history_text,
            )
            self._host._streamed_turn_prefix = display_prefix or prefix
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
            return {
                "message": {"content": accumulated, "thinking": accumulated_thinking},
                "done": True,
            }
        if isinstance(final_chunk, dict):
            # T8: the cloud SSE generator's final marker chunk is a plain
            # dict with no "message" KEY (`{"done": True, "usage": ...}`) --
            # `hasattr(dict, "message")` is always False (that checks for an
            # ATTRIBUTE, not a key), so the ollama-object branches below
            # never fire for it and would silently drop the accumulated text.
            # Preserve every other key (usage, done, ...) and set/replace
            # "message" by key, the dict-native equivalent of the attribute
            # assignment the object branches below perform.
            final_chunk = dict(final_chunk)
            final_chunk["message"] = {"content": accumulated, "thinking": accumulated_thinking}
            return final_chunk
        if hasattr(final_chunk, "model_copy"):
            try:
                from ollama import ChatResponse, Message

                new_msg = Message(role="assistant", content=accumulated, thinking=accumulated_thinking)
                return final_chunk.model_copy(update={"message": new_msg, "done": True})
            except Exception:
                pass
        if hasattr(final_chunk, "message"):
            msg = final_chunk.message
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
        turn_state.emit(TurnEvent.STREAM_TRUNCATE)
        log_non_negotiable_block(
            rule_id,
            "stream_truncation",
            preview=f"sentence_index={sentence_index} spoken_upto={spoken_upto}",
        )
        # S1: a crisis turn is never truncated into silence; the deterministic
        # crisis reply is appended to the job before it is sealed.
        crisis_line = self._append_crisis_reply(state) if is_crisis_turn() else ""
        state.router.seal(state.job)
        state.handled = True
        self._host._live_stream_state = None
        self._host._streamed_turn_job = state.job
        if state.raw_sentences:
            prefix = " ".join(state.raw_sentences[:spoken_upto]).strip()
        else:
            prefix = state.spoken_prefix()
        return f"{prefix} {crisis_line}".strip() if crisis_line else prefix

    def _append_crisis_reply(self, state: StreamAttemptState) -> str:
        """Append the active locale's crisis reply to the live job; returns the
        reply text (so the committed turn matches what was said) or "" when it
        could not be appended."""
        reply = i18n_active.crisis_reply()
        try:
            pieces = [p for p in self._host._sanitize_for_tts(reply) if p.strip()]
            fragments = self._host._fragment_for_tts(pieces)
            if fragments and state.router.append_chunks(state.job, fragments):
                return reply
        except Exception:
            logger.exception("crisis reply append failed")
        return ""
