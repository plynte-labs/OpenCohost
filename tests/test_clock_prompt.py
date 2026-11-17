"""clock-and-compact-polish-20261010 T2: the ``clock`` prompt section.

When the turn's ``clock`` decision says ``time``, the owner turn carries one
code-built system section with the machine's local date and time, formatted
from our own Spanish/English tables (never the OS locale). Both profiles get
it; turns without it stay byte-identical (the full golden is pinned in
tests/test_prompt_profile_assembler.py). The clock is injectable.
"""
from __future__ import annotations

import json
from datetime import datetime

import pytest

from opencohost.config import decisions as dc
from opencohost.config import settings
from opencohost.config.settings import OWNER_BUNDLE_SOURCE
from opencohost.core.context import clock
from opencohost.core.context import prompt_assembler as pa
from opencohost.core.decision import runtime as decision_runtime
from opencohost.core.decision.questions import VALUES
from opencohost.core.engine import prompt_trace
from opencohost.i18n import active as i18n_active
from tests.test_decision_engine_wiring import _motor
from tests.test_warmth_prompt import PERSONAS, _activate

# B-W4: this module pins the legacy prompt layout (system-message placement, one-pair
# history deque, legacy goldens). The v2 placement is tested in tests/test_prompt_layout_v2.py.
pytestmark = pytest.mark.usefixtures("legacy_prompt_layout")

NOW = datetime(2026, 10, 10, 13, 42)
ES_LINE = (
    "CURRENT LOCAL TIME: sábado 10 de octubre de 2026, 13:42 (local time of the owner's machine). "
    "Use it only to answer the time/date question."
)
EN_LINE = (
    "CURRENT LOCAL TIME: Saturday, October 10, 2026, 13:42 (local time of the owner's machine). "
    "Use it only to answer the time/date question."
)


@pytest.fixture(autouse=True)
def _reset_active():
    i18n_active.reset_active_bundle()
    yield
    i18n_active.reset_active_bundle()


# --- formatting ---------------------------------------------------------------


def test_spanish_line():
    assert clock.format_clock_line(NOW, "es") == ES_LINE


def test_english_line():
    assert clock.format_clock_line(NOW, "en") == EN_LINE
    assert clock.format_clock_line(NOW, "en-US") == EN_LINE


def test_unknown_language_is_spanish():
    assert clock.format_clock_line(NOW, "") == ES_LINE
    assert clock.format_clock_line(NOW, None) == ES_LINE


def test_padding_and_tables():
    line = clock.format_clock_line(datetime(2026, 1, 5, 9, 5), "es")
    assert "lunes 5 de enero de 2026, 09:05" in line
    line = clock.format_clock_line(datetime(2026, 12, 27, 0, 0), "es")
    assert "domingo 27 de diciembre de 2026, 00:00" in line


class _NoStrftime(datetime):
    def strftime(self, fmt):  # the OS locale must never be consulted
        raise AssertionError("strftime used")


def test_does_not_use_the_os_locale():
    now = _NoStrftime(2026, 3, 4, 7, 8)
    assert "miércoles 4 de marzo de 2026, 07:08" in clock.format_clock_line(now, "es")
    assert "Wednesday, March 4, 2026, 07:08" in clock.format_clock_line(now, "en")


# --- assembler ----------------------------------------------------------------


def _assemble(source="direct", text="¿qué hora es?", lang="es", use_system=True, **extra):
    _activate(lang)
    return pa.PromptContextAssembler().assemble(
        text,
        source,
        system_prompt=PERSONAS[lang],
        use_system_role=use_system,
        is_local=True,
        provider_cfg={},
        request_model="llama3",
        history_snapshot=[],
        personalization_enabled=False,
        memorias_enabled=False,
        **extra,
    )


@pytest.mark.parametrize("profile", ["full", "compact"])
@pytest.mark.parametrize("source", ["direct", "ptt"])
def test_owner_voice_turn_gets_the_clock_section(source, profile):
    setup = _assemble(source, clock_now=NOW, prompt_profile=profile)
    sections = dict(setup.sections)
    assert sections["clock"] == ES_LINE
    assert "clock" in setup.system_section_names
    assert ES_LINE in setup.messages[0]["content"]
    assert ES_LINE not in setup.prompt_echo_reference


def test_english_ui_gets_the_english_line():
    setup = _assemble(clock_now=NOW, lang="en", text="what time is it?")
    assert dict(setup.sections)["clock"] == EN_LINE


def test_no_system_role_carries_it_in_the_prompt():
    setup = _assemble(clock_now=NOW, use_system=False)
    assert ES_LINE in setup.messages[-1]["content"]


@pytest.mark.parametrize("source", ["chat", OWNER_BUNDLE_SOURCE])
def test_other_sources_never_get_it(source):
    setup = _assemble(source, clock_now=NOW)
    assert "clock" not in dict(setup.sections)


def test_without_clock_now_the_prompt_is_unchanged():
    base = _assemble()
    assert "clock" not in dict(base.sections)
    assert _assemble(clock_now=None).messages == base.messages


# --- engine -------------------------------------------------------------------


class ClockBackend:
    def __init__(self, choice="time", conf=0.95):
        self.choice, self.conf, self.calls = choice, conf, []

    def ask(self, state, questions, timeout_s):
        self.calls.append(tuple(questions))
        return {
            "ok": True,
            "answers": {
                q: {"choice": self.choice if q == "clock" else VALUES[q][0], "answer_confidence": self.conf}
                for q in questions
            },
        }


def _turn(monkeypatch, text, source="direct"):
    motor, seen = _motor(monkeypatch)
    motor._clock_now = lambda: NOW
    motor._generar_dialogo(text, source=source, commit_history=True)
    return motor, seen


def _system(seen) -> str:
    return seen["messages"][0][0]["content"]


def test_rule_time_ask_carries_the_line_without_laya(monkeypatch):
    _, seen = _turn(monkeypatch, "¿Qué hora es?")
    assert ES_LINE in _system(seen)


def test_plain_turn_has_no_line(monkeypatch):
    _, seen = _turn(monkeypatch, "hola")
    assert "CURRENT LOCAL TIME" not in _system(seen)


def test_chat_time_ask_has_no_line(monkeypatch):
    _, seen = _turn(monkeypatch, "¿qué hora es?", source="chat")
    assert all("CURRENT LOCAL TIME" not in m["content"] for m in seen["messages"][0])


def test_laya_on_adds_the_line_when_the_rule_misses(monkeypatch):
    decision_runtime.install_backend(ClockBackend("time"))
    _, seen = _turn(monkeypatch, "oye, ¿sabes qué día cae hoy?")
    assert ES_LINE in _system(seen)


def test_laya_shadow_or_off_never_adds_it(monkeypatch):
    decision_runtime.install_backend(ClockBackend("time"))
    dc.set_decision("clock", mode="shadow")
    _, seen = _turn(monkeypatch, "oye, ¿sabes qué día cae hoy?")
    assert "CURRENT LOCAL TIME" not in _system(seen)
    dc.set_decision("clock", mode="off")
    _, seen = _turn(monkeypatch, "oye, ¿sabes qué día cae hoy?")
    assert "CURRENT LOCAL TIME" not in _system(seen)


def test_off_mode_still_lets_the_rule_act(monkeypatch):
    decision_runtime.install_backend(ClockBackend("no_time"))
    dc.set_decision("clock", mode="off")
    _, seen = _turn(monkeypatch, "¿qué día es hoy?")
    assert ES_LINE in _system(seen)


def test_prompt_trace_records_the_clock_section(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "LOG_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "PROMPT_TRACE_ENABLED", True)
    prompt_trace._reset_for_tests()
    _turn(monkeypatch, "¿qué hora es?")
    recs = []
    for f in sorted((tmp_path / "prompt_trace").glob("*.jsonl")):
        recs.extend(json.loads(line) for line in f.read_text(encoding="utf-8").splitlines())
    names = [s["name"] for s in recs[0]["sections"]]
    assert "clock" in names
