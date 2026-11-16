"""Adverse-path gate for LLM output streaming, driven against a fake Ollama.

Open gate (LLM output streaming, ``llm_output_streaming_20260813``):
force a stall mid-stream and confirm that

1. the watchdog fires within its budget,
2. the already-spoken prefix is committed and the unspoken tail is dropped,
3. the client closes the socket (the server observes the disconnect), and
4. the turn queue continues: the next turn completes normally.

These tests run the REAL streaming transport: ``MotorVocalIA`` wired the way
``MotorVocalIA.run()`` wires it (the real ``ollama`` module, memoized
``ollama.Client`` instances keyed by timeout), the router flag armed the way
``EngineHost`` arms it, the real ``GenerationOrchestrator`` streaming loop and
the real ``httpx`` socket. Only two things are fakes: the Ollama server
(``tests/fakes/fake_ollama.py``, loopback, ephemeral port) and the speech
router (records submits, appends and seals; no audio).

``STREAM_IDLE_TIMEOUT_SECONDS`` is shortened from 40 s to ``IDLE_BUDGET_S`` so
the suite stays fast. The idle budget is enforced by the httpx read timeout
of the client memoized for that budget, which is exactly what production does.

What this does NOT prove: that a real Ollama runner frees its slot when the
client closes a STREAMING socket. The server-side disconnect observed here is
the client half of that contract. The Ollama half still needs one live check
(watch the runner free up in the Ollama log after a forced mid-stream abort).
"""

from __future__ import annotations

import logging
import queue
import threading
import time

import pytest

from opencohost.core import llm_engine
from opencohost.core.engine import llm_engine_models, llm_inference_service
from tests.fakes.fake_ollama import ChatScript, FakeOllamaServer

IDLE_BUDGET_S = 0.5
# Upper slack for "fired within its budget": scheduling and select polling on a
# loaded Windows runner. The real budget is 40 s, so this is a tight bound.
BUDGET_SLACK_S = 1.5
# Bound on any single turn; a hang fails the test instead of hanging the suite.
TURN_TIMEOUT_S = 15.0

FAILED_MODEL = "qwopus"
GOOD_MODEL = "llama3"


class RecordingRouter:
    """Speech-router stand-in: the streaming API surface plus ``submit``."""

    def __init__(self):
        self.jobs = []
        self.appends = []
        self.sealed = []
        self.submits = []
        self._lock = threading.Lock()

    def submit_streaming(self, source, priority, **_kwargs):
        with self._lock:
            job = {"id": len(self.jobs) + 1, "source": source, "sentences": []}
            self.jobs.append(job)
            return job

    def append_chunks(self, job, chunks):
        with self._lock:
            job["sentences"].extend(chunks)
            self.appends.append((job["id"], list(chunks)))
            return True

    def seal(self, job):
        with self._lock:
            self.sealed.append(job["id"])

    def submit(self, text, source, priority):
        with self._lock:
            self.submits.append((source, priority))

    def has_work(self):
        # No playback: audio "finishes" the instant it is handed over, so the
        # engine is never speech-busy and the queue may pop the next turn.
        return False


@pytest.fixture
def fake_ollama():
    server = FakeOllamaServer(models=(FAILED_MODEL, GOOD_MODEL))
    server.start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def engine(fake_ollama, monkeypatch):
    import ollama

    # Every Ollama path points at the fake: new clients via OLLAMA_HOST, and the
    # module-level helpers (bound at import to the default host) re-bound, so no
    # code path can reach a real daemon on 11434.
    monkeypatch.setenv("OLLAMA_HOST", fake_ollama.base_url)
    bound = ollama.Client(host=fake_ollama.base_url, timeout=5.0)
    for name in ("chat", "generate", "show", "ps", "list", "embed", "embeddings"):
        if hasattr(ollama, name):
            monkeypatch.setattr(ollama, name, getattr(bound, name))

    monkeypatch.setattr(llm_engine_models, "STREAM_IDLE_TIMEOUT_SECONDS", IDLE_BUDGET_S)
    monkeypatch.setattr(llm_inference_service, "STREAM_IDLE_TIMEOUT_SECONDS", IDLE_BUDGET_S)
    monkeypatch.setattr(llm_engine, "LLM_STREAMING_ENABLED", True)
    # Rollback persists the model it lands on; keep that out of the repo.
    monkeypatch.setattr(llm_engine_models, "_save_last_model_compat", lambda *a, **k: None)

    ui_events = []
    motor = llm_engine.MotorVocalIA(queue.Queue(), ui_events.append)
    # Same wiring as MotorVocalIA.run(), minus pygame.
    motor.ollama = ollama
    motor._ollama_chat_client = motor._create_ollama_chat_client(ollama)
    motor._create_ollama_chat_client(ollama, timeout=motor._post_switch_watchdog_timeout)
    motor.is_ready = True
    motor.use_system_role = True
    motor.current_model = FAILED_MODEL
    motor._desired_model = FAILED_MODEL
    motor._loaded_model = FAILED_MODEL
    motor._warmed_model = FAILED_MODEL
    motor._last_known_good_model = GOOD_MODEL
    # Armed by EngineHost on the product surface; streaming gates on it.
    motor._speech_router_enabled = True
    router = RecordingRouter()
    motor._ensure_router = lambda: router
    motor._speech_router = router
    motor._test_router = router
    motor._test_ui_events = ui_events
    return motor


def _run_bounded(fn, *, timeout=TURN_TIMEOUT_S):
    """Run *fn* on a daemon thread; fail (never hang) if it outlives *timeout*."""
    box = {}

    def target():
        try:
            box["result"] = fn()
        except BaseException as exc:  # surfaced to the test below
            box["error"] = exc

    worker = threading.Thread(target=target, daemon=True, name="adverse-path-turn")
    started = time.monotonic()
    worker.start()
    worker.join(timeout)
    elapsed = time.monotonic() - started
    assert not worker.is_alive(), f"turn still running after {timeout}s (hang)"
    if "error" in box:
        raise box["error"]
    return box.get("result"), elapsed


def _turn(engine, text="hola"):
    return _run_bounded(
        lambda: engine._generar_dialogo(text, source="direct", commit_history=True)
    )


def _messages(caplog, tag):
    return [r.getMessage() for r in caplog.records if tag in r.getMessage()]


SPOKEN = ["Primera frase limpia. ", "Segunda frase tranquila. "]
TAIL = ["Esta cola nunca se dice. "]


# ------------------------------------------------------------ the open gate


def test_stall_mid_stream_fires_watchdog_commits_prefix_frees_socket_and_queue_continues(
    engine, fake_ollama, caplog
):
    fake_ollama.script_chat(ChatScript(chunks=SPOKEN + TAIL, mode="stall", after=2))
    fake_ollama.script_chat(ChatScript(chunks=["Siguiente turno completo. ", "Sin drama."]))
    router = engine._test_router

    with caplog.at_level(logging.INFO, logger="OpenCohost"):
        engine.enqueue("primera pregunta", priority=0, source="direct")
        _run_bounded(engine._process_priority_queue)
        stalled = fake_ollama.wait_finished(0)
        history_after_stall = [turn["content"] for turn in engine.historial]
        recovered_events = list(engine._test_ui_events)
        failure = dict(engine._last_llm_failure or {})

        # (4) the queue continues: the next queued turn runs on the same engine.
        engine.enqueue("segunda pregunta", priority=0, source="direct")
        _run_bounded(engine._process_priority_queue)
        follow_up = fake_ollama.wait_finished(1)

    # (1) the watchdog fired within its (idle) budget: stall start -> client gave up.
    assert stalled.stalled_at is not None
    assert stalled.disconnected_at is not None, "client never closed the stalled socket"
    waited = stalled.disconnected_at - stalled.stalled_at
    assert IDLE_BUDGET_S * 0.9 <= waited <= IDLE_BUDGET_S + BUDGET_SLACK_S, waited
    assert failure.get("reason") == "watchdog_timeout"
    assert "llm_timeout_recovered" in recovered_events
    assert len(_messages(caplog, "Inference watchdog timeout")) == 1
    partial = _messages(caplog, "[STREAM_PARTIAL_EXIT]")
    assert len(partial) == 1 and "reason=TimeoutError" in partial[0]
    assert "source=direct" in partial[0] and "committed=True" in partial[0]
    # The idle stall is logged once, at the detection site, with its kind and budget.
    watchdog = _messages(caplog, "[STREAM_WATCHDOG]")
    assert len(watchdog) == 1
    assert "watchdog_timeout:0.50s" in watchdog[0] and "kind=idle" in watchdog[0]

    # (2) the spoken prefix is committed; the unspoken tail never reached the router.
    first_job = router.jobs[0]
    assert first_job["sentences"] == ["Primera frase limpia.", "Segunda frase tranquila."]
    assert first_job["id"] in router.sealed
    assert stalled.chunks_sent == 2
    assert history_after_stall[-1] == "Primera frase limpia. Segunda frase tranquila."
    assert not any("cola nunca" in content for content in history_after_stall)

    # (3) the server saw the client close the socket (proxy for "runner freed").
    assert stalled.client_closed is True

    # The rollback ran against the server only after the socket closed: warm
    # the last known-good model, unload the stalled one.
    generates = fake_ollama.generate_calls
    assert any(g["model"] == GOOD_MODEL and g["warm"] for g in generates)
    assert any(g["model"] == FAILED_MODEL and g["keep_alive"] == 0 for g in generates)
    assert min(g["at"] for g in generates) >= stalled.disconnected_at
    assert engine.current_model == GOOD_MODEL
    # The owner hears the spoken timeout notice on the owner band.
    assert any(src.startswith("direct") for src, _prio in router.submits)

    # (4) the next turn streamed to completion on the rolled-back model.
    assert follow_up.model == GOOD_MODEL
    assert follow_up.completed is True
    second_job = router.jobs[-1]
    assert second_job["sentences"] == ["Siguiente turno completo.", "Sin drama."]
    assert second_job["id"] in router.sealed
    assert engine.historial[-1]["content"] == "Siguiente turno completo. Sin drama."
    assert len(_messages(caplog, "[STREAM_DONE]")) == 1
    assert len(engine._priority_queue) == 0
    assert engine._processing is False
    # The turn path and the rollback only use endpoints the fake implements.
    assert set(fake_ollama.paths) <= {"/api/chat", "/api/generate", "/api/show", "/api/tags", "/api/ps"}


def test_stalled_owner_bundle_requeues_its_follower_and_the_queue_answers_it(
    engine, fake_ollama, caplog
):
    """Two queued owner questions are bundled into one turn (phase 2, §10). The
    bundle stalls after speaking; its follower is requeued and answered next."""
    fake_ollama.script_chat(ChatScript(chunks=SPOKEN + TAIL, mode="stall", after=2))
    fake_ollama.script_chat(ChatScript(chunks=["Respondo la otra. "]))
    engine.enqueue("primera pregunta", priority=0, source="direct")
    engine.enqueue("segunda pregunta", priority=0, source="direct")

    with caplog.at_level(logging.INFO, logger="OpenCohost"):
        _run_bounded(engine._process_priority_queue, timeout=2 * TURN_TIMEOUT_S)

    stalled, follow_up = fake_ollama.wait_finished(0), fake_ollama.wait_finished(1)
    assert stalled.client_closed is True
    partial = _messages(caplog, "[STREAM_PARTIAL_EXIT]")
    assert len(partial) == 1 and "source=owner-bundle" in partial[0]
    router = engine._test_router
    assert router.jobs[0]["source"] == "owner-bundle"
    assert router.jobs[0]["id"] in router.sealed
    assert follow_up.completed is True
    assert router.jobs[-1]["sentences"] == ["Respondo la otra."]
    assert len(fake_ollama.chat_calls) == 2
    assert len(engine._priority_queue) == 0
    assert engine._processing is False


def test_hang_before_first_byte_takes_legacy_rollback_with_no_job(engine, fake_ollama, caplog):
    fake_ollama.script_chat(ChatScript(chunks=SPOKEN, mode="hang"))

    with caplog.at_level(logging.INFO, logger="OpenCohost"):
        dialogo, _elapsed = _turn(engine)

    call = fake_ollama.wait_finished(0)
    assert dialogo == ""
    assert call.first_byte_at is None
    waited = call.disconnected_at - call.received_at
    assert IDLE_BUDGET_S * 0.9 <= waited <= IDLE_BUDGET_S + BUDGET_SLACK_S, waited
    assert call.client_closed is True
    assert engine._test_router.jobs == []
    assert list(engine.historial) == []
    assert engine._last_llm_failure["reason"] == "watchdog_timeout"
    assert engine.current_model == GOOD_MODEL
    assert not _messages(caplog, "[STREAM_PARTIAL_EXIT]")
    # Pre-speech idle stalls are logged too: the line does not depend on a partial exit.
    watchdog = _messages(caplog, "[STREAM_WATCHDOG]")
    assert len(watchdog) == 1
    assert "watchdog_timeout:0.50s" in watchdog[0] and "kind=idle" in watchdog[0]


# --------------------------------------------------------------- slow chunks


def test_slow_chunks_under_the_idle_budget_are_not_a_stall(engine, fake_ollama):
    fake_ollama.script_chat(
        ChatScript(chunks=SPOKEN, mode="slow", delay_s=IDLE_BUDGET_S * 0.4)
    )

    dialogo, _elapsed = _turn(engine)

    assert dialogo == "Primera frase limpia. Segunda frase tranquila."
    assert fake_ollama.wait_finished(0).completed is True
    assert engine.current_model == FAILED_MODEL
    assert "llm_timeout_recovered" not in engine._test_ui_events


def test_slow_chunks_past_the_total_cap_fire_the_total_watchdog(engine, fake_ollama, monkeypatch, caplog):
    total_cap = 1.0
    monkeypatch.setattr(engine, "_resolve_chat_watchdog_timeout", lambda *a, **k: total_cap)
    chunks = [f"Frase numero {i}. " for i in range(12)]
    fake_ollama.script_chat(ChatScript(chunks=chunks, mode="slow", delay_s=0.2))

    with caplog.at_level(logging.INFO, logger="OpenCohost"):
        dialogo, _elapsed = _turn(engine)

    call = fake_ollama.wait_finished(0)
    assert dialogo == ""
    assert call.client_closed is True
    assert 1 <= call.chunks_sent < len(chunks)
    job = engine._test_router.jobs[0]
    assert job["id"] in engine._test_router.sealed
    assert engine.historial[-1]["content"] == " ".join(job["sentences"])
    assert engine._last_llm_failure["reason"] == "watchdog_timeout"
    partial = _messages(caplog, "[STREAM_PARTIAL_EXIT]")
    assert len(partial) == 1 and "reason=TimeoutError" in partial[0]
    watchdog = _messages(caplog, "[STREAM_WATCHDOG]")
    assert len(watchdog) == 1
    assert "watchdog_timeout:1.00s" in watchdog[0] and "kind=total" in watchdog[0]


# ----------------------------------------------------- cut, HTTP 500, error


def test_connection_cut_mid_stream_commits_prefix_without_rollback(engine, fake_ollama, caplog):
    fake_ollama.script_chat(ChatScript(chunks=SPOKEN + TAIL, mode="cut", after=2))

    with caplog.at_level(logging.INFO, logger="OpenCohost"):
        dialogo, _elapsed = _turn(engine)

    assert dialogo == ""
    job = engine._test_router.jobs[0]
    assert job["sentences"] == ["Primera frase limpia.", "Segunda frase tranquila."]
    assert job["id"] in engine._test_router.sealed
    assert engine.historial[-1]["content"] == "Primera frase limpia. Segunda frase tranquila."
    partial = _messages(caplog, "[STREAM_PARTIAL_EXIT]")
    assert len(partial) == 1
    # A cut is not a stall: no rollback, the model stays.
    assert engine.current_model == FAILED_MODEL
    assert "llm_timeout_recovered" not in engine._test_ui_events
    assert fake_ollama.generate_calls == []


def test_connection_cut_before_any_sentence_is_non_committing(engine, fake_ollama):
    fake_ollama.script_chat(ChatScript(chunks=["Sin cierre de frase"], mode="cut", after=1))

    dialogo, _elapsed = _turn(engine)

    assert dialogo == ""
    assert engine._test_router.jobs == []
    assert list(engine.historial) == []
    assert engine.current_model == FAILED_MODEL


def test_http_500_is_non_committing_and_does_not_roll_back(engine, fake_ollama):
    fake_ollama.script_chat(ChatScript(chunks=SPOKEN, mode="http_500"))

    dialogo, _elapsed = _turn(engine)

    assert dialogo == ""
    assert fake_ollama.wait_finished(0).status == 500
    assert engine._test_router.jobs == []
    assert list(engine.historial) == []
    assert engine.current_model == FAILED_MODEL
    assert "llm_timeout_recovered" not in engine._test_ui_events


def test_error_line_mid_stream_commits_prefix(engine, fake_ollama, caplog):
    fake_ollama.script_chat(ChatScript(chunks=SPOKEN + TAIL, mode="error_line", after=2))

    with caplog.at_level(logging.INFO, logger="OpenCohost"):
        dialogo, _elapsed = _turn(engine)

    assert dialogo == ""
    job = engine._test_router.jobs[0]
    assert job["sentences"] == ["Primera frase limpia.", "Segunda frase tranquila."]
    assert job["id"] in engine._test_router.sealed
    assert engine.historial[-1]["content"] == "Primera frase limpia. Segunda frase tranquila."
    assert len(_messages(caplog, "[STREAM_PARTIAL_EXIT]")) == 1
    assert engine.current_model == FAILED_MODEL


def test_after_a_failed_turn_the_next_turn_completes(engine, fake_ollama):
    fake_ollama.script_chat(ChatScript(chunks=SPOKEN, mode="http_500"))
    fake_ollama.script_chat(ChatScript(chunks=["Vuelvo a estar. "]))

    _turn(engine, "uno")
    dialogo, _elapsed = _turn(engine, "dos")

    assert dialogo == "Vuelvo a estar."
    assert fake_ollama.wait_finished(1).completed is True
