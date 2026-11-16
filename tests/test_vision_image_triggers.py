"""V1.31: image/photo/screenshot references trigger a look; text questions route to text mode."""
from __future__ import annotations

import pytest

from opencohost.core.vision.gate import MODE_DEFAULT, MODE_TEXT, RuleVisionGate, look_mode

GATE = RuleVisionGate()

TRIGGERS = [
    "¿Qué opinas del texto presentado en las imágenes?",
    "qué dice la imagen",
    "qué te parece esta foto",
    "mira las fotos que puse",
    "qué ves en la captura",
    "explícame esta captura de pantalla",
    "what do you think of this picture",
    "what does the image say",
    "is this screenshot any good",
    "thoughts on these images",
]

NOT_TRIGGERS = [
    "mándame una foto tuya",
    "me gusta tomar fotos en vacaciones",
    "recuerdo la foto de ayer",
    "the chat sent a picture of a cat",
    "tomar una captura de cartas es aburrido en general",
    "hola kira cómo estás",
]


@pytest.mark.parametrize("text", TRIGGERS)
def test_image_references_trigger_a_look(text):
    assert GATE.decide(text, "direct").needs_vision is True


@pytest.mark.parametrize("text", NOT_TRIGGERS)
def test_incidental_mentions_do_not_trigger(text):
    assert GATE.decide(text, "direct").needs_vision is False


def test_viewer_chat_never_triggers():
    assert GATE.decide("qué dice la imagen", "chat").needs_vision is False


@pytest.mark.parametrize(
    "text",
    [
        "¿Qué opinas del texto presentado en las imágenes?",
        "qué dice la imagen",
        "what does the image say",
        "lee el texto de la captura",
    ],
)
def test_text_questions_about_images_route_to_text_mode(text):
    assert look_mode(text) == MODE_TEXT


@pytest.mark.parametrize("text", ["qué te parece esta foto", "mira esta captura"])
def test_plain_image_looks_stay_default_mode(text):
    assert look_mode(text) == MODE_DEFAULT
