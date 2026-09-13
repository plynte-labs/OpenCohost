"""
tests/test_prompt_context_assembler.py

Unit tests for PromptContextAssembler component.
"""

from unittest.mock import MagicMock

import pytest

from opencohost.config import settings
from opencohost.core.context.prompt_assembler import (
    DIGEST_CAPTURE_SOURCES,
    DIGEST_INJECT_SOURCES,
    EDITORIAL_INJECT_SOURCES,
    HISTORY_ASSISTANT_ONLY_SOURCES,
    MEMORIA_INJECT_SOURCES,
    OWNER_POLICY_SOURCES,
    OWNER_RESPONSE_POLICY,
    PERSONALIZATION_INJECT_SOURCES,
    GenerationSetup,
    PromptContextAssembler,
)


def test_basic_assemble_with_system_role():
    assembler = PromptContextAssembler()
    setup = assembler.assemble(
        "hola mundo",
        "direct",
        system_prompt="Eres Kira.",
        use_system_role=True,
        is_local=True,
        provider_cfg={},
        request_model="llama3",
        history_snapshot=[],
    )

    assert isinstance(setup, GenerationSetup)
    assert len(setup.messages) == 2
    assert setup.messages[0]["role"] == "system"
    assert "Eres Kira." in setup.messages[0]["content"]
    assert setup.messages[1]["role"] == "user"
    assert setup.messages[1]["content"] == "hola mundo"
    assert setup.opciones_llm["num_predict"] == 768
    assert setup.opciones_llm["temperature"] == 0.8


def test_basic_assemble_without_system_role():
    assembler = PromptContextAssembler()
    setup = assembler.assemble(
        "hola mundo",
        "direct",
        system_prompt="Eres Kira.",
        use_system_role=False,
        is_local=True,
        provider_cfg={},
        request_model="llama3",
        history_snapshot=[],
    )

    assert len(setup.messages) == 1
    assert setup.messages[0]["role"] == "user"
    assert "Eres Kira." in setup.messages[0]["content"]
    assert "hola mundo" in setup.messages[0]["content"]


def test_chat_source_applies_assistant_only_and_repetition_penalties():
    assembler = PromptContextAssembler()
    history = [
        {"role": "user", "content": "pregunta 1", "source": "chat"},
        {"role": "assistant", "content": "respuesta agenda 1", "source": "kira-agenda"},
        {"role": "assistant", "content": "respuesta agenda 2", "source": "kira-agenda"},
        {"role": "assistant", "content": "respuesta normal", "source": "chat"},
    ]
    setup = assembler.assemble(
        "mensaje de chat",
        "chat",
        system_prompt="Eres Kira.",
        use_system_role=True,
        is_local=True,
        provider_cfg={},
        request_model="llama3",
        history_snapshot=history,
    )

    # In chat mode: user slots dropped, only the LAST agenda assistant slot kept, plus chat assistant slot
    roles_and_contents = [(m["role"], m["content"]) for m in setup.messages]
    assert ("system", setup.messages[0]["content"]) in roles_and_contents
    assert ("assistant", "respuesta agenda 2") in roles_and_contents
    assert ("assistant", "respuesta agenda 1") not in roles_and_contents
    assert ("assistant", "respuesta normal") in roles_and_contents
    assert ("user", "mensaje de chat") in roles_and_contents

    # Sampling penalties applied for chat
    assert "repeat_penalty" in setup.opciones_llm
    assert "presence_penalty" in setup.opciones_llm
    assert "frequency_penalty" in setup.opciones_llm


def test_context_enrichments_ordering_memorias_before_digest_and_editorial():
    assembler = PromptContextAssembler()
    setup = assembler.assemble(
        "pregunta del host",
        "direct",
        system_prompt="Eres Kira.",
        use_system_role=True,
        is_local=True,
        provider_cfg={},
        request_model="llama3",
        history_snapshot=[],
        digest_block="linea digest 1",
        memorias_profile_id="default_profile",
        memorias_builder=lambda pid, ctx: "<memorias_guardadas>Dato 1</memorias_guardadas>",
        editorial_provider=lambda ctx: "<editorial_context>Card 1</editorial_context>",
    )

    user_msg = setup.messages[1]["content"]
    assert "<memorias_guardadas>Dato 1</memorias_guardadas>" in user_msg
    assert "linea digest 1" in user_msg
    assert "<editorial_context>Card 1</editorial_context>" in user_msg

    # Ordering: memorias appears before digest wrapper which appears before user query + editorial
    mem_idx = user_msg.index("<memorias_guardadas>")
    dig_idx = user_msg.index("linea digest 1")
    edt_idx = user_msg.index("<editorial_context>")
    assert mem_idx < dig_idx < edt_idx


def test_reasoning_model_detection_retains_governed_num_predict():
    assembler = PromptContextAssembler()
    setup = assembler.assemble(
        "calcula algo",
        "direct",
        system_prompt="Eres Kira.",
        use_system_role=True,
        is_local=True,
        provider_cfg={},
        request_model="qwq:latest",
        history_snapshot=[],
        is_reasoning_model=lambda m: True,
    )

    # Under ADR-056, num_predict is never dropped; it is bounded by budget governance
    assert "num_predict" in setup.opciones_llm
    assert setup.think is False


def test_evicted_pairs_callback_called():
    assembler = PromptContextAssembler()
    evicted_callback = MagicMock()
    # Provide a huge history that exceeds a tiny ctx budget
    long_history = [{"role": "user", "content": "x" * 2000}, {"role": "assistant", "content": "y" * 2000}] * 10

    setup = assembler.assemble(
        "pregunta",
        "direct",
        system_prompt="System",
        use_system_role=True,
        is_local=True,
        provider_cfg={},
        request_model="llama3",
        history_snapshot=long_history,
        effective_ctx_resolver=lambda m, n: 512,  # tiny budget forces eviction
        on_evicted_pairs=evicted_callback,
    )

    assert setup.ctx_evicted > 0
    evicted_callback.assert_called_once()


def test_cloud_assemble_applies_cloud_ceilings():
    assembler = PromptContextAssembler()
    setup = assembler.assemble(
        "hola mundo",
        "direct",
        system_prompt="Eres Kira.",
        use_system_role=True,
        is_local=False,
        provider_cfg={"active_provider": "openrouter", "profiles": {}},
        request_model="claude-3-5-sonnet",
        history_snapshot=[],
    )

    assert setup.opciones_llm["num_predict"] == 16384
    assert setup.native_ctx == 32768
    assert setup.effective_ctx == 32768


def test_prompt_tokens_estimated_and_drafting_budget_reserved():
    assembler = PromptContextAssembler()
    setup = assembler.assemble(
        "redacta un ensayo largo sobre inteligencia artificial",
        "direct",
        system_prompt="Eres un redactor experto.",
        use_system_role=True,
        is_local=True,
        provider_cfg={},
        request_model="llama3",
        history_snapshot=[],
        intent="drafting",
        effective_ctx_resolver=lambda m, n: 8192,
    )

    # Budget resolution was executed with non-zero prompt tokens and drafting intent
    assert setup.budget_resolution is not None
    assert setup.budget_resolution.prompt_tokens > 0
    # Drafting intent reserves at least 2048 output tokens
    assert setup.budget_resolution.effective_budget >= 2048
    assert setup.tts_eligible is False


def test_gemma_preserves_num_ctx_and_sets_temperature():
    assembler = PromptContextAssembler()
    setup = assembler.assemble(
        "hola mundo",
        "direct",
        system_prompt="Eres Kira.",
        use_system_role=True,
        is_local=True,
        provider_cfg={},
        request_model="gemma4:e4b",
        history_snapshot=[],
        effective_ctx_resolver=lambda m, n: 16384,
    )

    assert setup.opciones_llm["num_ctx"] == 16384
    assert setup.opciones_llm["temperature"] == 0.7


def test_owner_policy_appears_exactly_once_for_allowed_sources():
    assembler = PromptContextAssembler()
    allowed_sources = ["direct", "ptt", settings.OWNER_BUNDLE_SOURCE]
    assert OWNER_POLICY_SOURCES == frozenset({"direct", "ptt", settings.OWNER_BUNDLE_SOURCE})

    for src in allowed_sources:
        setup = assembler.assemble(
            "pregunta de prueba",
            src,
            system_prompt="Eres Kira.",
            use_system_role=True,
            is_local=True,
            provider_cfg={},
            request_model="gemma4:e4b",
            history_snapshot=[],
        )
        system_content = setup.messages[0]["content"]
        assert system_content.count(OWNER_RESPONSE_POLICY) == 1, f"Policy must appear exactly once for source '{src}'"


def test_excluded_sources_do_not_contain_owner_policy_and_retain_output(monkeypatch):
    import opencohost.core.context.prompt_assembler as pa_module

    assembler = PromptContextAssembler()
    excluded_sources = ["chat", "agenda", "kira-agenda", "stream", "custom_other"]

    sample_history = [
        {"role": "user", "content": "hola previo"},
        {"role": "assistant", "content": "respuesta previa", "source": "kira-agenda"},
        {"role": "assistant", "content": "respuesta normal", "source": "chat"},
    ]

    for src in excluded_sources:
        for use_sys in (True, False):
            # 1. Candidate assembly with owner policy enabled
            candidate_setup = assembler.assemble(
                "mensaje ordinario",
                src,
                system_prompt="Eres Kira.",
                use_system_role=use_sys,
                is_local=True,
                provider_cfg={},
                request_model="llama3",
                history_snapshot=sample_history,
            )

            # 2. Baseline assembly where OWNER_POLICY_SOURCES is empty (exact baseline behavior)
            with monkeypatch.context() as m:
                m.setattr(pa_module, "OWNER_POLICY_SOURCES", frozenset())
                baseline_setup = assembler.assemble(
                    "mensaje ordinario",
                    src,
                    system_prompt="Eres Kira.",
                    use_system_role=use_sys,
                    is_local=True,
                    provider_cfg={},
                    request_model="llama3",
                    history_snapshot=sample_history,
                )

            # Full equality of messages (all roles and exact content byte-for-byte)
            assert candidate_setup.messages == baseline_setup.messages, (
                f"Candidate messages diverged from baseline for source '{src}' (use_system_role={use_sys})"
            )

            # Full equality of options dict (including chat repetition/presence/frequency penalties)
            assert candidate_setup.opciones_llm == baseline_setup.opciones_llm, (
                f"Candidate options diverged from baseline for source '{src}'"
            )

            # Full equality of generation setup metadata
            assert candidate_setup.native_ctx == baseline_setup.native_ctx
            assert candidate_setup.effective_ctx == baseline_setup.effective_ctx
            assert candidate_setup.ctx_evicted == baseline_setup.ctx_evicted
            assert candidate_setup.editorial_block == baseline_setup.editorial_block
            assert candidate_setup.think == baseline_setup.think
            assert candidate_setup.preset == baseline_setup.preset
            assert candidate_setup.requested_budget == baseline_setup.requested_budget

            # Explicit check that policy is absent across all messages
            all_content = " ".join(msg["content"] for msg in candidate_setup.messages)
            assert OWNER_RESPONSE_POLICY not in all_content, (
                f"Policy must not appear in any message for excluded source '{src}'"
            )


def test_owner_policy_injection_boundaries_ordering(monkeypatch):
    from opencohost.core.profiles import personalization
    from opencohost.i18n import active as i18n_active

    monkeypatch.setattr(
        personalization,
        "build_injection_block",
        lambda sanitize_fn: "--- PERSONALIZATION BLOCK ---",
    )

    assembler = PromptContextAssembler()
    setup = assembler.assemble(
        "consulta del streamer",
        "direct",
        system_prompt="PERSONA BASE: Eres Kira.",
        use_system_role=True,
        is_local=True,
        provider_cfg={},
        request_model="llama3",
        history_snapshot=[],
        personalization_enabled=True,
    )

    system_content = setup.messages[0]["content"]
    assert "PERSONA BASE: Eres Kira." in system_content
    assert OWNER_RESPONSE_POLICY in system_content
    assert "--- PERSONALIZATION BLOCK ---" in system_content

    grounding_rules = i18n_active.grounding_rules()
    idx_persona = system_content.index("PERSONA BASE: Eres Kira.")
    idx_policy = system_content.index(OWNER_RESPONSE_POLICY)
    idx_personalization = system_content.index("--- PERSONALIZATION BLOCK ---")

    if grounding_rules:
        assert grounding_rules in system_content
        idx_grounding = system_content.index(grounding_rules)
        assert idx_persona < idx_grounding < idx_policy < idx_personalization
    else:
        assert idx_persona < idx_policy < idx_personalization


def test_both_role_framing_paths_receive_owner_policy():
    assembler = PromptContextAssembler()

    # 1. System role path
    setup_sys = assembler.assemble(
        "consulta directa",
        "direct",
        system_prompt="Eres Kira.",
        use_system_role=True,
        is_local=True,
        provider_cfg={},
        request_model="llama3",
        history_snapshot=[],
    )
    assert setup_sys.messages[0]["role"] == "system"
    assert OWNER_RESPONSE_POLICY in setup_sys.messages[0]["content"]
    assert setup_sys.messages[1]["role"] == "user"
    assert setup_sys.messages[1]["content"] == "consulta directa"

    # 2. Folded user message path (use_system_role=False)
    setup_user = assembler.assemble(
        "consulta directa",
        "direct",
        system_prompt="Eres Kira.",
        use_system_role=False,
        is_local=True,
        provider_cfg={},
        request_model="llama3",
        history_snapshot=[],
    )
    assert len(setup_user.messages) == 1
    assert setup_user.messages[0]["role"] == "user"
    user_content = setup_user.messages[0]["content"]
    assert OWNER_RESPONSE_POLICY in user_content
    assert "Eres Kira." in user_content
    assert "consulta directa" in user_content


def test_different_request_wording_passes_through_unchanged_and_policy_source_scoped():
    assembler = PromptContextAssembler()
    queries = [
        "hola",
        "explayate",
        "extiende tu respuesta",
        "¿cuál es la arquitectura?",
        "123456",
        "",
    ]

    for q in queries:
        # Direct source: always contains owner policy, query unchanged
        setup_direct = assembler.assemble(
            q,
            "direct",
            system_prompt="Eres Kira.",
            use_system_role=True,
            is_local=True,
            provider_cfg={},
            request_model="llama3",
            history_snapshot=[],
        )
        assert OWNER_RESPONSE_POLICY in setup_direct.messages[0]["content"]
        assert setup_direct.messages[1]["content"] == q

        # Chat source: never contains owner policy, query unchanged
        setup_chat = assembler.assemble(
            q,
            "chat",
            system_prompt="Eres Kira.",
            use_system_role=True,
            is_local=True,
            provider_cfg={},
            request_model="llama3",
            history_snapshot=[],
        )
        assert OWNER_RESPONSE_POLICY not in setup_chat.messages[0]["content"]
        assert setup_chat.messages[-1]["content"] == q


def test_no_new_sampling_or_budget_override_introduced():
    assembler = PromptContextAssembler()
    setup = assembler.assemble(
        "consulta estándar",
        "direct",
        system_prompt="Eres Kira.",
        use_system_role=True,
        is_local=True,
        provider_cfg={},
        request_model="llama3",
        history_snapshot=[],
    )

    # Verify standard sampling keys remain unaltered
    assert setup.opciones_llm["temperature"] == settings.LLM_TEMPERATURE
    assert setup.opciones_llm["top_p"] == settings.LLM_TOP_P
    assert setup.opciones_llm["num_predict"] == 768
    assert "repeat_penalty" not in setup.opciones_llm
    assert setup.preset == "balanced"
