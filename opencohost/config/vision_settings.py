"""Owner-adjustable vision settings (vision-on-demand V1.5).

The runtime values live as ``VISION_*`` attributes on
``opencohost.config.settings`` (boot defaults, env-overridable). The owner's
choice persists in ``settings.VISION_SETTINGS_FILE``. Mirrors the memory
layer controls: load fails closed per key to the base values (vision stays
off), save is atomic and reports failure, apply sets the module attributes.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, replace
from typing import Any, Mapping, Optional

from opencohost.config import settings

CAPTURE_DELAY_MIN_S = 0.0
CAPTURE_DELAY_MAX_S = settings.VISION_CAPTURE_DELAY_MAX_S
DESCRIBE_TIMEOUT_MIN_S = 1.0
DESCRIBE_TIMEOUT_MAX_S = 30.0
FRAMES_MIN = settings.VISION_FRAMES_MIN
FRAMES_MAX = settings.VISION_FRAMES_MAX
FRAME_INTERVAL_MIN_S = settings.VISION_FRAME_INTERVAL_MIN_S
FRAME_INTERVAL_MAX_S = settings.VISION_FRAME_INTERVAL_MAX_S
PORT_MIN = 1024
PORT_MAX = 65535
_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$")

_ATTRS = {
    "enabled": "VISION_ENABLED",
    "look_on_ptt": "VISION_LOOK_ON_PTT",
    "capture_delay_s": "VISION_CAPTURE_DELAY_S",
    "rule_gate_enabled": "VISION_RULE_GATE_ENABLED",
    "port": "VISION_PORT",
    "model": "VISION_MODEL",
    "describe_timeout_s": "VISION_DESCRIBE_TIMEOUT_S",
    "frames": "VISION_FRAMES",
    "frame_interval_s": "VISION_FRAME_INTERVAL_S",
    "show_thumbnails": "VISION_SHOW_THUMBNAILS",
    "cloud_direct_allowed": "VISION_CLOUD_DIRECT_ALLOWED",
    "vision_source": "VISION_SOURCE",
    "vision_cloud_preset": "VISION_CLOUD_PRESET",
    "vision_cloud_model": "VISION_CLOUD_MODEL",
    "vision_cloud_use_chat_key": "VISION_CLOUD_USE_CHAT_KEY",
}

# cloud-providers-vision P3: where the image is turned into something the chat
# model can use. Kept literal here so this module needs no vision-package import.
VISION_SOURCES = ("local", "chat_model", "cloud_model")


@dataclass(frozen=True)
class VisionSettings:
    enabled: bool
    look_on_ptt: bool
    capture_delay_s: float
    rule_gate_enabled: bool
    port: int
    model: str
    describe_timeout_s: float
    # V1.11 burst. Defaults keep a file saved before V1.11 loadable and match
    # the settings.py code defaults.
    frames: int = 3
    frame_interval_s: float = 1.0
    show_thumbnails: bool = True
    # cloud-providers-vision P2: opt-in to send images straight to a cloud chat
    # model that sees them. Off by default; a file saved before P2 loads as off.
    cloud_direct_allowed: bool = False
    # cloud-providers-vision P3: "local" (Ollama VLM, today), "chat_model" (images
    # go straight to the vision-capable cloud chat model, the P2 opt-in) or
    # "cloud_model" (a separate cloud vision model describes the image). The
    # three cloud fields below apply to "cloud_model" only; its API key is NOT a
    # setting (OS keyring, write-only).
    vision_source: str = "local"
    vision_cloud_preset: str = ""
    vision_cloud_model: str = ""
    vision_cloud_use_chat_key: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _clamp(value: float, lo: float, hi: float) -> float:
    return float(min(hi, max(lo, value)))


def _validated(key: str, value: Any) -> Any:
    """The normalized value for *key*, or None when *value* is invalid."""
    if key in ("enabled", "look_on_ptt", "rule_gate_enabled", "show_thumbnails",
               "cloud_direct_allowed", "vision_cloud_use_chat_key"):
        return value if isinstance(value, bool) else None
    if key == "capture_delay_s":
        return _clamp(value, CAPTURE_DELAY_MIN_S, CAPTURE_DELAY_MAX_S) if _is_number(value) else None
    if key == "describe_timeout_s":
        return _clamp(value, DESCRIBE_TIMEOUT_MIN_S, DESCRIBE_TIMEOUT_MAX_S) if _is_number(value) else None
    if key == "frames":
        ok = isinstance(value, int) and not isinstance(value, bool)
        return int(min(FRAMES_MAX, max(FRAMES_MIN, value))) if ok else None
    if key == "frame_interval_s":
        return _clamp(value, FRAME_INTERVAL_MIN_S, FRAME_INTERVAL_MAX_S) if _is_number(value) else None
    if key == "port":
        ok = isinstance(value, int) and not isinstance(value, bool) and PORT_MIN <= value <= PORT_MAX
        return value if ok else None
    if key == "model":
        ok = isinstance(value, str) and bool(_MODEL_RE.match(value.strip()))
        return value.strip() if ok else None
    if key == "vision_source":
        return value if isinstance(value, str) and value in VISION_SOURCES else None
    if key == "vision_cloud_preset":
        ok = isinstance(value, str) and (value == "" or value in settings.LLM_PROVIDER_PRESETS)
        return value if ok else None
    if key == "vision_cloud_model":
        if not isinstance(value, str):
            return None
        stripped = value.strip()
        return stripped if stripped == "" or _MODEL_RE.match(stripped) else None
    return None


def with_updates(base: VisionSettings, updates: Mapping[str, Any]) -> VisionSettings:
    """*base* with every valid known key of *updates* applied; invalid or
    unknown keys keep the base value."""
    changes = {}
    for key, value in updates.items():
        if key not in _ATTRS:
            continue
        normalized = _validated(key, value)
        if normalized is not None:
            changes[key] = normalized
    # P3: the P2 opt-in and the source are one choice. A file or client that only
    # knows ``cloud_direct_allowed`` keeps its behavior (True = chat_model); an
    # explicit ``vision_source`` wins and drives the opt-in.
    if "vision_source" not in changes and "cloud_direct_allowed" in changes:
        source = changes.get("vision_source", base.vision_source)
        if changes["cloud_direct_allowed"]:
            changes["vision_source"] = "chat_model"
        elif source == "chat_model":
            changes["vision_source"] = "local"
    if "vision_source" in changes:
        changes["cloud_direct_allowed"] = changes["vision_source"] == "chat_model"
    return replace(base, **changes)


def current() -> VisionSettings:
    """The effective runtime values the engine reads."""
    return VisionSettings(**{key: getattr(settings, attr) for key, attr in _ATTRS.items()})


def apply(values: VisionSettings) -> None:
    """Set the runtime ``settings.VISION_*`` attributes from *values*."""
    for key, attr in _ATTRS.items():
        setattr(settings, attr, getattr(values, key))


def load(path: Optional[str] = None, *, base: Optional[VisionSettings] = None) -> VisionSettings:
    """Persisted settings over *base* (default: ``current()``). An absent,
    unreadable or corrupt file returns *base* unchanged, so vision stays off
    unless the owner turned it on."""
    path = path if path is not None else settings.VISION_SETTINGS_FILE
    base = base if base is not None else current()
    try:
        if not os.path.exists(path):
            return base
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return base
    if not isinstance(data, dict):
        return base
    return with_updates(base, data)


def save(values: VisionSettings, path: Optional[str] = None) -> bool:
    """Atomic write (temp file + os.replace). False on any error, so the API
    can refuse a change that would not survive a restart."""
    path = path if path is not None else settings.VISION_SETTINGS_FILE
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(values.to_dict(), f)
        os.replace(tmp, path)
        return True
    except Exception:
        return False
