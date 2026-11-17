"""Read the transcription aloud in text mode (vision-on-demand V1.32). Synthetic only.

Text mode: after a successful describe the VL transcription is spoken on the
owner band sentence by sentence through the same output guard and TTS
sanitization as Kira's streamed replies, while the chat LLM runs in parallel
with an "already read aloud, only react" instruction. Privacy: logs carry counts
only, never transcription text.
"""
from __future__ import annotations

import logging
import queue
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from opencohost.config import settings
from opencohost.core import llm_engine
from opencohost.core.context.prompt_assembler import PromptContextAssembler
from opencohost.core.engine import llm_engine_vision
from opencohost.core.speech.router import PRIORITY_OWNER, SpeechJob, SpeechJobState
from opencohost.core.vision.capture import CapturedFrame
from opencohost.core.vision.gate import MODE_CLIP, MODE_DEFAULT, MODE_TEXT, VisionDecision
from opencohost.core.vision.sanitize import build_screen_block
from opencohost.core.vision.turn_step import VisionTurnStep
from tests.test_speech_router_stack import _synth_logger, _wait_until
from tests.test_speech_router_streaming import _armed, _stream_chunks, router_motors  # noqa: F401

# B-W4: this module pins the legacy prompt layout (system-message placement, one-pair
# history deque, legacy goldens). The v2 placement is tested in tests/test_prompt_layout_v2.py.
pytestmark = pytest.mark.usefixtures("legacy_prompt_layout")

BLOCKED = "As an AI language model I cannot do that."


# ------------------------------------------------------------ settings


def test_setting_defaults_on():
    assert settings.VISION_READ_ALOUD is True


def test_env_kill_switch_turns_it_off(monkeypatch):
    monkeypatch.setenv("OPENCOHOST_VISION_READ_ALOUD", "0")
    assert settings._resolve_vision_defaults()["VISION_READ_ALOUD"] is False
    monkeypatch.setenv("OPENCOHOST_VISION_READ_ALOUD", "1")
    assert settings._resolve_vision_defaults()["VISION_READ_ALOUD"] is True
    monkeypatch.delenv("OPENCOHOST_VISION_READ_ALOUD")
    assert settings._resolve_vision_defaults()["VISION_READ_ALOUD"] is True


# ------------------------------------------------------------ step result


@dataclass
class _Settings:
    describe_timeout_s: float = 8.0
    frames: int = 1
    frame_interval_s: float = 0.0
    show_thumbnails: bool = False


class _Gate:
    def __init__(self, mode):
        self.d = VisionDecision(True, ("explicit_look",), 0.0, mode)

    def decide(self, text, source):
        return self.d


class _Capture:
    def capture(self, **kwargs):
        return CapturedFrame(
            jpeg=b"\xff\xd8x", width=10, height=10, source_width=10, source_height=10,
            is_black=False, grab_ms=1.0, encode_ms=1.0, signature=b"\x01" * 2304, thumbnail=b"t",
        )


class _Describer:
    def __init__(self, text):
        self.text = text

    def describe(self, jpeg, question, *, timeout_s, cancel=None, **extra):
        return self.text


def _run(mode, text="Primera linea. Segunda linea.", question="mira esto"):
    step = VisionTurnStep(
        _Gate(mode), _Capture(), _Describer(text), lambda line: None, settings=_Settings(),
        sleep=lambda s: None, choice=lambda seq: seq[0],
    )
    return step.run(question, "direct")


def test_text_mode_result_carries_the_transcription():
    result = _run(MODE_TEXT, question="lee eso")
    assert result.outcome == "ok" and result.mode == MODE_TEXT
    assert result.transcription == "Primera linea. Segunda linea."


@pytest.mark.parametrize("mode", [MODE_CLIP, MODE_DEFAULT])
def test_other_modes_carry_no_transcription(mode):
    assert _run(mode).transcription == ""


def test_failed_describe_carries_no_transcription():
    result = _run(MODE_TEXT, text="", question="lee eso")
    assert result.outcome == "describe_empty" and result.transcription == ""


def test_transcription_never_reaches_the_vision_log(caplog):
    with caplog.at_level(logging.INFO, logger="OpenCohost"):
        _run(MODE_TEXT, text="Secreto unico. Otro.", question="lee eso")
    assert all("Secreto" not in r.getMessage() for r in caplog.records)


# ------------------------------------------------------------ router ordering


def test_job_is_not_protected_by_default():
    assert SpeechJob(job_id=1, text="x", source="direct", priority=0).protected is False


def test_protected_active_job_is_not_preempted_by_a_later_owner_submit(router_motors):  # noqa: F811
    motor, _, _ = _armed(router_motors, synthesize=_synth_logger([]), interrupt_enabled=True)
    router = motor._ensure_router()
    reading = router.submit_streaming("direct-vision-read", PRIORITY_OWNER, protected=True)
    router.append_chunks(reading, _stream_chunks("R", 1))
    assert _wait_until(lambda: reading.state is SpeechJobState.ACTIVE)
    pauses: list = []
    router.pause_speech = lambda reason, target=None: pauses.append(reason)
    reaction = router.submit_streaming("direct", PRIORITY_OWNER)
    assert pauses == []
    assert reading.state is SpeechJobState.ACTIVE
    router.seal(reading)
    assert _wait_until(lambda: reading.state is SpeechJobState.FINISHED)
    router.append_chunks(reaction, _stream_chunks("X", 1))
    router.seal(reaction)
    assert _wait_until(lambda: reaction.state is SpeechJobState.FINISHED)
    assert reading.suspensions == 0


def test_unprotected_active_job_is_still_preempted(router_motors):  # noqa: F811
    motor, _, _ = _armed(router_motors, synthesize=_synth_logger([]), interrupt_enabled=True)
    router = motor._ensure_router()
    first = router.submit_streaming("direct", PRIORITY_OWNER)
    router.append_chunks(first, _stream_chunks("A", 1))
    assert _wait_until(lambda: first.state is SpeechJobState.ACTIVE)
    pauses: list = []
    router.pause_speech = lambda reason, target=None: pauses.append(reason)
    router.submit_streaming("direct", PRIORITY_OWNER)
    assert pauses and pauses[0] in ("preempt", "discard")
    router.seal(first)


def test_plain_submit_accepts_protected(router_motors):  # noqa: F811
    motor, _, _ = _armed(router_motors, synthesize=_synth_logger([]), interrupt_enabled=True)
    router = motor._ensure_router()
    job = router.submit("Texto normal de prueba.", "direct-vision-read", PRIORITY_OWNER, protected=True)
    assert job.protected is True


# ------------------------------------------------------------ engine side


class _Job:
    def __init__(self, source, priority, protected=False):
        self.source, self.priority, self.protected = source, priority, protected
        self.text = ""
        self.chunks: list = []
        self.sealed = False
        self.state = SpeechJobState.QUEUED


class _Router:
    def __init__(self, calls):
        self.jobs, self.discarded, self.calls = [], [], calls

    def submit(self, text, source, priority, turn_id=None, protected=False):
        job = _Job(source, priority, protected)
        job.text = text
        self.jobs.append(job)
        self.calls.append("submit")
        return job

    def submit_streaming(self, source, priority, turn_id=None, protected=False):
        job = _Job(source, priority, protected)
        self.jobs.append(job)
        self.calls.append("submit_streaming")
        return job

    def append_chunks(self, job, chunks):
        job.chunks.extend(chunks)
        return True

    def seal(self, job):
        job.sealed = True
        self.calls.append("seal")

    def discard_job(self, job):
        self.discarded.append(job)
        self.calls.append("discard")
        job.state = SpeechJobState.DISCARDED


@pytest.fixture
def motor(monkeypatch):
    m = llm_engine.MotorVocalIA(queue.Queue(), lambda event: None)
    m.ollama = MagicMock()
    m._speech_router_enabled = True
    calls: list = []
    r = _Router(calls)
    m._ensure_router = lambda: r
    m._speech_router = r
    m._r = r
    m._calls = calls
    monkeypatch.setattr(settings, "VISION_READ_ALOUD", True)
    return m


def _step_returning(motor, transcription, *, status_line="Ya vi, dame un segundo.", mode=MODE_TEXT,
                    block="[SCREEN]\nx\n[/SCREEN]"):
    class Step:
        def run(self, text, source, *, cancel=None):
            motor._calls.append("step")
            # a progress line queued while describing, retired when the look ends
            motor._vision_progress_job = _Job("direct-vision-progress", PRIORITY_OWNER)
            return SimpleNamespace(
                screen_block=block, note="", outcome="ok", status_line=status_line,
                reason_codes=(), transcription=transcription, mode=mode,
            )

    motor._vision = SimpleNamespace(step=Step())
    motor._vision_bridge_started = lambda: True
    spoken: list = []
    motor._vision_speak_status = spoken.append
    return spoken


def test_transcription_is_read_on_the_owner_band_sentence_by_sentence(motor):
    spoken = _step_returning(motor, "Primera linea de prueba. Segunda linea de prueba.")
    motor._run_vision_look("lee eso", "ptt")
    (job,) = motor._r.jobs
    assert job.source == "ptt-vision-read" == "ptt" + llm_engine_vision.VISION_READ_SUFFIX
    assert job.priority == PRIORITY_OWNER and job.protected is True and job.sealed is True
    assert job.chunks == ["Primera linea de prueba.", "Segunda linea de prueba."]
    assert spoken == []  # the reading replaces the "got it, one sec" status line


def test_reading_starts_after_the_progress_line_is_retired(motor):
    _step_returning(motor, "Una frase. Otra frase.")
    motor._run_vision_look("lee eso", "direct")
    assert motor._calls.index("discard") < motor._calls.index("submit_streaming")


def test_look_carries_the_already_read_aloud_instruction(motor):
    _step_returning(motor, "Una frase. Otra frase.")
    look = motor._run_vision_look("lee eso", "direct")
    assert look.block and look.read_note == llm_engine_vision.VISION_READ_ALOUD_NOTE
    assert look.note == "" and look.fail_line == ""


def test_blocked_sentence_is_dropped_and_only_counts_are_logged(motor, caplog):
    _step_returning(motor, f"Secreto uno. {BLOCKED} Secreto tres.")
    with caplog.at_level(logging.INFO, logger="OpenCohost"):
        motor._run_vision_look("lee eso", "direct")
    (job,) = motor._r.jobs
    assert job.chunks == ["Secreto uno.", "Secreto tres."]
    messages = [r.getMessage() for r in caplog.records]
    assert any("read_aloud sentences=2 blocked=1" in m for m in messages)
    assert all("Secreto" not in m and "language model" not in m for m in messages)


def test_every_sentence_blocked_reads_nothing_and_adds_no_note(motor):
    spoken = _step_returning(motor, BLOCKED)
    look = motor._run_vision_look("lee eso", "direct")
    assert motor._r.jobs == []
    assert look.read_note == ""
    assert spoken == ["Ya vi, dame un segundo."]  # byte-identical to V1.28


def test_setting_off_is_byte_identical_to_v128(motor, monkeypatch):
    monkeypatch.setattr(settings, "VISION_READ_ALOUD", False)
    spoken = _step_returning(motor, "Una frase. Otra frase.")
    look = motor._run_vision_look("lee eso", "direct")
    assert motor._r.jobs == [] and look.read_note == ""
    assert spoken == ["Ya vi, dame un segundo."]


def test_clip_and_default_modes_never_read(motor):
    spoken = _step_returning(motor, "", mode=MODE_CLIP)
    look = motor._run_vision_look("mira esto", "direct")
    assert motor._r.jobs == [] and look.read_note == ""
    assert spoken == ["Ya vi, dame un segundo."]


def test_no_reading_without_the_speech_router(motor):
    motor._speech_router_enabled = False
    _step_returning(motor, "Una frase. Otra frase.")
    look = motor._run_vision_look("lee eso", "direct")
    assert motor._r.jobs == [] and look.read_note == ""


def test_no_reading_after_an_emergency_stop(motor):
    motor._cancelled_speech_prefixes = ("direct",)
    _step_returning(motor, "Una frase. Otra frase.")
    look = motor._run_vision_look("lee eso", "direct")
    assert motor._r.jobs == [] and look.read_note == ""


def test_router_failure_never_loses_the_turn(motor):
    _step_returning(motor, "Una frase. Otra frase.")

    def boom(*a, **k):
        raise RuntimeError("router down")

    motor._r.submit_streaming = boom
    look = motor._run_vision_look("lee eso", "direct")
    assert look.block and look.read_note == ""


def test_markdown_in_the_transcription_is_sanitized_like_a_reply(motor):
    _step_returning(motor, "**Titulo importante** de la tabla. Siguiente fila aqui.")
    motor._run_vision_look("lee eso", "direct")
    (job,) = motor._r.jobs
    assert all("*" not in chunk for chunk in job.chunks)


def test_failed_look_still_uses_the_failure_path(motor):
    class Step:
        def run(self, text, source, *, cancel=None):
            return SimpleNamespace(
                screen_block="", note="VISION NOTE: x", outcome="describe_timeout",
                status_line="", reason_codes=("explicit_look",), transcription="", mode=MODE_TEXT,
            )

    motor._vision = SimpleNamespace(step=Step())
    motor._vision_bridge_started = lambda: True
    look = motor._run_vision_look("lee eso", "direct")
    assert motor._r.jobs == [] and look.fail_line


def test_pregen_text_look_never_reads_aloud(motor):
    """V1.35: a pregen look runs in the background while Kira may still speak."""
    spoken = _step_returning(motor, "Primera linea de prueba. Segunda linea de prueba.")
    look = motor._run_vision_look("lee eso", "direct", stash=True)
    assert motor._r.jobs == [] and "submit_streaming" not in motor._calls
    assert look.block and look.read_note == ""
    assert spoken == []  # V1.36: a pregen look never speaks the status line either


def test_foreground_text_look_still_reads_after_the_pregen_gate(motor):
    _step_returning(motor, "Una frase. Otra frase.")
    look = motor._run_vision_look("lee eso", "direct", stash=False)
    assert len(motor._r.jobs) == 1 and look.read_note == llm_engine_vision.VISION_READ_ALOUD_NOTE


def test_foreground_reuse_of_a_pregen_look_does_not_read(motor):
    """V1.35: a reused stashed pregen look answers like V1.28 (block, no reading)."""
    _step_returning(motor, "Una frase. Otra frase.")
    motor._run_vision_look("lee eso", "direct", stash=True)
    reused = motor._run_vision_look("lee eso", "direct", stash=False)
    assert motor._calls.count("step") == 1  # reused, not looked twice
    assert motor._r.jobs == [] and reused.block and reused.read_note == ""


# V1.36: a pregen look (stash=True) speaks nothing at all: not the bridge, not
# the progress lines (the step's own callbacks) and not the status line.


def _speaking_step(motor, *, status_line="Ya vi, dame un segundo."):
    """Step that drives the real bridge/progress callbacks, like VisionTurnStep."""
    import threading

    class Step:
        def run(self, text, source, *, cancel=None):
            motor._vision_speak_bridge("Dejame mirar.")
            # the progress ticker speaks from its own thread
            t = threading.Thread(target=motor._vision_speak_progress, args=("Sigo leyendo...",))
            t.start()
            t.join()
            return SimpleNamespace(
                screen_block="[SCREEN]\nx\n[/SCREEN]", note="", outcome="ok",
                status_line=status_line, reason_codes=(), transcription="", mode=MODE_DEFAULT,
            )

    motor._vision = SimpleNamespace(step=Step())


def test_pregen_look_speaks_no_bridge_progress_or_status_line(motor):
    _speaking_step(motor)
    look = motor._run_vision_look("mira esto", "direct", stash=True)
    assert motor._r.jobs == [] and "submit" not in motor._calls
    assert look.block


def test_foreground_look_still_speaks_bridge_progress_and_status_line(motor):
    _speaking_step(motor)
    # the bridge job counts as started so the status line follows
    motor._vision_bridge_started = lambda: True
    motor._run_vision_look("mira esto", "direct", stash=False)
    suffixes = [j.source.rsplit("-vision-", 1)[-1] for j in motor._r.jobs]
    assert "bridge" in suffixes and "status" in suffixes


def test_pregen_silence_does_not_leak_into_the_next_foreground_look(motor):
    _speaking_step(motor)
    motor._run_vision_look("mira esto", "direct", stash=True)
    motor._vision_pregen_look = None  # force a real second look
    motor._run_vision_look("mira otra vez", "direct", stash=False)
    assert any(j.source.endswith("-vision-bridge") for j in motor._r.jobs)


def test_pregen_silence_is_released_when_the_step_raises(motor):
    class Boom:
        def run(self, text, source, *, cancel=None):
            raise RuntimeError("x")

    motor._vision = SimpleNamespace(step=Boom())
    motor._run_vision_look("mira esto", "direct", stash=True)
    motor._vision_speak_bridge("hola")
    assert any(j.source.endswith("-vision-bridge") for j in motor._r.jobs)


# ------------------------------------------------------------ prompt assembler


def _system(screen_block="", vision_read_note=""):
    return PromptContextAssembler().assemble(
        "lee eso", "direct",
        system_prompt="Eres Kira.", use_system_role=True, is_local=True, provider_cfg={},
        request_model="llama3", history_snapshot=[], personalization_enabled=False,
        memorias_enabled=False, screen_block=screen_block, vision_read_note=vision_read_note,
    ).messages[0]["content"]


def test_read_aloud_note_forbids_repeating_the_transcription():
    note = llm_engine_vision.VISION_READ_ALOUD_NOTE.lower()
    assert "already" in note and "aloud" in note and "never repeat" in note


def test_assembler_adds_the_note_on_a_vision_turn():
    block = build_screen_block("Una tabla con filas.")
    note = llm_engine_vision.VISION_READ_ALOUD_NOTE
    assert note in _system(block, note)
    assert note not in _system(block, "")


def test_assembler_ignores_the_note_without_a_screen_block():
    note = llm_engine_vision.VISION_READ_ALOUD_NOTE
    assert note not in _system("", note)


def test_assembler_without_a_note_is_unchanged_for_a_plain_vision_turn():
    block = build_screen_block("Una tabla con filas.")
    plain = _system(block, "")
    assert "READ ALOUD" not in plain


# ------------------------------------------------------------ end to end (real motor)

from tests.test_vision_engine_wiring import (  # noqa: E402
    FakeRouter,
    _arm_buffered,
    _arm_streaming,
    _user_content,
)
from tests.test_vision_engine_wiring import motor as wiring_motor  # noqa: E402,F401
from opencohost.core.vision.turn_step import VisionTurnResult  # noqa: E402


class _ProtectedRouter(FakeRouter):
    def submit_streaming(self, source, priority, turn_id=None, protected=False):
        job = super().submit_streaming(source, priority, turn_id)
        job.protected = protected
        return job


class _ReadStep:
    def __init__(self, events):
        self.events = events

    def run(self, text, source, *, cancel=None):
        self.events.append("vision")
        block = build_screen_block("Barra de vida al 30%.")
        return VisionTurnResult(
            block, ("explicit_look", "mode_text"), "ok", mode=MODE_TEXT,
            transcription="Barra de vida al 30%. Cuarenta monedas.",
        )


@pytest.mark.parametrize("path", ["buffered", "streaming"])
def test_reading_is_queued_before_generation_and_the_reply_gets_the_note(
    wiring_motor, monkeypatch, path,  # noqa: F811
):
    motor = wiring_motor
    monkeypatch.setattr(settings, "VISION_READ_ALOUD", True)
    router = _ProtectedRouter()
    motor._ensure_router = lambda: router
    motor._speech_router = router
    seen = (_arm_buffered if path == "buffered" else _arm_streaming)(motor, monkeypatch)
    motor._vision = SimpleNamespace(step=_ReadStep(motor._events))

    motor._generar_dialogo("lee eso", source="direct", commit_history=True)

    reading = router.streaming[0]
    assert reading.source == "direct-vision-read" and reading.protected and reading.sealed
    assert router.appends[0][1] == ["Barra de vida al 30%.", "Cuarenta monedas."]
    system = seen["messages"][0]["content"]
    assert llm_engine_vision.VISION_READ_ALOUD_NOTE in system
    # the note is turn-local: never in the user message or in history
    assert llm_engine_vision.VISION_READ_ALOUD_NOTE not in _user_content(seen)
    assert all(
        llm_engine_vision.VISION_READ_ALOUD_NOTE not in str(m.get("content", ""))
        for m in motor.historial
    )
    if path == "streaming":
        # the reaction is a LATER job: it queues behind the protected reading
        assert [j.source for j in router.streaming] == ["direct-vision-read", "direct"]
        assert router.streaming[1].protected is False


def test_setting_off_leaves_the_turn_byte_identical(wiring_motor, monkeypatch):  # noqa: F811
    motor = wiring_motor
    monkeypatch.setattr(settings, "VISION_READ_ALOUD", False)
    router = _ProtectedRouter()
    motor._ensure_router = lambda: router
    motor._speech_router = router
    seen = _arm_streaming(motor, monkeypatch)
    motor._vision = SimpleNamespace(step=_ReadStep(motor._events))

    motor._generar_dialogo("lee eso", source="direct", commit_history=True)

    assert [j.source for j in router.streaming] == ["direct"]
    assert llm_engine_vision.VISION_READ_ALOUD_NOTE not in seen["messages"][0]["content"]


# ------------------------------------------------ V1.33 toggle-aware protection


def _start_reading(router, turn_id=7):
    reading = router.submit_streaming(
        "direct-vision-read", PRIORITY_OWNER, turn_id=turn_id, protected=True,
    )
    router.append_chunks(reading, _stream_chunks("R", 1))
    assert _wait_until(lambda: reading.state is SpeechJobState.ACTIVE)
    pauses: list = []
    router.pause_speech = lambda reason, target=None: pauses.append((reason, target))
    return reading, pauses


@pytest.mark.parametrize("later_turn", [None, 7, 8])
def test_toggle_off_protects_the_reading_from_any_later_owner_submit(router_motors, later_turn):  # noqa: F811
    motor, _, _ = _armed(router_motors, synthesize=_synth_logger([]), interrupt_enabled=True)
    motor._discard_speech_residue = False
    router = motor._ensure_router()
    reading, pauses = _start_reading(router)
    router.submit_streaming("direct", PRIORITY_OWNER, turn_id=later_turn)
    router.submit("Otra cosa.", "direct", PRIORITY_OWNER, turn_id=later_turn)
    assert pauses == []
    router.seal(reading)


@pytest.mark.parametrize("later_turn", [None, 8])
@pytest.mark.parametrize("streaming", [True, False])
def test_toggle_on_lets_a_new_owner_reply_cut_the_reading(router_motors, later_turn, streaming):  # noqa: F811
    motor, _, _ = _armed(router_motors, synthesize=_synth_logger([]), interrupt_enabled=True)
    motor._discard_speech_residue = True
    router = motor._ensure_router()
    reading, pauses = _start_reading(router)
    if streaming:
        router.submit_streaming("direct", PRIORITY_OWNER, turn_id=later_turn)
    else:
        router.submit("Otra cosa.", "direct", PRIORITY_OWNER, turn_id=later_turn)
    # discard handling applies exactly as for any other Kira speech
    assert pauses == [("discard", reading)]
    router.seal(reading)


@pytest.mark.parametrize("streaming", [True, False])
def test_toggle_on_same_turn_reaction_never_cuts_its_own_reading(router_motors, streaming):  # noqa: F811
    motor, _, _ = _armed(router_motors, synthesize=_synth_logger([]), interrupt_enabled=True)
    motor._discard_speech_residue = True
    router = motor._ensure_router()
    reading, pauses = _start_reading(router, turn_id=7)
    if streaming:
        router.submit_streaming("direct", PRIORITY_OWNER, turn_id=7)
    else:
        router.submit("Reaccion.", "direct", PRIORITY_OWNER, turn_id=7)
    assert pauses == []
    router.seal(reading)


def test_toggle_is_read_live_at_submit_time(router_motors):  # noqa: F811
    motor, _, _ = _armed(router_motors, synthesize=_synth_logger([]), interrupt_enabled=True)
    motor._discard_speech_residue = False
    router = motor._ensure_router()
    reading, pauses = _start_reading(router)
    router.submit_streaming("direct", PRIORITY_OWNER, turn_id=8)
    assert pauses == []
    motor._discard_speech_residue = True  # owner flips it mid-reading
    router.submit_streaming("direct", PRIORITY_OWNER, turn_id=9)
    assert pauses == [("discard", reading)]
    router.seal(reading)


def test_reading_without_a_turn_id_is_never_same_turn(router_motors):  # noqa: F811
    motor, _, _ = _armed(router_motors, synthesize=_synth_logger([]), interrupt_enabled=True)
    motor._discard_speech_residue = True
    router = motor._ensure_router()
    reading = router.submit_streaming("direct-vision-read", PRIORITY_OWNER, protected=True)
    router.append_chunks(reading, _stream_chunks("R", 1))
    assert _wait_until(lambda: reading.state is SpeechJobState.ACTIVE)
    pauses: list = []
    router.pause_speech = lambda reason, target=None: pauses.append(reason)
    router.submit_streaming("direct", PRIORITY_OWNER)  # None == None must not match
    assert pauses == ["discard"]
    router.seal(reading)


class _TurnRouter(_ProtectedRouter):
    def submit_streaming(self, source, priority, turn_id=None, protected=False):
        job = super().submit_streaming(source, priority, turn_id, protected)
        job.turn_id = turn_id
        return job

    def submit(self, text, source, priority, turn_id=None, protected=False):
        job = super().submit(text, source, priority, turn_id)
        job.turn_id = turn_id
        return job


@pytest.mark.parametrize("path", ["buffered", "streaming"])
def test_reaction_carries_the_readings_turn_id(wiring_motor, monkeypatch, path):  # noqa: F811
    motor = wiring_motor
    monkeypatch.setattr(settings, "VISION_READ_ALOUD", True)
    router = _TurnRouter()
    motor._ensure_router = lambda: router
    motor._speech_router = router
    (_arm_buffered if path == "buffered" else _arm_streaming)(motor, monkeypatch)
    motor._vision = SimpleNamespace(step=_ReadStep(motor._events))

    motor._generar_dialogo("lee eso", source="direct", commit_history=True)
    reading = router.streaming[0]
    assert isinstance(reading.turn_id, int) and reading.protected
    if path == "streaming":
        assert router.streaming[1].turn_id == reading.turn_id
    else:
        # the buffered reply is spoken by the caller after the turn returns
        motor._speak_or_submit("Una reaccion corta.", "direct")
        assert router.submitted[-1].turn_id == reading.turn_id


def test_next_turn_does_not_inherit_the_readings_turn_id(wiring_motor, monkeypatch):  # noqa: F811
    motor = wiring_motor
    monkeypatch.setattr(settings, "VISION_READ_ALOUD", True)
    router = _TurnRouter()
    motor._ensure_router = lambda: router
    motor._speech_router = router
    _arm_streaming(motor, monkeypatch)
    motor._vision = SimpleNamespace(step=_ReadStep(motor._events))
    motor._generar_dialogo("lee eso", source="direct", commit_history=True)
    reading = router.streaming[0]
    motor._vision = SimpleNamespace(step=SimpleNamespace(run=lambda *a, **k: VisionTurnResult("", (), "skip")))
    _arm_streaming(motor, monkeypatch)
    motor._generar_dialogo("hola", source="direct", commit_history=True)
    assert router.streaming[-1].turn_id is None
    assert router.streaming[-1].turn_id != reading.turn_id


def test_pregen_generation_does_not_clear_the_readings_turn_token(wiring_motor, monkeypatch):  # noqa: F811
    """V1.34: a pregen between the reading and the buffered reaction keeps the token."""
    motor = wiring_motor
    monkeypatch.setattr(settings, "VISION_READ_ALOUD", True)
    router = _TurnRouter()
    motor._ensure_router = lambda: router
    motor._speech_router = router
    _arm_buffered(motor, monkeypatch)
    motor._vision = SimpleNamespace(step=_ReadStep(motor._events))
    motor._generar_dialogo("lee eso", source="direct", commit_history=True)
    reading = router.streaming[0]

    # the pregen worker generates the next head the way llm_engine_pregen does
    motor._vision = SimpleNamespace(step=SimpleNamespace(run=lambda *a, **k: VisionTurnResult("", (), "skip")))
    _arm_buffered(motor, monkeypatch)
    motor._generar_dialogo("siguiente", source="direct", commit_history=False, allow_vision=False)

    motor._speak_or_submit("Una reaccion corta.", "direct")
    assert router.submitted[-1].turn_id == reading.turn_id
