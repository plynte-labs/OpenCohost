"""S1 (guardrails-vulnerable-users): pure crisis detection, es + en."""
from __future__ import annotations

import pytest

from opencohost.core.safety.crisis import detect_crisis

POSITIVES = [
    # es, direct
    "quiero morir",
    "Ya no quiero seguir viviendo",
    "no quiero seguir viviendo",
    "pienso en quitarme la vida",
    "me quiero matar",
    "me voy a matar esta noche",
    "quiero suicidarme",
    "ya no quiero vivir más",
    "estoy cansado de vivir",
    "no aguanto más vivir así",
    "no le veo sentido a la vida",
    "tengo ideas suicidas",
    "quiero hacerme daño",
    # es, indirect
    "todos estarían mejor sin mí",
    "el mundo estaría mejor sin mí",
    "ojalá no despertar mañana",
    "ojalá me muriera",
    "desearía estar muerta",
    "quiero dejar de existir",
    "ya no tengo razones para vivir",
    # es, spaced/misspelled/slang/obfuscated
    "kiero morir",
    "me kiero matar",
    "QUIERO MORIRMEEEE",
    "quiero\u200b morirme",
    "quiero  morir",
    "ｑｕｉｅｒｏ ｍｏｒｉｒ",
    # en, direct
    "I want to die",
    "i wanna die",
    "I'm going to kill myself",
    "i want to kill myself",
    "I want to end my life",
    "I'm thinking about suicide",
    "I am suicidal",
    "I want to hurt myself",
    "I don't want to be alive anymore",
    "I don't want to live anymore",
    "I have nothing to live for",
    "I wish I was dead",
    # en, indirect
    "everyone would be better off without me",
    "everyone would be better off if I was gone",
    "I can't go on living like this",
    "i want to unalive myself",
    "I want to end it all",
]

NEGATIVES = [
    "me muero de risa",
    "me muero de hambre",
    "me quiero morir de la vergüenza",
    "me muero de sueño",
    "no quiero morir",
    "no quiero morirme nunca",
    "this boss is killing me",
    "I'd kill for a pizza",
    "this song is so good I could die",
    "I'm dying laughing",
    "I want to die laughing",
    "matar al jefe final es imposible",
    "vamos a matar al dragón",
    "I don't want to die",
    "quiero vivir mil años",
    "I want to live",
    "this meeting is killing me softly",
    "suicide squad es una buena película",
    "mi dieta es una tortura pero sigo viva",
    # abuse aimed at someone else is a separate concern, not a crisis signal
    "mátate",
    "kill yourself",
    "ojalá te mueras",
    "go kill yourself",
    "hola kira, qué tal el stream",
    "",
]


@pytest.mark.parametrize("text", POSITIVES)
def test_crisis_positives(text):
    assert detect_crisis(text) is True


@pytest.mark.parametrize("text", NEGATIVES)
def test_crisis_negatives(text):
    assert detect_crisis(text) is False


def test_non_string_input_is_not_a_crisis():
    assert detect_crisis(None) is False  # type: ignore[arg-type]
    assert detect_crisis(123) is False  # type: ignore[arg-type]
