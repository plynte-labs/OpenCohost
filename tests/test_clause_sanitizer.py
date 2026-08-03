"""Intra-sentence clause sanitizer — detection rules and severity axes.

The defect this tier exists for: a real ~16-minute agenda session emitted, inside
ONE sentence, a comma-delimited clause three times, one repetition non-contiguous.
Every pre-existing defense is structurally blind to that shape.

Scope is deliberately narrow and is tested as such: exact normalized clause
equality INSIDE a single sentence, no history, no intent detection.
"""
from __future__ import annotations

import pytest

from opencohost.core.context.repetition_guard import (
    SANITIZE_LENGTH_AXES_FLOOR,
    SANITIZE_MIN_REMAINING_CHARS,
    SANITIZE_REJECT_DISTINCT,
    SANITIZE_REJECT_FRAGMENTS,
    SANITIZE_REJECT_REMOVED_PCT,
    sanitize_clause_repetition,
)
from opencohost.smart_aggregator.kira_agenda_controller import KiraAgendaController

# The confirmed incident, verbatim.
INCIDENT = "No había roadmap, no había monetización, no había roadmap, no había roadmap."
INCIDENT_REPAIRED = "No había roadmap, no había monetización."

_LONG_CLAUSE = "el equipo revisó los logs del backend sin encontrar nada"
_PREAMBLE = (
    "Esto es un resumen largo de la sesión de anoche que existe para empujar el "
    "texto por encima del piso de los ejes de longitud sin repetir una cláusula"
)


# ---------------------------------------------------------------------------
# Detection rules
# ---------------------------------------------------------------------------

def test_noncontiguous_triple_repaired():
    """The incident: 3 occurrences, one of them non-adjacent, one sentence."""
    result = sanitize_clause_repetition(INCIDENT)

    assert result.text == INCIDENT_REPAIRED
    assert result.verdict == "repaired"
    assert result.removed_fragments == 2
    assert result.distinct_looping == 1
    assert result.max_occurrences == 3
    assert result.original_len == 76
    assert result.remaining_len == 40


@pytest.mark.parametrize("text", [
    "Sí, sí, esto va en serio.",
    "No, no, no, esto es otra cosa.",
    "Que no, que no, que no.",
])
def test_short_rhetorical_repeats_preserved(text):
    """Short interjections are rhetoric. The normalized-key floor protects them."""
    result = sanitize_clause_repetition(text)

    assert result.text == text
    assert result.verdict == "clean"
    assert result.removed_fragments == 0


def test_parallel_list_and_refrain_preserved():
    """A refrain across SENTENCES is out of scope by design — cross-sentence
    loops stay the agenda ladder's job (see test_cross_sentence_loop_still_detected)."""
    text = (
        "Micro, sigue sin funcionar. Cámara, sigue sin funcionar. "
        "Luz, sigue sin funcionar."
    )
    result = sanitize_clause_repetition(text)

    assert result.text == text
    assert result.verdict == "clean"


def test_question_exclamation_preserved():
    """Same words, different speech act: statement then echoed question."""
    text = "Así que borraste todo, ¿borraste todo de verdad?"
    result = sanitize_clause_repetition(text)

    assert result.text == text
    assert result.verdict == "clean"


def test_long_adjacent_double_dropped():
    """An adjacent long double is degeneration; the terminator is promoted."""
    result = sanitize_clause_repetition(
        "Esto no va a funcionar, esto no va a funcionar."
    )

    assert result.text == "Esto no va a funcionar."
    assert result.verdict == "repaired"
    assert result.removed_fragments == 1
    assert result.max_occurrences == 2


def test_nonadjacent_double_kept():
    """A, B, A is deliberate structure (bookending), not a loop."""
    text = "No había roadmap, no había monetización, no había roadmap."
    result = sanitize_clause_repetition(text)

    assert result.text == text
    assert result.verdict == "clean"
    assert result.removed_fragments == 0
    # Detected as a repeat, deliberately not acted on.
    assert result.max_occurrences == 2


@pytest.mark.parametrize("text", [
    "El uso subió 3,5 puntos y bajó 3.5 puntos después.",
    "Arrancamos 20:30, cerramos 22:45, sin cortes.",
    "Está en ejemplo.com/ruta,y también en ejemplo.com/ruta.",
])
def test_delimiters_without_whitespace_untouched(text):
    """A clause delimiter counts only when followed by whitespace."""
    result = sanitize_clause_repetition(text)

    assert result.text == text
    assert result.verdict == "clean"


def test_delimiter_rule_positive_control():
    """The whitespace rule must not be a blanket no-op: with the space, it fires."""
    result = sanitize_clause_repetition(
        "Está en ejemplo.com/ruta, está en ejemplo.com/ruta."
    )

    assert result.verdict == "repaired"
    assert result.text == "Está en ejemplo.com/ruta."


def test_empty_and_blank_are_clean():
    for text in ("", "   ", "\n"):
        result = sanitize_clause_repetition(text)
        assert result.text == text
        assert result.verdict == "clean"


def test_inter_sentence_whitespace_preserved():
    """A rebuild must not collapse newlines between sentences."""
    text = f"Primera línea intacta.\n{INCIDENT}\nÚltima línea intacta."
    result = sanitize_clause_repetition(text)

    assert result.text == (
        f"Primera línea intacta.\n{INCIDENT_REPAIRED}\nÚltima línea intacta."
    )


# ---------------------------------------------------------------------------
# Severity axes
# ---------------------------------------------------------------------------

def test_axis_fragments_rejects_alone():
    """Axis 1 in isolation: 5 clauses dropped, both length axes gated off."""
    clause = "el stream se cayó"
    text = clause[0].upper() + clause[1:] + ("," + f" {clause}") * 5
    result = sanitize_clause_repetition(text.rstrip(",") + ".")

    assert result.removed_fragments >= SANITIZE_REJECT_FRAGMENTS
    assert result.distinct_looping < SANITIZE_REJECT_DISTINCT
    assert result.original_len < SANITIZE_LENGTH_AXES_FLOOR
    assert result.verdict == "rejected"


def test_axis_fragments_one_notch_below_is_repaired():
    clause = "el stream se cayó"
    text = clause[0].upper() + clause[1:] + ("," + f" {clause}") * 4
    result = sanitize_clause_repetition(text.rstrip(",") + ".")

    assert result.removed_fragments == SANITIZE_REJECT_FRAGMENTS - 1
    assert result.verdict == "repaired"


def test_axis_distinct_rejects_alone():
    """Axis 2 in isolation: two separate clauses each looping, only 4 drops."""
    text = (
        "No había roadmap, no había roadmap, no había roadmap, "
        "tampoco había plan de negocio, tampoco había plan de negocio, "
        "tampoco había plan de negocio."
    )
    result = sanitize_clause_repetition(text)

    assert result.distinct_looping >= SANITIZE_REJECT_DISTINCT
    assert result.removed_fragments < SANITIZE_REJECT_FRAGMENTS
    assert result.original_len < SANITIZE_LENGTH_AXES_FLOOR
    assert result.verdict == "rejected"


def test_axis_distinct_one_notch_below_is_repaired():
    """One looping clause plus one merely-doubled clause: distinct stays at 1."""
    text = (
        "No había roadmap, no había roadmap, no había roadmap, "
        "tampoco había plan de negocio, tampoco había plan de negocio."
    )
    result = sanitize_clause_repetition(text)

    assert result.distinct_looping == SANITIZE_REJECT_DISTINCT - 1
    assert result.verdict == "repaired"


def test_axis_removed_pct_rejects_alone():
    """Axis 3 in isolation: long turn, 3 drops, >=30% gone, >=100 chars left."""
    text = f"{_PREAMBLE}. " + ", ".join([_LONG_CLAUSE] * 4) + "."
    result = sanitize_clause_repetition(text)

    assert result.removed_fragments == 3
    assert result.original_len >= SANITIZE_LENGTH_AXES_FLOOR
    assert result.removed_pct >= SANITIZE_REJECT_REMOVED_PCT
    assert result.remaining_len >= SANITIZE_MIN_REMAINING_CHARS   # axis 4 off
    assert result.distinct_looping < SANITIZE_REJECT_DISTINCT     # axis 2 off
    assert result.removed_fragments < SANITIZE_REJECT_FRAGMENTS   # axis 1 off
    assert result.verdict == "rejected"


def test_axis_removed_pct_gated_off_on_short_turns():
    """The incident removes 47% of its characters and must still be repairable —
    an earlier ungated draft of these same numbers rejected it."""
    result = sanitize_clause_repetition(INCIDENT)

    assert result.removed_pct >= SANITIZE_REJECT_REMOVED_PCT
    assert result.original_len < SANITIZE_LENGTH_AXES_FLOOR
    assert result.verdict == "repaired"


def test_axis_min_remaining_fires_but_is_never_independent():
    """Axis 4 fires on a gutted long turn.

    It cannot fire in isolation: the first copy of every dropped clause is always
    kept, so driving a >=300-char turn below 100 remaining chars requires >=4
    occurrences, which already arms axis 3. Kept as a belt, asserted honestly.
    """
    clause = "el backend volvió a caerse justo cuando arrancaba el bloque de preguntas"
    text = ", ".join([clause] * 5) + "."
    result = sanitize_clause_repetition(text)

    assert result.original_len >= SANITIZE_LENGTH_AXES_FLOOR
    assert result.remaining_len < SANITIZE_MIN_REMAINING_CHARS
    assert result.verdict == "rejected"
    # The non-independence, pinned so a future rule change surfaces it.
    assert result.removed_pct >= SANITIZE_REJECT_REMOVED_PCT


def test_clean_short_turn_is_never_length_judged():
    result = sanitize_clause_repetition("Todo tranquilo por acá.")

    assert result.verdict == "clean"
    assert result.removed_fragments == 0


# ---------------------------------------------------------------------------
# Idempotency and coexistence with the existing defenses
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    INCIDENT,
    "Sí, sí, esto va en serio.",
    "Esto no va a funcionar, esto no va a funcionar.",
    "No había roadmap, no había monetización, no había roadmap.",
    "El uso subió 3,5 puntos y bajó 3.5 puntos después.",
    f"{_PREAMBLE}. " + ", ".join([_LONG_CLAUSE] * 4) + ".",
    f"Primera línea intacta.\n{INCIDENT}\nÚltima línea intacta.",
])
def test_sanitizer_idempotent(text):
    once = sanitize_clause_repetition(text)
    twice = sanitize_clause_repetition(once.text)

    assert twice.text == once.text
    assert twice.verdict == "clean"
    assert twice.removed_fragments == 0


def test_cross_sentence_loop_still_detected():
    """The sanitizer must not disarm the agenda ladder it sits next to."""
    text = "El servidor sigue sin responder. El servidor sigue sin responder."
    result = sanitize_clause_repetition(text)

    assert result.text == text
    assert result.verdict == "clean"
    assert KiraAgendaController.has_looping_lines(result.text) is True


def test_no_double_removal_with_trim():
    """Both defenses on one text: the trim removes exactly the trailing
    cross-turn duplicate sentence, the sanitizer exactly the intra-sentence
    clauses, and the character arithmetic accounts for both independently."""
    prior = "Ese bug ya lo vimos la semana pasada."
    raw = f"{INCIDENT} {prior}"

    sanitized = sanitize_clause_repetition(raw)
    assert sanitized.text == f"{INCIDENT_REPAIRED} {prior}"

    from opencohost.smart_aggregator.sentence_trim import (
        trim_trailing_repeated_sentences,
    )
    trimmed, salvageable = trim_trailing_repeated_sentences(sanitized.text, [prior])

    assert salvageable is True
    assert trimmed == INCIDENT_REPAIRED
    # Sanitizer removed the clauses; trim removed the sentence. No overlap.
    sanitizer_removed = len(raw) - len(sanitized.text)
    trim_removed = len(sanitized.text) - len(trimmed)
    assert sanitizer_removed == len(INCIDENT) - len(INCIDENT_REPAIRED)
    assert len(raw) - len(trimmed) == sanitizer_removed + trim_removed


def test_trim_after_sanitize_is_noop_without_recent_context():
    from opencohost.smart_aggregator.sentence_trim import (
        trim_trailing_repeated_sentences,
    )
    sanitized = sanitize_clause_repetition(INCIDENT).text
    trimmed, salvageable = trim_trailing_repeated_sentences(sanitized, [])

    # Nothing left to trim, and the text still ends on a sentence boundary.
    assert trimmed == sanitized
    assert salvageable is True


# ---------------------------------------------------------------------------
# No intent detection — owner-mandated
# ---------------------------------------------------------------------------

def test_no_intent_detection_exists():
    """Fails loudly if intent detection is ever smuggled into the sanitizer.

    An operator may legitimately ask Kira to repeat something. The answer is
    scope (intra-response only), never a phrase list, a request-matching regex,
    or an extra model call.
    """
    import inspect

    from opencohost.core.context import repetition_guard

    source = inspect.getsource(repetition_guard)
    marker = "sanitize_clause_repetition"
    body = source[source.index("# Intra-sentence clause sanitizer"):]

    assert marker in body
    for banned in ("repet", "otra vez", "de nuevo", "ollama", "requests", "chat("):
        assert banned not in body.lower().replace("repetition", "").replace(
            "repeated", ""
        ).replace("repeat", ""), banned


# ---------------------------------------------------------------------------
# Pinned limitations — ADR-039 D10 / sanitizer_language_scope_20260729
# ---------------------------------------------------------------------------


def test_abbreviation_false_split_hides_the_repeat_from_both_tiers():
    """An abbreviation's '.' is a sentence boundary here, so this x3 repeat
    shreds into internally-clean pseudo-sentences — invisible to this tier AND
    to the agenda ladder (the shredded fragment is 23 chars, under its >24
    floor). Intentional: every candidate heuristic also merges legitimate short
    sentences ("Si. Vamos.") and drags the tier into cross-sentence scope. If
    this goes red, reopen conductor/tracks/sanitizer_language_scope_20260729 —
    do not "fix" the split. The fixture is load-bearing verbatim: a longer
    surname pushes the fragment past 24 and silently flips the ladder half.
    """
    text = "We spoke with Dr. Smith, we spoke with Dr. Smith, we spoke with Dr. Smith."
    result = sanitize_clause_repetition(text)

    assert result.text == text
    assert result.verdict == "clean"
    assert result.removed_fragments == 0
    assert KiraAgendaController.has_looping_lines(text) is False


@pytest.mark.parametrize("text", [
    "サーバーがまだ応答しません、サーバーがまだ応答しません、サーバーがまだ応答しません。",
    "服务器到现在还是没有任何响应，服务器到现在还是没有任何响应，服务器到现在还是没有任何响应。",
])
def test_cjk_delimiters_are_a_total_noop(text):
    """、，。 are not delimiters and CJK carries no whitespace, so an
    unsupported script gets a byte-identical no-op — never a mutation. Product
    locales are es/en. Both clauses sit ABOVE the 12-char key floor on purpose,
    so adding CJK delimiters must flip this red instead of hiding under the
    floor. If it goes red, reopen the language-scope track and re-prove rebuild
    idempotency for CJK before accepting the widening.
    """
    result = sanitize_clause_repetition(text)

    assert result.text == text
    assert result.verdict == "clean"
    assert result.removed_fragments == 0
