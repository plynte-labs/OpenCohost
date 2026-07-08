"""Tests for opencohost/ui/motor_event_handlers.py (Stage 3 extraction).

Strict-TDD RED phase — tests are written BEFORE the module exists.

Coverage:
  1. Dispatch table maps all 17 keys to the right callable (14 distinct handlers,
     3 alias keys: model_switch_applied, llm_tier_switch_applied, llm_tier_switch_failed).
  2. Unknown status is a no-op (get_handler_map(...).get(status) returns None).
  3. Widget-touching handlers marshal mutations via schedule_ui_update (not inline).
  4. on_motor_switch_failed clears model_status error->ready on rollback (Fix A).
  5. Timer helpers (_start/_stop/_tick speaking_alt, _reset/_stop inactivity, _on_inactivity_timeout)
     use only injected after / after_cancel (never a bare self.after).
"""

from __future__ import annotations

from unittest.mock import MagicMock, call


# ---------------------------------------------------------------------------
# Helpers — minimal dep dict so tests don't need a real VocalAIApp
# ---------------------------------------------------------------------------

def _make_deps(**overrides):
    """Build a minimal dependency dict for get_handler_map.

    All widget attrs are MagicMock so .configure() is tracked.
    schedule_ui_update captures the scheduled lambdas; tests execute them to
    verify the widget is mutated only inside the lambda, not inline.
    """
    from opencohost.ui.state import UIState

    scheduled = []

    def schedule_ui_update(fn, delay_ms=0):
        scheduled.append(fn)

    motor = MagicMock()
    motor.current_model = "gemma4:e2b"
    motor.llm_tiers = MagicMock()
    motor.llm_tiers.config.as_dict.return_value = {}
    motor.active_llm_tier = "fast"
    motor._last_switch_failure = None
    motor._last_llm_failure = None

    ui_state = UIState()

    deps = dict(
        ui_state=ui_state,
        motor_ia=motor,
        model_panel=MagicMock(),
        voice_panel=MagicMock(),
        ptt=MagicMock(),
        audio_bed=MagicMock(),
        avatar_bridge=MagicMock(),
        btn_grabar=MagicMock(),
        btn_voz=MagicMock(),
        btn_ws=MagicMock(),
        btn_primary_voice=MagicMock(),
        btn_enviar=MagicMock(),
        btn_download=MagicMock(),
        btn_mapear=MagicMock(),
        combo_modelos=MagicMock(),
        switch_ptt=MagicMock(),
        progress_download=MagicMock(),
        schedule_ui_update=schedule_ui_update,
        actualizar_pipeline=MagicMock(),
        log_accion=MagicMock(),
        print_log=MagicMock(),
        notify_operator=MagicMock(),
        check_pending_audio_bed_stop=MagicMock(),
        kira_agenda_schedule_tick=MagicMock(),
        kira_agenda_update_status=MagicMock(),
        kira_agenda_prefetch_while_speaking=MagicMock(),
        kira_agenda_clear_prefetch=MagicMock(),
        kira_agenda_consume_pending_chat_if_due=MagicMock(return_value=False),
        kira_agenda_play_prefetched_if_ready=MagicMock(return_value=False),
        on_ptt_press=MagicMock(),
        on_ptt_release=MagicMock(),
        on_ptt_click=MagicMock(),
        get_speaking_is_alt=lambda: False,
        set_speaking_is_alt=MagicMock(),
        get_speaking_alt_timer_id=lambda: None,
        set_speaking_alt_timer_id=MagicMock(),
        get_inactivity_timer_id=lambda: None,
        set_inactivity_timer_id=MagicMock(),
        inactivity_timeout_ms=120_000,
        after=MagicMock(return_value="timer-id-1"),
        after_cancel=MagicMock(),
        set_title=MagicMock(),
        kira_agenda=None,
        winfo_exists=lambda: True,
        _scheduled=scheduled,  # test helper — not read by module
    )
    deps.update(overrides)
    return deps


def _flush(deps, count=None):
    """Execute all (or first `count`) scheduled lambdas and return the list."""
    scheduled = deps["_scheduled"]
    to_run = scheduled[:count] if count is not None else list(scheduled)
    for fn in to_run:
        fn()
    return to_run


# ---------------------------------------------------------------------------
# 1. Dispatch table — all 17 keys
# ---------------------------------------------------------------------------

EXPECTED_17_KEYS = {
    "ready",
    "model_warming",
    "ollama_unavailable",
    "processing",
    "idle",
    "llm_timeout_recovered",
    "speaking_start",
    "speaking_end",
    "model_changed",
    "model_switch_pending",
    "model_switch_applied",
    "model_switch_failed",
    "llm_tier_switch_applied",
    "llm_tier_switch_failed",
    "download_start",
    "download_done",
    "download_error",
}


class TestDispatchTable:
    def test_all_17_keys_present(self):
        from opencohost.ui.motor_event_handlers import get_handler_map
        deps = _make_deps()
        table = get_handler_map(deps)
        assert EXPECTED_17_KEYS <= set(table.keys()), (
            f"Missing keys: {EXPECTED_17_KEYS - set(table.keys())}"
        )

    def test_unknown_status_returns_none(self):
        from opencohost.ui.motor_event_handlers import get_handler_map
        deps = _make_deps()
        table = get_handler_map(deps)
        assert table.get("this_does_not_exist") is None

    def test_all_17_keys_are_callable(self):
        from opencohost.ui.motor_event_handlers import get_handler_map
        deps = _make_deps()
        table = get_handler_map(deps)
        for key in EXPECTED_17_KEYS:
            assert callable(table[key]), f"Key '{key}' is not callable"

    def test_aliases_model_switch_applied_equals_model_changed(self):
        """model_switch_applied is an alias for on_motor_model_changed."""
        from opencohost.ui.motor_event_handlers import get_handler_map
        deps = _make_deps()
        table = get_handler_map(deps)
        # Both keys must call the same underlying logic — execute each and
        # verify they both update the title (a reliable model_changed signal).
        motor = deps["motor_ia"]
        motor.current_model = "alias-test-model"
        # Call model_changed handler
        table["model_changed"]()
        mc_calls = list(deps["_scheduled"])
        deps["_scheduled"].clear()
        # Call alias
        table["model_switch_applied"]()
        alias_calls = list(deps["_scheduled"])
        # Both should have scheduled the same number of title updates
        assert len(mc_calls) > 0
        assert len(mc_calls) == len(alias_calls)

    def test_aliases_llm_tier_switch_applied_equals_model_changed(self):
        """llm_tier_switch_applied is an alias for on_motor_model_changed."""
        from opencohost.ui.motor_event_handlers import get_handler_map
        deps = _make_deps()
        table = get_handler_map(deps)
        motor = deps["motor_ia"]
        motor.current_model = "tier-alias-model"
        table["model_changed"]()
        mc_count = len(deps["_scheduled"])
        deps["_scheduled"].clear()
        table["llm_tier_switch_applied"]()
        assert len(deps["_scheduled"]) == mc_count

    def test_alias_llm_tier_switch_failed_equals_model_switch_failed(self):
        """llm_tier_switch_failed is an alias for on_motor_switch_failed."""
        from opencohost.ui.motor_event_handlers import get_handler_map
        deps = _make_deps()
        # Give it a current_model so Fix-A branch runs (simpler path)
        deps["motor_ia"].current_model = "fallback-model"
        table = get_handler_map(deps)
        table["model_switch_failed"]()
        mf_count = len(deps["_scheduled"])
        deps["_scheduled"].clear()
        table["llm_tier_switch_failed"]()
        assert len(deps["_scheduled"]) == mf_count


# ---------------------------------------------------------------------------
# 2. Widget-touching handlers marshal via schedule_ui_update
# ---------------------------------------------------------------------------

class TestMarshalingViaScheduleUiUpdate:
    """Widget mutations must be INSIDE scheduled lambdas, not called inline."""

    def _assert_configure_not_called_before_flush(self, widget_key, handler_key, deps=None):
        if deps is None:
            deps = _make_deps()
        from opencohost.ui.motor_event_handlers import get_handler_map
        table = get_handler_map(deps)
        widget = deps[widget_key]
        table[handler_key]()
        # Widget must NOT be mutated inline
        widget.configure.assert_not_called()
        # After flushing scheduled lambdas, mutation occurs
        _flush(deps)
        widget.configure.assert_called()

    def test_on_motor_ready_btn_grabar_not_mutated_inline(self, monkeypatch):
        # btn_grabar is heavy-TTS-gated (qwen_tts_extirpation WU 1.2); force the
        # opt-in so the marshaling path is still exercised.
        import opencohost.ui.motor_event_handlers as meh
        monkeypatch.setattr(meh, "EXPERIMENTAL_HEAVY_TTS_ENABLED", True, raising=False)
        self._assert_configure_not_called_before_flush("btn_grabar", "ready")

    def test_on_motor_ready_btn_enviar_not_mutated_inline(self):
        self._assert_configure_not_called_before_flush("btn_enviar", "ready")

    def test_on_motor_model_warming_btn_download_not_mutated_inline(self):
        self._assert_configure_not_called_before_flush("btn_download", "model_warming")

    def test_on_motor_ollama_unavailable_btn_grabar_not_mutated_inline(self, monkeypatch):
        import opencohost.ui.motor_event_handlers as meh
        monkeypatch.setattr(meh, "EXPERIMENTAL_HEAVY_TTS_ENABLED", True, raising=False)
        self._assert_configure_not_called_before_flush("btn_grabar", "ollama_unavailable")

    def test_on_motor_processing_btn_enviar_not_mutated_inline(self):
        self._assert_configure_not_called_before_flush("btn_enviar", "processing")

    def test_on_motor_idle_btn_enviar_not_mutated_inline(self):
        self._assert_configure_not_called_before_flush("btn_enviar", "idle")

    def test_on_motor_download_start_btn_download_not_mutated_inline(self):
        self._assert_configure_not_called_before_flush("btn_download", "download_start")

    def test_on_motor_download_done_combo_not_mutated_inline(self):
        self._assert_configure_not_called_before_flush("combo_modelos", "download_done")

    def test_on_motor_switch_failed_model_panel_not_mutated_inline(self):
        deps = _make_deps()
        deps["motor_ia"].current_model = "gemma4:e2b"
        from opencohost.ui.motor_event_handlers import get_handler_map
        table = get_handler_map(deps)
        table["model_switch_failed"]()
        deps["model_panel"].restore_to_active_model.assert_not_called()
        _flush(deps)
        deps["model_panel"].restore_to_active_model.assert_called()

    def test_on_motor_model_changed_title_not_set_inline(self):
        deps = _make_deps()
        from opencohost.ui.motor_event_handlers import get_handler_map
        table = get_handler_map(deps)
        table["model_changed"]()
        deps["set_title"].assert_not_called()
        _flush(deps)
        deps["set_title"].assert_called()


# ---------------------------------------------------------------------------
# 6. Reference-voice cluster is heavy-TTS-gated (qwen_tts_extirpation WU 1.2)
# ---------------------------------------------------------------------------

class TestReferenceClusterFlagGate:
    """on_motor_ready enables btn_grabar/btn_voz (the heavy-TTS reference cluster)
    ONLY when heavy TTS is opted in. The light controls (btn_ws / btn_enviar /
    btn_primary_voice) are always enabled, regardless of the flag."""

    def _ready(self, monkeypatch, flag):
        import opencohost.ui.motor_event_handlers as meh
        monkeypatch.setattr(meh, "EXPERIMENTAL_HEAVY_TTS_ENABLED", flag, raising=False)
        deps = _make_deps()
        table = meh.get_handler_map(deps)
        table["ready"]()
        _flush(deps)
        return deps

    def test_reference_cluster_enabled_on_ready_when_flag_on(self, monkeypatch):
        deps = self._ready(monkeypatch, True)
        deps["btn_grabar"].configure.assert_called_with(state="normal")
        deps["btn_voz"].configure.assert_called_with(state="normal")

    def test_reference_cluster_not_enabled_on_ready_when_flag_off(self, monkeypatch):
        deps = self._ready(monkeypatch, False)
        deps["btn_grabar"].configure.assert_not_called()
        deps["btn_voz"].configure.assert_not_called()

    def test_light_controls_enabled_on_ready_regardless_of_flag(self, monkeypatch):
        deps = self._ready(monkeypatch, False)
        deps["btn_ws"].configure.assert_called_with(state="normal")
        deps["btn_enviar"].configure.assert_called_with(state="normal")
        deps["btn_primary_voice"].configure.assert_called_with(state="normal")


# ---------------------------------------------------------------------------
# 3. Fix A: on_motor_switch_failed clears model_status error->ready on rollback
# ---------------------------------------------------------------------------

class TestFixAModelStatusReset:
    def test_model_status_cleared_when_rollback_succeeded(self):
        """If current_model is set, error pill must clear to 'ready' after delay."""
        deps = _make_deps()
        deps["motor_ia"].current_model = "gemma4:e2b"
        # _safe_after stub runs synchronously (mirror test_app_shell_status_stale.py)
        executed_with_delay = []
        def sync_safe_after(fn, delay_ms=0):
            executed_with_delay.append((delay_ms, fn))
            fn()
        deps["schedule_ui_update"] = sync_safe_after
        from opencohost.ui.motor_event_handlers import get_handler_map
        table = get_handler_map(deps)
        deps["ui_state"].model_status = "error"
        table["model_switch_failed"]()
        assert deps["ui_state"].model_status == "ready", (
            "model_status should be cleared to 'ready' after rollback"
        )
        # The deferred reset must carry a delay_ms > 0
        delayed_calls = [d for d, _ in executed_with_delay if d > 0]
        assert delayed_calls, "Fix A reset must be deferred (delay_ms > 0)"

    def test_model_status_stays_error_without_current_model(self):
        """If no rollback model, error pill must STAY as 'error'."""
        deps = _make_deps()
        deps["motor_ia"].current_model = None
        executed = []
        def sync_safe_after(fn, delay_ms=0):
            executed.append(fn)
            fn()
        deps["schedule_ui_update"] = sync_safe_after
        from opencohost.ui.motor_event_handlers import get_handler_map
        table = get_handler_map(deps)
        deps["ui_state"].model_status = "error"
        table["model_switch_failed"]()
        assert deps["ui_state"].model_status == "error", (
            "model_status must stay 'error' when no rollback model"
        )

    def test_model_status_stays_error_on_empty_current_model(self):
        """Empty string current_model is falsy — no rollback."""
        deps = _make_deps()
        deps["motor_ia"].current_model = ""
        def sync_safe_after(fn, delay_ms=0):
            fn()
        deps["schedule_ui_update"] = sync_safe_after
        from opencohost.ui.motor_event_handlers import get_handler_map
        table = get_handler_map(deps)
        deps["ui_state"].model_status = "error"
        table["model_switch_failed"]()
        assert deps["ui_state"].model_status == "error"

    def test_fix_a_via_llm_tier_switch_failed_alias(self):
        """Same Fix A logic runs through the llm_tier_switch_failed alias."""
        deps = _make_deps()
        deps["motor_ia"].current_model = "gemma4:e2b"
        def sync_safe_after(fn, delay_ms=0):
            fn()
        deps["schedule_ui_update"] = sync_safe_after
        from opencohost.ui.motor_event_handlers import get_handler_map
        table = get_handler_map(deps)
        deps["ui_state"].model_status = "error"
        table["llm_tier_switch_failed"]()
        assert deps["ui_state"].model_status == "ready"


# ---------------------------------------------------------------------------
# 4. Timer helpers use only injected after / after_cancel
# ---------------------------------------------------------------------------

class TestTimerHelpersUseInjectedAfter:
    """_start_speaking_alt_timer, _tick_speaking_alt, _reset_inactivity_timer etc.
    must call only the injected `after` and `after_cancel` callables."""

    def test_start_speaking_alt_timer_calls_injected_after(self):
        from opencohost.ui.motor_event_handlers import on_motor_speaking_start
        deps = _make_deps()
        # Provide avatar_bridge so the avatar path runs
        on_motor_speaking_start(**_filter_kwargs(on_motor_speaking_start, deps))
        # The speaking_alt timer uses injected after (via _tick_speaking_alt)
        deps["after"].assert_called()

    def test_stop_speaking_alt_timer_calls_injected_after_cancel(self):
        from opencohost.ui.motor_event_handlers import on_motor_speaking_end
        deps = _make_deps()
        timer_id = "fake-timer"
        deps["get_speaking_alt_timer_id"] = lambda: timer_id
        deps["voice_panel"].is_ws_connected.return_value = False
        on_motor_speaking_end(**_filter_kwargs(on_motor_speaking_end, deps))
        deps["after_cancel"].assert_called_with(timer_id)

    def test_tick_speaking_alt_reschedules_via_injected_after(self):
        from opencohost.ui.motor_event_handlers import tick_speaking_alt
        deps = _make_deps()
        tick_speaking_alt(**_filter_kwargs(tick_speaking_alt, deps))
        deps["after"].assert_called()

    def test_reset_inactivity_timer_uses_injected_after(self):
        from opencohost.ui.motor_event_handlers import reset_inactivity_timer
        deps = _make_deps()
        reset_inactivity_timer(**_filter_kwargs(reset_inactivity_timer, deps))
        deps["after"].assert_called()

    def test_stop_inactivity_timer_calls_after_cancel(self):
        from opencohost.ui.motor_event_handlers import stop_inactivity_timer
        deps = _make_deps()
        timer_id = "inactivity-timer-id"
        deps["get_inactivity_timer_id"] = lambda: timer_id
        stop_inactivity_timer(**_filter_kwargs(stop_inactivity_timer, deps))
        deps["after_cancel"].assert_called_with(timer_id)


# ---------------------------------------------------------------------------
# 5. Individual handler smoke tests — verify key side-effects fire correctly
# ---------------------------------------------------------------------------

class TestHandlerSideEffects:
    def test_on_motor_ready_sets_ui_state_model_status_ready(self):
        deps = _make_deps()
        from opencohost.ui.motor_event_handlers import get_handler_map
        table = get_handler_map(deps)
        table["ready"]()
        assert deps["ui_state"].model_status == "ready"

    def test_on_motor_model_warming_sets_ui_state_model_status_loading(self):
        deps = _make_deps()
        from opencohost.ui.motor_event_handlers import get_handler_map
        table = get_handler_map(deps)
        table["model_warming"]()
        assert deps["ui_state"].model_status == "loading"

    def test_on_motor_switch_failed_sets_ui_state_model_status_error_first(self):
        deps = _make_deps()
        deps["motor_ia"].current_model = None  # no rollback — stays error
        from opencohost.ui.motor_event_handlers import get_handler_map
        table = get_handler_map(deps)
        table["model_switch_failed"]()
        assert deps["ui_state"].model_status == "error"

    def test_on_motor_idle_calls_actualizar_pipeline(self):
        deps = _make_deps()
        from opencohost.ui.motor_event_handlers import get_handler_map
        table = get_handler_map(deps)
        table["idle"]()
        deps["actualizar_pipeline"].assert_called_with("idle")

    def test_on_motor_processing_calls_actualizar_pipeline(self):
        deps = _make_deps()
        from opencohost.ui.motor_event_handlers import get_handler_map
        table = get_handler_map(deps)
        table["processing"]()
        deps["actualizar_pipeline"].assert_called_with("processing")

    def test_on_motor_download_start_calls_actualizar_pipeline(self):
        deps = _make_deps()
        from opencohost.ui.motor_event_handlers import get_handler_map
        table = get_handler_map(deps)
        table["download_start"]()
        deps["actualizar_pipeline"].assert_called_with("downloading")

    def test_on_motor_download_done_calls_actualizar_pipeline(self):
        deps = _make_deps()
        from opencohost.ui.motor_event_handlers import get_handler_map
        table = get_handler_map(deps)
        table["download_done"]()
        deps["actualizar_pipeline"].assert_called_with("idle")

    def test_on_motor_download_error_calls_actualizar_pipeline(self):
        deps = _make_deps()
        from opencohost.ui.motor_event_handlers import get_handler_map
        table = get_handler_map(deps)
        table["download_error"]()
        deps["actualizar_pipeline"].assert_called_with("error")

    def test_on_motor_model_changed_calls_actualizar_pipeline_idle(self):
        deps = _make_deps()
        from opencohost.ui.motor_event_handlers import get_handler_map
        table = get_handler_map(deps)
        table["model_changed"]()
        deps["actualizar_pipeline"].assert_called_with("idle")

    def test_on_motor_llm_timeout_recovered_calls_print_log(self):
        deps = _make_deps()
        from opencohost.ui.motor_event_handlers import get_handler_map
        table = get_handler_map(deps)
        table["llm_timeout_recovered"]()
        deps["print_log"].assert_called()

    def test_on_motor_speaking_start_calls_log_accion(self):
        deps = _make_deps()
        from opencohost.ui.motor_event_handlers import get_handler_map
        table = get_handler_map(deps)
        table["speaking_start"]()
        deps["log_accion"].assert_called()

    def test_on_motor_speaking_end_calls_stop_speaking_alt_timer(self):
        """speaking_end must cancel the alt timer via after_cancel."""
        deps = _make_deps()
        timer_id = "alt-timer"
        deps["get_speaking_alt_timer_id"] = lambda: timer_id
        deps["voice_panel"].is_ws_connected.return_value = False
        from opencohost.ui.motor_event_handlers import get_handler_map
        table = get_handler_map(deps)
        table["speaking_end"]()
        deps["after_cancel"].assert_called_with(timer_id)

    def test_on_piper_voice_locale_mismatch_notifies_operator(self):
        """Honest-degrade notice (design D8/pillar 5) must reach the operator,
        not just the raw ui_callback seam — dispatch must call notify_operator."""
        deps = _make_deps()
        from opencohost.ui.motor_event_handlers import get_handler_map
        table = get_handler_map(deps)
        table["piper_voice_locale_mismatch"]()
        deps["notify_operator"].assert_called_once()


# ---------------------------------------------------------------------------
# Helper — filter kwargs to only those accepted by a function
# ---------------------------------------------------------------------------

import inspect

def _filter_kwargs(fn, deps):
    """Return a subset of deps that fn's signature accepts as keyword args."""
    sig = inspect.signature(fn)
    return {k: v for k, v in deps.items() if k in sig.parameters}


# ---------------------------------------------------------------------------
# 6. STATUS_TO_HANDLER consistency guard
#    Catches any future edit that adds a key without a matching module function
#    or shell delegate.
# ---------------------------------------------------------------------------

class TestStatusToHandlerConsistency:
    """Guard that STATUS_TO_HANDLER, get_handler_map, module functions, and
    VocalAIApp delegates all stay in sync.

    If a future implementer adds an entry to STATUS_TO_HANDLER but forgets to
    add the module-level handler function or the shell delegate, one of these
    tests will fail loudly.
    """

    def test_get_handler_map_keys_match_status_to_handler(self):
        """get_handler_map must expose exactly the keys in STATUS_TO_HANDLER."""
        from opencohost.ui.motor_event_handlers import STATUS_TO_HANDLER, get_handler_map
        deps = _make_deps()
        table = get_handler_map(deps)
        assert set(table.keys()) == set(STATUS_TO_HANDLER.keys()), (
            f"get_handler_map keys differ from STATUS_TO_HANDLER keys.\n"
            f"  Extra in map:     {set(table.keys()) - set(STATUS_TO_HANDLER.keys())}\n"
            f"  Missing from map: {set(STATUS_TO_HANDLER.keys()) - set(table.keys())}"
        )

    def test_every_handler_name_is_a_module_callable(self):
        """Every value in STATUS_TO_HANDLER must be a callable in motor_event_handlers."""
        import opencohost.ui.motor_event_handlers as _m
        from opencohost.ui.motor_event_handlers import STATUS_TO_HANDLER
        missing = [
            fname for fname in STATUS_TO_HANDLER.values()
            if not callable(getattr(_m, fname, None))
        ]
        assert not missing, (
            f"STATUS_TO_HANDLER references function names missing from the module: {missing}"
        )

    def test_every_handler_name_has_shell_delegate(self):
        """Every value in STATUS_TO_HANDLER must have a '_<name>' delegate on VocalAIApp."""
        from opencohost.ui import app_shell
        from opencohost.ui.motor_event_handlers import STATUS_TO_HANDLER
        app = object.__new__(app_shell.VocalAIApp)
        missing = [
            fname for fname in STATUS_TO_HANDLER.values()
            if not callable(getattr(type(app), "_" + fname, None))
        ]
        assert not missing, (
            f"STATUS_TO_HANDLER references handler names with no '_<name>' delegate "
            f"on VocalAIApp: {missing}"
        )
