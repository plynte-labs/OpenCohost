"""F4 (interruptible_speech_architecture_20260804/runtime-findings-20260807.md)
— TTS markdown normalization.

`_tts_normalize_markdown` runs as the FIRST line of
`_sanitize_tts_text_for_playback`, before `_tts_strip_non_latin`: ordering is
load-bearing because the non-Latin strip's cleanup pass collapses newlines,
so markdown block detection must see the raw, still-lined text.

Pure-function tests only -- both functions under test are free functions in
`tts_sanitizer.py`, no `MotorVocalIA` construction needed.

`**x**`/`*x*`/`***x***` are deliberately NOT re-implemented inside
`_tts_normalize_markdown`: the pre-existing `_TTS_MARKDOWN_EMPHASIS_RE` +
keep-guard in `_sanitize_tts_text_for_playback` (pinned by
test_llm_engine_timeouts.py's math/code-like-asterisk tests) already handles
them correctly, further down the SAME function, unchanged. This file's B4
coverage therefore splits across `_tts_normalize_markdown` directly
(`__x__`/`_x_`, the genuinely new capability) and `_sanitize_tts_text_for_playback`
(confirming `**x**` still resolves end-to-end).
"""
from __future__ import annotations

import pytest

from opencohost.core.speech.tts_sanitizer import (
    _sanitize_tts_text_for_playback,
    _tts_normalize_markdown,
)
from opencohost.i18n import active as i18n_active


@pytest.fixture(autouse=True)
def _reset_active_locale():
    i18n_active.reset_active_bundle()
    yield
    i18n_active.reset_active_bundle()


# ---------------------------------------------------------------------------
# Stage A1 — fenced code blocks
# ---------------------------------------------------------------------------

def test_fenced_code_block_becomes_one_sentence():
    text = "```python\nprint('hi')\n```"
    assert _tts_normalize_markdown(text) == i18n_active.tts_markdown_code_notice()


def test_multiple_fenced_code_blocks_each_get_their_own_sentence():
    text = "```a```\ntexto\n```b```"
    notice = i18n_active.tts_markdown_code_notice()
    assert _tts_normalize_markdown(text) == f"{notice} texto {notice}"


def test_fenced_code_runs_first_so_inline_rules_never_touch_the_interior():
    text = "```\n**bold** [link](url) `code` | pipe\n```"
    result = _tts_normalize_markdown(text)
    assert result == i18n_active.tts_markdown_code_notice()
    assert "bold" not in result
    assert "link" not in result
    assert "pipe" not in result


# ---------------------------------------------------------------------------
# Stage A2 — markdown tables (>=2 contiguous pipe lines)
# ---------------------------------------------------------------------------

def test_markdown_table_becomes_one_sentence_with_row_count():
    """n counts DATA rows only -- header and the `|---|---|` alignment
    separator are structure, not rows Kira should claim exist (reviewer MINOR:
    a 1-data-row table was announced as n=3)."""
    text = "| A | B |\n|---|---|\n| 1 | 2 |"
    expected = i18n_active.tts_markdown_table_notice().format(n=1)
    assert _tts_normalize_markdown(text) == expected


def test_table_row_count_excludes_the_alignment_separator_row_even_without_dashes_only_check():
    """Separator row uses colons for alignment (`:---:`) -- still not data."""
    text = "| A | B |\n|:---:|:---:|\n| 1 | 2 |\n| 3 | 4 |"
    expected = i18n_active.tts_markdown_table_notice().format(n=2)
    assert _tts_normalize_markdown(text) == expected


def test_table_notice_falls_back_to_raw_string_when_format_raises(monkeypatch):
    """A locale template missing the {n} placeholder (or otherwise incompatible
    with .format(n=...)) must never crash the TTS path -- fall back to the raw
    notice string instead."""
    monkeypatch.setattr(
        i18n_active, "tts_markdown_table_notice", lambda: "{count} filas en la tabla"
    )
    text = "| A | B |\n|---|---|\n| 1 | 2 |"
    assert _tts_normalize_markdown(text) == "{count} filas en la tabla"


def test_single_pipe_line_is_not_treated_as_a_table():
    """Below the >=2 contiguous-line threshold: falls through to the residual
    pipe rule (B5) instead of becoming a table sentence."""
    text = "| just one row |\ntexto normal"
    assert _tts_normalize_markdown(text) == "just one row texto normal"


# ---------------------------------------------------------------------------
# Stage A3 — display LaTeX $$...$$
# ---------------------------------------------------------------------------

def test_display_math_block_becomes_one_sentence():
    text = "$$E = mc^2$$"
    assert _tts_normalize_markdown(text) == i18n_active.tts_markdown_formula_notice()


# ---------------------------------------------------------------------------
# Stage A4 — ATX headings
# ---------------------------------------------------------------------------

def test_atx_heading_strips_marker_and_adds_terminal_period():
    assert _tts_normalize_markdown("## Introducción") == "Introducción."


def test_atx_heading_does_not_double_existing_terminal_punctuation():
    assert _tts_normalize_markdown("# Ya termina.") == "Ya termina."


# ---------------------------------------------------------------------------
# Stage A5 — bullets / numbered items
# ---------------------------------------------------------------------------

def test_bullet_item_strips_marker_and_ensures_terminal_punctuation():
    assert _tts_normalize_markdown("- Comprar leche") == "Comprar leche."


def test_numbered_item_strips_marker_and_ensures_terminal_punctuation():
    assert _tts_normalize_markdown("1. Comprar leche") == "Comprar leche."


def test_short_numbered_marker_up_to_three_digits_still_strips():
    assert _tts_normalize_markdown("3) Tercera opción") == "Tercera opción."


# Reviewer BLOCKER: the numbered-list rule matched ANY leading digits, so a
# spoken year ("1999. Fue un buen año...") silently lost its digits while
# chat kept them -- content loss, not formatting. Marker is capped at 1-3
# digits so real list items (up to 999) still strip and years/quantities
# survive untouched (identity fast-path).
def test_four_digit_year_is_not_mistaken_for_a_numbered_list_marker():
    text = "1999. Fue un buen año para el rock."
    assert _tts_normalize_markdown(text) is text


def test_another_four_digit_year_is_not_mistaken_for_a_numbered_list_marker():
    text = "2026. Ese fue el año."
    assert _tts_normalize_markdown(text) is text


def test_four_digit_year_mid_text_survives_alongside_a_real_line_above_it():
    text = "Bien.\n1999. Fue un buen año."
    assert _tts_normalize_markdown(text) is text


# ---------------------------------------------------------------------------
# Stage A6 — blockquote markers stripped, horizontal rules dropped
# ---------------------------------------------------------------------------

def test_blockquote_marker_is_stripped_without_forcing_punctuation():
    assert _tts_normalize_markdown("> Cita textual") == "Cita textual"


def test_horizontal_rule_is_dropped():
    text = "Antes\n---\nDespués"
    assert _tts_normalize_markdown(text) == "Antes Después"


# ---------------------------------------------------------------------------
# Stage B1 — inline LaTeX (TeX-ish char required; dollar amounts untouched)
# ---------------------------------------------------------------------------

def test_inline_latex_with_texish_char_becomes_formula_phrase():
    text = "El área es $x^2$ metros"
    phrase = i18n_active.tts_markdown_formula_inline()
    assert _tts_normalize_markdown(text) == f"El área es {phrase} metros"


def test_dollar_amounts_are_untouched_identity():
    text = "Cuesta $5 y $10"
    assert _tts_normalize_markdown(text) is text


# ---------------------------------------------------------------------------
# Stage B2 — links
# ---------------------------------------------------------------------------

def test_markdown_link_becomes_its_text():
    text = "Mira [este enlace](https://example.com) ahora"
    assert _tts_normalize_markdown(text) == "Mira este enlace ahora"


# ---------------------------------------------------------------------------
# Stage B3 — inline code
# ---------------------------------------------------------------------------

def test_inline_code_becomes_its_inner_text():
    text = "Corré `python script.py` en la terminal"
    assert _tts_normalize_markdown(text) == "Corré python script.py en la terminal"


# ---------------------------------------------------------------------------
# Stage B4 — __x__ / _x_ (snake_case untouched); **x** stays with the
# pre-existing asterisk regex, confirmed via the full pipeline below.
# ---------------------------------------------------------------------------

def test_double_underscore_emphasis_becomes_its_inner_text():
    text = "Esto es __muy importante__ ahora"
    assert _tts_normalize_markdown(text) == "Esto es muy importante ahora"


def test_single_underscore_emphasis_becomes_its_inner_text():
    text = "Esto es _importante_ ahora"
    assert _tts_normalize_markdown(text) == "Esto es importante ahora"


def test_snake_case_is_untouched_identity():
    text = "La variable se llama mi_variable_nombre"
    assert _tts_normalize_markdown(text) is text


def test_full_pipeline_still_converts_double_asterisk_bold():
    """**x** is handled by the pre-existing tail regex in
    _sanitize_tts_text_for_playback, not duplicated in _tts_normalize_markdown."""
    text = "Esto es **muy importante**."
    assert _tts_normalize_markdown(text) is text  # unchanged by the new stage
    assert _sanitize_tts_text_for_playback(text) == "Esto es muy importante."


# ---------------------------------------------------------------------------
# Stage B5 — residual pipes -> space
# ---------------------------------------------------------------------------

def test_residual_pipe_characters_become_spaces():
    text = "Opciones: A | B | C"
    assert _tts_normalize_markdown(text) == "Opciones: A B C"


# ---------------------------------------------------------------------------
# Hard constraint — identity fast-path (same object) when no rule fires
# ---------------------------------------------------------------------------

def test_identity_fast_path_returns_same_object_when_no_rule_fires():
    text = "Respuesta normal sin marcado especial."
    assert _tts_normalize_markdown(text) is text
    assert _sanitize_tts_text_for_playback(text) is text


# ---------------------------------------------------------------------------
# Ordering — markdown detection must run before the non-Latin strip collapses
# newlines, else a heading line fuses with the body that follows it.
# ---------------------------------------------------------------------------

def test_heading_and_non_latin_glyph_both_normalize_in_the_correct_order():
    text = "## Título 😀\n\nBody text aquí."
    result = _sanitize_tts_text_for_playback(text)
    assert result == "Título. Body text aquí."
    assert "😀" not in result
    assert "#" not in result


# ---------------------------------------------------------------------------
# Integration-shaped — the 15:40 session answer: wide table + $...$ cells +
# fenced python block, collapsing from dozens of would-be fragments to a
# handful of sentences.
# ---------------------------------------------------------------------------

def test_integration_wide_table_and_fenced_code_collapse_to_a_handful_of_sentences():
    text = (
        "## Resumen\n"
        "\n"
        "| Métrica | Valor |\n"
        "|---|---|\n"
        "| Costo | $5 |\n"
        "| Total | $10 |\n"
        "\n"
        "```python\n"
        "print(\"done\")\n"
        "```\n"
        "\n"
        "Gracias por leer."
    )
    table_notice = i18n_active.tts_markdown_table_notice().format(n=2)  # 2 data rows, header+separator excluded
    code_notice = i18n_active.tts_markdown_code_notice()
    expected = f"Resumen. {table_notice} {code_notice} Gracias por leer."

    result = _tts_normalize_markdown(text)

    assert result == expected
    assert result.count(".") == 4  # a handful of sentences, not dozens of fragments
    for leftover in ("$", "|", "`", "#"):
        assert leftover not in result
