"""Tests for Cloud -> Local provider reconciliation and model switching (Strict TDD).

Covers:
1. Cloud to Local transition reconciles is_ready=True when Ollama is responsive.
2. Cloud to Local transition stays not ready when Ollama is unreachable.
3. switch_model reprobes Ollama when is_ready is False and Ollama is alive.
4. switch_model fails cleanly without corrupting desired model when Ollama is dead.
5. switch_llm_tier does not mutate active tier or model when engine is not ready.
6. switch_llm_tier succeeds and mutates model when prepared and ready.
7. Late local probe after switching back to Cloud does not overwrite Cloud state.
8. Rapid model switching (e2b -> e4b) applies target model cleanly.
9. switch_model during speech defers and applies cleanly at speech boundary.
"""

import os
import queue
import time
from unittest.mock import MagicMock, patch
import pytest

from opencohost.config import settings
from opencohost.core.llm_engine import MotorVocalIA
from opencohost.core.providers.llm_tiers import LLMTierConfig


def _cloud_config():
    return {
        "active_provider": "openai",
        "fallback_mode": "auto",
        "pregen_enabled": False,
        "profiles": {
            "openai": {
                "base_url": "https://api.example.com/v1",
                "model": "gpt-4o",
            },
        },
    }


def _local_config():
    return {
        "active_provider": "local",
        "fallback_mode": "auto",
        "pregen_enabled": False,
        "profiles": {},
    }


def _make_motor(tmp_path, *, provider_config=None):
    """Construct a MotorVocalIA with mocked I/O and isolated LAST_MODEL_FILE."""
    original_last_model = settings.LAST_MODEL_FILE
    settings.LAST_MODEL_FILE = os.path.join(str(tmp_path), "last_model.json")

    log_queue = queue.Queue()
    ui_events = []

    try:
        motor = MotorVocalIA(log_queue, ui_events.append)
    finally:
        settings.LAST_MODEL_FILE = original_last_model

    motor.ollama = MagicMock()
    motor.ollama.list.return_value = {
        "models": [{"name": "gemma4:e2b"}, {"name": "gemma4:e4b"}]
    }
    motor.pygame = MagicMock()
    motor.is_ready = True
    motor.current_model = "gemma4:e2b"
    motor._desired_model = "gemma4:e2b"
    motor._loaded_model = "gemma4:e2b"
    motor._provider_config = (
        provider_config if provider_config is not None else _local_config()
    )
    return motor, log_queue, ui_events


class TestProviderModelSwitchReconciliation:
    """Strict TDD tests for Cloud -> Local and model/tier switching reconciliation."""

    def test_cloud_to_local_reconciles_is_ready_true_when_ollama_alive(self, tmp_path):
        """1. When switching provider from Cloud to Local, if Ollama is responsive,
        self.is_ready must become True (reconciled/probed)."""
        motor, _, ui_events = _make_motor(tmp_path, provider_config=_cloud_config())
        motor.is_ready = True
        assert not motor._is_local

        # Ollama responds to list()
        motor.ollama.list.return_value = {
            "models": [{"name": "gemma4:e2b"}, {"name": "gemma4:e4b"}]
        }

        with (
            patch.object(motor, "_prepare_model", return_value=True),
            patch("opencohost.core.llm_engine.save_last_model"),
        ):
            motor.set_provider_config(_local_config())

        assert motor._is_local is True
        assert motor.is_ready is True

    def test_cloud_to_local_stays_not_ready_when_ollama_dead(self, tmp_path):
        """2. When switching from Cloud to Local, if Ollama raises Exception on list(),
        self.is_ready remains False, without reporting false success."""
        motor, _, ui_events = _make_motor(tmp_path, provider_config=_cloud_config())
        motor.is_ready = True
        assert not motor._is_local

        motor.ollama.list.side_effect = Exception("Connection refused: Ollama daemon down")

        with patch("opencohost.core.llm_engine.save_last_model"):
            motor.set_provider_config(_local_config())

        assert motor._is_local is True
        assert motor.is_ready is False
        assert "model_switch_applied" not in ui_events

    def test_switch_model_reprobes_when_is_ready_false_and_ollama_alive(self, tmp_path):
        """3. If self.is_ready was False, but Ollama is responsive when
        _dispatch_command('switch_model', 'gemma4:e4b') arrives, the engine
        must probe, succeed, prepare the model, set current_model='gemma4:e4b',
        and emit model_switch_applied."""
        motor, _, ui_events = _make_motor(tmp_path, provider_config=_local_config())
        motor.is_ready = False
        motor.current_model = "gemma4:e2b"
        motor._desired_model = "gemma4:e2b"

        motor.ollama.list.return_value = {
            "models": [{"name": "gemma4:e2b"}, {"name": "gemma4:e4b"}]
        }

        with (
            patch.object(motor, "_prepare_model", return_value=True),
            patch("opencohost.core.llm_engine.save_last_model"),
        ):
            motor._dispatch_command("switch_model", "gemma4:e4b")

        assert motor.is_ready is True
        assert motor.current_model == "gemma4:e4b"
        assert motor._desired_model == "gemma4:e4b"
        assert "model_switch_applied" in ui_events
        assert "model_switch_failed" not in ui_events

    def test_switch_model_fails_cleanly_when_ollama_dead(self, tmp_path):
        """4. If Ollama is unreachable, switch_model fails, current_model and
        _desired_model remain unchanged, and model_switch_failed is emitted."""
        motor, _, ui_events = _make_motor(tmp_path, provider_config=_local_config())
        motor.is_ready = False
        motor.current_model = "gemma4:e2b"
        motor._desired_model = "gemma4:e2b"

        motor.ollama.list.side_effect = Exception("Ollama unreachable")

        motor._dispatch_command("switch_model", "gemma4:e4b")

        assert motor.current_model == "gemma4:e2b"
        assert motor._desired_model == "gemma4:e2b"
        assert motor.is_ready is False
        assert "model_switch_failed" in ui_events
        assert "model_switch_applied" not in ui_events

    def test_switch_llm_tier_does_not_mutate_model_when_not_ready(self, tmp_path):
        """5. When is_ready is False or model preparation fails, switch_llm_tier('quality')
        must NOT mutate current_model or active_tier, must NOT emit llm_tier_switch_applied,
        and must emit llm_tier_switch_failed."""
        motor, _, ui_events = _make_motor(tmp_path, provider_config=_local_config())
        tier_cfg = LLMTierConfig(quality="gemma4:e4b", balanced="gemma4:e2b", fast="gemma4:e2b")
        motor.configure_llm_tiers(tier_cfg, active_tier="fast")
        motor.current_model = "gemma4:e2b"
        motor._desired_model = "gemma4:e2b"
        motor.is_ready = False

        motor.ollama.list.side_effect = Exception("Ollama offline")

        success = motor.switch_llm_tier("quality")

        assert success is False
        assert motor.active_llm_tier == "fast"
        assert motor.current_model == "gemma4:e2b"
        assert motor._desired_model == "gemma4:e2b"
        assert "llm_tier_switch_failed" in ui_events
        assert "llm_tier_switch_applied" not in ui_events

    def test_switch_llm_tier_succeeds_and_mutates_model_when_prepared(self, tmp_path):
        """6. When Ollama is ready and _prepare_model succeeds, switch_llm_tier('quality')
        updates active_tier and current_model, and emits llm_tier_switch_applied."""
        motor, _, ui_events = _make_motor(tmp_path, provider_config=_local_config())
        tier_cfg = LLMTierConfig(quality="gemma4:e4b", balanced="gemma4:e2b", fast="gemma4:e2b")
        motor.configure_llm_tiers(tier_cfg, active_tier="fast")
        motor.current_model = "gemma4:e2b"
        motor._desired_model = "gemma4:e2b"
        motor.is_ready = True

        with (
            patch.object(motor, "_prepare_model", return_value=True),
            patch("opencohost.core.llm_engine.save_last_model"),
        ):
            success = motor.switch_llm_tier("quality")

        assert success is True
        assert motor.active_llm_tier == "quality"
        assert motor.current_model == "gemma4:e4b"
        assert motor._desired_model == "gemma4:e4b"
        assert "llm_tier_switch_applied" in ui_events
        assert "llm_tier_switch_failed" not in ui_events

    def test_provider_race_late_local_probe_does_not_override_cloud_state(self, tmp_path):
        """7. If provider switches to Local (triggering a probe), but before the
        probe completes the provider switches back to Cloud, the late local probe
        must NOT set Local/ready state or downgrade Cloud ready state."""
        motor, _, ui_events = _make_motor(tmp_path, provider_config=_cloud_config())
        motor.is_ready = True

        # Switch to Local
        motor.set_provider_config(_local_config())
        epoch_during_local = motor.provider_epoch

        # Switch back to Cloud before any deferred/background probe finishes
        motor.set_provider_config(_cloud_config())
        assert motor.provider_epoch > epoch_during_local
        assert not motor._is_local

        # Late local probe result arrives (simulating callback/probe execution)
        # Even if local check had failed or succeeded under the old epoch,
        # cloud state must remain intact
        if hasattr(motor, "_on_local_probe_result"):
            motor._on_local_probe_result(success=False, epoch=epoch_during_local)
        else:
            # If probed via _check_ollama_service, verify it doesn't overwrite if provider is cloud
            motor.ollama.list.side_effect = Exception("Ollama dead")
            motor._check_ollama_service(notify_unavailable=False)

        assert not motor._is_local
        assert motor.is_ready is True
        assert motor._provider_config["active_provider"] == "openai"

    def test_rapid_model_switching_e2b_then_e4b(self, tmp_path):
        """8. Rapidly dispatching switch_model('gemma4:e2b') then switch_model('gemma4:e4b')
        cleanly applies the target models without corruption."""
        motor, _, ui_events = _make_motor(tmp_path, provider_config=_local_config())
        motor.is_ready = True
        motor.current_model = "gemma4:old"
        motor._desired_model = "gemma4:old"

        with (
            patch.object(motor, "_prepare_model", return_value=True),
            patch("opencohost.core.llm_engine.save_last_model"),
        ):
            motor._dispatch_command("switch_model", "gemma4:e2b")
            motor._dispatch_command("switch_model", "gemma4:e4b")

        assert motor.current_model == "gemma4:e4b"
        assert motor._desired_model == "gemma4:e4b"
        assert ui_events.count("model_switch_applied") >= 1

    def test_switch_model_during_speech_defers_and_applies_at_boundary(self, tmp_path):
        """9. switch_model while _speech_active=True defers and applies once speech clears."""
        motor, _, ui_events = _make_motor(tmp_path, provider_config=_local_config())
        motor.is_ready = True
        motor.current_model = "gemma4:e2b"
        motor._desired_model = "gemma4:e2b"
        motor._speaking = True
        assert motor._speech_active is True

        with (
            patch.object(motor, "_prepare_model", return_value=True),
            patch("opencohost.core.llm_engine.save_last_model"),
        ):
            motor._dispatch_command("switch_model", "gemma4:e4b")

            # Deferred during speech
            assert motor.current_model == "gemma4:e2b"
            assert motor._pending_model_switch == "gemma4:e4b"
            assert "model_switch_pending" in ui_events
            assert "model_switch_applied" not in ui_events

            # Speech ends -> check pending switch
            motor._speaking = False
            assert motor._speech_active is False
            motor._check_pending_model_switch()

            # Applied at boundary
            assert motor.current_model == "gemma4:e4b"
            assert motor._pending_model_switch is None
            assert "model_switch_applied" in ui_events

    def test_local_to_local_provider_put_preserves_ready_state(self, tmp_path):
        """10. A PUT config when already in local provider must preserve is_ready=True
        and not reset readiness or run spurious reconciliation."""
        motor, _, _ = _make_motor(tmp_path, provider_config=_local_config())
        motor.is_ready = True
        assert motor._is_local is True

        new_local_cfg = _local_config()
        new_local_cfg["pregen_enabled"] = True

        motor.ollama.list.side_effect = AssertionError("Should not probe on local->local PUT")
        motor.set_provider_config(new_local_cfg)

        assert motor._is_local is True
        assert motor.is_ready is True

    def test_late_failed_local_probe_cannot_override_new_cloud_ready_state(self, tmp_path):
        """11. When switching Cloud -> Local, a slow local probe that fails late must NOT
        mutate is_ready=False if the user has already switched back to Cloud."""
        motor, _, _ = _make_motor(tmp_path, provider_config=_cloud_config())
        motor.is_ready = True

        # Simulate probe that blocks and user switches back to Cloud mid-probe
        def slow_failing_probe():
            motor.set_provider_config(_cloud_config())
            return False

        with patch.object(motor, "_probe_ollama_service", side_effect=slow_failing_probe):
            motor.set_provider_config(_local_config())

        assert not motor._is_local
        assert motor.is_ready is True
        assert motor._provider_config["active_provider"] == "openai"

    def test_late_successful_local_probe_cannot_warm_or_mutate_after_cloud_switch(self, tmp_path):
        """12. When switching Cloud -> Local, a slow local probe that succeeds late must NOT
        trigger model preparation, warming, or mutate is_ready if user is now on Cloud."""
        motor, _, _ = _make_motor(tmp_path, provider_config=_cloud_config())
        motor.is_ready = True
        motor.current_model = "gemma4:e2b"

        prepare_mock = MagicMock(return_value=True)
        motor._prepare_model = prepare_mock

        def slow_successful_probe():
            motor.set_provider_config(_cloud_config())
            return True

        with patch.object(motor, "_probe_ollama_service", side_effect=slow_successful_probe):
            motor.set_provider_config(_local_config())

        assert not motor._is_local
        assert motor.is_ready is True
        prepare_mock.assert_not_called()

    def test_unavailable_tier_returns_before_prepare_or_switch(self, tmp_path):
        """13. When target_model is None (e.g. unknown tier), switch_llm_tier must immediately
        emit llm_tier_switch_failed, must NOT call _prepare_model or _probe_ollama_service,
        and must return False."""
        motor, _, ui_events = _make_motor(tmp_path, provider_config=_local_config())
        tier_cfg = LLMTierConfig(quality="gemma4:e4b", balanced="gemma4:e2b", fast="gemma4:e2b")
        motor.configure_llm_tiers(tier_cfg, active_tier="fast")
        motor.is_ready = True
        motor.current_model = "gemma4:e2b"

        probe_mock = MagicMock(return_value=True)
        prepare_mock = MagicMock(return_value=True)
        motor._probe_ollama_service = probe_mock
        motor._prepare_model = prepare_mock

        success = motor.switch_llm_tier("nonexistent_tier")

        assert success is False
        assert motor.active_llm_tier == "fast"
        assert motor.current_model == "gemma4:e2b"
        assert "llm_tier_switch_failed" in ui_events
        probe_mock.assert_not_called()
        prepare_mock.assert_not_called()

    def test_probe_ollama_service_uses_bounded_timeout_client(self, tmp_path):
        """14. _probe_ollama_service uses a bounded timeout client factory when available."""
        import types
        motor, _, _ = _make_motor(tmp_path, provider_config=_local_config())
        fake_module = types.ModuleType("ollama")
        client_instance = MagicMock()
        fake_module.Client = MagicMock(return_value=client_instance)
        motor.ollama = fake_module

        success = motor._probe_ollama_service(timeout=3.0)

        assert success is True
        fake_module.Client.assert_called_once_with(timeout=3.0)
        client_instance.list.assert_called_once()

    def test_command_reprobe_discards_stale_epoch_result(self, tmp_path):
        """15. When switch_model triggers a local reprobe, if the provider changes
        concurrently before probe completes, the probe result from the old epoch is discarded."""
        motor, _, ui_events = _make_motor(tmp_path, provider_config=_local_config())
        motor.is_ready = False
        motor.current_model = "gemma4:e2b"
        motor._desired_model = "gemma4:e2b"

        def slow_probe():
            # Mid-probe, provider transitions to cloud
            motor.set_provider_config(_cloud_config())
            return True

        with (
            patch.object(motor, "_probe_ollama_service", side_effect=slow_probe),
            patch.object(motor, "_prepare_model", return_value=True),
        ):
            motor._dispatch_command("switch_model", "gemma4:e4b")

        # Provider is cloud and was ready under cloud
        assert not motor._is_local
        assert motor.is_ready is True
