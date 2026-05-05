import os
import sys
import tempfile

import yaml

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from stream_admin import AdminManager
from stream_admin.moderation import ModerationEngine
from stream_admin.oauth_store import OAuthStore
from stream_admin.providers import ProviderUnsupportedError


def write_config(path, token_path):
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


def run_tests():
    print("=== RF4 Stream Admin local tests ===")
    with tempfile.TemporaryDirectory() as tmp:
        cfg_path = os.path.join(tmp, "stream_admin.yaml")
        token_path = os.path.join(tmp, "tokens.json")
        write_config(cfg_path, token_path)

        store = OAuthStore(token_path)
        store.save("youtube", {"access_token": "secret", "expires_at": 9999999999})
        assert store.has_token("youtube")
        loaded = store.load("youtube")
        assert loaded["access_token"] == "secret"
        print("[OK] RF4.1 token store local")

        manager = AdminManager(cfg_path)
        logs = []
        manager.on_log = logs.append
        status = manager.status()
        assert status["active_provider"] == "youtube"
        assert status["twitch_placeholder"] is True
        print("[OK] RF4.1/RF4.4 provider status + Twitch placeholder")

        suggestion = manager.suggest_metadata("El chat pidio rankeds")
        assert suggestion["requires_confirmation"] is True
        assert suggestion["payload"]["title"] == "Kira Gaming Live"
        print("[OK] RF4.1 metadata suggestion requires approval")

        try:
            manager.send_chat_message("hola chat")
            raise AssertionError("Chat message should be disabled")
        except Exception as exc:
            assert "desactivados" in str(exc)
        print("[OK] RF4.2/RF4.4 chat writes disabled by default")

        mod = ModerationEngine({"enabled": True, "mode": "alerts_only", "negative_temperature_threshold": 35})
        proposal = mod.evaluate({"temperature": 20, "emotions": {"anger": 0.7}}, {"rate": 2.5})
        assert proposal["action"] == "alert"
        print("[OK] RF4.2 alerts-only moderation")

        high_risk = manager.propose_high_risk_moderation("timeout", "channel123", "spam", 300)
        assert high_risk["requires_confirmation"] is True
        assert high_risk["payload"]["action"] == "timeout"
        print("[OK] RF4.2 timeout/ban high-risk confirmation")

        manager.ingest_rf3_event("vibe", {"temperature": 75, "emotions": {"excitement": 0.8}})
        manager.ingest_rf3_event("activity", {"rate": 1.5})
        context = manager.analytics_context_if_due()
        assert context and "Analiticas stream" in context
        print("[OK] RF4.3 analytics context")

        try:
            manager.providers["twitch"].authenticate()
            raise AssertionError("Twitch should be placeholder")
        except ProviderUnsupportedError:
            pass
        print("[OK] RF4 Twitch placeholder")

    print("=== RF4 local tests passed ===")


if __name__ == "__main__":
    run_tests()
