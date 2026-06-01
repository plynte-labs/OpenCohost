"""Tests for OBS WebSocket client."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from avatar.obs_client import OBSClient, OBSConfig
from avatar.avatar_state import AvatarState, AvatarStateBridge


class TestOBSConfig:
    def test_default_config(self):
        cfg = OBSConfig()
        assert cfg.enabled is False
        assert cfg.host == "localhost"
        assert cfg.port == 4455
        assert cfg.password == ""
        assert cfg.source_name == "KiraAvatar"
        assert cfg.scene_name == ""


class TestOBSClientInit:
    def test_init_with_defaults(self):
        cfg = OBSConfig()
        client = OBSClient(config=cfg, assets_folder=Path("/tmp"))
        assert client.config.enabled is False
        assert client.is_connected is False

    def test_init_with_custom_config(self):
        cfg = OBSConfig(
            enabled=True,
            host="192.168.1.100",
            port=5555,
            password="secret",
            source_name="MyAvatar",
        )
        client = OBSClient(config=cfg, assets_folder=Path("/tmp"))
        assert client.config.host == "192.168.1.100"
        assert client.config.port == 5555
        assert client.config.source_name == "MyAvatar"


class TestOBSClientConnection:
    def test_connect_success(self):
        mock_obs = MagicMock()
        mock_req = MagicMock()
        mock_req.get_version.return_value = {"obs_version": "30.0.0"}
        mock_obs.ReqClient.return_value = mock_req

        with patch.dict("sys.modules", {"obsws_python": mock_obs}):
            # Force reimport to pick up the mock
            import importlib
            import avatar.obs_client
            importlib.reload(avatar.obs_client)

            cfg = OBSConfig(enabled=True, host="localhost", port=4455)
            client = OBSClient(config=cfg, assets_folder=Path("/tmp"))
            result = client.connect()

            assert result is True
            assert client.is_connected is True

    def test_connect_disabled(self):
        cfg = OBSConfig(enabled=False)
        client = OBSClient(config=cfg, assets_folder=Path("/tmp"))
        result = client.connect()
        assert result is False
        assert client.is_connected is False

    def test_connect_refused(self):
        cfg = OBSConfig(enabled=True, host="localhost", port=99999)
        client = OBSClient(config=cfg, assets_folder=Path("/tmp"))
        result = client.connect()
        assert result is False

    def test_connect_refused_can_be_silent_for_retry_loop(self):
        logs = []
        cfg = OBSConfig(enabled=True, host="localhost", port=99999)
        client = OBSClient(config=cfg, assets_folder=Path("/tmp"), on_log=logs.append)

        result = client.connect(log_failures=False)

        assert result is False
        assert logs == []


class TestOBSClientBridge:
    def test_subscribe_bridge(self):
        cfg = OBSConfig()
        client = OBSClient(config=cfg, assets_folder=Path("/tmp"))
        bridge = AvatarStateBridge()

        client.subscribe_bridge(bridge)
        assert client._bridge_sub_id is not None

        client.unsubscribe_bridge()
        assert client._bridge_sub_id is None

    def test_state_change_without_connection(self):
        """State changes should be no-ops when not connected."""
        cfg = OBSConfig(enabled=False)
        client = OBSClient(config=cfg, assets_folder=Path("/tmp"))
        # Should not raise
        client.on_state_change(AvatarState.SPEAKING)


class TestOBSClientImageResolution:
    def test_uses_configured_state_image_path_first(self, tmp_path):
        configured = tmp_path / "custom-speaking.webp"
        configured.write_bytes(b"fake webp")
        fallback = tmp_path / "speaking.png"
        fallback.write_bytes(b"fake png")

        client = OBSClient(
            config=OBSConfig(),
            assets_folder=tmp_path,
            state_images={"speaking": configured},
        )

        assert client._get_image_path_for_state("speaking") == configured

    def test_finds_supported_extensions_in_assets_folder(self, tmp_path):
        image = tmp_path / "listening.jpg"
        image.write_bytes(b"fake jpg")
        client = OBSClient(config=OBSConfig(), assets_folder=tmp_path)

        assert client._get_image_path_for_state("listening") == image

    def test_fallback_to_configured_idle_image(self, tmp_path):
        idle = tmp_path / "custom-idle.jpeg"
        idle.write_bytes(b"fake jpeg")
        client = OBSClient(
            config=OBSConfig(),
            assets_folder=tmp_path,
            state_images={"idle": idle},
        )

        assert client._get_image_path_for_state("speaking") == idle

    def test_update_preserves_file_and_local_file_keys(self, tmp_path):
        image = tmp_path / "speaking.webp"
        image.write_bytes(b"fake webp")
        client = OBSClient(config=OBSConfig(source_name="Kira"), assets_folder=tmp_path)
        client._connected = True
        client._client = MagicMock()
        client._client.get_input_settings.return_value = MagicMock(
            input_settings={"unload": False, "linear_alpha": True}
        )

        client._update_obs_image(AvatarState.SPEAKING)

        client._client.set_input_settings.assert_called_once()
        settings = client._client.set_input_settings.call_args.kwargs["settings"]
        assert settings["file"] == str(image)
        assert settings["local_file"] == str(image)
        assert settings["unload"] is False


class TestOBSClientDisconnect:
    def test_disconnect_without_connect(self):
        cfg = OBSConfig(enabled=True)
        client = OBSClient(config=cfg, assets_folder=Path("/tmp"))
        # Should not raise
        client.disconnect()
        assert client.is_connected is False
