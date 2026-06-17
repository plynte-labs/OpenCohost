"""Utilities for removing OpenCohost-generated temp files safely."""

from __future__ import annotations

import os
from collections.abc import Callable
from logging import Logger
from pathlib import Path


OPENCOHOST_TEMP_PATTERNS = (
    "tts_chunk_*.mp3",
    "tts_chunk_*.wav",
    "out_ligero_*.wav",
    "out_pesado_*.wav",
)


def register_temp_file_cleanup(
    response,
    path: str,
    logger: Logger,
    *,
    exists: Callable[[str], bool] = os.path.exists,
    remove: Callable[[str], None] = os.remove,
):
    """Delete a generated temp file when a Flask response is closed."""

    def cleanup_file() -> None:
        try:
            if exists(path):
                remove(path)
                logger.debug("Removed generated temp audio file: %s", path)
        except OSError as exc:
            logger.warning("Could not remove generated temp audio file %s: %s", path, exc)

    response.call_on_close(cleanup_file)
    return response


def cleanup_opencohost_temp_artifacts(
    temp_root: str | os.PathLike[str],
    logger: Logger,
    *,
    min_age_seconds: float = 60.0,
    now: float | None = None,
    remove: Callable[[str], None] = os.remove,
) -> dict[str, int]:
    """Remove only known OpenCohost-generated temp artifacts."""
    import time

    root = Path(temp_root)
    stats = {"removed": 0, "skipped": 0, "failed": 0}
    if not root.exists() or not root.is_dir():
        return stats

    cutoff = (time.time() if now is None else now) - min_age_seconds
    for pattern in OPENCOHOST_TEMP_PATTERNS:
        for path in root.glob(pattern):
            try:
                if not path.is_file():
                    stats["skipped"] += 1
                    continue
                if min_age_seconds > 0 and path.stat().st_mtime > cutoff:
                    stats["skipped"] += 1
                    continue
                remove(str(path))
                stats["removed"] += 1
                logger.debug("Removed OpenCohost temp artifact: %s", path)
            except OSError as exc:
                stats["failed"] += 1
                logger.warning("Could not remove OpenCohost temp artifact %s: %s", path, exc)
    return stats
