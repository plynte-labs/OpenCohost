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

        # 2. Grounding rules
        grounding_block = i18n_active.grounding_rules()
        system_parts = [system_prompt]
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

        messages, evicted_pairs, ctx_evicted = context_budget.apply_char_budget_pure(
            messages,
            ctx_limit=effective_ctx,
            max_output_tokens=settings.LLM_MAX_TOKENS if is_local else settings.CLOUD_MAX_TOKENS,
            safety_factor=settings.CHAR_BUDGET_SAFETY_FACTOR,
        )

        if ctx_evicted > 0 and on_evicted_pairs is not None:
            on_evicted_pairs(evicted_pairs, native_ctx, effective_ctx)

        # 10. Sampling options
        opciones_llm: dict[str, Any] = {
            "temperature": settings.LLM_TEMPERATURE,
            "top_p": settings.LLM_TOP_P,
            "num_predict": settings.LLM_MAX_TOKENS if is_local else settings.CLOUD_MAX_TOKENS,
            "num_ctx": effective_ctx,
        }

        if is_local and "gemma" in request_model.lower():
            opciones_llm.pop("num_ctx", None)
            opciones_llm["temperature"] = 0.7

        if is_local and is_reasoning_model is not None and is_reasoning_model(request_model):
            opciones_llm.pop("num_predict", None)
            logger.debug("Modelo de razonamiento detectado. Límite de tokens removido.")

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
        )
