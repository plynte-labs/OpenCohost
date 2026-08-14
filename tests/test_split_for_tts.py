"""Unit tests for SpeechPipelineMixin._split_for_tts and its two stages.

_hablar_impl used to inline sanitize + sentence-split + comma-sub-split
into one block. It is now `_split_for_tts`, composed of two separate,
independently callable stages:

  - `_sanitize_for_tts`: strip markdown/quotes/newlines, then split into
    sentences on terminator+whitespace (B_ws — the same boundary
    production TTS ships today). Returns the list of SENTENCES: the
    granularity output_guard is meant to run at (not wired here).
  - `_fragment_for_tts`: take that sentence list and sub-split any
    sentence over 25 words on internal commas/semicolons, regrouping at
    >=8 words. Returns the final TTS-synthesizable chunks.

`_split_for_tts` is just `_fragment_for_tts(_sanitize_for_tts(text))` —
the extraction is mechanical, not a behavior change. These tests pin the
two-stage shape directly; tests/test_speech_router_stack.py's
`test_pre_split_is_replayed_verbatim_and_a_rejoined_slice_is_not` pins
that `_hablar_impl`'s real output through this path is unchanged.
"""

from opencohost.core import llm_engine

# Import via the `llm_engine` package entrypoint, not
# `opencohost.core.engine.llm_engine_speech` directly -- llm_engine_speech.py
# imports `llm_engine` at module level for `_eng`, so importing the mixin
# module first triggers a circular partial-import (see that file's own
# comment on the SpeechRouter TYPE_CHECKING import for the same pattern).
SpeechPipelineMixin = llm_engine.SpeechPipelineMixin


def test_sanitize_for_tts_splits_into_sentences_on_terminator_and_whitespace():
    sentences = SpeechPipelineMixin._sanitize_for_tts(
        "Primera oración. Segunda oración! ¿Tercera oración?"
    )
    assert sentences == ["Primera oración.", "Segunda oración!", "¿Tercera oración?"]


def test_sanitize_for_tts_strips_markdown_quotes_and_newlines():
    sentences = SpeechPipelineMixin._sanitize_for_tts('Como *IA*, no puedo\n"opinar".')
    assert sentences == ["Como IA, no puedo opinar."]


def test_fragment_for_tts_keeps_short_sentences_as_one_chunk_each():
    chunks = SpeechPipelineMixin._fragment_for_tts(["Frase corta uno.", "Frase corta dos."])
    assert chunks == ["Frase corta uno.", "Frase corta dos."]


def test_fragment_for_tts_subsplits_and_regroups_a_long_comma_heavy_sentence():
    g0 = " ".join(f"g0w{i}" for i in range(15))
    g1 = " ".join(f"g1w{i}" for i in range(8))
    g2 = " ".join(f"g2w{i}" for i in range(10))
    long_sentence = f"{g0}, {g1}, {g2}."

    chunks = SpeechPipelineMixin._fragment_for_tts([long_sentence])

    # Sub-split on commas, regrouped at >= 8 words per chunk (MIN_PALABRAS_POR_CHUNK).
    assert len(chunks) >= 2
    assert " ".join(chunks).replace(",", "").split() == long_sentence.replace(",", "").split()


def test_split_for_tts_is_sanitize_then_fragment_composed():
    text = 'Como *IA*, no puedo opinar. ' + ", ".join(f"palabra{i}" for i in range(30)) + "."

    combined = SpeechPipelineMixin._split_for_tts(text)
    staged = SpeechPipelineMixin._fragment_for_tts(SpeechPipelineMixin._sanitize_for_tts(text))

    assert combined == staged


def test_split_for_tts_matches_hablar_impls_current_inline_output():
    """The extraction must be byte-identical to the code it replaced."""
    text = "Hola mundo, esto es una prueba. Segunda frase con más texto aquí."
    assert SpeechPipelineMixin._split_for_tts(text) == [
        "Hola mundo, esto es una prueba.",
        "Segunda frase con más texto aquí.",
    ]
