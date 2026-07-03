"""Standalone engine host for the Kira FastAPI API layer (Phase 1).

The ONLY file in `opencohost/api/` that imports the core engine classes.
`EngineHost` constructs and owns a private `MotorVocalIA` + `HealthMonitor`
pair for a standalone process — wired the same way `app_shell.py` wires
the Tk app's engine, but never shared with it.
"""

import logging
import os
import tempfile
import threading

import ollama

from opencohost.core.health_monitor import HealthMonitor
from opencohost.core.llm_engine import MotorVocalIA
from opencohost.core.music_library import MusicLibrary
from opencohost.smart_aggregator.aggregator import Aggregator
from opencohost.smart_aggregator.kira_agenda_controller import KiraAgendaController

try:
    import msvcrt
except ImportError:  # pragma: no cover - Phase 1 targets Windows only
    msvcrt = None

_LOCK_PATH = os.path.join(tempfile.gettempdir(), "opencohost_api_engine.lock")

# opencohost/api/engine_host.py -> opencohost/ -> opencohost/config/smart_aggregator.yaml
_AGGREGATOR_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "config",
    "smart_aggregator.yaml",
)

_logger = logging.getLogger(__name__)


class _Drain:
    """No-op log sink.

    PRIVACY (design v2.1 B-MF1): the engine only ever calls `.put()` on this
    queue-like object (it never reads back). A pure drain both prevents
    unbounded growth AND, critically, MUST NOT log or persist the message —
    `log_queue.put` sites carry raw generated dialogue (llm_engine.py).
    Logging it here would leak Kira's conversation content to disk.
    """

    def put(self, _msg):
        pass


def _noop_event(*_args, **_kwargs):
    pass


class EngineHost:
    """Owns a standalone MotorVocalIA + HealthMonitor pair for this process."""

    def __init__(self, lock_path: str = _LOCK_PATH):
        self._lock_path = lock_path
        self._lock_fd = None
        self.motor = None
        self.monitor = None
        self.aggregator = None
        # RF3 control-plane single-flight guard for POST .../connect —
        # ponytail: one global lock, not per-source, since one process owns
        # exactly one Aggregator.
        self.aggregator_lock = threading.Lock()
        self.agenda = None
        # KiraAgendaController is plain lists/attrs, not thread-safe — one
        # global lock, mirrors aggregator_lock (one process, one controller).
        self.agenda_lock = threading.Lock()
        self.music_library = None

    def start(self) -> None:
        self._acquire_lock()
        try:
            self.motor = MotorVocalIA(_Drain(), ui_callback=_noop_event)
            self.monitor = HealthMonitor()
            self.motor.health_monitor = self.monitor  # mirrors app_shell.py:196-197
            self.motor.start()
            try:
                self.monitor.qwen_manager.attach_existing()
            except Exception:
                pass
            self.monitor.start()
            # RF3 stream chat-live control-plane: resilient by design — a
            # missing config or optional chat-source dependency must not
            # brick the engine/profiles/commands surface.
            try:
                self.aggregator = Aggregator(config_path=_AGGREGATOR_CONFIG_PATH, llm_interface=None)
            except Exception:
                self.aggregator = None
                _logger.exception("Aggregator construction failed; stream chat-live control-plane disabled")
            # WS3 slice 3: headless agenda controller — pure state, no threads,
            # no Ollama/TTS/OBS/UI calls. A construction failure must not brick
            # the rest of the host, mirrors the Aggregator pattern above.
            try:
                self.agenda = KiraAgendaController()
            except Exception:
                self.agenda = None
                _logger.exception("KiraAgendaController construction failed; agenda endpoints disabled")
            # WS3 slice 4: headless music library — pure file/JSON state read,
            # no pygame, no audio device (server-side `request_mood` playback
            # is deferred). A construction failure must not brick the rest of
            # the host, mirrors the Aggregator/Agenda pattern above.
            try:
                self.music_library = MusicLibrary()
                self.music_library.load()
            except Exception:
                self.music_library = None
                _logger.exception("MusicLibrary construction failed; music endpoints disabled")
        except Exception:
            self.stop()
            raise

    def stop(self) -> None:
        """Best-effort teardown — each step swallows exceptions."""
        # ponytail: KiraAgendaController and MusicLibrary are both pure state
        # (no threads, no close()/reset() method) — nothing to tear down here.
        if self.aggregator is not None:
            try:
                # connect() spawns a daemon source thread; disconnect() joins
                # it. Must run before the motor/lock teardown below.
                self.aggregator.disconnect()
            except Exception:
                pass
        if self.motor is not None:
            try:
                self.motor.command_queue.put(None)  # only engine-thread stop path
            except Exception:
                pass
        if self.monitor is not None:
            try:
                self.monitor.stop()
            except Exception:
                pass
        try:
            model = getattr(self.motor, "current_model", None)
            if model:
                ollama.generate(model=model, prompt="", keep_alive=0)
        except Exception:
            pass
        self._release_lock()

    def _acquire_lock(self) -> None:
        fd = os.open(self._lock_path, os.O_CREAT | os.O_RDWR)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            os.close(fd)
            raise RuntimeError(
                f"Another OpenCohost API engine host is already running (lock: {self._lock_path})"
            ) from exc
        os.write(fd, str(os.getpid()).encode())
        self._lock_fd = fd

    def _release_lock(self) -> None:
        if self._lock_fd is None:
            return
        try:
            os.lseek(self._lock_fd, 0, os.SEEK_SET)
            msvcrt.locking(self._lock_fd, msvcrt.LK_UNLCK, 1)
        except Exception:
            pass
        try:
            os.close(self._lock_fd)
        except Exception:
            pass
        self._lock_fd = None
