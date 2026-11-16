"""Follow-reading survives the pronunciation dictionary
(follow-reading-pronunciation R1).

The owner dictionary rewrites only the text handed to the TTS synthesizer.
Fragmentation, speech progress, the reply sink and streamed chunk records all
carry the sanitized DISPLAY text, so the UI can find the spoken fragment in
the reply it shows. Fragment boundaries are identical with or without a
dictionary. The output guard still judges the post-dictionary text.
"""
from __future__ import annotations

import pytest

from opencohost.api.engine_host import ChatReplySink
from opencohost.core.engine.llm_engine_speech import SpeechPipelineMixin
from opencohost.core.speech import pronunciation as pr
from opencohost.core.speech.router import PRIORITY_OWNER
from opencohost.core.speech.sentence_splitter import SentenceSplitter
from tests.test_speech_outcome_capture import _make_motor
from tests.test_speech_router import _GateMixerMusic, _ScriptedMixerMusic
from tests.test_speech_router_stack import (  # noqa: F401
    _armed,
    _synth_logger,
    _wait_until,
    router_motors,
)
from tests.test_timeout_notice_voice import ES_LINE, armed  # noqa: F401

REPLY = (
    "Eso no le toca al host fuera de esta ventana. "
    "La IA solo responde aqui, sin tocar nada mas del sistema operativo, "
    "si es un archivo del host, una correccion al nombre o algo mas que haga falta hoy."
)


@pytest.fixture(autouse=True)
def _dictionary(tmp_path, monkeypatch):
    monkeypatch.setattr(pr, "PRONUNCIATION_FILE", str(tmp_path / "pronunciation.json"))
    pr.reset_pronunciation()
    yield
    pr.reset_pronunciation()


def _set_owner_dictionary():
    entries = [
        {"match": "host", "replace": "jostt"},
        {"match": "IA", "replace": "Inteligencia Artificial"},
    ]
    assert pr.set_entries(entries) == []


def _baseline_split(text: str) -> list:
    pr.reset_pronunciation()
    out = SpeechPipelineMixin._split_for_tts(text)
    _set_owner_dictionary()
    return out


# -- fragmentation ------------------------------------------------------------


def test_split_carries_display_text_with_identical_boundaries():
    baseline = _baseline_split(REPLY)
    pieces = SpeechPipelineMixin._split_for_tts(REPLY)
    assert pieces == baseline
    assert any("host" in p for p in pieces)
    assert not any("jostt" in p or "Inteligencia" in p for p in pieces)


def test_streamed_sentence_pieces_carry_display_text():
    _set_owner_dictionary()
    splitter = SentenceSplitter()
    sentences = list(splitter.feed(REPLY + " ")) + list(splitter.flush())
    pieces = [p for s in sentences for p in SpeechPipelineMixin._sanitize_for_tts(s)]
    assert any("host" in p for p in pieces)
    assert not any("jostt" in p for p in pieces)


def test_spoken_text_applies_dictionary_after_safety_stages():
    from opencohost.core.speech.tts_sanitizer import _spoken_text_for_fragment

    _set_owner_dictionary()
    assert _spoken_text_for_fragment("El host usa IA.") == "El jostt usa Inteligencia Artificial."
    pr.reset_pronunciation()
    assert _spoken_text_for_fragment("El host usa IA.") == "El host usa IA."


def test_spoken_text_cannot_reintroduce_special_token():
    from opencohost.core.speech.tts_sanitizer import _spoken_text_for_fragment

    assert pr.set_entries([{"match": "zzz", "replace": "[INST]"}]) == []
    assert "[INST]" not in _spoken_text_for_fragment("hello zzz world")


# -- synthesis seam (buffered / direct) ----------------------------------------


def test_buffered_synthesis_gets_substitutions_and_outcome_keeps_display():
    baseline = _baseline_split(REPLY)
    synth_log: list = []
    motor, _, _ = _make_motor(mixer_music=_ScriptedMixerMusic(), synthesize=_synth_logger(synth_log))

    outcome = motor._hablar_impl(REPLY, source="direct")

    assert outcome.chunks == baseline
    assert len(synth_log) == len(baseline)
    joined = " ".join(synth_log)
    assert "jostt" in joined and "Inteligencia Artificial" in joined
    assert " host" not in joined


def test_fragment_silenced_by_dictionary_is_skipped_not_synthesized():
    synth_log: list = []
    motor, _, _ = _make_motor(mixer_music=_ScriptedMixerMusic(), synthesize=_synth_logger(synth_log))
    assert pr.set_entries([{"match": "Hola amigo", "replace": ""}]) == []

    outcome = motor._hablar_impl("Hola amigo. Segunda frase completa aqui.", source="direct")

    assert outcome.chunks == ["Hola amigo.", "Segunda frase completa aqui."]
    assert synth_log == ["Segunda frase completa aqui."]
    assert outcome.skipped == [0]


# -- speech progress (router) ----------------------------------------------------


def test_progress_fragment_is_display_text_while_audio_uses_dictionary(router_motors):
    baseline = _baseline_split(REPLY)
    synth_log: list = []
    mixer = _GateMixerMusic(block_at=(0,))
    motor, _, _ = _armed(router_motors, mixer_music=mixer, synthesize=_synth_logger(synth_log))

    motor._speak_or_submit(REPLY, source="direct", feed_turn_id=3)
    assert mixer.entered[0].wait(5.0)

    progress = motor.speech_progress()
    assert progress["fragment"] == baseline[0]
    assert "host" in progress["fragment"]
    assert progress["total"] == len(baseline)
    assert "jostt" in synth_log[0]
    mixer.release(0)


def test_readaloud_progress_fragment_is_display_text(router_motors):
    _set_owner_dictionary()
    synth_log: list = []
    mixer = _GateMixerMusic(block_at=(0,))
    motor, _, _ = _armed(router_motors, mixer_music=mixer, synthesize=_synth_logger(synth_log))

    motor.speak_readaloud("El host usa IA ahora mismo.", mode="queue")
    assert mixer.entered[0].wait(5.0)

    assert motor.speech_progress()["fragment"] == "El host usa IA ahora mismo."
    assert synth_log[0] == "El jostt usa Inteligencia Artificial ahora mismo."
    mixer.release(0)


def test_streamed_job_chunks_stay_display_and_audio_uses_dictionary(router_motors):
    _set_owner_dictionary()
    synth_log: list = []
    mixer = _GateMixerMusic(block_at=(0,))
    motor, _, _ = _armed(router_motors, mixer_music=mixer, synthesize=_synth_logger(synth_log))
    router = motor._ensure_router()
    pieces = SpeechPipelineMixin._sanitize_for_tts("Hablemos del host hoy mismo.")
    fragments = SpeechPipelineMixin._fragment_for_tts(pieces)

    job = router.submit_streaming("direct", PRIORITY_OWNER)
    assert router.append_chunks(job, fragments)
    assert mixer.entered[0].wait(5.0)

    assert job.chunks == ["Hablemos del host hoy mismo."]
    assert motor.speech_progress()["fragment"] == "Hablemos del host hoy mismo."
    assert synth_log == ["Hablemos del jostt hoy mismo."]
    router.seal(job)
    mixer.release(0)


# -- reply sink ----------------------------------------------------------------


def test_sink_fragments_match_display_text():
    baseline = _baseline_split(REPLY)
    sink = ChatReplySink()
    turn_id = sink.record(REPLY, "direct")
    fragments = sink.get(turn_id)["fragments"]
    assert fragments == baseline
    assert any("host" in f for f in fragments)


# -- timeout notice ------------------------------------------------------------


def test_timeout_notice_submits_display_text(armed):  # noqa: F811
    motor, router, _ = armed
    assert pr.set_entries([{"match": "respuesta", "replace": "contestacion"}]) == []
    motor._recover_from_stalled_inference(request_model="qwopus", source="direct", timeout=45.0)
    assert router.submit.call_args.args[0] == ES_LINE


# -- guard still judges the spoken text ----------------------------------------


def test_guard_still_sees_post_dictionary_text():
    from opencohost.core.context.generation_evaluator import output_guard_with_tts_check

    def fake_guard(text, source=""):
        return ("forbidden phrase" not in text, "[test_rule] blocked")

    assert pr.set_entries([{"match": "safe", "replace": "forbidden phrase"}]) == []
    allowed, reason = output_guard_with_tts_check("this is safe", "direct", fake_guard)
    assert allowed is False and reason.startswith("[tts-sanitized]")
