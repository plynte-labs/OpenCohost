"""Entry point: ``python -m opencohost.i18n --set-locale en``."""
from __future__ import annotations

import sys

from opencohost.i18n.cli import main

if __name__ == "__main__":
    sys.exit(main())
