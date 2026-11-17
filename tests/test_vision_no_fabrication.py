"""V1.30: no screen block + a screen reference means Kira must not invent content."""
from __future__ import annotations

import pytest

from opencohost.core.context import prompt_assembler as pa
from opencohost.core.context.prompt_assembler import PromptContextAssembler
from opencohost.core.vision.gate import references_screen_content
from opencohost.core.vision.sanitize import build_screen_block

# B-W4: this module pins the legacy prompt layout (system-message placement, one-pair
# history deque, legacy goldens). The v2 placement is tested in tests/test_prompt_layout_v2.py.
pytestmark = pytest.mark.usefixtures("legacy_prompt_layout")

REFERENCES = [
    "¿Qué opinas del texto presentado en las imágenes?",
    "que dice la imagen",
    "mira la foto que te mandé",
    "qué te parece esta captura",
    "what do you think of the screenshot",
    "describe the picture",
    "what's on the screen",
    "qué pasa en el video",
    "explícame lo que hay en la pantalla",
    "check the clip",
    "what do you see",
    "dime lo que ves",
]
NO_REFERENCE = [
    "hola kira",
    "cuéntame un chiste",
    "what is your favorite game",
    "recomiéndame un videojuego",
    "how are you today",
    "",
]


@pytest.mark.parametrize("text", REFERENCES)
def test_references_are_detected(text):
    assert references_screen_content(text) is True


@pytest.mark.parametrize("text", NO_REFERENCE)
def test_plain_chat_is_not_a_reference(text):
    assert references_screen_content(text) is False


def test_non_string_never_raises():
    assert references_screen_content(None) is False
    assert references_screen_content(42) is False


def _assemble(text, source="direct", screen_block="", vision_note="", vision_enabled=False):
    return PromptContextAssembler().assemble(
        text,
        source,
        system_prompt="Eres Kira.",
        use_system_role=True,
        is_local=True,
        provider_cfg={},
        request_model="llama3",
        history_snapshot=[],
        personalization_enabled=False,
        memorias_enabled=False,
        screen_block=screen_block,
        vision_note=vision_note,
        vision_enabled=vision_enabled,
    )


@pytest.mark.parametrize("source", ["direct", "ptt"])
def test_reference_without_block_injects_the_no_look_instruction(source):
    # Vision on: with vision off the turn gets VISION_OFF_INSTRUCTION instead
    # (vision-off-quick-enable-20261009, tests/test_vision_off_request.py).
    system = _assemble("¿qué dice la imagen?", source=source, vision_enabled=True).messages[0][
        "content"
    ]

    assert pa.NO_LOOK_INSTRUCTION in system
    low = pa.NO_LOOK_INSTRUCTION.lower()
    assert "did not look" in low and "/mira" in low and "never describe" in low


def test_reference_with_block_does_not_inject_it():
    block = build_screen_block("Una tabla.")
    system = _assemble("¿qué dice la imagen?", screen_block=block).messages[0]["content"]

    assert pa.NO_LOOK_INSTRUCTION not in system


def test_failed_look_note_replaces_the_no_look_instruction():
    note = "VISION NOTE: You tried to look but could not (reason: describe_timeout)."
    system = _assemble("¿qué dice la imagen?", vision_note=note).messages[0]["content"]

    assert note in system
    assert pa.NO_LOOK_INSTRUCTION not in system


def test_plain_chat_prompt_is_unchanged():
    system = _assemble("cuéntame un chiste").messages[0]["content"]

    assert pa.NO_LOOK_INSTRUCTION not in system


@pytest.mark.parametrize("source", ["chat", "kira-agenda", "livevoice"])
def test_non_owner_sources_never_get_it(source):
    system = _assemble("¿qué dice la imagen?", source=source).messages[0]["content"]

    assert pa.NO_LOOK_INSTRUCTION not in system
