"""is_local gating boundary — multi_provider_llm_20260723 Phase 3.

Pins the byte-identical local-path guarantee (spec B) plus the cloud-branch
behavior: both chat call sites branch on ``_is_local``; local-only machinery
(warm/unload, ollama.list/show/pull, gemma override, reasoning discovery,
reactive trim, watchdog model-rollback) is skipped when the active provider is
cloud; the watchdog timeout resolves to ``CLOUD_CHAT_TIMEOUT``; the pregen gate
defaults OFF on cloud; and ``set_provider_config`` swaps the config under the
engine lock so a racing PUT only affects the NEXT call.

Cloud dispatch is mocked (``cloud_llm_client.send_chat_completion``) — no real
network, no real key. The regression suites (test_llm_engine_tiers,
test_llm_tiers, test_llm_engine_model_trace, test_llm_engine_piper_fallback)
prove the local path stays byte-identical.
"""

import logging
import os
import queue
import sys
import threading
import time
from unittest.mock import MagicMock, patch
from opencohost.config.settings import CLOUD_CHAT_TIMEOUT, CLOUD_CTX_BUDGET, LLM_TEMPERATURE

import pytest

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)


_SEND = "opencohost.core.providers.cloud.cloud_llm_client.send_chat_completion"


def _make_motor(tmp_path, *, provider_config=None):
    """Construct a MotorVocalIA with ollama/pygame mocked and last_model isolated."""
    import opencohost.config.settings as settings

    original_last_model = settings.LAST_MODEL_FILE
    settings.LAST_MODEL_FILE = os.path.join(str(tmp_path), "last_model.json")

    log_queue = queue.Queue()
    ui_events = []

    try:
        from opencohost.core.llm_engine import MotorVocalIA
        motor = MotorVocalIA(log_queue, ui_events.append)
    finally:
        settings.LAST_MODEL_FILE = original_last_model

    motor.ollama = MagicMock()
    motor.ollama.chat = MagicMock(return_value={"message": {"content": "local", "thinking": ""}})
    motor.pygame = MagicMock()
    motor.is_ready = True
    motor._loaded_model = motor.current_model
    if provider_config is not None:
        motor._provider_config = provider_config
    return motor, log_queue, ui_events


def _cloud_config(pregen_enabled=False):
    return {
        "active_provider": "openai",
        "fallback_mode": "auto",
        "pregen_enabled": pregen_enabled,
        "profiles": {
            "openai": {"base_url": "https://api.example.com/v1", "model": "gpt-cloud"},
        },
    }


def _wait_until(predicate, *, timeout=1.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def _find_sentinel(obj, sentinel, depth=0, seen=None):
    """Recursively scan dict/list/str structure for `sentinel`; skips Mocks and
    arbitrary objects (only str/dict/list/tuple/set are traversed)."""
    if seen is None:
        seen = set()
    if depth > 6 or id(obj) in seen:
        return False
    seen.add(id(obj))
    if isinstance(obj, str):
        return sentinel in obj
    if isinstance(obj, dict):
        return any(
            _find_sentinel(k, sentinel, depth + 1, seen)
            or _find_sentinel(v, sentinel, depth + 1, seen)
            for k, v in obj.items()
        )
    if isinstance(obj, (list, tuple, set)):
        return any(_find_sentinel(x, sentinel, depth + 1, seen) for x in obj)
    return False


def _arm_connector_upgrade(motor):
    """Satisfy every connector-upgrade precondition so the ONLY gate that can
    still stop `_generar_dialogo` is the cloud pregen gate (F1)."""
    motor.speech_remaining_estimate = MagicMock(return_value=999.0)
    motor._frozen_stash = {"connector": None}
    motor._prefetched_agenda = None
    motor._pregen_inflight = None
    motor._llm_generating = False
    motor._generar_dialogo = MagicMock(return_value="linea de conexion")
    motor._preview_accept_agenda_output = MagicMock(return_value=True)


def _arm_scout(motor, monkeypatch):
    """Satisfy every scout precondition up to dispatch so the ONLY gate that can
    still stop the cloud dispatch is the cloud pregen gate (F4)."""
    import opencohost.core.llm_engine as engine
    monkeypatch.setattr(engine, "SCOUT_ENABLED", True, raising=False)
    motor._loaded_model = "resident-model"
    motor._pending_model_switch = None
    motor._awaiting_first_success_after_switch = False
    motor._check_capabilities_reasoning = MagicMock(return_value=False)
    motor.has_pending_priority_before = MagicMock(return_value=False)
    motor._scout_render_history = MagicMock(
        return_value=["Host: a", "Kira: b", "Host: c"] * 4
    )
    motor._scout_last_input_hash = None


# ---------------------------------------------------------------------------
# 1. _is_local property
# ---------------------------------------------------------------------------

class TestIsLocalProperty:
    def test_default_config_is_local(self, tmp_path):
        motor, _, _ = _make_motor(tmp_path)
        assert motor._is_local is True

    def test_cloud_provider_is_not_local(self, tmp_path):
        motor, _, _ = _make_motor(tmp_path, provider_config=_cloud_config())
        assert motor._is_local is False

    def test_missing_active_provider_defaults_local(self, tmp_path):
        motor, _, _ = _make_motor(tmp_path, provider_config={"profiles": {}})
        assert motor._is_local is True

    def test_active_profile_none_when_local(self, tmp_path):
        motor, _, _ = _make_motor(tmp_path)
        assert motor._active_profile() is None

    def test_active_profile_returns_cloud_profile(self, tmp_path):
        motor, _, _ = _make_motor(tmp_path, provider_config=_cloud_config())
        profile = motor._active_profile()
        assert profile["base_url"] == "https://api.example.com/v1"
        assert profile["model"] == "gpt-cloud"


# ---------------------------------------------------------------------------
# 2. Both chat call sites branch on _is_local
# ---------------------------------------------------------------------------

class TestChatBranch:
    def test_local_ollama_chat_uses_ollama_client(self, tmp_path):
        motor, _, _ = _make_motor(tmp_path)
        with patch(_SEND) as mock_send:
            result = motor._ollama_chat(model="qwen3:1.7b", messages=[], options={})
        motor.ollama.chat.assert_called_once()
        mock_send.assert_not_called()
        assert result["message"]["content"] == "local"

    def test_cloud_ollama_chat_dispatches_to_cloud_client(self, tmp_path):
        motor, _, _ = _make_motor(tmp_path, provider_config=_cloud_config())
        adapted = {"message": {"content": "cloud", "thinking": ""}, "usage": {}}
        with patch(_SEND, return_value=adapted) as mock_send:
            result = motor._ollama_chat(
                model="qwen3:1.7b",  # LOCAL tag — must be ignored for cloud
                messages=[{"role": "user", "content": "x"}],
                keep_alive=5,
                options={"num_predict": 100, "temperature": 0.5},
            )
        assert result["message"]["content"] == "cloud"
        motor.ollama.chat.assert_not_called()
        mock_send.assert_called_once()
        kwargs = mock_send.call_args.kwargs
        # base_url + model come from the ACTIVE PROFILE, not the passed local tag
        assert kwargs["base_url"] == "https://api.example.com/v1"
        assert kwargs["model"] == "gpt-cloud"

    def test_cloud_scout_chat_dispatches_to_cloud_client(self, tmp_path):
        motor, _, _ = _make_motor(tmp_path, provider_config=_cloud_config())
        adapted = {"message": {"content": "scout-cloud", "thinking": ""}, "usage": {}}
        with patch(_SEND, return_value=adapted) as mock_send:
            result = motor._ollama_scout_chat(model="qwen3:1.7b", messages=[], options={})
        assert result["message"]["content"] == "scout-cloud"
        motor.ollama.chat.assert_not_called()
        mock_send.assert_called_once()


# ---------------------------------------------------------------------------
# 3. Watchdog timeout resolution
# ---------------------------------------------------------------------------

class TestWatchdogTimeout:
    def test_local_uses_inference_timeout(self, tmp_path):
        motor, _, _ = _make_motor(tmp_path)
        assert motor._resolve_chat_watchdog_timeout(motor.current_model) == motor._inference_watchdog_timeout

    def test_cloud_uses_cloud_constant(self, tmp_path):
        motor, _, _ = _make_motor(tmp_path, provider_config=_cloud_config())
        assert motor._resolve_chat_watchdog_timeout("anything") == CLOUD_CHAT_TIMEOUT
        assert CLOUD_CHAT_TIMEOUT != motor._inference_watchdog_timeout


# ---------------------------------------------------------------------------
# 4. Local-only machinery gated behind is_local
# ---------------------------------------------------------------------------

class TestLocalOnlyMachineryGated:
    def test_cloud_check_ollama_service_skips_list(self, tmp_path):
        motor, _, _ = _make_motor(tmp_path, provider_config=_cloud_config())
        motor.ollama.list = MagicMock()
        assert motor._check_ollama_service() is True
        motor.ollama.list.assert_not_called()

    def test_cloud_prepare_model_skips_generate(self, tmp_path):
        motor, _, _ = _make_motor(tmp_path, provider_config=_cloud_config())
        motor.ollama.generate = MagicMock()
        motor._warmed_model = None
        result = motor._prepare_model("gpt-cloud")
        motor.ollama.generate.assert_not_called()
        assert result is True  # no local model to warm; treated as ready

    def test_local_prepare_model_still_warms(self, tmp_path):
        motor, _, _ = _make_motor(tmp_path)
        motor.ollama.generate = MagicMock(return_value={"response": "ok"})
        motor._warmed_model = None
        motor._prepare_model("qwen3:1.7b")
        warmed = [
            c.kwargs.get("model")
            for c in motor.ollama.generate.call_args_list
            if c.kwargs.get("prompt") == "Responde solo: ok"
        ]
        assert warmed == ["qwen3:1.7b"]

    def test_cloud_download_worker_skips_pull(self, tmp_path):
        motor, _, _ = _make_motor(tmp_path, provider_config=_cloud_config())
        motor.ollama.pull = MagicMock()
        motor._download_model_worker("some:model")
        motor.ollama.pull.assert_not_called()

    def test_cloud_fetch_show_skips_ollama_show(self, tmp_path):
        motor, _, _ = _make_motor(tmp_path, provider_config=_cloud_config())
        import ollama
        with patch.object(ollama, "show") as mock_show:
            assert motor._fetch_show("gpt-cloud") == {}
        mock_show.assert_not_called()

    def test_cloud_discover_ctx_degrades_without_show(self, tmp_path):
        from opencohost.config.settings import CTX_FALLBACK_DEFAULT
        motor, _, _ = _make_motor(tmp_path, provider_config=_cloud_config())
        import ollama
        with patch.object(ollama, "show") as mock_show:
            assert motor._discover_model_ctx("gpt-cloud") == CTX_FALLBACK_DEFAULT
        mock_show.assert_not_called()

    def test_cloud_capabilities_reasoning_false_without_show(self, tmp_path):
        motor, _, _ = _make_motor(tmp_path, provider_config=_cloud_config())
        import ollama
        with patch.object(ollama, "show") as mock_show:
            assert motor._check_capabilities_reasoning("gpt-cloud") is False
        mock_show.assert_not_called()


# ---------------------------------------------------------------------------
# 5. Full cloud generation path — dispatch + gemma gate + usage log
# ---------------------------------------------------------------------------

class TestCloudGeneration:
    def test_cloud_generation_dispatches_and_gates_gemma(self, tmp_path):
        motor, _, _ = _make_motor(tmp_path, provider_config=_cloud_config())
        # A local gemma tag must NOT trigger the gemma temperature override on cloud.
        motor.current_model = "gemma:2b"
        motor._loaded_model = "gemma:2b"
        captured = {}

        def fake_send(**kwargs):
            captured.update(kwargs)
            return {"message": {"content": "cloud reply", "thinking": ""}, "usage": {"total_tokens": 5}}

        with patch(_SEND, side_effect=fake_send):
            result = motor._generar_dialogo("hola", source="direct", commit_history=False)

        assert result == "cloud reply"
        motor.ollama.chat.assert_not_called()
        # gemma override (temperature=0.7) is local-only -> cloud keeps LLM_TEMPERATURE
        assert captured["options"]["temperature"] == LLM_TEMPERATURE
        assert captured["options"]["temperature"] != 0.7
        assert captured["model"] == "gpt-cloud"

    def test_cloud_generation_logs_usage(self, tmp_path, caplog):
        motor, _, _ = _make_motor(tmp_path, provider_config=_cloud_config())
        adapted = {"message": {"content": "reply", "thinking": ""}, "usage": {"total_tokens": 42}}
        with patch(_SEND, return_value=adapted):
            with caplog.at_level(logging.INFO, logger="OpenCohost"):
                motor._generar_dialogo("hola", source="direct", commit_history=False)
        usage_logs = [r for r in caplog.records if "cloud_llm_usage" in r.message]
        assert len(usage_logs) >= 1
        assert "42" in usage_logs[0].message

    def test_cloud_timeout_skips_model_rollback(self, tmp_path):
        motor, _, _ = _make_motor(tmp_path, provider_config=_cloud_config())
        motor._recover_from_stalled_inference = MagicMock()
        motor._ollama_chat_with_watchdog = MagicMock(
            side_effect=TimeoutError("watchdog_timeout:90.00s")
        )
        result = motor._generar_dialogo("hola", source="direct", commit_history=False)
        assert result == ""
        motor._recover_from_stalled_inference.assert_not_called()

    def test_local_timeout_triggers_model_rollback(self, tmp_path):
        motor, _, _ = _make_motor(tmp_path)
        motor._recover_from_stalled_inference = MagicMock()
        motor._ollama_chat_with_watchdog = MagicMock(
            side_effect=TimeoutError("watchdog_timeout:180.00s")
        )
        result = motor._generar_dialogo("hola", source="direct", commit_history=False)
        assert result == ""
        motor._recover_from_stalled_inference.assert_called_once()


# ---------------------------------------------------------------------------
# 6. Pregen cloud gate
# ---------------------------------------------------------------------------

class TestPregenCloudGate:
    def test_cloud_untouched_returns_false_no_generation(self, tmp_path):
        motor, _, _ = _make_motor(tmp_path, provider_config=_cloud_config(pregen_enabled=False))
        motor._generar_dialogo = MagicMock(return_value="draft")
        result = motor.pregenerate("some text", source="chat")
        assert result is False
        # No background worker should have been spawned.
        assert not _wait_until(lambda: motor._generar_dialogo.called, timeout=0.25)
        motor._generar_dialogo.assert_not_called()

    def test_cloud_enabled_proceeds(self, tmp_path):
        motor, _, _ = _make_motor(tmp_path, provider_config=_cloud_config(pregen_enabled=True))
        motor._generar_dialogo = MagicMock(return_value="draft")
        result = motor.pregenerate("some text", source="chat")
        assert result is True
        assert _wait_until(lambda: motor._generar_dialogo.called, timeout=1.5)

    def test_local_ignores_pregen_gate(self, tmp_path):
        # Local: pregen_enabled=False must NOT block (gate is cloud-only).
        motor, _, _ = _make_motor(tmp_path, provider_config={
            "active_provider": "local", "pregen_enabled": False, "profiles": {},
        })
        motor._generar_dialogo = MagicMock(return_value="draft")
        result = motor.pregenerate("some text", source="chat")
        assert result is True
        assert _wait_until(lambda: motor._generar_dialogo.called, timeout=1.5)


# ---------------------------------------------------------------------------
# 7. set_provider_config live-swap under _lock (race pin)
# ---------------------------------------------------------------------------

class TestSetProviderConfig:
    def test_swaps_active_provider(self, tmp_path):
        motor, _, _ = _make_motor(tmp_path)
        assert motor._is_local is True
        motor.set_provider_config(_cloud_config())
        assert motor._is_local is False
        assert motor._active_profile()["model"] == "gpt-cloud"

    def test_ignores_non_dict(self, tmp_path):
        motor, _, _ = _make_motor(tmp_path)
        before = motor._provider_config
        motor.set_provider_config("not a dict")
        assert motor._provider_config is before
        assert motor._is_local is True

    def test_inflight_generation_pinned_to_original_posture(self, tmp_path):
        """F2: a `set_provider_config` swap that lands mid-generation (after the
        entry snapshot, before dispatch) must NOT tear the in-flight call — it
        stays wholly on the ORIGINAL posture (local backend + local ctx budget);
        only the NEXT call observes the new provider."""
        motor, _, _ = _make_motor(tmp_path)  # start local
        motor.current_model = "qwen3:1.7b"  # non-gemma, non-reasoning
        motor._model_ctx_limit = {"qwen3:1.7b": 4096}
        motor._reasoning_model_cache = {"qwen3:1.7b": False}

        captured = {}

        def local_chat(**kwargs):
            captured["options"] = kwargs.get("options")
            return {"message": {"content": "local reply", "thinking": ""}}

        motor.ollama.chat.side_effect = local_chat

        # A PUT lands mid-generation: interpose the swap AFTER the posture
        # snapshot but BEFORE dispatch (the watchdog-timeout resolve sits between).
        orig_resolve = motor._resolve_chat_watchdog_timeout

        def swap_then_resolve(model, **kw):
            motor.set_provider_config(_cloud_config())
            return orig_resolve(model, **kw)

        motor._resolve_chat_watchdog_timeout = swap_then_resolve

        with patch(_SEND, return_value={
            "message": {"content": "CLOUD-LEAK", "thinking": ""}, "usage": {},
        }) as mock_send:
            result = motor._generar_dialogo("hola", source="direct", commit_history=False)

        # In-flight call completed wholly under the ORIGINAL (local) posture.
        assert result == "local reply"
        motor.ollama.chat.assert_called_once()
        mock_send.assert_not_called()
        # Local ctx budget, never the cloud budget (torn-state harm (a) would show 32768).
        assert captured["options"]["num_ctx"] != CLOUD_CTX_BUDGET
        # The swap DID land — the NEXT call sees the new provider.
        assert motor._is_local is False
        with patch(_SEND, return_value={
            "message": {"content": "cloud", "thinking": ""}, "usage": {},
        }) as mock_send2:
            motor._generar_dialogo("otra", source="direct", commit_history=False)
        mock_send2.assert_called_once()

    def test_inflight_turn_pinned_when_fallback_flag_flips(self, tmp_path):
        """F2 (multi_provider_llm_20260723): the EFFECTIVE posture is snapshotted
        ONCE at `_generar_dialogo` entry as
        `_cfg_is_local(cfg) or _cloud_fallback_active`. A `_cloud_fallback_active`
        flip mid-turn (e.g. an operator PUT clearing it) must NOT re-derive
        locality at dispatch/timeout — the in-flight turn completes wholly on its
        ENTRY posture (local backend + local watchdog), never leaking to cloud
        with locally-built options."""
        motor, _, _ = _make_motor(tmp_path, provider_config=_cloud_config())
        # Fallback engaged: config says cloud, but the flag pins THIS turn local.
        motor._cloud_fallback_active = True
        motor.current_model = "qwen3:1.7b"  # non-gemma, non-reasoning
        motor._model_ctx_limit = {"qwen3:1.7b": 4096}
        motor._reasoning_model_cache = {"qwen3:1.7b": False}

        captured = {}

        def local_chat(**kwargs):
            captured["options"] = kwargs.get("options")
            return {"message": {"content": "local reply", "thinking": ""}}

        motor.ollama.chat.side_effect = local_chat

        # The flag clears mid-generation, AFTER the entry snapshot but BEFORE
        # dispatch (the watchdog-timeout resolve sits between).
        orig_resolve = motor._resolve_chat_watchdog_timeout

        def clear_then_resolve(model, **kw):
            motor._cloud_fallback_active = False
            return orig_resolve(model, **kw)

        motor._resolve_chat_watchdog_timeout = clear_then_resolve

        with patch(_SEND, return_value={
            "message": {"content": "CLOUD-LEAK", "thinking": ""}, "usage": {},
        }) as mock_send:
            result = motor._generar_dialogo("hola", source="direct", commit_history=False)

        # In-flight call completed wholly under the ENTRY (local) posture.
        assert result == "local reply"
        motor.ollama.chat.assert_called_once()
        mock_send.assert_not_called()
        assert captured["options"]["num_ctx"] != CLOUD_CTX_BUDGET


# ---------------------------------------------------------------------------
# 8. Connector-upgrade cloud pregen gate (F1)
# ---------------------------------------------------------------------------

class TestConnectorUpgradeCloudGate:
    def test_cloud_off_connector_upgrade_skips_generation(self, tmp_path):
        motor, _, _ = _make_motor(tmp_path, provider_config=_cloud_config(pregen_enabled=False))
        _arm_connector_upgrade(motor)
        motor._maybe_generate_connector_upgrade()
        assert not _wait_until(lambda: motor._generar_dialogo.called, timeout=0.3)
        motor._generar_dialogo.assert_not_called()

    def test_cloud_on_connector_upgrade_proceeds(self, tmp_path):
        motor, _, _ = _make_motor(tmp_path, provider_config=_cloud_config(pregen_enabled=True))
        _arm_connector_upgrade(motor)
        motor._maybe_generate_connector_upgrade()
        assert _wait_until(lambda: motor._generar_dialogo.called, timeout=1.5)

    def test_local_connector_upgrade_untouched(self, tmp_path):
        motor, _, _ = _make_motor(tmp_path)  # local
        _arm_connector_upgrade(motor)
        motor._maybe_generate_connector_upgrade()
        assert _wait_until(lambda: motor._generar_dialogo.called, timeout=1.5)


# ---------------------------------------------------------------------------
# 9. Scout cloud pregen gate (F4)
# ---------------------------------------------------------------------------

class TestScoutCloudGate:
    def test_cloud_off_scout_skips_dispatch(self, tmp_path, monkeypatch):
        motor, _, _ = _make_motor(tmp_path, provider_config=_cloud_config(pregen_enabled=False))
        _arm_scout(motor, monkeypatch)
        with patch(_SEND) as mock_send:
            result = motor.scout_digest()
        assert result == []
        mock_send.assert_not_called()

    def test_cloud_on_scout_dispatches(self, tmp_path, monkeypatch):
        motor, _, _ = _make_motor(tmp_path, provider_config=_cloud_config(pregen_enabled=True))
        _arm_scout(motor, monkeypatch)
        with patch(_SEND, return_value={
            "message": {"content": "T1\nT2", "thinking": ""}, "usage": {},
        }) as mock_send:
            motor.scout_digest()
        mock_send.assert_called_once()

    def test_local_scout_untouched(self, tmp_path, monkeypatch):
        motor, _, _ = _make_motor(tmp_path)  # local
        _arm_scout(motor, monkeypatch)
        with patch(_SEND) as mock_send:
            motor.scout_digest()
        mock_send.assert_not_called()  # local never routes to cloud


# ---------------------------------------------------------------------------
# 10. Cloud reasoning response must not poison the LOCAL reasoning cache (F3)
# ---------------------------------------------------------------------------

class TestCloudReasoningCache:
    def test_cloud_response_does_not_poison_local_cache(self, tmp_path):
        motor, _, _ = _make_motor(tmp_path, provider_config=_cloud_config())
        local_tag = motor.current_model
        # First cloud response: empty content + thinking (reasoning-style) would
        # have written _reasoning_model_cache[local_tag] = True ungated.
        responses = [
            {"message": {"content": "", "thinking": "deep thoughts"}, "usage": {}},
            {"message": {"content": "final answer", "thinking": ""}, "usage": {}},
        ]
        with patch(_SEND, side_effect=responses):
            result = motor._generar_dialogo("hola", source="direct", commit_history=False)
        assert result == "final answer"
        assert local_tag not in motor._reasoning_model_cache


# ---------------------------------------------------------------------------
# 11. Cloud success must not mark a LOCAL model as last-known-good (F5)
# ---------------------------------------------------------------------------

class TestCloudSuccessGating:
    def test_cloud_success_does_not_touch_local_rollback_state(self, tmp_path):
        motor, _, _ = _make_motor(tmp_path, provider_config=_cloud_config())
        motor._last_known_good_model = "sentinel-local"
        motor._awaiting_first_success_after_switch = True
        adapted = {"message": {"content": "reply", "thinking": ""}, "usage": {}}
        with patch(_SEND, return_value=adapted):
            result = motor._generar_dialogo("hola", source="direct", commit_history=False)
        assert result == "reply"
        assert motor._last_known_good_model == "sentinel-local"
        assert motor._awaiting_first_success_after_switch is True


# ---------------------------------------------------------------------------
# 12. Cloud download refusal surfaces operator feedback (F6)
# ---------------------------------------------------------------------------

class TestCloudDownloadFeedback:
    def test_cloud_download_worker_emits_refusal_feedback(self, tmp_path):
        motor, log_queue, ui_events = _make_motor(tmp_path, provider_config=_cloud_config())
        motor.ollama.pull = MagicMock()
        motor._download_model_worker("some:model")
        motor.ollama.pull.assert_not_called()
        assert "download_error" in ui_events
        logged = list(log_queue.queue)
        assert any("cloud" in m.lower() for m in logged)


# ---------------------------------------------------------------------------
# 13. Cloud failure degrade + attribution (F7, F8)
# ---------------------------------------------------------------------------

class TestCloudFailureDegrade:
    def test_missing_profile_degrades_cleanly(self, tmp_path):
        # Cloud active but no profile configured for the active provider.
        cfg = {"active_provider": "openai", "pregen_enabled": False, "profiles": {}}
        motor, _, _ = _make_motor(tmp_path, provider_config=cfg)
        with patch(_SEND) as mock_send:
            result = motor._generar_dialogo("hola", source="direct", commit_history=False)
        assert result == ""
        mock_send.assert_not_called()
        # Degrades through the normal cloud-failure contract (transport-classified),
        # not the noisy outer catch-all: a RequestException subclass was recorded.
        assert motor._last_llm_failure is not None
        assert motor._last_llm_failure["reason"] == "CloudLLMResponseError"

    def test_cloud_failure_records_cloud_model_and_provider(self, tmp_path):
        from opencohost.core.providers.cloud.cloud_llm_client import CloudLLMResponseError
        motor, _, _ = _make_motor(tmp_path, provider_config=_cloud_config())
        motor.current_model = "local-tag:7b"  # the LOCAL tag must NOT be recorded
        with patch(_SEND, side_effect=CloudLLMResponseError("boom 401")):
            result = motor._generar_dialogo("hola", source="direct", commit_history=False)
        assert result == ""
        fail = motor._last_llm_failure
        assert fail["model"] == "gpt-cloud"   # cloud profile model, not the local tag
        assert fail["provider"] == "openai"
        assert fail["reason"] == "CloudLLMResponseError"

    def test_local_failure_dict_shape_unchanged(self, tmp_path):
        # Guard: the LOCAL transport-error dict shape stays byte-identical (no
        # "provider" key) so existing exact-match assertions keep passing.
        motor, _, _ = _make_motor(tmp_path)
        motor.current_model = "llama3"
        motor.ollama.chat.side_effect = ConnectionError("ollama refused")
        result = motor._generar_dialogo("hola", source="direct", commit_history=False)
        assert result == ""
        assert motor._last_llm_failure == {
            "model": "llama3",
            "source": "direct",
            "attempt": 1,
            "reason": "ConnectionError",
            "message": "ollama refused",
        }

    def test_cloud_response_error_no_rollback_no_health_change(self, tmp_path):
        from opencohost.core.providers.cloud.cloud_llm_client import CloudLLMResponseError
        motor, _, _ = _make_motor(tmp_path, provider_config=_cloud_config())
        motor._recover_from_stalled_inference = MagicMock()
        motor._last_known_good_model = "sentinel-local"
        with patch(_SEND, side_effect=CloudLLMResponseError("401 unauthorized")):
            result = motor._generar_dialogo("hola", source="direct", commit_history=False)
        assert result == ""
        motor._recover_from_stalled_inference.assert_not_called()
        assert motor._last_known_good_model == "sentinel-local"

    def test_cloud_api_key_never_stored_on_engine(self, tmp_path, monkeypatch):
        SENTINEL = "sk-SENTINEL-DO-NOT-LEAK-123"
        motor, _, _ = _make_motor(tmp_path, provider_config=_cloud_config())
        monkeypatch.setattr(motor, "_cloud_api_key", lambda pid: SENTINEL)
        captured = {}

        def fake_send(**kwargs):
            captured.update(kwargs)
            return {"message": {"content": "ok", "thinking": ""}, "usage": {}}

        with patch(_SEND, side_effect=fake_send):
            motor._generar_dialogo("hola", source="direct", commit_history=False)
        # The key rides ONLY in the send_chat_completion call kwargs...
        assert captured["api_key"] == SENTINEL
        # ...and appears on NO engine attribute.
        assert not _find_sentinel(vars(motor), SENTINEL)


# ---------------------------------------------------------------------------
# 14. Cloud token cap (2026-07-24 cloud incident item 3) — CLOUD_MAX_TOKENS
# ---------------------------------------------------------------------------

class TestCloudTokenCap:
    def test_cloud_dispatch_uses_cloud_max_tokens(self, tmp_path):
        from opencohost.config.settings import CLOUD_MAX_TOKENS
        motor, _, _ = _make_motor(tmp_path, provider_config=_cloud_config())
        captured = {}

        def fake_send(**kwargs):
            captured.update(kwargs)
            return {"message": {"content": "cloud reply", "thinking": ""}, "usage": {}}

        with patch(_SEND, side_effect=fake_send):
            motor._generar_dialogo("hola", source="direct", commit_history=False)

        assert captured["options"]["num_predict"] == CLOUD_MAX_TOKENS

    def test_local_dispatch_keeps_llm_max_tokens(self, tmp_path):
        from opencohost.config.settings import LLM_MAX_TOKENS
        motor, _, _ = _make_motor(
            tmp_path, provider_config={"active_provider": "local", "profiles": {}}
        )
        captured = {}

        def local_chat(**kwargs):
            captured["options"] = kwargs.get("options")
            return {"message": {"content": "local reply", "thinking": ""}}

        motor.ollama.chat.side_effect = local_chat
        motor._generar_dialogo("hola", source="direct", commit_history=False)

        assert captured["options"]["num_predict"] == LLM_MAX_TOKENS

    def test_cloud_reasoning_self_heal_still_omits_cap(self, tmp_path):
        """Layer-2 empty+thinking self-heal is unchanged: on cloud it still
        pops num_predict entirely (uncaps from CLOUD_MAX_TOKENS), never
        reintroducing a cap on the retry."""
        motor, _, _ = _make_motor(tmp_path, provider_config=_cloud_config())
        responses = [
            {"message": {"content": "", "thinking": "deep thoughts"}, "usage": {}},
            {"message": {"content": "final answer", "thinking": ""}, "usage": {}},
        ]
        captured_options = []

        def fake_send(**kwargs):
            captured_options.append(dict(kwargs.get("options") or {}))
            return responses[len(captured_options) - 1]

        with patch(_SEND, side_effect=fake_send):
            result = motor._generar_dialogo("hola", source="direct", commit_history=False)

        assert result == "final answer"
        assert "num_predict" not in captured_options[1]
