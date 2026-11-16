"""Migrated tests from stream_admin/test_local.py.

Preserves all 8 test cases:
  RF4.1 — Token store, provider status, metadata suggestion, read-only blocks
  RF4.2 — Moderation alerts, high-risk confirmation
  RF4.3 — Analytics context
  RF4.4 — Chat writes disabled, Twitch placeholder
"""

import json
import os

import pytest
import yaml

from opencohost.stream_admin import AdminManager
from opencohost.stream_admin.moderation import ModerationEngine
from opencohost.stream_admin.oauth_store import OAuthStore, restrict_file_permissions
from opencohost.stream_admin.providers import ProviderAuthError, ProviderUnsupportedError


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

    def test_token_store_save_is_atomic_no_leftover_tmp_file(self, temp_dir):
        """F4: save() must write via temp-file+os.replace, never leaving a
        torn/leftover .tmp file in the token directory, and the final file
        must always be valid, complete JSON (never truncated)."""
        token_path = os.path.join(temp_dir, "tokens.json")
        store = OAuthStore(token_path)
        store.save("youtube", {"access_token": "secret", "expires_at": 9999999999})

        assert os.listdir(temp_dir) == ["tokens.json"]
        with open(token_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        assert data["youtube"]["access_token"] == "secret"

    @pytest.mark.skipif(os.name != "nt", reason="Windows NTFS ACL delete semantics")
    def test_save_over_owner_restricted_target_in_modify_only_dir(self, tmp_path):
        """Regression: on Windows a prior save restricts the token file to owner
        (R,W) -- no DELETE. In a config dir that grants only Modify (no
        FILE_DELETE_CHILD), os.replace() cannot delete that target, so EVERY
        save/delete after the first raised PermissionError -> API 503
        key_store_write_failed. The store must self-heal and persist.

        Deterministic: the dir ACL is pinned to Modify-only and the file is
        forced into the exact owner-(R,W) state the owner's live file is in,
        so this fails on the pre-fix code and passes on the fixed code.
        """
        import subprocess

        username = os.environ.get("USERNAME", "")
        if not username:
            pytest.skip("no USERNAME in environment")

        cfg = tmp_path / "cfg"
        cfg.mkdir()
        # Mimic the real config dir: owner has Modify only (no delete-child).
        subprocess.run(
            ["icacls", str(cfg), "/inheritance:r", "/grant:r", f"{username}:(OI)(CI)(M)"],
            capture_output=True,
        )
        token_path = str(cfg / "llm_keys.json")
        store = OAuthStore(token_path)
        store.save("p1", {"api_key": "K1"})

        # Force yesterday's build's exact output: owner-only (R,W), no DELETE.
        subprocess.run(
            ["icacls", token_path, "/inheritance:r", "/grant:r", f"{username}:(R,W)"],
            capture_output=True,
        )

        # Pre-fix this raised PermissionError -> 503 on every save.
        store.save("p2", {"api_key": "K2"})
        assert store.load("p1")["api_key"] == "K1"
        assert store.load("p2")["api_key"] == "K2"

        # delete rewrites via the same _write_all path and must also succeed.
        store.delete("p1")
        assert store.load("p1") is None
        assert store.load("p2")["api_key"] == "K2"


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


class TestOAuthClientPermissions:
    """Cross-platform permission hardening for stream admin credentials."""

    def test_save_oauth_client_config_creates_file_and_calls_restrict(self, tmp_path, monkeypatch):
        cfg_path = tmp_path / "stream_admin.yaml"
        token_path = tmp_path / "tokens.json"
        client_path = tmp_path / "oauth_client.json"
        write_config(str(cfg_path), str(token_path))

        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        cfg["oauth"]["client_config_path"] = str(client_path)
        with open(cfg_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(cfg, f)

        manager = AdminManager(str(cfg_path))
        restricted_paths = []
        monkeypatch.setattr(
            "opencohost.stream_admin.admin_manager.restrict_file_permissions",
            lambda p: restricted_paths.append(str(p)) or True,
        )

        manager.save_oauth_client_config("my_id", "my_secret")

        assert client_path.exists()
        with open(client_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        assert data == {"client_id": "my_id", "client_secret": "my_secret"}
        assert restricted_paths == [str(client_path)]
        assert manager.config["youtube"]["client_id"] == "my_id"
        assert manager.config["youtube"]["client_secret"] == "my_secret"

    def test_restrict_file_permissions_windows_calls_icacls(self, tmp_path, monkeypatch):
        test_file = tmp_path / "test.json"
        test_file.write_text("{}", encoding="utf-8")

        calls = []
        monkeypatch.setattr(os, "name", "nt")
        monkeypatch.setenv("USERNAME", "testuser")

        import subprocess
        def fake_run(cmd, *args, **kwargs):
            if cmd and cmd[0] == "whoami":
                return subprocess.CompletedProcess(cmd, 1, stdout=b"", stderr=b"")
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")

        monkeypatch.setattr(subprocess, "run", fake_run)
        success = restrict_file_permissions(str(test_file))

        assert success is True
        assert len(calls) == 1
        assert calls[0] == [
            "icacls",
            str(test_file),
            "/inheritance:r",
            "/grant:r",
            "testuser:(R,W,D)",
        ]

    def test_restrict_file_permissions_windows_nonzero_returncode_returns_false_and_warns(
        self, tmp_path, monkeypatch, caplog
    ):
        test_file = tmp_path / "secret.json"
        test_file.write_text("{}", encoding="utf-8")
        monkeypatch.setattr(os, "name", "nt")
        monkeypatch.setenv("USERNAME", "testuser")

        import subprocess

        def fake_run(cmd, *args, **kwargs):
            if cmd and cmd[0] == "whoami":
                return subprocess.CompletedProcess(cmd, 1, stdout=b"", stderr=b"")
            return subprocess.CompletedProcess(cmd, 5, stdout=b"", stderr=b"Access is denied. SECRET-STDERR")

        monkeypatch.setattr(subprocess, "run", fake_run)
        with caplog.at_level("WARNING"):
            assert restrict_file_permissions(str(test_file)) is False
        assert any("icacls" in r.getMessage() for r in caplog.records)
        assert all("SECRET-STDERR" not in r.getMessage() for r in caplog.records)

    def test_restrict_file_permissions_windows_exception_returns_false_and_warns(
        self, tmp_path, monkeypatch, caplog
    ):
        test_file = tmp_path / "secret.json"
        test_file.write_text("{}", encoding="utf-8")
        monkeypatch.setattr(os, "name", "nt")
        monkeypatch.setenv("USERNAME", "testuser")

        import subprocess

        def boom(cmd, *args, **kwargs):
            raise OSError("icacls missing")

        monkeypatch.setattr(subprocess, "run", boom)
        with caplog.at_level("WARNING"):
            assert restrict_file_permissions(str(test_file)) is False
        assert caplog.records

    def test_restrict_file_permissions_windows_prefers_sid_when_available(self, tmp_path, monkeypatch):
        test_file = tmp_path / "secret.json"
        test_file.write_text("{}", encoding="utf-8")
        monkeypatch.setattr(os, "name", "nt")
        monkeypatch.setenv("USERNAME", "testuser")

        import subprocess

        calls = []

        def fake_run(cmd, *args, **kwargs):
            calls.append(cmd)
            if cmd and cmd[0] == "whoami":
                return subprocess.CompletedProcess(
                    cmd, 0, stdout=b'"host\\testuser","S-1-5-21-1-2-3-1001"\r\n', stderr=b""
                )
            return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")

        monkeypatch.setattr(subprocess, "run", fake_run)
        assert restrict_file_permissions(str(test_file)) is True
        assert calls[-1][-1] == "*S-1-5-21-1-2-3-1001:(R,W,D)"

    def test_restrict_file_permissions_posix_calls_chmod(self, tmp_path, monkeypatch):
        test_file = tmp_path / "test.json"
        test_file.write_text("{}", encoding="utf-8")

        chmod_calls = []
        monkeypatch.setattr(os, "name", "posix")
        monkeypatch.setattr(os, "chmod", lambda p, mode: chmod_calls.append((str(p), mode)))

        success = restrict_file_permissions(str(test_file))

        assert success is True
        assert chmod_calls == [(str(test_file), 0o600)]

    @pytest.mark.skipif(os.name != "nt", reason="Windows NTFS live ACL test")
    def test_save_oauth_client_config_real_windows_acl(self, tmp_path):
        username = os.environ.get("USERNAME", "")
        if not username:
            pytest.skip("no USERNAME in environment")

        cfg_path = tmp_path / "stream_admin.yaml"
        token_path = tmp_path / "tokens.json"
        client_path = tmp_path / "oauth_client.json"
        write_config(str(cfg_path), str(token_path))

        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        cfg["oauth"]["client_config_path"] = str(client_path)
        with open(cfg_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(cfg, f)

        manager = AdminManager(str(cfg_path))
        manager.save_oauth_client_config("real_client_id", "real_client_secret")

        assert client_path.exists()
        import subprocess
        result = subprocess.run(
            ["icacls", str(client_path)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert username.lower() in result.stdout.lower()

