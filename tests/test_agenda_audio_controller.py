"""Module-boundary + delegate-consistency gates for agenda_audio_controller.

Phase 7 (app_shell_agenda_audio_decomposition_20260624). Mirrors
test_motor_event_handlers.py::test_every_handler_name_has_shell_delegate:
the extracted function-module and its thin VocalAIApp delegates must never drift.

Behavior is already pinned by test_agenda_audio_shell_characterization.py,
test_kira_agenda_tick.py, test_audio_teardown.py, test_kira_orchestration_gaps.py
and test_app_shell_obs_resilience.py; this file guards the extraction contract.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path
from types import SimpleNamespace

from opencohost.ui import agenda_audio_controller as ctrl

MODULE_PATH = Path(__file__).resolve().parents[1] / "opencohost" / "ui" / "agenda_audio_controller.py"


def _public_functions() -> list[str]:
    return [
        name
        for name, obj in inspect.getmembers(ctrl, inspect.isfunction)
        if not name.startswith("_") and obj.__module__ == ctrl.__name__
    ]


def test_every_module_function_has_shell_delegate() -> None:
    """Every public controller function must have a '_<name>' delegate on VocalAIApp."""
    from opencohost.ui import app_shell

    app = object.__new__(app_shell.VocalAIApp)
    missing = [
        name for name in _public_functions()
        if not callable(getattr(type(app), "_" + name, None))
    ]
    assert not missing, (
        f"agenda_audio_controller exposes functions with no '_<name>' delegate "
        f"on VocalAIApp: {missing}"
    )


def test_module_never_imports_app_shell_or_heavy_deps() -> None:
    """The controller must stay a leaf module: no app_shell/VocalAIApp/torch/ollama."""
    source = MODULE_PATH.read_text(encoding="utf-8")
    # Strip the docstring's contract prose (it names these tokens to forbid them).
    body = source.split('"""', 2)[-1]
    for banned in ("import opencohost.ui.app_shell", "from opencohost.ui.app_shell",
                   "import torch", "import ollama", "transformers"):
        assert banned not in body, f"agenda_audio_controller must not contain: {banned!r}"


def test_deps_builder_is_module_level_function() -> None:
    """_agenda_audio_deps/_agenda_audio_call must be module-level (not methods) so
    unbound VocalAIApp.<delegate>(SimpleNamespace) calls in test_audio_teardown work."""
    from opencohost.ui import app_shell

    assert inspect.isfunction(app_shell._agenda_audio_deps)
    assert inspect.isfunction(app_shell._agenda_audio_call)
    # Reject a method smuggled in as an attribute on the class.
    assert not hasattr(app_shell.VocalAIApp, "_agenda_audio_deps")


def test_deps_builder_works_on_simplenamespace_shell() -> None:
    """The structural discovery (§0): deps resolve over vars(shell) for a plain
    SimpleNamespace, and the module never duplicates _pending_audio_bed_stop."""
    from opencohost.ui import app_shell

    shell = SimpleNamespace()
    deps = app_shell._agenda_audio_deps(shell)
    # Shared-state getter/setter round-trips through the live instance dict.
    assert deps["get_pending_audio_bed_stop"]() is False
    deps["set_pending_audio_bed_stop"](True)
    assert vars(shell)["_pending_audio_bed_stop"] is True
    assert deps["get_pending_audio_bed_stop"]() is True
    # Missing collaborators degrade to None (hasattr-parity), never AttributeError.
    assert deps["kira_agenda"] is None
    assert deps["audio_bed"] is None


def test_every_public_function_is_keyword_only() -> None:
    """motor_event_handlers convention: one deps dict serves all functions, so
    every public function takes keyword-only args (no positional-or-keyword)."""
    offenders = []
    for name in _public_functions():
        sig = inspect.signature(getattr(ctrl, name))
        for pname, p in sig.parameters.items():
            if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD):
                offenders.append(f"{name}({pname})")
    assert not offenders, f"non-keyword-only params found: {offenders}"
