"""Origin of a typed owner turn (runtime-fixes-20261110 T2).

``source`` says what kind of turn it is (``direct`` for a typed owner turn);
``origin`` says which door it came through. It is set only by the server-side
route that received the request, never read from a body, header or query
parameter:

  * ``desktop``: the Tauri app calling ``POST /api/chat/turn``;
  * ``remote``: anything that came through ``opencohost/gateway/``.

Anything else, including a missing value, is NOT desktop (fail closed). The
value rides next to ``source="direct"`` and never replaces it.
"""
from __future__ import annotations

from typing import Optional

DESKTOP = "desktop"
REMOTE = "remote"
_KNOWN = (DESKTOP, REMOTE)


def normalize(value) -> Optional[str]:
    """``desktop``, ``remote`` or None for anything else."""
    return value if isinstance(value, str) and value in _KNOWN else None


def is_desktop(value) -> bool:
    """True only for the exact string ``desktop``."""
    return isinstance(value, str) and value == DESKTOP
