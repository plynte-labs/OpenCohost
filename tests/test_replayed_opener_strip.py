"""Replayed assistant history drops a leading opener / vocative (kira-natural-replies task A)."""
from __future__ import annotations

import copy

import pytest

from opencohost.core.context import prompt_assembler as pa
from opencohost.core.context.prompt_assembler import PromptContextAssembler
from opencohost.i18n import active as i18n_active

strip = pa._strip_replayed_opener
NAMES = frozenset({"franguh"})
TAIL = "esa idea suena bien para el stream de hoy."


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Bueno, " + TAIL, "Esa idea suena bien para el stream de hoy."),
        ("Pues, " + TAIL, "Esa idea suena bien para el stream de hoy."),
        ("A ver, " + TAIL, "Esa idea suena bien para el stream de hoy."),
        ("Well, " + TAIL, "Esa idea suena bien para el stream de hoy."),
        ("Okay, " + TAIL, "Esa idea suena bien para el stream de hoy."),
        ("Hola! " + TAIL, "Esa idea suena bien para el stream de hoy."),
        ("Mira: " + TAIL, "Esa idea suena bien para el stream de hoy."),
        ("Franguh, " + TAIL, "Esa idea suena bien para el stream de hoy."),
        ("Bueno, Franguh, " + TAIL, "Esa idea suena bien para el stream de hoy."),
        ("BUENO, FRANGUH, " + TAIL, "Esa idea suena bien para el stream de hoy."),
        ("Bueno... Franguh. " + TAIL, "Esa idea suena bien para el stream de hoy."),
    ],
)
def test_strips_marker_and_vocative(text, expected):
    assert strip(text, NAMES) == expected


def test_english_marker_with_remainder_recapitalized():
    assert strip("Look, that idea sounds great for today's stream.", NAMES) == (
        "That idea sounds great for today's stream."
    )


def test_accent_insensitive_marker():
    assert strip("Ahí, " + TAIL, NAMES) == "Ahí, " + TAIL  # not a marker
    assert strip("Pués, " + TAIL, NAMES) == "Esa idea suena bien para el stream de hoy."


def test_subject_guard_name_without_punctuation():
    text = "Franguh tiene razón sobre el orden del stream de hoy."
    assert strip(text, NAMES) == text


def test_bare_marker_guard():
    text = "Bueno es saberlo antes de empezar el stream de hoy."
    assert strip(text, NAMES) == text


def test_each_pass_strips_two_segments_and_repeats_to_fixed_point():
    # One pass takes marker + name; a second pass takes the remaining marker, so the
    # helper stays idempotent.
    text = "Bueno, Franguh, hola, " + TAIL
    assert strip(text, NAMES) == "Esa idea suena bien para el stream de hoy."
    assert strip("Bueno, Mira, Pues, " + TAIL, NAMES) == "Esa idea suena bien para el stream de hoy."


def test_name_after_non_marker_not_stripped():
    text = "Claro, Franguh, " + TAIL
    assert strip(text, NAMES) == text


def test_recapitalize_skips_inverted_punctuation():
    assert strip("Bueno, ¿qué tal si probamos otra cosa en el stream?", NAMES) == (
        "¿Qué tal si probamos otra cosa en el stream?"
    )
    assert strip("Bueno, ¡vamos a empezar con el stream de hoy!", NAMES) == (
        "¡Vamos a empezar con el stream de hoy!"
    )


def test_short_remainder_keeps_original():
    assert strip("Bueno, Franguh, sí.", NAMES) == "Bueno, Franguh, sí."


def test_idempotent():
    once = strip("Bueno, Franguh, " + TAIL, NAMES)
    assert strip(once, NAMES) == once


def test_empty_names_strips_markers_only():
    assert strip("Bueno, Franguh, " + TAIL, frozenset()) == "Franguh, " + TAIL
    assert strip("Bueno, " + TAIL, ()) == "Esa idea suena bien para el stream de hoy."


def test_short_names_ignored():
    # A two-character name would match too much; it is never treated as a vocative.
    assert strip("Al, " + TAIL, frozenset({"al"})) == "Al, " + TAIL


def test_non_string_and_empty_are_safe():
    assert strip("", NAMES) == ""
    assert strip(None, NAMES) == ""


# ---- assembler integration -------------------------------------------------


class _Bundle:
    def __init__(self, code):
        self.code = code


OPENER_REPLY = "Bueno, Franguh, " + TAIL


def _history(*assistant_lines):
    out = []
    for line in assistant_lines:
        out.append({"role": "user", "content": "hola", "source": "direct"})
        out.append({"role": "assistant", "content": line, "source": "direct"})
    return out


@pytest.fixture
def nickname(monkeypatch):
    monkeypatch.setattr(
        pa.personalization,
        "load_personalization",
        lambda: {"enabled": True, "nickname": "Franguh"},
    )
    monkeypatch.setattr(i18n_active, "get_active_bundle", lambda: _Bundle("es"))


def _assemble(history, source="direct", personalization_enabled=True):
    return PromptContextAssembler().assemble(
        "cuéntame algo",
        source,
        system_prompt="Eres Kira.",
        use_system_role=True,
        is_local=True,
        provider_cfg={},
        request_model="llama3",
        history_snapshot=history,
        personalization_enabled=personalization_enabled,
        memorias_enabled=False,
    )


def _replayed_assistant(setup):
    return [m["content"] for m in setup.messages if m["role"] == "assistant"]


def test_assembled_replay_is_stripped(nickname):
    setup = _assemble(_history(OPENER_REPLY, "Bueno, " + TAIL))
    for content in _replayed_assistant(setup):
        assert not content.startswith("Bueno")
        assert content.startswith("Esa idea")


def test_viewer_assistant_only_history_is_stripped(nickname):
    setup = _assemble(_history(OPENER_REPLY), source="chat")
    assert _replayed_assistant(setup) == ["Esa idea suena bien para el stream de hoy."]


def test_stored_history_unchanged(nickname):
    history = _history(OPENER_REPLY, "Pues, " + TAIL)
    before = copy.deepcopy(history)
    _assemble(history)
    assert history == before


def test_prompt_bytes_stable_across_calls(nickname):
    history = _history(OPENER_REPLY, "Bueno, " + TAIL)
    first = _assemble(history)
    second = _assemble(history)
    assert first.messages == second.messages


@pytest.mark.usefixtures("legacy_prompt_layout")
def test_already_said_block_does_not_quote_raw_opener(nickname):
    setup = _assemble(_history(OPENER_REPLY))
    system = setup.messages[0]["content"]
    assert "Ya dijiste esto" in system
    assert "Bueno, Franguh" not in system
    assert "Esa idea suena bien para el stream de hoy." in system


def test_personalization_failure_fails_open(monkeypatch):
    monkeypatch.setattr(i18n_active, "get_active_bundle", lambda: _Bundle("es"))

    def _boom():
        raise RuntimeError("no profile")

    monkeypatch.setattr(pa.personalization, "load_personalization", _boom)
    setup = _assemble(_history("Bueno, " + TAIL))
    assert _replayed_assistant(setup) == ["Esa idea suena bien para el stream de hoy."]


def test_evicted_pairs_carry_stored_text(nickname, monkeypatch):
    """The eviction/digest path dedupes by stored content; it must see raw entries."""
    seen = []

    def _on_evicted(pairs, native_ctx, effective_ctx):
        seen.extend(pairs)

    history = _history(*[f"Bueno, Franguh, respuesta número {i} larga y detallada." for i in range(6)])
    PromptContextAssembler().assemble(
        "cuéntame algo",
        "direct",
        system_prompt="Eres Kira.",
        use_system_role=True,
        is_local=True,
        provider_cfg={},
        request_model="llama3",
        history_snapshot=history,
        personalization_enabled=True,
        memorias_enabled=False,
        native_ctx_resolver=lambda m: 600,
        effective_ctx_resolver=lambda m, n: 600,
        on_evicted_pairs=_on_evicted,
    )
    assert seen, "expected the tiny context to evict pairs"
    stored = {m["content"] for m in history if m["role"] == "assistant"}
    for _, a_msg in seen:
        assert a_msg["content"] in stored
