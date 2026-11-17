"""Tests for prompt assembler localization.

Verifies:
1. Drafting mode instruction localization (Spanish vs English) in both legacy and v2 layouts.
2. Vision-off prompt guidance localization ("Controls > Vision" / "Turn on vision" vs "Controles > Visión" / "Activar visión").
"""

from opencohost.core.context.prompt_assembler import (
    PromptContextAssembler,
    VISION_OFF_INSTRUCTION_EN,
    VISION_OFF_INSTRUCTION_ES,
    VISION_OFF_RULE_EN,
    VISION_OFF_RULE_ES,
)
from opencohost.i18n import active as i18n_active
from opencohost.i18n.startup import resolve_active_bundle

EXPECTED_DRAFTING_ES = (
    "MODO REDACCIÓN: Genera respuestas completas, estructuradas y detalladas. No resumas ni recortes la respuesta."
)
EXPECTED_DRAFTING_EN = (
    "DRAFTING MODE: Generate full, structured, comprehensive answers. Do not summarize or cut short."
)


def test_drafting_mode_spanish():
    """Verify Spanish drafting mode prompt injection in legacy and v2 layouts."""
    assembler = PromptContextAssembler()

    # Legacy layout places directive in system message
    setup_leg = assembler.assemble(
        "redacta una agenda",
        source="direct",
        system_prompt="Eres Kira.",
        use_system_role=True,
        is_local=True,
        provider_cfg={},
        request_model="llama3",
        history_snapshot=[],
        intent="drafting",
        layout="legacy",
        lang="es",
    )
    assert EXPECTED_DRAFTING_ES in setup_leg.messages[0]["content"]

    # V2 layout places directive in turn directive (final user message)
    setup_v2 = assembler.assemble(
        "redacta una agenda",
        source="direct",
        system_prompt="Eres Kira.",
        use_system_role=True,
        is_local=True,
        provider_cfg={},
        request_model="llama3",
        history_snapshot=[],
        intent="drafting",
        layout="v2",
        lang="es",
    )
    assert EXPECTED_DRAFTING_ES in setup_v2.messages[-1]["content"]


def test_drafting_mode_english():
    """Verify English drafting mode prompt injection in legacy and v2 layouts."""
    assembler = PromptContextAssembler()

    # Legacy layout
    setup_leg = assembler.assemble(
        "write an agenda",
        source="direct",
        system_prompt="You are Kira.",
        use_system_role=True,
        is_local=True,
        provider_cfg={},
        request_model="llama3",
        history_snapshot=[],
        intent="drafting",
        layout="legacy",
        lang="en",
    )
    assert EXPECTED_DRAFTING_EN in setup_leg.messages[0]["content"]

    # V2 layout
    setup_v2 = assembler.assemble(
        "write an agenda",
        source="direct",
        system_prompt="You are Kira.",
        use_system_role=True,
        is_local=True,
        provider_cfg={},
        request_model="llama3",
        history_snapshot=[],
        intent="drafting",
        layout="v2",
        lang="en",
    )
    assert EXPECTED_DRAFTING_EN in setup_v2.messages[-1]["content"]


def test_drafting_mode_active_bundle_english():
    """Verify active bundle setting drives English drafting mode prompt injection."""
    try:
        i18n_active.set_active_bundle(resolve_active_bundle(locale="en"))
        assembler = PromptContextAssembler()

        setup_leg = assembler.assemble(
            "write an agenda",
            source="direct",
            system_prompt="You are Kira.",
            use_system_role=True,
            is_local=True,
            provider_cfg={},
            request_model="llama3",
            history_snapshot=[],
            intent="drafting",
            layout="legacy",
        )
        assert EXPECTED_DRAFTING_EN in setup_leg.messages[0]["content"]

        setup_v2 = assembler.assemble(
            "write an agenda",
            source="direct",
            system_prompt="You are Kira.",
            use_system_role=True,
            is_local=True,
            provider_cfg={},
            request_model="llama3",
            history_snapshot=[],
            intent="drafting",
            layout="v2",
        )
        assert EXPECTED_DRAFTING_EN in setup_v2.messages[-1]["content"]
    finally:
        i18n_active.reset_active_bundle()


def test_vision_off_guidance_spanish():
    """Verify Spanish vision-off rules and instructions."""
    assert "Controles > Visión" in VISION_OFF_RULE_ES
    assert "Activar visión" in VISION_OFF_RULE_ES
    assert "Controles > Visión" in VISION_OFF_INSTRUCTION_ES
    assert "Activar visión" in VISION_OFF_INSTRUCTION_ES

    assembler = PromptContextAssembler()
    setup_v2 = assembler.assemble(
        "hola",
        source="direct",
        system_prompt="Eres Kira.",
        use_system_role=True,
        is_local=True,
        provider_cfg={},
        request_model="llama3",
        history_snapshot=[],
        vision_enabled=False,
        layout="v2",
        lang="es",
    )
    sys_v2 = setup_v2.messages[0]["content"]
    assert "Controles > Visión" in sys_v2
    assert "Activar visión" in sys_v2

    setup_leg = assembler.assemble(
        "mira mi pantalla",
        source="direct",
        system_prompt="Eres Kira.",
        use_system_role=True,
        is_local=True,
        provider_cfg={},
        request_model="llama3",
        history_snapshot=[],
        vision_enabled=False,
        layout="legacy",
        lang="es",
    )
    sys_leg = setup_leg.messages[0]["content"]
    assert "Controles > Visión" in sys_leg
    assert "Activar visión" in sys_leg


def test_vision_off_guidance_english():
    """Verify English vision-off rules and instructions."""
    assert "Controls > Vision" in VISION_OFF_RULE_EN
    assert "Turn on vision" in VISION_OFF_RULE_EN
    assert "Controls > Vision" in VISION_OFF_INSTRUCTION_EN
    assert "Turn on vision" in VISION_OFF_INSTRUCTION_EN

    assembler = PromptContextAssembler()
    setup_v2 = assembler.assemble(
        "hello",
        source="direct",
        system_prompt="You are Kira.",
        use_system_role=True,
        is_local=True,
        provider_cfg={},
        request_model="llama3",
        history_snapshot=[],
        vision_enabled=False,
        layout="v2",
        lang="en",
    )
    sys_v2 = setup_v2.messages[0]["content"]
    assert "Controls > Vision" in sys_v2
    assert "Turn on vision" in sys_v2

    setup_leg = assembler.assemble(
        "look at my screen",
        source="direct",
        system_prompt="You are Kira.",
        use_system_role=True,
        is_local=True,
        provider_cfg={},
        request_model="llama3",
        history_snapshot=[],
        vision_enabled=False,
        layout="legacy",
        lang="en",
    )
    sys_leg = setup_leg.messages[0]["content"]
    assert "Controls > Vision" in sys_leg
    assert "Turn on vision" in sys_leg


def test_web_flags_and_english_not_searched_note_have_no_spanish_drift():
    from opencohost.core.context import prompt_assembler as pa
    from opencohost.core.web import prompt as web_prompt

    assert pa.web_flags(web_prompt.WEB_NOT_SEARCHED_NOTE_EN, "") == (False, False)
    assert pa.web_flags(web_prompt.WEB_NOT_SEARCHED_NOTE_ES, "") == (False, False)
    assert pa.web_flags(web_prompt.WEB_NO_SUBJECT_NOTE, "") == (False, False)
    assert pa.web_flags(web_prompt.WEB_EMPTY_NOTE, "") == (True, True)

    assembler = PromptContextAssembler()
    setup_en = assembler.assemble(
        "search in web anthropic",
        source="direct",
        system_prompt="You are Kira.",
        use_system_role=True,
        is_local=False,
        provider_cfg={},
        request_model="gemini-3.5-flash-lite",
        history_snapshot=[],
        layout="v2",
        lang="en",
        web_note=web_prompt.WEB_NOT_SEARCHED_NOTE_ES,
    )
    user_turn = setup_en.messages[-1]["content"]
    assert "busca en internet" not in user_turn
    assert 'say "search the web' in user_turn

