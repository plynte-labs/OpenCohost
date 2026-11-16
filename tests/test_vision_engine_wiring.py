"""Engine turn-path wiring for VisionTurnStep (vision-on-demand V1.5).

Real MotorVocalIA, fake vision step, fake LLM transports. Pins:
- owner sources (direct, ptt) get the screen block into the assembled prompt
  on BOTH the buffered and the streaming path;
- the step runs strictly before LLM generation;
- chat/agenda/bundle sources and disabled vision never invoke the step;
- the bridge line goes through the speech router on the owner band and a
  bridge that never started is retired before the reply.
"""
from __future__ import annotations

import queue
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from opencohost.config.settings import OWNER_BUNDLE_SOURCE
from opencohost.core import llm_engine
from opencohost.core.speech.router import PRIORITY_OWNER, SpeechJobState
from opencohost.core.vision.sanitize import build_screen_block
from opencohost.core.vision.turn_step import VisionTurnResult

# B-W4: this module pins the legacy prompt layout (system-message placement, one-pair
# history deque, legacy goldens). The v2 placement is tested in tests/test_prompt_layout_v2.py.
pytestmark = pytest.mark.usefixtures("legacy_prompt_layout")

BLOCK = build_screen_block("Barra de vida al 30%.")


class FakeStep:
    def __init__(self, events, block=BLOCK, bridge=None):
        self.events = events
        self.block = block
        self.bridge = bridge
        self.calls = []

    def run(self, text, source, *, cancel=None):
        self.calls.append((text, source))
        self.events.append("vision")
        if self.bridge is not None:
            self.bridge("A ver, déjame mirar…")
        return VisionTurnResult(self.block, ("explicit_look",), "ok" if self.block else "no_vision")


class FakeJob:
    def __init__(self, job_id, text="", source="", priority=0):
        self.job_id = job_id
        self.text = text
        self.source = source
        self.priority = priority
        self.state = SpeechJobState.QUEUED
        self.sealed = False


class FakeRouter:
    def __init__(self):
        self.submitted = []
        self.streaming = []
        self.appends = []
        self.discarded = []

    def submit(self, text, source, priority, turn_id=None):
        job = FakeJob(len(self.submitted) + 1, text, source, priority)
        self.submitted.append(job)
        return job

    def submit_streaming(self, source, priority, turn_id=None):
        job = FakeJob(100 + len(self.streaming), source=source, priority=priority)
        self.streaming.append(job)
        return job

    def append_chunks(self, job, chunks):
        self.appends.append((job, list(chunks)))
        return True

    def seal(self, job):
        job.sealed = True

    def discard_job(self, job):
        self.discarded.append(job)
        job.state = SpeechJobState.DISCARDED


class FakeMessage:
    def __init__(self, content=""):
        self.content = content
        self.thinking = None


class FakeChunk:
    def __init__(self, content="", done=False):
        self.message = FakeMessage(content)
        self.done = done

    def get(self, key, default=None):
        return getattr(self, key, default)


@pytest.fixture
def motor(monkeypatch):
    m = llm_engine.MotorVocalIA(queue.Queue(), lambda event: None)
    m.current_model = "llama3"
    m.use_system_role = True
    m.ollama = MagicMock()
    m._speech_router_enabled = True
    router = FakeRouter()
    m._ensure_router = lambda: router
    m._speech_router = router
    m._fake_router = router
    m._events = []
    return m


def _arm_buffered(motor, monkeypatch):
    monkeypatch.setattr(llm_engine, "LLM_STREAMING_ENABLED", False)
    seen = {}

    def _chat(**kwargs):
        motor._events.append("llm")
        seen["messages"] = kwargs["messages"]
        return {"message": {"content": "Tienes poca vida, cúrate ya."}}

    motor._ollama_chat_with_watchdog = _chat
    motor._ollama_chat_streaming = MagicMock(side_effect=AssertionError("buffered only"))
    return seen


def _arm_streaming(motor, monkeypatch):
    monkeypatch.setattr(llm_engine, "LLM_STREAMING_ENABLED", True)
    seen = {}

    def _streaming(*, timeout, **kwargs):
        motor._events.append("llm")
        seen["messages"] = kwargs["messages"]

        def _gen():
            yield FakeChunk("Tienes poca vida, cúrate ya. ")
            yield FakeChunk("", done=True)

        return _gen()

    motor._ollama_chat_streaming = _streaming
    motor._ollama_chat_with_watchdog = MagicMock(side_effect=AssertionError("streaming only"))
    return seen


def _user_content(seen):
    return seen["messages"][-1]["content"]


# --------------------------------------------------------------- owner turns


@pytest.mark.parametrize("path", ["buffered", "streaming"])
@pytest.mark.parametrize("source", ["direct", "ptt"])
def test_owner_turn_gets_screen_block_before_generation(motor, monkeypatch, path, source):
    seen = (_arm_buffered if path == "buffered" else _arm_streaming)(motor, monkeypatch)
    step = FakeStep(motor._events)
    motor._vision = SimpleNamespace(step=step)

    dialogo = motor._generar_dialogo("¿qué ves?", source=source, commit_history=True)

    assert dialogo == "Tienes poca vida, cúrate ya."
    assert step.calls and step.calls[0][1] == source
    assert motor._events == ["vision", "llm"]
    assert BLOCK in _user_content(seen)
    # the description never enters conversation history
    assert all(BLOCK not in str(m.get("content", "")) for m in motor.historial)


def test_ptt_question_reaches_the_step_without_the_transport_wrapper(motor, monkeypatch):
    _arm_buffered(motor, monkeypatch)
    step = FakeStep(motor._events)
    motor._vision = SimpleNamespace(step=step)
    wrapped = llm_engine.i18n_active.ptt_wrapper().format(text="mira esto")

    motor._generar_dialogo(wrapped, source="ptt", commit_history=True)

    assert step.calls[0][0] == "mira esto"


def test_step_runs_once_per_turn_even_with_a_cloud_retry(motor, monkeypatch):
    seen = _arm_buffered(motor, monkeypatch)
    step = FakeStep(motor._events)
    motor._vision = SimpleNamespace(step=step)
    original = motor._build_generation_request
    builds = []

    def _spy(*args, **kwargs):
        builds.append(kwargs.get("screen_block"))
        return original(*args, **kwargs)

    motor._build_generation_request = _spy

    motor._generar_dialogo("¿qué ves?", source="direct", commit_history=True)

    assert len(step.calls) == 1
    assert builds == [BLOCK]
    assert BLOCK in _user_content(seen)


def test_empty_vision_result_leaves_prompt_untouched(motor, monkeypatch):
    seen = _arm_buffered(motor, monkeypatch)
    motor._vision = SimpleNamespace(step=FakeStep(motor._events, block=""))

    motor._generar_dialogo("hola", source="direct", commit_history=True)

    assert _user_content(seen) == "hola"


# ---------------------------------------------------- never invoked paths


@pytest.mark.parametrize("source", ["chat", "kira-agenda", OWNER_BUNDLE_SOURCE, "livevoice"])
def test_non_owner_sources_never_invoke_the_step(motor, monkeypatch, source):
    seen = _arm_buffered(motor, monkeypatch)
    step = FakeStep(motor._events)
    motor._vision = SimpleNamespace(step=step)

    motor._generar_dialogo("mira esto", source=source, commit_history=True)

    assert step.calls == []
    assert all(BLOCK not in str(m.get("content", "")) for m in seen["messages"])


def test_non_committing_call_never_invokes_the_step(motor, monkeypatch):
    _arm_buffered(motor, monkeypatch)
    step = FakeStep(motor._events)
    motor._vision = SimpleNamespace(step=step)

    motor._generar_dialogo("mira esto", source="direct", commit_history=False)

    assert step.calls == []


@pytest.mark.parametrize("vision", [None, SimpleNamespace(step=None)])
def test_disabled_vision_never_invokes_anything(motor, monkeypatch, vision):
    seen = _arm_buffered(motor, monkeypatch)
    motor._vision = vision

    dialogo = motor._generar_dialogo("hola", source="direct", commit_history=True)

    assert dialogo == "Tienes poca vida, cúrate ya."
    assert _user_content(seen) == "hola"


def test_motor_without_vision_attribute_works(motor, monkeypatch):
    """`__new__`-built motors in older suites never set `_vision`."""
    _arm_buffered(motor, monkeypatch)
    motor.__dict__.pop("_vision", None)

    assert motor._run_vision_for_turn("hola", "direct") == ("", "")


def test_step_exception_fails_open(motor, monkeypatch):
    seen = _arm_buffered(motor, monkeypatch)

    class Boom:
        def run(self, *a, **k):
            raise RuntimeError("x")

    motor._vision = SimpleNamespace(step=Boom())

    dialogo = motor._generar_dialogo("hola", source="direct", commit_history=True)

    assert dialogo == "Tienes poca vida, cúrate ya."
    assert _user_content(seen) == "hola"


# ---------------------------------------------------------------- bridge


def test_bridge_is_submitted_on_the_owner_band_with_an_owner_prefixed_tag(motor, monkeypatch):
    _arm_buffered(motor, monkeypatch)
    step = FakeStep(motor._events, bridge=motor._vision_speak_bridge)
    motor._vision = SimpleNamespace(step=step)

    motor._generar_dialogo("mira esto", source="ptt", commit_history=True)

    bridge = motor._fake_router.submitted[0]
    assert bridge.text == "A ver, déjame mirar…"
    assert bridge.priority == PRIORITY_OWNER
    assert bridge.source.startswith("ptt")
    assert bridge.source != "ptt"


def test_bridge_still_queued_after_the_look_is_retired(motor, monkeypatch):
    _arm_buffered(motor, monkeypatch)
    step = FakeStep(motor._events, bridge=motor._vision_speak_bridge)
    motor._vision = SimpleNamespace(step=step)

    motor._generar_dialogo("mira esto", source="direct", commit_history=True)

    bridge = motor._fake_router.submitted[0]
    assert motor._fake_router.discarded == [bridge]


@pytest.mark.parametrize("state", [SpeechJobState.ACTIVE, SpeechJobState.FINISHED])
def test_bridge_already_playing_or_done_is_left_alone(motor, monkeypatch, state):
    _arm_buffered(motor, monkeypatch)

    def _bridge_then_progress(line):
        motor._vision_speak_bridge(line)
        motor._fake_router.submitted[-1].state = state

    step = FakeStep(motor._events, bridge=_bridge_then_progress)
    motor._vision = SimpleNamespace(step=step)

    motor._generar_dialogo("mira esto", source="direct", commit_history=True)

    assert motor._fake_router.discarded == []


def test_bridge_without_router_is_skipped_never_blocking(motor, monkeypatch):
    _arm_buffered(motor, monkeypatch)
    motor._speech_router_enabled = False
    motor._hablar = MagicMock(side_effect=AssertionError("must not block the turn"))

    motor._vision_bridge_source = "direct"
    motor._vision_speak_bridge("A ver, déjame mirar…")

    assert motor._fake_router.submitted == []


# ------------------------------------------------- V1.8: failure note seam

NOTE = (
    "VISION NOTE: You tried to look at the owner's screen just now but could not "
    "(reason: describe_timeout). Say so briefly in character; do not claim they "
    "showed you nothing."
)


class NoteStep(FakeStep):
    """A failed look; by default the AUTOMATIC PTT look (V1.15: a requested
    look that fails skips the LLM, so the note seam is the automatic path)."""

    def __init__(self, events, block="", reasons=("ptt_switch", "describe_timeout"),
                 outcome="describe_timeout"):
        super().__init__(events, block=block)
        self.reasons = reasons
        self.outcome = outcome

    def run(self, text, source, *, cancel=None):
        self.calls.append((text, source))
        self.events.append("vision")
        return VisionTurnResult("", self.reasons, self.outcome,
                                {"capture_ms": 172, "describe_ms": 8000}, note=NOTE)


@pytest.mark.parametrize("path", ["buffered", "streaming"])
def test_failed_look_puts_the_note_in_the_prompt_never_in_history(motor, monkeypatch, path):
    seen = (_arm_buffered if path == "buffered" else _arm_streaming)(motor, monkeypatch)
    motor._vision = SimpleNamespace(step=NoteStep(motor._events, block=""))

    motor._generar_dialogo("¿qué ves?", source="ptt", commit_history=True)

    joined = "\n".join(str(m.get("content", "")) for m in seen["messages"])
    assert NOTE in joined
    assert _user_content(seen) == "¿qué ves?"
    # the note is turn-local: never in conversation history (and so never in memoria)
    assert all(NOTE not in str(m.get("content", "")) for m in motor.historial)
    assert all("VISION NOTE" not in str(m.get("content", "")) for m in motor.historial)


def test_note_is_reused_by_the_cloud_retry_without_looking_twice(motor, monkeypatch):
    _arm_buffered(motor, monkeypatch)
    step = NoteStep(motor._events, block="")
    motor._vision = SimpleNamespace(step=step)
    original = motor._build_generation_request
    notes = []

    def _spy(*args, **kwargs):
        notes.append(kwargs.get("vision_note"))
        return original(*args, **kwargs)

    motor._build_generation_request = _spy

    motor._generar_dialogo("¿qué ves?", source="ptt", commit_history=True)

    assert len(step.calls) == 1
    assert len(notes) == 1 and notes[0].startswith(NOTE)


# ------------------------------------------- V1.13: interactive pregen looks

from opencohost.core.engine import llm_engine_vision  # noqa: E402


def test_pregen_call_with_allow_vision_runs_the_step_before_generation(motor, monkeypatch):
    seen = _arm_buffered(motor, monkeypatch)
    step = FakeStep(motor._events)
    motor._vision = SimpleNamespace(step=step)

    motor._generar_dialogo("¿qué ves?", source="ptt", commit_history=False, allow_vision=True)

    assert step.calls == [("¿qué ves?", "ptt")]
    assert motor._events == ["vision", "llm"]
    assert BLOCK in _user_content(seen)


def test_allow_vision_false_skips_the_step_even_when_committing(motor, monkeypatch):
    _arm_buffered(motor, monkeypatch)
    step = FakeStep(motor._events)
    motor._vision = SimpleNamespace(step=step)

    motor._generar_dialogo("mira esto", source="direct", commit_history=True, allow_vision=False)

    assert step.calls == []


@pytest.mark.parametrize(
    "source,expected",
    [("ptt", True), ("direct", True), ("chat", False), ("kira-agenda", False)],
)
def test_pregen_worker_lets_only_owner_sources_look(motor, source, expected):
    calls = []

    def _gen(payload, **kwargs):
        calls.append(kwargs)
        return ""  # nothing stored; the worker returns after one attempt

    motor._generar_dialogo = _gen

    assert motor.pregenerate("mira esto", 1, source) is True
    motor._prefetch_thread.join(timeout=2.0)

    assert calls and calls[0]["commit_history"] is False
    assert calls[0]["allow_vision"] is expected


def test_discarded_pregen_look_is_reused_by_the_regeneration_without_looking_twice(motor, monkeypatch):
    seen = _arm_buffered(motor, monkeypatch)
    step = FakeStep(motor._events)
    motor._vision = SimpleNamespace(step=step)

    motor._generar_dialogo("¿qué ves?", source="ptt", commit_history=False, allow_vision=True)
    motor._generar_dialogo("¿qué ves?", source="ptt", commit_history=True)

    assert len(step.calls) == 1
    assert BLOCK in _user_content(seen)


def test_pregen_look_is_reused_only_once(motor, monkeypatch):
    _arm_buffered(motor, monkeypatch)
    step = FakeStep(motor._events)
    motor._vision = SimpleNamespace(step=step)

    motor._generar_dialogo("¿qué ves?", source="ptt", commit_history=False, allow_vision=True)
    motor._generar_dialogo("¿qué ves?", source="ptt", commit_history=True)
    motor._generar_dialogo("¿qué ves?", source="ptt", commit_history=True)

    assert len(step.calls) == 2


def test_pregen_look_is_not_reused_for_another_question(motor, monkeypatch):
    _arm_buffered(motor, monkeypatch)
    step = FakeStep(motor._events)
    motor._vision = SimpleNamespace(step=step)

    motor._generar_dialogo("¿qué ves?", source="ptt", commit_history=False, allow_vision=True)
    motor._generar_dialogo("¿y ahora?", source="ptt", commit_history=True)

    assert len(step.calls) == 2


def test_consumed_pregen_draft_drops_its_look(motor, monkeypatch):
    _arm_buffered(motor, monkeypatch)
    step = FakeStep(motor._events)
    motor._vision = SimpleNamespace(step=step)

    motor._generar_dialogo("¿qué ves?", source="ptt", commit_history=False, allow_vision=True)
    motor._prefetched_agenda = {"payload": "¿qué ves?", "source": "ptt", "dialogo": "x"}
    assert motor._take_pregen_if_match("¿qué ves?", "ptt") is not None
    motor._generar_dialogo("¿qué ves?", source="ptt", commit_history=True)

    assert len(step.calls) == 2


def test_stale_pregen_look_expires(motor, monkeypatch):
    _arm_buffered(motor, monkeypatch)
    step = FakeStep(motor._events)
    motor._vision = SimpleNamespace(step=step)
    now = [1000.0]
    monkeypatch.setattr(llm_engine_vision, "_monotonic", lambda: now[0])

    motor._generar_dialogo("¿qué ves?", source="ptt", commit_history=False, allow_vision=True)
    now[0] += llm_engine_vision.VISION_PREGEN_LOOK_TTL_S + 1
    motor._generar_dialogo("¿qué ves?", source="ptt", commit_history=True)

    assert len(step.calls) == 2


def test_pregen_without_an_attempted_look_stashes_nothing(motor, monkeypatch):
    _arm_buffered(motor, monkeypatch)
    step = FakeStep(motor._events, block="")  # gate said no look
    motor._vision = SimpleNamespace(step=step)

    motor._generar_dialogo("hola", source="ptt", commit_history=False, allow_vision=True)
    motor._generar_dialogo("hola", source="ptt", commit_history=True)

    assert len(step.calls) == 2


def test_foreground_look_is_never_stashed(motor, monkeypatch):
    _arm_buffered(motor, monkeypatch)
    step = FakeStep(motor._events)
    motor._vision = SimpleNamespace(step=step)

    motor._generar_dialogo("¿qué ves?", source="direct", commit_history=True)
    motor._generar_dialogo("¿qué ves?", source="direct", commit_history=True)

    assert len(step.calls) == 2


# ------------------------------------------------ V1.13: processing status line

STATUS = "Ya vi, dame un segundo."
BRIDGE = "A ver, déjame mirar…"


class StatusStep(FakeStep):
    """Speaks the bridge, moves it to ``bridge_state``, returns a status line."""

    def __init__(self, motor, *, bridge_state=SpeechJobState.FINISHED, block=BLOCK, status=STATUS):
        super().__init__(motor._events, block=block)
        self.motor = motor
        self.bridge_state = bridge_state
        self.status = status

    def run(self, text, source, *, cancel=None):
        self.calls.append((text, source))
        self.events.append("vision")
        self.motor._vision_speak_bridge(BRIDGE)
        self.motor._fake_router.submitted[-1].state = self.bridge_state
        outcome = "ok" if self.block else "describe_timeout"
        return VisionTurnResult(self.block, ("explicit_look",), outcome,
                                status_line=self.status if self.block else "")


def _record_submits(motor):
    router = motor._fake_router
    original = router.submit

    def _submit(text, source, priority, turn_id=None):
        motor._events.append(f"submit:{source}")
        return original(text, source, priority, turn_id)

    router.submit = _submit


@pytest.mark.parametrize("bridge_state", [SpeechJobState.ACTIVE, SpeechJobState.FINISHED])
def test_status_line_follows_a_spoken_bridge_on_the_owner_band_before_generation(
    motor, monkeypatch, bridge_state
):
    _arm_buffered(motor, monkeypatch)
    _record_submits(motor)
    motor._vision = SimpleNamespace(step=StatusStep(motor, bridge_state=bridge_state))

    motor._generar_dialogo("mira esto", source="ptt", commit_history=True)

    status = motor._fake_router.submitted[1]
    assert status.text == STATUS
    assert status.priority == PRIORITY_OWNER
    assert status.source == "ptt" + llm_engine_vision.VISION_STATUS_SUFFIX
    assert status.source.startswith("ptt")  # emergency stop prefix match
    assert status.source != motor._fake_router.submitted[0].source
    assert motor._events == ["vision", "submit:ptt-vision-bridge", "submit:ptt-vision-status", "llm"]


def test_status_line_still_queued_when_the_reply_is_ready_is_retired(motor, monkeypatch):
    _arm_buffered(motor, monkeypatch)
    motor._vision = SimpleNamespace(step=StatusStep(motor))

    motor._generar_dialogo("mira esto", source="direct", commit_history=True)

    status = motor._fake_router.submitted[1]
    assert motor._fake_router.discarded == [status]


def test_status_line_already_playing_is_left_alone(motor, monkeypatch):
    seen = _arm_buffered(motor, monkeypatch)
    original = motor._ollama_chat_with_watchdog

    def _chat(**kwargs):
        motor._fake_router.submitted[1].state = SpeechJobState.ACTIVE
        return original(**kwargs)

    motor._ollama_chat_with_watchdog = _chat
    motor._vision = SimpleNamespace(step=StatusStep(motor))

    motor._generar_dialogo("mira esto", source="direct", commit_history=True)

    assert motor._fake_router.discarded == []
    assert BLOCK in _user_content(seen)


def test_no_status_line_when_the_bridge_never_started(motor, monkeypatch):
    _arm_buffered(motor, monkeypatch)
    motor._vision = SimpleNamespace(step=StatusStep(motor, bridge_state=SpeechJobState.QUEUED))

    motor._generar_dialogo("mira esto", source="direct", commit_history=True)

    assert [j.text for j in motor._fake_router.submitted] == [BRIDGE]


def test_no_status_line_on_a_failed_look(motor, monkeypatch):
    _arm_buffered(motor, monkeypatch)
    motor._vision = SimpleNamespace(step=StatusStep(motor, block=""))

    motor._generar_dialogo("mira esto", source="direct", commit_history=True)

    assert [j.text for j in motor._fake_router.submitted] == [BRIDGE]


def test_no_status_line_without_the_speech_router(motor, monkeypatch):
    _arm_buffered(motor, monkeypatch)

    class NoBridgeStep(FakeStep):
        def run(self, text, source, *, cancel=None):
            self.calls.append((text, source))
            motor._vision_speak_bridge(BRIDGE)  # skipped: router off
            return VisionTurnResult(BLOCK, ("explicit_look",), "ok", status_line=STATUS)

    motor._speech_router_enabled = False
    motor._vision = SimpleNamespace(step=NoBridgeStep(motor._events))

    motor._run_vision_for_turn("mira esto", "direct")

    assert motor._fake_router.submitted == []


def test_reused_pregen_look_speaks_no_second_status_line(motor, monkeypatch):
    _arm_buffered(motor, monkeypatch)
    motor._vision = SimpleNamespace(step=StatusStep(motor))

    motor._generar_dialogo("mira esto", source="ptt", commit_history=False, allow_vision=True)
    motor._generar_dialogo("mira esto", source="ptt", commit_history=True)

    texts = [j.text for j in motor._fake_router.submitted]
    assert texts.count(STATUS) == 1
    assert texts.count(BRIDGE) == 1


# ------------------------------------- V1.15: failed requested look skips the LLM

from opencohost.core.vision.turn_step import FAILURE_LINES, VISION_FAILURE_SHORT_HINT  # noqa: E402

BUSY_ES = FAILURE_LINES["busy"]["es"]
BUSY_EN = FAILURE_LINES["busy"]["en"]
SELF_ES = FAILURE_LINES["self"]["es"]
GENERIC_ES = FAILURE_LINES["generic"]["es"]
ARMED_TIMEOUT = (("explicit_look", "describe_timeout"), "describe_timeout")


def _no_llm(motor, monkeypatch, path="buffered"):
    """Arm a transport that records an LLM call (the shortcut must make none)."""
    (_arm_buffered if path == "buffered" else _arm_streaming)(motor, monkeypatch)


def _llm_called(motor):
    return "llm" in motor._events


def _prompt_text(seen):
    return "\n".join(str(m.get("content", "")) for m in seen["messages"])


@pytest.mark.parametrize("path", ["buffered", "streaming"])
@pytest.mark.parametrize("source", ["direct", "ptt"])
def test_failed_armed_look_skips_generation_and_returns_the_canned_line(motor, monkeypatch, path, source):
    _no_llm(motor, monkeypatch, path)
    reasons, outcome = ARMED_TIMEOUT
    motor._vision = SimpleNamespace(step=NoteStep(motor._events, reasons=reasons, outcome=outcome))

    dialogo = motor._generar_dialogo("¿qué ves en esto?", source=source, commit_history=True)

    assert dialogo == BUSY_ES
    assert not _llm_called(motor)
    assert getattr(dialogo, "speak_source", None) == f"{source}-vision-fail"


def test_failed_armed_look_commits_the_turn_to_history_like_a_reply(motor, monkeypatch):
    _no_llm(motor, monkeypatch)
    motor._vision = SimpleNamespace(step=NoteStep(motor._events, reasons=ARMED_TIMEOUT[0]))

    motor._generar_dialogo("¿qué ves en esto?", source="direct", commit_history=True,
                           history_text="¿qué ves en esto?")

    contents = [str(m.get("content", "")) for m in motor.historial]
    assert "¿qué ves en esto?" in contents[-2]
    assert contents[-1] == BUSY_ES
    assert all("VISION NOTE" not in c for c in contents)


def test_screen_ref_failure_skips_generation(motor, monkeypatch):
    _no_llm(motor, monkeypatch)
    motor._vision = SimpleNamespace(step=NoteStep(
        motor._events, reasons=("screen_ref", "describe_timeout")))

    assert motor._generar_dialogo("mira la pantalla", source="direct") == BUSY_ES
    assert not _llm_called(motor)


def test_ptt_switch_with_a_screen_word_counts_as_requested(motor, monkeypatch):
    _no_llm(motor, monkeypatch)
    motor._vision = SimpleNamespace(step=NoteStep(motor._events))  # ptt_switch

    assert motor._generar_dialogo("¿qué hay en mi pantalla?", source="ptt") == BUSY_ES
    assert not _llm_called(motor)


def test_english_question_gets_the_english_line(motor, monkeypatch):
    _no_llm(motor, monkeypatch)
    motor._vision = SimpleNamespace(step=NoteStep(motor._events, reasons=ARMED_TIMEOUT[0]))

    assert motor._generar_dialogo("what do you see on this?", source="direct") == BUSY_EN


@pytest.mark.parametrize(
    "outcome,line", [("self_capture", SELF_ES), ("describe_unavailable", GENERIC_ES)]
)
def test_canned_line_varies_by_outcome(motor, monkeypatch, outcome, line):
    _no_llm(motor, monkeypatch)
    motor._vision = SimpleNamespace(step=NoteStep(
        motor._events, reasons=("explicit_look", outcome), outcome=outcome))

    assert motor._generar_dialogo("¿qué ves en esto?", source="direct") == line


def test_fail_shortcut_logs_one_line_without_user_text(motor, monkeypatch, caplog):
    _no_llm(motor, monkeypatch)
    motor._vision = SimpleNamespace(step=NoteStep(motor._events, reasons=ARMED_TIMEOUT[0]))

    with caplog.at_level("INFO", logger="OpenCohost"):
        motor._generar_dialogo("zanahoriapurpura en esto", source="direct")

    lines = [r.getMessage() for r in caplog.records if "fail_shortcut" in r.getMessage()]
    assert lines == ["[VISION] fail_shortcut outcome=describe_timeout skipped_llm=true"]
    assert all("zanahoriapurpura" not in r.getMessage() for r in caplog.records
               if r.getMessage().startswith("[VISION]"))


def test_failed_automatic_ptt_look_still_generates_with_a_keep_it_short_note(motor, monkeypatch):
    seen = _arm_buffered(motor, monkeypatch)
    motor._vision = SimpleNamespace(step=NoteStep(motor._events))  # ptt_switch, no screen word

    dialogo = motor._generar_dialogo("oye, ¿cómo voy?", source="ptt", commit_history=True)

    assert dialogo == "Tienes poca vida, cúrate ya."
    joined = _prompt_text(seen)
    assert NOTE in joined
    assert VISION_FAILURE_SHORT_HINT in joined


def test_cancelled_armed_look_does_not_shortcut(motor, monkeypatch):
    _arm_buffered(motor, monkeypatch)
    motor._vision = SimpleNamespace(step=NoteStep(
        motor._events, reasons=("explicit_look", "describe_cancelled"), outcome="describe_cancelled"))

    motor._generar_dialogo("¿qué ves en esto?", source="direct")

    assert _llm_called(motor)


def test_shortcut_error_falls_back_to_normal_generation(motor, monkeypatch):
    seen = _arm_buffered(motor, monkeypatch)
    motor._vision = SimpleNamespace(step=NoteStep(motor._events, reasons=ARMED_TIMEOUT[0]))

    def _boom(*a, **k):
        raise RuntimeError("x")

    monkeypatch.setattr(llm_engine_vision, "failure_line", _boom)

    dialogo = motor._generar_dialogo("¿qué ves en esto?", source="direct")

    assert dialogo == "Tienes poca vida, cúrate ya."
    assert NOTE in _prompt_text(seen)


def test_foreground_turn_speaks_the_canned_line_once_on_the_owner_band(motor, monkeypatch):
    _no_llm(motor, monkeypatch)
    motor._vision = SimpleNamespace(step=NoteStep(motor._events, reasons=ARMED_TIMEOUT[0]))

    motor._ejecutar_inferencia("¿qué ves en esto?", source="ptt")

    spoken = [j for j in motor._fake_router.submitted if j.text == BUSY_ES]
    assert len(spoken) == 1
    assert spoken[0].source == "ptt-vision-fail"
    assert spoken[0].source.startswith("ptt")  # emergency stop prefix match
    assert spoken[0].priority == PRIORITY_OWNER
    assert not _llm_called(motor)


def test_pregen_draft_with_the_canned_line_is_spoken_once_when_consumed(motor, monkeypatch):
    _no_llm(motor, monkeypatch)
    motor._vision = SimpleNamespace(step=NoteStep(motor._events, reasons=ARMED_TIMEOUT[0]))

    draft = motor._generar_dialogo("¿qué ves en esto?", source="ptt", commit_history=False,
                                   allow_vision=True)
    assert draft == BUSY_ES
    assert motor._fake_router.submitted == []  # the draft itself speaks nothing
    assert all(str(m.get("content", "")) != BUSY_ES for m in motor.historial)

    motor._speak_pregenerated({"payload": "¿qué ves en esto?", "dialogo": draft, "source": "ptt",
                               "history_text": "¿qué ves en esto?"})

    spoken = [j for j in motor._fake_router.submitted if j.text == BUSY_ES]
    assert len(spoken) == 1
    assert spoken[0].source == "ptt-vision-fail"
    assert spoken[0].priority == PRIORITY_OWNER
    assert str(motor.historial[-1].get("content", "")) == BUSY_ES
    assert not _llm_called(motor)


def test_discarded_pregen_failure_is_reused_without_a_second_look_or_llm(motor, monkeypatch):
    _no_llm(motor, monkeypatch)
    step = NoteStep(motor._events, reasons=ARMED_TIMEOUT[0])
    motor._vision = SimpleNamespace(step=step)

    motor._generar_dialogo("¿qué ves en esto?", source="ptt", commit_history=False, allow_vision=True)
    again = motor._generar_dialogo("¿qué ves en esto?", source="ptt", commit_history=True)

    assert again == BUSY_ES
    assert len(step.calls) == 1
    assert not _llm_called(motor)


@pytest.mark.parametrize("category", sorted(FAILURE_LINES))
@pytest.mark.parametrize("lang", ["es", "en"])
def test_canned_lines_pass_the_output_guard(category, lang):
    allowed, reason = llm_engine.output_guard(FAILURE_LINES[category][lang], source="ptt")
    assert allowed, reason


# ------------------------------------- V1.23: warming look takes the fail shortcut

WARMING_ES = "Todavía me estoy despertando la vista, pídemelo otra vez en unos segundos."
WARMING_EN = "My eyes are still waking up, ask me again in a few seconds."


class WarmingStep(FakeStep):
    """The look was decided but vision never became ready: no bridge, no note."""

    def __init__(self, events, reasons):
        super().__init__(events, block="")
        self.reasons = reasons

    def run(self, text, source, *, cancel=None):
        self.calls.append((text, source))
        self.events.append("vision")
        return VisionTurnResult("", self.reasons, "model_warming", {}, note="")


@pytest.mark.parametrize(
    "reasons,text,expected",
    [
        (("explicit_look", "model_warming"), "¿qué ves en esto?", WARMING_ES),
        (("screen_ref", "model_warming"), "mira la pantalla", WARMING_ES),
        (("look_verb", "model_warming"), "mira esto", WARMING_ES),
        (("look_verb", "model_warming"), "look at this", WARMING_EN),
    ],
)
def test_requested_look_while_warming_skips_the_llm_with_the_warming_line(
    motor, monkeypatch, reasons, text, expected
):
    _no_llm(motor, monkeypatch)
    motor._vision = SimpleNamespace(step=WarmingStep(motor._events, reasons))

    dialogo = motor._generar_dialogo(text, source="direct", commit_history=True)

    assert dialogo == expected
    assert not _llm_called(motor)
    assert getattr(dialogo, "speak_source", None) == "direct-vision-fail"


def test_automatic_ptt_look_while_warming_still_generates_without_a_note(motor, monkeypatch):
    seen = _arm_buffered(motor, monkeypatch)
    motor._vision = SimpleNamespace(
        step=WarmingStep(motor._events, ("ptt_switch", "model_warming")))

    dialogo = motor._generar_dialogo("hola, ¿cómo vas?", source="ptt", commit_history=True)

    assert dialogo == "Tienes poca vida, cúrate ya."
    assert "VISION NOTE" not in _prompt_text(seen)
    assert "Keep the reply short" not in _prompt_text(seen)


# ------------------------------------------- V1.24: capability line in the prompt


@pytest.mark.parametrize("enabled,present", [(True, True), (False, False)])
def test_capability_line_follows_the_vision_controller_state(motor, monkeypatch, enabled, present):
    from opencohost.core.context.prompt_assembler import VISION_CAPABILITY_LINE

    seen = _arm_buffered(motor, monkeypatch)
    motor._vision = SimpleNamespace(step=FakeStep(motor._events, block=""), enabled=enabled)

    motor._generar_dialogo("hola", source="direct", commit_history=True)

    assert (VISION_CAPABILITY_LINE in _prompt_text(seen)) is present


def test_capability_line_absent_without_a_vision_controller(motor, monkeypatch):
    from opencohost.core.context.prompt_assembler import VISION_CAPABILITY_LINE

    seen = _arm_buffered(motor, monkeypatch)

    motor._generar_dialogo("hola", source="direct", commit_history=True)

    assert VISION_CAPABILITY_LINE not in _prompt_text(seen)
