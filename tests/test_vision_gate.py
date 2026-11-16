"""Behavior tests for the vision gates (vision-on-demand V1.1).

The utterance lists are the frozen V0.4 spike sets (dev + heldout), copied
inline so the tests never read scratch files. Negatives are hard negatives:
precision on them must stay 1.0 because a false trigger costs ~5 s.
"""
from __future__ import annotations

import dataclasses
import threading

import pytest

from opencohost.core.vision.gate import (
    CompositeGate,
    ExplicitTriggerGate,
    RuleVisionGate,
    VisionDecision,
    VisionGate,
)

POSITIVES = [
    # dev (es)
    "Kira, mira esto", "¿qué onda con este jefe?", "chécate esto, está bien raro",
    "¿qué hago con este jefe?", "¿cuánta vida me queda?", "¿qué dice ahí?",
    "léeme lo que dice ese letrero", "¿ves lo que está pasando en pantalla?",
    "fíjate en la esquina de arriba", "¿cuánto tiempo le queda al timer?",
    "¿dónde estoy en el mapa?", "¿qué es eso que está brillando?",
    "oye, ¿qué opinas de este outfit?", "¿cuál de estas dos armas agarro?",
    "ayúdame a leer esta misión", "¿qué tal se ve mi build?",
    "no manches, mira nada más qué paisaje", "¿qué significa el ícono rojo de la esquina?",
    "¿cuántas balas tengo?", "¿qué nivel tiene ese enemigo?", "dime qué ves",
    "¿por dónde me voy, izquierda o derecha?",
    # dev (en)
    "Kira, look at this", "how much HP do I have", "read that for me",
    "what's this boss weak to?", "can you see my screen right now?",
    "check out this view", "what does that sign say?",
    "how much time is left on the timer?", "which one should I pick, the left or the right?",
    "what is that thing in the corner?", "where am I on the map?",
    "how does my character look?",
    # heldout (es)
    "échale un ojo a esto", "¿alcanzas a ver el número de allá arriba?",
    "¿qué pone en el cartel?", "¿me alcanza el dinero para comprar esa espada?",
    "¿cuántas pociones me quedan?", "¿le pego a este o al otro?",
    "Kira, ¿qué te parece esta armadura?", "voltea a ver la pantalla",
    "¿qué onda con ese bicho raro?", "¿de qué color es la barra del jefe?",
    # heldout (en)
    "glance at this real quick", "what level am I right now?",
    "can you tell me what this menu says?", "is my health low?",
    "which door should I take?", "yo, peep this",
    # extra widening (es/en), not from the frozen sets
    "¿cuál puerta me conviene?", "is my level high enough?",
]

NEGATIVES = [
    # dev (es)
    "mira, yo creo que este juego está sobrevalorado",
    "esto me recuerda a cuando jugaba en la secundaria", "¿te acuerdas del jefe de ayer?",
    "¿qué dice el chat?", "¿cuántos viewers tenemos?", "¿cuál es la capital de Australia?",
    "oye, ¿qué opinas de la nueva consola?", "mira, la neta no tengo ganas de seguir",
    "no veo la hora de que salga el DLC", "ya veo, tiene sentido", "cuéntame un chiste",
    "¿qué hora es?", "salúdame a la gente que acaba de llegar", "este juego me encanta",
    "a ver, ¿cómo te fue hoy?", "¿qué piensas de los juegos de terror?",
    "no mires eso, es spoiler", "¿cuánta vida tiene un dragón en Skyrim normalmente?",
    "ese jefe de la semana pasada estuvo durísimo", "¿qué dijiste hace rato?",
    "pon una canción tranqui", "vamos a tomar un descanso de cinco minutos",
    "¿quién ganó el mundial pasado?", "te ves muy seria hoy, Kira",
    "¿qué dice la gente del nuevo parche?", "chale, ya me mataron otra vez",
    # dev (en)
    "look, I think we should try another strategy", "this reminds me of Dark Souls",
    "what is chat saying?", "remember that boss from last stream?",
    "what's the tallest mountain in the world?", "I see what you mean",
    "how many followers did we get today?", "tell me something funny",
    "don't look at that, it's a spoiler", "this game is so good",
    "look, honestly I'm tired", "how much HP does Malenia have in general?",
    "let's see how long we can last", "what did you say earlier?",
    "say hi to the new subs", "what do you think about horror games?",
    "you look grumpy today, Kira", "what does the wiki say about this quest?",
    "ugh, I died again", "should I stream longer tonight?",
    # heldout (es)
    "mira, la verdad prefiero los juegos de estrategia",
    "eso me trae recuerdos de la infancia", "¿alguien en el chat ya pasó este juego?",
    "¿cuántas horas llevamos en directo?", "¿cuántos huesos tiene el cuerpo humano?",
    "ayer el jefe final me humilló bien gacho", "a ver si hoy sí lo logramos",
    "ni lo mires, todavía no llegamos ahí", "¿qué opinas del precio de las tarjetas gráficas?",
    "ya nos vemos mañana, banda", "¿qué dice tu horóscopo hoy?",
    "neta, este stream está bien chido",
    # heldout (en)
    "listen, I don't think this is working out", "look, we gotta be faster next time",
    "that brings back memories of Halo", "did anyone in chat donate?",
    "who wrote Pride and Prejudice?", "see you all tomorrow",
    "how many hit points does a creeper have?", "last week's boss was brutal",
    "this song slaps", "you're looking cheerful today", "let's take a quick break",
    "what's the stream title right now?",
]

NON_OWNER_SOURCES = ["chat", "viewer", "agenda", "twitch", "youtube", "system", ""]


# --------------------------------------------------------------------------
# VisionDecision / Protocol
# --------------------------------------------------------------------------

def test_decision_is_frozen():
    d = VisionDecision(needs_vision=False, reason_codes=("no_trigger",), capture_delay_s=0.0)
    with pytest.raises(dataclasses.FrozenInstanceError):
        d.needs_vision = True  # type: ignore[misc]


def test_gates_satisfy_protocol():
    explicit = ExplicitTriggerGate(look_on_ptt=False, capture_delay_s=1.0)
    rule = RuleVisionGate()
    for gate in (explicit, rule, CompositeGate(explicit, rule)):
        assert isinstance(gate, VisionGate)


# --------------------------------------------------------------------------
# ExplicitTriggerGate
# --------------------------------------------------------------------------

def _explicit(look_on_ptt: bool = False, delay: float = 1.5) -> ExplicitTriggerGate:
    return ExplicitTriggerGate(look_on_ptt=look_on_ptt, capture_delay_s=delay)


def test_explicit_no_trigger_by_default():
    d = _explicit().decide("hola", "direct")
    assert d.needs_vision is False
    assert d.reason_codes == ("no_trigger",)


@pytest.mark.parametrize("source", NON_OWNER_SOURCES)
def test_explicit_non_owner_sources_never_trigger(source):
    gate = _explicit(look_on_ptt=True)
    gate.arm_look()
    d = gate.decide("mira esto", source)
    assert d.needs_vision is False
    assert d.reason_codes == ("source_not_owner",)


@pytest.mark.parametrize("source", NON_OWNER_SOURCES)
def test_explicit_non_owner_turn_does_not_consume_armed_flag(source):
    gate = _explicit()
    gate.arm_look()
    gate.decide("hola", source)
    assert gate.is_armed() is True
    assert gate.decide("hola", "direct").needs_vision is True


def test_explicit_armed_direct_uses_configured_delay():
    gate = _explicit(delay=2.25)
    gate.arm_look()
    d = gate.decide("hola", "direct")
    assert d == VisionDecision(True, ("explicit_look",), 2.25)


def test_explicit_armed_ptt_has_zero_delay():
    gate = _explicit(delay=2.25)
    gate.arm_look()
    d = gate.decide("hola", "ptt")
    assert d == VisionDecision(True, ("explicit_look",), 0.0)


def test_explicit_look_flag_is_one_shot():
    gate = _explicit()
    gate.arm_look()
    assert gate.is_armed() is True
    assert gate.decide("a", "direct").needs_vision is True
    assert gate.is_armed() is False
    d = gate.decide("b", "direct")
    assert d.needs_vision is False and d.reason_codes == ("no_trigger",)


def test_explicit_ptt_switch_triggers_on_ptt_with_zero_delay():
    gate = _explicit(look_on_ptt=True, delay=3.0)
    d = gate.decide("hola", "ptt")
    assert d == VisionDecision(True, ("ptt_switch",), 0.0)


def test_explicit_ptt_switch_ignores_direct_source():
    d = _explicit(look_on_ptt=True).decide("hola", "direct")
    assert d.needs_vision is False and d.reason_codes == ("no_trigger",)


def test_explicit_armed_flag_wins_over_ptt_switch_and_is_consumed():
    gate = _explicit(look_on_ptt=True)
    gate.arm_look()
    assert gate.decide("x", "ptt").reason_codes == ("explicit_look",)
    assert gate.decide("x", "ptt").reason_codes == ("ptt_switch",)


def test_explicit_arm_is_idempotent():
    gate = _explicit()
    gate.arm_look()
    gate.arm_look()
    assert gate.decide("x", "direct").needs_vision is True
    assert gate.decide("x", "direct").needs_vision is False


def test_explicit_exactly_one_consumer_wins_under_threads():
    gate = _explicit()
    for _ in range(50):
        gate.arm_look()
        barrier = threading.Barrier(8)
        results: list[bool] = []
        lock = threading.Lock()

        def consume():
            barrier.wait()
            r = gate.decide("x", "direct").needs_vision
            with lock:
                results.append(r)

        threads = [threading.Thread(target=consume) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert results.count(True) == 1
        assert gate.is_armed() is False


def test_explicit_concurrent_arm_and_decide_do_not_raise():
    gate = _explicit()
    stop = threading.Event()
    errors: list[BaseException] = []

    def armer():
        try:
            while not stop.is_set():
                gate.arm_look()
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)

    t = threading.Thread(target=armer)
    t.start()
    try:
        for _ in range(2000):
            gate.decide("x", "direct")
    finally:
        stop.set()
        t.join()
    assert errors == []


# --------------------------------------------------------------------------
# RuleVisionGate
# --------------------------------------------------------------------------

@pytest.mark.parametrize("source", ["direct", "ptt"])
@pytest.mark.parametrize("text", NEGATIVES)
def test_rule_hard_negatives_never_trigger(text, source):
    assert RuleVisionGate().decide(text, source).needs_vision is False


@pytest.mark.parametrize("text", POSITIVES)
def test_rule_positives_trigger_for_owner(text):
    d = RuleVisionGate().decide(text, "direct")
    assert d.needs_vision is True
    assert d.reason_codes


@pytest.mark.parametrize("source", NON_OWNER_SOURCES)
@pytest.mark.parametrize("text", ["Kira, mira esto", "look at this", "¿qué dice ahí?"])
def test_rule_viewer_sources_never_trigger(text, source):
    d = RuleVisionGate().decide(text, source)
    assert d.needs_vision is False
    assert d.reason_codes == ("source_not_owner",)


def test_rule_precision_is_one_and_recall_is_full_on_spike_sets():
    gate = RuleVisionGate()
    tp = sum(gate.decide(t, "direct").needs_vision for t in POSITIVES)
    fp = sum(gate.decide(t, "direct").needs_vision for t in NEGATIVES)
    assert fp == 0
    assert tp / (tp + fp) == 1.0
    assert tp == len(POSITIVES)


def test_rule_positive_reason_codes_name_the_rule():
    d = RuleVisionGate().decide("Kira, mira esto", "direct")
    assert "look_verb" in d.reason_codes


def test_rule_suppressor_wins_and_is_reported():
    d = RuleVisionGate().decide("look at this stream", "direct")
    assert d.needs_vision is False
    assert "look_verb" in d.reason_codes and "suppressed_meta" in d.reason_codes


def test_rule_no_match_reports_no_trigger():
    d = RuleVisionGate().decide("cuéntame un chiste", "direct")
    assert d.needs_vision is False
    assert d.reason_codes == ("no_trigger",)


def test_rule_capture_delay_is_configurable_and_zero_for_ptt():
    gate = RuleVisionGate(capture_delay_s=1.75)
    assert gate.decide("mira esto", "direct").capture_delay_s == 1.75
    assert gate.decide("mira esto", "ptt").capture_delay_s == 0.0


def test_rule_does_not_log_utterance_text(caplog):
    with caplog.at_level("DEBUG"):
        RuleVisionGate().decide("mira esto secretword", "direct")
    assert "secretword" not in caplog.text


# --------------------------------------------------------------------------
# CompositeGate
# --------------------------------------------------------------------------

def test_composite_primary_yes_skips_secondary():
    class Boom:
        def decide(self, text, source):  # pragma: no cover
            raise AssertionError("secondary must not be asked")

    primary = _explicit()
    primary.arm_look()
    d = CompositeGate(primary, Boom()).decide("x", "direct")
    assert d.needs_vision is True and d.reason_codes == ("explicit_look",)


def test_composite_primary_no_asks_secondary_and_merges_reasons():
    d = CompositeGate(_explicit(), RuleVisionGate(capture_delay_s=2.0)).decide(
        "Kira, mira esto", "direct"
    )
    assert d.needs_vision is True
    assert d.reason_codes[0] == "no_trigger"
    assert "look_verb" in d.reason_codes
    assert d.capture_delay_s == 2.0


def test_composite_both_no_merges_reasons_without_duplicates():
    d = CompositeGate(_explicit(), RuleVisionGate()).decide("cuéntame un chiste", "direct")
    assert d.needs_vision is False
    assert d.reason_codes == ("no_trigger",)


def test_composite_without_secondary_returns_primary():
    d = CompositeGate(_explicit()).decide("x", "direct")
    assert d.needs_vision is False and d.reason_codes == ("no_trigger",)


@pytest.mark.parametrize("source", NON_OWNER_SOURCES)
def test_composite_viewer_never_triggers(source):
    primary = _explicit(look_on_ptt=True)
    primary.arm_look()
    gate = CompositeGate(primary, RuleVisionGate())
    assert gate.decide("mira esto", source).needs_vision is False


# ----------------------------------------- V1.15: requested vs automatic look

from opencohost.core.vision.gate import is_requested_look  # noqa: E402


@pytest.mark.parametrize(
    "reasons,text",
    [
        (("explicit_look",), "hola"),
        (("look_verb", "screen_ref"), "mira mi pantalla"),
        (("screen_ref",), "what's on my screen"),
        # The composite gate never asks the rule gate when the PTT switch fired,
        # so a screen reference in the PTT utterance still counts as requested.
        (("ptt_switch",), "¿qué hay en mi pantalla?"),
        (("ptt_switch",), "look at the screen"),
    ],
)
def test_requested_look(reasons, text):
    assert is_requested_look(reasons, text) is True


@pytest.mark.parametrize(
    "reasons,text",
    [
        (("ptt_switch",), "hola, ¿cómo vas?"),
        (("ptt_switch",), "no mires la pantalla"),  # suppressed by the rule gate
        (("no_trigger",), "pantalla"),
        ((), "pantalla"),
    ],
)
def test_not_a_requested_look(reasons, text):
    assert is_requested_look(reasons, text) is False


def test_requested_look_never_raises_on_odd_input():
    assert is_requested_look(("ptt_switch",), None) is False


# ------------------------- V1.23: a look verb is a request; V1.25: self-look wording

from opencohost.core.vision.gate import wants_self_look  # noqa: E402


def test_look_verb_turn_counts_as_requested_look():
    assert is_requested_look(("look_verb",), "mira esto") is True
    assert is_requested_look(("look_verb", "model_warming"), "look at this") is True


@pytest.mark.parametrize(
    "text",
    [
        "mira OpenCohost",
        "can you look at opencohost?",
        "mira la app",
        "Mira la aplicación",
        "mira la APLICACION un momento",
        "mira la interfaz",
        "look at the app",
        "what do you think of the interface",
        "mira Open Cohost",
    ],
)
def test_wants_self_look_matches_the_app_wording(text):
    assert wants_self_look(text) is True


@pytest.mark.parametrize(
    "text",
    ["", None, "mira esto", "mira la pantalla", "what is on my screen", "mira mi mapa",
     "una aplicación cualquiera", "the application server", "look at this"],
)
def test_wants_self_look_ignores_everything_else(text):
    assert wants_self_look(text) is False


# --- K9: data/info questions are self-knowledge, not a look request ---------

DATA_QUESTIONS = [
    "qué datos puedes ver",
    "¿Qué datos puedes ver?",
    "que informacion puedes ver",
    "qué información tienes",
    "qué info puedes ver",
    "what data can you see",
    "what info do you have",
    "What information can you see?",
]

REAL_LOOKS = [
    "qué puedes ver en mi pantalla",
    "puedes ver esto",
    "mira mi pantalla",
    "qué información puedes ver en mi pantalla",
    "what data can you see on my screen",
    "can you see this",
]


@pytest.mark.parametrize("source", ["direct", "ptt"])
@pytest.mark.parametrize("text", DATA_QUESTIONS)
def test_data_questions_do_not_trigger_the_look_path(text, source):
    assert RuleVisionGate().decide(text, source).needs_vision is False


@pytest.mark.parametrize("text", REAL_LOOKS)
def test_real_look_requests_still_trigger(text):
    assert RuleVisionGate().decide(text, "direct").needs_vision is True


# --- live-session-fixes L5: "del video" and bare video(s) -------------------

@pytest.mark.parametrize("text", [
    "que opinas del video.", "¿qué opinas del video?", "mira el vídeo", "what do you think of the video",
    "te gustó el video", "qué te pareció un video así",
])
def test_bare_video_references_trigger_a_look(text):
    assert RuleVisionGate().decide(text, "direct").needs_vision is True


def test_bare_video_triggers_the_same_clip_mode_as_a_bare_clip():
    from opencohost.core.vision.gate import MODE_CLIP, look_mode

    assert look_mode("que opinas del video.") == MODE_CLIP
    assert look_mode("que opinas del clip.") == MODE_CLIP
    assert RuleVisionGate().decide("que opinas del clip.", "direct").needs_vision is True


@pytest.mark.parametrize("text", ["vi un video ayer", "el video del stream pasado", "subí un video al chat"])
def test_video_in_past_or_meta_talk_stays_suppressed(text):
    assert RuleVisionGate().decide(text, "direct").needs_vision is False
