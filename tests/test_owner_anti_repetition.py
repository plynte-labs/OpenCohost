"""Owner anti-repetition: already-said block + mild owner sampling penalties."""
from __future__ import annotations

import pytest

from opencohost.config import settings
from opencohost.core.context import prompt_assembler as pa
from opencohost.core.context.prompt_assembler import PromptContextAssembler
from opencohost.i18n import active as i18n_active

# B-W4: this module pins the legacy prompt layout (system-message placement, one-pair
# history deque, legacy goldens). The v2 placement is tested in tests/test_prompt_layout_v2.py.
pytestmark = pytest.mark.usefixtures("legacy_prompt_layout")


class _Bundle:
    def __init__(self, code):
        self.code = code


def _history(*assistant_lines):
    out = []
    for line in assistant_lines:
        out.append({"role": "user", "content": "hola", "source": "direct"})
        out.append({"role": "assistant", "content": line, "source": "direct"})
    return out


def _assemble(source="direct", history=()):
    return PromptContextAssembler().assemble(
        "cuéntame algo",
        source,
        system_prompt="Eres Kira.",
        use_system_role=True,
        is_local=True,
        provider_cfg={},
        request_model="llama3",
        history_snapshot=list(history),
        personalization_enabled=False,
        memorias_enabled=False,
    )


@pytest.fixture
def lang(monkeypatch):
    def _set(code):
        monkeypatch.setattr(i18n_active, "get_active_bundle", lambda: _Bundle(code))

    return _set


def _system(setup):
    return setup.messages[0]["content"]


@pytest.mark.parametrize("source", ["direct", "ptt", settings.OWNER_BUNDLE_SOURCE])
def test_block_present_with_prior_assistant_turns(source, lang):
    lang("es")
    setup = _assemble(source, _history("Soy Kira, tu co-host virtual. Qué buen día.", "Claro, te cuento."))
    system = _system(setup)
    assert "Ya dijiste esto" in system
    assert "Soy Kira, tu co-host virtual." in system
    assert "Claro, te cuento." in system


def test_block_absent_without_prior_assistant_turn(lang):
    lang("es")
    assert "Ya dijiste esto" not in _system(_assemble("direct", []))
    only_user = [{"role": "user", "content": "hola", "source": "direct"}]
    assert "Ya dijiste esto" not in _system(_assemble("direct", only_user))


def test_block_is_last_part_and_uses_last_two_openers_only(lang):
    lang("es")
    setup = _assemble("direct", _history("Primera uno. x", "Segunda dos. y", "Tercera tres. z"))
    system = _system(setup)
    assert "Primera uno." not in system
    assert system.index("Segunda dos.") < system.index("Tercera tres.")
    assert system.rindex("Ya dijiste esto") > system.index("Modelo base actual")


def test_opener_is_trimmed_and_capped(lang):
    lang("es")
    long_line = "palabra " * 60
    system = _system(_assemble("direct", _history(long_line)))
    tail = system[system.index("Ya dijiste esto"):]
    quoted = [l for l in tail.splitlines() if l.startswith("- ")]
    assert len(quoted) == 1
    assert len(quoted[0]) == 2 + pa.ALREADY_SAID_OPENER_MAX_CHARS - 1  # trailing space trimmed


def test_localized_english_and_fallback(lang):
    lang("en")
    hist = _history("I am Kira, your co-host. Hello there.")
    en = _system(_assemble("direct", hist))
    assert "You already said this" in en
    assert "Ya dijiste" not in en
    lang("fr")
    assert "You already said this" in _system(_assemble("direct", hist))
    lang("es-AR")
    assert "Ya dijiste esto" in _system(_assemble("direct", hist))


def test_wording_mentions_no_reintroduction_and_no_closing_question(lang):
    lang("es")
    system = _system(_assemble("direct", _history("Hola, soy Kira.")))
    assert "no vuelvas a presentarte" in system
    assert "No termines cada respuesta con una pregunta" in system


@pytest.mark.parametrize("source", ["chat", "kira-agenda", "livevoice"])
def test_non_owner_prompts_never_get_block(source, lang):
    lang("es")
    setup = _assemble(source, _history("Soy Kira. Hola."))
    assert "Ya dijiste esto" not in "\n".join(m["content"] for m in setup.messages)


def test_owner_options_carry_mild_penalties():
    for source in ("direct", "ptt", settings.OWNER_BUNDLE_SOURCE):
        opts = _assemble(source).opciones_llm
        assert opts["repeat_penalty"] == settings.OWNER_REPEAT_PENALTY == 1.1
        assert opts["presence_penalty"] == settings.OWNER_PRESENCE_PENALTY == 0.3
        assert "frequency_penalty" not in opts


def test_chat_penalties_unchanged_and_agenda_has_none(monkeypatch):
    monkeypatch.setattr(settings, "AGENDA_PROGRESSION_VARIANT", "")
    monkeypatch.setattr(settings, "AGENDA_PROGRESSION_ENABLED", False)
    chat = _assemble("chat").opciones_llm
    assert chat["repeat_penalty"] == settings.CHAT_REPEAT_PENALTY == 1.2
    assert chat["presence_penalty"] == settings.CHAT_PRESENCE_PENALTY == 0.5
    assert chat["frequency_penalty"] == settings.CHAT_FREQUENCY_PENALTY == 0.5
    for source in ("kira-agenda", "livevoice"):
        opts = _assemble(source).opciones_llm
        assert "repeat_penalty" not in opts
        assert "presence_penalty" not in opts


def test_agenda_progression_repeat_penalty_is_agenda_only():
    # Progression ON (default): agenda-only repeat_penalty; no presence/frequency
    # penalties, and livevoice stays penalty-free.
    opts = _assemble("kira-agenda").opciones_llm
    assert opts["repeat_penalty"] == settings.AGENDA_REPEAT_PENALTY
    assert "presence_penalty" not in opts
    assert "frequency_penalty" not in opts
    assert "repeat_penalty" not in _assemble("livevoice").opciones_llm
