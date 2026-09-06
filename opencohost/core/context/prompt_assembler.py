"""
opencohost/core/context/prompt_assembler.py

Pure prompt assembly, context compilation, and sampling options builder.
Extracted from MotorVocalIA._build_generation_request.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional, Sequence

from opencohost.config import settings
from opencohost.core.context import context_budget
from opencohost.core.profiles import personalization
from opencohost.i18n import active as i18n_active

logger = logging.getLogger("OpenCohost")

# Source sets controlling context injection and filtering
DIGEST_CAPTURE_SOURCES = frozenset({"direct", "ptt", settings.OWNER_BUNDLE_SOURCE})
PERSONALIZATION_INJECT_SOURCES = frozenset({"direct", "ptt", settings.OWNER_BUNDLE_SOURCE})
MEMORIA_INJECT_SOURCES = frozenset({"direct", "ptt", settings.OWNER_BUNDLE_SOURCE})
DIGEST_INJECT_SOURCES = frozenset({"direct", "ptt", settings.OWNER_BUNDLE_SOURCE})
EDITORIAL_INJECT_SOURCES = frozenset({"direct", "ptt", settings.OWNER_BUNDLE_SOURCE})
HISTORY_ASSISTANT_ONLY_SOURCES = frozenset({"chat"})


@dataclass
class GenerationSetup:
    """Carries everything needed by the inference attempts and finalization."""

    messages: list[dict[str, Any]]
    opciones_llm: dict[str, Any]
    chat_timeout: float
    max_intentos: int
    start_llm: float
    native_ctx: int
    effective_ctx: int
    ctx_evicted: int
    editorial_block: str
    history_snapshot: list[dict[str, Any]]
    think: Optional[bool] = None
    intent: str = "chat"
    tts_eligible: bool = True
    budget_resolution: Optional[Any] = None
    preset: str = "balanced"
    requested_budget: Optional[int] = None


# Backward compatibility alias
_GenerationSetup = GenerationSetup


class PromptContextAssembler:
    """Encapsulates pure prompt construction and context budgeting."""

    def __init__(
        self,
        *,
        sanitize_history_fn: Optional[Callable[[str], str]] = None,
    ) -> None:
        self._sanitize_history_fn = sanitize_history_fn

    def assemble(
        self,
        contexto: str,
        source: str,
        *,
        system_prompt: str,
        use_system_role: bool,
        is_local: bool,
        provider_cfg: dict[str, Any],
        request_model: str,
        history_snapshot: Sequence[dict[str, Any]],
        digest_block: str = "",
        memorias_profile_id: Optional[str] = None,
        memorias_builder: Optional[Callable[[str, str], str]] = None,
        editorial_provider: Optional[Callable[[str], Optional[str]]] = None,
        native_ctx_resolver: Optional[Callable[[str], int]] = None,
        effective_ctx_resolver: Optional[Callable[[str, int], int]] = None,
        on_evicted_pairs: Optional[Callable[[list[tuple[dict[str, Any], dict[str, Any]]], int, int], None]] = None,
        is_reasoning_model: Optional[Callable[[str], bool]] = None,
        timeout_resolver: Optional[Callable[..., float]] = None,
        watchdog_timeout: Optional[float] = None,
        personalization_enabled: Optional[bool] = None,
        memorias_enabled: Optional[bool] = None,
        episodic_memory_block: str = "",
        intent: str = "chat",
        reasoning_settings_resolver: Optional[Callable[[str], dict[str, Any]]] = None,
        telemetry_tracker: Optional[Any] = None,
        budget_resolver: Optional[Callable[..., Any]] = None,
        residency_ratio_resolver: Optional[Callable[[str], Optional[float]]] = None,
        residency_ratio: Optional[float] = None,
    ) -> GenerationSetup:
        """Assemble messages, apply context budgets, and build sampling options."""
        messages: list[dict[str, Any]] = []

        # 1. Personalization block
        if personalization_enabled is None:
            personalization_enabled = settings.PERSONALIZATION_ENABLED
        if memorias_enabled is None:
            memorias_enabled = settings.MEMORIAS_ENABLED

        personalization_block = ""
        if source in PERSONALIZATION_INJECT_SOURCES and personalization_enabled:
            try:
                sanitize_fn = self._sanitize_history_fn
                personalization_block = personalization.build_injection_block(sanitize_fn)
            except Exception:
                personalization_block = ""

        # 2. Grounding rules & intent adaptation
        system_text = system_prompt
        clean_intent = str(intent or "chat").strip().lower()
        if clean_intent in ("drafting", "inferenceintent.drafting"):
            for pat in (
                "- Respondes en 2-4 oraciones. Nunca monólogos, pero tampoco monosílabos\n",
                "- Respondes en 2-4 oraciones. Nunca monólogos, pero tampoco monosílabos",
                "Respondes en 2-4 oraciones. Nunca monólogos, pero tampoco monosílabos",
            ):
                system_text = system_text.replace(pat, "")
            system_text += "\n\nMODO REDACCIÓN: Genera respuestas completas, estructuradas y detalladas sin restricción de brevedad."

        grounding_block = i18n_active.grounding_rules()
        system_parts = [system_text]
        if grounding_block:
            system_parts.append(grounding_block)
        if personalization_block:
            system_parts.append(personalization_block)
        system_content = "\n\n".join(system_parts)

        if use_system_role:
            messages.append({"role": "system", "content": system_content})

        # 3. History window projection
        history_list = list(history_snapshot)
        history_assistant_only = source in HISTORY_ASSISTANT_ONLY_SOURCES
        last_agenda_idx = -1
        if history_assistant_only:
            for idx, msg in enumerate(history_list):
                if (
                    isinstance(msg, dict)
                    and msg.get("role") == "assistant"
                    and str(msg.get("source", "")).startswith("kira-agenda")
                ):
                    last_agenda_idx = idx

        for idx, msg in enumerate(history_list):
            if not isinstance(msg, dict):
                continue
            if history_assistant_only:
                if msg.get("role") != "assistant":
                    continue
                if idx != last_agenda_idx and str(msg.get("source", "")).startswith("kira-agenda"):
                    continue
            messages.append({"role": msg["role"], "content": msg["content"]})

        # 4. Memorias block (RAG)
        memorias_block = ""
        if source in MEMORIA_INJECT_SOURCES and memorias_enabled and memorias_profile_id:
            if memorias_builder is not None:
                try:
                    memorias_block = memorias_builder(memorias_profile_id, contexto) or ""
                except Exception:
                    memorias_block = ""

        # 5. Editorial context
        editorial_block = ""
        if source in EDITORIAL_INJECT_SOURCES and editorial_provider is not None:
            try:
                editorial_block = editorial_provider(contexto) or ""
            except Exception:
                editorial_block = ""

        if editorial_block:
            enriched = f"{contexto}\n\n{editorial_block}"
            logger.info(
                "editorial direct context injected (source=%s, len=%d)",
                source,
                len(editorial_block),
            )
        else:
            enriched = contexto

        # 6. Digest block (<memoria_de_fondo>)
        if source in DIGEST_INJECT_SOURCES and digest_block:
            wrapped_digest = (
                i18n_active.memory_block_open()
                + "\n"
                + digest_block
                + "\n"
                + i18n_active.memory_block_close()
            )
            enriched = f"{wrapped_digest}\n\n{enriched}"
            logger.debug("L1 digest injected into direct prompt (len=%d)", len(digest_block))

        # 7. Memorias prepended before digest
        if source in MEMORIA_INJECT_SOURCES and memorias_block:
            enriched = f"{memorias_block}\n\n{enriched}"

        # 7b. Episodic memory v5 block
        if episodic_memory_block:
            enriched = f"{episodic_memory_block}\n\n{enriched}"

        # 8. User message framing
        if use_system_role:
            messages.append({"role": "user", "content": enriched})
        else:
            prompt_completo = f"{system_content}\n\n[{i18n_active.user_message_label()}]: {enriched}"
            messages.append({"role": "user", "content": prompt_completo})

        # 9. Context budget discovery and partitioning
        if is_local:
            native_ctx = (
                native_ctx_resolver(request_model)
                if native_ctx_resolver is not None
                else settings.CTX_FALLBACK_DEFAULT
            )
            effective_ctx = (
                effective_ctx_resolver(request_model, native_ctx)
                if effective_ctx_resolver is not None
                else native_ctx
            )
        else:
            native_ctx = settings.CLOUD_CTX_BUDGET
            effective_ctx = settings.CLOUD_CTX_BUDGET

        is_reasoning = bool(is_reasoning_model is not None and is_reasoning_model(request_model))
        r_cfg = reasoning_settings_resolver(request_model) if reasoning_settings_resolver else {}
        r_enabled = bool(r_cfg.get("enabled", False))
        if is_reasoning or r_enabled:
            r_budget = r_cfg.get("budget_tokens")
            r_preset = str(r_cfg.get("preset", "balanced"))
        else:
            r_enabled = False
            r_budget = None if is_local else settings.CLOUD_MAX_TOKENS
            r_preset = "balanced" if is_local else "custom"

        # Determine target output reserve for the context gate based on intent and requested budget
        if clean_intent == "drafting":
            target_output_reserve = max(2048, int(r_budget or 2048))
        elif r_budget is not None and int(r_budget) > 0:
            target_output_reserve = int(r_budget)
        else:
            target_output_reserve = settings.LLM_MAX_TOKENS if is_local else settings.CLOUD_MAX_TOKENS

        messages, evicted_pairs, ctx_evicted = context_budget.apply_char_budget_pure(
            messages,
            ctx_limit=effective_ctx,
            max_output_tokens=target_output_reserve,
            safety_factor=settings.CHAR_BUDGET_SAFETY_FACTOR,
        )

        if ctx_evicted > 0 and on_evicted_pairs is not None:
            on_evicted_pairs(evicted_pairs, native_ctx, effective_ctx)

        # Estimate prompt tokens conservatively from retained messages using the char/token safety factor
        estimated_prompt_chars = sum(len(m.get("content", "")) for m in messages if isinstance(m, dict))
        estimated_prompt_tokens = max(1, int(estimated_prompt_chars / max(1.0, float(settings.CHAR_BUDGET_SAFETY_FACTOR))))

        # 10. Sampling options & budget governance
        from opencohost.core.engine.llm_budget_engine import (
            resolve_generation_budget,
            InferenceIntent,
            BudgetResolution,
            BudgetVerdict,
        )

        opciones_llm: dict[str, Any] = {
            "temperature": settings.LLM_TEMPERATURE,
            "top_p": settings.LLM_TOP_P,
            "num_predict": settings.LLM_MAX_TOKENS if is_local else settings.CLOUD_MAX_TOKENS,
            "num_ctx": effective_ctx,
        }

        if is_local and "gemma" in request_model.lower():
            # Gemma models benefit from temperature 0.7; keep num_ctx: effective_ctx so
            # OpenCohost allocated_context and Ollama VRAM allocation remain 100% aligned.
            opciones_llm["temperature"] = 0.7

        think_param: Optional[bool] = None
        tts_eligible: bool = (clean_intent != "drafting")
        budget_res: Optional[BudgetResolution] = None

        if budget_resolver is not None:
            try:
                budget_res = budget_resolver(
                    intent=clean_intent,
                    allocated_context=effective_ctx,
                    request_model=request_model,
                    is_local=is_local,
                )
            except Exception:
                budget_res = None

        if budget_res is None:
            tps_ewma = 0.0
            if telemetry_tracker is not None:
                try:
                    prof = telemetry_tracker.get_profile(
                        provider="local" if is_local else "cloud",
                        model_id=request_model,
                        allocated_context=effective_ctx,
                    )
                    # ADR-056: Only authorize empirical EWMA TPS when profile is CALIBRATED (>= 10 samples).
                    # COLD and WARMING profiles observe without altering generation budgets.
                    state = getattr(prof, "state", None)
                    state_val = getattr(state, "value", str(state))
                    if state_val == "CALIBRATED":
                        tps_ewma = float(getattr(prof, "ewma_tps", 0.0) or 0.0)
                    else:
                        tps_ewma = 0.0
                except Exception:
                    tps_ewma = 0.0

            res_ratio: Optional[float] = residency_ratio
            if res_ratio is None and residency_ratio_resolver is not None:
                try:
                    res_ratio = residency_ratio_resolver(request_model)
                except Exception:
                    res_ratio = None

            parsed_intent = InferenceIntent.CHAT
            for it in InferenceIntent:
                if it.value == clean_intent:
                    parsed_intent = it
                    break

            budget_res = resolve_generation_budget(
                intent=parsed_intent,
                allocated_context=effective_ctx,
                prompt_tokens=estimated_prompt_tokens,
                requested_budget=r_budget,
                preset=r_preset,
                reasoning_enabled=r_enabled,
                tps_ewma=tps_ewma,
                residency_ratio=res_ratio,
            )

            # ADR-056: If BUDGET_INFEASIBLE, attempt emergency trimming of history pairs before abort
            if budget_res is not None and budget_res.verdict == BudgetVerdict.BUDGET_INFEASIBLE:
                total_dropped = 0
                while budget_res.verdict == BudgetVerdict.BUDGET_INFEASIBLE:
                    dropped = context_budget.trim_messages_reactive(messages, n_pairs=3)
                    if dropped == 0:
                        break
                    total_dropped += dropped
                    ctx_evicted += dropped * 2
                    re_chars = sum(len(m.get("content", "")) for m in messages if isinstance(m, dict))
                    re_prompt_tokens = max(1, int(re_chars / max(1.0, float(settings.CHAR_BUDGET_SAFETY_FACTOR))))
                    budget_res = resolve_generation_budget(
                        intent=parsed_intent,
                        allocated_context=effective_ctx,
                        prompt_tokens=re_prompt_tokens,
                        requested_budget=r_budget,
                        preset=r_preset,
                        reasoning_enabled=r_enabled,
                        tps_ewma=tps_ewma,
                        residency_ratio=res_ratio,
                    )
                if total_dropped > 0 and on_evicted_pairs is not None:
                    on_evicted_pairs([], native_ctx, effective_ctx)

        if budget_res is not None:
            opciones_llm["num_predict"] = budget_res.effective_budget
            think_param = budget_res.think if (is_reasoning or r_enabled) else None
            tts_eligible = budget_res.tts_eligible and (budget_res.verdict != BudgetVerdict.BUDGET_INFEASIBLE)
        elif is_local and is_reasoning_model is not None and is_reasoning_model(request_model):
            r_cfg = reasoning_settings_resolver(request_model) if reasoning_settings_resolver else {}
            r_enabled = bool(r_cfg.get("enabled", False))
            r_budget = int(r_cfg.get("budget_tokens", 512))
            think_param = r_enabled
            opciones_llm["num_predict"] = max(32, r_budget)

        if source == "chat":
            opciones_llm["repeat_penalty"] = settings.CHAT_REPEAT_PENALTY
            opciones_llm["presence_penalty"] = settings.CHAT_PRESENCE_PENALTY
            opciones_llm["frequency_penalty"] = settings.CHAT_FREQUENCY_PENALTY

        start_llm = time.time()
        max_intentos = 2

        if watchdog_timeout is not None:
            chat_timeout = watchdog_timeout
        elif timeout_resolver is not None:
            chat_timeout = timeout_resolver(
                request_model, provider_cfg=provider_cfg, is_local=is_local
            )
        else:
            chat_timeout = 30.0

        return GenerationSetup(
            messages=messages,
            opciones_llm=opciones_llm,
            chat_timeout=chat_timeout,
            max_intentos=max_intentos,
            start_llm=start_llm,
            native_ctx=native_ctx,
            effective_ctx=effective_ctx,
            ctx_evicted=ctx_evicted,
            editorial_block=editorial_block,
            history_snapshot=history_list,
            think=think_param,
            intent=clean_intent,
            tts_eligible=tts_eligible,
            budget_resolution=budget_res,
            preset=r_preset,
            requested_budget=r_budget,
        )
