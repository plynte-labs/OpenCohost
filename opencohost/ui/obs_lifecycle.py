"""OBS lifecycle — extracted from app_shell.py (ui_rendering_optimization_20260609 Phase 6).

All Tk mutation is marshaled through the injected schedule_ui_update (== app._safe_after).
State (_obs_client, _obs_retry_thread, _obs_retry_cancel, _joyita_obs_timer_id) stays as
instance attributes on VocalAIApp; this module reads/writes them only via injected
getter/setter callables.

This module must NEVER import opencohost.ui.app_shell or the VocalAIApp class.
torch/ollama/transformers are never imported here.  avatar_config is loaded deferred
(inside functions) per the existing app_shell pattern.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable

from opencohost.avatar.obs_client import OBSClient, OBSConfig
from opencohost.config.logger import get_logger

logger = get_logger()


def on_joyita_to_obs(
    *,
    text: str,
    get_obs_client: Callable[[], Any],
    get_joyita_timer_id: Callable[[], Any],
    set_joyita_timer_id: Callable[[Any], None],
    after: Callable[..., Any],
    after_cancel: Callable[[Any], None],
    clear_obs_joyita: Callable[[str], None],
) -> None:
    """Send the joyita message to an OBS Text source named 'KiraJoyita'.

    Clears it after 120 seconds unless a new joyita arrives sooner.

    NOTE: `after` here is the TIMER arm (bare Tk after for scheduling a clear-text
    callback, not a widget mutation).  Widget mutations go through schedule_ui_update.

    Args:
        text: The message to display.
        get_obs_client: Returns the current OBSClient instance (or None).
        get_joyita_timer_id: Returns the current joyita timer id (or None).
        set_joyita_timer_id: Stores the new timer id.
        after: Bare Tk after() callable — TIMERS ONLY (not widget mutation).
        after_cancel: Bare Tk after_cancel() callable.
        clear_obs_joyita: Callable that clears the joyita text source.
    """
    if not text:
        return
    obs = get_obs_client()
    if not obs or not obs.is_connected:
        return
    source_name = "KiraJoyita"
    if obs.set_obs_text(source_name, text):
        timer_id = get_joyita_timer_id()
        if timer_id is not None:
            try:
                after_cancel(timer_id)
            except Exception:
                pass
        new_id = after(120_000, lambda: clear_obs_joyita(source_name))
        set_joyita_timer_id(new_id)


def clear_obs_joyita(
    *,
    source_name: str,
    get_obs_client: Callable[[], Any],
    set_joyita_timer_id: Callable[[Any], None],
) -> None:
    """Reset the joyita timer id and blank the OBS text source.

    Args:
        source_name: The OBS source to blank.
        get_obs_client: Returns the current OBSClient instance (or None).
        set_joyita_timer_id: Clears the stored timer id.
    """
    set_joyita_timer_id(None)
    obs = get_obs_client()
    if obs and obs.is_connected:
        obs.set_obs_text(source_name, "")


def start_from_config(
    *,
    print_log: Callable[[str], None],
    schedule_ui_update: Callable[..., None],
    avatar_panel: Any,
    avatar_bridge: Any,
    get_obs_client: Callable[[], Any],
    set_obs_client: Callable[[Any], None],
    get_retry_thread: Callable[[], Any],
    set_retry_thread: Callable[[Any], None],
    get_retry_cancel: Callable[[], Any],
    set_retry_cancel: Callable[[Any], None],
    winfo_exists: Callable[[], bool],
    retry_delay: float = 5,
    obs_client_cls: type | None = None,
    obs_config_cls: type | None = None,
    thread_cls: type | None = None,
) -> bool:
    """Create/refresh OBS runtime client and start one cancellable retry loop.

    Body moved verbatim from VocalAIApp._obs_start_from_config; self.X replaced
    by injected callables.

    Args:
        print_log: Logger callable (== self._print_log).
        schedule_ui_update: Thread-safe UI scheduler (== self._safe_after).
        avatar_panel: The AvatarPanel instance.
        avatar_bridge: The AvatarStateBridge instance.
        get_obs_client: Returns current _obs_client value.
        set_obs_client: Writes _obs_client value.
        get_retry_thread: Returns current _obs_retry_thread value.
        set_retry_thread: Writes _obs_retry_thread value.
        get_retry_cancel: Returns current _obs_retry_cancel event (required; the
            disabled path must be able to cancel a live retry thread).
        set_retry_cancel: Writes _obs_retry_cancel value.
        winfo_exists: Callable that returns bool (shell window still alive).
        retry_delay: Seconds between connection retries.
        obs_client_cls: OBSClient class override (injected from app_shell so
            existing tests that patch app_shell.OBSClient keep working).
        obs_config_cls: OBSConfig class override (same reason).
        thread_cls: threading.Thread class override (same reason).

    Returns:
        True if a client is running or was created, False if OBS is disabled.
    """
    from opencohost.avatar.avatar_config import load_avatar_config
    avatar_cfg = load_avatar_config()

    if not avatar_cfg.obs.enabled:
        stop_runtime(
            print_log=print_log,
            avatar_panel=avatar_panel,
            get_obs_client=get_obs_client,
            set_obs_client=set_obs_client,
            get_retry_cancel=get_retry_cancel,
        )
        return False

    existing_thread = get_retry_thread()
    if (
        get_obs_client() is not None
        and existing_thread is not None
        and existing_thread.is_alive()
    ):
        return True

    existing_client = get_obs_client()
    if existing_client is not None and getattr(existing_client, "is_connected", False):
        try:
            avatar_panel.set_obs_client(existing_client)
        except Exception:
            pass
        return True

    _OBSClient = obs_client_cls if obs_client_cls is not None else OBSClient
    _OBSConfig = obs_config_cls if obs_config_cls is not None else OBSConfig
    _Thread = thread_cls if thread_cls is not None else threading.Thread

    try:
        client = _OBSClient(
            config=_OBSConfig(
                enabled=avatar_cfg.obs.enabled,
                host=avatar_cfg.obs.host,
                port=avatar_cfg.obs.port,
                password=avatar_cfg.obs.password,
                source_name=avatar_cfg.obs.source_name,
                scene_name=avatar_cfg.obs.scene_name,
            ),
            assets_folder=avatar_cfg.assets_folder,
            state_images=avatar_cfg.state_images,
            on_log=lambda msg: print_log(msg),
        )
        set_obs_client(client)
        cancel_event = threading.Event()
        set_retry_cancel(cancel_event)
        t = _Thread(
            target=connect_obs_loop,
            kwargs=dict(
                cancel_event=cancel_event,
                obs_client=client,
                get_obs_client=get_obs_client,
                print_log=print_log,
                schedule_ui_update=schedule_ui_update,
                avatar_panel=avatar_panel,
                avatar_bridge=avatar_bridge,
                winfo_exists=winfo_exists,
                retry_delay=retry_delay,
            ),
            daemon=True,
        )
        set_retry_thread(t)
        t.start()
        return True
    except Exception as e:
        print_log(f"[OBS] Failed to initialize: {e}")
        return False


def stop_runtime(
    *,
    print_log: Callable[[str], None],
    avatar_panel: Any,
    get_obs_client: Callable[[], Any],
    set_obs_client: Callable[[Any], None],
    get_retry_cancel: Callable[[], Any],
) -> None:
    """Cancel OBS retry loop and disconnect the live runtime client.

    Body moved verbatim from VocalAIApp._obs_stop_runtime; self.X replaced
    by injected callables.

    Args:
        print_log: Logger callable (kept for signature symmetry; logging via module logger).
        avatar_panel: The AvatarPanel instance.
        get_obs_client: Returns current _obs_client value.
        set_obs_client: Writes _obs_client value.
        get_retry_cancel: Returns current _obs_retry_cancel event (or None).
    """
    cancel_event = get_retry_cancel()
    if cancel_event is not None:
        cancel_event.set()
    obs_client = get_obs_client()
    if obs_client is not None:
        try:
            obs_client.disconnect()
        except Exception:
            logger.exception("Fallo al desconectar OBS")
    set_obs_client(None)
    try:
        avatar_panel.set_obs_client(None)
    except Exception:
        pass


def connect_obs_loop(
    *,
    cancel_event: threading.Event | None = None,
    obs_client: OBSClient | None = None,
    get_obs_client: Callable[[], Any],
    print_log: Callable[[str], None],
    schedule_ui_update: Callable[..., None],
    avatar_panel: Any,
    avatar_bridge: Any,
    winfo_exists: Callable[[], bool],
    retry_delay: float = 5,
) -> None:
    """Retry OBS connection without letting unexpected socket errors kill the thread.

    Body moved verbatim from VocalAIApp._connect_obs_loop; self.X replaced
    by injected callables.

    Widget mutations (avatar_panel.set_obs_client) go through schedule_ui_update.
    This function NEVER calls a bare after() — it has no such parameter.

    Args:
        cancel_event: Threading.Event that signals loop termination.
        obs_client: The OBSClient instance bound to this loop iteration.
        get_obs_client: Returns current _obs_client from the shell (staleness check).
        print_log: Logger callable.
        schedule_ui_update: Thread-safe UI scheduler (== self._safe_after).
        avatar_panel: The AvatarPanel instance.
        avatar_bridge: The AvatarStateBridge instance.
        winfo_exists: Callable that returns bool (shell window alive check).
        retry_delay: Seconds between connection retries.
    """
    logged_once = False
    managed_loop = cancel_event is not None
    while True:
        if cancel_event is not None and cancel_event.is_set():
            break
        try:
            active_client = obs_client if obs_client is not None else get_obs_client()
            if active_client is None or active_client is not get_obs_client():
                break
            if active_client.connect(log_failures=not logged_once):
                if cancel_event is not None and cancel_event.is_set():
                    break
                if active_client is not get_obs_client():
                    break
                active_client.subscribe_bridge(avatar_bridge)
                active_client.on_state_change(avatar_bridge.get_state())
                try:
                    if winfo_exists():
                        schedule_ui_update(lambda: avatar_panel.set_obs_client(active_client))
                except Exception:
                    pass
                return
            if not logged_once:
                print_log(
                    "[OBS] No se pudo conectar. OpenCohost reintentara cada 5s. "
                    "Abrí OBS y la conexión se restablecerá automáticamente."
                )
                logged_once = True
        except Exception:
            logger.exception("Fallo inesperado en loop de OBS")
            if not logged_once:
                print_log(
                    "[OBS] Error inesperado conectando. OpenCohost seguira reintentando cada 5s."
                )
                logged_once = True
        if managed_loop:
            if cancel_event is not None and cancel_event.wait(retry_delay):
                break
        else:
            time.sleep(retry_delay)
    # Client was destroyed (app closing)
