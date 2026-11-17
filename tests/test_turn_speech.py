"""Speech outcomes tied to the turn (turn-state-machine-20261112 T3).

A foreground turn stamps its tid on the ``SpeechJob`` it submits (``turn_uid``,
distinct from the vision/feed ``turn_id``); the router logs
``[TURN_SPEECH] tid= job= state= reason=`` from its existing post-lock log
sites. Jobs without a turn (agenda, pregen background, notices) log nothing.

Harness: real ``MotorVocalIA`` + REAL router thread + real ``_hablar_impl``
(only synthesis and the mixer are faked), as in tests/test_speech_router_stack.py.
"""
from __future__ import annotations

import logging
import re
import threading

import pytest

from opencohost.core.engine import turn_state
from opencohost.core.speech.router import SpeechJob, SpeechJobState, SpeechRouter
from tests.test_speech_router_stack import (  # noqa: F401  (fixtures + helpers)
    _GateMixerMusic,
    _armed,
    _job_text,
    _wait_until,
    router_motors,
)

LINE_RE = re.compile(
    r"^\[TURN_SPEECH\] tid=(?P<tid>t[0-9a-f]{8}) job=(?P<job>\d+|-) "
    r"state=(?P<state>started|finished|discarded|suspended|resumed|lost) "
    r"reason=(?P<reason>[a-z_]+)$"
)


def _lines(caplog):
    out = []
    for record in caplog.records:
        message = record.getMessage()
        if message.startswith("[TURN_SPEECH]"):
            match = LINE_RE.match(message)
            assert match, message
            out.append(match.groupdict())
    return out


def _states(lines):
    return [(line["state"], line["reason"]) for line in lines]


@pytest.fixture(autouse=True)
def _no_leaked_turn():
    turn_state.close_turn()
    yield
    turn_state.close_turn()


def _open_turn():
    turn = turn_state.open_turn("direct")
    assert turn is not None
    return turn.turn_id


class _OwnerLock:
    """A leaf lock that remembers which thread holds it."""

    def __init__(self):
        self._inner = threading.Lock()
        self.owner = None

    def acquire(self, *a, **k):
        got = self._inner.acquire(*a, **k)
        if got:
            self.owner = threading.get_ident()
        return got

    def release(self):
        self.owner = None
        self._inner.release()

    def locked(self):
        return self._inner.locked()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()


def test_stamped_job_logs_started_then_finished(router_motors, caplog):
    motor, rec, _ = _armed(router_motors)
    with caplog.at_level(logging.INFO, logger="OpenCohost"):
        tid = _open_turn()
        job = motor._speak_or_submit(_job_text("A", 2), source="direct")
        assert rec.wait_for("speaking_end", 1, timeout=10.0), rec.names()
    lines = _lines(caplog)

    assert job.turn_uid == tid
    assert _states(lines) == [("started", "activated"), ("finished", "complete")]
    assert {line["tid"] for line in lines} == {tid}
    assert {line["job"] for line in lines} == {str(job.job_id)}


def test_job_without_a_turn_logs_nothing(router_motors, caplog):
    motor, rec, _ = _armed(router_motors)
    with caplog.at_level(logging.INFO, logger="OpenCohost"):
        job = motor._speak_or_submit(_job_text("A", 2), source="kira-agenda:t1")
        assert rec.wait_for("speaking_end", 1, timeout=10.0), rec.names()

    assert job.turn_uid is None
    assert _lines(caplog) == []


def test_turn_id_is_untouched_by_the_stamp(router_motors):
    motor, rec, _ = _armed(router_motors)
    _open_turn()
    motor._vision_read_turn_id = 7
    job = motor._speak_or_submit(_job_text("A", 1), source="direct")
    assert rec.wait_for("speaking_end", 1, timeout=10.0), rec.names()

    assert job.turn_id == 7
    assert job.turn_uid is not None and job.turn_uid != job.turn_id


def test_cut_active_job_logs_discarded_with_reason(router_motors, caplog):
    mixer = _GateMixerMusic(block_at=(0,))
    motor, rec, _ = _armed(router_motors, mixer_music=mixer, interrupt_enabled=True)
    with caplog.at_level(logging.INFO, logger="OpenCohost"):
        tid = _open_turn()
        job = motor._speak_or_submit(_job_text("N", 2), source="direct")
        assert mixer.entered[0].wait(5.0)
        router = motor._speech_router
        assert _wait_until(lambda: router._active is job)
        router.discard_job(job)
        assert rec.wait_for("speaking_end", 1, timeout=10.0), rec.names()
    lines = _lines(caplog)

    assert _states(lines) == [("started", "activated"), ("discarded", "interrupted")]
    assert {line["tid"] for line in lines} == {tid}


def test_discarding_a_suspended_stack_entry_logs_discarded(router_motors, caplog):
    mixer = _GateMixerMusic(block_at=(0, 1))
    motor, rec, _ = _armed(router_motors, mixer_music=mixer, interrupt_enabled=True)
    with caplog.at_level(logging.INFO, logger="OpenCohost"):
        tid = _open_turn()
        notice = motor._speak_or_submit(_job_text("N", 2), source="direct")
        assert mixer.entered[0].wait(5.0)
        turn_state.close_turn()  # the preempting job is a different, unstamped submit
        motor._speak_or_submit(_job_text("R", 1), source="direct")
        assert mixer.entered[1].wait(5.0)
        router = motor._speech_router
        assert _wait_until(lambda: len(router._stack) == 1)
        router.discard_job(notice)
        mixer.release(1)
        assert rec.wait_for("speaking_end", 1, timeout=10.0), rec.names()
    lines = _lines(caplog)

    assert _states(lines) == [
        ("started", "activated"),
        ("suspended", "preempt"),
        ("discarded", "discard_stale"),
    ]
    assert {line["tid"] for line in lines} == {tid}


def test_ptt_suspend_and_resume_are_logged(router_motors, caplog):
    mixer = _GateMixerMusic(block_at=(1,))
    motor, rec, _ = _armed(router_motors, mixer_music=mixer, interrupt_enabled=True)
    with caplog.at_level(logging.INFO, logger="OpenCohost"):
        tid = _open_turn()
        motor._speak_or_submit(_job_text("A", 3), source="direct")
        assert mixer.entered[1].wait(5.0)
        motor.pause_speech_for_ptt()
        router = motor._speech_router
        assert _wait_until(lambda: len(router._stack) == 1)
        motor.resume_speech_after_ptt()
        assert rec.wait_for("speaking_end", 1, timeout=10.0), rec.names()
    lines = _lines(caplog)

    assert _states(lines) == [
        ("started", "activated"),
        ("suspended", "ptt"),
        ("resumed", "resume"),
        ("finished", "complete"),
    ]
    assert {line["tid"] for line in lines} == {tid}


def test_sweep_of_a_stacked_job_logs_discarded(router_motors, caplog):
    mixer = _GateMixerMusic(block_at=(0, 1))
    motor, rec, _ = _armed(router_motors, mixer_music=mixer, interrupt_enabled=True)
    with caplog.at_level(logging.INFO, logger="OpenCohost"):
        tid = _open_turn()
        motor._speak_or_submit(_job_text("N", 2), source="direct")
        assert mixer.entered[0].wait(5.0)
        turn_state.close_turn()
        motor._speak_or_submit(_job_text("R", 1), source="kira-agenda:x", priority=0)
        assert mixer.entered[1].wait(5.0)
        router = motor._speech_router
        assert _wait_until(lambda: len(router._stack) == 1)
        router.sweep_sources(("direct",))
        mixer.release(1)
        assert rec.wait_for("speaking_end", 1, timeout=10.0), rec.names()
    lines = _lines(caplog)

    assert ("discarded", "cancelled") in _states(lines)
    assert {line["tid"] for line in lines} == {tid}


def test_non_agenda_residue_sweep_logs_discarded(router_motors, caplog):
    mixer = _GateMixerMusic(block_at=(0, 1))
    motor, rec, _ = _armed(router_motors, mixer_music=mixer, interrupt_enabled=True)
    with caplog.at_level(logging.INFO, logger="OpenCohost"):
        tid = _open_turn()
        motor._speak_or_submit(_job_text("N", 2), source="direct")
        assert mixer.entered[0].wait(5.0)
        turn_state.close_turn()
        motor._speak_or_submit(_job_text("R", 1), source="kira-agenda:x", priority=0)
        assert mixer.entered[1].wait(5.0)
        router = motor._speech_router
        assert _wait_until(lambda: len(router._stack) == 1)
        router._sweep_non_agenda_stack()
        mixer.release(1)
        assert rec.wait_for("speaking_end", 1, timeout=10.0), rec.names()
    lines = _lines(caplog)

    assert ("discarded", "discard_residue") in _states(lines)
    assert {line["tid"] for line in lines} == {tid}


def test_release_unwind_logs_discarded():
    class _Motor:
        def _speech_boundary_end(self):
            pass

    router = SpeechRouter(_Motor())
    job = SpeechJob(job_id=3, text="x", source="direct", priority=0, turn_uid="tdeadbeef")
    records = []

    class _H(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    handler = _H(level=logging.INFO)
    log = logging.getLogger("OpenCohost")
    log.addHandler(handler)
    old = log.level
    log.setLevel(logging.INFO)
    try:
        router._release(job, SpeechJobState.DISCARDED)
    finally:
        log.removeHandler(handler)
        log.setLevel(old)

    assert records == ["[TURN_SPEECH] tid=tdeadbeef job=3 state=discarded reason=unwind"]


def test_error_discard_logs_lost(router_motors, caplog):
    motor, rec, _ = _armed(router_motors)

    def boom(*_a, **_k):
        raise RuntimeError("tts down")

    motor._hablar_impl = boom
    with caplog.at_level(logging.INFO, logger="OpenCohost"):
        tid = _open_turn()
        job = motor._speak_or_submit(_job_text("A", 2), source="direct")
        assert _wait_until(lambda: job.state == SpeechJobState.DISCARDED, timeout=10.0)
        assert _wait_until(lambda: any(s == "lost" for s, _r in _states(_lines(caplog))))
    lines = _lines(caplog)

    assert ("lost", "error") in _states(lines)
    assert {line["tid"] for line in lines} == {tid}


def test_no_turn_speech_line_is_emitted_under_the_sched_lock(router_motors, caplog):
    mixer = _GateMixerMusic(block_at=(1,))
    motor, rec, _ = _armed(router_motors, mixer_music=mixer, interrupt_enabled=True)
    router = SpeechRouter(motor)
    lock = _OwnerLock()
    router._sched_lock = lock
    motor._speech_router = router
    violations = []

    class _Probe(logging.Handler):
        def emit(self, record):
            if record.getMessage().startswith("[TURN_SPEECH]") and lock.owner == threading.get_ident():
                violations.append(record.getMessage())

    probe = _Probe(level=logging.INFO)
    log = logging.getLogger("OpenCohost")
    log.addHandler(probe)
    try:
        with caplog.at_level(logging.INFO, logger="OpenCohost"):
            _open_turn()
            job = motor._speak_or_submit(_job_text("A", 3), source="direct")
            assert mixer.entered[1].wait(5.0)
            motor.pause_speech_for_ptt()
            assert _wait_until(lambda: len(router._stack) == 1)
            motor.resume_speech_after_ptt()
            assert rec.wait_for("speaking_end", 1, timeout=10.0), rec.names()
            router.discard_job(job)  # no-op on a finished job
    finally:
        log.removeHandler(probe)

    assert len(_lines(caplog)) >= 4
    assert violations == []


def test_router_off_blocking_path_logs_start_and_finish(router_motors, caplog):
    motor, _rec, _ = _armed(router_motors, enabled=False)
    motor._hablar = lambda *a, **k: None
    with caplog.at_level(logging.INFO, logger="OpenCohost"):
        tid = _open_turn()
        result = motor._speak_or_submit("hola", source="direct")
    lines = _lines(caplog)

    assert result is None
    assert [(line["job"], line["state"]) for line in lines] == [("-", "started"), ("-", "finished")]
    assert {line["tid"] for line in lines} == {tid}


def test_router_off_without_a_turn_logs_nothing(router_motors, caplog):
    motor, _rec, _ = _armed(router_motors, enabled=False)
    motor._hablar = lambda *a, **k: None
    with caplog.at_level(logging.INFO, logger="OpenCohost"):
        motor._speak_or_submit("hola", source="kira-agenda:t1")

    assert _lines(caplog) == []


def test_router_off_error_logs_lost_and_still_raises(router_motors, caplog):
    motor, _rec, _ = _armed(router_motors, enabled=False)

    def boom(*_a, **_k):
        raise RuntimeError("tts down")

    motor._hablar = boom
    with caplog.at_level(logging.INFO, logger="OpenCohost"):
        _open_turn()
        with pytest.raises(RuntimeError):
            motor._speak_or_submit("hola", source="direct")

    assert _states(_lines(caplog)) == [("started", "activated"), ("lost", "error")]


# ──────────────────────────────────────────────────────────────────────────
# Engine level: a real foreground turn's tid shows up on its speech lines
# ──────────────────────────────────────────────────────────────────────────

from opencohost.core import llm_engine  # noqa: E402
from tests.fakes.fake_ollama import ChatScript  # noqa: E402
from tests.test_streaming_adverse_path_fake_ollama import (  # noqa: E402,F401  (fixtures)
    SPOKEN,
    _run_bounded,
    engine,
    fake_ollama,
)

TS_RE = re.compile(r"^\[TURN_STATE\] tid=(?P<tid>t[0-9a-f]{8}) ")


def test_foreground_turn_tid_appears_on_its_speech_lines(engine, fake_ollama, caplog):
    router = SpeechRouter(engine)
    engine._ensure_router = lambda: router
    engine._speech_router = router

    def _play(texto, source="direct", **kwargs):
        chunks = list(kwargs.get("pre_split") or [])
        return llm_engine.SpeechOutcome(
            chunks=chunks, cursor=len(chunks), spoken=list(range(len(chunks))),
            skipped=[], interrupted=False, error=None,
        )

    engine._hablar = _play
    router.start()
    fake_ollama.script_chat(ChatScript(chunks=SPOKEN))
    try:
        with caplog.at_level(logging.INFO, logger="OpenCohost"):
            engine.enqueue("hola", priority=0, source="direct")
            _run_bounded(engine._process_priority_queue)
            assert _wait_until(
                lambda: any(s == "finished" for s, _r in _states(_lines(caplog))), timeout=10.0
            ), [r.getMessage() for r in caplog.records if "TURN_" in r.getMessage()]
    finally:
        router.stop(timeout=5.0)

    state_tids = {
        m.group("tid")
        for m in (TS_RE.match(r.getMessage()) for r in caplog.records)
        if m
    }
    lines = _lines(caplog)
    assert len(state_tids) == 1
    assert _states(lines) == [("started", "activated"), ("finished", "complete")]
    assert {line["tid"] for line in lines} == state_tids
    joined = " ".join(r.getMessage() for r in caplog.records if "[TURN_SPEECH]" in r.getMessage())
    assert "Primera" not in joined and "hola" not in joined
