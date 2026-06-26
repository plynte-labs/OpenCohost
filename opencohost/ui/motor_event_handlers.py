"""Motor-event dispatch handlers — extracted from app_shell.py (ui_rendering_optimization_20260609 Phase 6 Stage 3).

All Tk widget mutation is marshaled through the injected schedule_ui_update
(== app._safe_after).  Timer scheduling uses only the injected ``after`` and
``after_cancel`` callables — NEVER a bare self.after call.

  schedule_ui_update(fn, delay_ms=0) — widget mutations (all handlers)
  after(delay_ms, fn)                — TIMERS ONLY (_tick_speaking_alt,
                                       _reset_inactivity_timer)
  after_cancel(timer_id)             — TIMERS ONLY (_stop_speaking_alt_timer,
                                       _stop_inactivity_timer)

Timer state (_speaking_is_alt, _speaking_alt_timer_id, _inactivity_timer_id)
stays as instance attributes on VocalAIApp; this module reads/writes them only
via the injected getter/setter callables supplied in the deps dict.

This module must NEVER import opencohost.ui.app_shell or the VocalAIApp class.
torch/ollama/transformers are never imported here.
"""

from __future__ import annotations

from typing import Any, Callable

from opencohost.avatar.avatar_state import AvatarState
from opencohost.config.logger import get_logger

logger = get_logger()


# ---------------------------------------------------------------------------
# Individual handler functions (keyword-only injected deps)
# ---------------------------------------------------------------------------

def on_motor_ready(
    *,
    ui_state: Any,
    motor_ia: Any,
    model_panel: Any,
    voice_panel: Any,
    btn_grabar: Any,
    btn_voz: Any,
    btn_ws: Any,
    btn_primary_voice: Any,
    btn_enviar: Any,
    schedule_ui_update: Callable[..., None],
    actualizar_pipeline: Callable[[str], None],
    set_title: Callable[[str], None],
    **_: Any,
) -> None:
    """Motor reached 'ready' state — enable all primary controls."""
    ui_state.model_status = "ready"
    schedule_ui_update(lambda: btn_grabar.configure(state="normal"))
    schedule_ui_update(lambda: btn_voz.configure(state="normal"))
    schedule_ui_update(lambda: btn_ws.configure(state="normal"))
    schedule_ui_update(lambda: btn_primary_voice.configure(state="normal"))
    schedule_ui_update(lambda: btn_enviar.configure(state="normal"))
    actualizar_pipeline("idle")
    schedule_ui_update(lambda: model_panel.update_model_info(model_panel.get_selected_tag()))
    if hasattr(motor_ia, "current_model"):
        schedule_ui_update(lambda: model_panel.set_active_model(motor_ia.current_model))
        # Sync combobox to actual startup model (may differ from DEFAULT_MODEL)
        model = motor_ia.current_model
        schedule_ui_update(lambda: model_panel.restore_to_active_model(model))
        schedule_ui_update(lambda: model_panel.set_llm_tier_state(motor_ia.llm_tiers.config.as_dict(), motor_ia.active_llm_tier))
        schedule_ui_update(lambda: set_title("Kira — OpenCohost"))
    # Start PTT flush watcher thread
    if voice_panel is not None and hasattr(voice_panel, "_start_ptt_flush_watcher"):
        voice_panel._start_ptt_flush_watcher()


def on_motor_model_warming(
    *,
    ui_state: Any,
    btn_enviar: Any,
    btn_download: Any,
    schedule_ui_update: Callable[..., None],
    actualizar_pipeline: Callable[[str], None],
    **_: Any,
) -> None:
    """Motor is warming a model — disable send/download buttons."""
    ui_state.model_status = "loading"
    schedule_ui_update(lambda: btn_enviar.configure(state="disabled"))
    schedule_ui_update(lambda: btn_download.configure(state="disabled", text="Preparando modelo..."))
    actualizar_pipeline("init")


def on_motor_ollama_unavailable(
    *,
    motor_ia: Any,
    model_panel: Any,
    btn_grabar: Any,
    btn_voz: Any,
    btn_ws: Any,
    btn_primary_voice: Any,
    btn_enviar: Any,
    avatar_bridge: Any,
    schedule_ui_update: Callable[..., None],
    actualizar_pipeline: Callable[[str], None],
    **_: Any,
) -> None:
    """Ollama is not reachable — disable all controls and show error state."""
    schedule_ui_update(lambda: btn_grabar.configure(state="disabled"))
    schedule_ui_update(lambda: btn_voz.configure(state="disabled"))
    schedule_ui_update(lambda: btn_ws.configure(state="disabled"))
    schedule_ui_update(lambda: btn_primary_voice.configure(state="disabled"))
    schedule_ui_update(lambda: btn_enviar.configure(state="disabled"))
    schedule_ui_update(
        lambda: model_panel.refresh_ollama_state(
            on_check_ollama=lambda: motor_ia.command_queue.put(("check_ollama", None))
        )
    )
    actualizar_pipeline("error")
    if avatar_bridge is not None:
        avatar_bridge.set_state(AvatarState.ERROR)


def on_motor_processing(
    *,
    btn_enviar: Any,
    combo_modelos: Any,
    btn_download: Any,
    btn_mapear: Any,
    schedule_ui_update: Callable[..., None],
    actualizar_pipeline: Callable[[str], None],
    **_: Any,
) -> None:
    """Motor is generating a response — disable controls that must not fire mid-flight."""
    actualizar_pipeline("processing")
    schedule_ui_update(lambda: btn_enviar.configure(state="disabled"))
    schedule_ui_update(lambda: combo_modelos.configure(state="disabled"))
    schedule_ui_update(lambda: btn_download.configure(state="disabled"))
    schedule_ui_update(lambda: btn_mapear.configure(state="disabled"))


def on_motor_idle(
    *,
    model_panel: Any,
    ptt: Any,
    combo_modelos: Any,
    btn_enviar: Any,
    btn_mapear: Any,
    switch_ptt: Any,
    kira_agenda: Any,
    on_ptt_press: Callable[..., None],
    on_ptt_release: Callable[..., None],
    on_ptt_click: Callable[..., None],
    kira_agenda_schedule_tick: Callable[[int], None],
    schedule_ui_update: Callable[..., None],
    actualizar_pipeline: Callable[[str], None],
    **_: Any,
) -> None:
    """Motor finished its current request — restore controls to idle state."""
    from opencohost.smart_aggregator import AgendaState
    actualizar_pipeline("idle")
    schedule_ui_update(lambda: btn_enviar.configure(state="normal"))
    schedule_ui_update(lambda: combo_modelos.configure(state="normal"))
    schedule_ui_update(lambda: model_panel.update_button_for_ollama_state())
    schedule_ui_update(lambda: switch_ptt.configure(state="normal"))
    schedule_ui_update(lambda: btn_mapear.configure(state="normal"))
    ptt.ensure_listener(on_press=on_ptt_press, on_release=on_ptt_release, on_click=on_ptt_click)
    if kira_agenda is not None and kira_agenda.state not in {
        AgendaState.OFF, AgendaState.PAUSED_NEEDS_OPERATOR, AgendaState.HARD_PAUSED
    }:
        kira_agenda_schedule_tick(500)


def on_motor_llm_timeout_recovered(
    *,
    motor_ia: Any,
    model_panel: Any,
    print_log: Callable[[str], None],
    schedule_ui_update: Callable[..., None],
    **_: Any,
) -> None:
    """LLM timed out but recovered — log the event and restore model info."""
    failure = getattr(motor_ia, "_last_llm_failure", None) or {}
    failed_model = failure.get("model", "?")
    recovered_model = getattr(motor_ia, "current_model", failed_model)
    print_log(
        f"[Sistema] ⚠️ Recuperación por timeout de inferencia en {failed_model}. Modelo activo: {recovered_model}"
    )
    schedule_ui_update(lambda: model_panel.restore_to_active_model(recovered_model))
    schedule_ui_update(lambda: model_panel.update_model_info(recovered_model))


def start_speaking_alt_timer(
    *,
    set_speaking_is_alt: Callable[[bool], None],
    get_speaking_is_alt: Callable[[], bool],
    get_speaking_alt_timer_id: Callable[[], Any],
    set_speaking_alt_timer_id: Callable[[Any], None],
    avatar_bridge: Any,
    after: Callable[..., Any],
    **_: Any,
) -> None:
    """Start a timer that alternates between SPEAKING and SPEAKING_ALT avatar states."""
    set_speaking_is_alt(False)
    tick_speaking_alt(
        set_speaking_is_alt=set_speaking_is_alt,
        get_speaking_is_alt=get_speaking_is_alt,
        get_speaking_alt_timer_id=get_speaking_alt_timer_id,
        set_speaking_alt_timer_id=set_speaking_alt_timer_id,
        avatar_bridge=avatar_bridge,
        after=after,
    )


def stop_speaking_alt_timer(
    *,
    get_speaking_alt_timer_id: Callable[[], Any],
    set_speaking_alt_timer_id: Callable[[Any], None],
    after_cancel: Callable[[Any], None],
    **_: Any,
) -> None:
    """Stop the speaking alternation timer."""
    timer_id = get_speaking_alt_timer_id()
    if timer_id is not None:
        after_cancel(timer_id)
        set_speaking_alt_timer_id(None)


def tick_speaking_alt(
    *,
    set_speaking_is_alt: Callable[[bool], None],
    get_speaking_is_alt: Callable[[], bool],
    get_speaking_alt_timer_id: Callable[[], Any],
    set_speaking_alt_timer_id: Callable[[Any], None],
    avatar_bridge: Any,
    after: Callable[..., Any],
    **_: Any,
) -> None:
    """Toggle between SPEAKING and SPEAKING_ALT and reschedule.

    NOTE: ``after`` here is the TIMER arm (bare Tk after for scheduling the next
    tick, not a widget mutation).  Widget mutations go through schedule_ui_update.
    """
    current = get_speaking_is_alt()
    new_alt = not current
    set_speaking_is_alt(new_alt)
    if avatar_bridge is not None:
        avatar_bridge.set_state(
            AvatarState.SPEAKING_ALT if new_alt else AvatarState.SPEAKING
        )
    # Alternate every 700ms — fast enough to look natural, slow enough to see both images
    new_id = after(
        700,
        lambda: tick_speaking_alt(
            set_speaking_is_alt=set_speaking_is_alt,
            get_speaking_is_alt=get_speaking_is_alt,
            get_speaking_alt_timer_id=get_speaking_alt_timer_id,
            set_speaking_alt_timer_id=set_speaking_alt_timer_id,
            avatar_bridge=avatar_bridge,
            after=after,
        ),
    )
    set_speaking_alt_timer_id(new_id)


def reset_inactivity_timer(
    *,
    get_inactivity_timer_id: Callable[[], Any],
    set_inactivity_timer_id: Callable[[Any], None],
    inactivity_timeout_ms: int,
    after: Callable[..., Any],
    after_cancel: Callable[[Any], None],
    avatar_bridge: Any,
    log_accion: Callable[[str], None],
    **_: Any,
) -> None:
    """Reset the inactivity timer. If Kira is idle for too long, she goes to sleep."""
    stop_inactivity_timer(
        get_inactivity_timer_id=get_inactivity_timer_id,
        set_inactivity_timer_id=set_inactivity_timer_id,
        after_cancel=after_cancel,
    )
    new_id = after(
        inactivity_timeout_ms,
        lambda: on_inactivity_timeout(
            get_inactivity_timer_id=get_inactivity_timer_id,
            set_inactivity_timer_id=set_inactivity_timer_id,
            avatar_bridge=avatar_bridge,
            log_accion=log_accion,
        ),
    )
    set_inactivity_timer_id(new_id)


def stop_inactivity_timer(
    *,
    get_inactivity_timer_id: Callable[[], Any],
    set_inactivity_timer_id: Callable[[Any], None],
    after_cancel: Callable[[Any], None],
    **_: Any,
) -> None:
    """Stop the inactivity timer."""
    timer_id = get_inactivity_timer_id()
    if timer_id is not None:
        after_cancel(timer_id)
        set_inactivity_timer_id(None)


def on_inactivity_timeout(
    *,
    get_inactivity_timer_id: Callable[[], Any],
    set_inactivity_timer_id: Callable[[Any], None],
    avatar_bridge: Any,
    log_accion: Callable[[str], None],
    **_: Any,
) -> None:
    """Kira has been idle for too long — go to sleep."""
    set_inactivity_timer_id(None)
    if avatar_bridge is not None:
        current = avatar_bridge.get_state()
        if current in (AvatarState.IDLE,):
            avatar_bridge.set_state(AvatarState.SLEEPING)
            log_accion("Kira se durmió por inactividad")


def on_motor_speaking_start(
    *,
    audio_bed: Any,
    kira_agenda: Any,
    avatar_bridge: Any,
    kira_agenda_update_status: Callable[[], None],
    kira_agenda_prefetch_while_speaking: Callable[[], None],
    kira_agenda_clear_prefetch: Callable[[], None],
    log_accion: Callable[[str], None],
    actualizar_pipeline: Callable[[str], None],
    set_speaking_is_alt: Callable[[bool], None],
    get_speaking_is_alt: Callable[[], bool],
    get_speaking_alt_timer_id: Callable[[], Any],
    set_speaking_alt_timer_id: Callable[[Any], None],
    after: Callable[..., Any],
    **_: Any,
) -> None:
    """Kira started speaking — duck audio bed, update avatar, start alternation timer."""
    from opencohost.smart_aggregator import AgendaState
    if audio_bed is not None:
        audio_bed.duck()
    # Use controller state, not motor source, to decide if this speech
    # was initiated by the agenda state machine.  The controller may
    # emit chat/PTT/stop actions whose motor source does not start
    # with "kira-agenda"; the state check is the authoritative signal.
    controller_generated = (
        kira_agenda is not None
        and kira_agenda.state in {AgendaState.SPEAKING, AgendaState.GENERATING, AgendaState.TOPIC_CLOSING}
    )
    if controller_generated:
        kira_agenda.mark_generation_accepted()
        kira_agenda_update_status()
        kira_agenda_prefetch_while_speaking()
    elif kira_agenda is not None:
        kira_agenda_clear_prefetch()
    actualizar_pipeline("speaking")
    log_accion("Kira comenzó a sintetizar respuesta")
    if avatar_bridge is not None:
        avatar_bridge.set_state(AvatarState.SPEAKING)
        start_speaking_alt_timer(
            set_speaking_is_alt=set_speaking_is_alt,
            get_speaking_is_alt=get_speaking_is_alt,
            get_speaking_alt_timer_id=get_speaking_alt_timer_id,
            set_speaking_alt_timer_id=set_speaking_alt_timer_id,
            avatar_bridge=avatar_bridge,
            after=after,
        )


def on_motor_speaking_end(
    *,
    audio_bed: Any,
    kira_agenda: Any,
    voice_panel: Any,
    ptt: Any,
    avatar_bridge: Any,
    switch_ptt: Any,
    on_ptt_press: Callable[..., None],
    on_ptt_release: Callable[..., None],
    on_ptt_click: Callable[..., None],
    kira_agenda_update_status: Callable[[], None],
    kira_agenda_consume_pending_chat_if_due: Callable[[], bool],
    kira_agenda_play_prefetched_if_ready: Callable[[], bool],
    kira_agenda_schedule_tick: Callable[[int], None],
    kira_agenda_clear_prefetch: Callable[[], None],
    check_pending_audio_bed_stop: Callable[[], None],
    get_speaking_alt_timer_id: Callable[[], Any],
    set_speaking_alt_timer_id: Callable[[Any], None],
    after_cancel: Callable[[Any], None],
    schedule_ui_update: Callable[..., None],
    actualizar_pipeline: Callable[[str], None],
    **_: Any,
) -> None:
    """Kira finished speaking — unduck audio bed, restore controls, update avatar."""
    from opencohost.smart_aggregator import AgendaState
    prefetched_started = False
    # Use controller state, not motor source (see on_motor_speaking_start).
    agenda_speech = (
        kira_agenda is not None
        and kira_agenda.state in {AgendaState.SPEAKING, AgendaState.GENERATING}
    )
    if agenda_speech:
        kira_agenda.mark_speech_complete()
        kira_agenda_update_status()
        if kira_agenda_consume_pending_chat_if_due():
            prefetched_started = True
        else:
            prefetched_started = kira_agenda_play_prefetched_if_ready()
        if not prefetched_started and kira_agenda.state not in {AgendaState.OFF, AgendaState.PAUSED_NEEDS_OPERATOR}:
            kira_agenda_schedule_tick(1200)
    elif kira_agenda is not None:
        kira_agenda_clear_prefetch()
    stop_speaking_alt_timer(
        get_speaking_alt_timer_id=get_speaking_alt_timer_id,
        set_speaking_alt_timer_id=set_speaking_alt_timer_id,
        after_cancel=after_cancel,
    )
    if audio_bed is not None:
        audio_bed.unduck()
        if agenda_speech or audio_bed.current_track is not None:
            audio_bed.on_boundary()
    # Fix 3: fire the deferred graceful audio-bed stop if soft_stop was
    # called while Kira was speaking.  This runs AFTER mark_speech_complete()
    # so the agenda state is already settled (OFF when closing speech ended).
    check_pending_audio_bed_stop()
    estado = "listening" if voice_panel.is_ws_connected() else "idle"
    actualizar_pipeline(estado)
    if avatar_bridge is not None:
        avatar_bridge.set_state(
            AvatarState.LISTENING if estado == "listening" else AvatarState.IDLE
        )
    schedule_ui_update(lambda: switch_ptt.configure(state="normal"))
    ptt.ensure_listener(on_press=on_ptt_press, on_release=on_ptt_release, on_click=on_ptt_click)


def on_motor_model_changed(
    *,
    motor_ia: Any,
    model_panel: Any,
    schedule_ui_update: Callable[..., None],
    actualizar_pipeline: Callable[[str], None],
    set_title: Callable[[str], None],
    **_: Any,
) -> None:
    """Motor switched to a new model — update title, model panel, and pipeline."""
    model = motor_ia.current_model
    schedule_ui_update(lambda: set_title("Kira — OpenCohost"))
    schedule_ui_update(lambda: model_panel.update_model_info(model))
    # Use restore_to_active_model (not set_active_model) so the combobox is
    # synced to the newly active model — mirroring the failure path at
    # on_motor_switch_failed, which already used restore_to_active_model.
    # set_active_model only updated _active_model_tag and buttons; it never
    # called combo_modelos.set(), leaving the combobox stale after a success.
    schedule_ui_update(lambda: model_panel.restore_to_active_model(model))
    schedule_ui_update(lambda: model_panel.set_llm_tier_state(motor_ia.llm_tiers.config.as_dict(), motor_ia.active_llm_tier))
    actualizar_pipeline("idle")


def on_motor_switch_pending(
    *,
    motor_ia: Any,
    model_panel: Any,
    print_log: Callable[[str], None],
    **_: Any,
) -> None:
    """A model switch was requested but deferred — log a status message."""
    desired = getattr(motor_ia, "_desired_model", None)
    if desired:
        display = model_panel.get_display_for_tag(desired)
        if not getattr(motor_ia, "is_ready", False):
            print_log(f"[Sistema] Cambio a {display} pendiente: Ollama no está listo.")
        else:
            print_log(f"[Sistema] Cambio a {display} pendiente: se aplicará al terminar la respuesta actual.")
    # Do NOT restore combobox — user's selection stays visible as intended target


def on_motor_switch_failed(
    *,
    ui_state: Any,
    motor_ia: Any,
    model_panel: Any,
    print_log: Callable[[str], None],
    schedule_ui_update: Callable[..., None],
    **_: Any,
) -> None:
    """A model switch failed — set error state, restore combobox, schedule Fix A reset.

    Status Bars Stale Fix A: if the motor rolled back to a working model, the
    system is operational again — clear the transient "error" pill after a short
    delay so it doesn't stick for the whole session (the failure is logged above).

    The deferred reset uses schedule_ui_update(func, delay_ms=2000) so that
    test_app_shell_status_stale.py's synchronous stub (_safe_after = lambda
    func, delay_ms=0: func()) drives it correctly.
    """
    ui_state.model_status = "error"
    failure = getattr(motor_ia, "_last_switch_failure", None)
    actual_model = motor_ia.current_model
    if failure:
        requested = failure.get("requested", "?")
        reason = failure.get("reason", "unknown")
        print_log(f"[Sistema] ❌ Cambio a {requested} fallido: {reason}. Modelo activo: {actual_model}")
        # Clear after read to prevent stale display
        motor_ia._last_switch_failure = None
    # Restore combobox to actual motor model
    schedule_ui_update(lambda: model_panel.restore_to_active_model(actual_model))
    schedule_ui_update(lambda: model_panel.set_llm_tier_state(motor_ia.llm_tiers.config.as_dict(), motor_ia.active_llm_tier))
    schedule_ui_update(lambda: model_panel.update_model_info(actual_model))
    # Fix A: clear transient error pill after rollback
    if actual_model:
        schedule_ui_update(
            lambda: setattr(ui_state, "model_status", "ready")
            if ui_state.model_status == "error" else None,
            2000,
        )


def on_motor_download_start(
    *,
    btn_download: Any,
    combo_modelos: Any,
    progress_download: Any,
    btn_primary_voice: Any,
    schedule_ui_update: Callable[..., None],
    actualizar_pipeline: Callable[[str], None],
    **_: Any,
) -> None:
    """A model download has started — show download progress controls."""
    schedule_ui_update(lambda: btn_download.configure(state="disabled", text="Descargando..."))
    schedule_ui_update(lambda: combo_modelos.configure(state="disabled"))
    schedule_ui_update(lambda: progress_download.pack(fill="x", padx=10, pady=(4, 10)))
    schedule_ui_update(lambda: progress_download.set(0))
    schedule_ui_update(lambda: btn_primary_voice.configure(state="disabled"))
    actualizar_pipeline("downloading")


def on_motor_download_done(
    *,
    motor_ia: Any,
    model_panel: Any,
    combo_modelos: Any,
    progress_download: Any,
    btn_primary_voice: Any,
    schedule_ui_update: Callable[..., None],
    actualizar_pipeline: Callable[[str], None],
    set_title: Callable[[str], None],
    **_: Any,
) -> None:
    """A model download completed — restore controls and update model info."""
    model = motor_ia.current_model
    # Invalidate stale install-status cache so the next _modelo_instalado call
    # reflects the newly downloaded model (fix #A).
    schedule_ui_update(lambda: model_panel._invalidate_model_cache())
    schedule_ui_update(lambda: model_panel.update_button_for_ollama_state())
    schedule_ui_update(lambda: combo_modelos.configure(state="normal"))
    schedule_ui_update(lambda: progress_download.pack_forget())
    schedule_ui_update(lambda: btn_primary_voice.configure(state="normal"))
    schedule_ui_update(lambda: set_title("Kira — OpenCohost"))
    schedule_ui_update(lambda: model_panel.update_model_info(model))
    actualizar_pipeline("idle")
    schedule_ui_update(lambda: model_panel.update_model_info(model_panel.get_selected_tag()))


def on_motor_download_error(
    *,
    model_panel: Any,
    combo_modelos: Any,
    progress_download: Any,
    btn_primary_voice: Any,
    avatar_bridge: Any,
    schedule_ui_update: Callable[..., None],
    actualizar_pipeline: Callable[[str], None],
    **_: Any,
) -> None:
    """A model download failed — restore controls and show error avatar state."""
    schedule_ui_update(lambda: model_panel.update_button_for_ollama_state())
    schedule_ui_update(lambda: combo_modelos.configure(state="normal"))
    schedule_ui_update(lambda: progress_download.pack_forget())
    schedule_ui_update(lambda: btn_primary_voice.configure(state="normal"))
    actualizar_pipeline("error")
    if avatar_bridge is not None:
        avatar_bridge.set_state(AvatarState.ERROR)


def on_ctx_pressure_high(**_: Any) -> None:
    """Handle a context-pressure-high signal from the LLM engine.

    Logs the event at WARNING level. A visual indicator is a future enhancement
    (out of scope for the context_overflow_guardrail track); this establishes the
    event plumbing without committing to a specific UI widget.
    """
    logger.warning("UI: ctx_pressure_high signal received from LLM engine.")


# ---------------------------------------------------------------------------
# Single source of truth: status string → handler function name
# ---------------------------------------------------------------------------

# Maps each motor status string to the name of the module-level handler function
# that handles it.  VocalAIApp._handle_motor_event looks up the corresponding
# delegate as "_" + name (e.g. "on_motor_ready" → self._on_motor_ready).
# get_handler_map() below is built from this constant so the two can never drift.
STATUS_TO_HANDLER: dict[str, str] = {
    "ready":                    "on_motor_ready",
    "model_warming":            "on_motor_model_warming",
    "ollama_unavailable":       "on_motor_ollama_unavailable",
    "processing":               "on_motor_processing",
    "idle":                     "on_motor_idle",
    "llm_timeout_recovered":    "on_motor_llm_timeout_recovered",
    "speaking_start":           "on_motor_speaking_start",
    "speaking_end":             "on_motor_speaking_end",
    "model_changed":            "on_motor_model_changed",
    "model_switch_pending":     "on_motor_switch_pending",
    "model_switch_applied":     "on_motor_model_changed",   # alias → model_changed
    "model_switch_failed":      "on_motor_switch_failed",
    "llm_tier_switch_applied":  "on_motor_model_changed",   # alias → model_changed
    "llm_tier_switch_failed":   "on_motor_switch_failed",   # alias → switch_failed
    "download_start":           "on_motor_download_start",
    "download_done":            "on_motor_download_done",
    "download_error":           "on_motor_download_error",
    "ctx_pressure_high":        "on_ctx_pressure_high",
}


# ---------------------------------------------------------------------------
# Dispatch map builder
# ---------------------------------------------------------------------------

def get_handler_map(deps: dict) -> dict[str, Callable[[], None]]:
    """Build a {status: handler()} dispatch map from an injected deps dict.

    Derived from STATUS_TO_HANDLER so the two can never drift.

    Args:
        deps: A dict with all keyword dependencies required by the handler
              functions above.  Produced by VocalAIApp._motor_handler_deps().

    Returns:
        A dict mapping motor status strings to zero-argument callables.
        Unknown status strings will be missing from the dict (caller does
        .get(status) and skips None).
    """
    _g = globals()
    return {
        status: (lambda fn=_g[fname]: lambda: fn(**deps))()
        for status, fname in STATUS_TO_HANDLER.items()
    }
