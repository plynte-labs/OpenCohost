"""One place that knows where the Ollama executable may live.

Used by the readiness probe (is Ollama installed?) and by the eager wake (which
executable to start). They used to keep two copies that drifted: the readiness
copy missed the machine-wide ``Program Files`` install, so a machine-wide install reported
``LOCAL_OLLAMA_MISSING`` while the wake would have found it.
"""

from __future__ import annotations

import os
import shutil
from typing import Optional

_UNIX_FALLBACKS = ("/usr/local/bin/ollama", "/usr/bin/ollama")


def _windows_candidates() -> list[str]:
    candidates: list[str] = []
    local_appdata = os.environ.get("LOCALAPPDATA")
    if local_appdata:
        candidates.append(os.path.join(local_appdata, "Programs", "Ollama", "ollama.exe"))
    program_files = os.environ.get("ProgramFiles")
    if program_files:
        candidates.append(os.path.join(program_files, "Ollama", "ollama.exe"))
    return candidates


def locate_ollama_binary() -> tuple[Optional[str], bool]:
    """(path, found_on_PATH). Order: PATH, per-user install, machine-wide
    install, common UNIX locations. ``(None, False)`` when none exists."""
    on_path = shutil.which("ollama")
    if on_path:
        return (on_path, True)
    for candidate in (*_windows_candidates(), *_UNIX_FALLBACKS):
        if os.path.isfile(candidate):
            return (candidate, False)
    return (None, False)


def find_ollama_executable() -> Optional[str]:
    """The Ollama executable path, or None."""
    return locate_ollama_binary()[0]
