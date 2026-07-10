"""Agenda/audio cluster — extracted from app_shell.py (app_shell_agenda_audio_decomposition_20260624 Phase 7).

Function-module + thin same-named delegate on VocalAIApp — the exact pattern of
obs_lifecycle.py and motor_event_handlers.py (NEVER a mixin).  Bodies moved
verbatim from VocalAIApp; ``self.X`` became injected values/callables supplied
by app_shell._agenda_audio_deps(shell).

Contract (mirrors the obs_lifecycle docstring contract):
- This module must NEVER import opencohost.ui.app_shell or the VocalAIApp class.
- torch/ollama/transformers are never imported here.
- All widget mutation goes through injected callables; timers only via the
  injected ``after`` / ``after_cancel``.  kira_agenda_update_status keeps its
  verbatim ``after(0, ...)`` widget pushes (it is only ever invoked on the Tk
  main thread today via re-marshaled motor events / _safe_after-driven polling).
- Shared state (_pending_audio_bed_stop, _kira_agenda_previous_filter_policy,
  timer ids, prefetched action, idle/suggestion counters) is OWNED by the shell
  as instance attributes; this module reads/writes it ONLY via injected
  getter/setter callables.  The state is never cached or duplicated here.
- Intra-cluster calls route through the injected shell-bound delegate callables
  (e.g. kira_agenda_tick calls the injected kira_agenda_update_status), never
  sibling module functions directly — instance-level MagicMock stubs in the
  existing tests must keep intercepting.
- ``generate_suggestions`` is NOT imported here: it is injected from app_shell
  so existing tests that patch app_shell.generate_suggestions keep working
  (same reason obs_lifecycle takes obs_client_cls from app_shell).
"""

from __future__ import annotations

import threading
from typing import Any, Callable

from opencohost.smart_aggregator import AgendaAction, AgendaState, ErrorCode, RecoveryPolicy
from opencohost.smart_aggregator.chat_input_contract import ChatContextPacketBuilder
from opencohost.config.logger import get_logger

logger = get_logger()


# ---------------------------------------------------------------------------
# Cluster A — agenda tick loop + FR2 idle suggestions
# ---------------------------------------------------------------------------

def dispatch_suggestion_recompute(
    *,
    gen: int,
    existing_topics: list,
    last_outputs: list,
    session_id: str,
    smart_agg: Any,
    motor_ia: Any,
    generate_suggestions: Callable[..., list],
    schedule_ui_update: Callable[..., None],
    apply_idle_suggestions: Callable[..., None],
    thread_cls: Any = None,
    **_: Any,
) -> None:
    """FR2: run heavy idle-tick reads on a worker; marshal result back via _safe_after."""
    def _worker() -> None:
        try:
            intent_summary = smart_agg.intent_aggregator.summarize()
            vibe_data = smart_agg.thermometer.compute_vibe()
            vibe_temp = vibe_data.get("temperature", 50) if isinstance(vibe_data, dict) else 50
            snapshots = smart_agg.history.get_recent_context_snapshots(session_id, max_items=3)
            suggestions = generate_suggestions(
                intent_summary=intent_summary,
                snapshots=snapshots,
                vibe_temperature=vibe_temp,
                existing_topics=existing_topics,
                last_outputs=last_outputs,
            )
        except Exception:
            logger.exception("idle-suggestion recompute failed")
            return
        # Topic Scout (topic_scout_llm_20260629): optional LLM "third source".
        # scout_digest() is fully self-contained (returns [] on any failure and
        # when SCOUT_ENABLED is off); wrapped defensively so it can never take
        # the rule-based suggestions down with it.
        try:
            scout_titles = motor_ia.scout_digest()
        except Exception:
            logger.exception("topic scout failed")
            scout_titles = []
        if scout_titles:
            suggestions = list(suggestions) + list(scout_titles)
        schedule_ui_update(lambda: apply_idle_suggestions(gen, suggestions))
    _Thread = thread_cls if thread_cls is not None else threading.Thread
    _Thread(target=_worker, daemon=True).start()


def apply_idle_suggestions(
    *,
    gen: int,
    suggestions: list,
    kira_agenda: Any,
    is_closing: Callable[[], bool],
    get_suggestion_gen: Callable[[], int],
    kira_agenda_update_status: Callable[[], None],
    **_: Any,
) -> None:
    """FR2: apply idle suggestions on UI thread (supersession guards: generation + agenda state)."""
    # Post-teardown guard: a late worker can marshal here after on_closing
    # set _closing; do not touch Tk/agenda during/after teardown.
    if is_closing():
        return
    if gen != get_suggestion_gen():
        return
    if kira_agenda is None or kira_agenda.state != AgendaState.IDLE:
        return
    if suggestions:
        kira_agenda.suggest_topics(suggestions)
        kira_agenda_update_status()


def kira_agenda_tick(
    *,
    kira_agenda: Any,
    motor_ia: Any,
    smart_agg: Any,
    stream_admin_log: Callable[[str], None],
    enqueue_kira_agenda_action: Callable[..., None],
    kira_agenda_restore_chat_filter: Callable[[], None],
    kira_agenda_update_status: Callable[[], None],
    kira_agenda_schedule_tick: Callable[[int], None],
    clear_obs_joyita: Callable[[str], None],
    dispatch_suggestion_recompute: Callable[..., None],
    get_idle_ticks: Callable[[], int],
    set_idle_ticks: Callable[[int], None],
    get_suggestion_gen: Callable[[], int],
    set_suggestion_gen: Callable[[int], None],
    **_: Any,
) -> None:
    if kira_agenda is None:
        return

    # Auto-recovery: if PAUSED, check if the recovery policy permits a retry
    if kira_agenda.state == AgendaState.PAUSED_NEEDS_OPERATOR:
        if kira_agenda.can_auto_resume():
            stream_admin_log(
                f"[Kira Agenda] Auto-recuperación: intento {kira_agenda.recovery.retry_attempt}"
                f" de {len(RecoveryPolicy.RETRY_DELAYS_SECONDS)}"
                f" | Error: {kira_agenda.recovery.error_code.human()}"
            )
            # Fall through to next_action — state is now IDLE
        elif kira_agenda.state == AgendaState.HARD_PAUSED:
            stream_admin_log(
                "[Kira Agenda] HARD_PAUSED: se requiere intervención del streamer. "
                f"Motivo: {kira_agenda.recovery.last_failure_reason or 'fallos repetidos'}"
            )
            kira_agenda_update_status()
            return  # No tick for HARD_PAUSED
        else:
            # Timer not ready — reschedule and wait
            kira_agenda_update_status()
            kira_agenda_schedule_tick(4500)
            return

    # Auto-exit: when IDLE with no active topic and nothing queued,
    # co-host has exhausted all planned work.  Transition to OFF so
    # the tick stops, the UI reflects reality, and chat flows through
    # the standalone RF3 path without overhead.
    # Moved AFTER next_action() so _select_next_topic() has a chance
    # to pick the next queued topic before we declare the session done.

    try:
        action = kira_agenda.next_action(
            motor_busy=getattr(motor_ia, "is_processing", False),
            kira_speaking=getattr(motor_ia, "is_speaking", False),
        )
    except Exception:
        # Safety net: if next_action ever throws (shouldn't — it's
        # deterministic and runs on internal state only), log, skip
        # this tick, and reschedule so Kira doesn't go permanently
        # silent on a live stream.
        logger.exception("KiraAgendaController.next_action() raised")
        stream_admin_log(
            "[Kira Agenda] Error interno en next_action; reintentando en el siguiente tick."
        )
        action = AgendaAction.none()

    enqueue_kira_agenda_action(action)

    if (
        action.kind == "none"
        and kira_agenda.state == AgendaState.IDLE
        and kira_agenda.active_topic is None
        and not kira_agenda.queued_topics()
    ):
        kira_agenda.state = AgendaState.OFF
        stream_admin_log("[Kira Agenda] Sesión completada: sin temas pendientes.")
        kira_agenda_restore_chat_filter()
        kira_agenda_update_status()
        clear_obs_joyita("KiraJoyita")
        return

    # Auto-suggestion trigger: on 3rd consecutive IDLE+empty-queue tick (~13.5s)
    if kira_agenda.state == AgendaState.IDLE and not kira_agenda.queued_topics():
        set_idle_ticks(get_idle_ticks() + 1)
        if get_idle_ticks() >= 3 and kira_agenda.can_suggest():
            # Rich-context gate: skip if aggregator has no data
            if smart_agg and getattr(smart_agg, "_session_id", None):
                # FR2: snapshot mutable agenda state on main thread, then dispatch
                # heavy reads (summarize/compute_vibe/snapshots) off the UI thread.
                existing_topics = list(kira_agenda.topics)
                last_outputs = list(kira_agenda.last_outputs)
                set_suggestion_gen(get_suggestion_gen() + 1)
                dispatch_suggestion_recompute(
                    get_suggestion_gen(), existing_topics, last_outputs, smart_agg._session_id,
                )
            set_idle_ticks(0)
    else:
        set_idle_ticks(0)

    kira_agenda_update_status()
    kira_agenda_schedule_tick(4500)


def kira_agenda_schedule_tick(
    *,
    delay_ms: int,
    kira_agenda: Any,
    kira_agenda_tick: Callable[[], None],
    get_kira_agenda_tick_id: Callable[[], Any],
    set_kira_agenda_tick_id: Callable[[Any], None],
    after: Callable[..., Any],
    after_cancel: Callable[[Any], None],
    **_: Any,
) -> None:
    if kira_agenda.state in {AgendaState.OFF, AgendaState.PAUSED_NEEDS_OPERATOR, AgendaState.HARD_PAUSED}:
        return
    if get_kira_agenda_tick_id() is not None:
        try:
            after_cancel(get_kira_agenda_tick_id())
        except Exception:
            pass
    set_kira_agenda_tick_id(after(delay_ms, kira_agenda_tick))


# ---------------------------------------------------------------------------
# Cluster B — prefetch
# ---------------------------------------------------------------------------

def enqueue_kira_agenda_action(
    *,
    action: AgendaAction,
    motor_ia: Any,
    set_prefetched_action: Callable[[Any], None],
    **_: Any,
) -> None:
    if action.kind != "enqueue" or not action.prompt:
        return
    set_prefetched_action(None)
    if hasattr(motor_ia, "clear_prefetched_agenda"):
        motor_ia.clear_prefetched_agenda()
    if action.source.startswith("kira-agenda") and hasattr(motor_ia, "replace_pending"):
        motor_ia.replace_pending(action.prompt, priority=action.priority, source=action.source)
    else:
        motor_ia.enqueue(
            action.prompt, priority=action.priority, source=action.source,
            history_text=action.history_text,
        )


def kira_agenda_has_higher_priority_pending(
    *,
    action: AgendaAction,
    motor_ia: Any,
    **_: Any,
) -> bool:
    if not hasattr(motor_ia, "has_pending_priority_before"):
        return False
    return bool(motor_ia.has_pending_priority_before(action.priority))


def kira_agenda_has_non_agenda_audio_work(
    *,
    motor_ia: Any,
    **_: Any,
) -> bool:
    """Return True when a human/direct path owns processing or speech."""
    processing_source = str(getattr(motor_ia, "current_processing_source", "") or "")
    speech_source = str(getattr(motor_ia, "current_speech_source", "") or "")
    processing = bool(getattr(motor_ia, "is_processing", False))
    speaking = bool(getattr(motor_ia, "is_speaking", False))

    if processing and not processing_source.startswith("kira-agenda"):
        return True
    if speaking and not speech_source.startswith("kira-agenda"):
        return True
    return False


def kira_agenda_consume_pending_chat_if_due(
    *,
    kira_agenda: Any,
    get_pending_compact_chat: Callable[[], str],
    set_pending_compact_chat: Callable[[str], None],
    enqueue_kira_agenda_action: Callable[..., None],
    kira_agenda_update_status: Callable[[], None],
    **_: Any,
) -> bool:
    compact_chat = get_pending_compact_chat().strip()
    if not compact_chat or kira_agenda is None:
        return False
    if not hasattr(kira_agenda, "chat_signal_due") or not kira_agenda.chat_signal_due():
        return False
    action = kira_agenda.next_action(compact_chat=compact_chat)
    if action.kind != "enqueue":
        return False
    set_pending_compact_chat("")
    enqueue_kira_agenda_action(action)
    kira_agenda_update_status()
    return True


def kira_agenda_prefetch_while_speaking(
    *,
    kira_agenda: Any,
    motor_ia: Any,
    is_kira_agenda_speech_source: Callable[[], bool],
    set_prefetched_action: Callable[[Any], None],
    **_: Any,
) -> None:
    if kira_agenda is None or not hasattr(motor_ia, "prefetch_agenda"):
        return
    if not is_kira_agenda_speech_source():
        return
    action = kira_agenda.prefetch_action_after_current_speech()
    if action.kind != "enqueue" or not action.prompt:
        return
    if motor_ia.prefetch_agenda(action.prompt, priority=action.priority, source=action.source):
        set_prefetched_action(action)


def kira_agenda_play_prefetched_if_ready(
    *,
    kira_agenda: Any,
    motor_ia: Any,
    is_closing: Callable[[], bool],
    get_prefetched_action: Callable[[], Any],
    set_prefetched_action: Callable[[Any], None],
    get_prefetch_retry_id: Callable[[], Any],
    set_prefetch_retry_id: Callable[[Any], None],
    kira_agenda_has_higher_priority_pending: Callable[..., bool],
    kira_agenda_has_non_agenda_audio_work: Callable[[], bool],
    kira_agenda_clear_prefetch: Callable[[], bool],
    kira_agenda_play_prefetched_if_ready: Callable[[], bool],
    kira_agenda_update_status: Callable[[], None],
    stream_admin_log: Callable[[str], None],
    after: Callable[..., Any],
    after_cancel: Callable[[Any], None],
    **_: Any,
) -> bool:
    # Guard: teardown in progress — do not reschedule.
    if is_closing():
        return False
    action = get_prefetched_action()
    if not action or not hasattr(motor_ia, "wait_prefetched_agenda"):
        return False
    if kira_agenda_has_higher_priority_pending(action):
        stream_admin_log("[Kira Agenda] Prefetch pausado: hay PTT/chat pendiente con más prioridad.")
        return kira_agenda_clear_prefetch()
    if kira_agenda_has_non_agenda_audio_work():
        stream_admin_log("[Kira Agenda] Prefetch cancelado: hay interacción directa activa.")
        return kira_agenda_clear_prefetch()
    # fix #B: non-blocking check (timeout=0); retry via after() if not ready yet.
    if not motor_ia.wait_prefetched_agenda(timeout=0):
        # Cancel any existing retry before scheduling a new one (double-schedule guard).
        if get_prefetch_retry_id() is not None:
            try:
                after_cancel(get_prefetch_retry_id())
            except Exception:
                pass
        # This method is always called from the Tk event loop (main thread).
        # Use after() directly to capture the cancellable ID.
        try:
            set_prefetch_retry_id(after(50, kira_agenda_play_prefetched_if_ready))
        except RuntimeError:
            set_prefetch_retry_id(None)
        return False
    if not kira_agenda.start_prefetched_action(action):
        stream_admin_log("[Kira Agenda] Prefetch descartado: el tema ya se completó.")
        return kira_agenda_clear_prefetch()
    set_prefetched_action(None)
    if motor_ia.play_prefetched_agenda():
        kira_agenda_update_status()
        return True
    return False


def kira_agenda_clear_prefetch(
    *,
    motor_ia: Any,
    set_prefetched_action: Callable[[Any], None],
    get_prefetch_retry_id: Callable[[], Any],
    set_prefetch_retry_id: Callable[[Any], None],
    after_cancel: Callable[[Any], None],
    **_: Any,
) -> bool:
    set_prefetched_action(None)
    # Cancel any pending prefetch retry to avoid a leaked timer (fix #4).
    if get_prefetch_retry_id() is not None:
        try:
            after_cancel(get_prefetch_retry_id())
        except Exception:
            pass
        set_prefetch_retry_id(None)
    if hasattr(motor_ia, "clear_prefetched_agenda"):
        motor_ia.clear_prefetched_agenda()
    return False


# ---------------------------------------------------------------------------
# Cluster C — audio coordination (post-FR1/FR3 forms)
# ---------------------------------------------------------------------------

def kira_agenda_enable(
    *,
    kira_agenda: Any,
    audio_bed: Any,
    stream_admin_log: Callable[[str], None],
    kira_agenda_force_strict_chat_filter: Callable[[], None],
    kira_agenda_update_status: Callable[[], None],
    kira_agenda_tick: Callable[[], None],
    dispatch_audio_play: Callable[[Callable[[], Any]], None],
    **_: Any,
) -> None:
    if not kira_agenda.queued_topics() and not kira_agenda.active_topic:
        stream_admin_log("[Kira Agenda] No se activa: no hay temas en cola.")
        kira_agenda_update_status()
        return
    kira_agenda_force_strict_chat_filter()
    kira_agenda.enable()
    if kira_agenda.state == AgendaState.OFF:
        # Fail-closed gate refused (GUARDRAILS_MISSING) — do not claim
        # activation or start the music bed. _kira_agenda_update_status
        # already renders the error code/label to the operator.
        stream_admin_log("[Kira Agenda] No se activa: guardrails de agenda faltantes.")
        kira_agenda_update_status()
        return
    stream_admin_log("[Kira Agenda] Modo co-host con agenda activado.")
    # Start music bed when co-host mode activates — it's an intentional segment.
    # FR3: the idle check is now atomic inside request_mood (only_if_idle=True)
    # so the check-then-act race between concurrent enables is eliminated.
    # request_mood already returns False when not enabled, so no pre-check needed.
    if audio_bed is not None:
        dispatch_audio_play(
            lambda: audio_bed.request_mood("normal", force=True, boundary=True, only_if_idle=True)
        )
    kira_agenda_update_status()
    kira_agenda_tick()


def kira_agenda_soft_stop(
    *,
    kira_agenda: Any,
    audio_bed: Any,
    stream_admin_log: Callable[[str], None],
    enqueue_kira_agenda_action: Callable[..., None],
    check_pending_audio_bed_stop: Callable[[], None],
    kira_agenda_update_status: Callable[[], None],
    set_pending_audio_bed_stop: Callable[[bool], None],
    **_: Any,
) -> None:
    action = kira_agenda.soft_stop()
    enqueue_kira_agenda_action(action)
    stream_admin_log("[Kira Agenda] Stop suave solicitado.")
    # Fix 3: defer the graceful audio_bed.stop() so it fires only AFTER
    # Kira finishes her in-flight closing speech.  Setting this flag here
    # tells _check_pending_audio_bed_stop (called from _on_motor_speaking_end)
    # to call audio_bed.stop(emergency=False) once the agenda is fully OFF.
    if audio_bed is not None:
        set_pending_audio_bed_stop(True)
        # Round 2 fix: when soft_stop() fast-exits (agenda already OFF/IDLE/
        # WAITING_SIGNAL with no active topic), no speech is enqueued so
        # _on_motor_speaking_end never fires.  Call the helper synchronously
        # here so the graceful stop fires immediately in that case.  When the
        # agenda IS still speaking, the helper's state != OFF guard returns
        # early and the existing deferred path (_on_motor_speaking_end) still
        # handles it — both call sites are intentionally kept.
        check_pending_audio_bed_stop()
    kira_agenda_update_status()


def check_pending_audio_bed_stop(
    *,
    kira_agenda: Any,
    audio_bed: Any,
    get_pending_audio_bed_stop: Callable[[], bool],
    set_pending_audio_bed_stop: Callable[[bool], None],
    **_: Any,
) -> None:
    """Fire the deferred graceful audio-bed stop if the agenda has finished.

    Called from _on_motor_speaking_end after each speech segment ends.
    Does nothing if the pending flag is not set or the agenda is still active.
    """
    if not get_pending_audio_bed_stop():
        return
    if audio_bed is None:
        set_pending_audio_bed_stop(False)
        return
    # Only fire once the agenda has fully wound down (state OFF).
    # Intermediate states (SPEAKING, GENERATING, TOPIC_CLOSING) mean the
    # closing speech is still being delivered — do not stop the music yet.
    from opencohost.smart_aggregator.kira_agenda_controller import AgendaState
    if kira_agenda is not None and kira_agenda.state != AgendaState.OFF:
        return
    set_pending_audio_bed_stop(False)
    audio_bed.stop(emergency=False)


def kira_agenda_emergency_stop(
    *,
    kira_agenda: Any,
    motor_ia: Any,
    audio_bed: Any,
    stream_admin_log: Callable[[str], None],
    kira_agenda_restore_chat_filter: Callable[[], None],
    kira_agenda_update_status: Callable[[], None],
    clear_obs_joyita: Callable[[str], None],
    set_prefetched_action: Callable[[Any], None],
    set_pending_audio_bed_stop: Callable[[bool], None],
    get_kira_agenda_tick_id: Callable[[], Any],
    set_kira_agenda_tick_id: Callable[[Any], None],
    get_prefetch_retry_id: Callable[[], Any],
    set_prefetch_retry_id: Callable[[Any], None],
    after_cancel: Callable[[Any], None],
    **_: Any,
) -> None:
    set_prefetched_action(None)
    kira_agenda.emergency_stop()
    if get_kira_agenda_tick_id() is not None:
        try:
            after_cancel(get_kira_agenda_tick_id())
        except Exception:
            pass
        set_kira_agenda_tick_id(None)
    # Cancel any pending prefetch retry (fix #B).
    if get_prefetch_retry_id() is not None:
        try:
            after_cancel(get_prefetch_retry_id())
        except Exception:
            pass
        set_prefetch_retry_id(None)
    # Bug 4 fix: set _speaking=False on the motor so the in-flight _hablar
    # thread's consumer loop exits immediately instead of draining the full
    # pre-filled audio queue.  Do this before drop_pending_sources so the
    # engine sees the interrupt signal first.
    if motor_ia is not None:
        motor_ia.interrupt_speaking()
        if hasattr(motor_ia, "drop_pending_sources"):
            motor_ia.drop_pending_sources(("kira-agenda",))
    # Bug 1 fix: hard-stop the music bed immediately on emergency teardown.
    # Secondary fix: clear any stale _pending_audio_bed_stop flag set by a
    # prior soft_stop, so a deferred speaking_end callback cannot trigger an
    # unwanted graceful stop after the hard stop has already fired.
    if audio_bed is not None:
        set_pending_audio_bed_stop(False)
        audio_bed.stop(emergency=True)
    stream_admin_log("[Kira Agenda] Emergencia: agenda detenida y pendientes descartados.")
    kira_agenda_restore_chat_filter()
    clear_obs_joyita("KiraJoyita")
    kira_agenda_update_status()


# ---------------------------------------------------------------------------
# Cluster D — chat-filter lifecycle
# ---------------------------------------------------------------------------

def kira_agenda_force_strict_chat_filter(
    *,
    smart_agg: Any,
    has_previous_filter_policy: Callable[[], bool],
    set_previous_filter_policy: Callable[[Any], None],
    **_: Any,
) -> None:
    if not smart_agg:
        return
    if not has_previous_filter_policy():
        set_previous_filter_policy(smart_agg.get_filter_policy())
    smart_agg.set_filter_policy("strict")


def kira_agenda_restore_chat_filter(
    *,
    smart_agg: Any,
    get_previous_filter_policy: Callable[[], Any],
    clear_previous_filter_policy: Callable[[], Any],
    **_: Any,
) -> None:
    if not smart_agg:
        return
    previous = get_previous_filter_policy()
    if previous:
        smart_agg.set_filter_policy(previous)
        clear_previous_filter_policy()


# ---------------------------------------------------------------------------
# Cluster E — status / aggregator glue + operator entry
# ---------------------------------------------------------------------------

def kira_agenda_add_topic(
    *,
    title: str,
    angle: str,
    constraints: list[str],
    priority: str = "normal",
    response_length: str = "normal",
    max_turns: int | None = None,
    kira_agenda: Any,
    notify_operator: Callable[..., None],
    stream_admin_log: Callable[[str], None],
    kira_agenda_update_status: Callable[[], None],
    **_: Any,
) -> None:
    title = (title or "").strip()
    try:
        if max_turns is not None:
            kira_agenda.set_session_settings(max_turns_per_topic=max_turns, response_length=response_length)
        topic = kira_agenda.add_topic(title, angle, constraints, approved=True, priority=priority, response_length=response_length)
        kira_agenda.queue_topic(topic.id)
    except ValueError as e:
        notify_operator("Kira Agenda", str(e))
        return
    stream_admin_log(f"[Kira Agenda] Tema aprobado y encolado: {topic.title} ({topic.priority}; sesión: {kira_agenda.rhythm}/{kira_agenda.response_length}, {kira_agenda.max_turns_per_topic} turnos globales)")
    kira_agenda_update_status()


def kira_agenda_update_status(
    *,
    kira_agenda: Any,
    stream_admin_ui: Any,
    cohost_agenda_panel: Any,
    agenda_persistence: Any,
    status_bar: Any,
    ui_state: Any,
    get_topic_inbox_bridge: Callable[[], Any],
    get_agenda_was_paused: Callable[[], bool],
    set_agenda_was_paused: Callable[[bool], None],
    after: Callable[..., Any],
    **_: Any,
) -> None:
    if stream_admin_ui is None or kira_agenda is None:
        return
    # Write-through persistence: every agenda mutation funnels through
    # this method; the call is a no-op when nothing actually changed.
    agenda_persistence.save_if_changed(kira_agenda)
    active = kira_agenda.active_topic
    queued = len(kira_agenda.queued_topics())
    recovery = kira_agenda.recovery
    error_info = ""
    if recovery.error_code != ErrorCode.NONE:
        error_info = f" · ⚠ {recovery.error_code.human()} (intento {recovery.retry_attempt}/{len(RecoveryPolicy.RETRY_DELAYS_SECONDS)})"
    if active:
        text = f"Kira está desarrollando: “{active.title}” · Estado: {kira_agenda.state.value} · modo: {kira_agenda.safety_mode} · fallos: {kira_agenda.failure_count}{error_info}"
        current_topic = f"“{active.title}”\nPrioridad: {active.priority} · Sesión: {kira_agenda.rhythm}/{kira_agenda.response_length}/{kira_agenda.safety_mode}\nTurnos hablados: {active.turns_spoken}/{kira_agenda.max_turns_per_topic}"
    else:
        text = f"Agenda: {kira_agenda.state.value} · temas en cola: {queued} · modo: {kira_agenda.safety_mode} · fallos: {kira_agenda.failure_count}{error_info}"
        current_topic = "Sin tema activo"
    if kira_agenda.state == AgendaState.HARD_PAUSED:
        color = "#cc3333"
    elif kira_agenda.state == AgendaState.PAUSED_NEEDS_OPERATOR:
        color = "#ffaa00"
    else:
        color = "#8fa3b8"
    after(0, lambda: stream_admin_ui.set_agenda_status(text, color))
    if cohost_agenda_panel is not None:
        queue_lines = [
            f"{idx}. [{topic.priority}] {topic.title}"
            for idx, topic in enumerate(kira_agenda.queued_topics(), start=1)
        ]
        after(0, lambda: cohost_agenda_panel.update_status(
            state=kira_agenda.state.value,
            current_topic=current_topic,
            queue_lines=queue_lines,
            failures=kira_agenda.failure_count,
            error_code=recovery.error_code.human(),
            error_reasons=recovery.last_reasons,
        ))
        # Forward DRAFTED + inbox proposals to the suggestions panel
        # Guard: bridge is None until _on_agenda_loaded fires (fix #C).
        topic_inbox_bridge = get_topic_inbox_bridge()
        if topic_inbox_bridge is not None:
            suggestions_list = topic_inbox_bridge.build_suggestions(kira_agenda)
            after(0, lambda sl=suggestions_list: cohost_agenda_panel.update_suggestions(sl))
    # BUG-003: visual signal for PAUSED_NEEDS_OPERATOR via TTS pill
    # Only update on state transitions to avoid overwriting pipeline-driven TTS state
    was_paused = get_agenda_was_paused()
    is_paused = kira_agenda.state == AgendaState.PAUSED_NEEDS_OPERATOR
    if status_bar and was_paused != is_paused:
        # Route through the UIState setter (single source of truth + validation)
        # instead of poking StatusBar directly. "paused" is a VALID_TTS_STATUSES value.
        if is_paused:
            after(0, lambda: setattr(ui_state, "tts_status", "paused"))
        else:
            after(0, lambda: setattr(ui_state, "tts_status", "idle"))
    set_agenda_was_paused(is_paused)


def on_smart_aggregated_context(
    *,
    data: dict,
    kira_agenda: Any,
    motor_ia: Any,
    smart_agg_ui: Any,
    enqueue_kira_agenda_action: Callable[..., None],
    kira_agenda_update_status: Callable[[], None],
    set_pending_compact_chat: Callable[[str], None],
    log_accion: Callable[[str], None],
    **_: Any,
) -> None:
    """Route compact chat either to Agenda Mode or the existing RF3 reaction."""
    # When the controller is PAUSED_NEEDS_OPERATOR, chat must fall
    # through to the standalone RF3 reaction path — otherwise chat
    # responses are silently dropped while the operator sees a frozen UI.
    if (
        kira_agenda
        and kira_agenda.state not in {AgendaState.OFF, AgendaState.PAUSED_NEEDS_OPERATOR, AgendaState.HARD_PAUSED}
        # When IDLE with no active topic and nothing queued, co-host has
        # exhausted all planned topics.  Let chat fall through to the
        # standalone RF3 reaction path instead of being silently consumed
        # by next_action() which returns none() in IDLE+empty state.
        and not (
            kira_agenda.state == AgendaState.IDLE
            and kira_agenda.active_topic is None
            and not kira_agenda.queued_topics()
        )
    ):
        intent_summary = data.get("intent_summary") or {}
        compact_chat = intent_summary.get("prompt") or ""
        if not compact_chat:
            # SECURITY: this fallback joins UNSUMMARIZED viewer chat. It is
            # contained downstream by _build_prompt's read-only data delimiters
            # (KiraAgendaController._wrap_untrusted_chat), which make prompt
            # injection structurally inert. The full fix — routing this path
            # through the structured ChatContextPacket so RAW chat never reaches
            # the prompt — is the "Raw-Chat Prompt Exposure" track in
            # conductor/tracks.md. Do not remove the downstream delimiters.
            context = data.get("context", [])[-6:]
            compact_chat = "\n".join(m.get("text", "") for m in context if m.get("text"))
        if getattr(motor_ia, "is_processing", False) or getattr(motor_ia, "is_speaking", False):
            set_pending_compact_chat(compact_chat.strip())
            kira_agenda_update_status()
            return
        action = kira_agenda.next_action(
            motor_busy=getattr(motor_ia, "is_processing", False),
            kira_speaking=getattr(motor_ia, "is_speaking", False),
            compact_chat=compact_chat,
        )
        enqueue_kira_agenda_action(action)
        kira_agenda_update_status()
        return
    # ── Standalone RF3 path ─────────────────────────────────────────
    # Phase B: use ChatContextPacket instead of defective compact_chat
    import opencohost.smart_aggregator.chat_input_contract as _ic
    if _ic.USE_INPUT_CONTRACT_PROMPT:
        try:
            context = data.get("context", [])
            intent_summary = data.get("intent_summary", {})
            old_compact = intent_summary.get("prompt", "")

            builder = ChatContextPacketBuilder()
            packet = builder.build(context)

            if not packet.should_call_llm:
                # No useful signal — stay silent instead of generating
                # "qué paz", "qué silencio", etc.
                log_accion(
                    f"[InputContract] stay_silent: "
                    f"msgs={packet.total_messages} users={packet.unique_users} "
                    f"event={packet.primary_event} confidence={packet.confidence:.2f}"
                )
                return

            # Valid signal: use packet context as prompt source
            new_context = packet.to_prompt_context()
            # Inject into data dict for smart_agg_ui
            data["intent_summary"]["prompt"] = new_context
            data["_source_used"] = "input_contract"

            log_accion(
                f"[InputContract] using packet: "
                f"event={packet.primary_event} goal={packet.response_goal} "
                f"highlight={'yes' if packet.selected_highlight else 'no'} "
                f"clusters={len(packet.topic_clusters)} "
                f"old_compact={old_compact[:80]!r}"
            )
        except Exception:
            # Fallback: use old compact_chat on any error
            data["_source_used"] = "fallback_old_compact"
    else:
        data["_source_used"] = "old_compact"

    smart_agg_ui.on_aggregated_context(data)
