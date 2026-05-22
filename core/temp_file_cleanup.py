"""Utilities for removing generated temp files after responses finish."""

from __future__ import annotations

import os
from collections.abc import Callable
from logging import Logger


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
