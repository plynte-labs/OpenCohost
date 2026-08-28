"""
opencohost/core/context/generation_evaluator.py

Pure post-generation output inspection, telemetry extraction, and guardrail evaluation.
Extracted from MotorVocalIA._finalize_generation.
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Optional, Sequence

from opencohost.config import settings
from opencohost.config.settings import (
    CLAUSE_SANITIZER_SOURCES,
    CTX_PRESSURE_HIGH_THRESHOLD,
)
from opencohost.core.context import context_budget
from opencohost.core.context.prompt_assembler import GenerationSetup
from opencohost.core.context.repetition_guard import (
    DEFAULT_CONFIG as REPETITION_CONFIG,
    RepetitionResult as RepetitionVerdict,
    ClauseSanitizeResult as SanitizeVerdict,
    detect_repetition,
    sanitize_clause_repetition,
)
from opencohost.config.validation import output_guard
from opencohost.core.speech.tts_sanitizer import _sanitize_tts_text_for_playback
from opencohost.i18n import active as i18n_active

logger = logging.getLogger("OpenCohost")

_GUARD_RULE_ID_RE = re.compile(r"\[([a-z0-9_]+)\]")


def guard_rule_id(reason: str) -> str:
    """Extract rule ID from guardrail reason string in brackets."""
    match = _GUARD_RULE_ID_RE.search(reason or "")
    return match.group(1) if match else "unknown_rule"


def output_guard_with_tts_check(
    text: str,
    source: str,
    output_guard_fn: Optional[Callable[..., tuple[bool, str]]] = None,
) -> tuple[bool, str]:
    """Check text with output_guard on raw text and on tts-sanitized text."""
    guard_fn = output_guard_fn or output_guard
    allowed, reason = guard_fn(text, source=source)
    if not allowed:
        return allowed, reason
    sanitized = _sanitize_tts_text_for_playback(text)
    allowed, reason = guard_fn(sanitized, source=source)
    if not allowed:
        return allowed, f"[tts-sanitized] {reason}"
    return True, ""


@dataclass
class CtxTelemetryData:
    """Encapsulates context telemetry metrics and bounded ring snapshot."""

    snapshot: dict[str, Any]
    utilization: float
    pressure_high: bool
    pec: int
    load_ms: float
    prefill_ms: float
    decode_ms: float
    eval_count: int


@dataclass
class ModelTraceData:
    """Encapsulates audit string and model-mismatch check result."""

    trace_msg: str
    trace_provider: str
    trace_transport: str
    trace_fallback_active: bool
    is_mismatch: bool


@dataclass
class EvaluationResult:
    """Carries the evaluated properties and verdicts of a raw generation outcome."""

    dialogo: str
    elapsed: float
    telemetry: Optional[CtxTelemetryData]
    model_trace: ModelTraceData
    clause_verdict: Optional[SanitizeVerdict]
    guard_allowed: bool
    guard_reason: str
    repetition_verdict: Optional[RepetitionVerdict]


class GenerationEvaluator:
    """Encapsulates pure generation inspection, normalization, and evaluation."""

    def __init__(
        self,
        *,
        output_guard_fn: Optional[Callable[..., tuple[bool, str]]] = None,
        guardrail_fallback_fn: Optional[Callable[[str, str], str]] = None,
        detect_repetition_fn: Optional[Callable[..., RepetitionVerdict]] = None,
        sanitize_clause_fn: Optional[Callable[[str], SanitizeVerdict]] = None,
    ) -> None:
        self._output_guard_fn = output_guard_fn
        self._guardrail_fallback_fn = guardrail_fallback_fn
        self._detect_repetition_fn = detect_repetition_fn
        self._sanitize_clause_fn = sanitize_clause_fn

    def resolve_fallback_line(self, source: str, reason: str = "") -> str:
        """Resolve neutral fallback line for guardrail or repetition blocks."""
        if self._guardrail_fallback_fn is not None:
            try:
                res = self._guardrail_fallback_fn(source, reason)
                if res is not None:
                    return res
            except Exception:
                pass
        lines = i18n_active.LEGACY_GUARDRAIL_FALLBACK_LINES
        if lines:
            import random

            return random.choice(lines)
        return ""

    def evaluate(
        self,
        raw_content: str,
        respuesta: Any,
        setup: GenerationSetup,
        *,
        source: str,
        is_local: bool,
        provider_cfg: dict[str, Any],
        request_model: str,
        desired_model: str,
        current_model: str,
        loaded_model: Optional[str],
        current_profile_name: str,
        is_streamed: bool = False,
        cfg_is_local_fn: Optional[Callable[[dict[str, Any]], bool]] = None,
        agenda_output_sanitizer: Optional[Callable[[str], str]] = None,
        agenda_output_transformer: Optional[Callable[[str], str]] = None,
        clause_sanitizer_sources: Optional[frozenset[str]] = None,
        sanitize_clause_fn: Optional[Callable[[str], SanitizeVerdict]] = None,
    ) -> EvaluationResult:
        """Evaluate raw model response, extract telemetry, audit trace, and check guards."""
        start_llm = setup.start_llm
        _native_ctx = setup.native_ctx
        _effective_ctx = setup.effective_ctx
        _ctx_evicted = setup.ctx_evicted
        history_snapshot = setup.history_snapshot

        # 1. Output extraction & unicode cleanup
        dialogo = (raw_content or "").strip("\x00\ufeff \t\r\n")
        elapsed = time.time() - start_llm

        # 2. Context telemetry derivation
        _pec_raw = getattr(respuesta, "prompt_eval_count", 0) if respuesta is not None else 0
        _pec_final = _pec_raw if isinstance(_pec_raw, (int, float)) else 0
        telemetry_data: Optional[CtxTelemetryData] = None

        if _pec_final > 0:
            _util = context_budget.utilization(_pec_final, _effective_ctx)
            _predur = getattr(respuesta, "prompt_eval_duration", 0)
            _prefill_ms = (_predur / 1e6) if isinstance(_predur, (int, float)) else 0.0
            _evaldur = getattr(respuesta, "eval_duration", 0)
            _decode_ms = (_evaldur / 1e6) if isinstance(_evaldur, (int, float)) else 0.0
            _loaddur = getattr(respuesta, "load_duration", 0)
            _load_ms = (_loaddur / 1e6) if isinstance(_loaddur, (int, float)) else 0.0
            _ec_raw = getattr(respuesta, "eval_count", 0)
            _ec_final = _ec_raw if isinstance(_ec_raw, (int, float)) else 0

            _ctx_provider = (
                "local" if is_local else (provider_cfg.get("active_provider") or "local")
            )
            _ctx_snapshot = {
                "request_id": str(uuid.uuid4()),
                "timestamp": time.time(),
                "source": source,
                "provider": _ctx_provider,
                "model": request_model,
                "native_ctx": _native_ctx,
                "effective_ctx": _effective_ctx,
                "ratio": _util,
                "prompt_eval_count": _pec_final,
                "load_ms": _load_ms,
                "prefill_ms": _prefill_ms,
                "decode_ms": _decode_ms,
                "eval_count": _ec_final,
                "evicted_pairs": _ctx_evicted,
            }
            pressure_high = _util >= CTX_PRESSURE_HIGH_THRESHOLD
            telemetry_data = CtxTelemetryData(
                snapshot=_ctx_snapshot,
                utilization=_util,
                pressure_high=pressure_high,
                pec=_pec_final,
                load_ms=_load_ms,
                prefill_ms=_prefill_ms,
                decode_ms=_decode_ms,
                eval_count=_ec_final,
            )

        # 3. Model trace & mismatch diagnosis
        generation_model = request_model
        desired = desired_model
        active = current_model
        loaded = loaded_model or "unknown"
        is_cfg_local = cfg_is_local_fn(provider_cfg) if cfg_is_local_fn is not None else True
        trace_provider = (
            "local" if is_local else (provider_cfg.get("active_provider") or "local")
        )
        trace_transport = "local" if is_local else "cloud"
        trace_fallback_active = is_local and not is_cfg_local
        trace_msg = (
            f"[MODEL_TRACE] desired={desired} active={active} "
            f"loaded={loaded} generation={generation_model} "
            f"profile={current_profile_name} source={source} "
            f"provider={trace_provider} transport={trace_transport} "
            f"fallback_active={trace_fallback_active}"
        )
        if trace_transport == "cloud":
            profiles = provider_cfg.get("profiles")
            profile_cfg = (
                profiles.get(trace_provider) if isinstance(profiles, dict) else None
            )
            cloud_model = (
                profile_cfg.get("model") if isinstance(profile_cfg, dict) else None
            ) or "unknown"
            trace_msg += f" cloud_model={cloud_model}"

        cloud_by_design = trace_transport == "cloud" and not trace_fallback_active
        is_mismatch = (
            desired != active or active != loaded or loaded != generation_model
        ) and not cloud_by_design
        model_trace = ModelTraceData(
            trace_msg=trace_msg,
            trace_provider=trace_provider,
            trace_transport=trace_transport,
            trace_fallback_active=trace_fallback_active,
            is_mismatch=is_mismatch,
        )

        # 4. Agenda output sanitizing
        if source.startswith("kira-agenda"):
            if agenda_output_sanitizer is not None:
                dialogo = agenda_output_sanitizer(dialogo)
            if agenda_output_transformer is not None:
                try:
                    dialogo = agenda_output_transformer(dialogo)
                except Exception:
                    logger.exception("Agenda output transformer failed")

        # 5. Clause repetition inspection and text repair (applied before guardrails)
        clause_verdict: Optional[SanitizeVerdict] = None
        effective_clause_sources = (
            clause_sanitizer_sources
            if clause_sanitizer_sources is not None
            else getattr(settings, "CLAUSE_SANITIZER_SOURCES", frozenset({"kira-agenda", "chat"}))
        )
        effective_clause_fn = (
            sanitize_clause_fn or self._sanitize_clause_fn or sanitize_clause_repetition
        )
        if source in effective_clause_sources and dialogo:
            clause_verdict = effective_clause_fn(dialogo)
            if not is_streamed and clause_verdict is not None:
                if not (clause_verdict.verdict == "rejected" and source.startswith("kira-agenda")):
                    dialogo = clause_verdict.text

        # 6. Output guard check (skipped for streamed turns where stream guard manages sentences)
        guard_allowed = True
        guard_reason = ""
        if not is_streamed and dialogo:
            guard_allowed, guard_reason = output_guard_with_tts_check(
                dialogo, source=source, output_guard_fn=self._output_guard_fn
            )

        # 7. Chat repetition check
        repetition_verdict: Optional[RepetitionVerdict] = None
        if source == "chat" and dialogo:
            recent_outputs = [
                m.get("content", "")
                for m in history_snapshot
                if isinstance(m, dict) and m.get("role") == "assistant"
            ][-REPETITION_CONFIG.window :]
            rep_fn = self._detect_repetition_fn or detect_repetition
            repetition_verdict = rep_fn(dialogo, recent_outputs)

        return EvaluationResult(
            dialogo=dialogo,
            elapsed=elapsed,
            telemetry=telemetry_data,
            model_trace=model_trace,
            clause_verdict=clause_verdict,
            guard_allowed=guard_allowed,
            guard_reason=guard_reason,
            repetition_verdict=repetition_verdict,
        )
