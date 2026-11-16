"""The evicted-pair ledger line must keep Kira's side short and bounded.

The fold runs after the char budget was applied, so an unbounded line could
push the prompt back over the context limit it was just trimmed to.
"""
from opencohost.core.context.prompt_assembler import (
    LEDGER_KIRA_SUMMARY_MAX_CHARS,
    ledger_kira_summary,
)


def test_cuts_at_first_period():
    assert ledger_kira_summary("Así es. Luego te cuento más.") == "Así es"


def test_cuts_at_exclamation_and_question_marks():
    assert ledger_kira_summary("¡Jajaja, vale! ¿Qué más tienes en mente?") == "¡Jajaja, vale"
    assert ledger_kira_summary("¿En serio? No lo sabía.") == "¿En serio"


def test_reply_without_terminal_punctuation_is_capped_on_a_word_boundary():
    reply = " ".join(["palabra"] * 200)  # ~1600 chars, no sentence end
    out = ledger_kira_summary(reply)
    assert len(out) <= LEDGER_KIRA_SUMMARY_MAX_CHARS
    assert out.endswith("…")
    assert out[:-1].split() == ["palabra"] * len(out[:-1].split())  # no word cut in half


def test_long_first_sentence_is_capped():
    reply = "Esta " + "es una oración muy larga " * 20 + "que termina aquí. Fin."
    assert len(ledger_kira_summary(reply)) <= LEDGER_KIRA_SUMMARY_MAX_CHARS


def test_decimal_numbers_do_not_cut_the_sentence():
    assert ledger_kira_summary("El modelo 3.5 es más rápido. Otra cosa.") == "El modelo 3.5 es más rápido"


def test_empty_or_punctuation_only_falls_back_to_a_bounded_prefix():
    assert ledger_kira_summary("") == ""
    assert ledger_kira_summary("...hola") == "...hola"[:LEDGER_KIRA_SUMMARY_MAX_CHARS]


def test_cap_is_120_chars():
    assert LEDGER_KIRA_SUMMARY_MAX_CHARS == 120
