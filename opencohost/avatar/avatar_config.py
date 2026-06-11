"""Avatar configuration loader/saver for VoiceAI.

Loads ``config/avatar.yaml``, validates state-image mappings, and manages
a local assets folder where user-selected images are copied to.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import yaml
except Exception:  # pragma: no cover - settings fallback
    yaml = None

from opencohost.config.settings import AVATAR_CONFIG_FILE, BASE_DIR
from opencohost.config.storage import USER_DATA_DIR

AVATAR_CONFIG_FILE = Path(AVATAR_CONFIG_FILE)
DEFAULT_ASSETS_FOLDER = Path(USER_DATA_DIR) / "assets" / "avatar" / "kira"

VALID_STATES = frozenset(
    {"idle", "listening", "thinking", "speaking", "speaking_alt", "sleeping", "angry", "error"}
)

SUPPORTED_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}


@dataclass
class OBSConfig:
    """OBS WebSocket connection configuration."""
    enabled: bool = False
    host: str = "localhost"
    port: int = 4455
    password: str = ""
    source_name: str = "KiraAvatar"
    scene_name: str = ""


@dataclass
class AvatarConfig:
    enabled: bool = True
    mode: str = "image_states"
    assets_folder: Path = field(default_factory=lambda: DEFAULT_ASSETS_FOLDER)
    state_images: dict[str, Path] = field(default_factory=dict)
    obs: OBSConfig = field(default_factory=OBSConfig)

    def get_image_for_state(self, state: str) -> Path | None:
        """Return the configured image path for a state, or None."""
        p = self.state_images.get(state)
        if p and p.exists():
            return p
        # Fallback: if a specific state is missing, try idle
        if state != "idle":
            idle_path = self.state_images.get("idle")
            if idle_path and idle_path.exists():
                return idle_path
        return None


def load_avatar_config(config_file: Path | None = None) -> AvatarConfig:
    """Load avatar configuration from YAML."""
    config_file = config_file or AVATAR_CONFIG_FILE
    raw: dict[str, Any] = {}
    if yaml is not None and config_file.exists():
        try:
            with open(config_file, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            if not isinstance(data, dict):
                return AvatarConfig()
            raw = data.get("avatar", {}) or {}
        except Exception:
            return AvatarConfig()

    assets_folder = DEFAULT_ASSETS_FOLDER
    af = raw.get("assets_folder", "")
    if af and str(af).strip():
        assets_folder = Path(os.path.expandvars(os.path.expanduser(str(af)))).resolve()

    state_images: dict[str, Path] = {}
    for state_name, img_path in (raw.get("state_images") or {}).items():
        if state_name not in VALID_STATES:
            continue
        if img_path and str(img_path).strip():
            state_images[state_name] = Path(
                os.path.expandvars(os.path.expanduser(str(img_path)))
            ).resolve()

    # OBS config
    obs_raw = raw.get("obs") or {}
    obs_config = OBSConfig(
        enabled=bool(obs_raw.get("enabled", False)),
        host=str(obs_raw.get("host", "localhost")),
        port=int(obs_raw.get("port", 4455)),
        password=str(obs_raw.get("password", "")),
        source_name=str(obs_raw.get("source_name", "KiraAvatar")),
        scene_name=str(obs_raw.get("scene_name", "")),
    )

    return AvatarConfig(
        enabled=bool(raw.get("enabled", True)),
        mode=str(raw.get("mode", "image_states")),
        assets_folder=assets_folder,
        state_images=state_images,
        obs=obs_config,
    )


def save_avatar_config(config: AvatarConfig, config_file: Path | None = None) -> None:
    """Persist avatar configuration to YAML."""
    config_file = config_file or AVATAR_CONFIG_FILE
    config_file.parent.mkdir(parents=True, exist_ok=True)

    state_images: dict[str, str] = {}
    for state_name, img_path in config.state_images.items():
        if state_name not in VALID_STATES:
            continue
        state_images[state_name] = str(img_path) if img_path else ""

    data = {
        "avatar": {
            "enabled": config.enabled,
            "mode": config.mode,
            "assets_folder": str(config.assets_folder) if config.assets_folder != DEFAULT_ASSETS_FOLDER else "",
            "state_images": state_images,
            "obs": {
                "enabled": config.obs.enabled,
                "host": config.obs.host,
                "port": config.obs.port,
                "password": config.obs.password,
                "source_name": config.obs.source_name,
                "scene_name": config.obs.scene_name,
            },
        }
    }

    if yaml is None:
        raise RuntimeError("PyYAML is not available; cannot save avatar config.")

    with open(config_file, "w", encoding="utf-8") as f:
        yaml.dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)


def assign_image_to_state(
    config: AvatarConfig,
    state: str,
    source_path: str | Path,
) -> AvatarConfig:
    """Copy *source_path* into the managed assets folder and update config.

    Returns a new AvatarConfig with the updated mapping.
    """
    source = Path(source_path).resolve()
    if not source.exists():
        raise FileNotFoundError(f"Source image not found: {source}")
    if source.suffix.lower() not in SUPPORTED_EXTENSIONS:
        raise ValueError(f"Unsupported image format: {source.suffix}")
    if state not in VALID_STATES:
        raise ValueError(f"Invalid avatar state: {state}")

    config.assets_folder.mkdir(parents=True, exist_ok=True)

    dest_name = f"{state}{source.suffix.lower()}"
    dest = config.assets_folder / dest_name

    # If the same file is already configured, no need to copy
    if source.resolve() != dest.resolve():
        shutil.copy2(str(source), str(dest))

    new_state_images = dict(config.state_images)
    new_state_images[state] = dest
    return AvatarConfig(
        enabled=config.enabled,
        mode=config.mode,
        assets_folder=config.assets_folder,
        state_images=new_state_images,
        obs=config.obs,
    )
