"""Strict TDD for the speech-router landing sequence, steps 0+1
(conductor/tracks/interruptible_speech_architecture_20260804/speech-router-design.md
§8). Telemetry ([SPEECH_LOST], [SPEECH_STACK]) and
capture-and-discard (`SpeechOutcome`). Zero audible behavior change: these
tests prove `_hablar_impl`'s REAL producer/consumer loop already does the
right thing and now REPORTS it accurately -- nothing resumes, nothing is
armed, no stack, no pause/resume behavior.

Convention mirrors tests/test_owner_question_bundling.py and
tests/test_tts_ptt_voice_death.py: a real, un-started `MotorVocalIA`
instance; ONLY Piper synthesis and `pygame.mixer` are faked; everything else
(the real chunk splitter, the real producer thread, the real consumer loop,
the real locks) runs for real. Cross-thread sequencing uses
`threading.Event` handshakes exclusively -- never `time.sleep` as a
synchronizer, and never a hand-set field the code is supposed to derive
(`_speech_progress`, `_speaking`, cursor are all read back AFTER causing them
through the real path, never assigned directly by a test).
"""
from __future__ import annotations

import logging
import queue
import re
import threading
from unittest.mock import MagicMock

from opencohost.core import llm_engine
from opencohost.core.llm_engine import MotorVocalIA
from opencohost.api.ptt_session import PttController


# ──────────────────────────────────────────────────────────────────────────
# Shared fakes — ONLY Piper synthesis and pygame.mixer are faked.
# ──────────────────────────────────────────────────────────────────────────


class _ScriptedMixerMusic:
    """Fake `pygame.mixer.music`. `get_busy()` reports busy (True) for
    exactly one fragment index (`block_at`, 0-based in play order) until the
    test calls `.release()` -- giving a test a real, code-derived signal
    (`entered_block`) that `_hablar_impl`'s consumer is genuinely mid-playback
    of that fragment, with zero sleeps anywhere in the handshake.
    """

    def __init__(self, block_at: int | None = None):
        self.block_at = block_at
        self.load_count = 0
        self.entered_block = threading.Event()
        self._release = threading.Event()
        self.stopped = False

    def load(self, _path):
        self.load_count += 1

    def play(self):
        pass

    def get_busy(self):
        idx = self.load_count - 1
        if self.block_at is not None and idx == self.block_at and not self._release.is_set():
            self.entered_block.set()
            return True
        return False

    def stop(self):
        self.stopped = True

    def unload(self):
        pass

    def release(self):
        self._release.set()


def _write_wav(out_path) -> bool:
    with open(out_path, "wb") as f:
        f.write(b"\x00" * 64)
    return True


def _sentences(n: int) -> list[str]:
    """n short, distinct, real sentences -> n real chunks after
    `_hablar_impl`'s own splitter (each well under the 25-word cap, so the
    splitter keeps them 1:1, one sentence per chunk index)."""
    return [f"Fragmento numero {i} de la prueba." for i in range(n)]


def _make_motor(*, mixer_music=None, synthesize=None):
    """A real MotorVocalIA, never started as a Thread (mirrors
    test_tts_ptt_voice_death.py::_make_motor + _wire_fake_piper). The
    edge-offline fast-path (`_edge_tts_offline=True`) sends every chunk
    straight to Piper, no network, matching the shipped test convention."""
    log_q: queue.Queue = queue.Queue()
    events: list[str] = []
    motor = MotorVocalIA(log_q, events.append)
    motor.ollama = MagicMock()
    motor.pygame = MagicMock()
    motor.pygame.mixer.music = mixer_music or _ScriptedMixerMusic()
    motor.voz_referencia = None
    motor.motor_tts = "ligero"
    motor.tts_local_only = False
    motor._edge_tts_offline = True  # fast-path straight to Piper, no network
    fake_piper = MagicMock()
    fake_piper.is_available.return_value = True
    fake_piper.synthesize.side_effect = synthesize or (lambda text, out_path: _write_wav(out_path))
    motor._piper = fake_piper
    return motor, log_q, events


def _run_clean(n: int = 4, source: str = "direct"):
    """Drive the real pipeline to natural completion -- the mixer never
    reports busy, so every fragment "plays" instantly and the consumer walks
    all the way to the "FIN" sentinel on the calling thread."""
    motor, _, _ = _make_motor(mixer_music=_ScriptedMixerMusic())
    return motor._hablar_impl(" ".join(_sentences(n)), source=source)


def _run_interrupted(n: int = 5, block_at: int = 2, source: str = "kira-agenda:t3"):
    """Drive the real pipeline on a background thread, wait (via a real Event
    the production busy-wait loop trips) until it is genuinely mid-playback
    of fragment `block_at`, then interrupt through the ONLY real lever steps
    0/1 have available: the existing public `interrupt_speaking()`."""
    mixer = _ScriptedMixerMusic(block_at=block_at)
    motor, _, _ = _make_motor(mixer_music=mixer)
    text = " ".join(_sentences(n))
    holder: dict = {}

    def _drive():
        holder["outcome"] = motor._hablar_impl(text, source=source)

    t = threading.Thread(target=_drive, daemon=True)
    t.start()
    assert mixer.entered_block.wait(5.0), "producer/consumer never reached the target fragment"
    motor.interrupt_speaking()
    t.join(5.0)
    assert not t.is_alive(), "interruption never unblocked _hablar_impl"
    return holder["outcome"]


def _partition(outcome):
    n = len(outcome.chunks)
    pending = list(range(outcome.cursor, n))
    return outcome.spoken, outcome.skipped, pending


def _run_drop_ahead_of_cut(n: int = 6, fail_idx: int = 4, block_at: int = 1,
                            source: str = "kira-agenda:t3"):
    """Composes the two scenarios the shipped tests keep apart (design §12):
    a producer-side synthesis failure (the `_drop` path) at an index AHEAD of
    where the consumer is mid-playback, then a real interruption while the
    consumer is still stuck there. `drop_ready` is set inside the failing
    synth itself so the interrupt is never issued before the drop has run --
    the drop provably precedes the cut."""
    sentences = _sentences(n)
    fail_text = sentences[fail_idx]
    drop_ready = threading.Event()

    def _synth(text, out_path):
        if text == fail_text:
            drop_ready.set()
            return False  # Piper synthesis failure -> the _drop path, producer ahead of the cut
        return _write_wav(out_path)

    mixer = _ScriptedMixerMusic(block_at=block_at)
    motor, _, _ = _make_motor(mixer_music=mixer, synthesize=_synth)
    text = " ".join(sentences)
    holder: dict = {}

    def _drive():
        holder["outcome"] = motor._hablar_impl(text, source=source)

    t = threading.Thread(target=_drive, daemon=True)
    t.start()
    assert mixer.entered_block.wait(5.0), "consumer never reached the target fragment"
    assert drop_ready.wait(5.0), "producer never reached the scripted failure"
    motor.interrupt_speaking()
    t.join(5.0)
    assert not t.is_alive(), "interruption never unblocked _hablar_impl"
    return holder["outcome"]


def _run_producer_death(monkeypatch, n: int = 4, die_idx: int = 1, source: str = "direct"):
    """Kills the producer thread outright (an uncaught exception from Piper,
    outside any per-chunk try) before it ever reaches the "FIN" sentinel.
    The consumer then times out on cola_audios.get() -- shrink
    TTS_AUDIO_QUEUE_TIMEOUT so the test doesn't wait out the real ~195s
    value (timing knob only, no logic change)."""
    monkeypatch.setattr(llm_engine, "TTS_AUDIO_QUEUE_TIMEOUT", 0.5)
    sentences = _sentences(n)
    die_text = sentences[die_idx]

    def _synth(text, out_path):
        if text == die_text:
            raise RuntimeError("producer killed before FIN")
        return _write_wav(out_path)

    motor, _, _ = _make_motor(mixer_music=_ScriptedMixerMusic(), synthesize=_synth)
    return motor._hablar_impl(" ".join(sentences), source=source)


# ──────────────────────────────────────────────────────────────────────────
# (1) clean full playback
# ──────────────────────────────────────────────────────────────────────────


def test_clean_full_playback_covers_every_fragment():
    outcome = _run_clean(4)

    assert outcome.cursor == len(outcome.chunks) == 4
    assert outcome.spoken == [0, 1, 2, 3]
    assert outcome.skipped == []
    assert outcome.interrupted is False
    assert outcome.error is None


# ──────────────────────────────────────────────────────────────────────────
# (2) mid-audio interruption — the CURSOR TRAP
# ──────────────────────────────────────────────────────────────────────────


def test_mid_audio_interruption_captures_cut_fragment_as_cursor():
    outcome = _run_interrupted(n=5, block_at=2)

    assert outcome.interrupted is True
    assert outcome.cursor == 2, "cursor must be the in-flight idx, never chunks_played"
    assert 2 not in outcome.spoken
    assert outcome.spoken == [0, 1]


# ──────────────────────────────────────────────────────────────────────────
# (3) synthesis failure — exactly one [SPEECH_LOST] line, rest still plays
# ──────────────────────────────────────────────────────────────────────────


def test_synthesis_failure_drops_exactly_one_fragment_and_continues(caplog):
    caplog.set_level(logging.INFO, logger="OpenCohost")
    sentences = _sentences(4)
    fail_text = sentences[1]

    def _synth(text, out_path):
        if text == fail_text:
            return False  # Piper synthesis failure -> the _drop path
        return _write_wav(out_path)

    motor, _, _ = _make_motor(mixer_music=_ScriptedMixerMusic(), synthesize=_synth)

    outcome = motor._hablar_impl(" ".join(sentences), source="direct")

    assert outcome.skipped == [1]
    assert outcome.interrupted is False
    assert outcome.cursor == len(outcome.chunks) == 4
    assert outcome.spoken == [0, 2, 3], "later fragments still play after the drop"

    lost_lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("[SPEECH_LOST]")]
    assert len(lost_lines) == 1, lost_lines
    assert re.fullmatch(r"\[SPEECH_LOST\] idx=1 reason=\S+ source=direct", lost_lines[0])
    assert "prueba" not in lost_lines[0], "PRIVACY: no fragment text in the telemetry line"


# ──────────────────────────────────────────────────────────────────────────
# (4) press hook — fail-open, never blocks the press path
# ──────────────────────────────────────────────────────────────────────────


class _FakeSession:
    """Minimal PttSession stand-in whose start() succeeds into listening with
    no real websocket (mirrors tests/test_ptt_cue.py::_FakeSession)."""

    def __init__(self, ws_uri, on_flush, on_event, on_close, **kwargs):
        self.session_id = "ptt_fake"
        self.state = "listening"

    def start(self):
        pass


def test_press_precheck_raising_still_completes_start():
    controller = PttController(
        "ws://test/whisperlive",
        MagicMock(),
        MagicMock(),
        session_factory=_FakeSession,
        on_press_precheck=lambda: (_ for _ in ()).throw(RuntimeError("precheck boom")),
    )
    # Fail-open: a raising press precheck must never block/break the press path.
    assert controller.start() == "ptt_fake"


# ──────────────────────────────────────────────────────────────────────────
# (5) partition invariant — spoken + skipped + pending covers every index
# ──────────────────────────────────────────────────────────────────────────


def test_partition_invariant_holds_for_clean_and_interrupted_runs(monkeypatch):
    clean = _run_clean(4)
    interrupted = _run_interrupted(n=5, block_at=3)
    drop_ahead = _run_drop_ahead_of_cut()
    producer_death = _run_producer_death(monkeypatch)

    for outcome in (clean, interrupted, drop_ahead, producer_death):
        spoken, skipped, pending = _partition(outcome)
        n = len(outcome.chunks)
        assert sorted(spoken + skipped + pending) == list(range(n)), (spoken, skipped, pending)
        assert not (set(spoken) & set(skipped))
        assert not (set(spoken) & set(pending))
        assert not (set(skipped) & set(pending))


# ──────────────────────────────────────────────────────────────────────────
# (6) drop ahead of a mid-audio cut — the drop must return to PENDING, not
#     double-count in `skipped` (design §12, defect 2)
# ──────────────────────────────────────────────────────────────────────────


def test_drop_ahead_of_cut_returns_to_pending(caplog):
    caplog.set_level(logging.INFO, logger="OpenCohost")
    outcome = _run_drop_ahead_of_cut()
    spoken, skipped, pending = _partition(outcome)

    assert outcome.interrupted is True
    assert outcome.cursor == 1
    assert spoken == [0]
    assert skipped == [], "index 4's drop landed ahead of the cut -> pending, not skipped"
    assert pending == list(range(1, 6))
    assert sorted(spoken + skipped + pending) == list(range(6))

    lost_lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("[SPEECH_LOST]")]
    idx4_lines = [ln for ln in lost_lines if "idx=4" in ln]
    assert len(idx4_lines) == 1, idx4_lines


# ──────────────────────────────────────────────────────────────────────────
# (7) producer death — the consumer's queue.Empty exit must report an honest
#     cursor, never the clean-completion signature (design §12, defect 1)
# ──────────────────────────────────────────────────────────────────────────


def test_producer_death_yields_honest_cursor(monkeypatch):
    outcome = _run_producer_death(monkeypatch)
    spoken, skipped, pending = _partition(outcome)

    assert outcome.error == "queue_empty_timeout"
    assert outcome.interrupted is False
    assert outcome.cursor == 1, "last_idx(0) + 1 -- idx 1 never reached the consumer"
    assert spoken == [0]
    assert skipped == []
    assert pending == [1, 2, 3]
    assert sorted(spoken + skipped + pending) == list(range(4))


# ──────────────────────────────────────────────────────────────────────────
# (8) the PRE-PLAY interrupt exits must not be silent — the mid-playback cut
#     logged [SPEECH_STACK] cut, the two exits before any audio started did
#     not, so a turn cut in that window left no trace at all in the log
# ──────────────────────────────────────────────────────────────────────────


def test_pre_dequeue_cut_logs_speech_stack_line(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="OpenCohost")
    motor, _, _ = _make_motor(mixer_music=_ScriptedMixerMusic())
    # Closure B1's real lever: a pause pending for the active router job clears
    # `_speaking` right after _hablar_impl re-arms it, so the consumer hits the
    # pre-dequeue guard on its very first pass -- before any fragment is
    # dequeued, let alone played.
    monkeypatch.setattr(motor, "_speech_pause_pending", lambda: True)

    outcome = motor._hablar_impl(" ".join(_sentences(3)), source="kira-agenda:t9")

    assert outcome.interrupted is True
    assert outcome.cursor == 0, "nothing was in hand -- last_idx(-1) + 1"
    assert outcome.spoken == []
    assert motor.pygame.mixer.music.load_count == 0, "no fragment ever played"

    cut_lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("[SPEECH_STACK] cut")]
    assert len(cut_lines) == 1, cut_lines
    assert re.fullmatch(
        r"\[SPEECH_STACK\] cut source=kira-agenda:t9 cursor=0 at=pre_dequeue", cut_lines[0]
    )
    assert "Fragmento" not in cut_lines[0], "PRIVACY: no fragment text in the telemetry line"
