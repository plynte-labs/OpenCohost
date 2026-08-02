"""Regression test for the routers<->main import-cycle landmine
(refactor_core_api_20260802 B5 Part B).

Before the fix, every router imported shared objects (helpers, locks,
logger) straight from `opencohost.api.main` at ITS OWN module level.
`main.py`'s own `app = create_app()` (module-load time) lazily imports the
routers package, so that worked as long as `main.py` was the entry point of
the import graph. Importing a router FIRST on a fresh interpreter instead
re-entered `opencohost.api.main`'s top-level execution mid-way through
`opencohost.api.routers/__init__.py`'s own import, raising ImportError
("cannot import name ... from partially initialized module").

Run via subprocess (never `importlib` in-process) so each case gets a truly
fresh `sys.modules` -- the landmine only reproduces on the FIRST import of
the package in a process.
"""

import subprocess
import sys

import pytest

# One wave-1 router (pre-existing B4 cycle) and one wave-2 router (new in
# B5) -- both go through the same `opencohost.api.shared`/`deps` seam now.
_ROUTER_MODULES = ["opencohost.api.routers.obs", "opencohost.api.routers.agent"]


@pytest.mark.parametrize("module", _ROUTER_MODULES)
def test_router_importable_first_on_fresh_interpreter(module):
    result = subprocess.run(
        [sys.executable, "-c", f"import {module}"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_routers_package_importable_first_on_fresh_interpreter():
    """The package `__init__.py` itself (ALL_ROUTERS) must also survive
    being the first thing imported, not just an individual submodule."""
    result = subprocess.run(
        [sys.executable, "-c", "from opencohost.api.routers import ALL_ROUTERS; assert len(ALL_ROUTERS) == 10"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
