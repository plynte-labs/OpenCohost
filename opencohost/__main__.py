"""
opencohost/__main__.py

OpenCohost CLI launcher entrypoint.
The legacy CustomTkinter GUI has been retired in favor of OpenCohost Tauri desktop UI.
"""

from __future__ import annotations

import sys


def main() -> None:
    """Inform user about legacy GUI retirement and direct to canonical entrypoints."""
    print("OpenCohost: The legacy CustomTkinter GUI has been retired.")
    print("To launch the headless backend API: opencohost-api (or python -m opencohost.api.cli)")
    print("To launch the modern desktop application: OpenCohost (Tauri)")
    sys.exit(0)


if __name__ == "__main__":
    main()
