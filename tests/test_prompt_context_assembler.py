"""
tests/test_prompt_context_assembler.py

Unit tests for PromptContextAssembler component.
"""

from unittest.mock import MagicMock

import pytest

from opencohost.core.context.prompt_assembler import (
    DIGEST_CAPTURE_SOURCES,
    DIGEST_INJECT_SOURCES,
    EDITORIAL_INJECT_SOURCES,
    HISTORY_ASSISTANT_ONLY_SOURCES,
    MEMORIA_INJECT_SOURCES,
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


def test_reasoning_model_detection_drops_num_predict():
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

    assert "num_predict" not in setup.opciones_llm


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
