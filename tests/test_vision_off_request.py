"""vision-off-quick-enable-20261009 T1: the owner asks to look while vision is off.

Pins:
- the prompt tells Kira her vision is off (and points to the "Activar visión"
  button) instead of suggesting /mira, which does nothing while off;
- vision-enabled prompts stay byte-identical (NO_LOOK + capability line);
- the engine fires one ``on_vision_off_request`` per owner request (pregen and
  main turn deduped), owner sources only, never raising, never logging text;
- the host records ``vision/vision_off_request`` with a closed detail.
"""
from __future__ import annotations

import logging

import pytest

from opencohost.core.context import prompt_assembler as pa
from opencohost.core.context.prompt_assembler import PromptContextAssembler
from opencohost.core.engine import llm_engine_vision
from opencohost.core.engine.llm_engine_vision import NO_LOOK, VisionTurnMixin
from tests.test_engine_host_vision import host_factory  # noqa: F401  (pytest fixture)

# B-W4: this module pins the legacy prompt layout (system-message placement, one-pair
# history deque, legacy goldens). The v2 placement is tested in tests/test_prompt_layout_v2.py.
pytestmark = pytest.mark.usefixtures("legacy_prompt_layout")

SCREEN_QUESTION = "Quiero que mires la pantalla"


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

def _system(text, source="direct", *, vision_enabled):
    setup = PromptContextAssembler().assemble(
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
        vision_enabled=vision_enabled,
    )
    return setup.messages[0]["content"]


@pytest.mark.parametrize("source", ["direct", "ptt"])
def test_vision_off_screen_question_says_vision_is_off(source):
    system = _system(SCREEN_QUESTION, source, vision_enabled=False)

    assert pa.VISION_OFF_INSTRUCTION in system
    assert pa.NO_LOOK_INSTRUCTION not in system
    assert "/mira" not in system
    assert "Activar visión" in system
    low = pa.VISION_OFF_INSTRUCTION.lower()
    assert "vision is turned off" in low and "never describe" in low


def test_vision_off_instruction_never_points_to_the_look_command():
    low = pa.VISION_OFF_INSTRUCTION.lower()
    assert "/mira" not in low
    assert "look button" not in low


def test_vision_enabled_screen_question_keeps_no_look_and_capability():
    system = _system(SCREEN_QUESTION, vision_enabled=True)

    assert pa.NO_LOOK_INSTRUCTION in system
    assert pa.VISION_CAPABILITY_LINE in system
    assert pa.VISION_OFF_INSTRUCTION not in system


def test_vision_enabled_prompt_is_unchanged_by_the_off_instruction():
    """Byte-identical: the enabled prompt is the base prompt plus the NO_LOOK
    and capability sections, in that order, exactly as before."""
    on = _system(SCREEN_QUESTION, vision_enabled=True)
    plain = _system("hola kira", vision_enabled=True)

    assert on.index(pa.NO_LOOK_INSTRUCTION) < on.index(pa.VISION_CAPABILITY_LINE)
    assert "VISION OFF" not in on and "VISION OFF" not in plain


def test_vision_off_plain_chat_gets_no_vision_section():
    system = _system("hola kira", vision_enabled=False)

    assert pa.VISION_OFF_INSTRUCTION not in system
    assert pa.NO_LOOK_INSTRUCTION not in system


def test_vision_off_viewer_chat_gets_no_vision_section():
    system = _system(SCREEN_QUESTION, "chat", vision_enabled=False)

    assert pa.VISION_OFF_INSTRUCTION not in system


def test_vision_off_section_is_traced_as_vision_off():
    setup = PromptContextAssembler().assemble(
        SCREEN_QUESTION,
        "direct",
        system_prompt="Eres Kira.",
        use_system_role=True,
        is_local=True,
        provider_cfg={},
        request_model="llama3",
        history_snapshot=[],
        personalization_enabled=False,
        memorias_enabled=False,
        vision_enabled=False,
    )
    names = setup.system_section_names
    assert "vision_off" in names and "no_look" not in names


@pytest.mark.parametrize("source", ["direct", "ptt"])
def test_vision_off_look_verb_without_screen_word_says_vision_is_off(source):
    """Prompt and event share one classifier: "mira esto" fires the event, so
    the prompt must also say vision is off (no drift)."""
    system = _system("mira esto", source, vision_enabled=False)

    assert pa.VISION_OFF_INSTRUCTION in system


def test_vision_off_ptt_wrapped_look_verb_says_vision_is_off():
    system = _system("El streamer acaba de decir (PTT): mira esto", "ptt", vision_enabled=False)

    assert pa.VISION_OFF_INSTRUCTION in system


def test_vision_enabled_look_verb_without_screen_word_is_unchanged():
    system = _system("mira esto", vision_enabled=True)

    assert pa.NO_LOOK_INSTRUCTION not in system
    assert pa.VISION_OFF_INSTRUCTION not in system


@pytest.mark.parametrize(
    "text,source,expected",
    [
        ("mira esto", "direct", True),
        ("El streamer acaba de decir (PTT): mira mi pantalla", "ptt", True),
        ("recuerdas la imagen de ayer", "direct", True),  # suppressed rule, screen ref
        ("hola", "direct", False),
        ("mira mi pantalla", "chat", False),
        (None, "direct", False),
    ],
)
def test_shared_classifier(text, source, expected):
    from opencohost.core.vision.gate import look_request_reasons

    assert bool(look_request_reasons(text, source)) is expected


# ---------------------------------------------------------------------------
# Engine hook
# ---------------------------------------------------------------------------

class _Vision:
    step = None
    enabled = False


class _Motor(VisionTurnMixin):
    def __init__(self, vision=_Vision()):
        self._vision = vision
        self.calls = 0
        self.on_vision_off_request = self._record

    def _record(self):
        self.calls += 1


@pytest.fixture(autouse=True)
def _clock(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(llm_engine_vision, "_monotonic", lambda: now[0])
    return now


@pytest.mark.parametrize("text", ["mira mi pantalla", SCREEN_QUESTION, "qué dice la imagen"])
def test_direct_look_request_with_vision_off_fires_once(text):
    motor = _Motor()

    assert motor._run_vision_look(text, "direct") is NO_LOOK
    assert motor.calls == 1


def test_ptt_request_with_history_wrapper_fires():
    motor = _Motor()

    look = motor._run_vision_look("El streamer acaba de decir (PTT): mira mi pantalla", "ptt")

    assert look is NO_LOOK
    assert motor.calls == 1


def test_look_verb_without_screen_word_fires():
    motor = _Motor()

    motor._run_vision_look("mira esto", "direct")

    assert motor.calls == 1


@pytest.mark.parametrize("text", ["hola", "cuéntame un chiste", ""])
def test_plain_chat_does_not_fire(text):
    motor = _Motor()

    motor._run_vision_look(text, "direct")

    assert motor.calls == 0


@pytest.mark.parametrize("source", ["chat", "agenda", "live"])
def test_non_owner_sources_never_fire(source):
    motor = _Motor()

    motor._run_vision_look("mira mi pantalla", source)

    assert motor.calls == 0


def test_pregen_and_main_turn_fire_once(_clock):
    motor = _Motor()
    text = "El streamer acaba de decir (PTT): mira mi pantalla"

    motor._run_vision_look(text, "ptt", stash=True)
    _clock[0] += 5.0
    motor._run_vision_look(text, "ptt")

    assert motor.calls == 1


def test_a_new_request_fires_again():
    motor = _Motor()

    motor._run_vision_look("mira mi pantalla", "direct")
    motor._run_vision_look("qué ves en la pantalla", "direct")

    assert motor.calls == 2


def test_same_request_after_the_ttl_fires_again(_clock):
    motor = _Motor()

    motor._run_vision_look("mira mi pantalla", "direct")
    _clock[0] += llm_engine_vision.VISION_PREGEN_LOOK_TTL_S + 1.0
    motor._run_vision_look("mira mi pantalla", "direct")

    assert motor.calls == 2


def test_raising_callback_never_raises():
    motor = _Motor()

    def boom():
        raise RuntimeError("host gone")

    motor.on_vision_off_request = boom

    assert motor._run_vision_look("mira mi pantalla", "direct") is NO_LOOK


def test_no_callback_is_a_no_op():
    motor = _Motor()
    motor.on_vision_off_request = None

    assert motor._run_vision_look("mira mi pantalla", "direct") is NO_LOOK


def test_vision_wiring_failed_is_treated_as_off():
    motor = _Motor(vision=None)

    motor._run_vision_look("mira mi pantalla", "direct")

    assert motor.calls == 1


def test_class_default_has_no_callback():
    class Bare(VisionTurnMixin):
        _vision = _Vision()

    assert Bare()._run_vision_look("mira mi pantalla", "direct") is NO_LOOK


def test_log_line_carries_codes_not_text(caplog):
    motor = _Motor()

    with caplog.at_level(logging.INFO, logger="OpenCohost"):
        motor._run_vision_look("mira mi pantalla secreta", "direct")

    lines = [r.getMessage() for r in caplog.records if "off_request" in r.getMessage()]
    assert len(lines) == 1
    assert lines[0].startswith("[VISION] decision=off_request source=direct reasons=")
    assert "secreta" not in lines[0] and "pantalla" not in lines[0]


def test_vision_on_never_fires():
    class _Step:
        def run(self, text, source):
            from opencohost.core.vision.turn_step import VisionTurnResult

            return VisionTurnResult("", ("no_trigger",), "no_vision")

    class _On:
        step = _Step()
        enabled = True

    motor = _Motor(vision=_On())

    motor._run_vision_look("mira mi pantalla", "direct")

    assert motor.calls == 0


# ---------------------------------------------------------------------------
# Host event
# ---------------------------------------------------------------------------

def _off_events(host):
    return [e for e in host.event_log.since(0)["events"] if e["action"] == "vision_off_request"]


def test_host_records_the_closed_event(host_factory):
    host = host_factory()

    host._on_vision_off_request()

    events = _off_events(host)
    assert len(events) == 1
    assert events[0]["source"] == "vision"
    assert events[0]["detail"] == {"reason": "look_requested"}


def test_host_wires_the_motor_callback(host_factory):
    host = host_factory()

    assert host.motor.runtime.on_vision_off_request == host._on_vision_off_request


def test_host_wires_the_callback_even_when_vision_wiring_failed(host_factory, monkeypatch):
    import opencohost.api.engine_host as engine_host_mod

    def boom(self):
        raise RuntimeError("no vision")

    monkeypatch.setattr(engine_host_mod.EngineHost, "_wire_vision", boom)
    host = host_factory()

    assert host.vision is None
    assert host.motor.runtime.on_vision_off_request == host._on_vision_off_request


def test_event_model_accepts_the_detail():
    from opencohost.api.models import EventOut

    out = EventOut(
        seq=1, ts=0.0, source="vision", action="vision_off_request",
        detail={"reason": "look_requested"},
    )
    assert out.detail == {"reason": "look_requested"}
