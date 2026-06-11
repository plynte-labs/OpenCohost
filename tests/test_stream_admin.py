"""Migrated tests from stream_admin/test_local.py.

Preserves all 8 test cases:
  RF4.1 — Token store, provider status, metadata suggestion, read-only blocks
  RF4.2 — Moderation alerts, high-risk confirmation
  RF4.3 — Analytics context
  RF4.4 — Chat writes disabled, Twitch placeholder
"""

import os

import pytest
import yaml

from opencohost.stream_admin import AdminManager
from opencohost.stream_admin.moderation import ModerationEngine
from opencohost.stream_admin.oauth_store import OAuthStore
from opencohost.stream_admin.providers import ProviderUnsupportedError


def write_config(path, token_path):
    """Write a test stream_admin config to the given path."""
    cfg = {
        "provider": {"default": "youtube", "mode": "read_only"},
        "oauth": {"token_path": token_path, "request_write_scopes": False},
        "youtube": {"client_id": "", "client_secret": ""},
        "twitch": {"enabled": False},
        "metadata": {
            "require_approval": True,
            "presets": [{"name": "Gaming", "title": "Kira Gaming Live", "description": "Jugando con el chat", "tags": ["kira", "gaming"]}],
            "categories": [{"id": "20", "name": "Gaming"}],
        },
        "moderation": {"enabled": True, "mode": "alerts_only", "negative_temperature_threshold": 35, "chat_rate_threshold": 1.0},
        "analytics": {"enabled": True, "inject_context": True, "update_interval_seconds": 0},
        "chat": {"allow_kira_chat_messages": False},
    }
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(cfg, f)


class TestOAuthStore:
    """RF4.1: OAuth token store."""

    def test_token_store_save_and_load(self, temp_dir):
        """RF4.1: Token store save/load cycle."""
        token_path = os.path.join(temp_dir, "tokens.json")
        store = OAuthStore(token_path)
        store.save("youtube", {"access_token": "secret", "expires_at": 9999999999})
        assert store.has_token("youtube")
        loaded = store.load("youtube")
        assert loaded["access_token"] == "secret"


class TestAdminManager:
    """RF4: AdminManager integration tests."""

    @pytest.fixture
    def admin_setup(self, temp_dir):
        """Create a configured AdminManager and token store."""
        cfg_path = os.path.join(temp_dir, "stream_admin.yaml")
        token_path = os.path.join(temp_dir, "tokens.json")
        write_config(cfg_path, token_path)

        store = OAuthStore(token_path)
        store.save("youtube", {"access_token": "secret", "expires_at": 9999999999})

        manager = AdminManager(cfg_path)
        logs = []
        manager.on_log = logs.append
        return manager, store, logs

    def test_provider_status_and_twitch_placeholder(self, admin_setup):
        """RF4.1/RF4.4: Provider status and Twitch placeholder."""
        manager, _, _ = admin_setup
        status = manager.status()
        assert status["active_provider"] == "youtube"
        assert status["twitch_placeholder"] is True

    def test_metadata_suggestion_requires_approval(self, admin_setup):
        """RF4.1: Metadata suggestion must require confirmation."""
        manager, _, _ = admin_setup
        suggestion = manager.suggest_metadata("El chat pidio rankeds")
        assert suggestion["requires_confirmation"] is True
        assert suggestion["payload"]["title"] == "Kira Gaming Live"

    def test_read_only_blocks_writes(self, admin_setup):
        """RF4.1: Read-only mode must block metadata writes."""
        manager, _, _ = admin_setup
        manager.suggest_metadata("El chat pidio rankeds")
        with pytest.raises(Exception) as exc_info:
            manager.apply_pending_action(force=True)
        assert "solo lectura" in str(exc_info.value)

    def test_chat_writes_disabled(self, admin_setup):
        """RF4.2/RF4.4: Chat writes must be disabled by default."""
        manager, _, _ = admin_setup
        with pytest.raises(Exception) as exc_info:
            manager.send_chat_message("hola chat")
        assert "desactivados" in str(exc_info.value)

    def test_twitch_placeholder(self, admin_setup):
        """RF4: Twitch provider must raise ProviderUnsupportedError."""
        manager, _, _ = admin_setup
        with pytest.raises(ProviderUnsupportedError):
            manager.providers["twitch"].authenticate()


class TestModerationEngine:
    """RF4.2: Moderation engine tests."""

    def test_alerts_only_moderation(self):
        """RF4.2: Moderation should propose alert for negative conditions."""
        mod = ModerationEngine({
            "enabled": True,
            "mode": "alerts_only",
            "negative_temperature_threshold": 35,
        })
        proposal = mod.evaluate(
            {"temperature": 20, "emotions": {"anger": 0.7}},
            {"rate": 2.5},
        )
        assert proposal["action"] == "alert"

    def test_high_risk_confirmation(self, temp_dir):
        """RF4.2: Timeout/ban actions must require confirmation."""
        cfg_path = os.path.join(temp_dir, "stream_admin.yaml")
        token_path = os.path.join(temp_dir, "tokens.json")
        write_config(cfg_path, token_path)
        manager = AdminManager(cfg_path)
        high_risk = manager.propose_high_risk_moderation("timeout", "channel123", "spam", 300)
        assert high_risk["requires_confirmation"] is True
        assert high_risk["payload"]["action"] == "timeout"


class TestAnalytics:
    """RF4.3: Analytics context injection."""

    def test_analytics_context(self, temp_dir):
        """RF4.3: Analytics context must be available when due."""
        cfg_path = os.path.join(temp_dir, "stream_admin.yaml")
        token_path = os.path.join(temp_dir, "tokens.json")
        write_config(cfg_path, token_path)
        manager = AdminManager(cfg_path)
        manager.ingest_rf3_event("vibe", {"temperature": 75, "emotions": {"excitement": 0.8}})
        manager.ingest_rf3_event("activity", {"rate": 1.5})
        context = manager.analytics_context_if_due()
        assert context is not None
        assert "Analiticas stream" in context
