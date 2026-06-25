"""OBS WebSocket client for OpenCohost avatar integration.

Connects to OBS Studio via obs-websocket and updates image sources
based on avatar state changes.

Usage::

    client = OBSClient(host="localhost", port=4455, password="secret")
    client.connect()
    sub_id = bridge.subscribe(client.on_state_change)
    client.set_source_name("KiraAvatar")  # name of Image source in OBS
    client.disconnect()
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from opencohost.avatar.avatar_state import AvatarState, AvatarStateBridge
from opencohost.config.logger import get_logger

logger = get_logger()

_SUPPORTED_IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp")


@dataclass
class OBSConfig:
    """OBS WebSocket connection configuration."""
    enabled: bool = False
    host: str = "localhost"
    port: int = 4455
    password: str = ""
    source_name: str = "KiraAvatar"  # Image source name in OBS
    scene_name: str = ""  # Optional: specific scene to target


class OBSClient:
    """Manages OBS WebSocket connection and avatar image updates.

    This client subscribes to AvatarStateBridge and pushes image
    source updates to OBS whenever the avatar state changes.
    """

    def __init__(
        self,
        config: OBSConfig,
        assets_folder: Path,
        state_images: Optional[dict[str, Path]] = None,
        on_log: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.config = config
        self.assets_folder = assets_folder
        self.state_images = state_images or {}
        self.on_log = on_log or (lambda m: None)

        self._client = None
        self._connected = False
        self._lock = threading.Lock()
        self._bridge_sub_id: Optional[int] = None
        self._state_bridge: Optional[AvatarStateBridge] = None

    # -- connection --------------------------------------------------------

    def connect(self, *, log_failures: bool = True) -> bool:
        """Connect to OBS WebSocket. Returns True on success."""
        if not self.config.enabled:
            self.on_log("[OBS] OBS integration is disabled in config.")
            return False

        if self._connected:
            return True

        try:
            import obsws_python as obs
            self._client = obs.ReqClient(
                host=self.config.host,
                port=self.config.port,
                password=self.config.password or None,
            )
            # Test connection
            version = self._client.get_version()
            self._connected = True
            obs_ver = getattr(version, 'obs_version', '?') or '?'
            self.on_log(f"[OBS] Connected to OBS {obs_ver}")
            logger.info("OBS WebSocket connected: %s:%d", self.config.host, self.config.port)
            return True
        except ImportError as e:
            if log_failures:
                self.on_log(f"[OBS] obsws-python not installed. Run: pip install obsws-python ({e})")
                logger.error("obsws-python not installed: %s", e)
            return False
        except ConnectionRefusedError:
            if log_failures:
                self.on_log(f"[OBS] Connection refused. Is OBS running with WebSocket on {self.config.host}:{self.config.port}?")
                logger.info("OBS WebSocket not ready yet: %s:%d", self.config.host, self.config.port)
            return False
        except Exception as e:
            if log_failures:
                self.on_log(f"[OBS] Connection error: {type(e).__name__}: {e}")
                logger.warning("OBS WebSocket not ready or returned an error: %s: %s", type(e).__name__, e)
            return False

    def disconnect(self) -> None:
        """Disconnect from OBS WebSocket."""
        self.unsubscribe_bridge()
        with self._lock:
            if self._client is not None:
                try:
                    self._client.disconnect()
                except Exception:
                    pass
                self._client = None
            self._connected = False
        self.on_log("[OBS] Disconnected.")

    @property
    def is_connected(self) -> bool:
        with self._lock:
            return self._connected

    # -- bridge subscription -----------------------------------------------

    def subscribe_bridge(self, bridge: AvatarStateBridge) -> None:
        """Subscribe to avatar state bridge for automatic updates."""
        self._state_bridge = bridge
        self._bridge_sub_id = bridge.subscribe(self.on_state_change)
        self.on_log(f"[OBS] Subscribed to avatar state bridge (id={self._bridge_sub_id}).")

    def unsubscribe_bridge(self) -> None:
        """Unsubscribe from avatar state bridge."""
        if self._state_bridge and self._bridge_sub_id is not None:
            self._state_bridge.unsubscribe(self._bridge_sub_id)
            self._bridge_sub_id = None
            self.on_log("[OBS] Unsubscribed from avatar state bridge.")

    # -- state handling ----------------------------------------------------

    def on_state_change(self, state: AvatarState) -> None:
        """Called when avatar state changes. Pushes update to OBS."""
        if not self.is_connected:
            return
        self._update_obs_image(state)

    def _update_obs_image(self, state: AvatarState) -> None:
        """Update the OBS image source for the given avatar state."""
        if not self._client:
            return

        state_key = state.value
        config_path = self._get_image_path_for_state(state_key)
        if config_path is None:
            self.on_log(f"[OBS] No image found for state '{state_key}'")
            return

        try:
            file_path = str(config_path)

            # Get current FULL settings to preserve OBS-required fields (unload, linear, etc.)
            try:
                current = self._client.get_input_settings(name=self.config.source_name)
            except Exception as e:
                self.on_log(f"[OBS] Input '{self.config.source_name}' not found: {e}")
                return

            # Build new settings from existing ones, only changing the image path.
            # OBS image sources need BOTH "file" and "local_file" set to the same path.
            new_settings = dict(current.input_settings)
            new_settings["file"] = file_path
            new_settings["local_file"] = file_path

            # overlay=False replaces ALL settings — this preserves OBS-required fields
            self._client.set_input_settings(
                name=self.config.source_name,
                settings=new_settings,
                overlay=False,
            )
            logger.debug("OBS avatar update: %s -> %s", state_key, file_path)
        except Exception as e:
            self.on_log(f"[OBS] Error updating image for '{state_key}': {e}")
            logger.exception("OBS image update error for state %s", state_key)

    def _get_image_path_for_state(self, state: str) -> Optional[Path]:
        """Get the absolute image path for a given avatar state."""
        configured = self.state_images.get(state)
        if configured and configured.exists():
            return configured

        for ext in _SUPPORTED_IMAGE_EXTENSIONS:
            candidate = self.assets_folder / f"{state}{ext}"
            if candidate.exists():
                return candidate

        # Fallback to idle
        if state != "idle":
            configured_idle = self.state_images.get("idle")
            if configured_idle and configured_idle.exists():
                return configured_idle
            for ext in _SUPPORTED_IMAGE_EXTENSIONS:
                idle_path = self.assets_folder / f"idle{ext}"
                if idle_path.exists():
                    return idle_path

        return None

    # -- manual test -------------------------------------------------------

    def test_connection(self) -> tuple[bool, str]:
        """Test OBS connection and return (success, message)."""
        try:
            import obsws_python as obs
            client = obs.ReqClient(
                host=self.config.host,
                port=self.config.port,
                password=self.config.password or None,
            )
            version = client.get_version()
            obs_version = getattr(version, 'obs_version', '?') or '?'
            client.disconnect()
            return True, f"Conectado a OBS {obs_version}"
        except ImportError:
            return False, "obsws-python no instalado"
        except ConnectionRefusedError:
            return False, f"Conexión rechazada en {self.config.host}:{self.config.port}"
        except Exception as e:
            return False, str(e)

    def set_obs_text(self, source_name: str, text: str) -> bool:
        """Update an OBS Text (GDI+) source with new content.

        The user must create a Text (GDI+) source in OBS with the given name.
        Returns True on success.
        """
        if not self._client or not self.is_connected:
            return False
        try:
            current = self._client.get_input_settings(name=source_name)
            new_settings = dict(current.input_settings)
            new_settings["text"] = text
            self._client.set_input_settings(
                name=source_name,
                settings=new_settings,
                overlay=False,
            )
            return True
        except Exception:
            return False
