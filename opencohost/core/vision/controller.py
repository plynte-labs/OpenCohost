"""VisionController: owns the vision pieces for one host (vision-on-demand V1.5).

Built by EngineHost; the engine only reads ``controller.step``. Nothing is
constructed while vision is disabled. Enabling builds the gate, the turn
step and the dedicated vision daemon handle, and starts the daemon on a
background thread: never on startup's critical path, never inside a turn.

V1.8 state machine (exposed as ``status()["state"]``)::

    off -> starting -> warming -> ready
              |           |
              +-> failed <+

- ``starting``: the background thread is bringing the daemon up.
- ``warming``: the daemon answers and the VL model is being loaded with a
  load-only call (``VisionDaemon.warm``), so a look never pays the ~3-7 s
  cold load inside the describe budget (the V1.7 ``describe_timeout``).
- ``ready``: the model is resident (``keep_alive`` -1, see describer.py).
- ``failed``: ``last_error`` names why (a daemon reason code,
  ``model_missing`` or ``warm_failed``). A look retries in the background.

A look while not ``ready`` fails open BEFORE the bridge line with
``daemon_not_ready`` / ``model_warming`` / ``model_missing``. While ``ready``,
the turn re-checks residency through ``/api/ps`` (cached ``ps_ttl_s``) and
re-warms in the background when the model was evicted.

V1.11: a single ``/api/ps`` miss is not trusted. The V1.7 second run logged
``ready->warming`` re-warms that loaded in ~200 ms, i.e. the model was still
resident. The model counts as not loaded only after ``PS_MISSES_TO_UNLOAD``
consecutive misses; a turn that sees one miss confirms it with a fresh,
uncached probe instead of failing open.

V1.11 also keeps an in-memory ring of the last looks' frames (only while
``show_thumbnails`` is on), cleared on disable, shutdown and when the owner
turns thumbnails off. Vision transparency: the ring holds the frames AS SENT to
the model (not a re-downscaled preview) and the look's description text, which
expires with them and is served only through ``description(look_id)``.

cloud-providers-vision P3: a ``vision_source`` other than ``local`` has no
daemon at all (nothing is built, started or warmed) and is ``ready`` as soon as
it is built; the describer (cloud model, or none for ``chat_model``) comes from
the same factory. Changing the source, the cloud preset or the cloud model
rebuilds the pieces live, with no restart.

Disable, shutdown and a model/port change unload the model (keep_alive 0)
before stopping the daemon; ``VisionDaemon.stop`` only kills an owned process,
so an external daemon is just unloaded.

Thread model: ``apply``/``arm_look``/``status``/``shutdown`` are called from
API threads, ``step.run`` from the engine thread, the start/warm worker from
its own thread. The lock guards the references and the state; the step is
swapped atomically and a turn keeps the reference it started with. A worker
whose daemon was torn down meanwhile does nothing, and unloads a model it
finished loading after the teardown.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Optional

from opencohost.core.vision.describer import VISION_KEEP_ALIVE, VisionDescribeError
from opencohost.core.vision.frames import ThumbnailRing, to_data_url
from opencohost.core.vision.gate import MODE_DEFAULT, CompositeGate, ExplicitTriggerGate, RuleVisionGate
from opencohost.core.vision.turn_step import OUTCOME_OK, VisionTurnResult, VisionTurnStep

logger = logging.getLogger("OpenCohost")

REASON_DAEMON_NOT_READY = "daemon_not_ready"
REASON_MODEL_WARMING = "model_warming"
REASON_MODEL_MISSING = "model_missing"
REASON_WARM_FAILED = "warm_failed"

STATE_OFF = "off"
STATE_STARTING = "starting"
STATE_WARMING = "warming"
STATE_READY = "ready"
STATE_FAILED = "failed"

_READY_STATES = ("running", "external")
DEFAULT_PS_TTL_S = 5.0
DEFAULT_WARM_TIMEOUT_S = 120.0
PS_MISSES_TO_UNLOAD = 2
# runtime-fixes-20261110 T3: how long a pasted image may wait for a cold vision
# stack. The 2026-11-09 log shows the daemon taking ~13 s to answer and the
# model load measuring 5-12.5 s across sessions (~26 s worst case together),
# so 40 s covers a cold start with margin; the per-call describe deadline
# (settings, 1-30 s) is separate and unchanged.
DEFAULT_COLD_START_WAIT_S = 40.0


def _spawn_thread(fn: Callable[[], None]) -> None:
    threading.Thread(target=fn, daemon=True, name="vision-daemon-start").start()


def _is_local(values: Any) -> bool:
    return getattr(values, "vision_source", "local") == "local"


def _cloud_key(values: Any) -> tuple:
    """What a built describer depends on besides the local port and model."""
    return (
        getattr(values, "vision_source", "local"),
        getattr(values, "vision_cloud_preset", ""),
        getattr(values, "vision_cloud_model", ""),
    )


def _ms(seconds: float) -> int:
    return max(0, int(round(seconds * 1000)))


class VisionController:
    def __init__(
        self,
        *,
        daemon_factory: Callable[[Any], Any],
        describer_factory: Callable[[Any, str], Any],
        capture_factory: Callable[[], Optional[Any]],
        speak_bridge: Callable[[str], None],
        on_looking: Optional[Callable[[bool], None]] = None,
        on_result: Optional[Callable[[dict], None]] = None,
        default_language: str = "es",
        spawn: Callable[[Callable[[], None]], None] = _spawn_thread,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        ps_ttl_s: float = DEFAULT_PS_TTL_S,
        keep_alive: int | str = VISION_KEEP_ALIVE,
        warm_timeout_s: float = DEFAULT_WARM_TIMEOUT_S,
        sleep: Callable[[float], None] = time.sleep,
        foreground_probe: Optional[Any] = None,
        on_describing: Optional[Callable[[], None]] = None,
        speak_progress: Optional[Callable[[str], None]] = None,
        direct_check: Optional[Callable[[], bool]] = None,
    ) -> None:
        self._daemon_factory = daemon_factory
        self._describer_factory = describer_factory
        self._capture_factory = capture_factory
        self._speak_bridge = speak_bridge
        self._on_looking = on_looking
        self._on_result = on_result
        self._default_language = default_language
        self._spawn = spawn
        self._clock = clock
        self._wall_clock = wall_clock
        self._ps_ttl_s = ps_ttl_s
        self._keep_alive = keep_alive
        self._warm_timeout_s = warm_timeout_s
        self._sleep = sleep
        self._foreground_probe = foreground_probe
        self._on_describing = on_describing
        self._speak_progress = speak_progress
        # P2: asked by each look whether it goes straight to a cloud chat model.
        self._direct_check = direct_check

        self._lock = threading.RLock()
        self._settings: Any = None
        self._step: Optional[VisionTurnStep] = None
        self._explicit: Optional[ExplicitTriggerGate] = None
        self._daemon: Any = None
        self._daemon_model: Optional[str] = None  # the model this daemon serves
        self._capture_available = False
        self._model_installed: Optional[bool] = None
        self._starting = False
        self._state = STATE_OFF
        self._last_error: Optional[str] = None
        self._loaded_cache: Optional[tuple[float, Optional[bool]]] = None
        self._last_result: Optional[dict] = None
        self._ps_misses = 0
        self._thumbnails = ThumbnailRing()

    # -- public API -------------------------------------------------------------

    @property
    def step(self) -> Optional[VisionTurnStep]:
        with self._lock:
            return self._step

    @property
    def enabled(self) -> bool:
        """True while vision is on (any state but off); the prompt reads it (V1.24)."""
        with self._lock:
            return self._state != STATE_OFF

    def apply(self, values: Any) -> None:
        """Enable, disable or retune vision from a ``VisionSettings``."""
        with self._lock:
            previous, self._settings = self._settings, values
            if not getattr(values, "show_thumbnails", False):
                self._thumbnails.clear()
            if not values.enabled:
                self._teardown()
                return
            rebuild = (
                self._step is None
                or previous is None
                or previous.port != values.port
                or previous.model != values.model
                or _cloud_key(previous) != _cloud_key(values)
            )
            if rebuild:
                self._teardown()
                self._build(values)
            else:
                self._retune(values)
            daemon = self._daemon
        self._kick(daemon)

    def arm_look(self, mode: Optional[str] = None) -> bool:
        """Arm the one-shot "look at this" flag. False while vision is off.
        ``mode`` optionally names the look mode (clip/text, V1.26/V1.27)."""
        with self._lock:
            if self._explicit is None:
                return False
            self._explicit.arm_look(mode)
            return True

    def status(self) -> dict:
        with self._lock:
            values = self._settings
            daemon = self._daemon
            step = self._step
            explicit = self._explicit
            installed = self._model_installed
            capture = self._capture_available
            state = self._state
            last_error = self._last_error
            last_result = dict(self._last_result) if self._last_result is not None else None
        daemon_state = "stopped"
        if daemon is not None:
            try:
                daemon_state = daemon.status().state
            except Exception:
                daemon_state = "failed"
        if state == STATE_READY and daemon is None:
            model_loaded = None  # no local daemon for a non-local source
        elif state == STATE_READY:
            model_loaded = self._model_loaded(daemon)
            state = self._honest_ready_state(daemon, daemon_state, model_loaded)
        elif state == STATE_WARMING:
            model_loaded = False
        else:
            model_loaded = None
        return {
            "enabled": bool(values is not None and values.enabled),
            "state": state,
            "last_error": last_error,
            "daemon_state": daemon_state,
            "model_installed": installed,
            "model_loaded": model_loaded,
            "capture_available": capture,
            "armed": bool(explicit is not None and explicit.is_armed()),
            "looking": bool(step is not None and step.looking),
            "model": getattr(values, "model", None),
            "port": getattr(values, "port", None),
            "last_result": last_result,
        }

    def _honest_ready_state(self, daemon: Any, daemon_state: str, model_loaded: Optional[bool]) -> str:
        """``ready`` means a describe would be served warm: the model is
        resident. A confirmed eviction (debounced ``/api/ps``) reads as
        ``warming`` and a dead daemon as ``starting``; both kick the same
        background recovery a turn would, so the owner never has to send an
        image to find out. An inconclusive ``/api/ps`` on a running daemon keeps
        ``ready`` (the describe path re-checks before using it)."""
        if model_loaded is False:
            self._restart(daemon, STATE_WARMING)
            return STATE_WARMING
        if model_loaded is None and daemon_state not in _READY_STATES:
            self._restart(daemon, STATE_STARTING)
            return STATE_STARTING
        return STATE_READY

    def describe_image(self, image: bytes, question: str, *, max_wait_s: float = DEFAULT_COLD_START_WAIT_S) -> str:
        """Describe ONE caller-supplied image (the operator's pasted image) with
        the configured describer. Blocking; raises ``VisionDescribeError``.

        Reuses the describer, the readiness check and the describe timeout of
        the screen look; there is no second Ollama client. PRIVACY: the bytes
        are only handed to the describer; nothing is stored or logged here."""
        step = self.step
        if step is None:
            raise VisionDescribeError("unavailable", "vision_off")
        not_ready = self._ready()
        waited = 0.0
        while not_ready == REASON_MODEL_WARMING and waited < max_wait_s:
            self._sleep(0.5)
            waited += 0.5
            not_ready = self._ready()
        if not_ready:
            logger.info("[VISION] describe_image not_ready=%s waited_s=%.1f", not_ready, waited)
            raise VisionDescribeError("unavailable", str(not_ready))
        timeout_s = float(getattr(self._settings, "describe_timeout_s", 15.0))
        return step._describer.describe(image, question, timeout_s=timeout_s)

    def shutdown(self) -> None:
        with self._lock:
            self._teardown()

    def thumbnails(self, look_id: str) -> Optional[list[str]]:
        """The look's thumbnails as JPEG data URLs, or None when the look is
        unknown or expired, or thumbnails are off."""
        with self._lock:
            values = self._settings
            if values is None or not getattr(values, "show_thumbnails", False):
                return None
            items = self._thumbnails.get(look_id)
        if items is None:
            return None
        return [to_data_url(item) for item in items]

    def description(self, look_id: str) -> Optional[str]:
        """The look's description text, or None when the look is unknown or
        expired or thumbnails are off. It lives and expires with the frames
        (same ring); never logged, never persisted, never in ``status()``."""
        with self._lock:
            values = self._settings
            if values is None or not getattr(values, "show_thumbnails", False):
                return None
        return self._thumbnails.get_description(look_id)

    def _store_thumbnails(self, look_id: str, thumbnails: list) -> None:
        """Step callback: keep the sent frames' previews, memory only."""
        with self._lock:
            values = self._settings
            if values is None or not getattr(values, "show_thumbnails", False):
                return
            self._thumbnails.put(look_id, thumbnails)

    # -- build / teardown ---------------------------------------------------------

    def _build(self, values: Any) -> None:
        self._explicit = ExplicitTriggerGate(values.look_on_ptt, values.capture_delay_s)
        gate = CompositeGate(self._explicit, self._rule_gate(values))
        if _is_local(values):
            self._daemon = self._daemon_factory(values)
            self._daemon_model = values.model
            describer = self._describer_factory(values, self._daemon.base_url)
        else:
            self._daemon = None
            self._daemon_model = None
            describer = self._describer_factory(values, None)
        try:
            capture = self._capture_factory()
        except Exception:
            capture = None
        self._capture_available = capture is not None
        self._model_installed = None
        self._step = VisionTurnStep(
            gate,
            capture,
            describer,
            self._speak_bridge,
            settings=values,
            ready_check=self._ready,
            on_looking=self._on_looking,
            on_result=self._record_result,
            default_language=self._default_language,
            sleep=self._sleep,
            foreground_probe=self._foreground_probe,
            on_thumbnails=self._store_thumbnails,
            on_describing=self._on_describing,
            speak_progress=self._speak_progress,
            direct_check=self._direct_check,
        )
        self._transition(STATE_STARTING if _is_local(values) else STATE_READY)
        logger.info(
            "[VISION] enabled port=%d capture_available=%s rule_gate=%s source=%s",
            values.port, self._capture_available, values.rule_gate_enabled,
            getattr(values, "vision_source", "local"),
        )

    def _retune(self, values: Any) -> None:
        explicit = self._explicit
        explicit.look_on_ptt = values.look_on_ptt
        explicit.capture_delay_s = values.capture_delay_s
        self._step.gate = CompositeGate(explicit, self._rule_gate(values))
        self._step.update_settings(values)

    @staticmethod
    def _rule_gate(values: Any) -> Optional[RuleVisionGate]:
        return RuleVisionGate(values.capture_delay_s) if values.rule_gate_enabled else None

    def _teardown(self) -> None:
        daemon, self._daemon = self._daemon, None
        model, self._daemon_model = self._daemon_model, None
        was_enabled = self._step is not None
        self._step = None
        self._explicit = None
        self._model_installed = None
        self._capture_available = False
        self._loaded_cache = None
        self._ps_misses = 0
        self._thumbnails.clear()
        if daemon is not None:
            if model:
                try:
                    daemon.unload(model)  # free the VRAM before the process goes
                except Exception:
                    logger.warning("[VISION] model unload failed")
            try:
                daemon.stop()  # stops only a process this host spawned
            except Exception:
                logger.warning("[VISION] daemon stop failed")
        if was_enabled:
            self._transition(STATE_OFF)
            logger.info("[VISION] disabled")

    # -- start + warm (background) ------------------------------------------------

    def _kick(self, daemon: Any) -> None:
        """Start the daemon and warm the model off-thread; single-flight."""
        if daemon is None:
            return
        with self._lock:
            if self._starting or daemon is not self._daemon:
                return
            self._starting = True
            model = self._daemon_model

        def _worker() -> None:
            try:
                self._start_and_warm(daemon, model)
            except Exception:
                logger.warning("[VISION] daemon start failed")
                self._transition_if_current(daemon, STATE_FAILED, REASON_DAEMON_NOT_READY)
            finally:
                with self._lock:
                    self._starting = False

        self._spawn(_worker)

    def _start_and_warm(self, daemon: Any, model: str) -> None:
        if not self._is_current(daemon):
            return
        status = daemon.ensure_running()
        state = status.state
        if state not in _READY_STATES:
            logger.info("[VISION] daemon state=%s model_installed=None", state)
            reason = getattr(status, "last_error", None) or REASON_DAEMON_NOT_READY
            self._transition_if_current(daemon, STATE_FAILED, reason)
            return
        installed = bool(daemon.model_installed(model))
        logger.info("[VISION] daemon state=%s model_installed=%s", state, installed)
        with self._lock:
            if daemon is not self._daemon:
                return
            self._model_installed = installed
        if not installed:
            self._transition_if_current(daemon, STATE_FAILED, REASON_MODEL_MISSING)
            return
        if not self._transition_if_current(daemon, STATE_WARMING):
            return
        started = self._clock()
        ok = bool(daemon.warm(model, keep_alive=self._keep_alive, timeout_s=self._warm_timeout_s))
        extra = f"model_load_ms={_ms(self._clock() - started)}"
        with self._lock:
            current = daemon is self._daemon
            if current:
                self._loaded_cache = (self._clock(), True) if ok else None
                self._ps_misses = 0
                if ok:
                    self._transition(STATE_READY, extra=extra)
                else:
                    self._transition(STATE_FAILED, REASON_WARM_FAILED, extra=extra)
        if ok and not current:
            # Torn down while loading: the teardown's unload ran before this
            # load finished, so the model would stay resident. Unload again.
            try:
                daemon.unload(model)
            except Exception:
                logger.warning("[VISION] late model unload failed")

    def _is_current(self, daemon: Any) -> bool:
        with self._lock:
            return daemon is self._daemon

    def _transition_if_current(self, daemon: Any, state: str, error: Optional[str] = None) -> bool:
        with self._lock:
            if daemon is not self._daemon:
                return False
            self._transition(state, error)
            return True

    def _transition(self, state: str, error: Optional[str] = None, *, extra: str = "") -> None:
        """Set the state; one ``[VISION] state=a->b`` line per change. Lock held."""
        if state == self._state and error == self._last_error:
            return
        previous = self._state
        self._state = state
        self._last_error = error
        logger.info(
            "[VISION] state=%s->%s error=%s%s",
            previous, state, error or "none", f" {extra}" if extra else "",
        )

    # -- readiness (turn path) ------------------------------------------------------

    def _model_loaded(self, daemon: Any, *, fresh: bool = False) -> Optional[bool]:
        """``/api/ps`` residency, cached ``ps_ttl_s`` so turns and polls stay cheap.

        Debounced (V1.11): False only after ``PS_MISSES_TO_UNLOAD`` consecutive
        misses; a single miss still reads as loaded. ``fresh`` bypasses the cache.
        """
        if daemon is None:
            return None
        now = self._clock()
        with self._lock:
            cached = self._loaded_cache
            model = self._daemon_model
        if not fresh and cached is not None and now - cached[0] < self._ps_ttl_s:
            return cached[1]
        try:
            loaded = daemon.model_loaded(model)
        except Exception:
            loaded = None
        loaded = None if loaded is None else bool(loaded)
        with self._lock:
            if daemon is not self._daemon:
                return loaded
            if loaded is False:
                self._ps_misses += 1
                if self._ps_misses < PS_MISSES_TO_UNLOAD:
                    loaded = True  # one miss is not trusted; see the module docstring
            elif loaded is True:
                self._ps_misses = 0
            self._loaded_cache = (now, loaded)
        return loaded

    def _pending_miss(self) -> bool:
        with self._lock:
            return 0 < self._ps_misses < PS_MISSES_TO_UNLOAD

    def _ready(self) -> Optional[str]:
        """``ready_check`` for the step: a reason code, or None when ready."""
        with self._lock:
            daemon = self._daemon
            state = self._state
            last_error = self._last_error
            daemonless = self._step is not None and not _is_local(self._settings)
        if daemonless and state != STATE_OFF:
            return None  # cloud sources have no local model to wait for
        if daemon is None or state == STATE_OFF:
            return REASON_DAEMON_NOT_READY
        if state in (STATE_STARTING, STATE_WARMING):
            # V1.23: a daemon still coming up is "warming" to the owner; the
            # step waits a bounded time for it instead of failing at once.
            return REASON_MODEL_WARMING
        if state == STATE_FAILED:
            self._kick(daemon)  # retry in the background; this turn fails open
            if last_error == REASON_MODEL_MISSING:
                return REASON_MODEL_MISSING
            return REASON_DAEMON_NOT_READY
        try:
            daemon_state = daemon.status().state
        except Exception:
            daemon_state = "failed"
        loaded = self._model_loaded(daemon) if daemon_state in _READY_STATES else None
        if loaded and self._pending_miss():
            # One unconfirmed miss: ask /api/ps again, uncached, before trusting it.
            loaded = self._model_loaded(daemon, fresh=True)
        if loaded is None:
            # The daemon died or stopped answering: restart, then re-warm.
            self._restart(daemon, STATE_STARTING)
            return REASON_DAEMON_NOT_READY
        if not loaded:
            # Evicted or unloaded behind our back: re-warm in the background.
            self._restart(daemon, STATE_WARMING)
            return REASON_MODEL_WARMING
        return None

    def _restart(self, daemon: Any, state: str) -> None:
        with self._lock:
            if daemon is not self._daemon:
                return
            self._loaded_cache = None
            self._ps_misses = 0
            self._transition(state)
        self._kick(daemon)

    # -- per-look result ----------------------------------------------------------

    def _record_result(self, result: VisionTurnResult) -> None:
        """Keep the look's result for ``status()`` and push the outcome event.

        Codes and latencies only: never the question, the description or the image.
        """
        timings = result.timings or {}
        # Published vocabulary is normal|text|clip; the gate calls "normal" default.
        mode = "normal" if result.mode == MODE_DEFAULT else result.mode
        record = {
            "id": result.look_id,
            "outcome": result.outcome,
            "capture_ms": timings.get("capture_ms"),
            "describe_ms": timings.get("describe_ms"),
            "at": self._wall_clock(),
            "frames_captured": result.frames_captured,
            "frames_sent": result.frames_sent,
            "self_capture_skipped": result.self_capture_skipped,
            "mode": mode,
        }
        with self._lock:
            self._last_result = record
            if result.description and getattr(self._settings, "show_thumbnails", False):
                self._thumbnails.set_description(result.look_id, result.description)
            if result.outcome == OUTCOME_OK:
                self._loaded_cache = (self._clock(), True)  # a describe just ran on it
                self._ps_misses = 0
        callback = self._on_result
        if callback is not None:
            try:
                # Codes, ids and counts only: never the question, text or frames.
                callback({
                    "outcome": record["outcome"],
                    "id": record["id"],
                    "describe_ms": record["describe_ms"],
                    "frames_sent": record["frames_sent"],
                    "mode": mode,
                })
            except Exception:
                logger.debug("[VISION] result event failed", exc_info=False)
