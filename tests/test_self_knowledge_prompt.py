"""self-knowledge K2: prompt assembler seam. OFF/absent must be byte-identical."""
from opencohost.core.context.prompt_assembler import PromptContextAssembler

import pytest
# B-W4: this module pins the legacy prompt layout (system-message placement, one-pair
# history deque, legacy goldens). The v2 placement is tested in tests/test_prompt_layout_v2.py.
pytestmark = pytest.mark.usefixtures("legacy_prompt_layout")


def _system(source="direct", **kw):
    setup = PromptContextAssembler().assemble(
        "qué hora es",
        source,
        system_prompt="Eres Kira.",
        use_system_role=True,
        is_local=True,
        provider_cfg={},
        request_model="llama3",
        history_snapshot=[],
        **kw,
    )
    return setup.messages[0]["content"], setup


def test_absent_or_empty_provider_is_byte_identical_to_baseline():
    base, base_setup = _system()
    empty, empty_setup = _system(self_knowledge=lambda text, source, model: "")
    assert empty == base
    assert empty_setup.messages == base_setup.messages


def test_provider_block_is_appended_once_and_receives_turn_inputs():
    seen = []

    def provider(text, source, model):
        seen.append((text, source, model))
        return "SELF-BLOCK"

    content, _ = _system(self_knowledge=provider)
    assert content.count("SELF-BLOCK") == 1
    assert content.endswith("SELF-BLOCK")
    assert seen == [("qué hora es", "direct", "llama3")]


def test_provider_exception_never_breaks_the_prompt():
    def boom(text, source, model):
        raise RuntimeError("x")

    base, _ = _system()
    content, _ = _system(self_knowledge=boom)
    assert content == base


def test_engine_host_arms_the_service_on_the_motor():
    from types import SimpleNamespace

    from opencohost.api.engine_host import EngineHost

    motor = SimpleNamespace(_vision_enabled=lambda: True, motor_tts="piper")
    host = EngineHost.__new__(EngineHost)
    host.motor = motor
    host._wire_self_knowledge()
    assert callable(motor.self_knowledge_service)
    block = motor.self_knowledge_service("what can you do", "direct", "m")
    assert "Vision is enabled" in block or "visión está activada" in block
