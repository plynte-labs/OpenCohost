"""Bounded immutable audio snapshot, published only by the playback consumer.

The serialized speech consumer is the sole writer. Readers capture one object
reference; bytes/timing/identity cannot tear and no second lock order is added.
"""
from dataclasses import dataclass, replace
from pathlib import Path
import time

MAX_AUDIO_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True)
class VrmAudioSnapshot:
    sequence: int
    data: bytes
    content_type: str
    started_at: float
    active: bool


def prepare_audio(path: str) -> tuple[bytes, str]:
    """Best effort, bounded read before playback; never retain a file path."""
    try:
        with open(path, "rb") as audio:
            data = audio.read(MAX_AUDIO_BYTES + 1)
        if not data or len(data) > MAX_AUDIO_BYTES:
            return b"", "application/octet-stream"
        return data, "audio/mpeg" if Path(path).suffix.lower() == ".mp3" else "audio/wav"
    except Exception:
        return b"", "application/octet-stream"


def publish_started(motor, prepared: tuple[bytes, str]) -> None:
    previous = getattr(motor, "_vrm_audio_snapshot", None)
    sequence = previous.sequence + 1 if previous is not None else 1
    data, content_type = prepared
    motor._vrm_audio_snapshot = VrmAudioSnapshot(
        sequence, data, content_type, time.monotonic(), bool(data)
    )


def publish_ended(motor) -> None:
    previous = getattr(motor, "_vrm_audio_snapshot", None)
    if previous is not None:
        motor._vrm_audio_snapshot = replace(previous, active=False, data=b"")
