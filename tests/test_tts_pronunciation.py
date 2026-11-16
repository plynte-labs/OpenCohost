"""TTS pronunciation dictionary (tts_pronunciation_dictionary / tts-pronunciation-dictionary T1).

Literal-only matching, applied at the END of the sanitizer so every safety
stage still wins. The module default is an EMPTY in-memory dictionary; the
autouse fixture below resets it so no test ever depends on the owner's real
``pronunciation.json``.
"""

import json
import time

import pytest

from opencohost.core.speech import pronunciation as pr
from opencohost.core.speech.tts_sanitizer import _sanitize_tts_text_for_playback


@pytest.fixture(autouse=True)
def _hermetic(tmp_path, monkeypatch):
    monkeypatch.setattr(pr, "PRONUNCIATION_FILE", str(tmp_path / "pronunciation.json"))
    pr.reset_pronunciation()
    yield
    pr.reset_pronunciation()


def _set(*pairs):
    errors = pr.set_entries([{"match": m, "replace": r} for m, r in pairs])
    assert errors == []


# --- matching -------------------------------------------------------------


def test_default_dictionary_is_empty_and_inert():
    assert pr.get_entries() == []
    assert pr.apply_pronunciation("OBS is fine") == "OBS is fine"


def test_literal_whole_word_case_insensitive():
    _set(("OBS", "o be ese"))
    assert pr.apply_pronunciation("open obs now") == "open o be ese now"
    assert pr.apply_pronunciation("OBS, Obs.") == "o be ese, o be ese."
    # Whole word only: no match inside a longer word.
    assert pr.apply_pronunciation("jobsite OBSOLETE") == "jobsite OBSOLETE"


def test_longest_first_on_overlap():
    _set(("GG", "bien jugado"), ("GG EZ", "bien jugado facil"))
    assert pr.apply_pronunciation("gg ez team") == "bien jugado facil team"
    assert pr.apply_pronunciation("gg team") == "bien jugado team"


@pytest.mark.parametrize(
    "match,text,expected",
    [
        ("C++", "I love C++ a lot", "I love ce mas mas a lot"),
        ("a.b", "see a.b and axb", "see ce mas mas and axb"),
        ("(x)", "call (x) now", "call ce mas mas now"),
    ],
)
def test_regex_metacharacters_stay_literal(match, text, expected):
    _set((match, "ce mas mas"))
    assert pr.apply_pronunciation(text) == expected


def test_spanish_accents_and_enye_word_boundaries():
    _set(("año", "anio"))
    assert pr.apply_pronunciation("el año pasado") == "el anio pasado"
    assert pr.apply_pronunciation("el AÑO pasado") == "el anio pasado"
    assert pr.apply_pronunciation("cumpleaños y añoranza") == "cumpleaños y añoranza"


def test_replacement_is_literal_no_backreferences():
    _set(("OBS", r"\1 \g"))
    assert pr.apply_pronunciation("OBS") == r"\1 \g"


def test_empty_replace_silences_the_word():
    _set(("lol", ""))
    assert pr.apply_pronunciation("that was lol funny") == "that was funny"


# --- validation -----------------------------------------------------------


def _codes(raw):
    _, errors = pr.validate_entries(raw)
    return {(e["index"], e["field"], e["code"]) for e in errors}


def test_validation_accepts_limits_boundary():
    raw = [{"match": "m" * 64, "replace": "r" * 128}]
    entries, errors = pr.validate_entries(raw)
    assert errors == [] and len(entries) == 1


def test_validation_rejects_too_many_entries():
    raw = [{"match": f"w{i}", "replace": "x"} for i in range(201)]
    _, errors = pr.validate_entries(raw)
    assert any(e["field"] == "entries" and e["code"] == "too_many_entries" for e in errors)
    ok = [{"match": f"w{i}", "replace": "x"} for i in range(200)]
    assert pr.validate_entries(ok)[1] == []


def test_validation_rejects_lengths_and_empty_match():
    assert (0, "match", "too_long") in _codes([{"match": "m" * 65, "replace": "x"}])
    assert (0, "replace", "too_long") in _codes([{"match": "m", "replace": "r" * 129}])
    assert (0, "match", "empty") in _codes([{"match": "   ", "replace": "x"}])


def test_validation_rejects_case_insensitive_duplicates():
    codes = _codes([{"match": "OBS", "replace": "a"}, {"match": "obs", "replace": "b"}])
    assert (1, "match", "duplicate") in codes


@pytest.mark.parametrize("bad", [".", "!", "?", "…", "\n", "<", ">", "*"])
def test_validation_rejects_forbidden_chars_in_replace(bad):
    assert (0, "replace", "forbidden_char") in _codes(
        [{"match": "x", "replace": f"a{bad}b"}]
    )


def test_validation_rejects_wrong_types():
    assert (0, "match", "not_string") in _codes([{"match": 5, "replace": "x"}])
    assert (0, "entries", "not_object") in _codes(["nope"])
    assert (0, "entries", "not_list") in {
        (e["index"], e["field"], e["code"]) for e in pr.validate_entries("x")[1]
    }


def test_set_entries_invalid_keeps_previous_dictionary():
    _set(("OBS", "o be ese"))
    errors = pr.set_entries([{"match": "", "replace": "x"}])
    assert errors
    assert pr.apply_pronunciation("OBS") == "o be ese"


# --- persistence ----------------------------------------------------------


def test_save_then_load_roundtrip_and_rebuild(tmp_path):
    assert pr.save_pronunciation([{"match": "OBS", "replace": "o be ese"}]) == []
    pr.reset_pronunciation()
    assert pr.apply_pronunciation("OBS") == "OBS"
    assert pr.load_pronunciation() == 1
    assert pr.apply_pronunciation("OBS") == "o be ese"


def test_save_returns_none_on_write_failure_and_keeps_memory(monkeypatch):
    _set(("OBS", "o be ese"))

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(pr.os, "replace", boom)
    assert pr.save_pronunciation([{"match": "GG", "replace": "bien jugado"}]) is None
    assert pr.apply_pronunciation("OBS GG") == "o be ese GG"


@pytest.mark.parametrize(
    "content",
    [None, "{not json", json.dumps([1, 2]), json.dumps({"entries": "x"}),
     json.dumps({"entries": [{"match": "", "replace": "x"}]})],
)
def test_load_fails_open_to_empty(tmp_path, content):
    path = tmp_path / "pronunciation.json"
    if content is not None:
        path.write_text(content, encoding="utf-8")
    _set(("OBS", "o be ese"))
    assert pr.load_pronunciation() == 0
    assert pr.apply_pronunciation("OBS") == "OBS"


def test_load_and_save_never_log_entry_text(tmp_path, caplog):
    caplog.set_level("DEBUG")
    path = tmp_path / "pronunciation.json"
    path.write_text("{corrupt SECRETWORD", encoding="utf-8")
    pr.load_pronunciation()
    pr.save_pronunciation([{"match": "SECRETWORD", "replace": "hidden"}])
    pr.load_pronunciation()
    assert "SECRETWORD" not in caplog.text and "hidden" not in caplog.text


# --- sanitizer integration -----------------------------------------------


def test_sanitizer_applies_dictionary_last():
    _set(("OBS", "o be ese"))
    assert _sanitize_tts_text_for_playback("Abre **OBS** ya") == "Abre o be ese ya"


def test_sanitizer_unchanged_when_dictionary_empty():
    assert _sanitize_tts_text_for_playback("Abre **OBS** ya") == "Abre OBS ya"


def test_control_blocks_in_input_still_dropped():
    _set(("OBS", "o be ese"))
    out = _sanitize_tts_text_for_playback("<think>OBS secret</think>OBS hola")
    assert "secret" not in out and out == "o be ese hola"


def test_replacement_cannot_reintroduce_special_token():
    _set(("zzz", "[INST]"))
    out = _sanitize_tts_text_for_playback("hello zzz world")
    assert "[INST]" not in out


def test_guard_sees_post_dictionary_text():
    from opencohost.core.context.generation_evaluator import output_guard_with_tts_check

    def fake_guard(text, source=""):
        return ("forbidden phrase" not in text, "[test_rule] blocked")

    _set(("safe", "forbidden phrase"))
    allowed, reason = output_guard_with_tts_check("this is safe", "direct", fake_guard)
    assert allowed is False and reason.startswith("[tts-sanitized]")
    pr.reset_pronunciation()
    assert output_guard_with_tts_check("this is safe", "direct", fake_guard) == (True, "")


def test_batch_split_path_keeps_display_text_and_speaks_dictionary():
    # follow-reading-pronunciation R1: fragments are display text; the
    # dictionary is applied per fragment at synthesis time only.
    from opencohost.core.engine.llm_engine_speech import SpeechPipelineMixin

    _set(("OBS", "o be ese"))
    pieces = SpeechPipelineMixin._split_for_tts("Abre OBS ahora. Luego sigue.")
    assert pieces == ["Abre OBS ahora.", "Luego sigue."]
    spoken = [SpeechPipelineMixin._spoken_text_for_tts(p) for p in pieces]
    assert spoken == ["Abre o be ese ahora.", "Luego sigue."]


def test_streaming_sentence_path_keeps_display_text_and_speaks_dictionary():
    from opencohost.core.engine.llm_engine_speech import SpeechPipelineMixin
    from opencohost.core.speech.sentence_splitter import SentenceSplitter

    _set(("OBS", "o be ese"))
    splitter = SentenceSplitter()
    sentences = list(splitter.feed("Abre OBS ahora. ")) + list(splitter.flush())
    assert sentences
    pieces = []
    for s in sentences:
        pieces.extend(SpeechPipelineMixin._sanitize_for_tts(s))
    assert any("OBS" in p for p in pieces)
    spoken = [SpeechPipelineMixin._spoken_text_for_tts(p) for p in pieces]
    assert any("o be ese" in p for p in spoken)
    assert not any("OBS" in p for p in spoken)


# --- performance ---------------------------------------------------------


def test_200_entries_on_long_text_is_fast():
    entries = [{"match": f"term{i}", "replace": f"say{i}"} for i in range(200)]
    assert pr.set_entries(entries) == []
    text = ("hola term7 que tal term199 y mas palabras de relleno " * 80)[:4000]
    pr.apply_pronunciation(text)  # warm
    start = time.perf_counter()
    out = pr.apply_pronunciation(text)
    elapsed = time.perf_counter() - start
    assert "say7" in out and "say199" in out
    assert elapsed < 0.05
