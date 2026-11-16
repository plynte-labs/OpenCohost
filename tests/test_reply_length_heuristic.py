"""reply-length-selector-20261009 T2: the pure auto heuristic."""

import pytest

from opencohost.core.context.reply_length import resolve_reply_level


def _auto(text, vision=False):
    return resolve_reply_level("auto", text, vision_turn=vision, extended_responses=False)


def test_profile_extended_forces_very_long_over_everything():
    for mode in ("auto", "short", "medium", "long", "very_long"):
        assert resolve_reply_level(
            mode, "hola", vision_turn=True, extended_responses=True
        ) == ("very_long", "profile_extended")


@pytest.mark.parametrize("mode", ["short", "medium", "long", "very_long"])
def test_explicit_mode_returns_itself(mode):
    assert resolve_reply_level(
        mode, "explícame cómo funciona todo esto en detalle", vision_turn=True, extended_responses=False
    ) == (mode, "setting")


def test_unknown_mode_behaves_as_auto():
    assert resolve_reply_level("enormous", "hola", vision_turn=False, extended_responses=False) == (
        "short",
        "brief_input",
    )


def test_vision_turn_is_medium():
    assert _auto("explícame qué ves en detalle", vision=True) == ("medium", "vision")
    assert _auto("hola", vision=True) == ("medium", "vision")


@pytest.mark.parametrize(
    "text",
    [
        "hola",
        "Hola Kira",
        "jajaja",
        "JAJAJAJA",
        "jeje",
        "ok",
        "gracias",
        "buenas",
        "hey",
        "lol",
        "thanks",
        "¡Hola!",
        "",
        "   ",
        "qué tal todo",
        "muchas gracias kira jaja",
        "buenas noches hola kira",
        "ok thanks lol haha",
    ],
)
def test_brief_input_is_short(text):
    assert _auto(text) == ("short", "brief_input")


@pytest.mark.parametrize(
    "text",
    [
        "explícame la diferencia entre RAM y VRAM",
        "Explica eso",
        "explicame como se configura el micro",
        "¿por qué el cielo se ve azul cuando sale el sol?",
        "Por que el stream se corta tanto",
        "y cómo funciona el sistema de turnos del juego",
        "dame una lista de juegos de terror buenos",
        "cuáles son los pasos para instalar mods",
        "compara estos dos procesadores de gama media",
        "cuéntame del juego en detalle por favor",
        "háblame a fondo de la historia del juego",
        "detalla lo que pasó en la partida",
        "explain the difference between these two builds",
        "why does the frame rate drop so much",
        "how does the matchmaking system work here",
        "give me a list of good horror games",
        "what are the steps to install this",
        "compare these two keyboards for me",
        "tell me about the lore in detail please",
        "explícame",
        "por qué",
    ],
)
def test_depth_request_is_long(text):
    assert _auto(text) == ("long", "depth_request")


@pytest.mark.parametrize(
    "text",
    [
        "qué opinas del juego que estoy jugando ahora",
        "me gusta mucho este listado de canciones que armé",
        "listado de canciones para el stream de hoy",
        "the listener count went up a lot today",
        "porque me dio sueño anoche y no dormí",
        "comparado con ayer el stream está tranquilo",
    ],
)
def test_otherwise_medium(text):
    # Word-boundary matching: "listado" is not "lista", "listener" is not
    # "list", "porque" is not "por qué", "comparado" is not "compara".
    assert _auto(text) == ("medium", "default")


def test_auto_never_returns_very_long():
    samples = ["", "hola", "explícame todo a fondo en detalle con pasos y una lista", "x " * 400]
    for text in samples:
        for vision in (False, True):
            assert _auto(text, vision)[0] != "very_long"


def test_depth_request_wins_over_short_word_count():
    # "explícame" alone is 1 word but an explicit depth request.
    assert _auto("explícame eso")[0] == "long"


def test_none_text_is_safe():
    assert _auto(None) == ("short", "brief_input")
