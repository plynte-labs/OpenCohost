"""UI-free OBS push chain for the headless API engine host (FIX-B).

The API-hosted ``MotorVocalIA`` (opencohost/api/engine_host.py) has no
``AvatarStateBridge``/``OBSClient`` — its ``ui_callback`` was a no-op, so
speaking/processing events never reached OBS. ``ObsRuntime`` restores that
push chain for the headless process by mirroring the CTK implementation
(opencohost/ui/obs_lifecycle.py + opencohost/ui/motor_event_handlers.py)
WITHOUT importing ``opencohost.ui`` — the engine host must never pull in the
desktop UI (hard contract, see opencohost/api/main.py module docstring).

Chain parity with the CTK app:
  * A private ``AvatarStateBridge`` is owned here; the ``OBSClient`` subscribes
    to it and pushes image-source updates on every state change.
  * ``start_from_config`` mirrors ``obs_lifecycle.start_from_config`` minus Tk:
    a disabled config stops the runtime and returns False; an enabled config
    builds an ``OBSClient`` and starts ONE cancellable daemon retry loop that
    connects every ``retry_delay`` seconds and, on the first success,
    subscribes the bridge and pushes the current state.
  * ``handle_motor_event`` maps motor status strings to ``AvatarState``
    mirroring ``motor_event_handlers`` (``speaking_start`` -> SPEAKING plus the
    SPEAKING/SPEAKING_ALT alternation timer, i.e. the visible mouth-flap;
    ``speaking_end``/``idle``/``ready`` -> IDLE; ``processing`` -> THINKING).

THREADING CONTRACT (critical): the motor invokes ``handle_motor_event`` on the
ENGINE thread. An OBS socket round-trip must NEVER run there — a slow or
blocked OBS socket would stall Kira's turn loop. ``handle_motor_event`` only
ENQUEUES the mapped ``AvatarState`` on an internal queue; a single daemon
worker drains it and calls ``bridge.set_state`` (which fans out to the
subscribed ``OBSClient``). The alternation timer likewise only enqueues.
"""

from __future__ import annotations

import queue
import threading
from typing import Optional

from opencohost.avatar.avatar_state import AvatarState, AvatarStateBridge
from opencohost.avatar.obs_client import OBSClient, OBSConfig
from opencohost.config.logger import get_logger

logger = get_logger()

# Motor status string -> coarse avatar state. "speaking_start" is handled
# separately (it also arms the alternation timer), so it is not in this map.
_EVENT_TO_STATE = {
    "speaking_end": AvatarState.IDLE,
    "processing": AvatarState.THINKING,
    "idle": AvatarState.IDLE,
    "ready": AvatarState.IDLE,
}

# Alternation cadence — parity with motor_event_handlers.tick_speaking_alt
# (700 ms: fast enough to look natural, slow enough to see both frames).
_SPEAKING_ALT_INTERVAL = 0.7


class ObsRuntime:
    """Owns an ``AvatarStateBridge`` + ``OBSClient`` + retry loop for the API host.

    Constructed once by ``EngineHost.start`` and torn down in ``EngineHost.stop``.
    ``obs_client_cls`` / ``obs_config_cls`` are injection seams for tests
    (mirrors ``obs_lifecycle.start_from_config``'s class overrides) so no real
    OBS websocket is ever opened under test.
    """

    def __init__(
        self,
        *,
        obs_client_cls: Optional[type] = None,
        obs_config_cls: Optional[type] = None,
        retry_delay: float = 5.0,
    ) -> None:
        self.bridge = AvatarStateBridge()
        self._obs_client_cls = obs_client_cls if obs_client_cls is not None else OBSClient
        self._obs_config_cls = obs_config_cls if obs_config_cls is not None else OBSConfig
        self._retry_delay = retry_delay

        # Guards the mutable client/retry-loop attributes below.
        self._lock = threading.Lock()
        self._obs_client = None
        self._retry_thread: Optional[threading.Thread] = None
        self._cancel_event: Optional[threading.Event] = None

        # Serializes apply_config()=stop()+start_from_config() as one unit so two
        # concurrent PUTs (PUT /api/obs/config + PUT /api/avatar/config on
        # FastAPI's threadpool) can never each read _obs_client as None and
        # orphan the loser's connected client (never disconnected).
        self._apply_lock = threading.Lock()

        # State-drain worker: handle_motor_event enqueues, ONE daemon worker
        # calls bridge.set_state. Never run OBS socket work on the engine thread.
        self._state_queue: "queue.Queue" = queue.Queue()
        self._worker_lock = threading.Lock()
        self._worker_thread: Optional[threading.Thread] = None

        # Alternation timer state (SPEAKING <-> SPEAKING_ALT mouth-flap).
        self._speaking_alt_timer: Optional[threading.Timer] = None
        self._speaking_is_alt = False
        # Monotonic epoch guarding the flap against a Timer cancel race: a tick
        # already in flight when _stop_speaking_alt cancels must NOT re-arm and
        # resurrect an orphaned flap chain. Bumped under _lock on start/stop.
        self._speaking_alt_epoch = 0

    # -- connection state --------------------------------------------------

    @property
    def is_connected(self) -> bool:
        """True when a live OBS client exists and reports a connected socket."""
        with self._lock:
            client = self._obs_client
        return bool(client is not None and getattr(client, "is_connected", False))

    # -- lifecycle ---------------------------------------------------------

    def start_from_config(self) -> bool:
        """Create/refresh the OBS client and start one cancellable retry loop.

        Mirrors ``obs_lifecycle.start_from_config`` minus Tk. Returns True when a
        client is running or was created, False when OBS is disabled in config.
        """
        from opencohost.avatar.avatar_config import load_avatar_config

        avatar_cfg = load_avatar_config()

        if not avatar_cfg.obs.enabled:
            self.stop()
            return False

        with self._lock:
            existing_client = self._obs_client
            existing_thread = self._retry_thread
        if (
            existing_client is not None
            and existing_thread is not None
            and existing_thread.is_alive()
        ):
            return True
        if existing_client is not None and getattr(existing_client, "is_connected", False):
            return True

        try:
            client = self._obs_client_cls(
                config=self._obs_config_cls(
                    enabled=avatar_cfg.obs.enabled,
                    host=avatar_cfg.obs.host,
                    port=avatar_cfg.obs.port,
                    password=avatar_cfg.obs.password,
                    source_name=avatar_cfg.obs.source_name,
                    scene_name=avatar_cfg.obs.scene_name,
                ),
                assets_folder=avatar_cfg.assets_folder,
                state_images=avatar_cfg.state_images,
                on_log=lambda msg: logger.info("%s", msg),
            )
            cancel_event = threading.Event()
            with self._lock:
                superseded = self._obs_client
                self._obs_client = client
                self._cancel_event = cancel_event
            # Defensively disconnect any client we are overwriting so a racing
            # start never orphans a connected socket (belt-and-suspenders with
            # apply_config's _apply_lock; the normal stop()+start path already
            # cleared _obs_client to None before we get here).
            if superseded is not None:
                try:
                    superseded.disconnect()
                except Exception:
                    logger.exception("[OBS] Failed to disconnect superseded client")
            self._ensure_worker()
            t = threading.Thread(
                target=self._connect_loop,
                kwargs=dict(cancel_event=cancel_event, obs_client=client),
                name="obs-connect-retry",
                daemon=True,
            )
            with self._lock:
                self._retry_thread = t
            t.start()
            return True
        except Exception:
            logger.exception("[OBS] Failed to initialize headless runtime")
            return False

    def apply_config(self) -> bool:
        """Rebuild the runtime from the current avatar.yaml (stop + start).

        A live ``OBSClient`` snapshots ``state_images`` at construction
        (obs_client.py) and binds host/port at connect, so an avatar-card or
        connection change requires a fresh client — not just a reconnect.

        The whole stop()+start_from_config() runs under ``_apply_lock`` so two
        concurrent config PUTs are serialized: the loser's client is always torn
        down by the winner's ``stop()`` instead of being silently orphaned.
        """
        with self._apply_lock:
            self.stop()
            return self.start_from_config()

    def stop(self) -> None:
        """Cancel the retry loop and disconnect the live client.

        Mirrors ``obs_lifecycle.stop_runtime`` minus Tk. Best-effort: every step
        swallows exceptions so teardown never raises into ``EngineHost.stop``.
        """
        self._stop_speaking_alt()
        with self._lock:
            cancel_event = self._cancel_event
            client = self._obs_client
            self._obs_client = None
            self._cancel_event = None
            self._retry_thread = None
        if cancel_event is not None:
            cancel_event.set()
        if client is not None:
            try:
                client.disconnect()
            except Exception:
                logger.exception("[OBS] Failed to disconnect headless client")

    # -- motor-event handling (engine thread) ------------------------------

    def handle_motor_event(self, event) -> None:
        """Map a motor status string to an avatar state and ENQUEUE it.

        Called on the ENGINE thread (via ``EngineHost``'s ui_callback router),
        so it must never block on an OBS socket — it only enqueues. The whole
        body is guarded: a mapping/enqueue error must never break Kira's turn.
        Unknown events are ignored.
        """
        try:
            if event == "speaking_start":
                self._start_speaking_alt()
                return
            state = _EVENT_TO_STATE.get(event)
            if state is None:
                return  # unknown / unmapped event
            self._stop_speaking_alt()
            self._enqueue_state(state)
        except Exception:
            logger.exception("[OBS] handle_motor_event failed for %r", event)

    def flush(self, timeout: Optional[float] = None) -> None:
        """TEST HOOK: block until every enqueued state has been applied.

        The daemon worker calls ``task_done`` for each item, so ``queue.join``
        returns once the queue has drained. ``timeout`` is accepted for symmetry
        but ``queue.Queue.join`` has no bound — the worker drains near-instantly.
        """
        self._state_queue.join()

    # -- internal: state-drain worker --------------------------------------

    def _ensure_worker(self) -> None:
        with self._worker_lock:
            if self._worker_thread is not None and self._worker_thread.is_alive():
                return
            t = threading.Thread(
                target=self._drain_states, name="obs-state-worker", daemon=True
            )
            self._worker_thread = t
            t.start()

    def _drain_states(self) -> None:
        while True:
            state = self._state_queue.get()
            try:
                self.bridge.set_state(state)
            except Exception:
                logger.exception("[OBS] Failed to apply avatar state %r", state)
            finally:
                self._state_queue.task_done()

    def _enqueue_state(self, state: AvatarState) -> None:
        self._ensure_worker()
        self._state_queue.put(state)

    # -- internal: alternation timer (mouth-flap) --------------------------

    def _start_speaking_alt(self) -> None:
        """Enqueue SPEAKING, then start the SPEAKING/SPEAKING_ALT alternation.

        Parity with motor_event_handlers.on_motor_speaking_start (explicit
        SPEAKING set) + start_speaking_alt_timer (the alternation). Bumps the
        flap epoch and hands it to the tick closure so this is the only chain
        allowed to re-arm.
        """
        self._stop_speaking_alt()
        self._enqueue_state(AvatarState.SPEAKING)
        with self._lock:
            self._speaking_alt_epoch += 1
            epoch = self._speaking_alt_epoch
            self._speaking_is_alt = False
        self._tick_speaking_alt(epoch)

    def _tick_speaking_alt(self, epoch: int) -> None:
        """Toggle SPEAKING <-> SPEAKING_ALT and re-arm. Runs on a Timer thread;
        only enqueues (never touches the OBS socket directly).

        Cancel-race guard: the epoch check, the toggle, AND the re-arm all
        happen under ``_lock`` against ``_speaking_alt_epoch``.
        ``_stop_speaking_alt`` bumps the epoch BEFORE it cancels, so a tick that
        had already fired past its cancel finds a stale epoch here and returns
        WITHOUT enqueueing a state or arming a replacement Timer — a cancelled
        flap chain can never resurrect itself and keep pushing SPEAKING while
        Kira is silent. Storing the new Timer under the same lock closes the
        window where ``_stop_speaking_alt`` could grab the old Timer just before
        this tick overwrote the field with a live one.
        """
        with self._lock:
            if epoch != self._speaking_alt_epoch:
                return
            new_alt = not self._speaking_is_alt
            self._speaking_is_alt = new_alt
            timer = threading.Timer(
                _SPEAKING_ALT_INTERVAL, self._tick_speaking_alt, args=(epoch,)
            )
            timer.daemon = True
            self._speaking_alt_timer = timer
        self._enqueue_state(AvatarState.SPEAKING_ALT if new_alt else AvatarState.SPEAKING)
        timer.start()

    def _stop_speaking_alt(self) -> None:
        with self._lock:
            # Bump the epoch BEFORE cancelling so any tick already in flight sees
            # a stale epoch and refuses to re-arm (fixes the cancel race that
            # left an orphaned flap chain enqueueing SPEAKING while Kira was
            # silent).
            self._speaking_alt_epoch += 1
            timer = self._speaking_alt_timer
            self._speaking_alt_timer = None
            self._speaking_is_alt = False
        if timer is not None:
            try:
                timer.cancel()
            except Exception:
                pass

    # -- internal: connect retry loop --------------------------------------

    def _connect_loop(self, *, cancel_event: threading.Event, obs_client) -> None:
        """Retry OBS connection without letting socket errors kill the thread.

        Mirrors ``obs_lifecycle.connect_obs_loop`` minus Tk: on the first
        successful connect, subscribe the bridge and push the current state,
        then exit. A staleness check bails if a newer client replaced this one.
        """
        logged_once = False
        while True:
            if cancel_event.is_set():
                return
            try:
                with self._lock:
                    active = self._obs_client
                if active is None or active is not obs_client:
                    return
                if obs_client.connect(log_failures=not logged_once):
                    if cancel_event.is_set():
                        return
                    with self._lock:
                        if self._obs_client is not obs_client:
                            return
                    obs_client.subscribe_bridge(self.bridge)
                    obs_client.on_state_change(self.bridge.get_state())
                    return
                logged_once = True
            except Exception:
                logger.exception("[OBS] Unexpected error in headless connect loop")
                logged_once = True
            if cancel_event.wait(self._retry_delay):
                return
