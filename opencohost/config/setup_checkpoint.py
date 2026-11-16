"""Setup-assistant checkpoint (setup-wizard-simple S10): where an interrupted setup resumes.

``{"version": 1, "step": str, "answers": {...}, "updated_at": iso}`` in its own small
JSON file (``settings.SETUP_CHECKPOINT_FILE``), next to the setup status but separate
from it, so a bad checkpoint is moved aside without resetting the status.

- Answers are an explicit allowlist of non-secret wizard choices. Unknown fields
  (an API key, a Tavily key, chat text) are dropped, never stored.
- Writes are atomic (``atomic_write_text``).
- A missing file means "no checkpoint". An unreadable, oversized, invalid or
  unknown-version file is renamed to ``<name>.corrupt-<timestamp>`` and reported
  once through ``LoadResult.reset``; the log line carries the reason, never content.

The path is read from ``settings`` at call time so tests can redirect it.
"""
from __future__ import annotations

import json
import logging
import os
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from opencohost.config import settings
from opencohost.config.storage import atomic_write_text

logger = logging.getLogger("opencohost.config.setup_checkpoint")

CHECKPOINT_VERSION = 1
# A real checkpoint is a few hundred bytes; anything far larger is not ours.
MAX_CHECKPOINT_BYTES = 16 * 1024

ResetReason = Literal["corrupt", "unsupported_version"]


class SetupCheckpointAnswers(BaseModel):
    """Non-secret wizard answers. Unknown keys are dropped (``extra="ignore"``)."""

    model_config = ConfigDict(extra="ignore", protected_namespaces=())

    language: Optional[Literal["es", "en"]] = None
    hear_kira: Optional[bool] = None
    engine: Optional[Literal["gpu", "cpu", "cloud"]] = None
    model: Optional[str] = Field(default=None, max_length=128, pattern=r"^\S+$")
    model_ready: bool = False
    cloud_preset: Optional[str] = Field(default=None, max_length=64, pattern=r"^\S+$")
    cloud_model: Optional[str] = Field(default=None, max_length=128, pattern=r"^\S+$")
    voice: Literal["unknown", "ok", "off"] = "unknown"
    voice_kind: Optional[Literal["piper", "edge"]] = None
    test_verdict: Optional[Literal["passed", "slow", "failed"]] = None
    personalize: Optional[bool] = None
    avatar: Optional[Literal["3d", "2d"]] = None


class SetupCheckpointBody(BaseModel):
    """What a client may send. ``updated_at`` is always set by the server."""

    model_config = ConfigDict(extra="ignore")

    version: Literal[1]
    # A step id from the UI machine; the UI maps an unknown or renamed id itself.
    step: str = Field(min_length=1, max_length=32, pattern=r"^[A-Za-z]+$")
    answers: SetupCheckpointAnswers = Field(default_factory=SetupCheckpointAnswers)


class SetupCheckpoint(SetupCheckpointBody):
    updated_at: str = Field(max_length=64)


@dataclass(frozen=True)
class LoadResult:
    checkpoint: Optional[dict]
    reset: Optional[ResetReason]


def _move_aside(path: str, reason: ResetReason) -> None:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    logger.warning("setup checkpoint unusable (%s); starting over", reason)
    try:
        os.replace(path, f"{path}.corrupt-{stamp}")
    except OSError:
        # Could not keep a copy: drop it so the next read is clean. Never raise.
        with suppress(OSError):
            os.remove(path)


def load_checkpoint() -> LoadResult:
    path = settings.SETUP_CHECKPOINT_FILE
    try:
        with open(path, "rb") as f:
            raw = f.read(MAX_CHECKPOINT_BYTES + 1)
    except FileNotFoundError:
        return LoadResult(None, None)
    except OSError:
        logger.warning("setup checkpoint unreadable; ignoring it")
        return LoadResult(None, None)

    if len(raw) > MAX_CHECKPOINT_BYTES:
        _move_aside(path, "corrupt")
        return LoadResult(None, "corrupt")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        _move_aside(path, "corrupt")
        return LoadResult(None, "corrupt")
    if not isinstance(data, dict):
        _move_aside(path, "corrupt")
        return LoadResult(None, "corrupt")
    version = data.get("version")
    if type(version) is not int or version != CHECKPOINT_VERSION:
        _move_aside(path, "unsupported_version")
        return LoadResult(None, "unsupported_version")
    try:
        checkpoint = SetupCheckpoint.model_validate(data)
    except ValidationError:
        _move_aside(path, "corrupt")
        return LoadResult(None, "corrupt")
    return LoadResult(checkpoint.model_dump(), None)


def save_checkpoint(body: dict) -> dict:
    """Validate, stamp and persist *body* atomically; returns what was stored.

    Raises ``ValueError`` for an invalid body and ``OSError`` when the write fails.
    """
    try:
        parsed = SetupCheckpointBody.model_validate(body)
    except ValidationError as exc:
        raise ValueError("invalid setup checkpoint") from exc
    stored = SetupCheckpoint(
        **parsed.model_dump(),
        updated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    ).model_dump()
    atomic_write_text(settings.SETUP_CHECKPOINT_FILE, json.dumps(stored, indent=2))
    return stored


def clear_checkpoint() -> None:
    """Remove the checkpoint. A missing file is fine; other errors raise ``OSError``."""
    try:
        os.remove(settings.SETUP_CHECKPOINT_FILE)
    except FileNotFoundError:
        pass
