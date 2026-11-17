"""Follow-reading contract: speech progress names the FEED turn and fragment
being spoken (live-polish-repetition-follow P3).

Real `MotorVocalIA`, real router thread, real `_hablar_impl` loop; only Piper
and `pygame.mixer` are faked (same harness as tests/test_speech_router_stack).
"""
from __future__ import annotations

import queue
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from opencohost.api.engine_host import ChatReplySink
from opencohost.core.llm_engine import MotorVocalIA
from opencohost.core.speech.router import PRIORITY_OWNER
from tests.test_chat_last_reply import _app
from tests.test_chat_last_reply import _reset_host_active  # noqa: F401
from tests.test_speech_router import _GateMixerMusic
from tests.test_speech_router_stack import (  # noqa: F401
    _armed,
    _job_sentences,
    _job_text,
    _wait_until,
    router_motors,
)


def _progress(motor):
    return motor.speech_progress()


def test_progress_reports_the_spoken_turn_not_the_newest(router_motors):
    mixer = _GateMixerMusic(block_at=(1, 4))
    motor, rec, _ = _armed(router_motors, mixer_music=mixer)

    job_a = motor._speak_or_submit(_job_text("A", 3), source="chat", feed_turn_id=7)
    assert mixer.entered[1].wait(5.0)
    motor._speak_or_submit(_job_text("B", 3), source="chat", feed_turn_id=8)

    p = _progress(motor)
    assert p["feed_turn_id"] == 7, "the queued newer turn must not win"
    assert p["job_id"] == job_a.job_id
    assert p["source"] == "chat"
    assert p["current_idx"] == 1
    assert p["fragment"] == _job_sentences("A", 3)[1]
    assert job_a.feed_turn_id == 7


def test_progress_follows_the_next_job_with_a_new_job_id(router_motors):
    mixer = _GateMixerMusic(block_at=(1, 4))
    motor, rec, _ = _armed(router_motors, mixer_music=mixer)

    job_a = motor._speak_or_submit(_job_text("A", 3), source="chat", feed_turn_id=7)
    assert mixer.entered[1].wait(5.0)
    job_b = motor._speak_or_submit(_job_text("B", 3), source="chat", feed_turn_id=8)
    mixer.release(1)
    assert mixer.entered[4].wait(5.0)

    p = _progress(motor)
    assert p["job_id"] == job_b.job_id and job_b.job_id > job_a.job_id
    assert p["feed_turn_id"] == 8
    assert p["fragment"] == _job_sentences("B", 3)[1]
    mixer.release(4)
    assert rec.wait_for("speaking_end", 2, timeout=10.0)
    assert _wait_until(lambda: _progress(motor) is None)


def test_discard_then_new_job_changes_job_id(router_motors):
    mixer = _GateMixerMusic(block_at=(1, 3))
    motor, rec, _ = _armed(router_motors, mixer_music=mixer, interrupt_enabled=True)

    job_a = motor._speak_or_submit(_job_text("A", 3), source="chat", feed_turn_id=7)
    assert mixer.entered[1].wait(5.0)
    motor._speech_router.discard_job(job_a)
    mixer.release(1)
    job_b = motor._speak_or_submit(_job_text("B", 3), source="chat", feed_turn_id=9)
    assert _wait_until(lambda: (_progress(motor) or {}).get("job_id") == job_b.job_id)
    p = _progress(motor)
    assert p["job_id"] != job_a.job_id
    assert p["feed_turn_id"] == 9
    mixer.release(3)


def test_default_submit_carries_no_feed_turn(router_motors):
    mixer = _GateMixerMusic(block_at=(1,))
    motor, _, _ = _armed(router_motors, mixer_music=mixer)

    job = motor._speak_or_submit(_job_text("S", 3), source="kira-agenda:status")
    assert job.feed_turn_id is None
    assert mixer.entered[1].wait(5.0)
    assert _progress(motor)["feed_turn_id"] is None
    mixer.release(1)


def test_readaloud_job_has_no_feed_turn(router_motors):
    mixer = _GateMixerMusic(block_at=(1,))
    motor, _, _ = _armed(router_motors, mixer_music=mixer)

    job = motor.speak_readaloud(_job_text("R", 3), mode="queue")
    assert job.feed_turn_id is None
    assert mixer.entered[1].wait(5.0)
    p = _progress(motor)
    assert p["feed_turn_id"] is None
    assert p["source"] == motor.READALOUD_SOURCE
    mixer.release(1)


def test_vision_read_streaming_job_has_no_feed_turn(router_motors):
    motor, _, _ = _armed(router_motors)
    router = motor._ensure_router()
    job = router.submit_streaming("direct-read", PRIORITY_OWNER, turn_id=3, protected=True)
    assert job.feed_turn_id is None and job.turn_id == 3
    router.discard_job(job)


def test_streaming_job_feed_turn_is_resolved_live_by_progress(router_motors):
    mixer = _GateMixerMusic(block_at=(0,))
    motor, _, _ = _armed(router_motors, mixer_music=mixer)
    router = motor._ensure_router()
    job = router.submit_streaming("direct", PRIORITY_OWNER)
    frags = _job_sentences("T", 2)
    assert router.append_chunks(job, frags)
    assert mixer.entered[0].wait(5.0)
    # The feed id is recorded AFTER the first chunk is appended: progress must
    # read it live from the job, not from a copy taken at playback start.
    job.feed_turn_id = 11
    p = _progress(motor)
    assert p["feed_turn_id"] == 11 and p["fragment"] == frags[0]
    router.seal(job)
    mixer.release(0)


# -- engine -> sink wiring ---------------------------------------------------


def test_non_streamed_reply_submits_with_its_feed_turn_id():
    sink = ChatReplySink()
    motor = MotorVocalIA(queue.Queue(), lambda event: None, dialogue_callback=sink.record)
    motor.ollama = MagicMock()
    motor.pygame = MagicMock()
    motor.is_ready = True
    motor.current_model = "llama3"
    motor._reasoning_model_cache["llama3"] = False
    motor._speak_or_submit = MagicMock()
    motor._ollama_chat = MagicMock(return_value={"message": {"content": "Che, buena esa."}})

    motor._ejecutar_inferencia("hola", source="direct")

    assert motor._speak_or_submit.call_args.kwargs["feed_turn_id"] == sink.last()["turn_id"]


def test_emit_dialogue_returns_the_recorded_id_only_for_real_ints():
    motor = MotorVocalIA(queue.Queue(), lambda event: None, dialogue_callback=lambda t, s: 5)
    assert motor._emit_dialogue("x", "direct") == 5
    motor.dialogue_callback = MagicMock()
    assert motor._emit_dialogue("x", "direct") is None
    motor.dialogue_callback = None
    assert motor._emit_dialogue("x", "direct") is None


def test_stream_chunk_stamps_the_job_with_the_feed_id():
    sink = ChatReplySink()
    motor = MotorVocalIA(queue.Queue(), lambda event: None, dialogue_callback=sink.record)
    job = MagicMock(job_id=4, chunks=["Hola.", "Que tal."], turn_id=None, feed_turn_id=None)
    state = MagicMock(job=job, source="direct")
    state.display_prefix = lambda: "Hola. Que tal."
    motor._emit_stream_chunk(state)
    assert job.feed_turn_id == sink.last()["turn_id"] != 0


# -- sink history + endpoints --------------------------------------------------


def test_sink_keeps_recent_turns_and_returns_one_by_id():
    sink = ChatReplySink()
    ids = [sink.record(f"Turno {i}. Segunda parte {i}.", "direct") for i in range(25)]
    assert sink.get(ids[-1])["text"] == "Turno 24. Segunda parte 24."
    assert sink.get(ids[-20])["fragments"] == ["Turno 5.", "Segunda parte 5."]
    assert sink.get(ids[0]) is None, "turns older than the window age out"
    assert sink.get(9999) is None


def test_last_reply_by_turn_id_returns_an_older_turn():
    app = _app()
    with TestClient(app) as client:
        sink = app.state.host.chat_sink
        first = sink.record("Primera. Otra.", "direct")
        second = sink.record("Segunda.", "chat")
        body = client.get(f"/api/chat/last-reply?turn_id={first}").json()
        assert body["turn_id"] == first and body["fragments"] == ["Primera.", "Otra."]
        assert client.get("/api/chat/last-reply").json()["turn_id"] == second


def test_last_reply_unknown_turn_id_is_404():
    app = _app()
    with TestClient(app) as client:
        app.state.host.chat_sink.record("Hola.", "direct")
        assert client.get("/api/chat/last-reply?turn_id=999").status_code == 404


def test_speech_progress_endpoint_exposes_new_fields_and_old_clients_still_work():
    app = _app()
    with TestClient(app) as client:
        app.state.host.motor._is_speaking = True
        body = client.get("/api/chat/speech-progress").json()
        assert {"speaking", "turn_id", "current_idx", "played", "total"} <= set(body)
        for key in ("feed_turn_id", "job_id", "source", "fragment"):
            assert body[key] is None
        app.state.host.motor.speech_progress = lambda: {
            "speaking": True, "turn_id": None, "current_idx": 1, "played": 1, "total": 3,
            "feed_turn_id": 4, "job_id": 12, "source": "chat", "fragment": "Hola.",
        }
        body = client.get("/api/chat/speech-progress").json()
        assert (body["feed_turn_id"], body["job_id"], body["source"], body["fragment"]) == (4, 12, "chat", "Hola.")
