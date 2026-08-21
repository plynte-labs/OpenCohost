"""Compatibility shim for the packaged PTT bridge.

The implementation lives in ``opencohost.api.ptt_f10_bridge`` so immutable
engine payloads contain every Tauri-required Python entry point. Existing
developer commands using ``python ptt_f10_bridge.py`` remain supported.
"""

from opencohost.api.ptt_f10_bridge import *  # noqa: F401,F403
from opencohost.api.ptt_f10_bridge import main


if __name__ == "__main__":
    main()
